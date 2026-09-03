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

"""Ken Burns 运镜：把静态分镜帧渲染成带缓慢缩放/平移的竖屏视频片段。

kenburns 档（静图+烧录）用它替代图生视频，零 API 成本。

## 平滑：zoompan 的整数栅格（`SRC_SCALE` 是唯一有效的杠杆）

`zoompan` 的 `x`/`y` 与取景窗**只能落在整数像素上**（表达式结果被截断），而
smoothstep 缓动两端的每帧位移小于 1px——于是头尾若干帧「原地不动」、中段每帧
跳好几像素，观感就是走走停停的抖动（用户实测点名「放大缩小、移动时整体画面
抖得厉害」）。

量化发生在**输入图的坐标系**上，所以成片上的误差 = 1px ÷ 下采样比
（取景窗宽 ÷ 输出宽）——把输入放得越大，同样的 1px 抖动在成片上越细。
五方案真机 A/B（5s 右移，逐帧差的突变均值 = 抖动度）：

| 方案 | 抖动度 | 静止帧 | 渲染 |
|---|---|---|---|
| 输入 2×（旧） | 1.071 | 20/149 | 1.5s |
| **输入 4×（现行）** | **0.574** | 10/149 | 1.8s |
| 输入 6× | 0.426 | 7/149 | 3.1s |
| 2× + 混合缓动 | 1.408 | 8/149 | 1.6s |
| 4× + 混合缓动 | 0.711 | 0/149 | 1.8s |

结论：**4× 是拐点**——抖动减半而渲染只多两成；6× 再降 26% 但耗时翻倍，不值。
**缓动两端加线性分量反而更差**（消灭了静止帧，却让步长跳变更频繁），故保留
纯 smoothstep。输出端超采样（zoompan 出 2× 再缩回）实测无效——量化在 zoompan
**内部**发生，输出端再怎么缩也追不回已经丢掉的精度（1.071→1.176，反而略差）。

八种运镜按镜号轮换（去"纯静态图滑动"感的专业口径，零成本）：
· **全部缓动化**（smoothstep 缓入缓出）——线性匀速是"机械感"的头号来源，
  纪录片级 Ken Burns 的关键正是 easing（xfade-easing/Bannerbear 等权威实践一致）；
· 在推/拉/左移/右移四经典之上新增：**对角推近/对角拉远**（zoom 与 x/y 同时动，
  画面产生纵深斜移）、**微旋推近**（叠加 ±0.9° 极缓旋转，稳定的"活"感）、
  **呼吸镜**（正弦微变焦，适合情绪/凝视镜头）；
· 运镜幅度保持缓慢克制（快速运动放大素材缺陷，slow/smooth 最稳）。
"""
from __future__ import annotations

from pathlib import Path

from ..ffmpeg import drawtext_text, filter_literal, find_font, run

STYLES = 8   # 运镜风格数（按镜号轮换，跨镜自然形成节奏变化）

# 输入放大倍率 = 平滑度旋钮（模块头有五方案 A/B 数据）：成片上的抖动 ≈ 1px ÷ 下采样比，
# 放大越多越平滑、渲染越慢。4× 是实测拐点（抖动减半、耗时 +20%）。
SRC_SCALE = 4.0
# 渲染算法版本：**进片段缓存键**（`compose._clip_cache_name`）。源指纹（mtime/dur）
# 盯的是素材变化，盯不住「运镜算法改了」——不带版本号的话，改完平滑度用户重合成
# 也看不到任何变化（文件名没变 → 直接复用旧片段），只能靠 --force 全量重渲。
ALGO_VERSION = 2

