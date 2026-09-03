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

"""产物版本栈。

原则：**重生成不覆盖**。分镜的每类产物（image/audio/clip）重新生成前，旧产物先归档进
`<workdir>/versions/`，版本号自增；画布路径（shots[].image 等「当前版」路径）保持稳定，
下游管线（合成/字幕/Studio）零改动。回滚 = 把某历史版拷回画布路径（归档不可变），
并把当前版先归档——版本号只增不减，谱系完整可审计。

登记落在章节 JSON 的 shots[].versions[stage]（列表，v 升序）：
    {"v": 1, "files": {"main": "...", "9:16": "..."}, "reason": "retake: 左手穿模",
     "params": {"prompt": "...", "seed": 123, "cost": 0.3}, "at": "..."}

当前版本号 = 归档数 + 1（首次生成即 v1，无归档条目）。
"""
from __future__ import annotations

import copy
import shutil
from datetime import datetime
from pathlib import Path

from ..errors import ProjectError

STAGES = ("image", "audio", "clip")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def history(shot: dict, stage: str) -> list[dict]:
    return list((shot.get("versions") or {}).get(stage) or [])


def current_version(shot: dict, stage: str) -> int:
    return len(history(shot, stage)) + 1


def _current_files(shot: dict, stage: str) -> dict[str, str]:
    """当前版的画布路径集合：main = 主产物；其余键 = 逐比例。
    字段已上云（URL）时先拉回本地——归档对象必须是本地文件。

    **按路径值去重（main 优先）**：引擎的回填约定是主字段恒取自逐比例字典
    （`s["clip"] = clips[主比例]`，per-aspect 生图同构），同一个文件会以两个键
    出现在这里。不去重的话 archive 对它 move 两次，第二次源已不存在，
    崩在版本条目落账之前——版本栈什么都没记，rollback 无从捞回。
    去重后同值字段的内容经 main 键一并归档与恢复，字段本身不受影响。"""
    from ..storage.media import ensure_local
    files: dict[str, str] = {}
    if stage == "image":
        if shot.get("image"):
            files["main"] = shot["image"]
        for asp, p in (shot.get("images") or {}).items():
            files[asp] = p
    elif stage == "clip":
        if shot.get("clip"):
            files["main"] = shot["clip"]
        for asp, p in (shot.get("clips") or {}).items():
            files[asp] = p
    elif stage == "audio":
        if shot.get("audio_file"):
            files["main"] = shot["audio_file"]
    files = {k: ensure_local(v) for k, v in files.items() if v}
    files = {k: v for k, v in files.items() if Path(v).is_file()}
    out: dict[str, str] = {}
    seen: set[str] = set()
    for k, v in files.items():          # 插入序 main 在前 → 同值路径由 main 持有
        if v not in seen:
            seen.add(v)
            out[k] = v
    return out


def archive(project, shot: dict, stage: str, *,
            reason: str = "", params: dict | None = None) -> int | None:
    """把该镜该阶段的**当前产物**移入 versions/ 归档，登记并返回归档版本号。

    无现存产物（首次生成）时返回 None、不产生条目。调用点：重生成之前。
    """
    files = _current_files(shot, stage)
    if not files:
        return None
    vdir = project.subdir("versions")
    v = current_version(shot, stage)
    stored: dict[str, str] = {}
    for key, src in files.items():
        src = Path(src)
        tag = "" if key == "main" else f"_{key.replace(':', 'x')}"
        dst = vdir / f"shot_{shot.get('id')}_{stage}{tag}_v{v:03d}{src.suffix}"
        shutil.move(str(src), dst)                 # 移动而非复制：磁盘零冗余
        # **落绝对路径**：归档条目会被后来的回滚按原样取用，而那时的工作目录未必还是
        # 现在这个（Studio 线程、spawn 出去的子进程、从别处调起的 CLI 各有各的 cwd）。
        # 存相对路径的后果是回滚当场 FileNotFoundError，而文件其实好端端躺在盘上。
        stored[key] = str(dst.resolve())
    entry = {"v": v, "files": stored, "at": _now()}
    if reason:
        entry["reason"] = reason
    if params:
        entry["params"] = params
    shot.setdefault("versions", {}).setdefault(stage, []).append(entry)
    return v


