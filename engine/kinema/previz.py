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

"""previz（3D 预演参考片）登记 —— 3D 导演控制台在引擎侧的落点。

**一句话**：把一段 previz mp4 登记成这一镜的生成条件，产物全部落回既有
`shots[]` 契约，**不新增任何并行状态机**：

    previz.mp4 ──┬─ 抽首帧 → shots[].image（走 supply 同一条登记轨：归档旧版 /
                 │            provider=supplied / 版本栈 / 落待审）→ Seedance 首帧
                 ├─ 抽末帧 → shots[].last_frame_ref                → Seedance 末帧
                 ├─ 存片段 → shots[].previz                        → Seedance 参考视频(V2V)
                 └─ 运镜   → shots[].camera(= preset.phrase) + shots[].camera_preset

三条不变量（写错一条就会在成片里才被发现）：

1. **previz 绝不写进 `shots[].clip`**。`compose` 把 `clip` 当最终成片素材直接播
   （`pipeline/compose.fit_clip`），previz 是灰模参考片——写进去等于把没上色的
   预演当成片交付。免费预览另有显式通道（kenburns + `--preview-previz`），
   那是用户主动要的、且只在 kenburns 模式，与本函数无关。
2. **首帧覆盖 `image` 是有条件的**。该镜已有精修图时默认**不覆盖**（`use_first_frame`
   缺省=auto）——3D 灰模首帧盖掉一张已生成的分镜图，是不可逆的体验事故
   （虽有版本栈可回滚，但灰模一旦被当作 Seedance 输入就产生真实成本）。要覆盖须显式指定。
3. **末帧与 `--chain` 争同一个 `last_frame` 槽**，每镜二选一，previz 优先——
   previz 末帧是这一镜自己的终态位姿，比「下一镜的分镜图」更贴近导演编排的意图。
   实现在 `cli.stage_gen_video`，本模块只负责把 `last_frame_ref` 写对。

`director_catalog()` 是控制台角色/动作/道具库的**引擎侧目录**（经 `/api/overview`
下发 `director_catalog`，前端零硬编码）。**默认角色是程序化生成的灰模人偶**
（`studio_app/director/rig.js` 按骨架表建 SkinnedMesh + 关键帧表建 AnimationClip），
不是下载来的 GLB——理由：previz 要求逐字节可复现（外部资产哈希一变、动画就变），
灰模本就该是无身份的体块，且这样零第三方资产许可风险、零二进制入库。目录与前端
注册表的锁步由 `tests/test_previz.py` 直接解析 rig.js 守卫。
"""
from __future__ import annotations

import json
import math
import shutil
from datetime import datetime
from pathlib import Path

from . import review
from .errors import ProjectError
from .pipeline import camera as camera_mod
from .pipeline import consistency, transitions

# previz 产物目录（章节工作目录内，与 images/audio/gen_clips 同级）
PREVIZ_SUBDIR = "previz"
# 参考视频容器（Seedance 只收 mp4/mov）
VIDEO_EXTS = frozenset({".mp4", ".mov"})

# —— Seedance 侧的硬限（超出即被服务端拒绝，本地先告警，别等烧一次建任务才知道）——
REF_MIN_SEC, REF_MAX_SEC = 2.0, 15.0
REF_MAX_MB = 50.0
REF_MIN_FPS, REF_MAX_FPS = 24.0, 60.0
REF_MIN_RATIO, REF_MAX_RATIO = 0.4, 2.5

# previz 渲染时长的钳制区间：与 `SeedanceProvider.billable_seconds` 的 native 口径
# **必须逐字一致**（tests/test_previz.py 用真 provider 对拍）——previz 时长与最终
# Seedance 片长 1:1 是「成片跟随预演」的前提，差一秒就是运动被拉伸/截断。
SNAP_MIN_SEC, SNAP_MAX_SEC = 4, 15


def snap_duration(dur: float) -> int:
    """把任意时长钳成 Seedance 的整秒档位（native 口径：四舍五入 + 4~15 钳制）。"""
    return max(SNAP_MIN_SEC, min(SNAP_MAX_SEC, round(float(dur or 0))))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================================
