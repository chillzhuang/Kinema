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

"""Studio HTTP 层与数据层之间的**接线**守卫（studio/server.py → scanner/actions/jobs）。

钉的是接线层特有的洞：`scanner.chapter_detail(ws_root, store, pid, cid)`
若被 server 少传一个 store，`/api/chapter` 整条路由 500，
而函数本身的单元测试照样全绿（都按正确签名直调）——
坏掉的是**谁怎么调它**，那一行没有任何测试经过。

这类 bug 的特征是「测试全绿但页面打不开」：
· 单测直调数据层函数 → 覆盖不到 server 的调用行；
· server 的路由分支只在真请求时求值 → import 期不报错，语法检查也不报错；
· 于是签名改了、调用点漏改，要等用户点开那个页面才炸。

故这里不测业务，只测**每一条 scanner./actions./jobs. 调用能否绑上真实签名**，
一次覆盖全部路由分支，且零 IO、零环境依赖。
"""
from __future__ import annotations

import ast
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kinema.studio import actions, jobs, scanner


def _server_src() -> tuple[str, Path]:
    import kinema
    p = Path(kinema.__file__).parent / "studio" / "server.py"
    return p.read_text(encoding="utf-8"), p


class TestConfigPathReachesSubprocesses(unittest.TestCase):
    """`studio --config` 经 KINEMA_MODELS 下发给后台任务与预览编译，页面与实发同一份配置。"""

    def test_bind_exports_env(self):
        import os
        from unittest import mock

        from kinema.studio import actions
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KINEMA_MODELS", None)
            actions.bind_config_path(None)
            self.assertNotIn("KINEMA_MODELS", os.environ)
            actions.bind_config_path("/x/models.yaml")
            self.assertEqual(os.environ["KINEMA_MODELS"], "/x/models.yaml")
        actions.bind_config_path(None)


class TestStudioWorkspacePath(unittest.TestCase):
    """Studio 的项目列表与片库必须从同一个规范化工作区读取。"""

    @staticmethod
    def _serve_args(workspace, *, root="."):
        from kinema import cli

        args = SimpleNamespace(config=None, workspace=str(workspace), root=root, port=8787,
                               restart=False, stop=False, status=False)
        with patch.object(cli.ConfigStore, "shared", return_value=object()), \
             patch("kinema.studio.server.running_instance", return_value=None), \
             patch("kinema.studio.server.other_studio_pids", return_value=[]), \
             patch("kinema.studio.serve") as serve:
            cli.cmd_studio(args)
        serve.assert_called_once()
        return serve.call_args.kwargs

    def test_repository_root_argument_is_normalized_before_serve(self):
        repo = Path(__file__).resolve().parents[2]
        kwargs = self._serve_args(repo)
        self.assertEqual(kwargs["workspace"], str((repo / "project").resolve()))
        self.assertEqual(kwargs["root"], str(repo.resolve()))

    def test_custom_workspace_keeps_data_root_and_aligns_default_library_root(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d) / "isolated"
            kwargs = self._serve_args(workspace)
        self.assertEqual(Path(kwargs["workspace"]).resolve(), workspace.resolve())
        self.assertEqual(Path(kwargs["root"]).resolve(), workspace.parent.resolve())

    def test_explicit_library_root_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            workspace = Path(d) / "isolated"
            library = Path(d) / "media"
            kwargs = self._serve_args(workspace, root=str(library))
        self.assertEqual(Path(kwargs["workspace"]).resolve(), workspace.resolve())
        self.assertEqual(Path(kwargs["root"]).resolve(), library.resolve())


