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

"""决策审计守卫。

核心守卫是 `test_decisions_survive_engine_save`：指挥层追加的决策，必须扛得住
引擎长任务用旧内存副本逐镜 `project.save()` 的整份覆写——与 review/comments
被吞同属一个型号的风险，唯一的治法是登记进 `_DOC_HUMAN_KEYS`。

第二条守卫是合并规则：必须是**按 id 取并集**，不是整键替换。两侧同时 append 时
整键替换会丢掉一边，「按 id 追加去重」的语义随之失效。
"""
from __future__ import annotations

import json
import tempfile
import unittest

from kinema.errors import ProjectError
from pathlib import Path

from kinema import decisions as dec
from kinema.project import Project

from tests.support import LocalBackendEnv


def _doc(**kw) -> dict:
    d = {"id": "ch01", "title": "第一集", "shots": [{"id": 1, "narration": "开场"}]}
    d.update(kw)
    return d


class TestDecisionEntry(unittest.TestCase):
    def test_add_shapes_the_entry(self):
        doc = _doc()
        e = dec.add(doc, choice="第3镜改远景", alternatives=["中景", "特写"],
                    why="突出孤独感", confidence="high")
        self.assertEqual(doc["decisions"], [e])
        self.assertTrue(e["id"])
        self.assertEqual(e["choice"], "第3镜改远景")
        self.assertEqual(e["alternatives"], ["中景", "特写"])
        self.assertEqual(e["confidence"], "high")
        self.assertTrue(e["at"])

    def test_default_confidence_and_validation(self):
        doc = _doc()
        self.assertEqual(dec.add(doc, choice="x")["confidence"], dec.DEFAULT_CONFIDENCE)
        with self.assertRaises(ProjectError):
            dec.add(doc, choice="   ")                       # 空决策没有留痕价值
        with self.assertRaises(ProjectError):
            dec.add(doc, choice="x", confidence="0.9")       # 枚举，不收数值

    def test_entry_is_json_safe(self):
        """审计日志必须能原样进 project.json（NaN/Infinity 不是合法 JSON）。"""
        doc = _doc()
        dec.add(doc, choice="c", alternatives=["a"], why="w")
        json.loads(json.dumps(doc, ensure_ascii=False))      # 不抛即合法

    def test_entries_tolerates_garbage(self):
        self.assertEqual(dec.entries({"decisions": "坏值"}), [])
        self.assertEqual(dec.entries({}), [])
        self.assertEqual(len(dec.entries({"decisions": [{"id": "1"}, "x"]})), 1)

    def test_report_lines_render(self):
        doc = _doc()
        dec.add(doc, choice="用 dubbed", alternatives=["kenburns"], why="要对口型")
        out = "\n".join(dec.report_lines(doc))
        self.assertIn("用 dubbed", out)
        self.assertIn("kenburns", out)
        self.assertIn("要对口型", out)
        self.assertIn("暂无", "\n".join(dec.report_lines(_doc())))


class TestUnionByID(unittest.TestCase):
    def test_append_dedup_by_id_in_merge_layer(self):
        """并集去重必须实现在合并层：两侧各 append 一条，两条都要留下。"""
        a = {"id": "1", "choice": "共有"}
        mem = [a, {"id": "2", "choice": "引擎侧"}]
        disk = [a, {"id": "3", "choice": "指挥层侧"}]
        out = dec.union_by_id(mem, disk)
        self.assertEqual([e["id"] for e in out], ["1", "2", "3"])

    def test_union_handles_none_and_garbage(self):
        self.assertEqual(dec.union_by_id(None, None), [])
        self.assertEqual(dec.union_by_id([{"id": "1"}], None), [{"id": "1"}])
        self.assertEqual(dec.union_by_id(["坏值"], [{"id": "1"}]), [{"id": "1"}])

    def test_entry_without_id_gets_content_derived_key(self):
        """**指数膨胀守卫**：缺 id 的条目（指挥层裸改 JSON 的常态）必须去重。

        去重键只认 id 的话，同一条无 id 条目在 union 两侧各留一份 → 每次 save
        条数翻倍（一趟 8 镜 run 即可膨胀到百万条量级），且 append-only
        取并集使其无法经引擎收回，只能手工改 JSON 救。
        """
        hand = {"choice": "本集用 kenburns", "why": "零成本", "confidence": "high"}
        out = dec.union_by_id([dict(hand)], [dict(hand)])
        self.assertEqual(len(out), 1, "无 id 条目没去重——条数会按 2^N 膨胀")
        self.assertTrue(out[0]["id"].startswith("sha256:"), "应就地补上内容派生 id")
        self.assertEqual(out[0]["choice"], hand["choice"])

    def test_derived_key_matches_healed_entry(self):
        """内存侧还是裸条目、磁盘侧已被上一次 save 补过 id → 仍是同一条。"""
        hand = {"choice": "第3镜改远景", "why": "孤独感"}
        healed = dec.union_by_id([dict(hand)], [])[0]
        self.assertEqual(dec.entry_key(dict(hand)), healed["id"])
        self.assertEqual(len(dec.union_by_id([dict(hand)], [healed])), 1)

    def test_derived_key_distinguishes_different_entries(self):
        """内容不同的无 id 条目不能被并掉（去重不是无差别塌缩）。"""
        a = {"choice": "用 kenburns"}
        b = {"choice": "用 dubbed"}
        self.assertNotEqual(dec.entry_key(a), dec.entry_key(b))
        self.assertEqual(len(dec.union_by_id([a, b], [dict(a), dict(b)])), 2)

    def test_entry_key_prefers_explicit_id(self):
        self.assertEqual(dec.entry_key({"id": "7", "choice": "x"}), "7")
        self.assertEqual(dec.entry_key({"id": "  ", "choice": "x"}),
                         dec.derived_id({"choice": "x"}))


