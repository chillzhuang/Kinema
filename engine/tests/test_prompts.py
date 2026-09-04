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

"""两条最贵策略的回归守护：
pipeline/prompts.py（发给 Seedream/Seedance 的每一个字）与
voicecast 镜级音色解析（每一句计费合成用哪把声音）。"""
from __future__ import annotations

import unittest

from kinema import voicecast
from kinema.pipeline import prompts
from kinema.pipeline.variation import MULTISHOT_RE
from kinema.project import Project
from tests.support import fake_path


class _ProjectStub:
    def __init__(self, voices=None):
        self.voices = voices or {}
        self.data = {}


class _StoreStub:
    def __init__(self, table=None):
        self.table = table or {}

    def resolve_voice(self, ref):
        if not ref:
            return None
        return self.table.get(ref, ref)


class TestImagePrompt(unittest.TestCase):
    def test_cinematography_floor_injected(self):
        s = {"image_prompt": "老者抚须大笑", "framing": "特写", "lighting": "侧逆光"}
        p = prompts.image_prompt(s)
        self.assertIn("特写", p)
        self.assertIn("侧逆光", p)
        self.assertLess(p.index("特写"), p.index("老者抚须大笑"))  # 镜头语言块前置

    def test_floor_not_duplicated_when_already_in_body(self):
        s = {"image_prompt": "特写：老者抚须", "framing": "特写"}
        self.assertEqual(prompts.image_prompt(s).count("特写"), 1)

    def test_en_lang_uses_en_fields_without_zh_floor(self):
        s = {"image_prompt": "中文体", "image_prompt_en": "an old man laughing",
             "framing": "特写"}
        p = prompts.image_prompt(s, prompt_lang="en")
        self.assertIn("an old man laughing", p)
        self.assertNotIn("特写", p)          # 英文体不混中文字段
        self.assertNotIn("中文体", p)

    def test_prefix_block_scene_order_and_negative(self):
        s = {"image_prompt": "回眸", "negative_prompt": "多余的手指"}
        p = prompts.image_prompt(s, style_prefix="水墨画风", character_block="洛——白衣少年",
                                 scene="山巅道观")
        self.assertLess(p.index("水墨画风"), p.index("洛——白衣少年"))
        self.assertLess(p.index("洛——白衣少年"), p.index("山巅道观"))
        self.assertLess(p.index("山巅道观"), p.index("回眸"))
        self.assertIn("。避免出现：多余的手指", p)

    def test_anti_text_floor_injected_without_user_negative(self):
        # 分镜图本体必须无字：没写 negative 的镜也要拿到地板
        p = prompts.image_prompt({"image_prompt": "回眸"})
        self.assertIn("。避免出现：字幕、画面文字、水印", p)

    def test_anti_text_floor_appended_after_user_negative(self):
        # 顺序硬约束：作者原话在前、地板在后（下游与既有断言按此定位）
        p = prompts.image_prompt({"image_prompt": "回眸", "negative_prompt": "多余的手指"})
        self.assertIn("。避免出现：多余的手指，字幕、画面文字、水印", p)
        self.assertLess(p.index("多余的手指"), p.index("字幕、画面文字、水印"))

    def test_anti_text_floor_not_duplicated(self):
        # 作者已自写「字幕」→ 不重复注入（沿用视频侧去重口径）
        p = prompts.image_prompt({"image_prompt": "回眸", "negative_prompt": "字幕"})
        self.assertEqual(p.count("字幕"), 1)
        # 英文写法同样认（subtitle 大小写不敏感）
        pe = prompts.image_prompt({"image_prompt": "回眸", "negative_prompt": "Subtitles"})
        self.assertNotIn("字幕", pe)

    def test_anti_text_floor_en(self):
        p = prompts.image_prompt({"image_prompt_en": "a girl turning back"},
                                 prompt_lang="en")
        self.assertIn("subtitles, captions, on-screen text, watermark", p)
        self.assertNotIn("字幕、画面文字", p)      # 英文体不混中文地板

    def test_anti_text_floor_dedups_per_word_not_per_block(self):
        """去重必须**逐词**：只认「字幕」一个锚就两头都错——
        作者写了它就整块跳过（连带丢掉「画面文字/水印」的保护），没写它就整块注入
        （哪怕作者已经写过「水印」）。而本仓自己的字段范例教作者写的正是「文字水印」，
        按块去重会让写了 negative 的正镜成片地把「水印」发两遍。"""
        # 作者写过的词不再补，没写的照补
        out = prompts.with_text_floor("画面抖动, 多余手指, 文字水印")
        self.assertEqual(out.count("水印"), 1, "作者写过「水印」就不该再发一遍")
        self.assertIn("字幕", out, "去重不许连带丢掉作者没写的那两个词")
        self.assertIn("画面文字", out)
        # 只写了「字幕」的镜仍要拿到另外两词（命中一个锚不等于三词齐全）
        self.assertEqual(prompts.with_text_floor("字幕"), "字幕，画面文字、水印")
        # 全都写过 → 一个字不追加
        self.assertEqual(prompts.with_text_floor("字幕、画面文字、水印"), "字幕、画面文字、水印")
        # 跨语种同义词也认（中文 negative 里写英文词是本仓常见写法）
        self.assertNotIn("字幕", prompts.with_text_floor("Subtitles"))
        self.assertNotIn("watermark", prompts.with_text_floor("no watermark", "en").lower()
                         .replace("no watermark", ""))
        # 顺序硬约束不变：作者原话恒在最前
        self.assertTrue(prompts.with_text_floor("多余手指").startswith("多余手指"))

    def test_text_floor_optout_list_matches_config_everywhere(self):
        """opt-out 名单只有一份真源（`config/models.yaml`），文档不许说少了。

        一档画风加进 opt-out 而文档没跟上时，点名清单就成了正在生效的假话——
        包括最权威的 `config/README.md`。判据从 yaml 现算，
        名单增删自动跟；行内点了名却点不全的才算漂移。"""
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        try:
            import yaml
        except ImportError:
            self.skipTest("无 PyYAML")
        cfg = yaml.safe_load((root / "config" / "models.yaml").read_text(encoding="utf-8"))
        off = {k for k, v in (cfg.get("profiles") or {}).items()
               if ((v or {}).get("image") or {}).get("image_text_floor") is False}
        self.assertTrue(off)
        docs = ["config/README.md", "engine/kinema/pipeline/prompts.py", "engine/kinema/cli.py",
                ".claude/skills/kinema/SKILL.md",
                ".claude/skills/kinema/references/storyboard.md",
                ".claude/skills/kinema/references/prompt-templates.md"]
        bad = []
        for rel in docs:
            txt = (root / rel).read_text(encoding="utf-8")
            for line in txt.splitlines():
                if "image_text_floor" not in line and "地板" not in line and "text_floor" not in line:
                    continue
                named = {p for p in off if p in line}
                if named and named != off and re.search(r"game_sim|explainer", line):
                    bad.append(f"{rel}: 只点了 {sorted(named)}，实为 {sorted(off)}")
        self.assertEqual(bad, [], "防字地板 opt-out 名单在文档里说少了：" + "；".join(bad))

    def test_anti_text_floor_optout(self):
        # HUD/信息图画风（profile 写 image_text_floor: false）：地板整块不注入，
        # 但作者自己的 negative 仍照常编译
        s = {"image_prompt": "血条与小地图", "negative_prompt": "多余的手指"}
        p = prompts.image_prompt(s, text_floor=False)
        self.assertNotIn("字幕、画面文字、水印", p)
        self.assertIn("。避免出现：多余的手指", p)
        self.assertNotIn("避免出现", prompts.image_prompt({"image_prompt": "血条"},
                                                          text_floor=False))

    def test_anti_text_floor_optout_wired_from_profile(self):
        # opt-out 的唯一来源是 profile 的 image.image_text_floor（router 透传进 params）——
        # 生图段必须真的把它传下去，否则 models.yaml 里写了也是死字段
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_image)
        self.assertIn("image_text_floor", src)
        self.assertIn("text_floor=", src)

    def test_retake_note_compiled(self):
        s = {"image_prompt": "回眸",
             "review": {"image": {"state": "retake", "note": "构图偏左"}}}
        self.assertIn("本次修正重点（务必执行）：构图偏左", prompts.image_prompt(s))

    def test_no_retake_note_when_done(self):
        s = {"image_prompt": "回眸",
             "review": {"image": {"state": "done", "note": "构图偏左"}}}
        self.assertNotIn("修正重点", prompts.image_prompt(s))


class TestImageStoryContracts(unittest.TestCase):
    """图侧剧情画面三件套：单帧剧情契约 / 设定图参考契约 / 防设定表地板。

    典型失败：全员数十人的千字外貌块 + 宫格版式设定图作参考，
    数十字的正文被压死——大片镜头被画成「人物设定总表」而非剧情画面。"""

    def test_story_frame_contract_between_prefix_and_cast(self):
        # 单帧剧情契约句缺省注入，排位在风格前缀之后、角色锚之前——
        # 先定性「这是一格戏」，角色锚才不会被读成图鉴条目
        s = {"image_prompt": "回眸"}
        p = prompts.image_prompt(s, style_prefix="水墨画风", character_block="洛——白衣少年")
        self.assertIn(prompts.STORY_FRAME_ZH, p)
        self.assertLess(p.index("水墨画风"), p.index(prompts.STORY_FRAME_ZH))
        self.assertLess(p.index(prompts.STORY_FRAME_ZH), p.index("洛——白衣少年"))

    def test_story_frame_contract_gated_by_text_floor(self):
        # HUD/信息图画风（image_text_floor: false）的帧不是剧情画面——契约句同门关闭
        p = prompts.image_prompt({"image_prompt": "血条与小地图"}, text_floor=False)
        self.assertNotIn(prompts.STORY_FRAME_ZH, p)
        self.assertNotIn("剧情画面", p)

    def test_story_frame_contract_en(self):
        p = prompts.image_prompt({"image_prompt_en": "a girl turning back"},
                                 prompt_lang="en")
        self.assertIn(prompts.STORY_FRAME_EN, p)
        self.assertNotIn(prompts.STORY_FRAME_ZH, p)   # 英文体不混中文契约

    def test_ref_base_contract_only_when_refs_attached(self):
        # 设定图参考契约句随 ref_base 实况：没附设定图的请求绝不空谈「所附设定图」
        s = {"image_prompt": "回眸"}
        self.assertNotIn("设定图", prompts.image_prompt(s))
        p = prompts.image_prompt(s, ref_base=True)
        self.assertIn(prompts.REF_BASE_ZH, p)
        self.assertIn("不要画入", p)          # 「未出场者不画」是契约句的必备义项
        self.assertIn("绝不照搬设定图的排版版式", p)
        pe = prompts.image_prompt({"image_prompt_en": "x"}, prompt_lang="en",
                                  ref_base=True)
        self.assertIn(prompts.REF_BASE_EN, pe)

    def test_ref_base_forbids_duplicating_a_character(self):
        # 设定图上的三视图会被实例化成「两个相同的人」（一镜里主角抱着两个同一角色、
        # 另一镜同一角色面对面站着）——契约句必须点破「多视图=同一个对象」
        for lang, needle in (("zh", "至多出现一次"), ("en", "at most once")):
            p = prompts.image_prompt({"image_prompt": "x", "image_prompt_en": "x"},
                                     prompt_lang=lang, ref_base=True)
            self.assertIn(needle, p, f"{lang} 契约句缺「单次出现」条款")
        self.assertIn("同一个对象", prompts.REF_BASE_ZH)

    def test_cast_empty_swaps_story_frame_variant(self):
        # 空出场表/弃锚的镜换无人变体：完整版「人物有具体动作与情绪神态」会诱导
        # 模型往空镜里画人（如纯天空镜被塞进古装女主）
        s = {"image_prompt": "仰拍夜空环形符文"}
        p = prompts.image_prompt(s, cast_empty=True)
        self.assertIn(prompts.STORY_FRAME_NOCAST_ZH, p)
        self.assertNotIn("情绪神态", p)
        self.assertIn("不添加正文之外的人物", p)
        # 有出场角色仍用完整版（表演指令是剧情感的一半）
        self.assertIn(prompts.STORY_FRAME_ZH, prompts.image_prompt(s))
        # 画风门一致：text_floor=False 时两个变体都不注入
        self.assertNotIn("单帧剧情画面",
                         prompts.image_prompt(s, cast_empty=True, text_floor=False))
        pe = prompts.image_prompt({"image_prompt_en": "empty sky"}, prompt_lang="en",
                                  cast_empty=True)
        self.assertIn(prompts.STORY_FRAME_NOCAST_EN, pe)

    def test_sheet_floor_after_text_floor_author_first(self):
        # 负面串三段次序硬约束：作者原话 → 防字地板 → 防设定表地板
        p = prompts.image_prompt({"image_prompt": "回眸", "negative_prompt": "多余的手指"})
        self.assertIn("角色设定表", p)
        self.assertLess(p.index("多余的手指"), p.index("字幕、画面文字、水印"))
        self.assertLess(p.index("字幕、画面文字、水印"), p.index("角色设定表"))

    def test_sheet_floor_gated_and_deduped(self):
        # text_floor=False 一并关掉防设定表地板；作者自写「设定表」不重复注入
        self.assertNotIn("角色设定表",
                         prompts.image_prompt({"image_prompt": "血条"}, text_floor=False))
        p = prompts.image_prompt({"image_prompt": "回眸", "negative_prompt": "设定表版式"})
        self.assertEqual(p.count("设定表"), 1)

    def test_sheet_floor_en(self):
        p = prompts.image_prompt({"image_prompt_en": "a duel at dawn"}, prompt_lang="en")
        self.assertIn("character design sheet", p)
        self.assertNotIn("角色设定表", p)      # 英文体不混中文地板


