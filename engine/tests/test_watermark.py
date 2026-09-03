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

"""动态水印单测（纯函数，零 ffmpeg 依赖）：弹性漫游仿真的物理正确性 +
filtergraph 构造。核心不变量：**连续、在场、界内**——段边界位置严格连续
（零瞬移）、alpha 仅入场淡入一次（零消失）、全程位置不出画面。"""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from kinema.ffmpeg import drawtext_text, filter_literal
from kinema.pipeline.watermark import (
    _est_text_box, build_bottom_filter, build_filter, build_fixed_filter,
    simulate_path)
from tests.support import LocalBackendEnv, fake_path


def _sim(seed=7, **kw):
    args = dict(width=1080, height=1920, tw=140, th=45, margin=20,
                duration=120.0, speed=3.0)
    args.update(kw)
    return simulate_path(random.Random(seed), **args)


def _build(**kw):
    args = dict(text="@翼宿", width=1080, height=1920, duration=19.2)
    args.update(kw)
    return build_filter(**args)


class TestSimulatePath(unittest.TestCase):
    def test_covers_full_duration_seamlessly(self):
        segs = _sim()
        self.assertAlmostEqual(segs[0][0], 0.0)
        self.assertAlmostEqual(segs[-1][1], 120.0)
        for a, b in zip(segs, segs[1:]):
            self.assertAlmostEqual(a[1], b[0])          # 时间无缝衔接

    def test_position_continuous_at_bounces(self):
        # 核心不变量：段末位置 == 下段起点（连续移动，零瞬移）
        for a, b in zip(_sim(), _sim()[1:]):
            t0, t1, x0, y0, vx, vy = a
            self.assertAlmostEqual(x0 + vx * (t1 - t0), b[2], places=6)
            self.assertAlmostEqual(y0 + vy * (t1 - t0), b[3], places=6)

    def test_positions_stay_in_bounds(self):
        xmin, xmax, ymin, ymax = 20, 1080 - 140 - 20, 20, 1920 - 45 - 20
        for t0, t1, x0, y0, vx, vy in _sim():
            for x, y in ((x0, y0), (x0 + vx * (t1 - t0), y0 + vy * (t1 - t0))):
                self.assertTrue(xmin - 0.01 <= x <= xmax + 0.01, f"x 出界: {x}")
                self.assertTrue(ymin - 0.01 <= y <= ymax + 0.01, f"y 出界: {y}")

    def test_bounce_reverses_hit_axis_with_random_speed(self):
        segs = _sim()
        self.assertGreater(len(segs), 2, "120s 应发生多次反弹")
        speeds = set()
        for a, b in zip(segs, segs[1:]):
            xmin, xmax = 20, 1080 - 140 - 20
            end_x = a[2] + a[4] * (a[1] - a[0])
            if end_x <= xmin + 0.01 or end_x >= xmax - 0.01:   # 撞左右墙
                self.assertLess(a[4] * b[4], 0, "被撞轴必须反向")
            speeds.add(round(abs(b[4]), 4))
        self.assertGreater(len(speeds), 1, "反弹后速度应随机变化（非镜面反射）")

    def test_deterministic_by_seed(self):
        self.assertEqual(_sim(seed=9), _sim(seed=9))
        self.assertNotEqual(_sim(seed=9), _sim(seed=10))


