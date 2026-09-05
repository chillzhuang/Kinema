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

"""源片同区间音轨作成片配乐（章级 `control_bgm`）。

控制段与成片在时间上 1:1（`bind` 把镜的 `dur` 对齐到段长），所以绑定区间
`[start, start+seconds)` 在源片里对应的那段音轨铺到该镜在时间轴上的位置，就与
舞步逐拍对上。这条路不经过模型：发给模型的是控制段的无声副本（`bind.send_path`），
模型的原生音是生成的、不会原样复现参考音频，音乐由合成段确定性铺回——与字幕后置
合成是同一个道理。

产物就是章节的 BGM 文件（`audio/bgm.*`），随后走标准 BGM 母线：入轨归一、母线电平
与末级归一都不另写一份。没绑控制视频的镜留静音。
"""
from __future__ import annotations

from pathlib import Path

from ..ffmpeg import run
from ..pipeline import mixdown, transitions
from . import assets as assets_mod


def bed_segments(project) -> list[tuple[float, Path, float, float, float]]:
    """时间轴上要铺的源片段：`[(偏移秒, 源片副本, 起点秒, 段长秒, 成片滞后秒)]`。

    只收有归一副本且源片带音轨的绑定镜。偏移取 `Project.timeline`：成片按 concat
    顺排，那就是合成用的同一条时间轴。滞后取 `gen.control.sync`（`sync.measure_sync`
    量出并判定够格的那份），没量或不够格即 0。
    """
    out = []
    for at, _end, s in project.timeline():
        seg = _segment_of(project, s)
        if seg is not None:
            out.append((float(at), *seg))
    return out


def _segment_of(project, shot: dict) -> tuple[Path, float, float, float] | None:
    if transitions.is_transition(shot):
        return None
    rec = (shot.get("gen") or {}).get("control") or {}
    aid = str(rec.get("asset") or "")
    if not aid:
        return None
    meta = assets_mod.read_asset(project, aid) or {}
    src = assets_mod.asset_dir(project, aid) / assets_mod.OUTPUTS["source"]
    if not src.is_file() or not (meta.get("source") or {}).get("audio"):
        return None
    sync = rec.get("sync") or {}
    lag = float(sync.get("lag") or 0.0) if sync.get("applied") else 0.0
    return src, float(rec.get("start") or 0.0), float(rec["seconds"]), lag


def cut_start(start: float, lag: float) -> float:
    """配乐在源片里的起点。成片比控制段晚 `lag` 秒，音乐就从早 `lag` 秒处起铺，
    拍点才落在成片的动作上；起点早不过源片开头，超出的偏移只能放弃。"""
    return max(0.0, start - lag)


def bed_signature(segments) -> str:
    """段落表的指纹：重框区间、换素材、补镜改了偏移、重量了对拍，都是另一段音乐。"""
    return "|".join(f"{at:.3f}:{src.parent.name}:{start:.3f}:{sec:.3f}:{lag:+.3f}"
                    for at, src, start, sec, lag in segments)


def build_bed(project, out: str | Path) -> dict:
    """拼一条与时间轴等长的配乐轨写到 `out`。返回 `{"segments", "seconds", "sig"}`。

    按时间轴逐镜 concat：绑定镜取源片段（起点按对拍偏移平移，见 `cut_start`），其余镜
    填等长静音，每一片都钳到该镜的 `dur`——段落只要与镜等长，顺排下来偏移自然对上，
    不必逐段 `adelay` 再混。
    先拼出原始音轨再做响度归一：`loudnorm` 的积分响度自带门限，静音段不拉低测量值；
    增益链取 BGM 入轨那一套（`bgm_gain_db` + `master_filter`），与曲库曲目落在同一
    响度床上。
    """
    total = project.total_duration()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".raw.wav")
    args: list[str] = []
    chains: list[str] = []
    segs = []
    for k, (at, _end, s) in enumerate(project.timeline()):
        dur = float(s.get("dur") or 0)
        seg = _segment_of(project, s)
        if seg is None:
            args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo"]
        else:
            src, start, seconds, lag = seg
            segs.append((float(at), src, start, seconds, lag))
            args += ["-ss", f"{cut_start(start, lag):.3f}", "-t", f"{seconds:.3f}",
                     "-i", str(src)]
        chains.append(f"[{k}:a]aresample=44100,aformat=channel_layouts=stereo,"
                      f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[p{k}]")
    n = len(chains)
    if not n:
        run(["-f", "lavfi", "-t", f"{max(total, 0.1):.3f}", "-i",
             "anullsrc=r=44100:cl=stereo", str(raw)], desc="配乐轨 · 静音")
    else:
        labels = "".join(f"[p{k}]" for k in range(n))
        run([*args, "-filter_complex",
             ";".join(chains) + f";{labels}concat=n={n}:v=0:a=1[a]",
             "-map", "[a]", "-ar", "44100", "-ac", "2", str(raw)],
            desc="配乐轨 · 源片段顺排")
    gain = mixdown.bgm_gain_db(
        mixdown.measure_loudness(mixdown.measure_file_args(["-i", str(raw)])))
    run(["-i", str(raw), "-af", mixdown.master_filter(gain, limit=mixdown.BGM_LIMIT_PEAK),
         "-ar", "44100", "-ac", "2", str(out)], desc="配乐轨 · 响度归一")
    raw.unlink(missing_ok=True)
    return {"segments": len(segs), "seconds": total, "sig": bed_signature(segs)}
