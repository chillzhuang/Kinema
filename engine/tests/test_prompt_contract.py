# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prompt 正式契约与确定性编译守卫。"""
from __future__ import annotations

import unittest

from kinema.pipeline import prompts
from kinema.prompt_contract import (
    AgentContractRegistry,
    PromptContractError,
    PromptSpec,
    stable_digest,
)


class TestPromptContract(unittest.TestCase):
    def setUp(self):
        self.registry = AgentContractRegistry.load()
        self.value = {
            "contract_version": "prompt/v1",
            "image": {
                "subject": "陆昭",
                "action": "俯身喷漆后停住",
                "composition": "人物位于右三分之一",
                "framing": "中近景",
                "lighting": "雨夜霓虹侧逆光",
                "text_en": "Lu Zhao freezes mid-spray",
                "negative": "多余人物",
            },
            "video": {
                "action_delta": "右手停止喷漆",
                "secondary_motion": "喷雾余粒下落",
                "camera": "缓慢推近",
                "end_state": "视线停在画外声源方向",
                "text_en": "His spraying hand stops",
            },
        }

    def test_registry_exposes_versioned_contract_and_digest(self):
        described = self.registry.describe("prompt")
        self.assertEqual(described["contract"]["version"], "prompt/v1")
        self.assertRegex(described["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_structured_shot_fields_default_to_chinese_and_text_en_is_explicit(self):
        fields = self.registry.prompt["stages"]
        for stage, names in {
            "image": ("subject", "action", "expression", "composition", "framing",
                      "angle", "lens", "lighting", "creative_notes", "negative"),
            "video": ("action_delta", "secondary_motion", "camera", "end_state",
                      "light_shift", "sound", "creative_notes"),
        }.items():
            for name in names:
                self.assertEqual(fields[stage]["fields"][name]["language"], "zh",
                                 f"{stage}.{name} 默认必须中文")
        self.assertEqual(fields["image"]["fields"]["text_en"]["language"], "en")
        self.assertEqual(fields["video"]["fields"]["text_en"]["language"], "en")

    def test_equivalent_json_numbers_have_one_canonical_digest(self):
        self.assertEqual(stable_digest({"dur": 5}), stable_digest({"dur": 5.0}))
        self.assertEqual(stable_digest({"offset": 0}), stable_digest({"offset": -0.0}))

    def test_prompt_spec_rejects_unknown_fields(self):
        value = dict(self.value)
        value["image"] = {**value["image"], "style": "赛博朋克"}
        with self.assertRaisesRegex(PromptContractError, "未知字段"):
            PromptSpec.parse(value, registry=self.registry)

    def test_projection_is_authoritative_and_deterministic(self):
        spec = PromptSpec.parse(self.value, registry=self.registry)
        projected = spec.project_fields()
        self.assertEqual(projected["image_prompt"],
                         "陆昭，俯身喷漆后停住，人物位于右三分之一")
        self.assertEqual(projected["video_prompt"], "喷雾余粒下落")
        self.assertEqual(projected["action"], "右手停止喷漆")
        self.assertEqual(projected["camera"], "缓慢推近")
        self.assertEqual(projected["negative_prompt"], "多余人物")
        self.assertEqual(spec.revision, stable_digest(spec.as_dict()))
        self.assertEqual(
            spec.as_dict(),
            PromptSpec.parse(spec.as_dict(), registry=self.registry).as_dict())

    def test_from_shot_round_trips_existing_author_fields(self):
        shot = {
            "image_prompt": "老人站在崖边",
            "image_prompt_en": "an old man on a cliff",
            "video_prompt": "老人缓缓抬头",
            "camera": "慢推",
            "negative_prompt": "多余人物",
        }
        projected = PromptSpec.from_shot(shot, registry=self.registry).project_fields()
        self.assertEqual(projected["image_prompt"], shot["image_prompt"])
        self.assertEqual(projected["video_prompt"], shot["video_prompt"])
        self.assertEqual(projected["camera"], shot["camera"])

    def test_from_shot_does_not_project_narration_into_image_prompt(self):
        """IR 只读作者字段：把 narration 回填成 creative_notes，Agent 原样回提就会把
        narration 写进 image_prompt，被记作变化并置 image retake。提示词回落留在编译期。"""
        projected = PromptSpec.from_shot({"narration": "他走了。", "camera": "慢推"},
                                         registry=self.registry).project_fields()
        self.assertEqual(projected["image_prompt"], "")

    def test_image_envelope_is_exact_and_time_independent(self):
        compiler = prompts.PromptCompiler(self.registry)
        options = {
            "style_prefix": "电影级赛博朋克",
            "scene": "雨夜巷道",
            "text_floor": True,
        }
        first = compiler.image({}, spec=self.value, **options)
        second = compiler.image({}, spec=self.value, **options)
        projected = PromptSpec.parse(self.value, registry=self.registry).project_fields()
        self.assertEqual(first.prompt, prompts.image_prompt(projected, **options))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotIn("避免出现", first.positive)
        self.assertIn("多余人物", first.negative)
        self.assertIn("避免出现", first.prompt)

    def test_envelope_fingerprint_covers_references_and_revisions(self):
        compiler = prompts.PromptCompiler(self.registry)
        base = compiler.video({}, native=True, spec=self.value)
        changed = compiler.video(
            {}, native=True, spec=self.value,
            references=[{
                "role": "shot_frame",
                "id": "shot:1",
                "sha256": "sha256:" + "1" * 64,
            }],
            skill_revision="sha256:" + "2" * 64,
        )
        self.assertNotEqual(base.fingerprint, changed.fingerprint)
        self.assertEqual(changed.prompt, changed.as_dict()["prompt"])

    def test_provider_limit_fails_before_envelope_instead_of_truncating(self):
        compiler = prompts.PromptCompiler(self.registry)
        with self.assertRaisesRegex(PromptContractError, "超过 provider 声明上限"):
            compiler.image({}, spec=self.value, max_chars=20)




class TestEntryStateContract(unittest.TestCase):
    """entry_state 契约槽位：from_shot 回读与 parse 提交两条路都要通到投影。"""

    def test_from_shot_round_trip_projects_entry_state(self):
        from kinema.prompt_contract import PromptSpec
        spec = PromptSpec.from_shot({"image_prompt": "画面", "entry_state": "从空椅接起"})
        self.assertEqual(spec.project_fields()["entry_state"], "从空椅接起")

    def test_parse_accepts_entry_state(self):
        from kinema.prompt_contract import PromptSpec
        spec = PromptSpec.parse({
            "contract_version": "prompt/v1",
            "image": {"subject": "陆昭"},
            "video": {"entry_state": "从空椅接起"},
        })
        self.assertEqual(spec.project_fields()["entry_state"], "从空椅接起")


if __name__ == "__main__":
    unittest.main()
