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

"""豆包「语音合成大模型」provider（标准 TTS，seed-tts-2.0，HTTP SSE，固定音色）。

与 seed-audio-1.0（音频生成，生成式，逐句音色会漂移）不同：
seed-tts-2.0 的 speaker 是**确定性固定音色**（"豆包语音合成模型 2.0" 的 uranus 音色，
番茄小说/剪映同款生产级配音），同一 speaker 全程同一把声音，天然不漂移——是角色配音的正解。

接口：POST https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse （HTTP SSE 单向流式）
鉴权：新版控制台单头 X-Api-Key（ARK_TTS_API_KEY）。
必带：X-Api-Resource-Id = seed-tts-2.0（决定用 2.0 音色 + 字符版计费，不占并发额度）。
响应：SSE 事件流；event=352(TTSResponse) 的 data 为 JSON，其中 data.data 是 base64 音频分片，
      按序拼接成完整音频；event=152(SessionFinished) 表示结束。
"""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from ..base import TTSProvider, TTSResult
from ...errors import ProviderError
from ...ffmpeg import probe_duration


class SeedTTSProvider(TTSProvider):
    name = "seedtts"

    def __init__(self, conn: dict, store):
        self.store = store
        # 与全系 provider 统一：配置只给 base_url，路径属调用协议、由适配器拼接
        self.base = conn.get("base_url", "https://openspeech.bytedance.com/api/v3").rstrip("/")
        self.resource_id = conn.get("resource_id", "seed-tts-2.0")
        self.default_voice = conn.get("voice", "zh_female_vv_uranus_bigtts")
        self.audio_format = conn.get("format", "mp3")
        self.sample_rate = int(conn.get("sample_rate", 24000))
        self.api_key_env = conn.get("api_key_env", "ARK_TTS_API_KEY")
        # 字符版计费单价（CNY/千字符）。0=未配置→不计入成本台账（区别于"肯定性零"）
        self.price_per_kchar = float(conn.get("price_per_kchar", 0.0))

    def _headers(self) -> dict:
        # resource_id 定在配置（seed-tts-2.0）——它决定音色档位与计费口径，不按次覆盖
        h = {"Content-Type": "application/json",
             "X-Api-Resource-Id": self.resource_id,
             "X-Api-Request-Id": str(uuid.uuid4()), "X-Api-Connect-Id": str(uuid.uuid4())}
        h["X-Api-Key"] = self.store.secret(self.api_key_env)
        return h

    def synthesize(self, text, out_path, *, voice=None, speech_rate=None, pitch_rate=None,
                   loudness_rate=None, emotion=None, emotion_scale=None,
                   instruction=None, **kwargs) -> TTSResult:
        audio_params = {"format": self.audio_format, "sample_rate": self.sample_rate}
        if speech_rate is not None:
            audio_params["speech_rate"] = int(speech_rate)
        if loudness_rate is not None:
            audio_params["loudness_rate"] = int(loudness_rate)
        if emotion:   # 多情感音色的情绪档 + 情绪强度（1~5，仅部分音色支持）
            audio_params["emotion"] = emotion
            if emotion_scale is not None:
                audio_params["emotion_scale"] = int(emotion_scale)
        req_params = {"text": text, "speaker": voice or self.default_voice,
                      "audio_params": audio_params}
        additions: dict = {}
        if pitch_rate is not None:
            additions["post_process"] = {"pitch": int(pitch_rate)}
        if instruction:   # 语音指令（2.0 系）：如「用哽咽的语气说」——对话式表现力控制
            # ⚠ 官方标准版（模版生成走的这一档）会**静默过滤**它：既不生效也不报错。
            # 上游 stage_tts 因此根本不下发 instruction，这里只保留透传能力
            additions["context_texts"] = [str(instruction)]
        if additions:     # additions 按接口要求传 json 字符串
            req_params["additions"] = json.dumps(additions, ensure_ascii=False)
        body = {"user": {"uid": "kinema"}, "req_params": req_params}
        from .._util import request_with_retry
        try:
            resp = request_with_retry("POST", f"{self.base}/tts/unidirectional/sse",
                                      json=body,
                                      headers=self._headers(), timeout=120,
                                      desc="seedtts")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"标准语音合成请求失败: {e}") from e
        if resp.status_code >= 400:
            # X-Tt-Logid 是服务端排障锚点（文档建议获取打印），错误信息随行
            raise ProviderError(f"标准语音合成 {resp.status_code}: {resp.text[:400]}"
                                f"（logid: {resp.headers.get('X-Tt-Logid', '-')}）")

        audio = self._parse_sse(resp.text,
                                logid=resp.headers.get("X-Tt-Logid", "-"))
        if not audio:
            raise ProviderError(f"标准语音合成未返回音频: {resp.text[:300]}")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(audio)
        cost = round(len(text) / 1000.0 * self.price_per_kchar, 6) \
            if self.price_per_kchar > 0 else 0.0
        return TTSResult(audio_path=str(out_path),
                         segments=[{"text": text, "start": 0.0,
                                    "end": probe_duration(out_path)}], cost=cost)

    @staticmethod
    def _parse_sse(text: str, logid: str = "-") -> bytes:
        """从 SSE 文本中取出 event=352 的 base64 音频分片并按序拼接。

        流中出现任何错误事件都视为整句失败——即使之前已收到部分分片。
        半句截断的音频若被当成功写盘，dur 会回填成截断时长，
        下游时间轴/字幕全部对齐到错误值且无人知晓。"""
        audio = bytearray()
        err = None
        for blk in text.split("\n\n"):
            ev = data = None
            for line in blk.split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
            if not data:
                continue
            try:
                j = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if ev == "352" and j.get("data"):
                try:
                    audio += base64.b64decode(j["data"])
                except Exception as e:  # noqa: BLE001
                    # 分片解码失败=音频已缺块，视同错误事件——静默丢弃这一片，
                    # 拼出的正是本函数要防的那种半句截断音频
                    err = err or {"code": "b64decode",
                                  "message": f"音频分片 base64 解码失败: {e}"}
            elif isinstance(j, dict) and j.get("code") not in (None, 0, 20000000):
                err = j
        if err:
            raise ProviderError(
                f"标准语音合成 code={err.get('code')}: {err.get('message')}"
                + (f"（已收到 {len(audio)} 字节分片，按失败处理防半句截断）" if audio else "")
                + f"（logid: {logid}）")
        return bytes(audio)
