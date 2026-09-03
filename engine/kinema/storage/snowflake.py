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

"""雪花 ID 生成器（Snowflake）—— 数据库主键。

标准 64 位布局：1 位符号 + 41 位毫秒时间戳 + 10 位工作机器号 + 12 位毫秒内序列。
趋势递增、全局唯一、可反解出生成时间；用 BIGINT 存储，兼容 Java/BladeX 生态。
工作机器号取 <主机 MAC ^ 进程号> 低 10 位（本地工具场景无需注册中心分配）。
"""
from __future__ import annotations

import os
import threading
import time
import uuid

_EPOCH = 1288834974657          # Twitter 起始纪元（2010-11-04），行业通用
_WORKER_BITS = 10
_SEQ_BITS = 12
_MAX_SEQ = (1 << _SEQ_BITS) - 1


class _Snowflake:
    def __init__(self, worker_id: int | None = None):
        if worker_id is None:
            worker_id = (uuid.getnode() ^ os.getpid()) & ((1 << _WORKER_BITS) - 1)
        self.worker_id = worker_id
        self._seq = 0
        self._last_ts = -1
        self._lock = threading.Lock()

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000

    def next_id(self) -> int:
        with self._lock:
            ts = self._now_ms()
            if ts < self._last_ts:            # 时钟回拨：等到追平（本地场景回拨幅度极小）
                ts = self._last_ts
            if ts == self._last_ts:
                self._seq = (self._seq + 1) & _MAX_SEQ
                if self._seq == 0:            # 当前毫秒序列耗尽 → 借下一毫秒
                    while ts <= self._last_ts:
                        ts = self._now_ms()
            else:
                self._seq = 0
            self._last_ts = ts
            return ((ts - _EPOCH) << (_WORKER_BITS + _SEQ_BITS)) \
                | (self.worker_id << _SEQ_BITS) | self._seq


_generator = _Snowflake()


def next_id() -> int:
    """取一个新的雪花 ID（进程内线程安全）。"""
    return _generator.next_id()
