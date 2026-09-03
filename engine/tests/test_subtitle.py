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

"""kinema.pipeline.subtitle 单元测试：_wrap 断行 / _ass_time 时间码 / 五种 mode 分发。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kinema.pipeline import speech, subtitle
from tests.support import fake_path


class TestAssTime(unittest.TestCase):
    def test_zero_and_negative_clamped(self):
        self.assertEqual(subtitle._ass_time(0), "0:00:00.00")
        self.assertEqual(subtitle._ass_time(-5.3), "0:00:00.00")

    def test_plain_value(self):
        self.assertEqual(subtitle._ass_time(12.5), "0:00:12.50")

    def test_centisecond_carry(self):
        # 3.999 → 厘秒 round 到 100，触发进位保护：秒 +1、厘秒归零
        self.assertEqual(subtitle._ass_time(3.999), "0:00:04.00")
        self.assertEqual(subtitle._ass_time(1.999), "0:00:02.00")

    def test_hour_minute_fields(self):
        self.assertEqual(subtitle._ass_time(3661.25), "1:01:01.25")


class TestWrap(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(subtitle._wrap("你好世界"), "你好世界")
        # 恰好 16 字不折行
        t16 = "一二三四五六七八九十甲乙丙丁戊己"
        self.assertEqual(subtitle._wrap(t16), t16)

    def test_punctuation_break_and_strip(self):
        # 18 字、标点在中点附近 → 在标点后断行，且行尾标点被剥掉（不悬挂在行首/行尾）
        text = "一二三四五六七八，九十甲乙丙丁戊己"
        self.assertEqual(subtitle._wrap(text), "一二三四五六七八\\N九十甲乙丙丁戊己")

    def test_nearest_punctuation_to_middle_wins(self):
        # 标点在 1 与 14 两处，离中点(9)更近的 14 被选中；行内标点保留、行尾标点剥除
        text = "一，二三四五六七八九十甲乙丙，丁戊己"
        self.assertEqual(subtitle._wrap(text),
                         "一，二三四五六七八九十甲乙丙\\N丁戊己")

    def test_no_punctuation_cuts_middle(self):
        text = "一二三四五六七八九十甲乙丙丁戊己庚辛"     # 18 字无标点
        self.assertEqual(subtitle._wrap(text),
                         "一二三四五六七八九\\N十甲乙丙丁戊己庚辛")

    def test_trailing_punctuation_not_a_break_point(self):
        # 唯一标点是末字符 → 不作断点（否则产生空行），退回中点切分
        text = "一二三四五六七八九十甲乙丙丁戊己。"       # 17 字
        self.assertEqual(subtitle._wrap(text),
                         "一二三四五六七八\\N九十甲乙丙丁戊己。")

    def test_two_line_limit(self):
        # _wrap 永远只产出至多两行（一个 \N）
        text = "一二三四五六七八，九十甲乙丙丁，戊己庚辛壬癸子丑寅卯"
        self.assertLessEqual(subtitle._wrap(text).count("\\N"), 1)

    def test_line_length_enforced_over_punctuation(self):
        # 标点在合法断点区间外时不迁就标点——
        # 只要总长 ≤2×16，两行都必须 ≤16 字（Netflix 简中口径）
        text = "啊" * 5 + "，" + "呀" * 22          # len=28，唯一标点在区间外
        lines = subtitle._wrap(text).split("\\N")
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertLessEqual(len(ln), 16, f"行超限: {ln!r}")

    def test_overflow_splits_balanced_without_loss(self):
        # 超两行容量（>32 字）平分兜底，不丢字
        text = "呀" * 40
        lines = subtitle._wrap(text).split("\\N")
        self.assertEqual(len(lines), 2)
        self.assertEqual("".join(lines), text)


class TestWrapLines(unittest.TestCase):
    def test_within_limit_unchanged(self):
        self.assertEqual(subtitle._wrap_lines("你好", 4, 2), "你好")

    def test_punctuation_hanging(self):
        # 标点悬挂：句读跟随前行，不落到下一行行首
        self.assertEqual(subtitle._wrap_lines("一二三，四五六。", 3, 3),
                         "一二三，\\N四五六。")

    def test_overflow_ellipsis(self):
        # 超出 max_lines 的部分以省略号收尾
        self.assertEqual(subtitle._wrap_lines("一二三四五六七八九十", 4, 2),
                         "一二三四\\N五六七…")


class TestRenderDispatch(unittest.TestCase):
    """五种 mode 的 render 分发：各自生成 ASS 并含模式特有的样式/元素标记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "sub.ass"

    def tearDown(self):
        self.tmp.cleanup()

    def _render(self, timeline, cfg):
        path = subtitle.render(timeline, self.out, sub_cfg=cfg)
        self.assertEqual(path, str(self.out))
        return self.out.read_text(encoding="utf-8")

    def test_caption_default_mode(self):
        timeline = [(0.0, 2.5, {"caption": "字幕文本"}),
                    (2.5, 5.0, {"narration": "旁白回退文本"}),
                    (5.0, 6.0, {})]                       # 无文案镜跳过
        content = self._render(timeline, None)
        self.assertIn("Style: Default", content)
        self.assertIn("PlayResX: 1080", content)
        self.assertEqual(content.count("Dialogue:"), 2)   # 空镜不产 Dialogue
        self.assertIn("0:00:00.00,0:00:02.50", content)
        self.assertIn("字幕文本", content)
        self.assertIn("旁白回退文本", content)

    def test_caption_speaker_tag(self):
        timeline = [(0.0, 2.0, {"caption": "你好", "speaker": "小明"})]
        content = self._render(timeline, {"mode": "caption", "speaker_tag": True})
        self.assertIn("「小明」", content)

    def test_bubble_mode_dialogue_vs_narration(self):
        timeline = [
            (0.0, 2.0, {"dialogue": "我们走吧", "speaker": "小明", "bubble_pos": "left"}),
            (2.0, 4.0, {"narration": "夜色渐深"}),        # 旁白无说话人 → 退回底部字幕
        ]
        content = self._render(timeline, {"mode": "bubble"})
        self.assertIn("BShape", content)                  # 气泡框体
        self.assertIn("BName", content)                   # 说话人名
        self.assertIn("我们走吧", content)
        self.assertIn(",Default,,0,0,0,,夜色渐深", content)

    def test_dialogue_box_mode(self):
        timeline = [(0.0, 3.0, {"dialogue": "你好啊", "speaker": "小明"})]
        content = self._render(timeline, {"mode": "dialogue_box"})
        self.assertIn("AVBox", content)
        self.assertIn("AVName", content)
        self.assertIn("小明", content)
        self.assertIn("你好啊", content)
        self.assertIn("▼", content)                       # 翻页指示符

    def test_centered_mode(self):
        timeline = [(0.0, 4.0, {"narration": "生活是一场修行", "attribution": "尼采"})]
        content = self._render(timeline, {"mode": "centered"})
        self.assertIn("QText", content)
        self.assertIn("— 尼采", content)

    def test_ranking_mode(self):
        timeline = [(0.0, 3.0, {"rank": 1, "title": "第一名", "caption": "说明文字"})]
        content = self._render(timeline, {"mode": "ranking"})
        self.assertIn("RBadge", content)
        self.assertIn("RNum", content)
        self.assertIn("第一名", content)
        self.assertIn("说明文字", content)


