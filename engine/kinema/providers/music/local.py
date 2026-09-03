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

"""本地免版权音乐库 provider。

从音频库 BGM 子库（music/bgm/ 按情绪分目录 calm/upbeat/cinematic/ambient）挑一首，
循环/裁剪到目标时长、**入轨响度归一**（mixdown.BGM_TARGET_I，各曲目原始响度差十几 dB，
不归一就是"有的集 BGM 吵有的轻"）、加淡入淡出。**ELEVENLABS_API_KEY 为空时自动启用**（见 ModelRouter）。
零成本、商用安全（放你自己的正规免版权曲子进去即可灵活调配）。库为空时退化为合成氛围床。

情绪目录与场景关键词来自 config/audio.yaml 的 bgm 段（audio_registry 统一读取，
缺 yaml 回落内嵌表）；库根发现：KINEMA_MUSIC_DIR → <repo>/music → ./music。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..base import MusicProvider, MusicResult
from ...audio_registry import library_root, load_registry
from ...ffmpeg import run
from ...pipeline import mixdown


class LocalMusicProvider(MusicProvider):
    name = "local"

    def __init__(self, store=None):
        self.store = store
        self.reg = load_registry()
        self.root = library_root(self.reg, store)

    def _pick(self, prompt, mood) -> Path | None:
        if not self.root or not self.root.is_dir():
            return None
        bgm = self.reg.get("bgm") or {}
        folder = None
        if mood and mood in bgm:                          # profile 显式 mood 直取
            d = self.root / ((bgm[mood] or {}).get("dir") or f"bgm/{mood}")
            folder = d if d.is_dir() else None
        if folder is None:                                # 提示词/画风关键词兜底匹配
            low = (prompt or "").lower()
            for m, meta in bgm.items():
                kws = (meta or {}).get("keywords") or []
                d = self.root / ((meta or {}).get("dir") or f"bgm/{m}")
                if d.is_dir() and any(str(k).lower() in low for k in kws):
                    folder = d
                    break
        base = folder or (self.root / "bgm")
        tracks = sorted(base.rglob("*.mp3")) if base.is_dir() else []
        if not tracks:
            tracks = sorted(self.root.rglob("*.mp3"))     # 库内乱放也兜住
        if not tracks:
            return None
        idx = int(hashlib.md5(f"{prompt}|{mood}".encode()).hexdigest()[:6], 16) % len(tracks)
        return tracks[idx]

    def generate(self, prompt, out_path, *, duration=60.0, mood=None, **kwargs) -> MusicResult:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        track = self._pick(prompt, mood)
        if track:
            fade_out = max(0.1, float(duration) - 2.0)
            src = ["-stream_loop", "-1", "-i", str(track), "-t", f"{duration:.2f}"]
            # 入轨归一：同一情绪目录里五首的响度就横跨 6.5 dB（-16.3 ~ -22.8 LUFS），
            # 直接写盘就是"有的集 BGM 吵有的轻"的根因（末级归一救不了——它整体推，
            # 救不了 BGM 与旁白的配比）。只测不改跑一遍 loudnorm 分析 → 静态增益推到
            # BGM_TARGET_I + 限幅兜峰值：确定性、不改动态范围；测的就是循环裁剪后的
            # 这一段而不是整首原曲。
            gain = mixdown.bgm_gain_db(mixdown.measure_loudness(mixdown.measure_file_args(src)))
            level = mixdown.master_filter(gain, limit=mixdown.BGM_LIMIT_PEAK)
            run([*src, "-af", f"{level},afade=t=in:st=0:d=1.5,"
                 f"afade=t=out:st={fade_out:.2f}:d=2",
                 "-ar", "44100", "-ac", "2", str(out_path)], desc="local bgm")
            return MusicResult(path=str(out_path), cost=0.0)
        # 库为空 → 合成柔和氛围床。这是明显的"机器音"，正式成片不该静默烧进
        # 交付物——大声告知补库路径
        print("  ⚠ 本地音乐库为空，BGM 已退化为合成氛围床（正弦波）——"
              "运行 `python music/download.py` 拉起始曲库，或设置 ELEVENLABS_API_KEY")
        run(["-f", "lavfi", "-i", f"sine=frequency=196:duration={duration}",
             "-f", "lavfi", "-i", f"sine=frequency=261.63:duration={duration}",
             "-filter_complex", "[0][1]amix=inputs=2,volume=0.3[a]", "-map", "[a]",
             "-ar", "44100", "-ac", "2", str(out_path)], desc="synth bgm")
        return MusicResult(path=str(out_path), cost=0.0)
