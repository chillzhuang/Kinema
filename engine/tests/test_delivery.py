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

"""配音表现力契约守卫——**本模块跑在音画同步生命线上**。

四条不许倒退的规矩：

1. **旁白轨拼接序列单一真源**：`voicecast.narration_parts` 同时供 `cli.stage_tts`
   与 `pipeline.compose._sync_narration`。两边一旦分叉，合成阶段会用「不含停顿」的
   序列把 tts 插好的停顿整段抹掉，还打印「已按有效分镜自动重拼」把破坏伪装成修复。
2. **停顿按 motion 门控·写读两侧对称**：写侧 `shot_pauses` 只让 kenburns 把
   `delivery.pause_*` 折进 dur；读侧 `request_seconds` 保证 gen-video 请求的秒数
   永远回到净配音时长。**只有写侧不够**——dur 是持久化字段，而主推顺序正是
   「先 kenburns 过节奏审 → 再切 dubbed 动态化」，切模式那一刻 dur 里已经折着停顿，
   照发即按无声空转向 Seedance 计费，成片里还多出等长的静默死区。
3. **dur 折算幂等**：从 probe 实测重算，绝不在旧 dur 上累加（tts 每跑一次都刷新 dur）；
   读侧同理——gen-video 把买下的整秒回填进 dur，连跑多次既不许越跑越短、也不许越跑越长。
4. **绝不回写 narration**：emphasis/note 只编译成喂 provider 的派生文本；标签进台词会
   同时炸字幕、炸官方音色朗读、炸按字数计费三处。
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import review
from kinema import voicecast
from kinema.project import Project
from kinema.providers.base import VideoProvider
from tests.support import fake_path


class _Res:
    def __init__(self, path, cost=0.0):
        self.path, self.cost = str(path), cost


def _shot(sid, **kw) -> dict:
    s = {"id": sid, "narration": f"第{sid}镜台词", "dur": 2.0}
    s.update(kw)
    return s


def _frontend_src():
    """Studio 前端全量源码（app/ 分片 + app.js 入口，按加载序拼接）——
    源级守卫一律读拼接文本：分片是纯架构切分，断言不该关心代码落在哪一片。"""
    import kinema
    assets = Path(kinema.__file__).parent / "studio_app"
    parts = sorted((assets / "app").glob("*.js")) + [assets / "app.js"]
    return "".join(p.read_text(encoding="utf-8") for p in parts)


class _Ctx:
    """临时章节工作区：project.json + <stem>_work/audio/shot_<id>.wav 占位文件。

    narration_parts 只判 `is_file()`（真实时长由 tts 侧 probe 决定），
    所以占位空文件足够，全程零 ffmpeg、离线可跑。
    """

    def __init__(self, data: dict, wavs=()):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.path = root / "ch01.json"
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.project = Project(self.path, data)
        self.adir = self.project.workdir / "audio"
        self.adir.mkdir(parents=True, exist_ok=True)
        for sid in wavs:
            (self.adir / f"shot_{sid}.wav").write_bytes(b"RIFFfake")

    def wav(self, sid) -> str:
        return str(self.adir / f"shot_{sid}.wav")

    def close(self):
        self.tmp.cleanup()


def _doc(motion="kenburns") -> dict:
    """标准样本：停顿镜 + 纯画面镜 + 弃用镜 + 普通镜。

    kenburns 下 tts 已折算：镜1 dur = 配音 1.0 + 前 0.5 + 后 0.3 = 1.8，
    镜4 dur = 配音 2.0 + 尾留白 0.25；dubbed/native 的 dur 是窗口，不折停顿。
    """
    return {
        "motion": motion,
        "shots": [
            _shot(1, dur=1.8, delivery={"pause_before": 0.5, "pause_after": 0.3}),
            _shot(2, narration="", caption="纯画面", dur=2.0),
            _shot(3, dur=9.9, review={"shot": {"state": "omt"}}),
            _shot(4, dur=2.25 if motion == "kenburns" else 2.0),
        ],
    }


class TestNarrationParts(unittest.TestCase):
    """拼接序列本身 + 「tts 与 compose 共用同一条」的防分叉守卫。"""

    def setUp(self):
        self.ctx = _Ctx(_doc(), wavs=(1, 3, 4))
        self.addCleanup(self.ctx.close)

    def test_sequence_has_pause_padding_and_skips_omt(self):
        parts, _seg, missing = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        self.assertEqual(parts, [
            ("silence", 0.5),                 # 镜1 前停顿
            ("file", self.ctx.wav(1)),
            ("silence", 0.3),                 # 镜1 后停顿
            ("silence", 2.0),                 # 镜2 纯画面镜等长占位
            ("file", self.ctx.wav(4)),        # 镜3 已弃用(omt)，整条不进轨
            ("silence", voicecast.TAIL_ROLL),  # 镜4 尾留白
        ])
        self.assertEqual(missing, [])

    def test_total_matches_timeline(self):
        """轨长（停顿计入）必须等于 Σdur——不等就会触发 compose 的自愈重拼。"""
        parts, _seg, _m = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        speech = {1: 1.0, 4: 2.0}             # 假想 probe 实测值
        total = sum(v if k == "silence" else speech[int(Path(v).stem.split("_")[1])]
                    for k, v in parts)
        self.assertAlmostEqual(total, self.ctx.project.total_duration(), places=3)

    def test_missing_wav_reported_not_silently_dropped(self):
        ctx = _Ctx(_doc(), wavs=(1,))         # 镜4 的 wav 缺失
        self.addCleanup(ctx.close)
        parts, _seg, missing = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(missing, [4])
        self.assertNotIn(("file", ctx.wav(4)), parts)

    def test_audio_file_field_wins_over_convention(self):
        """路径解析以 `shots[].audio_file` 优先（版本回滚后画布路径可能不同名）。"""
        alt = self.ctx.adir / "custom.wav"
        alt.write_bytes(b"RIFFfake")
        self.ctx.project.shots[0]["audio_file"] = str(alt)
        parts, _seg, _m = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        self.assertIn(("file", str(alt)), parts)

    def test_uploaded_url_field_falls_back_to_convention(self):
        """audio_file 已上云（OSS URL 字符串）→ 必须走 shot_audio_path 同一条
        候选链回落约定路径——把 URL 判成缺失的话，明明在盘的 wav 会让整镜
        被踢出旁白轨、后续镜的语音集体前移。"""
        self.ctx.project.shots[0]["audio_file"] = "https://oss.example.com/t/shot_1.wav"
        parts, _seg, missing = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        self.assertEqual(missing, [])
        self.assertIn(("file", self.ctx.wav(1)), parts)

    def test_compose_selfheal_uses_the_same_sequence(self):
        """**防自毁核心用例**：compose 自愈重拼必须原样吃 narration_parts 的序列。

        另写一份「无旁白镜插静音」的简化逻辑 → 停顿垫片被整段抹掉，
        而日志还会打印「已按有效分镜自动重拼」，破坏被伪装成修复。"""
        from kinema.pipeline import compose
        narration = self.ctx.adir / "narration.wav"
        narration.write_bytes(b"RIFFfake")
        expect, _seg, _m = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        seen = {}
        with mock.patch.object(compose, "probe_duration", lambda p: 99.0), \
             mock.patch.object(compose, "concat_audio",
                               lambda parts, out, **kw: seen.update(parts=parts, out=out)):
            compose._sync_narration(self.ctx.project, narration)
        self.assertEqual(seen["parts"], expect)
        self.assertIn(("silence", 0.5), seen["parts"])   # 停顿没被抹掉

    def _rebuild(self, doc, wavs=(1,)):
        """零漂移下走一遍自愈，返回 (是否重拼, 返回值)。"""
        from kinema.pipeline import compose
        ctx = _Ctx(doc, wavs=wavs)
        self.addCleanup(ctx.close)
        narration = ctx.adir / "narration.wav"
        narration.write_bytes(b"RIFFfake")
        total = ctx.project.total_duration()
        seen = {}
        with mock.patch.object(compose, "probe_duration", lambda p: total), \
             mock.patch.object(compose, "concat_audio",
                               lambda parts, out, **kw: seen.update(parts=parts)), \
             mock.patch.object(voicecast, "probe_duration", lambda p: 1.0):
            out = compose._sync_narration(ctx.project, narration)
        return "parts" in seen, out

    def test_seedance_modes_rebuild_the_track_even_at_zero_drift(self):
        """native 的对白镜整段让位静音、dubbed 的开口对齐取自底片声轨——两档的
        窗口贡献都恒等于 dur，盘上那条轨可以与时间轴等长而内容早已不符。"""
        doc = {"motion": "native", "native_voiceover": True,
               "shots": [_shot(1, speaker="凯尔", narration="走。", dur=2.0),
                         _shot(2, narration="夜里起了风。", dur=2.0)]}
        rebuilt, out = self._rebuild(doc, wavs=(1, 2))
        self.assertTrue(rebuilt, "native 混烧零漂移也必须重拼")
        self.assertIsNotNone(out)

    def test_track_without_any_voice_part_is_not_burned(self):
        """一条本章配音都不进轨：盘上那条 narration.wav 与当前分镜无关，
        烧它就是烧陈旧人声。"""
        doc = {"motion": "native", "native_voiceover": True,
               "shots": [_shot(1, speaker="凯尔", narration="走。", dur=2.0)]}
        _rebuilt, out = self._rebuild(doc, wavs=(1,))
        self.assertIsNone(out)

    def test_a_burn_shot_without_audio_is_named_not_silently_skipped(self):
        """混烧下旁白镜的人声就是那条 wav：缺了即成片那一段无人说话而字幕照烧。
        缺省不烧的 native 则相反——旁白镜没有 wav 是常态，报 missing 会让
        compose 的自愈误判成「无从重拼」而整章放弃。"""
        shots = [_shot(1, speaker="旁白", narration="第一句旁白。", dur=6.0),
                 _shot(2, speaker="陈默", narration="角色台词。", dur=6.0),
                 _shot(3, speaker="旁白", narration="第二句旁白。", dur=6.0)]
        burn = {"motion": "native", "native_voiceover": True, "shots": shots}
        ctx = _Ctx(burn, wavs=(1,))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration", return_value=6.0):
            _p, _s, missing = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(missing, [3])

        loose = _Ctx({"motion": "native", "shots": shots}, wavs=())
        self.addCleanup(loose.close)
        with mock.patch.object(voicecast, "probe_duration", return_value=6.0):
            _p, _s, missing = voicecast.narration_parts(loose.project, loose.adir)
        self.assertEqual(missing, [], "缺省不烧的 native 不该被点名")

    def test_missing_audio_is_announced_even_at_zero_drift(self):
        """漏配音的旁白镜由静音占满窗口，总时长分毫不差——按偏差门控告警
        就成了静默烧一条有洞的轨。"""
        from kinema.pipeline import compose
        ctx = _Ctx({"motion": "native", "native_voiceover": True,
                    "shots": [_shot(1, speaker="旁白", narration="漏配音。", dur=6.0)]},
                   wavs=())
        self.addCleanup(ctx.close)
        narration = ctx.adir / "narration.wav"
        narration.write_bytes(b"RIFFfake")
        buf = io.StringIO()
        with mock.patch.object(compose, "probe_duration", lambda p: 6.0), \
             mock.patch.object(compose, "concat_audio",
                               lambda parts, out, **kw: self.fail("缺配音时不许重拼")), \
             contextlib.redirect_stdout(buf):
            out = compose._sync_narration(ctx.project, narration)
        self.assertEqual(out, narration, "无从自愈时原样保留盘上那条轨")
        self.assertIn("镜 1", buf.getvalue())
        self.assertIn("tts --only 1", buf.getvalue())

    def test_a_time_fitted_track_still_burns(self):
        """配音超窗只落 ("fit", …)——按段类型枚举判「有没有人声」会把这类章
        整条轨丢掉，而它恰是台词写满的混烧章的常态。"""
        doc = {"motion": "native", "native_voiceover": True,
               "shots": [_shot(1, narration="夜里起了很大的风。", dur=1.0)]}
        from kinema.pipeline import compose
        ctx = _Ctx(doc, wavs=(1,))
        self.addCleanup(ctx.close)
        narration = ctx.adir / "narration.wav"
        narration.write_bytes(b"RIFFfake")
        seen = {}
        with mock.patch.object(compose, "probe_duration", lambda p: 1.0), \
             mock.patch.object(compose, "concat_audio",
                               lambda parts, out, **kw: seen.update(parts=parts)), \
             mock.patch.object(voicecast, "probe_duration", lambda p: 6.0):
            out = compose._sync_narration(ctx.project, narration)
        self.assertIsNotNone(out)
        self.assertTrue(any(k == "fit" for k, _ in seen["parts"]))


class TestStageTtsEndToEnd(unittest.TestCase):
    """stage_tts 收尾纪律的 CLI mock 全链路守卫（LocalBackendEnv + mock provider）。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmpd = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpd.name)

    def tearDown(self):
        self.tmpd.cleanup()
        self.env.restore()

    def _run(self, *argv):
        from kinema.cli import build_parser
        args = build_parser().parse_args(list(argv))
        return args.func(args)

    def _chapter(self, shots):
        ws = str(self.tmp / "ws")
        self._run("project", "new", "--title", "T", "--id", "t",
                  "--profile", "narration", "--workspace", ws)
        self._run("chapter", "new", "t", "--title", "C", "--workspace", ws)
        cf = Path(ws) / "t" / "chapters" / "ch01.json"
        doc = json.loads(cf.read_text(encoding="utf-8"))
        doc["motion"] = "kenburns"
        doc["shots"] = shots
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return ws, cf

    def test_only_run_with_missing_wavs_refuses_to_overwrite_track(self):
        """kenburns 下 `--only` 合成部分镜、其余台词镜 wav 不在盘 → 收尾必须
        点名拒拼，绝不写出缺镜的短轨覆盖 narration.wav（那些镜可能早已过审，
        review 闸看的是审阅态、不知道音轨被换过）。已合成镜的产物保留。"""
        from kinema.errors import KinemaError
        ws, cf = self._chapter([
            {"id": 1, "dur": 2.0, "narration": "第一句"},
            {"id": 2, "dur": 2.0, "narration": "第二句"}])
        adir = cf.parent / "ch01_work" / "audio"
        adir.mkdir(parents=True, exist_ok=True)
        keep = adir / "narration.wav"
        keep.write_bytes(b"RIFForiginal")          # 现有完整轨的哨兵
        with self.assertRaises(KinemaError) as cm:
            self._run("tts", "--chapter", "t/ch01", "--mock",
                      "--only", "1", "--workspace", ws)
        self.assertIn("2", str(cm.exception), "要点名缺 wav 的镜号")
        self.assertEqual(keep.read_bytes(), b"RIFForiginal",
                         "narration.wav 不得被缩水覆盖")
        self.assertTrue((adir / "shot_1.wav").is_file(), "已合成镜的产物保留")

    def test_line_dur_backfill_accepts_narration_key(self):
        """lines[] 的句用 `narration` 键写台词（voicecast.line_text 认它）时，
        dur 回填必须与 shot_lines 同判据——自写只认 text 的过滤会 zip 截断：
        第一句永远无 dur、第二句拿走第一句的时长，音轨全对而字幕从头错位。"""
        ws, cf = self._chapter([
            {"id": 1, "dur": 6.0, "lines": [
                {"speaker": "甲", "narration": "你来了。"},
                {"speaker": "乙", "text": "我来了。"}]}])
        self._run("tts", "--chapter", "t/ch01", "--mock", "--workspace", ws)
        lines = json.loads(cf.read_text(encoding="utf-8"))["shots"][0]["lines"]
        self.assertTrue(all(isinstance(ln.get("dur"), (int, float)) for ln in lines),
                        f"两句都要有实测 dur：{lines}")