# 一、体检（零成本本地 ffprobe；判据即 Seedance 的参考视频限额）
# ============================================================================
def inspect_previz(src) -> dict:
    """previz 参考片体检：时长/体积/帧率/宽高比四项 + 可解码性。

    处置沿用**供料体检**（`pipeline/mediacheck.inspect_image`）的既定纪律：
    **只有「ffprobe 解不出/无视频流」硬拦**（那种文件后续 ffmpeg 抽帧必炸），
    其余一律 ⚠ 告警不拦死——2–15s 之类的限额只在真开 V2V 时才生效，而 previz
    同时还承担首/末帧与免费预览两个用途，为一个可能不用的通道拦死另外两个是错的。
    """
    from .ffmpeg import probe_json
    p = Path(src)
    rep: dict = {"at": _now(), "ok": True, "warn": [], "info": [], "hard_fail": []}
    try:
        meta = probe_json(p)
    except Exception as e:  # noqa: BLE001  坏容器统一转成体检结论，绝不冒泡成裸栈
        rep["ok"] = False
        rep["hard_fail"].append({"code": "unreadable", "msg": f"ffprobe 解不出该视频：{e}"})
        return rep
    v = next((s for s in meta.get("streams") or []
              if s.get("codec_type") == "video"), None)
    if v is None:
        rep["ok"] = False
        rep["hard_fail"].append({"code": "no_video", "msg": "文件里没有视频流"})
        return rep
    try:
        dur = float((meta.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if w <= 0 or h <= 0:
        rep["ok"] = False
        rep["hard_fail"].append({"code": "no_size", "msg": "视频流没有有效宽高（0×0）"})
        return rep
    num, _, den = str(v.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = (float(num) / float(den)) if float(den or 0) else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    size_mb = p.stat().st_size / 1048576 if p.is_file() else 0.0
    ratio = w / h
    rep.update(duration=round(dur, 3), width=w, height=h,
               fps=round(fps, 3) if math.isfinite(fps) else None,
               size_mb=round(size_mb, 2), ratio=round(ratio, 3))
    rep["info"].append(f"{w}×{h} · {dur:.2f}s · {fps:.1f}fps · {size_mb:.1f}MB")
    if dur and not (REF_MIN_SEC <= dur <= REF_MAX_SEC):
        rep["warn"].append({"code": "duration",
                            "msg": f"时长 {dur:.2f}s 超出 Seedance 参考视频区间 "
                                   f"{REF_MIN_SEC:g}~{REF_MAX_SEC:g}s——开 V2V 会被拒收"})
    if size_mb > REF_MAX_MB:
        rep["warn"].append({"code": "size",
                            "msg": f"体积 {size_mb:.1f}MB 超过 {REF_MAX_MB:g}MB 上限"
                                   "——开 V2V 会被拒收（调低 previz 分辨率/码率）"})
    if fps and not (REF_MIN_FPS <= fps <= REF_MAX_FPS):
        rep["warn"].append({"code": "fps",
                            "msg": f"帧率 {fps:.1f} 不在 {REF_MIN_FPS:g}~{REF_MAX_FPS:g} 区间"
                                   "——开 V2V 会被拒收"})
    if not (REF_MIN_RATIO <= ratio <= REF_MAX_RATIO):
        rep["warn"].append({"code": "ratio",
                            "msg": f"宽高比 {ratio:.2f} 不在 {REF_MIN_RATIO}~{REF_MAX_RATIO} 区间"})
    return rep


# ============================================================================
# 二、登记（走 supply 同制度：归档 / 待审 / 版本栈 / 一致性判定作废）
# ============================================================================
def previz_dir(project) -> Path:
    return project.subdir(PREVIZ_SUBDIR)


def _find_shot(project, shot_no) -> dict:
    s = next((x for x in project.shots if str(x.get("id")) == str(shot_no)), None)
    if s is None:
        raise ProjectError(f"找不到镜 {shot_no}")
    return s


def register_previz(project, shot_no, src, *, camera_preset=None, aspect=None,
                    use_first_frame=None, store=None, skip_check=False) -> dict:
    """把一段 previz mp4 登记为镜 `shot_no` 的生成条件。

    参数：
      · `camera_preset` —— `pipeline/camera.CAMERA_PRESETS` 的 key；写
        `shots[].camera = preset.phrase`（发给 Seedance 的那一句）+ `camera_preset`（可回读重开）。
        未知 key 直接报错（**不静默忽略**：静默的后果是点了名的预设没进提示词，
        这一镜发出去时一句运镜都没有）。
      · `use_first_frame` —— True 强制用 previz 首帧覆盖 `shots[].image`；
        False 从不覆盖；**None=auto：仅当该镜尚无图时才登记**（见模块头不变量 2）。
      · `aspect` —— 逐比例登记首帧（写 `images{aspect}`）；缺省写主图。
      · `store` —— ConfigStore，供首帧走 supply 时的画布基准（体检用）。

    返回 `{shot, previz, first_frame, last_frame, image_registered, camera, inspect, archived}`。
    """
    s = _find_shot(project, shot_no)
    if transitions.is_transition(s):
        raise ProjectError("转场镜由合成段本地渲染，不接受 previz 登记")
    if review.is_omitted(s):
        raise ProjectError(f"镜 {shot_no} 已弃用(omt)——先恢复再登记 previz")
    if review.is_locked(s, "clip"):
        raise ProjectError(f"镜 {shot_no} 的片段已通过·锁定——previz 会改变下一版请求"
                           "（运镜/末帧/参考片），先 review set --stage clip --state retake")
    preset = None
    if camera_preset:
        preset = camera_mod.get(camera_preset)
        if preset is None:
            raise ProjectError(
                f"未知运镜 preset: {camera_preset}"
                f"（可选 {len(camera_mod.CAMERA_PRESETS)} 个，查看：`kinema previz presets`）")

    src = Path(src).expanduser()
    if not src.is_file():
        raise ProjectError(f"previz 视频不存在: {src}")
    if src.suffix.lower() not in VIDEO_EXTS:
        raise ProjectError(f"不支持的视频格式 {src.suffix}"
                           f"（Seedance 参考视频只收 {', '.join(sorted(VIDEO_EXTS))}）")

    # 体检在**拷贝与抽帧之前**（同 supply 的闸位纪律）：硬拦时工作目录里不留半成品，
    # 这一镜的既有产物一个都没动
    if skip_check:
        inspect = {"at": _now(), "skipped": True, "ok": True}
        print("· 已跳过 previz 体检（--skip-check）")
    else:
        inspect = inspect_previz(src)
        for m in inspect.get("info") or []:
            print(f"· {m}")
        for w in inspect.get("warn") or []:
            print(f"⚠ previz 体检 · {w['msg']}")
        if not inspect.get("ok"):
            raise ProjectError(
                "previz 体检未通过：" + "；".join(f["msg"] for f in inspect["hard_fail"])
                + "（确认文件可用且必须登记，加 --skip-check 跳过体检）")

    pdir = previz_dir(project)
    dst = pdir / f"shot_{s['id']}{src.suffix.lower()}"
    for old in pdir.glob(f"shot_{s['id']}.*"):      # 换容器时清旧扩展名残影
        if old != dst and old.suffix.lower() in VIDEO_EXTS:
            old.unlink()
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)

    from .ffmpeg import first_frame as _ff, last_frame as _lf
    first_png = pdir / f"shot_{s['id']}_first.png"
    last_png = pdir / f"shot_{s['id']}_last.png"
    _ff(dst, first_png)
    _lf(dst, last_png)

    # —— 首帧 → shots[].image（走 supply 那条登记轨，制度不打折）——
    image_registered, archived, skip_reason = False, None, None
    has_image = bool((s.get("images") or {}).get(aspect) if aspect else s.get("image"))
    want_first = bool(use_first_frame) if use_first_frame is not None else (not has_image)
    if want_first:
        if review.is_locked(s, "image"):
            # auto 档遇锁定镜不报错、只说明——「顺手登记首帧」失败不该让整条 previz 登记失败
            skip_reason = "该镜分镜图已通过·锁定（要用 previz 首帧覆盖请先 review set --state retake）"
            if use_first_frame:
                raise ProjectError(f"镜 {shot_no} {skip_reason}")
        else:
            from .supply import supply_image
            # 首帧照走供料体检：previz 渲染分辨率低于画布时这里会告警（"960×540 撑不满
            # 1920×1080 画布"正是手工 previz 最常见的疏漏），比不查有价值
            r = supply_image(project, s["id"], first_png, aspect=aspect,
                             store=store, skip_check=skip_check)
            image_registered, archived = True, r.get("archived")
    elif has_image:
        skip_reason = "该镜已有分镜图，默认不覆盖（要用 previz 首帧加 --use-first-frame）"

    # —— 末帧 / 参考片 / 运镜 ——
    s["last_frame_ref"] = str(last_png)
    s["previz"] = str(dst)
    if preset:
        s["camera"] = preset["phrase"]
        s["camera_preset"] = preset["key"]
    gen = s.setdefault("gen", {})
    prev = (gen.get("previz") or {}).get("version") or 0
    gen["previz"] = {"provider": "previz", "source": str(src), "cost": 0.0,
                     "version": prev + 1, "at": _now(),
                     "duration": inspect.get("duration"),
                     "fps": inspect.get("fps"),
                     "camera_preset": preset["key"] if preset else None,
                     "first_frame": str(first_png), "last_frame": str(last_png),
                     "inspect": inspect}
    project.save()
    if skip_reason:
        print(f"· 未登记首帧：{skip_reason}")
    return {"shot": s["id"], "previz": str(dst), "first_frame": str(first_png),
            "last_frame": str(last_png), "image_registered": image_registered,
            "archived": archived, "camera": s.get("camera"),
            "camera_preset": s.get("camera_preset"), "inspect": inspect,
            "skip_reason": skip_reason}


def clear_previz(project, shot_no) -> dict:
    """摘掉某镜的 previz 挂载（`previz`/`last_frame_ref`/`camera_preset`）。

    **只摘挂载、不删文件**（产物留在 `previz/` 目录里，随时可重新登记），也
    **不动 `shots[].image`**——首帧一旦登记就是这一镜正儿八经的分镜图，走版本栈
    回滚才是它的退出路径，从这里悄悄删掉会让该镜突然变成无图。
    """
    s = _find_shot(project, shot_no)
    dropped = [k for k in ("previz", "last_frame_ref", "camera_preset") if s.pop(k, None)]
    (s.get("gen") or {}).pop("previz", None)
    project.save()
    return {"shot": s["id"], "dropped": dropped}


def frames_dir(project, shot_no) -> Path:
    """控制台逐帧上传的落点：`<work>/previz/_frames/shot_<id>/`。

    刻意放在 `previz/` 之内而非另起顶层目录——`previz/` 已在媒体路径守卫内，
    且清理时随章节工作目录一起走。下划线前缀标记「中间产物」，`build_from_frames`
    编完即删。
    """
    d = previz_dir(project) / "_frames" / f"shot_{shot_no}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_from_frames(project, shot_no, *, fps: int = 24, keep_frames: bool = False,
                      **register_kw) -> dict:
    """把控制台上传的 PNG 序列编成 previz mp4 并登记（传输 A 的引擎收尾）。

    **确定性的最后一环**：控制台用 `mixer.setTime(i/fps)` 逐帧渲染（绝对定位，
    不受墙钟影响），这里用 `-framerate F ... -r F -frames:v N` 锁 CFR——两端都不
    依赖实时性，同一个场景在任何机器上渲出的 mp4 逐帧一致。**刻意不用
    MediaRecorder/captureStream**：那是墙钟实时录屏，掉帧且无法保证精确 fps/时长，
    previz 与最终 Seedance 片长对不齐，「成片跟随预演」这条承诺就断了。
    """
    from .ffmpeg import run
    fdir = frames_dir(project, shot_no)
    frames = sorted(fdir.glob("f*.png"))
    if not frames:
        raise ProjectError(
            f"镜 {shot_no} 没有已上传的 previz 帧（{fdir}）——"
            "请在 3D 导演控制台点「渲染 previz」，帧会逐张上传到这里")
    fps = max(1, int(fps or 24))
    staging = previz_dir(project) / "_incoming"
    staging.mkdir(parents=True, exist_ok=True)
    out = staging / f"shot_{shot_no}.mp4"
    run(["-framerate", str(fps), "-i", str(fdir / "f%05d.png"),
         "-r", str(fps), "-frames:v", str(len(frames)),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        desc="encode previz frames")
    try:
        r = register_previz(project, shot_no, out, **register_kw)
    finally:
        out.unlink(missing_ok=True)
        if not keep_frames:
            shutil.rmtree(fdir, ignore_errors=True)
    r["frames"] = len(frames)
    r["fps"] = fps
    return r


# ---- 场景编排文档（控制台可重开继续排戏） ----
_SCENE_KEYS = ("fps", "actors", "paths", "props", "cameras", "cuts")


def scene_hash(scene: dict) -> str:
    """场景内容哈希（`sha256:<hex16>`，与血缘指纹同格式）。

    用途是**按内容缓存 previz**：编排没变就不必重渲一遍（与 compose 按源指纹
    失效同构）。只哈希编排字段本身，不含 `updated_at` 之类的时间戳——否则每次
    保存哈希都变，缓存等于没有。
    """
    import hashlib
    payload = {k: scene.get(k) for k in _SCENE_KEYS}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def save_scene(project, scene: dict) -> dict:
    """把控制台的 3D 场景编排快照写进章节文档顶层 `previz`（engine-managed）。

    **整体替换而非合并**：场景是一个自洽的图（actor 引用 path、cut 引用 camera），
    字段级合并会造出"引用了已删除机位的镜头块"这种半坏状态。控制台每次保存都
    发全量，与 `workspace.set_graph` 的既定纪律一致。
    """
    if not isinstance(scene, dict):
        raise ProjectError("previz 场景必须是一个对象")
    doc = {k: scene.get(k) for k in _SCENE_KEYS if scene.get(k) is not None}
    doc.setdefault("fps", 24)
    doc["scene_hash"] = scene_hash(doc)
    doc["updated_at"] = _now()
    project.data["previz"] = doc
    project.save()
    return doc


def v2v_shot(shot: dict) -> bool:
    """本镜是否**有可发的** previz 参考片（字段在 + 文件/URL 真的在 + 本镜的
    运动预演仲裁没有落到简笔板）。

    单一真源：`cli` 的逐镜 V2V 分支与 `pipeline.framechain` 的孤岛判据共用——
    写两份的结局是链态判定与真发分叉。guide 仲裁是文档级判据，属于这里；
    总开关（`--previz` / `previz_v2v`）与 provider 能力位是运行时的，由调用方
    合成总闸后传入。
    """
    from .pipeline.checkpoint import has_file
    from .sketchboard import active_guide
    from .storage.media import is_url
    p = shot.get("previz")
    if not p or active_guide(shot) == "sketch":
        return False
    return bool(is_url(p) or has_file(p))


def previz_seconds(shot: dict) -> float:
    """该镜 previz 的时长（秒）——V2V 成本估算的输入侧口径。

    优先读登记时落下的 `gen.previz.duration`（零成本），缺失才 probe 文件。
    事前闸/dry-run 会对全片调它，每镜都 probe 一次是白等。
    """
    d = ((shot.get("gen") or {}).get("previz") or {}).get("duration")
    try:
        if d and float(d) > 0:
            return float(d)
    except (TypeError, ValueError):
        pass
    p = shot.get("previz")
    if not p or not Path(str(p)).is_file():
        return 0.0
    try:
        from .ffmpeg import probe_duration
        return float(probe_duration(p))
    except Exception:  # noqa: BLE001  探测失败不该让报价崩掉，按 0 计（宁可少估也不炸）
        return 0.0


# ============================================================================
# 三、全片预演（reel）—— 把逐镜 previz 串成一条长片，供人从头看一遍
# ============================================================================
# 逐镜产物只能逐个点开，看不出整场戏连起来的节奏（上一镜的收势接不接得住下一镜
# 的起势）。reel 按契约顺序把它们拼成一条 mp4，可直接播放/下载。
#
# 三条边界：
# 1. **reel 不是成片，也不喂模型**。它不进 `shots[].clip`（那是成片素材位），
#    不进 `output`（那是交付位），不作 V2V 参考（V2V 是**逐镜**发本镜那一段）。
# 2. **指针不进契约**。章节文档顶层 `previz` 是编排快照的整体替换区——
#    `save_scene` 只保留 `_SCENE_KEYS`，指针写进去下一次保存编排就没了。故清单
#    落 sidecar、存在性由磁盘推导（与「章节状态由产物动态推导不落盘」同纪律）。
# 3. **落点仍在 `<work>/previz/`**。片库只扫 `*_work/output/*.mp4`，放这儿不会
#    被当成成片收录；`collect_media` 只走契约字符串，不进契约也就不会被传上 OSS。
REEL_NAME = "reel.mp4"
REEL_MANIFEST = "reel.json"


def reel_path(project) -> Path:
    return previz_dir(project) / REEL_NAME


def reel_info(project) -> dict | None:
    """已合的全片预演清单（读 sidecar，零探测）。没合过或产物被删则 None。"""
    out, man = reel_path(project), previz_dir(project) / REEL_MANIFEST
    if not out.is_file() or not man.is_file():
        return None
    try:
        d = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    d["file"] = str(out.resolve())        # sidecar 可能是从别处拷来的，以磁盘为准
    d["size"] = out.stat().st_size
    return d


def reel_inputs(project) -> tuple[list[dict], list[dict]]:
    """按契约顺序挑出可入片的镜 → (可用清单, 跳过清单含原因)。

    路径过 `ensure_local`：OSS 模式下 `shots[].previz` 是 URL，不本地化就只会
    得到「一镜都没有」——而盘上每一镜都渲过（同 `pipeline/consistency.py` 的 `ensure_local` 教训）。
    """
    from .storage.media import ensure_local

    ready: list[dict] = []
    skipped: list[dict] = []
    for s in project.shots:
        sid = s.get("id")
        if review.is_omitted(s):
            skipped.append({"id": sid, "why": "omt"})
            continue
        if transitions.is_transition(s):
            # 转场镜由合成段本地渲染，本就没有 previz——它不是「漏渲了」
            skipped.append({"id": sid, "why": "transition"})
            continue
        raw = s.get("previz")
        local = ensure_local(raw) if raw else None
        p = Path(local).resolve() if local else None
        if p is None or not p.is_file():
            skipped.append({"id": sid, "why": "no_previz"})
            continue
        ready.append({"id": sid, "path": str(p),
                      "seconds": round(previz_seconds(s), 3)})
    return ready, skipped


def _video_spec(path: str) -> tuple:
    """(codec, 宽, 高, 像素格式, 帧率串) —— 判断能否直接流拷贝的全部依据。"""
    from .ffmpeg import probe_json
    v = next((x for x in (probe_json(path).get("streams") or [])
              if x.get("codec_type") == "video"), None) or {}
    return (v.get("codec_name"), int(v.get("width") or 0), int(v.get("height") or 0),
            v.get("pix_fmt"), str(v.get("avg_frame_rate") or ""))


def build_reel(project, *, fps: int | None = None) -> dict:
    """把各镜 previz 拼成 `<work>/previz/reel.mp4` + `reel.json` 清单。

    **同参数同源必得同输出**：控制台渲的 previz 全是同一套编码参数（同 canvas、
    同 fps、libx264/yuv420p），故默认走 **concat demuxer 流拷贝**——零重编码、
    零画质损失、快。只有掺进了外部登记的片子（`previz register --file`，参数各异）
    才回退重编码归一到首镜规格；混着来时流拷贝会得到花屏或时长错乱的文件，所以
    判据必须逐项比齐（codec/宽/高/像素格式/帧率），差一项就整体走重编码。

    **音轨一律丢弃**（`-an`）：previz 是无声灰模参考，而外部片子可能带音轨，
    有的有有的没有会让 concat 直接失败。
    """
    from .ffmpeg import concat_entry, ensure_tools, probe_duration, run
    ensure_tools()
    rows, skipped = reel_inputs(project)
    if not rows:
        raise ProjectError(
            "本章还没有任何镜渲出 previz，合不出全片预演——"
            "先在 3D 导演控制台逐镜「⏺ 渲染 previz」（或 `previz register --file`）")
    outdir = previz_dir(project)
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / REEL_NAME

    specs = [_video_spec(r["path"]) for r in rows]
    uniform = len(set(specs)) == 1
    # 归一目标取**众数规格**而不是「第一镜」：本仓库实测就混着一条早期 Retina 未锁
    # pixelRatio 渲出的 4K 遗留片，它一旦排在首位就会把整条 reel 拖成 4K（还连带
    # 把其余镜放大重编码）。少数派服从多数派，平票才回落出现顺序。
    order: list[tuple] = []
    tally: dict[tuple, int] = {}
    for sp in specs:
        k = (sp[1], sp[2], sp[4])
        if k not in tally:
            order.append(k)
        tally[k] = tally.get(k, 0) + 1
    w, h, rate_s = max(order, key=lambda k: (tally[k], -order.index(k)))
    if fps:
        rate = max(1, int(fps))
    else:
        num, _, den = rate_s.partition("/")
        try:
            rate = max(1, round(float(num) / float(den))) if float(den or 0) else 24
        except (TypeError, ValueError, ZeroDivisionError):
            rate = 24

    if uniform:
        lst = outdir / "_reel_concat.txt"
        lst.write_text("".join(concat_entry(r['path']) for r in rows),
                       encoding="utf-8")
        try:
            run(["-f", "concat", "-safe", "0", "-i", str(lst),
                 "-c:v", "copy", "-an", "-movflags", "+faststart", str(out)],
                desc="concat previz reel")
        finally:
            lst.unlink(missing_ok=True)
    else:
        args: list[str] = []
        for r in rows:
            args += ["-i", r["path"]]
        chain = ";".join(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate}[v{i}]"
            for i in range(len(rows)))
        graph = (chain + ";" + "".join(f"[v{i}]" for i in range(len(rows)))
                 + f"concat=n={len(rows)}:v=1:a=0[out]")
        run([*args, "-filter_complex", graph, "-map", "[out]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(out)],
            desc="concat previz reel (re-encode)")

    try:
        dur = round(float(probe_duration(out)), 3)
    except Exception:  # noqa: BLE001  探不出时长不该让已经合好的片子作废
        dur = round(sum(r["seconds"] for r in rows), 3)
    man = {
        "at": _now(), "chapter": project.id,
        "mode": "copy" if uniform else "reencode",
        "width": w, "height": h, "fps": rate,
        "duration": dur, "shots": rows, "skipped": skipped,
    }
    (outdir / REEL_MANIFEST).write_text(
        json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    man["file"] = str(out.resolve())
    man["size"] = out.stat().st_size
    return man


# ============================================================================
# 四、导演目录（角色 / 动作 / 道具原语）—— 前端零硬编码的真源
# ============================================================================
# 默认角色：程序化灰模人偶（前端 director/rig.js 按同名 key 建 SkinnedMesh）。
# `height` 是站立身高（米，与 camera.py 的「1.7 单位人 / 看点 1.5」坐标约定同尺）。
DIRECTOR_MODELS: tuple[dict, ...] = (
    {"key": "mannequin_m", "label": "男性人偶", "height": 1.78, "build": "male",
     "desc": "标准男性体块，肩宽腰窄——主角/成年男性角色的默认替身"},
    {"key": "mannequin_f", "label": "女性人偶", "height": 1.66, "build": "female",
     "desc": "标准女性体块，肩窄髋宽——成年女性角色的默认替身"},
    {"key": "mannequin_n", "label": "中性人偶", "height": 1.72, "build": "neutral",
     "desc": "无性别特征体块——群演/未定角色/机器人的通用替身"},
    {"key": "mannequin_c", "label": "儿童人偶", "height": 1.24, "build": "child",
     "desc": "儿童比例（头身比更大）——小孩角色，也用来给场景标尺度"},
)

# 动作片段：`loop` 决定是否循环播放；`speed` 是该动作的**内建位移速度**（米/秒，
# 0=原地），控制台按「路线长度 ÷ 时长」求实际地速后用 `timeScale = 地速/speed`
# 做步态同步——不同步就会脚滑（脚在原地蹭而人在飘）。
DIRECTOR_ACTIONS: tuple[dict, ...] = (
    {"key": "idle", "label": "待机", "loop": True, "speed": 0.0,
     "desc": "站立轻微呼吸摆动——没有指定动作时的缺省"},
    {"key": "walk", "label": "行走", "loop": True, "speed": 1.35,
     "desc": "常速步行，双臂交替摆动"},
    {"key": "run", "label": "奔跑", "loop": True, "speed": 4.2,
     "desc": "奔跑，步幅大、前倾、有腾空相"},
    {"key": "jump", "label": "跳跃", "loop": False, "speed": 0.0,
     "desc": "原地起跳-腾空-落地缓冲（一次性）"},
    {"key": "crawl", "label": "爬行", "loop": True, "speed": 0.55,
     "desc": "四肢着地低姿爬行——潜行/受伤/穿越低矮空间"},
    {"key": "prone", "label": "趴下", "loop": True, "speed": 0.0,
     "desc": "俯卧不动，仅呼吸起伏——隐蔽/倒地"},
    {"key": "fly", "label": "飞行", "loop": True, "speed": 3.0,
     "desc": "离地悬浮前进，身体前倾、双臂后掠——飞/御剑/浮空"},
    {"key": "sit", "label": "坐下", "loop": True, "speed": 0.0,
     "desc": "坐姿（对坐戏/餐桌/办公）"},
    {"key": "turn", "label": "转身", "loop": False, "speed": 0.0,
     "desc": "原地转身 180°（一次性）——最常见的「回头」表演"},
    {"key": "wave", "label": "挥手", "loop": False, "speed": 0.0,
     "desc": "抬臂挥手（一次性）——打招呼/示意"},
    {"key": "fall", "label": "倒下", "loop": False, "speed": 0.0,
     "desc": "失衡后仰倒地（一次性）——受击/力竭"},
    {"key": "attack", "label": "出招", "loop": False, "speed": 0.0,
     "desc": "上身发力挥击（一次性）——战斗节拍"},
    {"key": "crouch", "label": "蹲行", "loop": True, "speed": 0.9,
     "desc": "屈膝低姿移动——掩体间转移/潜近目标（配走位路线）"},
    {"key": "dodge", "label": "闪避", "loop": False, "speed": 0.0,
     "desc": "侧身急闪压低重心（一次性）——躲攻击/躲障碍物"},
    {"key": "cover", "label": "进掩体", "loop": True, "speed": 0.0,
     "desc": "深蹲贴掩体隐蔽，间或探头张望——配「掩体」道具"},
    {"key": "enter", "label": "上车", "loop": False, "speed": 0.0,
     "desc": "抬腿跨入→弯身坐落，结束保持坐姿（一次性）——上车/跨入舱门"},
    {"key": "ride", "label": "骑乘", "loop": True, "speed": 0.0,
     "desc": "分腿骑姿、双手前伸握缰——与「马」「车体」道具叠放成骑乘"},
)

# 道具场景族：控制台按它分组陈列。分组只影响检索路径，不影响体块本身——
# 一堵墙在城镇戏与室内戏里是同一块几何。
DIRECTOR_PROP_GROUPS: tuple[dict, ...] = (
    {"key": "basic", "label": "通用体块", "desc": "占位、挡镜、给运镜提供视差与遮挡"},
    {"key": "indoor", "label": "室内陈设", "desc": "对坐、办公、卧居的家具与坐点"},
    {"key": "town", "label": "城镇建筑", "desc": "街巷、城防与过场建筑的体量"},
    {"key": "ancient", "label": "古风建筑", "desc": "东方古建与仪式性构筑"},
    {"key": "nature", "label": "自然地貌", "desc": "外景植被、地形与生物"},
    {"key": "modern", "label": "现代器物", "desc": "街道设施、载具与工业构件"},
)

# 道具原语：只提供**体块**，用来占位、挡镜、给运镜提供视差参照与遮挡关系。
# previz 不做材质/光影/细节——身份与质感全部交给 AI 生成阶段与设定图。
# `group` 必须命中 DIRECTOR_PROP_GROUPS 的某个 key（守卫 test_catalog_shape_and_json_safe）。
DIRECTOR_PROPS: tuple[dict, ...] = (
    {"key": "box", "label": "箱体", "group": "basic", "size": [0.8, 0.8, 0.8],
     "desc": "通用方块——桌台/货箱/台阶的占位"},
    {"key": "pillar", "label": "立柱", "group": "basic", "size": [0.45, 3.2, 0.45],
     "desc": "柱子——前景擦镜的经典遮挡物"},
    {"key": "wall", "label": "墙面", "group": "basic", "size": [4.0, 2.8, 0.2],
     "desc": "墙/隔断——切空间、挡视线、做单点透视走廊"},
    {"key": "door", "label": "门框", "group": "basic", "size": [1.0, 2.1, 0.15],
     "desc": "门框——进出场调度与画框内画框"},
    {"key": "stairs", "label": "台阶", "group": "basic", "size": [1.2, 1.5, 2.25],
     "desc": "五级台阶——高差调度与上下场"},
    {"key": "arch", "label": "拱门", "group": "basic", "size": [1.9, 2.9, 0.3],
     "desc": "拱门——入口调度与画框中画框"},
    {"key": "table", "label": "长桌", "group": "indoor", "size": [1.8, 0.75, 0.8],
     "desc": "桌——对坐戏、会议、餐桌调度"},
    {"key": "chair", "label": "椅子", "group": "indoor", "size": [0.55, 1.05, 0.55],
     "desc": "单椅——对话/办公/审讯戏的坐点"},
    {"key": "bed", "label": "床", "group": "indoor", "size": [1.5, 0.9, 2.1],
     "desc": "床——卧室戏与躺姿承托"},
    {"key": "sofa", "label": "沙发", "group": "indoor", "size": [1.8, 0.9, 0.85],
     "desc": "沙发——客厅对话戏的坐点（配「坐下」自动落座）"},
    {"key": "bench", "label": "长凳", "group": "indoor", "size": [1.6, 0.5, 0.42],
     "desc": "长凳——公园/走廊/等候戏（配「坐下」自动落座）"},
    {"key": "shelf", "label": "书架", "group": "indoor", "size": [1.2, 1.9, 0.35],
     "desc": "多层书架——书房/档案室的背景墙与纵深分层"},
    {"key": "house", "label": "民居", "group": "town", "size": [3.6, 3.5, 4.2],
     "desc": "坡顶小屋——村落/街巷的基本单元，可并排铺出一条街"},
    {"key": "rampart", "label": "城墙", "group": "town", "size": [6.2, 4.4, 1.5],
     "desc": "带垛口的高墙段——城防戏、守城视角与巨大尺度参照"},
    {"key": "tower", "label": "角楼", "group": "town", "size": [2.9, 6.7, 2.9],
     "desc": "收分方塔——城墙转角、瞭望与制高点"},
    {"key": "gate", "label": "城门", "group": "town", "size": [5.4, 5.4, 2.0],
     "desc": "门洞＋城楼——进出城调度与穿门运镜的画框"},
    {"key": "bridge", "label": "石桥", "group": "town", "size": [3.0, 2.0, 7.0],
     "desc": "拱墩石桥——过场、对峙与俯拍纵深"},
    {"key": "well", "label": "水井", "group": "town", "size": [1.5, 2.3, 1.5],
     "desc": "井台＋井架——村口聚集点与近景遮挡"},
    {"key": "pagoda", "label": "楼阁塔", "group": "ancient", "size": [3.1, 8.0, 3.1],
     "desc": "多层收分塔——东方天际线与远景地标"},
    {"key": "stele", "label": "石碑", "group": "ancient", "size": [1.1, 2.6, 0.7],
     "desc": "碑座＋碑身——遗迹、墓地与信息点"},
    {"key": "lantern", "label": "石灯", "group": "ancient", "size": [0.7, 2.2, 0.7],
     "desc": "石灯笼——参道两侧的节奏点与夜戏光源位置"},
    {"key": "altar", "label": "祭台", "group": "ancient", "size": [2.4, 0.96, 2.4],
     "desc": "多级圆台——仪式中心、打坐处（配「坐下」自动落座）"},
    {"key": "campfire", "label": "篝火", "group": "ancient", "size": [0.8, 0.35, 0.8],
     "desc": "篝火——夜戏光源位置与围坐调度"},
    {"key": "tree", "label": "树", "group": "nature", "size": [1.6, 4.0, 1.6],
     "desc": "树冠+树干——外景视差与前景遮挡"},
    {"key": "rock", "label": "岩块", "group": "nature", "size": [1.2, 0.9, 1.1],
     "desc": "不规则岩块——外景地形与掩体"},
    {"key": "bush", "label": "灌木", "group": "nature", "size": [1.4, 1.1, 1.4],
     "desc": "低矮团丛——地面层次与蹲姿掩蔽"},
    {"key": "bamboo", "label": "竹丛", "group": "nature", "size": [1.1, 4.6, 1.1],
     "desc": "细高竹竿——竹林戏的竖向切分与穿行遮挡"},
    {"key": "cliff", "label": "岩台", "group": "nature", "size": [4.0, 2.7, 3.2],
     "desc": "阶梯状岩体——高差、崖边对峙与仰拍底座"},
    {"key": "log", "label": "倒木", "group": "nature", "size": [0.9, 0.6, 3.4],
     "desc": "横躺原木——林间障碍与坐点（配「坐下」自动落座）"},
    {"key": "horse", "label": "马", "group": "nature", "size": [0.6, 1.95, 2.2],
     "desc": "马体块（躯干/颈/头/四腿）——骑乘戏与尺度参照"},
    {"key": "vehicle", "label": "车体", "group": "modern", "size": [1.9, 1.5, 4.4],
     "desc": "车——街道调度与横移跟拍的速度参照"},
    {"key": "barrier", "label": "掩体", "group": "modern", "size": [1.7, 1.13, 0.36],
     "desc": "腰高矮墙——枪战/追逐的隐蔽点，配「进掩体」动作"},
    {"key": "lamp", "label": "灯柱", "group": "modern", "size": [0.4, 3.1, 0.4],
     "desc": "路灯柱——街景纵深与光源位置示意"},
    {"key": "fence", "label": "栅栏", "group": "modern", "size": [2.4, 0.95, 0.1],
     "desc": "栅栏——边界示意与前景遮挡"},
    {"key": "sign", "label": "路牌", "group": "modern", "size": [0.7, 2.2, 0.2],
     "desc": "路牌——街景信息点与停留理由"},
    {"key": "container", "label": "集装箱", "group": "modern", "size": [2.5, 2.6, 6.1],
     "desc": "货柜——码头/仓库/废土的模块化遮挡，可堆叠"},
)


def director_catalog() -> dict:
    """控制台资产目录（角色/动作/道具/运镜）——经 `/api/overview` 下发。

    运镜库刻意**不并进这里**（另发 `camera_catalog`）：它是 `shots[].camera` 的
    措辞真源、CLI 与 Skill 层也要用，与「3D 舞台上摆什么」不是一件事。
    """
    return {
        "models": [dict(m) for m in DIRECTOR_MODELS],
        "actions": [dict(a) for a in DIRECTOR_ACTIONS],
        "props": [dict(p) for p in DIRECTOR_PROPS],
        "prop_groups": [dict(g) for g in DIRECTOR_PROP_GROUPS],
        "limits": {"min_sec": SNAP_MIN_SEC, "max_sec": SNAP_MAX_SEC,
                   "ref_min_sec": REF_MIN_SEC, "ref_max_sec": REF_MAX_SEC,
                   "ref_max_mb": REF_MAX_MB},
    }
