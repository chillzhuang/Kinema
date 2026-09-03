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

"""静态审阅页导出（本地化交付形态）。

把一个章节渲染成**免登录、可离线打开**的审阅包发给客户：
    <out>/index.html   单页审阅书（成片 + 分镜脚本 + 审阅状态 + 评论，只读）
    <out>/media/       引用到的成片/分镜图/配音（拷贝，自包含）

设计：不依赖 Studio 服务，样式内联（沿用"剪辑室控制台"设计语言的精简子集）；
客户反馈走外部渠道回流，由制作方在 Studio 中转译为 retake 意见（内外隔离）。
"""
from __future__ import annotations

import html
import shutil
from datetime import datetime
from pathlib import Path

from .project import DEFAULT_ASPECT, effective_motion
from .review import STAGES, get_note, get_state, is_omitted, label

_CSS = """
:root{--bg:#0b0c0f;--card:#14171c;--line:rgba(255,255,255,.08);--txt:#e9ebf1;
--txt2:#a6adbd;--txt3:#626b7d;--amber:#f0a63c;--green:#46d08b;--red:#ef6a5a;
--mono:"SF Mono",ui-monospace,Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:15px/1.7 "Avenir Next","Helvetica Neue",
"PingFang SC","Microsoft YaHei",sans-serif;padding:48px 24px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:26px;margin-bottom:6px}
.sub{color:var(--txt2);margin-bottom:14px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:28px}
.chip{border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:12px;
color:var(--txt2)}
.chip.amber{color:var(--amber);border-color:rgba(240,166,60,.4)}
.chip.green{color:var(--green);border-color:rgba(70,208,139,.4)}
.chip.red{color:var(--red);border-color:rgba(239,106,90,.4)}
h2{font-size:16px;margin:34px 0 14px;display:flex;align-items:center;gap:10px}
h2 em{font:500 9px/1 var(--mono);letter-spacing:.25em;color:var(--txt3);font-style:normal}
h2::after{content:"";flex:1;height:1px;background:var(--line)}
video,audio{width:100%;border-radius:10px;background:#000}
.final{max-width:420px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin-bottom:14px;display:grid;grid-template-columns:200px 1fr;gap:16px}
.card.omt{opacity:.45}
.card img{width:100%;border-radius:8px;cursor:zoom-in}
.slate{font:700 11px/1 var(--mono);color:var(--amber);margin-bottom:8px}
.narr{font-size:14.5px;margin:6px 0}
.meta{color:var(--txt3);font-size:12px}
.note{color:var(--red);font-size:12.5px;margin-top:6px}
.cmt{border-left:2px solid var(--line);padding-left:10px;margin-top:8px;font-size:12.5px;
color:var(--txt2)}
.cmt b{color:var(--amber);font:600 10px/1 var(--mono)}
.script p{color:var(--txt2);font-size:13.5px;margin:4px 0 12px}
.script label{font:600 10px/1 var(--mono);letter-spacing:.15em;color:var(--txt3)}
.foot{margin-top:44px;color:var(--txt3);font-size:11px;text-align:center}
@media(max-width:720px){.card{grid-template-columns:1fr}}
"""


def _copy(src, media_dir: Path, seen: dict) -> str | None:
    """把媒体文件拷入 media/ 并返回相对路径（同一文件只拷一次；已上云自动拉回）。"""
    if not src:
        return None
    from .storage.media import ensure_local
    p = Path(ensure_local(src))
    if not p.is_file():
        return None
    key = str(p.resolve())
    if key not in seen:
        dst = media_dir / f"{len(seen):03d}_{p.name}"
        shutil.copy2(p, dst)
        seen[key] = f"media/{dst.name}"
    return seen[key]


def _e(s) -> str:
    return html.escape(str(s or ""))


def _ws_abs(ws, rel):
    """工作区相对路径 → 绝对路径（封面注册表的存储口径）；绝对路径原样放行。"""
    if not rel:
        return None
    q = Path(rel)
    return str(q if q.is_absolute() else ws.root / q)


