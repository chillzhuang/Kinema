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

"""排版字体风格库（封面/转场字卡共用）。

单一机械黑体与多数内容气质不匹配——标题排版按**风格**选字体而非按"有没有"：
  song  宋体衬线 · 庄重优雅（默认，杂志封面感）
  kai   楷体 · 古风手写（仙侠/水墨/国风）
  hei   现代粗黑 · 力量感（游戏/赛博/机甲/爱死机）
  yuan  圆体 · 治愈可爱（童话/绘本/黏土）

每种风格一条候选链（macOS 系统字体 + Linux Noto/文泉驿），首个存在者胜出；
整链落空按 _FALLBACK_ORDER 借用相邻风格，最后兜底 ffmpeg.find_font_cjk。
`default_style(profile)` 按画风名给缺省风格——封面/字卡与画面气质一致。"""
from __future__ import annotations

from pathlib import Path

# ---- 工程内置字体（免费商用·随仓库分发·跨系统一致，不依赖各机器系统字体）----
# 阿里巴巴普惠体 3.0：全球永久免费商用、无需署名；许可明确允许「嵌入产品打包分发」
# （红线仅「单独售卖字库文件」）——适合本白标可交付系统内置。字库落 assets/fonts/，
# 水印/角标用 fontfile 直接指路径；字幕用「族名 + libass fontsdir」加载（见 compose）。
FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
# 无衬线主字（黑体系）：现代干净，小字号比宋体清晰专业
PUHUITI_REGULAR = "AlibabaPuHuiTi-3-55-Regular.otf"   # 常规：水印/角标
PUHUITI_MEDIUM = "AlibabaPuHuiTi-3-65-Medium.otf"     # 中黑：字幕主字
PUHUITI_MEDIUM_FAMILY = "Alibaba PuHuiTi 3.0 65 Medium"   # 字幕 ASS Fontname（libass 按族名匹配）
PUHUITI_REGULAR_FAMILY = "Alibaba PuHuiTi 3.0 55 Regular"
# 衬线（宋体系·国风/水墨字幕）：思源宋体 SC（SIL OFL·免费商用·无署名义务）
NOTOSERIF_SC = "NotoSerifSC-SemiBold.otf"
# 用**含字重的族名**做 libass Fontname（同普惠体思路）：SemiBold 字重(fc 180)不等于 ASS
# bold=0 的 weight 400，用通用族名"Noto Serif SC"会因字重不符被 libass 判失配回退 Helvetica；
# 该字体另暴露了含字重的族名"Noto Serif SC SemiBold"，按它精确命中内置文件。
NOTOSERIF_SC_FAMILY = "Noto Serif SC SemiBold"
# 楷体（古风手写·封面/字卡）：霞鹜文楷 Lite（SIL OFL·免费商用）
WENKAI = "LXGWWenKaiLite-Regular.ttf"
WENKAI_FAMILY = "LXGW WenKai Lite"
# 展示型美术黑（logo 式标题·可选）：得意黑 Smiley Sans（SIL OFL·免费商用）
SMILEY = "SmileySans-Oblique.otf"
SMILEY_FAMILY = "Smiley Sans"


def bundled_path(filename: str) -> str | None:
    """工程内置字体绝对路径（存在才返回）——供 ffmpeg drawtext `fontfile=` 直接引用。"""
    p = FONTS_DIR / filename
    return str(p) if p.is_file() else None


FONT_STYLES: dict[str, dict] = {
    "song": {
        "label": "宋体衬线·庄重优雅",
        "candidates": [
            str(FONTS_DIR / NOTOSERIF_SC),    # 工程内置思源宋体 SC(免费商用·跨系统一致) 优先
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/System/Library/Fonts/Supplemental/STSong.ttf",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/opentype/source-han-serif/SourceHanSerifSC-Bold.otf",
        ],
    },
    "kai": {
        "label": "楷体·古风手写",
        "candidates": [
            str(FONTS_DIR / WENKAI),          # 工程内置霞鹜文楷 Lite(免费商用·跨系统一致) 优先
            "/System/Library/Fonts/Supplemental/Kaiti.ttc",
            "/System/Library/Fonts/Supplemental/STKaiti.ttf",
            "/usr/share/fonts/truetype/arphic/ukai.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        ],
    },
    "hei": {
        "label": "现代黑体·力量感",
        "candidates": [
            str(FONTS_DIR / PUHUITI_MEDIUM),    # 工程内置阿里普惠体(免费商用·跨系统一致) 优先
            str(FONTS_DIR / PUHUITI_REGULAR),
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ],
    },
    "yuan": {
        "label": "圆体·治愈可爱",
        "candidates": [
            "/System/Library/Fonts/Supplemental/Yuanti.ttc",
            "/System/Library/Fonts/Supplemental/YuGothic.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ],
    },
    "display": {
        "label": "美术黑·logo 式标题",      # 得意黑（工程内置·免费商用），展示型标题/角标可选
        "candidates": [str(FONTS_DIR / SMILEY)],
    },
}
# 风格链落空时的借用顺序（保证永远拿得到一个"最不违和"的字体）
_FALLBACK_ORDER = {"song": ("kai", "hei"), "kai": ("song", "hei"),
                   "hei": ("song",), "yuan": ("hei", "song"),
                   "display": ("hei",)}


def default_style(profile: str | None) -> str:
    """画风 → 缺省字体风格：古风衬线/现代粗黑/治愈圆体各归其位。"""
    p = (profile or "").lower()
    if any(k in p for k in ("xianxia", "ink", "anime3d", "quote")):
        return "kai"
    if any(k in p for k in ("game", "cyber", "mecha", "ldr", "pixel", "arcade",
                            "hd2d", "gba", "snes", "dark_fantasy", "ranking")):
        return "hei"
    if any(k in p for k in ("fairytale", "storybook", "clay", "doodle", "brick")):
        return "yuan"
    return "song"


def resolve_font(style: str | None = None, *, profile: str | None = None) -> str | None:
    """风格名/字体路径 → 实际字体文件。

    · style 是existing文件路径 → 直接用（用户自备字体）；
    · style 是风格名（song/kai/hei/yuan）→ 走候选链 + 借用链；
    · style 为空 → 按 profile 推缺省风格。全部落空回 find_font_cjk 兜底。"""
    if style and Path(style).is_file():
        return str(style)
    key = style if style in FONT_STYLES else default_style(profile)
    for k in (key, *_FALLBACK_ORDER.get(key, ())):
        for cand in FONT_STYLES[k]["candidates"]:
            if Path(cand).is_file():
                return cand
    from .ffmpeg import find_font_cjk
    return find_font_cjk()
