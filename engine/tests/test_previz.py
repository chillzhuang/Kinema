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

"""previz（3D 预演）登记与 V2V 通路守卫。

守的是四条最贵的不变量（每一条错了都要到成片阶段才被发现）：

1. **previz 绝不写进 `shots[].clip`** —— compose 把 clip 当最终成片素材直接播，
   写进去就是把无材质灰模当成片交付。
2. **首帧覆盖 `image` 是有条件的** —— 该镜已有图时默认不覆盖（灰模盖精修图不可逆），
   且覆盖时必须走 supply 那条轨（归档旧版 / provider=supplied / 落待审 / 版本栈）。
3. **previz 末帧优先于 `--chain` 下一镜图**，且 **V2V 开启时一帧都不发**
   （seedance V2V 分支根本没有 last_frame 槽）。
4. **V2V 是 opt-in、只在 native、且必须 provider 真支持** —— `generate(**kwargs)`
   会静默吞掉不支持的 `reference_video`，静默降级 = 钱照花结果全错。

另有两条口径锁步：previz 时长钳制与 `SeedanceProvider.billable_seconds` 逐值对拍
（previz 与成片 1:1 是「成片跟随预演」的前提）；`director_catalog()` 与前端
`director/rig.js` 的注册表键逐一对齐（目录是数据驱动前端的唯一真源）。
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import previz
from kinema.errors import ProjectError
from tests.support import LocalBackendEnv, fake_path

ASSETS = Path(__file__).resolve().parents[1] / "kinema" / "studio_app"
RIG_JS = ASSETS / "director" / "rig.js"


def _have_ffmpeg() -> bool:
    from shutil import which
    return which("ffmpeg") is not None and which("ffprobe") is not None


def _lavfi_clip(out: Path, *, dur=5.0, w=320, h=180, fps=24) -> Path:
    """用 lavfi 合成一段可解码的小视频（**零素材入仓**，与既有冒烟层同款）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={dur}",
         "-pix_fmt", "yuv420p", "-t", str(dur), str(out)],
        capture_output=True, check=True)
    return out


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


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
        """建一个最小章节（工作区形状 <ws>/<pid>/chapters/<cid>.json）并 load。"""
        from kinema.project import Project
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "profile": "anime", "motion": "native",
               "aspect": "16:9", "shots": shots or [
                   {"id": 1, "dur": 5.0, "narration": "台词一", "video_prompt": "转身"}]}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        # 项目文档必须在位：Studio 写路径的删态总闸经 Workspace.get_project 解析
        # 项目——章节永远隶属于登记过的项目，fixture 与生产同形
        pfile = cdir.parent / "project.json"
        if not pfile.is_file():
            pfile.write_text(json.dumps(
                {"id": "p1", "title": "p1", "chapters": [{"id": "ch01", "order": 1}]},
                ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)


# ============================================================ 一、钳制口径锁步
class TestDurationLockstep(unittest.TestCase):
    def test_snap_duration_matches_seedance_native_billing(self):
        """previz 渲多久 = Seedance 出多长，差一秒就是运动被拉伸/截断。

        两处各写一份钳制逻辑迟早分叉（一处改 4~15、另一处还是 2~12），
        故逐值对拍**真 provider**，而不是抄一份常数。
        """
        from kinema.providers.video.seedance import SeedanceProvider

        class _S:
            def secret(self, *a, **k):
                return "k"

        prov = SeedanceProvider({}, _S())
        for d in (0, 0.4, 1, 3.4, 3.6, 4, 5.5, 9.2, 14.6, 15, 30, 100):
            with self.subTest(dur=d):
                self.assertEqual(previz.snap_duration(d),
                                 prov.billable_seconds(d, dubbed=False))


# ============================================================ 二、体检
@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg/ffprobe")
class TestInspect(_Base):
    def test_good_clip_passes_with_no_warning(self):
        rep = previz.inspect_previz(_lavfi_clip(self.tmp / "ok.mp4", dur=5))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["warn"], [])
        self.assertAlmostEqual(rep["duration"], 5.0, delta=0.3)
        self.assertEqual((rep["width"], rep["height"]), (320, 180))

    def test_out_of_range_duration_only_warns(self):
        """2~15s 是 V2V 的限额；previz 同时还担着首/末帧与免费预览两个用途，
        为一个**可能根本不开**的通道拦死另外两个是错的——只告警。"""
        rep = previz.inspect_previz(_lavfi_clip(self.tmp / "short.mp4", dur=1))
        self.assertTrue(rep["ok"], "时长越界不许硬拦")
        self.assertIn("duration", [w["code"] for w in rep["warn"]])

    def test_unreadable_is_the_only_hard_fail(self):
        bad = self.tmp / "notavideo.mp4"
        bad.write_text("这其实是文本", encoding="utf-8")
        rep = previz.inspect_previz(bad)
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["hard_fail"])

    def test_probe_exception_never_bubbles(self):
        """坏容器统一转成体检结论——裸栈冒泡会让整条登记以看不懂的方式挂掉。"""
        with mock.patch("kinema.ffmpeg.probe_json", side_effect=RuntimeError("boom")):
            rep = previz.inspect_previz(_lavfi_clip(self.tmp / "x.mp4", dur=3))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["hard_fail"][0]["code"], "unreadable")


