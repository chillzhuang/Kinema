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

"""剧本改编 · Track A：确定性结构切分（纯 Python · 零 LLM · 零三方依赖）。

「Python + AI 两段式」的 **Python 半**：把源文本里*免费的确定性*全部提取，给
Claude 指挥层（AI 半）稳定锚点，从而降低 token 消耗与跨段漂移。本模块**只做
机械切分**，绝不做拆书/分集/抽实体/拆镜等语义判断——那是 Claude 的活（铁律
「引擎内无 LLM provider，智能由 Claude 提供」）。

三种确定性能力：
  · 剧本解析 `parse_screenplay` —— Fountain 语法（`INT./EXT.` 场景头 + 大写行
    角色名 + 对白 + `TO:` 转场）与 Final Draft `.fdx`（XML）。剧本天生结构化，
    正则 / stdlib xml 即可解析为「场景三元组」。
  · 小说切分 `split_novel` —— 按「第 N 章 / Chapter N / 序章 · 楔子 …」章标切
    块；无章标回落窗口化。
  · 窗口化 `window_text` —— 段落感知的定长重叠窗口，供 Claude 分片 `Read`。

统一入口 `structural_digest(text, kind)` 产出可 JSON 化的结构索引，由 CLI
落 `project/<pid>/source/segments.json`（sidecar，不进 project.json，避免巨
blob 拖垮每次 Project.load / DB upsert）。
"""
from __future__ import annotations

import hashlib
import html.parser
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

# ---- 指纹（与 lineage.fingerprint 同格式：sha256:<hex16>，touch 不误报）----


