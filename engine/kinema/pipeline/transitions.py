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

"""转场系统：两镜直接硬切效果差（无首尾帧衔接时尤甚）——用「转场镜」承接。

设计（转场即特殊镜，零侵入复用全部时间轴机制）：
· shots[] 里的 `{"kind": "transition", "dur": 1.6, "narration": "",
  "transition": {"type": "fade_black", "text": "一天后"}}` 就是一个转场镜；
· `narration` 为空 → 引擎既有的「纯画面镜静音占位」让旁白/字幕/BGM 时间轴自动对齐
  animatic/正式合成走同一条路径，无任何特殊分支；
· 生图/配音/图生视频阶段跳过转场镜（零 API 成本）——字卡由 compose 本地渲染；
· 相邻镜自动加**边缘淡化**：前镜尾部淡出到字卡底色 → 字卡（文字自身呼吸式淡入淡出）
  → 后镜头部从底色淡入——observable 效果即用户口径：
  「画面慢慢变暗 → 黑场中央显示"一天后" → 慢慢显现下一段」。

九种内置 + 素材路线（总时长 = 前镜淡出 edge + 停顿/字卡 dur + 后镜淡入 edge）：
  seamless    无缝转场（**Studio 弹层默认**）：几帧的均匀阶梯柔切，看上去就是直接
              切换、只去掉硬切的硬边；edge=0 不动相邻镜、缺省静音、总 0.1s
  fade        极简黑场呼吸（**无字缺省**）：慢慢黑下去再慢慢亮起来，总时长 ≈0.5s
              （0.2 淡出 + 0.1 黑场 + 0.2 淡入）——两镜硬切的最轻量解，纯 Python 零成本
  fade_black  渐黑字卡（**带字缺省**）：总时长 ≈1s（0.25 淡出 + 0.5 黑场居中显示
              「几天后」类文字 + 0.25 淡入）——时间跳跃的经典表达
  fade_white  白闪字卡（回忆/闪回/梦境）
  wipe/circle/slide/blur  冻结帧 xfade 族：前镜尾帧+后镜首帧静帧过渡（edge=0 像素连续）
  scan        轮廓扫描：霓虹亮条掠过、画面解析成发光轮廓线稿（缺省配 riser 音效）
  clip        素材转场：引用一段无字转场视频（**仅用户明确要求 AI 过场动画时**才
              先用 Seedance 生成存 assets/transitions/，此处引用拼接；音效仅
              dubbed/native 模式随片段保留——kenburns 模式主音轨走旁白+BGM）
后续加内置类型 = TRANSITIONS 注册表加一行 + render_card 支持其底色即可。
转场短音效三级解析：B 外置音效库 music/sfx/（config/audio.yaml 注册表，CC0 专业素材）
→ A 纯 ffmpeg 合成 whoosh_audio（缺文件自动兜底）→ C 用户点名 `sfx gen` AI 生成落库。
"""
from __future__ import annotations

from pathlib import Path

from ..audio_registry import library_root, load_registry
from ..errors import ProjectError
from ..ffmpeg import drawtext_text, filter_literal, first_frame, last_frame, probe_json, run
from ..fonts import resolve_font

