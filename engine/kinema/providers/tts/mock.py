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

"""离线 mock TTS provider：按文本估算时长，生成柔和正弦占位人声轨。

无需 API key。时长按中文≈5字/秒、英文≈2.5词/秒估算，用于驱动分镜时序对齐
（tts 阶段会用真实音频时长回填 shot.dur，实现「先锁音频再对齐」）。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..base import TTSProvider, TTSResult
from ...ffmpeg import run


def estimate_duration(text: str) -> float:
    cjk = len(re.findall(r"[一-鿿]", text))
    latin_words = len(re.findall(r"[A-Za-z]+", text))
    dur = cjk / 5.0 + latin_words / 2.5
    return max(1.2, round(dur, 2))


class MockTTSProvider(TTSProvider):
    name = "mock"

    def synthesize(self, text, out_path, *, voice=None, **kwargs) -> TTSResult:
        dur = estimate_duration(text)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        run(
            ["-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}",
             "-af", "volume=0.05", "-ar", "44100", "-ac", "1", str(out_path)],
            desc="mock tts",
        )
        return TTSResult(
            audio_path=str(out_path),
            segments=[{"text": text, "start": 0.0, "end": dur}],
            cost=0.0,
        )