class TestCharacterAnchorBlock(unittest.TestCase):
    """角色文字锚按镜装配：设定图在场只留绑定句、无设定图才落全文外貌、
    全员兜底块按预算裁决——「整块灌全员图鉴」绝不许出现。"""

    CAST = [{"name": "陆昭", "appearance": "24岁男性，清瘦挺拔，黑发短碎"},
            {"name": "姜栀", "appearance": "19岁少女，圆眼大瞳，小虎牙"}]

    def test_sheeted_gets_binding_line_not_appearance(self):
        # 设定图随请求附上的角色：一行绑定句，外貌文本一个字不复述
        # （复述即漂移——文字与像素不一致处全是漂移指令）
        block, narrowed = prompts.character_anchor_block(self.CAST, sheeted={"陆昭"})
        self.assertIn("陆昭（外观、体态与设定中登记的特征以其角色设定图为准）", block)
        self.assertNotIn("清瘦挺拔", block)
        self.assertIn("姜栀——19岁少女", block)   # 无设定图的出场角色仍靠全文外貌锚
        self.assertFalse(narrowed)

    def test_fallback_all_over_budget_drops_anchor(self):
        # 未点名且零命中的镜：小阵容整块保留（策略③一致性锚），
        # 超预算整卡弃锚并报告收窄——宁可不锚也不把几十人图鉴灌进一个空镜
        big = [{"name": f"角色{i:02d}", "appearance": "外" * 60} for i in range(33)]
        block, narrowed = prompts.character_anchor_block(big, fallback_all=True)
        self.assertEqual(block, "")
        self.assertTrue(narrowed)
        small, ok = prompts.character_anchor_block(self.CAST, fallback_all=True)
        self.assertIn("陆昭", small)
        self.assertIn("姜栀", small)
        self.assertFalse(ok)

    def test_explicit_cast_never_budget_dropped(self):
        # 显式点名的出场角色绝不因预算被裁——预算只裁「引擎猜的」全员兜底块
        big = [{"name": f"角色{i:02d}", "appearance": "外" * 60} for i in range(33)]
        block, narrowed = prompts.character_anchor_block(big, fallback_all=False)
        self.assertIn("角色00", block)
        self.assertFalse(narrowed)

    def test_nameless_or_bald_entries_skipped(self):
        block, _ = prompts.character_anchor_block(
            [{"name": "", "appearance": "无名"}, {"name": "光杆"}, self.CAST[0]])
        self.assertNotIn("无名", block)
        self.assertNotIn("光杆", block)
        self.assertIn("陆昭", block)


class TestShotCast(unittest.TestCase):
    """Project.shot_cast 三级解析：显式白名单（含空表）> 文本命中 > 全员兜底。"""

    def _proj(self, shot, characters):
        return Project(fake_path("project.json"),
                       {"id": "t", "shots": [shot], "characters": characters})

    CHARS = [{"name": "陆昭", "appearance": "清瘦青年"},
             {"name": "姜栀", "appearance": "圆眼少女", "keywords": ["舞者"]},
             {"name": "秦崖", "appearance": "壮汉"}]

    def test_explicit_list_is_strict_whitelist(self):
        shot = {"id": 1, "characters": ["陆昭"], "image_prompt": "姜栀在旁看着"}
        cast, fb = self._proj(shot, self.CHARS).shot_cast(shot)
        self.assertEqual([c["name"] for c in cast], ["陆昭"])   # 文本提到姜栀也不追加
        self.assertFalse(fb)

    def test_empty_list_means_no_cast_not_fallback(self):
        # 空表=明确无人出场（与 design_refs 同语义）——绝不回落全员
        cast, fb = self._proj({}, self.CHARS).shot_cast(
            {"id": 1, "characters": [], "image_prompt": "仰拍夜空环形符文"})
        self.assertEqual(cast, [])
        self.assertFalse(fb)

    def test_text_hit_by_name_and_keywords(self):
        p = self._proj({}, self.CHARS)
        cast, fb = p.shot_cast({"id": 1, "image_prompt": "陆昭俯身喷漆"})
        self.assertEqual([c["name"] for c in cast], ["陆昭"])
        self.assertFalse(fb)
        cast2, _ = p.shot_cast({"id": 2, "narration": "台上的舞者转身"})
        self.assertEqual([c["name"] for c in cast2], ["姜栀"])   # keywords 命中

    def test_unspecified_zero_hit_falls_back_to_all(self):
        cast, fb = self._proj({}, self.CHARS).shot_cast(
            {"id": 1, "image_prompt": "空荡的城市天桥"})
        self.assertEqual(len(cast), 3)
        self.assertTrue(fb)     # 兜底与否交 character_anchor_block 按预算裁决


class TestImageCastWiring(unittest.TestCase):
    """cli 生图段接线守卫（源级）：整块灌 style.character_block 的写法不许出现。"""

    def test_stage_gen_image_assembles_per_shot_anchors(self):
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_image)
        self.assertIn("shot_cast", src)                  # 按镜三级解析出场角色
        self.assertIn("character_anchor_block", src)     # 锚块经预算/绑定句装配
        self.assertIn("ref_base=", src)                  # 设定图契约句按实况传入
        self.assertIn("cast_empty=", src)                # 空镜换无人剧情契约变体
        self.assertNotIn('style.get("character_block")', src)   # 旧全员块死路


class TestVideoPrompt(unittest.TestCase):
    def test_camera_floor_and_anti_subtitle_floor(self):
        s = {"video_prompt": "老者转身", "camera": "缓慢推近",
             "speaker": "老者", "narration": "一句台词"}
        p = prompts.video_prompt(s, native=False)
        self.assertIn("运镜：缓慢推近", p)
        self.assertIn("避免出现：", p)                                  # 负面串成句
        self.assertTrue(p.rstrip().endswith("字幕、画面文字、水印"))    # 防字地板恒是尾词
        self.assertIn("口型与音频严格同步", p)              # dubbed 对白尾缀

    def test_dubbed_silent_shot_gets_closed_lips_not_lipsync(self):
        """纯画面镜的参考音是等长静音（cli 静音占位）：对口型指令会让模型
        对着静音找口型，闭唇句才是对该素材的正确执行。"""
        s = {"video_prompt": "老者转身", "camera": "缓慢推近"}
        p = prompts.video_prompt(s, native=False)
        self.assertIn("口唇保持闭合", p)
        self.assertNotIn("口型与音频严格同步", p)


class TestDubbedVoiceClause(unittest.TestCase):
    """dubbed 音频处置句的语态矩阵（与 native_voice_clause 同一套判据）。

    恒发「角色对口型」的单句写法有两处失效形态：旁白镜里画面角色把第三人称
    叙述念了出来；多人镜模型自选一张脸开口。语态与具名绑定缺一不可。"""

    def test_voiceover_shot_never_lipsyncs(self):
        c = prompts.dubbed_voice_clause({"narration": "旁白在讲述这段历史。"})
        self.assertIn("@配音1 是画外旁白", c)
        self.assertIn("口唇保持闭合", c)
        self.assertIn("绝不与音频对口型", c)
        self.assertNotIn("对口型说出", c)

    def test_dialogue_names_the_speaker_with_verbatim_line(self):
        c = prompts.dubbed_voice_clause({"speaker": "岚瑾", "narration": "台词。"})
        self.assertIn("岚瑾 @配音1 说：“台词。”", c,
                      "台词逐字入文——模型据此预知音节数与断句，人工审阅逐句可核")
        self.assertIn("由岚瑾对口型说出@配音1 的内容", c)
        self.assertIn("其余人物口唇保持闭合", c)
        self.assertIn("身体动作照常按运动设计进行", c,
                      "不声明并行，模型会站定说完、把走位设计整包顶掉")

    def test_multi_speaker_binds_in_order(self):
        c = prompts.dubbed_voice_clause({"lines": [
            {"speaker": "岚瑾", "text": "走。"},
            {"speaker": "老岸", "text": "小心。"}]})
        self.assertIn("@配音1 按序", c)
        self.assertIn("岚瑾说：“走。”", c)
        self.assertIn("老岸说：“小心。”", c)
        self.assertIn("各自只在自己那句对口型", c)

    def test_mixed_narration_line_gets_no_lipsync(self):
        c = prompts.dubbed_voice_clause({"lines": [
            {"speaker": "岚瑾", "text": "台词。"},
            {"speaker": "旁白", "text": "她转身离开。"}]})
        self.assertIn("画外旁白“她转身离开。”（无人对口型）", c)
        self.assertIn("岚瑾说：“台词。”", c)

    def test_silent_shot_states_no_voice(self):
        c = prompts.dubbed_voice_clause({})
        self.assertIn("本镜无人声", c)
        self.assertIn("口唇保持闭合", c)

    def test_floor_not_duplicated_when_user_wrote_subtitle_negative(self):
        s = {"video_prompt": "转身", "negative_prompt": "字幕"}
        p = prompts.video_prompt(s, native=False)
        self.assertEqual(p.count("字幕"), 1)

    def test_sfx_only_in_native(self):
        s = {"video_prompt": "落雨", "sfx": "雨声淅沥"}
        self.assertIn("环境音效：雨声淅沥", prompts.video_prompt(s, native=True))
        self.assertNotIn("环境音效", prompts.video_prompt(s, native=False))

    def test_native_speaker_line(self):
        s = {"video_prompt": "抬头", "speaker": "师父", "narration": "痴儿，回头。"}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("师父说：", p)
        self.assertIn("口型与台词同步", p)

    def test_negative_clause_is_always_the_tail(self):
        """负面串必须是最后一句：它先拼、人声句后拼的话，`_zh_join` 会用「，」
        把台词接到负面枚举尾巴上——「避免出现：…水印，师父说：“痴儿，回头。”，
        口型与台词同步」，要念的台词与对口型指令一起落进「避免出现」里，
        语义整个反过来，而这一步按秒计费。native 与 dubbed 两条尾缀同此。"""
        s = {"video_prompt": "抬头", "negative_prompt": "夸张表情",
             "speaker": "师父", "narration": "痴儿，回头。"}
        for native, tail in ((True, "口型与台词同步"), (False, "口型与音频严格同步")):
            p = prompts.video_prompt(s, native=native)
            self.assertLess(p.index(tail), p.index("避免出现："),
                            f"native={native}：人声句被拼到负面串之后")
            self.assertTrue(p.rstrip().endswith("水印"), f"native={native}：负面串不是尾句")

    def test_positive_plus_negative_is_byte_identical_to_what_is_sent(self):
        """页面「实发提示词」把正文与 AVOID 分列渲染，实发是单串 `prompt`——
        两者必须只差一句尾缀，否则页面展示的顺序与真发不是同一条稿。"""
        s = {"video_prompt": "抬头", "negative_prompt": "夸张表情",
             "speaker": "师父", "narration": "痴儿，回头。"}
        pos = prompts.video_prompt(s, native=True, include_negative=False)
        full = prompts.video_prompt(s, native=True, include_negative=True)
        self.assertTrue(full.startswith(pos))
        self.assertTrue(full[len(pos):].lstrip("。，").startswith("避免出现："))