# 注册表：type → 底色/文字色/相邻镜缺省边缘淡化秒数/停顿（字卡/过渡）缺省秒数。
# family=xfade 的类型走「冻结帧 xfade」：取前镜尾帧+后镜首帧做静帧过渡——
# concat 架构下零成本获得 xfade 级转场（画面像素与切点完全连续，edge=0 不动相邻镜）。
TRANSITIONS = {
    # seamless 排注册表第一行是接口约定：catalog() 按插入顺序输出，Studio 弹层
    # 默认选中第一项——「无缝转场是默认推荐」由此天然成立，前端零硬编码。
    # sound 显式 off：几帧长的柔切配不上一声「呼」，音效只在用户显式选择时给。
    "seamless":   {"label": "无缝转场", "bg": "black", "fg": "white",
                   "edge": 0.0, "dur": 0.1, "family": "xfade",
                   "xfade": "fade", "sound": "off"},
    "fade":       {"label": "极简黑场", "bg": "black", "fg": "white",
                   "edge": 0.2, "dur": 0.1},
    "fade_black": {"label": "渐黑字卡", "bg": "black", "fg": "white",
                   "edge": 0.25, "dur": 0.5},
    "fade_white": {"label": "白闪字卡", "bg": "white", "fg": "0x30343b",
                   "edge": 0.3, "dur": 0.6, "sound": "shimmer"},
    "wipe":       {"label": "对角翻页", "bg": "black", "fg": "white",
                   "edge": 0.0, "dur": 0.7, "family": "xfade"},
    "circle":     {"label": "圆形开合", "bg": "black", "fg": "white",
                   "edge": 0.0, "dur": 0.6, "family": "xfade", "xfade": "circleopen"},
    "slide":      {"label": "横向推移", "bg": "black", "fg": "white",
                   "edge": 0.0, "dur": 0.5, "family": "xfade", "xfade": "slideleft"},
    "blur":       {"label": "柔焦叠化", "bg": "black", "fg": "white",
                   "edge": 0.0, "dur": 0.6, "family": "xfade", "xfade": "hblur"},
    "scan":       {"label": "轮廓扫描", "bg": "black", "fg": "white",
                   "edge": 0.0, "dur": 0.9, "family": "xfade", "sound": "riser"},
    "clip":       {"label": "素材转场", "bg": "black", "fg": "white",
                   "edge": 0.4, "dur": 1.6},
}
DEFAULT_TYPE = "fade_black"   # spec 未知类型的容错回落（能显字的最安全档）
# 对角翻页方向 → xfade 过渡名（方向语义 = 新画面沿对角线从哪个角掀入）。
# 用 diag*（真·对角直线掀页）而非 wipe*（矩形角扫）——配合单段直切=干净翻书感。
_WIPE_DIR = {"tl": "diagtl", "tr": "diagtr", "bl": "diagbl", "br": "diagbr"}
_SLIDE_DIR = {"left": "slideleft", "right": "slideright",
              "up": "slideup", "down": "slidedown"}

# ---------------------------------------------------------------------------
# 转场目录元数据（Studio 类型选择器 / CLI / 守卫共用真源；键与 TRANSITIONS 锁步）。
# 每型的前端可选项：方向（部分 xfade 型）、主色（wipe/scan）、一句话描述、文字定位
# （card=字卡文字是主体·overlay=可选叠字）。缺省音效取 TRANSITIONS[type] 的 sound。
# ---------------------------------------------------------------------------
_DIRECTIONS = {
    "wipe":  [("tl", "左上"), ("tr", "右上"), ("bl", "左下"), ("br", "右下")],
    "slide": [("left", "向左"), ("right", "向右"), ("up", "向上"), ("down", "向下")],
    "scan":  [("up", "上扫"), ("down", "下扫")],
}
_COLORS = {
    # wipe 单段对角直切无色卡，不提供主色；scan 决定轮廓霓虹色
    "scan": [("green", "霓虹绿"), ("blue", "霓虹蓝")],
    "fade_black": [("black", "黑底白字"), ("white", "白底深灰字")],
    "fade_white": [("white", "白底深灰字"), ("black", "黑底白字")],
}
# 字卡族的成对底/字色档：bg 与 fg 是成对语义（注册表里 fade_black=black/white、
# fade_white=white/深灰），只覆盖 bg 的结局是 `--color white` 渲出纯白底纯白字。
_CARD_COLORS = {"black": ("black", "white"), "white": ("white", "0x30343b")}
_DESC = {
    "seamless":   "≈3 帧柔切·基本等于直接切换，只去掉硬切的硬边（总 0.1s；@30fps 约 3 帧）",
    "fade":       "无字缺省·最简过场，黑场一呼一吸（总~0.5s）",
    "fade_black": "有字缺省·渐黑到最黑处显字，叙事跳转常用（总~1s）",
    "fade_white": "过曝白场闪一下再显字，回忆/闪回",
    "wipe":       "色块沿对角席卷、再掀开露出新画面",
    "circle":     "新画面从圆心绽开／收合",
    "slide":      "新画面把旧画面推走",
    "blur":       "散焦交叉溶解，柔和叠化",
    "scan":       "霓虹亮条掠过，扫过处画面成发光线稿",
    "clip":       "AI 过场视频（需先备素材，Studio 选择器暂不含）",
}
# 文字定位：card=字卡型文字是主角（缺省该带字）；overlay=xfade 型文字为可选叠字
_TEXT_ROLE = {"fade_black": "card", "fade_white": "card"}
# 柔度档位：部分类型允许在弹层里直接选时长档（写 shots[].dur，不进 transition spec）。
# 与 _DIRECTIONS/_COLORS 同构——某型不登记即前端不显示该行。档位标签按秒表述不写
# 帧数：fps 是项目级配置，注册表是 fps 无关的静态表；实际渲染按 frame_aligned 吸附整帧。
_DURATIONS = {
    "seamless": [(0.07, "利落"), (0.1, "标准"), (0.17, "柔和")],
}
# 音效值 → 中文名（选择器展示；键与 _valid_sounds 锁步，缺文件时合成兜底见 whoosh_audio）
_SOUND_LABELS = {"whoosh": "呼·横掠", "riser": "吸·上升蓄势", "boom": "咚·低频落点",
                 "swish": "轻扫", "deep": "重扫", "glitch": "故障", "shimmer": "微光",
                 "pop": "啵·弹出", "ding": "叮·提示铃", "page": "翻页", "paper": "撕纸",
                 "impact": "砰·重击", "slash": "刃·挥砍", "heartbeat": "心跳", "wind": "风声",
                 "magic": "术·魔法闪光", "clock": "嗒·钟表滴答", "camera": "咔·相机快门",
                 "off": "静音"}


