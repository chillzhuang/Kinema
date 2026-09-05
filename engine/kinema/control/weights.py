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

"""权重目录与显式下载。

**引擎运行期绝不静默下载**：这两份权重合计约 115 MB，跑在一条已经要花几分钟的
处理链前面，静默拉取会把「网络卡了」表现成「处理卡住了」。缺文件直接抛，
由 `control fetch` 显式取。姿态那两份由上游库自己管在 `~/.cache/rtmlib/`，
不搬——搬了就要跟着它的命名走，而那不是我们的契约。
"""
from __future__ import annotations

import os
from pathlib import Path

from ..errors import KinemaError

# (文件名, 下载地址, 约定大小 MB, 说明)。大小只做完整性粗检——半截下载的文件
# 交给 onnxruntime 去报「模型损坏」是最难懂的一种错。
WEIGHTS = (
    ("depth_anything_v2_vits.onnx",
     "https://github.com/fabio-sim/Depth-Anything-ONNX/releases/download/v2.0.0/"
     "depth_anything_v2_vits.onnx",
     99.0, "相对深度（Depth Anything V2 Small·Apache-2.0）"),
    ("selfie_multiclass_256x256.tflite",
     "https://storage.googleapis.com/mediapipe-models/image_segmenter/"
     "selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite",
     16.0, "人物分割（MediaPipe selfie_multiclass·Apache-2.0）"),
)


def weights_dir() -> Path:
    """`KINEMA_CONTROL_WEIGHTS` > `~/.cache/kinema/control/`。"""
    env = os.environ.get("KINEMA_CONTROL_WEIGHTS")
    return Path(env).expanduser() if env else Path.home() / ".cache" / "kinema" / "control"


def weight_path(name: str) -> Path:
    return weights_dir() / name


def missing() -> list[tuple[str, str, str]]:
    """缺失（或明显不完整）的权重：`[(文件名, 下载地址, 说明)]`。"""
    out = []
    for name, url, mb, note in WEIGHTS:
        p = weight_path(name)
        if not p.is_file() or p.stat().st_size < mb * 1024 * 1024 * 0.9:
            out.append((name, url, note))
    return out


def require() -> None:
    """权重不齐就抛，附下载命令。"""
    lack = missing()
    if not lack:
        return
    lines = "\n".join(f"  · {n} —— {note}" for n, _u, note in lack)
    raise KinemaError(
        f"深度捕捉的权重不齐（{weights_dir()}）：\n{lines}\n"
        "  跑 `python3 -m kinema control fetch` 下载，或设 KINEMA_CONTROL_WEIGHTS "
        "指向已有的权重目录")


def fetch(*, force: bool = False) -> list[str]:
    """下载缺失权重，返回本次真下了哪几个。"""
    import urllib.request
    d = weights_dir()
    d.mkdir(parents=True, exist_ok=True)
    got = []
    lack = {n for n, _u, _t in missing()}
    want = WEIGHTS if force else [w for w in WEIGHTS if w[0] in lack]
    for name, url, _mb, _note in want:
        dst = d / name
        tmp = dst.with_suffix(dst.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)   # noqa: S310  地址是本模块内的常量
        tmp.replace(dst)
        got.append(name)
    return got