# ============================================================ 三、登记
@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg/ffprobe")
class TestRegister(_Base):
    def _register(self, project, **kw):
        src = _lavfi_clip(self.tmp / "previz_src.mp4", dur=5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            r = previz.register_previz(project, 1, src, **kw)
        return r, buf.getvalue()

    def test_registers_four_artifacts_and_never_touches_clip(self):
        project = self._project()
        r, _ = self._register(project, camera_preset="push_in")
        s = project.shots[0]
        self.assertTrue(Path(s["previz"]).is_file())
        self.assertTrue(Path(s["last_frame_ref"]).is_file())
        self.assertEqual(s["camera_preset"], "push_in")
        from kinema.pipeline import camera
        self.assertEqual(s["camera"], camera.CAMERA_PRESETS["push_in"]["phrase"])
        # 头号不变量：previz 绝不写进 clip（compose 会把 clip 当成片直接播）
        self.assertNotIn("clip", s)
        self.assertNotIn("clips", s)
        self.assertNotIn("previz", str(s.get("clip") or ""))
        self.assertTrue(r["image_registered"], "该镜本来无图 → auto 档应登记首帧")

    def test_first_frame_goes_through_supply_lane(self):
        """首帧登记必须走 supply 同一条轨：provider=supplied + 落待审 + 版本栈。"""
        project = self._project()
        self._register(project)
        s = project.shots[0]
        self.assertTrue(Path(s["image"]).is_file())
        self.assertEqual(s["gen"]["image"]["provider"], "supplied")
        self.assertEqual(s["review"]["image"]["state"], "wfa", "登记后必须落待审")
        self.assertEqual(s["status"], "done")

    def test_auto_never_overwrites_an_existing_image(self):
        """已有精修图时 auto 档不覆盖——灰模盖掉已生成的分镜图是不可逆体验事故。"""
        img = _png(self.tmp / "already.png")
        project = self._project([{"id": 1, "dur": 5.0, "image": str(img)}])
        r, out = self._register(project)
        self.assertFalse(r["image_registered"])
        self.assertEqual(project.shots[0]["image"], str(img))
        self.assertIn("默认不覆盖", out)
        # 但末帧/参考片/运镜照常挂上——「顺手登记首帧」失败不该让整条登记失败
        self.assertTrue(project.shots[0]["previz"])
        self.assertTrue(project.shots[0]["last_frame_ref"])

    def test_use_first_frame_forces_overwrite_and_archives_old(self):
        img = _png(self.tmp / "already.png")
        project = self._project([{"id": 1, "dur": 5.0, "image": str(img)}])
        r, _ = self._register(project, use_first_frame=True)
        self.assertTrue(r["image_registered"])
        self.assertEqual(r["archived"], 1, "覆盖前旧图必须进版本栈（可回滚）")
        self.assertNotEqual(project.shots[0]["image"], str(img))

    def test_locked_image_auto_skips_but_explicit_raises(self):
        """done 锁定镜：auto 档照常登记其余三件套，显式要覆盖才报错并给解锁路径。"""
        img = _png(self.tmp / "locked.png")
        project = self._project([{"id": 1, "dur": 5.0, "image": str(img),
                                  "review": {"image": {"state": "done"}}}])
        r, out = self._register(project)
        self.assertFalse(r["image_registered"])
        self.assertIn("默认不覆盖", out)
        self.assertTrue(project.shots[0]["previz"], "首帧没登记不该拖垮整条 previz 登记")
        self.assertEqual(project.shots[0]["image"], str(img))
        with self.assertRaises(ProjectError) as ctx:
            self._register(project, use_first_frame=True)
        self.assertIn("retake", str(ctx.exception))

    def test_locked_shot_without_image_reports_the_lock(self):
        """锁定但尚无图（表态先于产物）时，auto 会去登记首帧并撞上锁——原因要说清楚
        是「锁定」而不是「已有图」，否则用户按后者的解法（删图）永远解不开。"""
        project = self._project([{"id": 1, "dur": 5.0,
                                  "review": {"image": {"state": "done"}}}])
        r, out = self._register(project)
        self.assertFalse(r["image_registered"])
        self.assertIn("锁定", out)
        self.assertIn("retake", out)

    def test_transition_and_omitted_shots_are_rejected(self):
        project = self._project([
            {"id": 1, "kind": "transition", "dur": 1.0, "transition": {"type": "fade"}},
            {"id": 2, "dur": 4.0, "review": {"shot": {"state": "omt"}}}])
        src = _lavfi_clip(self.tmp / "p.mp4", dur=4)
        with self.assertRaises(ProjectError):
            previz.register_previz(project, 1, src)
        with self.assertRaises(ProjectError):
            previz.register_previz(project, 2, src)

    def test_unknown_camera_preset_raises_instead_of_silently_dropping(self):
        """静默忽略的后果是「点了名的运镜预设没进提示词」且零提示——必须报错。"""
        project = self._project()
        with self.assertRaises(ProjectError) as ctx:
            previz.register_previz(project, 1, _lavfi_clip(self.tmp / "p.mp4", dur=4),
                                   camera_preset="不存在的运镜")
        self.assertIn("未知运镜 preset", str(ctx.exception))

    def test_inspect_runs_before_any_artifact_is_written(self):
        """硬拦时工作目录里不留半成品，既有产物一个都没动（同 supply 的闸位纪律）。"""
        img = _png(self.tmp / "keep.png")
        project = self._project([{"id": 1, "dur": 5.0, "image": str(img)}])
        bad = self.tmp / "bad.mp4"
        bad.write_text("文本改名", encoding="utf-8")
        with self.assertRaises(ProjectError):
            previz.register_previz(project, 1, bad)
        self.assertEqual(project.shots[0]["image"], str(img))
        self.assertNotIn("previz", project.shots[0])
        self.assertFalse((project.workdir / "previz").exists(),
                         "体检硬拦时连 previz 目录都不该建")

    def test_clear_drops_mount_but_keeps_artifacts_and_image(self):
        project = self._project()
        self._register(project, camera_preset="push_in")
        pz = Path(project.shots[0]["previz"])
        img = project.shots[0]["image"]
        r = previz.clear_previz(project, 1)
        self.assertEqual(sorted(r["dropped"]),
                         ["camera_preset", "last_frame_ref", "previz"])
        self.assertTrue(pz.is_file(), "只摘挂载不删文件")
        self.assertEqual(project.shots[0]["image"], img,
                         "分镜图一旦登记就归版本栈管，绝不从这里悄悄删")

    def test_previz_seconds_prefers_recorded_duration(self):
        project = self._project()
        self._register(project)
        s = project.shots[0]
        self.assertAlmostEqual(previz.previz_seconds(s), 5.0, delta=0.3)
        # 记录在 gen.previz.duration 里 → 不必 probe（全片报价时每镜 probe 是白等）
        with mock.patch("kinema.ffmpeg.probe_duration",
                        side_effect=AssertionError("不该再 probe")):
            previz.previz_seconds(s)


# ============================================================ 四、末帧优先 / V2V 闸
@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg/ffprobe")
class TestGenVideoWiring(_Base):
    """全部走 `stage_gen_video --dry-run`（零 API、零计费）验实发口径。"""

    def _run(self, project, **kw):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=True, **kw)
        return buf.getvalue()

    def _two_shots_with_previz(self, **over):
        i1, i2 = _png(self.tmp / "s1.png"), _png(self.tmp / "s2.png")
        project = self._project([{"id": 1, "dur": 5.0, "image": str(i1),
                                  "video_prompt": "转身"},
                                 {"id": 2, "dur": 5.0, "image": str(i2),
                                  "video_prompt": "抬头"}],
                                frame_chain=True, **over)
        src = _lavfi_clip(self.tmp / "pz.mp4", dur=5)
        with contextlib.redirect_stdout(io.StringIO()):
            previz.register_previz(project, 1, src, camera_preset="push_in",
                                   use_first_frame=False)
        return project

    def test_previz_last_frame_beats_chain_next_shot(self):
        """两者争同一个 last_frame 槽 → previz 末帧赢（它是这一镜自己的终态位姿）。"""
        out = self._run(self._two_shots_with_previz())
        self.assertIn("末帧=previz", out)
        self.assertNotIn("末帧=镜2", out)

    def test_dubbed_chapter_never_advertises_a_previz_last_frame(self):
        """dubbed 走参考媒体任务，末帧槽不存在——审阅口与 Envelope 都不得标它。"""
        out = self._run(self._two_shots_with_previz(motion="dubbed"))
        self.assertNotIn("末帧=previz", out)

    def test_sketch_guided_previz_shot_is_not_a_v2v_island(self):
        """guide=sketch 的镜 previz 不参与：逐镜任务型态与链图孤岛判定同一个谓词。"""
        from kinema.pipeline import framechain
        clip = _lavfi_clip(self.tmp / "g.mp4", dur=2)
        shot = {"id": 1, "previz": str(clip), "guide": "sketch",
                "sketch": {"beats": [{"action": "起"}]}}
        self.assertFalse(previz.v2v_shot(shot))
        self.assertFalse(framechain.island(shot, v2v=True))
        shot.pop("guide")
        self.assertTrue(previz.v2v_shot(shot))

    def test_v2v_is_opt_in_and_off_by_default(self):
        """默认不发参考视频——V2V 多计输入视频秒，静默开启 = 静默改成本。"""
        out = self._run(self._two_shots_with_previz())
        self.assertNotIn("参考视频", out)

    def test_v2v_flag_turns_it_on_and_suppresses_last_frame(self):
        out = self._run(self._two_shots_with_previz(), previz=True)
        self.assertIn("参考视频=previz", out)
        self.assertIn("V2V", out)
        self.assertNotIn("末帧=", out, "V2V 分支根本没有 last_frame 槽")

    def test_project_level_switch_equals_the_flag(self):
        out = self._run(self._two_shots_with_previz(previz_v2v=True))
        self.assertIn("参考视频=previz", out)

    def test_v2v_only_in_native_mode(self):
        """dubbed 的对口型音频与运动迁移互相牵制，未经小样验证不默认叠加。"""
        p = self._two_shots_with_previz(motion="dubbed")
        out = self._run(p, previz=True)
        self.assertIn("只在 native 模式生效", out)
        self.assertNotIn("参考视频=previz", out)

    def test_v2v_prompt_uses_the_v2v_contract_sentence(self):
        """契约句必须换 V2V 版：图是参考图不是首帧、且要点明「别抄灰模画风」。"""
        from kinema.pipeline import prompts
        out = self._run(self._two_shots_with_previz(), previz=True)
        self.assertIn(prompts.CONTRACT_V2V_ZH, out)
        self.assertNotIn(prompts.CONTRACT_FIRST_ZH, out.split("镜2")[0])

    def test_v2v_quote_includes_input_video_seconds(self):
        """报价含输入视频秒——不含就与账单差一整段 previz 的钱。"""
        plain = self._run(self._two_shots_with_previz())
        v2v = self._run(self._two_shots_with_previz(), previz=True)
        n_plain = int(re.search(r"共 \d+ 镜.*?≈ (\d+)s", plain, re.S).group(1))
        n_v2v = int(re.search(r"共 \d+ 镜.*?≈ (\d+)s", v2v, re.S).group(1))
        self.assertEqual(n_v2v - n_plain, 5, "镜1 的 5s previz 应计入输入侧")

    def test_dry_run_and_live_share_one_shot_plan(self):
        """`_shot_plan` 是 dry-run 与真发的唯一判据源——重算必然分叉。"""
        src = (Path(__file__).resolve().parents[1]
               / "kinema" / "cli.py").read_text(encoding="utf-8")
        body = src.split("def stage_gen_video")[1].split("\ndef ")[0]
        # 三处调用：dry-run 清单、closeup 预判镜的整批出板前置、主循环——三处都只
        # 消费 `_shot_plan` 的结论，别处不许再算末帧/V2V 判据
        self.assertEqual(len(re.findall(r"=\s*_shot_plan\(s,\s*prov0?\)", body)), 3,
                         "dry-run/closeup 出板前置/主循环各调一次，别处不许再算末帧/V2V 判据")
        # 除 `_shot_plan` 内部那一次外，不许再裸调 `_flf2v`——裸调等于绕过 V2V/previz
        # 三条优先级重算一份链态，两份判据必然分叉
        self.assertEqual(len(re.findall(r"=\s*_flf2v\(s\)", body)), 1)


# ============================================================ 五、场景文档 / Studio 层
class TestScene(_Base):
    def test_save_scene_replaces_wholesale_and_hashes_content(self):
        project = self._project()
        s1 = previz.save_scene(project, {
            "fps": 24,
            "actors": [{"id": "a1", "model": "mannequin_m", "path": "p1"}],
            "paths": [{"id": "p1", "points": [[0, 0, 0], [2, 0, 3]]}],
            "cameras": [{"id": "c1", "preset": "push_in"}],
            "cuts": [{"shot": 1, "camera": "c1", "t_in": 0, "t_out": 5}]})
        self.assertTrue(s1["scene_hash"].startswith("sha256:"))
        self.assertEqual(project.data["previz"]["cuts"][0]["shot"], 1)
        # 整体替换：删掉机位后不许还残留在文档里（半坏引用比丢数据更难查）
        s2 = previz.save_scene(project, {"fps": 24, "actors": [], "cuts": []})
        self.assertEqual(project.data["previz"].get("cameras"), None)
        self.assertNotEqual(s1["scene_hash"], s2["scene_hash"])

    def test_scene_hash_ignores_timestamps(self):
        """哈希只认编排内容——含时间戳就每次保存都变，「内容没变就不重渲」失效。"""
        scene = {"fps": 24, "actors": [{"id": "a1"}], "cuts": []}
        h1 = previz.scene_hash(scene)
        h2 = previz.scene_hash({**scene, "updated_at": "2099-01-01T00:00:00"})
        self.assertEqual(h1, h2)

    def test_scene_survives_engine_save(self):
        """长任务的旧内存副本不该把刚保存的编排冲掉——场景是 engine-managed，
        走的是普通字段路径，这里钉住它至少不会被 `Project.save` 自身丢掉。"""
        project = self._project()
        previz.save_scene(project, {"fps": 24, "cuts": [{"shot": 1}]})
        from kinema.project import Project
        again = Project.load(project.path)
        self.assertEqual(again.data["previz"]["cuts"], [{"shot": 1}])


