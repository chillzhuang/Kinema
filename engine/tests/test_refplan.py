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

"""视频参考装配的单源守卫（RefPlan）。

「发哪几张图、什么顺序、各自什么职责」有五处消费位：manifest /
工作线程 ref_images / Envelope references / Studio 预览 paths / provider content[]。
本文件先用**黄金用例**钉住五处的逐位结果——五处若各自重建装配逻辑必然漂移；
再钉 RefPlan 自身的构造期不变量。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock

from pathlib import Path

from kinema.providers.base import VideoResult
from tests.support import LocalBackendEnv, fake_path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082"))
    return path


class _GoldenBase(unittest.TestCase):
    """全能参考镜的标准布景：分镜图 + 板 + 角色/场景/俯视/道具设定图各一。"""

    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.img = str(_png(self.tmp / "shot1.png"))
        self.board = str(_png(self.tmp / "board.png"))
        self.char_sheet = str(_png(self.tmp / "char_林深.png"))
        self.scene_sheet = str(_png(self.tmp / "scene_书店.png"))
        self.top_sheet = str(_png(self.tmp / "scene_top_书店.png"))
        self.prop_sheet = str(_png(self.tmp / "prop_断刃.png"))

    def tearDown(self):
        self._tmp.cleanup()
        self._env.restore()

    def _project(self, **over):
        from kinema.project import Project
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {
            "id": "p1_ch01", "profile": "anime", "motion": "native",
            "aspect": "16:9",
            "characters": [{"name": "林深", "sheet": self.char_sheet}],
            "props": [{"name": "断刃", "sheet": self.prop_sheet}],
            "scenes": [{"name": "书店", "sheet": self.scene_sheet,
                        "topview_sheet": self.top_sheet}],
            "shots": [{"id": 1, "dur": 5.0, "image": self.img,
                       "video_prompt": "转身", "guide": "sketch",
                       "characters": ["林深"], "props": ["断刃"],
                       "scenes": ["书店"],
                       "sketch": {"sheet": self.board, "beats": [
                           {"action": "起身"}, {"action": "迈步"},
                           {"action": "回望"}]}}],
        }
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    # 黄金基线：@图片N 编号 = 附图顺序（分镜图 → 板 → 设定图），设定图定序 =
    # 角色 → 场景(+紧跟其俯视图) → 道具
    GOLDEN_KINDS = ["frame", "board", "character", "scene", "scene_top", "prop"]

    def _run(self, project, *, dry_run, preview_sink=None):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        store = ConfigStore.load(None)
        with contextlib.redirect_stdout(io.StringIO()):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=dry_run, preview_sink=preview_sink)


class TestGolden(_GoldenBase):
    def test_provider_receives_the_manifest_order(self):
        """provider 收到的 image + ref_images 与 manifest 逐位对应（工作线程装配）。"""
        from kinema.providers.video import mock as vmock
        calls = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            calls.append({"image": image, **kw})
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = spy
        try:
            p = self._project()
            # 假片段字节 probe 不出时长——回填读的是真实产物属性，这里只关心装配
            with unittest.mock.patch("kinema.cli.probe_duration", return_value=5.0):
                self._run(p, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["image"], self.img)
        self.assertTrue(c["reference_only"])
        self.assertEqual(c["ref_images"],
                         [self.board, self.char_sheet, self.scene_sheet,
                          self.top_sheet, self.prop_sheet])
        self.assertIsNone(c["last_frame"])
        # Envelope 快照（血缘留痕）与实发同序
        env = p.data["shots"][0]["gen"]["clip"]["envelope"]
        roles = [r["role"] for r in env["references"]]
        self.assertEqual(roles, ["shot_frame", "sketch_board",
                                 "design_reference", "design_reference",
                                 "design_reference", "design_reference"])
        # 提示词按图号逐张点名：板职责句在，设定图绑定句指到正确图号
        prompt = c["prompt"]
        self.assertIn("@图片3", prompt)
        self.assertIn("林深", prompt)

    def test_preview_refs_match_the_manifest(self):
        """Studio 预览的 @图片N → 文件映射与 manifest 同一次装配。"""
        sink: list = []
        self._run(self._project(), dry_run=True, preview_sink=sink)
        self.assertEqual(len(sink), 1)
        refs = sink[0]["refs"]
        self.assertEqual([r["kind"] for r in refs], self.GOLDEN_KINDS)
        self.assertEqual([r["no"] for r in refs], [1, 2, 3, 4, 5, 6])
        self.assertEqual([r["path"] for r in refs],
                         [self.img, self.board, self.char_sheet,
                          self.scene_sheet, self.top_sheet, self.prop_sheet])

    def test_dry_run_note_reports_the_composition(self):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(self._project(), store,
                            ModelRouter(store, force_mock=True), dry_run=True)
        out = buf.getvalue()
        self.assertIn("分镜图+简笔板+设定图×3+场景俯视×1", out)


class TestInvariants(unittest.TestCase):
    """RefPlan 的构造期不变量：错位类事故在计划期就炸，不流到计费。"""

    def _rows(self, n, kind="prop"):
        return tuple((kind, f"名{i}", fake_path(f"p{i}.png")) for i in range(n))

    def test_manifest_aligns_with_refs(self):
        from kinema.pipeline.refplan import RefPlan
        rp = RefPlan(board=fake_path("b.png"), tails={"16:9": fake_path("t.png")},
                     rows=self._rows(3))
        self.assertEqual(len(rp.manifest), 1 + len(rp.refs_for("16:9")))
        self.assertEqual([k for k, _ in rp.manifest],
                         ["frame", "board", "tail", "prop", "prop", "prop"])

    def test_over_quota_rejected(self):
        from kinema.pipeline.refplan import RefPlan
        with self.assertRaises(ValueError):
            RefPlan(board=fake_path("b.png"), tails={"16:9": fake_path("t.png")},
                    rows=self._rows(6))   # 1+1+6=8 > 7

    def test_duplicate_path_rejected(self):
        from kinema.pipeline.refplan import RefPlan
        same = fake_path("same.png")
        rows = (("prop", "甲", same), ("scene", "乙", same))
        with self.assertRaises(ValueError):
            RefPlan(rows=rows)

    def test_unknown_kind_rejected(self):
        from kinema.pipeline.refplan import RefPlan
        with self.assertRaises(ValueError):
            RefPlan(rows=(("identity", "甲", fake_path("a.png")),))

    def test_route_b_head_is_scene_base(self):
        from kinema.pipeline.refplan import RefPlan
        rp = RefPlan(route="B", board=fake_path("b.png"), rows=self._rows(1))
        self.assertEqual(rp.manifest[0], ("scene_base", ""))

    def test_at_translates_content_index(self):
        from kinema.pipeline.refplan import RefPlan
        rp = RefPlan(board=fake_path("b.png"),
                     rows=(("character", "林深", fake_path("c.png")),))
        self.assertEqual(rp.at(3, image=fake_path("img.png")),
                         {"kind": "character", "name": "林深",
                          "path": fake_path("c.png")})
        self.assertEqual(rp.at(99)["kind"], "unknown")

    def test_at_counts_the_tail_slot_without_an_aspect(self):
        """尾帧在场、调用方没给比例时也不许把末项截成 unknown——
        错误翻译拿不到比例，末位的道具就会被静默漏点名。"""
        from kinema.pipeline.refplan import RefPlan
        rp = RefPlan(board=fake_path("b.png"), tails={"16:9": fake_path("t.png")},
                     rows=(("character", "林深", fake_path("c.png")),
                           ("prop", "断刃", fake_path("p.png"))))
        self.assertEqual(rp.at(5)["name"], "断刃")


if __name__ == "__main__":
    unittest.main()