def catalog(include_clip: bool = False) -> list[dict]:
    """全量转场目录（按 TRANSITIONS 注册顺序）：key/中文 label/family/方向选项/主色选项/
    缺省音效/文字定位/一句话描述。Studio 类型选择器的单一真源——前端零硬编码。
    clip 需素材、缺省不入选择器。"""
    out = []
    for key, base in TRANSITIONS.items():
        if key == "clip" and not include_clip:
            continue
        role = _TEXT_ROLE.get(key, "overlay")
        out.append({
            "key": key, "label": base["label"],
            "family": base.get("family", "card"),
            "directions": [{"value": v, "label": l} for v, l in _DIRECTIONS.get(key, [])],
            "colors": [{"value": v, "label": l} for v, l in _COLORS.get(key, [])],
            "sound": base.get("sound", "whoosh"),
            "dur": base["dur"],                       # 该型缺省时长（弹层据此高亮默认档）
            "durations": [{"value": v, "label": l} for v, l in _DURATIONS.get(key, [])],
            "text_role": role,
            # 能否加字：仅字卡型（fade_black/fade_white）有停顿显字；其余（极简黑场太短、
            # xfade 族一次性无停顿）加字无意义——前端据此显隐文案输入框。
            "text_ok": role == "card",
            "desc": _DESC.get(key, ""),
        })
    return out


def sound_catalog() -> list[dict]:
    """合法转场音效目录（值+中文名），供选择器；顺序：三色板 → 外置扩展键 → off。
    与 _valid_sounds() 锁步（外置键随 config/audio.yaml 注册表增减）。"""
    order = ["whoosh", "riser", "boom"]
    extra = sorted(_valid_sounds() - {"whoosh", "riser", "boom", "off"})
    out = [{"value": k, "label": _SOUND_LABELS.get(k, k)} for k in (*order, *extra)]
    out.append({"value": "off", "label": _SOUND_LABELS["off"]})
    return out


def pick_type(text: str | None = None, asset: str | None = None,
              explicit: str | None = None) -> str:
    """缺省类型选择：显式指定 > 有素材=clip > 有文字=fade_black > 无字=fade（最简）。"""
    if explicit in TRANSITIONS:
        return explicit
    if asset:
        return "clip"
    return "fade_black" if (text or "").strip() else "fade"


def default_dur(ttype: str) -> float:
    """该类型的停顿（字卡）缺省秒数——总时长另加两侧 edge。"""
    return TRANSITIONS.get(ttype, TRANSITIONS[DEFAULT_TYPE])["dur"]


# 转场镜 dur 的合法区间：下限≈1 帧@30fps；上限防手改 JSON 的越界值撑爆成片时长。
MIN_DUR, MAX_DUR = 0.03, 10.0


def resolve_dur(ttype: str, dur=None) -> float:
    """转场镜 dur 归一：未给 → 该型缺省；给了 → 钳制到 [MIN_DUR, MAX_DUR]。
    与 spec_of 同一条纪律——非法值自动回落，绝不让合成炸在配置错字上。"""
    if dur in (None, ""):
        return default_dur(ttype)
    try:
        return min(MAX_DUR, max(MIN_DUR, float(dur)))
    except (TypeError, ValueError):
        return default_dur(ttype)


