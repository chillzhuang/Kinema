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

"""项目模板 / 平台规格预设。

一个模板 = 「风格档 + 渲染模式 + 比例 + 集时长/分镜数区间 + 系列体量」打包，
`project new --template <名>` 一键实例化——平台规格不再靠人脑记，
`spec check <项目>` 随时核对每集是否达标（分账/接单交付的验收线）。

单一真源：config/templates.yaml（可增删改）；缺文件/缺 PyYAML 时回退内置默认。
数字区间一律 [下限, 上限]；episode.minutes 支持小数（0.5 = 30 秒）。
平台分账口径等运营参考写在 notes，供 Skill 层向用户解释定级逻辑。
"""
from __future__ import annotations

from pathlib import Path

from .errors import ConfigError

# 内置默认（templates.yaml 的最小可用子集）——保证零配置可用。
EMBEDDED_TEMPLATES = {
    "douyin_manju": {
        "label": "抖音漫剧",
        "platform": ["douyin"], "aspect": "9:16", "motion": "c", "profile": "anime",
        "episode": {"minutes": [1, 2], "shots": [8, 14]},
        "series": {"total_minutes": [100, 150]},
        "notes": "按分钟保底定级（A 2000 / S+ 5000 / 超头部 1–3 万元·分钟；2D 漫剧类型系数 40）",
    },
    "kuaishou_xingmang": {
        "label": "快手星芒短剧",
        "platform": ["kuaishou"], "aspect": "9:16", "motion": "c", "profile": "anime",
        "episode": {"minutes": [2, 5], "shots": [12, 30]},
        "series": {"episodes": [20, 30]},
        "notes": "星芒计划 CPM 阶梯 15/20/25；集数 20–30 为主流排播",
    },
    "bilibili_zhongshipin": {
        "label": "B站中视频",
        "platform": ["bilibili"], "aspect": "16:9", "motion": "a", "profile": "explainer",
        "episode": {"minutes": [3, 10], "shots": [12, 40]},
        "notes": "中视频计划横屏 ≥3 分钟；知识/解说类完播率优先",
    },
    "kepu_koubo": {
        "label": "科普口播",
        "platform": ["douyin", "xiaohongshu"], "aspect": "9:16", "motion": "a",
        "profile": "narration",
        "episode": {"minutes": [1, 3], "shots": [6, 12]},
        "notes": "钩子前 2 秒定生死；信息增量（具体数字/反常识）是完播核心",
    },
    "yulu_zhiyu": {
        "label": "语录治愈",
        "platform": ["douyin", "shipinhao"], "aspect": "9:16", "motion": "a",
        "profile": "quote",
        "episode": {"minutes": [0.5, 1], "shots": [4, 8]},
        "notes": "视频号中老年治愈盘大；BGM 情绪 calm，文字居中大字",
    },
    "ertong_huiben": {
        "label": "儿童绘本",
        "platform": ["youtube", "shipinhao"], "aspect": "16:9", "motion": "a",
        "profile": "storybook",
        "episode": {"minutes": [2, 4], "shots": [8, 14]},
        "notes": "横屏合家欢场景（电视/Pad）；语速放慢 speech_rate -20 左右",
    },
}


def _find_file() -> Path | None:
    for start in [Path.cwd(), Path(__file__).resolve().parent]:
        for d in [start, *start.parents]:
            cand = d / "config" / "templates.yaml"
            if cand.is_file():
                return cand
    return None


def load_templates() -> tuple[dict, str]:
    """全部模板与配置来源。文件与内置默认浅合并（同名以文件为准）。"""
    merged = dict(EMBEDDED_TEMPLATES)
    path = _find_file()
    if path is None:
        return merged, "<embedded>"
    try:
        import yaml
    except ImportError:
        return merged, "<embedded (PyYAML 缺失)>"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for name, t in (data.get("templates") or {}).items():
        if isinstance(t, dict):
            merged[name] = t
    return merged, str(path)


def get(name: str) -> dict:
    templates, _src = load_templates()
    if name not in templates:
        raise ConfigError(f"未知模板: {name}（可选: {', '.join(sorted(templates))}）。"
                          "见 config/templates.yaml")
    return {**templates[name], "name": name}


def apply_to_project(data: dict, tpl: dict) -> None:
    """把模板落进项目文档：风格/比例/平台/渲染模式 + 规格快照（spec check 的依据）。"""
    data["profile"] = tpl.get("profile") or data.get("profile")
    data["aspect"] = tpl.get("aspect") or data.get("aspect")
    if tpl.get("platform"):
        data["platform"] = list(tpl["platform"])
    if tpl.get("motion"):
        data["motion"] = tpl["motion"]        # 章节创建时继承（见 workspace.create_chapter）
    data["template"] = {k: v for k, v in tpl.items() if k != "name"}
    data["template"]["name"] = tpl.get("name")


# ---------------------------------------------------------------------------
# 规格核对（spec check）
# ---------------------------------------------------------------------------
def _in_range(v: float, rng: list | None) -> bool | None:
    """None=模板未约束该项。"""
    if not rng:
        return None
    lo, hi = float(rng[0]), float(rng[1])
    return lo <= v <= hi


def check_chapter(tpl: dict, *, duration_s: float, shots: int, aspect: str) -> list[dict]:
    """单章核对 → [{item, ok, actual, expect}]；ok=None 表示模板未约束。"""
    ep = tpl.get("episode") or {}
    out = []
    mins = ep.get("minutes")
    out.append({"item": "时长", "ok": _in_range(duration_s / 60, mins),
                "actual": f"{duration_s / 60:.1f} 分钟",
                "expect": f"{mins[0]}–{mins[1]} 分钟" if mins else "—"})
    sh = ep.get("shots")
    out.append({"item": "分镜数", "ok": _in_range(shots, sh),
                "actual": f"{shots} 镜",
                "expect": f"{sh[0]}–{sh[1]} 镜" if sh else "—"})
    want = tpl.get("aspect")
    out.append({"item": "比例", "ok": (aspect == want) if want else None,
                "actual": aspect or "—", "expect": want or "—"})
    return out


def check_series(tpl: dict, *, episodes: int, total_minutes: float) -> list[dict]:
    se = tpl.get("series") or {}
    out = []
    eps = se.get("episodes")
    out.append({"item": "集数", "ok": _in_range(episodes, eps),
                "actual": f"{episodes} 集",
                "expect": f"{eps[0]}–{eps[1]} 集" if eps else "—"})
    tm = se.get("total_minutes")
    ok = None
    if tm:   # 总量是「做满」目标：进行中未达下限给 None（进度），超上限才算 ⚠
        ok = True if _in_range(total_minutes, tm) else (None if total_minutes < tm[0] else False)
    out.append({"item": "总时长", "ok": ok,
                "actual": f"{total_minutes:.1f} 分钟",
                "expect": f"{tm[0]}–{tm[1]} 分钟" if tm else "—"})
    return out
