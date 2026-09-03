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

"""动态水印（防搬运）：文案在画面内**连续弹性漫游**——像 DVD 待机弹球。

运动模型（用户口径）：入场后水印**永不消失、永不瞬移**，匀速直线漂移；
碰到画面边界才反弹，且反弹后的方向/速度带随机性（不是固定镜面反射角）。
实现：Python 侧对整段时长做碰撞仿真（匀速 → 撞墙 → 随机角反弹 → 继续），
产出分段线性轨迹；每段一个 drawtext（enable 窗口衔接），**段边界位置严格
连续**——多段只是数学表达，观感是一条不间断的路径。

文本尺寸按字体度量估算（CJK≈1em/ASCII≈0.55em）用于碰撞边界，表达式再以
max/min(tw/th) 运行时钳制兜底。路线由 seed 确定性生成（缺省从文案+时长派生）：
同一输入同一条路线，幂等可复现、可单测。

防搬运语义：位置连续变化且反弹不可预测 → 裁切/delogo 无固定靶区。
产物 `<id>_wm_<比例>.mp4` 与原片并存（视频重编码，音频流直接复制）。
纯本地 ffmpeg，零 API 成本。
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

from ..ffmpeg import drawtext_text, filter_literal, find_font_cjk, probe_json, run

_MAX_SEGS = 400   # 仿真段数上限（防御异常参数，正常视频远达不到）


def _video_meta(path: str | Path) -> tuple[int, int, float]:
    info = probe_json(path)
    vs = next(s for s in info.get("streams", []) if s.get("codec_type") == "video")
    return int(vs["width"]), int(vs["height"]), float(info["format"]["duration"])


def _est_text_box(text: str, size: int) -> tuple[int, int]:
    """估算文本像素盒（碰撞边界用）：CJK≈1em、ASCII/半角≈0.55em，行高≈1.25em。"""
    units = sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in text)
    return max(size, round(units * size)), round(size * 1.25)


def simulate_path(rng: random.Random, *, width: int, height: int,
                  tw: int, th: int, margin: int, duration: float,
                  speed: float = 3.0) -> list[tuple]:
    """弹性漫游仿真 → 分段线性轨迹 [(t0, t1, x0, y0, vx, vy), ...]。

    speed 为基准速度（画面尺寸的百分比/秒）；每次反弹在 [0.6, 1.3]×speed 内
    重新取速——反弹角因此随机，不是镜面反射。被撞轴必然反向（弹回画面内），
    另一轴随机换向但在贴边时强制指向画面内（防止出界抖动）。
    段边界位置严格连续：x(t1⁻) == 下一段 x0。"""
    xmin, xmax = margin, max(margin + 1, width - tw - margin)
    ymin, ymax = margin, max(margin + 1, height - th - margin)
    sp = max(0.2, float(speed)) / 100.0

    def _v(dim):
        return rng.uniform(0.6 * sp, 1.3 * sp) * dim

    def _inward(sign, pos, lo, hi):
        if pos <= lo + 1:
            return 1
        if pos >= hi - 1:
            return -1
        return sign

    x, y = rng.uniform(xmin, xmax), rng.uniform(ymin, ymax)
    vx = _v(width) * rng.choice((-1, 1))
    vy = _v(height) * rng.choice((-1, 1))
    t, segs = 0.0, []
    while t < duration and len(segs) < _MAX_SEGS:
        hit_x = ((xmax - x) / vx) if vx > 0 else ((xmin - x) / vx)
        hit_y = ((ymax - y) / vy) if vy > 0 else ((ymin - y) / vy)
        hit = max(1e-3, min(hit_x, hit_y))
        t1 = min(duration, t + hit)
        segs.append((t, t1, x, y, vx, vy))
        if t1 >= duration:
            break
        x, y = x + vx * (t1 - t), y + vy * (t1 - t)
        if hit_x <= hit_y:   # 撞左右墙：x 轴必反向，y 轴随机（贴边强制向内）
            vx = _v(width) * (-1 if vx > 0 else 1)
            vy = _v(height) * _inward(rng.choice((-1, 1)), y, ymin, ymax)
        if hit_y <= hit_x:   # 撞上下墙（角落两轴同时反弹）
            vy = _v(height) * (-1 if vy > 0 else 1)
            vx = _v(width) * _inward(rng.choice((-1, 1)), x, xmin, xmax)
        t = t1
    return segs


def build_filter(text: str, *, width: int, height: int, duration: float,
                 font: str | None = None, seed: int | None = None,
                 size: int | None = None, opacity: float = 0.30,
                 color: str = "white", speed: float = 3.0,
                 fade: float = 0.6) -> str:
    """构造水印 filtergraph（drawtext 链，逗号相连可直接作 -vf 值）。

    · 连续弹性漫游（碰壁随机反弹），全程在场、零消失零瞬移；
    · 仅入场一次 fade 秒淡入（此后恒定透明度）；
    · 柔和投影替代硬描边（暗底可读、亮底不糙）；
    · x/y 以 max/min 钳制画面内（含运行时 tw/th，估算误差不越界）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("水印文案为空")
    if seed is None:   # 确定性缺省 seed：同文案+同时长 → 同一条路线（幂等/可测）
        seed = int(hashlib.md5(f"{text}|{duration:.2f}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    size = size or max(22, round(height * 0.025))
    margin = max(10, size // 2)
    tw, th = _est_text_box(text, size)
    segs = simulate_path(rng, width=width, height=height, tw=tw, th=th,
                         margin=margin, duration=duration, speed=speed)
    esc = drawtext_text(text)
    fade = max(0.2, float(fade))
    common = (f"fontsize={size}:fontcolor={color}"
              + (f":fontfile={filter_literal(font)}" if font else "")
              + ":shadowcolor=black@0.35:shadowx=1:shadowy=2"
              # 入场一次淡入，此后恒定——全程在场，绝不消失
              + f":alpha='{opacity:.2f}*min(1\\,t/{fade:.2f})'")
    parts = []
    for t0, t1, x0, y0, vx, vy in segs:
        x = (f"max({margin}\\,min(w-tw-{margin}\\,"
             f"{x0:.1f}+{vx:.2f}*(t-{t0:.3f})))")
        y = (f"max({margin}\\,min(h-th-{margin}\\,"
             f"{y0:.1f}+{vy:.2f}*(t-{t0:.3f})))")
        parts.append(f"drawtext=text={esc}:{common}:x='{x}':y='{y}'"
                     f":enable='between(t\\,{t0:.3f}\\,{t1:.3f})'")
    return ",".join(parts)


_CORNERS = ("tl", "tr", "bl", "br")   # 左上 / 右上 / 左下 / 右下


def build_fixed_filter(text: str, *, width: int, height: int,
                       position: str = "br", font: str | None = None,
                       size: int | None = None, color: str = "white",
                       opacity: float = 1.0, outline: int | None = None,
                       margin: int | None = None) -> str:
    """固定角标水印（**字幕式烧录**，与漂移水印互补）：单个 drawtext 钉死在四角之一，
    全程不动、不漫游。用于品牌署名/频道号——**清晰、不透明、细腻**：
      · 缺省完全不透明（opacity=1.0），不做半透明淡出，**不模糊**；
      · **细描边**（borderw≈3% 字号·很薄）保证任意背景可读，但不是粗黑框、边缘锐利=高清；
      · 字号缺省比字幕小四号（由调用方按字幕字号 ×0.52 传入），字体缺省工程内置阿里普惠体（免费商用·现代黑体）；
      · **贴边**：距屏幕边缺省只留约 1/3 字高（margin=size/3），紧贴四角不飘。
    position ∈ {tl, tr, bl, br}；非法值回落 br。x/y 用 tw/th 运行时度量右/下对齐，
    与字号无关地精确贴角（含 margin 安全内距）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("角标水印文案为空")
    if position not in _CORNERS:
        position = "br"
    size = size or max(20, round(height * 0.030))
    # 贴边：距屏幕边≈1/3 字高（不留一整个字的空）
    margin = margin if margin is not None else max(3, round(size / 3))
    # 细描边：约 3% 字号·很薄——够读但不是粗黑框
    outline = outline if outline is not None else max(1, round(size * 0.03))
    esc = drawtext_text(text)
    x = f"{margin}" if position in ("tl", "bl") else f"w-tw-{margin}"
    y = f"{margin}" if position in ("tl", "tr") else f"h-th-{margin}"
    alpha = "" if opacity >= 0.999 else f":alpha={max(0.0, opacity):.2f}"
    return (f"drawtext=text={esc}:fontsize={size}:fontcolor={color}"
            + (f":fontfile={filter_literal(font)}" if font else "")
            + f":borderw={outline}:bordercolor=black@0.45"   # 细锐薄描边（够读·不是粗黑框）
            + f":x='{x}':y='{y}'" + alpha)


def build_bottom_filter(text: str, *, width: int, height: int,
                        font: str | None = None, size: int | None = None,
                        color: str = "white", opacity: float = 0.55,
                        margin: int | None = None) -> str:
    """底部居中水印（**半透明常驻署名**，与漂移/角标互补）：钉在画面底部正中、
    离底边留一小段呼吸距，全程不动。定位是低干扰的常驻署名而非防搬运：
      · **半透明**（缺省 opacity 约五成半）——看得见但不抢画面；
      · **无描边、无底衬**——不画黑边黑条，只留一层极轻的柔影保证亮底可读；
      · 字号缺省取画面高的一小档（比角标更细），细字号+半透明降低视觉权重；
      · 底边距缺省≈0.6 字高——字幕底带在它上方（横屏字幕距底一个**字幕**字高、
        竖屏距底 360，见 subtitle._default_margin_v），两者天然分层不重叠。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("底部水印文案为空")
    size = size or max(18, round(height * 0.024))
    margin = margin if margin is not None else max(8, round(size * 0.6))
    esc = drawtext_text(text)
    alpha = f":alpha={max(0.0, min(1.0, opacity)):.2f}"
    return (f"drawtext=text={esc}:fontsize={size}:fontcolor={color}"
            + (f":fontfile={filter_literal(font)}" if font else "")
            + ":shadowcolor=black@0.30:shadowx=0:shadowy=1"
            + f":x='(w-tw)/2':y='h-th-{margin}'" + alpha)


def _wm_font() -> str | None:
    """水印/角标缺省字体：**工程内置阿里普惠体 Regular（免费商用·随仓库分发·跨系统一致）**——
    现代无衬线黑体，小字号比宋体衬线更清晰专业；不依赖各机器系统字体（换机/换系统不变样）。
    回落链（内置缺失时）：冬青黑 GB(mac·face 0=W3 简体) / Noto(Linux) → 系统 CJK。
    要换字体：project.json / branding.yaml 的 watermark_fixed.font 填任意字体绝对路径。"""
    from ..fonts import PUHUITI_REGULAR, bundled_path
    p = bundled_path(PUHUITI_REGULAR)
    if p:
        return p
    for q in ("/System/Library/Fonts/Hiragino Sans GB.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if Path(q).is_file():
            return q
    return find_font_cjk()


def apply(video_in: str | Path, video_out: str | Path, *,
          floating: dict | None = None, fixed: dict | None = None,
          bottom: dict | None = None, seed: int | None = None) -> str:
    """给成片叠加水印 → 写出水印版（原片不动，双版本并存）。

    floating: 漂移水印（防搬运）配置 {text, size, opacity, color, speed, fade}；
    fixed:    固定角标水印（品牌署名）配置 {text, position, size, color, opacity, font}；
    bottom:   底部居中水印（半透明常驻署名）配置 {text, size, color, opacity, margin, font}。
    三者可任意组合（在同一次重编码里一起烧），至少给一个非空 text。"""
    w, h, dur = _video_meta(video_in)
    filters = []
    if floating and (floating.get("text") or "").strip():
        filters.append(build_filter(
            floating["text"], width=w, height=h, duration=dur,
            font=_wm_font(), seed=seed, size=floating.get("size"),
            opacity=float(floating.get("opacity", 0.30)),
            color=floating.get("color", "white"),
            speed=float(floating.get("speed", 3.0)),
            fade=float(floating.get("fade", 0.6))))
    if fixed and (fixed.get("text") or "").strip():
        filters.append(build_fixed_filter(
            fixed["text"], width=w, height=h,
            position=fixed.get("position", "br"),
            font=fixed.get("font") or _wm_font(),
            size=fixed.get("size"), color=fixed.get("color", "white"),
            opacity=float(fixed.get("opacity", 1.0))))
    if bottom and (bottom.get("text") or "").strip():
        filters.append(build_bottom_filter(
            bottom["text"], width=w, height=h,
            font=bottom.get("font") or _wm_font(),
            size=bottom.get("size"), color=bottom.get("color", "white"),
            opacity=float(bottom.get("opacity", 0.55)),
            margin=bottom.get("margin")))
    if not filters:
        raise ValueError("没有要叠加的水印（floating/fixed/bottom 的 text 都为空）")
    vf = ",".join(filters)
    Path(video_out).parent.mkdir(parents=True, exist_ok=True)
    run(["-i", str(video_in), "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(video_out)],
        desc="watermark")
    return str(video_out)
