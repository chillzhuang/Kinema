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

"""离线 mock 音乐 provider：生成柔和背景床（两个正弦叠加），用于验证 BGM 混音。"""
from __future__ import annotations

from pathlib import Path

from ..base import MusicProvider, MusicResult
from ...ffmpeg import run


class MockMusicProvider(MusicProvider):
    name = "mock"

    def generate(self, prompt, out_path, *, duration=60.0, **kwargs) -> MusicResult:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        run(
            ["-f", "lavfi", "-i", f"sine=frequency=196:duration={duration}",
             "-f", "lavfi", "-i", f"sine=frequency=261.63:duration={duration}",
             "-filter_complex", "[0][1]amix=inputs=2,volume=0.3[a]",
             "-map", "[a]", "-ar", "44100", "-ac", "2", str(out_path)],
            desc="mock music",
        )
        return MusicResult(path=str(out_path), cost=0.0)
