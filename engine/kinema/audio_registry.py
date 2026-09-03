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

"""音频资产注册表（config/audio.yaml 的读取层）：BGM 情绪目录 + 音效语义键的单一真源。

「场景 → 目录/标准文件名」一张表说清：合成时 BGM 按 `bgm` 段（情绪 → `bgm/<mood>/`
目录 + 场景关键词），转场音效按 `sfx` 段（语义键 → `sfx/...` 文件）。库根 `music/`
（bgm 与 sfx 两个子库），`KINEMA_MUSIC_DIR` 整体改址、`KINEMA_AUDIO_CONFIG`
换注册表。yaml 缺失/缺 PyYAML 回落内嵌表 EMBEDDED_AUDIO——零依赖内核不破；
内嵌表与 yaml 真源的一致性由 test_config_drift 守卫（改任一侧须两处同步）。
"""
from __future__ import annotations

import os
from pathlib import Path

EMBEDDED_AUDIO = {
    "root": "music",
    "bgm": {   # 情绪 → 子目录 + 提示词/情绪关键词（选曲兜底匹配，含 profile 名与中英常见词）
        "calm": {"dir": "bgm/calm",
                 "keywords": ["calm", "治愈", "舒缓", "温柔", "安静", "轻松",
                              "quote", "storybook", "绘本", "语录"]},
        "upbeat": {"dir": "bgm/upbeat",
                   "keywords": ["upbeat", "欢快", "活力", "轻快", "happy",
                                "知识", "解说", "explainer"]},
        "cinematic": {"dir": "bgm/cinematic",
                      "keywords": ["cinematic", "史诗", "电影", "宏大", "hd2d",
                                   "游戏", "故事", "gba", "snes", "dark"]},
        "ambient": {"dir": "bgm/ambient",
                    "keywords": ["ambient", "氛围", "环境", "空灵", "scene",
                                 "雨", "雪", "雾"]},
    },
    "sfx": {   # 分类 → 语义键 → 文件（相对库根）
        "transitions": {
            "whoosh": "sfx/transitions/whoosh.wav",
            "riser": "sfx/transitions/riser.wav",
            "boom": "sfx/transitions/boom.wav",
            "swish": "sfx/transitions/swish.wav",
            "deep": "sfx/transitions/deep.wav",
            "glitch": "sfx/transitions/glitch.wav",
            "shimmer": "sfx/transitions/shimmer.wav",
            "pop": "sfx/transitions/pop.wav",
            "ding": "sfx/transitions/ding.wav",
            "page": "sfx/transitions/page.wav",
            "paper": "sfx/transitions/paper.wav",
            "impact": "sfx/transitions/impact.wav",
            "slash": "sfx/transitions/slash.wav",
            "heartbeat": "sfx/transitions/heartbeat.wav",
            "wind": "sfx/transitions/wind.wav",
            "magic": "sfx/transitions/magic.wav",
            "clock": "sfx/transitions/clock.wav",
            "camera": "sfx/transitions/camera.wav",
        },
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]   # kinema/ → engine/ → 仓库根


def load_registry() -> dict:
    """读 config/audio.yaml；缺文件/缺 PyYAML/坏文件一律回落内嵌表，绝不炸合成。"""
    env = os.environ.get("KINEMA_AUDIO_CONFIG")
    cands = [Path(env)] if env else [_repo_root() / "config" / "audio.yaml",
                                     Path.cwd() / "config" / "audio.yaml"]
    for p in cands:
        if p.is_file():
            try:
                import yaml
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if data:
                    return data
            except Exception:   # noqa: BLE001
                break
    return dict(EMBEDDED_AUDIO)


def library_root(reg: dict | None = None, store=None) -> Path:
    """音频库根发现：env KINEMA_MUSIC_DIR > store（models.yaml 同级推导）>
    仓库根/<root> > cwd 候选。root 字段缺省 music。"""
    env = os.environ.get("KINEMA_MUSIC_DIR")
    if env:
        return Path(env)
    rel = Path((reg if reg is not None else load_registry()).get("root") or "music")
    if rel.is_absolute():
        return rel
    cands = []
    if store is not None and getattr(store, "source", None):
        src = Path(store.source)
        if src.exists():
            cands.append(src.resolve().parent.parent / rel)   # config/ 的上级 = 仓库根
    cands += [_repo_root() / rel, Path.cwd() / rel, Path.cwd().parent / rel]
    for c in cands:
        if c.is_dir():
            return c
    return cands[0]


def bgm_track_count(reg: dict | None = None, store=None) -> int:
    """BGM 子库在盘的曲目数（**只数曲子，不数音效**）。

    给合成前的 BGM 闸判「本机有没有曲库」：`local` provider 在库空时会退化成合成
    正弦氛围床并烧进成片，那是明显的机器音，不该等交付时才被发现。
    口径与 `LocalMusicProvider._pick` 一致——只认 mp3、认 `bgm/` 下的任意层级
    （库内乱放同样兜住）；那边挑得到的这里就数得到，两处判据分叉会出现
    「闸说库是空的、provider 却挑出了曲子」。"""
    root = library_root(reg, store)
    if not root or not root.is_dir():
        return 0
    base = root / "bgm"
    return len(list(base.rglob("*.mp3")) if base.is_dir() else list(root.rglob("*.mp3")))