def text_fingerprint(text: str) -> str:
    """源文本内容指纹，格式对齐 `lineage.fingerprint`（`sha256:` + 16 位十六进制）。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# 候选编码（无 BOM、非 UTF-8 时按「中文多、乱码少」打分择优）。顺序 = 同分时的
# 优先编码名：gb18030 先于 gbk（超集·规范名）、big5 先于 big5hkscs、双字节末位。
_DECODE_CANDIDATES = ("gb18030", "big5", "gbk", "big5hkscs", "utf-16-le", "utf-16-be")


def _decode_score(sample: str) -> float:
    """给某候选解码结果打分：中文（CJK 统一表意区）占比高、乱码（U+FFFD 替换符 +
    私用区 PUA，编码误判的典型产物）占比低者胜。用于在候选编码间择优。"""
    n = len(sample)
    if not n:
        return -9.0
    cjk = sum(1 for c in sample if "一" <= c <= "鿿")
    bad = sum(1 for c in sample if c == "�" or "" <= c <= "")
    return (cjk - 6 * bad) / n


def decode_source(raw: bytes) -> tuple[str, str]:
    """稳健解码源文本字节流 → ``(文本, 编码名)``，**自动识别并转 UTF-8**。

    中文小说/剧本 .txt 常见 UTF-8 / GBK-GB18030 / Big5-繁体 / 带或不带 BOM 的
    UTF-16——不检测一律 ``errors=replace`` 会把整篇静默替换成 U+FFFD（主用例数据
    损坏）。策略：① 显式 BOM（UTF-16 / UTF-8-BOM）直接命中；② 合法 UTF-8 严格命中
    即用（最可信，极难误判）；③ 否则在候选编码里**按「中文多、乱码少」打分择优**——
    gb18030 对 Big5 字节会静默 mojibake（满屏私用区 PUA）却不报错，故不能「首个不报错
    就用」，必须逐一打分比较。返回文本已是 Unicode，入库落盘即 UTF-8；彻底无法解码
    （二进制/未知编码）时最优候选仍满是乱码，交由上层乱码闸 undecodable_ratio 拦截。
    """
    if not raw:
        return "", "utf-8"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    if raw[:3] == b"\xef\xbb\xbf":
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"          # 合法 UTF-8：直接用
    except UnicodeDecodeError:
        pass
    head = raw[:262144]                              # 头部 256KB 检测（够代表性、够快）
    best = None
    for enc in _DECODE_CANDIDATES:
        try:
            score = _decode_score(head.decode(enc, errors="replace"))
        except LookupError:                          # 该 codec 不可用（罕见）
            continue
        if best is None or score > best[0]:          # 严格 > ：同分保留更靠前的规范编码名
            best = (score, enc)
    if best is None:                                 # 理论不可达（gb18030 恒可 replace 解）
        return raw.decode("utf-8", errors="replace"), "utf-8/replace"
    return raw.decode(best[1], errors="replace"), best[1]


def undecodable_ratio(text: str) -> float:
    """乱码占比（入库前的最终兜底信号）：
      · U+FFFD 替换符 + 私用区 PUA（U+E000–U+F8FF，编码误判 mojibake 的典型产物）；
      · C0/C1 控制符（\t\n\r 之外的 <0x20、以及 DEL/0x7F–0x9F）——**二进制/加密文件**
        被当文本解码后的强特征，正常小说/剧本正文一律没有。
    正常简/繁中文与 UTF-16 文本此值≈0；据此拦截「自动转码后仍非文本」的上传，
    避免把乱码/二进制烧进 raw.txt 与切分。"""
    if not text:
        return 0.0
    bad = 0
    for c in text:
        o = ord(c)
        if c == "\ufffd" or 0xE000 <= o <= 0xF8FF:          # 替换符 + 私用区 PUA
            bad += 1
        elif o < 0x20 and c not in "\t\n\r":              # C0 控制符（保留制表/换行/回车）
            bad += 1
        elif 0x7F <= o <= 0x9F:                             # DEL + C1 控制符
            bad += 1
    return bad / len(text)


def _preview(s: str, n: int = 80) -> str:
    """段落/场景预览：压平空白、截断，供索引一眼可读。"""
    flat = re.sub(r"\s+", " ", (s or "")).strip()
    return flat[:n]


# ---- EPUB 解析：纯 stdlib 拆书（zipfile + ElementTree + html.parser）--------
#
# EPUB = 一个 ZIP 容器：`META-INF/container.xml` 指向 OPF 包文档；OPF 的
# manifest 登记全部资源、spine 定阅读顺序；目录（章标题）来自 EPUB3 的
# `<nav epub:type="toc">` 或 EPUB2 的 NCX。本段只做**机械抽正文 + 抽章标题**，
# 语义拆书/分集/抽实体一律仍归 Claude（铁律「引擎内无 LLM provider」）。产出的
# segments 与 `split_novel` 的 dict 形状**逐字段一致**，下游零分叉。

# XML 命名空间（用 `{*}` 通配符做 find 时无需，仅解析 epub:type 属性等场景用）


def _resolve_href(base_dir: str, href: str) -> str:
    """把 manifest/nav 里的相对 href 归一成 ZIP 内的绝对路径（去 #fragment）。"""
    href = (href or "").split("#", 1)[0]
    if not href:
        return ""
    return posixpath.normpath(posixpath.join(base_dir or "", href))


def _attr_epub_type(el) -> str:
    """取元素的 epub:type（命名空间可有可无，两种写法都认）。"""
    for k, v in el.attrib.items():
        if k == "type" or k.endswith("}type"):
            return (v or "").strip()
    return ""


class _HTMLTextExtractor(html.parser.HTMLParser):
    """XHTML → 纯文本：跳过 script/style/head 内容、块级元素后补换行、实体自动
    反转义（convert_charrefs），并把连续空行压成单空行。另单独截获 `<title>`（在
    head 里，正文抽取会跳过 head，故需专门捕获，作为章标题回落）。"""

    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "li", "tr", "blockquote", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "br" or tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return _collapse_lines("".join(self._parts))

    def get_title(self) -> str:
        return "".join(self._title_parts).strip()


