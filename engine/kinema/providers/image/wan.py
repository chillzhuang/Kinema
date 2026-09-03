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

"""通义万相图像 provider（阿里云百炼 DashScope，异步任务）。

国产性价比图像备选：wan2.7 新版 messages 协议一个接口覆盖文生图 + 0~9 张参考图。
流程：建任务(POST image-generation/generation，须带 X-DashScope-Async: enable)
→ 轮询(GET tasks/{id}) → 取 OSS 签名 URL 下载（仅 24 小时有效，立即落盘）。

要点（官方文档核对，help.aliyun.com/zh/model-studio）：
· 仅支持新版 messages 协议模型（wan2.7-image/-pro、wan2.6-image 等）；
  wan2.5 及以下旧协议请求体完全不同，不做兼容——高速开发期不留旧格式分支；
· size 用像素模式 "宽*高" 强制目标尺寸（档位模式 1K/2K 在有参考图时
  输出比例会跟随输入图，多比例出图必须像素模式）；wan2.7 像素上限 2048*2048；
· n 显式传 1——wan2.6-t2i 等模型默认 n=4，不显式传是四倍费用陷阱；
· 参考图收公网 URL 或 data URL（本地文件自动转 data URL）；seed ∈ [0, 2^31-1]；
· wan2.7 双档不支持 negative_prompt（模型级取消，wan2.6 系仍支持该参数）——
  负向意图写进正向提示词（「避免出现…」句式）；
· 北京/新加坡/美国弗吉尼亚三地 API Key 与域名相互隔离不可混用
  （国际站 base_url 换 https://dashscope-intl.aliyuncs.com/api/v1）。
"""
from __future__ import annotations

from pathlib import Path

from ..base import ImageProvider, ImageResult
from .._util import download, file_to_data_url, poll_task, raise_for_poll, request_with_retry
from ...errors import ProviderError


def _img_ref(path: str) -> str:
    """参考图：公网 URL / data URL 原样透传，本地文件转 data URL。"""
    s = str(path)
    return s if s.startswith(("http", "data:")) else file_to_data_url(s)


class WanImageProvider(ImageProvider):
    name = "wan"

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
        self.model = conn.get("model", "wan2.7-image")
        self.api_key_env = conn.get("api_key_env", "DASHSCOPE_API_KEY")
        self.price = float(conn.get("price_per_image", 0.2))
        self.poll_interval = int(conn.get("poll_interval", 4))
        self.timeout = int(conn.get("timeout", 300))

    def generate(self, prompt, out_path, *, ref_images=None, seed=None,
                 width=1080, height=1920, **kwargs) -> ImageResult:
        key = self.store.secret(self.api_key_env)
        refs = list(ref_images or [])
        if refs and "t2i" in self.model:   # wan2.6-t2i 等纯文生图模型不收 image 对象
            print(f"    [!] {self.model} 不支持参考图，已忽略 {len(refs)} 张"
                  "（角色一致性会打折——要参考图请换 wan2.7-image）")
            refs = []
        cap = 9 if self.model.startswith("wan2.7") else 4   # wan2.7 系 9 张；wan2.6-image 4 张
        content = [{"image": _img_ref(x)} for x in refs[:cap]]
        content.append({"text": prompt})
        params = {"size": f"{width}*{height}", "n": 1, "watermark": False}
        if seed is not None:
            params["seed"] = int(seed) % 2147483648
        body = {"model": self.model,
                "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": params}
        try:
            resp = request_with_retry(
                "POST", f"{self.base}/services/aigc/image-generation/generation",
                json=body, headers={"Authorization": f"Bearer {key}",
                                    "X-DashScope-Async": "enable"},
                timeout=60, desc="wan create")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Wan 建任务失败: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"Wan {resp.status_code}: {resp.text[:500]}")
        j = resp.json()
        task_id = (j.get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(
                f"Wan 未返回任务 id（code={j.get('code')}）: {j.get('message') or str(j)[:300]}")
        # 任务号立即可见：轮询断线可凭 id 找回（task_id 查询有效期 24 小时）
        print(f"    Wan 任务: {task_id}")

        url = self._poll(task_id, key)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        download(url, out_path)   # 签名 URL 公开可下、24h 过期——立即落盘
        return ImageResult(path=str(out_path), cost=self.price,
                           meta={"provider": "wan", "model": self.model,
                                 "task_id": task_id})

    def _poll(self, task_id, key) -> str:
        # 轮询三纪律（monotonic 截止/瞬态容忍/心跳）统一在 _util.poll_task：
        # 「只累加 sleep」的口径不含 HTTP 往返，网络抖动时会拖过名义上限
        url = f"{self.base}/tasks/{task_id}"

        def check():
            r = request_with_retry("GET", url,
                                   headers={"Authorization": f"Bearer {key}"},
                                   timeout=30, attempts=2, desc="wan poll")
            raise_for_poll(r, what="Wan", task_id=task_id)
            out = (r.json() or {}).get("output") or {}
            status = out.get("task_status")
            if status == "SUCCEEDED":
                # 新版协议产物路径：output.choices[].message.content[] 里的 image 字段
                for ch in out.get("choices") or []:
                    for item in ((ch.get("message") or {}).get("content") or []):
                        if item.get("image"):
                            return item["image"]
                raise ProviderError(f"Wan 任务完成但未取到图像 URL: {str(out)[:300]}")
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise ProviderError(
                    f"Wan 任务{status}: {out.get('message') or out.get('code') or out}")
            return None

        return poll_task(check, what="Wan", task_id=task_id,
                         timeout=self.timeout, interval=self.poll_interval,
                         timeout_hint="（task_id 查询有效期 24 小时）")