class TestPunctuationSeam(unittest.TestCase):
    """「。，」标点缝：作者正文以句号收尾时被 `"，".join` 接上。

    去缝必须集中在拼接位：`with_text_floor("")` 恒返回非空地板，负面串那一拼是
    无条件执行的——只在 sfx 一处去缝，没写 sfx 的镜照样产出「。，避免出现：…」。"""

    def test_no_seam_on_the_sfx_path(self):
        s = {"video_prompt": "终态停在闪光刚熄的一刻。", "sfx": "雨声，远处车流"}
        p = prompts.video_prompt(s, native=True)
        self.assertNotIn("。，", p)
        self.assertIn("的一刻。", p)          # 句号原样保留，不被补成「。，」
        self.assertIn("环境音效：雨声，远处车流", p)

    def test_no_seam_on_the_negative_path_without_any_sfx(self):
        """没写 sfx 时缝出在防字地板那一拼上——只在 sfx 一处去缝防不住这条路径。"""
        p = prompts.video_prompt({"video_prompt": "他缓缓抬头。"}, native=True)
        self.assertNotIn("。，", p)
        self.assertIn("他缓缓抬头。", p)
        self.assertIn("避免出现：", p)

    def test_seam_free_on_every_zh_join_site(self):
        """四个拼接位一起验：角色锚 / sfx / 负面串 / 台词句。"""
        s = {"video_prompt": "他缓缓抬头。", "sfx": "风声。", "negative_prompt": "畸形",
             "speaker": "陆昭", "narration": "我进去。"}
        for kw in ({"native": True}, {"native": False}):
            p = prompts.video_prompt(s, cast_anchor="本镜出场角色：陆昭。", **kw)
            for bad in ("。，", "！，", "？，", "…，"):
                self.assertNotIn(bad, p, f"{kw} 出现标点缝 {bad}")

    def test_comma_still_added_when_the_body_does_not_end_a_sentence(self):
        """别把逗号删过头——没有句末标点时仍须补「，」，否则两句黏成一句。"""
        p = prompts.video_prompt({"video_prompt": "他缓缓抬头", "sfx": "风声"}, native=True)
        self.assertIn("他缓缓抬头，", p, "没有句末标点时必须补「，」，否则两句黏成一句")
        self.assertIn("环境音效：风声", p)


class TestNativeVoiceClause(unittest.TestCase):
    """native 镜尾部的人声句：对白配口型，旁白闭唇。

    `f'{spk or "角色"}说：“{narr}”…'` 这种写法有两处错：`speaker` 空时编出泛称
    「角色」（正是本仓 `generic_name` 维度判违规的写法），且把第三人称叙述当台词
    要求模型配口型——旁白驱动的章会整章中招。

    分类**必须走 `voicecast.voice_kind`**（全仓单一真源，lint 的语态维度用同一个）：
    两处各写一份判据就会出现「lint 说这是旁白、提示词却让角色对口型」。"""

    def test_voiceover_shot_carries_both_the_text_and_the_closed_lips_clause(self):
        """旁白镜的两半都必须在：念什么 + 不要对口型。

        只发闭唇句那一半时，模型收到「有人在画外讲述」却收不到讲述内容，
        只能自行编造旁白；`native_voiceover` 缺省不烧固定音色，那条自编人声
        就是成片主音轨，与按 `narration` 烧录的字幕不同源。"""
        s = {"video_prompt": "推近", "narration": "渊启日前十分钟，陆昭正在喷漆。"}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("画外旁白讲述：“渊启日前十分钟，陆昭正在喷漆。”", p)
        self.assertIn(prompts.NARRATION_LIPS_ZH, p)
        self.assertNotIn("口型与台词同步", p)
        self.assertNotIn("角色说：", p, "泛称「角色」不许再出现")

    def test_named_narrator_is_still_a_voiceover(self):
        """`speaker` 写「旁白」也是旁白——判据认的是 voicecast 的旁白名单。"""
        s = {"video_prompt": "推近", "speaker": "旁白", "narration": "十年后。"}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("画外旁白讲述：“十年后。”", p)
        self.assertIn(prompts.NARRATION_LIPS_ZH, p)

    def test_dialogue_shot_keeps_lip_sync(self):
        s = {"video_prompt": "抬头", "speaker": "陆昭", "narration": "我进去，会怎么样。"}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("陆昭说：“我进去，会怎么样。”", p)
        self.assertIn("口型与台词同步", p)
        self.assertNotIn(prompts.NARRATION_LIPS_ZH, p)

    def test_emotion_swaps_the_verb_only_on_a_table_hit(self):
        """确定性查表、缺省「说」——引擎不造措辞，只把作者填好的 emotion 翻成动词。"""
        base = {"video_prompt": "抬头", "speaker": "陆昭", "narration": "别过来。"}
        self.assertIn("陆昭发着颤说：", prompts.video_prompt({**base, "emotion": "fear"},
                                                        native=True))
        self.assertIn("陆昭嘶声怒喝道：", prompts.video_prompt({**base, "emotion": "angry"},
                                                         native=True))
        # 大小写归一
        self.assertIn("陆昭哽着声音说：", prompts.video_prompt({**base, "emotion": "SAD"},
                                                         native=True))
        # 中文存量写法也认（实盘镜级 emotion 英文 104 处 / 中文 27 处）
        self.assertIn("陆昭失声道：", prompts.video_prompt({**base, "emotion": "震惊"},
                                                       native=True))
        # 未命中一律回落「说」，不猜不造词
        self.assertIn("陆昭说：", prompts.video_prompt({**base, "emotion": "若有所思"},
                                                    native=True))
        self.assertIn("陆昭说：", prompts.video_prompt(base, native=True))

    def test_table_keys_cover_the_schema_vocabulary(self):
        """表的键必须对得上数据契约声明的情绪档，否则整张表在实盘上空转
        （初版全用中文键，而 schema 与实盘主要是英文——命中率近乎为零）。"""
        for k in ("happy", "sad", "angry", "surprised", "fear", "excited", "coldness"):
            self.assertIn(k, prompts.DIALOGUE_VERB_ZH, f"schema 声明的情绪档 {k} 没进表")

    def test_voice_instruction_rides_along_the_line(self):
        """native 对白不经 TTS，句级语气指令只能随人声句发给模型：动词后括注。"""
        s = {"video_prompt": "抬头", "lines": [
            {"speaker": "阿川", "text": "才九个褶。", "emotion": "neutral",
             "voice_instruction": "泄气的自嘲，句尾拖一下"},
            {"speaker": "奶奶", "text": "不数褶。", "emotion": "gentle"}]}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("阿川说（泄气的自嘲，句尾拖一下）：“才九个褶。”", p)
        self.assertIn("奶奶放软了声音说：“不数褶。”", p)
        en = prompts.native_voice_clause(s, lang="en")
        self.assertIn("阿川 says (泄气的自嘲，句尾拖一下): “才九个褶。”", en)
        self.assertIn("奶奶 says: “不数褶。”", en)

    def test_voice_instruction_on_narration_line(self):
        """非闭声的旁白句同样带括注；闭声（mute）旁白镜不出人声句，指令随之不发。"""
        s = {"video_prompt": "推近", "narration": "十年后。", "voice_instruction": "语速偏慢"}
        self.assertIn("画外旁白讲述（语速偏慢）：“十年后。”", prompts.native_voice_clause(s))
        self.assertNotIn("语速偏慢", prompts.native_voice_clause(s, mute=True))

    def test_silent_shot_gets_the_no_speech_floor(self):
        """native 无台词镜必须带无人声地板：不带时提示词里没有任何东西拦着
        模型自配人声，是否出人声全凭运气。"""
        p = prompts.video_prompt({"video_prompt": "推近"}, native=True)
        self.assertIn("本镜无台词", p)
        self.assertIn(prompts.NARRATION_LIPS_ZH, p)
        self.assertIn("不加旁白或念白", p)


class TestBeatSoundReachesTheModel(unittest.TestCase):
    """逐拍 `sound` 进提示词后，镜级 `sfx` 必须让位——同一套声音设计不发两遍。"""

    S = {"id": 1, "dur": 4.0, "video_prompt": "翻跃", "sfx": "雨声，液压嘶声",
         "sketch": {"beats": [{"action": "起跳", "sound": "蹬踏"},
                              {"action": "落地", "sound": "落地闷响"}]}}

    def test_per_beat_sound_replaces_the_shot_level_summary(self):
        p = prompts.video_prompt(self.S, native=True, sketch=True, sketch_total=4.0)
        self.assertIn("声：蹬踏", p)
        self.assertIn("声：落地闷响", p)
        self.assertNotIn("环境音效：", p, "逐拍版在场，汇总版必须让位，否则同一批句子发两遍")

    def test_shot_level_sfx_survives_when_no_beat_wrote_sound(self):
        s = {**self.S, "sketch": {"beats": [{"action": "起跳"}, {"action": "落地"}]}}
        p = prompts.video_prompt(s, native=True, sketch=True, sketch_total=4.0)
        self.assertIn("环境音效：雨声，液压嘶声", p)
        self.assertNotIn("声：", p)

    def test_dubbed_gets_neither(self):
        p = prompts.video_prompt(self.S, native=False, sketch=True, sketch_total=4.0)
        self.assertNotIn("声：", p)
        self.assertNotIn("环境音效", p)

    def test_en_floor(self):
        s = {"video_prompt_en": "slow dolly in"}
        p = prompts.video_prompt(s, native=False, lang="en")
        self.assertIn("subtitles, captions, on-screen text, watermark", p)
        self.assertNotIn("运镜：", p)

    def test_clip_retake_note_compiled(self):
        s = {"video_prompt": "转身",
             "review": {"clip": {"state": "retake", "note": "动作太快"}}}
        self.assertIn("本次修正重点（务必执行）：动作太快",
                      prompts.video_prompt(s, native=False))


