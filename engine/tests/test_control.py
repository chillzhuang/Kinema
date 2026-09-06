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

"""深度捕捉守卫。

按「错了要到哪一步才被发现」排的六组：

1. **导入面洁净** —— 感知栈是可选依赖，`import kinema.control` 拖进 numpy/cv2
   就等于让每一次 `kinema --help` 替一个可选特性买单。
2. **三路仲裁** —— previz > control > sketch 只在 `active_guide` 定一次。判据
   分叉的后果是链图按孤岛断缝、实发却是首帧任务，而链态要落盘。
3. **绑定闸** —— 转场/omt/已有 previz 三道拒绝，以及超出参考视频带宽时
   拒绝而不是静默截断（截断＝运动被拉伸，账单照常）；片段已通过不拦——绑定与
   摘除是人对这一镜的直接决定，片段随之作废，锁不豁免。闸在处理源片之前过。
4. **报价一致** —— dry-run 与事前闸共用一个参考视频投影。两份手写副本里只教会
   一份，预留额度就少于真实账单，而全套测试照常全绿。
5. **纯函数** —— 时序与几何的边界行为，尤其是短序列（测试用的合成片本来就短）。
6. **mock 全链路** —— 除推理外全部真跑：解码、跟踪、时序、渲染、编码、sidecar。
"""
from __future__ import annotations

import contextlib
import io as _io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import control, review, sketchboard
from kinema.errors import ProjectError
from tests.support import LocalBackendEnv


def _have_ffmpeg() -> bool:
    from shutil import which
    return which("ffmpeg") is not None and which("ffprobe") is not None


def _have_stack() -> bool:
    import importlib.util as ilu
    return all(ilu.find_spec(m) for m in ("numpy", "cv2"))


