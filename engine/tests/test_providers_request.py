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

"""Provider 请求体回归守护：拦截 HTTP 层断言请求拼装。

覆盖最贵、最易被官方口径变更打断的拼装面：
  · seedtts 四组合矩阵（clone × instruction/cot → expressive 切换 + use_tag_parser
    + X-Api-Resource-Id 覆盖）与 _parse_sse 截断防护；
  · seedance 时长钳制（dubbed 取上整 / native 四舍五入 / 2.0 档位 4~15s）与
    参考媒体 vs 首尾帧的内容模式互斥；
  · minimax 火山系音色跨厂防呆降级；
  · nano-banana（Gemini 图像）：snake_case 请求/camelCase 响应不对称、
    thought 中间图过滤、拦截双通道（blockReason/finishReason）；
  · wan（通义万相）：X-DashScope-Async 头、像素模式 size、n=1 防四倍费用陷阱；
    stdlib JWT 结构、dubbed 拒绝语义；
  · veo（Google Veo 3.1）：时长枚举 4/6/8、1080p 强制 8s、产物下载鉴权头、
    dubbed 拒绝语义；
  · MySQL _MIGRATE_COLUMNS 幂等（坏一次 ALTER 会砖掉买家存量库）。
全部离线：桩式 request_with_retry 捕获 body/headers，probe_duration 打桩。"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema.errors import ProviderError
from tests.support import LocalBackendEnv, fake_path


def _png_bytes(width: int, height: int) -> bytes:
    """合法 PNG（纯 stdlib）：验收体检要 ffprobe 真解出尺寸，占位字节会被当坏图拒收。"""
    import struct
    import zlib

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x80" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


class _Store:
    def secret(self, name, required=True):
        return "test-key"


class _Resp:
    def __init__(self, *, status=200, text="", jdata=None, headers=None, content=b""):
        self.status_code = status
        self.text = text
        self._j = jdata
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._j


def _sse(chunks=(b"AB", b"CD"), err=None):
    """构造 SSE 响应文本：event=352 音频分片 + 可选错误事件。"""
    blocks = []
    for c in chunks:
        blocks.append("event:352\ndata:" + json.dumps(
            {"code": 0, "data": base64.b64encode(c).decode()}))
    if err is not None:
        blocks.append("event:459\ndata:" + json.dumps(err))
    blocks.append("event:152\ndata:{}")
    return "\n\n".join(blocks)


class TestSeedTTSRequestMatrix(unittest.TestCase):
    """四组合矩阵：官方/复刻 × 有无指令(cot) —— expressive 切换是计费与表现力的分水岭。"""

    def _synth(self, *, text="你好", voice="v1", resource_id=None, instruction=None,
               emotion=None, emotion_scale=None, price=0.0):
        from kinema.providers.tts import seedtts as m
        prov = m.SeedTTSProvider({"price_per_kchar": price}, _Store())
        captured = {}

        def fake(method, url, **kw):
            captured.update(method=method, url=url, body=kw["json"],
                            headers=kw["headers"])
            return _Resp(text=_sse())

        with tempfile.TemporaryDirectory() as d, \
             mock.patch("kinema.providers._util.request_with_retry", fake), \
             mock.patch.object(m, "probe_duration", lambda p: 1.0):
            res = prov.synthesize(text, f"{d}/o.wav", voice=voice,
                                  resource_id=resource_id, instruction=instruction,
                                  emotion=emotion, emotion_scale=emotion_scale)
            captured["cost"] = res.cost
        return captured

    def test_official_voice_with_instruction_no_expressive(self):
        c = self._synth(instruction="用哽咽的语气说")
        rp = c["body"]["req_params"]
        self.assertNotIn("model", rp)                      # 官方音色不切 expressive
        adds = json.loads(rp["additions"])
        self.assertEqual(adds["context_texts"], ["用哽咽的语气说"])
        self.assertEqual(c["headers"]["X-Api-Resource-Id"], "seed-tts-2.0")

    def test_emotion_params_and_url(self):
        c = self._synth(emotion="angry", emotion_scale=5)
        ap = c["body"]["req_params"]["audio_params"]
        self.assertEqual((ap["emotion"], ap["emotion_scale"]), ("angry", 5))
        self.assertTrue(c["url"].endswith("/api/v3/tts/unidirectional/sse"))

    def test_price_per_kchar_cost(self):
        c = self._synth(text="字" * 500, price=0.45)
        self.assertAlmostEqual(c["cost"], 0.225)            # 500 字 × 0.45/千字符
        c0 = self._synth(text="字" * 500, price=0.0)
        self.assertEqual(c0["cost"], 0.0)                   # 未配置单价不计费



class TestDoubaoRequestMatrix(unittest.TestCase):
    """seed-audio-1.0（`/tts/create`）的四种生成模式与互斥判据。

    这四种在接口上是互斥的，而 `generate(**kwargs)` 那一侧「不认识就丢、不报错」——
    把冲突留给服务端等于把一次计费换成一条 4xx，故适配器在发请求前自检。"""

    def _synth(self, *, text="你好", conn=None, **kw):
        from kinema.providers.tts import doubao as m
        prov = m.DoubaoTTSProvider(dict(conn or {}), _Store())
        captured = {}

        def fake(method, url, **rq):
            captured.update(method=method, url=url, body=rq["json"],
                            headers=rq["headers"])
            return _Resp(jdata={"audio": base64.b64encode(b"AB").decode(),
                            "original_duration": 12.0,
                            "subtitle": {"sentences": [
                                {"text": "你好", "start_time": 0, "end_time": 1200,
                                 "words": [{"text": "你", "start_time": 0, "end_time": 600},
                                           {"text": "好", "start_time": 600,
                                            "end_time": 1200}]}]}})

        # patch 打在适配器自己的绑定上：它是 `from .._util import` 进来的，
        # 换掉 `_util` 那一份不影响已绑定的引用——真发出去就是一次带计费的请求
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake), \
             mock.patch.object(m, "probe_duration", lambda p: 1.0):
            res = prov.synthesize(text, f"{d}/o.mp3", **kw)
            captured.update(cost=res.cost, segments=res.segments)
        return captured

    def test_every_reference_resource_lives_inside_references(self):
        """接口的请求体顶层只有 model/text_prompt/references/audio_config/watermark，
        speaker 与 audio_data/audio_url、image_* 全是 references 条目内的字段。
        平铺到顶层会退化成「不传参考资源」那一档：服务端拿不到音色、逐句各造一把
        声音，而这一路没有任何报错。"""
        top = {"model", "text_prompt", "references", "audio_config", "watermark"}
        for kw in ({"voice": "v1"},
                   {"ref_audio_url": "https://x/a.mp3"},
                   {"ref_audios": ["https://x/1.mp3", "https://x/2.mp3"]},
                   {"ref_image_url": "https://x/a.png"},
                   {"prompt_only": True}):
            b = self._synth(**kw)["body"]
            self.assertEqual(set(b) - top, set(), f"{kw} 发出了未登记的顶层键：{b.keys()}")

    def test_speaker_is_a_references_entry(self):
        b = self._synth(voice="v1")["body"]
        self.assertEqual(b["references"], [{"speaker": "v1"}])

    def test_prompt_only_sends_no_speaker(self):
        """定制生成造音色走这条：完全按 text_prompt 的声线描述生成，不指定任何音色。"""
        b = self._synth(prompt_only=True)["body"]
        self.assertNotIn("speaker", b)
        self.assertNotIn("audio_data", b)
        self.assertNotIn("image_data", b)

    def test_single_audio_is_a_one_item_array_not_a_scalar(self):
        """单条也不折叠成标量：`@音频N` 是数组下标引用，且条目字段只有官方列出的
        那五个——多发一个未登记的键要么被忽略、要么触发参数校验失败。"""
        b = self._synth(ref_audio_url="https://x/a.mp3")["body"]
        self.assertEqual(b["references"], [{"audio_url": "https://x/a.mp3"}])
        self.assertNotIn("speaker", b)

    def test_multi_audio_uses_references_in_order(self):
        """`references` 的顺序就是 text_prompt 里 @音频N 的编号，错位即张冠李戴。"""
        b = self._synth(ref_audios=["https://x/1.mp3", "https://x/2.mp3"])["body"]
        self.assertEqual([r["audio_url"] for r in b["references"]],
                         ["https://x/1.mp3", "https://x/2.mp3"])

    def test_image_reference_excludes_audio_and_speaker(self):
        with self.assertRaises(ProviderError):
            self._synth(ref_image_url="https://x/a.png", ref_audio_url="https://x/a.mp3")
        with self.assertRaises(ProviderError):
            self._synth(ref_image_url="https://x/a.png", voice="v1")

    def test_reference_audio_count_capped(self):
        from kinema.providers.tts.doubao import DoubaoTTSProvider
        with self.assertRaises(ProviderError):
            self._synth(ref_audios=["u"] * (DoubaoTTSProvider.MAX_REF_AUDIO + 1))

    def test_audio_config_carries_all_three_rates(self):
        ac = self._synth(voice="v1", speech_rate=10, pitch_rate=-2,
                         loudness_rate=5)["body"]["audio_config"]
        self.assertEqual((ac["speech_rate"], ac["pitch_rate"], ac["loudness_rate"]),
                         (10, -2, 5))
        self.assertTrue(ac["enable_subtitle"])

    def test_watermark_only_when_configured(self):
        self.assertNotIn("watermark", self._synth(voice="v1")["body"])
        b = self._synth(voice="v1", conn={"watermark": {"aigc_watermark": True}})["body"]
        self.assertEqual(b["watermark"], {"aigc_watermark": True})

    def test_word_level_timestamps_parsed(self):
        segs = self._synth(voice="v1")["segments"]
        self.assertEqual((segs[0]["start"], segs[0]["end"]), (0.0, 1.2))
        self.assertEqual([w["text"] for w in segs[0]["words"]], ["你", "好"])
        self.assertEqual(segs[0]["words"][1]["start"], 0.6)

    def test_cost_prefers_original_duration(self):
        """官方以 original_duration 计费；秒价缺席才退回字符价，两者都无=不入账。"""
        self.assertAlmostEqual(
            self._synth(voice="v1", conn={"price_per_second": 0.1})["cost"], 1.2)
        self.assertAlmostEqual(
            self._synth(text="字" * 500, voice="v1",
                        conn={"price_per_kchar": 0.4})["cost"], 0.2)
        self.assertEqual(self._synth(voice="v1")["cost"], 0.0)


class TestSeedTTSParseSSE(unittest.TestCase):
    def test_chunks_join_in_order(self):
        from kinema.providers.tts.seedtts import SeedTTSProvider
        self.assertEqual(SeedTTSProvider._parse_sse(_sse((b"AB", b"CD"))), b"ABCD")

    def test_error_after_partial_raises(self):
        # 半句截断必须按失败处理，不得静默写盘
        from kinema.providers.tts.seedtts import SeedTTSProvider
        text = _sse((b"AB",), err={"code": 45000001, "message": "quota"})
        with self.assertRaises(ProviderError) as ctx:
            SeedTTSProvider._parse_sse(text)
        self.assertIn("45000001", str(ctx.exception))

    def test_empty_stream_returns_empty(self):
        from kinema.providers.tts.seedtts import SeedTTSProvider
        self.assertEqual(SeedTTSProvider._parse_sse("event:152\ndata:{}"), b"")


class TestSeedanceRequest(unittest.TestCase):
    def _gen(self, *, dur, ref_audio=False, last_frame=False,
             reference_video=None, reference_video_seconds=0.0, **kw):
        from kinema.providers.video import seedance as m
        prov = m.SeedanceProvider({"price_per_second": 1.0, **(kw.pop("_conn", None) or {})},
                                  _Store())
        captured = {}

        def fake(method, url, **kwargs):
            if method == "POST":
                captured["body"] = kwargs["json"]
                return _Resp(jdata={"id": "task-1"})
            return _Resp(jdata={"status": "succeeded",
                                "content": {"video_url": "https://x/v.mp4"}})

        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "f.png"
            img.write_bytes(b"\x89PNG fake")
            aud = Path(d) / "a.wav"
            aud.write_bytes(b"RIFF fake")
            with mock.patch.object(m, "request_with_retry", fake), \
                 mock.patch.object(m, "download", lambda u, o, **k: Path(o).write_bytes(b"v")):
                res = prov.generate(
                    str(img), f"{d}/out.mp4", prompt="转身", dur=dur,
                    ref_audio=str(aud) if ref_audio else None,
                    last_frame=str(img) if last_frame else None,
                    reference_video=reference_video,
                    reference_video_seconds=reference_video_seconds, **kw)
        captured["cost"] = res.cost
        captured["meta"] = res.meta
        captured["text"] = captured["body"]["content"][0]["text"]
        captured["roles"] = [c.get("role") for c in captured["body"]["content"][1:]]
        captured["types"] = [c.get("type") for c in captured["body"]["content"][1:]]
        return captured

    # ---- 2.0 顶层参数（不是 1.x 的 --suffix 后缀） ----
    def test_params_are_top_level_json_not_suffix_flags(self):
        """2.0 官方要求顶层 JSON 参数；1.x 后缀串未文档化、`--fps` 在 2.0 根本不存在。

        后缀串最坏的情形不是被拒，而是**被当成提示词正文的一部分**——那等于每次
        请求都在创作内容末尾附一串噪声 token，且我们给的分辨率/时长全部落空。
        """
        c = self._gen(dur=5, seed=42)
        b = c["body"]
        self.assertEqual(b["duration"], 5)
        # 兜底档 720p：本适配器服务的全部型号都开放的唯一公共档（2.0 系列与 2.5 都不开
        # 1080p）。别名忘写 resolution 时落的就是它，兜底高一档＝换回一个远端 400 且更贵
        self.assertEqual(b["resolution"], "720p")
        self.assertEqual(b["ratio"], "9:16")           # 默认 1080×1920
        self.assertEqual(b["seed"], 42)
        self.assertIs(b["watermark"], False)
        self.assertIs(b["generate_audio"], True)
        self.assertEqual(c["text"], "转身", "创作正文里不许再拼任何 --suffix")
        for flag in ("--resolution", "--ratio", "--duration", "--fps", "--seed"):
            self.assertNotIn(flag, c["text"])
        self.assertNotIn("fps", b, "2.0 固定 24fps，没有 fps 参数")

    def test_seed_omitted_when_none_and_camera_fixed_opt_in(self):
        b = self._gen(dur=5)["body"]
        self.assertNotIn("seed", b, "没给 seed 就不该发这个字段（发 null 会被判非法）")
        self.assertNotIn("camera_fixed", b, "锁镜是 best-effort，只在显式要时发")
        self.assertIs(self._gen(dur=5, camera_fixed=True)["body"]["camera_fixed"], True)

    def test_dubbed_ceils_duration(self):
        c = self._gen(dur=5.2, ref_audio=True)              # 对口型取上整防截话
        self.assertEqual(c["body"]["duration"], 6)
        self.assertAlmostEqual(c["cost"], 6.0)              # 6s × 1.0（1080p 实价）

    def test_native_rounds_duration(self):
        self.assertEqual(self._gen(dur=5.4)["body"]["duration"], 5)

    def test_duration_clamped_4_to_15(self):
        # Seedance 2.0 官方档位 4~15s（1.0 时代是 2~12，勿回退）
        self.assertEqual(self._gen(dur=0.5)["body"]["duration"], 4)
        self.assertEqual(self._gen(dur=30)["body"]["duration"], 15)

    # ---- 参考视频（V2V / previz 运动迁移） ----
    def test_reference_video_content_item_and_roles(self):
        """V2V(Mode B)：图挂 reference_image 锁外观、视频挂 reference_video 锁运动。"""
        c = self._gen(dur=6, reference_video="https://oss/previz.mp4",
                      reference_video_seconds=6.0)
        self.assertEqual(c["types"], ["image_url", "video_url"])
        self.assertEqual(c["roles"], ["reference_image", "reference_video"])
        self.assertEqual(c["body"]["content"][2]["video_url"],
                         {"url": "https://oss/previz.mp4"})

    def test_reference_video_carries_the_sheets(self):
        """V2V 也能附参考图。官方那条铁律拦的是 first/last frame 与参考媒体混发，
        而 V2V 的图本来就挂 `role=reference_image`——同 role 再挂几张不沾那条规则。
        写实档的复刻镜靠这一条把**受信身份图**送进请求：分镜图是图生图、天然不
        受信，人脸拒之后没有它就没有第二形态可退。"""
        c = self._gen(dur=6, reference_video="https://oss/ctl.mp4",
                      reference_video_seconds=6.0,
                      ref_images=["https://oss/char.png", "https://oss/scene.png"])
        self.assertEqual(c["types"], ["image_url", "video_url", "image_url", "image_url"])
        self.assertEqual(c["roles"], ["reference_image", "reference_video",
                                      "reference_image", "reference_image"])

    def test_reference_video_never_sends_first_or_last_frame(self):
        """V2V 分支不发首/末帧——发了就是同时给两套互斥的画面锚点。"""
        c = self._gen(dur=6, last_frame=True,
                      reference_video="https://oss/p.mp4", reference_video_seconds=4)
        self.assertNotIn("first_frame", c["roles"])
        self.assertNotIn("last_frame", c["roles"])

    def test_reference_video_rejects_local_path(self):
        """本地路径/data-url 一律抛错并给出开 OSS 的路径，**绝不 data-url 兜底**。"""
        from kinema.errors import ProviderError
        with self.assertRaises(ProviderError) as ctx:
            self._gen(dur=5, reference_video=fake_path("previz.mp4"))
        self.assertIn("公网 URL", str(ctx.exception))
        self.assertIn("oss", str(ctx.exception).lower())

    def test_v2v_billing_includes_input_video_seconds(self):
        """token 计费把输入视频秒也算进去——只算输出 = 每次少记一整段 previz 的钱。"""
        c = self._gen(dur=6, reference_video="https://oss/p.mp4",
                      reference_video_seconds=5.2)
        self.assertEqual(c["body"]["duration"], 6, "请求时长仍是**输出**秒，不含输入")
        self.assertAlmostEqual(c["cost"], 12.0, msg="6s 输出 + 6s 输入（5.2 上整）× ¥1/s")
        self.assertEqual(c["meta"]["input_seconds"], 6)
        self.assertEqual(c["meta"]["output_seconds"], 6)

    def test_input_video_seconds_clamped_to_official_range(self):
        from kinema.providers.video import seedance as m
        prov = m.SeedanceProvider({}, _Store())
        self.assertEqual(prov.input_video_seconds(0), 0)      # 未开 V2V
        self.assertEqual(prov.input_video_seconds(0.4), 2)    # 下限 2s
        self.assertEqual(prov.input_video_seconds(4.1), 5)    # 上整
        self.assertEqual(prov.input_video_seconds(99), 15)    # 上限 15s

    def test_only_seedance_advertises_v2v(self):
        """能力标志必须显式——`generate(**kwargs)` 会静默吞掉不支持的 reference_video，
        不查标志就发＝previz 完全没参与的一次普通首帧生成，而请求照常计费。"""
        from kinema.providers.video import seedance, veo
        from kinema.providers.base import VideoProvider
        self.assertIs(seedance.SeedanceProvider.supports_reference_video, True)
        self.assertIs(VideoProvider.supports_reference_video, False)
        self.assertIs(veo.VeoVideoProvider.supports_reference_video, False)

    # ---- 额外参考图（简笔分镜板） ----
    def test_ref_images_only_in_reference_media_mode(self):
        """附板只在 dubbed（参考媒体模式）合法：排在图/音频之后。**首帧模式混发
        必须抛错**——官方拒绝 first/last frame 与 reference media 混用
        （400 InvalidParameter），静默丢图更坏：提示词里的「所附分镜板」会指向
        一个不存在的参考。"""
        from kinema.errors import ProviderError
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / "board.png"
            b.write_bytes(b"\x89PNG fake board")
            c2 = self._gen(dur=5, ref_audio=True, ref_images=[str(b)])
            self.assertEqual(c2["roles"],
                             ["reference_image", "reference_audio", "reference_image"])
            with self.assertRaises(ProviderError) as cm:
                self._gen(dur=5, ref_images=[str(b)])
            self.assertIn("首帧模式", str(cm.exception))
            with self.assertRaises(ProviderError):
                self._gen(dur=5, last_frame=True, ref_images=[str(b)])

    def test_reference_only_sends_pure_reference_content(self):
        """参考生视频（全能参考）：image 与 ref_images 全挂 reference_image，
        无首/末帧槽（last_frame 传了也忽略）；ratio 显式给——参考任务的 adaptive
        =模型自选比例，画布锁死时不显式给就可能拿回竖版。"""
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / "board.png"
            b.write_bytes(b"\x89PNG fake board")
            c = self._gen(dur=5, reference_only=True, last_frame=True,
                          ref_images=[str(b)])
        self.assertEqual(c["roles"], ["reference_image", "reference_image"])
        self.assertNotIn("first_frame", c["roles"])
        self.assertNotIn("last_frame", c["roles"])
        self.assertNotEqual(c["body"]["ratio"], "adaptive")

    def test_ref_images_capped_at_seven(self):
        """官方全图 ≤9 张——额外参考图钳到 7（合法宿主只有 dubbed 参考媒体模式）。"""
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(10):
                p = Path(d) / f"r{i}.png"
                p.write_bytes(b"\x89PNG fake")
                paths.append(str(p))
            c = self._gen(dur=5, ref_audio=True, ref_images=paths)
            # 8 = 分镜图那张 reference_image + 7 张额外参考图
            self.assertEqual(c["roles"].count("reference_image"), 8)

    def test_reference_images_capability_flags(self):
        """同 V2V 款能力闸：只有 seedance 与 mock 声明支持额外参考图。"""
        from kinema.providers.base import VideoProvider
        from kinema.providers.video import seedance, veo
        from kinema.providers.video.mock import MockVideoProvider
        self.assertIs(seedance.SeedanceProvider.supports_reference_images, True)
        self.assertIs(MockVideoProvider.supports_reference_images, True)
        self.assertIs(VideoProvider.supports_reference_images, False)
        self.assertIs(getattr(veo.VeoVideoProvider, "supports_reference_images"), False)

    def test_4k_override_switches_param_and_price(self):
        # CLI --resolution 4k 运行时覆盖：参数串与计费单价同步切换（预估与实际同源）
        from kinema.providers.video import seedance as m
        captured = {}

        def fake(method, url, **kw):
            if method == "POST":
                captured["body"] = kw["json"]
                return _Resp(jdata={"id": "task-1"})
            return _Resp(jdata={"status": "succeeded",
                                "content": {"video_url": "https://x/v.mp4"}})

        def run(conn):
            prov = m.SeedanceProvider(conn, _Store())
            prov.resolution = "4k"          # 模拟 _apply_resolution 运行时覆盖
            with tempfile.TemporaryDirectory() as d:
                img = Path(d) / "f.png"
                img.write_bytes(b"\x89PNG fake")
                with mock.patch.object(m, "request_with_retry", fake), \
                     mock.patch.object(m, "download",
                                       lambda u, o, **k: Path(o).write_bytes(b"v")):
                    return prov.generate(str(img), f"{d}/o.mp4", dur=4)

        res = run({"price_per_second": 1.0, "price_per_second_4k": 2.0})
        # 2.0 顶层参数：分辨率走顶层字段，不拼进创作正文
        self.assertEqual(captured["body"]["resolution"], "4k")
        self.assertAlmostEqual(res.cost, 8.0)          # 4s × 2.0（4K 档实价）
        self.assertEqual(res.meta["resolution"], "4k")
        res = run({"price_per_second": 1.0})           # 未配 4K 单价 → 回落基准价
        self.assertAlmostEqual(res.cost, 4.0)

    def test_ref_audio_excludes_last_frame(self):
        # 参考媒体模式与首尾帧互斥：dubbed 下 last_frame 必须被忽略
        c = self._gen(dur=4, ref_audio=True, last_frame=True)
        self.assertEqual(c["roles"], ["reference_image", "reference_audio"])

    def test_native_first_and_last_frame(self):
        c = self._gen(dur=4, last_frame=True)
        self.assertEqual(c["roles"], ["first_frame", "last_frame"])

    def test_last_frame_capability_is_per_alias_and_defaults_true(self):
        """末帧能力位随别名声明，缺省 True——首尾帧是主流能力，逐个开容易漏配。"""
        from kinema.providers.video.seedance import SeedanceProvider
        self.assertTrue(SeedanceProvider({}, _Store()).supports_last_frame)
        self.assertFalse(SeedanceProvider({"supports_last_frame": False},
                                          _Store()).supports_last_frame)

    def test_unsupported_last_frame_raises_instead_of_going_out_silently(self):
        """**服务端对不认识的 role 只丢不报**——`doubao-seedance-2-0-fast` 实测接受
        first_frame、静默丢弃 last_frame。发出去的后果是提示词按「运动须自然收束在
        末帧上」写、日志与页面都标着「末帧→镜N」，成片却一条缝都没衔接
        （chrome3/ch01 前四镜：首帧与分镜图是同一张，末帧与下一镜分镜图 SSIM
        0.19~0.29，与随机两张的 0.21~0.23 无异）。

        仲裁点在 `cli._shot_plan`；这里是与「首帧模式禁附参考图」同款的兜底硬拦——
        走到这里说明有调用方绕过了仲裁，宁可抛错也不静默发出去。
        """
        from kinema.errors import ProviderError
        with self.assertRaises(ProviderError) as cm:
            self._gen(dur=4, last_frame=True, _conn={"supports_last_frame": False})
        self.assertIn("last_frame", str(cm.exception))

    def test_unsupported_alias_still_sends_first_frame(self):
        """能力位只关末帧那一个槽：首帧照发，本镜退回纯首帧生成而不是整镜失败。"""
        c = self._gen(dur=4, _conn={"supports_last_frame": False})
        self.assertEqual(c["roles"], ["first_frame"])


class TestMiniMaxVoiceGuard(unittest.TestCase):
    def _synth(self, voice, conn=None):
        from kinema.providers.tts import minimax as m
        prov = m.MiniMaxTTSProvider(conn or {}, _Store())
        captured = {}

        def fake(method, url, **kw):
            captured["body"] = kw["json"]
            return _Resp(jdata={"data": {"audio": b"aa".hex()}})

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake), \
             mock.patch.object(m, "probe_duration", lambda p: 1.0):
            prov.synthesize("你好", f"{d}/o.mp3", voice=voice)
        return captured["body"]["voice_setting"]["voice_id"]

    def test_volcano_voice_downgrades_to_default(self):
        # 跨厂防呆：火山系音色对 MiniMax 无意义 → 降级本厂默认而非 4xx
        for bad in ("zh_male_x_uranus_bigtts", "ICL_uranus_zh_male_a_tob", "S_abc12345"):
            self.assertEqual(self._synth(bad), "Chinese (Mandarin)_Male_Announcer")

    def test_native_minimax_voice_passes_through(self):
        self.assertEqual(self._synth("female-shaonv"), "female-shaonv")

    def test_default_voice_follows_the_site(self):
        """两站系统音色 ID 命名互不相通：拿国内拼音短名打国际站（或反之）必被拒。
        缺省音色跟随 base_url 所在站；显式配置恒赢。"""
        cn = {"base_url": "https://api.minimaxi.com/v1"}
        self.assertEqual(self._synth(None, conn=cn), "male-qn-qingse")
        self.assertEqual(self._synth(None), "Chinese (Mandarin)_Male_Announcer")
        self.assertEqual(self._synth(None, conn={"voice": "female-shaonv"}),
                         "female-shaonv")


class TestMiniMaxTTSRequest(unittest.TestCase):
    """按官方 t2a_v2 文档钉死请求体与响应解析——偏离文档的每一处都是静默失效。"""

    def _call(self, conn=None, **kw):
        from kinema.providers.tts import minimax as m
        prov = m.MiniMaxTTSProvider(conn or {}, _Store())
        cap = {}

        def fake(method, url, **kwargs):
            cap["url"], cap["body"] = url, kwargs["json"]
            return _Resp(jdata={"base_resp": {"status_code": 0},
                                "data": {"audio": b"aa".hex()},
                                "extra_info": {"usage_characters": 26}})

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake), \
             mock.patch.object(m, "probe_duration", lambda p: 1.0):
            r = prov.synthesize("你好", f"{d}/o.mp3", **kw)
        cap["result"] = r
        return cap

    def test_emotion_reaches_the_request(self):
        """引擎每镜都下发 emotion（voicecast 不门控），若被 **kwargs 静默吞掉，
        走 minimax 的画风逐镜情绪整条失效且零提示。"""
        vs = self._call(emotion="angry")["body"]["voice_setting"]
        self.assertEqual(vs["emotion"], "angry")
        # 官方没有强度参数，火山那套 emotion_scale 应被丢弃而不是硬发
        vs2 = self._call(emotion="sad", emotion_scale=4)["body"]["voice_setting"]
        self.assertEqual(vs2["emotion"], "sad")
        self.assertNotIn("emotion_scale", vs2)
        # 非法情绪不硬发（会 400），降级中性并告警
        self.assertNotIn("emotion", self._call(emotion="狂喜")["body"]["voice_setting"])

    def test_speech_rate_is_converted_not_passed_through(self):
        """工程内是火山刻度（-50~100，0=原速），官方要的是 0.5~2 的倍率。
        若只认全流程从不下发的 `speed` 键，语速设置恒失效。"""
        self.assertEqual(self._call()["body"]["voice_setting"]["speed"], 1.0)
        self.assertAlmostEqual(self._call(speech_rate=50)["body"]["voice_setting"]["speed"], 1.5)
        # 钳进官方合法区间：火山的 -50 原样透传会直接 400
        self.assertAlmostEqual(self._call(speech_rate=-90)["body"]["voice_setting"]["speed"], 0.5)
        self.assertAlmostEqual(self._call(speech_rate=500)["body"]["voice_setting"]["speed"], 2.0)

    def test_group_id_is_optional_and_output_format_is_explicit(self):
        """当前 t2a_v2 的官方参数表里没有 GroupId，鉴权只有 Bearer；
        做成必填会让只有 API Key 的用户连试都试不了。"""
        from kinema.providers.tts import minimax as m

        class _NoGroup:
            def secret(self, name, required=True):
                return None if "GROUP" in name else "test-key"

        prov = m.MiniMaxTTSProvider({}, _NoGroup())
        cap = {}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry",
                               lambda meth, url, **kw: (cap.update(url=url, body=kw["json"]),
                                                        _Resp(jdata={"data": {"audio": b"a".hex()}}))[1]), \
             mock.patch.object(m, "probe_duration", lambda p: 1.0):
            prov.synthesize("你好", f"{d}/o.mp3")
        self.assertNotIn("GroupId", cap["url"])
        self.assertIn("GroupId", self._call()["url"])      # 配了就照带
        self.assertEqual(cap["body"]["output_format"], "hex")   # 别隐式依赖平台缺省
        self.assertEqual(cap["body"]["subtitle_type"], "sentence")

    def test_business_error_on_http_200_is_surfaced(self):
        """鉴权/余额/风控可以是 HTTP 200 + base_resp.status_code≠0 + data 为 null。"""
        from kinema.providers.tts import minimax as m
        prov = m.MiniMaxTTSProvider({}, _Store())
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
                m, "request_with_retry",
                lambda *a, **k: _Resp(jdata={"base_resp": {"status_code": 1004,
                                                           "status_msg": "insufficient balance"}})):
            with self.assertRaises(ProviderError) as e:
                prov.synthesize("x", f"{d}/o.mp3")
        self.assertIn("1004", str(e.exception))
        self.assertIn("insufficient balance", str(e.exception))

    def test_cost_uses_the_platform_reported_characters(self):
        """官方口径「1 个汉字算 2 个字符」，按 len(text) 估中文会系统性低估约一半；
        响应里回吐了权威值就用它。"""
        cap = self._call({"price_per_kchar": 1.0})
        self.assertAlmostEqual(cap["result"].cost, 26 / 1000.0)

    def test_subtitle_url_is_read_from_data_not_extra_info(self):
        """字幕链接在 data 下（extra_info 是 usage/audio_length 那九项）。
        取错父对象 + 裸 except 的结果是 subtitle_enable 白开、时间戳恒取不到。"""
        src = (Path(__file__).resolve().parent.parent
               / "kinema/providers/tts/minimax.py").read_text(encoding="utf-8")
        self.assertIn('sub_url = (j.get("data") or {}).get("subtitle_file")', src)
        self.assertNotIn('(j.get("extra_info") or {}).get("subtitle_file")', src)

    def test_default_model_matches_the_config_source(self):
        """兜底常量与 yaml、内嵌表三处同源——任一处掉队就会发官方已 legacy 的型号。"""
        from kinema.providers.tts import minimax as m
        self.assertEqual(m.MiniMaxTTSProvider({}, _Store()).model, "speech-2.8-hd")


class TestMiniMaxH3Request(unittest.TestCase):
    """H3 走 v2，与 Hailuo v1 是两套协议——照抄 v1 的形状会全线错。"""

    def _gen(self, conn=None, image=None, **kw):
        from kinema.providers.video import minimax as m
        prov = m.MiniMaxVideoProvider(conn or {}, _Store())
        cap = {}

        def fake(method, url, **kwargs):
            cap.setdefault("calls", []).append((method, url))
            if method == "POST":
                cap["body"] = kwargs["json"]
                cap["headers"] = kwargs.get("headers") or {}
                return _Resp(jdata={"task_id": "424010985738629"})
            return _Resp(jdata={"task": {"status": "succeeded",
                                         "content": {"url": "https://x/v.mp4"}}})

        # 轮询骨架首查先于任何 sleep（poll_task），首查即完成的假响应零等待，
        # 测试无须给 sleep 打桩
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake), \
             mock.patch.object(m, "download", lambda u, o: Path(o).write_bytes(b"m")):
            cap["result"] = prov.generate(image, f"{d}/o.mp4", **kw)
        return cap

    def test_content_array_with_roles_not_flat_fields(self):
        """v2 把首帧/尾帧收进 content[] 靠 role 区分；v1 那种扁平
        first_frame_image 字段在这里根本不存在。"""
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "a.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")
            cap = self._gen(image=str(img), last_frame=str(img), prompt="推近", dur=6)
        body = cap["body"]
        self.assertNotIn("first_frame_image", body)
        roles = [c.get("role") for c in body["content"]]
        self.assertEqual(roles, [None, "first_frame", "last_frame"])
        self.assertEqual(body["content"][0]["type"], "text")     # 必须有非空 text 项
        self.assertEqual(body["model"], "MiniMax-H3")
        self.assertEqual(body["resolution"], "768P")
        self.assertEqual(body["duration"], 6)

    def test_duration_is_clamped_into_the_official_range(self):
        """官方只收 4~15 的整数：低于/高于都会被直接拒。"""
        from kinema.providers.video import minimax as m
        prov = m.MiniMaxVideoProvider({}, _Store())
        self.assertEqual(prov.billable_seconds(2), 4)
        self.assertEqual(prov.billable_seconds(20), 15)
        self.assertEqual(prov.billable_seconds(6.4), 6)
        self.assertEqual(prov.billable_seconds(6.4, dubbed=True), 7)   # 对口型取上整防截话

    def test_poll_reads_lowercase_status_and_takes_url_directly(self):
        """v2 的状态是小写枚举、且查询直接给 content.url——照抄 v1 的
        首字母大写判断会永远等不到完成，照抄 v1 的 file_id 换取会多一次请求。"""
        cap = self._gen(prompt="x", dur=5)
        methods = [m for m, _ in cap["calls"]]
        self.assertEqual(methods[0], "POST")
        self.assertIn("GET", methods)
        self.assertTrue(any("/query/video_generation/424010985738629" in u
                            for _, u in cap["calls"]), "task_id 必须走 path 参数")
        self.assertFalse(any("files/retrieve" in u for _, u in cap["calls"]))
        self.assertTrue(cap["result"].has_audio)     # 输出恒带原生立体声

    def test_local_endpoint_sends_no_auth_header(self):
        """auth: none 的自托管端点不该被迫填一把假密钥。"""
        cap = self._gen({"auth": "none"}, prompt="x", dur=5)
        self.assertNotIn("Authorization", cap["headers"])
        cap2 = self._gen(prompt="x", dur=5)
        self.assertIn("Authorization", cap2["headers"])

    def test_dubbed_ref_audio_is_refused_not_swallowed(self):
        """官方明写「图生视频与多模态参考生视频互斥」——首帧图 + 参考音频对口型
        在 H3 上协议层不成立。不显式拒的话它会被 `**kwargs` 静默吞掉：不报错、
        口型对不上、H3 恒带的原生立体声还与我们的 TTS 撞轨，而钱照烧。
        同目录 veo 有同一道闸，两家口径必须一致。"""
        from kinema.providers.video import minimax as m
        prov = m.MiniMaxVideoProvider({}, _Store())
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ProviderError) as e:
                prov.generate(None, f"{d}/o.mp4", dur=5, ref_audio=f"{d}/a.wav")
        self.assertIn("seedance", str(e.exception))     # 要给出路，不能只说不行

    def test_seed_is_never_sent(self):
        """官方创建接口的请求体里没有 seed 字段——H3 不提供种子复现。
        发一个 schema 之外的参数是纯风险（服务端是忽略还是拒，官方没说）。"""
        self.assertNotIn("seed", self._gen(prompt="x", dur=5, seed=12345)["body"])

    def test_resolution_is_renormalized_before_sending(self):
        """`--resolution` 走 `_apply_resolution` 直接赋值、绕开构造器的白名单，
        而它的档位名（480p/720p/1080p/4k）没有一个是 H3 的合法值。"""
        from kinema.providers.video import minimax as m
        prov = m.MiniMaxVideoProvider({}, _Store())
        prov.resolution = "1080p"                 # 模拟 cli 的直接赋值
        cap = {}

        def fake(method, url, **kw):
            if method == "POST":
                cap["body"] = kw["json"]
                return _Resp(jdata={"task_id": "t1"})
            return _Resp(jdata={"task": {"status": "succeeded",
                                         "content": {"url": "https://x/v.mp4"}}})

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake), \
             mock.patch.object(m, "download", lambda u, o: Path(o).write_bytes(b"m")):
            prov.generate(None, f"{d}/o.mp4", prompt="x", dur=5)
        self.assertIn(cap["body"]["resolution"], m.RESOLUTIONS)

    def test_expired_is_not_an_official_status(self):
        from kinema.providers.video import minimax as m
        self.assertEqual(m._FAIL, ("failed", "cancelled"))

    def test_input_video_seconds_are_billed(self):
        from kinema.providers.video import minimax as m
        self.assertEqual(m.MiniMaxVideoProvider({}, _Store()).input_video_seconds(4.2), 5)


class TestMiniMaxImageRequest(unittest.TestCase):
    def _gen(self, conn=None, **kw):
        from kinema.providers.image import minimax as m
        prov = m.MiniMaxImageProvider(conn or {}, _Store())
        cap = {}

        def fake(method, url, **kwargs):
            cap["body"], cap["url"] = kwargs["json"], url
            return _Resp(jdata={"base_resp": {"status_code": 0},
                                "data": {"image_base64": [base64.b64encode(b"img").decode()]}})

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake):
            prov.generate("画一只猫", f"{d}/o.png", **kw)
        return cap

    def test_prompt_optimizer_is_off_and_format_is_base64(self):
        """提示词是本工程逐句拼装的契约，让平台自动改写等于把画风前缀
        与负面约束交给别人重写。"""
        body = self._gen(width=1920, height=1080)["body"]
        self.assertFalse(body["prompt_optimizer"])
        self.assertEqual(body["response_format"], "base64")

    def test_pixels_beat_aspect_ratio_so_images_match_the_canvas(self):
        """官方 aspect_ratio 的每一档都绑定固定分辨率（16:9 恒 1280×720），且
        同时给两者时**比例优先**。本工程三档画布全部精确命中枚举——走比例那条路
        只能拿到画布 44% 的像素量，合成时再放大 1.5 倍，每张分镜图都发虚且零告警。"""
        for w, h in ((1920, 1080), (1080, 1920), (1080, 1080)):
            body = self._gen(width=w, height=h)["body"]
            self.assertEqual((body.get("width"), body.get("height")), (w, h), f"{w}x{h}")
            self.assertNotIn("aspect_ratio", body)
        # 出界（官方像素档只收 [512,2048]）才降级到比例档
        body = self._gen(width=4096, height=2304)["body"]
        self.assertIn("aspect_ratio", body)
        self.assertNotIn("width", body)

    def test_reference_image_format_and_size_are_gated_locally(self):
        """官方只收 JPG/JPEG/PNG 且 <10MB。本地判死比让服务端回一个
        语焉不详的业务码强——本工程的设定图默认是 png，webp 会被拒。"""
        from kinema.providers.image import minimax as m
        prov = m.MiniMaxImageProvider({}, _Store())
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "a.webp"
            bad.write_bytes(b"RIFF")
            with self.assertRaises(ProviderError) as e:
                prov._ref(str(bad))
        self.assertIn("JPG", str(e.exception))

    def test_character_ref_kind_declared_and_consulted(self):
        """`ref_kind="character"` 能力位：设定集清单首张恒是场景全景
        （场景→取景地→角色→道具序），subject_reference 盲取首张=把 SCENE 图
        标成 type=character 发出去、日志只说「N 张只用第一张」看不出错在哪张。
        调用方必须按能力位筛出场角色设定图。"""
        import inspect

        from kinema import cli
        from kinema.project import Project
        from kinema.providers.image.minimax import MiniMaxImageProvider
        self.assertEqual(MiniMaxImageProvider.ref_kind, "character")
        self.assertEqual(MiniMaxImageProvider.max_ref_images, 1)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            scene, ca, cb = root / "scene.png", root / "hero.png", root / "extra.png"
            for f in (scene, ca, cb):
                f.write_bytes(b"\x89PNG\r\n\x1a\n")
            data = {"scene_ref": str(scene),
                    "characters": [{"name": "主角", "sheet": str(ca)},
                                   {"name": "配角", "sheet": str(cb)}],
                    "shots": [{"id": 1, "characters": ["主角"]}]}
            p = Project(root / "ch.json", data)
            self.assertEqual(p.character_sheet_refs(p.shots[0]), [str(ca)],
                             "只发出场角色的设定图")
            self.assertTrue(p.design_refs(p.shots[0])[0].endswith("scene.png"),
                            "完整设定集首张是场景——正是不能盲取首张的原因")
        src = inspect.getsource(cli.stage_gen_image)
        self.assertIn("ref_kind", src, "分镜生图的取参必须分流 ref_kind")
        self.assertIn("character_sheet_refs", src)

    def test_only_one_reference_image_is_sent(self):
        """官方每次只收一张且只支持 type=character——多垫图场景该走 seedream。"""
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.png", Path(d) / "b.png"
            for f in (a, b):
                f.write_bytes(b"\x89PNG\r\n\x1a\n")
            body = self._gen(ref_images=[str(a), str(b)])["body"]
        self.assertEqual(len(body["subject_reference"]), 1)
        self.assertEqual(body["subject_reference"][0]["type"], "character")

    def test_overlong_prompt_is_rejected_not_silently_truncated(self):
        from kinema.providers.image import minimax as m
        prov = m.MiniMaxImageProvider({}, _Store())
        from kinema.errors import ProviderError
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry") as request:
            with self.assertRaisesRegex(ProviderError, "禁止静默截断"):
                prov.generate("字" * 2000, f"{d}/o.png")
        request.assert_not_called()


class TestMiniMaxMusicRequest(unittest.TestCase):
    def _gen(self, conn=None, **kw):
        from kinema.providers.music import minimax as m
        prov = m.MiniMaxMusicProvider(conn or {}, _Store())
        cap = {}

        def fake(method, url, **kwargs):
            cap["url"], cap["body"] = url, kwargs["json"]
            return _Resp(jdata={"base_resp": {"status_code": 0},
                                "data": {"audio": b"mp3".hex(), "status": 2},
                                "extra_info": {"music_duration": 90000}})

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake):
            cap["result"] = prov.generate("温暖的钢琴", f"{d}/bgm.mp3", **kw)
        return cap

    def test_instrumental_by_default(self):
        """成片里已有旁白与对白，带人声的配乐会和旁白抢同一条频段——混音链的让路 EQ
        与 sidechain 闪避是按「器乐床 + 人声主轨」标定的。"""
        cap = self._gen(duration=60)
        self.assertTrue(cap["body"]["is_instrumental"])
        self.assertNotIn("lyrics", cap["body"])
        self.assertEqual(cap["body"]["output_format"], "hex")   # 别隐式依赖平台缺省
        # 显式给出唱词时才关掉纯器乐档
        self.assertFalse(self._gen(duration=60, lyrics="[verse]\n夜色")["body"]["is_instrumental"])

    def test_length_limits_are_enforced_client_side(self):
        body = self._gen(duration=60, lyrics="词" * 5000)["body"]
        self.assertEqual(len(body["lyrics"]), 3500)
        from kinema.providers.music import minimax as m
        prov = m.MiniMaxMusicProvider({}, _Store())
        cap = {}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
                m, "request_with_retry",
                lambda *a, **k: (cap.update(body=k["json"]),
                                 _Resp(jdata={"data": {"audio": b"m".hex()}}))[1]):
            prov.generate("字" * 3000, f"{d}/b.mp3")
        self.assertEqual(len(cap["body"]["prompt"]), 2000)

    def test_business_error_on_http_200_is_surfaced(self):
        from kinema.providers.music import minimax as m
        prov = m.MiniMaxMusicProvider({}, _Store())
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
                m, "request_with_retry",
                lambda *a, **k: _Resp(jdata={"base_resp": {"status_code": 2013,
                                                           "status_msg": "invalid params"}})):
            with self.assertRaises(ProviderError) as e:
                prov.generate("x", f"{d}/b.mp3")
        self.assertIn("2013", str(e.exception))

    def test_priced_per_track_not_per_minute(self):
        """官方是每首一口价（至多 5 分钟），按分钟折算会算错账。"""
        self.assertAlmostEqual(self._gen({"price_per_track": 1.0}, duration=60)["result"].cost, 1.0)


class TestNanoBananaRequest(unittest.TestCase):
    """Gemini 图像：请求端 snake_case / 响应端 camelCase 的不对称是最易失败点。"""

    _FINAL = base64.b64encode(b"IMG-FINAL").decode()
    _INTERIM = base64.b64encode(b"IMG-THOUGHT").decode()

    def _gen(self, *, ref=False, width=1080, height=1920, image_size="2K", jdata=None):
        from kinema.providers.image import nano_banana as m
        prov = m.NanoBananaProvider(
            {"price_per_image": 0.95, "image_size": image_size}, _Store())
        captured = {}
        default_j = {"candidates": [{"content": {"parts": [
            {"text": "构图说明"},
            {"inlineData": {"mimeType": "image/png", "data": self._INTERIM},
             "thought": True},
            {"inlineData": {"mimeType": "image/png", "data": self._FINAL}},
        ]}, "finishReason": "STOP"}]}

        def fake(method, url, **kw):
            captured.update(method=method, url=url, body=kw["json"],
                            headers=kw["headers"])
            return _Resp(jdata=default_j if jdata is None else jdata)

        with tempfile.TemporaryDirectory() as d:
            refs = []
            if ref:
                rp = Path(d) / "char_主角.png"
                rp.write_bytes(b"\x89PNG fake")
                refs = [str(rp)]
            with mock.patch.object(m, "request_with_retry", fake):
                res = prov.generate("本镜差异", f"{d}/o.png", ref_images=refs,
                                    width=width, height=height, seed=42)
                captured["out"] = Path(res.path).read_bytes()
                captured["cost"] = res.cost
        return captured

    def test_request_shape_and_auth(self):
        c = self._gen()
        self.assertTrue(c["url"].endswith(
            "/v1beta/models/gemini-3-pro-image:generateContent"))
        self.assertEqual(c["headers"]["x-goog-api-key"], "test-key")
        parts = c["body"]["contents"][0]["parts"]
        self.assertEqual(parts[-1], {"text": "本镜差异"})
        gc = c["body"]["generationConfig"]
        # 官方 REST 口径：modalities 恒 TEXT+IMAGE，比例/分辨率在 responseFormat.image
        self.assertEqual(gc["responseModalities"], ["TEXT", "IMAGE"])
        self.assertEqual(gc["responseFormat"],
                         {"image": {"aspectRatio": "9:16", "imageSize": "2K"}})
        self.assertNotIn("imageConfig", gc)   # 现行文档已无此字段，不得发送
        self.assertNotIn("seed", gc)          # 图像模型 seed 未证实——不得发送

    def test_ref_image_snake_case_inline(self):
        # 请求端官方 curl 口径是 snake_case inline_data/mime_type
        parts = self._gen(ref=True)["body"]["contents"][0]["parts"]
        ref = parts[0]["inline_data"]
        self.assertEqual(ref["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(ref["data"]), b"\x89PNG fake")

    def test_takes_last_non_thought_image(self):
        # Gemini 3 thinking 会先出中间构图图（thought: true）——必须取最后一张正式图
        self.assertEqual(self._gen()["out"], b"IMG-FINAL")

    def test_landscape_and_no_image_size(self):
        c = self._gen(width=1920, height=1080, image_size="")
        cfg = c["body"]["generationConfig"]["responseFormat"]["image"]
        self.assertEqual(cfg["aspectRatio"], "16:9")
        self.assertNotIn("imageSize", cfg)     # 2.5-flash 不支持，置 "" 必须省略

    def test_prompt_block_and_no_image_raise(self):
        with self.assertRaises(ProviderError) as ctx:
            self._gen(jdata={"candidates": [],
                             "promptFeedback": {"blockReason": "IMAGE_SAFETY"}})
        self.assertIn("IMAGE_SAFETY", str(ctx.exception))
        with self.assertRaises(ProviderError) as ctx:   # HTTP 200 但没出图
            self._gen(jdata={"candidates": [{"content": {"parts": [{"text": "x"}]},
                                             "finishReason": "NO_IMAGE"}]})
        self.assertIn("NO_IMAGE", str(ctx.exception))

    def test_oversized_inline_refs_rejected(self):
        # inline 请求体 ~20MB 上限：越限前给可行动报错，而非把 32MB 打给 Google 收 400
        from kinema.providers.image import nano_banana as m
        prov = m.NanoBananaProvider({}, _Store())
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "big.png"
            big.write_bytes(b"\x00" * 16_000_000)
            with self.assertRaises(ProviderError) as ctx:
                prov.generate("x", f"{d}/o.png", ref_images=[str(big), str(big)])
        self.assertIn("20MB", str(ctx.exception))

    def test_http_ref_fetch_error_raises(self):
        # 过期签名 URL 的错误页字节绝不能当参考图内联上送（错误根因不被掩盖）
        from kinema.providers.image import nano_banana as m
        with mock.patch.object(m, "request_with_retry",
                               lambda *a, **k: _Resp(status=403, text="expired")):
            with self.assertRaises(ProviderError) as ctx:
                m._inline("https://oss/expired.png")
        self.assertIn("403", str(ctx.exception))


class TestWanImageRequest(unittest.TestCase):
    """通义万相新版 messages 协议：异步头 / 像素模式 size / n=1 费用陷阱。"""

    def _gen(self, *, ref=False, n_refs=1, seed=None, poll_j=None, model=None):
        from kinema.providers.image import wan as m
        conn = {"price_per_image": 0.2, "poll_interval": 0}
        if model:
            conn["model"] = model
        prov = m.WanImageProvider(conn, _Store())
        captured = {}

        def fake(method, url, **kw):
            if method == "POST":
                captured.update(url=url, body=kw["json"], headers=kw["headers"])
                return _Resp(jdata={"output": {"task_status": "PENDING",
                                               "task_id": "t-1"}})
            return _Resp(jdata=poll_j if poll_j is not None else
                         {"output": {"task_status": "SUCCEEDED", "choices": [
                             {"message": {"content": [
                                 {"text": "x"}, {"image": "https://oss/x.png"}]}}]}})

        with tempfile.TemporaryDirectory() as d:
            refs = []
            if ref:
                rp = Path(d) / "ref.png"
                rp.write_bytes(b"\x89PNG fake")
                refs = [str(rp)] * n_refs
            with mock.patch.object(m, "request_with_retry", fake), \
                 mock.patch.object(m, "download",
                                   lambda u, o, **k: Path(o).write_bytes(b"i")):
                res = prov.generate("画一间花店", f"{d}/o.png", ref_images=refs,
                                    seed=seed)
                captured["cost"] = res.cost
        return captured

    def test_async_header_pixel_size_and_n1(self):
        c = self._gen()
        self.assertTrue(c["url"].endswith(
            "/api/v1/services/aigc/image-generation/generation"))
        self.assertEqual(c["headers"]["X-DashScope-Async"], "enable")
        self.assertEqual(c["headers"]["Authorization"], "Bearer test-key")
        p = c["body"]["parameters"]
        self.assertEqual(p["size"], "1080*1920")   # 像素模式：档位模式有参考图时比例会漂
        self.assertEqual(p["n"], 1)                # wan2.6-t2i 默认 n=4，不显式传=四倍费用
        self.assertIs(p["watermark"], False)
        content = c["body"]["input"]["messages"][0]["content"]
        self.assertEqual(content[-1], {"text": "画一间花店"})
        self.assertAlmostEqual(c["cost"], 0.2)

    def test_ref_image_data_url_and_seed_clamp(self):
        c = self._gen(ref=True, seed=2 ** 33 + 5)
        content = c["body"]["input"]["messages"][0]["content"]
        self.assertTrue(content[0]["image"].startswith("data:image/png;base64,"))
        self.assertEqual(c["body"]["parameters"]["seed"],
                         (2 ** 33 + 5) % 2147483648)   # 官方上限 2^31-1

    def test_failed_task_raises(self):
        with self.assertRaises(ProviderError) as ctx:
            self._gen(poll_j={"output": {"task_status": "FAILED",
                                         "message": "内容审核未通过"}})
        self.assertIn("内容审核未通过", str(ctx.exception))

    def test_ref_cap_follows_model_tier(self):
        # 参考图上限按模型分档：wan2.7 系 9 张；wan2.6-image 4 张；t2i 纯文生图直接忽略
        def n_imgs(c):
            return sum(1 for x in c["body"]["input"]["messages"][0]["content"]
                       if "image" in x)
        self.assertEqual(n_imgs(self._gen(ref=True, n_refs=6, model="wan2.6-image")), 4)
        self.assertEqual(n_imgs(self._gen(ref=True, n_refs=2, model="wan2.6-t2i")), 0)
        self.assertEqual(n_imgs(self._gen(ref=True, n_refs=10)), 9)


class TestVeoRequest(unittest.TestCase):
    """Veo 3.1：时长枚举 4/6/8 / 1080p 强制 8s / 产物下载必须带 API key 头。"""

    def _gen(self, *, dur, resolution="720p", width=1080, height=1920,
             last_frame=False, ref_audio=False, poll_j=None):
        from kinema.providers.video import veo as m
        prov = m.VeoVideoProvider(
            {"price_per_second": 0.72, "poll_interval": 0,
             "resolution": resolution}, _Store())
        captured = {"dl": {}}

        def fake(method, url, **kw):
            if method == "POST":
                captured.update(url=url, body=kw["json"], headers=kw["headers"])
                return _Resp(jdata={
                    "name": "models/veo-3.1-fast-generate-preview/operations/op-1"})
            captured["poll_url"] = url
            return _Resp(jdata=poll_j if poll_j is not None else
                         {"done": True, "response": {"generateVideoResponse": {
                             "generatedSamples": [
                                 {"video": {"uri": "https://dl/v.mp4"}}]}}})

        def fake_dl(u, o, **k):
            captured["dl"].update(url=u, headers=k.get("headers"))
            Path(o).write_bytes(b"v")

        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "f.png"
            img.write_bytes(b"\x89PNG fake")
            with mock.patch.object(m, "request_with_retry", fake), \
                 mock.patch.object(m, "download", fake_dl):
                res = prov.generate(
                    str(img), f"{d}/o.mp4", prompt="slow pan", dur=dur,
                    width=width, height=height,
                    last_frame=str(img) if last_frame else None,
                    ref_audio=f"{d}/a.wav" if ref_audio else None)
            captured["cost"] = res.cost
            captured["has_audio"] = res.has_audio
        return captured

    def test_duration_snaps_to_enum(self):
        self.assertEqual(self._gen(dur=5.4)["body"]["parameters"]["durationSeconds"], 6)
        self.assertEqual(self._gen(dur=4.4)["body"]["parameters"]["durationSeconds"], 4)
        self.assertEqual(self._gen(dur=30)["body"]["parameters"]["durationSeconds"], 8)
        # 等距点取大档（引擎默认镜长恰为 5s）：宁多尾帧不截话
        self.assertEqual(self._gen(dur=5.0)["body"]["parameters"]["durationSeconds"], 6)
        self.assertAlmostEqual(self._gen(dur=5.4)["cost"], 4.32)   # 6s × 0.72

    def test_1080p_forces_8s(self):
        c = self._gen(dur=4, resolution="1080p")
        self.assertEqual(c["body"]["parameters"]["durationSeconds"], 8)

    def test_last_frame_interpolation_forces_8s(self):
        # 官方对首尾帧插值强制 8s——720p 也不例外，发 4/6 会被拒
        c = self._gen(dur=4, last_frame=True)
        self.assertEqual(c["body"]["parameters"]["durationSeconds"], 8)
        self.assertAlmostEqual(c["cost"], 5.76)    # 8s × 0.72：计费与取档同源
        # billable_seconds 是 dry-run 与真发共用的取档口径，须同样收 last_frame 位
        from kinema.providers.video import veo as m
        prov = m.VeoVideoProvider({"resolution": "720p"}, _Store())
        self.assertEqual(prov.billable_seconds(4, last_frame=True), 8)
        self.assertEqual(prov.billable_seconds(4, last_frame=False), 4)

    def test_request_shape_and_poll_url(self):
        c = self._gen(dur=6, last_frame=True)
        self.assertTrue(c["url"].endswith(
            "/v1beta/models/veo-3.1-fast-generate-preview:predictLongRunning"))
        self.assertEqual(c["headers"]["x-goog-api-key"], "test-key")
        inst = c["body"]["instances"][0]
        self.assertEqual(inst["prompt"], "slow pan")
        self.assertEqual(inst["image"]["inlineData"]["mimeType"], "image/png")
        self.assertIn("lastFrame", inst)               # 首尾帧插值须与 image 同用
        p = c["body"]["parameters"]
        self.assertEqual(p["aspectRatio"], "9:16")
        self.assertEqual(p["personGeneration"], "allow_adult")   # 图生视频唯一合法值
        self.assertNotIn("negativePrompt", p)          # 3.1 文档已移除该参数
        self.assertNotIn("seed", p)
        # operation name 整串原样拼在 v1beta/ 后
        self.assertTrue(c["poll_url"].endswith(
            "/v1beta/models/veo-3.1-fast-generate-preview/operations/op-1"))
        self.assertTrue(c["has_audio"])                # 3.1 原生音频恒开

    def test_landscape_aspect(self):
        c = self._gen(dur=6, width=1920, height=1080)
        self.assertEqual(c["body"]["parameters"]["aspectRatio"], "16:9")

    def test_download_carries_api_key_header(self):
        # 产物 uri 裸 GET 会 4xx——必须带 x-goog-api-key
        c = self._gen(dur=6)
        self.assertEqual(c["dl"]["headers"], {"x-goog-api-key": "test-key"})

    def test_ref_audio_rejected_and_operation_error(self):
        with self.assertRaises(ProviderError) as ctx:
            self._gen(dur=6, ref_audio=True)
        self.assertIn("seedance", str(ctx.exception))
        with self.assertRaises(ProviderError) as ctx:
            self._gen(dur=6, poll_j={"done": True,
                                     "error": {"code": 13, "message": "blocked"}})
        self.assertIn("blocked", str(ctx.exception))

    def test_http_frame_fetch_error_raises(self):
        # 过期签名 URL 的错误页字节绝不能当首帧内联上送
        from kinema.providers.video import veo as m
        with mock.patch.object(m, "request_with_retry",
                               lambda *a, **k: _Resp(status=403, text="expired")):
            with self.assertRaises(ProviderError):
                m._inline("https://oss/expired.png")


class TestElevenLabsMusicRequest(unittest.TestCase):
    """ElevenLabs Music/SFX：模型与产物格式不显式钉死就交给了平台缺省，
    时长出官方合法域是建请求 400。"""

    def _music(self, *, duration=60.0, conn=None):
        from kinema.providers.music import elevenlabs as m
        prov = m.ElevenLabsMusicProvider(conn or {"price_per_min": 0.9}, _Store())
        cap = {}

        def fake(method, url, **kw):
            cap.update(url=url, body=kw["json"], params=kw["params"],
                       headers=kw["headers"])
            return _Resp(content=b"mp3-bytes")

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake):
            r = prov.generate("宁静的钢琴底床", f"{d}/o.mp3", duration=duration)
        cap["result"] = r
        return cap

    def test_music_pins_model_format_and_instrumental(self):
        c = self._music()
        self.assertTrue(c["url"].endswith("/v1/music"))
        self.assertEqual(c["headers"]["xi-api-key"], "test-key")
        b = c["body"]
        self.assertEqual(b["model_id"], "music_v2")        # 平台过渡期缺省是 v1
        self.assertIs(b["force_instrumental"], True)       # BGM 纯器乐纪律
        self.assertEqual(b["music_length_ms"], 60000)
        # 产物格式走查询参数：缺省 auto 会随模型切采样率
        self.assertEqual(c["params"], {"output_format": "mp3_44100_128"})
        self.assertAlmostEqual(c["result"].cost, 0.9)

    def test_music_length_clamped_into_official_range(self):
        # 官方合法域 [3000, 600000] 毫秒，出界是 400——本地钳制并告警
        self.assertEqual(self._music(duration=1.0)["body"]["music_length_ms"], 3000)
        self.assertEqual(self._music(duration=700.0)["body"]["music_length_ms"], 600000)

    def test_sfx_duration_clamped_to_official_range(self):
        from kinema.providers.music import elevenlabs as m
        prov = m.ElevenLabsMusicProvider({}, _Store())
        cap = {}

        def fake(method, url, **kw):
            cap.update(url=url, body=kw["json"])
            return _Resp(content=b"mp3")

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(m, "request_with_retry", fake):
            prov.sound_effect("金属剑刃出鞘", f"{d}/s.mp3", duration=45.0)
        self.assertTrue(cap["url"].endswith("/v1/sound-generation"))
        self.assertEqual(cap["body"]["duration_seconds"], 30.0)   # 官方上限 30s


class TestGenVideo4kGate(unittest.TestCase):
    """4K 二次确认节点：口头要 4K 可越过配置默认档，但正式生成必须显式授权。"""

    def _gate(self, **kw):
        from kinema.cli import _gate_4k
        return _gate_4k(kw.pop("resolution", "4k"), **kw)

    def test_4k_without_ack_blocked(self):
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError) as ctx:
            self._gate()
        self.assertIn("--yes", str(ctx.exception))     # 报错必须给出解锁路径

    def test_4k_passes_with_yes_dryrun_or_mock(self):
        self.assertIsNone(self._gate(yes=True))        # 二次授权后放行
        self.assertIsNone(self._gate(dry_run=True))    # 看报价不花钱，免授权
        self.assertIsNone(self._gate(mock=True))       # 离线不花钱，免授权

    def test_non_4k_never_blocked(self):
        for r in (None, "480p", "720p", "1080p"):      # 默认档与普通档不触发节点
            self.assertIsNone(self._gate(resolution=r))


class _FakeCursor:
    """记录 SQL 的假游标：INFORMATION_SCHEMA 查询返回预设已有列。"""

    def __init__(self, existing_cols):
        self.existing = existing_cols
        self.executed = []
        self._last_table = None

    def execute(self, sql, args=None):
        self.executed.append(sql.strip())
        if "INFORMATION_SCHEMA" in sql:
            self._last_table = (args or ("",))[0]

    def fetchall(self):
        return [(c,) for c in self.existing.get(self._last_table, [])]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestMysqlMigrateIdempotent(unittest.TestCase):
    def _run(self, existing_extra):
        from kinema.storage.mysql import MySQLStorage, _SCHEMA
        import re as _re
        # 基线：全部建表列齐备；existing_extra 控制迁移列是否已存在
        with tempfile.TemporaryDirectory() as d:
            st = MySQLStorage(Path(d), {"table_prefix": "kn_"})
            existing = {}
            for table, cols in MySQLStorage._MIGRATE_COLUMNS.items():
                have = set(existing_extra.get(table, cols))   # 缺省=全有
                existing[f"kn_{table}"] = list(have)
            cur = _FakeCursor(existing)

            class _Conn:
                def cursor(self):
                    return cur

            st._conn = _Conn()
            st.ensure_schema()
        return [s for s in cur.executed if s.startswith("ALTER TABLE")]

    def test_all_columns_present_zero_alter(self):
        self.assertEqual(self._run({}), [])                 # 幂等：重复连库零 ALTER

    def test_missing_column_gets_exactly_one_alter(self):
        from kinema.storage.mysql import MySQLStorage
        table, cols = next(iter(MySQLStorage._MIGRATE_COLUMNS.items()))
        missing = next(iter(cols))
        have = [c for c in cols if c != missing]
        alters = self._run({table: have})
        self.assertEqual(len(alters), 1)
        self.assertIn(missing, alters[0])
        self.assertIn(f"kn_{table}", alters[0])


class _FakeWidenCursor(_FakeCursor):
    """加宽通道的假游标：按查询是否点名 CHARACTER_MAXIMUM_LENGTH 分流两套返回。"""

    def __init__(self, add_cols, widen_lens):
        super().__init__(add_cols)
        self.widen = widen_lens          # {kn_table: {col: char_len}}
        self._widen_q = False

    def execute(self, sql, args=None):
        self._widen_q = "CHARACTER_MAXIMUM_LENGTH" in sql
        super().execute(sql, args)

    def fetchall(self):
        if self._widen_q:
            return [(c, n) for c, n in self.widen.get(self._last_table, {}).items()]
        return super().fetchall()


class TestMysqlWidenIdempotent(unittest.TestCase):
    """列加宽迁移守卫——典型失败：小说层把 characters[].role 养成近百字富文本定位，
    按「主角/反派」短标签设计的 VARCHAR(64) 当场 DataError 1406；截断写入=库里
    静默丢数据而文件侧全须全尾，两边从此对不上。加宽必须幂等（重复连库零 ALTER），
    长度未知（TEXT 列 NULL / 旧版单列返回）一律不动。"""

    def _run(self, role_len):
        from kinema.storage.mysql import MySQLStorage
        with tempfile.TemporaryDirectory() as d:
            st = MySQLStorage(Path(d), {"table_prefix": "kn_"})
            add_cols = {f"kn_{t}": list(cols)
                        for t, cols in MySQLStorage._MIGRATE_COLUMNS.items()}
            widen = {f"kn_{t}": {c: role_len for c in cols}
                     for t, cols in MySQLStorage._MIGRATE_WIDEN.items()}
            cur = _FakeWidenCursor(add_cols, widen)

            class _Conn:
                def cursor(self):
                    return cur

            st._conn = _Conn()
            st.ensure_schema()
        return [s for s in cur.executed if "MODIFY COLUMN" in s]

    def test_narrow_column_gets_widened(self):
        alters = self._run(64)
        self.assertEqual(len(alters), 1)
        self.assertIn("role", alters[0])
        self.assertIn("VARCHAR(255)", alters[0])

    def test_already_at_target_zero_alter(self):
        self.assertEqual(self._run(255), [])

    def test_wider_than_target_zero_alter(self):
        # 用户自行放宽过的列不许改窄回来
        self.assertEqual(self._run(1024), [])

    def test_unknown_length_zero_alter(self):
        # TEXT 列 CHARACTER_MAXIMUM_LENGTH 为 NULL / 旧 fake 单列返回——一律不动
        self.assertEqual(self._run(None), [])

    def test_widen_targets_match_schema(self):
        # 注册表目标必须与 _SCHEMA 建表 DDL 一致——新库建出来就该是宽的，
        # 否则新库还得靠加宽迁移补课，两处口径分叉
        from kinema.storage.mysql import MySQLStorage, _SCHEMA
        for table, cols in MySQLStorage._MIGRATE_WIDEN.items():
            block = _SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {{p}}{table}", 1)[1] \
                           .split("---", 1)[0]
            for col, (chars, _ddl) in cols.items():
                self.assertIn(f"{col}         VARCHAR({chars})", block,
                              f"_SCHEMA 里 {table}.{col} 未同步加宽到 {chars}")


class TestMysqlSoftDeleteColumn(unittest.TestCase):
    """软删列守卫：is_deleted 是唯一删除语义——建表、缺列迁移、upsert 三处必须同齐，
    漏任何一处都会让"清单过滤 is_deleted=0"在库侧失真。"""

    def test_is_deleted_registered_everywhere(self):
        import inspect
        from kinema.storage.mysql import MySQLStorage, _SCHEMA
        proj_block = _SCHEMA.split("CREATE TABLE IF NOT EXISTS {p}project", 1)[1] \
                            .split("---", 1)[0]
        self.assertIn("is_deleted", proj_block)              # 建表列
        self.assertIn("is_deleted",
                      MySQLStorage._MIGRATE_COLUMNS["project"])   # 缺列迁移登记
        src = inspect.getsource(MySQLStorage._upsert_project)
        self.assertIn("is_deleted=VALUES(is_deleted)", src)  # upsert 同步

    def test_physical_delete_removed(self):
        # 唯一删除语义=逻辑删除：任何存储后端都没有物理删除通道
        from kinema.storage import base, local, mysql
        for mod in (base.Storage, local.LocalStorage, mysql.MySQLStorage):
            self.assertFalse(hasattr(mod, "delete_project"),
                             f"{mod.__name__} 不应再有 delete_project（只能逻辑删除）")


class TestSeedreamPixelTier(unittest.TestCase):
    """按实际出图像素落档：画布以内低档，`--hd` 放大跨阈值后高档。"""

    def test_cost_follows_output_pixels(self):
        from kinema.providers.image.seedream import SeedreamProvider
        prov = SeedreamProvider({"price_per_image": 0.3, "price_per_image_hd": 0.6,
                                 "hd_pixels": 2_360_000}, None)
        self.assertEqual(prov.cost_for(1920, 1080), 0.3)
        self.assertEqual(prov.cost_for(2864, 1611), 0.6)
        flat = SeedreamProvider({"price_per_image": 0.3}, None)
        self.assertEqual(flat.cost_for(2864, 1611), 0.3, "未配高档单价时沿用低档")


class TestAgentImageOrder(unittest.TestCase):
    """agent 工单 provider（providers/image/agent.py）：不发网络请求的两态契约——
    缺图开单即抛（走批末汇总不中断），有图零成本验收登记；同 path 重开不叠
    重影；stage 与 adapter 必须共享工单常量。"""

    def _mk(self, root, exists=False):
        from kinema.providers.image import agent as m
        out = Path(root) / "images" / "shot_1.png"
        if exists:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_png_bytes(1920, 1080))
        return m, m.AgentImageProvider({}, None), out

    @contextlib.contextmanager
    def _agent_workspace(self):
        """搭一份离线 agent 工作区；所有 CLI 都被本地后端环境守卫包住。"""
        from kinema.cli import build_parser
        from kinema.storage import load_storage_config

        backend = LocalBackendEnv()
        backend.enable()
        try:
            with tempfile.TemporaryDirectory() as ws, \
                    mock.patch.dict(os.environ, {"KINEMA_AGENT_IMAGEGEN": "1"}), \
                    mock.patch("kinema.config_overlay.read", return_value={}):
                load_storage_config(reload=True)

                def run(*argv):
                    ns = build_parser().parse_args(list(argv) + ["--workspace", ws])
                    return ns.func(ns)

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    run("project", "new", "--title", "t", "--id", "p")
                    run("chapter", "new", "p", "--title", "c")
                chapter = Path(ws) / "p" / "chapters" / "ch01.json"
                yield Path(ws), chapter, run, output
        finally:
            backend.restore()

    @staticmethod
    def _set_shot(chapter: Path, **fields):
        data = json.loads(chapter.read_text(encoding="utf-8"))
        data["script"] = {"hook": "h", "body": "b", "cta": "c"}
        shot = {"id": 1, "dur": 3, "narration": "第一镜。",
                "image_prompt": "测试画面"}
        shot.update(fields)
        data["shots"] = [shot]
        chapter.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_writes_order_and_raises(self):
        with tempfile.TemporaryDirectory() as td:
            m, prov, out = self._mk(td)
            self.assertEqual(prov.max_ref_images, 5,
                             "agent 工单必须声明当前原生 image_gen 的参考图上限")
            with self.assertRaises(ProviderError) as ctx:
                prov.generate("提示词A", str(out), ref_images=["r1.png"],
                              seed=7, width=1920, height=1080, label="SHOT 1")
            self.assertIn(m.PENDING_MARK, str(ctx.exception))
            doc = json.loads((out.parent / m.ORDER_BASENAME)
                             .read_text(encoding="utf-8"))
            self.assertTrue(doc.get("_readme"), "工单必须自带操作说明")
            [e] = doc["orders"]
            self.assertEqual(e["prompt"], "提示词A")
            self.assertEqual((e["width"], e["height"]), (1920, 1080))
            self.assertEqual(e["ref_images"], ["r1.png"])
            self.assertEqual(e["path"], str(out))
            self.assertEqual(e["label"], "SHOT 1")
            # 同 path 重开：换提示词后单里只留最新一条，不叠重影
            with self.assertRaises(ProviderError):
                prov.generate("提示词B", str(out), width=1920, height=1080)
            doc = json.loads((out.parent / m.ORDER_BASENAME)
                             .read_text(encoding="utf-8"))
            [e] = doc["orders"]
            self.assertEqual(e["prompt"], "提示词B")

    def test_existing_file_ingests_at_zero_cost(self):
        with tempfile.TemporaryDirectory() as td:
            m, prov, out = self._mk(td, exists=True)
            res = prov.generate("提示词", str(out), width=1920, height=1080)
            self.assertEqual(res.cost, 0.0)
            self.assertEqual(res.path, str(out))
            self.assertTrue(res.meta.get("ingested"))
            self.assertFalse((out.parent / m.ORDER_BASENAME).exists(),
                             "验收路径不该开工单")

    def test_wrong_aspect_or_unreadable_delivery_is_refused_and_order_kept(self):
        """验收与素材直供同一把尺：比例不符或解不出图像流即拒收，工单保留待重画。"""
        with tempfile.TemporaryDirectory() as td:
            m, prov, out = self._mk(td)
            with self.assertRaises(ProviderError):
                prov.generate("提示词", str(out), width=1920, height=1080)   # 开单
            out.write_bytes(_png_bytes(400, 400))
            with self.assertRaisesRegex(ProviderError, "宽高比"):
                prov.generate("提示词", str(out), width=1920, height=1080)
            self.assertTrue(m.has_pending_order(out), "拒收后工单必须保留")
            out.write_bytes(b"not-a-png")
            with self.assertRaisesRegex(ProviderError, "不可用"):
                prov.generate("提示词", str(out), width=1920, height=1080)
            out.write_bytes(_png_bytes(1920, 1080))
            res = prov.generate("提示词", str(out), width=1920, height=1080)
            self.assertTrue(res.meta.get("ingested"))
            self.assertFalse(m.has_pending_order(out))

    def test_prompt_drift_since_order_is_carried_into_the_record(self):
        with tempfile.TemporaryDirectory() as td:
            m, prov, out = self._mk(td)
            with self.assertRaises(ProviderError):
                prov.generate("开单时的稿", str(out), width=1920, height=1080)
            out.write_bytes(_png_bytes(1920, 1080))
            res = prov.generate("改过的稿", str(out), width=1920, height=1080)
            self.assertEqual(res.meta.get("order_prompt"), "开单时的稿")

    def test_stage_shares_the_order_constants(self):
        """cli.stage_gen_image 的工单重置与批末指引必须 import 同一对常量——
        字符串各写一份，改名那天就是「单开了但收尾认不出来」。"""
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_image)
        self.assertIn("ORDER_BASENAME", src)
        self.assertIn("PENDING_MARK", src)

    def test_multi_aspect_opens_full_order_in_one_run(self):
        """多比例下首个 pending 不得中止本镜循环——工单必须一轮开全，否则
        agent 每画一张就要重跑一趟（回归：worker 循环首异常即弃剩余项）。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m
        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            with contextlib.redirect_stdout(output), \
                    self.assertRaises(KinemaError) as ctx:
                # --image-per-aspect 才是每比例一图（不带它时单图 compose 裁切，
                # 一条单本来就是对的）——多文件场景必须一轮开全
                run("gen-image", "--chapter", "p/ch01",
                    "--aspects", "16:9,9:16", "--image-per-aspect")
            self.assertIn(m.PENDING_MARK, str(ctx.exception))
            order = json.loads(
                (ws / "p" / "chapters" / "ch01_work" / "images"
                 / m.ORDER_BASENAME).read_text(encoding="utf-8"))
            self.assertEqual(len(order["orders"]), 2, "两个比例的单必须一轮开全")
            dims = {(e["width"], e["height"]) for e in order["orders"]}
            self.assertEqual(dims, {(1920, 1080), (1080, 1920)})
            for entry in order["orders"]:
                Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))

            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01",
                    "--aspects", "16:9,9:16", "--image-per-aspect")
            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertEqual(set(shot["images"]), {"16:9", "9:16"})
            self.assertEqual(shot["gen"]["image"]["provider"], "agent")
            self.assertEqual(shot["review"]["image"]["state"], "wfa")

    def test_retake_round_trip_accepts_the_delivered_image(self):
        """返工轮：开单那一轮归档旧版；agent 交付后重跑必须验收，不得再归档待验收物。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m

        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME

            def deliver():
                [entry] = json.loads(order_path.read_text(encoding="utf-8"))["orders"]
                Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))
                return entry["path"]

            with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
                run("gen-image", "--chapter", "p/ch01")
            first = deliver()
            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01")
                run("review", "set", "--chapter", "p/ch01", "--shots", "1",
                    "--stage", "image", "--state", "retake", "--note", "重画")
            with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
                run("gen-image", "--chapter", "p/ch01")
            self.assertFalse(Path(first).is_file(), "开单轮把旧版移进版本栈")
            deliver()
            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01")
            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertTrue(Path(shot["image"]).is_file())
            self.assertEqual(shot["review"]["image"]["state"], "wfa")
            self.assertEqual(len(shot["versions"]["image"]), 1, "只有旧版进版本栈")
            self.assertEqual(shot["gen"]["image"]["version"], 2)
            self.assertFalse(order_path.exists())

    def test_completed_single_order_is_ingested_with_provenance(self):
        """agent 写完规范目标文件后，重跑必须走统一登记链而不是普通断点复用。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m

        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
                run("gen-image", "--chapter", "p/ch01")
            order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME
            [entry] = json.loads(order_path.read_text(encoding="utf-8"))["orders"]
            Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))

            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01")

            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertEqual(shot["gen"]["image"]["provider"], "agent")
            self.assertEqual(shot["review"]["image"]["state"], "wfa")
            self.assertFalse(order_path.exists())

    def test_completed_order_wins_over_stale_existing_image_path(self):
        """旧章节仍有 image 路径时，待验收工单也必须回填本轮提示词。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m

        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
                run("gen-image", "--chapter", "p/ch01")
            order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME
            [entry] = json.loads(order_path.read_text(encoding="utf-8"))["orders"]
            Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))

            data = json.loads(chapter.read_text(encoding="utf-8"))
            shot = data["shots"][0]
            shot["image"] = entry["path"]
            shot["gen"] = {"image": {"provider": "agent", "prompt": "旧提示词"}}
            shot["review"] = {"image": {"state": "wfa"}}
            chapter.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01")

            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertIn("测试画面", shot["gen"]["image"]["prompt"])
            self.assertNotEqual(shot["gen"]["image"]["prompt"], "旧提示词")
            self.assertFalse(order_path.exists())

    def test_completed_candidate_orders_are_ingested_with_provenance(self):
        """候选图同样必须登记生成参数与待审状态，不能只把文件路径塞回章节。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m

        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
                run("gen-image", "--chapter", "p/ch01", "--candidates", "2")
            order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME
            orders = json.loads(order_path.read_text(encoding="utf-8"))["orders"]
            self.assertEqual(len(orders), 2)
            for entry in orders:
                Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))

            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01", "--candidates", "2")

            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertEqual(len(shot["image_candidates"]), 2)
            self.assertEqual(shot["gen"]["image_candidates"]["provider"], "agent")
            self.assertEqual(shot["gen"]["image_candidates"]["count"], 2)
            self.assertNotIn("image", shot["gen"], "候选不占画布，画布快照由 pick 落")
            self.assertEqual(shot["review"]["image"]["state"], "wfa")
            self.assertFalse(order_path.exists())

    def _accepted_canvas(self, ws, chapter, run, output):
        """开单 → 交付 → 验收，返回验收后的镜文档。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m
        with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
            run("gen-image", "--chapter", "p/ch01")
        order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME
        for entry in json.loads(order_path.read_text(encoding="utf-8"))["orders"]:
            Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))
        with contextlib.redirect_stdout(output):
            run("gen-image", "--chapter", "p/ch01")
        return json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]

    @staticmethod
    def _attach_clip(chapter: Path, **extra):
        data = json.loads(chapter.read_text(encoding="utf-8"))
        shot = data["shots"][0]
        clip = chapter.parent / "ch01_work" / "clips" / "shot_1.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"mp4")
        shot["clip"] = str(clip)
        shot.setdefault("review", {})["clip"] = {"state": "wfa"}
        shot.update(extra)
        chapter.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_accept_existing_with_unchanged_canvas_keeps_the_clip(self):
        """`--accept-existing` 只重编译登记：画布像素未变，存量片段不退化、判定不作废。"""
        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            self._accepted_canvas(ws, chapter, run, output)
            self._attach_clip(chapter, consistency={"verdict": "ok"})
            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01", "--accept-existing")
            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertEqual(shot["review"]["clip"]["state"], "wfa")
            self.assertEqual(shot["consistency"]["verdict"], "ok")
            self.assertIn("envelope", shot["gen"]["image"])

    def test_candidates_on_an_existing_canvas_leave_canvas_state_alone(self):
        """候选是待选品：画布在盘且未重做时，片段、判定、画布快照与审阅态一律不动。"""
        from kinema.errors import KinemaError
        from kinema.providers.image import agent as m
        with self._agent_workspace() as (ws, chapter, run, output):
            self._set_shot(chapter)
            before = self._accepted_canvas(ws, chapter, run, output)
            self._attach_clip(chapter, consistency={"verdict": "ok"})
            with contextlib.redirect_stdout(output), self.assertRaises(KinemaError):
                run("gen-image", "--chapter", "p/ch01", "--candidates", "2")
            order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME
            for entry in json.loads(order_path.read_text(encoding="utf-8"))["orders"]:
                Path(entry["path"]).write_bytes(_png_bytes(entry["width"], entry["height"]))
            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01", "--candidates", "2")
            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertEqual(shot["review"]["clip"]["state"], "wfa")
            self.assertEqual(shot["consistency"]["verdict"], "ok")
            self.assertEqual(shot["gen"]["image"]["prompt"], before["gen"]["image"]["prompt"])
            self.assertEqual(shot["review"]["image"]["state"], before["review"]["image"]["state"])
            self.assertEqual(shot["gen"]["image_candidates"]["count"], 2)
            with contextlib.redirect_stdout(output):
                run("pick", "--chapter", "p/ch01", "--shot", "1", "--use", "1")
            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            self.assertEqual(shot["review"]["clip"]["state"], "retake", "定稿上画布才退化片段")
            self.assertNotIn("consistency", shot)
            self.assertEqual(shot["gen"]["image"]["candidate"], 1)
            self.assertEqual(shot["gen"]["image"]["prompt"],
                             shot["gen"]["image_candidates"]["prompt"])

    def test_registered_url_without_provenance_is_not_reordered(self):
        """URL 媒体字段已代表产出；缺 gen 元数据不能触发 agent 重画。"""
        from kinema.providers.image import agent as m

        with self._agent_workspace() as (ws, chapter, run, output):
            url = "https://cdn.example.test/shot-1.png"
            self._set_shot(chapter, image=url)
            with contextlib.redirect_stdout(output):
                run("gen-image", "--chapter", "p/ch01")

            shot = json.loads(chapter.read_text(encoding="utf-8"))["shots"][0]
            order_path = ws / "p" / "chapters" / "ch01_work" / "images" / m.ORDER_BASENAME
            self.assertEqual(shot["image"], url)
            self.assertNotIn("image", shot.get("gen") or {})
            self.assertFalse(order_path.exists())


class TestDataUrlCache(unittest.TestCase):
    """参考图 data URL 缓存：同文件复用编码结果、文件一改立刻失效（键含
    mtime+size）——失效不灵的症状是「改了设定图但一致性没变」，比不缓存更坏。"""

    def test_cache_hits_and_invalidates_on_change(self):
        from kinema.providers import _util
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ref.png"
            f.write_bytes(b"\x89PNG-a")
            u1 = _util.file_to_data_url(f)
            self.assertIs(_util.file_to_data_url(f), u1)   # 命中缓存（同一对象）
            f.write_bytes(b"\x89PNG-bb")                   # 内容与大小都变 → 键变
            u2 = _util.file_to_data_url(f)
            self.assertNotEqual(u1, u2)
            self.assertIn(base64.b64encode(b"\x89PNG-bb").decode(), u2)

    def test_mime_follows_the_bytes_not_the_extension(self):
        """生图 provider 把 JPEG 字节写进 `.png` 是常见形态（本仓 seedream 产出的
        分镜图全是这样），而接口按 data URL 声明的 mime 解码——按扩展名标就是
        逐张发错类型。同款嗅探在参考音那条路上已经存在。"""
        from kinema.providers import _util
        with tempfile.TemporaryDirectory() as d:
            jpg_in_png = Path(d) / "shot_1.png"
            jpg_in_png.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
            self.assertTrue(_util.file_to_data_url(jpg_in_png)
                            .startswith("data:image/jpeg;base64,"))
            real_png = Path(d) / "sheet.png"
            real_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
            self.assertTrue(_util.file_to_data_url(real_png)
                            .startswith("data:image/png;base64,"))


class TestSeedreamPixelCeiling(unittest.TestCase):
    """`--hd`：在像素上限内放大出图尺寸，**宽高比逐位不变**。

    比例漂移 1 像素就会被下游素材体检判成宽高比不符，而那条判据的后果是
    「cover 取景裁掉主体」——所以按最简整数比放大，不乘浮点系数。"""

    def _prov(self, **conn):
        from kinema.providers.image.seedream import SeedreamProvider
        return SeedreamProvider(conn, None)

    def test_default_sends_the_canvas_size_unchanged(self):
        p = self._prov()
        self.assertEqual(p._fit_pixels(1920, 1080), (1920, 1080))

    def test_scaling_keeps_the_aspect_ratio_exact(self):
        p = self._prov(max_pixels=4624220)
        for w, h in ((1920, 1080), (1080, 1920), (1080, 1080)):
            nw, nh = p._fit_pixels(w, h)
            self.assertLessEqual(nw * nh, 4624220, f"{w}x{h} 超了总像素上限")
            self.assertGreater(nw * nh, w * h, f"{w}x{h} 没放大")
            self.assertEqual(nw * h, nh * w, f"{w}x{h} → {nw}x{nh} 比例漂移了")

    def test_a_canvas_already_over_the_cap_is_left_alone(self):
        """缩小画面不是这里的职责——超限交给服务端按它自己的约束裁决。"""
        p = self._prov(max_pixels=921600)
        self.assertEqual(p._fit_pixels(1920, 1080), (1920, 1080))


class TestPollTask(unittest.TestCase):
    """`_util.poll_task`：四家异步轮询共用的骨架。三条纪律各守一条——
    瞬态容忍（单次抖动不弃掉服务端照常计费的任务）/ 业务错误直抛 /
    monotonic 截止超时；外加「首查先于任何 sleep」（短任务不吃整个 interval）。"""

    def test_transient_errors_tolerated_then_fatal(self):
        from kinema.providers._util import poll_task
        calls = {"n": 0}

        def check():
            calls["n"] += 1
            raise ConnectionError("网络抖动")

        with self.assertRaises(ProviderError) as cm:
            poll_task(check, what="X", task_id="t1", timeout=60, interval=0)
        self.assertEqual(calls["n"], 5, "连接类异常应容忍到连续第 5 次才判失败")
        self.assertIn("t1", str(cm.exception))

    def test_provider_error_bypasses_tolerance(self):
        from kinema.providers._util import poll_task
        calls = {"n": 0}

        def check():
            calls["n"] += 1
            raise ProviderError("任务failed")

        with self.assertRaises(ProviderError):
            poll_task(check, what="X", task_id="t1", timeout=60, interval=0)
        self.assertEqual(calls["n"], 1, "业务性失败不得进容忍带重试")

    def test_first_check_precedes_any_sleep(self):
        import time
        from kinema.providers._util import poll_task
        t0 = time.monotonic()
        out = poll_task(lambda: "url", what="X", task_id="t1",
                        timeout=60, interval=30)
        self.assertEqual(out, "url")
        self.assertLess(time.monotonic() - t0, 1)

    def test_timeout_names_the_task(self):
        from kinema.providers._util import poll_task
        with self.assertRaises(ProviderError) as cm:
            poll_task(lambda: None, what="X", task_id="t1",
                      timeout=0.05, interval=0.01)
        self.assertIn("超时", str(cm.exception))
        self.assertIn("t1", str(cm.exception))

    def test_retry_status_rides_the_tolerance_lane(self):
        """轮询期 429/5xx 是瞬态，**不得终止轮询**——任务在服务端照常渲染，
        单次 503 弃单=已计费的生成变无效支出（视频侧 retries=0 弃单即整批停派，
        图侧自动重跑则为同一张图重复计费）。裁决单点 raise_for_poll：
        _RETRY_STATUS 走容忍带，业务 4xx 才终止。"""
        from kinema.providers._util import poll_task, raise_for_poll

        class _R:
            def __init__(self, code):
                self.status_code = code
                self.text = "busy"
        seq = [503, 429, 200]
        calls = {"n": 0}

        def check():
            code = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            raise_for_poll(_R(code), what="X", task_id="t1")
            return "url"

        self.assertEqual(poll_task(check, what="X", task_id="t1",
                                   timeout=60, interval=0), "url")
        self.assertEqual(calls["n"], 3, "503/429 之后必须继续轮询拿到产物")

        class _R404:
            status_code = 404
            text = "not found"
        with self.assertRaises(ProviderError):
            raise_for_poll(_R404(), what="X", task_id="t1")

    def test_no_adapter_rolls_its_own_poll_status_gate(self):
        """源级：四家轮询函数不许再自写 `status_code >= 400` 直抛 ProviderError
        ——那会把 _RETRY_STATUS 定义的瞬态一并终止（提交侧的 >=400 直抛是
        正确行为，只查轮询段）。"""
        import kinema
        root = Path(kinema.__file__).parent / "providers"
        anchors = {"video/seedance.py": "def _poll", "video/veo.py": "def _poll",
                   "video/minimax.py": "def _await", "image/wan.py": "def _poll"}
        for rel, anchor in anchors.items():
            src = (root / rel).read_text(encoding="utf-8")
            seg = src.split(anchor, 1)[1]
            self.assertNotIn("status_code >= 400", seg, rel)
            self.assertIn("raise_for_poll(", seg, rel)

    def test_every_async_provider_polls_through_the_skeleton(self):
        """四家轮询必须走同一骨架——「同一条纪律各抄一份」必然分头漂移：
        计时口径各说各话（只累加 sleep 不含 HTTP 往返）、容忍带缺失（单次抖动即弃单）。"""
        import kinema
        root = Path(kinema.__file__).parent / "providers"
        for rel in ("video/seedance.py", "video/veo.py", "video/minimax.py",
                    "image/wan.py"):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertIn("poll_task(", src, f"{rel} 未接入轮询骨架")
            self.assertNotIn("time.sleep", src, f"{rel} 不该再自带轮询等待")


class TestDoubaoSegments(unittest.TestCase):
    def test_timestamps_are_read_as_milliseconds(self):
        """接口的 start_time/end_time 恒为毫秒整数（字段说明「距音频开始的毫秒
        偏移值」）。按响应内容猜单位的话，整段短于一秒时 900ms 会被读成 900 秒。"""
        from kinema.providers.tts.doubao import DoubaoTTSProvider
        p = DoubaoTTSProvider({}, None)
        j = {"subtitle": {"sentences": [
            {"text": "短句", "begin_time": 0, "end_time": 900},
            {"text": "长句", "begin_time": 900, "end_time": 5200}]}}
        segs = p._segments(j, "短句长句", "unused.wav")
        self.assertEqual([segs[0]["end"], segs[1]["end"]], [0.9, 5.2])
        short = p._segments({"subtitle": {"sentences": [
            {"text": "嗯", "begin_time": 0, "end_time": 620}]}}, "嗯", "unused.wav")
        self.assertEqual(short[0]["end"], 0.62, "全响应不足一秒时同样按毫秒读")


class TestFacePolicyError(unittest.TestCase):
    """输入图被判疑似真人：该类失败重跑必然同样被拒，收尾须给处置方案。

    判据在模型侧的输入分类器上，改参数绕不开（`docs/kinema/seedance-face-policy.md`）。
    与通用失败共用「重跑会跳过已成功的」这条提示，即引导用户重试必败的请求。
    """

    @staticmethod
    def _resp(payload, status=400):
        class _R:
            status_code = status
            text = json.dumps(payload, ensure_ascii=False)

            @staticmethod
            def json():
                return payload
        return _R()

    def test_error_carries_the_structured_code(self):
        from kinema.providers.video import seedance
        err = seedance._create_error(self._resp({"error": {
            "code": "InputImageSensitiveContentDetected.PrivacyInformation",
            "message": "input image 'content[1]' may contain real person"}}), [])
        self.assertIsInstance(err, ProviderError)
        self.assertTrue(err.code.startswith(seedance.FACE_POLICY_CODE))

    def test_rejected_reference_is_named(self):
        """官方只报 content[N] 下标，而首帧、设定图与参考音同处一个数组，
        不翻译成身份则无从定位是哪张图被拒。"""
        from kinema.providers.video import seedance
        content = [{"type": "text"},
                   {"type": "image_url", "role": "first_frame"},
                   {"type": "image_url", "role": "reference_image"}]
        err = seedance._create_error(self._resp({"error": {
            "code": "InputImageSensitiveContentDetected.PrivacyInformation",
            "message": "input image 'content[1]' 'content[2]' may contain real person"}}),
            content)
        self.assertIn("first_frame", str(err))
        self.assertIn("reference_image", str(err))

    def test_other_4xx_keeps_the_plain_shape(self):
        from kinema.providers.video import seedance
        err = seedance._create_error(self._resp({"error": {
            "code": "InvalidParameter", "message": "bad ratio"}}), [])
        self.assertEqual(err.code, "InvalidParameter")
        self.assertIn("400", str(err))

    def test_summary_swaps_retry_advice_for_a_remedy(self):
        from kinema import cli
        from kinema.parallel import Done
        face = Done("s1", False, error=ProviderError(
            "x", code="InputImageSensitiveContentDetected.PrivacyInformation"))
        advice = cli._retry_advice([face])
        # 「重跑会自动跳过已成功的」是通用失败的口径，该类失败不得沿用
        self.assertNotIn("会自动跳过它们", advice)
        self.assertIn("处置路线", advice)
        self.assertIn("seedance-face-policy.md", advice)

    def test_generic_failure_still_tells_you_to_rerun(self):
        from kinema import cli
        from kinema.parallel import Done
        advice = cli._retry_advice([Done("s1", False, error=ProviderError("timeout"))])
        self.assertIn("会自动跳过它们", advice)

    def test_mixed_batch_gets_both(self):
        from kinema import cli
        from kinema.parallel import Done
        advice = cli._retry_advice([
            Done("s1", False, error=ProviderError(
                "x", code="InputImageSensitiveContentDetected.PrivacyInformation")),
            Done("s2", False, error=ProviderError("timeout"))])
        self.assertIn("处置路线", advice)
        self.assertIn("会自动跳过它们", advice)


if __name__ == "__main__":
    unittest.main()


class TestSeedancePollErrorCode(unittest.TestCase):
    """轮询期失败的错误码结构化上抛：输出侧审核拒收（任务 failed）要能按码分流，
    靠错误文案子串匹配的口径迟早随厂商措辞漂移。"""

    def _poll_failed(self, error_body):
        from kinema.providers.video import seedance as m
        prov = m.SeedanceProvider({"price_per_second": 1.0}, _Store())

        class _R:
            status_code = 200

            @staticmethod
            def json():
                return {"status": "failed", "error": error_body}
        orig = m.request_with_retry
        m.request_with_retry = lambda *a, **k: _R()
        try:
            prov._poll("t1", "key")
        finally:
            m.request_with_retry = orig

    def test_output_policy_code_survives_to_the_exception(self):
        from kinema.errors import ProviderError
        from kinema.providers.video.seedance import OUTPUT_POLICY_CODE
        with self.assertRaises(ProviderError) as ctx:
            self._poll_failed({"code": "OutputVideoSensitiveContentDetected.PolicyViolation",
                               "message": "output blocked"})
        self.assertTrue(str(ctx.exception.code).startswith(OUTPUT_POLICY_CODE))
        self.assertIn("output blocked", str(ctx.exception))

    def test_unstructured_error_body_keeps_code_none(self):
        from kinema.errors import ProviderError
        with self.assertRaises(ProviderError) as ctx:
            self._poll_failed("internal error")
        self.assertIsNone(ctx.exception.code)