class TestSurviveEngineSave(unittest.TestCase):
    """三方合并层守卫（Project.save 的 _DOC_HUMAN_KEYS / _DOC_APPEND_KEYS）。

    `Project.save` 尾部有持久化钩子（notify_saved），本机 fish 通用变量固化了
    mysql 后端——必须用 LocalBackendEnv 钉死 local，否则本用例会去连库。
    """

    def setUp(self):
        self.backend = LocalBackendEnv()
        self.backend.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ch01.json"

    def tearDown(self):
        self.tmp.cleanup()
        self.backend.restore()

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_decisions_survive_engine_save(self):
        """**核心守卫**：加载 → 磁盘上追加一条 → 引擎 save → 决策仍在。

        时序：gen-image 长任务在 T0 加载整份文档，指挥层 T1 用
        `decision add` 往磁盘追加，长任务 T2 逐镜 save。没有 `_DOC_HUMAN_KEYS`
        登记的话，T2 会用不认识 decisions 的旧内存副本整份覆写，决策消失且零提示。
        """
        self._write(_doc())
        engine = Project.load(self.path)                     # T0：引擎持有旧副本

        side = Project.load(self.path)                       # T1：指挥层 decision add
        added = dec.add(side.data, choice="第3镜改远景", why="突出孤独感")
        side.save()
        self.assertEqual(len(self._read()["decisions"]), 1)

        engine.data["shots"][0]["image"] = "images/shot_1.png"
        engine.save()                                        # T2：引擎逐镜 checkpoint

        disk = self._read()
        self.assertEqual([e["id"] for e in disk.get("decisions", [])], [added["id"]],
                         "引擎 save 把指挥层的决策吞了")
        self.assertEqual(disk["shots"][0]["image"], "images/shot_1.png")

    def test_engine_save_unions_both_sides(self):
        """引擎内存侧与磁盘侧各有新条目 → save 后两条都在（整键替换会丢一边）。"""
        self._write(_doc())
        engine = Project.load(self.path)
        e_mem = dec.add(engine.data, choice="引擎侧决策")     # 内存侧（模拟并发）

        side = Project.load(self.path)
        e_disk = dec.add(side.data, choice="指挥层侧决策")
        side.save()

        engine.save()
        ids = [e["id"] for e in self._read()["decisions"]]
        self.assertIn(e_mem["id"], ids)
        self.assertIn(e_disk["id"], ids)
        self.assertEqual(len(ids), len(set(ids)), "同一条被记了两遍")

    def test_no_decisions_key_when_never_used(self):
        """从没用过决策审计的项目，save 不该凭空写出一个空数组。"""
        self._write(_doc())
        p = Project.load(self.path)
        p.save()
        self.assertNotIn("decisions", self._read())

    def test_repeated_saves_are_idempotent(self):
        """连save 多次不叠加（并集去重的幂等性——逐镜 checkpoint 会 save 很多次）。"""
        self._write(_doc())
        p = Project.load(self.path)
        dec.add(p.data, choice="只此一条")
        for _ in range(5):
            p.save()
        self.assertEqual(len(self._read()["decisions"]), 1)

    def test_handwritten_entry_without_id_never_multiplies(self):
        """**指数膨胀守卫（端到端）**：磁盘上一条**没有 id** 的手写决策，连 save
        五次条数恒为 1。

        schema 把 `id` 标成 [engine-managed]、契约又要求「engine-managed 字段不要
        手写」，指挥层照章办事写出的就是无 id 条目；引擎逐镜 save 一次翻一倍，
        8 镜一趟 run 即可膨胀到百万条、上百 MB——project.json 会直接把
        Studio 的 JSON.parse 打崩。
        """
        self._write(_doc(decisions=[{"choice": "本集用 kenburns", "why": "零成本",
                                     "confidence": "high"}]))
        p = Project.load(self.path)
        for i in range(5):
            p.save()
            self.assertEqual(len(self._read()["decisions"]), 1,
                             f"第 {i + 1} 次 save 后无 id 决策被复制了")
        kept = self._read()["decisions"][0]
        self.assertEqual(kept["choice"], "本集用 kenburns")
        self.assertTrue(kept["id"], "save 后应已补上确定性 id（文档自愈）")

    def test_handwritten_entry_survives_concurrent_engine_save(self):
        """无 id 手写条目 + 引擎旧内存副本并发 save：既不丢、也不翻倍。"""
        self._write(_doc(decisions=[{"choice": "手写决策"}]))
        engine = Project.load(self.path)                     # T0：引擎持有旧副本
        side = Project.load(self.path)                       # T1：指挥层补记一条
        added = dec.add(side.data, choice="命令写的决策")
        side.save()

        engine.data["shots"][0]["image"] = "images/shot_1.png"
        engine.save()                                        # T2：逐镜 checkpoint

        disk = self._read()["decisions"]
        self.assertEqual(len(disk), 2, "并发 save 后条数不对（丢条或翻倍）")
        self.assertEqual([e["choice"] for e in disk], ["手写决策", "命令写的决策"])
        self.assertIn(added["id"], [e["id"] for e in disk])


if __name__ == "__main__":
    unittest.main()
