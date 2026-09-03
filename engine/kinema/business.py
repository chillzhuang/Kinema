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

"""成本运营台账（ledger）——预估与实际双轨分列：

  · 预估来自 `gen-video --dry-run` 落盘的 cost_estimate 快照（审阅提示词时顺手入账）；
  · 实际来自渲染逐笔回填的 cost；
  · 废片成本取版本栈归档条目 params.cost 之和——被替换版本的沉没成本，
    是 AI 生产线独有的运营指标（传统制片没有"一键重拍"这回事）。
"""
from __future__ import annotations

from .budget import spent_total
from .storage.base import chapter_title


def chapter_ledger(data: dict) -> dict:
    """单章台账行：预估 / 实际 / 废片 / 重roll / 有效镜数与时长。

    弃用（omt）镜不计入镜数与时长，但其历史花费仍留在 cost 与版本栈里——
    台账如实保留这笔支出，不做冲销。"""
    shots = data.get("shots") or []
    active = [s for s in shots
              if ((s.get("review") or {}).get("shot") or {}).get("state") != "omt"]
    waste, rerolls = 0.0, 0
    rerolls_by: dict = {}
    for s in shots:
        for stage, entries in (s.get("versions") or {}).items():
            n = len(entries or [])
            rerolls += n
            if n:
                # 按阶段分列：audio 重合成零成本、clip 重roll 按秒计费——混在一个
                # 数里会把「换旁白锁重跑 tts」读成「视频废了 N 次」
                rerolls_by[stage] = rerolls_by.get(stage, 0) + n
            waste += sum(float((e.get("params") or {}).get("cost") or 0)
                         for e in entries or [])
    est = ((data.get("cost_estimate") or {}).get("video") or {})
    actual = data.get("cost") or {}
    return {
        "shots": len(active), "omitted": len(shots) - len(active),
        "duration": round(sum(float(s.get("dur") or 0) for s in active), 2),
        "estimate_video": est.get("amount"),
        "estimate_at": est.get("at"),
        "actual": {k: v for k, v in actual.items() if k != "currency"},
        # 与 estimate_video 同口径的那一列：预估只算视频，合计含图/配音/音乐，
        # 两者并排会被读成「计费溢出」，而差额其实是别的 kind
        "actual_video": float(actual.get("video") or 0.0),
        # 合计走 budget.spent_total——预算双闸、台账、Studio 下发同一份求和
        "actual_total": spent_total(data),
        "waste": round(waste, 4),
        "rerolls": rerolls,
        "rerolls_by": rerolls_by,
    }


def project_ledger(ws, pid: str) -> dict:
    """项目台账：逐章行 + 汇总 + 运营指标。

    单位产出成本 = 实际总成本 / 有效镜数；重roll 均值 = 归档版本数 / 有效镜数。
    这两个指标跨项目可比，是判断「哪个风格档/哪种模式更烧钱」的依据。"""
    s = ws.get_project(pid)
    rows, chapters = [], s.list_chapters()
    for ch in chapters:
        cdata = ws.store.load_chapter(s.pid, ch["id"]) or {}
        rows.append({"chapter": ch["id"], "title": chapter_title(ch, cdata),
                     "created_at": ch.get("created_at"),
                     **chapter_ledger(cdata)})
    tot_shots = sum(r["shots"] for r in rows) or 1
    chapters_actual = round(sum(r["actual_total"] for r in rows), 4)
    # 系列级支出（设定图、系列主视觉、试音、资产局改、锚定预热）单列并计入总额；
    # 单位产出成本仍按章节支出算——那是「每镜多少钱」的口径，设定集不摊到镜
    series_cost = {k: round(float(v), 4) for k, v in (s.data.get("cost") or {}).items()
                   if k != "currency" and isinstance(v, (int, float))}
    series_total = round(sum(series_cost.values()), 4)
    actual = round(chapters_actual + series_total, 4)
    waste = round(sum(r["waste"] for r in rows), 4)
    rerolls = sum(r["rerolls"] for r in rows)
    rerolls_by: dict = {}
    for r in rows:
        for stage, n in (r.get("rerolls_by") or {}).items():
            rerolls_by[stage] = rerolls_by.get(stage, 0) + n
    return {
        "project": s.pid, "title": s.data.get("title") or s.pid,
        "template": (s.data.get("template") or {}).get("label"),
        "chapters": rows,
        "totals": {
            "estimate_video": round(sum(r["estimate_video"] or 0 for r in rows), 2),
            "actual_video": round(sum(r["actual_video"] for r in rows), 2),
            "actual": actual, "actual_chapters": chapters_actual,
            "series": series_cost, "series_total": series_total,
            "waste": waste, "rerolls": rerolls,
            "rerolls_by": rerolls_by,
            "duration": round(sum(r["duration"] for r in rows), 2),
            "shots": sum(r["shots"] for r in rows),
            "cost_per_shot": round(chapters_actual / tot_shots, 4),
            "rerolls_per_shot": round(rerolls / tot_shots, 2),
            "waste_ratio": round(waste / chapters_actual, 4) if chapters_actual else 0.0,
        },
    }
