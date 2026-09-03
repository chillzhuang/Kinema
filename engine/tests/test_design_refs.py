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

"""设定图一致性守卫：Project.matched_props / design_refs 与 lineage.required_refs 锁步。

道具设定图默认策略是「显式 props ∪ image_prompt/narration 里点名命中（name/keywords）
的道具」——若只挂显式 shots[].props，点名命中的道具不注入参考图，具名道具会被画成
泛化物件（「水杯魔王」退化成普通陶瓷马克杯）。design_refs（参考图装配）与
required_refs（就绪度护栏）共用 matched_props 单一真源。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kinema import lineage, review
from kinema.project import Project
from tests.support import FakeProject, LocalBackendEnv, fake_path


class DesignRefsCase(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scene = self._touch("scene.png")
        self.cat = self._touch("char_cat.png")
        self.cup = self._touch("prop_cup.png")
        self.napkin = self._touch("prop_napkin.png")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _touch(self, name: str) -> str:
        p = self.root / name
        p.write_bytes(b"x")
        return str(p)

    def _project(self, shot: dict, *, props=None, characters=None, **over) -> Project:
        data = {
            "scene": "客厅",
            "scene_ref": self.scene,
            "characters": characters if characters is not None else [
                {"name": "喵勇者", "sheet": self.cat}],
            "props": props if props is not None else [
                {"name": "水杯魔王", "keywords": ["水杯", "玻璃杯"], "sheet": self.cup},
                {"name": "纸巾卷魔王", "keywords": [], "sheet": self.napkin},
            ],
            "shots": [shot],
        }
        data.update(over)
        return Project(self.root / "project.json", data)

    # —— 核心回归：不写 props 也能挂道具设定图 ——
    def test_prop_attached_by_name_without_shot_props(self):
        """shot 不写 props，image_prompt 点名道具 name → 道具 sheet 进参考。"""
        shot = {"id": 1, "image_prompt": "橘猫挥爪扑向那只水杯魔王"}
        p = self._project(shot)
        self.assertIn(self.cup, p.design_refs(shot))
        self.assertNotIn(self.napkin, p.design_refs(shot))  # 未点名的道具不挂

    def test_prop_attached_by_keyword_in_narration(self):
        """keyword 桥接：narration 用『水杯』而注册名是『水杯魔王』→ 仍命中。"""
        shot = {"id": 1, "narration": "对准那只水杯，出击！", "image_prompt": "橘猫扑击"}
        p = self._project(shot)
        self.assertIn(self.cup, p.design_refs(shot))       # keyword 命中
        self.assertNotIn(self.napkin, p.design_refs(shot))  # 无 keyword、name 未出现

    def test_explicit_shot_props_still_honored_and_deduped(self):
        """显式 props 与文本命中取并集，仍命中且不重复。"""
        shot = {"id": 1, "props": ["水杯魔王"], "image_prompt": "橘猫扑向水杯魔王"}
        p = self._project(shot)
        refs = p.design_refs(shot)
        self.assertEqual(refs.count(self.cup), 1)

    def test_prop_not_matched_when_absent(self):
        """无关镜（不提道具、无显式 props）→ 不挂任何道具，保护 8 槽/不污染画面。"""
        shot = {"id": 1, "image_prompt": "橘猫在地板上打滚"}
        p = self._project(shot)
        refs = p.design_refs(shot)
        self.assertNotIn(self.cup, refs)
        self.assertNotIn(self.napkin, refs)
        self.assertEqual(refs, [self.scene, self.cat])

    def test_short_name_needs_explicit_or_keyword(self):
        """1 字道具名（如『刀』）不做子串泛匹配，只认显式 props 或 keyword。"""
        blade = self._touch("prop_blade.png")
        props = [{"name": "刀", "keywords": [], "sheet": blade}]
        # 文本含『刀』字但 name<2 → 不自动命中
        p1 = self._project({"id": 1, "image_prompt": "橘猫一刀劈下"}, props=props)
        self.assertNotIn(blade, p1.design_refs({"id": 1, "image_prompt": "橘猫一刀劈下"}))
        # 显式 props → 命中
        shot2 = {"id": 2, "props": ["刀"], "image_prompt": "橘猫一刀劈下"}
        p2 = self._project(shot2, props=props)
        self.assertIn(blade, p2.design_refs(shot2))

    def test_order_scene_then_chars_then_props(self):
        shot = {"id": 1, "image_prompt": "橘猫扑向水杯魔王"}
        p = self._project(shot)
        refs = p.design_refs(shot)
        self.assertEqual(refs[0], self.scene)
        self.assertEqual(refs[1], self.cat)
        self.assertEqual(refs[2], self.cup)

    def test_dedup_and_cap8(self):
        chars = [{"name": f"c{i}", "sheet": self._touch(f"c{i}.png")} for i in range(10)]
        shot = {"id": 1, "image_prompt": "群像 水杯魔王"}
        p = self._project(shot, characters=chars)
        refs = p.design_refs(shot)
        self.assertLessEqual(len(refs), 8)
        self.assertEqual(len(refs), len(set(refs)))
        # 被 8 张上限截断的余量可见（不静默）
        self.assertTrue(p.design_ref_overflow(shot))

    def test_provider_cap_prefers_current_scene_cast_and_props(self):
        named = self._touch("scene_named.png")
        cat2 = self._touch("char_cat2.png")
        props = [
            {"name": "水杯魔王", "keywords": ["水杯"], "sheet": self.cup},
            {"name": "纸巾卷魔王", "keywords": [], "sheet": self.napkin},
        ]
        p = self._project(
            {"id": 1, "characters": ["喵勇者", "配角"],
             "scenes": ["雨台阶"], "props": ["水杯魔王", "纸巾卷魔王"],
             "image_prompt": "水杯魔王 水杯 水杯 水杯",
             "image_prompt_en": ""},
            props=props,
            characters=[{"name": "喵勇者", "sheet": self.cat},
                        {"name": "配角", "sheet": cat2}],
            scenes=[{"name": "雨台阶", "sheet": named}],
        )
        refs, omitted = p.design_refs_for_provider(p.shots[0], 4)
        self.assertEqual(refs[:3], [named, self.cat, cat2])
        self.assertNotIn(self.scene, refs + omitted, "具名当前场景存在时全局图不进本镜清单")
        self.assertEqual(len(refs), 4)
        self.assertEqual(omitted, [self.napkin], "低频道具让位")

    def test_missing_sheet_file_dropped(self):
        props = [{"name": "水杯魔王", "keywords": [], "sheet": str(self.root / "nope.png")}]
        shot = {"id": 1, "image_prompt": "橘猫扑向水杯魔王"}
        p = self._project(shot, props=props)
        # sheet 指向不存在文件 → 不进 refs、不抛错
        self.assertEqual(p.design_refs(shot), [self.scene, self.cat])

    def test_skip_design_returns_empty(self):
        shot = {"id": 1, "image_prompt": "橘猫扑向水杯魔王"}
        p = self._project(shot, skip_design=True)
        self.assertEqual(p.design_refs(shot), [])
        self.assertEqual(lineage.required_refs(p, shot), [])

    # —— 锁步铁律：护栏与实际参考同口径 ——
    def test_required_refs_locksteps_design_refs(self):
        shot = {"id": 1, "image_prompt": "橘猫扑向水杯魔王"}
        p = self._project(shot)
        prop_paths = {r["path"] for r in lineage.required_refs(p, shot)
                      if r["kind"] == "prop"}
        self.assertTrue(prop_paths)  # 命中了道具
        self.assertTrue(prop_paths <= set(p.design_refs(shot)))

    def test_readiness_missing_for_matched_sheetless_prop(self):
        """文本命中道具但 sheet 未生成 → readiness 不 ok（据此 gen-video 硬拦生效）。"""
        props = [{"name": "水杯魔王", "keywords": [], "sheet": None}]
        shot = {"id": 1, "image_prompt": "橘猫扑向水杯魔王"}
        p = self._project(shot, props=props)
        ok, missing = lineage.readiness(p, shot)
        self.assertFalse(ok)
        self.assertIn("prop:水杯魔王", missing)


if __name__ == "__main__":
    unittest.main()


class NamedSceneTierCase(unittest.TestCase):
    """具名场景（取景地）这一档的守卫。

    场景若塞进 `props[]` 冒充道具，会套上**物件版式**（完整视图+局部细节框+接缝/机构+
    纯色浅灰底），把「孢子宫殿」画成蘑菇战锤、「原始孢子森林」画成防毒面具与齿轮球。
    本组钉死三件事：
      ① 具名场景是与道具**并列**的一档，不寄生在 props[]；
      ② 它的设定图走 `scene_rules()`（环境 key art：广角建立镜头/无人物/前中远三层），
         **绝不走 `prop_rules()`**；比例跟随项目而非 1:1；
      ③ 命中口径与道具同源（`_matched_entities`），design_refs 与 required_refs 不分叉。
    """

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hall = self._touch("scene_大厅.png")
        self.cup = self._touch("prop_cup.png")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _touch(self, name: str) -> str:
        p = self.root / name
        p.write_bytes(b"x")
        return str(p)

    def _project(self, shot: dict, **over) -> Project:
        data = {
            "characters": [],
            "props": [{"name": "水杯魔王", "keywords": ["水杯"], "sheet": self.cup}],
            "scenes": [{"name": "神之塔大厅", "keywords": ["金銮殿"], "sheet": self.hall}],
            "shots": [shot],
        }
        data.update(over)
        return Project(self.root / "project.json", data)

    # —— ① 独立一档：命中即挂，且与道具互不影响 ——
    def test_scene_attached_by_name(self):
        shot = {"id": 1, "image_prompt": "少年走进神之塔大厅，抬头看穹顶"}
        p = self._project(shot)
        self.assertIn(self.hall, p.design_refs(shot))
        self.assertNotIn(self.cup, p.design_refs(shot))

    def test_scene_attached_by_keyword(self):
        shot = {"id": 1, "narration": "那金銮殿般的大厅里，九根盘龙柱撑着穹顶"}
        p = self._project(shot)
        self.assertIn(self.hall, p.design_refs(shot))

    def test_scene_explicit_shot_field_always_hits(self):
        """镜级显式 `shots[].scenes` 是白名单，文本没点名也必挂（与 props 同待遇）。"""
        shot = {"id": 1, "image_prompt": "一个背影", "scenes": ["神之塔大厅"]}
        p = self._project(shot)
        self.assertIn(self.hall, p.design_refs(shot))

    def test_scene_not_attached_when_absent(self):
        shot = {"id": 1, "image_prompt": "校园操场上人群集结"}
        p = self._project(shot)
        self.assertNotIn(self.hall, p.design_refs(shot))

    # —— ② required_refs 与 design_refs 同源，永不分叉 ——
    def test_required_refs_matches_design_refs(self):
        shot = {"id": 1, "image_prompt": "少年走进神之塔大厅"}
        p = self._project(shot)
        keys = {r["key"] for r in lineage.required_refs(p, shot)}
        self.assertIn("scene:神之塔大厅", keys)
        # 全局固定场景未设 → 不应凭空出现 scene:main
        self.assertNotIn("scene:main", keys)

    def test_named_scene_takes_over_global_scene(self):
        """全局固定场景只在本镜没有具名场景时挂载与必需（与 `_primary_scene` 同一仲裁）：
        具名场景图已覆盖的镜不再要求全局图，顶层 scene 文本不会把它拦在就绪度上。"""
        gref = self._touch("scene.png")
        named = {"id": 1, "image_prompt": "少年走进神之塔大厅"}
        p = self._project(named, scene="客厅", scene_ref=gref)
        self.assertNotIn(gref, p.design_refs(named))
        self.assertIn(self.hall, p.design_refs(named))
        self.assertEqual({"scene:神之塔大厅"},
                         {r["key"] for r in lineage.required_refs(p, named)})
        bare = {"id": 2, "image_prompt": "少年独坐"}
        self.assertIn(gref, p.design_refs(bare))
        self.assertEqual({"scene:main"}, {r["key"] for r in lineage.required_refs(p, bare)})

    def test_global_scene_text_without_sheet_only_blocks_bare_shots(self):
        """顶层 scene 只有描述文本、没有 scene_ref：绑定了具名场景图的镜就绪，
        没有具名场景的镜才缺 scene:main。"""
        named = {"id": 1, "scenes": ["神之塔大厅"], "image_prompt": "少年抬头"}
        bare = {"id": 2, "image_prompt": "少年独坐"}
        p = self._project(named, scene="茶水间")
        p.data["shots"].append(bare)
        self.assertEqual((True, []), lineage.readiness(p, named))
        self.assertEqual((False, ["scene:场景"]), lineage.readiness(p, bare))

    def test_short_name_needs_keyword(self):
        """<2 字的场景名不做泛匹配（同道具口径，避免「塔」「街」满天命中）。"""
        p = self._project({"id": 1, "image_prompt": "他抬头看塔"},
                          scenes=[{"name": "塔", "keywords": [], "sheet": self.hall}])
        self.assertEqual([], p.design_refs({"id": 1, "image_prompt": "他抬头看塔"}))

    def test_skip_design_yields_nothing(self):
        shot = {"id": 1, "image_prompt": "少年走进神之塔大厅"}
        p = self._project(shot, skip_design=True)
        self.assertEqual([], p.design_refs(shot))
        self.assertEqual([], lineage.required_refs(p, shot))

    # —— ③ 版式：环境 key art，绝不是物件转台 ——
    def test_scene_sheet_uses_environment_template_not_prop_turnaround(self):
        from kinema import sheets
        prompt = sheets.scene_sheet_prompt("阴暗的巨型殿堂", "画风前缀")
        self.assertIn("environment key frame", prompt)
        self.assertIn("广角建立镜头", prompt)
        self.assertIn("无人物", prompt)
        # 物件转台的三个标志物一个都不许出现
        for banned in ("prop design sheet", "主视图居中", "纯色浅灰底"):
            self.assertNotIn(banned, prompt, f"场景图混进了物件转台版式：{banned}")

    def test_scene_sheet_is_one_full_frame_with_no_detail_insets(self):
        """场景图整幅就是一个画面，不带任何局部特写小图。

        失败形态：措辞用「设定图 / concept sheet」时模型按美术设定集惯例在底部补
        一条材质特写缩略图带（一张大图 + 五格局部放大）。这张图会被整张挂进分镜
        当光线与陈设基准，那条带子于是作为画面内容参考进入每一镜。
        角色与道具反过来——它们**需要**细节框与转台，别把这条禁令扩散过去。"""
        from kinema import sheets
        prompt = sheets.scene_sheet_prompt("传送门遗迹", "画风前缀")
        self.assertNotIn("sheet", prompt,
                         "场景提示词里出现 sheet 就会被按设定集版式画")
        self.assertIn("single full-bleed frame", prompt)
        for banned in ("底部缩略图带", "局部特写小图", "no inset panels",
                       "no thumbnail strip"):
            self.assertIn(banned, prompt, f"缺少细节小图禁令：{banned}")
        # 反向：道具/武器仍走设定图逻辑，细节框与转台是它们的正确形态
        prop = sheets.prop_sheet_prompt({"name": "长剑", "kind": "weapon"}, "画风前缀")
        self.assertIn("weapon design sheet", prop)
        self.assertIn("局部细节框", prop)

    def test_scene_prompt_cannot_take_a_layout_template(self):
        """场景恒不附版式样板——`template_role` 讲的全是分区/细节格/色板槽位，
        正是这张图要避开的东西。参数留着就是给下一个人挖坑。"""
        from kinema import sheets
        self.assertEqual(sheets.templates_for("scene"), [])
        with self.assertRaises(TypeError):
            sheets.scene_sheet_prompt("殿堂", "前缀", n_templates=2)

    def test_scene_aspect_follows_project_not_square(self):
        """场景是全片光线与陈设基准，比例必须跟项目走；1:1 会让构图整个变形。"""
        from kinema import sheets

        class _S:
            data = {"aspect": "9:16"}
        self.assertEqual("9:16", sheets.aspect_for("scene", _S()))
        self.assertEqual("1:1", sheets.aspect_for("prop"))
        self.assertEqual("16:9", sheets.aspect_for("character"))

    def test_rules_for_scene_is_environment_rules(self):
        """局部改造回喂的版式与生成用的必须同一份（refine 分叉过一次，见 sheets.py 抬头）。"""
        from kinema import sheets
        self.assertEqual(sheets.scene_rules(), sheets.rules_for("scene"))


class TestAssetLineageViewLockstep(unittest.TestCase):
    """Studio 资产视图（AL）取材守卫：

    典型失败形态：scanner 把章节文档里**全部**有图实体下发（设定同步会把全系列几十个
    实体推进每个章节文档），前端还另写了一份出场推导（无显式 props=全部挂、
    scene 恒连全镜、文本命中缺席）——资产墙与连线双双和引擎真实挂载分叉，
    正是「另写出场推导」这条禁令要拦的原型。
    正解：逐镜挂载 = lineage.required_refs 单一真源（scanner 下发 key 列表），
    design_assets = 全章并集；前端零推导只消费。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("Lineage", pid="lin")
        self.cf = self.s.create_chapter("第一章", cid="ch01")
        refs = self.ws.root / "lin" / "assets" / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        def touch(n):
            p = refs / n
            p.write_bytes(b"x")
            return str(p)
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        data["characters"] = [
            {"name": "主角", "sheet": touch("char_a.png")},
            {"name": "路人王", "sheet": touch("char_b.png")},   # 有图但全章未出场
        ]
        data["props"] = [
            {"name": "圣剑", "keywords": [], "sheet": touch("prop_s.png")},
            {"name": "空气壶", "keywords": [], "sheet": touch("prop_k.png")},  # 有图未命中
        ]
        data["scenes"] = [
            {"name": "王城", "keywords": [], "sheet": touch("scene_w.png")},
            {"name": "深林", "keywords": [], "sheet": touch("scene_d.png")},   # 有图未命中
        ]
        data["shots"] = [
            {"id": 1, "characters": ["主角"], "image_prompt": "主角在王城拔出圣剑"},
            {"id": 2, "characters": [], "image_prompt": "空镜：王城的天空"},
        ]
        self.cf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_only_chapter_used_assets_shipped(self):
        from kinema.studio import scanner
        d = scanner.chapter_detail(self.ws.root, None, "lin", "ch01")
        keys = {a["key"] for a in d["design_assets"]}
        self.assertIn("character:主角", keys)
        self.assertIn("prop:圣剑", keys)
        self.assertIn("scene:王城", keys)
        # 有图但全章没有一镜挂到的实体绝不上墙
        self.assertNotIn("character:路人王", keys)
        self.assertNotIn("prop:空气壶", keys)
        self.assertNotIn("scene:深林", keys)
        for a in d["design_assets"]:
            self.assertTrue(a.get("thumb"), f"{a['key']} 无缩略图不该上墙")

    def test_per_shot_refs_lockstep_with_lineage(self):
        from kinema import lineage
        from kinema.studio import scanner
        d = scanner.chapter_detail(self.ws.root, None, "lin", "ch01")
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        proj = Project(self.cf, data)
        for sv, s in zip(d["shots"], data["shots"]):
            want = [r["key"] for r in lineage.required_refs(proj, s)]
            self.assertEqual(sv["design_refs"], want,
                             f"镜 {s['id']} 下发的挂载与 required_refs 分叉")

    def test_frontend_consumes_shipped_refs_never_rederives(self):
        import kinema
        src = (Path(kinema.__file__).parent / "studio_app" / "app"
               / "chapter.js").read_text(encoding="utf-8")
        self.assertIn("s.design_refs", src, "前端没有消费下发的挂载键")
        # 前端自推导挂载的三种写法：一条都不许出现（必与引擎挂载分叉）
        self.assertNotIn("s.characters || chars", src)
        self.assertNotIn("s.props || props", src)
        self.assertNotIn('a.kind === "scene"\n        || ', src)
        # 具名取景地灯箱必须带 name（只有全局 scene:main 走 null）
        self.assertIn('key(a) === "scene:main" ? null : a.name', src)


