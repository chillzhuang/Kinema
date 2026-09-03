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

"""离线 mock 图像 provider：用 ffmpeg 生成占位分镜帧，用于端到端测试。

生成一张纯色竖屏帧（颜色由 seed+prompt 派生以区分各镜），中央叠加镜号标签。
无需任何 API key，便于验证分镜时序、Ken Burns 运镜与合成链路。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..base import ImageProvider, ImageResult
from ...ffmpeg import drawtext_text, filter_literal, run

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _find_font() -> str | None:
    for f in _FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    return None


class MockImageProvider(ImageProvider):
    name = "mock"
    # 离线彩排要能走通写实档全链路，故声明受信（mock 从不发真实请求）
    trusted_face_source = True

    def generate(self, prompt, out_path, *, ref_images=None, seed=None,
                 width=1080, height=1920, **kwargs) -> ImageResult:
        h = hashlib.md5(f"{seed}|{prompt}".encode("utf-8")).hexdigest()
        # 提亮，避免帧太暗看不清
        r, g, b = (min(255, 55 + int(int(h[i:i + 2], 16) * 0.72)) for i in (0, 2, 4))
        color = f"0x{r:02X}{g:02X}{b:02X}"
        label = str(kwargs.get("label") or "MOCK")

        filters = []
        font = _find_font()
        if font:
            filters.append(
                f"drawtext=fontfile={filter_literal(font)}:text={drawtext_text(label)}:fontcolor=white:"
                f"fontsize=150:x=(w-text_w)/2:y=(h-text_h)/2-120:"
                f"box=1:boxcolor=black@0.35:boxborderw=30"
            )
            filters.append(
                f"drawtext=fontfile={filter_literal(font)}:text='kinema mock frame':"
                f"fontcolor=white@0.85:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2+120"
            )
        vf = ",".join(filters) if filters else "null"

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        run(
            ["-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}",
             "-vf", vf, "-frames:v", "1", str(out_path)],
            desc="mock image",
        )
        return ImageResult(path=str(out_path), cost=0.0,
                           meta={"provider": "mock", "color": color})
