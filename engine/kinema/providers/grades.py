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

"""画质档位目录：每个视频适配器认哪个字段、该字段有哪些档。

**为什么是一张独立的表，而不是挂在配置层或适配器类上**：这份事实要同时喂给三个
方向——适配器发请求前的归一、配置中心的下拉、以及守卫。放在配置层，适配器就得反向
依赖上层；放在适配器类上，配置层为了取目录得把适配器模块导进来（甚至实例化）。
放在 providers 的最底层且零依赖，三个方向一律向下引用，没有环也没有导入代价。

**它只负责说明，不负责裁决**——目录里没有的值照发不误，`--resolution` 的
「显式点名恒赢」一个字不改。原因有二：厂商随时会开新档，本机表必然滞后于厂商，
把它做成闸就等于「厂商放开了、我们这儿还不让发」；而且档位与计费、时长是联动的
（Veo 非 720p 强制出片 8 秒），任何本地拒绝都会把一个能跑的调用变成跑不了。
真正的裁决在服务端。

**唯一的例外是 `minimax_video`**：H3 的档位名与所有别家都不同，不在发前归一就是
一次必被拒的请求，所以那个适配器的白名单直接派生自本表——对它而言这几行是承重的，
删一档等于把用户配好的值静默改写成 768P。守卫因此把 H3 那组值逐字钉死。

**每条档位必须带 `source`**：这些全是抄来的厂商事实，会过期。没有出处的收窄一律
不许进表——「某档不能用」如果只是猜测，代价是本地拒掉一个今天成立的调用。
"""
from __future__ import annotations

from typing import NamedTuple


class Grade(NamedTuple):
    """一个档位。

    `caveat` 是**提示不是禁令**：同一个 impl 被多个别名复用时（seedance 的 mini 与
    2.5 共用一个适配器），型号之间的档位差异只能在这里说清楚，不能靠从目录里删掉
    某一档来表达——那会让点名了大模型的用户也选不到。
    """
    value: str
    label: str
    source: str
    caveat: str = ""


class GradeSpec(NamedTuple):
    """某个适配器的档位口径。

    `field` 是它真正读的那个连接段字段——**刻意不假定叫 `resolution`**：各家厂商
    对「画质档」的字段名并不统一，写死一个名字，读别的字段的服务商就会在界面上
    显示一格它根本不读的输入框，改了也不生效还不报错。

    `fold` 说的是「该适配器自己会把大小写归一」，只有这样的家才准按大小写不敏感
    比对——**绝不能全局折叠**：seedance 与 veo 都是把值原样发出去的，对它们来说
    `720P` 确实不是合法档，折叠会把一条正确的告警变成漏报。
    """
    field: str
    label: str
    hint: str
    grades: tuple[Grade, ...]
    fold: bool = False

    def matches(self, value: str) -> bool:
        vals = [g.value for g in self.grades]
        return (value.upper() in [v.upper() for v in vals]) if self.fold \
            else value in vals


_ARK = "config/models.yaml 的 seedance 连接段注释（含实测记录）"

GRADES: dict[str, GradeSpec] = {
    "seedance": GradeSpec(
        field="resolution", label="分辨率档",
        hint="mini 与 2.5 共用本适配器，可用档位随型号不同",
        grades=(
            Grade("480p", "480p · 标清", _ARK),
            Grade("720p", "720p · 高清", _ARK + "；适配器兜底档 + 全部别名的缺省档"),
            Grade("1080p", "1080p · 全高清", "需在别名上显式声明",
                  "2.5 开放此档；2.0 fast/mini 不开放（`resolutions` 白名单会本地拦下）"),
            Grade("4k", "4K · 超高清", "CLI --resolution 档位表 + 4K 单价分支",
                  "本仓在册别名（fast/mini/2.5）都不开放 4k，设了也会被白名单拦下"),
        )),
    "veo": GradeSpec(
        field="resolution", label="分辨率档",
        hint="换档会连带改出片时长（连带计费），不只是清晰度",
        grades=(
            Grade("720p", "720p · 高清", "适配器缺省档"),
            Grade("1080p", "1080p · 全高清", "适配器注释的官方口径",
                  "非 720p 官方强制出片 8 秒：短镜也拿回 8 秒片段，钱照 8 秒算"),
            Grade("4k", "4K · 超高清", "适配器注释的官方口径",
                  "同样强制 8 秒出片与计费；未配 4K 单价则台账低估"),
        )),
    "minimax_video": GradeSpec(
        field="resolution", label="分辨率档",
        hint="H3 的档位名与其他厂商完全不同，只有这两个是合法值",
        fold=True,   # 构造器与发请求前各 upper 一次，`768p` 与 `768P` 等价
        grades=(
            Grade("768P", "768P · 标准", "适配器发前归一的白名单"),
            Grade("2K", "2K · 高清", "适配器发前归一的白名单",
                  "自托管的 H3-Base 封顶 768p，2K 上采只有云端 API 有"),
        )),
}


def spec_for(impl: str) -> GradeSpec | None:
    return GRADES.get(impl)


def values_of(impl: str) -> tuple[str, ...]:
    spec = GRADES.get(impl)
    return tuple(g.value for g in spec.grades) if spec else ()
