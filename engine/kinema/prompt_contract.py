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

"""Prompt 与 Agent 写入协议的版本化运行时契约。"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError


class PromptContractError(ConfigError):
    """PromptSpec、PromptEnvelope 或机器契约不合法。"""


def _canonical_value(value: Any) -> Any:
    """消除 JSON 表示差异；整数与等值浮点在所有宿主下得到同一摘要。"""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PromptContractError("值无法规范化为 JSON: 数值必须有限")
        if value == 0 or value.is_integer():
            return int(value)
        return value
    if isinstance(value, Mapping):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """返回跨宿主稳定的 JSON 表示。"""
    try:
        return json.dumps(
            _canonical_value(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PromptContractError(f"值无法规范化为 JSON: {exc}") from exc


def stable_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def reference_digest(source: Any) -> str:
    """本地引用按内容、远端或不可读标识按规范字符串摘要。"""
    value = str(source or "").strip()
    path = Path(value)
    try:
        if value and path.is_file():
            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return stable_digest(value)


def profile_revision(profile: str, provider: Any, params: Mapping[str, Any]) -> str:
    """Prompt 相关 profile/provider 能力快照；不含连接端点和密钥。"""
    return stable_digest({
        "profile": profile,
        "params": dict(params),
        "provider": {
            "name": getattr(provider, "name", ""),
            "model": getattr(provider, "model", ""),
            "prompt_lang": getattr(provider, "prompt_lang", "zh"),
            "resolution": getattr(provider, "resolution", ""),
            "max_prompt_chars": int(getattr(provider, "max_prompt_chars", 0) or 0),
            "supports_reference_video": bool(
                getattr(provider, "supports_reference_video", False)),
            "supports_reference_images": bool(
                getattr(provider, "supports_reference_images", False)),
            "supports_last_frame": bool(getattr(provider, "supports_last_frame", True)),
        },
    })


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise PromptContractError(f"{where} 必须是字符串")
    return " ".join(value.split())


class AgentContractRegistry:
    """只读加载资产编译器生成的机器契约。"""

    def __init__(self, data: Mapping[str, Any]):
        self._data = json.loads(canonical_json(data))
        if self._data.get("schema_version") != 1:
            raise PromptContractError("Agent contracts schema_version 不受支持")
        if self._data.get("contract_version") != "agent-contracts/v1":
            raise PromptContractError("Agent contracts contract_version 不受支持")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AgentContractRegistry":
        source = Path(path) if path else Path(__file__).parent / "_generated" / "agent_contracts.json"
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptContractError(f"无法加载 Agent contracts: {exc}") from exc
        if not isinstance(data, dict):
            raise PromptContractError("Agent contracts 顶层必须是对象")
        return cls(data)

    @property
    def prompt(self) -> dict[str, Any]:
        return json.loads(canonical_json(self._data["prompt"]))

    @property
    def chapter_plan(self) -> dict[str, Any]:
        return json.loads(canonical_json(self._data["chapter_plan"]))

    def describe(self, name: str) -> dict[str, Any]:
        if name == "prompt":
            value = self.prompt
        elif name == "chapter-plan":
            value = self.chapter_plan
        else:
            raise PromptContractError(f"未知 Agent contract: {name}")
        return {
            "name": name,
            "contract": value,
            "digest": stable_digest(value),
        }


@dataclass(frozen=True)
class PromptSpec:
    """严格、可摘要、可确定性投影的 Prompt 中间表示。"""

    contract_version: str
    image: tuple[tuple[str, str], ...]
    video: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, value: Mapping[str, Any], *,
              registry: AgentContractRegistry | None = None,
              require_image: bool = True) -> "PromptSpec":
        registry = registry or AgentContractRegistry.load()
        if not isinstance(value, Mapping):
            raise PromptContractError("PromptSpec 必须是对象")
        expected = {"contract_version", "image", "video"}
        unknown = set(value) - expected
        if unknown:
            raise PromptContractError("PromptSpec 含未知字段: " + ", ".join(sorted(unknown)))
        contract = registry.prompt
        if value.get("contract_version") != contract["version"]:
            raise PromptContractError(
                f"PromptSpec contract_version 必须为 {contract['version']}")
        normalized: dict[str, tuple[tuple[str, str], ...]] = {}
        for stage_name in ("image", "video"):
            raw = value.get(stage_name)
            if not isinstance(raw, Mapping):
                raise PromptContractError(f"PromptSpec.{stage_name} 必须是对象")
            stage = contract["stages"][stage_name]
            fields = stage["fields"]
            extra = set(raw) - set(fields)
            if extra:
                raise PromptContractError(
                    f"PromptSpec.{stage_name} 含未知字段: " + ", ".join(sorted(extra)))
            rows = tuple((name, _text(raw[name], f"PromptSpec.{stage_name}.{name}"))
                         for name in fields if name in raw and raw[name] is not None)
            present = {name for name, text in rows if text}
            for name in present:
                conflicts = set(fields[name].get("conflicts") or [])
                collided = sorted(present.intersection(conflicts))
                if collided:
                    raise PromptContractError(
                        f"PromptSpec.{stage_name}.{name} 与 "
                        + ", ".join(collided) + " 冲突")
            required_any = set(stage["required_any"])
            if required_any and (stage_name != "image" or require_image) \
                    and not present.intersection(required_any):
                raise PromptContractError(
                    f"PromptSpec.{stage_name} 至少填写一个: "
                    + ", ".join(stage["required_any"]))
            normalized[stage_name] = rows
        return cls(contract["version"], normalized["image"], normalized["video"])

    @classmethod
    def from_shot(cls, shot: Mapping[str, Any], *,
                  registry: AgentContractRegistry | None = None) -> "PromptSpec":
        """把现有章节作者字段投影回唯一 IR；不读取任何 engine-managed 字段。"""
        registry = registry or AgentContractRegistry.load()
        value = {
            "contract_version": registry.prompt["version"],
            "image": {
                "framing": str(shot.get("framing") or ""),
                "angle": str(shot.get("angle") or ""),
                "lens": str(shot.get("lens") or ""),
                "lighting": str(shot.get("lighting") or ""),
                "creative_notes": str(shot.get("image_prompt") or ""),
                "text_en": str(shot.get("image_prompt_en") or ""),
                "negative": str(shot.get("negative_prompt") or ""),
            },
            "video": {
                "action_delta": str(shot.get("action") or ""),
                "camera": str(shot.get("camera") or ""),
                "entry_state": str(shot.get("entry_state") or ""),
                "end_state": str(shot.get("end_state") or ""),
                "light_shift": str(shot.get("light_shift") or ""),
                "sound": str(shot.get("sfx") or ""),
                "creative_notes": str(shot.get("video_prompt") or ""),
                "text_en": str(shot.get("video_prompt_en") or ""),
            },
        }
        # 运行时从既有视频镜头恢复 IR 时，图像资产可能存在但旧作者字段为空；
        # 这不妨碍视频增量编译。Agent 新提交的 PromptSpec 仍走 parse 的严格默认。
        return cls.parse(value, registry=registry, require_image=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "image": dict(self.image),
            "video": dict(self.video),
        }

    @property
    def revision(self) -> str:
        return stable_digest(self.as_dict())

    def project_fields(self) -> dict[str, Any]:
        """确定性投影为章节现有 author-owned 字段。"""
        image = dict(self.image)
        video = dict(self.video)
        projected: dict[str, Any] = {}
        image_body = "，".join(value for name in (
            "subject", "action", "expression", "composition", "creative_notes")
                              if (value := image.get(name)))
        video_body = "，".join(value for name in (
            "secondary_motion", "creative_notes")
                              if (value := video.get(name)))
        projected["image_prompt"] = image_body
        projected["image_prompt_en"] = image.get("text_en", "")
        projected["negative_prompt"] = image.get("negative", "")
        for name in ("framing", "angle", "lens", "lighting"):
            projected[name] = image.get(name, "")
        projected["video_prompt"] = video_body
        projected["video_prompt_en"] = video.get("text_en", "")
        for source, target in (("action_delta", "action"), ("camera", "camera"),
                               ("entry_state", "entry_state"),
                               ("end_state", "end_state"), ("light_shift", "light_shift"),
                               ("sound", "sfx")):
            projected[target] = video.get(source, "")
        return projected


@dataclass(frozen=True)
class PromptReference:
    role: str
    id: str
    sha256: str

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "PromptReference":
        if not isinstance(value, Mapping) or set(value) != {"role", "id", "sha256"}:
            raise PromptContractError("Prompt reference 必须且只能包含 role/id/sha256")
        role = _text(value["role"], "reference.role")
        ref_id = _text(value["id"], "reference.id")
        digest = _text(value["sha256"], "reference.sha256")
        if not role or not ref_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise PromptContractError("Prompt reference role/id/sha256 不合法")
        return cls(role, ref_id, digest)

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "id": self.id, "sha256": self.sha256}


@dataclass(frozen=True)
class PromptEnvelope:
    contract_version: str
    compiler_version: str
    stage: str
    language: str
    positive: str
    negative: str
    prompt: str
    references: tuple[PromptReference, ...]
    spec_revision: str
    skill_revision: str
    profile_revision: str
    fingerprint: str

    @classmethod
    def create(cls, *, contract_version: str, compiler_version: str, stage: str,
               language: str, positive: str, negative: str, prompt: str,
               references: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
               spec_revision: str, skill_revision: str = "", profile_revision: str = "",
               ) -> "PromptEnvelope":
        if stage not in {"image", "video"}:
            raise PromptContractError(f"未知 Prompt stage: {stage}")
        if language not in {"zh", "en"}:
            raise PromptContractError(f"未知 Prompt language: {language}")
        refs = tuple(PromptReference.parse(item) for item in references)
        body = {
            "contract_version": contract_version,
            "compiler_version": compiler_version,
            "stage": stage,
            "language": language,
            "positive": positive,
            "negative": negative,
            "prompt": prompt,
            "references": [item.as_dict() for item in refs],
            "spec_revision": spec_revision,
            "skill_revision": skill_revision or stable_digest(""),
            "profile_revision": profile_revision or stable_digest(""),
        }
        return cls(
            contract_version=body["contract_version"],
            compiler_version=body["compiler_version"],
            stage=body["stage"],
            language=body["language"],
            positive=body["positive"],
            negative=body["negative"],
            prompt=body["prompt"],
            references=refs,
            spec_revision=body["spec_revision"],
            skill_revision=body["skill_revision"],
            profile_revision=body["profile_revision"],
            fingerprint=stable_digest(body),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "compiler_version": self.compiler_version,
            "stage": self.stage,
            "language": self.language,
            "positive": self.positive,
            "negative": self.negative,
            "prompt": self.prompt,
            "references": [item.as_dict() for item in self.references],
            "spec_revision": self.spec_revision,
            "skill_revision": self.skill_revision,
            "profile_revision": self.profile_revision,
            "fingerprint": self.fingerprint,
        }
