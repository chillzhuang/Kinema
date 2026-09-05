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

"""kinema CLI —— 执行引擎统一入口。

阶段命令（对应 design.md 流水线阶段4/5）：
  gen-image  逐镜生图（按 profile 解析模型 + 风格前缀 + 一致性参考）
  tts        逐镜配音 + 回填真实时长 + 时间戳
  subtitle   由分镜时间轴生成 ASS 字幕
  music      背景音乐
  compose    Ken Burns → 拼接 → 特效 → 混音 → 烧字幕 → 竖屏成片
  run        依次跑完（全自动一条龙）
工具命令：
  studio     启动可视化系统（浏览/播放产物）
  doctor     自检环境、配置源与 profiles
  init       生成 project.json 骨架

模型不写死在代码里：由 config/models.yaml 的 profiles 决定（见 ModelRouter）。
MVP 不含发布；发布由独立 skill 承接。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9一-鿿]+", "_", s or "x").strip("_") or "x"

from . import __version__, lineage, review
from . import models as models_mod
from . import budget as budget_mod
from . import decisions as decisions_mod
from . import novel as novel_mod
from . import study as study_mod
from .errors import KinemaError, ConfigError, ProjectError, ProviderError
from .ffmpeg import concat_audio, ensure_tools, probe_duration, probe_json, to_pcm
from .models import ConfigStore, ModelRouter
from . import control as control_mod
from . import parallel
from . import previz as previz_mod
from . import sheets
from . import sketchboard as sketch_mod
from . import skills
from . import voicebank
from . import voicecast
from .pipeline import anchorframe
from .pipeline.refplan import RefPlan
from .pipeline import camera as camera_mod
from .pipeline import candidates as candidates_mod
from .pipeline import compose as compose_mod
from .pipeline import consistency as consistency_mod
from .pipeline import framechain
from .pipeline import tailrelay
from .pipeline import mediacheck as mediacheck_mod
from .pipeline import prompts as prompts_mod
from .pipeline import transitions as transitions_mod
from .pipeline import variation as variation_mod
from . import fonts as fonts_mod
from .pipeline import subtitle as subtitle_mod
from .pipeline import versioning
from .pipeline.checkpoint import has_file, mark
from .project import (DEFAULT_ASPECT, Project, aspect_tag, chapter_flag,
                      chapter_title_number)
from .prompt_contract import profile_revision, reference_digest
from .workspace import Workspace, find_workspace


def _info(msg): print(f"  {msg}", flush=True)
def _step(msg): print(f"▶ {msg}", flush=True)


def _prompt_references(rows):
    """把计划期实际引用归一为 Envelope 的稳定内容指纹。"""
    result = []
    seen = set()
    for role, ref_id, source in rows:
        value = str(source or "").strip()
        if not value or (role, value) in seen:
            continue
        seen.add((role, value))
        result.append({
            "role": role,
            "id": str(ref_id or value),
            "sha256": reference_digest(value),
        })
    return result


def _prompt_revisions(project, profile: str, provider, profile_params) -> tuple[str, str]:
    """解析 Envelope 所需的 Skill/Profile revision；两者都来自运行时机器真源。"""
    from .agent_system import AgentCatalog
    catalog = AgentCatalog.load()
    # 章节落盘的绑定值：退役 Skill/画风走 bound_*，报错带换绑路径——这条在生图/
    # 生视频的封装路径上，报「未知 Skill」而不给出路等于把付费阶段堵死在门口
    skill_id = project.data.get("skill") or catalog.bound_profile(profile)
    skill_revision = catalog.bound_skill(skill_id)["digest"]
    return skill_revision, profile_revision(profile, provider, profile_params)


def _project_path(args) -> str:
    """解析要操作的视频/章节文件：优先 --chapter 项目id/章节id，否则 --project 文件。"""
    ch = getattr(args, "chapter", None)
    if ch:
        if "/" not in ch:
            raise KinemaError("--chapter 需形如 项目id/章节id，例如 lanterns/ch01")
        proj, cid = ch.split("/", 1)
        ws = Workspace.open(getattr(args, "workspace", None), create=False)
        return str(ws.get_project(proj).get_chapter_path(cid))
    if getattr(args, "project", None):
        return args.project
    raise KinemaError("请用 --project <文件> 或 --chapter <项目id/章节id> 指定要处理的视频")


def _effective_profile(shot, override, project):
    return shot.get("profile") or override or project.profile


def _op_locked(kind):
    """章节操作锁装饰器：会移动产物或改写文档的直连命令与生成阶段同闸。

    锁先于装载——装载后再锁拿到的仍是可能过期的副本。表态类命令不套。"""
    def deco(fn):
        def inner(args):
            from .locking import op_lock
            with op_lock(Path(_project_path(args)), kind=kind):
                return fn(args)
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return deco


def _regen_gate(project, s, stage, force, *, quiet: bool = False):
    """产物重生节点（状态机判定，**纯只读**）。

    返回 (skip, regen)：
      · 已通过(done) 且产物在 → 锁定跳过（--force 也不覆盖，防烧钱；要重生先 review set retake）
      · 重做(retake) → 视同该镜 force
      · 其余 → 按 force/缺失 判定

    `quiet` 供预检类调用（同一镜随后还会在计划循环里正式过闸）——跳过播报只在
    正式那一次说，双份播报会让清单看起来有重复镜。

    归档不在此处执行：计划循环内任何一镜的 provider 解析或提示词装配失败，
    都不能留下「旧产物已移走、版本条目未落盘」的中间态——归档统一由
    `_archive_regen` 在整批计划成功后、派活之前执行。
    """
    if review.is_omitted(s):
        if not quiet:
            _info(f"镜 {s.get('id')}: 已弃用(omt)，跳过")
        return True, False
    if review.is_locked(s, stage):
        if not quiet:
            _info(f"镜 {s.get('id')}: {stage} 已通过·锁定，跳过（重生先 review set --state retake）")
        return True, False
    return False, force or review.needs_retake(s, stage)


def _regen_reason(s: dict, stage: str) -> str:
    """重生归档条目的理由：带重做意见的 retake 原文入栈，供回滚时对照。"""
    retake = review.needs_retake(s, stage)
    note = review.get_note(s, stage)
    return f"retake: {note}" if retake and note else ("retake" if retake else "force")


def _staged(dst: Path, stage: bool) -> Path:
    """重生时新图先落临时名（`<画布名>.new.png`），成功后再归档旧版并替换到画布：
    生成期间画布与参考图都仍在盘，失败时画布原地不动。首次生成无旧版，直写画布。"""
    return dst.with_name(f"{dst.stem}.new{dst.suffix}") if stage else dst


def _archive_regen(project, items, stage):
    """把计划内 regen 镜的现有产物归档进版本栈（无产物时 archive 返回 None）。

    调用点固定在计划循环全部成功之后、`_mark_wip` 之前。生图侧只有 agent 工单路由
    走这里（工单要开在画布路径上）；API provider 的分镜图先落临时名、在回填时
    归档替换（`_staged`）。agent 工单路由的验收轮不归档：旧版在开单那一轮已进
    版本栈，此刻画布上是 agent 刚交付、尚未登记的图，归档等于把待验收物移走再重新开单。
    """
    from .providers.image.agent import has_pending_order
    for it in items:
        if not it.get("regen"):
            continue
        s = it["shot"]
        if getattr(it.get("prov"), "name", "") == "agent" and any(
                has_file(pth) and has_pending_order(pth)
                for pth in [s.get("image"), *(s.get("images") or {}).values()] if pth):
            continue
        v = versioning.archive(project, s, stage, reason=_regen_reason(s, stage),
                               params=(s.get("gen") or {}).get(stage))
        if v:
            _info(f"镜 {s.get('id')}: {stage} 旧版已归档 v{v:03d}")


def _mark_wip(project, items, stage):
    """派活前把计划内的镜置「生成中」(wip) 并落盘一次——Studio 大屏的忙态
    （分镜卡遮罩/看板 wip 列/时间线配色）以 review 状态为数据源，不写就永远看不见。

    先在计划项上留存原审阅条目：wip 只是过渡态，失败或未真生成时要原样放回
    （set_state 会重建条目，直接回写 todo/retake 会吞掉 retake 的意见与时间戳）。
    进程中途崩溃会把 wip 留在盘上——重跑同一条命令即自愈（重新置 wip → 完成落 wfa）。
    """
    for it in items:
        s = it["shot"]
        was = (s.get("review") or {}).get(stage)
        it["review_was"] = dict(was) if was else None
        review.set_state(s, stage, "wip")
    if items:
        project.save()


def _unmark_wip(s, stage, item):
    """生成没有发生（失败/复用跳过/断闸未派活）→ 恢复派活前的审阅条目。
    只在仍是 wip 时动手：成功路径 mark_generated 已把它推进 wfa。"""
    if review.get_state(s, stage) != "wip":
        return
    was = item.get("review_was")
    if was is None:
        (s.get("review") or {}).pop(stage, None)
    else:
        s.setdefault("review", {})[stage] = was


def _apply_aspect_args(project, args):
    """把 --aspect / --aspects / --both / --image-per-aspect / --motion / --effects
    落到 project——全部走 `override_runtime`：**只作用于本次渲染，绝不落盘**。

    这些 flag 表达的是"这一次这么跑"而非"把章节改成这样"。若直改 project 落盘：
    `assemble --kenburns`（这次想看静图版）经 stage_compose 收尾的 save 会把 native
    章节的 motion 永久改成 kenburns，此后 gen-video 拒发、片段音轨不再被采用，
    全程零提示（见 `Project.override_runtime`）。"""
    ov = project.override_runtime
    if getattr(args, "both", False):
        ov("aspects", ["9:16", "16:9"])
    elif getattr(args, "aspects", None):
        ov("aspects", [a.strip() for a in args.aspects.split(",") if a.strip()])
    elif getattr(args, "aspect", None):
        ov("aspect", args.aspect)
        ov("aspects", [args.aspect])
    if getattr(args, "image_per_aspect", False):
        ov("image_per_aspect", True)
    if getattr(args, "no_effects", False):   # 运行时特效覆盖（不改配置）：--no-effects 关全部
        ov("effects", [])
    elif getattr(args, "effects", None) is not None:
        ov("effects", [e.strip() for e in args.effects.split(",") if e.strip()])
    # native 配音混烧的一次性开关：native 缺省不烧我们的 TTS（片段自带原生人声，
    # 叠上去=同一句话两个人说）。加了才烧，且同样不落盘——「这一次要旁白」而非
    # 「把章节改成永远带旁白」（要常开写章节 native_voiceover: true）。
    if getattr(args, "burn_voice", False):
        ov("native_voiceover", True)
    if getattr(args, "motion", None):        # 简写 a/b/c 或全名，由 project.motion 归一
        ov("motion", args.motion)
    elif getattr(args, "native", False):
        ov("motion", "native")
    elif getattr(args, "dubbed", False):
        ov("motion", "dubbed")
    elif getattr(args, "kenburns", False):
        ov("motion", "kenburns")
    # 衔接缺省关闭（缺省档=逐镜全能参考），--chain 才是真正改变行为的一侧；
    # 两个都给时仍按关闭处理——显式要求"这次别衔接"的一方更可能是当下的意图。
    if getattr(args, "no_chain", False):
        ov("frame_chain", False)
    elif getattr(args, "chain", False):
        ov("frame_chain", True)


# ---------- 阶段 ----------
# style_prefix_en 缺失降级警告去重（每个 profile 每次进程只提示一次）
_warned_prefix_fallback: dict = {}


def _video_cast(project, shot) -> list[dict]:
    """本镜出场且已有设定图的角色，形态 `{kind, name, path}`。取材与图像侧文字锚同一
    口径（`Project.shot_cast`：显式出场表 > 文本命中 > 全员回落）——按「全部有设定图者」
    点名会把不在场的角色写成「本镜出场」，与未出场禁令互斥。"""
    cast, _fallback = project.shot_cast(shot)
    return [{"kind": "character", "name": c.get("name"), "path": c.get("sheet")}
            for c in cast if c.get("sheet")]


def _video_subject_kinds(project, shot) -> list[str]:
    """本镜出场角色的登记主体类型（动力学地板挑「随动附属物」名词用）。

    取材走 `Project.shot_cast` 而非 `_video_cast`：地板要的是「这一镜里演的是谁」，
    与设定图在不在盘无关——设定图还没画的主体，登记的类型一样成立。全员兜底那一档同样计入，
    此时名词最多合并出两三个类别，比给动物发「衣料」准确。
    """
    cast, _fallback = project.shot_cast(shot)
    return [prompts_mod.subject_kind(c) for c in cast]


def _cast_anchor_text(cast: list[dict], project, *, lang: str = "zh") -> str:
    """角色绑定句：名字 + 剪影锚点 + 正向视觉特征，供视频提示词前置。

    图生视频的首帧/首尾帧模式在协议上不能附带参考图，角色设定图无法随请求发出；
    设定的文字部分是此时唯一能传达角色特征的通道。完整外貌已经烘焙在首帧图的
    像素里，再复述一遍等于要求模型重画主体；`constraints` 是视觉禁令，必须走
    视频负面通道，不能与正向锚混用。
    """
    _flat = prompts_mod.flat_text      # 归一口与图像侧共用，绝不各写一份
    by_name = {c.get("name"): c for c in project.characters}
    parts = []
    for r in cast:
        c = by_name.get(r.get("name")) or {}
        # 标点感知拼接：剪影锚点常以「。」收尾，裸的分号 join 会拼出
        # 「…看清什么东西。；绝不摘掉…」，与 prompts 侧「。，」是同一病灶的另一个
        # 拼接点。分隔符判断统一交给 `prompts.zh_join_all`，不在这里再写一份。
        visual = _flat(c.get("visual_requirements"))
        if lang == "en":
            marks = "; ".join(x for x in (_flat(c.get("silhouette_notes")),
                                          f"must keep: {visual}" if visual else "") if x)
            parts.append(f"{r['name']} ({marks})" if marks else str(r["name"]))
            continue
        positive_visual = f"必须保留的视觉特征：{visual}" if visual else ""
        marks = prompts_mod.zh_join_all(
            [_flat(c.get("silhouette_notes")), positive_visual], sep="；")
        parts.append(f"{r['name']}（{marks}）" if marks else str(r["name"]))
    if not parts:
        return ""
    if lang == "en":
        return "Characters in this shot must match the provided frames: " + ", ".join(parts)
    return "本镜出场角色的形象须与所给画面一致：" + "、".join(parts)


def _gate_cast_anchor(project, shot, img_path, *, route: str = "A",
                      ref_plan=None) -> None:
    """拦截「角色出场、但画面里没有这个角色的身份来源」的镜——判据按路线分：

    **路线 A（缺省）**：视频模型只能从首帧取得角色的样貌。首帧若是纯场景空镜或
    未挂过该角色设定图的画面，提示词里再怎么描写，模型也只能凭训练集均值另造
    一个人——而且它不报错，要等看片才发现，此后还会沿首尾帧链把这个错误形象
    传给后续每一段。判据取分镜图生成时实际挂载的设定图（`gen.image.refs` 指纹），
    与生图时的真实入参同源。三类情形豁免：`skip_design` 项目、显式空出场表的镜、
    以及**没有 refs 记录**的图——素材直供、手工放置与早期数据都没有这份指纹，
    此时无从判断挂没挂过，不作推定、直接放行。

    **路线 B/C（降级）**：分镜图整个不进请求，「首帧认人」的立论不成立——身份
    完全由随请求附发的身份图承载，判据改成「本镜 cast 的身份图确实在这次真发的
    参考清单里」。被 7 张配额裁掉即拦（那不是提示能补的，身份来源真的没发出去）。
    """
    if project.skip_design or shot.get("characters") == []:
        return
    cast = _video_cast(project, shot)
    if not cast:
        return
    if route != "A":
        sent = {Path(str(p)).name for k, _n, p in
                (ref_plan.rows if ref_plan is not None else ())
                if k == "character"}
        missing = [r["name"] for r in cast
                   if Path(str(r["path"])).name not in sent]
        if not missing:
            return
        raise ProjectError(
            f"镜 {shot.get('id')}: 降级路线{route}下身份完全由身份图承载，但 "
            f"{'、'.join(missing)} 的身份图没进本次参考清单（多半被 7 张配额裁掉）。\n"
            "  减少该镜挂载的场景/道具，或收紧 shots[].characters 出场表后重跑")
    refs = ((shot.get("gen") or {}).get("image") or {}).get("refs") or {}
    if not refs:
        return          # 无指纹 = 无证据，不做推定
    anchored = {Path(k).name for k in refs}
    missing = [r["name"] for r in cast if Path(str(r["path"])).name not in anchored]
    if not missing:
        return
    raise ProjectError(
        f"镜 {shot.get('id')}: 首帧画面没有角色锚——{'、'.join(missing)} 在本镜出场，"
        f"但 {Path(str(img_path)).name} 生成时并未挂载其设定图。\n"
        "  视频模型只能从首帧认人，这样生成的角色形象会与设定不符，且沿首尾帧链扩散。\n"
        "  两条修法二选一：\n"
        f"    · 该镜确实无人出场 → 写 shots[].characters: [] 声明（空镜合法）\n"
        f"    · 该镜有人出场 → 先重出分镜图让设定图入参："
        f"gen-image --chapter {project.data.get('chapter', {}).get('project')}"
        f"/{project.data.get('chapter', {}).get('id')} --only {shot.get('id')} --force")


def _route_for(project, shot, *, identity: bool, ref_task: bool,
               board, scene_base, force: bool = False,
               v2v: bool = False) -> tuple[str, str]:
    """参考装配路线仲裁 → `(route, 理由)`，dry-run 与真发共用的纯判定。

    三级阶梯只在写实档（identity_sheet）武装：非写实档在照片级阈值之下、根本
    不触发人脸闸，给它们武装阶梯只会多花板钱。A 是现行为（image=分镜图，近景
    正脸随便画——人脸拒发生在建任务 HTTP 400、不计费，先试最好的构图是免费的）；
    B/C 是被拒后的降级形态（image=场景基准图，构图由板/提示词承载，身份恒由
    受信身份图承载）。`face_visibility: closeup` 是作者的可选预判：跳过注定被拒
    的 A 直接从 B 起步，省一次免费往返；标错也只是多试一次。

    `ref_task` = 本镜请求能挂参考装配（native 全能参考、dubbed 参考媒体，或控制
    视频的 V2V——三者的图都挂 `role=reference_image`），判据在 `cli._ref_task`。
    首帧与衔接镜的图占的是 `first_frame` 槽，官方禁与参考媒体混发，降级装配无处可挂、恒 A。

    可降级的硬前置（缺一即恒 A，由收尾文案给修法）：
      · 出场角色的身份图全部受信（sheet_origin == t2i）——不受信时 B/C 同样被拒，
        降级只会白买板；
      · 主场景基准图在盘（它要顶 image 位）；
      · 本镜不是 previz 镜（active_guide 缺省仲裁里 previz 优先于 board——previz
        的末帧/参考视频与降级装配互斥，买了板也挂不上）。
    """
    if not identity:
        return "A", "非写实档"
    if not ref_task:
        return "A", "非参考任务（首帧/衔接/V2V 协议禁混参考装配）"
    cast = _video_cast(project, shot)
    if not cast:
        return "A", "无出场角色"
    origin_of = {c.get("name"): c.get("sheet_origin")
                 for c in project.characters}
    stale = [r["name"] for r in cast if origin_of.get(r["name"]) != "t2i"]
    if stale:
        return "A", ("身份图不受信（" + "、".join(stale)
                     + " 的 sheet_origin ≠ t2i）——降级救不了，被拒后先 "
                     "project refs --only character:<名> --force 重出")
    if not scene_base:
        return "A", "主场景基准图不在盘，降级路线无 image 位可用"
    if sketch_mod.active_guide(shot) == "previz":
        return "A", "previz 镜（运动预演与降级装配互斥）"
    if force or str(shot.get("face_visibility") or "").strip() == "closeup":
        why = "人脸拒后降级" if force else "作者预判近景正脸"
        # V2V **恒不走 B**：板与控制视频是两个并列的运动权威（同 previz 那道闸的
        # 理由），盘上恰好有板也不挂。而无板在这条路上不是缺口——控制视频逐帧给定
        # 走位与景别，比板还硬；照搬「交提示词兜底」会把降级里最强的一档报成最弱的
        if v2v:
            return "C", f"{why}，构图由控制视频逐帧给定"
        if board:
            return "B", f"{why}，构图由板驱动"
        return "C", f"{why}·无板，构图交提示词与俯视图兜底"
    return "A", "先按分镜图路线试（人脸拒不计费）"


def _warn_cover_missing(project) -> None:
    """生图收尾时点名"封面还没出"——Studio 卡片的图源缺口只有这一刻提醒得及时。

    `#/projects` 主卡与项目页章节区的图源是「封面 → 成片海报帧 → 首个正镜分镜图」三级回落
    （`scanner._shot_thumb`）：末级只是缩略图源，不是封面；封面不出的整个制作期里卡片都顶着分镜图。
    封面只依赖设定图与画风，生图过审即可出，所以卡在生图收尾提醒最合适。
    """
    ch = project.data.get("chapter") or {}
    pid, cid = ch.get("project"), ch.get("id")
    if not pid:
        return          # 散装 --project 文件没有系列目录，封面无从谈起
    # 工作区章节恒为 project/<pid>/chapters/<cid>.json → 上溯两级即系列目录
    covers = project.path.parent.parent / "assets" / "covers"
    series_done = covers.is_dir() and any(covers.glob("series*.png"))
    chap_done = covers.is_dir() and any(covers.glob(f"{cid}_*.png")) if cid else True
    if series_done and chap_done:
        return
    todo = []
    if not series_done:
        todo.append(f"cover {pid}")
    if not chap_done:
        todo.append(f'cover {pid} --chapter {cid} --desc "本章画面描述"')
    _info("⚠ 封面尚未生成——Studio 项目卡与章节列表会是空白（图源两级回落全空）：\n"
          + "\n".join(f"    python3 -m kinema {c}" for c in todo))


def _warn_design_gap(project) -> None:
    """章节副本无设定、而其所属系列已有设定 → 大声警告（前哨）。

    章节继承是创建时拷贝，"先建章节后补设定集"会让本章节 characters/scene_ref
    为空——生图零参考、gen-video 就绪度节点整体跳过，而这一切不喊就毫无迹象。
    `project refs` 已会自动同步；这里兜底提醒手工编辑/历史章节的情况。"""
    if project.has_design or project.data.get("skip_design"):
        return
    ch = project.data.get("chapter") or {}
    if not ch.get("project"):
        return
    try:
        series_file = project.path.parent.parent / "project.json"
        if not series_file.is_file():
            return
        sdata = json.loads(series_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  系列文档读不到不阻断渲染
        return
    if sdata.get("characters") or sdata.get("props") or sdata.get("scene_ref"):
        print(f"  ⚠⚠ 本章节未携带设定集，但所属系列 {ch.get('project')} 已有角色/场景/道具设定！\n"
              "     当前生成将不带任何设定图参考，跨镜一致性无保障（最贵事故路径）。\n"
              f"     修复：python3 -m kinema project refs {ch.get('project')}"
              "（自动同步进全部章节）；\n"
              "     确要跳过设定集请显式 project set --skip-design。")


def stage_gen_image(project, store, router, *, profile=None, force=False, only=None,
                    candidates=None, no_moodboard=False, concurrency=None,
                    accept_existing=False, hd=False, preview_sink=None,
                    warn_cover=True):
    _warn_design_gap(project)
    _lint_gate(project, only=only)   # 调度软闸：扫全片（必须在 --only 过滤之前）
    imgdir = project.subdir("images")
    style_anchor = project.style_anchor_refs()   # 旧式全局锚点（新工程常空）；垫图改逐镜解析
    seed = project.style.get("seed")
    scene = project.scene
    # 色板/基调锚（建章时随 character_block 一起快照进 style）——仍整章恒定前置；
    # 角色文字锚则**按镜装配**（shot_cast + character_anchor_block，见 plan 循环）——
    # 绝不整块灌全员外貌清单：长篇改编 33 人 1986 字的图鉴块会挤占 60 字正文的权重，
    # 模型按字面交回「人物设定总表」。
    palette_anchor = (project.style.get("palette") or "").strip()
    has_design = project.has_design         # 有设定集 → 优先用它做参考（角色/场景/道具统一，最强一致）
    anchor_on = project.scene_ref_lock and not has_design
    per_aspect = project.image_per_aspect
    targets = project.aspects if per_aspect else [project.aspect]
    shots = project.shots
    if only:   # 只生成指定镜号（先出首镜确认再续跑）
        want = {x.strip() for x in str(only).split(",") if x.strip()}
        shots = [s for s in shots if str(s.get("id")) in want]
        if not shots:
            _info(f"没有匹配 --only {only} 的分镜。"); return
    # 一致性策略：① 有设定集 → 每镜参考「场景设定图+出场角色设定图+道具设定图」(最强)，
    #    提示词侧配 REF_BASE 契约句（外观基准/版式禁令/未出场不画）+ 出场角色绑定句；
    # ② 无设定集但 scene_ref_lock → 首镜整图锚定；③ 否则靠**按镜过滤的角色文字锚**
    #    + palette/scene + 固定 seed 前置（未点名且零命中的镜按 CHAR_BLOCK_BUDGET
    #    裁决全员兜底块——小阵容整块无害，几十人阵容宁可弃锚，见 prompts.py）。
    # 各镜 image_prompt 只写不同动作/姿态/表情/机位 → 场景角色一致、表演有变化。
    anchor: dict[str, str] = {}
    if anchor_on and shots:
        for asp in targets:
            a = project.image_for(shots[0], asp)
            if has_file(a):
                anchor[asp] = a
    mb_default = [] if no_moodboard else (project.style.get("moodboard") or [])  # 默认生效垫图（仅摘要用）
    _step(f"生图 · {len(shots)} 镜"
          + (f" · 候选宫格×{candidates}" if candidates else "")
          + (f" · 逐比例 {targets}" if per_aspect else "")
          + (" · 设定集参考" if has_design else (f" · 锚点图 {len(style_anchor)}" if style_anchor else ""))
          + (f" · 垫图 {len(mb_default)}" if mb_default else (" · 关垫图" if no_moodboard else ""))
          + (" · 锁角色" if project.characters else "") + (" · 锁场景" if scene else "")
          + (" · 首镜强锚" if anchor_on else ""))
    # ── 并发三段式（铁律见 kinema/parallel.py）──────────────────────────
    # ① 主线程定计划：状态机闸门 `_regen_gate`（只读判定）、直供闸、就绪度告警、
    #    提示词与参考图解析全在这里；旧产物归档由 `_archive_regen` 在整批计划
    #    成功后统一执行——计划期任何失败都不动盘上现状；
    # ② 并发只产文件：**一镜一件活**（不是一张图一件活）——一镜的逐比例/候选图共享
    #    同一份记账、血缘与审阅登记，捆在一起才能原子地落；
    # ③ 主线程按提交顺序回填：add_cost / lineage / review / save 全单线程。
    from .storage.media import ensure_local   # 设定图路径归一（OSS 模式下 sheet 是 URL）
    prompt_compiler = prompts_mod.PromptCompiler()
    plan: list[dict] = []
    narrowed_shots: list[str] = []   # 全员兜底块超预算被弃锚的镜（收尾一次性警告）
    for i, s in enumerate(shots):
        if transitions_mod.is_transition(s):   # 转场镜零 API 成本：字卡由合成段本地渲染
            continue
        skip, regen = _regen_gate(project, s, "image", force)   # 状态机×版本栈节点
        # preview（实发提示词预览）不吃状态机跳过：审的是「这一镜真发会是哪句」，
        # 已锁定/已有图的镜同样要看得到
        if skip and preview_sink is None:
            continue
        # 直供闸门：supplied 且无提示词的镜绝不 AI 重生（防 retake 波及烧钱覆盖资产）
        if ((s.get("gen") or {}).get("image") or {}).get("provider") == "supplied" \
                and not (s.get("image_prompt") or "").strip():
            _info(f"镜 {s.get('id')}: 直供画面（supplied）——跳过生图"
                  "（换画面用 supply 重供；要转 AI 生图先补 image_prompt）")
            continue
        if has_design:   # 就绪度警示：生图阶段只提醒，gen-video 才硬拦
            ok, missing = lineage.readiness(project, s)
            if not ok:
                _info(f"镜 {s.get('id')}: ⚠ 设定图不齐（{', '.join(missing)}）——"
                      f"一致性有风险，建议先 project refs")
        prof = _effective_profile(s, profile, project)
        prov, params = router.resolve("image", prof)
        if accept_existing and prov.name != "agent":
            raise KinemaError(
                f"--accept-existing 只用于 agent 生图路由的工单验收，当前生图 provider 是 "
                f"{prov.name}——要重编译提示词与血缘快照请置 retake 重生")
        _apply_hd(prov, hd)
        # 只有 provider 未声明更小的参考图容量时，通用 8 张清单才是实际发送上限。
        # 有 provider 专属容量时，由 _refs_of 输出实际发送/省略清单，避免两个上限混报。
        provider_cap = int(getattr(prov, "max_ref_images", 0) or 0)
        if has_design and (provider_cap <= 0 or provider_cap >= 8):
            over = project.design_ref_overflow(s)
            if over:
                _info(f"镜 {s.get('id')}: ⚠ 设定图超 8 张上限，已丢弃 {', '.join(over)}"
                      "——用 shots[].characters 收窄出场角色为道具腾名额")
        lang = getattr(prov, "prompt_lang", "zh")
        prefix, fell_back = prompts_mod.select_style_prefix(params, lang,
                                                            doc=project.data)
        if fell_back and not _warned_prefix_fallback.get(prof):
            _warned_prefix_fallback[prof] = True
            _info(f"⚠ provider 偏好英文提示词但 profile '{prof}' 缺 style_prefix_en，"
                  "已回退中文前缀（建议在 models.yaml 补齐英文位）")
        # 拼装策略（双语选材/摄影地板/防字地板/防设定表地板/剧情契约/负面句式/驳回闭环）
        # 下沉在 pipeline/prompts.py。防字地板缺省开；只有「画面里本来就该有字」的画风
        # 在 models.yaml 写 image.image_text_floor: false 关掉（game_sim / explainer）。
        # 角色文字锚**按镜装配**：本镜出场角色（shot_cast 三级解析）里，设定图随请求
        # 附上的只留绑定句、没设定图的才落全文外貌——设定图 refs 与提示词必须说同一
        # 件事，各说各话时「文字 vs 像素」的每一处不一致都是漂移指令。
        # 实发清单：provider 有参考位上限时按裁剪后的那份算，否则通用 8 张——被裁掉的
        # 角色不能再拿「以其设定图为准」的绑定句，它得落全文外貌
        srefs = ((project.design_refs_for_provider(s, provider_cap)[0] if provider_cap
                  else project.design_refs(s)) if has_design else [])
        cast, fallback_all = project.shot_cast(s)
        sheeted = set()
        if srefs:
            sref_set = set(srefs)
            for c in cast:
                sh = c.get("sheet")
                if sh and ensure_local(sh) in sref_set:
                    sheeted.add(c.get("name"))
        anchors, narrowed = prompts_mod.character_anchor_block(
            cast, sheeted=sheeted, fallback_all=fallback_all)
        # 文字锚允许小阵容在“无法判定出场者”时全员回落；视觉负面约束不跟着
        # 全员回落，避免空镜把某个未出场角色的禁穿/禁形态规则扩散到整镜。
        character_negative = prompts_mod.character_negative_block(
            cast if not fallback_all else [])
        if narrowed:
            narrowed_shots.append(str(s.get("id")))
        char_anchor = "；".join(x for x in [anchors, palette_anchor] if x)
        prompt_options = dict(
            style_prefix=prefix,
            character_block=char_anchor,
            character_negative=character_negative,
            scene=scene,
            prompt_lang=lang,
            text_floor=params.get("image_text_floor", True) is not False,
            ref_base=bool(srefs),
            cast_empty=not anchors,
            max_chars=int(getattr(prov, "max_prompt_chars", 0) or 0),
        )
        skill_revision, profile_revision = _prompt_revisions(project, prof, prov, params)
        # 一致性判定的计划期快照：invalidate 只作废**此刻已看见**的判定——运行期间
        # 指挥层并发落盘的新判定会经 save 的三方合并进内存，但那是引擎没见过的
        # 人工表态，作废它就是把「人工优先」纪律抹掉（守卫 TestConcurrentVerdictWins）
        plan.append({"i": i, "shot": s, "prov": prov, "prompt_options": prompt_options,
                     "params": params,   # preview 的对照语种要按它重取另一语种前缀
                     "skill_revision": skill_revision, "profile_revision": profile_revision,
                     "regen": regen, "cn_seen": bool(s.get("consistency"))})

    if narrowed_shots:
        _info(f"⚠ 阵容庞大（{len(project.characters)} 角色）且镜 "
              f"{', '.join(narrowed_shots)} 未点名出场角色——全员外貌块超预算已弃用，"
              "这些镜只靠正文与参考图约束；请补 shots[].characters（空表 []=明确无人出场）")

    if not plan:
        # 空计划＝本章图已全出（或整章 supplied/omt）——收尾提醒必须在这条出口也发。
        # 漏在这里等于 agent 出图模式永远收不到提醒：首轮抛「工单已开」，画完重跑
        # 时全镜已登记，正好走空计划，末尾那次调用一次都执行不到。
        if warn_cover:
            _warn_cover_missing(project)
        return

    # agent 工单模式：本轮开单前清掉旧单——工单永远只反映「本轮还缺的图」，
    # 上一轮的残单混进来会让 agent 重画已经验收登记的镜。
    from .providers.image.agent import ORDER_BASENAME as _AGENT_ORDER, \
        PENDING_MARK as _AGENT_PENDING, has_pending_order as _AGENT_HAS_ORDER, \
        prepare_order as _AGENT_PREPARE_ORDER
    if preview_sink is None and any(x["prov"].name == "agent" for x in plan):
        _AGENT_PREPARE_ORDER(imgdir / _AGENT_ORDER)

    _warned_refkind: dict = {}
    _warned_refcap: set[tuple[str, str]] = set()

    def _refs_of(item, asp):
        """设定集→场景/角色/道具设定图；垫图默认全局套用（镜级 shots[].refs 可覆盖）。
        **只读**（读文档字段、不改），故工作线程里调它是安全的。

        `ref_kind="character"` 的 provider（subject_reference 一类）单独分流：
        只发出场角色设定图，挑不到就不发并点名一次——盲塞首张的话，配了全局
        场景图的项目发出去的恒是 SCENE 全景被标成 type=character。"""
        s = item["shot"]
        prov = item["prov"]
        if getattr(prov, "ref_kind", "any") == "character":
            crefs = project.character_sheet_refs(s)
            if not crefs and not _warned_refkind.get(prov.name):
                _warned_refkind[prov.name] = True
                _info(f"  ⚠ {prov.name} 只收「角色主体」参考（type=character）——"
                      "无出场角色设定图的镜不发参考图（场景/垫图会被当角色特征学走）")
            cap = int(getattr(prov, "max_ref_images", 0) or 0)
            return crefs[:cap] if cap else crefs
        mb = [] if no_moodboard else project.moodboard_refs(s)
        if has_design:
            cap = int(getattr(prov, "max_ref_images", 0) or 0)
            if cap:
                design, omitted = project.design_refs_for_provider(s, cap)
                refs = design + mb
                omitted = [*omitted, *refs[cap:]]
                refs = refs[:cap]
                marker = (prov.name, str(s.get("id")))
                if omitted and marker not in _warned_refcap:
                    _warned_refcap.add(marker)
                    _info(f"  ⚠ {prov.name} 镜 {s.get('id')} 参考图上限 {cap} 张，"
                          f"实际发送 {', '.join(Path(x).name for x in refs)}；"
                          f"省略 {', '.join(Path(x).name for x in omitted)}"
                          "（已按场景→角色→本镜高频道具优先级选择）")
                return refs
            return (project.design_refs(s) + mb)[:8]
        anchor_refs = [anchor[asp]] if (item["i"] > 0 and anchor_on and asp in anchor) else []
        return (style_anchor + mb + anchor_refs)[:8]

    def _seal_prompt(items):
        """在计划期封装 Prompt；引用清单已确定后不在工作线程重新拼装。"""
        for item in items:
            refs = []
            for asp in targets:
                refs.extend(("image_reference", f"{asp}:{Path(str(path)).name}", path)
                            for path in _refs_of(item, asp))
            envelope = prompt_compiler.image(
                item["shot"], references=_prompt_references(refs),
                skill_revision=item["skill_revision"],
                profile_revision=item["profile_revision"],
                **item["prompt_options"])
            item["envelope"] = envelope
            item["prompt"] = envelope.prompt

    # preview：整批封装后即返回——绝不 _archive_regen/_mark_wip（两者都动盘上
    # 状态），不进工作线程。首镜强锚的后续镜按当前在盘锚点封装（真发时才逐镜续封）
    if preview_sink is not None:
        _seal_prompt(plan)
        for item in plan:
            env = item["envelope"]
            # 中英对照编译（展示用，不发送）：另一语种要换对应的画风前缀，
            # 不做 provider 字数上限强杀；references 不进文本，对照稿不带
            lang0 = item["prompt_options"]["prompt_lang"]
            alt_lang = "en" if lang0 != "en" else "zh"
            prefix_alt, _fb = prompts_mod.select_style_prefix(
                item["params"], alt_lang, doc=project.data)
            env_alt = prompt_compiler.image(
                item["shot"], skill_revision=item["skill_revision"],
                profile_revision=item["profile_revision"],
                **{**item["prompt_options"], "prompt_lang": alt_lang,
                   "style_prefix": prefix_alt, "max_chars": 0})
            preview_sink.append({
                "id": item["shot"].get("id"), "prompt": env.prompt,
                "positive": env.positive, "negative": env.negative,
                "lang": lang0,
                "alt": {"lang": alt_lang, "positive": env_alt.positive,
                        "negative": env_alt.negative},
                "fingerprint": env.fingerprint,
                "provider": item["prov"].name,
                "model": getattr(item["prov"], "model", None)})
        return
    # 首镜强锚下，后续镜的引用要等首镜回填后才确定；其余路径一次封装整批。
    _seal_prompt(plan[:1] if anchor_on else plan)
    if not candidates:                        # 候选不占画布，定稿（pick）时才归档
        _archive_regen(project, [it for it in plan if it["prov"].name == "agent"], "image")
        for it in plan:                       # 归档理由在置 wip 前取：wip 会盖掉 retake 态
            it["regen_reason"] = _regen_reason(it["shot"], "image")
    _mark_wip(project, plan, "image")   # Studio 忙态：本批全部进入「生成中」

    def _work(item):
        """工作线程：只发请求、只写自己那几个产物路径，**一行文档都不碰**。
        返回本镜的产物清单交主线程回填。"""
        s, prov, prompt, regen = item["shot"], item["prov"], item["prompt"], item["regen"]
        out = {"images": {}, "candidates": [], "cost": 0.0, "generated": False,
               "reused": False}
        pending = 0

        def _accepted(res):
            """agent 验收带回的附注：工单提示词与本次编译稿不同、尺寸类告警。"""
            m = getattr(res, "meta", None) or {}
            if m.get("order_prompt"):
                out["order_prompt"] = m["order_prompt"]
            out.setdefault("warnings", []).extend(m.get("warnings") or [])

        def _gen(*a, **kw):
            """agent 工单 pending 不中止本镜循环：多比例/多候选要**一轮把单开全**，
            首个 pending 就弃剩余项会让 agent 每画一张重跑一趟。真失败照抛。"""
            nonlocal pending
            try:
                return prov.generate(*a, **kw)
            except ProviderError as e:
                if _AGENT_PENDING not in str(e):
                    raise
                pending += 1
                return None

        def _flush_pending():
            if pending:
                raise ProviderError(f"{_AGENT_PENDING}（工单 {_AGENT_ORDER} · {pending} 项）")

        if candidates:            # 宫格候选：一镜出 N 张待选，人点选后才定稿
            asp = project.aspect
            w, h = store.canvas(asp)
            refs = _refs_of(item, asp)
            for k in range(1, int(candidates) + 1):
                cp = candidates_mod.candidate_path(project, s, k)
                if not regen and cp.is_file() and prov.name != "agent":
                    # 断点续跑：上一轮部分失败时**已付费的候选不重买**（只读判断，线程安全）
                    out["candidates"].append(str(cp)); out["reused"] = True
                    continue
                res = _gen(prompt, str(cp), ref_images=refs,
                           seed=candidates_mod.seed_for(seed, k),
                           width=w, height=h,
                           label=f"SHOT {s['id']} 候选{k}/{candidates}")
                if res is None:
                    continue
                out["candidates"].append(res.path)
                out["cost"] += res.cost
                out["generated"] = True
            _flush_pending()
            return out
        if per_aspect:
            for asp in targets:
                cur = (s.get("images") or {}).get(asp)
                pending_order = (prov.name == "agent" and cur
                                 and _AGENT_HAS_ORDER(cur))
                accept = accept_existing and prov.name == "agent"
                if not regen and has_file(cur) and not pending_order and not accept:
                    out["images"][asp] = cur; out["reused"] = True
                    continue
                if accept and not regen and has_file(cur) and not pending_order:
                    out["unchanged"] = True      # 只重编译登记，画布像素未变
                dst = imgdir / f"shot_{s['id']}_{aspect_tag(asp)}.png"
                if (not regen and prov.name != "agent"
                        and dst.is_file() and dst.stat().st_size > 0):
                    # 断点续跑：已付费落盘但未登记（回填前中断/同批重试）——
                    # 直接登记不重买，与候选宫格同一条纪律
                    out["images"][asp] = str(dst); out["salvaged"] = True
                    continue
                w, h = store.canvas(asp)
                res = _gen(prompt, str(_staged(dst, regen and prov.name != "agent")),
                           ref_images=_refs_of(item, asp), seed=seed,
                           width=w, height=h, label=f"SHOT {s['id']} {asp}")
                if res is None:
                    continue
                _accepted(res)
                out["images"][asp] = res.path
                out["cost"] += res.cost
                out["generated"] = True
            _flush_pending()
            return out
        asp = project.aspect
        # agent 工单完成后的验收例外：旧章节仍指向同一目标路径时，不能被
        # 普通「已有图片复用」短路；让 provider 以零成本回填本轮 envelope/血缘。
        pending_order = (prov.name == "agent" and s.get("image")
                         and _AGENT_HAS_ORDER(s.get("image")))
        accept = accept_existing and prov.name == "agent"
        if not regen and has_file(s.get("image")) and not pending_order and not accept:
            out["images"][asp] = s["image"]; out["reused"] = True
            return out
        if accept and not regen and has_file(s.get("image")) and not pending_order:
            out["unchanged"] = True          # 只重编译登记，画布像素未变
        dst = imgdir / f"shot_{s['id']}.png"
        if (not regen and prov.name != "agent"
                and dst.is_file() and dst.stat().st_size > 0):
            # 断点续跑：同 per_aspect——盘上已付费的图直接登记不重买
            out["images"][asp] = str(dst); out["salvaged"] = True
            return out
        w, h = store.canvas(asp)
        res = prov.generate(prompt, str(_staged(dst, regen and prov.name != "agent")),
                            ref_images=_refs_of(item, asp), seed=seed,
                            width=w, height=h, label=f"SHOT {s['id']}")
        _accepted(res)
        out["images"][asp] = res.path
        out["cost"] += res.cost
        out["generated"] = True
        return out

    failed: list = []
    budget_stop: dict = {"err": None}   # 预算断闸：停派新活，不打断本批收尾

    def _apply(d: parallel.Done):
        """主线程：回填 + 记账 + 血缘 + 审阅 + 落盘。**唯一改文档的地方。**"""
        item = d.meta
        s, prov, i = item["shot"], item["prov"], item["i"]
        if not d.ok:
            mark(s, "failed")
            _unmark_wip(s, "image", item)
            project.save()
            failed.append(d)
            _info(f"镜 {s['id']}: ✗ {d.message}")
            return
        r = d.value or {}
        if candidates:
            # 候选不占画布：只登记待选品与本批快照（pick 定稿时搬进 gen.image）。
            # 画布态（归档、血缘基线、一致性判定、存量片段）全部留到 pick 定稿时动
            s["image_candidates"] = list(r["candidates"])
            s.pop("image_picked", None)
            mark(s, "done")
            voided = None
            if r.get("generated"):
                s.setdefault("gen", {})["image_candidates"] = {
                    "prompt": item["prompt"], "seed": seed, "provider": prov.name,
                    "envelope": item["envelope"].as_dict(),
                    "cost": round(r["cost"], 4), "count": len(r["candidates"]),
                    # 指纹在出候选时记：pick 定稿时设定图可能已改版，按当下文件重算会漏判
                    "refs": {p: lineage.fingerprint(p)
                             for p in _refs_of(item, project.aspect) if lineage.fingerprint(p)}}
                # 待审在这里的含义是「待点选」：画布还没有图时候选就是下一步；画布
                # 已有图且未重做的，审阅态属于那张在盘的画布，不动
                if item["regen"] or not has_file(s.get("image")):
                    review.mark_generated(s, "image")
                else:
                    _unmark_wip(s, "image", item)
            else:
                _unmark_wip(s, "image", item)
            if r.get("cost", 0) > 0:
                try:
                    project.add_cost("image", r["cost"])
                except KinemaError as e:
                    budget_stop["err"] = e
            project.save()
            _info(f"镜 {s['id']} [{prov.name}]: {len(r['candidates'])} 张候选 → 宫格待选"
                  f"（Studio 点选或 `pick --shot {s['id']} --use 编号`）"
                  + ("  (旧一致性判定已作废)" if voided else ""))
            return
        else:
            if r.get("generated") and item["regen"] and prov.name != "agent":
                # 新图已落临时名：先归档旧画布再替换，画布字段任何时刻都指向在盘文件
                v = versioning.archive(project, s, "image", reason=item["regen_reason"],
                                       params=(s.get("gen") or {}).get("image"))
                if v:
                    _info(f"镜 {s['id']}: image 旧版已归档 v{v:03d}")
                for asp, pth in list(r["images"].items()):
                    p = Path(pth)
                    if p.name.endswith(".new.png"):
                        final = p.with_name(p.name[:-len(".new.png")] + ".png")
                        os.replace(p, final)
                        r["images"][asp] = str(final)
            if per_aspect:
                s.setdefault("images", {}).update(r["images"])
                s["image"] = (s["images"].get(project.aspect)
                              or next(iter(s["images"].values()), None))
            elif r["images"]:
                s["image"] = next(iter(r["images"].values()))
            # 首镜强锚：锚点图必须在后续镜发请求**之前**就位，故首批只跑首镜（见下）
            if anchor_on:
                for asp, pth in r["images"].items():
                    if i == 0:
                        anchor[asp] = pth
        mark(s, "done")
        voided = None
        # 三种「画布有了新登记」的来源走同一条登记链：本轮生成、盘上捡回（上一轮
        # 已付费但回填前中断）、agent 路由的重编译验收。前两种画布内容是新的；
        # 验收时画布未变的（unchanged）只刷新提示词与血缘快照，不动判定与片段
        if r.get("generated") or r.get("salvaged"):
            s.setdefault("gen", {})["image"] = {
                "prompt": item["prompt"], "seed": seed, "provider": prov.name,
                "envelope": item["envelope"].as_dict(),
                "cost": round(r["cost"], 4),
                "version": versioning.current_version(s, "image")}
            if r.get("order_prompt"):
                # agent 按开单时的稿画的，与本次编译稿已不同：登记的提示词要说真话
                s["gen"]["image"]["order_prompt"] = r["order_prompt"]
                _info(f"镜 {s['id']}: ⓘ 交付图按开单时的提示词绘制，作者字段此后有改动"
                      "——要按新稿重画请置 retake")
            for w in r.get("warnings") or []:
                _info(f"镜 {s['id']}: ⚠ {w}")
            lineage.record_refs(s, "image", _refs_of(item, project.aspect))   # 血缘登记
            lineage.clear_stale(s)
            if not r.get("unchanged"):
                # 旧标记失效：一致性判定判的是上一版图，新图还没人判过。只作废
                # 计划期已看见的判定（cn_seen）——运行期间并发落盘的人工判定不抹
                voided = consistency_mod.invalidate(s, "image") if item["cn_seen"] else None
                outcome = lineage.retake_clip_for_image(s)
                if outcome == "locked":
                    _info(f"镜 {s['id']}: ⚠ 片段按旧版分镜图生成且已锁定(done)——"
                          "要跟上新图请解锁后置 retake 重生")
                elif outcome == "retake":
                    _info(f"镜 {s['id']}: 片段已按旧版分镜图生成 → clip 置 retake"
                          "（下次 gen-video 自动重生，旧版入版本栈）")
            review.mark_generated(s, "image")
        else:
            _unmark_wip(s, "image", item)   # 复用/跳过：wip 是误报，恢复原态
        # **记账放在登记之后**：`add_cost` 超限时是「先入账再抛」，抛在登记前面就会
        # 把这张已生成已付费的图丢掉登记——重跑时同一张再买一次。捕获后只置停派标志，
        # 不打断本批收尾（在飞的钱已经花了，结果必须收），整批跑完再抛。
        if r.get("cost", 0) > 0:   # 单价未配置(=0)不入账，与 tts/music 同口径
            try:
                project.add_cost("image", r["cost"])
            except KinemaError as e:
                budget_stop["err"] = e
        project.save()          # 逐镜 checkpoint（主线程串行，无竞态）
        if r.get("reused"):
            _info(f"镜 {s['id']}: 已存在，跳过")
            return
        if r.get("salvaged"):
            _info(f"镜 {s['id']}: 盘上已有未登记的产物，直接登记 → 待审")
            return
        tag = "  (场景锚点)" if (i == 0 and anchor_on) else (
            "  (参考首镜)" if (i > 0 and anchor_on and anchor) else "")
        _info(f"镜 {s['id']} [{prov.name}]: ✓{tag}"
              + (f"  v{versioning.current_version(s, 'image'):03d} → 待审"
                 if r.get("generated") else "")
              + ("  (旧一致性判定已作废)" if voided else ""))

    def _tasks(items):
        return [parallel.Task(key=f"shot:{x['shot']['id']}",
                              run=(lambda it=x: _work(it)),
                              label=f"镜 {x['shot']['id']}", meta=x) for x in items]

    workers = parallel.resolve_workers(concurrency)
    if workers > 1:
        _info(f"并发生图 · {workers} 张同时")
    # **首镜强锚是一条真实的串行依赖**：后续镜要拿首镜成品当参考图，
    # 并发发出去时首镜还没落地 → 锚点为空、一致性直接失效。故拆两波：
    # 首镜单独跑完并回填 anchor，其余再并发。
    if anchor_on and plan:
        parallel.run(_tasks(plan[:1]), workers=1, on_done=_apply,
                     should_stop=lambda: budget_stop["err"] is not None,
                     on_progress=parallel.progress_printer("首镜"))
        rest = plan[1:]
        if rest and (not failed or all(_AGENT_PENDING in d.message for d in failed)):
            _seal_prompt(rest)
    else:
        rest = plan
    def _all_agent_pending(ds):
        return bool(ds) and all(_AGENT_PENDING in d.message for d in ds)

    # agent 待产图不是真失败：首镜「开了工单」也要把其余镜发下去，让工单一次
    # 开全——否则 agent 每画一张就要重跑一轮，一章 10 镜来回 10 趟。
    if rest and (not failed or _all_agent_pending(failed)):
        parallel.run(_tasks(rest), workers=workers, on_done=_apply,
                     should_stop=lambda: budget_stop["err"] is not None,
                     on_progress=parallel.progress_printer("生图"))
    elif rest:
        _info("首镜失败，后续镜不再发出（锚点缺失会让整章一致性失效）")

    # 断闸/首镜失败后未派活的镜还挂着 wip——那是误报（根本没生成过），统一恢复原态
    for it in plan:
        _unmark_wip(it["shot"], "image", it)
    project.save()

    if budget_stop["err"] and not failed:
        raise budget_stop["err"]
    if failed:
        if _all_agent_pending(failed):
            raise KinemaError(
                f"{len(failed)} 镜待 agent 产图——工单已开: {imgdir / _AGENT_ORDER}\n"
                "  用你的原生生图能力按工单逐条产图（prompt → path，尺寸"
                " width×height，ref_images 供垫图；首条通常是全章视觉锚，先画它"
                "并让后续镜风格向它看齐），完成后重跑同一条 gen-image 即自动验收登记。")
        # 失败镜已各自 mark(failed) 并落盘；其余镜的成果全部保住，这里统一抛出
        raise KinemaError(
            f"{len(failed)} 镜生图失败："
            + "、".join(f"{d.label}（{d.message}）" for d in failed)
            + "\n  已成功的镜已登记落盘，重跑同一条命令会自动跳过它们。")
    if warn_cover:
        _warn_cover_missing(project)


def _gate_4k(resolution, *, dry_run=False, yes=False, mock=False):
    """4K 二次确认节点：口头要 4K（CLI --resolution 4k）可越过配置默认档，
    但**正式生成必须 --yes 显式授权**；dry-run（看预估不花钱）与 mock（离线）放行。
    不带 --resolution 走 config 默认档（各别名的 `resolution`），不触发本节点。"""
    if resolution == "4k" and not (dry_run or yes or mock):
        raise KinemaError(
            "4K 为高成本档（总价≈2×1080p·并发独享 1·RPM 15/分钟，多镜连跑会排队），"
            "需二次确认后才会调用生成：\n"
            "  ① 先看报价：gen-video … --resolution 4k --dry-run（按 4K 档单价预估）\n"
            "  ② 确认无误后加 --yes 正式生成：gen-video … --resolution 4k --yes\n"
            "（不带 --resolution 则按该别名的配置默认档生成，无需确认）")


def _apply_hd(prov, hd: bool) -> None:
    """`--hd`：本次按 provider 的像素上限出图（仅本次进程，不落盘）。

    与 `--resolution` 同制——provider 自己按配置的上限在保持宽高比的前提下放大，
    这里只把开关打开。provider 没声明上限（`max_pixels` 缺省 0）时点名也不生效，
    照实说明而不是替它猜一个尺寸。"""
    if not hd:
        return
    cap = int(getattr(prov, "max_pixels_cap", 0) or getattr(prov, "MAX_PIXELS", 0) or 0)
    if cap <= 0:
        _info(f"⚠ --hd 对 {getattr(prov, 'name', '该 provider')} 不生效"
              "（未声明像素上限 max_pixels_cap）——本次按画布尺寸出图")
        return
    prov.max_pixels = cap


def _apply_resolution(prov, resolution):
    """CLI --resolution 运行时覆盖 provider 的配置默认档（仅本次进程，不落盘）。
    provider 的 generate 参数串、billable_seconds、effective_price_per_second
    都读 prov.resolution——改这一处，预估与实际计费自动同源。"""
    if not resolution:
        return
    if hasattr(prov, "resolution"):
        prov.resolution = resolution
    elif not getattr(_apply_resolution, "_warned", False):
        _apply_resolution._warned = True
        _info(f"[!] {prov.name} 没有分辨率档这个概念，--resolution 已忽略")


def _chain_break_note(nxt, why: str, *, sent_last: bool, v2v: bool,
                      ref_mode: bool = False, can_last: bool = True) -> str:
    """本镜为什么没有末帧（面向人的短语）；发了末帧、走 V2V、或本章不衔接时返回空串。

    五种原因收在一处：型号无末帧槽 / 全能参考 / 转场断链 / 末镜 / 下一镜缺图。
    前两种优先——链结构上明明可衔接，落到「下一镜缺图」的措辞就是误导。
    措辞一律取 `framechain.BREAK_ZH`——保证 dry-run 清单与真发日志对同一原因
    给出同一说法。
    """
    if sent_last or v2v or why == "off":
        return ""
    if not can_last:
        return framechain.BREAK_ZH["no_last_frame"]
    if ref_mode:
        return framechain.BREAK_ZH["ref_mode"]
    if nxt is not None and why == "":   # 结构上该衔接，卡在下一镜没图
        return framechain.BREAK_ZH["no_image"]
    return framechain.BREAK_ZH.get(why, "")


def _sync_island_seams(project, chain: bool, v2v_on: bool,
                       control_on: bool = False) -> dict:
    """孤岛镜两侧自动落无缝转场（判据与实现全在 `framechain.sync_seams`）。

    **落盘而不是只在内存里算**：转场镜是时间轴的一部分（tts 补静音占位、字幕对齐、
    compose 取相邻片段的冻结帧都按 `shots[]` 走），只在渲染时虚拟插入会让盘上的
    章节文档与成片不是同一份东西。改动逐条打印，不静默改用户的章节。
    """
    r = framechain.sync_seams(project.shots, chain, v2v=v2v_on, control=control_on)
    if not (r["added"] or r["removed"]):
        return r
    for nid, prev_id, next_id, why in r["added"]:
        _info(f"↔ 镜{prev_id}→镜{next_id} 之间自动插入无缝转场(镜{nid})："
              f"{framechain.BREAK_ZH.get(why, why)}——该处焊不上，改走 0.1s 软切")
    if r["removed"]:
        _info(f"↔ 已撤销不再需要的自动无缝转场：镜 "
              f"{'、'.join(str(x) for x in r['removed'])}（相关镜已不走参考模式）")
    project.save()
    return r


_warned_delta_skeleton: dict = {}
# 「provider 不支持参考视频」每个 provider 只喊一次（逐镜混画风时可能路由到多家）
_warned_v2v: dict = {}
_warned_ski: dict = {}   # 「provider 不支持额外参考图」每 provider 只喊一次
_warned_lf: dict = {}    # 「provider 不支持末帧」每 provider 只喊一次
_warned_tail: dict = {}  # 「provider 不支持尾帧接力」每 provider 只喊一次


def _warn_no_motion_design(shot, flf2v: bool = False):
    """该镜**一笔运动设计都没有**时点名一次（判据同源 `prompts.video_delta_missing`）。

    **这不是回退到 image_prompt**——整条复述首帧会引入复述式漂移。缺 video_prompt
    时引擎按 action/end_state/light_shift 拼 delta 骨架（两语种都发），六者全空才落
    固定兜底句。兜底句平淡但不引入首帧复述式漂移；该镜等于没有运动设计，故逐镜
    点名并给出补法。**只在真落兜底句时才喊**：填了 delta 的镜照喊会让日志与实发提示词相反。

    兜底句**分两种**，日志必须跟实发的那一条对上（否则用户照着日志去找，找不到）：
    链上镜（首尾帧都已 pin）落的是"沿最短路径过渡到末帧"，不是"保持不变"。"""
    tail = ("已落首尾帧兜底句「沿最短自然路径过渡到末帧」" if flf2v
            else "已落固定兜底句「保持不变 + 轻微自然运动」")
    _info(f"镜 {shot.get('id')}: 无运动设计（video_prompt 与 action/end_state/"
          f"light_shift 全空），{tail}")
    if not _warned_delta_skeleton.get("hint"):
        _warned_delta_skeleton["hint"] = True
        _info("  ↳ 视频请求恒带这一镜的分镜图，提示词只写增量：补 video_prompt，"
              "或填结构化的 action(动作) / end_state(终态) / light_shift(光线变化)")


# ---------- previz（3D 导演预演）→ Seedance 条件化 ----------
def _ws_root_of(project):
    """从章节文件反推工作区根：`<ws>/<pid>/chapters/<cid>.json`；散装 --project 回退发现链。

    OSS 的对象 Key = 前缀/**工作区相对路径**，ws_root 认错会让 `key_for()` 返回
    None（文件"不在工作区内"）从而拒绝上传——V2V 直接失效且原因很不直观。
    """
    p = project.path
    if p.parent.name == "chapters":
        return p.parent.parent.parent
    return find_workspace()


def _v2v_enabled(project, flag) -> bool:
    """V2V 总开关（**opt-in，两条路都得显式**）：`gen-video --previz` 或项目顶层 `previz_v2v`。

    默认关是既定决策：参考视频会让每次调用多计一段输入视频秒（token 计费），
    静默开启 = 静默改成本，违背本仓库「烧钱节点必须显式」的一贯纪律。
    """
    return bool(flag or project.data.get("previz_v2v"))


def _v2v_shot(s) -> bool:
    """本镜是否有可发的 previz 参考片——判据真源 `previz.v2v_shot`。

    这里只留别名：`framechain` 的孤岛判据要用同一条，而 pipeline 不该反向依赖 cli。
    """
    return previz_mod.v2v_shot(s)


def _control_enabled(project, flag) -> bool:
    """深度控制视频总开关（**opt-in，两条路都得显式**）：`gen-video --control`
    或章节顶层 `control_video`。默认关的理由与 `_v2v_enabled` 逐字相同——
    输入视频秒同样入账，静默开启 = 静默改成本。"""
    return bool(flag or project.data.get("control_video"))


def _ref_video(s, *, previz_on=False, control_on=False):
    """本镜真会发出去的参考视频：`(来源, 本地路径, 输入秒数)`，没有则 None。

    **这是参考视频的唯一投影**——报价、dry-run 清单、逐镜行文案与请求体四处
    共用它。分成四份手写的后果不是报错：`_plan_cost` 与 dry-run 循环本就是两份
    独立抄写，只教会其中一份新来源，事前闸预留的额度就少于真实账单，而整套
    测试照常全绿。

    来源由 `sketchboard.active_guide` 一处仲裁（previz > control > sketch），
    两条谓词已互斥；两个总开关各自独立，故一开一关时另一路照常不发。
    """
    if previz_on and previz_mod.v2v_shot(s):
        return ("previz", s["previz"], previz_mod.previz_seconds(s))
    if control_on and control_mod.control_shot(s):
        return ("control", s["control"], control_mod.control_seconds(s))
    return None


def _ref_video_url(shot_id, path, ws_root, *, mock=False):
    """参考视频本地路径 → Seedance 可拉取的**公网 URL**（复用既有 OSS 层，零新造）。

    视频参考不接受 base64/data-url/本地路径（`seedance._vid_url` 会抛错），故必须
    先上云。已是 URL 的直接透传（`oss sync` 过的章节）；`upload()` 是幂等 put，
    重复登记同一段参考视频只是覆盖同一个 Key。

    `mock=True` 时**不上云、直接给本地路径**——mock provider 不读这个值，
    上云徒增 OSS 密钥依赖，违背「离线链路零云依赖」的既定约束。
    """
    from .storage.media import get_media_store, is_url
    if is_url(path) or mock:
        return path
    ms = get_media_store(ws_root)
    # 判据是**能力齐备**而不是「上云是默认档」：参考视频在协议层只收公网 URL，
    # 为这一条链接把整份工作区的图都搬上云是冗余的（`media.backend` 保持 local，
    # 只有这一步按需上传；文档里存的仍是本地路径）
    if not ms.configured:
        raise KinemaError(
            f"镜 {shot_id} 要发参考视频(V2V)，但 OSS 未配置——"
            "Seedance 只接受公网 URL 的视频参考。\n"
            "  ① config/storage.yaml 的 media 段选 provider（如 aliyun，桶需 public-read）"
            "——**backend 不必改成 oss**，其余媒体照常留在本地\n"
            "  ② 桶、区域与密钥都走密钥链：KINEMA_OSS_BUCKET / KINEMA_OSS_REGION / "
            "KINEMA_OSS_ACCESS_KEY / KINEMA_OSS_SECRET_KEY"
            "（config/secrets.yaml 或同名环境变量；桶名不进随仓库分发的 storage.yaml）\n"
            "  ③ 或去掉 --previz / --control、关掉章节的 `previz_v2v` / `control_video`，"
            "退回纯首帧+运镜文案（T1–T3 通用层，Seedance/Veo 通吃）")
    return ms.upload(path)


# ---------- 预留额度（M15 事前闸）：纯只读预演 → 对账 → 决定发不发 ----------
def _salvageable_clip(project, shot_id, aspect) -> bool:
    """本比例的片段是否已在盘且完整：上一轮已按秒付费、落盘但回填前中断。

    真跑与事前闸必须共用它——判据分叉会让报价算上不需要再买的比例，
    预算够的批次反而被整批拦死。只拼路径不建目录：预演层零副作用。
    半截 mp4 缺 moov 读不出时长，按未产出处理。
    """
    p = project.workdir / "gen_clips" / f"shot_{shot_id}_{aspect_tag(aspect)}.mp4"
    if not p.is_file():
        return False
    try:
        return probe_duration(p) > 0
    except Exception:  # noqa: BLE001  读不出 = 半截文件，照常重生成
        return False


def _will_burn(project, shots, targets, force, *, ignore_refs=False):
    """本次真跑会实际发出去的清单：`[(镜, [比例…]), …]`。**纯只读、零副作用**。

    与 `_regen_gate` 保持同款判定但不复用——事前闸在「一次都没发」的前提下
    静默算账，闸门的逐镜跳过打印会把预演日志混进真跑日志。

    四道跳过闸与真跑循环逐条对齐，否则预估虚高会把 budget 本来够的项目拦死：
      ① 状态机（同 `_regen_gate` 的只读判定）：转场镜/弃用(omt)/已通过(done)锁定 → 跳过；
         retake 视同该镜 force。
      ② 就绪度（`lineage.readiness`）：设定图不齐的镜真跑也不会发。
      ③ 已登记片段：非重生时逐比例查 `clips[比例]` 在不在盘——断点续跑的核心，
         照抄 dry-run 的「只过 omt/transition」会把已产出的镜也算进预估。
      ④ 盘上待登记片段（`_salvageable_clip`）：付过费但上轮回填前中断的那些，
         真跑零成本捡回，报价里同样不该出现。

    **本清单是上界，不是等式**：真跑还会跳掉实发稿审阅锁不一致的镜
    （`gen.clip_approval.sha`）。那道闸要先把整条实发提示词编译出来才算得出 sha，
    而编译途中会上传 previz、预热锚定音——预演层复算不出，也不该有这些副作用。
    虚高只会少发不会多发，故取上界。

    比例维度是**逐比例一次调用**（真跑对 targets 里每个比例各 generate 一次），
    所以返回的是逐镜的比例**列表**而非布尔——调用方按 `len(aspects)` 计次计费。
    """
    plan: list[tuple[dict, list[str]]] = []
    for s in shots:
        if transitions_mod.is_transition(s):
            continue
        if review.is_omitted(s) or review.is_locked(s, "clip"):
            continue
        regen = bool(force) or review.needs_retake(s, "clip")
        if not ignore_refs and project.has_design:
            ok, _missing = lineage.readiness(project, s)
            if not ok:
                continue
        clips = s.get("clips") or {}
        asps = [a for a in targets
                if regen or not (has_file(clips.get(a))
                                 or _salvageable_clip(project, s.get("id"), a))]
        if asps:
            plan.append((s, asps))
    return plan


def _plan_cost(project, plan, prov, *, mode, native, adir, v2v=False, control=False,
               sends_last=None):
    """把 `_will_burn` 清单折成 `(计费总秒数, 调用次数, 最贵单次秒数)`。纯只读。

    秒数口径与真发同源三层：`voicecast.request_seconds` 取净画面秒数
    → `provider.billable_seconds` 按各厂档位钳制 → **V2V 时另加输入视频秒**
    （`provider.input_video_seconds`，2.0 是 token 计费、输入视频同样入账）。
    **总秒数乘比例数**——真跑对每个比例各生成一次，只累加一份是系统性低估
    （双比例时报价只有实际的一半）。

    `v2v` / `control` 由调用方按 `_v2v_enabled` / `_control_enabled` × provider 能力
    算好传入，本函数不自己判——自判必然与真发分叉（真发那边还要过 native 模式闸
    与逐镜参考视频在盘检查）。
    `sends_last` 同理是调用方给的镜级谓词（该镜是否发末帧）：末帧参与
    Veo 的取档（插值强制 8s），预估不吃它就会低于实际计费。
    """
    total = 0
    calls = 0
    max_n = 0
    for s, asps in plan:
        dur = voicecast.request_seconds(s, mode, adir=adir) \
            or float(project.data.get("duration", 5)) or 5
        n = prov.billable_seconds(dur, dubbed=not native,
                                  last_frame=bool(sends_last and sends_last(s)))
        rv = _ref_video(s, previz_on=v2v, control_on=control)
        if rv:
            n += prov.input_video_seconds(rv[2])
        total += n * len(asps)
        calls += len(asps)
        max_n = max(max_n, n)
    return total, calls, max_n


def _preflight_spend(project, plan, prov, *, mode, native, adir, targets,
                     confirm_spend=False, auto=False, v2v=False, control=False,
                     sends_last=None):
    """花钱前的预留额度节点：整批预估 vs 台账余额，不够就**一次都不发**。

    **只在内存算，不落盘**——尤其不碰 `cost_estimate.video`（那是 dry-run 的审阅
    快照 + ledger 预估侧 + 交付 manifest 的唯一来源，覆写会让「预估 vs 实际」失真）。

    两级裁决（见 `budget.verdict`）：
      · 硬超上限（已花+本批 > `budget`）→ 任何模式都拦，含 `run`/`--auto`：
        放行等于必然爆预算，而 `add_cost` 事后闸只会在烧掉一半之后才断。
      · 单笔超阈（最贵一次 > `budget_per_call`）→ 交互式命令要 `--confirm-spend`
        二次确认；`run`/`--auto` 下告警放行（否则一条龙死在这里且无解锁路径）。
    """
    price = getattr(prov, "effective_price_per_second", 0) \
        or getattr(prov, "price_per_second", 0) or 0.0
    if price <= 0:      # 单价未配置(=0)：不入账也不预留，与 add_cost 的"肯定性零"口径同源
        return
    total, calls, max_n = _plan_cost(project, plan, prov, mode=mode, native=native,
                                     adir=adir, v2v=v2v, control=control,
                                     sends_last=sends_last)
    if not calls:
        return
    est = round(total * price, 2)
    v = budget_mod.verdict(project.data, est, round(max_n * price, 2))
    head = (f"预留额度: 本批 {len(plan)} 镜 × {len(targets)} 比例 = {calls} 次调用 "
            f"≈ {total}s ≈ ¥{est:.2f}（{prov.name} ¥{price}/s"
            + ("·含 V2V 输入视频秒" if v2v else "") + "）")
    if v["budget"] is None:
        _info(head + " · 未设 budget（不设限）")
    else:
        _info(head + f" · 已花 ¥{v['spent']:.2f} / 预算 ¥{v['budget']:.2f}"
                     f" · 余 ¥{v['remaining']:.2f}")
    if v["over_budget"]:
        raise KinemaError(
            f"⊘ 预留额度不足，本批**一次都没有发出**（事前闸，账单为零）：\n"
            f"   本批 {len(plan)} 镜 × {len(targets)} 比例 = {calls} 次调用 ≈ {total}s"
            f" ≈ ¥{est:.2f}，而预算 ¥{v['budget']:.2f} 已花 ¥{v['spent']:.2f}、"
            f"仅余 ¥{v['remaining']:.2f}。\n"
            "   ① 提高章节 budget：chapter set <项目> <章节> --budget <元>（或删掉该字段=不设限）\n"
            "   ② 只烧已批准的镜：gen-video … --approved-only\n"
            "   ③ 先逐镜看报价：gen-video … --dry-run（不调用 API、不计费）")
    if v["over_cap"]:
        tip = (f"最贵一次调用 ≈ ¥{v['max_call']:.2f}（{max_n}s），"
               f"超过 budget_per_call ¥{v['cap']:.2f}")
        if auto:      # 一条龙：告警放行——run 下没有交互补 flag 的机会，硬拦会中断全链
            _info(f"⚠ 单笔超阈：{tip} —— 一条龙(run/--auto)下告警放行，未中断")
        elif not confirm_spend:
            raise KinemaError(
                f"⊘ 单笔超阈，本批**一次都没有发出**：{tip}。\n"
                "   ① 先看清楚：gen-video … --dry-run（逐镜提示词+报价，不调用 API）\n"
                "   ② 确认后加 --confirm-spend 正式生成\n"
                "   （--confirm-spend 与 --yes 分工不同：--yes 只授权 4K 高成本档）")
        else:
            _info(f"✓ 单笔超阈已确认（--confirm-spend）：{tip}")


def _prompt_sha(text: str) -> str:
    """实发稿审阅锁的比对口径：只哈希提示词正文。

    不用 Envelope fingerprint——它把 references 也算进去，而锚定音预热、设定图
    上云会改附件清单条目、正文一字不变，按 fingerprint 比对会把没改过的稿
    误报「审后有变」。写读两侧（Studio 表态 / gen-video 闸）必须同此一份。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def video_prompt_preview(project, store, router, *, only=None) -> list[dict]:
    """逐镜「实发提示词」结构化预览——**与 `gen-video --dry-run` 同一条编译路径**
    （`stage_gen_video` 的 preview 收集模式），Studio 分镜卡按需拉取。

    存在的唯一理由是单一真源：分镜卡若按作者字段（video_prompt/narration）自行
    拼装展示，与实发的 PromptEnvelope（契约句/时间轴/台词/情绪动词/音色绑定/负面
    约束）必然分叉——用户照页面调整、实发却是另一句。预览模式下绝不落盘、绝不
    抛镜级闸（角色锚缺失降级为 gate 注记）；调用方传入**用后即弃**的 Project
    （章节文档独立加载），编译期的内存改动（如孤岛接缝的同拓扑计算）不回写。"""
    rows: list = []
    stage_gen_video(project, store, router, dry_run=True, only=only,
                    preview_sink=rows)
    return rows


def image_prompt_preview(project, store, router, *, only=None) -> list[dict]:
    """逐镜「实发生图提示词」预览——与 `gen-image` 真发同一条封装路径
    （`stage_gen_image` 的 preview 收集模式）。制度同 `video_prompt_preview`：
    单一真源、零落盘、不吃状态机跳过；直供画面与转场镜不发生图请求，故不在
    返回清单里（页面据缺席即知「本镜无生图请求」）。"""
    rows: list = []
    stage_gen_image(project, store, router, only=only, preview_sink=rows)
    return rows


def _speaking_owners(project) -> list[str]:
    """本章开口说话的实体（真源 `voicebank.speaking_owners`，网页试听端点同用）。"""
    return voicebank.speaking_owners(project.active_shots)


def _series_of(project):
    """章节文档所属的系列；定位不到返回 None（`--project <文件>` 直渲染那条路径）。

    工作区根由章节文件路径反推（`<根>/<项目>/chapters/<章节>.json`，同
    `voicecast.voice_ref_dir` 的口径），不走 `Workspace.open(None)` 的发现逻辑——
    调用方可能是 `--workspace` 指定的目录，重新发现会解析到另一个根，把音色写进
    同名的另一个项目。写操作的目标必须由手上这份文档本身定位。"""
    pid = (project.data.get("chapter") or {}).get("project")
    cf = Path(project.path)
    if not pid or cf.parent.name != "chapters":
        return None
    try:
        return Workspace(cf.parent.parent.parent).get_project(pid)
    except Exception:  # noqa: BLE001  取不到系列文档不阻断渲染
        return None


def _cast_gate(project, router, *, skip: bool = False) -> None:
    """真发与 dry-run 前的选角闸：开口的说话人都要有音色引用（定制档案或显式
    指派的模版别名），profile 缺省不算。

    mock 与 `--project <文件>` 直渲染没有选角落点，放行；`skip` 是 gen-video 的
    `--no-auto-cast`，未选角说话人的嗓音交给模型。"""
    if skip or getattr(router, "force_mock", False):
        return
    series = _series_of(project)
    if series is None:
        return
    missing = voicebank.uncast_owners(series, _speaking_owners(project))
    if not missing:
        return
    pid = series.pid
    known = {c.get("name") for c in series.characters}
    fixes = []
    for who in missing:
        if who == voicebank.NARRATOR:
            fixes.append(f'voice custom {pid} --narrator --prompt "<声线描述>" --adopt 1')
        elif who in known:
            fixes.append(f'character set {pid} --name {who} --voice-prompt "<声线描述>"')
        else:
            fixes.append(f'character add {pid} --name {who} --voice-prompt "<声线描述>" …')
    raise KinemaError(
        f"⊘ 未选角：{'、'.join(missing)}——每个开口的说话人都要有音色引用\n"
        "   按声线描述定制并立档：\n   " + "\n   ".join(fixes)
        + "\n   声线描述按六槽位写 40~80 字：性别年龄段/音区明暗/音质质感/语速节奏/"
          "口音吐字/气质，不写情绪词"
        + "\n   要官方模版音色：voice audition → voice use")


def stage_gen_video(project, store, router, *, profile=None, force=False, dry_run=False,
                    approved_only=False, ignore_refs=False, resolution=None, yes=False,
                    confirm_spend=False, auto=False, previz=False, control=False,
                    video_provider=None, only=None, concurrency=None, tail_relay=False,
                    anchor_frame=False, no_auto_cast=False, no_lipsync=False,
                    preview_sink=None):
    """Seedance 图生视频。两种音频模式（见 project.motion）：
      · dubbed —— 传我们的固定音色配音做**对口型**（参考媒体模式：分镜图+板+设定图
        全作参考图随发，与首/末帧互斥）。
      · native —— 台词写进 prompt，模型**原生配音**。缺省档=**逐镜全能参考**
        （参考生视频任务：分镜图领衔+简笔板在盘即附+设定图，一镜一片、镜间直拼）；
        首尾帧衔接是显式 opt-in（章级 frame_chain / 镜级 shots[].frame_chain /
        --chain），参与镜退回首帧任务；只要首帧硬锁、不要焊末帧则走首帧锚定
        （章级 anchor_frame / 镜级 shots[].anchor_frame / --anchor-frame），
        代价是设定图、简笔板与尾帧接力三条附发通道让位。
    生成后用片段**实际时长**回填 shots[].dur，供字幕/合成对齐。
    省钱四闸：**预留额度**（`_preflight_spend`，一次都没发之前先对账，不够就全不发）；
    approved_only 只渲分镜图已通过(done)的镜（草稿两段式的正式档）；就绪度节点逐镜
    硬拦设定图不齐的镜；animatic 未过审时给出提醒。
    分辨率：默认走 provider 配置档（1080p）；--resolution 临时覆盖，4k 需 --yes 二次授权。

    **--dry-run 与实发只有镜头集合差异**：dry-run 的逐镜提示词清单列出全部正镜
    （审提示词要看全片），实发会跳过
         done/omt/已有片段/设定图不齐的镜——故 dry-run 收尾另打一行「真跑口径」，
         与事前闸 `_preflight_spend` 取同一张 `_will_burn` 清单，两个数字恒一致。
    每镜 provider、链态和 PromptEnvelope 编译入口完全同源。
    """
    # 渲染模式在任何报价与计费之前收口：kenburns 下合成按分镜图渲染，买回的片段
    # 不参与出片，请求秒数里还折着停顿——显式 kenburns 直接拒发并给出路径。未表态
    # 的章节读到的是按内容推出的档位（`project.default_motion`），真发把它写进章节，
    # 下游 tts/字幕/合成才与本次同口径；dry-run 与 preview 是只读审阅动作，不写。
    if not project.motion_declared:
        persist = not dry_run and preview_sink is None
        _announce_motion(project, persist=persist)
        if persist:
            _settle_motion(project)
    if project.motion == "kenburns":
        raise ProjectError(
            "本章 motion=kenburns（静图运镜档）：该模式下合成按分镜图渲染，"
            "gen-video 买回的片段不参与出片，生成即白花钱。\n"
            "   要出真视频：把章节 motion 改成 dubbed（对口型+烧录选角配音）或 "
            "native（模型原生音画），或本次临时 -m c / -m b")
    # scored 与 dubbed 互斥的硬闸（dry-run 同拦：报价一个买不得的组合没有意义）：
    # 对口型人声必须由逐镜 TTS 喂入 ref_audio，而 scored 由音频模型整轨生成人声，
    # 合成时片段音轨会被整轨替换——口型与观众听到的人声不是同一份，两道钱都白花
    if project.scored_audio and project.motion == "dubbed":
        raise ProjectError(
            "audio_mode=scored 与 dubbed 对口型互斥——对口型人声由逐镜 TTS 喂入，"
            "而 scored 把人声交给音频模型整轨生成，合成时片段音轨会被整轨替换。\n"
            "   生视频请改 motion: native；要对口型请改回 audio_mode: tracks")
    _gate_4k(resolution, dry_run=dry_run, yes=yes, mock=router.force_mock)
    _warn_design_gap(project)
    # 视频 provider 点名（运行时覆盖，如 seedance-2.5=2.5 大模型）：flag > 项目顶层
    # `video_provider` > profile 链。**缺省恒走 defaults 的 mini 主力**——大模型只有
    # 显式点名才上（成本与出片风格都不该被静默换掉）。dry-run 报价 / 事前闸 /
    # 逐镜真发三处都必须走同一个解析口（分叉=报价与账单对不上）。
    vp_name = str(video_provider or project.data.get("video_provider") or "").strip() or None

    def _vroute(prof):
        """Resolve the provider and the exact profile block used by Prompt revisioning."""
        return models_mod.resolve_video(router, store, project.data, prof,
                                        override=video_provider)
    prompt_compiler = prompts_mod.PromptCompiler()
    per_aspect = project.image_per_aspect
    targets = project.aspects if per_aspect else [project.aspect]
    seed = project.style.get("seed")
    mode = project.motion            # dubbed | native
    native = project.native_audio
    chain = project.frame_chain   # 已含「仅 native」判据，见 pipeline.framechain.active
    all_shots = project.shots
    # V2V（previz 运动迁移）总闸：opt-in × 仅 native × provider 真支持。
    # **只在 native 走**：dubbed 的 ref_audio 与运动迁移会互相牵制（口型跟音频、
    # 身体跟视频）。官方虽已放宽互斥，但缺少小样验证，不默认叠加。
    v2v_want = _v2v_enabled(project, previz)
    v2v_cap = bool(getattr(_vroute(project.profile)[0], "supports_reference_video", False))
    # 孤岛判定（framechain）与逐镜任务型态（_shot_plan）吃同一个总闸：
    # 能力位不进总闸时，链图按孤岛断缝、实发却是首帧任务
    v2v_on = v2v_want and native and v2v_cap
    # 深度控制视频与 previz 共用这一套总闸（同一个 `reference_video` 槽、同一条计费
    # 口径），只是开关与逐镜判据各自独立：一章可以只开其中一路。
    control_want = _control_enabled(project, control)
    control_on = control_want and native and v2v_cap
    want_any = v2v_want or control_want
    if want_any and not native:
        _info(f"⚠ 参考视频(V2V)只在 native 模式生效，当前是 {mode}——本次按纯首帧生成"
              "（dubbed 的对口型音频与运动迁移会互相牵制，未经小样验证不默认叠加）")
    elif want_any and not v2v_cap:
        _info("⚠ 当前视频 provider 不支持参考视频(V2V)——本次按纯首帧生成")
    # 上云根只要有任一路开着就得算出来：算漏了，上传助手会拿 None 去发现工作区，
    # key_for() 返回 None 后上传被静默拒绝
    ws_root = _ws_root_of(project) if (v2v_on or control_on) else None
    # 孤岛接缝自动落无缝转场：**必须在链图预计算之前**（它改 shots 结构）。
    # dry-run 与真发同跑同落——两条路径的链态不允许有差异。
    # 章级衔接关闭（缺省）时这里同时把历史遗留的自动软切撤干净——缺省档镜间直拼。
    # preview（Studio 实发提示词预览）同拓扑计算但**绝不落盘**：调用方传入的是
    # 用后即弃的文档副本，save 会把预览行为变成对用户章节的静默写入。
    if preview_sink is not None:
        framechain.sync_seams(project.shots, chain, v2v=v2v_on, control=control_on)
    else:
        _sync_island_seams(project, chain, v2v_on, control_on)
    # 链图**必须在 --approved-only 过滤之前**、基于原始 shots 预计算（理由见 framechain.plan）
    chain_map = framechain.plan(all_shots, chain, v2v=v2v_on, control=control_on,
                                native=native)
    # 被焊入的镜集合：结对/章级衔接的下游端要以分镜图第 0 帧硬锁，不能走缺省全能参考
    welded_in = framechain.welded_in_ids(chain_map)
    # 本次是否存在任何衔接诉求（章级或镜级结对）——能力位告警按它门控
    chain_any = chain or (native and any(framechain.pair_opt_in(s) for s in all_shots))
    # 尾帧接力总闸（章级 opt-in 或 --tail-relay × native/dubbed）：上一镜真实末帧作
    # 下一镜的 reference_image。provider 能力面逐镜判（`_relay_plan`），串行强制在
    # workers 决策处——注入次序依赖 parallel.run 的 workers=1 内联串行保证。
    relay_on = tailrelay.active(project.data, mode, override=tail_relay)

    def _previz_last(s):
        """该镜 previz 末帧图（在盘才算数）——不在盘就当没有，退回衔接链的末帧。"""
        p = s.get("last_frame_ref")
        return str(p) if p and has_file(p) else None

    def _frame_at(s, aspect):
        """该镜在该比例下**真正属于这个比例**的图（找不到返回 None）。

        与 `project.image_for` 的区别只有一条、也是要害：**次比例不回退主图**。
        `image_for` 的回退（`images[a] or image`）对渲染是对的（拿得到图总比没有好，
        Ken Burns 会重构图），对**首尾帧衔接是错的**——末帧要被模型当成这一镜运动的
        收束目标，喂一张 16:9 的图去收束一条 9:16 的请求，模型只能自己裁或自己拉伸。
        主比例例外：顶层 `image` 按定义就是主比例图（`stage_gen_image` 落盘时
        `s["image"] = s["images"].get(project.aspect)`；`supply` 不带 `--aspect` 时
        写的也是主比例位），单比例项目根本不写 `images`，所以那一支必须允许回退，
        否则等于把默认路径的整条链全断掉。"""
        per = (s.get("images") or {}).get(aspect)
        if per:
            from .storage.media import ensure_local
            return ensure_local(per)
        return project.image_for(s, aspect) if aspect == project.aspect else None

    def _flf2v(s):
        """本镜是否真的会发末帧 → `(下一镜|None, 断链原因, 是否发末帧)`。

        判据 = 链图给出下一镜 **且该镜在本次要出的每个比例下都有该比例的图在盘上**。
        焊缝两端已由链图判完（`framechain.sends`/`receives`），这里只补「图在不在盘」。
        多比例故意取保守的全称量词：宁可整镜退回常规首帧生成，也不要出现
        「提示词按只写过渡瘦身、请求里却没末帧」这种口径分叉。
        **取图必须走 `_frame_at` 而不是 `project.image_for`**：后者次比例缺图时会
        回退主图，于是「次比例缺图退回常规」这条登记在 `docs/agents/guard-map.md` 的
        不变量在「下一镜有顶层 image、只缺逐比例图」这个形状上**并不成立**——
        末帧会拿着一张主比例的图混进次比例请求（既有守卫恰好只构造了没有顶层
        image 的形状，绕开了这个洞）。
        `image_for` 在字段有值而文件不在时会返回坏路径，has_file 是这一路唯一的
        护栏——没有它，seedance 侧 file_to_data_url 会在**前面几镜已经烧完钱之后**
        才抛 FileNotFoundError。"""
        nxt, why = chain_map.get(id(s), (None, "off"))
        ok = bool(nxt) and all(has_file(_frame_at(nxt, a)) for a in targets)
        return nxt, why, ok

    def _shot_plan(s, prov):
        """本镜真正会发出去的条件组合——**dry-run 与真发共用这一个函数**。

        返回 `(下一镜, 断链原因, 是否走链末帧, previz末帧路径|None, 是否发参考视频,
        是否发末帧, 简笔板路径|None, 是否走全能参考, 是否首帧锚定, 参考视频投影)`。
        判据在这里一次定死，别处不重算（重算必然分叉）：
          ⓪ **guide 仲裁先行**（`sketchboard.active_guide` 是唯一真源）：
             guide=sketch 的镜 previz 两件套（末帧/V2V）一律不参与——两条运动预演
             路径互斥，同发必然互相打架。
          ① **V2V 显式 opt-in × provider 真支持参考视频**——`generate(**kwargs)`
             会静默吞掉不支持的 `reference_video`——请求照常计费，previz 却并未
             参与生成。能力面进判据而不是事后补救。
          ② **previz 末帧**占用末帧槽（须过 `supports_last_frame` 能力闸——
             不支持的型号收到 `role=last_frame` 只丢不报，`seedance-2.0-fast` 实测）。
          ③ **衔接参与镜走首帧任务**：章级 `frame_chain: true` 或镜级
             `shots[].frame_chain: true`（结对）——焊缝两端已由链图判完
             （`framechain.plan`），出链镜（chained）另发末帧、被焊入的镜
             （`welded_in`）以分镜图第 0 帧硬锁。
          ③′ **首帧锚定**（`anchorframe.anchored`：章级/镜级/`--anchor-frame`）把
             本镜从缺省档拽回首帧任务、且不发末帧——分镜图以 `role=first_frame`
             硬锁为第 0 帧。它**只否决缺省档那一支**：显式参考孤岛表态
             （`sketch.reference`）仍然生效，两个镜级显式表态里更具体的那个说了算。
             衔接参与镜本就是首帧任务，锚定对它们是空操作。
          ④ **以上都不沾的 native 镜落缺省档：全能参考**（参考生视频任务）——
             分镜图领衔、简笔板（在盘即附）与角色/场景/道具设定图全挂
             reference_image、**不发首/末帧**（首帧任务与参考媒体官方互斥，
             实测 400），一镜一片、镜间直拼。provider 不支持参考图
             （`supports_reference_images=False`）时退回纯首帧生成。
        简笔板能力闸（`supports_reference_images`）：不支持的 provider 一张
        都不发——`**kwargs` 静默吞掉后，提示词里的「所附分镜板」就是在向模型
        索要一个不存在的参考。板不附时 beats 时间轴照发（纯文本 timeline 独立成立）。
        显式 `sketch.reference` 表态仍被尊重（衔接章里强制该镜走参考孤岛，
        判据在 `framechain.island`）——缺省档下它与缺省行为重合，无需再写。
        """
        sk = sketch_mod.active_guide(s) == "sketch"
        nxt, why, chain_ok = _flf2v(s)
        rv = _ref_video(s, previz_on=v2v_on, control_on=control_on)
        shot_v2v = rv is not None
        # 末帧能力面：不支持的型号收到 role=last_frame 只丢不报（见 base.supports_last_frame）。
        # 两个末帧来源（previz 终态 / 衔接链下一镜图）共用同一个槽，故一处闸拦两条。
        # dubbed 的 ref_audio 走参考媒体任务，末帧槽不存在（seedance 适配器丢弃）
        can_last = bool(getattr(prov, "supports_last_frame", True))
        pz_last = None if (sk or shot_v2v or not can_last or not native) else _previz_last(s)
        # 出链（真发末帧）与被焊入（首帧硬锁）都算衔接参与者——两端都不能走参考任务
        chained = bool(chain_ok and not shot_v2v and not pz_last and can_last)
        in_weld = chained or (id(s) in welded_in)
        # 首帧锚定：缺省档的显式退出闸（判据单一真源 pipeline.anchorframe）。
        # V2V 与 previz 末帧优先——那两支已各自占用了图的角色与末帧槽。
        # **衔接参与镜一律不算锚定**：它们本就是首帧任务，锚定是空操作；标上去会
        # 让同一行既写「末帧=镜N」又写「镜间硬切」，两句话互相打脸。
        anchor = bool(not shot_v2v and not pz_last and not in_weld
                      and anchorframe.anchored(project.data, s, mode, anchor_frame))
        # 全能参考的两条入口：缺省章的非衔接参与镜（含结对衔接因下游缺图而临时
        # 落空的镜——那是补图即复原的临时态，按缺省档生成不留死角）；
        # 显式衔接章（章级 frame_chain）里只有 `sketch.reference` 表态的镜走参考
        # 孤岛，其余一律留在首帧任务——衔接章是对整章焊缝形态的整体 opt-in，不掺两种任务
        ref_mode = bool(native and not shot_v2v and not pz_last
                        and getattr(prov, "supports_reference_images", False)
                        and ((not chain and not in_weld and not anchor)
                             or sketch_mod.reference_shot(s, native)))
        board = None
        # 附板的两条合法通道：dubbed（参考媒体模式）与 native 全能参考（参考任务）。
        # native 首帧任务（衔接参与镜）仍然一张都不附（官方禁混，适配器亦硬拦）。
        if sk and (ref_mode or not native):
            p = sketch_mod.board_of(s)
            if p and has_file(p) and getattr(prov, "supports_reference_images", False):
                board = str(p)
        return (nxt, why, chained, pz_last, shot_v2v, bool(pz_last or chained),
                board, ref_mode, anchor, rv)

    def _ref_plan(s, prov, ref_mode, board=None, tails=None, route="A", base=None):
        """本镜的参考装配计划 → `(RefPlan|None, 被配额裁掉的项)`。

        RefPlan 是 manifest / 工作线程 ref_images / Envelope references /
        Studio 预览 / provider content[] 五处消费的**唯一产出点**——编号 =
        附图顺序（分镜图 → 板 → 上镜尾帧 → 设定图），错一位模型就把场景图当
        角色图用。native 全能参考与 dubbed 参考媒体两档合法；首帧任务（衔接
        参与镜与首帧锚定镜）恒 None——官方禁混参考图，附了就是 400。配额动态：
        引擎侧 ref_images 钳 7（官方全图 ≤9，分镜图另占 image 参数），板与尾帧
        真附时各让出一席；尾帧注入后必须经本函数**整体重建**（配额随之重算），
        绝不在旧计划上原位追加。被裁清单交由报价与真发两处点名。"""
        if not getattr(prov, "supports_reference_images", False):
            return None, []
        if not (ref_mode or not native or _control_v2v(s)):
            return None, []
        tails = dict(tails or {})
        rows, dropped = _video_sheet_refs(
            project, s, cap=7 - (1 if board else 0) - (1 if tails else 0),
            exclude_path=base)
        if not rows and not board and not tails and route == "A":
            return None, dropped
        return RefPlan(route=route, board=board, tails=tails,
                       rows=tuple((k, n, p) for k, n, p in rows),
                       dropped=tuple(dropped)), dropped

    _identity_cache: dict = {}
    _warned_origin: dict = {}

    def _identity_of(prof):
        """写实档判定（image.identity_sheet 能力位）。读点与 cmd_gen_refs 同一个
        （`router.resolve("image", prof)` 第二返回值）——不走 store.profile()，
        测试桩的 store 契约里没有它。"""
        if prof not in _identity_cache:
            _identity_cache[prof] = bool(
                router.resolve("image", prof)[1].get("identity_sheet"))
        return _identity_cache[prof]

    def _scene_base(s):
        """主场景基准图的在盘本地路径（降级路线的 image 位），无则 None。"""
        from .storage.media import ensure_local
        row = lineage.primary_scene_ref(project, s)
        p = ensure_local(row["path"]) if row and row.get("path") else None
        return p if (p and Path(p).is_file()) else None

    def _fallback_board(s, prov, sk_board):
        """降级路线可挂的板：guide 表态档优先；其次盘上板文件——降级用板不依赖
        guide 表态（板此时是构图承载件，不是运动预演的仲裁结果）。"""
        if sk_board:
            return sk_board
        p = sketch_mod.board_of(s)
        return (str(p) if p and has_file(p)
                and getattr(prov, "supports_reference_images", False) else None)

    def _control_v2v(s):
        """本镜是不是**控制视频**那一支 V2V。previz 那一支不算。

        两支参考视频进不进阶梯是分开判的：previz 是无材质灰模，降级路线的取景地
        契约与它对撞（`_route_for` 另有一道 previz 闸）；控制视频只是深度与骨骼的
        示意图，与外观装配正交——运动由它给、外观由设定图给，正是降级路线的形态。
        """
        rv = _ref_video(s, previz_on=v2v_on, control_on=control_on)
        return bool(rv and rv[0] == "control")

    def _ref_task(prov, ref_mode, v2v=False):
        """路线阶梯的任务门槛：本镜请求能挂参考装配。native 走全能参考
        （ref_mode），dubbed 恒为参考媒体（图+音频都是参考，板与设定图随
        `ref_audio` 合法附发）——两档的人脸敞口相同，降级形态也相同。

        **控制视频的 V2V 同属参考任务**：那条分支的图本来就挂
        `role=reference_image` 而不是首帧，多挂几张同 role 的参考图不触碰
        「首帧禁混参考媒体」那条官方铁律——它拦的是 first/last frame。
        把 V2V 排除在阶梯之外，写实档的复刻镜就是死局：分镜图是挂着设定图生的
        （图生图、天然不受信），人脸拒之后没有第二形态可退，而降级路线恰恰是
        唯一能把受信身份图送进请求的那一种。
        """
        return bool((ref_mode or not native or v2v)
                    and getattr(prov, "supports_reference_images", False))

    def _face_route(s, prof, prov, ref_mode, sk_board):
        """本镜的路线仲裁 + 降级装配素材 → (route, why, base, board)。
        dry-run 与真发共用；身份图不受信的具名告警在此只喊一次。"""
        identity = _identity_of(prof)
        base = _scene_base(s) if identity else None
        board = _fallback_board(s, prov, sk_board) if identity else sk_board
        route, why = _route_for(project, s, identity=identity,
                                ref_task=_ref_task(prov, ref_mode, _control_v2v(s)),
                                board=board, scene_base=base, v2v=_control_v2v(s))
        if identity and "不受信" in why and not _warned_origin.get("origin"):
            _warned_origin["origin"] = True
            _info(f"  ⚠ {why}")
        if route == "A":
            return route, why, None, sk_board
        return route, why, base, (board if route == "B" else None)

    def _ref_note(sk_board, manifest, tail=False, dropped=(), route="A"):
        """全能参考/参考媒体的组成短语（dry-run 清单与真发日志共用，别各拼一份）。

        取 `manifest`（含 kind）而不是张数：**场景俯视图与设定图分开报数**——合成
        「设定图×4」时，「基准图与图纸配没配齐」这件事在清单上完全看不出来，而两张
        缺一张正是这条路上唯一会静默发生的偏差。

        `dropped` 非空即把被配额裁掉的项就地点名：这一条必须与张数同处一行——
        「设定图×7」单独出现时读起来像「都发出去了」，而真相是第 8 项已经被丢掉。"""
        kinds = [k for k, _n in (manifest or ())]
        n_sheets = sum(1 for k in kinds
                       if k in ("character", "scene", "scene_main", "prop"))
        n_plans = sum(1 for k in kinds if k in ("scene_top", "scene_top_main"))
        parts = ["分镜图" if route == "A" else "场景图(取景基准)"]
        if sk_board:
            parts.append("简笔板")
        if tail:
            parts.append("上镜尾帧")
        if n_sheets:
            parts.append(f"设定图×{n_sheets}")
        if n_plans:
            parts.append(f"场景俯视×{n_plans}")
        note = "+".join(parts)
        if dropped:
            note += f"·⚠配额裁掉{len(dropped)}项：{'、'.join(dropped)}"
        return note

    def _sk_timeline(s, total=None):
        """时间轴提示注入判据：guide=sketch 且拍序列拆得出（authored beats 优先、
        缺省句读自动拆拍，单一真源 `effective_beats`）。板文件不是必要条件——
        timeline prompting 纯文本独立成立，板只是又一重像素锚。
        `total` 与拼装侧同基准传入：自动拆拍的拍数随时长收敛，判据与拼装取不同
        total 时会出现"这里判有、那里编出空"的错位。"""
        return sketch_mod.active_guide(s) == "sketch" \
            and bool(sketch_mod.effective_beats(s, total)[0])

    def _relay_plan(s, prov, shot_plan):
        """本镜的尾帧承接计划 → `(上一正镜|None, 是否可承接, 计划期尾帧|None)`。

        可承接 = 总闸开 × 有上一正镜 × 本镜请求能挂参考图（全能参考或 dubbed
        参考媒体，V2V/首帧任务恒否——首帧任务官方禁混参考图）。计划期尾帧取
        **盘上**已登记的（上一轮生成或重投留下的）；同批新鲜尾帧在上一镜回填
        时注入（`_relay_inject`），dry-run 与真发的计划期口径恒一致。"""
        if not relay_on:
            return None, False, None
        (_nxt, _why, _chained, _pz, shot_v2v, _flf2v, _board,
         ref_mode, _anchor, _rv) = shot_plan
        src = tailrelay.prev_shot(all_shots, s)
        ok = bool(src and (ref_mode or not native) and not shot_v2v
                  and getattr(prov, "supports_reference_images", False))
        return src, ok, (tailrelay.disk_tails(src, targets) if ok else None)

    def _anchor_plan_for(s, prov, ref_mode):
        """本镜音色锚定计划（dry-run 与真发共用的纯判定，实体在
        `voicecast.voice_anchor_plan`）——native 全能参考 × provider 有参考音位 ×
        章节未关（`voice_anchor: false`）× 非 scored 才成立。scored 下片段音轨
        会被剧本整轨替换，给模型锚音色是花请求体积买一把没人听到的声音。"""
        if not (native and ref_mode and not project.scored_audio):
            return None
        # 混烧下声源按镜分治（voicecast.burn_muted 单一判据）：旁白/无词镜闭声
        # 出演、人声走烧录轨，音色参考没有作用对象；对白镜由模型原生发声，
        # 锚定照常附发
        if project.native_voiceover and voicecast.burn_muted(s):
            return None
        if not int(getattr(prov, "max_ref_audios", 0) or 0):
            return None
        if not chapter_flag(project.data, "voice_anchor"):
            return None
        plan = voicecast.voice_anchor_plan(
            project, store, s, max_refs=int(prov.max_ref_audios))
        # 全员未选角/全员超位也返回（anchored 空）：dry-run 注记与页面要能
        # 照实说「这些说话人任模型自选嗓音」，静默略过就是把漂移藏起来
        return plan if any(plan[k] for k in ("anchored", "loose", "over")) else None

    def _anchor_state(aplan):
        """计划 + 在盘事实（纯读，不落盘）：
        `[{who, voice_type, no, clip|None, custom, desc|None}]`。
        clip=None 的官方音色由真发路径现场预热；dry-run 照实标「待预热」。

        `desc` 是造出这把声音的那段声线描述，只有定制音色有（模版音色的档案里
        `prompt` 恒空，官方别名是个标签不是描述）。按 voice_type 取而不是按 owner：
        锚定行里旁白叫「画外旁白」、档案库里叫「旁白」，按名字查旁白必然落空。

        三处构造绑定素材的地方（真发、dry-run、预览另一语种）都从这里继承，
        各自去取就会出现「审的稿不是发的稿」，而 dry-run 是付费前唯一的审阅口。"""
        return [{**r, "clip": clip, "custom": custom,
                 "desc": (voicebank.cast_for_type(project.data, r["voice_type"])
                          or {}).get("prompt") or None}
                for r in aplan["anchored"]
                for clip, custom in [voicebank.anchor_clip_for(project, r["voice_type"])]]

    _va_clip_cache: dict = {}    # voice_type → 预热/裁剪后的在盘 clip（本次运行内共享）
    _va_tts: list = []           # 懒解析的 TTS provider（只在真有官方音色要预热时解析）

    def _preheat_anchor(vt):
        """官方音色的锚定参考音不在盘时现场合成一句（与 `stage_tts` 的预热同一条
        路径与命名真源）；失败不阻断——该说话人本镜退回模型自选嗓音，点名告警。"""
        ref = voicecast.anchor_ref_path(voicecast.voice_ref_dir(project), vt)
        if ref.is_file():
            return str(ref)
        try:
            if not _va_tts:
                _va_tts.append(router.resolve("tts", project.profile)[0])
            # 预热是一次真实的 TTS 合成，入本章台账（缓存命中的那些不重复计费）
            res = _va_tts[0].synthesize(voicecast.ANCHOR_TEXT, str(ref), voice=vt)
            try:
                project.add_cost("tts", getattr(res, "cost", 0.0) or 0.0)
            finally:
                project.save()          # 计划期付费：随后的硬拦不经收尾 save
            return str(ref) if ref.is_file() else None
        except Exception as e:  # noqa: BLE001  预热失败只降级该音色，不断整批
            _info(f"⚠ 音色 {vt} 锚定音预热失败（{e}）——相关说话人任模型自选嗓音")
            return None

    def _fit_anchor(clip, cap):
        """把锚定音裁进单条时长配额（参考音**合计**超限是建任务 400，实测）。
        裁剪产物落项目级 assets/voices 缓存（同名幂等复用）。

        定制音色的档案 clip 会被 `oss sync` 改写成 URL，而它照样占合计预算——
        先拉回本地再按同一配额裁。拉不回来（媒体未上云到本 bucket）就无从 probe，
        原值发出由服务端裁决。"""
        from .storage.media import ensure_local, is_url
        src = ensure_local(clip)
        if is_url(src):
            return clip
        try:
            if probe_duration(src) <= cap + 0.05:
                return clip
        except Exception:  # noqa: BLE001  读不出时长的坏文件交给服务端报错，不在此吞
            return clip
        out = voicecast.voice_ref_dir(project) / f"fit_{Path(src).stem}_{cap:g}s.mp3"
        if not out.is_file():
            from .ffmpeg import run as _ffrun
            try:
                # run() 自带 ffmpeg 前缀与 -y/-loglevel，这里只给业务参数
                _ffrun(["-i", str(src), "-t", f"{cap}", str(out)], desc="裁剪锚定音")
            except Exception as e:  # noqa: BLE001
                _info(f"⚠ 锚定音裁剪失败（{e}）——原样发送，可能被服务端按总时长拒绝")
                return clip
        return str(out)

    def _anchor_clips(s, prov, state):
        """真发路径的锚定音落料：预热缺失项 → 按总时长预算裁剪 → 存活项重排编号。
        返回 `(提示词 anchors, [(voice_type, clip), …])`——绑定句的「参考音频N」
        与实附 audio_url 的顺序**由同一次重排产出**，谁掉队编号就一起变，绝不出现
        句子点名了一条没发出去（或序号错位）的参考音。"""
        ok = []
        for r in state:
            clip = r["clip"]
            if clip is None:
                if r["voice_type"] not in _va_clip_cache:
                    _va_clip_cache[r["voice_type"]] = _preheat_anchor(r["voice_type"])
                clip = _va_clip_cache[r["voice_type"]]
            if clip is None:
                _info(f"⚠ 镜 {s['id']} 说话人「{r['who']}」锚定音不可用——本镜该角色任模型自选嗓音")
                continue
            ok.append({**r, "clip": clip})
        uniq = list(dict.fromkeys(r["clip"] for r in ok))
        budget = float(getattr(prov, "max_ref_audio_seconds", 0) or 0)
        if budget > 0 and uniq:
            cap = voicecast.anchor_budget_cap(budget, len(uniq))
            fitted = {c: _fit_anchor(c, cap) for c in uniq}
            for r in ok:
                r["clip"] = fitted[r["clip"]]
        nos: dict = {}
        anchors, refs = [], []
        for r in ok:
            no = nos.get(r["voice_type"])
            if no is None:
                no = len(nos) + 1
                nos[r["voice_type"]] = no
                refs.append((r["voice_type"], r["clip"]))
            anchors.append({"who": r["who"], "no": no, "desc": r.get("desc")})
        return anchors, refs

    def _anchor_note(state, loose, prov=None, over=()):
        """dry-run 清单与 Studio 预览共用的锚定短语（付费前看得见的那一份）。

        逐条标实发时长（预算均分口径与 `_anchor_clips` 同源 `anchor_budget_cap`）：
        样本时长决定音色跟随幅度，短样本要在花钱前看见——「老康(7.3s)+阿汛(5.1s)」
        一眼就能读出第二条没到配额、该重预热。

        `loose` 与 `over` 分开说：前者去选角就能解决，后者是参考位已满、
        再选角也不会附发，得减说话人或换限额更高的档。"""
        tail = (f" · 音色参考位已满={'/'.join(over)}"
                f"(本档最多 {int(getattr(prov, 'max_ref_audios', 0) or 0)} 条·"
                "这些人任模型自选嗓音)" if over else "")
        if not state:
            return ((f" · 音色未选角={'/'.join(loose)}(模型自选嗓音·跨镜会漂移)"
                     if loose else "") + tail)
        budget = float(getattr(prov, "max_ref_audio_seconds", 0) or 0)
        n = len({r["voice_type"] for r in state})
        cap = voicecast.anchor_budget_cap(budget, n) if budget > 0 and n else None

        def _tag(r):
            if not r["clip"]:
                return "(待预热)"
            try:
                dur = probe_duration(r["clip"])
            except Exception:  # noqa: BLE001  URL/坏文件标不出时长，不因注记失败中断
                return ""
            return f"({min(dur, cap):.1f}s)" if cap else f"({dur:.1f}s)"

        names = "+".join(r["who"] + _tag(r) for r in state)
        note = f" · 音色锚定={names}"
        if loose:
            note += f"（未选角:{'/'.join(loose)}）"
        return note + tail

    def _video_envelope(s, prov, prof, profile_params, dur, shot_plan,
                        sheet_refs, ref_manifest, tail_refs=None,
                        voice_anchors=None, voice_refs=None, lang=None,
                        route="A", base_image=None, board=None):
        """dry-run 与真实生成共用的唯一 PromptEnvelope 编译入口。
        `voice_anchors`（绑定句素材）与 `voice_refs`（附发清单）由同一次锚定
        落料产出——两者错位即「绑定句点名了一条不存在的参考音」。
        `lang` 是**展示用**语种覆盖（preview 的中英对照编译）：只换措辞语种、
        不做 provider 字数上限强杀——实发恒走 provider 自己的 prompt_lang。
        `route`/`base_image`/`board` 随 RefPlan 传入：降级路线（B/C）下 image 位
        是场景基准图、板由 RefPlan 决定挂不挂——shot_plan 里的 sk_board 只覆盖
        guide=sketch 的表态档，降级轮补出的板不经它。"""
        (nxt, _why, chained, pz_last, shot_v2v, flf2v, sk_board,
         ref_mode, _anchor, rv) = shot_plan
        if route == "A" and board is None:
            board = sk_board
        ref_rows = []
        for vt, clip in (voice_refs or []):
            if clip:
                ref_rows.append(("voice_anchor", f"shot:{s['id']}:voice:{vt}", clip))
        if route == "A":
            for asp in targets:
                image = project.image_for(s, asp)
                if image:
                    ref_rows.append(("shot_frame", f"shot:{s['id']}:{asp}", image))
                last = pz_last or (project.image_for(nxt, asp)
                                   if chained and nxt else None)
                if last:
                    ref_rows.append(("last_frame", f"shot:{s['id']}:last:{asp}", last))
        elif base_image:
            # 降级路线：envelope 记的必须是真发的那张——把没发出去的分镜图写进
            # 快照就是血缘上的伪证（它还参与 fingerprint）
            ref_rows.append(("scene_base", f"shot:{s['id']}:base", base_image))
        if board:
            ref_rows.append(("sketch_board", f"shot:{s['id']}:board", board))
        for asp in targets:
            if tail_refs and tail_refs.get(asp):
                ref_rows.append(("tail_frame", f"shot:{s['id']}:tail:{asp}",
                                 tail_refs[asp]))
        ref_rows.extend(("design_reference", f"shot:{s['id']}:{Path(str(path)).name}", path)
                        for path in sheet_refs)
        if rv:
            ref_rows.append(("reference_video", f"shot:{s['id']}:{rv[0]}", rv[1]))
        skill_revision, profile_revision = _prompt_revisions(
            project, prof, prov, profile_params)
        video_cast, video_fallback = project.shot_cast(s)
        return prompt_compiler.video(
            s, native=native,
            lang=lang or getattr(prov, "prompt_lang", "zh"),
            # 分段用秒段还是镜头序号，随型号能力位走（真源
            # `VideoProvider.timeline_unit`）：两代的时间轴规范不同，
            # 编译期不知道发给谁就只能按一种发
            timeline_unit=getattr(prov, "timeline_unit", "second"),
            flf2v=flf2v, ref_video=shot_v2v,
            ref_video_kind=(rv[0] if rv else "previz"),
            sketch=_sk_timeline(s, dur), sketch_board=bool(board),
            ref_base=(route != "A"),
            sketch_total=dur,
            cast_anchor=_cast_anchor_text(_video_cast(project, s), project,
                                          lang=getattr(prov, "prompt_lang", "zh")),
            subject_kinds=_video_subject_kinds(project, s),
            character_negative=prompts_mod.character_negative_block(
                video_cast if not video_fallback else []),
            ref_mode=ref_mode, ref_sheets=len(sheet_refs), ref_manifest=ref_manifest,
            voice_anchors=voice_anchors or None,
            native_mute=bool(native and project.native_voiceover
                             and voicecast.burn_muted(s)),
            max_chars=0 if lang else int(getattr(prov, "max_prompt_chars", 0) or 0),
            references=_prompt_references(ref_rows),
            skill_revision=skill_revision, profile_revision=profile_revision)

    def _warn_sketch(prov, s, total=None, *, ref_mode=False, board=None):
        """guide=sketch 的镜没法完整生效时点名（配置洞要喊出来，不静默降级）。
        `total` = 调用方**已算好的**请求秒数（拍密度体检用）——绝不在这里再调
        request_seconds：那会让 stage_gen_video 里的读侧闸出现第三处调用，
        破坏「报价与真发恰好两处同源」的守卫。
        `ref_mode`/`board` 从 **同一次 `_shot_plan`** 取——板附没附上只有它说了算，
        这里另立条件，日志与真发就会口径分叉。"""
        act = sketch_mod.active_guide(s)
        if act != "sketch":
            # 板/beats 在盘、缺省仲裁却落到 previz 或控制视频 —— 整包静默失效是最贵的
            # 配置洞：用户花钱画了板、写了 beats，另一路一登记时间轴一个字都不再发。
            # 显式 guide 表态不喊（用户点过名，那条路就是本意）。
            if (str(s.get("guide") or "").strip().lower() not in sketch_mod.GUIDES
                    and (sketch_mod.board_of(s) or sketch_mod.beats_of(s))):
                _info(f"⚠ 镜 {s.get('id')} 有简笔板/beats 但缺省仲裁走 {act}——"
                      "时间轴与板都不参与本次生成；要用简笔路径请 "
                      f"`sketch use --shot {s.get('id')} --guide sketch` 显式表态")
            return
        if not sketch_mod.effective_beats(s, total)[0]:
            _info(f"⚠ 镜 {s.get('id')} guide=sketch 但没有任何运动设计"
                  "（video_prompt/action/end_state 全空、也没写 sketch.beats）——"
                  "本镜没有时间轴可编，previz 也不参与（显式表态不回落）")
            return
        dens = sketch_mod.beats_density(s, total)
        if dens:
            _info(f"⚠ 镜 {s.get('id')} 拍密度：{dens}")
        p = sketch_mod.board_of(s)
        if not p:
            return
        if board:
            # 板真会附发（dubbed 参考媒体 / native 全能参考缺省档）——漂移体检逐镜做
            _warn_board_drift(s)
            return
        # 板在盘却附不出去的三种，逐一点名：
        if not has_file(p):
            _info(f"⚠ 镜 {s.get('id')} 的简笔板文件不在盘上（{p}）——时间轴照发，板不附；"
                  "重跑 `sketch gen` 可补")
        elif not getattr(prov, "supports_reference_images", False):
            if not _warned_ski.get(prov.name):
                _warned_ski[prov.name] = True
                _info(f"⚠ {prov.name} 不支持额外参考图，简笔板不随请求附上（时间轴提示照发）"
                      "——要附板请把 video provider 路由回 seedance")
        elif native and not ref_mode:
            _info(f"ⓘ 镜 {s.get('id')} 参与首尾帧衔接（首帧任务禁混参考图）——板只当拍表："
                  "分段时间轴照发、板不附；要板随请求发请撤掉该镜的衔接"
                  "（章级 frame_chain / 镜级 shots[].frame_chain）")

    def _warn_board_drift(s):
        """板漂移体检（唯一真源 board_drift）：板画的还是旧节奏——照发不阻断（引擎
        只报不改），但时间轴与板此刻说的是两套秒数/动作，模型会二选一或折中。
        板真会附发的两条通道（dubbed、native+「板作参考」opt-in）都必须体检。"""
        drift = sketch_mod.board_drift(s)
        if drift and drift.get("beats"):
            _info(f"⚠ 镜 {s.get('id')} 的简笔板画的是旧拍序列（beats/提示词改过而板"
                  f"未重生）——板照发，但与时间轴已不一致；建议先 "
                  f"`sketch gen --only {s.get('id')} --force` 重生")
        elif drift and drift.get("dur"):
            _info(f"⚠ 镜 {s.get('id')} 的简笔板按 {drift['dur']['was']}s 画、现时长 "
                  f"{drift['dur']['now']}s——秒段标签已错位，建议 "
                  f"`sketch gen --only {s.get('id')} --force` 重生")

    def _warn_no_last_frame(prov, s):
        """provider 不支持末帧时点名一次（每个 provider 只喊一次，不刷屏）。

        不喊的后果是最贵的一种：日志与页面都标着「末帧→镜N」，成片里却一条缝
        都没衔接，而这要等全片合出来才看得见。门控取 `chain_any`——镜级结对
        （`shots[].frame_chain`）的衔接诉求与章级同样会被这个能力洞吞掉。"""
        if getattr(prov, "supports_last_frame", True) or not chain_any:
            return
        if _warned_lf.get(prov.name):
            return
        _warned_lf[prov.name] = True
        _info(f"⚠ {getattr(prov, 'model', prov.name)} 不支持末帧（role=last_frame 会被"
              "服务端静默丢弃）——本章的衔接诉求全部落空：相关镜按缺省全能参考"
              "（或纯首帧）生成，提示词也随之不再按「过渡专写」拼装。"
              "要首尾帧衔接请改用 `--video-provider seedance-mini`（或 seedance-2.5）")

    def _warn_no_tail_relay(prov):
        """本章开了尾帧接力、provider 却接不住时点名一次（每 provider 一次）。

        接力要两个能力位同时成立：`supports_return_last_frame`（拿得回尾帧）与
        `supports_reference_images`（下一镜附得上）。minimax-h3 两者皆无——官方
        v2 API 没有尾帧回传参数、首尾帧模式与参考素材互斥（见 minimax.py 模块头），
        veo 亦无参考图通道。缺任一位该 provider 的镜都按无承接生成，
        静默失效即「章节开着接力、成片却全是硬切」且零提示。"""
        if not relay_on or (getattr(prov, "supports_return_last_frame", False)
                            and getattr(prov, "supports_reference_images", False)):
            return
        if _warned_tail.get(prov.name):
            return
        _warned_tail[prov.name] = True
        lacks = []
        if not getattr(prov, "supports_return_last_frame", False):
            lacks.append("无尾帧回传（return_last_frame）")
        if not getattr(prov, "supports_reference_images", False):
            lacks.append("无多参考图通道")
        _info(f"⚠ {getattr(prov, 'model', prov.name)} 不支持尾帧接力"
              f"（{'、'.join(lacks)}）——tail_relay 对路由到该 provider 的镜自动失效，"
              "按无承接生成（时间轴照发）；要接力请把 video provider 路由回 seedance")

    # 首帧锚定的让位告警是**章级一次**，故用闭包局部而非模块级 `_warned_*`：
    # 那几个跨调用不重置，同进程连跑两章时第二章会整章不出声。
    _warned_anchor: dict = {}

    def _warn_anchor_tradeoff(anchor: bool):
        """首帧锚定生效时把让位的通道点名一次（整章一次，不逐镜刷屏）。

        首帧任务与参考媒体官方互斥（适配器亦硬拦），所以本档下设定图、简笔板与
        尾帧接力三条通道都发不出去。静默让位是这里最贵的失败形态：章节配置上
        设定图仍然挂着，实际跨镜一致性却只剩分镜图本身与文字角色锚。
        逐镜拓扑另有 dry-run 清单可看，此处只说整章口径。"""
        if not anchor or _warned_anchor.get("said"):
            return
        _warned_anchor["said"] = True
        lost = ["角色/场景/道具设定图", "简笔板"]
        if relay_on:
            lost.append("尾帧接力")
        _info("ⓘ 首帧锚定生效：分镜图以 first_frame 硬锁为第 0 帧、不发末帧，镜间保持硬切；"
              f"{'、'.join(lost)}在本档发不出去（首帧任务与参考媒体官方互斥），"
              "跨镜一致性只剩分镜图与文字角色锚")

    def _warn_no_v2v_support(prov, s):
        """provider 不支持参考视频时点名一次（每个 provider 只喊一次，不刷屏）。"""
        if not (v2v_on and _v2v_shot(s)) or getattr(prov, "supports_reference_video", False):
            return
        if _warned_v2v.get(prov.name):
            return
        _warned_v2v[prov.name] = True
        _info(f"⚠ {prov.name} 不支持参考视频(V2V)，相关镜按纯首帧生成"
              "——V2V 目前只有 seedance；要用请把该镜/项目的 video provider 路由回 seedance")

    shots = all_shots
    if only:   # 定向镜号（与 gen-image 同口径，单镜重roll/断点补渲用）：链图已按
        # 原始 shots 预计算（chain_map），过滤只筛渲染对象、不碰链邻居；
        # dry-run 报价与事前闸 _will_burn 同按过滤后清单（只为要发的镜对账）
        want = {x.strip() for x in str(only).split(",") if x.strip()}
        shots = [s for s in shots if str(s.get("id")) in want]
        if not shots:
            _info(f"没有匹配 --only {only} 的分镜。")
            return
    if approved_only:   # 草稿两段式：只有人批准过分镜图的镜才烧 Seedance
        shots = [s for s in shots if review.get_state(s, "image") == "done"]
        if not shots:
            _info("没有分镜图已通过(done)的镜。先出 animatic 过节奏审、逐镜表态后再来。")
            return
    adir = project.subdir("audio")
    # 草稿两段式提醒：animatic 是零成本的全片节奏审
    anim_state = review.get_state(project.data, "animatic") \
        if project.data.get("animatic") else None
    if anim_state is None:
        _info("提示: 可先 `animatic` 出全片 Ken Burns 样片过节奏审（零视频成本），"
              "批准的镜再 gen-video --approved-only（草稿两段式）")
    elif anim_state != "done":
        _info(f"提示: 全片样片 animatic 尚未通过审阅（当前: {review.label(anim_state)}）——"
              "建议先过节奏审再烧图生视频")
    _step(f"图生视频[{mode}] · {len(shots)} 镜"
          + (" · 仅已批准镜" if approved_only else "")
          + (f" · 逐比例 {targets}" if per_aspect else "")
          + ("  原生音画" if native else "  固定音色对口型")
          + (f" · 点名 provider={vp_name}" if vp_name else "")
          + (" · 首尾帧衔接(章级)" if chain
             # 章级判据走 anchorframe.active（已含 native 闸）——裸用 CLI 覆盖位会让
             # dubbed 章也标上一个根本不会生效的「首帧锚定」
             else (" · 首帧锚定(章级)·镜间硬切"
                   if anchorframe.active(project.data, mode, anchor_frame)
                   else (" · 全能参考·一镜一片" if native else "")))
          + (" · 含镜级结对衔接" if (not chain and chain_any and native) else "")
          + (" · 参考视频 V2V(previz 运动迁移)" if v2v_on else ""))
    # 停顿门控·读侧对称闸的提示：写侧 stage_tts 那句只在跑 tts 时打得出来，而
    # 「先 kenburns 过节奏审 → 到这一步才切 dubbed」是主推顺序——在这里切模式的人
    # 同样得看见，否则只会莫名发现账单比分镜时长短了一截（请求秒数走 request_seconds）
    if mode != "kenburns" and any(any(voicecast.declared_pauses(s)) for s in shots):
        _info(f"⚠ 本模式（{mode}）下停顿不生效：delivery.pause_before/after 既不折进请求"
              f"时长也不进旁白轨——请求秒数一律按逐镜配音实测取（对口型的真相是 ref_audio "
              f"的长度），不为无声空转付费。要停顿请用 kenburns，或把停顿写进分镜节奏本身")
    if native and project.native_voiceover:
        _info("ⓘ native 混烧（native_voiceover）按镜分治：旁白/无词镜闭声出演、"
              "TTS 旁白在合成时上主轨；对白镜由模型原生发声、锚定照常附发——"
              "同一章内角色恒是模型声、旁白恒是固定音色，说话人级单声源")

    # 计费前的两道质量闸：dry-run 只把事实说清，真发交互式问一次。
    # **preview 静默**（Studio 的实发提示词面板）：那是只读预览，发问会把它挂死。
    # 比例闸按**本次真要发的镜**判（含 --only/--approved-only 过滤），语态闸按
    # 整章判——旁白占比是章级性质，拿一个子集算出来的比例没有意义。
    if preview_sink is None:
        _gate_frame_aspect(
            project, store,
            [s for s in shots
             if not review.is_omitted(s) and not transitions_mod.is_transition(s)],
            targets, dry_run=dry_run)
        _gate_voiceover(project, dry_run=dry_run)

    # 选角闸排在任何一次 PromptEnvelope 编译之前：档案决定提示词里有没有音色绑定句
    if native:
        _cast_gate(project, router, skip=no_auto_cast)

    if dry_run:   # 提示词审阅：列出每镜发给视频模型的完整提示词 + 成本预估，不调用 API、不计费
        prov0, _profile_params0 = _vroute(project.profile)
        _apply_resolution(prov0, resolution)
        total = 0
        estimate = 0.0
        if preview_sink is None:
            print("  —— 提示词审阅（--dry-run，不调用 API、不计费）——")
            if resolution == "4k":
                print("  ⚠ 4K 高成本档（并发独享 1·RPM 15/分钟）：以下按 4K 单价预估，"
                      "正式生成须 --resolution 4k --yes 二次授权")
        n_active = 0
        # 写实档敞口（见收尾一行）：路线 A 的镜被拒后才降级，近景预判镜直接以降级
        # 形态起步；板费分「真发前必买」与「被拒后才补」两笔
        n_degrade = n_direct = n_boards = n_preboards = 0
        n_lips_secs = 0            # dubbed 对白镜秒数（口型精修报价，见收尾一行）
        for s in (x for x in shots
                  if not review.is_omitted(x) and not transitions_mod.is_transition(x)):
            n_active += 1
            prof = _effective_profile(s, profile, project)
            prov, profile_params = _vroute(prof)
            _apply_resolution(prov, resolution)
            # 请求秒数 = voicecast.request_seconds 单一真源（与真发同一条）：**不能裸取 dur**
            # ——kenburns 跑过 tts 的项目 dur 里折着 delivery.pause_*，照发即为无声空转计费
            dur = voicecast.request_seconds(s, mode, adir=adir) \
                or float(project.data.get("duration", 5)) or 5
            # 预览与实发同源：链态/previz 末帧/V2V 三者从**同一个 `_shot_plan`** 取
            # （这里是无 index 的过滤生成器，拿不到位置，也正因如此不能就地重扫一遍）
            _warn_no_v2v_support(prov, s)
            _warn_no_last_frame(prov, s)
            _warn_no_tail_relay(prov)
            (nxt, why, chained, pz_last, shot_v2v, flf2v, sk_board,
             ref_mode, anchor, rv) = shot_plan = _shot_plan(s, prov)
            # 计费秒数取 provider 自身口径（seedance 4~15 整秒 / veo 4|6|8 枚举）
            # ——预估与实际同源，路由到谁就按谁的档位算，节点不失真；末帧在场与否
            # 参与取档（Veo 对首尾帧插值强制 8s），故必须在 `_shot_plan` 之后算
            n = prov.billable_seconds(dur, dubbed=not native, last_frame=flf2v)
            _warn_anchor_tradeoff(anchor)
            _warn_sketch(prov, s, dur, ref_mode=ref_mode, board=sk_board)
            relay_src, relay_ok, plan_tails = _relay_plan(s, prov, shot_plan)
            route0, route_why0, base0, board0 = _face_route(
                s, prof, prov, ref_mode, sk_board)
            pre_board = (route0 == "C" and "无板" in route_why0
                         and not ((s.get("gen") or {}).get("clip_approval") or {}).get("sha"))
            if _identity_of(prof) and _ref_task(prov, ref_mode, _control_v2v(s)):
                if route0 == "A":
                    n_degrade += 1
                    # 被拒后补板的只有路线 A 的镜；控制视频的 V2V 降级恒不挂板
                    if not _control_v2v(s) and not _fallback_board(s, prov, sk_board):
                        n_boards += 1
                else:
                    n_direct += 1
                    if pre_board:
                        n_preboards += 1
            if not native and voicecast.voice_kind(s) == "dialogue":
                n_lips_secs += n
            rp0, dropped0 = _ref_plan(s, prov, ref_mode, board=board0,
                                      tails=plan_tails, route=route0, base=base0)
            sheets0 = rp0.sheet_paths if rp0 else []
            manifest0 = rp0.manifest if rp0 else None
            # 音色锚定预览：dry-run 只读在盘事实（绝不预热、绝不落盘），
            # 「待预热」标注即真发时会现场合成的那几把官方音色
            aplan0 = _anchor_plan_for(s, prov, ref_mode)
            va_state0 = _anchor_state(aplan0) if aplan0 else []
            if rv:   # V2V 计费含输入视频秒（token 制）——报价不含就与账单差一整段
                n += prov.input_video_seconds(rv[2])
            # **乘比例数**：真跑对 targets 里每个比例各 generate 一次（`--image-per-aspect`
            # + 双比例即 2 次）。只累加一份的话，双比例报价只有实际的一半——
            # 报价只有账单的一半，故与事前闸 `_plan_cost` 统一口径。
            total += n * len(targets)
            price = getattr(prov, "effective_price_per_second", 0) \
                or getattr(prov, "price_per_second", 1.0)
            estimate += n * len(targets) * price
            # `图=` 报的必须是**真占 image 位**的那一张：降级路线上分镜图整个不进
            # 请求，照报分镜图就是拿一张没发出去的图给报价背书（成功行同款纪律）
            img = base0 if route0 != "A" else project.image_for(s, project.aspect)
            imgname = Path(str(img)).name if img else "⚠缺图"
            src = ("台词内嵌 prompt" if native
                   else (f"配音=shot_{s['id']}.wav（对口型）"
                         if voicecast.shot_text(s)
                         else "静音占位（无台词·闭唇）"))
            # closeup 预判镜缺板时，真发在计费前就地补板并按路线 B 发出（审阅锁
            # 在场则保持无板 C 与锁稿一致）——本行按当前在盘形态编译，升级须
            # 注记：无注记时审的是无板 C 的稿，发出的却是多一句板职责句的 B 稿
            route_note = f"路线{route0}·" if route0 != "A" else ""
            if pre_board:
                route_note = "路线C→B(真发前自动补板)·"
            if prompts_mod.video_delta_missing(s):
                _warn_no_motion_design(s, flf2v)
            line = (f"  ▸ 镜{s['id']} · {n}s · 图={imgname} · {src}"
                    + (f" · 末帧=镜{nxt['id']}" if chained else "")
                    + (" · 末帧=previz" if pz_last else "")
                    # 衔接态下没末帧的镜要说明原因：一串「末帧=镜N」里夹着一镜没有时，
                    # 须能区分遗漏与正常结果（转场断链与末镜均属后者）
                    + (f" · {brk}" if (brk := _chain_break_note(
                        nxt, why, sent_last=flf2v, v2v=shot_v2v, ref_mode=ref_mode,
                        can_last=getattr(prov, "supports_last_frame", True))) else "")
                    + (f" · 参考视频={rv[0]} {rv[2]:.1f}s" if rv else "")
                    + ((f" · 全能参考("
                        + route_note
                        + f"{_ref_note(board0, manifest0, bool(plan_tails), dropped0, route0)}"
                        "·一镜一片)")
                       if ref_mode
                       # 首帧锚定：分镜图进 first_frame 槽，设定图/板/尾帧全部让位，
                       # 故这里不复用 _ref_note（它描述的是参考媒体那一组）
                       else (" · 首帧锚定(分镜图=第0帧·无参考图·镜间硬切)" if anchor
                             else ((f" · 参考图={route_note}"
                                    f"{_ref_note(board0, manifest0, bool(plan_tails), dropped0, route0)}")
                                   if (board0 or sheets0 or plan_tails)
                                   else (" · 分段时间轴(无板)" if _sk_timeline(s) else ""))))
                    # 承接注记：计划期盘上有尾帧即已入提示词；没有则说明真发时会在
                    # 上一镜回填后注入（dry-run 审到的基线提示词届时多一句承接职责句）
                    + ((f" · 承接=镜{relay_src['id']}尾帧") if plan_tails
                       else ((f" · 承接=镜{relay_src['id']}尾帧(生成时注入)")
                             if relay_ok and relay_src else ""))
                    + _anchor_note(va_state0, (aplan0 or {}).get("loose") or [],
                                   prov=prov, over=(aplan0 or {}).get("over") or []))
            if preview_sink is None:
                print(line)
            # 审阅的必须是**真发的那一条**：角色锚同样要装配进来，否则页面上审过的
            # 提示词与实际请求不是同一句，审阅这道闸就形同虚设。
            # preview 不抛镜级闸：页面要的是「看见问题」，拦断整章预览反而藏住它
            gate_note = None
            if preview_sink is None:
                _gate_cast_anchor(project, s, img, route=route0, ref_plan=rp0)
            else:
                try:
                    _gate_cast_anchor(project, s, img, route=route0, ref_plan=rp0)
                except ProjectError as e:
                    gate_note = str(e)
            try:
                envelope = _video_envelope(
                    s, prov, prof, profile_params, dur, shot_plan, sheets0, manifest0,
                    tail_refs=plan_tails,
                    voice_anchors=[{"who": r["who"], "no": r["no"],
                                    "desc": r.get("desc")} for r in va_state0],
                    voice_refs=[(r["voice_type"], r["clip"])
                                for r in va_state0 if r["clip"]],
                    route=route0, base_image=base0,
                    board=(rp0.board if rp0 else None))
            except Exception as e:  # noqa: BLE001  仅 preview 降级；dry-run 保持 fail loud
                if preview_sink is None:
                    raise
                preview_sink.append({"id": s.get("id"), "error": str(e),
                                     "note": line.strip()})
                continue
            if preview_sink is not None:
                # `@图片N` 编号 → 实附文件的映射（页面点击查看用）：编号真源就是
                # RefPlan（与 ref_manifest 及提示词绑定句同一次装配）——前端
                # 自行推断必然与实发错位。降级路线下 image 位是场景基准图。
                # RefPlan 为 None（零设定集/无板/无尾帧）时参考任务仍附本镜画面
                # 且契约句照写 @图片1——映射必须补上这一位，否则页面把它渲染成
                # 不可点的失效记号
                lead = base0 or img
                refs = (rp0.preview(lead, project.aspect) if rp0
                        else ([{"no": 1,
                                "kind": ("scene_base" if route0 != "A"
                                         else "frame"),
                                "name": "", "path": str(lead)}]
                              if lead and (ref_mode or not native) else []))
                # 中英对照的另一语种编译（展示用，不发送）：措辞真源同一份，
                # 作者缺 `_en` 字段时正文按「缺失互为回退」取中文值、只换引擎措辞
                lang0 = getattr(prov, "prompt_lang", "zh")
                alt_lang = "en" if lang0 != "en" else "zh"
                env_alt = _video_envelope(
                    s, prov, prof, profile_params, dur, shot_plan, sheets0,
                    manifest0, tail_refs=plan_tails,
                    voice_anchors=[{"who": r["who"], "no": r["no"],
                                    "desc": r.get("desc")} for r in va_state0],
                    voice_refs=[(r["voice_type"], r["clip"])
                                for r in va_state0 if r["clip"]],
                    lang=alt_lang, route=route0, base_image=base0,
                    board=(rp0.board if rp0 else None))
                appr = ((s.get("gen") or {}).get("clip_approval") or {})
                sha = _prompt_sha(envelope.prompt)
                preview_sink.append({
                    "id": s.get("id"), "prompt": envelope.prompt,
                    # positive/negative 分列下发：页面正文放 positive、负面串独立成块
                    # ——prompt 全文里尾接负面句，两者同屏即同一批词显示两遍
                    "positive": envelope.positive,
                    "negative": envelope.negative,
                    "lang": lang0,
                    "alt": {"lang": alt_lang, "positive": env_alt.positive,
                            "negative": env_alt.negative},
                    # 审阅锁三态：ok=审过且当前稿一致 / stale=审后字段有变 / None=未审
                    "prompt_sha": sha,
                    "approval": (("ok" if appr.get("sha") == sha else "stale")
                                 if appr.get("sha") else None),
                    "fingerprint": envelope.fingerprint,
                    "seconds": n, "provider": prov.name,
                    "model": getattr(prov, "model", None),
                    "resolution": getattr(prov, "resolution", None),
                    "aspects": list(targets), "mode": mode,
                    "note": line.strip(),
                    "refs": refs,
                    # @视频1（V2V 参考视频）→ 点看实体：来源、发出去的那份文件与输入
                    # 秒数。编号恒 1：每镜只发一条 reference_video，与提示词的运动半句同源
                    "videos": ([{"no": 1, "kind": rv[0], "path": rv[1], "seconds": rv[2]}]
                               if rv else []),
                    "anchors": [{"who": r["who"], "no": r["no"],
                                 "pending": not r["clip"], "clip": r["clip"]}
                                for r in va_state0],
                    "loose": (aplan0 or {}).get("loose") or [],
                    "over": (aplan0 or {}).get("over") or [],
                    # @配音1（dubbed 随请求附发的整镜配音）→ 试听实体。编号恒 1：
                    # 每镜只发一条 audio_url，与提示词的寻址句同源
                    "dub": ({"no": 1,
                             "who": ("、".join(dict.fromkeys(
                                 str(ln.get("speaker") or "").strip()
                                 for ln in voicecast.shot_lines(s)
                                 if str(ln.get("speaker") or "").strip()))
                                 or voicecast.NARRATOR_DISPLAY),
                             "clip": (str(w) if (w := voicecast.shot_audio_path(
                                 s, adir)) else None)}
                            if not native and voicecast.shot_text(s) else None),
                    "gate": gate_note})
                continue
            print("    提示词：" + envelope.prompt)
            print(f"    Envelope：{envelope.fingerprint}")
            print(f"    （引擎追加：provider={prov.name}"
                  f" 模型={getattr(prov, 'model', '—')}"
                  f" 分辨率={getattr(prov, 'resolution', '1080p')} "
                  f"比例={project.aspect} 计费时长={n}s"
                  + (f" ×{len(targets)} 比例" if len(targets) > 1 else "") + "）")
        if preview_sink is not None:
            return
        est = round(estimate, 2)
        if n_degrade or n_direct:
            # 近景预判镜的板在真发前必买；路线 A 的镜只在被人脸拒后补板（人脸拒本身
            # 不计费），两笔分开报。板按分镜图同价
            img_price = float(getattr(router.resolve("image", profile
                                                     or project.profile)[0],
                                      "price", 0) or 0)
            bits = []
            if n_degrade:
                bits.append(f"写实档 {n_degrade} 镜先按分镜图路线试，被人脸拒即降级重发"
                            "（被拒不计费、重发按秒另计）")
            if n_direct:
                bits.append(f"{n_direct} 镜按近景预判直接以降级形态发出"
                            "（再被拒即需重出身份图）")
            if n_preboards:
                bits.append(f"其中 {n_preboards} 镜真发前先出简笔板，"
                            f"+¥{round(n_preboards * img_price, 2):.2f} 板费随真发即付")
            if n_boards:
                bits.append(f"路线 A 中 {n_boards} 镜被拒时才补板，"
                            f"最坏再 +¥{round(n_boards * img_price, 2):.2f}")
            print("  ⓘ " + "；".join(bits))
        if not native and not no_lipsync and n_lips_secs > 0:
            try:
                lprov, _lpp = router.resolve("lipsync", profile or project.profile)
                lok, lwhy = lprov.configured()
            except Exception:  # noqa: BLE001  报价注记尽力而为，解析不出照实说
                lprov, lok, lwhy = None, False, "provider 解析失败"
            lp = float(getattr(lprov, "price_per_second", 0) or 0)
            if lok and lp > 0:
                print(f"  ⓘ 口型精修（dubbed 章对白镜）：对白镜 ≈ {n_lips_secs}s × "
                      f"¥{lp}/s ≈ +¥{round(n_lips_secs * lp, 2)}（--no-lipsync 可关）")
            elif lok:
                print(f"  ⓘ 口型精修（dubbed 章对白镜）：对白镜 ≈ {n_lips_secs}s，"
                      "单价未配置（providers.volc-lipsync.price_per_second）——将按 0 入账")
            else:
                print(f"  ⓘ 口型精修未配置（{lwhy}）——本次将跳过，对白镜按底片口型出片")
        print(f"  —— 共 {n_active} 镜"
              + (f" × {len(targets)} 比例" if len(targets) > 1 else "")
              + f" ≈ {total}s，逐镜 provider 预估费用 ≈ {est} 元。"
                f"审阅无误后**去掉 --dry-run** 正式生成。——")
        # 真跑口径（与事前闸 `_preflight_spend` 取同一张 `_will_burn` 清单）：上面那份
        # 是**全片**报价（审提示词要看全片），真跑会跳过 done 锁定/弃用/已有片段/
        # 设定图不齐的镜。断点续跑时两个数字差距很大，并排打出以说明口径差异。
        plan = _will_burn(project, shots, targets, force, ignore_refs=ignore_refs)
        b_total, b_calls, _b_max = _plan_cost(project, plan, prov0, mode=mode,
                                              native=native, adir=adir,
                                              v2v=v2v_on, control=control_on,
                                              # 末帧参与 Veo 取档（插值 8s），与真发同源
                                              sends_last=lambda s: _shot_plan(s, prov0)[5])
        if b_calls != n_active * len(targets):
            price = getattr(prov0, "effective_price_per_second", 0) \
                or getattr(prov0, "price_per_second", 1.0)
            print(f"  ⓘ 本次真跑只发 {len(plan)} 镜 / {b_calls} 次调用 ≈ {b_total}s "
                  f"≈ ¥{round(b_total * price, 2):.2f}"
                  "（已跳过 done 锁定 / 弃用 / 已有片段 / 设定图不齐的镜）")
        # 预估入台账（双轨制）：dry-run 审提示词的同时把预估落盘，
        # 渲染后 ledger 直接对照实际——预估不落盘，超支判定就没有对照基准。
        # **--only 过滤时不落盘**：单镜/局部报价覆盖掉全片预估 = ledger 的
        # 「预估(video)」列从此对不上任何真实口径（打印照打，只是不改台账）。
        if only or approved_only:
            # --approved-only 与 --only 同为镜级子集过滤：子集报价覆盖全片预估，
            # ledger 的「预估(video)」列从此对不上任何真实口径
            _info("预估不入台账（镜级子集口径，不覆盖全片预估）")
            return
        if b_calls == 0:
            # 真跑一镜都发不出（设定图不齐 / 缺图 / 全部在盘或锁定）：此刻的全片预估
            # 对不上任何一次真实口径，不写台账
            _info("预估不入台账（本次真跑一镜都发不出）")
            return
        from datetime import datetime as _dt
        average_price = round(est / total, 6) if total else 0
        project.data.setdefault("cost_estimate", {})["video"] = {
            "amount": est, "seconds": total, "price_per_second": average_price,
            "at": _dt.now().isoformat(timespec="seconds")}
        project.save()
        _info(f"预估 ¥{est} 已写入 cost_estimate（只作报价对照，不计入实付台账）")
        return

    # 预留额度（事前闸）：在第一次调用发出**之前**把整批算清楚。mock 无成本、
    # provider 与 dry-run 同口径按项目 profile 解析一个（逐镜混画风时是近似，
    # 但混画风本身就少见，近似口径足以拦住量级偏差）。
    if not router.force_mock:
        prov0, _profile_params0 = _vroute(project.profile)
        _apply_resolution(prov0, resolution)
        _preflight_spend(project,
                         _will_burn(project, shots, targets, force, ignore_refs=ignore_refs),
                         prov0, mode=mode, native=native, adir=adir, targets=targets,
                         confirm_spend=confirm_spend, auto=auto,
                         v2v=v2v_on and prov0.supports_reference_video,
                         control=control_on and prov0.supports_reference_video,
                         # 末帧参与 Veo 取档（插值 8s），与真发同源
                         sends_last=lambda s: _shot_plan(s, prov0)[5])

    # ── 并发三段式（铁律见 kinema/parallel.py）──────────────────────────
    # ① 主线程定计划：状态机闸门、就绪度节点、链态仲裁、previz 上云、提示词拼装、
    #    缺图/缺配音校验全在这里——都有副作用或读文档，绝不进工作线程。校验前置
    #    另有一层收益：任何一镜缺前置产物都在首次计费之前拦下，而不是烧到那一镜
    #    才发现。
    # ② 工作线程只发请求、只写自己的片段文件；
    # ③ 主线程按提交顺序回填：clips/dur/快照/审阅/记账/落盘全单线程。
    # dubbed 的配音前置闸：缺 wav 的有词镜在**任何付费动作之前**整批点名——
    # closeup 补板在计划循环内逐镜发生，逐镜才查配音会让前面镜的板费先花出去、
    # 批次才停在计划期。无台词镜不在此列：静音占位由计划循环现场补（本地零成本）
    if not native:
        no_wav = [s.get("id") for s in shots
                  if not transitions_mod.is_transition(s)
                  and not _regen_gate(project, s, "clip", force, quiet=True)[0]
                  and voicecast.shot_text(s)
                  and (force or review.needs_retake(s, "clip")
                       or any(not has_file((s.get("clips") or {}).get(a))
                              for a in targets))
                  and not has_file(str(adir / f"shot_{s.get('id')}.wav"))]
        if no_wav:
            raise ProjectError(
                f"dubbed 需先 tts：镜 {'/'.join(str(i) for i in no_wav)} 缺配音——"
                "先运行 tts --chapter 再重跑本命令")
    # closeup 预判镜缺板时，在计划循环之前整批并发出板（`stage_sketch_boards`
    # 三段式，与降级轮同一出口）。循环内的就地补板是单镜同步出口，逐镜串行每张
    # 约两分钟，四镜就让整批空等六分钟。判据与循环内那处完全一致：仲裁落在无板 C、
    # 审阅锁不在场；板到位后循环里的 _face_route 直接仲裁成 B。dry-run 不买板。
    if not dry_run:
        pre_boards: dict = {}
        for s in shots:
            if (transitions_mod.is_transition(s)
                    or str(s.get("face_visibility") or "").strip() != "closeup"
                    or _regen_gate(project, s, "clip", force, quiet=True)[0]
                    or ((s.get("gen") or {}).get("clip_approval") or {}).get("sha")
                    or (not ignore_refs and project.has_design
                        and not lineage.readiness(project, s)[0])):
                continue
            prof = _effective_profile(s, profile, project)
            prov, _pp = _vroute(prof)
            sp = _shot_plan(s, prov)
            route0, why0, _base0, _board0 = _face_route(s, prof, prov, sp[7], sp[6])
            if route0 == "C" and "无板" in why0:
                pre_boards.setdefault(prof, []).append(s)
        for prof, need in pre_boards.items():
            _info(f"closeup 预判 {len(need)} 镜缺简笔板：真发前整批并发出板（计入图像台账）")
            r = stage_sketch_boards(project, store, router, need, prof=prof)
            if r["budget_err"]:      # 预算闸：已出的板已登记入账，整批停在计划期
                raise r["budget_err"]
    plan: list[dict] = []
    for s in shots:      # 链态一律查预计算的 chain_map，不依赖本次循环下标
        if transitions_mod.is_transition(s):   # 转场镜不走图生视频（合成段本地渲染字卡）
            continue
        skip, regen = _regen_gate(project, s, "clip", force)   # 状态机×版本栈节点
        if skip:
            continue
        if not ignore_refs and project.has_design:   # 就绪度节点（省钱闸）
            ok, missing = lineage.readiness(project, s)
            if not ok:
                _info(f"镜 {s.get('id')}: ⊘ 设定图不齐（{', '.join(missing)}），"
                      "跳过图生视频——先 project refs 补齐（硬要跑加 --ignore-refs）")
                continue
        prof = _effective_profile(s, profile, project)
        prov, profile_params = _vroute(prof)
        _apply_resolution(prov, resolution)
        # 链态与提示词都**在比例循环之外**算一次：gen.clip 快照只存一条 prompt，
        # 放进循环会只记下最后一个比例那条，与 dry-run 台账/版本栈 params 对不上。
        # 与 dry-run 报价同一条读侧真源（口径分叉 = 报价与账单对不上，见 request_seconds）
        # ——必须先于 vprompt 算好：简笔板时间轴的秒段按它铺（sketch_total）；
        # 也先于 _warn_sketch：拍密度体检按同一个秒数判，绝不在告警里再调一次
        # （那会让本函数出现第三处 request_seconds，破坏「报价与真发恰好两处」的守卫）
        dur = voicecast.request_seconds(s, mode, adir=adir) \
            or float(project.data.get("duration", 5)) or 5
        _warn_no_v2v_support(prov, s)
        _warn_no_last_frame(prov, s)
        _warn_no_tail_relay(prov)
        (nxt, why, chained, pz_last, shot_v2v, flf2v, sk_board,
         ref_mode, anchor, rv) = shot_plan = _shot_plan(s, prov)
        _warn_anchor_tradeoff(anchor)
        _warn_sketch(prov, s, dur, ref_mode=ref_mode, board=sk_board)
        relay_src, relay_ok, plan_tails = _relay_plan(s, prov, shot_plan)
        if nxt and not chained and not pz_last and not shot_v2v and why == "":
            if ref_mode:
                _info(f"镜 {s['id']}: 下一镜(镜{nxt.get('id')})缺图，衔接不成立——"
                      "本镜按缺省全能参考生成；补齐下一镜的图再重生即可衔接")
            else:
                _info(f"镜 {s['id']}: 下一镜(镜{nxt.get('id')})缺图，本镜退回常规首帧生成"
                      "（不发末帧、提示词也不写过渡）——补齐下一镜的图再重生即可衔接")
        # 控制段发出去的是它的无声副本；盘上带源片音轨的那份是审看件，血缘也记它
        ref_video_url = _ref_video_url(
            s["id"], control_mod.send_path(rv[1]) if rv[0] == "control" else rv[1],
            ws_root, mock=router.force_mock) if rv else None
        if prompts_mod.video_delta_missing(s):
            _warn_no_motion_design(s, flf2v)
        # 随请求附发的设定图组合在比例循环之外取一次：路径清单发请求，
        # @图片N 编号清单进提示词——真附了几张、附在第几位，句子才配那样写
        route, route_why, base, board = _face_route(s, prof, prov, ref_mode, sk_board)
        if route == "C" and "无板" in route_why:
            # closeup 预判镜缺板：与降级轮同一个出口就地生板再走 B——否则作者的
            # 正确表态反而换来更弱的一档（C 一旦过闸就永远进不了补板轮）。
            # 审阅锁在场时不代买：审阅稿按无板 C 形态编译过，买板升 B 的实发稿
            # sha 必不一致、镜随即被跳过——板钱就白花了（闸在计费之前）
            if ((s.get("gen") or {}).get("clip_approval") or {}).get("sha"):
                _info(f"镜 {s['id']}: 审阅锁在场，不代买简笔板——按无板路线C比对审阅稿")
            else:
                try:
                    bp = stage_sketch_gen(project, store, router, s, prof)
                except KinemaError:
                    raise      # 预算闸：板已登记入账，整批停在计划期（同 add_cost 语义）
                except Exception as e:  # noqa: BLE001  生板失败按无板路线 C 继续
                    bp = None
                    _info(f"⚠ 镜 {s['id']} 预生板失败（{e}）——按无板路线C发出")
                if bp:
                    # 板到位即仲裁输入变化，路线与理由一并重取（同降级轮）——
                    # 只改 route 会打印「路线B（…无板…）」这种自相矛盾的日志
                    route, route_why, base, board = _face_route(
                        s, prof, prov, ref_mode, sk_board)
                    _info(f"镜 {s['id']}: 已就地生成简笔板（closeup 预判·计入图像台账）")
        if route != "A":
            _info(f"镜 {s['id']}: 降级路线{route}（{route_why}）——"
                  "分镜图不进请求，image 位由场景基准图顶替")
        rp, dropped = _ref_plan(s, prov, ref_mode, board=board,
                                tails=plan_tails, route=route, base=base)
        sheet_refs = rp.sheet_paths if rp else []
        ref_manifest = rp.manifest if rp else None
        # 音色锚定落料（预热/裁剪都在主线程计划期——工作线程绝不写共享缓存）：
        # anchors 进提示词绑定句、refs 随请求附发，两者由同一次重排产出恒对位
        aplan = _anchor_plan_for(s, prov, ref_mode)
        va_anchors, va_refs = (_anchor_clips(s, prov, _anchor_state(aplan))
                               if aplan else ([], []))
        if va_refs:
            _info(f"镜 {s['id']}: 音色锚定 ×{len(va_refs)}"
                  f"（{'+'.join(a['who'] for a in va_anchors)}）随请求附发")
        # 角色锚：首帧/首尾帧请求带不了设定图（协议互斥），文字锚点仍要送达；
        # 全能参考虽真附设定图，文字锚照发（图锁脸、文字锁名字与硬约束，各管一段）
        envelope = _video_envelope(
            s, prov, prof, profile_params, dur, shot_plan, sheet_refs, ref_manifest,
            tail_refs=plan_tails, voice_anchors=va_anchors, voice_refs=va_refs,
            route=route, base_image=base, board=(rp.board if rp else None))
        vprompt = envelope.prompt
        # 实发稿审阅锁（可选节点）：Studio 通过过的镜，真发前比对当前稿的正文 sha。
        # 一致=审过的就是发出的（重编译数学等价于「直接用」）；不一致=审后字段有变，
        # 跳过并点名，绝不把没审过的稿静默发出去。未上锁的镜照旧直发。
        # 尾帧接力的承接句注入发生在闸后（引擎既定增量），不作失效判定。
        appr = ((s.get("gen") or {}).get("clip_approval") or {})
        if appr.get("sha"):
            cur = _prompt_sha(vprompt)
            if cur != appr["sha"]:
                _info(f"镜 {s['id']}: ⊘ 实发稿与审阅版不一致（审后字段有变）——本镜跳过；"
                      "在 Studio「提示词 · PROMPTS」重新通过，或撤销审阅锁后重跑")
                continue
            _info(f"镜 {s['id']}: 审阅锁 ✓ 实发稿与审阅版一致（{cur[:8]}）")
        clips = s.setdefault("clips", {})
        todo: list[dict] = []
        for asp in targets:
            if not regen and has_file(clips.get(asp)):
                continue
            outp = project.subdir("gen_clips") / f"shot_{s['id']}_{aspect_tag(asp)}.mp4"
            if not regen and _salvageable_clip(project, s["id"], asp):
                _info(f"镜 {s['id']} {asp}: 片段已在盘（上次中断未登记）→ 直接登记不重买")
                todo.append({"asp": asp, "reuse": True, "out": str(outp)})
                continue
            if route == "A":
                img = project.image_for(s, asp)
                if not has_file(img):
                    raise ProjectError(f"镜 {s['id']} 缺少图，请先运行 gen-image。")
            else:
                img = base   # 降级路线：场景基准图顶 image 位（路线前置已验在盘）
            _gate_cast_anchor(project, s, img, route=route,
                              ref_plan=rp)   # 身份来源缺席 → 拦在计费之前
            # 末帧三选一（判据全在 `_shot_plan` 定死）：V2V 下不发 · previz 末帧优先 ·
            # 否则链上下一镜图（已逐比例 has_file 验过）。提示词写了「只写过渡」
            # 就一定发得出末帧，两者恒同源
            last = pz_last or (project.image_for(nxt, asp) if chained else None)
            ra = None
            if not native and not getattr(prov, "supports_ref_audio", False):
                # 计划期即拦：不支持的适配器会把 **kwargs 里的 ref_audio 静默吞掉，
                # 等 generate 抛错时已进入逐镜循环，计划期拦截避免半途中断
                raise ProjectError(
                    f"provider '{prov.name}' 不支持 dubbed 对口型（ref_audio）——"
                    "请用 seedance（--video-provider seedance-mini）或改 native 模式")
            if not native:  # dubbed 需要该镜配音（音频与比例无关）
                wav = adir / f"shot_{s['id']}.wav"
                if not has_file(wav):
                    if voicecast.shot_text(s):
                        raise ProjectError(f"镜 {s['id']} 缺配音(dubbed 需先 tts)：{wav}")
                    # 纯画面镜的对口型素材是等长静音：模型对静音的正确执行是
                    # 闭唇（提示词侧同源门控 voice_kind），tts 只为有词镜合成，
                    # 占位由消费方现场生成——本地 ffmpeg 零成本、同名幂等
                    adir.mkdir(parents=True, exist_ok=True)
                    from .ffmpeg import run as _ffrun
                    _ffrun(["-f", "lavfi", "-i",
                            f"anullsrc=r=44100:cl=mono:d={dur:.3f}",
                            "-t", f"{dur:.3f}", str(wav)], desc="静音占位")
                    _info(f"镜 {s['id']}: 无台词 → 生成 {dur:.2f}s 静音占位"
                          "（对口型素材=静音·闭唇）")
                ra = str(wav)
                # 长镜垫窗：参考媒体模式下片段时长跟随音频，配音短于设计窗口时
                # 把 ref_audio 垫静音尾补到请求秒数——不垫的话窗口被拉回台词
                # 长度，台词之外的表演区间整段丢失。产物按窗口秒数命名幂等，
                # 换音色（wav 变新）自动重垫
                n_win = prov.billable_seconds(dur, dubbed=True)
                try:
                    alen = probe_duration(str(wav))
                except Exception:  # noqa: BLE001  探不出长度按原样发送，服务端裁决
                    alen = None
                if alen is not None and alen < n_win - 0.05:
                    win_wav = adir / f"shot_{s['id']}_win{n_win}s.wav"
                    if (not win_wav.is_file()
                            or win_wav.stat().st_mtime < wav.stat().st_mtime):
                        from .ffmpeg import run as _ffrun
                        _ffrun(["-i", str(wav),
                                "-af", f"apad=whole_dur={n_win}",
                                str(win_wav)], desc="配音垫窗")
                    ra = str(win_wav)
            w, h = store.canvas(asp)
            todo.append({"asp": asp, "img": img, "last": last, "ra": ra,
                         "w": w, "h": h, "out": str(outp)})
        plan.append({"shot": s, "prov": prov, "vprompt": vprompt,
                     "envelope": envelope, "dur": dur, "regen": regen,
                     "todo": todo, "nxt": nxt, "why": why, "pz_last": pz_last,
                     "shot_v2v": shot_v2v, "flf2v": flf2v, "sk_board": sk_board,
                     "ref_mode": ref_mode, "sheet_refs": sheet_refs,
                     "ref_plan": rp, "route": route, "base": base,
                     "anchor": anchor, "dropped": dropped,
                     "ref_video_url": ref_video_url,
                     "ref_video_kind": rv[0] if rv else None,
                     # 本地路径供血缘指纹：上云后 `ref_video_url` 是公网地址，指纹取不到文件
                     "ref_video_path": rv[1] if rv else None,
                     "ref_video_seconds": rv[2] if rv else 0.0,
                     # 断因措辞要说「本模型没有末帧槽」而不是「下一镜缺图」
                     "can_last": getattr(prov, "supports_last_frame", True),
                     # 尾帧接力：capture=本镜请求尾帧回传；relay_*/prof/shot_plan 等
                     # 是 `_relay_inject` 在上一镜回填后向本镜重编译提示词的素材
                     "capture_tail": relay_on and getattr(
                         prov, "supports_return_last_frame", False),
                     "relay_ok": relay_ok,
                     "relay_src_id": (relay_src or {}).get("id"),
                     "tail_refs": plan_tails or {},
                     # 音色锚定：clips 随请求附发（与比例无关），anchors/refs 是
                     # 接力注入重编译提示词时的素材（绑定句不许在重编译时丢失）
                     "va_anchors": va_anchors, "va_refs": va_refs,
                     "prof": prof, "profile_params": profile_params,
                     "shot_plan": shot_plan, "ref_manifest": ref_manifest,
                     # 同 gen-image：invalidate 只作废计划期已看见的判定
                     "cn_seen": bool(s.get("consistency"))})

    if not plan:
        return
    for i, it in enumerate(plan):   # 接力注入按计划位次找下一项（见 _relay_inject）
        it["idx"] = i

    # 并发度：**缺省恒串行（workers=1），显式 --concurrency 才开**——视频按秒计费
    # 单价高，静默并发等于同时下多张高价订单；也刻意不吃 KINEMA_CONCURRENCY 环境
    # 变量（给生图配的全局值不该顺手把视频也并发了）。4K 档并发配额为 1（RPM 15），
    # 点了并发也压回串行。
    workers = 1 if concurrency is None else parallel.resolve_workers(concurrency)
    if workers > 1 and any(getattr(x["prov"], "resolution", "") == "4k" for x in plan):
        _info("⚠ 4K 档并发配额为 1（RPM 15/分钟）——本批强制串行")
        workers = 1
    # 串行只对「真会接力的批」强制：能力位不齐的 provider（如 minimax-h3 无尾帧
    # 回传与参考图通道）整批接不了力，剥夺它的并发换不来任何承接
    relay_live = relay_on and any(x.get("capture_tail") or x.get("relay_ok")
                                  for x in plan)
    if relay_live and workers > 1:
        # 注入次序依赖 parallel.run 的 workers=1 内联串行（跑完一件、回填一件、
        # 才派下一件）；并发下无法保证上一镜先回填，接力必然踩空
        _info("⚠ 尾帧接力（tail_relay）依赖按成片顺序串行注入——本批强制串行")
        workers = 1
    if workers > 1:
        _info(f"并发图生视频 · {workers} 镜同时（预算闸事前对账·失败即停派·重试恒关）")

    _archive_regen(project, plan, "clip")   # 计划成功后才动旧产物
    _mark_wip(project, plan, "clip")   # Studio 忙态：本批全部进入「生成中」

    def _work(item):
        """工作线程：只发请求、只写自己的片段文件，**一行文档都不碰**。"""
        s, prov = item["shot"], item["prov"]
        out = {"clips": {}, "cost": 0.0, "generated": False, "sent_last": False}
        try:
            _work_aspects(item, out)
        except Exception as e:
            if out["clips"]:
                e.partial = out       # 已付费的比例随异常带回主线程登记入账
            raise
        return out

    def _work_aspects(item, out):
        prov = item["prov"]
        for t in item["todo"]:
            if t.get("reuse"):          # 断点捡回：不发请求，零成本登记（走同一条登记链）
                out["clips"][t["asp"]] = t["out"]
                out["salvaged"] = True
                continue
            # 参考图顺序=提示词职责声明顺序，唯一真源 RefPlan（分镜图占 image
            # 参数领衔 → 板 → 上镜尾帧 → 设定图）。native 全能参考与 dubbed
            # 参考媒体两档同规（首帧任务恒 None）。尾帧逐比例取——同比例承接同比例
            refs = item["ref_plan"].refs_for(t["asp"]) if item.get("ref_plan") else None
            # 尾帧回传只在接力批显式请求：request body 是强校验，常态多发一个
            # 参数换来的可能是远端 400
            extra = {"return_last_frame": True} if item.get("capture_tail") else {}
            res = prov.generate(t["img"], t["out"], prompt=item["vprompt"],
                                dur=item["dur"], width=t["w"], height=t["h"],
                                seed=seed, last_frame=t["last"], ref_audio=t["ra"],
                                reference_video=item["ref_video_url"],
                                reference_video_seconds=item["ref_video_seconds"],
                                ref_images=refs,
                                reference_only=item["ref_mode"],
                                voice_anchors=[c for _vt, c in item["va_refs"]] or None,
                                **extra)
            if item.get("capture_tail"):
                url = (getattr(res, "meta", None) or {}).get("last_frame_url")
                if url:
                    out.setdefault("tails", {})[t["asp"]] = url
            out["sent_last"] = out["sent_last"] or t["last"] is not None
            out["clips"][t["asp"]] = res.path
            out["cost"] += res.cost
            out["generated"] = True
            # 服务端任务号随快照留痕（多比例时留最后一次真发的那个）：出问题时
            # 厂商只认它，事后从成片文件反查不出来
            out["task_id"] = (getattr(res, "meta", None) or {}).get("task_id")

    failed: list = []
    face_failed: list = []    # 人脸拒单列：建任务 400 不计费、可降级——不触发停派
    # 输出侧审核拒单列：成片渲染后被扫描拦下——判的是生成内容、逐镜独立，
    # 降级换装配与同参数重跑都改变不了判定，故不进降级轮；一镜的内容判定
    # 也不该把其余镜的派活停掉（与人脸拒同款分流，处置文案各自专属）
    output_failed: list = []
    budget_stop: dict = {"err": None}   # 预算断闸：停派新活，不打断本批收尾
    relay_missed: dict = {}   # 尾帧回传落空的点名闩锁（每 provider 一次，不刷屏）

    def _persist_tails(s, tails: dict) -> dict:
        """把本镜回传的尾帧落到 `<章节>_work/tail/`（逐比例），返回登记映射。

        官方 URL 有时效，回填时即刻落盘；mock 等离线 provider 回的是本地路径，
        走复制。**单帧失败只丢那一帧不抛**——片段已按秒付费落袋，尾帧只是接力
        素材，丢帧的代价由下一镜降级承担（盘上有多少用多少，不齐即不接力）。
        尾帧不进版本栈（同简笔板纪律：接力素材，重生成同名覆写）。"""
        out = {}
        for asp, src in (tails or {}).items():
            dest = project.subdir("tail") / f"shot_{s['id']}_{aspect_tag(asp)}_tail.png"
            try:
                if str(src).startswith(("http://", "https://")):
                    from .providers._util import download
                    download(src, dest)
                else:
                    import shutil
                    shutil.copyfile(src, dest)
            except Exception as e:  # noqa: BLE001
                _info(f"⚠ 镜 {s['id']} {asp} 尾帧落盘失败（{e}）——该比例不参与接力")
                continue
            out[asp] = str(dest)
        return out

    def _relay_recompile(nxt_item, tails):
        """按给定尾帧（None=无承接）重编译下一计划项的提示词与附图清单。

        注入与撤销共用这一个出口：提示词声明、@图片N 编号、实附 ref_images 三者
        必须一次改齐——只改其一就是「声明了一张不存在的参考」或反过来。
        编译失败保留计划项现状并返回 False（调用方决定要不要喊）。"""
        ns = nxt_item["shot"]
        # 裁剪清单在计划期已由报价/真发那两处点过名，承接重编译不重复喊。
        # RefPlan 整体重建而非原位追加尾帧：注入一张尾帧要让出一席配额，
        # 路线不重算不等于配额不重算——原位追加会撞 provider 的 [:7] 静默截断
        route = nxt_item.get("route", "A")
        base = nxt_item.get("base")
        old_rp = nxt_item.get("ref_plan")
        rp2, dropped2 = _ref_plan(ns, nxt_item["prov"], nxt_item["ref_mode"],
                                  board=(old_rp.board if old_rp else
                                         nxt_item["sk_board"]),
                                  tails=tails, route=route, base=base)
        sheets = rp2.sheet_paths if rp2 else []
        manifest = rp2.manifest if rp2 else None
        try:
            env = _video_envelope(ns, nxt_item["prov"], nxt_item["prof"],
                                  nxt_item["profile_params"], nxt_item["dur"],
                                  nxt_item["shot_plan"], sheets, manifest,
                                  tail_refs=tails,
                                  voice_anchors=nxt_item.get("va_anchors"),
                                  voice_refs=nxt_item.get("va_refs"),
                                  route=route, base_image=base,
                                  board=(rp2.board if rp2 else None))
        except Exception as e:  # noqa: BLE001  字数超限等编译失败不断批
            _info(f"⚠ 镜 {ns['id']} 承接提示词重编译失败（{e}）——保持原提示词发出")
            return False
        nxt_item.update({"vprompt": env.prompt, "envelope": env,
                         "sheet_refs": sheets, "ref_manifest": manifest,
                         # 裁剪清单随重算同步：尾帧多让出一席后实裁的项变了，
                         # 收尾日志仍拿计划期旧清单就是在少报一件被丢的东西
                         "ref_plan": rp2, "dropped": list(dropped2),
                         "tail_refs": dict(tails or {})})
        return True

    def _relay_inject(item, r):
        """上一镜回填完成后，把尾帧注入**下一个计划项**并重编译其提示词。

        次序由 workers=1 的内联串行保证（parallel.run：跑完一件、回填一件、才派
        下一件）。新鲜 URL 优先——免一次落盘往返，且不受官方产物 URL 时效影响；
        本轮落盘副本兜底（受信绑生成方式不绑字节，落盘重传不丢受信，
        见 docs/kinema/seedance-face-policy.md §2.2）。

        **拿不到尾帧时的兜底是撤销而不是保留**：本镜刚重生成过，计划期按旧版
        尾帧编入的承接句指向一张已随版本失效的图——带着它发出去，就是让模型
        「从一个不再存在的收尾延续」。撤销后下一镜按无承接的基线提示词发出。"""
        nxt_item = plan[item["idx"] + 1] if item["idx"] + 1 < len(plan) else None
        if not (nxt_item and nxt_item.get("relay_ok")
                and nxt_item.get("relay_src_id") == item["shot"].get("id")):
            return
        if nxt_item.get("_done") or nxt_item.get("_failed"):
            # 下一计划项已封笔（首轮出片）或已死（失败且未进重发队列）——
            # 它的请求不会再发，改写计划项只会留下一条「已注入」的假日志
            return
        s = item["shot"]
        fresh = r.get("tails") or {}
        disk = tailrelay.tails_of(s)   # 本轮新登记（旧登记已随 gen.clip 快照替换失效）
        tails = {}
        for asp in targets:
            src = fresh.get(asp) or disk.get(asp)
            if not src:   # 比例不齐不接力（与 disk_tails 同一条全称量词纪律）
                tails = None
                break
            tails[asp] = str(src)
        if tails:
            if _relay_recompile(nxt_item, tails):
                # 降级路线下身份完全由随发的身份图承载，而尾帧要占一席配额——
                # 注入后必须重验身份图仍在实发清单里。挤出去时承接让位：身份是
                # 该路线的立论前提，接缝只是镜间观感
                if nxt_item.get("route", "A") != "A":
                    try:
                        _gate_cast_anchor(project, nxt_item["shot"],
                                          nxt_item.get("base"),
                                          route=nxt_item["route"],
                                          ref_plan=nxt_item.get("ref_plan"))
                    except ProjectError:
                        if _relay_recompile(nxt_item, None):
                            _info(f"⚠ 镜 {nxt_item['shot']['id']}: 承接已撤销——"
                                  "尾帧占位会把出场角色的身份图挤出参考配额，"
                                  f"降级路线{nxt_item['route']}下身份图优先")
                        return
                _info(f"镜 {nxt_item['shot']['id']}: 已注入镜 {s['id']} 尾帧承接"
                      f"（{len(tails)} 比例）")
            return
        if not nxt_item.get("tail_refs"):
            return   # 计划期本就无承接，无需动作
        if _relay_recompile(nxt_item, None):
            _info(f"⚠ 镜 {nxt_item['shot']['id']}: 承接已撤销——镜 {s['id']} 本轮"
                  "重生成但未取得可用尾帧，计划期的旧版尾帧已随版本失效")

    def _apply(d: parallel.Done):
        """主线程：回填 + 记账 + 快照 + 审阅 + 落盘。**唯一改文档的地方。**"""
        item = d.meta
        s, prov = item["shot"], item["prov"]
        if not d.ok:
            mark(s, "failed")
            _unmark_wip(s, "clip", item)
            partial = getattr(d.error, "partial", None)
            if partial:
                # 多比例镜半途失败：已付费落盘的比例照常登记入账，重跑只补缺的比例
                s.setdefault("clips", {}).update(partial["clips"])
                if partial["cost"] > 0:
                    try:
                        project.add_cost("video", partial["cost"])
                    except KinemaError as e:
                        budget_stop["err"] = e
            project.save()
            # 审核拒与其他失败分流：人脸拒（建任务 400、不计费）有降级出路，
            # 输出拒（任务 failed）是内容判定、逐镜独立——两类都不该把镜 4..N
            # 的派活整批停掉；其余失败多为系统性故障，照旧失败即停
            from .providers.video.seedance import FACE_POLICY_CODE, OUTPUT_POLICY_CODE
            code = str(getattr(getattr(d, "error", None), "code", "") or "")
            (face_failed if code.startswith(FACE_POLICY_CODE)
             else output_failed if code.startswith(OUTPUT_POLICY_CODE)
             else failed).append(d)
            item["_failed"] = True    # 接力注入的死项守卫；进重发队列时复位
            _info(f"镜 {s['id']}: ✗ {d.message}")
            if code.startswith(FACE_POLICY_CODE) and item.get("ref_plan"):
                # 官方只报 content[N] 下标；@图片N = N（text 占 content[0]），
                # 经 RefPlan 翻成具体哪张图——「输入图审核未通过」和「角色身份图
                # 被拒」是两种完全不同的处置
                zh = {"frame": "分镜图", "scene_base": "场景基准图",
                      "board": "简笔板", "tail": "上镜尾帧",
                      "character": "角色身份图", "scene": "场景设定图",
                      "scene_main": "场景设定图", "scene_top": "场景俯视图",
                      "scene_top_main": "场景俯视图", "prop": "道具设定图"}
                # dubbed 的 content[] 是 [text, image, ref_audio, refs…]，V2V 是
                # [text, image, video, refs…]：第三项都不是图，它之后的图号映射
                # 回退一位；被拒的若正是它，无图可点名
                media_third = item["shot_v2v"] or (
                    not native and any(t.get("ra") for t in item["todo"]))
                for no in re.findall(r"content\[(\d+)\]", d.message or ""):
                    no_i = int(no)
                    if media_third:
                        if no_i == 2:
                            continue
                        if no_i > 2:
                            no_i -= 1
                    hit = item["ref_plan"].at(no_i)
                    if hit["kind"] != "unknown":
                        who = f"「{hit['name']}」" if hit["name"] else ""
                        _info(f"    ↳ 被拒的是 @图片{no_i}："
                              f"{zh.get(hit['kind'], hit['kind'])}{who}")
            return
        r = d.value or {}
        item["_done"] = True     # 接力注入的存活判据：请求已发出并回填，计划项封笔
        clips = s.setdefault("clips", {})
        clips.update(r["clips"])
        s["clip"] = clips.get(project.aspect) or next(iter(clips.values()), None)
        if r["clips"]:
            # 时间轴秒数 = 本轮买下的整秒，与 dry-run 报价共用 billable_seconds。
            # 厂商产物的容器时长恒比请求整秒多约一帧，按实测回填会让读侧变成
            # 4.1，再经 dubbed 的 ceil 进位买成 5s，每 retake 一次涨一秒。
            # `dubbed=not native` 与真发侧 `dubbed=bool(ref_audio)` 同值：
            # dubbed 恒附参考音（无词镜也写静音占位）。
            # 只在本轮真登记了片段时动——整章已在盘的空跑一个请求都不发
            s["dur"] = float(prov.billable_seconds(
                item["dur"], dubbed=not native, last_frame=item["flf2v"]))
        generated = r.get("generated") or r.get("salvaged")
        voided = None
        if generated:   # 生成参数快照 → 自动落「待审」（捡回的片段同样登记，cost 记 0）
            snap = {"prompt": item["vprompt"], "mode": mode, "provider": prov.name,
                    "envelope": item["envelope"].as_dict(),
                    "model": getattr(prov, "model", None),   # mini/2.5 双模型策略的复盘锚
                    # 实发档位随快照留痕：单价按档分级，而 --resolution 是运行时
                    # 覆盖、不落文档——不记就无从回答「这一版是按哪档买的」
                    "resolution": getattr(prov, "resolution", None),
                    "cost": round(r["cost"], 4),
                    "version": versioning.current_version(s, "clip")}
            if r.get("task_id"):
                snap["task_id"] = r["task_id"]
            if r.get("salvaged") and not r.get("generated"):
                snap["salvaged"] = True
            if item["ref_video_url"]:   # 留痕这一版跟的是哪一路运动源
                snap["reference_video"] = item["ref_video_url"]
                snap["reference_video_kind"] = item["ref_video_kind"]
                snap["camera_preset"] = s.get("camera_preset")
            if (item.get("ref_plan") and item["ref_plan"].board) or item["sk_board"]:
                # 同款留痕：这一版跟的是简笔分镜板（降级轮补的板不经 sk_board）
                snap["sketch_board"] = ((item.get("ref_plan").board
                                         if item.get("ref_plan") else None)
                                        or item["sk_board"])
            if item.get("route", "A") != "A":   # 降级路线留痕（复盘与审阅用）
                snap["face_route"] = item["route"]
            if item.get("tail_refs"):   # 同款留痕：这一版承接了上一镜尾帧
                snap["tail_relay_from"] = item.get("relay_src_id")
            if item.get("capture_tail"):
                fresh_tails = _persist_tails(s, r.get("tails") or {})
                if fresh_tails:
                    snap["tail_frames"] = fresh_tails
                elif not r.get("tails") and not relay_missed.get(prov.name):
                    # 回传落空要喊出来（配置洞不静默降级）：最可能是该型号/任务
                    # 类型不认 return_last_frame——整章接力都会落空，早知道早停
                    relay_missed[prov.name] = True
                    _info(f"⚠ {prov.name} 未回传尾帧（return_last_frame 可能不被"
                          "该型号/任务类型支持）——后续镜按无承接生成，"
                          "时间轴/板/设定图照发")
            s.setdefault("gen", {})["clip"] = snap
            # 这一版片段的提示词里写的是哪段台词（native 由模型念出、dubbed 由
            # ref_audio 对口型）——改台词后能认出旧片段与字幕已经不同源
            lineage.record_text(s, "clip")
            # 这一版片段的画面基准（路线 A=各比例分镜图，降级路线=场景基准图）与
            # 运动基准（控制段）：`lineage mark` 据此判定换图、重裁区间后片段过期，
            # 否则 gen-video 会对旧片段静默跳过。路径只从本计划项取——回调运行时
            # 计划循环早已结束，循环变量停在最后一镜上
            lineage.record_refs(s, "clip",
                                [t["img"] for t in item["todo"] if t.get("img")]
                                + ([item["ref_video_path"]]
                                   if item["ref_video_kind"] == "control" else []))
            # 新片段落地 → 旧一致性判定作废（判的是上一版片段的抽帧）。
            # 只作废计划期已看见的判定（cn_seen）——并发落盘的人工判定不抹
            voided = consistency_mod.invalidate(s, "clip") if item["cn_seen"] else None
            # 新片段按当前选角发的锚定音，旧音色的过期标记随之失效（同 stage_tts
            # 对 voice_stale 的处置）——不清的话卡片上那句「音色已更换」会一直挂着
            s.pop(voicebank.CLIP_STALE_KEY, None)
            s.pop(voicebank.CLIP_STALE_PREV_KEY, None)
            review.mark_generated(s, "clip")
            # 断点续跑三态回正：失败路径写 mark(failed)，成功路径必须对称回正——
            # 否则撞过 400 的镜重发成功后会永远挂着「失败」徽章
            mark(s, "done")
        else:
            _unmark_wip(s, "clip", item)   # 各比例都已在盘：wip 是误报，恢复原态
        # 记账放在登记之后（与 gen-image 同口径）：add_cost 超限先入账再抛，
        # 捕获后只置停派标志，在飞的结果照常收，整批跑完再抛
        if r.get("cost", 0) > 0:   # 单价未配置(=0)不入账，与 tts/music 同口径
            try:
                project.add_cost("video", r["cost"])
            except KinemaError as e:
                budget_stop["err"] = e
        project.save()
        # 衔接日志与**实际发出去的末帧**同源（sent_last）：只看链图返回值会出现
        # 「日志说衔接了、提示词写了过渡、请求里 last_frame=None」的三方不一致
        nxt, why = item["nxt"], item["why"]
        if item["shot_v2v"]:
            chain_note = f"  (参考视频→{item['ref_video_kind']} 运动迁移)"
        elif r.get("sent_last") and item["pz_last"]:
            chain_note = "  (末帧→previz 终态)"
        elif r.get("sent_last"):
            chain_note = f"  (末帧→镜{nxt['id']})"
        else:
            brk = _chain_break_note(nxt, why, sent_last=bool(r.get("sent_last")),
                                    v2v=item["shot_v2v"], ref_mode=item["ref_mode"],
                                    can_last=item.get("can_last", True))
            chain_note = f"  ({brk})" if brk else ""
        tail_sent = bool(item.get("tail_refs"))
        _info(_skip_note(s, prov.name, mode) if not generated else
              f"镜 {s['id']} [{prov.name}·{mode}]: ✓ {s.get('dur', '?')}s"
              + f"  v{versioning.current_version(s, 'clip'):03d} → 待审"
              + chain_note
              + ((f"  (全能参考："
                  + (f"路线{item['route']}·" if item.get("route", "A") != "A" else "")
                  + f"{_ref_note((item['ref_plan'].board if item.get('ref_plan') else item['sk_board']), item.get('ref_manifest'), tail_sent, item.get('dropped') or (), item.get('route', 'A'))}"
                  "·一镜一片)") if item["ref_mode"]
                 else ("  (首帧锚定：分镜图=第0帧·无参考图·镜间硬切)" if item.get("anchor")
                       # 参考媒体（dubbed）与全能参考同款取材：板与路线从 RefPlan
                       # 与计划项取——降级轮改写的是它们，取 sk_board/缺省路线
                       # 就是把 B 路的实发说成 A 路的组成
                       else ((f"  (参考图："
                              + (f"路线{item['route']}·"
                                 if item.get("route", "A") != "A" else "")
                              + f"{_ref_note((item['ref_plan'].board if item.get('ref_plan') else item['sk_board']), item.get('ref_manifest'), tail_sent, item.get('dropped') or (), item.get('route', 'A'))})")
                             if ((item.get("ref_plan") and item["ref_plan"].board)
                                 or item["sk_board"] or item["sheet_refs"] or tail_sent)
                             else ("  (简笔beats→时间轴)" if _sk_timeline(s) else ""))))
              + ("  (旧一致性判定已作废)" if voided else ""))
        if relay_on and generated:
            _relay_inject(item, r)

    # retries=0：视频单价是图片的几十倍，parallel 层的自动重试一次 = 为同一片段
    # 再付一次钱；HTTP 层的建任务重试（request_with_retry）与轮询容忍照常。
    # 失败即停派：与预算断闸同一个 should_stop——在飞的活跑完收结果（钱已花），
    # 新活不再发出。缺省 workers=1 时即严格串行（失败后一镜都不再发）。
    parallel.run([parallel.Task(key=f"shot:{x['shot']['id']}",
                                run=(lambda it=x: _work(it)),
                                label=f"镜 {x['shot']['id']}", meta=x) for x in plan],
                 workers=workers, retries=0, on_done=_apply,
                 should_stop=lambda: budget_stop["err"] is not None or bool(failed),
                 on_progress=parallel.progress_printer("图生视频"))
    # ── 降级轮：人脸拒的镜换参考装配重发一次（只一轮，二次被拒即死局）────
    # 主线程编排：生板（register_board + add_cost 都写文档）经三段式并发出板，计划重建
    # 在这里做完，工作线程照旧只发请求。人脸拒发生在建任务 HTTP 400、不计费——
    # 降级重发花的是新一次的视频费，与「同参数自动重试」有本质区别：输入变了。
    retry_items: list = []
    # 预算断闸或其他失败已停派时整轮不进：降级预处理会买板（付费），
    # 而重发不会发生——白花板钱还把镜从收尾清单里挪走
    # 第一遍：逐镜仲裁降级路线，收集要买板的镜；第二遍统一并发出板后再逐镜重建装配。
    # 板是图像 API 调用、互相独立，逐镜同步生板会让整轮空等
    pending: list[dict] = []
    for d in (list(face_failed)
              if not budget_stop["err"] and not failed else ()):
        item = d.meta
        s, prov = item["shot"], item["prov"]
        if item.get("route", "A") != "A":
            # 降级形态仍被拒：剩下的图全是安全的（场景/俯视/道具/板），唯一可能
            # 是身份图本身不受信——死局，交由收尾文案点名
            continue
        prof = item["prof"]
        identity = _identity_of(prof)
        base = _scene_base(s) if identity else None
        board = _fallback_board(s, prov, item["sk_board"])
        route2, why2 = _route_for(project, s, identity=identity,
                                  ref_task=_ref_task(prov, item["ref_mode"], _control_v2v(s)),
                                  board=board, scene_base=base, force=True,
                                  v2v=_control_v2v(s))
        if route2 == "A":
            _info(f"镜 {s['id']}: 不可降级（{why2}）——保持失败")
            continue
        appr = ((s.get("gen") or {}).get("clip_approval") or {})
        # 审阅锁在场且要新买板时不买：降级稿几乎必然与审阅版不一致、随后
        # 被跳过，板钱就白花了（闸在计费之前）。板已在盘则照常挂、由下方
        # 精确 sha 复检裁决
        need_board = route2 == "C" and not (appr.get("sha") and not board)
        pending.append({"d": d, "item": item, "prof": prof, "identity": identity,
                        "base": base, "board": board, "route2": route2, "why2": why2,
                        "need_board": need_board, "appr": appr})
    boards: dict = {}
    board_fail: dict = {}
    by_prof: dict = {}
    for pre in pending:
        if pre["need_board"]:
            by_prof.setdefault(pre["prof"], []).append(pre["item"]["shot"])
    for prof, need in by_prof.items():
        r = stage_sketch_boards(project, store, router, need, prof=prof)
        boards.update(r["boards"])
        board_fail.update({str(d.key): d.message for d in r["failed"]})
        if r["budget_err"]:   # 预算闸：已出的板已登记入账，降级轮就地中止
            budget_stop["err"] = r["budget_err"]
            _info("⚠ 降级生板触发预算闸——降级轮中止")
            break
    for pre in (pending if not budget_stop["err"] else ()):
        d, item = pre["d"], pre["item"]
        s, prov = item["shot"], item["prov"]
        identity, base, board = pre["identity"], pre["base"], pre["board"]
        route2, why2 = pre["route2"], pre["why2"]
        if pre["need_board"]:
            bp = boards.get(str(s["id"]))
            if bp:
                # 板到位即仲裁输入变化，路线与理由一并重取，不单独改写 route2
                board = bp
                route2, why2 = _route_for(
                    project, s, identity=identity,
                    ref_task=_ref_task(prov, item["ref_mode"], _control_v2v(s)),
                    board=board, scene_base=base, force=True, v2v=_control_v2v(s))
            else:
                _info(f"⚠ 镜 {s['id']} 降级生板失败（{board_fail.get(str(s['id']), '无拍')}）"
                      "——按无板路线C重发")
        appr = pre["appr"]
        rp2, dropped2 = _ref_plan(s, prov, item["ref_mode"],
                                  board=(board if route2 == "B" else None),
                                  tails=item.get("tail_refs") or None,
                                  route=route2, base=base)
        env2 = _video_envelope(s, prov, pre["prof"], item["profile_params"],
                               item["dur"], item["shot_plan"],
                               rp2.sheet_paths if rp2 else [],
                               rp2.manifest if rp2 else None,
                               tail_refs=item.get("tail_refs") or None,
                               voice_anchors=item.get("va_anchors"),
                               voice_refs=item.get("va_refs"),
                               route=route2, base_image=base,
                               board=(rp2.board if rp2 else None))
        if appr.get("sha") and _prompt_sha(env2.prompt) != appr["sha"]:
            _info(f"镜 {s['id']}: ⊘ 降级稿与审阅版不一致——本镜保持失败，"
                  "在 Studio「提示词 · PROMPTS」重新通过后重跑")
            continue
        try:
            _gate_cast_anchor(project, s, base, route=route2, ref_plan=rp2)
        except ProjectError as e:
            # 主计划路径的同名闸是「拦在计费之前」的合法硬停；这里钱已花完、
            # 其余镜等着收尾——单镜不可降级就保持失败，不打穿整个 stage
            _info(f"镜 {s['id']}: 不可降级——{e}")
            continue
        item.update({"route": route2, "base": base, "ref_plan": rp2,
                     "sheet_refs": rp2.sheet_paths if rp2 else [],
                     "ref_manifest": rp2.manifest if rp2 else None,
                     "dropped": dropped2, "vprompt": env2.prompt,
                     "envelope": env2})
        for t in item["todo"]:
            if not t.get("reuse"):
                t["img"] = base
        face_failed.remove(d)
        item["_failed"] = False       # 进入重发队列，接力注入恢复可达
        retry_items.append(item)
        _info(f"镜 {s['id']}: 降级路线{route2}（{why2}）→ 重发")
        nxt = plan[item["idx"] + 1] if item["idx"] + 1 < len(plan) else None
        if (nxt and nxt.get("_done") and nxt.get("tail_refs")
                and nxt.get("relay_src_id") == s.get("id")):
            # 首轮不停派的代价：下一镜已按本镜的旧版尾帧出片，本镜降级重做后
            # 接缝内容已换代——撤销无从谈起（片已出），只能点名
            _info(f"⚠ 镜 {nxt['shot']['id']} 已按镜 {s['id']} 的旧版尾帧出片——"
                  "本镜降级重做后接缝已过期，要对齐请重生下一镜")
    if retry_items and not budget_stop["err"]:
        _mark_wip(project, retry_items, "clip")
        parallel.run([parallel.Task(key=f"shot:{x['shot']['id']}:fallback",
                                    run=(lambda it=x: _work(it)),
                                    label=f"镜 {x['shot']['id']}·降级", meta=x)
                      for x in retry_items],
                     workers=1, retries=0, on_done=_apply,
                     should_stop=lambda: budget_stop["err"] is not None
                     or bool(failed),
                     on_progress=parallel.progress_printer("图生视频·降级轮"))

    # 断闸后未派活的镜还挂着 wip——那是误报（根本没生成过），统一恢复原态
    for it in plan:
        _unmark_wip(it["shot"], "clip", it)
    project.save()
    all_failed = failed + face_failed + output_failed
    if budget_stop["err"] and not all_failed:
        raise budget_stop["err"]
    if all_failed:
        raise KinemaError(
            f"{len(all_failed)} 镜图生视频失败："
            + "、".join(f"{d.label}（{d.message}）" for d in all_failed)
            + _retry_advice(all_failed)
            + (f"\n{budget_stop['err']}" if budget_stop["err"] else ""))
    # 口型精修（dubbed 章对白镜的增强步）：底片全部出齐后按最终配音重绘对白镜口型。
    # 内部自带三闸（模式/语态/在盘）与未配置点名跳过，此处不重判
    if not native and not no_lipsync:
        stage_lipsync(project, store, router, profile=profile, only=only)


# 首帧被判疑似真人时的收尾文案。判据与官方通道的完整版在
# `docs/kinema/seedance-face-policy.md`，这里只列现场可执行的动作。
_FACE_POLICY_ADVICE = (
    "\n  ⚠ 其中 {n} 镜输入图审核未通过（模型判输入图疑似真人）且未能自动降级"
    "（不可降级的原因已逐镜点名）。判据在模型侧的输入分类器，改参数绕不开——"
    "原样重跑与重出图再试都会被同样拒绝。\n"
    "     处置路线，按代价从低到高：\n"
    "     ① 写实档走受信身份图：project refs --only character:<名> --force 重出"
    "纯文生图身份图（sheet_origin=t2i），降级阶梯即自动可用；\n"
    "     ② 换无脸帧：人物戴不透光头盔/面罩，或改背身、远景、只拍手部"
    "（分类器判的是画面像不像真人照片，与图片来源无关）；\n"
    "     ③ 换低写实度画风：把 style_prompt 的媒介锚点从写实 CG 移到 2D 赛璐璐 /"
    "插画 / 风格化 3D，整章一次改到位。\n"
    "     判据与官方通道详见 docs/kinema/seedance-face-policy.md")

# 降级路线仍被拒 = 阶梯的死局：被拒图号已在失败回填时经 RefPlan 逐镜点名。
_FACE_DEADLOCK_ADVICE = (
    "\n  ⚠ 其中 {n} 镜在降级路线（场景图+板+身份图）下仍被拒，被拒的具体图号"
    "已逐镜点名。最常见的原因是角色身份图不是受信的纯文生图产物，修法：\n"
    "     python3 -m kinema project refs <项目id> --only character:<名> --force\n"
    "     重出受信身份图后重跑本命令。判据详见 docs/kinema/seedance-face-policy.md §2.2")

# 成片未过输出侧审核的收尾文案。审的是渲染出来的画面，与输入参考装配无关——
# 处置只有改内容一条路，绝不引导降级或同参数重跑。
_OUTPUT_POLICY_ADVICE = (
    "\n  ⚠ 其中 {n} 镜成片未过输出侧审核（成片渲染后被内容扫描拦下）。判的是"
    "生成结果，与输入参考图无关——降级换装配与同参数重跑都改变不了判定，"
    "处置是改内容后重生该镜：\n"
    "     · 排查该镜运动提示词与台词里可能落入政策类目的描述"
    "（近似真实公众人物、制服执法、未成年观感、敏感场景组合）；\n"
    "     · 写实档可重出身份图换一张长相（project refs --only character:<名> --force），"
    "避免生成结果与真实人物撞脸；\n"
    "     · 同题材连续被拒时改构图（背身/远景/剪影）再试。\n"
    "     该类失败是否计费以服务商账单为准；判据详见 docs/kinema/seedance-face-policy.md")


def _skip_note(s: dict, prov_name: str, mode: str) -> str:
    """盘上已有片段、本轮未发请求的镜的收尾行。

    报盘上那一版的实际路线（`gen.clip.face_route`），不报本轮计划的参考集——
    计划集按缺省路线 A 装配，降级出片的镜会被说成路线 A 的组成。"""
    fr = ((s.get("gen") or {}).get("clip") or {}).get("face_route")
    return (f"镜 {s['id']} [{prov_name}·{mode}]: 跳过 · 片段已在盘"
            + (f"（路线{fr} 出片）" if fr else ""))


def _retry_advice(failed) -> str:
    """失败清单 → 收尾口径。按错误码分流，不对错误文案做子串匹配。

    四类失败的处置各不同：通用失败多为瞬时故障，重跑可跳过已成功的镜；人脸拒
    且未降级要按阶梯口径处置；降级后仍被拒是死局，只有重出身份图一条修法；
    输出拒是内容判定，只有改内容一条路。"""
    from .providers.video.seedance import FACE_POLICY_CODE, OUTPUT_POLICY_CODE

    def _code(d):
        return str(getattr(getattr(d, "error", None), "code", "") or "")

    face = [d for d in failed if _code(d).startswith(FACE_POLICY_CODE)]
    out_rej = [d for d in failed if _code(d).startswith(OUTPUT_POLICY_CODE)]
    deadlock = [d for d in face if (getattr(d, "meta", None) or {})
                .get("route", "A") != "A"]
    plain = [d for d in face if d not in deadlock]
    tail = ("\n  已成功的镜已登记落盘，重跑同一条命令会自动跳过它们"
            "（错误信息里带任务号的，可凭它到服务商控制台找回产物；"
            "建任务阶段就被拒的没有任务号，也没有产物）。")
    out = ""
    if deadlock:
        out += _FACE_DEADLOCK_ADVICE.format(n=len(deadlock))
    if plain:
        out += _FACE_POLICY_ADVICE.format(n=len(plain))
    if out_rej:
        out += _OUTPUT_POLICY_ADVICE.format(n=len(out_rej))
    if len(face) + len(out_rej) != len(failed):
        out += tail
    return out


def _voice_ref_dir(project):
    """音色锚定参考音的存放目录（真源 `voicecast.voice_ref_dir`，此处恒建目录）。"""
    return voicecast.voice_ref_dir(project)


def stage_lipsync(project, store, router, *, profile=None, force=False,
                  only=None):
    """dubbed 章对白镜的口型精修（gen-video 收尾缺省接线的增强步）：底片 + 最终配音 → 视频改口型，只重绘口型区域。

    范围三闸：仅 dubbed（native 口型与发声同源；kenburns 无动态人像）；仅
    `voice_kind == dialogue` 的镜（旁白/静音镜按闭唇出片，无口型可修）；底片与
    该镜 wav 都在盘。产物 `shot_<id>_<tag>_lips.mp4` 是派生物、不进版本栈——
    底片（`clips_base`）与 wav 在盘即可随时重算。换音色的工作流因此是
    `tts --force → lipsync → assemble`，Seedance 底片零重生。

    幂等按源指纹：lips 文件比底片与 wav 都新即跳过（--force 推平）。
    provider 未配置（req_key/视觉密钥）或媒体上云未启用时点名跳过——
    增强步不拦出片主链，底片按闭口型出片。"""
    if project.motion != "dubbed":
        _info("lipsync 只作用于 dubbed（native 口型与发声同源，无需精修）——跳过")
        return
    prov, _lp = router.resolve("lipsync", profile or project.profile)
    ok, why = prov.configured()
    if not ok:
        _info(f"⚠ 口型精修跳过：{why}")
        # 跳过不等于没事：开口平移到窗口边界仍差一截、或底片没把整句演完的镜，
        # 出片就带口型残差——逐镜点名，别让人看片才发现
        _adir = project.subdir("audio")
        for s in project.active_shots:
            wavp = voicecast.shot_audio_path(s, _adir)
            note = voicecast.dubbed_sync_note(voicecast.dubbed_sync_report(
                s, wavp, s.get("clip"), float(s.get("dur") or 0)))
            if note:
                _info(f"   镜 {s['id']} 将带口型残差出片：{note}")
        return
    from .storage.media import ensure_local, get_media_store, is_url
    ms = None
    if not router.force_mock:
        ms = get_media_store(_ws_root_of(project))
        # 同参考视频：视觉服务只收公网 URL，判据是能力齐备而不是默认档
        if not ms.configured:
            _info("⚠ 口型精修跳过：OSS 未配置（视觉服务只收公网 URL 的视频与音频）——"
                  "config/storage.yaml 的 media 段填好 bucket/region 与密钥后重跑 "
                  "lipsync；backend 不必改成 oss")
            return
    adir = project.subdir("audio")
    targets = project.aspects if project.image_per_aspect else [project.aspect]
    want = ({x.strip() for x in str(only).split(",") if x.strip()}
            if only else None)
    jobs: list[dict] = []
    for s in project.active_shots:
        if transitions_mod.is_transition(s):
            continue
        if want and str(s.get("id")) not in want:
            continue
        if voicecast.voice_kind(s) != "dialogue":
            continue
        wav = adir / f"shot_{s['id']}.wav"
        if not has_file(wav):
            _info(f"镜 {s['id']}: 缺配音 wav，口型精修跳过（先 tts）")
            continue
        for asp in targets:
            base = ((s.get("clips_base") or {}).get(asp)
                    or (s.get("clips") or {}).get(asp))
            base_l = ensure_local(base) if base else None
            if not (base_l and Path(base_l).is_file()):
                continue
            out = (project.subdir("gen_clips")
                   / f"shot_{s['id']}_{aspect_tag(asp)}_lips.mp4")
            if (not force and out.is_file()
                    and out.stat().st_mtime >= Path(base_l).stat().st_mtime
                    and out.stat().st_mtime >= wav.stat().st_mtime):
                continue
            jobs.append({"s": s, "asp": asp, "base": str(base_l),
                         "wav": str(wav), "out": out})
    if not jobs:
        _info("口型精修：对白镜的口型已是最新，无事可做")
        return
    _step(f"口型精修 [{prov.name}] · {len(jobs)} 件"
          "（仅对白镜——旁白/静音镜按闭唇出片，无口型可修）")
    total = 0.0
    for j in jobs:
        s, asp = j["s"], j["asp"]
        try:
            dur = probe_duration(j["base"])
        except Exception:  # noqa: BLE001  底片读不出时长交给服务端口径，不在此拦
            dur = 0.0
        if router.force_mock:
            vurl, aurl = j["base"], j["wav"]
        else:
            vurl = j["base"] if is_url(j["base"]) else ms.upload(j["base"])
            aurl = ms.upload(j["wav"])
        try:
            res = prov.generate(vurl, aurl, str(j["out"]), dur=dur)
        except (ProviderError, ConfigError) as e:
            _info(f"镜 {s['id']} {asp}: ✗ 口型精修失败（{e}）——保留底片出片")
            continue
        # 底片指针只在首次精修时登记：此后 clips 指向 lips、clips_base 指向
        # Seedance 原片，重算恒以原片为源（对 lips 再改口型会逐代劣化）
        s.setdefault("clips_base", {}).setdefault(
            asp, (s.get("clips") or {}).get(asp))
        s.setdefault("clips", {})[asp] = res.path
        if asp == project.aspect:
            s["clip"] = res.path
        s.setdefault("gen", {}).setdefault("lipsync", {})[aspect_tag(asp)] = {
            "provider": prov.name, "cost": round(res.cost, 4)}
        try:
            if res.cost > 0:
                project.add_cost("lipsync", res.cost)
                total += res.cost
        finally:
            project.save()
        _info(f"镜 {s['id']} {asp}: ✓ 口型已按最终配音重绘（{dur:.1f}s）")
    if total:
        _info(f"口型精修费用 ≈ ¥{round(total, 2)}")


def _motion_default_label(project) -> str:
    """未表态章节按内容推出的档位及其依据（`project.default_motion` 的播报口径）。"""
    if project.scored_audio:
        return "native（audio_mode=scored 的人声整轨生成，与对口型互斥）"
    if project.motion == "native":
        return "native（章内有对白镜：模型自声，口型与音色同源）"
    return "dubbed（全旁白章：固定音色旁白烧录、闭唇出片）"


def _settle_motion(project) -> str:
    """真发前把渲染档写进未表态的章节。读侧 `Project.motion` 对未表态章节已按内容
    推出档位，这里只负责持久化：`-m/--native` 一类运行时覆盖一并升格为正式值，
    否则 `save()` 会把它还原，后续不带 flag 的 assemble/verify 会按另一个档位出片。
    已表态章节原样返回。"""
    mode = project.motion
    if not project.motion_declared:
        project.data["motion"] = mode
        project.commit_runtime("motion")
        project.save()
    return mode


def _announce_motion(project, *, persist: bool) -> None:
    """未表态章节的档位播报；运行时覆盖已由 flag 表态，不再播缺省。"""
    if project.runtime_overridden("motion"):
        if persist:
            _info(f"▷ 本次 -m {project.motion} 已写入章节 motion（章节此前未表态）")
        return
    _info(f"▷ 本章未指定渲染模式，按缺省 {_motion_default_label(project)} 执行"
          + ("，已写入章节 motion" if persist else "（本次口径，不写章节）")
          + "；静图样片请显式写 motion: kenburns")


def stage_tts(project, store, router, *, profile=None, force=False,
              concurrency=None, only=None, fit_dur=False):
    from .storage.media import is_url
    prov, params = router.resolve("tts", profile or project.profile)
    _cast_gate(project, router)
    adir = project.subdir("audio")
    # 音色解析优先级：镜.voice > 角色音色表[镜.speaker] > 旁白锁 > profile 默认 > 项目 voice_id
    default_ref = voicecast.default_voice_ref(project.data, params)
    vmap = project.voices
    # 语速：project 顶层 speech_rate（-50~100，0=原速，越大越快）或 profile 默认
    rate = project.data.get("speech_rate", params.get("speech_rate"))
    extra = {"speech_rate": rate} if rate is not None else {}
    # 音色锚定（默认开，仅 seed-audio 这类生成式）：每个音色先生成一段参考音频，之后该音色
    # 所有句子都用它合成 → 锁死同一把声音，根治 seed-audio 逐句音色漂移。
    # 参考音频缓存到项目级 assets/voices，全系列共用。关掉：project 顶层 voice_lock: false。
    use_anchor = bool(project.data.get("voice_lock", True)) \
        and getattr(prov, "supports_voice_anchor", False)
    anchor_dir = _voice_ref_dir(project) if use_anchor else None
    _anchors: dict = {}
    _anchor_want: dict = {}       # 计划循环收集（dict 保序去重），随后一次并发预热

    _step(f"配音 [{prov.name}] · 逐镜合成并回填时长"
          + (f" · 角色音色表 {len(vmap)} 项" if vmap else "")
          + (" · 音色锚定" if use_anchor else "")
          + (f" · 语速{rate:+d}" if rate else ""))

    # 停顿门控：`delivery.pause_*` 只在本地渲染模式（kenburns）折进 dur / 进旁白轨
    # ——dubbed/native 下 gen-video 按 dur 向 Seedance 计费而 ref_audio 里没有停顿，
    # 折算=无效购买无声空转
    motion = project.motion
    if not project.motion_declared and not project.runtime_overridden("motion"):
        _info(f"▷ 本章未指定渲染模式，配音按缺省 {motion} 口径回填时长"
              "（不写章节；正式表态在 gen-video 真发）")
    if motion != "kenburns" \
            and any(any(voicecast.declared_pauses(s)) for s in project.shots):
        _info(f"⚠ 本模式（{motion}）下停顿不生效：delivery.pause_before/after 一律不折进 dur、"
              f"不进旁白轨——Seedance 按 dur 计费而对口型音频里没有这段无声。"
              f"要停顿请用 kenburns，或把停顿写进分镜节奏本身")
    # 无台词镜上的 pause_* 是空操作——那一镜整段本来就是 dur 秒静音，再插停顿
    # 要么没意义、要么等于把同一段静音数两遍。**但不能静默丢弃**：作者写了却毫无
    # 效果，须明确提示该字段在此形态下不生效。
    if motion == "kenburns":
        noop_pause = [s.get("id") for s in project.shots
                      if not voicecast.shot_text(s)
                      and any(voicecast.declared_pauses(s))]
        if noop_pause:
            _info(f"⚠ 镜 {'/'.join(str(i) for i in noop_pause)} 是无台词的纯画面镜，"
                  f"其 delivery.pause_* 不生效——该镜整段本就是 dur 秒静音，"
                  f"要加长留白请直接调这几镜的 dur")
    # ── 并发三段式（铁律见 kinema/parallel.py）──────────────────────────
    # ① 主线程定计划：状态机闸门、版本归档、音色解析、**音色锚定预热**全在这里
    #    （锚定要发 API 且写共享缓存字典，放进工作线程会重复合成同一段参考音）；
    # ② 并发只合成：一镜一件活，只写自己的 wav，顺带把 probe 也并行了；
    # ③ 主线程按提交顺序回填：dur / 版本 / 审阅 / 落盘全单线程。
    muted_instr = []          # 写了语音指令/表演提示的镜（模版生成不消费，收尾统一提示）
    total_cost = 0.0
    plan: list[dict] = []
    # --only 定向镜号：只筛合成对象，用于补跑/重跑部分旁白镜。收尾的旁白轨
    # 拼接仍看全片：任何模式下进旁白轨却缺逐镜 wav 的台词镜都会让收尾点名
    # 拒拼（narration_parts 单一真源，绝不写出缺镜的短轨覆盖好轨）
    want_only = {x.strip() for x in str(only).split(",") if x.strip()} if only else None
    for s in project.shots:
        if want_only is not None and str(s.get("id")) not in want_only:
            continue
        if review.is_omitted(s):
            _info(f"镜 {s['id']}: 已弃用(omt)，跳过")
            continue
        # 台词判据走 shot_text（同时认识 shots[].lines[] 与老的 narration），
        # 句序列由 voicecast.shot_lines 单一真源给出——多角色镜逐句各自一把声音
        lines = voicecast.shot_lines(s)
        text = voicecast.shot_text(s)
        wav = adir / f"shot_{s['id']}.wav"
        if not text:
            gap = float(s.get("dur") or 0)
            if gap > 0:
                _info(f"镜 {s['id']}: 无旁白 → 插入 {gap:.2f}s 静音占位（保持后续音画对位）")
            else:
                _info(f"镜 {s['id']}: 无旁白且无时长，跳过")
            continue
        if not voicecast.in_narration_track(s, motion):
            # 声源按镜分治：native 的对白由模型原生发声（锚定附发），旁白轨
            # 只收旁白镜——这里合成出来的 wav 既不烧录也没有别的消费方
            _info(f"镜 {s['id']}: 对白镜由模型原生发声，旁白轨不收，跳过合成")
            continue
        if wav.is_file():
            s.setdefault("audio_file", str(wav))    # 老数据补登画布路径（版本栈定位用）
        spk = s.get("speaker")
        # 音色解析链（哪把声音计费合成）下沉在 voicecast.py，独立可测。
        # 多角色镜**逐句**解析：句没写 speaker/voice 时 shot_lines 已把镜级值继承下来，
        # 所以单段镜的结果与 resolve_shot_voice 逐字节一致（回落态零行为变化）
        voice_ref, voice_type = voicecast.resolve_shot_voice(project, store, s, default_ref)
        cast = [voicecast.resolve_line_voice(project, store, s, ln, default_ref)
                for ln in lines]
        # 状态机：通过=锁定不重合成（仍进旁白拼接）；重做=视同 force；覆盖前归档进版本栈。
        # 盘上 wav 念的是哪把声音记在 gen.audio；说话人、句级 voice 或音色表改了，
        # 解析出的音色与记录不同即重合成——文件在盘不等于对得上
        locked = review.is_locked(s, "audio") and wav.is_file()
        retake = review.needs_retake(s, "audio")
        rec_types = voicebank.recorded_voice_types(s)
        voice_changed = (bool(rec_types) and all(rec_types) and wav.is_file()
                         and rec_types != [t for _r, t in cast])
        regen = (force or retake or voice_changed) and not locked
        if voice_changed and not locked and not (force or retake):
            _info(f"镜 {s['id']}: 音色已变（{'/'.join(rec_types)} → "
                  f"{'/'.join(str(t) for _r, t in cast)}），重合成")
        if regen and wav.is_file():
            note = review.get_note(s, "audio")
            v = versioning.archive(project, s, "audio",
                                   reason=(f"retake: {note}" if retake and note else
                                           ("retake" if retake else
                                            "force" if force else "voice changed")),
                                   params=(s.get("gen") or {}).get("audio"))
            if v:
                _info(f"镜 {s['id']}: audio 旧版已归档 v{v:03d}")
        need = regen or not wav.is_file()
        # 逐句合成计划：单段镜**直接写整镜 wav**（不落中间文件、不走拼接——
        # 单句多一次拼接就多一次无谓转码）；多段镜逐句落 shot_<id>_L<k>.wav，
        # 收尾拼成同一个整镜 wav——
        # 对外产物永远只有 shot_<id>.wav，下游（review/版本栈/dubbed 的 ref_audio/
        # request_seconds）一行都不用改。
        # 音色锚定只在计划期收集（此处不发 API）：去重后统一并发预热，见下
        multi = len(lines) > 1
        segs = []
        for ln, (lref, ltype) in zip(lines, cast):
            lcast = voicebank.cast_for_type(project.data, ltype)
            lclip = (lcast or {}).get("clip")
            # clip 可能已被 oss sync 改写成 URL（适配器参考音条目 URL 感知），
            # 只有本地路径才验在盘
            if lclip and not is_url(lclip) and not Path(lclip).is_file():
                raise KinemaError(
                    f"镜 {s['id']} 的定制音色 {ltype} 找不到锚定参考音: {lclip}\n"
                    f"  重新试听并锁定：voice custom <项目> ... → "
                    f"voice use <项目> ... --custom --no <编号>")
            if lcast and not lclip:
                # 定制档案没有参考音=档案已残（立档即拷贝不可变音频，正常不会缺）
                # ——绝不静默退回官方 speaker（那等于换了一把声音且全程无提示）
                raise KinemaError(
                    f"镜 {s['id']} 的定制音色 {ltype} 档案缺参考音——"
                    "重新 voice custom 试音并 voice use --custom 锁定")
            if need and use_anchor and ltype and not lclip:
                _anchor_want[ltype] = None
            segs.append({
                "line": ln,
                "wav": voicecast.line_wav(s, ln, adir) if multi else wav,
                "voice_ref": lref, "voice_type": ltype,
                # 定制音色的参考音在锁定那一刻就定了，直接用；官方音色留空待预热回填。
                # cast 整条随行：合成时要用声线描述文案（prompt）拼剧本体正文
                "clip": lclip, "custom": bool(lclip), "cast": lcast,
            })
        # 表演提示的消费分工：定制生成把它编进剧本正文（见 _synth），官方模版
        # 生成不消费（标准版静默过滤）——只有存在模版句时才提示「没生效」
        if need and voicecast.delivery_instruction(s) \
                and not all(sg["custom"] for sg in segs):
            muted_instr.append(s["id"])
        plan.append({"shot": s, "wav": wav, "text": text, "need": need, "clip": None,
                     "voice_ref": voice_ref, "voice_type": voice_type,
                     "spk": spk, "locked": locked, "segs": segs, "multi": multi,
                     "lines": lines})

    # 音色锚定预热：**去重后并发**——每把声音只合成一次参考音（去重集合天然满足
    # 「同一音色只合成一次」的约束）；在计划循环里逐音色串行合成的话，多角色
    # 项目 N 把声音就是 N 次串行往返、全部排在主并发开始之前白等。
    # 单个音色预热失败不阻断：该音色退回无锚合成，锚定标记不亮。
    want = list(_anchor_want)
    if want:
        def _preheat(vt):
            # 路径与文案都取 voicecast 真源：命名/台词各抄一份，_score_anchor
            # 与页面试听就会与预热产物对不上
            ref = voicecast.anchor_ref_path(anchor_dir, vt)
            if ref.is_file():
                return str(ref), 0.0
            res = prov.synthesize(voicecast.ANCHOR_TEXT, str(ref), voice=vt)
            return str(ref), float(getattr(res, "cost", 0.0) or 0.0)
        for d in parallel.run(
                [parallel.Task(key=f"voice:{v}", run=(lambda vt=v: _preheat(vt)),
                               label=f"音色 {v}", meta={"vt": v}) for v in want],
                workers=min(len(want), parallel.resolve_workers(concurrency))):
            _anchors[d.meta["vt"]] = d.value[0] if d.ok else None
            if d.ok:
                total_cost += d.value[1]    # 预热是一次真实合成，随本章配音一起入账
        for it in plan:
            if not it["need"]:
                continue
            for sg in it["segs"]:
                if not sg["custom"]:
                    sg["clip"] = _anchors.get(sg["voice_type"])
            it["clip"] = it["segs"][0]["clip"] if it["segs"] else None

    # 定制音色的 provider 懒解析：全章没有定制实体时一次都不解析，
    # 免得只用模版生成的项目也被要求配齐 seed-audio 那条链路
    _custom_cache: list = []

    def _custom_prov():
        if not _custom_cache:
            _custom_cache.append(router.resolve_named("tts", voicebank.CUSTOM_PROVIDER))
        return _custom_cache[0]

    # Studio 忙态：只有真要合成的镜进「生成中」（locked/已有 wav 的镜只是重登记）
    _mark_wip(project, [x for x in plan if x["need"]], "audio")

    def _synth(seg, out):
        """合成**一句**到 seg["wav"]（多角色镜的每一句各自一把声音）。"""
        ln = seg["line"]
        # 逐句表现力：emotion 恒发（官方多情感音色带情绪的正道）。归一化的「句」与
        # 「镜」同形，故这里吃的就是 shot_expressive_params 那一份实现
        extra_line = {**extra, **voicecast.shot_expressive_params(ln)}
        # 定制音色只有 seed-audio-1.0 吃得下参考音，逐句点名它；其余实体照旧走
        # 章节 profile 解析出的 provider——同一章里两条路并存是常态
        sprov = _custom_prov() if seg["custom"] else prov
        if seg["custom"]:
            # 定制音色的一致性组合是**声线描述文案 + 参考音**两道锚同发：
            # 参考音（档案那条不可变音频）锁音色本身，描述原话钉气质/语速/口癖
            # ——只发参考音，长句与极端情绪下气质仍会走样。emotion 与表演提示
            # （delivery_instruction）对生成式模型走剧本正文，不走结构化参数
            # （那条通道是官方模版音色的，seed-audio 的请求体没有它）
            body = voicebank.line_prompt(
                seg["cast"], ln["text"],
                instruction=voicecast.delivery_instruction(ln),
                emotion=str(ln.get("emotion") or "").strip() or None)
            res = sprov.synthesize(body, str(seg["wav"]), ref_audio=seg["clip"],
                                   **extra_line)
        elif seg["clip"]:   # 官方音色锚定：用预热参考音合成（互斥于 speaker）
            res = sprov.synthesize(ln["text"], str(seg["wav"]),
                                   ref_audio=seg["clip"], **extra_line)
        else:
            res = sprov.synthesize(ln["text"], str(seg["wav"]),
                                   voice=seg["voice_type"], **extra_line)
        # 定制路的台词带句尾保护词，按 provider 的词级时间戳裁掉
        to_pcm(seg["wav"], end=voicebank.guard_cut(res.segments) if seg["custom"] else None)
        out["cost"] += res.cost
        out["synthesized"] = True

    def _work(item):
        """工作线程：逐句合成 → 拼成整镜 wav → 就地 probe 时长。**一行文档都不碰。**"""
        s, wav = item["shot"], item["wav"]
        out = {"cost": 0.0, "synthesized": False}
        if item["need"]:
            for seg in item["segs"]:
                _synth(seg, out)
            if item["multi"]:
                # 逐句 wav → 整镜 wav。句间停顿与镜级同一道模式门控（见 line_pauses）：
                # 只有本地渲染模式插得起，dubbed/native 插了就是按秒买无声
                parts: list[tuple[str, object]] = []
                for seg in item["segs"]:
                    pb, pa = voicecast.line_pauses(seg["line"], project.motion)
                    if pb > 0:
                        parts.append(("silence", pb))
                    parts.append(("file", str(seg["wav"])))
                    if pa > 0:
                        parts.append(("silence", pa))
                concat_audio(parts, wav, tail_fade=voicecast.TAIL_FADE)
        # 时长回填 = 配音实测 + 生效停顿（仅 kenburns）。**从 probe 重算而非累加**——
        # 每跑一次 tts 都会执行，累加即单调发散。probe 只读自己刚写的文件，可并行。
        # 多段镜 probe 的是拼好的整镜 wav（已含句间停顿），口径与单段镜完全一致
        out["speech"] = probe_duration(wav)
        # 逐句实测时长（窗口口径：句间停顿算进本句）——字幕逐句切换的时间来源
        if item["multi"]:
            spans = []
            for seg in item["segs"]:
                pb, pa = voicecast.line_pauses(seg["line"], project.motion)
                spans.append(round(probe_duration(seg["wav"]) + pb + pa, 3))
            out["line_durs"] = spans
        return out

    failed: list = []

    def _apply(d: parallel.Done):
        """主线程：回填 dur / 版本 / 审阅 / 落盘。**唯一改文档的地方。**"""
        nonlocal total_cost
        item = d.meta
        s = item["shot"]
        if not d.ok:
            _unmark_wip(s, "audio", item)
            project.save()
            failed.append(d)
            _info(f"镜 {s['id']}: ✗ {d.message}")
            return
        r = d.value or {}
        total_cost += r.get("cost", 0.0)
        s["audio_file"] = str(item["wav"])
        speech = r.get("speech", 0.0)
        pb, pa = voicecast.shot_pauses(s, motion)
        if motion != "native" and not (s.get("clip") or s.get("clips")):
            # dur 的真源随阶段移交：片段一旦在盘（URL 形态同算已产出），dur 已由
            # gen-video 按买下的整秒回填，配音只决定各窗口内的落点——此时按配音实测
            # 覆写会让 assemble 把每镜视频尾部裁掉（换音色 tts --force 的正路是
            # 底片零重生，时间轴必须原样保住）。native 恒不覆写：dur 由画面主导，
            # 配音只是混烧的叠加轨。dubbed 只延不缩：dur 是场→镜设计出的表演窗口
            # （台词只占窗口一段），配音超窗才把窗口撑大；kenburns 双向跟随配音
            # （静图放多久由配音+停顿说了算）
            new_dur = voicecast.shot_duration(s, speech, motion)
            if motion == "dubbed":
                s["dur"] = max(float(s.get("dur") or 0), new_dur)
            else:
                s["dur"] = new_dur
        # 逐句实测时长回填 lines[].dur（[engine-managed]）——字幕逐句切换的时间来源。
        # 只在真合成过时写：locked/跳过的镜保留上一轮的实测值，别用空值抹掉它
        durs = r.get("line_durs")
        if durs and isinstance(s.get("lines"), list):
            # 回填按**原始 lines 下标**对齐：shot_lines 会丢弃空文本段与非 dict 项，
            # 两边下标会错位——「有台词」的判据必须用同一份 voicecast.line_text
            #（它认 text 与 narration 两种键，自写过滤只认 text 就会 zip 截断错位）
            kept = [i for i, d0 in enumerate(s["lines"])
                    if isinstance(d0, dict) and voicecast.line_text(d0)]
            if len(kept) != len(durs):
                raise KinemaError(
                    f"镜 {s['id']}: 逐句时长 {len(durs)} 段与有台词句数 {len(kept)} 不一致"
                    "——dur 回填与 shot_lines 的过滤判据出现分叉，拒绝错位写入")
            for idx, span in zip(kept, durs):
                s["lines"][idx]["dur"] = span
        if r.get("synthesized"):   # 生成参数快照 → 自动落「待审」
            s.setdefault("gen", {})["audio"] = {
                "voice": item["voice_ref"], "voice_type": item["voice_type"],
                "provider": (_custom_prov().name if voicebank.is_custom(item["voice_type"])
                             else prov.name),
                "cost": round(r["cost"], 4),
                "version": versioning.current_version(s, "audio")}
            # 这条音轨念的是哪段台词——改台词后能认出旧配音已经对不上
            lineage.record_text(s, "audio")
            if item["multi"]:      # 多角色镜：留痕逐句用了哪把声音（排查「谁在说话」）
                s["gen"]["audio"]["cast"] = [
                    {"speaker": sg["line"].get("speaker"), "voice": sg["voice_ref"],
                     "voice_type": sg["voice_type"]} for sg in item["segs"]]
            # 新音轨落地=旧音色的过期标记失效（不清的话卡片上那句「音色已更换」
            # 会一直挂着，而它说的那件事已经不成立了）。`voice_stale_prev` 是
            # 换音色时暂存的原表态，重出之后同样没有可还原的东西
            s.pop("voice_stale", None)
            s.pop("voice_stale_prev", None)
            review.mark_generated(s, "audio")
        else:
            _unmark_wip(s, "audio", item)   # 复用/跳过：wip 是误报，恢复原态
        project.save()
        who = (item["spk"] or "旁白") if not item["multi"] else (
            "·".join(dict.fromkeys(
                str(sg["line"].get("speaker") or "旁白") for sg in item["segs"])))
        voi = (item["voice_ref"] or "默认") if not item["multi"] else (
            f"{len({sg['voice_type'] for sg in item['segs']})} 把声音")
        _info(f"镜 {s['id']} [{who}→{voi}"
              + ("·锚定" if _anchors.get(item["voice_type"]) else "")
              + ("·锁定" if item["locked"] else "")
              + (f"·{len(item['segs'])} 句" if item["multi"] else "")
              + f"]: {s['dur']:.2f}s"
              + (f"（配音 {speech:.2f}s + 停顿 {pb:.2f}/{pa:.2f}s）" if (pb or pa) else "")
              + (f"  v{versioning.current_version(s, 'audio'):03d} → 待审"
                 if r.get("synthesized") else ""))

    workers = parallel.resolve_workers(concurrency)
    if plan and workers > 1 and any(x["need"] for x in plan):
        _info(f"并发配音 · {workers} 句同时")
    parallel.run([parallel.Task(key=f"shot:{x['shot']['id']}",
                                run=(lambda it=x: _work(it)),
                                label=f"镜 {x['shot']['id']}", meta=x) for x in plan],
                 workers=workers, on_done=_apply,
                 on_progress=parallel.progress_printer("配音"))
    # 未派活/未生成的镜若还挂着 wip，统一恢复原态（wip 只反映真在跑的时段）
    for it in plan:
        _unmark_wip(it["shot"], "audio", it)
    # 费用已经发生，下面两条早退（失败镜、缺镜拒拼旁白轨）不能带着它走：
    # 台账少记会让事前与事后两道额度闸按偏低的已花额放行。
    # 落盘在 finally——超限时本笔已入账，不落盘等于没记。
    try:
        if total_cost > 0:
            project.add_cost("tts", total_cost)
    finally:
        project.save()
    if failed:
        raise KinemaError(
            f"{len(failed)} 镜配音失败："
            + "、".join(f"{d.label}（{d.message}）" for d in failed)
            + "\n  已成功的镜已登记落盘，重跑同一条命令会自动跳过它们。")
    if total_cost <= 0 and prov.name != "mock" and any(x["need"] for x in plan):
        _info("本批配音未入账：provider 未配置单价（不等于免费）——"
              "config/models.yaml 给该 provider 填 price_per_second 或 price_per_kchar")

    if muted_instr:
        _info(f"⚠ {len(muted_instr)} 镜写了语音指令/delivery.note（镜 "
              f"{','.join(str(i) for i in muted_instr[:8])}）——"
              f"模版生成走官方固定音色，标准版会静默过滤这条通道，"
              f"本次未下发；要表现力请用 emotion/emotion_scale")

    # --fit-dur：**让画面等台词**（用户诉求「5s 的镜台词要念 10s，就该自动改时间」）。
    # 只在 native/dubbed 有意义——kenburns 的 dur 本来就等于配音+停顿。
    # 两条纪律：① **已有 clip 的镜绝不动**（画面已生成、钱已付，改 dur 只会让片段与
    # 时间轴对不上）；② 只放宽不收窄（配音短于窗口是合法留白，画面继续演）。
    if fit_dur and project.uses_seedance:
        grown, locked_clip = [], []
        for s in project.shots:
            if review.is_omitted(s) or not voicecast.shot_text(s):
                continue
            wav = voicecast.shot_audio_path(s, adir)
            if wav is None:
                continue
            speech = probe_duration(wav)
            cur = float(s.get("dur") or 0)
            if speech <= cur + 0.05:
                continue
            if s.get("clip"):
                locked_clip.append(s["id"])
                continue
            s["dur"] = round(speech, 2)
            grown.append((s["id"], cur, s["dur"]))
        if grown:
            project.save()
            _info(f"⏱ --fit-dur: {len(grown)} 镜画面时长已放宽到配音实测——"
                  + "、".join(f"镜{i}({a:g}→{b:g}s)" for i, a, b in grown[:6])
                  + ("…" if len(grown) > 6 else ""))
            _info("   ⚠ 这几镜的 Seedance 请求秒数随之变长（还没烧的镜下次按新时长计费）；"
                  "已烧过的镜若要跟上须 review set --stage clip --state retake 后重烧")
        if locked_clip:
            _info(f"ⓘ 镜 {'/'.join(str(i) for i in locked_clip)} 已有视频片段，"
                  f"dur 由已买下的画面秒数锁定不放宽——配音仍按窗口压缩贴合；"
                  f"要让画面等台词请先打回该镜 clip 再重烧")
        if not grown and not locked_clip:
            _info("ⓘ --fit-dur: 没有配音超出画面窗口的镜，时长无需调整")

    # 旁白轨拼接序列 = voicecast.narration_parts 单一真源（compose 自愈重拼共用同一条）
    parts, segments, missing = voicecast.narration_parts(project, adir)
    if missing:
        # 缺镜时拒绝重拼：缺一镜的短轨会**覆盖**原本完整的 narration.wav，而这些
        # 镜可能早已过审（review 闸看的是审阅态，不知道音轨被换过）。已合成的
        # 产物、审阅登记与本批费用均已落盘，重跑只补缺的那几镜。
        ids = ",".join(str(i) for i in missing[:8]) + ("…" if len(missing) > 8 else "")
        raise KinemaError(
            f"{len(missing)} 镜有台词但逐镜 wav 不在盘（镜 {ids}）——旁白轨不重拼，"
            f"保留现有 narration.wav。先补跑：tts --only {ids}"
            "（已合成镜的产物与费用均已登记）")
    if all(k == "silence" for k, _ in parts):
        _info("没有可合成的旁白。")
        return

    narration = adir / "narration.wav"
    concat_audio(parts, narration, tail_fade=voicecast.TAIL_FADE)

    ts_file = adir / "timestamps.json"
    ts_file.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    project.audio["voice_provider"] = prov.name
    project.audio["narration_file"] = str(narration)
    project.audio["timestamps"] = str(ts_file)
    project.save()
    # 两个数必须对得上：旁白轨实测时长 ↔ 分镜时间轴（Σdur，停顿已折进 dur）。
    # 对不上就是音画同步出问题的第一现场，合成阶段还会再兜一次（_sync_narration）
    _info(f"全片配音轨: {narration.name} · 总时长 {probe_duration(narration):.2f}s"
          f" · 分镜时间轴 {project.total_duration():.2f}s")
    # native 混烧与 dubbed 主音轨：配音已逐镜压进窗口（变速不变调）保证音画对位，
    # 但压得太狠就是"念得像快进"——那是台词写太满，改词或加长镜头都归创作，引擎只点名不代改
    if motion in ("native", "dubbed"):
        over = voicecast.fit_overruns(project, adir)
        if over:
            head = "、".join(f"镜{i}({r}×)" for i, _sp, _w, r in over[:6])
            _info(f"⚠ {len(over)} 镜台词超出画面窗口 >{voicecast.FIT_TEMPO_WARN}×，"
                  f"混烧时会被压快到听感偏赶：{head}"
                  + ("…" if len(over) > 6 else ""))
            _info("  → 两条正路：① 精简这几镜的台词（lint narration_overrun 按在用音色"
                  "实测语速预估，真发前即可看到）；② 加长该镜 dur 后重烧 Seedance（贵）。"
                  "配音本身已按窗口对齐，不处理也能出片、只是那几句偏快")


# 字幕样式/语言判据单一真源在 pipeline.subtitle.sub_cfg：烧录、交付 SRT 与
# Studio 导出共用（studio 域不能反向引 cli，故实体下沉、此处留别名）。
_sub_cfg = subtitle_mod.sub_cfg


def stage_subtitle(project, store, router, *, profile=None, **_):
    # 预览/独立用：按主比例渲染一份；compose 会为每个目标比例各自渲染正确画布的字幕。
    w, h = store.canvas(project.aspect)
    out = project.subdir("subs") / f"sub_{aspect_tag(project.aspect)}.ass"
    sub_cfg = _sub_cfg(store, project, profile)
    mode = sub_cfg.get("mode", "caption")
    _step(f"字幕 · 模式 {mode}（主比例 {project.aspect}）")
    subtitle_mod.render(project.timeline(), out, canvas_w=w, canvas_h=h, sub_cfg=sub_cfg,
                        spans_of=compose_mod.speech_spans_resolver(project, project.aspect))
    project.audio["subtitle_file"] = str(out)
    project.save()
    _info(f"字幕: {out.name} · {len(project.shots)} 段")


# profile → 本地音乐库情绪子目录（local provider 用；其他 provider 忽略）
_MUSIC_MOOD = {
    "hd2d": "cinematic", "gba": "cinematic", "snes": "cinematic", "dark_fantasy": "cinematic",
    "anime": "cinematic", "game_sim": "upbeat",
    "explainer": "upbeat", "narration": "upbeat", "ranking": "upbeat",
    "quote": "calm", "storybook": "calm",
}


def stage_music(project, store, router, *, profile=None, force=False):
    prof = profile or project.profile
    prov, params = router.resolve("music", prof)
    adir = project.subdir("audio")
    dur = project.total_duration() or float(project.data.get("duration", 60))
    ext = "wav" if prov.name == "mock" else "mp3"
    out = adir / f"bgm.{ext}"
    if project.native_audio and project.data.get("control_bgm"):
        _stage_control_bed(project, out, dur, force=force)
        return
    # BGM 情绪：profile 配置优先（models.yaml profiles.<x>.music.mood），内置映射兜底
    mood = params.get("mood") or _MUSIC_MOOD.get(prof or store.default_profile)
    _step(f"背景音乐 [{prov.name}] · {dur:.1f}s"
          + (f" · 情绪 {mood}" if prov.name == "local" and mood else ""))
    res = None
    prompt = params.get("prompt") or project.data.get("music_prompt") or \
        f"background music for a short video about {project.data.get('theme', '')}"
    need = force or not out.is_file()
    if not need:
        # 幂等判据不能只看文件在否：时间轴改过（补镜/重跑 tts 按配音覆写 dur）后
        # 盘上还是旧时长的曲子——mixdown 用 aloop 铺满片长，曲尾的淡出会在成片
        # 中段变成「淡出到静音再从头淡入」的音乐断层。首轮永远正确，只有
        # 「改了再跑」才踩，故按实测时长比对（差 >1s 即重生，本地曲库零成本）
        have = probe_duration(out)
        if have and abs(have - dur) > 1.0:
            need = True
            _info(f"  BGM 时长与时间轴不符（曲 {have:.1f}s / 片 {dur:.1f}s）——重新生成")
    if need:
        res = prov.generate(prompt, str(out), duration=dur, mood=mood)
    project.audio["bgm_file"] = str(out)
    # 生成参数快照（复盘锚，与分镜 gen.* 同旨）：这条 BGM 是按什么长度/情绪/词生成的
    project.audio["bgm_params"] = {"duration": round(dur, 2), "mood": mood,
                                   "prompt": prompt[:200]}
    # 与 tts 同口径：超预算抛错前先把登记与本笔费用落盘
    try:
        if res is not None and res.cost > 0:
            project.add_cost("music", res.cost)
    finally:
        project.save()
    _info(f"BGM: {out.name}")


def _stage_control_bed(project, out, dur: float, *, force: bool) -> None:
    """`control_bgm`：用控制视频源片同一区间的音轨作这一章的 BGM（本地 ffmpeg，零 API 成本）。

    先量对拍：成片相对控制段的整体偏移记进 `gen.control.sync`，够格的偏移平移该镜配乐的
    起点。幂等判据是段落表指纹而不是片长：重框区间、换素材、偏移变了都不改片长，却已经
    是另一段音乐。
    """
    for r in control_mod.measure_sync(project):
        _info(f"对拍 · 镜 {r['shot']}：{control_mod.describe_sync(r)}")
    segs = control_mod.soundtrack_segments(project)
    sig = control_mod.soundtrack_signature(segs)
    params = project.audio.get("bgm_params") or {}
    _step(f"配乐 [深度捕捉源片音轨] · {dur:.1f}s · {len(segs)} 镜")
    if (force or not out.is_file() or params.get("source") != "control"
            or params.get("sig") != sig):
        r = control_mod.build_soundtrack(project, out)
        _info(f"BGM: {out.name}（{r['segments']} 段源片音轨，未绑定的镜留静音）")
    else:
        _info(f"BGM: {out.name}（段落未变，复用）")
    project.audio["bgm_file"] = str(out)
    project.audio["bgm_params"] = {"duration": round(dur, 2), "source": "control", "sig": sig}
    project.save()


def _score_rows(project, *, force=False, only=None, strict=True) -> list[dict]:
    """逐段计划表：`[{seg, text, part, sig, cached, burn}]`（纯只读·零成本）。

    dry-run 报价、真跑、Studio 的分段面板共用这一份——三处各算一遍必分叉，
    而分叉在这里的代价是"预览说不花钱、真跑却烧了三段"。

    `strict=False` 给只读预览用：剧本还没写/段数对不上时不抛，`text` 留空、
    该段标成"待写"——预览必须在剧本未完成时也可用。"""
    from . import audioscript
    segs = audioscript.plan(project)
    # 剧本先全取：缺哪一段就一分钱都不烧（`segment_script` 对不齐即抛）
    script_err = None
    try:
        scripts = [audioscript.segment_script(project, sg) for sg in segs]
    except KinemaError as e:
        if strict:
            raise
        # 预览侧要把「为什么全段待写」说出来：段数不匹配与剧本没写是两种病，
        # 只把 text 置空时，已写满的剧本也会显示满屏「待写」，原因无从定位
        scripts = [None] * len(segs)
        script_err = str(e)
    prev = {int(e.get("no") or 0): e for e in
            (((project.data.get("gen") or {}).get("score") or {}).get("segments") or [])}
    picked = set()
    if only:
        try:
            picked = {int(x) for x in str(only).split(",") if x.strip()}
        except ValueError:
            raise ProjectError(f"--only 需为段号逗号分隔（如 1,3）：{only}") from None
        bad = picked - {sg["no"] for sg in segs}
        if bad:
            raise ProjectError(f"--only 段号越界 {sorted(bad)}（本章共 {len(segs)} 段）")
    adir = project.subdir("audio")
    rows = []
    for sg, text in zip(segs, scripts):
        part = adir / f"score_{sg['no']:02d}.mp3"
        sig = audioscript.segment_sig(sg, text) if text is not None else None
        # 命中缓存 = 音频在盘 **且** 指纹与上次登记一致；只看文件在不在，
        # 改完剧本重跑会静默拼出一条"没生效"的旧音轨
        cached = bool(sig) and part.is_file() and (prev.get(sg["no"]) or {}).get("sig") == sig
        drift = bool(sig) and part.is_file() and not cached
        burn = bool(text is not None) and (
            (sg["no"] in picked) if picked else (force or not part.is_file()))
        rows.append({"seg": sg, "text": text, "part": part, "sig": sig,
                     "cached": cached, "drift": drift, "burn": burn,
                     "script_err": script_err})
    return rows


def _score_anchor(project, store, prov, seg, *, script="", ref_dir,
                  default_ref=None) -> tuple[list[str], str, list[str]]:
    """该段各说话人的**锚定参考音** → `(参考音路径表, 绑定前言, 未锚定的名字)`。

    没有这一步，`text_prompt` 里的声线描述只是"照这个气质造一把声音"——生成式模型
    每段各造一把，两段之间的旁白就是两个人。已选定/定制的音色也完全不参与。
    故凡是解析得出音色的说话人，一律把那把声音的参考音随请求发出去（接口的
    参考音频模式），并在正文前加一行绑定说明把名字与 `@音频N` 对上。

    剧本正文必须由调用方显式传入 `script`——`plan()` 的段字典不含剧本键，
    从 seg 里"顺手取"等于永远拿空串，`has_voice_def` 恒假，绑定行会把声线
    描述再发一遍（同一句描述在正文出现两次，还白占 3000 字符额度）。

    参考音来源与 `stage_tts` 同一套：定制音色用锁定时那条变体，官方音色用项目级
    锚定缓存（缺就现合成一句，全系列共用）。接口最多三条，超出的说话人只能靠
    文字描述，由调用方报出来。"""
    from . import audioscript
    plan = audioscript.anchor_plan(project, store, seg, default_ref)
    loose: list[str] = list(plan["loose"])
    # 先逐把解析参考音（同一把声音全段只解析一次），再统一编号——编号必须等于
    # 该 clip 在 refs 数组里的下标，某把官方音色现合成失败后如果沿用计划期的
    # 序号，后面每个 `@音频N` 都会指错条目
    resolved: dict[str, str] = {}
    dead: set[str] = set()
    for row in plan["anchored"]:
        who, vt = row["who"], row["voice_type"]
        if vt in resolved or vt in dead:
            continue
        if voicebank.is_custom(vt):
            # 定制音色的参考音就是音色本身：缺了必须显式报错，静默退回纯描述
            # 等于换了个人在配音（与 stage_tts 对同一情形的硬报错同一条纪律）。
            # clip 可能已被 oss sync 改写成 URL——适配器的参考音条目本就 URL
            # 感知（audio_url），URL 一律放行，只有本地路径才验在盘
            from .storage.media import is_url
            clip = voicebank.clip_for(project.data, vt)
            if not clip or (not is_url(clip) and not Path(clip).is_file()):
                raise KinemaError(
                    f"「{who}」的定制音色 {vt} 找不到锚定参考音: {clip or '未登记'}\n"
                    f"  重新试听并锁定：voice custom <项目> ... → "
                    f"voice use <项目> ... --custom --no <编号>")
        else:
            clip, spent = _anchor_clip(prov, ref_dir, vt)
            if spent > 0:
                try:                # 超限即断，但这笔已经花了：先落盘再让异常上抛
                    project.add_cost("tts", spent)
                finally:
                    project.save()
        if clip:
            resolved[vt] = clip
        else:                              # 官方音色现合成失败 → 这把声音整体退回纯描述
            dead.add(vt)
    refs: list[str] = []
    index: dict[str, int] = {}
    binds: list[str] = []
    for row in plan["anchored"]:
        who, vt = row["who"], row["voice_type"]
        if vt in dead:
            loose.append(who)
            continue
        no = index.get(vt)
        if no is None:
            refs.append(resolved[vt])
            no = len(refs)
            index[vt] = no
        # 绑定行的职责只有一件：把名字与 `@音频N` 对上。气质约束由剧本的声线定义段
        # 给——剧本里已经写了就不再重复，否则同一句描述在正文里出现两次
        # （还白占 3000 字符的额度）。剧本没写的才补一份——参考音必须携带绑定说明。
        desc, _ = audioscript.speaker_voice_desc(project, who)
        binds.append(f"{who} 的饰演者为@音频{no}。"
                     if audioscript.has_voice_def(script, who)
                     else f"{audioscript.voice_def(who, desc)}，饰演者为@音频{no}。")
    return refs, ("\n".join(binds) + "\n\n" if binds else ""), loose


def _anchor_clip(prov, ref_dir, voice_type: str) -> tuple[str | None, float]:
    """官方音色的锚定参考音（项目级缓存，缺则现合成一句）。返回 (路径, 本次合成费用)。

    路径取 `voicecast.anchor_ref_path` 单一真源——`stage_tts` 预热与页面试听
    共用同一条命名。**合成不带交付参数**：四个预热点写的是同一个文件，带上
    语速之类会让样本内容取决于谁先跑到；且样本时长决定音色跟随档位，按加速档
    合成出来的短样本会把贴合悄悄拉低一级。"""
    ref = voicecast.anchor_ref_path(ref_dir, voice_type)
    if ref.is_file():
        return str(ref), 0.0
    try:
        res = prov.synthesize(voicecast.ANCHOR_TEXT, str(ref), voice=voice_type)
    except Exception as e:  # noqa: BLE001  锚定失败退回纯描述，不阻断整段生成
        _info(f"⚠ 音色 {voice_type} 的参考音合成失败（{e}）——本段该说话人只按文字描述生成")
        return None, 0.0
    return str(ref), float(getattr(res, "cost", 0.0) or 0.0)


def _score_quote(prov, rows: list[dict]) -> float:
    """本次真发的预估费用（元）。seed-audio 按输出秒计费，故按段的规划秒长报价；
    单价为 0（未配置）时恒 0——与台账「零一律不入账」同口径。"""
    per_sec = float(getattr(prov, "price_per_second", 0.0) or 0.0)
    if per_sec <= 0:
        return 0.0
    return round(sum(r["seg"]["dur"] for r in rows if r["burn"]) * per_sec, 4)


def _score_dry_run(project, prov, rows: list[dict], problems: list[str]) -> None:
    """零成本预览：分段表 + 逐段状态 + 本次真发的报价。**一个请求都不发。**

    这条路按秒计费且整片一次买断——分几段、每段几秒、接缝落在哪一镜，
    必须在花钱之前全部可见。"""
    from . import audioscript
    _step(f"音频剧本 · 分段预览（零成本）· {len(rows)} 段 · "
          f"全片 {project.total_duration():.1f}s · "
          f"单段上限 {audioscript.MAX_SEGMENT_SEC:.0f}s")
    for r in rows:
        sg, ids = r["seg"], r["seg"]["shots"]
        if r["text"] is None:
            state = "剧本待写"
        elif r["burn"]:
            state = "本次生成"
        elif r["drift"]:
            state = "剧本已改·未点名重生"
        elif r["cached"]:
            state = "命中缓存·复用"
        else:
            # 仅 --only 点名了别的段时可达：这一段有剧本、音轨不在盘、又没被点名
            state = "音轨不在盘·未点名（要补则加进 --only）"
        # 演绎过几版要在这里看得见——不然 `--switch N --to-v ?` 里的 ? 无从填
        hist = (versioning.score_segment(project, sg["no"]) or {}).get("versions") or []
        ver = f" · 当前 v{len(hist) + 1:03d}（历史 {len(hist)} 版可切）" if hist else ""
        print(f"   第 {sg['no']} 段 · 镜 {ids[0]}~{ids[-1]}（{len(ids)} 镜）· "
              f"{sg['start']:.1f}s→{sg['end']:.1f}s · {sg['dur']:.1f}s · {state}{ver}")
    err = next((r["script_err"] for r in rows if r.get("script_err")), None)
    if err:
        print(f"   ⚠ {err}")
    for p in problems:
        print(f"   ⚠ {p}")
    burn = [r for r in rows if r["burn"]]
    quote = _score_quote(prov, burn)
    secs = sum(r["seg"]["dur"] for r in burn)
    if not burn:
        print("   本次真发：0 段（无需生成）")
    elif quote:
        print(f"   本次真发：{len(burn)} 段 · {secs:.0f}s · 预估 ¥{quote:.2f} [{prov.name}]")
    else:
        # 单价为 0 = 未配置，不是免费——照「零一律不入账」的口径说清楚，
        # 「预估 ¥0.00」与「不计费」必须区分开
        print(f"   本次真发：{len(burn)} 段 · {secs:.0f}s · [{prov.name}] "
              f"未配置 price_per_second，无法报价（不等于不计费）")


# 段版本切换后的整轨重拼实体在 audioscript.score_reconcat：CLI 与 Studio 共用
# （studio 域不能反向引 cli，故实体下沉、此处留别名——与上方 sub_cfg 同制度）。
from .audioscript import score_reconcat  # noqa: E402


def _score_switch(project, *, no: int, to_v: int) -> None:
    """把某段切到历史版：互换而非覆盖（来回切不丢任何一版）+ 重拼整轨。"""
    versioning.rollback_score_segment(project, no, to_v)
    total = score_reconcat(project)
    _info(f"✓ 第 {no} 段已切到 v{to_v}（原当前版已归档，可再切回）· "
          f"整轨已重拼 {total:.1f}s")
    _info("   成片要用上新音轨，重跑一次 assemble（合成不重新计费）")


def _score_draft(project, *, force=False) -> None:
    """按分镜起草音频剧本（确定性·零成本·不发任何请求）。

    分工是「引擎起稿 → 指挥层改写」：台词与段内秒段由引擎算，
    声线气质/配乐/音效/逐句语气归指挥层。**已写的段不覆盖**，只补空位。"""
    from . import audioscript
    rows, thin = audioscript.draft(project)
    raw = project.data.get("audio_script")
    old = (raw.get("segments") if isinstance(raw, dict) else
           ([raw] if isinstance(raw, str) and raw.strip() else [])) or []
    kept, wrote = 0, 0
    out = []
    for i, text in enumerate(rows):
        prev = str(old[i]).strip() if i < len(old) else ""
        if prev and not force:
            out.append(prev)
            kept += 1
        else:
            out.append(text)
            wrote += 1
    project.data["audio_script"] = {"segments": out}
    project.save()
    _step(f"音频剧本 · 按分镜起草（零成本）· 写入 {wrote} 段"
          + (f" · 保留已写 {kept} 段（覆盖加 --force）" if kept else ""))
    _info("底稿只含**台词与段内秒段**（引擎能确定的那部分）——"
          "声线气质、配乐、音效、逐句语气要交给 AI 在底稿上改写：\n"
          "   网页「AU 音频剧本」台点「⧉ 音频剧本指令」，或按 kinema-audio SKILL 第四节自己改")
    if thin:
        _info(f"⚠ 这些说话人还没有声线描述，底稿用的是中性底：{'、'.join(thin)}\n"
              f"   补法：定制生成给他们造音色（那段描述会被起草直接取用），"
              f"或让 AI 按人设改写底稿的声线定义段")


def stage_score(project, store, router, *, profile=None, force=False,
                concurrency=None, only=None, dry_run=False, draft=False,
                switch=None, to_v=None):
    """音频剧本生成（`audio_mode: scored`）：逐段把剧本发给音频模型，
    拼成整片唯一的音轨。人声、音乐、音效都在这一条里，合成段不再叠 BGM。

    段与段之间不做交叉淡化——接缝按 `audioscript.plan` 落在转场镜上，那里画面
    本来就在切，音乐重新起头是这段戏结束的正常听感。

    **幂等按段不按片**：已在盘的段直接复用，只补缺的那些。这条路按秒计费且单段
    可达 115 秒，整片重来一次的代价是逐镜 TTS 的量级——断点续跑必须落在段上。"""
    from datetime import datetime
    from . import audioscript
    if draft:                       # 起草是纯本地写文档，连 provider 都不必解析
        _score_draft(project, force=force)
        return
    if switch is not None:          # 切换段版本：纯本地文件互换 + 重拼，零成本
        if to_v is None:
            raise KinemaError("切换段版本要同时给 --switch <段号> 与 --to-v <版本号>")
        _score_switch(project, no=int(switch), to_v=int(to_v))
        return
    prov = router.resolve_named("tts", voicebank.CUSTOM_PROVIDER)
    _cast_gate(project, router)
    problems = audioscript.check(project)
    # dry-run 只读：超限与缺剧本都当成"要报给人看的现状"而不是终止条件——
    # 预览须在剧本未完成时也可用
    if dry_run:
        _score_dry_run(project, prov,
                       _score_rows(project, force=force, only=only, strict=False),
                       problems)
        return
    if problems:
        raise KinemaError("音频剧本分段超出单次生成上限：\n  " + "\n  ".join(problems))
    rows = _score_rows(project, force=force, only=only)
    adir = project.subdir("audio")
    # 产物是 wav 而不是 mp3：`concat_audio` 恒以 pcm_s16le 写出（与 narration.wav
    # 同一条拼接原语），塞进 mp3 容器 ffmpeg 直接拒收。逐段 part 仍按 provider
    # 返回的编码留 .mp3——拼接用 filter concat 逐输入解码，不看扩展名
    out = adir / "score.wav"
    # 剧本改了却没点名重生 → 大声报，但**绝不擅自烧钱**：这条路按秒计费，
    # 「自动跟上最新剧本」在这里等于自动扣款；自动失效只适用于零成本的合成缓存。
    stale = [r["seg"]["no"] for r in rows if r["drift"] and not r["burn"]]
    if stale:
        _info(f"⚠ 第 {'、'.join(map(str, stale))} 段的剧本已改动，但本次沿用旧音轨"
              f"（按秒计费，不自动重生）——要跟上改动跑 "
              f"`score --only {','.join(map(str, stale))}`")
    # **整轨已经有了就到此为止**，不看逐段中间产物在不在：段文件可能已被
    # oss sync 改写成 URL，也可能换机/清理后不在本地——只按段文件判的话，
    # 会判成「一段都没生成」，把整章按秒重新买一遍。
    # 已上云的 `score_file` 是 URL，`has_file` 视其为在（渲染前 ensure_local 拉回）；
    # 本地文件则必须验时长——ffmpeg 拼接失败会留下 0 字节产物，只验存在会在下一次
    # 报「全部复用」然后拿空音轨去合成。
    from .storage.media import is_url
    on_cloud = is_url(str(project.audio.get("score_file") or ""))
    ready = has_file(project.audio.get("score_file")) and (
        on_cloud or (out.is_file() and probe_duration(out) > 0))
    if ready and not force and not only:
        _info(f"音频剧本: {'已上云' if on_cloud else out.name} · "
              f"{len(rows)} 段全部复用（重生成加 --force，改某段用 --only 段号）")
        return
    burn = [r for r in rows if r["burn"]]
    quote = _score_quote(prov, burn)
    _step(f"音频剧本 [{prov.name}] · 本次生成 {len(burn)}/{len(rows)} 段 · "
          f"全片 {project.total_duration():.1f}s"
          + (f" · 预估 ¥{quote:.2f}" if quote else ""))

    # 音色锚定：把每个说话人已选定/已定制的那把声音随请求发出去。锚定在主线程
    # 一次算完（可能要为官方音色现合成参考音，且同一把声音全项目共用一个缓存文件），
    # 绝不进工作线程——那会让几段并发各合成一遍同一段参考音。
    # **必须排在归档之前**：定制参考音缺失会在这里抛错，而归档会搬动盘上的
    # 现存段——抛在它后面时尚未生成就已移动了现存产物
    params = router.resolve("tts", profile or project.profile)[1]
    rate = project.data.get("speech_rate", params.get("speech_rate"))
    extra = {"speech_rate": rate} if rate is not None else {}
    # 与 stage_tts 同一条音色解析链：旁白锁 > profile 默认 > 项目 voice_id
    default_ref = voicecast.default_voice_ref(project.data, params)
    ref_dir = _voice_ref_dir(project)
    for r in burn:
        r["refs"], r["bind"], r["loose"] = _score_anchor(
            project, store, prov, r["seg"], script=r["text"] or "",
            ref_dir=ref_dir, default_ref=default_ref)

    # 重生成之前先把现存那一版移进段版本栈——生成写的是同一个路径，等它跑完再归档
    # 归进去的已经是新的那版。生成式模型每次演绎都不同，这条谱系是**创作工具**
    # （同一段连出几版挑一版），不是误操作恢复用的备份
    archived = [versioning.archive_score_segment(
        project, r["seg"]["no"],
        reason="drift（剧本已改）" if r["drift"] else ("force" if force else "regen"))
        for r in burn]
    if any(archived):
        # 归档已经移动了盘上文件，版本账必须当场落盘：只活在内存里的账，任一段
        # 生成失败即整批丢失——versions/ 留下无账孤儿，下次归档按 len(hist)+1
        # 重发同名文件，shutil.move 会把已付费的旧演绎静默覆盖掉
        project.save()
    loose = sorted({w for r in burn for w in r["loose"]})
    if loose:
        _info(f"⚠ 未锚定音色的说话人：{'、'.join(loose)}——只按剧本里的文字描述生成，"
              f"多段之间可能不像同一个人。给他们试音选定（voice audition/use）"
              f"或做定制音色（voice custom）即自动锚定；单段最多锚 "
              f"{voicecast.MAX_ANCHOR_REFS} 把声音（接口上限）")

    def _one(row):
        # 有参考音就走接口的参考音频模式（`prompt_only` 只用于一把声音都没锚定的段）
        return row, prov.synthesize(row["bind"] + row["text"], str(row["part"]),
                                    ref_audios=row["refs"] or None,
                                    prompt_only=not row["refs"], **extra)

    # retries=0：按秒计费的调用一次都不许自动重试（详 `docs/agents/concurrency.md`）
    # ——瞬时错误已由 providers/_util 在 HTTP 层退避，逃到这一层的重试就是白付第二遍钱
    done = parallel.run(
        [parallel.Task(key=f"score:{r['seg']['no']}", run=(lambda x=r: _one(x)),
                       label=f"第 {r['seg']['no']} 段 {r['seg']['dur']:.0f}s",
                       meta={"no": r["seg"]["no"]}) for r in burn],
        workers=min(len(burn), parallel.resolve_workers(concurrency)), retries=0)
    got, failed, failed_nos = {}, [], set()
    for d in sorted(done, key=lambda x: x.meta["no"]):
        if d.ok:
            row, res = d.value
            got[row["seg"]["no"]] = res
        else:
            failed.append(f"第 {d.meta['no']} 段生成失败: {d.error}")
            failed_nos.add(int(d.meta["no"]))
    # 登记与记账**先于**抛错（并发纪律 1.3）：这批钱已经花了，任一段失败都不能
    # 让成功段丢登记——sig 不落盘，下次重跑就把它们报成「剧本已改·未点名重生」，
    # 诱导用 --only 再买一遍其实已经在盘的音频
    parts, meta, cost, missing = [], [], 0.0, []
    for r in rows:
        sg, no = r["seg"], r["seg"]["no"]
        res = got.get(no)
        if res is not None:
            cost += res.cost
        prev = versioning.score_segment(project, no) or {}
        if not r["part"].is_file():
            # 段音频不在盘（生成失败/工作目录被清）：沿用既有登记（含刚归档进
            # 版本栈的账），绝不编造一条指向空文件的新登记。
            # 刚报过「生成失败」的段不再点第二遍名
            if no not in failed_nos:
                missing.append(no)
            if prev:
                meta.append(prev)
            continue
        parts.append(("file", str(r["part"])))
        meta.append({"no": no, "shots": sg["shots"], "planned": sg["dur"],
                     # `file` 是段版本栈的标准路径字段（archive/rollback 认它）
                     "file": str(r["part"]),
                     "sig": r["sig"] if res is not None else prev.get("sig", r["sig"]),
                     "actual": round(probe_duration(r["part"]), 2),
                     # 谱系随段走：这一段换过几版、每版什么时候生成的，切换时要看得见
                     "versions": prev.get("versions") or [],
                     "segments": res.segments if res is not None else prev.get("segments", [])})
    gen_score = project.data.setdefault("gen", {}).setdefault("score", {})
    gen_score["provider"] = prov.name
    gen_score["segments"] = meta
    if failed or missing:
        # 整轨此刻与逐段登记对不上了：时长/时间戳留着旧值，页面会显示上一轮的全长
        gen_score.pop("duration", None)
        gen_score.pop("at", None)
    try:
        if cost > 0:
            project.add_cost("score", cost)
    finally:
        project.save()
    if failed or missing:
        # 已成功的段此刻已登记入账（下次按指纹复用），报错只点名缺的——
        # 避免整批按秒重新计费
        raise KinemaError("\n".join(failed + [f"第 {no} 段音频不在盘" for no in missing])
                          + "\n   已生成的段已登记入账，修好后重跑 score 只补缺的那些"
                          + "（不重复计费），整轨会在补齐后重拼")
    concat_audio(parts, out)
    total = probe_duration(out)
    if total <= 0:
        # 拼出空文件就地删掉再抛：留着它下一次跑会被当成"已在盘"，
        # 而各段音频还在盘上，修好后重跑一分钱都不用再花
        out.unlink(missing_ok=True)
        raise KinemaError(f"音频剧本拼接后时长为 0（{len(parts)} 段）——各段音频仍在 "
                          f"{adir.name}/，排查后重跑 score 不重复计费")
    project.audio["score_file"] = str(out)
    gen_score["duration"] = round(total, 2)
    gen_score["at"] = datetime.now().isoformat(timespec="seconds")
    project.save()
    want = project.total_duration()
    _info(f"音频剧本: {out.name} · {total:.1f}s"
          + (f" · 本次 ¥{cost:.2f}" if cost else ""))
    # 时长对不上不拉伸也不静默：画面按分镜 dur 走，音轨长了会被裁、短了留静音，
    # 差多少必须让人看见——修法是改剧本的时间控制或改分镜时长，不是让引擎猜
    if want and abs(total - want) > 1.0:
        _info(f"⚠ 音轨 {total:.1f}s 与分镜时间轴 {want:.1f}s 相差 "
              f"{abs(total - want):.1f}s——按 kinema-audio 的时间控制 [起s:止s] 对齐剧本，"
              f"或改分镜 dur")


def stage_compose(project, store, router, *, profile=None, out=None, force=False):
    ensure_tools()
    prof = profile or project.profile
    effects = store.effects_for(prof, project.effects)
    sub_cfg = _sub_cfg(store, project, prof)
    aspects = project.aspects
    _step(f"合成 · 比例 {aspects}"
          + (f" · 特效[{','.join(effects)}]" if effects else ""))
    for asp in aspects:
        # 覆盖前先归档：合成写的是同一个输出路径，不在这里拦一道，上一版成片就没了。
        # 成片是全链最贵的产物（图+配音+视频+算力全在里面），它必须与分镜产物一样可回溯。
        old = versioning.archive_output(project, asp, reason="重新合成")
        try:
            final = compose_mod.build(
                project, store, aspect=asp, effects=effects, sub_cfg=sub_cfg,
                out=(out if (out and len(aspects) == 1) else None), force=force)
        except BaseException:
            # 归档是移动：合成没跑成就把刚归档那版放回去，绝不留下一个指向空路径的成片字段
            if versioning.restore_last_output(project, asp):
                project.save()
                _info(f"[{asp}] 合成未完成，上一版成片已放回原处")
            raise
        project.output[asp] = final
        project.data.pop("verify", None)        # 成片换了，上一版的体检结论作废
        project.save()
        _info(f"[{asp}] 成片: {final} · {probe_duration(final):.2f}s"
              + (f"  (上一版已归档 v{versioning.output_current_version(project, asp) - 1:03d})"
                 if old else ""))


# ---------- 组合 ----------
def _auto_approve_reviews(project):
    """run 一条龙收尾自动过审：三阶段产物中「待审(wfa)」的一律置「通过(done)」。

    run = 全自动模式，跑完即视为验收通过，不该让 Studio 里挂一屏待审。
    只动 wfa——todo/wip/retake/done 一律不碰（不吞人工表态）；转场镜与弃用镜跳过。
    mock 不走此函数：done=锁定（--force 也不覆盖），mock 占位物一旦锁定，
    后续真跑会被全部跳过——占位产物从此阻断正式生成。
    """
    changed = 0
    for s in project.data.get("shots", []):
        if transitions_mod.is_transition(s) or review.is_omitted(s):
            continue
        for stage in review.STAGES:
            if review.get_state(s, stage) == "wfa":
                review.set_state(s, stage, "done")
                changed += 1
    if changed:
        project.save()
        _info(f"自动过审: run 全自动=验收通过，{changed} 项待审已置为通过"
              f"（要保留待审人工把关，加 --no-approve）")


def _assemble_review_gate(project) -> list:
    """合成前审阅闸：返回未过审的 (镜号, 阶段) 清单（空=全过审，可出正式成片）。

    视觉阶段随渲染模式——kenburns 查 image、dubbed/native 查 clip；
    要产旁白轨的章（`needs_narration_track`）另查进旁白轨的台词镜的 audio
    ——native 混烧的对白镜由模型发声，按设计没有 audio 产物，不进此闸。
    转场镜与弃用镜跳过。
    这是「免费合成」这步的防线：正式成片（assemble→output/）须全部镜过审；
    未过审看零成本草稿走 animatic，或 assemble --draft 明确出草稿。
    run/--auto 不经此闸（收尾 _auto_approve_reviews 自动过审）。"""
    visual = "clip" if project.uses_seedance else "image"
    missing: list = []
    for s in project.data.get("shots", []):
        if transitions_mod.is_transition(s) or review.is_omitted(s):
            continue
        if review.get_state(s, visual) != "done":
            missing.append((s.get("id"), visual))
        if project.needs_narration_track and voicecast.narration_shot(s, project.motion) \
                and review.get_state(s, "audio") != "done":
            missing.append((s.get("id"), "audio"))
    return missing


def _require_clips(project) -> None:
    """动镜档合成前的片段收口：正镜缺片段即中止，不落回静图。就绪度跳过、审核拒与
    失败在图生视频阶段各自打印过原因，这里只点名镜号。"""
    missing = [str(s.get("id")) for s in project.active_shots
               if not transitions_mod.is_transition(s)
               and any(not has_file(project.clip_for(s, a)) for a in project.aspects)]
    if missing:
        raise KinemaError(
            f"⊘ {len(missing)} 镜没有片段（镜 {'、'.join(missing)}），全自动出片中止"
            "——逐镜原因见上方图生视频日志（设定图不齐 / 审核拒 / 失败）；补齐后重跑")


def _reject_native_bgm_conflict(project, *, want=None) -> None:
    """native 混烧与曲库 BGM 互斥：混烧已把片段原生音降为背景床占住 BGM 母线。
    `run` 与 `assemble` 同走此判定，同一份章节文档不得一边拒绝一边静默丢音。"""
    if project.native_audio and project.native_voiceover \
            and (want or project.data.get("native_bgm") or project.data.get("control_bgm")):
        raise KinemaError(
            "⊘ native 配音混烧与 BGM 互斥：混烧已把片段原生音降为背景床占住 BGM 母线，"
            "再叠曲库 BGM 或源片音轨会把模型自带的环境与空间感整个顶掉。\n"
            "   要曲库 BGM：去掉 --burn-voice / 章节 native_voiceover: false（原生人声作主轨）\n"
            "   要固定音色旁白：保持混烧，BGM 交给模型原生音")


def _stage_audio_bed(project, store, router, *, profile=None, force=False,
                     concurrency=None) -> None:
    """成片的音频底：音频剧本整轨（scored）与曲库 BGM 的三档互斥选曲。

    `run` 与 `assemble` 共用这一份——两条路径各写一份，同一份章节文档会出两种
    成片。选曲判据必须与 `compose.build` 的 use_bgm 逐字一致：这边跑了那边不认
    是白花一次选曲，那边认了这边没跑就是 compose 指着一个不存在的 bgm 文件。

    force 刻意不下传给 stage_score：`--force` 说的是「重跑本地产物」，而 score
    的 force 是整章按秒重新买断——重生音频剧本走 `score --force`（显式购买意图）。
    """
    _reject_native_bgm_conflict(project)
    if project.scored_audio:     # 音频剧本：人声/音乐/音效已在一条轨里
        stage_score(project, store, router, profile=profile, concurrency=concurrency)
    if compose_mod.use_bgm_for(project):
        stage_music(project, store, router, profile=profile, force=force)


def cmd_run(args):
    store = ConfigStore.load(args.config)
    path = Path(_project_path(args))
    router = ModelRouter(store, force_mock=args.mock)
    ensure_tools()
    # 一条龙全程独占章节（与 _stage_wrapper 同一把操作锁；进程内可重入，
    # 各阶段自身的申请落在同一持有内），锁先于装载
    from .locking import op_lock
    with op_lock(path, kind="run"):
        project = Project.load(path)
        _apply_aspect_args(project, args)
        _cast_gate(project, router)          # 花第一笔钱之前
        # 一条龙是真发：未表态章节在此定档并写入，各阶段随后读到同一个档位。
        # 写入在准入之后：被拒的 run 不该留下表态
        if not project.motion_declared:
            _announce_motion(project, persist=True)
            _settle_motion(project)
        _info(f"配置源: {store.source} · profile: "
              f"{args.profile or project.profile or store.default_profile}"
              f" · 比例 {project.aspects} · 运动 {project.motion}"
              + ("  (mock)" if args.mock else ""))
        # 一条龙收尾由 _finish_run 补封面，生图收尾的「封面尚未生成」在这里是误报
        stage_gen_image(project, store, router, profile=args.profile, force=args.force,
                        concurrency=getattr(args, "concurrency", None),
                        warn_cover=False)
        # 旁白轨由固定音色配音承担（kenburns/dubbed 全轨；native 混烧只给旁白镜，
        # 对白由模型原生发声，stage_tts 按 in_narration_track 跳过它们）
        if project.needs_narration_track:
            stage_tts(project, store, router, profile=args.profile, force=args.force,
                      concurrency=getattr(args, "concurrency", None))
        if project.uses_seedance:            # dubbed/native 走 Seedance 图生视频
            # auto=True：一条龙下「单笔超阈」告警放行——run 没有加 --confirm-spend 的机会，
            # 硬拦会中断整条流水线。硬超上限(budget)仍然拦，那个放行等于必然爆预算。
            stage_gen_video(project, store, router, profile=args.profile, force=args.force,
                            auto=True)
            _require_clips(project)
        stage_subtitle(project, store, router, profile=args.profile)
        _stage_audio_bed(project, store, router, profile=args.profile,
                         force=args.force,
                         concurrency=getattr(args, "concurrency", None))
        stage_compose(project, store, router, profile=args.profile, out=args.out,
                      force=args.force)
        if not args.mock and not args.no_approve:
            _auto_approve_reviews(project)
    return _finish_run(project, args)


def _finish_run(project, args) -> int:
    """收尾：补封面并打总结。成片与过审在此之前已完成，封面失败只影响 Studio 卡片图源：
    总结照打、退出码保持非零，并给出与本次同参数的补封面命令。"""
    cover = _run_cover_args(project, args)
    try:
        if cover is not None:
            cmd_cover(cover)
    except KinemaError as e:
        hint = (f"python3 -m kinema cover {cover.project} --chapter {cover.chapter} "
                f"--workspace {cover.workspace}"
                + (" --mock" if cover.mock else "")
                + (f" --profile {cover.profile}" if cover.profile else "")
                + (f" --config {cover.config}" if cover.config else ""))
        _info(f"⚠ {e}")
        _info(f"  → 成片已完成；补封面：{hint}")
        _print_summary(project)
        return 1
    _print_summary(project)
    return 0


def _run_cover_args(project, args):
    """一条龙收尾补章节封面的参数（系列主视觉缺席时一并出）：Studio 项目卡与章节列表的图源
    是「封面 → 成片海报帧 → 首个正镜分镜图」三级回落，全自动跑完不该让卡片一直顶着分镜图。
    已在盘的不重生；散装 `--project` 文件没有系列目录，返回 None。"""
    from types import SimpleNamespace
    ch = project.data.get("chapter") or {}
    pid, cid = ch.get("project"), ch.get("id")
    if not pid or not cid or Path(project.path).parent.name != "chapters":
        return None
    return SimpleNamespace(
        project=pid, chapter=cid, all=False, title=None, subtitle=None, desc=None,
        cast=None, aspects=None, size=None, typeset_title=False, font=None,
        profile=args.profile, mock=args.mock, force=False, no_moodboard=False,
        config=args.config, workspace=str(Path(project.path).parents[2]))


def _series_cover_needed(force: bool, chapter, have_all: bool) -> bool:
    """系列主视觉是全部章节封面的风格锚：缺席必补；--force 只在未点名章节时波及它。"""
    return not have_all or (force and not chapter)


def _ask_yes(question: str, *, default: bool) -> bool:
    """终端确认。**非交互恒取 default、绝不阻塞**——合成挂在出片主链上，
    在无人值守的进程里等输入会把任务挂到看门狗超时，看起来像是莫名其妙卡死。
    非 TTY（管道 / Studio 后台任务 / CI）与读不到输入（EOF、Ctrl-C）都按缺省走。"""
    if not sys.stdin.isatty():
        return default
    try:
        ans = input(f"{question} {'(Y/n)' if default else '(y/N)'} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return default if not ans else ans in ("y", "yes", "是")


def _frame_aspect_gaps(project, store, shots, targets) -> list[str]:
    """分镜图比例与画布不符的镜（面向人的说明串，逐镜一条）。

    测量口径**复用素材体检**（`mediacheck.aspect_overflow` + `SUPPLY_ASPECT_TOL`）——
    阈值与算法只能有一份；这里另写的只有后果措辞：Ken Burns 下不符是「cover 取景
    裁掉主体」，图生视频下是另一回事——分镜图无论进 `first_frame` 还是
    `reference_image`，比例不符都意味着模型必须自己重新构图，人工审过的那一帧
    不会是成片的画面。
    **只测量、不拦截**：调用方决定怎么处置。ffprobe 不在 PATH 时返回空
    （体检是护栏，不该反过来锁死没装全工具的机器）。
    """
    if not mediacheck_mod.ffprobe_available():
        return []
    out: list[str] = []
    for s in shots:
        for asp in targets:
            img = project.image_for(s, asp)
            if not has_file(img):
                continue                     # 缺图归就绪度节点管，不在此重复报
            try:
                info = mediacheck_mod.image_info(probe_json(str(img)))
            except Exception:                # noqa: BLE001  解不出的图归素材体检管
                continue
            if not info:
                continue
            size = (info["width"], info["height"])
            canvas = store.canvas(asp)
            ov = mediacheck_mod.aspect_overflow(size, canvas)
            if ov > mediacheck_mod.SUPPLY_ASPECT_TOL:
                out.append(f"镜 {s.get('id')} [{asp}] 分镜图 {size[0]}×{size[1]} "
                           f"与画布 {canvas[0]}×{canvas[1]} 不同比（差 {ov * 100:.0f}%）")
            break                            # 逐镜只报一次，比例是同一张图的属性
    return out


def _gate_frame_aspect(project, store, shots, targets, *, dry_run: bool) -> None:
    """计费前的分镜图比例闸：不同比就说清后果，交互式问一次。

    为什么必须有这道闸：`providers/image/agent.py` 明写尺寸不在验收时硬卡、留给
    「compose 前的素材体检」复查，而 `gen-video` 跑在 compose 之前——没有这道闸，
    一张 3:2 的分镜图可以一路走到付费请求里，成片回来才发现开场是另一个构图。

    **不硬拦**：比例不符仍能出片，只是出的不是审过的那一帧，这属于要不要接受的
    取舍而非买不得的组合。**非交互不替用户决定**：只把事实说清并照常发出，
    在 Studio 后台任务/CI 里替用户中止和替用户确认一样越权。
    """
    gaps = _frame_aspect_gaps(project, store, shots, targets)
    if not gaps:
        return
    _info(f"⚠ {len(gaps)} 镜的分镜图与画布不同比——模型必须自己重新构图，"
          "审过的那一帧不会是成片画面：")
    for g in gaps[:8]:
        _info(f"   · {g}")
    if len(gaps) > 8:
        _info(f"   · …另有 {len(gaps) - 8} 镜")
    _info("   修法：按画布比例重出这几张分镜图（gen-image --force --only <镜号>）")
    if dry_run:
        return
    if not sys.stdin.isatty():
        _info("   非交互环境：本次照常发出")
        return
    if not _ask_yes("   仍然按现在的分镜图继续生成吗？", default=False):
        raise ProjectError("已中止——先把分镜图按画布比例重出，再跑 gen-video")


def _voiceover_gap(project) -> tuple[int, int, str] | None:
    """声明语态与实际旁白镜占比不符时返回 `(旁白镜数, 正镜数, 语态)`，否则 None。

    判据整条走 `variation`：语态取 `voiceover_mode`（顶层声明 > 画风/Skill 缺省），
    超限与否取 `voiceover_overrun`（lint 的 `voiceover_heavy` 读同一个）。这里
    绝不另算一遍占比或另设样本下限，否则会出现「lint 说没超、闸说超了」这种
    自相矛盾的状态。
    """
    shots = [s for s in project.shots
             if not transitions_mod.is_transition(s) and not review.is_omitted(s)]
    mode = variation_mod.voiceover_mode(project.data)
    over = variation_mod.voiceover_overrun(shots, mode)
    if not over:
        return None
    return over[0], over[1], mode


def _gate_voiceover(project, *, dry_run: bool) -> None:
    """计费前的旁白语态闸：声明了剧情/氛围语态却镜镜旁白时问一次。

    lint 早就报这一条（`voiceover_heavy`），但 lint 不拦、也不在花钱那一步出现，
    整章带着该告警出片，成片就是解说腔。这道闸不改判据、不改阈值，
    只是把同一条结论搬到付费前再说一次，并给出「要么改语态声明、要么改分镜」
    两条明确出路。

    与比例闸同制：不硬拦（说书式剧情片是合法选择），非交互不替用户决定。
    """
    gap = _voiceover_gap(project)
    if not gap:
        return
    n_vo, n, mode = gap
    limit = ("上限 %d%%" % round(variation_mod.VOICEOVER_HEAVY_RATIO * 100)
             if mode == "sparse" else "零人声叙述")
    _info(f"⚠ 本章声明 voiceover: {mode}（{limit}），实际 {n_vo}/{n} 镜由旁白讲述"
          "——按这个分镜出片会得到解说腔，而不是剧情片")
    _info("   两条出路：把动作/战斗/环境镜的 narration 留空（引擎自动静音占位），"
          "或整片确是解说型就在章节顶层写 voiceover: lead")
    if dry_run:
        return
    if not sys.stdin.isatty():
        _info("   非交互环境：本次照常发出")
        return
    if not _ask_yes("   仍然按现在的旁白分布继续生成吗？", default=False):
        raise ProjectError("已中止——先收敛旁白镜或改 voiceover 声明，再跑 gen-video")


def _score_gate(project, store, router, args) -> None:
    """合成前的音频剧本闸：整轨还没买过时先报价，不替用户按下按秒计费的那一步。

    `assemble` 走到 `_stage_audio_bed` 会直接调 `stage_score`，而那条路按秒计费、
    单段可达上限秒数，整章一次买断。审阅闸拦不住它——scored 章 `needs_tts` 恒假，
    审阅的 audio 支整个不参与判定。整轨已在盘时 `stage_score` 自身按段幂等复用，
    不重复计费，所以这道闸只在「一次都没买过」时出现。

    `run` 不经过这里：一条龙是用户显式要的全自动，与 gen-video 在那条路上放行
    单笔超阈告警同一口径。"""
    if not project.scored_audio or has_file(project.audio.get("score_file")):
        return
    prov = router.resolve_named("tts", voicebank.CUSTOM_PROVIDER)
    rows = _score_rows(project, strict=False)
    quote = _score_quote(prov, rows)
    if getattr(args, "yes", False):
        _info(f"⚠ 音频剧本整轨尚未生成，本次合成将按秒买断 ≈¥{quote:.2f}（--yes 已授权）")
        return
    loc = getattr(args, "chapter", None) or "<项目>/<章节>"
    raise KinemaError(
        f"⊘ 合成已拦截：音频剧本整轨尚未生成，直接合成会按秒买断整章音频"
        f"（本次 {sum(1 for r in rows if r['burn'])} 段 ≈¥{quote:.2f}）。\n"
        f"   先看分段与报价（零成本）：score --chapter {loc} --dry-run\n"
        f"   确认后生成：score --chapter {loc}\n"
        f"   或就地授权本次买断：assemble --chapter {loc} --yes")


def _bgm_gate(project, store, args) -> None:
    """合成前的 BGM 闸：讲清这一章会得到什么背景乐，只在**有决定要做**时发问。

    三档形态互斥：scored（人声/音乐/音效已在音频剧本那一条轨里）· native（片段自带
    模型原生音）· kenburns/dubbed（恒用曲库 BGM）。前两档各有一个显式加铺开关
    （`scored_bgm` / `native_bgm`）。

    发问只在两种情形，其余只报一行事实——合成是会被反复重跑的节点，每次都拦着
    问一遍等于逼人闭着眼按回车：
      ① 本章要用曲库 BGM 而本机曲库是空的：`local` provider 会退化成合成正弦
         氛围床**并烧进成片**，那是明显的机器音，得在渲之前问要不要先拉曲库；
      ② 本章一条 BGM 都不会有，且从没就此表过态：表态落盘，问过一次不再问。

    `--bgm/--no-bgm` 预先作答即完全不问。"""
    import subprocess

    from . import audio_registry

    def _persist(key: str, value: bool) -> None:
        # 闸持的是只读副本：表态按磁盘现状落盘，合成阶段随后在操作锁内重新装载
        project.data[key] = value
        Project.mutate(project.path, lambda p: p.data.__setitem__(key, value))

    want = getattr(args, "bgm", None)
    scored, native = project.scored_audio, project.native_audio
    burn = project.native_voiceover
    _reject_native_bgm_conflict(project, want=want)
    if want is not None:
        key = "scored_bgm" if scored else "native_bgm" if native else None
        if key is None and not want:
            raise KinemaError(
                "⊘ kenburns/dubbed 的曲库 BGM 不可关闭：这两种模式的成片除旁白外没有别的声音，"
                "关掉 BGM 等于交付一条只有人声的干轨。要无配乐请改用 native 或 scored。")
        if key:
            _persist(key, bool(want))
    use_lib = compose_mod.use_bgm_for(project)
    if not use_lib:
        why = ("音频剧本已含配乐与音效" if scored
               else "配音混烧已把片段原生音降为背景床" if burn else "片段自带模型原生音")
        _info(f"BGM: 本章不叠曲库 BGM（{why}）")
        # 只对「一条 BGM 都不会有」的 native 发问，且只发一次：表态落盘后不再打扰。
        # scored 与混烧那两档各自已有声音来源，不是「没有 BGM」而是「BGM 另有出处」。
        # **只有真问出口的答案才落盘**：非交互时 `_ask_yes` 返回的是缺省值、不是
        # 用户的意思，存下去等于替他做了决定，而且从此再也不会问第二遍
        if native and not burn and "native_bgm" not in project.data:
            # 绑了带音轨的控制视频而没表态 `control_bgm`：源片同区间音轨才是这支舞的配乐，
            # 只提曲库会把人引到一条与动作无关的曲子上
            if control_mod.soundtrack_segments(project):
                _info("   绑定的控制视频源片带音轨：章节写 control_bgm: true 即取源片同区间音轨"
                      "作这一章的配乐（与曲库 BGM 二选一）")
            if not sys.stdin.isatty():
                _info("   要在原生音之下加铺一层曲库 BGM：assemble --bgm（或章节写 native_bgm: true）")
                return
            _persist("native_bgm", _ask_yes(
                "   模型原生音多为环境声——要在它之下再铺一层曲库 BGM 吗？", default=False))
            use_lib = bool(project.data["native_bgm"])
        if not use_lib:
            return
    if project.native_audio and project.data.get("control_bgm"):
        _info("BGM: 取深度捕捉源片同一区间的音轨，不从曲库选曲；未绑定控制视频的镜留静音")
        return
    # 曲库空不空只对 `local` 有意义：配了 ELEVENLABS_API_KEY 时曲子是生成的，
    # 本机有没有库都不影响（provider 解析失败按 local 处理——那是缺省档）
    try:
        prov_name = ModelRouter(store).resolve("music", project.profile)[0].name
    except Exception:  # noqa: BLE001  闸不阻断出片主链
        prov_name = "local"
    if prov_name != "local":
        _info(f"BGM: [{prov_name}] 生成式配乐，无需本地曲库")
        return
    n = audio_registry.bgm_track_count(store=store)
    if n:
        _info(f"BGM: 曲库 {n} 首在盘")
        return
    # 库空 → provider 会退化成合成正弦氛围床并烧进成片
    script = audio_registry.library_root(store=store) / "download.py"
    _info("⚠ BGM: 本机曲库为空——继续合成会烧进一条合成正弦氛围床（明显的机器音）")
    if not script.is_file():
        _info(f"   曲库目录 {script.parent} 下没有 download.py——"
              "放正规授权曲子进 bgm/<情绪>/ 即可，或设 ELEVENLABS_API_KEY")
        return
    # **拉曲库只在人明确点头时发生**：那是一次上百个文件的网络下载，无人值守的
    # 进程（Studio 任务 / CI / 管道）里自作主张开始下载是不能接受的副作用，
    # 故此处不走 `_ask_yes` 的缺省值那条路，先硬判交互性
    if not sys.stdin.isatty():
        _info(f"   补库：python {script}（CC0 起始曲库，免署名可商用）；"
              "或设 ELEVENLABS_API_KEY 走生成式配乐")
        return
    if not _ask_yes(f"   现在运行 {script} 拉起始曲库吗？（CC0·免署名可商用）", default=True):
        _info(f"   跳过。要补库：python {script}；或设 ELEVENLABS_API_KEY 走生成式配乐")
        return
    code = subprocess.call([sys.executable, str(script)])
    n = audio_registry.bgm_track_count(store=store)
    if code == 0 and n:
        _info(f"✓ 曲库已就位：{n} 首")
        return
    _info(f"⚠ 拉取未完成（退出码 {code} · 现有 {n} 首）——本次仍会用合成氛围床")


def cmd_assemble(args):
    """合成节点：字幕 → 背景乐 → 合成成片（静图形态）。动态形态先 gen-video，再 assemble --dubbed|--native。
    审阅闸：正式成片须全部镜过审；未过审用 animatic 看零成本草稿，或加 --draft 强出草稿。"""
    store = ConfigStore.load(args.config)
    path = Path(_project_path(args))
    # 三道闸持只读副本：BGM 闸可能等用户回答，不能占着操作锁，也不能拿这份副本写盘
    project = Project.load(path)
    _apply_aspect_args(project, args)
    router = ModelRouter(store, force_mock=args.mock)
    ensure_tools()
    missing = _assemble_review_gate(project)                  # 审阅闸：未过审拦截正式成片
    if missing and not getattr(args, "draft", False):
        by_stage: dict = {}
        for sid, stage in missing:
            by_stage.setdefault(stage, []).append(str(sid))
        loc = getattr(args, "chapter", None) or "<项目>/<章节>"
        detail = " · ".join(f"{stage} 未过审 镜{','.join(ids)}" for stage, ids in by_stage.items())
        first_stage = missing[0][1]
        tips = [f"   看板：review list --chapter {loc}",
                f"   批准：review set --chapter {loc} --shots <镜号> --stage {first_stage} --state done"]
        if "audio" in by_stage and not (project.audio or {}).get("narration_file"):
            # audio 缺席常是「产物未登记」而非「待审」（典型：native 章节用
            # `-m a` 临时按 kenburns 渲、从没跑过 tts）——「批准一个没登记的
            # 产物」无法执行，须先给生成入口。登记字段缺失不等于从未合成，
            # 措辞只陈述字段事实
            tips.insert(1, f"   旁白轨未登记（audio.narration_file 缺失）：先 "
                           f"`tts --chapter {loc}` 再过审——已合成过的重跑即重新登记"
                           "（native 章节本无旁白轨，临时切模式渲要先补配音）")
        raise KinemaError(
            f"⊘ 合成已拦截：{len(missing)} 项未过审（{detail}）——正式成片须全部镜过审。\n"
            + "\n".join(tips) + "\n"
            f"   零成本草稿：animatic --chapter {loc}\n"
            f"   强出草稿成片：assemble --chapter {loc} --draft")
    if missing:                                               # --draft 逃生舱：出草稿但明确标注
        _info(f"⚠ --draft：{len(missing)} 项未过审，输出为草稿成片（正式交付前请过审）")
    _score_gate(project, store, router, args)
    _bgm_gate(project, store, args)
    # 与 run 同一把章节操作锁：assemble 也是「多阶段改产物」的整段操作；锁内重新装载，
    # 问话期间的外部写入不被闸持的旧副本覆盖
    from .locking import op_lock
    with op_lock(path, kind="assemble"):
        project = Project.load(path)
        _apply_aspect_args(project, args)
        stage_subtitle(project, store, router, profile=args.profile)
        _stage_audio_bed(project, store, router, profile=args.profile,
                         force=args.force,
                         concurrency=getattr(args, "concurrency", None))
        stage_compose(project, store, router, profile=args.profile, out=args.out,
                      force=args.force)
    _print_summary(project)


def _print_summary(project):
    cost = project.data.get("cost", {})
    cur = cost.get("currency", "CNY")
    line = " · ".join(f"{k}={v}" for k, v in cost.items() if k != "currency")
    print("\n✓ 完成")
    for asp in project.aspects:
        out = project.output.get(asp)
        if out:
            print(f"   [{asp}] {out}")
    print(f"   时长: {project.total_duration():.2f}s · 分镜: {len(project.shots)} 镜")
    if line:
        print(f"   成本({cur}): {line}")


# ---------- 工具 ----------
def cmd_studio(args):
    import os
    import signal
    from .studio import serve
    from .studio.server import other_studio_pids, running_instance
    from .workspace import find_workspace
    # 长驻进程必须用 shared（按 mtime 自失效）：load() 会把启动瞬间的配置钉死在
    # serve 的闭包里——运行期新增画风后项目建得成（写路径走 _fresh_store），
    # 章节页却因冻结快照解不出 profile 整页 500
    store = ConfigStore.shared(args.config)
    # 默认自动定位仓库根 project/（也兼容 KINEMA_WORKSPACE），不依赖当前目录
    # 所有入口统一经过 find_workspace：仓库根/engine 根参数必须归一到仓库根
    # project/，否则 Studio 的项目清单与 CLI/存储层会各看一套数据。
    ws = str(find_workspace(getattr(args, "workspace", None)))
    ws_root = Path(ws).resolve()
    inst = running_instance(ws_root)

    def _residual_warn():
        pids = other_studio_pids(exclude=inst["pid"] if inst else None)
        if pids:
            print(f"⚠ 另有 {len(pids)} 个 Studio 进程在跑（pid {', '.join(map(str, pids))}）"
                  "——多为历史会话残留，全部清理：pkill -f 'kinema studio'")

    # --stop / --status：只管控，不启动
    if getattr(args, "stop", False):
        if inst:
            try:
                os.kill(inst["pid"], signal.SIGTERM)
                print(f"⊘ 已停止本工作区 Studio（pid {inst['pid']} · {inst['url']}）")
            except OSError as e:
                print(f"停止失败: {e}")
        else:
            print("（本工作区没有在跑的 Studio 实例）")
        _residual_warn()
        return
    if getattr(args, "status", False):
        print(f"本工作区 Studio: {inst['url']}（pid {inst['pid']}）" if inst
              else "本工作区 Studio: 未运行")
        _residual_warn()
        return

    # 单例：已有实例直接复用（不重起）；要换端口/重启用 --restart
    if inst and not getattr(args, "restart", False):
        print(f"♻ Studio 已在运行，直接访问：{inst['url']}")
        print("   （重启/换端口：studio --restart｜停止：studio --stop）")
        _residual_warn()
        return
    if inst and getattr(args, "restart", False):
        try:
            os.kill(inst["pid"], signal.SIGTERM)
            print(f"↻ 已停旧实例（pid {inst['pid']}），重启中…")
            import time
            time.sleep(0.6)                   # 让旧实例释放端口再绑定
        except OSError:
            pass
    _residual_warn()

    root = args.root
    if root == ".":
        # 默认根设为工作区父目录，成片画廊与项目列表使用同一份 project/ 数据。
        # 显式 --root 时才允许用户指定额外的片库扫描范围。
        root = str(ws_root.parent)
    serve(root=root, port=args.port, store=store, workspace=ws,
          config=getattr(args, "config", None))


def cmd_doctor(args):
    print(f"kinema {__version__}")
    try:
        ensure_tools()
        import subprocess
        ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        print("  ffmpeg:", ver.stdout.splitlines()[0])
    except KinemaError as e:
        print("  [x]", e)
        return
    # 孤儿 ffmpeg（父进程被杀后无人认领·会一直空烧 CPU）：doctor 只报告不动手，
    # 清理走 `studio` 启动自动收割，或按提示手动 kill
    from .ffmpeg import find_orphan_ffmpeg
    orphans = find_orphan_ffmpeg()
    if orphans:
        print(f"  ⚠ 发现 {len(orphans)} 只孤儿 ffmpeg（父进程已死仍在烧 CPU）：")
        for o in orphans:
            print(f"      pid {o['pid']}: …{o['cmd'][-72:]}")
        print(f"      清理：kill -9 {' '.join(str(o['pid']) for o in orphans)}"
              "（或启动 studio 自动收割）")
    store = ConfigStore.load(args.config)
    print(f"  配置源: {store.source}")
    if store.fallback:
        # 内置精简配置是缩水子集：画风目录/字幕样式都不全，Studio 新建项目会
        # 悄悄少画风（kn-anime3d 46→30 时只剩 1 条路线）——必须点名后果与解法
        why = ("缺 PyYAML（models.yaml 在盘上但读不了）"
               if store.fallback == "missing-pyyaml" else "未找到 config/models.yaml")
        n = len(store.data.get("profiles") or {})
        print(f"  [!] 正在用内置精简配置：{why}——仅 {n} 个内置画风在服务，"
              "Studio 新建项目画风不全。修复: pip install PyYAML 后重启 studio")
    from .storage import get_storage, load_storage_config
    scfg = load_storage_config()
    print(f"  存储后端: {scfg['backend']}（{scfg['source']}）")
    if scfg["backend"] == "mysql":
        try:
            st = get_storage(find_workspace(getattr(args, 'workspace', None)))
            print(f"  MySQL: {st.describe()} · {st.counts()}")
        except Exception as e:  # noqa: BLE001
            print(f"  [x] MySQL 不可用: {e}")
    print(f"  默认 profile: {store.default_profile}")
    print(f"  profiles: {', '.join((store.data.get('profiles') or {}).keys())}")
    provs = store.data.get("providers") or {}
    ready = [n for n, c in provs.items() if c.get("status") == "ready"]
    planned = [n for n, c in provs.items() if c.get("status") != "ready"]
    print(f"  providers ready: {', '.join(ready)}")
    print(f"  providers planned: {', '.join(planned)}")
    # 本地音乐库：ELEVENLABS_API_KEY 为空时 BGM 走这里；库空会退化为正弦氛围床
    if not store.secret("ELEVENLABS_API_KEY", required=False):
        try:
            from .providers.music.local import LocalMusicProvider
            lib = LocalMusicProvider(store)
            n_tracks = len(list(lib.root.rglob("*.mp3"))) \
                if lib.root and lib.root.is_dir() else 0
            if n_tracks:
                print(f"  音乐库: {n_tracks} 首（{lib.root}）")
            else:
                print("  [!] 音乐库为空且未配 ELEVENLABS_API_KEY——BGM 将退化为合成"
                      "氛围床（正弦波）。运行 `python music/download.py` 拉起始曲库。")
        except Exception:  # noqa: BLE001
            pass
    # 可选依赖体检（INFRA-6，照上面音乐库降级提示的范式）。如实说明：
    # 角色一致性走「consistency scan 抽帧+配设定图 → 交指挥层多模态判定」，
    # 引擎一行相似度都不算——ffmpeg 只会抽帧，ssim/psnr 对同角色换姿态无判别力。
    # 用 find_spec 探测而不 import：torch 一 import 就是数秒与数百 MB 内存，
    # 且本引擎任何代码路径都不消费它们（装了也不会被调用）。
    try:
        import importlib.util as _ilu
        _vis = [n for n in ("PIL", "numpy", "open_clip")
                if _ilu.find_spec(n) is not None]
    except Exception:  # noqa: BLE001  探测失败不该影响体检其余项
        _vis = []
    print(f"  可选依赖 vision: {'、'.join(_vis) if _vis else '未安装（不影响任何功能）'}"
          " —— 一致性校验 = consistency scan 抽帧产料 + 指挥层判定，引擎不算分数")
    # 深度捕捉只在 doctor 报，**不进 `setup --check` 的 checks**：那一份任何一项
    # 为假就把 ready 打成 false，而 AGENTS.md 把 ready=true 当作「直接开工」的信号——
    # 一个每台新机器都缺的可选栈会永久卡住那个信号。同 vision extras 的先例。
    try:
        _ready, _notes = control_mod.available()
    except Exception:  # noqa: BLE001  探测失败不该影响体检其余项
        _ready, _notes = False, ["探测失败"]
    print("  可选依赖 control: " + ("已就绪" if _ready else "、".join(_notes))
          + " —— 深度捕捉（实拍运动 → 参考视频），本机 CPU 推理、零 API 成本；"
            "不装不影响其他任何功能")
    # 未配单价的 provider：`budget` 与 `budget_per_call` 两道闸都按台账算，
    # 单价为 0 = 这家的花费完全不入账，闸对它全程不生效。必须在体检里点名，
    # 否则「台账缺这一行」与「没花钱」无法区分。
    _PRICE_KEYS = ("price_per_image", "price_per_second", "price_per_kchar", "price_per_min")
    nopay = [k for k, c in ((store.data.get("providers") or {}).items())
             if c.get("status", "ready") == "ready" and c.get("kind") != "music"
             and c.get("impl", k) != "agent"   # agent 工单零成本是设计而非漏配
             and not any(float(c.get(x) or 0) > 0 for x in _PRICE_KEYS)]
    if nopay:
        print(f"  [!] 未配单价（成本闸对它们不生效，花费不入台账）: {'、'.join(sorted(nopay))}"
              " —— 按控制台实时价填 config/models.yaml 的 price_* 或在配置中心填")
    # 生效的生图路由：三级解析（显式激活 > agent 声明 > 默认）结果不打出来，
    # 「为什么没调 API / 为什么还在要 key」就无从排查。
    try:
        from .models import image_route
        _rt = image_route(store)
        _src = {"explicit": "models 显式激活", "agent": "agent 原生声明（KINEMA_AGENT_IMAGEGEN）",
                "default": "默认链"}[_rt["source"]]
        print(f"  生图路由: {_rt['provider']}（{_src}）"
              + ("——工单模式，不检测生图密钥" if _rt["source"] == "agent" else ""))
    except Exception:  # noqa: BLE001 路由算不出不该挡体检
        pass
    # 覆盖层生效面：命令行看到的必须与网页是同一件事，否则网页侧的修改在
    # 命令行无迹可循——它凌驾于上面打印的 providers 之上
    ov = getattr(store, "overlay", None)
    if ov:
        print(f"  模型配置覆盖层: {ov['path']}"
              f"（{len(ov['providers'])} 个连接段 · {len(ov['defaults'])} 个激活项"
              f"{'：' + '、'.join(ov['defaults']) if ov['defaults'] else ''}）"
              " —— 详见 `config show`")
    print("  提示: 无 API key 时用 `run --mock` 端到端离线跑通。")


# ---------- Agent / Skill 控制平面 ----------
def cmd_agent_catalog(args):
    from .agent_system import AgentCatalog
    catalog = AgentCatalog.load()
    items = catalog.all()
    if getattr(args, "kind", None):
        items = [item for item in items if item["kind"] == args.kind]
    if getattr(args, "status", None):
        items = [item for item in items if item["status"] == args.status]
    result = {
        "schema_version": 2,
        "catalog_version": catalog.version,
        "manifest_digest": catalog.manifest_digest,
        "skills": items,
    }
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"Agent catalog {catalog.version} · {len(items)} skills · {catalog.manifest_digest}")
    for item in items:
        profiles = ",".join(item.get("profiles") or []) or "—"
        print(f"  {item['id']:<22} {item['kind']:<10} {item['status']:<9} "
              f"profile={profiles} · {item['digest']}")
    return 0


def cmd_agent_route(args):
    from .agent_system import AgentCatalog
    project_skill = getattr(args, "project_skill", None)
    if getattr(args, "project", None):
        if project_skill:
            raise KinemaError("--project 与 --project-skill 二选一")
        ws = Workspace.open(getattr(args, "workspace", None), create=False)
        project_skill = ws.get_project(args.project).data.get("skill")
    decision = AgentCatalog.load().route(
        project_skill=project_skill,
        skill=getattr(args, "skill", None),
        profile=getattr(args, "profile", None),
    )
    result = decision.as_dict()
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['skill']}  ← {result['source']}")
        print(f"  {result['reason']}")
        print(f"  catalog {result['catalog_version']} · {result['digest']}")
    return 0


def cmd_agent_doctor(args):
    from .agent_system import agent_doctor
    result = agent_doctor()
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Agent doctor · catalog {result.get('catalog_version') or '不可用'}")
        for item in result["findings"]:
            print(f"  {'✓' if item['ok'] else '✗'} {item['id']}: {item['detail']}")
    return 0 if result["ok"] else 1


def cmd_agent_assets(args):
    from .agent_assets import AgentAssetError, check_assets, compile_assets
    try:
        result = compile_assets() if args.asset_action == "compile" else check_assets()
    except AgentAssetError as exc:
        result = {"ok": False, "errors": [str(exc)]}
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"✓ Agent assets {args.asset_action}: {result['skills']} skills · "
              f"catalog {result['catalog_version']} · {result['manifest_digest']}")
    else:
        print("✗ Agent assets 校验失败:")
        for error in result.get("errors") or ["未知错误"]:
            print(f"  - {error}")
    return 0 if result["ok"] else 1


def cmd_agent_contract(args):
    from .prompt_contract import AgentContractRegistry
    result = AgentContractRegistry.load().describe(args.contract_name)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['name']} · {result['digest']}")
        print(json.dumps(result["contract"], ensure_ascii=False, indent=2))
    return 0


def cmd_agent_context(args):
    from .agent_gateway import AgentGateway
    result = AgentGateway.open(getattr(args, "workspace", None)).context(args.chapter, args.task)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['chapter']} · {result['task']} · {result['revision']}")
        print(f"  skill={result['binding']['skill']} · profile={result['binding']['profile']}"
              f" · shots={len(result['shots'])}")
    return 0


def cmd_agent_plan(args):
    from .agent_gateway import AgentGateway, load_plan
    as_json = bool(getattr(args, "json", False))
    chapter = ""
    try:
        plan = load_plan(args.file)
        chapter = str(plan.get("chapter") or "")
        gateway = AgentGateway.open(getattr(args, "workspace", None))
        result = gateway.apply(plan) if args.plan_action == "apply" else gateway.validate(plan)
    except KinemaError as e:
        if not as_json:
            raise
        # --json 是机器通道：拒绝理由走 stdout，形状与 `agent assets --json` 一致，退出码不变
        print(json.dumps({"ok": False, "chapter": chapter, "action": args.plan_action,
                          "errors": [str(e)]}, ensure_ascii=False, indent=2))
        return 1
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        verb = "已应用" if args.plan_action == "apply" else "校验通过"
        print(f"ChapterPlan {verb} · {result['chapter']} · {result['plan_digest']}")
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


def cmd_agent_explain(args):
    from .agent_gateway import AgentGateway
    result = AgentGateway.open(
        getattr(args, "workspace", None), getattr(args, "config", None)).explain(
        args.chapter, args.shot, args.stage)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        state = "stale" if result["stale"] else "current"
        print(f"{result['chapter']} · 镜{result['shot']} · {result['stage']} · {state}")
        for reason in result["stale_reasons"]:
            print(f"  - {reason}")
    return 0


# ---------- 模型配置覆盖层（网页配置中心的命令行对等面）----------
def _config_store(args):
    """配置中心专用的加载：每次都新读，绝不复用长驻实例。

    配置改完立刻要看见效果，而 Studio 持有的是进程级单例——复用它就会出现
    「保存成功但显示的还是旧值」，且表象上无从判断这是缓存所致。
    """
    return ConfigStore.load(getattr(args, "config", None))


def _config_ws(args):
    """工作区根（数据库同步层用；找不到就只写文件，不算失败——文件才是运行时真源）。"""
    try:
        ws = find_workspace(getattr(args, "workspace", None))
        return ws if ws.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


def _config_now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def cmd_config_show(args):
    from . import config_overlay as ovl
    ws = _config_ws(args)
    if getattr(args, "pull", False):
        ovl.pull(ws)
    store = _config_store(args)
    view = ovl.config_view(store, ws)
    if getattr(args, "json", False):
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return
    print(f"配置真源: {view['source']}")
    ov = view["overlay"]
    print(f"覆盖层  : {view['overlay_path'] or '（已关闭）'}"
          + (f" · {len(ov['providers'])} 个连接段 · {len(ov['defaults'])} 个激活项"
             if ov else " · 未配置（全部跟随配置文件）"))
    print("\n激活项（defaults.providers）")
    for c in view["capabilities"]:
        cap = c["id"]
        src = "覆盖层" if view["activated_by"][cap] == "overlay" else "配置文件"
        print(f"  {cap:8} {c['zh']:5} → {view['active'][cap] or '—':16} [{src}]")
    dev = view["profile_deviations"]
    if dev:
        # 解析链的第一跳是 profile 自己的能力块，优先级高于上面的全局激活项。
        # 不打这一行，全局激活项对这些画风不生效的原因就无从解释。
        print(f"  ⚠ {len(dev)} 个画风自带偏离项，不受上面的激活项影响："
              + "、".join(f"{d['profile']}({d['capability']}={d['provider']})"
                          for d in dev[:6])
              + ("…" if len(dev) > 6 else ""))
    print("\n连接段")
    for p in view["providers"]:
        mark = "*" if p["overridden"] else " "
        key = p["key"]["state"]
        print(f" {mark}{p['alias']:17} {p['kind'] or '?':6} impl={p['impl']:14}"
              f" {p['status']:8} key={key:6} {p['base_url'] or ''}")
        if p["overridden"]:
            print(f"    ↳ 本机覆盖: {', '.join(p['overridden'])}")
    print("\n  * = 该别名有本机覆盖 · key: env=环境变量 local=本机密钥文件 "
          "file=secrets.yaml unset=未设 none=无需密钥")


def cmd_config_set(args):
    from . import config_overlay as ovl
    fields = {}
    for item in args.set or []:
        if "=" not in item:
            raise ConfigError(f"--set 要写成 字段=值（收到 {item!r}）")
        k, v = item.split("=", 1)
        fields[k.strip()] = v.strip()
    for k in args.reset or []:
        fields[k.strip()] = None
    if not fields and not args.reset_all:
        raise ConfigError("没有要改的东西：用 --set 字段=值 / --reset 字段 / --reset-all")
    doc = ovl.save(providers={args.provider: None if args.reset_all else fields},
                   ws_root=_config_ws(args), now=_config_now(),
                   explicit=getattr(args, "config", None))
    entry = (doc.get("providers") or {}).get(args.provider) or {}
    print(f"✓ {args.provider}: "
          + (", ".join(f"{k}={v}" for k, v in entry.items()) if entry
             else "已清空覆盖，回落 config/models.yaml"))


def cmd_config_activate(args):
    from . import config_overlay as ovl
    store = _config_store(args)
    alias = args.provider
    if alias and alias != "-":
        conn = store.provider_conn(alias)         # 未知别名在这里就抛，不等到发请求
        kind = conn.get("kind")
        if kind and kind != args.capability:
            raise ConfigError(
                f"provider '{alias}' 是 {kind} 能力，不能激活给 {args.capability}")
        # 落盘用**归一后的新名**：旧名兼容位只该在读的时候认，写进覆盖层就等于
        # 让它沉淀下去，此后每次加载都要再翻译一次、还会随数据库同步到别的机器
        alias = conn.get("name", alias)
    else:
        alias = None
    ovl.save(defaults={args.capability: alias}, ws_root=_config_ws(args),
             now=_config_now(), explicit=getattr(args, "config", None))
    print(f"✓ {args.capability} → {alias or '跟随 config/models.yaml'}")


def cmd_config_secret(args):
    """写一个密钥到本机密钥文件。**值只从标准输入读，绝不做命令行参数**——
    命令行参数会进 shell 历史与进程列表。"""
    from . import config_overlay as ovl
    import getpass
    if args.clear:
        r = ovl.write_secret(args.env, None, explicit=getattr(args, "config", None))
    else:
        val = getpass.getpass(f"{args.env} = ")     # 不回显
        if not val.strip():
            raise ConfigError("未输入内容，已取消（要清除请用 --clear）")
        r = ovl.write_secret(args.env, val, explicit=getattr(args, "config", None))
    print(f"✓ {r['env']}: {'已清除' if r['state'] == 'unset' else '已写入本机密钥文件'}"
          "（该文件不入库、不提交、永不回显）")


def cmd_config_test(args):
    from . import config_overlay as ovl
    store = _config_store(args)
    aliases = [args.provider] if args.provider else sorted(store.data.get("providers") or {})
    bad = 0
    for a in aliases:
        r = ovl.probe(store, a)
        print(f"{'✓' if r['ok'] else '✗'} {a}")
        for c in r["checks"]:
            if not c["ok"] or args.verbose:
                print(f"    {'·' if c['ok'] else '✗'} {c['name']}"
                      + (f" — {c['detail']}" if c["detail"] else ""))
        bad += 0 if r["ok"] else 1
    print(f"\n{len(aliases) - bad}/{len(aliases)} 项通过"
          "（零成本自检：只查解析层，一个生成请求都不发）")


# ---------- 审阅状态机 / 版本栈 ----------
def _load_video(args):
    """加载要操作的章节/视频文档（复用 --project/--chapter 定位）。"""
    return Project.load(_project_path(args))


def _parse_shots(project, expr):
    if not expr or str(expr).lower() == "all":
        return project.shots
    want = {x.strip() for x in str(expr).split(",") if x.strip()}
    out = [s for s in project.shots if str(s.get("id")) in want]
    if not out:
        raise KinemaError(f"没有匹配 --shots {expr} 的分镜")
    return out


def cmd_review_list(args):
    project = _load_video(args)
    shots = project.shots
    summ = review.summary(shots, audio_of=lambda s: voicecast.has_audio_stage(s, project))
    print(f"审阅看板 · {project.id} · {len(shots)} 镜（弃用 {summ['omitted']}）")
    if project.data.get("animatic"):
        anim = review.get_state(project.data, "animatic")
        print(f"  全片样片 animatic: {review.label(anim)}"
              f"（{(project.data['animatic'].get('at') or '')[:16]}）")
    for stage in review.STAGES:
        counts = " · ".join(f"{review.label(k)} {v}" for k, v in sorted(summ[stage].items()))
        print(f"  {stage:<5} | {counts or '—'}")
    print()
    for s in shots:
        if args.state:
            hit = (args.state == "omt" and review.is_omitted(s)) or \
                  any(review.get_state(s, st) == args.state for st in review.STAGES)
            if not hit:
                continue
        head = f"  镜{s.get('id'):>3}" + ("  ⊘弃用" if review.is_omitted(s) else "")
        cells = []
        for st in review.STAGES:
            if st == "audio" and not voicecast.has_audio_stage(s, project):
                cells.append("audio:—")
                continue
            cell = f"{st}:{review.label(review.get_state(s, st))}"
            if versioning.history(s, st):
                cell += f"·v{versioning.current_version(s, st)}"
            cells.append(cell)
        print(f"{head}  {'  '.join(cells)}   「{(s.get('narration') or '')[:18]}」")
        for st in review.STAGES:
            if review.get_note(s, st):
                print(f"        ↳ {st} 意见: {review.get_note(s, st)}")


def cmd_review_set(args):
    """表态按磁盘现状落盘（`Project.mutate`）：长任务运行期间照常可用，装载后引擎
    刚回填的产物字段不会被这里的旧副本写回。"""
    path = _project_path(args)
    stage = args.stage or ("shot" if args.state == "omt" else None)
    if stage in review.CHAPTER_STAGES:     # 章节级产物（animatic 全片样片）表态
        Project.mutate(path, lambda p: review.set_state(p.data, stage, args.state, note=args.note))
        print(f"✓ {stage} → {review.label(args.state)}"
              + (f"（意见: {args.note}）" if args.note else ""))
        if stage == "animatic" and args.state == "done":
            print("   节奏审通过。正式渲染: gen-video --approved-only（只烧已批准的镜）")
        return
    if stage is None:
        raise KinemaError("请用 --stage image|audio|clip 指定产物（--state omt 弃用整镜可省略）")
    if not args.shots:
        raise KinemaError("请用 --shots 指定镜号（如 1,3 或 all）")

    def fn(project):
        shots = _parse_shots(project, args.shots)
        for s in shots:
            review.set_state(s, stage, args.state, note=args.note)
        return len(shots)
    n = Project.mutate(path, fn)
    print(f"✓ 已更新 {n} 镜 · {stage} → {review.label(args.state)}"
          + (f"（意见: {args.note}）" if args.note else ""))
    if args.state == "retake":
        print("   下次运行对应生成阶段将强制重生，旧版自动归档进版本栈。")
    elif args.state == "done":
        print("   已锁定：--force 也不会覆盖；要重生请先置为 retake。")
    elif args.state == "omt":
        print("   整镜已弃用：不进时间轴/字幕/成片。恢复: review set --stage shot --state todo")


def cmd_versions_list(args):
    project = _load_video(args)
    shots = _parse_shots(project, args.shots) if args.shots else project.shots
    shown = 0
    for s in shots:
        stages = [(st, versioning.history(s, st)) for st in versioning.STAGES]
        if not any(h for _, h in stages):
            continue
        shown += 1
        print(f"镜 {s.get('id')}:")
        for st, hist in stages:
            if not hist:
                continue
            print(f"  {st} · 当前 v{versioning.current_version(s, st):03d}")
            for e in hist:
                print(f"    v{e['v']:03d}  {e.get('at', '')}  "
                      f"{e.get('reason', '')}  [{len(e.get('files') or {})} 文件]")
    if not shown:
        print("（暂无归档版本——首次生成即 v1，重生成/回滚时才产生归档）")


@_op_locked("versions-rollback")
def cmd_versions_rollback(args):
    project = _load_video(args)
    target = next((s for s in project.shots if str(s.get("id")) == str(args.shot)), None)
    if target is None:
        raise KinemaError(f"找不到镜 {args.shot}")
    versioning.rollback(project, target, args.stage, args.to)
    # 配音回滚要重探时长（kenburns 的窗口就是配音长度）。片段回滚不动 dur：
    # 那是已经为当前时间轴买下的画面秒数，换用哪一版底片不改变买了多少秒；
    # 按被换回来的文件重探，等于把容器多出的那一帧写回读侧，dubbed 的 ceil
    # 会在下一次重烧时进位多买一秒。片段与窗口的长度差由 fit_clip 收敛。
    if args.stage == "audio":
        main = target.get("audio_file")
        if has_file(main):
            target["dur"] = round(probe_duration(main), 2)
    # 画布内容换成历史版 → 旧一致性判定作废（与 Studio 回滚同一纪律；audio 空操作）
    consistency_mod.invalidate(target, args.stage)
    if args.stage == "image" and lineage.retake_clip_for_image(target) == "retake":
        print(f"   镜 {args.shot} 的片段按被换掉的画面生成 → clip 置 retake")
    review.set_state(target, args.stage, "wfa", note=f"回滚至 v{args.to}")
    project.save()
    print(f"✓ 镜 {args.shot} 的 {args.stage} 已回滚至 v{args.to} 内容（原当前版已归档，谱系完整）")
    if args.stage == "audio":
        print("   提示: 重跑 tts 以重拼旁白整轨（已有音频不重合成，只重新拼接回填时间戳）")
    else:
        print("   提示: 重跑 assemble/compose --force 让成片使用回滚后的画面")


# ---------- 宫格候选点选：候选 → 人眼定稿上画布 ----------
@_op_locked("pick")
def cmd_pick(args):
    project = _load_video(args)
    target = next((s for s in project.shots if str(s.get("id")) == str(args.shot)), None)
    if target is None:
        raise KinemaError(f"找不到镜 {args.shot}")
    r = candidates_mod.pick(project, target, int(args.use),
                            approve=not args.keep_open)
    print(f"✓ 镜 {r['shot']} 已定稿候选 #{r['no']} → {Path(r['canvas']).name}"
          + (f"（原画布已归档 {r['archived']}）" if r['archived'] else ""))
    if r["state"] == "done":
        print("   分镜图已通过锁定（宫格点选=人眼定稿）；要换选直接再 pick 其他编号。")
    else:
        print("   已落待审（--keep-open）；确认后 review set --state done 锁定。")


# ---------- 草稿两段式 ----------
def cmd_animatic(args):
    """全片 Ken Burns 样片（animatic）：静图+配音+字幕合成，零视频成本过**节奏审**。
    通过后批准的镜再走 gen-video --approved-only 烧 Seedance——先便宜后昂贵。"""
    store = ConfigStore.load(args.config)
    project = Project.load(_project_path(args))
    _apply_aspect_args(project, args)
    ensure_tools()
    # 转场镜按设计就没有分镜图（生图/配音/图生视频全跳过，字卡由合成段本地渲染），
    # 判据必须与 compose.build 的同类预检同源——漏了 is_transition 会让**任何加过
    # 转场的章节都跑不了 animatic**（零成本节奏审节点直接不可达）。
    no_img = [s.get("id") for s in project.active_shots
              if not transitions_mod.is_transition(s)
              and not has_file(project.image_for(s, project.aspect))]
    if no_img:
        raise KinemaError(f"缺分镜图的镜: {no_img}。先 gen-image（候选模式还需 pick 定稿）")
    no_dur = [s.get("id") for s in project.active_shots if float(s.get("dur") or 0) <= 0]
    if no_dur:
        raise KinemaError(f"缺时长的镜: {no_dur}。先跑 tts 回填真实时长，"
                             "或在分镜脚本里填预估 dur（节奏审需要时长）")
    prof = args.profile or project.profile
    effects = store.effects_for(prof, project.effects)
    sub_cfg = _sub_cfg(store, project, prof)
    target_motion = project.motion
    files = {}
    _step(f"草稿两段式 · 全片 animatic（Ken Burns 样片，零视频成本）· 比例 {project.aspects}")
    # 强制静图运镜：走 override_runtime（与 --motion 同一套机制，save 时自动还原）
    # ——绝不手写 try/finally 还原，还原纪律只此一份
    project.override_runtime("motion", "kenburns")
    # scored 章节的音轨还没生成时降级成 tracks：animatic 是**零成本**节奏审节点，
    # 不能因为一条按秒计费的音轨没买就整个不可达（缺 narration/bgm 时合成段本来
    # 就出静音片，节奏该看的是画面时长）。音轨已在盘则照常用，样片更接近成片。
    if project.scored_audio and not has_file(project.audio.get("score_file")):
        project.override_runtime("audio_mode", "tracks")
        _info("ⓘ 音频剧本尚未生成，本次样片按无音轨渲染（节奏审看画面时长）——"
              "要带声音先跑 score")
    for asp in project.aspects:
        f = compose_mod.build(project, store, aspect=asp, effects=effects,
                              sub_cfg=sub_cfg, force=args.force, variant="animatic")
        files[asp] = f
        _info(f"[{asp}] 样片: {f} · {probe_duration(f):.2f}s")
    from datetime import datetime
    project.data["animatic"] = {"files": files, "motion_target": target_motion,
                                "at": datetime.now().isoformat(timespec="seconds")}
    review.set_state(project.data, "animatic", "wfa")
    project.save()
    print(f"\n✓ 全片样片已生成（{len(files)} 个比例）→ 待审")
    print("   过节奏审：太拖的镜 review set --state omt 弃用 · 要改的 retake · 好的 done")
    print("   样片本身表态：review set --stage animatic --state done（或 Studio 章节页点批）")
    if project.uses_seedance:
        print("   通过后正式渲染：gen-video --approved-only（只烧已批准的镜）→ compose")


def cmd_milestones(args):
    """三级里程碑（「先首镜」的全片化）：首镜 → 首集 → 全片。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    chapters = s.list_chapters()
    if not chapters:
        print(f"{s.pid}: 还没有章节。chapter new {s.pid} --title '<本集剧情短标题>'")
        return
    first = ws.store.load_chapter(s.pid, chapters[0]["id"]) or {}
    fshots = [x for x in first.get("shots") or []
              if (x.get("review") or {}).get("shot", {}).get("state") != "omt"]
    fs = fshots[0] if fshots else None
    m1 = "done" if (fs and review.get_state(fs, "image") == "done") else \
         ("wfa" if (fs and has_file(fs.get("image"))) else "todo")
    m2 = "done" if chapters[0]["status"] == "rendered" else \
         ("wfa" if first.get("animatic") else "todo")
    rendered = sum(1 for c in chapters if c["status"] == "rendered")
    m3 = "done" if rendered == len(chapters) else ("wfa" if rendered else "todo")
    ico = {"done": "✓", "wfa": "◐", "todo": "○"}
    print(f"里程碑 · {s.pid} 「{s.data.get('title')}」")
    print(f"  {ico[m1]} M1 首镜 —— 第一章首镜分镜图"
          + {"done": "已通过锁定", "wfa": "已生成待审", "todo": "未生成"}[m1])
    print(f"  {ico[m2]} M2 首集 —— 第一章"
          + {"done": "已出成片", "wfa": "已有全片样片(animatic)", "todo": "未合成"}[m2])
    print(f"  {ico[m3]} M3 全片 —— {rendered}/{len(chapters)} 章已渲染")
    for c in chapters:
        cdata = ws.store.load_chapter(s.pid, c["id"]) or {}
        anim = review.get_state(cdata, "animatic") if cdata.get("animatic") else None
        print(f"      · {c['id']:<8} [{c['status']:<8}] 分镜 {len(cdata.get('shots') or []):>2}"
              + (f" · 样片 {review.label(anim)}" if anim else ""))
    nxt = {"todo": f"先首镜: gen-image --only 1 --chapter {s.pid}/{chapters[0]['id']}",
           "wfa": "首镜过审: review set --shots 1 --stage image --state done"}
    if m1 != "done":
        print(f"  下一步 → {nxt[m1]}")
    elif m2 != "done":
        print(f"  下一步 → 首集跑通: animatic 过节奏审 → 渲染 --chapter {s.pid}/{chapters[0]['id']}")
    elif m3 != "done":
        print("  下一步 → 批量续集：逐章重复「分镜→样片→批准→渲染」")


# ---------- 资产血缘：就绪度 + 过期传播 + 跨项目复用 ----------
def _lineage_targets(args):
    """要检查的章节 Project 列表：--chapter 单章 / 位置参数项目id 全章节。"""
    if getattr(args, "chapter", None):
        return [_load_video(args)]
    if getattr(args, "project_id", None):
        ws = Workspace.open(args.workspace, create=False)
        s = ws.get_project(args.project_id)
        return [Project.load(s.get_chapter_path(ch["id"])) for ch in s.chapters]
    raise KinemaError("请指定 --chapter 项目id/章节id 或位置参数 <项目id>（全部章节）")


def cmd_lineage_status(args):
    for project in _lineage_targets(args):
        print(f"血缘 · {project.id}")
        any_row = False
        for s in project.shots:
            if review.is_omitted(s):
                continue
            ok, missing = lineage.readiness(project, s)
            stale = lineage.stale_refs(s, "image") or s.get("stale_refs")
            clip_stale = lineage.stale_refs(s, "clip")
            words = [st for st in lineage.TEXT_STAGES if lineage.stale_text(s, st)]
            if ok and not stale and not clip_stale and not words:
                continue
            any_row = True
            print(f"  镜{s.get('id'):>3}"
                  + (f"  ⊘ 缺设定图: {', '.join(missing)}" if missing else "")
                  + (f"  ⚠ 设定已更新: {', '.join(stale)}"
                     + ("（已锁定，需人工裁决）" if review.is_locked(s, 'image') else "")
                     if stale else "")
                  + (f"  ⚠ 片段出自旧版画面: {', '.join(clip_stale)}"
                     + ("（已锁定，需人工裁决）" if review.is_locked(s, 'clip') else "")
                     if clip_stale else "")
                  + (f"  ⚠ 台词已改: {'/'.join(words)} 阶段的产物出自旧台词"
                     + ("（已锁定，需人工裁决）"
                        if all(review.is_locked(s, st) for st in words) else "")
                     if words else ""))
        if not any_row:
            print("  ✓ 全部就绪，无过期引用")


def cmd_lineage_mark(args):
    total_r = total_f = total_tr = total_tf = 0

    def fn(project):
        r, f = lineage.mark_stale(project)
        tr, tf = lineage.mark_text_stale(project)     # 台词那条边，与设定图并列
        return r, f, tr, tf
    for target in _lineage_targets(args):
        # 过期标记是表态类写入，按磁盘现状落盘；长任务期间照常可用
        r, f, tr, tf = Project.mutate(target.path, fn)
        if r or f or tr or tf:
            print(f"{target.id}: 设定图/画面 置重做 {r} 镜 · 锁定仅标记 {f} 镜"
                  f" | 台词 置重做 {tr} 镜 · 锁定仅标记 {tf} 镜")
        total_r += r
        total_f += f
        total_tr += tr
        total_tf += tf
    if not (total_r or total_f or total_tr or total_tf):
        print("没有过期的分镜（设定图与台词均未变化，或旧数据尚无血缘登记）。")
        return
    if total_r or total_f:
        print(f"\n✓ 设定图/画面变化：{total_r} 镜已置 retake"
              "（下次 gen-image / gen-video 自动重生+归档）"
              + (f"；{total_f} 镜已锁定仅挂过期标记" if total_f else ""))
    if total_tr or total_tf:
        print(f"✓ 台词变化：{total_tr} 镜已置 retake"
              "（下次 tts / gen-video 自动重出+归档）"
              + (f"；{total_tf} 镜已锁定仅挂过期标记" if total_tf else ""))


def cmd_assets_list(args):
    """跨项目全局资产库：所有项目的角色/道具/场景设定一览（IP 连载复用的地图）。"""
    ws = Workspace.open(args.workspace, create=False)
    rows = []
    for p in ws.list_projects():
        pid = p.get("id")
        for c in p.get("characters") or []:
            # ✓ = 这把声音出自本项目的音色档案（可回听、可换回）；无档案的是手工指派
            cast = voicebank.cast_for_ref(p, c.get("name"), c.get("voice"))
            rows.append((pid, "character", c.get("name"), bool(c.get("sheet")),
                         ("✓" if cast else "") + (c.get("voice") or "—"),
                         (c.get("origin") or {}).get("project")))
        for pr in p.get("props") or []:
            kind = "weapon" if pr.get("kind") == "weapon" else "prop"
            rows.append((pid, kind, pr.get("name"), bool(pr.get("sheet")), "—",
                         (pr.get("origin") or {}).get("project")))
        if p.get("scene") or p.get("scene_ref"):
            rows.append((pid, "scene", "main", bool(p.get("scene_ref")), "—", None))
    if args.kind:
        rows = [r for r in rows if r[1] == args.kind]
    if not rows:
        print("（空）没有匹配的资产。")
        return
    print(f"全局资产库 · {len(rows)} 项")
    print(f"  {'项目':<14}{'类型':<10}{'名称':<10}{'设定图':<6}{'音色':<14}来源")
    for pid, kind, name, sheet, voice, origin in rows:
        print(f"  {pid:<14}{kind:<10}{str(name):<10}{'✓' if sheet else '—':<6}"
              f"{voice:<14}{('← ' + origin) if origin else ''}")
    print("\n复用: assets import <目标项目> --from <源项目> --name <名称> [--kind character]")


def cmd_assets_import(args):
    """跨项目资产复用：把源项目的角色/道具/场景设定（含设定图与音色锁）拷进目标项目。"""
    import shutil
    from datetime import datetime
    ws = Workspace.open(args.workspace, create=False)
    src = ws.get_project(args.src)
    dst = ws.get_project(args.project)
    kind, name = args.kind, args.name
    origin = {"project": src.pid, "at": datetime.now().isoformat(timespec="seconds")}

    def _copy_file(path, prefix):
        """设定图/样本拷进目标项目 refs 目录，返回新路径（无文件则原样返回 None/URL）。"""
        if not path or str(path).startswith("http"):
            return path
        p = Path(path)
        if not p.is_file():
            return None
        dstf = dst.refs_dir / f"{prefix}_{_safe_name(name)}{p.suffix}"
        shutil.copy2(p, dstf)
        return str(dstf)

    if kind == "character":
        c = next((x for x in src.characters if x.get("name") == name), None)
        if c is None:
            raise KinemaError(f"源项目 {src.pid} 没有角色 {name}")
        if any(x.get("name") == name for x in dst.characters) and not args.force:
            raise KinemaError(f"目标项目已有角色 {name}（覆盖加 --force）")
        entry = dict(c)
        entry["sheet"] = _copy_file(c.get("sheet"), "char")
        # 候选是上一个项目的临时物，跟着角色走没有意义（路径还指向源项目）
        entry.pop("audition", None)
        entry.pop("custom_audition", None)
        entry["origin"] = origin
        # 在用音色**先随行再落角色**：档案连同那条不可变音频一并引入，否则目标
        # 项目里 `custom:vc_*` 解析不出参考音，配音会当场报错；顺序反过来，
        # 引入失败（源音频已删）时角色已入库、voice 还悬挂指着源项目的档案号
        cast = voicebank.cast_for_ref(src.data, name, c.get("voice"))
        got = voicebank.import_cast(dst, cast) if cast is not None else None
        if got is not None:
            entry["voice"] = voicebank.cast_ref(got)
        # --force 的同名剔除必须排在 import_cast 之后：commit() 进锁即从磁盘重载
        # 整份文档，之前只在内存里做的剔除会被重载丢弃，append 后新旧两条同名并存
        dst.data["characters"] = [x for x in dst.characters if x.get("name") != name]
        dst.characters.append(entry)
        dst.save()
        print(f"✓ 角色「{name}」已从 {src.pid} 引入 {dst.pid}"
              + ("（含设定图）" if entry.get("sheet") else "")
              + (f"（音色档案 {got['id']} 随行）" if cast is not None else ""))
        print(f"   新章节自动继承；已有章节要用它需重建或手工同步角色表。")
    elif kind in ("prop", "weapon"):
        p = next((x for x in src.props if x.get("name") == name), None)
        if p is None:
            raise KinemaError(f"源项目 {src.pid} 没有道具 {name}")
        if any(x.get("name") == name for x in dst.props):
            if not args.force:
                raise KinemaError(f"目标项目已有道具 {name}（覆盖加 --force）")
            dst.data["props"] = [x for x in dst.props if x.get("name") != name]
        entry = dict(p)
        entry["sheet"] = _copy_file(p.get("sheet"), "prop")
        entry["origin"] = origin
        dst.props.append(entry)
        dst.save()
        print(f"✓ 道具「{name}」已从 {src.pid} 引入 {dst.pid}")
    elif kind == "scene":
        if dst.data.get("scene_ref") and not args.force:
            raise KinemaError(f"目标项目已有场景设定（覆盖加 --force）")
        dst.data["scene"] = src.data.get("scene", "")
        dst.data["scene_ref"] = _copy_file(src.data.get("scene_ref"), "scene")
        dst.data["scene_origin"] = origin
        dst.save()
        print(f"✓ 场景设定已从 {src.pid} 引入 {dst.pid}")
    else:
        raise KinemaError(f"未知资产类型: {kind}")


# ---------- OSS 媒体上云（本地=渲染工作副本，OSS=媒体持久层，与 db 对称） ----------
def _oss_docs(ws, project=None, chapter=None):
    """待处理文档集: [(kind, pid, cid, data)]。chapter 形如 项目id/章节id。"""
    out = []
    if chapter:
        if "/" not in chapter:
            raise KinemaError("--chapter 需形如 项目id/章节id")
        pid, cid = chapter.split("/", 1)
        series = ws.get_project(pid)          # 软删项目在这里拒绝，与 stage 入口同闸
        out.append(("project", pid, None, series.data))
        out.append(("chapter", pid, cid, ws.store.load_chapter(pid, cid)))
    else:
        for p in ws.list_projects():
            pid = p.get("id")
            if project and pid != project:
                continue
            out.append(("project", pid, None, p))
            for ch in p.get("chapters", []):
                out.append(("chapter", pid, ch["id"],
                            ws.store.load_chapter(pid, ch["id"])))
    return [(k, p, c, d) for k, p, c, d in out if d]


def cmd_oss_status(args):
    from .storage.media import get_media_store
    ws = Workspace.open(args.workspace, create=False)
    ms = get_media_store(ws.root)
    print(f"媒体后端: {ms.backend}（config/storage.yaml 的 media 段）")
    print(f"详情: {ms.describe()}")
    if ms.enabled:
        try:
            print(f"连通性: ✓ {ms._cli().head()}")
        except Exception as e:  # noqa: BLE001
            print(f"连通性: ✗ {e}")
    else:
        print("提示: 设 media.backend=oss 并配置 provider/bucket/region 与"
              " KINEMA_OSS_ACCESS_KEY / KINEMA_OSS_SECRET_KEY 后即可上云。")


def cmd_oss_sync(args):
    """确认后上传：收集文档引用的本地媒体 → 上传 OSS → 文档路径改写为 URL →
    保存（数据库随之同步）。本地文件保留为渲染工作副本。"""
    from .storage.media import collect_media, get_media_store, rewrite_media
    ws = Workspace.open(args.workspace, create=False)
    ms = get_media_store(ws.root)
    if not ms.enabled:
        raise KinemaError("media.backend 不是 oss（config/storage.yaml）。"
                             "临时启用: KINEMA_MEDIA_BACKEND=oss")
    docs = _oss_docs(ws, project=args.project, chapter=args.chapter)
    files: dict[str, Path] = {}
    for _k, _p, _c, d in docs:
        for f in collect_media(d, ws.root):
            files[str(f)] = f
    if not files:
        print("没有需要上传的本地媒体（可能已全部上云）。")
        return
    total = sum(f.stat().st_size for f in files.values())
    print(f"待上传: {len(files)} 个文件 · {total / 1048576:.1f} MB")
    print(f"目标: {ms.describe()}")
    if not args.yes:
        if input("确认上传并把文档路径改写为 OSS 地址？输入 yes 继续: ").strip().lower() != "yes":
            print("已取消。")
            return
    mapping = {}
    for i, (k, f) in enumerate(sorted(files.items()), 1):
        mapping[k] = ms.upload(f)
        _info(f"[{i}/{len(files)}] ↑ {f.name}")
    changed = 0
    for kind, pid, cid, d in docs:
        n = rewrite_media(d, mapping)
        if n:
            changed += n
            if kind == "project":
                ws.store.save_project(pid, d)
            else:
                ws.store.save_chapter(pid, cid, d)
    print(f"\n✓ 上云完成: {len(files)} 个文件 · 文档改写 {changed} 处（JSON 与数据库已同步）")
    print("   本地文件保留为渲染工作副本；换机恢复用 `kinema oss pull`。")


def cmd_oss_pull(args):
    """按文档中的 OSS URL 把缺失媒体拉回本地（换机/删档恢复，与 db pull 对称）。"""
    from .storage.media import _walk_strings, get_media_store, is_url
    ws = Workspace.open(args.workspace, create=False)
    ms = get_media_store(ws.root)
    if not ms.enabled:
        raise KinemaError("media.backend 不是 oss（config/storage.yaml）。")
    urls: set = set()

    def probe(v):
        if is_url(v) and ms.local_for(v) is not None:
            urls.add(v)
        return None

    for _k, _p, _c, d in _oss_docs(ws, project=args.project, chapter=args.chapter):
        _walk_strings(d, probe)
    missing = [u for u in sorted(urls)
               if not (ms.local_for(u) and ms.local_for(u).is_file())]
    if not missing:
        print(f"引用媒体 {len(urls)} 个，本地齐全，无需拉取。")
        return
    print(f"拉取 {len(missing)} 个缺失媒体 …")
    for i, u in enumerate(missing, 1):
        p = ms.download(u)
        _info(f"[{i}/{len(missing)}] ↓ {p.name}")
    print(f"✓ 已恢复 {len(missing)} 个媒体到本地工作区。")


# ---------- 音色选角：试音 → 立档 → 启用（档案库 voicebank） ----------
def _voice_owner(args, *, required: bool = True) -> str | None:
    """命令行指向的实体。`--narrator` 是 `--name 旁白` 的糖，两者不并存。"""
    if getattr(args, "narrator", False):
        if getattr(args, "name", None):
            raise KinemaError("--narrator 与 --name 只能给一个")
        return voicebank.NARRATOR
    name = getattr(args, "name", None)
    if not name and required:
        raise KinemaError("要指定实体：--name <角色> 或 --narrator")
    return name


def cmd_voice_audition(args):
    store = ConfigStore.load(args.config)
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    s = Workspace.open(args.workspace, create=False).get_project(args.project)
    cands = [x.strip() for x in args.candidates.split(",")] if args.candidates else None
    owner = _voice_owner(args, required=False)
    owners = [owner] if owner else [c["name"] for c in s.characters]
    if not owners:
        raise KinemaError(f"项目 {s.pid} 还没有角色（先 character add，或用 --narrator 给旁白试音）")
    for who in owners:
        _step(f"试音 · {who} · {len(cands) if cands else voicebank.DEFAULT_COUNT} 个候选")
        r = voicebank.audition(store, router, s, who, candidates=cands, text=args.text)
        for e in r["entries"]:
            _info(f"[{e['no']}] {e['voice']}  ({e['voice_type']})  → {Path(e['path']).name}")
        _info(f"    本批合成费用 ¥{r['cost']:.4f}" if r["cost"] > 0
          else "    本批合成未入账：provider 未配置单价（不等于免费）")
    print("\n✓ 试音完成。逐条试听后选定（选中即立一条音色档案）：")
    print(f"   python3 -m kinema voice use {s.pid} --name <角色> --no <编号>")
    print("   （或在 Studio 项目页的选角卡点「用这条」）")


def _custom_count(args) -> int:
    """定制条数：显式 `--count` > 给了 `--adopt N` 时取 N > 缺省批量。

    `--adopt` 的语义是不试听直接立档第 N 条，多生成的候选没有消费者；
    每条演绎都是一次合成往返，缺省批量只在要试听挑选时才有意义。"""
    if getattr(args, "count", None) is not None:
        return max(1, int(args.count))
    adopt = getattr(args, "adopt", None)
    return int(adopt) if adopt else voicebank.CUSTOM_COUNT


def cmd_voice_custom(args):
    """定制生成：一段声线描述 → N 次演绎。选中的那条音频本身就是这把音色。"""
    store = ConfigStore.load(args.config)
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    s = Workspace.open(args.workspace, create=False).get_project(args.project)
    owner = _voice_owner(args)
    flag = "--narrator" if owner == voicebank.NARRATOR else f"--name {owner}"
    adopt = getattr(args, "adopt", None)
    count = _custom_count(args)
    if adopt:
        if not 1 <= int(adopt) <= count:
            raise KinemaError(f"--adopt {adopt} 超出本批候选范围（本批 1~{count}）")
        _step(f"定制生成 · {owner} · {count} 条 · 立档第 {adopt} 条")
        _print_voice_use(voicebank.cast_custom(s, store, router, owner, args.prompt,
                                               count=count, no=int(adopt),
                                               text=args.text))
        return
    _step(f"定制生成 · {owner} · {count} 条")
    r = voicebank.custom_audition(store, router, s, owner, prompt=args.prompt,
                                  count=count, text=args.text)
    for e in r["entries"]:
        _info(f"[{e['no']}] {Path(e['path']).name}")
    _info(f"    本批合成费用 ¥{r['cost']:.4f}" if r["cost"] > 0
          else "    本批合成未入账：provider 未配置单价（不等于免费）")
    print("\n✓ 定制已生成。试听后选定：")
    print(f"   python3 -m kinema voice use {s.pid} {flag} --custom --no <编号>")
    print("   （选中的那条音频就是这把音色本身——全片每句都拿它当参考音合成）")


def _print_voice_use(r) -> None:
    """启用一把声音之后的播报（`voice use` 与 `voice custom --adopt` 共用）。

    两条血缘边分开报：配音重跑是零成本的（下次 tts 自动重出），片段重做按秒计费，
    合成一句会让人读不懂账单。"""
    zh = "定制" if r["mode"] == "custom" else "模版"
    print(f"✓ 「{r['owner']}」已启用{zh}音色 {r['cast']} → {r['voice']}")
    print(f"   参考音: {r['clip']}")
    # 锚定音：native 真发时随请求附发的那条，选定时就落盘，页面「参考音频N」当场可听
    print(f"   锚定音: {r['anchor']}" if r.get("anchor")
          else "   ⚠ 锚定音预热失败——native 生视频真发时会再试一次；先跑 doctor 查 TTS 配置")
    print(f"   已同步 {r['chapters_synced']} 个章节；此后这个人的每一句都用这把声音。")
    if r["mode"] == "custom":
        print("   未经试听；换一条演绎：重跑 voice custom，或 voice use … --custom --no N 选本批另一条")
    if r["voice_retake"] or r["voice_stale"]:
        print(f"   ⚠ {r['voice_retake'] + r['voice_stale']} 镜的配音出自旧音色："
              f"{r['voice_retake']} 镜已置重做（下次 tts 自动重出）"
              + (f"，{r['voice_stale']} 镜已通过审阅、只挂过期标记等你裁决"
                 if r["voice_stale"] else ""))
    if r.get("clip_retake") or r.get("clip_stale"):
        print(f"   ⚠ {r['clip_retake'] + r['clip_stale']} 镜的片段按旧音色烧过人声："
              f"{r['clip_retake']} 镜已置重做——**重做按秒计费**，"
              "先 gen-video --dry-run 看报价"
              + (f"；另有 {r['clip_stale']} 镜已通过审阅、只挂过期标记等你裁决"
                 if r["clip_stale"] else ""))


def cmd_voice_use(args):
    """启用一把声音：从候选立档，或换回档案里已有的某一条。"""
    store = ConfigStore.load(getattr(args, "config", None))
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    s = Workspace.open(args.workspace, create=False).get_project(args.project)
    if getattr(args, "cast", None):
        r = voicebank.use_cast(s, store, args.cast, router=router)
    else:
        owner = _voice_owner(args)
        if not args.no:
            raise KinemaError("要给 --no <编号>（候选编号），或用 --cast <档案号> 换回历史音色")
        r = (voicebank.use_custom(s, store, owner, args.no, router=router) if args.custom
             else voicebank.use_audition(s, store, owner, args.no, router=router))
    _print_voice_use(r)


def cmd_voice_bank(args):
    """看某个实体的音色档案：哪条在用、每条被谁引用着、哪条可以删。"""
    s = Workspace.open(args.workspace, create=False).get_project(args.project)
    owner = _voice_owner(args, required=False)
    for who in ([owner] if owner else voicebank.owners(s)):
        v = voicebank.bank_view(s, who)
        print(f"\n■ {who}  在用: {v['voice'] or '—（走 profile 默认）'}")
        if not v["casts"]:
            print("   （还没有音色档案——character set --voice-prompt / voice custom --narrator 定制；模版走 voice audition）")
            continue
        for c in v["casts"]:
            refs = c["refs"]
            mark = "✓ 在用" if c["active"] else ("可删" if refs["deletable"] else "有引用")
            what = c["alias"] or (c["prompt"] or "")[:24]
            print(f"   {c['id']}  {'定制' if c['mode'] == 'custom' else '模版'}"
                  f"  {what:<26} {c['at'][:16].replace('T', ' ')}  [{mark}]")
            if refs["generated"]:
                print(f"        · {refs['generated']} 个分镜的配音出自这把声音")
            if refs["assigned"]:
                print(f"        · {refs['assigned']} 处仍指派着它")


def cmd_voice_rm(args):
    s = Workspace.open(args.workspace, create=False).get_project(args.project)
    r = voicebank.delete_cast(s, args.cast)
    print(f"✓ 音色档案 {r['cast']}（{r['owner']}）已删除 · 同步 {r['chapters_synced']} 个章节")


def cmd_voice_list(args):
    s = Workspace.open(args.workspace, create=False).get_project(args.project)
    print(f"{s.pid} 选角 ({len(s.characters)} 角色 + 旁白):")
    for who in voicebank.owners(s):
        v = voicebank.bank_view(s, who)
        n_cast = len(v["casts"])
        pending = (v["audition"].get("entries") or v["custom_audition"].get("entries"))
        state = "在用" if v["active"] else ("待选" if pending else "未选角")
        print(f"  · {who:<8} {v['voice'] or '-':<16} [{state}·档案 {n_cast}]")



def cmd_export_review(args):
    """静态审阅包导出：免登录单页 HTML + 自包含媒体，发客户离线审阅。"""
    store = ConfigStore.load(getattr(args, "config", None))
    project = _load_video(args)
    from .export import build_review_page
    out = args.out or str(project.exports_dir / f"{project.path.stem}_review")
    index = build_review_page(project, store, out)
    print(f"✓ 静态审阅包已导出: {index}")
    print("   整个目录可打包发给客户（免登录、离线可打开、媒体已自包含）")


# ---------- 持久化（config/storage.yaml：local 默认 / mysql 可选）----------
def _mysql_store(args):
    from .errors import ConfigError
    from .storage import get_storage, load_storage_config
    cfg = load_storage_config()
    ws = find_workspace(getattr(args, "workspace", None))
    if cfg["backend"] != "mysql":
        raise ConfigError("当前 backend=local。要使用数据库请在 config/storage.yaml 设 backend: mysql，"
                          "或临时 export KINEMA_STORAGE_BACKEND=mysql")
    return get_storage(ws), ws


def cmd_db_status(args):
    from .storage import get_storage, load_storage_config
    cfg = load_storage_config()
    ws = find_workspace(getattr(args, "workspace", None))
    print(f"存储配置: {cfg['source']}")
    print(f"后端: {cfg['backend']}")
    st = get_storage(ws)
    print(f"详情: {st.describe()}")
    if cfg["backend"] == "mysql":
        c = st.counts()
        print(f"库内: 项目 {c['projects']} · 章节 {c['chapters']} · 已渲染 {c['rendered']}"
              f" · 资产 {c['assets']} · 分镜 {c['shots']}")
    else:
        n = len(st.list_projects())
        print(f"本地: 项目 {n}（提示: 配置 mysql 后首次访问会自动懒迁移入库）")


def cmd_db_init(args):
    st, _ = _mysql_store(args)
    st._db()   # 连接即建表
    print(f"✓ 已连接并确保表结构: {st.describe()}")


def cmd_db_sync(args):
    """本地 JSON → 数据库（全量 upsert，媒体不动、只登记路径）。"""
    from .storage.local import LocalStorage
    st, ws = _mysql_store(args)
    local = LocalStorage(ws)
    np = nc = 0
    for data in local.list_projects():
        pid = data.get("id")
        if not pid:
            continue
        st._upsert_project(pid, data)
        np += 1
        for ch in data.get("chapters", []):
            cdata = local.load_chapter(pid, ch.get("id"))
            if cdata:
                st._upsert_chapter(pid, ch["id"], cdata)
                nc += 1
    print(f"✓ 已同步 本地→数据库: 项目 {np} · 章节 {nc}")


def cmd_db_schema(args):
    """导出完整 MySQL 建库建表脚本（单一真源=代码内 _SCHEMA，永不漂移）。
    用于交付/DBA 审阅/手工建库；程序连接时仍会自动 CREATE TABLE IF NOT EXISTS。"""
    from .storage import load_storage_config
    from .storage.mysql import _SCHEMA
    cfg = load_storage_config()
    db = (cfg.get("mysql") or {}).get("database", "kinema")
    prefix = (cfg.get("mysql") or {}).get("table_prefix", "kn_")
    from datetime import datetime
    lines = [
        "-- ============================================================================",
        "-- kinema · MySQL 完整建库建表脚本",
        f"-- 生成: python3 -m kinema db schema（{datetime.now().strftime('%Y-%m-%d')}）",
        "-- 单一真源: engine/kinema/storage/mysql.py 的 _SCHEMA —— 请勿手改本文件，",
        "--           改 schema 后重新执行上述命令再生成。",
        "-- 说明: 雪花ID单主键 · 业务标识唯一键 · 逻辑外键(无物理FK) · 全表全列 COMMENT",
        "--       媒体只存路径 · 完整文档存 data 列（可整体恢复）",
        "-- ============================================================================",
        "",
        f"CREATE DATABASE IF NOT EXISTS `{db}` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        f"USE `{db}`;",
        "",
    ]
    for stmt in _SCHEMA.replace("{p}", prefix).split("---"):
        lines.append(stmt.strip() + ";")
        lines.append("")
    ddl = "\n".join(lines)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(ddl, encoding="utf-8")
        print(f"✓ 建表脚本已导出: {args.out}（{ddl.count('CREATE TABLE')} 张表）")
    else:
        print(ddl)


def cmd_db_pull(args):
    """数据库 → 本地 JSON（换机/删档恢复文档；媒体文件需另行拷贝）。"""
    st, _ = _mysql_store(args)
    np = nc = 0
    for data in st.list_projects():      # list/load 自带镜像写盘
        np += 1
        for ch in data.get("chapters", []):
            if st.load_chapter(data["id"], ch.get("id")):
                nc += 1
    print(f"✓ 已恢复 数据库→本地: 项目 {np} · 章节 {nc}（媒体文件不在库中，如缺请从原机拷贝 *_work/）")


def cmd_init(args):
    data = {
        "id": args.id or Path(args.project).stem,
        "theme": args.theme or "",
        "platform": args.platform.split(",") if args.platform else [],
        "duration": args.duration,
        "aspect": args.aspect or DEFAULT_ASPECT,
        "profile": args.profile,
        "framework": args.framework,
        "style": {"seed": 12345},
        "shots": [],
    }
    p = Path(args.project)
    if p.exists() and not args.force:
        raise KinemaError(f"{p} 已存在（加 --force 覆盖）")
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已创建 {p}（profile={args.profile}）。请由 Skill 层填充 script/style/shots。")


# ---------- 项目模板 / 平台规格 ----------
def cmd_template_list(args):
    from . import templates as tpl_mod
    ts, src = tpl_mod.load_templates()
    print(f"项目模板 · {len(ts)} 个（{src}）")
    for name, t in sorted(ts.items()):
        ep = t.get("episode") or {}
        mins = ep.get("minutes")
        print(f"  {name:<22}{t.get('label', ''):<10}{t.get('aspect', '—'):<7}"
              f"motion={t.get('motion', '—')}  profile={t.get('profile', '—'):<14}"
              + (f"每集 {mins[0]}–{mins[1]} 分钟" if mins else ""))
    print("\n实例化: project new --title X --template <名> · 核对: spec check <项目>")


def cmd_template_show(args):
    from . import templates as tpl_mod
    t = tpl_mod.get(args.name)
    print(f"模板 {args.name} 「{t.get('label')}」")
    for k, zh in [("platform", "平台"), ("aspect", "比例"), ("motion", "渲染模式"),
                  ("profile", "风格档"), ("episode", "单集规格"), ("series", "系列体量"),
                  ("notes", "要点")]:
        if t.get(k) is not None:
            print(f"  {zh}: {t[k]}")


def cmd_spec_check(args):
    """平台规格核对：逐章时长/分镜数/比例 + 系列体量是否达标（交付验收线）。"""
    from . import templates as tpl_mod
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    tpl = s.data.get("template")
    if not tpl:
        # 无模板即无规格可核对：交付四闸按不适用放行，不当失败
        print(f"ⓘ 项目 {s.pid} 未绑定模板，无平台规格可核对（跳过）。"
              "要按平台规格验收：新项目 `project new --template <名>`，"
              "已有项目在 project.json 顶层补 template 块（template show <名> 参考）。")
        return
    ico = {True: "✓", False: "⚠", None: "·"}
    print(f"规格核对 · {s.pid} 「{s.data.get('title')}」 · 模板 {tpl.get('label') or tpl.get('name')}")
    total_min, episodes, bad = 0.0, 0, 0
    for ch in s.list_chapters():
        cdata = ws.store.load_chapter(s.pid, ch["id"]) or {}
        active = [x for x in cdata.get("shots") or []
                  if ((x.get("review") or {}).get("shot") or {}).get("state") != "omt"]
        dur = sum(float(x.get("dur") or 0) for x in active)
        rows = tpl_mod.check_chapter(tpl, duration_s=dur, shots=len(active),
                                     aspect=cdata.get("aspect") or "")
        flag = "⚠" if any(r["ok"] is False for r in rows) else "✓"
        bad += (flag == "⚠")
        cells = " · ".join(
            f"{ico[r['ok']]}{r['item']} {r['actual']}"
            + (f"（要求 {r['expect']}）" if r["ok"] is False else "")
            for r in rows)
        print(f"  {ch['id']:<8}{flag}  {cells}")
        total_min += dur / 60
        episodes += 1
    for r in tpl_mod.check_series(tpl, episodes=episodes, total_minutes=total_min):
        print(f"  系列    {ico[r['ok']]}  {r['item']}: {r['actual']}（目标 {r['expect']}）")
    if tpl.get("notes"):
        print(f"  ⓘ {tpl['notes']}")
    print(("✓ 全部达标" if not bad else f"⚠ {bad} 章未达标——先补拍/调整再交付"))


# ---------- 分镜单调度 lint ----------
def cmd_lint(args):
    """分镜单调度体检：运镜/情绪/景别/空词/占位旁白/高光分布（纯计算·不落盘）。

    与 `spec check` 同族：只读文档算统计量，一个字节都不写回 project.json。
    **不套 `_stage_wrapper`**——那层带 router/store/checkpoint 语义（要连模型配置、
    要 ensure_tools），而 lint 连 ffmpeg 都不需要。"""
    project = _load_video(args)
    data = project.data
    ad = variation_mod.resolve_art_direction(data)
    findings = variation_mod.lint(data)
    n = len(variation_mod.active_shots(data))
    ch = (data.get("chapter") or {}).get("title") or data.get("theme") or project.path.stem
    _step(f"分镜单 lint · {ch} · {n} 正镜 · 旋钮 variety={ad['variety']}"
          f"/motion={ad['motion']}/density={ad['density']}"
          + (f" · 回避词 {len(ad['avoid'])}" if ad["avoid"] else ""))
    for f in findings:
        _info(f.line())
        if f.hint:
            print(f"      → {f.hint}", flush=True)
    s = variation_mod.summarize(findings)
    if not findings:
        print("✓ 各维度都没话说（软闸只看字面统计量，好不好看仍需人眼）")
    else:
        print(f"{'⚠' if s['warn'] else 'ⓘ'} {s['warn']} 警告 / {s['info']} 提示"
              "——lint 结论不落盘，改完再跑一次即可")
    if getattr(args, "strict", False) and s["warn"]:
        raise SystemExit(2)


def _lint_gate(project, *, only=None) -> None:
    """生图前的调度软闸：只提示、不阻断、不落盘。

    **必须扫全片 shots、且必须在 `--only` 过滤之前调用**——单镜重生时若只扫 1 镜，
    相邻运镜雷同/景别分布/情绪多样性三个维度会全部失真（Studio 分镜卡
    「↻ 重新生成」每次都是单镜过闸）。`--only` 时降为一行汇总，不刷屏。"""
    findings = variation_mod.lint(project.data)
    if not findings:
        return
    s = variation_mod.summarize(findings)
    if only:
        _info(f"分镜单 lint（全片口径）：{s['warn']} 警告 / {s['info']} 提示"
              "——详情跑 `python3 -m kinema lint --chapter <项目id/章节id>`")
        return
    _info(f"分镜单 lint：{s['warn']} 警告 / {s['info']} 提示（只提示不阻断）")
    for f in findings[:6]:
        _info(f"  {f.line()}")
    if len(findings) > 6:
        _info(f"  …还有 {len(findings) - 6} 条，跑 `kinema lint` 看全量与改写建议")


# ---------- 跨镜批量编辑 ----------
@_op_locked("batch-edit")
def cmd_batch_edit(args):
    from . import batch as batch_mod
    project = _load_video(args)
    shots = _parse_shots(project, args.shots or "all")
    ops = [(o, getattr(args, o)) for o in ("set", "append", "prepend", "replace")
           if getattr(args, o) is not None]
    if len(ops) != 1:
        raise KinemaError("请指定且仅指定一种操作: --set / --append / --prepend / --replace \"旧=>新\"")
    op, value = ops[0]
    r = batch_mod.apply(project, shots, args.field, op, value,
                        mark_retake=not args.no_retake,
                        include_locked=args.include_locked, note=args.note)
    if not r["changed"]:
        print("没有产生变化"
              + (f"（{r['skipped_locked']} 镜已锁定被保护，--include-locked 可纳入）"
                 if r["skipped_locked"] else "") + "。")
        return
    print(f"✓ 批量修改 {args.field}[{op}] · 改动 {r['changed']} 镜"
          + (f" · 锁定保护跳过 {r['skipped_locked']} 镜" if r["skipped_locked"] else "")
          + (f" · 无变化 {r['unchanged']} 镜" if r["unchanged"] else ""))
    if r.get("retaken"):
        # 点出阶段：一个字段可能同时打回生图与片段（如 negative_prompt 两侧都吃），
        # 只说「已置重做」没说清后续要重跑哪几个阶段
        print(f"   已置重做（下次生成自动重生+旧版归档）· 阶段 {'/'.join(r['stages'])}"
              f": 镜 {', '.join(r['retaken'])}")
    print(f"   撤销: batch undo --chapter ... （操作ID {r['op_id']}，batch log 可查）")


@_op_locked("batch-undo")
def cmd_batch_undo(args):
    from . import batch as batch_mod
    project = _load_video(args)
    r = batch_mod.undo(project, args.op)
    print(f"✓ 已撤销批量操作 {r['op_id']}（{r['field']}）· 还原 {r['restored']} 镜的字段与审阅状态"
          + (f" · {r['skipped_locked']} 镜已通过锁定未动" if r.get("skipped_locked") else ""))


def cmd_batch_log(args):
    from . import batch as batch_mod
    project = _load_video(args)
    entries = batch_mod.history(project)
    if not entries:
        print("（空）没有批量操作记录。")
        return
    print(f"批量操作日志 · {project.id} · {len(entries)} 条（旧→新）")
    for e in entries:
        print(f"  {e.get('at', '')}  #{e.get('id')}  {e.get('field')}[{e.get('op')}] "
              f"“{str(e.get('value'))[:40]}”  改动 {len(e.get('changes') or {})} 镜")


# ---------- 框选局部改造（"只改这一处"的第四种迭代手段）----------
def cmd_refine(args):
    from . import refine as refine_mod
    store = ConfigStore.load(args.config)
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    rect = None
    if args.rect:
        try:
            x, y, w, h = (float(v) for v in args.rect.split(","))
            rect = {"x": x, "y": y, "w": w, "h": h}
        except ValueError:
            raise KinemaError('rect 需要 "x,y,w,h" 四个 0~1 相对值，如 0.6,0.1,0.3,0.25')
    if args.shot:
        from .locking import op_lock
        with op_lock(Path(_project_path(args)), kind="refine"):
            project = _load_video(args)
            r = refine_mod.refine_shot_image(project, store, router, shot_no=args.shot,
                                             rect=rect, instruction=args.note,
                                             no_moodboard=getattr(args, "no_moodboard", False))
        print(f"✓ 镜 {r['shot']} 局部改造完成（{r['region']}）→ v{r['version']:03d} 待审")
        print(f"   旧版已归档进版本栈，可 versions rollback 反悔")
        return
    if args.asset:
        kind, _, name = args.asset.partition(":")
        if not args.project_id:
            raise KinemaError("设定图改造需要 --id <项目>")
        ws = Workspace.open(args.workspace, create=False)
        r = refine_mod.refine_asset(ws, store, router, pid=args.project_id,
                                    kind=kind, name=name or None,
                                    rect=rect, instruction=args.note,
                                    no_moodboard=getattr(args, "no_moodboard", False))
        print(f"✓ 设定图局部改造完成（{r['region']}）: {r['image']}")
        print(f"   旧版备份: {r['backup']}")
        if r["stale_retaken"] or r["stale_flagged"]:
            print(f"   血缘传播: {r['stale_retaken']} 镜置重做 · {r['stale_flagged']} 锁定镜挂过期标记")
        return
    raise KinemaError("要么 --shot <镜号>（分镜图），要么 --asset character:名|scene|prop:名（设定图）")


def cmd_pick_ref(args):
    from .refine import pick_asset_candidate
    ws = Workspace.open(args.workspace, create=False)
    kind, _, name = args.asset.partition(":")
    r = pick_asset_candidate(ws, args.id, kind=kind, name=name or None, no=args.use)
    print(f"✓ 设定图已定稿: {r['kind']}"
          + (f" {r['name']}" if r["name"] else "")
          + f" ← 候选 {r['no']}/{r['candidates']} → {r['image']}")
    if r["stale_retaken"] or r["stale_flagged"]:
        print(f"   血缘传播: {r['stale_retaken']} 镜置重做 · {r['stale_flagged']} 锁定镜挂过期标记")
    print("   换选随时重跑本命令（旧稿自动备份 _vNNN）。")


# ---------- 生意工具箱 / 交付 ----------
def cmd_sfx_list(args):
    from .audio_registry import library_root, load_registry
    reg = load_registry()
    root = library_root(reg)
    print(f"音效库 {root}/sfx（B 外置素材优先 · 缺文件自动回落 ffmpeg 合成 · config/audio.yaml 注册表）")
    for cat, items in (reg.get("sfx") or {}).items():
        if not isinstance(items, dict):
            continue
        print(f"  [{cat}]")
        for k, v in items.items():
            rel = v.get("file") if isinstance(v, dict) else v
            desc = (v.get("desc") or "") if isinstance(v, dict) else ""
            mark = "✓ 外置" if (root / rel).is_file() else "○ 合成兜底"
            print(f"    {mark}  {k:<8} {rel}  {desc}")
    print("补齐外置素材：python music/download.py（BGM+音效两套一键）· 或 sfx gen --kind <键> --yes（AI 生成）")


def cmd_sfx_gen(args):
    from .audio_registry import library_root, load_registry
    from .ffmpeg import run
    from .pipeline import transitions as tr
    reg = load_registry()
    entry = ((reg.get("sfx") or {}).get(args.category) or {}).get(args.kind) or {}
    rel = ((entry.get("file") if isinstance(entry, dict) else entry)
           or f"sfx/{args.category}/{args.kind}.wav")
    out = library_root(reg) / rel
    if out.exists() and not args.force:
        print(f"已存在（--force 覆盖）: {out}")
        return
    prompt = args.desc or (entry.get("desc") if isinstance(entry, dict) else None) \
        or f"short cinematic {args.kind} transition sound effect, clean, no music"
    if args.mock:
        src, filt = tr.whoosh_audio(args.dur, kind=args.kind
                                    if args.kind in ("whoosh", "riser", "boom") else "whoosh")
        out.parent.mkdir(parents=True, exist_ok=True)
        run(["-f", "lavfi", "-i", src, "-af", filt, str(out)], desc=f"mock sfx {args.kind}")
        print(f"[mock] 合成占位音效已落库: {out}")
        return
    if not args.yes:
        raise SystemExit("AI 生成音效是付费操作（ElevenLabs），确认后加 --yes；"
                         "零成本替代：python music/download.py 拉 CC0 素材，或不放文件走合成兜底。")
    store = ConfigStore.load(args.config)
    from .providers.music.elevenlabs import ElevenLabsMusicProvider
    prov = ElevenLabsMusicProvider(store.provider_conn("elevenlabs"), store)
    tmp = out.with_suffix(".gen.mp3")
    r = prov.sound_effect(prompt, tmp, duration=args.dur)
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-i", str(tmp), "-ar", "44100", "-ac", "2", str(out)], desc="sfx→wav")
    tmp.unlink(missing_ok=True)
    cost = f" · ¥{r.cost:.2f}" if r.cost else ""
    print(f"已生成并落库: {out}{cost}（compose 此后自动优先用它；请在 music/ATTRIBUTION.md 登记来源=AI 生成）")


@_op_locked("supply")
def cmd_supply(args):
    """素材直供：现成图片 → 分镜画面（拷入 images/ 同名规则，直供也走版本栈+待审）。

    登记前过一道**供料体检**（分辨率/宽高比/alpha/可解码性，零成本纯本地探测）：
    只有「ffprobe 解不出」硬拦，其余告警不拦死并留痕 gen.image.inspect；
    体检本体在 `supply.supply_image` 内部，网页上传走同一条闸。"""
    from . import supply as supply_mod
    project = Project.load(_project_path(args))
    store = ConfigStore.load(args.config)
    r = supply_mod.supply_image(project, args.shot, args.file, aspect=args.aspect,
                                store=store, skip_check=args.skip_check)
    ins = r.get("inspect") or {}
    print(f"✓ 素材已直供: 镜 {r['shot']}"
          + (f"（{r['aspect']}）" if r["aspect"] else "")
          + f" → {r['path']}"
          + (f"（旧版已归档 v{r['archived']:03d}）" if r["archived"] else ""))
    if ins.get("width"):
        print(f"   体检 {ins['width']}×{ins['height']} · {ins.get('pix_fmt')}"
              + (f" · 覆盖画布 {ins['coverage'] * 100:.0f}%"
                 if ins.get("coverage") is not None else "")
              + (f" · {len(ins.get('warn') or [])} 项告警" if ins.get("warn") else ""))
    print("   已落待审 · provider=supplied（生图阶段自动跳过此镜，零生图成本）")
    if r.get("clip") == "retake":
        print("   片段按被换掉的画面生成 → clip 置 retake（下次 gen-video 重生）")
    elif r.get("clip") == "locked":
        print("   ⚠ 片段已通过锁定，按旧画面生成——要跟上新图请解锁后置 retake")
    if r.get("rebaselined"):
        print(f"   血缘基线已重设为当前设定图（{'、'.join(r['rebaselined'])}）"
              "，「设定已更新」标记同时清除")


# ---------- previz（3D 导演预演）：登记 / 摘除 / 运镜库 ----------
@_op_locked("previz-register")
def cmd_previz_register(args):
    """把一段 previz mp4 登记成该镜的生成条件（首帧/末帧/参考片/运镜四件套）。

    产物全部落回既有 `shots[]` 契约，走 supply 同一套版本栈与待审制度——
    3D 导演控制台在引擎侧就落在这一条命令上，控制台只是它的可视化前端。
    """
    project = Project.load(_project_path(args))
    store = ConfigStore.load(args.config)
    ensure_tools()
    use_first = True if args.use_first_frame else (False if args.no_first_frame else None)
    r = previz_mod.register_previz(
        project, args.shot, args.file, camera_preset=args.camera, aspect=args.aspect,
        use_first_frame=use_first, store=store, skip_check=args.skip_check)
    _step(f"previz 已登记 · 镜 {r['shot']}")
    _info(f"参考片: {r['previz']}")
    _info(f"末帧  : {r['last_frame']} → shots[].last_frame_ref"
          "（发 Seedance 末帧时优先于衔接链给出的下一镜图）")
    if r["image_registered"]:
        _info(f"首帧  : {r['first_frame']} → shots[].image（provider=supplied · 已落待审"
              + (f" · 旧版已归档 v{r['archived']:03d}）" if r["archived"] else "）"))
    if r.get("camera"):
        _info(f"运镜  : [{r['camera_preset']}] {r['camera']}")
    print("下一步：`gen-video --chapter … -m b`（native）出片；"
          "要让成片跟随预演的运镜与走位，加 `--previz` 启用参考视频 V2V")


@_op_locked("previz-build")
def cmd_previz_build(args):
    """把控制台上传的 PNG 序列编成 previz mp4 并登记（Studio「渲染 previz」的后台任务）。

    **与 `register` 走同一条登记路径**——网页与 CLI 不许各写一份登记逻辑，
    否则版本栈/待审/首帧覆盖三条纪律迟早在网页那边失效。
    """
    project = Project.load(_project_path(args))
    store = ConfigStore.load(args.config)
    ensure_tools()
    use_first = True if args.use_first_frame else (False if args.no_first_frame else None)
    r = previz_mod.build_from_frames(
        project, args.shot, fps=args.fps, keep_frames=args.keep_frames,
        camera_preset=args.camera, aspect=args.aspect, use_first_frame=use_first,
        store=store, skip_check=args.skip_check)
    _step(f"previz 已渲染并登记 · 镜 {r['shot']} · {r['frames']} 帧 @ {r['fps']}fps")
    _info(f"参考片: {r['previz']}")
    if r.get("camera"):
        _info(f"运镜  : [{r['camera_preset']}] {r['camera']}")


def cmd_previz_clear(args):
    project = Project.load(_project_path(args))
    r = previz_mod.clear_previz(project, args.shot)
    if r["dropped"]:
        print(f"✓ 镜 {r['shot']} 已摘除 previz 挂载: {', '.join(r['dropped'])}"
              "（产物文件保留在 previz/ 目录，分镜图不动）")
    else:
        print(f"镜 {r['shot']} 本来就没有 previz 挂载")


def cmd_previz_list(args):
    project = Project.load(_project_path(args))
    rows = [s for s in project.shots if s.get("previz")]
    if not rows:
        print("本章还没有任何镜登记 previz——先在 Studio 的 3D 导演控制台排戏并渲染，"
              "或 `previz register --shot N --file 预演.mp4`")
        return
    _step(f"previz 挂载 · {len(rows)} 镜")
    for s in rows:
        sec = previz_mod.previz_seconds(s)
        print(f"  ▸ 镜{s['id']} · {sec:.1f}s · {Path(str(s['previz'])).name}"
              + (f" · 运镜[{s.get('camera_preset')}]" if s.get("camera_preset") else "")
              + (" · 末帧✓" if s.get("last_frame_ref") else ""))


_REEL_SKIP_ZH = {"omt": "已弃用", "transition": "转场镜（无 previz）",
                 "no_previz": "还没渲 previz"}


def cmd_previz_reel(args):
    """把各镜 previz 拼成一条全片预演（零 API 成本，纯本地 ffmpeg）。

    这是**观看物**，不是成片：不进 `clip`/`output`，不喂模型（V2V 恒逐镜发本镜
    那一段）。逐镜点开看不出整场戏连起来的节奏，而节奏正是排戏要审的东西。
    """
    project = Project.load(_project_path(args))
    r = previz_mod.build_reel(project, fps=args.fps)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    _step(f"全片预演已合成 · {len(r['shots'])} 镜 · {r['duration']:.1f}s"
          f" · {r['width']}×{r['height']} @ {r['fps']}fps"
          + ("（流拷贝·零重编码）" if r["mode"] == "copy" else "（规格不齐·已重编码归一）"))
    _info(f"文件: {r['file']}  ({r['size'] / 1048576:.1f} MB)")
    print("  " + " · ".join(f"镜{x['id']} {x['seconds']:.1f}s" for x in r["shots"]))
    if r["skipped"]:
        # 少了哪几镜必须说清楚——「合出来了」不等于「全片都在里面」
        print("  未入片: " + " · ".join(
            f"镜{x['id']}（{_REEL_SKIP_ZH.get(x['why'], x['why'])}）" for x in r["skipped"]))
    print("3D 导演控制台工具条「▤ 合成全片 / ▶ 看全片 / ⬇」是同一份文件")


def cmd_previz_presets(args):
    """列出全部运镜 preset（3D 装备 + 发给 Seedance 的措辞，两者同源）。"""
    cat = camera_mod.catalog()
    if getattr(args, "json", False):
        print(json.dumps(cat, ensure_ascii=False, indent=2))
        return
    want = getattr(args, "group", None)
    _step(f"运镜库 · {len(cat)} 个 preset（●稳定 / ▲进阶一集≤4 / ■高危仅峰值一镜）")
    cur = None
    for c in cat:
        if want and c["group"] != want:
            continue
        if c["group"] != cur:
            cur = c["group"]
            print(f"\n— {c['group_label']} —")
        print(f"  {c['tier_mark']} {c['key']:<18} {c['label']}（{c['label_en']}）"
              f"  {c['duration']:g}s · {c['rig']}")
        print(f"      {c['desc']}")
        print(f"      camera: {c['phrase']}")


# ---------- control（深度捕捉）：素材处理 / 绑定 / 清单 / 权重 ----------
def cmd_control_build(args):
    """把一段实拍片处理成控制视频素材。

    **刻意不带 `@_op_locked`**：这条链要跑几分钟且全程不碰章节文档，占着章节
    操作锁会把 gen-image/tts/assemble 一起堵死。它自己的互斥在 `control/` 目录内
    的 build 锁上（跨进程，CLI 与 Studio 共用）。绑定类动词则必须持锁——见下。
    """
    project = Project.load(_project_path(args))
    _step(f"深度捕捉 · {Path(args.source).name}")
    t0 = time.time()

    def on_progress(pass_no, done, total):
        # 进度是**汇报**不是活。Studio 把本命令派成子进程并读它的 stdout；
        # Studio 一重启，管道就断，下一次 print 抛 BrokenPipeError——那会把已经
        # 跑了几分钟的处理连同半成品一起葬掉，而没人在听进度并不是失败。
        try:
            _info(f"pass{pass_no} {done}/{total}")
        except OSError:
            pass

    r = control_mod.build_asset(project, args.source, asset_id=args.asset,
                                styled=not args.no_styled, mock=args.mock,
                                on_progress=on_progress)
    src = r["source"]
    _step(f"素材已就绪 · {r['id']} · {r['people']} 人 · "
          f"{src['frames']} 帧 @ {src['fps']:g}fps · {src['seconds']:.1f}s "
          f"（{time.time() - t0:.0f}s）")
    for k, v in (r.get("timings") or {}).items():
        _info(f"{k}: {v}s")
    _info(f"产物: {'、'.join(sorted(r.get('outputs') or {}))}")

    # 上传时就选好了镜的话，处理完直接绑上。**绑定要持章节操作锁**，而本命令刻意
    # 不持（它跑几分钟且不碰文档），故在这里单独取一次，锁只罩住真正写盘的那一步。
    if getattr(args, "bind_shot", None):
        from .locking import op_lock
        with op_lock(Path(_project_path(args)), kind="control-bind"):
            b = control_mod.bind_shot(Project.load(_project_path(args)),
                                      args.bind_shot, r["id"],
                                      store=ConfigStore.load(args.config))
        _step(f"已绑到镜 {b['shot']} · {b['start']:g}~{b['end']:g}s（{b['seconds']}s）")
        print("下一步：`gen-video --chapter … -m b --control --dry-run` 审报价")
        return
    print("下一步：`control bind --shot <镜号> --asset "
          f"{r['id']} --start <起点秒> --end <终点秒>` 绑到镜上，"
          "再 `gen-video --control --dry-run` 审报价")


@_op_locked("control-bind")
def cmd_control_bind(args):
    project = Project.load(_project_path(args))
    store = ConfigStore.load(args.config)
    ensure_tools()
    r = control_mod.bind_shot(project, args.shot, args.asset, start=args.start,
                              end=args.end, fit=args.fit,
                              replace_previz=args.replace_previz, store=store)
    _step(f"控制视频已绑定 · 镜 {r['shot']} · 素材 {r['asset']} "
          f"@ {r['start']:g}~{r['end']:g}s")
    _info(f"段落: {r['control']}（{r['seconds']}s · 贴合 {r['fit']}）")
    if args.end is not None:
        _info(f"本镜 dur 已对齐到 {r['dur']}s——控制段与成片 1:1 是运动不被拉伸的前提")
    _info("已把本镜片段置 retake——换了运动源，旧片段不再是这一版的产物")
    print("下一步：`gen-video --chapter … -m b --control --dry-run` 审逐镜提示词与"
          "输入视频秒数；持久开启在章节写 `control_video: true`")


@_op_locked("control-unbind")
def cmd_control_unbind(args):
    project = Project.load(_project_path(args))
    r = control_mod.unbind_shot(project, args.shot)
    if not r["dropped"]:
        print(f"镜 {r['shot']} 本来就没有控制视频绑定。")
        return
    _step(f"已摘除 · 镜 {r['shot']}（{'、'.join(r['dropped'])}）")
    _info("段落文件保留在盘上——重绑同一素材时省一次重编码")


def cmd_control_compare(args):
    """出某镜的对照片。只读文档、只写自己的产物，故不取章节锁。"""
    project = Project.load(_project_path(args))
    ensure_tools()
    s = next((x for x in project.data.get("shots") or []
              if str(x.get("id")) == str(args.shot)), None)
    if s is None:
        raise ProjectError(f"找不到镜 {args.shot}")
    dst = control_mod.build_shot_compare(project, s)
    rec = (s.get("gen") or {}).get("control") or {}
    three = "compare3" in dst.name
    _step(f"{'三' if three else '两'}合一对照已出 · 镜 {s['id']} · "
          f"{rec.get('start', 0):g}~{rec.get('end', 0):g}s")
    _info(f"{dst}（左源片 · 右控制"
          + ("；成片与素材同画幅时排在最右、画幅取向不同时另起一行" if three else "")
          + "·声音取源片）")
    if not three:
        _info("成片段出来后再跑一次即得三合一")
        return
    # 只报不记：记录归 music 阶段（持章节锁）写，这里没有锁
    sync = control_mod.estimate_lag(s["control"], s["clip"],
                                    seconds=float(rec.get("seconds") or s.get("dur") or 0))
    _info(f"对拍：{control_mod.describe_sync(sync)}")


@_op_locked("control-delete")
def cmd_control_delete(args):
    project = Project.load(_project_path(args))
    r = control_mod.delete_asset(project, args.asset)
    _step(f"素材已删除 · {r['asset']}")


def cmd_control_list(args):
    project = Project.load(_project_path(args))
    items = control_mod.list_assets(project)
    bound = {}
    for s in project.data.get("shots") or []:
        rec = (s.get("gen") or {}).get("control") or {}
        if rec.get("asset"):
            bound.setdefault(rec["asset"], []).append(s)
    if getattr(args, "json", False):
        print(json.dumps({"assets": items,
                          "bound": {k: [s.get("id") for s in v] for k, v in bound.items()}},
                         ensure_ascii=False, indent=2))
        return
    if not items:
        print("本章还没有控制视频素材。用 `control build --source <视频>` 处理一段。")
        return
    for a in items:
        src = a.get("source") or {}
        head = f"  {a['id']:<24} {a.get('status', '?'):<12} {a.get('people', 0)} 人"
        if src:
            head += f" · {src.get('seconds', 0):.1f}s @ {src.get('fps', 0):g}fps"
        print(head)
        if a.get("error"):
            print(f"      ✗ {a['error']}")
        for s in bound.get(a["id"], []):
            rec = (s.get("gen") or {}).get("control") or {}
            drift = control_mod.control_drift(
                s, control_mod.build_digest(project, a["id"]))
            print(f"      → 镜 {s.get('id')} @ {rec.get('start', 0):g}s "
                  f"× {rec.get('seconds', 0)}s"
                  + (f"  ⚠ {drift}——重绑一次即可" if drift else ""))


def cmd_control_fetch(args):
    from .control import weights as weights_mod
    lack = weights_mod.missing()
    if args.check:
        if not lack:
            print(f"✓ 深度捕捉权重齐备（{weights_mod.weights_dir()}）")
            return
        for name, url, note in lack:
            print(f"  ✗ {name} —— {note}\n    {url}")
        return 1
    if not lack:
        print("✓ 权重已齐备，无需下载。")
        return
    _step(f"下载 {len(lack)} 份权重 → {weights_mod.weights_dir()}")
    for name in weights_mod.fetch():
        _info(f"✓ {name}")


# ---------- sketch（简笔分镜预演板）：生成 / 仲裁 / 摘除 / 清单 ----------
def _cast_sheet_refs(project, s, cap: int = 4) -> list[tuple[str, str]]:
    """本镜出场角色的 `(名字, 设定图本地路径)`（≤cap 组，`lineage.required_refs`
    同一真源）。简笔板生成专用——板只需要角色可辨认，场景/道具归 `_video_sheet_refs`。"""
    from .storage.media import ensure_local
    out: list[tuple[str, str]] = []
    for r in lineage.required_refs(project, s):
        if r.get("kind") == "character" and r.get("path") and len(out) < cap:
            p = ensure_local(r["path"])
            if p and Path(p).is_file():
                out.append((r.get("name") or "", str(p)))
    return out


def _video_sheet_refs(project, s, cap: int = 6,
                      exclude_path=None) -> tuple[list[tuple[str, str, str]], list[str]]:
    """随视频请求附发的设定图组合 `(kind, 名字, 本地路径)`（≤cap 组）与**被配额裁掉
    的项**。角色优先、场景次之、道具殿后——身份漂移的代价最大，配额紧张时角色优先。

    场景基准图按命中逐条发（成品画面：陈设、材质与光线），**俯视布局图每镜至多一张**
    （空间图纸：边界、通行、轴线与机位站位），紧跟它自己的那张基准图。视频模型要在
    空间里推镜头，只给一张斜着看的画面时，它对「镜头此刻在屋里哪个位置、两个人谁在左
    谁在右」没有依据，运镜一动空间就重编一遍；图纸补的正是这一半。哪一张由
    `lineage.primary_layout_ref` 定（该镜的主场景），主场景没有图纸时一张都不发——
    别处的平面图比不发更坏，且逐场景各挂一张会把作者点名的道具挤出配额。
    **图侧（`design_refs`）刻意不挂**：分镜图定的是这一帧长什么样，8 张
    参考位挤掉一张角色设定图换来一张平面图，是拿身份一致性换空间提示。

    取材与图侧挂载同一真源 `lineage.required_refs`（显式白名单 ∪ 文本命中）＋与它
    同源的 `lineage.primary_layout_ref`，两侧各扫一遍迟早口径分叉。cap 是硬预算
    （调用方按「7−板」传入）：seedance 全图限额 ≤9、引擎侧 ref_images 钳 7，
    分镜图占 image 参数、简笔板占一席——超出配额的项若交由下游静默丢弃，丢谁不可
    预期，故在此定序裁剪。

    **裁掉了谁必须报出来**：`shots[].props` 里显式点名的道具，也可能因为同镜挂了
    五个场景而排在配额之外被丢掉，而请求照常发出、账照常计。到 cap 静默 `break`
    的话，被丢的项在日志、dry-run 清单和快照里都不留痕，看到的只有「设定图×7」
    ——读起来像点名的东西都发出去了。返回第二位即被裁清单（面向人的
    「类别「名字」」串），由调用方在报价与真发两处同时点名。
    **只统计真有图的项**：没生成设定图的挂载归就绪度节点管，混进来会把两类问题
    说成一类。

    kind 随行是给提示词的 @图片N 职责绑定用的（`prompts.sheet_binding_clause`）：
    编号与措辞都要按种类点名，只给路径就得让拼装侧猜这张图是谁。全局固定场景
    （`scene:main` / `scene_top:main`）单独派生 `scene_main` / `scene_top_main`
    两档——它们的 `name` 恒是字面「场景」，套进具名模板会产出「为场景「场景」的
    设定图」这种指向无身份资产的指令。"""
    from .storage.media import ensure_local
    rows = lineage.required_refs(project, s)
    lay = lineage.primary_layout_ref(project, s)
    # 按 key 而非 name 认领：取景地正好叫「场景」时与全局档的 `scene:main` 撞名
    lay_key = lay["key"] if lay else None
    ordered = [r for r in rows if r.get("kind") == "character"]
    for r in rows:                      # 主场景那张图纸紧跟它自己的基准图
        if r.get("kind") != "scene":
            continue
        ordered.append(r)
        if lay_key == "scene_top:" + str(r.get("key", "")).split(":", 1)[-1]:
            ordered.append(lay)
    ordered += [r for r in rows if r.get("kind") == "prop"]
    label_zh = {"character": "角色", "scene": "场景", "scene_top": "场景俯视图",
                "prop": "道具"}
    kind_main = {"scene:main": "scene_main", "scene_top:main": "scene_top_main"}
    out: list[tuple[str, str, str]] = []
    dropped: list[str] = []
    for r in ordered:
        if not r.get("path"):
            continue
        p = ensure_local(r["path"])
        if not (p and Path(p).is_file()):
            continue
        if exclude_path and str(p) == str(exclude_path):
            # 降级路线下主场景基准图已顶 image 位——再进清单就是同一张图发两次、
            # 白占一席，且契约句与职责句会对它各说一套。整行跳过、不计配额不计裁
            continue
        if str(p) in {q for _k, _n, q in out}:
            # 两个实体共用同一张图（登记别名/复制项目的相对路径改写）：同一文件
            # 发两次不增加信息，还会撞 RefPlan 的路径查重——只保首次命中那一席
            continue
        if len(out) >= cap:
            dropped.append(f"{label_zh.get(r.get('kind'), '设定')}"
                           f"「{r.get('name') or ''}」")
            continue
        kind = kind_main.get(r.get("key")) or r.get("kind")
        out.append((kind, r.get("name") or "", str(p)))
    return out, dropped


def _sketch_refs(project, s, store):
    """板生成的参考图组合（顺序与 `board_prompt` 的职责声明逐条对应）：
    ① 内置版式样板（在盘的全附，同版式多示例）② 该镜分镜图（有则附——素描以它
    为画面基准）③ 该镜出场角色设定图（`_cast_sheet_refs`，≤4 张）。
    **刻意不注入项目参考库 moodboard**——垫图锁的是成片画风，而板是素描基调、
    不掺成片画风，彩色垫图混进来会把成片色引入素描板。"""
    refs = []
    tpls = sketch_mod.templates()
    refs.extend(str(p) for p in tpls)
    img = project.image_for(s, project.aspect)
    with_img = bool(img and has_file(img))
    if with_img:
        refs.append(str(img))
    sheets_ = _cast_sheet_refs(project, s)
    refs.extend(p for _n, p in sheets_)
    return refs, bool(tpls), with_img, [n for n, _p in sheets_ if n]


def stage_sketch_gen(project, store, router, s, prof=None) -> str | None:
    """为单镜就地生成简笔板——gen-video 降级轮与 closeup 预生板共用的出口。

    与 `cmd_sketch_gen` 同一批真源：秒段=`voicecast.request_seconds`、提示词=
    `board_prompt`、参考=`_sketch_refs`、登记=`register_board`、记账=`add_cost`。
    拆不出拍返回 None（beats 是创作资产，引擎绝不代编——自动拆拍已覆盖
    video_prompt/action/end_state 的句读）。主线程串行调用：登记与记账都写文档。
    """
    prov, _params = router.resolve("image", prof or project.profile)
    lang = getattr(prov, "prompt_lang", "zh")
    w, h = store.canvas(sketch_mod.BOARD_ASPECT)
    adir = project.subdir("audio")
    total = voicecast.request_seconds(s, project.motion, adir=adir) \
        or float(s.get("dur") or 0) or None
    beats, auto = sketch_mod.effective_beats(s, total)
    if not beats:
        return None
    out = sketch_mod.board_out(project, s)
    refs, with_tpl, with_img, chars = _sketch_refs(project, s, store)
    scene_names = [str(x).strip() for x in (s.get("scenes") or []) if str(x).strip()]
    prompt = sketch_mod.board_prompt(
        s, lang=lang, with_template=with_tpl, with_shot_image=with_img,
        char_names=chars or None, scene_name="、".join(scene_names) or None,
        total=total)
    res = prov.generate(prompt, str(out), ref_images=refs, width=w, height=h,
                        label=f"SKETCH BOARD SHOT {s['id']}")
    path = getattr(res, "path", None) or str(out)
    cost = float(getattr(res, "cost", 0) or 0)
    # 登记先于记账（parallel 铁律③）：入账抛额度异常时这张已付费的板必须已在档
    sketch_mod.register_board(project, s, path, prompt=prompt, provider=prov.name,
                              cost=cost, refs=len(refs), auto=auto, seconds=total)
    # 付费产物登记即落盘：本函数在 gen-video 计划期被逐镜调用，随后任何一镜的硬拦
    # 都会让整批不经收尾 save 退出，板在盘而文档无登记无账、重跑再买
    try:
        if cost > 0:
            project.add_cost("image", cost)
    finally:
        project.save()
    return path


def stage_sketch_boards(project, store, router, shots, *, prof=None, force=False,
                        note=None, concurrency=None) -> dict:
    """按 beats 为多镜并发生成简笔板（三段式：主线程排计划 → 工作线程只产文件 →
    主线程按提交顺序登记与记账），`cmd_sketch_gen` 与 gen-video 降级轮共用。

    返回 {"boards": {镜号: 板路径}, "failed": [Done], "no_beats": [镜号], "budget_err": 异常|None}：
    缺 beats 的镜跳过（beats 是指挥层写的创作资产，引擎绝不代编）；已有板幂等跳过
    （`force` 重生，直接覆盖——板是预演观看物，不走分镜图那套版本栈）；预算断闸只停派，
    已完成的板照常登记，异常交调用方裁决。单镜同步出口 `stage_sketch_gen` 走同一批真源。"""
    prov, _params = router.resolve("image", prof or project.profile)
    lang = getattr(prov, "prompt_lang", "zh")
    w, h = store.canvas(sketch_mod.BOARD_ASPECT)   # 板恒 16:9（阅读物，与项目比例无关）
    adir = project.subdir("audio")
    no_beats, items = [], []
    for s in shots:
        # 板的秒段与 gen-video 请求秒数**同源**（voicecast.request_seconds 单一真源）
        # ——dur 在 kenburns 折着停顿、dubbed 按配音实测，裸用 dur 画出来的板
        # 就是一份对不上片长的假节奏脚本。**必须先于 effective_beats 算好**：
        # 自动拆拍的拍数按它收敛（5s 镜切 4 拍而非按句读切 6 拍）
        total = voicecast.request_seconds(s, project.motion, adir=adir) \
            or float(s.get("dur") or 0) or None
        beats, auto = sketch_mod.effective_beats(s, total)
        if not beats:
            no_beats.append(s["id"])
            continue
        out = sketch_mod.board_out(project, s)
        if out.is_file() and not force:
            _info(f"镜 {s['id']}: 简笔板已在（{out.name}），跳过（--force 重生）")
            continue
        cov = sketch_mod.beats_coverage(s, total)
        if cov:
            _info(f"⚠ 镜 {s['id']} beats 秒段体检：{cov}")
        dens = sketch_mod.beats_density(s, total)
        if dens:
            _info(f"⚠ 镜 {s['id']} 拍密度体检：{dens}")
        refs, with_tpl, with_img, chars = _sketch_refs(project, s, store)
        scene_names = [str(x).strip() for x in (s.get("scenes") or []) if str(x).strip()]
        prompt = sketch_mod.board_prompt(
            s, lang=lang, with_template=with_tpl, with_shot_image=with_img,
            char_names=chars or None,
            scene_name="、".join(scene_names) or None,
            note=note, total=total)
        items.append({"shot": s, "out": out, "prompt": prompt, "refs": refs,
                      "beats": len(beats), "auto": auto, "seconds": total})
    result = {"boards": {}, "failed": [], "no_beats": no_beats, "budget_err": None}
    if not items:
        return result
    tasks = [parallel.Task(
        key=str(it["shot"]["id"]),
        run=(lambda it=it: prov.generate(it["prompt"], str(it["out"]),
                                         ref_images=it["refs"], width=w, height=h,
                                         label=f"SKETCH BOARD SHOT {it['shot']['id']}")),
        label=f"镜{it['shot']['id']}板", out=it["out"]) for it in items]
    by_key = {str(it["shot"]["id"]): it for it in items}

    def _apply(d):
        it = by_key[d.key]
        s = it["shot"]
        if not d.ok:
            result["failed"].append(d)
            _info(f"镜 {s['id']}: ✗ 简笔板生成失败 {d.message}")
            return
        path = getattr(d.value, "path", None) or str(it["out"])
        cost = float(getattr(d.value, "cost", 0) or 0)
        # 登记先于记账（parallel 铁律③）：入账抛额度异常时这张已付费的板必须已在档
        sketch_mod.register_board(project, s, path, prompt=it["prompt"],
                                  provider=prov.name, cost=cost, refs=len(it["refs"]),
                                  auto=it["auto"], seconds=it["seconds"])
        if cost > 0:
            try:
                project.add_cost("image", cost)
            except KinemaError as e:
                result["budget_err"] = e
        project.save()
        result["boards"][str(s["id"])] = path
        _info(f"镜 {s['id']}: ✓ 简笔板 {Path(path).name}（{it['beats']} 拍"
              + ("·自动拆拍" if it["auto"] else "")
              + (f" · ¥{cost:.2f}" if cost > 0 else "") + "）")
        if sketch_mod.active_guide(s) != "sketch":
            _info(f"  ⓘ 镜 {s['id']} 当前生效路径是 "
                  f"{sketch_mod.active_guide(s) or '（未配置）'}——要让 gen-video 走这块板，"
                  f"跑 `sketch use --chapter … --shot {s['id']} --guide sketch`")

    parallel.run(tasks, workers=parallel.resolve_workers(concurrency),
                 on_done=_apply, should_stop=lambda: result["budget_err"] is not None,
                 on_progress=parallel.progress_printer("简笔板"))
    return result


@_op_locked("sketch")
def cmd_sketch_gen(args):
    """按 beats 逐镜生成简笔分镜板（一板一图·image provider 计费与分镜图同价）。"""
    project = Project.load(_project_path(args))
    store = ConfigStore.load(args.config)
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    prov, _params = router.resolve("image", args.profile or project.profile)
    w, h = store.canvas(sketch_mod.BOARD_ASPECT)
    shots = [s for s in project.shots
             if not transitions_mod.is_transition(s) and not review.is_omitted(s)]
    only = getattr(args, "only", None)
    if only:
        want = {x.strip() for x in str(only).split(",") if x.strip()}
        shots = [s for s in shots if str(s.get("id")) in want]
    # kenburns 不发 gen-video，而板与拍序列的唯一去处就是视频请求（板绝不进
    # image/clip）——这一档下每张板都是按分镜图同价买来的、不参与成片的产物。
    # **只告警不拦**：「先排戏、再切 native」是正当顺序。
    if not project.uses_seedance:
        _info(f"⚠ 本章 motion={project.motion}，不发 gen-video：板与拍序列都不参与出片，"
              "只作排戏对照，而每张板按分镜图同价计费")
        _info("  → 要让它们进请求，把章节 motion 改成 native 或 dubbed")
    _step(f"简笔分镜板 · {len(shots)} 镜 · {w}×{h}（16:9 固定）· provider={prov.name}")
    r = stage_sketch_boards(project, store, router, shots, prof=args.profile,
                            force=args.force, note=getattr(args, "note", None),
                            concurrency=getattr(args, "concurrency", None))
    if r["no_beats"]:
        _info(f"⚠ {len(r['no_beats'])} 镜没有任何运动设计（video_prompt/action/end_state 全空），"
              "已跳过：镜 " + "、".join(str(x) for x in r["no_beats"]))
        _info("  → 补运动设计，或按 kinema-sketchboard skill 写 shots[].sketch.beats"
              "（authored beats 恒优先于自动拆拍）")
    if r["budget_err"]:
        raise r["budget_err"]
    if r["failed"]:
        # 与 gen-image/gen-video/tts 同一收尾纪律：失败必须以非零退出码收场——
        # Studio 按 returncode 映射 done/failed，exit 0 会让前端弹「生成完成」，
        # 而下游 _warn_sketch 的告警全都要求板在盘，板没生成就一句都不喊
        raise KinemaError(
            f"{len(r['failed'])} 板生成失败："
            + "、".join(f"{d.label}（{d.message}）" for d in r["failed"])
            + "\n  已成功的板已登记落盘，重跑同一条命令只补失败的镜。")
    if not r["boards"] and not r["no_beats"]:
        _info("没有可生成的简笔板。")
        return
    print("下一步：`gen-video --chapter … --dry-run` 审时间轴提示词"
          "（缺省档板在盘即附发；衔接章要走参考孤岛的镜才需 sketch ref）；"
          "与 3D previz、控制视频互斥，多配置时用 "
          "`sketch use --guide sketch|previz|control` 表态")

def cmd_sketch_use(args):
    """逐镜表态运动预演路径：previz / control / sketch，或 auto（清除表态·回到自动仲裁）。"""
    project = Project.load(_project_path(args))
    ids = None
    if not getattr(args, "all", False):
        if getattr(args, "shot", None) is None:
            raise ProjectError("要么 --shot N 要么 --all")
        ids = {int(args.shot)}
    changed = []
    for s in project.shots:
        if transitions_mod.is_transition(s):
            continue
        if ids is not None and s.get("id") not in ids:
            continue
        if args.guide == "auto":
            s.pop("guide", None)
        else:
            s["guide"] = args.guide
        changed.append(s)
    if ids is not None and not changed:
        raise ProjectError(f"找不到镜 {args.shot}")
    project.save()
    for s in changed:
        act = sketch_mod.active_guide(s)
        print(f"  镜{s['id']}: guide={s.get('guide') or 'auto'} → 生效路径="
              + (act or "（三路都没配·普通首帧生成）"))


def cmd_sketch_ref(args):
    """逐镜开关「板作参考」——只在**章级衔接开启的章**里才改变行为。

    缺省档（章不衔接）本就是全能参考：板在盘即随请求附发，此开关与缺省行为重合、
    开不开都一样。开了章级 `frame_chain: true` 的章里它才是取舍开关：该镜强制走
    参考任务（板/分镜图/设定图全挂 reference_image），代价是这一镜没有首/末帧槽
    ——既不向下一镜发末帧，上一镜也焊不到它身上，两侧接缝由
    `framechain.sync_seams` 自动补 0.1s 无缝转场（链上孤岛）。
    关：衔接章里板只当拍表，beats 照编成分段时间轴随提示词发出，衔接照旧。
    """
    project = Project.load(_project_path(args))
    ids = None
    if not getattr(args, "all", False):
        if not getattr(args, "shots", None):
            raise ProjectError("要么 --shots 3,14 要么 --all")
        # 镜号按字符串比对（与 --only 同一惯例）——坏输入归到「找不到镜」，不炸栈
        ids = {x.strip() for x in str(args.shots).replace("，", ",").split(",") if x.strip()}
    on = args.state == "on"
    changed = []
    for s in project.shots:
        if transitions_mod.is_transition(s):
            continue
        if ids is not None and str(s.get("id")) not in ids:
            continue
        sketch_mod.set_reference(s, on)
        changed.append(s)
    if ids is not None and len(changed) != len(ids):
        missing = sorted(ids - {str(s.get("id")) for s in changed})
        raise ProjectError(f"找不到镜 {missing}")
    project.save()
    for s in changed:
        board = sketch_mod.board_of(s)
        ready = bool(board and has_file(board))
        print(f"  镜{s['id']}: 板作参考={'开' if on else '关'}"
              + ("" if not on else
                 ("（板在盘·下次 gen-video 生效）"
                  if ready else "（⚠ 该镜还没有板——先 sketch gen，否则此开关不生效）")))
    # 开关一动孤岛集合就变了，软切当场同步（不留到 gen-video 才改结构）；
    # 缺省章（不衔接）没有缝的概念，这一步只会把历史遗留的自动软切撤干净
    _sync_island_seams(project, project.frame_chain,
                       _v2v_enabled(project, False) and project.native_audio,
                       _control_enabled(project, False) and project.native_audio)
    if on and project.frame_chain:
        _info("提示：本章开着首尾帧衔接——开「板作参考」的镜是链上孤岛，"
              "开得越多断掉的接缝越多（每断一处补一个 0.1s 软切）。"
              "只给真需要板级调度的镜开。")
    elif on:
        _info("ⓘ 本章未开首尾帧衔接（缺省档）——全能参考本就是缺省行为，"
              "板在盘即自动附发，此开关只在衔接章里才改变行为。")


def cmd_sketch_clear(args):
    project = Project.load(_project_path(args))
    r = sketch_mod.clear_board(project, args.shot)
    if r["dropped"]:
        print(f"✓ 镜 {r['shot']} 已摘除简笔板挂载: {', '.join(r['dropped'])}"
              "（板文件保留在 sketch/ 目录，beats 不动）")
    else:
        print(f"镜 {r['shot']} 本来就没有简笔板挂载")


def cmd_sketch_list(args):
    project = Project.load(_project_path(args))
    adir = project.subdir("audio")
    rows = [s for s in project.shots if not transitions_mod.is_transition(s)]
    _step(f"简笔分镜板 · {len(rows)} 正镜")
    n_beats = n_board = 0
    for s in rows:
        # total 先行：自动拆拍的拍数按它收敛（与 sketch gen 同一口径，否则清单
        # 报的拍数与真生成时的拍数对不上）
        total = voicecast.request_seconds(s, project.motion, adir=adir) \
            or float(s.get("dur") or 0) or None
        beats, auto = sketch_mod.effective_beats(s, total)
        board = sketch_mod.board_of(s)
        n_beats += bool(beats)
        n_board += bool(board and has_file(board))
        act = sketch_mod.active_guide(s)
        src = "自动" if auto else ("自定义" if beats else "—")
        gs = (s.get("gen") or {}).get("sketch") or {}
        marks = ""
        cov = sketch_mod.beats_coverage(s, total)
        if cov:
            marks += " ⚠秒段"
        dens = sketch_mod.beats_density(s, total)
        if dens:
            marks += " ⚠拍密度"
        drift = sketch_mod.board_drift(s)   # 漂移判据唯一真源（scanner 同款）
        if drift and drift.get("dur"):
            marks += f" ⚠时长已变({drift['dur']['was']}→{drift['dur']['now']}s)"
        if drift and drift.get("beats"):
            marks += " ⚠拍序列已变"
        # 生效档：sketch·板随发（板在盘——缺省全能参考档下随请求附发；衔接章里
        # 须显式 `sketch ref --state on` 才走参考孤岛）/ sketch·纯时间轴（无板，
        # 分段时间轴照发）/ previz / —
        act_disp = act or "—"
        if act == "sketch":
            has_board = bool(board and has_file(board))
            act_disp = "sketch·板随发" if has_board else "sketch·纯时间轴"
            if sketch_mod.reference_opt_in(s) and not has_board:
                marks += " ⚠开了板作参考却没有板"
        print(f"  镜{s['id']:>3} · 拍 {len(beats) or '—':>2}({src}) ·"
              f" 板 {'✓' if board and has_file(board) else '—'}"
              + (f"@{gs.get('seconds')}s" if gs.get("seconds") else "")
              + f" · guide={s.get('guide') or 'auto':<7} · 生效={act_disp}" + marks)
        if cov:
            _info(f"    ↳ {cov}")
        if dens:
            _info(f"    ↳ {dens}")
    print(f"  合计: 拆拍就绪 {n_beats}/{len(rows)} · 板 {n_board}/{len(rows)}"
          "（自动=按运动设计句读拆拍；写 shots[].sketch.beats 可精确控拍；"
          "⚠时长已变=板生成后 dur 改了 · ⚠拍序列已变=beats/提示词改了板没跟上，"
          "两者都建议 --force 重生）")


def cmd_ledger(args):
    from .business import project_ledger
    ws = Workspace.open(args.workspace, create=False)
    r = project_ledger(ws, args.project)
    t = r["totals"]
    print(f"成本台账 · {r['project']} 「{r['title']}」"
          + (f" · 模板 {r['template']}" if r["template"] else ""))
    # 预估列只有视频，故实际也并排一列视频——两列同口径才可对账；合计列另给，
    # 它含图/配音/音乐，与预估并排会被读成「计费溢出」
    print(f"  {'章节':<10}{'时长':>8}{'镜数':>5}{'预估(video)':>12}{'实际(video)':>12}"
          f"{'实际合计':>10}{'废片':>10}{'重roll':>7}")
    for row in r["chapters"]:
        est = f"¥{row['estimate_video']:.2f}" if row["estimate_video"] else "—"
        print(f"  {row['chapter']:<10}{row['duration']:>7.1f}s{row['shots']:>5}"
              f"{est:>12}  ¥{row['actual_video']:>9.2f}  ¥{row['actual_total']:>7.2f}"
              f"  ¥{row['waste']:>7.2f}{row['rerolls']:>7}")
    if t.get("series_total"):
        print(f"  {'系列':<10}{'':>8}{'':>5}{'—':>12}  {'—':>10}  ¥{t['series_total']:>7.2f}"
              "  （设定图/主视觉/试音，不摊到镜）")
    print(f"  合计: 实际 ¥{t['actual']:.2f} · 废片 ¥{t['waste']:.2f}（占比 {t['waste_ratio']:.0%}）"
          + (f" · 视频 预估 ¥{t['estimate_video']:.2f} / 实际 ¥{t['actual_video']:.2f}"
             if t["estimate_video"] else ""))
    by = t.get("rerolls_by") or {}
    by_note = ("（" + " / ".join(f"{k} {n}" for k, n in sorted(by.items())) + "）"
               if by else "")
    print(f"  运营指标: 单镜成本 ¥{t['cost_per_shot']:.3f} · 单镜重roll {t['rerolls_per_shot']:.2f} 次{by_note}"
          f" · 总时长 {t['duration'] / 60:.1f} 分钟 · {t['shots']} 镜")


def _watermark_specs(store, project, args):
    """解析本次要烧的三类水印 → (floating, fixed, bottom)，任一为 None 表示不烧该类。
      · 漂移水印 floating：文案链 --text > project.watermark > branding.watermark.text；
      · 固定角标 fixed：文案链 --corner-text > project.watermark_fixed.text > branding；
        位置链 --corner-pos > project.watermark_fixed.position > branding（缺省 br）；
        字号缺省 = 该项目字幕字号 ×0.52（「比字幕小四号」），也可 project/branding 直给。
      · 底部水印 bottom：文案链 --bottom-text > project.watermark_bottom.text > branding
        （底部居中·半透明·无描边无底衬，样式细节见 watermark.build_bottom_filter）。
    --from-project（Studio 走）：文案**只认 project 字段**、不回落 branding——保证
    UI 里清空某类水印后重烧就真的没有它（否则 branding 全局默认会把它加回来）。"""
    from .branding import load_branding
    bland = load_branding()
    wm_cfg = bland.get("watermark") or {}
    fx_cfg = bland.get("watermark_fixed") or {}
    bt_cfg = bland.get("watermark_bottom") or {}
    proj_fx = project.data.get("watermark_fixed") or {}
    proj_bt = project.data.get("watermark_bottom") or {}
    from_project = bool(getattr(args, "from_project", False))

    ftext = (project.data.get("watermark") or "").strip() if from_project else (
        (getattr(args, "text", None) or "").strip()
        or (project.data.get("watermark") or "").strip()
        or (wm_cfg.get("text") or "").strip())
    floating = {"text": ftext, "size": wm_cfg.get("size"),
                "opacity": float(wm_cfg.get("opacity", 0.30)),
                "color": wm_cfg.get("color", "white"),
                "speed": float(wm_cfg.get("speed", 3.0)),
                "fade": float(wm_cfg.get("fade", 0.6))} if ftext else None

    ctext = (proj_fx.get("text") or "").strip() if from_project else (
        (getattr(args, "corner_text", None) or "").strip()
        or (proj_fx.get("text") or "").strip()
        or (fx_cfg.get("text") or "").strip())
    if ctext:
        pos = (getattr(args, "corner_pos", None) or proj_fx.get("position")
               or fx_cfg.get("position") or "br")
        size = proj_fx.get("size") or fx_cfg.get("size")
        if not size:                     # 缺省「比字幕小四号」= 字幕字号 ×0.52
            sub_size = int(_sub_cfg(store, project).get("size", 58) or 58)
            size = max(14, round(sub_size * 0.52))
        fixed = {"text": ctext, "position": pos, "size": int(size),
                 "opacity": float(proj_fx.get("opacity", fx_cfg.get("opacity", 1.0))),
                 "color": proj_fx.get("color") or fx_cfg.get("color", "white"),
                 "font": proj_fx.get("font") or fx_cfg.get("font")}
    else:
        fixed = None

    btext = (proj_bt.get("text") or "").strip() if from_project else (
        (getattr(args, "bottom_text", None) or "").strip()
        or (proj_bt.get("text") or "").strip()
        or (bt_cfg.get("text") or "").strip())
    bottom = {"text": btext,
              "size": proj_bt.get("size") or bt_cfg.get("size"),
              "opacity": float(proj_bt.get("opacity", bt_cfg.get("opacity", 0.55))),
              "color": proj_bt.get("color") or bt_cfg.get("color", "white"),
              "margin": proj_bt.get("margin") or bt_cfg.get("margin"),
              "font": proj_bt.get("font") or bt_cfg.get("font")} if btext else None
    return floating, fixed, bottom


@_op_locked("watermark")
def cmd_watermark(args):
    """成片后处理：漂移水印（防搬运）+ 固定角标（品牌署名）+ 底部水印（半透明署名）。
    原片不动，产出 <id>_wm_<比例>.mp4 双版本并存；三类可任意组合（一次重编码一起烧）。

    漂移路线由 seed 确定性生成（缺省从文案+时长派生），重复执行产出相同水印，幂等。"""
    from .pipeline import watermark as wm_mod
    from .storage.media import ensure_local

    project = _load_video(args)
    store = ConfigStore.load(args.config)
    ensure_tools()
    floating, fixed, bottom = _watermark_specs(store, project, args)
    if not floating and not fixed and not bottom:
        raise KinemaError(
            "没有水印可打。漂移水印文案（--text / project.watermark / branding）、"
            "固定角标文案（--corner-text / project.watermark_fixed.text / branding）与"
            "底部水印文案（--bottom-text / project.watermark_bottom.text）至少给一个。")
    outputs = {a: p for a, p in (project.data.get("output") or {}).items()
               if isinstance(p, str) and p}
    if not outputs:
        raise KinemaError("没有成片可打水印（output 为空）——先 assemble/compose 出片。")
    kinds = " + ".join(x for x in [
        f"漂移「{floating['text']}」" if floating else "",
        f"角标「{fixed['text']}」@{fixed['position']}" if fixed else "",
        f"底部「{bottom['text']}」" if bottom else ""] if x)
    _step(f"成片水印 · {kinds} · {len(outputs)} 个比例（原片保留）")
    done = project.data.setdefault("output_wm", {})
    for asp, path in outputs.items():
        src = Path(ensure_local(path))
        if not src.is_file():
            _info(f"[{asp}] 成片缺失，跳过: {src}")
            continue
        tag = aspect_tag(asp)
        stem = src.stem
        base = stem[: -(len(tag) + 1)] if stem.endswith(f"_{tag}") else stem
        out = src.with_name(f"{base}_wm_{tag}{src.suffix}")
        if out.is_file() and not args.force:
            _info(f"[{asp}] 已存在，跳过（--force 重打）: {out.name}")
            done[asp] = str(out)
            continue
        wm_mod.apply(src, out, floating=floating, fixed=fixed, bottom=bottom)
        done[asp] = str(out)
        _info(f"[{asp}] 水印版: {out.name}（原片: {src.name}）")
    project.save()
    print("✓ 水印完成 · 水印版与无水印版并存于 output/（水印版路径记录在 output_wm）")


def cmd_verify(args):
    """成片自审：对已出的成片跑一遍机器体检（黑屏 / 该响却哑 / 削波 / 响度 /
    时长对不上 / 字幕缺条），结论写进 project.json 顶层 `verify`。

    **默认不接进 assemble/run**（对齐 `--draft` 逃生舱哲学：合成该出片就出片，
    体检是另一道自愿闸）；网页「自审」按钮走同一条命令。
    阈值全部在真实成片上标定，见 pipeline/mediacheck 模块头的标定表。
    零 API 成本（纯本地 ffmpeg 探测），只读产物、只写 verify 字段。"""
    project = _load_video(args)
    store = ConfigStore.load(args.config)
    ensure_tools()
    aspects = [args.aspect] if getattr(args, "aspect", None) else project.aspects
    lang = _sub_cfg(store, project, args.profile).get("lang") or "zh"
    _step(f"成片自审 · 比例 {aspects} · 抽样 {args.samples} 点/比例（零成本）")
    rep = mediacheck_mod.verify(project, store, aspects=aspects,
                                samples=args.samples, sub_lang=lang)
    # 体检跑十几秒，只写 verify 一个键：按磁盘现状落盘，不用这份旧副本整份写回
    Project.mutate(project.path, lambda p: p.data.__setitem__("verify", rep))
    # flush=True：硬失败时下面要抛错，stderr 不刷 stdout 会让报告显示在错误行之后
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2), flush=True)
    else:
        for line in mediacheck_mod.report_lines(rep):
            print(line, flush=True)
    bad = [a for a, b in rep.items()
           if a not in ("at", "voice") and isinstance(b, dict) and not b.get("ok")]
    todo = sum(len(b.get("todo") or []) for a, b in rep.items()
               if a != "at" and isinstance(b, dict))
    n_asp = sum(1 for a, b in rep.items()
                if a not in ("at", "voice") and isinstance(b, dict))
    if bad:
        raise KinemaError(
            f"⊘ 自审未通过：{len(bad)} 个比例有硬失败（{'、'.join(bad)}）——"
            "修完重跑 assemble 再 verify")
    print(f"✓ 自审通过 · {n_asp} 个比例"
          + (f" · {todo} 项待修（不拦，见上）" if todo else ""))


# ---------- 角色跨镜一致性：引擎产料 → 指挥层判定 → CLI 回填 ----------
def cmd_consistency_scan(args):
    """产料：逐镜代表帧 + 角色设定图配对清单（零 API 成本，**引擎不打分**）。

    kenburns 的"代表帧"就是分镜图本身（缩放拷贝，不必抽帧）；dubbed/native 从
    片段中点抽帧。产物落 `<章节>_work/consistency/`，指挥层照 manifest.json
    逐镜 Read 图片做多模态比对，再用 `consistency set` 回填判定。
    为什么不自动打分见 pipeline/consistency 模块头（ffmpeg 无人脸检测、
    像素度量对换姿态无判别力、CLIP 比的是版式不是角色）。"""
    project = _load_video(args)
    ensure_tools()
    _step(f"角色一致性产料 · {project.id}（零成本；引擎只产料，判定交指挥层）")
    man = consistency_mod.scan(project, only=args.only, aspect=args.aspect,
                               stage=args.stage)
    if args.json:
        print(json.dumps(man, ensure_ascii=False, indent=2))
        return
    for line in consistency_mod.report_lines(man):
        print(line, flush=True)
    s = man["summary"]
    print(f"✓ 产料完成 → {Path(man['dir']) / consistency_mod.MANIFEST}")
    if s["no_compare"]:
        print(f"   ⚠ 有 {s['no_compare']} 镜没有可比对角色（原因见上）——"
              "这不等于「比对通过」，别当成绿灯")
    print("   下一步：逐镜 Read 代表帧与角色设定图比对，然后\n"
          "     python3 -m kinema consistency set --chapter <项目/章节> --shot N "
          "--verdict ok|drift [--score 0~1] [--note ...] [--retake]")


def cmd_consistency_set(args):
    """回填指挥层判定到 `shots[].consistency`（引擎不自动打分，score 是主观分）。

    `--retake` 判漂移时打回重做：未锁定镜置 retake（下次生成自动重生+旧版归档），
    已通过锁定的镜只留判定当标记——done 由人工置定，引擎不自动解除。
    dubbed/native 判 clip 漂移会**连 image 一起打回**（根因几乎总在分镜图）。"""
    def fn(project):
        target = next((s for s in project.shots if str(s.get("id")) == str(args.shot)), None)
        if target is None:
            raise KinemaError(f"找不到镜 {args.shot}")
        return consistency_mod.set_verdict(project, target, args.verdict, score=args.score,
                                           note=args.note, by=args.by, retake=args.retake)
    r = Project.mutate(_project_path(args), fn)
    e = r["entry"]
    mark = "✓ 一致" if args.verdict == "ok" else "⚠ 漂移"
    print(f"✓ 镜 {args.shot} 角色一致性 → {mark}"
          + (f" · 分 {e['score']}" if "score" in e else "")
          + (f" · {args.note}" if args.note else "") + f" · 判定人 {e['by']}")
    if not e.get("frame"):
        print("   ⓘ 本次判定未挂产料存证（尚未 consistency scan 或该镜不在清单里）")
    if r["retaken"]:
        print(f"   已置重做: {'、'.join(r['retaken'])}"
              + ("（clip 漂移的根因几乎总在分镜图，故连 image 一并打回）"
                 if "clip" in r["retaken"] else "")
              + " —— 下次运行对应阶段强制重生，旧版自动归档。")
    if r["locked"]:
        print(f"   ⊘ {'、'.join(r['locked'])} 已通过锁定，未代你解锁——"
              "判定已留在 shots[].consistency；确要重做请 review set --state retake")
    if args.verdict == "drift" and not args.retake:
        print("   （未加 --retake：只登记判定，不动审阅状态）")


# ---------- 决策审计：指挥层的取舍逐条留痕（append-only） ----------
def cmd_decision_add(args):
    """追加一条决策到章节文档 `decisions[]`（**必须走本命令，不要裸改 JSON**）。

    裸改会被两条路径静默吞掉：① 引擎长任务持有旧内存副本逐镜 save 时整份覆写；
    ② mysql 模式下库行更新时间较新会直接覆写本地文件（`Project.load` 之前就没了）。
    本命令按磁盘现状追加（`Project.mutate`），写盘顺带 upsert 入库，上述两条覆写都不会发生。"""
    def fn(project):
        e = decisions_mod.add(project.data, choice=args.choice,
                              alternatives=args.alt or [], why=args.why or "",
                              confidence=args.confidence)
        return e, len(decisions_mod.entries(project.data))
    entry, total = Project.mutate(_project_path(args), fn)
    print(f"✓ 决策已记录 #{entry['id']} · 置信度 {entry['confidence']} · {entry['at']}")
    print(f"   决定: {entry['choice']}")
    if entry["alternatives"]:
        print(f"   备选: {' / '.join(entry['alternatives'])}")
    if entry["why"]:
        print(f"   理由: {entry['why']}")
    print(f"   共 {total} 条 —— 查看: decision list")


def cmd_decision_list(args):
    """列出章节的决策记录（append-only 审计日志，只增不改不删）。"""
    project = _load_video(args)
    if args.json:
        print(json.dumps(decisions_mod.entries(project.data), ensure_ascii=False, indent=2))
        return
    items = decisions_mod.entries(project.data)
    _step(f"决策审计 · {project.id} · {len(items)} 条")
    for line in decisions_mod.report_lines(project.data):
        print(line)


def cmd_transition(args):
    """转场镜管理：两镜之间插入「转场镜」——渐黑/白闪字卡或素材转场。

    转场即特殊镜（kind=transition，narration 空 → 静音占位自动对齐时间轴）：
    生图/配音/图生视频全跳过（零 API 成本），字卡由合成段本地渲染；相邻镜自动
    加边缘淡化——观感即「画面渐暗 → 黑场显示"一天后" → 渐显下一段」。"""
    path = Path(_project_path(args))
    project = Project.load(path)
    shots = project.shots
    if args.taction == "list":
        rows = [(s.get("id"), transitions_mod.spec_of(s)) for s in shots
                if transitions_mod.is_transition(s)]
        if not rows:
            print("（无转场镜）transition add --after <镜id> --text \"一天后\" 插入")
            return
        for sid, sp in rows:
            auto = next((s for s in shots if s.get("id") == sid
                         and (s.get("transition") or {}).get("auto")), None)
            print(f"  镜{sid} · {sp['type']}"
                  + (f" · 「{sp['text']}」" if sp['text'] else "")
                  + (f" · 素材 {sp['asset']}" if sp.get('asset') else "")
                  + f" · 边缘淡化 {sp['edge']}s"
                  + ("  ⟨自动·孤岛接缝⟩" if auto else ""))
        return
    # 改 shots[] 结构：与生成任务同一把操作锁，锁内重新装载
    from .locking import op_lock
    with op_lock(path, kind="transition"):
        project = Project.load(path)
        shots = project.shots
        if args.taction == "sync":
            # 手动同步一次（gen-video 也会自动跑）：零成本先看清结构会被改成什么样
            r = _sync_island_seams(project, project.frame_chain,
                                   _v2v_enabled(project, False) and project.native_audio,
                                   _control_enabled(project, False) and project.native_audio)
            if not (r["added"] or r["removed"]):
                print("✓ 孤岛接缝已是最新（无需增删自动无缝转场）")
            return
        if args.taction == "rm":
            s = next((x for x in shots if str(x.get("id")) == str(args.id)), None)
            if s is None:
                raise KinemaError(f"找不到镜 {args.id}")
            if not transitions_mod.is_transition(s):
                raise KinemaError(
                    f"镜 {args.id} 不是转场镜——普通镜绝不删除（要弃用走 review set --state omt）")
            auto = (s.get("transition") or {}).get("auto") == framechain.AUTO_MARK
            shots.remove(s)
            project.save()
            print(f"✓ 已移除转场镜 {args.id}")
            if auto and project.frame_chain:
                _info("⚠ 这是孤岛接缝的自动无缝转场——本章开着首尾帧衔接，只要相邻镜还走"
                      "参考孤岛/V2V，下次 gen-video（或 transition sync）会把它补回来。"
                      "要永久去掉：关掉那一镜的参考模式（`sketch ref --state off`／"
                      "去掉 previz_v2v）、关掉章级衔接，或在同一处手写一个自己的转场"
                      "（手写的不会被顶掉）")
            elif auto:
                _info("ⓘ 这是历史遗留的自动无缝转场——本章未开首尾帧衔接（缺省档镜间直拼），"
                      "它不会被补回来")
            return
        # add：在 --after 指定镜之后插入
        ids = [s.get("id") for s in shots]
        if not any(str(x) == str(args.after) for x in ids):
            raise KinemaError(f"找不到镜 {args.after}（现有: {', '.join(map(str, ids))}）")
        nid = max((int(x) for x in ids if str(x).isdigit()), default=0) + 1
        # 缺省类型：有素材=clip｜有文字=fade_black(总~1s)｜无字=fade 极简黑场呼吸(总~0.5s)
        ttype = transitions_mod.pick_type(args.text, args.asset, args.type)
        spec = {"type": ttype}
        if args.text:
            spec["text"] = args.text
        if args.asset:
            spec["asset"] = args.asset
        if args.edge is not None:
            spec["edge"] = args.edge
        for k in ("direction", "color", "sound"):
            if getattr(args, k, None):
                spec[k] = getattr(args, k)
        shot = {"id": nid, "kind": "transition",
                "dur": transitions_mod.resolve_dur(ttype, args.dur),
                "narration": "", "transition": spec}
        idx = next(i for i, x in enumerate(ids) if str(x) == str(args.after))
        shots.insert(idx + 1, shot)
        project.save()
        sp = transitions_mod.spec_of(shot)
        detail = (f"过渡 {shot['dur']}s·不动相邻镜" if not sp["edge"]
                  else f"淡出 {sp['edge']}s + 停顿 {shot['dur']}s + 淡入 {sp['edge']}s")
        print(f"✓ 转场镜 {nid} 已插入到镜 {args.after} 之后 · {sp['type']}"
              + (f" · 「{sp['text']}」" if sp["text"] else "")
              + f" · 总时长≈{transitions_mod.total_span(shot)}s"
              f"（{detail}；重跑 assemble/compose 生效，tts 自动补静音占位）")


def cmd_cover(args):
    """封面设计：系列主视觉 + 章节封面（默认 3:4 竖版 + 4:3 横版双套，构图方向词随画幅）。

    与字幕/水印同一条「本体无字」路线：模型只画无字背景（key visual 构图，底部留
    标题安全区），主标题与「第 N 集」由 ffmpeg 排版后置——系列与全部章节共用同一
    排版模板、章节背景以系列封面背景为首张参考图、同画风前缀同 seed，三锚点锁死
    系列感：一眼看出是同一部动漫的第几集。产物落 assets/covers/（无字 *_bg.png
    真源与成品并存）；注册系列文档 cover 块 / 章节文档 cover 字段（工作区相对路径）。"""
    store = ConfigStore.load(args.config)
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    router = ModelRouter(store, force_mock=args.mock)
    from .pipeline import cover as cover_mod
    ensure_tools()

    prof = args.profile or s.data.get("profile")
    prov, params = router.resolve("image", prof)
    lang = getattr(prov, "prompt_lang", "zh")
    prefix, _deg = prompts_mod.select_style_prefix(params, lang, doc=s.data)
    # 写实档（image.identity_sheet，读点与 gen-refs 同一个）：工法栈换电影海报档
    photoreal = bool(params.get("identity_sheet"))
    covers = s.dir / "assets" / "covers"
    title = (args.title or s.data.get("title") or s.pid).strip()
    # 阵容显式点名：None=缺省规则（主角优先取前 3）；[]=不注入阵容句。
    # 名字必须命中名册：错名若被静默丢弃，成片阵容与点名意图不一致且无从追查
    cast_names = None
    if getattr(args, "cast", None):
        raw = str(args.cast).strip()
        if raw.lower() in ("none", "无"):
            cast_names = []
        else:
            cast_names = [x.strip() for x in raw.split(",") if x.strip()]
            known = {c.get("name") for c in (s.data.get("characters") or [])}
            bad = [n for n in cast_names if n not in known]
            if bad:
                raise KinemaError(
                    f"--cast 点名了名册里没有的角色: {'、'.join(bad)}"
                    f"（已登记: {'、'.join(sorted(x for x in known if x)) or '无'}）")
    # 比例集合：默认竖 3:4 + 横 4:3 双套；--aspects 自定义任意比例列表；
    # --size 直接给像素（单套，键取归约比例）
    if args.size:
        w0, h0 = (int(x) for x in args.size.lower().replace("*", "x").split("x"))
        import math as _m
        g = _m.gcd(w0, h0)
        targets = {f"{w0 // g}:{h0 // g}": (w0, h0)}
    elif args.aspects:
        targets = {a.strip(): cover_mod.size_for(a.strip())
                   for a in args.aspects.split(",") if a.strip()}
    else:
        targets = {a: cover_mod.size_for(a) for a in cover_mod.DEFAULT_ASPECTS}
    accent = ((store.profile(prof).get("subtitle") or {}).get("accent")) or "#ffd45e"
    font = fonts_mod.resolve_font(args.font, profile=prof)
    seed = (s.data.get("style") or {}).get("seed")
    wsroot = ws.root.resolve()

    def _rel(p):
        try:
            return Path(p).resolve().relative_to(wsroot).as_posix()
        except ValueError:
            return str(p)

    no_mb = getattr(args, "no_moodboard", False)
    mb_active = [] if no_mb else s.moodboard_active()   # 参考库垫图也套用到封面（统一模块风格）

    def _refs(extra=None):
        """参考图：系列风格真源（章节时）→ 角色设定图 → 场景设定图 → 参考库垫图，最多 8 张。"""
        cand = list(extra or [])
        cand += [c["sheet"] for c in (s.data.get("characters") or []) if c.get("sheet")]
        if s.data.get("scene_ref"):
            cand.append(s.data["scene_ref"])
        cand += mb_active
        out = []
        for r in cand:
            q = Path(r) if Path(r).is_absolute() else wsroot / r
            if q.is_file():
                out.append(str(q))
        return out[:8]

    def _primary(reg):
        imgs = reg.get("images") or {}
        return imgs.get("3:4") or next(iter(imgs.values()), None)

    # 封面作业按「对象 × 比例」拆成两段：① 无字背景（`_bg` 真源：Studio 卡片浮层标题与
    # 缩略消费，必须无字）② 题字（缺省 AI 题字以本比例背景为首参考再生成一次，标题画成
    # logo 级设计元素；`--typeset-title` 退回 ffmpeg 排版，字绝对精确）。系列背景是章节
    # 背景的风格锚、系列成品是章节题字的字形锚，故分三个波次并发：系列背景 → 系列题字 +
    # 章节背景 → 章节题字；同一波次内互不依赖。`mk_prompt(asp)` 逐比例拼——构图方向词
    # 随画幅分支，循环外拼一次会给 4:3 横画布也发「竖版海报构图」。
    typeset = getattr(args, "typeset_title", False)
    workers = parallel.resolve_workers(None)
    spent: dict = {}

    def _run_wave(tasks, what) -> set:
        """跑完一个波次，返回失败对象的集合（成功的对象由调用方登记）。"""
        failed: list = []

        def _apply(d):
            if not d.ok:
                failed.append(d)
                _info(f"{d.label}: ✗ {d.message}")
                return
            stem = d.meta["stem"]
            spent[stem] = spent.get(stem, 0.0) + float(getattr(d.value, "cost", 0) or 0)
        if tasks:
            parallel.run(tasks, workers=workers, retries=0, on_done=_apply,
                         on_progress=parallel.progress_printer(what))
        return {d.meta["stem"] for d in failed}

    def _bg_tasks(stem, mk_prompt, refs):
        return [parallel.Task(
            key=f"{stem}:{asp}:bg",
            run=(lambda a=asp, w=w, h=h: prov.generate(
                mk_prompt(a), str(covers / f"{stem}_bg_{aspect_tag(a)}.png"),
                ref_images=refs, seed=seed, width=w, height=h)),
            label=f"{stem} {asp} 背景", meta={"stem": stem})
            for asp, (w, h) in targets.items()]

    def _title_tasks(stem, subtitle, title_refs):
        out = []
        for asp, (w, h) in targets.items():
            t = aspect_tag(asp)
            bg = covers / f"{stem}_bg_{t}.png"
            final = covers / f"{stem}_{t}.png"
            if typeset:
                cover_mod.compose_cover(bg, final, title=title, subtitle=subtitle,
                                        width=w, height=h, accent=accent, font=font)
                continue
            tprompt = cover_mod.title_art_prompt(
                title, subtitle=subtitle or "", style_prefix=prefix, lang=lang,
                series_ref=bool(title_refs))
            refs = [str(bg)] + [str(x) for x in (title_refs or [])]
            out.append(parallel.Task(
                key=f"{stem}:{asp}:title",
                run=(lambda p=tprompt, f=final, r=refs, w=w, h=h: prov.generate(
                    p, str(f), ref_images=r, seed=seed, width=w, height=h)),
                label=f"{stem} {asp} 题字", meta={"stem": stem}))
        return out

    def _registry(stem, old):
        reg = {"images": dict(old.get("images") or {}), "bg": dict(old.get("bg") or {})}
        for asp in targets:
            t = aspect_tag(asp)
            reg["images"][asp] = _rel(covers / f"{stem}_{t}.png")
            reg["bg"][asp] = _rel(covers / f"{stem}_bg_{t}.png")
        return reg

    def _fail(stems):
        raise KinemaError(f"封面生成失败：{'、'.join(sorted(stems))}——已完成的对象已登记，"
                          "重跑同一条命令补失败的")

    # ---- 系列主视觉（章节封面的风格真源，须先于章节存在）----
    series_sub = args.subtitle if not (args.chapter or getattr(args, "all", False)) else None
    scov = s.data.get("cover") or {}
    have_all = scov.get("images") and all(a in scov["images"] for a in targets)
    need_series = _series_cover_needed(bool(args.force), args.chapter, bool(have_all))
    if not need_series:
        _info("系列封面已存在，沿用作风格锚" + ("" if args.chapter else "（--force 重生）"))
    # --desc 系列封面同样消费：若只让章节循环读 args.desc，系列提示词会静默
    # 忽略用户写的 key visual 描述
    srefs = _refs()

    def sprompt(asp):
        return cover_mod.cover_prompt(s.data, desc=(args.desc or "").strip(),
                                      style_prefix=prefix, lang=lang,
                                      ref_base=bool(srefs), aspect=asp,
                                      cast_names=cast_names, photoreal=photoreal)

    # ---- 章节封面清单（第 N 集按章节序号自动）----
    chapters = sorted(s.data.get("chapters") or [], key=lambda c: c.get("order", 0))
    if getattr(args, "all", False):
        wanted = {c.get("id") for c in chapters}
    elif args.chapter:
        wanted = {args.chapter}
        if args.chapter not in {c.get("id") for c in chapters}:
            raise KinemaError(
                f"章节不存在: {args.chapter}"
                f"（现有: {', '.join(c.get('id', '') for c in chapters) or '无'}）")
    else:
        wanted = set()
    jobs: list[dict] = []
    for idx, ch in enumerate(chapters, 1):
        cid = ch.get("id")
        if cid not in wanted:
            continue
        proj = Project.load(s.get_chapter_path(cid))
        ccov = proj.data.get("cover") or {}
        if not isinstance(ccov, dict):
            ccov = {}
        if ccov.get("images") and all(a in ccov["images"] for a in targets) \
                and not args.force:
            _info(f"[{cid}] 封面已存在，跳过（--force 重生）")
            continue
        # 画面命题三级回落：--desc > 章节 cover_prompt > 章节 theme。`run` 收尾的
        # 自动封面没有指挥层写的 desc，theme 是章节文档里唯一现成的一句剧情命题——
        # 没有它就只剩标题氛围句，工法栈会自己编一个世界
        jobs.append({"cid": cid, "proj": proj, "ccov": ccov,
                     "title": ch.get("title") or cid,
                     "sub": args.subtitle or f"第 {idx} 集",
                     "desc": (args.desc or "").strip()
                     or (proj.data.get("cover_prompt") or "").strip()
                     or (proj.data.get("theme") or "").strip()})

    # 波次 1：系列背景
    if need_series:
        _step(f"系列主视觉 · 「{title}」 · {' + '.join(targets)}")
        if _run_wave(_bg_tasks("series", sprompt, srefs), "封面·系列背景"):
            _fail({"series"})
    # 波次 2：系列题字 + 章节背景（章节以系列背景为风格锚，此刻已在盘）
    wave = _title_tasks("series", series_sub, None) if need_series else []
    sbg = (s.data.get("cover") or {}).get("bg") or {}
    anchors = [covers / f"series_bg_{aspect_tag(a)}.png" for a in targets] if need_series else []
    anchors += [wsroot / p for p in sbg.values()]
    anchors += [wsroot / p for p in ((s.data.get("cover") or {}).get("images") or {}).values()]
    style_anchor = next((a for a in anchors if a.is_file()), None)
    for job in jobs:
        crefs = _refs([style_anchor] if style_anchor else None)

        def cprompt(asp, _t=job["title"], _d=job["desc"], _r=bool(crefs)):
            return cover_mod.cover_prompt(s.data, chapter_title=_t, desc=_d,
                                          style_prefix=prefix, lang=lang,
                                          ref_base=_r, aspect=asp,
                                          cast_names=cast_names, photoreal=photoreal)
        _step(f"[{job['cid']}] 章节封面 · 「{title} · {job['sub']}」 · {' + '.join(targets)}")
        wave += _bg_tasks(job["cid"], cprompt, crefs)
    bad = _run_wave(wave, "封面·系列题字/章节背景")
    if need_series and "series" not in bad:
        reg = _registry("series", scov)
        with s.commit():      # 生成期以分钟计，其间别的写者可能已改过这份文档
            s.data["cover"] = {**reg, "primary": _primary(reg),
                               "prompt": sprompt(next(iter(targets)))}
            if spent.get("series", 0) > 0:
                s.add_cost("image", spent["series"])   # 系列主视觉进系列台账
        _info(f"系列封面: ✓ {', '.join(reg['images'].values())}")
    if bad:
        _fail(bad)
    # 波次 3：章节题字（AI 题字的系列感锚：系列封面成品作参考，声明「字形沿用」）
    simgs = [wsroot / p for p in ((s.data.get("cover") or {}).get("images") or {}).values()]
    title_anchor = next((a for a in simgs if a.is_file()), None)
    wave = []
    for job in jobs:
        wave += _title_tasks(job["cid"], job["sub"], [title_anchor] if title_anchor else None)
    bad = _run_wave(wave, "封面·章节题字")
    for job in jobs:
        if job["cid"] in bad:
            continue
        reg = _registry(job["cid"], job["ccov"])
        # 生成期以分钟计，其间别的写者（tts/gen-video/Studio 派的子进程）可能已改过
        # 这份章节文档——登记走 `Project.mutate`（按磁盘最新态应用小变更）。
        # 登记先于记账：add_cost 的「先入账再抛」要连同封面登记一起落盘——封面与
        # 分镜图/简笔板同为图像生成，不入台账的话事前/事后额度闸对它双双失效
        budget_err: list = []

        def _register(p2, _reg=reg, _job=job):
            p2.data["cover"] = {**_reg, "primary": _primary(_reg), "subtitle": _job["sub"]}
            if spent.get(_job["cid"], 0) > 0:
                try:
                    p2.add_cost("image", spent[_job["cid"]])
                except KinemaError as e:
                    budget_err.append(e)
        Project.mutate(job["proj"].path, _register)
        if budget_err:
            raise budget_err[0]
        _info(f"[{job['cid']}] 封面: ✓ {', '.join(reg['images'].values())}（{job['sub']}）")
    if bad:
        _fail(bad)
    total_cost = sum(spent.values())
    if total_cost > 0:
        _info(f"本次封面生成费用 ≈ ¥{round(total_cost, 4)}")
    print("✓ 封面完成 · assets/covers/（无字背景 *_bg_<比例>.png 与成品并存，Studio 项目卡/章节列表自动展示）")


def cmd_deliver(args):
    from .deliver import build_delivery
    project = _load_video(args)
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()] \
        if args.platforms else None
    license_kind = None
    if getattr(args, "chapter", None) and "/" in args.chapter:
        pdoc = Workspace.open(args.workspace, create=False) \
            .get_project(args.chapter.split("/")[0]).data
        platforms = platforms or pdoc.get("platform")
        license_kind = pdoc.get("license")
    store = ConfigStore.load(args.config)
    r = build_delivery(project, platforms=platforms, license_kind=license_kind,
                       out_dir=Path(args.out) if args.out else None,
                       make_zip=not args.no_zip,
                       subtitle_lang=_sub_cfg(store, project).get("lang"))
    print(f"✓ 交付包已导出 · {len(r['platforms'])} 平台（{', '.join(r['platforms'])}）"
          f" · 比例 {', '.join(r['aspects'])} · {r['files']} 个文件")
    print(f"   目录: {r['dir']}")
    if r["zip"]:
        print(f"   压缩包: {r['zip']}")
    if not license_kind:
        print("   ⓘ 项目未声明版权标记（manifest 记为 unspecified）：project set <项目> --license exclusive|nonexclusive")


def cmd_export_pitch(args):
    from .export import build_pitch_page
    ws = Workspace.open(args.workspace, create=False)
    index = build_pitch_page(ws, args.project, out_dir=args.out)
    print(f"✓ 提案书已导出: {index}")
    print("   浏览器打开 →「打印 → 存为 PDF」即得提案 PDF（A4 印刷排版已内置，"
          "屏幕看是暗色影视风）")
_SETUP_KEYS = [
    ("ARK_API_KEY", "Seedream 生图 + Seedance 图生视频"),
    ("ARK_TTS_API_KEY", "配音 seed-tts-2.0（语音独立凭证）"),
    ("ELEVENLABS_API_KEY", "BGM（为空自动切本地音乐库）"),
]


def cmd_setup(args):
    """安装向导：交付/换机后从零到 mock 跑通；--check 为非交互验收自检。"""
    import contextlib
    import io
    import os
    import shutil as _sh
    from .storage import load_storage_config, get_storage

    as_json = getattr(args, "json", False)
    checks: list[tuple[str, bool, str]] = []
    # --json 时静音检查过程中的人读杂音（如 ConfigStore 的 provider 别名更名
    # 提醒会 print 到 stdout）——混进一行就撕破纯 JSON 面；stderr 原样放行。
    with (contextlib.redirect_stdout(io.StringIO()) if as_json
          else contextlib.nullcontext()):
        has_ff = bool(_sh.which("ffmpeg") and _sh.which("ffprobe"))
        checks.append(("ffmpeg / ffprobe", has_ff,
                       "已就绪" if has_ff else "缺失 → brew install ffmpeg / apt install ffmpeg"))
        # 密钥模板自动落地：secrets.yaml 是 gitignore 的，全新 clone 一定没有。
        # 让用户手抄一份是「填了却读不到」的常见来源（漏 key、格式写错、没注释），
        # 这里直接从随包模板复制全空的一份，用户只需填值。已存在则原样不动。
        from . import config_overlay as _ovl0
        _born = _ovl0.ensure_secrets_yaml(_ovl0.config_dir(args.config))
        if _born is not None:
            print(f"  ✓ 已生成密钥文件 {_born}（全空模板·已在 .gitignore·填值即用）")
        store = None
        try:
            store = ConfigStore.load(args.config)
            checks.append(("配置 models.yaml", True, str(getattr(store, "source", ""))))
        except Exception as e:  # noqa: BLE001  配置坏了要如实进清单，而不是中断自检
            checks.append(("配置 models.yaml", False, str(e)))
        scfg = load_storage_config()
        if scfg["backend"] == "mysql":
            try:
                st = get_storage(find_workspace(getattr(args, "workspace", None)))
                checks.append(("存储 MySQL", True, st.describe()))
            except Exception as e:  # noqa: BLE001
                checks.append(("存储 MySQL", False, f"{e}（可改回 local：config/storage.yaml）"))
        else:
            checks.append(("存储 local", True, "JSON 即数据库（零依赖）"))
        route = None
        if store is not None:
            try:  # 生图三级路由（显式激活 > agent 声明 > 默认）——agent 判就绪要看它
                from .models import image_route
                route = image_route(store)
            except Exception:  # noqa: BLE001 路由算不出不该挡自检
                route = None

    def key_state(k):
        """密钥来自哪一层。**必须分得出 secrets.local.json 与 secrets.yaml**——
        `ConfigStore.secrets` 是两层合并后的结果，只按它报「secrets.yaml 已设」
        会把网页填的那把说成 yaml 里的，而那个值并不在 yaml 里，按 yaml 排查无结果。"""
        from . import config_overlay as _ovl
        return {"env": "env 已设", "local": "本机密钥文件已设",
                "file": "secrets.yaml 已设", "none": "无需密钥"}.get(
                    _ovl.key_state(store, k), "未设")

    # 必需密钥进 ready：AGENTS 承诺「ready=true 直接开工」，缺 ARK/TTS 密钥的机器走到
    # 生图/配音才在 provider 调用处失败。ELEVENLABS 有本地曲库回落，不算必需
    from . import config_overlay as _ovl
    for k, zh in _SETUP_KEYS:
        if k == "ELEVENLABS_API_KEY":
            continue
        present = _ovl.key_state(store, k) not in ("unset", "none")
        checks.append((f"密钥 {k}", present,
                       zh if present else f"未设（{zh}）→ 写入 config/secrets.local.json 或环境变量"))
    bad = sum(1 for _n, ok, _h in checks if not ok)
    if as_json:
        # 机器可读面（隐含 --check）：纯 JSON 独占 stdout——「绿灯不重复引导
        # 配置」的判定以这份输出为准，agent 只解析 ready，不必抓人读清单。
        # 密钥回状态不回值（key_state 三态先例），这里同样只出状态枚举。
        from . import config_overlay as _ovl
        print(json.dumps({
            "ready": bad == 0,
            "checks": [{"name": n, "ok": ok, "detail": hint}
                       for n, ok, hint in checks],
            "keys": [{"key": k, "label": zh, "state": _ovl.key_state(store, k),
                      "state_zh": key_state(k)} for k, zh in _SETUP_KEYS],
            "image_route": route,   # source=agent 时生图不检测 ARK key（仅视频需要）
        }, ensure_ascii=False, indent=2))
        sys.exit(1 if bad else 0)
    title = "验收自检 · setup --check" if args.check else "安装向导 · setup"
    print(title)
    for name, ok, hint in checks:
        print(f"  {'✓' if ok else '✗'} {name:<18}{hint}")
    for k, zh in _SETUP_KEYS:
        print(f"  · 密钥 {k:<20}{key_state(k)}（{zh}）")
    if route:
        src = {"explicit": "models 显式激活", "agent": "agent 原生声明（KINEMA_AGENT_IMAGEGEN）",
               "default": "默认"}[route["source"]]
        print(f"  · 生图路由 {route['provider']:<18}{src}"
              + ("——工单模式，生图不检测 ARK_API_KEY（该 key 仅 gen-video 视频需要）"
                 if route["source"] == "agent" else ""))
    if args.check:
        print("  → 密钥未设也能跑 mock；端到端验证: run --chapter <项目>/<章节> --mock")
        print(("✓ 自检通过" if not bad else f"⚠ {bad} 项未就绪，见上"))
        sys.exit(1 if bad else 0)

    # 交互式：密钥 → 存储提示 → 示例工程 mock 跑通
    # 写本机密钥文件而不是 secrets.yaml：两者同时存在时**本机那份优先**，
    # 往被遮蔽的一层写会得到「明明填了却不生效」；而且这条路与网页配置中心、
    # `config secret` 是同一个出口，三处填的是同一个地方。
    from . import config_overlay as _ovl
    spath = _ovl.secrets_path()
    print(f"\n—— 密钥配置（回车跳过；写入 {spath or '（覆盖层已关闭，跳过）'}；"
          "环境变量优先级更高）——")
    for k, zh in _SETUP_KEYS:
        v = input(f"  {k}（{zh}，当前{key_state(k)}）: ").strip()
        if v and spath:
            _ovl.write_secret(k, v)
            print(f"    ✓ 已写入 {k}（本机密钥文件·不入库不提交）")
    print("\n—— 存储 ——")
    print(f"  当前后端: {scfg['backend']}（改用 MySQL：编辑 config/storage.yaml 的 backend，"
          "连接后自动建表；密码放 secrets.yaml 的 KINEMA_MYSQL_PASSWORD）")
    ans = input("\n创建示例工程并 mock 跑通全链路吗？(Y/n) ").strip().lower()
    if ans in ("", "y", "yes"):
        if not has_ff:
            print("  ✗ 无 ffmpeg，跑不了合成——装好后重新 setup 或直接 run --mock")
            return
        ws = Workspace.open(getattr(args, "workspace", None))
        pid = "setup_demo"
        if not ws.exists(pid):
            ws.create_project("安装自检", pid=pid)
        s = ws.get_project(pid)
        chs = s.list_chapters()
        if chs:
            cid = chs[0]["id"]
        else:
            cf = s.create_chapter("链路自检")
            cid = Path(cf).stem
            data = json.loads(Path(cf).read_text(encoding="utf-8"))
            data["script"] = {"hook": "安装自检", "body": "验证生图/配音/合成链路", "cta": "完成"}
            data["shots"] = [
                {"id": 1, "dur": 3, "narration": "链路自检，第一镜。",
                 "caption": "SETUP CHECK 1/2", "image_prompt": "简洁的测试画面，编号一"},
                {"id": 2, "dur": 3, "narration": "链路自检，第二镜。",
                 "caption": "SETUP CHECK 2/2", "image_prompt": "简洁的测试画面，编号二"},
            ]
            Path(cf).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        ns = build_parser().parse_args(
            ["run", "--chapter", f"{pid}/{cid}", "--mock"]
            + (["--workspace", args.workspace] if getattr(args, "workspace", None) else []))
        if ns.func(ns):
            print(f"\n✗ 示例工程未跑通：封面步骤失败，见上方提示: {pid}/{cid}")
            return 1
        print(f"\n✓ 示例工程已跑通（mock 零成本）: {pid}/{cid} → studio 大屏可查看成片")
    print("\n下一步: python3 -m kinema doctor · studio · 正式项目 project new --template <名>")


# ---------- 工作区 / 项目管理（JSON 即数据库 CRUD）----------
def cmd_project_new(args):
    ws = Workspace.open(args.workspace)
    tpl = None
    if getattr(args, "template", None):
        from . import templates as tpl_mod
        tpl = tpl_mod.get(args.template)
    s = ws.create_project(
        args.title, theme=args.theme or "", profile=args.profile,
        platform=(args.platform.split(",") if args.platform else None),
        aspect=args.aspect, pid=args.id, template=tpl,
        skill=getattr(args, "skill", None))
    if getattr(args, "subtitle_lang", None):
        s.data["subtitle_lang"] = args.subtitle_lang   # zh/en/both，章节创建时继承
    # 画风单点落位（实体在 workspace.snapshot_style_prompt，Studio 建项目同源）
    try:
        store = ConfigStore.load(getattr(args, "config", None))
    except Exception:   # noqa: BLE001 —— 无配置不阻断建项目
        store = None
    from .workspace import snapshot_style_prompt
    snapshot_style_prompt(s, store)
    s.save()
    print(f"✓ 项目已创建: {s.pid}  「{s.data['title']}」  profile={s.data['profile']}"
          + (f"  skill={s.data.get('skill')}" if s.data.get("skill") else "")
          + (f"  模板={tpl['label']}（{s.data.get('aspect')} · motion={s.data.get('motion', '-')}）"
             if tpl else ""))
    if s.data.get("style_prompt"):
        print(f"   画风快照: {s.data['style_prompt'][:36]}…（project.style_prompt，"
              f"全片生图统一取此字段；project set --style-prompt 可改）")
    print(f"   目录: {s.dir}")
    if tpl:
        print(f"   规格已绑定：spec check {s.pid} 随时核对达标情况")
    print(f"   下一步: character add {s.pid} --name 角色名 --voice-prompt \"<声线描述>\" ; "
          f"chapter new {s.pid} --title '<本集剧情短标题>'")


def cmd_project_list(args):
    ws = Workspace.open(args.workspace, create=False)
    if getattr(args, "deleted", False):   # 回收站视图：只看已逻辑删除的
        gone = [p for p in ws.list_projects(include_deleted=True)
                if int(p.get("is_deleted") or 0)]
        if not gone:
            print("回收站为空。")
            return
        print(f"回收站 ({len(gone)}):")
        for p in gone:
            print(f"  [已删除] {p['id']:<16} 「{p['title']}」 "
                  f"删除于 {p.get('deleted_at', '?')}  →  恢复: "
                  f"kinema project restore {p['id']}")
        return
    projs = ws.list_projects()
    n_gone = len(ws.list_projects(include_deleted=True)) - len(projs)
    if not projs:
        print("（空）暂无项目。用 `kinema project new --title ...` 创建。")
    else:
        print(f"项目 ({len(projs)}):")
        for p in projs:
            # 逐字段 .get：存量项目缺 title 之类字段时，单个坏条目不许打断整张列表
            print(f"  [{p.get('status', '?'):<8}] {p.get('id', '?'):<16} "
                  f"「{p.get('title') or p.get('id', '?')}」 "
                  f"profile={p.get('profile')} 角色{len(p.get('characters') or [])} "
                  f"章节{len(p.get('chapters') or [])}")
    if n_gone:
        print(f"  （回收站另有 {n_gone} 个：project list --deleted 查看）")


def cmd_project_show(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.id, include_deleted=True)   # 详情要能看回收站里的
    d = s.data
    if int(d.get("is_deleted") or 0):
        print(f"✕ 项目已逻辑删除（{d.get('deleted_at', '?')}）——"
              f"恢复: kinema project restore {d['id']}")
    print(f"项目 {d['id']}  「{d['title']}」  [{d.get('status')}]  "
          f"profile={d.get('profile')} aspect={d.get('aspect')}")
    if d.get("theme"):
        print(f"  主题: {d['theme']}")
    des = d.get("design", {})
    for k, label in [("logline", "一句话"), ("synopsis", "梗概"), ("world", "世界观"),
                     ("tone", "基调"), ("palette", "色板"), ("style_notes", "风格备注")]:
        if des.get(k):
            print(f"  {label}: {des[k]}")
    print(f"  角色 ({len(s.characters)}):")
    for c in s.characters:
        print(f"    · {c['name']}  音色={c.get('voice') or '-'}  "
              f"{c.get('role') or ''}  {(c.get('appearance') or '')[:40]}")
    print(f"  章节 ({len(s.chapters)}):")
    for ch in s.list_chapters():
        print(f"    · {ch['id']}  「{ch['title']}」  [{ch['status']}]")


def cmd_project_set(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.id)
    if args.title is not None:
        s.data["title"] = args.title
    if args.theme is not None:
        s.data["theme"] = args.theme
    if args.profile is not None:
        # 显式值先过 catalog 再落盘（与 `project new` 同一道闸、同一份真源）：
        # 写进 project.json 的 profile/skill 就是**绑定事实**，未登记的值当场看不出
        # 异常，等到 agent route/context 才炸——那时报错点已离开输入现场。
        skills.skill_for_profile(args.profile)
        s.data["profile"] = args.profile
        # 不自动级联派生字段：project.json 里没有「skill/style_prompt 是否显式绑定」
        # 的记录，按「未显式绑过才重派生」猜会改错用户在显式绑定项目上的后改
        # （文档路径恰恰教用户这么做）。只把後果点名，改不改由用户定。
        if (s.data.get("style_prompt") or "").strip():
            print("  ⚠ 项目写有 style_prompt（生图画风的单点真源，优先于 profile 前缀）"
                  "——只改 profile 画面不会换风格；要换请同步 "
                  "`project set --style-prompt …`（或置空回落新画风前缀）")
        if s.data.get("skill"):
            print(f"  ⓘ 绑定 skill 仍是 {s.data['skill']}（旁白语态等派生随它）；"
                  "要换请 `project set --skill …` 或删除绑定")
    if getattr(args, "scene", None) is not None:
        s.data["scene"] = args.scene         # 固定场景（gen-refs 会据此出场景设定图）
    if getattr(args, "skill", None):
        # 归一到 catalog 的规范 id（新章节起继承）；未登记值同 profile 那条闸
        s.data["skill"] = skills.validate_skill(args.skill, bind=True)
    if getattr(args, "skip_design", False):
        s.data["skip_design"] = True         # 跳过设定集 → 退回首镜锚定
    if getattr(args, "license", None):
        s.data["license"] = args.license     # 版权标记（进交付包 manifest）
    if getattr(args, "subtitle_lang", None):
        s.data["subtitle_lang"] = args.subtitle_lang   # 已建章节不回溯，新章节起继承
    if getattr(args, "style_prompt", None) is not None:
        s.data["style_prompt"] = args.style_prompt     # 画风单点真源（生图统一取用）
    if getattr(args, "style_prompt_en", None) is not None:
        s.data["style_prompt_en"] = args.style_prompt_en
    s.set_design(logline=args.logline, synopsis=args.synopsis, world=args.world,
                 tone=args.tone, palette=args.palette)
    print(f"✓ 已更新项目 {s.pid}")


def cmd_project_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    ws.delete_project(args.id, archive=args.archive)
    if args.archive:
        print(f"已归档项目 {args.id}（status=archived，仍在清单中）")
    else:
        print(f"✕ 已逻辑删除项目 {args.id}——数据/产物/库行完整保留，"
              f"清单与流程不再可见；恢复: kinema project restore {args.id}")


def cmd_project_restore(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.restore_project(args.id)
    print(f"✓ 已恢复项目 {args.id}「{s.data.get('title')}」——回到清单与全部流程。")


def cmd_project_moodboard(args):
    """参考库（风格垫图）管理：列出 / 登记 / 移除 / 切换默认启用。

    库项默认启用（on=True）即注入全部设定图/分镜图/封面生成——整体锁死模块风格；
    停用（--off）留库但不默认套用，逐镜可用 shots[].refs 精确覆盖、一次性用 --no-moodboard。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.id)

    def _match(raw, fn):
        return fn(str(Path(raw).expanduser().resolve())) or fn(str(raw))

    if args.add:
        p = Path(args.add).expanduser().resolve()
        if not p.is_file():
            raise KinemaError(f"找不到图片: {p}")
        s.add_moodboard(str(p))
        _info(f"已登记参考库垫图（默认启用）: {p.name}")
    elif args.rm:
        _info("已移除" if _match(args.rm, s.remove_moodboard) else f"⚠ 库中无此项: {args.rm}")
    elif args.on:
        _info("已启用（默认套用全局生成）" if _match(args.on, lambda p: s.set_moodboard_on(p, True))
              else f"⚠ 未变更（已启用或不在库）: {args.on}")
    elif args.off:
        _info("已停用（留库不默认套用）" if _match(args.off, lambda p: s.set_moodboard_on(p, False))
              else f"⚠ 未变更（已停用或不在库）: {args.off}")

    lib = s.moodboard
    if not lib:
        _info("（参考库为空——登记垫图后所有设定图/分镜图/封面默认套用该风格）"); return
    _step(f"参考库 {len(lib)} 张 · 默认生效 {len(s.moodboard_active())} 张（✓=默认套用全局生成）")
    for x in lib:
        print(f"  {'✓' if x.get('on', True) else '·'} {x['path']}")


# 角色设定图【三区两视铁律】提示词的**实现在 `kinema/sheets.py`**（与道具/场景规则
# 同处，供 project refs / refine 局部改造 / 灯箱重生三条路径共用）。这里保留同名
# 别名：既有调用点与守卫用例都从 cli 导入它，改导入路径只会平添一次无谓的漂移。
_char_sheet_prompt = sheets.char_sheet_prompt


def cmd_gen_refs(args):
    """生成项目设定集：角色设定图(三区两视 = 正面肖像特写 + 正面全身 + 背面全身·全身空手不持武器)
    + 场景设定图 + 道具/武器设定图。
    存于 project/<pid>/assets/refs/ 并回填 project.json；之后 chapter 继承、各镜强制参考 → 跨镜跨集强一致。
    """
    store = ConfigStore.load(args.config)
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.id)
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    ensure_tools()
    prof = args.profile or s.data.get("profile")
    prov, params = router.resolve("image", prof)
    # 写实档（image.identity_sheet）：角色设定图走纯文生图吃视频侧受信豁免。
    # 受信是平台按产物溯源判的，provider 必须自声明具备该通道（能力位制度，
    # 不按 name 前缀猜）——不具备时身份图拦在角色排产处（见循环内闸），
    # 不受信的产物记成受信来源会让视频侧降级路线放行一张必被拒的图。
    identity = bool(params.get("identity_sheet"))
    trusted = bool(getattr(prov, "trusted_face_source", False))
    prefix, fell_back = prompts_mod.select_style_prefix(
        params, getattr(prov, "prompt_lang", "zh"), doc=s.data)
    if fell_back:
        _info(f"⚠ provider 偏好英文提示词但 profile '{prof}' 缺 style_prefix_en，已回退中文前缀")
    seed = int(hashlib.md5(f"{s.pid}/refs".encode()).hexdigest()[:6], 16)
    rdir, props, force = s.refs_dir, s.props, getattr(args, "force", False)
    ncand = max(1, int(getattr(args, "candidates", 1) or 1))
    from .storage.media import ensure_local
    from .refine import archive_asset_sheet   # 重生前归档旧设定图 → 版本栈可回滚（直出档）
    no_mb = getattr(args, "no_moodboard", False)

    def _mb(entity_refs):   # 逐张设定图各自解析垫图（显式 refs 精确用·[]不用·否则默认生效集）
        if no_mb:
            return []
        return [p for p in (ensure_local(v) for v in s.moodboard_refs_for(entity_refs))
                if p and Path(p).is_file()]

    _warned_refkind: dict = {}
    only = (getattr(args, "only", None) or "").strip()   # 单张设定图定向重生（网页设定图垫图重生走此）
    only_kind, _, only_name = only.partition(":")
    only_name = only_name or None

    def _want(kind, name=None):   # --only 过滤：不给=全出；给 kind[:name]=只出该张
        if only_kind and only_kind != kind:
            return False
        # 无名档（全局固定场景与它的俯视图）传 name=None，同样受名字过滤约束：
        # `--only scene:书店` 点的是那个取景地，不该连带重出并归档全片视觉基线那一张。
        # 不带冒号的 `--only scene` / `--only topview` 仍全出——only_name 为空时短路在前。
        return not (only_name and name != only_name)

    # 扩展设定图（opt-in）：--expressions/--poses 显式开，或 --only 直接点名扩展类
    # 也算开（`--only expression:洛 --force` 单张重生要能独立成立）。
    want_expr = getattr(args, "expressions", False) or only_kind == "expression"
    want_pose = getattr(args, "poses", False) or only_kind == "pose"
    ext = [z for z, on in (("表情", want_expr), ("动作", want_pose)) if on]
    # 俯视布局图**与场景基准图恒配对**，没有关闭开关：只有基准图的场景，视频请求
    # 拿到的空间证据缺一半（`lineage.primary_layout_ref` 是它唯一的消费者）。
    # 缺就补、重出就一起重出，判据与其余设定图同一套。
    regen_any = False                    # 有主设定图被(重)生成 → 触发血缘传播
    _step(f"生成设定集 · 角色 {len(s.characters)} · 道具 {len(props)}"
          + (f" · 场景 {len(s.scenes)}" if s.scenes else "")
          + (" · 全局场景" if (s.data.get('scene') or '').strip() else "")
          + (f" · 扩展({'/'.join(ext)})" if ext else "")
          + (f" · 每项 {ncand} 张候选" if ncand > 1 else ""))

    # ── 并发三段式（见 kinema/parallel.py 的铁律）──
    # ① 主线程定计划：过滤/闸门/归档旧版全在这里（都有副作用，绝不进工作线程）；
    # ② 并发只产文件：每张图一件活，只碰自己的产物路径；
    # ③ 主线程按提交顺序回填：sheet 路径 / 候选表 / save 一律单线程，
    #    否则两个线程各改一半再各自整份写盘 = 丢更新，且不报错只是"生成了没登记上"。
    plan: list[dict] = []                # 计划表：每项 = 一张设定图（含候选各算一张）

    # 「角色主体」语义的参考位（subject_reference）：非角色档一张参考都附不上。
    # 提示词的样板声明与俯视图取材都以此分支——声明一张发不出去的参考，
    # 等于向模型索要不存在的东西
    restricted = getattr(prov, "ref_kind", "any") == "character"

    def _guard_refkind(kind, refs):
        """「角色主体」语义的参考位（subject_reference）：给场景/道具设定图垫风格图
        会被平台当成角色特征学走——非角色档一张参考都不附，提示词照发。"""
        if not restricted or kind == "character":
            return refs
        if refs and not _warned_refkind.get(prov.name):
            _warned_refkind[prov.name] = True
            _info(f"  ⚠ {prov.name} 只收「角色主体」参考——非角色设定图不附垫图/样板")
        return []

    def _plan(kind, name, prompt_txt, std_out, cand_stem, label, w, h, entity_refs=None):
        """把一张（或 ncand 张候选）设定图排进计划表。**此刻不发任何请求。**

        参考图顺序 = **版式样板在最前**（该类的全部样板，同版式多示例），其后才是
        项目参考库垫图（moodboard）——样板教的是分区骨架、垫图教的是画风，前者在前
        与提示词里 `sheets.template_role` 的职责声明位置一致（不声明职责，模型会连
        样板的人物与配色一起复制）。"""
        mb_refs = _mb(entity_refs)
        tpls = sheets.templates_for(kind)
        if tpls:
            mb_refs = [*map(str, tpls), *mb_refs]
        if identity and kind == "character":
            # 受信豁免绑「是不是文生图产物」：蓝图与 moodboard 挂任何一张都变回
            # 图生图、整条豁免失效——refs 必须整体为空，不是只掐 moodboard
            mb_refs = []
        mb_refs = _guard_refkind(kind, mb_refs)
        if ncand <= 1:
            plan.append({"kind": kind, "name": name, "slot": 0, "out": std_out,
                         "label": label, "prompt": prompt_txt, "refs": mb_refs,
                         "seed": seed, "w": w, "h": h})
            return
        for k in range(1, ncand + 1):
            plan.append({"kind": kind, "name": name, "slot": k,
                         "out": rdir / f"{cand_stem}_{k}.png",
                         "label": f"{label} 候选{k}/{ncand}", "prompt": prompt_txt,
                         "refs": mb_refs, "seed": candidates_mod.seed_for(seed, k),
                         "w": w, "h": h})

    def _plan_direct(bucket, kind, name, prompt_txt, std_out, label, w, h, *,
                     entity_refs=None, refs=None):
        """恒直出单张的设定图排进 `bucket`（候选宫格与 pick-ref 定稿链路只接主设定图
        三类）。`refs` 显式给定参考图清单时不再解析实体垫图——俯视布局图走这一支：
        它的参考是该场景的基准图，不是画风垫图。"""
        bucket.append({"kind": kind, "name": name, "slot": 0, "out": std_out,
                       "label": label, "prompt": prompt_txt,
                       "refs": _guard_refkind(
                           kind, list(refs) if refs is not None else _mb(entity_refs)),
                       "seed": seed, "w": w, "h": h})

    # ── ① 主线程定计划：归档已把旧图移出标准路径，版本条目必须在同一个块里落盘。
    #    留在内存等回填期写，会被那时的重读丢掉，下一次归档的 v 号随之撞上已有文件。
    with s.commit():
        props = s.props                  # 进锁后重新定位（commit 已换掉 s.data）
        for c in s.characters:               # 角色设定图（三区两视定稿：正面肖像特写｜正面全身｜背面全身）
            if not _want("character", c.get("name")):
                continue
            if not force and (has_file(c.get("sheet")) or c.get("sheet_candidates")):
                _info(f"角色 {c['name']}: 已有设定图/候选，跳过（--force 重出）"); continue
            fatigue = variation_mod.fatigue_look([c])
            if fatigue:
                # 缺省角色气色健康、神态有精神；疲态只在用户点名时写并登记进
                # visual_requirements。闸在计费之前，判据与 lint 同源
                _info(f"✗ 角色 {c['name']}: 外貌写了疲态（{'/'.join(fatigue[0][1])}）"
                      "——缺省角色气色健康、神态有精神，不出这张设定图；"
                      "不是用户要求的就改掉描述（character set --appearance …），"
                      "确是用户要求的把该特征登记进 visual_requirements"
                      "（character set --add-visual-requirement <词>）即放行")
                continue
            if identity and not trusted:
                # 身份图的全部价值在视频侧受信豁免，闸在归档与计费之前：
                # 不受信 provider 直出的图既过不了人脸审核，又会以 t2i 名义
                # 给降级路线背书
                raise ProjectError(
                    f"写实档（identity_sheet）的角色设定图必须由受信文生图 provider 直出，"
                    f"{prov.name} 未声明 trusted_face_source——产物不落在视频侧受信豁免内，"
                    "发进视频请求必被人脸审核拒绝。\n"
                    "  把 image provider 路由到 seedream（config/models.yaml），"
                    "或改用非写实 profile 后重跑")
            if force and ncand <= 1 and has_file(c.get("sheet")):   # 直出重生前归档旧版
                archive_asset_sheet(s, "character", c["name"], reason="重新生成设定集")
            w, h = store.canvas(sheets.aspect_for("character"))   # 三区横版信息密度最优
            safe = _safe_name(c["name"])
            if identity:      # 纯文生图：声明张数同步归零（0 = 未附，见 char_sheet_prompt）
                prompt_txt = _char_sheet_prompt(c, prefix, n_templates=0)
            else:
                prompt_txt = _char_sheet_prompt(
                    c, prefix, n_templates=len(sheets.templates_for("character")))
            _plan("character", c["name"], prompt_txt,
                  rdir / f"char_{safe}.png", f"cand_char_{safe}",
                  f"CHAR {c['name']}", w, h, c.get("refs"))

        scene = (s.data.get("scene") or "").strip()   # 场景设定图
        if scene and _want("scene") and (force or not (has_file(s.data.get("scene_ref"))
                                    or s.data.get("scene_ref_candidates"))):
            if force and ncand <= 1 and has_file(s.data.get("scene_ref")):   # 直出重生前归档旧版
                archive_asset_sheet(s, "scene", reason="重新生成设定集")
            w, h = store.canvas(sheets.aspect_for("scene", s))
            _plan("scene", None,
                  sheets.scene_sheet_prompt(scene, prefix),
                  rdir / "scene.png", "cand_scene", "SCENE", w, h, s.data.get("scene_refs"))

        for sc in s.scenes:                  # 具名场景设定图（环境 key art 版式，非道具三视版式）
            if not _want("scene", sc.get("name")):
                continue
            if not force and (has_file(sc.get("sheet")) or sc.get("sheet_candidates")):
                continue
            if force and ncand <= 1 and has_file(sc.get("sheet")):   # 直出重生前归档旧版
                archive_asset_sheet(s, "scene", sc["name"], reason="重新生成设定集")
            w, h = store.canvas(sheets.aspect_for("scene", s))
            safe = _safe_name(sc["name"])
            _plan("scene", sc["name"],
                  sheets.scene_sheet_prompt(sc.get("desc") or sc["name"], prefix),
                  rdir / f"scene_{safe}.png", f"cand_scene_{safe}",
                  f"SCENE {sc['name']}", w, h, sc.get("refs"))

        for p in props:                      # 道具/武器设定图
            if not _want("prop", p.get("name")):
                continue
            if not force and (has_file(p.get("sheet")) or p.get("sheet_candidates")):
                continue
            if force and ncand <= 1 and has_file(p.get("sheet")):   # 直出重生前归档旧版
                archive_asset_sheet(s, "prop", p["name"], reason="重新生成设定集")
            w, h = store.canvas(sheets.aspect_for("prop"))
            safe = _safe_name(p["name"])
            _plan("prop", p["name"],
                sheets.prop_sheet_prompt(p, prefix,
                                         # 受限参考位下样板附不上，声明张数同步归零
                                         n_templates=0 if restricted
                                         else len(sheets.templates_for("prop"))),
                rdir / f"prop_{safe}.png", f"cand_prop_{safe}", f"PROP {p['name']}", w, h,
                p.get("refs"))

        # ── 扩展设定图（表情/动作，规格真源 sheets.py 第四节）──
        # 恒直出单张（候选宫格与 pick-ref 定稿链路只接主设定图三类）；
        # **不进每镜自动挂载**（design_refs 有 8 张硬上限，挤掉主设定图得不偿失）——
        # 关键镜要用时显式写 shots[].refs，或供审片/表演对照。
        if want_expr:                        # 表情设定图（4×3 十二格·required_emotions 优先入格）
            for c in s.characters:
                if not _want("expression", c.get("name")):
                    continue
                if not force and has_file(c.get("expression_sheet")):
                    _info(f"角色 {c['name']}: 已有表情设定图，跳过（--force 重出）"); continue
                if force and has_file(c.get("expression_sheet")):
                    archive_asset_sheet(s, "expression", c["name"], reason="重新生成表情设定图")
                w, h = store.canvas(sheets.aspect_for("expression"))
                _plan_direct(plan, "expression", c["name"],
                             sheets.expression_sheet_prompt(c, prefix),
                             rdir / f"char_expr_{_safe_name(c['name'])}.png",
                             f"EXPR {c['name']}", w, h, entity_refs=c.get("refs"))
        if want_pose:                        # 动作设定图（5×3 十五格·required_actions 优先入格）
            for c in s.characters:
                if not _want("pose", c.get("name")):
                    continue
                if not force and has_file(c.get("pose_sheet")):
                    _info(f"角色 {c['name']}: 已有动作设定图，跳过（--force 重出）"); continue
                if force and has_file(c.get("pose_sheet")):
                    archive_asset_sheet(s, "pose", c["name"], reason="重新生成动作设定图")
                w, h = store.canvas(sheets.aspect_for("pose"))
                _plan_direct(plan, "pose", c["name"], sheets.pose_sheet_prompt(c, prefix),
                             rdir / f"char_pose_{_safe_name(c['name'])}.png",
                             f"POSE {c['name']}", w, h, entity_refs=c.get("refs"))

    # ── ② 并发只产文件 ──────────────────────────────────────────────────
    workers = parallel.resolve_workers(getattr(args, "concurrency", None))

    def _task(item):
        def _run():
            return prov.generate(item["prompt"], str(item["out"]),
                                 ref_images=item["refs"], seed=item["seed"],
                                 width=item["w"], height=item["h"], label=item["label"])
        key = f"{item['kind']}:{item['name'] or ''}#{item['slot']}"
        return parallel.Task(key=key, run=_run, label=item["label"], out=item["out"],
                             meta=item)

    # ── ③ 主线程按提交顺序回填（唯一改文档的地方）──────────────────────
    picked_of: dict[tuple, str] = {}          # (kind,name) → 定稿路径（直出档）
    cands_of: dict[tuple, list] = {}          # (kind,name) → 候选路径表（宫格档）
    failed: list = []
    spent = 0.0            # 设定图是系列级资产，费用进系列台账（Series.add_cost）
    ZH_KIND = {"character": "角色设定图", "prop": "道具设定图", "scene": "场景设定图",
               "expression": "表情设定图", "pose": "动作设定图",
               "topview": "场景俯视图"}

    def _apply(d: parallel.Done):
        nonlocal regen_any, spent
        item = d.meta
        ident = (item["kind"], item["name"])
        if not d.ok:
            failed.append(d)
            _info(f"✗ {item['label']}: {d.message}")
            return
        # 幂等护栏救回来的那张没有 res 对象，产物已在盘上，路径就是我们给的那个
        path = getattr(d.value, "path", None) or str(item["out"])
        spent += float(getattr(d.value, "cost", 0.0) or 0.0)
        if item["slot"] == 0:
            picked_of[ident] = path
        else:
            cands_of.setdefault(ident, []).append(path)
        if item["kind"] in ("character", "prop", "scene"):
            regen_any = True   # 扩展设定图不进每镜挂载——重生它不许把下游分镜置 retake
        # **逐项即时反馈**：打印放在这里（按提交顺序、随完成流出），不要攒到回填循环——
        # 攒着的话十几张图跑几分钟只有心跳输出，无法判断哪几张已完成
        zh = ZH_KIND[item["kind"]]
        who = f" {item['name']}" if item["name"] else ""
        if item["slot"] == 0:
            _info(f"{zh}{who}: ✓")
        elif len(cands_of[ident]) == ncand:
            _info(f"{zh}{who}: {ncand} 张候选 → 宫格待选")

    def _run_wave(items, note=""):
        """跑一批计划：并发只产文件，`_apply` 在主线程按提交顺序回填两张表。"""
        if not items:
            return []
        _step(f"生图 {len(items)} 张{note}"
              + (f" · 并发 {workers}" if workers > 1 else " · 串行")
              + (f"（每项 {ncand} 张候选）" if ncand > 1 else ""))
        return parallel.run([_task(x) for x in items], workers=workers,
                            on_done=_apply,
                            on_progress=parallel.progress_printer("设定图"))

    results = _run_wave(plan)

    # ── ③ 主线程回填：生成期以分钟计，其间别的写者（含 Studio 派出的子进程）
    #    可能已经写过这份文档。按 (kind,name) 身份合并，不按完成顺序对应。
    with s.commit():
        props = s.props                       # 进锁后重新定位（必须）
        # sheet_origin 是生成方式的事实记录（t2i 才落在视频侧受信豁免内），写 sheet
        # 的每一处同批写；候选批整批同一来源，定稿时由 pick_asset_candidate 转正
        origin = "t2i" if identity else "i2i"
        # 直出定稿同时清掉候选三件（candidates/candidates_origin/picked）：
        # 残留的候选表会让跳过判据恒短路，且此后 pick 会用上一批的来源记录
        # 覆盖 sheet_origin——受信记录被旧值污染
        for c in s.characters:
            ident = ("character", c["name"])
            if ident in picked_of:
                c["sheet"] = picked_of[ident]
                c["sheet_origin"] = origin
                c.pop("sheet_candidates", None)
                c.pop("sheet_candidates_origin", None)
                c.pop("sheet_picked", None)
            elif ident in cands_of:
                c["sheet_candidates"] = cands_of[ident]; c.pop("sheet_picked", None)
                c["sheet_candidates_origin"] = origin
        if ("scene", None) in picked_of:
            s.data["scene_ref"] = picked_of[("scene", None)]
            s.data.pop("scene_ref_candidates", None)
            s.data.pop("scene_ref_picked", None)
        elif ("scene", None) in cands_of:
            s.data["scene_ref_candidates"] = cands_of[("scene", None)]
            s.data.pop("scene_ref_picked", None)
        for sc in s.scenes:
            ident = ("scene", sc["name"])
            if ident in picked_of:
                sc["sheet"] = picked_of[ident]
                sc.pop("sheet_candidates", None)
                sc.pop("sheet_picked", None)
            elif ident in cands_of:
                sc["sheet_candidates"] = cands_of[ident]; sc.pop("sheet_picked", None)
        for p in props:
            ident = ("prop", p["name"])
            if ident in picked_of:
                p["sheet"] = picked_of[ident]
                p.pop("sheet_candidates", None)
                p.pop("sheet_picked", None)
            elif ident in cands_of:
                p["sheet_candidates"] = cands_of[ident]; p.pop("sheet_picked", None)
        for c in s.characters:                    # 扩展设定图回填（恒直出，无候选分支）
            for kd, field in (("expression", "expression_sheet"), ("pose", "pose_sheet")):
                if (kd, c["name"]) in picked_of:
                    c[field] = picked_of[(kd, c["name"])]

    # ── 第二波：场景俯视布局图 ────────────────────────────────────────────
    # **必须排在场景基准图落盘之后**：俯视图以基准图为空间取材（同一个地方的两张
    # 图，一张画面、一张图纸），基准图这一轮才刚生成，第一波定计划时它还不在盘上。
    # 排进同一批并发只会让新建项目的俯视图统统拿不到参考、各画各的空间。
    top_plan: list[dict] = []
    deferred: list[tuple] = []        # 基准图不在盘、本轮不画的 (场景名, 图纸是否已在盘)
    # 俯视图的唯一参考是场景基准图；受限参考位下附不上，画出的图纸与基准图
    # 交代的不是同一个空间，还会占住 topview_sheet 让跳过判据永远短路
    if restricted and ((s.data.get("scene") or "").strip() or s.scenes):
        _info(f"  ⓘ 俯视布局图本轮不画——{prov.name} 只收「角色主体」参考，"
              "场景基准图无法随请求附上")
    # 本轮以候选形式重出基准图的场景。候选不落 `sheet`（定稿在 pick-ref），第二波
    # 看到的还是上一版那张，照它画出来的图纸与即将定稿的基准图交代的不是同一个空间。
    pending = {x["name"] for x in plan if x["kind"] == "scene"} if ncand > 1 else set()
    with s.commit():
        scene = (s.data.get("scene") or "").strip()   # 进锁后重新定位
        tw, th = store.canvas(sheets.aspect_for("topview"))

        def _topview_src(name, sheet):
            """该场景基准图的本地路径；不在盘、或本轮正被候选重出，返回 None。"""
            if name in pending:
                return None
            src = ensure_local(sheet) if sheet else None
            return src if (src and Path(src).is_file()) else None

        def _plan_topview(name, desc, src, std_out, label):
            """一张俯视图排进第二波。`src` = 该场景基准图的本地路径，**调用方已确认
            在盘**：图纸的价值全在与基准图交代同一个空间，没有它画出来的那张会占住
            `topview_sheet`，而此后的跳过判据只看文件在不在，这一对图再没有机会对齐。"""
            _plan_direct(top_plan, "topview", name,
                         sheets.scene_topview_prompt(desc, prefix),
                         std_out, label, tw, th, refs=[src])

        # `--only scene[:名]` 连带出它的俯视图：两张图是一对，重出基准图却留着
        # 一张按旧空间画的平面图，等于让视频请求同时挂两份互相矛盾的空间证据。
        def _want_top(name=None):
            if restricted:
                return False
            return (_want("topview", name) if only_kind == "topview"
                    else _want("scene", name))

        # 就绪判定必须排在 `archive_asset_sheet` 之前：归档是移动文件且标准字段路径
        # 不变，先归档再判定不画，等于把在盘的旧图纸移进版本栈、字段指向空路径。
        if scene and _want_top() and (force or not has_file(s.data.get("scene_topview_ref"))):
            src = _topview_src(None, s.data.get("scene_ref"))
            if not src:
                deferred.append(("全局固定场景",
                                 has_file(s.data.get("scene_topview_ref"))))
            else:
                if has_file(s.data.get("scene_topview_ref")):
                    archive_asset_sheet(s, "topview", reason="重新生成场景俯视图")
                _plan_topview(None, scene, src,
                              rdir / "scene_top.png", "TOPVIEW")
        for sc in s.scenes:
            if not _want_top(sc.get("name")):
                continue
            if not force and has_file(sc.get("topview_sheet")):
                continue
            src = _topview_src(sc.get("name"), sc.get("sheet"))
            if not src:
                deferred.append((sc["name"], has_file(sc.get("topview_sheet"))))
                continue
            if has_file(sc.get("topview_sheet")):
                archive_asset_sheet(s, "topview", sc["name"],
                                    reason="重新生成场景俯视图")
            _plan_topview(sc["name"], sc.get("desc") or sc["name"], src,
                          rdir / f"scene_top_{_safe_name(sc['name'])}.png",
                          f"TOPVIEW {sc['name']}")
    # 补救口径按「图纸在不在盘」分开说：还没有图纸的场景，裸重跑会自然补出；
    # 已有图纸的场景，裸重跑撞上「图纸在盘就跳过」那道闸，必须点名 --force 才重画。
    fresh = [n for n, on_disk in deferred if not on_disk]
    stale = [n for n, on_disk in deferred if on_disk]
    if fresh:
        _info(f"  ⓘ {len(fresh)} 张俯视图本轮不画（{'、'.join(fresh)}）——场景基准图"
              "不在盘（候选待定稿，或本轮没生成成功）。图纸的价值全在与基准图交代"
              f"同一个空间，定稿后重跑 `project refs {s.pid}` 会以它补出")
    if stale:
        _info(f"  ⚠ {len(stale)} 张俯视图保持原样（{'、'.join(stale)}）——场景基准图"
              "不在盘，此刻重画只会画出另一个空间。裸重跑不会再动它："
              f"基准图落盘后跑 `project refs {s.pid} --only topview[:名] --force`")
    results += _run_wave(top_plan, note="（场景俯视 · 以基准图为空间取材）")
    with s.commit():
        if ("topview", None) in picked_of:
            s.data["scene_topview_ref"] = picked_of[("topview", None)]
        for sc in s.scenes:
            if ("topview", sc["name"]) in picked_of:
                sc["topview_sheet"] = picked_of[("topview", sc["name"])]
        if spent > 0:
            s.add_cost("image", spent)       # 设定图是系列级资产，费用进系列台账

    rep = parallel.summarize(results)
    if rep["retried"]:
        _info(f"重试后成功 {len(rep['retried'])} 张："
              + "、".join(d.label for d in rep["retried"]))
    if rep["salvaged"]:
        _info(f"产物已在盘、免于重复付费 {len(rep['salvaged'])} 张（幂等护栏）")
    if spent > 0:
        _info(f"本次设定图费用 ≈ ¥{round(spent, 4)}（系列台账）")
    from .providers.image.agent import ORDER_BASENAME as _order_name, \
        PENDING_MARK as _pending_mark
    if failed and all(_pending_mark in d.message for d in failed):
        raise KinemaError(
            f"{len(failed)} 张待 agent 产图——工单已开: {rdir / _order_name}\n"
            "  用你的原生生图能力按工单逐条产图（prompt → path，尺寸 width×height，"
            "ref_images 供垫图），完成后重跑同一条 project refs 即自动验收登记。")
    if failed:
        # 成功的已在 commit 内登记；未成的逐张点名并以非零退出，脚本与 Agent 据此停在设定阶段
        raise KinemaError(
            f"{len(failed)} 张设定图未生成：" + "、".join(f"{d.label}（{d.message}）" for d in failed)
            + f"\n   重跑（已成功的会自动跳过）：`kinema project refs {s.pid}`"
            "；单张定向：`--only character:名`")

    if ncand > 1:
        print("\n候选已出。定稿：Studio 项目页宫格点选，或 "
              f"`project pick-ref {s.pid} --asset character:名|scene|prop:名 --use 编号`")

    nc = sum(1 for c in s.characters if c.get("sheet"))
    npf = sum(1 for p in props if p.get("sheet"))
    # 场景一栏按「基准图 / 俯视图」成对报数：两张缺一张时视频请求只拿到一半空间证据，
    # 而合成一个「场景 ✓」会把这个缺口盖掉
    n_scene = sum(1 for x in s.scenes if x.get("sheet")) \
        + (1 if s.data.get("scene_ref") else 0)
    n_top = sum(1 for x in s.scenes if x.get("topview_sheet")) \
        + (1 if s.data.get("scene_topview_ref") else 0)
    n_place = len(s.scenes) + (1 if (s.data.get("scene") or "").strip() else 0)
    print(f"\n✓ 设定集完成 → {rdir}")
    print(f"   角色 {nc}/{len(s.characters)} · 道具 {npf}/{len(props)} · "
          f"场景基准图 {n_scene}/{n_place} · 场景俯视图 {n_top}/{n_place}")
    print("   后续 chapter 会继承，各镜自动强制参考这些设定图（除非 skip_design）。")
    print("   场景俯视图随视频请求发出，每镜附该镜主场景的那一张（画面基准 + 空间图纸），"
          "分镜图侧不挂——图侧 8 张参考位留给身份与外观。")
    if want_expr or want_pose:
        ne = sum(1 for c in s.characters if c.get("expression_sheet"))
        na = sum(1 for c in s.characters if c.get("pose_sheet"))
        print(f"   扩展设定图：表情表 {ne} · 动作表 {na}"
              "——不进每镜自动挂载，关键镜用 shots[].refs 显式挂或供审片对照")

    if s.chapters:   # 设定集同步：先建章节后补设定也不脱钩
        st = s.sync_design_to_chapters()
        if st["chapters"]:
            print(f"   已同步进 {st['chapters']} 个已建章节"
                  f"（补入 {st['added']} 项 · 更新 {st['updated']} 项）"
                  "——各镜设定图参考与就绪度节点即刻生效")

    if regen_any and s.chapters:   # 血缘传播：设定图更新 → 下游分镜自动标过期
        tr = tf = 0
        for ch in s.chapters:
            try:
                chp = Project.load(s.get_chapter_path(ch["id"]))
            except Exception:  # noqa: BLE001  章节文档缺失/损坏不阻断设定集流程
                continue
            r, f = lineage.mark_stale(chp)
            if r or f:
                chp.save()
                tr += r; tf += f
        if tr or tf:
            print(f"   血缘传播: {tr} 镜已置 retake（按新设定图重生）"
                  + (f" · {tf} 镜已锁定仅挂过期标记" if tf else "")
                  + " —— lineage status 可查明细")


def cmd_scene_add(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    kw = getattr(args, "keyword", None) or []
    s.add_scene(args.name, desc=args.desc or "", keywords=kw)
    print(f"✓ 场景已加入 {s.pid}: {args.name}"
          + (f" · 关键词 {'/'.join(kw)}" if kw else ""))


def cmd_scene_list(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    print(f"{s.pid} 具名场景 ({len(s.scenes)}):")
    for x in s.scenes:
        print(f"  · {x['name']}  设定图={'✓' if x.get('sheet') else '—'}  "
              f"{(x.get('desc') or '')[:40]}")
    if (s.data.get("scene") or "").strip():
        print(f"  （另有全局固定场景：{s.data['scene'][:40]}…"
              f" 设定图={'✓' if s.data.get('scene_ref') else '—'}）")


def cmd_scene_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    ws.get_project(args.project).remove_scene(args.name)
    print(f"已移除场景 {args.name}")


def cmd_prop_add(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    kw = getattr(args, "keyword", None) or []
    s.add_prop(args.name, desc=args.desc or "", kind=args.kind, keywords=kw)
    print(f"✓ 设定已加入 {s.pid}: {args.name}（{args.kind}）"
          + (f" · 关键词 {'/'.join(kw)}" if kw else ""))


def cmd_prop_list(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    print(f"{s.pid} 道具/武器设定 ({len(s.props)}):")
    for p in s.props:
        print(f"  · {p['name']}  [{p.get('kind','prop')}]  设定图={'✓' if p.get('sheet') else '—'}  "
              f"{(p.get('desc') or '')[:40]}")


def cmd_prop_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    ws.get_project(args.project).remove_prop(args.name)
    print(f"已移除设定 {args.name}")


def _norm_gender(v):
    """性别值归一（试音过滤 character_gender 判定链第一环的唯一写入口径）：
    存 male/female，认 男/女/男性/女性/m/f，其他值明确拒绝（写错=闸静默失效）。"""
    g = {"male": "male", "m": "male", "男": "male", "男性": "male",
         "female": "female", "f": "female", "女": "female", "女性": "female"} \
        .get(str(v).strip().lower())
    if not g:
        raise ProjectError(f"gender 只认 male/female/男/女（收到 {v!r}）")
    return g


def _norm_subject_kind(v):
    """主体类型归一：只接受显式分类，不从 appearance 文本猜测。"""
    kind = {
        "human": "human", "人": "human", "人类": "human",
        "animal": "animal", "动物": "animal", "宠物": "animal",
        "creature": "creature", "生物": "creature", "异兽": "creature",
        "robot": "robot", "机器人": "robot", "机械体": "robot",
        "spirit": "spirit", "灵体": "spirit",
        "other": "other", "其他": "other",
    }.get(str(v or "").strip().lower())
    if not kind:
        raise ProjectError("subject-kind 只认 human/animal/creature/robot/spirit/other")
    return kind


def cmd_character_add(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    s.add_character(args.name, appearance=args.appearance or "",
                    role=args.role or "", ref_image=args.ref,
                    outfit=getattr(args, "outfit", "") or "", hair=getattr(args, "hair", "") or "",
                    weapon=getattr(args, "weapon", "") or "",
                    keywords=getattr(args, "keyword", None) or [],
                    gender=_norm_gender(args.gender) if getattr(args, "gender", None) else None,
                    subject_kind=(_norm_subject_kind(args.subject_kind)
                                  if getattr(args, "subject_kind", None) else None),
                    visual_requirements=(getattr(args, "visual_requirement", None) or []))
    extras = {k: getattr(args, k, None) for k in
              ("speech_style", "personality", "arc", "silhouette_notes")}
    extras["constraints"] = getattr(args, "constraint", None)
    extras["taboo_lines"] = getattr(args, "taboo", None)
    extras = {k: v for k, v in extras.items() if v}
    if extras:
        s.set_character(args.name, **extras)
    voice = _voice_choice(s, args, args.name)
    print(f"✓ 角色已加入 {s.pid}: {args.name}  音色={voice}"
          f"（下一步 project refs {s.pid} 生成角色设定图）")
    _warn_fatigue(s, args.name)


def _warn_fatigue(s, name: str) -> None:
    """外貌字段写了疲态而未登记 visual_requirements：建档时即提醒，不等 `project refs`
    的出图闸。判据与 lint、refs 闸同源（`variation.fatigue_look`）。"""
    c = next((x for x in s.characters if x.get("name") == name), None)
    rows = variation_mod.fatigue_look([c] if c else [])
    if rows:
        print(f"   ⚠ 外貌写了疲态（{'/'.join(rows[0][1])}）——缺省角色气色健康、神态有精神；"
              "不是用户要求的就改掉描述，确是用户要求的用 --add-visual-requirement 登记该特征，"
              "否则 project refs 不出这张设定图")


def _voice_choice(series, args, owner: str) -> str:
    """`--voice-prompt`（缺省路径，定制并立档）与 `--voice`（模版别名，显式例外）
    二选一；都没给返回「未选角」，真发前的选角闸会点名。"""
    prompt = getattr(args, "voice_prompt", None)
    alias = getattr(args, "voice", None)
    if prompt and alias:
        raise KinemaError("--voice-prompt 与 --voice 只能给一个：定制按描述造声，别名是模版音色")
    if prompt:
        store = ConfigStore.load(getattr(args, "config", None))
        router = ModelRouter(store, force_mock=getattr(args, "mock", False))
        _print_voice_use(voicebank.cast_custom(series, store, router, owner, prompt))
        return "定制"
    if alias:
        _assign_voice(series, args, owner, alias)
        return f"{alias}（模版）"
    return "未选角"


def _assign_voice(series, args, owner: str, ref: str) -> None:
    """`--voice` 的落点，统一走 `voicebank.assign_voice`。

    只写 `characters[].voice` 不足以完成指派：已建章节持有的是建章时的拷贝，
    可试听的锚定音也需现合成。预热失败不影响指派本身（真发时会再试一次）。"""
    store = ConfigStore.load(getattr(args, "config", None))
    router = ModelRouter(store, force_mock=getattr(args, "mock", False))
    r = voicebank.assign_voice(series, store, owner, ref, router=router)
    if not r.get("anchor"):
        _info(f"  ⓘ 「{owner}」的音色样本未能预热（缺 TTS 凭证或合成失败）——"
              "指派已生效，生视频真发时会再合成一次")


def _list_arg(explicit, add, existing):
    """列表字段的统一口径：`--x` 整体替换（给几个就是几个）· `--add-x` 并集追加。
    两者都没给 → 返回 None（= 本次不动这个字段）。"""
    if explicit:
        return list(dict.fromkeys(explicit))
    if add:
        merged = list(existing or [])
        for v in add:
            if v not in merged:
                merged.append(v)
        return merged
    return None


def _after_set(s, label, name, changed, args):
    print(f"✓ {label}设定已更新 {s.pid}: {name} · " + "、".join(changed))
    if getattr(args, "sync", False):
        st = s.sync_design_to_chapters()
        print(f"   ↳ 已同步 {st['chapters']} 个章节（补入 {st['added']} · "
              f"更新 {st['updated']}）")
    elif s.chapters:
        # 章节继承是**创建时拷贝**：不推送的话「系列填了、章节看不见」（AGENTS.md §3 不变量）
        print(f"   ⚠ 已建 {len(s.chapters)} 个章节持有的是创建时的拷贝——"
              f"加 --sync 推送到存量章节（或下次 project refs 收尾自动同步）")


def cmd_character_set(args):
    """更新既有角色的**文字**设定（外貌四件套 + 文字人设四件 + M8 五字段）。

    为什么必须有这条命令：设定是边写边长的（弧光推进/换装/断剑/新绰号），
    在此之前只能手改 project.json——而那条路会被引擎长任务的旧内存副本整份
    覆写、mysql 模式下还会被较新的库行在 load 之前盖掉（同 decisions 教训）。
    """
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    cur = next((c for c in s.characters if c.get("name") == args.name), None)
    fields = {k: getattr(args, k, None) for k in
              ("voice", "appearance", "role", "outfit", "hair", "weapon",
               "speech_style", "personality", "arc", "silhouette_notes", "status")}
    if getattr(args, "gender", None):
        fields["gender"] = _norm_gender(args.gender)
    if getattr(args, "subject_kind", None):
        fields["subject_kind"] = _norm_subject_kind(args.subject_kind)
    fields["constraints"] = _list_arg(args.constraint, args.add_constraint,
                                      (cur or {}).get("constraints"))
    fields["taboo_lines"] = _list_arg(args.taboo, args.add_taboo,
                                      (cur or {}).get("taboo_lines"))
    fields["keywords"] = _list_arg(args.keyword, args.add_keyword,
                                   (cur or {}).get("keywords"))
    fields["visual_requirements"] = _list_arg(
        args.visual_requirement, args.add_visual_requirement,
        (cur or {}).get("visual_requirements"))
    for cli_k, k in (("emotion", "required_emotions"), ("action", "required_actions"),
                     ("view", "required_views")):
        fields[k] = _list_arg(getattr(args, cli_k), None, None)
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields and not getattr(args, "voice_prompt", None):
        raise ProjectError("没给任何要改的字段（character set --help 看可设项）")
    fields.pop("voice", None)
    if fields:
        s.set_character(args.name, **fields)
    if getattr(args, "voice_prompt", None) or getattr(args, "voice", None):
        _voice_choice(s, args, args.name)
        fields["voice"] = True
    _after_set(s, "角色", args.name, sorted(fields), args)
    _warn_fatigue(s, args.name)


def cmd_character_show(args):
    """打印角色的**文字设定卡**（写正文/写台词该读的那几样）——比 Read 整份
    project.json 省得多，长篇项目里 novel 登记块本身就有几万字。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    names = [args.name] if args.name else [c.get("name") for c in s.characters]
    if args.name and not any(c.get("name") == args.name for c in s.characters):
        have = "、".join(c.get("name") or "?" for c in s.characters) or "无"
        raise ProjectError(f"没有角色「{args.name}」（现有: {have}）")
    ZH = {"role": "定位", "appearance": "外貌", "outfit": "服装", "hair": "发型",
          "weapon": "武器", "keywords": "别名", "speech_style": "台词口吻",
          "personality": "性格内核", "arc": "人物弧光", "taboo_lines": "行为禁区",
          "required_emotions": "必演情绪", "required_actions": "必演动作",
          "required_views": "必要视角", "silhouette_notes": "剪影特征",
          "constraints": "画面硬约束", "subject_kind": "主体类型",
          "visual_requirements": "正向视觉特征"}
    for name in names:
        c = next(x for x in s.characters if x.get("name") == name)
        card = N.persona_card(c)
        print(f"■ {name}" + (f"  音色={c['voice']}" if c.get("voice") else "")
              + ("  设定图✓" if c.get("sheet") else "  设定图—"))
        for k in ("role", "subject_kind", "appearance", "outfit", "hair", "weapon", "keywords",
                  "speech_style", "personality", "arc", "taboo_lines",
                  "required_emotions", "required_actions", "required_views",
                  "silhouette_notes", "visual_requirements", "constraints"):
            v = card.get(k) if k in card else c.get(k)
            if not v:
                continue
            print(f"    {ZH[k]}: " + ("、".join(map(str, v)) if isinstance(v, list) else str(v)))
        miss = [ZH[k] for k in ("speech_style", "personality", "arc", "taboo_lines")
                if not c.get(k)]
        if miss:
            print(f"    ⚠ 缺文字人设: {'、'.join(miss)}"
                  "（人设门的判据，缺了这门只能凭印象判）")


def cmd_prop_set(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    cur = next((p for p in s.props if p.get("name") == args.name), None)
    fields = {k: v for k, v in (("desc", args.desc), ("kind", args.kind)) if v}
    kw = _list_arg(args.keyword, args.add_keyword, (cur or {}).get("keywords"))
    if kw is not None:
        fields["keywords"] = kw
    if not fields:
        raise ProjectError("没给任何要改的字段（prop set --help 看可设项）")
    s.set_prop(args.name, **fields)
    _after_set(s, "道具", args.name, sorted(fields), args)


def cmd_scene_set(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    cur = next((x for x in s.scenes if x.get("name") == args.name), None)
    fields = {"desc": args.desc} if args.desc else {}
    kw = _list_arg(args.keyword, args.add_keyword, (cur or {}).get("keywords"))
    if kw is not None:
        fields["keywords"] = kw
    if not fields:
        raise ProjectError("没给任何要改的字段（scene set --help 看可设项）")
    s.set_named_scene(args.name, **fields)
    _after_set(s, "场景", args.name, sorted(fields), args)


def cmd_character_list(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    print(f"{s.pid} 角色 ({len(s.characters)}):")
    for c in s.characters:
        print(f"  · {c['name']}  音色={c.get('voice') or '-'}  {c.get('role') or ''}  "
              f"{(c.get('appearance') or '')[:50]}")


def cmd_character_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    ws.get_project(args.project).remove_character(args.name)
    print(f"已移除角色 {args.name}")


def cmd_chapter_new(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    cf = s.create_chapter(args.title, cid=args.id, theme=args.theme or "")
    cid = Path(cf).stem
    print(f"✓ 章节已创建: {s.pid}/{cid}  「{args.title}」（已继承 profile/角色音色/设定）")
    num = chapter_title_number(args.title)
    if num:
        print(f"   ⚠ 标题含序号「{num}」：序号由章节 id/order 与封面排版管理，"
              "标题应是本集剧情的裸短标题（lint chapter_title_numbered 会持续点名）")
    print(f"   文件: {cf}（待 Skill 填 script/shots）")
    print(f"   渲染: kinema run --chapter {s.pid}/{cid} [--dubbed | --native]")


def cmd_chapter_list(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    print(f"{s.pid} 章节 ({len(s.chapters)}):")
    for ch in s.list_chapters():
        print(f"  · {ch['id']}  「{ch['title']}」  [{ch['status']}]")


def cmd_chapter_show(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    cf = s.get_chapter_path(args.chapter_id)
    v = json.loads(cf.read_text(encoding="utf-8"))
    print(f"章节 {s.pid}/{args.chapter_id}  「{v.get('chapter', {}).get('title')}」  "
          f"[{s.chapter_status(args.chapter_id)}]")
    print(f"  profile={v.get('profile')} aspect={v.get('aspect')} "
          f"分镜={len(v.get('shots', []))} 角色音色={len(v.get('voices', {}))}")
    if v.get("script", {}).get("hook"):
        print(f"  钩子: {v['script']['hook']}")
    print(f"  文件: {cf}")


def cmd_chapter_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    ws.get_project(args.project).delete_chapter(args.chapter_id)
    print(f"已删除章节 {args.chapter_id}")


def cmd_chapter_set(args):
    """章节元数据维护：标题，以及建章时拷贝的 `skill`/`profile` 绑定与视频路由。

    只开标题、绑定与路由三类键：章节其余作者字段的唯一入口是 ChapterPlan
    （`agent context` → `plan validate` → `plan apply`），在这里复刻第二份可写面
    就是两条写路径各改一半。绑定两键**不在** plan 白名单里（画风是项目级单点
    真源，章节副本只为可复现），Skill/画风退役后又必须能改；`video_provider`
    是路由点名（大模型只有显式点名才上），同样不属于创作字段。

    `--inherit` 删掉三键让章节回落缺省；与显式值同时给时先删后设。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    path = s.get_chapter_path(args.chapter_id)
    from .locking import op_lock
    with op_lock(path, kind="chapter-set"):
        project = Project.load(path)
        changed = []
        if getattr(args, "inherit", False):
            for key in ("skill", "profile", "video_provider"):
                if project.data.pop(key, None) is not None:
                    changed.append(f"删 {key}")
        if getattr(args, "video_provider", None):
            store = ConfigStore.load(getattr(args, "config", None))
            ModelRouter(store).resolve_named("video", args.video_provider)   # 未登记/非视频别名当场失败
            project.data["video_provider"] = args.video_provider
            changed.append(f"video_provider={args.video_provider}")
        if getattr(args, "profile", None):
            skills.skill_for_profile(args.profile)      # 未登记画风当场失败，绝不落盘
            project.data["profile"] = args.profile
            changed.append(f"profile={args.profile}")
        if getattr(args, "skill", None):
            project.data["skill"] = skills.validate_skill(args.skill, bind=True)
            changed.append(f"skill={project.data['skill']}")
        title = (getattr(args, "title", None) or "").strip()
        if title:
            # 标题存两处（章节文档 chapter.title + 系列登记表），同批改
            project.data.setdefault("chapter", {})["title"] = title
            for c in s.chapters:
                if c.get("id") == args.chapter_id:
                    c["title"] = title
            changed.append(f"title={title}")
        for key in ("budget", "budget_per_call"):    # 额度闸只读章节文档顶层
            val = getattr(args, key, None)
            if val is not None:
                project.data[key] = val
                changed.append(f"{key}={val}")
        if not changed:
            raise KinemaError("没有要改的字段：--title / --skill / --profile / --video-provider / "
                              "--budget / --budget-per-call / --inherit 至少给一个")
        locked = review.chapter_locked(project.data.get("shots") or [],
                                       {c.split("=")[0].removeprefix("删 ") for c in changed})
        if locked:
            raise KinemaError(
                f"章节已有 {'/'.join(locked)} 通过锁定，profile / video_provider 改变生成输入——"
                "要重生置 retake，只解锁不重生置 wfa（review set --state …）")
        project.save()
        if title:
            s.save()
        print(f"✓ 章节 {s.pid}/{args.chapter_id} 绑定已更新：{' · '.join(changed)}")
    num = chapter_title_number(title)
    if num:
        print(f"  ⚠ 标题含序号「{num}」：序号由章节 id/order 与封面排版管理，"
              "标题应是本集剧情的裸短标题")
    if getattr(args, "profile", None) and (project.data.get("style_prompt") or "").strip():
        print("  ⚠ 本章写有 style_prompt（生图画风的单点真源，优先于 profile 前缀）"
              "——只改 profile 画面不会换风格")
    if getattr(args, "profile", None) and not getattr(args, "skill", None) \
            and project.data.get("skill"):
        print(f"  ⓘ 本章绑定 skill 仍是 {project.data['skill']}（旁白语态等派生随它）")


def _stage_wrapper(fn):
    def inner(args):
        store = ConfigStore.load(args.config)
        path = Path(_project_path(args))
        router = ModelRouter(store, force_mock=getattr(args, "mock", False))
        if getattr(args, "preview_json", False):
            project = Project.load(path)
            _apply_aspect_args(project, args)
            # 结构化预览（Studio「实发提示词」经子进程调用的出口，gen-image /
            # gen-video 两用）：独立分流——绝不走下面的 op_lock（只读，渲染进行中
            # 也要能预览）与收尾 project.save()（预览落盘=对用户章节的静默写入）。
            # 杂项打印全部吞掉，stdout 末行是唯一的 JSON 载荷（调用方按末行解析——
            # 配置层加载告警等更早的打印无从拦截）。
            import contextlib
            import io
            preview = (video_prompt_preview if fn is stage_gen_video
                       else image_prompt_preview)
            with contextlib.redirect_stdout(io.StringIO()):
                rows = preview(project, store, router,
                               only=getattr(args, "only", None))
            print(json.dumps(rows, ensure_ascii=False))
            return
        kw = {}
        if hasattr(args, "profile"):
            kw["profile"] = args.profile
        if hasattr(args, "force"):
            kw["force"] = args.force
        if getattr(args, "out", None) is not None:
            kw["out"] = args.out
        if getattr(args, "dry_run", False):     # gen-video 报价 / score 分段预览
            kw["dry_run"] = True
        if getattr(args, "only", None):          # gen-image / gen-video / tts 定向镜号
            kw["only"] = args.only
        if getattr(args, "fit_dur", False):      # tts：让画面等台词（放宽 dur）
            kw["fit_dur"] = True
        if getattr(args, "draft", False):        # score：按分镜起草（零成本）
            kw["draft"] = True
        if getattr(args, "switch", None) is not None:   # score：切换段版本（零成本）
            kw["switch"] = args.switch
            kw["to_v"] = getattr(args, "to_v", None)
        if getattr(args, "candidates", None):    # gen-image 宫格候选
            kw["candidates"] = args.candidates
        if getattr(args, "no_moodboard", False):  # gen-image 本次不套用参考库垫图（「不要垫图」）
            kw["no_moodboard"] = True
        if getattr(args, "accept_existing", False):
            kw["accept_existing"] = True
        if getattr(args, "hd", False):           # gen-image 本次按 provider 像素上限出图
            kw["hd"] = True
        if getattr(args, "concurrency", None) is not None:   # 生图/配音缺省4；gen-video 缺省1（显式才并发）
            kw["concurrency"] = args.concurrency
        if getattr(args, "approved_only", False):  # gen-video 草稿两段式正式档
            kw["approved_only"] = True
        if getattr(args, "ignore_refs", False):    # gen-video 越过就绪度节点
            kw["ignore_refs"] = True
        if getattr(args, "resolution", None):      # gen-video 分辨率运行时覆盖 config 默认
            kw["resolution"] = args.resolution
        if getattr(args, "yes", False):            # gen-video 4K 高成本档二次授权
            kw["yes"] = True
        if getattr(args, "confirm_spend", False):  # gen-video 单笔超阈（budget_per_call）二次确认
            kw["confirm_spend"] = True             # ← 漏这一行 = flag 加了却永远是默认值且不报错
        if getattr(args, "previz", False):         # gen-video 参考视频 V2V（previz 运动迁移）opt-in
            kw["previz"] = True
        if getattr(args, "control", False):        # gen-video 深度控制视频 V2V opt-in
            kw["control"] = True
        if getattr(args, "tail_relay", False):     # gen-video 尾帧接力 opt-in（章级字段亦可）
            kw["tail_relay"] = True
        if getattr(args, "anchor_frame", False):   # gen-video 首帧锚定 opt-in（章级/镜级字段亦可）
            kw["anchor_frame"] = True
        if getattr(args, "no_auto_cast", False):   # gen-video 本次跳过选角闸
            kw["no_auto_cast"] = True
        if getattr(args, "no_lipsync", False):     # gen-video 本次不做 dubbed 口型精修
            kw["no_lipsync"] = True
        if getattr(args, "video_provider", None):  # gen-video 运行时点名 provider 别名（如 seedance-2.5）
            kw["video_provider"] = args.video_provider
        # 章节操作锁：生成/合成阶段独占章节文档——两个操作各持旧内存副本
        # 交错写盘会互相覆盖对方回填的产物字段，预算判定也要求同章串行。
        # 锁先于装载：锁外装载的副本可能已被刚收尾的另一操作改过。
        # 表态类命令（review/consistency 等）走 Project.mutate，长任务期间照常可用。
        from .locking import op_lock
        with op_lock(path, kind=fn.__name__.removeprefix("stage_")):
            project = Project.load(path)
            _apply_aspect_args(project, args)
            fn(project, store, router, **kw)
            project.save()
    return inner


# ---------- 剧本改编（adapt）：Track A 纯 Python 承接 ----------
# 智能环节（拆书/分集/抽实体/拆镜）由 Claude 指挥层完成并直接写 adaptation/
# episodes/characters 进 JSON；这里只做机械承接：正文落盘 + 结构切分 + 幂等建章
# + 实体合并入库（合并不覆盖）。

def cmd_adapt_import(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    src = Path(args.file)
    if not src.is_file():
        raise ProjectError(f"找不到源文件: {src}")
    kind = None if args.kind == "auto" else args.kind
    r = s.ingest_source(filename=src.name, data=src.read_bytes(), kind=kind)   # 与 Studio 上传共用
    _step(f"源文本入库: {s.pid}")
    if r["encoding"] == "utf-8/replace":
        _info("⚠ 源文件编码无法识别，已按替换字符解码——正文可能损坏，"
              "请转存为 UTF-8 或 GBK 后重试")
    _info(f"类型={r['kind']} · 编码={r['encoding']} · {r['chars']} 字 · 按「{r['segment_kind']}」切分 {r['n_segments']} 段")
    _info(f"正文: {s.source_dir / 'raw.txt'}")
    _info(f"结构索引: {s.source_dir / 'segments.json'}（AI 拆书/分集据此定位原文段）")
    _info("下一步（AI 指挥层）：读 source/raw.txt + segments.json → 拆书写 adaptation → "
          "分集写 episodes[] → `adapt scaffold` 建章。见 kinema-project SKILL「剧本改编」。")


def cmd_adapt_scaffold(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    if not s.episodes:
        raise ProjectError("episodes[] 为空——请先由 AI 指挥层写入分集大纲"
                           "（见 `adapt show` / kinema-project SKILL「剧本改编」）")
    only = None
    if args.only:
        try:
            only = [int(x) for x in str(args.only).split(",") if x.strip()]
        except ValueError:
            raise ProjectError(f"--only 需为集号逗号分隔（如 1,3,5）：{args.only}")
        if not only:
            raise ProjectError(f"--only 未解析出有效集号：{args.only}")
    res = s.scaffold_episodes(only=only)
    _step(f"分集建章: {s.pid}")
    # 一章一集协议的出声核对：源按章标切分（segment_kind=chapter）时，分集数应
    # 等于源章节数（一章=一集=一个视频章节，绝不合并——多章压一集会丢关键情节
    # 与设定）。只告警不阻断：例外由用户点名（kinema-project SKILL 3.5 第 4 步），
    # 引擎不替人裁决；窗口化切分（无章标）没有「章数」可对，不判。
    seg_file = s.source_dir / "segments.json"
    if seg_file.is_file():
        try:
            digest = json.loads(seg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            digest = {}
        n_seg = digest.get("n_segments")
        if str(digest.get("segment_kind") or "") == "chapter" \
                and n_seg and int(n_seg) != len(s.episodes):
            _info(f"⚠ 分集数（{len(s.episodes)}）≠ 源章节数（{n_seg}）——协议是一章一集"
                  "（小说有多少章，视频就有多少章节）；若非用户点名的例外，"
                  "请按章补齐 episodes[] 再重跑 scaffold")
    if res["created"]:
        _info(f"新建 {len(res['created'])} 章: {', '.join(res['created'])}")
    if res["updated"]:
        _info(f"刷新大纲 {len(res['updated'])} 章: {', '.join(res['updated'])}")
    if res["warned"]:
        _info(f"⚠ 已拆镜章大纲变更（需人工核对是否重拆）: {', '.join(res['warned'])}")
    if not res["created"] and not res["updated"]:
        _info("无变化（幂等重跑）")
    _info("集号→章节: " + ("  ".join(f"{k}→{v}" for k, v in sorted(res["mapping"].items())) or "—"))
    s.sync_design_to_chapters()   # 设定集回灌已建章节（与 project refs 收尾同制度）
    _info("下一步：`project refs` 生设定图 → 逐章 `gen-image`（照 chapter.outline 拆 shots）")


def cmd_adapt_show(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    src = s.data.get("source") or {}
    ad = s.data.get("adaptation") or {}
    eps = s.episodes
    print(f"改编 · {s.pid}  「{s.data.get('title', '')}」")
    if src:
        print(f"  源文本: {src.get('kind')} · {src.get('chars')} 字 · {src.get('file')}  [{src.get('sha256')}]")
    else:
        print("  源文本: —（尚未 `adapt import`）")
    if ad:
        print("  拆书:")
        for k, label in (("mainline", "主线"), ("core_conflict", "贯穿冲突"),
                         ("world_bible", "世界观宪法"), ("cut_unit", "分集单位")):
            if ad.get(k):
                print(f"    {label}: {ad[k]}")
        for k, label in (("set_pieces", "名场面"), ("cool_points", "爽点")):
            if ad.get(k):
                print(f"    {label}: {' / '.join(ad[k])}")
    else:
        print("  拆书: —（尚未由 AI 写入 adaptation）")
    print(f"  分集大纲 ({len(eps)}):")
    for ep in eps:
        cid = ep.get("chapter_id")
        tag = f" → {cid}" if cid else "（未建章）"
        print(f"    [{ep.get('no')}] {ep.get('title', '')} {tag}")
        if ep.get("logline"):
            print(f"        {ep['logline']}")
    if not eps:
        print("    —（尚未由 AI 写入 episodes[]）")
    g = s.data.get("graph") or {}
    gn, ge = g.get("nodes") or [], g.get("edges") or []
    if gn:
        print(f"  关系图谱: {len(gn)} 节点 · {len(ge)} 关系")
        if g.get("summary"):
            print(f"    {g['summary']}")
    else:
        print("  关系图谱: —（尚未由 AI 写入 graph）")


def cmd_adapt_merge_entities(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    f = Path(args.file)
    if not f.is_file():
        raise ProjectError(f"找不到候选实体文件: {f}")
    try:
        payload = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ProjectError(f"候选实体不是合法 JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ProjectError('候选实体 JSON 顶层须为对象：{"characters":[...],"props":[...]}')
    chars, props = payload.get("characters"), payload.get("props")
    if chars is not None and not isinstance(chars, list):
        raise ProjectError("characters 须为数组")
    if props is not None and not isinstance(props, list):
        raise ProjectError("props 须为数组")
    stats = s.upsert_entities(characters=chars, props=props)
    s.sync_design_to_chapters()
    _step(f"实体合并入库: {s.pid}")
    _info(f"新增 {stats['added']} · 更新 {stats['updated']}"
          "（合并不覆盖·保人工 voice/keywords/comments·keywords 取并集）")


def cmd_adapt_graph(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    f = Path(args.file)
    if not f.is_file():
        raise ProjectError(f"找不到图谱文件: {f}")
    try:
        payload = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ProjectError(f"图谱不是合法 JSON: {e}") from e
    stats = s.set_graph(payload)   # 整体替换 + 校验悬空边（见 Series.set_graph）
    _step(f"关系图谱入库: {s.pid}")
    _info(f"{stats['nodes']} 节点 · {stats['edges']} 关系（整体替换·AI 指挥层真源）")
    _info("下一步：Studio 剧本工作台「图谱」Tab 查看可视化关系网 + 核心知识点缩写。")


# ---------- 原创小说创作（novel）：与 adapt 互补的「边写边长」承接 ----------
# adapt 面向「一次性导入既有全本」，novel 面向「自己一章章写」。写正文/判文风/
# 判人设的智能一律归 Claude 指挥层（kinema-novel SKILL 的每章五步闭环）；
# 这里只做机械承接：正文落盘+版本归档+字数指纹+实体命中统计+伏笔账本状态机+
# 里程碑检查点+跨章确定性 lint。域逻辑单一真源 kinema/novel.py。

def cmd_novel_init(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    N.manuscript_dir(s)
    fields = {k: getattr(args, k) for k in ("pov", "tense", "voice", "diction")}
    avoid = [x.strip() for x in (args.avoid or "").split(",") if x.strip()]
    with s.commit():
        style = s.data.setdefault("narrative_style", {})
        for k, v in fields.items():
            if v:
                style[k] = v
        if avoid:
            merged = list(style.get("avoid") or [])
            for w in avoid:
                if w not in merged:
                    merged.append(w)
            style["avoid"] = merged
        style.setdefault("baseline", [])
    _step(f"小说创作层初始化: {s.pid}")
    _info(f"正文目录: {N.manuscript_dir(s)}（.md 非媒体后缀·永不上 OSS）")
    st = s.data.get("narrative_style") or {}
    _info("文风契约 narrative_style: " + (" · ".join(
        f"{k}={st[k]}" for k in ("pov", "tense", "voice", "diction") if st.get(k))
        or "（空，待指挥层填）"))
    if not st.get("baseline"):
        _info("⚠ baseline 为空——文风门没有基线样本可锚：从认可的正文摘 2~3 段"
              "写进 narrative_style.baseline（防漂靠基线比对，不靠每章复述风格）")
    _info("下一步（AI 指挥层）：按 kinema-novel SKILL 的每章五步闭环开写；"
          "每写完一章 `novel save` 登记。")


def cmd_novel_save(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    f = Path(args.file)
    if not f.is_file():
        raise ProjectError(f"找不到正文文件: {f}")
    state = None
    if getattr(args, "state", None):
        sf = Path(args.state)
        if not sf.is_file():
            raise ProjectError(f"找不到状态文件: {sf}")
        try:
            state = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise ProjectError(f"状态快照不是合法 JSON: {e}") from e
    r = N.save_chapter(s, no=args.no,
                       text=f.read_text(encoding="utf-8", errors="replace"),
                       title=args.title, digest=args.digest, state=state,
                       payoff=getattr(args, "payoff", None),
                       payoff_kind=getattr(args, "payoff_kind", None),
                       hook=getattr(args, "hook", None))
    _step(f"第 {r['no']} 章正文登记: {s.pid}"
          + ("（内容未变·幂等）" if r["noop"] else ""))
    _info(f"{r['chars']} 字 · {r['sha256']} · {r['file']}")
    if r["archived"]:
        _info(f"旧稿归档: v{r['archived']['v']} → {r['archived']['file']}")
    ents = [n for k in ("characters", "props", "scenes") for n in r["entities"][k]]
    _info("本章命中已登记实体: " + ("、".join(ents) if ents else "无"))
    _info("⚠ 引擎只认得**已登记**实体——本章若有新角色/NPC/场景/道具或旧设定变更，"
          "及时回写：`character add` / `adapt merge-entities` / `scene add` / "
          "`prop add`，再 `project refs` 补设定图")
    todo = []
    if r["missing_digest"]:
        todo.append(f"`novel digest {s.pid} --no {r['no']} --text \"…\"`（精简大纲·两三句）")
    if r["missing_state"]:
        todo.append(f"`novel state {s.pid} --no {r['no']} --file state.json`（章末状态快照）")
    if todo:
        _info("本章必做（下一章写前要读）: " + " · ".join(todo)
              + "  ← 也可以在 `novel save` 里用 --digest/--state 一次写完")
    _info(f"进度: 已登记 {r['count']} 章 · 累计 {r['total_chars']} 字")
    if r["checkpoint"]:
        # recap/lint 的 --from/--to 是**真章号**：拿章数当章号，接盘导入 51~63
        # 再写到 70 时会让 agent 在 11~20 的空窗口上做七门复核
        lo = max(1, r["no"] - N.MILESTONE_EVERY + 1)
        _step(f"★ 检查点已满档（第 {r['no']} 章）——**先复核再续写**，"
              "按 kinema-novel SKILL「七门复核 + 批次报告」执行：")
        _info(f"① 取料: `novel recap {s.pid} --from {lo} --to {r['no']}`"
              f" + `novel lint {s.pid} --from {lo} --to {r['no']}`")
        _info("② 七门复核: 合宪(world_bible/卷纲) · 人设(taboo_lines 盲测) · "
              "连贯(state/时间线/持有物) · AI 味(lint 口癖/带区/复读) · "
              "文风(对 baseline_metrics 的 z 分) · 伏笔清账 · 节奏(payoff 间隔)")
        _info("③ 设定对账回写: `character set` / `prop set` / `scene set` / "
              "`adapt graph` / `novel bible`（弧光推进、换装、断剑都要落回去）")
        _info(f"④ 《批次报告》先落盘 project/{s.pid}/plan/batch-{lo}-{r['count']}.md，"
              f"再 `novel log {s.pid} --kind checkpoint --at {r['count']} --ref <路径> "
              "--text \"…\"` 留痕——跨会话接手第 2 步读的就是它")
        _info("⑤ 报告给用户看**逐章概要**，然后**停下等指令**——别自己接着往下写")


def cmd_novel_digest(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.set_digest(s, args.no, args.text)
    _step(f"第 {r['no']} 章精简大纲已登记: {s.pid}")
    _info(r["digest"])


def cmd_novel_state(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    f = Path(args.file)
    if not f.is_file():
        raise ProjectError(f"找不到状态文件: {f}")
    try:
        payload = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ProjectError(f"状态快照不是合法 JSON: {e}") from e
    r = N.set_state(s, args.no, payload)
    _step(f"第 {r['no']} 章章末状态已登记: {s.pid}")
    for k in ("time", "location"):
        if r["state"].get(k):
            _info(f"{k}: {r['state'][k]}")
    for name, note in (r["state"].get("characters") or {}).items():
        _info(f"· {name}: {note}")
    for h in r["state"].get("hooks") or []:
        _info(f"⤷ 悬念: {h}")


def cmd_novel_thread_add(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    t = N.thread_add(s, title=args.title, setup=args.setup, due=args.due,
                     tier=args.tier, note=args.note or "")
    _step(f"伏笔登记: {t['id']}「{t['title']}」")
    _info(f"埋于第 {t['setup']} 章"
          + (f" · 期限第 {t['due']} 章" if t.get("due") else " · 无期限（lint 会盯挂起时长）"))


def cmd_novel_thread_pay(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    t = N.thread_mark(s, args.id, status="paid", paid_in=args.paid_in, note=args.note)
    _step(f"伏笔回收: {t['id']}「{t['title']}」→ 第 {t['paid_in']} 章")


def cmd_novel_thread_drop(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    t = N.thread_mark(s, args.id, status="dropped", note=args.note)
    _step(f"伏笔弃置: {t['id']}「{t['title']}」（记录在案，不再追讨）")


def cmd_novel_lint(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.lint(s, frm=args.frm, to=args.to)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    win = r.get("window")
    rows = [it for it in r["findings"]
            if not (getattr(args, "level", None) == "warn" and it["level"] != "warn")]
    lv = r.get("levels") or {}
    _step(f"小说跨章体检: {s.pid} · {r['chapters']} 章 · {r['total_chars']} 字"
          + (f" · 文体窗口 第 {win[0]}~{win[1]} 章" if win else "")
          + f" · ⚠{lv.get('warn', 0)} 待办 / {lv.get('info', 0)} 提示")
    if not rows:
        _info("干净（无发现）" if not r["findings"] else "（本级别下无发现）")
    for it in rows:
        _info(("⚠ " if it["level"] == "warn" else "· ") + f"[{it['code']}] {it['msg']}")
    _info("必修类: gap / digest_missing / state_missing / manuscript_drift"
          "（这四类归零才算这批写完）· 必处置: thread_expired")
    _info("（lint 只出可测量量——AI 味/人设 OOC/文风崩没崩，判定权在指挥层七门复核）")


def cmd_novel_show(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    o = N.overview(s)
    if getattr(args, "json", False):
        print(json.dumps(o, ensure_ascii=False, indent=2))
        return
    print(f"小说创作 · {s.pid}  「{s.data.get('title', '')}」")
    print(f"  进度: {o['count']} 章 · {o['total_chars']} 字 · "
          f"下一检查点第 {o['next_checkpoint']} 章"
          + ("（本档已满·先做七门复核）" if o["checkpoint_due"] else ""))
    st = o["narrative_style"]
    if st:
        line = " · ".join(f"{k}={st[k]}" for k in ("pov", "tense", "voice", "diction")
                          if st.get(k))
        print(f"  文风: {line or '—'} · 基线样本 {len(st.get('baseline') or [])} 段"
              f" · 数值基线 {'已立' if st.get('baseline_metrics') else '未立'}"
              f" · 忌讳词 {len(st.get('avoid') or [])} 个")
    for e in o.get("log") or []:
        print(f"  ✎ [{e.get('kind')}]"
              + (f" 第{e['at_chapter']}章" if e.get("at_chapter") else "")
              + f" {e.get('text', '')[:78]}"
              + (f"  → {e['ref']}" if e.get("ref") else ""))
    # 逐章清单缺省折叠：350 章的项目全量输出达 470KB，一条 show 即超出上下文预算
    chs = o["chapters"]
    if not getattr(args, "all", False) and len(chs) > 10:
        print(f"  逐章登记态: 共 {len(chs)} 章，只列最近 10 章（全表 --all / --json）")
        chs = chs[-10:]
    for c in chs:
        marks = ("✓大纲" if (c.get("digest") or "").strip() else "✗大纲",
                 "✓状态" if c.get("state") else "✗状态")
        print(f"  [{c['no']:>3}] {c.get('title') or '（无题）'} · {c.get('chars', 0)} 字 "
              f"· {' '.join(marks)}"
              + (f" · v{len(c['versions'])}+1 版" if c.get("versions") else ""))
        if (c.get("digest") or "").strip():
            print(f"        {c['digest']}")
    tv, full = o["threads"], getattr(args, "all", False)
    if tv["open"] or tv["paid"] or tv["dropped"]:
        print(f"  伏笔账本: open {len(tv['open'])} · paid {len(tv['paid'])} · "
              f"dropped {len(tv['dropped'])} · ⚠超期 {len(tv['expired'])}")
        opens = sorted(tv["open"], key=lambda t: (
            not t.get("expired"), t.get("due") is None,
            int(t.get("due") or 10 ** 9)))
        for t in (opens if full else opens[:8]):
            flag = " ⚠超期" if t.get("expired") else ""
            due = f" · 期限第 {t['due']} 章" if t.get("due") else " · 无期限"
            print(f"    ○ {t['id']}「{t['title']}」埋于第 {t['setup']} 章{due}{flag}")
        if not full and len(opens) > 8:
            print(f"    …另有 {len(opens) - 8} 条未回收（--all 看全表）")
        if full:
            for t in tv["paid"]:
                print(f"    ● {t['id']}「{t['title']}」第 {t['setup']} → "
                      f"{t['paid_in']} 章 已回收")
    av = o["arcs"]
    if av["arcs"]:
        MARK = {"done": "✔", "writing": "▶", "planned": "○"}
        rows = av["arcs"]
        if not full and len(rows) > 5:
            i = max(0, next((k for k, a in enumerate(rows)
                             if a["state"] == "writing"), len(rows) - 1) - 1)
            rows = rows[i:i + 4]
        print(f"  卷/幕规划 ({len(av['arcs'])}"
              + ("" if full or rows is av["arcs"] else "，只列当前卷前后") + "):")
        for a in rows:
            rng = (f"第 {a.get('from')}~{a.get('to')} 章" if a.get("to")
                   else f"第 {a.get('from')} 章起")
            print(f"    {MARK[a['state']]} 卷{a['no']}「{a.get('title') or ''}」{rng}"
                  + (f" · {a['goal']}" if a.get("goal") else ""))
    else:
        print(f"  卷/幕规划: 无（`novel arc {s.pid} --no 1 --title … --from 1 --to 30` "
              "立纲——检查点第一门「有没有跑偏大纲」要有对照物）")


def _print_arc_body(a, *, indent="    "):
    """卷纲正文的**单一渲染口径**——`novel arcs` 与 `novel brief` 共用。

    各写一份必分叉：若某一侧漏印 turns（节拍），检查点第①门「有没有跑偏大纲」
    唯一的逐章对照物就被写前必读包丢掉。
    """
    for k, zh in (("premise", "前提"), ("goal", "本卷目标"), ("climax", "高潮"),
                  ("note", "备注")):
        if a.get(k):
            print(f"{indent}{zh}: {a[k]}")
    for t in a.get("turns") or []:
        print(f"{indent}· 节拍: {t}")


def _arc_kw(args):
    return {"title": args.title, "frm": args.frm, "to": args.to,
            "premise": args.premise, "goal": args.goal, "climax": args.climax,
            "turns": args.turn, "note": args.note}


def cmd_novel_arc_set(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    a = N.arc_upsert(s, no=args.no, **_arc_kw(args))
    _step(("卷规划登记: " if a["created"] else "卷规划更新: ")
          + f"卷{a['no']}「{a.get('title') or ''}」")
    if a.get("from"):
        _info(f"覆盖 第 {a['from']}~{a.get('to') or '？'} 章"
              + (f" · 目标: {a['goal']}" if a.get("goal") else ""))
    _info("（进度态是派生判定不落盘——写到哪一卷由最新章号现算，同伏笔超期）")


def cmd_novel_arc_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    a = N.arc_rm(s, args.no)
    _step(f"已删卷规划: 卷{a['no']}「{a.get('title') or ''}」")


def cmd_novel_arc_list(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    v = N.arcs_view(s)
    if getattr(args, "json", False):
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return
    print(f"卷/幕规划 · {s.pid} · 已写到第 {v['current_no']} 章")
    if not v["arcs"]:
        print(f"  （无——`novel arc {s.pid} --no 1 --title … --from 1 --to 30` 立纲）")
    for a in v["arcs"]:
        mark = {"done": "✔ 已收卷", "writing": "▶ 进行中", "planned": "○ 未开写"}[a["state"]]
        print(f"  卷{a['no']}「{a.get('title') or ''}」 第 {a.get('from')}~"
              f"{a.get('to') or '？'} 章 · {mark}")
        _print_arc_body(a, indent="      ")
    for g in v["gaps"]:
        print(f"  · 断档: 第 {g['at'][0]}~{g['at'][1]} 章不属于任何一卷")
    for o in v["overlaps"]:
        print(f"  ⚠ 重叠: 卷{o['a']} 与 卷{o['b']} 同覆盖第 {o['at'][0]}~{o['at'][1]} 章")


def cmd_novel_brief(args):
    """写前必读包：把「五处翻查」压成一次调用。长篇最常见的失败机制是写第 60 章时
    前 59 章的约束不在上下文——全书回灌超出预算，凭印象写则必然偏离既有约束。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    chars = [x.strip() for x in (args.chars or "").split(",") if x.strip()]
    want_bible = [x.strip() for x in (getattr(args, "bible", None) or "").split(",")
                  if x.strip()]
    b = N.brief(s, no=args.no, chars=chars or None, all_chars=args.all_chars,
                bible=want_bible or None)
    if getattr(args, "json", False):
        print(json.dumps(b, ensure_ascii=False, indent=2))
        return
    print(f"【写前必读 · {s.pid} 第 {b['no']} 章】")
    if b["checkpoint_due"]:
        print(f"  ★ 检查点已满档（{b['current_no']} 章）——先做七门复核出批次报告，"
              "别直接开写下一章")
    st = b["narrative_style"]
    line = " · ".join(f"{k}={st[k]}" for k in ("pov", "tense", "voice", "diction")
                      if st.get(k))
    print(f"■ 文风契约: {line or '（未立契）'}")
    if st.get("avoid"):
        print(f"    忌讳词: {'、'.join(st['avoid'])}")
    for i, seg in enumerate(st.get("baseline") or [], 1):
        print(f"    基线{i}: {seg[:120]}" + ("…" if len(seg) > 120 else ""))
    if not (st.get("baseline") or []):
        print("    ⚠ 无基线样本——**文风门空转**（不是通过）："
              f"`novel style {s.pid} --add-baseline 认可的正文.md`")
    # 世界观宪法：**按节取**。全量在长篇上已经贵到只剩「全读/全不读」两个选项
    # （第 350 章全量 195KB / --no-bible 16.7KB），而宪法本身是分好节的。
    # 缺省给目录 + 按本章相关性挑的那几节；点名 `--bible 三,七` 或 `--bible all`。
    if b["world_bible"] and not args.no_bible:
        picked = {x["title"] for x in b["bible_sections"]}
        got = sum(x["chars"] for x in b["bible_sections"])
        print(f"■ 世界观宪法: {len(b['bible_toc'])} 节 / {b['bible_total']} 字"
              f"——本章取 {len(picked)} 节 / {got} 字"
              f"（{int(got * 100 / max(1, b['bible_total']))}%）")
        print("    目录（要哪节 `--bible 关键词,关键词`，全量 `--bible all`）:")
        for x in b["bible_toc"]:
            print(f"      {'▣' if x['title'] in picked else '□'} "
                  f"{x['title']}（{x['chars']} 字）")
        for x in b["bible_sections"]:
            print()
            for ln in str(x["body"]).splitlines():
                if ln.strip():
                    print(f"    {ln.strip()}")
    elif b["world_bible"]:
        print(f"■ 世界观宪法: 已省略（--no-bible）· {b['bible_total']} 字 / "
              f"{len(b['bible_toc'])} 节")
    print(f"■ 未回收伏笔 ({len(b['open_threads'])}，快到期的排前面)")
    for t in b["open_threads"]:
        print(f"    {'⚠' if t.get('expired') else '○'} {t['id']}「{t['title']}」"
              f"埋于第 {t['setup']} 章"
              + (f" · 期限第 {t['due']} 章" if t.get("due") else " · 无期限")
              + (f" · {t['note']}" if t.get("note") else ""))
    print(f"■ 在场角色人设卡 ({len(b['characters'])})"
          + ("（默认取上一章在场者；--chars 点名 / --all 全表）" if not args.chars else ""))
    for c in b["characters"]:
        print(f"    ▸ {c['name']}" + (f"（{c['role']}）" if c.get("role") else ""))
        for k, zh in (("speech_style", "口吻"), ("personality", "性格"),
                      ("arc", "弧光"), ("appearance", "外貌")):
            if c.get(k):
                print(f"        {zh}: {c[k]}")
        if c.get("taboo_lines"):
            print(f"        禁区: {'、'.join(c['taboo_lines'])}")
    if b["unknown_chars"]:
        print(f"    ⚠ 角色表里没有: {'、'.join(b['unknown_chars'])}"
              "（名字写错，或该 character add 补登记）")
    if b["thin_personas"]:
        # 「没有料可比」绝不等于「比对通过」（同 consistency 的 REASONS 纪律）：
        # 缺 speech_style/taboo_lines 的角色，其第②门人设盲测在物理上是空转的
        print(f"    ⚠ 人设卡不全（口吻/性格/弧光/禁区四件缺项）: "
              f"{'、'.join(b['thin_personas'])}"
              f" → 这些人的第②门盲测是空转，`character set {s.pid} --name X "
              "--speech-style … --taboo …` 补足")
    if b["chars_capped"]:
        print(f"    （角色多，只列了前 {N.BRIEF_CHAR_CAP} 个——用 --chars 点名要谁）")
    p = b["prev"]
    if p:
        print(f"■ 上一章（第 {p['no']} 章 {p.get('title') or ''}）")
        if p.get("digest"):
            print(f"    大纲: {p['digest']}")
        stt = p.get("state") or {}
        if not stt:
            print("    ⚠ 无章末状态快照——下一章的位置/时间/持有物无锚可对")
        for k, zh in (("time", "时间"), ("location", "地点"), ("note", "备注")):
            if stt.get(k):
                print(f"    {zh}: {stt[k]}")
        for name, note in (stt.get("characters") or {}).items():
            print(f"    · {name}: {note}")
        for hk in stt.get("hooks") or []:
            print(f"    ⤷ 悬念: {hk}")
    for d in b["recent_digests"][:-1]:
        if d.get("digest"):
            print(f"    〔第 {d['no']} 章回顾〕{d['digest']}")
    # 当前卷纲排在最后 = 最靠近落笔处（长料在前、本章要写什么在最末）
    a = b["arc"]
    print("■ 当前卷（本章要推进的就是它）: "
          + (f"卷{a['no']}「{a.get('title') or ''}」第 {a.get('from')}~"
             f"{a.get('to') or '？'} 章" if a else "（无卷规划——第①门无对照物）"))
    if a:
        _print_arc_body(a)
    bg = b["budget"]
    print("■ 取料账: " + " · ".join(f"{k} {v}" for k, v in bg.items() if k != "合计")
          + f" → 合计 {bg['合计']} 字符")


def cmd_novel_recap(args):
    """批次复核物料：《批次报告》的骨架。逐项数出来的，不是凭印象复述的。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.recap(s, frm=args.frm, to=args.to)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if not r["count"]:
        _info("窗口内没有已登记章节")
        return
    print(f"【批次复核物料 · {s.pid} 第 {r['from']}~{r['to']} 章】"
          f"{r['count']} 章 · {r['total_chars']} 字 · 均 {r['avg_chars']} 字/章")
    print("\n逐章概要（直接进《批次报告》给用户看）:")
    print("| 章 | 标题 | 字数 | 概要 |")
    print("|---|---|---|---|")
    long_digest = False
    for c in r["chapters"]:
        d = (c["digest"] or "⚠ 缺精简大纲").replace("\n", " ").replace("|", "｜")
        if len(d) > 180:                   # 表格是给人看的：过长的大纲截断，全文走 --json
            d, long_digest = d[:180] + "…", True
        print(f"| {c['no']} | {c['title'] or '（无题）'} | {c['chars']} | {d} |")
    if long_digest:
        print("（表内大纲已截断——全文走 `--json`；digest 本该是两三句：事件+变化+尾钩）")
    if r["missing_digest"]:
        print(f"⚠ 缺大纲: 第 {', '.join(map(str, r['missing_digest']))} 章"
              "（连读审连贯全靠它，补 `novel digest`）")
    if r["missing_state"]:
        print(f"· 缺章末状态: 第 {', '.join(map(str, r['missing_state']))} 章")
    th = r["threads"]
    print(f"\n伏笔动静: 本批新埋 {len(th['opened'])} · 本批回收 {len(th['paid'])} · "
          f"仍欠 {len(th['open'])} · ⚠超期 {len(th['expired'])}")
    for t in th["opened"]:
        print(f"  + {t['id']}「{t['title']}」埋于第 {t['setup']} 章"
              + (f" · 期限第 {t['due']} 章" if t.get("due") else " · 无期限"))
    for t in th["paid"]:
        print(f"  ✓ {t['id']}「{t['title']}」第 {t['setup']} → {t['paid_in']} 章 回收")
    for t in th["expired"]:
        print(f"  ⚠ {t['id']}「{t['title']}」已超期（期限第 {t['due']} 章）")
    ne = r["new_entities"]
    if any(ne.values()):
        print("\n本批首次登场的**已登记**实体:")
        for kind, zh in (("characters", "角色"), ("scenes", "场景"), ("props", "道具")):
            if ne[kind]:
                print(f"  {zh}: " + "、".join(f"{x['name']}(第{x['no']}章)" for x in ne[kind]))
    print("  ⚠ 正文里反复出现却不在上表的名字 = 漏登记的设定，"
          "当场 `character add`/`scene add`/`prop add` 补上（引擎认不出新实体）")
    for a in r["arcs"]:
        print(f"\n所属卷: 卷{a['no']}「{a.get('title') or ''}」第 {a.get('from')}~"
              f"{a.get('to') or '？'} 章 · "
              + {"done": "本批收卷", "writing": "进行中", "planned": "未开写"}[a["state"]]
              + (f" · 目标: {a['goal']}" if a.get("goal") else ""))
    pc = r["pacing"]
    if pc["declared"]:
        print(f"\n节奏账（第⑦门）: {pc['declared']}/{r['count']} 章声明了 payoff · "
              + "、".join(f"{k}×{v}" for k, v in pc["by_level"].items())
              + ("  断章型: " + "、".join(f"{k}×{v}" for k, v in pc["hooks"].items())
                 if pc["hooks"] else ""))
    p = r["prose"]
    print(f"\n文体量化（AI 味自检的可测量面 · 已剥 markdown）: "
          f"句长均值 {p['avg_sentence_len']} 字（离散比 {p['sd_ratio']}） · "
          f"长句占比 {int(p['long_ratio'] * 100)}% · 短句 {int(p['short_ratio'] * 100)}% · "
          f"对白占比 {int(p['dialogue_ratio'] * 100)}% · "
          f"≥40字句 {p['long40_ratio']} · 明喻 {p['simile_per_k']}/千字 · "
          f"三连顿号 {p['tri_list_per_k']}/千字 · 用词多样度 "
          + (str(p["mattr"]) if p["mattr"] is not None
             else "—（本批不足一个测量窗口）"))
    for bd in r["bands"]:
        print(f"  ⚠ {bd['label']} {bd['value']} "
              f"{'低于' if bd['side'] == 'low' else '高于'}带区 "
              f"{bd['band'][0] if bd['side'] == 'low' else bd['band'][1]} → {bd['hint']}")
    mk = p.get("markup") or {}
    if mk.get("bold_paragraphs") or mk.get("rules"):
        print(f"  ⚠ 正文里的 markdown: **非面板**整段加粗 {mk['bold_paragraphs']} 段"
              f"（{int(mk['bold_para_ratio'] * 100)}%；另有 {mk['panel_paragraphs']} "
              f"段面板行属正常）· 分隔线 {mk['rules']} 行 → `novel normalize {s.pid}`")
    hot = [x for x in p["slop"] if x["n"] >= N.SLOP_MIN_HITS]
    if hot:
        print("  口癖命中（→ 是改写方向，不是禁令）:")
        for it in hot[:8]:
            print(f"    「{it['term']}」×{it['n']}（每千字 {it['per_k']}）→ {it['hint']}")
    for k, n in p["head_repeats"]:
        print(f"  · {n} 个段落以「{k}」开头")
    for x in r["repeats"]:
        print(f"  · 复读短语「{x['phrase']}」×{x['n']}"
              + "（第 " + "/".join(map(str, x["chapters"][:4])) + " 章）")
    print("\n（引擎只交数不交结论——七门复核的判定与《批次报告》的措辞归指挥层）")


def cmd_novel_thread_set(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    t = N.thread_set(s, args.id, title=args.title, setup=args.setup, due=args.due,
                     tier=args.tier, note=args.note)
    _step(f"伏笔更新: {t['id']}「{t['title']}」")
    _info(f"埋于第 {t['setup']} 章"
          + (f" · 期限第 {t['due']} 章" if t.get("due") else " · 无期限")
          + (f" · {t['tier']} 线" if t.get("tier") else ""))


def cmd_novel_log(args):
    """创作日志：跨会话唯一的「上次是怎么判的」载体。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    if args.text is None:
        rows = N.log_view(s.data, kind=args.kind, limit=args.limit or 0)
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        print(f"创作日志 · {s.pid}（{len(rows)} 条）")
        if not rows:
            print(f"  （空——检查点做完必须 `novel log {s.pid} --kind checkpoint "
                  "--at N --ref plan/batch-N-M.md --text \"…\"` 记一条，"
                  "否则复核结论一到新会话就没了）")
        for e in rows:
            print(f"  [{e.get('kind')}]"
                  + (f" 第{e['at_chapter']}章" if e.get("at_chapter") else "")
                  + f" {e.get('at', '')[:16]}")
            print(f"      {e.get('text', '')}")
            if e.get("ref"):
                print(f"      → {e['ref']}")
        return
    e = N.log_add(s, kind=args.kind, text=args.text, at=args.at, ref=args.ref)
    _step(f"创作日志已记: [{e['kind']}]"
          + (f" 第{e['at_chapter']}章" if e.get("at_chapter") else ""))
    _info(e["text"][:160])


def cmd_novel_sweep(args):
    """跨七层检索一个词——「改设定＝改七层」的收工判据。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.sweep(s, args.term, min_len=args.min_len)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    ZH = {"manuscript": "① 正文", "entities": "② 设定卡(角色/道具/场景)",
          "digest": "③ 章节 digest", "state": "④ 章末 state",
          "arcs": "⑤ 卷纲", "threads": "⑥ 伏笔账本",
          "bible": "⑦ 宪法/主线/文风/图谱"}
    _step(f"跨层检索「{r['term']}」· {s.pid} · 共 {r['total']} 处")
    for k in N.SWEEP_LAYERS:
        L = r["layers"][k]
        if not L["n"]:
            print(f"  {ZH[k]}: 0")
            continue
        print(f"  {ZH[k]}: {L['n']} 处")
        for row in L["rows"]:
            print(f"      {row['where']}  {row['line']}")
        if L["more"]:
            print(f"      …另有 {L['more']} 处")
    if r["total"]:
        _info("收工判据：命中归零，或每一条都能逐条说出「为什么可以留」——"
              "三、四、七这三层最容易漏，因为它们不出现在正文里，"
              f"而 brief/recap/检查点恰恰从它们取料。处置完 `novel log {s.pid} "
              "--kind overhaul --text \"…\"` 记一条")
    else:
        _info("七层归零 ✓")


def cmd_novel_reindex(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.reindex(s, no=args.no, archive=args.archive)
    _step(f"稿件重登记: {s.pid} · 更新 {len(r['fixed'])} 章 · "
          f"无变化 {len(r['kept'])} 章")
    if r["fixed"]:
        _info("已按磁盘重算字数/指纹/实体命中: 第 "
              + ", ".join(map(str, r["fixed"][:20]))
              + ("…" if len(r["fixed"]) > 20 else "") + " 章")
    for a in r["archived"]:
        _info(f"留档: 第 {a['no']} 章 → {a['file']}")
    if r["missing"]:
        _info(f"⚠ 登记了但文件不在: 第 {', '.join(map(str, r['missing']))} 章")


def cmd_novel_normalize(args):
    """正文排版规范化：剥掉非面板的加粗（执行铁律「粗体只给面板」）。"""
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.normalize(s, no=args.no, dry_run=args.dry_run)
    tt = r["totals"]
    _step(("【预演】" if r["dry_run"] else "") + f"正文排版规范化: {s.pid} · "
          f"扫 {r['scanned']} 章 · 需改 {len(r['changed'])} 章")
    _info(f"整段加粗 {tt['paragraph_bold']} 段 · 行内加粗 {tt['inline_bold']} 处 "
          f"→ 剥；面板/系统行 {tt['kept_panel']} 处 → 保留")
    for c in r["changed"][:8]:
        _info(f"  第 {c['no']} 章 {c['before']}→{c['after']} 字"
              f"（段 {c['paragraph_bold']} · 行内 {c['inline_bold']}）")
    if len(r["changed"]) > 8:
        _info(f"  …另有 {len(r['changed']) - 8} 章")
    if r["dry_run"]:
        _info("以上一个字都没写盘——去掉 --dry-run 才真的改")
    elif r["changed"]:
        _info(f"旧稿已逐章进版本栈，可回滚: `novel revert {s.pid} --no N`")
    if r["rules"]:
        _info(f"⚠ 另有 {r['rules']} 行 `---` 分隔线**本命令刻意不碰**——"
              "「这一条是断场还是节拍停顿」必须读上下文才判得出（同一章里两种都有），"
              "没有安全的机械判据。要治就人工逐章判，引擎只负责数出来")


def cmd_novel_revert(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.revert(s, no=args.no, v=args.v)
    _step(f"第 {r['no']} 章已回滚到 v{r['restored']['v']}（{r['chars']} 字）")
    if r["archived_v"]:
        _info(f"回滚前那一版已归档 v{r['archived_v']}（reason=rollback-out，可再滚回去）")
    _info("⚠ 该章的 digest/state 不会跟着回滚——正文变了就连读一遍下一章开头验衔接")


def cmd_novel_versions(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    rows = N.version_files(s, args.no)
    e = N.find_entry(s, args.no) or {}
    print(f"第 {args.no} 章版本谱系 · {s.pid} · 当前 {e.get('chars', 0)} 字 "
          f"{e.get('sha256', '')}")
    if not rows:
        print("  （无历史版本——每次 novel save 改稿才会归档旧版）")
    for v in rows:
        why = next((h.get("reason") for h in (e.get("versions") or [])
                    if h.get("v") == v["v"]), "")
        print(f"  v{v['v']}  {v['chars']} 字  {v['file']}"
              + (f"  ({why})" if why else ""))
    if rows:
        print(f"  回滚: `novel revert {s.pid} --no {args.no} --v {rows[-1]['v']}`")


def cmd_novel_baseline(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.baseline_metrics(s, frm=args.frm, to=args.to)
    _step(f"文风数值基线已立: {s.pid} · 取第 {r['from']}~{r['to']} 章的 "
          f"{r['n_chapters']} 章")
    for k in N.METRIC_KEYS:
        _info(f"{k}: {r[k]} ± {r[k + '_sd']}")
    _info("此后 `novel lint` 的文体段会对这条基线报 z 分（|z|>2 才提），"
          "z 恒现算不落盘——同伏笔超期纪律")


def cmd_novel_style(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    add_base = []
    for x in (args.add_baseline or []):
        p = Path(x)
        add_base.append(p.read_text(encoding="utf-8").strip() if p.is_file() else x)
    split = lambda v: [w.strip() for w in (v or "").split(",") if w.strip()]  # noqa: E731
    st = N.style_update(s, pov=args.pov, tense=args.tense, voice=args.voice,
                        diction=args.diction, add_baseline=add_base,
                        rm_baseline=args.rm_baseline,
                        add_avoid=split(args.add_avoid),
                        rm_avoid=split(args.rm_avoid))
    _step(f"文风契约已更新: {s.pid}")
    _info(" · ".join(f"{k}={st[k]}" for k in ("pov", "tense", "voice", "diction")
                     if st.get(k)) or "（四项均未填）")
    _info(f"基线样本 {len(st.get('baseline') or [])} 段 · "
          f"忌讳词 {len(st.get('avoid') or [])} 个"
          + (f"（{'、'.join(st['avoid'][:12])}）" if st.get("avoid") else ""))
    if not st.get("baseline_metrics"):
        _info(f"下一步: `novel baseline {s.pid} --from 1 --to 10` 立数值基线，"
              "文风门才有 z 分可对（只有文字样本时它靠肉眼比对）")


def cmd_novel_bible(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    if args.file:
        f = Path(args.file)
        if not f.is_file():
            raise ProjectError(f"找不到宪法文件: {f}")
        text = f.read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        wb = (s.data.get("adaptation") or {}).get("world_bible") or ""
        secs = N.bible_sections(wb)
        print(f"世界观宪法 · {s.pid} · {len(wb)} 字 / {len(secs)} 节")
        for x in secs:
            print(f"  {x['title']}（{x['chars']} 字）")
        if not wb:
            print(f"  （空——`novel bible {s.pid} --file 宪法.md` 写入）")
        return
    r = N.bible_set(s, text, section=args.section, append=args.append)
    _step(f"世界观宪法已写入（{r['mode']}）: {s.pid} · {r['chars']} 字 / "
          f"{r['sections']} 节")
    _info("⚠ 改宪法＝可能推翻既有设定：跑一次 "
          f"`novel sweep {s.pid} --term \"被废掉的词\"` 逐层扫干净再收工")


def cmd_novel_export(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    from . import novel as N
    r = N.export(s, frm=args.frm, to=args.to, strip=args.strip_markup,
                 out=args.out)
    _step(f"正文导出: 第 {r['range'][0]}~{r['range'][1]} 章 · {r['chapters']} 章 · "
          f"{r['chars']} 字")
    _info(r["file"] + ("（已剥 markdown 记号）" if args.strip_markup else ""))
    if r["missing"]:
        _info(f"⚠ 登记了但文件不在，已跳过: 第 {', '.join(map(str, r['missing']))} 章")
    _info(f"要做成视频接 `adapt import <新项目> --file {r['file']}`（见 kinema-project）")


# ---------- 参考片读片（study）：立项前门 ----------
# 只量**可测量的节奏骨架**（切点密度/每镜时长/静音占比/等间隔关键帧），
# 「像不像」「该用哪种 motion」一律不判——判定属指挥层（引擎内无 LLM 铁律），
# 规则写在 kinema-project SKILL「参考片立项模式」。
# 版权护栏：契约只存工作区相对路径（否则 `oss sync` 把第三方片传上公网桶）、
# 产物目录不带 `_work` 后缀（否则被 Studio 片库当成自家成片收录）。

def cmd_study_import(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    # 切点探测要整片解码一遍（长片可能跑几十秒），先打一行免得看着像卡死
    _info(f"读片中（切点探测需整片解码一遍，长片请稍候）: {Path(args.file).name}")
    e = s.ingest_study(Path(args.file), title=args.title or "", slug=args.slug,
                       cuts=args.cuts, frames=args.frames, subs=args.subs)
    m, r = e["media"], e["rhythm"]
    _step(f"参考片读片: {s.pid} / {e['slug']}")
    _info(f"时长 {m['dur']}s · {m['width']}×{m['height']} · {m['fps']}fps · "
          f"{'有' if m['has_audio'] else '无'}音轨")
    _info(f"节奏: {r['n_cuts']} 刀 / {r['n_shots']} 镜 · 密度 {r['cuts_per_min']} 刀每分 · "
          f"均镜 {r['avg_shot_sec']}s（{r['min_shot_sec']}~{r['max_shot_sec']}s）· "
          f"静音占比 {r['silence_ratio'] if r['silence_ratio'] is not None else '—'}")
    _info(f"关键帧 {e['n_frames']} 张: {s.dir / e['frames_dir']}")
    _info(f"切点全表: {s.dir / e['digest']}" + (f" · 字幕: {e['subs']}" if e["subs"] else ""))
    _info("下一步（AI 指挥层）：读 digest.json + 关键帧 → 出「保留什么 / 必须改什么」两栏 → "
          "定镜数与 motion。见 kinema-project SKILL「参考片立项模式」。")
    _info("⚠ 参考片是第三方素材：只作节奏参照，绝不进交付目录、绝不上云；"
          f"读完即 `study rm {s.pid} --slug {e['slug']}` 清掉本地副本。")


def cmd_study_show(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    entries = s.study
    print(f"读片 · {s.pid}  「{s.data.get('title', '')}」")
    if not entries:
        print("  —（尚未 `study import`）")
        return
    for e in entries:
        m, r = e.get("media") or {}, e.get("rhythm") or {}
        print(f"  [{e.get('slug')}] {e.get('title', '')}  {e.get('file')}")
        print(f"      {m.get('dur')}s · {m.get('width')}×{m.get('height')} · "
              f"{m.get('fps')}fps · 音轨 {'有' if m.get('has_audio') else '无'}")
        print(f"      {r.get('n_cuts')} 刀 / {r.get('n_shots')} 镜 · "
              f"密度 {r.get('cuts_per_min')} 刀每分 · 均镜 {r.get('avg_shot_sec')}s "
              f"({r.get('min_shot_sec')}~{r.get('max_shot_sec')}s) · "
              f"静音 {r.get('silence_ratio') if r.get('silence_ratio') is not None else '—'}")
        print(f"      全表 {e.get('digest')} · 关键帧 {e.get('n_frames')} 张 "
              f"{e.get('frames_dir')}" + (f" · 字幕 {e['subs']}" if e.get("subs") else ""))


def cmd_study_rm(args):
    ws = Workspace.open(args.workspace, create=False)
    s = ws.get_project(args.project)
    e = s.remove_study(args.slug)
    _step(f"读片记录已删除: {s.pid} / {e['slug']}")
    _info("参考片本体 + digest + 关键帧已一并清除（版权卫生·读完即删）")


# ---------- argparse ----------
def build_parser():
    p = argparse.ArgumentParser(prog="kinema", description="kinema 执行引擎（主题→成片）")
    p.add_argument("--version", action="version", version=f"kinema {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_concurrency(x):
        """并发度开关（生图/配音这类常规并行阶段用，缺省 4）。

        gen-video **不挂本开关**：视频按秒计费且单价高（各别名 price_per_second），并发是
        显式 opt-in——它有自己的 --concurrency 注册（缺省恒串行、不吃环境变量、
        4K 强制串行），见 gen-video 的 argparse 块。
        """
        x.add_argument("--concurrency", type=int, default=None, metavar="N",
                       help=f"并发生成张数（缺省 {parallel.DEFAULT_WORKERS}，上限 "
                            f"{parallel.MAX_WORKERS}；也可用环境变量 "
                            f"{parallel._ENV_WORKERS} 固化。`--concurrency 1` 退回串行）")

    def add_common(sp, profile=True):
        sp.add_argument("--project", "-p", help="视频 project.json 路径")
        sp.add_argument("--chapter", help="改用工作区章节：项目id/章节id（等价其视频文件）")
        sp.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")
        sp.add_argument("--config", help="models.yaml 路径（默认自动发现）")
        if profile:
            sp.add_argument("--profile", default=None, help="覆盖风格档（见 config/models.yaml）")
            sp.add_argument("--mock", action="store_true", help="用 mock 离线跑")
        sp.add_argument("--force", action="store_true", help="忽略 checkpoint 强制重生")

    def add_aspect(sp):
        sp.add_argument("--effects", default=None,
                        help="运行时特效覆盖（逗号分隔，如 rain,vignette）——只管这一次运行，"
                             "不写入章节（常开请在章节 json 写顶层 effects）")
        sp.add_argument("--no-effects", dest="no_effects", action="store_true",
                        help="本次合成关闭全部特效（章节 effects 置空）")
        sp.add_argument("--aspect", default=None, help="主比例 9:16|16:9|1:1（缺省跟随项目主比例）")
        sp.add_argument("--aspects", default=None, help="要输出的比例列表，逗号分隔，如 9:16,16:9")
        sp.add_argument("--both", action="store_true", help="同时输出竖屏+横屏 (9:16 和 16:9)")
        sp.add_argument("--image-per-aspect", dest="image_per_aspect", action="store_true",
                        help="每个比例单独出图（画质最佳、成本翻倍）；否则出一套图重构取景")
        sp.add_argument("--motion", "-m", dest="motion",
                        choices=["a", "b", "c", "kenburns", "dubbed", "native"],
                        help="渲染模式简写：a=kenburns b=native c=dubbed（或直接写全名）")
        sp.add_argument("--dubbed", action="store_true",
                        help="模式 dubbed：Seedance 图生视频，闭唇出片、配音上主轨（全旁白解说章的制式）")
        sp.add_argument("--native", action="store_true",
                        help="模式 native：Seedance 原生音画，模型自声 + 音色锚定（对白上镜章的制式；旁白镜混烧见 native_voiceover）")
        sp.add_argument("--kenburns", action="store_true",
                        help="模式 kenburns：静图 Ken Burns 运镜（零视频成本；不作缺省，须显式选）")
        sp.add_argument("--chain", action="store_true",
                        help="本次开启章级首尾帧衔接（缺省关闭——缺省档是逐镜全能参考、"
                             "一镜一片）：每镜以下一镜的图作末帧，遇转场镜断链；"
                             "只焊某两镜用镜级 shots[].frame_chain: true")
        sp.add_argument("--no-chain", dest="no_chain", action="store_true",
                        help="本次关闭章级首尾帧衔接（压过章节 frame_chain: true 与 --chain）："
                             "全部镜按缺省全能参考独立生成")
        sp.add_argument("--burn-voice", dest="burn_voice", action="store_true",
                        help="native 才有意义：把我们的固定音色配音**烧进**成片"
                             "（TTS 旁白上主轨；模型原生音轨只在旁白镜窗口压为背景床，"
                             "对白镜窗口原电平直通——那里它就是主人声）。缺省不烧——"
                             "native 片段自带原生人声，叠上去=同一句话两个人说；"
                             "要常开就在章节写 native_voiceover: true")

    for name, fn, prof in [
        ("gen-image", stage_gen_image, True), ("gen-video", stage_gen_video, True),
        ("tts", stage_tts, True), ("subtitle", stage_subtitle, True),
        ("music", stage_music, True), ("score", stage_score, True),
        ("lipsync", stage_lipsync, True),
    ]:
        sp = sub.add_parser(name)
        add_common(sp, profile=prof)
        if name in ("gen-image", "gen-video"):
            add_aspect(sp)
            sp.add_argument("--preview-json", dest="preview_json", action="store_true",
                            help="逐镜实发提示词的结构化 JSON（stdout 末行）——与真发"
                                 "同一条编译路径但零落盘零清单打印；Studio「实发提示词」"
                                 "面板经此出口取数，人工审阅仍用 gen-video --dry-run")
        if name == "gen-video":
            sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                            help="只列出发给 Seedance 的完整提示词与成本预估，不调用 API、不计费（审阅用）")
            sp.add_argument("--approved-only", dest="approved_only", action="store_true",
                            help="只渲染分镜图已通过(done)的镜——草稿两段式的正式档")
            sp.add_argument("--ignore-refs", dest="ignore_refs", action="store_true",
                            help="越过就绪度节点：设定图不齐也硬跑（默认逐镜拦截省钱）")
            sp.add_argument("--resolution", choices=["480p", "720p", "1080p", "4k"],
                            default=None,
                            help="本次生成的分辨率档，临时覆盖该 provider 别名在 models.yaml 里声明的档"
                                 "（缺省主力 seedance-mini 声明的是 720p，它没有 1080p/4K 档；"
                                 "1080p 归 seedance-2.5）；4k 为高成本档，须 --yes 二次确认")
            sp.add_argument("--yes", action="store_true",
                            help="确认 4K 高成本档（总价≈2×1080p·并发独享 1）后正式生成")
            sp.add_argument("--confirm-spend", dest="confirm_spend", action="store_true",
                            help="确认单笔超阈（最贵一次调用超过项目 budget_per_call）后正式生成；"
                                 "与 --yes 分工不同（--yes 只授权 4K 档）")
            sp.add_argument("--previz", action="store_true",
                            help="启用参考视频 V2V：把该镜 previz 预演片作 reference_video 发给 "
                                 "Seedance 迁移运镜/走位/节奏（仅 native·需媒体上云；"
                                 "**会多计输入视频秒**，故默认关，也可在项目顶层写 previz_v2v: true）")
            sp.add_argument("--control", dest="control", action="store_true",
                            help="深度控制视频：把各镜绑定的控制视频作 Seedance 参考视频发出，"
                                 "按实拍源片的人物运动演出本项目的角色（仅 native·"
                                 "**会多计输入视频秒**，故默认关）。**`run` 一条龙不吃本 flag**——"
                                 "那条路只认章节顶层的 `control_video: true`")
            sp.add_argument("--tail-relay", dest="tail_relay", action="store_true",
                            help="尾帧接力：每镜请求尾帧回传，下一镜把上一镜真实末帧作参考图"
                                 "承接开场构图与光线（native/dubbed·仅 seedance/mock·强制串行；"
                                 "板/设定图/时间轴照发。持久开启在章节写 tail_relay: true）")
            sp.add_argument("--anchor-frame", dest="anchor_frame", action="store_true",
                            help="首帧锚定：分镜图以 first_frame 硬锁为片段第 0 帧、不发末帧，"
                                 "镜间仍硬切（仅 native）。缺省的全能参考只把分镜图当参考图，"
                                 "开头几帧由模型自行调和，审过的那一帧并不是成片首帧。"
                                 "代价：设定图/简笔板/尾帧接力三条通道让位（官方禁混）。"
                                 "持久开启在章节写 anchor_frame: true，单镜用 "
                                 "shots[].anchor_frame: true")
            sp.add_argument("--no-lipsync", dest="no_lipsync", action="store_true",
                            help="本次不做口型精修（dubbed 缺省会在底片出齐后按最终配音"
                                 "重绘对白镜口型；旁白/静音镜恒不修）。未配置 req_key/"
                                 "视觉密钥时本就自动跳过，无需此开关")
            sp.add_argument("--no-auto-cast", dest="no_auto_cast", action="store_true",
                            help="本次跳过选角闸：未选角的说话人不附锚定音，嗓音交给模型")
            sp.add_argument("--video-provider", dest="video_provider", default=None,
                            help="本次点名视频 provider 别名（如 seedance-2.5=Seedance 2.5 大模型；"
                                 "缺省恒走 mini 主力）。持久点名写章节顶层 video_provider"
                                 "（chapter set --video-provider）；"
                                 "点名优先级：本 flag > 章节字段 > profile 链")
            sp.add_argument("--only", help="只生成指定镜号（逗号分隔，如 5 或 1,3）——单镜重roll/"
                                           "断点补渲用；dry-run 报价与事前闸同按过滤后清单")
            # 专用注册（不挂共用开关）：视频并发是显式 opt-in——缺省恒串行、
            # 不吃 KINEMA_CONCURRENCY 环境变量，预算闸事前对账 + 失败即停派 +
            # parallel 层重试恒关兜底；4K 档并发配额 1，点了也压回串行
            sp.add_argument("--concurrency", type=int, default=None, metavar="N",
                            help="并发生成镜数（**缺省 1 恒串行**——视频按秒计费单价高，"
                                 "一镜跑完确认再下一镜；显式给 N 才并发，上限 "
                                 f"{parallel.MAX_WORKERS}；4K 档强制串行）")
        if name == "tts":
            add_concurrency(sp)      # 配音也是逐句 API，可并行（gen-video 走上面的专用注册）
            sp.add_argument("--only", help="只合成指定镜号（逗号分隔，如 5 或 1,3）——native "
                                           "混烧常用：只给点名的镜配固定音色；旁白轨仍按全片拼"
                                           "（kenburns/dubbed 下其余台词镜的 wav 须已在盘，缺则点名拒拼）")
            sp.add_argument("--fit-dur", dest="fit_dur", action="store_true",
                            help="让画面等台词：配音超出画面窗口的镜，dur 自动放宽到配音实测"
                                 "（仅 native/dubbed 有意义；**已有 clip 的镜不动**，"
                                 "画面已生成钱已付；只放宽不收窄）")
        if name == "score":
            # 音频剧本（audio_mode: scored）：整片音轨按转场切段一次买断，
            # 逐段幂等——已在盘的段不重付费，`--only` 定向重生某一段
            sp.add_argument("--draft", action="store_true",
                            help="按分镜起草剧本（零成本·不调 API）：逐句写进「谁·段内秒段·"
                                 "台词原文」，台词逐字取 narration。声线/配乐/音效/语气要"
                                 "交给 AI 在底稿上改写（网页「⧉ 音频剧本指令」）；"
                                 "已写的段不覆盖，要推平加 --force")
            sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                            help="只列分段表（几段/每段几秒/接缝落在哪一镜）与报价，"
                                 "不调用 API、不计费——这条路按秒计费，烧钱前必看")
            # 段版本栈：生成式模型每次演绎都不同，同一段连出几版挑一版是正常用法
            sp.add_argument("--switch", type=int, metavar="段号",
                            help="把某段切到历史版（零成本·纯本地互换 + 重拼整轨），"
                                 "与 --to-v 同用；来回切不丢任何一版")
            sp.add_argument("--to-v", dest="to_v", type=int, metavar="N",
                            help="切到第 N 版（配合 --switch；版本号见 --dry-run 的分段表）")
            sp.add_argument("--only", help="只重生指定**段号**（逗号分隔，如 2 或 1,3）——"
                                           "注意是段号不是镜号；改了某一段剧本时用它，"
                                           "其余段沿用盘上音轨不重付费")
            add_concurrency(sp)
        if name == "lipsync":
            sp.add_argument("--only", help="只精修指定镜号（逗号分隔）——换音色后的标准工作流："
                                           "tts --force → lipsync → assemble（底片零重生）")
        if name == "gen-image":
            sp.add_argument("--only", help="只生成指定镜号（逗号分隔，如 1 或 1,2）——先出首镜确认再续跑")
            sp.add_argument("--accept-existing", dest="accept_existing", action="store_true",
                            help="仅验收已有 agent 图片并重新编译提示词/血缘，不调用模型、不归档图片")
            sp.add_argument("--candidates", type=int, metavar="N",
                            help="宫格候选：每镜出 N 张待选（2~9），人点选后才定稿上画布")
            sp.add_argument("--no-moodboard", dest="no_moodboard", action="store_true",
                            help="本次生成不套用项目参考库垫图（默认全局套用；「不要垫图参考」时用）")
            sp.add_argument("--hd", action="store_true",
                            help="本次按 provider 的像素上限出图（宽高比不变，尺寸更大）——"
                                 "Ken Burns 推近与封面吃这个余量；视频档位不受影响，"
                                 "且会跨到高像素价档")
            add_concurrency(sp)
        sp.set_defaults(func=_stage_wrapper(fn))

    sp = sub.add_parser("compose", help="合成成片")
    add_common(sp, profile=True)
    add_aspect(sp)
    sp.add_argument("--out", default=None, help="输出成片路径（仅单比例时生效）")
    sp.set_defaults(func=_stage_wrapper(stage_compose))

    sp = sub.add_parser("assemble", help="合成节点：字幕→背景乐→合成成片（本地确定性渲染）")
    add_common(sp, profile=True)
    add_aspect(sp)
    sp.add_argument("--out", default=None, help="输出成片路径（仅单比例时生效）")
    sp.add_argument("--draft", action="store_true",
                    help="逃生舱：跳过审阅闸，未过审也出草稿成片（正式交付前请过审）")
    sp.add_argument("--yes", action="store_true",
                    help="授权本次按秒买断音频剧本整轨（scored 章且整轨从未生成时才需要）")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--bgm", dest="bgm", action="store_const", const=True,
                   help="要曲库 BGM：native 章节在原生音之下加铺一层（落盘 native_bgm）；"
                        "scored 章节在剧本轨之上加铺（落盘 scored_bgm）。"
                        "预先作答即合成前不再发问")
    g.add_argument("--no-bgm", dest="bgm", action="store_const", const=False,
                   help="不要曲库 BGM（同上落盘）。kenburns/dubbed 不可关——"
                        "那两种模式除旁白外没有别的声音")
    sp.set_defaults(func=cmd_assemble, bgm=None)

    sp = sub.add_parser("run", help="一条龙：gen-image→tts→gen-video(动镜档)→subtitle→music→compose→cover")
    sp.add_argument("--project", "-p")
    sp.add_argument("--chapter", help="工作区章节：项目id/章节id")
    sp.add_argument("--workspace")
    sp.add_argument("--config", default=None)
    sp.add_argument("--profile", default=None, help="覆盖风格档")
    sp.add_argument("--out", default=None)
    sp.add_argument("--mock", action="store_true", help="全环节 mock 离线跑")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-approve", action="store_true", help="全自动跑完后不自动过审，保留待审状态")
    add_concurrency(sp)   # 只影响一条龙里的生图/配音段；gen-video 段恒串行
                          # （视频并发要显式单独跑 gen-video --concurrency N）
    add_aspect(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("studio", help="启动可视化系统（项目仪表盘 + 成片画廊）；单例·已在跑则复用")
    sp.add_argument("--root", default=".", help="片库扫描根目录（默认仓库根，即 project/ 的父目录）")
    sp.add_argument("--workspace", default=None,
                    help="工作区数据目录（默认仓库根 project/；传仓库根或 engine/ 会自动归一）")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--config", default=None)
    sp.add_argument("--restart", action="store_true", help="停掉本工作区已在跑的实例再重启（换端口也用它）")
    sp.add_argument("--stop", action="store_true", help="停止本工作区的 Studio 实例，不启动")
    sp.add_argument("--status", action="store_true", help="查看本工作区 Studio 是否在跑 + 残留进程检测")
    sp.set_defaults(func=cmd_studio)

    sp = sub.add_parser("doctor", help="环境/配置自检")
    sp.add_argument("--config", default=None)
    sp.add_argument("--workspace", default=None)
    sp.set_defaults(func=cmd_doctor)

    ap = sub.add_parser("agent", help="Agent/Skill 控制平面：目录、路由、契约、上下文与计划式写入") \
        .add_subparsers(dest="agent_action", required=True)
    x = ap.add_parser("catalog", help="查看机器可读 Skill catalog")
    # 枚举真源在编译器侧（agent_assets.KINDS/STATUSES）——这里再抄一份的话，
    # 新增 kind 后 CLI 会拒掉合法过滤值
    from .agent_assets import KINDS as _skill_kinds, STATUSES as _skill_statuses
    x.add_argument("--kind", choices=sorted(_skill_kinds))
    x.add_argument("--status", choices=sorted(_skill_statuses))
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_catalog)
    x = ap.add_parser("route", help="确定性 Skill 路由：项目绑定 > 显式 Skill > 显式 profile > kinema")
    x.add_argument("--project", help="项目 id；读取其 project.skill")
    x.add_argument("--project-skill", help="直接传入已绑定的 project.skill（机器调用）")
    x.add_argument("--skill", help="显式 Skill id")
    x.add_argument("--profile", help="显式 profile id")
    x.add_argument("--workspace", default=None)
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_route)
    x = ap.add_parser("doctor", help="检查 catalog、生成漂移、宿主发现路径与上下文预算")
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_doctor)
    x = ap.add_parser("assets", help="编译或只读检查 Agent/Skill 生成物")
    mode = x.add_mutually_exclusive_group()
    mode.add_argument("--compile", dest="asset_action", action="store_const", const="compile",
                      help="从 agent/ 真源重新编译全部生成物")
    mode.add_argument("--check", dest="asset_action", action="store_const", const="check",
                      help="只读检查源码、生成物与上下文预算")
    x.set_defaults(asset_action="check")
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_assets)
    x = ap.add_parser("contract", help="查看 PromptSpec 或 ChapterPlan 正式机器契约")
    x.add_argument("contract_name", choices=["prompt", "chapter-plan"])
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_contract)
    x = ap.add_parser("context", help="按任务读取章节最小上下文、binding 与 revision")
    x.add_argument("--chapter", required=True, help="项目id/章节id")
    x.add_argument("--task", required=True, choices=["storyboard", "image", "video", "review"])
    x.add_argument("--workspace", default=None)
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_context)
    x = ap.add_parser("plan", help="校验或应用 ChapterPlan 语义写入")
    xp = x.add_subparsers(dest="plan_action", required=True)
    for action, help_text in (("validate", "纯计算校验 ChapterPlan，不写盘"),
                              ("apply", "revision CAS 后原子应用 ChapterPlan")):
        y = xp.add_parser(action, help=help_text)
        y.add_argument("--file", required=True, help="ChapterPlan JSON 文件")
        y.add_argument("--workspace", default=None)
        y.add_argument("--json", action="store_true")
        y.set_defaults(func=cmd_agent_plan)
    x = ap.add_parser("explain", help="解释最近 PromptEnvelope 与当前作者字段是否漂移")
    x.add_argument("--chapter", required=True, help="项目id/章节id")
    x.add_argument("--shot", required=True, type=int)
    x.add_argument("--stage", required=True, choices=["image", "video"])
    x.add_argument("--workspace", default=None)
    x.add_argument("--config", default=None)
    x.add_argument("--json", action="store_true")
    x.set_defaults(func=cmd_agent_explain)

    # ---- 模型配置覆盖层（网页配置中心的命令行对等面，同一条写路径）----
    sp = sub.add_parser("config", help="模型配置：连接段/激活项/密钥（覆盖 config/models.yaml；"
                                       "未配置的一律回落配置文件）")
    cfsub = sp.add_subparsers(dest="cfaction", required=True)

    def add_cf(x):
        x.add_argument("--workspace", default=None,
                       help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")
        x.add_argument("--config", default=None, help="models.yaml 路径（默认自动发现）")

    x = cfsub.add_parser("show", help="看生效配置：激活项 / 连接段 / 密钥三态 / 本机覆盖了哪些字段")
    add_cf(x)
    x.add_argument("--json", action="store_true", help="机器可读输出")
    x.add_argument("--pull", action="store_true",
                   help="先从数据库回流一次（换机继承：库行比本地新才覆盖）")
    x.set_defaults(func=cmd_config_show)

    x = cfsub.add_parser("set", help="改某个 provider 别名的连接字段（不写的字段保持跟随配置文件）")
    add_cf(x)
    x.add_argument("--provider", required=True, help="providers 段的别名，如 seedream")
    x.add_argument("--set", action="append", default=None, metavar="字段=值",
                   help="可重复，如 --set base_url=https://... --set model=xxx")
    x.add_argument("--reset", action="append", default=None, metavar="字段",
                   help="清除某字段的本机覆盖，回落配置文件（可重复）")
    x.add_argument("--reset-all", action="store_true", help="清除该别名的全部本机覆盖")
    x.set_defaults(func=cmd_config_set)

    x = cfsub.add_parser("activate", help="激活某能力用哪个 provider（写 defaults.providers）")
    add_cf(x)
    x.add_argument("--capability", required=True, choices=["image", "video", "tts", "music"])
    x.add_argument("--provider", required=True,
                   help="providers 段的别名；填 - 表示恢复跟随 config/models.yaml")
    x.set_defaults(func=cmd_config_activate)

    x = cfsub.add_parser("secret", help="写一个密钥到本机密钥文件（值从终端读入不回显、不入库、不提交）")
    add_cf(x)
    x.add_argument("--env", required=True, help="环境变量名，如 MINIMAX_API_KEY")
    x.add_argument("--clear", action="store_true", help="清除该密钥")
    x.set_defaults(func=cmd_config_secret)

    x = cfsub.add_parser("test", help="连通性自检（零成本：只查解析层，一个生成请求都不发）")
    add_cf(x)
    x.add_argument("--provider", default=None, help="只测一个别名（缺省全测）")
    x.add_argument("--verbose", "-v", action="store_true", help="通过项也逐条打印")
    x.set_defaults(func=cmd_config_test)

    op = sub.add_parser("oss", help="媒体上云（阿里云OSS/腾讯云COS/火山TOS，本地为渲染副本）") \
            .add_subparsers(dest="ossaction", required=True)
    x = op.add_parser("status", help="媒体后端与连通性自检")
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_oss_status)
    x = op.add_parser("sync", help="确认后上传媒体并把 JSON/数据库路径改写为 OSS 地址")
    x.add_argument("--project", help="只同步该项目"); x.add_argument("--chapter", help="只同步该章节（项目id/章节id）")
    x.add_argument("--yes", action="store_true", help="跳过确认")
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_oss_sync)
    x = op.add_parser("pull", help="按 OSS 地址把缺失媒体拉回本地（换机恢复）")
    x.add_argument("--project"); x.add_argument("--chapter")
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_oss_pull)

    vc = sub.add_parser("voice", help="音色选角：试音 → 立档 → 启用（档案可回听·可换回·带引用闸）") \
            .add_subparsers(dest="vcaction", required=True)
    x = vc.add_parser("audition", help="模版试音：同段台词、若干把官方音色（默认 5 条）")
    x.add_argument("project"); x.add_argument("--name", help="角色名（缺省=全部角色）")
    x.add_argument("--narrator", action="store_true", help="给旁白试音（与 --name 互斥）")
    x.add_argument("--candidates", help="候选音色别名，逗号分隔（缺省按性别/旁白池自动补足）")
    x.add_argument("--text", help="自定义试音台词")
    x.add_argument("--mock", action="store_true"); x.add_argument("--config", default=None)
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_voice_audition)
    x = vc.add_parser("custom", help="定制生成：按声线描述出 N 条演绎（seed-audio-1.0）")
    x.add_argument("project"); x.add_argument("--name", help="角色名")
    x.add_argument("--narrator", action="store_true", help="给旁白定制")
    x.add_argument("--prompt", required=True,
                   help="声线描述（六槽位 40~80 字：性别年龄段/音区明暗/音质质感/语速节奏/"
                        "口音吐字/气质，不写情绪词），如「五十岁男性，低音区偏暗，嗓音略带沙哑、"
                        "胸腔共鸣强，语速偏慢、句尾下沉，标准普通话，气质沉稳」")
    x.add_argument("--count", type=int, default=None,
                   help="定制条数（缺省 3；给了 --adopt N 而未显式指定时只生成 N 条）")
    x.add_argument("--adopt", type=int, default=None, metavar="N",
                   help="生成后把第 N 条立档启用（缺省路径：写描述即定制，未经试听）")
    x.add_argument("--text", help="试听台词（缺省用内置的角色/旁白试音词）")
    x.add_argument("--mock", action="store_true"); x.add_argument("--config", default=None)
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_voice_custom)
    x = vc.add_parser("use", help="启用一把声音：候选按编号立档，或 --cast 换回历史档案")
    x.add_argument("project"); x.add_argument("--name", help="角色名")
    x.add_argument("--narrator", action="store_true", help="旁白")
    x.add_argument("--no", type=int, default=0, help="候选编号（模版试音 / --custom 定制那批）")
    x.add_argument("--custom", action="store_true", help="编号取自 voice custom 出的那批")
    x.add_argument("--cast", help="音色档案号（voice bank 里的 vc_NNNN），换回历史音色")
    x.add_argument("--mock", action="store_true"); x.add_argument("--config", default=None)
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_voice_use)
    x = vc.add_parser("bank", help="音色档案：哪条在用、每条被谁引用着、哪条可以删")
    x.add_argument("project"); x.add_argument("--name", help="角色名（缺省=全部实体）")
    x.add_argument("--narrator", action="store_true", help="只看旁白")
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_voice_bank)
    x = vc.add_parser("rm", help="删除一条音色档案（在用/已产出配音/仍被指派的一律拒绝）")
    x.add_argument("project"); x.add_argument("--cast", required=True, help="音色档案号 vc_NNNN")
    x.add_argument("--workspace", default=None); x.set_defaults(func=cmd_voice_rm)
    x = vc.add_parser("list", help="全员选角概览：在用音色与档案数")
    x.add_argument("project"); x.add_argument("--workspace", default=None)
    x.set_defaults(func=cmd_voice_list)

    sp = sub.add_parser("export-review", help="导出静态审阅包（免登录 HTML + 自包含媒体，发客户）")
    add_common(sp, profile=False)
    sp.add_argument("--out", default=None, help="输出目录（默认 project/<项目>/exports/<章节>_review/）")
    sp.set_defaults(func=cmd_export_review)

    rv = sub.add_parser("review", help="审阅状态机：待审/通过/重做/弃用（通过=锁定防烧钱）") \
            .add_subparsers(dest="rvaction", required=True)
    x = rv.add_parser("list", help="审阅看板：各镜各产物状态/版本/重做意见")
    add_common(x, profile=False)
    x.add_argument("--state", choices=list(review.STATES), help="只看该状态的镜")
    x.set_defaults(func=cmd_review_list)
    x = rv.add_parser("set", help="表态：done=通过锁定 · retake=打回重做 · omt=弃用整镜")
    add_common(x, profile=False)
    x.add_argument("--shots", help="镜号列表（如 1,3）或 all（--stage animatic 时不需要）")
    x.add_argument("--stage", choices=["image", "audio", "clip", "shot", "animatic"],
                   help="产物阶段（--state omt 弃用整镜时可省略；animatic=章节级全片样片）")
    x.add_argument("--state", required=True, choices=list(review.STATES))
    x.add_argument("--note", help="意见（retake 建议必填：后续将编译进该镜下一版提示词）")
    x.set_defaults(func=cmd_review_set)

    sp = sub.add_parser("pick", help="宫格候选点选：把候选 N 定为该镜分镜图（人眼定稿=通过锁定）")
    add_common(sp, profile=False)
    sp.add_argument("--shot", required=True, help="镜号")
    sp.add_argument("--use", required=True, type=int, help="候选编号（1 起）")
    sp.add_argument("--keep-open", dest="keep_open", action="store_true",
                    help="定稿后不锁定，落待审（默认点选即通过锁定）")
    sp.set_defaults(func=cmd_pick)

    sp = sub.add_parser("animatic",
                        help="草稿两段式：全片 Ken Burns 样片（零视频成本过节奏审）")
    add_common(sp, profile=True)
    add_aspect(sp)
    sp.set_defaults(func=cmd_animatic)

    sp = sub.add_parser("milestones", help="三级里程碑：首镜 → 首集 → 全片（进度与下一步）")
    sp.add_argument("project", help="项目id")
    sp.add_argument("--workspace", default=None)
    sp.set_defaults(func=cmd_milestones)

    lg = sub.add_parser("lineage", help="资产血缘：就绪度 / 设定图过期传播") \
            .add_subparsers(dest="lgaction", required=True)
    x = lg.add_parser("status", help="逐镜就绪度与过期引用一览")
    x.add_argument("project_id", nargs="?", help="项目id（检查全部章节）")
    x.add_argument("--chapter", help="只查该章节：项目id/章节id")
    x.add_argument("--project", "-p", help="或直接指定章节 json 文件")
    x.add_argument("--workspace", default=None)
    x.set_defaults(func=cmd_lineage_status)
    x = lg.add_parser("mark", help="把过期镜标「已过期需重生成」（未锁定置 retake）")
    x.add_argument("project_id", nargs="?", help="项目id（处理全部章节）")
    x.add_argument("--chapter", help="只处理该章节：项目id/章节id")
    x.add_argument("--project", "-p", help="或直接指定章节 json 文件")
    x.add_argument("--workspace", default=None)
    x.set_defaults(func=cmd_lineage_mark)

    at = sub.add_parser("assets", help="全局资产库：跨项目查看/复用角色·道具·场景设定") \
            .add_subparsers(dest="ataction", required=True)
    x = at.add_parser("list", help="所有项目的资产一览（IP 复用地图）")
    x.add_argument("--kind", choices=["character", "prop", "weapon", "scene"])
    x.add_argument("--workspace", default=None)
    x.set_defaults(func=cmd_assets_list)
    x = at.add_parser("import", help="把源项目的资产（含设定图/音色锁）引入目标项目")
    x.add_argument("project", help="目标项目id")
    x.add_argument("--from", dest="src", required=True, help="源项目id")
    x.add_argument("--name", required=True, help="资产名称（scene 可任填）")
    x.add_argument("--kind", default="character",
                   choices=["character", "prop", "weapon", "scene"])
    x.add_argument("--force", action="store_true", help="目标已有同名资产时覆盖")
    x.add_argument("--workspace", default=None)
    x.set_defaults(func=cmd_assets_import)

    vv = sub.add_parser("versions", help="产物版本栈：重生成不覆盖，可列出/回滚") \
            .add_subparsers(dest="vsaction", required=True)
    x = vv.add_parser("list", help="查看各镜产物版本谱系")
    add_common(x, profile=False)
    x.add_argument("--shots", help="镜号列表（缺省=全部）")
    x.set_defaults(func=cmd_versions_list)
    x = vv.add_parser("rollback", help="回滚某镜某产物到历史版本（当前版先归档）")
    add_common(x, profile=False)
    x.add_argument("--shot", required=True, help="镜号")
    x.add_argument("--stage", required=True, choices=list(versioning.STAGES))
    x.add_argument("--to", required=True, type=int, help="目标版本号（versions list 查看）")
    x.set_defaults(func=cmd_versions_rollback)

    dbp = sub.add_parser("db", help="持久化管理（config/storage.yaml：local/mysql）") \
             .add_subparsers(dest="dbaction", required=True)
    for name, fn, hlp in [
        ("status", cmd_db_status, "查看后端与连通性"),
        ("init", cmd_db_init, "连接 MySQL 并创建表结构"),
        ("sync", cmd_db_sync, "本地 JSON → 数据库（全量登记）"),
        ("pull", cmd_db_pull, "数据库 → 本地 JSON（换机恢复文档）"),
    ]:
        x = dbp.add_parser(name, help=hlp)
        x.add_argument("--workspace", default=None)
        x.set_defaults(func=fn)
    x = dbp.add_parser("schema", help="导出完整建库建表 SQL 脚本（交付/DBA 审阅用）")
    x.add_argument("--out", default=None, help="输出文件（缺省打印到终端）")
    x.set_defaults(func=cmd_db_schema)

    sp = sub.add_parser("init", help="生成 project.json 骨架")
    sp.add_argument("--project", "-p", required=True)
    sp.add_argument("--id", default=None)
    sp.add_argument("--theme", default=None)
    sp.add_argument("--platform", default=None, help="逗号分隔，如 douyin,youtube（缺省不绑定）")
    sp.add_argument("--duration", type=int, default=60)
    sp.add_argument("--aspect", default=None, help="9:16|16:9|1:1（缺省横屏 16:9）")
    sp.add_argument("--profile", default="narration")
    sp.add_argument("--framework", default="PAS")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    # ---- project / character / chapter（工作区 CRUD）----
    def ws_arg(x):
        x.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/）")

    tp = sub.add_parser("template", help="项目模板/平台规格预设") \
            .add_subparsers(dest="tpaction", required=True)
    x = tp.add_parser("list", help="全部模板一览"); x.set_defaults(func=cmd_template_list)
    x = tp.add_parser("show", help="查看模板详情"); x.add_argument("name")
    x.set_defaults(func=cmd_template_show)

    sc = sub.add_parser("spec", help="平台规格核对（交付验收线）") \
            .add_subparsers(dest="scaction", required=True)
    x = sc.add_parser("check", help="逐章时长/分镜/比例 + 系列体量是否达标")
    x.add_argument("project", help="项目id")
    x.add_argument("--workspace", default=None)
    x.set_defaults(func=cmd_spec_check)

    sp = sub.add_parser("lint", help="分镜单调度体检：运镜/情绪/景别/空词/占位旁白/高光"
                                     "（纯计算·不落盘·阈值由顶层 art_direction 旋钮驱动）")
    add_common(sp, profile=False)
    sp.add_argument("--strict", action="store_true",
                    help="有警告即以非零码退出（给脚本/流水线卡口用；默认只打印）")
    sp.set_defaults(func=cmd_lint)

    bt = sub.add_parser("batch", help="跨镜批量编辑：一句话全片生效，可撤销") \
            .add_subparsers(dest="btaction", required=True)
    x = bt.add_parser("edit", help="批量改字段（锁定镜默认保护；提示词类自动置重做）")
    add_common(x, profile=False)
    x.add_argument("--shots", help="镜号列表（如 1,3)或 all（缺省=all）")
    from .batch import EDITABLE_FIELDS as _BF
    x.add_argument("--field", required=True, choices=list(_BF),
                   help="要改的字段（提示词/文案/摄影字段）")
    x.add_argument("--set", help="整体替换为该值")
    x.add_argument("--append", help="在原值后追加")
    x.add_argument("--prepend", help="在原值前插入")
    x.add_argument("--replace", help='子串替换："旧=>新"，如 "白天=>夜晚"')
    x.add_argument("--note", help="重做意见（缺省自动生成，编译进下一版提示词）")
    x.add_argument("--no-retake", dest="no_retake", action="store_true",
                   help="只改文案不置重做（默认提示词类字段改完自动 retake）")
    x.add_argument("--include-locked", dest="include_locked", action="store_true",
                   help="连已通过锁定(done)的镜一起改（默认保护跳过）")
    x.set_defaults(func=cmd_batch_edit)
    x = bt.add_parser("undo", help="撤销批量操作（缺省=最近一次；还原字段与审阅状态）")
    add_common(x, profile=False)
    x.add_argument("--op", help="操作ID（batch log 查看；缺省=最近一次）")
    x.set_defaults(func=cmd_batch_undo)
    x = bt.add_parser("log", help="批量操作日志")
    add_common(x, profile=False)
    x.set_defaults(func=cmd_batch_log)

    sp = sub.add_parser("refine", help="框选局部改造：区域+指令 → 图像模型编辑重生（旧版归档/血缘传播）")
    add_common(sp)
    sp.add_argument("--shot", help="镜号（分镜图局部改造，配 --chapter）")
    sp.add_argument("--asset", help="设定图局部改造：character:名 / scene / prop:名 / "
                                    "expression:名 / pose:名 / topview[:名]（配 --id）")
    sp.add_argument("--id", dest="project_id", help="asset 模式的项目 id")
    sp.add_argument("--rect", help='框选区域 "x,y,w,h"（0~1 相对值，缺省=整图）')
    sp.add_argument("--note", required=True, help="改造指令（这块区域要改成什么）")
    sp.add_argument("--no-moodboard", dest="no_moodboard", action="store_true",
                    help="局部改造不套用参考库垫图（默认随改造喂入保风格）")
    sp.set_defaults(func=cmd_refine)

    sp = sub.add_parser("supply", help="素材直供：现成图片登记为分镜画面，跳过 AI 生图"
                        "（固定场景+资产图解说模式的核心；同走版本栈/审阅制度）")
    add_common(sp, profile=False)
    sp.add_argument("--shot", required=True, help="镜号")
    sp.add_argument("--file", required=True, help="本地图片路径（png/jpg/jpeg/webp）")
    sp.add_argument("--aspect", default=None,
                    help="逐比例直供（如 9:16 写入 images{aspect}；缺省写主图 image）")
    sp.add_argument("--skip-check", dest="skip_check", action="store_true",
                    help="跳过供料体检（体检只对「ffprobe 解不出」硬拦；"
                         "确认素材可用且不可再生时用此逃生舱）")
    sp.set_defaults(func=cmd_supply)

    sp = sub.add_parser("previz", help="3D 预演参考片：登记为分镜条件（首帧/末帧/参考视频/运镜四件套），"
                                       "Studio 的 3D 导演控制台即本命令的可视化前端")
    pzsub = sp.add_subparsers(dest="pzaction", required=True)

    def add_pz(x):
        x.add_argument("--project", "-p", help="视频 project.json 路径")
        x.add_argument("--chapter", help="改用工作区章节：项目id/章节id")
        x.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")
        x.add_argument("--config", default=None, help="models.yaml 路径（默认自动发现）")

    x = pzsub.add_parser("register", help="登记一段 previz mp4：抽首帧→分镜图(可选) · 抽末帧→last_frame_ref"
                                          " · 存参考片→previz · 写运镜→camera")
    add_pz(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.add_argument("--file", required=True, help="previz 视频路径（mp4/mov）")
    x.add_argument("--camera", default=None,
                   help="运镜 preset key（如 push_in / dolly_zoom；查全表 `previz presets`）"
                        "——会写进 shots[].camera 作为发给 Seedance 的运镜措辞")
    x.add_argument("--aspect", default=None, help="逐比例登记首帧（缺省写主图 image）")
    x.add_argument("--use-first-frame", dest="use_first_frame", action="store_true",
                   help="强制用 previz 首帧覆盖该镜分镜图（缺省只在该镜尚无图时才登记，"
                        "防灰模盖掉已生成的精修图）")
    x.add_argument("--no-first-frame", dest="no_first_frame", action="store_true",
                   help="只登记末帧与参考片，绝不碰 shots[].image")
    x.add_argument("--skip-check", dest="skip_check", action="store_true",
                   help="跳过 previz 体检（时长/体积/帧率/宽高比只告警，硬拦仅「解不出」）")
    x.set_defaults(func=cmd_previz_register)

    x = pzsub.add_parser("build", help="把控制台上传的 PNG 序列编成 previz mp4 并登记"
                                       "（Studio「渲染 previz」的后台任务入口）")
    add_pz(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.add_argument("--fps", type=int, default=24, help="previz 帧率（缺省 24，与 Seedance 一致）")
    x.add_argument("--camera", default=None, help="运镜 preset key（同 register）")
    x.add_argument("--aspect", default=None, help="逐比例登记首帧（缺省写主图）")
    x.add_argument("--use-first-frame", dest="use_first_frame", action="store_true",
                   help="强制用 previz 首帧覆盖该镜分镜图")
    x.add_argument("--no-first-frame", dest="no_first_frame", action="store_true",
                   help="只登记末帧与参考片，绝不碰 shots[].image")
    x.add_argument("--keep-frames", dest="keep_frames", action="store_true",
                   help="编码后保留帧序列（排查渲染问题时用；缺省编完即删）")
    x.add_argument("--skip-check", dest="skip_check", action="store_true",
                   help="跳过 previz 体检")
    x.set_defaults(func=cmd_previz_build)

    x = pzsub.add_parser("clear", help="摘除某镜的 previz 挂载（保留产物文件，不动分镜图）")
    add_pz(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.set_defaults(func=cmd_previz_clear)

    x = pzsub.add_parser("list", help="本章各镜的 previz 挂载一览")
    add_pz(x)
    x.set_defaults(func=cmd_previz_list)

    x = pzsub.add_parser("reel", help="全片预演：把各镜 previz 按分镜顺序拼成一条长片"
                                      "（可直接播放/下载；零 API 成本，不是成片）")
    add_pz(x)
    x.add_argument("--fps", type=int, default=None,
                   help="重编码归一时的目标帧率（缺省取首镜帧率；各镜规格一致时走"
                        "流拷贝，本参数不生效）")
    x.add_argument("--json", action="store_true", help="输出清单 JSON")
    x.set_defaults(func=cmd_previz_reel)

    x = pzsub.add_parser("presets", help="运镜库全表（36 个：3D 相机装备 + Seedance 措辞同源）")
    x.add_argument("--group", choices=list(camera_mod.GROUPS), default=None,
                   help="只看某一桶：basic 基础机位 / classic 经典技法 / master 大师签名")
    x.add_argument("--json", action="store_true", help="输出目录 JSON（供脚本/前端消费）")
    x.set_defaults(func=cmd_previz_presets)

    sp = sub.add_parser("control", help="深度捕捉：把实拍片处理成「人物深度+骨骼」控制视频，"
                                        "绑到镜上作参考视频迁移运动（运动来自源片、外观来自分镜图）")
    ctsub = sp.add_subparsers(dest="ctaction", required=True)

    def add_ct(x):
        x.add_argument("--project", "-p", help="视频 project.json 路径")
        x.add_argument("--chapter", help="改用工作区章节：项目id/章节id")
        x.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")
        x.add_argument("--config", default=None, help="models.yaml 路径（默认自动发现）")

    x = ctsub.add_parser("build", help="把一段实拍片处理成控制视频素材"
                                       "（本机 CPU，零 API 花费；每源秒约十几秒）")
    add_ct(x)
    x.add_argument("--source", required=True, help="源片路径（mp4/mov，≤30s）")
    x.add_argument("--asset", default=None, help="素材 id（缺省按文件名+内容指纹派生）")
    x.add_argument("--no-styled", dest="no_styled", action="store_true",
                   help="不出精细版（给人看的浮雕版），省约三成渲染时间")
    x.add_argument("--mock", action="store_true",
                   help="替身模型跑完整编排（离线彩排/测试用，产物是几何图形）")
    x.add_argument("--bind-shot", dest="bind_shot", default=None,
                   help="处理完成后自动绑到该镜（区间取 0 起、按该镜秒数）")
    x.set_defaults(func=cmd_control_build)

    x = ctsub.add_parser("bind", help="把素材的某一段绑到某镜（--start/--end 框定区间即段长，dur 随之对齐；不给 --end 时按该镜秒数裁段；贴合画布）")
    add_ct(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.add_argument("--asset", required=True, help="素材 id")
    x.add_argument("--start", type=float, default=0.0, help="在素材里的起点秒（缺省 0）")
    x.add_argument("--end", type=float, default=None,
                   help="在素材里的终点秒。给了则区间说了算：段长=终点-起点（4~15 整秒），"
                        "并把本镜 dur 对齐过去；不给则段长由该镜请求秒数定")
    x.add_argument("--fit", choices=list(control_mod.bind.FITS), default="pad",
                   help="贴合章节画布：pad=补黑边（缺省）/ crop=居中裁")
    x.add_argument("--replace-previz", dest="replace_previz", action="store_true",
                   help="该镜已有 3D 预演时先清除它（一镜只发一条参考视频）")
    x.set_defaults(func=cmd_control_bind)

    x = ctsub.add_parser("unbind", help="摘除某镜的控制视频绑定（保留段落文件）")
    add_ct(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.set_defaults(func=cmd_control_unbind)

    x = ctsub.add_parser("list", help="本章素材库与绑定一览（含素材重建/时长漂移标记）")
    add_ct(x)
    x.add_argument("--json", action="store_true", help="输出 JSON")
    x.set_defaults(func=cmd_control_list)

    x = ctsub.add_parser("compare", help="出某镜的对照片（源片段|控制段[|成片段]）")
    add_ct(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.set_defaults(func=cmd_control_compare)

    x = ctsub.add_parser("delete", help="删除素材目录（仍有镜绑着则拒）")
    add_ct(x)
    x.add_argument("--asset", required=True, help="素材 id")
    x.set_defaults(func=cmd_control_delete)

    x = ctsub.add_parser("fetch", help="下载深度与分割权重（约 115MB；引擎运行期绝不静默下载）")
    x.add_argument("--check", action="store_true", help="只报告缺哪些，不下载")
    x.set_defaults(func=cmd_control_fetch)

    sp = sub.add_parser("sketch", help="简笔分镜预演板：一镜一板（9 格铅笔素描+五色标注），"
                                       "beats 编译成分段时间轴喂 Seedance；与 3D previz 并行互斥")
    sksub = sp.add_subparsers(dest="skaction", required=True)

    def add_sk(x):
        x.add_argument("--project", "-p", help="视频 project.json 路径")
        x.add_argument("--chapter", help="改用工作区章节：项目id/章节id")
        x.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")
        x.add_argument("--config", default=None, help="models.yaml 路径（默认自动发现）")

    x = sksub.add_parser("gen", help="按 shots[].sketch.beats 逐镜生成简笔板"
                                     "（缺 beats 的镜跳过并点名；已有板幂等，--force 重生）")
    add_sk(x)
    x.add_argument("--only", default=None, help="只生成指定镜号（逗号分隔，如 1,3,5）")
    x.add_argument("--profile", default=None, help="画风 profile（只用于解析 image provider，"
                                                   "板是素描基调、不掺成片画风）")
    x.add_argument("--force", action="store_true", help="已有板也重生（直接覆盖）")
    x.add_argument("--note", default=None,
                   help="重生成意见（编译进本次板提示词的「修正重点」——灯箱「重新生成」走此通道）")
    x.add_argument("--mock", action="store_true", help="mock 离线彩排（零成本占位图）")
    x.add_argument("--concurrency", type=int, default=None,
                   help="并发数（缺省 4·上限 16·1=退回串行）")
    x.set_defaults(func=cmd_sketch_gen)

    x = sksub.add_parser("use", help="逐镜表态运动预演路径（previz/control/sketch 三选一互斥；"
                                     "auto=清除表态回到自动仲裁：previz > control > sketch）")
    add_sk(x)
    x.add_argument("--shot", type=int, default=None, help="镜号")
    x.add_argument("--all", action="store_true", help="全部正镜一起表态")
    x.add_argument("--guide", required=True, choices=[*sketch_mod.GUIDES, "auto"],
                   help="生效路径：previz=3D 预演 / control=深度控制视频 / sketch=简笔板 / "
                        "auto=清除表态")
    x.set_defaults(func=cmd_sketch_use)

    x = sksub.add_parser("ref", help="逐镜开关「板作参考」——只在章级衔接（frame_chain: true）"
                                     "的章里改变行为：开=该镜强制走参考任务（链上孤岛，两侧自动补"
                                     " 0.1s 无缝转场）；缺省档（章不衔接）板在盘本就自动附发，"
                                     "此开关与缺省行为重合")
    add_sk(x)
    x.add_argument("--shots", help="镜号列表（如 3,14）")
    x.add_argument("--all", action="store_true", help="全部正镜")
    x.add_argument("--state", required=True, choices=["on", "off"],
                   help="on=衔接章里强制该镜走参考任务（断本镜首尾帧）/ off=跟随缺省")
    x.set_defaults(func=cmd_sketch_ref)

    x = sksub.add_parser("clear", help="摘除某镜的简笔板挂载（板文件保留，beats 不动）")
    add_sk(x)
    x.add_argument("--shot", type=int, required=True, help="镜号")
    x.set_defaults(func=cmd_sketch_clear)

    x = sksub.add_parser("list", help="本章各镜 beats/板/guide/生效路径一览")
    add_sk(x)
    x.set_defaults(func=cmd_sketch_list)

    sp = sub.add_parser("ledger", help="成本台账：预估/实际双轨 + 废片/重roll 运营指标")
    sp.add_argument("project", help="项目id"); ws_arg(sp); sp.set_defaults(func=cmd_ledger)

    sp = sub.add_parser("watermark", help="成片水印：漂移水印（防搬运）+ 固定角标（品牌署名），水印版与原片并存")
    add_common(sp, profile=False)
    sp.add_argument("--text", help="漂移水印文案（缺省依次取 project.json watermark / branding.yaml watermark.text）")
    sp.add_argument("--corner-text", dest="corner_text",
                    help="固定角标水印文案（字幕式烧录·清晰不透明·比字幕小四号；缺省取 project.json watermark_fixed.text / branding）")
    sp.add_argument("--corner-pos", dest="corner_pos", choices=["tl", "tr", "bl", "br"],
                    help="固定角标位置：tl 左上 / tr 右上 / bl 左下 / br 右下（缺省 project/branding，再缺省 br）")
    sp.add_argument("--bottom-text", dest="bottom_text",
                    help="底部水印文案（底部居中·半透明·无描边无底衬，离底留呼吸距、"
                         "不与字幕底带重叠；缺省取 project.json watermark_bottom.text）")
    sp.add_argument("--from-project", dest="from_project", action="store_true",
                    help="只按 project.json 的 watermark/watermark_fixed/watermark_bottom 渲染、"
                         "不回落 branding（Studio 用，保证与 UI 状态一致）")
    sp.set_defaults(func=cmd_watermark)

    sp = sub.add_parser("verify", help="成片自审：容器/黑屏/该响却哑/时长/字幕/旁白轨/片段六类硬判 + 削波/响度/落点待修"
                        "（零成本纯本地 ffmpeg；结论写 project.json 顶层 verify）")
    # 刻意不走 add_common：verify 是**只读**体检，没有 --force 语义（加了会误导成"强制重生"）
    sp.add_argument("--project", "-p", help="视频 project.json 路径")
    sp.add_argument("--chapter", help="改用工作区章节：项目id/章节id（等价其视频文件）")
    sp.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")
    sp.add_argument("--config", help="models.yaml 路径（默认自动发现）")
    sp.add_argument("--profile", default=None, help="覆盖风格档（只影响字幕语言解析）")
    sp.add_argument("--aspect", default=None, help="只查某个比例（缺省全部已出比例）")
    sp.add_argument("--samples", type=int, default=mediacheck_mod.DEFAULT_SAMPLES,
                    help=f"黑屏抽样点数/比例（缺省 {mediacheck_mod.DEFAULT_SAMPLES}；已自动跳过转场黑场窗）")
    sp.add_argument("--json", action="store_true", help="输出原始报告 JSON（供脚本消费）")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("consistency",
                        help="角色跨镜一致性：引擎产料（代表帧×角色设定图配对）→ 指挥层多模态判定 →"
                             " CLI 回填（引擎不打分：ffmpeg 无人脸检测、像素度量对换姿态无判别力）")
    csub = sp.add_subparsers(dest="cnaction", required=True)
    # 刻意不走 add_common：scan 是零成本只读产料、set 是人工判定回填，两者都没有
    # --force/--mock 语义（加了会误导成"强制重生/离线打分"）
    def add_cn(x):
        x.add_argument("--project", "-p", help="视频 project.json 路径")
        x.add_argument("--chapter", help="改用工作区章节：项目id/章节id")
        x.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")

    x = csub.add_parser("scan", help="产料：每镜代表帧（kenburns=分镜图缩放 / dubbed·native=片段中点帧）"
                                     "+ manifest.json 配对清单，零 API 成本")
    add_cn(x)
    x.add_argument("--only", help="只产料指定镜号（逗号分隔，如 1 或 1,3）")
    x.add_argument("--aspect", default=None, help="按哪个比例取帧（缺省项目主比例）")
    x.add_argument("--stage", default=None, choices=list(consistency_mod.VISUAL_STAGES),
                   help="取哪一级渲染物作代表帧（缺省按模式：kenburns=image，dubbed/native=clip）；"
                        "dubbed/native 动态化之前判分镜图用 image，判定与打回随之记在 image")
    x.add_argument("--json", action="store_true", help="输出原始 manifest JSON（供脚本/指挥层消费）")
    x.set_defaults(func=cmd_consistency_scan)

    x = csub.add_parser("set", help="回填判定：ok 一致 / drift 漂移（判定由指挥层给出，引擎不打分）")
    add_cn(x)
    x.add_argument("--shot", required=True, help="镜号")
    x.add_argument("--verdict", required=True, choices=list(consistency_mod.VERDICTS),
                   help="ok=角色与设定图一致 · drift=已漂移")
    x.add_argument("--score", type=float, default=None,
                   help="主观一致性分 0~1（指挥层给，**不是机器算的**；可不填）")
    x.add_argument("--note", default=None, help="哪里不一致（会编译进重做意见→下一版提示词）")
    x.add_argument("--by", default=consistency_mod.DEFAULT_BY,
                   help=f"判定人（缺省 {consistency_mod.DEFAULT_BY}=指挥层）")
    x.add_argument("--retake", action="store_true",
                   help="判 drift 时顺手打回重做：未锁定镜置 retake；clip 漂移连 image 一起打回")
    x.set_defaults(func=cmd_consistency_set)

    sp = sub.add_parser("decision",
                        help="决策审计：把制作中的取舍逐条留痕到章节 decisions[]（append-only）。"
                             "必须走本命令——裸改 JSON 会被引擎长任务的旧内存副本或 mysql 库行覆写")
    dsub = sp.add_subparsers(dest="daction", required=True)
    # 同 consistency：刻意不走 add_common（决策记录既不重生产物也没有 mock 语义）
    def add_dc(x):
        x.add_argument("--project", "-p", help="视频 project.json 路径")
        x.add_argument("--chapter", help="改用工作区章节：项目id/章节id")
        x.add_argument("--workspace", help="工作区数据目录（默认仓库根 project/ 或 KINEMA_WORKSPACE）")

    x = dsub.add_parser("add", help="追加一条决策（只增不改不删：记错了再记一条覆盖性的）")
    add_dc(x)
    x.add_argument("--choice", required=True, help="最终决定（一句话，如「第3镜改用远景，突出孤独感」）")
    x.add_argument("--alt", action="append", default=[], metavar="备选",
                   help="被否掉的备选方案（可重复，最多 10 条）")
    x.add_argument("--why", default="", help="为什么这么选（后续据此不再反复推翻）")
    x.add_argument("--confidence", default=decisions_mod.DEFAULT_CONFIDENCE,
                   choices=list(decisions_mod.CONFIDENCE),
                   help=f"置信度（缺省 {decisions_mod.DEFAULT_CONFIDENCE}）：low 的决策后续可优先重议")
    x.set_defaults(func=cmd_decision_add)

    x = dsub.add_parser("list", help="列出全部决策记录")
    add_dc(x)
    x.add_argument("--json", action="store_true", help="输出原始 JSON（供脚本/指挥层消费）")
    x.set_defaults(func=cmd_decision_list)

    sp = sub.add_parser("transition", help="转场镜：两镜之间插入渐黑/白闪字卡（如「一天后」）或素材转场，相邻镜自动边缘淡化")
    tsub = sp.add_subparsers(dest="taction", required=True)
    for ta, thelp in (("add", "插入转场镜"), ("rm", "移除转场镜"), ("list", "列出全部转场镜"),
                      ("sync", "同步孤岛接缝的自动无缝转场（走全能参考/V2V 的镜两侧，"
                               "gen-video 也会自动跑一次）")):
        tp = tsub.add_parser(ta, help=thelp)
        add_common(tp, profile=False)
        if ta == "add":
            tp.add_argument("--after", required=True, help="插到哪个镜之后（镜 id）")
            tp.add_argument("--type", choices=sorted(transitions_mod.TRANSITIONS),
                            default=None,
                            help="转场类型（缺省智能选：无字=fade 极简黑场呼吸总~0.5s｜带 --text=fade_black 渐黑字卡总~1s）")
            tp.add_argument("--text", help="字卡文案（如「几天后」「三年后」；留空=fade 极简黑场，纯 Python 零成本）")
            tp.add_argument("--asset", help="素材转场：转场视频路径（用户明确要 AI 过场动画时先用 Seedance 生成存 assets/transitions/ 再引用）")
            tp.add_argument("--dur", type=float, help="停顿/过渡秒数（缺省随类型：seamless 0.1｜fade 0.1｜fade_black 0.5｜wipe 0.7；"
                                                      "seamless 柔度档 0.07 利落/0.1 标准/0.17 柔和；总时长另加两侧淡化）")
            tp.add_argument("--edge", type=float, help="相邻镜边缘淡化秒数（缺省随类型 0.2~0.4；xfade 族为 0）")
            tp.add_argument("--direction", choices=["tl", "tr", "bl", "br",
                                                    "left", "right", "up", "down"],
                            help="方向：wipe 用角（缺省 tr=向右上掀开）；slide 用边（缺省 left）")
            tp.add_argument("--color", choices=["black", "white", "green", "blue"],
                            help="色板：字卡型底/字成对档（black=黑底白字/white=白底深灰字）；"
                                 "scan 扫描霓虹色（green/blue，缺省 green）")
            tp.add_argument("--sound", choices=["whoosh", "riser", "boom", "off"],
                            help="转场短音效（纯 ffmpeg 合成零素材）：whoosh「呼」缺省｜riser「吸」上升蓄势｜boom「咚」低频落点｜off 关闭")
        if ta == "rm":
            tp.add_argument("--id", required=True, help="要移除的转场镜 id（只能删转场镜）")
        tp.set_defaults(func=cmd_transition)

    sp = sub.add_parser("sfx", help="音效库（music/sfx）：外置素材优先(B)·缺失合成兜底(A)·点名 AI 生成(C)，注册表 config/audio.yaml")
    sfxsub = sp.add_subparsers(dest="sfxaction", required=True)
    x = sfxsub.add_parser("list", help="注册表与素材就位状态")
    x.set_defaults(func=cmd_sfx_list)
    x = sfxsub.add_parser("gen", help="AI 生成音效并落库（ElevenLabs，付费须 --yes；--mock 零成本占位）")
    x.add_argument("--kind", required=True, help="音效键（whoosh/riser/boom 或注册表新键）")
    x.add_argument("--category", default="transitions", help="注册表分类（缺省 transitions）")
    x.add_argument("--desc", default=None, help="生成提示词（缺省用注册表 desc）")
    x.add_argument("--dur", type=float, default=1.2, help="目标时长秒（0.5~22）")
    x.add_argument("--config", default=None)
    x.add_argument("--mock", action="store_true")
    x.add_argument("--force", action="store_true")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(func=cmd_sfx_gen)

    sp = sub.add_parser("cover", help="封面设计：系列主视觉 + 章节封面（3:4 竖版海报，缺省 AI 题字·logo 级标题设计，系列风格统一）")
    sp.add_argument("project", help="项目 id")
    sp.add_argument("--chapter", help="生成指定章节封面（如 ch01；缺省只做系列主视觉）")
    sp.add_argument("--all", action="store_true", help="系列主视觉 + 全部章节封面一次生成")
    sp.add_argument("--title", help="主标题（缺省=项目标题；小说名/动漫名全系列不变）")
    sp.add_argument("--subtitle", help="副标题覆盖（章节缺省=「第 N 集」按章节序号自动）")
    sp.add_argument("--desc", help="本章画面描述（角色姿势/氛围，指挥层精写；缺省按章节标题自动拼）")
    sp.add_argument("--cast", help="封面阵容显式点名：逗号分隔角色名（按给定顺序注入，越过缺省的前 3 人规则）；"
                                   "none=不注入阵容句（构图全交 --desc）。缺省 role 只排序不筛人，撵人只有这条路")
    sp.add_argument("--aspects", help="比例列表，逗号分隔（缺省 3:4,4:3 竖横双套；任意比例如 21:9 短边 1080 自动推导）")
    sp.add_argument("--size", help="直接给像素 宽x高（单套，覆盖 --aspects）")
    sp.add_argument("--typeset-title", dest="typeset_title", action="store_true",
                    help="退回 ffmpeg 排版后置标题（零成本逃生舱·字绝对精确；缺省=AI 题字两段式，标题是画出来的设计元素，须人工查错字）")
    sp.add_argument("--font", help="标题字体：song=宋体衬线(默认按画风) kai=楷体古风 hei=现代粗黑 yuan=圆体治愈，或字体文件路径（仅 --typeset-title 排版模式用）")
    sp.add_argument("--profile", default=None, help="覆盖风格档（缺省=项目 profile）")
    sp.add_argument("--mock", action="store_true", help="mock 离线出占位封面（排版层真实可验）")
    sp.add_argument("--force", action="store_true",
                    help="重生已存在的封面（点名 --chapter 时只重生该章，系列主视觉不动）")
    sp.add_argument("--no-moodboard", dest="no_moodboard", action="store_true",
                    help="封面不套用项目参考库垫图（默认全局套用统一模块风格）")
    sp.add_argument("--config", help="models.yaml 路径（默认自动发现）")
    sp.add_argument("--workspace", help="工作区目录")
    sp.set_defaults(func=cmd_cover)

    sp = sub.add_parser("deliver", help="交付包一键导出：成片+封面+字幕+文案+manifest → zip")
    add_common(sp, profile=False)
    sp.add_argument("--platforms", help="逗号分隔平台列表（缺省=项目 platform）")
    sp.add_argument("--out", help="输出目录（缺省=project/<项目>/exports/，浅层好找）")
    sp.add_argument("--no-zip", dest="no_zip", action="store_true", help="只出目录不打 zip")
    sp.set_defaults(func=cmd_deliver)

    sp = sub.add_parser("export-pitch", help="项目提案书：单页 HTML，浏览器打印即 PDF")
    sp.add_argument("project", help="项目id")
    sp.add_argument("--out", help="输出目录（缺省=project/<项目>/pitch/）")
    ws_arg(sp); sp.set_defaults(func=cmd_export_pitch)

    sp = sub.add_parser("setup", help="安装向导：ffmpeg→密钥→存储→示例工程 mock 跑通")
    sp.add_argument("--check", action="store_true", help="非交互验收自检（交付验收用，只查不写）")
    sp.add_argument("--json", action="store_true",
                    help="机器可读就绪判定（隐含 --check；agent 解析 ready 字段即可）")
    sp.add_argument("--config", default=None); ws_arg(sp); sp.set_defaults(func=cmd_setup)

    pp = sub.add_parser("project", help="项目(系列)管理").add_subparsers(dest="paction", required=True)
    x = pp.add_parser("new", help="新建项目"); x.add_argument("--title", required=True)
    x.add_argument("--theme"); x.add_argument("--profile", default="narration")
    x.add_argument("--skill", help="绑定指挥层 skill（缺省由画风派生，如 kn-anime；报项目名/编号即可让 AI 查得该调哪个 skill）")
    x.add_argument("--platform", help="逗号分隔，如 douyin,youtube（缺省不绑定平台）")
    x.add_argument("--aspect", default=None,
                   help="主比例 9:16|16:9|1:1（缺省横屏 16:9；平台不改变缺省，竖屏须显式指定）")
    x.add_argument("--template", help="平台规格模板（template list 查看；一键落位风格/比例/模式/规格）")
    x.add_argument("--subtitle-lang", dest="subtitle_lang", choices=["zh", "en", "both"],
                   help="字幕语言（缺省 zh）：en=英文（shots 用 narration_en）；both=中英双语（双套文案分镜时一并写好）")
    x.add_argument("--id"); ws_arg(x); x.set_defaults(func=cmd_project_new)
    x = pp.add_parser("list", help="列出项目")
    x.add_argument("--deleted", action="store_true", help="只看回收站（已逻辑删除的项目）")
    ws_arg(x); x.set_defaults(func=cmd_project_list)
    x = pp.add_parser("show", help="查看项目详情"); x.add_argument("id"); ws_arg(x)
    x.set_defaults(func=cmd_project_show)
    x = pp.add_parser("set", help="更新项目/总体设计/固定场景"); x.add_argument("id")
    for f in ("title", "theme", "profile", "logline", "synopsis", "world", "tone", "palette", "scene"):
        x.add_argument(f"--{f}")
    x.add_argument("--skip-design", dest="skip_design", action="store_true",
                   help="跳过设定集：不生成/参考设定图，退回首镜锚定")
    x.add_argument("--license", choices=["exclusive", "nonexclusive"],
                   help="版权标记：exclusive=独家 nonexclusive=非独家（进交付包 manifest）")
    x.add_argument("--subtitle-lang", dest="subtitle_lang", choices=["zh", "en", "both"],
                   help="字幕语言（新建章节起继承；已建章节在其 json 顶层 subtitle_lang 手动补）")
    x.add_argument("--style-prompt", dest="style_prompt",
                   help="画风前缀·中文（单点真源：分镜图/设定图/封面统一取用；立项自动从 profile 快照，"
                        "改此项=全局换画风；新建章节起继承，已建章节经 agent plan apply 改 style_prompt）")
    x.add_argument("--style-prompt-en", dest="style_prompt_en",
                   help="画风前缀·英文（prompt_lang=en 的海外模型自动选用）")
    x.add_argument("--skill", help="换绑定 skill（旁白语态等派生随它；只改 profile 不会动它）")
    ws_arg(x); x.set_defaults(func=cmd_project_set)
    x = pp.add_parser("rm", help="逻辑删除项目（唯一删除语义：数据/产物/库行完整保留，随时 restore 恢复）")
    x.add_argument("id")
    x.add_argument("--archive", action="store_true", help="归档而非删除（status=archived，仍在清单中）")
    ws_arg(x); x.set_defaults(func=cmd_project_rm)
    x = pp.add_parser("restore", help="恢复逻辑删除的项目（清 is_deleted，立即回到清单与全部流程）")
    x.add_argument("id"); ws_arg(x); x.set_defaults(func=cmd_project_restore)
    x = pp.add_parser("refs", help="生成设定集（角色/场景/道具设定图，后续各镜强制参考）")
    x.add_argument("id"); x.add_argument("--profile", default=None)
    x.add_argument("--force", action="store_true", help="重生已有设定图")
    x.add_argument("--only", default=None, metavar="kind[:名]",
                   help="只(重)生成单张设定图：character:名 / scene[:名] / prop:名 / "
                        "expression:名 / pose:名 / topview[:名]（网页设定图垫图重生走此）"
                        "。scene 连带出该场景的俯视图；只重画图纸用 topview[:名]。"
                        "带名字只动被点名的那张；全局固定场景那一档走不带冒号的 scene / topview")
    x.add_argument("--expressions", action="store_true",
                   help="附带生成角色表情设定图（4×3 十二格·required_emotions 优先入格；"
                        "扩展图恒直出单张、不走候选宫格、不进每镜自动挂载）")
    x.add_argument("--poses", action="store_true",
                   help="附带生成角色动作设定图（5×3 十五格·required_actions 优先入格；"
                        "角色有武器时对抗类动作持械）")
    x.add_argument("--candidates", type=int, default=1,
                   help="每项设定图出 N 张候选待宫格点选（缺省 1 张直出定稿；扩展设定图不适用）")
    x.add_argument("--no-moodboard", dest="no_moodboard", action="store_true",
                   help="本次设定图不套用项目参考库垫图（默认全局套用）")
    add_concurrency(x)
    x.add_argument("--mock", action="store_true"); x.add_argument("--config", default=None)
    ws_arg(x); x.set_defaults(func=cmd_gen_refs)
    x = pp.add_parser("moodboard", help="参考库（风格垫图）：列出/登记/移除/切换默认启用（默认全局套用到所有生成）")
    x.add_argument("id", help="项目 id")
    x.add_argument("--add", metavar="图片路径", help="登记一张垫图进参考库（默认启用，绝对/相对路径均可）")
    x.add_argument("--rm", metavar="路径", help="从参考库移除一张垫图")
    x.add_argument("--on", metavar="路径", help="启用某垫图（默认套用到所有生成）")
    x.add_argument("--off", metavar="路径", help="停用某垫图（留库但不默认套用，靠提示词）")
    ws_arg(x); x.set_defaults(func=cmd_project_moodboard)
    x = pp.add_parser("pick-ref", help="设定图候选点选定稿（旧稿备份·血缘传播·可换选）")
    x.add_argument("id", help="项目id")
    x.add_argument("--asset", required=True, help="character:名 / scene / prop:名")
    x.add_argument("--use", required=True, type=int, help="候选编号（1 起）")
    ws_arg(x); x.set_defaults(func=cmd_pick_ref)

    cc = sub.add_parser("character", help="角色预设管理").add_subparsers(dest="caction", required=True)
    x = cc.add_parser("add", help="添加角色（含服装/发型/武器，供设定图生成）")
    x.add_argument("project"); x.add_argument("--name", required=True)
    x.add_argument("--voice-prompt", dest="voice_prompt",
                   help="声线描述（六槽位 40~80 字：性别年龄段/音区明暗/音质质感/语速节奏/"
                        "口音吐字/气质，不写情绪词）：按它定制一把音色并立档启用")
    x.add_argument("--voice", help="模版音色别名（显式例外；缺省走 --voice-prompt 定制）")
    x.add_argument("--appearance"); x.add_argument("--role")
    x.add_argument("--speech-style", dest="speech_style", help="台词口吻")
    x.add_argument("--personality", help="性格内核")
    x.add_argument("--arc", help="人物弧光")
    x.add_argument("--silhouette", dest="silhouette_notes", help="剪影特征（进设定图提示词）")
    x.add_argument("--constraint", action="append", help="画面硬约束（可多次）：编译进 negative_prompt")
    x.add_argument("--taboo", action="append", help="行为禁区（可多次）")
    x.add_argument("--outfit", help="服装"); x.add_argument("--hair", help="发型")
    x.add_argument("--weapon", help="武器/持物")
    x.add_argument("--subject-kind", help="主体类型 human/animal/creature/robot/spirit/other；不填不猜测")
    x.add_argument("--visual-requirement", action="append",
                   help="必须保留的正向视觉特征（可多次），如左臂义体、圆框眼镜；不进 negative")
    x.add_argument("--keyword", action="append",
                   help="别名/绰号/尊称（可多次）——正文实体命中与缺席判定的兜底口径；"
                        "本名不足 2 字或常以绰号称呼的角色不补这个就永远命中不了")
    x.add_argument("--gender", help="性别 male/female（也认 男/女）——试音候选按此过滤；"
                                    "不填时引擎按 现有音色→appearance/role 文本 推断")
    x.add_argument("--ref"); ws_arg(x); x.set_defaults(func=cmd_character_add)
    x = cc.add_parser("set", help="更新既有角色的**文字**设定（外貌四件套/文字人设四件/"
                      "M8 五字段）——设定边写边长，别手改 project.json（会被长任务整份覆写）")
    x.add_argument("project"); x.add_argument("--name", required=True)
    x.add_argument("--voice-prompt", dest="voice_prompt",
                   help="声线描述（六槽位 40~80 字，写法同 add）：按它重新定制一把音色并立档启用"
                        "（旧声烧过的片段置 retake）")
    x.add_argument("--voice", help="模版音色别名（显式例外）")
    x.add_argument("--appearance"); x.add_argument("--role")
    x.add_argument("--outfit", help="服装"); x.add_argument("--hair", help="发型")
    x.add_argument("--weapon", help="武器/持物")
    x.add_argument("--subject-kind", help="主体类型 human/animal/creature/robot/spirit/other；不填不猜测")
    x.add_argument("--speech-style", dest="speech_style",
                   help="台词口吻（人设门盲测判据：遮住名字读台词能认出是谁）")
    x.add_argument("--personality", help="性格内核（决定他在压力下怎么选）")
    x.add_argument("--arc", help="人物弧光（起点→当前阶段→终点；推进了就来改这条）")
    x.add_argument("--taboo", action="append",
                   help="行为禁区（整体替换·可多次）：命中即人设门打回")
    x.add_argument("--add-taboo", dest="add_taboo", action="append", help="行为禁区（并集追加）")
    x.add_argument("--keyword", action="append", help="别名/绰号（整体替换·可多次）：正文实体命中兜底")
    x.add_argument("--add-keyword", dest="add_keyword", action="append", help="别名（并集追加）")
    x.add_argument("--gender", help="性别 male/female（也认 男/女）——试音候选按此过滤")
    x.add_argument("--silhouette", dest="silhouette_notes", help="剪影特征（进设定图提示词）")
    x.add_argument("--constraint", action="append",
                   help="画面硬约束（整体替换·可多次）：编译进 negative_prompt，**不进设定图**")
    x.add_argument("--add-constraint", dest="add_constraint", action="append",
                   help="画面硬约束（并集追加）")
    x.add_argument("--visual-requirement", action="append",
                   help="正向视觉特征（整体替换·可多次）：进正向提示词，不进 negative")
    x.add_argument("--add-visual-requirement", dest="add_visual_requirement", action="append",
                   help="正向视觉特征（并集追加）")
    x.add_argument("--emotion", action="append", help="全系列必演情绪（整体替换·可多次）")
    x.add_argument("--action", action="append", help="全系列必演动作（整体替换·可多次）")
    x.add_argument("--view", action="append", help="全系列必要视角（整体替换·可多次）")
    x.add_argument("--status", choices=novel_mod.CHAR_STATUS,
                   help="在场状态：active（缺省）/ departed 已退场 / dead 已死。"
                        "非 active 不再进 novel lint 的「连续缺席」提醒——"
                        "长篇里永久退场是常态，恒报即等于不报")
    x.add_argument("--sync", action="store_true",
                   help="顺带把设定推送到已建章节（存量章节持有的是创建时拷贝）")
    ws_arg(x); x.set_defaults(func=cmd_character_set)
    x = cc.add_parser("show", help="打印角色文字设定卡（写正文/写台词的写前必读物料，"
                      "比 Read 整份 project.json 省得多）")
    x.add_argument("project"); x.add_argument("--name", help="缺省打印全部角色")
    ws_arg(x); x.set_defaults(func=cmd_character_show)
    x = cc.add_parser("list", help="列出角色"); x.add_argument("project"); ws_arg(x)
    x.set_defaults(func=cmd_character_list)
    x = cc.add_parser("rm", help="移除角色"); x.add_argument("project"); x.add_argument("--name", required=True)
    ws_arg(x); x.set_defaults(func=cmd_character_rm)

    sc = sub.add_parser("scene", help="具名场景（取景地）设定管理").add_subparsers(
        dest="scaction", required=True)
    x = sc.add_parser("add", help="添加具名场景（出环境 key art 设定图）")
    x.add_argument("project"); x.add_argument("--name", required=True)
    x.add_argument("--desc", default="")
    x.add_argument("--keyword", action="append",
                   help="场景别名/关键词（可多次）：image_prompt/narration 命中即自动挂设定图")
    ws_arg(x); x.set_defaults(func=cmd_scene_add)
    x = sc.add_parser("set", help="更新既有场景的文字设定（desc/keywords）——"
                      "场景随剧情变（塌了/换季/易主）就来改这条")
    x.add_argument("project"); x.add_argument("--name", required=True)
    x.add_argument("--desc")
    x.add_argument("--keyword", action="append", help="别名（整体替换·可多次）")
    x.add_argument("--add-keyword", dest="add_keyword", action="append", help="别名（并集追加）")
    x.add_argument("--sync", action="store_true", help="顺带推送到已建章节")
    ws_arg(x); x.set_defaults(func=cmd_scene_set)
    x = sc.add_parser("list", help="列出具名场景")
    x.add_argument("project"); ws_arg(x); x.set_defaults(func=cmd_scene_list)
    x = sc.add_parser("rm", help="移除具名场景")
    x.add_argument("project"); x.add_argument("--name", required=True)
    ws_arg(x); x.set_defaults(func=cmd_scene_rm)

    dp = sub.add_parser("prop", help="道具/武器设定管理").add_subparsers(dest="daction", required=True)
    x = dp.add_parser("add", help="添加道具/武器设定"); x.add_argument("project")
    x.add_argument("--name", required=True); x.add_argument("--desc")
    x.add_argument("--kind", choices=["prop", "weapon"], default="prop")
    x.add_argument("--keyword", action="append",
                   help="道具别名/关键词（可多次）：image_prompt/narration 命中即自动挂设定图")
    ws_arg(x); x.set_defaults(func=cmd_prop_add)
    x = dp.add_parser("set", help="更新既有道具的文字设定（desc/kind/keywords）——"
                      "剑断了、令牌易主就来改这条，别手改 project.json")
    x.add_argument("project"); x.add_argument("--name", required=True)
    x.add_argument("--desc"); x.add_argument("--kind", choices=["prop", "weapon"])
    x.add_argument("--keyword", action="append", help="别名（整体替换·可多次）")
    x.add_argument("--add-keyword", dest="add_keyword", action="append", help="别名（并集追加）")
    x.add_argument("--sync", action="store_true", help="顺带推送到已建章节")
    ws_arg(x); x.set_defaults(func=cmd_prop_set)
    x = dp.add_parser("list", help="列出道具/武器"); x.add_argument("project"); ws_arg(x)
    x.set_defaults(func=cmd_prop_list)
    x = dp.add_parser("rm", help="移除道具/武器"); x.add_argument("project"); x.add_argument("--name", required=True)
    ws_arg(x); x.set_defaults(func=cmd_prop_rm)

    hh = sub.add_parser("chapter", help="章节管理").add_subparsers(dest="haction", required=True)
    x = hh.add_parser("new", help="新建章节"); x.add_argument("project")
    x.add_argument("--title", required=True,
                   help="本集剧情标题（钩子式短标题如「一稿过」，勿复用项目名）")
    x.add_argument("--theme"); x.add_argument("--id"); ws_arg(x); x.set_defaults(func=cmd_chapter_new)
    x = hh.add_parser("list", help="列出章节"); x.add_argument("project"); ws_arg(x)
    x.set_defaults(func=cmd_chapter_list)
    x = hh.add_parser("show", help="查看章节"); x.add_argument("project"); x.add_argument("chapter_id")
    ws_arg(x); x.set_defaults(func=cmd_chapter_show)
    x = hh.add_parser("rm", help="删除章节"); x.add_argument("project"); x.add_argument("chapter_id")
    ws_arg(x); x.set_defaults(func=cmd_chapter_rm)
    x = hh.add_parser("set", help="改章节绑定（建章时拷贝的 skill/profile）与视频路由点名；"
                                  "其余作者字段走 agent plan apply，此处不开第二份可写面")
    x.add_argument("project"); x.add_argument("chapter_id")
    x.add_argument("--title", help="改本集标题（裸剧情短标题，不带「第N章/第N集」序号）")
    x.add_argument("--skill", help="换绑 skill（未登记值当场失败；旁白语态等派生随它）")
    x.add_argument("--profile", help="换绑画风（未登记值当场失败；style_prompt 在场时不改画面）")
    x.add_argument("--video-provider", dest="video_provider",
                   help="本章 gen-video 持久点名的视频 provider 别名（如 seedance-2.5）；"
                        "缺省恒走 mini 主力，gen-video --video-provider 单次优先")
    x.add_argument("--inherit", action="store_true",
                   help="删掉本章自持的 skill/profile/video_provider，回落缺省")
    x.add_argument("--budget", type=float, default=None,
                   help="本章花费上限（元）：事前闸与事后闸都读它；0 = 不设限")
    x.add_argument("--budget-per-call", dest="budget_per_call", type=float, default=None,
                   help="本章单笔调用上限（元）：gen-video 超阈须 --confirm-spend；0 = 不设限")
    x.add_argument("--config", default=None, help=argparse.SUPPRESS)
    ws_arg(x); x.set_defaults(func=cmd_chapter_set)

    ad = sub.add_parser(
        "adapt", help="剧本改编：小说/剧本入库→分集→建章（Track A 纯 Python 承接；"
        "拆书/分集/抽取由 AI 指挥层完成，见 kinema-project SKILL）"
    ).add_subparsers(dest="adaction", required=True)
    x = ad.add_parser("import", help="源文本入库：正文落盘 source/raw.txt + 结构预切分 "
                      "segments.json + 回填 source 指针块（纯机械·零 LLM）")
    x.add_argument("project"); x.add_argument("--file", required=True,
                   help="小说/剧本源文件（.txt/.fountain/.fdx）")
    x.add_argument("--kind", choices=["auto", "novel", "screenplay"], default="auto",
                   help="源文本类型（缺省 auto 自动判定）")
    ws_arg(x); x.set_defaults(func=cmd_adapt_import)
    x = ad.add_parser("scaffold", help="据 episodes[] 幂等批量建章 + 拷 outline"
                      "（章号==集号·可重跑不炸）")
    x.add_argument("project"); x.add_argument("--only", help="只建这几集（集号逗号分隔，如 1,3,5）")
    ws_arg(x); x.set_defaults(func=cmd_adapt_scaffold)
    x = ad.add_parser("show", help="打印 source 元数据 + 拆书 adaptation + 分集大纲")
    x.add_argument("project"); ws_arg(x); x.set_defaults(func=cmd_adapt_show)
    x = ad.add_parser("merge-entities", help="合并 AI 产出的候选实体入库"
                      "（合并不覆盖·保人工 voice/keywords/comments·keywords 取并集）")
    x.add_argument("project"); x.add_argument("--file", required=True,
                   help='候选实体 JSON：{"characters":[...],"props":[...]}')
    ws_arg(x); x.set_defaults(func=cmd_adapt_merge_entities)
    x = ad.add_parser("graph", help="落库 AI 产出的人物关系 / 世界观图谱"
                      "（nodes+edges·整体替换·校验悬空边；剧本工作台「图谱」Tab 渲染）")
    x.add_argument("project"); x.add_argument("--file", required=True,
                   help='图谱 JSON：{"summary":"…","nodes":[{id,name,type,…}],'
                        '"edges":[{source,target,relation,kind}]}')
    ws_arg(x); x.set_defaults(func=cmd_adapt_graph)

    st = sub.add_parser(
        "study", help="参考片读片：量一支本地参考片的节奏骨架（切点密度/每镜时长/"
        "静音占比/等间隔关键帧）供立项参考。只量不判——motion 选型由 AI 指挥层做，"
        "见 kinema-project SKILL「参考片立项模式」"
    ).add_subparsers(dest="staction", required=True)
    x = st.add_parser("import", help="参考片入库：拷进 study/<slug>/ + 量节奏 + 抽帧 + "
                      "落 digest.json（纯机械·零 LLM·不联网·不吃 URL）")
    x.add_argument("project"); x.add_argument("--file", required=True,
                   help="本地参考片（.mp4/.mov/.webm/.mkv/.m4v/.avi）")
    x.add_argument("--cuts", type=float, default=study_mod.CUT_THRESHOLD,
                   help=f"切点判据阈值 0~1（缺省 {study_mod.CUT_THRESHOLD}，越小越敏感）")
    x.add_argument("--frames", type=int, default=study_mod.DEFAULT_FRAMES,
                   help=f"等间隔抽帧数（缺省 {study_mod.DEFAULT_FRAMES}，上限 {study_mod.MAX_FRAMES}）")
    x.add_argument("--subs", help="外挂字幕 .srt（缺省自动导出内嵌字幕流，无则跳过）")
    x.add_argument("--title", help="备注名（缺省用 slug）")
    x.add_argument("--slug", help="目录名（缺省从文件名派生并自动去重；同 slug 重导=覆盖重算）")
    ws_arg(x); x.set_defaults(func=cmd_study_import)
    x = st.add_parser("show", help="打印全部读片记录的节奏可测量量 + sidecar 指针")
    x.add_argument("project"); ws_arg(x); x.set_defaults(func=cmd_study_show)
    x = st.add_parser("rm", help="删读片记录 + 整个 study/<slug>/（版权卫生：读完即删）")
    x.add_argument("project"); x.add_argument("--slug", required=True)
    ws_arg(x); x.set_defaults(func=cmd_study_rm)

    nv = sub.add_parser(
        "novel", help="原创小说创作层：正文登记/精简大纲/章末状态/伏笔账本/里程碑/"
        "跨章体检（写作与叙事判定由 AI 指挥层完成，见 kinema-novel SKILL）"
    ).add_subparsers(dest="nvaction", required=True)
    x = nv.add_parser("init", help="初始化 manuscript/ 正文目录 + 文风契约 "
                      "narrative_style 骨架（文风单点真源，防漂靠基线比对）")
    x.add_argument("project")
    x.add_argument("--pov", help="叙事视角（如「第三人称有限·跟随主角」）")
    x.add_argument("--tense", help="时态（如「过去时」）")
    x.add_argument("--voice", help="叙事声音（如「冷峻克制·短句快节奏」）")
    x.add_argument("--diction", help="语域用词（如「现代口语为主·系统词条精确化」）")
    x.add_argument("--avoid", help="文字忌讳词（逗号分隔·并集合入，lint 扫最新章）")
    ws_arg(x); x.set_defaults(func=cmd_novel_init)
    x = nv.add_parser("save", help="登记一章正文：落盘 manuscript/chNNNN.md + 版本归档 + "
                      "字数指纹 + 实体命中统计 + 里程碑提醒（同内容重跑幂等不叠版本）")
    x.add_argument("project"); x.add_argument("--no", type=int, required=True, help="章号")
    x.add_argument("--file", required=True, help="正文文件（.md/.txt，UTF-8）")
    x.add_argument("--title", help="章节标题")
    x.add_argument("--digest", help="精简大纲（两三句；也可事后 novel digest 补）")
    x.add_argument("--state", help="章末状态快照 JSON 文件（三条命令合一，省一次往返）")
    x.add_argument("--payoff", choices=novel_mod.PAYOFF_LEVELS,
                   help="本章兑现等级（第⑦门节奏账·opt-in；不声明则整段不报）")
    x.add_argument("--payoff-kind", dest="payoff_kind",
                   choices=novel_mod.PAYOFF_KINDS, help="兑现类型（同型连用 3 章即报）")
    x.add_argument("--hook", choices=novel_mod.HOOK_KINDS,
                   help="断章型（同型连用 3 章即报——断章七型要轮着来）")
    ws_arg(x); x.set_defaults(func=cmd_novel_save)
    x = nv.add_parser("digest", help="登记一章精简大纲（本章事件+变化+尾钩，两三句）")
    x.add_argument("project"); x.add_argument("--no", type=int, required=True)
    x.add_argument("--text", required=True)
    ws_arg(x); x.set_defaults(func=cmd_novel_digest)
    x = nv.add_parser("state", help="登记章末状态快照（下一章写前必读）："
                      '{"time","location","characters":{名:一句话},"hooks":[…],"note"}')
    x.add_argument("project"); x.add_argument("--no", type=int, required=True)
    x.add_argument("--file", required=True, help="状态快照 JSON 文件")
    ws_arg(x); x.set_defaults(func=cmd_novel_state)
    x = nv.add_parser("thread-add", help="登记一条伏笔（埋设章 + 期限或跨度档；"
                      "超期由 lint 派生判定，绝不落盘）")
    x.add_argument("project"); x.add_argument("--title", required=True)
    x.add_argument("--setup", type=int, required=True, help="埋设章号")
    x.add_argument("--due", type=int, help="回收期限章号（不给则按 --tier 推）")
    x.add_argument("--tier", choices=tuple(novel_mod.THREAD_TIERS),
                   help="跨度档：short=+30 章 / mid=+100 章 / long=无期限但恒进长期挂起统计"
                        "（长线也必须显式声明——不填 due 不等于免于追踪）")
    x.add_argument("--note")
    ws_arg(x); x.set_defaults(func=cmd_novel_thread_add)
    x = nv.add_parser("thread-set", help="改伏笔的标题/埋设章/期限/跨度档/备注"
                      "（改状态走 thread-pay / thread-drop）")
    x.add_argument("project"); x.add_argument("--id", required=True)
    x.add_argument("--title"); x.add_argument("--setup", type=int)
    x.add_argument("--due", type=int)
    x.add_argument("--tier", choices=tuple(novel_mod.THREAD_TIERS))
    x.add_argument("--note")
    ws_arg(x); x.set_defaults(func=cmd_novel_thread_set)
    x = nv.add_parser("thread-pay", help="标记伏笔已回收（必须给回收章号——账本的意义就在这条对账线）")
    x.add_argument("project"); x.add_argument("--id", required=True, help="伏笔 id（thNN）")
    x.add_argument("--in", dest="paid_in", type=int, required=True, help="回收章号")
    x.add_argument("--note")
    ws_arg(x); x.set_defaults(func=cmd_novel_thread_pay)
    x = nv.add_parser("thread-drop", help="弃置伏笔（记录在案不再追讨；记错了可再 add）")
    x.add_argument("project"); x.add_argument("--id", required=True)
    x.add_argument("--note")
    ws_arg(x); x.set_defaults(func=cmd_novel_thread_drop)
    x = nv.add_parser("arc", help="卷/幕规划（长篇的大纲落点）：登记或更新一卷。"
                      "检查点第一门「有没有跑偏大纲」要有对照物，靠的就是它")
    x.add_argument("project"); x.add_argument("--no", type=int, required=True, help="卷号")
    x.add_argument("--title", help="卷名（新建必给）")
    x.add_argument("--from", dest="frm", type=int, help="起始章号（新建必给）")
    x.add_argument("--to", dest="to", type=int, help="终止章号（可后补）")
    x.add_argument("--premise", help="本卷前提（承接上卷什么局面）")
    x.add_argument("--goal", help="本卷要达成的叙事目标")
    x.add_argument("--climax", help="本卷高潮/收卷方式")
    x.add_argument("--turn", action="append", help="关键节拍（可多次·整体替换）")
    x.add_argument("--note")
    ws_arg(x); x.set_defaults(func=cmd_novel_arc_set)
    x = nv.add_parser("arc-rm", help="删除一卷规划")
    x.add_argument("project"); x.add_argument("--no", type=int, required=True)
    ws_arg(x); x.set_defaults(func=cmd_novel_arc_rm)
    x = nv.add_parser("arcs", help="列出卷规划 + 派生进度态（已收卷/进行中/未开写）"
                      "与覆盖体检（断档/重叠）")
    x.add_argument("project"); x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_arc_list)
    x = nv.add_parser("brief", help="【写前必读包】文风契约+当前卷纲+世界观宪法+上章状态"
                      "+未回收伏笔+在场角色人设卡——一次调用取齐，别再整本回灌")
    x.add_argument("project")
    x.add_argument("--no", type=int, help="要写的章号（缺省=最新章+1）")
    x.add_argument("--chars", help="点名要哪几个角色的人设卡（逗号分隔；"
                   "缺省取上一章在场者）")
    x.add_argument("--all", dest="all_chars", action="store_true", help="列全部角色人设卡")
    x.add_argument("--bible", help="点名要宪法的哪几节（标题子串，逗号分隔）；"
                   "`all` = 全量。缺省按本章相关性自动挑几节 + 打全目录")
    x.add_argument("--no-bible", dest="no_bible", action="store_true",
                   help="连宪法目录也省掉（同一会话连写多章、宪法已在上下文里时用）")
    x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_brief)
    x = nv.add_parser("recap", help="【批次复核物料】逐章概要表+伏笔动静+本批新登场实体"
                      "+缺项+文体量化——十章检查点《批次报告》的骨架（逐项数不许估）")
    x.add_argument("project")
    x.add_argument("--from", dest="frm", type=int, help=f"起始章（缺省=最近 {novel_mod.MILESTONE_EVERY} 章）")
    x.add_argument("--to", dest="to", type=int, help="终止章（缺省=最新章）")
    x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_recap)
    x = nv.add_parser("lint", help="跨章确定性体检（零成本·不落盘）：章号断档/缺大纲缺状态/"
                      "伏笔超期与挂起/角色缺席/卷覆盖断档重叠/忌讳词/篇幅节奏/"
                      "**文体量化（AI 味口癖·句长离散度·对白占比·跨章复读）**——只出可测量量不判文学")
    x.add_argument("project")
    x.add_argument("--from", dest="frm", type=int,
                   help=f"文体扫描窗口起章（缺省=最近 {novel_mod.PROSE_WINDOW} 章；"
                        "账目类检查恒看全书）")
    x.add_argument("--to", dest="to", type=int, help="文体扫描窗口止章")
    x.add_argument("--level", choices=("warn", "all"), default="all",
                   help="只看待办（warn）还是全部（缺省 all）")
    x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_lint)
    x = nv.add_parser("show", help="创作总览：进度/文风契约/最近日志/逐章登记态/"
                      "伏笔账本/卷规划/下一检查点（缺省折叠，接手第 1 步跑它）")
    x.add_argument("project")
    x.add_argument("--all", action="store_true", help="不折叠（长篇上会很长）")
    x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_show)
    x = nv.add_parser("log", help="创作日志（append-only）：检查点结论/重大决策/全书手术"
                      "——跨会话唯一的「上次是怎么判的」载体。不给 --text 即列出")
    x.add_argument("project")
    x.add_argument("--kind", choices=novel_mod.LOG_KINDS, default="note")
    x.add_argument("--text", help="记什么（不给=列出）")
    x.add_argument("--at", type=int, help="发生在第几章")
    x.add_argument("--ref", help="关联文件（如 plan/batch-66-75.md）")
    x.add_argument("--limit", type=int, help="列出时只看最近 N 条")
    x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_log)
    x = nv.add_parser("sweep", help="跨七层检索一个词（正文/设定卡/digest/state/卷纲/"
                      "伏笔/宪法）——「改设定＝改七层」的收工判据，引擎只出清单不判合法性")
    x.add_argument("project"); x.add_argument("--term", required=True)
    x.add_argument("--min-len", dest="min_len", type=int, default=2,
                   help="检索词最短字数（防短词把七层刷满）")
    x.add_argument("--json", action="store_true")
    ws_arg(x); x.set_defaults(func=cmd_novel_sweep)
    x = nv.add_parser("reindex", help="按磁盘正文重算字数/指纹/实体命中回写登记块"
                      "（手改过正文、或后补了角色 keywords 之后跑）")
    x.add_argument("project")
    x.add_argument("--no", type=int, help="只重算这一章（缺省=全书）")
    x.add_argument("--archive", action="store_true",
                   help="把当前磁盘稿另存一份进版本栈留档")
    ws_arg(x); x.set_defaults(func=cmd_novel_reindex)
    x = nv.add_parser("normalize", help="正文排版规范化：剥掉**非面板**的加粗"
                      "（执行铁律「粗体只给面板」；逐章走 save 故旧稿进版本栈可回滚）。"
                      "刻意不碰 `---` 分隔线——那条要读上下文才判得出")
    x.add_argument("project")
    x.add_argument("--no", type=int, help="只规范化这一章（缺省=全书）")
    x.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="只报要改什么，一个字不写盘")
    ws_arg(x); x.set_defaults(func=cmd_novel_normalize)
    x = nv.add_parser("revert", help="章级回滚：把某历史版拷回正文并重登记"
                      "（当前稿先归档，可再滚回去）")
    x.add_argument("project"); x.add_argument("--no", type=int, required=True)
    x.add_argument("--v", type=int, help="回滚到第几版（缺省=最近一版）")
    ws_arg(x); x.set_defaults(func=cmd_novel_revert)
    x = nv.add_parser("versions", help="列出某章的版本谱系")
    x.add_argument("project"); x.add_argument("--no", type=int, required=True)
    ws_arg(x); x.set_defaults(func=cmd_novel_versions)
    x = nv.add_parser("baseline", help="在认可的那批整章上算文风数值基线（μ±σ）落 "
                      "narrative_style.baseline_metrics——文风门第一次有数可对")
    x.add_argument("project")
    x.add_argument("--from", dest="frm", type=int, required=True)
    x.add_argument("--to", dest="to", type=int, required=True)
    ws_arg(x); x.set_defaults(func=cmd_novel_baseline)
    x = nv.add_parser("style", help="改文风契约（唯一写路径，别裸改 narrative_style）："
                      "四项 + 基线样本增删 + 忌讳词增删")
    x.add_argument("project")
    x.add_argument("--pov"); x.add_argument("--tense")
    x.add_argument("--voice"); x.add_argument("--diction")
    x.add_argument("--add-baseline", dest="add_baseline", action="append",
                   help="追加一段基线样本（可给文件路径或直接给正文；可多次）")
    x.add_argument("--rm-baseline", dest="rm_baseline", type=int,
                   help="删除第 N 段基线样本（1 起）")
    x.add_argument("--add-avoid", dest="add_avoid", help="追加忌讳词（逗号分隔）")
    x.add_argument("--rm-avoid", dest="rm_avoid", help="删除忌讳词（逗号分隔）")
    ws_arg(x); x.set_defaults(func=cmd_novel_style)
    x = nv.add_parser("bible", help="写世界观宪法（唯一写路径，别裸改 "
                      "adaptation.world_bible）：整份替换 / --section 按节替换 / --append；"
                      "不给内容即列出节目录")
    x.add_argument("project")
    x.add_argument("--file", help="宪法文件（.md）")
    x.add_argument("--text", help="直接给正文（短改用）")
    x.add_argument("--section", help="只替换标题含此子串的那一节（须唯一命中）")
    x.add_argument("--append", action="store_true", help="追加到末尾")
    ws_arg(x); x.set_defaults(func=cmd_novel_bible)
    x = nv.add_parser("export", help="按登记章序合并正文导出（不是文件名字典序，"
                      "断号不会静默错位；绝不收 versions/ 旧稿）")
    x.add_argument("project")
    x.add_argument("--from", dest="frm", type=int)
    x.add_argument("--to", dest="to", type=int)
    x.add_argument("--strip-markup", dest="strip_markup", action="store_true",
                   help="剥掉 markdown 记号出纯文本（交稿/改编成视频用）")
    x.add_argument("--out", help="输出路径（缺省 project/<pid>/exports/）")
    ws_arg(x); x.set_defaults(func=cmd_novel_export)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        return result if isinstance(result, int) else 0
    except KinemaError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
