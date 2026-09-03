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

"""参考片读片（study · M10）守卫。

**头号用例是 `TestRelativePathOnly`（版权护栏）**：参考片是第三方素材，一旦契约
里写成绝对路径，`collect_media` 就会把它收进上传清单、`oss sync` 随后把片子传上
用户自己的公网 OSS 并生成可访问 URL——那是公网转载。用例用「相对不收 / 绝对必收」
的对照形式钉死：既证明护栏生效，也证明护栏是**唯一**拦住它的东西（若哪天有人把
路径改成绝对，用例的第二半会立刻变成事故复现）。

其余：纯函数解析（ffmpeg 文本格式变了立刻红）、digest/契约的形态与分工
（全表进 sidecar、契约只留指针+计数）、有限数纪律、非视频拒收。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kinema import study as S
from kinema.errors import ProjectError
from kinema.storage.media import collect_media

from tests.support import LocalBackendEnv

_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ---- 真实 ffmpeg 输出样本（从本机实测拷贝，别改成手写臆造格式）----
SHOWINFO_OUT = """\
[Parsed_showinfo_1 @ 0x600000000a50] config in time_base: 1/10240, frame_rate: 10/1
[Parsed_showinfo_1 @ 0x600000000a50] n:   0 pts:  20480 pts_time:2       duration:   1024 duration_time:0.1     fmt:yuv420p sar:1/1 s:320x240 i:P iskey:1 type:I
[Parsed_showinfo_1 @ 0x600000000a50] color_range:unknown color_space:unknown
[Parsed_showinfo_1 @ 0x600000000a50] n:   1 pts:  40960 pts_time:4.5     duration:   1024 duration_time:0.1     fmt:yuv420p sar:1/1 s:320x240 i:P iskey:0 type:P
"""

SCDET_OUT = """\
[scdet @ 0x600000524000] lavfi.scd.score: 15.625, lavfi.scd.time: 2
[scdet @ 0x600000524000] lavfi.scd.score: 15.625, lavfi.scd.time: 4.5
"""

SILENCE_OUT = """\
[silencedetect @ 0x6000007683c0] silence_start: 0
[silencedetect @ 0x6000007683c0] silence_end: 1.5 | silence_duration: 1.5
[silencedetect @ 0x6000007683c0] silence_start: 8.25
[silencedetect @ 0x6000007683c0] silence_end: 9.25 | silence_duration: 1
"""


class TestParseSceneCuts(unittest.TestCase):
    def test_showinfo_format(self):
        self.assertEqual(S.parse_scene_cuts(SHOWINFO_OUT), [2.0, 4.5])

    def test_scdet_format(self):
        self.assertEqual(S.parse_scene_cuts(SCDET_OUT), [2.0, 4.5])

    def test_duration_time_is_not_mistaken_for_a_cut(self):
        # showinfo 每行都带 duration_time:0.1；泛化正则会把每帧帧长当切点、切点表翻倍
        self.assertNotIn(0.1, S.parse_scene_cuts(SHOWINFO_OUT))

    def test_dedup_and_sort(self):
        # 两条滤镜各报一次同一刀 → 合并；输出恒升序
        self.assertEqual(S.parse_scene_cuts(SCDET_OUT + SHOWINFO_OUT), [2.0, 4.5])
        self.assertEqual(S.parse_scene_cuts("pts_time:5\npts_time:1\n"), [1.0, 5.0])

    def test_empty_and_garbage(self):
        self.assertEqual(S.parse_scene_cuts(""), [])
        self.assertEqual(S.parse_scene_cuts("no cuts here at all"), [])


class TestParseSilences(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(S.parse_silences(SILENCE_OUT, 10.0),
                         [[0.0, 1.5], [8.25, 9.25]])

    def test_dangling_start_closes_at_duration(self):
        # 片子在静音中结束时 ffmpeg 只打 start——不补就丢掉「结尾大段留白」这一最典型特征
        txt = "silence_start: 6.0\n"
        self.assertEqual(S.parse_silences(txt, 10.0), [[6.0, 10.0]])
        self.assertEqual(S.parse_silences(txt, None), [])      # 无时长宁可少算

    def test_ratio(self):
        self.assertEqual(S.silence_ratio([[0.0, 1.5], [8.25, 9.25]], 10.0), 0.25)
        self.assertIsNone(S.silence_ratio([], None))           # 测不到 ≠ 全程有声
        self.assertEqual(S.silence_ratio([], 10.0), 0.0)


class TestRhythm(unittest.TestCase):
    def test_shot_table_from_cuts(self):
        t = S.shot_table([2.0, 4.0], 6.0)
        self.assertEqual([s["dur"] for s in t], [2.0, 2.0, 2.0])
        self.assertEqual(t[0]["start"], 0.0)
        self.assertEqual(t[-1]["end"], 6.0)

    def test_out_of_range_cuts_dropped(self):
        # 越界/倒序切点（探测抖动）不许造出 dur<=0 的镜
        t = S.shot_table([-1.0, 3.0, 2.0, 99.0], 6.0)
        self.assertEqual([s["dur"] for s in t], [3.0, 3.0])
        self.assertTrue(all(s["dur"] > 0 for s in t))

    def test_cut_rounding_to_duration_makes_no_empty_shot(self):
        # 5.9996 与片长 6.0 取整后都是 6.0——拿原值比会造出一个 dur=0 的空镜、n_shots 多一
        t = S.shot_table([2.0, 5.9996], 6.0)
        self.assertEqual([s["dur"] for s in t], [2.0, 4.0])
        self.assertTrue(all(s["dur"] > 0 for s in t))

    def test_metrics(self):
        r = S.rhythm([2.0, 4.0], 6.0, [[0.0, 1.5]])
        self.assertEqual((r["n_cuts"], r["n_shots"]), (2, 3))
        self.assertEqual(r["cuts_per_min"], 20.0)
        self.assertEqual(r["avg_shot_sec"], 2.0)
        self.assertEqual(r["silence_ratio"], 0.25)

    def test_no_audio_silence_is_none_not_zero(self):
        r = S.rhythm([], 6.0, [], has_audio=False)
        self.assertIsNone(r["silence_ratio"])

    def test_engine_emits_no_verdict(self):
        # 铁律「引擎内无 LLM」：引擎只出可测量量，motion 选型/快慢判定归指挥层
        r = S.rhythm([2.0], 6.0, [])
        for banned in ("motion", "verdict", "pace", "style", "recommend"):
            self.assertNotIn(banned, r)


class TestFrameTimes(unittest.TestCase):
    def test_even_and_midpoints(self):
        self.assertEqual(S.frame_times(10.0, 5), [1.0, 3.0, 5.0, 7.0, 9.0])

    def test_hard_cap(self):
        # 上限是「按时间轴均匀降采样」而不是截断前 48 张——末点必须仍靠近片尾
        ts = S.frame_times(600.0, 1000)
        self.assertEqual(len(ts), S.MAX_FRAMES)
        self.assertGreater(ts[-1], 590.0)

    def test_degenerate(self):
        self.assertEqual(S.frame_times(None, 5), [])
        self.assertEqual(S.frame_times(0.0, 5), [])
        self.assertEqual(S.frame_times(10.0, 0), [])       # --frames 0 = 只要数不要图


class TestMediaMeta(unittest.TestCase):
    def _probe(self, **v):
        return {"streams": [{"codec_type": "video", "codec_name": "h264",
                             "width": 1920, "height": 1080,
                             "avg_frame_rate": v.get("rate", "25/1")}],
                "format": {"duration": v.get("dur", "62.4")}}

    def test_basic(self):
        m = S.media_meta(self._probe())
        self.assertEqual((m["dur"], m["fps"], m["width"], m["height"]),
                         (62.4, 25.0, 1920, 1080))
        self.assertFalse(m["has_audio"])
        self.assertFalse(m["has_subs"])

    def test_zero_denominator_never_divides_by_zero(self):
        # 图片流/无帧率时 avg_frame_rate 是 "0/0"——写 inf 进 project.json = Studio 白屏
        m = S.media_meta(self._probe(rate="0/0"))
        self.assertIsNone(m["fps"])

    def test_audio_and_subs_flags(self):
        p = self._probe()
        p["streams"] += [{"codec_type": "audio"}, {"codec_type": "subtitle"}]
        m = S.media_meta(p)
        self.assertTrue(m["has_audio"] and m["has_subs"])

    def test_empty_probe(self):
        m = S.media_meta(None)
        self.assertIsNone(m["dur"])
        self.assertIsNone(m["vcodec"])


class TestDigestShape(unittest.TestCase):
    """digest = 全表 sidecar；契约条目 = 只留指针 + 计数（source/segments.json 先例）。"""

    def _digest(self):
        meta = {"dur": 6.0, "fps": 10.0, "width": 320, "height": 240,
                "vcodec": "h264", "has_audio": True, "has_subs": False}
        return S.build_digest(slug="ref", rel_file="study/ref/ref.mp4",
                              source_name="ref.mp4", sha256="sha256:abc0000000000000",
                              meta=meta, cuts=[2.0, 4.0], silences=[[0.0, 1.0]],
                              frames=[{"file": "f01.jpg", "t": 0.5}],
                              params={"cut_threshold": 0.3})

    def test_digest_has_full_tables(self):
        d = self._digest()
        for k in ("slug", "file", "sha256", "at", "params", "media",
                  "rhythm", "cuts", "shots", "silences", "frames"):
            self.assertIn(k, d)
        self.assertEqual(len(d["shots"]), 3)

    def test_contract_entry_keeps_pointers_and_counts_only(self):
        e = S.contract_entry(self._digest(), title="参考片", rel_dir="study/ref",
                             subs=None)
        for heavy in ("cuts", "shots", "silences", "frames"):
            self.assertNotIn(heavy, e)          # 全表随片长线性膨胀，绝不进契约
        self.assertEqual(e["digest"], "study/ref/digest.json")
        self.assertEqual(e["frames_dir"], "study/ref/frames")
        self.assertEqual(e["n_frames"], 1)
        self.assertEqual(e["rhythm"]["n_cuts"], 2)

    def test_digest_and_entry_are_valid_json_no_nan(self):
        # NaN/Infinity 不是合法 JSON：落进 project.json 会让 Studio 整页 JSON.parse 崩
        d = self._digest()
        e = S.contract_entry(d, title="x", rel_dir="study/ref", subs=None)
        json.dumps(d, allow_nan=False)
        json.dumps(e, allow_nan=False)

    def test_finite_guard(self):
        self.assertIsNone(S._finite(float("inf")))
        self.assertIsNone(S._finite(float("nan")))
        self.assertIsNone(S._finite("3"))
        self.assertIsNone(S._finite(True))
        self.assertEqual(S._finite(3), 3.0)


class TestProbeArgs(unittest.TestCase):
    """形态守卫：这几条 flag 经实测标定，改动须重新标定。"""

    def test_cut_args(self):
        a = S.cut_args("/x/r.mp4", 0.3)
        self.assertIn("-an", a)                                   # 不解音频，快一大截
        self.assertIn("select='gt(scene,0.300)',showinfo", a)

    def test_silence_args_has_vn(self):
        # 无音轨且不加 -vn 时 ffmpeg 退出 0 且静默无输出 → 被误判成「全程有声」
        self.assertIn("-vn", S.silence_args("/x/r.mp4"))
        self.assertIn("silencedetect=n=-30dB:d=0.4", S.silence_args("/x/r.mp4"))

    def test_frame_args_seeks_before_input(self):
        a = S.frame_args("/x/r.mp4", 3.0, "/o/f01.jpg")
        self.assertLess(a.index("-ss"), a.index("-i"))            # 输入前 = 关键帧快跳
        self.assertEqual(a[a.index("-frames:v") + 1], "1")

    def test_subs_args_first_stream(self):
        self.assertIn("0:s:0", S.subs_args("/x/r.mp4", "/o/subs.srt"))


class TestSlugify(unittest.TestCase):
    def test_ascii_and_dedup(self):
        self.assertEqual(S.slugify("Ref Clip 01.mp4"), "ref-clip-01-mp4")
        self.assertEqual(S.slugify("参考片"), "ref")               # 中文归一为空即回落
        self.assertEqual(S.slugify("ref", {"ref"}), "ref-2")
        self.assertEqual(S.slugify("ref", {"ref", "ref-2"}), "ref-3")


# ============================================================================
# 版权护栏（本模块头号用例）
# ============================================================================
class TestRelativePathOnly(unittest.TestCase):
    """`study[]` 里的路径**只准是工作区相对路径**——绝对路径会被 `oss sync` 传上公网桶。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        d = self.ws / "p1" / "study" / "ref"
        (d / "frames").mkdir(parents=True)
        (d / "ref.mp4").write_bytes(b"fake-mp4")
        (d / "subs.srt").write_text("1\n", encoding="utf-8")
        (d / "digest.json").write_text("{}", encoding="utf-8")
        self.rel_doc = {"study": [{
            "slug": "ref", "file": "p1/study/ref/ref.mp4",
            "digest": "p1/study/ref/digest.json",
            "frames_dir": "p1/study/ref/frames", "subs": "p1/study/ref/subs.srt"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_media_ignores_reference_film(self):
        self.assertEqual(collect_media(self.rel_doc, self.ws), [])

    def test_absolute_path_would_be_collected(self):
        """对照组：证明相对路径是**唯一**拦住上云的东西（改成绝对即事故复现）。"""
        abs_doc = {"study": [{
            "file": str(self.ws / "p1" / "study" / "ref" / "ref.mp4"),
            "subs": str(self.ws / "p1" / "study" / "ref" / "subs.srt")}]}
        got = {p.name for p in collect_media(abs_doc, self.ws)}
        self.assertEqual(got, {"ref.mp4", "subs.srt"})

    def test_contract_entry_paths_are_relative(self):
        e = S.contract_entry(
            S.build_digest(slug="ref", rel_file="study/ref/ref.mp4",
                           source_name="a.mp4", sha256=None,
                           meta={"dur": 6.0, "has_audio": False}, cuts=[], silences=[],
                           frames=[], params={}),
            title="", rel_dir="study/ref", subs="study/ref/subs.srt")
        for k in ("file", "digest", "frames_dir", "subs"):
            self.assertFalse(e[k].startswith("/"), f"{k} 必须是工作区相对路径")

    def test_artifact_dir_has_no_work_suffix(self):
        """产物目录不得带 `_work`——`scanner.rglob('*_work')` 是片库扫描入口，
        带后缀参考片会被当成自家成片收进片库。"""
        e = S.contract_entry(
            S.build_digest(slug="ref", rel_file="study/ref/ref.mp4", source_name="a.mp4",
                           sha256=None, meta={"dur": 1.0}, cuts=[], silences=[],
                           frames=[], params={}),
            title="", rel_dir="study/ref", subs=None)
        for k in ("file", "digest", "frames_dir"):
            self.assertNotIn("_work", e[k])


class TestRejectNonMedia(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("读片", pid="stu", profile="hd2d")
        self.junk = Path(self.tmp.name) / "note.txt"
        self.junk.write_text("这不是视频", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_missing_file(self):
        with self.assertRaises(ProjectError):
            self.s.ingest_study(Path(self.tmp.name) / "nope.mp4")

    def test_wrong_extension(self):
        with self.assertRaises(ProjectError):
            self.s.ingest_study(self.junk)
        self.assertFalse((self.s.dir / "study").exists())      # 拒收即零副作用

    @unittest.skipUnless(_HAS_FFMPEG, "需要 ffprobe")
    def test_fake_video_extension(self):
        # 后缀对但内容不是视频 → ffprobe 解不开 → 拒收（不许留半截目录）
        fake = Path(self.tmp.name) / "fake.mp4"
        fake.write_bytes(b"definitely not a video" * 20)
        with self.assertRaises(ProjectError):
            self.s.ingest_study(fake)

    def test_rm_unknown_slug(self):
        with self.assertRaises(ProjectError):
            self.s.remove_study("nope")


@unittest.skipUnless(_HAS_FFMPEG, "需要 ffmpeg/ffprobe")
class TestIngestSmoke(unittest.TestCase):
    """端到端：合成一支「三段硬切 + 有声」的样片，跑完整读片链路。"""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.src = Path(cls._dir.name) / "demo-ref.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=red:s=160x120:d=2:r=10",
               "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=2:r=10",
               "-f", "lavfi", "-i", "color=c=green:s=160x120:d=2:r=10",
               "-f", "lavfi", "-i", "sine=f=440:d=6:r=44100",
               "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
               "-map", "[v]", "-map", "3:a", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.src)]
        cls.ok = subprocess.run(cmd, capture_output=True).returncode == 0

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def setUp(self):
        if not self.ok:
            self.skipTest("样片合成失败")
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("读片", pid="stu", profile="hd2d")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_end_to_end(self):
        e = self.s.ingest_study(self.src, frames=4, title="参考片A")
        root = self.s.dir / "study" / e["slug"]
        self.assertTrue((root / "ref.mp4").is_file())
        self.assertTrue((root / "digest.json").is_file())
        self.assertEqual(e["n_frames"], 4)
        self.assertEqual(len(list((root / "frames").glob("*.jpg"))), 4)
        # 三段硬切 = 2 刀 3 镜，每镜 2s
        self.assertEqual(e["rhythm"]["n_cuts"], 2)
        self.assertEqual(e["rhythm"]["n_shots"], 3)
        self.assertAlmostEqual(e["rhythm"]["avg_shot_sec"], 2.0, delta=0.2)
        self.assertEqual(e["rhythm"]["cuts_per_min"], 20.0)
        # 正弦音轨全程有声 → 静音占比 0（不是 None）
        self.assertEqual(e["rhythm"]["silence_ratio"], 0.0)
        digest = json.loads((root / "digest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(digest["shots"]), 3)
        self.assertEqual(len(digest["cuts"]), 2)

    def test_saved_doc_is_strict_json_and_relative(self):
        self.s.ingest_study(self.src, frames=2)
        doc = self.ws.get_project("stu").data
        json.dumps(doc, allow_nan=False)                  # 无 NaN/Infinity
        entry = doc["study"][0]
        self.assertFalse(entry["file"].startswith("/"))
        self.assertEqual(collect_media(doc, self.ws.root), [])   # 绝不进上传清单

    def test_reimport_same_slug_is_idempotent(self):
        a = self.s.ingest_study(self.src, frames=4, slug="ref")
        b = self.s.ingest_study(self.src, frames=2, slug="ref")
        entries = self.ws.get_project("stu").data["study"]
        self.assertEqual(len(entries), 1)                 # 覆盖重算，不堆版本栈
        self.assertEqual((a["slug"], b["slug"]), ("ref", "ref"))
        self.assertEqual(b["n_frames"], 2)
        # 上一版残帧必须清干净（f03/f04 不许留下）
        frames = sorted(p.name for p in (self.s.dir / "study" / "ref" / "frames").glob("*"))
        self.assertEqual(frames, ["f01.jpg", "f02.jpg"])

    def test_auto_slug_dedup_keeps_both(self):
        a = self.s.ingest_study(self.src, frames=1)
        b = self.s.ingest_study(self.src, frames=1)
        self.assertNotEqual(a["slug"], b["slug"])
        self.assertEqual(len(self.ws.get_project("stu").data["study"]), 2)

    def test_external_subs_copied_in(self):
        srt = Path(self.tmp.name) / "ext.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n参考台词\n", encoding="utf-8")
        e = self.s.ingest_study(self.src, frames=1, slug="ref", subs=srt)
        self.assertEqual(e["subs"], "study/ref/subs.srt")
        self.assertTrue((self.s.dir / "study" / "ref" / "subs.srt").is_file())

    def test_missing_subs_rejected_before_any_copy(self):
        """缺 --subs 文件必须在**拷贝之前**拒收——否则盘上会留一份没登记的第三方片子。"""
        with self.assertRaises(ProjectError):
            self.s.ingest_study(self.src, frames=1, slug="ref",
                                subs=Path(self.tmp.name) / "nope.srt")
        self.assertFalse((self.s.dir / "study" / "ref").exists())
        self.assertEqual(self.ws.get_project("stu").data.get("study", []), [])

    def test_frames_zero_keeps_numbers_only(self):
        e = self.s.ingest_study(self.src, frames=0, slug="ref")
        self.assertEqual(e["n_frames"], 0)
        self.assertEqual(e["rhythm"]["n_cuts"], 2)          # 数照样量得出
        self.assertEqual(list((self.s.dir / "study" / "ref" / "frames").glob("*")), [])

    def test_remove_wipes_local_copy(self):
        e = self.s.ingest_study(self.src, frames=1, slug="ref")
        self.s.remove_study(e["slug"])
        self.assertFalse((self.s.dir / "study" / "ref").exists())
        self.assertEqual(self.ws.get_project("stu").data["study"], [])


if __name__ == "__main__":
    unittest.main()
