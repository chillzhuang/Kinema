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

"""对照片 —— 把同一段时间的几路画面横排到一条视频里审看。

三个规格，同一件事：

    素材级（二合一）  左 源片    | 右 控制视频          全长，build 收尾时出
    镜级（二合一）    左 源片段  | 右 控制段            绑定区间，按需出
    镜级（三合一）    左 源片段  | 中 控制段 | 右 成片段  绑定区间，按需出

镜级两档按成片有没有出决定，共用同一套几何——出片前后看到的两格是同一个东西，
只是多了一格。

**每格取素材原片的画幅，不是章节画布。** 发出去的那段控制视频是按画布贴合过的
（`ratio_mode: adaptive` 的别名会跟着参考视频的几何走，那层贴合是承重的），但它只是
投递格式：一段 9:16 的实拍装进 16:9 画布后，68% 的像素是补出来的黑边。审看时把黑边
一并摞进来，看到的就是几格黑底中间几条细人影。成片那一路按章节画布出，与素材画幅
不一致时 pad 装进同一格——各格等大才比得出运动。crop 贴合的镜是例外：发出去的段
只保留画布比例的中央区域，前两格套同一条裁切、画幅取裁好那段的几何，否则对照片里
看到的是模型没收到的画面。

**摞的方向随之按素材画幅的长边定**：竖片横着排、横片竖着摞。16:9 的两格并排是 32:9、
三格是 16:3，而灯箱的播放位定宽——画幅越扁它给得出的高度越少，每格反而比换个方向摞
时小一半。

**音轨恒取源片那一路**。控制段是哑的（它要发给视频模型，而 native 章的声音由模型
生成，把实拍背景音一并发过去是拿账单赌模型不拿它做文章），成片段则常常本来就没有
声音。审看要听的也正是源片：骨骼跟没跟上拍子、这一段是不是起在那个拍点上，都得
听着原始节奏判。要听模型出的声音走灯箱的「只看成片段」。

**对照片永远不进请求也不进成片**。发给视频模型的恒是 `control.mp4` 裁出的纯控制段，
烧进放映的恒是 `shots[].clip`——横排画面进了任何一条，出来的就是一张分屏成片。
`shots[].control` 因此仍指纯控制段，对照片只经 Studio 的预览字段下发。

镜级各路必须来自**同一区间**：源片段与控制段按 `gen.control` 的 `start`/`seconds`
现裁，成片段本就是照那个区间生成的。区间对不齐的对照片会让人把「模型没跟住运动」
和「我裁错了段」看成同一件事。
"""
from __future__ import annotations

from pathlib import Path

from ..errors import ProjectError
from ..ffmpeg import run
from . import assets as assets_mod
from . import io as io_mod
from .params import STACK_TILE


def build_asset_compare(project, asset_id: str) -> Path:
    """素材的二合一对照（左源片、右控制视频），全长。返回产物路径。"""
    adir = assets_mod.asset_dir(project, asset_id)
    src = adir / assets_mod.OUTPUTS["source"]
    ctl = adir / assets_mod.OUTPUTS["control"]
    dst = adir / assets_mod.OUTPUTS["compare"]
    if not src.is_file() or not ctl.is_file():
        raise ProjectError(f"素材 {asset_id} 的源片或控制视频不在盘——重跑 `control build`")
    # 音轨取源片那一路：审看时听得到原始节奏，才判得出骨骼跟没跟上拍子
    spec = io_mod.probe_source(src)
    full = spec["seconds"]
    run(io_mod.stack_args(dst, [(src, 0, full), (ctl, 0, full)],
                          canvas=(spec["width"], spec["height"]),
                          tile=STACK_TILE, fps=spec["fps"], audio_from=0),
        desc=f"对照片 {asset_id}")
    return dst


def build_shot_compare(project, shot: dict) -> Path:
    """某镜的对照片。有成片段就出三合一，没有就出二合一。返回产物路径。"""
    rec = (shot.get("gen") or {}).get("control") or {}
    ctl = shot.get("control")
    clip = shot.get("clip")
    if not ctl or not Path(ctl).is_file():
        raise ProjectError(f"镜 {shot.get('id')} 没有控制段——先 `control bind`")
    clip = str(clip) if clip and Path(str(clip)).is_file() else None

    adir = assets_mod.asset_dir(project, str(rec.get("asset") or ""))
    src = adir / assets_mod.OUTPUTS["source"]
    a_ctl = adir / assets_mod.OUTPUTS["control"]
    if not src.is_file():
        raise ProjectError(f"素材 {rec.get('asset')} 的源片副本不在盘——它可能已被删除")

    # 前两格取**素材原片**而不是发出去的那段控制视频：两者内容逐帧相同，区别只在
    # 后者按章节画布贴合过——pad 补出的那层黑边是投递格式，摞进对照片就是白占地方；
    # crop 贴合则真丢了画面，前两格得按裁好那段的画幅套同一条裁切。
    # 帧率仍以发出去的那一份为准：成片理应贴着它走，帧数也据此守恒。
    start, seconds = float(rec.get("start") or 0), int(rec["seconds"])
    fit = str(rec.get("fit") or "pad")
    sent = io_mod.probe_source(ctl)
    if fit == "crop":
        canvas = (sent["width"], sent["height"])
    else:
        spec = io_mod.probe_source(src)
        canvas = (spec["width"], spec["height"])
    tiles = [(src, start, seconds), (a_ctl, start, seconds)]
    tail = None
    if clip:
        # 成片本身就是那一段，从头取。画幅取向与素材相反（竖拍素材、横屏成片）时
        # 另起一行或一列，不塞进素材格——塞进去只剩中间一条细画面（见 io.stack_args）
        cw, chh = io_mod.probe_dims(clip)
        if (chh > cw) != (canvas[1] > canvas[0]):
            tail = (clip, 0, seconds)
        else:
            tiles.append((clip, 0, seconds))
    n_tiles = len(tiles) + (1 if tail else 0)
    dst = assets_mod.shot_compare_path(project, shot["id"], tiles=n_tiles)
    run(io_mod.stack_args(dst, tiles, canvas=canvas, tile=STACK_TILE, fps=sent["fps"],
                          audio_from=0, fit=fit, tail=tail),
        desc=f"{n_tiles} 合一对照 镜{shot['id']}")
    return dst
