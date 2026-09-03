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

"""封面系统（系列主视觉 key visual + 章节封面，默认 3:4 竖版 + 4:3 横版双套）。

设计哲学——**两段式**：
· 第一段照旧只画**无字背景画**（key visual 构图，底部留标题安全区）——它是
  `*_bg` 真源，Studio 项目卡浮层标题与 84px 章节缩略都必须消费无字版（防重字/防噪点）；
· 第二段（缺省）**AI 题字**：以无字背景为首参考图再生成一次，模型把标题画成
  动漫大片 logo 级的**设计元素**（质感字效/与画面光效融合）——这才是成品 `*.png`。
  ffmpeg drawtext 排版后置的路线（规避「AI 画字会错字」）不作缺省：Seedream 的
  中文题字恰恰是强项，白字贴图的排版观感实测不及 AI 题字（用户实测点名）；
  章节封面题字时把**系列封面成品**一并作参考并声明「字形沿用」，系列感照旧锁住。
· 逃生舱 `--typeset-title`：退回 ffmpeg 排版后置（零成本、字绝对精确）——
  AI 题字出了错字/漂移时的兜底；排版层代码原样保留。
· **题字必须人工/指挥层 Read 质检**：引擎无 OCR，错字只能靠眼睛抓，错了就
  `--force` 重滚或退逃生舱。

系列感三锚点（用户口径：一眼看出是同一部动漫的第几集）：
① 同一排版模板：主标题/集数字号、落位、描边、底部压暗全部同参；
② 章节背景以**系列封面背景**（series_bg，无字版）为首张参考图——风格/色板承接；
③ 同一画风前缀（profile 双语前缀）+ 系列 seed + 角色设定图参考。

顶级漫剧 key visual 构图 DNA（提示词模板内置，中英双语，**七条工法栈**——判据来自
Netflix key art 封面研究、动画 key visual 构图理论与 2024-2025 国漫海报拆解
（数英：繁多元素堆叠/中心主体+环绕式构成/撞色反差/明暗对比四大主流打法），校准）：
① 层级比例 hieratic scale：主角最大最前、脸是全画面最亮最实的焦点、**表情必须有戏**
  （Netflix 的封面研究：表情强烈的特写脸赢过一切；面无表情站桩是最差解）；配角按重要度递减、
  人数宁少勿挤（拥挤群像=「看不出这部剧讲什么」，Houseki no Kuni 反例）；
② 三层纵深 + 环境叙事：前景粒子浅景深 / 中景人物 / 背景**巨大的世界观符号或威胁意象**
  ——用尺度对比讲「世界之大与险」（environmental storytelling）；
③ 叙事瞬间：画面要一眼读出剧讲什么，人物与背景威胁有视线或姿态呼应，**构图带对角线动势**，
  **绝不全员正对镜头排排站**（失败形态：四人一排站桩）；
④ 元素编排：以主角为中心的**环绕式元素层**——本剧独有的世界观标志物/能量流/发光符纹
  绕人物错落环布（近大远小近亮远暗），撑起密度与壮观感但绝不遮脸
  （「单调感」的根因就是这一层空缺：只有人物+天象，没有题材符号在画面里流动）；
⑤ 框架性前景：画框边缘虚化残片/光效碎屑形成半包围边框，锁视线+造空间感（框式构图）；
⑥ 色彩战略：一个主导色统治画面 + 一处对撞色锚点；冷退暖进分离人物与背景，明暗对比强烈；
⑦ 电影级布光：边缘光勾轮廓、体积光斜射、亮部集中在主角面部。
· 底部约三分之一构图低密度（标题安全区）、顶部留呼吸空间；
· 杂志封面级细节；防字地板（负向意图写进正向 prompt）。

产物落 `project/<pid>/assets/covers/`：`series_bg.png`（无字真源）+ `series.png`（成品）、
`<cid>_bg.png` + `<cid>.png`。注册：系列文档顶层 `cover` 块、章节文档 `cover` 字段
（均存**工作区相对路径**，与 OSS Key 映射同构）。
"""
from __future__ import annotations

from pathlib import Path

from ..errors import ProjectError
from ..ffmpeg import drawtext_text, filter_literal, run
from ..fonts import resolve_font

