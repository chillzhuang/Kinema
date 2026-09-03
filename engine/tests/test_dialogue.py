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

"""镜内多段台词 `shots[].lines[]` —— 一个镜头里两个人对话的守卫。

若镜在全链路上是**原子**的（每镜一次 synthesize、每镜一条字幕事件），把两人对话
写进同一个 narration 时只能整段一把声音念完——明明是两个人对话，成片却只有一个
角色的配音，很出戏。

本文件钉死三件事：① 句序列归一化（继承/丢空段/回落）；② 逐句各自解析音色并
逐句合成，整镜 wav 仍是唯一对外产物；③ 字幕逐句切换、说话人跟着换。
外加**回落态**——没写 lines 的镜仍按整段 narration 一次合成、一条字幕事件。
"""
from __future__ import annotations

import unittest

from kinema import voicecast
from kinema.pipeline import subtitle
from kinema.project import Project
from tests.support import fake_path


class _Store:
    """最小 store 替身：音色别名 → voice_type（真 ConfigStore 的同名出口）。"""

    TABLE = {"少年": "vt_shaonian", "温柔长辈": "vt_wenrou", "Vivi": "vt_vivi",
             "默认": "vt_default"}

    def resolve_voice(self, ref):
        return self.TABLE.get(ref, ref)


def _project(**over):
    data = {"motion": "kenburns", "voices": {"林深": "少年", "陆昭": "温柔长辈",
                                             "旁白": "Vivi"}}
    data.update(over)
    return Project("p.json", data)


DIALOGUE_SHOT = {
    "id": 2, "dur": 8, "emotion": "calm",
    "lines": [
        {"speaker": "林深", "text": "你到底想干什么？", "emotion": "angry"},
        {"speaker": "旁白", "text": "他没有回答。"},
        {"speaker": "陆昭", "text": "我只想要真相。"},
    ],
}


class TestLineNormalisation(unittest.TestCase):
    """`shot_lines` 是「镜 → 句序列」的唯一入口，下游只认识「句」这一种粒度。"""

    def test_single_narration_shot_falls_back_to_one_line(self):
        """没写 lines 的老镜回落成单段——全链路零迁移的地基。"""
        got = voicecast.shot_lines({"id": 1, "narration": "旁白一句", "speaker": "旁白"})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "旁白一句")
        self.assertEqual(got[0]["speaker"], "旁白")
        self.assertEqual(voicecast.shot_lines({"id": 1}), [], "纯画面镜没有句")

    def test_lines_inherit_shot_level_defaults(self):
        """句没写 speaker/emotion 就继承镜级——多数镜是「一个人说 + 偶尔插旁白」，
        逼每句都抄一遍只会抄错。"""
        got = voicecast.shot_lines(DIALOGUE_SHOT)
        self.assertEqual([x["speaker"] for x in got], ["林深", "旁白", "陆昭"])
        self.assertEqual(got[0]["emotion"], "angry", "句级显式值优先")
        self.assertEqual(got[1]["emotion"], "calm", "句没写 → 继承镜级")

    def test_blank_lines_are_dropped_not_synthesised(self):
        """作者留的空行不该变成一次计费合成，且序号要连续（分句 wav 命名靠它）。"""
        got = voicecast.shot_lines({"id": 3, "lines": [
            {"text": "第一句"}, {"text": "   "}, {"text": ""}, {"text": "第二句"}]})
        self.assertEqual([x["text"] for x in got], ["第一句", "第二句"])
        self.assertEqual([x["i"] for x in got], [0, 1])

    def test_shot_text_is_the_has_speech_predicate(self):
        """「这镜有没有话要说」的统一判据——读 narration 的老代码换用它即可
        同时认识 lines[]。漏掉任何一处，多角色镜就会被当成纯画面镜。"""
        self.assertTrue(voicecast.shot_text(DIALOGUE_SHOT))
        self.assertIn("我只想要真相", voicecast.shot_text(DIALOGUE_SHOT))
        self.assertFalse(voicecast.shot_text({"id": 9}))

    def test_normalised_line_is_shaped_like_a_shot(self):
        """句与镜**同形**（emotion/emotion_scale/voice_instruction/delivery 四件套），
        所以表现力那套实现能被直接复用、绝不为多角色再写第二份。"""
        ln = voicecast.shot_lines({"id": 4, "voice_instruction": "压低声音",
                                   "lines": [{"text": "一句", "emotion": "sad"}]})[0]
        self.assertEqual(voicecast.shot_expressive_params(ln), {"emotion": "sad"})
        self.assertEqual(voicecast.delivery_instruction(ln), "压低声音")


