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
import unittest
from pathlib import Path

from kinema import cli
from kinema.models import ConfigStore, ModelRouter
from kinema.project import Project


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
        """时间轴、情绪动词、台词、音色绑定都在**同一份**编译产物里——
        分镜卡若只显示作者字段，看不见的正是这一半。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _native_chapter(Path(d))
            rows, _out = self._rows(p)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIn("台词时间轴：", r["prompt"])
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


if __name__ == "__main__":
    unittest.main()
