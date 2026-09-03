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

"""交付包一键导出。

一个章节 = 一个交付包：成片（多比例）+ 封面 + 字幕（ASS 原样 + SRT 通用版）
+ 各平台发布文案/标签 + manifest 元数据清单，按平台分目录后整体打 zip——
接单交付与自运营发布共用同一份产物。

合规内嵌（发布与合规的交付面）：
  · AI 生成披露：文案末尾附披露行，manifest 记录声明与所用模型清单
    （多数平台已要求 AI 内容显式标注，先在交付物层面把披露带上）；
  · 版权标记：manifest 记录独家/非独家（project.license，缺省视为未声明）。

字幕说明：成片内已烧录 ASS 样式字幕；SRT 供平台上传外挂字幕或后期二改使用，
事件序列与烧录同走 `subtitle.shot_events`（多角色镜 `lines[]` 逐句一条），
语言口径由调用方传入 `subtitle.sub_cfg` 的解析结果。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from .errors import ProjectError
from .ffmpeg import run as ff_run
from .project import Project, aspect_tag
from .storage.media import ensure_local

AI_DISCLOSURE = "本内容由 AI 辅助生成（画面/配音为 AI 生成，文案与审核由人工完成）"


def _fmt_srt_time(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)):03d}"


def build_srt(project: Project, *, lang: str | None = None) -> str:
    """从分镜时间轴生成 SRT——**与烧录字幕严格同源**：事件序列走
    `subtitle.shot_events`（全部版式的共用入口）：多角色镜 `lines[]` 逐句一条
    cue、按各句实测时长切分；单段镜整镜一条，文本仍是 narration>caption 的
    音字一致铁律。lang 由调用方传 `sub_cfg` 的解析结果（subtitle 块的 lang
    覆盖顶层 `subtitle_lang`，与烧录同判），缺省回落顶层字段。
    both 时一条 cue 内中文主行 + 英文副行两行。"""
    from .pipeline.subtitle import shot_events
    lang = lang or project.data.get("subtitle_lang") or "zh"
    lines, no = [], 0
    for start, end, s in project.timeline():
        for ts, te, main, sub, _spk in shot_events(s, start, end, lang):
            cue = "\n".join(x for x in (main, sub) if x)
            if not cue:
                continue
            no += 1
            lines += [str(no), f"{_fmt_srt_time(ts)} --> {_fmt_srt_time(te)}", cue, ""]
    return "\n".join(lines)


def _registered_cover(project: Project) -> Path | None:
    """`cover` 命令产出的章节封面（工作区相对路径存储，系列目录的上一级即工作区）。"""
    reg = project.data.get("cover") or {}
    rel = reg.get("primary") if isinstance(reg, dict) else None
    if not rel:
        return None
    q = Path(str(rel))
    if not q.is_absolute():
        q = project.path.parent.parent.parent / q
    return q if q.is_file() else None


def _extract_cover(video: Path, out: Path) -> bool:
    """从成片抽首帧做封面（`cover` 命令未出过封面时的回落，与 Studio 海报同一取帧点）。"""
    try:
        ff_run(["-ss", "0.8", "-i", str(video), "-frames:v", "1", "-q:v", "2",
                "-y", str(out)], desc="封面抽帧")
        return out.is_file()
    except Exception:  # noqa: BLE001  无 ffmpeg 或坏文件时交付包降级为无封面
        return False


def _platform_copy(project: Project, platform: str) -> str:
    """平台发布文案：优先 Skill 层写好的 script.per_platform，回退基础文案。"""
    script = project.data.get("script") or {}
    pp = (script.get("per_platform") or {}).get(platform) or {}
    title = pp.get("title") or (project.data.get("chapter") or {}).get("title") \
        or project.data.get("title") or project.id
    body = pp.get("caption") or "\n".join(
        x for x in [script.get("hook"), script.get("body"), script.get("cta")] if x)
    tags = pp.get("hashtags") or []
    parts = [f"【标题】{title}", "", body or "（文案待补：script.per_platform 或 hook/body/cta）"]
    if tags:
        parts += ["", " ".join(t if t.startswith("#") else f"#{t}" for t in tags)]
    parts += ["", AI_DISCLOSURE]
    return "\n".join(parts)


def _providers_used(project: Project) -> list[str]:
    provs = set()
    for s in project.data.get("shots") or []:
        for snap in (s.get("gen") or {}).values():
            if snap.get("provider"):
                provs.add(snap["provider"])
    return sorted(provs)


def build_delivery(project: Project, *, platforms: list[str] | None = None,
                   license_kind: str | None = None, out_dir: Path | None = None,
                   make_zip: bool = True, subtitle_lang: str | None = None) -> dict:
    """构建交付包目录（含多平台子目录 + manifest），可选打 zip。

    成片取 data.output 的全部比例；一个比例都没有说明还没走完合成，直接拒绝
    （交付包必须是可交付状态，不出半成品）。"""
    outputs = {a: ensure_local(p) for a, p in (project.data.get("output") or {}).items()}
    outputs = {a: p for a, p in outputs.items() if p and Path(p).is_file()}
    if not outputs:
        raise ProjectError("没有可交付的成片（data.output 为空）——先完成 assemble 合成。")
    # 平台不做静默兜底（兜 douyin 的默认会把未绑定平台的项目打成抖音交付包）：
    # 交付包按平台分目录组织，没有平台就没有可组织的对象，直接把缺口说清楚。
    platforms = platforms or project.data.get("platform")
    if not platforms:
        raise ProjectError("项目未绑定目标平台——deliver --platform 指定，"
                           "或建项目时 project new --platform 落进 project.json。")
    stamp = datetime.now().strftime("%Y%m%d")
    # 只清理引擎自建的缺省目录；`--out` 指向的目录归用户所有，非空即拒
    if out_dir:
        root = Path(out_dir)
        if root.exists() and any(root.iterdir()):
            raise ProjectError(f"输出目录非空: {root}——请指定空目录或新目录")
    else:
        root = project.exports_dir / f"{project.id}_{stamp}"
        if root.exists():
            shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    # 公共素材只生成一次，各平台目录里复制（交付对象通常按平台分发给不同运营）
    primary = Path(outputs.get(project.aspect) or next(iter(outputs.values())))
    registered = _registered_cover(project)
    cover_tmp = root / ("_cover" + (registered.suffix.lower() if registered else ".jpg"))
    if registered:
        shutil.copy2(registered, cover_tmp)
        has_cover = True
    else:
        has_cover = _extract_cover(primary, cover_tmp)
    cover_name = "cover" + cover_tmp.suffix
    srt_text = build_srt(project, lang=subtitle_lang)
    subs_dir = project.workdir / "subs"

    files = []
    for plat in platforms:
        pdir = root / plat
        pdir.mkdir()
        for asp, path in outputs.items():
            dst = pdir / f"{project.id}_{aspect_tag(asp)}.mp4"
            shutil.copy2(path, dst)
            files.append(str(dst.relative_to(root)))
        if has_cover:
            shutil.copy2(cover_tmp, pdir / cover_name)
            files.append(f"{plat}/{cover_name}")
        if srt_text:
            (pdir / "subtitle.srt").write_text(srt_text, encoding="utf-8")
            files.append(f"{plat}/subtitle.srt")
        for asp in outputs:
            ass = subs_dir / f"sub_{aspect_tag(asp)}.ass"
            if ass.is_file():
                shutil.copy2(ass, pdir / ass.name)
                files.append(f"{plat}/{ass.name}")
        (pdir / "copy.txt").write_text(_platform_copy(project, plat), encoding="utf-8")
        files.append(f"{plat}/copy.txt")
    if has_cover:
        cover_tmp.unlink()

    ch = project.data.get("chapter") or {}
    manifest = {
        "project": ch.get("project") or project.id,
        "chapter": ch.get("id"),
        "title": ch.get("title") or project.data.get("title") or project.id,
        "theme": project.data.get("theme"), "profile": project.profile,
        "aspects": list(outputs), "duration": project.total_duration(),
        "shots": len(project.active_shots),
        "platforms": platforms,
        "cost": project.data.get("cost"),
        "cost_estimate": project.data.get("cost_estimate"),
        "ai_disclosure": {"statement": AI_DISCLOSURE,
                          "providers": _providers_used(project)},
        "license": license_kind or "unspecified",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": files,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_path = None
    if make_zip:
        zip_path = shutil.make_archive(str(root), "zip", root_dir=root)
    return {"dir": str(root), "zip": zip_path, "platforms": platforms,
            "files": len(files) + 1, "aspects": list(outputs)}
