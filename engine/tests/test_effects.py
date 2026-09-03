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

"""特效系统守卫：EFFECTS/EFFECT_META 一致、catalog 元数据、fire 火焰特效、
set_effects 写回过滤、scanner 生效特效解析。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from kinema import effects

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class TestEffectsCatalog(unittest.TestCase):
    def test_meta_covers_registry(self):
        # EFFECT_META 必须与 EFFECTS 注册表键一一对应（新增特效需同步登记元数据）
        self.assertEqual(set(effects.EFFECT_META), set(effects.EFFECTS))

    def test_catalog_shape_and_order(self):
        cat = effects.catalog()
        self.assertEqual([e["key"] for e in cat], list(effects.EFFECTS))   # 按注册顺序
        for e in cat:
            self.assertTrue(e["key"] and e["label"] and e["category"])
            self.assertIn(e["category"], effects.CATEGORY_LABELS)
            self.assertIsInstance(e["audio"], bool)

    def test_audio_effects_flagged(self):
        # 带环境音的仅天气三件套
        audio = {e["key"] for e in effects.catalog() if e["audio"]}
        self.assertEqual(audio, {"rain", "snow", "fog"})


class TestParticleEffects(unittest.TestCase):
    def test_only_two_particle_effects(self):
        # 粒子类只有星辰与萤火虫（fire/embers/dust/petals 不得入名单）
        particle = {k for k, m in effects.EFFECT_META.items() if m["category"] == "particle"}
        self.assertEqual(particle, {"sparkles", "fireflies"})
        for gone in ("fire", "embers", "dust", "petals"):
            self.assertNotIn(gone, effects.EFFECTS)
            self.assertIsNone(effects.build_plan(gone, 100, 100, 30))

    def test_sparkles_static_fireflies_float(self):
        sp = effects.build_plan("sparkles", 1920, 1080, 30)
        self.assertIn("scroll=vertical=0.0:horizontal=0.0", sp.overlay_input)  # 星辰固定不动
        ff = effects.build_plan("fireflies", 1920, 1080, 30)
        self.assertTrue(ff.overlay_input)

    def test_unknown_effect_returns_none(self):
        self.assertIsNone(effects.build_plan("nope", 100, 100, 30))


class TestCraftEffects(unittest.TestCase):
    """手作质感两件套（纸艺拼贴/kn-clay 定格可用）。"""

    def test_paper_grain_texture_is_static(self):
        # 纸纹必须静止（纸就是同一张纸）：noise 不得带 allf=t；对照组 film_grain
        # 恰恰要求 allf=t（活的胶片颗粒）——两者一静一动是各自质感的定义
        pg = effects.build_plan("paper_grain", 1920, 1080, 30)
        self.assertIn("noise=alls=", pg.overlay_input)
        self.assertNotIn("allf=t", pg.overlay_input, "纸纹逐帧刷新=沸腾的纸，必须静止")
        fg = effects.build_plan("film_grain", 1920, 1080, 30)
        self.assertIn("allf=t", fg.overlay_input, "胶片颗粒必须逐帧刷新（活颗粒）")
        self.assertEqual(pg.overlay_blend, "softlight", "纸纹只调亮度不偏色")

    def test_stopmotion_quantizes_then_restores_fps(self):
        # 定格顿挫 = fps=12 量化 + 回补容器帧率（缺回补会改变片段帧率，
        # 与 concat 的「流参数完全一致」约束冲突，字幕/时间轴也会漂）
        p = effects.build_plan("stopmotion", 1920, 1080, 30)
        self.assertEqual(p.vfilters, ["fps=12", "fps=30"])
        self.assertIsNone(p.overlay_input)
        p25 = effects.build_plan("stopmotion", 1280, 720, 25)
        self.assertEqual(p25.vfilters[-1], "fps=25", "回补帧率必须跟随容器 fps")


class TestEffectFiltergraphSafety(unittest.TestCase):
    """三条影视级铁律的回归守卫（反了分别是：全屏染色 / 满屏灰霾 / 整条渲染崩溃）。"""

    def _layers(self):
        """所有特效的图层/子图/主滤镜文本片段（供模式检查）。"""
        for name in effects.EFFECTS:
            p = effects.build_plan(name, 1920, 1080, 30)
            for chunk in (p.overlay_input, p.overlay_filter, p.subgraph,
                          *(p.vfilters or [])):
                if chunk:
                    yield name, chunk

    def test_no_time_expr_in_lut(self):
        # 铁律③：lut/lutrgb/lutyuv 在初始化时求值一次，表达式含逐帧变量 t/T/N 会让 ffmpeg
        # 直接崩溃退出（萤火虫 lutrgb=...sin(t) 整条渲染失败）。时变只能走 geq/eq/hue。
        import re
        for name, chunk in self._layers():
            for m in re.finditer(r"lut(?:rgb|yuv)?=[^,]*", chunk):
                seg = m.group(0)
                self.assertNotRegex(
                    seg, r"[^a-zA-Z](sin|cos)\s*\(",
                    f"{name}: lut 表达式不得含时变三角函数（会崩溃）：{seg}")

    def test_threshold_uses_lut_not_lutyuv_on_gray(self):
        # 铁律②：gray 平面上抠稀疏点必须 lut=y=，不能 format=gray 后接 lutyuv=y=
        #（lutyuv 在单平面 gray 上误判、把 1% 稀疏点算成满屏灰霾）。
        for name, chunk in self._layers():
            self.assertNotIn(
                "format=gray,lutyuv", chunk,
                f"{name}: 阈值化用 lut 不用 lutyuv（gray 上 lutyuv 会满屏灰）")

    def test_particle_overlays_are_light_blend(self):
        # 铁律①：发光叠加层用 screen/lighten/softlight（compose 在 RGB 空间 blend，色度安全）；
        # overlay(alpha) 仅留给需要真遮挡的物理层。此处确认粒子不是 overlay(alpha)。
        for name in ("fireflies", "sparkles"):
            p = effects.build_plan(name, 1920, 1080, 30)
            self.assertNotEqual(p.overlay_blend, "overlay",
                                f"{name}: 发光粒子层应走 RGB blend 而非 alpha overlay")

    def test_vertical_motion_directions(self):
        # 运动方向守卫（scroll 实测标定：vertical 负=下落·正=上升·0=静止）——防"雨雪往上飘"回归。
        # 下落物（雨/雪）<0；上升物（萤火）>0；星辰固定不动=0。
        import re
        expect = {"rain": "down", "snow": "down",
                  "fireflies": "up", "sparkles": "static"}
        for name, want in expect.items():
            p = effects.build_plan(name, 1920, 1080, 30)
            vs = [float(x) for x in re.findall(r"scroll=vertical=(-?[\d.]+)", p.overlay_input)]
            self.assertTrue(vs, f"{name}: 未找到 scroll vertical")
            for v in vs:
                if want == "down":
                    self.assertLess(v, 0, f"{name}: 下落物 scroll vertical 应为负（负=下落），得 {v}")
                elif want == "up":
                    self.assertGreater(v, 0, f"{name}: 上升物 scroll vertical 应为正（正=上升），得 {v}")
                else:
                    self.assertEqual(v, 0, f"{name}: 星辰应固定不动 scroll vertical=0，得 {v}")


@unittest.skipUnless(_HAS_FFMPEG, "需要系统 ffmpeg 做 filtergraph 冒烟")
class TestEffectRenderSmoke(unittest.TestCase):
    """把每个特效按 compose 的编织方式跑通一小段——直接抓 filtergraph 语法/滤镜可用性问题。"""

    def _render(self, name, w=320, h=180):
        import subprocess
        p = effects.build_plan(name, w, h, 30)
        inp = ["-f", "lavfi", "-i", f"color=0x557799:s={w}x{h}:r=30:d=0.2"]
        idx, fc, cur, vk = 1, [], "0:v", 0
        if p.vfilters:
            fc.append(f"[{cur}]" + ",".join(p.vfilters) + f"[v{vk}]"); cur = f"v{vk}"; vk += 1
        if p.subgraph:
            fc.append(p.subgraph.format(IN=cur, OUT=f"v{vk}")); cur = f"v{vk}"; vk += 1
        if p.overlay_input:
            inp += ["-f", "lavfi", "-i", p.overlay_input]; oi = idx; idx += 1
            of = p.overlay_filter or "null"
            if p.overlay_blend == "overlay":
                fc.append(f"[{oi}:v]{of},format=yuva420p[o{oi}]")
                fc.append(f"[{cur}][o{oi}]overlay[v{vk}]")
            else:
                fc.append(f"[{oi}:v]{of},format=gbrp[o{oi}]")
                fc.append(f"[{cur}]format=gbrp[b{oi}];"
                          f"[b{oi}][o{oi}]blend=all_mode={p.overlay_blend},format=yuv420p[v{vk}]")
            cur = f"v{vk}"; vk += 1
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", *inp,
               "-filter_complex", ";".join(fc), "-map", f"[{cur}]",
               "-frames:v", "2", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stderr

    def test_all_effects_render(self):
        for name in effects.EFFECTS:
            rc, err = self._render(name)
            self.assertEqual(rc, 0, f"{name} filtergraph 渲染失败：{err.strip()[:300]}")


class TestSetEffects(unittest.TestCase):
    """actions.set_effects：写章节 effects、未知名拒绝、None 删覆盖、[] 关全部。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv(); self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.wsp = str(Path(self.tmp.name) / "ws")
        self.ws = Workspace.open(self.wsp)
        self.s = self.ws.create_project("E", profile="game_sim")
        self.s.create_chapter("第一章")            # ch01

    def tearDown(self):
        self.tmp.cleanup(); self.env.restore()

    def test_unknown_names_rejected_loud(self):
        """未知特效名当场拒绝而不是静默过滤——静默过滤的终局是前端把它画成
        生效 chip、合成端却没有这层，两处同时假成功；写路径必须与合成端
        `effects_for` 同一判据。"""
        from kinema.errors import KinemaError
        from kinema.studio import actions
        with self.assertRaises(KinemaError) as cm:
            actions.set_effects(self.wsp, self.s.pid, "ch01",
                                effects=["rain", "BOGUS", "sparkles"])
        self.assertIn("BOGUS", str(cm.exception))
        self.assertNotIn("effects", self.ws.store.load_chapter(self.s.pid, "ch01"))
        r = actions.set_effects(self.wsp, self.s.pid, "ch01",
                                effects=["rain", "sparkles"])
        self.assertEqual(r["effects"], ["rain", "sparkles"])
        self.assertIsNone(r["job"])                            # 未 recompose 无 job
        self.assertEqual(self.ws.store.load_chapter(self.s.pid, "ch01")["effects"],
                         ["rain", "sparkles"])

    def test_none_removes_override(self):
        from kinema.studio import actions
        actions.set_effects(self.wsp, self.s.pid, "ch01", effects=["rain"])
        actions.set_effects(self.wsp, self.s.pid, "ch01", effects=None)
        self.assertNotIn("effects", self.ws.store.load_chapter(self.s.pid, "ch01"))

    def test_empty_disables_all(self):
        from kinema.studio import actions
        r = actions.set_effects(self.wsp, self.s.pid, "ch01", effects=[])
        self.assertEqual(r["effects"], [])