class TestBilingual(unittest.TestCase):
    """字幕语言（zh/en/both）：文本位选取、英文词界折行、双语栈式版式。"""

    _SHOT = {"id": 1, "narration": "少年背起行囊，离开了村庄。",
             "narration_en": "The boy shouldered his pack and left the village."}

    def _render(self, lang, shot=None, mode="caption"):
        tl = [(0.0, 3.0, shot or dict(self._SHOT))]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.ass"
            subtitle.render(tl, out, canvas_w=1080, canvas_h=1920,
                            sub_cfg={"mode": mode, "lang": lang})
            return out.read_text(encoding="utf-8")

    def test_pick_texts_chain(self):
        # 音字一致铁律：有旁白的镜字幕逐字取 narration（配音念什么字幕写什么），
        # caption 只在无旁白的纯画面镜补位
        s = dict(self._SHOT, caption="中文字幕位", caption_en="EN caption")
        self.assertEqual(subtitle.pick_texts(s, "zh"),
                         ("少年背起行囊，离开了村庄。", ""))
        self.assertEqual(subtitle.pick_texts(s, "en")[0],
                         "The boy shouldered his pack and left the village.")
        self.assertEqual(subtitle.pick_texts(s, "both"),
                         ("少年背起行囊，离开了村庄。",
                          "The boy shouldered his pack and left the village."))

    def test_caption_fills_silent_shot(self):
        # 无旁白的纯画面镜：caption 补位（不留空窗）
        s = {"caption": "三年后·深夜", "caption_en": "Three years later"}
        self.assertEqual(subtitle.pick_texts(s, "zh"), ("三年后·深夜", ""))
        self.assertEqual(subtitle.pick_texts(s, "en")[0], "Three years later")

    def test_en_falls_back_to_zh(self):
        # 缺英文位回落中文——宁可有字不留空窗
        s = {"narration": "只有中文。"}
        self.assertEqual(subtitle.pick_texts(s, "en")[0], "只有中文。")

    def test_voice_tags_stripped_from_subtitle(self):
        """`<cot text=…>` 是**给 TTS 的**语音标签（storyboard.md 明文教写在台词里），
        字幕逐字取 narration 会把它原样烧进画面——取字幕时必须脱标签留内容。
        清洗只发生在这一步：绝不反向改写 narration（那是音字一致铁律的真源）。"""
        s = {"narration": "<cot text=急促难耐>快跑！</cot>别回头。",
             "narration_en": "<cot text=urgent>Run!</cot> Don't look back."}
        self.assertEqual(subtitle.pick_texts(s, "zh")[0], "快跑！别回头。")
        self.assertEqual(subtitle.pick_texts(s, "en")[0], "Run! Don't look back.")
        self.assertEqual(s["narration"], "<cot text=急促难耐>快跑！</cot>别回头。")  # 原文不动
        # 演出型模式（对话框/居中/榜单）的文本位同样清洗
        self.assertEqual(subtitle.strip_voice_tags("<break time=\"300ms\"/>稍等"), "稍等")
        # 半角尖括号不是标签：台词里的「体温<36」不许被吃掉
        self.assertEqual(subtitle.strip_voice_tags("体温<36 度"), "体温<36 度")
        self.assertEqual(subtitle.strip_voice_tags(""), "")

    def test_zh_default_single_line(self):
        c = self._render("zh")
        self.assertIn("少年背起行囊", c)
        self.assertNotIn("DefaultEn", c)              # 单语不产生英文样式
        self.assertNotIn("shouldered", c)

    def test_en_single_line_uses_en_text(self):
        c = self._render("en")
        self.assertIn("The boy shouldered", c)
        self.assertNotIn("少年背起行囊", c)

    def test_both_single_event_stacked(self):
        c = self._render("both")
        self.assertIn("少年背起行囊", c)               # 中文主行
        self.assertIn("The boy shouldered", c)         # 英文副行
        self.assertIn("Style: DefaultEn,", c)          # 英文副行样式（0.62× 字号）
        self.assertEqual(c.count("Dialogue:"), 1)      # 一镜一个事件（防 libass 碰撞换位）
        # 单事件内中文在前、\rDefaultEn 切换后英文在后 → 版式恒定中文上英文下
        line = next(l for l in c.splitlines() if l.startswith("Dialogue:"))
        self.assertLess(line.index("少年"), line.index("\\rDefaultEn"))
        self.assertLess(line.index("\\rDefaultEn"), line.index("The boy"))

    def test_wrap_en_word_boundary(self):
        w = subtitle._wrap_en("the quick brown fox jumps over the lazy dog "
                              "near the quiet river bank at dawn", 42)
        lines = w.split("\\N")
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertLessEqual(len(ln), 43)
            self.assertFalse(ln.startswith(" ") or ln.endswith(" "))
        self.assertNotIn("fo\\N", w)                   # 绝不从单词中间切开

    def test_en_view_for_stage_modes(self):
        # 演出型模式（气泡等）en 单语：文本位换用英文字段
        c = self._render("en", shot=dict(self._SHOT, speaker=""), mode="bubble")
        self.assertIn("The boy shouldered", c)
        self.assertNotIn("少年背起行囊", c)


