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

"""控制视频素材库 —— 目录布局与 `asset.json` sidecar 读写。

素材**不进章节契约**：一次上传就是 `assets/<id>/` 下一个自足的目录，存在性由
磁盘推导（同 previz 的 reel 清单）。这样 `control build` 全程不 load/save 章节
文档，多条素材并行处理也不会与绑定、编排保存互相丢更新。

sidecar 一律走 `atomic_write_json`：build 每 24 帧回写一次进度，而 Studio 扫描器
每 3 秒读同一个文件——非原子写必然被读到半截，而读端按惯例把解析失败当「没有这条
素材」，表现就是处理中的素材在页面上闪烁消失。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .. import lineage
from ..storage import atomic_write_json
from .params import CONTROL_SUBDIR

# 素材目录下的固定文件名。键即 `asset.json.outputs` 的键，也是 Studio 预览的档位名。
OUTPUTS = {
    "control": "control.mp4",   # 灰度人物深度 + OpenPose 骨骼，喂模型的主控制视频
    "compare": "compare.mp4",   # 二合一对照（左源片 | 右控制视频），控制台放的是它
    "styled": "styled.mp4",     # 白色黏土浮雕 + 辉光骨骼，给人看的精细版
    "sheet": "sheet.png",       # 对照图（左源片 | 右控制视频），缩略带用
    "strip": "strip.png",       # 每半秒一格的缩略条，控制台在它上面拖选段窗
    "source": "source.mp4",     # cfr 归一后的源片副本
}
SIDECAR = "asset.json"
# 逐帧缓存：pass1 的推理结果。调参重渲不必重跑模型，成功收尾后删除。
CACHE = "_cache.npz"

VIDEO_EXTS = frozenset({".mp4", ".mov"})


def control_dir(project) -> Path:
    return project.subdir(CONTROL_SUBDIR)


def assets_dir(project) -> Path:
    d = control_dir(project) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def incoming_dir(project) -> Path:
    """上传落盘的临时位。下划线前缀标记中间产物，build 成功后移走。"""
    d = control_dir(project) / "_incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_dir(project, asset_id: str) -> Path:
    return assets_dir(project) / asset_id


def cut_path(project, shot_id) -> Path:
    """某镜绑定后裁出的那一段（带源片同区间音轨的审看件）；上传给视频模型的是它的无声副本，
    见 `bind.send_path`。"""
    return control_dir(project) / f"shot_{shot_id}_control.mp4"


def shot_compare_path(project, shot_id, *, tiles: int) -> Path:
    """某镜的对照片：`tiles=2` 是源片段 | 控制段，`tiles=3` 再加生成片段。

    按需构建：大多数镜不会被点开审看，每镜先烧一份是白花几秒转码和一份磁盘。
    两种规格各占一个文件名——合用一个名字的话，成片落下之后盘上那份二合一就成了
    会被当成三合一播的陈货。
    """
    return control_dir(project) / f"shot_{shot_id}_compare{tiles}.mp4"


def asset_id_for(src: str | Path) -> str:
    """`<文件名 slug>-<内容指纹前 4 位>`。

    指纹而不是文件名：两个同名但内容不同的源片（各家导出器都爱叫 `export.mp4`）
    因此不会撞进同一个 id。
    """
    p = Path(src)
    slug = re.sub(r"[^a-z0-9]+", "-", p.stem.lower()).strip("-") or "clip"
    fp = lineage.fingerprint(str(p))
    if not fp:
        raise FileNotFoundError(str(p))
    return f"{slug[:32]}-{fp.split(':', 1)[1][:4]}"


def unique_asset_id(project, src: str | Path) -> str:
    """派生一个**尚未占用**的素材 id，撞了就缀号。

    重传同一个源片是常规操作（上一次处理得不理想想重来、或想留两份各切各的段），
    而就地覆盖是不可逆的：旧产物被顶掉，已绑的镜却还指着照旧产物裁出来的段落，
    盘上那一段与素材从此对不上。所以重传恒得一条新素材；要就地重建，走显式
    `--asset <既有 id>` ——那是明确表态，不是手滑。
    """
    base = asset_id_for(src)
    if not asset_dir(project, base).exists():
        return base
    n = 2
    while asset_dir(project, f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def read_asset(project, asset_id: str) -> dict | None:
    """读一条素材的 sidecar；不存在或读到半截都返回 None。"""
    p = asset_dir(project, asset_id) / SIDECAR
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_asset(project, asset_id: str, data: dict) -> None:
    atomic_write_json(asset_dir(project, asset_id) / SIDECAR, data)


def list_assets(project) -> list[dict]:
    """素材库全表，按上传时间倒序。纯磁盘推导，不读章节文档。"""
    root = control_dir(project) / "assets"
    if not root.is_dir():
        return []
    out = [a for d in sorted(root.iterdir()) if d.is_dir()
           for a in (read_asset(project, d.name),) if a]
    return sorted(out, key=lambda a: str(a.get("uploaded_at") or ""), reverse=True)


def media_paths(project, asset_id: str) -> dict[str, str]:
    """素材各产物的工作区相对路径（只收真在盘的）。"""
    d = asset_dir(project, asset_id)
    return {k: str(d / name) for k, name in OUTPUTS.items() if (d / name).is_file()}


def new_record(asset_id: str, name: str, source: dict) -> dict:
    return {
        "id": asset_id,
        "name": name,
        "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "queued",
        "progress": {"pass": 0, "done": 0, "total": int(source.get("frames") or 0)},
        "error": None,
        "source": source,
        "people": 0,
        "tracks": [],
        "outputs": {},
        "timings": {},
    }


def build_digest(project, asset_id: str) -> str | None:
    """产出的 `control.mp4` 的内容指纹——绑定时记进 `gen.control.build`，
    素材重建后据此判定已绑的段落过期（`bind.control_drift`）。"""
    return lineage.fingerprint(str(asset_dir(project, asset_id) / OUTPUTS["control"]))
