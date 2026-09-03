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

"""视觉/听觉特效框架。

每个特效返回一个 EffectPlan，由 compose 统一编织进 FFmpeg filtergraph：
- vfilters：直接链在主视频上的调色/几何滤镜（无需额外输入）
- subgraph：复杂子图（含内部 label/分号），用 {IN}/{OUT} 占位（如泛光多尺度辉光）
- overlay_*：需要一个额外图层并叠加（雨/雪/雾/粒子/火焰/光扫）
- audio_*：额外环境音（雨/雪/雾/火）

—— 三条影视级铁律（决定"专业"与"廉价"之别，改特效前必读）——

① **发光层在 RGB 空间混合**：粒子/火焰/光扫用 blend=screen 时，必须在 gbrp(RGB)
   空间做（compose 已统一处理 overlay_blend!="overlay" 的分支）。切勿在 YUV 上
   screen——中性色度(128)会参与运算把整幅画面染偏色（星尘会被染成全屏品红）。

② **阈值化用 lut 不用 lutyuv**：把噪声抠成稀疏粒点用 `lut=y='if(gt(val,T),255,0)'`。
   `format=gray,lutyuv=y=...` 在单平面 gray 上会误判、把 1% 稀疏点算成 67% 满屏灰
   （所有粒子层会变成一层灰霾而非离散光点）。

③ **时变亮度用 geq/eq/hue，切勿用 lutrgb/lutyuv 的 t**：lut 系在初始化时求值一次，
   表达式里写 `t` 会直接让 ffmpeg 崩溃退出（萤火虫特效整条渲染失败）。
   逐帧变化只能走支持每帧求值的 geq(T)、eq(eval=frame)、hue(t)。

性能：程序化层一律在**低分辨率**（长边≈base）生成完再 bicubic 放大——粒子/雾/火焰
本就是柔光，低分辨率天然柔化且省 16× 算力（1080p 全分辨率 perlin/geq 太慢）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EffectPlan:
    name: str
    vfilters: list[str] = field(default_factory=list)   # 链在主视频上的简单滤镜
    subgraph: str | None = None                         # 复杂子图（含内部 label/分号），用 {IN}/{OUT} 占位
    overlay_input: str | None = None                    # 额外视频图层的 lavfi 源（可为多源合成图）
    overlay_filter: str | None = None                   # 作用于该图层的滤镜（None→compose 填 null）
    overlay_blend: str = "screen"                       # 与主视频的混合模式（overlay=alpha 合成；其余=RGB blend）
    audio_input: str | None = None                      # 额外环境音的 lavfi 源
    audio_filter: str | None = None                     # 作用于环境音的滤镜


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _even(n) -> int:
    return max(2, int(n) // 2 * 2)


def _lowres(w: int, h: int, base: int = 480) -> tuple[int, int]:
    """按主画布比例缩到长边≈base 的低分辨率工作尺寸（偶数）。程序化层在此算完再放大。"""
    if w >= h:
        return _even(base), _even(base * h / w)
    return _even(base * w / h), _even(base)


def _particle_layer(w: int, h: int, fps: int, *, seed: int, thresh: int,
                    drift_v: float, drift_h: float, tint: str,
                    twinkle: float = 0.55, tw_spd: float = 0.5, tw_scale: float = 0.5,
                    blur: float = 4.0, base: int = 480, sparse: bool = False) -> str:
    """漂浮粒子层工厂（火星/萤火/星尘/浮尘共用，参数化风格）——管线：

      低分辨率黑底 → 静噪 → **lut** 阈值抠稀疏定点（非 lutyuv，避免满屏灰）→
      定向漂移(scroll) → **geq** 逐区相位明灭（非 lutrgb-t，避免崩溃；不同位置不同
      相位=像真萤火各自闪）→ bicubic 放大成柔光斑 → gblur 光晕 → 染色。

    输出 rgb24（黑处=纯黑），交由 compose 在 gbrp 空间 screen 叠加。

    **运动方向铁律（scroll 已实测标定）**：竖直 drift_v 负=下落、正=上升；水平 drift_h
    正=右、负=左。速度=画面尺寸比例/帧，px/s ≈ drift_v×高×fps（1080@30fps 时 ×32400）。
    落雨落雪落瓣用负 drift_v，火星/浮尘上飘用正 drift_v。

    **密度**：单噪阈值最高只能到 ~147（噪声值域封顶 148），仍有 ~1% ≈ 千余点——太密。
    需要真稀疏（萤火/火星，几十点）时 sparse=True：两个独立噪声场相乘(AND)，只留双方都亮
    的点，thresh≈130 时 480×270 上约 60 点。稠密细闪（星尘/浮尘）走单噪 sparse=False。
    """
    lw, lh = _lowres(w, h, base)
    rest = round(1.0 - twinkle, 3)
    twk = round(twinkle, 3)
    if sparse:                       # 两噪相乘=可控稀疏（几十个离散点）
        points = (
            f"color=black:s={lw}x{lh}:r={fps},noise=alls=100:all_seed={seed},"
            f"format=gray,lut=y='if(gt(val,{thresh}),255,0)'[pa];"
            f"color=black:s={lw}x{lh}:r={fps},noise=alls=100:all_seed={seed + 58},"
            f"format=gray,lut=y='if(gt(val,{thresh}),255,0)'[pb];"
            f"[pa][pb]blend=all_mode=multiply,format=gray,"
        )
    else:                            # 单噪=稠密细点
        points = (
            f"color=black:s={lw}x{lh}:r={fps},noise=alls=100:all_seed={seed},"
            f"format=gray,lut=y='if(gt(val,{thresh}),255,0)',"
        )
    return (
        f"{points}"
        f"scroll=vertical={drift_v}:horizontal={drift_h},"
        f"geq=lum='lum(X,Y)*({rest}+{twk}*(0.5+0.5*sin("
        f"6.283*T*{tw_spd}+X*{tw_scale}+Y*{round(tw_scale * 0.8, 3)})))',"
        f"scale={w}:{h}:flags=bicubic,gblur=sigma={blur},"
        f"format=rgb24,lutrgb={tint}"
    )


# ---------------------------------------------------------------------------
# 调色 / 质感（作用于主视频，YUV 安全）
# ---------------------------------------------------------------------------
def _vignette(w, h, fps) -> EffectPlan:
    # 影院级柔角：ffmpeg 原生 vignette（径向自然衰减，比 drawbox 压角柔）+ 极轻 gamma
    # 加深，角落聚焦中心而不显生硬。angle 越小越柔——PI/5 偏重，收到 PI/6.3 更克制。
    return EffectPlan("vignette",
                      vfilters=["vignette=angle=PI/6.3:mode=backward", "eq=gamma=0.97"])


def _film_grain(w, h, fps) -> EffectPlan:
    # 真实胶片颗粒（logiclrd「ultimate film grain」思路的 ffmpeg 化）：
    # 半分辨率灰噪 → 放大得到"颗粒尺寸"（非逐像素死板）→ softlight 叠加（只调亮度不偏色、
    # 中间调受影响最大=最像真胶片的暗部干净/中间调起粒）。走 RGB blend，色度安全。
    hw, hh = _even(w / 2), _even(h / 2)
    layer = (
        f"color=gray:s={hw}x{hh}:r={fps},"
        "noise=alls=26:allf=t,"                          # 逐帧刷新（allf=t）=活的颗粒
        f"scale={w}:{h}:flags=bilinear,eq=contrast=1.15"
    )
    return EffectPlan("film_grain", overlay_input=layer, overlay_blend="softlight")


def _paper_grain(w, h, fps) -> EffectPlan:
    # 纸纹质感（拼贴/剪纸风的底面质感）：与胶片颗粒同族但**纹理静止**——
    # 纸就是同一张纸，纤维不逐帧刷新（noise 刻意不带 allf=t，对照 _film_grain 的
    # "活颗粒"）；半分辨率灰噪放大成纤维颗粒 + 纵向微拉丝（纸纤维方向性）→
    # softlight 叠加（只调亮度不偏色）；主画面再压一层微暖 = 米白卡纸底色。
    hw, hh = _even(w / 2), _even(h / 2)
    layer = (
        f"color=gray:s={hw}x{hh}:r={fps},"
        "noise=alls=24,"                                 # 无 allf=t：纤维静止=同一张纸
        f"scale={w}:{h}:flags=bilinear,"
        "gblur=sigma=0.5:sigmaV=1.3,"                    # 纵向微拉丝=纸纤维方向
        "eq=contrast=1.18"
    )
    return EffectPlan(
        "paper_grain",
        vfilters=["colortemperature=temperature=5900:mix=0.4",
                  "eq=brightness=0.01:saturation=0.95"],
        overlay_input=layer, overlay_blend="softlight")


def _stopmotion(w, h, fps) -> EffectPlan:
    # 定格顿挫（剪纸拼贴/粘土定格的「拍二格」质感）：先把运动量化到 12fps 再回
    # 容器帧率——画面只在每秒 12 个时刻更新、每帧停留更久，手作定格的顿挫感即来
    # 于此；回补帧率保证字幕烧录与音画时间轴不受影响。刻意 12 不取 8：8fps 在
    # 快运镜下会读成掉帧卡顿，12fps 是定格片发行的常见口径。
    return EffectPlan("stopmotion", vfilters=["fps=12", f"fps={fps}"])


def _warm(w, h, fps) -> EffectPlan:
    # 黄金时刻暖调（不是粗暴加黄）：色温压到 4700K 暖 + 电影 S 曲线（暗抬、亮压、层次）
    # + 阴影微青/高光微暖的橘青分离（克制）+ 轻微提饱和。
    return EffectPlan(
        "warm",
        vfilters=[
            "colortemperature=temperature=4700:mix=0.85:pl=0.2",
            "curves=all='0/0.02 0.25/0.22 0.5/0.52 0.75/0.8 1/0.98'",
            "colorbalance=rs=-0.03:bs=0.04:rh=0.04:bh=-0.05",
            "eq=saturation=1.07",
        ])


def _bloom(w, h, fps) -> EffectPlan:
    # 多尺度泛光（HD-2D/新海诚招牌辉光）：只对真高光(>0.7)抽取，两级模糊(紧+散)叠加=
    # 各向同性柔光晕，再 screen 回主画面。全程 gbrp，色度安全；阈值曲线保证暗部不发灰。
    thr = "curves=all='0/0 0.68/0 0.86/0.72 1/1'"
    sub = (
        "[{IN}]format=gbrp,split=3[bl0][bl1][bl2];"
        f"[bl1]{thr},gblur=sigma=7[blg1];"
        f"[bl2]{thr},gblur=sigma=22[blg2];"
        "[blg1][blg2]blend=all_mode=screen[blg];"
        "[bl0][blg]blend=all_mode=screen,format=yuv420p[{OUT}]"
    )
    return EffectPlan("bloom", subgraph=sub)


# ---------------------------------------------------------------------------
# 游戏复古
# ---------------------------------------------------------------------------
def _scanlines(w, h, fps) -> EffectPlan:
    # 每 4px 一条半透明横线 = CRT/游戏扫描线质感（用户认可，保持不动）
    return EffectPlan("scanlines", vfilters=["drawgrid=w=iw:h=4:t=1:color=black@0.18"])


def _hud(w, h, fps) -> EffectPlan:
    # 战术抬头显示（3A 游戏 HUD 美学）：四角取景框 + 分段血条/护盾条 + 中心目标准星（缺口
    # 十字 + 四角括线 + 中心点）+ 右上雷达（扫描线 + 光点）+ 右下弹药条。统一科技青
    # (0x27E8FF)体系、关键簇带半透暗底板（任何背景上都清晰可读）。
    # **坐标必须用 iw/ih（输入画面尺寸）——drawbox 里的 w/h 是"框自身宽高"不是画面尺寸，
    # 写 x=w-… 会把元素全挤到左上角**。纯 drawbox 零字体依赖、跨平台稳。
    cy = "0x27E8FF"       # 科技青
    hp = "0x3BF07A"       # 生命绿
    am = "0xF5A623"       # 弹药琥珀
    dk = "black@0.5"      # 条内底槽
    pn = "black@0.22"     # 簇底板
    px = ",".join([
        # —— 四角取景 L 框 ——
        f"drawbox=x=48:y=48:w=128:h=3:color={cy}@0.9:t=fill",
        f"drawbox=x=48:y=48:w=3:h=128:color={cy}@0.9:t=fill",
        f"drawbox=x=iw-176:y=48:w=128:h=3:color={cy}@0.9:t=fill",
        f"drawbox=x=iw-51:y=48:w=3:h=128:color={cy}@0.9:t=fill",
        f"drawbox=x=48:y=ih-51:w=128:h=3:color={cy}@0.9:t=fill",
        f"drawbox=x=48:y=ih-176:w=3:h=128:color={cy}@0.9:t=fill",
        f"drawbox=x=iw-176:y=ih-51:w=128:h=3:color={cy}@0.9:t=fill",
        f"drawbox=x=iw-51:y=ih-176:w=3:h=128:color={cy}@0.9:t=fill",
        # —— 左上状态簇（底板 + 生命条外框/底槽/绿填 72% + 3 道分段缝 + 护盾条）——
        f"drawbox=x=72:y=98:w=486:h=82:color={pn}:t=fill",
        f"drawbox=x=92:y=112:w=430:h=26:color={cy}@0.85:t=2",
        f"drawbox=x=96:y=116:w=422:h=18:color={dk}:t=fill",
        f"drawbox=x=96:y=116:w=304:h=18:color={hp}@0.95:t=fill",
        f"drawbox=x=201:y=116:w=2:h=18:color=black@0.65:t=fill",
        f"drawbox=x=306:y=116:w=2:h=18:color=black@0.65:t=fill",
        f"drawbox=x=411:y=116:w=2:h=18:color=black@0.65:t=fill",
        f"drawbox=x=96:y=146:w=344:h=10:color={dk}:t=fill",
        f"drawbox=x=96:y=146:w=172:h=10:color={cy}@0.9:t=fill",
        # —— 中心准星：缺口十字（四向刻度）+ 四角括线 + 中心点 ——
        f"drawbox=x=iw/2-44:y=ih/2-1:w=26:h=3:color=white@0.92:t=fill",
        f"drawbox=x=iw/2+18:y=ih/2-1:w=26:h=3:color=white@0.92:t=fill",
        f"drawbox=x=iw/2-1:y=ih/2-44:w=3:h=26:color=white@0.92:t=fill",
        f"drawbox=x=iw/2-1:y=ih/2+18:w=3:h=26:color=white@0.92:t=fill",
        f"drawbox=x=iw/2-34:y=ih/2-34:w=14:h=2:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2-34:y=ih/2-34:w=2:h=14:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2+20:y=ih/2-34:w=14:h=2:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2+32:y=ih/2-34:w=2:h=14:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2-34:y=ih/2+32:w=14:h=2:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2-34:y=ih/2+20:w=2:h=14:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2+20:y=ih/2+32:w=14:h=2:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2+32:y=ih/2+20:w=2:h=14:color={cy}@0.8:t=fill",
        f"drawbox=x=iw/2-3:y=ih/2-3:w=6:h=6:color={cy}:t=fill",
        # —— 右上雷达（底板 + 边框 + 内十字 + 对角扫描线 + 光点）——
        f"drawbox=x=iw-286:y=96:w=190:h=190:color={pn}:t=fill",
        f"drawbox=x=iw-286:y=96:w=190:h=190:color={cy}@0.8:t=2",
        f"drawbox=x=iw-191:y=106:w=1:h=170:color={cy}@0.35:t=fill",
        f"drawbox=x=iw-276:y=191:w=170:h=1:color={cy}@0.35:t=fill",
        f"drawbox=x=iw-284:y=98:w=126:h=126:color={cy}@0.28:t=1",
        f"drawbox=x=iw-196:y=176:w=8:h=8:color={cy}:t=fill",
        f"drawbox=x=iw-150:y=150:w=6:h=6:color={hp}:t=fill",
        f"drawbox=x=iw-236:y=210:w=6:h=6:color={am}:t=fill",
        # —— 右下弹药簇（底板 + 主弹药条 + 副状态条）——
        f"drawbox=x=iw-256:y=ih-168:w=180:h=52:color={pn}:t=fill",
        f"drawbox=x=iw-240:y=ih-152:w=148:h=14:color={dk}:t=fill",
        f"drawbox=x=iw-240:y=ih-152:w=118:h=14:color={am}@0.9:t=fill",
        f"drawbox=x=iw-240:y=ih-130:w=148:h=8:color={dk}:t=fill",
        f"drawbox=x=iw-240:y=ih-130:w=88:h=8:color={cy}@0.85:t=fill",
    ])
    return EffectPlan("hud", vfilters=[px])


# ---------------------------------------------------------------------------
# 天气（overlay + 环境音）
# ---------------------------------------------------------------------------
def _rain(w, h, fps) -> EffectPlan:
    # 影视级雨（双层视差 + 雨丝 + 冷调压暗 + 雨声）：
    #  · 近/远两层不同密度、粗细、落速 → 景深（近层粗快亮、远层细慢淡）
    #  · 雨丝 = 稀疏点经强纵向 gblur 拉成带运动模糊的斜线（非方块）
    #  · 主画面压暗降饱和 + 冷调；screen(RGB) 叠雨丝——色度安全
    lw, lh = _lowres(w, h, 600)

    def streak(seed, thr, sv, sh, sx, sy, gain):     # 一层雨丝
        return (
            f"color=black:s={lw}x{lh}:r={fps},"
            f"noise=alls=100:all_seed={seed},format=gray,"
            f"lut=y='if(gt(val,{thr}),{gain},0)',"
            f"gblur=sigma={sx}:sigmaV={sy},"          # 纵向强模糊=雨丝
            f"scroll=vertical={sv}:horizontal={sh},"  # 下落(负) + 微斜风(正=右)
            f"scale={w}:{h}:flags=bilinear"
        )
    # drift_v 负=下落；速度实测 px/s≈|sv|×高×fps（近层≈2000px/s、远层≈1300px/s）
    far = streak(101, 150, -0.040, 0.0022, 0.4, 5, 150)
    near = streak(137, 138, -0.062, 0.0032, 0.7, 9, 255)
    layer = (f"{far},format=gbrp[rf];"
             f"{near},format=gbrp[rn];"
             "[rf][rn]blend=all_mode=screen,"
             "format=rgb24,lutrgb=r='val*0.72':g='val*0.86':b=val")   # 冷白蓝雨色
    return EffectPlan(
        "rain",
        vfilters=["eq=brightness=-0.05:saturation=0.86:contrast=1.02",
                  "colorbalance=bs=0.10:bm=0.05:rs=-0.03"],
        overlay_input=layer, overlay_blend="screen",
        audio_input="anoisesrc=color=pink:amplitude=0.85",
        audio_filter="highpass=f=420,lowpass=f=6800,volume=0.24",
    )


def _snow(w, h, fps) -> EffectPlan:
    # 影视级雪（双层视差 + 柔圆雪花 + 冷调 + 风噪）：近层大而快、远层小而慢且更虚（离焦），
    # 各带反向微飘=风。screen(RGB) 叠加，白雪不偏色。
    lw, lh = _lowres(w, h, 480)

    def flakes(seed, thr, sv, sh, blur, gain):
        return (
            f"color=black:s={lw}x{lh}:r={fps},"
            f"noise=alls=100:all_seed={seed},format=gray,"
            f"lut=y='if(gt(val,{thr}),{gain},0)',"
            f"scroll=vertical={sv}:horizontal={sh},"
            f"scale={w}:{h}:flags=bicubic,gblur=sigma={blur}"
        )
    # drift_v 负=下落（雪比雨慢一个量级）；近层大而快、远层小而慢；水平反向微飘=风+景深
    far = flakes(211, 140, -0.0035, 0.0018, 3.0, 150)
    near = flakes(233, 148, -0.0065, -0.0022, 5.5, 255)
    layer = (f"{far},format=gbrp[sf];"
             f"{near},format=gbrp[sn];"
             "[sf][sn]blend=all_mode=screen,format=rgb24")
    return EffectPlan(
        "snow",
        vfilters=["eq=brightness=0.01:saturation=0.9", "colorbalance=bs=0.05:bm=0.03"],
        overlay_input=layer, overlay_blend="screen",
        audio_input="anoisesrc=color=white:amplitude=0.3",
        audio_filter="lowpass=f=1100,volume=0.08",
    )


def _fog(w, h, fps) -> EffectPlan:
    # 体积雾（perlin 真流动，非一张糊图平移）：低分辨率 perlin 时域演化(tscale)=雾团翻卷 +
    # 缓慢横飘，放大重糊成柔幔。screen 叠一层低亮灰雾=物理正确的"雾把暗部抬灰、压低对比"；
    # 主画面再降对比降饱和补足空气透视。低频环境音。
    lw, lh = _lowres(w, h, 320)
    layer = (
        f"perlin=size={lw}x{lh}:rate={fps}:octaves=4:persistence=0.72:"
        f"xscale=1.5:yscale=1.3:tscale=0.22,"
        f"format=gray,eq=contrast=1.25:brightness=-0.10,"       # 拉开雾团浓淡、整体压暗（screen 用）
        f"scale={w}:{h}:flags=bicubic,gblur=sigma=18,"
        f"format=rgb24,lutrgb=r='val*0.92':g='val*0.95':b=val"  # 冷灰白雾
    )
    return EffectPlan(
        "fog",
        vfilters=["eq=contrast=0.9:brightness=0.03:saturation=0.82",
                  "colorbalance=bs=0.03:bm=0.02"],
        overlay_input=layer, overlay_blend="screen",
        audio_input="anoisesrc=color=brown:amplitude=0.5",
        audio_filter="lowpass=f=480,volume=0.06",
    )


# ---------------------------------------------------------------------------
# 光效
# ---------------------------------------------------------------------------
def _light_sweep(w, h, fps) -> EffectPlan:
    # 斜光扫过（纪录片/MV 打光的滤镜化）：低分辨率 geq 画一道对角高斯柔光带，中心随 T 从
    # 屏外左匀速扫到屏外右（两端都在画外→循环无跳变），放大重糊后 screen 轻叠=一束柔光缓缓
    # 掠过。全程 geq/gblur，无逐像素死表达式的初始化陷阱。
    lw, lh = _lowres(w, h, 320)
    period = 7.0
    sigma = round(lw * 0.11, 2)
    slope = 0.55
    margin = round(lw * 0.55, 2)
    span = round(lw + 2 * margin, 2)
    center = f"(({span})*mod(T,{period})/{period}-{margin})"
    layer = (
        f"nullsrc=s={lw}x{lh}:r={fps},format=gray,"
        f"geq=lum='210*exp(-pow(X+{slope}*Y-{center},2)/(2*pow({sigma},2)))',"
        f"scale={w}:{h}:flags=bicubic,gblur=sigma=10,"
        f"format=rgb24,lutrgb=r=val:g='val*0.99':b='val*0.94'"   # 近白微暖
    )
    return EffectPlan("light_sweep", overlay_input=layer, overlay_blend="screen")


# ---------------------------------------------------------------------------
# 粒子（overlay，发光层走 screen）——只保留星辰与萤火虫两种精细粒子层
# ---------------------------------------------------------------------------
def _fireflies(w, h, fps) -> EffectPlan:
    # 萤火虫：几十点黄绿柔光**极缓慢悬浮**(drift_v 微正=轻轻上浮) + 各自明灭（geq 逐区相位，
    # 非全层同步闪）。sparse=两噪相乘约 60 点；blur 收紧到 3.0=清晰有形的光点而非糊团。夏夜/治愈。
    layer = _particle_layer(
        w, h, fps, seed=33, thresh=131, drift_v=0.0015, drift_h=0.0016,
        twinkle=0.75, tw_spd=0.5, tw_scale=0.5, blur=3.0, sparse=True,
        tint="r='val*0.62':g=val:b='val*0.2'")
    return EffectPlan("fireflies", overlay_input=layer, overlay_blend="screen")


def _sparkles(w, h, fps) -> EffectPlan:
    # 星辰闪烁：冷白细点**固定不动**(drift=0)、只逐粒明灭——像夜空繁星忽明忽暗、位置恒定不漂移。
    # sparse=两噪相乘减量（不满屏）+ blur=1.0 细锐星点（不糊）。星空/魔法/梦幻。
    layer = _particle_layer(
        w, h, fps, seed=47, thresh=124, drift_v=0.0, drift_h=0.0,
        twinkle=0.8, tw_spd=1.1, tw_scale=0.9, blur=1.0, sparse=True,
        tint="r=val:g='val*0.95':b='val*0.74'")
    return EffectPlan("sparkles", overlay_input=layer, overlay_blend="screen")


# ---------------------------------------------------------------------------
EFFECTS = {
    "vignette": _vignette,
    "film_grain": _film_grain,
    "paper_grain": _paper_grain,
    "stopmotion": _stopmotion,
    "scanlines": _scanlines,
    "hud": _hud,
    "rain": _rain,
    "snow": _snow,
    "fog": _fog,
    "bloom": _bloom,
    "warm": _warm,
    "light_sweep": _light_sweep,
    "fireflies": _fireflies,
    "sparkles": _sparkles,
}


def build_plan(name: str, w: int, h: int, fps: int) -> EffectPlan | None:
    fn = EFFECTS.get(name)
    return fn(w, h, fps) if fn else None


# ---------------------------------------------------------------------------
# 特效元数据（目录真源）：CLI --effects / Studio 选择器 / 漂移守卫共用同一份。
# 键必须与 EFFECTS 注册表一一对应（test_config_drift 强制守卫）。
# ---------------------------------------------------------------------------
CATEGORY_LABELS = {
    "texture": "质感调色", "game": "游戏复古", "weather": "天气",
    "particle": "粒子", "light": "光效",
}

EFFECT_META = {
    "vignette":    {"label": "暗角",       "category": "texture",  "audio": False, "desc": "镜头四周柔和压暗，视线聚焦中心"},
    "film_grain":  {"label": "胶片颗粒",   "category": "texture",  "audio": False, "desc": "带颗粒尺寸的真实胶片噪点，中间调起粒暗部干净"},
    "paper_grain": {"label": "纸纹",       "category": "texture",  "audio": False, "desc": "静止卡纸纤维颗粒+微暖底色，拼贴/剪纸质感"},
    "stopmotion":  {"label": "定格顿挫",   "category": "texture",  "audio": False, "desc": "运动量化到 12fps 再回原帧率，手作定格的拍二格顿挫"},
    "warm":        {"label": "暖调",       "category": "texture",  "audio": False, "desc": "黄金时刻暖色 + 电影 S 曲线 + 橘青分离"},
    "bloom":       {"label": "泛光",       "category": "texture",  "audio": False, "desc": "多尺度高光柔光辉光，HD-2D/新海诚招牌"},
    "scanlines":   {"label": "CRT 扫描线", "category": "game",     "audio": False, "desc": "每 4px 一条半透黑线，街机/复古"},
    "hud":         {"label": "游戏 HUD",   "category": "game",     "audio": False, "desc": "取景框+分段血条护盾+准星+雷达的战术抬头显示"},
    "rain":        {"label": "雨",         "category": "weather",  "audio": True,  "desc": "双层视差雨丝+冷调压暗+雨声"},
    "snow":        {"label": "雪",         "category": "weather",  "audio": True,  "desc": "双层视差柔雪花缓降+风噪"},
    "fog":         {"label": "雾",         "category": "weather",  "audio": True,  "desc": "perlin 流动体积雾团翻卷+低频环境音"},
    "light_sweep": {"label": "斜光扫过",   "category": "light",    "audio": False, "desc": "柔和高光斜带周期掠过，通用质感★"},
    "fireflies":   {"label": "萤火虫",     "category": "particle", "audio": False, "desc": "黄绿柔光乱漂+各自明灭呼吸"},
    "sparkles":    {"label": "星辰",       "category": "particle", "audio": False, "desc": "冷白星点固定不动只闪烁，星空/魔法梦幻"},
}


def catalog() -> list[dict]:
    """全量特效目录（按 EFFECTS 注册顺序）：每项含 key/中文 label/类别/是否带环境音/一句话描述。
    这是特效可发现性的单一真源——CLI、Studio 选择器、漂移守卫都从这里取。"""
    out = []
    for key in EFFECTS:
        m = EFFECT_META.get(key, {"label": key, "category": "texture",
                                  "audio": False, "desc": ""})
        out.append({"key": key, "label": m["label"], "category": m["category"],
                    "category_label": CATEGORY_LABELS.get(m["category"], m["category"]),
                    "audio": m["audio"], "desc": m["desc"]})
    return out
