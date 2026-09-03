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

"""章节写入协调守卫：文件锁互斥/重入、Project.mutate 的磁盘基线契约、
阶段命令的章节操作锁准入。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kinema import locking
from kinema.errors import KinemaError
from kinema.project import Project


class TestFileLock(unittest.TestCase):
    """flock/msvcrt 锁按打开文件描述判归属：同进程内两个独立句柄同样互斥，
    跨进程冲突可以在单进程内等价复现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "ch01.json"
        self.doc.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_nonblocking_conflict_raises(self):
        lock_path = self.doc.with_suffix(".json.oplock")
        with locking.FileLock(lock_path, blocking=False):
            with self.assertRaises(KinemaError):
                locking.FileLock(lock_path, blocking=False).acquire()

    def test_op_lock_is_reentrant_in_process(self):
        with locking.op_lock(self.doc, "gen_video"):
            with locking.op_lock(self.doc, "images"):     # run 串多阶段的嵌套形态
                pass
            # 内层退出后外层仍持有：外部（模拟另一进程的裸句柄）仍拿不到
            with self.assertRaises(KinemaError):
                locking.FileLock(self.doc.with_suffix(".json.oplock"),
                                 blocking=False).acquire()
        # 全部退出后可再获取
        with locking.op_lock(self.doc, "images"):
            pass

    def test_op_lock_conflict_names_holder(self):
        with locking.op_lock(self.doc, "gen_video"):
            with self.assertRaisesRegex(KinemaError, "gen_video"):
                # 绕开进程内重入登记，等价于另一进程的申请
                lock = locking._OpLock(self.doc, "images")
                locking._HELD.pop(locking._held_key(lock.path), None)
                with lock:
                    pass

    def test_op_lock_is_exclusive_across_threads(self):
        """重入只对同一线程成立：Studio 请求线程各自独立，第二个线程必须被拒。"""
        import threading
        entered = threading.Event()
        release = threading.Event()
        errors: list = []

        def holder():
            with locking.op_lock(self.doc, "rollback"):
                entered.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        try:
            self.assertTrue(entered.wait(5))
            try:
                with locking.op_lock(self.doc, "rollback"):
                    errors.append("第二个线程不得进入")
            except KinemaError:
                pass
        finally:
            release.set()
            t.join(5)
        self.assertEqual(errors, [])


