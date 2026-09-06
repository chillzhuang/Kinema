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

"""镜级绑定 —— 把素材的某一段裁成这一镜的控制视频并写进契约。

契约只有两处：`shots[].control` 存裁好那一段的路径（扁平串，与 `shots[].previz`
同形），元数据全在 `shots[].gen.control`。扁平是有理由的：`agent_gateway` 的引用
摘要扫描会递归收集列出键下的每一个字符串，嵌套字典会把素材 id、起点与时间戳
一并当成「参考物」摘要，从此每镜恒报引用漂移。

**绝不写 `shots[].clip`**——compose 视 `clip` 为最终成片素材直接播放，控制视频是
黑底灰度浮雕，写进去就是把它当成片交付了。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import previz, review, voicecast
from ..errors import ProjectError
from ..ffmpeg import run
from ..pipeline import transitions
from ..pipeline.checkpoint import has_file
from . import assets as assets_mod
from .io import fit_filter, probe_audio

FITS = ("pad", "crop")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _find_shot(project, shot_no) -> dict:
    for x in project.data.get("shots") or []:
        if str(x.get("id")) == str(shot_no):
            return x
    raise ProjectError(f"找不到镜 {shot_no}")


# ---------------------------------------------------------------- 只读谓词
def control_shot(shot: dict) -> bool:
    """本镜是否**有可发的**控制视频（字段在 + 文件/URL 真的在 + 仲裁判给了 control）。

    单一真源：`cli` 的逐镜 V2V 分支与 `pipeline.framechain` 的孤岛判据共用。
    仲裁是文档级判据属于这里；native 与 provider 能力位是运行时的，由调用方合成
    总闸后传入。绑定本身就是发送的表态。
    """
    from ..sketchboard import active_guide
    from ..storage.media import is_url
    p = shot.get("control")
    if not p or active_guide(shot) != "control":
        return False
    return bool(is_url(p) or has_file(p))


def control_seconds(shot: dict) -> float:
    """本镜控制视频的段长（秒）——V2V 输入侧的计费口径。

    只读绑定时落下的 `gen.control.seconds`，**不 probe 文件**：事前闸与 dry-run
    会对全片调它，每镜探一次是白等，而这个数在裁段那一刻就已经定死。
    """
    rec = (shot.get("gen") or {}).get("control") or {}
    try:
        return float(rec.get("seconds") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def control_drift(shot: dict, current_build: str | None) -> str | None:
    """已绑的这一段是否与素材现状脱节；返回原因短语，无漂移返回 None。

    两种漂移各有各的因，且都不该由内容哈希去发现（那一层看的是段落文件本身，
    而段落文件在这两种情形里一个字节都没变）：

    · **素材重建**——`control build` 重跑只改 `assets/<id>/`，已裁出的段落
      仍是旧那一版的像素；
    · **镜时长改了**——`dur` 是作者字段，改完段长就不再与请求的画面秒数 1:1，
      而 1:1 正是运动不被拉伸或截断的前提。
    """
    rec = (shot.get("gen") or {}).get("control") or {}
    if not rec:
        return None
    build = rec.get("build")
    if build and current_build and build != current_build:
        return "素材已重建"
    want = rec.get("dur_at")
    now = shot.get("dur")
    if want is not None and now is not None and float(want) != float(now):
        return f"镜时长已从 {want}s 改成 {now}s"
    return None


def request_seconds(project, shot: dict) -> tuple[float, int]:
    """本镜的 `(净画面秒数, 控制段应有的秒数)`——控制段必须与生成的片段等长。

    口径与 previz 逐字同源：`voicecast.request_seconds` 取净画面秒数，再过
    `previz.snap_duration` 的 4~15 档位钳（那道钳与真 provider 的
    `billable_seconds` 由 `tests/test_previz.py` 逐值对拍）。**绝不另铸一份钳位**。
    原始秒数一并返回，供绑定期判定这一镜是否根本超出参考视频的可用带宽。
    """
    adir = project.subdir("audio")
    dur = voicecast.request_seconds(shot, project.motion, adir=adir) \
        or float(project.data.get("duration", 5)) or 5
    return dur, previz.snap_duration(dur)


# ---------------------------------------------------------------- 裁段
def cut_segment(src: Path, dst: Path, *, start: float, seconds: int,
                fit: str, canvas: tuple[int, int], fps: int) -> None:
    """从素材里裁出一段并贴合画布。**重编码不 `-c copy`**——按关键帧取整会让
    起点漂到最近的 I 帧上，而选段的意义正是起点精确。

    **贴合画布是承重的不是修饰**：Seedance 在 `ratio_mode: adaptive` 的别名上遇到
    参考视频会发 `ratio="adaptive"`，成片就跟着参考视频的几何走而不是章节画布——
    段落先贴合，比例才不会被带跑。贴合滤镜与对照片共用 `io.fit_filter`。

    **段落带源片同区间的音轨，发出去的是它的无声副本**（`send_path`）。盘上这一份是
    审看件：这一段起没起在拍点上、骨骼跟没跟上节奏，光看黑底浮雕判不出来，Studio 与
    `@视频1` 点开的就是它。而本章是 native（声音由模型生成），把实拍源片的背景音一并发
    过去，赌的是模型不拿它做文章——发送副本只保留视频流，不重编码。
    """
    w, h = canvas
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(["-ss", f"{start:.3f}", "-t", f"{seconds:d}", "-i", str(src),
         "-map", "0:v:0", "-map", "0:a?", "-vf", fit_filter(fit, w, h), "-r", str(fps),
         "-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", str(dst)],
        desc=f"裁控制段 {dst.name}")
    mute = _mute_path(dst)
    if probe_audio(dst):
        run(["-i", str(dst), "-map", "0:v:0", "-an", "-c:v", "copy",
             "-movflags", "+faststart", str(mute)], desc=f"无声发送副本 {mute.name}")
    else:
        # 换绑到没有音轨的素材时，上一条素材留下的副本不能比段落活得久：
        # 发送按副本优先，留着它发出去的就是另一段运动
        mute.unlink(missing_ok=True)


def _mute_path(control: Path) -> Path:
    return control.with_name(f"{control.stem}_mute{control.suffix}")


def send_path(control: str) -> str:
    """发给模型的那一份：控制段的无声副本；没有副本的段落本身就是无声的（源片没有
    音轨），原样发。URL 形式的段落（`oss sync` 过的章节）由上传层透传，这里不碰。"""
    p = Path(control)
    if not p.is_file():
        return control
    mute = _mute_path(p)
    return str(mute) if mute.is_file() else control


# ---------------------------------------------------------------- 绑定 / 摘除
def _segment_seconds(project, shot: dict, start: float, end: float | None) -> int:
    """这一镜要裁多长。返回整秒段长，必要时把 `dur` 对齐过去。

    两种来源，优先级明确：

    · **给了 `end`** —— 区间是人在缩略条上框的，它说了算。段长随之，并写回 `dur`：
      控制段与成片 1:1 是运动不被拉伸的前提，两个数字不许各说各的。落在 4~15 之外
      直接拒绝而不是钳进去——静默钳位就是拿 15 秒的运动去演 20 秒的镜。
    · **没给** —— 段长由这一镜自己的请求秒数定（`request_seconds`），沿用旧口径。
    """
    raw, want = request_seconds(project, shot)
    if end is None:
        if round(raw) > previz.SNAP_MAX_SEC:
            raise ProjectError(
                f"镜 {shot['id']} 请求 {round(raw)}s，而参考视频最长 {previz.SNAP_MAX_SEC}s——"
                f"1:1 复刻只能在 {previz.SNAP_MIN_SEC}~{previz.SNAP_MAX_SEC}s 内成立。"
                f"把这一镜拆成两镜，或在绑定时框一段更短的区间")
        return want

    span = float(end) - float(start)
    seconds = round(span)
    if not previz.SNAP_MIN_SEC <= seconds <= previz.SNAP_MAX_SEC:
        raise ProjectError(
            f"选中区间 {span:.1f}s 不可用——参考视频只收 "
            f"{previz.SNAP_MIN_SEC}~{previz.SNAP_MAX_SEC} 秒，长的拆成两镜、短的往外拉")
    if seconds != want:
        shot["dur"] = seconds
        _, now = request_seconds(project, shot)
        if now != seconds:
            raise ProjectError(
                f"镜 {shot['id']} 的配音需要 {now}s，窗口缩不到 {seconds}s——"
                f"这一段的运动与台词长度对不上，改台词或改区间")
    return seconds


def bind_preflight(project, shot_no, asset_id: str | None = None, *,
                   replace_previz: bool = False, whole_shot: bool = False) -> dict:
    """这一镜此刻能不能接受一条控制视频；不能就抛错，能就返回镜。

    与 `bind_shot` 共用这一份判据，也是 `control build --bind-shot` 在处理源片
    **之前**过的闸：这几条都不依赖素材内容，处理跑完几分钟后才发现镜不能绑，
    等于让人白等一趟再重传。

    `asset_id` 是随后要绑的那条素材：build 显式 `--asset <既有 id>` 就地重建时传它，
    绑着同一条素材的镜是回来改区间、照常放行；不传表示素材尚未生成、id 待派，
    任何既有绑定都算「绑着别的素材」。`whole_shot` 表示随后的绑定不框区间、按
    整镜长度裁（`--bind-shot` 的自动绑），此时镜长必须落在参考视频带宽内。

    片段已通过（`done`）**不在此列**：绑定是人对这一镜运动源的直接决定，片段
    随之作废（`review.retake_by_decision`），锁不豁免。"""
    s = _find_shot(project, shot_no)
    if transitions.is_transition(s):
        raise ProjectError("转场镜由合成段本地渲染，不接受控制视频绑定")
    if review.is_omitted(s):
        raise ProjectError(f"镜 {shot_no} 已弃用(omt)——先恢复再绑定控制视频")
    if s.get("previz") and not replace_previz:
        raise ProjectError(
            f"镜 {shot_no} 已有 3D 预演，一镜只发一条参考视频。"
            f"要改用控制视频加 --replace-previz（会清除预演登记，文件保留）")
    # 同一条素材重绑是改区间（常做的事），换一条素材则是换运动源——后者要先解绑。
    # 直接顶掉的话，这一镜的段落文件被就地重写，而「它演的是哪条素材」只在
    # `gen.control` 里悄悄换了个 id，回头没人能从成片上看出运动源什么时候变了
    held = ((s.get("gen") or {}).get("control") or {}).get("asset")
    if held and held != asset_id:
        raise ProjectError(
            f"镜 {shot_no} 已绑素材 {held}——一镜只收一条控制视频，先解绑再改绑"
            + (f" {asset_id}" if asset_id else ""))
    if whole_shot:
        # 段长由这一镜自己定：超出参考视频带宽的镜在处理之前就该被拦下
        _segment_seconds(project, s, 0.0, None)
    return s


def bind_shot(project, shot_no, asset_id: str, *, start: float = 0.0,
              end: float | None = None, fit: str = "pad",
              replace_previz: bool = False, store=None) -> dict:
    """把素材 `asset_id` 的 `[start, end)` 一段绑定到某镜。写盘一次。

    `end` 省略时段长由这一镜的请求秒数定；给了则以区间为准（见 `_segment_seconds`）。
    """
    if fit not in FITS:
        raise ProjectError(f"未知贴合方式 {fit}（可选 {'/'.join(FITS)}）")
    rec = assets_mod.read_asset(project, asset_id)
    if not rec:
        raise ProjectError(f"找不到素材 {asset_id}——先 `control build`，或 `control list` 看现有素材")
    if rec.get("status") != "done":
        raise ProjectError(f"素材 {asset_id} 尚未处理完（当前 {rec.get('status')}）")

    # 镜态闸在裁段之前：硬拦时工作目录里不留半成品
    s = bind_preflight(project, shot_no, asset_id, replace_previz=replace_previz)

    # 参考视频的服务端上限恒是 15 秒，**与别名的 `max_duration` 无关**（2.5 允许
    # 30 秒输出，参考视频仍只收 15）
    seconds = _segment_seconds(project, s, start, end)
    total = float((rec.get("source") or {}).get("seconds") or 0.0)
    if start < 0 or start + seconds > total + 1e-3:
        raise ProjectError(
            f"素材 {asset_id} 只有 {total:.1f}s，装不下从 {start:.1f}s 起的 {seconds}s 段——"
            f"起点最大 {max(0.0, total - seconds):.1f}s")

    src = assets_mod.asset_dir(project, asset_id) / assets_mod.OUTPUTS["control"]
    if not src.is_file():
        raise ProjectError(f"素材 {asset_id} 的 control.mp4 不在盘——重跑 `control build`")

    if replace_previz and s.get("previz"):
        previz.clear_previz(project, shot_no)
        s = _find_shot(project, shot_no)

    fps = store.fps if store is not None else int(project.data.get("fps") or 30)
    canvas = store.canvas(project.aspect) if store is not None else (1080, 1920)
    dst = assets_mod.cut_path(project, s["id"])
    cut_segment(src, dst, start=start, seconds=seconds, fit=fit, canvas=canvas, fps=fps)

    s["control"] = str(dst)
    gen = s.setdefault("gen", {})
    prev = (gen.get("control") or {}).get("version") or 0
    gen["control"] = {
        "provider": "control", "version": prev + 1, "cost": 0.0,
        "asset": asset_id, "start": round(float(start), 3), "seconds": seconds,
        "end": round(float(start) + seconds, 3), "fit": fit, "at": _now(),
        # 漂移基线：素材那一版的内容指纹 + 绑定时这一镜的时长
        "build": assets_mod.build_digest(project, asset_id),
        "dur_at": s.get("dur"),
        "people": rec.get("people"), "fps": (rec.get("source") or {}).get("fps"),
    }
    # 换了运动源，已产出的片段就不再是这一版的产物。不置 retake 的话，盘上有片段的
    # 镜会被 gen-video 当作已完成直接跳过，绑好的控制视频一帧也发不出去。
    # 锁定的片段同样作废：绑定是人对这一镜的直接决定，不是引擎自行重生。
    unlocked = review.is_locked(s, "clip")
    retake = review.retake_by_decision(s, "clip")
    # 对照片是照旧区间拼的，区间一改它就在说另一段的事。删掉即可，按需重建。
    _drop_compare(project, s["id"])
    project.save()
    return {"shot": s["id"], "control": s["control"], "asset": asset_id,
            "start": round(float(start), 3), "end": gen["control"]["end"],
            "seconds": seconds, "dur": s.get("dur"), "fit": fit,
            "retake": retake, "unlocked": unlocked and retake == "retake"}


def _drop_compare(project, shot_id) -> None:
    """清掉该镜按需生成的两档对照片。"""
    for tiles in (2, 3):
        assets_mod.shot_compare_path(project, shot_id, tiles=tiles).unlink(missing_ok=True)


def unbind_shot(project, shot_no) -> dict:
    """摘除绑定。**不删文件**——重绑常见，留着段落省一次重编码。

    与绑定同一条规则：运动源变了，已产出的片段作废，锁定不豁免。两边若不同规
    （摘除放行、绑定被锁拒），文档说没绑、锁定的片段却是按控制视频生成的，而唯一
    能回到一致的动作又被同一把锁拒绝。"""
    s = _find_shot(project, shot_no)
    dropped = [k for k in ("control",) if s.pop(k, None)]
    if (s.get("gen") or {}).pop("control", None):
        dropped.append("gen.control")
    retake = None
    if dropped:
        retake = review.retake_by_decision(s, "clip")
        _drop_compare(project, s["id"])
        project.save()
    return {"shot": s["id"], "dropped": dropped, "retake": retake}


def bound_shots(project, asset_id: str) -> list:
    """仍绑着该素材的镜号。"""
    return [s.get("id") for s in project.data.get("shots") or []
            if isinstance(s, dict)
            and ((s.get("gen") or {}).get("control") or {}).get("asset") == asset_id]


def delete_asset(project, asset_id: str) -> dict:
    """删素材目录。仍有镜绑着就拒——那些镜的段落还指着它的内容。"""
    import shutil
    bound = bound_shots(project, asset_id)
    if bound:
        # 这条几乎总是在网页上被看到（终端里删素材是少数），故不点名 CLI 动词——
        # 页面上按不了 `control unbind`，说「先执行解绑操作」才对得上眼前的按钮
        raise ProjectError(
            f"素材 {asset_id} 仍绑在镜 {'、'.join(str(x) for x in bound)} 上——先执行解绑操作")
    d = assets_mod.asset_dir(project, asset_id)
    if not d.is_dir():
        raise ProjectError(f"找不到素材 {asset_id}")
    shutil.rmtree(d)
    return {"asset": asset_id}