# 分镜 camera 语义 → 运镜风格（按序匹配，先长词后短词防误中）：
# 指挥层写分镜时的运镜意图直接驱动风格选择——「缓慢推近」真的推近、
# 「拉远揭示」真的拉远；无 camera/不匹配时回落镜号轮换（保底不千篇一律）。
_CAMERA_STYLES = (
    (("对角", "斜移", "diag"), 4),
    (("旋", "rotate", "roll"), 6),
    (("呼吸", "凝视", "凝望", "breath"), 7),
    (("拉远", "拉出", "后拉", "zoom out", "pull", "dolly out"), 2),
    (("推近", "推进", "前推", "zoom in", "push", "dolly in"), 0),
    (("左移", "向左", "pan left", "left"), 3),
    (("右移", "向右", "pan right", "right"), 1),
)


def style_for(camera: str | None, idx: int) -> int:
    """按分镜 camera 语义选运镜风格；无语义命中回落镜号轮换。"""
    c = (camera or "").lower()
    for keys, style in _CAMERA_STYLES:
        if any(k in c for k in keys):
            return style
    return idx % STYLES


def _effect(idx: int, frames: int) -> dict:
    """返回 {z, x, y[, rot]} 表达式（zoompan 语法）。idx 决定运镜风格。

    e = smoothstep(on/denom)：缓入缓出进度（p²(3-2p)），替代线性匀速。
    rot（可选）是叠加在 zoompan 之后的 rotate 角度表达式（弧度，随 t）。"""
    denom = max(1, frames - 1)
    p = f"(on/{denom})"
    e = f"({p}*{p}*(3-2*{p}))"          # smoothstep 缓动
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    kind = idx % STYLES
    if kind == 0:   # 缓推（eased dolly-in）
        return {"z": f"1+0.16*{e}", "x": cx, "y": cy}
    if kind == 1:   # 右移（eased pan）
        return {"z": "1.12", "x": f"(iw-iw/zoom)*{e}", "y": cy}
    if kind == 2:   # 缓拉（eased dolly-out）
        return {"z": f"1.16-0.16*{e}", "x": cx, "y": cy}
    if kind == 3:   # 左移（eased pan）
        return {"z": "1.12", "x": f"(iw-iw/zoom)*(1-{e})", "y": cy}
    if kind == 4:   # 对角推近（左下→右上斜移 + 变焦，纵深感）
        return {"z": f"1+0.14*{e}", "x": f"(iw-iw/zoom)*{e}",
                "y": f"(ih-ih/zoom)*(1-{e})"}
    if kind == 5:   # 对角拉远（右上→左下）
        return {"z": f"1.14-0.14*{e}", "x": f"(iw-iw/zoom)*(1-{e})",
                "y": f"(ih-ih/zoom)*{e}"}
    if kind == 6:   # 微旋推近（±0.9° 极缓旋转叠加，"活"而不晕）
        return {"z": f"1+0.13*{e}", "x": cx, "y": cy,
                "rot": "0.016*sin(PI*t/{dur})"}
    # kind == 7  呼吸镜（正弦微变焦，情绪/凝视镜头）
    return {"z": f"1.10+0.045*sin(PI*{p})", "x": cx, "y": cy}


def _edge_fade_vf(dur: float, fade_in: float, fade_in_color: str,
                  fade_out: float, fade_out_color: str) -> str:
    """转场边缘淡化（transitions.edge_fades 的落点）：头部从底色淡入/尾部淡出到底色。"""
    parts = []
    if fade_in > 0:
        parts.append(f"fade=t=in:st=0:d={fade_in:.2f}:color={fade_in_color}")
    if fade_out > 0:
        parts.append(f"fade=t=out:st={max(0.0, dur - fade_out):.2f}"
                     f":d={fade_out:.2f}:color={fade_out_color}")
    return ("," + ",".join(parts)) if parts else ""


