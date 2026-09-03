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

"""指令台（openDirectiveDialog）的源级守卫 —— 见 `docs/agents/directive-dialog.md`。

全站「把指令交给 AI」的按钮共二十余处，它们拼的都是**半成品模板**：正文里留着
`<在此填写>` 这样的槽，等用户填了才算一条完整指令。若退化为「一点即复制」，
不报错也不掉别的测试，只是用户把 `<在此写打磨方向>` 原样粘给了 AI，或者绕开
按钮手打一句没有定位坐标的话。故逐条钉住那些能跑通但交互失效的写法。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


def _assets() -> Path:
    import kinema
    return Path(kinema.__file__).parent / "studio_app"


def _src(name: str) -> str:
    return (_assets() / name).read_text(encoding="utf-8")


# 全部会出现指令按钮的视图文件
_VIEWS = ("app/chapter.js", "app/project.js", "app/widgets.js",
          "director/stage.js", "director/ui.js", "app/playbook.js",
          "app/overview.js")


class TestDirectiveDialogComponent(unittest.TestCase):
    """组件本身：站内组件单一落位 + 合并语义 + 键盘契约。"""

    def setUp(self):
        self.src = _src("app/components.js")

    def test_component_lives_in_components_and_is_exported(self):
        """指令台是站内组件（`director-stage-ui.md` ①「只用站内组件」）——落位只此一处，
        改观感只动 components.js + style.css，全站生效。"""
        self.assertIn("function openDirectiveDialog(", self.src)
        self.assertIn("openDirectiveDialog", self.src.split("export {")[-1],
                      "组件必须导出，否则视图只能各自手搓一遍")
        self.assertIn("openShell(", self.src,
                      "弹层必须复用 openShell 骨架（backdrop/Escape/退场三件套只写一份）")

    def test_merge_always_ends_with_the_need_line(self):
        """合并结果恒是「基础指令 + 换行 + 需求：<用户写的/占位>」——
        一条路、一个形状，不按有没有槽分叉出两种排版。"""
        self.assertIn("function dqMerge(", self.src)
        self.assertRegex(
            self.src,
            r"return `\$\{dqBase\(directive\)\}\\n\$\{DQ_NEED\}\$\{t \|\| dqSlot\(ask\)\}`")

    def test_empty_brief_still_copies_a_usable_template(self):
        """需求留空 = 复制「基础指令 + 需求：<占位>」——占位原样留给用户自己填，
        与指令台上线前「模板里带个尖括号」的老行为等价，不会少给东西。"""
        self.assertIn('const dqSlot = (ask) => `<${ask || "在此写你的需求"}>`;', self.src)

    def test_copy_feedback_contract(self):
        """复制后主钮先亮「✓ 已复制」再收起——toast 在屏幕另一头，
        手指还在按钮上，反馈得落在按下去的那个点。"""
        self.assertIn("✓ 已复制", self.src)
        self.assertIn('go.classList.add("ok")', self.src)

    def test_footer_carries_no_shortcut_hint(self):
        """底栏只留「字数 · 取消 · 复制指令」——不放快捷键提示条，
        那是噪音（真要用的人不看提示也会按）。"""
        self.assertNotIn("dq-kbd", self.src)
        self.assertNotIn("dq-kbd", _src("style.css"))

    def test_styles_exist_for_every_structural_class(self):
        """组件用到的类名必须都在 style.css 里有主人（漏一个就是裸 DOM 上屏）。"""
        css = _src("style.css")
        for cls in (".dq-card", ".dq-hd", ".dq-eyebrow", ".dq-meta", ".dq-x",
                    ".dq-body", ".dq-seck", ".dq-rule", ".dq-pre", ".dq-slot",
                    ".dq-ta", ".dq-note", ".dq-ft", ".dq-stat",
                    ".dq-ft-acts", ".dq-go"):
            self.assertIn(cls, css, f"{cls} 没有样式")
        self.assertNotIn(".dq-tail", css,
                         "尾部追加块已下线——需求恒是末行行内高亮，全站一个样式")


class TestNeedLineContract(unittest.TestCase):
    """需求行由组件唯一生成——这是「全站弹层长一个样」的机制保证。"""

    def test_component_is_the_only_author_of_the_need_line(self):
        """`需求：` 只许出现在 components.js 的 DQ_NEED，视图层一个字都不许自己拼。

        若留两条路——指令自带槽的走就地替换、没槽的把需求追加成末尾
        【我的需求】面板——同一个弹层在不同按钮下会长两个样（一处行内琥珀高亮、
        一处底部一整块），一眼就是「风格不统一」。只此一条路时，
        视图层再自己写一行「需求：」就会出现两行需求，故直接钉死。"""
        comp = _src("app/components.js")
        self.assertIn('const DQ_NEED = "需求：";', comp)
        self.assertIn("function dqMerge(directive, ask, need)", comp)
        for name in _VIEWS:
            for i, line in enumerate(_src(name).splitlines(), 1):
                if line.lstrip().startswith(("*", "//", "/*")):
                    continue                       # 注释里解释这套机制是允许的
                self.assertNotRegex(line, r"""["'`]需求：""",
                                    f"{name}:{i} 视图层自己拼了需求行——它归组件独有"
                                    "（散文里提「需求：」不算，只禁以它开头的字面量）")

    def test_tail_append_block_is_gone(self):
        """视图层不得出现【我的需求】尾部追加块（注释行不算）。"""
        for name in _VIEWS:
            for line in _src(name).splitlines():
                if line.lstrip().startswith(("*", "//", "/*")):
                    continue
                self.assertNotIn("【我的需求】", line, f"{name}: 尾部追加块回来了")
        self.assertNotIn("dq-tail", "\n".join(_src(n) for n in _VIEWS))

    def test_the_old_slot_mechanism_is_fully_retired(self):
        """调用点不传 `slot:`、不设 `*_SLOT` 常量——两处写同一个槽必然漂移；
        槽由组件按 `ask` 现生成，槽与正文根本不可能对不上（正文里没有槽）。"""
        blob = "\n".join(_src(n) for n in _VIEWS)
        self.assertNotRegex(blob, r"\bslot:\s", "还有调用点在传 slot:")
        self.assertNotRegex(blob, r"const\s+[A-Z][A-Z0-9_]*_SLOT\s*=", "还有 *_SLOT 常量")

    def test_every_dialog_names_what_to_write(self):
        """每个弹层都要给 `ask`（末行占位写清「这里该写什么」）——
        缺了会落到通用兜底文案，十七个弹层就有一个显得没人管。"""
        opens, asks = 0, 0
        for name in _VIEWS:
            src = _src(name)
            opens += src.count("openDirectiveDialog({")
            asks += len(re.findall(r"^\s*(?:directive, )?ask:", src, re.M))
        self.assertGreaterEqual(opens, 15, "调用点少了？改造被回退了")
        self.assertGreaterEqual(asks, opens,
                                f"{opens} 个弹层只有 {asks} 个给了 ask")


if __name__ == "__main__":
    unittest.main()
