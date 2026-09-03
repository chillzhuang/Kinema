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

"""kinema.pipeline.candidates 单元测试：候选命名/派生 seed/pick 定稿与归档语义。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kinema import review
from kinema.errors import KinemaError
from kinema.pipeline import candidates, versioning
from tests.support import FakeProject


class TestPureHelpers(unittest.TestCase):
    def test_seed_for(self):
        self.assertEqual(candidates.seed_for(100, 0), 100)
        self.assertEqual(candidates.seed_for(100, 1), 100 + 7919)
        self.assertEqual(candidates.seed_for("100", 2), 100 + 2 * 7919)
        self.assertIsNone(candidates.seed_for(None, 3))    # 无基准 seed → 交给模型随机

    def test_candidate_path_naming(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = FakeProject(tmp)
            p = candidates.candidate_path(proj, {"id": "s1"}, 2)
            self.assertEqual(p, Path(tmp) / "images" / "shot_s1_cand2.png")

    def test_listing(self):
        self.assertEqual(candidates.listing({}), [])
        shot = {"image_candidates": ["/a.png", "/b.png"]}
        out = candidates.listing(shot)
        self.assertEqual(out, ["/a.png", "/b.png"])
        out.append("/c.png")                               # 返回副本，不共享内部列表
        self.assertEqual(len(shot["image_candidates"]), 2)


class TestPick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = FakeProject(self.tmp.name)
        images = self.proj.subdir("images")
        self.c1 = images / "shot_s1_cand1.png"
        self.c2 = images / "shot_s1_cand2.png"
        self.c1.write_text("cand-1")
        self.c2.write_text("cand-2")
        self.shot = {"id": "s1", "image_candidates": [str(self.c1), str(self.c2)]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_pick_errors(self):
        with self.assertRaises(KinemaError):
            candidates.pick(self.proj, {"id": "s9"}, 1)    # 无候选
        with self.assertRaises(KinemaError):
            candidates.pick(self.proj, self.shot, 3)       # 编号超界
        self.c2.unlink()
        with self.assertRaises(KinemaError):
            candidates.pick(self.proj, self.shot, 2)       # 候选文件丢失

    def test_pick_copies_to_canvas_and_locks_done(self):
        res = candidates.pick(self.proj, self.shot, 2)
        canvas = Path(res["canvas"])
        self.assertEqual(canvas.name, "shot_s1.png")
        self.assertEqual(canvas.read_text(), "cand-2")     # 候选上画布
        self.assertTrue(self.c2.is_file())                 # 拷贝而非移动：候选保留可反悔
        self.assertEqual(self.shot["image"], str(canvas))
        self.assertEqual(self.shot["image_picked"], 2)
        self.assertEqual(self.shot["gen"]["image"]["candidate"], 2)
        self.assertEqual(res["version"], 1)                # 首次定稿无归档
        self.assertIsNone(res["archived"])
        self.assertEqual(res["state"], "done")             # 人眼定稿 → 直接锁定
        self.assertTrue(review.is_locked(self.shot, "image"))
        self.assertGreaterEqual(self.proj.saved, 1)

    def test_pick_without_approve_lands_wfa(self):
        res = candidates.pick(self.proj, self.shot, 1, approve=False)
        self.assertEqual(res["state"], "wfa")
        self.assertEqual(review.get_state(self.shot, "image"), "wfa")
        self.assertIn("待复核", review.get_note(self.shot, "image"))

    def test_repick_archives_previous_canvas(self):
        candidates.pick(self.proj, self.shot, 2)           # 定稿 #2
        res = candidates.pick(self.proj, self.shot, 1)     # 换选 #1（不同编号可换）
        self.assertEqual(Path(res["canvas"]).read_text(), "cand-1")
        self.assertEqual(res["archived"], "v001")          # 旧画布进版本栈
        self.assertEqual(res["version"], 2)
        hist = versioning.history(self.shot, "image")
        self.assertEqual(len(hist), 1)
        self.assertIn("pick", hist[0]["reason"])
        self.assertEqual(Path(hist[0]["files"]["main"]).read_text(), "cand-2")

    def test_pick_same_candidate_when_locked_raises(self):
        candidates.pick(self.proj, self.shot, 2)
        with self.assertRaises(KinemaError):
            candidates.pick(self.proj, self.shot, 2)       # 已定稿同一张 → 拒绝


if __name__ == "__main__":
    unittest.main()


class TestPickPerAspectAndFingerprints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = FakeProject(self.tmp.name)
        images = self.proj.subdir("images")
        self.c1 = images / "shot_s1_cand1.png"
        self.c1.write_text("cand-1")
        self.main = images / "shot_s1.png"
        self.main.write_text("old-main")
        self.side = images / "shot_s1_9x16.png"
        self.side.write_text("old-side")
        self.shot = {"id": "s1", "image": str(self.main),
                     "images": {"16:9": str(self.main), "9:16": str(self.side)},
                     "image_candidates": [str(self.c1)],
                     "gen": {"image_candidates": {"prompt": "p", "provider": "fake",
                                                  "refs": {"/sheet/a.png": "sha256:old"}}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_pick_drops_per_aspect_canvases_that_went_into_the_archive(self):
        """逐比例画布随归档进版本栈后，images{} 必须一并退场——留着会让 image_for
        优先取到已移走的路径，镜被 done 锁死且无图可渲。"""
        candidates.pick(self.proj, self.shot, 1)
        self.assertNotIn("images", self.shot)
        self.assertEqual(Path(self.shot["image"]).read_text(), "cand-1")
        self.assertFalse(self.side.exists())
        hist = versioning.history(self.shot, "image")
        self.assertEqual(set(hist[0]["files"]), {"main", "9:16"})

    def test_pick_keeps_the_fingerprints_recorded_when_candidates_were_made(self):
        candidates.pick(self.proj, self.shot, 1)
        self.assertEqual(self.shot["gen"]["image"]["refs"], {"/sheet/a.png": "sha256:old"})
