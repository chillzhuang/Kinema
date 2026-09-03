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

"""kinema.pipeline.versioning 单元测试：归档（移动+自增）与回滚（归档不可变、可反复）。"""
from __future__ import annotations

import os
import tempfile
import unittest

from kinema.errors import ProjectError
from pathlib import Path

from kinema.pipeline import versioning
from tests.support import FakeProject


class TestVersioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = FakeProject(self.tmp.name)
        self.images = self.project.subdir("images")

    def tearDown(self):
        self.tmp.cleanup()

    def _canvas(self, content: str) -> Path:
        p = self.images / "shot_s1.png"
        p.write_text(content)
        return p

    def test_stages_enum(self):
        self.assertEqual(versioning.STAGES, ("image", "audio", "clip"))

    def test_first_generation_archives_nothing(self):
        shot = {"id": "s1"}                                  # 无现存产物
        self.assertIsNone(versioning.archive(self.project, shot, "image"))
        self.assertEqual(versioning.history(shot, "image"), [])
        self.assertEqual(versioning.current_version(shot, "image"), 1)

    def test_archive_moves_files_and_increments_version(self):
        # 引擎真实产出形状：主字段恒取自逐比例字典（同一字符串），只有另一比例
        # 才是独立文件——「主字段与主比例各是一个文件」的形状引擎产不出来，
        # 用它当夹具会让去重不变量失去守卫
        canvas = self._canvas("v1-main")
        aspect = self.images / "shot_s1_9x16.png"
        aspect.write_text("v1-aspect")
        shot = {"id": "s1", "image": str(canvas),
                "images": {"16:9": str(canvas), "9:16": str(aspect)}}

        v = versioning.archive(self.project, shot, "image",
                               reason="retake: 左手穿模", params={"cost": 0.3})
        self.assertEqual(v, 1)
        self.assertFalse(canvas.exists())                    # 移动而非复制：原位不留副本
        self.assertFalse(aspect.exists())
        entry = versioning.history(shot, "image")[0]
        self.assertEqual(entry["v"], 1)
        self.assertEqual(entry["reason"], "retake: 左手穿模")
        self.assertEqual(entry["params"], {"cost": 0.3})
        # 归档命名：main 无标签，比例键的冒号替换为 x；
        # 与 main 共指同一文件的比例键只归档一次、不产生条目键
        self.assertTrue(entry["files"]["main"].endswith("shot_s1_image_v001.png"))
        self.assertTrue(entry["files"]["9:16"].endswith("shot_s1_image_9x16_v001.png"))
        self.assertNotIn("16:9", entry["files"])
        self.assertEqual(Path(entry["files"]["main"]).read_text(), "v1-main")
        self.assertEqual(versioning.current_version(shot, "image"), 2)

        # 再生成后二次归档 → 版本号自增到 v2
        self._canvas("v2-main")
        aspect.write_text("v2-aspect")
        self.assertEqual(versioning.archive(self.project, shot, "image"), 2)
        self.assertEqual(versioning.current_version(shot, "image"), 3)

    def test_archive_clip_dedupes_shared_path(self):
        """clip 的引擎产出形状：`s["clip"]` 恒取自 `clips[主比例]`，同一文件以
        两个键出现——归档必须只搬一次且条目落账；对它 move 两次会崩在登记
        之前，clip 版本栈永远是空的、rollback 无从捞回。"""
        clip = self.images / "shot_s1_16x9.mp4"
        clip.write_text("clip-v1")
        shot = {"id": "s1", "clip": str(clip), "clips": {"16:9": str(clip)}}
        v = versioning.archive(self.project, shot, "clip")
        self.assertEqual(v, 1)
        self.assertFalse(clip.exists())
        entry = versioning.history(shot, "clip")[0]
        self.assertEqual(list(entry["files"]), ["main"])
        self.assertEqual(Path(entry["files"]["main"]).read_text(), "clip-v1")

    def test_rollback_clip_with_shared_path(self):
        """同形状的回滚：内容经 main 键恢复，clip/clips 字段仍指画布路径不悬挂。"""
        clip = self.images / "shot_s1_16x9.mp4"
        clip.write_text("A")
        shot = {"id": "s1", "clip": str(clip), "clips": {"16:9": str(clip)}}
        versioning.archive(self.project, shot, "clip")       # v1 = A
        clip.write_text("B")                                 # 模拟重生成
        versioning.rollback(self.project, shot, "clip", 1)
        self.assertEqual(clip.read_text(), "A")
        self.assertEqual(shot["clip"], str(clip))
        self.assertEqual(shot["clips"]["16:9"], str(clip))

    def test_rollback_restores_target_and_archives_current(self):
        canvas = self._canvas("A")
        shot = {"id": "s1", "image": str(canvas)}
        versioning.archive(self.project, shot, "image")      # v1 = A
        self._canvas("B")                                    # 模拟重生成
        versioning.rollback(self.project, shot, "image", 1)
        self.assertEqual(canvas.read_text(), "A")            # 画布回到 v1 内容
        hist = versioning.history(shot, "image")
        self.assertEqual([e["v"] for e in hist], [1, 2])     # 当前版(B)先归档为 v2
        self.assertIn("rollback-out", hist[1]["reason"])
        # 归档不可变：v1 文件仍在（回滚是拷贝不是移动）
        self.assertTrue(Path(hist[0]["files"]["main"]).is_file())

    def test_rollback_swaps_generation_snapshot(self):
        """gen 快照描述的是画布上那一版：回滚拷回 v1 文件时，`gen.image` 也要换回 v1 的
        生成参数，`explain`/Studio 的「实发提示词」才对得上画面；当前版的参数随归档入栈。"""
        canvas = self._canvas("A")
        shot = {"id": "s1", "image": str(canvas), "gen": {"image": {"prompt": "A 稿"}}}
        versioning.archive(self.project, shot, "image", params=shot["gen"]["image"])
        self._canvas("B")
        shot["gen"]["image"] = {"prompt": "B 稿"}
        versioning.rollback(self.project, shot, "image", 1)
        self.assertEqual(shot["gen"]["image"], {"prompt": "A 稿"})
        hist = versioning.history(shot, "image")
        self.assertEqual(hist[1]["params"], {"prompt": "B 稿"})
        versioning.rollback(self.project, shot, "image", 2)
        self.assertEqual(shot["gen"]["image"], {"prompt": "B 稿"})
        shot["versions"]["image"][0].pop("params")            # 目标版无留痕 → 快照摘除
        versioning.rollback(self.project, shot, "image", 1)
        self.assertNotIn("image", shot["gen"])

    def test_rollback_is_repeatable(self):
        canvas = self._canvas("A")
        shot = {"id": "s1", "image": str(canvas)}
        versioning.archive(self.project, shot, "image")      # v1 = A
        self._canvas("B")
        versioning.rollback(self.project, shot, "image", 1)  # 画布=A，v2=B
        versioning.rollback(self.project, shot, "image", 2)  # 再滚回 B
        self.assertEqual(canvas.read_text(), "B")
        hist = versioning.history(shot, "image")
        self.assertEqual([e["v"] for e in hist], [1, 2, 3])  # 版本号只增不减
        self.assertEqual(Path(hist[0]["files"]["main"]).read_text(), "A")
        self.assertEqual(Path(hist[1]["files"]["main"]).read_text(), "B")

    def test_rollback_unknown_version_raises(self):
        canvas = self._canvas("A")
        shot = {"id": "s1", "image": str(canvas)}
        versioning.archive(self.project, shot, "image")
        self._canvas("B")
        with self.assertRaises(ProjectError):
            versioning.rollback(self.project, shot, "image", 99)

    def test_rollback_without_current_artifact_raises(self):
        canvas = self._canvas("A")
        shot = {"id": "s1", "image": str(canvas)}
        versioning.archive(self.project, shot, "image")      # 画布已被移走且未重生成
        with self.assertRaises(ProjectError):
            versioning.rollback(self.project, shot, "image", 1)


