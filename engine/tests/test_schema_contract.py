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

"""数据契约守卫：`docs/kinema/project.schema.json` 与实现不许二次漂移。

**显式清单驱动**（不做全量遍历）——`DECLARED_FIELDS` 是一份「已登记进契约的
字段路径 + 归属标记」清单，给引擎新增读写字段时把它追加进来，守卫随之覆盖到
新面。刻意**不做**全量 description 完整性校验：存量 38 个 property 没有
description（characters[].name、props[].kind、cost.* 等），全量校验一落地就是
一片红，把真正该看的漂移淹掉。

标记语义：`ENGINE` = 描述须以 `[engine-managed]` 开头（引擎回填，作者不手写）；
`AUTHOR` = 作者（Skill 指挥层）填写，描述里**不得**出现 `[engine-managed]`。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

SCHEMA_PATH = (Path(__file__).resolve().parents[2]
               / "docs" / "kinema" / "project.schema.json")

ENGINE = "engine"       # [engine-managed]：引擎回填
AUTHOR = "author"       # 作者（指挥层）填写

# 已登记字段清单：(路径, 归属)。路径用 `.` 分层、`[]` 表示进数组 items。
# 新增契约字段时**追加在本清单末尾**，并按所属特性分组、加一行小标题。
#
# `[engine-managed]` 前缀只标**块级**归属：块下子字段的描述一律不带前缀，
# 齐备性交给各特性自己的契约用例（下面逐块注出是哪一个）。
DECLARED_FIELDS: list[tuple[str, str]] = [
    # —— 音频剧本（scored）——
    ("gen", ENGINE),
    ("scored_bgm", AUTHOR),
    ("native_bgm", AUTHOR),
    # —— 音色档案库（选中的每把声音各一条不可变档案）——
    ("voice_bank", ENGINE),
    ("characters[].voice", AUTHOR),
    ("characters[].audition", ENGINE),
    ("characters[].custom_audition", ENGINE),
    ("characters[].gender", AUTHOR),
    ("narrator", AUTHOR),
    ("narrator.voice", AUTHOR),
    ("narrator.audition", ENGINE),
    ("narrator.custom_audition", ENGINE),
    ("shots[].voice_stale", ENGINE),
    ("shots[].voice_stale_prev", ENGINE),
    ("shots[].voice_clip_stale", ENGINE),
    ("shots[].voice_clip_stale_prev", ENGINE),
    # —— 具名场景（取景地）分档：场景不再寄生 props[] ——
    ("scenes", AUTHOR),
    ("scenes[].name", AUTHOR),
    ("scenes[].desc", AUTHOR),
    ("scenes[].keywords", AUTHOR),
    ("scenes[].sheet", ENGINE),
    ("shots[].scenes", AUTHOR),
    # —— 基线（既有字段，立守卫框架用）——
    ("skill", AUTHOR),
    ("moodboard", AUTHOR),
    ("shots[].refs", AUTHOR),
    ("shots[].framing", AUTHOR),
    ("shots[].props", AUTHOR),
    ("shots[].review", ENGINE),
    ("shots[].versions", ENGINE),
    ("shots[].gen", ENGINE),
    ("characters[].versions", ENGINE),
    ("scene_ref_versions", ENGINE),
    ("cost_estimate", ENGINE),
    # —— 结构化分镜字段 ——
    ("shots[].shot_intent", AUTHOR),
    ("shots[].narrative_role", AUTHOR),
    ("shots[].hero_moment", AUTHOR),
    # —— art_direction 风格圣经旋钮 ——
    ("art_direction", AUTHOR),
    ("art_direction.variety", AUTHOR),
    ("art_direction.motion", AUTHOR),
    ("art_direction.density", AUTHOR),
    ("art_direction.avoid", AUTHOR),
    # —— 视频侧 delta 骨架字段 ——
    ("shots[].action", AUTHOR),
    ("shots[].entry_state", AUTHOR),
    ("shots[].end_state", AUTHOR),
    ("shots[].light_shift", AUTHOR),
    # —— 尾帧接力（tail_relay）：章级开关是「你填」；尾帧登记在 gen.clip
    #    （engine-managed 块已整体登记），判据真源 pipeline/tailrelay.py ——
    ("tail_relay", AUTHOR),
    # —— 成片自审 verify ——
    ("verify", ENGINE),
    # —— 角色清单前置（全是「你填」，绝非引擎回填）——
    ("characters[].required_emotions", AUTHOR),
    ("characters[].required_actions", AUTHOR),
    ("characters[].required_views", AUTHOR),
    ("characters[].silhouette_notes", AUTHOR),
    ("characters[].constraints", AUTHOR),
    # —— 角色跨镜一致性判定（引擎产料+回填，指挥层判定）——
    # 只登记块本身；子字段（verdict/score/at/by/note/frame/sheets）由
    # tests/test_consistency.py 守齐备性。
    ("shots[].consistency", ENGINE),
    # —— 配音表现力契约（全是「你填」；引擎只读不回填）——
    ("shots[].delivery", AUTHOR),
    ("shots[].delivery.emphasis", AUTHOR),
    ("shots[].delivery.pause_before", AUTHOR),
    ("shots[].delivery.pause_after", AUTHOR),
    ("shots[].delivery.note", AUTHOR),
    ("voice_performance", AUTHOR),
    ("voice_performance.pacing", AUTHOR),
    ("voice_performance.energy_curve", AUTHOR),
    # —— 预留额度（单笔阈是「你填」，与既有 budget 同族）——
    ("budget_per_call", AUTHOR),
    # —— 决策审计（走 decision add CLI 写入，故标 engine-managed）——
    ("decisions", ENGINE),
    ("decisions[].id", ENGINE),
    ("decisions[].at", ENGINE),
    # —— 参考片读片（系列文档；`study import` 回填，指挥层只读）——
    # 版权护栏（相对路径 / 无 _work 后缀）由 test_study 守卫。
    ("study", ENGINE),
    ("study[].file", ENGINE),
    ("study[].digest", ENGINE),
    ("study[].rhythm", ENGINE),
    # —— 3D 导演控制台 M1（previz 登记 + Seedance V2V）——
    # 三个 shot 字段全由 `previz register` / 控制台回填；`previz_v2v` 是「你填」的
    # 成本开关（与 budget/budget_per_call 同族）；章节级 `previz` 是场景编排快照。
    ("shots[].camera_preset", ENGINE),
    ("shots[].previz", ENGINE),
    ("shots[].last_frame_ref", ENGINE),
    ("previz_v2v", AUTHOR),
    # 章节级场景快照只登记块本身；子字段（fps/actors/paths/cameras/cuts/scene_hash）
    # 由 tests/test_previz.py 守齐备性。
    ("previz", ENGINE),
    # —— 原创小说创作层（novel）——
    # 文字人设四件：人工创作字段——进 sync_design_to_chapters 白名单（系列→章节推送），
    # **绝不进 upsert_entities**（重抽不覆盖），两条纪律由 test_novel 守卫。
    ("characters[].speech_style", AUTHOR),
    ("characters[].personality", AUTHOR),
    ("characters[].arc", AUTHOR),
    ("characters[].taboo_lines", AUTHOR),
    # 文风单点真源（与 style_prompt 同范式：立项定、改它=全局换文风；
    # 防漂靠 baseline 比对，绝不靠每章复述——复述即漂移在文本上同样成立）
    ("narrative_style", AUTHOR),
    ("narrative_style.pov", AUTHOR),
    ("narrative_style.tense", AUTHOR),
    ("narrative_style.voice", AUTHOR),
    ("narrative_style.diction", AUTHOR),
    ("narrative_style.baseline", AUTHOR),
    ("narrative_style.avoid", AUTHOR),
    # 登记块与伏笔账本：经 `novel` CLI 写入故标 engine-managed（同 decisions 惯例）；
    # 子字段沿块级归属惯例不逐一登记，齐备性由 test_novel 契约用例守卫。
    ("novel", ENGINE),
    ("threads", ENGINE),
    # —— 卷/幕规划与角色别名 ——
    # arcs 是长篇的大纲落点（经 `novel arc` 写入故标 engine-managed，同 threads）；
    # 「写到哪一卷」是派生判定绝不落盘，由 test_novel 守卫。
    ("arcs", ENGINE),
    ("characters[].keywords", AUTHOR),
    # —— 小说层商用化 ——
    # 文风**数值**基线：z 分恒现算不落盘，只有这个 μ±σ 向量落盘（同 threads 超期纪律）
    ("narrative_style.baseline_metrics", ENGINE),
    # 在场状态：非 active 不再进「连续缺席」提醒。**你填**（引擎判不出退场是不是剧情安排），
    # 故不标 engine-managed；进 CHAR_SETTABLE 与 char_fields 白名单、绝不进 upsert_entities
    ("characters[].status", AUTHOR),
    # 性别 male|female：试音候选过滤 character_gender 判定链第一环（显式 > 现有音色 >
    # 文本推断）。**你填**（--gender），同 status 纪律：进两张白名单、绝不进 upsert_entities
    ("characters[].gender", AUTHOR),
    # —— 简笔分镜预演板（sketch board，与 previz 并行互斥）——
    # sketch 块整体按「你填」登记：核心是指挥层写的 beats（9 拍逐秒动作设计）；
    # 引擎回填的 sheet 子字段沿块级归属惯例不单独登记（生成元数据在 gen.sketch）。
    # guide 是互斥仲裁表态（sketch/previz），已进 _SHOT_HUMAN_KEYS（长任务不抹）。
    ("shots[].sketch", AUTHOR),
    ("shots[].guide", AUTHOR),
    # —— 视频双模型策略（seedance-mini 缺省 / seedance-2.5 点名才用）——
    # 持久点名档（与 previz_v2v 同族的「你填」开关）；单次点名走 CLI flag 不落盘
    ("video_provider", AUTHOR),
    # —— 扩展设定图（表情表/动作表，`project refs --expressions/--poses`）——
    # 引擎回填路径 + 各自独立版本栈；刻意不进每镜自动挂载（design_refs 8 张上限），
    # 也不进 sync_design_to_chapters 白名单（章节不消费它们，系列级持有即可）
    # —— 旁白语态（旁白不是必填件；lint voiceover_heavy 的声明位）——
    ("voiceover", AUTHOR),
    ("characters[].expression_sheet", ENGINE),
    ("characters[].expression_versions", ENGINE),
    ("characters[].pose_sheet", ENGINE),
    ("characters[].pose_versions", ENGINE),
    # —— 场景俯视图（与场景基准图配对出图，视频请求每镜至多附一张）——
    # 路径 / 版本栈 / 意见池各自独立于基准图那一份：两张图各自重生、各自回滚，
    # 共用一份就会把只对图纸成立的批注带进基准图的重生指令
    ("scenes[].topview_sheet", ENGINE),
    ("scenes[].topview_versions", ENGINE),
    ("scenes[].comments", ENGINE),
    ("scenes[].topview_comments", ENGINE),
    ("scene_topview_ref", ENGINE),
    ("scene_topview_versions", ENGINE),
    ("scene_comments", ENGINE),
    ("scene_topview_comments", ENGINE),
    # —— Agent Gateway 计划式写入审计 ——
    ("agent_provenance", ENGINE),
    # —— 风格垫图的章级下发位（项目级 moodboard[] on=true 项由引擎同步进来）——
    # 与 style 块里两个从 init 起就零消费的死键（image_provider/video_provider）
    # 同批清理：图像没有「项目文档点名 provider」这条通道（真源是
    # image_route > profile.image.provider > defaults.providers.image），
    # 视频那条通道的真源是**顶层** video_provider（已登记于上）。
    ("style.moodboard", ENGINE),
    # —— 写实人物合规（photoreal face）——
    # 近景人脸预判：可选表态，只决定 gen-video 的路线起点，已进 _SHOT_HUMAN_KEYS
    # 与 batch.EDITABLE_FIELDS（STAGE_FIELDS 登记为空元组：不使产物过期）。
    ("shots[].face_visibility", AUTHOR),
    # 设定图生成方式的事实记录（t2i 才受信）；写 sheet 的五条路径同批写，
    # 已进 workspace.char_fields 白名单（系列改了章节要看得见）。
    ("characters[].sheet_origin", ENGINE),
    # —— 引擎回填的产物字段（作者不手写）——
    ("shots[].status", ENGINE),
    ("shots[].image", ENGINE),
    ("shots[].clip", ENGINE),
    ("audio", ENGINE),
    ("output", ENGINE),
    ("cost", ENGINE),
    # —— 深度控制视频（人物深度 + OpenPose 骨骼）——
    # 镜级绑定只存裁好的那一段路径，素材/起点/段长在 shots[].gen.control。
    ("shots[].control", ENGINE),
    ("control_video", AUTHOR),
]


def _load() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _resolve(schema: dict, path: str) -> dict | None:
    """按 `a.b[].c` 语法定位 property 节点，找不到返回 None。"""
    node: dict = schema
    for seg in path.split("."):
        into_items = seg.endswith("[]")
        key = seg[:-2] if into_items else seg
        props = node.get("properties") or {}
        if key not in props:
            return None
        node = props[key]
        if into_items:
            items = node.get("items")
            if not isinstance(items, dict):
                return None
            node = items
    return node


@unittest.skipUnless(SCHEMA_PATH.is_file(), f"缺 schema 文件: {SCHEMA_PATH}")
class TestSchemaContract(unittest.TestCase):
    def test_schema_parses(self):
        schema = _load()
        self.assertEqual(schema.get("type"), "object")
        self.assertTrue(schema.get("properties"), "顶层 properties 不能为空")
        self.assertTrue(schema.get("description"))

    def test_declared_field_paths_present(self):
        schema = _load()
        missing = [p for p, _ in DECLARED_FIELDS if _resolve(schema, p) is None]
        self.assertEqual(missing, [], f"契约清单里的字段在 schema 中缺失: {missing}")

    def test_gen_description_covers_supply_inspect(self):
        """供料体检报告落 `shots[].gen.image.inspect`。

        `gen` 是 `additionalProperties: {type: object}` 的宽松声明，**刻意不写死
        子 schema**（结构随阶段不同）——契约只能靠 description 承载，故在此钉死
        关键词，防「代码写了字段、契约没同步」的老漂移。"""
        node = _resolve(_load(), "shots[].gen") or {}
        desc = node.get("description") or ""
        for kw in ("inspect", "supplied", "skip-check"):
            self.assertIn(kw, desc, f"gen 的 description 少了 {kw}（M16 供料体检）")

    def test_engine_managed_prefix_for_declared(self):
        # 只校验清单内的字段（根 description 与未登记项一律跳过）
        schema = _load()
        bad: list[str] = []
        for path, owner in DECLARED_FIELDS:
            node = _resolve(schema, path)
            if node is None:
                continue                       # 缺失由上一条用例报
            desc = (node.get("description") or "").strip()
            if owner == ENGINE and not desc.startswith("[engine-managed]"):
                bad.append(f"{path}（应标 [engine-managed]）")
            if owner == AUTHOR and "[engine-managed]" in desc:
                bad.append(f"{path}（作者填写字段不该标 [engine-managed]）")
        self.assertEqual(bad, [], f"归属标注错位: {bad}")

    def test_no_duplicate_shot_size(self):
        # `framing` 是景别单一真源（prompts 摄影地板 / mysql 列 / app.js / export 四处消费），
        # 不许再引入同义的 shot_size 造第二套景别语义
        schema = _load()
        self.assertIsNotNone(_resolve(schema, "shots[].framing"))
        self.assertIsNone(_resolve(schema, "shots[].shot_size"))


if __name__ == "__main__":
    unittest.main()