class TestServerCallsMatchDataLayerSignatures(unittest.TestCase):
    """server.py 里对数据层的每一次调用都必须能绑上被调方的真实签名。"""

    _MODULES = {"scanner": scanner, "actions": actions, "jobs": jobs}

    def _mismatches(self) -> list[str]:
        src, _ = _server_src()
        sentinel = object()
        bad = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
                continue
            mod = self._MODULES.get(f.value.id)
            if mod is None:
                continue
            fn = getattr(mod, f.attr, None)
            if not callable(fn):
                continue
            # *args / **kwargs 展开的实参个数编译期不可知，跳过（当前无此写法）
            if any(isinstance(a, ast.Starred) for a in node.args) \
                    or any(k.arg is None for k in node.keywords):
                continue
            try:
                inspect.signature(fn).bind(
                    *[sentinel] * len(node.args),
                    **{k.arg: sentinel for k in node.keywords})
            except TypeError as e:
                bad.append(f"server.py:{node.lineno} {f.value.id}.{f.attr}() → {e}")
        return bad

    def test_every_call_binds(self):
        bad = self._mismatches()
        self.assertEqual(bad, [], "HTTP 层与数据层签名脱节（页面会 500，但单测照样全绿）：\n"
                                 + "\n".join(bad))

    def test_the_guard_actually_binds_something(self):
        """守卫自身的活性检查：解析不到任何调用就说明匹配逻辑失效了，
        那时 test_every_call_binds 会恒绿失效。"""
        src, path = _server_src()
        self.assertTrue(path.is_file())
        seen = {f"{n.func.value.id}.{n.func.attr}"
                for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in self._MODULES
                and callable(getattr(self._MODULES[n.func.value.id], n.func.attr, None))}
        self.assertGreater(len(seen), 20, "扫到的数据层调用太少，AST 匹配逻辑多半已失效")
        self.assertIn("scanner.chapter_detail", seen)   # 已知漏扫点，钉住


class TestChapterRouteWiring(unittest.TestCase):
    """`/api/chapter` 那一行的定点守卫：store 必须传进去。

    `chapter_detail` 用 store 解析音色（`store.resolve_voice`）——少传它时
    Python 会把 pid 当 store、cid 当 pid，参数整体错位一位。上面的签名守卫
    已能拦下这次的形态（少一个参数直接 TypeError），但**同为四参数的错位传法
    绑得上签名却依然是错的**，故这里额外钉住实参名字。
    """

    def test_store_is_passed_positionally_in_order(self):
        src, _ = _server_src()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "chapter_detail":
                names = [a.id if isinstance(a, ast.Name) else ast.dump(a) for a in node.args]
                self.assertEqual(names, ["ws_root", "store", "pid", "cid"],
                                 "参数错位：chapter_detail(ws_root, store, pid, cid)")
                return
        self.fail("server.py 里找不到 chapter_detail 调用")


class TestUploadPathContainment(unittest.TestCase):
    """上传端点的定位参数（project/chapter/shot）由请求方任意给定，
    拼进文件路径前必须过 flat_name / project_dir 两道闸——闸后写盘，
    不允许「先落盘、登记失败再发现路径越界」。"""

    def test_flat_name_rejects_path_segments(self):
        from kinema.studio import server
        for bad in ("", ".", "..", "a/b", "../x", "/abs"):
            self.assertFalse(server.flat_name(bad), bad)
        for ok in ("demo", "ch01", "12", "my-proj_2"):
            self.assertTrue(server.flat_name(ok), ok)

    def test_project_dir_contains_and_requires_document(self):
        import tempfile
        from kinema.studio import server
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "demo").mkdir()
            (ws / "demo" / "project.json").write_text("{}", encoding="utf-8")
            (ws / "bare").mkdir()
            self.assertEqual(server.project_dir(ws, "demo"), (ws / "demo").resolve())
            self.assertIsNone(server.project_dir(ws, "bare"),
                              "无 project.json 的目录不是项目")
            self.assertIsNone(server.project_dir(ws, "../demo"))
            self.assertIsNone(server.project_dir(ws, str((ws / "demo").resolve())))

    def test_upload_handlers_resolve_project_through_the_gate(self):
        src, _ = _server_src()
        for fn in ("_shot_upload", "_previz_frame", "_previz_upload",
                   "_control_upload", "_moodboard_upload"):
            body = src.split(f"def {fn}(", 1)[1].split("\n        def ", 1)[0]
            self.assertIn("project_dir(", body,
                          f"{fn} 必须经 project_dir 定位项目目录")


