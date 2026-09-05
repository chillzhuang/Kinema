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

"""混音守卫：闪避条件与让路 EQ 次序、三模式差异、
环境音不闪避、末级响度归一 + 削波防护、转场音效提前量与 edge 解耦。

本模块钉死的是**数值与拓扑**（谁进闪避、谁在谁之前、
增益怎么算），冒烟层只验 filtergraph 语法在真实 ffmpeg 上跑得通。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kinema.pipeline import mixdown
from kinema.pipeline import transitions as tr

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_DUR = 12.0


def _table():
    return mixdown.InputTable("silent.mp4")


def _full(tbl, *, narration, bgm, ambient=(), gain=0.0, bed_eq=True):
    """走一遍完整音频图，返回 filtergraph 全文。"""
    pre = mixdown.premix_graph(tbl, narration=narration, bgm=bgm,
                               ambient=list(ambient), bed_eq=bed_eq)
    if pre:
        mixdown.master_graph(tbl, pre, gain)
    return ";".join(tbl.audio)


class TestKenburnsPauseWindow(unittest.TestCase):
    """kenburns 的字幕窗口要避开声明的停顿：dur = 配音 + 停顿，人声只占中间那段。"""

    def test_spans_follow_declared_pauses(self):
        from kinema.pipeline.compose import speech_spans_resolver
        from kinema.project import Project
        p = Project("x.json", {"motion": "kenburns", "shots": []})
        spans_of = speech_spans_resolver(p)
        self.assertIsNotNone(spans_of)
        shot = {"id": 1, "dur": 6.0, "narration": "一句",
                "delivery": {"pause_before": 1.0, "pause_after": 0.5}}
        self.assertEqual(spans_of(shot), [(1.0, 5.5)])
        # 未声明停顿的镜也有尾留白：字幕在留白前收
        self.assertEqual(spans_of({"id": 2, "dur": 4.0, "narration": "一句"}), [(0.0, 3.75)])
        self.assertIsNone(speech_spans_resolver(
            Project("y.json", {"motion": "kenburns", "audio_mode": "scored", "shots": []})))


class TestDucking(unittest.TestCase):
    def test_ducking_only_when_narration_and_bgm(self):
        # 旁白 + BGM 同时在场才闪避
        tbl = _table()
        narr = mixdown.narration_track(tbl, "narration.wav", dur=_DUR)
        bg = mixdown.bgm_track(tbl, "bgm.mp3", dur=_DUR, ducked=True)
        graph = _full(tbl, narration=narr, bgm=bg)
        self.assertIn("sidechaincompress=", graph)
        self.assertIn("asplit=2[na_mix][na_sc]", graph)      # 旁白一路进混音一路当侧链
        self.assertIn("[bg_eq][na_sc]sidechaincompress", graph)
        # 只有旁白（没配 BGM）：绝不出现闪避
        tbl2 = _table()
        n2 = mixdown.narration_track(tbl2, "narration.wav", dur=_DUR)
        self.assertNotIn("sidechaincompress", _full(tbl2, narration=n2, bgm=None))
        # 只有 BGM（纯画面片）：同样不闪避，且用独奏电平（不衰减，见下条）
        tbl3 = _table()
        b3 = mixdown.bgm_track(tbl3, "bgm.mp3", dur=_DUR, ducked=False)
        solo = _full(tbl3, narration=None, bgm=b3)
        self.assertNotIn("sidechaincompress", solo)
        self.assertIn(f"volume={mixdown.BGM_GAIN_SOLO}", solo)

    def test_duck_params_calibrated(self):
        # 标定值（真机实测旁白 mean -26 dB 定阈值）——改动必须连同注释理由一起改
        self.assertEqual(mixdown.DUCK["threshold"], 0.05)
        self.assertEqual(mixdown.DUCK["ratio"], 8)
        self.assertEqual(mixdown.DUCK["attack"], 25)
        self.assertEqual(mixdown.DUCK["release"], 400)
        self.assertEqual(mixdown.DUCK["makeup"], 1)
        self.assertEqual(mixdown.duck_params(),
                         "threshold=0.05:ratio=8:attack=25:release=400:makeup=1")

    def test_voice_pocket_eq_before_sidechain(self):
        # 让路 EQ 必须在 BGM 链上、且在 sidechaincompress 之前
        # （duck 后再 EQ 会二次改写闪避深度，听感深浅不可控）
        tbl = _table()
        narr = mixdown.narration_track(tbl, "narration.wav", dur=_DUR)
        bg = mixdown.bgm_track(tbl, "bgm.mp3", dur=_DUR, ducked=True)
        graph = _full(tbl, narration=narr, bgm=bg)
        self.assertIn(f"[{bg}]{mixdown.VOICE_POCKET_EQ}[bg_eq]", graph)
        self.assertLess(graph.index("equalizer="), graph.index("sidechaincompress="))
        self.assertIn("g=-3.5", mixdown.VOICE_POCKET_EQ)     # 挖中频而非砍掉


class TestModes(unittest.TestCase):
    def test_native_has_no_bgm_no_ducking(self):
        # native：模型原生音画，只有片段音轨这一路；无 BGM 母线 → 无闪避、无 EQ
        tbl = _table()
        na = mixdown.clip_audio_track(tbl, dur=_DUR)
        self.assertEqual(na, "na")
        self.assertTrue(tbl.audio[0].startswith("[0:a]"))     # 主音轨取输入 0 的音频流
        graph = _full(tbl, narration=na, bgm=None)
        self.assertNotIn("sidechaincompress", graph)
        self.assertNotIn("equalizer", graph)
        self.assertNotIn("aloop", graph)
        self.assertIn("alimiter", graph)                      # 末级仍在（削波防护是无条件的）

    def test_native_voiceover_burn_ducks_the_clip_bed(self):
        """native 配音混烧：TTS 旁白上主轨（0 dB），片段原生音轨降背景床
        （NATIVE_BED_GAIN·占 BGM 槽位）——让路 EQ + sidechain 闪避照旧作用其上，
        说话段模型自配的同句台词被压成弱底、句间氛围恢复。**这条路要显式 opt-in**
        （--burn-voice / native_voiceover:true）；没开的 native 章节仍走
        clip_audio_track 原样直通（上一用例），两态绝不互串。"""
        tbl = _table()
        na = mixdown.narration_track(tbl, "narr.wav", dur=_DUR)
        bg = mixdown.clip_bed_track(tbl, dur=_DUR)
        self.assertEqual(bg, "bg")
        bed = next(f for f in tbl.audio if f.startswith("[0:a]") and "volume=" in f)
        self.assertIn(f"volume={mixdown.NATIVE_BED_GAIN}", bed,
                      "原生音轨必须降到床电平（对白/音效是场景躯体，比纯音乐床略高）")
        graph = _full(tbl, narration=na, bgm=bg)
        self.assertIn("sidechaincompress", graph, "混烧必须闪避——否则双声道台词对唱")
        self.assertIn("equalizer", graph)          # 让路 EQ 在闪避之前的既有纪律
        self.assertNotIn("aloop", graph)           # 床是片段音轨，不是循环 BGM
        # compose 接线源级：混烧只认 native（dubbed 的片段音轨=我们 TTS 的对口型版，
        # 再叠原始 TTS 会让同一句台词出现两条人声）
        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        self.assertIn("narration and project.native_audio", src)
        self.assertIn("clip_bed_track", src)
        self.assertIn("project.native_audio and project.native_voiceover", src,
                      "native 混烧必须显式 opt-in——「盘上有 wav 就自动烧」是老陷阱")

    def test_bed_suppression_is_gated_to_voiceover_windows(self):
        """床压制按旁白镜窗口门控：声源按镜分治后，片段音轨在对白镜窗口里是
        主人声——整轨静态压制会把它压低 8dB 还挖中频，对白比旁白明显发虚。
        窗口门控形态下降电平与让路 EQ 都只在旁白窗口生效，premix 不再整轨
        叠 EQ（叠了就是窗口内双重挖频）。"""
        tbl = _table()
        na = mixdown.narration_track(tbl, "narr.wav", dur=_DUR)
        bg = mixdown.clip_bed_track(tbl, dur=_DUR,
                                    bed_windows=[(0.0, 3.0), (9.0, 12.0)])
        bed = next(f for f in tbl.audio if f.startswith("[0:a]") and "volume=" in f)
        self.assertIn("enable='between(t,0.000,3.000)+between(t,9.000,12.000)'", bed)
        self.assertIn("equalizer", bed, "让路 EQ 与降电平同窗门控")
        graph = _full(tbl, narration=na, bgm=bg, bed_eq=False)
        self.assertIn("sidechaincompress", graph,
                      "闪避保留——旁白轨驱动，对白窗口里旁白静音天然不触发")
        self.assertEqual(graph.count("equalizer"), 1,
                         "premix 关掉整轨 EQ 后全图只剩床轨自带的那一处")
        # compose 接线源级：窗口取自 timeline 的旁白镜
        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        self.assertIn('voicecast.voice_kind(s) == "voiceover"', src)
        self.assertIn("bed_windows=vo_wins", src)

    def test_native_never_burns_the_voiceover_without_an_explicit_opt_in(self):
        """**native 默认不烧配音**。

        native 的片段自带模型原生人声，再叠一层 TTS 就是同一句话两个人在说。
        若判据是「盘上有 narration.wav 就烧」的零开关，这条路径必被坑：
        章节先走 kenburns/dubbed（这两种模式 tts 是标配）→ 切成 native →
        旧 narration.wav 原样留在盘上（切 motion **不清** audio.narration_file）→
        assemble 照烧；compose 还先 `_sync_narration` 把它按当前时间轴重拼对齐，
        于是"跟画面对得上"，只是凭空多一层人声、全程零提示。

        本用例按四种组合钉死 compose 的 burn_narr 真值表（源级判据，
        与 `Project.native_voiceover` 的缺省一起）。"""
        from kinema.project import Project

        def _burn(motion, has_narr, opt_in):
            """复刻 compose.build 的 burn_narr 判据（真值表口径）。"""
            pr = Project("p.json", {"motion": motion,
                                     **({"native_voiceover": True} if opt_in else {})})
            use_clip_audio = pr.native_audio
            return bool(has_narr and (not use_clip_audio
                                      or (pr.native_audio and pr.native_voiceover)))

        # native：有配音也不烧，除非显式开
        self.assertFalse(_burn("native", True, False),
                         "native 默认不烧——否则切 motion 留下的陈旧配音轨会被静默烧进成片")
        self.assertTrue(_burn("native", True, True), "显式 opt-in 后要烧")
        # dubbed：恒烧我们的 TTS——片段音轨是模型对参考音的重演，嗓音逐镜自选、
        # 不进成片；固定音色的承诺由旁白轨兑现
        self.assertTrue(_burn("dubbed", True, False))
        self.assertTrue(_burn("dubbed", True, True))
        # kenburns：片段无音轨，narration 就是主音轨，恒烧
        self.assertTrue(_burn("kenburns", True, False))
        self.assertFalse(_burn("kenburns", False, False), "没配音自然没得烧")

    def test_native_voiceover_defaults_to_off_and_is_runtime_overridable(self):
        """开关缺省关；`--burn-voice` 走 override_runtime（本次生效、绝不落盘）——
        与 --motion/--aspect 同一条纪律：flag 表达「这一次这么跑」。"""
        from kinema.project import Project
        pr = Project("p.json", {"motion": "native"})
        self.assertFalse(pr.native_voiceover, "缺省必须是不烧")
        pr.override_runtime("native_voiceover", True)
        self.assertTrue(pr.native_voiceover)
        # CLI 接线：--burn-voice 只经 override_runtime 落到本次渲染
        import inspect

        from kinema import cli
        src = inspect.getsource(cli._apply_aspect_args)
        self.assertIn('ov("native_voiceover", True)', src)
        self.assertIn("burn_voice", src)

    def test_skipping_the_burn_is_announced_not_silent(self):
        """盘上有配音却不烧时必须打印原因——沉默会让用户/agent 以为「配音丢了」
        而重跑 tts（白花钱），这正是老陷阱的镜像反面。"""
        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        self.assertIn("has_narr and use_clip_audio and not burn_narr", src)
        self.assertIn("--burn-voice", src, "提示里必须给出怎么开")
        self.assertIn("native_voiceover", src, "提示里必须给出常开字段名")
        # 不烧 ≠ 删文件：TTS 是花过钱的产物，切回 kenburns 还要用
        self.assertNotIn("unlink", src)
        self.assertIn("原样保留", src)

    def test_scored_is_one_finished_track_with_nothing_layered_on_it(self):
        """`audio_mode: scored`：音频模型已经把人声/音乐/音效混成一条成品轨，
        我们这一侧**什么都不再叠**——BGM、片段原生音轨、逐镜 TTS 旁白三路全让开。

        叠任何一路都是同一类事故的三个形态：与剧本里的人声撞成两层。
        既然没有 BGM 母线，闪避与让路 EQ 也无从谈起（模型替我们做完了那一层）。"""
        tbl = _table()
        na = mixdown.narration_track(tbl, "score.wav", dur=_DUR)   # 成品轨走主轨 0 dB
        graph = _full(tbl, narration=na, bgm=None)
        self.assertNotIn("sidechaincompress", graph, "不叠 BGM 就没有可闪避的对象")
        self.assertNotIn("aloop", graph, "成品轨是整片一条，绝不循环铺底")
        self.assertIn("alimiter", graph)                # 末级削波防护仍是无条件的

        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        self.assertIn("scored = project.scored_audio", src)
        rule = inspect.getsource(compose_mod.use_bgm_for)
        self.assertLess(rule.index("scored_audio"), rule.index("native_audio"),
                        "scored 缺省必须关掉 BGM（模型已配过乐），scored_bgm 是唯一的"
                        "显式例外——分支必须先于 native 短路，否则 native 章节声明了"
                        "也静默失效（片段音轨已让开，例外与画面模式无关）")
        self.assertIn("use_clip_audio = False", src,
                      "scored 下 Seedance 片段自带音轨必须让开，否则两层人声")
        self.assertIn("burn_narr and not scored", src,
                      "scored 下逐镜旁白轨不参与：这条路根本没有逐镜 wav")
        # 缺音轨要报错而不是静默出一条哑片——音轨是这条路的全部产出
        self.assertIn("audio_mode=scored", src)
        self.assertIn("kinema-audio", src, "报错里要指向剧本写在哪、怎么写")

    def test_scored_says_why_the_narration_on_disk_is_not_used(self):
        """切过路线的章节盘上会同时存在 narration.wav 与 score.wav。
        不用前者时必须出声——沉默会被读成「配音丢了」而重跑 tts（白花钱），
        与 native 那条「不烧也要说」是同一条纪律。"""
        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        self.assertIn("has_narr and scored", src)
        self.assertIn("原样保留", src)
        self.assertNotIn("unlink(narration", src, "不用 ≠ 删文件：那是花过钱的产物")

    def test_master_bounds_differ_per_mode(self):
        # kenburns/dubbed 主音轨是我们自己的旁白轨（窄幅）；native 是模型回吐的
        # 重编码音频，响度完全不受控（宽幅救）
        self.assertEqual(mixdown.master_spec("kenburns")["max_gain"], 9.0)
        self.assertEqual(mixdown.master_spec("dubbed")["max_gain"], 9.0)
        self.assertEqual(mixdown.master_spec("native")["max_gain"], 14.0)
        self.assertEqual(mixdown.master_spec("不存在的模式"), mixdown.MASTER_MODES["kenburns"])
        far = {"input_i": -40.0, "input_tp": -20.0}
        self.assertEqual(mixdown.master_gain_db(far, motion="kenburns"), 9.0)
        self.assertEqual(mixdown.master_gain_db(far, motion="native"), 14.0)

    def test_solo_chapter_reaches_target(self):
        # 无旁白章节（白噪音/环境音沉浸、金句配乐）：主音轨不在场，在场的全是引擎侧
        # 确定性源 → 独奏档 MASTER_SOLO，末级必须真能推到 LOUDNESS_I
        self.assertEqual(mixdown.BGM_GAIN_SOLO, 1.0)   # BGM 已入轨归一，独奏时它就是节目本体
        self.assertEqual(mixdown.master_spec("kenburns", solo=True), mixdown.MASTER_SOLO)
        self.assertEqual(mixdown.master_spec("native", solo=True), mixdown.MASTER_SOLO)
        # 纯 BGM：入轨归一到 -20 LUFS，独奏不衰减 → 只需 +4 dB 就到目标
        pure_bgm = {"input_i": mixdown.BGM_TARGET_I, "input_tp": mixdown.BGM_TARGET_TP}
        g = mixdown.master_gain_db(pure_bgm, motion="kenburns", solo=True)
        self.assertEqual(mixdown.BGM_TARGET_I + g, mixdown.LOUDNESS_I)
        # 纯环境音：实测最静的一路（snow 床 -50.0 LUFS）也必须够得着目标——
        # 环境音床按设计就是"垫在人声之下"的量级，独奏时它就是全部节目内容
        for amb_i in (-31.6, -48.0, -50.0):            # 实测 rain / fog / snow 独奏
            g = mixdown.master_gain_db({"input_i": amb_i, "input_tp": amb_i + 8},
                                       motion="kenburns", solo=True)
            self.assertAlmostEqual(amb_i + g, mixdown.LOUDNESS_I, delta=0.1,
                                   msg=f"环境音独奏 {amb_i} LUFS 被钳制截断")
        # 有旁白时窄幅钳制原样保留（越权偏差=素材出事，宁可欠推）
        self.assertEqual(mixdown.master_gain_db(pure_bgm, motion="kenburns"), 4.0)
        quiet = {"input_i": -34.3, "input_tp": -16.0}
        self.assertEqual(mixdown.master_gain_db(quiet, motion="kenburns"), 9.0)
        # 独奏档只放宽上推：引擎侧源不会跑热，下压仍是窄幅
        self.assertEqual(mixdown.MASTER_SOLO["min_gain"],
                         mixdown.MASTER_MODES["kenburns"]["min_gain"])
        # 整段静音仍不推（独奏档也不例外——绝不把底噪抬成噪音墙）
        self.assertEqual(mixdown.master_gain_db({"input_i": "-inf"},
                                                motion="kenburns", solo=True), 0.0)


class TestNarrationMatch(unittest.TestCase):
    """native 混烧的两路人声对齐：TTS 旁白与模型对白来源不同，电平相差可达 18 dB
    （旁白 -31.8 / 对白 -13.2 LUFS）；末级归一是整体推，旁白若按 0 dB 入混就被更响的
    对白窗口压在底下。旁白轨入混前按对白镜窗口的实测响度推静态增益，两路同响度后
    末级再推。只在混烧走这条，kenburns/dubbed 的旁白是 0 dB 基准。"""

    def test_narration_track_takes_an_intake_gain_before_the_sidechain_split(self):
        tbl = _table()
        na = mixdown.narration_track(tbl, "narr.wav", dur=_DUR, gain_db=17.4)
        self.assertIn("volume=17.4dB,apad", tbl.audio[0], "增益在 apad 之前、进 [na] 之后分叉给闪避")
        graph = _full(tbl, narration=na, bgm=mixdown.clip_bed_track(tbl, dur=_DUR))
        self.assertLess(graph.index("volume=17.4dB"), graph.index("asplit=2[na_mix][na_sc]"),
                        "闪避由对齐后的旁白驱动")
        # 缺省 0 dB：不写 volume 节点（kenburns/dubbed 主轨形态不变）
        tbl = _table()
        mixdown.narration_track(tbl, "narr.wav", dur=_DUR)
        self.assertNotIn("volume=", tbl.audio[0])

    def test_match_gain_targets_the_measured_dialogue_level(self):
        # 旁白窗口 -30.7、对白窗口 -13.3 → +17.4
        self.assertEqual(mixdown.narration_match_gain_db({"input_i": -30.7},
                                                         {"input_i": -13.3}), 17.4)
        # 钳制：TTS 侧最深 +18，负向 -12
        self.assertEqual(mixdown.narration_match_gain_db({"input_i": -40.0},
                                                         {"input_i": -10.0}), 18.0)
        self.assertEqual(mixdown.narration_match_gain_db({"input_i": -10.0},
                                                         {"input_i": -30.0}), -12.0)
        # 对白测不到 → 没有对齐目标，保持 0 dB 交末级；旁白测不到 / 近静音 → 0
        self.assertEqual(mixdown.narration_match_gain_db({"input_i": -30.7}, None), 0.0)
        self.assertEqual(mixdown.narration_match_gain_db({"input_i": -30.7},
                                                         {"input_i": "-inf"}), 0.0)
        self.assertEqual(mixdown.narration_match_gain_db(None, {"input_i": -13.3}), 0.0)
        self.assertIn("不对齐", mixdown.narration_match_report({"input_i": -30.7}, None, 0.0))
        self.assertIn("+17.4 dB", mixdown.narration_match_report(
            {"input_i": -30.7}, {"input_i": -13.3}, 17.4))
        self.assertIn("钳制", mixdown.narration_match_report(
            {"input_i": -40.0}, {"input_i": -10.0}, 18.0))

    def test_window_measurement_selects_only_the_windows(self):
        args = mixdown.measure_windows_args("clips.mp4", [(6.0, 13.0), (13.0, 22.0)])
        af = args[args.index("-af") + 1]
        self.assertTrue(af.startswith("aselect='between(t,6.000,13.000)+between(t,13.000,22.000)'"))
        self.assertIn("asetpts=N/SR/TB", af)             # 重排时间戳，loudnorm 看到连续音频
        self.assertTrue(af.endswith(mixdown.MEASURE_FILTER))
        self.assertEqual(args[-3:], ["-f", "null", "-"])

    def test_compose_matches_only_when_burning_and_only_against_dialogue_windows(self):
        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        burn = src.index("narration and project.native_audio")
        match = src.index("narration_match_gain_db")
        self.assertLess(burn, match, "对齐只在混烧分支")
        self.assertLess(match, src.index("clip_bed_track"), "对齐在旁白轨与床轨之前算好")
        self.assertIn('voicecast.voice_kind(s) == "dialogue"', src, "对白窗口取自 timeline 的对白镜")
        self.assertIn("gain_db=match_db", src)
        self.assertIn("if dl_wins:", src, "整章无对白镜时没有对齐目标，不测不推")
        # kenburns/dubbed/scored 的主轨调用不带增益
        self.assertEqual(src.count("gain_db=match_db"), 1)

    @unittest.skipUnless(_HAS_FFMPEG, "需要 ffmpeg")
    def test_window_measurement_on_real_ffmpeg_recovers_the_level_gap(self):
        """-30 dB 与 -13 dB 两段正弦，按窗口测出的差值应为 17 dB（±1）。"""
        with tempfile.TemporaryDirectory() as td:
            narr = Path(td) / "narr.wav"
            clip = Path(td) / "clip.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=6",
                            "-af", "volume=-30dB", str(narr)], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=6",
                            "-af", "volume=-13dB", str(clip)], check=True)
            n = mixdown.measure_loudness(mixdown.measure_windows_args(narr, [(0.0, 3.0)]))
            d = mixdown.measure_loudness(mixdown.measure_windows_args(clip, [(3.0, 6.0)]))
            self.assertIsNotNone(n); self.assertIsNotNone(d)
            self.assertAlmostEqual(mixdown.narration_match_gain_db(n, d), 17.0, delta=1.0)


class TestBgmStaleness(unittest.TestCase):
    """stage_music 的幂等判据：文件在否之外必须比对实测时长——时间轴改过
    （补镜/重跑 tts 覆写 dur）后旧曲铺满片长，mixdown 的 aloop 会让曲尾淡出在
    成片中段变成「淡出到静音再从头淡入」的断层，而日志打印的却是新参数。"""

    class _Music:
        name = "mock"                       # 走 wav 路，免 mp3 编码器依赖

        def __init__(self, calls):
            self.calls = calls

        def generate(self, prompt, out, *, duration, mood=None):
            self.calls.append(round(float(duration), 1))
            subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                            "-t", str(duration), "-y", str(out)], capture_output=True)

            class _R:
                cost = 0.0
            return _R()

    @unittest.skipUnless(_HAS_FFMPEG, "需要 ffmpeg")
    def test_duration_mismatch_regenerates(self):
        import contextlib
        import io
        import json
        from kinema import cli
        from kinema.project import Project
        calls: list = []
        prov = self._Music(calls)

        class _Router:
            def resolve(self, cap, prof):
                return prov, {}

        class _Store:
            default_profile = "narration"

        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "ch01.json"
            cf.write_text(json.dumps({"motion": "kenburns", "shots": [
                {"id": 1, "dur": 10.0, "narration": "x"}]}), encoding="utf-8")
            project = Project.load(cf)
            out = project.subdir("audio") / "bgm.wav"
            subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                            "-t", "2", "-y", str(out)], capture_output=True)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.stage_music(project, _Store(), _Router())     # 2s 旧曲 vs 10s 片长
            self.assertEqual(calls, [10.0], "时长不符必须重生")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.stage_music(project, _Store(), _Router())     # 时长已符 → 幂等跳过
            self.assertEqual(calls, [10.0], "时长相符不许重复计费")
            self.assertEqual(project.audio["bgm_params"]["duration"], 10.0,
                             "生成参数快照要落 project.audio 供复盘")


class TestAmbient(unittest.TestCase):
    def test_ambient_not_ducked(self):
        # 环境音（雨/风）与转场音效走独立母线，直接进 amix，绝不进侧链
        tbl = _table()
        narr = mixdown.narration_track(tbl, "narration.wav", dur=_DUR)
        bg = mixdown.bgm_track(tbl, "bgm.mp3", dur=_DUR, ducked=True)
        amb = mixdown.ambient_track(tbl, "anoisesrc=color=pink", "volume=0.2", dur=_DUR)
        sfx = mixdown.transition_sound_track(tbl, filt="anull", dur=_DUR, delay=1.0,
                                             lavfi="sine=f=100")
        graph = _full(tbl, narration=narr, bgm=bg, ambient=[amb, sfx])
        duck_line = [l for l in tbl.audio if "sidechaincompress" in l][0]
        self.assertNotIn(amb, duck_line)
        self.assertNotIn(sfx, duck_line)
        mix_line = [l for l in tbl.audio if "amix=" in l][0]
        self.assertIn(f"[{amb}]", mix_line)
        self.assertIn(f"[{sfx}]", mix_line)
        self.assertIn("[na_mix][bg_duck]", mix_line)          # 旁白与闪避后的 BGM
        self.assertIn("normalize=0", mix_line)
        self.assertIn("amix=inputs=4", graph)

    def test_sfx_bus_gain_applied(self):
        # 转场音效母线统一收电平：合成 boom 自带 volume=2.2，直接入轨就是削波元凶
        tbl = _table()
        mixdown.transition_sound_track(tbl, filt="anull", dur=_DUR, delay=0.5,
                                       lavfi="sine=f=54")
        self.assertIn(f"volume={mixdown.SFX_GAIN}", tbl.audio[0])
        self.assertLess(mixdown.SFX_GAIN, 1.0)
        self.assertIn("adelay=500|500", tbl.audio[0])


class TestMaster(unittest.TestCase):
    def test_limiter_or_loudnorm_at_tail(self):
        # 末级必须有响度归一 + 削波防护，且是**最后**一环
        tbl = _table()
        narr = mixdown.narration_track(tbl, "narration.wav", dur=_DUR)
        bg = mixdown.bgm_track(tbl, "bgm.mp3", dur=_DUR, ducked=True)
        graph = _full(tbl, narration=narr, bgm=bg, gain=6.5)
        self.assertTrue(tbl.audio[-1].endswith("[aout]"))
        self.assertIn("volume=6.5dB", tbl.audio[-1])
        self.assertIn(f"alimiter=level_in=1:level_out=1:limit={mixdown.LIMIT_PEAK}",
                      tbl.audio[-1])
        self.assertIn("level=disabled", tbl.audio[-1])        # 默认 enabled 会把增益抹掉
        self.assertLess(graph.index("amix="), graph.index("alimiter"))
        # 限幅上限与真峰目标同源
        self.assertAlmostEqual(mixdown.LIMIT_PEAK, 10 ** (mixdown.LOUDNESS_TP / 20), places=3)
        self.assertEqual((mixdown.LOUDNESS_I, mixdown.LOUDNESS_TP, mixdown.LOUDNESS_LRA),
                         (-16.0, -1.5, 11.0))

    def test_master_gain_from_measurement(self):
        # 实测样片甲 -23.3 LUFS → 推 +7.3 dB 到目标；样片乙 -22.5 → +6.5
        self.assertEqual(mixdown.master_gain_db({"input_i": -23.3, "input_tp": -7.9},
                                                motion="kenburns"), 7.3)
        self.assertEqual(mixdown.master_gain_db({"input_i": -22.5, "input_tp": -0.9},
                                                motion="kenburns"), 6.5)
        # 峰值不参与末级钳制（波峰因子 20+ dB 时按峰值收等于放弃归一），
        # 超出的那几个瞬态交给限幅器：-0.9 + 6.5 已到 +5.6 dB，须削到 -1.5 dBTP
        self.assertEqual(mixdown.peak_reduction_db({"input_tp": -0.9}, 6.5), 7.1)
        # 测不到 / 整段静音 → 不推增益（绝不把底噪抬成噪音墙）
        self.assertEqual(mixdown.master_gain_db(None, motion="dubbed"), 0.0)
        self.assertEqual(mixdown.master_gain_db({"input_i": "-inf"}, motion="dubbed"), 0.0)
        # 增益为 0 时不写 volume 节点，但限幅永远在
        self.assertNotIn("volume=", mixdown.master_filter(0.0))
        self.assertIn("alimiter", mixdown.master_filter(0.0))

    def test_bgm_intake_normalized(self):
        # BGM 入轨归一：各曲目推到同一响度床（同一情绪目录横跨 6.5 dB）
        self.assertEqual(mixdown.BGM_TARGET_I, -20.0)
        self.assertEqual(mixdown.bgm_gain_db({"input_i": -26.0, "input_tp": -12.0}), 6.0)
        self.assertEqual(mixdown.bgm_gain_db({"input_i": -16.3, "input_tp": -0.7}), -3.7)
        # 安静但峰值顶格的曲子（波峰因子 22 dB）照推不误——峰值交给 BGM 母线自己的
        # 限幅器；"按峰值少推一点"正是"有的集轻"的成因
        self.assertEqual(mixdown.bgm_gain_db({"input_i": -22.8, "input_tp": -0.1}), 2.8)
        self.assertEqual(mixdown.bgm_gain_db(None), 0.0)
        # BGM 母线自带限幅（写进 mp3 的削平救不回来），档位比成片末级更保守
        chain = mixdown.master_filter(2.8, limit=mixdown.BGM_LIMIT_PEAK)
        self.assertIn("volume=2.8dB", chain)
        self.assertIn(f"limit={mixdown.BGM_LIMIT_PEAK}", chain)
        self.assertIn("level=disabled", chain)
        self.assertLess(mixdown.BGM_LIMIT_PEAK, mixdown.LIMIT_PEAK)

    def test_parse_measurement_survives_logs(self):
        # loudnorm 的 JSON 块后面还有 ffmpeg 日志行，取末尾会抓空
        stderr = ('[Parsed_loudnorm_0 @ 0x1] \n{\n\t"input_i" : "-23.30",\n'
                  '\t"input_tp" : "-7.90",\n\t"input_lra" : "2.20",\n'
                  '\t"input_thresh" : "-33.50",\n\t"normalization_type" : "linear"\n}\n'
                  '[out#0/null @ 0x2] video:0KiB audio:750KiB muxing overhead: unknown\n'
                  'size=N/A time=00:00:12.00 bitrate=N/A speed=164x\n')
        got = mixdown.parse_measurement(stderr)
        self.assertEqual(got["input_i"], -23.3)
        self.assertEqual(got["input_lra"], 2.2)
        self.assertEqual(got["normalization_type"], "linear")   # 非数值字段原样保留
        self.assertIsNone(mixdown.parse_measurement(""))
        self.assertIsNone(mixdown.parse_measurement("ffmpeg: no such file"))

    def test_report_flags_abnormal_material(self):
        # 对账日志：正常情况一行；峰值/动态异常各追加一条告警（合成时肉眼可查）
        normal = mixdown.report({"input_i": -23.3, "input_tp": -7.9, "input_lra": 2.2}, 7.3)
        self.assertIn("-23.3 → -16.0 LUFS", normal)
        self.assertIn("+7.3 dB", normal)
        self.assertNotIn("⚠", normal)
        hot = mixdown.report({"input_i": -22.5, "input_tp": -0.9, "input_lra": 7.1}, 6.5)
        self.assertIn("峰值需削", hot)                        # 削 7.1 dB > 6 dB 阈值
        wide = mixdown.report({"input_i": -20.0, "input_tp": -12.0, "input_lra": 15.0}, 4.0)
        self.assertIn("响度范围", wide)
        self.assertIn("末级只保留限幅", mixdown.report(None, 0.0))
        self.assertIn("整段近静音", mixdown.report({"input_i": "-inf"}, 0.0))

    def test_measure_graph_is_audio_only(self):
        # 分析命令只带音频子图：视频链带着未连接输出会让 ffmpeg 直接报错
        tbl = _table()
        tbl.video.append("[0:v]scale=1920:1080[vout]")
        narr = mixdown.narration_track(tbl, "narration.wav", dur=_DUR)
        pre = mixdown.premix_graph(tbl, narration=narr, bgm=None)
        args = mixdown.measure_mix_args(tbl, pre)
        graph = args[args.index("-filter_complex") + 1]
        self.assertNotIn("scale=1920", graph)
        self.assertIn("loudnorm=", graph)
        self.assertIn("print_format=json", graph)
        self.assertEqual(args[-5:], ["-map", "[lnmeas]", "-f", "null", "-"])
        self.assertEqual(args[:2], ["-i", "silent.mp4"])       # 输入编号与实发同源


class TestInputTable(unittest.TestCase):
    def test_shared_index_across_video_and_audio(self):
        # 视频叠层与音频轨共用同一个输入号计数器（这是过去混音段无法单测的根因）
        tbl = _table()
        self.assertEqual(tbl.add_lavfi("color=c=black:s=2x2"), 1)   # 特效叠层
        self.assertEqual(tbl.add_input("bgm.mp3"), 2)               # 音频
        self.assertEqual(tbl.add_lavfi("sine=f=100"), 3)
        self.assertEqual(tbl.args.count("-i"), 4)
        tbl.video.append("V")
        tbl.audio.append("A")
        self.assertEqual(tbl.filters, ["V", "A"])

    def test_no_mix_when_silent(self):
        tbl = _table()
        self.assertIsNone(mixdown.premix_graph(tbl, narration=None, bgm=None))
        self.assertEqual(tbl.audio, [])


class TestTransitionSoundLead(unittest.TestCase):
    def test_transition_sound_lead_independent_of_edge(self):
        # 9 种转场里 seamless/wipe/circle/slide/blur/scan 六种 edge=0.0，靠 edge 拿不到
        # 任何提前量；SOUND_LEAD 是独立常量，edge=0 的类型也必须提前起声
        # （seamless 缺省静音，本用例只验证提前量算术，与是否真挂声无关）
        zero_edge = [t for t, m in tr.TRANSITIONS.items() if m["edge"] == 0.0]
        self.assertEqual(set(zero_edge),
                         {"seamless", "wipe", "circle", "slide", "blur", "scan"})
        self.assertGreater(mixdown.SOUND_LEAD, 0.0)
        for t in zero_edge:
            spec = tr.spec_of({"kind": "transition", "transition": {"type": t}})
            self.assertEqual(spec["edge"], 0.0)
            self.assertAlmostEqual(mixdown.sound_start(10.0, spec["edge"]),
                                   10.0 - mixdown.SOUND_LEAD)
        # 字卡族仍在前镜淡出起点之上再提前
        black = tr.spec_of({"kind": "transition", "transition": {"type": "fade_black"}})
        self.assertAlmostEqual(mixdown.sound_start(10.0, black["edge"]),
                               10.0 - 0.25 - mixdown.SOUND_LEAD)
        # 片头转场不能出现负时间（adelay 会炸）
        self.assertEqual(mixdown.sound_start(0.1, 0.0), 0.0)

    def test_edge_and_span_formulas_untouched(self):
        # 提前量绝不靠改 TRANSITIONS.edge 实现：改了会连带改画面淡化时长与片段缓存键
        fade = {"kind": "transition", "dur": tr.default_dur("fade"),
                "transition": {"type": "fade"}}
        self.assertAlmostEqual(tr.total_span(fade), 0.5)
        self.assertIn("atrim=0:1.300", tr.fit_sound_filter(0.9))


class _LavfiTable(mixdown.InputTable):
    """冒烟层专用输入表：把文件输入改成 lavfi 造源，**零素材文件入仓**。"""

    SOURCES = {
        "narration.wav": "sine=f=300:r=44100:d=2",
        "bgm.mp3": "anoisesrc=color=brown:r=44100:d=2",
        "sfx.wav": "sine=frequency=54:r=44100:d=0.6",
    }

    def __init__(self, first: str = "sine=f=300:r=44100:d=2"):
        super().__init__("placeholder")
        self.args = ["-f", "lavfi", "-i", first]

    def add_input(self, path):
        return self.add_lavfi(self.SOURCES[str(path)])


@unittest.skipUnless(_HAS_FFMPEG, "需要 ffmpeg")
class TestMixSmoke(unittest.TestCase):
    """把混音图按 compose 的编织方式在真实 ffmpeg 上跑通——直接抓语法/滤镜可用性
    （alimiter 的 level=disabled、sidechaincompress 参数名、adelay 落位…）。"""

    def _run(self, tbl, amap):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *tbl.args,
               "-filter_complex", ";".join(tbl.filters), "-map", amap,
               "-t", "1", "-f", "null", "-"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-1200:])

    def test_full_chain_renders(self):
        # 旁白 + BGM（让路 EQ + 闪避）+ 环境音 + 转场音效 → 相加 → 增益 + 限幅
        tbl = _LavfiTable()
        narr = mixdown.narration_track(tbl, "narration.wav", dur=1.0)
        bg = mixdown.bgm_track(tbl, "bgm.mp3", dur=1.0, ducked=True)
        amb = mixdown.ambient_track(tbl, "anoisesrc=color=pink:r=44100:d=2",
                                    "volume=0.2", dur=1.0)
        sfx = mixdown.transition_sound_track(
            tbl, filt=tr.fit_sound_filter(0.6), dur=1.0, delay=0.2, file="sfx.wav")
        pre = mixdown.premix_graph(tbl, narration=narr, bgm=bg, ambient=[amb, sfx])
        self._run(tbl, mixdown.master_graph(tbl, pre, 6.5))

    def test_native_chain_renders(self):
        # native：只有片段音轨这一路（无 BGM/闪避），末级仍要跑通
        tbl = _LavfiTable()
        na = mixdown.clip_audio_track(tbl, dur=1.0)
        pre = mixdown.premix_graph(tbl, narration=na, bgm=None)
        self._run(tbl, mixdown.master_graph(tbl, pre, -3.0))

    def test_synth_transition_sound_renders(self):
        # 合成兜底路线（boom 自带 volume=2.2）经母线衰减后仍是合法链
        tbl = _LavfiTable()
        na = mixdown.clip_audio_track(tbl, dur=1.0)
        src, filt = tr.whoosh_audio(0.6, kind="boom")
        sfx = mixdown.transition_sound_track(tbl, filt=filt, dur=1.0, delay=0.1, lavfi=src)
        pre = mixdown.premix_graph(tbl, narration=na, bgm=None, ambient=[sfx])
        self._run(tbl, mixdown.master_graph(tbl, pre, 0.0))

    def test_measure_pass_reports_loudness(self):
        # 分析命令能真测出响度，并据此算出非零静态增益
        tbl = _LavfiTable("sine=f=1000:r=44100:d=2")
        na = mixdown.clip_audio_track(tbl, dur=2.0)
        pre = mixdown.premix_graph(tbl, narration=na, bgm=None)
        got = mixdown.measure_loudness(mixdown.measure_mix_args(tbl, pre))
        self.assertIsNotNone(got)
        self.assertLess(got["input_i"], 0.0)
        self.assertNotEqual(mixdown.master_gain_db(got, motion="kenburns"), 0.0)

    def test_measure_never_raises_on_bad_args(self):
        # 响度体检失败绝不能把合成卡死——退化为「测不到 → 增益 0 → 只留限幅」
        self.assertIsNone(mixdown.measure_loudness(["-i", "/无此文件.wav",
                                                    "-f", "null", "-"]))

    def _render(self, args, out):
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            *args, "-ar", "44100", "-ac", "2", str(out)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-1200:])

    def test_solo_chapter_reaches_target_on_real_ffmpeg(self):
        """无旁白章节（白噪音沉浸 / kn-quote 金句配乐）真机走完整两条链：
        BGM 入轨归一写盘 → compose 独奏路径 → 末级归一，**成片响度必须真到 LOUDNESS_I**。

        钉死的是「独奏母线 × 钳制区间」的合谋：BGM_GAIN_SOLO 退回 0.2 或独奏档退回
        MASTER_MODES，实测都会掉到 -25 LUFS（比有旁白章节低 9 dB），而纯函数断言看不出来。
        """
        src = ["-f", "lavfi", "-i", "anoisesrc=color=brown:r=44100:d=10:a=0.3", "-t", "8"]
        with tempfile.TemporaryDirectory() as d:
            # ① 入轨归一：与 providers/music/local 写盘那一步同一条 (bgm_gain_db + master_filter)
            gain = mixdown.bgm_gain_db(
                mixdown.measure_loudness(mixdown.measure_file_args(src)))
            bgm = Path(d) / "bgm.wav"
            self._render([*src, "-af",
                          mixdown.master_filter(gain, limit=mixdown.BGM_LIMIT_PEAK)], bgm)
            # ② compose 独奏路径：无旁白 → bgm_track(ducked=False) → premix → 末级
            tbl = mixdown.InputTable(bgm)          # 0 号=无声成片位（音频链不引用）
            bg = mixdown.bgm_track(tbl, bgm, dur=8.0, ducked=False)
            pre = mixdown.premix_graph(tbl, narration=None, bgm=bg)
            measured = mixdown.measure_loudness(mixdown.measure_mix_args(tbl, pre))
            self.assertIsNotNone(measured)
            g = mixdown.master_gain_db(measured, motion="kenburns", solo=True)
            amap = mixdown.master_graph(tbl, pre, g)     # 先挂末级链再拼图（顺序敏感）
            out = Path(d) / "out.wav"
            self._render([*tbl.args, "-filter_complex", ";".join(tbl.filters),
                          "-map", amap, "-t", "8"], out)
            final = mixdown.measure_loudness(
                mixdown.measure_file_args(["-i", str(out)]))
            self.assertIsNotNone(final)
            self.assertAlmostEqual(final["input_i"], mixdown.LOUDNESS_I, delta=1.0,
                                   msg=f"无旁白章节成片只到 {final['input_i']} LUFS")


class TestClipCacheKey(unittest.TestCase):
    """片段缓存键（compose._clip_cache_name）：文件名负责**参数**过期、源指纹负责
    **内容**过期，两者合起来才是「同输入同输出」。运镜风格号必须进缓存键——
    否则改 `shots[].camera` 时源图 mtime/dur 都没变，成片静默复用旧运镜片段。"""

    def test_camera_change_changes_cache_key(self):
        from kinema.pipeline import compose, kenburns
        s = {"id": 3, "camera": "缓慢推近"}
        a = compose._clip_cache_name(s, kenburns.style_for(s["camera"], 0), 0, 0)
        s2 = {"id": 3, "camera": "拉远揭示"}
        b = compose._clip_cache_name(s2, kenburns.style_for(s2["camera"], 0), 0, 0)
        self.assertNotEqual(a, b, "换运镜语义必须换缓存键，否则旧运镜片段被静默复用")

    def test_omit_shift_changes_cache_key(self):
        # 无 camera 语义时风格随镜位轮换——前面弃一镜、位移变了，风格随之变，
        # 缓存键必须跟着变，否则会复用「老位置」的旧风格片段
        from kinema.pipeline import compose, kenburns
        s = {"id": 5}
        a = compose._clip_cache_name(s, kenburns.style_for(None, 4), 0, 0)
        b = compose._clip_cache_name(s, kenburns.style_for(None, 3), 0, 0)
        self.assertNotEqual(a, b)

    def test_gen_clip_has_no_style_component(self):
        # 图生视频片段没有 Ken Burns 运镜：不掺风格号，改 camera 不触发无谓重渲
        from kinema.pipeline import compose
        self.assertEqual(compose._clip_cache_name({"id": 2}, None, 0, 0),
                         "shot_2.mp4")

    def test_fade_params_all_enter_the_key(self):
        """淡化的**每个渲染输入都要进键**：秒数两位小数带分隔（round×10 会把
        0.25 与 0.2 折成同键、fade↔fade_black 邻镜共键）、底色一改必换键
        （改转场底色不换键=「改了不生效」，只能 --force 全量重渲）。"""
        from kinema.pipeline import compose, kenburns
        v = f"a{kenburns.ALGO_VERSION}" if kenburns.ALGO_VERSION > 1 else ""
        self.assertEqual(compose._clip_cache_name({"id": 7}, 4, 0, 0),
                         f"shot_7_k4{v}.mp4")                 # 无淡化不带后缀
        base = compose._clip_cache_name({"id": 7}, 4, 0.5, 1.0)
        self.assertNotEqual(base, compose._clip_cache_name({"id": 7}, 4, 0.25, 1.0))
        self.assertNotEqual(compose._clip_cache_name({"id": 7}, 4, 0.25, 0),
                            compose._clip_cache_name({"id": 7}, 4, 0.2, 0),
                            "0.25 与 0.2 必须不同键（round×10 同为 2 的旧病）")
        black = compose._clip_cache_name({"id": 7}, 4, 0.5, 1.0,
                                         fic="black", foc="black")
        tinted = compose._clip_cache_name({"id": 7}, 4, 0.5, 1.0,
                                          fic="black", foc="0xEFE6D3")
        self.assertNotEqual(black, tinted, "换淡化底色必须换键")

    def test_orphan_clips_are_swept_after_a_successful_compose(self):
        """缓存键一变，旧键片段就永不再命中却也没人删——每次 ALGO_VERSION
        升级都会在章节工作目录留下一批孤儿片段白占空间。
        清理必须**只动本次没用到的 shot_*.mp4**、且在合成成功之后。"""
        import tempfile

        from kinema.pipeline import compose
        with tempfile.TemporaryDirectory() as d:
            cd = Path(d)
            used = []
            for name in ("shot_1_k0a2.mp4", "shot_2_tr.mp4"):
                (cd / name).write_bytes(b"x")
                used.append(str(cd / name))
            for name in ("shot_1_k0.mp4", "shot_3_k5.mp4", "shot_9_k1_L7f.mp4"):
                (cd / name).write_bytes(b"x")          # 旧键孤儿
            (cd / "concat.txt").write_text("keep me")   # 非片段文件不许碰
            n = compose._sweep_orphan_clips(cd, used)
            self.assertEqual(n, 3)
            left = sorted(f.name for f in cd.iterdir())
            self.assertEqual(left, ["concat.txt", "shot_1_k0a2.mp4", "shot_2_tr.mp4"])

    def test_sweep_runs_only_after_successful_render(self):
        """源级：清理必须排在 `run(...)` 之后——渲染失败/中断时一个字节都不许删
        （那时本次清单不完备，删了等于把还能复用的片段也一并删掉）。"""
        import inspect

        from kinema.pipeline import compose
        src = inspect.getsource(compose.build)
        self.assertIn("_sweep_orphan_clips", src)
        self.assertLess(src.index('run(args, desc=f"compose'),
                        src.index("_sweep_orphan_clips(clips_dir"),
                        "清理不许排在合成之前")

    def test_render_algo_version_busts_the_cache(self):
        """**运镜算法改版必须让旧片段失效**：源指纹（mtime/dur）盯的是素材变化，
        盯不住「算法改了」——不进缓存键的话，改完平滑度用户重合成会静默复用旧片段、
        以为改动没生效（历史上只能靠 --force 全量重渲）。
        图生视频片段（style=None）无 Ken Burns 运镜，不该被这个分量牵连。"""
        from unittest import mock

        from kinema.pipeline import compose, kenburns
        with mock.patch.object(kenburns, "ALGO_VERSION", 2):
            a = compose._clip_cache_name({"id": 1}, 3, 0, 0)
            gen_a = compose._clip_cache_name({"id": 1}, None, 0, 0)
        with mock.patch.object(kenburns, "ALGO_VERSION", 3):
            b = compose._clip_cache_name({"id": 1}, 3, 0, 0)
            gen_b = compose._clip_cache_name({"id": 1}, None, 0, 0)
        self.assertNotEqual(a, b, "算法版本变了缓存键必须变")
        self.assertEqual(gen_a, gen_b, "图生视频片段不带运镜，不该被算法版本牵连")
        with mock.patch.object(kenburns, "ALGO_VERSION", 1):
            self.assertEqual(compose._clip_cache_name({"id": 1}, 3, 0, 0),
                             "shot_1_k3.mp4", "v1 不带后缀：存量片段名不变、不无谓重渲")


if __name__ == "__main__":
    unittest.main()


class TestBgmGate(unittest.TestCase):
    """合成前的 BGM 闸：讲清这一章会得到什么背景乐，且**绝不在无人值守时替人做决定**。

    合成挂在出片主链上，`assemble` 又被 Studio 后台任务、CI 与管道反复调用——
    闸里任何一处「非交互时按缺省走」都要先问一句「这个缺省是不是在替用户拿主意」。
    """

    def _src(self, fn):
        import inspect
        return inspect.getsource(fn)

    def test_native_bgm_is_a_switch_but_never_alongside_burned_voice(self):
        """BGM 母线是单占的：native 混烧已把片段原生音降为背景床占着它，
        再放曲库 BGM 进来会把那条床整个顶掉（`bg_label` 被覆写、模型自带的
        环境与空间感全丢）。compose 侧必须按同一判据兜住——Studio 直调
        stage_compose 是绕过 CLI 闸的真实路径。"""
        from kinema.pipeline import compose as compose_mod
        src = self._src(compose_mod.use_bgm_for)
        self.assertIn('bool(project.data.get("native_bgm")) and not project.native_voiceover',
                      src, "native 加铺必须与混烧互斥，且判据写在 compose 里")
        self.assertIn("use_bgm = use_bgm_for(project)", self._src(compose_mod.build))

    def test_run_and_assemble_select_music_through_one_predicate(self):
        """选曲与用曲判据必须逐字一致：这边跑了那边不认就是白花一次选曲，
        那边认了这边没跑就是 compose 指着一个不存在的 bgm 文件。两条路径
        各写一份选曲，同一份章节文档会出两种成片。"""
        from kinema import cli
        for fn in (cli._stage_audio_bed, cli._bgm_gate):
            self.assertIn("compose_mod.use_bgm_for(project)", self._src(fn),
                          "选曲与用曲只有 compose.use_bgm_for 一份判据")
        for fn in (cli.cmd_run, cli.cmd_assemble):
            body = self._src(fn)
            self.assertIn("_stage_audio_bed(", body)
            self.assertNotIn("stage_music(", body)

    def test_ask_yes_never_blocks_without_a_terminal(self):
        """非 TTY 恒取缺省、绝不读 stdin：后台任务里等输入会一路挂到看门狗
        超时被杀，表现是毫无输出地卡死。"""
        from kinema import cli
        import io
        import sys as _sys
        real = _sys.stdin
        _sys.stdin = io.StringIO("y\n")          # 有内容但 isatty() 为 False
        try:
            self.assertFalse(cli._ask_yes("要吗？", default=False))
            self.assertTrue(cli._ask_yes("要吗？", default=True))
        finally:
            _sys.stdin = real

    def test_gate_makes_no_decision_and_downloads_nothing_when_unattended(self):
        """两处非交互红线，都是「缺省值不等于用户的意思」：

        · native 的表态**不落盘**——存下去等于替他做了决定，而且从此不再问第二遍；
        · 曲库为空时**不自动拉库**——那是一次上百个文件的网络下载，无人值守的
          进程里自作主张开始下载是不能接受的副作用。
        """
        from kinema import cli
        src = self._src(cli._bgm_gate)
        head, tail = src.split("download.py", 1)
        self.assertIn("if not sys.stdin.isatty():", head,
                      "native 表态落盘前必须先硬判交互性，不能靠 _ask_yes 的缺省值")
        self.assertIn("if not sys.stdin.isatty():", tail,
                      "拉曲库前必须先硬判交互性")
        self.assertLess(tail.index("if not sys.stdin.isatty():"),
                        tail.index("subprocess.call"),
                        "交互性判定必须早于真正发起下载")

    def test_background_jobs_have_no_stdin(self):
        """Studio 起子进程不给 stdin，引擎里带确认的闸就一定读不到 TTY。
        不显式断开时子进程继承服务端的 stdin，而 Studio 常常是从终端起的。"""
        from kinema.studio import jobs
        self.assertIn("stdin=subprocess.DEVNULL", self._src(jobs._stream))


class TestDubbedMainTrack(unittest.TestCase):
    """Seedance 参考媒体是重演不是嵌入：片段人声嗓音逐镜自选（实测与发去的 TTS
    包络相关性极低），同一角色跨镜换声。固定音色的承诺由旁白轨兑现——片段原声
    只在 native 保留；dubbed 的字幕落点随主音轨改探逐镜 wav（对片段音轨探测会把
    字幕对到一条观众听不到的轨上）。"""

    def test_clip_audio_only_survives_on_native(self):
        import inspect

        from kinema.pipeline import compose as compose_mod
        src = inspect.getsource(compose_mod.build)
        self.assertIn("use_clip_audio = project.native_audio", src)

    def test_dubbed_subtitle_spans_probe_the_tts_wav(self):
        import inspect

        from kinema.pipeline import compose as compose_mod
        spans = inspect.getsource(compose_mod.speech_spans_resolver)
        self.assertIn("if burned:", spans)
        self.assertIn('f"shot_{sid}.wav"', spans)


class TestBurnDoubleVoiceGate(unittest.TestCase):
    """混烧前拦下「烧录承担的旁白镜生成时模型被要求出声」的存量片段：闪避只在
    我们的音轨出声时触发，模型把同一段挪到句间静音段念时压不住，成片必然
    同一段两个人声——不存在可交付的形态，故硬拦而非告警放行。声源按镜分治，
    对白镜由模型发声、旁白轨对它插静音，出声措辞在对白镜上是正稿不进闸。"""

    @staticmethod
    def _positive(shot, **options):
        """快照正文取真发编译器的输出：闸的判据必须与产生端同一份字面量，
        测试里手抄串会让措辞一改就静默放行（旁白锚定的 @配音N 就切开过它）。"""
        from kinema.pipeline.prompts import PromptCompiler
        return PromptCompiler().video(dict(shot), native=True,
                                      **options).as_dict()["positive"]

    def _project(self, shot, positive):
        import json
        import tempfile

        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = dict(shot)
        if positive is not None:
            s["gen"] = {"clip": {"envelope": {"positive": positive}}}
        data = {"id": "ch01", "motion": "native", "native_voiceover": True,
                "aspect": "16:9", "shots": [s]}
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project(cf, data)

    def _gate(self, shot, positive):
        from kinema.pipeline import compose
        compose._gate_native_double_voice(self._project(shot, positive))

    def test_speaking_voiceover_clip_is_refused_with_both_exits(self):
        from kinema.errors import ProjectError
        shot = {"id": 1, "dur": 4.0, "narration": "走。"}
        with self.assertRaises(ProjectError) as ctx:
            self._gate(shot, self._positive(shot))
        msg = str(ctx.exception)
        self.assertIn("镜 1", msg)
        self.assertIn("retake", msg)
        self.assertIn("native_voiceover: false", msg)

    def test_anchored_voiceover_clip_is_refused(self):
        """旁白带音色锚定时实发正文是「画外旁白 @配音1 讲述：」——出声措辞被
        编号标记切开，按措辞枚举判就恒放行，而这正是默认路径（native 章自动选角）。"""
        from kinema.errors import ProjectError
        from kinema import voicecast
        shot = {"id": 1, "dur": 4.0, "narration": "走。"}
        pos = self._positive(shot, voice_anchors=[
            {"who": voicecast.NARRATOR_DISPLAY, "voice_type": "v", "no": 1}])
        self.assertIn("@配音1", pos, "锚定标记不在场时本用例退化成普通开口稿")
        with self.assertRaises(ProjectError):
            self._gate(shot, pos)

    def test_muted_clip_passes(self):
        shot = {"id": 1, "dur": 4.0, "narration": "走。"}
        self._gate(shot, self._positive(shot, native_mute=True))

    def test_clip_generated_without_any_line_passes(self):
        """生成时没有台词（事后才补的 narration）：那条片段里本就没有模型人声。"""
        self._gate({"id": 1, "dur": 4.0, "narration": "走。"},
                   self._positive({"id": 1, "dur": 4.0}))

    def test_dialogue_clip_with_vocal_markers_passes(self):
        """对白镜的出声稿是正稿：其人声由模型承担、旁白轨插静音，不构成双声。"""
        shot = {"id": 1, "dur": 4.0, "speaker": "凯尔", "narration": "走。"}
        self._gate(shot, self._positive(shot))

    def test_clip_without_envelope_passes(self):
        """没有生成快照的片段不在判据内——查无实据不拦。"""
        self._gate({"id": 1, "dur": 4.0, "narration": "走。"}, None)


class TestBurnMissingNarrationTrackGate(unittest.TestCase):
    """混烧章整条旁白轨不在盘时拒合成。

    旁白镜按闭声稿出演，人声就是这条轨；轨不在盘时主音轨整体退回片段原生音，
    那几段的人声形态不受控而字幕照烧，且那条分支上没有任何打印。
    盘上已有轨的半缺态归 `narration_parts` 的 missing 逐镜点名，不在本闸射程。"""

    def _project(self, shots, *, motion="native", burn=True, **extra):
        import json
        import tempfile

        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data = {"id": "ch01", "motion": motion, "aspect": "16:9", "shots": shots,
                **extra}
        if burn:
            data["native_voiceover"] = True
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project(cf, data)

    def _gate(self, shots, *, has_narr=False, **kw):
        from kinema.pipeline import compose
        compose._gate_narration_track(self._project(shots, **kw), has_narr)

    def test_missing_track_refuses_and_names_only_narration_shots(self):
        from kinema.errors import ProjectError
        shots = [{"id": 1, "dur": 4.0, "narration": "夜色渐深。"},
                 {"id": 2, "dur": 4.0,
                  "lines": [{"speaker": "林深", "text": "你终于来了。"}]}]
        with self.assertRaises(ProjectError) as ctx:
            self._gate(shots)
        msg = str(ctx.exception)
        self.assertIn("镜 1", msg)
        self.assertIn("tts --chapter", msg)
        self.assertIn("--burn-voice", msg)
        self.assertIn("native_voiceover: false", msg)
        self.assertNotIn("1/2", msg,
                         "对白镜按设计永远没有逐镜 wav，点名它就是把闸写宽")

    def test_all_dialogue_burn_chapter_passes(self):
        """全对白的混烧章没有人声要烧，stage_tts 也不会登记 narration_file。"""
        self._gate([{"id": 1, "dur": 4.0,
                     "lines": [{"speaker": "林深", "text": "你终于来了。"}]}])

    def test_wordless_and_omitted_shots_do_not_arm_the_gate(self):
        self._gate([{"id": 1, "dur": 4.0,
                     "lines": [{"speaker": "林深", "text": "走。"}]},
                    {"id": 2, "dur": 4.0},
                    {"id": 3, "dur": 4.0, "caption": "三年后"},
                    {"id": 4, "dur": 4.0, "narration": "夜色渐深。",
                     "review": {"shot": {"state": "omt"}}}])

    def test_track_on_disk_passes(self):
        self._gate([{"id": 1, "dur": 4.0, "narration": "夜色渐深。"}], has_narr=True)

    def test_scored_chapter_stays_out(self):
        """scored 的人声随音乐音效由音频模型整轨产出，旁白轨不进合成
        （`build` 的 `_sync_narration` 对 scored 直接跳过）。拦它等于要求去合成
        一条随后被丢弃的轨，而 `cmd_run` 的配音门按同一个章级判据本就不会跑。"""
        self._gate([{"id": 1, "dur": 4.0, "narration": "夜色渐深。"}],
                   audio_mode="scored")

    def test_other_motions_stay_out(self):
        """扩到 kenburns 会让 `animatic`（把 motion 覆盖成 kenburns 后直调
        `compose.build`）在跑 tts 之前整条不可达。"""
        shots = [{"id": 1, "dur": 4.0, "narration": "夜色渐深。"}]
        for motion, burn in (("native", False), ("kenburns", True), ("dubbed", True)):
            with self.subTest(motion=motion, burn=burn):
                self._gate(shots, motion=motion, burn=burn)

    def test_gate_is_wired_into_build_and_reuses_voicecast(self):
        import inspect

        from kinema.pipeline import compose
        self.assertIn("_gate_narration_track(project, has_narr)",
                      inspect.getsource(compose.build))
        src = inspect.getsource(compose._gate_narration_track)
        self.assertIn("voicecast.in_narration_track", src)
        self.assertIn("voicecast.shot_text", src)


class TestNarrationTrackIsAChapterLevelQuestion(unittest.TestCase):
    """「这一章要不要产出旁白轨」与「这一镜必须有 audio 产物」是两个量纲。"""

    @staticmethod
    def _project(**data):
        from kinema.project import Project
        return Project(Path("ch01.json"), {"id": "ch01", **data})

    def test_truth_table(self):
        cases = [({"motion": "kenburns"}, True),
                 ({"motion": "dubbed"}, True),
                 ({"motion": "native"}, False),
                 ({"motion": "native", "native_voiceover": True}, True),
                 ({"motion": "native", "native_voiceover": True,
                   "audio_mode": "scored"}, False),
                 ({"motion": "kenburns", "audio_mode": "scored"}, False)]
        for data, want in cases:
            with self.subTest(**data):
                self.assertIs(self._project(**data).needs_narration_track, want)

    def test_shot_level_predicate_is_untouched(self):
        """混烧翻真会让对白镜的 audio 恒 != done——合成前审阅闸随即永久拦死，
        Studio 看板也会给每个对白镜挂一条做不完的待办。"""
        self.assertIs(self._project(motion="native",
                                    native_voiceover=True).needs_tts, False)

    def test_run_asks_the_chapter_level_question(self):
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_run)
        self.assertIn("project.needs_narration_track", src)
        self.assertNotIn("native_voiceover", src, "判据不许在 CLI 里再拼一份")


class TestBurnResidualVoiceProbe(unittest.TestCase):
    """混烧前的输出侧人声探测：闭声稿执行无确定性保证，提示词层的闸拦不住
    模型临场出的那段声——旁白镜片段测到语音段就点名试听（要叠 TTS 旁白，
    残留人声与之直接相撞；只报不拦，振幅判据分不清人声与响亮音效）。
    对白镜的人声由模型承担，不在探测范围。"""

    def _project(self, shots, aspects=("16:9",)):
        import json
        import tempfile

        from kinema.project import Project
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for s in shots:
            for asp, name in (s.get("clips") or {}).items():
                c = tmp / name
                c.write_bytes(b"clip")
                s["clips"][asp] = str(c)
            if s.get("clip"):
                c = tmp / s["clip"]
                c.write_bytes(b"clip")
                s["clip"] = str(c)
        data = {"id": "ch01", "motion": "native", "native_voiceover": True,
                "aspect": aspects[0], "aspects": list(aspects), "shots": shots}
        cf = tmp / "ch01.json"
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project(cf, data)

    def _probe(self, shots, windows, *, aspect="16:9", aspects=("16:9",),
               by_path=None):
        import contextlib
        import io
        import unittest.mock

        from kinema.pipeline import compose
        p = self._project(shots, aspects)
        buf = io.StringIO()
        def _spans(path, *a, **k):
            return by_path(path) if by_path else windows
        with unittest.mock.patch("kinema.pipeline.speech.speech_windows",
                                 side_effect=_spans):
            with contextlib.redirect_stdout(buf):
                compose._warn_native_residual_voice(p, aspect)
        return buf.getvalue()

    def test_voiceover_clip_with_speech_is_named(self):
        out = self._probe([{"id": 1, "dur": 4.0, "speaker": "旁白",
                            "narration": "夜里起了风。", "clip": "s1.mp4"}],
                          [(0.33, 0.97)])
        self.assertIn("镜 1", out)
        self.assertIn("试听", out)

    def test_dialogue_shot_is_not_probed(self):
        out = self._probe([{"id": 1, "dur": 4.0, "speaker": "甲",
                            "narration": "走。", "clip": "s1.mp4"}],
                          [(0.33, 0.97)])
        self.assertEqual(out, "")

    def test_silent_windows_stay_quiet(self):
        out = self._probe([{"id": 1, "dur": 4.0, "speaker": "旁白",
                            "narration": "夜里起了风。", "clip": "s1.mp4"}], [])
        self.assertEqual(out, "")

    def test_probe_follows_the_aspect_being_composed(self):
        """逐比例出片时每个比例是模型的一次独立采样：探的必须是本次在合成的
        那一支，读主比例指针会让另一支的残留人声永远测不到。"""
        shot = {"id": 1, "dur": 4.0, "speaker": "旁白",
                "narration": "夜里起了风。",
                "clips": {"16:9": "s1_16x9.mp4", "9:16": "s1_9x16.mp4"}}
        loud = lambda p: [(0.3, 0.9)] if "9x16" in str(p) else []
        import copy
        self.assertEqual(self._probe(copy.deepcopy([shot]), None,
                                     aspect="16:9", aspects=("16:9", "9:16"),
                                     by_path=loud), "")
        self.assertIn("镜 1", self._probe(copy.deepcopy([shot]), None,
                                          aspect="9:16",
                                          aspects=("16:9", "9:16"),
                                          by_path=loud))


class TestNativeAudioEdge(unittest.TestCase):
    """native 片段音频边缘平滑：一镜一片各自带环境音，硬切处环境床是硬台阶——
    fit_clip 在 keep_audio 下头尾各淡 NATIVE_AUDIO_EDGE 秒抹平（只动音频不动画面）。"""

    def test_fit_clip_audio_filter_carries_edge_fades(self):
        import inspect

        from kinema.pipeline import kenburns
        src = inspect.getsource(kenburns.fit_clip)
        self.assertIn("afade=t=in", src)
        self.assertIn("afade=t=out", src)
        self.assertIn("min(float(audio_edge or 0.0), dur / 2)", src,
                      "淡化秒数须按短镜钳半，超过镜长的淡化=整镜无声")

    def test_compose_passes_edge_and_keys_the_cache(self):
        """参数必须进片段缓存键：不进键的话，旧缓存的硬台阶片段会被静默复用。"""
        import inspect

        from kinema.pipeline import compose
        self.assertGreater(compose.NATIVE_AUDIO_EDGE, 0)
        src = inspect.getsource(compose.build)
        self.assertIn("audio_edge=NATIVE_AUDIO_EDGE", src)
        self.assertIn('"_ae" if use_clip_audio and use_gen', src)


class TestNarrationTrackFromFitOnlyParts(unittest.TestCase):
    """旁白 wav 长于窗口时 `narration_parts` 只给出 `("fit", …)` 段：整轨照样拼、
    `audio.narration_file` 照样登记。只认 `("file", …)` 才算有旁白会让混烧章在
    合成前被缺轨闸拦下。"""

    def test_fit_only_track_is_assembled(self):
        import contextlib
        import io
        import json
        import tempfile
        import unittest.mock as um

        from kinema import voicecast
        from kinema.cli import stage_tts
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "ch01.json"
            cf.write_text(json.dumps({
                "id": "ch01", "aspect": "16:9", "motion": "native", "native_voiceover": True,
                "shots": [{"id": 1, "dur": 4.0, "narration": "凌晨两点，便利店里只剩最后一串关东煮。"},
                          {"id": 2, "dur": 6.0,
                           "lines": [{"speaker": "老周", "text": "最后一串。"}]}]},
                ensure_ascii=False), encoding="utf-8")
            p = Project.load(cf)
            store = ConfigStore.load(None)
            with contextlib.redirect_stdout(io.StringIO()), \
                 um.patch.object(voicecast, "probe_duration", lambda _p: 4.9):
                stage_tts(p, store, ModelRouter(store, force_mock=True))
            narr = p.audio.get("narration_file")
            self.assertTrue(narr and Path(narr).is_file())