# 常用封面比例 → 像素（缺省一次出 3:4 竖版 + 4:3 横版两套；其余比例自由传参）
COVER_SIZES = {"3:4": (1080, 1440), "4:3": (1440, 1080), "2:3": (1080, 1620),
               "3:2": (1620, 1080), "1:1": (1080, 1080),
               "9:16": (1080, 1920), "16:9": (1920, 1080)}
DEFAULT_ASPECT = "3:4"
DEFAULT_ASPECTS = ("3:4", "4:3")   # 竖向 + 横向双默认


def size_for(aspect: str) -> tuple[int, int]:
    """比例串 → 像素。预设表优先；任意 "宽:高" 比例按短边 1080 推导（偶数对齐），
    AI/用户可自由传任何比例（如 21:9、2:1）无需改表。"""
    if aspect in COVER_SIZES:
        return COVER_SIZES[aspect]
    a, b = (float(x) for x in aspect.replace("：", ":").split(":"))
    if a <= 0 or b <= 0:
        raise ProjectError(f"非法封面比例: {aspect}")
    if a >= b:   # 横版：高 1080
        w, h = round(1080 * a / b / 2) * 2, 1080
    else:        # 竖版：宽 1080
        w, h = 1080, round(1080 * b / a / 2) * 2
    return w, h

# ---------------------------------------------------------------------------
# 提示词拼装（key visual 构图 DNA）
# ---------------------------------------------------------------------------
# 五条工法栈（模块 docstring 有判据出处）——这是封面的「摄影地板」，
# 与 desc（指挥层写的画面创意）互补：desc 说画什么，DNA 管怎么画才顶级。
# 工法栈按「画什么世界」分两档：动画档的世界观符号、能量流与发光符纹是 key art
# 的常规语汇；写实档（identity_sheet 的照片级媒介）没有这些东西可画，同一段 DNA
# 会让模型给一部便利店夜戏配上龙骨与符文。共用的层级/叙事/前景/色彩/布光条款只
# 写一份，纵深与元素编排两条按档切换。
_DNA_ZH_HEAD = (
    "层级比例——主角最大最前约占画面六成高，脸部是全画面最亮最实的视觉焦点，"
    "表情必须有戏（决意、紧绷、回望的瞬间神情，绝不面无表情），"
    "配角按重要度递减缩小、错落斜后成纵深，人数宁少勿挤；")
_DNA_ZH_DEPTH = {
    "anime": "三层纵深——前景飘散发光粒子与浅景深虚化，中景人物层，"
             "背景是吞没画面的巨大世界观符号或威胁意象，用尺度对比讲出世界之大与险；",
    "real": "三层纵深——前景是取景地里真实存在的近物（雨丝、玻璃反光、蒸汽、门框或"
            "货架边缘）虚化成浅景深，中景人物层，背景是本剧取景地的真实环境，"
            "用透视、光源层次与尺度对比讲出空间感；",
}
_DNA_ZH_NARRATIVE = (
    "叙事瞬间——画面要一眼读出这部剧讲什么，人物与背景意象之间有视线或姿态的呼应，"
    "构图带对角线动势，绝不全员正对镜头排排站；")
_DNA_ZH_ELEMENTS = {
    "anime": "元素编排——以主角为中心的环绕式元素层：这部剧独有的世界观标志物、"
             "能量流与发光符纹绕人物错落环布、近大远小近亮远暗，撑起画面密度与壮观感但绝不遮脸；",
    "real": "元素编排——环绕人物的只有取景地里真实存在的物件与光源（灯具、雨水、水汽、"
            "反光、随身物件），近大远小近亮远暗撑起画面密度，不添加能量流、发光符纹、"
            "粒子特效或任何世界观符号，绝不遮脸；",
}
_DNA_ZH_TAIL = (
    "框架性前景——画框边缘有虚化的前景残片或光效碎屑形成半包围边框，把视线锁向主体；"
    "色彩战略——一个主导色统治全画面，一处对撞色点缀制造视觉锚点，"
    "冷色退后暖色前进把人物从背景里分离出来，明暗对比强烈；"
    "电影级布光——边缘光勾勒人物轮廓，体积光从斜后方射入，亮部集中在主角面部；"
    "杂志封面级细节密度与质感，"
    "画面底部三分之一保持低密度留白供标题排版，顶部留出呼吸空间。"
    "避免出现：任何文字、字母、数字、标志、水印、边框、签名")
