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

"""时序稳定 —— 整段拿到内存里做，逐帧感知结果在这里才变成能看的动画。

三条曲线各有各的抖法：遮罩边缘逐帧跳、关键点偶尔掉、相对深度每帧的尺度都不同。
离线处理允许非因果滤波，故一律用居中窗口，比实时滤波平得多。
"""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import iou, kp_bbox
from .params import DEPTH_SIGMA, GAP_MAX_SEC, MIN_TRACK_SEC, SMOOTH_SIGMA


def fill_gaps(track, gap_max: int = 8):
    """线性补上不超过 `gap_max` 帧的空洞；长缺口留着不补。

    长缺口意味着这个人真的离开了画面（或被完全挡住），跨过去插值会画出一条
    凭空穿过画面的肢体。首尾的缺口同样不补——没有两端就没有插值可言。
    """
    valid = ~np.isnan(track)
    if valid.sum() < 2:
        return track
    idx = np.arange(len(track))
    filled = np.interp(idx, idx[valid], track[valid])
    out = track.copy()
    t, total = 0, len(track)
    while t < total:
        if valid[t]:
            t += 1
            continue
        u = t
        while u < total and not valid[u]:
            u += 1
        if (u - t) <= gap_max and t > 0 and u < total:
            out[t:u] = filled[t:u]
        t = u
    return out


def smooth_nan(track, sigma: float):
    """NaN 感知的高斯平滑（归一化卷积）：缺失点不参与加权，也不被填上。

    卷积走**全卷积后取中段**而不是 `mode="same"`：后者返回的长度是
    `max(信号, 核)`，核比信号长时结果就比输入还长，随后按输入长度的掩码索引
    直接越界。深度窗的核是 73 帧，任何短于 3 秒的素材都会撞上——而测试用的
    合成片正好在那个量级。取中段的写法与序列长度无关。
    """
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    valid = ~np.isnan(track)
    num = np.convolve(np.where(valid, track, 0.0), k)[r:r + len(track)]
    den = np.convolve(valid.astype(np.float64), k)[r:r + len(track)]
    out = np.where(den > 1e-6, num / np.maximum(den, 1e-6), np.nan)
    out[~valid] = np.nan
    return out


def stabilise_tracks(per_frame: list, nframes: int, fps: float = 24.0) -> dict:
    """逐帧的 `[(track_id, 18×2 关键点), …]` → `{id: (T, 18, 2)}` 平滑轨迹。

    两个阈值都由 `fps` 派生而不是写死帧数——同一份帧数阈值在 30fps 与 60fps 源片上
    严宽差一倍：

    · 短于 `MIN_TRACK_SEC` 的 track 整条丢弃。那多半是背景里的一次误检，留着就是
      画面里凭空挥动的半个骨架。
    · 缺口补洞上限取 `GAP_MAX_SEC`。运动模糊会让检测连续漏掉十几帧，补得太短
      骨架就一闪一闪；补得太长则会在人真的走出画面时画出一条穿过画面的肢体。
    """
    min_len = max(2, int(round(MIN_TRACK_SEC * fps)))
    gap_max = max(1, int(round(GAP_MAX_SEC * fps)))
    ids = sorted({tid for fr in per_frame for tid, _ in fr})
    tracks: dict[int, np.ndarray] = {}
    for tid in ids:
        arr = np.full((nframes, 18, 2), np.nan, np.float32)
        for i, fr in enumerate(per_frame):
            for t, kp in fr:
                if t == tid:
                    arr[i] = kp
        present = ~np.isnan(arr[:, :, 0]).all(axis=1)
        if present.sum() < min_len:
            continue
        for j in range(18):
            for a in range(2):
                arr[:, j, a] = smooth_nan(fill_gaps(arr[:, j, a], gap_max), SMOOTH_SIGMA)
        tracks[tid] = arr
    return tracks


def keep_person_components(m, people: list, min_area: int):
    """丢掉太小、或不与任何人物关键点框相交的遮罩连通域。

    分割模型偶尔会把背景里的一小片织物判成衣服；不挨着任何一个检测到的人的
    色块，按定义就不是人。
    """
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return m
    boxes = [b for b in (kp_bbox(kp, expand=1.4) for kp in people) if b is not None]
    keep = np.zeros(n, bool)
    for c in range(1, n):
        x, y, w, h, area = stats[c]
        if area < min_area:
            continue
        keep[c] = True if not boxes else any(iou((x, y, x + w, y + h), b) > 0 for b in boxes)
    return keep[lab].astype(np.uint8)


def stabilise_mask(masks, per_frame: list):
    """遮罩去抖：3 帧多数投票 → 闭/开运算 → 连通域过滤。

    投票只取相邻三帧——再宽会让快速挥动的手臂拖出残影，而遮罩的抖动本来就是
    单帧尺度的。
    """
    total = len(masks)
    h, w = masks.shape[1:]
    out = np.empty_like(masks)
    k5 = np.ones((5, 5), np.uint8)
    k3 = np.ones((3, 3), np.uint8)
    min_area = int(0.0005 * h * w)
    for t in range(total):
        lo, hi = max(0, t - 1), min(total, t + 2)
        vote = masks[lo:hi].astype(np.uint8).sum(axis=0)
        m = (vote * 2 > (hi - lo)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k5)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k3)
        out[t] = keep_person_components(m, [kp for _, kp in per_frame[t]],
                                        min_area).astype(bool)
    return out


def depth_window(depths, masks_crop, sigma: float = DEPTH_SIGMA):
    """每帧人物像素的 (2, 98) 百分位上下界，再对两条曲线做时序低通。

    相对深度逐帧只定义到仿射尺度，所以必须自己定归一化区间。两条被否掉的做法：
    逐帧 min-max 是闪烁的主因；链式帧间对齐会累积漂移（两百多帧后人物整体发黑）。按人物自身取百分位则让身体始终占满动态范围，低通再把帧间的尺度跳变
    抹平。代价是「人物整体走近/走远」这一维被归一化掉了——要留住它需要静止背景
    像素做锚，而那在运镜素材上不成立。
    """
    total = len(depths)
    lo = np.full(total, np.nan)
    hi = np.full(total, np.nan)
    for t in range(total):
        v = depths[t][masks_crop[t]]
        if v.size >= 100:
            lo[t], hi[t] = np.percentile(v, [2, 98])
    idx = np.arange(total)
    valid = ~np.isnan(lo)
    if not valid.any():
        return (np.full(total, float(depths.min())),
                np.full(total, float(depths.max()) + 1e-3))
    lo = smooth_nan(np.interp(idx, idx[valid], lo[valid]), sigma)
    hi = smooth_nan(np.interp(idx, idx[valid], hi[valid]), sigma)
    return lo, np.maximum(hi, lo + 1e-3)