def build_review_page(project, store, out_dir: str | Path) -> Path:
    """章节 → 静态审阅包。project 为引擎 Project 实例（章节文档）。"""
    out = Path(out_dir)
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)
    seen: dict = {}
    data = project.data
    title = (data.get("chapter") or {}).get("title") or data.get("theme") or project.id

    # 成片（多比例）
    finals = []
    for asp, path in (data.get("output") or {}).items():
        rel = _copy(path, media, seen)
        if rel:
            finals.append((asp, rel))

    parts = [f"<h1>{_e(title)}</h1>",
             f'<div class="sub">{_e(data.get("theme") or "")}</div>',
             '<div class="chips">',
             f'<span class="chip amber">{_e(data.get("profile") or "")}</span>',
             f'<span class="chip">{_e(" / ".join(project.aspects))}</span>',
             f'<span class="chip">{_e(project.motion)}</span>',
             f'<span class="chip">{len(project.shots)} 镜 · {project.total_duration():.1f}s</span>',
             "</div>"]

    if finals:
        parts.append("<h2>成片 <em>FINAL CUT</em></h2>")
        for asp, rel in finals:
            parts.append(f'<div class="meta">{_e(asp)}</div>'
                         f'<video class="final" controls preload="metadata" src="{rel}"></video>')

    sc = data.get("script") or {}
    if any(sc.get(k) for k in ("hook", "body", "cta")):
        parts.append('<h2>文案 <em>SCRIPT</em></h2><div class="script">')
        for k, zh in (("hook", "HOOK 钩子"), ("body", "BODY 正文"), ("cta", "CTA 行动")):
            if sc.get(k):
                parts.append(f"<label>{zh}</label><p>{_e(sc[k])}</p>")
        parts.append("</div>")

    parts.append(f"<h2>分镜脚本 <em>STORYBOARD · {len(project.shots)}</em></h2>")
    adir = project.workdir / "audio"
    for s in project.shots:
        omt = is_omitted(s)
        img = _copy(s.get("image"), media, seen)
        wav = _copy(adir / f"shot_{s.get('id')}.wav", media, seen)
        clip = _copy(s.get("clip"), media, seen)
        badges = []
        for st in STAGES:
            state = get_state(s, st)
            if state == "todo":
                continue
            cls = {"done": "green", "retake": "red"}.get(state, "amber")
            badges.append(f'<span class="chip {cls}">{ {"image":"图","audio":"音","clip":"片"}[st] }·{label(state)}</span>')
        notes = "；".join(f"{st}: {get_note(s, st)}" for st in STAGES if get_note(s, st))
        cmts = "".join(
            f'<div class="cmt"><b>{_e(c.get("stage",""))}'
            + (f' @{c.get("t"):.1f}s' if c.get("t") is not None else "")
            + f"</b> {_e(c.get('text'))}</div>"
            for c in (s.get("comments") or []))
        parts.append(f"""
<div class="card{' omt' if omt else ''}">
  <div>
    <div class="slate">SHOT {int(s.get('id', 0)):02d}{' · 已弃用' if omt else ''}</div>
    {f'<img src="{img}" alt="" />' if img else ''}
    {f'<video controls preload="none" src="{clip}"></video>' if clip else ''}
  </div>
  <div>
    <div class="chips">{''.join(badges)}</div>
    <div class="narr">{_e(s.get('narration'))}</div>
    <div class="meta">{_e(s.get('speaker') or '')} · {_e(s.get('framing') or '')} ·
      {_e(s.get('camera') or '')} · {float(s.get('dur') or 0):.1f}s</div>
    {f'<audio controls preload="none" src="{wav}"></audio>' if wav else ''}
    {f'<div class="note">审阅意见：{_e(notes)}</div>' if notes else ''}
    {cmts}
  </div>
</div>""")

    parts.append(f'<div class="foot">kinema · 静态审阅包 · 生成于 '
                 f'{datetime.now().strftime("%Y-%m-%d %H:%M")} · 本页可离线打开</div>')

    page = (f'<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8"/>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"/>'
            f'<title>{_e(title)} · 审阅</title><style>{_CSS}</style></head>'
            f'<body><div class="wrap">{"".join(parts)}</div></body></html>')
    index = out / "index.html"
    index.write_text(page, encoding="utf-8")
    return index


