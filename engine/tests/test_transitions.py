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

"""转场系统单测（纯函数，零 ffmpeg 依赖）：spec 归一化、边缘淡化推导、
字卡文字层构造。核心不变量：**转场即特殊镜**——边缘淡化只由相邻转场镜驱动、
底色一致衔接（渐黑接黑卡接黑淡入）、未知类型回落缺省绝不炸合成。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from kinema.pipeline.kenburns import STYLES, _effect
from kinema.audio_registry import EMBEDDED_AUDIO
from kinema.pipeline.transitions import (DEFAULT_TYPE, TRANSITIONS,
                                            build_card_filter, catalog,
                                            default_dur, edge_fades,
                                            fit_sound_filter, is_transition,
                                            pick_type, resolve_sound_file,
                                            sound_catalog, spec_of,
                                            total_span, whoosh_audio)


def _tr(ttype="fade_black", text="一天后", **kw):
    t = {"type": ttype, "text": text, **kw}
    return {"id": 9, "kind": "transition", "dur": 1.6, "narration": "", "transition": t}


def _shot(i):
    return {"id": i, "narration": "台词", "dur": 4}


class TestCatalog(unittest.TestCase):
    """转场目录（Studio 类型选择器真源）：键与 TRANSITIONS 锁步、方向/主色/音效元数据齐全。"""

    def test_catalog_covers_types_excl_clip(self):
        keys = [t["key"] for t in catalog()]
        self.assertEqual(keys, [k for k in TRANSITIONS if k != "clip"])   # clip 需素材，缺省不入选择器
        self.assertIn("clip", [t["key"] for t in catalog(include_clip=True)])

    def test_catalog_entry_shape(self):
        for t in catalog():
            self.assertTrue(t["key"] and t["label"] and t["desc"])
            self.assertIn(t["family"], ("card", "xfade"))
            self.assertIn(t["text_role"], ("card", "overlay"))
            for opt in (*t["directions"], *t["colors"]):     # 选项都是 {value,label}
                self.assertTrue(opt["value"] and opt["label"])

    def test_directional_types_carry_options(self):
        by = {t["key"]: t for t in catalog()}
        self.assertEqual([o["value"] for o in by["wipe"]["directions"]], ["tl", "tr", "bl", "br"])
        self.assertEqual([o["value"] for o in by["slide"]["directions"]], ["left", "right", "up", "down"])
        self.assertEqual([o["value"] for o in by["scan"]["colors"]], ["green", "blue"])
        self.assertFalse(by["circle"]["directions"])          # 圆开合无方向
        self.assertFalse(by["fade"]["colors"])                # 极简黑场无主色
        self.assertFalse(by["wipe"]["colors"])                # 对角翻页单段直切无色卡→无主色

    def test_wipe_is_diagonal_single_phase(self):
        # 对角翻页用 diag*（真·对角直线掀页）——防回归到 wipe*/两段黑卡（中途全黑冒黑矩形）
        from kinema.pipeline.transitions import _WIPE_DIR
        self.assertTrue(all(v.startswith("diag") for v in _WIPE_DIR.values()), _WIPE_DIR)

    def test_scan_colors_are_rgb_multiply(self):
        # 轮廓扫描用 RGB 乘色（灰度边缘×霓虹色，黑底乘 0 保持纯黑）——防回归 lutyuv 定值染全帧
        from kinema.pipeline.transitions import _SCAN_COLORS
        for neon, mult in _SCAN_COLORS.values():
            self.assertIn("val", mult)          # 乘色系数含亮度变量 val
            self.assertNotIn("u=", mult)        # 不是 lutyuv 的定值色度

    def test_text_ok_only_on_card_types(self):
        # 能加字仅字卡型（有停顿显字）——前端据此显隐文案框；xfade 一次性/极简黑场太短皆不可
        by = {t["key"]: t for t in catalog()}
        self.assertTrue(by["fade_black"]["text_ok"])
        self.assertTrue(by["fade_white"]["text_ok"])
        self.assertFalse(by["fade"]["text_ok"])
        for k in ("seamless", "wipe", "circle", "slide", "blur", "scan"):
            self.assertFalse(by[k]["text_ok"], k)

    def test_sound_catalog_matches_valid(self):
        vals = [s["value"] for s in sound_catalog()]
        self.assertEqual(vals[:3], ["whoosh", "riser", "boom"])   # 三色板在前
        self.assertEqual(vals[-1], "off")                         # 静音殿后
        for s in sound_catalog():
            self.assertTrue(s["value"] and s["label"])


class TestSeamless(unittest.TestCase):
    """无缝转场（soft cut）四条不变量：任一回归都会静默毁掉「看上去就是直接切换」。

    ① 注册表第一行 → 弹层**预选**（`cat[0].key` 是前端接口约定）——预选只决定
       按下「插入转场」时插的是哪一型，与「本章有没有转场」无关（缺省一个都没有，
       见 `TestNoImplicitTransitions`）；
    ② 缺省静音（几帧的柔切上挂一声「呼」是事故，音效只在用户显式选择时有）；
    ③ edge=0 且总时长=停顿本身（不动相邻镜、不失效片段缓存）；
    ④ 短过渡走均匀帧阶梯 + 帧吸附（音画等长，concat 不引入漂移）——**只在短路径上**，
       既有八型逐字节不变，见 `test_long_xfades_untouched`。"""

    def test_seamless_is_first_and_silent(self):
        self.assertEqual(catalog()[0]["key"], "seamless", "注册表第一行=弹层预选项")
        base = TRANSITIONS["seamless"]
        self.assertEqual(base["sound"], "off")
        self.assertEqual(base["edge"], 0.0)
        self.assertEqual(base["dur"], 0.1)
        shot = {"kind": "transition", "dur": 0.1, "transition": {"type": "seamless"}}
        self.assertAlmostEqual(total_span(shot), 0.1)
        self.assertEqual(spec_of(shot)["sound"], "off", "不显式选音效就必须是静音")

    def test_duration_tiers_registry(self):
        # 柔度档走通用注册表（与方向/主色同构）：seamless 三档，其余类型不登记即无此行
        by = {t["key"]: t for t in catalog()}
        tiers = by["seamless"]["durations"]
        self.assertEqual([d["value"] for d in tiers], [0.07, 0.1, 0.17])
        for d in tiers:
            self.assertTrue(d["label"])
        for k, t in by.items():
            self.assertEqual(t["dur"], TRANSITIONS[k]["dur"], k)
            if k != "seamless":
                self.assertEqual(t["durations"], [], k)

    def test_resolve_dur_clamps(self):
        from kinema.pipeline.transitions import MAX_DUR, MIN_DUR, resolve_dur
        self.assertEqual(resolve_dur("seamless"), 0.1)          # 未给 → 该型缺省
        self.assertEqual(resolve_dur("seamless", ""), 0.1)
        self.assertEqual(resolve_dur("seamless", 0.17), 0.17)
        self.assertEqual(resolve_dur("seamless", 0.001), MIN_DUR)
        self.assertEqual(resolve_dur("seamless", 999), MAX_DUR)
        self.assertEqual(resolve_dur("seamless", "abc"), 0.1)   # 错字回落，不炸
        self.assertEqual(resolve_dur("fade_black", None), 0.5)

    def test_frame_aligned_snaps_to_frame_grid(self):
        # -t 对视频按帧保留、对音频按秒截断——时长不在帧网格上时段内音画不等长，
        # concat 逐段累积成漂移。柔度档 0.07/0.17 在 30fps 下都不是整帧，必须吸附。
        from kinema.pipeline.transitions import frame_aligned
        self.assertAlmostEqual(frame_aligned(0.07, 30), 2 / 30)
        self.assertAlmostEqual(frame_aligned(0.1, 30), 3 / 30)
        self.assertAlmostEqual(frame_aligned(0.17, 30), 5 / 30)
        self.assertAlmostEqual(frame_aligned(0.1, 24), 2 / 24)   # 24fps 下 0.1s 也不是整帧
        self.assertAlmostEqual(frame_aligned(0.001, 30), 1 / 30)  # 至少 1 帧

    def _render_args(self, ttype: str, dur: float, fps: int = 30) -> list:
        """拦截 ffmpeg：抽帧与渲染都不真跑，只捕获 render_xfade_card 拼出的参数。"""
        from unittest import mock

        from kinema.pipeline import transitions as tr
        seen = {}
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.mp4"
            with mock.patch.object(tr, "last_frame"), \
                    mock.patch.object(tr, "first_frame"), \
                    mock.patch.object(tr, "run", lambda args, **kw: seen.update(a=args)):
                tr.render_xfade_card(p, p, Path(td) / "out.mp4",
                                     spec=spec_of({"kind": "transition",
                                                   "transition": {"type": ttype}}),
                                     dur=dur, width=1920, height=1080, fps=fps,
                                     with_audio=True)
        return seen["a"]

    def test_softcut_stair_filtergraph(self):
        # 均匀帧阶梯：duration=(帧数+1)/fps、offset=0、select 丢第 0 帧——零重复帧。
        # 回归到通用公式（duration=dur*0.9）= 切点上多顿一帧，正是用户抱怨的"僵硬"。
        args = self._render_args("seamless", 0.1)
        fc = args[args.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade:duration=0.1333:offset=0", fc)
        self.assertIn(r"select='gte(n\,1)'", fc)
        self.assertIn("setpts=N/FRAME_RATE/TB", fc)
        self.assertEqual(args[2:4], ["-t", "0.167"],
                         "前镜帧输入必须盖满 xfade 窗(win+1/fps)，否则阶梯被截断")
        self.assertIn("anullsrc=r=44100:cl=stereo:d=0.100", " ".join(args))
        self.assertIn("-t 0.100 -r 30", " ".join(args), "输出 -t 必须帧吸附后与音频等长")

    def test_softcut_tiers_stay_frame_aligned(self):
        # 利落档 0.07 @30fps=2.1 帧 → 吸附 2 帧（0.067s），音画同长
        args = " ".join(self._render_args("seamless", 0.07))
        self.assertIn("anullsrc=r=44100:cl=stereo:d=0.067", args)
        self.assertIn("duration=0.1000:offset=0", args)          # (2+1)/30

    def test_long_xfades_untouched(self):
        """**既有八型逐字节不变**——这是「只加一个类型、不动其余任何逻辑」的判据。

        短路径按帧数分界（≤SHORT_MAX_FRAMES）而非类型名，而既有八型的缺省段长是
        15~27 帧 @30fps，全部落在线上方：通用公式（duration=dur*0.9 / offset=dur*0.05）、
        不吸附帧、不出现 select。加新类型时顺手把帧吸附铺到全体，等于悄悄改了每一种
        既有转场的成片输出——用户点名不许。"""
        args = " ".join(self._render_args("wipe", 0.7))
        self.assertIn("duration=0.630:offset=0.035", args)
        self.assertNotIn("select=", args)
        for key, base in TRANSITIONS.items():
            if base.get("family") == "xfade" and key != "seamless":
                self.assertGreater(round(base["dur"] * 30), 8,
                                   f"{key} 缺省段长掉进短路径=既有类型输出被改")

    def test_card_family_is_not_frame_aligned(self):
        """字卡族（fade/fade_black/fade_white/clip）渲染不做帧吸附——同上，
        既有类型的段长一帧都不许动。"""
        import inspect

        from kinema.pipeline import transitions as tr
        self.assertNotIn("frame_aligned", inspect.getsource(tr.render_card))
        self.assertNotIn("frame_aligned", inspect.getsource(tr._render_scan))

    def test_xfade_still_requires_both_neighbours(self):
        """xfade 族要拿前镜尾帧 + 后镜首帧，故仍要求前后都有普通镜；章首/章尾退化为
        字卡并告警——全体 xfade 型同一条规则，新增类型不搞特例。"""
        import inspect

        from kinema.pipeline import compose
        src = inspect.getsource(compose.build)
        self.assertIn('spec["family"] == "xfade" and prev_c and next_c', src)
        self.assertIn("需要前后都有普通镜", src)

    def test_add_paths_share_resolve_dur(self):
        # 两条写路径（Studio actions / CLI）统一走 resolve_dur——钳制缺一条就是后门
        import inspect

        from kinema import cli
        from kinema.studio import actions
        self.assertIn("tr.resolve_dur(kind, dur)",
                      inspect.getsource(actions.transition_add))
        self.assertIn("transitions_mod.resolve_dur(ttype, args.dur)",
                      inspect.getsource(cli.cmd_transition))


class TestStudioAddParams(unittest.TestCase):
    """Studio 加转场端点参数透传：type/direction/color/sound 写入 spec 并被 spec_of 归一化。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv(); self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.wsp = str(Path(self.tmp.name) / "ws")
        self.ws = Workspace.open(self.wsp)
        self.s = self.ws.create_project("T", profile="narration")
        self.s.create_chapter("c1")
        ch = self.ws.store.load_chapter(self.s.pid, "ch01")
        ch["shots"] = [{"id": 1, "kind": "shot", "narration": "a"},
                       {"id": 2, "kind": "shot", "narration": "b"}]
        self.ws.store.save_chapter(self.s.pid, "ch01", ch)

    def tearDown(self):
        self.tmp.cleanup(); self.env.restore()

    def test_type_direction_color_sound_written(self):
        from kinema.studio import actions
        r = actions.transition_add(self.wsp, self.s.pid, "ch01", after=1, ttype="wipe",
                                   text="", direction="tr", color="white", sound="swish")
        self.assertEqual(r["spec"], {"type": "wipe", "direction": "tr",
                                     "color": "white", "sound": "swish"})
        # spec_of 归一化：direction 保留；wipe 无色卡故 color 不落 bg（成对语义守卫）
        sp = spec_of({"transition": r["spec"]})
        self.assertEqual(sp["bg"], "black")
        self.assertEqual(sp["direction"], "tr")

    def test_color_semantics_per_family(self):
        """`--color` 的三种归宿必须各归各位：字卡族=底/字**成对**换档（只覆盖 bg
        的结局是 fade_black --color white 渲出纯白底纯白字）；scan=独立霓虹字段
        （挤占 bg 的话尾帧槽位退化字卡时会渲出整屏纯蓝）；wipe=不消费。"""
        sp = spec_of({"transition": {"type": "fade_black", "color": "white"}})
        self.assertEqual((sp["bg"], sp["fg"]), ("white", "0x30343b"))
        sp2 = spec_of({"transition": {"type": "fade_black", "color": "black"}})
        self.assertEqual((sp2["bg"], sp2["fg"]), ("black", "white"))
        sp3 = spec_of({"transition": {"type": "scan", "color": "blue"}})
        self.assertEqual(sp3["neon"], "blue")
        self.assertEqual((sp3["bg"], sp3["fg"]), ("black", "white"),
                         "scan 的霓虹色绝不挤占底/字色槽")

    def test_type_only_no_text_ok(self):
        from kinema.studio import actions
        r = actions.transition_add(self.wsp, self.s.pid, "ch01", after=1, ttype="circle")
        self.assertEqual(r["spec"], {"type": "circle"})       # 纯类型无文案

    def test_blank_text_not_persisted(self):
        from kinema.studio import actions
        r = actions.transition_add(self.wsp, self.s.pid, "ch01", after=1,
                                   ttype="fade", text="   ")
        self.assertNotIn("text", r["spec"])                   # 空白文案不写入


