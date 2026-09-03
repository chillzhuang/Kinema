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

"""agent 原生生图 provider（工单模式）——生图能力长在驱动引擎的 agent 身上。

引擎（纯 Python、零 LLM）调不到 Codex / Gemini 之类 agent 会话里的原生
imagegen 工具，所以这里反转调用方向：`generate()` 不发任何网络请求——

- 目标文件已存在 → 验收登记（返回 `ImageResult`，零成本入账，review/
  版本/血缘与 API 链完全同一条写路径）；
- 不存在 → 把**拼装完成的最终提示词**（风格前缀/运镜前置/角色锚/契约句
  一个不少——agent 拿裸 image_prompt 自由发挥等于绕开整条既有的
  提示词装配）连同目标路径、尺寸、参考图，落进同目录的
  `agent_order.json` 工单，然后抛 `ProviderError` 走既有的「批末汇总、
  不中断」失败语义；stage 收尾会把工单路径与操作指引一次性报出。

agent 照工单产图后**重跑同一条 gen-image 命令即自动验收**——与断点续跑
同构，没有第二套状态机。路由三级解析（models 显式激活 > 声明
`KINEMA_AGENT_IMAGEGEN=1` > 默认 provider）见 `models.image_route`；
本适配器不 Codex 专属，任何有原生生图能力的 agent 都走这一条。

验收按工单尺寸体检（`pipeline.mediacheck.inspect_image`，与素材直供同一把尺）：
解不出图像流或宽高比与工单不符即拒收、工单保留待重画；分辨率不足只告警——
草图看构图允许低分，但比例错的图进合成必被裁掉一截。
"""

import json
import threading
from pathlib import Path

from ..base import ImageProvider, ImageResult
from ...errors import ProviderError

ORDER_BASENAME = "agent_order.json"
PENDING_MARK = "待 agent 产图"
_ORDER_README = ("用你的原生生图能力逐条产图：按 prompt 生成 width×height 的图片"
                 "写到 path（PNG）；ref_images 是一致性参考（工具支持垫图就用上）；"
                 "全部完成后重跑原 gen-image 命令即自动验收登记。")

_LOCK = threading.Lock()   # 并发工作线程共写同一份工单


def has_pending_order(path: str | Path) -> bool:
    """判断目标路径是否仍在 agent 工单中，供 stage 绕过旧图复用分支。"""
    target = str(path)
    order = Path(path).parent / ORDER_BASENAME
    try:
        doc = json.loads(order.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(str(item.get("path")) == target
               for item in (doc.get("orders") or [])
               if isinstance(item, dict))


def prepare_order(order: str | Path) -> None:
    """重跑前只清理没有落盘目标的旧单，保留可零成本验收的条目。"""
    order = Path(order)
    try:
        doc = json.loads(order.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        order.unlink(missing_ok=True)
        return
    keep = [item for item in (doc.get("orders") or [])
            if isinstance(item, dict)
            and Path(str(item.get("path") or "")).is_file()
            and Path(str(item.get("path") or "")).stat().st_size > 0]
    if not keep:
        order.unlink(missing_ok=True)
        return
    doc["orders"] = keep
    order.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _order_entry(path: Path) -> dict | None:
    order = path.parent / ORDER_BASENAME
    try:
        doc = json.loads(order.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return next((item for item in (doc.get("orders") or [])
                 if isinstance(item, dict) and str(item.get("path")) == str(path)), None)


def _accept(path: Path, prompt: str, width: int, height: int) -> dict:
    """交付物验收：尺寸按工单画布体检，提示词与工单不同时把工单那份带回登记。"""
    from ...pipeline import mediacheck
    rep = mediacheck.inspect_image(path, canvas=(width, height))
    if rep.get("hard_fail"):
        raise ProviderError(
            f"agent 交付的图不可用（{path.name}）："
            + "；".join(f["msg"] for f in rep["hard_fail"]) + "——工单保留，请重画")
    crop = float(rep.get("crop") or 0.0)
    if crop > mediacheck.SUPPLY_ASPECT_TOL:
        raise ProviderError(
            f"agent 交付的图宽高比与工单不符（{rep.get('width')}×{rep.get('height')}，"
            f"工单 {width}×{height}，取景会裁掉约 {crop * 100:.0f}%）——工单保留，请按尺寸重画")
    meta = {"provider": "agent", "ingested": True,
            "warnings": [w["msg"] for w in rep.get("warn") or []]}
    entry = _order_entry(path)
    if entry and entry.get("prompt") and entry["prompt"] != prompt:
        meta["order_prompt"] = entry["prompt"]
    return meta


def _drop_order_entry(path: str | Path) -> None:
    """验收完成后移除单条工单，最后一条完成时删除工单文件。"""
    order = Path(path).parent / ORDER_BASENAME
    target = str(path)
    with _LOCK:
        try:
            doc = json.loads(order.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        orders = [item for item in (doc.get("orders") or [])
                  if isinstance(item, dict) and str(item.get("path")) != target]
        if orders:
            doc["orders"] = orders
            order.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            order.unlink(missing_ok=True)


class AgentImageProvider(ImageProvider):
    name = "agent"
    # 当前会话的原生 image_gen 工具最多接收 5 张参考图；声明能力上限，
    # 由 stage_gen_image 在工单和验收两次装配时使用同一份实际清单，禁止静默截断。
    max_ref_images = 5

    def __init__(self, conn: dict | None = None, store=None):
        self.conn = conn or {}

    def generate(self, prompt, out_path, *, ref_images=None, seed=None,
                 width=1080, height=1920, **kwargs) -> ImageResult:
        p = Path(out_path)
        if p.is_file() and p.stat().st_size > 0:
            meta = _accept(p, prompt, width, height)
            _drop_order_entry(p)
            return ImageResult(path=str(out_path), cost=0.0, meta=meta)
        entry = {
            "label": str(kwargs.get("label") or p.stem),
            "path": str(out_path),
            "prompt": prompt,
            "width": width,
            "height": height,
            "ref_images": [str(r) for r in (ref_images or [])],
            "seed": seed,
        }
        order = p.parent / ORDER_BASENAME
        with _LOCK:
            order.parent.mkdir(parents=True, exist_ok=True)
            try:
                doc = json.loads(order.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
            if not isinstance(doc.get("orders"), list):
                doc = {"_readme": _ORDER_README, "orders": []}
            # 同一 path 只留最新一条：重开工单/换提示词都不该在单里叠出重影
            doc["orders"] = [e for e in doc["orders"] if e.get("path") != entry["path"]]
            doc["orders"].append(entry)
            order.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        raise ProviderError(f"{PENDING_MARK}（工单 {order.name}）")