class TestDurIdempotent(unittest.TestCase):
    """dur = probe + 停顿，**重算而非累加**——tts 每跑一次都会刷新这一行。"""

    def test_repeated_tts_does_not_stack_pauses(self):
        s = _shot(1, delivery={"pause_before": 0.5, "pause_after": 0.3})
        for _ in range(5):                    # 连跑五次 tts（probe 实测恒为 1.0）
            s["dur"] = voicecast.shot_duration(s, 1.0, "kenburns")
        self.assertEqual(s["dur"], 1.8)

    def test_dubbed_dur_is_pure_speech(self):
        s = _shot(1, delivery={"pause_before": 0.5, "pause_after": 0.3})
        self.assertEqual(voicecast.shot_duration(s, 1.0, "dubbed"), 1.0)

    def test_dur_is_finite_json_safe(self):
        """NaN/Infinity 不是合法 JSON——落进 dur 会让 Studio 整页 JSON.parse 崩。"""
        s = _shot(1, delivery={"pause_before": float("nan"), "pause_after": float("inf")})
        d = voicecast.shot_duration(s, float("nan"), "kenburns")
        self.assertEqual(d, voicecast.TAIL_ROLL)
        json.dumps({"dur": d}, allow_nan=False)          # 不抛即合法

    def test_tts_backfill_yields_to_clip_measured_dur(self):
        """dur 真源随阶段移交：片段在盘后归片段实测，tts 不得按配音实测回退。

        换音色正路 `tts --force → assemble` 依赖这一条——若覆写，配音轨通常比
        片段短，assemble 会把每镜视频尾部裁掉（6 镜共缩 5.2s）。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_tts)
        self.assertIn('if motion != "native" and not (s.get("clip") or s.get("clips")):',
                      src, "dubbed 的 dur 覆写必须带「片段已在盘」豁免")


class TestPauseModeGate(unittest.TestCase):
    """停顿只在 kenburns 生效——dubbed/native 折算 = 直接多计费。"""

    def test_pauses_zero_outside_kenburns(self):
        s = _shot(1, delivery={"pause_before": 0.5, "pause_after": 0.3})
        self.assertEqual(voicecast.shot_pauses(s, "kenburns"), (0.5, 0.3))
        self.assertEqual(voicecast.shot_pauses(s, "dubbed"), (0.0, 0.0))
        self.assertEqual(voicecast.shot_pauses(s, "native"), (0.0, 0.0))
        # 声明值与生效值分离：作者写了什么照样读得到（供 CLI 提示「本模式下不生效」）
        self.assertEqual(voicecast.declared_pauses(s), (0.5, 0.3))

    def test_no_pause_padding_in_dubbed_track(self):
        ctx = _Ctx(_doc(motion="dubbed"), wavs=(1, 3, 4))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration",
                               lambda p: {"shot_1.wav": 1.0,
                                          "shot_4.wav": 2.0}[Path(p).name]):
            parts, _seg, _m = voicecast.narration_parts(ctx.project, ctx.adir)
        # 停顿垫片（0.5/0.3）不进 dubbed；窗口垫齐（1.8-1.0=0.8）是另一回事——
        # 那是片段实测窗口的对齐量，不是 delivery 停顿
        self.assertEqual(parts, [("file", ctx.wav(1)),
                                 ("silence", 0.8),        # 垫齐窗口，非停顿垫片
                                 ("silence", 2.0),        # 纯画面镜占位仍在
                                 ("file", ctx.wav(4))])

    def test_gate_saves_real_money(self):
        """把停顿折进 dubbed 的 dur → Seedance 按 ceil 秒计费，每镜无效购买整秒无声。"""
        from kinema.providers.video.seedance import SeedanceProvider
        prov = SeedanceProvider({}, None)
        s = _shot(1, delivery={"pause_before": 0.6, "pause_after": 0.4})
        gated = voicecast.shot_duration(s, 5.2, "dubbed")          # 门控后 = 纯配音
        naive = round(5.2 + 0.6 + 0.4, 2)                          # 不门控（错误写法）
        self.assertEqual(prov.billable_seconds(gated, dubbed=True), 6)
        self.assertEqual(prov.billable_seconds(naive, dubbed=True), 7)   # 无效计费 1 元/镜

    def test_pause_clamped_and_sanitized(self):
        mk = lambda pb, pa: _shot(1, delivery={"pause_before": pb, "pause_after": pa})  # noqa: E731
        self.assertEqual(voicecast.declared_pauses(mk(99, -3)), (voicecast.MAX_PAUSE, 0.0))
        self.assertEqual(voicecast.declared_pauses(mk("慢一点", None)), (0.0, 0.0))
        self.assertEqual(voicecast.declared_pauses(_shot(1)), (0.0, 0.0))
        self.assertEqual(voicecast.declared_pauses(_shot(1, delivery="乱写")), (0.0, 0.0))


class TestTailTreatment(unittest.TestCase):
    """句尾处理：seed-audio 的输出在末字后 0.1s 内截止、末音节被切，拼轨淡出 +
    kenburns 尾留白把它收成自然收尾。尾留白与停顿同一道门控——dubbed/native 一秒都不多买。"""

    def test_kenburns_pause_after_has_a_floor(self):
        self.assertEqual(voicecast.shot_pauses(_shot(1), "kenburns"), (0.0, voicecast.TAIL_ROLL))
        short = _shot(1, delivery={"pause_after": 0.1})
        self.assertEqual(voicecast.shot_pauses(short, "kenburns"), (0.0, voicecast.TAIL_ROLL))
        long = _shot(1, delivery={"pause_before": 0.6, "pause_after": 0.4})
        self.assertEqual(voicecast.shot_pauses(long, "kenburns"), (0.6, 0.4))
        self.assertEqual(voicecast.shot_duration(_shot(1), 1.0, "kenburns"),
                         round(1.0 + voicecast.TAIL_ROLL, 2))

    def test_floor_never_reaches_paid_modes(self):
        for motion in ("dubbed", "native"):
            self.assertEqual(voicecast.shot_pauses(_shot(1), motion), (0.0, 0.0))
            self.assertEqual(voicecast.shot_duration(_shot(1), 5.2, motion), 5.2)

    def test_concat_audio_fades_every_voice_segment(self):
        from kinema import ffmpeg as ff
        seen = {}
        with mock.patch.object(ff, "run", lambda args, **kw: seen.update(args=args)), \
                mock.patch.object(ff, "probe_duration", lambda p: 2.0):
            ff.concat_audio([("file", "/x/a.wav"), ("silence", 0.25),
                             ("cut", ("/x/b.wav", 0.3)), ("fit", ("/x/c.wav", 1.5))],
                            "/x/o.wav", tail_fade=voicecast.TAIL_FADE)
        graph = seen["args"][seen["args"].index("-filter_complex") + 1]
        fade = "areverse,afade=t=in:d=0.070,areverse"
        chains = graph.split(";")[:-1]
        self.assertEqual([fade in c for c in chains], [True, False, True, True])
        self.assertLess(chains[2].index("asetpts=PTS-STARTPTS"), chains[2].index(fade))
        self.assertLess(chains[3].index("atrim=0:1.500"), chains[3].index(fade))
        seen.clear()
        with mock.patch.object(ff, "run", lambda args, **kw: seen.update(args=args)):
            ff.concat_audio([("file", "/x/a.wav")], "/x/o.wav")
        self.assertNotIn("afade", seen["args"][seen["args"].index("-filter_complex") + 1])

    def test_custom_lines_carry_a_tail_guard(self):
        """seed-audio 只在整段末端截音：台词后垫一句保护词，真句子才能完整收尾。"""
        from kinema import voicebank
        cast = {"owner": "旁白", "prompt": "低音区偏暗"}
        self.assertTrue(voicebank.line_prompt(cast, "不要回答。").endswith("说道：“不要回答。好。”"))
        def w(text, start, end):
            return {"text": text, "start": start, "end": end}
        # 冒号结尾的台词：模型把保护词并进同一句，只有词级时间戳分得开
        merged = [{"text": "书里写道：好。", "start": 0.28, "end": 2.92, "words": [
            w("书", 0.28, 0.5), w("里", 0.5, 0.8), w("写", 0.8, 1.04), w("道", 1.04, 1.16),
            w("：", 1.16, 1.16), w("好", 2.56, 2.92), w("。", 2.92, 2.92)]}]
        self.assertEqual(voicebank.guard_cut(merged), 1.46)               # 道.end + TAIL_KEEP
        merged[0]["words"][5]["start"] = 1.4
        self.assertEqual(voicebank.guard_cut(merged), 1.35)               # 不越过保护词起点
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            voicebank.guard_cut([{"text": "不要回答。好。", "start": 0.0, "end": 3.8}])

    def test_narration_tracks_pass_the_fade(self):
        from kinema import cli
        from kinema.pipeline import compose
        self.assertEqual(inspect.getsource(cli.stage_tts).count("tail_fade=voicecast.TAIL_FADE"), 2)
        self.assertIn("tail_fade=voicecast.TAIL_FADE",
                      inspect.getsource(compose._sync_narration))


class TestProviderAudioIsPcm(unittest.TestCase):
    """provider 回吐的音频落盘即归一成 PCM：无 Xing 头的 mp3 按码率估时长比解码多一帧，
    dur 逐镜多 48 ms，整轨漂移就是从这里攒出来的。"""

    def test_synth_normalizes_every_segment(self):
        from kinema import cli
        self.assertIn('to_pcm(seg["wav"], end=voicebank.guard_cut(res.segments) if seg["custom"] else None)',
                      inspect.getsource(cli.stage_tts))

    def test_to_pcm_end_truncates(self):
        from kinema import ffmpeg as ff
        if shutil.which("ffmpeg") is None:
            self.skipTest("需要 ffmpeg")
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "shot_2.wav"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000:duration=2",
                            "-c:a", "pcm_s16le", str(wav)], check=True)
            ff.to_pcm(wav, end=1.25)
            self.assertAlmostEqual(ff.probe_duration(wav), 1.25, delta=0.01)

    def test_to_pcm_rewrites_in_place_as_pcm(self):
        from kinema import ffmpeg as ff
        if shutil.which("ffmpeg") is None or "libmp3lame" not in subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout:
            self.skipTest("需要 ffmpeg + libmp3lame")
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "shot_1.wav"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000:duration=1",
                            "-c:a", "libmp3lame", "-b:a", "64k", "-write_xing", "0",
                            "-f", "mp3", str(wav)], check=True)
            ff.to_pcm(wav)
            codec = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                                    "-of", "csv=p=0", str(wav)], capture_output=True, text=True).stdout.strip()
            self.assertEqual(codec, "pcm_s16le")
            self.assertAlmostEqual(ff.probe_duration(wav), 1.0, delta=0.06)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["shot_1.wav"])


class TestRequestSecondsReadGate(unittest.TestCase):
    """**读侧对称闸**：gen-video 向 provider 请求的秒数一律走 `request_seconds`。

    写侧 `shot_pauses` 只管得住「写 dur 那一刻」，而 dur 是持久化字段——
    主推的节点顺序恰恰是「先 kenburns 出样片过节奏审 → 再 gen-video --dubbed 动态化」，
    切模式那一刻盘上的 dur 已含停顿。没有这道闸，停顿就原样计费给 Seedance，
    并在成片里变成同等长度的静默死区（还会被 probe 回填成正式时长、拉长字幕窗口）。
    """

    def test_kenburns_folded_dur_does_not_reach_seedance(self):
        """复现原案：kenburns 折算过的 dur 进 dubbed，请求秒数必须回到纯配音。"""
        from kinema.providers.video.seedance import SeedanceProvider
        prov = SeedanceProvider({}, None)
        ctx = _Ctx({"motion": "dubbed", "shots": [_shot(1)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        s["delivery"] = {"pause_before": 2.0, "pause_after": 3.0}
        s["dur"] = voicecast.shot_duration(s, 1.2, "kenburns")     # tts 在 kenburns 下折算
        self.assertEqual(s["dur"], 6.2)
        with mock.patch.object(voicecast, "probe_duration", lambda p: 1.2):
            req = voicecast.request_seconds(s, "dubbed", adir=ctx.adir)
        self.assertEqual(req, 1.2)                                  # 净画面秒数 = ref_audio 长度
        self.assertEqual(prov.billable_seconds(req, dubbed=True), 4)         # 修好：4s
        self.assertEqual(prov.billable_seconds(s["dur"], dubbed=True), 7)    # 裸取 dur：无效计费 ¥3

    def test_native_picture_seconds_never_follow_the_voiceover(self):
        """**native 的画面秒数由 dur 说了算**（混烧后的语义，详
        `docs/agents/native-voiceover.md`）：模型原生配音，我们的 TTS 只是叠加轨——拿配音长度请求 Seedance 就是让「台词多长」
        决定「画面多长」。画面 5s 而配音 10.27s 的镜，照发即**多花一倍
        的钱**、片段节奏也与分镜设计完全不符。"""
        ctx = _Ctx({"motion": "native", "shots": [_shot(1, dur=5.0)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        with mock.patch.object(voicecast, "probe_duration", lambda p: 10.27):
            self.assertEqual(voicecast.request_seconds(s, "native", adir=ctx.adir), 5.0)
        # 短配音同理不许把画面缩短
        with mock.patch.object(voicecast, "probe_duration", lambda p: 2.66):
            self.assertEqual(voicecast.request_seconds(s, "native", adir=ctx.adir), 5.0)
        # dubbed 反过来：ref_audio 就是配音，对口型的真相本就是这条音轨的长度
        with mock.patch.object(voicecast, "probe_duration", lambda p: 10.27):
            self.assertEqual(voicecast.request_seconds(s, "dubbed", adir=ctx.adir), 10.27)

    def test_native_switch_also_sheds_the_pause(self):
        """kenburns 跑过 tts 再切 native 同样受影响——native 也走同一条读侧真源。

        与上一条的分界是**「dur 里到底有没有折过停顿」**：按「配音+声明停顿」
        反查能对上才认定是 kenburns 的历史残留、扣回净配音；对不上（native 原生
        设计的画面秒数）一律以 dur 为准。"""
        ctx = _Ctx({"motion": "native", "shots": [_shot(1)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        s["delivery"] = {"pause_before": 1.5, "pause_after": 0.5}
        s["dur"] = voicecast.shot_duration(s, 4.0, "kenburns")
        with mock.patch.object(voicecast, "probe_duration", lambda p: 4.0):
            self.assertEqual(voicecast.request_seconds(s, "native", adir=ctx.adir), 4.0)

    def test_no_wav_keeps_dur_verbatim(self):
        """没跑过 tts ⇒ 停顿从未折进 dur（作者手写/上一版片段实测）→ 不许再扣一次。"""
        ctx = _Ctx({"motion": "native", "shots": [_shot(1, dur=8.0)]})   # 不建 wav
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        s["delivery"] = {"pause_before": 2.0, "pause_after": 1.0}
        self.assertEqual(voicecast.request_seconds(s, "native", adir=ctx.adir), 8.0)

    def test_repeated_regen_is_idempotent(self):
        """gen-video 会把片段实测回填进 dur——连跑多次不许把镜越跑越短。"""
        ctx = _Ctx({"motion": "dubbed", "shots": [_shot(1)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        s["delivery"] = {"pause_before": 2.0, "pause_after": 3.0}
        s["dur"] = voicecast.shot_duration(s, 1.2, "kenburns")
        with mock.patch.object(voicecast, "probe_duration", lambda p: 1.2):
            for _ in range(5):
                req = voicecast.request_seconds(s, "dubbed", adir=ctx.adir)
                s["dur"] = round(req, 2)          # 模拟 probe(片段) 回填
        self.assertEqual(s["dur"], 1.2)

    def test_dubbed_design_window_survives_short_voice(self):
        """场→镜的长镜：dur 是设计出的表演窗口，台词只占一段——配音实测不许把
        窗口拉回台词长度（10s 主戏镜配 3.5s 台词，被压成 4s 就是砍掉表演区间）。"""
        ctx = _Ctx({"motion": "dubbed", "shots": [_shot(1, dur=10.0)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        with mock.patch.object(voicecast, "probe_duration", lambda p: 3.5):
            self.assertEqual(voicecast.request_seconds(s, "dubbed", adir=ctx.adir), 10.0)

    def test_tts_backfill_never_shrinks_a_dubbed_window(self):
        """写侧对称：dubbed 的 dur 回填只延不缩（配音超窗才撑大窗口），
        kenburns 照旧双向跟随配音+停顿。源级钉死分支在场。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_tts)
        self.assertIn('if motion == "dubbed":', src)
        self.assertIn('max(float(s.get("dur") or 0), new_dur)', src)

    def test_gen_video_pads_ref_audio_to_the_window(self):
        """发送侧垫窗：配音短于请求秒数时把 ref_audio 垫静音尾补齐——参考媒体
        模式下片段时长跟随音频，不垫窗口就被拉回台词长度。源级钉死。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertIn("apad=whole_dur=", src)
        self.assertIn("prov.billable_seconds(dur, dubbed=True)", src)

    def test_no_pause_projects_are_byte_identical_to_old_behaviour(self):
        """没写停顿的项目（=存量绝大多数）口径必须一字不改，否则这就是次生事故。"""
        ctx = _Ctx({"motion": "dubbed", "shots": [_shot(1, dur=5.0)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        with mock.patch.object(voicecast, "probe_duration", lambda p: 5.0):
            self.assertEqual(voicecast.request_seconds(s, "dubbed", adir=ctx.adir), 5.0)

    def test_audio_file_field_wins_and_broken_wav_falls_back(self):
        """路径解析与 narration_parts 同源；探测失败不许让整轮渲染炸掉。"""
        from kinema.errors import FFmpegError
        ctx = _Ctx({"motion": "dubbed", "shots": [_shot(1, dur=3.0)]}, wavs=(1,))
        self.addCleanup(ctx.close)
        s = ctx.project.shots[0]
        alt = ctx.adir / "custom.wav"
        alt.write_bytes(b"RIFFfake")
        s["audio_file"] = str(alt)
        seen = []
        with mock.patch.object(voicecast, "probe_duration",
                               lambda p: seen.append(str(p)) or 2.0):
            # 配音(2.0s)短于设计窗口(3.0s) → 窗口权威，余量是表演时间（发送侧垫窗）
            self.assertEqual(voicecast.request_seconds(s, "dubbed", adir=ctx.adir), 3.0)
        self.assertEqual(seen, [str(alt)])
        def _boom(p):
            raise FFmpegError("半截文件")
        with mock.patch.object(voicecast, "probe_duration", _boom):
            self.assertEqual(voicecast.request_seconds(s, "dubbed", adir=ctx.adir), 3.0)

    def test_kenburns_defensive_and_finite(self):
        s = _shot(1, dur=float("inf"), delivery={"pause_before": 1.0})
        self.assertEqual(voicecast.request_seconds(s, "kenburns"), 0.0)
        req = voicecast.request_seconds(s, "dubbed", speech_dur=float("nan"))
        self.assertEqual(req, 0.0)
        json.dumps({"dur": req}, allow_nan=False)        # 不抛即合法 JSON

    def test_both_gen_video_paths_share_the_read_gate(self):
        """**防口径分叉**：dry-run 报价与真发都必须调 request_seconds。

        两处一旦分叉，`--dry-run` 报的价就不是最终账单——而 dry-run 正是
        「烧钱前必看」这个节点的全部意义。"""
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertEqual(src.count("voicecast.request_seconds("), 2, src)
        # 裸取 dur 的写法不许留（留一处就是留一条绕过读侧闸的路）
        self.assertNotIn('float(s.get("dur") or project.data.get("duration"', src)
        # 在这一步才切模式的人也要看得见停顿失效告警（写侧那句只在跑 tts 时打得出来）
        self.assertIn("declared_pauses", src)


class TestClipDurWritebackIsWhatWeBought(unittest.TestCase):
    """gen-video 回填的 dur 是**本轮买下的整秒**，不是片段容器实测时长。

    厂商产物的容器恒比请求整秒多约一帧。回填实测会让读侧变成 4.1，dubbed 的
    `billable_seconds` 再 ceil 一次就买成 5s——每 retake 一次涨一秒且无上限。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    class _Prov(VideoProvider):
        name = "spy"
        prompt_lang = "zh"
        resolution = "720p"
        supports_ref_audio = True

        def __init__(self):
            self.bought = []

        def configured(self):
            return False, "口型精修未配置（本守卫只看图生视频那一步）"

        def generate(self, image, out_path, *, dur=5.0, **kw):
            # 买下的秒数由 provider 自己的档位算，测试不写死数字——档位改了
            # 这条守卫也不该误红
            self.bought.append(self.billable_seconds(dur, dubbed=bool(kw.get("ref_audio"))))
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"clip")
            return _Res(p)

    class _Router:
        force_mock = True

        def __init__(self, prov):
            self.prov = prov

        def resolve(self, capability, profile):
            return self.prov, {}

    class _Store:
        def canvas(self, aspect):
            return (1280, 720)

    def _run(self, project, prov):
        from kinema import cli
        # 4.096 是厂商产物的真实形态（整秒 + 约一帧容器补帧）：按它回填就是
        # 本守卫要拦的那条路。cli 侧的 probe 只用于判「配音要不要垫窗」
        with mock.patch.object(cli, "probe_duration", return_value=4.096), \
                mock.patch.object(voicecast, "probe_duration", return_value=3.2):
            cli.stage_gen_video(project, self._Store(), self._Router(prov))

    def _project(self):
        img = self.tmp / "s1.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        wav = self.tmp / "ch01_work" / "audio" / "shot_1.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFFfake")
        path = self.tmp / "ch01.json"
        data = {"id": "ch01", "motion": "dubbed", "aspect": "16:9",
                "shots": [{"id": 1, "narration": "缆绳再检查一遍。", "dur": 3.2,
                           "video_prompt": "缓慢推近", "image": str(img)}]}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project.load(path)

    def test_writeback_records_the_purchased_seconds(self):
        p = self._project()
        prov = self._Prov()
        self._run(p, prov)
        self.assertEqual(p.shots[0]["dur"], float(prov.bought[0]))

    def test_retakes_never_buy_an_extra_second(self):
        p = self._project()
        prov = self._Prov()
        self._run(p, prov)
        for _ in range(3):
            review.set_state(p.shots[0], "clip", "retake")
            self._run(p, prov)
        self.assertEqual(set(prov.bought), {prov.bought[0]},
                         f"连续重烧的计费秒数在增长：{prov.bought}")