# ============================================================================
# 项目提案书（Pitch Deck）
# ============================================================================
# 屏幕态是暗色影视级排版（衬线大标题 + 电影感封面），@media print 切换成
# 白底印刷排版（浏览器「打印→存为 PDF」即得提案 PDF，零额外依赖）。
_PITCH_CSS = """
:root{--bg:#0b0c0f;--card:#14171c;--line:rgba(255,255,255,.08);--txt:#e9ebf1;
--txt2:#a6adbd;--txt3:#626b7d;--amber:#f0a63c;--green:#46d08b;--red:#ef6a5a;
--mono:"SF Mono",ui-monospace,Menlo,monospace;
--serif:"Songti SC","STSong","Noto Serif SC","SimSun",serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:15px/1.78 "Avenir Next","Helvetica Neue",
"PingFang SC","Microsoft YaHei",sans-serif}
.page{max-width:920px;margin:0 auto;padding:40px 32px 64px}
.eyebrow{font:600 10px/1 var(--mono);letter-spacing:.38em;color:var(--amber);
margin-bottom:20px}
.cover{position:relative;min-height:82vh;display:flex;flex-direction:column;
justify-content:flex-end;padding:56px 48px;border-radius:20px;overflow:hidden;
border:1px solid var(--line);margin-bottom:72px}
.cover-bg{position:absolute;inset:0;background-size:cover;background-position:center;
filter:saturate(.9)}
.cover-shade{position:absolute;inset:0;background:linear-gradient(180deg,
rgba(11,12,15,.18) 0%,rgba(11,12,15,.55) 52%,rgba(11,12,15,.95) 100%)}
.cover>*:not(.cover-bg):not(.cover-shade){position:relative}
h1{font:700 56px/1.12 var(--serif);letter-spacing:.02em;margin-bottom:16px;
text-shadow:0 2px 24px rgba(0,0,0,.5)}
.logline{font:17px/1.9 var(--serif);color:var(--txt2);max-width:620px;margin-bottom:24px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1px solid rgba(255,255,255,.16);border-radius:999px;padding:4px 14px;
font-size:12px;color:var(--txt2);background:rgba(0,0,0,.25)}
.chip.amber{color:var(--amber);border-color:rgba(240,166,60,.45)}
.chip.warn{color:var(--red);border-color:rgba(239,106,90,.45)}
.cover-meta{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:26px;padding-top:16px;
border-top:1px solid rgba(255,255,255,.14);color:var(--txt3);
font:500 10px/1.8 var(--mono);letter-spacing:.2em}
h2{font:600 24px/1.3 var(--serif);margin:64px 0 20px;display:flex;align-items:baseline;gap:12px}
h2 em{font:500 9px/1 var(--mono);letter-spacing:.28em;color:var(--txt3);font-style:normal}
h2::after{content:"";flex:1;height:1px;background:var(--line);align-self:center}
.bible{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.bible .cell{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:14px 16px}
.bible label{font:600 9px/1 var(--mono);letter-spacing:.2em;color:var(--txt3)}
.bible p{color:var(--txt2);font-size:13.5px;margin-top:6px}
.scene{width:100%;aspect-ratio:21/9;object-fit:cover;border-radius:12px;
margin-top:12px;border:1px solid var(--line)}
.cast{display:flex;flex-direction:column;gap:14px}
.actor{display:flex;align-items:stretch;background:var(--card);
border:1px solid var(--line);border-radius:14px;overflow:hidden;break-inside:avoid}
/* 设定图智能框：定高 270px，宽度随图片原生比例自适应（横版铺宽、竖版收窄），
   object-fit:contain 整图完整呈现零裁切；max-width 兜底防超宽挤没文案列 */
.actor img{flex:none;height:270px;width:auto;max-width:58%;object-fit:contain;
display:block;background:#181b21}
.actor .ph{flex:none;width:210px;min-height:270px;display:grid;place-items:center;
color:var(--txt3);font:600 26px/1 var(--mono);background:#181b21}
.actor .info{flex:1;min-width:0;padding:18px 24px;display:flex;flex-direction:column;
justify-content:center;border-left:1px solid var(--line)}
.actor b{font:600 17px/1.4 var(--serif)}
.actor i{font-style:normal;color:var(--amber);font:600 10px/1.9 var(--mono);
display:block;letter-spacing:.12em;margin-top:2px}
.actor p{color:var(--txt2);font-size:12.5px;line-height:1.75;margin-top:7px}
.ep{display:grid;grid-template-columns:225px 1fr;gap:18px;background:var(--card);
border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px;
break-inside:avoid}
.ep.no-cover{grid-template-columns:1fr}
.ep>img{width:100%;aspect-ratio:3/4;border-radius:9px;object-fit:cover;
border:1px solid var(--line)}
.ep .no{font:700 10px/1 var(--mono);color:var(--amber);letter-spacing:.18em}
.ep b{font:600 19px/1.4 var(--serif);display:block;margin:7px 0 3px}
.ep .meta{color:var(--txt3);font-size:12px}
.ep-hook{font:14.5px/1.8 var(--serif);color:var(--txt2);border-left:2px solid var(--amber);
padding-left:12px;margin:10px 0 2px}
.ep-strip{display:flex;gap:6px;margin-top:12px}
.ep-strip img{width:56px;height:74px;object-fit:cover;border-radius:6px;
border:1px solid var(--line)}
.spec{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px;color:var(--txt2);font-size:13.5px;line-height:2}
.spec b{color:var(--txt);font-family:var(--mono)}
.backcover{margin-top:88px;padding:64px 32px 56px;text-align:center;
border:1px solid var(--line);border-radius:20px;
background:radial-gradient(60% 80% at 50% 0%,rgba(240,166,60,.07),transparent 70%)}
.bc-mark{width:54px;height:54px;border-radius:15px;background:var(--amber);
display:inline-grid;place-items:center;margin-bottom:18px}
.bc-mark span{display:block;width:0;height:0;border-left:16px solid #0b0c0f;
border-top:10px solid transparent;border-bottom:10px solid transparent;margin-left:4px}
.bc-name{font:700 24px/1.3 var(--serif);letter-spacing:.06em}
.bc-tag{font:500 10px/1 var(--mono);letter-spacing:.34em;color:var(--txt3);margin-top:8px}
.bc-meta{margin-top:26px;padding-top:18px;border-top:1px dashed var(--line);
display:inline-block;color:var(--txt3);font-size:11px;letter-spacing:.06em}
@media(max-width:680px){.bible{grid-template-columns:1fr}.ep{grid-template-columns:1fr}
h1{font-size:38px}.cover{min-height:64vh;padding:36px 24px}}
@media print{
 body{background:#fff;color:#1a1d23}
 :root{--card:#fff;--line:#d8dbe2;--txt:#1a1d23;--txt2:#3d4350;--txt3:#8a90a0;
 --amber:#b06a10}
 .page{padding:0;max-width:none}
 h1{font-size:40px;text-shadow:none}
 .cover{min-height:auto;padding:120px 56px;page-break-after:always}
 .cover-shade{background:linear-gradient(180deg,rgba(255,255,255,.28) 0%,
 rgba(255,255,255,.82) 60%,rgba(255,255,255,.97) 100%)}
 h2{page-break-after:avoid;margin-top:40px}
 .actor,.ep,.spec{box-shadow:none;break-inside:avoid}
 .backcover{page-break-before:always;border:none;background:none;margin-top:0;
 padding-top:38vh}
 a{color:inherit;text-decoration:none}
}
"""