_DNA_EN_HEAD = (
    "hieratic scale — the protagonist largest and "
    "closest at about sixty percent of frame height, their face the brightest and sharpest "
    "focal point with a charged expression (resolve, tension, a glance back — never a blank "
    "face), supporting cast scaled down by importance and staggered behind in depth, fewer "
    "figures over a crowd; ")
_DNA_EN_DEPTH = {
    "anime": "three-layer depth — glowing foreground particles with shallow "
             "depth of field, a mid-ground character layer, and a vast world-symbol or looming threat "
             "swallowing the background, scale contrast telling how big and dangerous this world is; ",
    "real": "three-layer depth — real near objects from the location (rain streaks, glass "
            "reflections, steam, a door frame or shelf edge) blurred into shallow depth of field, "
            "a mid-ground character layer, and the story's real location as the background, "
            "perspective, layered light sources and scale contrast conveying the space; ",
}
_DNA_EN_NARRATIVE = (
    "a narrative instant — the frame must read at a glance what the story is about, with "
    "eyelines or postures connecting characters to the background image and a diagonal "
    "sense of motion in the composition, never the whole cast lined up facing the camera; ")
_DNA_EN_ELEMENTS = {
    "anime": "element orchestration — an orbit of this story's signature motifs, energy streams and "
             "glowing sigils staggered around the protagonist, larger and brighter when near, "
             "building density and spectacle without ever covering faces; ",
    "real": "element orchestration — only objects and light sources that really exist on the "
            "location orbit the characters (lamps, rain, vapour, reflections, carried items), larger "
            "and brighter when near, no energy streams, glowing sigils, particle effects or "
            "world symbols of any kind, never covering faces; ",
}
_DNA_EN_TAIL = (
    "framing foreground — "
    "blurred debris or light shards at the frame edges forming a half-enclosing border "
    "that locks the eye onto the subject; color strategy — one dominant hue ruling the frame "
    "with a single clashing accent as the visual anchor, cool receding and warm advancing "
    "to separate figures from the background, with strong chiaroscuro contrast; "
    "cinematic lighting — rim light outlining figures, "
    "volumetric light from behind, highlights concentrated on the protagonist's face; "
    "magazine-cover level detail and polish, keep the bottom third of the frame "
    "low-density for title typography and leave breathing room at the top. "
    "Avoid: any text, letters, numbers, logos, watermarks, borders, signatures")


def _dna(lang: str, photoreal: bool) -> str:
    kind = "real" if photoreal else "anime"
    if lang == "en":
        return (("top-tier photoreal film key visual poster craft: " if photoreal
                 else "top-tier anime key visual poster craft: ")
                + _DNA_EN_HEAD + _DNA_EN_DEPTH[kind] + _DNA_EN_NARRATIVE
                + _DNA_EN_ELEMENTS[kind] + _DNA_EN_TAIL)
    return (("顶级电影海报 key visual 构图工法：" if photoreal else "顶级动画海报 key visual 构图工法：")
            + _DNA_ZH_HEAD + _DNA_ZH_DEPTH[kind] + _DNA_ZH_NARRATIVE
            + _DNA_ZH_ELEMENTS[kind] + _DNA_ZH_TAIL)


_DNA_ZH = _dna("zh", False)
_DNA_EN = _dna("en", False)


def _backdrop(series_data: dict) -> str:
    """封面背景描述：顶层 `scene` 优先，空则取登记的取景地（`scenes[]` 前两处）。

    取景地是分镜绑定的正式登记位，顶层 `scene` 只是没绑具名场景时的全局描述——
    只读顶层的话，正规登记了取景地的项目反而没有背景，工法栈里的世界观意象会
    自行编造一个与本剧无关的世界。"""
    scene = (series_data.get("scene") or "").strip()
    if scene:
        return scene
    descs = []
    for sc in (series_data.get("scenes") or [])[:2]:
        if isinstance(sc, dict):
            d = (sc.get("desc") or sc.get("name") or "").strip()
            if d:
                descs.append(d)
    return "；".join(descs)


