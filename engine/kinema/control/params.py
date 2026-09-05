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

"""深度控制视频的常量表 —— 纯字面量，不导入任何感知栈。

Seedance 侧的参考视频硬限（时长/体积/帧率/比例）不在这里重铸：那一份在
`previz.py` 已经是单一真源，两个特性发的是同一个 `role=reference_video` 槽。
"""
from __future__ import annotations

# 章节工作目录内的产物落点（与 previz/ sketch/ 同级）
CONTROL_SUBDIR = "control"

# 姿态与遮罩的工作短边。深度另有自己的固定输入尺寸（见 DEPTH_SIZE）。
# 540 是拐点：再低则远景小人的手臂在遮罩上开始断裂，再高则每帧多花的
# 分割时间买不到可见的轮廓改善。
WORK_SHORT = 540

# 预编译 ONNX 的固定输入边长。**不能等比喂图**——这份权重导出时就锁死了
# 518×518，要动态形状得自己用 torch 重导。故做法是只裁人物包围框再拉成正方形。
DEPTH_SIZE = 518

# 单条源片的时长上限。Studio 把 build 派成子进程，`jobs._job_timeout` 对没有
# `meta.shots` 的任务硬顶 1800 秒然后 killpg——超时被杀时 CLI 跑不到失败处理，
# sidecar 会永远停在非终态。按每源秒约 17.5 秒 CPU 算，30 秒源片落在
# 525 秒左右，离那道墙有三倍余量。真正要发出去的段本来也只有 4~15 秒。
MAX_SOURCE_SEC = 30.0

# 姿态置信度地板：低于此的关节置 NaN，交给时序层补洞或留空
KPT_THR = 0.3
# 有效关节少于此数的检测不成其为人（半身入画的下限）
MIN_JOINTS = 6
# 一次检测的平均关节置信度地板。**这一条专治幽灵人**：背景里的车、反光、
# 衣物褶皱能凑够 6 个低分关节，逐条都过 KPT_THR 却整体很虚。这种轨迹能连着
# 一两秒，光靠 MIN_TRACK_SEC 拦不住，在成片里就是一条乱挥的肢体。
# 真人通常在 0.7 以上。
MIN_KPT_MEAN = 0.5
# 短于此**秒数**的 track 视为误检丢弃。按秒而不是按帧：8 帧在 30fps 下是 0.27 秒、
# 60fps 下只有 0.13 秒，同一份阈值在两种源片上严宽差一倍。
MIN_TRACK_SEC = 0.5

# 贪心 IoU 跟踪：配对阈值与失配容忍帧数（一人短暂被另一人挡住时不丢 id）
TRACK_IOU = 0.25
TRACK_MAX_MISS = 12

# 关键点缺口补洞的上限**秒数**，以及 NaN 感知高斯平滑的 sigma。同样按秒不按帧。
# 0.4 秒是运动模糊导致的连续漏检的典型长度（快速舞蹈素材上漏检可达三成帧）；
# 再长就该判定为「人真的离开了画面」，跨过去插值会画出一条凭空穿过画面的肢体。
GAP_MAX_SEC = 0.4
SMOOTH_SIGMA = 1.2
# 深度上下界曲线的时序低通 sigma（帧）
DEPTH_SIGMA = 12.0

# 运动自适应 EMA：静止像素去闪烁，运动像素权重趋零故不拖影
EMA_TAU = 0.06
EMA_STRENGTH = 0.5

# 裁切框外扩比例：关键点框用于跟踪与每人分割，正方形框用于深度推理
BOX_EXPAND = 1.25
BOX_MARGIN = 0.12

# 输出编码质量。控制视频是模型的输入而不是交付物，但压花的骨骼线会被读成
# 画面内容，故给得比成片还高一档。
CRF = 14

# OpenPose-18 的连线表与配色。**这是互操作契约不是审美选择**——下游视频模型
# 认的就是 `controlnet_aux` 那套画法，换一根连线或一个颜色，模型对骨架的解读
# 就不再是它训练时见过的那套。索引按 1 起（与上游画法一致）。
LIMB_SEQ = ((2, 3), (2, 6), (3, 4), (4, 5), (6, 7), (7, 8), (2, 9), (9, 10), (10, 11),
            (2, 12), (12, 13), (13, 14), (2, 1), (1, 15), (15, 17), (1, 16), (16, 18))
COLORS = ((255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
          (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
          (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
          (255, 0, 255), (255, 0, 170), (255, 0, 85))

# 选段缩略条的格距（秒）——控制台在它上面拖选段窗，起点也吸附到这个格
STRIP_STEP_SEC = 0.5

# 对照片每格被摞的那一维（竖摞时是宽、横排时是高）。对照片是审看用的，不进成片
# 也不喂模型，故按「浏览器里拖得动」定档而不是按源片分辨率：三格按原尺寸拼出来
# 是 5760 宽，拖动就卡。
STACK_TILE = 720

# sidecar 的状态机。`failed` 是终态，中间产物保留供排查。
STATUSES = ("queued", "analysing", "stabilising", "rendering", "comparing", "done", "failed")

# 运动对拍核对（成片 vs 控制段）：两路画面都按 SYNC_RATE 重采样、缩到 SYNC_WORK_WIDTH
# 宽后取帧间差分能量做互相关。量出的整体偏移只在相关峰够清楚（SYNC_MIN_CORR）且落在
# 窗口内（SYNC_MAX_LAG_SEC）时才用于配乐平移——量不准就不动，宁可差几帧也不把音乐
# 挪到一个错的位置上。
SYNC_RATE = 24
SYNC_WORK_WIDTH = 160
SYNC_MAX_LAG_SEC = 1.0
SYNC_MIN_CORR = 0.3