class TestBuildFilter(unittest.TestCase):
    def test_watermark_present_from_start_never_disappears(self):
        f = _build()
        # 首段从 t=0 开始；alpha 只有"入场淡入"一种表达（无淡出、无换位消失）
        self.assertIn("between(t\\,0.000\\,", f)
        n = f.count("drawtext=")
        self.assertEqual(f.count("alpha='0.30*min(1\\,t/0.60)'"), n)

    def test_segments_seamless_no_gap(self):
        import re
        f = _build(duration=120.0)
        wins = [(float(a), float(b)) for a, b in
                re.findall(r"between\(t\\,([\d.]+)\\,([\d.]+)\)", f)]
        self.assertAlmostEqual(wins[0][0], 0.0)
        self.assertAlmostEqual(wins[-1][1], 120.0, places=1)
        for (_, e), (s, _) in zip(wins, wins[1:]):
            self.assertAlmostEqual(e, s, places=3)       # 窗口无缝 → 永在场

    def test_positions_clamped_with_runtime_text_metrics(self):
        f = _build()
        n = f.count("drawtext=")
        self.assertEqual(f.count("min(w-tw-"), n)        # 估算误差由运行时钳制兜底
        self.assertEqual(f.count("min(h-th-"), n)

    def test_deterministic_default_seed(self):
        self.assertEqual(_build(), _build())
        self.assertNotEqual(_build(), _build(text="@别的频道"))

    def test_style_params(self):
        font = fake_path("f.ttc")
        f = _build(size=48, opacity=0.5, color="yellow", font=font)
        self.assertIn("fontsize=48", f)
        self.assertIn("fontcolor=yellow", f)
        self.assertIn(f"fontfile={filter_literal(font)}", f)
        self.assertIn("shadowcolor=black@0.35", f)       # 柔和投影保证亮暗场景可读
        self.assertIn("alpha='0.50*min(1\\,t/0.60)'", f)

    def test_default_size_scales_with_height(self):
        self.assertIn(f"fontsize={round(1920 * 0.025)}", _build())

    def test_special_chars_escaped_in_text(self):
        f = _build(text="100%的:爱,'真'")
        self.assertIn(f"text={drawtext_text('100%的:爱,\'真\'')}", f)

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            _build(text="  ")


def _fixed(**kw):
    args = dict(text="@翼宿", width=1920, height=1080, size=50)
    args.update(kw)
    return build_fixed_filter(**args)


class TestBuildFixedFilter(unittest.TestCase):
    """固定角标水印：单个静止 drawtext、四角定位、缺省不透明+细锐描边（高清不糊）。"""

    def test_single_static_drawtext(self):
        f = _fixed()
        self.assertEqual(f.count("drawtext="), 1)     # 固定=单个 drawtext（不像漂移分段）
        self.assertNotIn("between(t", f)              # 无 enable 时间窗 → 全程在场不闪不动

    def test_opaque_and_sharp_by_default(self):
        f = _fixed()
        self.assertNotIn("alpha=", f)                 # 缺省完全不透明（不透明模糊）
        self.assertIn("borderw=", f)                  # 细锐描边（清晰不糊，替代柔光晕）
        self.assertNotIn("shadowcolor", f)            # 无柔和投影（那是漂移水印的）

    def test_corner_br_default(self):
        f = _fixed()
        self.assertIn(":x='w-tw-", f)                 # 右
        self.assertIn(":y='h-th-", f)                 # 下

    def test_corner_tl(self):
        f = _fixed(position="tl")
        self.assertNotIn("w-tw-", f)                  # 左：裸内距、非右对齐
        self.assertNotIn("h-th-", f)                  # 上：裸内距、非下对齐

    def test_corner_tr(self):
        f = _fixed(position="tr")
        self.assertIn(":x='w-tw-", f)                 # 右
        self.assertNotIn("h-th-", f)                  # 上

    def test_corner_bl(self):
        f = _fixed(position="bl")
        self.assertNotIn("w-tw-", f)                  # 左
        self.assertIn(":y='h-th-", f)                 # 下

    def test_margin_hugs_edge(self):
        # 贴边：距屏幕边≈字号/3（1/3 字高），不再留一整个字
        f = build_fixed_filter(text="x", width=1920, height=1080, size=60)   # round(60/3)=20
        self.assertIn("x='w-tw-20'", f)
        self.assertIn("y='h-th-20'", f)

    def test_thin_outline_not_thick(self):
        # 细描边、不是粗黑框：borderw ≤ 2px（size≤60）
        import re
        m = re.search(r"borderw=(\d+)", _fixed(size=50))
        self.assertTrue(m)
        self.assertLessEqual(int(m.group(1)), 2)

    def test_illegal_position_falls_back_br(self):
        self.assertEqual(_fixed(position="middle"), _fixed(position="br"))

    def test_size_and_color(self):
        f = _fixed(size=40, color="yellow")
        self.assertIn("fontsize=40", f)
        self.assertIn("fontcolor=yellow", f)

    def test_opacity_applied_when_below_one(self):
        self.assertIn("alpha=0.80", _fixed(opacity=0.8))

    def test_font_embedded(self):
        font = fake_path("x.ttc")
        self.assertIn(f"fontfile={filter_literal(font)}", _fixed(font=font))

    def test_text_escaped(self):
        self.assertIn(f"text={drawtext_text('100%')}", _fixed(text="100%"))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            _fixed(text="  ")


