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

"""Kinema Agent 运行时：结构化目录、确定性路由与健康检查。

本模块不解析 Skill Markdown，也不调用 LLM。所有运行时事实来自编译进 Python 包的
``_generated/agent_catalog.json``；缺失或损坏直接报错，不维护第二套默认表。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import ConfigError


class AgentCatalogError(ConfigError):
    """Agent catalog 缺失、损坏或调用参数不合法。"""


# 落盘绑定指向已退役 Skill/画风时追加的换绑指引（见 `AgentCatalog.bound_skill`）。
_RETIRED_BINDING_HINT = (
    "——项目仍绑定着它，该值已不在 catalog 内。用 "
    "`project set <项目id> --skill <新 id>` 或 `--profile <新画风>` 换绑；"
    "已建章节各持一份建章时的拷贝，走 `chapter set <项目id> <章节id> --skill …`"
    "（或 `--inherit` 回落项目派生）"
    "（在册清单：`agent catalog --json`）"
)


@dataclass(frozen=True)
class RouteDecision:
    skill: str
    source: str
    reason: str
    catalog_version: str
    digest: str
    kind: str
    status: str
    default_profile: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "source": self.source,
            "reason": self.reason,
            "catalog_version": self.catalog_version,
            "digest": self.digest,
            "kind": self.kind,
            "status": self.status,
            "default_profile": self.default_profile,
        }


class AgentCatalog:
    """已编译 Skill catalog 的只读值对象。"""

    def __init__(self, payload: dict[str, Any]):
        if payload.get("schema_version") != 2:
            raise AgentCatalogError("Agent catalog schema_version 必须为 2")
        version = payload.get("catalog_version")
        skills = payload.get("skills")
        if not isinstance(version, str) or not isinstance(skills, list) or not skills:
            raise AgentCatalogError("Agent catalog 缺 catalog_version 或 skills")
        by_id: dict[str, dict[str, Any]] = {}
        profiles: dict[str, str] = {}
        for item in skills:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise AgentCatalogError("Agent catalog 含非法 Skill 描述符")
            skill_id = item["id"]
            if skill_id in by_id:
                raise AgentCatalogError(f"Agent catalog Skill 重复: {skill_id}")
            if not isinstance(item.get("digest"), str) or not item["digest"].startswith("sha256:"):
                raise AgentCatalogError(f"Agent catalog Skill 缺 digest: {skill_id}")
            by_id[skill_id] = item
            if item.get("kind") in {"route", "workflow"}:
                for profile in item.get("profiles") or []:
                    if profile in profiles:
                        raise AgentCatalogError(
                            f"Agent catalog profile 重复绑定: {profile} -> {profiles[profile]}, {skill_id}")
                    profiles[profile] = skill_id
        if "kinema" not in by_id:
            raise AgentCatalogError("Agent catalog 缺通用工作流 kinema")
        self._payload = payload
        self._skills = tuple(skills)
        self._by_id = by_id
        self._profiles = profiles
        self.version = version

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "AgentCatalog":
        try:
            if path is None:
                text = resources.files("kinema").joinpath(
                    "_generated/agent_catalog.json").read_text(encoding="utf-8")
            else:
                text = Path(path).read_text(encoding="utf-8")
            payload = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentCatalogError(
                "Agent catalog 不可用；在源码工作区运行 `python3 tools/agent_assets.py compile`: "
                f"{exc}") from exc
        if not isinstance(payload, dict):
            raise AgentCatalogError("Agent catalog 顶层必须是对象")
        return cls(payload)

    @property
    def manifest_digest(self) -> str:
        return str(self._payload.get("manifest_digest") or "")

    def all(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._skills]

    def get(self, skill_id: str) -> dict[str, Any]:
        try:
            return dict(self._by_id[skill_id])
        except KeyError as exc:
            raise AgentCatalogError(f"未知 Skill: {skill_id}") from exc

    def has(self, skill_id: str) -> bool:
        return skill_id in self._by_id

    def bound_skill(self, skill_id: str) -> dict[str, Any]:
        """解析**项目/章节已落盘**的 Skill 绑定（与 `get` 只差报错措辞）。

        绑定值是历史事实，不是本次输入：Skill 退役后它会让 `agent route` 与
        `agent context`——Agent 改章节的唯一正式入口——整条卡死，而「未知 Skill」
        本身不含任何可执行的下一步。仍然硬失败（绑定不许静默降级），但必须把
        换绑路径写进错误里。"""
        try:
            return self.get(skill_id)
        except AgentCatalogError as exc:
            raise AgentCatalogError(f"{exc}{_RETIRED_BINDING_HINT}") from exc

    def bound_profile(self, profile: str | None) -> str:
        """解析已落盘的 profile 绑定 → 归属 Skill；退役画风同 `bound_skill` 待遇。"""
        try:
            return self.profile_skill(profile)
        except AgentCatalogError as exc:
            raise AgentCatalogError(f"{exc}{_RETIRED_BINDING_HINT}") from exc

    def route_catalog(self) -> list[dict[str, Any]]:
        """Studio 的画风分组目录；专项/项目/overlay 不伪装成 profile 路由。"""
        result = []
        for item in self._skills:
            if item.get("kind") not in {"route", "workflow"}:
                continue
            catalog = item["catalog"]
            result.append({
                "id": item["id"],
                "cmd": item["cmd"],
                "label": catalog["label"],
                "en": catalog["en"],
                "usage": catalog["usage"],
                "profiles": list(item["profiles"]),
            })
        return result

    def profile_skill(self, profile: str | None) -> str:
        if profile is None or not str(profile).strip():
            return "kinema"
        try:
            return self._profiles[str(profile)]
        except KeyError as exc:
            raise AgentCatalogError(f"未知 profile: {profile}") from exc

    def voiceover_default(self, profile: str | None, skill: str | None = None) -> str:
        # lint 会故意吞入半成品/损坏字段做纯计算体检；这里解析的是“缺省语态”而不是
        # 显式路由命令。非字符串或未知值等同未声明，统一回到通用工作流；真正的
        # `agent route` 与项目创建仍走 get/profile_skill 的严格校验，不共享这条边界。
        if isinstance(skill, str) and skill in self._by_id:
            item = self.get(skill)
            if item.get("voiceover") is not None:
                return str(item["voiceover"])
        skill_id = self._profiles.get(profile) if isinstance(profile, str) else None
        item = self.get(skill_id or "kinema")
        value = item.get("voiceover")
        if value is None:
            raise AgentCatalogError(f"Skill 未声明 voiceover: {item['id']}")
        return str(value)

    def route(self, *, project_skill: str | None = None, skill: str | None = None,
              profile: str | None = None) -> RouteDecision:
        selected: dict[str, Any]
        source: str
        reason: str
        if project_skill and project_skill.strip():
            selected = self.bound_skill(project_skill.strip())
            source = "project.skill"
            reason = f"项目已绑定 {selected['id']}，项目绑定优先于本次显式选择"
        elif skill and skill.strip():
            selected = self.get(skill.strip())
            source = "explicit.skill"
            reason = f"使用显式 Skill {selected['id']}"
        elif profile and profile.strip():
            skill_id = self.profile_skill(profile.strip())
            selected = self.get(skill_id)
            source = "explicit.profile"
            reason = f"profile {profile.strip()} 确定性绑定到 {skill_id}"
        else:
            selected = self.get("kinema")
            source = "default"
            reason = "没有项目绑定、显式 Skill 或显式 profile，使用通用工作流 kinema"
        return RouteDecision(
            skill=selected["id"],
            source=source,
            reason=reason,
            catalog_version=self.version,
            digest=selected["digest"],
            kind=selected["kind"],
            status=selected["status"],
            default_profile=selected.get("default_profile"),
        )


def agent_doctor(explicit_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """检查运行时 catalog/contracts、生成漂移、发现别名与上下文预算。"""
    from .agent_assets import AgentAssetError, alias_error, check_assets, repository_root

    findings: list[dict[str, Any]] = []
    try:
        catalog = AgentCatalog.load()
        findings.append({
            "id": "runtime_catalog",
            "ok": True,
            "detail": f"catalog {catalog.version} · {len(catalog.all())} skills · {catalog.manifest_digest}",
        })
    except AgentCatalogError as exc:
        catalog = None
        findings.append({"id": "runtime_catalog", "ok": False, "detail": str(exc)})

    try:
        from .prompt_contract import AgentContractRegistry, PromptContractError
        contracts = AgentContractRegistry.load()
        prompt = contracts.describe("prompt")
        plan = contracts.describe("chapter-plan")
        findings.append({
            "id": "runtime_contracts",
            "ok": True,
            "detail": (f"{prompt['contract']['version']} {prompt['digest']} · "
                       f"{plan['contract']['version']} {plan['digest']}"),
        })
    except PromptContractError as exc:
        findings.append({"id": "runtime_contracts", "ok": False, "detail": str(exc)})

    try:
        root = repository_root(explicit_root)
        assets = check_assets(root)
        findings.append({
            "id": "generated_assets",
            "ok": assets["ok"],
            "detail": ("源码与生成物一致" if assets["ok"] else "；".join(assets["errors"])),
        })
        alias_problem = alias_error(root)   # 判据与 check_assets 同源（alias_error 单点）
        findings.append({
            "id": "skill_discovery",
            "ok": alias_problem is None,
            "detail": alias_problem or ".agents/skills 与 .claude/skills 指向同一消费目录",
        })
        budget = json.loads((root / "agent" / "manifest.json").read_text(encoding="utf-8"))["budgets"]
        kernel_bytes = len((root / "AGENTS.md").read_bytes())
        max_lines = max(
            len(path.read_text(encoding="utf-8").splitlines())
            for path in (root / ".claude" / "skills").glob("*/SKILL.md")
        )
        budget_ok = kernel_bytes <= budget["agent_kernel_bytes"] and max_lines <= budget["skill_lines"]
        findings.append({
            "id": "context_budget",
            "ok": budget_ok,
            "detail": (f"AGENTS.md {kernel_bytes}/{budget['agent_kernel_bytes']} bytes · "
                       f"最大 SKILL.md {max_lines}/{budget['skill_lines']} 行"),
        })
    except (AgentAssetError, OSError, json.JSONDecodeError) as exc:
        findings.append({"id": "workspace_assets", "ok": False, "detail": str(exc)})

    hosts: list[dict[str, Any]] = []
    for host, command in (("codex", "codex"), ("claude-code", "claude"), ("cursor", "cursor")):
        executable = shutil.which(command)
        version = None
        if executable:
            try:
                probe = subprocess.run(
                    [executable, "--version"], capture_output=True, text=True, timeout=2, check=False)
                version = (probe.stdout or probe.stderr).strip().splitlines()[0] or None
            except (OSError, subprocess.SubprocessError, IndexError):
                version = None
        hosts.append({"host": host, "installed": bool(executable), "path": executable, "version": version})
    findings.append({
        "id": "host_runtime",
        "ok": True,
        "detail": " · ".join(
            f"{item['host']}={item['version'] or ('installed' if item['installed'] else 'not-installed')}"
            for item in hosts),
    })

    return {
        "ok": all(item["ok"] for item in findings),
        "catalog_version": catalog.version if catalog else None,
        "hosts": hosts,
        "findings": findings,
    }
