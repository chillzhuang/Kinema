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

"""kinema.workspace 单元测试：title slug、章节 id 生成、create_chapter 继承拷贝。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from kinema.errors import ProjectError
from tests.support import LocalBackendEnv


class TestWorkspaceDiscovery(unittest.TestCase):
    """源码仓库中的所有入口都必须落到同一个根 project/ 数据目录。"""

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def test_default_from_engine_uses_repository_project(self):
        """即使 engine/project 存在，默认发现也不能被近处目录劫持。"""
        from kinema.workspace import find_workspace

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "checkout"
            (repo / "engine" / "project").mkdir(parents=True)
            (repo / "project").mkdir()
            with patch.dict(os.environ, {"KINEMA_WORKSPACE": ""}), \
                    patch("kinema.workspace._source_checkout_root", return_value=repo), \
                    patch("kinema.workspace.Path.cwd", return_value=repo / "engine"):
                self.assertEqual(find_workspace().resolve(), (repo / "project").resolve())

    def test_repository_and_engine_explicit_paths_normalize_to_same_root(self):
        from kinema.workspace import find_workspace

        repo = self._repo_root()
        expected = (repo / "project").resolve()
        self.assertEqual(find_workspace(str(repo)).resolve(), expected)
        self.assertEqual(find_workspace(str(repo / "engine")).resolve(), expected)
        self.assertEqual(find_workspace(str(repo / "engine" / "project")).resolve(), expected)
        self.assertEqual(find_workspace(str(repo / "project")).resolve(), expected)

    def test_legacy_engine_project_in_another_checkout_is_normalized(self):
        """显式管理另一份源码检出时，历史 engine/project 同样不能继续分叉。"""
        from kinema.workspace import find_workspace

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "other-checkout"
            (repo / "engine" / "kinema").mkdir(parents=True)
            (repo / "engine" / "project").mkdir()
            (repo / "config").mkdir()
            (repo / "config" / "storage.yaml").write_text("backend: local\n",
                                                               encoding="utf-8")
            self.assertEqual(
                find_workspace(str(repo / "engine" / "project")).resolve(),
                (repo / "project").resolve())

    def test_custom_workspace_path_is_not_rewritten(self):
        from kinema.workspace import find_workspace

        with tempfile.TemporaryDirectory() as d:
            custom = Path(d) / "isolated-workspace"
            self.assertEqual(find_workspace(str(custom)), custom)

    def test_local_and_mysql_backends_share_the_normalized_root(self):
        from kinema.storage import get_storage, load_storage_config
        from kinema.workspace import find_workspace

        env = LocalBackendEnv()
        env.enable()
        try:
            repo = self._repo_root()
            root = find_workspace(str(repo)).resolve()
            local = get_storage(root)
            self.assertEqual(local.root, root)

            os.environ["KINEMA_STORAGE_BACKEND"] = "mysql"
            load_storage_config(reload=True)
            mysql = get_storage(root)
            self.assertEqual(mysql.root, root)
            self.assertEqual(local.root, mysql.root)
            self.assertEqual(local.backend, "local")
            self.assertEqual(mysql.backend, "mysql")
        finally:
            env.restore()


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        # 每用例独立 root，规避 get_storage 的 (root, backend) 实例缓存
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()


class TestSlugAndProjectId(WorkspaceCase):
    def test_english_title_slug(self):
        s = self.ws.create_project("Hello, World!")
        self.assertEqual(s.pid, "hello-world")
        self.assertTrue((s.dir / "chapters").is_dir())     # 目录骨架落位

    def test_chinese_title_falls_back_to_project(self):
        s = self.ws.create_project("你好世界")
        self.assertEqual(s.pid, "project")                 # 中文剥净 → 回退 "project"

    def test_conflict_appends_numeric_suffix(self):
        self.ws.create_project("你好世界")
        s2 = self.ws.create_project("再见世界")             # 同样回退 → 冲突 -2 后缀
        self.assertEqual(s2.pid, "project-2")
        s3 = self.ws.create_project("第三个")
        self.assertEqual(s3.pid, "project-3")

    def test_explicit_pid_conflict_raises(self):
        self.ws.create_project("Demo", pid="demo")
        with self.assertRaises(ProjectError):
            self.ws.create_project("Demo Again", pid="demo")


class TestSkillBinding(WorkspaceCase):
    """建项目落 skill：缺省由画风确定性派生，显式指定原样采纳（含共享画风的 skill）。"""

    def test_skill_derived_from_profile(self):
        self.assertEqual(self.ws.create_project("动漫片", profile="shinkai",
                                                pid="a").data.get("skill"), "kn-anime")
        self.assertEqual(self.ws.create_project("赛博片", profile="cyberpunk",
                                                pid="b").data.get("skill"), "kn-cyberpunk")
        self.assertEqual(self.ws.create_project("口播", pid="c").data.get("skill"),
                         "kinema")   # 缺省 profile=narration

    def test_explicit_skill_override_honored(self):
        # kn-showcase 与 explainer 共享画风，不在 profile→skill 派生表内，须显式采纳
        s = self.ws.create_project("解说复用", profile="explainer",
                                   skill="kn-showcase", pid="d")
        self.assertEqual(s.data.get("skill"), "kn-showcase")

    def test_blank_skill_falls_back_to_derive(self):
        s = self.ws.create_project("空 skill", profile="hd2d", skill="  ", pid="e")
        self.assertEqual(s.data.get("skill"), "kn-game")

    def test_project_set_validates_before_it_persists(self):
        """`project set --skill/--profile` 与建项目走同一道闸。

        这两个值一旦落盘就是**绑定事实**：未登记的值在写入现场毫无异常，等到
        `agent route`/`agent context` 才炸（那时报错点已离开输入现场），而
        Skill 退役后的换绑指引恰恰把用户导向这条命令——它自己不校验，
        一个笔误就重造一份死绑定。"""
        import contextlib
        import io

        from kinema.cli import main
        self.ws.create_project("绑定校验", profile="anime", pid="bind")
        args = ["project", "set", "bind", "--workspace", str(self.ws.root)]
        for bad in (["--skill", "kn-not-a-skill"], ["--profile", "not_a_profile"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = main(args + bad)
            self.assertEqual(rc, 1, f"{bad} 应当当场失败而不是落盘")
            data = self.ws.get_project("bind").data
            self.assertEqual(data.get("skill"), "kn-anime")
            self.assertEqual(data.get("profile"), "anime")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main(args + ["--skill", "kn-game", "--profile", "hd2d"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(self.ws.get_project("bind").data.get("skill"), "kn-game")

    def test_chapter_set_rebinds_the_creation_time_copy(self):
        """`chapter set` 是章节绑定的唯一受支持改法（建章拷贝不随 `project set` 回灌）。

        校验与建项目同闸；`--inherit` 删两键回落项目派生。**只开这两个字段**——
        章节其余作者字段的入口是 ChapterPlan，CLI 再开一份就是两条写路径各改一半。"""
        import contextlib
        import io
        import json as _json

        from kinema.cli import main
        s = self.ws.create_project("章节绑定", profile="anime", pid="cs")
        cf = s.create_chapter("第一章")
        args = ["chapter", "set", "cs", "ch01", "--workspace", str(self.ws.root)]

        def _run(extra):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = main(args + extra)
            return rc, buf.getvalue()

        def _doc():
            return _json.loads(Path(cf).read_text(encoding="utf-8"))

        self.assertEqual(_doc().get("profile"), "anime")     # 建章即拷贝
        rc, _ = _run(["--skill", "kn-not-a-skill"])
        self.assertEqual(rc, 1)
        self.assertEqual(_doc().get("skill"), "kn-anime", "未登记值不许落盘")
        rc, out = _run([])
        self.assertEqual(rc, 1, "一个字段都没给要明确报错，不是静默成功")
        rc, out = _run(["--skill", "kn-explainer", "--profile", "explainer"])
        self.assertEqual(rc, 0, out)
        self.assertEqual((_doc().get("skill"), _doc().get("profile")),
                         ("kn-explainer", "explainer"))
        rc, out = _run(["--video-provider", "seedance-2.5"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(_doc().get("video_provider"), "seedance-2.5")
        rc, out = _run(["--inherit"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("skill", _doc())
        self.assertNotIn("profile", _doc())
        self.assertNotIn("video_provider", _doc(), "--inherit 连本章自持的视频档一起删")
        self.assertIn("chapter", _doc(), "回落只删 skill/profile/video_provider 三键，不许动其余文档")

    def test_chapter_title_is_editable_and_numbering_is_called_out(self):
        """标题存两处（章节文档 + 系列登记表），`chapter set --title` 同批改；
        建章与改名时带序号的标题只提醒不拦（序号归 id/order 与封面排版）。"""
        import contextlib
        import io
        import json as _json

        from kinema.cli import main
        self.ws.create_project("标题", profile="anime", pid="ct")
        ws_args = ["--workspace", str(self.ws.root)]

        def _run(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = main(argv + ws_args)
            return rc, buf.getvalue()

        rc, out = _run(["chapter", "new", "ct", "--title", "第一章：归途"])
        self.assertEqual(rc, 0, out)
        self.assertIn("含序号「第一章」", out)
        rc, out = _run(["chapter", "set", "ct", "ch01", "--title", "归途"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("含序号", out)
        cf = self.ws.get_project("ct").get_chapter_path("ch01")
        doc = _json.loads(Path(cf).read_text(encoding="utf-8"))
        self.assertEqual(doc["chapter"]["title"], "归途")
        reg = [c for c in self.ws.get_project("ct").chapters if c["id"] == "ch01"]
        self.assertEqual(reg[0]["title"], "归途")
        rc, out = _run(["chapter", "set", "ct", "ch01", "--title", "  "])
        self.assertEqual(rc, 1, "空标题等于没给字段")


class TestChapters(WorkspaceCase):
    def test_delete_chapter_removes_its_lock_files(self):
        s = self.ws.create_project("锁", pid="lk", profile="anime")
        s.create_chapter("一", cid="ch01")
        cf = self.ws.store.chapter_path("lk", "ch01")
        with s.chapter_write("ch01"):
            pass
        self.assertTrue(cf.with_name(cf.name + ".oplock").is_file())
        s.delete_chapter("ch01")
        self.assertFalse(cf.is_file())
        self.assertFalse(cf.with_name(cf.name + ".oplock").is_file())
        self.assertFalse(cf.with_name(cf.name + ".lock").is_file())

    def test_chapter_id_sequence(self):
        s = self.ws.create_project("Series Demo")
        cf1 = s.create_chapter("第一章")
        cf2 = s.create_chapter("第二章")
        self.assertEqual(cf1.name, "ch01.json")            # ch01…按序生成
        self.assertEqual(cf2.name, "ch02.json")
        ids = [c["id"] for c in s.list_chapters()]
        self.assertEqual(ids, ["ch01", "ch02"])

    def test_chapter_id_skips_deleted_numbers(self):
        s = self.ws.create_project("Series Demo")
        for title in ("一", "二", "三"):
            s.create_chapter(title)
        s.delete_chapter("ch02")
        self.assertEqual(s.create_chapter("四").name, "ch04.json")

    def test_remove_entity_drops_chapter_copies(self):
        s = self.ws.create_project("删除", pid="rm", profile="anime")
        s.add_character("甲", appearance="黑发")
        s.add_character("乙", appearance="白发")
        s.add_prop("剑")
        s.add_scene("桥")
        s.create_chapter("一", cid="ch01")
        s.remove_character("甲")
        s.remove_prop("剑")
        s.remove_scene("桥")
        doc = json.loads(self.ws.store.chapter_path("rm", "ch01").read_text(encoding="utf-8"))
        self.assertEqual([c["name"] for c in doc["characters"]], ["乙"])
        self.assertEqual(doc["props"], [])
        self.assertEqual(doc["scenes"], [])
        self.assertNotIn("甲", doc["style"]["character_block"])
        self.assertEqual(s.list_chapters()[0]["status"], "draft")   # 无 shots → 草稿

    def test_duplicate_chapter_id_raises(self):
        s = self.ws.create_project("Series Demo")
        s.create_chapter("第一章")
        with self.assertRaises(ProjectError):
            s.create_chapter("重复章", cid="ch01")

    def test_duplicate_character_raises(self):
        s = self.ws.create_project("Series Demo")
        s.add_character("阿黎")
        with self.assertRaises(ProjectError):
            s.add_character("阿黎")

    def test_create_chapter_inherits_art_direction(self):
        """风格圣经旋钮与 style_prompt 同待遇：建章时拷贝一份。

        lint 只读**章节文档**顶层——不继承的话，系列级 project.json 里写的旋钮
        对新建章节静默失效，每章都回落中位 5，告警松紧与作者设定不符且无提示。
        深拷贝：改章节的 avoid 不得回写污染系列文档。"""
        s = self.ws.create_project("Series Demo")
        s.data["art_direction"] = {"variety": 9, "motion": 8, "density": 4,
                                   "avoid": ["水墨"]}
        s.save()
        video = json.loads(s.create_chapter("第一章").read_text(encoding="utf-8"))
        self.assertEqual(video["art_direction"]["variety"], 9)
        self.assertEqual(video["art_direction"]["avoid"], ["水墨"])
        video["art_direction"]["avoid"].append("篡改")
        self.assertEqual(s.data["art_direction"]["avoid"], ["水墨"])   # 深拷贝隔离

    def test_create_chapter_without_art_direction_omits_key(self):
        """没写旋钮就不要凭空往章节文档塞空块（缺省由 resolve_art_direction 回落）。"""
        s = self.ws.create_project("Series Demo")
        video = json.loads(s.create_chapter("第一章").read_text(encoding="utf-8"))
        self.assertNotIn("art_direction", video)

    def test_create_chapter_inherits_project_assets(self):
        s = self.ws.create_project("Series Demo", profile="anime", aspect="16:9")
        s.add_character("阿黎", voice="voice_a", appearance="红衣少女")
        s.set_design(palette="暖橙色调", tone="治愈")
        s.set_scene("江南小镇")
        cf = s.create_chapter("第一章")

        video = json.loads(cf.read_text(encoding="utf-8"))
        self.assertEqual(video["id"], f"{s.pid}_ch01")
        self.assertEqual(video["profile"], "anime")        # profile 继承
        # 绑定 skill 一并继承：voiceover_default(profile, skill) 的 skill 位靠它
        # 供料——不拷贝的话共享画风的 skill（kn-showcase）语态 override 永远打不中
        self.assertEqual(video.get("skill"), s.data.get("skill"))
        self.assertEqual(video["aspect"], "16:9")
        self.assertEqual(video["voices"], {"阿黎": "voice_a"})   # 角色音色表
        self.assertEqual(video["characters"][0]["name"], "阿黎")  # 角色设定拷贝
        self.assertEqual(video["scene"], "江南小镇")
        self.assertEqual(video["chapter"],
                         {"project": s.pid, "id": "ch01", "title": "第一章"})
        # style.seed 由 "<pid>/<cid>" 的 md5 前 6 位十六进制确定（跨镜一致的确定性种子）
        expect_seed = int(hashlib.md5(f"{s.pid}/ch01".encode()).hexdigest()[:6], 16)
        self.assertEqual(video["style"]["seed"], expect_seed)
        self.assertIn("阿黎——红衣少女", video["style"]["character_block"])
        self.assertEqual(video["style"]["palette"], "暖橙色调")

        # 章节里的角色是深拷贝：改章节文档不影响项目文档
        chdata = self.ws.store.load_chapter(s.pid, "ch01")
        chdata["characters"][0]["name"] = "改名"
        self.assertEqual(s.characters[0]["name"], "阿黎")

    def test_prop_keywords_sync_to_chapters(self):
        """道具 keywords 必须同步进已建章节文档——design_refs 读章节文档，
        否则对存量章节静默失效（同类）。"""
        s = self.ws.create_project("KW Demo", profile="anime")
        cf = s.create_chapter("第一章")            # 先建章节（此时系列还没这个道具）
        s.add_prop("水杯魔王", desc="玻璃杯", keywords=["水杯", "玻璃杯"])
        s.sync_design_to_chapters()
        chdata = self.ws.store.load_chapter(s.pid, "ch01")
        prop = next(p for p in chdata["props"] if p["name"] == "水杯魔王")
        self.assertEqual(prop.get("keywords"), ["水杯", "玻璃杯"])

    def test_default_aspect_is_landscape(self):
        # 全局默认横屏 16:9（用户点名才走竖屏/方形）
        s = self.ws.create_project("默认比例")
        self.assertEqual(s.data["aspect"], "16:9")
        cf = s.create_chapter("第一章")
        video = json.loads(cf.read_text(encoding="utf-8"))
        self.assertEqual(video["aspect"], "16:9")

    def test_exports_dir_is_shallow_project_level(self):
        # 导出专用目录：标准工作区 = project/<项目>/exports/（浅层好找，审阅包/交付包统一落位）
        from kinema.project import Project
        s = self.ws.create_project("导出落位", profile="anime")
        cf = s.create_chapter("第一章")
        proj = Project.load(cf)
        self.assertEqual(proj.exports_dir.resolve(),
                         (s.dir / "exports").resolve())   # macOS /var↔/private/var 符号链
        self.assertTrue(proj.exports_dir.is_dir())

    def test_chapter_inherits_style_prompt(self):
        # 画风单点字段：立项快照落项目顶层，chapter new 时拷贝继承
        s = self.ws.create_project("画风继承", profile="anime")
        s.data["style_prompt"] = "赛璐璐动画风格，"
        s.data["style_prompt_en"] = "cel anime style, "
        s.save()
        cf = s.create_chapter("第一章")
        video = json.loads(cf.read_text(encoding="utf-8"))
        self.assertEqual(video["style_prompt"], "赛璐璐动画风格，")
        self.assertEqual(video["style_prompt_en"], "cel anime style, ")


class TestSoftDelete(WorkspaceCase):
    """逻辑删除（唯一删除语义）：清单/聚合全过滤、写路径总闸、可恢复、id 不复活。"""

    def _mk(self, title, pid):
        s = self.ws.create_project(title, pid=pid)
        return s

    def test_delete_sets_flags_and_keeps_files(self):
        s = self._mk("Alpha", "alpha")
        self.ws.delete_project("alpha")
        data = self.ws.store.load_project("alpha")
        self.assertEqual(data["is_deleted"], 1)
        self.assertTrue(data["deleted_at"])
        self.assertTrue((s.dir / "project.json").is_file())   # 文件原样保留

    def test_list_filters_and_include_deleted(self):
        self._mk("Alpha", "alpha")
        self._mk("Beta", "beta")
        self.ws.delete_project("alpha")
        ids = [p["id"] for p in self.ws.list_projects()]
        self.assertEqual(ids, ["beta"])                        # 清单过滤单点
        all_ids = [p["id"] for p in self.ws.list_projects(include_deleted=True)]
        self.assertEqual(sorted(all_ids), ["alpha", "beta"])

    def test_get_project_gate_blocks_all_stage_paths(self):
        # get_project 是 _project_path 的总闸：删态项目一切 stage 命令在此拦下
        self._mk("Alpha", "alpha")
        self.ws.delete_project("alpha")
        with self.assertRaises(ProjectError) as ctx:
            self.ws.get_project("alpha")
        self.assertIn("project restore", str(ctx.exception))   # 报错自带恢复指引
        s = self.ws.get_project("alpha", include_deleted=True)  # 恢复/详情通道
        self.assertEqual(s.pid, "alpha")

    def test_deleted_id_stays_occupied(self):
        # 已删项目仍占 id——防重名新建"复活"撞目录
        self._mk("Alpha", "alpha")
        self.ws.delete_project("alpha")
        self.assertTrue(self.ws.exists("alpha"))
        with self.assertRaises(ProjectError):
            self.ws.create_project("Alpha Again", pid="alpha")

    def test_restore_clears_flags(self):
        self._mk("Alpha", "alpha")
        self.ws.delete_project("alpha")
        s = self.ws.restore_project("alpha")
        self.assertEqual(s.data["is_deleted"], 0)
        self.assertNotIn("deleted_at", s.data)
        self.assertEqual([p["id"] for p in self.ws.list_projects()], ["alpha"])
        self.ws.get_project("alpha")                           # 总闸放行

    def test_scanner_aggregates_all_filter_deleted(self):
        # Studio 全部聚合口（summary/queue/board/search/library）一个不漏地过滤软删
        from kinema.studio import scanner
        alive = self._mk("Alive Show", "alive")
        gone = self._mk("Ghost Show", "ghost")
        for s in (alive, gone):
            cf = s.create_chapter("第一章")
            data = json.loads(cf.read_text(encoding="utf-8"))
            data["shots"] = [{"id": 1, "narration": "hello",
                              "review": {"image": {"state": "wfa"}}}]
            self.ws.store.save_chapter(s.pid, "ch01", data)
            work = cf.parent / f"{cf.stem}_work" / "output"
            work.mkdir(parents=True)
            (work / f"{s.pid}_ch01_16x9.mp4").write_bytes(b"v")
        self.ws.delete_project("ghost")
        root = self.ws.root
        self.assertEqual([p["id"] for p in scanner.workspace_summary(root)], ["alive"])
        self.assertEqual({i["project"] for i in scanner.review_queue(root)}, {"alive"})
        self.assertEqual({r["project"] for r in scanner.board(root)["chapters"]}, {"alive"})
        self.assertEqual({h["project"] for h in scanner.search(root, "Show")}, {"alive"})
        self.assertEqual({v["project"] for v in scanner.library(root)}, {"alive"})
        rec = scanner.recycle_bin(root)                        # 回收站单列可见
        self.assertEqual([r["id"] for r in rec], ["ghost"])
        self.assertTrue(rec[0]["deleted_at"])


class TestWorkspaceSummaryOrder(WorkspaceCase):
    """Studio 项目清单按 created_at 倒序：新建的项目排在最前。

    存储层按 id 排序（CLI 清单与 MySQL 协调依赖它），展示序只在 scanner 这一层定；
    /api/projects、总览与侧栏项目树共用同一份，顺序必须一致。
    """

    def _mk(self, pid, created_at=None):
        s = self.ws.create_project(pid.upper(), pid=pid)
        data = self.ws.store.load_project(pid)
        if created_at is None:
            data.pop("created_at", None)
        else:
            data["created_at"] = created_at
        self.ws.store.save_project(pid, data)
        return s

    def test_newest_first_missing_created_at_last(self):
        from kinema.studio import scanner
        self._mk("aaa", "2026-01-01T00:00:00")     # id 升序 ≠ 时间序，才验得出来
        self._mk("bbb", "2026-08-20T10:00:00")
        self._mk("ccc", "2026-03-05T09:30:00")
        self._mk("zzz", None)                      # 老文档缺字段：垫底而非报错
        self.assertEqual([p["id"] for p in scanner.workspace_summary(self.ws.root)],
                         ["bbb", "ccc", "aaa", "zzz"])

    def test_same_timestamp_keeps_id_order(self):
        # 同秒创建（批量建项目）时不抖动：稳定排序保留存储层的 id 升序
        from kinema.studio import scanner
        for pid in ("cc", "aa", "bb"):
            self._mk(pid, "2026-08-24T12:00:00")
        self.assertEqual([p["id"] for p in scanner.workspace_summary(self.ws.root)],
                         ["aa", "bb", "cc"])


class TestSuiteNeverTouchesTheRealBackend(unittest.TestCase):
    """**测试套件自身的守卫**：碰 Workspace 的模块必须钉 LocalBackendEnv。

    `Workspace.open` 的后端读 `KINEMA_STORAGE_BACKEND`，而开发机的 shell 环境
    常年固化成 mysql（fish 通用变量）。漏钉的用例不会报「配置错了」——tempdir
    形同虚设，`create_project` 直接写进用户真库，跑一次污染一次，第二次才以
    `项目已存在: x` 的面目暴露出来，而那时脏数据已经在库里了。

    AGENTS.md 已将此写为纪律，但文档纪律没有 CI 强制；本守卫补上机器检查。
    """

    # 判据覆盖两类经环境解析后端的入口：Workspace 域与 storage 工厂/配置缓存。
    # 直接实例化 LocalStorage/MySQLStorage(打桩) 不经环境解析，不在此列。
    _TOUCHES = ("Workspace.open(", ".create_project(",
                "get_storage(", "load_storage_config(")

    def test_every_module_touching_workspace_pins_the_local_backend(self):
        offenders = []
        for f in sorted(Path(__file__).parent.glob("test_*.py")):
            src = f.read_text(encoding="utf-8")
            if any(t in src for t in self._TOUCHES) and "LocalBackendEnv" not in src:
                offenders.append(f.name)
        self.assertEqual(
            offenders, [],
            "以下测试模块碰了 Workspace 却没钉 LocalBackendEnv，"
            "在 KINEMA_STORAGE_BACKEND=mysql 的机器上会写进真库：" + ", ".join(offenders))


class TestMysqlReadCoordinationSingleSource(unittest.TestCase):
    """mysql 读路径的冲突协调只许有一份判据：`_row_newer` 的「新者赢」。

    `list_projects` 是 Studio 开首页的第一条读路径，先于任何 `load_project`
    运行——它若各写一套「文件为准」，换机/恢复旧副本场景会用过期文件覆写
    库中较新行，且 `updated_at` 随之写旧，此后再也检测不出覆盖发生过
    （`_upsert_project` 连带的资产清理还会删掉另一台机器新建的角色/声纹）。

    离线打桩 `_db`/`_exec`：构造函数不连库，真实连接是 `_db()` 惰性建立的。
    """

    def _store(self, root: Path, *, db_doc: dict, db_at: datetime):
        from kinema.storage.mysql import MySQLStorage
        st = MySQLStorage(root, {"table_prefix": "kn_"})
        st._db = lambda: None

        def fake_exec(sql, args=None, *, fetch=None):
            # 不钉 SQL 形状（列多列少都伺候），让用例只红在冲突判据上
            if sql.startswith("SELECT code FROM kn_project"):
                return [(db_doc["id"],)]
            if sql.startswith("SELECT code, data FROM kn_project"):
                return [(db_doc["id"], json.dumps(db_doc, ensure_ascii=False))]
            if sql.startswith("SELECT data, updated_at FROM kn_project"):
                return (json.dumps(db_doc, ensure_ascii=False), db_at)
            raise AssertionError(f"读路径不该发这条 SQL: {sql}")

        st._exec = fake_exec
        st._upserts = []
        st._upsert_project = lambda code, data: st._upserts.append((code, data))
        return st

    def _local(self, root: Path, doc: dict, *, age_hours: float = 0.0) -> Path:
        pfile = root / doc["id"] / "project.json"
        pfile.parent.mkdir(parents=True)
        pfile.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        if age_hours:
            old = (datetime.now() - timedelta(hours=age_hours)).timestamp()
            os.utime(pfile, (old, old))
        return pfile

    def test_list_projects_lets_newer_db_row_win(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_doc = {"id": "p1", "title": "库中较新"}
            pfile = self._local(root, {"id": "p1", "title": "本地过期"}, age_hours=1)
            st = self._store(root, db_doc=db_doc, db_at=datetime.now())
            out = st.list_projects()
            self.assertEqual([p["title"] for p in out], ["库中较新"])
            self.assertEqual(st._upserts, [],
                             "库行较新时绝不拿本地旧文档上行覆盖")
            # 本地工作副本刷新为库中版本——与 load_project 同一行为
            self.assertEqual(json.loads(pfile.read_text(encoding="utf-8")), db_doc)

    def test_list_projects_uploads_newer_local_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            local_doc = {"id": "p1", "title": "本地较新"}
            self._local(root, local_doc)
            st = self._store(root, db_doc={"id": "p1", "title": "库中过期"},
                             db_at=datetime.now() - timedelta(hours=1))
            out = st.list_projects()
            self.assertEqual([p["title"] for p in out], ["本地较新"])
            self.assertEqual(st._upserts, [("p1", local_doc)])   # 文件新 → 上行入库


class TestMysqlSyncClockSingleSource(unittest.TestCase):
    """参与「新者赢」判据的 updated_at 列只许有一个时刻源：写入方的客户端墙钟。

    `_row_newer` 拿它与本地文件 `st_mtime` 比大小，而 PyMySQL 还回的是 naive
    DATETIME、`.timestamp()` 恒按本机时区解释。列里混进 `NOW()`（MySQL 会话时钟，
    容器缺省 UTC）判据就整体偏一个时区差，两个方向都会覆盖掉较新的一侧。
    """

    def _capture(self, method, *args):
        from kinema.storage.mysql import MySQLStorage
        with tempfile.TemporaryDirectory() as d:
            st = MySQLStorage(Path(d), {"table_prefix": "kn_"})
            st._db = lambda: None
            seen = []

            def fake_exec(sql, params=None, *, fetch=None):
                seen.append((sql, params))
                return (1,) if fetch == "one" else None

            st._exec = fake_exec
            st._project_db_id = lambda code: 1
            st._sync_shots = lambda *a, **k: {}
            st._sync_chapter_assets = lambda *a, **k: None
            st._sync_comments = lambda *a, **k: None
            getattr(st, method)(*args)
            return seen

    def _assert_client_clock(self, seen, table: str):
        from kinema.storage.mysql import MySQLStorage
        rows = [(sql, params) for sql, params in seen if f"INTO kn_{table}" in sql]
        self.assertTrue(rows, f"没有捕获到写 kn_{table} 的语句")
        sql, params = rows[0]
        self.assertNotIn("NOW()", sql,
                         f"kn_{table}.updated_at 仍取 MySQL 会话时钟")
        stamp = datetime.strptime(params[-1], "%Y-%m-%d %H:%M:%S")
        self.assertLess(abs((stamp - datetime.now()).total_seconds()), 5,
                        "写入的 updated_at 不是本机墙钟")
        # 与读侧判据闭环：同刻写下的文件判「不算新」（平手偏向文件），
        # 一小时前的文件判「库更新」——PyMySQL 还回的正是这样一个 naive datetime
        with tempfile.NamedTemporaryFile() as f:
            path = Path(f.name)
            self.assertFalse(MySQLStorage._row_newer(stamp, path))
            old = (datetime.now() - timedelta(hours=1)).timestamp()
            os.utime(path, (old, old))
            self.assertTrue(MySQLStorage._row_newer(stamp, path))

    def test_chapter_row_stamps_the_client_clock(self):
        seen = self._capture("_upsert_chapter", "p1", "ch01", {"shots": []})
        self._assert_client_clock(seen, "chapter")

    def test_setting_row_stamps_the_client_clock(self):
        seen = self._capture("save_settings", "models", "overlay", {"a": 1})
        self._assert_client_clock(seen, "setting")


class TestMysqlExistenceCoversTheDatabase(unittest.TestCase):
    """mysql 模式下「这个 id 占了没」必须查库，不能只看本地文件。

    `project/` 是 gitignored 的工作副本，换机/重装后盘上为空而库里有行；按文件判存在
    会放行同名新建，`_upsert_project` 随即覆盖 data 列、`_sync_assets` 与
    `_sync_voice_casts` 连带删光该项目的资产与音色档案行。章节侧同理，且
    `list_projects`/`load_project` 只回填项目文档，章节要点名才 rehydrate。

    离线打桩 `_db`/`_exec`（同 `TestMysqlReadCoordinationSingleSource` 的做法）。
    """

    def _store(self, root: Path, *, projects=(), chapters=()):
        from kinema.storage.mysql import MySQLStorage
        st = MySQLStorage(root, {"table_prefix": "kn_"})
        st._db = lambda: None

        def fake_exec(sql, args=None, *, fetch=None):
            if sql.startswith("SELECT 1 FROM kn_project"):
                return (1,) if args[0] in projects else None
            if sql.startswith("SELECT 1 FROM kn_chapter"):
                return (1,) if tuple(args) in chapters else None
            raise AssertionError(f"存在性判据不该发这条 SQL: {sql}")

        st._exec = fake_exec
        return st

    def test_project_id_in_database_counts_as_taken(self):
        with tempfile.TemporaryDirectory() as d:
            st = self._store(Path(d), projects={"demo"})
            self.assertTrue(st.project_exists("demo"), "库里有行却判成 id 空闲")
            self.assertFalse(st.project_exists("nope"))

    def test_creating_over_a_database_only_project_is_refused(self):
        env = LocalBackendEnv()
        env.enable()
        try:
            from kinema.workspace import Workspace
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                ws = Workspace(root)
                ws.store = self._store(root, projects={"demo"})
                with self.assertRaises(ProjectError):
                    ws.create_project("第二部", pid="demo")
                self.assertEqual(list(root.iterdir()), [],
                                 "被拒的新建不该留下半个项目目录")
        finally:
            env.restore()

    def test_chapter_id_in_database_counts_as_taken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            from kinema.workspace import Series, Workspace
            st = self._store(root, chapters={("x", "ch03")})
            self.assertTrue(st.chapter_exists("x", "ch03"), "库里有章却判成章号空闲")
            self.assertFalse(st.chapter_exists("x", "ch04"))
            ws = Workspace(root)
            ws.store = st
            series = Series(ws, "x", {"id": "x", "chapters": []})
            with self.assertRaises(ProjectError):
                series.create_chapter("第三集", cid="ch03")

    def test_chapter_creation_gate_goes_through_the_store(self):
        """建章闸走 store，不裸用 `cf.is_file()`——后者与 `scaffold_episodes` 的判据分叉。"""
        src = (Path(__file__).parents[1] / "kinema" / "workspace.py").read_text(
            encoding="utf-8")
        seg = src.split("def create_chapter(")[1].split("\n    def ")[0]
        self.assertIn("self.ws.store.chapter_exists(", seg, "建章闸没走存储层")
        self.assertNotIn("if cf.is_file():", seg, "建章闸仍在按本地文件判存在")


class TestSeriesDocumentWriteLock(unittest.TestCase):
    """系列文档的写必须持**跨进程**文件锁：Studio 把生成类操作派成子进程
    （studio/jobs.spawn_cli），进程内的 RLock 对它们之间的竞争完全无效。"""

    def test_save_and_commit_hold_the_file_lock(self):
        env = LocalBackendEnv()
        env.enable()
        try:
            from kinema import workspace as workspace_mod
            from kinema.workspace import Workspace
            with tempfile.TemporaryDirectory() as d:
                ws = Workspace(Path(d))
                series = ws.create_project("锁测", pid="lk")
                lock = Path(d) / "lk" / "project.json.lock"
                held = []
                real = workspace_mod._doc_lock

                @contextmanager
                def spy(path):
                    held.append(Path(path))
                    with real(path):
                        yield

                workspace_mod._doc_lock = spy
                try:
                    series.save()
                    with series.commit():
                        series.data["theme"] = "x"
                finally:
                    workspace_mod._doc_lock = real
                self.assertTrue(held, "Series 写盘没有经过文档写锁")
                self.assertEqual({p.name for p in held}, {"project.json"},
                                 "锁必须挂在系列文档路径上")
                self.assertTrue(lock.is_file(), "未落下与章节同底座的 .lock")
        finally:
            env.restore()

    def test_nested_save_inside_commit_does_not_self_deadlock(self):
        """flock 按打开文件描述判归属，同线程二次申请会自己等自己——
        `commit()` 块内的那次 `save()` 必须走重入分支。"""
        env = LocalBackendEnv()
        env.enable()
        try:
            from kinema.workspace import Workspace
            with tempfile.TemporaryDirectory() as d:
                ws = Workspace(Path(d))
                series = ws.create_project("重入", pid="re")
                done = threading.Event()

                def run():
                    with series.commit():
                        series.data["theme"] = "nested"
                        series.save()          # 块内嵌套：不得阻塞
                    done.set()

                t = threading.Thread(target=run, daemon=True)
                t.start()
                t.join(timeout=10)
                self.assertTrue(done.is_set(), "commit 内嵌 save 发生自锁")
                self.assertEqual(ws.get_project("re").data["theme"], "nested")
        finally:
            env.restore()

    def test_long_running_writers_merge_instead_of_overwriting(self):
        """设定图重生/局部改造/封面三条长任务的收尾必须锁内重读后按身份合并。
        整份写回命令启动时那份快照，会把生成期内别处（含另一个子进程）的写入抹掉。"""
        cli = (Path(__file__).parents[1] / "kinema" / "cli.py").read_text(encoding="utf-8")
        refs = cli.split("def cmd_gen_refs(")[1].split("\ndef ")[0]
        # 两波各两段：主设定图（计划期归档 + 回填期合并），场景俯视图同样两段——
        # 俯视图必须排在基准图落盘之后才拿得到参考，故它自成一波
        self.assertEqual(refs.count("with s.commit():"), 4,
                         "gen-refs 应为两波各两段提交：计划期归档 + 回填期合并")
        self.assertNotIn("s.save()", refs, "gen-refs 仍在整份覆写")
        cover = cli.split("def cmd_cover(")[1].split("\ndef ")[0]
        self.assertIn("with s.commit():", cover, "系列封面回填未走锁内重读")
        self.assertNotIn("\n    s.save()", cover, "系列封面仍在整份覆写")
        refine = (Path(__file__).parents[1] / "kinema" / "refine.py").read_text(
            encoding="utf-8")
        seg = refine.split("def refine_asset(")[1].split("\ndef ")[0]
        self.assertEqual(seg.count("with series.commit():"), 2,
                         "局部改造应为两段提交：归档 + 回填")
        self.assertNotIn("series.save()", seg, "局部改造仍在整份覆写")


class TestAtomicDocumentWrite(unittest.TestCase):
    """项目/章节文档必须原子落盘：Studio 线程与 spawn_cli 子进程并发读写，
    半截文件会被读端当「不存在」处理，进而以陈旧副本整份回写丢更新。"""

    def test_write_replaces_and_leaves_no_tmp(self):
        from kinema.storage import atomic_write_json
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "project.json"
            atomic_write_json(p, {"id": "x"})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"id": "x"})
            self.assertEqual(list(Path(d).glob("*.tmp")), [],
                             "临时文件必须随 replace 消失")

    def test_read_raises_on_corrupt(self):
        """损坏与缺失是两种状态：损坏必须抛错，缺失才返 None。"""
        from kinema.errors import DocumentCorruptError
        from kinema.storage import local as L
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "project.json"
            p.write_text('{"id": "x', encoding="utf-8")   # 模拟被打断的写
            with self.assertRaises(DocumentCorruptError):
                L._read(p)
            self.assertIsNone(L._read(Path(d) / "missing.json"))


class TestChapterTitleSingleSource(unittest.TestCase):
    """章节标题只认一个源：**章节文档赢，project.json 的登记表兜底**。

    标题天生落在两处（建章时写登记表、改名时改章节文档），两处各读各的结果是
    章节页已经改名、项目页与首页还挂着旧名——同一个东西在两个页面上叫两个名字，
    表象与数据损坏无法区分。
    """

    def test_document_title_wins(self):
        from kinema.studio.scanner import _chapter_title
        reg = {"id": "ch01", "title": "旧名"}
        doc = {"chapter": {"title": "新名"}}
        self.assertEqual(_chapter_title(reg, doc), "新名")

    def test_registry_covers_a_missing_document(self):
        """章节文件未建或被删时，登记表是唯一还知道这章叫什么的地方。"""
        from kinema.studio.scanner import _chapter_title
        reg = {"id": "ch01", "title": "登记名"}
        for doc in (None, {}, {"chapter": {}}, {"chapter": None}):
            self.assertEqual(_chapter_title(reg, doc), "登记名", repr(doc))

    def test_falls_back_to_id_when_nothing_named_it(self):
        from kinema.studio.scanner import _chapter_title
        self.assertEqual(_chapter_title({"id": "ch07"}, {}), "ch07")

    def test_no_view_reads_the_registry_title_directly(self):
        """读侧**不许再直读登记表 title**——正向数调用点的旧守卫把「只修了三处」
        的半成品状态写进了断言（待审队列/看板/搜索/成本页四处漏网照样绿，
        搜索还是功能性假阴性）。反向断言挡的是「新增了一处漏网」。"""
        root = Path(__file__).resolve().parents[1] / "kinema"
        for rel in ("studio/scanner.py", "business.py"):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn('ch.get("title")', src, rel)

    def test_resolver_lives_in_the_shared_storage_layer(self):
        """解析口实体在存储层（storage.base.chapter_title）：引擎域 business 的
        成本页同用，studio 私有的话它只能再抄一份（studio 不可被引擎反向引）。"""
        from kinema.business import chapter_title as biz
        from kinema.storage.base import chapter_title as base
        from kinema.studio.scanner import _chapter_title as scan
        self.assertIs(scan, base)
        self.assertIs(biz, base)


class TestCorruptDocumentFailClosed(WorkspaceCase):
    """损坏文档必须显式报错并保住 ID。

    按「不存在」处理的后果：存在性判断与创建闸同时失守，同 ID 新建会把
    仍可人工修复的文件整份覆盖，一次读失败被放大为丢数据。"""

    def test_corrupt_chapter_raises_and_creation_refuses(self):
        from kinema.errors import DocumentCorruptError
        s = self.ws.create_project("坏档案")
        cf = s.create_chapter("第一章")
        cf.write_text('{"id": "half', encoding="utf-8")
        with self.assertRaises(DocumentCorruptError):
            s.get_chapter_path("ch01")
        with self.assertRaises(ProjectError):
            s.create_chapter("第一章", cid="ch01")
        self.assertEqual(cf.read_text(encoding="utf-8"), '{"id": "half',
                         "损坏文件必须原样保留")

    def test_corrupt_project_keeps_id_and_listing_survives(self):
        from kinema.errors import DocumentCorruptError
        s = self.ws.create_project("坏项目")
        (s.dir / "project.json").write_text("not json", encoding="utf-8")
        self.assertTrue(self.ws.exists(s.pid), "损坏项目必须仍判「存在」")
        with self.assertRaises(DocumentCorruptError):
            self.ws.store.load_project(s.pid)
        ok = self.ws.create_project("好项目")
        listed = [p.get("id") for p in self.ws.store.list_projects()]
        self.assertIn(ok.pid, listed, "单个坏文档不得让清单整体失效")
        self.assertNotIn(s.pid, listed)


class TestCreateDefaultsHorizontal(WorkspaceCase):
    """建项缺省契约：比例恒 16:9 横屏、平台不做默认绑定（schema 同口径：
    竖屏/方形须用户点名）。

    lastcar 实案：models.yaml 无人消费的 defaults.aspect: "9:16" 死键 + 建项默认
    platform=["douyin"] 联手诱导指挥层把未指定比例的项目建成竖屏，`--both` 双出时
    16:9 成片由竖图 cover-crop，构图全毁。缺省真源收敛为 project.DEFAULT_ASPECT
    单点后，本组守卫钉住三件事：缺省值本身、显式值不被覆盖、章节继承同口径。"""

    def test_defaults_are_horizontal_and_platform_unbound(self):
        s = self.ws.create_project("默认项目")
        self.assertEqual(s.data["aspect"], "16:9")
        self.assertEqual(s.data["platform"], [])

    def test_explicit_aspect_and_platform_win(self):
        s = self.ws.create_project("竖屏项目", aspect="9:16", platform=["douyin"])
        self.assertEqual(s.data["aspect"], "9:16")
        self.assertEqual(s.data["platform"], ["douyin"])

    def test_chapter_inherits_default_aspect(self):
        s = self.ws.create_project("默认项目")
        video = json.loads(s.create_chapter("第一章").read_text(encoding="utf-8"))
        self.assertEqual(video["aspect"], "16:9")
        self.assertEqual(video["platform"], [])

    def test_aspect_fallbacks_are_single_source(self):
        # 源级扫描：引擎内比例兜底一律引用 project.DEFAULT_ASPECT，不得再写
        # 比例字面量——16:9/9:16 各处各写一份正是本次分叉的根因。
        import re
        pkg = Path(__file__).resolve().parent.parent / "kinema"
        pat = re.compile(r'\.get\(\s*"aspect"\s*(?:,\s*|\)\s*or\s+)"(?:9:16|16:9|1:1)"')
        hits = [f"{p.relative_to(pkg)}:{i}"
                for p in sorted(pkg.rglob("*.py"))
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if pat.search(line)]
        self.assertEqual(hits, [],
                         "比例兜底出现字面量，应引用 project.DEFAULT_ASPECT: " + "; ".join(hits))


class TestDeliverRequiresPlatform(WorkspaceCase):
    """交付包平台缺省不兜底：未绑定平台必须抛明确领域错误，绝不静默打成抖音包
    （交付包按平台分目录组织，平台是它的组织轴，猜不得）。"""

    def test_unbound_platform_raises(self):
        from kinema.deliver import build_delivery
        from kinema.project import Project
        mp4 = Path(self.tmp.name) / "final.mp4"
        mp4.write_bytes(b"x")
        proj = Project(Path(self.tmp.name) / "p.json",
                       {"id": "p", "output": {"16:9": str(mp4)}, "platform": []})
        with self.assertRaises(ProjectError) as ctx:
            build_delivery(proj, make_zip=False)
        self.assertIn("平台", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class TestProjectListRobustness(WorkspaceCase):
    """`project list` 对存量坏条目的容错：单个项目缺 title 之类字段时，
    整张列表不许被一条 KeyError 打断。"""

    def test_missing_title_does_not_break_the_list(self):
        import argparse
        import contextlib
        import io

        from kinema.cli import cmd_project_list
        s = self.ws.create_project("Alpha", pid="alpha")
        s.data.pop("title", None)
        s.save()
        self.ws.create_project("Beta", pid="beta")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_project_list(argparse.Namespace(
                workspace=str(self.ws.root), deleted=False))
        out = buf.getvalue()
        self.assertIn("alpha", out)
        self.assertIn("Beta", out)


class TestNarratorVoiceInheritance(WorkspaceCase):
    """旁白锁的两种写法建章都要认：`voice use` 立档写 `narrator.voice`，
    系列顶层直写 `narrator_voice`——只认前者时后者是个静默死键，
    旁白落回 profile 默认且全程无提示。"""

    def test_top_level_narrator_voice_reaches_the_chapter(self):
        import json
        s = self.ws.create_project("旁白继承", pid="nv")
        s.data["narrator_voice"] = "译制腔"
        s.save()
        cf = s.create_chapter("第一章")
        self.assertEqual(json.loads(cf.read_text(encoding="utf-8"))
                         .get("narrator_voice"), "译制腔")

    def test_cast_narrator_object_still_wins(self):
        import json
        s = self.ws.create_project("旁白立档", pid="nv2")
        s.data["narrator"] = {"voice": "沧桑老者"}
        s.data["narrator_voice"] = "译制腔"
        s.save()
        cf = s.create_chapter("第一章")
        self.assertEqual(json.loads(cf.read_text(encoding="utf-8"))
                         .get("narrator_voice"), "沧桑老者",
                         "试音立档的选角优先于顶层直写")