@unittest.skipUnless(_have_ffmpeg(), "需要 ffmpeg/ffprobe")
class TestReel(_Base):
    """全片预演（各镜 previz → 一条长片）——它是**观看物**，越界即事故。"""

    def _chapter_with_previz(self, ids=(1, 2), **over):
        shots = [{"id": i, "dur": 5.0, "narration": f"台词{i}"} for i in ids]
        project = self._project(shots=shots, **over)
        for s in project.shots:
            if s.get("id") in ids:
                s["previz"] = str(_lavfi_clip(
                    previz.previz_dir(project) / f"shot_{s['id']}.mp4", dur=2.0))
        project.save()
        return project

    def test_reel_concatenates_in_shot_order_and_stream_copies(self):
        """同参数同源（控制台渲的全是同一套编码参数）必须走**流拷贝**：
        零重编码零画质损失。顺序恒按契约 `shots[]`，不是文件名字典序。"""
        project = self._chapter_with_previz((1, 2, 3))
        r = previz.build_reel(project)
        self.assertEqual(r["mode"], "copy")
        self.assertEqual([x["id"] for x in r["shots"]], [1, 2, 3])
        self.assertTrue(Path(r["file"]).is_file())
        self.assertAlmostEqual(r["duration"], 6.0, delta=0.4)
        self.assertEqual(r["skipped"], [])

    def test_reel_never_touches_clip_or_output(self):
        """reel 不是成片：既不能进 `shots[].clip`（compose 视 clip 为成片素材直接播），
        也不能进顶层 `output`（那是交付位）。"""
        project = self._chapter_with_previz()
        previz.build_reel(project)
        from kinema.project import Project
        again = Project.load(project.path)
        self.assertIsNone(again.data.get("output"))
        for s in again.shots:
            self.assertIsNone(s.get("clip"))

    def test_reel_pointer_survives_saving_the_scene(self):
        """指针**不进契约**：顶层 `previz` 是编排快照的整体替换区，写进去就会被
        下一次「保存编排」抹掉——这正是它必须由磁盘 sidecar 推导的理由。"""
        project = self._chapter_with_previz()
        previz.build_reel(project)
        previz.save_scene(project, {"fps": 24, "cuts": [{"shot": 1}]})
        info = previz.reel_info(project)
        self.assertIsNotNone(info, "保存一次编排就找不到全片预演了")
        self.assertTrue(Path(info["file"]).is_file())

    def test_reel_lands_outside_the_film_library_scan(self):
        """产物落 `<work>/previz/`——片库只扫 `*_work/output/*.mp4`，
        放错地方就会被当成一条成片收进片库（同 study 目录不带 `_work` 的教训）。"""
        project = self._chapter_with_previz()
        r = previz.build_reel(project)
        p = Path(r["file"])
        self.assertEqual(p.parent.name, previz.PREVIZ_SUBDIR)
        self.assertNotIn("output", p.parts)

    def test_reel_reports_which_shots_are_missing(self):
        """少了哪几镜必须逐条说清——「合出来了」不等于「全片都在里面」，
        静默漏镜会被读成「整场戏我已经看过一遍了」。"""
        project = self._chapter_with_previz((1, 3))
        project.data["shots"].insert(1, {"id": 2, "dur": 5.0, "narration": "没渲的镜"})
        project.data["shots"].append(
            {"id": 4, "kind": "transition", "dur": 1.0, "narration": "",
             "transition": {"type": "fade"}})
        project.data["shots"].append(
            {"id": 5, "dur": 5.0, "review": {"shot": {"state": "omt"}}})
        project.save()
        r = previz.build_reel(project)
        self.assertEqual([x["id"] for x in r["shots"]], [1, 3])
        self.assertEqual({x["id"]: x["why"] for x in r["skipped"]},
                         {2: "no_previz", 4: "transition", 5: "omt"})

    def test_reel_refuses_when_nothing_was_rendered(self):
        """一镜都没渲时报错要给出下一步，别产出一个 0 字节的空片子。"""
        project = self._project()
        with self.assertRaises(ProjectError) as cm:
            previz.build_reel(project)
        self.assertIn("渲染 previz", str(cm.exception))
        self.assertFalse(previz.reel_path(project).exists())

    def test_reel_reencodes_when_specs_differ(self):
        """掺进外部登记的片子（`previz register --file`，参数各异）时流拷贝会得到
        花屏/时长错乱，必须整体回退重编码归一。"""
        project = self._chapter_with_previz()
        odd = project.shots[1]
        odd["previz"] = str(_lavfi_clip(
            previz.previz_dir(project) / "外部.mp4", dur=2.0, w=640, h=360, fps=30))
        project.save()
        r = previz.build_reel(project)
        self.assertEqual(r["mode"], "reencode")
        self.assertEqual((r["width"], r["height"]), (320, 180), "应归一到众数规格")
        self.assertTrue(Path(r["file"]).is_file())

    def test_reel_normalizes_to_the_majority_spec_not_the_first_shot(self):
        """归一目标是**众数**：本仓库真实存在早期 Retina 未锁 pixelRatio 渲出的 4K
        遗留片，它一旦排在首位，「按第一镜归一」就会把整条 reel 拖成 4K。"""
        project = self._chapter_with_previz((1, 2, 3))
        project.shots[0]["previz"] = str(_lavfi_clip(     # 首镜是那条 2× 遗留片
            previz.previz_dir(project) / "legacy_2x.mp4", dur=2.0, w=640, h=360))
        project.save()
        r = previz.build_reel(project)
        self.assertEqual((r["width"], r["height"]), (320, 180),
                         "首镜的异常规格不该带偏整条片子")

    def test_reel_localizes_urls_before_probing(self):
        """OSS 模式下 `shots[].previz` 是 URL——不过 `ensure_local` 就会得出
        「一镜都没有」，而盘上每一镜都渲过。"""
        project = self._chapter_with_previz()
        real = [s["previz"] for s in project.shots]
        for s in project.shots:
            s["previz"] = "https://oss.example.com/" + Path(s["previz"]).name
        table = dict(zip((s["previz"] for s in project.shots), real))
        with mock.patch("kinema.storage.media.ensure_local",
                        side_effect=lambda v: table.get(v, v)):
            rows, skipped = previz.reel_inputs(project)
        self.assertEqual([x["id"] for x in rows], [1, 2])
        self.assertEqual(skipped, [])

    def test_reel_info_needs_both_video_and_manifest(self):
        """只剩半边（片子被删/清单被删）一律当没有——报个能播的 URL 指向不存在的
        文件，用户点开只会看到播放器报错。"""
        project = self._chapter_with_previz()
        previz.build_reel(project)
        self.assertIsNotNone(previz.reel_info(project))
        (previz.previz_dir(project) / previz.REEL_MANIFEST).unlink()
        self.assertIsNone(previz.reel_info(project))

    def test_scanner_derives_reel_from_disk(self):
        """scanner 视图必须来自磁盘（sidecar），且要带够「基于哪几镜、什么时候合的」
        ——只给一个能播的 URL，用户判断不出它是不是漏了刚渲的那一镜。"""
        from kinema.studio import scanner
        project = self._chapter_with_previz()
        previz.build_reel(project)
        work = Path(project.path).parent / f"{Path(project.path).stem}_work"
        v = scanner._previz_reel_view(work)
        self.assertEqual(v["shots"], [1, 2])
        self.assertTrue(v["video"].startswith("/media?path="))
        for k in ("built_at", "duration", "size", "name"):
            self.assertIsNotNone(v[k], f"视图缺 {k}")


class TestStudioLayer(_Base):
    """Studio 写路径与 CLI 同源：`_load → 领域模块 → save`，长任务走 jobs 子进程。"""

    def _ws(self):
        return self.tmp / "proj"

    def test_previz_save_goes_through_the_single_write_path(self):
        from kinema.studio import actions
        self._project()
        r = actions.previz_save(self._ws(), "p1", "ch01",
                                scene={"fps": 24, "cuts": [{"shot": 1}]})
        self.assertTrue(r["scene_hash"].startswith("sha256:"))
        self.assertEqual(r["cuts"], 1)
        d = json.loads((self._ws() / "p1" / "chapters" / "ch01.json")
                       .read_text(encoding="utf-8"))
        self.assertEqual(d["previz"]["cuts"], [{"shot": 1}])

    def test_v2v_switch_is_removed_not_set_false(self):
        """关掉就删字段而不是写 false——契约里留一堆 false 会让"没配过"和"关掉了"
        分不清，也让 diff 噪声化（同 effects override 的既定纪律）。"""
        from kinema.studio import actions
        self._project()
        actions.previz_set_v2v(self._ws(), "p1", "ch01", on=True)
        d = json.loads((self._ws() / "p1" / "chapters" / "ch01.json").read_text("utf-8"))
        self.assertIs(d["previz_v2v"], True)
        actions.previz_set_v2v(self._ws(), "p1", "ch01", on=False)
        d = json.loads((self._ws() / "p1" / "chapters" / "ch01.json").read_text("utf-8"))
        self.assertNotIn("previz_v2v", d)

    def test_v2v_switch_refused_while_clips_are_locked(self):
        """章级开关与 Gateway 同一张锁表：clip 已通过锁定时翻转 V2V 改变请求形态，拒绝。"""
        from kinema.errors import KinemaError
        from kinema.studio import actions
        self._project()
        cf = self._ws() / "p1" / "chapters" / "ch01.json"
        d = json.loads(cf.read_text("utf-8"))
        d["shots"][0]["review"] = {"clip": {"state": "done"}}
        cf.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(KinemaError):
            actions.previz_set_v2v(self._ws(), "p1", "ch01", on=True)
        self.assertNotIn("previz_v2v", json.loads(cf.read_text("utf-8")))
        actions.previz_set_v2v(self._ws(), "p1", "ch01", on=False)   # 值不变不拦

    def test_render_job_carries_the_locator_meta(self):
        """`meta` 是忙态定位名片——缺了它，分镜卡的「预演渲染中」遮罩挂不上去，
        刷新页面后忙态也对不上账（`/api/jobs` 按 project/chapter/shot 过滤）。"""
        from kinema.studio import actions, jobs
        self._project()
        with mock.patch.object(jobs, "spawn_cli",
                               side_effect=lambda a, **k: ("JID", k)[0]) as sp:
            r = actions.previz_render(self._ws(), "p1", "ch01", shot=1,
                                      fps=24, camera="push_in")
        self.assertEqual(r["job"], "JID")
        args, kw = sp.call_args
        self.assertEqual(args[0][:4], ["previz", "build", "--chapter", "p1/ch01"])
        self.assertIn("--camera", args[0])
        self.assertEqual(kw["meta"], {"project": "p1", "chapter": "ch01",
                                      "shot": "1", "kind": "previz"})

    def test_render_rejects_unknown_shot_before_spawning(self):
        from kinema.errors import KinemaError
        from kinema.studio import actions, jobs
        self._project()
        with mock.patch.object(jobs, "spawn_cli",
                               side_effect=AssertionError("不该起任务")):
            with self.assertRaises(KinemaError):
                actions.previz_render(self._ws(), "p1", "ch01", shot=99)

    def test_seedance_handoff_requires_a_registered_previz(self):
        from kinema.errors import KinemaError
        from kinema.studio import actions, jobs
        self._project()
        with mock.patch.object(jobs, "spawn_cli",
                               side_effect=AssertionError("不该起任务")):
            with self.assertRaises(KinemaError) as ctx:
                actions.previz_to_seedance(self._ws(), "p1", "ch01")
        self.assertIn("先在 3D 导演控制台", str(ctx.exception))

    def test_scanner_ships_previz_fields_without_confusing_it_with_clip(self):
        """分镜卡的成片位只认 `clip`；previz 必须是**另一个字段**下发。"""
        from kinema.models import ConfigStore
        from kinema.studio import scanner
        project = self._project()
        project.shots[0].update(previz=fake_path("shot_1.mp4"), camera_preset="push_in",
                                last_frame_ref=fake_path("shot_1_last.png"))
        project.data["previz_v2v"] = True
        project.save()
        d = scanner.chapter_detail(self._ws(), ConfigStore.load(None), "p1", "ch01")
        s = d["shots"][0]
        self.assertIn("previz", s)
        self.assertEqual(s["camera_preset"], "push_in")
        self.assertIsNone(s["clip"], "previz 绝不许出现在 clip 位")
        self.assertIs(d["previz_v2v"], True)


