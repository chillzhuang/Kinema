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

"""包围框与正方形裁切 —— 纯 numpy/OpenCV 的几何原语，无状态。"""
from __future__ import annotations

import cv2
import numpy as np

from .params import BOX_EXPAND, BOX_MARGIN


def kp_bbox(kp, expand: float = BOX_EXPAND):
    """一组关键点的包围框 `[x0, y0, x1, y1]`，全是 NaN 时返回 None。"""
    v = kp[~np.isnan(kp).any(axis=1)]
    if len(v) == 0:
        return None
    x0, y0 = v.min(axis=0)
    x1, y1 = v.max(axis=0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = (x1 - x0) * expand, (y1 - y0) * expand
    return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], np.float32)


def iou(a, b) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / union) if union > 0 else 0.0


def square_box(x0, y0, x1, y1, w: int, h: int, margin: float = BOX_MARGIN):
    """把任意矩形扩成正方形 `(x0, y0, 边长)`。深度模型的输入是固定正方形，
    非等比塞进去会把人拉扁；先取正方形再缩放，形变只剩下与画面比例无关的那一份。"""
    side = int(round(max(x1 - x0, y1 - y0) * (1 + 2 * margin)))
    side = max(64, min(side, max(w, h)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return int(round(cx - side / 2)), int(round(cy - side / 2)), side


def union_box(mask, kps_list, w: int, h: int):
    """覆盖全部人物的正方形裁切（遮罩像素 ∪ 全部有效关键点）。

    人分散在画面两端时会退化成接近整帧——可以接受：那种构图里本来也没有
    「只放大人物」的余地。
    """
    xs0, ys0, xs1, ys1 = [], [], [], []
    ys, xs = np.where(mask)
    if xs.size:
        xs0.append(xs.min()); ys0.append(ys.min())
        xs1.append(xs.max()); ys1.append(ys.max())
    for kp in kps_list:
        b = kp_bbox(kp, expand=1.0)
        if b is not None:
            xs0.append(b[0]); ys0.append(b[1])
            xs1.append(b[2]); ys1.append(b[3])
    if not xs0:
        return (0, 0, max(w, h))
    return square_box(min(xs0), min(ys0), max(xs1), max(ys1), w, h)


def crop_square(img, box):
    """按正方形框裁图；越界处按边缘复制补齐（补黑会在人物贴边时造出假轮廓）。"""
    x0, y0, side = box
    h, w = img.shape[:2]
    pl, pt = max(0, -x0), max(0, -y0)
    pr, pb = max(0, x0 + side - w), max(0, y0 + side - h)
    if pl or pt or pr or pb:
        img = cv2.copyMakeBorder(img, pt, pb, pl, pr, cv2.BORDER_REPLICATE)
    return img[y0 + pt:y0 + pt + side, x0 + pl:x0 + pl + side]


def paste_square(canvas, patch, box) -> None:
    """把正方形块按框写回画布，取较大值（多人各自的遮罩因此天然并起来）。"""
    x0, y0, side = box
    h, w = canvas.shape[:2]
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(w, x0 + side), min(h, y0 + side)
    if cx1 > cx0 and cy1 > cy0:
        canvas[cy0:cy1, cx0:cx1] = np.maximum(
            canvas[cy0:cy1, cx0:cx1],
            patch[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0])
