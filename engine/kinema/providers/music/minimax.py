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

"""MiniMax 音乐生成（music-3.0）。

同步返回，与本厂 TTS 同一套响应形态：音频缺省是 **hex 字符串**（`data.audio`），
业务错误走 `base_resp.status_code`（可以是 HTTP 200 + 非零业务码）。

⚠ 开通限制：官方自 2026-08-20 起付费音乐接口不再对新用户开放（存量账号可用），
新账号请改用 elevenlabs 或本地曲库。契约参考：
https://platform.minimax.io/docs/api-reference/music-generation

**本工程只用它做纯器乐 BGM**：`is_instrumental: true`。理由是成片里已经有旁白
与对白，带人声的配乐会和旁白抢同一条频段——混音链的让路 EQ 与 sidechain 闪避
是按「器乐床 + 人声主轨」标定的，塞进第二条人声会把闪避判据搅乱。
需要唱词的场景请直接用 CLI 传 lyrics，而不是让 BGM 通道自作主张。

⚠ **时长不是请求参数**：官方按 prompt/lyrics 生成，没有"给我 60 秒"这样的字段
（价格页口径是「每首至多 5 分钟」）。所以调用方要的 `duration` 在这里只用于
**记账与告警**，实际长度以产物为准——compose 侧本来就按成片时长裁剪 BGM。
"""
from __future__ import annotations

from pathlib import Path

from ..base import MusicProvider, MusicResult
from .._util import auth_headers, download, request_with_retry
from ...errors import ProviderError


class MiniMaxMusicProvider(MusicProvider):
    name = "minimax_music"

    def __init__(self, conn: dict, store):
        self.conn = conn
        self.store = store
        self.base = conn.get("base_url", "https://api.minimax.io/v1").rstrip("/")
        self.model = conn.get("model", "music-3.0")
        self.api_key_env = conn.get("api_key_env", "MINIMAX_API_KEY")
        # 按首计费——官方口径是「每首至多 5 分钟」一口价，没有按分钟这一维度
        self.price_per_track = float(conn.get("price_per_track", 0.0))

    def generate(self, prompt, out_path, *, duration=60.0, **kwargs) -> MusicResult:
        auth = auth_headers(self.conn, self.store)
        lyrics = kwargs.get("lyrics")
        if not lyrics and not (prompt or "").strip():
            # 纯器乐档官方明写 prompt 必填（1~2000 字符）。本地判死比换一次服务端
            # 业务码强——那个码不会告诉你缺的是 prompt 还是别的。
            raise ProviderError("MiniMax 音乐的纯器乐档必须给 prompt（风格/情绪/场景描述）")
        body = {
            "model": self.model,
            "prompt": (prompt or "")[:2000],       # 官方上限 2000 字符
            # 纯器乐：成片里已有旁白与对白，带人声的配乐会和旁白抢同一条频段
            "is_instrumental": not lyrics,
            "output_format": "hex",                # 与下面的解码路径显式对齐，不吃缺省
            "audio_setting": {"sample_rate": 44100, "bitrate": 128000, "format": "mp3"},
        }
        if lyrics:
            body["lyrics"] = str(lyrics)[:3500]    # 官方上限 3500 字符
        try:
            resp = request_with_retry(
                "POST", f"{self.base}/music_generation", json=body,
                headers={**auth, "Content-Type": "application/json"},
                timeout=300, desc="minimax music")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"MiniMax 音乐请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"MiniMax 音乐 {resp.status_code}: {resp.text[:500]}")
        j = resp.json()
        br = j.get("base_resp") or {}
        code = br.get("status_code")
        if code not in (0, None):
            # 1002 是官方明说可重试的限流码，与余额不足/风控这类不可重试错误混成一句
            # 会让人白白放弃一次本该等一会儿再来的请求。业务错误走 HTTP 200 回吐，
            # `request_with_retry` 的 HTTP 码重试对它完全看不见。
            tip = "（限流，稍后重试即可；免费档 music-3.0-free 只有 3 RPM）" if code == 1002 else ""
            raise ProviderError(
                f"MiniMax 音乐业务错误 {code}: {br.get('status_msg')}{tip}")
        data = j.get("data") or {}
        audio = data.get("audio")
        if not audio:
            raise ProviderError(f"MiniMax 音乐未返回音频: {resp.text[:400]}")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if str(audio).startswith("http"):          # output_format=url 档（有效期 24h）
            download(audio, Path(out_path))
        else:
            Path(out_path).write_bytes(bytes.fromhex(audio))
        got = float((j.get("extra_info") or {}).get("music_duration") or 0) / 1000.0
        if got and duration and got + 1 < duration:
            # 时长不是请求参数，拿到的可能短于需要的；compose 侧会循环/裁剪，
            # 但短太多会听出接缝，所以说一声而不是闷着
            print(f"  ⚠ MiniMax 音乐产物 {got:.1f}s 短于需要的 {duration:.1f}s，"
                  "合成时会循环铺满（接缝明显的话换一段或改用本地曲库）")
        # **按首一口价**是官方唯一的计费维度（至多 5 分钟同价），没有「按分钟」这一档；
        # 按分钟折算会把一段 25 秒的产物记成 0.42 分钟的钱，那是一笔错账。
        return MusicResult(path=str(out_path), cost=round(self.price_per_track, 6))
