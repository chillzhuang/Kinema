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

"""框选局部改造 + 设定图候选定稿（画布上的两种"人眼裁决"）。

局部改造是第四种迭代手段：整镜重roll / retake 重生 / 版本回滚之外的"只改这一处"。

画布上框选一块区域 + 一句指令 → 以**当前图为参考图**调用图像模型指令编辑重生
（Seedream 参考图编辑能力）。区域坐标（0~1 相对值）编译成明确的位置语义写进
编辑指令，并强约束"框选之外不得改动"——模型不支持真正的像素级蒙版，位置语义
+ 参考图一致性约束是当前工程上最稳的近似。

两条路径共用同一套安全语义：
  · 分镜图：改前旧版自动归档进版本栈（可回滚），改后落「待审」；已通过锁定的镜拒绝改
    （防烧钱语义与状态机一致——要改先解锁）。
  · 设定图（角色/场景/道具）：改前旧图移入版本栈（assets/refs/versions/·可回滚），改后血缘
    自动传播——引用该设定图的下游分镜标过期（与 gen-refs --force 同一条通路）。
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import lineage, review
from . import sheets
from . import supply
from .errors import KinemaError, ProjectError
from .pipeline import consistency, mediacheck, versioning
from .pipeline import prompts as prompts_mod
from .storage.media import ensure_local

_COLS = ("左", "中", "右")
_ROWS = ("上", "中", "下")


def region_phrase(rect: dict | None) -> str:
    """rect {x,y,w,h}（0~1）→ 位置语义，如「画面右上区域（约占画面 12%）」。"""
    if not rect:
        return "整个画面"
    cx = float(rect.get("x", 0)) + float(rect.get("w", 1)) / 2
    cy = float(rect.get("y", 0)) + float(rect.get("h", 1)) / 2
    col = _COLS[min(2, int(cx * 3))]
    row = _ROWS[min(2, int(cy * 3))]
    pos = "中央" if col == "中" and row == "中" else f"{row}方{col}侧".replace("中方", "正").replace("方中侧", "方中部")
    area = max(1, round(float(rect.get("w", 1)) * float(rect.get("h", 1)) * 100))
    return f"画面{pos}区域（约占画面 {area}%）"


def _edit_prompt(rect: dict | None, instruction: str) -> str:
    return (f"图像编辑：仅修改{region_phrase(rect)}——{instruction}。"
            "其余画面的内容、构图、配色、光线与画风必须与原图保持完全一致，"
            "不得改动框选区域之外的任何细节。")


def refine_shot_image(project, store, router, *, shot_no, rect=None,
                      instruction: str, aspect: str | None = None,
                      no_moodboard: bool = False) -> dict:
    """分镜图局部改造：归档旧版 → 参考原图编辑重生 → 落待审。"""
    instruction = (instruction or "").strip()
    if not instruction:
        raise KinemaError("改造指令不能为空（说清楚这块区域要改成什么）")
    s = next((x for x in project.shots if str(x.get("id")) == str(shot_no)), None)
    if s is None:
        raise KinemaError(f"找不到镜 {shot_no}")
    if review.is_locked(s, "image"):
        raise KinemaError(f"镜 {shot_no} 的分镜图已通过锁定——先置 retake 再改造")
    asp = aspect or project.aspect
    src = project.image_for(s, asp)
    if not src or not Path(src).is_file():
        raise ProjectError(f"镜 {shot_no} 还没有分镜图，先 gen-image")

    prof = s.get("profile") or project.profile
    prov, _params = router.resolve("image", prof)
    prompt = _edit_prompt(rect, instruction)
    w, h = store.canvas(asp)
    from .project import aspect_tag
    out = project.subdir("images") / f"shot_{s['id']}_{aspect_tag(asp)}.png" \
        if (s.get("images") or {}).get(asp) else project.subdir("images") / f"shot_{s['id']}.png"
    refs = [ensure_local(src)]                 # 原图为主参考；参考库垫图随之喂入保风格（G4）
    if not no_moodboard:
        for m in project.moodboard_refs(s):
            if m not in refs:
                refs.append(m)
    # 先产出到临时名：原图在生成期间仍是参考图输入，也是失败时的画布；
    # 成功后再归档旧版并替换，画布字段任何时刻都指向在盘文件
    tmp = out.with_name(f"{out.stem}.refine{out.suffix}")
    res = prov.generate(prompt, str(tmp), ref_images=refs[:8],
                        width=w, height=h, label=f"REFINE SHOT {s['id']}")
    versioning.archive(project, s, "image", reason=f"局部改造：{instruction}",
                       params=(s.get("gen") or {}).get("image"))
    os.replace(res.path, out)
    res.path = str(out)
    if (s.get("images") or {}).get(asp):
        s["images"][asp] = res.path
    if asp == project.aspect or not s.get("image"):
        s["image"] = res.path
    # 血缘快照(refs)必须原样带过来：整块替换会把它冲掉，这一镜从此再不参与过期判定。
    # 局部改造只重画一块矩形、输入侧设定图一张都没重新进过场，所以**留着旧基线、
    # 不清 `stale_refs`**——与素材直供的 `lineage.rebaseline` 分工在此。
    prev_refs = ((s.get("gen") or {}).get("image") or {}).get("refs")
    s.setdefault("gen", {})["image"] = {
        "prompt": prompt, "provider": prov.name, "cost": round(res.cost, 4),
        "refine": {"rect": rect, "instruction": instruction},
        **({"refs": prev_refs} if prev_refs else {}),
        "version": versioning.current_version(s, "image")}
    consistency.invalidate(s, "image")   # 图被改过 → 旧一致性判定作废（判的是改造前那版）
    clip = lineage.retake_clip_for_image(s)
    review.set_state(s, "image", "wfa", note=f"局部改造：{instruction}")
    # 记账放在登记之后且保证落盘：add_cost 超预算是「先入账再抛」，登记排在它
    # 后面会被一并丢掉——钱花了、图在盘上、文档却没这一版
    try:
        project.add_cost("image", res.cost)
    finally:
        project.save()
    return {"shot": s.get("id"), "image": res.path, "cost": res.cost,
            "clip": clip,
            "region": region_phrase(rect),
            "version": versioning.current_version(s, "image")}


def _asset_target(series, kind: str, name: str | None):
    """定位设定图文件与回写位置。kind: character / scene / prop。"""
    if kind == "character":
        c = next((x for x in series.characters if x.get("name") == name), None)
        if not c:
            raise KinemaError(f"找不到角色 {name}")
        return c.get("sheet"), lambda p: c.__setitem__("sheet", p)
    if kind == "scene":
        if name:                       # 具名取景地（scenes[]）——无名才是全局固定场景
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            if not sc:
                raise KinemaError(f"找不到场景 {name}")
            return sc.get("sheet"), lambda p: sc.__setitem__("sheet", p)
        return series.data.get("scene_ref"), \
            lambda p: series.data.__setitem__("scene_ref", p)
    if kind == "prop":
        p0 = next((x for x in series.props if x.get("name") == name), None)
        if not p0:
            raise KinemaError(f"找不到道具 {name}")
        return p0.get("sheet"), lambda p: p0.__setitem__("sheet", p)
    if kind in ("expression", "pose"):    # 扩展设定图：角色实体上的独立字段
        c = next((x for x in series.characters if x.get("name") == name), None)
        if not c:
            raise KinemaError(f"找不到角色 {name}")
        field = "expression_sheet" if kind == "expression" else "pose_sheet"
        return c.get(field), lambda p: c.__setitem__(field, p)
    if kind == "topview":                 # 场景俯视布局图（具名/全局与 scene 同构）
        if name:
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            if not sc:
                raise KinemaError(f"找不到场景 {name}")
            return sc.get("topview_sheet"), lambda p: sc.__setitem__("topview_sheet", p)
        return series.data.get("scene_topview_ref"), \
            lambda p: series.data.__setitem__("scene_topview_ref", p)
    raise KinemaError(f"未知资产类型: {kind}"
                      "（可选: character / scene / prop / expression / pose / topview）")


def _asset_holder(series, kind: str, name: str | None):
    """取该设定图对应的实体 dict——`sheets.rules_for` 要据此决定版式里的可变项
    （角色/pose 点不点名武器）。场景基准图与俯视图无可变项，返回空 dict。"""
    if kind in ("character", "expression", "pose"):   # pose 的版式规则要按武器点名
        return next((x for x in series.characters if x.get("name") == name), {}) or {}, None
    if kind == "prop":
        return next((x for x in series.props if x.get("name") == name), {}) or {}, None
    return {}, None


def _asset_refs(series, kind: str, name: str | None):
    """该设定图的逐张垫图选择（None=默认生效集 / [] =不用 / [路径…]=精确）——
    与 gen-refs 的 c.get('refs')/scene_refs 同源，refine 也据此各自挑各自的垫图。"""
    if kind in ("character", "expression", "pose"):
        c = next((x for x in series.characters if x.get("name") == name), None)
        return c.get("refs") if c else None
    if kind == "prop":
        p0 = next((x for x in series.props if x.get("name") == name), None)
        return p0.get("refs") if p0 else None
    if kind in ("scene", "topview"):
        if name:
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            return (sc or {}).get("refs")
        return series.data.get("scene_refs")
    return None


def _propagate(series) -> tuple[int, int]:
    """设定图内容变化 → 全部章节血缘传播（与 gen-refs --force 同一条通路）。

    章节副本的设定字段对齐走 `sync_design_to_chapters` 的白名单（角色/道具/
    具名场景与两张全局场景图共用同一份，别在这里再抄一份字段清单）；对齐后
    逐章按内容指纹标过期。`sheet_origin` 的空值单独补齐：白名单同步只覆盖有值
    字段，而该字段以缺失表达「来源无记录」——章节里留一份旧值，等于替一张
    来历不明的图背书受信。"""
    from .project import Project
    origin_of = {c.get("name"): c.get("sheet_origin") for c in series.characters}
    series.sync_design_to_chapters()
    retaken = flagged = 0
    for ch in series.chapters:
        cid = ch.get("id")
        with series.chapter_write(cid):
            data = series.ws.store.load_chapter(series.pid, cid)
            if not data:
                continue
            cleared = False
            for cc in data.get("characters") or []:
                name = cc.get("name")
                if name in origin_of and origin_of[name] is None \
                        and cc.get("sheet_origin"):
                    cc.pop("sheet_origin", None)
                    cleared = True
            if cleared:
                series.ws.store.save_chapter(series.pid, cid, data)
            chp = Project.load(series.get_chapter_path(cid))
            r, f = lineage.mark_stale(chp)
            if r or f:
                chp.save()
                retaken += r
                flagged += f
    return retaken, flagged


def _asset_version_ctx(series, kind: str, name: str | None):
    """设定图版本栈上下文 → (holder, media_key, versions_key, vdir)。
    角色/道具=实体 dict 的 sheet + versions；场景=系列文档 scene_ref + scene_ref_versions。"""
    vdir = series.refs_dir / "versions"
    if kind == "character":
        ent = next((x for x in series.characters if x.get("name") == name), None)
        if not ent:
            raise KinemaError(f"找不到角色 {name}")
        return ent, "sheet", "versions", vdir
    if kind == "prop":
        p0 = next((x for x in series.props if x.get("name") == name), None)
        if not p0:
            raise KinemaError(f"找不到道具 {name}")
        return p0, "sheet", "versions", vdir
    if kind == "scene":
        if name:                       # 具名取景地与道具同构：实体 dict 的 sheet + versions
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            if not sc:
                raise KinemaError(f"找不到场景 {name}")
            return sc, "sheet", "versions", vdir
        return series.data, "scene_ref", "scene_ref_versions", vdir
    if kind in ("expression", "pose"):   # 扩展设定图：独立字段 + 独立版本栈键
        ent = next((x for x in series.characters if x.get("name") == name), None)
        if not ent:
            raise KinemaError(f"找不到角色 {name}")
        return (ent, "expression_sheet", "expression_versions", vdir) \
            if kind == "expression" else (ent, "pose_sheet", "pose_versions", vdir)
    if kind == "topview":
        if name:
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            if not sc:
                raise KinemaError(f"找不到场景 {name}")
            return sc, "topview_sheet", "topview_versions", vdir
        return series.data, "scene_topview_ref", "scene_topview_versions", vdir
    raise KinemaError(f"未知资产类型: {kind}"
                      "（可选: character / scene / prop / expression / pose / topview）")


def _version_label(holder: dict, media_key: str, kind: str, name: str | None) -> str:
    cur = holder.get(media_key)
    return Path(cur).stem if cur else f"{kind}_{name or 'scene'}"


def archive_asset_sheet(series, kind: str, name: str | None = None, *,
                        reason: str = "", params: dict | None = None) -> str | None:
    """重生成/改造前归档设定图当前版进版本栈，返回归档路径（无现存产物返 None）。

    角色档把 `sheet_origin` 记进版本条目：回滚要能恢复那一版的生成方式，
    否则回到受信的 t2i 旧版后仍顶着 i2i 标记、被迫白花一次重出。"""
    holder, mk, vk, vdir = _asset_version_ctx(series, kind, name)
    if kind == "character" and holder.get("sheet_origin"):
        params = {**(params or {}), "sheet_origin": holder["sheet_origin"]}
    return versioning.archive_asset(
        holder, media_key=mk, versions_key=vk, vdir=vdir,
        label=_version_label(holder, mk, kind, name), reason=reason, params=params)


def rollback_asset_sheet(series, kind: str, name: str | None, to_v: int) -> tuple[int, int]:
    """回滚设定图到 to_v（当前版先归档）+ 血缘传播（下游分镜标过期）。返回 (retaken, flagged)。"""
    holder, mk, vk, vdir = _asset_version_ctx(series, kind, name)
    to_origin = next((((e.get("params") or {}).get("sheet_origin"))
                      for e in versioning.asset_history(holder, vk)
                      if e.get("v") == int(to_v)), None)
    cur_origin = holder.get("sheet_origin") if kind == "character" else None
    versioning.rollback_asset(holder, media_key=mk, versions_key=vk, vdir=vdir,
                              label=_version_label(holder, mk, kind, name), to_v=int(to_v))
    if kind == "character":
        # rollback-out 条目由 versioning 直接落，这里补记出库版的来源；标准字段
        # 切到 to_v 的来源（条目没记 = 早于该字段的存量，视为未知）
        if cur_origin:
            # rollback_asset 刚归档过当前版，出库条目必在栈尾（空栈不可能到这里）
            holder[vk][-1].setdefault("params", {})["sheet_origin"] = cur_origin
        if to_origin:
            holder["sheet_origin"] = to_origin
        else:
            holder.pop("sheet_origin", None)
    series.save()
    return _propagate(series)


def pick_asset_candidate(ws, pid, *, kind, name=None, no) -> dict:
    """设定图宫格点选定稿：候选 N → 标准路径落位（旧稿备份），血缘传播。

    候选文件保留，随时可换选——换选走同一条路（再备份、再传播）。"""
    series = ws.get_project(pid)
    if kind == "character":
        ent = next((x for x in series.characters if x.get("name") == name), None)
        if ent is None:
            raise KinemaError(f"找不到角色 {name}")
        cands, set_std = ent.get("sheet_candidates") or [], \
            lambda p: ent.update({"sheet": p, "sheet_picked": int(no)})
    elif kind == "scene" and name:
        ent = next((x for x in series.scenes if x.get("name") == name), None)
        if ent is None:
            raise KinemaError(f"找不到场景 {name}")
        cands, set_std = ent.get("sheet_candidates") or [], \
            lambda p: ent.update({"sheet": p, "sheet_picked": int(no)})
    elif kind == "scene":
        ent = series.data
        cands = ent.get("scene_ref_candidates") or []
        set_std = lambda p: ent.update({"scene_ref": p, "scene_ref_picked": int(no)})  # noqa: E731
    elif kind == "prop":
        ent = next((x for x in series.props if x.get("name") == name), None)
        if ent is None:
            raise KinemaError(f"找不到道具 {name}")
        cands, set_std = ent.get("sheet_candidates") or [], \
            lambda p: ent.update({"sheet": p, "sheet_picked": int(no)})
    else:
        raise KinemaError(f"未知资产类型: {kind}（可选: character / scene / prop）")
    if not cands:
        raise KinemaError(f"该{kind}没有候选设定图（先 project refs --candidates N）")
    if not (1 <= int(no) <= len(cands)):
        raise KinemaError(f"候选编号超界: {no}（共 {len(cands)} 张）")
    src = Path(ensure_local(cands[int(no) - 1]))
    if not src.is_file():
        raise ProjectError(f"候选文件缺失: {src}")
    # 候选名 cand_<kind>_<名>_<k> → 标准名 <kind>_<名>（scene 为 scene.png）
    std = src.with_name(re.sub(r"^cand_", "", src.stem).rsplit("_", 1)[0] + src.suffix)
    if std.is_file():
        archive_asset_sheet(series, kind, name, reason=f"候选换选 → 第 {no} 张")
    shutil.copy2(src, std)
    set_std(str(std))
    if kind == "character":
        # 候选批整批同一来源（生成时记在 sheet_candidates_origin），换选即转正；
        # 早于该字段的存量候选没有记录，按未知处理
        batch_origin = ent.get("sheet_candidates_origin")
        if batch_origin:
            ent["sheet_origin"] = batch_origin
        else:
            ent.pop("sheet_origin", None)
    series.save()
    retaken, flagged = _propagate(series)
    return {"kind": kind, "name": name, "no": int(no), "image": str(std),
            "candidates": len(cands),
            "stale_retaken": retaken, "stale_flagged": flagged}


def supply_asset_sheet(ws, pid, *, kind, name=None, src, skip_check: bool = False) -> dict:
    """素材直供设定图：把一张现成图**替换**到标准设定图路径，不调用任何模型。

    与「候选换选」`pick_asset_candidate` 同一条落位通路，只是来源换成外部文件，故三件事
    一件都不能省：
      · **旧图先进版本栈**（不是覆盖）——设定图是全片一致性的根，覆盖掉就再也找不回来，
        而"换错了想换回去"恰恰是直供最常见的下一步；
      · **标准路径不变**——下游章节文档里存的是**路径**，换文件不换路径，血缘只需刷新
        过期标记，不必回头改一堆引用；
      · **血缘传播**（`_propagate`）——设定图一换，引用它的分镜就过期了。不传播的后果是
        下游还挂着旧脸的分镜图、状态却仍显示"已通过"，这是最坏的一类静默错误。

    体检只对"ffprobe 解不出"硬拦（等价 `supply --skip-check` 的逃生舱）；分辨率/宽高比
    这类只报不拦——实拍或手绘素材不可再生，不因建议级问题拒收。
    """
    src = Path(ensure_local(str(src)))
    if src.suffix.lower() not in supply.IMAGE_EXTS:
        raise KinemaError(f"不支持的图片格式 {src.suffix}"
                          f"（可选: {', '.join(sorted(supply.IMAGE_EXTS))}）")
    if not src.is_file():
        raise ProjectError(f"素材文件不存在: {src}")
    series = ws.get_project(pid)
    holder, mk, _vk, _vdir = _asset_version_ctx(series, kind, name)
    rep = mediacheck.inspect_image(src, canvas=None)
    if not skip_check and rep.get("hard_fail"):
        raise ProjectError(
            f"素材体检未过：{'；'.join(x['msg'] for x in rep['hard_fail'])}"
            "（确认无误可加 skip_check 跳过）")
    cur = holder.get(mk)
    if cur:
        # 沿用现有标准名（含扩展名可能不同：png ← jpg 也要换名，否则新旧两个文件并存）
        std = Path(ensure_local(cur)).with_suffix(src.suffix)
        archive_asset_sheet(series, kind, name,
                            reason=f"素材直供替换: {src.name}")
    else:
        std = series.refs_dir / f"{_version_label(holder, mk, kind, name)}{src.suffix}"
        std.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, std)
    holder[mk] = str(std)
    if kind == "character":
        holder["sheet_origin"] = "external"   # 外部素材：生成方式未知，恒不受信
    series.save()
    retaken, flagged = _propagate(series)
    return {"kind": kind, "name": name, "image": str(std), "inspect": rep,
            "stale_retaken": retaken, "stale_flagged": flagged}


def refine_asset(ws, store, router, *, pid, kind, name=None, rect=None,
                 instruction: str, no_moodboard: bool = False) -> dict:
    """设定图局部改造：备份旧图 → 原位编辑重生 → 血缘传播（下游分镜标过期）。"""
    instruction = (instruction or "").strip()
    if not instruction:
        raise KinemaError("改造指令不能为空")
    series = ws.get_project(pid)
    # 写实档的角色身份图不做局部改造：改造恒以旧图作主参考（下方 refs 第一项），
    # 属图生图，产物当场失去视频侧的受信豁免——而 refine 的核心是框选局改，抽掉
    # 源图它就不再是同一个函数。判在归档之前：先归档再抛会把好图移出标准路径。
    if kind == "character":
        _prov0, params0 = router.resolve("image", series.data.get("profile"))
        if params0.get("identity_sheet"):
            raise KinemaError(
                "写实档的角色设定图是受信身份图（纯文生图），局部改造会让它失去"
                "受信、生成视频时被人脸审核拒绝。要调整请改角色描述后整张重出：\n"
                f"  python3 -m kinema project refs {pid} --only character:{name} --force")
    # 归档已把旧图移出标准路径，版本条目必须在同一个块里落盘：留在内存等收尾写，
    # 会被那时的重读丢掉，下一次归档的 v 号随之撞上已有文件。
    with series.commit():
        src, _wb = _asset_target(series, kind, name)   # 进锁后重新定位
        if not src or not Path(ensure_local(src)).is_file():
            # 俯视布局图与场景基准图配对生成，裸 `project refs` 就会补上，无需附加开关
            flag = {"expression": " --expressions",
                    "pose": " --poses"}.get(kind, "")
            raise ProjectError(f"该{kind}还没有设定图，先 project refs {pid}{flag}")
        src = Path(ensure_local(src))
        bak = archive_asset_sheet(series, kind, name,   # 旧图移入版本栈（可回滚），并作主参考
                                  reason=f"局部改造：{instruction[:40]}")

    prov, params = router.resolve("image", series.data.get("profile"))
    # 开头声明 + **该类设定图的完整版式纪律**必须一并回喂：只给「旧图 + 改哪儿改什么」，
    # 模型不知道这张图本来该是三区 / 三视该等大 / 全身该空手，改完版式就塌了。
    # 比例同理走单一真源——在这里写死 `"1:1" if kind != "scene"` 会把本该
    # 16:9 横版的角色设定图出成方图。
    # 开头声明也走单一真源 `sheets.prefix_for`：俯视布局图用固定的制图风格声明，
    # 拿项目画风前缀去改一张平面图，改完就成了鸟瞰渲染。
    style_prefix, _fell = prompts_mod.select_style_prefix(
        params, getattr(prov, "prompt_lang", "zh"), doc=series.data)
    prefix = sheets.prefix_for(kind, style_prefix)
    holder, _wb = _asset_holder(series, kind, name)
    rules = sheets.rules_for(kind, holder)
    prompt = sheets.join_prompt([prefix, *rules, _edit_prompt(rect, instruction)])
    w, h = store.canvas(sheets.aspect_for(kind, series))
    refs = [str(bak)]                          # 旧图为主参考；该设定图的逐张垫图随之喂入保风格
    # 俯视布局图不吃垫图：垫图锁的是成片画风，而它是制图（生成侧同判据，见
    # `cmd_gen_refs` 的第二波）——喂进来只会把图纸拉回画面。
    if not no_moodboard and kind != "topview":
        for m in (ensure_local(v) for v in
                  series.moodboard_refs_for(_asset_refs(series, kind, name))):
            if m and Path(m).is_file() and m not in refs:
                refs.append(m)
    res = prov.generate(prompt, str(src), ref_images=refs[:8],
                        width=w, height=h, label=f"REFINE {kind.upper()} {name or ''}")
    with series.commit():                # 生成期以分钟计，其间别的写者可能已改过它
        _src, write_back = _asset_target(series, kind, name)   # 进锁后重新定位（必须）
        write_back(res.path)
        if kind == "character":          # 局改产物是图生图（写 sheet 处同批写来源）
            holder2, _ = _asset_holder(series, kind, name)
            holder2["sheet_origin"] = "i2i"
        if res.cost > 0:
            series.add_cost("image", res.cost)
    # 扩展设定图（表情/动作/俯视）不进每镜自动挂载——改了它没有下游分镜可作废，
    # 传播会把全章无辜置 retake（那是花钱重生），故只有主设定图三类才传播。
    if kind in ("character", "prop", "scene"):
        retaken, flagged = _propagate(series)
    else:
        retaken = flagged = 0
    return {"kind": kind, "name": name, "image": res.path, "backup": str(bak),
            "cost": res.cost, "region": region_phrase(rect),
            "at": datetime.now().isoformat(timespec="seconds"),
            "stale_retaken": retaken, "stale_flagged": flagged}