class TestSpec(unittest.TestCase):
    def test_registry_and_defaults(self):
        self.assertEqual(DEFAULT_TYPE, "fade_black")
        for t, cfg in TRANSITIONS.items():
            self.assertIn("bg", cfg)
            self.assertIn("edge", cfg)
            self.assertGreater(cfg["dur"], 0)

    def test_pick_type_smart_defaults(self):
        # 缺省智能选：无字=fade 极简｜有字=fade_black｜素材=clip｜显式指定优先
        self.assertEqual(pick_type(), "fade")
        self.assertEqual(pick_type(""), "fade")
        self.assertEqual(pick_type("几天后"), "fade_black")
        self.assertEqual(pick_type(None, "assets/transitions/ink.mp4"), "clip")
        self.assertEqual(pick_type("有字", None, "fade_white"), "fade_white")

    def test_xfade_family_zero_edge(self):
        # 冻结帧 xfade 族：画面像素与切点连续，不动相邻镜（edge=0）
        for t in ("seamless", "wipe", "circle", "slide", "blur"):
            self.assertEqual(TRANSITIONS[t].get("family"), "xfade", t)
            self.assertEqual(TRANSITIONS[t]["edge"], 0.0, t)
        # wipe 单段直切没有色卡：color 不消费、bg 保持注册表缺省（覆盖了也没人读，
        # 「看起来可配实际无效」比不提供更糟）
        sp = spec_of({"kind": "transition",
                      "transition": {"type": "wipe", "direction": "bl",
                                     "color": "white", "sound": "off"}})
        self.assertEqual((sp["family"], sp["direction"], sp["bg"], sp["sound"]),
                         ("xfade", "bl", "black", "off"))
        self.assertEqual(spec_of(_tr())["sound"], "whoosh")   # 音效缺省开

    def test_whoosh_chain(self):
        # 「呼」短音效：棕噪+低通+快起缓落包络（纯 ffmpeg 合成零素材）
        src, filt = whoosh_audio(0.7)
        self.assertIn("anoisesrc=color=brown", src)
        self.assertIn("lowpass", filt)
        self.assertIn("afade=t=in", filt)
        self.assertIn("afade=t=out", filt)
        self.assertIn("d=0.300", whoosh_audio(0.05)[0])       # 时长下限钳制

    def test_sound_palette(self):
        # riser=whoosh 倒放（上升蓄势）；boom=54Hz 低频正弦骤起缓衰
        _, riser = whoosh_audio(0.7, kind="riser")
        self.assertIn("areverse", riser)
        boom_src, boom = whoosh_audio(0.7, kind="boom")
        self.assertIn("sine=frequency=54", boom_src)
        self.assertIn("aecho", boom)
        self.assertNotIn("areverse", whoosh_audio(0.7)[1])    # whoosh 不倒放
        # 非法 sound 值回落 whoosh；注册表扩展键（纯外置）合法
        sp = spec_of(_tr(sound="爆炸"))
        self.assertEqual(sp["sound"], "whoosh")
        self.assertEqual(spec_of(_tr(sound="boom"))["sound"], "boom")
        self.assertEqual(spec_of(_tr(sound="glitch"))["sound"], "glitch")
        # fade_white 缺省配 shimmer（回忆/梦境的风铃感；缺文件回落 whoosh 合成）
        self.assertEqual(spec_of(_tr(ttype="fade_white"))["sound"], "shimmer")

    def test_builtin_total_spans(self):
        # 用户口径总时长：fade≈0.5s（0.2+0.1+0.2）、fade_black≈1s（0.25+0.5+0.25）
        fade = {"kind": "transition", "dur": default_dur("fade"),
                "transition": {"type": "fade"}}
        black = {"kind": "transition", "dur": default_dur("fade_black"),
                 "transition": {"type": "fade_black", "text": "几天后"}}
        self.assertAlmostEqual(total_span(fade), 0.5)
        self.assertAlmostEqual(total_span(black), 1.0)

    def test_is_transition(self):
        self.assertTrue(is_transition(_tr()))
        self.assertFalse(is_transition(_shot(1)))
        self.assertFalse(is_transition({}))

    def test_unknown_type_is_an_error(self):
        # 写入口只允许目录内的类型；手改出来的错字回落成黑场字卡是静默改片
        from kinema.errors import ProjectError
        with self.assertRaisesRegex(ProjectError, "whoosh_3d"):
            spec_of(_tr(ttype="whoosh_3d"))

    def test_asset_implies_clip(self):
        sp = spec_of({"kind": "transition",
                      "transition": {"asset": "assets/transitions/ink.mp4"}})
        self.assertEqual(sp["type"], "clip")

    def test_edge_overridable(self):
        self.assertAlmostEqual(spec_of(_tr(edge=1.2))["edge"], 1.2)
        self.assertAlmostEqual(spec_of(_tr())["edge"],
                               TRANSITIONS["fade_black"]["edge"])


