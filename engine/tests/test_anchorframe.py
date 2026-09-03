# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later

"""首帧锚定（anchor_frame）：判据真源、gen-video 接线与三条让位通道。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from kinema.pipeline import anchorframe
from tests.support import LocalBackendEnv


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


class TestArbitration(unittest.TestCase):
    """判据只有一个来源，且边界与 framechain/tailrelay 同制逐条钉死。"""

    def test_active_requires_opt_in(self):
        self.assertFalse(anchorframe.active({}, "native"),
                         "缺省档是全能参考——字段缺席绝不静默切换请求拓扑")
        self.assertTrue(anchorframe.active({"anchor_frame": True}, "native"))
        self.assertTrue(anchorframe.active({}, "native", override=True))
        self.assertFalse(anchorframe.active({"anchor_frame": False}, "native"))

    def test_only_native_can_anchor(self):
        # dubbed 恒走参考媒体（ref_audio 与首帧互斥）；kenburns 不调视频模型
        for m in ("dubbed", "kenburns"):
            self.assertFalse(anchorframe.active({"anchor_frame": True}, m), m)
            self.assertFalse(
                anchorframe.anchored({}, {"anchor_frame": True}, m), m)
            self.assertFalse(anchorframe.active({}, m, override=True), m)

    def test_shot_opt_in_is_independent_of_chapter(self):
        self.assertTrue(anchorframe.shot_opt_in({"anchor_frame": True}))
        self.assertFalse(anchorframe.shot_opt_in({}))
        self.assertFalse(anchorframe.shot_opt_in(None))
        # 镜级表态不需要章级同时开
        self.assertTrue(anchorframe.anchored({}, {"anchor_frame": True}, "native"))
        # 章级开则逐镜皆锚，镜上不必再写一份
        self.assertTrue(
            anchorframe.anchored({"anchor_frame": True}, {"id": 1}, "native"))


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
                        "video_prompt": f"镜{i}向前迈一步后停住"})
        return out

    def _dry(self, project, **kw):
        from kinema import cli as cli_mod
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        for latch in (cli_mod._warned_ski, cli_mod._warned_v2v,
                      cli_mod._warned_lf, cli_mod._warned_tail):
            latch.clear()
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=True, **kw)
        return buf.getvalue()


class TestGenVideoWiring(_Base):
    """缺省档与锚定档在 dry-run 上的可见差异——报价清单是人工审阅这道闸的全部依据。"""

    def test_default_stays_on_all_reference(self):
        out = self._dry(self._project(self._shots(1)))
        self.assertIn("全能参考", out)
        self.assertNotIn("首帧锚定", out, "缺省档绝不出现锚定标记")

    def test_chapter_opt_in_switches_every_shot(self):
        out = self._dry(self._project(self._shots(2), anchor_frame=True))
        self.assertIn("首帧锚定(章级)", out, "章级开关要在阶段抬头说清")
        self.assertEqual(out.count("首帧锚定(分镜图=第0帧"), 2, "逐镜都要标出拓扑")
        self.assertNotIn("全能参考", out)

    def test_cli_override_switches_without_touching_the_document(self):
        project = self._project(self._shots(1))
        out = self._dry(project, anchor_frame=True)
        self.assertIn("首帧锚定", out)
        self.assertIsNone(project.data.get("anchor_frame"),
                          "本次覆盖不落盘——与 --chain 同制")

    def test_shot_opt_in_only_touches_that_shot(self):
        shots = self._shots(2)
        shots[1]["anchor_frame"] = True
        out = self._dry(self._project(shots))
        first, second = out.split("镜2 ·")[0], "镜2 ·" + out.split("镜2 ·")[1]
        self.assertIn("全能参考", first, "没表态的镜留在缺省档")
        self.assertIn("首帧锚定", second)

    def test_dubbed_chapter_ignores_the_flag(self):
        """dubbed 恒走参考媒体——标一个不会生效的锚定比不标更糟。"""
        out = self._dry(self._project(self._shots(1), motion="dubbed"),
                        anchor_frame=True)
        self.assertNotIn("首帧锚定", out)

    def test_tradeoff_is_named_once_per_chapter(self):
        """三条让位通道必须出声：静默让位＝配置上设定图仍挂着，实际却没参与生成。"""
        out = self._dry(self._project(self._shots(3), anchor_frame=True))
        self.assertEqual(out.count("首帧锚定生效"), 1, "整章一次，不逐镜刷屏")
        self.assertIn("设定图", out)
        self.assertIn("简笔板", out)

    def test_tradeoff_names_tail_relay_only_when_it_was_asked_for(self):
        """没开接力就不提它——列一条用户没要过的损失只会制造困惑。"""
        on = self._dry(self._project(self._shots(2), anchor_frame=True,
                                     tail_relay=True))
        self.assertIn("尾帧接力", on.split("首帧锚定生效")[1].split("\n")[0])
        off = self._dry(self._project(self._shots(2), anchor_frame=True))
        self.assertNotIn("尾帧接力", off.split("首帧锚定生效")[1].split("\n")[0])

    def test_anchored_shot_sends_no_reference_sheet(self):
        """协议硬约束：首帧任务禁混参考图。锚定镜的附图清单必须真的空掉。"""
        sheet = _png(self.tmp / "char_a.png")
        shots = self._shots(1)
        shots[0]["characters"] = ["阿甲"]
        project = self._project(
            shots, anchor_frame=True,
            characters=[{"name": "阿甲", "sheet": str(sheet), "appearance": "灰毛"}])
        out = self._dry(project)
        self.assertNotIn("设定图×", out, "锚定档一张设定图都不许进请求")
        self.assertIn("无参考图", out)

    def test_chain_chapter_is_unaffected(self):
        """衔接参与镜本就是首帧任务，锚定对它们是空操作、不许改写链态。"""
        shots = self._shots(2)
        out = self._dry(self._project(shots, frame_chain=True, anchor_frame=True))
        self.assertIn("末帧=镜2", out, "衔接照旧焊缝")
        self.assertNotIn("首帧锚定(分镜图=第0帧", out)


class TestQuotaTruncationIsNamed(_Base):
    """配额裁剪不许静默：作者显式点名的设定图被丢掉却毫无痕迹是最贵的失败形态。"""

    def _many_sheets_project(self, n_props: int):
        shots = self._shots(1)
        shots[0]["characters"] = ["阿甲"]
        shots[0]["props"] = [f"道具{i}" for i in range(1, n_props + 1)]
        props = [{"name": f"道具{i}", "sheet": str(_png(self.tmp / f"p{i}.png"))}
                 for i in range(1, n_props + 1)]
        return self._project(
            shots,
            characters=[{"name": "阿甲", "sheet": str(_png(self.tmp / "c.png")),
                         "appearance": "灰毛"}],
            props=props)

    def test_within_quota_says_nothing(self):
        out = self._dry(self._many_sheets_project(3))
        self.assertIn("设定图×4", out)
        self.assertNotIn("配额裁掉", out)

    def test_over_quota_names_what_was_dropped(self):
        # 角色 1 + 道具 8 = 9 项，配额 7 → 必须点名被丢的那 2 项
        out = self._dry(self._many_sheets_project(8))
        self.assertIn("设定图×7", out)
        self.assertIn("配额裁掉2项", out)
        self.assertIn("道具「道具8」", out, "被丢的项要指名道姓，不能只报个数")

    def test_dropped_note_sits_on_the_same_line_as_the_count(self):
        """「设定图×7」单独出现读起来像「都发出去了」——两者必须同行。"""
        out = self._dry(self._many_sheets_project(8))
        line = next(ln for ln in out.splitlines() if "设定图×7" in ln)
        self.assertIn("配额裁掉", line)


if __name__ == "__main__":
    unittest.main()
