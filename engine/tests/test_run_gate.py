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

"""动镜档出片的三道片段收口与两条同源闸。

动镜档（dubbed/native）一镜一片：正镜缺片段时 `run` 中止、`compose.build` 拒合成、
`verify` 硬判——三处同一判据，静图形态只经显式 `-m a`。native 混烧与曲库 BGM 的
互斥由 `run` 与 `assemble` 共用一处判定。局部改造先产出到临时名再归档替换，
参考图与画布在生成期间始终在盘。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kinema.errors import KinemaError, ProjectError
from kinema.project import Project
from tests.support import LocalBackendEnv

_PNG = b"\x89PNG\r\n\x1a\n"


class _Store:
    fps = 30

    def canvas(self, aspect):
        return (1920, 1080)


class _Case(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        self.env.restore()

    def _project(self, shots, **over) -> Project:
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "aspect": "16:9", "aspects": ["16:9"],
               "motion": "native", "shots": shots}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    def _file(self, name: str, body: bytes = b"x") -> str:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        return str(p)


class TestRunRequiresClips(_Case):
    def test_missing_clip_aborts_before_compose(self):
        from kinema import cli
        clip = self._file("c1.mp4")
        p = self._project([{"id": 1, "dur": 4, "clip": clip},
                           {"id": 2, "dur": 4},
                           {"id": 3, "kind": "transition", "dur": 1.0,
                            "transition": {"type": "fade_black"}},
                           {"id": 4, "dur": 4, "review": {"shot": {"state": "omt"}}}])
        with self.assertRaises(KinemaError) as ctx:
            cli._require_clips(p)
        self.assertIn("镜 2", str(ctx.exception))
        self.assertNotIn("镜 3", str(ctx.exception), "转场镜不走图生视频")
        self.assertNotIn("镜 4", str(ctx.exception), "弃用镜不进成片")

    def test_all_clips_present_passes(self):
        from kinema import cli
        clip = self._file("c1.mp4")
        p = self._project([{"id": 1, "dur": 4, "clip": clip}])
        cli._require_clips(p)


class TestComposeRefusesMissingClips(_Case):
    def test_seedance_chapter_without_clip_raises_before_rendering(self):
        from kinema.pipeline import compose
        img = self._file("s1.png", _PNG)
        p = self._project([{"id": 1, "dur": 4, "image": img, "clip": self._file("c1.mp4")},
                           {"id": 2, "dur": 4, "image": img}])
        with self.assertRaises(ProjectError) as ctx:
            compose.build(p, _Store(), aspect="16:9")
        self.assertIn("镜 2", str(ctx.exception))
        self.assertIn("-m a", str(ctx.exception), "静图形态的入口要给出")


class TestVerifyRequiresClips(_Case):
    def test_clip_missing_is_hard_fail_only_for_seedance_chapters(self):
        from kinema.pipeline import mediacheck as mc
        out = self._file("out.mp4", b"")
        clip = self._file("c1.mp4")
        shots = [{"id": 1, "dur": 2.0, "clip": clip}, {"id": 2, "dur": 2.0}]
        rep = mc.verify_aspect(self._project(shots, output={"16:9": out}),
                               _Store(), aspect="16:9", samples=1)
        codes = [f["code"] for f in rep["hard_fail"]]
        self.assertIn("clip_missing", codes)
        self.assertIn("镜 2", next(f["msg"] for f in rep["hard_fail"]
                                   if f["code"] == "clip_missing"))
        rep = mc.verify_aspect(self._project(shots, output={"16:9": out}, motion="kenburns"),
                               _Store(), aspect="16:9", samples=1)
        self.assertNotIn("clip_missing", [f["code"] for f in rep["hard_fail"]])


class TestNativeBgmConflictSingleSource(_Case):
    def test_run_and_assemble_reject_the_same_document(self):
        from kinema import cli
        p = self._project([{"id": 1, "dur": 4, "narration": "旁白。"}],
                          native_voiceover=True, native_bgm=True)
        with self.assertRaises(KinemaError) as a:
            cli._stage_audio_bed(p, _Store(), None)
        with self.assertRaises(KinemaError) as b:
            cli._reject_native_bgm_conflict(p, want=None)
        self.assertEqual(str(a.exception), str(b.exception))
        p.data["native_bgm"] = False
        cli._reject_native_bgm_conflict(p)
        with self.assertRaises(KinemaError):
            cli._reject_native_bgm_conflict(p, want=True)


class _RefProbeProv:
    """记录参考图在调用时刻是否在盘，再把占位字节写到目标路径。"""
    name = "fake"
    prompt_lang = "zh"

    def __init__(self):
        self.ref_exists = None
        self.out = None

    def generate(self, prompt, out_path, *, ref_images=None, **kw):
        self.ref_exists = Path(ref_images[0]).is_file()
        self.out = out_path
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_PNG + b"refined")

        class _R:
            def __init__(self, path):
                self.path, self.cost = str(path), 0.0
        return _R(p)


class _Router:
    force_mock = True

    def __init__(self, prov):
        self.prov = prov

    def resolve(self, capability, profile):
        return self.prov, {}


class TestRefineKeepsSourceOnDisk(_Case):
    def test_reference_is_read_before_archive_and_canvas_is_replaced(self):
        from kinema import refine
        from kinema.pipeline import versioning
        p = self._project([{"id": 1, "dur": 3, "narration": "x"}], motion="kenburns")
        canvas = p.subdir("images") / "shot_1.png"
        canvas.write_bytes(_PNG + b"v1")
        p.shots[0]["image"] = str(canvas)
        prov = _RefProbeProv()
        r = refine.refine_shot_image(p, _Store(), _Router(prov), shot_no=1, instruction="改")
        self.assertTrue(prov.ref_exists, "参考图在生成时刻必须仍在盘上")
        self.assertNotEqual(prov.out, str(canvas), "产出先落临时名，不覆写画布")
        self.assertEqual(r["image"], str(canvas))
        self.assertEqual(canvas.read_bytes(), _PNG + b"refined")
        self.assertFalse(Path(prov.out).exists(), "临时文件已替换为画布")
        hist = versioning.history(p.shots[0], "image")
        self.assertEqual(len(hist), 1)
        self.assertEqual(Path(hist[0]["files"]["main"]).read_bytes(), _PNG + b"v1")

    def test_failed_generation_leaves_canvas_untouched(self):
        from kinema import refine
        from kinema.pipeline import versioning

        class _Boom(_RefProbeProv):
            def generate(self, prompt, out_path, **kw):
                raise RuntimeError("provider down")

        p = self._project([{"id": 1, "dur": 3, "narration": "x"}], motion="kenburns")
        canvas = p.subdir("images") / "shot_1.png"
        canvas.write_bytes(_PNG + b"v1")
        p.shots[0]["image"] = str(canvas)
        with self.assertRaises(RuntimeError):
            refine.refine_shot_image(p, _Store(), _Router(_Boom()), shot_no=1, instruction="改")
        self.assertEqual(canvas.read_bytes(), _PNG + b"v1")
        self.assertEqual(versioning.history(p.shots[0], "image"), [])


class TestVideoCastFollowsShotCast(_Case):
    """视频角色锚与图像侧文字锚同一取材口径：只提到甲的镜不把乙写成「本镜出场」。"""

    def test_text_hit_narrows_the_video_cast(self):
        from kinema import cli
        sheet_a, sheet_b = self._file("a.png"), self._file("b.png")
        p = self._project([{"id": 1, "dur": 4, "image_prompt": "林深站在门口"},
                           {"id": 2, "dur": 4, "image_prompt": "空镜", "characters": []},
                           {"id": 3, "dur": 4, "image_prompt": "两人对视"}],
                          characters=[{"name": "林深", "sheet": sheet_a},
                                      {"name": "王姨", "sheet": sheet_b}])
        self.assertEqual([r["name"] for r in cli._video_cast(p, p.shots[0])], ["林深"])
        self.assertEqual(cli._video_cast(p, p.shots[1]), [])
        self.assertEqual({r["name"] for r in cli._video_cast(p, p.shots[2])}, {"林深", "王姨"},
                         "无显式表也无命中 → 全员回落，与图像侧一致")


class TestSetupReadyIncludesKeys(unittest.TestCase):
    """`setup --check` 的 ready 含必需密钥：AGENTS 承诺 ready=true 直接开工，缺密钥的机器
    不能到 tts/gen-image 才在 provider 调用处失败。ELEVENLABS 有本地曲库回落，不计入。"""

    def _ready(self, state_of):
        import contextlib
        import io
        from unittest import mock

        from kinema import cli, config_overlay
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        try:
            buf = io.StringIO()
            with mock.patch.object(config_overlay, "key_state", lambda store, k: state_of(k)), \
                 contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
                cli.main(["setup", "--check", "--json"])
            return json.loads(buf.getvalue()), ctx.exception.code
        finally:
            env.restore()

    def test_missing_required_key_is_a_red_item(self):
        rep, code = self._ready(lambda k: "unset" if k == "ARK_TTS_API_KEY" else "env")
        names = {c["name"]: c["ok"] for c in rep["checks"]}
        self.assertFalse(rep["ready"])
        self.assertEqual(code, 1)
        self.assertFalse(names["密钥 ARK_TTS_API_KEY"])
        self.assertTrue(names["密钥 ARK_API_KEY"])
        self.assertNotIn("密钥 ELEVENLABS_API_KEY", names, "曲库密钥有本地回落，不进 ready")
        rep, code = self._ready(lambda k: "env")
        self.assertTrue(all(c["ok"] for c in rep["checks"] if c["name"].startswith("密钥")))


class TestComposeInvalidatesVerify(_Case):
    def test_new_output_drops_the_previous_verify_report(self):
        from unittest import mock

        from kinema import cli
        from kinema.pipeline import compose as compose_mod
        img = self._file("s1.png", _PNG)
        p = self._project([{"id": 1, "dur": 4, "image": img}], motion="kenburns",
                          verify={"at": "t", "16:9": {"ok": True}})
        out = self._file("out/final.mp4", b"mp4")

        class _S(_Store):
            def effects_for(self, prof, eff):
                return []
        with mock.patch.object(compose_mod, "build", return_value=out), \
             mock.patch.object(cli, "_sub_cfg", lambda *a, **k: {}), \
             mock.patch.object(cli, "ensure_tools", lambda: None), \
             mock.patch.object(cli, "probe_duration", return_value=4.0), \
             mock.patch("builtins.print"):
            cli.stage_compose(p, _S(), None)
        self.assertNotIn("verify", json.loads(Path(p.path).read_text(encoding="utf-8")))


class TestRunCoverWiring(_Case):
    def test_run_tail_asks_for_the_chapter_cover(self):
        from types import SimpleNamespace
        from unittest import mock

        from kinema import cli
        p = self._project([{"id": 1, "dur": 4}], chapter={"project": "p1", "id": "ch01"})
        seen = vars(cli._run_cover_args(p, SimpleNamespace(profile=None, mock=True, config=None)))
        self.assertEqual((seen["project"], seen["chapter"], seen["all"]), ("p1", "ch01", False))
        self.assertFalse(seen["force"], "已在盘的封面不重生")
        self.assertEqual(Path(seen["workspace"]).resolve(), (self.tmp / "proj").resolve())

    def test_run_tail_reports_cover_failure_after_the_summary(self):
        """封面失败发生在成片与过审之后：总结照打、退出码非零，补封面命令与本次 run
        同工作区、同 mock/profile/config，照抄不会落错工作区或真花钱。"""
        from types import SimpleNamespace
        from unittest import mock

        from kinema import cli
        p = self._project([{"id": 1, "dur": 4}], chapter={"project": "p1", "id": "ch01"})
        lines = []
        with mock.patch.object(cli, "cmd_cover", side_effect=KinemaError("封面生成失败：ch01")), \
                mock.patch.object(cli, "_print_summary", lambda proj: lines.append("summary")), \
                mock.patch("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a)))):
            rc = cli._finish_run(p, SimpleNamespace(profile="anime", mock=True, config=None))
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1], "summary")
        self.assertIn("⚠ 封面生成失败：ch01", lines[0])
        hint = next(line for line in lines if "cover p1 --chapter ch01" in line)
        ws = cli._run_cover_args(p, SimpleNamespace(profile="anime", mock=True, config=None)).workspace
        for part in (f"--workspace {ws}", "--mock", "--profile anime"):
            self.assertIn(part, hint)
        self.assertNotIn("--config", hint)


class TestScannerCapsDefault(unittest.TestCase):
    def test_unresolved_provider_uses_adapter_defaults(self):
        from kinema.providers.base import VideoProvider
        from kinema.studio import scanner
        self.assertEqual(scanner._video_caps(None),
                         {"refs": VideoProvider.supports_reference_images,
                          "last": VideoProvider.supports_last_frame,
                          "v2v": VideoProvider.supports_reference_video})
