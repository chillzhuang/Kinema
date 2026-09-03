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

"""本地语音转写（faster-whisper）——native 声源的文字核对与逐句划界。

native 的人声由视频模型生成，「念的是不是字幕那句话」「每句话从第几秒说到
第几秒」在链路里没有别的实测来源，本模块补这一层：verify 用它比对台词文字
（`native_voice_unverified` 的核对出口），合成侧在语音段数与句数对不上时用它
按句文本重新划界字幕落点。

faster-whisper 是**可选依赖**（`asr` extra：`pip install -e "engine[asr]"`，
模型本地推理、零 API 成本）：
未安装或加载失败时所有入口返回 None，调用方按「测不了」回落，绝不抛错中断
合成或体检——转写是增强信息，不是硬前置。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 模型档位：small 在 CPU 上逐镜秒级完成、中文词错率足够支撑相似度判定；
# 换档改这里，两处消费方（verify / 字幕划界）自动跟随
ASR_MODEL = "small"

# 中文解码的字形引导。whisper 对普通话默认输出繁体，字形差异逐字进比对——
# 一句 18 字的台词只因繁简就把召回从 0.88 打到 0.59，落进「没按稿念」的假警。
# 用一句简体示例把解码引到稿面同一套字形上，比在归一层补繁简表更靠近根因；
# 数词字形引导句压不住，走 `_DIGITS` 折叠。
_ZH_STYLE_PROMPT = "以下是普通话的句子，请用简体中文转写，数字写成汉字。"

_model = None
_unavailable = False


def _get_model():
    """惰性加载并进程内复用（模型加载秒级、转写毫秒级，逐镜重载不可接受）。
    失败记忆化：缺包/缺模型在同一进程内只探测一次，不逐镜重试。"""
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model
    try:
        from faster_whisper import WhisperModel
        _model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8")
    except Exception:  # noqa: BLE001  可选依赖：任何加载失败都按不可用处理
        _unavailable = True
    return _model


def available() -> bool:
    return _get_model() is not None


def transcribe(path, *, lang: str = "zh") -> dict | None:
    """整段转写 → `{"text", "segments": [{start,end,text}], "words": [(s,e,w)]}`，
    不可用或转写失败返回 None。

    带词级时间戳（逐句划界的原料）并开 VAD 过滤——native 片段的环境床（雨、
    海浪）会被无 VAD 的解码强行凑成幻听文本，比对分数被这类噪声字拉低。
    中文另给字形引导（`_ZH_STYLE_PROMPT`），把转写与稿面对齐到同一套字形。

    **判空即无 VAD 复解一次**：Silero 的语音概率对轻声台词整段判负，转写为空会让
    verify 报「念出 0%」，一段正确的片段被判死、代价是一次按秒计费的重投。复解不
    放大误报——闭声镜没有稿面文字、不进核对，环境床凑出的幻听字对不上台词，
    分数照样低。"""
    model = _get_model()
    if model is None:
        return None
    try:
        res = _decode(model, path, lang, vad=True)
        return res if res["text"] else _decode(model, path, lang, vad=False)
    except Exception:  # noqa: BLE001  转写失败按测不了回落，不中断调用方
        return None


def _decode(model, path, lang: str, *, vad: bool) -> dict:
    segments, _info = model.transcribe(
        str(path), language=lang, word_timestamps=True, vad_filter=vad,
        initial_prompt=_ZH_STYLE_PROMPT if lang == "zh" else None)
    segs, words = [], []
    for seg in segments:
        segs.append({"start": round(float(seg.start), 2),
                     "end": round(float(seg.end), 2),
                     "text": str(seg.text).strip()})
        for w in (seg.words or []):
            words.append((float(w.start), float(w.end), str(w.word)))
    text = " ".join(s["text"] for s in segs).strip()
    if _norm(text) and _norm(text) in _norm(_ZH_STYLE_PROMPT):
        # 音频里没有语音时解码器会把引导句本身吐出来（whisper 在静音上复述
        # prompt）。整段都落在引导句里就是这种复述，不是音频内容——闭声镜
        # 的转写本该是空的，留着它会让「听到什么」这一栏说谎
        return {"text": "", "segments": [], "words": []}
    return {"text": text, "segments": segs, "words": words}


# 数词字形折叠：字形引导句压不住阿拉伯数字——台词「零七，报数」会转写成
# 「07 报数」，召回掉到 0.5。折到同一套字形上再比，比在提示词里反复要求可靠
_DIGITS = str.maketrans("0123456789〇", "零一二三四五六七八九零")


def _norm(s: str) -> str:
    """比对口径：只留汉字、字母与数字，数词统一折成汉字。标点、空白、数字字形与
    ASR 的分词差异都不是「念错了」，进判据只会把真漂移淹在格式噪声里。"""
    return re.sub(r"[^\w一-鿿]+", "", str(s or "")).lower().translate(_DIGITS)


def speech_chars(text: str) -> int:
    """稿面按比对口径归一后的字数：语速实测与预估共用同一分母。"""
    return len(_norm(text))


def text_match(expected: str, heard: str) -> float:
    """台词文本与转写文本的相似度（0~1，归一化后字符级，双向对称）。"""
    a, b = _norm(expected), _norm(heard)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def text_recall(expected: str, heard: str) -> float:
    """稿面文字被念出来的比例（0~1，归一化后字符级，**单向**）。

    回答的是「这一稿念了多少」，所以分母只有稿面：对称相似度对漏念不敏感
    （转写只有稿子的前 43% 时它仍给出 0.6），而模型把长句念一半就转场正是
    生成式声源的主要失效形态。转写里多出来的字（环境床凑出的幻听）不进分母，
    它们不是「没按稿念」。"""
    a, b = _norm(expected), _norm(heard)
    if not a or not b:
        return 0.0
    matched = sum(m.size for m in SequenceMatcher(None, a, b).get_matching_blocks())
    return matched / len(a)


def line_recalls(lines: list[str], heard: str) -> list[float]:
    """逐句稿面召回（与 lines 等长）。

    整稿按序与转写做一次匹配，再把匹配块按各句在整稿里的字符区间摊回去。
    逐句独立比对不成立：短句的字在别句的转写里也能配上，整句漏念照样满分。"""
    parts = [_norm(t) for t in lines]
    a, b = "".join(parts), _norm(heard)
    if not a or not b:
        return [0.0] * len(parts)
    hit = bytearray(len(a))
    for m in SequenceMatcher(None, a, b).get_matching_blocks():
        hit[m.a:m.a + m.size] = b"\x01" * m.size
    out, pos = [], 0
    for p in parts:
        out.append(sum(hit[pos:pos + len(p)]) / len(p) if p else 0.0)
        pos += len(p)
    return out


# 整镜转写与全部台词的最低相合度：低于它说明模型念的根本不是这些句子，
# 按比例划界只会给错误内容标上煞有介事的时间——直接放弃划界。
# 这道闸用双向相似度而不是召回：划界要的是「转写与稿子彼此对得上」，
# 转写里大量稿外内容同样会让字数配额切歪，而召回对那种情形恒给满分
_ALIGN_MATCH_MIN = 0.5


def line_windows(path, lines: list[dict], win: float) -> list[tuple] | None:
    """按句文本把转写词流划成逐句窗口 → `[(start, end), …]`（与 lines 等长），
    划不了返回 None。

    划界按**字数配额**切词流（每句应占的归一化字符份额），而不是逐字对齐：
    ASR 的个别误字不动摇比例，逐字对齐则一个错字就断链。前置整镜相合度闸
    （`_ALIGN_MATCH_MIN`）：内容对不上时任何划界都是编造。"""
    if not lines or not win or win <= 0:
        return None
    quota = [len(_norm(str(ln.get("text") or ""))) for ln in lines]
    if not all(quota):
        return None
    res = transcribe(path)
    if not res or not res.get("words"):
        return None
    words = [(s, e, _norm(w)) for s, e, w in res["words"] if _norm(w)]
    heard_total = sum(len(w) for _s, _e, w in words)
    if not heard_total:
        return None
    if text_match("".join(str(ln.get("text") or "") for ln in lines),
                  res.get("text") or "") < _ALIGN_MATCH_MIN:
        return None
    total = sum(quota)
    spans, wi, consumed = [], 0, 0
    for k, q in enumerate(quota):
        last = k == len(quota) - 1
        want = heard_total - consumed if last else max(1, round(heard_total * q / total))
        start_w, got = wi, 0
        while wi < len(words) and (last or got < want):
            got += len(words[wi][2])
            wi += 1
        if wi <= start_w:
            return None
        consumed += got
        spans.append((words[start_w][0], words[wi - 1][1]))
    out, floor = [], 0.0
    for a, b in spans:
        a = max(floor, min(float(a), win))
        b = max(a, min(float(b), win))
        if b - a < 0.05:
            return None
        out.append((round(a, 2), round(b, 2)))
        floor = b
    return out