class TestTimestampsWithPause(unittest.TestCase):
    """timestamps.json 的 segments = 窗口口径（含停顿），与 parts 同一条 offset。"""

    def setUp(self):
        self.ctx = _Ctx(_doc(), wavs=(1, 3, 4))
        self.addCleanup(self.ctx.close)

    def test_segments_include_pause_and_tile_timeline(self):
        _p, seg, _m = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        self.assertEqual([x["shot_id"] for x in seg], [1, 4])       # omt 不出段
        self.assertEqual((seg[0]["start"], seg[0]["end"]), (0.0, 1.8))
        self.assertEqual((seg[0]["pause_before"], seg[0]["pause_after"]), (0.5, 0.3))
        # 镜2 纯画面镜占 2.0s → 镜4 起点 = 1.8 + 2.0，窗口含尾留白
        self.assertEqual((seg[1]["start"], seg[1]["end"]), (3.8, 6.05))
        self.assertEqual((seg[1]["pause_before"], seg[1]["pause_after"]),
                         (0.0, voicecast.TAIL_ROLL))
        self.assertAlmostEqual(seg[-1]["end"], self.ctx.project.total_duration(), places=3)

    def test_segments_carry_narration_verbatim(self):
        _p, seg, _m = voicecast.narration_parts(self.ctx.project, self.ctx.adir)
        self.assertEqual(seg[0]["text"], "第1镜台词")

    def test_dubbed_segments_have_no_pause_keys(self):
        ctx = _Ctx(_doc(motion="dubbed"), wavs=(1, 3, 4))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration", lambda p: 1.0):
            _p, seg, _m = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertNotIn("pause_before", seg[0])