class TestMarginV(unittest.TestCase):
    """底部字幕距底——**横竖屏逻辑不同**：竖屏避让平台 UI(360)、横屏/方形贴底
    (下留约一个字高=字号)。见 _default_margin_v。"""

    def test_portrait_keeps_high(self):
        self.assertEqual(subtitle._default_margin_v(1080, 1920), 360)   # 9:16 避让平台 UI 区

    def test_landscape_hugs_bottom(self):
        mv = subtitle._default_margin_v(1920, 1080)                     # 16:9，默认字号 58
        self.assertEqual(mv, 58)                                        # 贴底、下留约一个字高
        self.assertLess(mv, 360)                                        # 明显低于竖屏

    def test_square_hugs_bottom(self):
        self.assertEqual(subtitle._default_margin_v(1080, 1080), 58)

    def test_margin_scales_with_font_size(self):
        # 缝隙随画风字号缩放，观感恒为「一个字」
        self.assertEqual(subtitle._default_margin_v(1920, 1080, 62), 62)
        self.assertEqual(subtitle._default_margin_v(1920, 1080, 50), 50)

    def _ass(self, builder, **kw):
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "s.ass")
            builder([(0.0, 2.0, {"narration": "贴底测试"})], out,
                    canvas_w=1920, canvas_h=1080, **kw)
            return Path(out).read_text(encoding="utf-8")

    def test_caption_ass_uses_landscape_margin(self):
        # 左右安全边=一个字宽、底边距=一个字高 → 三个边距都等于横屏缺省字号(66)
        txt = self._ass(subtitle.build_from_timeline, opts={"lang": "zh"})
        self.assertIn(",2,66,66,66,1", txt)       # Alignment=2 底部居中 + 三边距均一个字
        self.assertNotIn(",2,80,80,", txt)        # 横向边恒一个字宽，不得是 80px 定值
        self.assertNotIn("66,66,360,1", txt)      # 横屏不用竖屏的 360

    def test_bubble_narration_uses_landscape_margin(self):
        # 无 speaker 的旁白 → 走底部字幕（game_sim 同款路径）
        txt = self._ass(subtitle.build_bubble, opts={"mode": "bubble"})
        self.assertIn(",2,66,66,66,1", txt)
        self.assertNotIn(",2,80,80,", txt)

    def test_explicit_margin_respected(self):
        txt = self._ass(subtitle.build_from_timeline, opts={"margin_v": 200})
        self.assertIn(",2,66,66,200,1", txt)      # 显式 margin_v 不被覆盖（横向仍一个字）


