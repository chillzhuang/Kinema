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

"""白标品牌配置。

系统按卖断交付时，买家在 config/branding.yaml 里换名字/口号/主题色，
Studio 大屏、提案页、审阅包页脚即挂买家自己的牌子——代码零改动。
accent 只接受 #RRGGBB，非法值忽略（防止把界面主题色改成不可读的东西）。
"""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT_BRANDING = {
    "name": "Kinema",
    "tagline": "PRODUCTION STUDIO",
    "accent": None,          # 缺省用界面自带琥珀色
    "watermark": {},         # 漂移水印默认（text/opacity/size/speed/fade/color），见 branding.yaml
    "watermark_fixed": {},   # 固定角标默认（text/position/size/opacity/color/font）
    "watermark_bottom": {},  # 底部水印默认（text/size/opacity/color/margin/font）
}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _find_file() -> Path | None:
    for start in [Path.cwd(), Path(__file__).resolve().parent]:
        for d in [start, *start.parents]:
            cand = d / "config" / "branding.yaml"
            if cand.is_file():
                return cand
    return None


def load_branding() -> dict:
    """品牌配置（缺文件/缺 PyYAML 用默认）。"""
    brand = dict(DEFAULT_BRANDING)
    path = _find_file()
    if path is None:
        return brand
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001  品牌配置坏了不该拖垮 Studio，回退默认
        return brand
    for k in ("name", "tagline", "accent"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            brand[k] = v.strip()
    if brand.get("accent") and not _HEX.match(brand["accent"]):
        brand["accent"] = None
    # 三段水印配置整段透传：CLI 与 Studio 的「project > branding」文案回落链都吃
    # 这里，哪段漏了透传，哪段的全局默认就静默失效（配置写了却永远不生效）
    for k in ("watermark", "watermark_fixed", "watermark_bottom"):
        if isinstance(data.get(k), dict):
            brand[k] = data[k]
    return brand
