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

"""预留额度：花钱**之前**把整批要发的请求算清楚再决定发不发（M15 事前闸）。

与 `Project.add_cost` 的分工——两道闸方向相反，缺一不可：

  · `add_cost` 是**事后闸**：钱已经花了，先入账再抛错。它只能阻止「继续烧」，
    所以整批必然已经烧掉一部分（超限那一笔以及它之前的全部）。
  · 本模块是**事前闸**：一次调用都还没发出去，就按整批预估与台账余额对账，
    不够就一镜都不发。用户看到中断时账单是零。

**不重复计费天然成立**：预算比的永远是真实台账 `cost`（`add_cost` 累加的那份），
预估侧 `cost_estimate` 从不参与裁决——预估参与裁决会出现「dry-run 跑两遍额度
被吃光」的错误结果。本模块也**不写任何字段**（preflight 只在内存算），调用方同样不得借机
回写 `cost_estimate.video`：那是 dry-run 的审阅快照 + ledger 预估侧 + 交付
manifest 的唯一来源，覆写会让「预估 vs 实际」永久失真。

两个旋钮（都是章节文档顶层、都由人填、都不填=不设限）：
  · `budget`          —— 项目总额上限（元）。既有字段，`add_cost` 事后闸也读它。
  · `budget_per_call` —— **单笔**调用上限（元）。总额够但单镜特别贵（4K/长镜）时
    要求二次确认，防「一条命令按错档位烧掉一个月额度」。

数值一律过有限性守卫：非数字 / NaN / Infinity / ≤0 统统按「不设限」处理——
落盘进 project.json 的数只能是有限数（NaN 不是合法 JSON，Studio 整页会崩）。
"""
from __future__ import annotations

import math

TOTAL_KEY = "budget"            # 项目总额上限（元）
CALL_KEY = "budget_per_call"    # 单笔调用上限（元）
CURRENCY_KEY = "currency"       # cost 台账里的非金额键


def limit(value) -> float | None:
    """把用户填的额度归一成「有限正数或 None（不设限）」。

    与 `add_cost` 的 `if budget:` 口径一致：0 == 不设限。非数字/NaN/Infinity
    同样落 None——不拿 NaN 去做比较（NaN 的比较恒 False，会静默把闸变成
    永远放行：表面上限额仍在，实际不再拦截）。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def spent_total(doc: dict) -> float:
    """台账已花总额（元）——**只认 `cost`，不认 `cost_estimate`**。

    `add_cost` 的事后闸直接调用本函数求和——两道闸共用这一份实现，
    不存在「事前闸说够、事后闸立刻断」的口径分叉面。
    """
    cost = (doc or {}).get("cost") or {}
    total = 0.0
    for k, v in cost.items():
        if k == CURRENCY_KEY:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            total += f
    return round(total, 4)


def verdict(doc: dict, estimate: float, max_call: float = 0.0) -> dict:
    """整批预估的裁决（纯函数，零副作用）。

    入参：`estimate` = 本批全部调用的预估总额（元）；`max_call` = 本批**最贵一次
    调用**的预估金额（元）。两者都由调用方按 provider 自身计费口径算好
    （`billable_seconds` × `effective_price_per_second`），本模块不碰 provider。

    出参（全部有限数，可直接进日志/异常文案）：
      over_budget —— 已花 + 本批预估 > budget（**硬超上限**：任何模式下都拦，
                     包括 `run`/`--auto`，因为放行等于必然爆预算）；
      over_cap    —— 最贵单笔 > budget_per_call（**软超阈**：交互式命令要
                     `--confirm-spend` 二次确认，`run`/`--auto` 下告警放行——
                     否则一条龙会死在这里且没有解锁路径）。
    """
    budget = limit((doc or {}).get(TOTAL_KEY))
    cap = limit((doc or {}).get(CALL_KEY))
    spent = spent_total(doc)
    est = max(0.0, limit(estimate) or 0.0)
    call = max(0.0, limit(max_call) or 0.0)
    return {
        "estimate": round(est, 2),
        "max_call": round(call, 2),
        "budget": budget,
        "cap": cap,
        "spent": spent,
        "remaining": None if budget is None else round(budget - spent, 2),
        "over_budget": budget is not None and (spent + est) > budget,
        "over_cap": cap is not None and call > cap,
    }