class TestAdaptiveWrap(unittest.TestCase):
    """横竖屏自适应换行：每行字数随画布宽 + 字号推导（竖屏 16、横屏 31）。
    「文字真的超出画面宽度（左右各留一字）才换行」，横竖屏不一刀切。"""

    def test_cjk_max_chars_portrait_equals_16(self):
        # 竖屏 1080 宽、字号 58 → (1080-116)//58 = 16
        self.assertEqual(subtitle._cjk_max_chars(1080, 58), 16)

    def test_cjk_max_chars_landscape_wider(self):
        # 横屏 1920 宽、字号 58 → 31（近竖屏两倍，不过早换行）
        self.assertEqual(subtitle._cjk_max_chars(1920, 58), 31)
        self.assertGreater(subtitle._cjk_max_chars(1920, 58),
                           subtitle._cjk_max_chars(1080, 58))

    def test_max_chars_scales_with_size(self):
        # 字号更大 → 每行容得下的字更少
        self.assertGreater(subtitle._cjk_max_chars(1920, 50),
                           subtitle._cjk_max_chars(1920, 62))

    def test_max_chars_has_floor(self):
        self.assertGreaterEqual(subtitle._cjk_max_chars(100, 58), 8)

    def test_latin_matches_legacy_on_portrait(self):
        # 拉丁上限竖屏恒 34（2×16+2），横屏才放宽
        self.assertEqual(subtitle._latin_max_chars(1080, 58), 34)
        self.assertGreater(subtitle._latin_max_chars(1920, 58), 34)

    def _line(self, text, canvas_w, canvas_h):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.ass"
            subtitle.build_from_timeline([(0.0, 3.0, {"narration": text})], out,
                                         canvas_w=canvas_w, canvas_h=canvas_h,
                                         opts={"lang": "zh"})
            return next(l for l in out.read_text(encoding="utf-8").splitlines()
                        if l.startswith("Dialogue:"))

    def test_landscape_keeps_long_line_single(self):
        # 20 字：竖屏(≤16)换行、横屏(≤31)保持单行——同一文案横竖屏排布不同
        text = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸"          # 20 字
        self.assertEqual(len(text), 20)
        self.assertNotIn("\\N", self._line(text, 1920, 1080))       # 横屏单行
        self.assertIn("\\N", self._line(text, 1080, 1920))          # 竖屏换行

    def test_portrait_wraps_at_17(self):
        # 竖屏 17 字换行（与旧 16 上限行为一致）
        text = "一二三四五六七八九十甲乙丙丁戊己庚"                # 17 字
        self.assertIn("\\N", self._line(text, 1080, 1920))

    def test_corner_note_smaller_bottom_left(self):
        # 左下角「特殊字幕」预留：字小一号(0.8×)+左下对齐(\an1)，与主字幕分两条事件
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.ass"
            subtitle.build_from_timeline(
                [(0.0, 2.0, {"narration": "主字幕", "corner_note": "第三章·深夜"})],
                out, canvas_w=1920, canvas_h=1080, opts={"lang": "zh"})
            txt = out.read_text(encoding="utf-8")
        self.assertIn("第三章·深夜", txt)
        self.assertIn("\\an1", txt)                # 左下对齐
        # 0.8× 主字号——跟着横屏缺省走，别把基准值抄成字面量
        self.assertIn(f"\\fs{round(subtitle.LANDSCAPE_SIZE * 0.8)}", txt)
        self.assertEqual(txt.count("Dialogue:"), 2)    # 主字幕 + 角标两条

    def test_corner_note_absent_no_change(self):
        # 缺 corner_note → 只有主字幕一条，零影响
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.ass"
            subtitle.build_from_timeline([(0.0, 2.0, {"narration": "主字幕"})],
                                         out, canvas_w=1920, canvas_h=1080)
            txt = out.read_text(encoding="utf-8")
        self.assertNotIn("\\an1", txt)
        self.assertEqual(txt.count("Dialogue:"), 1)


