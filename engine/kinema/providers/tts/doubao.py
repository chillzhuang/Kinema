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

"""豆包音频生成 provider（字节 · 火山引擎语音技术，seed-audio-1.0，非流式 HTTP）。

接口：POST https://openspeech.bytedance.com/api/v3/tts/create ，model=seed-audio-1.0。
一次请求 → 单响应，返回 base64 音频 + 字幕(sentences 时间戳，enable_subtitle=true)，
支持 speaker(2.0 大模型音色)、参考音频/图。最长 120s。比流式接口更稳、更简单。

鉴权：新版控制台单头 Header X-Api-Key（环境变量 ARK_TTS_API_KEY）。
⚠ TTS 用独立语音凭证，不是 ARK_API_KEY！控制台 console.volcengine.com/speech
多角色：各镜 shots[].voice 设不同 speaker。
本适配器是**「定制生成」的落点**：text_prompt 吃自然语言音频剧本，references 吃参考
音频/图片，一次可出「人声 + BGM + 音效」已混好的整段音轨。

**四种生成模式互斥**（接口约束，本适配器在发请求前自检，不让服务端来告诉你写错了）：
纯文本（只给 text_prompt，按描述凭空造声音）· 固定音色（speaker）· 参考音频
（audio_data / audio_url / references 里的音频）· 参考图片（image_data / image_url）。
图片参考不能与任何音频参考或 speaker 同用。

⚠ subtitle 的时间字段名/单位上线前以官方文档核对（本实现防御式解析，管线时长以实际音频为准）。
"""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from ..base import TTSProvider, TTSResult
from .._util import download, request_with_retry
from ...errors import ProviderError
from ...ffmpeg import probe_duration


def _finite(x) -> float:
    """时间戳/时长的容错取值：字段缺失或非数字一律当 0，不让一条脏数据掀翻整批解析。"""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_url(src: str) -> bool:
    return str(src).startswith(("http://", "https://"))


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def _stamp(d: dict) -> dict:
    """一条字幕单元（句或词）→ {text, start, end}，秒为单位。

    接口的 `start_time`/`end_time` 恒为**毫秒整数**（音频生成 HTTP 的字段说明：
    「距音频开始的毫秒偏移值」），直接除 1000。按响应内容猜单位会在整段短于一秒的
    响应上把 900ms 读成 900 秒。"""
    start = _finite(d.get("begin_time", d.get("start_time", d.get("start"))))
    end = _finite(d.get("end_time", d.get("end")))
    return {"text": d.get("text", ""),
            "start": round(start / 1000.0, 3), "end": round(end / 1000.0, 3)}


