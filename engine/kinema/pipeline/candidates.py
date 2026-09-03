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

"""宫格候选选优（直出可用率 +80%）。

生成端（gen-image --candidates N）：每镜一次出 N 张候选（同提示词、派生 seed），
只落 shots[].image_candidates，**不占画布**（shots[].image 仍空）——候选是待选品，
不是产物。人在 Studio 宫格（或 CLI pick）点选后才定稿上画布。

点选端（本模块 pick，CLI `pick` 与 Studio POST /api/pick 共用）：
  · 画布已有图 → 先归档进版本栈（reason=pick，谱系完整）；
  · 选中候选**拷贝**上画布（候选文件保留，可反悔换选）；
  · 宫格点选=人眼定稿 → image 直接置 done 锁定（防烧钱终态），
    `approve=False` 可改为落待审。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import KinemaError


def seed_for(base_seed, k: int):
    """候选 k 的派生 seed：同镜候选必须不同 seed 才有差异；无基准 seed 时交给模型随机。"""
    return (int(base_seed) + k * 7919) if base_seed is not None else None


def candidate_path(project, shot: dict, k: int) -> Path:
    return project.subdir("images") / f"shot_{shot.get('id')}_cand{k}.png"


def listing(shot: dict) -> list[str]:
    return list(shot.get("image_candidates") or [])


def pick(project, shot: dict, no: int, *, approve: bool = True) -> dict:
    """把候选 no（1 起）定为该镜分镜图。返回 {shot, no, canvas, version, state}。"""
    from .. import review
    from . import consistency, versioning
    cands = listing(shot)
    if not cands:
        raise KinemaError(f"镜 {shot.get('id')} 没有候选图（先 gen-image --candidates N）")
    if not 1 <= no <= len(cands):
        raise KinemaError(f"候选编号超界: {no}（该镜共 {len(cands)} 张候选）")
    src = Path(cands[no - 1])
    if not src.is_file():
        raise KinemaError(f"候选文件丢失: {src}")
    if review.is_locked(shot, "image") and shot.get("image_picked") == no:
        raise KinemaError(f"镜 {shot.get('id')} 已定稿为候选 #{no}，无需重选")

    # 画布已有图 → 归档（换选/重选都留谱系）
    v = versioning.archive(project, shot, "image",
                           reason=f"pick: 换选候选 #{no}",
                           params=(shot.get("gen") or {}).get("image"))
    canvas = project.subdir("images") / f"shot_{shot.get('id')}.png"
    shutil.copy2(src, canvas)                  # 拷贝而非移动：候选保留可反悔
    shot["image"] = str(canvas)
    # 逐比例画布已随归档进版本栈：不清 images{} 的话 image_for 会优先取到已移走的路径
    shot.pop("images", None)
    shot["image_picked"] = no
    # 画布快照取自出候选那一批：提示词、Envelope 与血缘基线描述的是这张图
    from .. import lineage
    snap = (shot.get("gen") or {}).get("image_candidates") or {}
    gen = shot.setdefault("gen", {})
    prev_refs = (gen.get("image") or {}).get("refs")
    gen["image"] = {k: snap[k] for k in ("prompt", "seed", "provider", "envelope", "cost")
                    if k in snap}
    gen["image"]["candidate"] = no
    gen["image"]["version"] = versioning.current_version(shot, "image")
    if snap.get("refs"):
        # 指纹取出候选那一刻的记录：此后设定图改版，定稿的这张要判过期
        gen["image"]["refs"] = dict(snap["refs"])
        lineage.clear_stale(shot)
    elif prev_refs:
        gen["image"]["refs"] = prev_refs     # 无批快照时沿用旧基线，镜不退出过期判定
    consistency.invalidate(shot, "image")      # 画布换了张 → 旧一致性判定作废
    clip = lineage.retake_clip_for_image(shot)
    state = "done" if approve else "wfa"
    review.set_state(shot, "image", state,
                     note=None if approve else f"宫格换选 #{no}，待复核")
    project.save()
    return {"shot": shot.get("id"), "no": no, "canvas": str(canvas),
            "version": gen["image"]["version"], "state": state,
            "archived": f"v{v:03d}" if v else None, "clip": clip}