if __name__ == "__main__":
    unittest.main()


class TestDefaultSize(unittest.TestCase):
    """底部字幕缺省字号——三档画布分治：竖屏 80（单行仅约 16 字、手机竖持观感）、
    横屏 66、方形 58 基准。作者在章节 `subtitle` 块写的字号恒优先。

    横屏那一档是**画布宽度修正**不是审美偏好：9:16 与 1:1 画布都是 1080 宽，
    16:9 却是 1920 宽，基准里那句「1080 宽基准」从来没适用于横屏。
    见 `default_size` / `resolve_size`。"""

    def test_portrait_defaults_large(self):
        self.assertEqual(subtitle.default_size(1080, 1920), 80)

    def test_landscape_bumps_square_keeps_base(self):
        self.assertEqual(subtitle.default_size(1920, 1080), 66, "横屏画布 1920 宽")
        self.assertEqual(subtitle.default_size(1080, 1080), 58, "方形画布 1080 宽，无需修正")

    def _ass(self, canvas_w, canvas_h, opts):
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "s.ass")
            subtitle.build_from_timeline(
                [(0.0, 2.0, {"narration": "字号测试"})], out,
                canvas_w=canvas_w, canvas_h=canvas_h, opts=opts)
            return Path(out).read_text(encoding="utf-8")

    def test_portrait_ass_burns_80(self):
        txt = self._ass(1080, 1920, {"lang": "zh"})
        self.assertRegex(txt, r"Style: Default,[^,]+,80,")

    def test_landscape_ass_burns_66(self):
        txt = self._ass(1920, 1080, {"lang": "zh"})
        self.assertRegex(txt, r"Style: Default,[^,]+,66,")

    def test_explicit_size_wins_on_portrait(self):
        txt = self._ass(1080, 1920, {"lang": "zh", "size": 62})
        self.assertRegex(txt, r"Style: Default,[^,]+,62,")


