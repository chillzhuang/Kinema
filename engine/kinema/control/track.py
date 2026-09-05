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

"""多人跟踪 —— 关键点框上的贪心 IoU 匹配。

姿态库自带的 tracker 用 track id 直接索引关键点数组，人数变化时越界即静默返回
未重排的结果——多人场景下骨架会在两个人之间跳。这一份自己写、只做一件事。
"""
from __future__ import annotations

from .geometry import iou, kp_bbox
from .params import TRACK_IOU, TRACK_MAX_MISS


class Tracker:
    """逐帧把检测到的人配到已有 track 上。

    一个人短暂被另一个人挡住时检测会掉几帧，故失配的 track 保留
    `TRACK_MAX_MISS` 帧再删——直接删会让同一个人在遮挡后拿到新 id，
    骨骼颜色随之整个换掉。
    """

    def __init__(self) -> None:
        self.tracks: dict[int, dict] = {}
        self.next_id = 0

    def update(self, kps: list) -> list[int]:
        """返回与 `kps` 等长、按提交顺序对齐的 track id 列表。"""
        boxes = [kp_bbox(kp) for kp in kps]
        ids = [-1] * len(kps)
        pairs = [(iou(t["box"], b), tid, j)
                 for tid, t in self.tracks.items()
                 for j, b in enumerate(boxes) if b is not None]
        pairs.sort(reverse=True)
        used_t: set[int] = set()
        used_d: set[int] = set()
        for score, tid, j in pairs:
            if score < TRACK_IOU:
                break
            if tid in used_t or j in used_d:
                continue
            used_t.add(tid)
            used_d.add(j)
            ids[j] = tid
            self.tracks[tid] = {"box": boxes[j], "miss": 0}
        for tid in list(self.tracks):
            if tid not in used_t:
                self.tracks[tid]["miss"] += 1
                if self.tracks[tid]["miss"] > TRACK_MAX_MISS:
                    del self.tracks[tid]
        for j, b in enumerate(boxes):
            if ids[j] == -1 and b is not None:
                ids[j] = self.next_id
                self.tracks[self.next_id] = {"box": b, "miss": 0}
                self.next_id += 1
        return ids