_FIELD_BY_STAGE = {"image": ("image", "images"), "clip": ("clip", "clips"),
                   "audio": ("audio_file", None)}


def _drop_field(shot: dict, stage: str, key: str) -> None:
    """把该镜该阶段某个键的画布字段摘除（main=主字段，其余键=逐比例字典项）。"""
    main_field, dict_field = _FIELD_BY_STAGE[stage]
    if key == "main":
        shot.pop(main_field, None)
    elif dict_field:
        (shot.get(dict_field) or {}).pop(key, None)


def rollback(project, shot: dict, stage: str, to_v: int) -> None:
    """回滚到历史版本：当前版先归档（reason=rollback-out），再把 v 的文件**拷回**
    画布路径（归档条目不可变，可反复回滚）。

    目标版没有的键（如后来才加的比例图）：文件已随归档移走，对应画布字段
    一并摘除——否则字段悬挂指向不存在的文件。"""
    entries = {e["v"]: e for e in history(shot, stage)}
    if to_v not in entries:
        raise ProjectError(
            f"镜 {shot.get('id')} 的 {stage} 无版本 v{to_v}"
            f"（现有归档: {sorted(entries) or '无'}，当前=v{current_version(shot, stage)}）")
    target = entries[to_v]
    cur_files = _current_files(shot, stage)
    if not cur_files:
        raise ProjectError(f"镜 {shot.get('id')} 的 {stage} 当前无产物，无从回滚")
    # 目标文件先解析（历史条目可能已被 oss sync 改写为 URL）并校验齐全，
    # 之后才归档当前版——顺序相反时，目标缺失的回滚会先把当前版移走，
    # 画布落空且归档条目来不及落盘
    from ..storage.media import ensure_local
    resolved: dict[str, str] = {}
    for key in cur_files:
        src = target["files"].get(key)
        if not src:
            continue                              # 目标版无此键 → 归档后摘除字段
        src = ensure_local(src)
        if not Path(src).is_file():
            raise FileNotFoundError(f"归档文件丢失: {src}")
        resolved[key] = src
    # 当前版出库归档（reason 记录谱系：此后画布内容 = to_v），生成参数随之入栈，
    # 再滚回来时 gen 快照才有来源；归档条目本身不可变
    gen = shot.get("gen") or {}
    archive(project, shot, stage, reason=f"rollback-out（切换到 v{to_v}）",
            params=gen.get(stage))
    for key, dst in cur_files.items():
        src = resolved.get(key)
        if not src:
            _drop_field(shot, stage, key)         # 目标版无此键 → 字段随文件一起退场
            continue
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)                    # 拷贝而非移动：归档可反复回滚
    # gen 快照描述的是画布上那一版：换回目标版的生成参数，目标版没有留痕就摘除
    if target.get("params") is not None:
        shot.setdefault("gen", {})[stage] = copy.deepcopy(target["params"])
    else:
        (shot.get("gen") or {}).pop(stage, None)


# ── 设定图（角色/场景/道具）版本栈 ──────────────────────────────────────
# 与分镜版本栈同制，但设定图是**单图、无 stage 分层**：登记落扁平列表 holder[versions_key]
# （角色/道具=实体 dict 的 "versions"；场景=系列文档的 "scene_ref_versions"）。标准字段
# （sheet / scene_ref）路径稳定，重生成前旧图移入 <assets/refs/versions/> 归档、版本号自增；
# 回滚=当前版先归档、把某历史版**拷回**标准路径（归档不可变，可反复回滚）。

def asset_history(holder: dict, versions_key: str = "versions") -> list[dict]:
    return list(holder.get(versions_key) or [])


def asset_current_version(holder: dict, versions_key: str = "versions") -> int:
    return len(asset_history(holder, versions_key)) + 1


