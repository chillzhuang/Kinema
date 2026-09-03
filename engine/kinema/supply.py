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

"""素材直供（BYO Assets）：把现成图片登记为某镜的画面，跳过 AI 生图。

「固定场景图 + 资产图动态拼接」类解说视频的一等公民支持：产品图/图表/
截图/实拍图直接作为分镜画面，Ken Burns 运镜、转场、旁白、字幕、BGM
全链路照常——生图零成本，只花 TTS。

制度不打折：直供与 AI 生图走同一套版本栈/审阅状态机——
  · 覆盖已有画面前自动归档旧版（versions 可回滚）；
  · done 锁定镜拒绝直供覆盖（与 _regen_gate 同语义，先 retake）；
  · 登记后落「待审」，gen.image.provider="supplied" 标记来源（Studio 显示
    直供徽标、隐藏无意义的 AI 重生按钮）。
文件统一拷入章节工作目录 images/（与生成图同位同名规则），源路径记录在
gen.image.source 供追溯；逐比例直供写 images{aspect}。

**供料体检落在 `supply_image` 内部，不在 CLI 层**——Studio 的
`actions.supply_shot_image` 与 `server._shot_upload` 都直调本函数，体检写在
CLI 里等于「网页上传完全不体检」。体检插在**后缀闸之后、归档之前**：顺序反了
的话，硬拦时旧图已被移进版本栈而新图没登记，这一镜会变成无图状态。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from . import lineage, review
from .errors import ProjectError
from .pipeline import consistency, versioning

# 与 Studio /media 白名单同族的图片扩展名（视频/音频不属于分镜画面）
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _aspect_tag(aspect: str) -> str:
    return aspect.replace(":", "x")


def _inspect(project, src, *, aspect, store, skip_check: bool) -> dict:
    """素材体检并打印结论；硬失败直接抛 ProjectError（**调用点必须在归档之前**）。

    低分基准 = `store.canvas(aspect or project.aspect)`；没给 store（理论上不该
    发生，两个调用点都传）就只查可解码性与 alpha。判据与阈值见
    `pipeline/mediacheck` 的「二、供料体检」段。"""
    from .pipeline import mediacheck
    if skip_check:
        print("· 已跳过素材体检（--skip-check）——分辨率/宽高比/alpha 一律不查")
        return {"at": datetime.now().isoformat(timespec="seconds"),
                "skipped": True, "ok": True}
    canvas = store.canvas(aspect or project.aspect) if store is not None else None
    rep = mediacheck.inspect_image(src, canvas=canvas)
    for m in rep.get("info") or []:
        print(f"· {m}")
    for w in rep.get("warn") or []:
        print(f"⚠ 素材体检 · {w['msg']}")
    if not rep.get("ok"):
        raise ProjectError(
            "素材体检未通过：" + "；".join(f["msg"] for f in rep["hard_fail"])
            + "（确认素材可用且必须登记，加 --skip-check 跳过体检）")
    return rep


def supply_image(project, shot_no, src, *, aspect: str | None = None,
                 store=None, skip_check: bool = False) -> dict:
    """把本地图片直供为镜 shot_no 的画面。返回 {shot, path, archived, aspect, inspect}。

    aspect=None 写主图 shots[].image；给比例（如 "9:16"）写 images{aspect}。
    store = ConfigStore（体检的画布基准来源）；skip_check=True 整个跳过体检。
    """
    s = next((x for x in project.shots
              if str(x.get("id")) == str(shot_no)), None)
    if s is None:
        raise ProjectError(f"找不到镜 {shot_no}")
    if s.get("kind") == "transition":
        raise ProjectError("转场镜由合成段本地渲染，不接受直供画面")
    if review.is_omitted(s):
        raise ProjectError(f"镜 {shot_no} 已弃用(omt)——先恢复再直供")
    if review.is_locked(s, "image"):
        raise ProjectError(f"镜 {shot_no} 分镜图已通过·锁定——要替换先 "
                           "review set --state retake")
    src = Path(src).expanduser()
    if not src.is_file():
        raise ProjectError(f"素材不存在: {src}")
    if src.suffix.lower() not in IMAGE_EXTS:
        raise ProjectError(f"不支持的图片格式 {src.suffix}"
                           f"（可选: {', '.join(sorted(IMAGE_EXTS))}）")

    # 供料体检：**必须在这一格**——后缀闸之后（不体检非图片）、归档之前
    # （硬拦时旧图还没被搬进版本栈，这一镜不会变成无图状态）
    inspect = _inspect(project, src, aspect=aspect, store=store,
                       skip_check=skip_check)

    # 覆盖前归档旧版（与生成路径同制度，可回滚）
    archived = versioning.archive(project, s, "image",
                                  reason=f"素材直供替换: {src.name}")
    imgs = project.subdir("images")
    stem = (f"shot_{s['id']}_{_aspect_tag(aspect)}" if aspect
            else f"shot_{s['id']}")
    dst = imgs / f"{stem}{src.suffix.lower()}"
    # 同镜换格式时清掉旧扩展名残影（png→jpg 等），防合成吃到旧文件
    for old in imgs.glob(f"{stem}.*"):
        if old != dst and old.suffix.lower() in IMAGE_EXTS:
            old.unlink()
    shutil.copyfile(src, dst)

    if aspect:
        s.setdefault("images", {})[aspect] = str(dst)
    else:
        s["image"] = str(dst)
    s["status"] = "done"
    gen = s.setdefault("gen", {})
    prev = (gen.get("image") or {}).get("version") or 0
    gen["image"] = {"provider": "supplied", "source": str(src),
                    "cost": 0.0, "version": prev + 1, "inspect": inspect}
    consistency.invalidate(s, "image")     # 换了张图 → 旧一致性判定作废（判的是被替换那张）
    # **直供＝这一镜重新出过图**：血缘基线按当前设定图重设、过期标记随之清掉
    # （真源 lineage.rebaseline）。缺了这一步两头都错——「⚠ 设定已更新」只有再走一次
    # API 生成才擦得掉；而上一行整块替换 gen["image"] 已把旧 refs 快照冲掉，
    # 这一镜从此再不参与过期判定。
    rebased = lineage.rebaseline(project, s, "image")
    clip = lineage.retake_clip_for_image(s)   # 片段按被换掉的画面生成
    review.mark_generated(s, "image")      # 直供同样落「待审」，人审后过
    project.save()
    return {"shot": s["id"], "path": str(dst), "archived": archived,
            "aspect": aspect, "inspect": inspect, "rebaselined": rebased,
            "clip": clip}