def _bottom(**kw):
    args = dict(text="@翼宿", width=1920, height=1080)
    args.update(kw)
    return build_bottom_filter(**args)


class TestBuildBottomFilter(unittest.TestCase):
    """底部居中水印：钉在底部正中、离底留呼吸距、半透明、无描边无底衬（柔影保读）。
    定位与两位前辈互补——漂移管防搬运、角标管贴角署名、它管常驻署名。"""

    def test_single_static_centered(self):
        f = _bottom()
        self.assertEqual(f.count("drawtext="), 1)
        self.assertIn(":x='(w-tw)/2'", f)             # 水平居中
        self.assertIn(":y='h-th-", f)                 # 距底一段呼吸距
        self.assertNotIn("between(t", f)              # 全程在场不闪不动

    def test_translucent_no_border(self):
        f = _bottom()
        self.assertIn("alpha=0.55", f)                # 缺省半透明（高级感的来源之一）
        self.assertNotIn("borderw=", f)               # 无描边——绝不出现黑边黑框
        self.assertNotIn("box=", f)                   # 无底衬色条
        self.assertIn("shadowcolor", f)               # 只留极轻柔影保证亮底可读

    def test_sits_below_the_subtitle_band(self):
        # 横屏字幕底缘距底一个字幕字高（subtitle._default_margin_v，字号 58 → 58px）；
        # 底部水印整体必须落在那条线之下：字号+底距 < 58 才不与字幕重叠
        import re
        f = _bottom(width=1920, height=1080)
        size = int(re.search(r"fontsize=(\d+)", f).group(1))
        margin = int(re.search(r"y='h-th-(\d+)'", f).group(1))
        self.assertLess(round(size * 1.25) + margin, 58,
                        "底部水印侵入了横屏字幕底带——会与字幕重叠")

    def test_style_overrides(self):
        font = fake_path("x.ttc")
        f = _bottom(size=30, color="yellow", opacity=0.4, margin=12,
                    font=font)
        self.assertIn("fontsize=30", f)
        self.assertIn("fontcolor=yellow", f)
        self.assertIn("alpha=0.40", f)
        self.assertIn("y='h-th-12'", f)
        self.assertIn(f"fontfile={filter_literal(font)}", f)

    def test_text_escaped_and_empty_raises(self):
        self.assertIn(f"text={drawtext_text('100%')}", _bottom(text="100%"))
        with self.assertRaises(ValueError):
            _bottom(text="  ")


