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

"""视频请求的参考图装配单源（RefPlan）。

一镜「发哪几张图、什么顺序、各自什么职责」有五个消费点：`_sheets_for` 的
manifest、工作线程的 `ref_images`、PromptEnvelope 的 references、Studio 预览的
`@图片N → 文件` 映射、provider 的 `content[]`。若五处各自重建，任一处不同步时
请求照发、账照计、零报错，模型按错位的职责句用图——最贵的一类静默失效。
本对象在计划期构造一次，五处只消费。

`@图片N` 编号真源：`content[]` 里图片出现的顺序（text 不占号）——`image` 参数
在前、`ref_images` 按传入顺序在后。`manifest` 与之逐位对应；`refs_for` 产出的
就是 `ref_images` 实参。

路线（route）：
  A  image=分镜图（现行为）
  B  image=场景基准图 + 简笔板（分镜图整个不进请求）
  C  B 去掉板
B/C 只在写实档（identity_sheet）的降级阶梯上出现；首位 manifest kind 随 route
切换（A=frame，B/C=scene_base），两者都是占号不产句的占位档。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 引擎侧 ref_images 硬上限：官方全图 ≤9，image 参数另占一位，留一位余量。
# 超限不是裁剪问题而是错位问题——seedance 侧按 [:7] 静默截断，manifest 仍是
# 全量，职责句就会点名一张没进 content[] 的图。构造期直接拒绝。
MAX_REF_IMAGES = 7

# manifest 里合法的 kind 全集：占位/专写档 ∪ 职责档（sheet_binding_clause 按表
# 产句）。注意口径差：prompts._PLACEHOLDER_KINDS 不含 tail——tail 在提示词侧有
# 专写的承接句，这里它只是「不属设定图行」的结构档。表外 kind 在提示词侧抛
# PromptContractError，这里在构造期就拦。
_SHEET_KINDS = frozenset({"character", "scene", "scene_main",
                          "scene_top", "scene_top_main", "prop"})


@dataclass(frozen=True)
class RefPlan:
    """一镜参考装配的不可变计划。`rows` = `_video_sheet_refs` 的 (kind, 名, 路径)。

    `tails` 按比例齐备或整体为空（全称量词纪律，与 tailrelay.disk_tails 同源）；
    manifest 只有一份——尾帧逐比例换文件不换位置。
    """
    route: str = "A"
    board: str | None = None
    tails: dict = field(default_factory=dict)
    rows: tuple = ()
    dropped: tuple = ()

    def __post_init__(self):
        for k, _n, _p in self.rows:
            if k not in _SHEET_KINDS:
                raise ValueError(f"RefPlan 不认识的设定图 kind: {k}")
        refs = self.refs_for(next(iter(self.tails), None))
        if len(refs) > MAX_REF_IMAGES:
            raise ValueError(
                f"参考图 {len(refs)} 张超出上限 {MAX_REF_IMAGES}——配额算术漏了"
                "板或尾帧的占位（provider 会静默截断，职责句随之错位）")
        if len(set(refs)) != len(refs):
            raise ValueError("参考图路径重复——同一张图占两个图号，职责句自相矛盾")

    # ---- 结构 ----
    @property
    def has_tail(self) -> bool:
        return bool(self.tails)

    @property
    def sheet_paths(self) -> list:
        return [p for _k, _n, p in self.rows]

    @property
    def manifest(self) -> list:
        """`[(kind, 名), …]`，与 `[image] + refs_for(asp)` 逐位等长。"""
        head = "frame" if self.route == "A" else "scene_base"
        return ([(head, "")]
                + ([("board", "")] if self.board else [])
                + ([("tail", "")] if self.tails else [])
                + [(k, n) for k, n, _p in self.rows])

    def refs_for(self, asp) -> list:
        """该比例下真发的 `ref_images` 实参（不含 image 参数那一张）。"""
        return ([self.board] if self.board else []) \
            + ([self.tails[asp]] if self.tails and asp in self.tails else []) \
            + self.sheet_paths

    # ---- 消费面 ----
    def preview(self, image, asp) -> list:
        """Studio 预览的 `@图片N → 文件` 映射（编号与 manifest 同一次装配）。"""
        paths = [image] + self.refs_for(asp)
        return [{"no": i + 1, "kind": k, "name": n,
                 "path": str(p) if p else None}
                for i, ((k, n), p) in enumerate(zip(self.manifest, paths))]

    def at(self, no: int, *, image=None, asp=None) -> dict:
        """`@图片no`（1 起）→ {kind, name, path}，供 content[N] 错误下标翻译。

        `asp` 缺省取任一在册尾帧比例——尾帧在场时不占位会让 refs 比 manifest
        短一位，末项被 zip 静默截掉、翻译成 unknown。"""
        if asp is None and self.tails:
            asp = next(iter(self.tails))
        entries = list(zip(self.manifest, [image] + self.refs_for(asp)))
        if not (1 <= int(no) <= len(entries)):
            return {"kind": "unknown", "name": "", "path": None}
        (k, n), p = entries[int(no) - 1]
        return {"kind": k, "name": n, "path": p}
