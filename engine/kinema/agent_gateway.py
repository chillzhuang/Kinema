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

"""多宿主 Agent 的最小上下文、计划校验与章节级 CAS 写入网关。"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import review, voicecast
from .project import Project, chapter_flag, effective_audio_mode, effective_motion
from .agent_system import AgentCatalog, AgentCatalogError
from .errors import ProjectError
from .prompt_contract import (
    AgentContractRegistry,
    PromptContractError,
    PromptSpec,
    profile_revision,
    reference_digest,
    stable_digest,
)
from .workspace import Workspace


TARGET_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class AgentGatewayError(ProjectError):
    """Agent context 或 ChapterPlan 不符合 Gateway 契约。"""


def chapter_revision(data: Mapping[str, Any]) -> str:
    """章节内容 ETag；格式化、键顺序与文件 mtime 不参与。"""
    return stable_digest(data)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _target(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or value.count("/") != 1:
        raise AgentGatewayError("chapter 必须形如 项目id/章节id")
    pid, cid = value.split("/", 1)
    if not TARGET_PART_RE.fullmatch(pid) or not TARGET_PART_RE.fullmatch(cid):
        raise AgentGatewayError("chapter 只允许字母、数字、下划线和连字符")
    return pid, cid


class _ChapterLock:
    """apply 的非阻塞章节写互斥：先持章节操作锁，再持文档写锁。

    操作锁把计划写入与生成/合成任务串行——作者字段不在 `Project.save` 的合并面，
    任务收尾写盘会把本次 apply 写回旧值而 Agent 已拿到 ok。文档写锁与
    `Project.save` 同路径（`locking.save_lock`）：校验与写盘之间不允许任何其他
    写者插入，两套锁文件会留出交错窗口。"""

    def __init__(self, chapter_path: Path):
        from .locking import FileLock, op_lock
        self._op = op_lock(chapter_path, kind="agent-plan")
        self._lock = FileLock(
            chapter_path.with_suffix(chapter_path.suffix + ".lock"),
            blocking=False,
            conflict_msg="章节正被其他写入占用（Agent plan apply 或引擎写盘），请稍后重试")

    def __enter__(self):
        from .errors import KinemaError
        try:
            self._op.__enter__()
        except KinemaError as exc:
            raise AgentGatewayError(f"{exc}；重新读取 context 后再提交计划") from exc
        # 引擎 save 的持锁窗口是毫秒级，有限重试吸收这类瞬时占用；
        # 持续占用（另一个 apply 或长时间写盘）仍按契约快速失败
        last: KinemaError | None = None
        for _ in range(5):
            try:
                self._lock.acquire()
                return self
            except KinemaError as exc:
                last = exc
                time.sleep(0.05)
        self._op.__exit__(None, None, None)
        raise AgentGatewayError(str(last)) from last

    def __exit__(self, exc_type, exc, tb):
        try:
            self._lock.release()
        finally:
            self._op.__exit__(exc_type, exc, tb)


def _type_ok(value: Any, spec: Mapping[str, Any]) -> bool:
    expected = spec["type"]
    if expected == "string":
        ok = isinstance(value, str)
    elif expected == "number":
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(float(value)))
    elif expected == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "boolean":
        ok = isinstance(value, bool)
    elif expected == "string_list":
        ok = (isinstance(value, list)
              and all(isinstance(item, str) and bool(item.strip()) for item in value)
              and len(value) == len(set(value)))
    elif expected == "object":
        ok = isinstance(value, dict)
    elif expected in ("beat_list", "line_list"):
        ok = isinstance(value, list) and len(value) > 0
    else:
        return False
    return ok and ("enum" not in spec or value in spec["enum"])


def _validate_value(value: Any, spec: Mapping[str, Any], where: str) -> None:
    if not _type_ok(value, spec):
        raise AgentGatewayError(f"{where} 类型或枚举值不合法")
    if "minimum" in spec and value < spec["minimum"]:
        raise AgentGatewayError(f"{where} 不能小于 {spec['minimum']}")
    if "maximum" in spec and value > spec["maximum"]:
        raise AgentGatewayError(f"{where} 不能大于 {spec['maximum']}")
    if spec.get("type") == "object" and isinstance(spec.get("properties"), Mapping):
        properties = spec["properties"]
        if spec.get("additional_properties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise AgentGatewayError(
                    f"{where} 含不可写字段: {', '.join(sorted(unknown))}")
        for name in spec.get("required") or ():
            if not str(value.get(name) or "").strip():
                raise AgentGatewayError(f"{where}.{name} 必填非空")
        for name, child in value.items():
            if name in properties:
                _validate_value(child, properties[name], f"{where}.{name}")
    if spec.get("type") in ("beat_list", "line_list"):
        for index, item in enumerate(value):
            _validate_value(item, spec["items"], f"{where}[{index}]")


def _merged_field(current: Any, value: Any, spec: Mapping[str, Any]) -> Any:
    if spec.get("merge") is True:
        merged = copy.deepcopy(current) if isinstance(current, dict) else {}
        merged.update(copy.deepcopy(value))
        return merged
    return copy.deepcopy(value)


def _changed_fields(baseline: Mapping[str, Any], values: Mapping[str, Any],
                    specs: Mapping[str, Any]) -> set[str]:
    """提交值按 merge 语义并进基线后仍与基线不同的字段。校验与落盘共用这一个判据。"""
    return {name for name, value in values.items()
            if baseline.get(name) != _merged_field(baseline.get(name), value, specs[name])}


def _effective_chapter_baseline(data: Mapping[str, Any], names,
                                specs: Mapping[str, Any]) -> dict[str, Any]:
    """章级字段的生效值，与引擎读侧同源：渲染档按内容定档、音频路线缺省 tracks、
    布尔开关按 `chapter_flag`，其余取盘上值。失效传播与锁校验按它比对：写明这些缺省
    会落盘，但不该让整章产物重做。"""
    out: dict[str, Any] = {}
    for name in names:
        if name == "motion":
            out[name] = effective_motion(data)
        elif name == "audio_mode":
            out[name] = effective_audio_mode(data)
        elif specs[name]["type"] == "boolean":
            out[name] = chapter_flag(data, name)
        else:
            out[name] = data.get(name)
    return out


def _retake_stale_products(shot: dict, changed: set) -> None:
    """作者字段改动后，按 `review.STAGE_FIELDS` 把已产出且未锁定的阶段置 retake——
    否则 gen-image / gen-video 看到产物在盘即跳过，改动永远进不了下一版。
    不写重做意见：镜级意见会编译进下一版提示词。"""
    review.retake_produced(
        shot, [st for st in review.STAGES if changed.intersection(review.fields_for(st))])


def _document_reference_digests(value: Any) -> set[str]:
    """收集章节当前仍可解析的字符串引用摘要，供 Envelope 引用漂移判断。"""
    result: set[str] = set()
    reference_keys = {
        "image", "images", "refs", "moodboard", "sheet", "scene_ref",
        "character_ref", "style_board", "last_frame_ref", "previz",
    }

    def visit(item: Any, reference_context: bool = False) -> None:
        if isinstance(item, str) and reference_context:
            result.add(reference_digest(item))
        elif isinstance(item, Mapping):
            for key, child in item.items():
                if key in {"gen", "agent_provenance"}:
                    continue
                child_context = reference_context or key in reference_keys \
                    or str(key).endswith(("_ref", "_sheet"))
                visit(child, child_context)
        elif isinstance(item, list):
            for child in item:
                visit(child, reference_context)

    visit(value)
    return result


class AgentGateway:
    """围绕单章节文档的正式 Agent 入口；不解析 Skill Markdown。"""

    def __init__(self, workspace: Workspace,
                 registry: AgentContractRegistry | None = None,
                 config: str | None = None):
        self.workspace = workspace
        self.registry = registry or AgentContractRegistry.load()
        self.catalog = AgentCatalog.load()
        self.config = config

    @classmethod
    def open(cls, workspace: str | None = None, config: str | None = None) -> "AgentGateway":
        return cls(Workspace.open(workspace, create=False), config=config)

    def contract(self, name: str) -> dict[str, Any]:
        return self.registry.describe(name)

    def _chapter(self, target: str) -> tuple[str, str, Path, dict[str, Any]]:
        pid, cid = _target(target)
        series = self.workspace.get_project(pid)
        path = series.get_chapter_path(cid)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentGatewayError(f"无法读取章节 {target}: {exc}") from exc
        if not isinstance(data, dict):
            raise AgentGatewayError(f"章节 {target} 顶层必须是对象")
        return pid, cid, path, data

    def context(self, target: str, task: str) -> dict[str, Any]:
        plan_contract = self.registry.chapter_plan
        if task not in plan_contract["tasks"]:
            raise AgentGatewayError(
                f"未知 Agent task: {task}（可选: {', '.join(plan_contract['tasks'])}）")
        _pid, _cid, _path, data = self._chapter(target)
        profile = str(data.get("profile") or "narration")
        # 章节落盘的 skill/profile 是绑定事实：退役值走 bound_* 报错（含换绑指引），
        # 不与「本次显式输入」的严格校验共用措辞
        skill = str(data.get("skill") or self.catalog.bound_profile(profile))
        skill_meta = self.catalog.bound_skill(skill)

        entity_fields = ("name", "desc", "appearance", "role", "keywords", "constraints",
                         "subject_kind", "visual_requirements", "outfit", "hair", "weapon")
        entities = {}
        for group in ("characters", "props", "scenes"):
            entities[group] = []
            for item in (data.get(group) or []):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                compact = {
                    key: copy.deepcopy(item[key]) for key in entity_fields if key in item
                }
                compact["sheet_ready"] = bool(item.get("sheet"))
                entities[group].append(compact)

        common = ("id", "dur", "characters", "props", "scenes",
                  "narration", "narration_en", "caption", "caption_en")
        # 可写的字段必须可读：`lines[]` 与 `delivery` 是整块替换语义，Agent 拿不到
        # 现值就无法安全做读改写
        task_fields = {
            "storyboard": common + ("speaker", "emotion", "emotion_scale", "voice",
                                      "voice_instruction", "delivery", "lines",
                                      "face_visibility", "shot_intent", "narrative_role",
                                      "hero_moment", "priority", "profile"),
            "image": common + ("framing", "angle", "lens", "lighting", "negative_prompt",
                               "face_visibility"),
            "video": common + ("action", "camera", "entry_state", "end_state",
                                 "light_shift", "sfx", "guide", "sketch",
                                 "anchor_frame", "frame_chain"),
            "review": ("id", "dur", "image", "clip", "review", "consistency"),
        }
        shots = []
        for shot in data.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            item = {key: copy.deepcopy(shot[key]) for key in task_fields[task] if key in shot}
            if task in {"storyboard", "image", "video"}:
                item["prompt_spec"] = PromptSpec.from_shot(
                    shot, registry=self.registry).as_dict()
            gen_stage = "image" if task == "image" else "clip" if task == "video" else None
            if gen_stage:
                envelope = (((shot.get("gen") or {}).get(gen_stage) or {}).get("envelope"))
                if isinstance(envelope, dict):
                    item["last_prompt_fingerprint"] = envelope.get("fingerprint")
            shots.append(item)

        # 章级可读面 = 可写白名单 + 只读的 `style`（画风快照，Agent 只看不改）
        chapter_fields = ("style", *self.registry.chapter_plan["chapter_fields"])
        prompt_contract = self.registry.describe("prompt")
        chapter_plan_contract = self.registry.describe("chapter-plan")
        view = Project(Path("."), data)
        return {
            "contract_version": "agent-context/v1",
            "chapter": target,
            "revision": chapter_revision(data),
            "task": task,
            "binding": {
                "skill": skill,
                "skill_revision": skill_meta["digest"],
                "profile": profile,
                "prompt_contract": self.registry.prompt["version"],
                "chapter_plan_contract": plan_contract["version"],
            },
            "contracts": {
                "prompt": {
                    "uri": f"kinema://contracts/{self.registry.prompt['version']}",
                    "version": self.registry.prompt["version"],
                    "digest": prompt_contract["digest"],
                },
                "chapter_plan": {
                    "uri": f"kinema://contracts/{plan_contract['version']}",
                    "version": plan_contract["version"],
                    "digest": chapter_plan_contract["digest"],
                },
                "project_schema": {
                    "uri": "kinema://schemas/project/v1",
                    "source": "docs/kinema/project.schema.json",
                },
            },
            "chapter_data": {
                key: copy.deepcopy(data[key]) for key in chapter_fields if key in data
            },
            # 引擎按内容推导、文档里常缺席的两个档位；Agent 写明它们不构成生效变更
            "effective": {
                "motion": effective_motion(data),
                "audio_mode": effective_audio_mode(data),
            },
            "entities": entities,
            "shots": shots,
            "constraints": {
                "budget": copy.deepcopy(data.get("budget") or {}),
                "review": review.summary(
                    [item for item in (data.get("shots") or []) if isinstance(item, dict)],
                    audio_of=lambda s: voicecast.has_audio_stage(s, view)),
            },
            "write_contract": {
                "chapter_fields": copy.deepcopy(plan_contract["chapter_fields"]),
                "shot_fields": copy.deepcopy(plan_contract["shot_fields"]),
                "operations": list(plan_contract["operations"]),
                "required_add_fields": list(plan_contract["required_add_fields"]),
                "provenance_fields": copy.deepcopy(plan_contract["provenance_fields"]),
            },
        }

    def _validate_fields(self, values: Any, registry: Mapping[str, Any], where: str,
                         data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise AgentGatewayError(f"{where} 必须是对象")
        unknown = set(values) - set(registry)
        if unknown:
            raise AgentGatewayError(f"{where} 含不可写字段: {', '.join(sorted(unknown))}")
        normalized = {}
        entity_names = {
            group: {str(item.get("name")) for item in (data.get(group) or [])
                    if isinstance(item, dict) and item.get("name")}
            for group in ("characters", "props", "scenes")
        }
        for name, value in values.items():
            spec = registry[name]
            _validate_value(value, spec, f"{where}.{name}")
            try:
                stable_digest(value)
            except PromptContractError as exc:
                raise AgentGatewayError(f"{where}.{name} 不是合法有限 JSON 值: {exc}") from exc
            entity = spec.get("entity")
            if entity:
                missing = set(value) - entity_names[entity]
                if missing:
                    raise AgentGatewayError(
                        f"{where}.{name} 引用了未登记实体: {', '.join(sorted(missing))}")
            normalized[name] = copy.deepcopy(value)
        return normalized

    def _validated(self, plan: Mapping[str, Any], data: Mapping[str, Any],
                   *, require_revision: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(plan, Mapping):
            raise AgentGatewayError("ChapterPlan 必须是对象")
        allowed = {"contract_version", "chapter", "expected_revision", "chapter_patch",
                   "shots", "provenance"}
        unknown = set(plan) - allowed
        if unknown:
            raise AgentGatewayError("ChapterPlan 含未知字段: " + ", ".join(sorted(unknown)))
        required = {"contract_version", "chapter", "expected_revision", "shots", "provenance"}
        missing = required - set(plan)
        if missing:
            raise AgentGatewayError("ChapterPlan 缺字段: " + ", ".join(sorted(missing)))
        contract = self.registry.chapter_plan
        if plan.get("contract_version") != contract["version"]:
            raise AgentGatewayError(
                f"ChapterPlan contract_version 必须为 {contract['version']}")
        target = str(plan.get("chapter") or "")
        _target(target)
        expected = plan.get("expected_revision")
        if not isinstance(expected, str) or not REVISION_RE.fullmatch(expected):
            raise AgentGatewayError("expected_revision 必须是完整 sha256 revision")
        current = chapter_revision(data)
        if require_revision and expected != current:
            raise AgentGatewayError(
                f"章节 revision 冲突：expected={expected} current={current}；重新读取 context 后重算计划")

        chapter_patch = self._validate_fields(
            plan.get("chapter_patch", {}), contract["chapter_fields"], "chapter_patch", data)
        chapter_specs = contract["chapter_fields"]
        # 章级字段逐个比对：与盘上相同的剔除并在 summary 点名，整份计划无变更才在收尾拒绝；
        # 防重放靠 revision 校验，不靠逐字段拒绝
        persisted = _changed_fields(data, chapter_patch, chapter_specs)
        unchanged_chapter = sorted(name for name in chapter_patch if name not in persisted)
        chapter_patch = {name: value for name, value in chapter_patch.items() if name in persisted}
        # 失效传播与锁校验只看生效值：写明引擎推导的缺省会落盘，但不是变更
        effective_changed = sorted(_changed_fields(
            _effective_chapter_baseline(data, chapter_patch, chapter_specs),
            chapter_patch, chapter_specs))
        raw_ops = plan.get("shots")
        if not isinstance(raw_ops, list):
            raise AgentGatewayError("shots 必须是数组")
        current_shots = data.get("shots")
        if not isinstance(current_shots, list):
            raise AgentGatewayError("当前章节 shots 必须是数组")
        invalid_shots = [
            index for index, shot in enumerate(current_shots)
            if not isinstance(shot, dict)
            or not isinstance(shot.get("id"), int)
            or isinstance(shot.get("id"), bool)
            or shot["id"] <= 0
        ]
        if invalid_shots:
            raise AgentGatewayError(
                "当前章节含非法镜头结构: " + ", ".join(f"shots[{i}]" for i in invalid_shots))
        current_ids = [shot["id"] for shot in current_shots]
        if len(current_ids) != len(set(current_ids)):
            raise AgentGatewayError("当前章节含重复镜号，拒绝计划式写入")
        chapter_locked = review.chapter_locked(current_shots, effective_changed)
        if chapter_locked:
            raise AgentGatewayError(
                "chapter_patch 变更影响已通过锁定阶段 " + ", ".join(chapter_locked)
                + "；要重生置 retake，只解锁不重生置 wfa（review set --state …）")
        existing = {shot["id"]: shot for shot in current_shots}
        max_id = max(existing, default=0)
        last_add_id = max_id
        seen: set[int] = set()
        operations = []
        counts = {name: 0 for name in contract["operations"]}
        for index, raw in enumerate(raw_ops):
            where = f"shots[{index}]"
            if not isinstance(raw, Mapping):
                raise AgentGatewayError(f"{where} 必须是对象")
            extra = set(raw) - {"op", "id", "fields", "prompt_spec", "note"}
            if extra:
                raise AgentGatewayError(f"{where} 含未知字段: {', '.join(sorted(extra))}")
            op = raw.get("op")
            shot_id = raw.get("id")
            if op not in contract["operations"]:
                raise AgentGatewayError(f"{where}.op 不合法")
            if not isinstance(shot_id, int) or isinstance(shot_id, bool) or shot_id <= 0:
                raise AgentGatewayError(f"{where}.id 必须是正整数")
            where = f"shots[{index}](镜{shot_id})"
            if shot_id in seen:
                raise AgentGatewayError(f"ChapterPlan 重复操作镜 {shot_id}")
            seen.add(shot_id)
            fields = self._validate_fields(
                raw.get("fields", {}), contract["shot_fields"], f"{where}.fields", data)
            if "dur" in fields and float(fields["dur"]) <= 0:
                raise AgentGatewayError(f"{where}.fields.dur 必须大于 0")
            if "profile" in fields:
                try:
                    self.catalog.profile_skill(fields["profile"])
                except AgentCatalogError as exc:
                    raise AgentGatewayError(
                        f"{where}.fields.profile 未登记: {fields['profile']}") from exc
            prompt_spec = None
            if "prompt_spec" in raw:
                try:
                    prompt_spec = PromptSpec.parse(raw["prompt_spec"], registry=self.registry)
                except PromptContractError as exc:
                    raise AgentGatewayError(f"{where}.prompt_spec: {exc}") from exc
            note = raw.get("note")
            if note is not None and not isinstance(note, str):
                raise AgentGatewayError(f"{where}.note 必须是字符串")
            if note is not None and op in {"add", "update"}:
                raise AgentGatewayError(f"{where}.note 只允许 omit/restore 使用")
            if op == "add":
                if shot_id in existing or shot_id <= last_add_id:
                    raise AgentGatewayError(
                        f"{where}: add id 必须按升序且大于 {last_add_id}")
                last_add_id = shot_id
                missing_add = set(contract["required_add_fields"]) - set(fields)
                if missing_add:
                    raise AgentGatewayError(
                        f"{where}: add 缺字段 {', '.join(sorted(missing_add))}")
                if prompt_spec is None:
                    raise AgentGatewayError(f"{where}: add 必须提供 prompt_spec")
            elif shot_id not in existing:
                raise AgentGatewayError(f"{where}: 镜 {shot_id} 不存在")
            if op == "update":
                current_shot = existing[shot_id]
                projected = prompt_spec.project_fields() if prompt_spec is not None else {}
                # merge 字段（如 sketch）按合并后的结果判变化——与 apply 的落盘语义
                # 同源，否则提交一份与现状相同的 beats 也会被记作一次实际变化
                changes = _changed_fields(current_shot, fields, contract["shot_fields"])
                changes.update(
                    name for name, value in projected.items()
                    if current_shot.get(name) != value
                )
                if not changes:
                    raise AgentGatewayError(f"{where}: update 没有实际变化")
                locked = [
                    stage for stage in review.STAGES
                    if changes.intersection(review.fields_for(stage))
                    and review.is_locked(current_shot, stage)
                ]
                if locked:
                    raise AgentGatewayError(
                        f"{where}: 变更影响已通过锁定阶段 {', '.join(locked)}；"
                        "要重生置 retake，只解锁不重生置 wfa（review set --state …）")
            if op in {"omit", "restore"} and (fields or prompt_spec is not None):
                raise AgentGatewayError(f"{where}: {op} 只允许 id 与 note")
            if op == "omit" and review.is_omitted(existing[shot_id]):
                raise AgentGatewayError(f"{where}: 镜 {shot_id} 已是 omt")
            if op == "restore" and not review.is_omitted(existing[shot_id]):
                raise AgentGatewayError(f"{where}: 镜 {shot_id} 当前不是 omt")
            operations.append({
                "op": op, "id": shot_id, "fields": fields,
                "prompt_spec": prompt_spec, "note": note.strip() if isinstance(note, str) else None,
            })
            counts[op] += 1

        provenance = plan.get("provenance")
        if not isinstance(provenance, Mapping):
            raise AgentGatewayError("provenance 必须是对象")
        extra = set(provenance) - set(contract["provenance_fields"])
        if extra:
            raise AgentGatewayError("provenance 含未知字段: " + ", ".join(sorted(extra)))
        normalized_provenance = self._validate_fields(
            provenance, contract["provenance_fields"], "provenance", data)
        normalized_provenance = {
            name: value.strip() for name, value in normalized_provenance.items()
        }
        for required_name in ("host", "model"):
            if not normalized_provenance.get(required_name):
                raise AgentGatewayError(f"provenance.{required_name} 必填")
        if not chapter_patch and not operations:
            raise AgentGatewayError(
                "ChapterPlan 没有任何变更"
                + (f"（chapter_patch 未变化字段: {', '.join(unchanged_chapter)}）"
                   if unchanged_chapter else ""))
        normalized = {
            "contract_version": contract["version"],
            "chapter": target,
            "expected_revision": expected,
            "chapter_patch": chapter_patch,
            "shots": operations,
            "provenance": normalized_provenance,
        }
        summary = {
            "chapter_fields": sorted(chapter_patch),
            "unchanged_chapter_fields": unchanged_chapter,
            "chapter_effective_changes": effective_changed,
            "shot_operations": counts,
            "shot_ids": [item["id"] for item in operations],
        }
        return normalized, summary

    def validate(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        target = str(plan.get("chapter") or "") if isinstance(plan, Mapping) else ""
        _pid, _cid, _path, data = self._chapter(target)
        normalized, summary = self._validated(plan, data)
        return {
            "ok": True,
            "chapter": target,
            "revision": chapter_revision(data),
            "plan_digest": stable_digest(self._plan_json(normalized)),
            "summary": summary,
        }

    @staticmethod
    def _plan_json(plan: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(plan))
        for item in result.get("shots") or []:
            spec = item.get("prompt_spec")
            if isinstance(spec, PromptSpec):
                item["prompt_spec"] = spec.as_dict()
        return result

    def apply(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        target = str(plan.get("chapter") or "") if isinstance(plan, Mapping) else ""
        pid, cid = _target(target)
        series = self.workspace.get_project(pid)
        path = series.get_chapter_path(cid)
        with _ChapterLock(path):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentGatewayError(f"无法读取章节 {target}: {exc}") from exc
            normalized, summary = self._validated(plan, current)
            before = chapter_revision(current)
            updated = copy.deepcopy(current)
            for name, value in normalized["chapter_patch"].items():
                updated[name] = _merged_field(
                    updated.get(name), value, self.registry.chapter_plan["chapter_fields"][name])
            # 改了决定某阶段产物的生效值，已产出未锁定的镜进重做队列
            chapter_changed = set(summary["chapter_effective_changes"])
            chapter_stages = [stage for stage, owned in review.CHAPTER_STAGE_FIELDS.items()
                              if chapter_changed & owned]
            shots = updated.setdefault("shots", [])
            by_id = {shot.get("id"): shot for shot in shots if isinstance(shot, dict)}
            for operation in normalized["shots"]:
                op, shot_id = operation["op"], operation["id"]
                if op == "add":
                    shot = {"id": shot_id, **operation["fields"]}
                    shot.update(operation["prompt_spec"].project_fields())
                    shots.append(shot)
                    by_id[shot_id] = shot
                elif op == "update":
                    shot = by_id[shot_id]
                    # 镜级字段与章级同规走 _merged_field：merge 字段（sketch）只覆写
                    # 提交的子键，engine 管的 `sheet` 等同层子字段原地保留
                    shot_specs = self.registry.chapter_plan["shot_fields"]
                    changed = _changed_fields(shot, operation["fields"], shot_specs)
                    for name, value in operation["fields"].items():
                        shot[name] = _merged_field(shot.get(name), value, shot_specs[name])
                    if operation["prompt_spec"] is not None:
                        projected = operation["prompt_spec"].project_fields()
                        changed.update(k for k, v in projected.items() if shot.get(k) != v)
                        shot.update(projected)
                    _retake_stale_products(shot, changed)
                elif op == "omit":
                    review.set_state(by_id[shot_id], "shot", "omt", note=operation["note"])
                else:
                    review.set_state(by_id[shot_id], "shot", "todo", note=operation["note"])
            if chapter_stages:
                for shot in shots:
                    if isinstance(shot, dict) and not review.is_omitted(shot):
                        review.retake_produced(shot, chapter_stages)

            serializable_plan = self._plan_json(normalized)
            provenance = {
                "contract_version": normalized["contract_version"],
                "prompt_contract_version": self.registry.prompt["version"],
                "plan_digest": stable_digest(serializable_plan),
                "before_revision": before,
                "host": normalized["provenance"]["host"],
                "model": normalized["provenance"]["model"],
                "applied_at": _now(),
            }
            if normalized["provenance"].get("request_id"):
                provenance["request_id"] = normalized["provenance"]["request_id"]
            ledger = updated.setdefault("agent_provenance", [])
            if not isinstance(ledger, list):
                raise AgentGatewayError("当前章节 agent_provenance 必须是数组")
            ledger.append(provenance)
            self.workspace.store.save_chapter(pid, cid, updated)
            after = chapter_revision(updated)
        return {
            "ok": True,
            "chapter": target,
            "before_revision": before,
            "revision": after,
            "plan_digest": provenance["plan_digest"],
            "summary": summary,
        }

    def explain(self, target: str, shot_id: int, stage: str) -> dict[str, Any]:
        if stage not in {"image", "video"}:
            raise AgentGatewayError("stage 必须是 image 或 video")
        _pid, _cid, _path, data = self._chapter(target)
        shot = next((item for item in data.get("shots") or []
                     if isinstance(item, dict) and item.get("id") == shot_id), None)
        if shot is None:
            raise AgentGatewayError(f"镜 {shot_id} 不存在")
        gen_stage = "image" if stage == "image" else "clip"
        envelope = (((shot.get("gen") or {}).get(gen_stage) or {}).get("envelope"))
        current_spec = PromptSpec.from_shot(shot, registry=self.registry)
        reasons = []
        if not isinstance(envelope, dict):
            # 无 envelope 的三种事实分开报：素材直供（画面不是提示词生成的）与
            # 局部改造（画面叠加了人工矩形编辑）都是**正确状态**而非欠账，
            # 与「从没走过契约生成」混成一个原因会误导指挥层去重生它们。
            # 这两态只存在于 image（supply/refine 只写 gen.image），
            # video 阶段恒走通用原因。
            gen_entry = (shot.get("gen") or {}).get(gen_stage) or {}
            if gen_stage == "image" and gen_entry.get("provider") == "supplied":
                reasons.append("image_supplied_externally")
            elif gen_stage == "image" and isinstance(gen_entry.get("refine"), dict):
                reasons.append("image_manually_refined")
            else:
                reasons.append("not_generated_with_prompt_envelope")
            envelope = None
        else:
            if envelope.get("contract_version") != self.registry.prompt["version"]:
                reasons.append("prompt_contract_changed")
            if envelope.get("compiler_version") != self.registry.prompt["compiler_version"]:
                reasons.append("prompt_compiler_changed")
            if envelope.get("spec_revision") != current_spec.revision:
                reasons.append("prompt_spec_changed")
            profile = str(data.get("profile") or "narration")
            skill = str(data.get("skill") or self.catalog.bound_profile(profile))
            if envelope.get("skill_revision") != self.catalog.bound_skill(skill)["digest"]:
                reasons.append("skill_changed")
            from .models import ConfigStore, ModelRouter
            store = ConfigStore.load(self.config)
            router = ModelRouter(store)
            shot_profile = str(shot.get("profile") or profile)
            if stage == "image":
                provider = router.resolve("image", shot_profile)[0]
                params = dict(store.profile(shot_profile).get("image") or {})
            else:
                from .models import resolve_video
                provider, params = resolve_video(router, store, data, shot_profile)
            if envelope.get("profile_revision") != profile_revision(
                    shot_profile, provider, params):
                reasons.append("profile_changed")
            current_reference_digests = _document_reference_digests(data)
            # tail_frame 是引擎瞬态接力素材：文档落点在 gen 子树（上面的摘要收集
            # 刻意跳过 gen），镜内注入时引用的还是有时效的官方 URL——参与比对会把
            # 每个接力片段刚生成即恒判过期。承接血缘由 gen.clip.tail_relay_from
            # 留痕，不参与引用漂移判定。
            stale_references = [
                str(item.get("id") or "") for item in (envelope.get("references") or [])
                if isinstance(item, Mapping)
                and item.get("role") != "tail_frame"
                and item.get("sha256") not in current_reference_digests
            ]
            if stale_references:
                reasons.append("references_changed")
        return {
            "chapter": target,
            "shot": shot_id,
            "stage": stage,
            "stale": bool(reasons),
            "stale_reasons": reasons,
            "current_spec_revision": current_spec.revision,
            "stale_references": stale_references if envelope is not None else [],
            "envelope": copy.deepcopy(envelope),
        }


def load_plan(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentGatewayError(f"无法读取 ChapterPlan: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentGatewayError("ChapterPlan 顶层必须是对象")
    return value
