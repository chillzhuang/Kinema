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

"""Nano Banana 图像 provider（Google Gemini API，generateContent 同步返回）。

海外效果优先图像：Gemini 图像模型（Nano Banana 系列），角色一致性最强梯队。
默认接 gemini-3-pro-image（效果优先）；要换更便宜的 gemini-3.1-flash-image，
在 providers 段加别名指 impl: nano_banana 即可（零代码）。

要点（官方文档核对，docs/image-generation + REST 示例）：
· 同步接口：POST {base}/models/{model}:generateContent，产物 base64 内联返回，无轮询；
· 参考图 = contents.parts 里的普通 inline_data part（纯 base64，不收外部 http URL——
  URL 参考图会先拉取字节再内联）；请求端 snake_case，响应端一定是 camelCase inlineData；
· 比例/分辨率走 generationConfig.responseFormat.image（aspectRatio 枚举 + imageSize
  大写 K，小写 k 被拒）；responseModalities 按官方口径恒发 ["TEXT","IMAGE"]——
  图像模型的响应本就带文字 part，产物提取只认 image/* 的 inlineData；
  gemini-2.5-flash-image 不支持 imageSize（且官方已排定 2026-10-02 关停，勿新配）
  ——要省钱改配 gemini-3.1-flash-image / -lite-image（lite 仅 1K）；
· seed 对图像模型未证实生效（proto 有字段、图像文档零提及）——不发送，一致性靠参考图；
· SynthID 水印强制；无负向提示词参数（避免物写进正向 prompt，与国产模型同款套路）；
· 中国大陆无法直连 generativelanguage.googleapis.com，需自备网络出口。
⚠ 官方主文档已迁 Interactions API，generateContent 归入 Legacy 但明示仍全量支持。
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from ..base import ImageProvider, ImageResult
from .._util import request_with_retry, save_bytes
from ...errors import ProviderError

# 官方 aspectRatio 常规枚举（3-pro/2.5-flash 口径；3.1-flash 另有 1:4 等极端比例不列入）
_ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")


def _aspect(w: int, h: int) -> str:
    """画布宽高 → 官方比例枚举里最接近的一档。"""
    r = w / max(1, h)
    return min(_ASPECTS, key=lambda a: abs(r - int(a.split(":")[0]) / int(a.split(":")[1])))


def _inline(ref: str) -> dict:
    """参考图 → inline_data part（Gemini 只收内联 base64，不收外部 URL）。"""
    s = str(ref)
    if s.startswith("data:"):
        head, b64 = s.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
    elif s.startswith("http"):
        r = request_with_retry("GET", s, timeout=60, desc="nano_banana ref")
        if r.status_code >= 400:   # 过期签名 URL 的错误页字节绝不能当图上送
            raise ProviderError(f"Nano Banana 参考图拉取失败 {r.status_code}: {s[:120]}")
        mime = (r.headers.get("Content-Type") or "image/png").split(";")[0]
        b64 = base64.b64encode(r.content).decode()
    else:
        p = Path(s)
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        b64 = base64.b64encode(p.read_bytes()).decode()
    return {"inline_data": {"mime_type": mime, "data": b64}}


class NanoBananaProvider(ImageProvider):
    name = "nano_banana"

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.model = conn.get("model", "gemini-3-pro-image")
        self.api_key_env = conn.get("api_key_env", "GEMINI_API_KEY")
        self.price = float(conn.get("price_per_image", 0.95))
        self.image_size = conn.get("image_size", "2K")   # ""=不发送（2.5-flash 不支持）

    def generate(self, prompt, out_path, *, ref_images=None, seed=None,
                 width=1080, height=1920, **kwargs) -> ImageResult:
        key = self.store.secret(self.api_key_env)
        parts = [_inline(x) for x in (ref_images or [])]
        inline_mb = sum(len(p["inline_data"]["data"]) for p in parts) / 1_000_000
        if inline_mb > 19:   # 官方 inline 请求体上限约 20MB，越限前给可行动的报错
            raise ProviderError(
                f"Nano Banana 参考图内联总量 ≈{inline_mb:.0f}MB 超过 ~20MB 请求上限——"
                "请压缩设定图或减少参考图张数（--refs 精选核心几张）。")
        parts.append({"text": prompt})
        img_cfg = {"aspectRatio": _aspect(width, height)}
        if self.image_size:
            img_cfg["imageSize"] = self.image_size
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                     "responseFormat": {"image": img_cfg}}}
        # seed 不发送：对 Gemini 图像模型未证实生效，确定性靠参考图+提示词
        try:
            resp = request_with_retry(
                "POST", f"{self.base}/models/{self.model}:generateContent",
                json=body, headers={"x-goog-api-key": key}, timeout=180,
                desc="nano_banana")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Nano Banana 请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"Nano Banana {resp.status_code}: {resp.text[:500]}")
        data, mime = self._extract_image(resp.json())
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        save_bytes(base64.b64decode(data), out_path)
        return ImageResult(path=str(out_path), cost=self.price,
                           meta={"provider": "nano_banana", "model": self.model,
                                 "mime_type": mime})

    @staticmethod
    def _extract_image(j: dict) -> tuple[str, str]:
        """从响应取最终图像 (base64, mimeType)（响应端字段一定是 camelCase inlineData）。

        Gemini 3 系 thinking 默认开，可能先出中间构图图（thought: true）——
        取最后一个非 thought 且 mimeType 为 image/* 的 part；无图时按官方失败
        双通道给可排障错误：prompt 级拦截看 promptFeedback.blockReason，产出级看
        finishReason（IMAGE_SAFETY/NO_IMAGE 等，此时 HTTP 仍是 200）。"""
        cands = j.get("candidates") or []
        if not cands:
            block = (j.get("promptFeedback") or {}).get("blockReason") or "未知"
            raise ProviderError(f"Nano Banana 提示词被拦截（blockReason={block}）")
        c0 = cands[0]
        imgs = [p["inlineData"] for p in ((c0.get("content") or {}).get("parts") or [])
                if not p.get("thought") and (p.get("inlineData") or {}).get("data")
                and str((p.get("inlineData") or {}).get("mimeType", "image/")).startswith("image/")]
        if not imgs:
            raise ProviderError(
                f"Nano Banana 未返回图像（finishReason={c0.get('finishReason') or '未知'}）")
        return imgs[-1]["data"], imgs[-1].get("mimeType") or "image/png"