class TestStaleMarkClearsOnEveryNewImage(unittest.TestCase):
    """**新产物落地 = 旧过期标记失效 + 新血缘基线**——这条纪律必须覆盖每一条
    「这一镜换了张图」的路径，而不只是 API 生成那一条。

    典型失败症状：设定图出 v2，卡片挂上「⚠ 设定已更新 / 已变化：scene_产线中庭.png」；
    自己做了张新图 `supply` 上去，标记擦不掉，只有再走一次 `gen-image` 才消得掉。
    同一次 `supply` 还把整个 `gen["image"]` 换成新 dict、连旧的 `refs` 快照一起冲掉，
    于是这一镜从此再不参与过期判定——后者隐性，代价更大。
    """

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scene = self.root / "scene_产线中庭.png"
        self.scene.write_bytes(b"v1")
        self.src = self.root / "我自己做的图.png"
        self.src.write_bytes(b"\x89PNG" + b"0" * 64)

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _project(self):
        data = {"id": "ch", "scene": "产线中庭", "scene_ref": str(self.scene),
                "characters": [], "props": [], "aspect": "16:9",
                "shots": [{"id": 1, "image_prompt": "产线中庭全景", "dur": 4}]}
        return Project(self.root / "ch01.json", data)

    def _make_stale(self, p):
        """设定图出 v2 → 该镜挂上过期标记（走真实的 record_refs → mark_stale 链）。"""
        s = p.shots[0]
        lineage.record_refs(s, "image", [str(self.scene)])
        self.scene.write_bytes(b"v2")                 # 设定图重生成
        lineage.mark_stale(p)
        self.assertEqual(s.get("stale_refs"), ["scene_产线中庭.png"])
        return s

    def test_supplying_an_image_clears_the_stale_badge(self):
        """人亲手把新图放上去，就等同这一镜重新出过图——不必非走一次 API 才算数。"""
        from kinema.supply import supply_image
        p = self._project()
        s = self._make_stale(p)
        supply_image(p, 1, self.src, skip_check=True)
        self.assertIsNone(s.get("stale_refs"), "直传之后「⚠ 设定已更新」必须当场消掉")

    def test_supplying_rebaselines_instead_of_erasing_lineage(self):
        """清标记只是显性那一半：还要按**当前**设定图重记指纹，否则这一镜就此脱离
        血缘——下一次设定图再改它不会再报警。"""
        from kinema.supply import supply_image
        p = self._project()
        s = self._make_stale(p)
        supply_image(p, 1, self.src, skip_check=True)
        refs = ((s.get("gen") or {}).get("image") or {}).get("refs") or {}
        self.assertEqual(list(refs), [str(self.scene)], "直供后必须留下新基线")
        self.assertEqual(refs[str(self.scene)], lineage.fingerprint(str(self.scene)))
        self.assertEqual(lineage.stale_refs(s), [], "刚重设的基线不该立刻又判过期")
        self.scene.write_bytes(b"v3")                 # 设定图再出一版
        self.assertEqual(lineage.stale_refs(s), ["scene_产线中庭.png"],
                         "重设基线之后，血缘必须照常继续工作")

    def test_refine_keeps_the_baseline_and_the_badge(self):
        """局部改造是同一类写路径但分工不同：只重画一块矩形、输入侧设定图没有重新
        进过场，图并没有因此符合新设定。要修的是「别把 `refs` 冲掉」，标记照旧挂着。"""
        import ast

        import kinema
        src = (Path(kinema.__file__).parent / "refine.py").read_text(encoding="utf-8")
        self.assertIn('"refs": prev_refs', src,
                      "refine 整块替换 gen[image] 时必须把血缘快照带过来")
        self.assertNotIn("clear_stale", src, "局部改造不清过期标记（输入侧没换）")
        ast.parse(src)

    def test_every_image_landing_path_is_accounted_for(self):
        """**穷举写路径**：全引擎写 `shots[].image` 的只有四条，每条的归属在这里钉死。
        新增第五条会让断言变红，逼作者当场表态它属于哪一类——靠「记得改」迟早再漏。"""
        import ast

        import kinema
        pkg = Path(kinema.__file__).parent
        writers = set()
        for py in sorted(pkg.rglob("*.py")):
            # providers/ 是协议适配层：它写的 `body["image"]` 是**请求体**的键，
            # 手上根本没有章节文档。整包排除比给每一处加白名单准确
            if py.relative_to(pkg).parts[0] == "providers":
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript) or not isinstance(
                        node.ctx, ast.Store):
                    continue
                sl = node.slice
                if isinstance(sl, ast.Constant) and sl.value == "image":
                    writers.add(str(py.relative_to(pkg)))
        self.assertEqual(sorted(writers),
                         ["cli.py", "pipeline/candidates.py", "refine.py", "supply.py"],
                         f"写 shots[].image 的路径变了，先给它定归属：{sorted(writers)}")
        # cli（API 生成）与 supply（直供）重设基线；candidates（宫格换选）取的是
        # 同一批生成物、gen 快照原地更新不碰 refs；refine 见上一条
        self.assertIn("lineage.clear_stale", (pkg / "cli.py").read_text(encoding="utf-8"))
        self.assertIn("lineage.rebaseline", (pkg / "supply.py").read_text(encoding="utf-8"))
        cand = (pkg / "pipeline" / "candidates.py").read_text(encoding="utf-8")
        self.assertIn('get("image_candidates")', cand,
                      "宫格换选的画布快照与血缘基线取自出候选那一批")
        self.assertIn('dict(snap["refs"])', cand, "指纹取出候选那一刻的记录，不在定稿时重算")
        self.assertIn("prev_refs", cand, "无批快照时沿用旧基线，镜不退出过期判定")