# ============================================================ 六、目录 ↔ 前端锁步
class TestDirectorCatalog(unittest.TestCase):
    def test_catalog_shape_and_json_safe(self):
        cat = previz.director_catalog()
        self.assertEqual(set(cat),
                         {"models", "actions", "props", "prop_groups", "limits"})
        json.dumps(cat, ensure_ascii=False, allow_nan=False)
        for m in cat["models"]:
            self.assertEqual(set(m), {"key", "label", "height", "build", "desc"})
            self.assertGreater(m["height"], 0)
        for a in cat["actions"]:
            self.assertEqual(set(a), {"key", "label", "loop", "speed", "desc"})
            self.assertIsInstance(a["loop"], bool)
        # 分组只影响检索路径，但每件道具都必须归得进某一组：落不进组的会掉进选择器
        # 的「其他」段，新加的体块因此在场景族里找不到
        known = {g["key"] for g in cat["prop_groups"]}
        for p in cat["props"]:
            self.assertEqual(set(p), {"key", "label", "group", "size", "desc"})
            self.assertEqual(len(p["size"]), 3)
            self.assertIn(p["group"], known)

    def test_catalog_is_a_copy(self):
        cat = previz.director_catalog()
        cat["models"][0]["label"] = "改坏了"
        self.assertNotEqual(previz.DIRECTOR_MODELS[0]["label"], "改坏了")

    def test_locomotion_actions_declare_a_speed(self):
        """位移类动作必须报内建速度——否则步态同步算不出 timeScale，人会飘着走。"""
        moving = {"walk", "run", "crawl", "fly", "crouch"}
        for a in previz.DIRECTOR_ACTIONS:
            with self.subTest(action=a["key"]):
                if a["key"] in moving:
                    self.assertGreater(a["speed"], 0)
                else:
                    self.assertEqual(a["speed"], 0)

    @unittest.skipUnless(RIG_JS.is_file(), "前端 rig.js 尚未落地")
    def test_frontend_rig_registry_locksteps_with_catalog(self):
        """目录是数据驱动前端的唯一真源——前端少一个 key 就是「选了没反应」。

        默认角色是**程序化灰模**（不是下载的 GLB），故资产一致性守卫的落点就是
        「引擎目录 ↔ rig.js 注册表」这一对。
        """
        js = RIG_JS.read_text(encoding="utf-8")

        def keys_of(block: str) -> set[str]:
            seg = js.split(f"/* @registry:{block} */")[1].split(f"/* @end:{block} */")[0]
            return set(re.findall(r"^\s{2}(\w+):", seg, re.M))

        self.assertEqual(keys_of("models"), {m["key"] for m in previz.DIRECTOR_MODELS})
        self.assertEqual(keys_of("actions"), {a["key"] for a in previz.DIRECTOR_ACTIONS})
        self.assertEqual(keys_of("props"), {p["key"] for p in previz.DIRECTOR_PROPS})

    @unittest.skipUnless(RIG_JS.is_file(), "前端 rig.js 尚未落地")
    def test_action_speed_and_loop_match_between_engine_and_rig(self):
        """`speed`/`loop` 在两边各存一份，分叉的后果是**脚滑**——目录报 1.35m/s、
        动作实际按 4.2 编，控制台算出的 `timeScale` 就是错的，人会飘着走路。
        键对齐还不够，值也必须对齐。"""
        js = RIG_JS.read_text(encoding="utf-8")
        seg = js.split("/* @registry:actions */")[1].split("/* @end:actions */")[0]
        got = {}
        for m in re.finditer(r"^\s{2}(\w+):\s*\{([^}]*)\}", seg, re.M):
            body = m.group(2)
            loop = re.search(r"loop:\s*(true|false)", body)
            speed = re.search(r"speed:\s*([\d.]+)", body)
            got[m.group(1)] = (loop.group(1) == "true", float(speed.group(1)))
        want = {a["key"]: (bool(a["loop"]), float(a["speed"]))
                for a in previz.DIRECTOR_ACTIONS}
        self.assertEqual(got, want, "rig.js 的动作 loop/speed 与引擎目录分叉")


# ============================================================ 七、前端契约守卫
DIRECTOR = ASSETS / "director"


def strip_js(src: str, *, strings: bool = True) -> str:
    """去掉注释（可选连同字符串字面量一并去掉）。

    源码级守卫扫的是「代码里有没有真的用到某个符号」；注释里为解释取舍而提到的
    同名词会让检测器把说明当成用法。查的目标本身就是字符串字面量时（例如「有没有
    硬编码某个 key」），传 `strings=False` 只去注释。
    """
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    if strings:
        for q in ("`", '"', "'"):
            src = re.sub(q + r"(?:[^" + q + r"\\]|\\.)*" + q, " ", src)
    return src


