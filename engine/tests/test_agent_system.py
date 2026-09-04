# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Agent/Skill 控制平面守卫。"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import kinema
from kinema.agent_assets import (AgentAssetError, check_assets, expand_contract_refs,
                                 validate_sources)
from kinema.agent_system import AgentCatalog, AgentCatalogError


class TestAgentControlPlane(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(kinema.__file__).parent.parent.parent
        cls.manifest = json.loads(
            (cls.root / "agent" / "manifest.json").read_text(encoding="utf-8"))
        cls.catalog = AgentCatalog.load()

    def test_manifest_schema_and_all_packages_are_single_sourced(self):
        """单源布局：Skill 正文实体只有 `.claude/skills/` 一份，且与 manifest 登记
        双向对齐——发现目录混入未登记包、或登记了没有实体的 skill 都是漂移。
        （`agent/` 下不得再出现第二棵 skills 源码树——那正是被收敛掉的重复。）"""
        schema = json.loads(
            (self.root / "agent" / "manifest.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 2)
        declared = [item["id"] for item in self.manifest["skills"]]
        packages = sorted(
            path.name for path in (self.root / ".claude" / "skills").iterdir()
            if path.is_dir() and not path.name.startswith("."))
        self.assertEqual(sorted(declared), packages)
        self.assertFalse((self.root / "agent" / "skills").exists(),
                         "agent/skills 复活了——单源布局下正文只在 .claude/skills/")

    def test_generated_assets_are_clean_and_within_context_budget(self):
        report = check_assets(self.root)
        self.assertTrue(report["ok"], report["errors"])
        self.assertLessEqual(
            report["agent_kernel_bytes"], self.manifest["budgets"]["agent_kernel_bytes"])

    def test_runtime_contract_is_compiled_from_the_agent_source(self):
        source = json.loads(
            (self.root / "agent" / "contracts.json").read_text(encoding="utf-8"))
        generated = json.loads(
            (self.root / self.manifest["outputs"]["contracts"]).read_text(encoding="utf-8"))
        self.assertEqual(expand_contract_refs(source), generated)

    def test_line_members_share_the_shot_specs_through_refs(self):
        """句级表现力字段在源里只是指向镜级同名规格的 $ref：生成物平铺后两处相等，
        运行时契约因此能列出 lines[]/beats[] 的合法成员，不再只有一个类型名。"""
        source = json.loads(
            (self.root / "agent" / "contracts.json").read_text(encoding="utf-8"))
        shot_fields = source["chapter_plan"]["shot_fields"]
        self.assertEqual(shot_fields["lines"]["items"]["properties"]["delivery"],
                         {"$ref": "#/chapter_plan/shot_fields/delivery"})
        expanded = expand_contract_refs(source)["chapter_plan"]["shot_fields"]
        self.assertEqual(expanded["lines"]["items"]["properties"]["delivery"],
                         shot_fields["delivery"])
        self.assertEqual(expanded["lines"]["items"]["required"], ["text"])
        self.assertEqual(expanded["sketch"]["properties"]["beats"]["items"]["required"],
                         ["action"])
        with self.assertRaisesRegex(AgentAssetError, "不存在的节点"):
            expand_contract_refs({"a": {"$ref": "#/missing"}})
        with self.assertRaisesRegex(AgentAssetError, "只支持本文件指针"):
            expand_contract_refs({"a": {"$ref": "other.json#/x"}})

    def test_runtime_catalog_contains_provenance_for_every_skill(self):
        """登记即入 catalog，一条不落——状态枚举里没有「登记了但不下发」的隔离档：
        单源布局下包就在发现目录里，宿主一定能加载，任何『目录里关掉』的状态都在
        撒谎（quarantine 因此被整个删除而不是留着不实现）。"""
        manifest_ids = [item["id"] for item in self.manifest["skills"]]
        catalog = self.catalog.all()
        self.assertEqual([item["id"] for item in catalog], manifest_ids)
        for item in catalog:
            self.assertEqual(item["trust"], "first-party")
            self.assertEqual(item["owner"], "Kinema")
            self.assertRegex(item["digest"], r"^sha256:[0-9a-f]{64}$")
            package_meta = json.loads(
                (self.root / ".claude" / "skills" / item["id"] / "skill.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(package_meta["digest"], item["digest"])

    def test_route_precedence_is_explicit_and_deterministic(self):
        routed = self.catalog.route(
            project_skill="kn-showcase", skill="kn-game", profile="anime")
        self.assertEqual(routed.skill, "kn-showcase")
        self.assertEqual(routed.source, "project.skill")
        routed = self.catalog.route(skill="kn-game", profile="anime")
        self.assertEqual(routed.skill, "kn-game")
        self.assertEqual(routed.source, "explicit.skill")
        routed = self.catalog.route(profile="anime")
        self.assertEqual(routed.skill, "kn-anime")
        self.assertEqual(routed.source, "explicit.profile")
        routed = self.catalog.route()
        self.assertEqual(routed.skill, "kinema")
        self.assertEqual(routed.source, "default")

    def test_unknown_explicit_route_values_fail_closed(self):
        with self.assertRaises(AgentCatalogError):
            self.catalog.route(skill="not-a-skill")
        with self.assertRaises(AgentCatalogError):
            self.catalog.route(profile="not_a_profile")

    def test_retired_binding_fails_closed_but_names_the_way_out(self):
        """落盘绑定指向已退役 Skill/画风时，硬失败照旧、但必须给出换绑路径。

        Skill 下线是常规动作，存量项目的 `skill`/`profile` 却是历史落盘事实：
        没有这条指引，`agent route`/`agent context`（Agent 改章节的唯一正式入口）
        对那些项目就是一句「未知 Skill」的死路——而修法（`project set --skill`）
        既不在报错里、也不在报错指向的任何地方。**不能改成静默兜底**：绑定降级
        会让项目在另一套画风 DNA 下继续产出。"""
        with self.assertRaises(AgentCatalogError) as ctx:
            self.catalog.route(project_skill="kn-retired")
        msg = str(ctx.exception)
        self.assertIn("project set", msg)
        self.assertIn("--skill", msg)
        with self.assertRaises(AgentCatalogError) as ctx2:
            self.catalog.bound_profile("retired_profile")
        self.assertIn("project set", str(ctx2.exception))
        # 本次显式输入不共用这套措辞：那是"你刚敲错了"，不是"存量绑定要迁移"
        with self.assertRaises(AgentCatalogError) as ctx3:
            self.catalog.route(skill="not-a-skill")
        self.assertNotIn("project set", str(ctx3.exception))

    def test_persisted_bindings_resolve_through_bound_helpers(self):
        """读**落盘**绑定的四处入口必须走 `bound_*`——漏一处就是那条入口继续
        抛无指引的裸错（route、章节上下文、explain 陈旧判定、生图/生视频的
        Envelope 封装是四条独立通道，任何一条都能把项目卡死）。"""
        import inspect

        from kinema import agent_gateway
        src = inspect.getsource(agent_gateway)
        self.assertEqual(src.count("self.catalog.bound_skill("), 2,
                         "章节上下文与 explain 陈旧判定各一处，缺一处即分叉")
        self.assertEqual(src.count("self.catalog.bound_profile("), 2)
        route_src = inspect.getsource(type(self.catalog).route)
        self.assertIn("self.bound_skill(project_skill", route_src)
        from kinema import cli
        rev_src = inspect.getsource(cli._prompt_revisions)
        self.assertIn("catalog.bound_skill(", rev_src)
        self.assertIn("catalog.bound_profile(", rev_src)

    def test_cli_exposes_machine_readable_route(self):
        from kinema.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["agent", "route", "--profile", "anime", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["skill"], "kn-anime")
        self.assertEqual(payload["source"], "explicit.profile")

    def test_doctor_checks_the_runtime_contract_bundle(self):
        from kinema.agent_system import agent_doctor
        result = agent_doctor(self.root)
        findings = {item["id"]: item for item in result["findings"]}
        self.assertTrue(findings["runtime_contracts"]["ok"], findings["runtime_contracts"])
        self.assertIn("prompt/v1", findings["runtime_contracts"]["detail"])


class TestAgentManifestSemanticValidation(unittest.TestCase):
    def _skill(self, skill_id, *, kind, profiles=None, depends_on=None, catalog=None):
        return {
            "id": skill_id,
            "kind": kind,
            "status": "stable",
            "source": f".claude/skills/{skill_id}",
            "description": f"{skill_id} description",
            "profiles": profiles or [],
            "default_profile": (profiles or [None])[0],
            "depends_on": depends_on or [],
            "activation": ["explicit"],
            "voiceover": "lead" if kind in {"route", "workflow"} else None,
            "permissions": ["workspace.read"],
            "catalog": catalog,
        }

    def test_dependency_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude" / "skills" / "a").mkdir(parents=True)
            (root / ".claude" / "skills" / "b").mkdir(parents=True)
            for name in ("a", "b"):
                (root / ".claude" / "skills" / name / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test\n---\n\n# {name}\n", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "models.yaml").write_text(
                "profiles:\n  p:\n    label: P\nproviders:\n", encoding="utf-8")
            (root / "agent" / "adapters").mkdir(parents=True)
            (root / "agent" / "adapters" / "host.md").write_text("host\n", encoding="utf-8")
            manifest = {
                "schema_version": 2,
                "catalog_version": "2.0.0",
                "defaults": {
                    "owner": "Kinema", "license": "AGPL-3.0-or-later",
                    "source": "workspace", "trust": "first-party",
                },
                "budgets": {
                    "agent_kernel_bytes": 24576, "skill_lines": 500,
                    "description_chars": 420,
                },
                "outputs": {
                    "skills": ".claude/skills",
                    "catalog": "engine/kinema/_generated/agent_catalog.json",
                    "contracts": "engine/kinema/_generated/agent_contracts.json",
                    "index": "docs/skills/INDEX.md",
                    "adapters": {"agent/adapters/host.md": "HOST.md"},
                },
                "skills": [
                    self._skill("a", kind="route", profiles=["p"], depends_on=["b"],
                                catalog={"label": "A", "en": "A", "usage": "A"}),
                    self._skill("b", kind="capability", depends_on=["a"]),
                ],
            }
            with self.assertRaisesRegex(AgentAssetError, "依赖循环"):
                validate_sources(root, manifest)
