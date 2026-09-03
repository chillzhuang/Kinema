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

"""首帧锚定（anchor_frame）：让本镜以分镜图作 `role=first_frame` 起步，且不焊末帧。

native 的缺省档是全能参考：分镜图以 `role=reference_image` 附发，模型据它调和出
开头几帧。人工审过的那张分镜图因此**不是成片的第 0 帧**——实测同一镜的片段首帧
与分镜图在构图、主体姿态与陈设上都不是同一个镜头。要让首帧硬锁在审过的画面上，
协议上只有 `role=first_frame` 一条路。

不开本档时，另一条到达首帧任务的通道是首尾帧衔接（framechain）：它同时把下一镜
的分镜图钉成本镜末帧，接缝处随之不是硬切——「首帧要锁」和「镜间要硬切」两个诉求
在那条通道的数据模型上被绑成一个开关，单镜取不到「只锁首帧」这一态。本模块补的
就是这一档。

三档承接阶梯里它落在最前一格（对照 `tailrelay` 的表）：

    首帧锚定   分镜图=first_frame，不发末帧，镜间硬切
    尾帧接力   分镜图=reference_image + 上镜真实末帧，软承接
    首尾帧衔接 分镜图=first_frame + 下一镜图=last_frame，无缝焊接

**代价必须先说清**：首帧任务与参考媒体官方互斥（适配器亦硬拦），所以本档下
角色/场景/道具设定图、简笔板、尾帧接力的附发通道全部让位，跨镜一致性只剩
分镜图本身与文字角色锚。分镜图已经把该镜的外观与空间画定时这笔账划算，
角色中途入画或需要设定图补细节的镜不适用。

**显式 opt-in**：章级 `anchor_frame: true` / 镜级 `shots[].anchor_frame: true` /
本次 `gen-video --anchor-frame`。缺省仍是全能参考——它换掉的是请求拓扑与
一致性来源，不该被静默切换。

判据单一真源：渲染侧（`cli._shot_plan`）从这里取，别处不重算。
"""
from __future__ import annotations

from ..project import normalize_motion


def active(data: dict, motion: str, override: bool = False) -> bool:
    """本章首帧锚定是否处于章级开启态：显式 opt-in × native。

    仅 native 成立：dubbed 恒走参考媒体通道（`ref_audio` 与首帧互斥），kenburns
    不调用视频模型。`motion` 取已归一的模式名（`Project.motion` 口径）——把模式
    并进判据而不是留给调用方各自再 `and native`，与 `framechain.active` 同制。
    """
    if normalize_motion(motion) != "native":
        return False
    return bool(override or (data or {}).get("anchor_frame"))


def shot_opt_in(shot: dict) -> bool:
    """镜级表态 `shots[].anchor_frame: true`：只让这一镜锁首帧，其余照走缺省档。

    与 `framechain.pair_opt_in` 的区别是它不涉及第二镜——首帧锚定是单镜属性，
    没有「焊缝两端」的概念，故也不需要在下游镜再写一份。
    """
    return bool((shot or {}).get("anchor_frame"))


def anchored(data: dict, shot: dict, motion: str, override: bool = False) -> bool:
    """本镜是否走首帧锚定 = 章级开启 或 镜级表态（章级判据已含 native）。

    镜级表态同样受模式闸约束：dubbed 章里写 `shots[].anchor_frame: true` 不生效，
    否则适配器会在参考媒体模式下收到一个发不出去的首帧要求。
    """
    if normalize_motion(motion) != "native":
        return False
    return bool(override or (data or {}).get("anchor_frame") or shot_opt_in(shot))
