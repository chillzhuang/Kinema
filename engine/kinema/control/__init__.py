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

"""深度控制视频 —— 把一段实拍片处理成「人物深度浮雕 + OpenPose 骨骼」的控制视频，
绑定到镜后作 Seedance `reference_video` 发出：**运动来自源片，外观来自分镜图**。

它是 previz、简笔分镜之外的第三条运动预演路径，三者每镜互斥，仲裁真源仍是
`sketchboard.active_guide`。

两层分工：

    素材库（磁盘推导，不进契约）      镜级绑定（进契约，engine-managed）
    assets/<id>/control.mp4    ──裁段──▶  shots[].control  +  shots[].gen.control

`build_asset` 全程不 load/save 章节文档，故多条素材并行处理与绑定、编排保存之间
不存在丢更新；反过来 `bind_shot` 要写文档，必须由调用方持章节操作锁。

**本模块的导入面是干净的**：`params` / `assets` / `bind` 只依赖引擎自身与
ffmpeg，感知栈（numpy / opencv / onnxruntime / mediapipe）一律在 `build_asset`
里现用现导。守卫见 `tests/test_control.py` —— 引擎的默认导入路径不该为一个可选
特性拖进几百 MB 依赖。
"""
from __future__ import annotations

from .assets import (asset_dir, asset_id_for, assets_dir, build_digest, control_dir,
                     cut_path, incoming_dir, list_assets, media_paths, read_asset)
from .assets import shot_compare_path
from .bind import (bind_shot, bound_shots, control_drift, control_seconds,
                   control_shot, delete_asset, request_seconds, send_path, unbind_shot)
from .compare import build_asset_compare, build_shot_compare
from .params import CONTROL_SUBDIR, MAX_SOURCE_SEC
from .soundtrack import bed_segments as soundtrack_segments
from .soundtrack import bed_signature as soundtrack_signature
from .soundtrack import build_bed as build_soundtrack
from .sync import describe as describe_sync
from .sync import estimate_lag, measure_sync


def build_asset(project, source, *, asset_id=None, name=None, styled=True,
                mock=False, on_progress=None) -> dict:
    """把一段源片处理成素材。感知栈在这里才被导入（见模块说明）。"""
    from .pipeline import build_asset as _build
    return _build(project, source, asset_id=asset_id, name=name, styled=styled,
                  mock=mock, on_progress=on_progress)


def available() -> tuple[bool, list[str]]:
    """感知栈与权重是否齐备：`(就绪, 缺失项文案)`。doctor 与 Studio 就绪条共用。"""
    from .models import readiness
    return readiness()


__all__ = [
    "CONTROL_SUBDIR", "MAX_SOURCE_SEC", "asset_dir", "asset_id_for", "assets_dir",
    "available", "bind_shot", "bound_shots", "build_asset", "build_asset_compare",
    "build_digest", "build_shot_compare", "build_soundtrack", "control_dir",
    "control_drift", "control_seconds", "control_shot", "cut_path", "delete_asset",
    "describe_sync", "estimate_lag", "incoming_dir", "list_assets", "measure_sync",
    "media_paths", "read_asset", "request_seconds", "send_path", "shot_compare_path",
    "soundtrack_segments", "soundtrack_signature", "unbind_shot",
]
