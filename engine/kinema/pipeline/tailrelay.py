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

"""尾帧接力（tail_relay）：把上一镜片段的**真实末帧**作为下一镜的参考图发出。

全能参考/参考媒体两档的镜间缺省是硬切直拼——相邻镜的分镜图各画各的，接缝两侧的
构图与光线没有任何像素级约束。首尾帧衔接（framechain）能焊缝，但参与镜退回首帧
任务、板与设定图的附发通道全部让位（官方禁混）。尾帧接力走第三条路：生成时请求
`return_last_frame` 拿回本镜片段的最后一帧，下一镜把它作为一张 `role=reference_image`
附发，职责句声明「开场从它延续」——板、设定图与分段时间轴照发，衔接与参考两头都保住。

**显式 opt-in**（章级 `tail_relay: true` 或 `gen-video --tail-relay`）：它改变请求
组成与提示词形态，且要求整批按成片顺序串行——下一镜的尾帧注入发生在上一镜回填
之后，`parallel.run` 只有 workers=1 保证这个次序。

官方尾帧在**受信模型产物**清单内且 URL 有时效（见 seedance.py 的 last_frame_url
说明）：同批接力优先用新鲜 URL（免落盘往返、不受时效影响），本地落盘副本供跨轮
重投。重投被输入闸拦下（如超出受信有效期）时该镜按无承接的基线提示词重发，
或重生上一镜取新鲜尾帧；受信判据见 docs/kinema/seedance-face-policy.md。

判据单一真源：渲染侧（cli.stage_gen_video）从这里取，别处不重算。
"""
from __future__ import annotations

from . import transitions
from .. import review
from ..project import normalize_motion
from .checkpoint import has_file


def active(data: dict, motion: str, override: bool = False) -> bool:
    """本章尾帧接力是否开启：显式 opt-in ×（native 或 dubbed）。

    kenburns 不调用视频模型，无帧可回传；`motion` 取已归一的模式名
    （`Project.motion` 口径）。provider 能力面（`supports_return_last_frame` /
    `supports_reference_images`）由调用方按路由结果逐镜判，不揉进章级总闸。
    """
    if normalize_motion(motion) not in ("native", "dubbed"):
        return False
    return bool(override or (data or {}).get("tail_relay"))


def prev_shot(shots: list, shot: dict) -> dict | None:
    """成片里紧接着出现的上一正镜（承接来源）；找不到返回 None。

    必须在**未过滤**的完整 shots 上找（与 `framechain.plan` 同一条纪律）：从
    `--only` 过滤后的清单取邻居，会把成片里并不相邻的两镜接到一起。
    遇转场镜即断——转场是场景切换标记，跨转场承接等于把两个场景的收尾与开场
    缝成一段；弃用镜（omt）跳过继续往前找。
    """
    idx = next((i for i, item in enumerate(shots) if item is shot), None)
    if idx is None:
        return None
    for prev in reversed(shots[:idx]):
        if transitions.is_transition(prev):
            return None
        if review.is_omitted(prev):
            continue
        return prev
    return None


def tails_of(shot: dict) -> dict:
    """该镜已登记的逐比例尾帧（`gen.clip.tail_frames` 原样返回，不判在盘）。"""
    clip = ((shot or {}).get("gen") or {}).get("clip") or {}
    tails = clip.get("tail_frames")
    return dict(tails) if isinstance(tails, dict) else {}


def disk_tails(shot: dict | None, targets) -> dict | None:
    """上一镜在盘的逐比例尾帧：**每个要出的比例都在盘**才算数，否则 None。

    与 `cli._flf2v` 的全称量词同一条保守纪律——一个比例带承接、另一个比例
    裸发，两版成片的接缝观感就不是同一部片子。
    """
    if not shot:
        return None
    tails = tails_of(shot)
    out = {}
    for asp in targets:
        path = tails.get(asp)
        if not (path and has_file(path)):
            return None
        out[asp] = str(path)
    return out
