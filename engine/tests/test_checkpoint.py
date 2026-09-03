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

"""kinema.pipeline.checkpoint 单元测试：has_file 本地/URL 语义。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kinema.pipeline import checkpoint


class TestHasFile(unittest.TestCase):
    def test_empty_values(self):
        self.assertFalse(checkpoint.has_file(None))
        self.assertFalse(checkpoint.has_file(""))

    def test_url_counts_as_produced(self):
        # 已上云的 URL 视为已产出（避免误重生成烧钱），不做本地文件检查
        self.assertTrue(checkpoint.has_file("https://oss.example.com/av/x/shot_1.png"))
        self.assertTrue(checkpoint.has_file("http://cdn.example.com/clip.mp4"))

    def test_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "shot_1.png"
            f.write_bytes(b"png")
            self.assertTrue(checkpoint.has_file(str(f)))
            self.assertFalse(checkpoint.has_file(str(Path(tmp) / "missing.png")))


class TestNeeds(unittest.TestCase):
    def test_mark(self):
        shot = {}
        checkpoint.mark(shot, "image_done")
        self.assertEqual(shot["status"], "image_done")


if __name__ == "__main__":
    unittest.main()