def render_shot(image: str, dur: float, out_path: str, *,
                width=1080, height=1920, fps=30, effect_index=0, label=None,
                fade_in=0.0, fade_in_color="black",
                fade_out=0.0, fade_out_color="black") -> str:
    frames = max(2, round(dur * fps))
    fx = _effect(int(effect_index), frames)
    z, x, y = fx["z"], fx["x"], fx["y"]
    # 输入放大 SRC_SCALE×：zoompan 的整数栅格误差在成片上被下采样比摊薄，
    # 这是平滑度的唯一有效杠杆（模块头有 A/B 数据）
    big_w = int(width * SRC_SCALE) // 2 * 2
    big_h = int(height * SRC_SCALE) // 2 * 2
    if fx.get("rot"):
        # 旋转风格：zoompan 先出 9% 过扫画布 → 极缓旋转 → 中心裁回目标尺寸
        # （过扫余量远大于 0.9° 旋转的边缘缺口，四角永不露黑）
        ow = int(width * 1.09) // 2 * 2
        oh = int(height * 1.09) // 2 * 2
        rot = fx["rot"].format(dur=f"{max(dur, 0.1):.3f}")
        mid = (f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={ow}x{oh}:fps={fps},"
               f"rotate=a='{rot}':c=black,crop={width}:{height}")
    else:
        mid = f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps}"
    vf = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h},"
        + mid + ",format=yuv420p"
        + _edge_fade_vf(dur, fade_in, fade_in_color, fade_out, fade_out_color)
    )
    if label:  # 可选角标（mock 图生视频用它与静图运镜片段区分）
        font = find_font()
        if font:
            vf += (f",drawtext=fontfile={filter_literal(font)}:text={drawtext_text(label)}:fontcolor=white@0.85:"
                   "fontsize=40:x=40:y=40:box=1:boxcolor=black@0.4:boxborderw=12")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    run(
        ["-loop", "1", "-framerate", str(fps), "-i", str(image),
         "-t", f"{dur:.3f}", "-vf", vf, "-r", str(fps),
         "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-an", str(out_path)],
        desc=f"kenburns {Path(image).name}",
    )
    return str(out_path)


def fit_clip(src: str, dur: float, out_path: str, *,
             width=1080, height=1920, fps=30, keep_audio=False,
             fade_in=0.0, fade_in_color="black",
             fade_out=0.0, fade_out_color="black",
             audio_edge=0.0) -> str:
    """把图生视频片段规整成画布尺寸、指定 fps、精确 dur 秒的片段（便于与其它片段拼接）。

    keep_audio=False（kenburns/dubbed）：去掉原生音频、比目标短则冻结最后一帧补足（音频另走旁白+BGM）。
    keep_audio=True（dubbed/native）：**保留片段自带音频**（我们的固定音色对口型 / Seedance 原生音画），
      视频与音频都规整；dur 取时间轴秒数，图生视频片段恒不短于它（provider 按
      整秒出片、容器还多约一帧），多出的部分在此裁掉。
    fade_in/fade_out：转场边缘淡化（相邻转场镜驱动，见 transitions.edge_fades）。
    audio_edge：音频边缘平滑秒数（仅 keep_audio 生效）——一镜一片各自带环境音，
      硬切处环境床是硬台阶，头尾各做一段等长淡化把台阶抹平；只动音频不动画面。
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fade_vf = _edge_fade_vf(dur, fade_in, fade_in_color, fade_out, fade_out_color)
    if keep_audio:
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},fps={fps},setsar=1,format=yuv420p" + fade_vf)
        af = f"aresample=44100,apad,atrim=0:{dur:.3f}"
        ae = min(float(audio_edge or 0.0), dur / 2)
        if ae > 0:
            af += (f",afade=t=in:st=0:d={ae:.3f}"
                   f",afade=t=out:st={max(dur - ae, 0.0):.3f}:d={ae:.3f}")
        run(
            ["-i", str(src), "-vf", vf, "-t", f"{dur:.3f}", "-r", str(fps),
             "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
             "-af", af, str(out_path)],
            desc=f"fit clip+audio {Path(src).name}",
        )
        return str(out_path)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1,"
        f"tpad=stop_mode=clone:stop_duration=3600,format=yuv420p" + fade_vf
    )
    run(
        ["-i", str(src), "-an", "-vf", vf, "-t", f"{dur:.3f}", "-r", str(fps),
         "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", str(out_path)],
        desc=f"fit clip {Path(src).name}",
    )
    return str(out_path)
