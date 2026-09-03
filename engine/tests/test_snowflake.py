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

"""storage.snowflake 行为守卫——全仓数据库主键的唯一生成点。

ID 生成器出错不会当场炸：重复主键要等到 mysql 按唯一键 upsert 互相覆盖时
才以「两行数据变一行」的面目暴露，且无从回溯。守三件事：并发唯一（竞态）、
时钟回拨不倒退（行为）、毫秒内序列耗尽借下一毫秒（资源边界）。
时钟全部打桩，零真实等待。
"""
from __future__ import annotations

import threading
import unittest

from kinema.storage.snowflake import _Snowflake

_WORKER_BITS, _SEQ_BITS = 10, 12


class TestSnowflake(unittest.TestCase):
    def test_concurrent_ids_are_unique(self):
        g = _Snowflake(worker_id=7)
        ids: list[int] = []
        lock = threading.Lock()

        def grab():
            got = [g.next_id() for _ in range(500)]
            with lock:
                ids.extend(got)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), 4000)
        self.assertEqual(len(set(ids)), 4000, "并发下出现重复主键")
        self.assertEqual((ids[0] >> _SEQ_BITS) & ((1 << _WORKER_BITS) - 1), 7,
                         "工作机器号未落进 ID 的机器位")

    def test_clock_rollback_never_goes_backwards(self):
        """回拨期间按上一毫秒继续发号（序列递增），ID 恒严格递增。
        真实场景是 NTP 校时把本机时钟往回拨——生成器若跟着倒退，
        新 ID 会小于已入库的旧 ID，趋势递增与按时间反解全部作废。"""
        g = _Snowflake(worker_id=1)
        now = [1_000_000]
        # 实例属性遮蔽静态方法，纯打桩。别把本用例扩到同毫秒 >4096 次发号：
        # 时钟恒不前进会让「借毫秒」的自旋永不返回（那条边界归下一个用例）
        g._now_ms = lambda: now[0]
        a = g.next_id()
        now[0] = 999_900             # 回拨 100ms
        b = g.next_id()
        self.assertGreater(b, a, "时钟回拨后 ID 倒退")
        self.assertEqual(b >> (_WORKER_BITS + _SEQ_BITS),
                         a >> (_WORKER_BITS + _SEQ_BITS),
                         "回拨期间应钉在上一毫秒的时间位上，不得前进或倒退")

    def test_sequence_exhaustion_borrows_the_next_millisecond(self):
        """同一毫秒发满 4096 个后必须自旋借下一毫秒，而不是绕回 seq=0 重发。
        绕回即重复主键——这正是 12 位序列的边界，唯一一处会静默撞号的地方。"""
        g = _Snowflake(worker_id=1)
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            # 前 4097 次读钟停在同一毫秒（第 4097 次触发序列绕回进自旋），
            # 自旋内的下一次读钟才翻页
            return 5_000_000 if calls["n"] <= 4097 else 5_000_001

        g._now_ms = fake
        ids = [g.next_id() for _ in range(4097)]
        self.assertEqual(len(set(ids)), 4097, "序列耗尽后出现重复主键")
        self.assertEqual(ids, sorted(ids), "借毫秒后 ID 失去递增性")
        self.assertEqual(ids[-1] >> (_WORKER_BITS + _SEQ_BITS),
                         (ids[0] >> (_WORKER_BITS + _SEQ_BITS)) + 1,
                         "第 4097 个 ID 应落在借来的下一毫秒")