def _clip(out: Path, *, dur=4.0, w=320, h=180, fps=24, audio=True) -> Path:
    """带音轨的合成源片。**必须有音轨**——「音轨原样复制」是本特性的产出契约，
    而既有的 lavfi 助手都是纯视频。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={dur}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(dur), str(out)]
    subprocess.run(args, capture_output=True, check=True)
    return out


def _probe_wh(path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True).stdout.strip().split(",")
    return int(r[0]), int(r[1])


def _has_audio(path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return "audio" in r.stdout


class _Base(unittest.TestCase):
    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        self._env.restore()

    def _project(self, shots=None, **over):
        from kinema.project import Project
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "profile": "anime", "motion": "native",
               "aspect": "16:9", "duration": 5,
               "shots": shots or [{"id": 1, "dur": 5.0, "narration": "", "image": "s1.png"}]}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        pf = cdir.parent / "project.json"
        if not pf.is_file():
            pf.write_text(json.dumps(
                {"id": "p1", "title": "p1", "chapters": [{"id": "ch01", "order": 1}]},
                ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    def _fake_asset(self, project, aid="dance-0001", *, seconds=12.0, fps=24.0):
        """手写一条素材（sidecar + 一段可解码的 control.mp4）。

        绑定契约不需要真跑感知栈：素材是它的**输入**。这样这一整组守卫只要
        装了 ffmpeg 就能跑。
        """
        adir = control.asset_dir(project, aid)
        adir.mkdir(parents=True, exist_ok=True)
        _clip(adir / "control.mp4", dur=seconds, fps=fps, audio=False)
        rec = {"id": aid, "name": f"{aid}.mp4", "status": "done", "people": 1,
               "uploaded_at": "2026-09-04T00:00:00+08:00",
               "source": {"width": 320, "height": 180, "fps": fps,
                          "frames": int(seconds * fps), "seconds": seconds, "audio": False},
               "outputs": {"control": "control.mp4"}}
        control.assets.write_asset(project, aid, rec)
        return aid


# ======================================================== 一、导入面洁净
class TestImportSurface(unittest.TestCase):
    def test_importing_control_pulls_no_perception_stack(self):
        """`import kinema.control` 只准拉进引擎自身。

        子进程里验，因为本测试进程早就把 numpy 导进来了。感知栈几百 MB，
        让它进默认导入路径等于给每次 CLI 启动加一笔与本特性无关的开销。
        """
        code = ("import sys, kinema.control;"
                "print(','.join(m for m in ('numpy','cv2','onnxruntime','mediapipe','rtmlib')"
                " if m in sys.modules))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_contract_api_is_exported(self):
        for name in ("bind_shot", "unbind_shot", "control_shot", "control_seconds",
                     "control_drift", "list_assets", "delete_asset", "build_asset"):
            self.assertTrue(hasattr(control, name), name)


# ======================================================== 二、三路仲裁
class TestArbitration(unittest.TestCase):
    def test_guides_are_ordered_previz_control_sketch(self):
        self.assertEqual(sketchboard.GUIDES, ("previz", "control", "sketch"))

    def test_control_loses_to_previz_and_beats_beats(self):
        """缺省仲裁：previz 压 control，control 压 beats。

        第二条是本特性最贵的静默失败：引擎的 `motion_plan` lint 会催作者写
        `sketch.beats`，写了之后若 beats 反压控制视频，`v2v_shot` 式的一票否决
        会让控制视频一声不响不发——整章白买。
        """
        beats = {"beats": [{"t": "0-1", "action": "起"}]}
        self.assertEqual(sketchboard.active_guide(
            {"previz": "a.mp4", "control": "c.mp4"}), "previz")
        self.assertEqual(sketchboard.active_guide(
            {"control": "c.mp4", "sketch": beats}), "control")
        self.assertEqual(sketchboard.active_guide({"sketch": beats}), "sketch")
        self.assertIsNone(sketchboard.active_guide({}))

    def test_configured_lanes_ignore_auto_split_beats(self):
        """徽章问「配了几条」：自动拆拍是缺省句读的措辞，每个写了运动提示词的镜都有，
        按它算就是每一镜都配了简笔板；只有登记的板或 authored beats 才算一条路径。"""
        control_only = {"control": "shot_1_control.mp4", "video_prompt": "她转身。抬手。落步。"}
        self.assertEqual(sketchboard.configured_guides(control_only), ["control"])
        self.assertEqual(sketchboard.active_guide(control_only), "control")
        both = {**control_only, "sketch": {"beats": [{"action": "转身"}]}}
        self.assertEqual(sketchboard.configured_guides(both), ["control", "sketch"])
        self.assertEqual(sketchboard.configured_guides({"previz": "p.mp4", **both}),
                         ["previz", "control", "sketch"], "顺序即缺省仲裁的优先序")
        self.assertEqual(sketchboard.configured_guides({}), [])

    def test_explicit_guide_still_wins(self):
        self.assertEqual(sketchboard.active_guide(
            {"guide": "sketch", "previz": "a.mp4", "control": "c.mp4"}), "sketch")
        self.assertEqual(sketchboard.active_guide(
            {"guide": "control", "previz": "a.mp4"}), "control")

    def test_previz_predicate_requires_arbitration_to_pick_previz(self):
        """`previz.v2v_shot` 的判据是「仲裁判给我」而不是「没判给简笔板」——
        否则显式 `guide: control` 的镜会同时满足两条 V2V 判据。"""
        from kinema import previz
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.mp4"
            f.write_bytes(b"x")
            self.assertTrue(previz.v2v_shot({"previz": str(f)}))
            self.assertFalse(previz.v2v_shot(
                {"previz": str(f), "control": str(f), "guide": "control"}))

    def test_beats_lint_exempts_control_bound_shots(self):
        """绑了控制视频的镜不该再挨「缺 beats」的警告——引擎不能一边催作者写
        beats、一边让 beats 把控制视频顶掉。"""
        from kinema.pipeline import variation
        ctx = {"motion": "native", "profile": "anime"}
        shots = [{"id": 1, "dur": 5, "control": "c.mp4"}]
        codes = {f.code for f in variation._lint_motion_plan(shots, {}, ctx)}
        self.assertNotIn("motion_plan", codes)

    def test_framechain_island_sees_control_only_when_switched_on(self):
        from kinema.pipeline import framechain
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.mp4"
            f.write_bytes(b"x")
            shot = {"id": 1, "control": str(f)}
            self.assertFalse(framechain.island(shot))
            self.assertFalse(framechain.island(shot, v2v=True))
            self.assertTrue(framechain.island(shot, control=True))


class TestLint(unittest.TestCase):
    """付费前的零成本闸。运行时那行 `⚠ 只在 native 生效` 打完照样烧钱，
    lint 才拦得住。"""

    @staticmethod
    def _codes(doc):
        from kinema.pipeline import variation
        return [f.code for f in variation.lint(doc) if f.code.startswith("control")]

    def _doc(self, **over):
        doc = {"motion": "native", "profile": "anime",
               "shots": [{"id": 1, "dur": 5, "narration": "旁白", "control": "c.mp4"}]}
        doc.update(over)
        return doc

    def test_clean_binding_is_silent(self):
        """native 章绑了就发：除 motion 外没有别的前置，lint 无话可说。"""
        self.assertEqual(self._codes(self._doc()), [])

    def test_dubbed_chapter_is_named(self):
        doc = self._doc(motion="dubbed")
        self.assertIn("control_inert", self._codes(doc))

    def test_previz_coexistence_and_duration_drift_are_named(self):
        doc = self._doc(shots=[{"id": 1, "dur": 8, "narration": "旁白",
                                "control": "c.mp4", "previz": "p.mp4",
                                "gen": {"control": {"dur_at": 5}}}])
        self.assertEqual(self._codes(doc), ["control_binding", "control_binding"])

    def test_unbound_chapter_is_never_flagged(self):
        doc = self._doc(shots=[{"id": 1, "dur": 5, "narration": "旁白"}])
        self.assertEqual(self._codes(doc), [])

    def test_explicit_guide_elsewhere_shadows_the_binding(self):
        """显式 `guide: sketch` 会让绑好的控制视频一帧不发——与 previz 共存同一条 lint。"""
        doc = self._doc(shots=[{"id": 1, "dur": 5, "narration": "旁白",
                                "control": "c.mp4", "guide": "sketch"}])
        self.assertEqual(self._codes(doc), ["control_binding"])

    def test_explicit_control_guide_settles_the_previz_clash(self):
        doc = self._doc(shots=[{"id": 1, "dur": 5, "narration": "旁白",
                                "control": "c.mp4", "previz": "p.mp4",
                                "guide": "control"}])
        self.assertEqual(self._codes(doc), [])


class TestExclusionGates(_Base):
    """三路运动预演一镜只生效一条：登记入口两头都拦，表态入口三路一视同仁。"""

    def test_previz_registration_refuses_a_control_bound_shot(self):
        """与 `bind` 拒绝 previz 镜镜像：靠缺省仲裁悄悄压掉用户框过区间的那条绑定不算互斥。"""
        from kinema import previz
        p = self._project(shots=[{"id": 1, "dur": 5, "narration": "", "control": "c.mp4"}])
        with self.assertRaises(ProjectError) as cm:
            previz.register_previz(p, 1, str(self.tmp / "pz.mp4"))
        self.assertIn("control unbind", str(cm.exception))

    def test_guide_statement_accepts_every_lane(self):
        """`sketch use --guide` 与 Studio `/api/sketch/guide` 的合法值都取 `GUIDES`：
        少一条，那一路就只能由 Agent 写契约、人在 CLI 与网页上表不了态。"""
        import inspect
        from kinema import cli
        from kinema.studio import actions
        for g in sketchboard.GUIDES:
            ns = cli.build_parser().parse_args(
                ["sketch", "use", "--chapter", "x/ch", "--shot", "1", "--guide", g])
            self.assertEqual(ns.guide, g)
        # 两个入口都经 `set_guide` 这一个写点：合法值与「表态改了生效路径就作废片段」
        # 只在那里定一次
        self.assertIn("sketch_mod.set_guide", inspect.getsource(actions.sketch_guide))
        self.assertIn("sketch_mod.set_guide", inspect.getsource(cli.cmd_sketch_use))
        self.assertIn("GUIDES", inspect.getsource(sketchboard.set_guide))


# ======================================================== 三、绑定闸
@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
class TestBind(_Base):
    def test_bind_writes_flat_path_and_metadata_in_gen(self):
        """`shots[].control` 是**扁平路径串**：Gateway 的引用摘要会递归收集
        列出键下的每个字符串，嵌套字典会把素材 id 与时间戳也当成参考物摘要，
        从此每镜恒报引用漂移。"""
        p = self._project()
        aid = self._fake_asset(p)
        r = control.bind_shot(p, 1, aid, start=2.0)
        s = p.data["shots"][0]
        self.assertIsInstance(s["control"], str)
        self.assertTrue(Path(s["control"]).is_file())
        rec = s["gen"]["control"]
        self.assertEqual((rec["asset"], rec["start"], rec["seconds"]), (aid, 2.0, 5))
        self.assertEqual(r["seconds"], 5)
        self.assertNotIn("clip", s)          # 绝不写进成片位

    def test_bind_refuses_transition_and_omt(self):
        p = self._project(shots=[
            {"id": 1, "kind": "transition", "dur": 1},
            {"id": 2, "dur": 5, "review": {"shot": {"state": "omt"}}},
        ])
        aid = self._fake_asset(p)
        for shot, word in ((1, "转场"), (2, "弃用")):
            with self.subTest(shot=shot):
                with self.assertRaises(ProjectError) as cm:
                    control.bind_shot(p, shot, aid)
                self.assertIn(word, str(cm.exception))

    def test_bind_and_unbind_retake_a_locked_clip(self):
        """绑定/摘除运动源是人对这一镜的直接决定，`done` 锁不豁免（与版本回滚、
        宫格换选同类）。锁只挡引擎自行重生。两边必须同规：若摘除放行而绑定被锁拒，
        文档说没绑、锁定的片段却是按控制视频生成的，而唯一能回到一致的动作又被
        同一把锁拒绝。"""
        clip = self.tmp / "shot_1.mp4"
        clip.write_bytes(b"mp4")
        p = self._project(shots=[{"id": 1, "dur": 5, "clip": str(clip),
                                  "review": {"clip": {"state": "done"}}}])
        aid = self._fake_asset(p)
        r = control.bind_shot(p, 1, aid)
        s = p.data["shots"][0]
        self.assertEqual(review.get_state(s, "clip"), "retake")
        self.assertEqual((r["retake"], r["unlocked"]), ("retake", True))
        # 再次通过后摘除：同一条规则，片段作废
        review.set_state(s, "clip", "done")
        r2 = control.unbind_shot(p, 1)
        self.assertEqual(r2["retake"], "retake")
        self.assertEqual(review.get_state(s, "clip"), "retake")
        # 没有片段的镜无物可作废
        p2 = self._project(shots=[{"id": 1, "dur": 5,
                                   "review": {"clip": {"state": "done"}}}])
        aid2 = self._fake_asset(p2)
        self.assertIsNone(control.bind_shot(p2, 1, aid2)["retake"])

    def test_preflight_refuses_before_the_source_is_processed(self):
        """`control build --bind-shot` 在处理源片**之前**过镜态闸：这几条都不依赖
        素材内容，跑完几分钟才发现镜不能绑等于让人白等一趟再重传。素材未生成时
        任何既有绑定都算「绑着别的素材」。"""
        p = self._project(shots=[
            {"id": 1, "kind": "transition", "dur": 1},
            {"id": 2, "dur": 5, "review": {"shot": {"state": "omt"}}},
            {"id": 3, "dur": 5, "previz": "pz.mp4"},
            {"id": 4, "dur": 20},
            {"id": 5, "dur": 5},
            {"id": 6, "dur": 5},
        ])
        aid = self._fake_asset(p)
        control.bind_shot(p, 5, aid)
        for shot, word in ((1, "转场"), (2, "弃用"), (3, "--replace-previz"),
                           (4, "15s"), (5, "先解绑")):
            with self.subTest(shot=shot):
                with self.assertRaises(ProjectError) as cm:
                    control.bind_preflight(p, shot, whole_shot=True)
                self.assertIn(word, str(cm.exception))
        self.assertEqual(control.bind_preflight(p, 6, whole_shot=True)["id"], 6)
        # 镜长只在整镜自动绑时判：框区间的绑定由区间定段长，20s 的镜照常可选
        self.assertEqual(control.bind_preflight(p, 4, aid)["id"], 4)
        # 点名了素材：绑着同一条的镜是回来改区间（`--asset <既有 id>` 就地重建后重绑），
        # 绑着别的才拒
        self.assertEqual(control.bind_preflight(p, 5, aid, whole_shot=True)["id"], 5)
        with self.assertRaises(ProjectError):
            control.bind_preflight(p, 5, "walk-0002", whole_shot=True)

    def test_upload_with_a_target_shot_fails_before_spawning_the_build(self):
        """Studio 上传时点了镜：镜不能绑要在上传这一步说，而不是几分钟后失败在
        任务日志里——人已经离开页面，失败只有 tail 知道。"""
        from kinema.studio import actions, jobs
        p = self._project(shots=[{"id": 1, "dur": 5}])
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid)
        with mock.patch.object(jobs, "spawn_cli", return_value="j1") as sp:
            with self.assertRaises(ProjectError) as cm:
                actions.control_build(self.tmp / "proj", "p1", "ch01",
                                      source=str(self.tmp / "new.mp4"), bind_shot=1)
            self.assertIn("先解绑", str(cm.exception))
            sp.assert_not_called()
            r = actions.control_build(self.tmp / "proj", "p1", "ch01",
                                      source=str(self.tmp / "new.mp4"))
            # 点名既有素材就地重建并绑回同一镜：那是重建后的重绑，照常派活
            r2 = actions.control_build(self.tmp / "proj", "p1", "ch01",
                                       source=str(self.tmp / "new.mp4"),
                                       asset=aid, bind_shot=1)
        self.assertEqual((r["job"], r2["job"]), ("j1", "j1"))
        self.assertIn("--asset", sp.call_args[0][0])

    def test_cli_build_rebinds_the_same_asset_after_an_in_place_rebuild(self):
        """`control build --asset <既有 id> --bind-shot N` 是「素材已重建——重绑一次」
        的单命令形态：镜 N 绑着的正是这条素材，预检按点名的素材判、放行；
        绑着别的素材才在处理之前拒。"""
        from types import SimpleNamespace
        from kinema import cli
        p = self._project(shots=[{"id": 1, "dur": 5}])
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid)
        rec = control.read_asset(p, aid)

        def _args(asset):
            return SimpleNamespace(chapter="p1/ch01", workspace=str(self.tmp / "proj"),
                                   project=None, source=str(self.tmp / "src.mp4"),
                                   asset=asset, no_styled=True, mock=True,
                                   bind_shot=1, config=None)
        with mock.patch.object(cli.control_mod, "build_asset", return_value=rec) as bld, \
                contextlib.redirect_stdout(_io.StringIO()):
            cli.cmd_control_build(_args(aid))
            self.assertEqual(bld.call_count, 1)
            with self.assertRaises(ProjectError) as cm:
                cli.cmd_control_build(_args("walk-0002"))
            self.assertIn("先解绑", str(cm.exception))
            self.assertEqual(bld.call_count, 1, "预检拒了就不该再跑处理")
        from kinema.project import Project
        self.assertEqual(Project.load(p.path).shots[0]["gen"]["control"]["asset"], aid)

    def test_bind_refuses_previz_shot_without_replace(self):
        p = self._project(shots=[{"id": 1, "dur": 5, "previz": "pz.mp4"}])
        aid = self._fake_asset(p)
        with self.assertRaises(ProjectError) as cm:
            control.bind_shot(p, 1, aid)
        self.assertIn("--replace-previz", str(cm.exception))
        control.bind_shot(p, 1, aid, replace_previz=True)
        s = p.data["shots"][0]
        self.assertNotIn("previz", s)
        self.assertIn("control", s)

    def test_bind_refuses_a_second_asset_until_the_shot_is_unbound(self):
        """一镜只收一条控制视频。换素材若直接顶掉，段落文件被就地重写，而「这一镜
        演的是哪条素材」只在 `gen.control` 里换了个 id——成片上看不出运动源什么时候
        变了。同一条素材重绑是改区间，照旧放行。"""
        p = self._project()
        a1 = self._fake_asset(p, "dance-0001")
        a2 = self._fake_asset(p, "walk-0002")
        control.bind_shot(p, 1, a1, start=1.0, end=6.0)
        with self.assertRaises(ProjectError) as cm:
            control.bind_shot(p, 1, a2)
        self.assertIn(a1, str(cm.exception))
        self.assertEqual(control.bind_shot(p, 1, a1, start=2.0, end=7.0)["start"], 2.0)
        control.unbind_shot(p, 1)
        self.assertEqual(control.bind_shot(p, 1, a2)["asset"], a2)

    def test_bind_refuses_shot_longer_than_reference_band(self):
        """参考视频的服务端上限恒 15s，**与别名的 `max_duration` 无关**。
        超了必须拒绝——静默截断是拿 15s 的运动去演 20s 的镜。"""
        p = self._project(shots=[{"id": 1, "dur": 20.0, "narration": ""}])
        aid = self._fake_asset(p, seconds=25.0)
        with self.assertRaises(ProjectError) as cm:
            control.bind_shot(p, 1, aid)
        self.assertIn("15", str(cm.exception))

    def test_bind_refuses_start_beyond_asset(self):
        p = self._project()
        aid = self._fake_asset(p, seconds=6.0)
        with self.assertRaises(ProjectError) as cm:
            control.bind_shot(p, 1, aid, start=4.0)
        self.assertIn("起点最大", str(cm.exception))

    def test_cut_matches_canvas_for_both_fits(self):
        """段落必须先贴合章节画布：`ratio_mode: adaptive` 的别名遇到参考视频会
        发 `ratio="adaptive"`，成片跟着参考视频的几何走而不是画布。"""
        from kinema.ffmpeg import probe_json
        from kinema.models import ConfigStore
        store = ConfigStore.load(None)
        want = store.canvas("16:9")
        for fit in ("pad", "crop"):
            with self.subTest(fit=fit):
                p = self._project()
                aid = self._fake_asset(p)
                control.bind_shot(p, 1, aid, fit=fit, store=store)
                v = next(x for x in probe_json(p.data["shots"][0]["control"])["streams"]
                         if x["codec_type"] == "video")
                self.assertEqual((int(v["width"]), int(v["height"])), want)

    def test_unbind_clears_contract_but_keeps_file(self):
        p = self._project()
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid)
        cut = Path(p.data["shots"][0]["control"])
        r = control.unbind_shot(p, 1)
        s = p.data["shots"][0]
        self.assertNotIn("control", s)
        self.assertNotIn("control", s.get("gen") or {})
        self.assertTrue(cut.is_file())
        self.assertEqual(sorted(r["dropped"]), ["control", "gen.control"])

    def test_delete_refuses_while_bound_and_names_the_shots(self):
        p = self._project()
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid)
        with self.assertRaises(ProjectError) as cm:
            control.delete_asset(p, aid)
        self.assertIn("镜 1", str(cm.exception))
        control.unbind_shot(p, 1)
        control.delete_asset(p, aid)
        self.assertFalse(control.asset_dir(p, aid).exists())

    def test_drift_reports_rebuild_and_duration_change(self):
        p = self._project()
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid)
        s = p.data["shots"][0]
        self.assertIsNone(control.control_drift(s, control.build_digest(p, aid)))
        self.assertIn("重建", control.control_drift(s, "sha256:0000000000000000"))
        s["dur"] = 8.0
        self.assertIn("时长", control.control_drift(s, control.build_digest(p, aid)))


# ======================================================== 四、报价与请求一致
@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
class TestGenVideoWiring(_Base):
    def _bound(self):
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s1.png"}])
        img = self.tmp / "s1.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        p.data["shots"][0]["image"] = str(img)
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid)
        return p

    def test_ref_video_projection_respects_each_switch(self):
        from kinema import cli
        p = self._bound()
        s = p.data["shots"][0]
        self.assertIsNone(cli._ref_video(s))
        self.assertIsNone(cli._ref_video(s, previz_on=True))
        rv = cli._ref_video(s, control_on=True)
        self.assertEqual((rv[0], rv[1], rv[2]), ("control", s["control"], 5.0))

    def test_prompt_preview_exposes_the_reference_video_entity(self):
        """提示词说「@视频1」，预览行就得带着那一份文件：面板按它渲成可点看的引用。"""
        from kinema import cli
        from kinema.models import ConfigStore, ModelRouter
        p = self._bound()
        s = p.data["shots"][0]
        store = ConfigStore.load(None)
        rows = cli.video_prompt_preview(p, store, ModelRouter(store, force_mock=True))
        self.assertIn("@视频1", rows[0]["prompt"])
        self.assertEqual(rows[0]["videos"],
                         [{"no": 1, "kind": "control", "path": s["control"], "seconds": 5.0}])

    def test_dry_run_quote_and_preflight_agree(self):
        """`--dry-run` 的总秒数与事前闸的预留必须同源。两份手写副本里只教会
        一份，预留就少于账单，而全套测试照常全绿。"""
        from kinema import cli
        from kinema.models import ConfigStore, ModelRouter
        p = self._bound()
        store = ConfigStore.load(None)
        router = ModelRouter(store, force_mock=True)
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.stage_gen_video(p, store, router, dry_run=True)
        out = buf.getvalue()
        self.assertIn("参考视频=control", out)
        prov, _ = cli._vroute_for(p, store, router) if hasattr(cli, "_vroute_for") else (None, None)
        self.assertRegex(out, r"共 1 镜 ≈ 10s")   # 5s 输出 + 5s 输入

    def test_control_shot_suppresses_last_frame_and_reference_mode(self):
        from kinema import cli
        from kinema.models import ConfigStore, ModelRouter
        p = self._bound()
        p.data["shots"].append({"id": 2, "dur": 5.0, "narration": "",
                                "image": p.data["shots"][0]["image"]})
        store = ConfigStore.load(None)
        router = ModelRouter(store, force_mock=True)
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.stage_gen_video(p, store, router, dry_run=True)
        out = buf.getvalue()
        self.assertIn("参考视频=control", out)
        self.assertIn("全能参考", out)          # 未绑的镜仍走缺省档

    def test_dry_run_never_uploads_anything(self):
        """报价是零成本的：`--dry-run` 不调 API，也就没有理由把参考视频推上云。

        上传发生在计划循环里、而 dry-run 在那之前就收口——两者相隔几百行，
        重构时把 return 挪后一点就会静默开始上传（对象存储照常计费、还要求
        本机配了密钥），而所有断言仍然全绿。故这里把「一个字节都不传」钉死。
        """
        from kinema import cli
        from kinema.models import ConfigStore, ModelRouter
        from kinema.storage import media as media_mod
        p = self._bound()
        store = ConfigStore.load(None)
        router = ModelRouter(store, force_mock=False)   # mock 走另一条免上云的短路

        class _Tripwire(media_mod.MediaStore):
            @property
            def configured(self):
                return True

            def upload(self, local):
                raise AssertionError(f"dry-run 不该上传任何东西：{local}")

        with mock.patch.object(media_mod, "MediaStore", _Tripwire):
            media_mod._stores.clear()
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.stage_gen_video(p, store, router, dry_run=True)
            media_mod._stores.clear()
        self.assertIn("参考视频=control", buf.getvalue())

    def test_only_the_two_public_url_paths_upload_on_capability(self):
        """上云判据分两档，混用即错向：

        · `configured`（能力齐备）—— 协议层只收公网 URL 的那两条：参考视频、口型精修。
          它们按需上传单个文件，**不要求整份工作区改档**。
        · `enabled`（上云是默认档）—— `oss sync` / `pull` / `status` 与缺媒体时的回拉，
          那是整份工作区搬家。

        判反了的后果不对称：该用 configured 的写成 enabled，用户为一条视频链接
        被逼着把所有图都搬上云；反过来则是没打算上云的人被悄悄传了一堆文件。
        """
        src = (Path(__file__).resolve().parents[1] / "kinema" / "cli.py") \
            .read_text(encoding="utf-8")
        for fn, want in (("_ref_video_url", "ms.configured"),
                         ("stage_lipsync", "ms.configured"),
                         ("cmd_oss_sync", "ms.enabled")):
            body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
            self.assertIn(want, body, f"{fn} 的上云判据不是 {want}")

    def test_mock_provider_records_reference_video(self):
        """离线守卫必须看得见参考视频真的进了请求：`generate(**kwargs)` 会静默
        吞掉不认识的键，mock 不显式收下就等于全链路对本特性全盲。"""
        from kinema.providers.video.mock import MockVideoProvider
        import inspect
        sig = inspect.signature(MockVideoProvider.generate).parameters
        self.assertIn("reference_video", sig)
        self.assertIn("reference_video_seconds", sig)


# ======================================================== 五、纯函数
@unittest.skipUnless(_have_stack(), "需要 numpy 与 opencv")
class TestPureFunctions(unittest.TestCase):
    def test_smooth_nan_handles_sequences_shorter_than_the_kernel(self):
        """深度窗的核是 73 帧，任何短于 3 秒的素材都比它短。

        `np.convolve(mode="same")` 返回的长度是 `max(信号, 核)`——核更长时结果
        比输入还长，随后按输入长度的掩码索引直接越界。原型正是在这里崩的，
        而测试用的合成片恰好落在这个量级。
        """
        import numpy as np
        from kinema.control import temporal
        for n in (1, 5, 24, 73, 200):
            with self.subTest(n=n):
                out = temporal.smooth_nan(np.ones(n, float), 12.0)
                self.assertEqual(len(out), n)
                np.testing.assert_allclose(out, 1.0, rtol=1e-6)

    def test_smooth_nan_keeps_holes(self):
        import numpy as np
        from kinema.control import temporal
        a = np.array([1.0, np.nan, 1.0, 1.0])
        out = temporal.smooth_nan(a, 1.2)
        self.assertTrue(np.isnan(out[1]))
        self.assertFalse(np.isnan(out[0]))

    def test_fill_gaps_bridges_short_holes_only(self):
        import numpy as np
        from kinema.control import temporal
        a = np.array([0.0] + [np.nan] * 3 + [4.0])
        self.assertFalse(np.isnan(temporal.fill_gaps(a, 8)).any())
        b = np.array([0.0] + [np.nan] * 20 + [21.0])
        self.assertTrue(np.isnan(temporal.fill_gaps(b, 8)).any())

    def test_track_thresholds_scale_with_fps(self):
        """阈值按秒而不是按帧：8 帧在 30fps 下是 0.27 秒、60fps 下只有 0.13 秒，
        同一份帧数阈值在两种源片上严宽差一倍。"""
        import numpy as np
        from kinema.control import temporal
        from kinema.control.models import MockBundle
        fig = MockBundle()._figure
        # 一条只出现 10 帧的轨迹：30fps 下是 0.33 秒 < 0.5 秒门槛，该丢
        per_frame = [[(0, fig(640, 360, 320.0, i * 0.2))] if i < 10 else []
                     for i in range(60)]
        self.assertEqual(temporal.stabilise_tracks(per_frame, 60, 30.0), {})
        # 同样 10 帧，在 10fps 下是 1 秒，该留
        self.assertEqual(list(temporal.stabilise_tracks(per_frame, 60, 10.0)), [0])

    def test_low_confidence_detections_are_rejected(self):
        """背景里的车、反光能凑够 6 个刚过门槛的关节，逐条合格而整体很虚——
        单人画面里也会凭空多出一条连着一两秒的轨迹，成片里就是一条乱挥的肢体。"""
        from kinema.control.params import KPT_THR, MIN_JOINTS, MIN_KPT_MEAN
        self.assertGreater(MIN_KPT_MEAN, KPT_THR,
                           "整体门槛必须严于单关节门槛，否则等于没加")
        src = (Path(__file__).resolve().parents[1] / "kinema" / "control"
               / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("sc[keep].mean()", src)
        self.assertIn(f"MIN_JOINTS", src)

    def test_depth_window_survives_a_single_frame(self):
        import numpy as np
        from kinema.control import temporal
        d = np.random.RandomState(0).rand(1, 8, 8).astype(np.float32)
        lo, hi = temporal.depth_window(d, np.ones((1, 8, 8), bool))
        self.assertEqual((len(lo), len(hi)), (1, 1))
        self.assertGreater(hi[0], lo[0])

    def test_tracker_keeps_ids_while_two_people_approach(self):
        """两人相向靠近时 id 不许互换——换了就是两个人的骨骼在成片里对调。

        用真的人形点云（`MockBundle` 那套站姿）而不是一团点：贪心 IoU 匹配的
        行为完全取决于包围框形状，退化成线段的点云测不出真实表现。
        """
        from kinema.control.models import MockBundle
        from kinema.control.track import Tracker
        fig = MockBundle()._figure
        t = Tracker()
        seen = []
        for step in range(12):
            a = fig(640, 360, 200.0 + step * 6, step * 0.3)
            b = fig(640, 360, 440.0 - step * 6, step * 0.3 + 1.7)
            seen.append(tuple(t.update([a, b])))
        self.assertEqual(seen[0], (0, 1))
        self.assertTrue(all(s == (0, 1) for s in seen), seen)

    def test_tracker_reuses_the_id_after_a_short_dropout(self):
        """检测掉几帧（被挡住）后必须还是同一个 id——每次遮挡都换 id
        就是骨骼颜色在成片里整套换掉。"""
        from kinema.control.models import MockBundle
        from kinema.control.track import Tracker
        fig = MockBundle()._figure
        t = Tracker()
        first = t.update([fig(640, 360, 320.0, 0.0)])
        for _ in range(3):
            t.update([])
        again = t.update([fig(640, 360, 322.0, 0.4)])
        self.assertEqual(first, again)

    def test_square_box_stays_inside_the_canvas_scale(self):
        from kinema.control.geometry import square_box
        x, y, side = square_box(-40, -40, 10, 300, 320, 180)
        self.assertGreaterEqual(side, 64)
        self.assertLessEqual(side, 320)

    def test_iou_is_zero_for_disjoint_boxes(self):
        from kinema.control.geometry import iou
        self.assertEqual(iou((0, 0, 1, 1), (5, 5, 6, 6)), 0.0)
        self.assertAlmostEqual(iou((0, 0, 2, 2), (0, 0, 2, 2)), 1.0)


# ======================================================== 六、参数构造器
class TestIoArgs(unittest.TestCase):
    def test_decode_keeps_frame_parity(self):
        from kinema.control.io import decode_args
        a = decode_args("/w/x_work/control/assets/a/source.mp4", 540, 960)
        self.assertIn("-fps_mode", a)
        self.assertEqual(a[a.index("-fps_mode") + 1], "passthrough")

    def test_encode_copies_audio_without_shortest(self):
        """`-shortest` 会把帧数守恒交给音频的采样对齐：AAC 一帧 1024 采样，
        时长落不到帧边界的片子（大多数）音轨都短一点点，最后一个视频帧就被砍掉
        ——3.000s 的片子会出来 89/90 帧，整条链判失败。"""
        from kinema.control.io import encode_args
        a = encode_args("/w/x_work/control/assets/a/control.mp4",
                        "/w/x_work/control/assets/a/source.mp4", 1080, 1920, 24.0, True)
        self.assertNotIn("-shortest", a)
        self.assertEqual(a[a.index("-c:a") + 1], "copy")
        self.assertIn("1:a:0", a)

    def test_every_long_running_argv_carries_the_work_marker(self):
        """孤儿回收器只认命令行里带 `_work/` 的 ffmpeg。读用户桌面文件、写向
        `pipe:` 的解码器不带这个标记，父进程被杀后会永远占着一颗核心——
        这正是源片必须先归一进素材目录再开跑的原因。"""
        from kinema.ffmpeg import _ORPHAN_MARK
        from kinema.control.io import decode_args, encode_args, normalise_args
        base = "/ws/p1/chapters/ch01_work/control/assets/a"
        for argv in (decode_args(f"{base}/source.mp4", 540, 960),
                     encode_args(f"{base}/control.mp4", f"{base}/source.mp4",
                                 1080, 1920, 24.0, True),
                     normalise_args("/home/u/dance.mp4", f"{base}/source.mp4", 24.0)):
            self.assertTrue(any(_ORPHAN_MARK in str(x) for x in argv), argv)


@unittest.skipUnless(_have_stack(), "需要 numpy/opencv")
class TestSheetLayout(unittest.TestCase):
    """封面与对照片共用**同一条方向判据**：竖素材并排、横素材上下。

    封面这一头还多一层理由：缩略带的格子只定高、宽随图的原比例（`.cvc-cell img`）
    ——横素材并排出来是 32:9，一条素材就顶出一个比竖素材宽三倍半的格子，
    把整条带子挤走样。
    """

    def _sheet(self, w, h):
        import cv2
        import numpy as np
        from kinema.control.render import write_sheet
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sheet.png"
            write_sheet(out, [np.zeros((h, w, 3), np.uint8)] * 2)
            img = cv2.imread(str(out))
            return img.shape[1], img.shape[0]

    def test_portrait_footage_puts_the_two_panels_side_by_side(self):
        self.assertEqual(self._sheet(720, 1280), (720, 640))

    def test_landscape_footage_stacks_the_two_panels(self):
        self.assertEqual(self._sheet(1280, 720), (640, 720))


# ======================================================== 七、mock 全链路
@unittest.skipUnless(_have_ffmpeg() and _have_stack(), "需要 ffmpeg + numpy/opencv")
class TestMockPipeline(_Base):
    def test_build_runs_the_whole_orchestration_without_touching_the_chapter(self):
        p = self._project()
        src = _clip(self.tmp / "src.mp4", dur=3.0, w=480, h=270)
        doc = Path(p.path)
        before = doc.read_bytes()

        seen = []
        rec = control.build_asset(p, src, mock=True,
                                  on_progress=lambda *a: seen.append(a))
        self.assertEqual(rec["status"], "done")
        self.assertGreaterEqual(rec["people"], 1)
        # 帧数守恒是硬不变量：差一帧就是运动与成片错位
        from kinema.ffmpeg import probe_frames
        adir = control.asset_dir(p, rec["id"])
        self.assertEqual(probe_frames(adir / "control.mp4"), rec["source"]["frames"])
        for f in ("control.mp4", "styled.mp4", "sheet.png", "strip.png", "source.mp4"):
            self.assertTrue((adir / f).is_file(), f)
        self.assertFalse((adir / "_cache.npz").exists())
        self.assertTrue(any(x[0] == 1 for x in seen) and any(x[0] == 2 for x in seen))
        # build 从不 load/save 章节文档——这是多条素材并行处理不会丢更新的全部理由
        self.assertEqual(doc.read_bytes(), before)

    def test_build_copies_the_source_audio_track(self):
        p = self._project()
        src = _clip(self.tmp / "src.mp4", dur=2.0, w=320, h=180, audio=True)
        rec = control.build_asset(p, src, mock=True, styled=False)
        from kinema.ffmpeg import probe_json
        out = control.asset_dir(p, rec["id"]) / "control.mp4"
        kinds = {s["codec_type"] for s in probe_json(out)["streams"]}
        self.assertIn("audio", kinds)

    def test_build_refuses_an_over_long_source_before_loading_models(self):
        from kinema.control.params import MAX_SOURCE_SEC
        from kinema.errors import KinemaError
        p = self._project()
        src = _clip(self.tmp / "long.mp4", dur=MAX_SOURCE_SEC + 1, w=160, h=90, audio=False)
        with self.assertRaises(KinemaError) as cm:
            control.build_asset(p, src, mock=True)
        self.assertIn(f"{MAX_SOURCE_SEC:.0f}s", str(cm.exception))

    def test_failed_build_lands_a_terminal_status(self):
        """跑挂的素材必须落终态——否则页面上的进度条永远转下去。"""
        from unittest import mock as umock
        from kinema.errors import KinemaError
        p = self._project()
        src = _clip(self.tmp / "src.mp4", dur=2.0, w=320, h=180)
        with umock.patch("kinema.control.pipeline._pass1", side_effect=KinemaError("炸了")):
            with self.assertRaises(KinemaError):
                control.build_asset(p, src, mock=True)
        aid = control.asset_id_for(src)
        rec = control.read_asset(p, aid)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("炸了", rec["error"])


class TestSkillBinding(unittest.TestCase):
    """kinema-depth 是能力包，不进 `project.skill`。

    误绑的代价不对称：`agent route --project` 从此返回一个 capability，项目丢掉
    自己的画风指挥层，改回来要逐项目加逐章重设。判据取 manifest 里的
    `activation`——声明与执法同源，不另建一张「哪些 skill 能绑」的表。
    """

    def test_capability_skills_are_refused_as_a_project_binding(self):
        from kinema.errors import ConfigError
        from kinema.skills import validate_skill
        self.assertEqual(validate_skill("kinema-depth"), "kinema-depth")          # 显式调用照常
        with self.assertRaises(ConfigError) as cm:
            validate_skill("kinema-depth", bind=True)
        self.assertIn("kinema-depth", str(cm.exception))

    def test_route_skills_still_bind(self):
        from kinema.skills import validate_skill
        self.assertEqual(validate_skill("kn-anime", bind=True), "kn-anime")

    def test_the_gate_matches_every_manifest_declaration(self):
        """闸的判据必须与 manifest 逐条对齐：声明了 project-bound 的全放行、
        没声明的全拒绝。今天这条闸精确成立、零误伤，明天加 skill 时也要如此。"""
        from kinema.agent_system import AgentCatalog
        from kinema.errors import ConfigError
        from kinema.skills import validate_skill
        for item in AgentCatalog.load().all():
            bindable = "project-bound" in (item.get("activation") or [])
            with self.subTest(skill=item["id"]):
                if bindable:
                    self.assertEqual(validate_skill(item["id"], bind=True), item["id"])
                else:
                    with self.assertRaises(ConfigError):
                        validate_skill(item["id"], bind=True)


class TestMediaCapabilityVsMode(unittest.TestCase):
    """「上云能力」与「上云是默认档」是两件事。

    参考视频在协议层只收公网 URL，本地路径不是慢一点而是发不出去。为这一条链接
    把整份工作区的图都搬上云是冗余的——故那条路判 `configured`（能力齐备），
    而 `oss sync` 一类的整体迁移仍判 `enabled`（默认档）。
    """

    @staticmethod
    def _store(**over):
        from kinema.storage.media import MediaStore
        cfg = {"backend": "local", "provider": "aliyun", "bucket": "b",
               "region": "cn-hangzhou", "ak": "a", "sk": "s"}
        cfg.update(over)
        return MediaStore(Path("/ws"), cfg)

    def test_local_backend_with_full_oss_config_is_capable_but_not_default(self):
        ms = self._store()
        self.assertFalse(ms.enabled)      # 默认档仍是 local
        self.assertTrue(ms.configured)    # 但参考视频发得出去
        self.assertIn("OSS 已配置", ms.describe())

    def test_incomplete_config_is_not_capable(self):
        for missing in ("bucket", "ak", "sk"):
            with self.subTest(missing=missing):
                self.assertFalse(self._store(**{missing: ""}).configured)
        # 位置缺失也不算齐备：aliyun 的 endpoint 由 region 拼，两者都空拼不出来
        self.assertFalse(self._store(region="", endpoint="").configured)
        self.assertTrue(self._store(region="", endpoint="https://x").configured)

    def test_reference_video_gate_reads_capability_not_mode(self):
        """判据写错成 `enabled` 时，配好 OSS 却用 local 档的人会被拒——
        而那正是本条要支持的主用法。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "cli.py").read_text(encoding="utf-8")
        body = src.split("def _ref_video_url(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("ms.configured", body)
        self.assertNotIn("ms.enabled", body)


class TestReadinessSurface(unittest.TestCase):
    """依赖没装时，用户必须在**拖视频之前**就知道。

    就绪态不下发到卡上时，缺依赖的用户拖完视频只会看到一个失败任务加一行
    ModuleNotFoundError。
    """

    def test_readiness_is_shipped_with_the_chapter_payload(self):
        from kinema.studio import scanner
        r = scanner._control_ready()
        self.assertIn("ready", r)
        self.assertIsInstance(r["notes"], list)

    def test_readiness_has_exactly_one_source(self):
        """就绪态只跟章节 payload 走——再开一个 `/api/control/ready` 端点就是
        第二真源，两边迟早说两套话。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "studio"
               / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/control/ready", src)

    def test_card_gates_the_entry_and_prints_the_install_command(self):
        js = (Path(__file__).resolve().parents[1] / "kinema" / "studio_app" / "app"
              / "control.js").read_text(encoding="utf-8")
        self.assertIn("control_ready", js)
        self.assertIn("engine[control]", js)      # 卡上可复制的安装命令
        self.assertIn("--no-deps rtmlib", js)     # 少这一行就会装出被遮蔽的 cv2

    def test_desk_is_a_card_with_dialogs_not_a_full_screen_route(self):
        """深度捕捉与简笔分镜是同一类东西的两个实例，形态必须一致：卡上一条缩略带
        + 弹层选镜。另起一个全幅路由会让章节页读起来像三个软件，而它的产物、忙态、
        折叠门全都要再实现一遍。"""
        app = (Path(__file__).resolve().parents[1] / "kinema" / "studio_app"
               / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("#/control/", app)
        self.assertNotIn("view-control", app)
        js = (Path(__file__).resolve().parents[1] / "kinema" / "studio_app" / "app"
              / "control.js").read_text(encoding="utf-8")
        self.assertNotIn("export function mount", js)
        self.assertIn("openShell", js)            # 弹层一律走骨架工厂

    def test_desk_shares_one_stylesheet_block_with_the_sketchboard(self):
        """两张卡共用同一段样式规则——各存一份必然慢慢长歪。"""
        css = (Path(__file__).resolve().parents[1] / "kinema" / "studio_app"
               / "style.css").read_text(encoding="utf-8")
        for k in ("card", "go", "strip", "cell", "stat", "note"):
            self.assertIn(f".skb-{k}, .cvc-{k}", css, k)

    def test_notes_carry_the_actual_commands(self):
        """红条上的文案必须自带命令——只说「未就绪」等于让用户去猜。"""
        from kinema.control import models
        models._deps = (False, ["mediapipe"])
        try:
            ready, notes = models.readiness()
            self.assertFalse(ready)
            self.assertTrue(any("engine[control]" in n for n in notes), notes)
        finally:
            models._deps = None


class TestLiveFeedback(unittest.TestCase):
    """上传后界面必须当场有反应，且处理期间进度要自己往前走。

    两半缺一不可，而它们同源：`control build` **从不碰章节文档**。
    · 服务端建档要先哈希整个文件、再整段解码数帧，那几秒里它一无所有——
      故卡上必须消费本地忙态账本，提交即出占位格；
    · `updated_at` 全程不动——故轮询签名必须自带一段深度素材摘要，
      否则整个处理期间都被判成「无变化」，只有整页刷新才看得见。
    """

    @staticmethod
    def _js(name):
        return (Path(__file__).resolve().parents[1] / "kinema" / "studio_app" / "app"
                / name).read_text(encoding="utf-8")

    def test_card_renders_from_the_local_busy_ledger(self):
        js = self._js("control.js")
        card = js.split("function controlCard(", 1)[1].split("\n/* ---", 1)[0]
        self.assertIn("CTLJOBS", card, "入口卡必须读忙态账本，否则提交后界面纹丝不动")

    def test_poll_signature_covers_control_assets(self):
        js = self._js("chapter.js")
        sig = js.split("function chapterSignature(", 1)[1].split("\n}", 1)[0]
        self.assertIn("control_assets", sig)
        self.assertIn("progress", sig, "进度不进签名，处理期间轮询恒判无变化")

    def test_build_still_never_touches_the_chapter_document(self):
        """上面两条都是为了绕开这个约束——它本身不许被「顺手改一下」解决掉。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "control"
               / "pipeline.py").read_text(encoding="utf-8")
        for banned in ("Project.load", "project.save(", "Project.mutate"):
            self.assertNotIn(banned, src, banned)