class TestVideoPromptIncremental(unittest.TestCase):
    """增量编译铁律：视频请求恒带该镜分镜图，画面基底已给定，
    提示词只写增量——**绝不整条复述 image_prompt**（那是跨镜漂移的头号来源）。"""

    IP = "白发老者立于崖边，青衫猎猎，远山云海翻涌，逆光勾出发丝边缘"

    def test_no_fallback_to_image_prompt(self):
        """缺 video_prompt 时不回退 image_prompt，落 delta 兜底句。"""
        p = prompts.video_prompt({"image_prompt": self.IP, "image_prompt_en": "an old man"},
                                 native=True)
        self.assertNotIn("白发老者立于崖边", p)
        self.assertNotIn("云海翻涌", p)
        self.assertIn(prompts.DELTA_FALLBACK_ZH, p)
        p_en = prompts.video_prompt({"image_prompt_en": "an old man on a cliff"},
                                    native=True, lang="en")
        self.assertNotIn("an old man on a cliff", p_en)
        self.assertIn(prompts.DELTA_FALLBACK_EN, p_en)

    def test_delta_skeleton_injected(self):
        s = {"action": "抬手抹掉眼角", "end_state": "手停在半空", "light_shift": "暖黄渐转冷蓝"}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("动作：抬手抹掉眼角", p)
        self.assertIn("终态：手停在半空", p)
        self.assertIn("光线变化：暖黄渐转冷蓝", p)
        self.assertNotIn(prompts.DELTA_FALLBACK_ZH, p)      # 有增量就不落兜底句

    def test_delta_skeleton_injected_in_english_too(self):
        """en provider（veo/nano-banana 是 status=ready 的现役备选）下 delta 三字段
        **必须照发**——它们没有 `_en` 对位，按本模块「缺失互为回退」的双语选材口径取
        同一批值、只换英文标签。丢弃它们不是"省略"而是"顶替"：正文会落到
        DELTA_FALLBACK_EN 那句「只做轻微呼吸」上，等于把作者写的整套运动设计反着
        发出去还照价计费。"""
        s = {"action": "从伏案缓缓直起上身", "end_state": "手停在半空",
             "light_shift": "暖黄转冷蓝"}
        p = prompts.video_prompt(s, native=True, lang="en")
        self.assertIn("Action: 从伏案缓缓直起上身", p)
        self.assertIn("Ends on: 手停在半空", p)          # 首尾帧运动收束点，最不能丢
        self.assertIn("Light shift: 暖黄转冷蓝", p)
        self.assertNotIn(prompts.DELTA_FALLBACK_EN, p)   # 有增量就绝不落反向兜底句
        self.assertNotIn("动作：", p)                     # en 用英文标签，不混中文标签
        # 作者另写了 video_prompt_en 时，骨架与正文共存、正文不被顶替
        pb = prompts.video_prompt(dict(s, video_prompt_en="he rises from the desk"),
                                  native=True, lang="en")
        self.assertIn("he rises from the desk", pb)
        self.assertIn("Ends on: 手停在半空", pb)
        self.assertNotIn(prompts.DELTA_FALLBACK_EN, pb)

    def test_delta_missing_flag_matches_actual_fallback(self):
        """`video_delta_missing` 是 cli「已落兜底句」提示的判据——必须与真正落
        DELTA_FALLBACK_* 的条件逐字一致，否则日志与实发提示词相反。"""
        self.assertTrue(prompts.video_delta_missing({"image_prompt": self.IP}))
        for f, _zh, _en in prompts.DELTA_FIELDS:          # 任一 delta 字段有值即不算空
            self.assertFalse(prompts.video_delta_missing({f: "抬手"}), f)
        self.assertFalse(prompts.video_delta_missing({"video_prompt_en": "he rises"}))

    def test_delta_not_duplicated_when_already_in_video_prompt(self):
        s = {"video_prompt": "他抬手抹掉眼角，肩膀垮下去", "action": "抬手抹掉眼角",
             "end_state": "手停在半空"}
        p = prompts.video_prompt(s, native=True)
        self.assertEqual(p.count("抬手抹掉眼角"), 1)          # 指挥层已写进正文的不重复注入
        self.assertIn("终态：手停在半空", p)                   # 没写进正文的照常注入

    def test_contract_sentence_once_and_role_specific(self):
        """契约句只出一次，且措辞按喂图角色二分——dubbed 走 role=reference_image，
        对它说「以所给首帧为准」是错误措辞（模型收到的根本不是首帧）。"""
        s = {"video_prompt": "老者转身"}
        pn = prompts.video_prompt(s, native=True)
        self.assertEqual(pn.count(prompts.CONTRACT_FIRST_ZH), 1)
        self.assertNotIn("参考图", pn)
        pd = prompts.video_prompt(s, native=False)
        self.assertEqual(pd.count(prompts.CONTRACT_REF_ZH), 1)
        self.assertNotIn("首帧", pd)
        self.assertTrue(pd.startswith(prompts.CONTRACT_REF_ZH))   # 前置
        pe = prompts.video_prompt(s, native=True, lang="en")
        self.assertEqual(pe.count(prompts.CONTRACT_FIRST_EN), 1)

    def test_single_camera_clause_when_video_prompt_missing(self):
        """缺 video_prompt 时 vmotion 可能为空，`cam not in vmotion` 起不到去重
        作用——此时仍须只出一条运镜句。"""
        p = prompts.video_prompt({"camera": "缓慢推近", "image_prompt": self.IP}, native=True)
        self.assertEqual(p.count("运镜："), 1)
        self.assertEqual(p.count("缓慢推近"), 1)

    def test_camera_still_leads_the_creative_body(self):
        """契约句是画面基准声明；创作内容里 camera 仍在首位（前位 token 权重最高）。"""
        p = prompts.video_prompt({"camera": "缓慢推近", "action": "抬手"}, native=True)
        self.assertLess(p.index("运镜：缓慢推近"), p.index("动作：抬手"))


class TestContractLocksIdentityNotComposition(unittest.TestCase):
    """契约句锁的是**身份**，不是构图。

    首句权重最高：说「构图保持一致」会与紧随其后的 `运镜：拉远/环绕/升镜揭示`
    直接冲突（那些运镜的定义就是改变构图），模型二选一、通常服从首句 → 运镜幅度被
    压扁成"几乎没动"。典型冲突：「拉远：镜头缓缓平稳后拉并持续上升…
    主体在画面底部逐渐缩成一个小点」的运镜句，配上一句说构图不许变的首句。

    **本类是反向断言**：
    四支契约句 + 两档兜底句一处都不得出现锁构图措辞——
    只管契约句而漏掉 `DELTA_FALLBACK` 等于把矛盾从首句搬到正文，一个字都没消灭。"""

    OPENED = ("prompts.CONTRACT_FIRST_ZH", "prompts.CONTRACT_REF_ZH",
              "prompts.CONTRACT_FIRST_EN", "prompts.CONTRACT_REF_EN",
              "prompts.DELTA_FALLBACK_ZH", "prompts.DELTA_FALLBACK_EN")

    def test_no_constant_still_locks_composition(self):
        for name in self.OPENED:
            s = getattr(prompts, name.split(".")[1])
            self.assertNotIn("构图保持一致", s, f"{name} 又把构图锁回去了")
            self.assertNotIn("场景与构图保持一致", s, name)
            self.assertNotIn("构图不变", s, name)
            self.assertNotIn("composition unchanged", s, name)

    def test_composition_is_explicitly_handed_to_the_camera_move(self):
        """不许只是"删掉锁构图"就完事——必须显式说清构图归谁管，
        未指定处模型会按训练分布自行补全。"""
        for name in ("CONTRACT_FIRST_ZH", "CONTRACT_REF_ZH", "DELTA_FALLBACK_ZH"):
            self.assertIn("构图与机位按本镜运镜自然变化", getattr(prompts, name), name)
        for name in ("CONTRACT_FIRST_EN", "CONTRACT_REF_EN", "DELTA_FALLBACK_EN"):
            self.assertIn("camera framing", getattr(prompts, name), name)

    def test_identity_is_what_stays_locked(self):
        """放开构图不等于放开身份——漂移的真正来源是人/衣/景/画风。"""
        for name in ("CONTRACT_FIRST_ZH", "CONTRACT_REF_ZH"):
            v = getattr(prompts, name)
            for kept in ("同一个主体", "同一套登记外观与穿戴", "同一个场景", "同一画风"):
                self.assertIn(kept, v, name)
        self.assertIn("主体、登记穿戴/配件与场景保持不变", prompts.DELTA_FALLBACK_ZH)

    def test_role_specific_wording_survives_the_rewrite(self):
        """改措辞不许顺手改坏喂图角色的二分（dubbed 收到的根本不是首帧），
        也不许改掉 `test_sketchboard` 依赖的「以所给首帧为画面基准」字面前缀。"""
        self.assertTrue(prompts.CONTRACT_FIRST_ZH.startswith("以所给首帧为画面基准"))
        self.assertTrue(prompts.CONTRACT_REF_ZH.startswith("以所给参考图为画面基准"))
        self.assertNotIn("参考图", prompts.CONTRACT_FIRST_ZH)
        self.assertNotIn("首帧", prompts.CONTRACT_REF_ZH)

    def test_end_to_end_a_pull_out_shot_no_longer_contradicts_itself(self):
        """端到端：拉远镜的实发串里不得出现锁构图首句。"""
        s = {"camera": "拉远：镜头缓缓平稳后拉并持续上升，主体在画面底部逐渐缩成一个小点",
             "video_prompt": "六层培养舱自下而上层层入画"}
        for kw in ({"native": True}, {"native": False}):
            p = prompts.video_prompt(s, **kw)
            self.assertNotIn("构图保持一致", p)
            self.assertIn("构图与机位按本镜运镜自然变化", p)


