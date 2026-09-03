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

"""预留额度守卫。

三层结构：
  ① `budget` 纯裁决层——额度归一（NaN/Infinity/≤0 一律不设限）、已花口径、两级判定；
  ② `cli._will_burn` / `cli._plan_cost` 只读预演层——**零副作用**是这一层的关键：
     预演若复用 `_regen_gate`，会在「钱一分没花」的前提下把旧产物移进版本栈；
  ③ `cli._preflight_spend` 裁决落地——拦得住 + 不落盘（尤其不碰 cost_estimate.video）。

provider 只需要 `billable_seconds` / `effective_price_per_second` / `name` 三样，
本文件就地造一个十行桩（support.py 是通用设施，不为单个用例加 provider 桩）。
"""
from __future__ import annotations

import contextlib
import copy
import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import budget as budget_mod
from kinema import cli
from kinema.errors import KinemaError
from kinema.project import Project
from kinema.pipeline import versioning
from kinema.project import Project
from tests.support import fake_path


class _Prov:
    """视频 provider 的最小替身：只提供计费口径与单价（不联网、不生成）。"""
    name = "stub"

    def __init__(self, price=1.0):
        self.effective_price_per_second = price
        self.price_per_second = price

    def billable_seconds(self, dur, *, dubbed=False, last_frame=False):
        return max(1, math.ceil(float(dur)))


def _shot(sid, **kw):
    s = {"id": sid, "narration": f"第{sid}镜", "dur": 5.0}
    s.update(kw)
    return s


def _project(tmp: Path, data: dict) -> Project:
    """不落盘造 Project（预演层全程只读；需要 save 的用例自己写盘）。"""
    return Project(tmp / "ch01.json", data)


# ---------------------------------------------------------------------------
# ① 纯裁决层
# ---------------------------------------------------------------------------
class TestBudgetPrimitives(unittest.TestCase):
    def test_limit_normalizes_junk_to_unlimited(self):
        # NaN 的比较恒 False → 静默把闸变成永远放行（表面限额仍在、实际不再拦截），必须归一成 None
        for bad in (None, "", "abc", 0, -3, float("nan"), float("inf")):
            self.assertIsNone(budget_mod.limit(bad), f"{bad!r} 应视为不设限")
        self.assertEqual(budget_mod.limit("12.5"), 12.5)

    def test_spent_total_matches_add_cost_semantics(self):
        doc = {"cost": {"currency": "CNY", "video": 3.5, "tts": 1.25, "bad": "x"}}
        self.assertEqual(budget_mod.spent_total(doc), 4.75)
        self.assertEqual(budget_mod.spent_total({}), 0.0)

    def test_verdict_over_budget_counts_already_spent(self):
        doc = {"budget": 10, "cost": {"currency": "CNY", "video": 8}}
        v = budget_mod.verdict(doc, 5.0)
        self.assertTrue(v["over_budget"])          # 8 + 5 > 10
        self.assertEqual(v["remaining"], 2.0)
        self.assertFalse(budget_mod.verdict(doc, 1.0)["over_budget"])   # 8 + 1 ≤ 10

    def test_verdict_ignores_cost_estimate(self):
        """预估侧从不参与裁决——否则 dry-run 跑两遍额度就被吃光。"""
        doc = {"budget": 10, "cost": {"currency": "CNY", "video": 1},
               "cost_estimate": {"video": {"amount": 999, "seconds": 999}}}
        v = budget_mod.verdict(doc, 5.0)
        self.assertFalse(v["over_budget"])
        self.assertEqual(v["spent"], 1.0)

    def test_verdict_cap_is_independent_of_total(self):
        doc = {"budget_per_call": 6}
        v = budget_mod.verdict(doc, 100.0, 8.0)
        self.assertTrue(v["over_cap"])
        self.assertFalse(v["over_budget"])         # 没设 budget → 总额不设限
        self.assertFalse(budget_mod.verdict(doc, 100.0, 6.0)["over_cap"])   # 等于阈不算超

    def test_verdict_fields_are_finite(self):
        """落进日志/异常文案的数只能是有限数（NaN 一旦进 JSON，Studio 整页崩）。"""
        doc = {"budget": float("nan"), "cost": {"video": float("inf")}}
        v = budget_mod.verdict(doc, float("nan"), float("inf"))
        for k in ("estimate", "max_call", "spent"):
            self.assertTrue(math.isfinite(v[k]), f"{k} 不是有限数: {v[k]}")
        self.assertIsNone(v["budget"])