def build_pitch_page(ws, pid: str, out_dir: str | Path | None = None) -> Path:
    """项目 → 提案书（单页 HTML + 自包含媒体；浏览器打印即 PDF）。

    提案面向外部（平台/客户/买家），只放卖相内容：梗概/世界观/角色/样张/规格；
    成本、估值、审阅意见等内部数据一律不出现。"""
    from .branding import load_branding
    from . import templates as tpl_mod

    s = ws.get_project(pid)
    data = s.data
    out = Path(out_dir) if out_dir else (s.dir / "exports" / "pitch")
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)
    seen: dict = {}
    brand = load_branding()
    design = data.get("design") or {}
    tpl = data.get("template") or {}

    # 逐章样张（首镜大图 + 前三镜样张条 + 钩子金句）+ 规格核对
    episodes, total_min = [], 0.0
    for ch in s.list_chapters():
        cdata = ws.store.load_chapter(s.pid, ch["id"]) or {}
        shots = [x for x in cdata.get("shots") or []
                 if ((x.get("review") or {}).get("shot") or {}).get("state") != "omt"]
        imgs = [x.get("image") for x in shots if x.get("image")]
        dur = sum(float(x.get("dur") or 0) for x in shots)
        total_min += dur / 60
        ok = None
        if tpl:
            rows = tpl_mod.check_chapter(tpl, duration_s=dur, shots=len(shots),
                                         aspect=cdata.get("aspect") or "")
            ok = not any(r["ok"] is False for r in rows)
        # 分集海报链：章节封面成品(3:4 竖版) → 多比例表兜底 → 首镜样张
        # 封面注册的是工作区相对路径（cover 命令口径），须挂 ws.root 解析
        ccov = cdata.get("cover") or {}
        episodes.append({"id": ch["id"], "title": ch.get("title") or ch["id"],
                         "img": _copy(_ws_abs(ws, ccov.get("primary")
                                              or (ccov.get("images") or {}).get("3:4"))
                                      or (imgs[0] if imgs else None), media, seen),
                         "strip": [u for u in (_copy(p, media, seen) for p in imgs[1:4]) if u],
                         "hook": ((cdata.get("script") or {}).get("hook") or "").strip(),
                         "dur": dur, "shots": len(shots), "ok": ok})

    chips = [f'<span class="chip amber">{_e(data.get("profile") or "")}</span>',
             f'<span class="chip">{_e(data.get("aspect") or DEFAULT_ASPECT)}</span>',
             f'<span class="chip">{len(episodes)} 集 · {total_min:.1f} 分钟</span>']
    if tpl.get("label"):
        chips.append(f'<span class="chip">{_e(tpl["label"])}</span>')
    for p in data.get("platform") or []:
        chips.append(f'<span class="chip">{_e(p)}</span>')

    # 电影感封面：系列主视觉打底（工作区相对路径挂 ws.root）→ 场景设定图 →
    # 首集样张 → 纯色渐变
    cover_bg = _copy(_ws_abs(ws, (data.get("cover") or {}).get("primary")),
                     media, seen) \
        or _copy(data.get("scene_ref"), media, seen) \
        or next((ep["img"] for ep in episodes if ep["img"]), None)
    today = datetime.now().strftime("%Y.%m")
    parts = ['<div class="cover">',
             (f'<div class="cover-bg" style="background-image:url({cover_bg})"></div>'
              if cover_bg else ""),
             '<div class="cover-shade"></div>',
             f'<div class="eyebrow">{_e(brand["name"])} · SERIES PITCH</div>',
             f'<h1>{_e(data.get("title") or pid)}</h1>',
             f'<div class="logline">{_e(design.get("logline") or data.get("theme") or "")}</div>',
             f'<div class="chips">{"".join(chips)}</div>',
             f'<div class="cover-meta"><span>{_e(brand["tagline"])}</span>'
             f'<span>EPISODES {len(episodes):02d}</span>'
             f'<span>RUNTIME {total_min:.1f} MIN</span><span>{today}</span></div>',
             "</div>"]

    cells = [(k, zh) for k, zh in (("world", "世界观 WORLD"), ("tone", "基调 TONE"),
                                   ("palette", "色板 PALETTE"), ("style_notes", "风格注记 STYLE"))
             if design.get(k)]
    if cells or data.get("scene"):
        parts.append("<h2>故事与世界 <em>SERIES BIBLE</em></h2>")
        if cells:
            parts.append('<div class="bible">' + "".join(
                f'<div class="cell"><label>{zh}</label><p>{_e(design[k])}</p></div>'
                for k, zh in cells) + "</div>")
        scene = _copy(data.get("scene_ref"), media, seen)
        if scene:
            parts.append(f'<img class="scene" src="{scene}" alt="场景设定图"/>')

    chars = data.get("characters") or []
    if chars:
        parts.append(f"<h2>角色 <em>CAST · {len(chars)}</em></h2><div class=\"cast\">")
        for c in chars:
            img = _copy(c.get("sheet") or c.get("ref_image"), media, seen)
            visual = (f'<img src="{img}" alt=""/>' if img
                      else f'<div class="ph">{_e((c.get("name") or "?")[:1])}</div>')
            parts.append(
                f'<div class="actor">{visual}<div class="info">'
                f'<b>{_e(c.get("name"))}</b><i>{_e(c.get("role") or "")}'
                + (f' · 声 {_e(c.get("voice"))}' if c.get("voice") else "")
                + f"</i><p>{_e(c.get('appearance') or '')}</p></div></div>")
        parts.append("</div>")

    if episodes:
        parts.append(f"<h2>分集样张 <em>EPISODES · {len(episodes)}</em></h2>")
        for i, ep in enumerate(episodes, 1):
            spec_chip = ("" if ep["ok"] is None else
                         ('<span class="chip">规格达标</span>' if ep["ok"]
                          else '<span class="chip warn">规格未达标</span>'))
            strip = ("".join(f'<img src="{u}" alt=""/>' for u in ep["strip"])
                     if ep["strip"] else "")
            parts.append(
                # 无海报的集不留空白左栏：卡片降级为单列满宽（no-cover）
                f'<div class="ep{"" if ep["img"] else " no-cover"}">'
                + (f'<img src="{ep["img"]}" alt=""/>' if ep["img"] else "")
                + f'<div><span class="no">EP {i:02d} · {_e(ep["id"])}</span>'
                  f'<b>{_e(ep["title"])}</b>'
                  f'<div class="meta">{ep["dur"] / 60:.1f} 分钟 · {ep["shots"]} 镜'
                + (f'　{spec_chip}' if spec_chip else "") + "</div>"
                + (f'<div class="ep-hook">「{_e(ep["hook"])}」</div>' if ep["hook"] else "")
                + (f'<div class="ep-strip">{strip}</div>' if strip else "")
                + "</div></div>")

    craft = []
    if (data.get("style_prompt") or "").strip():
        craft.append(("画风基因 STYLE DNA", data["style_prompt"].rstrip("，, ")))
    motion_zh = {"kenburns": "静帧电影运镜（Ken Burns 推拉摇移，节奏由旁白驱动）",
                 "dubbed": "AI 图生视频 + 固定音色旁白烧录（全旁白解说形态，人物闭唇出片）",
                 "native": "AI 原生音画（角色按音色锚定开口，台词与环境音效一体生成）"}
    craft.append(("动态工艺 MOTION",
                  motion_zh[effective_motion(data)]))
    if chars:
        voiced = sum(1 for c in chars if c.get("voice"))
        craft.append(("声演阵容 VOICE CAST",
                      f"{voiced} 位角色绑定专属固定音色，跨集零漂移"
                      + ("；旁白独立选角锁定" if (data.get("narrator") or {}).get("locked") else "")))
    if chars or (data.get("scene") or "").strip():
        craft.append(("一致性工程 CONSISTENCY",
                      f"{len(chars)} 份角色设定图（正脸肖像＋全身两视）与场景设定图"
                      "全镜强制参考，角色/场景跨集统一"))
    sub_zh = {"zh": "中文", "en": "英文", "both": "中英双语"}
    craft.append(("成片交付 DELIVERY",
                  f"{data.get('aspect') or '16:9'} 主比例（支持多比例出片）· "
                  f"{sub_zh.get(data.get('subtitle_lang') or 'zh', '中文')}字幕后置烧录 · "
                  "视频本体无字可二次剪辑"))
    parts.append("<h2>制作工艺 <em>CRAFT</em></h2>")
    parts.append('<div class="bible">' + "".join(
        f'<div class="cell"><label>{_e(zh)}</label><p>{_e(v)}</p></div>'
        for zh, v in craft) + "</div>")

    spec_lines = []
    ep_spec = tpl.get("episode") or {}
    if ep_spec.get("minutes"):
        spec_lines.append(f"单集 <b>{ep_spec['minutes'][0]}–{ep_spec['minutes'][1]} 分钟</b>")
    se = tpl.get("series") or {}
    if se.get("episodes"):
        spec_lines.append(f"全季 <b>{se['episodes'][0]}–{se['episodes'][1]} 集</b>")
    if se.get("total_minutes"):
        spec_lines.append(f"总量 <b>{se['total_minutes'][0]}–{se['total_minutes'][1]} 分钟</b>"
                          f"（当前 {total_min:.1f}）")
    parts.append("<h2>制作规格与合规 <em>SPEC & COMPLIANCE</em></h2>")
    parts.append('<div class="spec">'
                 + ("交付规格：" + " · ".join(spec_lines) + "<br/>" if spec_lines else "")
                 + "交付物：多比例成片 MP4 · 系列/分集封面 · ASS+SRT 双字幕 · "
                   "平台发布文案 · manifest 元数据清单<br/>"
                 + "内容标注：本片为 AI 辅助生成内容，发布时按平台要求进行 AI 生成披露。"
                 + "</div>")

    parts.append(
        '<div class="backcover"><div class="bc-mark"><span></span></div>'
        f'<div class="bc-name">{_e(brand["name"])}</div>'
        f'<div class="bc-tag">{_e(brand["tagline"])}</div>'
        f'<div class="bc-meta">《{_e(data.get("title") or pid)}》系列提案 · '
        f'{datetime.now().strftime("%Y-%m-%d")} · 浏览器打印即 PDF</div></div>')

    page = (f'<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="UTF-8"/>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"/>'
            f'<title>{_e(data.get("title") or pid)} · 提案</title>'
            f'<style>{_PITCH_CSS}</style></head>'
            f'<body><div class="page">{"".join(parts)}</div></body></html>')
    index = out / "index.html"
    index.write_text(page, encoding="utf-8")
    return index