class TestBuildRobustness(unittest.TestCase):
    def test_progress_reporting_never_kills_the_build(self):
        """Studio 一重启，子进程的 stdout 管道就断，下一次进度 print 抛
        BrokenPipeError。没人在听进度**不是失败**——那会把跑了几分钟的处理
        连同半成品一起葬掉。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "cli.py").read_text(encoding="utf-8")
        body = src.split("def cmd_control_build(", 1)[1].split("\n@", 1)[0]
        cb = body.split("def on_progress(", 1)[1].split("\n    r = ", 1)[0]
        self.assertIn("except OSError", cb)

    def test_clear_incoming_resolves_both_sides(self):
        """相对路径进来时也要清掉——两边不 resolve 就永远不相等，
        清理成了一句永不执行的死代码——CLI 传相对路径时会留下一份。"""
        from kinema.control import incoming_dir
        from kinema.control.pipeline import clear_incoming
        env = LocalBackendEnv(); env.enable()
        try:
            with tempfile.TemporaryDirectory() as d:
                cdir = Path(d) / "proj" / "p1" / "chapters"
                cdir.mkdir(parents=True)
                (cdir / "ch01.json").write_text(
                    json.dumps({"id": "c", "shots": []}), encoding="utf-8")
                (cdir.parent / "project.json").write_text(
                    json.dumps({"id": "p1"}), encoding="utf-8")
                from kinema.project import Project
                proj = Project.load(cdir / "ch01.json")
                f = incoming_dir(proj) / "x.mp4"
                f.write_bytes(b"x")
                import os
                rel = Path(os.path.relpath(f, Path.cwd()))
                clear_incoming(proj, rel)
                self.assertFalse(f.exists())
        finally:
            env.restore()

    def test_incoming_is_cleared_only_on_success(self):
        """归一副本才是正本；上传件留着就是同一段视频占两份盘。失败时反而要留，
        重试不必让用户再传一遍。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "control"
               / "pipeline.py").read_text(encoding="utf-8")
        body = src.split("def build_asset(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("clear_incoming(project, src0)", body)
        # 清理必须在 try 的成功分支里，不在 except / finally
        after_except = body.split("except Exception as exc:", 1)[1]
        self.assertNotIn("clear_incoming", after_except)


class TestDirectiveLanes(unittest.TestCase):
    def test_every_guide_has_a_branch_in_the_gen_video_directive(self):
        """「复制 gen-video 指令」的运动规划行必须认识每一条仲裁路径。

        漏掉一条不会报错——那一档的镜静静落进兜底支，指令台于是叫 agent 去写详细
        运动分段，而引擎正在同时压掉那类措辞。判据取 `sketchboard.GUIDES`，
        下一个加 guide 的人自动被这条守卫拦住。
        """
        js = (Path(__file__).resolve().parents[1] / "kinema" / "studio_app" / "app"
              / "chapter.js").read_text(encoding="utf-8")
        lane = js.split("const lane = ", 1)[1].split("const txt = ", 1)[0]
        for g in sketchboard.GUIDES:
            self.assertIn(f'guide_active === "{g}"', lane, g)


# ======================================================== 八、契约归属
class TestContractOwnership(unittest.TestCase):
    def test_author_cannot_write_shot_control_through_the_gateway(self):
        """`shots[].control` 是 engine-managed：作者提交它必须被 plan validate 拒收。
        拒收是结构性的（白名单差集），只要 `control` 永不进 `shot_fields`。"""
        root = Path(__file__).resolve().parents[2]
        contracts = json.loads((root / "agent" / "contracts.json").read_text(encoding="utf-8"))
        self.assertNotIn("control", contracts["chapter_plan"]["shot_fields"])

    def test_control_video_has_no_chapter_switch(self):
        """绑定即发、解绑即不发：发不发由 `shots[].control` 的绑定状态推导，没有章级
        字段、CLI 旗标或 Studio 开关。一个开关是让同一个人对同一件事表两次态，还能
        被忘记打开——绑定成功、片段却一帧不发。"""
        from kinema import cli
        root = Path(__file__).resolve().parents[2]
        contracts = json.loads((root / "agent" / "contracts.json").read_text(encoding="utf-8"))
        self.assertNotIn("control_video", contracts["chapter_plan"]["chapter_fields"])
        self.assertNotIn("control_video", set().union(*review.CHAPTER_STAGE_FIELDS.values()))
        schema = json.loads((root / "docs" / "kinema" / "project.schema.json")
                            .read_text(encoding="utf-8"))
        self.assertNotIn("control_video", schema["properties"])
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["gen-video", "--chapter", "x/ch", "--control"])
        self.assertNotIn("control_video",
                         (root / "engine" / "kinema" / "studio_app" / "app" / "control.js")
                         .read_text(encoding="utf-8"))

    def test_control_is_reachable_by_the_reference_digest_scan(self):
        """漏了这一条，`agent explain --stage video` 会对每个绑了控制视频的镜
        永远报 `references_changed`——Envelope 里有它的摘要，文档扫描却够不着。"""
        from kinema.agent_gateway import _document_reference_digests
        from kinema.prompt_contract import reference_digest
        doc = {"shots": [{"id": 1, "control": "/w/x_work/control/shot_1_control.mp4"}]}
        self.assertIn(reference_digest("/w/x_work/control/shot_1_control.mp4"),
                      _document_reference_digests(doc))


