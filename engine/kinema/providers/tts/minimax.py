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

"""MiniMax TTS provider（海螺 · speech-2.x）。

中文口播备选：中英混读平滑、低延迟、可开启字幕时间戳。

连接来自 config/models.yaml 的 providers.minimax；密钥 MINIMAX_API_KEY + MINIMAX_GROUP_ID。
国内域名 api.minimaxi.com / 国际 api.minimax.io（两区 key 不互通）。
⚠ 上线前核对：t2a_v2 参数、音频返回是 hex/url、subtitle 结构。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..base import TTSProvider, TTSResult
from .._util import download, request_with_retry
from ...errors import ProviderError
from ...ffmpeg import probe_duration


# 官方 `voice_setting.emotion` 枚举九档。没有 neutral——最接近的是 calm；也没有强度
# 参数，火山那套 1~5 的 emotion_scale 在这里没有对应字段。
EMOTIONS = frozenset({"happy", "sad", "angry", "fearful", "disgusted",
                      "surprised", "calm", "fluent", "whisper"})
# 缺省音色**按站点分派**：两站的系统音色 ID 是两套互不相通的命名——国内站是
# male-qn-qingse 一类拼音短名，国际站是 Chinese (Mandarin)_Xxx 一类长名，拿国内
# ID 打国际站（或反之）都是一次必被拒的请求。连接段显式配了 voice 则以它为准，
# 且必须取自当前 base_url 所在站的系统音色表。
_VOICE_CN = "male-qn-qingse"                        # 国内站系统音色（青涩青年）
_VOICE_INTL = "Chinese (Mandarin)_Male_Announcer"   # 国际站系统音色（播音男声）
# 其中两档**按模型门控**：官方明写 fluent / whisper 仅 speech-2.6-turbo 与
# speech-2.6-hd 生效，2.8 两档不支持 whisper。当成合法值发出去就是一次被拒的请求，
# 所以在本地降级并说明，而不是让服务端来告诉用户。
EMOTIONS_2_6_ONLY = frozenset({"fluent", "whisper"})


def _speed(rate) -> float:
    """项目顶层 `speech_rate` → 官方 `voice_setting.speed`。

    两边刻度不同：工程内沿用火山口径（-50~100，0=原速），官方要的是 0.5~2 的倍率。
    必须读引擎真正下发的键：读一个全流程从不下发的键，语速设置在 minimax 路径上
    就恒失效；而把火山刻度原样透传又会撞死在合法区间上（-50 直接 400）。
    """
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return 1.0
    return max(0.5, min(2.0, 1.0 + r / 100.0))


class MiniMaxTTSProvider(TTSProvider):
    name = "minimax"

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://api.minimax.io/v1").rstrip("/")
        # 兜底常量与 config/models.yaml、models.py 内嵌表**三处同源**（2.8 是当前档，
        # 2.6/02 官方已归 legacy；且语气词标签只有 2.8 两档支持）
        self.model = conn.get("model", "speech-2.8-hd")
        self.default_voice = conn.get("voice") or \
            (_VOICE_CN if "minimaxi.com" in self.base else _VOICE_INTL)
        self.api_key_env = conn.get("api_key_env", "MINIMAX_API_KEY")
        self.group_id_env = conn.get("group_id_env", "MINIMAX_GROUP_ID")
        # 计费单价（CNY/千字符）。0=未配置→不计入成本台账
        self.price_per_kchar = float(conn.get("price_per_kchar", 0.0))

    def synthesize(self, text, out_path, *, voice=None, **kwargs) -> TTSResult:
        key = self.store.secret(self.api_key_env)
        # GroupId 是旧域名接口的遗留参数：现行官方文档**两站都没有**这个查询参数
        # （连声音复刻的 files/upload 也只要 Bearer）。做成必填会让「只有 API Key」的
        # 用户连试都试不了，故配了就带、没配就不带——仅为兼容历史网关，
        # 官方口径以 Bearer 单一鉴权为准。
        group_id = self.store.secret(self.group_id_env, required=False)
        # 跨厂防呆：项目/voices.yaml 里锁定的火山系音色（uranus/ICL_/S_ 复刻）对
        # MiniMax 无意义——直接送会 4xx。降级到本厂默认音色并大声提示重新选角，
        # 而不是让整条 tts 阶段崩在一个音色 ID 上（换 TTS 厂商的自动适配垫层）。
        vid = voice or self.default_voice
        if vid and ("uranus" in vid or vid.startswith(("ICL_", "S_"))):
            print(f"  ⚠ 音色 '{vid}' 是火山引擎体系（uranus/ICL/复刻），MiniMax 不识别——"
                  f"已降级为本厂默认音色 '{self.default_voice}'。请为该角色重新试音选角"
                  "（voice audition），或在 voices.yaml 配 MiniMax 音色。")
            vid = self.default_voice
        voice_setting = {"voice_id": vid, "speed": _speed(kwargs.get("speech_rate")),
                         "vol": 1.0, "pitch": 0}
        # 引擎每镜都在下发 emotion（voicecast 不门控），落在 `**kwargs` 里不取就被
        # 静默吞掉——走 minimax 的画风逐镜情绪整条失效且零提示。**不支持强度**——
        # 火山那套 1~5 的 emotion_scale 在这里没有对应字段，映射时丢弃而不是硬发。
        # 情绪：官方 `voice_setting.emotion`，其中 fluent/whisper 按模型门控（见常量）。
        emo = str(kwargs.get("emotion") or "").strip().lower()
        if emo in EMOTIONS_2_6_ONLY and "2.6" not in self.model:
            print(f"  ⚠ 情绪 '{emo}' 仅 speech-2.6-hd/turbo 支持，当前 {self.model} 不支持，"
                  "本镜按中性合成（要用请把 model 换成 2.6 档）")
        elif emo in EMOTIONS:
            voice_setting["emotion"] = emo
        elif emo:
            print(f"  ⚠ MiniMax 不支持情绪 '{emo}'（可选：{'、'.join(sorted(EMOTIONS))}），本镜按中性合成")
        body = {
            "model": self.model, "text": text, "stream": False,
            "subtitle_enable": True, "subtitle_type": "sentence",
            # 显式写死返回形态：官方缺省是 hex，而下面按 hex 解码——缺省值属于
            # 可被平台调整的项，隐式依赖它等于把"音频是 hex 还是 URL"交给别人决定
            "output_format": "hex",
            "voice_setting": voice_setting,
            "audio_setting": {"sample_rate": 32000, "format": "mp3", "channel": 1},
        }
        url = f"{self.base}/t2a_v2" + (f"?GroupId={group_id}" if group_id else "")
        try:
            resp = request_with_retry(
                "POST", url, json=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"}, timeout=120,
                desc="minimax tts")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"MiniMax TTS 请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"MiniMax TTS {resp.status_code}: {resp.text[:500]}")

        j = resp.json()
        # 业务错误可以是 HTTP 200 + base_resp.status_code≠0 + data 为 null
        # （鉴权/余额/参数非法/内容风控都走这条）。只看 HTTP 码的话，这类失败会被
        # 报成含义模糊的「未返回音频」，真正的原因混在整段 json 里
        br = j.get("base_resp") or {}
        if br.get("status_code") not in (0, None):
            raise ProviderError(
                f"MiniMax TTS 业务错误 {br.get('status_code')}: {br.get('status_msg')}")
        audio_hex = (j.get("data") or {}).get("audio")
        if not audio_hex:
            raise ProviderError(f"MiniMax TTS 未返回音频: {json.dumps(j)[:500]}")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(bytes.fromhex(audio_hex))
        # 计费字符数**取平台回吐的权威值**：官方口径「1 个汉字算 2 个字符」，
        # 按 len(text) 估中文旁白会系统性低估约一半
        used = (j.get("extra_info") or {}).get("usage_characters")
        chars = float(used) if isinstance(used, (int, float)) and used > 0 else len(text)
        cost = round(chars / 1000.0 * self.price_per_kchar, 6) \
            if self.price_per_kchar > 0 else 0.0
        return TTSResult(audio_path=str(out_path),
                         segments=self._parse_segments(j, text, out_path), cost=cost)

    def _parse_segments(self, j, text, out_path):
        try:
            # 字幕下载链接在 `data` 下，不在 `extra_info`（后者是 audio_length/
            # usage_characters 那九项）。取错父对象 + 下面的裸 except，结果是
            # subtitle_enable 白开、句级时间戳恒取不到，且一行告警都没有
            sub_url = (j.get("data") or {}).get("subtitle_file")
            if sub_url:
                tmp = Path(out_path).with_suffix(".subtitle.json")
                download(sub_url, tmp)
                items = json.loads(tmp.read_text(encoding="utf-8"))
                segs = [{"text": it.get("text", ""),
                         "start": float(it.get("time_begin", 0)) / 1000.0,
                         "end": float(it.get("time_end", 0)) / 1000.0}
                        for it in items if it.get("text")]
                if segs:
                    return segs
        except Exception as e:  # noqa: BLE001
            # 拿不到句级时间戳不该让整条配音失败（字幕仍可按整句铺），但必须说一声——
            # 静默回落会让「字幕时间轴对不齐」变成一个查不出来的问题
            print(f"  ⚠ MiniMax 字幕时间戳解析失败（{e}），本句按整段时间轴处理")
        return [{"text": text, "start": 0.0, "end": probe_duration(out_path)}]