def frame_aligned(dur: float, fps: int) -> float:
    """段时长吸附到整帧（n/fps，至少 1 帧）。

    ffmpeg 的 `-t` 对视频按「保留 PTS < t 的帧」截断、对音频按秒截断——时长不落在
    帧网格上时（如 fps=24 下的 0.1s），同一段的视频与音频会差出亚帧级长度，
    `concat -c copy` 逐段累积成音画漂移。

    **只在短过渡路径上调用**（见 `SHORT_MAX_FRAMES`）。字卡族与常规长度的 xfade
    刻意不吸附：段长改动哪怕不足一帧也是改了它们的输出，整帧吸附的影响面
    必须严格圈在短过渡路径之内。"""
    return max(1, round(float(dur) * fps)) / fps


def total_span(shot: dict) -> float:
    """转场总时长（用户口径）：淡出 + 停顿 + 淡入。"""
    sp = spec_of(shot)
    return round(sp["edge"] * 2 + float(shot.get("dur") or 0), 2)


def is_transition(shot: dict) -> bool:
    return (shot or {}).get("kind") == "transition"


def spec_of(shot: dict) -> dict:
    """转场镜 → 归一化 spec。未登记的 type 直接报错：CLI/Studio 写入口都只允许目录内的
    类型，手改出来的错字回落成黑场字卡是静默改片。"""
    t = dict(shot.get("transition") or {})
    if t.get("asset"):
        t.setdefault("type", "clip")
    ttype = t.get("type") or DEFAULT_TYPE
    if ttype not in TRANSITIONS:
        raise ProjectError(f"镜 {shot.get('id')} 的转场类型未登记: {ttype}"
                           f"（可选: {', '.join(TRANSITIONS)}）")
    base = TRANSITIONS[ttype]
    bg, fg = base["bg"], base["fg"]
    color = str(t.get("color") or "").strip().lower()
    neon = ""
    if color:
        if ttype == "scan":
            neon = color             # scan 的主色是轮廓霓虹，不挤占底/字色槽——
            #                          否则尾帧槽位退化为字卡时会渲出整屏纯色
        elif base.get("family", "card") == "card" and color in _CARD_COLORS:
            bg, fg = _CARD_COLORS[color]
        # 其余组合不消费 color：wipe 单段直切没有色卡；字卡族只认成对档
    return {"type": ttype, "text": (t.get("text") or "").strip(),
            "asset": t.get("asset"), "font": t.get("font"),
            "bg": bg, "fg": fg, "neon": neon,
            "family": base.get("family", "card"),
            "direction": (t.get("direction") or "").strip().lower(),
            "sound": (t.get("sound") if t.get("sound") in _valid_sounds()
                      else base.get("sound", "whoosh")),
            "edge": float(t.get("edge", base["edge"]))}


def edge_fades(shots: list[dict], i: int) -> tuple[float, str, float, str]:
    """第 i 个片段应带的边缘淡化：(淡入秒, 淡入色, 淡出秒, 淡出色)。

    只由**相邻转场镜**驱动：前一镜是转场 → 本镜头部从其底色淡入；
    后一镜是转场 → 本镜尾部淡出到其底色。转场镜自身不加边缘淡化
    （字卡文字有自己的呼吸节奏）。"""
    if is_transition(shots[i]):
        return 0.0, "black", 0.0, "black"
    fi, fic, fo, foc = 0.0, "black", 0.0, "black"
    if i > 0 and is_transition(shots[i - 1]):
        sp = spec_of(shots[i - 1])
        fi, fic = sp["edge"], sp["bg"]
    if i + 1 < len(shots) and is_transition(shots[i + 1]):
        sp = spec_of(shots[i + 1])
        fo, foc = sp["edge"], sp["bg"]
    return fi, fic, fo, foc


