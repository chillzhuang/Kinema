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

"""成片自审 verify 守卫。

**两层结构**：
  ① 纯函数解析层（永远跑）——吃 signalstats stdout / volumedetect stderr、
     阈值判定、黑场禁区推导、抽样点、容差、期望字幕条数；
  ② `@skipUnless(_HAS_FFMPEG)` 冒烟层——用 lavfi 现造黑帧 / 哑音 / 有声素材，
     **零素材文件入仓**（临时目录内生成，用完即删）。

阈值全部在真实成片上标定（见 mediacheck 模块头的标定表），本文件的断言就是
那份标定的固化：改阈值必须先在真片上重新标定，再改这里。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import ffmpeg
from kinema.errors import FFmpegError
from kinema.pipeline import mediacheck as mc
from kinema.project import Project

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class _Store:
    """ConfigStore 的最小替身（verify 只用 fps）。"""
    fps = 30


def _project(tmp: Path, data: dict) -> Project:
    """不落盘地造一个 Project（构造器直吃 data，verify 全程只读）。
    夹具缺省是静图形态：kenburns 不作引擎缺省，须显式写。"""
    data.setdefault("motion", "kenburns")
    return Project(tmp / "ch01.json", data)


# ---------------------------------------------------------------------------
# ① 纯函数层
# ---------------------------------------------------------------------------
class TestParseSignalstats(unittest.TestCase):
    REAL = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.signalstats.YMIN=0\n"
        "lavfi.signalstats.YLOW=5\n"
        "lavfi.signalstats.YAVG=42.0888\n"
        "lavfi.signalstats.YHIGH=130\n"
        "lavfi.signalstats.YMAX=255\n"
        "lavfi.signalstats.UAVG=134.721\n"
    )

    def test_parses_real_output(self):
        st = mc.parse_signalstats(self.REAL)
        self.assertAlmostEqual(st["yavg"], 42.0888, places=3)
        self.assertEqual(st["ymax"], 255.0)
        self.assertEqual(st["ymin"], 0.0)

    def test_empty_or_garbage_returns_none(self):
        self.assertIsNone(mc.parse_signalstats(""))
        self.assertIsNone(mc.parse_signalstats("frame:0 pts:0\nmuxing overhead\n"))


class TestBlackThreshold(unittest.TestCase):
    """真实成片标定的双条件判据：`YAVG≤20 且 YMAX≤24`。

    YMAX 是关键判别量——任何真实画面（哪怕最暗的夜戏）都会有高光把 YMAX 顶到
    255，只有合成黑场才会 YMAX≤24。改这四条断言=改验收口径。"""

    def test_true_black_is_black(self):
        # 转场 fade/fade_black 的字卡中央 YAVG=16 / YMAX=16
        self.assertTrue(mc.is_black_frame({"yavg": 16.0, "ymax": 16.0}))

    def test_very_dark_but_not_black_passes(self):
        # 0x0a0a0a 极暗非黑 25/25 —— 不判黑
        self.assertFalse(mc.is_black_frame({"yavg": 25.0, "ymax": 25.0}))

    def test_real_night_scene_passes(self):
        # 真实夜戏 t=1.7 → YAVG=40.2 / YMAX=255
        self.assertFalse(mc.is_black_frame({"yavg": 40.2, "ymax": 255.0}))

    def test_low_yavg_with_highlight_is_not_black(self):
        # 极暗夜戏平均亮度可能压到阈值内，但只要有一点高光就不是黑屏
        self.assertFalse(mc.is_black_frame({"yavg": 12.0, "ymax": 255.0}))

    def test_boundary_values(self):
        self.assertTrue(mc.is_black_frame({"yavg": 20.0, "ymax": 24.0}))
        self.assertFalse(mc.is_black_frame({"yavg": 20.1, "ymax": 24.0}))
        self.assertFalse(mc.is_black_frame({"yavg": 20.0, "ymax": 24.1}))

    def test_unmeasurable_never_reports_black(self):
        self.assertFalse(mc.is_black_frame(None))
        self.assertFalse(mc.is_black_frame({"yavg": 16.0}))     # 缺 ymax


class TestParseVolumedetect(unittest.TestCase):
    REAL = (
        "[Parsed_volumedetect_0 @ 0x600] n_samples: 0\n"
        "[Parsed_volumedetect_0 @ 0x601] n_samples: 3002368\n"
        "[Parsed_volumedetect_0 @ 0x601] mean_volume: -24.9 dB\n"
        "[Parsed_volumedetect_0 @ 0x601] max_volume: -0.9 dB\n"
        "[Parsed_volumedetect_0 @ 0x601] histogram_0db: 5\n"
    )

    def test_takes_last_group(self):
        # 一次运行会打印两组（首组 n_samples: 0）——必须取最后一组
        v = mc.parse_volumedetect(self.REAL)
        self.assertEqual(v["mean_db"], -24.9)
        self.assertEqual(v["max_db"], -0.9)

    def test_two_measured_groups_takes_the_later(self):
        txt = ("mean_volume: -60.0 dB\nmax_volume: -50.0 dB\n"
               "mean_volume: -18.9 dB\nmax_volume: -3.1 dB\n")
        v = mc.parse_volumedetect(txt)
        self.assertEqual((v["mean_db"], v["max_db"]), (-18.9, -3.1))

    def test_no_output_returns_none(self):
        self.assertIsNone(mc.parse_volumedetect(""))
        self.assertIsNone(mc.parse_volumedetect("Output file is empty\n"))


class TestBlackWindows(unittest.TestCase):
    """转场黑场禁区：不排除的话每条带转场的片子都会误报黑屏。"""

    def _proj(self, tmp):
        return _project(tmp, {"shots": [
            {"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0},
            # fade：edge=0.2 / dur=0.1（注册表缺省）
            {"id": 3, "kind": "transition", "dur": 0.1,
             "transition": {"type": "fade"}},
            {"id": 4, "dur": 2.0},
        ]})

    def test_window_covers_transition_span_plus_both_edges(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._proj(Path(td))
        # 转场镜时间轴 [4.0, 4.1]，两侧各扩 edge=0.2 → (3.8, 4.3)
        self.assertEqual(mc.black_windows(p), [(3.8, 4.3)])

    def test_no_transition_no_window(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"shots": [{"id": 1, "dur": 2.0},
                                              {"id": 2, "dur": 2.0}]})
        self.assertEqual(mc.black_windows(p), [])

    def test_explicit_edge_override_widens_window(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"shots": [
                {"id": 1, "dur": 1.5},
                {"id": 2, "kind": "transition", "dur": 1.0,
                 "transition": {"type": "fade", "edge": 0.5}},
                {"id": 3, "dur": 1.5}]})
        self.assertEqual(mc.black_windows(p), [(1.0, 3.0)])

    def test_merge_overlapping(self):
        self.assertEqual(mc.merge_windows([(1.0, 2.0), (1.5, 3.0), (5.0, 6.0)]),
                         [(1.0, 3.0), (5.0, 6.0)])

    def test_window_clamped_at_zero(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"shots": [
                {"id": 1, "kind": "transition", "dur": 0.1,
                 "transition": {"type": "fade"}},
                {"id": 2, "dur": 2.0}]})
        self.assertEqual(mc.black_windows(p)[0][0], 0.0)


class TestSamplePoints(unittest.TestCase):
    def test_never_lands_in_a_window(self):
        wins = [(3.8, 4.3), (10.0, 11.0)]
        pts = mc.sample_points(20.0, 12, wins)
        self.assertEqual(len(pts), 12)
        for t in pts:
            for a, b in wins:
                self.assertFalse(a <= t <= b, f"抽样点 {t} 落进黑场禁区 ({a},{b})")

    def test_stays_inside_margins(self):
        pts = mc.sample_points(5.0, 6, [])
        self.assertTrue(all(mc.EDGE_MARGIN <= t <= 5.0 - mc.EDGE_MARGIN for t in pts))

    def test_deterministic(self):
        self.assertEqual(mc.sample_points(37.9, 8, [(5.63, 6.13)]),
                         mc.sample_points(37.9, 8, [(5.63, 6.13)]))

    def test_all_forbidden_returns_empty(self):
        self.assertEqual(mc.sample_points(2.0, 5, [(0.0, 2.0)]), [])

    def test_zero_length_video_returns_empty(self):
        self.assertEqual(mc.sample_points(0.05, 5, []), [])

    def test_allowed_spans_split_by_window(self):
        self.assertEqual(mc.allowed_spans(10.0, [(4.0, 6.0)], margin=0.0),
                         [(0.0, 4.0), (6.0, 10.0)])


class TestDurationTolerance(unittest.TestCase):
    """帧量化感知：逐片段 frames=round(dur*fps)，N 镜累计合法误差可达 N/fps。"""

    def test_floor_is_half_second(self):
        self.assertEqual(mc.duration_tolerance(3, 30), 0.5)

    def test_scales_with_shot_count(self):
        self.assertEqual(mc.duration_tolerance(30, 30), 1.0)
        self.assertEqual(mc.duration_tolerance(60, 24), 2.5)

    def test_fps_zero_falls_back_to_30(self):
        self.assertEqual(mc.duration_tolerance(60, 0), 2.0)


class TestAudioExpected(unittest.TestCase):
    """「该响却哑」的硬失败集合——**必须含 native**。"""

    def _p(self, td, data):
        return _project(Path(td), data)

    def test_native_always_expects_audio(self):
        with tempfile.TemporaryDirectory() as td:
            # native：needs_tts=False、不叠 BGM、无旁白——片段丢音轨被降级后
            # 成片没有任何音频兜底，正是最该抓的事故
            p = self._p(td, {"motion": "native",
                             "shots": [{"id": 1, "dur": 2.0, "narration": ""}]})
        self.assertTrue(mc.audio_expected(p))

    def test_dubbed_expects_audio(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._p(td, {"motion": "dubbed",
                             "shots": [{"id": 1, "dur": 2.0, "narration": ""}]})
        self.assertTrue(mc.audio_expected(p))

    def test_kenburns_with_narration_expects_audio(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._p(td, {"shots": [{"id": 1, "dur": 2.0, "narration": "有台词"}]})
        self.assertTrue(mc.audio_expected(p))

    def test_kenburns_with_bgm_expects_audio(self):
        with tempfile.TemporaryDirectory() as td:
            bgm = Path(td) / "bgm.wav"
            bgm.write_bytes(b"x")
            p = self._p(td, {"audio": {"bgm_file": str(bgm)},
                             "shots": [{"id": 1, "dur": 2.0}]})
            self.assertTrue(mc.audio_expected(p))   # 断言须在临时目录存活期内

    def test_silent_kenburns_does_not_expect_audio(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._p(td, {"shots": [{"id": 1, "dur": 2.0}]})
        self.assertFalse(mc.audio_expected(p))

    def test_omitted_shot_narration_does_not_count(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._p(td, {"shots": [
                {"id": 1, "dur": 2.0},
                {"id": 2, "dur": 2.0, "narration": "弃镜台词",
                 "review": {"shot": {"state": "omt"}}}]})
        self.assertFalse(mc.audio_expected(p))


class TestSubtitleExpectation(unittest.TestCase):
    def test_counts_shots_with_text(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"shots": [
                {"id": 1, "dur": 2.0, "narration": "甲"},
                {"id": 2, "dur": 2.0, "caption": "乙"},     # 无旁白镜由 caption 补位
                {"id": 3, "dur": 1.0, "kind": "transition",
                 "transition": {"type": "fade"}},
                {"id": 4, "dur": 2.0}]})                    # 纯画面镜不出字幕
        self.assertEqual(mc.expected_subtitle_events(p, "zh"), 2)

    def test_corner_note_only_shot_not_counted(self):
        # subtitle.build_from_timeline 的 continue 在 corner_note 分支之前，
        # 「只有 corner_note 无旁白」的镜现状下永远渲不出角标——期望值按现状算
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"shots": [
                {"id": 1, "dur": 2.0, "narration": "甲"},
                {"id": 2, "dur": 2.0, "corner_note": "摄于台北"}]})
        self.assertEqual(mc.expected_subtitle_events(p, "zh"), 1)

    def test_bilingual_counts_english_only_shot(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"shots": [
                {"id": 1, "dur": 2.0, "narration": "甲", "narration_en": "A"},
                {"id": 2, "dur": 2.0, "narration_en": "B only"}]})
        self.assertEqual(mc.expected_subtitle_events(p, "both"), 2)

    def test_count_dialogues_ignores_header(self):
        ass = ("[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
               "Format: Layer, Start, End\n"
               "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,甲\n"
               "Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,乙\n")
        self.assertEqual(mc.count_dialogues(ass), 2)
        self.assertEqual(mc.count_dialogues(""), 0)


class TestLoudnessJudgement(unittest.TestCase):
    def test_on_target_film_has_no_offset_flag(self):
        # 响度归一后的成片实测 -16.1 LUFS（目标 -16）
        self.assertEqual(mc.loudness_off_target({"input_i": -16.1}), -0.1)

    def test_pre_normalization_film_is_off_target(self):
        # 响度归一前的成片实测 -22.7 LUFS → 偏离 6.7，记「待修」不硬拦
        off = mc.loudness_off_target({"input_i": -22.7})
        self.assertGreater(abs(off), mc.LOUDNESS_TOL)

    def test_unmeasurable_returns_none(self):
        self.assertIsNone(mc.loudness_off_target(None))
        self.assertIsNone(mc.loudness_off_target({"input_i": "n/a"}))

    def test_silence_infinity_never_leaks_into_report(self):
        """整段静音时 loudnorm 报 `"-inf"`，`parse_measurement` 会转成真正的
        `float('-inf')`（不是字符串）——若原样落进结论块，`json.dump` 就吐出
        `-Infinity`，project.json 不再是合法 JSON、Studio 章节页 `res.json()` 直接死。
        故 `loudness_i`/`loudness_off_target` 必须双双 None。"""
        silent = {"input_i": float("-inf"), "input_tp": float("-inf"),
                  "target_offset": float("inf")}
        self.assertIsNone(mc.loudness_i(silent))
        self.assertIsNone(mc.loudness_off_target(silent))
        self.assertIsNone(mc.loudness_i({"input_i": float("nan")}))
        # 有限值仍照常出数
        self.assertEqual(mc.loudness_i({"input_i": -16.1}), -16.1)


class TestProbeArgShapes(unittest.TestCase):
    """探测命令的 flag 形态（真机标定，钉死不许改）。"""

    def test_volume_args_pin_vn(self):
        # 无音轨且**不加 -vn** 时 ffmpeg 退出 0 且静默无输出 →
        # 会被误判成「测到了但值为空」；加 -vn 才退出 234 明确失败
        args = mc.volume_args("/x/a.mp4")
        self.assertIn("-vn", args)
        self.assertLess(args.index("-vn"), args.index("-af"))
        self.assertEqual(args[args.index("-af") + 1], "volumedetect")

    def test_frame_args_seek_before_input_and_single_frame(self):
        args = mc.frame_stats_args("/x/a.mp4", 3.5)
        self.assertEqual(args[:2], ["-ss", "3.500"])
        self.assertLess(args.index("-ss"), args.index("-i"))   # 前置快进
        self.assertEqual(args[args.index("-frames:v") + 1], "1")
        self.assertIn("-an", args)                             # 不解音频
        self.assertIn("signalstats,metadata=print:file=-", args)

    def test_frame_args_clamp_negative_time(self):
        self.assertEqual(mc.frame_stats_args("/x/a.mp4", -1.0)[1], "0.000")


class TestNeverRaises(unittest.TestCase):
    """probe 异常全程转成条目，绝不冒泡——否则第一支坏片就中断整条 verify。"""

    def test_probe_helpers_swallow_exceptions(self):
        with mock.patch.object(mc, "run_capture", side_effect=OSError("no ffmpeg")):
            self.assertIsNone(mc.probe_frame("/x/a.mp4", 1.0))
            self.assertIsNone(mc.probe_volume("/x/a.mp4"))
        with mock.patch.object(mc, "probe_json", side_effect=FFmpegError("boom")):
            self.assertIsNone(mc.has_audio_stream("/x/a.mp4"))

    def test_broken_container_becomes_hard_fail_not_exception(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bad = tmp / "broken.mp4"
            bad.write_bytes(b"")                              # 0 字节：probe 必抛
            p = _project(tmp, {"id": "ch01", "aspect": "16:9",
                               "output": {"16:9": str(bad)},
                               "shots": [{"id": 1, "dur": 2.0}]})
            rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertFalse(rep["ok"])
        self.assertEqual([f["code"] for f in rep["hard_fail"]], ["container"])

    def test_narrated_chapter_without_narration_track_is_hard_fail(self):
        """该产旁白轨的章没登记旁白轨：成片没有固定音色人声，BGM 会让整片均值
        远高于静音阈值，靠「该响却哑」抓不住，必须单独硬判。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bad = tmp / "broken.mp4"
            bad.write_bytes(b"")
            base = {"id": "ch01", "aspect": "16:9", "output": {"16:9": str(bad)},
                    "shots": [{"id": 1, "dur": 2.0, "narration": "一句旁白"}]}
            rep = mc.verify_aspect(_project(tmp, base), _Store(), aspect="16:9", samples=2)
            self.assertIn("narration_missing", [f["code"] for f in rep["hard_fail"]])
            wav = tmp / "narration.wav"
            wav.write_bytes(b"RIFF")
            with_track = {**base, "audio": {"narration_file": str(wav)}}
            rep = mc.verify_aspect(_project(tmp, with_track), _Store(), aspect="16:9", samples=2)
            self.assertNotIn("narration_missing", [f["code"] for f in rep["hard_fail"]])
            silent = {**base, "shots": [{"id": 1, "dur": 2.0}]}     # 无词章不要求旁白轨
            rep = mc.verify_aspect(_project(tmp, silent), _Store(), aspect="16:9", samples=2)
            self.assertNotIn("narration_missing", [f["code"] for f in rep["hard_fail"]])

    def test_missing_output_is_hard_fail(self):
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"output": {}, "shots": [{"id": 1, "dur": 2.0}]})
            rep = mc.verify_aspect(p, _Store(), aspect="16:9")
        self.assertEqual([f["code"] for f in rep["hard_fail"]], ["missing"])

    def test_boolean_approved_key_is_filtered(self):
        # output.approved 是 boolean（schema），既有惯例 isinstance(p,str) and p
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"output": {"approved": True},
                                    "shots": [{"id": 1, "dur": 2.0}]})
            rep = mc.verify_aspect(p, _Store(), aspect="approved")
        self.assertEqual([f["code"] for f in rep["hard_fail"]], ["missing"])