def _collapse_lines(text: str) -> str:
    """逐行 strip、把连续空行压成单空行、去首尾空行。"""
    out: list[str] = []
    for ln in text.split("\n"):
        s = ln.strip()
        if s:
            out.append(s)
        elif out and out[-1] != "":
            out.append("")
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _decode_xml_bytes(data: bytes) -> str:
    """按 XML 声明里的 encoding 解码 XHTML/NCX 字节（缺省 utf-8）；带 BOM 走
    utf-8-sig；未知 codec 或解码异常一律回落 utf-8（errors=replace 不炸）。"""
    if data[:3] == b"\xef\xbb\xbf":
        return data.decode("utf-8-sig", errors="replace")
    enc = "utf-8"
    m = re.search(rb'encoding=["\']([\w\-]+)["\']', data[:200])
    if m:
        try:
            enc = m.group(1).decode("ascii")
        except Exception:
            enc = "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _strip_html(data: bytes) -> tuple[str, str]:
    """XHTML 字节 → (正文纯文本, `<title>` 文本)。解析异常时返回空文本。"""
    parser = _HTMLTextExtractor()
    parser.feed(_decode_xml_bytes(data))
    parser.close()
    return parser.get_text(), parser.get_title()


def _parse_nav_xhtml(data: bytes, base_dir: str) -> list[tuple[str, str]]:
    """EPUB3 nav.xhtml → [(内容文档 ZIP 路径, 章标题), ...]（阅读顺序）。
    优先 `<nav epub:type="toc">`，无则取首个 `<nav>`。"""
    root = ET.fromstring(data)                       # 可能抛 ParseError，由上层兜
    # 注意：ElementTree 的 iter() 是字面 tag 匹配、不认 {*} 通配，必须走 findall 路径
    navs = root.findall(".//{*}nav")
    chosen = None
    for nav in navs:
        if _attr_epub_type(nav) == "toc":
            chosen = nav
            break
    if chosen is None and navs:
        chosen = navs[0]
    pairs: list[tuple[str, str]] = []
    if chosen is not None:
        for a in chosen.findall(".//{*}a"):
            href = a.get("href")
            title = "".join(a.itertext()).strip()
            if href and title:
                pairs.append((_resolve_href(base_dir, href), title))
    return pairs


def _parse_ncx(data: bytes, base_dir: str) -> list[tuple[str, str]]:
    """EPUB2 toc.ncx → [(内容文档 ZIP 路径, 章标题), ...]（navPoint 顺序）。"""
    root = ET.fromstring(data)                       # 可能抛 ParseError，由上层兜
    pairs: list[tuple[str, str]] = []
    for np in root.findall(".//{*}navPoint"):        # iter() 不认 {*} 通配，用 findall
        label = np.find("{*}navLabel/{*}text")
        content = np.find("{*}content")
        if label is None or content is None:
            continue
        title = "".join(label.itertext()).strip()
        src = content.get("src")
        if title and src:
            pairs.append((_resolve_href(base_dir, src), title))
    return pairs


def _epub_nav_titles(zf: zipfile.ZipFile, manifest: dict, spine_el) -> dict:
    """解析目录，产出 {内容文档 ZIP 路径(去 fragment): 章标题}（首个标题胜）。
    优先 EPUB3 nav（manifest 里 properties 含 nav 的项），回落 EPUB2 NCX
    （spine@toc → manifest[toc].href）。任何子部件坏了都静默跳过，不阻断抽正文。"""
    def _merge(pairs: list[tuple[str, str]]) -> dict:
        out: dict = {}
        for href, title in pairs:
            if href and href not in out:            # 首个标题胜
                out[href] = title
        return out

    # EPUB3：properties 是空格分隔列表，含 "nav" 者为目录文档
    for meta in manifest.values():
        if "nav" in (meta.get("properties") or "").split():
            try:
                pairs = _parse_nav_xhtml(zf.read(meta["href"]),
                                         posixpath.dirname(meta["href"]))
                mp = _merge(pairs)
                if mp:
                    return mp
            except (KeyError, ET.ParseError, OSError):
                pass
            break
    # EPUB2：spine 的 toc 属性指向 NCX manifest 项
    toc_id = spine_el.get("toc") if spine_el is not None else None
    if toc_id and toc_id in manifest:
        try:
            ncx_href = manifest[toc_id]["href"]
            pairs = _parse_ncx(zf.read(ncx_href), posixpath.dirname(ncx_href))
            return _merge(pairs)
        except (KeyError, ET.ParseError, OSError):
            pass
    return {}