class TestNarratorPredicate(unittest.TestCase):
    """「这句是不是旁白」的单一真源。判据两半缺一不可，各写一份就会分叉：
    漏「没点名」那半会让提示词把作者漏填的旁白句编成没有主语的「说：“…”」并要求
    为第三人称叙述配口型；漏 `.lower()` 会让 lint 把 `speaker: "VO"` 报成角色对白。"""

    def test_named_aliases_are_case_insensitive(self):
        for spk in ("旁白", "画外音", "vo", "VO", "Narrator", "VoiceOver"):
            self.assertTrue(voicecast.is_narrator(spk), spk)

    def test_an_unnamed_line_is_narration(self):
        for spk in (None, "", "   "):
            self.assertTrue(voicecast.is_narrator(spk), repr(spk))

    def test_a_named_character_is_not(self):
        self.assertFalse(voicecast.is_narrator("林深"))

    def test_every_consumer_goes_through_it(self):
        """全链八处消费点共用这一份——漏抄一处就是一次静默分叉。"""
        import inspect
        import pathlib

        from kinema import voicebank
        from kinema.pipeline import prompts, variation
        root = pathlib.Path(voicecast.__file__).parent
        for mod in (voicebank, prompts, variation):
            src = inspect.getsource(mod)
            self.assertNotIn("NARRATOR_NAMES", src,
                             f"{mod.__name__} 又抄了一份旁白判据，改用 voicecast.is_narrator")
        # voicecast 自身只在 is_narrator 里读这张表
        own = (root / "voicecast.py").read_text(encoding="utf-8")
        self.assertEqual(own.count("in NARRATOR_NAMES"), 1)

    def test_scored_lint_reads_english_aliases_as_narration(self):
        """`scored_native_dialogue` 判据漏 `.lower()` 时，`speaker: "VO"` 的旁白镜
        会被报成角色对白，照 hint 改反而把正确配置改坏。"""
        from kinema.pipeline import variation as vr
        doc = {"motion": "native", "audio_mode": "scored",
               "shots": [{"id": 1, "dur": 4.0, "speaker": "VO",
                          "narration": "那一晚风很大。"}]}
        hits = [f for f in vr.lint(doc) if f.code == "scored_native_dialogue"]
        self.assertEqual(hits, [], "旁白镜不该被报成角色对白")


class TestPerLineCasting(unittest.TestCase):
    """逐句各自一把声音——整镜共用一把会让两个人的对话只剩一个角色的配音。"""

    def setUp(self):
        self.pr, self.st = _project(), _Store()

    def test_each_line_resolves_its_own_voice(self):
        voices = [voicecast.resolve_line_voice(self.pr, self.st, DIALOGUE_SHOT, ln, "默认")[1]
                  for ln in voicecast.shot_lines(DIALOGUE_SHOT)]
        self.assertEqual(voices, ["vt_shaonian", "vt_vivi", "vt_wenrou"])
        self.assertEqual(len(set(voices)), 3, "三个说话人必须三把声音")

    def test_line_voice_priority_matches_the_shot_level_chain(self):
        """句级优先级与镜级同构：lines[].voice > voices[speaker] > 镜级 > 兜底。
        两条链一旦各写一份，试音选定的音色会在多角色镜上悄悄失效。"""
        shot = {"id": 5, "speaker": "林深", "voice": "Vivi",
                "lines": [{"text": "甲"},                                   # 继承镜级 voice
                          {"speaker": "陆昭", "text": "乙"},                 # 查音色表
                          {"speaker": "陆昭", "text": "丙", "voice": "少年"}]}  # 句级显式最高
        got = [voicecast.resolve_line_voice(self.pr, self.st, shot, ln, "默认")[1]
               for ln in voicecast.shot_lines(shot)]
        self.assertEqual(got, ["vt_vivi", "vt_wenrou", "vt_shaonian"])

    def test_single_line_shot_matches_resolve_shot_voice_exactly(self):
        """回落态：单段镜逐句解析的结果必须与镜级 resolve_shot_voice 完全一致。"""
        shot = {"id": 6, "speaker": "林深", "narration": "一句话"}
        old = voicecast.resolve_shot_voice(self.pr, self.st, shot, "默认")
        new = voicecast.resolve_line_voice(self.pr, self.st, shot,
                                           voicecast.shot_lines(shot)[0], "默认")
        self.assertEqual(old, new)

    def test_multi_voice_predicate_counts_voices_not_lines(self):
        """「两个人对话」的判据是**声音真的不同**，不是写了几段——
        同一个人连说三句不算多角色。"""
        same = {"id": 7, "speaker": "林深",
                "lines": [{"text": "甲"}, {"text": "乙"}, {"text": "丙"}]}
        self.assertFalse(voicecast.is_multi_voice(self.pr, self.st, same, "默认"))
        self.assertTrue(voicecast.is_multi_voice(self.pr, self.st, DIALOGUE_SHOT, "默认"))


