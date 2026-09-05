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

"""分镜审阅状态机 —— 人是主管，AI 是艺术家。

对象：每个分镜 × 每类产物（image 分镜图 / audio 配音 / clip 动态片段），
外加整镜级 shot（弃用/恢复）与**章节级产物**（animatic 全片样片）。
状态五态 + 弃用：

    todo → wip → wfa(待审) → done(通过)
                    ↓ ↑
                  retake(重做)          omt(弃用，仅整镜级)

语义用布尔标志驱动，不硬编码状态名：
    is_done             通过 → 产物锁定，引擎跳过重生（--force 也不覆盖，防烧钱）
    is_retake           重做 → 下次运行该阶段时强制重生（相当于该镜的 force）
    is_feedback_request 待审 → 引擎每次生成完成后自动落此态，等人表态
    is_omitted          弃用 → 整镜不进时间轴/字幕/成片

数据落在章节 JSON 的 shots[].review：
    "review": {
      "shot":  {"state": "omt",    "note": "节奏太拖", "at": "..."},
      "image": {"state": "done",   "at": "..."},
      "clip":  {"state": "retake", "note": "第3秒左手穿模", "at": "..."}
    }
retake 的 note 是结构化反馈：会把它编译进该镜下一版提示词。
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache

# 可审产物阶段；"shot" 为整镜级（弃用/恢复），不参与产物锁定
STAGES = ("image", "audio", "clip")
# 章节级可审产物：审阅数据挂在章节 JSON 顶层 review（草稿两段式）
CHAPTER_STAGES = ("animatic",)

STATES = {
    "todo":   {"label": "待办"},
    "wip":    {"label": "生成中"},
    "wfa":    {"label": "待审", "is_feedback_request": True},
    "retake": {"label": "重做", "is_retake": True},
    "done":   {"label": "通过", "is_done": True},
    "omt":    {"label": "弃用", "is_omitted": True},
}

# 镜级作者字段 → 改动会让哪些阶段的产物与文档对不上（**全仓单一真源**）。
# 两个消费者按相反方向读同一张表：`batch.apply` 要「这个字段该标哪几个阶段
# retake」，`agent_gateway` 要「这个阶段的产物由哪些字段决定，改了要不要拦
# 已锁定的镜」。各维护一份的后果实测已经发生过——`negative_prompt` 在一处只
# 算 image（而它同样拼进 video_prompt）、`narration` 在一处只算 audio（而 native
# 把它写进视频提示词、dubbed 的 ref_audio 由它合成）、`entry_state` 在一处整个
# 漏登记，于是批量改完这三个字段，已生成的片段既不重做也不告警。
#
# 空元组 = 只影响后置合成物（字幕由 compose 每次重新编译，转场是渲染期参数）或
# 只供 lint/看板消费，没有需要重生的付费产物。
#
# 契约白名单（agent/contracts.json shot_fields）里的每个镜级字段都在此登记，
# 守卫钉住包含关系：若白名单扩面而失效表不动，Gateway 改了字段既不置 retake 也不受锁。
STAGE_FIELDS: dict[str, tuple[str, ...]] = {
    # 画面描述
    "image_prompt": ("image",), "image_prompt_en": ("image",),
    "refs": ("image",),                       # 镜级垫图集合是生图输入
    "framing": ("image",), "angle": ("image",), "lens": ("image",),
    "lighting": ("image",),
    # 运动描述与 delta 骨架：缺 video_prompt 时由 prompts.video_prompt 拼进提示词
    "video_prompt": ("clip",), "video_prompt_en": ("clip",),
    "action": ("clip",), "camera": ("clip",),
    "entry_state": ("clip",), "end_state": ("clip",), "light_shift": ("clip",),
    "sfx": ("clip",), "guide": ("clip",), "sketch": ("clip",),
    # 两侧都吃：负面串同时进生图与视频提示词；设定引用与画风档决定两边的参考图
    "negative_prompt": ("image", "clip"),
    "characters": ("image", "clip"), "props": ("image", "clip"),
    "scenes": ("image", "clip"), "profile": ("image", "clip"),
    # 人声侧：native 把台词写进视频提示词，dubbed 的 ref_audio 由这段文本合成，
    # 故改台词不只是重跑 TTS
    "narration": ("audio", "clip"), "lines": ("audio", "clip"),
    "speaker": ("audio", "clip"), "voice": ("audio", "clip"),
    "emotion": ("audio", "clip"), "emotion_scale": ("audio", "clip"),
    "voice_instruction": ("audio", "clip"), "delivery": ("audio", "clip"),
    "dur": ("audio", "clip"),
    # 镜级请求形态开关：首帧锚定与镜级结对衔接改变视频请求的槽位
    "anchor_frame": ("clip",), "frame_chain": ("clip",),
    # 后置合成物（字幕/角标/榜单排版）
    "caption": (), "caption_en": (), "narration_en": (), "dialogue": (),
    "attribution": (), "corner_note": (), "title": (), "rank": (), "bubble_pos": (),
    "transition": (),
    # 规划与看板字段，引擎不据此生成
    "shot_intent": (), "narrative_role": (), "hero_moment": (), "priority": (),
    # 近景人脸预判：只决定 gen-video 的路线起点（写实档 closeup 直接从板驱动
    # 起步），不使任何已产出物过期——被拒的镜没有片段，能出片的说明没被拒
    "face_visibility": (),
}

# 章级字段 → 受影响阶段（与 STAGE_FIELDS 同制度；Gateway、Studio 章级开关与
# `chapter set` 的锁判定，以及 Gateway 的章级 retake 传播都按此表）：
#   image：画风、场景与画风档改变每一镜的生图输入；
#   audio：渲染档、音频制式与语速决定旁白轨是否成立及其内容；
#   clip：渲染档、参考视频、衔接、尾帧接力、首帧锚定、音色锚定、画风档与视频
#         provider 改变请求形态，混烧开关改变旁白镜是开口稿还是闭声稿。
CHAPTER_STAGE_FIELDS: dict[str, frozenset[str]] = {
    "image": frozenset({"scene", "style_prompt", "style_prompt_en", "profile"}),
    "audio": frozenset({"motion", "audio_mode", "speech_rate"}),
    "clip": frozenset({"motion", "previz_v2v", "control_video", "tail_relay",
                       "anchor_frame", "native_voiceover", "frame_chain",
                       "voice_anchor", "profile", "video_provider"}),
}


def chapter_locked(shots: list, fields) -> list[str]:
    """章级字段改动会撞上的已锁定阶段（空表=可改）。"""
    changed = set(fields)
    return [stage for stage, owned in CHAPTER_STAGE_FIELDS.items()
            if changed & owned
            and any(is_locked(s, stage) for s in shots if isinstance(s, dict))]


_PRODUCT_FIELD = {"image": ("image", "images"), "audio": ("audio_file", None),
                  "clip": ("clip", "clips")}


def retake_produced(shot: dict, stages, *, note: str | None = None) -> list[str]:
    """字段改动后的失效传播：已产出且未锁定、未在重做的阶段置 retake，返回置位的阶段。
    没有产物的阶段不动——下次生成本就会产出；锁定阶段在校验期已拒绝。"""
    from .pipeline.checkpoint import has_file
    out: list[str] = []
    for stage in stages:
        if stage not in STAGES:
            continue
        main, many = _PRODUCT_FIELD[stage]
        produced = has_file(shot.get(main)) or bool(many and shot.get(many))
        if not produced or is_locked(shot, stage) or needs_retake(shot, stage):
            continue
        set_state(shot, stage, "retake", note=note)
        out.append(stage)
    return out


def stages_for(field: str) -> tuple[str, ...]:
    """改这个字段会失效的产物阶段；未登记的字段返回空元组。"""
    return STAGE_FIELDS.get(field, ())


@lru_cache(maxsize=None)
def fields_for(stage: str) -> frozenset[str]:
    """这个阶段的产物由哪些镜级字段决定（`STAGE_FIELDS` 的反向视图）。

    结果缓存：Gateway 每校验一条 update 就要按三个阶段各取一次反向集，而
    `STAGE_FIELDS` 是模块级常量，逐次重扫只是白算。返回 frozenset，调用方
    拿不到可变引用。"""
    return frozenset(f for f, st in STAGE_FIELDS.items() if stage in st)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _flag(state: str, flag: str) -> bool:
    return bool(STATES.get(state, {}).get(flag))


def label(state: str) -> str:
    return STATES.get(state, {}).get("label", state)


# ---- 读 ----
def _entry(shot: dict, stage: str) -> dict:
    """审阅条目；块或条目不是对象时视为未表态（文档被手改坏的形态在读侧归一，
    消费方不各自兜底）。"""
    rv = shot.get("review")
    entry = rv.get(stage) if isinstance(rv, dict) else None
    return entry if isinstance(entry, dict) else {}


def get_state(shot: dict, stage: str) -> str:
    return _entry(shot, stage).get("state", "todo")


def get_note(shot: dict, stage: str) -> str | None:
    return _entry(shot, stage).get("note")


def is_locked(shot: dict, stage: str) -> bool:
    """产物已通过 → 锁定，任何重生（含 --force）都不得覆盖。"""
    return _flag(get_state(shot, stage), "is_done")


def needs_retake(shot: dict, stage: str) -> bool:
    """被打回重做 → 下次该阶段强制重生。"""
    return _flag(get_state(shot, stage), "is_retake")


def is_omitted(shot: dict) -> bool:
    """整镜弃用（不进时间轴/字幕/成片，各阶段跳过）。"""
    return _flag(get_state(shot, "shot"), "is_omitted")


# ---- 写 ----
def set_state(shot: dict, stage: str, state: str, *, note: str | None = None) -> None:
    if state not in STATES:
        raise ValueError(f"未知审阅状态: {state}（可选: {', '.join(STATES)}）")
    if stage != "shot" and stage not in STAGES and stage not in CHAPTER_STAGES:
        raise ValueError(f"未知审阅阶段: {stage}"
                         f"（可选: shot, {', '.join(STAGES + CHAPTER_STAGES)}）")
    entry = {"state": state, "at": _now()}
    if note:
        entry["note"] = note
    elif get_note(shot, stage) and state == "retake":
        entry["note"] = get_note(shot, stage)      # 未给新意见时保留旧的重做意见
    shot.setdefault("review", {})[stage] = entry
    # 通过即消费批注：该阶段的锚定意见使命已尽（重生时已编译进提示词、
    # 版本栈 reason 留有全文存证）——不留到新版产物上误导下一轮审阅
    if state == "done" and stage in STAGES and shot.get("comments"):
        kept = [c for c in shot["comments"] if c.get("stage", "image") != stage]
        if len(kept) != len(shot["comments"]):
            shot["comments"] = kept


def mark_generated(shot: dict, stage: str) -> None:
    """引擎生成完成 → 自动落「待审」，等人表态（审阅闭环的入口）。"""
    set_state(shot, stage, "wfa")


def summary(shots: list[dict], *, audio_of=None) -> dict:
    """按阶段统计各状态数量（弃用镜单列），供 CLI/大屏仪表。

    `audio_of(shot)` 回答本镜有没有 audio 产物（`voicecast.has_audio_stage`）：
    native 对白镜与无词镜没有旁白 wav，计进 audio 就是一条永远关不掉的待办。"""
    out: dict = {s: {} for s in STAGES}
    out["omitted"] = sum(1 for s in shots if is_omitted(s))
    for s in shots:
        if is_omitted(s) or s.get("kind") == "transition":   # 转场镜零产物，不进审阅统计
            continue
        for stage in STAGES:
            if stage == "audio" and audio_of is not None and not audio_of(s):
                continue
            st = get_state(s, stage)
            out[stage][st] = out[stage].get(st, 0) + 1
    return out