class TestSheetBindingClause(unittest.TestCase):
    """设定图逐张 @图片N 职责绑定（官方引用语法）：编号=content[] 附图顺序、
    frame/board 占位只占号不产句、没有设定图恒为空串——只附不点名，模型对
    多图的职责分配靠猜（场景图被当角色图用就是从编号错位开始的）。"""

    M = [("frame", ""), ("board", ""), ("character", "林深"),
         ("scene", "废墟"), ("prop", "怀表")]

    def test_numbers_follow_content_order(self):
        c = prompts.sheet_binding_clause(self.M)
        self.assertTrue(c.startswith("；"))
        self.assertIn("@图片3 为角色「林深」的设定图", c)
        self.assertIn("@图片4 为场景「废墟」的设定图", c)
        self.assertIn("@图片5 为道具「怀表」的设定图", c)
        self.assertNotIn("@图片1 为", c, "分镜图归契约句点名，绑定段不重复")
        self.assertNotIn("@图片2 为", c, "板归 board_role_clause，绑定段只占号")
        # 「仍须可辨认」是回指全部设定图的独立收尾，不粘在最后一句尾巴上——最后一句
        # 可能是俯视布局图，那张压根不进画面，谈不上改画时可辨认
        self.assertIn("以上各张设定图所锁定的主体，在运动中改画时仍须可辨认", c)

    def test_empty_without_sheets(self):
        self.assertEqual(prompts.sheet_binding_clause([("frame", ""), ("board", "")]), "")
        self.assertEqual(prompts.sheet_binding_clause(None), "")
        self.assertEqual(prompts.sheet_binding_clause([]), "")

    def test_video_prompt_prefers_manifest_over_generic(self):
        s = {"video_prompt": "他抬头", "camera": "缓慢推近"}
        p = prompts.video_prompt(s, native=True, ref_mode=True, ref_sheets=1,
                                 ref_manifest=[("frame", ""), ("character", "林深")])
        self.assertIn("@图片2 为角色「林深」的设定图", p)
        self.assertNotIn("凡随请求附有对应设定图者", p,
                         "逐张点名在场就不再发泛称句——同一件事说两遍")
        self.assertTrue(p.startswith(prompts.CONTRACT_ALLREF_ZH))

    def test_dubbed_manifest_binding(self):
        s = {"video_prompt": "他抬头"}
        p = prompts.video_prompt(s, native=False, ref_sheets=1,
                                 ref_manifest=[("frame", ""), ("scene", "废墟")])
        self.assertIn("@图片2 为场景「废墟」的设定图", p)

    def test_en_variant(self):
        c = prompts.sheet_binding_clause([("frame", ""), ("character", "Lin")], "en")
        self.assertIn("@Image 2 is the design sheet for character 'Lin'", c)

    def test_global_scene_gets_its_own_wording(self):
        """全局固定场景的 `name` 恒是字面「场景」（`lineage` 的 scene:main）——
        套进具名模板会产出「为场景「场景」的设定图」，一条指向无身份资产的正向
        指令。职责半句仍与具名场景同源，不许另写一份。"""
        c = prompts.sheet_binding_clause([("frame", ""), ("scene_main", "场景")])
        self.assertIn("@图片2 为本片固定场景的设定图", c)
        self.assertNotIn("「场景」", c)
        self.assertIn(prompts._REF_KIND_ZH["scene"][1], c, "职责措辞与具名场景同源")
        en = prompts.sheet_binding_clause([("frame", ""), ("scene_main", "场景")], "en")
        self.assertIn("the film's fixed location", en)

    def test_global_and_named_scene_coexist_with_distinct_wording(self):
        """两者是并列的两档（`test_design_refs` 钉死），绑定句必须能区分开。"""
        c = prompts.sheet_binding_clause(
            [("frame", ""), ("scene_main", "场景"), ("scene", "回声雨星")])
        self.assertIn("@图片2 为本片固定场景的设定图", c)
        self.assertIn("@图片3 为场景「回声雨星」的设定图", c)


class TestAbsentSubjectFloor(unittest.TestCase):
    """视频侧的「未出现者不画」逃逸句。

    设定图缺省全挂，一镜可能带着别处的场景图与不在场的角色图进请求，而逐张职责
    绑定说的全是「以之为准」——那是无条件的正向指令，模型据此把另一个空间的陈设
    并进本镜画面是合规执行。图像侧的 `REF_BASE_ZH` 有同款收尾，视频侧若只说
    「仍须可辨认」，缺的正是这半句边界。"""

    def test_binding_clause_carries_the_floor(self):
        c = prompts.sheet_binding_clause([("frame", ""), ("scene", "废墟")])
        self.assertIn(prompts.ABSENT_FLOOR_ZH, c)
        en = prompts.sheet_binding_clause([("frame", ""), ("scene", "ruins")], "en")
        self.assertIn(prompts.ABSENT_FLOOR_EN, en)

    def test_generic_fallback_carries_it_too(self):
        """回落的泛称句同样在无条件说「以设定图为准」，同样需要这条边界。"""
        self.assertIn(prompts.ABSENT_FLOOR_ZH, prompts.ALLREF_SHEETS_ZH)
        self.assertIn(prompts.ABSENT_FLOOR_EN, prompts.ALLREF_SHEETS_EN)

    def test_tail_only_manifest_stays_clean(self):
        """尾帧不是「要照着画的设定资产」——只挂尾帧时不该出现这句。"""
        c = prompts.sheet_binding_clause([("frame", ""), ("tail", "")])
        self.assertNotIn(prompts.ABSENT_FLOOR_ZH, c)

    def test_video_prompt_ships_it(self):
        p = prompts.video_prompt({"video_prompt": "他抬头"}, native=True,
                                 ref_mode=True,
                                 ref_manifest=[("frame", ""), ("scene", "废墟")])
        self.assertIn("本镜未出现的角色、场景与物件不要画入", p)

    def test_image_side_wording_is_the_precedent_not_a_copy(self):
        """图像侧那条留在 REF_BASE，两处各管各的通道，不共用常量也不互相引用。"""
        self.assertIn("未出现的角色与物件不要画入", prompts.REF_BASE_ZH)
        self.assertNotIn(prompts.ABSENT_FLOOR_ZH, prompts.REF_BASE_ZH)


class TestStructuralLock(unittest.TestCase):
    """结构锁：放开构图的配套地板——机位可以随运镜变，但不许模型自己切一刀。

    只发**一句**且只在真正的单机位连续镜上发；四道门与一道去重的理由写在
    `prompts.py` 的 `STRUCT_LOCK_ZH` 常量注释里，改门控前先读那段。"""

    S = {"video_prompt": "他缓缓抬头", "camera": "缓慢推近"}

    def test_sent_on_a_plain_native_shot(self):
        for lang, lock in (("zh", prompts.STRUCT_LOCK_ZH), ("en", prompts.STRUCT_LOCK_EN)):
            p = prompts.video_prompt(self.S, native=True, lang=lang)
            self.assertEqual(p.count(lock), 1, lang)

    def test_never_on_v2v_but_sent_with_board(self):
        """V2V 的运动权威是参考视频，多一句就是两条并列指令；附板只管拍序，
        分段时间轴对多镜型号本就有切镜压力，结构锁照发。"""
        for lang, lock in (("zh", prompts.STRUCT_LOCK_ZH), ("en", prompts.STRUCT_LOCK_EN)):
            self.assertNotIn(lock, prompts.video_prompt(self.S, native=True, lang=lang,
                                                        ref_video=True), lang)
            self.assertIn(lock, prompts.video_prompt(self.S, native=True, lang=lang,
                                                     sketch_board=True), lang)

    def test_sent_on_reference_mode_without_board(self):
        """全能参考已是 native 缺省档：一镜一片恰恰要求「一段连续拍摄」——
        不发结构锁时模型会把「构图可以变」读成「可以换机位重开一镜」。附板镜同样发。"""
        for lang, lock in (("zh", prompts.STRUCT_LOCK_ZH), ("en", prompts.STRUCT_LOCK_EN)):
            p = prompts.video_prompt(self.S, native=True, lang=lang, ref_mode=True)
            self.assertEqual(p.count(lock), 1, lang)
        p = prompts.video_prompt(self.S, native=True, ref_mode=True, sketch_board=True)
        self.assertEqual(p.count(prompts.STRUCT_LOCK_ZH), 1)

    def test_never_on_an_in_shot_wipe(self):
        """`foreground_wipe` 一族是本仓正在教的「长镜内无痕转场」（chrome2 镜12 实盘）
        ——给它发结构锁就是当场否掉作者刚点名的技法。判据从 CAMERA_PRESETS 派生。"""
        from kinema.pipeline import camera as camera_mod
        wipes = [k for k, v in camera_mod.CAMERA_PRESETS.items() if v.get("wipe")]
        self.assertTrue(wipes, "预设库里一个 wipe 档都没有了——本用例失去意义，请同批重写")
        for key in wipes:
            preset = camera_mod.CAMERA_PRESETS[key]
            s = {"video_prompt": "他缓缓抬头", "camera": preset["phrase"], "camera_preset": key}
            self.assertNotIn(prompts.STRUCT_LOCK_ZH, prompts.video_prompt(s, native=True))

    def test_not_repeated_when_the_author_already_wrote_it(self):
        """chrome/chrome2 两章的作者自己写过同款约束——与 camera/sfx 同制去重。"""
        s = {"camera": "一镜到底：镜头连续移动无跳切，先仰望塔身", "video_prompt": "翻跃上升"}
        self.assertNotIn(prompts.STRUCT_LOCK_ZH, prompts.video_prompt(s, native=True))
        s_en = {"video_prompt_en": "one continuous take, no cuts between the four movements"}
        self.assertNotIn(prompts.STRUCT_LOCK_EN,
                         prompts.video_prompt(s_en, native=True, lang="en"))

    def test_wording_stays_out_of_the_four_forbidden_words(self):
        """「一镜到底」是 camera.py `oner` 的 label（会被读成一条运镜指令）；
        另三个词会打红既有的「native 不提参考图 / 非链上镜不提末帧」断言。"""
        for lock in (prompts.STRUCT_LOCK_ZH, prompts.STRUCT_LOCK_EN):
            for bad in ("一镜到底", "首帧", "末帧", "参考图", "运镜："):
                self.assertNotIn(bad, lock)

    def test_wording_is_positive_not_a_negation_pile(self):
        """否定串放在整条提示词权重最高的位置，正是本仓否掉「负面串前置」的同一条理据。"""
        for bad in ("不切换机位", "不出现剪辑点", "不分屏", "不插入"):
            self.assertNotIn(bad, prompts.STRUCT_LOCK_ZH)
        for bad in ("no cuts", "no split screen", "no camera-angle"):
            self.assertNotIn(bad, prompts.STRUCT_LOCK_EN)

    def test_contract_sentence_is_still_the_absolute_first(self):
        p = prompts.video_prompt(self.S, native=True)
        self.assertTrue(p.startswith(prompts.CONTRACT_FIRST_ZH))
        self.assertLess(p.index(prompts.STRUCT_LOCK_ZH), p.index("运镜：缓慢推近"))


class TestEngineNeverEmitsMultishotSyntax(unittest.TestCase):
    """装配后的提示词不许出现多镜语法。

    lint 的 `multishot_syntax` 只扫作者写的 `video_prompt`，引擎自己拼出来的那条
    没有任何扫描面；判据借 `variation.MULTISHOT_RE`，与那条 lint 同一个真源。
    附板镜按位置纪律不发结构锁，故两形态各扫一次——结构锁是提示词里的一句话，
    管不住段头本身。"""

    SHOT = {"id": 1, "camera": "中景平移", "action": "走过长廊", "guide": "sketch",
            "sketch": {"beats": [{"camera": "推近", "action": "起身"},
                                 {"camera": "环绕", "action": "回望"}]},
            "lines": [{"speaker": "林深", "text": "你终于来了。"}]}

    def test_authored_fixture_is_clean(self):
        """夹具自带多镜写法的话，下面测的就是夹具而不是引擎。"""
        for field in ("video_prompt", "camera"):
            self.assertIsNone(MULTISHOT_RE.search(self.SHOT.get(field) or ""))

    def test_compiled_timeline_never_carries_it(self):
        for unit in ("second", "shot"):
            for lang in ("zh", "en"):
                for board in (False, True):
                    with self.subTest(unit=unit, lang=lang, board=board):
                        p = prompts.video_prompt(
                            self.SHOT, native=True, lang=lang, ref_mode=True,
                            sketch=True, sketch_board=board, sketch_total=6.0,
                            timeline_unit=unit)
                        self.assertIsNone(MULTISHOT_RE.search(p), p)
                        # 废掉的是记号，分段本身必须还在
                        self.assertIn("起身", p)
                        self.assertIn("回望", p)