class TestEngineStaleDetection(unittest.TestCase):
    """常驻进程 vs 磁盘代码的错配检测——「测试全绿但页面行为是旧的」另一半。

    Studio 的 Python 定格于启动时刻、前端资源逐请求读盘，引擎代码更新后不重启
    就是新前端配旧后端。该错配已两次放倒章节页且每次都要排查到进程年龄才破案，
    故指纹语义（只认 *.py、跟踪 mtime/size）与注入接线（_json 唯一出口 ×
    serve 传 boot 指纹）都必须钉死。"""

    def test_fingerprint_tracks_py_changes_only(self):
        import tempfile
        import time as _t
        from kinema.studio import server
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            (root / "sub").mkdir()
            (root / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
            (root / "note.md").write_text("draft\n", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "a.py").write_text("stray\n", encoding="utf-8")
            fp0 = server.engine_fingerprint(root)
            self.assertEqual(fp0, server.engine_fingerprint(root), "同一状态必须同指纹")
            os.utime(root / "note.md", (1, 1))
            (root / "__pycache__" / "a.py").write_text("stray2\n", encoding="utf-8")
            self.assertEqual(fp0, server.engine_fingerprint(root),
                             "非 .py 与 __pycache__ 的变动不许惊动指纹——"
                             "前端资源逐请求读盘，提示重启只会误导")
            os.utime(root / "a.py", (_t.time() + 9, _t.time() + 9))
            self.assertNotEqual(fp0, server.engine_fingerprint(root),
                                "源码 mtime 变了指纹必须变，否则错配永远测不出来")

    def test_stale_flag_is_wired_from_boot_to_json(self):
        src, _ = _server_src()
        self.assertIn("boot_fp=engine_fingerprint()", src,
                      "serve() 必须在启动时记 boot 指纹传给 handler")
        body = src.split("def _json(", 1)[1].split("\n        def ", 1)[0]
        self.assertIn("_engine_stale(boot_fp)", body,
                      "亮牌必须挂在 _json 唯一出口——挂在个别路由上，"
                      "用户停在别的页面就永远看不到")
        self.assertIn('"engine_stale"', body, "键名是前端 core.api 的消费契约")


class TestCustomVoiceStaysWhereTheUserIsWorking(unittest.TestCase):
    """定制生成：**生成变体不许让页面跳离当前位置**。

    失败形态：按描述生成三条变体成功后，前端若 `getOverview(true); render()`
    整页重绘，页签默认落点只认已锁定的 `voice_mode`（此刻仍是 preset），
    刚花钱生成的三条变体连同「定制生成」页签一起消失，用户回到模版生成。
    这条路是按秒计费的：看不见 = 会再点一次。

    两半各钉一处：端点必须自带可播放地址（前端才有得就地插入），
    前端必须停在这条路上（页签判据 + 不重绘）。
    """

    @staticmethod
    def _project_src() -> str:
        import kinema
        return (Path(kinema.__file__).parent / "studio_app" / "app"
                / "project.js").read_text(encoding="utf-8")

    def test_endpoint_returns_playable_urls_for_each_candidate(self):
        # 只回 {no} 的话前端拿不到音频，就地插入只能渲染成占位文字，
        # 于是又得整页重绘去换 URL——页面跳离的根因就在这里。
        # 两条试音路共用同一个下发口径，各写一份必然只有一边带上音频
        src = inspect.getsource(actions._audition_view)
        self.assertIn("_murl", src, "候选必须带 media：URL 口径与 scanner 下发同一个")
        self.assertIn('"media"', src)
        for fn in (actions.voice_audition, actions.voice_custom):
            self.assertIn("_audition_view", inspect.getsource(fn))

    def test_tab_default_follows_the_voice_in_use(self):
        """页签回答的是「这个人的声音怎么来的」，所以落点跟在用档案走；还没有
        在用音色时停在定制生成（缺省路径）。跟着候选跑的话，刚在模版页签点了
        「用这条」，就会因为定制那边留着几条旧候选被弹到另一页。"""
        src = self._project_src()
        body = src.split("function voiceRoutes(", 1)[1].split("\n}", 1)[0]
        self.assertIn('active ? active.mode : "custom"', body)
        self.assertLess(body.index('key: "custom"'), body.index('key: "preset"'))

    def test_generating_candidates_does_not_repaint_the_page(self):
        src = self._project_src()
        for anchor in ('"↻ 重新试音"', '"↻ 重新生成定制"'):
            gen = src.split(anchor, 1)[1].split("box.append(gen)", 1)[0]
            self.assertIn("paint(r.entries", gen, "候选行必须就地换，不是整页重绘")
            self.assertNotIn("render()", gen,
                             "生成候选绝不许 render()——重绘会把人甩回缺省页签；"
                             "启用(use)才该重绘（那一步改的是全项目状态）")

    def test_candidates_never_render_a_selected_state(self):
        """候选是临时物。给它画选中态，重新试音换掉整批之后，页面就会把另一条
        音频显示成「已选」——选中态的真源只能是档案。"""
        src = self._project_src()
        body = src.split("function auditionRows(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("chosen", body)
        self.assertIn('"已入档"', body, "已立档的候选要标出来，而不是标成已选")


class TestGracefulSigterm(unittest.TestCase):
    """`--restart`/`--stop` 用 SIGTERM 接管实例：必须以 0 收场——不装处理器时
    进程死于默认信号处置（退出码 143），外层后台任务/编排器会把每次正常换班
    都记成一次失败。"""

    def test_sigterm_handler_exits_zero(self):
        from kinema.studio import server
        with self.assertRaises(SystemExit) as cm:
            server._graceful_term()
        self.assertEqual(cm.exception.code, 0)

    def test_serve_wires_the_handler(self):
        # 接线守卫：处理器存在但 serve 没装等于没修
        from kinema.studio import server
        src = inspect.getsource(server.serve)
        self.assertIn("signal.SIGTERM, _graceful_term", src)


class TestDeletedProjectRejectsChapterWrites(unittest.TestCase):
    """软删项目的章节级写路径必须被 `Workspace.get_project` 总闸拦下。

    storage 的 `load_chapter`/`chapter_path` 不查 `is_deleted`——actions 若只靠
    它们装载，`/api/review`、`/api/rollback` 等端点对已删项目照写不误；前端
    `.ro-deleted` 只拦页面点击，`curl` 一发就穿。恢复后须立即恢复可写。"""

    def setUp(self):
        import tempfile
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        s = self.ws.create_project("闸门", pid="gate")
        s.create_chapter("第一章", cid="ch01")
        data = self.ws.store.load_chapter("gate", "ch01")
        data["shots"] = [{"id": 1, "dur": 3, "narration": "第一镜。"}]
        self.ws.store.save_chapter("gate", "ch01", data)

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def test_soft_deleted_project_blocks_writes_and_restore_reopens(self):
        from kinema.errors import ProjectError
        root = self.ws.root
        r = actions.set_review(root, "gate", "ch01",
                               shots=[1], stage="image", state="done")
        self.assertEqual(r["updated"], 1)
        self.ws.delete_project("gate")
        with self.assertRaises(ProjectError):
            actions.set_review(root, "gate", "ch01",
                               shots=[1], stage="image", state="retake")
        self.ws.restore_project("gate")
        r = actions.set_review(root, "gate", "ch01",
                               shots=[1], stage="image", state="retake")
        self.assertEqual(r["updated"], 1)

    def test_every_chapter_loader_passes_the_gate(self):
        """不点名装载口：actions 里凡触碰章节装载原语（store.chapter_path /
        Project.load / Project.mutate）的顶层函数都必须过 `_gate`——只钉三个
        名字的话，明天新增第四条旁路照绿，闸就成了假闸。"""
        import re
        src = inspect.getsource(actions)
        primitives = ("store.chapter_path(", "Project.load(", "Project.mutate(")
        offenders = []
        for block in re.split(r"\n(?=@|def )", src):
            m = re.search(r"\A(?:@[\w.]+(?:\(\))?\s*\n)*def (\w+)\(", block)
            if not m or m.group(1) == "_gate":
                continue
            if any(t in block for t in primitives) and "_gate(" not in block:
                offenders.append(m.group(1))
        self.assertEqual(offenders, [],
                         f"这些函数装载章节却未过删态闸: {offenders}")


class TestStudioNeverImportsCli(unittest.TestCase):
    """studio 域不得反向依赖 cli（cli 是入口层，studio→cli 会把 8000 行入口
    模块拽进每个网页请求的依赖面；共享实体一律下沉领域模块，cli 留别名）。"""

    # 覆盖直接/别名/动态三类形态；rglob 防 studio/ 将来加子包后扫不到
    _CLI_IMPORT_FORMS = ("from ..cli import", "from kinema.cli import",
                         "from .cli import", "from .. import cli",
                         "from kinema import cli", "import kinema.cli",
                         'import_module("kinema.cli"')

    def test_no_cli_import_in_studio_modules(self):
        import kinema
        studio = Path(kinema.__file__).parent / "studio"
        offenders = []
        for f in sorted(studio.rglob("*.py")):
            src = f.read_text(encoding="utf-8")
            if any(form in src for form in self._CLI_IMPORT_FORMS):
                offenders.append(f.name)
        self.assertEqual(offenders, [],
                         "studio 模块反向 import 了 cli——把实体下沉领域模块，cli 留别名")


if __name__ == "__main__":
    unittest.main()


class TestCoverFallbackChain(unittest.TestCase):
    """卡片图源三级回落：章节/系列封面 → 成片海报帧 → 分镜图。

    前两级都缺时没有兜底就是整片空白（封面不是自动产物，成片没合就两级全空），
    既不报错也无迹象。兜底图源顶上来之后必须仍能看出「封面还欠着」——
    `cover_missing` 与章节项的 `cover` 只认真封面，绝不被回落图源填上。
    """

    def setUp(self):
        import tempfile
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        s = self.ws.create_project("岸上那一抖", pid="dog")
        s.create_chapter("第一章", cid="ch01")
        imgs = self.ws.root / "dog" / "chapters" / "ch01_work" / "images"
        imgs.mkdir(parents=True)
        (imgs / "shot_1.png").write_bytes(b"png")
        data = self.ws.store.load_chapter("dog", "ch01")
        data["shots"] = [{"id": 1, "dur": 3, "narration": "第一镜。",
                          "image": str(imgs / "shot_1.png")}]
        self.ws.store.save_chapter("dog", "ch01", data)

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _cover_file(self, name: str) -> str:
        covers = self.ws.root / "dog" / "assets" / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        (covers / name).write_bytes(b"png")
        return f"dog/assets/covers/{name}"

    def test_shot_image_backs_the_card_when_cover_and_render_are_missing(self):
        proj = scanner.workspace_summary(self.ws.root)[0]
        self.assertIn("shot_1.png", proj["cover"] or "")
        self.assertTrue(proj["cover_missing"], "回落图源顶上来不算做过封面")
        ch = scanner.project_detail(self.ws.root, None, "dog")["chapters"][0]
        self.assertIn("shot_1.png", ch["poster"] or "")
        self.assertIsNone(ch["cover"], "章节 cover 只认真封面")

    def test_real_cover_wins_over_the_fallback(self):
        data = self.ws.store.load_project("dog")
        data["cover"] = {"primary": self._cover_file("series_3x4.png")}
        self.ws.store.save_project("dog", data)
        cdata = self.ws.store.load_chapter("dog", "ch01")
        cdata["cover"] = {"primary": self._cover_file("ch01_3x4.png")}
        self.ws.store.save_chapter("dog", "ch01", cdata)
        proj = scanner.workspace_summary(self.ws.root)[0]
        self.assertIn("series_3x4.png", proj["cover"] or "")
        self.assertFalse(proj["cover_missing"])
        ch = scanner.project_detail(self.ws.root, None, "dog")["chapters"][0]
        self.assertIn("ch01_3x4.png", ch["poster"] or "")
        self.assertIn("ch01_3x4.png", ch["cover"] or "")

    def test_omitted_shot_image_is_not_used(self):
        # 弃镜不进成片，拿它当门面等于用一张已经废掉的画代表整章
        cdata = self.ws.store.load_chapter("dog", "ch01")
        cdata["shots"][0]["review"] = {"shot": {"state": "omt"}}
        self.ws.store.save_chapter("dog", "ch01", cdata)
        proj = scanner.workspace_summary(self.ws.root)[0]
        self.assertIsNone(proj["cover"])
        ch = scanner.project_detail(self.ws.root, None, "dog")["chapters"][0]
        self.assertIsNone(ch["poster"])