class TestBundledFont(unittest.TestCase):
    """工程内置免费商用字体（阿里普惠体 3.0）：水印/角标/字幕都引用它、不依赖系统字体
    （随仓库分发·跨系统一致）。守卫字库在位 + 三处默认都指向它。"""

    def test_font_files_bundled(self):
        from kinema.fonts import (FONTS_DIR, PUHUITI_MEDIUM, PUHUITI_REGULAR,
                                     bundled_path)
        self.assertTrue((FONTS_DIR / PUHUITI_REGULAR).is_file(), "缺内置普惠体 Regular")
        self.assertTrue((FONTS_DIR / PUHUITI_MEDIUM).is_file(), "缺内置普惠体 Medium")
        self.assertEqual(bundled_path(PUHUITI_REGULAR), str(FONTS_DIR / PUHUITI_REGULAR))
        self.assertIsNone(bundled_path("不存在的字体.otf"))

    def test_watermark_font_uses_bundled(self):
        # 浮动水印 + 固定角标都走内置普惠体（免费商用），不用系统字体
        from kinema.fonts import PUHUITI_REGULAR
        from kinema.pipeline.watermark import _wm_font
        self.assertTrue(str(_wm_font()).endswith(PUHUITI_REGULAR))

    def test_subtitle_default_is_bundled_family(self):
        # 字幕缺省字体 = 内置普惠体族名（libass 经 compose 的 fontsdir 加载）
        from kinema.fonts import PUHUITI_MEDIUM_FAMILY
        from kinema.pipeline.subtitle import _CAPTION_DEFAULTS
        self.assertEqual(_CAPTION_DEFAULTS["font"], PUHUITI_MEDIUM_FAMILY)

    def test_all_style_chains_prefer_bundled(self):
        # 全部字型链首位=内置免费商用字体（hei 普惠体 / song 思源宋体 / kai 文楷 / display 得意黑）
        from kinema.fonts import FONTS_DIR, resolve_font
        for style in ("hei", "song", "kai", "display"):
            self.assertTrue(str(resolve_font(style)).startswith(str(FONTS_DIR)),
                            f"{style} 链首位应为内置字体")

    def test_serif_kai_font_files_bundled(self):
        from kinema.fonts import FONTS_DIR, NOTOSERIF_SC, SMILEY, WENKAI
        for f in (NOTOSERIF_SC, WENKAI, SMILEY):
            self.assertTrue((FONTS_DIR / f).is_file(), f"缺内置字体 {f}")

    def test_subtitle_font_alias_maps_system_names(self):
        # profile 遗留系统字名（Songti SC / Kaiti）→ 内置免费商用族名（libass 经 fontsdir 命中）
        from kinema.fonts import NOTOSERIF_SC_FAMILY, WENKAI_FAMILY
        from kinema.pipeline.subtitle import _norm_font
        self.assertEqual(_norm_font("Songti SC"), NOTOSERIF_SC_FAMILY)
        self.assertEqual(_norm_font("宋体"), NOTOSERIF_SC_FAMILY)
        self.assertEqual(_norm_font("Kaiti"), WENKAI_FAMILY)
        self.assertEqual(_norm_font("Alibaba PuHuiTi 3.0 65 Medium"),
                         "Alibaba PuHuiTi 3.0 65 Medium")   # 非系统名原样返回


class TestEstTextBox(unittest.TestCase):
    def test_cjk_wider_than_ascii(self):
        w_cjk, _ = _est_text_box("翼宿翼宿", 40)
        w_ascii, _ = _est_text_box("abcd", 40)
        self.assertGreater(w_cjk, w_ascii)
        self.assertEqual(w_cjk, 160)                     # 4 × 1em × 40px

    def test_min_width_one_em(self):
        self.assertEqual(_est_text_box("a", 40)[0], 40)


class TestBrandingWatermarkSections(unittest.TestCase):
    """branding 三段水印配置的透传：loader 漏掉哪段，哪段的「project > branding」
    全局默认链（CLI 文案回落与 Studio 预填共用）就静默断掉——配置写了却永远不生效。"""

    def test_all_three_sections_pass_through(self):
        import os
        from kinema.branding import load_branding
        tmp = tempfile.TemporaryDirectory()
        cwd = os.getcwd()
        try:
            root = Path(tmp.name)
            (root / "config").mkdir()
            (root / "config" / "branding.yaml").write_text(
                "name: X\n"
                "watermark: {text: '@漂移'}\n"
                "watermark_fixed: {text: '@角标', position: tl}\n"
                "watermark_bottom: {text: '— 翼宿 —'}\n", encoding="utf-8")
            os.chdir(root)          # _find_file 从 cwd 起找，临时目录的配置先命中
            brand = load_branding()
        finally:
            os.chdir(cwd)
            tmp.cleanup()
        self.assertEqual(brand["watermark"]["text"], "@漂移")
        self.assertEqual(brand["watermark_fixed"],
                         {"text": "@角标", "position": "tl"})
        self.assertEqual(brand["watermark_bottom"], {"text": "— 翼宿 —"})