# ---------------------------------------------------------------------------
# ② 只读预演层（_will_burn / _plan_cost）
# ---------------------------------------------------------------------------
class TestWillBurn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _clip(self, name: str) -> str:
        p = self.dir / name
        p.write_bytes(b"clip")
        return str(p)

    def test_will_burn_skips_done_and_existing_clips(self):
        """四道跳过闸：done 锁定 / 弃用 / 转场 / 已有片段（断点续跑的核心）。"""
        shots = [
            _shot(1, review={"clip": {"state": "done"}}),           # 锁定
            _shot(2, review={"shot": {"state": "omt"}}),            # 弃用
            {"id": 3, "kind": "transition", "dur": 1.0},            # 转场
            _shot(4, clips={"16:9": self._clip("s4.mp4")}),         # 片段已在盘
            _shot(5),                                               # 唯一要发的
        ]
        p = _project(self.dir, {"shots": shots, "aspect": "16:9"})
        plan = cli._will_burn(p, p.shots, ["16:9"], False)
        self.assertEqual([s["id"] for s, _a in plan], [5])

    def test_retake_regenerates_even_with_existing_clip(self):
        shots = [_shot(1, review={"clip": {"state": "retake"}}, clips={"16:9": self._clip("s1.mp4")})]
        p = _project(self.dir, {"shots": shots, "aspect": "16:9"})
        plan = cli._will_burn(p, p.shots, ["16:9"], False)
        self.assertEqual([(s["id"], a) for s, a in plan], [(1, ["16:9"])])

    def test_done_survives_force(self):
        """`--force` 也不覆盖 done 锁定镜——预演层与 `_regen_gate` 必须逐条对齐。"""
        p = _project(self.dir, {"shots": [_shot(1, review={"clip": {"state": "done"}})],
                                "aspect": "16:9"})
        self.assertEqual(cli._will_burn(p, p.shots, ["16:9"], True), [])

    def test_will_burn_multiplies_by_aspect_count(self):
        """逐比例一次调用：双比例 = 2 次 = 2 份秒数——漏乘比例数就是报价对半低估。"""
        p = _project(self.dir, {"shots": [_shot(1), _shot(2)], "aspect": "16:9"})
        targets = ["16:9", "9:16"]
        plan = cli._will_burn(p, p.shots, targets, False)
        self.assertEqual([len(a) for _s, a in plan], [2, 2])
        total, calls, max_n = cli._plan_cost(p, plan, _Prov(), mode="native",
                                             native=True, adir=self.dir)
        self.assertEqual((total, calls, max_n), (20, 4, 5))
        # 单比例正好是它的一半
        plan1 = cli._will_burn(p, p.shots, ["16:9"], False)
        self.assertEqual(cli._plan_cost(p, plan1, _Prov(), mode="native",
                                        native=True, adir=self.dir)[0], 10)

    def test_partial_aspect_resume(self):
        """一个比例出了、另一个没出 → 只补没出的那次调用。"""
        shots = [_shot(1, clips={"16:9": self._clip("s1.mp4")})]
        p = _project(self.dir, {"shots": shots, "aspect": "16:9"})
        plan = cli._will_burn(p, p.shots, ["16:9", "9:16"], False)
        self.assertEqual([(s["id"], a) for s, a in plan], [(1, ["9:16"])])

    def test_preflight_has_no_side_effects(self):
        """**预演层最关键的守卫**：预演绝不触发 `versioning.archive`。

        `_regen_gate` 对每个 retake/force 镜会把旧产物**移动**进版本栈 + 改写 JSON。
        预演跑一遍再因超预算中止 = 钱一分没花却先把现场破坏了。
        """
        clip = self._clip("s1.mp4")
        shots = [_shot(1, review={"clip": {"state": "retake"}}, clips={"16:9": clip}, clip=clip),
                 _shot(2)]
        data = {"shots": shots, "aspect": "16:9", "budget": 1000}
        p = _project(self.dir, data)
        before = copy.deepcopy(data)
        with mock.patch.object(versioning, "archive") as arch, \
                contextlib.redirect_stdout(io.StringIO()):
            plan = cli._will_burn(p, p.shots, ["16:9"], True)
            cli._preflight_spend(p, plan, _Prov(), mode="native", native=True,
                                 adir=self.dir, targets=["16:9"])
        arch.assert_not_called()
        self.assertEqual(p.data, before, "预演改动了文档")
        self.assertTrue(Path(clip).is_file(), "预演把旧产物移走了")