class TestVideoPromptFlf2v(unittest.TestCase):
    """首尾帧过渡专写：只在 cli 真的发出末帧时才写「只写过渡」。

    `flf2v` 是形参、不是自读——`project.frame_chain` 为真也可能因 dubbed 或
    下一镜缺图而根本没发末帧，自读必然与实际请求分叉。"""

    S = {"video_prompt": "他缓缓抬头"}

    def test_transition_rule_appended_when_chained(self):
        p = prompts.video_prompt(self.S, native=True, flf2v=True)
        self.assertEqual(p.count(prompts.FLF2V_ZH), 1)
        self.assertLess(p.index(prompts.FLF2V_ZH), p.index("他缓缓抬头"))

    def test_no_transition_rule_without_chain(self):
        p = prompts.video_prompt(self.S, native=True)
        self.assertNotIn(prompts.FLF2V_ZH, p)
        self.assertNotIn("末帧", p)

    def test_dubbed_never_gets_transition_rule(self):
        """dubbed 走参考媒体模式，seedance 侧末帧被忽略——写过渡句是错误措辞。"""
        p = prompts.video_prompt(self.S, native=False, flf2v=True)
        self.assertNotIn(prompts.FLF2V_ZH, p)
        self.assertNotIn("末帧", p)

    def test_en_variant(self):
        p = prompts.video_prompt({"video_prompt_en": "he looks up"},
                                 native=True, lang="en", flf2v=True)
        self.assertIn(prompts.FLF2V_EN, p)


class TestSelectStylePrefix(unittest.TestCase):
    def test_zh_provider_uses_zh(self):
        params = {"style_prefix": "水墨画风，", "style_prefix_en": "ink wash, "}
        self.assertEqual(prompts.select_style_prefix(params, "zh"), ("水墨画风", False))

    def test_en_provider_uses_en(self):
        params = {"style_prefix": "水墨画风，", "style_prefix_en": "ink wash style, "}
        self.assertEqual(prompts.select_style_prefix(params, "en"),
                         ("ink wash style", False))

    def test_en_missing_falls_back_with_flag(self):
        prefix, fell_back = prompts.select_style_prefix({"style_prefix": "水墨画风，"}, "en")
        self.assertEqual(prefix, "水墨画风")
        self.assertTrue(fell_back)          # 降级要被上层警告

    def test_empty_prefix_no_fallback_flag(self):
        self.assertEqual(prompts.select_style_prefix({}, "en"), ("", False))

    def test_doc_style_prompt_overrides_profile(self):
        # 画风单点真源：项目/章节文档顶层 style_prompt（立项快照）压过 profile 前缀
        params = {"style_prefix": "水墨画风，", "style_prefix_en": "ink wash style, "}
        doc = {"style_prompt": "赛博霓虹画风，", "style_prompt_en": "cyber neon style, "}
        self.assertEqual(prompts.select_style_prefix(params, "zh", doc=doc),
                         ("赛博霓虹画风", False))
        self.assertEqual(prompts.select_style_prefix(params, "en", doc=doc),
                         ("cyber neon style", False))

    def test_doc_blank_falls_back_to_profile(self):
        # 文档字段为空白 → 回落 profile 前缀
        params = {"style_prefix": "水墨画风，"}
        self.assertEqual(
            prompts.select_style_prefix(params, "zh", doc={"style_prompt": "  "}),
            ("水墨画风", False))



class TestShotVoiceResolution(unittest.TestCase):
    def test_priority_shot_voice_wins(self):
        proj = _ProjectStub(voices={"洛": "少年"})
        store = _StoreStub({"少年": "vt_shaonian", "御姐": "vt_yujie"})
        ref, vt = voicecast.resolve_shot_voice(
            proj, store, {"speaker": "洛", "voice": "御姐"}, "默认")
        self.assertEqual((ref, vt), ("御姐", "vt_yujie"))

    def test_speaker_map_then_default(self):
        proj = _ProjectStub(voices={"洛": "少年"})
        store = _StoreStub({"少年": "vt_shaonian"})
        ref, vt = voicecast.resolve_shot_voice(proj, store, {"speaker": "洛"}, "默认")
        self.assertEqual((ref, vt), ("少年", "vt_shaonian"))
        ref, vt = voicecast.resolve_shot_voice(proj, store, {"speaker": "路人"}, "默认")
        self.assertEqual((ref, vt), ("默认", "默认"))     # 不在别名表 → 原样返回

    def test_expressive_params(self):
        """模版生成只有 emotion 一条通道：`voice_instruction` 不进请求体
        （官方标准版静默过滤）。编译器仍在，归定制生成。详见 test_delivery。"""
        s = {"emotion": "angry", "emotion_scale": 5, "voice_instruction": "用哽咽的语气说"}
        self.assertEqual(voicecast.shot_expressive_params(s),
                         {"emotion": "angry", "emotion_scale": 5})
        self.assertEqual(voicecast.shot_expressive_params({"emotion_scale": 3}), {})
        self.assertEqual(voicecast.shot_expressive_params({"emotion": "sad"}),
                         {"emotion": "sad"})


class TestCharacterGenderFieldFlow(unittest.TestCase):
    """显式 gender 是人工字段——判定链本体归 tests/test_voicebank.py，这里只守流转。"""

    def test_gender_is_settable_and_synced_never_extracted(self):
        """显式 gender 的三条流转纪律（同 status）：进 CHAR_SETTABLE（character set
        --gender 可写）· 进 sync_design_to_chapters 白名单（存量章节看得见）·
        绝不进 upsert_entities（重抽不清人工字段）。"""
        import inspect

        from kinema.workspace import Series
        self.assertIn("gender", Series.CHAR_SETTABLE)
        sync_src = inspect.getsource(Series.sync_design_to_chapters)
        self.assertIn('"gender"', sync_src, "gender 不进 sync 白名单=系列填了章节看不见")
        up_src = inspect.getsource(Series.upsert_entities)
        self.assertNotIn('"gender"', up_src, "gender 是人工字段，重抽绝不登记")


class TestCharacterAnchorHardConstraints(unittest.TestCase):
    """角色锚：**外观交给像素、禁令交给负面通道**，两半各管各的。

    典型失败形态：设定图上白刻只有左臂是义体，生成的图里两条手臂都成了金属——
    `constraints`（"左臂以外不出现义体"）若只进单一路径、
    不在图像和视频负面通道统一落位，模型看设定图只学到"这角色有一条机械臂"，
    随手泛化成两条。
    约束是**否定式**的，设定图画不出"不该有什么"，只能靠文字说。
    """

    CAST = [{"name": "白刻", "subject_kind": "human",
             "appearance": "二十六七岁东亚女性，冷白皮肤",
             "visual_requirements": ["左臂义体", "八头身比例协调"],
             "constraints": ["左臂以外不出现义体"]}]

    def test_sheeted_character_keeps_positive_anchor_clean(self):
        block, _ = prompts.character_anchor_block(self.CAST, sheeted={"白刻"})
        self.assertIn("以其角色设定图为准", block)
        self.assertIn("服饰", block)
        self.assertIn("左臂义体", block)
        self.assertNotIn("左臂以外不出现义体", block)

    def test_sheeted_character_never_restates_appearance(self):
        """有像素锚时复述外貌=要求模型重画主体，是跨镜漂移头号来源。"""
        block, _ = prompts.character_anchor_block(self.CAST, sheeted={"白刻"})
        self.assertNotIn("冷白皮肤", block)

    def test_character_negative_block_keeps_all_hard_constraints(self):
        """没有设定图或有设定图，硬约束都进入同一条负面通道。"""
        neg = prompts.character_negative_block(self.CAST)
        self.assertIn("左臂以外不出现义体", neg)
        self.assertNotIn("左臂义体", neg)
        self.assertNotIn("八头身比例协调", neg, "正向视觉要求不能进入负面")

    def test_unsheeted_character_positive_anchor_is_only_appearance(self):
        block, _ = prompts.character_anchor_block(self.CAST)
        self.assertIn("冷白皮肤", block)
        self.assertIn("左臂义体", block)
        self.assertNotIn("左臂以外不出现义体", block)

    def test_constraint_free_character_reads_clean(self):
        cast = [{"name": "路人", "appearance": "中年男性"}]
        self.assertEqual(prompts.character_anchor_block(cast, sheeted={"路人"})[0],
                         "路人（外观、体态与设定中登记的特征以其角色设定图为准）")
        self.assertEqual(prompts.character_anchor_block(cast)[0], "路人——中年男性")

    def test_flat_text_is_the_single_normalizer(self):
        """列表/单条两种形态的归一只能有一份——图像侧与视频侧读的是同一批字段。"""
        self.assertEqual(prompts.flat_text(["甲", "乙"]), "甲；乙")
        self.assertEqual(prompts.flat_text("甲"), "甲")
        self.assertEqual(prompts.flat_text(None), "")
        self.assertEqual(prompts.flat_text(["", "  ", "丙"]), "丙")
        from pathlib import Path as _P
        src = (_P(__file__).resolve().parents[1] / "kinema" / "cli.py").read_text(
            encoding="utf-8")
        self.assertIn("prompts_mod.flat_text", src, "cli 侧必须复用同一个归一口")

    def test_animal_outfit_and_taboo_land_in_negative_block(self):
        cast = [{"name": "涟耳", "subject_kind": "animal",
                 "outfit": "不穿戴任何人造物",
                 "constraints": ["人类服装, 直立行走"],
                 "taboo_lines": ["不做拟人化手势"]}]
        neg = prompts.character_negative_block(cast)
        self.assertIn("人类服装", neg)
        self.assertIn("直立行走", neg)
        self.assertNotIn("不做拟人化手势", neg)
        self.assertIn("不穿任何人类衣物", neg)
        self.assertIn("不戴项圈、吊坠、饰品或鞍具", neg)

    def test_registered_accessory_is_not_banned_for_owner(self):
        neg = prompts.character_negative_block([
            {"name": "阿月", "subject_kind": "animal",
             "outfit": "只佩戴一枚旧黄铜星航项圈，不穿人类衣物",
             "constraints": ["直立行走"]}])
        self.assertIn("除登记服饰/穿戴与配件外", neg)
        self.assertNotIn("不戴项圈、吊坠、饰品或鞍具", neg)

    def test_character_negative_is_not_positive_prompt(self):
        shot = {"image_prompt": "低伏观察", "negative_prompt": "字幕"}
        character_negative = prompts.character_negative_block([
            {"name": "涟耳", "subject_kind": "animal",
             "outfit": "不穿戴任何人造物",
             "constraints": ["人类服装, 直立行走"]}])
        prompt = prompts.image_prompt(shot, character_block="涟耳（以设定图为准）",
                                       character_negative=character_negative)
        positive = prompt.split("。避免出现：", 1)[0]
        self.assertNotIn("人类服装", positive)
        self.assertIn("人类服装", prompt)

    def test_human_clothing_and_accessories_stay_positive(self):
        cast = [{"name": "指挥官", "subject_kind": "human",
                 "appearance": "成年女性",
                 "outfit": "深蓝色军装",
                 "visual_requirements": ["圆框眼镜", "左臂义体"],
                 "constraints": ["不出现额外未登记徽章"]}]
        block, _ = prompts.character_anchor_block(cast)
        self.assertIn("登记服饰/穿戴：深蓝色军装", block)
        self.assertIn("圆框眼镜", block)
        self.assertIn("左臂义体", block)
        neg = prompts.character_negative_block(cast)
        self.assertIn("不出现额外未登记徽章", neg)
        self.assertNotIn("深蓝色军装", neg)
        self.assertNotIn("圆框眼镜", neg)
        self.assertNotIn("不穿任何人类衣物", neg)

    def test_video_constraints_use_negative_channel(self):
        negative = prompts.character_negative_block([{
            "name": "涟耳", "subject_kind": "animal",
            "outfit": "不穿戴任何人造物",
            "constraints": ["不直立行走"],
        }])
        positive = prompts.video_prompt(
            {"video_prompt": "低伏穿过发光苔藓"}, native=True,
            cast_anchor="涟耳（自然体态）", character_negative=negative,
            include_negative=False)
        full = prompts.video_prompt(
            {"video_prompt": "低伏穿过发光苔藓"}, native=True,
            cast_anchor="涟耳（自然体态）", character_negative=negative,
            include_negative=True)
        self.assertNotIn("不直立行走", positive)
        self.assertIn("不直立行走", full)
        self.assertIn("不穿任何人类衣物", full)


