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

"""字幕生成：由分镜时间轴 + 字幕文案生成 ASS，合成段烧录。

原则：**视频本体永远不带字**（生成模型的提示词有防字地板），字幕全部在这里
按画风样式后置烧录——换样式 = 改 profile 的 subtitle 配置，成片重合成即生效。

五种模式：caption 底部字幕（默认，全参数化随画风换装）/ bubble 头顶对话气泡
（对白进气泡、旁白自动退回底部字幕）/ dialogue_box 游戏对话框 / centered 居中
大字 / ranking 榜单徽章。

行长与断行遵循 Netflix 简中 Timed Text 规范：行宽按画布与字号推（见
`_cjk_max_chars`）、缺省两行、优先在标点处断、居中坐落底部安全区之上。
行宽是硬约束（`WrapStyle: 2` 下 libass 不会替超宽行折返，只会跑出安全边距），
放不下两行就折第三行——见 `_wrap`。
字幕文本以 narration 为真源（**音字必须逐字一致**——观众听到什么就看到什么，
影视字幕铁律）；caption 只在无旁白的纯画面镜补位。时间轴由各镜时长累加得到，
与配音时长天然对齐（tts 阶段已把 shot.dur 回填为真实音频时长）。

字幕语言（sub_cfg.lang，项目文档顶层 `subtitle_lang` 下发，建项目时定）：
  zh    中文（默认）——narration > caption；
  en    英文——narration_en > caption_en（缺英文位回落中文，不留空窗）；
  both  中英双语——中文主行在上、英文副行（0.62× 字号）贴底部安全区，
        两行栈式排布同屏显示（外语学习/出海双投的标准双字幕版式）。
双语完整支持 caption 底部模式；bubble/dialogue_box/centered/ranking 等演出型
模式版式空间有限：en 单语时文本位换用英文字段，both 时保持中文主行。
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import voicecast
from ..fonts import NOTOSERIF_SC_FAMILY, PUHUITI_MEDIUM_FAMILY, WENKAI_FAMILY

# 系统字体名 → 工程内置免费商用等价族名：profile 里写的系统字（如国风衬线的 Songti SC、
# 古风的 Kaiti）自动映射到内置思源宋体 / 霞鹜文楷，libass 经 compose 的 fontsdir 加载——
# 全链路免费商用 + 跨系统一致，profile 无需逐一改（也就不动 models.yaml/内嵌表触发漂移守卫）。
_FONT_ALIAS = {
    "songti sc": NOTOSERIF_SC_FAMILY, "songti": NOTOSERIF_SC_FAMILY,
    "stsong": NOTOSERIF_SC_FAMILY, "宋体": NOTOSERIF_SC_FAMILY,
    "kaiti sc": WENKAI_FAMILY, "kaiti": WENKAI_FAMILY,
    "stkaiti": WENKAI_FAMILY, "楷体": WENKAI_FAMILY,
}


def _norm_font(name: str) -> str:
    """字体名归一：系统衬线/楷体名 → 工程内置免费商用等价族名（大小写不敏感；未命中原样返回）。"""
    return _FONT_ALIAS.get((name or "").strip().lower(), name)


def _ass_time(t: float) -> str:
    # 先整体折算成厘秒再分解——秒位进位自然级联到分/时
    # （逐位取整再进位会在 59.999 处产出 "0:00:60.00" 非法时间码）
    total_cs = int(round(max(0.0, t) * 100))
    cs = total_cs % 100
    s = (total_cs // 100) % 60
    m = (total_cs // 6000) % 60
    h = total_cs // 360000
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


_PUNCT = "，。！？；、,.!?;："

# 字幕事件的可读性下限：单条 Dialogue 的最短停留时长与阅读速率上限
# （Netflix 简中 Timed Text 口径）。实测有声段是说话时长，短句需据此补足。
MIN_EVENT_SEC = 1.2
READ_CHARS_PER_SEC = 7.0


# ---------------- 断点禁则（中文排版「禁则处理」的最小可执行子集）----------------
# 中文没有词间空格，断点只能靠规则挑。四张表按「断在这里有多糟」分档，**行宽是硬的、
# 断点好看是软的**：合法区间里挑不出干净位置时照断不误，绝不为了不断词而让行超宽。
#
# 唯一的硬禁则是原子串——它挡的不是难看，是**读错**。

# 数字与拉丁连写单元：断进去等于改写内容。`豆瓣8.9分` 断在小数点上、点又随断行
# 退场，屏幕上就是「豆瓣8 / 9分」，观众读到的是 89 分。
_ATOM_RE = re.compile(r"[0-9]+(?:[.,:][0-9]+)*%?|[A-Za-z]+(?:['’\-][A-Za-z]+)*")

# 中文数字串与「数词 + 量词」：`一万二千` `十一点` `第一部` `一条` 拆开不改数值，
# 但要回读一遍才认得出来，按软禁则回避。量词只收高频闭集——认不出来就是不回避，
# 不做词典化（引擎里没有分词器，也不引一个：同一份文案在两台机器上必须断在同一处）。
_NUM_RE = re.compile(
    r"[〇零一二两三四五六七八九十百千万亿]+"
    r"[个位名只条张本部册页章节句段行卷台辆座层间家所件套双对副支块片面群批次回遍"
    r"步口头粒颗杯碗瓶盒包袋年月日天时点分秒岁周季米里尺寸斤吨克升度倍成元角号]?")

# 黏住后字、**不能留在行尾**的字，按黏得有多死分两档：
#   强黏——否定与程度副词、介词、数量指示前缀，它们后面必有词，单独收尾就是把词拆了；
#   弱黏——助动词、连词、范围副词，收尾读得通（「他不会 / 来」），只是不漂亮。
# 分两档是必要的：`会不会` 三个字里 `会` 和 `不` 都黏后字，一视同仁的话四个候选断点
# 全被同等罚分，平衡项会把刀正好落在最糟的 `会不 / 会` 上。
_BIND_STRONG = set("不没未无很最更太第每各某和与及跟在从把被对向往给让使由为")
_BIND_WEAK = set("都也就才又再还只会能要想可应并而但或若因如除自于这那哪几半本该同")
# 黏住前字、**不能落到行首**的字：结构助词与语气词（行首孤字）。
_ORPHAN = set("的地得了着过们吗呢吧呀啊嘛哦啦")
# 成对符号：外沿是天然断点（`《华氏451》` 之后、`《` 之前断），内沿反过来最忌。
_CLOSE_MARK, _OPEN_MARK = set("）】》」』〉〗］｝"), set("（【《「『〈〖［｛")
# 连接号：两侧都不能断（`雷·布拉德伯里` 是一个人名，`三年后·深夜` 是一个时间地点）。
_CONNECTOR = set("·・‧–—~～")

# 断点罚分（小者优先）。同分再比「离目标位置有多远」——即旧实现的「离中点最近」。
_P_PUNCT, _P_MARK, _P_CLEAN, _P_WEAK, _P_STRONG, _P_ATOM = 0, 1, 2, 4, 6, 100


def _atom_starts(text: str) -> dict[int, int]:
    """断点索引 → 该原子串起点。落进数字/拉丁连写单元内部的断点要么罚到最重
    （`_break_penalty`），要么退到单元起点（`_wrap_lines`），两处共用这一张表。"""
    out: dict[int, int] = {}
    for m in _ATOM_RE.finditer(text):
        for i in range(m.start() + 1, m.end()):
            out[i] = m.start()
    return out


def _soft_breaks(text: str) -> set[int]:
    """软禁则位：中文数字串 / 数词量词内部。"""
    out: set[int] = set()
    for m in _NUM_RE.finditer(text):
        out.update(range(m.start() + 1, m.end()))
    return out


def _break_penalty(text: str, i: int, hard: dict[int, int], soft: set[int]) -> int:
    """在 i 处断行有多糟（断点 i：line1 = text[:i]）。"""
    if i in hard:
        return _P_ATOM
    prev, nxt = text[i - 1], text[i]
    if prev in _CONNECTOR or nxt in _CONNECTOR:
        return _P_STRONG
    if prev in _PUNCT:                       # 句读处断行最自然（标点随断行退场）
        return _P_PUNCT
    if prev in _CLOSE_MARK or nxt in _OPEN_MARK or prev == " " or nxt == " ":
        return _P_MARK
    if nxt in _ORPHAN or prev in _BIND_STRONG or prev in _OPEN_MARK or nxt in _CLOSE_MARK:
        return _P_STRONG
    if prev in _BIND_WEAK or i in soft:
        return _P_WEAK
    return _P_CLEAN


def _pick_cut(text: str, lo: int, hi: int, target: int) -> tuple[int, int]:
    """在合法区间 `[lo, hi]` 里挑断点，返回 `(断点, 罚分)`。

    取 (罚分, 离 target 的距离, 靠前) 最小者——后两项即旧实现的「离中点最近、
    并列取靠前」。区间非空就一定挑得出断点：**禁则从不否决换行**，只排序。"""
    hard, soft = _atom_starts(text), _soft_breaks(text)
    best = min(range(lo, hi + 1),
               key=lambda i: (_break_penalty(text, i, hard, soft), abs(i - target), i))
    return best, _break_penalty(text, best, hard, soft)


def _split_cjk(text: str, max_chars: int, max_lines: int) -> tuple[list[str], bool]:
    """把 text 切成 ≤max_lines 行、每行 ≤max_chars（不丢字）。
    返回 `(行, 是否断进了原子串)`——后者让 `_wrap` 知道该不该多折一行换回来。

    逐行推进而非递归：行数由文案长度除以行宽得来，没有上界，递归会把「一条超长
    旁白」变成栈深度。最后一行放不下时原样留下（此时已无行可折）。"""
    lines: list[str] = []
    forced = False
    left = max_lines
    while len(text) > max_chars and left > 1:
        n = len(text)
        lo, hi = max(1, n - (left - 1) * max_chars), min(max_chars, n - 1)
        if lo > hi:                               # 行容量不足（超宽原子串）→ 等分兜底
            lo = hi = max(1, min(n - 1, n // left))
        cut, penalty = _pick_cut(text, lo, hi, n // left)
        forced = forced or penalty >= _P_ATOM
        head = text[:cut].rstrip(" " + _PUNCT)
        if head:
            lines.append(head)
        text = text[cut:].lstrip(" " + _PUNCT)
        left -= 1
    if text:
        lines.append(text)
    return lines, forced


def _wrap(text: str, max_chars: int = 16) -> str:
    """竖屏窄屏折行（Netflix 简中口径）：**每行恒 ≤max_chars**（\\N 为 ASS 换行）。

    断点在「各行都不超限」的区间内按罚分挑（见 `_break_penalty`）：优先句读，
    断点处标点随断行退场（行尾不悬、行首不孤）；不断进数字与拉丁连写单元。

    行数按需：两行放得下就是两行（正常情形），放不下折第三行、以此类推。
    **不留超宽行**——`_caption_header` 写的是 `WrapStyle: 2`（只认 \\N），
    超宽行 libass 不会替你折，它会径直越过安全边距跑出画面。行数是文案长度
    与画风字号共同决定的：一条 25 字旁白配 80 号字（每行 11 字）本就是三行。
    行数放不下原子串时也宁可再多折一行——`8.9` 断成两行是读错，多一行只是占版面。"""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    base = max(2, -(-len(text) // max_chars))
    lines, forced = _split_cjk(text, max_chars, base)
    widest = max((m.end() - m.start() for m in _ATOM_RE.finditer(text)), default=0)
    if forced and widest <= max_chars:            # 原子串本身超宽时换不回来，不白试
        for extra in (1, 2):
            alt, alt_forced = _split_cjk(text, max_chars, base + extra)
            if not alt_forced:
                lines = alt
                break
    return "\\N".join(lines)


def _wrap_main(text: str, lang: str, zh_max: int, en_max: int) -> str:
    """主行折行：**按哪把尺折由文本自己决定**——en 单语且整行无 CJK 时按词折
    （`_wrap_en`），否则按全角折（`_wrap`）。

    caption 与演出型模式的旁白回退共用这一个判据。两处各写一份的后果是气泡模式的
    英文旁白落进全角折行器：全角尺按 1em 推行宽，同一条边距下英文能放两倍字符，
    于是按中文行宽把英文腰斩，`_wrap` 的原子串禁则也只能保住不超过行宽的那些词。"""
    return (_wrap_en(text, en_max) if lang == "en" and not _has_cjk(text)
            else _wrap(re.sub(r"\s+", " ", text), zh_max))


def _wrap_en(text: str, max_chars: int = 42) -> str:
    """英文按词折行（≤max_chars/行、至多两行）：断点取不超限的最靠中词界。
    超两行容量时第二行以词界截断加省略号——英文不做逐字符硬切。"""
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) <= max_chars:
        return text
    words = text.split(" ")
    line1, i = "", 0
    while i < len(words):
        cand = (line1 + " " + words[i]).strip()
        if len(cand) > max_chars and line1:
            break
        line1 = cand
        i += 1
    rest = " ".join(words[i:])
    if len(rest) > max_chars:                      # 超两行容量 → 词界截断
        cut = rest.rfind(" ", 0, max_chars - 1)
        rest = rest[:cut if cut > 0 else max_chars - 1] + "…"
    return f"{line1}\\N{rest}" if rest else line1


# 剥语音标签的实现在 voicecast：字幕取文本与 `line_spans` 算字数权重要用同一把尺，
# 而 voicecast 在依赖链下游（本模块 import 它，反向不成立）。此处转出保持既有调用点。
strip_voice_tags = voicecast.strip_voice_tags


def sub_cfg(store, project, prof=None) -> dict:
    """字幕样式解析：profile 的画风样式打底，项目文档 subtitle 块覆盖——
    「语录居中/游戏气泡」这类单项目诉求不必动配置文件。
    字幕语言：项目文档顶层 `subtitle_lang`（建项目时定，zh/en/both）下发为
    cfg.lang；subtitle 块显式写 lang 时以块为准（章节级微调口）。

    烧录（cli 各合成段）、交付 SRT（deliver）与 Studio 导出共用这一份判据——
    语言口径各读各的话，块里写 en 的项目会烧英文、外挂 SRT 却出中文。"""
    cfg = dict((store.profile(prof or project.profile) or {}).get("subtitle") or {})
    doc = project.data.get("subtitle") or {}
    cfg.update(doc)
    cfg.setdefault("lang", project.data.get("subtitle_lang") or "zh")
    # 记下字号的出处：合并后画风字号与作者字号长得一样，而横屏缺省要盖过前者、
    # 绝不盖过后者（判定在 `resolve_size`，那里才知道画布横竖）
    if "size" in cfg and "size" not in doc:
        cfg[PROFILE_SIZE_KEY] = True
    return cfg


def pick_texts(shot: dict, lang: str) -> tuple[str, str]:
    """按字幕语言取（主行, 副行）文本。有旁白的镜**字幕逐字取 narration**
    （音字一致铁律：配音念什么字幕就写什么），caption 仅在无旁白镜补位；
    英文位同理 narration_en>caption_en；en 缺英文回落中文（宁可有字不留空窗）。
    取用时剥离 `<cot …>` 等语音标签（只给 TTS，不该出现在画面上）。"""
    zh = strip_voice_tags(shot.get("narration") or shot.get("caption") or "")
    en = strip_voice_tags(shot.get("narration_en") or shot.get("caption_en") or "")
    if lang == "en":
        return (en or zh), ""
    if lang == "both":
        return zh, en
    return zh, ""


def _en_view(shot: dict) -> dict:
    """en 单语时给演出型模式（气泡/对话框/居中/榜单）用的英文视图：
    narration/caption 换用英文位（缺则保留中文），其余字段原样。
    多角色镜同理逐句换位（lines[].text_en → text），缺英文位保留中文。"""
    out = dict(shot)
    if (shot.get("narration_en") or "").strip():
        out["narration"] = strip_voice_tags(shot["narration_en"])
    if (shot.get("caption_en") or "").strip():
        out["caption"] = strip_voice_tags(shot["caption_en"])
    if isinstance(shot.get("lines"), list):
        out["lines"] = [
            ({**d, "text": strip_voice_tags(d["text_en"])}
             if isinstance(d, dict) and str(d.get("text_en") or "").strip() else d)
            for d in shot["lines"]]
    return out


def shot_events(shot: dict, start: float, end: float,
                lang: str = "zh",
                spans: list[tuple[float, float]] | None = None
                ) -> list[tuple[float, float, str, str, str]]:
    """本镜的字幕事件序列 `[(起, 止, 主行, 副行, 说话人), …]`——**全部版式的共用入口**。

    为什么要有这一层：一个镜头里两个人对话时（`shots[].lines[]`），配音是逐句换声音的，
    字幕却还按「整镜一条 Dialogue」渲染的话，三句话会挤成一条、横跨整镜时长——
    声音已经换人了字幕还停在上一句，比不换声音更出戏。故有 lines 的镜**逐句一条事件**，
    按各句实测时长（`lines[].dur`，tts 回填）在镜窗口内切分。

    `spans` = 片段音轨里实测的有声段落（相对镜起点，`pipeline.speech.speech_windows`）。
    在场时字幕落进这几段，缺省则铺满镜窗口——native 片段常有数秒的开口前置量，
    铺满窗口即等长的字幕提前量。

    三条纪律：
      · **没有 lines 的镜走 `pick_texts` 原路**（含 caption 补位、en 回落中文），
        逐字节保持既有行为——绝大多数镜是单段，不该为多角色能力付回归代价；
      · 未跑 tts（`lines[].dur` 缺失，scored 与 native 恒如此）时按**字数比例**
        切可用窗口，切分走 `voicecast.line_spans`——那也是提示词台词时间轴与
        音频剧本秒段的实现，三处共用一把尺才不会出现「模型按第 3.1 秒换人、
        字幕在第 6 秒换人」；
      · 句数与实测段数对不上时只取整体首尾（首段起点到末段止点），不把第 N 句
        塞进第 N 段——段与句的对应关系此时不成立，逐句对位的误差大于铺满窗口。"""
    lines = shot.get("lines")
    norm = voicecast.shot_lines(shot) if isinstance(lines, list) and lines else []
    win = _speech_window(start, end, spans)
    if not norm:
        main, sub = pick_texts(shot, lang)
        if not (main or sub):
            return []
        ts, te = _readable(win[0], win[1], main or sub, float(start), float(end))
        return [(ts, te, main, sub, str(shot.get("speaker") or ""))]
    if spans and len(spans) == len(norm):
        # 句数与实测段数一致：起止直接取对应段，不再按时长比例推算。
        # 可读性补足逐句以「上一句终点」为下界、「下一段起点」为上界：各自补足会让
        # 两条挨得近的短句互相越界，同屏出现两条字幕。
        out: list[tuple[float, float, str, str, str]] = []
        base, cursor = float(start), float(start)
        for i, (ln, (a, b)) in enumerate(zip(norm, spans)):
            main, sub = _line_texts(ln, lang)
            if not (main or sub):
                continue
            hi = base + spans[i + 1][0] if i + 1 < len(spans) else float(end)
            ts, te = _readable(base + a, base + b, main or sub,
                               max(cursor, float(start)), max(hi, cursor))
            out.append((ts, te, main, sub, str(ln.get("speaker") or "")))
            cursor = te
        return out
    start, end = win
    span = max(float(end) - float(start), 0.0)
    durs = [float(ln.get("dur") or 0.0) for ln in norm]
    total = sum(durs)
    if total <= 0:                      # 没跑过 tts：按字数比例切（见上）
        cut = voicecast.line_spans(norm, span)
        # 零长窗口（dur 缺失/为 0）时 line_spans 返回空，逐句给零长事件：
        # 条数是 verify 字幕体检的下限，不能因为窗口塌了就整镜不出字幕
        durs = [b - a for _ln, a, b in cut] if cut else [0.0] * len(norm)
    else:                               # 按实测比例铺满镜窗口（tts 后 total≈span，比例即等分）
        durs = [d * span / total for d in durs]
    out = []
    t = float(start)
    for ln, d in zip(norm, durs):
        main, sub = _line_texts(ln, lang)
        if main or sub:
            out.append((round(t, 3), round(min(t + d, float(end)), 3),
                        main, sub, str(ln.get("speaker") or "")))
        t += d
    return out


def _line_texts(line: dict, lang: str) -> tuple[str, str]:
    """一句台词按字幕语言取 `(主行, 副行)`——en 缺英文时回落中文，both 出双行。"""
    zh = strip_voice_tags(line.get("text") or "")
    en = strip_voice_tags(line.get("text_en") or "")
    if lang == "en":
        return (en or zh), ""
    if lang == "both":
        return zh, en
    return zh, ""


def _readable(a: float, b: float, text: str,
              lo: float, hi: float) -> tuple[float, float]:
    """把字幕事件补足到可读时长，且不越出镜窗口 `[lo, hi]`。

    实测有声段是说话时长而非阅读时长，短句据此出的事件短于可读下限。按 Netflix
    简中的最短事件时长与阅读速率补足，优先向后延（说完后画面仍在），后方不足再向前借。
    """
    need = max(MIN_EVENT_SEC, len(text.replace("\\N", "")) / READ_CHARS_PER_SEC)
    if b - a >= need or hi - lo <= need:
        return round(max(a, lo), 3), round(min(b, hi), 3)
    b = min(a + need, hi)
    a = max(b - need, lo)
    return round(a, 3), round(b, 3)


def _speech_window(start: float, end: float,
                   spans: list[tuple[float, float]] | None) -> tuple[float, float]:
    """本镜字幕可用的时间窗：有实测有声段落取其整体首尾，否则取整个镜窗口。

    取整体首尾而非逐段，是因为本窗口服务于「未能逐句对上」的路径——段与句的对应
    关系此时不成立，只有说话的起止边界可信。"""
    if not spans:
        return float(start), float(end)
    a = float(start) + min(x for x, _ in spans)
    b = float(start) + max(y for _, y in spans)
    return round(max(a, float(start)), 3), round(min(b, float(end)), 3)


# 底部字幕（caption）的样式参数——全部可被 profile 的 subtitle 配置覆盖，
# 缺省即传统白字黑边（Netflix 简中规范的中性样式），画风化只是换装不换机制。
_CAPTION_DEFAULTS = {
    "font": PUHUITI_MEDIUM_FAMILY,  # 工程内置阿里普惠体中黑（免费商用·随仓库分发跨系统一致）——
                                    # libass 经 compose 传入的 fontsdir 加载；画风可覆盖 font
    "size": 58,                  # 字号（1080 宽基准·横屏/方形缺省；竖屏缺省见 default_size）
    "text_color": "#ffffff",
    "outline_color": "#202020",
    "outline": 4,                # 描边宽
    "shadow": 1,                 # 投影
    "bold": 0,                   # 普惠体 Medium 已够份量，不再叠 faux-bold（faux-bold 小字会糊）
    "spacing": 0,                # 字距
    "margin_v": 360,             # 竖屏(9:16)距底边距（避让平台底部 UI）；横屏/方形自动降为贴底，见 _default_margin_v
    "accent": "#ffd45e",         # 说话人名字色（speaker_tag 开启时用）
    "speaker_tag": False,        # 台词前缀「名字」（对白型画风建议开）
}


# 竖屏(9:16)字幕缺省字号：竖屏每行只有约 16 个全角字、观看多为手机竖持，
# 横屏基准字号在竖屏实际观感偏小，缺省抬大一档。显式配置（画风 subtitle.size /
# 章节覆盖）恒优先，此值只在无人表态时兜底——与 margin_v 的横竖分治同一条纪律。
PORTRAIT_SIZE = 80

# 横屏(16:9)字幕缺省字号。**这一档不是审美偏好，是画布宽度修正**：
# 9:16 与 1:1 的画布都是 1080 宽，而 16:9 是 **1920 宽**（config/models.yaml 的
# canvas 表），于是 `_CAPTION_DEFAULTS["size"]` 那句「1080 宽基准」从来就没适用于
# 横屏——同一个字号在横屏上只有一半的相对高度，实测明显偏小。
LANDSCAPE_SIZE = 66

# `sub_cfg` 用它标记「`size` 这个值来自画风 profile、不是作者表的态」。
# 横屏缺省要盖过画风字号，但绝不能盖过作者在章节 `subtitle` 块里写下的字号，
# 两者在合并后的 cfg 里长得一模一样，故合并时就把出处记下来。
# 直接构造 opts 的调用方（测试、内部直调）不带这个键 → 视为明确指定，照旧生效。
PROFILE_SIZE_KEY = "_size_from_profile"


def default_size(canvas_w: int, canvas_h: int) -> int:
    """底部字幕缺省字号（画布自适应）：竖屏 PORTRAIT_SIZE / 横屏 LANDSCAPE_SIZE /
    方形取基准值。**无人表态时**的兜底，作者显式字号恒优先（见 `resolve_size`）。"""
    if canvas_h > canvas_w:
        return PORTRAIT_SIZE
    if canvas_w > canvas_h:
        return LANDSCAPE_SIZE
    return int(_CAPTION_DEFAULTS["size"])


def resolve_size(opts: dict | None, canvas_w: int, canvas_h: int) -> int:
    """本次真正生效的底部字幕字号——**烧录与 Studio 样式面板共用这一个出口**。

    优先级：作者在项目/章节 `subtitle` 块写的字号 > 画布缺省 > 画风 profile 字号。

    横屏对画风字号是**硬覆盖**：profile 里那批 52~62 与常量 58 都是照 1080 宽基准
    写的，落到 1920 宽的 16:9 画布上一律偏小；作者没在章节里表过态时按画布缺省抬到
    `LANDSCAPE_SIZE`。竖屏与方屏画布本就是 1080 宽，画风字号照旧逐个生效。

    两处判据必须走同一个函数：面板显示的生效值与真烧出来的不一致时，用户会照着
    面板上那个数去调，而调的是另一个东西。"""
    opts = opts or {}
    size = opts.get("size")
    if size is not None and not opts.get(PROFILE_SIZE_KEY):
        return int(size)                       # 作者/调用方明确指定
    if canvas_w > canvas_h:
        return LANDSCAPE_SIZE                  # 横屏：画布宽度修正盖过画风字号
    return int(size) if size is not None else default_size(canvas_w, canvas_h)


def _default_margin_v(canvas_w: int, canvas_h: int, size: int | None = None) -> int:
    """底部字幕距底边距的画布自适应默认（未在 profile 显式配 margin_v 时用）：
    **横竖屏逻辑不同**——
      竖屏(9:16)：底部 20~25% 是平台 UI 区（点赞/评论/关注），字幕必须上抬避让 → 360；
      横屏(16:9)/方形(1:1)：无平台底部 UI 遮挡，字幕**贴底**，下方只留「约一个字高」的
      呼吸缝（= 字号 size，size=58 → 58px）。这样横屏字幕真正落到底部安全区，
      也给未来左下角「特殊字幕」腾出同一底带（见 build_from_timeline 的 corner_note）。
    与字号绑定而非画布比例：画风字号 50~62 时缝隙同步缩放，观感恒为「一个字」。"""
    if canvas_h > canvas_w:                     # 竖屏：底部是平台 UI 区，上抬避让
        return _CAPTION_DEFAULTS["margin_v"]     # 360
    return round(size or _CAPTION_DEFAULTS["size"])   # 横屏/方形：贴底，下留一个字高（58）


def _cjk_max_chars(canvas_w: int, size: int, margin: int | None = None) -> int:
    """按**画布宽 + 字号**推每行全角字数上限（横竖屏自适应换行的核心）：
    左右各留一个字宽的安全边，其余按字宽整除——全角 CJK 字宽≈字号(1em)。
      竖屏 1080 宽 / 58 → (1080-116)//58 = 16；
      横屏 1920 宽 / 58 → (1920-116)//58 = 31（不会像竖屏那样十几字就换行）。
    即「文字要真的超出画面宽度（减去左右各一字余量）才换行」。

    `margin` 是左右安全边的**另行指定**（缺省=一个字宽）：角标那类行内改字号
    （`\\fs`）的元素，安全边仍由样式表的 MarginL/R 按主字号定，字宽却是它自己的
    字号——两个数必须分开传，否则按主字号算宽度会白白多折一行。"""
    return max(8, (canvas_w - 2 * (size if margin is None else margin)) // size)


def _latin_max_chars(canvas_w: int, size: int) -> int:
    """英文/拉丁每行字符上限：拉丁字宽≈半个全角，故≈2×全角上限。
    以全角上限锚定并**取 max(下限, 计算值)**——竖屏落在下限（34/42），
    横屏才放宽（→64），只放长不缩短。"""
    return 2 * _cjk_max_chars(canvas_w, size) + 2


def _style_color(hexstr: str, alpha: int = 0) -> str:
    h = hexstr.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{max(0, min(255, alpha)):02X}{b}{g}{r}".upper()


def _caption_header(canvas_w: int, canvas_h: int, st: dict, *,
                    bilingual: bool = False) -> str:
    # 双语＝单条 Dialogue 内嵌 {\rDefaultEn} 行内切换样式（中文主行在上、
    # 英文副行在下，整块底对齐 margin_v）——两条独立事件会触发 libass 碰撞
    # 避让把副行顶到主行上方，单事件版式恒定，这是双语 ASS 的标准做法。
    size_en = max(28, round(st["size"] * 0.62))
    zh_margin = st["margin_v"]
    # 左右安全边 = 一个字宽（=字号）——与换行 _cjk_max_chars 的「左右各留一字」
    # 同源：横屏满行(≈31 全角)不越过标称安全框（固定 80px 边距只合 1.4 字，满行会侵入）。
    margin_h = int(st["size"])
    en_style = (
        f"Style: DefaultEn,{st['font']},{size_en},"
        f"{_style_color(st['text_color'])},{_style_color(st['text_color'])},"
        f"{_style_color(st['outline_color'])},&H64000000,"
        f"0,0,0,0,100,100,0,0,1,"
        f"{max(2, int(st['outline']) - 1)},{st['shadow']},2,{margin_h},{margin_h},{st['margin_v']},1\n"
    ) if bilingual else ""
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\n"
        f"PlayResY: {canvas_h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{st['font']},{st['size']},"
        f"{_style_color(st['text_color'])},{_style_color(st['text_color'])},"
        f"{_style_color(st['outline_color'])},&H64000000,"
        f"{1 if st['bold'] else 0},0,0,0,100,100,{st['spacing']},0,1,"
        f"{st['outline']},{st['shadow']},2,{margin_h},{margin_h},{zh_margin},1\n"
        + en_style +
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )


def build_from_timeline(timeline, out_path, *, canvas_w=1080, canvas_h=1920,
                        opts=None, spans_of=None) -> str:
    """timeline: [(start, end, shot), ...] → 底部字幕 ASS（样式随画风配置换装）。

    opts.lang（zh/en/both）决定文本位与版式：both 出中文主行 + 英文副行两条
    Dialogue（Default/DefaultEn 双样式栈式排布）；en 单行英文（缺英文回落中文）。"""
    st = {**_CAPTION_DEFAULTS, **(opts or {})}
    st["font"] = _norm_font(st["font"])          # 系统衬线/楷体名 → 内置免费商用族名
    st["size"] = resolve_size(opts, canvas_w, canvas_h)   # 见 resolve_size 的优先级
    size = int(st["size"])
    if "margin_v" not in (opts or {}):          # 未显式配 → 横竖屏自适应贴底（见 _default_margin_v）
        st["margin_v"] = _default_margin_v(canvas_w, canvas_h, size)
    zh_max = _cjk_max_chars(canvas_w, size)      # 每行全角字数：横屏≈31、竖屏=16
    # 英文每行字符：只放长不缩短——竖屏落在下限(单行34/both副行42)，横屏才放宽(→64)
    en_max = max(34, _latin_max_chars(canvas_w, size))       # en 单语整行（全字号）
    sub_en_max = max(42, _latin_max_chars(canvas_w, size))   # both 英文副行（0.62× 字号，容得更多）
    lang = st.get("lang") or "zh"
    lines = [_caption_header(canvas_w, canvas_h, st, bilingual=(lang == "both"))]
    for start, end, shot in timeline:
        # 逐句展开：多角色镜一句一条 Dialogue（跟着声音换人），单段镜恒是一条、
        # 起止即整镜窗口（见 shot_events）
        for ts_f, te_f, main, sub, spk in shot_events(
                shot, start, end, lang, spans_of(shot) if spans_of else None):
            if not main and not sub:
                continue
            ts, te = _ass_time(ts_f), _ass_time(te_f)
            wrapped = ""
            if main:
                wrapped = _wrap_main(main, lang, zh_max, en_max)
                if spk and st.get("speaker_tag") and lang != "en":
                    wrapped = (f"{{\\1c{_ass_color(st['accent'])}}}「{spk}」"
                               f"{{\\1c{_ass_color(st['text_color'])}}}{wrapped}")
            if sub:   # 英文副行（both）：与中文合并为一个事件，行内 \r 切样式
                wrapped = (f"{wrapped}\\N{{\\rDefaultEn}}{_wrap_en(sub, sub_en_max)}"
                           if wrapped else f"{{\\rDefaultEn}}{_wrap_en(sub, sub_en_max)}")
            if wrapped:
                lines.append(f"Dialogue: 0,{ts},{te},Default,,0,0,0,,{wrapped}")
        ts, te = _ass_time(start), _ass_time(end)
        # 左下角「特殊字幕」（可选·预留）：注释/署名/位置提示等——**字小一号**（0.8×）+
        # 左下对齐(\an1)，与居中主字幕横向错开、共享同一底带。字小 + 居中主字幕通常较
        # 短 → 不重合；横屏满行(~31 全角)的居中主字幕左缘可能触及此角，故建议用于旁白
        # 短/无旁白的镜。默认关闭：仅当分镜显式写了 shots[].corner_note 才渲染，缺省零影响。
        note = (shot.get("corner_note") or "").strip()
        if note:
            fs = max(20, round(size * 0.8))
            bord = max(2, int(st["outline"]) - 1)
            # 折行按角标**自己的字号**算：它比主字幕小一号，同一条边距里放得下更多字。
            # 按 zh_max（主字号推的）折就是凭空少放两三个字，短角标也被拦腰断开。
            note_max = _cjk_max_chars(canvas_w, fs, margin=size)
            lines.append(f"Dialogue: 0,{ts},{te},Default,,0,0,0,,"
                         f"{{\\an1\\fs{fs}\\bord{bord}}}{_wrap(note, note_max)}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out_path)


def _has_cjk(text: str) -> bool:
    return any(ord(c) > 0x2E7F for c in text)


# ---------------- 游戏对话框（JRPG 文本框）----------------
def _ass_color(hexstr: str) -> str:
    h = hexstr.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{b}{g}{r}&".upper()


def _ass_alpha(a) -> str:
    return f"&H{max(0, min(255, int(a))):02X}&"


def _wrap_lines(text: str, max_chars: int, max_lines: int) -> str:
    """框体版式（气泡/对话框/居中大字）的定宽折行：满行即断、标点悬挂、超行省略号。

    与 `_wrap` 的区别是这里的行宽由框体几何定死，故按字数硬切；但**原子串同样不切**
    （`_atom_starts`）——`8.9` 在气泡里断成两行和在底部字幕里断成两行一样是读错。
    退到原子串起点会让该行少一两个字，框体宽度按最长行算、恒跟得上。
    **起点落在本行内才退**：原子串比行宽还长时起点在行首之前，退无可退，照原位
    硬切——再退就是每行一个字符，整条塌成逐字一行加省略号。"""
    text = re.sub(r"\s+", " ", text.strip())
    atoms = _atom_starts(text)
    lines, i = [], 0
    while i < len(text) and len(lines) < max_lines:
        end = min(i + max_chars, len(text))
        start = atoms.get(end)
        if start is not None and start > i:           # 断点落进原子串 → 退到其起点
            end = start
        chunk = text[i:end]
        i += len(chunk)
        while i < len(text) and text[i] in _PUNCT:   # 标点悬挂：句读不孤行
            chunk += text[i]
            i += 1
        lines.append(chunk)
    if i < len(text) and lines:                       # 溢出用省略号收尾
        lines[-1] = lines[-1][:-1] + "…"
    return "\\N".join(lines)


def _bevel(w: int, h: int, r: int) -> str:
    """带切角的矩形绘制路径（JRPG 框体质感）。"""
    return (f"m {r} 0 l {w - r} 0 l {w} {r} l {w} {h - r} "
            f"l {w - r} {h} l {r} {h} l 0 {h - r} l 0 {r}")


def _dialogue_header(cw, ch, name_c, text_c) -> str:
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
           "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
           "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
           "MarginL, MarginR, MarginV, Encoding")
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {cw}\nPlayResY: {ch}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n" + fmt + "\n"
        "Style: AVBox,Alibaba PuHuiTi 3.0 65 Medium,40,&H00FFFFFF,&H00FFFFFF,&H00202020,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
        f"Style: AVName,Alibaba PuHuiTi 3.0 65 Medium,46,{name_c},{name_c},&H00201008,&H64000000,"
        "1,0,0,0,100,100,0.5,0,1,2,0,7,0,0,0,1\n"
        f"Style: AVText,Alibaba PuHuiTi 3.0 65 Medium,54,{text_c},{text_c},&H00201510,&H64000000,"
        "0,0,0,0,100,100,0.3,0,1,2,0,7,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )


def build_dialogue_box(timeline, out_path, *, canvas_w=1080, canvas_h=1920, opts=None,
                       spans_of=None) -> str:
    """游戏对话框风格字幕：框体 + 说话人名牌 + 台词 + ▼。样式全参数化，可微调成其他风格。

    每镜文本取 dialogue > narration > caption；说话人取 shots[].speaker。
    """
    opts = opts or {}
    box_fill = _ass_color(opts.get("box", "#161018"))
    box_alpha = _ass_alpha(opts.get("box_alpha", 40))
    border = _ass_color(opts.get("border", "#e8c979"))
    name_c = _ass_color(opts.get("name_color", "#ffd45e"))
    text_c = _ass_color(opts.get("text_color", "#f6f1e6"))

    mx = 60
    bx, bw, bh = mx, canvas_w - 2 * mx, 300
    by = canvas_h - bh - 120
    bev = 18

    out = [_dialogue_header(canvas_w, canvas_h, name_c, text_c)]
    timeline = expand_timeline(timeline, spans_of)   # 多角色镜逐句展开（名牌/归属跟着换人）
    for start, end, shot in timeline:
        text = strip_voice_tags(shot.get("dialogue") or shot.get("narration")
                                or shot.get("caption") or "")
        if not text:
            continue
        st, et = _ass_time(start), _ass_time(end)
        speaker = (shot.get("speaker") or "").strip()

        # 框体
        out.append(
            f"Dialogue: 0,{st},{et},AVBox,,0,0,0,,{{\\pos({bx},{by})\\1c{box_fill}"
            f"\\1a{box_alpha}\\bord3\\3c{border}\\p1}}{_bevel(bw, bh, bev)}")
        # 说话人名牌 + 名字
        if speaker:
            pw, ph = 44 + len(speaker) * 48, 64
            px, py = bx + 30, by - ph + 10
            out.append(
                f"Dialogue: 1,{st},{et},AVBox,,0,0,0,,{{\\pos({px},{py})\\1c{box_fill}"
                f"\\1a&H10&\\bord2\\3c{border}\\p1}}{_bevel(pw, ph, 12)}")
            out.append(
                f"Dialogue: 2,{st},{et},AVName,,0,0,0,,{{\\pos({px + 26},{py + 8})}}{speaker}")
        # 台词
        wrapped = _wrap_lines(text, 18, 3)
        out.append(
            f"Dialogue: 2,{st},{et},AVText,,0,0,0,,{{\\pos({bx + 46},{by + 66})}}{wrapped}")
        # ▼ 指示符
        out.append(
            f"Dialogue: 3,{st},{et},AVText,,0,0,0,,{{\\pos({bx + bw - 66},{by + bh - 70})\\fs44}}▼")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(out_path)


# ---------------- 语录/励志（居中大字）----------------
def build_centered(timeline, out_path, *, canvas_w=1080, canvas_h=1920, opts=None,
                   spans_of=None) -> str:
    """居中大字：适合语录/励志/治愈。文本取 dialogue>narration>caption，署名取 shots[].attribution。"""
    opts = opts or {}
    text_c = _ass_color(opts.get("text_color", "#ffffff"))
    accent = _ass_color(opts.get("accent", "#ffd45e"))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\nPlayResY: {canvas_h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: QText,Alibaba PuHuiTi 3.0 65 Medium,80,{text_c},{text_c},&H00202020,&H96000000,1,0,0,0,"
        "100,100,1,0,1,3,2,5,80,80,0,1\n"
        f"Style: QAttr,Alibaba PuHuiTi 3.0 65 Medium,46,{accent},{accent},&H00202020,&H96000000,0,1,0,0,"
        "100,100,0,0,1,2,1,5,80,80,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out = [header]
    cy = int(canvas_h * 0.42)
    timeline = expand_timeline(timeline, spans_of)   # 多角色镜逐句展开（名牌/归属跟着换人）
    for start, end, shot in timeline:
        text = strip_voice_tags(shot.get("dialogue") or shot.get("narration")
                                or shot.get("caption") or "")
        if not text:
            continue
        st, et = _ass_time(start), _ass_time(end)
        out.append(f"Dialogue: 0,{st},{et},QText,,0,0,0,,{{\\pos({canvas_w//2},{cy})}}"
                   + _wrap_lines(text, 13, 4))
        attr = (shot.get("attribution") or "").strip()
        if attr:
            out.append(f"Dialogue: 0,{st},{et},QAttr,,0,0,0,,{{\\pos({canvas_w//2},{cy + 240})}}— {attr}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(out_path)


def expand_timeline(timeline, spans_of=None):
    """把镜级时间轴**逐句展开**成 `[(起, 止, 镜视图), …]`——演出型版式的统一入口。

    多角色镜（`shots[].lines[]`）展开成每句一项，句文本覆写进视图的
    `narration`/`dialogue`、说话人覆写进 `speaker`；单段镜原样透传（同一个 dict 对象，
    零拷贝零行为变化）。视图里 `lines` 置空防二次展开。

    为什么走"改时间轴"而不是"改各版式的循环体"：对话框/气泡/居中三个版式的循环体
    形态各异（名牌、气泡尖角、署名各有各的算法），逐个塞进一层嵌套循环既要重排缩进
    又容易改漏；把"一个镜可能是好几句"这件事在**进循环之前**就摊平，各版式只需在
    开头加一行 `timeline = expand_timeline(timeline)`，它们眼里的世界依旧是「一项一句」。"""
    out = []
    for start, end, shot in timeline:
        spans = spans_of(shot) if spans_of else None
        norm = (voicecast.shot_lines(shot)
                if isinstance(shot.get("lines"), list) and shot["lines"] else [])
        if len(norm) <= 1:
            out.append((*_speech_window(start, end, spans), shot))
            continue
        for a, b, main, _sub, spk in shot_events(shot, start, end, "zh", spans):
            out.append((a, b, {**shot, "narration": main, "dialogue": main,
                               "speaker": spk, "lines": None}))
    return out


# ---------------- 榜单/盘点（序号徽章）----------------
def build_ranking(timeline, out_path, *, canvas_w=1080, canvas_h=1920, opts=None,
                  spans_of=None) -> str:
    """榜单：左上角序号徽章 + 条目标题 + 底部说明。序号取 shots[].rank，标题取 title/speaker。"""
    opts = opts or {}
    badge = _ass_color(opts.get("badge", "#ff4d4f"))
    num_c = _ass_color(opts.get("num_color", "#ffffff"))
    title_c = _ass_color(opts.get("title_color", "#ffffff"))
    text_c = _ass_color(opts.get("text_color", "#f4f4f4"))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\nPlayResY: {canvas_h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: RBadge,Alibaba PuHuiTi 3.0 65 Medium,40,&H00FFFFFF,&H00FFFFFF,&H00202020,&H00000000,0,0,0,0,"
        "100,100,0,0,1,0,0,7,0,0,0,1\n"
        f"Style: RNum,Alibaba PuHuiTi 3.0 65 Medium,96,{num_c},{num_c},&H00202020,&H64000000,1,0,0,0,"
        "100,100,0,0,1,2,0,7,0,0,0,1\n"
        f"Style: RTitle,Alibaba PuHuiTi 3.0 65 Medium,64,{title_c},{title_c},&H00181818,&H64000000,1,0,0,0,"
        "100,100,0.5,0,1,3,1,7,0,0,0,1\n"
        f"Style: RText,Alibaba PuHuiTi 3.0 65 Medium,52,{text_c},{text_c},&H00181818,&H64000000,0,0,0,0,"
        "100,100,0,0,1,3,1,2,90,90,300,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out = [header]
    for start, end, shot in timeline:
        st, et = _ass_time(start), _ass_time(end)
        rank = str(shot.get("rank", "")).strip()
        if rank:
            bw, bh, bx, by = 150, 150, 70, 150
            out.append(f"Dialogue: 0,{st},{et},RBadge,,0,0,0,,{{\\pos({bx},{by})\\1c{badge}"
                       f"\\1a&H10&\\bord0\\p1}}{_bevel(bw, bh, 20)}")
            out.append(f"Dialogue: 1,{st},{et},RNum,,0,0,0,,{{\\an5\\pos({bx + bw//2},{by + bh//2})}}{rank}")
        title = (shot.get("title") or shot.get("speaker") or "").strip()
        if title:
            out.append(f"Dialogue: 1,{st},{et},RTitle,,0,0,0,,{{\\pos({70 + (170 if rank else 0)},{185})}}{title}")
        text = strip_voice_tags(shot.get("caption") or shot.get("narration") or "")
        if text:
            # 说明文本是这一镜念出来的那句话，跟实测语音走；徽章、序号与标题是常驻
            # 叠加层，交代的是「这是第几名」，恒铺满整镜
            ts, te = _speech_window(start, end, spans_of(shot) if spans_of else None)
            ts, te = _readable(ts, te, text, float(start), float(end))
            out.append(f"Dialogue: 1,{_ass_time(ts)},{_ass_time(te)},RText,,0,0,0,,"
                       f"{_wrap(re.sub(r'\s+', ' ', text))}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(out_path)


# ---------------- 头顶对话气泡（漫画/游戏演出）----------------
def _bubble_path(w: int, h: int, r: int, tx: int) -> str:
    """圆角气泡 + 底边下指尾巴的绘制路径（贝塞尔圆角；尾巴指向下方的说话人）。"""
    tw, th = 46, 40
    tx = max(r + 8, min(w - r - tw - 8, tx))
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} "
            f"l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {tx + tw} {h} l {tx + tw // 3} {h + th} l {tx} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} "
            f"l 0 {r} b 0 0 0 0 {r} 0")


def build_bubble(timeline, out_path, *, canvas_w=1080, canvas_h=1920, opts=None,
                 spans_of=None) -> str:
    """头顶对话气泡：**对白进气泡、旁白退回底部字幕**（漫画语义——气泡=有人在说）。

    人物逐帧坐标无从得知，气泡默认落在画面上部（头顶区域的通用近似）；
    Skill 层写分镜时可用 shots[].bubble_pos = left/center/right 按构图标注
    水平落点，气泡尾巴始终指向下方的说话人。
    """
    opts = opts or {}
    fill = _ass_color(opts.get("bubble", "#ffffff"))
    fill_a = _ass_alpha(opts.get("bubble_alpha", 24))
    border = _ass_color(opts.get("border", "#2a2a33"))
    text_c = opts.get("text_color", "#1b1d24")       # 亮底深字是漫画气泡的可读性正解
    name_c = opts.get("name_color", "#8a5a1c")
    font = _norm_font(opts.get("font", "Alibaba PuHuiTi 3.0 65 Medium"))
    size = int(opts.get("size", 52))
    by_default = int(canvas_h * float(opts.get("y", 0.16)))   # 气泡顶边（上部头顶区）

    cap = {**_CAPTION_DEFAULTS, **(opts.get("caption") or {})}  # 旁白回退样式可另配
    cap["font"] = _norm_font(cap["font"])                       # 旁白回退底部字幕同样归一
    # 演出型版式里的旁白回退底部字幕走同一条判据（`caption` 子块是它自己的作者层，
    # 画风的 profile 字号不流进这里，故不带 PROFILE_SIZE_KEY——写了就是明确指定）
    cap["size"] = resolve_size(opts.get("caption"), canvas_w, canvas_h)
    cap_size = int(cap["size"])
    if "margin_v" not in (opts.get("caption") or {}):           # 旁白底部字幕同样横竖屏自适应贴底
        cap["margin_v"] = _default_margin_v(canvas_w, canvas_h, cap_size)
    cap_zh_max = _cjk_max_chars(canvas_w, cap_size)             # 旁白底部字幕换行同样随画布宽自适应
    cap_en_max = max(34, _latin_max_chars(canvas_w, cap_size))  # 英文旁白按词折，与 caption 同尺（见 _wrap_main）
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\nPlayResY: {canvas_h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: BShape,Alibaba PuHuiTi 3.0 65 Medium,40,&H00FFFFFF,&H00FFFFFF,&H00202020,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
        f"Style: BText,{font},{size},{_style_color(text_c)},{_style_color(text_c)},"
        "&H00FFFFFF,&H00000000,1,0,0,0,100,100,0.5,0,1,0,0,7,0,0,0,1\n"
        f"Style: BName,{font},{int(size * 0.68)},{_style_color(name_c)},{_style_color(name_c)},"
        "&H00181818,&H00000000,1,0,0,0,100,100,1,0,1,2,0,7,0,0,0,1\n"
        f"Style: Default,{cap['font']},{cap['size']},{_style_color(cap['text_color'])},"
        f"{_style_color(cap['text_color'])},{_style_color(cap['outline_color'])},&H64000000,"
        f"{1 if cap['bold'] else 0},0,0,0,100,100,{cap['spacing']},0,1,"
        f"{cap['outline']},{cap['shadow']},2,{cap_size},{cap_size},{cap['margin_v']},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out = [header]
    lang = opts.get("lang") or "zh"          # 旁白回退的折行判据（气泡正文另有框体几何）
    pos_x = {"left": 0.28, "center": 0.5, "right": 0.72}
    timeline = expand_timeline(timeline, spans_of)   # 多角色镜逐句展开（名牌/归属跟着换人）
    for start, end, shot in timeline:
        text = strip_voice_tags(shot.get("dialogue") or shot.get("narration")
                                or shot.get("caption") or "")
        if not text:
            continue
        st, et = _ass_time(start), _ass_time(end)
        speaker = (shot.get("speaker") or "").strip()
        if not speaker:   # 旁白：没有说话人就没有气泡，走底部字幕
            out.append(f"Dialogue: 0,{st},{et},Default,,0,0,0,,"
                       + _wrap_main(text, lang, cap_zh_max, cap_en_max))
            continue
        wrapped = _wrap_lines(text, 12, 3)
        nlines = wrapped.count("\\N") + 1
        longest = max(len(x) for x in wrapped.split("\\N"))
        pad_x, pad_y = 44, 30
        bw = min(canvas_w - 140, longest * int(size * 1.06) + pad_x * 2)
        bh = nlines * int(size * 1.3) + pad_y * 2
        cx = int(canvas_w * pos_x.get((shot.get("bubble_pos") or "center"), 0.5))
        bx = max(50, min(canvas_w - 50 - bw, cx - bw // 2))
        by = by_default
        fad = "\\fad(140,90)"
        out.append(
            f"Dialogue: 0,{st},{et},BShape,,0,0,0,,{{\\pos({bx},{by}){fad}\\1c{fill}"
            f"\\1a{fill_a}\\bord3\\3c{border}\\p1}}"
            f"{_bubble_path(bw, bh, 26, cx - bx - 23)}")
        out.append(
            f"Dialogue: 1,{st},{et},BName,,0,0,0,,"
            f"{{\\pos({bx + 10},{by - int(size * 0.68) - 16}){fad}}}{speaker}")
        out.append(
            f"Dialogue: 1,{st},{et},BText,,0,0,0,,"
            f"{{\\pos({bx + pad_x},{by + pad_y}){fad}}}{wrapped}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(out_path)


# ---------------- 统一分发 ----------------
def render(timeline, out_path, *, canvas_w=1080, canvas_h=1920, sub_cfg=None,
           spans_of=None) -> str:
    """按 sub_cfg.mode 选字幕/叠加样式：
    caption(默认·样式随画风) / bubble(头顶气泡) / dialogue_box(游戏对话框) /
    centered(居中大字) / ranking(榜单徽章)。"""
    sub_cfg = sub_cfg or {}
    mode = sub_cfg.get("mode", "caption")
    # en 单语：演出型模式的文本位换用英文字段（both 的双行版式仅 caption 支持）
    if mode != "caption" and (sub_cfg.get("lang") or "zh") == "en":
        timeline = [(s0, e0, _en_view(sh)) for s0, e0, sh in timeline]
    if mode == "bubble":
        return build_bubble(timeline, out_path, canvas_w=canvas_w, canvas_h=canvas_h,
                           opts=sub_cfg, spans_of=spans_of)
    if mode == "dialogue_box":
        return build_dialogue_box(timeline, out_path, canvas_w=canvas_w, canvas_h=canvas_h,
                                 opts=sub_cfg, spans_of=spans_of)
    if mode == "centered":
        return build_centered(timeline, out_path, canvas_w=canvas_w, canvas_h=canvas_h,
                             opts=sub_cfg, spans_of=spans_of)
    if mode == "ranking":
        return build_ranking(timeline, out_path, canvas_w=canvas_w, canvas_h=canvas_h,
                            opts=sub_cfg, spans_of=spans_of)
    return build_from_timeline(timeline, out_path, canvas_w=canvas_w, canvas_h=canvas_h,
                               opts=sub_cfg, spans_of=spans_of)