# ---------------------------------------------------------------------------
# ③ 事前闸裁决
# ---------------------------------------------------------------------------
class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, data, *, targets=("16:9",), price=1.0, **kw):
        """跑一次事前闸；返回 (project, 打印出来的内容)。stdout 收进缓冲，套件输出保持干净。"""
        p = _project(self.dir, data)
        plan = cli._will_burn(p, p.shots, list(targets), False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._preflight_spend(p, plan, _Prov(price), mode="native", native=True,
                                 adir=self.dir, targets=list(targets), **kw)
        self.log = buf.getvalue()
        return p

    def test_preflight_blocks_when_batch_exceeds_budget(self):
        data = {"shots": [_shot(i) for i in range(1, 6)], "aspect": "16:9",
                "budget": 10, "cost": {"currency": "CNY", "video": 0}}
        with self.assertRaises(KinemaError) as ctx:      # 5 镜 × 5s × ¥1 = ¥25 > 10
            self._run(data)
        msg = str(ctx.exception)
        self.assertIn("一次都没有发出", msg)
        self.assertIn("--approved-only", msg)
        # 事前闸不动台账：钱一分没花，cost 必须原样（这正是它与 add_cost 事后闸的分别）
        self.assertEqual(data["cost"], {"currency": "CNY", "video": 0})

    def test_preflight_passes_within_budget(self):
        data = {"shots": [_shot(1)], "aspect": "16:9", "budget": 100}
        self._run(data)     # 不抛即通过

    def test_preflight_counts_aspects_before_blocking(self):
        """单比例够、双比例不够——低估 bug 若复发，这条会漏放行。"""
        data = {"shots": [_shot(1), _shot(2)], "aspect": "16:9", "budget": 15}
        self._run(data, targets=("16:9",))                  # 10 ≤ 15
        with self.assertRaises(KinemaError):
            self._run(data, targets=("16:9", "9:16"))       # 20 > 15

    def test_preflight_does_not_write_cost_estimate(self):
        """preflight 只在内存算：绝不覆写 cost_estimate.video（ledger + 交付 manifest 唯一来源）。"""
        snapshot = {"video": {"amount": 3.0, "seconds": 3, "price_per_second": 1.0,
                              "at": "2026-07-25T00:00:00"}}
        data = {"shots": [_shot(1)], "aspect": "16:9", "budget": 100,
                "cost_estimate": copy.deepcopy(snapshot)}
        p = self._run(data)
        self.assertEqual(p.data["cost_estimate"], snapshot)
        # 没有预估快照的项目也不该被凭空写出一份
        data2 = {"shots": [_shot(1)], "aspect": "16:9", "budget": 100}
        self.assertNotIn("cost_estimate", self._run(data2).data)

    def test_subset_dry_run_never_overwrites_full_estimate(self):
        """dry-run 报价入台账的口径闸：`--approved-only` 与 `--only` 同为镜级
        子集过滤——子集报价落盘会覆盖全片预估，ledger 的「预估(video)」列从此
        对不上任何真实口径（额度被拦后按文档改用 --approved-only 重跑报价，
        恰是最常撞上的场景）。"""
        import contextlib
        import io

        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            img = tmp / "s.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            data = {"motion": "dubbed", "aspect": "16:9", "shots": [
                {"id": 1, "dur": 5.0, "image": str(img), "video_prompt": "x",
                 "review": {"image": {"state": "done"}}},
                {"id": 2, "dur": 5.0, "image": str(img), "video_prompt": "y"}]}
            p = _project(tmp, data)
            store = ConfigStore.load(None)
            with contextlib.redirect_stdout(io.StringIO()):
                stage_gen_video(p, store, ModelRouter(store, force_mock=True),
                                dry_run=True, approved_only=True)
            self.assertNotIn("cost_estimate", p.data,
                             "子集口径的报价不许写进全片预估")

    def test_zero_price_provider_skips_gate(self):
        """单价未配置(=0)不入账也不预留（与 add_cost 的"肯定性零"同口径）。"""
        data = {"shots": [_shot(i) for i in range(1, 9)], "aspect": "16:9", "budget": 1}
        self._run(data, price=0)     # 不抛

    def test_per_call_cap_requires_confirm_spend(self):
        data = {"shots": [_shot(1, dur=12)], "aspect": "16:9", "budget_per_call": 6}
        with self.assertRaises(KinemaError) as ctx:
            self._run(data)
        self.assertIn("--confirm-spend", str(ctx.exception))
        self._run(data, confirm_spend=True)      # 确认后放行

    def test_per_call_cap_warns_but_passes_under_auto(self):
        """run/--auto 下单笔超阈告警放行——硬拦=一条龙死在这里且无解锁路径。"""
        data = {"shots": [_shot(1, dur=12)], "aspect": "16:9", "budget_per_call": 6}
        self._run(data, auto=True)               # 不抛
        self.assertIn("单笔超阈", self.log)      # 但必须留下告警，不能静默放行

    def test_hard_budget_still_blocks_under_auto(self):
        """硬超上限在 run/--auto 下**仍然拦**：放行等于必然爆预算。"""
        data = {"shots": [_shot(i) for i in range(1, 6)], "aspect": "16:9", "budget": 3}
        with self.assertRaises(KinemaError):
            self._run(data, auto=True)


