# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later

"""尾帧接力（tail_relay）：判据真源、gen-video 接线与串行注入闭环。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema.pipeline import tailrelay
from tests.support import LocalBackendEnv


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


class TestArbitration(unittest.TestCase):
    """active / prev_shot / disk_tails 是唯一判据来源，边界逐条钉死。"""

    def test_active_requires_opt_in_and_video_mode(self):
        self.assertFalse(tailrelay.active({}, "native"))
        self.assertTrue(tailrelay.active({"tail_relay": True}, "native"))
        self.assertTrue(tailrelay.active({"tail_relay": True}, "dubbed"))
        self.assertTrue(tailrelay.active({}, "native", override=True))
        # kenburns 不调视频模型——开关写了也不生效
        self.assertFalse(tailrelay.active({"tail_relay": True}, "kenburns"))

    def test_prev_shot_skips_omitted_and_breaks_on_transition(self):
        s1 = {"id": 1}
        s2 = {"id": 2, "review": {"shot": {"state": "omt"}}}
        tr = {"id": 3, "kind": "transition", "transition": {"type": "fade_black"}}
        s4 = {"id": 4}
        s5 = {"id": 5}
        shots = [s1, s2, s4, tr, s5]
        self.assertIsNone(tailrelay.prev_shot(shots, s1), "首镜无承接来源")
        self.assertIs(tailrelay.prev_shot(shots, s4), s1, "弃用镜要跳过继续往前找")
        self.assertIsNone(tailrelay.prev_shot(shots, s5), "转场是场景切换标记，跨转场即断")
        self.assertIsNone(tailrelay.prev_shot(shots, {"id": 9}), "不在清单里的镜返回 None")

    def test_disk_tails_requires_every_target_aspect(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        good = _png(tmp / "t169.png")
        shot = {"gen": {"clip": {"tail_frames": {"16:9": str(good),
                                                 "9:16": str(tmp / "missing.png")}}}}
        self.assertIsNone(tailrelay.disk_tails(shot, ["16:9", "9:16"]),
                          "任一比例不在盘即整体不接力（全称量词）")
        self.assertEqual(tailrelay.disk_tails(shot, ["16:9"]), {"16:9": str(good)})
        self.assertIsNone(tailrelay.disk_tails(None, ["16:9"]))
        self.assertIsNone(tailrelay.disk_tails({"gen": {}}, ["16:9"]))


class _Base(unittest.TestCase):
    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self.addCleanup(self._env.restore)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _project(self, shots, **over):
        from kinema.project import Project
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "profile": "anime", "motion": "native",
               "aspect": "16:9", "shots": shots}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    def _shots(self, n=2):
        out = []
        for i in range(1, n + 1):
            img = _png(self.tmp / f"s{i}.png")
            out.append({"id": i, "dur": 5.0, "image": str(img),
                        "video_prompt": f"镜{i}的运动"})
        return out

    @staticmethod
    def _clear_latches():
        # 「每 provider 只喊一次」的闩锁是模块级的，测试进程内跨用例共享——不清掉，
        # 先跑的用例把消息占走，后跑用例的断言靠执行顺序巧合通过
        from kinema import cli as cli_mod
        for latch in (cli_mod._warned_ski, cli_mod._warned_v2v,
                      cli_mod._warned_lf, cli_mod._warned_tail):
            latch.clear()

    def _dry(self, project, **kw):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        self._clear_latches()
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=True, **kw)
        return buf.getvalue()


class TestDryRun(_Base):
    def test_relay_notes_and_plan_time_tail_injection(self):
        """dry-run 与真发同一计划期口径：盘上有尾帧即入提示词（@图片N 职责句 +
        参考组成短语），没有则注记「生成时注入」；首镜没有承接来源不注记。"""
        shots = self._shots(2)
        tail = _png(self.tmp / "tail_169.png")
        shots[0]["gen"] = {"clip": {"tail_frames": {"16:9": str(tail)}}}
        out = self._dry(self._project(shots, tail_relay=True))
        self.assertIn("承接=镜1尾帧", out)
        self.assertIn("上一镜的收尾画面", out, "尾帧真入计划就必须声明职责")
        self.assertIn("上镜尾帧", out, "参考组成短语要点名尾帧")
        head = out.split("镜2 ·")[0]
        self.assertNotIn("承接=", head, "首镜没有承接来源")

    def test_relay_without_disk_tail_defers_to_injection(self):
        out = self._dry(self._project(self._shots(2), tail_relay=True))
        self.assertIn("承接=镜1尾帧(生成时注入)", out)
        self.assertNotIn("上一镜的收尾画面", out,
                         "计划期没有尾帧就不许在提示词里声明（提示词与实附恒一致）")

    def test_relay_off_by_default(self):
        out = self._dry(self._project(self._shots(2)))
        self.assertNotIn("承接=", out, "尾帧接力是显式 opt-in")

    def test_frame_chain_shots_do_not_relay(self):
        """衔接章参与镜走首帧任务（官方禁混参考图）——尾帧接力对它们不成立。"""
        out = self._dry(self._project(self._shots(2), tail_relay=True,
                                      frame_chain=True))
        self.assertNotIn("承接=", out)

    def test_transition_breaks_relay(self):
        shots = self._shots(2)
        shots.insert(1, {"id": 9, "kind": "transition", "dur": 1.6,
                         "narration": "", "transition": {"type": "fade_black"}})
        out = self._dry(self._project(shots, tail_relay=True))
        self.assertNotIn("承接=", out, "跨转场不承接（场景切换标记）")

    def test_relay_resumes_after_transition_for_later_pairs(self):
        """转场只断它跨的那一处：转场后第一镜承接取消，再往后的相邻镜照常接力
        ——防止有人日后把「遇转场断开」实现成「整章失效」。"""
        shots = self._shots(3)
        shots.insert(1, {"id": 9, "kind": "transition", "dur": 1.6,
                         "narration": "", "transition": {"type": "fade_black"}})
        out = self._dry(self._project(shots, tail_relay=True))
        self.assertNotIn("承接=镜1尾帧", out, "转场跨处的承接必须取消")
        self.assertIn("承接=镜2尾帧", out, "转场之后的相邻镜对照常接力")

    def test_dry_run_names_incapable_provider(self):
        """dry-run 同样点名能力洞：审阅时就该知道这章接不了力，而不是烧完才发现。"""
        from unittest import mock as _mock
        from kinema.providers.video.mock import MockVideoProvider
        project = self._project(self._shots(2), tail_relay=True)
        with _mock.patch.object(MockVideoProvider, "supports_return_last_frame", False), \
                _mock.patch.object(MockVideoProvider, "supports_reference_images", False):
            out = self._dry(project)
        self.assertIn("不支持尾帧接力", out)
        self.assertNotIn("承接=", out)


class TestLiveRelay(_Base):
    """真发路径（mock 拦截）：尾帧回传 → 落盘登记 → 串行注入下一镜。"""

    def _run_live(self, project, with_tail=True, **kw):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video.mock import MockVideoProvider
        _Base._clear_latches()
        store = ConfigStore.load(None)
        calls = []

        def _gen(prov_self, image, out_path, **kwargs):
            calls.append({"image": image, **kwargs})
            meta = {}
            if with_tail and kwargs.get("return_last_frame"):
                # 模拟官方尾帧回传：回一张真实在盘的帧图。with_tail=False 模拟
                # 该型号/任务类型不认 return_last_frame（字段缺失而非报错）
                meta["last_frame_url"] = str(_png(Path(str(out_path) + ".tail.png")))
            # 刻意不写 out_path：假片段过不了 ffprobe，dur 回填分支自然跳过
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False, meta=meta)

        buf = io.StringIO()
        with mock.patch.object(MockVideoProvider, "generate", _gen), \
                contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True), **kw)
        return calls, buf.getvalue()

    def test_tail_captured_registered_and_injected_into_next_shot(self):
        project = self._project(self._shots(2), tail_relay=True)
        calls, out = self._run_live(project)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].get("return_last_frame"), "接力批每镜都要请求尾帧回传")
        s1, s2 = project.shots
        tails = ((s1.get("gen") or {}).get("clip") or {}).get("tail_frames") or {}
        self.assertIn("16:9", tails, "尾帧要落盘登记进 gen.clip.tail_frames")
        self.assertTrue(Path(tails["16:9"]).is_file())
        self.assertIn("_work/tail/", tails["16:9"].replace("\\", "/"))
        # 注入闭环：镜2 的请求参考图携带镜1 尾帧，提示词声明承接职责
        refs2 = calls[1].get("ref_images") or []
        self.assertTrue(any("tail" in str(r) for r in refs2),
                        f"镜2 的 ref_images 应含镜1 尾帧，实际 {refs2}")
        self.assertIn("上一镜的收尾画面", calls[1].get("prompt") or "")
        snap2 = (s2.get("gen") or {}).get("clip") or {}
        self.assertEqual(snap2.get("tail_relay_from"), 1, "留痕：这一版承接自镜1")
        roles = [r.get("role") for r in (snap2.get("envelope") or {}).get("references", [])]
        self.assertIn("tail_frame", roles, "Envelope 引用要含尾帧（可审计可判过期）")
        self.assertIn("已注入镜 1 尾帧承接", out)

    def test_capture_miss_degrades_next_shot_without_claim(self):
        """回传落空的兜底：下一镜不带尾帧、提示词不声明承接，且点名一次。"""
        project = self._project(self._shots(2), tail_relay=True)
        calls, out = self._run_live(project, with_tail=False)
        self.assertEqual(len(calls), 2)
        refs2 = calls[1].get("ref_images") or []
        self.assertFalse(any("tail" in str(r) for r in refs2),
                         f"没有尾帧就不许附，实际 {refs2}")
        self.assertNotIn("上一镜的收尾画面", calls[1].get("prompt") or "",
                         "没附就不许声明——声明一个不存在的参考=向模型索要幻觉")
        s1, s2 = project.shots
        self.assertNotIn("tail_frames", ((s1.get("gen") or {}).get("clip") or {}))
        self.assertNotIn("tail_relay_from", ((s2.get("gen") or {}).get("clip") or {}))
        self.assertIn("未回传尾帧", out, "配置洞要喊出来，不静默降级")

    def test_stale_plan_tail_revoked_when_source_regenerates_without_tail(self):
        """上一镜重生成却没拿到尾帧：计划期按旧版尾帧编入的承接必须撤销——
        带着一张已随版本失效的收尾画面声称「从它延续」比不承接更糟。"""
        shots = self._shots(2)
        old_tail = _png(self.tmp / "old_tail.png")
        shots[0]["gen"] = {"clip": {"tail_frames": {"16:9": str(old_tail)}}}
        project = self._project(shots, tail_relay=True)
        calls, out = self._run_live(project, with_tail=False, force=True)
        self.assertEqual(len(calls), 2)
        refs2 = calls[1].get("ref_images") or []
        self.assertFalse(any(str(old_tail) == str(r) for r in refs2),
                         f"旧版尾帧不许再随请求发出，实际 {refs2}")
        self.assertNotIn("上一镜的收尾画面", calls[1].get("prompt") or "")
        self.assertIn("承接已撤销", out)

    def test_inject_compile_failure_falls_back_to_baseline(self):
        """注入后重编译失败（如超 provider 字数上限）：保持基线提示词发出——
        绝不出现「附了尾帧却没声明」或反过来的中间态。"""
        from kinema.pipeline import prompts as prompts_mod
        from kinema.prompt_contract import PromptContractError
        project = self._project(self._shots(2), tail_relay=True)
        real_video = prompts_mod.PromptCompiler.video

        def flaky(compiler, shot, **kw):
            if any(r.get("role") == "tail_frame"
                   for r in (kw.get("references") or [])):
                raise PromptContractError("超 provider 字数上限（模拟）")
            return real_video(compiler, shot, **kw)

        with mock.patch.object(prompts_mod.PromptCompiler, "video", flaky):
            calls, out = self._run_live(project)
        self.assertEqual(len(calls), 2)
        self.assertIn("重编译失败", out, "编译失败要点名，不静默")
        refs2 = calls[1].get("ref_images") or []
        self.assertFalse(any("tail" in str(r) for r in refs2),
                         "基线提示词不含尾帧声明就不许附尾帧")
        self.assertNotIn("上一镜的收尾画面", calls[1].get("prompt") or "")
        snap2 = (project.shots[1].get("gen") or {}).get("clip") or {}
        self.assertNotIn("tail_relay_from", snap2)

    def test_no_relay_flag_sends_no_return_last_frame(self):
        project = self._project(self._shots(2))
        calls, _out = self._run_live(project)
        self.assertTrue(all("return_last_frame" not in c for c in calls),
                        "非接力批不发 return_last_frame——请求体强校验，多发即险 400")

    def test_relay_forces_serial_when_concurrency_requested(self):
        project = self._project(self._shots(2), tail_relay=True)
        _calls, out = self._run_live(project, concurrency=3)
        self.assertIn("强制串行", out, "注入次序依赖 workers=1 内联串行")

    def test_incapable_provider_auto_ignores_relay(self):
        """能力位不齐（minimax-h3 形态：无尾帧回传、无参考图通道）：接力自动失效
        ——不发回传请求、不注入承接、每 provider 点名一次、且不剥夺并发。"""
        from kinema.providers.video.mock import MockVideoProvider
        project = self._project(self._shots(2), tail_relay=True)
        with mock.patch.object(MockVideoProvider, "supports_return_last_frame", False), \
                mock.patch.object(MockVideoProvider, "supports_reference_images", False):
            calls, out = self._run_live(project, concurrency=2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("return_last_frame" not in c for c in calls),
                        "接不了力就不发回传参数（请求体强校验）")
        self.assertNotIn("上一镜的收尾画面",
                         "".join(str(c.get("prompt") or "") for c in calls))
        self.assertEqual(out.count("不支持尾帧接力"), 1, "每 provider 只喊一次")
        self.assertNotIn("强制串行", out, "整批接不了力就不剥夺并发")
        s1 = project.shots[0]
        self.assertNotIn("tail_frames", ((s1.get("gen") or {}).get("clip") or {}))


class TestMockTailBranch(unittest.TestCase):
    """mock 的尾帧回传分支要真实执行一次——其余用例都把 generate 整个 patch 掉，
    这条分支若坏在 with_suffix/复制细节上，全 patch 的用例永远发现不了。"""

    def test_generate_emits_tail_stub_only_when_requested(self):
        import base64
        import shutil
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        img = tmp / "in.png"
        img.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
            "h6FO1AAAAABJRU5ErkJggg=="))
        from kinema.providers.video.mock import MockVideoProvider
        prov = MockVideoProvider.__new__(MockVideoProvider)
        res = prov.generate(str(img), str(tmp / "clip.mp4"), dur=1.0,
                            width=64, height=64, return_last_frame=True)
        tail = (res.meta or {}).get("last_frame_url")
        self.assertTrue(tail and Path(tail).is_file(), "回传尾帧文件必须真实在盘")
        res2 = prov.generate(str(img), str(tmp / "clip2.mp4"), dur=1.0,
                             width=64, height=64)
        self.assertNotIn("last_frame_url", res2.meta or {},
                         "未请求回传就不许出现该键（与真 provider 形状一致）")


class TestSketchShadowWarning(_Base):
    def test_board_and_beats_shadowed_by_previz_get_named(self):
        """板/beats 在盘、缺省仲裁落 previz——整包静默失效必须逐镜点名。"""
        shots = self._shots(1)
        shots[0]["sketch"] = {"beats": [{"action": "抬手"}, {"action": "转身"}]}
        shots[0]["previz"] = str(self.tmp / "pz.mp4")
        out = self._dry(self._project(shots))
        self.assertIn("缺省仲裁走 previz", out)
        self.assertIn("sketch use", out, "告警要给出可行动项")

    def test_explicit_previz_guide_stays_silent(self):
        shots = self._shots(1)
        shots[0]["sketch"] = {"beats": [{"action": "抬手"}, {"action": "转身"}]}
        shots[0]["previz"] = str(self.tmp / "pz.mp4")
        shots[0]["guide"] = "previz"
        out = self._dry(self._project(shots))
        self.assertNotIn("缺省仲裁走 previz", out, "显式表态=用户点过名，不喊")


if __name__ == "__main__":
    unittest.main()