class TestLandscapeSizeOverridesProfile(unittest.TestCase):
    """横屏字号修正对**画风字号是硬覆盖、对作者字号绝不覆盖**。

    44 个 profile 里 28 个自己钉了 `subtitle.size`（52~62），全是照 1080 宽基准写的；
    16:9 画布 1920 宽，照搬下来一律偏小。作者没在章节 `subtitle` 块表态时按画布缺省
    抬到 LANDSCAPE_SIZE，表了态就一个字不动。竖屏与方屏画布本就是 1080 宽，
    画风字号照旧逐个生效。"""

    def _ass(self, canvas_w, canvas_h, opts):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.ass"
            subtitle.build_from_timeline([(0.0, 2.0, {"narration": "字号"})], str(out),
                                         canvas_w=canvas_w, canvas_h=canvas_h, opts=opts)
            return out.read_text(encoding="utf-8")

    def _profile_cfg(self, size):
        """`sub_cfg` 合并后的形态：画风给了字号、章节没表态。"""
        return {"lang": "zh", "size": size, subtitle.PROFILE_SIZE_KEY: True}

    def test_landscape_overrides_a_profile_size(self):
        self.assertEqual(subtitle.resolve_size(self._profile_cfg(52), 1920, 1080), 66)
        self.assertRegex(self._ass(1920, 1080, self._profile_cfg(52)),
                         r"Style: Default,[^,]+,66,")

    def test_portrait_and_square_keep_the_profile_size(self):
        """这两档画布就是 1080 宽，没有要修正的东西——画风差异必须留住。"""
        self.assertEqual(subtitle.resolve_size(self._profile_cfg(52), 1080, 1920), 52)
        self.assertEqual(subtitle.resolve_size(self._profile_cfg(52), 1080, 1080), 52)

    def test_author_size_wins_everywhere(self):
        """章节 `subtitle.size` 是明确表态，横屏也不许动它。"""
        authored = {"lang": "zh", "size": 52}          # 无 PROFILE_SIZE_KEY = 作者写的
        for w, h in ((1920, 1080), (1080, 1920), (1080, 1080)):
            self.assertEqual(subtitle.resolve_size(authored, w, h), 52, f"{w}x{h}")
        self.assertRegex(self._ass(1920, 1080, authored), r"Style: Default,[^,]+,52,")

    def test_direct_callers_without_the_marker_are_treated_as_explicit(self):
        """内部直调/测试传的 `opts={"size": …}` 不带出处标记——那是明确指定，
        横屏不许改写它，否则这个函数就成了「有时听你的有时不听」。"""
        self.assertEqual(subtitle.resolve_size({"size": 70}, 1920, 1080), 70)

    def test_studio_panel_shows_the_burned_value(self):
        """面板生效值与真烧出来的必须同源——不然用户照着面板上那个数去调，
        调的是另一个东西。"""
        import inspect

        from kinema.studio import scanner
        src = inspect.getsource(scanner._subtitle_style_view)
        self.assertIn("resolve_size(cfg, w, hgt)", src)