if __name__ == "__main__":
    unittest.main()


class TestPerformanceFloor(unittest.TestCase):
    """表演地板：缺省不叹气、不深呼吸、不流泪——视频模型把「有生命感」演过头的
    三种惯用形态，不写也会自发冒出来。只有本镜正文点名了才是剧情要求：对应词摘掉、
    其余照拦；否定写法不算点名；台词文本不算（说「我哭了」不等于画面要流泪）。"""

    @staticmethod
    def _neg(p, lang="zh"):
        return p.split("Avoid: " if lang == "en" else "避免出现：")[-1]

    def test_default_negative_blocks_all_four(self):
        neg = self._neg(prompts.video_prompt({"video_prompt": "他转身走开"}, native=True))
        for t in ("叹气", "深呼吸", "明显的胸肩起伏", "流泪"):
            self.assertIn(t, neg)

    def test_author_mention_releases_only_that_term_and_negation_does_not_count(self):
        neg = self._neg(prompts.video_prompt(
            {"video_prompt": "他叹了口气，眼眶泛红但没落泪"}, native=True))
        self.assertNotIn("叹气", neg)
        self.assertIn("流泪", neg)

    def test_beats_and_line_emotion_count_but_dialogue_text_does_not(self):
        s = {"video_prompt": "她看着他",
             "sketch": {"beats": [{"t": "0-2s", "action": "泪水滑下"}]},
             "lines": [{"speaker": "A", "text": "我哭了", "emotion": "平静"}]}
        self.assertNotIn("流泪", self._neg(prompts.video_prompt(s, native=True)))
        s2 = {"video_prompt": "她看着他",
              "lines": [{"speaker": "A", "text": "我哭了", "emotion": "平静"}]}
        self.assertIn("流泪", self._neg(prompts.video_prompt(s2, native=True)))
        s3 = {"video_prompt": "她看着他",
              "lines": [{"speaker": "A", "text": "走吧", "emotion": "含泪"}]}
        self.assertNotIn("流泪", self._neg(prompts.video_prompt(s3, native=True)))

    def test_english_side(self):
        p = prompts.video_prompt({"video_prompt_en": "he sighs, never cries"},
                                 native=True, lang="en")
        neg = self._neg(p, "en")
        self.assertNotIn("sighing", neg)
        self.assertIn("tears", neg)

    def test_author_negative_is_not_duplicated(self):
        neg = self._neg(prompts.video_prompt(
            {"video_prompt": "他转身", "negative_prompt": "流泪, 眼镜"}, native=True))
        self.assertEqual(neg.count("流泪"), 1)

    def test_motion_floors_do_not_ask_for_visible_breathing(self):
        """「呼吸起伏」会被模型演成深呼吸与叹气——生命感用「细微微动」表达。"""
        for s in (prompts.MICRO_MOTION_ZH, prompts.DELTA_FALLBACK_ZH):
            self.assertNotIn("呼吸", s)
        for s in (prompts.MICRO_MOTION_EN, prompts.DELTA_FALLBACK_EN):
            self.assertNotIn("breath", s)


class TestMicroMotionTail(unittest.TestCase):
    """微动恒常尾句：写了运动设计的镜才追加，治「主动作演完全体停死」。

    成因反直觉——写了一整套大动作的镜反而**没有**微动保底，而只写了一笔都没有的
    镜倒有（兜底句自己就说「只做轻微自然的生命感微动与环境流动」）。所以两者必须
    **互斥**，且互斥用结构表达（`if not body:` 的 else 分支）而不是字符串比对。

    注入点是**沙箱实测选出来的**：放在 `if not body:` 之前无条件追加会打红三条
    （`test_no_fallback_to_image_prompt` 与 `test_review.TestFrameChainLastFrameGate`
    的两条），而文档声称守着等价关系的 `test_delta_missing_flag_matches_actual_fallback`
    照样绿——所以下面第三条断言才是真正挡住错误注入点的那一条。"""

    def test_appended_to_a_shot_that_has_motion_design(self):
        for lang, tail in (("zh", prompts.MICRO_MOTION_ZH), ("en", prompts.MICRO_MOTION_EN)):
            s = {"video_prompt": "他猛地转身挥出一刀", "video_prompt_en": "he spins and swings"}
            p = prompts.video_prompt(s, native=True, lang=lang)
            self.assertEqual(p.count(tail), 1, lang)

    def test_wording_never_says_hold_still(self):
        """它是"额外还要动"的指令，不是"别动"——含「保持不变」就把自己变成反向指令。"""
        self.assertNotIn("保持不变", prompts.MICRO_MOTION_ZH)
        self.assertNotIn("unchanged", prompts.MICRO_MOTION_EN)

    def test_never_doubled_onto_a_fallback_shot(self):
        """**这条是唯一挡得住错误注入点的断言**：兜底句已经在说轻微呼吸了。"""
        p = prompts.video_prompt({"image_prompt": "老者立于崖边"}, native=True)
        self.assertIn(prompts.DELTA_FALLBACK_ZH, p)
        self.assertNotIn(prompts.MICRO_MOTION_ZH, p)
        p_en = prompts.video_prompt({"image_prompt_en": "an old man"}, native=True, lang="en")
        self.assertIn(prompts.DELTA_FALLBACK_EN, p_en)
        self.assertNotIn(prompts.MICRO_MOTION_EN, p_en)

    def test_not_added_on_a_chained_shot(self):
        """FLF2V 已在要求「运动须自然收束在末帧上」——再加一条持续指令会互相稀释。"""
        p = prompts.video_prompt({"video_prompt": "他猛地转身"}, native=True, flf2v=True)
        self.assertNotIn(prompts.MICRO_MOTION_ZH, p)

    def test_not_repeated_when_the_author_already_wrote_breathing(self):
        """作者正文常自写呼吸/起伏——与 camera/sfx 同制去重。"""
        s = {"video_prompt": "他静立不动，肩背随急促呼吸起伏"}
        self.assertNotIn(prompts.MICRO_MOTION_ZH, prompts.video_prompt(s, native=True))

    def test_survives_the_auto_sketch_path(self):
        """自动拆拍那一支是 `vmotion = tl`（整体替代正文）——注在 body 上会被静默吞掉。"""
        s = {"id": 1, "dur": 4.0, "video_prompt": "他起身。他迈步。他回望。"}
        p = prompts.video_prompt(s, native=True, sketch=True, sketch_total=4.0)
        self.assertIn("时间轴：", p)
        self.assertIn(prompts.MICRO_MOTION_ZH, p)

    def test_camera_is_still_the_first_token_of_the_creative_body(self):
        p = prompts.video_prompt({"camera": "缓慢推近", "video_prompt": "他转身"}, native=True)
        self.assertLess(p.index("运镜：缓慢推近"), p.index(prompts.MICRO_MOTION_ZH))

    def test_no_punctuation_seam_before_the_tail(self):
        """正文以句号收尾时，这一拼若无条件补「，」会产出「。，」。"""
        p = prompts.video_prompt({"video_prompt": "他猛地转身挥出一刀。"}, native=True)
        self.assertNotIn("。，", p)
        self.assertIn("挥出一刀。" + prompts.MICRO_MOTION_ZH, p)

    def test_wording_is_clean_of_the_lint_word_lists(self):
        """该常量每镜逐字重发——命中反 slop / 抽象情绪词表就是全片刷屏。"""
        from kinema.pipeline import variation as vr
        for term in list(vr.SLOP_TERMS) + list(vr.EMOTION_TERMS) + list(vr.UNFILMABLE_TERMS):
            self.assertNotIn(term, prompts.MICRO_MOTION_ZH, term)
            self.assertNotIn(term, prompts.micro_motion(["animal"]), term)


class TestPaceFloor(unittest.TestCase):
    """播放速率地板：动作按真实速度演。

    「前肢交替各三个循环」写在 10 秒镜上等于指定 0.3Hz 的步频，而该物种冲刺的真实
    步频是 2.5~3Hz；越听话的型号越照着压慢。这类错误藏在「次数 ÷ 镜长」这个除法里、
    从字面看不出来，所以做成地板而不是逐镜自查。
    """

    def test_injected_by_default(self):
        p = prompts.video_prompt({"video_prompt": "它猛地转身冲出去"}, native=True)
        self.assertIn(prompts.PACE_ZH, p)
        p_en = prompts.video_prompt({"video_prompt_en": "it spins and bolts"},
                                    native=True, lang="en")
        self.assertIn(prompts.PACE_EN, p_en)

    def test_yields_to_a_deliberate_speed_technique(self):
        """作者点名升格/慢放/延时时慢或快是有意的——地板会与它当场对撞。"""
        for word in ("升格", "慢放", "子弹时间", "延时摄影", "快进"):
            p = prompts.video_prompt({"video_prompt": f"用{word}展现水珠飞散"}, native=True)
            self.assertNotIn(prompts.PACE_ZH, p, word)
        # 写在运镜里同样要认出来——变速技法两处都可能写
        p = prompts.video_prompt({"video_prompt": "转身", "camera": "跟拍，升格"}, native=True)
        self.assertNotIn(prompts.PACE_ZH, p)
        for word in ("slow motion", "bullet time", "speed ramp", "timelapse"):
            p = prompts.video_prompt({"video_prompt_en": f"a {word} of the spray"},
                                     native=True, lang="en")
            self.assertNotIn(prompts.PACE_EN, p, word)

    def test_not_injected_on_v2v(self):
        """运动节奏归参考视频管，再压一条速率指令是两个并列的运动权威。"""
        p = prompts.video_prompt({"video_prompt": "转身"}, native=True, ref_video=True)
        self.assertNotIn(prompts.PACE_ZH, p)

    def test_sits_in_the_head_before_the_camera(self):
        p = prompts.video_prompt({"video_prompt": "转身", "camera": "缓慢推近"}, native=True)
        self.assertLess(p.index(prompts.PACE_ZH), p.index("运镜：缓慢推近"))

    def test_wording_never_asks_for_stillness_or_hits_the_word_lists(self):
        from kinema.pipeline import variation as vr
        self.assertNotIn("保持不变", prompts.PACE_ZH)
        for term in list(vr.SLOP_TERMS) + list(vr.EMOTION_TERMS) + list(vr.UNFILMABLE_TERMS):
            self.assertNotIn(term, prompts.PACE_ZH, term)