class TestLinePauseGate(unittest.TestCase):
    """句间停顿与镜级同一道模式门控——不给新的计费陷阱开口子。"""

    def test_pauses_only_apply_in_local_render_modes(self):
        ln = voicecast.shot_lines({"id": 8, "lines": [
            {"text": "甲", "delivery": {"pause_before": 0.3, "pause_after": 0.5}}]})[0]
        self.assertEqual(voicecast.line_pauses(ln, "kenburns"), (0.3, 0.5))
        self.assertEqual(voicecast.line_pauses(ln, "kenburns"), (0.3, 0.5))
        # dubbed/native 的 dur 要向 Seedance 按秒付费（ceil 取整），
        # 插停顿等于每段对白无效购买一截无声
        self.assertEqual(voicecast.line_pauses(ln, "dubbed"), (0.0, 0.0))
        self.assertEqual(voicecast.line_pauses(ln, "native"), (0.0, 0.0))
        self.assertEqual(voicecast.line_pauses(ln, "kenburns"),
                         voicecast.shot_pauses({"delivery": ln["delivery"]}, "kenburns"),
                         "句级与镜级必须同一道闸、同一个上限")


class TestSubtitleFollowsTheVoice(unittest.TestCase):
    """字幕逐句切换——声音换了人字幕还停在上一句，比不换声音更出戏。"""

    def test_multi_line_shot_yields_one_event_per_line(self):
        s = dict(DIALOGUE_SHOT)
        for ln, d in zip(s["lines"], (1.4, 1.5, 1.2)):
            ln["dur"] = d
        ev = subtitle.shot_events(s, 10.0, 14.1, "zh")
        self.assertEqual(len(ev), 3)
        self.assertEqual([e[4] for e in ev], ["林深", "旁白", "陆昭"], "说话人要跟着换")
        self.assertAlmostEqual(ev[0][0], 10.0, places=2)
        self.assertAlmostEqual(ev[-1][1], 14.1, places=2, msg="末句必须收在镜窗口末尾")
        for a, b in zip(ev, ev[1:]):                    # 首尾相接、不重叠不留缝
            self.assertAlmostEqual(a[1], b[0], places=2)

    def test_events_follow_the_same_split_the_prompt_timeline_uses(self):
        """没跑 tts（lines[].dur 缺失）时按字数比例切，且与 voicecast.line_spans
        逐位相等——提示词把第几秒交给谁，字幕就在第几秒换人。"""
        ev = subtitle.shot_events(DIALOGUE_SHOT, 0.0, 9.0, "zh")
        self.assertEqual(len(ev), 3)
        want = voicecast.line_spans(voicecast.shot_lines(DIALOGUE_SHOT), 9.0)
        for e, (_ln, a, b) in zip(ev, want):
            self.assertAlmostEqual(e[0], a, places=2)
            self.assertAlmostEqual(e[1], b, places=2)

    def test_split_weight_ignores_voice_tags(self):
        """`<cot>` 是给 TTS 的标签、念不出来，不该在字幕上占时间：
        标签句与等长裸句拿到同样的窗口。"""
        tagged = {"id": 9, "lines": [{"speaker": "A", "text": "<cot text=急促>快跑</cot>"},
                                     {"speaker": "B", "text": "快跑"}]}
        ev = subtitle.shot_events(tagged, 0.0, 4.0, "zh")
        self.assertAlmostEqual(ev[0][1] - ev[0][0], ev[1][1] - ev[1][0], places=2)

    def test_zero_length_window_still_emits_one_event_per_line(self):
        """dur 缺失导致窗口塌成零长时仍逐句出事件：条数是 verify 字幕体检的下限。"""
        ev = subtitle.shot_events(DIALOGUE_SHOT, 5.0, 5.0, "zh")
        self.assertEqual(len(ev), 3)

    def test_single_line_shot_is_byte_identical_to_the_old_path(self):
        """回落态：无 lines 的镜恒一条事件、起止即整镜窗口、
        文本口径仍是 pick_texts（含 caption 补位）。"""
        shot = {"id": 1, "narration": "旁白一句", "speaker": "旁白"}
        self.assertEqual(subtitle.shot_events(shot, 2.0, 5.0, "zh"),
                         [(2.0, 5.0, "旁白一句", "", "旁白")])
        cap = {"id": 2, "caption": "纯画面补位"}
        self.assertEqual(subtitle.shot_events(cap, 0.0, 1.0, "zh")[0][2], "纯画面补位")
        self.assertEqual(subtitle.shot_events({"id": 3}, 0.0, 1.0, "zh"), [],
                         "无台词无字幕镜不产事件")

    def test_bilingual_takes_the_per_line_english(self):
        s = {"id": 4, "lines": [{"speaker": "A", "text": "中文甲", "text_en": "EN A"},
                                {"speaker": "B", "text": "中文乙"}]}
        ev = subtitle.shot_events(s, 0.0, 2.0, "both")
        self.assertEqual((ev[0][2], ev[0][3]), ("中文甲", "EN A"))
        self.assertEqual(ev[1][3], "", "没写英文位就留空，不拿中文冒充")
        en = subtitle.shot_events(s, 0.0, 2.0, "en")
        self.assertEqual(en[0][2], "EN A")
        self.assertEqual(en[1][2], "中文乙", "en 缺英文回落中文，不留空窗")

    def test_expand_timeline_flattens_for_performance_layouts(self):
        """对话框/气泡/居中三个版式靠展开后的时间轴换名牌——
        不展开的话三句话共用一个名牌，画面上永远是同一个人在说。"""
        tl = [(0.0, 3.0, {"id": 1, "narration": "单段", "speaker": "旁白"}),
              (3.0, 9.0, DIALOGUE_SHOT)]
        out = subtitle.expand_timeline(tl)
        self.assertEqual(len(out), 4, "1 + 3")
        self.assertIs(out[0][2], tl[0][2], "单段镜必须原样透传（同一个对象·零拷贝）")
        self.assertEqual([v["speaker"] for _a, _b, v in out[1:]], ["林深", "旁白", "陆昭"])
        for _a, _b, v in out[1:]:
            self.assertIsNone(v["lines"], "展开后的视图要置空 lines 防二次展开")
            self.assertEqual(v["narration"], v["dialogue"],
                             "两个文本位都覆写——各版式取哪个都拿得到本句")


