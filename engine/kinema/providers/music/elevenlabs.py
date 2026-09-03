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

"""ElevenLabs Music provider（背景音乐）。

推荐默认 BGM provider：有官方 API + 源头授权（唱片公司/出版方合作训练，商用清洁），
按 prompt 同步生成整段配乐，响应即裸音频字节流。

要点（官方文档核对，elevenlabs.io/docs/api-reference/music/compose）：
· POST {base}/music，头 xi-api-key，JSON body，200 返回音频文件字节（非 JSON）；
· model_id 显式钉死：官方过渡期缺省仍是 music_v1，不发就拿不到 v2 音质，
  且缺省随平台切换会静默改变产物形态；
· output_format 是**查询参数**，缺省 auto 会随模型切采样率（v2 → 48kHz）——
  固定传值，落盘产物规格不交给缺省决定；
· music_length_ms 合法域 [3000, 600000]，出界是建请求 400——本地钳制并告警，
  产物长短差异由 compose 侧循环/裁剪吸收；
· force_instrumental 恒开：成片里已有旁白与对白，带人声的配乐会和旁白抢同一条
  频段——混音链的让路 EQ 与 sidechain 闪避按「器乐床 + 人声主轨」标定
  （与 MiniMax 音乐通道的 is_instrumental 同一条纪律）；
· 计费按分钟（API 口径 $0.15/分钟），价格经 price_per_min 连接段配置。

连接来自 config/models.yaml 的 providers.elevenlabs；密钥 ELEVENLABS_API_KEY。
"""
from __future__ import annotations

from pathlib import Path

from ..base import MusicProvider, MusicResult
from .._util import request_with_retry
from ...errors import ProviderError

# music_length_ms 官方合法域（毫秒）
MUSIC_MS_MIN, MUSIC_MS_MAX = 3000, 600000
# duration_seconds 官方合法域（秒）
SFX_SEC_MIN, SFX_SEC_MAX = 0.5, 30.0


class ElevenLabsMusicProvider(MusicProvider):
    name = "elevenlabs"

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://api.elevenlabs.io/v1").rstrip("/")
        self.api_key_env = conn.get("api_key_env", "ELEVENLABS_API_KEY")
        self.model = conn.get("model", "music_v2")
        self.output_format = conn.get("output_format", "mp3_44100_128")
        self.price_per_min = float(conn.get("price_per_min", 0.0))
        self.price_per_sfx = float(conn.get("price_per_sfx", 0.0))

    def generate(self, prompt, out_path, *, duration=60.0, **kwargs) -> MusicResult:
        key = self.store.secret(self.api_key_env)
        ms = int(duration * 1000)
        if not MUSIC_MS_MIN <= ms <= MUSIC_MS_MAX:
            clamped = max(MUSIC_MS_MIN, min(MUSIC_MS_MAX, ms))
            print(f"  ⚠ ElevenLabs Music 时长只收 {MUSIC_MS_MIN / 1000:g}~"
                  f"{MUSIC_MS_MAX / 1000:g} 秒，{duration:g}s 已钳到 "
                  f"{clamped / 1000:g}s（compose 侧按成片时长循环/裁剪）")
            ms = clamped
        body = {"model_id": self.model, "prompt": prompt,
                "music_length_ms": ms, "force_instrumental": True}
        try:
            resp = request_with_retry("POST", f"{self.base}/music", json=body,
                                      params={"output_format": self.output_format},
                                      headers={"xi-api-key": key,
                                               "Content-Type": "application/json"},
                                      timeout=300, desc="elevenlabs music")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"ElevenLabs Music 请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"ElevenLabs Music {resp.status_code}: {resp.text[:500]}")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(resp.content)
        return MusicResult(path=str(out_path),
                           cost=round(self.price_per_min * duration / 60.0, 4))

    def sound_effect(self, prompt, out_path, *, duration=1.2, **kwargs) -> MusicResult:
        """AI 音效生成（转场音效 C 路线，用户点名才用；生成一次落 sfx/ 库永久复用）。

        POST /v1/sound-generation，返回 mp3 字节流；duration_seconds 官方合法域
        0.5~30 秒。计费口径：指定时长按秒计，不指定按次一口价——本工程的转场
        音效恒为秒级短音，显式传时长更省。"""
        key = self.store.secret(self.api_key_env)
        body = {"text": prompt,
                "duration_seconds": round(max(SFX_SEC_MIN, min(SFX_SEC_MAX, duration)), 2),
                "prompt_influence": 0.3}
        try:
            resp = request_with_retry("POST", f"{self.base}/sound-generation", json=body,
                                      headers={"xi-api-key": key,
                                               "Content-Type": "application/json"},
                                      timeout=120, desc="elevenlabs sfx")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"ElevenLabs SFX 请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"ElevenLabs SFX {resp.status_code}: {resp.text[:500]}")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(resp.content)
        return MusicResult(path=str(out_path), cost=round(self.price_per_sfx, 4))
