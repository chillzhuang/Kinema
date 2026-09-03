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

"""Skill/Profile 运行时视图。

唯一真源是 ``agent/manifest.json``；``tools/agent_assets.py`` 把它与 Skill 源码编译成
包内 catalog。本模块只提供业务侧需要的薄 API，不维护任何手写目录或语态表。
"""
from __future__ import annotations

from .agent_system import AgentCatalog


_CATALOG = AgentCatalog.load()
SKILLS: list[dict] = _CATALOG.route_catalog()
_PROFILE_TO_SKILL = {profile: item["id"] for item in SKILLS for profile in item["profiles"]}
# 语态枚举唯一定义在编译器侧（agent_assets），运行时转发同一份——两处各写
# 一份 tuple/set 时，编译期校验与 lint 消费的合法值迟早分叉
from .agent_assets import VOICEOVER_MODES  # noqa: E402
_VOICEOVER_DEFAULTS: dict[str, str] = {
    item["id"]: item["voiceover"] for item in _CATALOG.all()
    if item.get("voiceover") is not None
}


def skill_for_profile(profile: str | None) -> str:
    """画风 → 归属 Skill id；未知显式值直接报错，空值才使用通用工作流。"""
    return _CATALOG.profile_skill(profile)


def validate_skill(skill: str) -> str:
    """校验显式 Skill 并返回规范 id。"""
    return _CATALOG.get(skill)["id"]


def voiceover_default(profile: str | None, skill: str | None = None) -> str:
    """画风/绑定 skill → 旁白语态缺省（lead/sparse/none）。

    显式绑定的 Skill（如 overlay）优先于画风派生。"""
    return _CATALOG.voiceover_default(profile, skill)


def skill_catalog() -> list[dict]:
    """全量 skill 目录（有序，供 Studio overview 下发、前端建项目分组用）。"""
    return [{"id": s["id"], "cmd": s["cmd"], "label": s["label"],
             "en": s["en"], "usage": s["usage"], "profiles": list(s["profiles"])}
            for s in SKILLS]


def skill_board() -> dict:
    """指挥层全量大屏视图（Studio ``#/skill`` 只读下发）。

    与 :func:`skill_catalog` 的差别：那份是画风分组目录（只含 route/workflow，
    供建项目分组），这份是全集群——七类 kind 一条不落，字段直接投影编译 catalog，
    前端零硬编码条目；``catalog_version``/``manifest_digest`` 供页面标明数据出处。"""
    items = []
    for item in _CATALOG.all():
        cat = item.get("catalog") or {}
        items.append({
            "id": item["id"], "cmd": item["cmd"], "kind": item["kind"],
            "status": item["status"],
            "label": cat.get("label"), "en": cat.get("en"), "usage": cat.get("usage"),
            "description": item["description"],
            "profiles": list(item.get("profiles") or []),
            "default_profile": item.get("default_profile"),
            "depends_on": list(item.get("depends_on") or []),
            "activation": list(item.get("activation") or []),
            "voiceover": item.get("voiceover"),
            "permissions": list(item.get("permissions") or []),
            "source": item["source"],
            "entrypoint": item["entrypoint"],
        })
    return {"catalog_version": _CATALOG.version,
            "manifest_digest": _CATALOG.manifest_digest,
            "skills": items}


def all_profiles() -> set[str]:
    """目录覆盖的全部画风（漂移守卫用：应恰好等于 models.yaml profiles）。"""
    return set(_PROFILE_TO_SKILL)