class TestEdgeFades(unittest.TestCase):
    def test_neighbors_fade_into_card_color(self):
        shots = [_shot(1), _tr(), _shot(2)]
        fi, fic, fo, foc = edge_fades(shots, 0)      # 前镜：尾部淡出到黑
        self.assertEqual((fi, fo, foc), (0.0, 0.25, "black"))
        fi, fic, fo, foc = edge_fades(shots, 2)      # 后镜：头部从黑淡入
        self.assertEqual((fi, fic, fo), (0.25, "black", 0.0))

    def test_minimal_fade_edges(self):
        # 极简 fade：0.2s 淡出 + 0.1s 黑场 + 0.2s 淡入 = 0.5s 呼吸
        shots = [_shot(1), _tr(ttype="fade", text=""), _shot(2)]
        self.assertEqual(edge_fades(shots, 0)[2], 0.2)
        self.assertEqual(edge_fades(shots, 2)[0], 0.2)

    def test_card_itself_no_edge_fade(self):
        shots = [_shot(1), _tr(), _shot(2)]
        self.assertEqual(edge_fades(shots, 1), (0.0, "black", 0.0, "black"))

    def test_white_transition_fades_white(self):
        shots = [_shot(1), _tr(ttype="fade_white"), _shot(2)]
        self.assertEqual(edge_fades(shots, 0)[3], "white")
        self.assertEqual(edge_fades(shots, 2)[1], "white")

    def test_plain_junction_no_fade(self):
        shots = [_shot(1), _shot(2)]
        self.assertEqual(edge_fades(shots, 0), (0.0, "black", 0.0, "black"))
        self.assertEqual(edge_fades(shots, 1), (0.0, "black", 0.0, "black"))

    def test_sandwiched_shot_fades_both_sides(self):
        shots = [_tr(), _shot(5), _tr(ttype="fade_white")]
        fi, fic, fo, foc = edge_fades(shots, 1)
        self.assertEqual((fic, foc), ("black", "white"))
        self.assertGreater(fi, 0)
        self.assertGreater(fo, 0)


