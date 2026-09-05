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

"""Studio 前端「打不开」类事故的守卫——**免构建的代价在这里补回来**。

Studio 走原生 ESM、没有打包器也没有编译期。名字写错不会有任何征兆，
一直到那行代码真的被执行：路由的 catch 把 `ReferenceError` 收成一句
「加载失败：xxx is not defined」，整个视图变成一张错误卡。

真出过一次，值得把死法写清楚：`finalCard()` 里的合成按钮从 `genBtn`
改名成 `stillsBtn`/`animateBtn`（commit be5954a），两处引用只改了一处。
漏的那处在「**已有成片**」分支上——章节没合成过就走不到，于是它在仓库里
躺了很多天，直到用户第一次合成完成片、点开章节页，页面当场白掉。
测试全绿、lint 无话可说，因为根本没有一道工序读过这一行。

这类 bug 的共性是**分支覆盖率**：前端大量代码挂在"有了某个产物才渲染"的
条件上，而这些条件恰恰只在流水线跑完之后才成立。靠人点页面去覆盖不现实，
靠"改完记得全站点一遍"更不现实。所以判据必须是源级的、静态的、每次都跑的。

两道闸（分析器在 `tests/jsscope.py`，纯 stdlib，不需要 node）：

1. **没有悬空引用**——引用的每个名字都得有出处（本文件声明 / import 进来 /
   浏览器内建）。改名重构漏改引用，这里当场红。
2. **import 图解析得通**——import 的文件真实存在，且对面真的 export 了这个名字。
   这是同一块伤口的另一半：整模块加载失败，页面同样只剩「加载失败」。

分析器刻意保守（整文件平铺、不建作用域树）：宁可漏报也不误报。误报的积累会
不断扩大白名单收录范围，一旦真 bug 被收进去，这套守卫即失效。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    from . import jsscope
except ImportError:   # 规范跑法 `discover -s tests` 把本模块当顶层导入，无父包可言
    import jsscope


def _assets() -> Path:
    import kinema
    return Path(kinema.__file__).parent / "studio_app"


class TestFrontendResolves(unittest.TestCase):
    """全部 ESM 源文件必须静态自洽——浏览器打开之前就该知道它跑不跑得起来。"""

    def test_no_dangling_identifiers(self):
        """引用的名字都有出处。

        红了怎么看：报的是「文件:行号 名字」。九成是改名/删代码漏了引用；
        若确认那是浏览器内建全局（而不是某个模块忘了 import），
        才往 `jsscope.GLOBALS` 里加——加之前请再确认一遍。
        """
        bad = {}
        for f in jsscope.frontend_files(_assets()):
            for name, line in jsscope.dangling_identifiers(f).items():
                bad[f"{f.name}:{line}"] = name
        self.assertEqual(bad, {}, "这些名字谁也没声明过，执行到就是 "
                                  f"ReferenceError、整页「加载失败」：{bad}")

    def test_import_graph_resolves(self):
        """import 的文件存在，且对面确实导出了这个名字。

        ESM 的具名 import 对不上是**加载期**错误：整个模块图挂掉，
        不是某个视图坏掉而是整个 Studio 白屏。
        """
        problems = []
        for f in jsscope.frontend_files(_assets()):
            problems += jsscope.broken_imports(f)
        self.assertEqual(problems, [], f"import 解析不通：{problems}")

    def test_every_module_is_reachable_from_the_entry(self):
        """app/ 下的每个分片都得有人 import——孤儿文件里的代码不会执行，
        对它的修改不生效且排查耗时。"""
        assets = _assets()
        imported = set()
        for f in jsscope.frontend_files(assets):
            for spec, _want, _line in jsscope.module_imports(f):
                if spec.startswith("."):
                    imported.add((f.parent / spec).resolve())
        orphans = sorted(p.name for p in (assets / "app").glob("*.js")
                         if p.resolve() not in imported)
        self.assertEqual(orphans, [], f"这些前端分片没有任何模块 import：{orphans}")


class TestBooleanAttributesAreNotFootguns(unittest.TestCase):
    """`h(tag, {disabled: false})` 必须渲出**能点的**按钮。

    HTML 布尔属性只要出现就为真——`setAttribute("disabled", false)` 写出
    `disabled="false"`，那是禁用。于是一行读起来完全正常的代码渲出一个死钮，
    而且不报任何错。

    靠每个调用点手写 `cond ? "" : null` 绕开，那种绕法迟早被忘掉，
    所以判据收进 `h()` 一处。
    """

    def test_h_skips_false_boolean_attributes(self):
        src = (_assets() / "app" / "core.js").read_text(encoding="utf-8")
        body = src.split("function h(tag, attrs", 1)[1].split("\n}", 1)[0]
        self.assertIn("BOOL_ATTRS", body, "布尔属性要单列判据，不能一路 setAttribute")
        self.assertIn('v !== false', body, "false 必须当作「不设这个属性」")
        for k in ("disabled", "hidden", "readonly", "checked"):
            self.assertIn(f'"{k}"', src, f"布尔属性表漏了 {k}")

    def test_enumerated_attributes_still_pass_false_through(self):
        # `spellcheck="false"` / `contenteditable="false"` 是**有效值**不是禁用，
        # 一刀切「false 就不设」会把它们一起吃掉
        src = (_assets() / "app" / "core.js").read_text(encoding="utf-8")
        names = src.split("BOOL_ATTRS = new Set(", 1)[1].split(")", 1)[0]
        for k in ("spellcheck", "contenteditable", "draggable"):
            self.assertNotIn(k, names, f"{k} 是枚举属性，不该进布尔属性表")


class TestBusyButtonsAlwaysComeBack(unittest.TestCase):
    """转圈的按钮必须回得来。

    `runBusy` 缺省不还原、由随后的重渲带回可用状态。可有几处调用点**刻意不重渲**
    （生成候选要停在当前页签、起草只填框），于是那些按钮永久转圈、永久点不动，
    只能刷新整页。判据因此收进 `runBusy` 一处：按钮还在文档里就还原。
    """

    def test_restore_is_decided_by_the_dom_not_by_a_flag(self):
        src = (_assets() / "app" / "components.js").read_text(encoding="utf-8")
        body = src.split("function runBusy(", 1)[1].split("\n}", 1)[0]
        self.assertIn("btn.isConnected", body, "还原判据必须是「按钮还在不在文档里」")
        self.assertNotIn("restoreOnDone", body, "开关会被漏传，漏一次就是一个死钮")

    def test_no_call_site_passes_a_restore_flag(self):
        """调用点一旦能声明还原，就一定有地方忘了声明。"""
        bad = [f.name for f in jsscope.frontend_files(_assets())
               if "restore:" in f.read_text(encoding="utf-8")]
        self.assertEqual(bad, [], f"这些文件还在给 runBusy 传还原开关：{bad}")


class TestEventTargetIsCapturedBeforeAnyAwait(unittest.TestCase):
    """`event.currentTarget` 只在事件派发期间有值：任何一个 `await` 回来它就是
    null——`runBusy(ev.currentTarget, …)` 排在 `await uiConfirm` 之后时，确认框
    点了「确认」什么都不会发生，只弹一条看不懂的 JS 报错。
    制式统一为 handler 首行 `const btn = ev.currentTarget`。"""

    def test_no_call_site_hands_current_target_to_run_busy(self):
        bad = []
        for f in jsscope.frontend_files(_assets()):
            src = f.read_text(encoding="utf-8")
            for pat in ("runBusy(ev.currentTarget", "runBusy(e.currentTarget"):
                if pat in src:
                    bad.append(f"{f.name}: {pat}")
        self.assertEqual(bad, [], f"先取 `const btn = ev.currentTarget` 再用：{bad}")


class TestRefreshAfterWriteCallShape(unittest.TestCase):
    """`refreshAfterWrite(d)` 收的是章节对象：传成 `(d.project, d.id)` 后路由校验
    `r.pid === undefined` 恒假，章节视图静默不重渲——存稿后读数不动、切路线后
    整卡停旧态，还都被 3s 轮询偶尔掩盖，是最难查的一类「有时好有时坏」。"""

    def test_no_call_site_passes_strings(self):
        pat = re.compile(r"refreshAfterWrite\(\s*\w+\.(?:project|id)\b")
        bad = [f.name for f in jsscope.frontend_files(_assets())
               if pat.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], f"refreshAfterWrite 只收章节对象：{bad}")


class TestLibraryFilterBar(unittest.TestCase):
    """片库工具条：项目维度走站内下拉。

    **一项目一枚 chip** 的铺法在项目一多时会铺满两行、得逐枚扫过去，而且与待审队列/
    看板/成本三处的项目选择器长得完全不是一个东西。判据钉「用同一个 `uiSelect`」，
    不去数 chip。

    控件初值必须回填 `galFilter`（它跨视图存活）——只画控件不回填，从别的页回来
    就是「筛选生效着、控件显示全量」，看起来像筛选坏了。
    """

    def _library(self) -> str:
        src = (_assets() / "app" / "ledger.js").read_text(encoding="utf-8")
        return src.split("async function viewLibrary(", 1)[1]

    def test_project_filter_is_the_shared_select(self):
        self.assertIn("uiSelect(", self._library(),
                      "项目维度与待审队列/看板/成本同一个选择器")

    def test_controls_start_from_the_persisted_filter(self):
        body = self._library()
        self.assertIn("value: galFilter.kw", body, "检索框初值要回填")
        self.assertIn("value: galFilter.project", body, "项目下拉初值要回填")

    def test_bar_chrome_has_styles(self):
        """下拉不定宽会被 flex 拉走（`.us` 本体是 width:100%），检索框不加宽
        就还是那条 260px 的短框——两条都只在样式表里，DOM 侧看不出来。"""
        css = (_assets() / "style.css").read_text(encoding="utf-8")
        for sel in (".lib-bar .us", ".lib-bar .fsearch"):
            self.assertIn(sel, css, f"{sel} 只有 DOM 没有样式")

    def test_search_box_metrics_are_shared_with_the_queue_and_board(self):
        """片库 / 待审队列 / 看板搜的是同量级的东西，框就该一样长。分两条写
        必然只改一处——三个页面的同一个框长短不一，看得见却查不出为什么。"""
        css = (_assets() / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.lib-bar \.fsearch,\s*\.kb-toolbar \.fsearch",
                         "两处度量要合成一条选择器，别各写各的")


class TestNoPhantomCssVariables(unittest.TestCase):
    """`var(--x)` 引到的自定义属性必须真有人定义。

    没定义、又没写兜底值时，整条声明作废：不报错、不回退到上一条规则，什么都不发生。
    `.cvc-row:hover { background: var(--panel-2) }` 就是这样一条不存在的悬停底色——
    样式表里读起来完全正常，浏览器里那一列行没有任何悬停反馈。同一个假 token 还能
    静默扩散：`--dim` 一度在两张卡的十三处文字色上都是这个结果。

    只查没有兜底的那种：`var(--x, …)` 写了兜底就是有意的可选量。运行时由 JS 写上去的
    （`--brand` / `--sheet-ar` 之类）不算未定义。
    """

    NAME = r"(--[a-z0-9-]+)"

    def test_every_variable_used_without_a_fallback_is_defined(self):
        app = _assets()
        css = "\n".join(f.read_text(encoding="utf-8")
                        for f in (app / "style.css", app / "index.html"))
        js = "\n".join(f.read_text(encoding="utf-8")
                       for f in sorted((app / "app").glob("*.js"))
                       + sorted((app / "director").glob("*.js")) + [app / "app.js"])
        defined = set(re.findall(self.NAME + r"\s*:", css))
        runtime = set(re.findall(self.NAME + r"\s*:", js)) \
            | set(re.findall(r'setProperty\(\s*"' + self.NAME + '"', js))
        used = set(re.findall(r"var\(\s*" + self.NAME + r"\s*\)", css))
        self.assertEqual(sorted(used - defined - runtime), [],
                         "这些自定义属性没人定义，用到它们的声明整条作废")


class TestModalScrimStaysFrosted(unittest.TestCase):
    """模态遮罩只有一档，且必须是毛玻璃而不是黑板。

    `backdrop-filter` 与不透明度是一件事的两半：底色一过 .8，糊出来的毛玻璃就完全
    看不出来，模态浮在自己的场景之上这件事也随之消失——放映厅、图片灯箱、命令面板
    曾一起挂在 `.9 + blur(10px)` 上，读起来就是三块纯黑板，而 CSS 里写着 blur。

    值收在 `--scrim` / `--scrim-blur` 上：上一轮有人发现版本谱系被糊得看不清，
    办法是给它单写一条更轻的覆盖——症状按住了，另外三处照旧。
    """

    # 模态遮罩层在本仓库一律叫 `*-backdrop` / `*-overlay`；图上压字的渐变（`-scrim`）
    # 是另一回事，不在此列
    SCRIM_SELECTOR = re.compile(r"(?:^|[\s,])[.#][\w-]*(?:backdrop|overlay)\b")

    @staticmethod
    def _rules(css: str):
        """(选择器, 声明块) 逐条。够用的粗切：本仓库样式表没有嵌套 at-rule 块。"""
        for chunk in css.split("}"):
            sel, _, body = chunk.rpartition("{")
            if sel.strip() and body.strip():
                yield sel.strip().splitlines()[-1].strip(), body

    def test_every_scrim_reads_the_shared_token(self):
        css = (_assets() / "style.css").read_text(encoding="utf-8")
        alpha = re.search(r"--scrim:\s*rgba\([^)]*?([\d.]+)\s*\)", css)
        self.assertIsNotNone(alpha, "--scrim 必须是 rgba，遮罩得透光")
        self.assertLess(float(alpha.group(1)), 0.85, "遮罩太实，毛玻璃就白糊了")
        self.assertRegex(css, r"--scrim-blur:\s*blur\(")
        bad = [sel for sel, body in self._rules(css)
               if self.SCRIM_SELECTOR.search(sel) and "background" in body
               and "var(--scrim)" not in body]
        self.assertEqual(bad, [], f"遮罩底色要走 --scrim，别各写各的: {bad}")


class TestInlineEmphasisReachesTheScreen(unittest.TestCase):
    """提示条与弹层的文案是成段散文，作者写强调按 Markdown 落笔（`**…**`）。
    这些位置渲染的是纯文本，那两颗星就原样落在屏幕上。

    判据必须在渲染处：逐条文案去删星号，漏一条就是一处星号，而且下一个人写新文案
    还会再犯。给 AI 用的指令台文本是另一回事——那本来就是 Markdown，不许被这条波及。
    """

    def _app(self, name: str) -> str:
        return (_assets() / "app" / name).read_text(encoding="utf-8")

    def test_renderers_compile_emphasis(self):
        core = self._app("core.js")
        self.assertIn("function rich(text)", core)
        self.assertIn(r"split(/\*\*([^*]+)\*\*/g)", core,
                      "强调编译要认成对的 **，且捕获中间的内容")
        self.assertNotIn("innerHTML", core.split("function rich(text)", 1)[1]
                         .split("\n}", 1)[0],
                         "这些文案会拼进用户数据（音色描述/文件名），只能拼文本节点")
        tip = core.split("function tipShow(", 1)[1].split("\n}", 1)[0]
        self.assertIn("rich(", tip, "提示条正文必须过强调编译")
        dlg = self._app("components.js").split("function uiDialog(", 1)[1].split("\n}", 1)[0]
        self.assertIn("rich(message)", dlg, "弹层正文必须过强调编译")

    def test_no_element_is_handed_raw_emphasis(self):
        """直接塞给 `h()` 的散文绕开了两个渲染器，必须自己过一遍编译。"""
        bad = []
        for f in jsscope.frontend_files(_assets()):
            for i, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
                if re.search(r'h\("(?:p|span|div|b|i)"', line) and "**" in line \
                        and "rich(" not in line:
                    bad.append(f"{f.name}:{i}")
        self.assertEqual(bad, [], f"这些行把带 ** 的文案直接渲成了纯文本：{bad}")


class TestVerifyStripReadsEngineConclusions(unittest.TestCase):
    """自审条只渲染引擎写好的结论。ASR 人声核对的相合判据在引擎
    （`mediacheck.VOICE_TEXT_RECALL_MIN`），逐行结论落在 `rows[].note`；
    前端再比一次阈值，引擎调阈值后页面上的「x/y 片段相符」仍按旧值数。"""

    def test_asr_rows_are_counted_by_the_engine_conclusion(self):
        from kinema.pipeline import mediacheck
        src = (_assets() / "app" / "chapter.js").read_text(encoding="utf-8")
        block = src[src.index('vo.kind === "asr"'):]
        block = block[:block.index("旁白轨落点")]
        self.assertNotIn(str(mediacheck.VOICE_TEXT_RECALL_MIN), block,
                         "阈值判据不得在前端复算一份")
        self.assertIn("!r.note", block, "相符与否读引擎写的 note")


class TestPresentationTablesMatchEngineEnums(unittest.TestCase):
    """core.js 三张静态小表与引擎真源逐项对拍（源级）。

    这些表是同步渲染的静态常量——首帧就要画徽章与中文名，等 /api/overview
    异步下发会闪空，所以准许硬编码；代价是必须与引擎枚举锁步：审阅状态少一态
    徽章直接显示裸态名，转场/渲染模式少一项中文名回落英文键。
    export.py 的 motion_zh 是同一类展示层拷贝，键集一并钉住。"""

    def _js_block(self, name: str) -> str:
        src = (_assets() / "app" / "core.js").read_text(encoding="utf-8")
        m = re.search(rf"const {name} = \{{(.*?)\}};", src, re.S)
        self.assertIsNotNone(m, f"core.js 里找不到 {name} 表")
        return m.group(1)

    def test_review_table_covers_every_engine_state(self):
        from kinema import review
        pairs = dict(re.findall(r'(\w+):\s*\{ zh: "([^"]+)"', self._js_block("REVIEW")))
        self.assertEqual(set(pairs), set(review.STATES),
                         "REVIEW 表键集与 review.STATES 分叉")
        for state, meta in review.STATES.items():
            self.assertEqual(pairs[state], meta["label"],
                             f"{state} 徽章文案与引擎 label 分叉")

    def test_motion_table_covers_every_engine_mode(self):
        # 真源是 MOTIONS 全集，不是别名表的 values——后者恰好等于全集纯属巧合，
        # 新模式不配单字母别名时按 values 对拍会反过来要求展示表删掉合法模式
        from kinema.project import MOTIONS, _MOTION_MAP
        keys = set(re.findall(r"\n  (\w+):", "\n" + self._js_block("MOTION")))
        self.assertEqual(keys, set(MOTIONS), "MOTION 表键集与渲染模式全集分叉")
        self.assertLessEqual(set(_MOTION_MAP.values()), set(MOTIONS),
                             "别名表映射到了未登记进 MOTIONS 的模式")

    def test_transition_names_match_engine_labels(self):
        from kinema.pipeline.transitions import TRANSITIONS
        pairs = dict(re.findall(r'(\w+):\s*"([^"]+)"', self._js_block("TRANSITION_ZH")))
        self.assertEqual(pairs, {k: v["label"] for k, v in TRANSITIONS.items()},
                         "TRANSITION_ZH 与引擎 TRANSITIONS label 分叉")

    def test_export_motion_names_cover_every_engine_mode(self):
        import inspect
        import kinema.export as export_mod
        from kinema.project import MOTIONS
        m = re.search(r"motion_zh = \{(.*?)\}", inspect.getsource(export_mod), re.S)
        self.assertIsNotNone(m, "export.py 里找不到 motion_zh 表")
        keys = set(re.findall(r'"(\w+)":', m.group(1)))
        self.assertEqual(keys, set(MOTIONS), "export.motion_zh 键集与渲染模式全集分叉")


class TestShotPresentationLanguage(unittest.TestCase):
    """分镜表展示层与生成契约分工明确：枚举翻译，提示词保留双语。"""

    def _src(self, name: str) -> str:
        return (_assets() / "app" / name).read_text(encoding="utf-8")

    def test_emotion_is_localized_without_rewriting_the_stored_enum(self):
        display = self._src("shot-display.js")
        chapter = self._src("chapter.js")
        self.assertIn('curious: "好奇"', display)
        self.assertIn('serious: "严肃"', display)
        self.assertIn("displayEmotion(s.emotion)", chapter)
        self.assertNotIn('displayEmotion(s.image_prompt', chapter,
                         "自由文本提示词不应走枚举翻译")

    def test_expanded_prompt_rows_remain_chinese_main_and_english_secondary(self):
        chapter = self._src("chapter.js")
        self.assertIn('promptRow("IMAGE", s.image_prompt, s.image_prompt_en)', chapter)
        self.assertIn('promptRow("MOTION", s.video_prompt, s.video_prompt_en)', chapter)
        self.assertIn('class: "p-en"', chapter)


if __name__ == "__main__":
    unittest.main()


class TestAnalyzerActuallyBites(unittest.TestCase):
    """分析器自身必须有牙：悬空引用的最小样例要报，常见合法语法一律不许误报。

    误报会逼着后来人往 `jsscope.GLOBALS` 里塞白名单，白名单一旦收进真 bug，
    上面三条守卫就等于没有；漏报则是分析器静默失效——两头都要钉住。
    """

    def _dangling(self, src: str) -> dict:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "probe.js"
            f.write_text(src, encoding="utf-8")
            return jsscope.dangling_identifiers(f)

    def test_minimal_dangling_reference_is_reported(self):
        self.assertEqual(self._dangling("export function f() { return undeclaredName; }\n"),
                         {"undeclaredName": 1})

    def test_legal_syntax_is_not_reported(self):
        cases = {
            "解构": "const obj = {a: 1, b: 2}; const {a, b} = obj; export const s = a + b;\n",
            "默认参数": "export function g(x = 1, {y = 2} = {}) { return x + y; }\n",
            "类方法": "export class K { constructor() { this.v = 1; } m() { return this.v; } static s() { return 2; } }\n",
            "可选链": "const o = {}; export const r = o?.k?.() ?? o?.[0];\n",
            "标签模板": "const html = (s, ...v) => s.join(''); const v = 1; export const t = html`<div>${v}</div>`;\n",
            "指数字面量": "export const n = 1e3 + 2.5e-2 + 0xFF;\n",
            "插值里套正则": "const s = 'aaa'; export const t = `${s.replace(/a/g, 'b')}`;\n",
            "箭头参数": "export const h = (p, q) => p + q; export const k = async ({m}) => m;\n",
            "for/catch 绑定": "export function z() { for (const it of [1]) { console.log(it); } try {} catch (err) { console.log(err); } }\n",
        }
        for label, src in cases.items():
            with self.subTest(label):
                self.assertEqual(self._dangling(src), {}, f"{label} 被误报")


class TestLabelDoesNotSwallowButtons(unittest.TestCase):
    """`<label>` 里不许包 `<button>` —— 点行等于按按钮。

    HTML 的 label **激活行为**会把点击转发给内部第一个可标注控件（button 正是其一），
    这条路不走冒泡，`stopPropagation` 拦不住。行是 label、行里有「解绑」按钮时，
    点行内任何一处文字都会静默解绑——用户以为自己只是选中了这一行。

    真出过：深度捕捉的分镜行原本是 label，一次浏览器点选把一条已绑的控制视频
    直接摘掉了，而页面上没有任何反馈。判据是源级的：label 该包的是 checkbox 与
    input，包了 button 就是选错了元素。
    """

    @staticmethod
    def _label_spans(src: str):
        """逐个 `h("label"` 调用，按括号配平切出它的实参范围。"""
        for m in re.finditer(r'h\(\s*"label"', src):
            i = src.index("(", m.start())
            depth, j = 0, i
            while j < len(src):
                if src[j] == "(":
                    depth += 1
                elif src[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            yield m.start(), src[i:j]

    def test_no_button_inside_a_label(self):
        bad = []
        for f in sorted((_assets() / "app").glob("*.js")) + [_assets() / "app.js"]:
            src = f.read_text(encoding="utf-8")
            for pos, span in self._label_spans(src):
                if re.search(r'h\(\s*"button"', span):
                    bad.append(f"{f.name}:{src[:pos].count(chr(10)) + 1}")
        self.assertEqual(bad, [], "label 会把点击转发给里面的 button，"
                                  f"点这一行任何地方都等于按下它：{bad}")