def build_card_filter(*, text: str, width: int, height: int, dur: float,
                      font: str | None = None, fg: str = "white") -> str:
    """字卡文字层 filtergraph（纯函数可测）：中央大字呼吸式淡入淡出。

    文字自身 alpha 三段：前 1/4（≤0.5s）淡入 → 恒定 → 末 1/4（≤0.5s）淡出，
    与相邻镜的边缘淡化叠出「暗下去 → 字浮现 → 字隐去 → 亮起来」的完整呼吸。
    排版两处工业字卡细节：短纯汉字文案自动**疏排字距**（细空格 U+2009，
    影视字卡惯例的疏朗排法，混排数字/标点不动）；同色低透明描边形成
    **柔和光晕**，去掉大字压在纯色底上的生硬硬边。"""
    text = (text or "").strip()
    if not text:
        return "null"   # 纯色停顿（无字转场）也是合法用法
    disp = text
    if 2 <= len(disp) <= 10 and all("一" <= ch <= "鿿" for ch in disp):
        disp = " ".join(disp)
    r = min(0.5, max(0.15, dur * 0.25))
    alpha = (f"if(lt(t\\,{r:.2f})\\,t/{r:.2f}\\,"
             f"if(gt(t\\,{dur - r:.2f})\\,({dur:.2f}-t)/{r:.2f}\\,1))")
    size = max(30, round(height * 0.075))
    halo = max(2, size // 14)
    fontopt = f":fontfile={filter_literal(font)}" if font else ""
    return (f"drawtext=text={drawtext_text(disp)}:fontsize={size}"
            f":fontcolor={fg}{fontopt}:alpha='{alpha}'"
            f":borderw={halo}:bordercolor={fg}@0.18"
            f":shadowcolor=black@0.3:shadowx=1:shadowy=2"
            f":x=(w-text_w)/2:y=(h-text_h)/2")


def render_card(out: str | Path, *, spec: dict, dur: float, width: int, height: int,
                fps: int, with_audio: bool, profile: str | None = None) -> str:
    """渲染字卡片段（纯本地 lavfi 单段，零 API 成本）。

    编码参数与 kenburns 片段完全同参（libx264/yuv420p/同 fps；dubbed/native 下
    附静音 aac 44100 立体声）——concat -c copy 拼接要求各段流参数一致。

    段长按传入值原样用，**不做整帧吸附**：整帧吸附只属于 seamless 的短过渡路径，
    字卡族（fade/fade_black/fade_white/clip）恒走通用路径，输出不受其影响。"""
    vf = build_card_filter(text=spec["text"], width=width, height=height, dur=dur,
                           font=resolve_font(spec.get("font"), profile=profile),
                           fg=spec["fg"])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"color=c={spec['bg']}:s={width}x{height}:r={fps}:d={dur:.3f}"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.3f}"]
    args += ["-vf", f"{vf},format=yuv420p", "-t", f"{dur:.3f}", "-r", str(fps),
             "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
    args += [str(out)]
    run(args, desc=f"transition card {spec['type']}")
    return str(out)


# 短过渡分界（帧数）：线之上的八型缺省段长是 15~27 帧 @30fps，全部走通用公式，
# 输出不受短过渡路径影响；线之下只有 seamless
# 的三个柔度档（2/3/5 帧）。按**帧数**分界而不按类型名，是因为缺陷源于段长不源于
# 类型：通用 xfade 公式（duration=dur*0.9 · offset=dur*0.05）的第 0 帧恒是前镜末帧的
# 复刻，十几帧的过渡里这 1 帧不可感，三帧长的柔切里它就是切点上多顿的那一下
# （33ms@30fps，实测像素对比确认）。手动把某型调到几帧长时同样受益，无需登记。
SHORT_MAX_FRAMES = 8


def render_xfade_card(prev_clip: str | Path, next_clip: str | Path, out: str | Path, *,
                      spec: dict, dur: float, width: int, height: int, fps: int,
                      with_audio: bool, profile: str | None = None) -> str:
    """冻结帧 xfade 过渡段：前镜尾帧 → 后镜首帧，全单段直切（前帧直接过渡到后帧）。

    wipe（对角翻页）= diag* 沿对角直线把新画面掀入（像翻书直接翻过去）。
    **不走两段式黑卡**：色卡先盖满全屏、中途整帧变黑冒出一个黑矩形再掀开，
    观感差；单段 diag 直切干净利落。circle/slide/blur 各自 xfade。全部纯本地 ffmpeg。

    **段长 ≤SHORT_MAX_FRAMES 帧时**（实际只有 seamless 的三档）走另一条路：先吸附
    整帧，再用**均匀帧阶梯**——duration=(帧数+1)/fps、offset=0 让第 k 帧恰为
    k/(帧数+1) 的混合比，用 select 丢掉第 0 帧（纯前镜帧的复刻）；每帧都是新的混合比、
    零重复帧，select 留在 filtergraph 内做帧精确裁切，无 -ss 浮点边界风险。
    **这条线之上的八型走通用公式、不吸附帧**，输出不受短过渡路径影响。

    抽帧统一走 `ffmpeg.first_frame`/`last_frame`——末帧那条带「-sseof 取不到就按
    时长回退」，此处不维护第二份缺回退的抄本；临时帧图在 finally 清理，
    渲染失败（含 scan 分支）不留孤儿 PNG。"""
    tmp = Path(out).parent
    p_img = tmp / f"{Path(out).stem}_p.png"
    n_img = tmp / f"{Path(out).stem}_n.png"
    last_frame(prev_clip, p_img)
    first_frame(next_clip, n_img)
    try:
        # 短过渡判定取**传入的原始段长**，判完才吸附——先吸附再判会让恰好卡在分界上的
        # 段长因舍入跳档，同一个 dur 在不同 fps 下走不同的路
        short = round(dur * fps) <= SHORT_MAX_FRAMES
        if short:
            dur = frame_aligned(dur, fps)
        norm = f"scale={width}:{height},setsar=1,fps={fps},format=yuv420p"
        if spec["type"] == "scan":
            return _render_scan(p_img, n_img, out, spec=spec, dur=dur, width=width,
                                height=height, fps=fps, with_audio=with_audio, norm=norm)
        if spec["type"] == "wipe":
            tr = _WIPE_DIR.get(spec.get("direction") or "", "diagtr")   # 对角直切掀页
        elif spec["type"] == "slide" and spec.get("direction") in _SLIDE_DIR:
            tr = _SLIDE_DIR[spec["direction"]]
        else:
            tr = spec.get("xfade") or TRANSITIONS[spec["type"]].get("xfade", "fade")
        if short:
            frames = round(dur * fps)
            win = (frames + 1) / fps
            fc = (f"[0:v]{norm}[a];[1:v]{norm}[b];"
                  f"[a][b]xfade=transition={tr}:duration={win:.4f}:offset=0[x];"
                  f"[x]select='gte(n\\,1)',setpts=N/FRAME_RATE/TB[v]")
            p_t = win + 1 / fps            # 前镜帧输入必须盖满整个 xfade 窗，否则阶梯被截断
        else:
            fc = (f"[0:v]{norm}[a];[1:v]{norm}[b];"
                  f"[a][b]xfade=transition={tr}:duration={dur * 0.9:.3f}"
                  f":offset={dur * 0.05:.3f}[v]")
            p_t = dur
        inputs = ["-loop", "1", "-t", f"{p_t:.3f}", "-i", str(p_img),
                  "-loop", "1", "-t", f"{dur + 0.2:.3f}", "-i", str(n_img)]
        vmap = "[v]"
        if spec.get("text"):   # 过渡上叠字卡（呼吸式淡入淡出，同 render_card 口径）
            txt = build_card_filter(text=spec["text"], width=width, height=height,
                                    dur=dur, font=resolve_font(spec.get("font"),
                                                               profile=profile),
                                    fg=spec["fg"])
            fc += f";[v]{txt}[vt]"
            vmap = "[vt]"
        args = list(inputs)
        if with_audio:
            args += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.3f}"]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        args += ["-filter_complex", fc, "-map", vmap]
        if with_audio:
            args += ["-map", "2:a",           # 两个视频输入(p_img/n_img)之后的 anullsrc
                     "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
        args += ["-t", f"{dur:.3f}", "-r", str(fps),
                 "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", str(out)]
        run(args, desc=f"transition xfade {spec['type']}")
        return str(out)
    finally:
        for f in (p_img, n_img):
            Path(f).unlink(missing_ok=True)


# 扫描霓虹色：(亮条色, 轮廓 RGB 乘色系数)。用 RGB 乘色（灰度边缘 × 霓虹色）而非 lutyuv：
# 只有边缘（高亮度）被染色、黑底乘 0 保持**纯黑**——旧 lutyuv 给全帧定值色度把黑底也染成
# 整片绿/蓝。gbrp↔yuv420p 往返实测无紫底（关键是显式 format 收尾）。
_SCAN_COLORS = {"green": ("0x39FF14", "r='val*0.224':g='val':b='val*0.078'"),
                "blue":  ("0x21C7FF", "r='val*0.129':g='val*0.78':b='val'")}


def _band_y(scan_tr: str, height: int, band_h: int, d1: float) -> str:
    """扫描亮条 y 轨迹：与 wipe 边界同速——up=底→顶，down=顶→底。"""
    t0, dsp = d1 * 0.05, d1 * 0.9
    prog = f"((t-{t0:.3f})/{dsp:.3f})"
    if scan_tr == "wipeup":
        return f"{height}-{prog}*{height + band_h}"
    return f"-{band_h}+{prog}*{height + band_h}"


def _render_scan(p_img, n_img, out, *, spec, dur, width, height, fps,
                 with_audio, norm) -> str:
    """轮廓扫描过渡（赛博扫光）：扫描亮条自下而上掠过——扫过处旧画面被「解析」
    成霓虹辉光轮廓线稿（edgedetect Canny → 染绿/蓝 → gblur 辉光 → screen 叠回），
    线稿短暂停留后淡入新画面。人物/主体因对比度天然成为轮廓主角。
    纯 ffmpeg（无 AI 分割）；direction=up/down 扫向，color=green/blue。

    段长由 `render_xfade_card` 决定后原样传入（短过渡才吸附整帧）——此处不自行吸附：
    吸附与否由调用方一次定死，scan 缺省 27 帧走通用路径。"""
    neon, tint = _SCAN_COLORS.get(spec.get("neon") if spec.get("neon") in _SCAN_COLORS
                                  else "green", _SCAN_COLORS["green"])
    # spec.neon 承载 scan 的 color 参数（green/blue），底/字色槽不被挤占
    color_key = spec.get("direction") or ""
    scan_tr = "wipedown" if color_key == "down" else "wipeup"
    d1 = dur * 0.55          # 阶段1：扫描条掠过，画面逐行「轮廓化」
    d2 = dur - d1            # 阶段2：轮廓线稿 → 新画面
    band_h = max(6, height // 60)
    fc = (
        f"[0:v]{norm},split[pa][pb];"
        # 辉光轮廓线稿：Canny 勾线（白线黑底）→ 双层辉光（细晕 + 大光晕 screen 叠加）→
        # RGB 乘霓虹色（黑底乘 0 保持纯黑、只染轮廓线）→ 回 yuv420p 供 xfade
        f"[pb]format=gray,edgedetect=low=0.07:high=0.19,"
        f"format=gray,split[e1][e2];"
        f"[e1]gblur=sigma=1.2[eg1];[e2]gblur=sigma=7[eg2];"
        f"[eg1][eg2]blend=all_mode=screen,format=gbrp,lutrgb={tint},"
        f"format=yuv420p[edges];"
        f"[1:v]{norm}[nx];"
        # 阶段1：wipe 边界即「扫描进度」——原画面被逐行替换成轮廓线稿
        f"[pa][edges]xfade=transition={scan_tr}:duration={d1 * 0.9:.3f}"
        f":offset={d1 * 0.05:.3f}[ph1];"
        # 阶段2：线稿淡入新画面
        f"[ph1][nx]xfade=transition=fade:duration={d2 * 0.9:.3f}"
        f":offset={d1 + d2 * 0.05:.3f}[base];"
        # 扫描亮条与 wipe 边界同速移动（enable 仅阶段1）
        f"[2:v]format=rgba,colorchannelmixer=aa=0.55,gblur=sigma=3[band];"
        f"[base][band]overlay=x=0:y='{_band_y(scan_tr, height, band_h, d1)}'"
        f":enable='between(t,{d1 * 0.05:.3f},{d1 * 0.95:.3f})'[v]"
    )
    inputs = ["-loop", "1", "-t", f"{d1 + 0.2:.3f}", "-i", str(p_img),
              "-loop", "1", "-t", f"{dur + 0.2:.3f}", "-i", str(n_img),
              "-f", "lavfi", "-i", f"color=c={neon}:s={width}x{band_h}:r={fps}:d={dur:.3f}"]
    if with_audio:
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.3f}"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    args = inputs + ["-filter_complex", fc, "-map", "[v]"]
    if with_audio:
        args += ["-map", "3:a", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
    args += ["-t", f"{dur:.3f}", "-r", str(fps),
             "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", str(out)]
    run(args, desc="transition scan")
    for f in (p_img, n_img):
        Path(f).unlink(missing_ok=True)
    return str(out)


def whoosh_audio(span: float, *, kind: str = "whoosh") -> tuple[str, str]:
    """转场短音效（纯 ffmpeg 合成零素材依赖）。返回 (lavfi 输入, 滤镜链)——
    由 compose 按转场起点 adelay 进环境音混音通道（BGM/旁白之上，kenburns 也有效）。

    三种音色（sound 参数选，与画面动势匹配）：
      whoosh  「呼」——棕噪→低通聚拢成风声→快起缓落包络（缺省，扫/翻类通用）；
      riser   「吸」——whoosh 倒放（areverse）成上升蓄势感，适合掀开/揭示型转场；
      boom    「咚」——54Hz 低频正弦骤起缓衰 + 轻回声，适合黑场落点/章节重音。"""
    d = max(0.3, min(2.5, span))
    if kind == "boom":
        src = f"sine=frequency=54:r=44100:d={d:.3f}"
        filt = (f"afade=t=in:st=0:d=0.02,"
                f"afade=t=out:st={d * 0.12:.3f}:d={d * 0.88:.3f},"
                f"aecho=0.6:0.5:60:0.35,volume=2.2,"
                f"aformat=channel_layouts=stereo")
        return src, filt
    src = f"anoisesrc=color=brown:r=44100:d={d:.3f}"
    filt = (f"lowpass=f=700,highpass=f=90,"
            f"afade=t=in:st=0:d={d * 0.25:.3f},"
            f"afade=t=out:st={d * 0.35:.3f}:d={d * 0.65:.3f},"
            f"volume=1.6")
    if kind == "riser":
        filt += ",areverse"   # 倒放的呼 = 由远及近的吸气式蓄势
    return src, filt + ",aformat=channel_layouts=stereo"


# ---------------------------------------------------------------------------
# 转场音效声源（B 外置素材优先 · A 合成兜底 · C 点名 AI 生成落库）
# ---------------------------------------------------------------------------
# 注册表在 config/audio.yaml 的 sfx 段（audio_registry 统一读取，BGM 同源）；
# 媒体不入库（.gitignore）、缺资产时降级、库根 KINEMA_MUSIC_DIR 整体改址。


def _valid_sounds() -> set:
    """合法音效值 = 合成三色板 + off + 注册表 sfx.transitions 全部键（外置扩展键，
    如 swish/deep/glitch/shimmer——缺文件时 whoosh_audio 以「呼」兜底）。"""
    keys = ((load_registry().get("sfx") or {}).get("transitions") or {})
    return {"whoosh", "riser", "boom", "off", *keys}


def resolve_sound_file(kind: str, category: str = "transitions") -> Path | None:
    """B 路线：audio.yaml sfx 段查 kind 的文件且真实存在才返回路径；否则 None（合成兜底）。"""
    reg = load_registry()
    entry = ((reg.get("sfx") or {}).get(category) or {}).get(kind) or {}
    rel = entry.get("file") if isinstance(entry, dict) else entry
    if not rel:
        return None
    p = library_root(reg) / rel
    return p if p.is_file() else None


def fit_sound_filter(span: float) -> str:
    """外置音效适配链：统一采样/立体声 → 允许自然尾巴但钳制时长 → 尾部短淡出防咔哒。"""
    d = max(0.3, min(2.5, span)) + 0.4          # 比画面动势略长的自然收尾
    return (f"aresample=44100,aformat=channel_layouts=stereo,"
            f"atrim=0:{d:.3f},afade=t=out:st={max(0.0, d - 0.12):.3f}:d=0.12,"
            f"volume=0.9")


def fit_asset(src: str | Path, out: str | Path, *, dur: float, width: int, height: int,
              fps: int, with_audio: bool) -> str:
    """素材转场（如 Seedance 生成的无字转场视频）规整成可拼接片段。

    with_audio 时：素材自带音轨则保留（转场音效），没有则补静音——
    保证与其他片段流参数一致，concat 不炸。"""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},fps={fps},setsar=1,"
          f"tpad=stop_mode=clone:stop_duration=3600,format=yuv420p")
    has_aud = any(st.get("codec_type") == "audio"
                  for st in (probe_json(src) or {}).get("streams") or [])
    args = ["-i", str(src)]
    amap = None
    if with_audio and has_aud:
        amap = "0:a"
    elif with_audio:
        args += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.3f}"]
        amap = "1:a"
    args += ["-vf", vf, "-t", f"{dur:.3f}", "-r", str(fps),
             "-map", "0:v",
             "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if amap:
        args += ["-map", amap, "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                 "-ac", "2", "-af", f"aresample=44100,apad,atrim=0:{dur:.3f}"]
    else:
        args += ["-an"]
    args += [str(out)]
    run(args, desc=f"transition asset {Path(str(src)).name}")
    return str(out)
