# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Agent Gateway 最小上下文、ChapterPlan CAS 与 CLI 守卫。"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from kinema.agent_gateway import AgentGateway, AgentGatewayError, _ChapterLock, chapter_revision
from kinema.pipeline.prompts import PromptCompiler
from kinema.prompt_contract import profile_revision, reference_digest
from tests.support import LocalBackendEnv


class TestAgentGateway(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.addCleanup(self.env.restore)
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        pdir = self.root / "demo"
        (pdir / "chapters").mkdir(parents=True)
        project = {
            "id": "demo", "title": "测试", "profile": "anime", "skill": "kn-anime",
            "characters": [{"name": "陆昭", "appearance": "黑发青年", "secret": "hidden"}],
            "props": [{"name": "喷罐", "desc": "银色喷罐"}],
            "scenes": [{"name": "雨巷", "desc": "雨夜巷道"}],
            "chapters": [{"id": "ch01", "title": "第一章"}],
        }
        chapter = {
            "id": "demo_ch01", "profile": "anime", "skill": "kn-anime",
            "chapter": {"project": "demo", "id": "ch01", "title": "第一章"},
            "scene": "雨夜巷道", "secret_token": "must-not-leak",
            "characters": project["characters"], "props": project["props"],
            "scenes": project["scenes"],
            "shots": [{
                "id": 1, "dur": 4, "narration": "他停下手。", "characters": ["陆昭"],
                "props": ["喷罐"], "scenes": ["雨巷"],
                "image_prompt": "陆昭在雨巷停下喷漆",
                "video_prompt": "喷雾余粒缓慢下落", "camera": "缓慢推近",
            }],
        }
        (pdir / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        self.chapter_path = pdir / "chapters" / "ch01.json"
        self.chapter_path.write_text(
            json.dumps(chapter, ensure_ascii=False, indent=2), encoding="utf-8")
        self.gateway = AgentGateway.open(str(self.root))

    def _data(self):
        return json.loads(self.chapter_path.read_text(encoding="utf-8"))

    def _spec(self, subject="陆昭"):
        return {
            "contract_version": "prompt/v1",
            "image": {"subject": subject, "composition": "人物位于右三分之一"},
            "video": {"action_delta": "右手停止喷漆", "camera": "缓慢推近"},
        }

    def _plan(self, **over):
        data = self._data()
        plan = {
            "contract_version": "chapter-plan/v1",
            "chapter": "demo/ch01",
            "expected_revision": chapter_revision(data),
            "chapter_patch": {"voiceover": "sparse"},
            "shots": [{
                "op": "update", "id": 1,
                "fields": {"narration": "他听见身后的脚步。"},
                "prompt_spec": self._spec(),
            }],
            "provenance": {"host": "codex", "model": "gpt-5.6"},
        }
        plan.update(over)
        return plan

    def test_context_is_minimal_versioned_and_secret_free(self):
        context = self.gateway.context("demo/ch01", "storyboard")
        self.assertEqual(context["contract_version"], "agent-context/v1")
        self.assertEqual(context["binding"]["skill"], "kn-anime")
        self.assertEqual(context["revision"], chapter_revision(self._data()))
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertIn("prompt_spec", context["shots"][0])
        self.assertFalse(context["entities"]["characters"][0]["sheet_ready"])
        self.assertRegex(context["contracts"]["prompt"]["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(context["contracts"]["project_schema"]["source"],
                         "docs/kinema/project.schema.json")
        self.assertTrue(context["write_contract"]["chapter_fields"]["script"]["merge"])
        self.assertNotIn("style", context["write_contract"]["chapter_fields"])
        self.assertEqual(context["write_contract"]["required_add_fields"],
                         ["dur", "narration"])

    def test_plan_field_registry_only_exposes_project_schema_fields(self):
        schema_path = Path(__file__).resolve().parents[2] / "docs/kinema/project.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        contract = self.gateway.registry.chapter_plan
        top = schema["properties"]
        shot = top["shots"]["items"]["properties"]
        self.assertEqual(set(contract["chapter_fields"]) - set(top), set())
        self.assertEqual(set(contract["shot_fields"]) - set(shot), set())
        self.assertEqual(top["script"]["type"], contract["chapter_fields"]["script"]["type"])
        self.assertEqual(top["art_direction"]["type"],
                         contract["chapter_fields"]["art_direction"]["type"])

    def test_nested_chapter_fields_are_strict_semantic_merges(self):
        data = self._data()
        data["script"] = {"hook": "旧钩子", "body": "正文", "approved": True}
        data["art_direction"] = {"variety": 5, "avoid": ["陈词滥调"]}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        before = self.chapter_path.read_bytes()
        for patch, message in (
            ({"script": {"approved": False}}, "不可写字段"),
            ({"art_direction": {"motion": 11}}, "不能大于 10"),
            ({"style": {"seed": 1}}, "不可写字段"),
        ):
            with self.assertRaisesRegex(AgentGatewayError, message):
                self.gateway.validate(self._plan(chapter_patch=patch, shots=[]))
            self.assertEqual(before, self.chapter_path.read_bytes())

        plan = self._plan(
            chapter_patch={"script": {"hook": "新钩子"},
                           "art_direction": {"motion": 7}},
            shots=[])
        self.gateway.apply(plan)
        data = self._data()
        self.assertEqual(data["script"], {
            "hook": "新钩子", "body": "正文", "approved": True,
        })
        self.assertEqual(data["art_direction"], {
            "variety": 5, "avoid": ["陈词滥调"], "motion": 7,
        })

    def test_validate_is_pure_and_returns_semantic_summary(self):
        before = self.chapter_path.read_bytes()
        result = self.gateway.validate(self._plan())
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["shot_operations"]["update"], 1)
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_unknown_fields_and_entities_fail_without_writing(self):
        before = self.chapter_path.read_bytes()
        plan = self._plan(shots=[{
            "op": "update", "id": 1, "fields": {"characters": ["未登记角色"]},
        }])
        with self.assertRaisesRegex(AgentGatewayError, "未登记实体"):
            self.gateway.validate(plan)
        self.assertEqual(before, self.chapter_path.read_bytes())
        plan = self._plan(chapter_patch={"output": "hack.mp4"})
        with self.assertRaisesRegex(AgentGatewayError, "不可写字段"):
            self.gateway.validate(plan)
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_invalid_current_shot_and_unused_note_fail_closed(self):
        before = self.chapter_path.read_bytes()
        plan = self._plan(chapter_patch={}, shots=[{
            "op": "update", "id": 1, "fields": {"hero_moment": True},
            "note": "这条不会被消费",
        }])
        with self.assertRaisesRegex(AgentGatewayError, "note 只允许 omit/restore"):
            self.gateway.validate(plan)
        self.assertEqual(before, self.chapter_path.read_bytes())

        data = self._data()
        data["shots"].append({"id": "bad"})
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        before = self.chapter_path.read_bytes()
        with self.assertRaisesRegex(AgentGatewayError, "非法镜头结构"):
            self.gateway.validate(self._plan())
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_unknown_profile_nonfinite_number_and_noop_fail_without_writing(self):
        before = self.chapter_path.read_bytes()
        for plan, message in (
            (self._plan(shots=[{"op": "update", "id": 1,
                                "fields": {"profile": "missing_profile"}}]), "未登记"),
            (self._plan(shots=[{"op": "update", "id": 1,
                                "fields": {"dur": float("nan")}}]), "类型或枚举值"),
            (self._plan(shots=[{"op": "update", "id": 1,
                                "fields": {"dur": 0}}]), "必须大于 0"),
            (self._plan(chapter_patch={
                "script": {"per_platform": {"douyin": {"score": float("nan")}}},
            }, shots=[]),
             "不是合法有限 JSON 值"),
            (self._plan(shots=[{"op": "update", "id": 1,
                                "fields": {"narration": "他停下手。"}}]), "没有实际变化"),
        ):
            with self.assertRaisesRegex(AgentGatewayError, message):
                self.gateway.validate(plan)
            self.assertEqual(before, self.chapter_path.read_bytes())

    def test_chapter_lock_table_covers_audio_and_mixed_burn_fields(self):
        """章级 done 锁表是 review.CHAPTER_STAGE_FIELDS 一份：混烧开关与首帧锚定改片段
        请求形态，渲染档与音频制式改旁白轨是否成立。"""
        from kinema import review
        self.assertNotIn("_CHAPTER_STAGE_FIELDS",
                         Path(__file__).resolve().parents[1].joinpath(
                             "kinema/agent_gateway.py").read_text(encoding="utf-8"))
        data = self._data()
        data["shots"][0]["review"] = {"clip": {"state": "done"}}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for field, value in (("native_voiceover", True), ("anchor_frame", True)):
            self.assertIn(field, review.CHAPTER_STAGE_FIELDS["clip"])
            with self.assertRaisesRegex(AgentGatewayError, "锁定阶段 clip"):
                self.gateway.apply(self._plan(chapter_patch={field: value}, shots=[]))
        data["shots"][0]["review"] = {"audio": {"state": "done"}}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(AgentGatewayError, "锁定阶段 audio"):
            self.gateway.apply(self._plan(chapter_patch={"audio_mode": "scored"}, shots=[]))

    def test_update_marks_produced_unlocked_stages_for_retake(self):
        """改了作者字段，已产出且未锁定的阶段进重做队列；否则 gen-* 看产物在盘即跳过。"""
        data = self._data()
        img = self.root / "demo" / "chapters" / "s1.png"
        img.write_bytes(b"png")
        data["shots"][0]["image"] = str(img)
        data["shots"][0]["review"] = {"image": {"state": "wfa"}}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.gateway.apply(self._plan(chapter_patch={}, shots=[{
            "op": "update", "id": 1, "fields": {}, "prompt_spec": self._spec("新主体")}]))
        shot = self._data()["shots"][0]
        self.assertEqual(shot["review"]["image"]["state"], "retake")
        self.assertNotIn("clip", shot["review"], "没有片段产物的阶段不置重做")

    def test_chapter_patch_marks_produced_unlocked_stages_for_retake(self):
        """章级字段改动与镜级同规：决定某阶段产物的字段变了，已产出未锁定的镜进重做队列；
        否则改了 style_prompt 全章旧画风图一张不重出。"""
        from kinema import review
        data = self._data()
        img = self.root / "demo" / "chapters" / "s1.png"
        img.write_bytes(b"png")
        data["shots"][0]["image"] = str(img)
        data["shots"][0]["review"] = {"image": {"state": "wfa"}}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.gateway.apply(self._plan(chapter_patch={"style_prompt": "水彩"}, shots=[]))
        shot = self._data()["shots"][0]
        self.assertEqual(shot["review"]["image"]["state"], "retake")
        self.assertNotIn("clip", shot["review"], "没有片段产物的阶段不置重做")
        for field, stage in (("speech_rate", "audio"), ("frame_chain", "clip"),
                             ("voice_anchor", "clip"), ("video_provider", "clip")):
            self.assertIn(field, review.CHAPTER_STAGE_FIELDS[stage])

    def test_context_exposes_every_writable_chapter_field(self):
        context = self.gateway.context("demo/ch01", "storyboard")
        self.assertEqual(set(context["write_contract"]["chapter_fields"]),
                         set(self.gateway.registry.chapter_plan["chapter_fields"]))
        self.assertIn("delivery", self.gateway.registry.chapter_plan["shot_fields"])
        with self.assertRaisesRegex(AgentGatewayError, "不能大于"):
            self.gateway.validate(self._plan(chapter_patch={"speech_rate": 500}, shots=[]))
        with self.assertRaisesRegex(AgentGatewayError, "不合法"):
            self.gateway.validate(self._plan(chapter_patch={}, shots=[{
                "op": "update", "id": 1, "fields": {"bubble_pos": "top"}}]))

    def test_motion_must_be_a_registered_render_mode(self):
        from kinema.project import MOTIONS
        before = self.chapter_path.read_bytes()
        with self.assertRaisesRegex(AgentGatewayError, "motion"):
            self.gateway.validate(self._plan(chapter_patch={"motion": "seedance"}, shots=[]))
        self.assertEqual(before, self.chapter_path.read_bytes())
        self.assertEqual(
            sorted(self.gateway.registry.chapter_plan["chapter_fields"]["motion"]["enum"]),
            sorted(MOTIONS))

    def test_review_locked_stage_rejects_semantic_changes(self):
        data = self._data()
        data["shots"][0]["review"] = {"image": {"state": "done"}}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        before = self.chapter_path.read_bytes()
        plan = self._plan(chapter_patch={}, shots=[{
            "op": "update", "id": 1, "fields": {}, "prompt_spec": self._spec("新主体"),
        }])
        with self.assertRaisesRegex(AgentGatewayError, "已通过锁定阶段 image"):
            self.gateway.apply(plan)
        self.assertEqual(before, self.chapter_path.read_bytes())
        plan = self._plan(chapter_patch={"style_prompt": "改成水彩"}, shots=[])
        with self.assertRaisesRegex(AgentGatewayError, "chapter_patch.*锁定阶段 image"):
            self.gateway.apply(plan)
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_plan_accepts_sketch_beats_and_merge_preserves_sheet(self):
        data = self._data()
        data["shots"][0]["sketch"] = {"sheet": "board.png"}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        beats = [{"t": "0-2s", "action": "抬手", "camera": "缓推"}, {"action": "转身"}]
        plan = self._plan(expected_revision=chapter_revision(self._data()), shots=[{
            "op": "update", "id": 1, "fields": {"sketch": {"beats": beats}},
        }])
        self.gateway.apply(plan)
        shot = self._data()["shots"][0]
        self.assertEqual(shot["sketch"]["beats"], beats)
        self.assertEqual(shot["sketch"]["sheet"], "board.png",
                         "merge 语义：engine 管的 sheet 不许被 beats 提交覆写掉")

    def test_plan_rejects_malformed_beats(self):
        for bad in ([],                                   # 空列表
                    [{"t": "0-2s"}],                      # 缺 action
                    [{"action": ""}],                     # action 空
                    [{"action": "抬手", "cam": "推"}],    # 未知键（拼错=写了没人消费）
                    [{"action": 3}],                      # 非字符串
                    "0-2s 抬手"):                         # 非列表
            plan = self._plan(shots=[{
                "op": "update", "id": 1, "fields": {"sketch": {"beats": bad}}}])
            with self.assertRaises(AgentGatewayError, msg=f"bad={bad!r}"):
                self.gateway.validate(plan)

    def test_plan_accepts_multi_speaker_lines(self):
        """`lines[]` 是镜内多角色逐句换声的唯一形态——白名单不收它，长镜里的
        对白交换就只能并进 narration（一把声音念完）或绕过正门写盘。"""
        lines = [{"speaker": "阿岩", "text": "就在前面。", "emotion": "calm"},
                 {"speaker": "小雨", "text": "等等我！", "voice": "少年"}]
        plan = self._plan(shots=[{
            "op": "update", "id": 1, "fields": {"lines": lines}}])
        self.gateway.apply(plan)
        self.assertEqual(self._data()["shots"][0]["lines"], lines)

    def test_plan_rejects_malformed_lines(self):
        for bad in ([],                                    # 空列表
                    [{"speaker": "阿岩"}],                 # 缺 text
                    [{"text": ""}],                        # text 空
                    [{"text": "走。", "dur": 1.2}],        # engine-managed 键
                    [{"text": "走。", "speakr": "阿岩"}],  # 拼错键
                    "阿岩：走。"):                          # 非列表
            plan = self._plan(shots=[{
                "op": "update", "id": 1, "fields": {"lines": bad}}])
            with self.assertRaises(AgentGatewayError, msg=f"bad={bad!r}"):
                self.gateway.validate(plan)

    def test_plan_rejects_sheet_write_through_sketch(self):
        plan = self._plan(shots=[{
            "op": "update", "id": 1, "fields": {"sketch": {"sheet": "hack.png"}}}])
        with self.assertRaises(AgentGatewayError):
            self.gateway.validate(plan)

    def test_entry_state_flows_through_prompt_spec(self):
        spec = self._spec()
        spec["video"]["entry_state"] = "镜头仍停在空椅上"
        plan = self._plan(shots=[{
            "op": "update", "id": 1, "fields": {}, "prompt_spec": spec}])
        self.gateway.apply(plan)
        self.assertEqual(self._data()["shots"][0]["entry_state"], "镜头仍停在空椅上")

    def test_video_context_exposes_entry_state_and_sketch(self):
        data = self._data()
        data["shots"][0]["entry_state"] = "承接空椅构图"
        data["shots"][0]["sketch"] = {"beats": [{"action": "抬手"}]}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        context = self.gateway.context("demo/ch01", "video")
        self.assertEqual(context["shots"][0]["entry_state"], "承接空椅构图")
        self.assertIn("beats", context["shots"][0]["sketch"])

    def test_apply_projects_prompt_and_appends_provenance(self):
        plan = self._plan(provenance={"host": " codex ", "model": " gpt-5.6 "}, shots=[
            {"op": "update", "id": 1, "fields": {"hero_moment": True},
             "prompt_spec": self._spec("陆昭与喷罐")},
            {"op": "add", "id": 2,
             "fields": {"dur": 3, "narration": "脚步停在巷口。", "scenes": ["雨巷"]},
             "prompt_spec": self._spec("巷口的陌生人")},
        ])
        result = self.gateway.apply(plan)
        data = self._data()
        self.assertEqual(result["before_revision"], plan["expected_revision"])
        self.assertEqual(result["revision"], chapter_revision(data))
        self.assertTrue(data["shots"][0]["hero_moment"])
        self.assertEqual(data["shots"][0]["image_prompt"], "陆昭与喷罐，人物位于右三分之一")
        self.assertEqual(data["shots"][1]["id"], 2)
        self.assertEqual(data["agent_provenance"][-1]["host"], "codex")
        self.assertEqual(data["agent_provenance"][-1]["model"], "gpt-5.6")
        self.assertNotIn("prompt_spec", data["shots"][0])

    def test_revision_conflict_is_zero_write(self):
        plan = self._plan()
        self.gateway.apply(plan)
        before = self.chapter_path.read_bytes()
        with self.assertRaisesRegex(AgentGatewayError, "revision 冲突"):
            self.gateway.apply(plan)
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_concurrent_apply_lock_fails_before_writing(self):
        before = self.chapter_path.read_bytes()
        with _ChapterLock(self.chapter_path):
            with self.assertRaisesRegex(AgentGatewayError, "正被其他写入"):
                self.gateway.apply(self._plan())
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_apply_refuses_while_engine_operation_holds_the_chapter(self):
        """生成任务持章期间计划不落盘：作者字段不在 save 合并面，任务收尾会写回旧值。"""
        from kinema.locking import FileLock
        before = self.chapter_path.read_bytes()
        holder = FileLock(self.chapter_path.with_suffix(".json.oplock"),
                          blocking=False).acquire()
        try:
            with self.assertRaisesRegex(AgentGatewayError, "已有操作在执行"):
                self.gateway.apply(self._plan())
        finally:
            holder.release()
        self.assertEqual(before, self.chapter_path.read_bytes())

    def test_omit_and_restore_only_touch_review_state(self):
        plan = self._plan(chapter_patch={}, shots=[{"op": "omit", "id": 1, "note": "节奏重复"}])
        self.gateway.apply(plan)
        self.assertEqual(self._data()["shots"][0]["review"]["shot"]["state"], "omt")
        plan = self._plan(chapter_patch={}, shots=[{"op": "restore", "id": 1}])
        self.gateway.apply(plan)
        self.assertEqual(self._data()["shots"][0]["review"]["shot"]["state"], "todo")

    def test_explain_detects_prompt_spec_drift(self):
        from kinema.models import ConfigStore, ModelRouter
        data = self._data()
        image_ref = self.root / "demo" / "chapters" / "shot_1.png"
        image_ref.write_bytes(b"image-v1")
        data["shots"][0]["image"] = str(image_ref)
        skill_revision = self.gateway.catalog.get("kn-anime")["digest"]
        store = ConfigStore.load()
        provider, params = ModelRouter(store).resolve("image", "anime")
        envelope = PromptCompiler().image(
            data["shots"][0], skill_revision=skill_revision,
            profile_revision=profile_revision("anime", provider, params),
            references=[{
                "role": "shot_frame", "id": "shot:1",
                "sha256": reference_digest(image_ref),
            }]).as_dict()
        data["shots"][0].setdefault("gen", {})["image"] = {"envelope": envelope}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        current = self.gateway.explain("demo/ch01", 1, "image")
        self.assertFalse(current["stale"])
        image_ref.write_bytes(b"image-v2")
        reference_stale = self.gateway.explain("demo/ch01", 1, "image")
        self.assertIn("references_changed", reference_stale["stale_reasons"])
        self.assertEqual(reference_stale["stale_references"], ["shot:1"])
        image_ref.write_bytes(b"image-v1")
        data = self._data()
        data["shots"][0]["gen"]["image"]["envelope"]["profile_revision"] = \
            "sha256:" + "0" * 64
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        profile_stale = self.gateway.explain("demo/ch01", 1, "image")
        self.assertIn("profile_changed", profile_stale["stale_reasons"])
        data = self._data()
        data["shots"][0]["gen"]["image"]["envelope"]["profile_revision"] = \
            envelope["profile_revision"]
        data["shots"][0]["gen"]["image"]["envelope"]["compiler_version"] = "0.9.0"
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        compiler_stale = self.gateway.explain("demo/ch01", 1, "image")
        self.assertIn("prompt_compiler_changed", compiler_stale["stale_reasons"])
        data = self._data()
        data["shots"][0]["gen"]["image"]["envelope"]["compiler_version"] = \
            envelope["compiler_version"]
        data["shots"][0]["image_prompt"] = "新的构图"
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        stale = self.gateway.explain("demo/ch01", 1, "image")
        self.assertIn("prompt_spec_changed", stale["stale_reasons"])

    def test_explain_ignores_transient_tail_frame_reference(self):
        """尾帧引用不参与过期比对：其文档落点在 gen 子树（引用摘要收集不覆盖），
        镜内注入时引用的还是有时效的官方 URL——参与比对会让每个接力片段
        刚生成即被恒判 references_changed。"""
        from kinema.models import ConfigStore, ModelRouter
        data = self._data()
        image_ref = self.root / "demo" / "chapters" / "shot_1.png"
        image_ref.write_bytes(b"image-v1")
        data["shots"][0]["image"] = str(image_ref)
        skill_revision = self.gateway.catalog.get("kn-anime")["digest"]
        store = ConfigStore.load()
        provider, params = ModelRouter(store).resolve("video", "anime")
        envelope = PromptCompiler().video(
            data["shots"][0], native=True, skill_revision=skill_revision,
            profile_revision=profile_revision("anime", provider, params),
            references=[
                {"role": "shot_frame", "id": "shot:1",
                 "sha256": reference_digest(image_ref)},
                {"role": "tail_frame", "id": "shot:1:tail:16:9",
                 "sha256": reference_digest("https://ark.example/tail.png")},
            ]).as_dict()
        data["shots"][0].setdefault("gen", {})["clip"] = {
            "envelope": envelope, "tail_frames": {"16:9": "chapters/tail.png"}}
        self.chapter_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        result = self.gateway.explain("demo/ch01", 1, "video")
        self.assertNotIn("references_changed", result["stale_reasons"])
        self.assertEqual(result["stale_references"], [])

    def test_explain_names_supplied_and_refined_absence_distinctly(self):
        """无 envelope 的三种事实要报三个名字：素材直供与局部改造是**正确状态**
        （画面本来就不是 / 不再是 envelope 提示词的纯输出），与「从没走过契约
        生成」混成一个原因，指挥层会把前两者误当成待重生的欠账。"""
        cases = [
            ({"provider": "supplied", "source": "/x.png", "cost": 0.0},
             "image_supplied_externally"),
            ({"provider": "seedream", "refine": {"rect": [0, 0, 1, 1],
                                                 "instruction": "去掉字"}},
             "image_manually_refined"),
            ({}, "not_generated_with_prompt_envelope"),
        ]
        for gen_image, expected in cases:
            data = self._data()
            data["shots"][0].setdefault("gen", {})["image"] = gen_image
            self.chapter_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            r = self.gateway.explain("demo/ch01", 1, "image")
            self.assertTrue(r["stale"])
            self.assertEqual(r["stale_reasons"], [expected])

    def test_cli_contract_context_validate_and_apply(self):
        from kinema.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["agent", "contract", "prompt", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["contract"]["version"], "prompt/v1")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["agent", "context", "--chapter", "demo/ch01",
                       "--task", "image", "--workspace", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["chapter"], "demo/ch01")

        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps(self._plan(), ensure_ascii=False), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["agent", "plan", "validate", "--file", str(plan_file),
                       "--workspace", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["agent", "plan", "apply", "--file", str(plan_file),
                       "--workspace", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
