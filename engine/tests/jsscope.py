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

"""Studio 前端的**静态可解析性**分析器（纯 stdlib，零依赖，不需要 node）。

Studio 是原生 ESM、免构建——好处是改完刷新即生效，代价是**没有编译期**：
写错一个名字不会有任何征兆，直到那行代码真的被执行。而前端大量代码挂在
条件分支上（"有成片时才渲染这块"），于是一个悬空引用可以在仓库里躺很久，
只在某个状态第一次出现时把整页打成「加载失败」——改名重构若只改了
两处引用里的一处，漏的那处可能要到**合成出成片之后**才走到。

本模块提供两项确定性检查，都只吃源码文本：

* :func:`dangling_identifiers` —— 引用了谁也没声明过的名字（`x is not defined`）
* :func:`broken_imports` —— import 的文件不存在，或对面根本没 export 这个名字

两者都是 `assertEqual({}, ...)` 型判据：报出来的每一条，浏览器一定会抛。

**故意保守**：分析器不建作用域树，只做"整文件平铺"的声明集合——所以
"声明了但不在这个作用域可见"这类错抓不到，宁可漏报也绝不误报（误报的积累会
不断扩大白名单，最终把真 bug 也收进去）。要抓的是重构改名留下的悬空引用，那一类平铺就够。
"""
from __future__ import annotations

import re
from pathlib import Path

# ── 词法 ────────────────────────────────────────────────────────────────
IDENT = re.compile(r"[A-Za-z_$][\w$]*")

KEYWORDS = frozenset("""
await break case catch class const continue debugger default delete do else
export extends finally for function if import in instanceof let new of return
static super switch this throw try typeof var void while with yield async get
set true false null from as
""".split())

# 浏览器/语言内建全局：bare 引用它们合法。新增一个前，先确认那真是全局，
# 而不是某个模块忘了 import——把 bug 塞进白名单是这套守卫唯一的失效方式。
GLOBALS = frozenset("""
globalThis window document console navigator location history screen
localStorage sessionStorage performance crypto fetch alert confirm prompt
matchMedia getComputedStyle requestAnimationFrame cancelAnimationFrame
requestIdleCallback queueMicrotask setTimeout clearTimeout setInterval
clearInterval structuredClone atob btoa innerWidth innerHeight scrollX scrollY
encodeURIComponent decodeURIComponent encodeURI decodeURI
parseInt parseFloat isNaN isFinite undefined NaN Infinity
Object Array String Number Boolean Symbol BigInt Math JSON Date RegExp
Error TypeError RangeError SyntaxError Promise Map Set WeakMap WeakSet
Proxy Reflect Intl URL URLSearchParams AbortController FormData Blob File
FileReader Image Audio Event CustomEvent EventTarget Node Element HTMLElement
DOMParser XMLHttpRequest WebSocket ResizeObserver IntersectionObserver
MutationObserver TextEncoder TextDecoder ArrayBuffer DataView CSS
Uint8Array Uint8ClampedArray Int32Array Float32Array Float64Array
""".split())


def blank_noise(src: str, keep_strings: bool = False) -> str:
    """把注释与字符串**字面量内容**抹成等长空白（保留换行=保留行号），只留代码。

    `keep_strings=True` 时只抹注释、留下引号串的原文——给 import 分析用：
    模块路径本身就写在字符串里，一并抹掉就再没得解析了。

    模板串保留 `${}` 里的表达式（那是真代码，`${foo}` 里的 foo 会真的求值），
    插值标记本身抹掉——否则 `$` 会被当成一个独立标识符。
    正则字面量整段抹掉：`/[a-z]+/` 里的 a、z 不是引用。

    实现是**上下文栈**而不是逐层手写嵌套：模板串里能套插值、插值里能套模板串、
    还能套正则和注释。手写嵌套只能覆盖到写的时候想得到的那几层，
    漏的那层会把字面量内容当代码读出来（如 ``${x.replace(/[^\\w-]/g, "_")}``
    里的正则会漏成一个名叫 `w` 的悬空引用）。
    """
    out: list[str] = []
    i, n = 0, len(src)
    prev = ""                        # 上一个有效字符：区分除号与正则字面量
    # 帧：["code", 花括号深度] / ["tpl", None]。插值 `${` 压一个 code 帧，
    # 该帧深度归零时遇到的 `}` 就是插值的收口而非代码块的收口。
    stack: list[list] = [["code", 0]]

    def blank(text: str) -> str:
        return "".join(c if c == "\n" else " " for c in text)

    while i < n:
        frame = stack[-1]
        c, nxt = src[i], src[i + 1] if i + 1 < n else ""

        if frame[0] == "tpl":                       # ── 模板串文本区 ──
            if c == "\\":
                out.append("  ")
                i += 2
            elif c == "`":
                out.append(" ")
                i += 1
                stack.pop()
                prev = "`"
            elif c == "$" and nxt == "{":
                out.append("  ")
                i += 2
                stack.append(["code", 0])
                prev = ""
            else:
                out.append(c if c == "\n" else " ")
                i += 1
            continue

        if c == "/" and nxt == "/":                 # ── 代码区 ──
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(blank(src[i:j]))
            i = j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(blank(src[i:j]))
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    j += 1
                    break
                j += 1
            out.append(src[i:j] if keep_strings else blank(src[i:j]))
            i = j
            prev = '"'
        elif c == "`":
            out.append(" ")
            i += 1
            stack.append(["tpl", None])
            prev = "`"
        elif c == "/" and not (prev.isalnum() or prev in "_$)]}`\"'"):
            j, in_class = i + 1, False              # 正则字面量
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "\n":
                    break
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    j += 1
                    break
                j += 1
            while j < n and src[j].isalpha():       # 尾旗标 g/i/m/s/u/y
                j += 1
            out.append(blank(src[i:j]))
            i = j
            prev = "/"
        elif c == "{":
            frame[1] += 1
            out.append(c)
            i += 1
            prev = c
        elif c == "}" and frame[1] == 0 and len(stack) > 1:
            out.append(" ")                          # 插值收口，回到模板文本区
            i += 1
            stack.pop()
            prev = "}"
        elif c == "}":
            frame[1] -= 1
            out.append(c)
            i += 1
            prev = c
        else:
            out.append(c)
            if not c.isspace():
                prev = c
            i += 1
    return "".join(out)