def _cast(characters: list[dict], lang: str = "zh", limit: int = 3,
          names: list[str] | None = None) -> str:
    """角色阵容描述：主角在前，配角随后（外貌摘要压缩到短语级）。

    两条选人纪律（判据见模块 docstring 的 key visual 研究）：
    · **主角优先**——`role` 含「主」的角色（男主/女主/主角）排最前，其余按登记序；
      长篇改编的 roster 几十人、登记序不等于戏份序，按序取前 N 会漏掉排位靠后的主角组；
    · **上限 3 人**——Netflix 实测「清晰单焦点赢过拥挤群像」，四人以上一字排开正是
      本仓库失败的那张「站桩全家福」；要更多人上封面由指挥层在 desc 里显式点名。

    `names` 是显式点名的阵容（`cover --cast`），给了就按给定顺序全收，主角排序
    与上限均不适用；空列表=不注入阵容句。`role` 只参与缺省排序、没有排除语义
    ——正好 3 人的项目按缺省怎么设 role 都是全员上封面，desc 里写「不要出现某
    角色」压不住引擎注入的阵容句（带全文外观的正向指令比否定句强势），要撵人
    只有显式点名这一条路。"""
    if names is not None:
        by_name = {c.get("name"): c for c in characters if c.get("name")}
        picked = [by_name[n] for n in names if n in by_name]
        parts = []
        for i, c in enumerate(picked):
            role = ("主角" if i == 0 else "配角") if lang == "zh" \
                else ("protagonist " if i == 0 else "supporting ")
            app = (c.get("appearance") or "").strip().replace("\n", " ")
            app = (f"（{app[:40]}）" if lang == "zh" else f" ({app[:60]})") if app else ""
            parts.append(f"{role}{c['name']}{app}")
        return ("、" if lang == "zh" else ", ").join(parts)
    named = [c for c in characters if c.get("name")]
    leads = [c for c in named if "主" in (c.get("role") or "")]
    rest = [c for c in named if c not in leads]
    parts = []
    for i, c in enumerate((leads + rest)[:limit]):
        role = ("主角" if (i == 0 or c in leads) else "配角") if lang == "zh" \
            else ("protagonist " if (i == 0 or c in leads) else "supporting ")
        app = (c.get("appearance") or "").strip().replace("\n", " ")
        app = (f"（{app[:40]}）" if lang == "zh" else f" ({app[:60]})") if app else ""
        parts.append(f"{role}{c['name']}{app}")
    return ("、" if lang == "zh" else ", ").join(parts)


def _orientation(aspect: str, lang: str) -> str:
    """构图方向词（提示词首句用）：**唯一随画幅分支的措辞**——DNA 与题字段的
    留白措辞都是画幅中性的。缺省双比例集里 4:3 横画布若也收到「竖版海报构图」，
    模型会在横画布上挤出竖版构图的留白。"""
    try:
        w, h = (int(x) for x in str(aspect).split(":"))
    except (TypeError, ValueError):
        w, h = 3, 4
    if lang == "en":
        return "vertical" if h > w else ("horizontal" if w > h else "square")
    return "竖版" if h > w else ("横版" if w > h else "方形")