class TestOutputVersionStack(unittest.TestCase):
    """成片版本栈：合成覆盖前归档、逐比例各一支谱系、回滚是互换、失败可回填。

    成片是全链最贵的产物（图 + 配音 + 视频 + 算力都烘焙在里面），而 `compose.build`
    写的是同一个输出路径——没有这层归档，重合成一次上一版就永久没了。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = FakeProject(self.tmp.name, {})
        self.out = Path(self.tmp.name) / "output"
        self.out.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _film(self, aspect="16:9", body=b"v1"):
        f = self.out / f"film_{aspect.replace(':', 'x')}.mp4"
        f.write_bytes(body)
        self.p.data.setdefault("output", {})[aspect] = str(f)
        return f

    def test_first_compose_has_nothing_to_archive(self):
        self.assertIsNone(versioning.archive_output(self.p, "16:9"))
        self.assertEqual(versioning.output_current_version(self.p, "16:9"), 1)

    def test_archive_moves_and_numbers(self):
        f = self._film(body=b"first")
        dst = versioning.archive_output(self.p, "16:9", reason="重新合成")
        self.assertTrue(Path(dst).is_file())
        self.assertFalse(f.is_file(), "归档是移动，标准路径应腾空给下一次合成")
        self.assertEqual(Path(dst).read_bytes(), b"first")
        self.assertEqual(versioning.output_current_version(self.p, "16:9"), 2)
        self.assertEqual(versioning.output_history(self.p, "16:9")[0]["reason"], "重新合成")

    def test_aspects_keep_separate_lineages(self):
        """16:9 与 9:16 是两支不同的片子——混进一张表就没法按比例回滚。"""
        self._film("16:9", b"h"); self._film("9:16", b"v")
        versioning.archive_output(self.p, "16:9")
        self.assertEqual(len(versioning.output_history(self.p, "16:9")), 1)
        self.assertEqual(versioning.output_history(self.p, "9:16"), [])

    def test_rollback_swaps_content_and_archives_current(self):
        f = self._film(body=b"first")
        versioning.archive_output(self.p, "16:9")
        f.write_bytes(b"second")                       # 模拟新一次合成写回标准路径
        versioning.rollback_output(self.p, "16:9", 1)
        self.assertEqual(f.read_bytes(), b"first", "回滚必须真的换内容，不只是改字段")
        hist = versioning.output_history(self.p, "16:9")
        self.assertEqual(len(hist), 2)
        self.assertIn("rollback-out", hist[-1]["reason"], "原当前版要进栈，回滚才可逆")
        self.assertEqual(Path(hist[-1]["file"]).read_bytes(), b"second")

    def test_rollback_to_unknown_version_raises(self):
        self._film()
        with self.assertRaises(ProjectError):
            versioning.rollback_output(self.p, "16:9", 9)

    def test_rollback_without_current_film_raises(self):
        with self.assertRaises(ProjectError):
            versioning.rollback_output(self.p, "16:9", 1)

    def test_failed_compose_restores_the_archived_film(self):
        """归档后合成失败 → 标准路径空着，而它是水印/校验/交付三条链的入口。"""
        f = self._film(body=b"first")
        versioning.archive_output(self.p, "16:9")
        self.assertFalse(f.is_file())
        back = versioning.restore_last_output(self.p, "16:9")
        self.assertEqual(Path(back).read_bytes(), b"first")
        self.assertEqual(versioning.output_history(self.p, "16:9"), [],
                         "回填要连条目一起撤销，否则谱系里留一条指向空文件的鬼影")

    def test_restore_is_a_noop_without_history(self):
        self._film()
        self.assertIsNone(versioning.restore_last_output(self.p, "16:9"))



class TestArchivePathsAreAbsolute(unittest.TestCase):
    """归档条目必须落**绝对路径**。

    条目会被后来的回滚按原样取用，而那时的工作目录未必还是归档时那个——Studio 线程、
    spawn 出去的子进程、从别处调起的 CLI 各有各的 cwd。存相对路径的后果实测过：回滚当场
    `FileNotFoundError: 归档文件丢失`，而文件好端端躺在盘上，谁也想不到是 cwd 的问题。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)                    # 用相对路径构造，模拟事故现场
        self.p = FakeProject("ws", {})

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_shot_archive_records_absolute(self):
        shot = {"id": 1}
        img = Path("ws") / "shot_1.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"x")
        shot["image"] = str(img)                   # 相对路径入场
        versioning.archive(self.p, shot, "image", reason="t")
        rec = versioning.history(shot, "image")[0]["files"]["main"]
        self.assertTrue(Path(rec).is_absolute(), f"归档路径必须绝对: {rec}")
        self.assertTrue(Path(rec).is_file())

    def test_asset_archive_records_absolute(self):
        f = Path("ws") / "sheet.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        holder = {"sheet": str(f)}                 # 相对路径入场
        versioning.archive_asset(holder, media_key="sheet", versions_key="versions",
                                 vdir=Path("ws") / "versions", label="sheet", reason="t")
        rec = holder["versions"][0]["file"]
        self.assertTrue(Path(rec).is_absolute(), f"归档路径必须绝对: {rec}")
        self.assertTrue(Path(rec).is_file())

    def test_rollback_survives_a_cwd_change(self):
        """归档后换个工作目录再回滚——这正是事故的复现路径。"""
        f = Path("ws") / "sheet.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"old")
        holder = {"sheet": str(f.resolve())}
        vdir = (Path("ws") / "versions").resolve()
        versioning.archive_asset(holder, media_key="sheet", versions_key="versions",
                                 vdir=vdir, label="sheet")
        f.write_bytes(b"new")
        os.chdir(self.cwd)                         # 换 cwd
        versioning.rollback_asset(holder, media_key="sheet", versions_key="versions",
                                  vdir=vdir, label="sheet", to_v=1)
        self.assertEqual(Path(holder["sheet"]).read_bytes(), b"old")