class TestNativeVoiceoverBurn(unittest.TestCase):
    """native 配音混烧：配音是**叠加轨**、画面主导时间轴（compose 侧 TTS 上主轨、
    原生音轨降背景床闪避，见 test_mix）。本类钉音画对位的三条底线：
    · 没配音的台词镜是**常态不是错误**（台词由模型原生自配）→ 按 dur 插等长静音，
      绝不进 missing——进了 missing，compose 自愈会直接放弃重拼；
    · 配音镜垫齐窗口（file + 垫片静音到 dur）——不垫的话后续所有镜的配音整体前移；
    · stage_tts 在 native 下**绝不回填 dur**——那是 Seedance 计费/片段实测秒数，
      按配音实测覆写会把请求秒数与时间轴一起改坏。"""

    def test_unvoiced_shot_pads_silence_and_voiced_shot_fills_the_window(self):
        ctx = _Ctx(_doc("native"), wavs=(1,))     # 镜1 有配音；镜4 有台词无配音
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration", return_value=1.2):
            parts, seg, missing = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(missing, [], "native 未配音的台词镜绝不进 missing")
        self.assertEqual(parts, [
            ("file", ctx.wav(1)), ("silence", 0.6),   # 配音 1.2s 垫到窗口 1.8s
            ("silence", 2.0),                          # 镜2 纯画面等长占位
            ("silence", 2.0),                          # 镜4 台词未配音 → 等长静音
        ])
        # 窗口口径：段起止按 dur（画面主导），不按配音实测；停顿在 native 恒 0 不进垫片
        self.assertEqual((seg[0]["start"], seg[0]["end"]), (0.0, 1.8))

    def test_dialogue_shot_is_silenced_even_with_wav_on_disk(self):
        """声源按镜分治：native 的对白由模型原生发声，旁白轨对对白镜恒插等长
        静音——盘上有 wav（早年整章合成留下的）也绝不接入，接入即同一句两个
        人声且两条时间轴不同源；也不进 missing（那会让 compose 自愈放弃重拼）。"""
        doc = _doc("native")
        doc["shots"][0]["speaker"] = "凯尔"          # 镜1 变对白镜，wav 仍在盘
        ctx = _Ctx(doc, wavs=(1,))
        self.addCleanup(ctx.close)
        parts, seg, missing = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(missing, [])
        self.assertNotIn(("file", ctx.wav(1)), parts)
        self.assertEqual(parts[0], ("silence", 1.8), "对白镜按窗口占等长静音")

    def test_stage_tts_skips_dialogue_shots_on_native(self):
        """native 下 stage_tts 不给对白镜合成——产物既不烧录也没有别的消费方，
        留在盘上只会诱发「有 wav 就该烧」的误判。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_tts)
        self.assertIn("voicecast.in_narration_track(s, motion)", src,
                      "跳过判据与拼接/超窗点名共用一份——各写一份就会彼此矛盾")

    def test_overlong_line_is_time_fitted_into_the_window(self):
        """配音长于窗口 → **变速不变调压进窗口**（`("fit", …)`），不是裁词也不是
        放任漂移：三十几镜的章能攒出几十秒偏差，后半段旁白与画面完全对不上
        还会被末尾裁掉。压缩比超 `FIT_TEMPO_WARN` 的镜由 `fit_overruns` 点名——
        那是台词写太满，改词或加长镜头归创作，引擎只报不代改。"""
        ctx = _Ctx(_doc("native"), wavs=(1,))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration", return_value=4.0):
            parts, seg, _m = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(parts[0], ("fit", (ctx.wav(1), 1.8)),
                         "超窗必须压进窗口，绝不原样接入让后面全体漂移")
        self.assertNotIn(("silence", 0.6), parts, "压缩后不该再垫静音")
        self.assertEqual((seg[0]["start"], seg[0]["end"]), (0.0, 1.8), "窗口口径不变")
        with mock.patch.object(voicecast, "probe_duration", return_value=4.0):
            over = voicecast.fit_overruns(ctx.project, ctx.adir)
        self.assertEqual(over[0][0], 1)
        self.assertAlmostEqual(over[0][3], round(4.0 / 1.8, 2), places=2)

    def test_dialogue_shot_wav_is_not_flagged_as_overrun(self):
        """native 对白镜的 wav 根本不进旁白轨（早年整章合成留下的陈旧产物）——
        点名它「会被压快」就是让人去改一句不会被烧的台词。"""
        doc = {"motion": "native",
               "shots": [_shot(1, speaker="凯尔", narration="走。", dur=1.0)]}
        ctx = _Ctx(doc, wavs=(1,))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration", return_value=9.0):
            parts, _seg, _m = voicecast.narration_parts(ctx.project, ctx.adir)
            self.assertEqual(voicecast.fit_overruns(ctx.project, ctx.adir), [])
        self.assertEqual(parts, [("silence", 1.0)], "对白镜恒插等长静音")

    def test_tempo_chain_is_legal_for_any_ratio(self):
        """`atempo` 单级只收 0.5~2.0，极端比必须串联——2.05× 这种
        （10.27s 压进 5s）不串联的话 ffmpeg 直接拒绝整条 filtergraph。"""
        from kinema.ffmpeg import tempo_chain
        self.assertEqual(tempo_chain(1.0), [], "无需变速时不加滤镜")
        self.assertEqual(tempo_chain(1.5), ["atempo=1.5000"])
        self.assertEqual(tempo_chain(2.05), ["atempo=2.0", "atempo=1.0250"])
        self.assertEqual(tempo_chain(0.3), ["atempo=0.5", "atempo=0.6000"])
        for r in (0.3, 0.5, 1.0, 1.3, 2.0, 2.05, 4.5, 9.0):
            for f in tempo_chain(r):
                v = float(f.split("=")[1])
                self.assertTrue(0.5 <= v <= 2.0, f"{r} 拆出非法档位 {v}")
            prod = 1.0
            for f in tempo_chain(r):
                prod *= float(f.split("=")[1])
            self.assertAlmostEqual(prod, r, places=3, msg=f"{r} 的链乘积对不上")

    def test_concat_audio_emits_tempo_and_hard_trim(self):
        """fit 段的 filtergraph：atempo 链 + 按目标硬裁——atempo 输出长度有毫秒级
        误差，逐镜攒起来又变成整轨漂移（这条轨的全部意义就是对位）。"""
        import inspect

        from kinema import ffmpeg as ff
        src = inspect.getsource(ff.concat_audio)
        self.assertIn('kind == "fit"', src)
        self.assertIn("tempo_chain", src)
        self.assertIn("atrim=0:", src, "变速后必须按目标硬裁一刀")

    def test_stage_tts_native_leaves_dur_alone(self):
        import contextlib
        import io

        from kinema.cli import stage_tts
        from kinema.models import ConfigStore, ModelRouter
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        ctx = _Ctx(_doc("native"))
        self.addCleanup(ctx.close)
        durs = {s["id"]: s["dur"] for s in ctx.project.shots}
        store = ConfigStore.load(None)
        with contextlib.redirect_stdout(io.StringIO()):
            stage_tts(ctx.project, store, ModelRouter(store, force_mock=True))
        for s in ctx.project.shots:
            self.assertEqual(s["dur"], durs[s["id"]],
                             f"native 下 tts 不许改镜 {s['id']} 的 dur")
        self.assertTrue((ctx.adir / "narration.wav").is_file(), "旁白轨照拼（混烧的料）")

    def test_fit_dur_grows_the_window_but_never_touches_burned_clips(self):
        """`tts --fit-dur`＝**让画面等台词**（用户诉求「5s 的镜台词要念 10s，
        就该自动改时间」）。两条纪律：
          · **已有 clip 的镜绝不动**——画面已生成、钱已付，改 dur 只会让片段与
            时间轴对不上（那种镜只能先打回 clip 再重烧）；
          · **只放宽不收窄**——配音短于窗口是合法留白，画面继续演。
        """
        import contextlib
        import io

        from kinema.cli import stage_tts
        from kinema.models import ConfigStore, ModelRouter
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        doc = {"motion": "native", "shots": [
            _shot(1, dur=5.0),                      # 台词长 → 应放宽
            _shot(2, dur=5.0, clip=fake_path("burned.mp4")),   # 已烧片段 → 不许动
            _shot(3, dur=9.0),                      # 配音短于窗口 → 不许收窄
        ]}
        ctx = _Ctx(doc)
        self.addCleanup(ctx.close)
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with mock.patch.object(voicecast, "probe_duration", lambda p: 8.0), \
                mock.patch("kinema.cli.probe_duration", lambda p: 8.0), \
                contextlib.redirect_stdout(buf):
            stage_tts(ctx.project, store, ModelRouter(store, force_mock=True),
                      fit_dur=True)
        by = {s["id"]: s for s in ctx.project.shots}
        self.assertEqual(by[1]["dur"], 8.0, "台词超窗的镜必须放宽到配音实测")
        self.assertEqual(by[2]["dur"], 5.0, "已有 clip 的镜画面已定，绝不许改 dur")
        self.assertEqual(by[3]["dur"], 9.0, "配音短于窗口是留白，绝不收窄")
        out = buf.getvalue()
        self.assertIn("画面时长已放宽", out)
        self.assertIn("已有视频片段", out, "被锁定的镜要点名给出路")

    def test_fit_dur_is_opt_in_and_scoped_to_video_modes(self):
        """默认不开（时长是创作决定，且 native 下放宽 dur = 未来 Seedance 更贵）；
        kenburns 本来就回填 dur=配音+停顿，此开关对它们是空操作。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_tts)
        self.assertIn("fit_dur=False", src, "必须 opt-in")
        # 模式判据走 Project.uses_seedance 单一真源，不手写 motion 字面量
        self.assertIn("fit_dur and project.uses_seedance", src)
        self.assertIn('if s.get("clip")', src, "已烧片段的镜必须跳过")

    def test_tts_only_filters_synthesis_but_track_still_covers_the_chapter(self):
        """tts --only：只合成点名的镜（native 混烧常用形态），旁白轨仍按全片拼
        ——未点名的镜按窗口占静音位，绝不让点名镜的配音错位。"""
        import contextlib
        import io

        from kinema.cli import stage_tts
        from kinema.models import ConfigStore, ModelRouter
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        ctx = _Ctx(_doc("native"))
        self.addCleanup(ctx.close)
        store = ConfigStore.load(None)
        with contextlib.redirect_stdout(io.StringIO()):
            stage_tts(ctx.project, store, ModelRouter(store, force_mock=True), only="1")
        self.assertTrue((ctx.adir / "shot_1.wav").is_file())
        self.assertFalse((ctx.adir / "shot_4.wav").is_file(), "--only 1 不许合成镜4")
        parts, _seg, missing = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(missing, [])
        self.assertEqual(parts[0][0], "file", "镜1 配音在轨首")
        self.assertEqual(parts[-1], ("silence", 2.0), "镜4 未配音按窗口占位")


