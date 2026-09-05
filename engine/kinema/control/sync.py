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

"""运动对拍：成片相对控制段的时间偏移。

我们这一侧的时间链是确定的：控制段按绑定区间逐帧裁出、成片按同样的秒数请求、
配乐按同一区间裁切。量不到的只有模型那一段——它跟随参考视频的走位与节奏，但不
保证逐帧对齐，实测常有几帧的整体滞后。本模块拿两路画面的帧间差分能量做互相关，
量出这个整体偏移；配乐随之平移，舞步与拍点才真正对上。

只量一个整体偏移：模型在镜内忽快忽慢那部分，平移救不了，只能重生成。所以相关峰
不够清楚时不动配乐，量到的偏移超出窗口也不用（阈值见 `params`）。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from ..ffmpeg import run_capture
from ..pipeline.checkpoint import has_file
from .params import SYNC_MAX_LAG_SEC, SYNC_MIN_CORR, SYNC_RATE, SYNC_WORK_WIDTH

_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=(-?[\d.]+)")


def motion_energy(path, *, seconds: float, rate: int = SYNC_RATE,
                  width: int = SYNC_WORK_WIDTH) -> list[float] | None:
    """逐帧运动能量：帧间差分的平均亮度。两路先重采样到同一帧率，序列才逐帧可比
    （控制段随章节帧率，成片随厂商）。缩到小尺寸只为省时——量的是能量随时间的起伏，
    不是画面细节。读不出来返回 None。"""
    rc, out, _err = run_capture(
        ["-t", f"{seconds:.3f}", "-i", str(path), "-an",
         "-vf", f"fps={rate},scale={width}:-2,format=gray,tblend=all_mode=difference,"
                "signalstats,metadata=print:file=-",
         "-f", "null", "-"], loglevel="error", desc="motion energy")
    if rc != 0:
        return None
    vals = [float(m.group(1)) for m in _YAVG.finditer(out)]
    return vals or None


def _zscore(xs: list[float]) -> list[float] | None:
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / n)
    if sd <= 1e-9:
        return None          # 整段没有运动起伏，无从对拍
    return [(x - mean) / sd for x in xs]


def cross_lag(a: list[float], b: list[float], *, max_lag: int) -> tuple[int, float] | None:
    """`b` 相对 `a` 的最佳整数偏移（帧）与该处的相关：`b[t + k] ≈ a[t]`，k > 0 即 b 滞后。

    只在重叠长度不少于一半时取值，否则窗口边缘几个点的偶然相关会冒充峰值。"""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    best = None
    for k in range(-max_lag, max_lag + 1):
        lo, hi = max(0, -k), min(n, n - k)
        if hi - lo < n // 2:
            continue
        score = sum(a[i] * b[i + k] for i in range(lo, hi)) / (hi - lo)
        if best is None or score > best[1]:
            best = (k, score)
    return best


def estimate_lag(control_path, clip_path, *, seconds: float,
                 rate: int = SYNC_RATE) -> dict | None:
    """成片相对控制段的整体偏移：`{"lag": 秒（正=成片晚）, "corr": 峰值相关, "applied": 是否够格用于配乐平移}`。

    任何一路量不出来返回 None——对拍是核对，不是出片的前置条件。"""
    a = motion_energy(control_path, seconds=seconds, rate=rate)
    b = motion_energy(clip_path, seconds=seconds, rate=rate)
    if not a or not b:
        return None
    za, zb = _zscore(a), _zscore(b)
    if za is None or zb is None:
        return None
    best = cross_lag(za, zb, max_lag=int(round(SYNC_MAX_LAG_SEC * rate)))
    if best is None:
        return None
    k, corr = best
    return {"lag": round(k / rate, 3), "corr": round(corr, 3),
            "applied": corr >= SYNC_MIN_CORR}


def measure_sync(project) -> list[dict]:
    """给每个绑了控制视频且成片在盘的镜量偏移，写进 `gen.control.sync`（落盘由调用方做，
    它持着章节锁）。成片不在盘的镜清掉旧值——那是上一版成片的偏移。"""
    out = []
    for s in project.active_shots:
        rec = (s.get("gen") or {}).get("control")
        if not rec:
            continue
        ctl, clip = s.get("control"), s.get("clip")
        if not (ctl and has_file(ctl) and clip and has_file(clip)):
            rec.pop("sync", None)
            continue
        seconds = float(rec.get("seconds") or 0) or float(s.get("dur") or 0)
        r = estimate_lag(ctl, clip, seconds=seconds)
        if r is None:
            rec.pop("sync", None)
            continue
        rec["sync"] = {**r, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        out.append({"shot": s.get("id"), **r})
    return out


def describe(sync: dict | None) -> str:
    """一句人话：给日志、`control compare` 与 Studio 的对照片信息栏共用。"""
    if not sync:
        return "未量到偏移"
    lag = float(sync.get("lag") or 0)
    corr = float(sync.get("corr") or 0)
    aligned = abs(lag) < 1e-9
    head = ("成片与控制段逐帧对齐" if aligned
            else f"成片比控制段{'晚' if lag > 0 else '早'} {abs(lag):.2f}s")
    if not sync.get("applied"):
        tail = "相关不足，配乐不平移"
    else:
        tail = "配乐照原区间铺" if aligned else "配乐随之平移"
    return f"{head}（相关 {corr:.2f}）——{tail}"