class TestProjectMutate(unittest.TestCase):
    """表态写入的基线是磁盘现状：引擎长任务回填的字段不得被旧副本写回。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ch01.json"
        self.path.write_text(json.dumps(
            {"id": "p_ch01", "shots": [{"id": 1}]}, ensure_ascii=False),
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _disk(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_mutate_applies_on_fresh_disk_state(self):
        stale = Project.load(self.path)                    # 引擎风格的长持副本
        doc = self._disk()
        doc["shots"][0]["image"] = "images/shot_1.png"     # 引擎 checkpoint 回填
        self.path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        def fn(p):
            p.data["shots"][0].setdefault("review", {})["image"] = {"state": "done"}
            return {"ok": True}
        got = Project.mutate(self.path, fn)
        self.assertEqual(got, {"ok": True})
        disk = self._disk()
        self.assertEqual(disk["shots"][0]["image"], "images/shot_1.png",
                         "表态不得抹掉引擎已回填的产物字段")
        self.assertEqual(disk["shots"][0]["review"]["image"]["state"], "done")
        self.assertIsNotNone(stale)                        # 旧副本的存在不影响契约

    def test_mutate_retries_on_concurrent_write(self):
        calls = {"n": 0}

        def fn(p):
            if calls["n"] == 0:      # 首轮：竞争写者在读取与提交之间落盘
                doc = self._disk()
                doc["cost"] = {"image": 1.5}
                self.path.write_text(json.dumps(doc, ensure_ascii=False),
                                     encoding="utf-8")
            calls["n"] += 1
            p.data["audio_mode"] = "scored"
        Project.mutate(self.path, fn)
        disk = self._disk()
        self.assertEqual(calls["n"], 2, "基线变了必须以新基线重放")
        self.assertEqual(disk["cost"], {"image": 1.5}, "竞争写者的字段必须保留")
        self.assertEqual(disk["audio_mode"], "scored")

    def test_mutate_propagates_fn_error_without_writing(self):
        before = self.path.read_bytes()
        with self.assertRaises(KinemaError):
            Project.mutate(self.path, lambda p: (_ for _ in ()).throw(
                KinemaError("找不到评论")))
        self.assertEqual(self.path.read_bytes(), before)


class TestStageOpLockAdmission(unittest.TestCase):
    """阶段命令的准入：章节被占用时第二个操作在开工前失败，而不是交错写盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "ch01.json"
        self.doc.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_operation_fails_fast(self):
        holder = locking.FileLock(self.doc.with_suffix(".json.oplock"),
                                  blocking=False).acquire()   # 等价另一进程持有
        try:
            with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                with locking.op_lock(self.doc, "images"):
                    self.fail("持有中的章节不得进入第二个操作")
        finally:
            holder.release()

    def test_versions_rollback_refuses_while_op_held(self):
        """产物移动类直连命令与生成阶段同闸——被占章节在触碰文档前即失败。"""
        from types import SimpleNamespace

        from kinema import cli
        doc = Path(self.tmp.name) / "roll.json"
        doc.write_text(json.dumps({"id": "x", "shots": [{"id": 1}]}),
                       encoding="utf-8")
        before = doc.read_bytes()
        holder = locking.FileLock(doc.with_suffix(".json.oplock"),
                                  blocking=False).acquire()
        try:
            with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                cli.cmd_versions_rollback(SimpleNamespace(
                    project=str(doc), chapter=None, workspace=None,
                    shot="1", stage="image", to=1))
        finally:
            holder.release()
        self.assertEqual(doc.read_bytes(), before, "拒绝必须发生在任何写入之前")

    def test_editing_commands_refuse_while_op_held(self):
        """非表态编辑（批量改词、宫格点选、局改）与生成阶段同闸：作者字段不在
        save 的合并面，任务收尾写盘会把编辑写回旧值。"""
        from types import SimpleNamespace

        from kinema import cli
        doc = Path(self.tmp.name) / "edit.json"
        doc.write_text(json.dumps({"id": "x", "shots": [{"id": 1}]}),
                       encoding="utf-8")
        before = doc.read_bytes()
        holder = locking.FileLock(doc.with_suffix(".json.oplock"),
                                  blocking=False).acquire()
        try:
            for fn in (cli.cmd_batch_edit, cli.cmd_batch_undo, cli.cmd_pick,
                       cli.cmd_previz_register):
                with self.assertRaisesRegex(KinemaError, "已有操作在执行", msg=fn.__name__):
                    fn(SimpleNamespace(project=str(doc), chapter=None, workspace=None,
                                       shots="all", shot="1", use=1, keep_open=False,
                                       op=None, config=None))
            with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                cli.cmd_refine(SimpleNamespace(project=str(doc), chapter=None,
                                               workspace=None, shot="1", asset=None,
                                               rect=None, note="改", config=None,
                                               mock=True))
            with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                cli.cmd_transition(SimpleNamespace(project=str(doc), chapter=None,
                                                   workspace=None, taction="add", after="1",
                                                   text="一天后", asset=None, type=None,
                                                   edge=None, dur=None, direction=None,
                                                   color=None, sound=None, config=None))
        finally:
            holder.release()
        self.assertEqual(doc.read_bytes(), before)

    def test_statement_commands_write_through_mutate(self):
        """表态类命令（review set / verify 结论）按磁盘现状落盘：装载后引擎写入的字段不被旧副本抹掉。"""
        import json as _json
        from types import SimpleNamespace
        from unittest import mock

        from kinema import cli
        from kinema.pipeline import mediacheck
        doc = Path(self.tmp.name) / "stmt.json"
        doc.write_text(_json.dumps({"id": "x", "motion": "kenburns",
                                    "shots": [{"id": 1, "dur": 2.0}]}), encoding="utf-8")

        def racing_verify(project, store, **kw):
            d = _json.loads(doc.read_text(encoding="utf-8"))
            d["shots"][0]["image"] = "engine-wrote-this"   # 体检期间引擎回填
            doc.write_text(_json.dumps(d), encoding="utf-8")
            return {"at": "t", "16:9": {"ok": True, "todo": []}}
        with mock.patch.object(mediacheck, "verify", racing_verify), \
             mock.patch.object(cli, "ensure_tools", lambda: None), \
             mock.patch.object(cli, "_sub_cfg", lambda *a, **k: {"lang": "zh"}), \
             mock.patch("builtins.print"):
            cli.cmd_verify(SimpleNamespace(project=str(doc), chapter=None, workspace=None,
                                           config=None, profile=None, aspect=None,
                                           samples=2, json=False))
        d = _json.loads(doc.read_text(encoding="utf-8"))
        self.assertEqual(d["shots"][0]["image"], "engine-wrote-this")
        self.assertIn("verify", d)
        with mock.patch("builtins.print"):
            cli.cmd_review_set(SimpleNamespace(project=str(doc), chapter=None, workspace=None,
                                               config=None, stage="image", state="done",
                                               shots="1", note=None))
        d = _json.loads(doc.read_text(encoding="utf-8"))
        self.assertEqual(d["shots"][0]["review"]["image"]["state"], "done")
        self.assertEqual(d["shots"][0]["image"], "engine-wrote-this")

    def test_series_sync_refuses_while_chapter_op_held(self):
        """系列→章节传播（设定字段对齐、垫图、大纲、音色表）逐章过操作锁：这些键不在
        save 的合并面，与运行中的任务交错会被其收尾写盘回滚。"""
        from tests.support import LocalBackendEnv

        env = LocalBackendEnv()
        env.enable()
        try:
            from kinema.workspace import Workspace
            ws = Workspace.open(str(Path(self.tmp.name) / "ws3"))
            s = ws.create_project("传播锁测")
            s.create_chapter("第一章")
            cf = s.dir / "chapters" / "ch01.json"
            before = cf.read_bytes()
            holder = locking.FileLock(cf.with_suffix(".json.oplock"),
                                      blocking=False).acquire()
            try:
                s.data.setdefault("characters", []).append({"name": "甲", "appearance": "高"})
                for call in (s.sync_design_to_chapters, s.sync_moodboard,
                             lambda: s.upsert_chapter_outline("ch01", "大纲")):
                    with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                        call()
            finally:
                holder.release()
            self.assertEqual(cf.read_bytes(), before)
            s.sync_design_to_chapters()                     # 空闲：正常写入
            self.assertNotEqual(cf.read_bytes(), before)
        finally:
            env.restore()

    def test_studio_edit_refuses_while_op_held(self):
        from tests.support import LocalBackendEnv

        env = LocalBackendEnv()
        env.enable()
        try:
            from kinema.studio import actions
            from kinema.workspace import Workspace
            ws = Workspace.open(str(Path(self.tmp.name) / "ws2"))
            s = ws.create_project("编辑锁测")
            s.create_chapter("第一章")
            cf = s.dir / "chapters" / "ch01.json"
            holder = locking.FileLock(cf.with_suffix(".json.oplock"),
                                      blocking=False).acquire()
            try:
                for call in (lambda: actions.transition_add(ws.root, s.pid, "ch01", after=1),
                             lambda: actions.set_effects(ws.root, s.pid, "ch01", effects=[]),
                             lambda: actions.set_shot_refs(ws.root, s.pid, "ch01", shot=1,
                                                           refs=[])):
                    with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                        call()
            finally:
                holder.release()
            actions.set_effects(ws.root, s.pid, "ch01", effects=[])   # 空闲：正常写入
        finally:
            env.restore()

    def test_studio_exclusive_refuses_while_op_held(self):
        from tests.support import LocalBackendEnv

        env = LocalBackendEnv()
        env.enable()
        try:
            from kinema.studio import actions
            from kinema.workspace import Workspace
            ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
            s = ws.create_project("锁测")
            s.create_chapter("第一章")
            cf = s.dir / "chapters" / "ch01.json"
            holder = locking.FileLock(cf.with_suffix(".json.oplock"),
                                      blocking=False).acquire()
            try:
                with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                    with actions._exclusive(ws.root, s.pid, "ch01", "rollback"):
                        self.fail("被占章节不得进入产物移动动作")
                for call in (lambda: actions.previz_clear(ws.root, s.pid, "ch01", shot=1),
                             lambda: actions.sketch_clear(ws.root, s.pid, "ch01", shot=1)):
                    with self.assertRaisesRegex(KinemaError, "已有操作在执行"):
                        call()
            finally:
                holder.release()
        finally:
            env.restore()


if __name__ == "__main__":
    unittest.main()