def archive_asset(holder: dict, *, media_key: str, versions_key: str, vdir,
                  label: str, reason: str = "", params: dict | None = None) -> str | None:
    """把设定图当前产物移入 vdir 归档、登记进 holder[versions_key]，返回归档路径。

    无现存产物（首次生成）返 None、不产生条目。**标准字段 holder[media_key] 字符串保持不变**
    ——调用方随后重生/回滚会把新内容写回同一路径（与分镜 archive 同一约定）。"""
    from ..storage.media import ensure_local
    cur = holder.get(media_key)
    cur = ensure_local(cur) if cur else None
    if not cur or not Path(cur).is_file():
        return None
    hist = holder.setdefault(versions_key, [])
    v = len(hist) + 1
    vdir = Path(vdir)
    vdir.mkdir(parents=True, exist_ok=True)
    dst = vdir / f"{label}_v{v:03d}{Path(cur).suffix}"
    shutil.move(str(cur), str(dst))               # 移动而非复制：磁盘零冗余
    entry = {"v": v, "file": str(dst.resolve()), "at": _now()}   # 绝对路径，理由见 archive()
    if reason:
        entry["reason"] = reason
    if params:
        entry["params"] = params
    hist.append(entry)
    return str(dst)


def rollback_asset(holder: dict, *, media_key: str, versions_key: str, vdir,
                   label: str, to_v: int) -> str:
    """回滚设定图到 to_v：当前版先归档（reason=rollback-out），把 to_v 文件**拷回**标准路径。

    返回标准本地路径（内容=to_v）。归档条目不可变，可反复回滚。"""
    from ..storage.media import ensure_local
    entries = {e["v"]: e for e in asset_history(holder, versions_key)}
    if to_v not in entries:
        raise ProjectError(f"无版本 v{to_v}（现有归档: {sorted(entries) or '无'}，"
                         f"当前=v{asset_current_version(holder, versions_key)}）")
    std = holder.get(media_key)
    std = ensure_local(std) if std else None
    if not std or not Path(std).is_file():
        raise ProjectError("该设定图当前无产物，无从回滚")
    # 归档条目可能已被 oss sync 改写为 URL——读侧与标准字段同样先落地
    tgt = ensure_local(entries[to_v]["file"])
    if not Path(tgt).is_file():
        raise FileNotFoundError(f"归档文件丢失: {tgt}")
    # 当前版出库归档（此后标准路径内容 = to_v）
    archive_asset(holder, media_key=media_key, versions_key=versions_key, vdir=vdir,
                  label=label, reason=f"rollback-out（切换到 v{to_v}）")
    shutil.copy2(tgt, std)                         # 拷回标准路径
    holder[media_key] = std                        # 字段回到本地标准路径（曾是 URL 则落地）
    return std


# ---------------------------------------------------------------- 成片版本栈
# 成片与分镜产物的差别只有两点：它落在**章节文档顶层**（不属于任何一镜）、且**逐比例
# 各一条谱系**（16:9 与 9:16 是两支不同的片子，混在一张表里没法回滚）。其余（归档即移动、
# 回滚是互换、条目不可变）与设定图那套完全一致，故直接复用 `archive_asset`/`rollback_asset`，
# 只在这里把「逐比例」这层壳补上。
_OUTPUT_VERSIONS = "output_versions"


def _output_holder(project, aspect: str) -> dict:
    """把「顶层 output[比例] + 顶层 output_versions[比例]」包成资产级助手认识的 holder。

    `versions` 放的是**真实列表对象**（不是副本）——助手 append 的条目要直接落进文档，
    拷一份回来再合并就是给"两处各记一半"开口子。
    """
    hist = project.data.setdefault(_OUTPUT_VERSIONS, {}).setdefault(aspect, [])
    # 直读 `data["output"]` 而不是 `Project.output` 属性：本模块对宿主只要求"有 data 和
    # subdir"，属性依赖会把版本栈绑死在 Project 这一个实现上（离线测试与替身宿主都用不了）
    return {"file": (project.data.get("output") or {}).get(aspect), "versions": hist}


def output_history(project, aspect: str) -> list[dict]:
    return list((project.data.get(_OUTPUT_VERSIONS) or {}).get(aspect) or [])


def output_current_version(project, aspect: str) -> int:
    return len(output_history(project, aspect)) + 1


# ------------------------------------------------- 音频剧本段版本栈（scored 路线）
# 粒度是**段**而不是整轨：`score --only N` 就是按段重生成的，「这次演绎和上次哪个好」
# 也只能按段比。整轨是各段拼出来的派生物——切完段重拼一次即可，它自己不需要谱系。
# 生成式模型每次演绎都不同，所以这里的版本栈不是"防手滑"而是**创作工具**：
# 同一段剧本连出三版挑一版，与设定图宫格候选是同一件事。
_SCORE_SEGS = ("gen", "score", "segments")


