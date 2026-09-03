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

"""本地 ASR（pipeline/asr）与其两个消费方的守卫。

faster-whisper 是可选依赖，所有用例 mock `transcribe`——测试离线、确定性、
不下模型。判据分三层：
  ① 划界纯函数（line_windows 的配额切分 / 相合闸 / 钳位）；
  ② verify 的人声文字核对节（native_voice_check：目标镜筛选 / 阈值 / 缺件跳过）；
  ③ 合成侧回落链（段句不等 → ASR 划界 → 收整体首尾）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema.pipeline import asr


def _res(words, text=None):
    """构造 transcribe 返回体：words=[(start,end,word)]。"""
    return {"text": text if text is not None else "".join(w for _s, _e, w in words),
            "segments": [], "words": list(words)}


class TestTextMatch(unittest.TestCase):
    def test_punctuation_and_spacing_do_not_count(self):
        self.assertEqual(asr.text_match("缆绳，再检查一遍。", "缆绳再检查一遍"), 1.0)

    def test_different_content_scores_low(self):
        self.assertLess(asr.text_match("缆绳再检查一遍", "今晚吃什么"), 0.3)

    def test_empty_sides_are_zero(self):
        self.assertEqual(asr.text_match("", "有话"), 0.0)
        self.assertEqual(asr.text_match("有话", ""), 0.0)


class TestTranscribeDecoding(unittest.TestCase):
    """中文解码的字形引导：whisper 对普通话默认出繁体，字形差异逐字进比对——
    一句 18 字台词只因繁简召回就从 0.88 掉到 0.59，落进「没按稿念」的假警。"""

    def test_a_transcript_that_only_echoes_the_prompt_is_dropped(self):
        """静音片段上解码器会复述引导句本身。整段落在引导句里就不是音频内容——
        闭声镜的转写本该是空的，留着它会让「听到什么」这一栏说谎。"""

        class _Seg:
            def __init__(self, text):
                self.start, self.end, self.text, self.words = 0.0, 1.0, text, []

        class _M:
            def transcribe(self, path, **kw):
                return [_Seg("请用简体中文转写，")], None

        with mock.patch.object(asr, "_get_model", return_value=_M()):
            self.assertEqual(asr.transcribe("a.mp4")["text"], "")

    def test_vad_silence_is_re_decoded_without_the_filter(self):
        """Silero 对轻声台词整段判负：转写为空会让 verify 报「念出 0%」，
        一段正确的片段被判死、代价是一次按秒计费的重投。判空即无 VAD 复解一次。"""

        class _Seg:
            def __init__(self, text):
                self.start, self.end, self.text, self.words = 0.0, 1.0, text, []

        calls = []

        class _M:
            def transcribe(self, path, **kw):
                calls.append(kw["vad_filter"])
                return ([] if kw["vad_filter"] else [_Seg("不要回答。")]), None

        with mock.patch.object(asr, "_get_model", return_value=_M()):
            self.assertEqual(asr.transcribe("a.mp4")["text"], "不要回答。")
        self.assertEqual(calls, [True, False], "先带 VAD 解一次，判空才复解")

    def test_a_real_take_never_pays_for_a_second_decode(self):
        class _Seg:
            def __init__(self, text):
                self.start, self.end, self.text, self.words = 0.0, 1.0, text, []

        calls = []

        class _M:
            def transcribe(self, path, **kw):
                calls.append(kw["vad_filter"])
                return [_Seg("零七，报数。")], None

        with mock.patch.object(asr, "_get_model", return_value=_M()):
            asr.transcribe("a.mp4")
        self.assertEqual(calls, [True])

    def test_chinese_gets_a_script_prompt(self):
        seen = {}

        class _M:
            def transcribe(self, path, **kw):
                seen.update(kw)
                return [], None

        with mock.patch.object(asr, "_get_model", return_value=_M()):
            asr.transcribe("a.mp4")
            self.assertEqual(seen["initial_prompt"], asr._ZH_STYLE_PROMPT)
            asr.transcribe("a.mp4", lang="en")
            self.assertIsNone(seen["initial_prompt"], "字形引导是中文专有，别喂给英文解码")


class TestTextRecall(unittest.TestCase):
    """核对问的是「这一稿念了多少」，所以分母只有稿面。对称相似度对漏念不敏感：
    转写只有稿子前 43% 时它仍给出 0.6，而长句念一半就转场是模型声源的主要失效形态。"""

    def test_full_read_scores_one(self):
        self.assertEqual(asr.text_recall("缆绳，再检查一遍。", "缆绳再检查一遍"), 1.0)

    def test_half_read_falls_below_the_threshold(self):
        from kinema.pipeline.mediacheck import VOICE_TEXT_RECALL_MIN
        expected = "缆绳再检查一遍，我们今晚就出海。"
        self.assertGreaterEqual(asr.text_match(expected, "缆绳再检查一遍"),
                                VOICE_TEXT_RECALL_MIN, "对称口径会放行这一条")
        self.assertLess(asr.text_recall(expected, "缆绳再检查一遍"),
                        VOICE_TEXT_RECALL_MIN)

    def test_extra_heard_words_do_not_count_against_the_take(self):
        """环境床凑出的幻听字不是「没按稿念」，不进分母。"""
        self.assertEqual(asr.text_recall("走。", "走 呼——风声"), 1.0)

    def test_arabic_digits_fold_onto_the_script(self):
        """字形引导句压不住阿拉伯数字：「零七，报数」会转写成「07 报数」，召回
        掉到 0.5，落进「没按稿念」的假警。折到同一套字形上再比。"""
        self.assertEqual(asr.text_recall("零七，报数。", "07 报数"), 1.0)
        self.assertEqual(asr.text_recall("二〇二六年", "2026年"), 1.0)

    def test_empty_sides_are_zero(self):
        self.assertEqual(asr.text_recall("", "有话"), 0.0)
        self.assertEqual(asr.text_recall("有话", ""), 0.0)


class TestLineWindows(unittest.TestCase):
    LINES = [{"text": "缆绳再检查一遍。"}, {"text": "早就检查过啦！"}]

    def test_partitions_words_by_char_quota(self):
        words = [(0.5, 1.0, "缆绳"), (1.0, 1.8, "再检查"), (1.8, 2.6, "一遍"),
                 (3.4, 4.0, "早就"), (4.0, 4.8, "检查"), (4.8, 5.6, "过啦")]
        with mock.patch.object(asr, "transcribe", return_value=_res(words)):
            spans = asr.line_windows("x.mp4", self.LINES, 6.0)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0], (0.5, 2.6))
        self.assertEqual(spans[1], (3.4, 5.6))

    def test_mismatched_content_refuses_to_align(self):
        """整镜相合度低于闸值时放弃划界——给错误内容标时间就是编造落点。"""
        words = [(0.5, 1.0, "今晚"), (1.0, 1.8, "吃什么"), (2.0, 2.8, "随便")]
        with mock.patch.object(asr, "transcribe", return_value=_res(words)):
            self.assertIsNone(asr.line_windows("x.mp4", self.LINES, 6.0))

    def test_unavailable_asr_returns_none(self):
        with mock.patch.object(asr, "transcribe", return_value=None):
            self.assertIsNone(asr.line_windows("x.mp4", self.LINES, 6.0))

    def test_spans_are_clamped_and_monotonic(self):
        words = [(-0.5, 1.0, "缆绳再检查"), (1.0, 2.0, "一遍"),
                 (2.0, 9.0, "早就检查过啦")]
        with mock.patch.object(asr, "transcribe", return_value=_res(words)):
            spans = asr.line_windows("x.mp4", self.LINES, 6.0)
        self.assertEqual(spans[0][0], 0.0, "词时间戳越界须钳进镜窗口")
        self.assertEqual(spans[1][1], 6.0)
        self.assertLessEqual(spans[0][1], spans[1][0])


class TestNativeVoiceCheck(unittest.TestCase):
    """verify 的人声文字核对节：native 声源的「字幕与人声一致」从待核对
    收成实测结论；混烧章只查对白镜（旁白是烧录 TTS，与字幕同源）。"""

    def _project(self, doc):
        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for s in doc.get("shots", []):
            if s.get("clip") and not str(s["clip"]).startswith("http"):
                c = tmp / s["clip"]
                c.write_bytes(b"clip")
                s["clip"] = str(c)
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project(cf, doc)

    def _check(self, doc, heard_by_shot):
        from kinema.pipeline import mediacheck

        def fake_transcribe(path, **kw):
            return {"text": heard_by_shot.pop(0), "segments": [], "words": []}

        with mock.patch.object(asr, "available", return_value=True), \
             mock.patch.object(asr, "transcribe", side_effect=fake_transcribe):
            return mediacheck.native_voice_check(self._project(doc))

    def test_drift_is_reported_and_match_passes(self):
        doc = {"id": "c", "motion": "native", "aspect": "16:9",
               "shots": [{"id": 1, "dur": 4.0, "speaker": "甲",
                          "narration": "缆绳再检查一遍。", "clip": "s1.mp4"},
                         {"id": 2, "dur": 4.0, "speaker": "乙",
                          "narration": "早就检查过啦！", "clip": "s2.mp4"}]}
        rep = self._check(doc, ["缆绳再检查一遍", "今晚吃什么"])
        self.assertEqual(rep["kind"], "asr")
        self.assertEqual(len(rep["rows"]), 2)
        self.assertGreaterEqual(rep["rows"][0]["score"], 0.9)
        self.assertEqual(len(rep["todo"]), 1)
        self.assertEqual(rep["todo"][0]["code"], "voice_text_drift")
        self.assertIn("镜 2", rep["todo"][0]["msg"])

    def test_a_clip_on_the_cloud_is_pulled_back_not_skipped(self):
        """`oss sync` 把 clip 改写成 URL 而本地文件仍在盘上——裸读字段会让
        跑过上云的章节整章判成「片段不在本地」，报出一个没测过的 0/N。"""
        from kinema.storage import media
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        local = tmp / "s1.mp4"
        local.write_bytes(b"clip")
        doc = {"id": "c", "motion": "native", "aspect": "16:9",
               "shots": [{"id": 1, "dur": 4.0, "speaker": "甲",
                          "narration": "缆绳再检查一遍。",
                          "clip": "https://oss.example.com/c/s1.mp4"}]}
        with mock.patch.object(media, "ensure_local", return_value=str(local)):
            rep = self._check(doc, ["缆绳再检查一遍"])
        self.assertEqual(rep["rows"][0]["score"], 1.0)

    def test_burn_chapter_checks_only_dialogue(self):
        doc = {"id": "c", "motion": "native", "native_voiceover": True,
               "aspect": "16:9",
               "shots": [{"id": 1, "dur": 4.0, "speaker": "甲",
                          "narration": "走。", "clip": "s1.mp4"},
                         {"id": 2, "dur": 4.0, "speaker": "旁白",
                          "narration": "起风了。", "clip": "s2.mp4"}]}
        rep = self._check(doc, ["走"])
        self.assertEqual([r["id"] for r in rep["rows"]], [1],
                         "旁白镜的人声是烧录 TTS，不进 ASR 判据")

    def test_missing_dependency_says_so(self):
        doc = {"id": "c", "motion": "native", "aspect": "16:9",
               "shots": [{"id": 1, "dur": 4.0, "speaker": "甲",
                          "narration": "走。", "clip": "s1.mp4"}]}
        from kinema.pipeline import mediacheck
        with mock.patch.object(asr, "available", return_value=False):
            rep = mediacheck.native_voice_check(self._project(doc))
        self.assertFalse(rep["available"])
        self.assertIn("faster-whisper", rep["note"])

    def test_non_native_chapter_is_out_of_scope(self):
        from kinema.pipeline import mediacheck
        doc = {"id": "c", "motion": "dubbed", "aspect": "16:9",
               "shots": [{"id": 1, "dur": 4.0, "speaker": "甲",
                          "narration": "走。"}]}
        self.assertIsNone(mediacheck.native_voice_check(self._project(doc)))

    def test_a_dropped_line_is_reported_even_when_shot_recall_passes(self):
        """两字句整句漏念只让整镜召回掉 9%，字幕却会按稿面烧出一条无声字幕。"""
        from kinema.pipeline import mediacheck
        doc = {"id": "c", "motion": "native", "aspect": "16:9",
               "shots": [{"id": 1, "dur": 7.0, "clip": "s1.mp4",
                          "lines": [{"speaker": "何姐", "text": "这个，四小时一次，一次半袋。"},
                                    {"speaker": "周远", "text": "半袋。"},
                                    {"speaker": "何姐", "text": "温水擦身，别捂着。"}]}]}
        rep = self._check(doc, ["这个四小时一次,一次半袋 温水擦身,别捂着"])
        self.assertGreaterEqual(rep["rows"][0]["score"], 0.9)
        self.assertEqual([f["code"] for f in rep["todo"]], ["voice_line_dropped"])
        self.assertIn("半袋", rep["todo"][0]["msg"])
        self.assertIn("0/1 片段与台词相符",
                      mediacheck.report_lines({"at": "t", "voice": rep})[0])

    def test_report_lines_render_the_asr_section(self):
        from kinema.pipeline import mediacheck
        rep = {"at": "t", "voice": {"ok": True, "kind": "asr", "available": True,
                                    "rows": [{"id": 1, "score": 0.95},
                                             {"id": 2, "score": 0.2}],
                                    "todo": [{"code": "voice_text_drift",
                                              "msg": "镜 2 …"}]}}
        lines = mediacheck.report_lines(rep)
        self.assertIn("[人声核对] ASR 文字比对 · 1/2 片段与台词相符 · 1 项待修",
                      lines[0])

    def test_skipped_clips_are_not_counted_as_checked(self):
        """跳过的片段没有 score：计进分母会让「0/6 相符」被读成体检结论。"""
        from kinema.pipeline import mediacheck
        rep = {"at": "t", "voice": {"ok": True, "kind": "asr", "available": True,
                                    "rows": [{"id": 1, "note": "片段不在本地，跳过"},
                                             {"id": 2, "score": 0.95}],
                                    "todo": []}}
        self.assertIn("· 1/1 片段与台词相符 · 1 片段未核对",
                      mediacheck.report_lines(rep)[0])


class TestComposeAsrFallback(unittest.TestCase):
    """字幕落点：原生声源的多句镜恒以 ASR 的按句划界为准。

    振幅段数恰好等于句数不构成对位依据——两段可以全落在第二句内部（第一句
    因音量低整句检不出）而段数照样是 2，据此逐句对号入座就把 3.4s 的句子
    压成 1.26s，话没说完字幕就没了。
    """

    def _resolve(self, doc, *, amplitude, aligned, aspect="16:9"):
        """跑一遍真 resolver，返回 (spans, line_windows 调用次数)。"""
        from kinema.pipeline import compose, speech
        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for sh in doc["shots"]:
            for key in ("clip",):
                if sh.get(key):
                    f = tmp / sh[key]
                    f.write_bytes(b"media")
                    sh[key] = str(f)
        adir = tmp / "ch01_work" / "audio"
        adir.mkdir(parents=True, exist_ok=True)
        for sh in doc["shots"]:
            (adir / f"shot_{sh['id']}.wav").write_bytes(b"RIFFfake")
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        project = Project(cf, doc)
        with mock.patch.object(speech, "speech_windows", return_value=amplitude), \
             mock.patch.object(compose, "probe_duration",
                               return_value=float(doc["shots"][0].get("dur") or 5.0)), \
             mock.patch.object(asr, "line_windows", return_value=aligned) as lw:
            spans = compose.speech_spans_resolver(project, aspect)(doc["shots"][0])
        return spans, lw.call_count

    # 振幅「碰巧两段」，而真实语音在别处——ASR 那一组才是对的
    AMPLITUDE = [(2.582, 3.354), (3.792, 5.046)]
    ALIGNED = [(0.43, 1.79), (2.33, 5.71)]

    def _doc(self, **over):
        shot = {"id": 1, "dur": 6.0, "clip": "s1.mp4",
                "lines": [{"speaker": "陈默", "text": "拖轮十分钟到位。"},
                          {"speaker": "旁白", "text": "他没有说的是，这条航道他走了十七年。"}]}
        shot.update(over.pop("shot", {}))
        doc = {"id": "ch01", "motion": "native", "native_voiceover": True,
               "aspect": "16:9", "shots": [shot]}
        doc.update(over)
        return doc

    def test_semantic_alignment_wins_over_a_coincidental_segment_count(self):
        spans, calls = self._resolve(self._doc(), amplitude=self.AMPLITUDE,
                                     aligned=self.ALIGNED)
        self.assertEqual(calls, 1, "多句原生声源镜必须请 ASR，不看振幅段数")
        self.assertEqual(spans, self.ALIGNED)

    def test_collapses_to_the_overall_span_when_alignment_is_unavailable(self):
        """未装 faster-whisper / 转写与稿子对不上：收整体首尾。
        字幕宁可不换人也要覆盖完整——换在错的地方比不换更糟。"""
        spans, calls = self._resolve(self._doc(), amplitude=self.AMPLITUDE,
                                     aligned=None)
        self.assertEqual(calls, 1)
        self.assertEqual(spans, [(self.AMPLITUDE[0][0], self.AMPLITUDE[-1][1])])

    def test_a_burned_shot_never_asks_asr(self):
        """烧录承担的镜（此处：混烧章的旁白镜）逐句时长另有确定性真源
        `lines[].dur`，不拿概率判据去覆盖一个确定量。"""
        doc = self._doc(shot={"lines": [{"speaker": "旁白", "text": "第一句。"},
                                        {"speaker": "旁白", "text": "第二句。"}]})
        spans, calls = self._resolve(doc, amplitude=self.AMPLITUDE,
                                     aligned=self.ALIGNED)
        self.assertEqual(calls, 0)
        self.assertEqual(spans, [(self.AMPLITUDE[0][0], self.AMPLITUDE[-1][1])])

    def test_a_single_line_shot_never_asks_asr(self):
        doc = self._doc(shot={"lines": [{"speaker": "陈默", "text": "只有一句。"}]})
        spans, calls = self._resolve(doc, amplitude=self.AMPLITUDE,
                                     aligned=self.ALIGNED)
        self.assertEqual(calls, 0)
        self.assertEqual(spans, [(self.AMPLITUDE[0][0], self.AMPLITUDE[-1][1])])


if __name__ == "__main__":
    unittest.main()


class TestSilenceDetectionStates(unittest.TestCase):
    """「没有静音」与「探测失败」必须分开：短句 wav 没有 ≥0.35s 的静音时 stderr 里不出现
    silencedetect 字样，把它判成失败会让字幕落点、对白对齐与残留人声探测整套静默失效。"""

    def test_no_events_means_one_voiced_window(self):
        from unittest import mock
        from kinema.pipeline import speech
        with mock.patch.object(speech, "_floor_db", return_value=-30.0), \
             mock.patch.object(speech, "_ffmpeg_audio", return_value=""):
            self.assertEqual(speech._silences("x.wav", 1.6), [])
            self.assertEqual(speech.speech_windows("x.wav", 1.6), [(0.0, 1.6)])

    def test_failed_run_is_none(self):
        from unittest import mock
        from kinema.pipeline import speech
        with mock.patch.object(speech, "_floor_db", return_value=-30.0), \
             mock.patch.object(speech, "_ffmpeg_audio", return_value=None):
            self.assertIsNone(speech._silences("x.wav", 1.6))
            self.assertEqual(speech.speech_windows("x.wav", 1.6), [])

    def test_burned_shot_probes_the_wav_length_not_the_window(self):
        """dubbed 的 dur 是表演窗口、只延不缩：按 dur 探 wav 会把 EOF 到窗口尾判成有声。"""
        from unittest import mock
        from kinema.pipeline import compose, speech
        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        adir = tmp / "ch01_work" / "audio"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "shot_1.wav").write_bytes(b"RIFFfake")
        doc = {"id": "ch01", "motion": "dubbed", "aspect": "16:9",
               "shots": [{"id": 1, "dur": 8.0, "narration": "一句。"}]}
        seen = {}

        def fake_windows(media, duration, *, clean=False):
            seen["duration"] = duration
            return [(0.0, 1.6)]
        with mock.patch.object(speech, "speech_windows", fake_windows), \
             mock.patch.object(compose, "probe_duration", return_value=1.6), \
             mock.patch.object(compose.voicecast, "dubbed_sync_offset", return_value=0.0):
            spans = compose.speech_spans_resolver(Project(tmp / "ch01.json", doc), "16:9")(doc["shots"][0])
        self.assertEqual(seen["duration"], 1.6)
        self.assertEqual(spans, [(0.0, 1.6)])


class TestLineRecalls(unittest.TestCase):
    """逐句召回：整稿一次匹配后按句区间摊回，短句的字不会被别句的转写认领。"""

    def test_a_dropped_short_line_scores_zero_while_the_shot_stays_above_its_floor(self):
        lines = ["这个，四小时一次，一次半袋。", "半袋。", "温水擦身，别捂着。"]
        heard = "这个四小时一次,一次半袋 温水擦身,别捂着"
        self.assertGreaterEqual(asr.text_recall(" ".join(lines), heard), 0.9)
        self.assertEqual(asr.line_recalls(lines, heard), [1.0, 0.0, 1.0])

    def test_an_asr_slip_inside_a_line_stays_above_the_line_floor(self):
        per = asr.line_recalls(["孩子多大？", "两岁半，三十九度二。"],
                               "孩子多大 两岁半 三十九多二")
        self.assertEqual(per[0], 1.0)
        self.assertGreater(per[1], 0.8)

    def test_empty_sides_are_zero(self):
        self.assertEqual(asr.line_recalls(["走。"], ""), [0.0])
        self.assertEqual(asr.line_recalls([], "走"), [])