def cover_prompt(series_data: dict, *, chapter_title: str = "", desc: str = "",
                 style_prefix: str = "", lang: str = "zh",
                 ref_base: bool = False, aspect: str = "3:4",
                 cast_names: list[str] | None = None,
                 photoreal: bool = False) -> str:
    """封面背景提示词：画风前缀 + 阵容 + 姿态/氛围 + 参考契约 + 场景 + 构图 DNA。

    `photoreal` 是写实档（`image.identity_sheet`）：工法栈换成电影海报那一档，
    背景与环绕元素只取自取景地的真实陈设与光源。

    系列与章节封面 desc（Skill 层/用户给的画面描述）都优先；缺省系列=叙事定妆瞬间、
    章节=按标题给氛围句——引擎只做机械拼装，创意描述由指挥层供给。

    `ref_base=True` 表示本次请求**真的附带了设定图参考**（cli 按 _refs 实况传入）——
    复用图侧 REF_BASE 契约句（prompts.py 单一真源）：外观以设定图为准、绝不照搬版式、
    **desc 未提及的角色不画**。缺它的失败形态：某配角的蓝色设定图在参考里、提示词却
    没提它，模型把这只自由发挥的吉祥物顺着 DNA 的「暖色前进」染成了橙色布丁。"""
    from .prompts import REF_BASE_EN, REF_BASE_ZH
    chars = series_data.get("characters") or []
    scene = _backdrop(series_data)
    cast = _cast(chars, lang, names=cast_names)
    if style_prefix:   # select_style_prefix 会剥尾部标点——补回分隔符再拼正文
        style_prefix = style_prefix.rstrip("，, ") + (", " if lang == "en" else "，")
    if lang == "en":
        action = desc or (
            f"episode mood: {chapter_title}, each character in a pose and expression "
            "that fits this episode" if chapter_title else
            ("the protagonist caught in a charged story moment — glancing back, pausing, "
             "or mid-way through the single most typical action of this story — supporting "
             "cast echoing it with eyelines or posture, everyone connected to the location "
             "behind them" if photoreal else
             "the protagonist caught in a charged story moment — glancing back, bracing "
             "toward the looming threat, or mid-motion — supporting cast echoing that "
             "momentum, everyone connected to the world imagery behind them"))
        body = (f"{_orientation(aspect, lang)} "
                + ("photoreal film key visual poster composition: " if photoreal
                   else "anime key visual poster composition: ")
                + (f"{cast}, " if cast else "") + f"{action}; "
                + (f"{REF_BASE_EN}; " if ref_base else "")
                + (f"backdrop of {scene}, " if scene else "")
                + _dna(lang, photoreal))
        return f"{style_prefix}{body}" if style_prefix else body
    action = desc or (
        f"本集氛围：{chapter_title}，各角色摆出贴合本集剧情的动作与表情" if chapter_title
        else ("主角定格在一个有故事的瞬间——回望、停顿或正在做本剧里最典型的那个动作，"
              "配角以视线或姿态呼应主角，所有人与身后的取景地之间有呼应" if photoreal
              else "主角定格在一个有故事的瞬间——回望、迎向威胁或蓄势起手，"
                   "配角以动势呼应主角，所有人与身后的世界观意象之间有呼应"))
    body = (f"{_orientation(aspect, lang)}"
            + ("电影海报主视觉构图：" if photoreal else "动画海报主视觉构图：")
            + (f"{cast}，" if cast else "") + f"{action}；"
            + (f"{REF_BASE_ZH}；" if ref_base else "")
            + (f"背景为{scene}，" if scene else "")
            + _dna(lang, photoreal))
    return f"{style_prefix}{body}" if style_prefix else body


# ---------------------------------------------------------------------------
# AI 题字（第二段：无字 key visual → 带设计级标题的成品）
# ---------------------------------------------------------------------------
def title_art_prompt(title: str, *, subtitle: str = "", style_prefix: str = "",
                     lang: str = "zh", series_ref: bool = False) -> str:
    """题字段提示词：以无字背景为基准，只做「嵌入标题设计」这一件事。

    三条硬约束（每条都对应一种实测/可预见的失败）：
    · **画面保持不变**——不声明就是整幅重画，key visual 白出；
    · **字数钉死 + 逐字复述**——AI 画中文最常见的错是多字/漏字/形近字，
      把「恰好 N 个字」和标题原文各说一遍，错字率显著下降；
    · **除标题外禁一切其他文字**——防字地板换成「白名单式」（标题在白名单内）。
    `series_ref=True`（章节题字）时声明「字形沿用第二张参考图的标题设计」——
    系列感从 drawtext 的同参数排版，换成参考图驱动的同字形。"""
    title = (title or "").strip()
    if not title:
        raise ProjectError("题字标题为空")
    n = len(title)
    if style_prefix:
        style_prefix = style_prefix.rstrip("，, ") + (", " if lang == "en" else "，")
    if lang == "en":
        body = (
            "Use the first reference image as the exact base: keep the artwork, "
            "composition, characters and lighting completely unchanged. Do exactly one "
            f"thing — embed the main title \"{title}\" ({n} characters, no more, no less) "
            "into the low-density area of the lower frame: blockbuster anime logo-level "
            "title design, every stroke correct and clearly readable, a designed typeface "
            "with dynamic energy, material effects drawn from the artwork's dominant "
            "palette (metallic sheen, glowing rim, subtle halo), the title fused "
            "naturally with the scene lighting yet contrasted enough to read at a glance"
            + (f'; below the title add a smaller subtitle "{subtitle}"' if subtitle else "")
            + ("; the title lettering must follow the exact same typeface and material "
               "style as the title in the second reference image" if series_ref else "")
            + ". Apart from the title"
            + (" and subtitle" if subtitle else "")
            + ", no other text, letters, numbers, watermarks, borders or signatures "
              "anywhere in the frame")
        return f"{style_prefix}{body}" if style_prefix else body
    body = (
        "以所给第一张图为画面基准，画面内容、构图、人物与光效完全保持不变，"
        f"只做一件事：在画面中下部的低密度留白区嵌入大标题「{title}」"
        f"——标题恰好 {n} 个汉字、一字不多一字不少，逐字为「{'、'.join(title)}」，"
        "每个字笔画正确、清晰可读；动漫大片 logo 级标题设计：字形有设计感与动势，"
        "带与画面主色系同源的质感字效（金属质感、发光描边、淡淡光晕），"
        "标题与画面光效自然融合、边缘干净，与背景对比度足以一眼读出"
        + (f"；标题正下方以小一号字排副标题「{subtitle}」" if subtitle else "")
        + ("；标题字形沿用第二张参考图中的标题设计——同一字体、同一质感、同一色系"
           if series_ref else "")
        + "。除上述标题"
        + ("与副标题" if subtitle else "")
        + "外，画面中不出现任何其他文字、字母、数字、水印、边框、签名")
    return f"{style_prefix}{body}" if style_prefix else body