class TestKenburnsMotion(unittest.TestCase):
    """八种缓动运镜（去"静态图滑动"感）：全部 smoothstep 缓入缓出、互不重复。"""

    def test_eight_distinct_eased_styles(self):
        self.assertEqual(STYLES, 8)
        seen = set()
        for k in range(STYLES):
            fx = _effect(k, 60)
            self.assertTrue({"z", "x", "y"} <= fx.keys())
            seen.add((fx["z"], fx["x"], fx["y"]))
        self.assertEqual(len(seen), 8)                 # 互不相同（跨镜节奏变化）
        self.assertIn("(3-2*", _effect(0, 60)["z"])    # smoothstep 缓动（非线性匀速）
        self.assertIn("rot", _effect(6, 60))           # 微旋推近带 rotate 表达式
        self.assertIn("sin(PI*", _effect(7, 60)["z"])  # 呼吸镜正弦微变焦

    def test_diagonal_moves_both_axes(self):
        fx = _effect(4, 60)                            # 对角推近：x/y 同时随缓动走
        self.assertNotIn("iw/2", fx["x"])
        self.assertNotIn("ih/2", fx["y"])

    def test_style_follows_camera_semantics(self):
        # 分镜 camera 语义驱动运镜（智能）；无语义回落镜号轮换（不千篇一律）
        from kinema.pipeline.kenburns import style_for
        self.assertEqual(style_for("缓慢推近", 5), 0)
        self.assertEqual(style_for("拉远揭示全景", 0), 2)
        self.assertEqual(style_for("slow dolly out", 0), 2)
        self.assertEqual(style_for("向左缓移", 0), 3)
        self.assertEqual(style_for("对角斜移入场", 0), 4)
        self.assertEqual(style_for("微旋环绕", 0), 6)
        self.assertEqual(style_for("凝视呼吸感", 0), 7)
        self.assertEqual(style_for(None, 5), 5)          # 回落轮换
        self.assertEqual(style_for("固定机位", 9), 1)    # 不匹配 → 9%8=1


