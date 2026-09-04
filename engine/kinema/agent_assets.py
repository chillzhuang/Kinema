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

"""Agent/Skill 控制平面的确定性编译器。

可编辑真源分两处：元数据与契约在 ``agent/``（``manifest.json``、``contracts.json``、
``adapters/``），Skill 正文与 references 直接在发现目录 ``.claude/skills/``——单源无拷贝。
本模块负责原地规范化 SKILL.md frontmatter（name/description 等元数据由 manifest 派生，
``kinema-digest`` 覆盖 manifest 条目与正文/references、刻意不含 frontmatter 自身与
``skill.json``，因此无自指），并编译运行时 catalog、契约、宿主入口和人读索引。
编译不使用时间戳、随机数或环境相关字段；相同输入必定得到字节级相同的输出。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PROFILE_RE = re.compile(r"^[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.S)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
REFERENCE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_/-])(references/[A-Za-z0-9_./-]+)")
KINDS = {"workflow", "route", "capability", "project", "system", "scaffold", "overlay"}
STATUSES = {"stable", "scaffold"}
# 语态枚举唯一定义（运行时 skills.py 转发本元组；lint 按序展示）。
# manifest 校验另行接受 None=该 skill 未声明语态缺省，见 validate_sources。
VOICEOVER_MODES = ("lead", "sparse", "none")
ACTIVATION_MODES = {"auto", "explicit", "project-bound"}
PERMISSIONS = {
    "workspace.read", "workspace.write", "kinema.cli", "network.research",
    "environment.configure",
}
CONTRACT_REFERENCE = Path(".claude/skills/kinema/references/prompt-contract.md")


class AgentAssetError(ValueError):
    """Agent 资产契约或编译产物不合法。"""


def repository_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """返回 Kinema 仓库根；编译命令只服务源码工作区，不做安装包猜测。"""
    root = Path(explicit).resolve() if explicit else Path(__file__).resolve().parents[2]
    if not (root / "agent" / "manifest.json").is_file():
        raise AgentAssetError(f"不是 Kinema Agent 源码工作区: {root}")
    return root


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / "agent" / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentAssetError(f"无法读取 Agent manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentAssetError("agent/manifest.json 顶层必须是对象")
    return value


def _read_contracts(root: Path) -> dict[str, Any]:
    path = root / "agent" / "contracts.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentAssetError(f"无法读取 Agent contracts: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentAssetError("agent/contracts.json 顶层必须是对象")
    return value


def validate_contracts(contracts: dict[str, Any]) -> None:
    """校验 Prompt 与 ChapterPlan 机器契约的闭合集合。"""
    errors: list[str] = []
    if contracts.get("schema_version") != 1:
        errors.append("contracts.schema_version 必须为 1")
    if contracts.get("contract_version") != "agent-contracts/v1":
        errors.append("contracts.contract_version 必须为 agent-contracts/v1")

    prompt = contracts.get("prompt")
    if not isinstance(prompt, dict):
        errors.append("contracts.prompt 必须是对象")
        prompt = {}
    if prompt.get("version") != "prompt/v1":
        errors.append("prompt.version 必须为 prompt/v1")
    if not SEMVER_RE.fullmatch(str(prompt.get("compiler_version") or "")):
        errors.append("prompt.compiler_version 必须是 x.y.z")
    if prompt.get("overflow_policy") != "error":
        errors.append("prompt.overflow_policy 必须为 error（禁止静默截断）")
    stages = prompt.get("stages")
    if not isinstance(stages, dict) or set(stages) != {"image", "video"}:
        errors.append("prompt.stages 必须且只能包含 image/video")
        stages = {}
    prompt_types = {"string"}
    for stage_name in ("image", "video"):
        stage = stages.get(stage_name)
        if not isinstance(stage, dict):
            errors.append(f"prompt.stages.{stage_name} 必须是对象")
            continue
        fields = stage.get("fields")
        if not isinstance(fields, dict) or not fields:
            errors.append(f"prompt.stages.{stage_name}.fields 必须是非空对象")
            fields = {}
        for name, spec in fields.items():
            if not isinstance(name, str) or not ID_RE.fullmatch(name.replace("_", "-")):
                errors.append(f"Prompt 字段名不合法: {stage_name}.{name}")
            if not isinstance(spec, dict) or spec.get("type") not in prompt_types \
                    or spec.get("owner") != "agent" \
                    or spec.get("allowed_stages") not in ([stage_name], ["image", "video"]) \
                    or spec.get("language") not in {"any", "zh", "en"} \
                    or spec.get("default") != "" \
                    or not isinstance(spec.get("conflicts"), list) \
                    or not isinstance(spec.get("deprecated"), bool) \
                    or not spec.get("projection") or not spec.get("description"):
                errors.append(f"Prompt 字段定义不完整: {stage_name}.{name}")
            elif any(conflict not in fields or conflict == name
                     for conflict in spec["conflicts"]):
                errors.append(f"Prompt 字段 conflicts 不合法: {stage_name}.{name}")
        required_any = stage.get("required_any")
        if not isinstance(required_any, list) or any(name not in fields for name in required_any):
            errors.append(f"prompt.stages.{stage_name}.required_any 引用了未知字段")

    plan = contracts.get("chapter_plan")
    if not isinstance(plan, dict):
        errors.append("contracts.chapter_plan 必须是对象")
        plan = {}
    if plan.get("version") != "chapter-plan/v1":
        errors.append("chapter_plan.version 必须为 chapter-plan/v1")
    if plan.get("operations") != ["add", "update", "omit", "restore"]:
        errors.append("chapter_plan.operations 顺序必须为 add/update/omit/restore")
    if plan.get("tasks") != ["storyboard", "image", "video", "review"]:
        errors.append("chapter_plan.tasks 顺序必须为 storyboard/image/video/review")
    # beat_list = 简笔分镜拍序列（`sketch.beats`）的专用类型：对象数组、`action`
    # 必填非空、其余键可选字符串——形态校验的运行时真源在 agent_gateway._beat_ok。
    # line_list = 镜内多段台词（`lines[]`）的专用类型：对象数组、`text` 必填非空、
    # 说话人/音色/情绪/英文对位可选——运行时真源在 agent_gateway._line_ok
    allowed_types = {"string", "number", "integer", "boolean", "string_list",
                     "object", "beat_list", "line_list"}

    def valid_field_spec(spec: Any) -> bool:
        if not isinstance(spec, dict) or spec.get("type") not in allowed_types:
            return False
        if "merge" in spec and (not isinstance(spec["merge"], bool)
                                 or spec.get("type") != "object"):
            return False
        if "additional_properties" in spec \
                and not isinstance(spec["additional_properties"], bool):
            return False
        for boundary in ("minimum", "maximum"):
            if boundary in spec and (spec.get("type") not in {"number", "integer"}
                                     or not isinstance(spec[boundary], (int, float))
                                     or isinstance(spec[boundary], bool)):
                return False
        if "minimum" in spec and "maximum" in spec \
                and spec["minimum"] > spec["maximum"]:
            return False
        if spec.get("type") == "object" and "properties" in spec:
            properties = spec["properties"]
            if not isinstance(properties, dict) or not properties:
                return False
            if not all(valid_field_spec(child) for child in properties.values()):
                return False
        return True

    for group in ("chapter_fields", "shot_fields", "provenance_fields"):
        fields = plan.get(group)
        if not isinstance(fields, dict) or not fields:
            errors.append(f"chapter_plan.{group} 必须是非空对象")
            continue
        for name, spec in fields.items():
            if not valid_field_spec(spec):
                errors.append(f"ChapterPlan 字段定义不合法: {group}.{name}")
    required_add = plan.get("required_add_fields")
    shot_fields = plan.get("shot_fields") if isinstance(plan.get("shot_fields"), dict) else {}
    if not isinstance(required_add, list) or any(name not in shot_fields for name in required_add):
        errors.append("chapter_plan.required_add_fields 引用了未知字段")
    if errors:
        raise AgentAssetError("Agent 契约校验失败:\n- " + "\n- ".join(errors))


def _render_contract_reference(contracts: dict[str, Any]) -> str:
    prompt = contracts["prompt"]
    plan = contracts["chapter_plan"]
    rows = [
        "<!-- 由 tools/agent_assets.py 根据 agent/contracts.json 生成；请勿手改。 -->",
        "",
        "# Prompt 正式契约",
        "",
        f"契约版本：`{prompt['version']}` · 编译器版本：`{prompt['compiler_version']}` · "
        f"ChapterPlan：`{plan['version']}`。机器真源是 `agent/contracts.json`；本文件仅供 Agent 作者阅读。",
        "",
        "PromptSpec 是计划与编译期 IR。章节只保存它确定性投影后的作者字段，不额外保存 PromptSpec 副本。",
        "PromptSpec 是全量替换语义：省略的槽位会投影为空并清除旧作者字段；修改时以 `agent context` 返回的当前 PromptSpec 为基线。",
        "画风、角色设定引用、负面地板和 provider 参数由编译器注入，Agent 不在语义槽里复制这些内容。",
        "",
    ]
    for stage_name, title in (("image", "图像语义"), ("video", "视频动作增量")):
        stage = prompt["stages"][stage_name]
        rows.extend([
            f"## {title}",
            "",
            stage["description"],
            "",
            "| 字段 | 责任 | 语言 | 投影目标 | 含义 |",
            "|---|---|---|---|---|",
        ])
        for name, spec in stage["fields"].items():
            rows.append(
                f"| `{name}` | `{spec['owner']}` | `{spec['language']}` | "
                f"`{spec['projection']}` | {spec['description']} |")
        if stage["required_any"]:
            values = "、".join(f"`{name}`" for name in stage["required_any"])
            rows.extend(["", f"至少一个字段非空：{values}。"])
        rows.append("")
    example = {
        "contract_version": prompt["version"],
        "image": {
            "subject": "主体身份",
            "action": "快门时刻动作",
            "composition": "空间与构图",
            "lighting": "光线",
            "text_en": "Complete English image semantics",
        },
        "video": {
            "action_delta": "首帧之后发生的动作",
            "secondary_motion": "次级运动",
            "camera": "单一主运镜",
            "end_state": "结束状态",
            "text_en": "Complete English motion delta",
        },
    }
    rows.extend([
        "## PromptSpec 示例",
        "",
        "```json",
        json.dumps(example, ensure_ascii=False, indent=2),
        "```",
        "",
        "## ChapterPlan 写入协议",
        "",
        "固定流程：先 `agent context` 取得最小上下文和 `revision`，再构造计划，先 `plan validate`，",
        "确认摘要后才 `plan apply`。apply 只接受当前 revision；冲突时重读上下文并重算计划。",
        "`chapter_patch` 里与现状相同的字段会被剔除并在 `summary.unchanged_chapter_fields` 列出；",
        "镜级 `update` 至少要改一个字段；整份计划没有任何变更时整份拒绝。",
        "写明引擎缺省会落盘但不算生效变更：`motion` 按内容定档、`audio_mode` 缺省 tracks、",
        "布尔开关缺席按引擎缺省（`voice_anchor` 开，其余关）。失效传播与 done 锁校验只看",
        "`summary.chapter_effective_changes`；`context.effective` 给出推导的 motion 与 audio_mode。",
        "",
        "允许的镜头操作：`add`、`update`、`omit`、`restore`。禁止 delete、镜头重排、任意 JSON Patch",
        "和整份章节覆盖。图像/视频字段只通过 `prompt_spec` 提交。",
        "",
        "新增镜头必须提供：" + "、".join(f"`{name}`" for name in plan["required_add_fields"])
        + " 与 `prompt_spec`。",
        "",
        "### 章节字段",
        "",
        "| 字段 | 类型 | 写入语义 |",
        "|---|---|---|",
    ])
    for name, spec in plan["chapter_fields"].items():
        properties = "、".join(f"`{key}`" for key in (spec.get("properties") or {}))
        semantics = (f"浅合并，只允许 {properties}" if spec.get("merge") is True
                     else "字段级替换")
        rows.append(f"| `{name}` | `{spec['type']}` | {semantics} |")
    rows.extend([
        "",
        "镜头白名单：" + "、".join(f"`{name}`" for name in plan["shot_fields"]) + "。",
        "",
        "### ChapterPlan 最小示例",
        "",
        "```json",
        json.dumps({
            "contract_version": plan["version"],
            "chapter": "demo/ch01",
            "expected_revision": "sha256:" + "0" * 64,
            "chapter_patch": {"voiceover": "sparse"},
            "shots": [{
                "op": "update",
                "id": 1,
                "fields": {"narration": "他听见身后的脚步。"},
                "prompt_spec": example,
            }],
            "provenance": {"host": "codex", "model": "model-id"},
        }, ensure_ascii=False, indent=2),
        "```",
        "",
    ])
    return "\n".join(rows)


def _config_profiles(root: Path) -> set[str]:
    """零依赖读取 models.yaml 的 profiles 一级键。

    这里只校验 Skill/Profile 绑定集合，不承担 YAML 语义解析；两个空格缩进的 profile
    键是 ``config/models.yaml`` 的稳定文档结构。
    """
    path = root / "config" / "models.yaml"
    lines = path.read_text(encoding="utf-8").splitlines()
    inside = False
    result: set[str] = set()
    for line in lines:
        if not inside:
            if line.strip() == "profiles:" and not line.startswith(" "):
                inside = True
            continue
        if line and not line.startswith((" ", "#")):
            break
        match = re.match(r"^  ([a-z0-9_]+):\s*(?:#.*)?$", line)
        if match:
            result.add(match.group(1))
    if not result:
        raise AgentAssetError("config/models.yaml 未解析到任何 profiles")
    return result


def _skill_body(text: str, skill_id: str) -> str:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise AgentAssetError(f".claude/skills/{skill_id}/SKILL.md 缺 YAML frontmatter")
    body = text[match.end():]
    return body.lstrip("\r\n").replace("\r\n", "\n").rstrip() + "\n"


def _frontmatter(skill: dict[str, Any], manifest: dict[str, Any], *, digest: str) -> str:
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    defaults = manifest["defaults"]
    rows = [
        "---",
        f"name: {skill['id']}",
        f"description: {quote(skill['description'])}",
        "metadata:",
        '  kinema-managed-by: "agent/manifest.json"',
        f"  kinema-kind: {quote(skill['kind'])}",
        f"  kinema-status: {quote(skill['status'])}",
        f"  kinema-version: {quote(manifest['catalog_version'])}",
        f"  kinema-owner: {quote(defaults['owner'])}",
        f"  kinema-source: {quote(defaults['source'])}",
        f"  kinema-trust: {quote(defaults['trust'])}",
        f"  kinema-digest: {quote(digest)}",
        "---",
        "",
    ]
    return "\n".join(rows)


def _expected_skill_text(root: Path, manifest: dict[str, Any], skill: dict[str, Any],
                         digest: str) -> str:
    """正文来自盘上包、frontmatter 由 manifest+digest 派生的规范全文（含行预算闸）。"""
    path = root / skill["source"] / "SKILL.md"
    body = _skill_body(path.read_text(encoding="utf-8"), skill["id"])
    rendered = _frontmatter(skill, manifest, digest=digest) + body
    if len(rendered.splitlines()) > manifest["budgets"]["skill_lines"]:
        raise AgentAssetError(
            f"{skill['id']}/SKILL.md 超过 {manifest['budgets']['skill_lines']} 行预算")
    return rendered


def _skill_digest(root: Path, skill: dict[str, Any]) -> str:
    source = root / skill["source"]
    digest = hashlib.sha256()
    digest.update(_canonical_bytes(skill))
    digest.update(b"\0")
    for path in sorted(p for p in source.rglob("*")
                       if p.is_file() and not p.is_symlink() and not p.name.startswith(".")):
        rel = path.relative_to(source).as_posix()
        if rel == "skill.json":
            continue  # 生成物投影：它携带 digest 本体，参与摘要即自指
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if rel == "SKILL.md":
            data = _skill_body(path.read_text(encoding="utf-8"), skill["id"]).encode("utf-8")
        else:
            data = path.read_bytes()
        digest.update(data)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _cycle_errors(skills: list[dict[str, Any]]) -> list[str]:
    graph = {skill["id"]: list(skill["depends_on"]) for skill in skills}
    visiting: list[str] = []
    visited: set[str] = set()
    errors: list[str] = []

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            start = visiting.index(node)
            errors.append("Skill 依赖循环: " + " -> ".join(visiting[start:] + [node]))
            return
        visiting.append(node)
        for dep in graph.get(node, []):
            visit(dep)
        visiting.pop()
        visited.add(node)

    for name in graph:
        visit(name)
    return errors


def _documentation_errors(source_root: Path, package: Path) -> list[str]:
    """检查正式相对链接、reference 断链与孤儿 reference。"""
    errors: list[str] = []
    citations: set[Path] = set()
    for markdown in sorted(package.rglob("*.md")):
        text = markdown.read_text(encoding="utf-8")
        for raw in MARKDOWN_LINK_RE.findall(text):
            value = raw.strip().split()[0].strip("<>")
            if not value or value.startswith(("http://", "https://", "mailto:", "#", "/")):
                continue
            target = (markdown.parent / value.split("#", 1)[0]).resolve()
            if not _within(target, source_root):
                errors.append(
                    f"Skill 链接逃逸源码根: {markdown.relative_to(source_root)} -> {value}")
            elif not target.exists():
                errors.append(
                    f"Skill 断链: {markdown.relative_to(source_root)} -> {value}")
            elif target != markdown.resolve():
                citations.add(target)
        # 兼容正文里的 `references/x.md` 作业指令；它不是 Markdown 链接，但同样是
        # Agent 会照着读取的正式依赖，必须参与断链与孤儿检查。
        for value in REFERENCE_TOKEN_RE.findall(text):
            target = (package / value.rstrip(".,;:，。；：）)]}" )).resolve()
            if not target.exists():
                errors.append(
                    f"Skill reference 断链: {markdown.relative_to(source_root)} -> {value}")
            elif target != markdown.resolve():
                citations.add(target)
    refs = package / "references"
    if refs.is_dir():
        for path in sorted(p.resolve() for p in refs.rglob("*") if p.is_file()):
            if path not in citations:
                errors.append(f"孤儿 Skill reference: {path.relative_to(source_root)}")
    return errors


def validate_sources(root: Path, manifest: dict[str, Any]) -> None:
    """校验 manifest、源码包、依赖图、profile 覆盖和信任边界。"""
    errors: list[str] = []
    if manifest.get("schema_version") != 2:
        errors.append("schema_version 必须为 2")
    if not SEMVER_RE.fullmatch(str(manifest.get("catalog_version") or "")):
        errors.append("catalog_version 必须是 x.y.z")

    defaults = manifest.get("defaults")
    if not isinstance(defaults, dict):
        errors.append("defaults 必须是对象")
        defaults = {}
    for key in ("owner", "license", "source", "trust"):
        if not defaults.get(key):
            errors.append(f"defaults.{key} 必填")
    if defaults.get("source") != "workspace" or defaults.get("trust") != "first-party":
        errors.append("stable 控制平面只接受 workspace / first-party 真源")

    budgets = manifest.get("budgets")
    if not isinstance(budgets, dict):
        errors.append("budgets 必须是对象")
        budgets = {}
    for key in ("agent_kernel_bytes", "skill_lines", "description_chars"):
        if not isinstance(budgets.get(key), int) or budgets.get(key, 0) <= 0:
            errors.append(f"budgets.{key} 必须是正整数")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        errors.append("outputs 必须是对象")
        outputs = {}
    output_paths: list[Path] = []
    for key in ("skills", "catalog", "contracts", "index"):
        value = outputs.get(key)
        if not isinstance(value, str) or not value:
            errors.append(f"outputs.{key} 必填")
        else:
            output_paths.append(root / value)
    adapters = outputs.get("adapters")
    if not isinstance(adapters, dict) or not adapters:
        errors.append("outputs.adapters 必须是非空映射")
        adapters = {}
    for source, target in adapters.items():
        if not isinstance(source, str) or not isinstance(target, str):
            errors.append("outputs.adapters 的 source/target 必须是字符串")
            continue
        if not (root / source).is_file():
            errors.append(f"Host Adapter 源不存在: {source}")
        output_paths.append(root / target)
    if len({path.resolve() for path in output_paths}) != len(output_paths):
        errors.append("outputs 存在重复目标")
    for path in output_paths:
        if not _within(path, root):
            errors.append(f"输出路径逃逸仓库根: {path}")

    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills:
        errors.append("skills 必须是非空数组")
        skills = []
    ids: set[str] = set()
    mapped_profiles: dict[str, str] = {}
    source_root = root / ".claude" / "skills"
    for index, skill in enumerate(skills):
        where = f"skills[{index}]"
        if not isinstance(skill, dict):
            errors.append(f"{where} 必须是对象")
            continue
        skill_id = skill.get("id")
        if not isinstance(skill_id, str) or not ID_RE.fullmatch(skill_id) or len(skill_id) > 64:
            errors.append(f"{where}.id 不合法: {skill_id!r}")
            continue
        if skill_id in ids:
            errors.append(f"Skill id 重复: {skill_id}")
        ids.add(skill_id)
        if skill.get("source") != f".claude/skills/{skill_id}":
            errors.append(f"{skill_id}.source 必须是 .claude/skills/{skill_id}")
        if skill.get("kind") not in KINDS:
            errors.append(f"{skill_id}.kind 不合法: {skill.get('kind')!r}")
        if skill.get("status") not in STATUSES:
            errors.append(f"{skill_id}.status 不合法: {skill.get('status')!r}")
        description = skill.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{skill_id}.description 必填")
        elif len(description) > min(1024, int(budgets.get("description_chars") or 1024)):
            errors.append(f"{skill_id}.description 超过预算: {len(description)} 字符")
        profiles = skill.get("profiles")
        if not isinstance(profiles, list) or len(profiles) != len(set(profiles)):
            errors.append(f"{skill_id}.profiles 必须是无重复数组")
            profiles = []
        for profile in profiles:
            if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
                errors.append(f"{skill_id} 含非法 profile: {profile!r}")
            if skill.get("kind") in {"route", "workflow"}:
                previous = mapped_profiles.get(profile)
                if previous:
                    errors.append(f"profile {profile} 重复绑定: {previous}, {skill_id}")
                mapped_profiles[profile] = skill_id
        default_profile = skill.get("default_profile")
        if default_profile is not None and (not isinstance(default_profile, str)
                                            or not PROFILE_RE.fullmatch(default_profile)):
            errors.append(f"{skill_id}.default_profile 不合法")
        deps = skill.get("depends_on")
        if not isinstance(deps, list) or len(deps) != len(set(deps)):
            errors.append(f"{skill_id}.depends_on 必须是无重复数组")
        activation = skill.get("activation")
        if not isinstance(activation, list) or not activation \
                or len(activation) != len(set(activation)) \
                or set(activation) - ACTIVATION_MODES:
            errors.append(f"{skill_id}.activation 不合法")
        if skill.get("voiceover") not in (*VOICEOVER_MODES, None):
            errors.append(f"{skill_id}.voiceover 不合法")
        permissions = skill.get("permissions")
        if not isinstance(permissions, list) or len(permissions) != len(set(permissions)) \
                or set(permissions) - PERMISSIONS:
            errors.append(f"{skill_id}.permissions 不合法")
        catalog = skill.get("catalog")
        if skill.get("kind") in {"route", "workflow"}:
            if not isinstance(catalog, dict) or any(not catalog.get(k) for k in ("label", "en", "usage")):
                errors.append(f"{skill_id}.catalog 必须提供 label/en/usage")
        elif catalog is not None:
            errors.append(f"{skill_id}.catalog 仅 route/workflow 可用")

        package = source_root / skill_id
        if not package.is_dir() or not (package / "SKILL.md").is_file():
            errors.append(f"Skill 源码包缺失: .claude/skills/{skill_id}/SKILL.md")
            continue
        if package.is_symlink():
            errors.append(f"Skill 源码包不得是 symlink: {skill_id}")
        for path in package.rglob("*"):
            if path.is_symlink():
                errors.append(f"Skill 源码不得含 symlink: {path.relative_to(root)}")
        try:
            _skill_body((package / "SKILL.md").read_text(encoding="utf-8"), skill_id)
        except (OSError, UnicodeError, AgentAssetError) as exc:
            errors.append(str(exc))
        errors.extend(_documentation_errors(source_root, package))

    if source_root.is_dir():
        package_ids = {p.name for p in source_root.iterdir() if p.is_dir() and not p.name.startswith(".")}
        for orphan in sorted(package_ids - ids):
            errors.append(f"未登记 Skill 包混入发现目录: .claude/skills/{orphan}")
        for missing in sorted(ids - package_ids):
            errors.append(f"manifest 指向不存在的 Skill 源码包: {missing}")

    for skill in skills:
        if not isinstance(skill, dict) or skill.get("id") not in ids:
            continue
        for dep in skill.get("depends_on") or []:
            if dep not in ids:
                errors.append(f"{skill['id']} 依赖不存在的 Skill: {dep}")
            if dep == skill["id"]:
                errors.append(f"{skill['id']} 不得依赖自身")
    errors.extend(_cycle_errors([skill for skill in skills if isinstance(skill, dict) and skill.get("id") in ids]))

    try:
        configured_profiles = _config_profiles(root)
        actual_profiles = set(mapped_profiles)
        if actual_profiles != configured_profiles:
            missing = sorted(configured_profiles - actual_profiles)
            extra = sorted(actual_profiles - configured_profiles)
            if missing:
                errors.append("未绑定 profile: " + ", ".join(missing))
            if extra:
                errors.append("manifest 含未知 profile: " + ", ".join(extra))
        for skill in skills:
            default_profile = skill.get("default_profile") if isinstance(skill, dict) else None
            if default_profile and default_profile not in configured_profiles:
                errors.append(f"{skill.get('id')}.default_profile 不在 models.yaml: {default_profile}")
    except (OSError, AgentAssetError) as exc:
        errors.append(str(exc))

    if errors:
        raise AgentAssetError("Agent 资产校验失败:\n- " + "\n- ".join(errors))


def _descriptor(root: Path, manifest: dict[str, Any], skill: dict[str, Any]) -> dict[str, Any]:
    digest = _skill_digest(root, skill)
    result = dict(skill)
    result.update({
        "cmd": f"/{skill['id']}",
        "entrypoint": f".claude/skills/{skill['id']}/SKILL.md",
        "digest": digest,
        "source_revision": digest,
        "owner": manifest["defaults"]["owner"],
        "license": manifest["defaults"]["license"],
        "trust": manifest["defaults"]["trust"],
    })
    return result


# INDEX 分组标题与展示序（键集必须恒等于 KINDS——_render_index 开头强制）。
# 这里只放标题这一份展示信息；合法 kind 集合的真源是 KINDS，argparse choices
# 与 manifest 校验都引用它，新增 kind 漏配标题在编译期就红，不会静默掉组。
_INDEX_GROUP_TITLES = {
    "workflow": "通用工作流",
    "route": "画风路由",
    "overlay": "生产策略覆盖",
    "capability": "专项能力",
    "project": "项目与长篇",
    "system": "系统能力",
    "scaffold": "规划骨架",
}


def _render_index(catalog: dict[str, Any]) -> str:
    if set(_INDEX_GROUP_TITLES) != KINDS:
        missing = KINDS - set(_INDEX_GROUP_TITLES)
        extra = set(_INDEX_GROUP_TITLES) - KINDS
        raise AgentAssetError(
            f"INDEX 分组标题与 KINDS 不一致（缺 {sorted(missing)} / 多 {sorted(extra)}）"
            "——该类 skill 会在 INDEX.md 静默消失，先补 _INDEX_GROUP_TITLES")
    groups = list(_INDEX_GROUP_TITLES.items())
    rows = [
        "<!-- 由 tools/agent_assets.py 生成；请勿手改。 -->",
        "",
        "# Kinema Skill Catalog",
        "",
        "本索引由 `agent/manifest.json`、`agent/contracts.json` 与 `.claude/skills/` 正文确定性生成。",
        "宿主直接发现 `.claude/skills/`；Codex 等兼容 `.agents/skills` 的工具读取同一目录别名。",
        "",
        "选择优先级：项目绑定 > 显式 Skill > 显式 profile > `kinema`。",
        "",
    ]
    for kind, title in groups:
        items = [item for item in catalog["skills"] if item["kind"] == kind]
        if not items:
            continue
        rows.extend([f"## {title}", "", "| Skill | 状态 | 用途 | Profile |", "|---|---|---|---|"])
        for item in items:
            link = f"[`{item['cmd']}`](../../{item['entrypoint']})"
            usage = (item.get("catalog") or {}).get("usage") or item["description"]
            profiles = ", ".join(f"`{name}`" for name in item["profiles"]) or "—"
            rows.append(f"| {link} | `{item['status']}` | {usage} | {profiles} |")
        rows.append("")
    rows.extend([
        "## 维护",
        "",
        "```bash",
        "python3 tools/agent_assets.py compile",
        "python3 tools/agent_assets.py check",
        "cd engine && python3 -m kinema agent doctor --json",
        "```",
        "",
        "Skill 正文与 references 直接在 `.claude/skills/` 修改；名称、描述、权限等元数据只改",
        "`agent/manifest.json`。frontmatter、`skill.json`、本索引、运行时 catalog 与 contracts",
        "由编译器维护，改完必须重新 compile。",
        "",
    ])
    return "\n".join(rows)


def _render(root: Path, manifest: dict[str, Any], staging: Path) -> dict[str, Any]:
    outputs = manifest["outputs"]
    contracts = _read_contracts(root)
    validate_contracts(contracts)
    descriptors = [_descriptor(root, manifest, skill) for skill in manifest["skills"]]
    catalog = {
        "schema_version": 2,
        "catalog_version": manifest["catalog_version"],
        "manifest_digest": _sha256(_canonical_bytes(manifest)),
        "generated_by": "tools/agent_assets.py",
        "skills": descriptors,
    }

    catalog_path = staging / outputs["catalog"]
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_bytes(_json_bytes(catalog))
    contracts_path = staging / outputs["contracts"]
    contracts_path.parent.mkdir(parents=True, exist_ok=True)
    contracts_path.write_bytes(_json_bytes(contracts))
    index_path = staging / outputs["index"]
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(_render_index(catalog), encoding="utf-8", newline="\n")
    for source, target in outputs["adapters"].items():
        target_path = staging / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        content = (root / source).read_text(encoding="utf-8").replace("\r\n", "\n").rstrip() + "\n"
        target_path.write_text(content, encoding="utf-8", newline="\n")
    return catalog


def _replace_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = target.with_name(f".{target.name}.next")
    shutil.copy2(source, candidate)
    os.replace(candidate, target)


def compile_assets(explicit_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """校验并编译全部 Agent 资产，返回机器可读摘要。"""
    root = repository_root(explicit_root)
    manifest = _read_manifest(root)
    contracts = _read_contracts(root)
    validate_contracts(contracts)
    reference_path = root / CONTRACT_REFERENCE
    rendered_reference = _render_contract_reference(contracts)
    if not reference_path.is_file() or reference_path.read_text(encoding="utf-8") != rendered_reference:
        reference_path.parent.mkdir(parents=True, exist_ok=True)
        reference_path.write_text(rendered_reference, encoding="utf-8", newline="\n")
    validate_sources(root, manifest)

    changed_sources: list[str] = []
    for skill in manifest["skills"]:
        package = root / skill["source"]
        path = package / "SKILL.md"
        expected = _expected_skill_text(root, manifest, skill, _skill_digest(root, skill))
        if path.read_text(encoding="utf-8") != expected:
            path.write_text(expected, encoding="utf-8", newline="\n")
            changed_sources.append(path.relative_to(root).as_posix())
        meta_path = package / "skill.json"
        expected_meta = _json_bytes(_descriptor(root, manifest, skill))
        if not meta_path.is_file() or meta_path.read_bytes() != expected_meta:
            meta_path.write_bytes(expected_meta)
            changed_sources.append(meta_path.relative_to(root).as_posix())

    with tempfile.TemporaryDirectory(prefix="kinema-agent-assets-") as tmp:
        staging = Path(tmp)
        catalog = _render(root, manifest, staging)
        outputs = manifest["outputs"]
        _replace_file(staging / outputs["catalog"], root / outputs["catalog"])
        _replace_file(staging / outputs["contracts"], root / outputs["contracts"])
        _replace_file(staging / outputs["index"], root / outputs["index"])
        for _source, target in outputs["adapters"].items():
            _replace_file(staging / target, root / target)

    return {
        "ok": True,
        "catalog_version": catalog["catalog_version"],
        "manifest_digest": catalog["manifest_digest"],
        "skills": len(catalog["skills"]),
        "source_headers_updated": changed_sources,
    }


def alias_error(root: Path) -> str | None:
    """`.agents/skills` 别名不变量：必须存在且解析到 `.claude/skills` 同一实体。

    check 与 doctor 共用本判据——「不得出现第二份 Skill 实体」是编译产物级
    不变量，只在 doctor 报的话，日常只跑 check 的工作流对别名破坏不设防。"""
    alias = root / ".agents" / "skills"
    target = root / ".claude" / "skills"
    if alias.exists() and alias.resolve() == target.resolve():
        return None
    return ".agents/skills 未指向 .claude/skills（Skill 实体只许一份，别名坏了跑 tools/agents_alias.py）"


def check_assets(explicit_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """只读检查源码与全部生成物，绝不修改工作区。"""
    root = repository_root(explicit_root)
    manifest = _read_manifest(root)
    contracts = _read_contracts(root)
    validate_contracts(contracts)
    expected_reference = _render_contract_reference(contracts)
    validate_sources(root, manifest)
    errors: list[str] = []
    alias_problem = alias_error(root)
    if alias_problem:
        errors.append(alias_problem)
    reference_path = root / CONTRACT_REFERENCE
    if not reference_path.is_file():
        errors.append(f"{CONTRACT_REFERENCE.as_posix()} 不存在")
    elif reference_path.read_text(encoding="utf-8") != expected_reference:
        errors.append(f"{CONTRACT_REFERENCE.as_posix()} 内容漂移")
    for skill in manifest["skills"]:
        package = root / skill["source"]
        path = package / "SKILL.md"
        expected = _expected_skill_text(root, manifest, skill, _skill_digest(root, skill))
        if path.read_text(encoding="utf-8") != expected:
            errors.append(f"SKILL.md frontmatter/digest 漂移（改后未重新 compile）: "
                          f"{path.relative_to(root)}")
        meta_path = package / "skill.json"
        expected_meta = _json_bytes(_descriptor(root, manifest, skill))
        if not meta_path.is_file():
            errors.append(f"{meta_path.relative_to(root)} 不存在")
        elif meta_path.read_bytes() != expected_meta:
            errors.append(f"skill.json 漂移（改后未重新 compile）: {meta_path.relative_to(root)}")

    with tempfile.TemporaryDirectory(prefix="kinema-agent-check-") as tmp:
        staging = Path(tmp)
        catalog = _render(root, manifest, staging)
        outputs = manifest["outputs"]
        for key in ("catalog", "contracts", "index"):
            expected = staging / outputs[key]
            actual = root / outputs[key]
            if not actual.is_file():
                errors.append(f"{outputs[key]} 不存在")
            elif expected.read_bytes() != actual.read_bytes():
                errors.append(f"{outputs[key]} 内容漂移")
        for _source, target in outputs["adapters"].items():
            expected = staging / target
            actual = root / target
            if not actual.is_file():
                errors.append(f"{target} 不存在")
            elif expected.read_bytes() != actual.read_bytes():
                errors.append(f"{target} 内容漂移")

    kernel = root / "AGENTS.md"
    kernel_bytes = len(kernel.read_bytes()) if kernel.is_file() else 0
    if not kernel.is_file():
        errors.append("AGENTS.md 不存在")
    elif kernel_bytes > manifest["budgets"]["agent_kernel_bytes"]:
        errors.append(
            f"AGENTS.md 超过预算: {kernel_bytes} > {manifest['budgets']['agent_kernel_bytes']} bytes")
    return {
        "ok": not errors,
        "catalog_version": catalog["catalog_version"],
        "manifest_digest": catalog["manifest_digest"],
        "skills": len(catalog["skills"]),
        "agent_kernel_bytes": kernel_bytes,
        "errors": errors,
    }