class TestOssOutputLocalized(unittest.TestCase):
    """读 output 前必须过 `storage.media.ensure_local`——值可能是 OSS URL，
    直接喂 ffprobe 会在无网/私有桶下把好片判成硬失败。"""

    def test_url_goes_through_ensure_local(self):
        from kinema.storage import media
        url = "https://bucket.example.com/project/x/out_16x9.mp4"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            local = tmp / "out.mp4"
            local.write_bytes(b"\0" * 10)
            p = _project(tmp, {"output": {"16:9": url},
                               "shots": [{"id": 1, "dur": 2.0}]})
            with mock.patch.object(media, "ensure_local",
                                   return_value=str(local)) as m:
                with mock.patch.object(mc, "probe_duration", return_value=2.0), \
                     mock.patch.object(mc, "probe_json", return_value={"streams": []}), \
                     mock.patch.object(mc, "probe_frame", return_value=None):
                    rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=1)
        m.assert_called_once_with(url)
        self.assertEqual(rep["file"], str(local))
        self.assertTrue(rep["ok"])          # 纯画面无 BGM → 无音轨属正常

    def test_localize_failure_skips_not_fails(self):
        from kinema.storage import media
        url = "https://bucket.example.com/project/x/out_16x9.mp4"
        with tempfile.TemporaryDirectory() as td:
            p = _project(Path(td), {"output": {"16:9": url},
                                    "shots": [{"id": 1, "dur": 2.0}]})
            with mock.patch.object(media, "ensure_local",
                                   side_effect=OSError("网络不可达")):
                rep = mc.verify_aspect(p, _Store(), aspect="16:9")
        self.assertTrue(rep["ok"])          # 拉不回来 = 无从体检，记 info 跳过
        self.assertEqual(rep["hard_fail"], [])
        self.assertTrue(rep["info"])