class DoubaoTTSProvider(TTSProvider):
    name = "doubao"
    MAX_REF_AUDIO = 3          # 接口上限：最多三条参考音频，对应 @音频1..3
    supports_voice_anchor = True   # 生成式模型逐句漂移，靠 ref_audio 参考音锁音色

    def __init__(self, conn: dict, store):
        self.store = store
        # 与全系 provider 统一：配置只给 base_url，路径由适配器拼接
        self.base = conn.get("base_url", "https://openspeech.bytedance.com/api/v3").rstrip("/")
        self.model = conn.get("model", "seed-audio-1.0")
        self.default_voice = conn.get("voice", "zh_female_vv_uranus_bigtts")
        self.audio_format = conn.get("format", "mp3")
        self.sample_rate = int(conn.get("sample_rate", 24000))
        self.api_key_env = conn.get("api_key_env", "ARK_TTS_API_KEY")
        # 计费单价，0=未配置→不计入成本台账。官方以 original_duration（模型输出原始
        # 时长）计费，故秒价优先；字符价保留给按字符结算的存量配置
        self.price_per_second = float(conn.get("price_per_second", 0.0))
        self.price_per_kchar = float(conn.get("price_per_kchar", 0.0))
        # 隐式水印元数据（配置里给了才发；显式水印会在音频结尾加节奏标识，默认关）
        self.watermark = conn.get("watermark") or {}

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Api-Key": self.store.secret(self.api_key_env)}

    def _reference_body(self, *, voice, ref_audio, ref_audio_url,
                        ref_image, ref_image_url, ref_audios, prompt_only) -> dict:
        """按四种生成模式装配参考资源，并在发请求前把互斥冲突拦下来。

        **参考资源一律进 `references` 数组**：接口的请求体顶层只有 model /
        text_prompt / references / audio_config / watermark 五项，
        `speaker`、`audio_data`/`audio_url`、`image_data`/`image_url` 全部是
        数组条目内的字段（官方参数表的父子结构与 cURL/Python/Go 三份样例一致）。
        条目平铺到顶层会退化成「不传参考资源」那一档——服务端拿不到音色，
        生成式模型逐句各造一把声音，而这一路没有任何报错。

        数组顺序即 `text_prompt` 里 `@音频N` 的编号（官方：「参考音频的上传顺序
        须与 text_prompt 中 @音频N 的编号顺序严格对应」），故单条也不折叠。"""
        audio_refs = [x for x in (ref_audio_url and [ref_audio_url] or [])
                      + list(ref_audios or []) if x]
        if ref_audio:
            audio_refs.insert(0, ref_audio)
        has_image = bool(ref_image or ref_image_url)
        if has_image and (audio_refs or voice):
            raise ProviderError("图片参考不能与音频参考或 speaker 同用（接口互斥约束）")
        if len(audio_refs) > self.MAX_REF_AUDIO:
            raise ProviderError(
                f"参考音频最多 {self.MAX_REF_AUDIO} 条，收到 {len(audio_refs)} 条")
        if has_image:
            item = ({"image_data": _b64(ref_image)} if ref_image
                    else {"image_url": ref_image_url})
            return {"references": [item]}
        if audio_refs:
            return {"references": [self._ref_item(x) for x in audio_refs]}
        if prompt_only:
            return {}          # 纯文本生成：按 text_prompt 的描述凭空造声音
        return {"references": [{"speaker": voice or self.default_voice}]}

    @staticmethod
    def _ref_item(src: str) -> dict:
        """`references` 数组的一条音频参考。条目字段只有官方列出的那五个，
        多发一个未登记的键要么被忽略、要么触发参数校验失败。"""
        return {"audio_url": src} if _is_url(src) else {"audio_data": _b64(src)}

    def synthesize(self, text, out_path, *, voice=None, ref_audio=None, ref_audio_url=None,
                   ref_image=None, ref_image_url=None, ref_audios=None, prompt_only=False,
                   speech_rate=None, pitch_rate=None, loudness_rate=None,
                   **kwargs) -> TTSResult:
        """合成一段。`text` 即接口的 `text_prompt`——它既可以是待合成的台词，
        也可以是一整段描述声线/音乐/音效的音频剧本。

        参考资源四选一（互斥，见 `_reference_body`）：
          · ref_audio / ref_audio_url / ref_audios（多条）——**音色锚定**：同一角色全程
            共用同一段参考音频，是这个生成式模型不漂移的唯一办法。
          · ref_image / ref_image_url——按图片气质生成声音。
          · voice（豆包 2.0 音色 ID）→ speaker。
          · prompt_only=True——不发 speaker，完全按 `text` 里的描述造声音，
            「定制生成」用它产出那条锚定参考音。
        """
        audio_config = {"format": self.audio_format, "sample_rate": self.sample_rate,
                        "enable_subtitle": True}
        for key, val in (("speech_rate", speech_rate), ("pitch_rate", pitch_rate),
                         ("loudness_rate", loudness_rate)):
            if val is not None:
                audio_config[key] = int(val)
        body = {"model": self.model, "text_prompt": text, "audio_config": audio_config}
        body.update(self._reference_body(
            voice=voice, ref_audio=ref_audio, ref_audio_url=ref_audio_url,
            ref_image=ref_image, ref_image_url=ref_image_url,
            ref_audios=ref_audios, prompt_only=prompt_only))
        if self.watermark:
            body["watermark"] = dict(self.watermark)
        try:
            resp = request_with_retry("POST", f"{self.base}/tts/create", json=body,
                                      headers=self._headers(), timeout=120, desc="doubao tts")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"豆包音频生成请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"豆包音频生成 {resp.status_code}: {resp.text[:500]}")

        j = resp.json()
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if j.get("audio"):
            Path(out_path).write_bytes(base64.b64decode(j["audio"]))
        elif j.get("url"):
            download(j["url"], out_path)
        else:
            raise ProviderError(f"豆包音频生成未返回音频: {json.dumps(j, ensure_ascii=False)[:500]}")

        return TTSResult(audio_path=str(out_path),
                         segments=self._segments(j, text, out_path),
                         cost=self._cost(j, text, out_path))

    def _cost(self, j: dict, text: str, out_path: str) -> float:
        """本次费用。官方以 `original_duration`（模型输出原始时长，上限 120s）计费，
        故秒价优先；响应缺这个字段时退回实测音频时长，两者都拿不到才按字符价。"""
        if self.price_per_second > 0:
            secs = _finite(j.get("original_duration")) or probe_duration(out_path)
            return round(secs * self.price_per_second, 6)
        if self.price_per_kchar > 0:
            return round(len(text) / 1000.0 * self.price_per_kchar, 6)
        return 0.0

    def _segments(self, j: dict, text: str, out_path: str) -> list[dict]:
        """从 subtitle.sentences 解析句级时间戳，逐句再带上 words 词级时间戳；
        失败则回退单段（时长取实际音频）。词级只是附加键，下游按 text/start/end
        读的路径不受影响。"""
        try:
            sentences = ((j.get("subtitle") or {}).get("sentences")) or []
            segs = []
            for s in sentences:
                seg = _stamp(s)
                words = [_stamp(w) for w in (s.get("words") or [])]
                if words:
                    seg["words"] = words
                segs.append(seg)
            if segs:
                return segs
        except Exception:  # noqa: BLE001
            pass
        return [{"text": text, "start": 0.0, "end": probe_duration(out_path)}]
