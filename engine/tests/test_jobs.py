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

"""Studio 异步任务器（studio/jobs.py）：网页端重新生成/局部改造的后台执行底座。

守卫点：任务成败入表（done/failed + tail）、函数任务结果透传、
有界保留不清 running、未知 id 返回 None——这是「点按钮花钱」链路的地基，
状态错报会让用户对着转圈等一个已失败的任务。
"""
import sys
import time
import unittest

from kinema.studio import jobs


def _wait(jid, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = jobs.status(jid)
        if j and j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("任务超时未结束")


class TestJobRunner(unittest.TestCase):
    def test_spawn_argv_success_captures_output(self):
        jid = jobs.spawn_argv([sys.executable, "-c", "print('job-ok')"],
                              label="t-ok")
        j = _wait(jid)
        self.assertEqual(j["state"], "done")
        self.assertIn("job-ok", j["tail"])
        self.assertEqual(j["code"], 0)

    def test_spawn_argv_failure_marks_failed(self):
        jid = jobs.spawn_argv(
            [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
            label="t-fail")
        j = _wait(jid)
        self.assertEqual(j["state"], "failed")
        self.assertEqual(j["code"], 3)
        self.assertIn("boom", j["tail"])

    def test_running_job_streams_tail_live(self):
        """运行中 tail 就要可见——收口才有内容的话，数分钟的长任务在前端
        只能干转圈，日志一行都看不到。"""
        code = ("import time\n"
                "print('early-line', flush=True)\n"
                "time.sleep(1.5)\n"
                "print('late-line', flush=True)\n")
        jid = jobs.spawn_argv([sys.executable, "-c", code], label="t-stream")
        seen = ""
        t0 = time.time()
        while time.time() - t0 < 10:
            j = jobs.status(jid)
            if j and j["state"] != "running":
                break
            if j and "early-line" in (j["tail"] or ""):
                seen = j["tail"]
                break
            time.sleep(0.05)
        self.assertIn("early-line", seen, "running 期间 tail 未流式更新")
        j = _wait(jid)
        self.assertEqual(j["state"], "done")
        self.assertIn("late-line", j["tail"])

    def test_previz_v2v_job_meta_carries_shots(self):
        """章节级批量任务的 meta.shots 契约：任务是一条、镜有一批，前端分镜卡的
        逐镜忙态恢复只认这份清单（sketch 已有先例，「交给 Seedance」同款）。"""
        from pathlib import Path
        import kinema
        root = Path(kinema.__file__).parent
        src = (root / "studio" / "actions.py").read_text(encoding="utf-8")
        seg = src.split('"kind": "previz_v2v"')[1].split("return")[0]
        self.assertIn('"shots"', seg, "previz_v2v 任务缺 meta.shots——章节视图忙态无从恢复")
        js = (root / "studio_app" / "app" / "chapter.js").read_text(encoding="utf-8")
        self.assertIn("function trackClipJob", js, "前端缺批量片段任务的忙态跟踪器")
        self.assertIn("trackClipJob(pid, cid, j.id, m.shots)", js,
                      "对账循环未把 previz_v2v 任务接进忙态恢复")

    def test_job_timeout_scales_with_shot_count(self):
        """批量任务超时随镜数放宽：1800s 是单镜任务的防僵尸上限，多镜串行的
        「交给 Seedance」被它腰斩=已花的钱打水漂。"""
        self.assertEqual(jobs._job_timeout(None), jobs._TIMEOUT)
        self.assertEqual(jobs._job_timeout({"shots": ""}), jobs._TIMEOUT)
        self.assertEqual(jobs._job_timeout({"shots": "1,2,3"}),
                         max(jobs._TIMEOUT, jobs._PER_SHOT * 3))
        self.assertEqual(jobs._job_timeout({"shots": ",".join(map(str, range(1, 11)))}),
                         jobs._PER_SHOT * 10)

    def test_background_completion_reaches_the_user(self):
        """任务完成时用户可能不在浏览器前：toast 汇聚点必须接后台递达
        （标题「●」亮点标记，刻意不弹系统通知——零权限零打扰），切回页面标题复位。"""
        from pathlib import Path
        import kinema
        root = Path(kinema.__file__).parent / "studio_app"
        core = (root / "app" / "core.js").read_text(encoding="utf-8")
        self.assertIn("function notifyAway", core, "缺后台递达函数")
        self.assertIn("notifyAway(msg, bad)", core.split("function toast(msg")[1],
                      "toast 未接后台递达——切走标签页的用户等不到完成信号")
        app = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn("visibilitychange", app, "缺切回页面的标题标记复位钩子")

    def test_run_fn_result_passthrough_and_exception(self):
        ok = _wait(jobs.run_fn(lambda: {"region": "中中 · 5%"}, label="fn-ok"))
        self.assertEqual(ok["state"], "done")
        self.assertEqual(ok["result"]["region"], "中中 · 5%")
        bad = _wait(jobs.run_fn(lambda: (_ for _ in ()).throw(ValueError("坏了")),
                                label="fn-bad"))
        self.assertEqual(bad["state"], "failed")
        self.assertIn("坏了", bad["tail"])

    def test_unknown_id_none_and_status_is_copy(self):
        self.assertIsNone(jobs.status("no-such-job"))
        jid = jobs.run_fn(lambda: {"x": 1}, label="copy")
        j = _wait(jid)
        j["state"] = "tampered"                     # 改副本不得污染任务表
        self.assertNotEqual(jobs.status(jid)["state"], "tampered")

    def test_bounded_registry_keeps_recent_only(self):
        for k in range(jobs._KEEP + 12):
            _wait(jobs.run_fn(lambda k=k: {"k": k}, label=f"b{k}"))
        finished = [v for v in jobs._JOBS.values() if v["state"] != "running"]
        self.assertLessEqual(len(finished), jobs._KEEP + 1)


class TestActiveReconcile(unittest.TestCase):
    """忙态对账（active + meta）：前端凭 /api/jobs 恢复分镜卡「生成中」——
    meta 定位名片丢失或过滤错位，刷新页面后忙态就凭空消失/张冠李戴。"""

    def test_meta_stored_and_active_filters(self):
        import threading
        gate = threading.Event()
        meta = {"project": "demo", "chapter": "ch01",
                "shot": "3", "kind": "regen"}
        jid = jobs.run_fn(lambda: gate.wait(10) and {}, label="hold", meta=meta)
        try:
            self.assertEqual(jobs.status(jid)["meta"], meta)
            # 命中过滤：项目+章节都对得上才进清单
            hits = jobs.active("demo", "ch01")
            self.assertIn(jid, [j["id"] for j in hits])
            # 错位过滤：别的章节/项目看不到这条任务
            self.assertNotIn(jid, [j["id"] for j in jobs.active("demo", "ch02")])
            self.assertNotIn(jid, [j["id"] for j in jobs.active("other", None)])
        finally:
            gate.set()
        _wait(jid)
        # 收尾后退出进行中清单（active 只报 running）
        self.assertNotIn(jid, [j["id"] for j in jobs.active("demo", "ch01")])

    def test_meta_defaults_empty_dict(self):
        j = _wait(jobs.run_fn(lambda: {}, label="no-meta"))
        self.assertEqual(j["meta"], {})


class TestRegenNote(unittest.TestCase):
    """重生 note 编译：镜上锚定意见自动带方位词汇入（驳回闭环的进料口）——
    意见没有"已解决"中间态，审核通过即被 review.set_state 整体消费删除。"""

    def test_pending_comments_compiled_with_position(self):
        from kinema.studio.actions import _regen_note
        s = {"comments": [
            {"text": "手指画崩了", "x": 0.9, "y": 0.85},
            {"text": "整体太暗"},                          # 无锚点=整体意见
            {"text": "口型对不上", "stage": "audio"},      # 非 image 阶段不进
        ]}
        note = _regen_note(s)
        self.assertIn("手指画崩了（画面下右）", note)
        self.assertIn("整体太暗", note)
        self.assertNotIn("口型对不上", note)

    def test_no_pending_falls_back_plain(self):
        from kinema.studio.actions import _regen_note
        self.assertEqual(_regen_note({"comments": []}), "Studio 重新生成")
        self.assertEqual(_regen_note({}), "Studio 重新生成")


class TestAssetComments(unittest.TestCase):
    """设定图提意见（actions.add_comment/update_comment/regen_asset）：存系列文档资产、
    编译进 regen note，任务**成功后**才按 id 消费（分镜侧是过审即消费；设定图无
    审阅环节，任务成功即视为已应用）——设定图/场景图也有【提意见】通道。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        import tempfile
        from pathlib import Path
        self.env = LocalBackendEnv(); self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.wsp = str(Path(self.tmp.name) / "ws")
        self.ws = Workspace.open(self.wsp)
        self.s = self.ws.create_project("Cat", profile="game_sim")
        self.s.add_character("喵勇者", appearance="橘猫")

    def tearDown(self):
        self.tmp.cleanup(); self.env.restore()

    def test_character_comment_stored_and_compiled(self):
        from kinema.studio import actions
        r = actions.add_comment(self.wsp, self.s.pid, None, text="披风改正红",
                                x=0.5, y=0.7, asset_kind="character", asset_name="喵勇者")
        cat = next(c for c in self.ws.get_project(self.s.pid).characters
                   if c["name"] == "喵勇者")
        self.assertEqual(cat["comments"][0]["text"], "披风改正红")
        note = actions._regen_note({"comments": cat["comments"]})
        self.assertIn("披风改正红", note)
        self.assertIn("下中", note)              # 九宫格方位（y=0.7→下, x=0.5→中）
        actions.update_comment(self.wsp, self.s.pid, None,
                               comment_id=r["comment"]["id"], delete=True,
                               asset_kind="character", asset_name="喵勇者")
        cat2 = next(c for c in self.ws.get_project(self.s.pid).characters
                    if c["name"] == "喵勇者")
        self.assertEqual(cat2["comments"], [])

    def test_scene_comment_pool(self):
        from kinema.studio import actions
        actions.add_comment(self.wsp, self.s.pid, None, text="加点雾",
                            asset_kind="scene")
        self.assertEqual(
            self.ws.get_project(self.s.pid).data["scene_comments"][0]["text"], "加点雾")

    def test_named_scene_comments_stay_on_the_entry(self):
        """具名取景地的意见挂 `scenes[]` 实体条目，与全局场景池、其他取景地互不串
        ——分派丢了 name 的话，给 A 提的意见会出现在 B 的灯箱里，重生任一张
        会拿别人的意见去 refine，且一次消费清空全部。"""
        from kinema.studio import actions
        from kinema.errors import KinemaError
        self.s.add_scene("古城墙")
        self.s.add_scene("桃花林")
        self.s.save()
        actions.add_comment(self.wsp, self.s.pid, None, text="城砖更旧些",
                            asset_kind="scene", asset_name="古城墙")
        actions.add_comment(self.wsp, self.s.pid, None, text="全局图加雾",
                            asset_kind="scene")            # 不带 name = 全局场景图
        p = self.ws.get_project(self.s.pid)
        wall = next(x for x in p.scenes if x["name"] == "古城墙")
        peach = next(x for x in p.scenes if x["name"] == "桃花林")
        self.assertEqual([c["text"] for c in wall["comments"]], ["城砖更旧些"])
        self.assertEqual(peach.get("comments") or [], [])
        self.assertEqual([c["text"] for c in p.data["scene_comments"]], ["全局图加雾"])
        with self.assertRaises(KinemaError):               # 名字打错要报，不落全局
            actions.add_comment(self.wsp, self.s.pid, None, text="x",
                                asset_kind="scene", asset_name="没这个地方")

    def test_regen_asset_without_comment_rebuilds_by_the_original_rules(self):
        """**没提意见也要能重出**——绝不因「没写意见」直接拒绝。

        「没意见」恰恰是最常见的诉求——就是不满意、想按**原本那套版式规则**再抽
        一张。故降级到 `project refs --only <资产> --force`：它走设定图的完整版式
        提示词（sheets 单一真源），比拿一句空指令去 refine 正确得多。
        有意见时仍走 refine（把批注编译进指令、只改该改的地方）。
        """
        from unittest import mock as _m
        from kinema.studio import actions
        sent = []
        with _m.patch("kinema.studio.jobs.spawn_cli",
                      side_effect=lambda args, **kw: (sent.append(args), "j")[1]):
            r = actions.regen_asset(self.wsp, self.s.pid,
                                    kind="character", name="喵勇者", mock=True)
            self.assertEqual(r["mode"], "fresh")
            self.assertIn("--force", sent[-1])
            self.assertIn("refs", sent[-1])
            # 补一条意见 → 改走 refine
            actions.add_comment(self.wsp, self.s.pid, None, text="头发再亮点",
                                asset_kind="character", asset_name="喵勇者")
            r2 = actions.regen_asset(self.wsp, self.s.pid,
                                     kind="character", name="喵勇者", mock=True)
            self.assertEqual(r2["mode"], "refine")
            self.assertIn("refine", sent[-1])

    def test_regen_comments_survive_until_the_job_succeeds(self):
        """批注是「产物确认生成后才可销毁的凭据」：提交时绝不清空——refine 因
        API 报错/余额不足退出时 argv 不落盘、任务表随 Studio 重启即失，提交即
        清等于永久丢失。成功回调按 id 消费，任务运行期间新提的意见原样保留。"""
        from unittest import mock as _m
        from kinema.studio import actions
        actions.add_comment(self.wsp, self.s.pid, None, text="披风改正红",
                            asset_kind="character", asset_name="喵勇者")
        captured = {}
        with _m.patch("kinema.studio.jobs.spawn_cli",
                      side_effect=lambda args, **kw: (captured.update(kw), "j")[1]):
            r = actions.regen_asset(self.wsp, self.s.pid,
                                    kind="character", name="喵勇者", mock=True)
        self.assertEqual(r["mode"], "refine")
        cat = next(c for c in self.ws.get_project(self.s.pid).characters
                   if c["name"] == "喵勇者")
        self.assertEqual(len(cat["comments"]), 1,
                         "提交后批注必须还在——任务失败时它是唯一的重试凭据")
        # 任务运行期间又提了一条 → 成功回调只消费已编译那批
        actions.add_comment(self.wsp, self.s.pid, None, text="耳朵加白毛",
                            asset_kind="character", asset_name="喵勇者")
        captured["on_success"]()
        cat2 = next(c for c in self.ws.get_project(self.s.pid).characters
                    if c["name"] == "喵勇者")
        self.assertEqual([c["text"] for c in cat2["comments"]], ["耳朵加白毛"])

    def test_unknown_asset_kind_raises(self):
        from kinema.studio import actions
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            actions.add_comment(self.wsp, self.s.pid, None, text="x",
                                asset_kind="bogus")


class TestStudioSingleton(unittest.TestCase):
    """Studio 单例 pidfile（server.running_instance）：一工作区只保一实例、陈旧自清，
    防「乱起进程」——历史上 30+ 残留 Studio 就是没这道闸。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_pidfile(self, pid, port=8787):
        import json
        from kinema.studio.server import _pidfile
        pf = _pidfile(self.ws)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(json.dumps({"pid": pid, "port": port, "host": "127.0.0.1"}))
        return pf

    def test_no_pidfile_returns_none(self):
        from kinema.studio.server import running_instance
        self.assertIsNone(running_instance(self.ws))

    def test_stale_pidfile_cleaned(self):
        import subprocess
        from kinema.studio.server import running_instance
        proc = subprocess.Popen(["sleep", "1"]); dead = proc.pid
        proc.terminate(); proc.wait()                     # 该 pid 现已死
        pf = self._write_pidfile(dead)
        self.assertIsNone(running_instance(self.ws))      # 进程已退 → None
        self.assertFalse(pf.is_file())                    # 陈旧 pidfile 被清理

    def test_live_pid_detected_with_url(self):
        import subprocess
        from kinema.studio.server import running_instance
        proc = subprocess.Popen(["sleep", "30"])
        try:
            self._write_pidfile(proc.pid, port=8899)
            inst = running_instance(self.ws)
            self.assertIsNotNone(inst)
            self.assertEqual(inst["pid"], proc.pid)
            self.assertEqual(inst["url"], "http://127.0.0.1:8899")
        finally:
            proc.terminate(); proc.wait()

    def test_other_studio_pids_excludes_self_and_shell(self):
        from kinema.studio.server import other_studio_pids
        # 当前测试进程不是 `-m kinema studio`，不应被计入
        self.assertNotIn(__import__("os").getpid(), other_studio_pids())


if __name__ == "__main__":
    unittest.main()
