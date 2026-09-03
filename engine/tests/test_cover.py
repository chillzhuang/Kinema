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

"""封面系统单测（纯函数，零 ffmpeg 依赖）：key visual 提示词拼装 + 排版
filtergraph 构造。核心不变量：**防字地板、标题安全区、系列感同版式**——
背景提示词必带"避免文字"与"底部留白"，排版层同参数必产同版式。"""
from __future__ import annotations

import unittest

from kinema.errors import ProjectError

from kinema.fonts import FONT_STYLES, default_style, resolve_font
from kinema.pipeline.cover import (COVER_SIZES, DEFAULT_ASPECT,
                                      DEFAULT_ASPECTS, build_text_filter,
                                      cover_prompt, size_for)

_SERIES = {
    "title": "凡人问道",
    "scene": "云海之上的悬浮剑冢",
    "characters": [
        {"name": "韩立", "appearance": "青衫剑修，剑眉凤目"},
        {"name": "南宫婉", "appearance": "白衣胜雪"},
        {"name": "无名老祖"},
    ],
}


class TestCoverPrompt(unittest.TestCase):
    def test_series_prompt_shape(self):
        p = cover_prompt(_SERIES, style_prefix="顶级3D国漫渲染风格，")
        self.assertTrue(p.startswith("顶级3D国漫渲染风格，"))     # 画风前缀在最前
        self.assertIn("主角韩立", p)                             # 首位角色是主角
        self.assertIn("配角南宫婉", p)
        self.assertIn("云海之上的悬浮剑冢", p)                   # 场景入题
        self.assertIn("有故事的瞬间", p)                         # 系列缺省=叙事定妆瞬间
        self.assertIn("底部三分之一保持低密度留白", p)           # 标题安全区
        self.assertIn("避免出现：任何文字", p)                   # 防字地板

    def test_dna_carries_the_key_visual_craft_stack(self):
        """key visual 五条工法栈一条不许丢（判据：Netflix key art 实测 + 动画海报构图理论）。

        DNA 若只剩「错落纵深+仰角布光」，产出就是四人正对镜头一字排开的
        站桩全家福——表情空、无叙事、无世界观意象、无色彩战略，正是要禁的形态。"""
        p = cover_prompt(_SERIES)
        self.assertIn("表情必须有戏", p)                 # ① 层级比例：表情强于一切
        self.assertIn("人数宁少勿挤", p)
        self.assertIn("世界观符号或威胁意象", p)         # ② 环境叙事背景层
        self.assertIn("尺度对比", p)
        self.assertIn("绝不全员正对镜头排排站", p)       # ③ 叙事瞬间：站桩禁令
        self.assertIn("对角线动势", p)
        self.assertIn("环绕式元素层", p)                 # ④ 元素编排（单调感的根因）
        self.assertIn("绝不遮脸", p)
        self.assertIn("框架性前景", p)                   # ⑤ 框式构图锁视线
        self.assertIn("一个主导色", p)                   # ⑥ 色彩战略
        self.assertIn("对撞色", p)
        self.assertIn("明暗对比强烈", p)
        self.assertIn("亮部集中在主角面部", p)           # ⑦ 布光聚焦
        pe = cover_prompt(_SERIES, lang="en")
        for needle in ("hieratic scale", "never a blank face", "looming threat",
                       "never the whole cast lined up", "diagonal", "element orchestration",
                       "framing foreground", "one dominant hue", "chiaroscuro"):
            self.assertIn(needle, pe, f"EN DNA 缺「{needle}」")

    def test_chapter_prompt_desc_priority(self):
        # 章节：desc（指挥层精写）优先；缺省回落章节标题氛围句
        p = cover_prompt(_SERIES, chapter_title="血战黑风寨",
                         desc="韩立持剑回首，身后火光冲天")
        self.assertIn("韩立持剑回首", p)
        self.assertNotIn("本集氛围", p)
        p2 = cover_prompt(_SERIES, chapter_title="血战黑风寨")
        self.assertIn("本集氛围：血战黑风寨", p2)

    def test_backdrop_falls_back_to_registered_scenes(self):
        """顶层 scene 空、取景地登记在 scenes[] 时封面必须拿得到背景——否则工法栈的
        「世界观意象」会给一部便利店夜戏编出龙骨与符文。"""
        series = {"title": "深夜便利店", "characters": [{"name": "老周"}],
                  "scenes": [{"name": "便利店", "desc": "雨夜街角二十四小时便利店室内"},
                             {"name": "巷口", "desc": "湿漉漉的柏油路"},
                             {"name": "病房", "desc": "第三处不入题"}]}
        p = cover_prompt(series)
        self.assertIn("背景为雨夜街角二十四小时便利店室内；湿漉漉的柏油路", p)
        self.assertNotIn("第三处不入题", p)
        self.assertIn("背景为云海之上的悬浮剑冢", cover_prompt(_SERIES), "顶层 scene 仍优先")

    def test_photoreal_dna_swaps_fantasy_motifs_for_the_location(self):
        """写实档没有能量流与符纹可画：纵深与元素编排两条换成取景地的真实物件与
        光源，其余工法条款与动画档共用；动画档逐字不变。"""
        real = cover_prompt(_SERIES, photoreal=True)
        self.assertIn("电影海报主视觉构图", real)
        self.assertIn("顶级电影海报 key visual 构图工法", real)
        self.assertIn("取景地里真实存在的物件与光源", real)
        self.assertNotIn("发光符纹绕人物", real)
        self.assertNotIn("巨大世界观符号", real)
        self.assertIn("底部三分之一保持低密度留白", real)
        anime = cover_prompt(_SERIES)
        self.assertIn("动画海报主视觉构图", anime)
        self.assertIn("能量流与发光符纹", anime)
        real_en = cover_prompt(_SERIES, lang="en", photoreal=True)
        self.assertIn("photoreal film key visual poster", real_en)
        self.assertNotIn("glowing sigils staggered", real_en)

    def test_no_cast_switches_to_the_object_stack(self):
        """没有阵容句就走静物意象档：图书/解说/展示类项目没有角色，动画/写实两档的
        「主角脸部最亮、表情必须有戏」会逼模型凭空造人。判据只有一条——阵容句为空。"""
        objless = {"title": "图书解说", "scene": "暖光书房的木质书桌"}
        p = cover_prompt(objless, desc="一本深蓝布面无字精装书直立在桌上")
        self.assertIn("静物意象海报主视觉构图：一本深蓝布面无字精装书", p)
        self.assertIn("顶级静物意象海报 key visual 构图工法", p)
        self.assertIn("没有任何人物、面孔或人形剪影", p)
        self.assertIn("不添加能量流、发光符纹、粒子特效、飞溅火星", p)
        self.assertIn("背景为暖光书房的木质书桌", p)
        self.assertIn("底部三分之一保持低密度留白", p)     # 标题安全区条款不随档丢失
        self.assertIn("避免出现：任何文字", p)             # 防字地板条款不随档丢失
        for figure_only in ("表情必须有戏", "亮部集中在主角面部", "能量流与发光符纹绕人物",
                            "主角定格在一个有故事的瞬间"):
            self.assertNotIn(figure_only, p, f"静物档不得残留人物档条款「{figure_only}」")
        # 缺省命题（无 desc）也不许回落到「主角定格」
        self.assertIn("核心主体定格在一个有指向性的瞬间", cover_prompt(objless))
        self.assertIn("本集氛围：不要回答，核心主体与环境意象贴合本集命题",
                      cover_prompt(objless, chapter_title="不要回答"))
        # 有角色但 --cast none：构图全交 desc，同样不得再喂人物档
        p2 = cover_prompt(_SERIES, cast_names=[], desc="只拍一把剑插在剑冢")
        self.assertIn("静物意象海报主视觉构图：只拍一把剑插在剑冢", p2)
        self.assertNotIn("表情必须有戏", p2)
        # 写实档 + 无阵容：静物档优先于媒介分档
        p3 = cover_prompt(objless, photoreal=True)
        self.assertIn("顶级静物意象海报 key visual 构图工法", p3)
        self.assertNotIn("电影海报主视觉构图", p3)
        pe = cover_prompt(objless, lang="en", desc="a blank cloth-bound hardcover")
        self.assertIn("object-driven key visual poster composition: a blank cloth-bound", pe)
        self.assertIn("no people, faces or human silhouettes", pe)
        self.assertIn("Avoid: any text", pe)
        for figure_only in ("never a blank face", "hieratic scale — the protagonist",
                            "glowing sigils staggered"):
            self.assertNotIn(figure_only, pe)
        # 有阵容的两档逐字不变
        self.assertIn("表情必须有戏", cover_prompt(_SERIES))
        self.assertIn("表情必须有戏", cover_prompt(_SERIES, photoreal=True))

    def test_cli_feeds_photoreal_and_theme_fallback(self):
        """cmd_cover 源级接线：写实档判定按 identity_sheet 传给两条封面路；章节
        画面命题缺 --desc 与 cover_prompt 时回落章节 theme（run 收尾的自动封面
        只有这一句现成的剧情命题）。"""
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.cmd_cover)
        self.assertIn('photoreal = bool(params.get("identity_sheet"))', src)
        self.assertEqual(src.count("photoreal=photoreal"), 2)
        self.assertIn('or (proj.data.get("theme") or "").strip()', src)

    def test_en_prompt_uses_en_dna(self):
        p = cover_prompt(_SERIES, style_prefix="cinema style, ", lang="en")
        self.assertIn("vertical anime key visual poster", p)
        self.assertIn("protagonist 韩立", p)
        self.assertIn("Avoid: any text", p)                      # 防字地板英文位
        self.assertNotIn("避免出现", p)

    def test_orientation_word_follows_aspect(self):
        """构图方向词是**唯一随画幅分支的措辞**（DNA 与题字留白画幅中性）：
        缺省双比例集里 4:3 横画布若也收到「竖版海报构图」，模型会在横画布上
        挤出竖版构图的留白。"""
        self.assertIn("竖版动画海报", cover_prompt(_SERIES, aspect="3:4"))
        self.assertIn("横版动画海报", cover_prompt(_SERIES, aspect="4:3"))
        self.assertIn("方形动画海报", cover_prompt(_SERIES, aspect="1:1"))
        self.assertIn("horizontal anime key visual",
                      cover_prompt(_SERIES, lang="en", aspect="16:9"))

    def test_cast_capped(self):
        many = {"characters": [{"name": f"角色{i}"} for i in range(9)]}
        p = cover_prompt(many)
        self.assertIn("角色0", p)
        self.assertNotIn("角色3", p)                             # 阵容 ≤3：清晰单焦点赢过拥挤群像

    def test_ref_base_contract_reused_from_prompts(self):
        """附了设定图就注入 REF_BASE 契约句（prompts.py 单一真源，绝不另写一份）。

        缺契约句时，设定图在参考里、提示词却没提到的配角归模型自由发挥——
        蓝色吉祥物能顺着 DNA 的「暖色前进」被染成橙色布丁。契约句的
        「desc 未提及的角色不画」正是为此而设。"""
        from kinema.pipeline import prompts
        p = cover_prompt(_SERIES, ref_base=True)
        self.assertIn(prompts.REF_BASE_ZH, p)
        self.assertNotIn(prompts.REF_BASE_ZH, cover_prompt(_SERIES))   # 无参考不空谈
        pe = cover_prompt(_SERIES, lang="en", ref_base=True)
        self.assertIn(prompts.REF_BASE_EN, pe)

    def test_cli_feeds_desc_and_refbase_to_both_cover_paths(self):
        """cmd_cover 源级接线守卫：系列与章节两条路都必须消费 --desc 并按实况传 ref_base。

        只让章节循环读 args.desc，系列封面提示词就会静默忽略它——
        用户精写的 key visual 描述白写，跑的一直是缺省文案。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_cover)
        self.assertEqual(src.count("cover_mod.cover_prompt("), 2)
        series_seg = src.split("---- 系列主视觉")[1].split("---- 章节封面")[0]
        self.assertIn("desc=(args.desc", series_seg, "系列封面没消费 --desc")
        self.assertEqual(src.count("ref_base="), 2, "两条路都要按实况传 ref_base")
        self.assertEqual(src.count("aspect=asp"), 2,
                         "两条路都要逐比例拼提示词（方向词随画幅，循环外拼一次="
                         "给横画布也发竖版构图）")

    def test_cli_chapter_cover_costs_enter_the_ledger(self):
        """封面与分镜图/简笔板同为图像生成：章节封面费用必须 add_cost 入台账——
        不入账则事前/事后额度闸对封面双双失明，且失明外溢到后续 gen-video 的
        额度判断。系列主视觉走系列台账（Series.add_cost），不摊到任何一章。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_cover)
        chapter_seg = src.split("波次 3")[1]
        self.assertIn('add_cost("image"', chapter_seg)
        series_seg = src.split("---- 系列主视觉")[1].split("波次 3")[0]
        self.assertIn('s.add_cost("image"', series_seg)
        self.assertNotIn("p2.add_cost", series_seg)

    def test_title_art_prompt_hard_constraints(self):
        """AI 题字段的三条硬约束一条不许丢：画面不变契约 / 字数钉死+逐字复述 /
        白名单式防字地板（除标题外禁一切其他文字）。"""
        from kinema.pipeline.cover import title_art_prompt
        p = title_art_prompt("神渊主播")
        self.assertIn("完全保持不变", p)                 # 不声明就是整幅重画
        self.assertIn("「神渊主播」", p)
        self.assertIn("恰好 4 个汉字", p)                # 字数钉死
        self.assertIn("神、渊、主、播", p)               # 逐字复述压错字率
        self.assertIn("除上述标题外", p)                 # 白名单式防字地板
        self.assertIn("不出现任何其他文字", p)
        self.assertNotIn("副标题", p)                    # 无副标题不出这一格
        p2 = title_art_prompt("神渊主播", subtitle="第 1 集")
        self.assertIn("副标题「第 1 集」", p2)
        self.assertIn("除上述标题与副标题外", p2)
        # 章节题字锁系列感：声明字形沿用第二张参考图
        p3 = title_art_prompt("神渊主播", series_ref=True)
        self.assertIn("字形沿用第二张参考图", p3)
        self.assertNotIn("第二张参考图", p)              # 系列封面自己不空谈
        pe = title_art_prompt("ABYSS", lang="en", subtitle="EP 1")
        self.assertIn('"ABYSS"', pe)
        self.assertIn("no more, no less", pe)
        self.assertIn("no other text", pe)
        with self.assertRaises(ProjectError):
            title_art_prompt("  ")

    def test_cli_two_pass_ai_title_with_typeset_escape(self):
        """cmd_cover 源级接线：缺省两段式 AI 题字（成品来自第二次生成），
        `--typeset-title` 才走 drawtext 排版；章节题字必须带系列成品作字形参考。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_cover)
        self.assertIn("typeset_title", src)              # 逃生舱在位
        self.assertIn("title_art_prompt(", src)          # AI 题字段
        self.assertEqual(src.count("prov.generate("), 2, "两段式=背景任务与题字任务各一次生成")
        self.assertIn("[title_anchor] if title_anchor else None", src,  # 章节字形沿用系列成品
                      "章节题字丢了系列封面参考——字形逐章漂移")
        # compose_cover 只准出现在逃生舱分支（缺省 AI 题字不得再叠 drawtext）
        self.assertEqual(src.count("cover_mod.compose_cover("), 1)

    def test_cast_leads_first_not_roster_order(self):
        """主角优先：role 含「主」的角色排最前——长篇改编 roster 几十人、
        登记序不等于戏份序，按序取前 N 会把排位靠后的主角组漏出封面
        （主角登记在第 6 位时，按登记序取前 4 根本轮不到它）。"""
        s = {"characters": [
            {"name": "路人甲", "role": "反派"},
            {"name": "路人乙"},
            {"name": "洛", "role": "男主"},
            {"name": "雀", "role": "女主·节拍者"},
        ]}
        p = cover_prompt(s)
        self.assertIn("主角洛", p)
        self.assertIn("主角雀", p)
        self.assertLess(p.index("洛"), p.index("路人甲"))   # 主角组整体前置
        self.assertNotIn("路人乙", p)                        # 上限 3：主角组+第一配角


class TestBuildTextFilter(unittest.TestCase):
    def _f(self, **kw):
        args = dict(title="凡人问道", width=1080, height=1440)
        args.update(kw)
        return build_text_filter(**args)

    def test_default_aspect_is_3_4(self):
        self.assertEqual(DEFAULT_ASPECT, "3:4")
        self.assertEqual(COVER_SIZES["3:4"], (1080, 1440))       # 宽3高4（用户口径）

    def test_default_dual_aspects(self):
        # 缺省一次出竖 3:4 + 横 4:3 双套
        self.assertEqual(DEFAULT_ASPECTS, ("3:4", "4:3"))
        self.assertEqual(size_for("4:3"), (1440, 1080))

    def test_arbitrary_aspect_short_side_1080(self):
        # 任意比例：短边 1080 自动推导（偶数对齐），AI 自由传参不改表
        self.assertEqual(size_for("21:9"), (2520, 1080))
        self.assertEqual(size_for("2:3"), (1080, 1620))
        w, h = size_for("1:2.35")
        self.assertEqual(w, 1080)
        self.assertEqual(h % 2, 0)
        with self.assertRaises(ProjectError):
            size_for("0:3")


class TestFontStyles(unittest.TestCase):
    def test_style_registry_complete(self):
        # 四种风格齐备且各有候选链（排版按题材归位字体，不落单一默认黑体）
        for k in ("song", "kai", "hei", "yuan"):
            self.assertTrue(FONT_STYLES[k]["candidates"], k)
            self.assertTrue(FONT_STYLES[k]["label"], k)

    def test_default_style_by_profile(self):
        self.assertEqual(default_style("anime_xianxia"), "kai")   # 古风→楷
        self.assertEqual(default_style("anime3d"), "kai")
        self.assertEqual(default_style("anime_ldr"), "hei")       # 爱死机→粗黑
        self.assertEqual(default_style("game_vn"), "hei")
        self.assertEqual(default_style("storybook"), "yuan")      # 绘本→圆体
        self.assertEqual(default_style("narration"), "song")      # 缺省宋体衬线

    def test_resolve_never_crashes(self):
        # 候选链落空也走借用链→find_font_cjk，不抛错
        for k in ("song", "kai", "hei", "yuan", None, "不存在的风格"):
            resolve_font(k)                                       # 不抛即过
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".ttf") as f:
            self.assertEqual(resolve_font(f.name), f.name)        # 自备字体路径直通


class TestBuildTextFilterLayout(unittest.TestCase):
    def _f(self, **kw):
        args = dict(title="凡人问道", width=1080, height=1440)
        args.update(kw)
        return build_text_filter(**args)

    def test_scrim_and_title(self):
        f = self._f()
        self.assertIn("geq=", f)                                 # 底部渐变压暗（可读性）
        self.assertIn("format=rgb24", f)
        self.assertEqual(f.count("drawtext="), 1)                # 无副标题=只有主标题
        self.assertIn("x=(w-text_w)/2", f)                       # 水平居中

    def test_subtitle_decorated_with_accent(self):
        f = self._f(subtitle="第 3 集", accent="#e0311a")
        self.assertEqual(f.count("drawtext="), 2)
        self.assertIn("— 第 3 集 —", f)                          # 集数装饰横线
        self.assertIn("fontcolor=#e0311a", f)                    # 画风主题色

    def test_series_consistency_same_params_same_layout(self):
        # 系列感的锁：同参数必产完全相同的版式串（章节间零漂移）
        self.assertEqual(self._f(subtitle="第 1 集"), self._f(subtitle="第 1 集"))

    def test_font_size_adapts_to_title_length(self):
        import re
        short = int(re.search(r"fontsize=(\d+)", self._f(title="凡人")).group(1))
        long = int(re.search(r"fontsize=(\d+)",
                             self._f(title="凡人修仙风云录之问道九重天")).group(1))
        self.assertGreater(short, long)                          # 长标题自动缩字号
        self.assertLessEqual(short, round(1080 * 0.14))          # 字号有上限

    def test_special_chars_escaped(self):
        self.assertIn(r"text='问道\:飞升'", self._f(title="问道:飞升"))

    def test_empty_title_raises(self):
        with self.assertRaises(ProjectError):
            self._f(title="  ")


if __name__ == "__main__":
    unittest.main()


class TestCoverReminderReachesEveryExit(unittest.TestCase):
    """生图收尾的封面提醒必须在「空计划」出口也发得出来。

    提醒若只挂在 `stage_gen_image` 的最后一行，图出齐后重跑同一条命令走的是
    `if not plan: return`，提醒一次都不出现。agent 出图模式两条路都绕开——首轮抛
    「工单已开」到不了末尾，画完重跑正好落在空计划出口。封面就能一路缺到成片。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from tests.support import LocalBackendEnv
        from kinema.workspace import Workspace
        self.env = LocalBackendEnv()
        self.env.enable()
        self.addCleanup(self.env.restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        s = self.ws.create_project("岸上那一抖", pid="dog")
        self.cf = s.create_chapter("第一章", cid="ch01")
        imgs = self.cf.parent / f"{self.cf.stem}_work" / "images"
        imgs.mkdir(parents=True)
        (imgs / "shot_1.png").write_bytes(b"png")
        data = self.ws.store.load_chapter("dog", "ch01")
        data["shots"] = [{"id": 1, "dur": 3, "narration": "第一镜。",
                          "image_prompt": "狗站在浅滩上",
                          "image": str(imgs / "shot_1.png"),
                          "review": {"image": {"state": "done"}}}]
        self.ws.store.save_chapter("dog", "ch01", data)

    def _rerun(self) -> str:
        """图已全出的重跑：计划为空，绝不解析 provider（解析了就是又要花钱）。"""
        import contextlib
        import io
        from kinema import cli
        from kinema.project import Project

        class _NoRouter:
            def resolve(self, capability, profile):
                raise AssertionError("空计划不该解析 provider")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.stage_gen_image(Project.load(self.cf), None, _NoRouter())
        return buf.getvalue()

    # 断言认收尾提醒**独有**的命令行：lint 的封面维度同在这条输出里，
    # 按「封面」二字断言两者分不开，钉不住修的是哪一处
    _SERIES_TODO = "python3 -m kinema cover dog"
    _CHAPTER_TODO = "--chapter ch01"

    def test_empty_plan_exit_still_names_the_missing_covers(self):
        out = self._rerun()
        self.assertIn(self._SERIES_TODO, out)                 # 系列主视觉
        self.assertIn(self._CHAPTER_TODO, out)                # 本章封面

    def test_silent_once_both_covers_exist(self):
        covers = self.ws.root / "dog" / "assets" / "covers"
        covers.mkdir(parents=True)
        (covers / "series_3x4.png").write_bytes(b"png")
        (covers / "ch01_3x4.png").write_bytes(b"png")
        out = self._rerun()
        self.assertNotIn(self._SERIES_TODO, out)
        self.assertNotIn(self._CHAPTER_TODO, out)


class TestChapterCoverWriteLock(unittest.TestCase):
    """章节封面的登记必须按磁盘最新态应用（`Project.mutate`）：生成期以分钟计，
    其间 tts/gen-video/Studio 子进程都可能写过章节文档，用生成前的旧副本整份
    save 会把它们的写入抹掉（与系列分支的 `s.commit()` 同一条纪律）。"""

    def test_registration_applies_on_the_latest_disk_state(self):
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_cover)
        seg = src[src.index("波次 3"):]
        self.assertIn("Project.mutate", seg,
                      "登记须按磁盘最新态应用；自持 save_lock 再调 proj.save() 会"
                      "嵌套申请同一把文件锁（flock 按文件描述判归属）")
        self.assertNotIn("proj.save()\n", seg,
                         "登记不得走旧副本整份 save")