@unittest.skipUnless(DIRECTOR.is_dir(), "3D 控制台前端尚未落地")
class TestFrontendContract(unittest.TestCase):
    """控制台前端与引擎共享的四条口径——错了都只会在成片里才被发现。

    前端没有单测框架（Studio 是零构建原生 JS SPA），这里用**源码级断言**守住
    「两边各存一份、分叉了没人知道」的那几处。不替代 E2E，只提供最低限度的自动检查。
    """

    def _read(self, name):
        return (DIRECTOR / name).read_text(encoding="utf-8")

    def test_snap_duration_matches_engine(self):
        """previz 时长钳制在前端（拖时间轴）与引擎（登记/报价）各有一份。
        分叉 = 导演在 3D 里排了 3s、引擎按 4s 向 Seedance 计费，运动被拉伸。"""
        js = self._read("timeline.js")
        mn = int(re.search(r"SNAP_MIN\s*=\s*(\d+)", js).group(1))
        mx = re.search(r"SNAP_MAX\s*=\s*(\d+)", js).group(1)
        self.assertEqual((mn, int(mx)), (previz.SNAP_MIN_SEC, previz.SNAP_MAX_SEC))
        self.assertIn("Math.round", js, "钳制必须是四舍五入（与 native 计费口径一致）")

    def test_easing_names_cover_every_preset(self):
        """preset 的 ease 名前端认不出就静默退回 linear——运镜看起来"没缓动"，
        而 3D 里的运动与写给 Seedance 的「缓慢平稳」措辞对不上。"""
        from kinema.pipeline import camera
        js = self._read("cameras.js")
        seg = js.split("export const EASE = {")[1].split("\n};")[0]
        known = set(re.findall(r"^\s{2}([A-Za-z]\w*):", seg, re.M))
        self.assertTrue(set(camera.EASES).issubset(known),
                        f"cameras.js 缺少缓动实现: {set(camera.EASES) - known}")
        for p in camera.CAMERA_PRESETS.values():
            self.assertIn(p["ease"], known)

    def test_export_strips_editor_helpers(self):
        """**previz 会作 reference_video 直接喂给 Seedance**——画面里混进 gizmo 箭头 /
        站位圈 / 路线样条 / 选中高亮，模型会当成场景内容试着复现。渲染前后必须
        成对切换洁净模式。"""
        js = self._read("stage.js")
        self.assertIn("setExportMode(true)", js)
        self.assertIn("setExportMode(false)", js)
        body = js.split("function setExportMode(")[1].split("\n  }")[0]
        for helper in ("gizmo", "mark", "axis", "pathLines", "emissive"):
            self.assertIn(helper, body, f"洁净模式漏掉了 {helper}，它会被烧进参考片")
        # 恢复必须在 finally 里：渲染中途抛错也不能把编辑器留在"没有 gizmo"的状态。
        # 批量渲染下这条更要紧——一镜失败就退出循环，没有 finally 就整场留在洁净模式
        tail = js.split("async function renderBatch(")[1]
        self.assertIn("finally {\n      setExportMode(false);", tail)

    def test_preview_and_export_share_one_evaluator(self):
        """`sceneAt(t)` 是预览与逐帧导出共用的唯一求值函数——所见即所渲。
        导出另写一份求值，预览里排好的戏与渲出来的片子就会是两回事。"""
        js = self._read("stage.js")
        self.assertEqual(js.count("function sceneAt("), 1)
        self.assertIn("sceneAt: (tl) => sceneAt(cut.t_in + tl)", js,
                      "导出必须复用 sceneAt，且按镜头块起点偏移")
        self.assertIn("sceneAt(timeline.t)", js, "预览循环也走同一个函数")

    def test_action_blend_stays_a_pure_function_of_time(self):
        """段间过渡的权重必须只由 t 与轨段表决定。

        `crossFade` 的权重按 mixer 时间累加，掉一帧两侧权重就不同——预览与逐帧
        导出会在同一个时间点得到不同的姿势，「所见即所渲」不再成立；而 previz 会
        作参考视频直接喂给模型。
        """
        js = self._read("actors.js")
        code = strip_js(js)
        for banned in ("crossFade", "performance.now", "Date.now"):
            self.assertNotIn(banned, code,
                             f"动作求值出现了 {banned}，过渡不再只依赖时间")
        self.assertIn("_blendWindow", code, "没有段间过渡窗口")
        # 未参与本帧的动作必须显式关掉：mixer 按权重累加所有 enabled 的 action，
        # 留一条权重非零的旧动作就是两个姿势叠在一起
        weigh = js.split("_weigh(pairs) {")[1].split("\n  }")[0]
        self.assertIn("enabled = false", weigh)

    def test_pose_curves_are_densified_without_moving_authored_extremes(self):
        """姿势表记的是极值姿势，而四元数关键帧轨在 three 里只有线性插值
        （该轨类型没有平滑插值实现）。两个极值之间走直线，关节角速度在每个关键帧
        处突变。故必须在编译成四元数之前加密，且把采样夹在相邻关键帧的取值区间内
        ——样条的自然过冲会把作者写定的极值再推出去一截，髋部高度通道上还会让脚
        穿过地面。
        """
        js = self._read("rig.js")
        build = js.split("export function buildClip(")[1]
        self.assertIn("denseSamples", build, "buildClip 未加密就直接建轨")
        self.assertIn("curveAt", build)
        curve = js.split("function curveAt(")[1].split("\n}")[0]
        self.assertIn("Math.min(Math.max(", curve,
                      "加密采样没有夹在相邻关键帧的取值区间内")

    def test_playhead_tick_does_not_rebuild_the_timeline(self):
        """播放头每帧前进只能就地改读数与高亮，不能重建时间轴结构。

        `renderTimeline` 是整块 `innerHTML = ""` 重建。挂到每帧的 `onTick` 上，工具条
        按钮会在 mousedown 与 mouseup 之间被换掉，而 `click` 要求两者落在同一元素上
        ——播放期间播放/暂停、保存编排、渲染 previz、交给 Seedance 连同每个镜头块都
        无法点击，只有键盘快捷键仍有效。
        """
        js = self._read("stage.js")
        tick = js.split("onTick:")[1].split("}")[0]
        self.assertNotIn("paintTimeline", tick, "每帧重建时间轴会让工具条点不动")
        self.assertIn("syncPlayhead", tick)
        # 播放/暂停自身同理：重建会把刚被点中的那个按钮换掉
        toggle = js.split("togglePlay: () =>")[1].split("\n")[0]
        self.assertNotIn("paintTimeline", toggle)
        ui = self._read("ui.js")
        self.assertIn("export function syncTimelineHead(", ui)

    def test_every_seat_anchor_is_reachable(self):
        """声明了座面的道具必须列进落座白名单。

        `rig.PROPS[].seat` 只是数据，触发吸附的是 `stage.SEAT_FOR`。两处分叉时道具的
        目录 desc 仍写着「配「坐下」自动落座」，而落座不会发生。
        """
        rig = self._read("rig.js")
        seg = rig.split("/* @registry:props */")[1].split("/* @end:props */")[0]
        declared = set()
        for entry in re.split(r"(?m)^  (?=\w+:\s*\{)", seg):
            m = re.match(r"(\w+):", entry)
            if m and "seat:" in entry.split("build:")[0]:
                declared.add(m.group(1))
        self.assertTrue(declared, "没采到 seat 锚点，检测器等于没跑")
        stage = self._read("stage.js")
        listed = set(re.findall(
            r'"(\w+)"', stage.split("const SEAT_FOR = {")[1].split("\n  };")[0]))
        self.assertEqual(declared, listed,
                         f"seat 锚点与 SEAT_FOR 分叉: {declared ^ listed}")

    def test_seat_snap_derives_height_from_the_pose(self):
        """落座高度必须由「座面 − 该姿势的臀底偏移」算出，不能逐道具手调偏移。

        手调量按某一个体型的某一个坐姿定死：换成儿童人偶或换成骑乘姿，同一个偏移
        就把人抬高或压低几十厘米。`seat` 因此记座面高度，落差由 `pelvisDrop` 与当前
        `hips.position.y` 现算。
        """
        js = self._read("stage.js")
        body = js.split("function snapToSeats(")[1].split("\n  }")[0]
        self.assertIn("pelvisDrop", body, "落座高度没有按体型反推臀底")
        self.assertIn("hips.position.y", body, "落座高度没有跟随当前姿势")
        rig = self._read("rig.js")
        self.assertIn("export function pelvisDrop(", rig)
        # 骨盆体块的尺寸只能有一处取值，否则反推的臀底与画出来的体块会分叉
        self.assertEqual(rig.count("function pelvisBlock("), 1)
        self.assertIn("limb(hips, mat, pelvisBlock(modelKey));", rig)

    def test_joint_angles_respect_anatomy(self):
        """姿势表里的肘与膝只能朝解剖学允许的方向弯。

        `swing` 的约定是「远端向前(+Z)」，故肘屈曲恒为正、膝屈曲恒为负。写反了不
        报错，渲出来是一个关节朝反侧折的人偶。这类角度在 3D 视口的常规视距下不易
        辨认，故在源码层拦截。
        """
        js = self._read("rig.js")
        tables = js.split("/* ------------------------------------------------------------------ 动作片段 */")[-1]
        bad = []
        for m in re.finditer(r"(forearm[LR]|shin[LR]):\s*\[\s*(-?[\d.]+)", tables):
            joint, deg = m.group(1), float(m.group(2))
            if joint.startswith("forearm") and deg < 0:
                bad.append(f"{joint}={deg}（肘向后折）")
            if joint.startswith("shin") and deg > 0:
                bad.append(f"{joint}={deg}（膝向前折）")
        self.assertEqual(bad, [], "姿势表出现反关节：" + "、".join(bad))

    def test_torso_and_limb_swings_use_separate_signs(self):
        """躯干链与四肢的 `swing` 必须分开取号。

        躯干链（hips/spine/chest/neck/head）的远端指向 +Y，四肢指向 −Y；`R_x(θ)` 对这
        两个方向的作用相反。用同一个符号时，「上身前倾 20°」写出来是后仰 20°。
        另一面是：`hips` 一转，腿与臂的世界朝向已跟着转过一次，此时按世界直觉再填
        一次角度即把同一次旋转叠两遍，结果是整个人沉到地面以下。
        """
        js = self._read("rig.js")
        seg = js.split("const AXIAL = new Set(")[1].split(")")[0]
        self.assertEqual(set(re.findall(r'"(\w+)"', seg)),
                         {"hips", "spine", "chest", "neck", "head"})
        body = js.split("function quatOf(")[1].split("\n}")[0]
        self.assertIn("AXIAL.has(bone)", body, "quatOf 没有按骨骼分组取号")

    def test_action_picker_is_driven_by_the_shipped_catalog(self):
        """选择器的动作清单与分桶判据必须取自下发目录。

        控制台自行列举动作，目录增删时就会分叉成「选项点了没反应」——与
        rig.js 注册表锁步守的是同一件事，只是落在 UI 这一侧。
        """
        js = self._read("ui.js")
        self.assertIn("openActionPicker", js)
        self.assertIn("ctx.dir.actions", js, "动作清单没有取自下发目录")
        code = strip_js(js, strings=False)
        for a in previz.DIRECTOR_ACTIONS:
            self.assertNotIn(f'"{a["key"]}"', code,
                             f"ui.js 硬编码了动作 key {a['key']}")

    def test_thumbnail_renderer_is_isolated_and_released(self):
        """缩略图走独立的离屏渲染器，并随控制台一起释放。

        舞台渲染器的尺寸与 pixelRatio 同时受视口布局与逐帧导出支配（导出恒锁
        pixelRatio=1 且按 Seedance 目标分辨率渲染），插入第三方改动会同时破坏
        这两条。独立上下文的代价是它跨路由留存：不在 dispose 里释放，反复进出
        控制台就会一路累积到浏览器丢弃最早的上下文。
        """
        pv = self._read("preview.js")
        self.assertIn("new THREE.WebGLRenderer(", pv, "缩略图没有自己的渲染器")
        self.assertNotIn('from "./stage.js"', pv, "缩略图反向依赖了舞台模块")
        # 播放轮询靠画布是否还在 DOM 里自净，弹层关闭后不留孤儿 rAF
        self.assertIn("isConnected", pv)
        stage = self._read("stage.js")
        tail = stage.split("return function dispose() {")[1]
        self.assertIn("disposePreview()", tail, "控制台卸载没有释放缩略图上下文")

    def test_render_goes_through_the_single_registration_path(self):
        """网页渲染必须落到 `previz build` → `register_previz`——绝不另写一份登记
        逻辑，否则版本栈 / 待审 / 首帧不覆盖三条纪律迟早只在 CLI 那边成立。"""
        self.assertIn("/api/previz/render", self._read("exporter.js"))
        actions = (Path(__file__).resolve().parents[1] / "kinema" / "studio"
                   / "actions.py").read_text(encoding="utf-8")
        seg = actions.split("def previz_render(")[1].split("\ndef ")[0]
        self.assertIn('"previz", "build"', seg)

    def test_batch_render_registers_one_shot_at_a_time(self):
        """批量渲染**必须串行**：每镜的登记都是一个 `previz build` 子进程，它
        load → 改 → save 章节文档。并发跑两个就是经典丢更新——后写的那个以自己
        load 到的旧副本为准，前一镜刚登记的 previz/image 凭空消失，且不报任何错。

        所以循环体里必须 `await` 到该镜登记落盘（`waitJob`）才进下一镜。
        """
        js = self._read("stage.js")
        body = js.split("async function renderBatch(")[1].split("\n  }")[0]
        self.assertIn("await waitJob(job)", body,
                      "登记未 await 就进下一镜 = 两个 previz build 并发写同一份章节文档")
        self.assertIn("if (!jr.ok)", body, "某镜登记失败要停下，别把后面的镜也一起废掉")
        self.assertIn("if (r.aborted) break", body, "Esc 中止要停在镜与镜之间")
        # 中止/失败都不许回滚已登记的镜——重来一次代价是逐帧重渲，很贵
        self.assertNotIn("clear_previz", body)

    def test_viewport_stays_alive_between_shots(self):
        """渲染循环让位的是**逐帧导出那一镜**，不是整批。

        拿整批标志 `S.rendering` 当判据的话，镜与镜之间那段编码等待里循环也不跑，
        而 `exportFrames` 收尾的 `setSize` 恰好把画布清成透明黑——实测「渲完一镜
        卡住一两秒 + 半黑屏」：A/B 量到空档内 16 次采样有 13 次整幅全黑。
        收窄到 `S.exporting` 后同一段采样零全黑帧。

        洁净模式同理不能撑到整批：渲染循环画 PiP 时每帧成对 `setExportMode`
        (true→false)，循环一恢复就把整批的洁净态关掉（`director-stage-ui.md` ⑩）。
        """
        js = self._read("stage.js")
        loop = js.split("function frame(")[1].split("\n  }")[0]
        self.assertIn("if (S.exporting) return;", loop,
                      "让位判据必须是单镜导出标志")
        self.assertNotIn("if (S.rendering) return;", loop,
                         "用整批标志让位 = 镜间画面死掉并被清成黑屏")
        body = js.split("async function renderBatch(")[1].split("\n  }\n")[0]
        # 洁净模式与让位都必须**在循环体内**成对开合，且收尾要 resize 让循环立刻重画
        seg = body.split("for (let k = 0")[1]
        self.assertIn("S.exporting = true;", seg)
        self.assertIn("setExportMode(true);", seg)
        self.assertIn("resize();", seg, "buffer 尺寸复位后画布是空的，必须让循环重画")

    def test_idle_viewport_stops_burning_frames(self):
        """渲染循环每帧做「全场景求值 + 两次完整渲染」（主画面 + 右下监视器）。
        画面静止时没有任何东西需要重画，却按 rAF 满帧跑 = 真机风扇长转、
        headless（WebGL 退化成 SwiftShader 软件渲染）实测 900%+ CPU。

        三道闸：后台标签页一帧不画；模态弹层遮住工作台时一帧不画（动作选择器里
        每格都在实时演一遍动作，看不见的全景渲染会把开销全压给看得见的缩略图）；
        前台静止（未播放 / 无拖拽 / 无输入）降到约 12fps。实测渲染次数 82/s →
        24/s。任何交互立刻恢复满帧，故不影响跟手。
        """
        js = self._read("stage.js")
        loop = js.split("function frame(")[1].split("\n  }")[0]
        self.assertIn("document.hidden", loop, "后台标签页不该继续渲染")
        self.assertIn('".dlg:not(.closing)"', loop, "模态弹层遮住时不该继续渲染")
        self.assertIn("IDLE_FRAME_MS", loop, "静止态必须降频")
        # 降频的排除项要盖住所有「正在动」的状态，漏一个就是那个交互变卡
        for st in ("timeline.playing", "S.placing", "S.drawingPathFor",
                   "S.dragObj", "S.dragCam", "S.dragCamPin", "S.dragPin", "S.dragLook"):
            self.assertIn(st, loop, f"降频判据漏了 {st}，该状态下画面会变迟钝")
        # 导出完全绕开本循环，previz 逐帧确定性不受降频影响
        self.assertIn("if (S.exporting) return;", loop)
        # window 级监听必须在 dispose 摘掉，否则来回进出导演台会叠一串
        disp = js.split("return function dispose()")[1]
        self.assertIn('window.removeEventListener("keydown", markInput)', disp)

    def test_portrait_aux_panels_are_height_sized_and_lockstepped(self):
        """监视器与分镜参考按「宽度百分比」定尺寸时，竖幅（9:16）的高是宽的
        1.78 倍，两块面板会盖满视口。竖幅必须挂 .tall 改按**高度**
        定尺寸；监视器渲染缓冲的尺寸在 JS 里另有一份，必须与 CSS 逐值锁步——
        分叉 = 每帧渲一张远大于显示框的图（GPU 白烧）或缓冲小于显示框（糊图）。"""
        js = self._read("stage.js")
        css = (ASSETS / "style.css").read_text(encoding="utf-8")
        self.assertIn("const portrait = canvas[1] > canvas[0]", js)
        self.assertIn('portrait ? "dz-pip tall" : "dz-pip"', js)
        self.assertIn('portrait ? "dz-refpanel tall" : "dz-refpanel"', js)
        pip_tall = css.split(".dz-pip.tall {")[1].split("}")[0]
        self.assertIn("width: auto", pip_tall)
        css_h = re.search(r"height:\s*(\d+)%", pip_tall)
        self.assertIsNotNone(css_h, ".dz-pip.tall 必须按高度定尺寸")
        ref_tall = css.split(".dz-refpanel.tall:not(.noimg) {")[1].split("}")[0]
        self.assertIn("width: auto", ref_tall)
        self.assertRegex(ref_tall, r"height:\s*\d+%")
        # 渲染缓冲与 CSS 显示尺寸逐值对拍（横竖两个口径各一份）
        js_h = re.search(r"hh \* 0\.(\d+) \* canvas\[0\] / canvas\[1\]", js)
        self.assertIsNotNone(js_h, "竖幅渲染缓冲必须按视口高度取尺寸")
        self.assertEqual(js_h.group(1), css_h.group(1),
                         "PIP 渲染缓冲比例必须与 .dz-pip.tall 的 height 同值")
        js_w = re.search(r"Math\.round\(w \* 0\.(\d+)\)", js)
        css_w = re.search(r"width:\s*(\d+)%", css.split(".dz-pip {")[1].split("}")[0])
        self.assertEqual(js_w.group(1), css_w.group(1),
                         "PIP 渲染缓冲比例必须与 .dz-pip 的 width 同值")

    def test_busy_veil_covers_the_mode_switch(self):
        """编码等待期必须有毛玻璃忙态蒙版，且**先于退出洁净模式**盖上。

        顺序反了就会露出一帧编辑视图（gizmo / 站位圈 / 路线全部闪回），下一镜又
        立刻切回洁净——那一下闪烁正是「割裂」。撤蒙版则要等到**下一镜开渲的瞬间**，
        中间（refreshChapter / 起下一镜）撤了就又露出一次静止的编辑视图。
        """
        js = self._read("stage.js")
        body = js.split("async function renderBatch(")[1].split("\n  }\n")[0]
        seg = body.split("for (let k = 0")[1]
        show_at = seg.index("showBusy(busy, {")
        exit_at = seg.index("setExportMode(false);")
        self.assertLess(show_at, exit_at, "蒙版必须先于退出洁净模式盖上")
        # 撤在下一镜开渲前（循环体内、导出之前），不是任务完成时
        head = seg.split("S.exporting = true;")[0]
        self.assertIn("showBusy(busy, null);", head)
        self.assertIn("if (!S.abort)", seg, "中止时不该再盖蒙版")
        # 全片合成同样是等待期，也要有蒙版；Esc 中止要当场撤掉
        self.assertIn("showBusy(busy, { title: \"合成全片预演中\"", js)
        esc = js.split('e.key === "Escape"')[1].split("\n    }")[0]
        self.assertIn("showBusy(busy, null)", esc, "Esc 后蒙版必须立刻撤，否则像卡死")

    def test_busy_veil_fades_fast_enough_to_be_seen(self):
        """这段等待实测才 ~0.7s——淡入按常规 .22s 的话蒙版几乎没真正现身
        （截到的那一帧 opacity 仍是 0），白留一次闪烁。"""
        css = (ASSETS / "style.css").read_text(encoding="utf-8")
        seg = css.split(".dz-busy {")[1].split("}")[0]
        m = re.search(r"transition:\s*opacity\s*\.(\d+)s", seg)
        self.assertIsNotNone(m, ".dz-busy 缺淡入过渡")
        self.assertLessEqual(int(m.group(1)), 15, "淡入太慢，短等待期里蒙版来不及现身")
        self.assertIn("backdrop-filter", seg, "要的是毛玻璃，不是一块纯色板")
        self.assertIn("position: relative", css.split(".dz-stage {")[1].split("}")[0],
                      ".dz-stage 必须是定位上下文，否则蒙版会盖住整个 Studio")

    def test_short_job_is_not_billed_the_long_job_poll_interval(self):
        """previz 登记实测约 0.7s，而 `pollJob` 缺省 1600ms **且第一次检查也要等满
        一轮**（那是给 Seedance 分钟级任务定的）——照缺省轮询每镜白等 1~1.6s。"""
        js = self._read("stage.js")
        body = js.split("function waitJob(")[1].split("\n  }")[0]
        m = re.search(r"interval:\s*(\d+)", body)
        self.assertIsNotNone(m, "waitJob 必须显式调快轮询，别吃 pollJob 的长任务缺省值")
        self.assertLessEqual(int(m.group(1)), 400)

    def test_render_button_opens_a_shot_picker(self):
        """渲染入口是**选镜弹层**（可单镜/多镜/全选），不是「只渲当前镜头块」——
        排戏以整场为单位，逐镜点开渲染无法追踪哪些镜已渲过。"""
        js = self._read("stage.js")
        self.assertIn("renderPreviz: () => openRenderPicker()", js)
        picker = js.split("function openRenderPicker()")[1].split("\n  }\n")[0]
        for quick in ("全选", "只选未渲", "清空"):
            self.assertIn(quick, picker, f"选镜弹层缺快捷动作「{quick}」")
        self.assertIn("!done(c.shot)", picker, "缺省应只勾未渲的镜")
        self.assertIn("openShell(", picker, "弹层一律走站内骨架工厂，别手搓 .dlg")

    def test_refresh_chapter_backfills_every_shipped_field(self):
        """`refreshChapter` 是白名单式浅拷贝——scanner 新下发的章节级字段必须在这里
        逐一回填，漏一个的表现是**「后台任务成功了，界面纹丝不动」且不报任何错**。

        `previz_reel` 就是典型：漏了它，全片合成完成、文件也在盘上，按钮却始终
        停在「合成全片」。故凡是控制台读得到的 `ch.<字段>`，回填处都得有它。
        """
        js = self._read("stage.js")
        body = js.split("async function refreshChapter()")[1].split("\n  }")[0]
        # 只查**会被后台任务改写**的那一类：`shots` 与全部 previz 面。`aspect`/`title`
        # 这些进门定死、整场不变，回填它们只是噪音。
        used = {f for f in re.findall(r"\bch\.(\w+)", js)
                if f == "shots" or f.startswith("previz")}
        self.assertIn("previz_reel", used, "取样没采到字段，这条守卫等于没跑")
        for f in sorted(used):
            self.assertIn(f"ch.{f} = fresh.{f}", body,
                          f"refreshChapter 漏回填 ch.{f} —— 该字段变化后界面不会更新")

    def test_director_imports_every_host_symbol_it_uses(self):
        """`director/*.js` 用到的宿主 App 符号必须**显式 import**。

        App 是 ES Module，宿主符号不经全局作用域可见——不 import 就是运行期
        ReferenceError，而这种错**只在点到那一步才炸**：逐帧上传那条手写
        fetch 若漏了 `CSRF`（帧体是 image/png 二进制，走不了恒发 JSON 的
        `post()`，故必须自带 token），戏排完、保存完全程无异样，直到按下「渲染」
        才报「CSRF is not defined」。静态分析这一层不查，问题将只能在用户侧暴露。
        """
        app = ASSETS / "app"
        exports = set()
        for f in sorted(app.glob("*.js")):
            s = strip_js(f.read_text(encoding="utf-8"))
            for m in re.finditer(r"export\s*{([^}]*)}", s, re.S):
                exports |= {t.strip().split(" as ")[-1].strip()
                            for t in m.group(1).split(",") if t.strip()}
            exports |= set(re.findall(
                r"export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)", s))
        self.assertIn("CSRF", exports, "app 层导出集没采到，检测器等于没跑")

        for f in sorted(DIRECTOR.glob("*.js")):
            s = strip_js(f.read_text(encoding="utf-8"))
            imported = set()
            for m in re.finditer(
                    r"import\s*(?:\*\s*as\s*(\w+)|{([^}]*)}|(\w+))\s*from", s, re.S):
                imported |= {g for g in (m.group(1), m.group(3)) if g}
                if m.group(2):
                    imported |= {t.strip().split(" as ")[-1].strip()
                                 for t in m.group(2).split(",") if t.strip()}
            body = re.sub(r"import\s.*?from\s*\S+;?", " ", s, flags=re.S)
            local = set(re.findall(r"(?:function|const|let|var|class)\s+(\w+)", body))
            for m in re.finditer(r"(?:const|let|var)\s*{([^}]*)}", body):   # 解构绑定
                local |= {t.strip().split(":")[-1].split("=")[0].strip()
                          for t in m.group(1).split(",") if t.strip()}
            for m in re.finditer(r"\(([^()]*)\)\s*(?:=>|{)", body):         # 形参
                local |= {t for t in (p.strip().split("=")[0].strip()
                                      for p in m.group(1).split(","))
                          if re.fullmatch(r"\w+", t)}
            miss = sorted((set(re.findall(r"\b\w+\b", body)) & exports)
                          - imported - local)
            self.assertEqual(miss, [], f"director/{f.name} 用了却没 import: {miss}")

    def test_cut_duration_has_a_single_source_of_truth(self):
        """`dur` 是镜头块时长的单一真源，`normalize()` 只在它缺失时才从 t_out-t_in 反推。

        反过来写会让 `setCutDur` 变成**静默空操作**：先把 dur 改成 6、再从还没更新的
        `t_out - t_in`（旧的 5）算回去，新值当场被覆盖 → 「换了运镜但镜头块时长不跟着变」，
        而且不报任何错。
        """
        js = self._read("timeline.js")
        body = js.split("normalize() {")[1].split("\n  }")[0]
        self.assertIn("c.dur != null", body,
                      "normalize 必须先认 c.dur，只有它缺失才从 t_out-t_in 反推")
        self.assertNotIn("(c.t_out ?? 0) - (c.t_in ?? 0) || c.dur", body,
                        "这个顺序会让 setCutDur 空操作")

    def test_actor_gizmo_hides_the_useless_y_handle(self):
        """角色永远贴地（`onGizmoMove` 把 y 压回 0），所以**不能给它 Y 轴手柄**——
        那根竖直箭头恰恰画在角色正中、最容易抓，拖它却毫无反应，用户由此得出
        「拖动根本不生效」的结论。道具要保留 Y（箱子放桌上）。
        """
        js = self._read("stage.js")
        seg = js.split("function select(item)")[1].split("\n  }")[0]
        self.assertIn("gizmo.showY = false", seg, "角色必须隐藏 Y 轴手柄")
        self.assertIn("gizmo.showY = true", seg, "道具必须保留 Y 轴手柄")
        self.assertIn("translationSnap", js, "走位应吸附到网格，previz 的走位是要喂给模型的")

    def test_console_uses_studio_components_not_native_controls(self):
        """控制台只用站内组件（`uiSelect`/`uiCheck`/`.efx-opt`/`listSearch`/`.dlg`）。
        原生 `<select>`/`<input type=checkbox|range>` 由操作系统绘制，在深色主题下
        配色圆角字体全对不上，一眼就露出「这块是外挂的」。"""
        ui = self._read("ui.js")
        for bad in ('h("select"', 'type: "checkbox"', 'type: "range"', 'type: "number"'):
            self.assertNotIn(bad, ui, f"控制台里出现了原生控件: {bad}")
        for good in ("uiSelect(", "uiCheck()", "efx-opt", "listSearch("):
            self.assertIn(good, ui, f"应复用站内组件: {good}")

    def test_missing_catalog_fails_with_an_actionable_message(self):
        """目录缺失必须**当场**报清楚，别一路走到 `preset.ease` 才抛
        "Cannot read properties of undefined"——那句错完全指不到根因。

        现实中的根因只有一个：**Studio 进程比代码旧**。静态资源每次请求都从磁盘读
        （前端总是新的），而 `scanner.py` 是进程启动时加载进内存的，服务不重启就
        永远不会下发新目录 → 新前端 + 旧后端 → 目录为空。所以报错必须点名
        `studio --restart`，否则用户只会以为控制台坏了。
        """
        js = self._read("stage.js")
        head = js.split("const { root, outliner")[0]
        self.assertIn("!catalog.length", head, "取完目录必须立刻校验，不能带着空目录往下走")
        self.assertIn("studio --restart", head, "报错须给出可执行的解法")

    def test_unknown_preset_falls_back_instead_of_crashing(self):
        """单个 preset key 认不出来只该退化成固定机位，不该把整个场景打崩。"""
        js = self._read("cameras.js")
        self.assertIn("FALLBACK_PRESET", js)
        ctor = js.split("constructor(preset, opts = {})")[1].split("\n  }")[0]
        self.assertIn("preset = FALLBACK_PRESET", ctor)

    def test_first_entry_builds_an_empty_stage_not_an_auto_arrangement(self):
        """首次进入=**空台**：只铺时间轴必需的结构骨架（每正镜一个镜头块+一个机位，
        registered camera_preset 之外一律 static），零角色/零道具/零走位/零动作——
        编排是导演的创作，引擎绝不代排。
        重排/代排的唯一入口=「复制 AI 编排指令」交指挥层按分镜图写入。"""
        js = self._read("stage.js")
        self.assertNotIn("autoBlock", js, "自动编排已整体删除，绝不复活")
        self.assertNotIn("autoblock", js, "autoblock.js 模块已删，import 也不许残留")
        body = js.split("function restore(doc)")[1].split("\n  }")[0]
        self.assertIn("if (doc && (doc.cuts || []).length)", body,
                      "有场景快照仍走恢复分支")
        skeleton = body.split("} else {")[1]
        self.assertIn("addCamera", skeleton, "空台仍需每镜一个机位（镜头块 1:1 从属）")
        self.assertIn("timeline.addCut", skeleton)
        self.assertNotIn("addActor", skeleton, "空台绝不摆角色")
        self.assertNotIn("addProp", skeleton, "空台绝不摆道具")
        self.assertIn("copyAiPlan:", js, "代排入口=复制 AI 编排指令（智能归指挥层）")
        self.assertIn("复制 AI 编排指令", self._read("ui.js"))
        director = Path(__file__).resolve().parents[1] / "kinema" / "studio_app" / "director"
        self.assertFalse((director / "autoblock.js").exists(), "自动编排模块文件已删除")

    def test_path_drawing_starts_at_the_actor(self):
        """画走位时**起点必须是角色当前站位**，不是第一次点击的地方。

        否则 t=0 时角色会瞬移到路线起点：用户按自己的视角在地上点几下，人就"嗖"地
        跑到十几米外，而镜头还看着原处——表现出来正是「人偶永远在最右上角、还拖不动」。
        """
        js = self._read("stage.js")
        seg = js.split("togglePathDraw:")[1].split("\n    },")[0]
        self.assertIn("a.object.position.toArray()", seg,
                      "起笔就该把角色当前站位放进 pathBuf")
        self.assertIn("focusSubject(a)", seg, "画完要把镜头带过去（路线可能拉很远）")

    def test_static_assets_are_never_cached(self):
        """`/assets/*` 必须 `no-store`：本地开发工具，改完前端刷新就该生效。
        不给缓存头时浏览器会启发式缓存 js/css，于是出现最难查的一类现象——
        「明明改了也重启了，界面行为还是旧的」。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "studio"
               / "server.py").read_text(encoding="utf-8")
        seg = src.split('if path.startswith("/assets/")')[1][:400]
        self.assertIn("cache=False", seg)

    def test_spa_route_registers_and_disposes(self):
        """WebGL context 是稀缺资源（浏览器只给十几个）——路由切走不卸载，
        来回切几次控制台就再也起不来了。"""
        app = "".join(q.read_text(encoding="utf-8") for q in
                      [*sorted((ASSETS / "app").glob("*.js")), ASSETS / "app.js"])
        self.assertIn('name: "stage"', app)
        self.assertIn("stageDispose", app)
        self.assertIn("previz:", app.split("const JOB_ZH")[1].split("\n")[0],
                      "JOB_ZH 缺 previz，分镜卡忙态遮罩会退回泛化的「生成中」")

    def test_custom_camera_track_completes_within_the_cut(self):
        """机位自定义轨道（拖轨迹路点烘焙而来）必须**弧长参数化 + 归一时间**取点——
        镜头块结束的那一帧恰好走到轨道终点、全程匀速。写成 getPoint(t) 会在控制点
        附近忽快忽慢；自己按秒推进而不归一，就是「分镜时间到了镜头才走一半」。
        轨迹可视与 rig 求值还必须共用同一个建曲线入口
        `camPathCurve`——两边各建一条，「画出来的线」与「相机真正飞的线」迟早分叉。"""
        cams = self._read("cameras.js")
        self.assertIn("export function camPathCurve", cams)
        seg = cams.split("_worldPos(te, anchor)")[1].split("\n  }")[0]
        self.assertIn("_customCurve.getPointAt(te", seg,
                      "自定义轨道必须走弧长参数化 getPointAt（匀速 + 终点必达）")
        stage = self._read("stage.js")
        self.assertIn("camPathCurve(cam.path)", stage,
                      "轨迹可视必须复用 rig 的建曲线入口——画的线就是飞的线")
        # 契约字段：serialize 带 path、AI 编排指令声明 path（指挥层也能写自定义轨道）
        self.assertIn("path: c.path || null", stage)
        self.assertIn("path:[[x,y,z]",
                      stage.split("function buildAiPlanInstruction")[1])

    def test_camera_pin_drag_bakes_then_edits(self):
        """拖路点的第一下把 preset 程序轨道**烘焙**成显示中的世界路点（形状原样，
        只是从「程序算的」变成「可编辑的」），之后逐点自由改；换运镜必须清掉
        自定义轨道——留着它，新运镜在位置上完全不生效，表现为「换了运镜画面
        却一点没变」且不报任何错。"""
        stage = self._read("stage.js")
        self.assertIn("function bakeWaypoints", stage)
        seg = stage.split("function dragCamPinTo")[1].split("\n  }")[0]
        self.assertIn("gesture.moved", seg, "<6px 还算点击——不烘焙也不动点")
        self.assertIn("d.cam.path = d.bake", seg, "第一下真实位移时才烘焙")
        sp = stage.split("setPreset:")[1].split("\n    },")[0]
        self.assertIn("c.path = null", sp, "换运镜必须回到新 preset 的程序轨道")

    def test_camera_track_has_a_vertical_degree_of_freedom(self):
        """机位轨道必须有**看得见的垂直入口**（路点针的 ↕ 升降手柄）——只藏在 ⇧
        修饰键里，垂直自由度在界面上就没有可见痕迹（迈克尔·贝式
        低走高的仰拍环绕就排不出来）。垂直换算必须沿**世界 Y 轴的屏幕投影**
        （`liftDelta`）——粗糙的「dy×固定系数」在俯仰角变化时忽快忽慢；正俯视时
        Y 轴投影退化成一个点，除零护栏不能省。"""
        pt = self._read("pathtool.js")
        self.assertIn("camPinLift", pt, "路点针必须带 ↕ 升降手柄的拾取面")
        stage = self._read("stage.js")
        self.assertIn("function liftDelta", stage)
        seg = stage.split("function dragCamPinTo")[1].split("\n  }")[0]
        self.assertIn("liftDelta", seg, "垂直拖必须走轴投影换算，不是 dy×固定系数")
        ld = stage.split("function liftDelta")[1].split("\n  }")[0]
        self.assertIn("l2 < 16", ld, "正俯视时 Y 轴投影退化——除零护栏不能省")
        # ⇧+拖机身 = 整条轨道升降（先烘焙，静止机位明确拒绝而非烘零长度坏轨道）
        self.assertIn('mode === "lift"', stage)
        self.assertIn("function ensureCamPath", stage)

    def test_playback_hides_manipulation_aids(self):
        """播放中隐藏全部操纵件（gizmo / 选中圈 / 悬停圈 / 机位路点针）——播放是
        「看戏」，操纵手柄跟着角色满场跑（或被甩在路线起点）都是
        干扰；暂停/选中才是「排戏」。gizmo 必须 **detach 而非藏 visible**——
        只藏 visible 的 helper 会跟不上走位的角色，暂停时按对象当前位置
        重新 attach 一了百了。"""
        stage = self._read("stage.js")
        self.assertIn("timeline.playing !== lastPlaying", stage)
        seg = stage.split("if (timeline.playing !== lastPlaying)")[1].split("\n    }")[0]
        self.assertIn("gizmo.detach()", seg, "播放即 detach，不是只藏 visible")
        aids = stage.split("function syncAids()")[1].split("\n  }")[0]
        self.assertEqual(aids.count("!timeline.playing"), 2,
                         "选中圈与悬停圈都必须在播放中隐藏")
        viz = stage.split("function syncCamViz(")[1].split("\n  }")[0]
        self.assertIn("!timeline.playing", viz, "机位路点针在播放中必须收起")
        sel = stage.split("function select(item)")[1].split("\n  }")[0]
        self.assertEqual(sel.count("if (!timeline.playing) gizmo.attach"), 2,
                         "播放中点选（角色/道具）不弹手柄，暂停时 frame 补挂")
        # 幽灵 gizmo 根因：PiP 洁净渲染每帧成对调 setExportMode，恢复侧无条件
        # `helper.visible = true` 会把 detach 后本应消失的 root 重新点亮——
        # 冻在最后挂载位置，人走远了手柄留在原地。恢复必须回真值。
        exp = stage.split("function setExportMode(")[1].split("\n  }")[0]
        self.assertIn("!!gizmo.object", exp,
                      "洁净模式恢复只在挂着对象时点亮 root，防幽灵 gizmo")

    def test_body_drag_defaults_to_start_point_not_whole_track(self):
        """自定义轨道下拖机身缺省**只挪起点**（1 号路点=开拍位置）——整条平移必须是
        显式意图（⌘/Ctrl 修饰键）。缺省整条平移是最高频的误操作源：
        「想摆个开拍位置，一拖把后续路点全带走」。机位名牌只写「镜号·运镜名」
        且选中该机位即隐藏——塞轨道态会把牌子撑宽到盖住机身。"""
        stage = self._read("stage.js")
        self.assertIn('mode: "start"', stage)
        self.assertIn("ev.metaKey || ev.ctrlKey", stage, "整条平移必须走显式修饰键")
        seg = stage.split('if (d.mode === "start")')[1].split("return;")[0]
        self.assertIn("d.cam.path[0]", seg)
        self.assertNotIn(".map(", seg, "start 模式绝不能整条 map——那正是要修的误操作")
        tag = stage.split("function syncCamTag(")[1].split("\n  }")[0]
        self.assertIn("S.selected !== cam", tag, "选中该机位时名牌必须隐藏（编辑态防遮挡）")
        self.assertNotIn("自定义轨道", tag, "名牌不塞轨道态——会撑宽到盖住机身")

    def test_camera_pick_surface_is_the_glyph_not_a_bubble(self):
        """机身拾取面=可见几何（机身盒+镜头锥）+ 视锥漏斗**弱命中**——绝不挂隐形
        拾取大球（那会让「在旁边空白处点击拖动也能把机位拖走」，触发范围太广）。
        两条铁律：① 视锥线框必须 `line.raycast = () => {}`——Raycaster 对 Line 的
        命中阈值默认 **1 米**，留着它比大球还失控；② 漏斗是**弱命中**（它常正对
        主体，按强命中算会让「点主体」隔着漏斗误选机位），`pickObject` 只在没点到
        任何实体时才认它，且判定必须先于父级攀爬（爬到 camBody 就成强命中了）。
        拖拽读数显示在视角胶囊正下方（`.dz-draghint`），不挤在左上 HUD。"""
        pt = self._read("pathtool.js")
        body = pt.split("export function buildCamBody")[1].split("\n}")[0]
        self.assertNotIn("SphereGeometry", body, "机身的隐形拾取大球必须移除")
        self.assertIn("line.raycast = () => {}", pt, "视锥线框必须禁掉射线命中")
        self.assertIn("pickFrustum", pt, "视锥漏斗=第二拾取面（弱命中）")
        stage = self._read("stage.js")
        po = stage.split("function pickObject")[1].split("\n  }")[0]
        self.assertIn("weakCam", po, "漏斗=弱命中，实体优先")
        self.assertLess(po.index("pickFrustum"), po.index("while (o"),
                        "漏斗判定必须先于父级攀爬")
        self.assertIn("setDragHint", stage)
        self.assertIn("dz-draghint", stage)

    def test_traj_sampling_never_crosses_the_cut_boundary(self):
        """轨迹采样的第 49 个点落在 t_out 上，而 `cutAt(t_out)` 属于**下一个镜头块**
        （区间左闭右开）——不夹住的话末样本（=5 号针）是下一镜机位的起始位姿：
        预设轨迹表现为「相机走到 4 号针就切镜、5 号针永远到不了」（希区柯克变焦
        最先撞上），烘焙出的自定义轨道更是会在结尾真的飞向别的机位。"""
        stage = self._read("stage.js")
        seg = stage.split("function sampleCamTraj")[1].split("\n  }")[0]
        self.assertIn("Math.min", seg, "末样本必须夹在 t_out 之内")
        self.assertIn("cut.t_out - 1e-3", seg)


if __name__ == "__main__":
    unittest.main()
