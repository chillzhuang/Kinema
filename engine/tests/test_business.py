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

"""kinema.business 单元测试：台账数学——双轨成本、废片、omt 语义、运营指标。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kinema import business
from tests.support import LocalBackendEnv


def _chapter_data() -> dict:
    return {
        "shots": [
            {"id": "s1", "dur": 4.0,
             "versions": {"image": [{"v": 1, "params": {"cost": 0.3}},
                                    {"v": 2, "params": {"cost": 0.5}}]}},
            {"id": "s2", "dur": 6.0,                       # 弃用镜：不计镜数/时长
             "review": {"shot": {"state": "omt"}},
             "versions": {"clip": [{"v": 1, "params": {"cost": 1.2}}]}},
            {"id": "s3", "dur": 2.0,
             "versions": {"audio": [{"v": 1}]}},           # 无 params.cost → 0
        ],
        "cost_estimate": {"video": {"amount": 12.5, "at": "2026-07-01"}},
        "cost": {"image": 1.0, "tts": 0.5, "currency": "CNY"},
    }


class TestChapterLedger(unittest.TestCase):
    def test_series_cost_is_listed_and_folded_into_the_total(self):
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        with tempfile.TemporaryDirectory() as d:
            from kinema.workspace import Workspace
            ws = Workspace.open(str(Path(d) / "ws"))
            s = ws.create_project("台账", pid="ledger")
            s.add_cost("image", 1.2)
            s.add_cost("tts", 0.3)
            s.save()
            tot = business.project_ledger(ws, s.pid)["totals"]
        self.assertEqual(tot["series"], {"image": 1.2, "tts": 0.3})
        self.assertEqual(tot["series_total"], 1.5)
        self.assertEqual(tot["actual"], 1.5)
        self.assertEqual(tot["actual_chapters"], 0)

    def test_estimate_and_actual_dual_track(self):
        led = business.chapter_ledger(_chapter_data())
        self.assertEqual(led["estimate_video"], 12.5)
        self.assertEqual(led["estimate_at"], "2026-07-01")
        self.assertEqual(led["actual"], {"image": 1.0, "tts": 0.5})   # currency 不入账
        self.assertEqual(led["actual_total"], 1.5)

    def test_waste_is_version_stack_cost_sum(self):
        led = business.chapter_ledger(_chapter_data())
        # 废片 = 全部归档条目 params.cost 之和（含弃用镜；无 cost 记 0）
        self.assertEqual(led["waste"], 2.0)                # 0.3 + 0.5 + 1.2
        self.assertEqual(led["rerolls"], 4)                # 归档条目总数
        self.assertEqual(led["rerolls_by"],                # 按阶段分列：零成本的
                         {"image": 2, "clip": 1, "audio": 1})  # audio 重合成不与付费 clip 混读

    def test_omt_excluded_from_shots_but_sunk_cost_kept(self):
        led = business.chapter_ledger(_chapter_data())
        self.assertEqual(led["shots"], 2)                  # s2 弃用不计镜数
        self.assertEqual(led["omitted"], 1)
        self.assertEqual(led["duration"], 6.0)             # 4.0 + 2.0，不含弃用镜
        self.assertGreaterEqual(led["waste"], 1.2)         # 弃用镜沉没成本仍留账

    def test_empty_chapter(self):
        led = business.chapter_ledger({})
        self.assertEqual(led["shots"], 0)
        self.assertEqual(led["actual_total"], 0)
        self.assertEqual(led["waste"], 0.0)
        self.assertIsNone(led["estimate_video"])


class TestProjectLedger(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_metrics_per_shot_and_waste_ratio(self):
        from kinema.workspace import Workspace
        ws = Workspace.open(str(self.root))
        s = ws.create_project("Ledger Demo")
        s.create_chapter("第一章")
        ws.store.save_chapter(s.pid, "ch01", _chapter_data())

        led = business.project_ledger(ws, s.pid)
        self.assertEqual(led["chapters"][0]["chapter"], "ch01")
        tot = led["totals"]
        self.assertEqual(tot["shots"], 2)
        self.assertEqual(tot["estimate_video"], 12.5)
        self.assertEqual(tot["actual"], 1.5)
        self.assertEqual(tot["waste"], 2.0)
        self.assertEqual(tot["rerolls"], 4)
        self.assertEqual(tot["cost_per_shot"], round(1.5 / 2, 4))
        self.assertEqual(tot["rerolls_per_shot"], 2.0)     # 4 / 2
        self.assertEqual(tot["waste_ratio"], round(2.0 / 1.5, 4))

    def test_zero_cost_project_avoids_division_by_zero(self):
        from kinema.workspace import Workspace
        ws = Workspace.open(str(self.root))
        s = ws.create_project("Empty Demo")
        s.create_chapter("空章")                            # 无 shots 无 cost
        tot = business.project_ledger(ws, s.pid)["totals"]
        self.assertEqual(tot["cost_per_shot"], 0.0)
        self.assertEqual(tot["waste_ratio"], 0.0)
        self.assertEqual(tot["shots"], 0)


if __name__ == "__main__":
    unittest.main()

class TestLedgerColumnsAreComparable(unittest.TestCase):
    """预估列只算视频，实际必须并排给同口径的一列。两列口径不同并排展示，
    差额（图/配音/音乐）会被读成计费溢出。"""

    def test_actual_video_sits_beside_the_video_estimate(self):
        from kinema.business import chapter_ledger
        row = chapter_ledger({"shots": [{"id": 1, "dur": 4.0}],
                              "cost_estimate": {"video": {"amount": 21.5}},
                              "cost": {"video": 21.5, "image": 3.3,
                                       "currency": "CNY"}})
        self.assertEqual(row["estimate_video"], row["actual_video"])
        self.assertGreater(row["actual_total"], row["actual_video"])