class TestDeliveryCompile(unittest.TestCase):
    """emphasis/note 编译成派生指令；台词原文一个字都不许动。"""

    def test_compile_order_and_format(self):
        s = _shot(1, voice_instruction="用哽咽的语气说",
                  delivery={"emphasis": ["一定", "回来"], "note": "句末收住，别扬调"})
        self.assertEqual(voicecast.delivery_instruction(s),
                         "用哽咽的语气说；重读「一定」「回来」；句末收住，别扬调")

    def test_emphasis_accepts_string_and_dedupes(self):
        s = _shot(1, delivery={"emphasis": "一定、回来，一定"})
        self.assertEqual(voicecast.delivery_instruction(s), "重读「一定」「回来」")
        many = _shot(1, delivery={"emphasis": [f"w{i}" for i in range(20)]})
        self.assertEqual(voicecast.delivery_instruction(many).count("「"),
                         voicecast.MAX_EMPHASIS)

    def test_empty_delivery_compiles_to_nothing(self):
        self.assertEqual(voicecast.delivery_instruction(_shot(1)), "")
        self.assertEqual(voicecast.delivery_instruction(_shot(1, delivery={})), "")

    def test_narration_never_rewritten(self):
        """**铁律**：编译只产派生文本，`shots[].narration` 原文逐字不变、不含任何标签。"""
        s = _shot(1, narration="你一定要回来。", voice_instruction="哽咽",
                  delivery={"emphasis": ["一定"], "note": "慢",
                            "pause_before": 0.5, "pause_after": 0.5})
        before = s["narration"]
        voicecast.delivery_instruction(s)
        voicecast.shot_expressive_params(s)
        voicecast.shot_duration(s, 1.0, "kenburns")
        self.assertEqual(s["narration"], before)
        self.assertNotIn("<", s["narration"])
        self.assertNotIn("重读", s["narration"])

    def test_expressive_params_only_carry_emotion(self):
        """模版生成（官方固定音色）**只有 emotion 这一条通道**：写了
        `voice_instruction`/`delivery.note` 也不进请求体——标准版会静默过滤，
        发了等于凭空多一段噪音。编译器本身仍在（`delivery_instruction`），
        它归「定制生成」——那条路是自然语言 prompt 驱动的。"""
        s = _shot(1, emotion="sad", emotion_scale=4, voice_instruction="哽咽",
                  delivery={"note": "慢一点"})
        self.assertEqual(voicecast.shot_expressive_params(s),
                         {"emotion": "sad", "emotion_scale": 4})
        self.assertEqual(voicecast.delivery_instruction(s), "哽咽；慢一点")

    def test_expressive_without_any_instruction_stays_clean(self):
        s = _shot(1, emotion="happy")
        self.assertEqual(voicecast.shot_expressive_params(s), {"emotion": "happy"})