class TestLintSeesPerLineEmotion(unittest.TestCase):
    """体检要认识逐句标注——否则会催作者去补一个他已经写得更细的字段。"""

    def test_emotion_written_per_line_counts_as_written(self):
        from kinema.pipeline import variation
        shots = [
            {"id": 1, "narration": "没情绪的独白"},                       # 真的没写
            {"id": 2, "lines": [{"speaker": "A", "text": "甲", "emotion": "angry"},
                                {"speaker": "B", "text": "乙"}]},          # 逐句写了
        ]
        out = variation._lint_emotion(shots, {}, {"motion": "kenburns"})
        miss = [f for f in out if f.code == "emotion_missing"]
        self.assertTrue(miss, "镜 1 确实没写，应当报")
        self.assertEqual(tuple(miss[0].shots), (1,),
                         "镜 2 逐句写了 emotion，不该被算成白开水")


class TestPipelineWiring(unittest.TestCase):
    """源级接线：多角色镜绝不能在任何一处被当成「没有台词」。"""

    def _src(self, mod):
        import inspect
        return inspect.getsource(mod)

    def test_speech_predicates_all_go_through_shot_text(self):
        """「这镜有没有话要说」的判据统一走 shot_text——漏一处，
        多角色镜就会在那一处被当成纯画面镜（插静音/不查审阅/不算音频）。"""
        from kinema import cli
        from kinema.pipeline import mediacheck, variation
        from kinema.studio import scanner
        for mod in (cli, mediacheck, variation, scanner, voicecast):
            src = self._src(mod)
            self.assertNotIn('(s.get("narration") or "").strip()', src,
                             f"{mod.__name__} 仍在直接判 narration，看不见 lines[]")

    def test_narration_track_uses_the_line_aware_predicate(self):
        """旁白轨拼接是后果最重的一处：有 lines 无 narration 若被当成纯画面镜，
        整镜配音会被替换成等长静音——音轨直接废掉，且不报任何错。"""
        src = self._src(voicecast)
        self.assertIn("text = shot_text(s)", src)

    def test_native_voice_clause_reads_lines_not_raw_narration(self):
        """`prompts` 若裸读 narration，只写 lines[] 的镜在 native 提示词里会
        退化成无台词镜——Seedance 收不到该说什么，且全程无报错。

        这里走行为断言而非源级 grep：`prompts` 另有一处合法的
        `shot.get("narration")`（image_prompt 缺笔时的画面兜底），
        源级字面量拦不住其一还会误伤其二。"""
        from kinema.pipeline import prompts
        clause = prompts.native_voice_clause(
            {"id": 1, "lines": [{"speaker": "林深", "text": "走。"}]})
        self.assertIn("林深", clause)
        self.assertIn("走。", clause)
        full = prompts.video_prompt(
            {"id": 1, "dur": 5, "video_prompt": "推近。",
             "lines": [{"speaker": "林深", "text": "走。"}]}, native=True)
        self.assertIn("林深说：“走。”", full, "整条 native 提示词必须带上这句台词")

    def test_narration_only_shot_wraps_into_the_same_three_forms(self):
        """没写 lines[] 的镜按 narration 单段包装，三种语态各自的完整形态。

        旁白那一支同样要带上原文——`voice_kind` 判成旁白只决定「不做口型」，
        不决定「不告诉模型念什么」。"""
        from kinema.pipeline import prompts
        self.assertEqual(
            prompts.native_voice_clause(
                {"speaker": "林深", "emotion": "angry", "narration": "你到底想干什么？"}),
            "林深嘶声怒喝道：“你到底想干什么？”，口型与台词同步。")
        self.assertEqual(
            prompts.native_voice_clause({"speaker": "旁白", "narration": "少年抬头。"}),
            f"画外旁白讲述：“少年抬头。”，{prompts.NARRATION_LIPS_ZH}。")
        self.assertEqual(
            prompts.native_voice_clause({"id": 9}),
            f"本镜无台词，{prompts.NARRATION_LIPS_ZH}，"
            f"{prompts.NO_LINE_VOICE_MARK_ZH}。")

    def test_narrator_line_inside_a_dialogue_shot_gets_no_lip_sync(self):
        """`voice_kind` 是整镜口径：对白里插一句旁白时它恒判 dialogue，
        句级不再判一次就会要求模型给第三人称叙述配口型。"""
        from kinema.pipeline import prompts
        clause = prompts.native_voice_clause({"id": 1, "lines": [
            {"speaker": "林深", "text": "你到底想干什么？"},
            {"speaker": "旁白", "text": "他没有回答。"}]})
        self.assertIn("画外旁白讲述：“他没有回答。”", clause)
        self.assertNotIn("旁白说：", clause)
        self.assertTrue(clause.endswith("画外旁白那句不做口型。"))

    def test_unnamed_line_inside_a_dialogue_shot_is_narration_too(self):
        """句级 speaker 留空＝旁白，全链其余六处都这么认。提示词侧漏这半个判据
        就会编出没有主语的「说：“…”」，还要求为一句第三人称叙述配口型。"""
        from kinema.pipeline import prompts
        clause = prompts.native_voice_clause({"id": 1, "lines": [
            {"speaker": "林深", "text": "你到底想干什么？"},
            {"text": "他没有回答。"}]})
        self.assertIn("画外旁白讲述：“他没有回答。”", clause)
        self.assertNotIn("说：“他没有回答。”", clause)
        self.assertTrue(clause.endswith("画外旁白那句不做口型。"))

    def test_unnamed_narration_line_carries_the_narrator_anchor_tag(self):
        """绑定句给画外旁白排了 @配音N，正文里那一句就必须带同一个编号——
        点名了一条正文中无对位的参考音，模型无从对位。"""
        from kinema import voicecast
        from kinema.pipeline import prompts
        clause = prompts.native_voice_clause(
            {"id": 1, "lines": [{"speaker": "林深", "text": "你到底想干什么？"},
                                {"text": "他没有回答。"}]},
            anchors=[{"who": "林深", "voice_type": "v1", "no": 1},
                     {"who": voicecast.NARRATOR_DISPLAY, "voice_type": "v2", "no": 2}])
        self.assertIn("画外旁白 @配音2 讲述：“他没有回答。”", clause)

    def test_shot_wav_stays_the_only_public_artifact(self):
        """分句 wav 只是中间物：对外产物恒是 shot_<id>.wav，
        于是 review 表态/版本栈/dubbed 的 ref_audio/request_seconds 全部零改动。"""
        shot = {"id": 7}
        ln = {"i": 2}
        self.assertTrue(str(voicecast.line_wav(shot, ln, fake_path()))
                        .endswith("shot_7_L2.wav"))
        from kinema import cli
        src = self._src(cli)
        self.assertIn('"wav": voicecast.line_wav(s, ln, adir) if multi else wav', src,
                      "单段镜必须直接写整镜 wav（不落中间文件、不走拼接）")


if __name__ == "__main__":
    unittest.main()