class TestTextLineage(unittest.TestCase):
    """台词文本血缘：改了台词，旧配音与旧片段必须能被认出来。

    改台词而旧产物还在盘上时，成片里听到的与章节文档写的不是同一句话，而字幕
    恒按文档编译——native 尤其明显：那条人声是模型按提示词里的台词念出来的。
    判据走内容指纹而非字段名，所以手改 JSON、Studio 落盘、把 narration 拆成
    `lines[]` 都认得出（`review.STAGE_FIELDS` 那条按字段名判的边只在编辑
    经过 Gateway / batch 时生效，两者互补）。"""

    def _generated(self, shot: dict, *stages: str) -> dict:
        for st in stages:
            lineage.record_text(shot, st)
        return shot

    def test_fingerprint_reads_the_single_text_source(self):
        """`lines[]` 与等价 narration 同指纹——口径必须是 voicecast.shot_text。

        另算一份文本口径就会出现「改了 lines[] 而指纹没动」。"""
        self.assertEqual(
            lineage.text_fingerprint({"narration": "走。"}),
            lineage.text_fingerprint({"lines": [{"text": "走。"}]}))
        self.assertNotEqual(
            lineage.text_fingerprint({"narration": "走。"}),
            lineage.text_fingerprint({"narration": "别走。"}))
        self.assertIsNone(lineage.text_fingerprint({"id": 1}))

    def test_changed_text_marks_both_consuming_stages(self):
        s = self._generated({"id": "1", "narration": "旧台词"}, "audio", "clip")
        self.assertFalse(lineage.stale_text(s, "audio"))
        self.assertFalse(lineage.stale_text(s, "clip"))
        s["narration"] = "新台词"
        self.assertTrue(lineage.stale_text(s, "audio"))
        self.assertTrue(lineage.stale_text(s, "clip"))

    def test_splitting_into_lines_is_detected(self):
        """把一段 narration 拆成逐句是常见改法，且改的是**内容**（拆句会改
        字幕节奏与逐句音色），不能因为 narration 键还在就当没动。"""
        s = self._generated({"id": "1", "narration": "他没有回答。走吧。"}, "clip")
        s["lines"] = [{"text": "他没有回答。"}, {"text": "走吧。"}]
        self.assertTrue(lineage.stale_text(s, "clip"))

    def test_unregistered_and_emptied_text_do_not_false_alarm(self):
        """旧数据尚无登记 → 无从判定；台词被删空 → 旧产物是不再被使用而非内容
        不符，判成过期会留下一个 tts 永远清不掉的 retake。"""
        self.assertFalse(lineage.stale_text({"id": "1", "narration": "x"}, "audio"))
        s = self._generated({"id": "1", "narration": "旧台词"}, "audio")
        s["narration"] = ""
        self.assertFalse(lineage.stale_text(s, "audio"))

    def test_mark_grades_by_lock_and_counts_by_shot(self):
        """未锁定置 retake、已通过锁定只挂标记（done 由人工置定，引擎不自动解除）。
        计数按镜：一镜的 audio 与 clip 同时过期是常态，按阶段计会报成两倍。"""
        a = self._generated({"id": "1", "narration": "旧"}, "audio", "clip")
        b = self._generated({"id": "2", "narration": "旧"}, "audio", "clip")
        review.set_state(b, "audio", "done")
        review.set_state(b, "clip", "done")
        for s in (a, b):
            s["narration"] = "新"
        proj = FakeProject(fake_path("unused"), {"shots": [a, b]})
        self.assertEqual(lineage.mark_text_stale(proj), (1, 1))
        self.assertEqual(review.get_state(a, "audio"), "retake")
        self.assertEqual(review.get_state(a, "clip"), "retake")
        self.assertEqual(review.get_state(b, "clip"), "done")

    def test_marking_leaves_no_derived_flag_on_disk(self):
        """判定恒现算，不落过期标记字段。

        `stale_refs` 那条必须落盘是因为要读设定图文件算哈希；台词哈希不碰磁盘，
        再存一份就是同一事实的第二个真源，而且只有 `lineage mark` 会写它——
        Studio 里改完台词直到有人跑一次 CLI 才看得见。"""
        s = self._generated({"id": "1", "narration": "旧"}, "audio", "clip")
        s["narration"] = "新"
        lineage.mark_text_stale(FakeProject(fake_path("unused"), {"shots": [s]}))
        self.assertNotIn("stale_text", s)
        self.assertTrue(lineage.stale_text(s, "clip"), "判定本身照常成立")

    def test_regenerating_rebaselines_without_an_explicit_clear(self):
        """`gen.<阶段>.text_fp` 就是基线：重生成覆写它，判定自然回到干净，
        且只影响重生成的那一个阶段。"""
        s = self._generated({"id": "1", "narration": "旧"}, "audio", "clip")
        s["narration"] = "新"
        lineage.record_text(s, "audio")            # 只重跑了 tts
        self.assertFalse(lineage.stale_text(s, "audio"))
        self.assertTrue(lineage.stale_text(s, "clip"),
                        "片段里的台词还是旧的，不许被 tts 的重出顺手抹掉")

    def test_marking_writes_no_review_note(self):
        """不写重做意见。

        clip 的重做意见会被 `prompts.video_prompt` 编译进下一版视频提示词
        （「本次修正重点」）：「台词改了」对模型毫无信息量——新台词本来就在同一
        条提示词里——而这一步按秒计费。写了还会盖掉作者自己的重做意见
        （`set_state` 不给新意见时本会保留旧的）。"""
        from kinema.pipeline import prompts
        s = self._generated(
            {"id": "1", "dur": 4, "video_prompt": "推近", "narration": "旧台词"},
            "audio", "clip")
        review.set_state(s, "clip", "retake", note="第3秒左手穿模")
        s["narration"] = "新台词"
        lineage.mark_text_stale(FakeProject(fake_path("unused"), {"shots": [s]}))
        self.assertEqual(review.get_note(s, "clip"), "第3秒左手穿模",
                         "引擎记账不许盖掉作者写的重做意见")
        self.assertIn("本次修正重点（务必执行）：第3秒左手穿模",
                      prompts.video_prompt(s, native=True),
                      "编译进提示词的必须是作者的意见，不是引擎的过期记账")

    def test_omitted_shot_is_out_of_scope(self):
        s = self._generated({"id": "1", "narration": "旧"}, "audio")
        s["narration"] = "新"
        review.set_state(s, "shot", "omt")
        self.assertEqual(lineage.text_sweep(FakeProject(fake_path("x"), {"shots": [s]})), [])

    def test_both_producers_register_the_baseline(self):
        """两处落盘都要记指纹——漏一处，那个阶段从此没有台词血缘，
        台词再改多少次都不报警。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "cli.py"
               ).read_text(encoding="utf-8")
        for stage in ("audio", "clip"):
            self.assertIn(f'lineage.record_text(s, "{stage}")', src)


class TestImageToClipLineage(unittest.TestCase):
    """image→clip 那条血缘边：片段以分镜图为首帧/画面参考，图换底片段就过期。

    缺这条边的实测症状：`gen-image --force` 换了底片后直接跑 `gen-video`，
    输出「完成 · 用时 0s」——一帧没重生、静默跳过，成片里新图旧片并存。"""

    _PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082")

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)
        self.img = self.tmp / "shot_1_16x9.png"
        self.img.write_bytes(self._PNG)

    def tearDown(self):
        self.tmp_ctx.cleanup()
        self.env.restore()

    def _project(self, shot_over=None) -> Project:
        clip = self.tmp / "shot_1_16x9.mp4"
        clip.write_bytes(b"clip")
        shot = {"id": 1, "dur": 4.0, "image": str(self.img),
                "clip": str(clip), "clips": {"16:9": str(clip)}}
        shot.update(shot_over or {})
        data = {"id": "ch01", "motion": "dubbed", "aspect": "16:9",
                "shots": [shot]}
        cf = self.tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project(cf, data)

    def test_mark_stale_retakes_clip_when_image_changed(self):
        p = self._project()
        s = p.shots[0]
        lineage.record_refs(s, "clip", [str(self.img)])
        self.img.write_bytes(self._PNG + b"v2")
        r, f = lineage.mark_stale(p)
        self.assertEqual((r, f), (1, 0))
        self.assertEqual(review.get_state(s, "clip"), "retake")
        self.assertIsNone(review.get_note(s, "clip"),
                          "不写重做意见——会被编译进下一版视频提示词")

    def test_locked_clip_is_flagged_not_retaken(self):
        p = self._project()
        s = p.shots[0]
        lineage.record_refs(s, "clip", [str(self.img)])
        review.set_state(s, "clip", "done")
        self.img.write_bytes(self._PNG + b"v2")
        r, f = lineage.mark_stale(p)
        self.assertEqual((r, f), (0, 1))
        self.assertEqual(review.get_state(s, "clip"), "done", "锁定由人裁决")

    def test_unchanged_image_marks_nothing(self):
        p = self._project()
        lineage.record_refs(p.shots[0], "clip", [str(self.img)])
        self.assertEqual(lineage.mark_stale(p), (0, 0))
        self.assertNotEqual(review.get_state(p.shots[0], "clip"), "retake")


class TestGenImageExpiresClip(unittest.TestCase):
    """事件边：gen-image 落了新图的那一刻就知道片段过期了，不必等人跑
    `lineage mark`——未锁定置 retake，锁定只点名。"""

    _PNG = TestImageToClipLineage._PNG

    class _Res:
        def __init__(self, path):
            self.path, self.cost = str(path), 0.0

    class _Prov:
        name = "fake"
        prompt_lang = "zh"

        def generate(self, prompt, out_path, **kw):
            q = Path(out_path)
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(TestGenImageExpiresClip._PNG + b"regen")
            return TestGenImageExpiresClip._Res(q)

    class _Router:
        force_mock = True

        def resolve(self, capability, profile):
            return TestGenImageExpiresClip._Prov(), {}

    class _Store:
        def canvas(self, aspect):
            return (1920, 1080)

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)

    def tearDown(self):
        self.tmp_ctx.cleanup()
        self.env.restore()

    def _project(self, clip_state=None) -> Project:
        img = self.tmp / "old.png"
        img.write_bytes(self._PNG)
        clip = self.tmp / "shot_1_16x9.mp4"
        clip.write_bytes(b"clip")
        shot = {"id": 1, "dur": 4.0, "image_prompt": "站在门口",
                "image": str(img), "clip": str(clip),
                "clips": {"16:9": str(clip)}}
        data = {"id": "ch01", "motion": "dubbed", "aspect": "16:9",
                "shots": [shot]}
        cf = self.tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        p = Project.load(cf)
        s = p.shots[0]
        review.set_state(s, "image", "retake")
        if clip_state:
            review.set_state(s, "clip", clip_state)
        return p

    def _run(self, p):
        import contextlib
        import io

        from kinema import cli
        with contextlib.redirect_stdout(io.StringIO()):
            cli.stage_gen_image(p, self._Store(), self._Router(), only="1")

    def test_new_image_retakes_existing_clip(self):
        p = self._project()
        self._run(p)
        self.assertEqual(review.get_state(p.shots[0], "clip"), "retake")

    def test_locked_clip_survives_with_a_warning_only(self):
        p = self._project(clip_state="done")
        self._run(p)
        self.assertEqual(review.get_state(p.shots[0], "clip"), "done")
