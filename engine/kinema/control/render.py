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

"""全分辨率渲染 —— 遮罩、深度精修、骨骼、精细版。

深度这条链上的每一步都是为了修一个具体的伪影，顺序不能换：

  贴回画布 → 去阶梯 → 轮廓内侧补深度 → 反锐化 → 运动自适应 EMA → 乘 alpha

对深度**不做引导滤波**（那会把轮廓和体内起伏一起糊掉）；引导滤波只用在遮罩上，
让 256² 分辨率的分割边缘吸附到原图边缘，头发丝与手指轮廓由此而来。
"""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import paste_square
from .params import COLORS, EMA_STRENGTH, EMA_TAU, LIMB_SEQ, STRIP_STEP_SEC


def alpha_from_mask(mask_small, guide_rgb, w: int, h: int):
    """粗遮罩 → 贴合图像边缘的软 alpha（约 2 px 抗锯齿过渡）。"""
    guide = guide_rgb.astype(np.float32) / 255.0
    m = cv2.resize(mask_small.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    r = max(3, int(min(w, h) / 135))
    a = cv2.ximgproc.guidedFilter(guide, m, r, 1e-4)
    return np.clip((a - 0.42) / 0.16, 0, 1)


def refine_depth(depth_crop, box, alpha, w: int, h: int, sx: float, sy: float, prev):
    """裁切深度贴回全分辨率画布并精修。返回 `(0~1 深度, 供下一帧的 EMA 状态)`。"""
    x0, y0, side = box
    bx, by = int(round(x0 * sx)), int(round(y0 * sy))
    bs = int(round(side * sx))
    canvas = np.zeros((h, w), np.float32)
    paste_square(canvas, cv2.resize(depth_crop, (bs, bs), interpolation=cv2.INTER_CUBIC),
                 (bx, by, bs))
    canvas = cv2.GaussianBlur(canvas, (0, 0), 1.5)   # 去掉裁切放大留下的阶梯

    # 轮廓内侧补深度：模型在人物边界内侧几像素会把深度往「远」衰减，直接按遮罩
    # 切出来就是一圈发黑软边。用可信内部深度做归一化外推、按到背景的距离加权，
    # 轮廓上补满、往内平滑消失。**只抬不压**，且不用膨胀填边——那会留一圈亮环。
    k = max(3, int(min(w, h) / 90))
    hard = (alpha > 0.5).astype(np.uint8)
    ref = cv2.erode(hard, np.ones((k * 3 // 2 | 1,) * 2, np.uint8)).astype(np.float32)
    ext = (cv2.GaussianBlur(canvas * ref, (0, 0), k)
           / np.maximum(cv2.GaussianBlur(ref, (0, 0), k), 1e-3))
    dist = cv2.distanceTransform(hard, cv2.DIST_L2, 5)
    wgt = np.clip(1.0 - dist / (2.5 * k), 0, 1)
    canvas = canvas + (wgt * wgt * (3 - 2 * wgt)) * np.maximum(ext - canvas, 0)

    # 大半径反锐化找回体内起伏。分母用 alpha 的模糊，否则黑背景会把暗晕渗进身体
    sigma = min(w, h) / 54
    blur_a = cv2.GaussianBlur(alpha, (0, 0), sigma)
    blur_d = cv2.GaussianBlur(canvas * alpha, (0, 0), sigma) / np.maximum(blur_a, 1e-3)
    canvas = np.clip(canvas + 0.35 * (canvas - blur_d) * alpha, 0, 1)

    # 运动自适应 EMA：静止像素去闪烁，运动像素权重趋零故不拖影
    if prev is not None:
        wt = np.exp(-((canvas - prev) / EMA_TAU) ** 2)
        canvas = canvas + (prev - canvas) * (EMA_STRENGTH * wt)
    return canvas, canvas


def _draw_limbs(canvas, kp, stickwidth: int, aa: bool):
    flag = cv2.LINE_AA if aa else cv2.LINE_8
    for i, (a, b) in enumerate(LIMB_SEQ):
        p, q = kp[a - 1], kp[b - 1]
        if np.isnan(p).any() or np.isnan(q).any():
            continue
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        length = float(np.hypot(*(p - q)))
        angle = float(np.degrees(np.arctan2(q[1] - p[1], q[0] - p[0])))
        poly = cv2.ellipse2Poly((int(mx), int(my)), (max(int(length / 2), 1), stickwidth),
                                int(angle), 0, 360, 1)
        cv2.fillConvexPoly(canvas, poly, COLORS[i], lineType=flag)


def _draw_joints(canvas, kp, radius: int, aa: bool):
    flag = cv2.LINE_AA if aa else cv2.LINE_8
    for j in range(18):
        if not np.isnan(kp[j]).any():
            cv2.circle(canvas, (int(kp[j, 0]), int(kp[j, 1])), radius, COLORS[j],
                       thickness=-1, lineType=flag)


def skeleton(kps_full, w: int, h: int, stickwidth: int, radius: int, *, aa: bool = False):
    """OpenPose-18 火柴人，2× 超采样后缩回。

    先把所有人的肢体画完并整体压到 60% 亮度，再叠满亮度的关节点——交叠时关节
    压在肢体之上，两个人手臂交叉的帧才看得出谁是谁。
    """
    c = np.zeros((h * 2, w * 2, 3), np.uint8)
    for kp in kps_full:
        _draw_limbs(c, kp * 2, stickwidth * 2, aa)
    c[:] = (c * 0.6).astype(np.uint8)
    for kp in kps_full:
        _draw_joints(c, kp * 2, radius * 2, aa)
    return cv2.resize(c, (w, h), interpolation=cv2.INTER_AREA)


def control_frame(depth01, pose_rgb):
    """主控制帧：灰度深度铺底，骨骼像素直接覆盖。"""
    out = np.repeat((depth01 * 255).astype(np.uint8)[:, :, None], 3, axis=2)
    on = pose_rgb.any(axis=2)
    out[on] = pose_rgb[on]
    return out


def styled_frame(depth01, alpha, kps_full, w: int, h: int, stickwidth: int, radius: int):
    """精细版：按深度法线打光的白色黏土浮雕 + 辉光骨骼。给人看，不喂模型。"""
    z = depth01 * (min(w, h) * 0.09)
    gx = cv2.Sobel(z, cv2.CV_32F, 1, 0, ksize=5) / 48.0
    gy = cv2.Sobel(z, cv2.CV_32F, 0, 1, ksize=5) / 48.0
    nz = 1.0 / np.sqrt(gx * gx + gy * gy + 1.0)
    nx, ny = -gx * nz, -gy * nz
    light = np.array([-0.45, -0.55, 0.70])
    light /= np.linalg.norm(light)
    diff = np.clip(nx * light[0] + ny * light[1] + nz * light[2], 0, 1)
    rim = (1.0 - nz) ** 1.5 * 0.25          # 边缘光，让侧轮廓从黑底里浮出来
    lum = (0.50 + 0.50 * diff) * (0.60 + 0.40 * depth01) + rim
    lum = np.clip(lum, 0, 1) ** 0.85 * alpha

    base = np.repeat((lum * 255).astype(np.uint8)[:, :, None], 3, axis=2).astype(np.float32)
    skel = skeleton(kps_full, w, h, max(2, stickwidth * 2 // 3), radius,
                    aa=True).astype(np.float32)
    glow = cv2.GaussianBlur(skel, (0, 0), stickwidth * 2.0) * 0.8
    out = 255.0 - (255.0 - base) * (255.0 - glow) / 255.0      # screen 混合
    on = skel.max(axis=2) > 8
    out[on] = out[on] * 0.15 + np.minimum(skel[on] * 1.25, 255) * 0.85
    for kp in kps_full:
        for j in range(18):
            if not np.isnan(kp[j]).any():
                cv2.circle(out, (int(kp[j, 0]), int(kp[j, 1])), max(2, radius // 2),
                           (255, 255, 255), -1, cv2.LINE_AA)
    return np.clip(out, 0, 255).astype(np.uint8)


def stick_width(w: int, h: int) -> int:
    """骨骼粗细随画幅缩放——固定像素宽在竖屏与横屏上会差出一倍观感。"""
    return max(2, int(round(4 * min(w, h) / 512)))


def write_sheet(path, tiles) -> None:
    """对照图：源片与控制视频各缩一半，**竖素材并排、横素材上下**。

    只留两格是刻意的——这张图的读者是缩略带上百来像素高的一个格子，它要回答的只有
    一个问题：「这段素材的骨骼贴不贴得住动作」。四格挤进那个尺寸后每一格都太小，
    而单独的深度图与精细版在那个尺寸下与控制图几乎无法区分。

    方向判据与对照片同源（见 `io.stack_args`）。这里还多一层理由：缩略带的格子只定高、
    宽随图的原比例（`.cvc-cell img`），横素材并排出来是 32:9，一条素材就顶出一个比
    竖素材宽三倍半的格子，把整条带子挤走样。
    """
    h, w = tiles[0].shape[:2]
    small = [cv2.resize(t, (w // 2, h // 2), interpolation=cv2.INTER_AREA) for t in tiles]
    cv2.imwrite(str(path), cv2.cvtColor(
        np.concatenate(small, axis=0 if w > h else 1), cv2.COLOR_RGB2BGR))


def write_strip(path, frames, fps: float, height: int = 96) -> None:
    """每 `STRIP_STEP_SEC` 一格的缩略条——控制台在它上面拖选段窗，
    格距即选段起点的吸附步长，两者必须同源。"""
    if not frames:
        return
    tiles = []
    for f in frames:
        h, w = f.shape[:2]
        tiles.append(cv2.resize(f, (max(1, int(w * height / h)), height),
                                interpolation=cv2.INTER_AREA))
    cv2.imwrite(str(path), cv2.cvtColor(np.concatenate(tiles, axis=1), cv2.COLOR_RGB2BGR))


def strip_stride(fps: float) -> int:
    return max(1, int(round(fps * STRIP_STEP_SEC)))
