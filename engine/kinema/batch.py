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

"""跨镜批量编辑。

「把所有镜头改成夜晚 / 全片换发色」一句话全片生效：Skill 层做语义编译
（自然语言 → 字段 + 操作），本模块做**确定性执行**——逐镜改字段、留痕、可撤销。

设计要点：
  · 操作四种：set 整体替换 / append 追加 / prepend 前置 / replace 子串替换（旧=>新）。
  · **锁定镜保护**：字段所属阶段已通过(done)的镜默认跳过（定稿的钱不再动），
    `include_locked=True` 才纳入——与状态机的防烧钱语义一致。
  · **编辑即待重生**：提示词类字段改完自动把所属阶段置 retake（意见=本次批量说明，
    会编译进下一版提示词；重生前旧产物自动归档进版本栈）——「批量编辑 × 版本栈」
    使全片级修改可安全回滚。`mark_retake=False` 可只改文案不触发重生。
  · **操作日志**：每次批量操作记录字段旧值到章节 JSON 的 batch_ops，`undo` 还原
    字段并按同一条规则把受影响阶段置 retake（撤销也是一次编辑，产物同样过期）；
    日志保留最近 50 条。
"""
from __future__ import annotations

from datetime import datetime

from .errors import KinemaError
from .storage.snowflake import next_id

# 可批量编辑的字段。**只收标量文本字段**——`_transform` 做的是字符串拼接与
# 子串替换，把 `characters`（数组）或 `dur`（数值）放进来会把原值静默改写成
# 字符串。所以本表是 `review.STAGE_FIELDS` 的子集而非同义词：那张表回答的是
# 「改了要重生什么」，本表回答的是「哪些字段允许按字符串批量改」。
EDITABLE_FIELDS = (
    "image_prompt", "image_prompt_en", "negative_prompt",
    "video_prompt", "video_prompt_en",
    "narration", "caption",
    "lighting", "sfx", "transition",
    "camera", "framing", "angle", "lens", "face_visibility",
    "action", "entry_state", "end_state", "light_shift",
)
OPS = ("set", "append", "prepend", "replace")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _transform(old: str, op: str, value: str) -> str:
    old = old or ""
    if op == "set":
        return value
    if op == "append":
        return old + value
    if op == "prepend":
        return value + old
    if op == "replace":
        if "=>" not in value:
            raise KinemaError('replace 需要 "旧=>新" 形式，如 --replace "白天=>夜晚"')
        src, dst = value.split("=>", 1)
        return old.replace(src, dst)
    raise KinemaError(f"未知操作: {op}（可选: {', '.join(OPS)}）")


def apply(project, shots: list[dict], field: str, op: str, value: str, *,
          mark_retake: bool = True, include_locked: bool = False,
          note: str | None = None) -> dict:
    """批量执行。返回 {op_id, changed, skipped_locked, unchanged, stages}。

    一个字段可能同时让多个阶段的产物失效（如 `narration`：TTS 要重跑，native
    片段的提示词里也写着这段话）——受影响阶段取自 `review.STAGE_FIELDS`。
    锁定保护按**任一受影响阶段已通过**判：定稿的产物不因另一个阶段还没定就被
    连带打回。
    """
    from . import review
    if field not in EDITABLE_FIELDS:
        raise KinemaError(f"不支持批量编辑的字段: {field}"
                             f"（可选: {', '.join(EDITABLE_FIELDS)}）")
    stages = review.stages_for(field)
    changes, retaken = {}, []
    skipped_locked = unchanged = 0
    for s in shots:
        sid = str(s.get("id"))
        if not include_locked and any(review.is_locked(s, st) for st in stages):
            skipped_locked += 1
            continue
        old = s.get(field) or ""
        new = _transform(old, op, value)
        if new == old:
            unchanged += 1
            continue
        changes[sid] = old
        s[field] = new
        if not mark_retake:
            continue
        hit = False
        for st in stages:
            if review.is_locked(s, st):     # include_locked 放进来的定稿镜不动它
                continue
            review.set_state(s, st, "retake",
                             note=note or f"批量修改（{field}）：{value}")
            hit = True
        if hit:
            retaken.append(sid)
    if not changes:
        return {"op_id": None, "changed": 0, "skipped_locked": skipped_locked,
                "unchanged": unchanged, "stages": stages}
    entry = {"id": str(next_id()), "at": _now(), "field": field, "op": op,
             "value": value, "stages": list(stages), "changes": changes,
             "mark_retake": bool(mark_retake)}
    log = project.data.setdefault("batch_ops", [])
    log.append(entry)
    del log[:-50]                              # 只留最近 50 条
    project.save()
    return {"op_id": entry["id"], "changed": len(changes),
            "skipped_locked": skipped_locked, "unchanged": unchanged,
            "stages": stages, "retaken": retaken}


def undo(project, op_id: str | None = None) -> dict:
    """撤销一次批量操作（缺省=最近一次）：还原字段旧值，受影响阶段按 apply 同一规则
    置 retake——批量之后按新值重生过的产物与旧值一样对不上。"""
    from . import review
    log = project.data.get("batch_ops") or []
    if not log:
        raise KinemaError("没有可撤销的批量操作（batch_ops 为空）")
    entry = log[-1] if op_id is None else \
        next((e for e in log if str(e.get("id")) == str(op_id)), None)
    if entry is None:
        raise KinemaError(f"找不到批量操作 {op_id}（batch log 查看）")
    field = entry["field"]
    stages = tuple(entry.get("stages") or ())
    mark_retake = entry.get("mark_retake", True)
    restored = skipped_locked = 0
    by_id = {str(s.get("id")): s for s in project.data.get("shots") or []}
    for sid, old in (entry.get("changes") or {}).items():
        s = by_id.get(sid)
        if s is None:
            continue
        # 批量之后人已按新值定稿的镜不动：撤销字段会让文档与已通过的产物错位，
        # 而锁只能由人解
        if any(review.is_locked(s, st) for st in stages):
            skipped_locked += 1
            continue
        s[field] = old
        if mark_retake:
            for st in stages:
                review.set_state(s, st, "retake", note=f"撤销批量修改 {entry['id']}（{field}）")
        restored += 1
    log.remove(entry)
    project.save()
    return {"op_id": entry["id"], "field": field, "restored": restored,
            "skipped_locked": skipped_locked}


def history(project) -> list[dict]:
    return list(project.data.get("batch_ops") or [])
