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

"""离线口型精修桩：把底片原样落到产物路径（本地路径直收，不要求 URL）。

零 ffmpeg、零网络——mock 链路只验「stage 的调度、登记与指针切换」，
口型像不像不在离线验证面里。"""
from __future__ import annotations

import shutil

from pathlib import Path

from ..base import LipsyncProvider, VideoResult


class MockLipsyncProvider(LipsyncProvider):
    name = "mock"
    price_per_second = 0.0

    def configured(self) -> tuple[bool, str]:
        return True, ""

    def generate(self, video_url: str, audio_url: str, out_path: str,
                 *, dur: float = 0.0, **kwargs) -> VideoResult:
        src = Path(str(video_url))
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            # copyfile 而非 copy2：产物 mtime 必须是"现在"——stage 的幂等判据
            # 按「lips 比底片与 wav 都新」比对，继承底片 mtime 会让重算永不收敛
            shutil.copyfile(src, out)
        else:
            out.write_bytes(b"mock-lipsync")
        return VideoResult(path=str(out), cost=0.0, has_audio=True,
                           meta={"provider": self.name})