class TestWatermarkStudioAction(unittest.TestCase):
    """Studio 水印动作：scanner 状态字段 + 空提交删除 + 无成片守卫（生成路径走 ffmpeg
    子进程，由端到端覆盖，此处只测无 ffmpeg 依赖的分支）。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("WM", pid="wm", profile="hd2d")
        self.cf = self.s.create_chapter("第一集", cid="ch01")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_scanner_surfaces_default_and_state(self):
        from kinema.studio import scanner
        d = scanner.chapter_detail(self.ws.root, self.ws.store, "wm", "ch01")
        self.assertIn("watermark", d)
        self.assertFalse(d["watermark"]["active"])       # 尚无水印版
        self.assertFalse(d["watermark"]["has_output"])   # 尚无成片
        self.assertIsInstance(d["watermark"]["text"], str)   # 漂移预填默认文案（branding）
        self.assertFalse(d["watermark"]["floating_on"])  # 尚未设漂移水印
        fx = d["watermark"]["fixed"]                     # 固定角标状态块
        self.assertFalse(fx["on"])                       # 尚未设角标
        self.assertIn(fx["position"], ("tl", "tr", "bl", "br"))  # 四角缺省
        self.assertIsInstance(fx["text"], str)

    def test_scanner_surfaces_fixed_after_set(self):
        # 设了 watermark_fixed → scanner 如实下发 on/position/text
        from kinema.studio import scanner
        from kinema.project import Project
        proj = Project.load(self.cf)
        proj.data["watermark_fixed"] = {"text": "@翼宿", "position": "tl"}
        proj.save()
        d = scanner.chapter_detail(self.ws.root, self.ws.store, "wm", "ch01")
        fx = d["watermark"]["fixed"]
        self.assertTrue(fx["on"])
        self.assertEqual(fx["position"], "tl")
        self.assertEqual(fx["text"], "@翼宿")

    def test_bottom_prefill_falls_back_to_branding(self):
        # 底部水印预填与漂移/角标同链（章节 > branding）：章节没设时预填全局默认；
        # on 仍只认章节自身——预填只是显示，绝不等于已启用
        from kinema import branding
        from kinema.studio import scanner
        orig = branding.load_branding
        branding.load_branding = lambda: {**branding.DEFAULT_BRANDING,
                                          "watermark_bottom": {"text": "— 翼宿 —"}}
        try:
            d = scanner.chapter_detail(self.ws.root, self.ws.store, "wm", "ch01")
        finally:
            branding.load_branding = orig
        bt = d["watermark"]["bottom"]
        self.assertEqual(bt["text"], "— 翼宿 —")
        self.assertFalse(bt["on"])

    def test_no_output_raises(self):
        from kinema.studio import actions
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            actions.set_watermark(self.ws.root, "wm", "ch01", text="@x")

    def test_empty_text_removes_watermark(self):
        from kinema.studio import actions
        from kinema.project import Project
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        wmf = outdir / "wm_ch01_wm_16x9.mp4"
        wmf.write_bytes(b"video")
        proj = Project.load(self.cf)
        proj.data["output_wm"] = {"16:9": str(wmf)}
        proj.data["watermark"] = "@旧文案"
        proj.save()
        r = actions.set_watermark(self.ws.root, "wm", "ch01", text="")   # 空提交 = 删除
        self.assertEqual(r, {"watermarked": False, "removed": 1})
        self.assertFalse(wmf.is_file())                  # 水印版文件已删
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertIsNone(data.get("output_wm"))         # 记录已清
        self.assertIsNone(data.get("watermark"))         # 文案回落 branding 默认

    def test_fixed_no_output_raises(self):
        from kinema.studio import actions
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            actions.set_watermark(self.ws.root, "wm", "ch01",
                                  fixed_text="@翼宿", fixed_position="tl")

    def test_set_fixed_saves_field(self):
        # 有成片 → 记住 watermark_fixed{text,position} 并触发后台重烧（spawn 打桩免子进程）
        from kinema.studio import actions, jobs
        from kinema.project import Project
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        vid = outdir / "wm_ch01_16x9.mp4"
        vid.write_bytes(b"video")
        proj = Project.load(self.cf)
        proj.data["output"] = {"16:9": str(vid)}
        proj.save()
        orig = jobs.spawn_cli
        jobs.spawn_cli = lambda *a, **k: "job-x"
        try:
            r = actions.set_watermark(self.ws.root, "wm", "ch01",
                                      fixed_text="@翼宿", fixed_position="tr")
        finally:
            jobs.spawn_cli = orig
        self.assertTrue(r["watermarked"])
        self.assertTrue(r["fixed"])
        self.assertFalse(r["floating"])                  # 只开了角标、没漂移
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertEqual(data["watermark_fixed"], {"text": "@翼宿", "position": "tr"})

    def test_clear_fixed_removes_when_no_floating(self):
        # 只有固定角标、无漂移 → 清角标 = 删水印版（两类都空才删）
        from kinema.studio import actions
        from kinema.project import Project
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        wmf = outdir / "wm_ch01_wm_16x9.mp4"
        wmf.write_bytes(b"video")
        proj = Project.load(self.cf)
        proj.data["output_wm"] = {"16:9": str(wmf)}
        proj.data["watermark_fixed"] = {"text": "@翼宿", "position": "tl"}
        proj.save()
        r = actions.set_watermark(self.ws.root, "wm", "ch01", fixed_text="")
        self.assertEqual(r, {"watermarked": False, "removed": 1})
        self.assertFalse(wmf.is_file())
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertIsNone(data.get("watermark_fixed"))
        self.assertIsNone(data.get("output_wm"))

    def test_omitted_field_leaves_that_kind_untouched(self):
        """三态语义：字段缺省=不动 · ""=清除 · 非空=设置。

        合并成一个写入口后，「只改漂移」不能把角标顺手抹掉——网页每次都送两个字段，
        但 CLI/脚本可能只送一个，缺省必须是「不动」而不是「清空」。"""
        from kinema.studio import actions, jobs
        self._with_output(watermark_fixed={"text": "@角标", "position": "tl"})
        orig = jobs.spawn_cli
        jobs.spawn_cli = lambda *a, **k: "job-x"
        try:
            r = actions.set_watermark(self.ws.root, "wm", "ch01", text="@漂移")
        finally:
            jobs.spawn_cli = orig
        self.assertTrue(r["floating"])
        self.assertTrue(r["fixed"], "只改漂移时角标必须原样留着")
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertEqual(data["watermark"], "@漂移")
        self.assertEqual(data["watermark_fixed"], {"text": "@角标", "position": "tl"})

    def test_both_kinds_written_in_one_pass_one_job(self):
        """三类一次写完、**只起一个重烧任务**——分多次 POST 会让多个
        `watermark --from-project --force` 同时改写同一批 output_wm，
        后完成的那个以其他类的旧状态为准（"刚设的角标又没了"）。"""
        from kinema.studio import actions, jobs
        self._with_output()
        calls = []
        orig = jobs.spawn_cli
        jobs.spawn_cli = lambda *a, **k: (calls.append(a), "job-x")[1]
        try:
            r = actions.set_watermark(self.ws.root, "wm", "ch01",
                                      text="@漂移", fixed_text="@角标",
                                      fixed_position="bl", bottom_text="@底部")
        finally:
            jobs.spawn_cli = orig
        self.assertEqual(len(calls), 1, "三类水印只该起一个重烧任务")
        self.assertTrue(r["floating"] and r["fixed"] and r["bottom"])
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertEqual(data["watermark"], "@漂移")
        self.assertEqual(data["watermark_fixed"], {"text": "@角标", "position": "bl"})
        self.assertEqual(data["watermark_bottom"], {"text": "@底部"})

    def test_bottom_only_burns_and_clears(self):
        """底部水印与另两类同权：单独可烧、清空参与「全空删水印版」判定。"""
        from kinema.studio import actions, jobs
        from kinema.project import Project
        self._with_output()
        orig = jobs.spawn_cli
        jobs.spawn_cli = lambda *a, **k: "job-x"
        try:
            r = actions.set_watermark(self.ws.root, "wm", "ch01", bottom_text="@底部")
        finally:
            jobs.spawn_cli = orig
        self.assertTrue(r["watermarked"] and r["bottom"])
        self.assertFalse(r["floating"] or r["fixed"])
        # 清空 → 三类全空 = 删水印版路径（无 output_wm 时 removed=0）
        r2 = actions.set_watermark(self.ws.root, "wm", "ch01", bottom_text="")
        self.assertEqual(r2["watermarked"], False)
        self.assertIsNone(Project.load(self.cf).data.get("watermark_bottom"))

    def test_burn_false_writes_without_spawning(self):
        """`burn=False` 只写盘不烧：同一次提交还改字幕样式时，重烧归 rebuild
        单链——这里再起水印任务就是两个任务抢写同一批 output_wm。"""
        from kinema.studio import actions, jobs
        self._with_output()
        calls = []
        orig = jobs.spawn_cli
        jobs.spawn_cli = lambda *a, **k: (calls.append(a), "job-x")[1]
        try:
            r = actions.set_watermark(self.ws.root, "wm", "ch01",
                                      bottom_text="@底部", burn=False)
        finally:
            jobs.spawn_cli = orig
        self.assertEqual(calls, [], "burn=False 不许起任何重烧任务")
        self.assertEqual(r["burned"], False)
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertEqual(data["watermark_bottom"], {"text": "@底部"})

    def test_scanner_surfaces_bottom_block(self):
        from kinema.studio import scanner
        from kinema.project import Project
        proj = Project.load(self.cf)
        proj.data["watermark_bottom"] = {"text": "@底部"}
        proj.save()
        d = scanner.chapter_detail(self.ws.root, self.ws.store, "wm", "ch01")
        self.assertEqual(d["watermark"]["bottom"], {"text": "@底部", "on": True})


    def test_clearing_both_removes_the_watermarked_copy(self):
        from kinema.studio import actions
        from kinema.project import Project
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        wmf = outdir / "wm_ch01_wm_16x9.mp4"
        wmf.write_bytes(b"video")
        proj = Project.load(self.cf)
        proj.data["output_wm"] = {"16:9": str(wmf)}
        proj.data["watermark"] = "@漂移"
        proj.data["watermark_fixed"] = {"text": "@角标", "position": "br"}
        proj.save()
        r = actions.set_watermark(self.ws.root, "wm", "ch01", text="", fixed_text="")
        self.assertEqual(r, {"watermarked": False, "removed": 1})
        self.assertFalse(wmf.is_file())
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        for k in ("watermark", "watermark_fixed", "output_wm"):
            self.assertIsNone(data.get(k))

    def _with_output(self, **extra):
        from kinema.project import Project
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        vid = outdir / "wm_ch01_16x9.mp4"
        vid.write_bytes(b"video")
        proj = Project.load(self.cf)
        proj.data["output"] = {"16:9": str(vid)}
        proj.data.update(extra)
        proj.save()

    def test_rebuild_no_output_raises(self):
        # 没成片 → 拒绝重新构建（先完整合成一次）
        from kinema.studio import actions
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            actions.rebuild_final(self.ws.root, "wm", "ch01")

    def test_rebuild_assemble_only_without_watermark(self):
        # 无水印 → 只 assemble --draft（重烧字幕/特效）一步
        from kinema.studio import actions, jobs
        self._with_output()
        cap = {}
        orig = jobs.spawn_seq
        jobs.spawn_seq = lambda steps, **k: (cap.update(steps=steps) or "job-x")
        try:
            r = actions.rebuild_final(self.ws.root, "wm", "ch01")
        finally:
            jobs.spawn_seq = orig
        self.assertEqual(r["steps"], 1)
        self.assertFalse(r["rewatermark"])
        self.assertIn("assemble", cap["steps"][0])
        self.assertIn("--draft", cap["steps"][0])

    def test_rebuild_chains_watermark_when_set(self):
        # 有水印 → assemble 之后串一步 watermark --from-project 刷新水印版
        from kinema.studio import actions, jobs
        self._with_output(watermark_fixed={"text": "@翼宿", "position": "br"})
        cap = {}
        orig = jobs.spawn_seq
        jobs.spawn_seq = lambda steps, **k: (cap.update(steps=steps) or "job-x")
        try:
            r = actions.rebuild_final(self.ws.root, "wm", "ch01")
        finally:
            jobs.spawn_seq = orig
        self.assertEqual(r["steps"], 2)
        self.assertTrue(r["rewatermark"])
        self.assertIn("watermark", cap["steps"][1])
        self.assertIn("--from-project", cap["steps"][1])

    def test_rebuild_cleans_stale_watermark_version_when_fields_empty(self):
        # 「清空全部水印 + 改字幕样式」同次提交：水印走 burn=False 只写盘，删除
        # 水印版的收口不在那条路上——rebuild 必须补清理，否则 output_wm 残留、
        # 页面仍默认播那条旧字幕旧水印的片
        from kinema.studio import actions, jobs
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        wmf = outdir / "wm_ch01_wm_16x9.mp4"
        wmf.write_bytes(b"video")
        self._with_output(output_wm={"16:9": str(wmf)})   # 字段全空，仅残留水印版
        cap = {}
        orig = jobs.spawn_seq
        jobs.spawn_seq = lambda steps, **k: (cap.update(steps=steps) or "job-x")
        try:
            r = actions.rebuild_final(self.ws.root, "wm", "ch01")
        finally:
            jobs.spawn_seq = orig
        self.assertEqual(r["steps"], 1)                  # 无水印可刷，只 assemble
        self.assertFalse(r["rewatermark"])
        self.assertFalse(wmf.is_file())                  # 旧水印版文件已删
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertIsNone(data.get("output_wm"))         # 记录已清



class TestSubtitleStyleAction(unittest.TestCase):
    """字幕样式写入口：白名单键逐键覆盖章节 subtitle 块、None/"" 删键回落画风缺省、
    style=None 整组回落但**绝不动 lang/mode 等行为键**；scanner 下发生效值与覆盖原文。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("SUB", pid="sub", profile="hd2d")
        self.cf = self.s.create_chapter("第一集", cid="ch01")
        outdir = self.s.dir / "chapters" / "ch01_work" / "output"
        outdir.mkdir(parents=True, exist_ok=True)
        vid = outdir / "sub_ch01_16x9.mp4"
        vid.write_bytes(b"video")
        from kinema.project import Project
        proj = Project.load(self.cf)
        proj.data["output"] = {"16:9": str(vid)}
        proj.save()

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _set(self, **kw):
        from kinema.studio import actions
        return actions.set_subtitle_style(self.ws.root, "sub", "ch01", **kw)

    def test_style_keys_merge_and_clear(self):
        r = self._set(style={"size": 66, "text_color": "#ffe14d"}, rebuild=False)
        self.assertEqual(r["subtitle"], {"size": 66, "text_color": "#ffe14d"})
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertEqual(data["subtitle"]["size"], 66)
        # 逐键回落：值给 None/"" 即删那一键，其余覆盖保留
        self._set(style={"size": None}, rebuild=False)
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertNotIn("size", data["subtitle"])
        self.assertEqual(data["subtitle"]["text_color"], "#ffe14d")

    def test_reset_keeps_behaviour_keys(self):
        from kinema.project import Project
        proj = Project.load(self.cf)
        proj.data["subtitle"] = {"lang": "both", "size": 66, "outline": 2}
        proj.save()
        self._set(style=None, rebuild=False)
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        self.assertEqual(data["subtitle"], {"lang": "both"},
                         "整组回落只清样式键——lang/mode 等行为键绝不能被顺手抹掉")

    def test_unknown_key_rejected(self):
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            self._set(style={"lang": "en"}, rebuild=False)

    def test_rebuild_goes_through_the_single_chain(self):
        """rebuild=True 走 rebuild_final 单链（合成→水印按序），不另起水印任务。"""
        from kinema.studio import jobs
        seqs = []
        orig = jobs.spawn_seq
        jobs.spawn_seq = lambda steps, **k: (seqs.append(steps), "job-x")[1]
        try:
            r = self._set(style={"size": 62}, rebuild=True)
        finally:
            jobs.spawn_seq = orig
        self.assertEqual(r["job"], "job-x")
        self.assertEqual(len(seqs), 1)
        self.assertEqual(seqs[0][0][0], "assemble", "重烧字幕必须经 assemble 重合成")

    def test_scanner_surfaces_effective_and_override(self):
        # store 按 server 的口径传 ConfigStore（scanner 的 store 形参就是它，
        # 见 server.py 的 ConfigStore.shared）——生效值要合并画风样式必须有它
        from kinema.models import ConfigStore
        from kinema.studio import scanner
        self._set(style={"size": 70}, rebuild=False)
        d = scanner.chapter_detail(self.ws.root, ConfigStore.load(None), "sub", "ch01")
        ss = d["subtitle_style"]
        self.assertEqual(ss["override"], {"size": 70})
        self.assertEqual(ss["effective"]["size"], 70)
        self.assertIn("text_color", ss["effective"], "生效值必须给全样式面")

if __name__ == "__main__":
    unittest.main()