class TestVoicePerformanceInheritance(unittest.TestCase):
    """`voice_performance` 建章时拷贝（改系列不回灌）；**不进设定集同步白名单**。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv()
        self.env.enable()
        self.addCleanup(self.env.restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _series(self):
        from kinema.workspace import Workspace
        ws = Workspace.open(self.tmp.name, create=True)
        s = ws.create_project("表现力", pid="vp")
        s.data["voice_performance"] = {"pacing": "偏慢，句间留白",
                                       "energy_curve": "前克制→中推高→尾收回"}
        s.save()
        return s

    def test_new_chapter_copies_block(self):
        s = self._series()
        s.create_chapter("第一集")
        data = s.ws.store.load_chapter("vp", "ch01")
        self.assertEqual(data["voice_performance"]["pacing"], "偏慢，句间留白")

    def test_series_edit_does_not_backfill_existing_chapter(self):
        s = self._series()
        s.create_chapter("第一集")
        s.data["voice_performance"]["pacing"] = "改成很快"
        s.save()
        s.sync_design_to_chapters()          # 设定集同步不该碰表现力块
        data = s.ws.store.load_chapter("vp", "ch01")
        self.assertEqual(data["voice_performance"]["pacing"], "偏慢，句间留白")


class TestScannerExposesDelivery(unittest.TestCase):
    """Studio 镜头表专业视图直读 delivery——不下发就是新的展示漂移。"""

    def test_shot_view_carries_delivery(self):
        from kinema.studio import scanner
        src = Path(scanner.__file__).read_text(encoding="utf-8")
        self.assertIn('"delivery": s.get("delivery")', src)

    def test_no_numeric_condition_as_h_child(self):
        """`h()` 只跳过 null/false——数字 0 会被 `String(c)` 渲染成文本「0」。

        `x.length && h(...)` 作子节点时，空数组会在格子里凭空多出一个「0」
        （凡是有 voice_instruction 却没写 delivery 的镜全中）。条件必须布尔化。"""
        src = _frontend_src()
        self.assertNotRegex(src, r"\.length && h\(")
        self.assertIn("dvBits.length > 0 && h(", src)

    def test_emphasis_split_matches_backend(self):
        """字符串 emphasis 的切分口径前后端必须一致，否则展示与实发不是一回事。

        后端 `_emphasis_words` 按 `、,，/|` 切（schema 明写「写字符串也可」）；
        前端不切就会出现引擎发「重读「一定」「回来」」而网页显示「重读「一定、回来」」。"""
        src = _frontend_src()
        self.assertIn('String(dv.emphasis == null ? "" : dv.emphasis).split(/[、,，/|]/)', src)
        self.assertEqual(voicecast._emphasis_words("一定,回来"), ["一定", "回来"])
        self.assertEqual(voicecast._emphasis_words("一定、回来|一定"), ["一定", "回来"])


class TestBillingContractUnchanged(unittest.TestCase):
    """计费按 `len(text)` 走原文——编译文本是派生物，绝不回写 narration。"""

    def test_instruction_does_not_inflate_char_count(self):
        s = _shot(1, narration="你一定要回来。", voice_instruction="哽咽",
                  delivery={"emphasis": ["一定"], "note": "慢"})
        instr = voicecast.delivery_instruction(s)
        self.assertGreater(len(instr), 0)
        self.assertEqual(len(s["narration"]), len("你一定要回来。"))
        self.assertNotIn(instr, s["narration"])


class TestSeedanceCeilSanity(unittest.TestCase):
    """门控用例里的计费口径与 provider 实现同源（防口径漂移误判）。"""

    def test_ceil_matches_provider(self):
        from kinema.providers.video.seedance import SeedanceProvider
        prov = SeedanceProvider({}, None)
        for d in (4.1, 5.2, 6.2, 9.9):
            self.assertEqual(prov.billable_seconds(d, dubbed=True),
                             max(4, min(15, math.ceil(d))))


class TestLicenseNotices(unittest.TestCase):
    """AGPL 的**声明面**守卫：本项目对外只声明一种授权——GNU AGPL v3。
    协议的义务大半是"必须写在那儿"，而写在那儿的东西最容易在某次重构里被顺手删掉——
    删了不报错、不掉测试，只在合规审查时变成硬伤。"""

    def _root(self) -> Path:
        import kinema
        return Path(kinema.__file__).parent.parent.parent

    def test_license_file_is_verbatim_agpl(self):
        """LICENSE 必须是 AGPL v3 原文，且**一个字都不许改**（协议首段自己写死了这条）。"""
        for rel in ("LICENSE", "engine/LICENSE"):
            txt = (self._root() / rel).read_text(encoding="utf-8")
            self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", txt, rel)
            self.assertIn("Version 3, 19 November 2007", txt, rel)
            # 13 条是 AGPL 区别于 GPL 的那一条；被换成 GPL 原文时这里当场红
            self.assertIn("13. Remote Network Interaction", txt, rel)
            self.assertNotIn("BladeX", txt, rel)   # 原文里塞私货 = 不再是 AGPL

    def test_every_source_file_carries_the_agpl_header(self):
        """AGPL 第 5(a)(b) 条：分发的每份源码都要带许可声明。抽查引擎全部 py/js。"""
        import kinema
        pkg = Path(kinema.__file__).parent
        # vendor/ 是第三方 MIT 代码：**必须排除**——给别人的代码盖我们的许可头
        # 等于对他人著作主张授权，比漏盖严重得多。它的合规靠自带 NOTICE（见下一条）。
        files = [p for p in list(pkg.rglob("*.py")) + list(pkg.rglob("*.js"))
                 if "vendor" not in p.parts]
        self.assertGreater(len(files), 100, "包内源文件数异常，守卫可能扫错了目录")
        missing = [str(p.relative_to(pkg)) for p in files
                   if "GNU Affero General Public License" not in
                   p.read_text(encoding="utf-8", errors="ignore")[:2000]]
        self.assertEqual(missing, [], f"这些文件缺 AGPL 头：{missing}")

    def test_vendored_third_party_keeps_its_own_license(self):
        """第三方 vendored 代码保留上游 MIT 声明，且有 NOTICE 交代出处与许可。
        AGPL 第 7 条允许并入 MIT 代码，但并入**不改变**这些文件自身的许可。"""
        import kinema
        vendor = Path(kinema.__file__).parent / "studio_app" / "vendor"
        notice = (vendor / "NOTICE.md").read_text(encoding="utf-8")
        self.assertIn("MIT", notice)
        self.assertIn("Three.js", notice)
        for name in ("three.module.js", "three.core.min.js"):
            head = (vendor / name).read_text(encoding="utf-8", errors="ignore")[:400]
            self.assertIn("SPDX-License-Identifier: MIT", head, name)
            self.assertNotIn("Affero", head, f"{name} 被误盖了我们的 AGPL 头")

    def test_headers_carry_a_machine_readable_spdx_tag(self):
        """SPDX 标识让 FOSSology / ScanCode 这类合规扫描器**确定性识别**授权，
        而不是对正文做模糊匹配——后者在改过一个字的声明上就可能给出 unknown。
        REUSE 规范与 Linux 内核都按这个来。"""
        import kinema
        pkg = Path(kinema.__file__).parent
        files = [q for q in list(pkg.rglob("*.py")) + list(pkg.rglob("*.js"))
                 + list(pkg.rglob("*.css")) + list(pkg.rglob("*.html"))
                 if "vendor" not in q.parts]
        self.assertGreater(len(files), 100)
        missing = [str(q.relative_to(pkg)) for q in files
                   if "SPDX-License-Identifier: AGPL-3.0-or-later"
                   not in q.read_text(encoding="utf-8", errors="ignore")[:2200]]
        self.assertEqual(missing, [], f"这些文件缺 SPDX 标识：{missing}")

    def test_headers_carry_no_positioning_copy(self):
        """许可头**只放归属与授权，绝不放产品定位语**。

        定位语一旦写进头部就是一百多个文件的耦合——改一次说法要全树重扫，
        且必然与 README／Studio 弹层漂移出多种说法。
        首行恒为项目归属声明，定位语只准出现在 README（中英）与法律弹层。"""
        import kinema
        pkg = Path(kinema.__file__).parent
        files = [q for q in list(pkg.rglob("*.py")) + list(pkg.rglob("*.js"))
                 + list(pkg.rglob("*.css")) + list(pkg.rglob("*.html"))
                 if "vendor" not in q.parts]
        bad: dict = {}
        for q in files:
            head = q.read_text(encoding="utf-8", errors="ignore")[:2200]
            head = head[:head.find("SPDX-License-Identifier")]
            hits = [w for w in ("local-first", "production system", "本地优先",
                                "生产系统", "智能体", "filmmaking agent")
                    if w in head]
            if "This file is part of Kinema." not in head:
                hits.append("缺归属声明")
            if hits:
                bad[str(q.relative_to(pkg))] = hits
        self.assertEqual(bad, {}, f"许可头混入定位语／缺归属声明：{bad}")

    def test_notice_body_matches_the_fsf_wording(self):
        """三段正文必须与 FSF 原版逐字一致——改写免责声明会削弱其法律效力，
        而"看起来差不多"的自创措辞正是合规审查里最难解释的东西。"""
        import kinema
        head = (Path(kinema.__file__).parent / "sheets.py").read_text(encoding="utf-8")[:2200]
        for line in (
            "This program is free software: you can redistribute it and/or modify",
            "it under the terms of the GNU Affero General Public License as published by",
            "but WITHOUT ANY WARRANTY; without even the implied warranty of",
            "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the",
            "along with this program.  If not, see <https://www.gnu.org/licenses/>.",
        ):
            self.assertIn(line, head, f"FSF 原版措辞被改动：{line}")

    def test_headers_are_english_only(self):
        """头部注释是给全球读者与合规工具看的，中文只留在面向中文用户的 UI 与文档里。"""
        import re, kinema
        pkg = Path(kinema.__file__).parent
        bad = []
        for q in list(pkg.rglob("*.py")) + list(pkg.rglob("*.js")):
            if "vendor" in q.parts:
                continue
            head = q.read_text(encoding="utf-8", errors="ignore")[:1400]
            # 取声明块本身（到 FSF 尾句为止），正文里的中文注释不在此约束内
            cut = head.find("https://www.gnu.org/licenses/")
            if cut < 0:
                continue
            if re.search(r"[\u4e00-\u9fff]", head[:cut]):
                bad.append(str(q.relative_to(pkg)))
        self.assertEqual(bad, [], f"这些文件的许可头里混了中文：{bad}")

    def test_headers_declare_agpl_and_nothing_else(self):
        """许可头只说一件事：AGPL。商业授权是另行签署的合同、**不随代码走**，
        逐文件写进头部只会让 FOSSology / ScanCode 这类 SCA 工具在同一份源码上
        读出两个互相矛盾的结论。商业授权的口径在 README 的许可证节，不进每个文件的头。"""
        import kinema
        pkg = Path(kinema.__file__).parent
        bad = {}
        for p in pkg.rglob("*"):
            if (not p.is_file() or "vendor" in p.parts
                    or p.suffix not in (".py", ".js", ".css", ".html", ".yaml", ".toml")):
                continue
            head = p.read_text(encoding="utf-8", errors="ignore")[:2000]
            hits = [m for m in ("BladeX Commercial License", "Commercial license:") if m in head]
            if hits:
                bad[str(p.relative_to(pkg))] = hits
        self.assertEqual(bad, {}, f"这些文件的许可头带了 AGPL 之外的授权声明：{bad}")

    def test_studio_ui_shows_appropriate_legal_notices(self):
        """AGPL 第 5(d) 条：交互式界面必须显示 Appropriate Legal Notices。
        协议第 0 条把"显示"定义为四件齐备——版权 · 无担保 · 可按本协议分发 · 如何看全文。"""
        import kinema
        assets = Path(kinema.__file__).parent / "studio_app"
        html = (assets / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="rail-legal"', html, "侧栏缺法律声明入口")
        src = _frontend_src()
        self.assertIn("bindLegalNotice", src)
        # 四件套逐件在位（措辞可改，但这四层意思一件都不能少）
        self.assertIn("Copyright (C) 2018-2099 BladeX", src)      # ① 版权
        self.assertIn("不附带任何担保", src)                        # ② 无担保
        self.assertIn("再分发与修改", src)                          # ③ 可按本协议分发
        self.assertIn("agpl-3.0", src)                            # ④ 如何查看全文
        # 企业闭源商用的去处留一行就够——这里是法律声明，不是销售页
        self.assertIn("商业授权", src)
        # 绑定必须早于取数：后端挂了也要能查许可证
        entry = (assets / "app.js").read_text(encoding="utf-8")
        self.assertLess(entry.index("bindLegalNotice()"), entry.index("await getOverview()"),
                        "法律声明绑定必须排在首屏取数之前")

    def test_readme_states_the_license_plainly(self):
        """首页只需交代三件事：以 AGPL v3 开源 · 闭源商用另有授权 · 找谁买。
        商业授权是线下签署的合同，条款不随仓库分发；README 许可证节即库内对外的
        完整口径，本用例校验上述三项信息在中英两份首页齐备。

        （配图引用的存在性归 TestRepoAssetsLiveUnderAssets，那里按全路径查而不限前缀。）"""
        root = self._root()
        for name, buyline in (("README.md", "commercial license"),
                              ("README.zh-CN.md", "商业授权")):
            readme = (root / name).read_text(encoding="utf-8")
            self.assertIn("AGPL", readme, name)
            self.assertIn(buyline, readme, name)
            self.assertIn("bladejava@qq.com", readme, name)


class TestUserFacingErrorsAreKinemaErrors(unittest.TestCase):
    """`main()` 只友好化 KinemaError：这四个模块若用 ValueError 携带精心措辞的
    中文用户提示，命令行吐的就是裸 traceback（Studio 兜 Exception 返 400，
    同一错误两端表现不一致）。刻意**不给 main() 开 ValueError 通用通道**——
    那会把真正的编程错误也伪装成用户提示。（review/watermark 的同款
    CLI 不可达；adaptation 的 EPUB 错误在 workspace 层有捕获——均不在此闸内。）"""

    _MODULES = ("pipeline/versioning.py", "pipeline/cover.py",
                "decisions.py", "pipeline/consistency.py")

    def test_no_cjk_valueerror_in_gated_modules(self):
        import re
        root = Path(__file__).resolve().parents[1] / "kinema"
        pat = re.compile(r"raise ValueError\([^)]*[一-鿿]")
        for rel in self._MODULES:
            src = (root / rel).read_text(encoding="utf-8")
            self.assertIsNone(pat.search(src),
                              f"{rel}: 面向用户的中文提示必须抛 KinemaError 子类")


class TestRepoAssetsLiveUnderAssets(unittest.TestCase):
    """素材归 `assets/`、文档归 `docs/`——分界一旦松动就会慢慢长回去。

    素材若散进 `docs/images/`、`docs/brand/` 这类第二落点，同类素材就有了两个去处，
    而「该放什么图」的登记表又只在其中一处；这类双写迟早各自漂移。纪律是：图片、视频、
    矢量标识一律落 `assets/`，`docs/` 只留 markdown 与规范文本（`project.schema.json`
    数据契约、`kinema.sql` 建表脚本——它们是规范不是素材，且路径被代码与命令示例写死）。

    这三条守卫分别挡三种回流：素材漏回 docs/、README 引用指向不存在的文件、素材没人引用。"""

    MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                      ".mp4", ".mov", ".webm", ".ico", ".otf", ".ttf"}
    READMES = ("README.md", "README.zh-CN.md")

    def _root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _readmes(self) -> dict:
        root = self._root()
        return {n: (root / n).read_text(encoding="utf-8") for n in self.READMES}

    def test_docs_holds_no_media_files(self):
        """`docs/` 下零素材。漏一张回去，错位文件就会成为后续落位的错误参照。"""
        root = self._root()
        strays = sorted(p.relative_to(root).as_posix()
                        for p in (root / "docs").rglob("*")
                        if p.is_file() and p.suffix.lower() in self.MEDIA_SUFFIXES)
        self.assertEqual([], strays, "素材不该留在 docs/，挪进 assets/ 并同步改引用")

    def test_assets_have_no_orphans(self):
        """反向查：`assets/` 里每个素材都得有人引用。没人引用的图就是该删的图，
        留着会造成是否仍被引用的歧义。"""
        root = self._root()
        blob = "".join(self._readmes().values())
        orphans = []
        for sub in ("screenshots",):
            for p in sorted((root / "assets" / sub).iterdir()):
                if not p.is_file() or p.name.startswith("."):
                    continue
                rel = p.relative_to(root).as_posix()
                if rel not in blob:
                    orphans.append(rel)
        self.assertEqual([], orphans, "这些素材没有任何 README 引用——要么用起来，要么删掉")


class TestAgentDocsAreSingleSourced(unittest.TestCase):
    """跨工具 agent 文档的**唯一真源纪律**。

    `AGENTS.md` 是 Linux Foundation 托管的跨工具约定，Claude Code / Codex / Cursor /
    Copilot / Windsurf / Aider / Zed 均原生读取；各家专属文件只做指针。守卫钉死这一点
    的原因很实际：一旦某个指针里开始长出真内容，它与 AGENTS.md 就会各自演化，
    而读者按工具不同拿到两份互相矛盾的说明——这类分叉不报错，只让人做错事。
    """

    def _root(self):
        import kinema
        return Path(kinema.__file__).parent.parent.parent

    def test_kinema_skill_stays_lean_and_refs_resolve(self):
        """kinema/SKILL.md 与 AGENTS.md 同一套索引化纪律：规则结论句常驻主档，
        长解释在 references/ 详情层。行预算由 Agent manifest 统一声明；正文指到的 references 文件
        必须存在，反之每个文件必须被正文引用——单边断链即规则静默消失。"""
        import re
        skill_dir = self._root() / ".claude" / "skills" / "kinema"
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        manifest = json.loads((self._root() / "agent" / "manifest.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(text.splitlines()), manifest["budgets"]["skill_lines"],
                        "kinema/SKILL.md 又长回去了——新内容落 references/ + 一行索引")
        pointed = set(re.findall(r"references/([a-z-]+\.md)", text))
        on_disk = {p.name for p in (skill_dir / "references").glob("*.md")}
        self.assertEqual(pointed - on_disk, set(), "正文指向了不存在的 references 文件")
        self.assertEqual(on_disk - pointed, set(), "references 文件没有任何正文引用（孤儿）")

    def test_agents_reading_map_matches_docs_agents_on_disk(self):
        """AGENTS.md §7 阅读地图与 `docs/agents/` 详情层的双向对齐：地图指到的
        详情文档必须在盘上，反之每篇详情文档必须被地图索引。详情层路径同时散在
        kernel、SKILL 源与引擎注释多处，改动落点时漏改任何一处都不报错，
        只是那条导航静默断链，或新写的详情文档永远没人读到。"""
        import re
        root = self._root()
        text = (root / "AGENTS.md").read_text(encoding="utf-8")
        pointed = set(re.findall(r"docs/agents/([a-z0-9-]+\.md)", text))
        on_disk = {p.name for p in (root / "docs" / "agents").glob("*.md")}
        self.assertTrue(pointed, "AGENTS.md 不再索引 docs/agents/——阅读地图整体丢失")
        self.assertEqual(pointed - on_disk, set(),
                         "AGENTS.md 指向了不存在的详情文档（断链）")
        self.assertEqual(on_disk - pointed, set(),
                         "docs/agents/ 有文档未被 AGENTS.md 索引（孤儿，没人会读到）")

    def test_skills_stay_in_the_claude_discovery_path(self):
        """Claude Code 只在 `.claude/skills/` 发现项目级 Skill；单源布局下正文实体
        也只有这一份（在发现目录原地编辑，frontmatter/skill.json 由编译器维护）。

        守的是**实体唯一**：人读索引在 `docs/skills/INDEX.md`。无论谁在根目录
        长出 `skills/`、还是往 `docs/skills/` 塞 SKILL.md，都是在发现路径
        之外长出第二份正文实体——这才是真正会让斜杠命令与正文各自漂移的那件事。"""
        root = self._root()
        self.assertTrue((root / ".claude" / "skills").is_dir())
        strays = sorted(p.relative_to(root).as_posix()
                        for place in (root / "skills", root / "docs" / "skills")
                        for p in place.rglob("SKILL.md"))
        self.assertEqual([], strays,
                         "发现路径之外出现 SKILL.md 实体——实体只准在 .claude/skills/，"
                         "那是 Claude Code 的唯一发现路径")

    def test_agents_alias_points_at_claude_skills(self):
        """`.agents/skills` 是给 Codex / Gemini CLI / Amp 一族的发现别名
        （agentskills.io 生态的中立路径；Cursor/Copilot 原生兼容读 .claude/）。
        实体永远只在 .claude/skills/，别名必须指回它——链接被删、改向或
        被实体目录顶替都不报错，只是 21 个 skill 在那些工具里静默消失，
        或分裂成两份各自漂移的实体。Windows 未开 core.symlinks 的检出会
        把链接落地成文本残根（无破坏，仅发现降级），修复走一次
        `python tools/agents_alias.py`（免管理员 junction）——所以残根这里
        只 skip 并报修复命令，指错方向/实体顶替才红灯。"""
        import os
        root = self._root()
        alias = root / ".agents" / "skills"
        target = (root / ".claude" / "skills").resolve()
        self.assertTrue((root / "tools" / "agents_alias.py").is_file(),
                        "缺 tools/agents_alias.py——守卫与文档都在承诺这个修复工具")
        if os.name != "posix":
            if alias.is_dir():  # 真 symlink 或 NTFS junction：验指向
                self.assertEqual(alias.resolve(), target,
                                 ".agents/skills 未指向 .claude/skills"
                                 "（实体目录顶替=第二份真源）")
            else:
                self.skipTest("Windows 文本残根检出：跑一次 "
                              "python tools/agents_alias.py 即修复")
            return
        self.assertTrue(alias.is_symlink(),
                        ".agents/skills 必须是 symlink（实体目录=第二份真源）")
        self.assertEqual(alias.resolve(), target,
                         ".agents/skills 未指向 .claude/skills")

    def test_every_skill_has_discoverable_frontmatter(self):
        """frontmatter 是跨工具契约（agentskills.io 规范）：name 小写字母数字
        连字符、≤64 字符且与目录同名；description 1–1024 字符。越界不报错——
        严格实现（skills-ref 校验器、Gemini CLI）只是静默跳过该 skill，
        它在那些工具里就成了一份没人读的 markdown，所以按规范上限钉死。"""
        import re
        root = self._root()
        bad = {}
        for d in sorted((root / ".claude" / "skills").iterdir()):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            head = f.read_text(encoding="utf-8")[:4000]
            m = re.match(r"---\n(.*?)\n---", head, re.S)
            if not m:
                bad[d.name] = "缺 frontmatter"
                continue
            fm = m.group(1)
            name = re.search(r"^name:\s*(\S+)\s*$", fm, re.M)
            desc = re.search(r"^description:\s*(.+?)(?=^\S|\Z)", fm, re.M | re.S)
            if not name or name.group(1) != d.name or len(name.group(1)) > 64 \
                    or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name.group(1)):
                bad[d.name] = f"name 不合规：{name.group(1) if name else '缺失'}"
            elif not desc or not 1 <= len(desc.group(1).strip()) <= 1024:
                n = len(desc.group(1).strip()) if desc else 0
                bad[d.name] = f"description 越界：{n} 字符（规范上限 1024）"
        self.assertEqual(bad, {}, f"这些 skill 的 frontmatter 不合规：{bad}")


class TestDevelopMapMatchesRepo(unittest.TestCase):
    """DEVELOP.md 自我承诺的三重对拍（该文件开头与 §九 都声明「由本守卫强制」）：
    ① 模块清单与代码树双向全量比对（多写少写都红）② 命令表逐条对 argparse
    双向比对 ③ 反引号仓库路径逐个查存在。336 行的全景地图靠人记不住，
    仅有「被守卫钉住」的声明而无对应断言也不可验证——本类补上真实比对。"""

    # (起始标题, 结束标题, 相对目录, 是否递归, 是否只认表格行)。表格型段落只从
    # `|` 行取声明，作者在段内散文里提到别的 `x.py` 不算多写；散文型段落（能力层/
    # 存储/Studio）的清单本身就写在散文里，只能整段扫。tests 段忽略 __init__.py。
    _PY_SECTIONS = (
        ("### 核心域", "### 合成流水线", "engine/kinema", False, True),
        ("### 合成流水线", "### 能力层", "engine/kinema/pipeline", False, True),
        ("### 能力层", "### 存储", "engine/kinema/providers", True, False),
        ("### 存储", "### Studio 后端", "engine/kinema/storage", False, False),
        ("### Studio 后端", "## 四、", "engine/kinema/studio", False, False),
        ("## 六、测试守卫地图", "## 七、", "engine/tests", False, False),
    )
    # 路径存在性只查以仓库顶层段开头的 token——`app/` 这类相对提法不在此列；
    # 含以下任一字符的 token 是占位/成句/带注释路径，不当作路径查
    _PATH_TOPS = ("agent/", "assets/", "config/", "docs/", "engine/", "music/",
                  "tools/", ".claude/", ".agents/", ".cursor/", ".github/")
    _NOT_A_PATH = ("<", ">", "*", "$", " ", "（", "(", ")")

    def _root(self):
        import kinema
        return Path(kinema.__file__).parent.parent.parent

    def _text(self) -> str:
        return (self._root() / "DEVELOP.md").read_text(encoding="utf-8")

    @staticmethod
    def _is_ignored_path(root: Path, rel: str) -> bool:
        """只把 Git 明确判定为 ignored 的缺席路径视为本机可选配置。

        `DEVELOP.md` 记录的是仓库事实；本机覆盖层和用户数据按设计不进仓库，
        因而不能要求 clean clone 必须带着它们。使用 `git check-ignore` 而不是
        手写文件名白名单，确保守卫遵循 `.gitignore` 的实际匹配语义。
        """
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "--", rel],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0

    @staticmethod
    def _section(text: str, start: str, end: str) -> str:
        # 标题锚在行首：将来加目录（TOC）后按裸 index 会先命中目录项、整段抽空
        i = text.index("\n" + start)
        return text[i:text.index("\n" + end, i)]

    def test_module_tables_match_disk_bidirectionally(self):
        import re
        text, root = self._text(), self._root()
        problems = []
        for start, end, rel, recursive, table_only in self._PY_SECTIONS:
            sec = self._section(text, start, end)
            scope = ("\n".join(ln for ln in sec.splitlines() if ln.startswith("|"))
                     if table_only else sec)
            doc = set(re.findall(r"`([A-Za-z0-9_]+\.py)`", scope))
            d = root / rel
            disk = {p.name for p in (d.rglob("*.py") if recursive else d.glob("*.py"))}
            if rel == "engine/tests":
                doc.discard("__init__.py")
                disk.discard("__init__.py")
            if doc != disk:
                problems.append(f"{start} 段：文档多写 {sorted(doc - disk)} / "
                                f"漏写 {sorted(disk - doc)}")
        # 前端模块表（§四）：app/ 与 director/ 的 .js 双向对齐（app.js 入口在列）
        sec4 = self._section(text, "## 四、Studio 前端", "## 五、")
        docjs = set(re.findall(r"`([A-Za-z0-9_-]+\.js)`", sec4))
        base = root / "engine" / "kinema" / "studio_app"
        diskjs = ({p.name for p in (base / "app").glob("*.js")}
                  | {p.name for p in (base / "director").glob("*.js")} | {"app.js"})
        if docjs != diskjs:
            problems.append(f"§四 前端：文档多写 {sorted(docjs - diskjs)} / "
                            f"漏写 {sorted(diskjs - docjs)}")
        self.assertEqual(problems, [], "DEVELOP.md 模块清单与代码树漂移：" + "；".join(problems))

    def test_command_table_matches_argparse_bidirectionally(self):
        import re
        from kinema.cli import build_parser
        sub = next(a for a in build_parser()._actions
                   if a.__class__.__name__ == "_SubParsersAction")
        real = set(sub.choices)
        sec = self._section(self._text(), "## 五、CLI 命令全表", "## 六、")
        rows = "\n".join(ln for ln in sec.splitlines() if ln.startswith("|"))
        doc = {t for t in re.findall(r"`([^`]+)`", rows)
               if re.fullmatch(r"[a-z][a-z0-9-]*", t)}
        self.assertEqual(doc - real, set(), f"命令表列了不存在的命令: {sorted(doc - real)}")
        self.assertEqual(real - doc, set(), f"以下命令未进命令表: {sorted(real - doc)}")

    def test_backticked_repo_paths_exist(self):
        import re
        root = self._root()
        missing = []
        for tok in sorted(set(re.findall(r"`([^`\n]+)`", self._text()))):
            t = tok.strip().rstrip("/")
            if any(c in t for c in self._NOT_A_PATH):
                continue
            if not t.startswith(self._PATH_TOPS):
                continue
            if not (root / t).exists():
                if self._is_ignored_path(root, t):
                    continue
                missing.append(tok)
        self.assertEqual(missing, [], f"DEVELOP.md 引用了不存在的路径: {missing}")

    def test_gitignored_config_is_optional(self):
        """本机覆盖层按 `.gitignore` 语义归类为可选路径，而非硬编码白名单。"""
        root = self._root()
        self.assertTrue(self._is_ignored_path(root, "config/models.local.json"))


class TestReadmeLayoutTreeMatchesRepo(unittest.TestCase):
    """两份 README 的「工程结构」树必须指向真实存在的路径，且中英两棵树同形。

    这棵树是新读者认路的第一张图，不钉住就会漂移：新目录漏登、改名后树里
    留旧名、脚本只列一半。
    这类漂移不报错，只让人按图找不到文件——与 DEVELOP.md 的反引号路径守卫同款问题，
    故用同款闸。**只钉路径与树形，不钉注释里的数字**（那是产品文案，按 AGENTS.md §6
    不设脆弱断言）。"""

    _HEADINGS = ("## 🗂️ Project Structure", "## 🗂️ 工程结构")
    # 运行期目录，克隆出来时并不存在（gitignored 用户数据），只校验树形不校验落盘
    _RUNTIME = {"project"}

    def _root(self):
        import kinema
        return Path(kinema.__file__).parent.parent.parent

    def _tree_paths(self, text: str) -> list[str]:
        """把树块还原成仓库相对路径清单（按缩进推父级，一行多项按 ` · ` 拆）。"""
        import re
        head = next(h for h in self._HEADINGS if h in text)
        block = text[text.index(head):]
        block = block[block.index("```text") + 7:]
        block = block[:block.index("```")]
        parents, out = [], []
        for line in block.splitlines():
            m = re.match(r"^([│ ]*)(?:├── |└── )(.*)$", line)
            if not m:
                continue
            depth = len(m.group(1)) // 4
            names = [n.strip().rstrip("/") for n in
                     m.group(2).split("#")[0].strip().split(" · ") if n.strip()]
            del parents[depth:]
            if len(names) == 1 and "." not in names[0].rsplit("/", 1)[-1]:
                parents.append(names[0])          # 目录才当父级
            for n in names:
                out.append("/".join([*parents[:depth], n]))
        return out

    def test_every_listed_path_exists(self):
        root = self._root()
        for name in ("README.md", "README.zh-CN.md"):
            paths = self._tree_paths((root / name).read_text(encoding="utf-8"))
            self.assertGreater(len(paths), 20, f"{name}: 结构树没解析出来")
            missing = [p for p in paths
                       if p.split("/")[0] not in self._RUNTIME
                       and not (root / p).exists()]
            self.assertEqual([], missing, f"{name} 结构树列了不存在的路径: {missing}")

    def test_both_languages_describe_the_same_tree(self):
        """中英两棵树各自维护，改一棵漏另一棵是这里最常见的失手。"""
        root = self._root()
        en = self._tree_paths((root / "README.md").read_text(encoding="utf-8"))
        zh = self._tree_paths((root / "README.zh-CN.md").read_text(encoding="utf-8"))
        self.assertEqual(en, zh, "README 中英结构树不同形（条目或层级不一致）")


class TestAgentsAliasTool(unittest.TestCase):
    """`tools/agents_alias.py`——Windows 文本残根的一键修复工具，三条契约：
    ①残根/错向链接换成指对的链接 ②实体目录绝不代删（那可能是孤本数据）
    ③幂等，跑几遍都是 0。工具在守卫与三份文档里被点名承诺，所以行为
    也得有守卫，不能只靠一段没人跑的脚本注释。"""

    def _tool(self):
        import importlib.util
        import kinema
        root = Path(kinema.__file__).parent.parent.parent
        spec = importlib.util.spec_from_file_location(
            "agents_alias", root / "tools" / "agents_alias.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _run(self, mod, root):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return mod.main(root)

    def test_stub_is_replaced_and_tool_is_idempotent(self):
        """Windows 检出的残根形态=一个写着目标路径的普通文本文件。"""
        mod = self._tool()
        import os
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".claude" / "skills" / "kinema").mkdir(parents=True)
            (root / ".agents").mkdir()
            (root / ".agents" / "skills").write_text("../.claude/skills",
                                                     encoding="utf-8")
            self.assertEqual(self._run(mod, root), 0)
            alias = root / ".agents" / "skills"
            self.assertEqual(alias.resolve(),
                             (root / ".claude" / "skills").resolve())
            if os.name == "posix":
                self.assertTrue(alias.is_symlink())
            self.assertEqual(self._run(mod, root), 0, "第二遍必须照样成功")

    def test_real_directory_is_refused_not_deleted(self):
        """实体目录顶替链接=第二份真源；此时应拒绝执行，而非删除目录。"""
        mod = self._tool()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".claude" / "skills").mkdir(parents=True)
            entity = root / ".agents" / "skills"
            entity.mkdir(parents=True)
            (entity / "SKILL.md").write_text("孤本改动", encoding="utf-8")
            self.assertEqual(self._run(mod, root), 1)
            self.assertEqual((entity / "SKILL.md").read_text(encoding="utf-8"),
                             "孤本改动", "实体数据一个字节都不许动")


if __name__ == "__main__":
    unittest.main()


class TestDubbedWindowAlignment(unittest.TestCase):
    """dubbed 主音轨的窗口口径：窗口=dur（gen-video 回填的片段实测秒数），配音
    短则垫静音齐窗、超窗变速压入——与 native 混烧同一条分支。

    dubbed 落在逐 wav 连拼的通用分支时，片段比 wav 长的那零点几秒逐镜累计成
    整轨前移（实测 6 镜攒出 3.8s：镜 6 的旁白提前 3.2s 落进镜 5 画面，被听成
    「配音音色不一样」）。kenburns 不经此分支：它的 dur 本就由配音实测回填，
    窗口恒等于 wav。"""

    def _doc(self):
        return {"motion": "dubbed", "shots": [
            _shot(1, dur=5.09),          # 片段实测 5.09 > 配音 4.44 → 垫 0.65
            _shot(2, narration="", caption="纯画面", dur=5.0),
            _shot(3, dur=4.1),           # 片段 4.1 < 配音 4.3 → 变速压窗
        ]}

    def test_short_wav_pads_to_the_clip_window(self):
        ctx = _Ctx(self._doc(), wavs=(1, 3))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration",
                               lambda p: {"shot_1.wav": 4.44,
                                          "shot_3.wav": 4.3}[Path(p).name]):
            parts, seg, missing = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(missing, [])
        self.assertIn(("file", ctx.wav(1)), parts)
        i = parts.index(("file", ctx.wav(1)))
        self.assertEqual(parts[i + 1], ("silence", 0.65),
                         "配音短于片段窗口必须垫齐，否则后续所有镜整体前移")
        self.assertIn(("fit", (ctx.wav(3), 4.1)), parts,
                      "配音超窗走变速压入，不裁词也不放任漂移")
        # 窗口口径的 segments 与视频时间轴同源
        self.assertEqual([s["start"] for s in seg if s["shot_id"] == 3],
                         [round(5.09 + 5.0, 3)])

    def test_track_total_equals_video_timeline(self):
        ctx = _Ctx(self._doc(), wavs=(1, 3))
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration",
                               lambda p: {"shot_1.wav": 4.44,
                                          "shot_3.wav": 4.3}[Path(p).name]):
            parts, _seg, _m = voicecast.narration_parts(ctx.project, ctx.adir)
        total = 0.0
        for k, v in parts:
            if k == "silence":
                total += v
            elif k == "fit":
                total += v[1]
            else:
                total += {ctx.wav(1): 4.44, ctx.wav(3): 4.3}[v]
        self.assertAlmostEqual(total, ctx.project.total_duration(), places=2)


class TestDubbedMouthSync(unittest.TestCase):
    """dubbed 对白镜的开口对齐：配音在窗口内平移到底片开口时点。

    参考媒体模式下模型把开口安排在动作设计允许的时点（"先回头再开口"的镜实测
    嘴比窗口起点晚整秒），wav 钉在窗口起点烧录就是声先于嘴。平移量=底片声轨
    首个语音段起点 − wav 语音起点，`voicecast.dubbed_sync_offset` 单一真源，
    烧录（narration_parts）与字幕落点（compose.speech_spans_resolver）共用。"""

    def _ctx(self, *, dur=5.5, speaker="老周"):
        shot = _shot(1, dur=dur)
        if speaker:
            shot["speaker"] = speaker
        ctx = _Ctx({"motion": "dubbed", "shots": [shot]}, wavs=(1,))
        clip = Path(ctx.tmp.name) / "shot_1.mp4"
        clip.write_bytes(b"clip")
        ctx.project.data["shots"][0]["clip"] = str(clip)
        return ctx, str(clip)

    def _windows(self, wav_on, clip_on):
        def fake(media, duration, *, clean=False):
            on = wav_on if clean else clip_on      # 干净档只用于 TTS wav 一侧
            return [(on, duration)]
        return fake

    def _parts(self, ctx, wav_on, clip_on):
        from kinema.pipeline import speech
        with mock.patch.object(voicecast, "probe_duration", lambda p: 4.0), \
             mock.patch.object(speech, "speech_windows",
                               self._windows(wav_on, clip_on)):
            return voicecast.narration_parts(ctx.project, ctx.adir)

    def test_late_mouth_pads_head_silence(self):
        ctx, _ = self._ctx()
        self.addCleanup(ctx.close)
        parts, seg, _m = self._parts(ctx, wav_on=0.5, clip_on=1.5)
        self.assertEqual(parts[0], ("silence", 1.0), "开口晚一秒，配音后移一秒")
        self.assertEqual(parts[1], ("file", ctx.wav(1)))
        self.assertEqual(parts[2], ("silence", 0.5), "尾垫补齐到窗口，总长不变")
        self.assertEqual(seg[0]["sync"], 1.0)

    def test_early_mouth_trims_head_silence(self):
        ctx, _ = self._ctx()
        self.addCleanup(ctx.close)
        parts, seg, _m = self._parts(ctx, wav_on=0.7, clip_on=0.4)
        self.assertEqual(parts[0], ("cut", (ctx.wav(1), 0.3)),
                         "嘴早于配音语音起点时前移，裁的只是头部静音")
        self.assertEqual(parts[1], ("silence", 1.8), "5.5 − (4.0 − 0.3) = 1.8")
        self.assertEqual(seg[0]["sync"], -0.3)

    def test_shift_clamped_inside_the_window(self):
        ctx, clip = self._ctx()
        self.addCleanup(ctx.close)
        with mock.patch.object(voicecast, "probe_duration", lambda p: 4.0):
            from kinema.pipeline import speech
            with mock.patch.object(speech, "speech_windows",
                                   self._windows(0.5, 9.0)):
                p = voicecast.dubbed_sync_offset(
                    ctx.project.data["shots"][0], ctx.wav(1), clip, 5.5)
            self.assertEqual(p, 1.5, "后移不得把语音尾推出窗口（5.5−4.0）")
            with mock.patch.object(speech, "speech_windows",
                                   self._windows(0.5, 0.0)):
                p = voicecast.dubbed_sync_offset(
                    ctx.project.data["shots"][0], ctx.wav(1), clip, 5.5)
            self.assertEqual(p, -0.5, "前移不得裁进语音（至多裁掉全部头部静音）")

    def test_voiceover_shot_is_never_shifted(self):
        ctx, _ = self._ctx(speaker=None)
        self.addCleanup(ctx.close)
        parts, seg, _m = self._parts(ctx, wav_on=0.5, clip_on=1.5)
        self.assertEqual(parts[0], ("file", ctx.wav(1)),
                         "旁白镜按闭唇出片，无口型可对，恒原位烧录")
        self.assertNotIn("sync", seg[0])

    def test_subtitle_spans_share_the_same_source(self):
        import inspect

        from kinema.pipeline import compose
        self.assertIn("dubbed_sync_offset",
                      inspect.getsource(compose.speech_spans_resolver),
                      "字幕落点必须与烧录共用同一份平移量——各算一份即声画字分家")
        self.assertIn("if not drift and not project.uses_seedance:",
                      inspect.getsource(compose._sync_narration),
                      "图生视频两档零漂移也要重算拼接序列：窗口与逐镜 wav 可分离，"
                      "盘上那条轨可以与时间轴等长而内容早已不符")

    def test_clamped_gap_and_mouth_mismatch_are_named(self):
        """残差点名：钳制吸收不掉的开口差、口型没演完整句，都不许静默出片。"""
        ctx, clip = self._ctx(dur=6.08)
        self.addCleanup(ctx.close)
        from kinema.pipeline import speech
        with mock.patch.object(voicecast, "probe_duration", lambda p: 5.47), \
             mock.patch.object(speech, "speech_windows",
                               lambda m, d, *, clean=False:
                               [(0.6, 5.45)] if clean
                               else [(2.26, 3.72), (4.2, 4.85)]):
            r = voicecast.dubbed_sync_report(
                ctx.project.data["shots"][0], ctx.wav(1), clip, 6.08)
            _p, seg, _m = voicecast.narration_parts(ctx.project, ctx.adir)
        self.assertEqual(r["sync"], 0.61, "平移钳在窗口边界（6.08−5.47）")
        self.assertAlmostEqual(r["gap"], 1.05, places=2)
        note = voicecast.dubbed_sync_note(r)
        self.assertIn("开口仍差 +1.05s", note)
        self.assertIn("底片口型 2.1s ≠ 台词 4.8s", note)
        self.assertEqual(seg[0]["sync_note"], note,
                         "拼接与点名同一次测量——各测一份就会一处报一处不报")

    def test_small_gap_stays_silent(self):
        r = {"sync": 0.5, "gap": 0.2, "mouth": 4.0, "speech": 4.2}
        self.assertEqual(voicecast.dubbed_sync_note(r), "")

    def test_flag_surfaces_at_burn_and_at_lipsync_skip(self):
        import inspect

        from kinema import cli
        from kinema.pipeline import compose
        self.assertIn("口型残差点名", inspect.getsource(compose._sync_narration))
        self.assertIn("dubbed_sync_note", inspect.getsource(cli.stage_lipsync),
                      "lipsync 被跳过时残差镜必须点名——跳过不等于没事")

    def test_concat_audio_cut_trims_and_rebases(self):
        from kinema import ffmpeg as ff
        seen = {}
        with mock.patch.object(ff, "run",
                               lambda args, **kw: seen.update(args=args)):
            ff.concat_audio([("cut", ("/x/a.wav", 0.3)), ("silence", 1.0)], "/x/o.wav")
        graph = seen["args"][seen["args"].index("-filter_complex") + 1]
        self.assertIn("atrim=start=0.300", graph)
        self.assertIn("asetpts=PTS-STARTPTS", graph,
                      "不归零时间戳，该段带着裁掉的偏移入场，整轨从这一段起错位")


class TestRefAudioWindowPadding(unittest.TestCase):
    """发送侧垫窗的行为面：配音短于设计窗口时，实发 ref_audio 是垫到请求秒数的
    静音尾版本；配音已罩满窗口则原样直发。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)

    def tearDown(self):
        self.tmp_ctx.cleanup()
        self.env.restore()

    def _run(self, dur, voice_sec):
        import contextlib
        import io
        import unittest.mock as um

        from kinema.cli import stage_gen_video
        from kinema.ffmpeg import probe_duration, run as ffrun
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        from kinema.providers.video import mock as vmock
        png = self.tmp / "shot_1.png"
        png.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082"))
        cdir = self.tmp / "p" / "chapters"
        cdir.mkdir(parents=True)
        doc = {"id": "ch01", "motion": "dubbed", "aspect": "16:9",
               "shots": [{"id": 1, "dur": dur, "speaker": "甲", "narration": "走。",
                          "image": str(png), "video_prompt": "回身。"}]}
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        p = Project.load(cf)
        adir = p.subdir("audio")
        adir.mkdir(parents=True, exist_ok=True)
        ffrun(["-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={voice_sec}",
               "-t", f"{voice_sec}", str(adir / "shot_1.wav")], desc="test wav")
        sent = {}
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            from kinema.providers.base import VideoResult
            sent["ra"] = kw.get("ref_audio")
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = spy
        try:
            store = ConfigStore.load(None)
            # wav 走真探测（垫窗判据要读它的真实长度），假片段按 dur 返回
            def fake_probe(path):
                ps = str(path)
                return probe_duration(ps) if ps.endswith(".wav") else float(dur)
            with contextlib.redirect_stdout(io.StringIO()), \
                 um.patch("kinema.cli.probe_duration", side_effect=fake_probe):
                stage_gen_video(p, store, ModelRouter(store, force_mock=True),
                                dry_run=False, no_lipsync=True)
        finally:
            vmock.MockVideoProvider.generate = orig
        return sent["ra"], probe_duration(sent["ra"])

    def test_short_voice_is_padded_to_the_window(self):
        ra, alen = self._run(dur=8.0, voice_sec=2.0)
        self.assertIn("_win8s", ra)
        self.assertAlmostEqual(alen, 8.0, delta=0.1)

    def test_full_voice_is_sent_verbatim(self):
        ra, alen = self._run(dur=4.0, voice_sec=4.0)
        self.assertNotIn("_win", ra)
        self.assertAlmostEqual(alen, 4.0, delta=0.1)
