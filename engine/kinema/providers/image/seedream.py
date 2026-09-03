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

"""Seedream 图像 provider（字节 · 火山方舟 Ark，OpenAI 兼容 images 接口）。

推荐默认图像 provider：一次可出多张风格一致组图、中文渲染强、性价比高。
一致性：把角色设定图/风格板作为 ref_images 传入（Seedream 4.x 支持图像编辑/参考）。

连接信息来自 config/models.yaml 的 providers.seedream；密钥 ARK_API_KEY。
⚠ 上线前请核对：model id、size 上限、参考图字段名与计费单价（见 providers.md）。
"""
from __future__ import annotations

from pathlib import Path

from ..base import ImageProvider, ImageResult
from .._util import download, request_with_retry, save_bytes, file_to_data_url
from ...errors import ProviderError


class SeedreamProvider(ImageProvider):
    # 官方方式2「指定宽高像素值」的总像素上限（5.0 pro）。只限总像素与宽高比，
    # 不限单边；真 4K（829 万）超出本档，要换 4.5 型号
    MAX_PIXELS = 4624220

    name = "seedream"
    # 同账号方舟链路：文生图产物在 Seedance 输入侧受信（face-policy §2.2 实测）
    trusted_face_source = True

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
        self.model = conn.get("model", "doubao-seedream-5-0-pro-260628")
        self.api_key_env = conn.get("api_key_env", "ARK_API_KEY")
        # 官方按输出像素分两档：阈值以内按 price_per_image，超出按 price_per_image_hd
        # （未配置时沿用低档）。画布尺寸与 `--hd` 放大都会改变落在哪一档
        self.price = float(conn.get("price_per_image", 0.2))
        self.price_hd = float(conn.get("price_per_image_hd", 0) or 0)
        self.hd_pixels = int(conn.get("hd_pixels", 0) or 0)
        # 出图像素上限（0=按画布原尺寸发）。接口的方式2「指定宽高像素值」只限总像素
        # 与宽高比，不限单边；设了这个数就在**保持画布宽高比不变**的前提下放大到
        # 它以内。画布尺寸本身仍由 canvas 决定，合成与字幕不受影响。
        # 官方 5.0 pro 的总像素上限是 4624220（16:9 约 2864x1611），真 4K 要换 4.5 档。
        self.max_pixels = int(conn.get("max_pixels", 0))
        # `--hd` 单次点名时用的上限（配置未覆盖就取型号的官方上限）。与 max_pixels
        # 分开两个键：一个是「平时按多大出」，一个是「点名要高清时能到多大」
        self.max_pixels_cap = int(conn.get("max_pixels_cap", 0)) or self.MAX_PIXELS

    def _fit_pixels(self, width: int, height: int) -> tuple[int, int]:
        """把请求尺寸放大到像素上限以内，**宽高比逐比例不变**。

        按最简整数比放大而不是乘浮点系数：16:9 乘 1.4 会得到 2688x1512 这种
        恰好整除的巧合值，换成 1080x1080 或别的画布就会出现 1 像素的比例漂移，
        而模型按实际宽高出图、下游按画布比例做体检，漂移会被判成宽高比不符。

        `max_pixels` 未配或画布本身已超限时原样返回——缩小画面不是这里的职责，
        超限由服务端按它自己的约束裁决。"""
        if self.max_pixels <= 0 or width <= 0 or height <= 0:
            return width, height
        from math import gcd, isqrt
        g = gcd(width, height)
        rw, rh = width // g, height // g
        n = isqrt(self.max_pixels // (rw * rh))
        if n <= g:                      # 放大不了（或会缩小）就别动
            return width, height
        return rw * n, rh * n

    def generate(self, prompt, out_path, *, ref_images=None, seed=None,
                 width=1080, height=1920, **kwargs) -> ImageResult:
        key = self.store.secret(self.api_key_env)
        width, height = self._fit_pixels(width, height)
        body = {
            "model": self.model,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "response_format": "url",
            "watermark": False,
        }
        if seed is not None:
            body["seed"] = int(seed)
        if ref_images:
            body["image"] = [
                x if str(x).startswith(("http", "data:")) else file_to_data_url(x)
                for x in ref_images
            ]
        try:
            resp = request_with_retry(
                "POST", f"{self.base}/images/generations", json=body,
                headers={"Authorization": f"Bearer {key}"}, timeout=180,
                desc="seedream")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Seedream 请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"Seedream {resp.status_code}: {resp.text[:500]}")

        data = resp.json().get("data") or []
        if not data:
            raise ProviderError(f"Seedream 未返回图像: {resp.text[:500]}")
        item = data[0]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        if item.get("url"):
            download(item["url"], out_path)
        elif item.get("b64_json"):
            import base64
            save_bytes(base64.b64decode(item["b64_json"]), out_path)
        else:
            raise ProviderError(f"Seedream 返回缺少 url/b64_json: {item}")
        return ImageResult(path=str(out_path), cost=self.cost_for(width, height),
                           meta={"provider": "seedream", "model": self.model})

    def cost_for(self, width: int, height: int) -> float:
        """按实际出图像素落档的单张价。"""
        if self.price_hd and self.hd_pixels and width * height > self.hd_pixels:
            return self.price_hd
        return self.price