class TestRollbackTargetFirst(unittest.TestCase):
    """回滚必须先解析并校验目标、再归档当前版。

    顺序相反时目标缺失的回滚会把当前版移进版本栈后才失败：画布落空、
    rollback-out 条目又随异常丢失，现场既不可见也不可恢复。
    历史条目可能已被 oss sync 改写为 URL——读侧必须经 ensure_local 落地。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = FakeProject(self.tmp.name)
        self.images = self.project.subdir("images")

    def tearDown(self):
        self.tmp.cleanup()

    def _shot_with_history(self):
        canvas = self.images / "shot_s1.png"
        canvas.write_text("v1")
        shot = {"id": "s1", "image": str(canvas)}
        versioning.archive(self.project, shot, "image")
        canvas.write_text("v2")
        shot["image"] = str(canvas)
        return shot, canvas

    def test_missing_target_leaves_scene_untouched(self):
        shot, canvas = self._shot_with_history()
        Path(versioning.history(shot, "image")[0]["files"]["main"]).unlink()
        with self.assertRaises(FileNotFoundError):
            versioning.rollback(self.project, shot, "image", 1)
        self.assertEqual(canvas.read_text(), "v2")
        self.assertEqual(len(versioning.history(shot, "image")), 1,
                         "目标缺失时不得追加 rollback-out 条目")

    def test_url_history_resolves_before_archive(self):
        from unittest.mock import patch
        shot, canvas = self._shot_with_history()
        entry = versioning.history(shot, "image")[0]
        real = entry["files"]["main"]
        entry["files"]["main"] = "https://oss.example/kn/history_v001.png"
        with patch("kinema.storage.media.ensure_local",
                   side_effect=lambda v: real if str(v).startswith("http") else v):
            versioning.rollback(self.project, shot, "image", 1)
        self.assertEqual(canvas.read_text(), "v1")
        self.assertEqual(len(versioning.history(shot, "image")), 2)

    def test_asset_url_target_resolves(self):
        from unittest.mock import patch
        std = self.images / "sheet.png"
        std.write_text("old")
        holder = {"sheet": str(std)}
        vdir = self.project.subdir("versions")
        versioning.archive_asset(holder, media_key="sheet", versions_key="versions",
                                 vdir=vdir, label="char_x")
        std.write_text("new")
        real = holder["versions"][0]["file"]
        holder["versions"][0]["file"] = "https://oss.example/kn/char_x_v001.png"
        with patch("kinema.storage.media.ensure_local",
                   side_effect=lambda v: real if str(v).startswith("http") else v):
            versioning.rollback_asset(holder, media_key="sheet", versions_key="versions",
                                      vdir=vdir, label="char_x", to_v=1)
        self.assertEqual(std.read_text(), "old")

    def test_restore_last_output_resolves_url(self):
        from unittest.mock import patch
        out = self.images / "final.mp4"
        out.write_text("old-cut")
        self.project.data["output"] = {"16:9": str(out)}
        versioning.archive_output(self.project, "16:9")
        real = self.project.data["output_versions"]["16:9"][0]["file"]
        self.project.data["output_versions"]["16:9"][0]["file"] = \
            "https://oss.example/kn/final_v001.mp4"
        with patch("kinema.storage.media.ensure_local",
                   side_effect=lambda v: real if str(v).startswith("http") else v):
            got = versioning.restore_last_output(self.project, "16:9")
        self.assertEqual(got, str(out))
        self.assertEqual(out.read_text(), "old-cut")


class TestRegenGateDefersArchive(unittest.TestCase):
    """`_regen_gate` 只做状态机判定；归档由 `_archive_regen` 在整批计划成功后执行。

    闸门在计划循环内归档时，后续任一镜的 provider 解析失败都会留下
    「旧产物已移走、版本条目未落盘」的中间态，且重跑会以同号覆盖孤儿归档。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = FakeProject(self.tmp.name)
        self.images = self.project.subdir("images")

    def tearDown(self):
        self.tmp.cleanup()

    def _retake_shot(self, sid: str):
        from kinema import review
        canvas = self.images / f"shot_{sid}.png"
        canvas.write_text("current")
        shot = {"id": sid, "image": str(canvas)}
        review.set_state(shot, "image", "retake", note="左手穿模")
        return shot, canvas

    def test_gate_is_read_only(self):
        from kinema import cli
        shot, canvas = self._retake_shot("s1")
        skip, regen = cli._regen_gate(self.project, shot, "image", False)
        self.assertEqual((skip, regen), (False, True))
        self.assertTrue(canvas.is_file(), "闸门不得移动产物")
        self.assertEqual(versioning.history(shot, "image"), [],
                         "闸门不得写版本条目")

    def test_archive_regen_archives_only_regen_items(self):
        from kinema import cli
        s1, c1 = self._retake_shot("s1")
        s2 = {"id": "s2", "image": str(self.images / "shot_s2.png")}
        Path(s2["image"]).write_text("keep")
        cli._archive_regen(self.project,
                           [{"shot": s1, "regen": True},
                            {"shot": s2, "regen": False}], "image")
        self.assertFalse(c1.is_file())
        self.assertEqual(len(versioning.history(s1, "image")), 1)
        self.assertIn("左手穿模", versioning.history(s1, "image")[0]["reason"])
        self.assertTrue(Path(s2["image"]).is_file())
        self.assertEqual(versioning.history(s2, "image"), [])


if __name__ == "__main__":
    unittest.main()
