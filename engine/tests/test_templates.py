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

"""kinema.templates 单元测试：内置模板、区间校验（含进行中语义）、apply_to_project。"""
from __future__ import annotations

import unittest

from kinema import templates
from kinema.errors import ConfigError

_BUILTIN = {"douyin_manju", "kuaishou_xingmang", "bilibili_zhongshipin",
            "kepu_koubo", "yulu_zhiyu", "ertong_huiben"}


class TestLoad(unittest.TestCase):
    def test_embedded_has_six_templates(self):
        self.assertEqual(set(templates.EMBEDDED_TEMPLATES), _BUILTIN)
        for name, tpl in templates.EMBEDDED_TEMPLATES.items():
            self.assertIn("label", tpl, name)
            self.assertIn("profile", tpl, name)
            self.assertIn("aspect", tpl, name)

    def test_load_templates_superset_of_embedded(self):
        merged, src = templates.load_templates()
        self.assertTrue(_BUILTIN.issubset(set(merged)))
        self.assertIsInstance(src, str)

    def test_get_known_and_unknown(self):
        tpl = templates.get("douyin_manju")
        self.assertEqual(tpl["name"], "douyin_manju")      # get 会补 name 键
        self.assertIn("episode", tpl)
        with self.assertRaises(ConfigError):
            templates.get("no_such_template")


class TestApplyToProject(unittest.TestCase):
    def test_fields_landed(self):
        data = {"profile": "narration", "aspect": "9:16"}
        tpl = {"name": "t1", "label": "测试模板", "profile": "anime",
               "aspect": "16:9", "platform": ["douyin"], "motion": "c",
               "episode": {"minutes": [1, 2]}}
        templates.apply_to_project(data, tpl)
        self.assertEqual(data["profile"], "anime")
        self.assertEqual(data["aspect"], "16:9")
        self.assertEqual(data["platform"], ["douyin"])
        self.assertEqual(data["motion"], "c")
        # 规格快照：模板全量入 template，name 保留
        self.assertEqual(data["template"]["name"], "t1")
        self.assertEqual(data["template"]["label"], "测试模板")
        self.assertEqual(data["template"]["episode"], {"minutes": [1, 2]})

    def test_missing_keys_keep_existing(self):
        data = {"profile": "quote", "aspect": "1:1"}
        templates.apply_to_project(data, {"name": "bare"})
        self.assertEqual(data["profile"], "quote")         # 模板未给 → 保留原值
        self.assertEqual(data["aspect"], "1:1")
        self.assertNotIn("platform", data)
        self.assertNotIn("motion", data)


class TestSpecCheck(unittest.TestCase):
    TPL = {"aspect": "9:16", "episode": {"minutes": [1, 2], "shots": [8, 14]}}

    def _by_item(self, rows):
        return {r["item"]: r for r in rows}

    def test_chapter_in_range(self):
        rows = self._by_item(templates.check_chapter(
            self.TPL, duration_s=90, shots=10, aspect="9:16"))
        self.assertTrue(rows["时长"]["ok"])                 # 1.5 分钟 ∈ [1,2]
        self.assertTrue(rows["分镜数"]["ok"])
        self.assertTrue(rows["比例"]["ok"])

    def test_chapter_out_of_range(self):
        rows = self._by_item(templates.check_chapter(
            self.TPL, duration_s=30, shots=5, aspect="16:9"))
        self.assertFalse(rows["时长"]["ok"])                # 0.5 分钟低于下限
        self.assertFalse(rows["分镜数"]["ok"])
        self.assertFalse(rows["比例"]["ok"])

    def test_chapter_unconstrained_is_none(self):
        rows = self._by_item(templates.check_chapter(
            {}, duration_s=90, shots=10, aspect="9:16"))
        self.assertIsNone(rows["时长"]["ok"])               # 模板未约束 → None
        self.assertIsNone(rows["分镜数"]["ok"])
        self.assertIsNone(rows["比例"]["ok"])

    def test_series_total_minutes_progress_semantics(self):
        tpl = {"series": {"episodes": [20, 30], "total_minutes": [100, 150]}}
        by = lambda tm: self._by_item(templates.check_series(   # noqa: E731
            tpl, episodes=25, total_minutes=tm))
        self.assertIsNone(by(50)["总时长"]["ok"])           # 未达下限 → 进行中(None)
        self.assertTrue(by(120)["总时长"]["ok"])            # 区间内 → 达标
        self.assertFalse(by(200)["总时长"]["ok"])           # 超上限 → 超标
        self.assertTrue(by(120)["集数"]["ok"])              # 25 集 ∈ [20,30]

    def test_series_unconstrained(self):
        rows = self._by_item(templates.check_series(
            {}, episodes=5, total_minutes=10))
        self.assertIsNone(rows["集数"]["ok"])
        self.assertIsNone(rows["总时长"]["ok"])


if __name__ == "__main__":
    unittest.main()
