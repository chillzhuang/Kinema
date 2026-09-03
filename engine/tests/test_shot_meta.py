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

"""结构化分镜元数据：shot_intent / narrative_role / hero_moment + framing 放宽。

守卫三件事：
1. schema 契约——三字段是自由文本/布尔、**不设枚举**，`framing` 由 enum(8) 放宽为
   string（消除 schema ↔ SKILL 12 项 ↔ 真实数据「双人中景」三方漂移）；
2. `hero_moment` 的**诚实边界**——description 必须写明「引擎不读」，且花钱与渲染
   主链（cli / prompts / compose / kenburns / providers）里对三字段零命中；
3. Studio 分镜下发白名单透传三字段（不加则只存不显）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import LocalBackendEnv

SCHEMA_PATH = (Path(__file__).resolve().parents[2]
               / "docs" / "kinema" / "project.schema.json")
PKG = Path(__file__).resolve().parents[1] / "kinema"

NEW_FIELDS = ("shot_intent", "narrative_role", "hero_moment")


def _shot_props() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["properties"]["shots"]["items"]["properties"]


@unittest.skipUnless(SCHEMA_PATH.is_file(), f"缺 schema 文件: {SCHEMA_PATH}")
class TestShotMetaSchema(unittest.TestCase):
    def test_new_fields_are_free_text_without_enum(self):
        props = _shot_props()
        self.assertEqual(props["shot_intent"]["type"], "string")
        self.assertEqual(props["narrative_role"]["type"], "string")
        self.assertEqual(props["hero_moment"]["type"], "boolean")
        for f in NEW_FIELDS:                       # framing 的教训：新字段一律不定枚举
            self.assertNotIn("enum", props[f], f"{f} 不该定枚举")

    def test_hero_moment_declares_engine_does_not_read(self):
        # 诚实降级：priority 是前车之鉴（写进契约、引擎零命中却不说明）
        desc = _shot_props()["hero_moment"]["description"]
        self.assertIn("引擎不读", desc)
        self.assertIn("priority", desc)            # 与 priority 的分工写清楚
        self.assertIn("profile", desc)             # 要贵走 shots[].profile，引擎不自动升档

    def test_framing_relaxed_to_string(self):
        framing = _shot_props()["framing"]
        self.assertEqual(framing["type"], "string")
        self.assertNotIn("enum", framing)
        desc = framing["description"]
        for code in ("EWS", "WS", "FS", "MLS", "MS", "MCU", "CU", "ECU",
                     "OTS", "POV", "2S", "INS"):   # storyboard.md 景别对照表 12 项
            self.assertIn(code, desc)

    def test_no_duplicate_shot_size(self):
        # 不新增 shot_size：framing 已被 prompts 摄影地板/mysql 列/app.js/export 四处消费
        self.assertNotIn("shot_size", _shot_props())


class TestEngineDoesNotConsume(unittest.TestCase):
    """三字段不得出现在花钱与渲染主链——`hero_moment` 供 lint 与人审，永不驱动预算。"""

    TARGETS = ("cli.py", "models.py", "pipeline/prompts.py", "pipeline/compose.py",
               "pipeline/kenburns.py", "pipeline/cover.py", "pipeline/candidates.py",
               "batch.py")

    def _sources(self):
        for rel in self.TARGETS:
            yield PKG / rel
        yield from (PKG / "providers").rglob("*.py")

    def test_targets_all_exist(self):
        """守卫的守卫：路径写错会被下面的 `is_file()` 静默跳过，
        于是「某文件不读 hero_moment」这条断言看似绿灯、实则从未跑过。"""
        missing = [rel for rel in self.TARGETS if not (PKG / rel).is_file()]
        self.assertEqual(missing, [], f"守卫目标路径不存在（该条守卫等于没跑）: {missing}")

    def test_no_hits_in_cost_and_render_chain(self):
        hits = []
        for p in self._sources():
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8")
            hits += [f"{p.name}:{f}" for f in NEW_FIELDS if f in text]
        self.assertEqual(hits, [], f"叙事元数据渗进了花钱/渲染主链: {hits}")


class TestScannerSurfacesShotMeta(unittest.TestCase):
    """Studio 分镜下发白名单：三字段透传（不加则只存不显）。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("Meta", pid="meta")
        self.cf = self.s.create_chapter("第一章", cid="ch01")
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        data["shots"] = [{
            "id": 1, "dur": 3.0, "narration": "他终于开口。",
            "image_prompt": "近景",
            "framing": "双人中景",                 # 真实数据里的组合口径，不受枚举约束
            "shot_intent": "点破身份，让观众第一次意识到他在说谎",
            "narrative_role": "转折",
            "hero_moment": True,
        }, {"id": 2, "dur": 2.0, "narration": "", "image_prompt": "空镜"}]
        self.cf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_chapter_detail_passes_through(self):
        from kinema.studio import scanner
        d = scanner.chapter_detail(self.ws.root, self.ws.store, "meta", "ch01")
        s1, s2 = d["shots"][0], d["shots"][1]
        self.assertEqual(s1["shot_intent"], "点破身份，让观众第一次意识到他在说谎")
        self.assertEqual(s1["narrative_role"], "转折")
        self.assertIs(s1["hero_moment"], True)
        self.assertEqual(s1["framing"], "双人中景")
        for f in NEW_FIELDS:                       # 未填的镜给 None，不缺键（前端可直接读）
            self.assertIn(f, s2)
            self.assertIsNone(s2[f])


if __name__ == "__main__":
    unittest.main()