class TestSfxLibrary(unittest.TestCase):
    """音效三级解析：B 外置文件（注册且存在）→ A 合成兜底（返回 None 走 whoosh_audio）。
    注册表 config/audio.yaml 与内嵌表的一致性由 test_config_drift 守卫。"""

    def tearDown(self):
        os.environ.pop("KINEMA_MUSIC_DIR", None)

    def test_missing_file_falls_back_to_synth(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["KINEMA_MUSIC_DIR"] = td    # 空库 → 全部回落合成
            self.assertIsNone(resolve_sound_file("whoosh"))
            self.assertIsNone(resolve_sound_file("boom"))

    def test_existing_file_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sfx" / "transitions"
            p.mkdir(parents=True)
            (p / "boom.wav").write_bytes(b"RIFF0000WAVE")
            os.environ["KINEMA_MUSIC_DIR"] = td    # 库根整体改址（bgm/sfx 同根）
            got = resolve_sound_file("boom")
            self.assertIsNotNone(got)
            self.assertTrue(str(got).endswith("boom.wav"))
            self.assertIsNone(resolve_sound_file("whoosh"))   # 同库缺的键仍兜底

    def test_unknown_kind_is_none(self):
        self.assertIsNone(resolve_sound_file("laser"))        # 注册表没有的键

    def test_fit_chain_clamps_and_fades(self):
        f = fit_sound_filter(0.9)                             # 0.9 + 0.4 自然尾巴
        self.assertIn("atrim=0:1.300", f)
        self.assertIn("afade=t=out", f)
        self.assertIn("channel_layouts=stereo", f)
        self.assertIn("atrim=0:2.900", fit_sound_filter(9.0))  # 跨度钳制 2.5 + 0.4

    def test_embedded_registry_covers_sound_palette(self):
        # 内嵌缺省必须覆盖合成三色板 + 纯外置扩展键（swish/deep/glitch/shimmer）
        keys = set(EMBEDDED_AUDIO["sfx"]["transitions"])
        self.assertLessEqual({"whoosh", "riser", "boom"}, keys)
        self.assertLessEqual({"swish", "deep", "glitch", "shimmer"}, keys)
        # 内容型打点音效（解说/说书/漫剧战斗/剪纸拼贴各一枚）——键在此钉死，
        # 少一个就说明 audio.yaml、EMBEDDED_AUDIO、download.py 三处又分叉了
        self.assertLessEqual({"pop", "ding", "page", "paper", "impact", "slash",
                              "heartbeat", "wind", "magic", "clock", "camera"}, keys)

    def test_every_sound_key_has_a_chinese_label(self):
        # sound_catalog 是 Studio 选择器的单一真源；漏标签会在界面上直接露出英文键名
        for item in sound_catalog():
            self.assertNotEqual(item["label"], item["value"],
                                f"音效键 {item['value']} 缺中文 label")


class TestCardFilter(unittest.TestCase):
    def test_text_breathes_in_and_out(self):
        f = build_card_filter(text="一天后", width=1080, height=1920, dur=1.6)
        self.assertIn("text='一 天 后'", f)   # 纯汉字短文案自动疏排字距
        self.assertIn("if(lt(t\\,", f)                # 前段淡入
        self.assertIn("if(gt(t\\,", f)                # 末段淡出
        self.assertIn("x=(w-text_w)/2:y=(h-text_h)/2", f)   # 屏幕正中央

    def test_soft_halo_and_letterspacing_rules(self):
        # 同色低透明描边=柔和光晕（白字白晕、深字深晕，随 fg 联动）
        f = build_card_filter(text="三年后", width=1080, height=1920, dur=2.0)
        self.assertIn("bordercolor=white@0.18", f)
        f2 = build_card_filter(text="三年后", width=1080, height=1920, dur=2.0,
                               fg="0x30343b")
        self.assertIn("bordercolor=0x30343b@0.18", f2)
        # 混排（数字/标点/西文）不疏排——细空格只给纯汉字短文案
        f3 = build_card_filter(text="3年后", width=1080, height=1920, dur=2.0)
        self.assertIn("text='3年后'", f3)
        self.assertNotIn(" ", f3)

    def test_empty_text_is_plain_pause(self):
        # 无字转场（纯色停顿）也是合法用法——返回 null 滤镜而非报错
        self.assertEqual(build_card_filter(text="", width=1080, height=1920,
                                           dur=1.0), "null")

    def test_deterministic_same_params(self):
        kw = dict(text="三年后", width=1080, height=1920, dur=2.0)
        self.assertEqual(build_card_filter(**kw), build_card_filter(**kw))

    def test_specials_escaped(self):
        f = build_card_filter(text="10% 之后:再见", width=1080, height=1920, dur=1.5)
        from kinema.ffmpeg import drawtext_text
        self.assertIn(f"text={drawtext_text('10% 之后:再见')}", f)


class TestAudioLibraryLedger(unittest.TestCase):
    """曲库的**登记账**守卫。全库「CC0 · 免署名 · 可商用」这句承诺，靠的是每个文件
    都能追回来源；而这条链最容易在加曲那一刻断——往 download.py 加一行不难，
    回头补 ATTRIBUTION 才是会忘的那步。忘了不报错、不掉测试，只在有人问
    「这首哪来的、能不能商用」时变成答不上来。"""

    def _music(self) -> Path:
        import kinema
        return Path(kinema.__file__).parent.parent.parent / "music"

    def _bgm_keys(self) -> list[str]:
        import re
        return re.findall(r'"(bgm/[a-z]+/[^"]+)":',
                          (self._music() / "download.py").read_text(encoding="utf-8"))

    def test_every_bgm_entry_is_registered_in_attribution(self):
        import re
        keys = self._bgm_keys()
        self.assertGreater(len(keys), 90, "曲库条目数异常，守卫可能扫错了文件")
        att = (self._music() / "ATTRIBUTION.md").read_text(encoding="utf-8")
        rows = {f"bgm/{mood}/{f.strip()}" for mood, f in
                re.findall(r"^\| ([a-z]+) \| ([^|]+?) \|", att, re.M)
                if mood in ("calm", "upbeat", "cinematic", "ambient")}
        self.assertEqual(sorted(set(keys) - rows), [], "这些曲子没在 ATTRIBUTION 登记来源")
        self.assertEqual(sorted(rows - set(keys)), [], "ATTRIBUTION 有曲子已不在 download.py")

    def test_documented_track_count_matches_reality(self):
        """曲库说明写的曲目数必须与脚本实际条目一致；用户 README 只准往下取整。

        分两档是因为两处承担的义务不同：`music/` 下那两份是**来源登记册**，
        用户拿它逐条核对「这首哪来的、能不能商用」，少一条就是查不到出处，
        故必须精确。根 README 是概览，与其余计数同制走 `N+` 约数——**但只准
        少报不准多报**：写 `200+` 而实际只有 103 首，是对许可覆盖面的虚假陈述。
        """
        n = len(self._bgm_keys())
        root = self._music().parent
        # Agent Kernel 不承载快照数字；登记册必须精确。
        for rel in ("music/ATTRIBUTION.md", "music/README.md"):
            self.assertIn(f"{n} 首", (root / rel).read_text(encoding="utf-8"), rel)
        # 根 README 的约数：解析出声称的下限，只校验它没有超过实际条目数。
        import re
        for rel, pat in (("README.zh-CN.md", r"(\d+)\+ 首 BGM"),
                         ("README.md", r"(\d+)\+ track score")):
            m = re.search(pat, (root / rel).read_text(encoding="utf-8"))
            self.assertIsNotNone(m, f"{rel} 未按 `N+` 形式声明曲目数")
            self.assertLessEqual(int(m.group(1)), n,
                                 f"{rel} 声称的曲目下限高于实际 {n} 首")

    def test_script_only_fetches_irrevocable_public_domain_hosts(self):
        """**脚本真正会去拉的 URL** 只许落在不可撤回的 CC0 / 公共领域源上。

        Mixkit、Pixabay 这类「免费可商用、但许可可撤回且条款禁止脚本批量抓」的站点，
        只能由用户在浏览器里手挑后落盘。把它们写进抓取逻辑，等于让**每一个跑这个脚本
        的用户**在下载那一刻违反对方条款——Mixkit 服务条款 9(10) 明文禁止
        "use scripts or bots to mass download Items"，9(4) 禁止以 "stock or inventory
        basis" 向第三方提供，而 bgm/ 与 sfx/ 恰是按情绪与类别编目的 inventory。

        判据落在 URL 而非全文：这两个源出现在 MANUAL_HINT 里是**对的**——
        那是打印给用户的手动下载指引，正是它们该待的地方。"""
        import re, urllib.parse
        src = (self._music() / "download.py").read_text(encoding="utf-8")
        urls = re.findall(r'"(https?://[^"]+)"', src)
        hosts = {urllib.parse.urlparse(u).netloc for u in urls}
        allowed = {"web.archive.org", "cdn.freesound.org", "bladex.cn"}
        self.assertGreater(len(urls), 20, "URL 数异常，守卫可能扫错了文件")
        self.assertEqual(hosts - allowed, set(), f"这些主机进了自动抓取逻辑：{hosts - allowed}")

    def test_attribution_tables_render_as_tables(self):
        """登记表必须是**结构完整的 markdown 表**，不能只是「一堆看起来像表格行的文本」。

        批量往表里追加行时，只要接缝处多一个空行，markdown 就当场结束表格、把后面几十行
        并成一段密密麻麻的文字——内容一个字没少，`assertIn` 一类的守卫全绿，页面却已经坏了。
        所以这里查两件结构性的事：数据行不许游离在表外，`##` 标题前必须有空行。"""
        import re
        lines = (self._music() / "ATTRIBUTION.md").read_text(encoding="utf-8").split("\n")
        inside, orphan, tight = False, [], []
        for i, l in enumerate(lines):
            if re.match(r"^\|[\s:|-]+\|$", l):
                inside = True
            elif not l.startswith("|"):
                inside = False
            elif not inside and not (i + 1 < len(lines) and lines[i + 1].startswith("|---")):
                orphan.append(i + 1)
            if l.startswith("##") and i and lines[i - 1].strip():
                tight.append(i + 1)
        self.assertEqual(orphan, [], f"这些行游离在表格之外（接缝处多了空行）：{orphan}")
        self.assertEqual(tight, [], f"这些标题前缺空行，会被并进上一段：{tight}")

    def test_manual_only_sources_are_spelled_out_for_the_user(self):
        """禁止自动抓 ≠ 不能用。两个手动源必须在收尾提示里写清楚落位规则，
        否则「想加曲子」的用户只会去改脚本——那正是我们要避免的那件事。"""
        src = (self._music() / "download.py").read_text(encoding="utf-8")
        hint = src[src.index("MANUAL_HINT = "):src.index("def _get(")]
        for host in ("pixabay.com", "mixkit.co"):
            self.assertIn(host, hint, f"{host} 未写进手动源提示")


class TestFallbackAudioPad(unittest.TestCase):
    """dubbed/native 混用形态：静图回落片段必须补静音轨（与转场卡同参），
    且音轨形态进片段缓存键——否则 concat -c copy 的流布局不一致，
    回落镜之后所有镜的音频整体前移。"""

    def test_compose_pads_fallback_clips_with_card_params(self):
        import kinema
        src = (Path(kinema.__file__).parent / "pipeline" / "compose.py") \
            .read_text(encoding="utf-8")
        pad = src.split("def _pad_silent_audio", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("anullsrc=r=44100:cl=stereo", pad, "静音轨参数须与转场卡一致")
        self.assertIn('"-c:v", "copy"', pad, "补轨只做封装级，不得重编码视频流")
        seg = src.split("def _render_seg", 1)[1].split("\n    if stale_jobs", 1)[0]
        self.assertIn('j.get("pad_audio")', seg, "回落分支缺补轨接线")
        self.assertIn('"_au"', src, "音轨形态必须进缓存键，防跨形态复用无声片段")
        self.assertLess(src.index("主音轨可用性必须在渲染任何片段"),
                        src.index("# 第一遍"),
                        "主音轨降级判定必须先于片段/转场卡渲染")


class TestNoImplicitTransitions(unittest.TestCase):
    """**没有孤岛镜就一个转场都没有**——镜与镜之间是首尾帧衔接或连贯硬切。

    转场镜的构造点全仓库只有三处，且第三处受严格约束：
      · CLI `cli.cmd_transition`（`kinema transition add`）—— 用户亲手插；
      · Studio `actions.transition_add`（弹层「插入转场」按钮）—— 用户亲手插；
      · `pipeline.framechain.sync_seams` —— **只在参考态孤岛镜两侧**、**只插
        `seamless`**、**只带 `auto="island"` 标记**（可回收，配置一撤就自己撤走）。
        它不是"自动加转场"，是"焊缝焊不上的地方补一个软切"：那两处接缝无论如何
        都是硬切，引擎只是把硬边磨掉。
    生成/合成/改编/建章其余任何一条路径都不得自己插一条。

    这条不变量本来是"显而易见"的，所以从来没人写守卫；直到有一次槽位提示词被改成
    「默认「无缝转场」：≈3 帧柔切」——一镜一个槽位，每个槽位都这么写，用户读到的就是
    「你把每个镜都加上转场了」，并据此认定引擎在自动加转场。代码其实一直是对的，
    但**没有任何东西**能证明它是对的，只能逐次人工解释。本类提供机器验证：
    出口有限且可枚举 + 措辞不许把"预选"说成"默认已加"。
    """

    def test_transition_shots_have_exactly_three_write_paths(self):
        """全引擎只有三处**构造** `kind="transition"` 的镜——多一处就是自动加转场的后门。

        判据走 AST 找字典字面量而不是 grep 文本：文档字符串里的示例 JSON、注释里的
        举例都不该算数，而按行号钉死又会被上方任何一次编辑推翻（断言随无关编辑
        失效）。
        """
        import ast

        import kinema
        pkg = Path(kinema.__file__).parent
        makers = set()
        for py in sorted(pkg.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value == "kind"
                            and isinstance(v, ast.Constant) and v.value == "transition"):
                        makers.add(str(py.relative_to(pkg)))
        self.assertEqual(
            sorted(makers), ["cli.py", "pipeline/framechain.py", "studio/actions.py"],
            "转场镜的构造点变了——应只有 CLI `transition add`、Studio "
            f"`actions.transition_add`、`framechain.sync_seams` 三处：{sorted(makers)}")

    def test_engine_side_maker_is_recyclable_seamless_only(self):
        """引擎侧那一处**只准**造带标记的无缝转场——否则它就成了通用的自动加转场后门。

        标记是可回收性的全部依据：`sync_seams` 靠它区分「自己上一轮插的」与
        「用户手写的」，前者随配置撤走、后者一个字都不碰。没有标记 = 引擎往用户的
        章节里塞了一条永远删不干净的东西。
        """
        import ast

        import kinema
        from kinema.pipeline import framechain
        src = (Path(kinema.__file__).parent / "pipeline" / "framechain.py") \
            .read_text(encoding="utf-8")
        made = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "kind"
                    for k in node.keys):
                made.append(node)
        self.assertEqual(len(made), 1, "引擎侧只该有一处构造转场镜")
        flat = ast.dump(made[0])
        self.assertIn("AUTO_TYPE", flat, "引擎侧只准造 seamless（常量引用，不许写死字面量）")
        self.assertIn("AUTO_MARK", flat, "引擎侧造的转场必须带 auto 标记才可回收")
        self.assertEqual(framechain.AUTO_TYPE, "seamless")
        self.assertEqual(framechain.AUTO_MARK, "island")

    def _chapter_js(self) -> str:
        import kinema
        return (Path(kinema.__file__).parent / "studio_app" / "app" / "chapter.js") \
            .read_text(encoding="utf-8")

    def test_removing_a_transition_puts_the_slot_back(self):
        """**删卡必须补槽**——否则那一格永久空着，再也点不出转场。

        槽位的渲染判据是「下一镜不是转场」：有转场时前一镜压根没生成过槽位。
        删除走的是原地删节点（为了不刷新不跳顶）且把 `chapSig` 对齐到最新签名
        （为了轮询不重绘）——两个优化都对，合起来却让空缺一直留到手动刷新。
        实测投诉：删完转场，红框那一片什么都没有了。
        """
        js = self._chapter_js()
        seg = js.split("/api/transition/remove", 1)[1].split("finally", 1)[0]
        self.assertIn("node.replaceWith(transitionSlot(", seg,
                      "删除转场卡后必须把「＋转场」槽位补回原位")
        self.assertIn("prev.kind !== \"transition\"", seg,
                      "只有前一镜是普通镜才补槽（转场不接转场）")

    def test_tail_slot_is_labelled_end_frame(self):
        """末镜之后那个槽位叫「＋尾帧」：同一个功能同一条接口，只是用途不同——
        后面没有下一镜可衔接，插在那儿是给全片收尾（黑场字卡打一行字）。"""
        js = self._chapter_js()
        self.assertIn('tail ? "＋ 尾帧" : "＋ 转场"', js)
        self.assertIn("i === d.shots.length - 1", js, "末位判据须由渲染处传入")
        slot = js.split("function transitionSlot", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("片尾帧", slot, "末位提示须说清这是收尾而非两镜之间过渡")
        self.assertIn("退化为字卡", slot,
                      "末位插冻结帧型会退化为字卡——提示必须实话实说，别让人插完才发现")
        # 功能不变：仍是同一个 /api/transition/add，参数一字不改
        self.assertEqual(slot.count("/api/transition/add"), 1)

    def test_slot_tip_does_not_claim_a_default_transition(self):
        """槽位提示是**插入入口**的说明，不是本章状态的陈述。

        一镜一个槽位，任何「默认…」的措辞都会被读成「每镜已经加上了」。
        """
        import kinema
        js = (Path(kinema.__file__).parent / "studio_app" / "app" / "chapter.js") \
            .read_text(encoding="utf-8")
        tip = js.split("function transitionSlot", 1)[1].split("onclick", 1)[0]
        self.assertIn("缺省没有转场", tip, "槽位必须先说清「不点就没有」")
        self.assertNotIn("默认「无缝转场」", tip,
                         "槽位提示不许宣称本章默认带某种转场——那是弹层预选，不是章节状态")


if __name__ == "__main__":
    unittest.main()