def score_segments(project) -> list[dict]:
    """`gen.score.segments[]` 的真实列表对象（不存在则建空表）。

    返回真列表而非副本——版本栈助手 append 的条目要直接落进文档。"""
    node = project.data
    for k in _SCORE_SEGS[:-1]:
        node = node.setdefault(k, {})
    return node.setdefault(_SCORE_SEGS[-1], [])


def score_segment(project, no: int) -> dict | None:
    return next((e for e in score_segments(project) if int(e.get("no") or 0) == int(no)), None)


def archive_score_segment(project, no: int, *, reason: str = "") -> str | None:
    """重生成某段**之前**把现存那一版移进版本栈；没有现存音频返回 None。

    与成片同一纪律：生成写的是同一个路径，等它跑完再归档，归进去的已经是新的那版。"""
    seg = score_segment(project, no)
    if not seg:
        return None
    return archive_asset(seg, media_key="file", versions_key="versions",
                         vdir=project.subdir("versions"),
                         label=f"score_{int(no):02d}", reason=reason)


def rollback_score_segment(project, no: int, to_v: int) -> str:
    """把某段切回 to_v：当前版先归档、历史版拷回标准路径（**互换而非覆盖**，
    来回切不丢任何一版）。调用方随后必须重拼整轨——段换了而整轨没重拼，
    盘上那条音轨就还是旧的，而页面会显示已切换。"""
    seg = score_segment(project, no)
    if not seg or not seg.get("file"):
        raise ProjectError(f"第 {no} 段当前没有音频，无从切换")
    return rollback_asset(seg, media_key="file", versions_key="versions",
                          vdir=project.subdir("versions"),
                          label=f"score_{int(no):02d}", to_v=int(to_v))


def archive_output(project, aspect: str, *, reason: str = "") -> str | None:
    """把该比例的现存成片移进版本栈，返回归档路径；没有现存成片返回 None。

    **必须在 `compose.build` 之前调**：合成写的是同一个输出路径，等它跑完再归档，
    归进去的已经是新片子、旧片子早被覆盖没了（成片是最贵的产物，覆盖不可逆）。
    """
    from ..project import aspect_tag
    holder = _output_holder(project, aspect)
    if not holder["file"]:
        return None
    dst = archive_asset(holder, media_key="file", versions_key="versions",
                        vdir=project.subdir("versions"),
                        label=f"output_{aspect_tag(aspect)}", reason=reason)
    return dst


def rollback_output(project, aspect: str, to_v: int) -> str:
    """回滚该比例的成片到 to_v：当前版先归档，历史版拷回标准输出路径。

    与分镜回滚同一语义——**互换而非覆盖**，来回切不丢任何一版。
    """
    from ..project import aspect_tag
    holder = _output_holder(project, aspect)
    if not holder["file"]:
        raise ProjectError(f"{aspect} 当前没有成片，无从回滚")
    std = rollback_asset(holder, media_key="file", versions_key="versions",
                         vdir=project.subdir("versions"),
                         label=f"output_{aspect_tag(aspect)}", to_v=int(to_v))
    project.data.setdefault("output", {})[aspect] = std
    return std


def restore_last_output(project, aspect: str) -> str | None:
    """把最近一次 `archive_output` 归档的那一版原样放回标准路径，并撤销该条目。

    归档是**移动**（与全仓库版本栈同一约定，磁盘零冗余），于是「归档完、合成失败」
    这一瞬间标准路径是空的。合成失败本就常见（ffmpeg 参数、素材损坏、磁盘满），
    不回填就会留下一个指向不存在文件的 `output[比例]`，而那是水印/校验/交付三条链的
    入口。返回放回去的路径；没有可回填的条目返回 None。
    """
    hist = (project.data.get(_OUTPUT_VERSIONS) or {}).get(aspect) or []
    if not hist:
        return None
    from ..storage.media import ensure_local
    entry = hist[-1]
    # 归档条目可能已被 oss sync 改写为 URL——回填前先落地
    src = Path(ensure_local(entry.get("file") or "") or "")
    std = (project.data.get("output") or {}).get(aspect)
    if not std or not src.is_file():
        return None
    shutil.move(str(src), str(std))
    hist.pop()
    return str(std)
