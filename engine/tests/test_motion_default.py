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

"""gen-video 的渲染模式决策点守卫。

产物只有 dubbed/native 两种消费口径：kenburns 下合成按分镜图渲染，买回的片段
不参与出片。故 gen-video 入口对渲染模式收口——从未表态的章节按内容定档
（`project.default_motion`：有对白落 native、全旁白落 dubbed、scored 落 native）
并播报，真发落盘表态、只读动作不写章节；显式 kenburns 拒发。
守住的是「计费口径与合成口径必须同源」。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock

from pathlib import Path

from kinema.errors import ProjectError
from kinema.project import Project
from tests.support import LocalBackendEnv


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082"))
    return path


class _Case(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.img = str(_png(self.tmp / "shot1.png"))

    def tearDown(self):
        self._tmp.cleanup()
        self.env.restore()

    def _project(self, **over) -> tuple[Project, Path]:
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "aspect": "16:9", "voiceover": "lead",
               "shots": [{"id": 1, "dur": 4.0, "narration": "旁白一句。",
                          "video_prompt": "缓推", "image": self.img,
                          "images": {"16:9": self.img}}]}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        p = Project.load(cf)
        adir = p.subdir("audio")
        (adir / "shot_1.wav").write_bytes(b"RIFFxxxxWAVE")
        return p, cf

    def _run(self, project, *, dry_run=True):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video import mock as vmock
        store = ConfigStore.load(None)
        buf = io.StringIO()
        orig = vmock.MockVideoProvider.generate

        def fake(prov, image, out_path, **kw):
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=True,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = fake
        try:
            with unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=4.0):
                with contextlib.redirect_stdout(buf):
                    stage_gen_video(project, store,
                                    ModelRouter(store, force_mock=True),
                                    dry_run=dry_run)
        finally:
            vmock.MockVideoProvider.generate = orig
        return buf.getvalue()


class TestUndeclaredDefault(_Case):
    """从未表态的章节：全旁白夹具缺省 dubbed，播报恒在；真发落盘、dry-run 不落盘。"""

    def test_dry_run_announces_without_persisting(self):
        p, cf = self._project()
        out = self._run(p, dry_run=True)
        self.assertIn("按缺省 dubbed", out)
        self.assertIn("不写章节", out, "只读动作的播报必须讲明不落盘")
        self.assertIn("[dubbed]", out, "报价口径必须按缺省档走，不得按 kenburns 折算")
        self.assertNotIn("motion", json.loads(cf.read_text(encoding="utf-8")),
                         "dry-run 是只读审阅动作，不得替章节表态")

    def test_real_send_persists_the_declaration(self):
        p, cf = self._project()
        out = self._run(p, dry_run=False)
        self.assertIn("已写入章节 motion", out)
        ondisk = json.loads(cf.read_text(encoding="utf-8"))
        self.assertEqual(ondisk.get("motion"), "dubbed",
                         "真发必须落盘表态，否则 assemble 仍按 kenburns 弃用片段")
        self.assertTrue(ondisk["shots"][0].get("clips"), "片段照常产出")

    def test_scored_project_defaults_to_native(self):
        p, _ = self._project(audio_mode="scored")
        out = self._run(p, dry_run=True)
        self.assertIn("按缺省 native", out,
                      "scored 的人声整轨生成、与对口型互斥——缺省不得撞上硬闸")


class TestDeclaredUntouched(_Case):
    """已表态的章节：显式值恒不被覆盖，显式 kenburns 拒发。"""

    def test_explicit_kenburns_is_refused(self):
        p, cf = self._project(motion="kenburns")
        with self.assertRaises(ProjectError) as ctx:
            self._run(p, dry_run=True)
        self.assertIn("不参与出片", str(ctx.exception))
        self.assertIn("-m c", str(ctx.exception), "拒发必须给出改道路径")
        self.assertNotIn('"motion": "dubbed"', cf.read_text(encoding="utf-8"))

    def test_explicit_native_gets_no_announcement(self):
        p, _ = self._project(motion="native")
        out = self._run(p, dry_run=True)
        self.assertNotIn("未指定渲染模式", out)
        self.assertIn("[native]", out)

    def test_runtime_override_counts_as_declared(self):
        p, cf = self._project()
        p.override_runtime("motion", "dubbed")   # 等价于 CLI 的 -m c
        out = self._run(p, dry_run=True)
        self.assertNotIn("未指定渲染模式", out,
                         "flag 已表态，再播缺省即口径重复")
        self.assertNotIn("motion", json.loads(cf.read_text(encoding="utf-8")))

    def test_runtime_override_persists_on_real_send(self):
        """未表态章节带 -m 真发：flag 值升格为章节表态。不落盘的话 save 会把它还原，
        随后不带 flag 的 assemble/verify 按另一个档位出片，买来的片段被弃用。"""
        p, cf = self._project()
        p.override_runtime("motion", "native")
        out = self._run(p, dry_run=False)
        self.assertIn("已写入章节 motion", out)
        self.assertEqual(json.loads(cf.read_text(encoding="utf-8")).get("motion"), "native")


class TestContentDefault(unittest.TestCase):
    """未表态章节的档位只由 `project.effective_motion` 一处推导，读侧全部经它。"""

    def test_default_follows_content(self):
        from kinema.project import effective_motion
        dialogue = {"id": 1, "lines": [{"speaker": "甲", "text": "走。"}]}
        voiceover = {"id": 2, "narration": "他走了。"}
        self.assertEqual(effective_motion({}), "dubbed")
        self.assertEqual(effective_motion({"shots": [voiceover]}), "dubbed")
        self.assertEqual(effective_motion({"shots": [voiceover, dialogue]}), "native")
        self.assertEqual(effective_motion({"audio_mode": "scored"}), "native")
        omitted = {**dialogue, "review": {"shot": {"state": "omt"}}}
        self.assertEqual(effective_motion({"shots": [voiceover, omitted]}), "dubbed",
                         "弃用镜的对白不参与定档")
        self.assertEqual(effective_motion({"motion": "kenburns", "shots": [dialogue]}),
                         "kenburns", "已表态原样返回")
        self.assertEqual(effective_motion({"motion": "c"}), "dubbed", "别名归一")

    def test_every_reader_agrees(self):
        """Project.motion / uses_seedance / lint / 库索引 / scanner 对未表态章节同一答案。"""
        from kinema.pipeline import variation
        from kinema.project import uses_seedance
        from kinema.storage import base as storage_base
        from kinema.studio import scanner
        data = {"id": "x", "shots": [{"id": 1, "lines": [{"speaker": "甲", "text": "走。"}]}]}
        self.assertEqual(_project_stub(data).motion, "native")
        self.assertTrue(uses_seedance(data))
        self.assertEqual(variation.render_mode(data), "native")
        self.assertEqual(scanner._motion(data), "native")
        self.assertEqual(storage_base.chapter_meta(Path("/nonexistent"), "p", "c", data)["motion"],
                         "native")


class TestTtsFollowsTheSameDefault(unittest.TestCase):
    """tts 与 gen-video 读同一个 `Project.motion`。

    dur 回填语义随档位分叉：未表态章节若按 kenburns 回填，场→镜设计出的
    表演窗口会在配音一步被整批缩成台词长度（58s 时间轴会被缩到 30.5s），
    而这正发生在 gen-video 收口之前——配音是 dubbed 流程的前置节点。"""

    def test_undeclared_chapter_keeps_design_windows_through_tts(self):
        import contextlib
        import io
        import json
        import tempfile
        import unittest.mock as um

        from kinema import voicecast
        from kinema.cli import stage_tts
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "ch01.json"
            cf.write_text(json.dumps({
                "id": "ch01", "aspect": "16:9",
                "shots": [{"id": 1, "dur": 12.0, "narration": "他走了。"}]},
                ensure_ascii=False),
                encoding="utf-8")
            p = Project.load(cf)
            store = ConfigStore.load(None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 um.patch.object(voicecast, "probe_duration", lambda _p: 2.0):
                stage_tts(p, store, ModelRouter(store, force_mock=True))
            self.assertEqual(p.shots[0]["dur"], 12.0,
                             "缺省 dubbed 口径下设计窗口只延不缩")
            self.assertIn("按缺省 dubbed 口径", buf.getvalue())
            # 同一次作业内旁白轨拼接、run 的阶段门读到的也是这个档位；磁盘不写
            self.assertEqual(p.motion, "dubbed")
            self.assertNotIn("motion", json.loads(cf.read_text(encoding="utf-8")))


def _project_stub(data):
    from kinema.project import Project
    return Project(Path("chapters") / "x.json", {"id": "x", **data})


class TestGenVideoLedgerAndSalvage(_Case):
    """真跑的三条钱账纪律：捡回的片段走同一条登记链（cost 记 0）；多比例镜半途失败
    时已付费的比例照常登记入账；真跑一镜都发不出的 dry-run 不把全片预估写进台账。"""

    def test_salvaged_clip_is_registered_not_left_in_limbo(self):
        p, cf = self._project(motion="dubbed")
        clip = p.workdir / "gen_clips" / "shot_1_16x9.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"paid-clip")
        with unittest.mock.patch("kinema.cli._salvageable_clip", return_value=True):
            self._run(p, dry_run=False)
        shot = json.loads(cf.read_text(encoding="utf-8"))["shots"][0]
        self.assertEqual(shot["clips"]["16:9"], str(clip))
        self.assertTrue(shot["gen"]["clip"].get("salvaged"))
        self.assertEqual(shot["gen"]["clip"]["cost"], 0.0)
        self.assertEqual(shot["review"]["clip"]["state"], "wfa")
        self.assertIn("text_fp", json.dumps(shot["gen"]["clip"], ensure_ascii=False) + json.dumps(shot),
                      "捡回的片段同样记台词指纹")

    def test_dry_run_with_nothing_to_send_writes_no_estimate(self):
        p, cf = self._project(motion="dubbed")
        with unittest.mock.patch("kinema.cli._will_burn", return_value=[]):
            out = self._run(p, dry_run=True)
        self.assertIn("一镜都发不出", out)
        self.assertNotIn("cost_estimate", json.loads(cf.read_text(encoding="utf-8")))

    def test_partial_aspect_failure_still_books_the_paid_aspect(self):
        from kinema.cli import stage_gen_video
        from kinema.errors import KinemaError
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video import mock as vmock
        p, cf = self._project(motion="dubbed", aspects=["16:9", "9:16"], image_per_aspect=True)
        p.shots[0]["images"]["9:16"] = self.img
        p.save()
        store = ConfigStore.load(None)
        orig = vmock.MockVideoProvider.generate

        def fake(prov, image, out_path, **kw):
            if "9x16" in str(out_path):
                raise RuntimeError("upstream 500 after the first aspect was billed")
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=2.5, has_audio=True,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = fake
        try:
            with unittest.mock.patch("kinema.cli.probe_duration", return_value=4.0), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(KinemaError):
                stage_gen_video(p, store, ModelRouter(store, force_mock=True), dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        d = json.loads(cf.read_text(encoding="utf-8"))
        self.assertTrue(d["shots"][0]["clips"]["16:9"].endswith("shot_1_16x9.mp4"))
        self.assertNotIn("9:16", d["shots"][0]["clips"])
        self.assertEqual(d["cost"]["video"], 2.5)