class TestSpeechSyncedEvents(unittest.TestCase):
    """字幕落点跟随片段音轨里实测的有声段落。

    native 的人声由视频模型生成：`dur` 是计费秒数，`lines[].dur` 仅在跑过 TTS 后
    有值，两者都不表达开口时刻。缺这个事实，字幕只能铺满整个镜窗口。
    """

    def test_single_line_shot_narrows_to_the_spoken_window(self):
        shot = {"id": 1, "narration": "左引擎报废了。", "speaker": "奚岚"}
        (a, b, main, _sub, _spk), = subtitle.shot_events(
            shot, 10.0, 15.0, "zh", spans=[(3.0, 4.4)])
        self.assertEqual(main, "左引擎报废了。")
        self.assertGreater(a, 12.0, "字幕仍从镜窗口起点开始")
        self.assertLessEqual(b, 15.0)

    def test_falls_back_to_the_whole_shot_without_spans(self):
        # 无实测段落时逐字节保持既有行为：探测失败不得导致字幕缺失
        shot = {"id": 1, "narration": "左引擎报废了。"}
        self.assertEqual(subtitle.shot_events(shot, 10.0, 15.0, "zh"),
                         [(10.0, 15.0, "左引擎报废了。", "", "")])

    def test_each_line_takes_its_own_measured_span(self):
        shot = {"id": 2, "lines": [{"speaker": "甲", "text": "你到底想干什么？"},
                                   {"speaker": "乙", "text": "我只想要真相。"}]}
        ev = subtitle.shot_events(shot, 0.0, 10.0, "zh",
                                  spans=[(1.0, 3.0), (6.0, 8.5)])
        self.assertEqual([e[4] for e in ev], ["甲", "乙"])
        self.assertEqual((ev[0][0], ev[0][1]), (1.0, 3.0))
        self.assertEqual((ev[1][0], ev[1][1]), (6.0, 8.5))

    def test_span_count_mismatch_uses_the_overall_window_only(self):
        """段数与句数不一致时不逐句对位：此时对应关系不成立，误差大于铺满窗口。"""
        shot = {"id": 3, "lines": [{"speaker": "甲", "text": "一"},
                                   {"speaker": "乙", "text": "二"}]}
        ev = subtitle.shot_events(shot, 0.0, 10.0, "zh", spans=[(2.0, 7.0)])
        self.assertEqual(len(ev), 2)
        self.assertGreaterEqual(ev[0][0], 2.0)
        self.assertLessEqual(ev[-1][1], 7.0)

    def test_short_utterance_is_padded_to_stay_readable(self):
        """实测有声段是说话时长而非阅读时长，短句需补足到可读。"""
        shot = {"id": 4, "narration": "灰隼七号，归队。"}
        (a, b, _m, _s, _k), = subtitle.shot_events(
            shot, 0.0, 8.0, "zh", spans=[(1.0, 1.3)])
        self.assertGreaterEqual(round(b - a, 2), subtitle.MIN_EVENT_SEC)

    def test_padding_never_escapes_the_shot_window(self):
        shot = {"id": 5, "narration": "很长的一句台词需要读很久才读得完呢"}
        (a, b, _m, _s, _k), = subtitle.shot_events(
            shot, 4.0, 4.8, "zh", spans=[(0.1, 0.3)])
        self.assertGreaterEqual(a, 4.0)
        self.assertLessEqual(b, 4.8)


class TestSpeechSpanContract(unittest.TestCase):
    """按段直取分支的两条前置：事件不许重叠、段与句的对应关系要有依据。"""

    LINES = [{"speaker": "甲", "text": "走。"}, {"speaker": "乙", "text": "好。"}]

    def test_events_never_overlap(self):
        """两句都短于可读下限、且实测段落挨得近时，各补各的会让两条字幕同屏。"""
        ev = subtitle.shot_events({"id": 1, "lines": self.LINES}, 0.0, 10.0, "zh",
                                  spans=[(2.0, 2.4), (2.6, 3.0)])
        self.assertEqual(len(ev), 2)
        self.assertLessEqual(ev[0][1], ev[1][0])
        for a, b in ((ev[0][0], ev[0][1]), (ev[1][0], ev[1][1])):
            self.assertGreaterEqual(round(b - a, 2), subtitle.MIN_EVENT_SEC)

    def test_events_stay_inside_the_shot_window(self):
        ev = subtitle.shot_events({"id": 1, "lines": self.LINES}, 4.0, 6.0, "zh",
                                  spans=[(0.1, 0.3), (1.5, 1.7)])
        self.assertGreaterEqual(ev[0][0], 4.0)
        self.assertLessEqual(ev[-1][1], 6.0)


