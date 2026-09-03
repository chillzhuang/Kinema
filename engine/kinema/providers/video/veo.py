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

"""Veo 图生视频 provider（Google Gemini API，长时任务 predictLongRunning）。

视频备选：首帧驱动图生视频，**原生音画恒开**（对白/音效随片生成，适合 native 模式）。
流程：建任务(POST models/{model}:predictLongRunning) → 轮询 operation 直到 done
→ 带 API key 下载 video.uri（产物服务端仅保留 2 天，立即落盘）。

要点（官方文档核对，ai.google.dev/gemini-api/docs/veo）：
· 现役仅 veo-3.1 三档（standard/fast/lite）；3.0/2.0 已关停，勿配；
· durationSeconds 枚举 4/6/8；1080p/4k/参考图/续片/**首尾帧插值**一律强制 8s
  ——4~6s 短镜只能 720p 纯首帧；
· aspectRatio 仅 "16:9"/"9:16" 两枚举（1:1 画布就近取 16:9 并告警）；
· 首帧 = instances[0].image.inlineData（纯 base64）；lastFrame 首尾帧插值须与 image 同用；
· 下载 video.uri 必须带 x-goog-api-key 头并跟随 302（裸 GET 会 4xx）；
· 不支持参考音频对口型（dubbed 用 seedance）；negativePrompt 参数已移除；
  seed 官方虽收但明言不保证确定性——均不发送，画面锚定靠首帧；
  SynthID 水印强制；无免费层，安全拦截不出片不计费；
· 中国大陆无法直连 generativelanguage.googleapis.com，需自备网络出口。
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from ..base import VideoProvider, VideoResult, resolution_prices
from .._util import download, poll_task, raise_for_poll, request_with_retry
from ...errors import ProviderError

_DURATIONS = (4, 6, 8)


def _inline(path: str) -> dict:
    """帧图 → inlineData（Gemini API 只收内联 base64，不收外部 URL/GCS）。"""
    s = str(path)
    if s.startswith("data:"):
        head, b64 = s.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
    elif s.startswith("http"):
        r = request_with_retry("GET", s, timeout=60, desc="veo frame")
        if r.status_code >= 400:   # 过期签名 URL 的错误页字节绝不能当图上送
            raise ProviderError(f"Veo 帧图拉取失败 {r.status_code}: {s[:120]}")
        mime = (r.headers.get("Content-Type") or "image/png").split(";")[0]
        b64 = base64.b64encode(r.content).decode()
    else:
        p = Path(s)
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        b64 = base64.b64encode(p.read_bytes()).decode()
    return {"inlineData": {"mimeType": mime, "data": b64}}


class VeoVideoProvider(VideoProvider):
    name = "veo"

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = conn.get("model", "veo-3.1-fast-generate-preview")
        self.api_key_env = conn.get("api_key_env", "GEMINI_API_KEY")
        self.resolution = conn.get("resolution", "720p")   # 720p/1080p/4k；非 720p 强制 8s；CLI --resolution 可覆盖
        self.price_per_second = float(conn.get("price_per_second", 0.72))
        self.price_per_second_4k = float(conn.get("price_per_second_4k", 0))  # 0=未配→回落基准价
        self.price_by_resolution = resolution_prices(conn)
        self.poll_interval = int(conn.get("poll_interval", 10))
        self.timeout = int(conn.get("timeout", 600))       # 官方口径峰值 6 分钟

    def billable_seconds(self, dur: float, *, dubbed: bool = False,
                         last_frame: bool = False) -> int:
        """时长枚举 4/6/8 就近取档；等距时取大档（宁多尾帧不截话——Veo 原生
        对白/音效按 prompt 节奏生成，截短会切话）。非 720p 与首尾帧插值
        （lastFrame 在场）官方一律强制 8s。"""
        if last_frame or self.resolution != "720p":
            return 8
        return min(_DURATIONS, key=lambda v: (abs(v - dur), -v))

    def generate(self, image, out_path, *, prompt="", dur=5.0, width=1080, height=1920,
                 seed=None, last_frame=None, ref_audio=None, **kwargs) -> VideoResult:
        if ref_audio:
            raise ProviderError(
                "Veo 不支持喂参考音频对口型——dubbed 模式请用 seedance；"
                "Veo 原生音画恒开，适合 native 模式（-m b）。")
        key = self.store.secret(self.api_key_env)
        n = self.billable_seconds(dur, last_frame=bool(last_frame))
        if width == height:
            print("    [!] Veo 不支持 1:1，按 16:9 出片后需自行重构取景")
        aspect = "9:16" if height > width else "16:9"
        instance = {"prompt": prompt, "image": _inline(image)}
        if last_frame:
            instance["lastFrame"] = _inline(last_frame)   # 首尾帧插值，须与 image 同用
        body = {"instances": [instance],
                "parameters": {"aspectRatio": aspect,
                               "resolution": self.resolution,
                               # 官方参数表呈现为字符串、SDK 走同一 REST 发 int——发 int；
                               # 若真实小样撞 400 再改 str(n)（规格建议的降级路径）
                               "durationSeconds": n,
                               # 图生视频只允许 allow_adult（allow_all 是文生视频口径）
                               "personGeneration": "allow_adult"}}
        # negativePrompt 已从 3.1 文档移除；seed 官方收但不保证确定性——均不发送

        try:
            resp = request_with_retry(
                "POST", f"{self.base}/models/{self.model}:predictLongRunning",
                json=body, headers={"x-goog-api-key": key}, timeout=60,
                desc="veo create")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Veo 建任务失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"Veo {resp.status_code}: {resp.text[:500]}")
        op = resp.json().get("name")
        if not op:
            raise ProviderError(f"Veo 未返回 operation name: {resp.text[:300]}")
        print(f"    Veo 任务: {op}")

        uri = self._poll(op, key)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        # 产物下载必须带 API key 头且跟随重定向；服务端仅保留 2 天——立即落盘
        download(uri, out_path, headers={"x-goog-api-key": key})
        return VideoResult(path=str(out_path),
                           cost=round(self.effective_price_per_second * n, 4),
                           has_audio=True,   # 3.1 全系原生音频恒开（无关闭参数）
                           meta={"provider": "veo", "model": self.model,
                                 "resolution": self.resolution, "task_id": op})

    def _poll(self, op: str, key: str) -> str:
        # 轮询三纪律统一在 _util.poll_task，此处只保留 Google operation 的解析
        url = f"{self.base}/{op}"   # operation name 整串原样拼接

        def check():
            r = request_with_retry("GET", url, headers={"x-goog-api-key": key},
                                   timeout=30, attempts=2, desc="veo poll")
            raise_for_poll(r, what="Veo", task_id=op)
            j = r.json() or {}
            if not j.get("done"):
                return None
            if j.get("error"):
                e = j["error"]
                raise ProviderError(
                    f"Veo 任务失败 code={e.get('code')}: {e.get('message')}"
                    "（安全/音频过滤拦截不计费）")
            samples = (((j.get("response") or {}).get("generateVideoResponse") or {})
                       .get("generatedSamples") or [])
            uri = (samples[0].get("video") or {}).get("uri") if samples else None
            if not uri:
                raise ProviderError(f"Veo 完成但无 video.uri: {str(j)[:300]}")
            return uri

        return poll_task(check, what="Veo", task_id=op,
                         timeout=self.timeout, interval=self.poll_interval)