# ======================================================== 十、区间与对照片
class TestSegmentRange(_Base):
    """区间绑定：段长由框选决定，并把该镜 `dur` 对齐过去。"""

    def test_range_sets_segment_length_and_aligns_dur(self):
        """1:1 是运动不被拉伸的前提。框了 7 秒就得裁 7 秒、镜也变 7 秒——
        两个数字各说各的，成片里就是运动被拉长或截断。"""
        p = self._project(shots=[{"id": 1, "dur": 12.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=20.0)
        r = control.bind_shot(p, 1, aid, start=2.0, end=9.0)
        self.assertEqual(r["seconds"], 7)
        self.assertEqual(r["start"], 2.0)
        self.assertEqual(r["end"], 9.0)
        self.assertEqual(p.shots[0]["dur"], 7)
        self.assertEqual(p.shots[0]["gen"]["control"]["dur_at"], 7)

    def test_range_outside_the_ladder_is_refused_not_clamped(self):
        """静默钳位就是拿 15 秒的运动去演 20 秒的镜——账单照收，运动错位。"""
        p = self._project(shots=[{"id": 1, "dur": 12.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=25.0)
        for start, end in ((0.0, 20.0), (0.0, 2.0)):
            with self.assertRaises(ProjectError) as cm:
                control.bind_shot(p, 1, aid, start=start, end=end)
            self.assertIn("4~15", str(cm.exception))

    def test_omitting_end_keeps_the_shot_driven_length(self):
        """不框区间时段长仍由该镜的请求秒数定，`dur` 一个字不动。"""
        p = self._project(shots=[{"id": 1, "dur": 9.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=20.0)
        r = control.bind_shot(p, 1, aid, start=1.0)
        self.assertEqual(r["seconds"], 9)
        self.assertEqual(p.shots[0]["dur"], 9.0)

    def test_rebinding_drops_the_stale_compare(self):
        """对照片是照旧区间拼的，区间一改它就在说另一段的事。"""
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=20.0)
        control.bind_shot(p, 1, aid, start=0.0, end=5.0)
        stale = [control.shot_compare_path(p, 1, tiles=n) for n in (2, 3)]
        for f in stale:
            f.write_bytes(b"stale")
        control.bind_shot(p, 1, aid, start=6.0, end=12.0)
        self.assertEqual([f for f in stale if f.is_file()], [])

    def test_unbinding_drops_the_compare_too(self):
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=20.0)
        control.bind_shot(p, 1, aid, start=0.0, end=5.0)
        stale = [control.shot_compare_path(p, 1, tiles=n) for n in (2, 3)]
        for f in stale:
            f.write_bytes(b"stale")
        control.unbind_shot(p, 1)
        self.assertEqual([f for f in stale if f.is_file()], [])


class TestAssetIdNeverClobbers(_Base):
    """重传同一个源片得**新素材**，不顶掉旧的。

    覆盖是不可逆的：旧产物没了，而已绑的镜还指着照旧产物裁出来的段落，
    盘上那一段与素材从此对不上——而重传同一个文件是常规操作。
    """

    def test_same_source_twice_yields_two_assets(self):
        p = self._project()
        aid = self._fake_asset(p, aid="dance-0001")
        # 造一个内容指纹恰好落在既有 id 上的源片：直接拿它的 control.mp4 当源
        src = control.asset_dir(p, aid) / "control.mp4"
        base = control.assets.asset_id_for(src)
        control.asset_dir(p, base).mkdir(parents=True, exist_ok=True)
        self.assertEqual(control.assets.unique_asset_id(p, src), f"{base}-2")
        control.asset_dir(p, f"{base}-2").mkdir(parents=True, exist_ok=True)
        self.assertEqual(control.assets.unique_asset_id(p, src), f"{base}-3")

    def test_build_falls_back_to_a_free_id(self):
        """回退路径必须是 `unique_asset_id`——换回 `asset_id_for` 就是覆盖，
        而覆盖一条已绑素材没有任何提示，事后也无从恢复。
        显式 `--asset <既有 id>` 仍就地重建：那是明确表态。"""
        import inspect
        from kinema.control import pipeline
        src = inspect.getsource(pipeline.build_asset)
        self.assertIn("unique_asset_id", src)
        self.assertNotIn("asset_id_for", src)


class TestCompareArgs(unittest.TestCase):
    @staticmethod
    def _graph(args):
        return args[args.index("-filter_complex") + 1]

    def test_portrait_tiles_go_side_by_side_and_carry_no_ffmpeg_prefix(self):
        """`ffmpeg.run` 自带 `ffmpeg -hide_banner -loglevel error -y`。参数构造口
        再带一遍，输出文件名就会被当成第二个 `-i` 之后的位置参数。"""
        from kinema.control.io import stack_args
        a = stack_args("/w/x_work/control/c.mp4",
                       [("/a.mp4", 1.0, 5), ("/b.mp4", 1.0, 5), ("/c.mp4", 0, 5)],
                       canvas=(720, 1280), tile=720, fps=24.0, audio_from=0)
        self.assertNotEqual(a[0], "ffmpeg")
        self.assertEqual(a.count("-i"), 3)
        # 竖片横着排：各格尺寸必须一模一样（hstack 要求同高），高钉在 tile 上
        self.assertIn("hstack=inputs=3", self._graph(a))
        sizes = re.findall(r"scale=(\d+):(\d+)", self._graph(a))
        self.assertEqual(len(sizes), 3)
        self.assertEqual(len(set(sizes)), 1, f"各格不等大: {sizes}")
        self.assertEqual(sizes[0][1], "720")
        # 帧率不一致时 hstack 按最快那一路补帧：时长对而帧数翻倍，逐帧比对失真
        self.assertEqual(self._graph(a).count("fps=24"), 3)
        # 各路起点不同（源片与控制段从区间起裁，成片本就是那一段）——裁在这里做，
        # 不落中间文件；先裁一遍再拼一遍等于每格编码两次
        self.assertEqual(a[:8], ["-ss", "1.000", "-t", "5.000", "-i", "/a.mp4",
                                 "-ss", "1.000"])
        # `?` 让没有音轨的那一路照常出片——对照片是给人看的，静音不是失败
        self.assertIn("0:a?", a)

    def test_landscape_tiles_stack_downwards_and_pin_the_other_axis(self):
        """横画幅并排会拼成 32:9 的一条带子。方向一换，归一的那一维也得换边——
        `vstack` 要求各路同宽，照旧归一高度的话 ffmpeg 直接拒。"""
        from kinema.control.io import stack_args
        a = stack_args("/w/x_work/control/c.mp4", [("/a.mp4", 0, 5), ("/b.mp4", 0, 5)],
                       canvas=(1920, 1080), tile=720, fps=24.0)
        self.assertIn("vstack=inputs=2", self._graph(a))
        sizes = re.findall(r"scale=(\d+):(\d+)", self._graph(a))
        self.assertEqual(len(set(sizes)), 1, f"各格不等大: {sizes}")
        self.assertEqual(sizes[0][0], "720", "竖摞要各路同宽，钉的是宽")

    def test_stack_without_audio_maps_no_audio_stream(self):
        from kinema.control.io import stack_args
        a = stack_args("/w/x_work/c.mp4", [("/a.mp4", 0, 3), ("/b.mp4", 0, 3)],
                       canvas=(1080, 1080), tile=540, fps=30.0)
        self.assertNotIn("-c:a", a)

    def test_crop_fit_applies_the_segment_crop_to_every_tile(self):
        """crop 贴合的段只保留画布比例的中央区域；对照片各格按 pad 装原片全幅的话，
        看到的是模型没收到的画面。贴合滤镜与裁段同一条 `fit_filter`。"""
        from kinema.control.io import fit_filter, stack_args
        a = stack_args("/w/x_work/c.mp4", [("/a.mp4", 0, 3), ("/b.mp4", 0, 3)],
                       canvas=(1920, 1080), tile=720, fps=24.0, fit="crop")
        g = self._graph(a)
        self.assertEqual(g.count("force_original_aspect_ratio=increase,crop="), 2)
        self.assertNotIn("pad=", g)
        self.assertEqual(fit_filter("crop", 1920, 1080).split(",")[0],
                         "scale=1920:1080:force_original_aspect_ratio=increase")

    def test_clip_of_the_other_orientation_gets_its_own_row_or_column(self):
        """竖拍素材配 16:9 成片：成片塞进竖格里只剩中间一条细画面，得另起一行、
        宽对齐整行；横片竖摞时则贴到右侧、高对齐整列。"""
        from kinema.control.io import stack_args
        a = stack_args("/w/x_work/c.mp4", [("/a.mp4", 1, 5), ("/b.mp4", 1, 5)],
                       canvas=(720, 1280), tile=720, fps=24.0, tail=("/clip.mp4", 0, 5))
        g = self._graph(a)
        self.assertIn("hstack=inputs=2[m]", g, "两格竖片仍横排在主拼接里")
        self.assertIn("[2:v]fps=24.000000,scale=808:-2,setsar=1[tl]", g,
                      "成片宽对齐整行（2×404），等比缩放不裁不补")
        self.assertIn("[m][tl]vstack=inputs=2[v]", g, "另起一行")
        self.assertEqual(a.count("-i"), 3)
        a = stack_args("/w/x_work/c.mp4", [("/a.mp4", 0, 5), ("/b.mp4", 0, 5)],
                       canvas=(1920, 1080), tile=720, fps=24.0, tail=("/clip.mp4", 0, 5))
        g = self._graph(a)
        self.assertIn("vstack=inputs=2[m]", g)
        self.assertIn("scale=-2:808,setsar=1[tl]", g, "竖的成片高对齐整列（2×404）")
        self.assertIn("[m][tl]hstack=inputs=2[v]", g, "贴到右侧")


@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
class TestCompareBuild(_Base):
    def test_before_the_clip_it_is_a_two_up_that_carries_the_source_audio(self):
        """出片前审看的是二合一，**不是那条哑的控制段**。

        盘上的段落带源片同区间的音轨——审看要听的正是源片的原始节奏：这一段起没起在
        拍点上，光看深度浮雕判不出来。发给模型的是它的无声副本：native 章的声音由模型
        生成，把实拍背景音一并发过去是拿账单赌模型不拿它做文章。
        """
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        adir = control.asset_dir(p, aid)
        for name in ("control.mp4", "source.mp4"):     # 真素材这两份都带源音轨
            _clip(adir / name, dur=12.0, fps=24, audio=True)
        control.bind_shot(p, 1, aid, start=0.0, end=5.0)
        seg = p.shots[0]["control"]
        self.assertTrue(_has_audio(seg), "盘上的段落带源片音轨")
        sent = control.send_path(seg)
        self.assertNotEqual(sent, seg)
        self.assertTrue(sent.endswith("_mute.mp4"))
        self.assertFalse(_has_audio(sent), "发给模型的那一份必须是哑的")
        self.assertEqual(_probe_wh(sent), _probe_wh(seg), "副本只去声，画面原样")
        dst = control.build_shot_compare(p, p.shots[0])
        self.assertEqual(dst.name, "shot_1_compare2.mp4")
        self.assertTrue(_has_audio(dst), "审看件必须带源片音轨")

    def test_rebinding_onto_a_silent_source_drops_the_stale_mute_copy(self):
        """发送按副本优先：换绑到无声素材后，上一条素材留下的副本就是另一段运动。"""
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        loud = self._fake_asset(p, "loud-0001", seconds=12.0)
        _clip(control.asset_dir(p, loud) / "control.mp4", dur=12.0, fps=24, audio=True)
        quiet = self._fake_asset(p, "quiet-0001", seconds=12.0)
        control.bind_shot(p, 1, loud, start=0.0, end=5.0)
        seg = p.shots[0]["control"]
        self.assertTrue(control.send_path(seg).endswith("_mute.mp4"))
        control.unbind_shot(p, 1)
        control.bind_shot(p, 1, quiet, start=0.0, end=5.0)
        self.assertEqual(control.send_path(seg), seg, "无声段落原样发，旧副本已清")
        self.assertFalse(_has_audio(seg))
        self.assertEqual(control.send_path("https://cdn/x/shot_1_control.mp4"),
                         "https://cdn/x/shot_1_control.mp4", "URL 形式透传")

    def test_tiles_take_the_footage_shape_not_the_chapter_canvas(self):
        """竖片装进 16:9 画布后 68% 的像素是补出来的黑边——那层补边是投递格式，
        摞进对照片就是几格黑底中间几条细人影。方向随素材画幅走：竖片横着排。"""
        from kinema.ffmpeg import probe_json
        from kinema.models import ConfigStore
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        adir = control.asset_dir(p, aid)
        for name in ("control.mp4", "source.mp4"):        # 竖素材
            _clip(adir / name, dur=12.0, w=180, h=320, fps=24, audio=True)
        # 16:9 画布：发出去的那一段被贴合成横的，对照片不该跟着横
        control.bind_shot(p, 1, aid, start=0.0, end=5.0, store=ConfigStore.load(None))
        seg = next(x for x in probe_json(p.shots[0]["control"])["streams"]
                   if x["codec_type"] == "video")
        self.assertGreater(int(seg["width"]), int(seg["height"]), "发出去的那份仍按画布贴合")
        v = next(x for x in probe_json(control.build_shot_compare(p, p.shots[0]))["streams"]
                 if x["codec_type"] == "video")
        w, hgt = int(v["width"]), int(v["height"])
        self.assertEqual(hgt, 720, "竖片横着排，各格同高")
        self.assertLess(w, 2 * hgt, f"该是两格竖片并排，拿到的是 {w}×{hgt}")

    def test_landscape_footage_stacks_downwards(self):
        """横画幅并排会拼成 32:9 的一条带子，灯箱的播放位是定宽的——画幅越扁，
        它能给出的高度越少，每格反而比换个方向摞时小一半。"""
        from kinema.ffmpeg import probe_json
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        _clip(control.asset_dir(p, aid) / "source.mp4", dur=12.0, w=320, h=180,
              fps=24, audio=True)
        control.bind_shot(p, 1, aid, start=0.0, end=5.0)
        v = next(x for x in probe_json(control.build_shot_compare(p, p.shots[0]))["streams"]
                 if x["codec_type"] == "video")
        w, hgt = int(v["width"]), int(v["height"])
        self.assertGreater(hgt, w, f"横画幅该竖着摞，拿到的是 {w}×{hgt}")
        self.assertEqual(w, 720, "竖摞时归一的是宽——各路不同宽 vstack 直接拒")

    def test_three_up_tiles_are_equal_width_and_same_length(self):
        """三格不等宽的话，看到的第一件事会是「三格不一样大」而不是运动对不对。
        成片按章节画布出，与素材画幅不一致时 pad 进同一格。"""
        from kinema.ffmpeg import probe_frames
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        adir = control.asset_dir(p, aid)
        for name in ("control.mp4", "source.mp4"):     # 竖素材 → 三格横着排
            _clip(adir / name, dur=12.0, w=180, h=320, fps=24, audio=True)
        control.bind_shot(p, 1, aid, start=1.0, end=6.0)
        # 成片段替身：画布尺寸与控制段一致，扮演 gen-video 的产物
        ctl = Path(p.shots[0]["control"])
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(ctl)],
            capture_output=True, text=True).stdout.strip().split(",")
        clip = control.control_dir(p) / "clip_stub.mp4"
        _clip(clip, dur=5.0, w=int(info[0]), h=int(info[1]), fps=24, audio=False)
        p.shots[0]["clip"] = str(clip)

        # 先出两格再出三格：格宽必须一模一样——「出片前后看到的两格是同一个东西，
        # 只是多了一格」。只查合成宽能不能被 3 整除是查不出来的（398+404+404 照样能）
        p.shots[0].pop("clip")
        two = _probe_wh(control.build_shot_compare(p, p.shots[0]))
        p.shots[0]["clip"] = str(clip)
        dst = control.build_shot_compare(p, p.shots[0])
        three = _probe_wh(dst)
        self.assertEqual(dst.name, "shot_1_compare3.mp4")
        self.assertEqual(three[1], two[1], "各格等高才排得成一行")
        self.assertEqual(three[0] * 2, two[0] * 3, f"格宽不一致: 两格{two} 三格{three}")
        # 帧数以控制段为准——那是真发给模型的那一份，三格逐帧对得上才比得出运动
        self.assertEqual(probe_frames(dst), probe_frames(ctl))
        # 音轨恒取源片那一路：成片段替身是哑的，成片本来也常常没有声音，
        # 而这张对照片要听的是原始节奏
        self.assertTrue(_has_audio(dst), "三合一的音轨要取源片段那一路")

    def test_crop_bound_shot_compares_the_cropped_region(self):
        """`--fit crop` 的镜：前两格按裁好那段的画幅套同一条裁切，画幅随之取章节画布
        （竖片裁成横的 → 横画幅竖着摞），帧数仍与发出去的段守恒。"""
        from kinema.ffmpeg import probe_frames
        from kinema.models import ConfigStore
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        adir = control.asset_dir(p, aid)
        for name in ("control.mp4", "source.mp4"):        # 竖素材
            _clip(adir / name, dur=12.0, w=180, h=320, fps=24, audio=True)
        control.bind_shot(p, 1, aid, start=0.0, end=5.0, fit="crop",
                          store=ConfigStore.load(None))
        seg_w, seg_h = _probe_wh(p.shots[0]["control"])
        self.assertGreater(seg_w, seg_h, "16:9 画布下裁出的段是横的")
        dst = control.build_shot_compare(p, p.shots[0])
        w, hgt = _probe_wh(dst)
        self.assertEqual(w, 720, "横画幅竖着摞，归一的是宽")
        self.assertEqual(hgt, 2 * (int(round(seg_h * 720 / seg_w / 2)) * 2),
                         "两格都是画布比例——原片全幅没有进对照片")
        self.assertEqual(probe_frames(dst), probe_frames(p.shots[0]["control"]))

    def test_landscape_clip_under_portrait_footage_drops_to_a_second_row(self):
        """竖拍素材 + 16:9 成片：三合一的成片另起一行、宽对齐两格竖片那一行。"""
        from kinema.ffmpeg import probe_frames
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        adir = control.asset_dir(p, aid)
        for name in ("control.mp4", "source.mp4"):     # 竖素材
            _clip(adir / name, dur=12.0, w=180, h=320, fps=24, audio=True)
        control.bind_shot(p, 1, aid, start=0.0, end=5.0)
        clip = control.control_dir(p) / "clip_stub.mp4"
        _clip(clip, dur=5.0, w=320, h=180, fps=24, audio=False)      # 横屏成片
        p.shots[0]["clip"] = str(clip)
        dst = control.build_shot_compare(p, p.shots[0])
        self.assertEqual(dst.name, "shot_1_compare3.mp4")
        w, hgt = _probe_wh(dst)
        self.assertEqual(w, 2 * 404, "整行宽 = 两格竖片之和，成片没有挤进第三格")
        self.assertEqual(hgt, 720 + 454, "成片按整行宽等比缩放后落在第二行")
        self.assertEqual(probe_frames(dst), probe_frames(p.shots[0]["control"]))
        self.assertTrue(_has_audio(dst))


@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
class TestSoundtrack(_Base):
    """`control_bgm`：源片同一区间的音轨作成片的 BGM，偏移按时间轴、区间按绑定。"""

    @staticmethod
    def _mean_db(path, start, seconds) -> float:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(seconds),
                            "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        return float(m.group(1)) if m else -100.0

    def _bound_project(self):
        p = self._project(shots=[{"id": 1, "dur": 5.0, "narration": "", "image": "s.png"},
                                 {"id": 2, "dur": 5.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p, seconds=12.0)
        _clip(control.asset_dir(p, aid) / "source.mp4", dur=12.0, fps=24, audio=True)
        rec = control.read_asset(p, aid)
        rec["source"]["audio"] = True
        control.assets.write_asset(p, aid, rec)
        control.bind_shot(p, 2, aid, start=1.0, end=6.0)
        return p

    def test_bed_places_the_bound_segment_at_the_shots_timeline_offset(self):
        p = self._bound_project()
        segs = control.soundtrack_segments(p)
        self.assertEqual([(at, start, sec, lag) for at, _src, start, sec, lag in segs],
                         [(5.0, 1.0, 5.0, 0.0)], "镜 2 从 5s 起，取源片 1~6s，没量过对拍即不平移")
        out = p.subdir("audio") / "bgm.wav"
        r = control.build_soundtrack(p, out)
        self.assertEqual((r["segments"], r["seconds"]), (1, 10.0))
        self.assertLess(self._mean_db(out, 0, 4.5), -80, "未绑定的镜 1 是静音")
        self.assertGreater(self._mean_db(out, 5.5, 4), -40, "镜 2 的窗口里有源片音轨")

    def test_signature_changes_when_the_interval_moves(self):
        p = self._bound_project()
        before = control.soundtrack_signature(control.soundtrack_segments(p))
        control.bind_shot(p, 2, p.shots[1]["gen"]["control"]["asset"], start=2.0, end=7.0)
        self.assertNotEqual(before, control.soundtrack_signature(control.soundtrack_segments(p)))

    def test_bgm_gate_names_the_source_track_before_asking_about_the_library(self):
        """native 章绑了带音轨的控制视频却没表态 `control_bgm`：闸先把源片音轨这条路报出来，
        只提曲库会把人引到一条与动作无关的曲子上。没有带音轨的绑定镜就不提。"""
        from types import SimpleNamespace
        from kinema import cli
        from kinema.models import ConfigStore

        def gate(p):
            buf = _io.StringIO()
            with mock.patch("sys.stdin") as stdin, contextlib.redirect_stdout(buf):
                stdin.isatty.return_value = False
                cli._bgm_gate(p, ConfigStore.load(None), SimpleNamespace(bgm=None))
            return buf.getvalue()

        out = gate(self._bound_project())
        self.assertIn("control_bgm: true", out)
        self.assertIn("assemble --bgm", out, "曲库那条路照旧报")
        self.assertNotIn("control_bgm: true", gate(self._project()), "没绑源片就不提")

    def test_bed_shifts_with_an_applied_lag_only(self):
        """够格的对拍偏移平移配乐起点并进指纹；不够格的偏移记着但不用。"""
        from kinema.control import soundtrack
        p = self._bound_project()
        rec = p.shots[1]["gen"]["control"]
        base = control.soundtrack_signature(control.soundtrack_segments(p))
        rec["sync"] = {"lag": 0.25, "corr": 0.12, "applied": False}
        self.assertEqual(control.soundtrack_segments(p)[0][4], 0.0)
        self.assertEqual(control.soundtrack_signature(control.soundtrack_segments(p)), base)
        rec["sync"] = {"lag": 0.25, "corr": 0.8, "applied": True}
        segs = control.soundtrack_segments(p)
        self.assertEqual(segs[0][4], 0.25)
        self.assertNotEqual(control.soundtrack_signature(segs), base, "偏移变了是另一段音乐")
        self.assertEqual(soundtrack.cut_start(1.0, 0.25), 0.75, "成片晚 0.25s，音乐早 0.25s 起")
        self.assertEqual(soundtrack.cut_start(0.1, 0.25), 0.0, "早不过源片开头")
        self.assertEqual(soundtrack.cut_start(1.0, -0.25), 1.25, "成片早了则音乐晚起")

    def test_control_bgm_takes_the_native_bgm_bus_and_respects_the_burn_gate(self):
        from kinema.pipeline import compose
        p = self._project(control_bgm=True)
        self.assertTrue(compose.use_bgm_for(p))
        p.data["native_voiceover"] = True
        self.assertFalse(compose.use_bgm_for(p), "混烧已占住母线，源片音轨同样让路")

    def test_program_music_is_neither_ducked_nor_notched(self):
        """源片音轨是这一章的主音乐。走缺省的配乐链会被压到 0.3、挖掉 2 kHz、再被
        环境声触发闪避，末级只能推 +12 dB 靠限幅器补回来。"""
        import inspect
        from kinema.pipeline import compose, mixdown
        self.assertTrue(compose.bgm_is_program(self._project(control_bgm=True)))
        self.assertFalse(compose.bgm_is_program(self._project(native_bgm=True)),
                         "曲库 BGM 仍是床")
        tbl = mixdown.InputTable("silent.mp4")
        na = mixdown.clip_audio_track(tbl, dur=12.0)
        bg = mixdown.bgm_track(tbl, "bgm.mp3", dur=12.0, ducked=False)
        self.assertEqual(mixdown.premix_graph(tbl, narration=na, bgm=bg, duck=False), "amix")
        graph = ";".join(tbl.audio)
        self.assertNotIn("sidechaincompress", graph)
        self.assertNotIn(mixdown.VOICE_POCKET_EQ, graph)
        self.assertIn(f"volume={mixdown.BGM_GAIN_SOLO}", graph)
        src = inspect.getsource(compose.build)
        self.assertIn("ducked=bool(narr_label) and not program", src)
        self.assertIn("duck=not program", src)
        # 原生音退居环境床：它 -1 dBTP 的脚步瞬态会让末级限幅器随每一步把音乐按下去
        self.assertIn("gain=mixdown.NATIVE_BED_GAIN if program else 1.0", src)
        tbl2 = mixdown.InputTable("silent.mp4")
        mixdown.clip_audio_track(tbl2, dur=12.0, gain=mixdown.NATIVE_BED_GAIN)
        self.assertIn(f"volume={mixdown.NATIVE_BED_GAIN:g},", tbl2.audio[0])
        tbl3 = mixdown.InputTable("silent.mp4")
        mixdown.clip_audio_track(tbl3, dur=12.0)
        self.assertNotIn("volume=", tbl3.audio[0], "缺省仍是 0 dB 主轨")


def _flash_clip(out: Path, *, shift=0.0, dur=6.0, fps=24) -> Path:
    """黑底上三次白块闪现的合成片；`shift` 把三次闪现整体后移，即人为造出的成片滞后。"""
    win = "+".join(f"between(t,{a + shift:.3f},{b + shift:.3f})"
                   for a, b in ((1.0, 1.25), (2.5, 2.6), (4.0, 4.4)))
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s=160x90:r={fps}:d={dur}",
                    "-vf", f"drawbox=x=40:y=20:w=80:h=50:color=white:t=fill:enable='{win}'",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                   capture_output=True, check=True)
    return out


class TestSync(_Base):
    """运动对拍：量的是成片相对控制段的**整体**偏移，量不准就不动配乐。"""

    def test_cross_lag_sign_convention(self):
        from kinema.control import sync
        a = [0.0] * 40
        a[10] = 1.0
        b = [0.0] * 40
        b[13] = 1.0
        k, corr = sync.cross_lag(a, b, max_lag=6)
        self.assertEqual(k, 3, "b 的事件晚 3 帧即 b 滞后，偏移为正")
        self.assertGreater(corr, 0)
        self.assertEqual(sync.cross_lag(b, a, max_lag=6)[0], -3)
        # 只在窗口边缘两三个点上重合的偏移不取：那点偶然相关会冒充峰值
        far_a = [1.0] + [0.0] * 9
        far_b = [0.0] * 8 + [1.0, 0.0]
        self.assertNotEqual(sync.cross_lag(far_a, far_b, max_lag=9)[0], 8)

    @unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
    def test_estimate_lag_recovers_a_time_shift(self):
        from kinema.control import sync
        a = _flash_clip(self.tmp / "a.mp4")
        b = _flash_clip(self.tmp / "b.mp4", shift=0.25)
        r = sync.estimate_lag(a, b, seconds=6.0)
        self.assertAlmostEqual(r["lag"], 0.25, delta=1.5 / 24)
        self.assertGreater(r["corr"], 0.9)
        self.assertTrue(r["applied"])
        self.assertAlmostEqual(sync.estimate_lag(b, a, seconds=6.0)["lag"], -0.25, delta=1.5 / 24)

    @unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
    def test_flat_motion_yields_nothing(self):
        """整段没有运动起伏（纯黑）就没有可对的拍，返回 None 而不是一个偶然的偏移。"""
        from kinema.control import sync
        flat = self.tmp / "flat.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=24:d=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(flat)],
                       capture_output=True, check=True)
        self.assertIsNone(sync.estimate_lag(flat, _flash_clip(self.tmp / "a.mp4"), seconds=3.0))
        self.assertIsNone(sync.estimate_lag(self.tmp / "missing.mp4", flat, seconds=3.0))

    @unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg")
    def test_measure_sync_records_per_shot_and_clears_stale_values(self):
        p = self._project(shots=[{"id": 1, "dur": 6.0, "narration": "", "image": "s.png"}])
        aid = self._fake_asset(p)
        control.bind_shot(p, 1, aid, start=0.0, end=6.0)
        s = p.shots[0]
        s["control"] = str(_flash_clip(self.tmp / "ctl.mp4"))
        s["clip"] = str(_flash_clip(self.tmp / "clip.mp4", shift=0.25))
        out = control.measure_sync(p)
        self.assertEqual([r["shot"] for r in out], [1])
        rec = s["gen"]["control"]["sync"]
        self.assertAlmostEqual(rec["lag"], 0.25, delta=1.5 / 24)
        self.assertTrue(rec["applied"])
        self.assertIn("at", rec)
        s["clip"] = None
        self.assertEqual(control.measure_sync(p), [])
        self.assertNotIn("sync", s["gen"]["control"], "成片不在了，上一版的偏移一并清掉")


class TestOssConfigLivesInSecrets(unittest.TestCase):
    """桶与区域走密钥链而不是 storage.yaml——后者随仓库分发，填进去就跟着提交。"""

    def test_bucket_and_region_resolve_from_env(self):
        import kinema.storage.media as media
        media._stores.clear()
        with mock.patch.dict(os.environ, {"KINEMA_OSS_BUCKET": "b-env",
                                          "KINEMA_OSS_REGION": "r-env",
                                          "KINEMA_OSS_ACCESS_KEY": "ak",
                                          "KINEMA_OSS_SECRET_KEY": "sk"}):
            cfg = media._media_config()
        self.assertEqual(cfg["bucket"], "b-env")
        self.assertEqual(cfg["region"], "r-env")
        media._stores.clear()

    def test_shipped_template_keeps_bucket_empty(self):
        """填了值的模板会把一个私人桶名提交进公共仓库。"""
        import yaml
        root = Path(__file__).resolve().parents[2]
        raw = yaml.safe_load((root / "config" / "storage.yaml").read_text(encoding="utf-8"))
        self.assertEqual(raw["media"]["bucket"], "")
        self.assertEqual(raw["media"]["region"], "")


if __name__ == "__main__":
    unittest.main()
