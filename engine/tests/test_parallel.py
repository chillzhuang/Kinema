# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""并发生成守卫：并发带来提速，代价是错误不再当场暴露。

这里守四类事故，每一类都不会报错、只会让人在成片里才发现：

1. **串了身份**——甲的图登记到乙头上。并发的完成顺序天生是乱的，任何"按顺序
   对应"的写法在串行时都对、一并发就错。故所有用例都刻意让**完成顺序倒置**。
2. **丢更新**——两个线程各改一半文档再各自整份写盘。守的是"工作线程一行文档
   都不碰"这条铁律（源级 + 行为双验）。
3. **重复付费**——对着业务错误一遍遍重试，或产物已落盘却又买一次。
4. **视频档被静默并发**——Seedance 按秒计费且单价高，并发只许**显式 opt-in**：
   缺省恒串行、不吃环境变量、4K 强制串行、parallel 层重试恒关。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from kinema import parallel
from tests.support import LocalBackendEnv

CLI = Path(__file__).resolve().parents[1] / "kinema" / "cli.py"


# ============================================================ 一、执行器本身
class TestExecutor(unittest.TestCase):
    def test_results_follow_submission_order_not_completion(self):
        """回调与返回值恒按**提交顺序**——并发下完成顺序是乱的，
        按完成顺序回填就是把甲的产物登记给乙。"""
        finished = []

        def mk(i):
            def run():
                time.sleep((6 - i) * 0.02)      # 越靠后越快完成 → 完成顺序倒置
                finished.append(i)
                return i
            return parallel.Task(key=f"t{i}", run=run)

        seen = []
        res = parallel.run([mk(i) for i in range(1, 6)], workers=5,
                           on_done=lambda d: seen.append(d.key))
        self.assertEqual(finished, [5, 4, 3, 2, 1], "没并发起来，本用例失去意义")
        self.assertEqual(seen, ["t1", "t2", "t3", "t4", "t5"])
        self.assertEqual([d.value for d in res], [1, 2, 3, 4, 5])

    def test_single_worker_takes_the_thread_free_path(self):
        """`workers<=1` 必须一个线程都不起——出问题时 `--concurrency 1`
        是退回既有串行行为对拍的唯一逃生舱，它自己再引入线程就没意义了。"""
        names = set()
        parallel.run([parallel.Task(key="a",
                                    run=lambda: names.add(threading.current_thread().name))],
                     workers=1)
        self.assertEqual(names, {threading.current_thread().name})

    def test_concurrency_actually_overlaps(self):
        """并发是真并发：5 件各 0.1s 的活总耗时应远小于串行的 0.5s。"""
        t0 = time.time()
        parallel.run([parallel.Task(key=str(i), run=lambda: time.sleep(0.1))
                      for i in range(5)], workers=5)
        self.assertLess(time.time() - t0, 0.35)

    def test_one_failure_never_kills_the_batch(self):
        """一件炸掉，其余照常完成——并发批量的全部价值就在"其余的照常出"。"""
        def boom():
            raise RuntimeError("invalid parameter: bad prompt")
        tasks = [parallel.Task(key="ok1", run=lambda: 1),
                 parallel.Task(key="bad", run=boom),
                 parallel.Task(key="ok2", run=lambda: 2)]
        res = parallel.run(tasks, workers=3)
        self.assertEqual([d.ok for d in res], [True, False, True])
        self.assertEqual(parallel.summarize(res)["ok"], 2)

    def test_retries_transient_but_never_business_errors(self):
        """**宁可不重试，也不要重试业务错误**：图像 API 按次计费，对着
        「提示词违规」重试三次＝白付三次钱还多触发两次风控。"""
        calls = {"t": 0, "b": 0}

        def transient():
            calls["t"] += 1
            if calls["t"] < 3:
                raise OSError("connection reset by peer")
            return "ok"

        def business():
            calls["b"] += 1
            raise ValueError("content policy violation")

        res = parallel.run([parallel.Task(key="t", run=transient),
                            parallel.Task(key="b", run=business)],
                           workers=2, retries=2, backoff=0.01)
        self.assertTrue(res[0].ok)
        self.assertEqual(calls["t"], 3, "瞬时错误应重试到成功")
        self.assertFalse(res[1].ok)
        self.assertEqual(calls["b"], 1, "业务错误一次都不该重试")

    def test_is_transient_classifier(self):
        for e, want in [(OSError("Connection reset by peer"), True),
                        (RuntimeError("HTTP 503 server error"), True),
                        (RuntimeError("read timed out"), False),   # 已送达，可能已计费
                        (RuntimeError("HTTP 502 bad gateway"), False),
                        (RuntimeError("429 too many requests"), True),
                        (ValueError("invalid parameter: size"), False),
                        (RuntimeError("unauthorized: bad api key"), False),
                        (RuntimeError("内容违规"), False),
                        # 混合文本里业务判据优先——「超时」字样也救不了余额不足
                        (RuntimeError("timeout waiting; 余额不足"), False)]:
            with self.subTest(e=str(e)):
                self.assertEqual(parallel.is_transient(e), want)

    def test_existing_output_salvages_instead_of_paying_again(self):
        """幂等护栏：产物已落盘（provider 收尾记账时才抛）就当成功——
        盲目重试等于为同一张图付两次钱。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.png"
            calls = {"n": 0}

            def half_done():
                calls["n"] += 1
                out.write_bytes(b"\x89PNG")       # 图已经写好了
                raise OSError("connection reset")  # 收尾才炸
            res = parallel.run([parallel.Task(key="a", run=half_done, out=out)],
                               workers=1, retries=3, backoff=0.01)
            self.assertTrue(res[0].ok)
            self.assertEqual(calls["n"], 1, "产物已在盘却又买了一次")
            self.assertTrue(res[0].meta.get("salvaged"))

    def test_should_stop_halts_further_dispatch(self):
        """预算断闸这类信号：已在飞的跑完（钱已花，结果要收），但不再派新活。"""
        ran = []
        stop = {"v": False}
        tasks = [parallel.Task(key=str(i), run=lambda i=i: ran.append(i))
                 for i in range(10)]
        parallel.run(tasks, workers=1, should_stop=lambda: stop["v"],
                     on_done=lambda d: stop.update(v=len(ran) >= 3))
        self.assertEqual(len(ran), 3)

    def test_worker_cap_is_bounded(self):
        """并发度钳到 1~16：再高被 API 限流吃掉，还把一次失败的爆炸半径放大。"""
        self.assertEqual(parallel.resolve_workers(999), parallel.MAX_WORKERS)
        self.assertEqual(parallel.resolve_workers(0), 1)
        self.assertEqual(parallel.resolve_workers(None), parallel.DEFAULT_WORKERS)
        self.assertEqual(parallel.resolve_workers("x"), parallel.DEFAULT_WORKERS)


# ============================================================ 一.5、进度反馈
class TestProgressFeedback(unittest.TestCase):
    """**"按提交顺序消费"必然带来一段静默**：第一件活慢时，后面早跑完的结果
    都排在队里不打印，用户看到的就是"一直卡着毫无反馈"。
    顺序化的只该是**回填**，不该把**进度感**也一起顺序化了。"""

    def test_heartbeat_fires_while_a_slow_head_blocks_the_queue(self):
        beats = []

        def slow_head():
            time.sleep(0.5)

        tasks = [parallel.Task(key="head", run=slow_head, label="慢的那件")] + \
                [parallel.Task(key=str(i), run=lambda: None, label=f"快{i}")
                 for i in range(3)]
        parallel.run(tasks, workers=4, tick=0.1,
                     on_progress=lambda n, t, el, live=(): beats.append((n, tuple(live))))
        self.assertTrue(beats, "head 慢的整段时间里一次进度都没报——正是「卡死」的观感")
        # 心跳要能说清**是谁在拖**，否则用户只知道"还在跑"不知道卡在哪
        self.assertTrue(any("慢的那件" in live for _, live in beats),
                        "心跳没点名在途的那件活")
        self.assertTrue(any(n >= 3 for n, _ in beats),
                        "快的三件早完成了，进度数应当反映出来")

    def test_no_heartbeat_when_everything_is_fast(self):
        """活很快跑完时不该刷屏——心跳是给"等得着急"用的。"""
        beats = []
        parallel.run([parallel.Task(key=str(i), run=lambda: None) for i in range(4)],
                     workers=4, tick=5.0,
                     on_progress=lambda *a, **k: beats.append(a))
        self.assertEqual(beats, [])

    def test_non_tty_printer_only_writes_on_change(self):
        """非 TTY（Studio 把任务 stdout 存成日志文件）每 3 秒刷一次 `\\r`
        会在日志里交错成乱码；只在数字变化时落一行。"""
        buf = io.StringIO()                      # StringIO 不是 tty
        p = parallel.progress_printer("生图", stream=buf)
        p(1, 5, 3.0); p(1, 5, 6.0); p(1, 5, 9.0)   # 数字没变
        self.assertEqual(buf.getvalue().count("\n"), 1)
        p(2, 5, 12.0)
        self.assertEqual(buf.getvalue().count("\n"), 2)
        self.assertNotIn("\r", buf.getvalue())

    def test_close_line_separates_success_from_failure(self):
        """「5/5 完成」在四镜失败时会被读成五件都出了：有失败的批次把成功与
        失败数分开报，全成功才说「完成」。"""
        buf = io.StringIO()
        p = parallel.progress_printer("图生视频", stream=buf)
        p.close(5, 5, 10.0, failed=4)
        self.assertIn("5/5 处理完 · 成功 1 · 失败 4", buf.getvalue())
        buf2 = io.StringIO()
        parallel.progress_printer("图生视频", stream=buf2).close(5, 5, 10.0)
        self.assertIn("5/5 完成", buf2.getvalue())

    def test_run_reports_failed_count_to_close(self):
        buf = io.StringIO()

        def boom():
            raise RuntimeError("x")
        parallel.run([parallel.Task(key="a", run=lambda: 1),
                      parallel.Task(key="b", run=boom)],
                     workers=1, retries=0,
                     on_progress=parallel.progress_printer("生图", stream=buf))
        self.assertIn("2/2 处理完 · 成功 1 · 失败 1", buf.getvalue())

    def test_refs_stream_results_instead_of_batching_them(self):
        """设定图的逐项「✓」必须在 `_apply` 里随完成打印。攒到 `parallel.run`
        之后的回填循环里，就是十几张图跑几分钟一行不出。"""
        src = CLI.read_text(encoding="utf-8")
        seg = src.split("def cmd_gen_refs(")[1].split("\ndef ")[0]
        apply_body = seg.split("    def _apply(d: parallel.Done):")[1].split("\n    results =")[0]
        self.assertIn("_info(f\"{zh}{who}: ✓\")", apply_body,
                      "逐项成功没有随完成流式打印")
        back = seg.split("# ── ③ 主线程回填")[1].split("# ── 第二波")[0]
        self.assertNotIn("_info(", back, "回填循环又打印了一遍（会重复刷屏）")
        self.assertIn("with s.commit():", back,
                      "回填必须在锁内按磁盘最新副本合并，不得整份覆写启动时的快照")
        # 第二波（场景俯视图）同一条纪律：逐项「✓」仍由 _apply 流式打印，
        # 这一段只写文档——它跑的是同一个 `_apply`，再打一遍就是重复刷屏
        wave2 = seg.split("results += _run_wave(top_plan")[1].split(
            "rep = parallel.summarize")[0]
        self.assertNotIn("_info(", wave2, "俯视图回填又打印了一遍")
        self.assertIn("with s.commit():", wave2, "俯视图回填未走锁内重读")


# ============================================================ 二、接线纪律（源级）
class TestWiring(unittest.TestCase):
    src = CLI.read_text(encoding="utf-8")

    def test_seedance_stage_concurrency_is_explicit_optin(self):
        """gen-video 的并发是**显式 opt-in**：缺省 workers=1（与老串行行为一致）、
        不吃 KINEMA_CONCURRENCY 环境变量——给生图配的全局并发不该顺手把 1 元/秒
        的视频档也并发了；4K 档并发配额 1，点了并发也压回串行；parallel 层重试
        恒关（自动重试一次 = 为同一片段付两次钱）。"""
        seg = self.src.split("def stage_gen_video(")[1].split("\ndef ")[0]
        self.assertIn("1 if concurrency is None else parallel.resolve_workers", seg,
                      "缺省必须恒串行（且不吃环境变量），显式 --concurrency 才并发")
        self.assertIn("retries=0", seg, "视频阶段的 parallel 层自动重试必须恒关")
        self.assertIn('"4k"', seg, "缺 4K 强制串行的判据")
        self.assertIn("parallel.run", seg, "图生视频未接三段式并发层")
        self.assertIn("should_stop", seg, "缺预算/失败断闸接线")
        # argparse 侧：共用开关（缺省4）不得挂给 gen-video——它有专用注册（缺省1）
        self.assertIn('if name == "gen-image":\n', self.src)
        vid = self.src.split('sp.add_argument("--previz"')[1].split("if name ==")[0]
        self.assertNotIn("add_concurrency", vid)
        self.assertIn('"--concurrency"', vid, "gen-video 缺专用 --concurrency 注册")

    def test_disk_resume_prevents_double_billing(self):
        """断点续跑判据必须含磁盘口径：已付费落盘但回填前中断的产物直接登记，
        不再买一次（文档字段只反映「登记过」，不反映「花过钱」）。
        gen-video 按秒计费更贵，另须过 ffprobe 完整性验证——半截 mp4 不捡。"""
        img = self.src.split("def stage_gen_image(")[1].split("\ndef ")[0]
        self.assertIn("dst.is_file() and dst.stat().st_size > 0", img,
                      "gen-image 缺盘上续跑判据")
        salv = self.src.split("def _salvageable_clip(")[1].split("\ndef ")[0]
        self.assertIn("probe_duration(p)", salv,
                      "gen-video 断点捡回缺 ffprobe 完整性验证")
        vid = self.src.split("def stage_gen_video(")[1].split("\ndef ")[0]
        self.assertIn("_salvageable_clip(project", vid, "gen-video 真跑未用共用捡回判据")
        self.assertIn('"reuse": True', vid, "gen-video 缺登记复用通道")

    def test_preflight_and_real_run_share_the_salvage_predicate(self):
        """事前闸与真跑必须用**同一份**「盘上待登记片段」判据。各写一份时预估会把
        不需要再买的比例算进报价，预算够的批次反而被 `_preflight_spend` 整批拦死。"""
        burn = self.src.split("def _will_burn(")[1].split("\ndef ")[0]
        self.assertIn("_salvageable_clip(project", burn, "事前闸漏了盘上捡回这道闸")
        salv = self.src.split("def _salvageable_clip(")[1].split("\ndef ")[0]
        self.assertNotIn("subdir(", salv, "事前闸是纯只读预演，判据不得建目录")

    def test_workers_never_touch_the_document(self):
        """铁律：工作线程只产文件。任何一个阶段的 `_work` 里出现 save/add_cost/
        mark/review，就是把丢更新写进了架构（gen-image/tts/gen-video 三处同查）。"""
        parts = self.src.split("    def _work(item):")[1:]
        self.assertEqual(len(parts), 3, "预期恰好三个 _work（gen-image/tts/gen-video）")
        for part in parts:
            body = part.split("\n    failed:")[0]
            for banned in ("project.save(", ".add_cost(", "mark(s,", "review.mark_generated",
                           "lineage.record_refs", "consistency_mod.invalidate"):
                self.assertNotIn(banned, body, f"工作线程里出现了文档写操作：{banned}")

    def test_video_bookkeeping_lives_in_the_apply_callback(self):
        """gen-video 的回填/记账/审阅/落盘同样全在主线程回调里（与 gen-image 同款
        三段式），时长回填也在主线程——写的是 clips/dur 文档字段。"""
        seg = self.src.split("def stage_gen_video(")[1].split("\ndef ")[0]
        apply_body = seg.split("    def _apply(d: parallel.Done):")[1] \
                        .split("\n    parallel.run(")[0]
        for want in ("project.add_cost", "review.mark_generated", "project.save()",
                     "consistency_mod.invalidate", "billable_seconds"):
            self.assertIn(want, apply_body, f"gen-video 主线程回调里缺 {want}")

    def test_stages_mark_wip_before_dispatch(self):
        """Studio「生成中」忙态以 review=wip 为数据源：三个生成阶段都必须在派活前
        置 wip、失败/未真生成时恢复原条目（成功由 mark_generated 推进 wfa）——
        不写入，前端已备好的遮罩/看板列/时间线配色就永远是空的。"""
        for fn_name in ("stage_gen_image", "stage_tts", "stage_gen_video"):
            seg = self.src.split(f"def {fn_name}(")[1].split("\ndef ")[0]
            self.assertIn("_mark_wip(project,", seg, f"{fn_name} 缺派活前的 wip 标记")
            self.assertIn("_unmark_wip(", seg, f"{fn_name} 缺 wip 恢复")

    def test_bookkeeping_lives_in_the_apply_callback(self):
        """反向确认：记账/血缘/审阅/落盘确实都在主线程回调里。"""
        seg = self.src.split("    def _apply(d: parallel.Done):")[1].split("\n    def _tasks(")[0]
        for want in ("project.add_cost", "lineage.record_refs", "review.mark_generated",
                     "project.save()", "consistency_mod.invalidate"):
            self.assertIn(want, seg, f"主线程回调里缺 {want}")

    def test_anchor_mode_serializes_the_first_shot(self):
        """首镜强锚是**真实的串行依赖**：后续镜要拿首镜成品当参考图，
        并发发出去时首镜还没落地 → 锚点为空、整章一致性静默失效。"""
        seg = self.src.split("def stage_gen_image(")[1].split("\ndef ")[0]
        self.assertIn("if anchor_on and plan:", seg)
        self.assertIn("workers=1", seg, "首镜必须单独串行跑完再并发其余")

    def test_refs_plan_phase_has_the_side_effects(self):
        """归档旧设定图（`archive_asset_sheet` 会移动文件+改 JSON）必须留在
        主线程的计划阶段，绝不能进工作线程。"""
        seg = self.src.split("def cmd_gen_refs(")[1].split("\ndef ")[0]
        plan_part = seg.split("def _task(item):")[0]
        self.assertIn("archive_asset_sheet(", plan_part)
        worker = seg.split("def _task(item):")[1].split("def _apply(")[0]
        self.assertNotIn("archive_asset_sheet", worker)
        self.assertNotIn("s.save()", worker)

    def test_tts_anchor_preheat_is_deduped_and_before_dispatch(self):
        """音色锚定预热：去重集合**并发**预热（每把声音只合成一次参考音），
        且必须完成在 wip 派活/主并发之前——锚是逐句合成的输入。"""
        seg = self.src.split("def stage_tts(")[1].split("\ndef ")[0]
        self.assertIn("_anchor_want", seg, "缺预热收集集合（去重保序）")
        self.assertIn("def _preheat(", seg, "缺并发预热函数")
        self.assertLess(seg.index("def _preheat("), seg.index("_mark_wip(project,"),
                        "预热必须先于派活")

    def test_compose_segment_pass_is_concurrent(self):
        """compose 第一遍逐镜片段是纯本地 ffmpeg、各写各的 shot_*.mp4——必须走
        并发层重渲过期片段，且**不给幂等护栏**（Task.out）：编码中断留下的半截
        mp4 会被护栏当成品编进成片。孤儿清理保持在整片合成成功之后。"""
        import kinema
        src = (Path(kinema.__file__).parent / "pipeline"
               / "compose.py").read_text(encoding="utf-8")
        first = src.split("# 第一遍")[1].split("# 第二遍")[0]
        self.assertIn("parallel.run", first, "片段渲染未接并发层")
        self.assertIn("retries=0", first, "本地渲染失败不该自动重试（非瞬时错误）")
        task_call = first.split("parallel.Task(")[1].split("for j in")[0]
        self.assertNotIn(" out=", task_call,
                         "片段渲染不得挂幂等护栏（半截 mp4 会被当成品）")
        self.assertLess(src.index('run(args, desc=f"compose'),
                        src.index("_sweep_orphan_clips(clips_dir, clip_paths)"),
                        "孤儿清理必须在整片合成成功之后")


# ============================================================ 二.5、wip 过渡态与轮询心跳
class TestWipHelpers(unittest.TestCase):
    """wip 过渡态的进出契约：进=留存原审阅条目，出=原样放回。
    直接回写 todo/retake 会吞掉 retake 的意见与时间戳——必须整条目留存。"""

    class _P:
        def save(self):
            pass

    def test_roundtrip_preserves_prior_review_entry(self):
        from kinema import cli
        s = {"id": 1, "review": {"clip": {"state": "retake",
                                          "note": "第3秒左手穿模", "at": "t0"}}}
        item = {"shot": s}
        cli._mark_wip(self._P(), [item], "clip")
        self.assertEqual(s["review"]["clip"]["state"], "wip")
        cli._unmark_wip(s, "clip", item)
        self.assertEqual(s["review"]["clip"]["state"], "retake")
        self.assertEqual(s["review"]["clip"]["note"], "第3秒左手穿模")
        self.assertEqual(s["review"]["clip"]["at"], "t0")

    def test_unmark_removes_entry_when_none_existed(self):
        from kinema import cli
        s = {"id": 1}
        item = {"shot": s}
        cli._mark_wip(self._P(), [item], "image")
        self.assertEqual(s["review"]["image"]["state"], "wip")
        cli._unmark_wip(s, "image", item)
        self.assertNotIn("image", s.get("review") or {})

    def test_unmark_leaves_progressed_states_alone(self):
        from kinema import cli
        s = {"id": 1, "review": {"image": {"state": "wfa", "at": "t1"}}}
        cli._unmark_wip(s, "image", {"shot": s, "review_was": None})
        self.assertEqual(s["review"]["image"]["state"], "wfa")


class TestPollHeartbeat(unittest.TestCase):
    """轮询心跳：按墙钟节流（轮询间隔各家不同，按次数节流会跟着 provider 走），
    首拍不立即打——任务号那一行刚打完，紧跟一行心跳是噪音。"""

    def test_throttles_by_wall_clock(self):
        from kinema.providers._util import poll_heartbeat
        buf = io.StringIO()
        beat = poll_heartbeat("任务 t-1", interval=0.05)
        with redirect_stdout(buf):
            beat()                        # 未到间隔：静默
            self.assertEqual(buf.getvalue(), "")
            time.sleep(0.06)
            beat()                        # 过了间隔：落一行「已等待 Ns」
            beat()                        # 间隔内再拍：不重复
        out = buf.getvalue()
        self.assertIn("已等待", out)
        self.assertIn("任务 t-1", out)
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn("\r", out)       # 整行输出，进任务日志/混排输出不踩踏


# ============================================================ 三、端到端一一对应
@unittest.skipUnless(CLI.is_file(), "缺 cli")
class TestIdentityUnderConcurrency(unittest.TestCase):
    """完成顺序**倒置**时，每个角色/每一镜仍拿到自己那张图。"""

    def setUp(self):
        self._env = LocalBackendEnv(); self._env.enable()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); self._env.restore()

    def _run(self, *a):
        from kinema.cli import build_parser
        ns = build_parser().parse_args(list(a))
        with redirect_stdout(io.StringIO()):
            ns.func(ns)

    def _reverse_order_provider(self, weight):
        """让完成顺序与提交顺序相反的 mock provider。返回 (还原函数, 完成序列)。"""
        from kinema.providers.image import mock as mockmod
        orig = mockmod.MockImageProvider.generate
        seen = []

        def slow(inner_self, prompt, out_path, **kw):
            time.sleep(weight(str(kw.get("label", ""))))
            seen.append(str(kw.get("label", "")))
            return orig(inner_self, prompt, out_path, **kw)
        mockmod.MockImageProvider.generate = slow
        return (lambda: setattr(mockmod.MockImageProvider, "generate", orig)), seen

    def test_character_sheets_never_swap(self):
        ws = str(self.tmp / "project")
        names = ["甲", "乙", "丙", "丁", "戊"]
        self._run("project", "new", "--title", "并发", "--id", "p1",
                  "--profile", "anime", "--workspace", ws)
        for n in names:
            self._run("character", "add", "p1", "--name", n,
                      "--appearance", f"外貌{n}", "--workspace", ws)
        w = {n: (len(names) - i) * 0.03 for i, n in enumerate(names)}
        restore, seen = self._reverse_order_provider(
            lambda lbl: w.get(lbl.split()[-1], 0.01))
        try:
            self._run("project", "refs", "p1", "--mock", "--concurrency", "5",
                      "--workspace", ws)
        finally:
            restore()
        got = [x.split()[-1] for x in seen if x.startswith("CHAR")]
        self.assertEqual(got, list(reversed(names)), "完成顺序没倒置，用例失去意义")
        doc = json.loads((self.tmp / "project" / "p1" / "project.json").read_text())
        for c in doc["characters"]:
            self.assertIn(c["name"], Path(c["sheet"]).name,
                          f"角色 {c['name']} 的设定图串成了 {c['sheet']}")

    def test_shot_images_never_swap(self):
        ws = str(self.tmp / "project")
        self._run("project", "new", "--title", "镜并发", "--id", "p2",
                  "--profile", "anime", "--workspace", ws)
        self._run("chapter", "new", "p2", "--title", "第一章", "--workspace", ws)
        cf = self.tmp / "project" / "p2" / "chapters" / "ch01.json"
        doc = json.loads(cf.read_text())
        doc["shots"] = [{"id": i, "dur": 3.0, "narration": f"台词{i}",
                         "image_prompt": f"画面{i}"} for i in range(1, 7)]
        cf.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        def weight(lbl):
            m = re.match(r"SHOT (\d+)", lbl)
            return (7 - int(m.group(1))) * 0.03 if m else 0.01
        restore, seen = self._reverse_order_provider(weight)
        try:
            self._run("gen-image", "--chapter", "p2/ch01", "--mock",
                      "--concurrency", "6", "--workspace", ws)
        finally:
            restore()
        ids = [int(re.match(r"SHOT (\d+)", x).group(1)) for x in seen]
        self.assertEqual(ids, list(range(6, 0, -1)), "完成顺序没倒置，用例失去意义")
        got = json.loads(cf.read_text())
        for s in got["shots"]:
            self.assertEqual(Path(s["image"]).name, f"shot_{s['id']}.png",
                             f"镜 {s['id']} 的图串了")
            # 每镜的审阅/版本登记也要各自落对（并发下最容易漏的一层）
            self.assertEqual((s["review"]["image"] or {}).get("state"), "wfa")
            self.assertEqual((s["gen"]["image"] or {}).get("version"), 1)


    def test_serial_and_concurrent_produce_identical_documents(self):
        """**最强的一条**：`--concurrency 1` 与 `--concurrency 8` 跑出来的章节文档
        必须逐字段相同（抹掉时间戳后）。并发引入的任何顺序依赖、竞态或漏登记，
        都会在这里现形——而单看某一次并发结果是看不出来的。"""
        import copy

        def once(pid, workers):
            ws = str(self.tmp / pid)
            self._run("project", "new", "--title", "T", "--id", "t",
                      "--profile", "anime", "--workspace", ws)
            self._run("chapter", "new", "t", "--title", "C", "--workspace", ws)
            cf = Path(ws) / "t" / "chapters" / "ch01.json"
            doc = json.loads(cf.read_text())
            doc["shots"] = [{"id": i, "dur": 3.0, "narration": f"第{i}句",
                             "image_prompt": f"图{i}"} for i in range(1, 8)]
            cf.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            self._run("gen-image", "--chapter", "t/ch01", "--mock",
                      "--concurrency", str(workers), "--workspace", ws)
            self._run("tts", "--chapter", "t/ch01", "--mock",
                      "--concurrency", str(workers), "--workspace", ws)
            return json.loads(cf.read_text()), ws

        def scrub(d, ws):
            s = json.dumps(d, ensure_ascii=False, sort_keys=True).replace(ws, "WS")
            s = re.sub(r'"(at|created_at|updated_at)":\s*"[^"]*"', '"\\1":"T"', s)
            return json.loads(s)

        a, wa = once("ser", 1)
        b, wb = once("par", 8)
        self.assertEqual(scrub(a, wa), scrub(copy.deepcopy(b), wb),
                         "串行与并发产出的文档不一致——并发引入了顺序依赖或漏登记")

    def test_budget_break_never_loses_a_paid_registration(self):
        """`add_cost` 是「先入账再抛」。若记账排在登记之前，超限那一刻这张
        **已生成已付费**的图就会丢掉登记——重跑时同一张再买一次。"""
        src = CLI.read_text(encoding="utf-8")
        seg = src.split("    def _apply(d: parallel.Done):")[1].split("\n    def _tasks(")[0]
        self.assertLess(seg.index('review.mark_generated(s, "image")'),
                        seg.index('project.add_cost("image"'),
                        "记账排在了登记前面：超限时会丢掉已付费那张的登记")
        self.assertIn('budget_stop["err"] = e', seg,
                      "超限应只置停派标志、让本批收尾跑完（在飞的钱已经花了）")

    def test_reference_sheets_enter_the_series_ledger_not_a_chapter(self):
        """设定图是**系列级**资产：费用进 Series.add_cost，不摊到某一章的台账。"""
        src = CLI.read_text(encoding="utf-8")
        seg = src.split("def cmd_gen_refs(")[1].split("\ndef ")[0]
        code = "\n".join(ln for ln in seg.splitlines()
                         if not ln.strip().startswith("#"))
        self.assertIn('s.add_cost("image"', code)
        self.assertNotIn("project.add_cost(", code)


if __name__ == "__main__":
    unittest.main()


# ============================================================ 四、Studio 并发写
class TestSeriesCommit(unittest.TestCase):
    """`Series.save()` 是**无合并的整份覆写**，而 Studio 是 ThreadingHTTPServer、
    且好几个写操作要跑十几秒（角色试音 5 条 TTS）。两个请求各拿一份副本、各跑各的、
    再各自整份写回——后写的把先写的整段抹掉，**且不报任何错**。

    典型失败序列：给角色 A 生成试音（mp3 已落盘）→ 期间给 B 也点了试音 →
    B 先完成、A 后完成用旧副本覆盖 → B 的音频文件都在，`audition` 字段却空了 →
    点「选定」报「没有编号 1 的试音（现有: 无）」。
    """

    def setUp(self):
        self._env = LocalBackendEnv(); self._env.enable()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); self._env.restore()

    def _series(self):
        from kinema.workspace import Workspace
        ws = Workspace.open(str(self.tmp / "project"))
        s = ws.create_project("并发", pid="cc", profile="anime")
        s.add_character("甲"); s.add_character("乙"); s.save()
        return str(self.tmp / "project")

    def test_concurrent_long_writes_do_not_clobber_each_other(self):
        from kinema.workspace import Workspace
        root = self._series()

        def work(name, delay):
            # 各自 get_project（与 Studio 每个请求一份副本完全一致）
            s = Workspace.open(root, create=False).get_project("cc")
            time.sleep(delay)                       # 模拟十几秒的合成
            with s.commit():                        # 登记收进锁 + 进锁重载
                c = next(x for x in s.characters if x["name"] == name)
                c["audition"] = [{"no": 1, "voice": f"v-{name}"}]

        ts = [threading.Thread(target=work, args=a) for a in (("甲", 0.30), ("乙", 0.05))]
        [t.start() for t in ts]; [t.join() for t in ts]
        got = Workspace.open(root, create=False).get_project("cc")
        for c in got.characters:
            self.assertTrue(c.get("audition"),
                            f"角色 {c['name']} 的试音被并发写抹掉了")

    def test_commit_reloads_so_stale_copies_cannot_win(self):
        """进锁后必须**重新加载**：只加锁不重载，手里那份旧副本照样整份覆盖。"""
        from kinema.workspace import Workspace
        root = self._series()
        stale = Workspace.open(root, create=False).get_project("cc")   # 早早拿到的旧副本
        other = Workspace.open(root, create=False).get_project("cc")
        with other.commit():                                            # 别人先写了一笔
            next(x for x in other.characters if x["name"] == "甲")["audition"] = [{"no": 1}]
        with stale.commit():                                            # 旧副本再写另一个角色
            next(x for x in stale.characters if x["name"] == "乙")["audition"] = [{"no": 1}]
        got = Workspace.open(root, create=False).get_project("cc")
        self.assertTrue(next(c for c in got.characters if c["name"] == "甲").get("audition"),
                        "commit 没重载：旧副本把别人先写的那笔覆盖了")

    def test_slow_generation_stays_outside_the_lock(self):
        """契约：耗时生成必须在 `with` 之外——放进锁里两个试音会串成两倍时长。
        两条试音路（模版/定制）各跑十几秒，任一条犯规都足以让并发退化成串行。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "voicebank.py"
               ).read_text(encoding="utf-8")
        for fn in ("def audition(", "def custom_audition("):
            seg = src.split(fn)[1].split("\ndef ")[0]
            self.assertLess(seg.index("prov.synthesize("), seg.index("with series.commit():"),
                            f"{fn} 把合成放进了 commit 锁里，两个实体的试音会互相排队")
