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

"""运镜库漂移守卫（`pipeline/camera.py`）。

**头号用例是逐字节比对 storyboard.md**：21 条复用运镜（经典技法 12 + 大师签名 9）
的 `label`/`label_en`/`phrase`/`phrase_en`/`tier` 必须与
`.claude/skills/kinema/references/storyboard.md`《进阶运镜预设库》表格
**一字不差**——那张表是 Skill 指挥层手写 `shots[].camera` 时的取词表，控制台
3D 选中同名 preset 写进去的必须是同一句话。两边一旦分叉，同一个「缓慢环绕」
在手写与 3D 两条路径上会给模型两种指令，而这种分叉只会在成片里才被发现。

其余用例守 3D 求值契约（keys 单调/t 端点/fov 区间/枚举合法/orbit 与首末关键帧
自洽）与目录形态（catalog 键齐备、与 CAMERA_PRESETS 锁步）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from kinema.pipeline import camera

STORYBOARD = (Path(__file__).resolve().parents[2]
              / ".claude" / "skills" / "kinema" / "references" / "storyboard.md")

# storyboard 表格里的 21 条 → camera.py 的 key（人工维护的对照，正是要被守的那层映射）
REUSED: dict[str, str] = {
    # 经典技法 12
    "希区柯克变焦": "dolly_zoom",
    "焦点转移": "rack_focus",
    "升镜揭示": "crane_reveal",
    "缓慢环绕": "slow_orbit",
    "侧向跟拍": "tracking",
    "FPV 穿越": "fpv",
    "一镜到底": "oner",
    "机械臂扫摆": "robotic_arm",
    "手持纪实": "handheld",
    "甩镜": "whip_pan",
    "急推": "crash_zoom",
    "子弹时间": "bullet_time",
    # 大师签名 9
    "迈克尔·贝英雄环绕": "bay_orbit",
    "斯皮尔伯格惊愕推近": "spielberg_push",
    "库布里克对称推进": "kubrick_push",
    "斯派克·李滑行": "spike_lee",
    "卢贝兹基漂浮": "lubezki_float",
    "小津低机位": "ozu",
    "老男孩横移": "side_scroll",
    "韦斯·安德森甩摇": "wes_whip",
    "前景擦镜": "foreground_wipe",
}
MARK_TO_TIER = {v: k for k, v in camera.TIERS.items()}


def _parse_storyboard() -> dict[str, dict]:
    """解析《进阶运镜预设库》两张表 → {中文名: {label_en, phrase, phrase_en, tier}}。

    判据刻意宽松（只认「五列 + 第四列是 ●▲■」），这样表格加列/换顺序时是**解析不到**
    而非静默错配——解析不到会让 21 条对不齐、用例直接红，比悄悄放行安全。
    """
    rows: dict[str, dict] = {}
    for line in STORYBOARD.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[3] not in camera.TIERS.values():
            continue
        parts = [x.strip() for x in cells[0].split("<br>")]
        rows[parts[0]] = {"label_en": parts[1] if len(parts) > 1 else None,
                          "phrase": cells[1], "phrase_en": cells[2],
                          "tier": MARK_TO_TIER[cells[3]]}
    return rows


@unittest.skipUnless(STORYBOARD.is_file(), "storyboard.md 不在（引擎被单独分发时跳过）")
class TestStoryboardVerbatim(unittest.TestCase):
    """21 条复用运镜与 storyboard.md 逐字节一致——本模块的头号守卫。"""

    @classmethod
    def setUpClass(cls):
        cls.rows = _parse_storyboard()

    def test_storyboard_still_has_21_presets(self):
        # 解析器失灵（表格改结构）时先在这里红，避免下面的逐条比对因"没解析到"而空过
        self.assertEqual(len(self.rows), 21,
                         f"storyboard 运镜表解析到 {len(self.rows)} 条（应为 21）：{list(self.rows)}")
        self.assertEqual(set(self.rows), set(REUSED),
                         "storyboard 运镜表与 REUSED 映射分叉——"
                         f"多={set(self.rows) - set(REUSED)} 少={set(REUSED) - set(self.rows)}")

    def test_phrase_and_label_byte_identical(self):
        for zh, key in REUSED.items():
            with self.subTest(preset=key):
                p = camera.CAMERA_PRESETS[key]
                row = self.rows[zh]
                self.assertEqual(p["label"], zh)
                self.assertEqual(p["phrase"], row["phrase"],
                                 f"{key} 的中文措辞与 storyboard 分叉")
                self.assertEqual(p["phrase_en"], row["phrase_en"],
                                 f"{key} 的英文措辞与 storyboard 分叉")
                if row["label_en"]:
                    self.assertEqual(p["label_en"], row["label_en"])

    def test_tier_matches_storyboard(self):
        # 风险档是纪律 UI 的依据（▲提示 dur≥5s、■藏开关后）——不能只在一边改
        for zh, key in REUSED.items():
            with self.subTest(preset=key):
                self.assertEqual(camera.CAMERA_PRESETS[key]["tier"], self.rows[zh]["tier"])


class TestPresetShape(unittest.TestCase):
    """3D 求值契约：控制台按这些不变量飞相机，写坏一处就是一整镜白渲。"""

    def test_thirty_six_presets_unique_labels(self):
        self.assertEqual(len(camera.CAMERA_PRESETS), 36)
        labels = [p["label"] for p in camera.CAMERA_PRESETS.values()]
        self.assertEqual(len(set(labels)), 36, "运镜中文名重复（选择器会出现两个同名项）")

    def test_enums_are_legal(self):
        for key, p in camera.CAMERA_PRESETS.items():
            with self.subTest(preset=key):
                self.assertIn(p["rig"], camera.RIGS)
                self.assertIn(p["tier"], camera.TIERS)
                self.assertIn(p["ease"], camera.EASES)
                self.assertIn(p.get("look", "keys"), camera.LOOKS)
                self.assertIn(p["group"], camera.GROUPS)

    def test_keys_monotonic_and_span_full_range(self):
        for key, p in camera.CAMERA_PRESETS.items():
            with self.subTest(preset=key):
                ks = p["keys"]
                self.assertGreaterEqual(len(ks), 2, "至少要有起止两个关键帧")
                ts = [k["t"] for k in ks]
                self.assertEqual(ts[0], 0.0, "首关键帧必须落在 t=0")
                self.assertEqual(ts[-1], 1.0, "末关键帧必须落在 t=1")
                self.assertEqual(ts, sorted(ts), "关键帧 t 必须单调不减")
                for k in ks:
                    self.assertEqual(len(k["pos"]), 3)
                    self.assertEqual(len(k["target"]), 3)

    def test_fov_within_lens_range(self):
        # 10~90 度 ≈ 135mm 长焦 ~ 16mm 超广；越界不是镜头感而是求值写错
        for key, p in camera.CAMERA_PRESETS.items():
            for k in p["keys"]:
                with self.subTest(preset=key, t=k["t"]):
                    self.assertGreaterEqual(k["fov"], 10)
                    self.assertLessEqual(k["fov"], 90)

    def test_duration_sane_and_advanced_long_enough(self):
        for key, p in camera.CAMERA_PRESETS.items():
            with self.subTest(preset=key):
                self.assertGreater(p["duration"], 0)
                self.assertLessEqual(p["duration"], 15, "单段 previz 不超过 Seedance 15s 上限")
        # storyboard 纪律：▲ 进阶档建议 dur≥5s（whip/crash 那种「快」属于 ■ 档）
        for key in camera.keys_by_tier("advanced"):
            with self.subTest(preset=key):
                self.assertGreaterEqual(camera.CAMERA_PRESETS[key]["duration"], 5.0,
                                        "▲ 进阶档默认时长应 ≥5s（storyboard 四戒）")

    def test_orbit_path_agrees_with_endpoint_keys(self):
        """orbit 族的 path 是位置真源，keys 只是回退/预览——两者必须自洽。

        不自洽的后果很隐蔽：支持 path 的求值器飞一条弧线，回退到 keys 的场合
        （或导出静态预览）飞另一条直线，同一个 preset 在两处看起来是两个运镜。
        """
        import math
        for key, p in camera.CAMERA_PRESETS.items():
            path = p.get("path")
            if not path or path.get("type") != "orbit":
                continue
            with self.subTest(preset=key):
                r, h = path["radius"], path["height"]
                h_end = path.get("height_end", h)
                for t, ang, hh in ((0, path["az_start"], h), (1, path["az_end"], h_end)):
                    k = next(x for x in p["keys"] if x["t"] == float(t))
                    rad = math.radians(ang)
                    self.assertAlmostEqual(k["pos"][0], r * math.sin(rad), places=1)
                    self.assertAlmostEqual(k["pos"][1], hh, places=1)
                    self.assertAlmostEqual(k["pos"][2], r * math.cos(rad), places=1)

    def test_dolly_zoom_is_the_only_scale_locked_one(self):
        # lock_subject_scale 是 fov 由距离推导（vertigo 耦合）的唯一特例分支；
        # 多一个 preset 打开它，就多一处 fov 关键帧被静默忽略
        locked = [k for k, p in camera.CAMERA_PRESETS.items() if p.get("lock_subject_scale")]
        self.assertEqual(locked, ["dolly_zoom"])

    def test_phrase_never_stacks_two_rigs(self):
        """四戒之一：一个 preset = 一个主运镜，措辞里不许出现第二个 rig 的技法名。

        叠运镜让 Seedance 崩——而 `camera` 是逐字进提示词的，措辞里塞两个技法
        等于绕过 UI 的「灰置叠加」纪律从数据层把两个运镜叠了。
        """
        # 只挑互斥性强、彼此不可能同镜出现的技法名（"推近"与"环绕"可共存于描述性从句，
        # 故取整词级的强信号词）
        exclusive = ("希区柯克变焦", "子弹时间", "一镜到底", "甩镜", "急推", "手持")
        for key, p in camera.CAMERA_PRESETS.items():
            hits = [w for w in exclusive if w in p["phrase"]]
            with self.subTest(preset=key):
                self.assertLessEqual(len(hits), 1,
                                     f"{key} 的措辞同时点名了多个主运镜: {hits}")


class TestCatalog(unittest.TestCase):
    def test_catalog_locksteps_with_presets(self):
        cat = camera.catalog()
        self.assertEqual([c["key"] for c in cat], list(camera.CAMERA_PRESETS))
        for row in cat:
            with self.subTest(preset=row["key"]):
                self.assertEqual(set(row), set(camera._CATALOG_KEYS))
                self.assertIn(row["tier_mark"], camera.TIERS.values())
                self.assertEqual(row["tracks_subject"], row["look"] == "subject")

    def test_catalog_is_json_serialisable_and_deep_copied(self):
        import json
        cat = camera.catalog()
        json.dumps(cat, ensure_ascii=False, allow_nan=False)   # 下发前端必须是合法 JSON
        cat[0]["keys"][0]["fov"] = 999                          # 改目录副本
        self.assertNotEqual(
            camera.CAMERA_PRESETS[cat[0]["key"]]["keys"][0]["fov"], 999,
            "catalog() 返回的必须是副本——前端/调用方改一下就污染真源是灾难")

    def test_phrase_of_and_get(self):
        self.assertEqual(camera.phrase_of("slow_orbit"),
                         camera.CAMERA_PRESETS["slow_orbit"]["phrase"])
        self.assertEqual(camera.phrase_of("slow_orbit", "en"),
                         camera.CAMERA_PRESETS["slow_orbit"]["phrase_en"])
        self.assertEqual(camera.phrase_of("不存在的运镜"), "",
                         "未知 key 必须回空串——写一句错的运镜比不写更贵")
        self.assertIsNone(camera.get("不存在的运镜"))
        self.assertEqual(camera.get("push_in")["key"], "push_in")

    def test_groups_cover_every_preset(self):
        seen = {p["group"] for p in camera.CAMERA_PRESETS.values()}
        self.assertEqual(seen, set(camera.GROUPS), "有 preset 落在未登记的分组里")


if __name__ == "__main__":
    unittest.main()