class TestSpeechWindows(unittest.TestCase):
    """`speech_windows` 只回答「哪几处在出声」，不回答「哪一段对哪一句」。

    段界是能量边界、与句界无因果关系：句中换气会把一句切成多段，低音量的句子
    整句检不出。故它既不裁段（整句的真实起止只有全量段落能给出，裁掉一段，
    调用方收首尾时就会把被裁段的正身留在字幕窗口之外），也不回报对位资格。
    """

    def test_returns_pairs(self):
        self.assertEqual(speech.speech_windows(fake_path("missing.mp4"), 5.0), [])

    def test_zero_duration_is_not_probed(self):
        self.assertEqual(speech.speech_windows(fake_path("missing.mp4"), 0.0), [])

    def test_does_not_answer_line_correspondence(self):
        """不收句数、不回报「段数是否等于句数」：计数相等是巧合，不是对位依据。
        逐句落点的真源是 asr.line_windows。"""
        import inspect
        sig = inspect.signature(speech.speech_windows)
        self.assertNotIn("want", sig.parameters)
        src = inspect.getsource(speech.speech_windows)
        self.assertNotIn("[:want]", src)

    def test_clean_profile_floor_sits_between_speech_body_and_noise(self):
        """干净 TTS wav 的峰均差远大于模型片段（未压缩语音的峰只在爆破音上）：
        按峰下探 8 dB 会把语句主体整段判成静音。干净档下探 25 dB 且钳位放深，
        阈值须落在语句体（-20~-30 dB）之下、数字底噪（<-50 dB）之上。"""
        self.assertEqual(speech.RELATIVE_FLOOR_DB_CLEAN, 25.0)
        self.assertLess(speech.FLOOR_MAX_DB_CLEAN, -12.0,
                        "干净档钳位若与模型片段档同层，TTS 语句体照样被判静音")
        self.assertGreater(speech.FLOOR_MIN_DB_CLEAN, -55.0)

    def test_compose_probes_tts_with_the_clean_profile(self):
        """探测档位与该镜的声源同源：烧录承担的镜探逐镜 TTS（干净档），
        模型发声的镜探片段音轨（含音效底床）——两档参数互换任一侧都整段误判。
        判据是逐镜的 `burned`（混烧章旁白镜烧录、对白镜模型发声），不是章级。"""
        import inspect

        from kinema.pipeline import compose
        src = inspect.getsource(compose.speech_spans_resolver)
        self.assertIn("clean=burned", src)
        self.assertIn("voicecast.voice_kind(shot)", src,
                      "烧录与否必须按镜判（混烧章的旁白镜与对白镜声源不同）")


class TestRankingFollowsSpeech(unittest.TestCase):
    """榜单：说明文本跟实测语音，徽章/序号/标题恒铺满整镜。

    前者是这一镜念出来的那句话，后者交代「这是第几名」，是常驻叠加层。
    """

    def _events(self, spans):
        import tempfile
        out = Path(tempfile.mkdtemp()) / "r.ass"
        subtitle.build_ranking(
            [(0.0, 5.0, {"id": 1, "rank": "1", "title": "甲",
                         "narration": "第一名是甲。"})],
            out, canvas_w=1920, canvas_h=1080,
            spans_of=(lambda s: spans) if spans else None)
        rows = [ln for ln in out.read_text(encoding="utf-8").splitlines()
                if ln.startswith("Dialogue")]
        return {ln.split(",")[3]: (ln.split(",")[1], ln.split(",")[2]) for ln in rows}

    def test_text_follows_the_spoken_window(self):
        ev = self._events([(3.0, 4.0)])
        self.assertEqual(ev["RText"][0], "0:00:03.00")
        for style in ("RBadge", "RNum", "RTitle"):
            self.assertEqual(ev[style], ("0:00:00.00", "0:00:05.00"), style)

    def test_without_spans_text_spans_the_shot(self):
        self.assertEqual(self._events(None)["RText"], ("0:00:00.00", "0:00:05.00"))
