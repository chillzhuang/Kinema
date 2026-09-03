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

"""MiniMax 图像生成（image-01）。

形状比视频简单得多：**同步返回，没有异步任务与轮询**，文生图与图生图共用一个端点，
给不给 `subject_reference` 决定走哪条。

⚠ 上线前核对（本适配器同时用到两页：t2i 管基础参数、i2i 管 subject_reference）：
https://platform.minimax.io/docs/api-reference/image-generation-t2i
https://platform.minimax.io/docs/api-reference/image-generation-i2i

⚠ 两站的一处真差异：国内站另有可选的 `aigc_watermark`（布尔，缺省 false），国际站没有。

**参考图能力与本工程的落差要说清楚**：官方 `subject_reference` 只支持
`type: "character"`、且**每次请求只收一张**，建议是单张正面人像。而本工程的
一致性工法是「三区角色设定图 + 参考库垫图 + 版式样板图」同时喂多张——这里只能
取一张，且三区设定图喂进去会被当成一张普通人像参考。所以：
  · 有角色设定图时取第一张作 character 参考（比没有强）
  · 多参考图场景（moodboard/样板图/多角色）请继续用 Seedream 那一档
"""
from __future__ import annotations

import base64
from pathlib import Path

from ..base import ImageProvider, ImageResult
from .._util import auth_headers, download, file_to_data_url, request_with_retry
from ...errors import ProviderError

# 官方 aspect_ratio 枚举；宽高另可用 width/height（512~2048 且须被 8 整除，成对给）
RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")


def _pixel_ok(v: int) -> bool:
    """像素档的合法区间（官方 [512, 2048]，且须被 8 整除——取整由调用方做）。"""
    return bool(v) and 512 <= (v // 8 * 8) <= 2048


def _ratio_of(width: int, height: int) -> str | None:
    if not width or not height:
        return None
    r = width / height
    best = min(RATIOS, key=lambda s: abs(r - (int(s.split(":")[0]) / int(s.split(":")[1]))))
    bw, bh = (int(x) for x in best.split(":"))
    return best if abs(r - bw / bh) < 0.06 else None


class MiniMaxImageProvider(ImageProvider):
    name = "minimax_image"
    max_prompt_chars = 1500       # 官方硬上限；PromptCompiler 超限即拒绝，不截断
    ref_kind = "character"       # subject_reference 只有 type=character 一档：
    #                              收到的图会被当角色主体特征学走，场景/风格垫图不能进
    max_ref_images = 1           # 官方每次只收一张

    def __init__(self, conn: dict, store):
        self.conn = conn
        self.store = store
        self.base = conn.get("base_url", "https://api.minimax.io/v1").rstrip("/")
        self.model = conn.get("model", "image-01")
        self.api_key_env = conn.get("api_key_env", "MINIMAX_API_KEY")
        self.price_per_image = float(conn.get("price_per_image", 0.0))
        if self.model != "image-01":
            # image-01-live 的 width/height 与 21:9 档不生效，另有 live 专属的 style 对象；
            # 本适配器按 image-01 的参数表拼装，换模型时哪些参数会被忽略要说清楚
            print(f"  ⚠ MiniMax 图像适配器按 image-01 的参数表拼装，当前 model="
                  f"{self.model}：该模型下 width/height 等参数可能不生效，请核对官方字段表")

    def generate(self, prompt, out_path, *, ref_images=None, seed=None,
                 width=1080, height=1920, **kwargs) -> ImageResult:
        auth = auth_headers(self.conn, self.store)
        if len(prompt or "") > self.max_prompt_chars:
            raise ProviderError(
                f"MiniMax 图像提示词 {len(prompt)} 字符，超过官方上限 "
                f"{self.max_prompt_chars}；拒绝发送，禁止静默截断")
        body = {"model": self.model, "prompt": prompt, "n": 1,
                # 官方缺省 false，显式写死：提示词是本工程拼装的契约，
                # 让平台自动改写等于把画风前缀与负面约束交给别人重写
                "prompt_optimizer": False,
                "response_format": "base64"}
        # **优先给像素而不是给比例**：官方 aspect_ratio 的每一档都绑定一个固定分辨率
        # （16:9 恒为 1280×720、9:16 恒为 720×1280…），而且同时给两者时**比例优先**。
        # 本工程三档画布（1920×1080 / 1080×1920 / 1080×1080）全部精确命中枚举，
        # 于是走比例这条路只能拿到画布 44% 的像素量，合成时再放大 1.5 倍——每一张
        # 分镜图都发虚，且全程没有任何告警。像素合法（[512,2048] 且被 8 整除）就直接给。
        if _pixel_ok(width) and _pixel_ok(height):
            body["width"], body["height"] = width // 8 * 8, height // 8 * 8
        else:
            ratio = _ratio_of(width, height) or "1:1"
            body["aspect_ratio"] = ratio
            print(f"  ⚠ MiniMax 图像的像素档只收 [512,2048]，{width}×{height} 出界，"
                  f"已改用比例档 {ratio}（分辨率由平台按该档固定值决定）")
        if seed is not None:
            body["seed"] = int(seed)
        first = next((r for r in (ref_images or []) if r), None)
        if first:
            if len(ref_images or []) > 1:
                print(f"  ⚠ MiniMax 图像每次只收一张参考图（官方限制），"
                      f"本镜 {len(ref_images)} 张只用第一张；多垫图场景请用 Seedream 那一档")
            body["subject_reference"] = [{"type": "character",
                                          "image_file": self._ref(first)}]
        try:
            resp = request_with_retry(
                "POST", f"{self.base}/image_generation", json=body,
                headers={**auth, "Content-Type": "application/json"},
                timeout=180, desc="minimax image")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"MiniMax 图像请求失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"MiniMax 图像 {resp.status_code}: {resp.text[:500]}")
        j = resp.json()
        # 与 TTS 同：业务错误可以是 HTTP 200 + base_resp.status_code≠0
        br = j.get("base_resp") or {}
        if br.get("status_code") not in (0, None):
            raise ProviderError(
                f"MiniMax 图像业务错误 {br.get('status_code')}: {br.get('status_msg')}")
        data = j.get("data") or {}
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        b64 = (data.get("image_base64") or [None])[0]
        if b64:
            Path(out_path).write_bytes(base64.b64decode(b64))
        else:
            url = (data.get("image_urls") or [None])[0]
            if not url:
                meta = j.get("metadata") or {}
                raise ProviderError(
                    f"MiniMax 图像未返回产物（success={meta.get('success_count')} "
                    f"failed={meta.get('failed_count')}）: {resp.text[:300]}")
            download(url, Path(out_path))      # url 档有效期 24h，即时落盘
        return ImageResult(path=str(out_path), cost=self.price_per_image,
                           meta={"model": self.model})

    def _ref(self, path: str) -> str:
        if str(path).startswith(("http://", "https://", "data:")):
            return str(path)
        p = Path(path)
        if not p.is_file():
            raise ProviderError(f"参考图不存在: {path}")
        # 官方只收 JPG/JPEG/PNG 且小于 10MB。本地判死比让服务端回一个语焉不详的
        # 业务码强——本工程的设定图默认是 png，webp 走到这里会被服务端拒。
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise ProviderError(
                f"MiniMax 参考图只收 JPG/JPEG/PNG（收到 {p.suffix}）: {p}——请先转码")
        if p.stat().st_size >= 10 * 1024 * 1024:
            raise ProviderError(
                f"MiniMax 参考图须小于 10MB（当前 {p.stat().st_size / 1048576:.1f}MB）: {p}")
        return file_to_data_url(str(p))
