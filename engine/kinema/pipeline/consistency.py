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

"""角色跨镜一致性校验：**引擎产料 → 指挥层判定 → CLI 回填**。

# 为什么引擎不打分

「裁出角色区域再自动打分」在本工程的零依赖底线下做不到：
· ffmpeg **没有人脸/人体检测**（`cropdetect` 只检黑边，`signalstats` 只出亮度色度统计）；
· `ssim`/`psnr` 是**同分辨率逐像素**度量，对「同一角色换姿态/换角度/换景别」毫无判别力
  ——同角色不同镜的 SSIM 往往低于不同角色同构图的 SSIM，用它判一致性等于掷硬币；
· 就算装 CLIP（INFRA-6 的 `vision-clip` extra，GB 级）也不成立：一边是**分镜整帧**
  （含背景、构图、多角色），另一边是**三区灰底设定表**（`cli._char_sheet_prompt` 的
  三区两视版式），全图余弦相似度会被版式与背景主导，量出来的是"像不像一张设定表"，
  不是"这个角色像不像这个角色"。

所以本模块只做**产料**——把「这一镜的代表帧」与「这一镜该出场角色的设定图」配好对、
落成一份 `manifest.json`，交给真正有多模态判别力的指挥层（Claude）去看图下判断，
判断结果再由 `consistency set` 回填。**引擎全程不产生任何分数**，`score` 字段是
指挥层给的主观分（0~1），不是机器算的。

# 产物

    <章节>_work/consistency/
        shot_<id>.png     每镜代表帧（kenburns=分镜图缩放拷贝 / dubbed·native=片段中点帧）
        manifest.json     配对清单（帧 ↔ 角色设定图，绝对路径，可直接 Read）

# 三条不许绕开的口径

1. **角色项只从 `lineage.required_refs` 取 `kind=="character"`**——绝不另写一套
   「本镜出场角色」推导。另写一套 = 与 `design_refs`（实际喂给模型的参考图）分叉，
   重演过一次道具挂载不对称事故：校验用的角色集与生成用的角色集必须同源。
2. **设定图路径必须过 `storage.media.ensure_local()`**——`required_refs` 返回的是
   `characters[].sheet` **原值**，OSS 模式下那是一条 https URL：直接喂 ffmpeg 会失败，
   写进 manifest 让 Claude `Read` 也会失败（Read 只吃本地路径）。
3. **"没有可比对角色"必须显式说出来**（`reason` + `report_lines` 的整行告警）。
   `shots[].characters == []`（显式空出场表）、`skip_design` 项目、设定图还没生成
   ——这三种都会产出**空 sheets** 的镜。静默产出空清单，指挥层会把"没料可比"
   误读成"比过了没问题"，这是本功能最危险的失效模式。

# 与审阅状态机的接线

`consistency set --verdict drift --retake` 照抄 `lineage.mark_stale` 的纪律：
未锁定 → `review.set_state(retake)`（下次生成阶段自动重生 + 旧版归档进版本栈）；
已通过锁定(done) → **只留 `shots[].consistency` 这条判定当标记，绝不代人解锁**。
dubbed/native 判 clip 漂移时**同时把 image 也置 retake**：Seedance 恒以该镜分镜图
作首帧/参考图，片段里的角色长歪，根因几乎总在分镜图——只重生片段等于在同一根因上再试一次。

反向那一半同样是纪律：**渲染物一被替换，旧判定当场作废**（`invalidate`，照
`lineage.clear_stale` 的范式）。判定是对**某一版渲染物**下的结论，留着就会挂在一张
没人判过的新图上，甚至与人工刚点的「✓ 已通过」同时出现——判定信号从此不可信。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from .. import lineage, review
from ..errors import FFmpegError, ProjectError
from ..project import normalize_motion
from ..ffmpeg import ensure_tools, probe_duration, run
from . import transitions as tr_mod

# 产物子目录（`Project.subdir` 建目录并返回）
SUBDIR = "consistency"
MANIFEST = "manifest.json"

# 代表帧宽度：768px 足够多模态判别五官/服装/配色，又不让 manifest 目录膨胀；
# **绝不放大**（源比它小就按源尺寸出，见 `_scale_expr`）——放大只是插值出的假细节。
FRAME_WIDTH = 768
# 时长不可知时的抽帧点位（与 Studio 缩略图 `server._poster` 同点位，避开首帧编码边界）
FALLBACK_TS = 0.8

VERDICTS = ("ok", "drift")
DEFAULT_BY = "claude"            # 判定人缺省=指挥层（引擎不打分，机器永远不会是 by）
# 判定判的是「画面」，而画面只有两种渲染物：kenburns=分镜图 · dubbed/native=片段。
# audio 不在内——重跑配音不改画面，不该动判定（见 `invalidate`）。
VISUAL_STAGES = ("image", "clip")

# 「本镜无可比对角色」的原因枚举（→ 中文说明，CLI 逐行打印）
REASONS: dict[str, str] = {
    "skip_design": "项目 skip_design：不走设定集，没有设定图可比",
    "empty_cast": "本镜显式空出场表 shots[].characters=[]，作者声明本镜无角色",
    "no_cast": "项目未登记任何角色，characters 为空",
    "cast_unmatched": "shots[].characters 点名的角色不在项目角色表里，名字可能写错",
    "sheets_missing": "出场角色的设定图文件不存在——先跑 project refs 生成设定集",
}
# 跳过原因（有角色可比、但这一镜还没产物可抽帧）
SKIPS: dict[str, str] = {
    "no_image": "尚未生成分镜图（先跑 gen-image）",
    "no_clip": "尚未生成动态片段（先跑 gen-video）",
    "frame_failed": "抽帧失败（源文件可能损坏/长度为零）",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 纯函数层（无 IO，永远可测）
# ---------------------------------------------------------------------------
def frame_stage(motion: str) -> str:
    """代表帧取自哪一类产物 → 对应审阅阶段：kenburns=image，dubbed/native=clip。

    别名归一走 `project.normalize_motion`（唯一真源，含 video→dubbed 兼容）；
    这也是 `--retake` 该打回哪个阶段的单一真源。kenburns 归 image：片段是本地
    确定性合成，角色长相只可能来自分镜图，判图即判全部。"""
    m = normalize_motion(motion)
    return "image" if m == "kenburns" else "clip"


def retake_stages(motion: str, stage: str | None = None) -> tuple[str, ...]:
    """判漂移时该打回的阶段序列。`stage` 是本次判定所看的渲染物（清单 `stage`），
    缺省按模式推导。

    clip 阶段判漂移 → **连 image 一起打回**：图生视频恒以该镜分镜图作首帧/参考图，
    角色长歪的根因几乎总在分镜图，只重生片段是在同一根因上重试。
    image 阶段判漂移只打回 image：新图落地时存量片段由 lineage 就地置 retake。"""
    st = stage if stage in VISUAL_STAGES else frame_stage(motion)
    return ("image",) if st == "image" else ("clip", "image")


def frame_timestamp(duration) -> float:
    """片段代表帧的时间点：取**中点**（叙事动作展开处，比首帧更能看出角色是否走形）。

    时长不可知/非正数 → 回落 `FALLBACK_TS`；首尾各留 0.05s 避开编码边界。"""
    try:
        d = float(duration)
    except (TypeError, ValueError):
        return FALLBACK_TS
    if d <= 0:
        return FALLBACK_TS
    return round(max(0.05, min(d - 0.05, d / 2.0)), 2)


def _scale_expr(width: int = FRAME_WIDTH) -> str:
    """缩放滤镜：宽度上限 width，**源更小则保持原样**（不放大）。

    表达式里的逗号必须包在单引号内——`-vf` 的裸逗号是滤镜链分隔符，
    不引起来 `min(768,iw)` 会被切成两个滤镜直接报错。"""
    return f"scale='min({width},iw)':-1"


def is_scannable(shot: dict) -> bool:
    """参与扫描的镜：转场镜（零成本本地渲染、无角色）与弃用镜都不算。"""
    return not (tr_mod.is_transition(shot) or review.is_omitted(shot))


def no_compare_reason(project, shot: dict, sheets: list, missing: list) -> str | None:
    """空 sheets 的原因（有 sheets 则返回 None）——**绝不静默**。

    指挥层拿到空清单时必须知道是"作者声明本镜没角色"还是"设定图没生成"，
    前者跳过合规，后者是漏做了一步。"""
    if sheets:
        return None
    if project.skip_design:
        return "skip_design"
    cast = shot.get("characters")
    if isinstance(cast, list) and not cast:
        return "empty_cast"
    if missing:
        return "sheets_missing"
    if not project.characters:
        return "no_cast"
    if isinstance(cast, list) and cast:
        return "cast_unmatched"
    return "no_cast"


# ---------------------------------------------------------------------------
# 配对：本镜代表帧 ↔ 本镜角色设定图
# ---------------------------------------------------------------------------
def shot_sheets(project, shot: dict) -> tuple[list[dict], list[str]]:
    """本镜要比对的角色设定图，返回 (可用清单, 缺文件的角色名)。

    角色集**直接取 `lineage.required_refs` 的 kind=="character"**（与 design_refs
    同源，见模块头口径 1）；路径过 `ensure_local`（口径 2）并绝对化——manifest 要
    被指挥层直接 `Read`，相对路径会随 cwd 漂。"""
    from ..storage.media import ensure_local

    ready: list[dict] = []
    missing: list[str] = []
    for r in lineage.required_refs(project, shot):
        if r.get("kind") != "character":
            continue
        name = r.get("name")
        raw = r.get("path")
        local = ensure_local(raw) if raw else None
        p = Path(local).resolve() if local else None
        if p is not None and p.is_file():
            ready.append({"name": name, "path": str(p)})
        else:
            missing.append(name)
    return ready, missing


def _frame_source(project, shot: dict, aspect: str, stage: str) -> tuple[str | None, str | None]:
    """本镜代表帧的源：kenburns 直接用分镜图（图就是帧，不必抽帧），
    dubbed/native 用图生视频片段。返回 (源路径, 跳过原因)。"""
    if stage == "image":
        src = project.image_for(shot, aspect)
        return (src, None) if src and Path(src).is_file() else (None, "no_image")
    src = project.clip_for(shot, aspect)
    return (src, None) if src and Path(src).is_file() else (None, "no_clip")


def _extract(src: str, out: Path, stage: str, shot: dict) -> None:
    """出帧：图片走缩放拷贝，视频走中点抽帧（失败抛 FFmpegError，由 scan 兜住）。"""
    if stage == "image":
        run(["-i", str(src), "-frames:v", "1", "-vf", _scale_expr(), str(out)],
            desc=f"consistency frame shot {shot.get('id')}")
        return
    try:
        dur = probe_duration(src)
    except FFmpegError:
        dur = shot.get("dur")          # 探不到就用设计时长兜底，绝不因此放弃这一镜
    ts = frame_timestamp(dur)
    run(["-ss", f"{ts:.2f}", "-i", str(src), "-frames:v", "1",
         "-vf", _scale_expr(), str(out)],
        desc=f"consistency frame shot {shot.get('id')}")


# ---------------------------------------------------------------------------
# 产料主入口
# ---------------------------------------------------------------------------
def scan(project, *, only=None, aspect: str | None = None,
         stage: str | None = None) -> dict:
    """产料：逐镜出代表帧 + 配好角色设定图，落 `manifest.json` 并返回该清单。

    **零 API 成本**（纯本地 ffmpeg），**不打分**、**不改任何审阅状态**。
    尚未生成产物的镜只计数跳过，绝不抛错中断整章扫描（口径见模块头）。

    `stage` 显式选源：dubbed/native 章在动态化之前传 `"image"` 判分镜图，
    判定与打回随清单里的 `stage` 走；缺省按模式推导。"""
    ensure_tools()                      # 抽帧前先确认工具在，别扫到一半才炸
    asp = aspect or project.aspect
    if stage is not None and stage not in VISUAL_STAGES:
        raise ProjectError(f"未知阶段: {stage}（可选: {', '.join(VISUAL_STAGES)}）")
    stage = stage or frame_stage(project.motion)
    outdir = project.subdir(SUBDIR)
    want = {x.strip() for x in str(only).split(",") if x.strip()} if only else None

    rows: list[dict] = []
    for s in project.shots:
        if not is_scannable(s):
            continue
        if want is not None and str(s.get("id")) not in want:
            continue
        sheets, missing = shot_sheets(project, s)
        row = {
            "id": s.get("id"),
            "frame": None,
            "characters": [x["name"] for x in sheets],
            "sheets": sheets,
            "missing_sheets": missing,
            "reason": no_compare_reason(project, s, sheets, missing),
            "skipped": None,
            "verdict": (s.get("consistency") or {}).get("verdict"),
        }
        src, skip = _frame_source(project, s, asp, stage)
        if skip:
            row["skipped"] = skip
        else:
            out = outdir / f"shot_{s.get('id')}.png"
            try:
                _extract(src, out, stage, s)
                row["frame"] = str(out.resolve())
            except FFmpegError:
                row["skipped"] = "frame_failed"
        rows.append(row)

    ready = [r for r in rows if r["frame"] and r["sheets"]]
    manifest = {
        "at": _now(),
        "chapter": project.id,
        "motion": project.motion,
        "stage": stage,
        "aspect": asp,
        "dir": str(outdir.resolve()),
        "shots": rows,
        "summary": {
            "shots": len(rows),
            "ready": len(ready),
            # 四个计数**互斥且加起来等于 shots**——被 skip 的镜即便也带 reason
            # 也只算 skipped（report_lines 对 skipped 行 `continue`，压根不打印它的
            # reason）。不排他会出现「可比对 0 · 无可比对角色 2 · 跳过 4」这种
            # 2+4>4 的自相矛盾汇总，还会让 CLI 打出「原因见上」而上面一行原因都没有。
            "no_compare": sum(1 for r in rows if r["reason"] and not r["skipped"]),
            "skipped": sum(1 for r in rows if r["skipped"]),
        },
        "howto": [
            "引擎只产料不打分：逐镜 Read frame 与 sheets 里的角色设定图，人眼/多模态比对"
            "五官、发型、服装配色、体型与标志性配件是否同一个人。",
            "判完回填：python3 -m kinema consistency set --chapter <项目/章节> "
            "--shot <镜号> --verdict ok|drift [--score 0~1] [--note 哪里不一致] [--retake]",
            "--retake 只对未锁定镜生效（已通过 done 的镜只留判定当标记，机器不代人解锁）。",
        ],
    }
    (outdir / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_manifest(project) -> dict | None:
    """读回上次产料清单（`consistency set` 用它给判定挂上产料存证）；没有返回 None。"""
    f = project.workdir / SUBDIR / MANIFEST
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  清单坏了不该拦住人工判定回填
        return None


def manifest_row(manifest: dict | None, shot_id) -> dict | None:
    """从清单里取某镜那一行。"""
    for r in ((manifest or {}).get("shots") or []):
        if str(r.get("id")) == str(shot_id):
            return r
    return None


def report_lines(manifest: dict) -> list[str]:
    """产料结果的逐行报告——**"无可比对角色"必须显式成行**（见模块头口径 3）。"""
    out: list[str] = []
    st = manifest.get("stage")
    src_zh = "分镜图缩放" if st == "image" else "片段中点抽帧"
    out.append(f"代表帧来源: {manifest.get('motion')} → {st}（{src_zh}）"
               f" · 比例 {manifest.get('aspect')} · 目录 {manifest.get('dir')}")
    for r in manifest.get("shots") or []:
        head = f"  镜{str(r.get('id')):>3}"
        if r.get("skipped"):
            out.append(f"{head}  ⏭ 跳过：{SKIPS.get(r['skipped'], r['skipped'])}")
            continue
        names = "、".join(r.get("characters") or [])
        if r.get("reason"):
            out.append(f"{head}  ⚠ 本镜无可比对角色（原因：{REASONS.get(r['reason'], r['reason'])}）")
        else:
            out.append(f"{head}  帧 {Path(r['frame']).name}  ↔ 角色设定图 "
                       f"{len(r.get('sheets') or [])} 张（{names}）"
                       + (f"  [上次判定 {r['verdict']}]" if r.get("verdict") else ""))
        if r.get("missing_sheets"):
            out.append(f"       ⚠ 缺设定图：{'、'.join(r['missing_sheets'])}"
                       "——这些角色本镜无从比对，先跑 project refs")
    s = manifest.get("summary") or {}
    out.append(f"共 {s.get('shots', 0)} 镜 · 可比对 {s.get('ready', 0)}"
               f" · 无可比对角色 {s.get('no_compare', 0)} · 跳过 {s.get('skipped', 0)}")
    return out


# ---------------------------------------------------------------------------
# 回填：指挥层判定 → shots[].consistency（+ 可选打回重做）
# ---------------------------------------------------------------------------
def _clean_score(score) -> float:
    """把 `--score` 归一到 [0,1] 的有限数。

    **落盘目的地是 project.json，而 `NaN`/`Infinity` 不在 JSON 规范里**：
    `json.dump` 默认照吐，Python 自己读得回来，但浏览器 `JSON.parse` 直接抛——
    Studio 章节页会整页加载失败。与 `mediacheck.loudness_i` 的有限性守卫同源纪律：
    **结论块里只准出现有限数**。分数是人给的主观标注，越界一律钳到边界而非报错。"""
    try:
        v = float(score)
    except (TypeError, ValueError):
        raise ProjectError(f"--score 必须是 0~1 的数字，收到: {score!r}") from None
    if not math.isfinite(v):
        raise ProjectError(f"--score 必须是有限数（不接受 nan/inf），收到: {score!r}")
    return round(min(1.0, max(0.0, v)), 3)


def set_verdict(project, shot: dict, verdict: str, *, score=None, note: str | None = None,
                by: str = DEFAULT_BY, retake: bool = False) -> dict:
    """把指挥层的判定写进 `shots[].consistency`，返回 {"entry", "retaken", "locked"}。

    `retake=True` 且判 drift 时按 `retake_stages` 打回（未锁定置 retake，已锁定只留
    判定当标记）。调用方负责 `project.save()`。"""
    if verdict not in VERDICTS:
        raise ProjectError(f"未知判定: {verdict}（可选: {', '.join(VERDICTS)}）")
    entry: dict = {"verdict": verdict, "at": _now(), "by": by}
    if score is not None:
        entry["score"] = _clean_score(score)
    if note:
        entry["note"] = note
    manifest = load_manifest(project)
    row = manifest_row(manifest, shot.get("id"))
    judged = (manifest or {}).get("stage") if row else None
    if row:                              # 挂上产料存证：判的是哪一帧、比的哪几张设定图
        if row.get("frame"):
            entry["frame"] = row["frame"]
        if row.get("sheets"):
            entry["sheets"] = [x.get("path") for x in row["sheets"] if x.get("path")]
    entry["stage"] = judged if judged in VISUAL_STAGES else frame_stage(project.motion)
    shot["consistency"] = entry

    retaken: list[str] = []
    locked: list[str] = []
    if retake and verdict == "drift":
        msg = "角色跨镜一致性判定为漂移" + (f"（{note}）" if note else "") \
              + "——请按角色设定图重生成"
        for st in retake_stages(project.motion, entry["stage"]):
            if review.is_locked(shot, st):
                locked.append(st)        # 锁是人给的，机器不越权解锁（同 lineage.mark_stale）
                continue
            review.set_state(shot, st, "retake", note=msg)
            retaken.append(st)
    return {"entry": entry, "retaken": retaken, "locked": locked}


# ---------------------------------------------------------------------------
# 失效：渲染物一被替换 → 旧判定当场作废
# ---------------------------------------------------------------------------
def invalidate(shot: dict, stage: str) -> dict | None:
    """把该镜的旧一致性判定整条清掉（渲染物换了，判定就不再成立），返回被清掉的那条。

    与 `lineage.clear_stale` 同一条纪律——**新产物落地 = 旧标记失效**。不清会烂两处：
      ① Studio 分镜卡只看 `consistency.verdict`（`app.js` 的角标），会在一张全新的、
         还没人判过的图上继续挂「⚠ 角色漂移」，人工点 done 之后甚至与「✓ 已通过」
         同时出现——判定信号从此不可信（要么被无视，要么让人把做完的活再返一遍）；
      ② `entry.frame` 存证只在那一版渲染物下成立：产料帧名固定 `shot_<id>.png`，
         下一次 `scan` 就地覆盖，留着的路径会指向另一张图，是主动错误的溯源。

    **凡是替换渲染物的门都必须调用**（新开门也要照办，否则同一 bug 换个入口复活）：
    `cli.stage_gen_image`（含宫格候选）/ `cli.stage_gen_video` 重生、`supply` 素材直供、
    `refine.refine_shot_image` 局部改造、`candidates.pick` 宫格换选、版本回滚
    （`cli.cmd_versions_rollback` + `studio.actions.rollback_version` 两处入口）。
    `stage` 不在 `VISUAL_STAGES`（如 audio）时是空操作——重跑配音不改画面。

    并发安全：`consistency` 已登记进 `project._SHOT_HUMAN_KEYS`，这里的 pop 走三方合并
    ——本次长任务运行期间人工新落的判定仍以磁盘为准，不会被引擎的旧内存副本抹掉。
    """
    if stage not in VISUAL_STAGES:
        return None
    return shot.pop("consistency", None)
