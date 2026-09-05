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

"""两遍流水线的编排。

    [归一] 源片 → assets/<id>/source.mp4（定帧率；之后一切以它为准）
    [pass 1] 工作分辨率逐帧：分割 → 姿态 → 跟踪 → 每人裁切复判 → 深度
    [时序]   整段在内存里稳定遮罩、骨骼与深度区间
    [pass 2] 再解一遍全分辨率帧当引导图，流式渲染并写进编码进程

第二遍宁可重解一次也不缓存全分辨率帧：1080×1920 的 288 帧就是 1.8 GB，
而 ffmpeg 解一帧只要几毫秒。

**全程不 load/save 章节文档**——进度只写自己的 sidecar。这是多条素材并行处理
不会与绑定、编排保存互相丢更新的全部理由。
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..errors import KinemaError
from ..ffmpeg import ensure_tools, run
from ..locking import FileLock
from . import assets as assets_mod
from . import io as io_mod
from . import models as models_mod
from . import render as render_mod
from . import temporal
from .geometry import crop_square, kp_bbox, paste_square, square_box, union_box
from .params import (DEPTH_SIZE, KPT_THR, MAX_SOURCE_SEC, MIN_JOINTS,
                     MIN_KPT_MEAN, WORK_SHORT)
from .track import Tracker

# 进度回写节流：每多少帧刷一次 sidecar。太密会让 Studio 的扫描器一直读到
# 正在改写的文件，太疏则页面上的进度条像卡住。
_PROGRESS_EVERY = 24


def _work_size(w: int, h: int, short: int) -> tuple[int, int]:
    """按短边缩到工作分辨率，两边取偶（x264 要求）。"""
    scale = short / min(w, h)
    return int(round(w * scale / 2)) * 2, int(round(h * scale / 2)) * 2


def _pass1(bundle, src, w, h, fps, nframes, report):
    """逐帧推理。返回 `(逐帧 track, 深度栈, 裁切框栈, 遮罩栈)`。"""
    tracker = Tracker()
    per_frame: list[list] = []
    depths = np.empty((nframes, DEPTH_SIZE, DEPTH_SIZE), np.float32)
    boxes = np.zeros((nframes, 3), np.int32)
    masks = np.zeros((nframes, h, w), bool)
    import cv2
    n = 0
    for i, rgb in enumerate(io_mod.decode_frames(src, w, h)):
        if i >= nframes:
            break
        mask = bundle.segment(rgb, ts_ms=int(i * 1000 / fps))

        kps, scores = bundle.pose(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        people = []
        for kp, sc in zip(kps, scores):
            kp = kp.astype(np.float32).copy()
            keep = sc >= KPT_THR
            kp[~keep] = np.nan
            # 关节够数**且整体够硬**：背景里的车、反光、衣物褶皱能凑够 6 个刚过
            # 门槛的关节，逐条都合格而整体很虚——那条轨迹在成片里就是一条乱挥的肢体
            if keep.sum() >= MIN_JOINTS and float(sc[keep].mean()) >= MIN_KPT_MEAN:
                people.append(kp)
        per_frame.append(list(zip(tracker.update(people), people)))

        # 每人再裁切分割一次：分割模型按近景半身训练，整帧远景会很粗；裁切等于把
        # 宽景变回它的训练域。**框内用精细结果替换而不是并集**——整帧那张遮罩是
        # 256² 铺满全画幅（一个遮罩像素管到 2×4 个源像素），并集只会往外加面积、
        # 永远减不掉，精细轮廓当场被吹胖成一团。框外仍留粗遮罩：姿态没检到的人
        # 还得靠它兜住。
        if people:
            fine = np.zeros((h, w), np.uint8)
            covered = np.zeros((h, w), np.uint8)
            ones = None
            for kp in people:
                b = kp_bbox(kp, expand=1.3)
                box = square_box(b[0], b[1], b[2], b[3], w, h, margin=0.05)
                crop = np.ascontiguousarray(crop_square(rgb, box))
                paste_square(fine, bundle.segment(crop).astype(np.uint8), box)
                if ones is None or ones.shape[0] != box[2]:
                    ones = np.ones((box[2], box[2]), np.uint8)
                paste_square(covered, ones, box)
            mask = np.where(covered > 0, fine > 0, mask)
        masks[i] = mask

        box = union_box(mask, people, w, h)
        boxes[i] = box
        depths[i] = bundle.depth(crop_square(rgb, box))
        n = i + 1
        if n % _PROGRESS_EVERY == 0 or n == nframes:
            report(1, n, nframes)
    if n != nframes:
        raise KinemaError(f"解码只拿到 {n} 帧，源片报 {nframes} 帧——源片可能已损坏")
    return per_frame, depths, boxes, masks


def _pass2(src, out_dir, tracks, depths, boxes, lo, hi, masks_s, *,
           full, work, fps, nframes, has_audio, styled, report):
    """全分辨率渲染并写入编码进程。"""
    (fw, fh), (ww, wh) = full, work
    sx, sy = fw / ww, fh / wh
    stick = render_mod.stick_width(fw, fh)
    names = ["control"] + (["styled"] if styled else [])
    enc = {k: io_mod.Encoder(out_dir / assets_mod.OUTPUTS[k], src, fw, fh, fps, has_audio)
           for k in names}
    stride = render_mod.strip_stride(fps)
    sheet, strip = None, []
    prev = None
    try:
        for i, rgb in enumerate(io_mod.decode_frames(src, fw, fh)):
            if i >= nframes:
                break
            alpha = render_mod.alpha_from_mask(masks_s[i], rgb, fw, fh)
            d = np.clip((depths[i] - lo[i]) / (hi[i] - lo[i]), 0, 1)
            canvas, prev = render_mod.refine_depth(d, boxes[i], alpha, fw, fh, sx, sy, prev)
            depth01 = canvas * alpha

            kps_full = []
            for arr in tracks.values():
                kp = arr[i].copy()
                if np.isnan(kp[:, 0]).all():
                    continue
                kp[:, 0] *= sx
                kp[:, 1] *= sy
                kps_full.append(kp)
            pose_rgb = render_mod.skeleton(kps_full, fw, fh, stick, stick)
            control = render_mod.control_frame(depth01, pose_rgb)
            enc["control"].write(control)
            styled_rgb = None
            if styled:
                styled_rgb = render_mod.styled_frame(canvas, alpha, kps_full,
                                                     fw, fh, stick, stick)
                enc["styled"].write(styled_rgb)

            if i % stride == 0:
                strip.append(control)
            if i == nframes // 3:
                # 取全片三分之一处那一帧：开头常是静止的预备姿势，取中段才看得出
                # 骨骼贴不贴得住动作
                sheet = [rgb.copy(), control.copy()]
            if (i + 1) % _PROGRESS_EVERY == 0 or i + 1 == nframes:
                report(2, i + 1, nframes)
    finally:
        for e in enc.values():
            e.close()
    if sheet:
        render_mod.write_sheet(out_dir / assets_mod.OUTPUTS["sheet"], sheet)
    render_mod.write_strip(out_dir / assets_mod.OUTPUTS["strip"], strip, fps)


def build_asset(project, source, *, asset_id=None, name=None, styled=True,
                mock=False, on_progress=None) -> dict:
    """把一段源片处理成素材目录。返回收尾后的 `asset.json`。"""
    ensure_tools()
    src0 = Path(source)
    if not src0.is_file():
        raise KinemaError(f"源片不在: {src0}")
    if src0.suffix.lower() not in assets_mod.VIDEO_EXTS:
        raise KinemaError(f"只收 {'/'.join(sorted(assets_mod.VIDEO_EXTS))}：{src0.name}")

    # 章级互斥：这条链是 CPU 密集的，两条并行只会互相抢核。Studio 的任务器没有
    # 队列，故锁必须落在跨进程一层——CLI 与 Studio 也因此互相排队。
    lock = FileLock(assets_mod.control_dir(project) / ".build.lock", blocking=False,
                    conflict_msg="本章已有一条深度捕捉在跑——它占满 CPU，请等它结束再来")
    with lock:
        # 准入与建档在 try 之外：这一段抛错时还没有素材可以标失败，
        # 而 sidecar 一旦落盘，之后的任何失败都必须写回终态
        # 没显式点名就取一个没被占用的 id：重传同一个源片得新素材，绝不顶掉旧的
        aid = asset_id or assets_mod.unique_asset_id(project, src0)
        adir = assets_mod.asset_dir(project, aid)
        adir.mkdir(parents=True, exist_ok=True)

        probe = io_mod.probe_source(src0)
        if probe["seconds"] > MAX_SOURCE_SEC:
            raise KinemaError(
                f"源片 {probe['seconds']:.1f}s 超过 {MAX_SOURCE_SEC:.0f}s 上限——"
                f"真正发出去的段只有 4~15s，先把源片裁到需要的那一段再上传"
                f"（处理耗时与时长成正比，长片会撞上任务器的硬超时）")

        rec = assets_mod.new_record(aid, name or src0.name, probe)
        assets_mod.write_asset(project, aid, rec)
        try:
            out = _run_passes(project, aid, adir, rec, src0, probe,
                              styled=styled, mock=mock, on_progress=on_progress)
            # 成功才清上传件：`assets/<id>/source.mp4` 已是归一后的正本，原件再留
            # 就是同一段视频在盘上占两份。失败时留着，重试不必让用户再传一遍。
            clear_incoming(project, src0)
            return out
        except Exception as exc:
            # 中间产物**保留**——排查一条跑挂的素材靠的就是它们；状态落终态，
            # 页面才不会永远转圈
            rec.update(status="failed", error=str(exc)[:400])
            assets_mod.write_asset(project, aid, rec)
            raise


def _run_passes(project, aid, adir, rec, src0, probe, *, styled, mock, on_progress):
    def report(pass_no, done, total):
        rec["progress"] = {"pass": pass_no, "done": done, "total": total}
        assets_mod.write_asset(project, aid, rec)
        if on_progress:
            on_progress(pass_no, done, total)

    # 归一必须在任何逐帧步骤之前：可变帧率会让「一帧进一帧出」在解码端就不成立，
    # 而且之后每个 ffmpeg 子进程的 argv 上都得带 `_work/` 路径，孤儿回收器才认得
    srcn = adir / assets_mod.OUTPUTS["source"]
    run(io_mod.normalise_args(src0, srcn, probe["fps"]), desc=f"归一 {src0.name}")
    nframes = probe["frames"]
    full = (probe["width"], probe["height"])
    work = _work_size(*full, WORK_SHORT)

    timings = {}
    rec["status"] = "analysing"
    assets_mod.write_asset(project, aid, rec)
    bundle = models_mod.load(mock=mock)
    t0 = time.time()
    try:
        per_frame, depths, boxes, masks = _pass1(
            bundle, srcn, work[0], work[1], probe["fps"], nframes, report)
    finally:
        bundle.close()
    timings["pass1"] = round(time.time() - t0, 1)

    rec["status"] = "stabilising"
    assets_mod.write_asset(project, aid, rec)
    t0 = time.time()
    import cv2
    masks_s = temporal.stabilise_mask(masks, per_frame)
    masks_crop = np.stack([
        cv2.resize(crop_square(masks_s[i].astype(np.uint8), tuple(boxes[i])),
                   (DEPTH_SIZE, DEPTH_SIZE), interpolation=cv2.INTER_NEAREST)
        for i in range(nframes)]).astype(bool)
    lo, hi = temporal.depth_window(depths, masks_crop)
    tracks = temporal.stabilise_tracks(per_frame, nframes, probe["fps"])
    timings["stabilise"] = round(time.time() - t0, 1)

    rec["status"] = "rendering"
    assets_mod.write_asset(project, aid, rec)
    t0 = time.time()
    _pass2(srcn, adir, tracks, depths, boxes, lo, hi, masks_s,
           full=full, work=work, fps=probe["fps"], nframes=nframes,
           has_audio=probe["audio"], styled=styled, report=report)
    timings["pass2"] = round(time.time() - t0, 1)

    # 帧数守恒是本特性的硬不变量：控制视频与成片逐帧对齐，差一帧就是运动错位
    out = adir / assets_mod.OUTPUTS["control"]
    from ..ffmpeg import probe_frames
    got = probe_frames(out)
    if got != nframes:
        raise KinemaError(f"控制视频 {got} 帧、源片 {nframes} 帧，帧数不守恒")

    # 二合一对照：帧数核对之后再拼，免得把一条已经错位的控制视频拼成看着挺像的对照片
    rec["status"] = "comparing"
    assets_mod.write_asset(project, aid, rec)
    t0 = time.time()
    from . import compare as compare_mod
    compare_mod.build_asset_compare(project, aid)
    timings["compare"] = round(time.time() - t0, 1)

    rec.update(status="done", people=len(tracks), timings=timings,
               tracks=[{"id": int(t), "frames": int((~np.isnan(a[:, 0, 0])).sum())}
                       for t, a in sorted(tracks.items())],
               outputs={k: v for k, v in assets_mod.OUTPUTS.items()
                        if (adir / v).is_file()})
    rec["progress"] = {"pass": 2, "done": nframes, "total": nframes}
    assets_mod.write_asset(project, aid, rec)
    (adir / assets_mod.CACHE).unlink(missing_ok=True)
    return rec


def clear_incoming(project, path) -> None:
    """上传落点的临时文件收尾。素材已自带归一副本，临时件没有留存价值。

    **两边都要 resolve 再比**：CLI 传进来的可能是相对路径，而 `incoming_dir` 恒是
    绝对路径——直接比 `Path` 对象永远不相等，清理就成了一句永不执行的死代码。
    """
    p = Path(path)
    try:
        inside = p.resolve().parent == assets_mod.incoming_dir(project).resolve()
    except OSError:
        return
    if inside and p.is_file():
        p.unlink(missing_ok=True)