def is_epub(raw: bytes, filename: str = "") -> bool:
    """判定字节流是否 EPUB：后缀 `.epub`，或（ZIP 魔数 + 含 container.xml）。
    坏 ZIP 一律 False（不抛）。"""
    if (filename or "").lower().endswith(".epub"):
        return True
    if not raw or raw[:4] != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return "META-INF/container.xml" in zf.namelist()
    except Exception:
        return False


def extract_epub(raw: bytes) -> tuple[str, list[dict]]:
    """EPUB 字节流 → ``(全书正文, segments)``。

    segments 与 `split_novel` 的 dict 形状逐字段一致：
    ``{index, type:"chapter", title, char_start, char_end, preview}``（index 从 1）。
    正文按 spine 顺序拼接，章标题优先取目录（nav/NCX），回落 `<title>`，再回落
    `第 N 章`。加密（DRM）/ 无正文文档等根本不可读的情形抛 `ValueError`（中文提
    示）；单个子文档坏了则跳过、不拖垮整本。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError("EPUB 文件损坏或非 ZIP 容器，无法解析。") from e
    with zf:
        names = set(zf.namelist())
        # DRM 闸：含 encryption.xml 的加密 EPUB 无法解析
        if "META-INF/encryption.xml" in names:
            raise ValueError(
                "加密 EPUB（含 DRM）无法解析——请提供无 DRM 版本，"
                "或用 Calibre 导出 EPUB/TXT 后再上传。")
        # ① container.xml → OPF 路径
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError) as e:
            raise ValueError("EPUB 缺少或无法解析 META-INF/container.xml。") from e
        rootfile = container.find(".//{*}rootfile")
        opf_path = rootfile.get("full-path") if rootfile is not None else ""
        if not opf_path:
            raise ValueError("EPUB container.xml 未声明 OPF 根文件。")
        # ② OPF → manifest + spine
        try:
            opf = ET.fromstring(zf.read(opf_path))
        except (KeyError, ET.ParseError) as e:
            raise ValueError("EPUB OPF 包文档缺失或无法解析。") from e
        opf_dir = posixpath.dirname(opf_path)
        manifest: dict = {}
        for item in opf.findall(".//{*}manifest/{*}item"):
            iid = item.get("id")
            if not iid:
                continue
            manifest[iid] = {
                "href": _resolve_href(opf_dir, item.get("href")),
                "media-type": (item.get("media-type") or "").strip().lower(),
                "properties": (item.get("properties") or ""),
            }
        spine_el = opf.find(".//{*}spine")
        spine: list[str] = []
        if spine_el is not None:
            for ref in spine_el.findall("{*}itemref"):
                idref = ref.get("idref")
                if idref:
                    spine.append(idref)
        # ③ 目录（章标题）：内容文档 ZIP 路径 → 标题
        nav_map = _epub_nav_titles(zf, manifest, spine_el)
        # ④ 按 spine 顺序抽正文（仅 xhtml；子文档坏了跳过、空文本跳过）
        docs: list[tuple[str, str, str]] = []       # (zip_path, doc_text, doc_title)
        for idref in spine:
            meta = manifest.get(idref)
            if not meta or meta["media-type"] != "application/xhtml+xml":
                continue
            try:
                doc_text, doc_title = _strip_html(zf.read(meta["href"]))
            except (KeyError, ET.ParseError, OSError, ValueError):
                continue
            if not doc_text.strip():
                continue
            docs.append((meta["href"], doc_text, doc_title))
        if not docs:
            raise ValueError("EPUB 无可读正文内容文档")
        # ⑤ 拼正文 + 记录每篇起始偏移（篇间以空行分隔）
        full_text = ""
        offsets: list[int] = []
        for _zpath, doc_text, _t in docs:
            offsets.append(len(full_text))
            full_text += doc_text + "\n\n"
        # ⑥ 组装 segments（形状对齐 split_novel）
        segments: list[dict] = []
        for i, (zpath, _doc_text, doc_title) in enumerate(docs):
            char_start = offsets[i]
            char_end = offsets[i + 1] if i + 1 < len(offsets) else len(full_text)
            title = nav_map.get(zpath) or doc_title or f"第 {i + 1} 章"
            segments.append({"index": i + 1, "type": "chapter",
                             "title": title.strip()[:40],
                             "char_start": char_start, "char_end": char_end,
                             "preview": _preview(full_text[char_start:char_end])})
        return full_text, segments


# ---- 格式判定 --------------------------------------------------------------

# Fountain 场景头：行首 INT / EXT / EST / INT./EXT / I/E（后接 . 或空格）
_SCENE_HEAD = re.compile(
    r"^\s*(INT\.?/EXT|I/E|INT|EXT|EST)[\.\s]", re.IGNORECASE | re.MULTILINE)


def detect_format(text: str, *, filename: str = "") -> str:
    """启发式判定源文本类型：``screenplay`` | ``novel``。

    后缀优先（.fdx/.fountain）→ FinalDraft XML 头 → 场景头计数（≥2 判剧本）。
    判不准不致命：只影响用哪套确定性切分，语义仍由 Claude 兜底。
    """
    low = (filename or "").lower()
    if low.endswith(".fdx"):
        return "screenplay"
    if low.endswith((".fountain", ".spmd")):
        return "screenplay"
    head = text.lstrip()[:400]
    if head.startswith("<?xml") and "<FinalDraft" in head:
        return "screenplay"
    # 散文（中/英）几乎不会出现行首 INT./EXT. 场景头，≥2 即可稳判剧本
    return "screenplay" if len(_SCENE_HEAD.findall(text)) >= 2 else "novel"


# ---- 剧本解析：Fountain -----------------------------------------------------

_FOUNTAIN_HEAD_LINE = re.compile(
    r"^(INT\.?/EXT|I/E|INT|EXT|EST)\b\.?\s*(.*)$", re.IGNORECASE)
# 强制场景头：单个 . 起头（但排除 ..，那是强制动作/转场里的省略号语义）
_FORCED_SCENE = re.compile(r"^\.[^\.].*$")
# 角色名 cue：全大写（含空格/数字/&），可带 (V.O.) 等括注；@ 强制
_CHAR_EXT = re.compile(r"\s*\(.*?\)\s*$")


def _parse_heading(rest: str, int_ext: str) -> dict:
    """把场景头拆成 (int_ext, location, time_of_day)。时段取最后一个 ` - ` / ` — ` 段。"""
    parts = re.split(r"\s[-—–]\s", rest.strip())
    if len(parts) >= 2:
        location, tod = parts[0].strip(), parts[-1].strip()
    else:
        location, tod = rest.strip(), ""
    return {"int_ext": int_ext.upper().replace(" ", ""),
            "location": location, "time_of_day": tod.upper()}


def _is_char_cue(line: str, prev_blank: bool, next_nonblank: bool) -> bool:
    """Fountain 角色 cue 判定：上空行 + 下非空 + 去括注后为全大写（或 @ 强制）。"""
    s = line.rstrip()
    if not s or not prev_blank or not next_nonblank:
        return False
    if s.startswith("@"):
        return True
    core = _CHAR_EXT.sub("", s).strip()
    if len(core) < 2:
        return False
    # 全大写字母开头、无小写字母；允许空格/数字/./&/'
    return core[:1].isalpha() and core.upper() == core and not core.endswith(":")


def _parse_fountain(text: str) -> list[dict]:
    # 用 keepends 计算每行真实起始偏移（兼容 \n / \r\n / \r，不硬编码分隔符长度）
    raw_lines = text.splitlines(keepends=True)
    offsets, acc = [], 0
    for ln in raw_lines:
        offsets.append(acc)
        acc += len(ln)
    lines = [ln.rstrip("\r\n") for ln in raw_lines]
    total = len(text)

    scenes: list[dict] = []
    cur: dict | None = None

    def _blank(i: int) -> bool:
        return i < 0 or i >= len(lines) or not lines[i].strip()

    for i, raw in enumerate(lines):
        line = raw.rstrip()
        stripped = line.strip()
        head_m = _FOUNTAIN_HEAD_LINE.match(stripped)
        forced = bool(_FORCED_SCENE.match(stripped))
        if head_m or forced:
            if cur is not None:
                cur["char_end"] = offsets[i]
            if head_m:
                meta = _parse_heading(head_m.group(2), head_m.group(1))
                heading = stripped
            else:  # 强制场景头 .SOMETHING
                meta = _parse_heading(stripped[1:], "")
                heading = stripped[1:]
            cur = {"index": len(scenes) + 1, "type": "scene",
                   "heading": heading, "characters": [],
                   "char_start": offsets[i], "char_end": total, **meta}
            scenes.append(cur)
            continue
        if cur is not None and _is_char_cue(line, _blank(i - 1), not _blank(i + 1)):
            name = _CHAR_EXT.sub("", stripped.lstrip("@")).strip()
            if name and name not in cur["characters"]:
                cur["characters"].append(name)

    for sc in scenes:
        seg = text[sc["char_start"]:sc["char_end"]]
        sc["preview"] = _preview(seg)
    return scenes


# ---- 剧本解析：Final Draft .fdx（XML） -------------------------------------


def _parse_fdx(text: str) -> list[dict]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    scenes: list[dict] = []
    cur: dict | None = None
    for para in root.iter("Paragraph"):
        ptype = (para.get("Type") or "").strip()
        body = "".join(t.text or "" for t in para.iter("Text")).strip()
        if not body and ptype != "Scene Heading":
            continue
        if ptype == "Scene Heading":
            m = _FOUNTAIN_HEAD_LINE.match(body)
            if m:
                meta = _parse_heading(m.group(2), m.group(1))
            else:
                meta = {"int_ext": "", "location": body, "time_of_day": ""}
            cur = {"index": len(scenes) + 1, "type": "scene", "heading": body,
                   "characters": [], "char_start": None, "char_end": None,
                   "preview": _preview(body), **meta}
            scenes.append(cur)
        elif ptype == "Character" and cur is not None:
            name = _CHAR_EXT.sub("", body).strip()
            if name and name not in cur["characters"]:
                cur["characters"].append(name)
    return scenes


def parse_screenplay(text: str, *, filename: str = "") -> list[dict]:
    """剧本 → 场景列表。.fdx / FinalDraft XML 走 XML 解析，其余走 Fountain 正则。"""
    low = (filename or "").lower()
    head = text.lstrip()[:400]
    if low.endswith(".fdx") or (head.startswith("<?xml") and "<FinalDraft" in head):
        return _parse_fdx(text)
    return _parse_fountain(text)


# ---- 小说切分：章标 --------------------------------------------------------

_CH_MARK = re.compile(
    r"^[ \t　]*("
    r"第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章回节節卷篇折]"
    r"|Chapter\s+\d+|CHAPTER\s+[IVXLCDM]+"
    r"|序章|序幕|楔子|引子|尾声|尾聲|终章|終章|后记|後記|番外"
    r")[^\n]*$",
    re.MULTILINE)


# 章标行的反例过滤：真章标（「第3章 渊启日」「楔子」）从不带句读，而正文叙述
# 完全可能以章标关键词起头——实测 350 章长篇切出 20 个误切块，全是
# 「第一节做了四个小时。」（杖的节）与「楔子只能进不能退。」（楔这件形态）
# 这类行首撞词的叙述句，且每一行都带句读。按句读过滤，两类一刀分开。
_PROSE_PUNCT = re.compile(r"[。！？；，…]")


def split_novel(text: str) -> list[dict]:
    """小说按章标切块。无章标返回空列表（调用方回落窗口化）。

    每块：``{index, type:"chapter", title, char_start, char_end, preview}``。
    首个章标之前的正文（前言/引子）并入首块前，作为 index=0 的「序」块。
    """
    marks = [m for m in _CH_MARK.finditer(text)
             if not _PROSE_PUNCT.search(m.group(0))]
    if not marks:
        return []
    units: list[dict] = []
    # 序块：首个章标前的正文（前言/引子），index=0；无则不产
    if marks[0].start() > 0 and text[:marks[0].start()].strip():
        units.append({"index": 0, "type": "chapter", "title": "序",
                      "char_start": 0, "char_end": marks[0].start(),
                      "preview": _preview(text[:marks[0].start()])})
    # 章块：index 1..N（与序块 0 连续）
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        units.append({"index": i + 1, "type": "chapter",
                      "title": m.group(0).strip()[:40],
                      "char_start": start, "char_end": end,
                      "preview": _preview(text[start:end])})
    return units


# ---- 窗口化：段落感知定长重叠 ----------------------------------------------


def window_text(text: str, *, size: int = 4000, overlap: int = 200) -> list[dict]:
    """把长文切成段落感知的定长重叠窗口，供 Claude 分片 Read。

    不切断段落：累积段落直到超过 size 才另起窗口；相邻窗口带 overlap 尾巴衔接
    上下文。每窗：``{index, type:"window", char_start, char_end, preview}``。
    """
    if not text.strip():
        return []
    # 保留偏移地按行累积成段（空行分段）：paras = [(char_start, char_end), ...]
    paras, buf_start, buf_len, pos = [], None, 0, 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            if buf_start is None:
                buf_start = pos
            buf_len += len(line)
        elif buf_start is not None:
            paras.append((buf_start, buf_start + buf_len))
            buf_start, buf_len = None, 0
        pos += len(line)
    if buf_start is not None:
        paras.append((buf_start, buf_start + buf_len))
    if not paras:
        return []

    # 累积段落成窗口组（[起段, 止段] 闭区间，不切断段落）
    groups, g_start, g_len = [], 0, 0
    for i, (a, b) in enumerate(paras):
        g_len += b - a
        if g_len >= size:
            groups.append((g_start, i))
            g_start, g_len = i + 1, 0
    if g_start < len(paras):
        groups.append((g_start, len(paras) - 1))

    out = []
    for k, (gs, ge) in enumerate(groups):
        a, b = paras[gs][0], paras[ge][1]
        if k > 0 and overlap > 0:      # 向前借 overlap 字符做上下文衔接（窗口间有意重叠）
            a = max(0, a - overlap)
        out.append({"index": k + 1, "type": "window", "char_start": a, "char_end": b,
                    "preview": _preview(text[a:b])})
    return out


# ---- 统一入口：结构索引 ----------------------------------------------------


def structural_digest(text: str, kind: str, *, filename: str = "") -> dict:
    """产出可 JSON 化的结构索引（segments sidecar 的内容）。

    kind=screenplay → 场景列表；kind=novel → 章标块，无章标回落窗口化。
    返回 ``{kind, chars, n_segments, segment_kind, segments:[...]}``；
    source.sha256 由 CLI 用 `lineage.fingerprint(落盘文件)` 计（权威、与血缘同源）。
    """
    if kind == "screenplay":
        segs = parse_screenplay(text, filename=filename)
        seg_kind = "scene"
    else:
        segs = split_novel(text)
        seg_kind = "chapter"
        if not segs:
            segs = window_text(text)
            seg_kind = "window"
    return {"kind": kind, "chars": len(text), "n_segments": len(segs),
            "segment_kind": seg_kind, "segments": segs}