class TestMicroMotionFollowsSubjectKind(unittest.TestCase):
    """随动附属物的名词按 `characters[].subject_kind` 选。

    「发丝衣料随动作摆动」发给一个动物主体，说的是它身上没有的东西——每镜逐字重发，
    等于全片都在要求一样不存在的附属物跟随运动。名词只能查表，绝不从外貌文本猜
    （与 `subject_kind` 本身「不填不猜测」同一条纪律）。
    """

    def test_each_registered_kind_gets_its_own_noun(self):
        self.assertIn("毛发随动作", prompts.micro_motion(["animal"]))
        self.assertIn("发丝与衣料随动作", prompts.micro_motion(["human"]))
        self.assertIn("线缆与外挂配重", prompts.micro_motion(["robot"]))
        self.assertNotIn("衣料", prompts.micro_motion(["animal"]))
        self.assertNotIn("毛发", prompts.micro_motion(["human"]))

    def test_unregistered_kind_drops_the_clause_instead_of_guessing(self):
        """认不出就只发力学与呼吸两半——错的名词比不写更糟。"""
        for kinds in ((), ("other",), ("", None), ("不存在的类型",)):
            out = prompts.micro_motion(kinds)
            self.assertEqual(out, prompts.MICRO_MOTION_ZH, kinds)
            self.assertNotIn("随动作自然跟随", out, kinds)
        # 力学与呼吸两半在任何情况下都不许丢——它们才是这条地板的本体
        self.assertIn(prompts.MICRO_MOTION_HEAD_ZH, prompts.MICRO_MOTION_ZH)
        self.assertIn(prompts.MICRO_MOTION_TAIL_ZH, prompts.MICRO_MOTION_ZH)

    def test_mixed_cast_merges_in_registration_order_without_picking_a_lead(self):
        out = prompts.micro_motion(["human", "animal"])
        self.assertIn("发丝与衣料、毛发随动作", out)
        self.assertEqual(prompts.micro_motion(["animal", "animal"]).count("毛发"), 1,
                         "同类型只说一次")

    def test_english_uses_a_participle_so_uncountable_nouns_agree(self):
        """`fur follow …` 主谓不一致，而名词是查表来的、数不固定。"""
        out = prompts.micro_motion(["animal"], "en")
        self.assertIn("fur following the motion", out)
        self.assertNotIn("fur follow the", out)
        self.assertEqual(prompts.micro_motion((), "en"), prompts.MICRO_MOTION_EN)

    def test_video_prompt_ships_the_selected_noun(self):
        p = prompts.video_prompt({"video_prompt": "它猛地转身冲出去"}, native=True,
                                 subject_kinds=["animal"])
        self.assertIn("毛发随动作自然跟随摆动后回落", p)
        self.assertNotIn("发丝", p)


class TestZhJoinAllIsTheOnlySeamFix(unittest.TestCase):
    """`。；` 是 `。，` 的同类：`cli._cast_anchor_text` 若用 `"；".join` 拼视觉锚点与特征，
    而剪影锚点常以句号收尾——中招面比「。，」还大得多。

    两个拼接点必须共用同一个函数：各写一份判断就会各修一半（这条守卫钉的正是这件事）。"""

    def test_period_ending_part_does_not_get_a_separator(self):
        got = prompts.zh_join_all(["像随时准备蹲下去看清什么东西。", "绝不摘掉面罩"], sep="；")
        self.assertNotIn("。；", got)
        self.assertEqual(got, "像随时准备蹲下去看清什么东西。绝不摘掉面罩")

    def test_separator_still_added_without_sentence_punctuation(self):
        self.assertEqual(prompts.zh_join_all(["八头身修长剪影", "绝不露齿笑"], sep="；"),
                         "八头身修长剪影；绝不露齿笑")

    def test_empty_parts_are_dropped(self):
        self.assertEqual(prompts.zh_join_all(["", "只有一条", None], sep="；"), "只有一条")
        self.assertEqual(prompts.zh_join_all([], sep="；"), "")

    def test_cli_uses_the_shared_joiner_not_a_bare_join(self):
        """源级：cli 侧不许再写回裸 `"；".join` —— 那是这条缝复活的唯一形态。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli._cast_anchor_text)
        self.assertIn("zh_join_all", src)
        self.assertNotIn('"；".join', src)


class TestEntryStateDelta(unittest.TestCase):
    """entry_state 进 delta 骨架：承接句领投（先说从哪接、再说做什么）。"""

    def test_entry_state_injected_with_label(self):
        s = {"video_prompt": "他缓缓抬头", "entry_state": "镜头仍停在空椅上"}
        p = prompts.video_prompt(s, native=True)
        self.assertIn("开场承接：镜头仍停在空椅上", p)
        self.assertLess(p.index("开场承接"), p.index("他缓缓抬头"))

    def test_entry_state_en_label(self):
        s = {"video_prompt_en": "he looks up", "entry_state": "hold on the empty chair"}
        p = prompts.video_prompt(s, native=True, lang="en")
        self.assertIn("Opens from: hold on the empty chair", p)

    def test_duplicate_text_not_reinjected(self):
        s = {"video_prompt": "镜头仍停在空椅上，他缓缓抬头",
             "entry_state": "镜头仍停在空椅上"}
        self.assertNotIn("开场承接", prompts.video_prompt(s, native=True))


class TestTailBindingClause(unittest.TestCase):
    def test_tail_kind_gets_dedicated_duty_sentence(self):
        clause = prompts.sheet_binding_clause(
            [("frame", ""), ("tail", ""), ("character", "陆昭")])
        self.assertIn("@图片2 为上一镜的收尾画面", clause)
        self.assertIn("@图片3 为角色「陆昭」的设定图", clause)
        self.assertIn("仍须可辨认", clause)

    def test_tail_only_manifest_skips_sheet_trailer(self):
        clause = prompts.sheet_binding_clause([("frame", ""), ("tail", "")])
        self.assertIn("@图片2 为上一镜的收尾画面", clause)
        self.assertNotIn("仍须可辨认", clause, "收尾句只跟设定图走——尾帧不是设定资产")


class TestAllrefBaseLighting(unittest.TestCase):
    """降级路线的光线权威：镜写了 `lighting` 时移交本镜描述，基准图只保留
    陈设与材质——基准图的时段是生成场景图时自选的，一张黄昏空景会把白天戏
    整镜拖成夜戏（正路 A 下分镜图压住它、问题不显形，一降级就是光线真源）。"""

    def test_authored_lighting_overrides_the_base_plate(self):
        c = prompts.allref_base_contract({"lighting": "正午强光，顶光直射"}, "zh")
        self.assertIn("本镜光线按「正午强光，顶光直射」执行", c)
        self.assertIn("其光线与时段不沿用", c)
        self.assertNotIn("光线基调的基准", c)
        e = prompts.allref_base_contract({"lighting": "noon sun"}, "en")
        self.assertIn("light this shot as described - noon sun", e)

    def test_without_lighting_the_plate_stays_the_light_baseline(self):
        self.assertEqual(prompts.allref_base_contract({}, "zh"),
                         prompts.CONTRACT_ALLREF_BASE_ZH)
        self.assertEqual(prompts.allref_base_contract({}, "en"),
                         prompts.CONTRACT_ALLREF_BASE_EN)

    def test_video_prompt_ref_base_threads_the_shot(self):
        p = prompts.video_prompt(
            {"id": 1, "dur": 5, "video_prompt": "回身。", "lighting": "白天正午"},
            native=True, ref_mode=True, ref_base=True)
        self.assertIn("本镜光线按「白天正午」执行", p)


class TestVoiceClauseSeconds(unittest.TestCase):
    """2.5 档台词秒段整秒化：逐段边界取整、每句至少 1 秒、与拍表时间轴同一粒度。"""

    def test_short_line_keeps_at_least_one_second(self):
        spans = [({"text": "很长很长很长很长很长的一句话"}, 0.0, 4.6),
                 ({"text": "好"}, 4.6, 5.0)]
        self.assertEqual(prompts._integer_spans(spans, 5.0), [(0, 4), (4, 5)])
        s = {"id": 1, "dur": 5, "lines": [{"speaker": "甲", "text": "很长很长很长很长很长的一句话"},
                                          {"speaker": "乙", "text": "好"}]}
        clause = prompts.native_voice_clause(s, total=5.0, unit="second")
        self.assertIn("0-4秒：甲", clause)
        self.assertIn("4-5秒：乙", clause)
        self.assertNotIn("5-5秒", clause)


class TestWipePhraseGate(unittest.TestCase):
    def test_hand_written_wipe_phrase_suppresses_structural_lock(self):
        """Skill 教作者抄进 camera 的是预设的中文措辞列（没有 camera_preset 键）——
        判据只认 key/label 时，擦镜镜会同时收到「无痕切换」与「同一台摄影机不间断」。"""
        from kinema.pipeline import camera as camera_mod
        for key, preset in camera_mod.CAMERA_PRESETS.items():
            if not preset.get("wipe"):
                continue
            s = {"video_prompt": "他缓缓抬头", "camera": preset["phrase"]}
            self.assertNotIn(prompts.STRUCT_LOCK_ZH, prompts.video_prompt(s, native=True))


class TestEnglishVoiceClauses(unittest.TestCase):
    def test_en_provider_gets_english_voice_and_fix_sentences(self):
        s = {"id": 1, "dur": 5, "video_prompt_en": "he slowly looks up",
             "lines": [{"speaker": "Lin", "text": "You came."}],
             "review": {"clip": {"state": "retake", "note": "too slow"}}}
        vp = prompts.video_prompt(s, native=True, lang="en")
        self.assertIn("Lin says: “You came.”", vp)
        self.assertIn("Revision focus (must apply): too slow.", vp)
        self.assertNotIn("口型", vp)
        self.assertNotIn("本次修正重点", vp)
        silent = prompts.native_voice_clause({"id": 2, "dur": 3}, lang="en")
        self.assertTrue(prompts.positive_is_voiceless(silent))
        self.assertIn("nobody lip-syncs", prompts.dubbed_voice_clause(
            {"id": 3, "lines": [{"speaker": "Lin", "text": "Go."},
                                {"speaker": "旁白", "text": "He left."}]}, lang="en"))

    def test_video_fix_note_ends_the_sentence_before_the_voice_clause(self):
        s = {"id": 1, "dur": 3, "video_prompt": "他缓缓抬头",
             "review": {"clip": {"state": "retake", "note": "动作太慢"}}}
        vp = prompts.video_prompt(s, native=True)
        self.assertIn("本次修正重点（务必执行）：动作太慢。", vp)
        self.assertNotIn("动作太慢，本镜无台词", vp)
