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

"""Studio「实发提示词」预览守卫：页面显示的必须是实发的那一句。

三条不变量：
  · **同一条编译路径**——preview 是 `stage_gen_video` dry-run 的收集模式，不是
    另写一份拼装（scanner/前端若自拼展示，与 PromptEnvelope 必然分叉，用户照
    页面调整、实发却是另一句）；
  · **零落盘**——preview 拿到的是用后即弃的文档副本，孤岛接缝只做同拓扑内存
    计算、绝不 `project.save()`，镜级闸（角色锚缺失）降级为注记不拦断；
  · **端到端同源**——scanner 端点走 `cli.video_prompt_preview`，前端按钮打
    `/api/video-preview`，三层各自另起炉灶中的任何一层都是回归。
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import cli
from kinema.models import ConfigStore, ModelRouter
from kinema.project import Project
from tests.support import LocalBackendEnv


def _native_chapter(tmp: Path) -> Project:
    """临时 native 章节（**不落盘**：cf 指向的文件刻意不存在，save 一旦发生
    就会在断言里现形）。陈昭已选角、林晚未选角，双句对白铺台词时间轴。"""
    cf = tmp / "proj" / "chapters" / "ch01.json"
    return Project(cf, {
        "profile": "narration", "motion": "native",
        "voices": {"陈昭": "v_a"},
        "shots": [{"id": 1, "dur": 6, "video_prompt": "对峙，缓推。",
                   "lines": [{"speaker": "陈昭", "emotion": "angry",
                              "text": "你早就知道了。"},
                             {"speaker": "林晚", "text": "我不知道！"}]}]})


class TestVideoPromptPreview(unittest.TestCase):
    def _rows(self, project):
        store = ConfigStore.load(None)
        router = ModelRouter(store, force_mock=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows = cli.video_prompt_preview(project, store, router)
        return rows, buf.getvalue()

    def test_preview_carries_the_full_compiled_prompt(self):
        """情绪动词、台词、音色绑定都在**同一份**编译产物里——
        分镜卡若只显示作者字段，看不见的正是这一半。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _native_chapter(Path(d))
            rows, _out = self._rows(p)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertNotIn("台词时间轴：", r["prompt"])
        self.assertIn("陈昭 @配音1 嘶声怒喝道：“你早就知道了。”", r["prompt"])
        self.assertIn("陈昭的说话音色以所附参考音频为准", r["prompt"])
        self.assertEqual([a["who"] for a in r["anchors"]], ["陈昭"])
        self.assertTrue(r["anchors"][0]["pending"], "锚定音未预热要照实标注")
        self.assertEqual(r["loose"], ["林晚"])
        self.assertTrue(r["note"].startswith("▸ 镜1"))
        self.assertIn("fingerprint", r)
        # 中英对照：另一语种由引擎同源编译（展示用，实发恒主语种）
        self.assertEqual(r["lang"], "zh")
        self.assertEqual(r["alt"]["lang"], "en")
        self.assertTrue(r["alt"]["positive"], "英文对照稿必须生成")

    def test_preview_never_writes_and_never_prints_the_review_listing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _native_chapter(Path(d))
            cf = p.path
            _rows, out = self._rows(p)
            self.assertFalse(cf.exists(),
                             "preview 落盘=对用户章节的静默写入（孤岛接缝那条 save）")
        self.assertNotIn("提示词审阅", out, "逐镜清单打印只属于 --dry-run")
        self.assertNotIn("▸ 镜", out)

    def test_registered_subject_kind_does_not_add_unprovided_body_parts(self):
        import tempfile

        for kind, body, body_en, unwanted, unwanted_en in (
            ("robot", "光滑密闭的球形机器人转身。", "A sealed spherical robot turns.",
             "线缆与外挂配重", "cables and hanging counterweights"),
            ("animal", "没有毛发的斯芬克斯猫向前走两步。", "A hairless sphynx cat takes two steps.",
             "毛发随动作", "fur following"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                p = _native_chapter(Path(d))
                p.data["characters"] = [{"name": "主体", "subject_kind": kind}]
                p.shots[0].update(characters=["主体"], lines=[],
                                  video_prompt=body, video_prompt_en=body_en)
                rows, _ = self._rows(p)
                self.assertIn(body, rows[0]["prompt"])
                self.assertNotIn(unwanted, rows[0]["prompt"])
                self.assertIn(body_en, rows[0]["alt"]["positive"])
                self.assertNotIn(unwanted_en, rows[0]["alt"]["positive"])

    def test_image_preview_compiles_the_real_prompt(self):
        """IMAGE 也走实发：编译产物（风格前缀/负面地板随行）而非作者字段原文。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _native_chapter(Path(d))
            p.data["shots"][0]["image_prompt"] = "陈昭抬头看向镜头"
            store = ConfigStore.load(None)
            router = ModelRouter(store, force_mock=True)
            with contextlib.redirect_stdout(io.StringIO()):
                rows = cli.image_prompt_preview(p, store, router)
            self.assertFalse(p.path.exists(), "image 预览同样零落盘")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIn("陈昭抬头看向镜头", r["prompt"])
        self.assertNotEqual(r["prompt"], "陈昭抬头看向镜头",
                            "必须是编译产物而非作者字段原文")
        self.assertTrue(r["negative"], "防字地板等负面约束必须随行")
        self.assertIn("fingerprint", r)
        self.assertEqual(r["alt"]["lang"], "en")
        self.assertTrue(r["alt"]["negative"], "英文对照的负面地板同样要生成")

    def test_approval_lock_states_flow_from_the_prompt_sha(self):
        """审阅锁三态：通过后 ok；改动任何进稿字段后 stale——真发闸按同一 sha
        口径对 stale 镜跳过点名（见下一条源级钉）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _native_chapter(Path(d))
            rows, _ = self._rows(p)
            self.assertIsNone(rows[0]["approval"])
            sha = rows[0]["prompt_sha"]
            p.data["shots"][0].setdefault("gen", {})["clip_approval"] = {
                "sha": sha, "at": "t0"}
            rows2, _ = self._rows(p)
            self.assertEqual(rows2[0]["approval"], "ok")
            p.data["shots"][0]["lines"][0]["text"] = "改过的台词。"
            rows3, _ = self._rows(p)
            self.assertEqual(rows3[0]["approval"], "stale")

    def test_the_send_gate_shares_the_sha_and_skips_stale(self):
        import inspect
        src = inspect.getsource(cli.stage_gen_video)
        # 三处消费同一 sha 口径各恰好一次：预览行、真发闸、降级轮重编稿的复检
        # ——降级稿的提示词变了，审阅过的镜不许把没审过的稿静默发出去
        self.assertEqual(src.count("_prompt_sha("), 3,
                         "预览行/真发闸/降级轮复检必须各调同一 sha 口径恰好一次")
        self.assertIn("实发稿与审阅版不一致", src)
        self.assertIn("降级稿与审阅版不一致", src)
        self.assertIn("clip_approval", inspect.getsource(cli))

    def test_the_three_layers_share_one_source(self):
        import inspect

        from kinema.studio import scanner, server
        src = inspect.getsource(scanner.video_preview)
        self.assertIn("--preview-json", src,
                      "端点必须走 CLI 结构化出口（jobs 同款子进程范式）")
        self.assertIn("gen-image", src, "生图预览与视频预览同端点并行取数")
        self.assertIn("preview_json", inspect.getsource(cli._stage_wrapper),
                      "CLI 侧必须有 --preview-json 分流（零落盘零锁）")
        self.assertIn("/api/video-preview", inspect.getsource(server))
        js = (Path(__file__).resolve().parents[1]
              / "kinema/studio_app/app/chapter.js").read_text(encoding="utf-8")
        self.assertIn("/api/video-preview", js)
        self.assertIn("实发提示词", js)

    def test_preview_mode_returns_before_the_ledger_write(self):
        """dry-run 会把预估写进 cost_estimate 并 save；preview 必须在此之前返回。"""
        import inspect
        src = inspect.getsource(cli.stage_gen_video)
        self.assertIn("if preview_sink is not None:\n            return", src)


class TestPreviewRuntimeOptions(unittest.TestCase):
    def setUp(self):
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = _native_chapter(Path(tmp.name))
        self.project.data["shots"] = [
            {"id": n, "dur": 6, "image_prompt": "人物走向门口",
             "sketch": {"beats": [{"t": "0-3s", "action": "起跑"},
                                  {"t": "3-6s", "action": "跑出画面"}]}}
            for n in (1, 2)]
        self.project.path.parent.mkdir(parents=True)
        self.project.save()
        self.original = self.project.path.read_bytes()
        self.store = ConfigStore.load(None)

    def _preview(self, command="gen-video", *flags):
        args = cli.build_parser().parse_args(
            [command, "--project", str(self.project.path), "--preview-json", *flags])
        output = io.StringIO()
        with mock.patch.object(ConfigStore, "load", return_value=self.store), \
             mock.patch.object(Project, "save", side_effect=AssertionError("preview saved")), \
             mock.patch("kinema.locking.op_lock", side_effect=AssertionError("preview locked")), \
             mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(output):
            args.func(args)
        self.assertEqual(self.project.path.read_bytes(), self.original)
        return json.loads(output.getvalue().splitlines()[-1])

    def test_model_resolution_and_timing_follow_cli_overrides(self):
        base = self._preview()
        rows = self._preview("gen-video", "--only", "1", "--video-provider", "seedance-2.5",
                             "--resolution", "1080p", "--dry-run")
        self.assertEqual([row["id"] for row in rows], [1])
        self.assertEqual(rows[0]["model"], self.store.provider_conn("seedance-2.5")["model"])
        self.assertEqual(rows[0]["resolution"], "1080p")
        self.assertIn("第0-3秒", rows[0]["prompt"])
        self.assertIn("第1段", base[0]["prompt"])
        self.assertEqual(self._preview(), base, "运行时覆盖不能污染后续默认预览")

    def test_profile_and_anchor_override_change_the_actual_prompt(self):
        self.store.data["profiles"]["anime"]["video"] = {"provider": "seedance-2.5"}
        rows = self._preview("gen-video", "--profile", "anime", "--anchor-frame")
        self.assertEqual(rows[0]["model"], self.store.provider_conn("seedance-2.5")["model"])
        self.assertIn("首帧锚定", rows[0]["note"])
        self.assertIn("以所给首帧", rows[0]["prompt"])
        self.assertEqual(rows[0]["refs"], [])

    def test_image_preview_also_honors_runtime_profile(self):
        self.store.data["profiles"]["anime"]["image"]["style_prefix"] = "水彩线条风格，"
        rows = self._preview("gen-image", "--profile", "anime", "--only", "2", "--mock")
        self.assertEqual([row["id"] for row in rows], [2])
        self.assertIn("水彩线条风格", rows[0]["prompt"])

    def test_previz_override_attaches_the_motion_reference(self):
        video = self.project.path.parent / "previz.mp4"
        video.write_bytes(b"offline reference")
        self.project.shots[0].update(previz=str(video), guide="previz",
                                     gen={"previz": {"duration": 6}})
        self.project.save()
        self.original = self.project.path.read_bytes()
        self.assertEqual(self._preview()[0]["videos"], [])
        row = self._preview("gen-video", "--previz", "--only", "1")[0]
        self.assertEqual(row["videos"], [{"no": 1, "kind": "previz",
                                         "path": str(video), "seconds": 6}])
        self.assertNotIn("第1段", row["prompt"])


if __name__ == "__main__":
    unittest.main()
