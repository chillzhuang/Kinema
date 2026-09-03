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

"""导航外壳双形态（左侧栏 ⇄ 顶部导航）的源级守卫。

这套形态切换的全部风险都不在"能不能切"，而在**切了之后哪里悄悄错位**——
错位不报错、不掉测试，只在某个视图某个屏宽下露出来。故守卫逐条钉住那些
"改回去也照样能跑、但界面就坏了"的写法。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


def _assets() -> Path:
    import kinema
    return Path(kinema.__file__).parent / "studio_app"


def _css() -> str:
    return (_assets() / "style.css").read_text(encoding="utf-8")


def _html() -> str:
    return (_assets() / "index.html").read_text(encoding="utf-8")


def _frontend_src() -> str:
    """Studio 前端全量源码（app/ 分片 + app.js 入口）——源级守卫一律读拼接文本，
    断言不该关心代码落在哪一片。"""
    assets = _assets()
    parts = sorted((assets / "app").glob("*.js")) + [assets / "app.js"]
    return "".join(p.read_text(encoding="utf-8") for p in parts)


class TestFrontendRaceGuards(unittest.TestCase):
    """迟到写入与缓存穿透串的两条纪律（源级）。"""

    def test_bust_join_logic_single_sourced(self):
        """缓存穿透串的接符裁决只许 core.withBust 一份——本地媒体带 ?、云端直链
        （OSS）常无查询串，自拼 `&t=` 会把它变成路径一部分（OSS 404，且 BUST
        无清除，本会话内该镜缩略图与灯箱大图永久裂）。"""
        pat = 'includes("?") ? "&" : "?"'
        hits = [f.name for f in sorted((_assets() / "app").glob("*.js"))
                + [_assets() / "app.js"]
                if pat in f.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["core.js"], "接符逻辑只许 withBust 一份")
        for rel in ("app/chapter.js", "app/widgets.js", "app/shot-tools.js"):
            self.assertIn("withBust(", (_assets() / rel).read_text(encoding="utf-8"), rel)
        # `&t=` 不经 withBust 直接拼：无查询串的 OSS 地址会被拼坏
        self.assertNotIn("}&t=${", (_assets() / "app/chapter.js").read_text(encoding="utf-8"))

    def test_render_has_stale_route_guards(self):
        """慢视图过期守卫：await 回来必须核对 routeKey（core.softRefresh 早有同款
        纪律）——迟到视图不覆盖已切走的页面、迟到的 startPoll 不给旧章节装定时器。"""
        src = (_assets() / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.routeKey !== routeKey", src)
        self.assertIn("if (!gone) startPoll", src)
        chap = (_assets() / "app/chapter.js").read_text(encoding="utf-8")
        self.assertIn("stale && stale()", chap)
        st = (_assets() / "app/shot-tools.js").read_text(encoding="utf-8")
        self.assertIn('r.pid === d.project && r.cid === d.id', st,
                      "refreshAfterWrite 必须核对 pid/cid，不许只判路由名")


class TestLegalNoticeSurvivesBothShells(unittest.TestCase):
    """AGPL 第 5(d) 条的 Appropriate Legal Notices 不因换形态或窄屏而豁免。"""

    def test_legal_sign_is_never_hidden_by_the_shell(self):
        css, html = _css(), _html()
        self.assertIn('id="rail-legal"', html)
        # 页脚两行：作者签一行；出品签与许可签同一行两个落点（前者跳官网、
        # 后者开声明弹层）。AGPL 第 5(d) 条要的是一个**可点开**的显眼条目，
        # 做成静态文字不算数。
        self.assertIn('class="rail-author"', html)
        self.assertIn("POWERED BY © <b>BLADEX</b>", html)
        self.assertIn('href="https://bladex.cn"', html)
        self.assertRegex(html, r'class="rail-legal"[^>]*>AGPL V3</button>')
        # 顶栏形态把页脚收成右端一组，三个签都还在
        self.assertRegex(css, r'html\[data-shell="top"\] \.rail-foot \{')
        # 窄屏让位的是作者签与出品签；法律签一旦出现在隐藏清单里即为合规缺陷
        narrow = css[css.index("@media (max-width: 900px) {\n  /* 窄屏恒顶栏"):]
        narrow = narrow[:narrow.index("\n}")]
        self.assertIn(".rail-author", narrow)
        self.assertIn(".rail-power", narrow)
        self.assertNotIn(".rail-legal", narrow)

    def test_narrow_screens_no_longer_swallow_the_whole_nav(self):
        """旧断点把整条侧栏 display:none 且无任何替代入口，900px 以下
        五个主菜单与项目树只能靠手敲 hash 到达。窄屏恒顶栏后这条必须消失。"""
        css = _css()
        self.assertNotRegex(css, r"@media \(max-width: 900px\) \{\s*\n\s*\.rail \{ display: none; \}")


class TestShellSwitchControl(unittest.TestCase):
    """开关本身的两个易错点：重复挂载、以及切换后不通知实测布局的视图。"""

    def test_switch_container_is_static_and_mounted_once(self):
        """挂进 renderRail 会因它全站被调九次而堆出十几个重复胶囊。"""
        html, src = _html(), _frontend_src()
        self.assertIn('id="lay-seg"', html)
        body = src[src.index("function mountShellSwitch("):]
        body = body[:body.index("\n}")]
        self.assertIn("host.dataset.bound", body)

    def test_switching_notifies_layouts_that_measure_themselves(self):
        """剧本工作台的两栏高度是实测距顶算的，只在 resize 时重算；
        形态切换不触发 resize，不补这一下就会多出/少掉一条导航条的高度。"""
        src = _frontend_src()
        body = src[src.index("function applyShell("):]
        self.assertIn('window.dispatchEvent(new Event("resize"))', body[:body.index("\n}")])

    def test_narrow_screens_force_the_top_shell(self):
        src = _frontend_src()
        self.assertIn('window.matchMedia("(max-width: 900px)")', src)
        self.assertIn('NARROW.matches ? "top" : shellPref()', src)

    def test_shell_is_resolved_before_first_paint(self):
        """app.js 是 type=module 恒 defer：等它执行时页面已按侧栏画过一帧，
        顶栏用户每次刷新都会看见一次跳动。故首帧前的赋值走 index.html 的内联脚本。

        这是对「app.js 为唯一入口」的显式例外，因此把它钉小：只准读偏好与写属性。
        """
        html = _html()
        inline = re.findall(r"<script>\s*(.*?)</script>", html, re.S)
        self.assertEqual(len(inline), 1, "index.html 只该有一段内联脚本（外壳形态引导）")
        boot = inline[0]
        self.assertIn('localStorage.getItem("kn-shell")', boot)
        self.assertIn("document.documentElement.dataset.shell", boot)
        self.assertNotIn("import", boot)
        self.assertLess(len(boot), 420, "引导脚本超出受限例外的体量，逻辑应回 shell.js")

    def test_default_shell_is_top_and_agrees_across_both_resolvers(self):
        """缺省形态**在两处各算一遍**：index.html 的首帧引导脚本，与 shell.js 的
        shellPref。分叉不报错、也掉不了别的测试——表现是首帧按引导脚本画、
        shell.js 随后改回去，于是每次刷新闪一下形态，而这段引导脚本存在的
        全部意义就是消掉那一闪。

        判据统一写成「显式存了 side 才用 side」（而非「存了 top 才用 top」）：
        未表过态一律顶栏，表过态的偏好照旧从 localStorage 取回。
        """
        html, src = _html(), _frontend_src()
        boot = re.findall(r"<script>\s*(.*?)</script>", html, re.S)[0]
        self.assertRegex(boot, r'var m = "top"')
        self.assertRegex(boot, r'localStorage\.getItem\("kn-shell"\) === "side"')
        self.assertNotIn('=== "top"', boot, "引导脚本的判据反了：缺省该是顶栏")

        pref = src[src.index("const shellPref = ("):]
        pref = pref[:pref.index("\n};")]
        self.assertIn('=== "side" ? "side" : "top"', pref)
        self.assertIn('catch { return "top"; }', pref,
                      "隐私模式读不到 localStorage 时也该落到同一个缺省")


class TestEngineStaleBanner(unittest.TestCase):
    """引擎错配贴条的前端契约（服务端语义见 test_studio_routes 同名检测）。

    三件必须同时在位：顶栏静态元素、core.api 的单点消费、样式。缺元素则
    api 揭幕落空，缺消费则元素永远 hidden，缺样式则贴条渲染成裸文本——
    三者各自都不报错，合起来才是一条能被看见的提示。"""

    def test_banner_element_consumer_and_style_all_present(self):
        html = _html()
        self.assertIn('id="engine-stale"', html, "顶栏缺贴条元素")
        self.assertRegex(html, r'id="engine-stale"[^>]*\bhidden', "贴条必须默认隐藏")
        core = (_assets() / "app" / "core.js").read_text(encoding="utf-8")
        self.assertIn("j.engine_stale", core,
                      "api() 必须消费 engine_stale——所有 GET 与轮询的唯一入口，"
                      "挂在别处就只有个别页面能亮牌")
        self.assertIn("function flagEngineStale", core)
        self.assertIn("kinema studio --restart", core, "点击必须给出可执行的重启命令")
        self.assertIn(".engine-stale {", _css())


if __name__ == "__main__":
    unittest.main()