class TestEffectsResolved(unittest.TestCase):
    """scanner._effects_resolved：生效特效 = effects_for(profile, override)（与合成同源）。"""

    def test_unknown_effect_raises_at_compose_gate(self):
        """`effects_for` 是合成/animatic 的唯一收口：未知名当场报错——
        `build_plan` 返回 None 被过滤而阶段行在过滤之前就打印了「特效[...]」，
        日志与 Studio 同时报告「已应用」，实际一层都没有。"""
        from kinema.errors import ConfigError
        from kinema.models import ConfigStore
        store = ConfigStore.load()
        with self.assertRaises(ConfigError) as cm:
            store.effects_for("game_sim", ["rain", "dust"])
        self.assertIn("dust", str(cm.exception))
        self.assertEqual(store.effects_for("game_sim", ["rain"]), ["rain"])

    def test_scanner_view_degrades_instead_of_500(self):
        """展示路径不设硬闸：含未知名的存量章节页照常打开，原样下发由前端
        chip 标「未注册」——硬闸只设在花钱的合成端。"""
        from kinema.studio.scanner import _effects_resolved
        from kinema.models import ConfigStore
        store = ConfigStore.load()
        self.assertEqual(_effects_resolved(store, "game_sim", ["rain", "dust"]),
                         ["rain", "dust"])

    def test_override_replaces_default(self):
        from kinema.studio.scanner import _effects_resolved
        from kinema.models import ConfigStore
        store = ConfigStore.load()
        self.assertEqual(_effects_resolved(store, "game_sim", None),
                         store.effects_for("game_sim", None))     # 回落画风缺省
        self.assertEqual(_effects_resolved(store, "game_sim", ["rain"]), ["rain"])  # 覆盖

    def test_no_store_falls_back_to_override(self):
        from kinema.studio.scanner import _effects_resolved
        self.assertEqual(_effects_resolved(None, "game_sim", ["rain"]), ["rain"])
        self.assertEqual(_effects_resolved(None, "game_sim", None), [])


if __name__ == "__main__":
    unittest.main()


class TestNoAutoEffects(unittest.TestCase):
    """特效是显式创作决定：画风 effects 只是候选目录，章节/项目没点名就一层
    都不加——`effects_for` 不回落画风清单。"""

    def test_profile_effects_are_catalog_not_default(self):
        from kinema.models import ConfigStore
        store = ConfigStore.load()
        profiled = [p for p in (store.data.get("profiles") or {})
                    if (store.profile(p) or {}).get("effects")]
        self.assertTrue(profiled, "至少应有一个画风声明候选特效，前提不成立即本测试失效")
        for prof in profiled:
            self.assertEqual(store.effects_for(prof, None), [],
                             f"画风 {prof} 的 effects 不许自动叠加")

    def test_explicit_override_still_applies(self):
        from kinema.models import ConfigStore
        store = ConfigStore.load()
        self.assertEqual(store.effects_for("game_sim", ["rain"]), ["rain"])
