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

"""口型精修（lipsync）增强步守卫。

dubbed 的缺省档形态：Seedance 底片出齐后，对白镜按最终配音由视频改口型服务
重绘口型；旁白/静音镜按闭唇出片、恒不精修。产物是派生物（底片+wav 可重算），
`clips` 指向 lips 文件、`clips_base` 保底片——换音色只重跑 tts+lipsync+assemble。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import unittest.mock

from pathlib import Path

from kinema.models import ConfigStore, ModelRouter
from kinema.project import Project
from tests.support import LocalBackendEnv


def _chapter(tmp: Path, shots: list[dict]) -> Project:
    cf = tmp / "ch01.json"
    data = {"id": "ch01", "motion": "dubbed", "aspect": "16:9", "shots": shots}
    cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return Project(cf, data)


class _StageCase(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)
        self.store = ConfigStore.load(None)
        self.router = ModelRouter(self.store, force_mock=True)

    def tearDown(self):
        self.tmp_ctx.cleanup()
        self.env.restore()

    def _files(self, project, sid, *, wav=True, clip=True):
        adir = project.subdir("audio")
        gdir = project.subdir("gen_clips")
        if wav:
            (adir / f"shot_{sid}.wav").write_bytes(b"RIFFwav")
        if clip:
            c = gdir / f"shot_{sid}_16x9.mp4"
            c.write_bytes(b"clip")
            return str(c)
        return None


class TestStageScope(_StageCase):
    """只修对白镜：旁白/静音镜按闭唇出片，对它们改口型就是给闭着的嘴找口型。"""

    def test_only_dialogue_shots_are_processed(self):
        from kinema.cli import stage_lipsync
        shots = [
            {"id": 1, "dur": 5.0, "narration": "旁白讲述。"},
            {"id": 2, "dur": 5.0, "speaker": "岚瑾", "narration": "台词。"},
            {"id": 3, "dur": 5.0, "narration": ""},
        ]
        p = _chapter(self.tmp, shots)
        for sid in (1, 2, 3):
            c = self._files(p, sid)
            p.data["shots"][sid - 1]["clips"] = {"16:9": c}
            p.data["shots"][sid - 1]["clip"] = c
        stage_lipsync(p, self.store, self.router)
        s1, s2, s3 = p.data["shots"]
        self.assertNotIn("clips_base", s1, "旁白镜不精修")
        self.assertNotIn("clips_base", s3, "静音镜不精修")
        self.assertTrue(s2["clips"]["16:9"].endswith("_lips.mp4"))
        self.assertTrue(Path(s2["clips"]["16:9"]).is_file())
        self.assertTrue(s2["clips_base"]["16:9"].endswith("shot_2_16x9.mp4"),
                        "底片指针必须保留——重算恒以底片为源")
        self.assertEqual(s2["clip"], s2["clips"]["16:9"], "主比例快捷路径同步切换")

    def test_idempotent_until_sources_change(self):
        from kinema.cli import stage_lipsync
        p = _chapter(self.tmp, [{"id": 1, "dur": 5.0, "speaker": "甲",
                                 "narration": "台词。"}])
        c = self._files(p, 1)
        p.data["shots"][0]["clips"] = {"16:9": c}
        stage_lipsync(p, self.store, self.router)
        lips = Path(p.data["shots"][0]["clips"]["16:9"])
        m1 = lips.stat().st_mtime
        stage_lipsync(p, self.store, self.router)
        self.assertEqual(lips.stat().st_mtime, m1, "源未变必须跳过，不重复计费")
        # 换音色：wav 变新 → 重算（换音色工作流 tts --force → lipsync 的判据）
        wav = p.subdir("audio") / "shot_1.wav"
        os.utime(wav, (time.time() + 5, time.time() + 5))
        stage_lipsync(p, self.store, self.router)
        self.assertGreater(lips.stat().st_mtime, m1)

    def test_non_dubbed_is_skipped(self):
        from kinema.cli import stage_lipsync
        p = _chapter(self.tmp, [{"id": 1, "dur": 5.0, "speaker": "甲",
                                 "narration": "台词。"}])
        p.data["motion"] = "native"
        c = self._files(p, 1)
        p.data["shots"][0]["clips"] = {"16:9": c}
        stage_lipsync(p, self.store, self.router)
        self.assertNotIn("clips_base", p.data["shots"][0],
                         "native 口型与发声同源，不经此步")

    def test_unconfigured_real_provider_skips_gracefully(self):
        """req_key/视觉密钥未配置时点名跳过——增强步不拦出片主链。"""
        from kinema.cli import stage_lipsync
        p = _chapter(self.tmp, [{"id": 1, "dur": 5.0, "speaker": "甲",
                                 "narration": "台词。"}])
        c = self._files(p, 1)
        p.data["shots"][0]["clips"] = {"16:9": c}
        stage_lipsync(p, self.store, ModelRouter(self.store))   # 真 provider，无 req_key
        self.assertNotIn("clips_base", p.data["shots"][0])
        self.assertEqual(p.data["shots"][0]["clips"]["16:9"], c, "底片原样保留")

    def test_skip_names_residual_shots_but_not_omitted(self):
        """跳过时的残差点名只看在产镜——弃镜的 clip/wav 还在盘，点名它只会
        把人引去修一个不进成片的镜。"""
        import contextlib
        import io

        from kinema import review, voicecast
        from kinema.cli import stage_lipsync
        p = _chapter(self.tmp, [
            {"id": 1, "dur": 5.0, "speaker": "甲", "narration": "台词。"},
            {"id": 2, "dur": 5.0, "speaker": "乙", "narration": "台词。"},
        ])
        for sid in (1, 2):
            p.data["shots"][sid - 1]["clip"] = self._files(p, sid)
        review.set_state(p.data["shots"][1], "shot", "omt")
        rep = {"sync": 0.5, "gap": 1.0, "mouth": 2.0, "speech": 4.0}
        buf = io.StringIO()
        with unittest.mock.patch.object(voicecast, "dubbed_sync_report",
                                        lambda *a, **k: dict(rep)):
            with contextlib.redirect_stdout(buf):
                stage_lipsync(p, self.store, ModelRouter(self.store))
        out = buf.getvalue()
        self.assertIn("镜 1 将带口型残差出片", out)
        self.assertNotIn("镜 2", out)


class TestVolcAdapter(unittest.TestCase):
    """volc 适配器的请求面：req_key 必须显式配置；提交体字段与签名头齐备。"""

    class _Store:
        def secret(self, name, required=True):
            return "test-" + name

    def test_req_key_is_mandatory_with_doc_pointer(self):
        from kinema.providers.lipsync.volc import VolcLipsyncProvider
        prov = VolcLipsyncProvider({}, self._Store())
        ok, why = prov.configured()
        self.assertFalse(ok)
        self.assertIn("req_key", why)
        self.assertIn("接口文档", why, "缺配置的提示必须给出到哪里查")

    def test_submit_body_and_signature_headers(self):
        from kinema.providers.lipsync import volc as m
        prov = m.VolcLipsyncProvider({"req_key": "rk_test",
                                      "price_per_second": 0.1}, self._Store())
        captured = {}

        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {"code": 10000, "data": {"task_id": "t-1"}}

        def fake(method, url, **kw):
            captured["url"] = url
            captured["headers"] = kw["headers"]
            captured["body"] = json.loads(kw["data"])
            return _R()
        orig = m.request_with_retry
        m.request_with_retry = fake
        try:
            data = prov._call("CVSync2AsyncSubmitTask", {
                "req_key": prov.req_key,
                prov.video_field: "https://x/v.mp4",
                prov.audio_field: "https://x/a.wav"})
        finally:
            m.request_with_retry = orig
        self.assertEqual(data["task_id"], "t-1")
        self.assertIn("Action=CVSync2AsyncSubmitTask", captured["url"])
        self.assertEqual(captured["body"]["req_key"], "rk_test")
        self.assertEqual(captured["body"]["video_url"], "https://x/v.mp4")
        self.assertEqual(captured["body"]["audio_url"], "https://x/a.wav")
        auth = captured["headers"]["Authorization"]
        self.assertIn("HMAC-SHA256 Credential=", auth)
        self.assertIn("/cn-north-1/cv/request", auth)
        self.assertIn("X-Date", captured["headers"])
        self.assertIn("X-Content-Sha256", captured["headers"])

    def test_local_paths_are_rejected_with_unlock_path(self):
        from kinema.errors import ProviderError
        from kinema.providers.lipsync.volc import VolcLipsyncProvider
        prov = VolcLipsyncProvider({"req_key": "rk"}, self._Store())
        with self.assertRaises(ProviderError) as ctx:
            prov.generate("/tmp/a.mp4", "https://x/a.wav", "/tmp/out.mp4")
        self.assertIn("公网 URL", str(ctx.exception))


class TestDefaultWiring(unittest.TestCase):
    """缺省档接线：dubbed 生视频收尾自动进入口型精修，--no-lipsync 才关。"""

    def test_gen_video_tail_runs_lipsync_by_default(self):
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertIn("if not native and not no_lipsync:", src)
        self.assertIn("stage_lipsync(project, store, router", src)

    def test_lipsync_registered_as_capability(self):
        from kinema.models import EMBEDDED_DEFAULTS, _ADAPTERS
        self.assertIn(("lipsync", "volc_lipsync"), _ADAPTERS)
        self.assertEqual(
            EMBEDDED_DEFAULTS["defaults"]["providers"]["lipsync"], "volc-lipsync")
        store = ConfigStore.load(None)
        self.assertEqual(store.default_provider("lipsync"), "volc-lipsync")