# ---------------------------------------------------------------------------
# 文字排版（后置合成逃生舱 `--typeset-title`：零成本、字绝对精确）
# ---------------------------------------------------------------------------
def _units(text: str) -> float:
    """文本宽度估算单位：CJK≈1em、ASCII/半角≈0.55em（与水印同口径）。"""
    return sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in text)


def build_text_filter(*, title: str, subtitle: str | None = None,
                      width: int, height: int, font: str | None = None,
                      accent: str = "#ffd45e") -> str:
    """封面排版 filtergraph（接在缩放裁切之后，逗号相连可直接拼 -vf）。

    · 底部亮度渐变压暗（geq 逐像素，单帧渲染零性能顾虑）——任何背景上文字都可读；
    · 主标题：大号粗描边白字 + 柔和投影，字号按标题长度自适应、宽度不超画面 86%；
    · 副标题「— 第 N 集 —」：主题色小字（accent 取画风字幕样式，与系列видео同色系）。
    同参数 = 同版式：系列与全部章节共用此函数，系列感由此锁死。"""
    title = (title or "").strip()
    if not title:
        raise ProjectError("封面标题为空")
    size = min(round(width * 0.14), max(44, round(width * 0.86 / max(_units(title), 1))))
    y0 = round(height * 0.60)
    span = max(1, height - y0)
    shade = "(1-0.62*clip((Y-{y0})/{sp}\\,0\\,1))".format(y0=y0, sp=span)
    parts = ["format=rgb24",
             f"geq=r='r(X,Y)*{shade}':g='g(X,Y)*{shade}':b='b(X,Y)*{shade}'"]
    fontopt = f":fontfile={filter_literal(font)}" if font else ""
    ty = round(height * 0.775) - size
    parts.append(
        f"drawtext=text={drawtext_text(title)}:fontsize={size}:fontcolor=white{fontopt}"
        f":borderw={max(2, size // 24)}:bordercolor=black@0.55"
        f":shadowcolor=black@0.45:shadowx=2:shadowy=4"
        f":x=(w-text_w)/2:y={ty}")
    if subtitle and subtitle.strip():
        s2 = max(30, round(size * 0.40))
        deco = f"— {subtitle.strip()} —"
        parts.append(
            f"drawtext=text={drawtext_text(deco)}:fontsize={s2}:fontcolor={accent}{fontopt}"
            f":borderw=2:bordercolor=black@0.5:shadowcolor=black@0.4:shadowx=1:shadowy=2"
            f":x=(w-text_w)/2:y={ty + size + round(s2 * 0.9)}")
    return ",".join(parts)


def compose_cover(bg: str | Path, out: str | Path, *, title: str,
                  subtitle: str | None = None, width: int = 1080, height: int = 1440,
                  font: str | None = None, accent: str = "#ffd45e",
                  profile: str | None = None) -> str:
    """背景图 → 封面成品：cover-fit 缩放裁切到目标比例 + 排版层。纯本地 ffmpeg 单帧。

    font 收风格名（song/kai/hei/yuan）或字体文件路径；缺省按 profile 推
    ——古风衬线/现代粗黑/治愈圆体按题材归位，不落单一默认黑体。"""
    font = resolve_font(font, profile=profile)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},"
          + build_text_filter(title=title, subtitle=subtitle,
                              width=width, height=height, font=font, accent=accent))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run(["-i", str(bg), "-vf", vf, "-frames:v", "1", "-update", "1", str(out)],
        desc="cover")
    return str(out)