class TestRunCapture(unittest.TestCase):
    """`run()` 原语义零变化 + verify 只走 `run_capture`（`run()` 会吞输出）。"""

    def test_render_primitive_still_pins_loglevel_error(self):
        class _P:
            returncode, stdout, stderr = 0, "", ""
        with mock.patch.object(subprocess, "run", return_value=_P()) as m:
            self.assertIsNone(ffmpeg.run(["-i", "a.mp4", "o.mp4"]))
        self.assertEqual(m.call_args.args[0][:5],
                         ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"])

    def test_frame_probe_uses_error_level_stdout(self):
        with mock.patch.object(mc, "run_capture",
                               return_value=(0, TestParseSignalstats.REAL, "")) as m:
            st = mc.probe_frame("/x/a.mp4", 1.0)
        self.assertEqual(m.call_args.kwargs["loglevel"], "error")   # 结论走 stdout
        self.assertEqual(st["ymax"], 255.0)

    def test_volume_probe_uses_info_level_stderr(self):
        with mock.patch.object(mc, "run_capture",
                               return_value=(0, "", TestParseVolumedetect.REAL)) as m:
            v = mc.probe_volume("/x/a.mp4")
        self.assertEqual(m.call_args.kwargs["loglevel"], "info")    # 结论走 stderr
        self.assertEqual(v["mean_db"], -24.9)

    def test_nonzero_returncode_means_unmeasurable(self):
        with mock.patch.object(mc, "run_capture", return_value=(234, "", "")):
            self.assertIsNone(mc.probe_volume("/x/a.mp4"))


# ---------------------------------------------------------------------------
# ② 冒烟层（lavfi 现造素材，零文件入仓）
# ---------------------------------------------------------------------------
@unittest.skipUnless(_HAS_FFMPEG, "需要系统 ffmpeg 做 verify 冒烟")
class TestVerifySmoke(unittest.TestCase):
    """用 lavfi 现造黑帧 / 哑音 / 有声素材跑真探测——素材全在临时目录，用完即删。"""

    W, H, FPS, DUR = 320, 180, 30, 4.0

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="kn_verify_")
        self.tmp = Path(self.td)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _make(self, name, *, vf=None, audio=None, black=False):
        """造一支小样片：底色（可选中段涂黑）+ 可选音轨。"""
        out = self.tmp / name
        src = ("color=c=black" if black else "color=c=0x557799")
        args = ["-f", "lavfi", "-i",
                f"{src}:s={self.W}x{self.H}:r={self.FPS}:d={self.DUR}"]
        if audio:
            args += ["-f", "lavfi", "-i", audio]
        if vf:
            args += ["-vf", vf]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", f"{self.DUR}"]
        if audio:
            args += ["-c:a", "aac", "-b:a", "96k"]
        args += [str(out)]
        rc, _o, err = ffmpeg.run_capture(args, loglevel="error")
        self.assertEqual(rc, 0, f"造样片失败: {err[-300:]}")
        return out

    def _proj(self, shots, out, **extra):
        data = {"id": "ch01", "aspect": "16:9", "aspects": ["16:9"],
                "output": {"16:9": str(out)}, "shots": shots}
        data.update(extra)
        return _project(self.tmp, data)

    def _wav(self) -> Path:
        """有词章的旁白轨登记物（体检只看在盘，不解码）。"""
        wav = self.tmp / "ch01_work" / "audio" / "narration.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF")
        return wav

    def _write_ass(self, n):
        subs = self.tmp / "ch01_work" / "subs"
        subs.mkdir(parents=True, exist_ok=True)
        body = "".join(f"Dialogue: 0,0:00:0{i},0:00:0{i + 1},Default,,0,0,0,,行{i}\n"
                       for i in range(n))
        (subs / "sub_16x9.ass").write_text("[Events]\n" + body, encoding="utf-8")

    # ---- 黑屏 ----
    def test_all_black_film_hard_fails(self):
        out = self._make("black.mp4", black=True)
        p = self._proj([{"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0}], out)
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=4)
        self.assertFalse(rep["ok"])
        self.assertIn("black", [f["code"] for f in rep["hard_fail"]])
        self.assertTrue(all(s["black"] for s in rep["black_samples"]))

    def test_normal_film_has_no_black(self):
        out = self._make("ok.mp4")
        p = self._proj([{"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0}], out)
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=4)
        self.assertTrue(rep["ok"], rep["hard_fail"])
        self.assertFalse(any(s["black"] for s in rep["black_samples"]))

    def test_black_inside_transition_window_is_excluded(self):
        """核心用例：片中 1.5~2.5s 满屏黑。

        · 分镜里有覆盖该段的转场镜 → 禁区排除 → 通过（否则每条带转场的片子都误报）；
        · 同一支片子换成无转场的分镜 → 抽样点落进黑段 → 硬失败（证明禁区是真在起作用，
          不是把黑屏检测整个关掉了）。"""
        out = self._make(
            "mid_black.mp4",
            vf="drawbox=x=0:y=0:w=iw:h=ih:color=black@1:t=fill:"
               "enable='between(t,1.5,2.5)'")
        with_tr = self._proj([
            {"id": 1, "dur": 1.5},
            {"id": 2, "kind": "transition", "dur": 1.0,
             "transition": {"type": "fade", "edge": 0.5}},     # 禁区 (1.0, 3.0)
            {"id": 3, "dur": 1.5}], out)
        self.assertEqual(mc.black_windows(with_tr), [(1.0, 3.0)])
        rep = mc.verify_aspect(with_tr, _Store(), aspect="16:9", samples=8)
        self.assertTrue(rep["ok"], rep["hard_fail"])

        no_tr = self._proj([{"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0}], out)
        rep2 = mc.verify_aspect(no_tr, _Store(), aspect="16:9", samples=8)
        self.assertFalse(rep2["ok"])
        self.assertIn("black", [f["code"] for f in rep2["hard_fail"]])

    # ---- 音频 ----
    def test_silent_track_with_narration_hard_fails(self):
        out = self._make("mute.mp4", audio=f"anullsrc=r=44100:cl=mono:d={self.DUR}")
        self._write_ass(2)
        p = self._proj([{"id": 1, "dur": 2.0, "narration": "甲"},
                        {"id": 2, "dur": 2.0, "narration": "乙"}], out)
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertFalse(rep["ok"])
        self.assertIn("mute", [f["code"] for f in rep["hard_fail"]])
        self.assertLessEqual(rep["audio"]["mean_db"], mc.SILENT_MEAN_DB)
        # 静音正是 loudnorm 报 -inf 的场景：结论块必须是**合法 JSON**
        # （allow_nan=False 等价于 JS `JSON.parse` 的严格口径），否则
        # project.json 一写就废、Studio 章节页打不开
        self.assertIsNone(rep["audio"]["loudness_i"])
        self.assertIsNone(rep["audio"]["loudness_off"])
        json.dumps(rep, ensure_ascii=False, allow_nan=False)

    def test_native_without_audio_stream_hard_fails(self):
        # native 片段丢音轨被 compose 降级后成片没有任何音频兜底——最该抓的事故
        out = self._make("noaudio.mp4")
        p = self._proj([{"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0}], out,
                       motion="native")
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertFalse(rep["ok"])
        self.assertIn("no_audio", [f["code"] for f in rep["hard_fail"]])

    def test_silent_kenburns_without_audio_is_only_info(self):
        out = self._make("silentkb.mp4")
        p = self._proj([{"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0}], out)
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertTrue(rep["ok"], rep["hard_fail"])
        self.assertTrue(rep["info"])

    def test_audible_film_passes(self):
        out = self._make("tone.mp4",
                         audio=f"sine=f=440:r=44100:d={self.DUR},volume=0.2")
        self._write_ass(2)
        p = self._proj([{"id": 1, "dur": 2.0, "narration": "甲"},
                        {"id": 2, "dur": 2.0, "narration": "乙"}], out, audio={"narration_file": str(self._wav())})
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=4)
        self.assertTrue(rep["ok"], rep["hard_fail"])
        self.assertTrue(rep["audio"]["has_stream"])
        self.assertGreater(rep["audio"]["mean_db"], mc.SILENT_MEAN_DB)
        self.assertEqual(rep["subtitle"]["dialogues"], 2)

    # ---- 时长 / 字幕 ----
    def test_duration_mismatch_hard_fails(self):
        out = self._make("short.mp4")                    # 实际 4s
        p = self._proj([{"id": 1, "dur": 5.0}, {"id": 2, "dur": 5.0}], out)  # 期望 10s
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertFalse(rep["ok"])
        self.assertIn("duration", [f["code"] for f in rep["hard_fail"]])
        self.assertEqual(rep["duration"]["tolerance"], 0.5)

    def test_duration_within_frame_quantization_passes(self):
        out = self._make("ok2.mp4")                      # 4.0s
        p = self._proj([{"id": 1, "dur": 2.02}, {"id": 2, "dur": 2.02}], out)
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertTrue(rep["ok"], rep["hard_fail"])

    def test_missing_subtitle_file_hard_fails(self):
        out = self._make("nosub.mp4",
                         audio=f"sine=f=440:r=44100:d={self.DUR},volume=0.2")
        p = self._proj([{"id": 1, "dur": 2.0, "narration": "甲"},
                        {"id": 2, "dur": 2.0, "narration": "乙"}], out)
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertIn("subtitle", [f["code"] for f in rep["hard_fail"]])

    def test_subtitle_shortfall_is_todo_not_hard_fail(self):
        out = self._make("fewsub.mp4",
                         audio=f"sine=f=440:r=44100:d={self.DUR},volume=0.2")
        self._write_ass(1)                               # 只有 1 条，期望 2 条
        p = self._proj([{"id": 1, "dur": 2.0, "narration": "甲"},
                        {"id": 2, "dur": 2.0, "narration": "乙"}], out, audio={"narration_file": str(self._wav())})
        rep = mc.verify_aspect(p, _Store(), aspect="16:9", samples=2)
        self.assertTrue(rep["ok"], rep["hard_fail"])
        self.assertIn("subtitle", [f["code"] for f in rep["todo"]])

    # ---- 整体报告 ----
    def test_verify_report_shape(self):
        out = self._make("shape.mp4")
        p = self._proj([{"id": 1, "dur": 2.0}, {"id": 2, "dur": 2.0}], out)
        rep = mc.verify(p, _Store(), samples=2)
        self.assertIn("at", rep)
        self.assertIn("16:9", rep)
        blk = rep["16:9"]
        for k in ("ok", "hard_fail", "todo", "duration", "black_samples",
                  "audio", "subtitle"):
            self.assertIn(k, blk)
        # allow_nan=False：Infinity/NaN 不是合法 JSON，落盘即毁 project.json
        json.dumps(rep, ensure_ascii=False, allow_nan=False)
        self.assertTrue(mc.report_lines(rep))


if __name__ == "__main__":
    unittest.main()


class TestVoicePlacement(unittest.TestCase):
    """旁白轨逐镜语音落点（narration 主音轨两档专属）：带 BGM 的成片本体不做
    振幅级语音检测——判据对象恒是 assemble 重拼后的旁白轨。"""

    def _project(self, motion="dubbed", shots=None):
        import json
        import tempfile

        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        data = {"id": "ch01", "motion": motion, "aspect": "16:9",
                "audio": {"narration_file": str(tmp / "narration.wav")},
                "shots": shots or [
                    {"id": 1, "dur": 4.0, "speaker": "甲", "narration": "走。"},
                    {"id": 2, "dur": 4.0, "narration": ""}]}
        (tmp / "narration.wav").write_bytes(b"RIFFfake")
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project(cf, data)

    def _run(self, project, segs):
        import unittest.mock as um

        from kinema.pipeline import mediacheck
        with um.patch.object(mediacheck, "probe_duration", lambda p: 8.0), \
             um.patch("kinema.pipeline.speech.speech_windows",
                      lambda *a, **k: segs):
            return mediacheck.voice_placement(project)

    def test_worded_shot_without_speech_is_a_todo(self):
        rep = self._run(self._project(), [])
        self.assertEqual([f["code"] for f in rep["todo"]], ["voice_missing"])

    def test_aligned_speech_mid_window_passes(self):
        """开口对齐允许语音起点落在窗口中段——不判头部位置。"""
        rep = self._run(self._project(), [(1.8, 3.6)])
        self.assertEqual(rep["todo"], [])
        self.assertEqual(rep["rows"][0]["speech"], [(1.8, 3.6)])

    def test_stray_speech_in_silent_window_is_a_todo(self):
        rep = self._run(self._project(), [(0.5, 3.5), (5.0, 6.5)])
        self.assertEqual([f["code"] for f in rep["todo"]], ["voice_stray"])

    def test_border_bleed_under_threshold_is_tolerated(self):
        rep = self._run(self._project(), [(0.5, 3.5), (3.9, 4.25)])
        self.assertEqual(rep["todo"], [])

    def test_native_and_scored_are_out_of_scope(self):
        self.assertIsNone(self._run(self._project(motion="native"), []))
        p = self._project()
        p.data["audio_mode"] = "scored"
        self.assertIsNone(self._run(p, []))


class TestExpectedSubtitleCountsLines(unittest.TestCase):
    """期望字幕条数必须认得 lines[]：裸 pick_texts 只读 narration/caption，
    逐句字幕的章节期望值会塌到只剩 caption 补位，「不少于」检查形同虚设。"""

    def test_lines_only_shots_count_as_worded(self):
        import json
        import tempfile

        from kinema.pipeline import mediacheck
        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        data = {"id": "ch01", "motion": "dubbed", "aspect": "16:9", "shots": [
            {"id": 1, "dur": 4.0, "narration": "",
             "lines": [{"speaker": "甲", "text": "走。"},
                       {"speaker": "乙", "text": "等等。"}]},
            {"id": 2, "dur": 4.0, "narration": "", "caption": "黎明前"},
            {"id": 3, "dur": 4.0, "narration": ""}]}
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        p = Project(cf, data)
        self.assertEqual(mediacheck.expected_subtitle_events(p, "zh"), 2)