class TestCoverCast(unittest.TestCase):
    """封面阵容的显式点名（`cover --cast`）：缺省规则里 `role` 只排序不筛人——
    正好 3 人的项目怎么设 role 都是全员上封面，desc 里的否定句压不住引擎注入的
    阵容句（带全文外观的正向指令更强势），撵人只有显式点名这一条路。"""

    SERIES = {"characters": [
        {"name": "凯尔", "role": "男主", "appearance": "沙色斗篷"},
        {"name": "瑟拉", "role": "女主", "appearance": "赭红头巾"},
        {"name": "白甲兵", "role": "士兵", "appearance": "骨白色分件式陶瓷板甲"}]}

    def test_default_roster_takes_everyone_under_the_cap(self):
        from kinema.pipeline.cover import cover_prompt
        p = cover_prompt(self.SERIES)
        self.assertIn("白甲兵", p)

    def test_named_cast_excludes_the_rest(self):
        from kinema.pipeline.cover import cover_prompt
        p = cover_prompt(self.SERIES, cast_names=["凯尔", "瑟拉"])
        self.assertIn("主角凯尔", p)
        self.assertIn("配角瑟拉", p)
        self.assertNotIn("白甲兵", p)
        self.assertNotIn("陶瓷板甲", p, "被撵走的角色不得再带外观全文进提示词")

    def test_named_order_wins_over_lead_sorting(self):
        from kinema.pipeline.cover import cover_prompt
        p = cover_prompt(self.SERIES, cast_names=["瑟拉", "凯尔"])
        self.assertLess(p.index("瑟拉"), p.index("凯尔"))

    def test_empty_cast_drops_the_roster_clause_cleanly(self):
        from kinema.pipeline.cover import cover_prompt
        p = cover_prompt(self.SERIES, cast_names=[],
                         desc="只拍一只靴子踏进沙丘")
        for n in ("凯尔", "瑟拉", "白甲兵"):
            self.assertNotIn(n, p)
        self.assertIn("构图：只拍一只靴子踏进沙丘", p, "空阵容不得留下悬空的顿号")

    def test_cli_rejects_unregistered_names(self):
        """错名静默掉人后，出的封面与点名意图对不上还查不到原因——必须硬拦。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_cover)
        self.assertIn("--cast 点名了名册里没有的角色", src)
