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

"""音轨里的有声段落——字幕落点的事实来源。

两类探测源，档位按来源分（`clean` 参数）：native 片段的人声由视频模型生成、
混着音效底床，开口时刻只有产物本身知道；dubbed 的主音轨是逐镜 TTS wav，
信号干净但峰均差大。`dur` 是计费秒数，`lines[].dur` 仅在跑过 TTS 后有值，
两者都不表达「这一镜第几秒开始说话」。

判据：把音轨滤到人声频带 → 量出该段峰值 → 按相对峰值的阈值跑 `silencedetect`，
取静音区间的补集。频带与相对阈值缺一不可——不滤频带时引擎与爆燃的低频能量把整条
音轨顶在阈值之上；写死 dB 阈值则在另一档响度的镜上全中或全不中（native 片段逐镜
响度差好几档）。

能量判据不区分人声与同频带音效，故只用于有台词的镜；检测不出时由调用方回落到
按镜窗口铺满。
"""
from __future__ import annotations

import re
import subprocess

# 人声频带：下限避开引擎与结构共振的低频，上限避开金属摩擦与嘶声。
VOICE_BAND = "highpass=f=250,lowpass=f=3400"
# 阈值相对本段峰值的下探量。经验分界：更小会把较响的背景计入人声，
# 更大会把句中的弱读音节切成断句。
RELATIVE_FLOOR_DB = 8.0
# 相对阈值的钳位区间。上钳避免近乎无声的片段把底噪计入人声，
# 下钳避免单次爆响把峰值顶高后连人声都落在阈值之下。
FLOOR_MIN_DB, FLOOR_MAX_DB = -45.0, -12.0
# 干净源档位（TTS 逐镜 wav）：没有引擎轰鸣与音效底床，但峰均差远大于模型片段
# ——未压缩语音的峰只在爆破音上，语句主体低 20 dB 以上，按峰下探 8 dB 会把
# 语句主体整段判成静音、只剩最响的音节被当成整句（5s 语音会检成 0.8s）。
# 下探深度与钳位按逐镜 TTS 实测标定：人声频带峰 -8.5~-2.7 / 语句体 -20~-30 /
# 底噪低于 -50，max-25 恰好落在两者之间。
RELATIVE_FLOOR_DB_CLEAN = 25.0
FLOOR_MIN_DB_CLEAN, FLOOR_MAX_DB_CLEAN = -50.0, -20.0
# 断句的最短静音。字间停顿普遍在 0.2~0.4s，阈值低于此会把一句话切成多段。
MIN_SILENCE_SEC = 0.35
# 有声段的最短时长，低于此按单次音效（关门、撞击）丢弃。
MIN_SPEECH_SEC = 0.3
# 起止余量：silencedetect 报的是能量跨过阈值的时刻，辅音起始段常在阈值之下，
# 不留余量会切掉字头。
PAD_SEC = 0.12

_SILENCE_START = re.compile(r"silence_start:\s*([-\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([-\d.]+)")
_MAX_VOLUME = re.compile(r"max_volume:\s*([-\d.]+) dB")


def _ffmpeg_audio(media: str, afilter: str) -> str | None:
    """在音轨上跑一条滤镜链并回收 ffmpeg 日志；ffmpeg 不可用时 None。

    `-vn` 是必需项：本链路只读音频包络，保留视频流会让每次探测多解码一遍画面。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-vn", "-i", str(media),
             "-af", afilter, "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:            # 无音轨 / 坏文件：滤镜没跑起来
        return None
    return proc.stderr or ""


def _floor_db(media: str, *, clean: bool = False) -> float | None:
    """本段人声频带的静音阈值（相对峰值下探并钳进合理区间）；量不出返回 None。

    `clean`：源是干净的 TTS wav（无音效底床）时用深下探档——两档源的峰均差
    相差一个量级，共用一套下探量必有一侧整段误判。"""
    log = _ffmpeg_audio(media, f"{VOICE_BAND},volumedetect")
    if not log:
        return None
    hit = _MAX_VOLUME.search(log)
    if not hit:
        return None
    if clean:
        floor = float(hit.group(1)) - RELATIVE_FLOOR_DB_CLEAN
        return max(FLOOR_MIN_DB_CLEAN, min(FLOOR_MAX_DB_CLEAN, floor))
    floor = float(hit.group(1)) - RELATIVE_FLOOR_DB
    return max(FLOOR_MIN_DB, min(FLOOR_MAX_DB, floor))


def _silences(media: str, duration: float,
              *, clean: bool = False) -> list[tuple[float, float]] | None:
    """媒体里的静音区间；ffmpeg 不可用或解析不出返回 None（与「没有静音」区分开）。"""
    floor = _floor_db(media, clean=clean)
    if floor is None:
        return None
    log = _ffmpeg_audio(
        media, f"{VOICE_BAND},silencedetect=noise={floor:.1f}dB:d={MIN_SILENCE_SEC}")
    if log is None:
        return None
    # 一条没有静音事件的音轨 stderr 里不出现 silencedetect 字样：那是「没有静音」，
    # 不是「探测失败」——两态在这里分开，否则短句镜的语音同步整套静默失效
    starts = [float(m) for m in _SILENCE_START.findall(log)]
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    out = []
    for i, st in enumerate(starts):
        en = ends[i] if i < len(ends) else duration
        if en > st:
            out.append((max(st, 0.0), min(en, duration)))
    return out


def speech_windows(media: str, duration: float,
                   *, clean: bool = False) -> list[tuple[float, float]]:
    """媒体里的有声段落 → `[(起, 止), …]`，相对媒体起点。

    本函数只回答「整段音轨里哪几处在出声」，**不回答哪一段对应哪一句**：
    段界是能量边界，与句界没有因果关系——句中换气会把一句切成多段，低音量的
    句子会整句落在噪声底之下一段都不出。段数与句数相等也只是巧合，据此逐句
    对号入座会把字幕贴到别的句子的时间上。逐句落点只能来自语义划界
    （`asr.line_windows`），本函数的产出供调用方判断「这镜有没有出声」以及
    回落时收整体首尾。

    故也**不原样裁掉任何一段**：整句的真实起止只有全量段落能给出，在这里按
    时长或位置挑掉一段，调用方收首尾时就会把被裁段的正身留在字幕窗口之外。

    检测不出、无音轨、整段静音一律返回空列表，由调用方回落。
    """
    if duration <= 0:
        return []
    sil = _silences(media, duration, clean=clean)
    if sil is None:
        return []
    voiced: list[tuple[float, float]] = []
    cursor = 0.0
    for st, en in sorted(sil):
        if st - cursor >= MIN_SPEECH_SEC:
            voiced.append((cursor, st))
        cursor = max(cursor, en)
    if duration - cursor >= MIN_SPEECH_SEC:
        voiced.append((cursor, duration))
    return [(round(max(a - PAD_SEC, 0.0), 3), round(min(b + PAD_SEC, duration), 3))
            for a, b in voiced]