class TestAddCostStillGuards(unittest.TestCase):
    def test_add_cost_still_charges_then_raises(self):
        """事后闸不变：本笔先入账再抛错（钱已经花了必须记上）。

        两道闸方向相反、缺一不可——事前闸拦不到的（逐镜混画风路由到更贵的家、
        provider 实际计费与预估有出入）仍由事后闸兜底。
        """
        with tempfile.TemporaryDirectory() as td:
            p = Project(Path(td) / "ch01.json", {"budget": 5, "shots": []})
            p.add_cost("video", 3.0)                    # 3 ≤ 5，不抛
            self.assertEqual(p.data["cost"]["video"], 3.0)
            with self.assertRaises(KinemaError):
                p.add_cost("video", 4.0)                # 7 > 5 → 抛
            self.assertEqual(p.data["cost"]["video"], 7.0, "本笔必须已入账")


class TestTtsBillingPrecedesEarlyExits(unittest.TestCase):
    """本批 TTS 的实付额必须在任何早退之前入账。

    `total_cost` 在 `parallel.run` 收尾时已是终值，而「部分镜失败」与「缺镜拒拼旁白轨」
    两条早退都会带着已合成那几句的实付额直接抛出去。台账少记会让事前/事后两道额度闸
    按偏低的已花额放行。落点也只许有一个：多处入账即重复计费。
    """

    def test_billing_is_single_and_before_the_failure_raise(self):
        src = (Path(__file__).parents[1] / "kinema" / "cli.py").read_text(encoding="utf-8")
        seg = src.split("def stage_tts(")[1].split("\ndef ")[0]
        self.assertEqual(seg.count('add_cost("tts"'), 1, "TTS 记账落点不止一处")
        bill = seg.index('add_cost("tts"')
        self.assertLess(bill, seg.index("镜配音失败："), "失败早退排在记账之前")
        self.assertLess(bill, seg.index("镜有台词但逐镜 wav 不在盘"),
                        "缺镜拒拼早退排在记账之前")


class TestGateParity(unittest.TestCase):
    """事前/事后两道闸必须共用同一份求和实现——一旦各自求和，字符串化的
    成本值会出现「事前闸计入、事后闸放过」的静默口径分叉。"""

    def test_add_cost_sums_via_spent_total(self):
        doc = {"budget": 3, "cost": {"currency": "CNY", "image": "2.5"}}
        p = Project(Path(fake_path("project.json")), doc)
        with self.assertRaises(KinemaError):
            p.add_cost("tts", 1.0)      # "2.5"（字符串）同样计入，与事前闸一致
        self.assertEqual(budget_mod.spent_total(p.data), 3.5)


if __name__ == "__main__":
    unittest.main()