# ── 声明面 ──────────────────────────────────────────────────────────────
def _pattern_names(pat: str) -> set[str]:
    """一段绑定模式里被绑定的名字：`{a, b: c, ...d} = x` → a、c、d。

    默认值表达式（`= expr`）里的标识符是**引用**不是绑定，跳过。
    """
    names: set[str] = set()
    i, depth, in_default = 0, 0, False
    while i < len(pat):
        ch = pat[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth <= 1:
            in_default = False
        elif ch == "=" and pat[i + 1:i + 2] != "=" and pat[i - 1:i] not in ("!", "<", ">"):
            in_default = True
        elif ch == ":" and depth >= 1:
            # `{ key: binding }`：冒号左边是键名，撤回它
            left = list(IDENT.finditer(pat[:i]))
            if left:
                names.discard(left[-1].group())
        if not in_default:
            m = IDENT.match(pat, i)
            if m:
                if m.group() not in KEYWORDS:
                    names.add(m.group())
                i = m.end()
                continue
        i += 1
    return names


def _declarator_names(code: str) -> set[str]:
    """`const/let/var` 的绑定名——含解构、含 `let a = 1, b = 2` 的第二个。"""
    names: set[str] = set()
    for m in re.finditer(r"\b(?:const|let|var)\b", code):
        i, depth, head = m.end(), 0, m.end()
        while i < len(code):
            ch = code[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and ch == ";":
                break
            elif depth == 0 and ch == "=" and code[i + 1:i + 2] != "=":
                names |= _pattern_names(code[head:i])
                i, d2 = i + 1, 0            # 跳过初始化表达式到下一个顶层逗号
                while i < len(code):
                    c2 = code[i]
                    if c2 in "([{":
                        d2 += 1
                    elif c2 in ")]}":
                        if d2 == 0:
                            break
                        d2 -= 1
                    elif d2 == 0 and c2 in ";,":
                        break
                    i += 1
                if i >= len(code) or code[i] != ",":
                    head = i
                    break
                head = i + 1
            i += 1
        names |= _pattern_names(code[head:i])
    return names


def declared_names(code: str) -> set[str]:
    """整文件平铺的"已声明"集合（含 import 绑定、函数/类名、形参、方法名）。

    宁可多收：多收只会漏报，少收会误报。
    """
    names = _declarator_names(code)
    names |= {m.group(1) for m in re.finditer(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)", code)}
    names |= {m.group(1) for m in re.finditer(r"\bclass\s+([A-Za-z_$][\w$]*)", code)}
    names |= {m.group(1) for m in re.finditer(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", code)}
    names |= {m.group(1) for m in re.finditer(r"(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*=>", code)}
    # 形参：任何后随 `=>` 或 `{` 的括号组
    for m in re.finditer(r"\(([^()]*)\)\s*(?:=>|\{)", code):
        names |= _pattern_names(m.group(1))
    # 方法/简写方法定义 `name(a, b) {` —— 是属性名不是自由引用
    names |= {m.group(1) for m in re.finditer(
        r"(?:^|[{;,}\n])\s*(?:static\s+|async\s+|get\s+|set\s+|\*\s*)*"
        r"([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*\{", code)}
    for m in re.finditer(r"\bimport\b([^;]*?)\bfrom\b", code, re.S):
        spec = re.sub(r"\bas\b", " ", m.group(1))
        names |= {w.group() for w in IDENT.finditer(spec) if w.group() not in KEYWORDS}
    return names


# ── 引用面 ──────────────────────────────────────────────────────────────
def referenced_names(code: str):
    """产出 (名字, 行号)：代码里以**自由变量**形态出现的标识符。

    排除：属性访问（`.foo` / `?.foo`）、对象键与 label（`foo:`）、
    数字字面量的指数位（`1e9` 里的 `e9`）、`function`/`class` 后的定义名。
    """
    for m in IDENT.finditer(code):
        w = m.group()
        if w in KEYWORDS:
            continue
        if m.start() and (code[m.start() - 1].isalnum() or code[m.start() - 1] in "_$"):
            continue                                   # 更大 token 的一部分（1e9）
        before = code[:m.start()].rstrip()
        if before.endswith((".", "?.")) or re.search(r"\b(function|class)\s*\*?\s*$", before):
            continue
        if code[m.end():].lstrip().startswith(":"):    # 对象键 / label（三元也一并让过）
            continue
        yield w, code[:m.start()].count("\n") + 1


def dangling_identifiers(path: Path) -> dict[str, int]:
    """该文件里引用了、却在**全文件**范围内谁也没声明过的名字 → {名字: 首个行号}。"""
    code = blank_noise(path.read_text(encoding="utf-8"))
    known = declared_names(code) | GLOBALS
    bad: dict[str, int] = {}
    for name, line in referenced_names(code):
        if name not in known:
            bad.setdefault(name, line)
    return bad


# ── import 图 ───────────────────────────────────────────────────────────
def exported_names(code: str) -> set[str]:
    """一个模块对外 export 的名字集合。"""
    names: set[str] = set()
    for m in re.finditer(r"\bexport\s*\{([^}]*)\}", code, re.S):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            names.add(part.split()[-1])                # `a as b` → b
    for m in re.finditer(r"\bexport\s+(?:default\s+)?"
                         r"(?:async\s+)?(?:function\s*\*?|class|const|let|var)\s+"
                         r"([A-Za-z_$][\w$]*)", code):
        names.add(m.group(1))
    if re.search(r"\bexport\s+default\b", code):
        names.add("default")
    return names


def imported_bindings(code: str):
    """产出 (来源 spec, {本地名: 对面导出名}, 行号)。副作用 import 也报（映射为空）。

    入参须是 `blank_noise(src, keep_strings=True)` 的产物——模块路径写在字符串里，
    用默认的 `blank_noise` 会连路径一起抹掉，解析出来的 spec 是一串空格。
    """
    for m in re.finditer(r"\bimport\b\s*(?:([^;]*?)\s*\bfrom\b\s*)?"
                         r"[\"']([^\"']+)[\"']", code, re.S):
        clause, spec = (m.group(1) or "").strip(), m.group(2)
        line = code[:m.start()].count("\n") + 1
        want: dict[str, str] = {}
        if clause:
            named = re.search(r"\{([^}]*)\}", clause, re.S)
            if named:
                for part in named.group(1).split(","):
                    bits = part.strip().split()
                    if not bits:
                        continue
                    want[bits[-1]] = bits[0]           # `a as b` → 本地 b ← 导出 a
            head = re.sub(r"\{[^}]*\}", "", clause, flags=re.S)
            head = re.sub(r"\*\s*as\s+[A-Za-z_$][\w$]*", "", head)
            for bit in head.split(","):
                bit = bit.strip()
                if bit and IDENT.fullmatch(bit):
                    want[bit] = "default"
        yield spec, want, line


def module_imports(path: Path):
    """该文件的 import 清单——注释已抹、字符串保留（模块路径在字符串里）。"""
    return imported_bindings(
        blank_noise(path.read_text(encoding="utf-8"), keep_strings=True))


def broken_imports(path: Path) -> list[str]:
    """该文件里解析不了的 import：文件不存在 / 对面没导出这个名字。"""
    problems: list[str] = []
    for spec, want, line in module_imports(path):
        if not spec.startswith("."):
            continue                                   # 裸模块名：本工程不用打包器，不该出现
        target = (path.parent / spec).resolve()
        if not target.is_file():
            problems.append(f"{path.name}:{line} import 的文件不存在：{spec}")
            continue
        have = exported_names(blank_noise(target.read_text(encoding="utf-8")))
        for local, exported in sorted(want.items()):
            if exported not in have:
                problems.append(
                    f"{path.name}:{line} {target.name} 没有导出 `{exported}`"
                    + (f"（想绑成 {local}）" if local != exported else ""))
    return problems


def frontend_files(assets: Path) -> list[Path]:
    """Studio 前端全部 ESM 源文件（app/ 分片 + app.js 入口，vendor 不算）。"""
    return sorted((assets / "app").glob("*.js")) + [assets / "app.js"]
