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

"""Studio 数据层：扫描工作区/项目/章节/成片，组装展示模型。

只读文件系统，不发 HTTP 请求（「实发提示词」预览经子进程调 CLI 编译，见 `video_preview`）。
所有磁盘路径在出口处统一转成 /media?path= 形式的 URL，由 server 层做目录白名单校验后流式返回。

数据模型（与 workspace.md 三层结构对应）：
  overview  —— 全局统计 + profile 清单 + 最近成片
  projects  —— 项目卡片摘要（角色/道具/章节进度/封面）
  project   —— 单项目全量：总体设计 + 角色设定(含设定图/音色/试听) + 道具 + 场景 + 章节表
  chapter   —— 单章节制作快照：脚本 + 逐镜(图/配音/生成片段/提示词) + 阶段进度 +
               多比例成片 + BGM/旁白/字幕 + 成本（供前端轮询，边生成边看）
  library   —— 片库：全部成片（跨项目 + 散落 project.json 的产物）
"""
from __future__ import annotations

import json
import math
import re
import urllib.parse
from pathlib import Path

from .. import review
from .. import voicebank
from .. import voicecast
from .. import effects as fx
from .. import previz as _pz
from ..errors import ConfigError
from .. import sketchboard as _sk
from ..pipeline import anchorframe
from ..pipeline import camera as _cam
from ..pipeline import framechain
from ..pipeline.checkpoint import has_file as _has_file
from ..pipeline import transitions as _tr
from ..pipeline import variation as _var
from ..budget import spent_total
from ..storage import get_storage
from ..project import DEFAULT_ASPECT, chapter_flag, effective_motion, scored_audio as _scored_audio
from ..storage.base import chapter_status, chapter_title


def _effects_resolved(store, profile, override):
    """生效特效（仅章节/项目点名，与合成端 effects_for 同源解析；画风清单
    不自动叠加，语义真源见 models.effects_for）。
    含未知名时不让章节页整页失败：原样下发，chip 由前端标「未注册」，
    真正的硬闸在合成端（effects_for 会拒绝）。"""
    if store is not None and hasattr(store, "effects_for"):
        try:
            return store.effects_for(profile, override)
        except ConfigError:
            return list(override or [])
    return list(override or [])

# 渲染档读侧：唯一真源 project.effective_motion（别名归一 + 未表态按内容定档）


def _alive(data: dict) -> bool:
    """软删过滤：is_deleted=1 的项目不进任何清单/聚合（summary/queue/board/
    search/library）——详情页除外（要渲染回收站状态与恢复入口）。"""
    return not int((data or {}).get("is_deleted") or 0)


def _ccov(cdata: dict) -> str | None:
    """章节封面主图（多比例 cover 块取 primary=3:4 竖版）。"""
    c = cdata.get("cover")
    return c.get("primary") if isinstance(c, dict) else None


def _cover_urls(data: dict, ws_root: Path, key: str = "images") -> dict:
    """封面多比例 URL 表（cover.images 带字成品 / cover.bg 无字真源，
    默认竖3:4+横4:3双套）——前端按容器形状自动适配：宽容器取 4:3 横版、
    竖容器取 3:4 竖版；卡片类自绘标题的容器取 bg 无字版防双重标题。"""
    c = data.get("cover")
    imgs = (c or {}).get(key) if isinstance(c, dict) else None
    return {a: media_url(ws_root / p) for a, p in (imgs or {}).items()
            if (ws_root / p).is_file()}


def media_url(path) -> str:
    p = Path(path).resolve()
    return _mtime_v("/media?path=" + urllib.parse.quote(str(p)), p)


def poster_url(path) -> str:
    return "/poster?path=" + urllib.parse.quote(str(Path(path).resolve()))


def _mtime_v(url: str, p: Path) -> str:
    """URL 带 mtime 版本参数：重生/局部改造复用同名文件时突破浏览器缓存。"""
    try:
        return f"{url}&v={int(p.stat().st_mtime)}"
    except OSError:
        return url


def _murl(path) -> str | None:
    """存在才给 URL，不存在给 None（前端据此渲染占位态）。
    已上云的媒体（http URL）直接返回——浏览器直连 OSS，本地零流量。"""
    if not path:
        return None
    if isinstance(path, str) and path.startswith("http"):
        return path
    p = Path(path)
    return media_url(p) if p.is_file() else None


def _asset_version_hist(vers) -> list[dict]:
    """设定图归档条目 → 前端视图（媒体转 URL，含归档原因/参数快照）。与分镜版本谱系同构。"""
    return [{"v": e.get("v"), "at": e.get("at"), "reason": e.get("reason"),
             "params": e.get("params"), "url": _murl(e.get("file"))}
            for e in (vers or []) if _murl(e.get("file"))]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _motion(data: dict) -> str:
    return effective_motion(data)


def _chain_view(shot: dict, nxt: dict | None, why: str) -> tuple[int | None, str | None]:
    """本镜的链态视图：`(衔接到的镜号 | None, 断链措辞 | None)`。

    结构与生成模式的判定**全部**来自 `framechain.scan`——焊缝两端都在那里判完，
    这里不重判：同一条规则的第二份抄本在规则扩面（V2V / previz 末帧）时必漏判。
    只剩「下一镜缺图」在这里补：那是磁盘状态、不是结构，引擎发末帧前同样要确认
    图在盘上，页面标着衔接而实际退回纯首帧生成，同样是对不上。
    不在衔接态、以及转场镜（本就不调用视频模型）两项都留空：逐镜挂一句「不衔接」是噪音。
    """
    if why == "off" or shot.get("kind") == "transition":
        return None, None
    if nxt is None:
        return None, framechain.BREAK_ZH.get(why)
    if not (nxt.get("image") or nxt.get("images")):
        return None, framechain.BREAK_ZH["no_image"]
    return nxt.get("id"), None


# 章节标题唯一解析口（章节文档赢、登记表兜底）——实体在存储层共用：
# 引擎域 business 的成本页同用，studio 私有的话它只能再抄一份
_chapter_title = chapter_title


def _aspects(data: dict) -> list[str]:
    a = data.get("aspects")
    return list(a) if a else [data.get("aspect", DEFAULT_ASPECT)]


def _shots_duration(data: dict) -> float:
    return round(sum(float(s.get("dur") or 0) for s in data.get("shots", [])), 2)


def _aspect_from_tag(tag: str) -> str:
    return tag.replace("x", ":")


def _first_output(workdir: Path) -> Path | None:
    outdir = workdir / "output"
    mp4s = sorted(outdir.glob("*.mp4")) if outdir.is_dir() else []
    return mp4s[0] if mp4s else None


def _shot_thumb(cdata: dict) -> str | None:
    """卡片图源的最后一级回落：首个已出图的正镜分镜图。

    图源只有「封面 → 成片海报帧」两级的话，封面没出、成片又没合的整个制作期里
    两级全空，项目卡与章节卡就是整片空白，既不报错也无迹象。分镜图零成本、与本章
    同画风，比空白强。它**不是封面**——不进 `cover`/`covers` 表，也不改变
    `cover_missing` 判定，只当缩略图源；真封面一出立刻被顶替。
    弃镜/转场镜跳过判据复用 `variation.active_shots` 单一真源。
    """
    for s in _var.active_shots(cdata):
        url = _murl(s.get("image"))
        if url:
            return url
    return None


def _outputs(workdir: Path) -> list[dict]:
    """output/*.mp4 → [{aspect, video, poster, size, mtime, watermarked}]，按文件名
    _9x16 等后缀识别比例；`<base>_wm_<tag>.mp4` 是水印版（防搬运）标 watermarked=True，
    前端播放器默认展示水印版（交付版），与原片并存可切换。"""
    outdir = workdir / "output"
    out = []
    for mp4 in (sorted(outdir.glob("*.mp4")) if outdir.is_dir() else []):
        m = re.search(r"_(\d+x\d+)$", mp4.stem)
        st = mp4.stat()
        out.append({
            "aspect": _aspect_from_tag(m.group(1)) if m else None,
            "name": mp4.name,
            "video": media_url(mp4),
            "poster": poster_url(mp4),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "watermarked": "_wm_" in mp4.stem,
        })
    return out


def _previz_reel_view(workdir: Path) -> dict | None:
    """全片预演视图：`previz/reel.mp4` + `reel.json` 都在才算数。

    **纯磁盘推导**（同「章节状态由产物推导不落盘」）：reel 是观看物不是契约资产，
    指针写进顶层 `previz` 会被下一次保存编排整体替换掉。清单里 `built_at` 与
    `shots` 让前端能说清「这条片子是基于哪几镜、什么时候合的」——只给一个能播的
    URL，用户没法判断它是不是漏了刚渲的那一镜。
    """
    mp4 = workdir / _pz.PREVIZ_SUBDIR / _pz.REEL_NAME
    man = workdir / _pz.PREVIZ_SUBDIR / _pz.REEL_MANIFEST
    if not mp4.is_file() or not man.is_file():
        return None
    try:
        d = json.loads(man.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    st = mp4.stat()
    return {
        "video": media_url(mp4), "poster": poster_url(mp4),
        "name": mp4.name, "size": st.st_size, "mtime": st.st_mtime,
        "built_at": d.get("at"), "duration": d.get("duration"),
        "mode": d.get("mode"), "fps": d.get("fps"),
        "shots": [x.get("id") for x in (d.get("shots") or [])],
        "skipped": d.get("skipped") or [],
    }


def _sketch_view(s: dict) -> dict | None:
    """单镜的简笔板视图：拆拍就绪（authored 或自动）/已出板的镜才下发。

    · `lines`=逐拍对照行（与板提示词面板内容同一拼装，灯箱拍表用）；
    · `warn`=authored t 的秒段覆盖体检（beats_coverage 单一真源）；
    · `stale`=时长漂移 / `stale_beats`=拍序列漂移（beats/提示词改过而板未重生）
      ——判据唯一真源 `sketchboard.board_drift`，scanner 只消费绝不自算。
    全部零探测（纯文档字段，scanner 不跑 ffmpeg）。"""
    # total 先行（板生成时的秒数基准优先）：自动拆拍的拍数随时长收敛，
    # 前端显示的拍数必须与板上真实格数同源
    gs = (s.get("gen") or {}).get("sketch") or {}
    total = gs.get("seconds") or s.get("dur")
    beats, auto = _sk.effective_beats(s, total)
    board = _sk.board_of(s)
    if not (board or beats):
        return None
    warns = [w for w in (_sk.beats_coverage(s, total), _sk.beats_density(s, total)) if w]
    view = {"sheet": _murl(board), "beats": len(beats), "auto": auto,
            "lines": _sk.panel_lines(s, total=total) if beats else [],
            "warn": "；".join(warns) or None}
    drift = _sk.board_drift(s)
    if drift:
        if drift.get("dur"):
            view["stale"] = drift["dur"]
        if drift.get("beats"):
            view["stale_beats"] = True
    return view


def _sketch_stats(shots: list) -> dict:
    """简笔分镜台区块统计：只数正镜（转场跳过，与 `sketch gen` 同口径）。
    `beats` = 拆拍就绪数（authored 或自动拆拍，即"点了就能生成"的镜）；
    `boards` 只数**板文件真在盘**的镜——登记指针悬空时报"已出板"会让用户去点一个
    404 的灯箱。"""
    from ..pipeline.checkpoint import has_file
    beats = boards = total = 0
    for s in shots:
        if not isinstance(s, dict) or s.get("kind") == "transition":
            continue
        total += 1
        # 这里只判"拆不拆得出料"（拍数多少与统计无关），但仍按 dur 传 total
        # 保持真源口径统一——绝不留裸调用（漏传一处就是一条分叉的路）
        if _sk.effective_beats(s, s.get("dur"))[0]:
            beats += 1
        b = _sk.board_of(s)
        if b and has_file(b):
            boards += 1
    return {"beats": beats, "boards": boards, "total": total}


def _audio_script_view(proj_view, store, data: dict, adir) -> dict:
    """音频剧本台区块（`audio_mode: scored`）：分段表 + 逐段剧本与音轨状态。

    分段判据取 `audioscript.plan` 单一真源——网页绝不自己按 dur 累加切一遍；
    切在哪一镜决定了音乐在哪里重新起头，两处各算一份必然分叉，而对不上的代价是
    用户照页面写好了时间控制，真跑却切在别处。

    **恒下发**（不只在 scored 时）：`mode` 告诉前端当前走哪条路，剧本与分段在
    tracks 下同样看得见——不然要切换过去的人无从判断这一章切几段、要写多少字。"""
    from ..pipeline.checkpoint import has_file
    raw = data.get("audio_script")
    parts = raw.get("segments") if isinstance(raw, dict) else None
    written = ([str(x) for x in parts] if isinstance(parts, list)
               else ([str(raw)] if isinstance(raw, str) and raw.strip() else []))
    gen = {int(e.get("no") or 0): e for e in
           (((data.get("gen") or {}).get("score") or {}).get("segments") or [])}
    segs, problems = [], []
    if proj_view is not None:
        try:
            from .. import audioscript
            problems = audioscript.check(proj_view)
            # 参考音计划：谁的声音会随请求发出去、谁只能靠文字描述。判据与真发同一个
            # 函数——按秒计费的一步，页面说带音色而实发没带是最贵的那种不一致。
            # default_ref 的回落链也必须与真发同源（旁白锁 > profile 默认 > 项目
            # voice_id）：函数同一份而输入不同源，未选旁白音色的章节会被误报
            # 「⚠ 无参考音」，而实发会用 profile 默认音色现合成参考音锚上
            try:
                tts_params = dict(store.profile(data.get("profile")).get("tts") or {})
            except Exception:  # noqa: BLE001  画风名失效不该让整张分段表变空
                tts_params = {}
            default_ref = voicecast.default_voice_ref(data, tts_params)

            from .. import voicebank

            def anchor(sg, text):
                try:
                    plan = audioscript.anchor_plan(proj_view, store, sg, default_ref)
                except Exception:  # noqa: BLE001  解析不出不阻断整台，按「都没锚上」显示
                    return {"anchored": [], "loose": []}
                rows = []
                for r in plan["anchored"]:
                    vt = r["voice_type"]
                    # 试听地址与真发同源：在盘事实统一走 voicebank.anchor_clip_for
                    # （定制=档案不可变音频，官方=项目级锚定缓存）；缓存还没合成过
                    # 就给 None，页面照实说「发送时现合成」
                    clip, _custom = voicebank.anchor_clip_for(proj_view, vt)
                    # 绑定行预览 = 引擎发送时会加在正文前的那一行（判据同 _score_anchor：
                    # 剧本已写声线定义只补对位，没写才连描述一起补）
                    desc, _real = audioscript.speaker_voice_desc(proj_view, r["who"])
                    bind = (f"{r['who']} 的饰演者为@音频{r['no']}。"
                            if audioscript.has_voice_def(text, r["who"])
                            else f"{audioscript.voice_def(r['who'], desc)}，"
                                 f"饰演者为@音频{r['no']}。")
                    rows.append({**r, "media": _murl(clip), "bind": bind})
                return {"anchored": rows, "loose": plan["loose"]}

            for sg in audioscript.plan(proj_view):
                no = sg["no"]
                meta = gen.get(no) or {}
                # 段音轨可能已随 oss sync 上云（`gen.score.segments[].file` 被改写成
                # URL）：按 Path.is_file 判会把已买断的段翻成「未生成」，生成弹窗
                # 还会默认勾选重买——in 盘判定必须走 URL 感知的 has_file
                part = meta.get("file") or str(adir / f"score_{no:02d}.mp3")
                # 段谱系：生成式模型每次演绎都不同，「这版和上版哪个好」只能听着比，
                # 所以历史版也要给出可播放地址（真源 versioning 的段版本栈）
                hist = [{"v": e.get("v"), "at": e.get("at"), "reason": e.get("reason"),
                         "media": _murl(e.get("file"))}
                        for e in (meta.get("versions") or []) if _murl(e.get("file"))]
                script = written[no - 1] if no <= len(written) else ""
                # 没写过就把**底稿**一起下发：起草是确定性纯函数（零成本、无副作用），
                # 能算出来的东西不该等人点一下才出现。前端拿它预填空框并标「底稿·未存」
                # ——**只填框不落盘**，写进文档仍然只有存稿这一条路
                draft = ""
                if not str(script).strip():
                    try:
                        draft = audioscript.draft_segment(proj_view, sg)[0]
                    except Exception:  # noqa: BLE001  纯画面段起不了稿，留空交人写
                        draft = ""
                segs.append({**sg, "script": script, "draft": draft,
                             "generated": has_file(part),
                             "media": _murl(part),
                             "actual": meta.get("actual"),
                             # 绑定行预览按「将要发出去的那份」判：已存稿看存稿，
                             # 没存稿看预填底稿（起草头部自带声线定义段）
                             "anchor": anchor(sg, str(script).strip() or draft),
                             "versions": hist,
                             "current_v": len(meta.get("versions") or []) + 1})
        except Exception:  # noqa: BLE001  区块是增强信息，分镜不全时不阻断制作台
            segs, problems = [], []
    # 整轨同段音轨一个口径：`audio.score_file` 已上云是 URL，回落常规路径兜底
    score = (data.get("audio") or {}).get("score_file") or (adir / "score.wav")
    return {
        "mode": "scored" if _scored_audio(data) else "tracks",
        "segments": segs,
        "problems": problems,
        # 只数真有内容的段：存稿会把空段一并写进 segments[]（条数必须等于分段数），
        # 按长度数会把「两个空框」报成「已写两段」
        "written": sum(1 for x in written if str(x).strip()),
        "score": _murl(score),
        "duration": ((data.get("gen") or {}).get("score") or {}).get("duration"),
        "limit": _score_limit(),
    }


def _score_limit() -> float:
    from .. import audioscript
    return audioscript.MAX_SEGMENT_SEC


def _uses_video(data: dict) -> bool:
    """dubbed/native 判据取 `project.uses_seedance` 纯函数单一真源——与
    `_needs_narration_track` 同一条纪律：网页绝不按 motion 再抄一份。刻意不构造
    Project：看板逐章调用，构造快照会对每章全部镜的版本栈做深拷贝，只为读一个字符串。"""
    from ..project import uses_seedance
    return uses_seedance(data)


def _needs_narration_track(data: dict) -> bool:
    """这一章要不要产出旁白轨——**判据取 `Project.needs_narration_track` 单一真源**。

    网页绝不在这里按 motion 再抄一份：`scored`（整章音频剧本）下一句都不配，
    而抄本只看 motion，会把「配音 0/9 待办」挂在一条根本不跑 tts 的章节上，
    还把它算进成本看板的缺项——与 framechain 那条「页面只显示不自算」同一条纪律。
    问的是章级那一个：`needs_tts` 答的是「这一镜必须有 audio 产物」，而 native
    混烧的对白镜按 `voicecast.in_narration_track` 永远没有，拿它当阶段条的判据
    会把一道必做且会在合成期硬拦的工序显示成「本章不需要」。
    构造轻量 Project 视图零 IO（`__init__` 只 resolve 路径 + 取人工表态基线）。"""
    from ..project import Project
    return Project(Path("."), data).needs_narration_track


def _watermark_view(data: dict, has_output: bool) -> dict:
    """水印状态：三类可任意组合——
      · 漂移水印（防搬运）：text=预填文案（章节 watermark > branding）、floating_on=是否已设；
      · 固定角标（品牌署名·字幕式烧录）：fixed{text 预填, position 四角, on 是否已设}；
      · 底部水印（半透明常驻署名）：bottom{text 预填, on 是否已设}；
      · active=是否已生成水印版（player 默认播水印版用）、has_output=有无成片可打水印。
    前端据此预填对话框与按钮态。"""
    from ..branding import load_branding
    bland = load_branding()
    text = ((data.get("watermark") or "").strip()
            or ((bland.get("watermark") or {}).get("text") or "").strip())
    fx = data.get("watermark_fixed") or {}
    fx_cfg = bland.get("watermark_fixed") or {}
    fx_text = ((fx.get("text") or "").strip() or (fx_cfg.get("text") or "").strip())
    bt = data.get("watermark_bottom") or {}
    bt_cfg = bland.get("watermark_bottom") or {}
    return {
        "text": text,
        "active": bool(data.get("output_wm")),
        "has_output": has_output,
        "floating_on": bool((data.get("watermark") or "").strip()),
        "fixed": {
            "text": fx_text,
            "position": fx.get("position") or fx_cfg.get("position") or "br",
            "on": bool((fx.get("text") or "").strip()),
        },
        "bottom": {
            # 预填与漂移/角标同链（章节 > branding）；on 仍只认章节自身——
            # 预填只是显示，绝不等于已启用
            "text": (bt.get("text") or "").strip() or (bt_cfg.get("text") or "").strip(),
            "on": bool((bt.get("text") or "").strip()),
        },
    }


def _subtitle_style_view(store, data: dict) -> dict:
    """字幕样式视图（放映区「字幕样式」面板的数据源）：`effective`=生效值
    （profile 画风样式打底 + 章节 subtitle 块覆盖，与烧录同一条 `sub_cfg` 判据），
    `override`=章节覆盖块里样式键的原文（面板据此区分「画风缺省」与「本章已调」）。
    只下发样式面白名单（`actions._SUBTITLE_STYLE_KEYS`）——lang/mode 等行为键
    不进面板，改错一个整章字幕换语言/换版式。"""
    from ..pipeline.subtitle import _CAPTION_DEFAULTS, resolve_size, sub_cfg
    from ..project import DEFAULT_ASPECT, Project
    from .actions import _SUBTITLE_STYLE_KEYS
    try:
        cfg = sub_cfg(store, Project(Path("."), data))
    except Exception:  # noqa: BLE001  样式视图是增强信息，失败不阻断制作台
        cfg = {}
    # 字号缺省随画布横竖分治（与烧录侧 build_from_timeline 同判据）：面板显示的
    # 生效值必须与真烧出来的一致，否则竖屏章节面板显示横屏基准、成片却按竖屏缺省烧
    try:
        w, hgt = store.canvas(data.get("aspect") or DEFAULT_ASPECT)
    except Exception:  # noqa: BLE001
        w, hgt = 1920, 1080
    base = dict(_CAPTION_DEFAULTS)
    ov = data.get("subtitle") or {}
    # 字号单独走 `resolve_size`：横屏对画风字号是硬覆盖，按 `cfg.get("size")` 取会
    # 显示成画风那个数，而真烧出来的是横屏缺省——面板与成片必须是同一个数
    effective = {k: cfg.get(k, base.get(k)) for k in _SUBTITLE_STYLE_KEYS}
    if "size" in _SUBTITLE_STYLE_KEYS:
        effective["size"] = resolve_size(cfg, w, hgt)
    return {
        "effective": effective,
        "override": {k: ov[k] for k in _SUBTITLE_STYLE_KEYS if k in ov},
    }


def _chapter_status(cf: Path, data: dict) -> str:
    # 判据真源在 storage.base.chapter_status（workspace/mysql 索引列同源）
    return chapter_status(cf.parent / f"{cf.stem}_work", data)


def _merge_cost(total: dict, cost: dict | None) -> None:
    """分币种分项桶。数值口径与 `budget.spent_total` 逐条对齐（数字字符串计入、
    NaN/Inf 拒收）——两套口径会让总览合计 ≠ 各章 cost_total 之和，且 Inf 一旦
    进桶，round() 会把整页 overview JSON 污染掉。"""
    if not cost:
        return
    cur = cost.get("currency", "CNY")
    bucket = total.setdefault(cur, {})
    for k, v in cost.items():
        if k == "currency":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(f):
            continue
        bucket[k] = round(bucket.get(k, 0.0) + f, 4)


# ============================================================================
# 角色 / 道具 / 场景（设定集）
# ============================================================================
def _character_view(c: dict, store) -> dict:
    """角色预设 → 展示模型：设定图 + 在用音色。

    选角那一整块（档案、引用账、候选）由 `voice_bank` 逐实体单独下发——它对旁白与
    角色同形，塞进角色条目就得给旁白再写一份。"""
    return {
        "name": c.get("name"),
        "role": c.get("role") or "",
        "appearance": c.get("appearance") or "",
        "outfit": c.get("outfit") or "",
        "hair": c.get("hair") or "",
        "weapon": c.get("weapon") or "",
        "voice": c.get("voice"),
        "voice_type": store.resolve_voice(c.get("voice")) if store else c.get("voice"),
        "sheet": _murl(c.get("sheet")),
        "comments": c.get("comments") or [],        # 设定图提意见（锚定批注，重生成时编译）
        # 设定图候选宫格（project refs --candidates N，点选定稿）
        "sheet_candidates": [{"no": k + 1, "url": u} for k, u in
                             enumerate(_murl(p) for p in (c.get("sheet_candidates") or []))
                             if u],
        "sheet_picked": c.get("sheet_picked"),
        "ref_image": _murl(c.get("ref_image")),
        "refs": c.get("refs"),          # 逐张垫图覆盖（None=跟随默认 / []=不用 / [路径…]=精确）
        "versions": len(c.get("versions") or []),        # 设定图归档数（重生成/回滚自增）
        "version_history": _asset_version_hist(c.get("versions")),
    }


def _prop_view(p: dict) -> dict:
    return {"name": p.get("name"), "desc": p.get("desc") or "",
            "kind": p.get("kind") or "prop", "sheet": _murl(p.get("sheet")),
            "comments": p.get("comments") or [],        # 设定图提意见
            "sheet_candidates": [{"no": k + 1, "url": u} for k, u in
                                 enumerate(_murl(x) for x in (p.get("sheet_candidates") or []))
                                 if u],
            "sheet_picked": p.get("sheet_picked"),
            "refs": p.get("refs"),          # 逐张垫图覆盖（None=默认 / []=不用 / [路径…]=精确）
            "versions": len(p.get("versions") or []),
            "version_history": _asset_version_hist(p.get("versions"))}


def _scene_view(x: dict) -> dict:
    """取景地 → 展示模型：与道具同构的基准图那一套，外加成对的俯视布局图。

    俯视图有自己的版本栈与提意见池（`topview_versions` / `topview_comments`），
    与基准图互不串——两张图各自重生、各自回滚，共用一个池就会把「墙的位置画错了」
    的批注带进基准图的重生请求。"""
    return _prop_view(x) | {
        "kind": "scene",
        "topview": _murl(x.get("topview_sheet")),
        "topview_comments": x.get("topview_comments") or [],
        "topview_versions": len(x.get("topview_versions") or []),
        "topview_version_history": _asset_version_hist(x.get("topview_versions")),
    }


def _graph_view(data: dict) -> dict | None:
    """关系图谱视图（剧本工作台「图谱」Tab）：透传 nodes/edges + summary，并为**名字
    命中已建设定图**的节点挂上缩略图 thumb + ref（前端点该节点即开设定图富灯箱，复用
    ch01 的 actx 重生成/点评链路）。无节点返回 None。引擎不消费此数据，纯规划/可视化。"""
    g = data.get("graph") or {}
    nodes = g.get("nodes") or []
    if not nodes:
        return None
    sheets = {}   # name -> (kind, sheet_path)
    for c in data.get("characters") or []:
        if c.get("name") and c.get("sheet"):
            sheets[c["name"]] = ("character", c["sheet"])
    for p in data.get("props") or []:                       # 角色优先，道具不覆盖同名角色
        if p.get("name") and p.get("sheet") and p["name"] not in sheets:
            sheets[p["name"]] = ("prop", p["sheet"])
    for x in data.get("scenes") or []:                      # 具名场景（图谱 location 节点挂它）
        if x.get("name") and x.get("sheet") and x["name"] not in sheets:
            sheets[x["name"]] = ("scene", x["sheet"])
    out_nodes = []
    for n in nodes:
        m = dict(n)
        hit = sheets.get(n.get("name"))
        if hit:
            m["thumb"] = _murl(hit[1])
            m["ref"] = {"kind": hit[0], "name": n.get("name")}
        out_nodes.append(m)
    return {"summary": g.get("summary") or "", "nodes": out_nodes,
            "edges": g.get("edges") or [], "updated_at": g.get("updated_at")}


# ============================================================================
# 片库（跨项目扫描 *_work/output/*.mp4，路由 #/library）
# ============================================================================
def library(root: Path) -> list[dict]:
    videos = []
    if not root.is_dir():
        return videos
    # 属主项目软删过滤：片库是文件系统扫描（不经项目清单），必须自查
    # 章节所属项目文档的 is_deleted——已删项目的成片不进片库
    _pdoc_cache: dict = {}

    def _owner_alive(pid) -> bool:
        if not pid:
            return True                    # 散落文件模式（无属主项目）照常收录
        if pid not in _pdoc_cache:
            _pdoc_cache[pid] = _read_json(root / str(pid) / "project.json")
        return _alive(_pdoc_cache[pid])

    for work in root.rglob("*_work"):
        outdir = work / "output"
        if not outdir.is_dir():
            continue
        stem = work.name[: -len("_work")]
        pj = work.parent / f"{stem}.json"
        data = _read_json(pj) if pj.is_file() else {}
        chapter = data.get("chapter") or {}
        if not _owner_alive(chapter.get("project")):
            continue
        for item in _outputs(work):
            videos.append({
                **item,
                "id": data.get("id") or stem,
                "title": chapter.get("title") or data.get("theme") or stem,
                "theme": data.get("theme") or stem,
                "profile": data.get("profile") or "narration",
                "platform": data.get("platform") or [],
                "motion": _motion(data),
                "duration": _shots_duration(data) or data.get("duration"),
                "shots_count": len(data.get("shots", [])),
                "effects": data.get("effects"),
                "cost": data.get("cost"),
                "project": chapter.get("project"),
                "chapter": chapter.get("id"),
            })
    videos.sort(key=lambda v: v["mtime"], reverse=True)
    return videos


# ============================================================================
# 工作区（项目列表）
# ============================================================================
def workspace_summary(ws_root: Path) -> list[dict]:
    projects = []
    if not ws_root:
        return projects
    store = get_storage(ws_root)
    for data in store.list_projects():
        if not _alive(data):        # 软删过滤（is_deleted=0 才进清单）
            continue
        d = ws_root / data.get("id", "")
        chapters, cover, rendered, shots_total = [], None, 0, 0
        thumb = None                       # 三级回落的兜底：首章首个已出图的分镜图
        motions: set[str] = set()          # 项目内各章渲染模式聚合（供片库式筛选）
        # 图源三级回落：真封面（cover 命令产物，多比例取 primary=3:4 竖版）→
        # 首个成片海报帧 → 分镜图。第三级兜住前两级都缺的制作期空白
        pcov = (data.get("cover") or {}).get("primary")
        has_cover = bool(pcov and (ws_root / pcov).is_file())
        if has_cover:
            cover = media_url(ws_root / pcov)
        for ch in sorted(data.get("chapters", []), key=lambda c: c.get("order", 0)):
            cf = d / "chapters" / f"{ch.get('id')}.json"
            cdata = store.load_chapter(data.get("id"), ch.get("id")) or {}
            status = _chapter_status(cf, cdata) if cdata else "missing"
            n = len(cdata.get("shots", []))
            shots_total += n
            if status == "rendered":
                rendered += 1
                first = _first_output(cf.parent / f"{cf.stem}_work")
                if first and cover is None:
                    cover = poster_url(first)
            if thumb is None:
                thumb = _shot_thumb(cdata)
            ccov = (cdata.get("cover") or {}).get("primary") \
                if isinstance(cdata.get("cover"), dict) else None
            cmotion = _motion(cdata) if cdata else None
            if cmotion:
                motions.add(cmotion)
            chapters.append({"id": ch.get("id"), "title": _chapter_title(ch, cdata),
                             "order": ch.get("order"), "status": status, "shots": n,
                             "motion": cmotion,
                             "cover": media_url(ws_root / ccov)
                             if ccov and (ws_root / ccov).is_file() else None})
        refs = [c for c in data.get("characters", []) if c.get("sheet")]
        projects.append({
            "id": data.get("id"), "title": data.get("title"), "theme": data.get("theme"),
            "profile": data.get("profile"), "skill": data.get("skill"),
            "platform": data.get("platform") or [],
            "aspect": data.get("aspect"), "status": data.get("status"),
            "created_at": data.get("created_at"), "updated_at": data.get("updated_at"),
            "characters": len(data.get("characters", [])),
            "props": len(data.get("props", [])),
            "scenes": len(data.get("scenes", [])),
            "has_refs": bool(refs) or bool(data.get("scene_ref")),
            "chapters": chapters, "rendered": rendered,
            "motions": sorted(motions),         # 章级渲染模式去重（片库式筛选用）
            "shots": shots_total, "cover": cover or thumb,
            # 真封面缺位（海报帧/分镜图顶上来不算数）——前端据此打「无封面」标，
            # 否则兜底图源会让缺口看起来像已经做过封面
            "cover_missing": not has_cover,
            "covers": _cover_urls(data, ws_root),   # 多比例表，前端按容器适配
            "covers_bg": _cover_urls(data, ws_root, "bg"),   # 无字真源（卡片自绘标题用）
            "logline": (data.get("design") or {}).get("logline") or "",
        })
    # 新建在前：Studio 侧的项目清单统一按 created_at 倒序（`%Y-%m-%dT%H:%M:%S` 可直接
    # 字典序比较）。缺 created_at 的老文档排在末尾；同一时间戳靠稳定排序保留存储层的
    # id 升序。存储层仍按 id 排（CLI 清单与库行协调依赖它），这里只改展示序。
    projects.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return projects


def _voice_bank_view(ws_root: Path, store, pid: str, data: dict) -> dict:
    """选角下发：`{实体: 视图}`。判据（在用哪条、引用账、候选是否已入档）全部取自
    `voicebank`——页面只展示不自算，两侧各写一份判据必然分叉。

    音频路径统一过 `media_url`：档案音频与候选都要能在网页里直接试听。"""
    from ..workspace import Series, Workspace
    series = Series(Workspace(ws_root), pid, data)
    out = {}
    for owner, v in voicebank.bank_views(series, store).items():
        v["anchor"] = _murl(v.get("anchor"))
        v["casts"] = [{**c, "clip": _murl(c.get("clip"))} for c in v["casts"]]
        for key in ("audition", "custom_audition"):
            v[key] = {**v[key],
                      "entries": [{**e, "media": _murl(e.get("path")), "path": None}
                                  for e in v[key].get("entries") or []]}
        out[owner] = v
    return out


# ============================================================================
# 项目详情
# ============================================================================
def project_detail(ws_root: Path, store, pid: str) -> dict | None:
    docs = get_storage(ws_root)
    data = docs.load_project(pid)
    if not data:
        return None

    chapters = []
    for ch in sorted(data.get("chapters", []), key=lambda c: c.get("order", 0)):
        cf = ws_root / pid / "chapters" / f"{ch.get('id')}.json"
        cdata = docs.load_chapter(pid, ch.get("id")) or {}
        work = cf.parent / f"{cf.stem}_work"
        first = _first_output(work)
        chapters.append({
            "id": ch.get("id"), "title": _chapter_title(ch, cdata),
            "order": ch.get("order"), "created_at": ch.get("created_at"),
            "status": _chapter_status(cf, cdata) if cf.is_file() else "missing",
            "shots": len(cdata.get("shots", [])),
            "duration": _shots_duration(cdata),
            "motion": _motion(cdata),
            "aspects": _aspects(cdata),
            "cost": cdata.get("cost"),
            # 合计服务端算好（budget.spent_total）；没记过账下发 None
            "cost_total": spent_total(cdata) if cdata.get("cost") else None,
            "video": media_url(first) if first else None,
            # 缩略图三级回落：章节封面（cover 命令产物，primary=3:4）→ 成片海报帧
            # → 分镜图。`cover` 只认真封面，前端据此区分「已做封面」与「兜底顶上」
            "poster": (media_url(ws_root / _ccov(cdata))
                       if _ccov(cdata) and (ws_root / _ccov(cdata)).is_file()
                       else (poster_url(first) if first else _shot_thumb(cdata))),
            "cover": (media_url(ws_root / _ccov(cdata))
                      if _ccov(cdata) and (ws_root / _ccov(cdata)).is_file()
                      else None),
            "covers": _cover_urls(cdata, ws_root),   # 多比例表，前端按容器适配
            "covers_bg": _cover_urls(cdata, ws_root, "bg"),
            "updated_at": cf.stat().st_mtime if cf.is_file() else None,
        })

    _pcov = (data.get("cover") or {}).get("primary")
    return {
        "id": data.get("id"), "title": data.get("title"), "theme": data.get("theme"),
        "profile": data.get("profile"), "skill": data.get("skill"),
        "platform": data.get("platform") or [],
        "aspect": data.get("aspect"), "status": data.get("status"),
        # 软删态照常下发——详情页要渲染回收站横幅与恢复入口（清单类才过滤）
        "is_deleted": int(data.get("is_deleted") or 0),
        "deleted_at": data.get("deleted_at"),
        "cover": media_url(ws_root / _pcov)
        if _pcov and (ws_root / _pcov).is_file() else None,
        "covers": _cover_urls(data, ws_root),   # 多比例表，前端按容器适配
        "created_at": data.get("created_at"), "updated_at": data.get("updated_at"),
        "template": data.get("template"),   # 平台规格模板快照（前端做达标核对）
        "design": data.get("design") or {},
        "scene": data.get("scene") or "",
        "scene_ref": _murl(data.get("scene_ref")),
        "scene_ref_candidates": [{"no": k + 1, "url": u} for k, u in
                                 enumerate(_murl(p) for p in
                                           (data.get("scene_ref_candidates") or []))
                                 if u],
        "scene_ref_picked": data.get("scene_ref_picked"),
        "scene_comments": data.get("scene_comments") or [],   # 场景设定图提意见
        "scene_refs": data.get("scene_refs"),   # 场景逐张垫图覆盖（None=默认 / []=不用 / [路径…]=精确）
        "scene_versions": len(data.get("scene_ref_versions") or []),   # 场景设定图归档数
        "scene_version_history": _asset_version_hist(data.get("scene_ref_versions")),
        # 全局固定场景的俯视布局图（与具名取景地的 topview 同构，落系列文档顶层）
        "scene_topview": _murl(data.get("scene_topview_ref")),
        "scene_topview_comments": data.get("scene_topview_comments") or [],
        "scene_topview_versions": len(data.get("scene_topview_versions") or []),
        "scene_topview_version_history":
            _asset_version_hist(data.get("scene_topview_versions")),
        # 选角（旁白与每个角色同形）：在用的那把 + 音色档案（含引用账）+ 两路候选
        "voice_bank": _voice_bank_view(ws_root, store, pid, data),
        "skip_design": bool(data.get("skip_design")),
        "characters": [_character_view(c, store)
                       for c in data.get("characters", [])],
        "props": [_prop_view(p) for p in data.get("props", [])],
        "scenes": [_scene_view(x) for x in data.get("scenes", [])],
        "chapters": chapters,
        # 剧本改编（系列文档）：源文本指针 / 拆书 / 分集大纲（前端「剧本」页渲染）
        "source": data.get("source") or None,
        "adaptation": data.get("adaptation") or {},
        "episodes": data.get("episodes") or [],
        # 参考库/风格垫图（项目级参考图，默认注入每张设定图/分镜图生成）：
        # {path 用于删除/切换, url 用于展示, on 是否默认套用全局生成}
        "moodboard": [
            {"path": it["path"], "url": media_url(Path(it["path"])), "on": it.get("on", True)}
            for it in ({"path": x, "on": True} if isinstance(x, str) else (x or {})
                       for x in (data.get("moodboard") or []))
            if it.get("path") and Path(it["path"]).is_file()],
        # 人物关系 / 世界观图谱（剧本工作台「图谱」Tab；含设定图挂载，见 _graph_view）
        "graph": _graph_view(data),
    }


def script_detail(ws_root: Path, store, pid: str) -> dict | None:
    """剧本工作台负载 = project_detail（源文本指针/拆书/分集/角色/道具/章节）
    + 源正文分段**目录**（segments.json 元数据：偏移/预览/标题/段字数）。

    正文不随首屏下发——正文按段懒加载走 script_segment（超长小说首屏只传目录，
    避免把整篇正文塞进单个 JSON 拖垮响应）。source_chars 取自入库真源指针
    （set_source 落的 chars），免读全文。"""
    base = project_detail(ws_root, store, pid)
    if base is None:
        return None
    src = base.get("source") or {}      # project_detail 已下发 source 指针，无需再取库
    segs, seg_kind = [], None
    segfile = ws_root / pid / "source" / "segments.json"
    if segfile.is_file():
        try:
            digest = json.loads(segfile.read_text(encoding="utf-8"))
            segs = digest.get("segments") or []
            seg_kind = digest.get("segment_kind")
        except Exception:  # noqa: BLE001  索引损坏不阻断详情页（目录仍可看）
            pass
    for s in segs:                       # 目录瘦身：只留偏移/预览/元数据 + 段字数
        a, b = s.get("char_start"), s.get("char_end")
        s["chars"] = (b - a) if isinstance(a, int) and isinstance(b, int) else None
        s.pop("text", None)              # 正文不随首屏下发（按段懒加载）
    base["segments"] = segs
    base["segment_kind"] = seg_kind
    base["source_chars"] = int(src.get("chars") or 0)   # 入库真源字数（免读全文）
    # 原创小说创作层（剧本工作台「创作」Tab）：进度/逐章登记态/伏笔账本/文风契约/
    # 里程碑——纯只读视图（novel.view 绝不 mutate），正文按章懒加载走 novel_chapter
    data = get_storage(ws_root).load_project(pid) or {}
    from .. import novel as _novel
    base["novel"] = _novel.view(data)
    return base


def novel_chapter(ws_root: Path, pid: str, no: int) -> dict | None:
    """按章取原创正文（懒加载，与 script_segment 同范式）：登记条目元数据 +
    manuscript/chNNNN.md 正文。返回 None = 未登记该章。单章封顶 200K 字。"""
    from .. import novel as _novel
    data = get_storage(ws_root).load_project(pid)
    if not data:
        return None
    entry = next((c for c in ((data.get("novel") or {}).get("chapters") or [])
                  if int(c.get("no") or 0) == int(no)), None)
    if entry is None:
        return None
    out = {k: entry.get(k) for k in ("no", "title", "chars", "digest", "state",
                                     "entities", "updated_at")}
    out["versions"] = len(entry.get("versions") or [])
    fp = ws_root / pid / _novel.chapter_relpath(int(no))
    text = fp.read_text(encoding="utf-8", errors="replace") if fp.is_file() else ""
    cap = 200_000
    if len(text) > cap:
        text, out["seg_truncated"] = text[:cap], True
    out["text"] = text
    return out


def script_segment(ws_root: Path, pid: str, index: int) -> dict | None:
    """按段取源正文（懒加载）：定位 segments.json 里 index 段，按 char_start:char_end
    从 raw.txt 切片。返回 None = 无索引/无此段。fdx 剧本段无字符偏移 → text 空 + note。

    单段封顶 200K 字（防异常超长章拖垮响应；正常章远达不到），触发置 seg_truncated。"""
    segfile = ws_root / pid / "source" / "segments.json"
    if not segfile.is_file():
        return None
    try:
        digest = json.loads(segfile.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  索引损坏 = 无正文可取
        return None
    seg = next((s for s in (digest.get("segments") or [])
                if s.get("index") == index), None)
    if seg is None:
        return None
    a, b = seg.get("char_start"), seg.get("char_end")
    out = {
        "index": index, "type": seg.get("type"),
        "title": seg.get("title"), "heading": seg.get("heading"),
        "int_ext": seg.get("int_ext"), "location": seg.get("location"),
        "time_of_day": seg.get("time_of_day"),
        "char_start": a, "char_end": b,
    }
    if not (isinstance(a, int) and isinstance(b, int)):
        out["text"] = ""
        out["note"] = "此源无字符偏移（如 .fdx 剧本），正文无法按段截取"
        return out
    fp = ws_root / pid / "source" / "raw.txt"
    text = fp.read_text(encoding="utf-8", errors="replace")[a:b] if fp.is_file() else ""
    out["chars"] = b - a                 # 段全跨度（与目录 script_detail 的 chars 一致）
    cap = 200_000
    if len(text) > cap:                  # 仅**输出**封顶；差额由 seg_truncated 标记，不改 chars 语义
        text, out["seg_truncated"] = text[:cap], True
    out["text"] = text
    return out


# ============================================================================
# 章节详情（制作台快照，供轮询）
# ============================================================================
def video_preview(ws_root: Path, store, pid: str, cid: str) -> dict | None:
    """逐镜「实发提示词」——真源 `gen-video --preview-json`（与 `--dry-run` 同一条
    编译路径的结构化出口）。**按需端点，不进 `chapter_detail` 的 3s 轮询**：整章
    编译非零成本，挂进轮询会把只读扫描拖重。

    走子进程而不是进程内 import：studio 域不得反向依赖 cli（层级守卫
    `TestStudioNeverImportsCli`——cli 是入口层，import 会把它拽进每个网页请求的
    依赖面），长任务经 jobs 起 CLI 子进程是既定范式，本端点同款。子进程天然隔离
    了 preview 编译期的内存改动（孤岛接缝同拓扑计算），本进程的文档缓存零污染。
    本地渲染模式（kenburns）无视频请求，无从预览。"""
    import subprocess
    import sys

    data = get_storage(ws_root).load_chapter(pid, cid)
    if data is None:
        return None
    from .jobs import _engine_dir
    uses_video = _uses_video(data)

    def _spawn(stage):
        return subprocess.Popen(
            [sys.executable, "-m", "kinema", stage,
             "--chapter", f"{pid}/{cid}", "--preview-json",
             "--workspace", str(ws_root)],
            cwd=_engine_dir(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)

    def _harvest(p):
        """→ (rows, 错误文本或 None)。载荷在 stdout 末行：更早的行是配置层加载
        告警等杂项打印，出口无从拦截。"""
        try:
            out, err = p.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            p.kill()
            return [], "预览编译超时（>180s）——先查 doctor 与章节规模"
        if p.returncode != 0:
            tail = "\n".join((err or out or "").strip().splitlines()[-5:])
            return [], tail or "预览编译失败（无输出）"
        try:
            return json.loads(out.strip().splitlines()[-1]), None
        except (ValueError, IndexError):
            return [], "预览输出解析失败——引擎版本可能不含 --preview-json"

    # 生图与生视频两路并行拉起（各自都是同真发的编译路径）；kenburns
    # 本地渲染无视频请求，只走生图那一路
    p_img = _spawn("gen-image")
    p_vid = _spawn("gen-video") if uses_video else None
    img_rows, img_err = _harvest(p_img)
    vid_rows, vid_err = _harvest(p_vid) if p_vid else ([], None)

    def _mediaize(r):
        """把子进程回传的本地路径换成页面可访问的媒体 URL（@图片N 点看/锚定音试听）。
        路径不出站：换不成 URL 就置 None，前端按不可点渲染。"""
        for ref in r.get("refs") or []:
            ref["media"] = _murl(ref.pop("path", None))
        for a in r.get("anchors") or []:
            a["media"] = _murl(a.pop("clip", None))
        d = r.get("dub")
        if d:
            d["media"] = _murl(d.pop("clip", None))
        return r

    # 字面量合并而非下标赋值：`x["image"] = …` 形态会命中 test_design_refs 对
    # 「shots[].image 写路径穷举」的 AST 守卫——这里组装的是响应体，不是章节文档
    by_id: dict[str, dict] = {}
    for r in img_rows:
        k = str(r.get("id"))
        by_id[k] = {**by_id.get(k, {}), "image": _mediaize(r)}
    for r in vid_rows:
        k = str(r.get("id"))
        by_id[k] = {**by_id.get(k, {}), "video": _mediaize(r)}
    # 按章节镜序输出；两路都缺席的镜（直供/转场/弃用）不占行
    shots = [{"id": s.get("id"), **by_id[str(s.get("id"))]}
             for s in data.get("shots") or [] if str(s.get("id")) in by_id]
    errs = "；".join(x for x in (
        f"生图预览：{img_err}" if img_err else None,
        f"视频预览：{vid_err}" if vid_err else None) if x)
    return {"shots": shots, "error": errs or None}


def _video_caps(prov) -> dict:
    """视频 provider 的参考相关能力位投影；路由不出（缺配置等）按适配器基类的缺省位取。"""
    from ..providers.base import VideoProvider
    base = VideoProvider
    return {"refs": getattr(prov or base, "supports_reference_images", False),
            "last": getattr(prov or base, "supports_last_frame", False),
            "v2v": getattr(prov or base, "supports_reference_video", False)}


def anchor_ref_task(data: dict, s: dict, *, motion: str, chain_on: bool,
                    chain_map: dict, welded: set, caps: dict) -> bool:
    """本镜是否走全能参考任务——锚定参考音只在该任务型态下随请求附发。

    组合与 `cli._shot_plan` 的仲裁同序（guide 先行 → V2V → previz 末帧 →
    衔接/锚定 → 缺省档），原子判据全部取各自的单一真源模块；链末帧的
    「下一镜图在盘」这里放宽为「下一镜有图」（页面无从逐比例验盘）。
    衔接参与镜、首帧锚定镜与 previz 镜走首帧任务，官方禁混参考媒体——
    在这些镜上标锚定，就是页面声称附了一条实发不带的参考音。"""
    sk = _sk.active_guide(s) == "sketch"
    v2v_on = bool(data.get("previz_v2v")) and motion == "native"
    if not sk and v2v_on and _pz.v2v_shot(s) and caps["v2v"]:
        return False
    lfr = s.get("last_frame_ref")
    if lfr and not sk and caps["last"] and _has_file(lfr):
        return False
    nxt, why = chain_map.get(id(s), (None, "off"))
    chained = bool(nxt and why == "" and caps["last"]
                   and (nxt.get("image") or nxt.get("images")))
    in_weld = chained or id(s) in welded
    anchor = not in_weld and anchorframe.anchored(data, s, motion)
    return bool(caps["refs"]
                and ((not chain_on and not in_weld and not anchor)
                     or _sk.reference_shot(s, True)))


def chapter_detail(ws_root: Path, store, pid: str, cid: str) -> dict | None:
    docs = get_storage(ws_root)
    data = docs.load_chapter(pid, cid)
    if data is None:
        return None
    cf = ws_root / pid / "chapters" / f"{cid}.json"
    proj = docs.load_project(pid) or {}
    work = cf.parent / f"{cf.stem}_work"
    adir = work / "audio"

    aspects = _aspects(data)
    motion = _motion(data)
    uses_video = _uses_video(data)
    needs_narr = _needs_narration_track(data)
    voices = data.get("voices") or {}
    voice_types = {k: (store.resolve_voice(v) if store else v) for k, v in voices.items()}
    # 每个说话人的音色试听：取档案库里那条不可变音频。章节文档自带档案库，
    # 所以脱离项目文档单独看章节也听得到（与配音真正用的参考音是同一条）
    voice_samples = {}
    for spk in voices:
        cast = voicebank.cast_for_ref(data, spk, voicebank.owner_ref(data, spk))
        if cast and _murl(cast.get("clip")):
            voice_samples[spk] = _murl(cast["clip"])

    # 就绪度：用章节文档构造轻量 Project 视图复用 lineage 判定
    from ..lineage import TEXT_STAGES as _text_stages
    from ..lineage import readiness as _readiness
    from ..lineage import required_refs as _required_refs
    from ..lineage import stale_text as _stale_text
    from ..project import Project as _Project
    proj_view = _Project(cf, data) if cf.is_file() else None
    # 资产视图取材：逐镜挂载走 lineage.required_refs 单一真源——绝不把章节文档里
    # **全部**有图实体下发（设定同步会把全系列几十个实体推进每个章节文档），
    # 也不许前端自己另写出场推导，两者都会与引擎真实挂载分叉。
    used_assets: dict[str, dict] = {}   # key → {key,kind,name,thumb}（全章并集·首见序）

    # 首尾帧链态：判据取 `pipeline.framechain` 单一真源，与 gen-video 实发同口径。
    # 网页只显示、绝不自算——同一条规则写两份必然分叉，而对不上的代价是用户按页面
    # 上的标记去审成片，却发现接缝根本没衔接。
    chain_on = framechain.active(data, motion)

    # 音色锚定逐镜标注（native 全能参考缺省档才有意义）：判据与真发同一个
    # `voicecast.voice_anchor_plan`——按秒计费的一步，页面说带锚定而实发没带
    # 是最贵的那种不一致。参考音位与逐镜任务型态都进判据：衔接参与镜、首帧
    # 锚定镜与 previz 镜走首帧任务（官方禁混参考媒体），页面在这些镜上不标锚定；
    # provider 无参考音位（`max_ref_audios=0`）时整章不标。scored 下人声整轨
    # 替换，锚定不参与。条数上限按 2.0 系列缺省档（MAX_ANCHOR_REFS）取。
    vprov = None
    try:
        from ..models import ModelRouter, resolve_video
        vprov = resolve_video(ModelRouter(store), store, data, data.get("profile"))[0]
    except Exception:  # noqa: BLE001  路由不出（缺配置等）按缺省能力位标注
        vprov = None
    va_on = (motion == "native" and not _scored_audio(data)
             and chapter_flag(data, "voice_anchor")
             and proj_view is not None and store is not None
             and (vprov is None
                  or int(getattr(vprov, "max_ref_audios", 0) or 0) > 0))
    _caps = _video_caps(vprov)
    # `v2v` 取**持久化的**那一半（章/项目级 `previz_v2v`）——`gen-video --previz` 是
    # 运行时覆盖，页面看不见也不该猜；持久开着的项目页面就按孤岛显示，与真发同口径。
    # provider 能力位并进总闸，与 gen-video 的孤岛判定同源
    chain_map = framechain.plan(data.get("shots", []), chain_on,
                                v2v=bool(data.get("previz_v2v")) and _caps["v2v"],
                                native=(motion == "native"))
    _welded = framechain.welded_in_ids(chain_map)
    _burn = bool(data.get("native_voiceover"))

    def _va_view(s):
        if not va_on or s.get("kind") == "transition" or not voicecast.shot_text(s):
            return None
        # 混烧下声源按镜分治（判据与真发同源 voicecast.burn_muted）：
        # 旁白/无词镜闭声出演、锚定不附发，页面不标；对白镜照常
        if _burn and voicecast.burn_muted(s):
            return None
        if not anchor_ref_task(data, s, motion=motion, chain_on=chain_on,
                               chain_map=chain_map, welded=_welded, caps=_caps):
            return None
        try:
            # 限额取真发那一档：参考位是否已满按 provider 的 max_ref_audios 判，
            # 用函数缺省值会让高限额档的第 4 个说话人在卡片上被标成「已满」
            vap = voicecast.voice_anchor_plan(
                proj_view, store, s,
                max_refs=int(getattr(vprov, "max_ref_audios", 0) or 0)
                or voicecast.MAX_ANCHOR_REFS)
        except Exception:  # noqa: BLE001  增强信息，解析不出不阻断制作台
            return None
        if not any(vap[k] for k in ("anchored", "loose", "over")):
            return None
        return {"anchored": [{"who": r["who"], "no": r["no"]}
                             for r in vap["anchored"]],
                "loose": vap["loose"], "over": vap["over"]}

    # 逐镜：图（含逐比例）/ 配音 / 生成片段（含逐比例）/ 提示词 / 状态
    shots, img_done, aud_done, aud_total, clip_done = [], 0, 0, 0, 0
    counted = 0        # 阶段进度的分母：弃用镜不参与 run/assemble 的任何产物要求
    for s in data.get("shots", []):
        active = not review.is_omitted(s)
        counted += active
        if active and s.get("kind") == "transition":
            # 转场镜「生而完成」：纯本地渲染零生成产物，进度计满不留缺口
            # ——镜号连续（1/2/3转场/4…），进度分母保持总镜数最直觉（9/9 而非 8/8）
            img_done += 1
            clip_done += 1
        images = {a: u for a, u in ((a, _murl(p)) for a, p in (s.get("images") or {}).items()) if u}
        image = _murl(s.get("image")) or next(iter(images.values()), None)
        clips = {a: u for a, u in ((a, _murl(p)) for a, p in (s.get("clips") or {}).items()) if u}
        clip = _murl(s.get("clip")) or next(iter(clips.values()), None)
        wav = adir / f"shot_{s.get('id')}.wav"
        audio = media_url(wav) if wav.is_file() else None
        if active and image:
            img_done += 1
        if active and clip:
            clip_done += 1
        # 章级要不要旁白轨与镜级要不要 wav 分别取各自的单一真源：native 混烧的
        # 对白镜由模型原生发声，把它算进分母就是一条永远做不完的待办
        if active and needs_narr and voicecast.narration_shot(s, motion):
            aud_total += 1
            if audio:
                aud_done += 1
        chain_next, chain_break = _chain_view(s, *chain_map.get(id(s), (None, "off")))
        spk = s.get("speaker")
        rv = s.get("review") or {}
        missing_refs: list = []
        ref_keys: list = []
        if proj_view is not None and not data.get("skip_design"):
            try:
                _ok, missing_refs = _readiness(proj_view, s)
            except Exception:  # noqa: BLE001  就绪度是增强信息，失败不阻断制作台
                missing_refs = []
            # 本镜真实挂载的设定图（转场镜零画面不挂）——资产视图连线的唯一数据源
            if s.get("kind") != "transition":
                try:
                    for r in _required_refs(proj_view, s):
                        ref_keys.append(r["key"])
                        if r["key"] not in used_assets:
                            used_assets[r["key"]] = {
                                "key": r["key"], "kind": r["kind"],
                                "name": r["name"], "thumb": _murl(r.get("path"))}
                except Exception:  # noqa: BLE001  同上，增强信息不阻断
                    ref_keys = []
        shots.append({
            "id": s.get("id"), "speaker": spk,
            # 转场镜：时间线显示为字卡卡片，可插拔
            "kind": s.get("kind"),
            "transition": s.get("transition"),
            # 宫格候选：待选图列表 + 已点选编号
            "image_candidates": [{"no": k + 1, "url": u} for k, u in
                                 enumerate(_murl(p) for p in (s.get("image_candidates") or []))
                                 if u],
            "image_picked": s.get("image_picked"),
            # 血缘：过期引用 + 缺失设定图 + 本镜真实挂载（required_refs 单一真源 key 列表）
            "stale_refs": s.get("stale_refs"),
            # 音色血缘：这一版配音出自哪把已经换掉的声音（只挂在已通过审阅的镜上——
            # 未锁定的直接置了重做，走审阅状态机那条路显示）
            "voice_stale": s.get("voice_stale"),
            # 片段侧同一条边：native 对白镜的人声由模型念出，过期留痕挂在片段上。
            # 未锁定的镜同样带这个标记——clip 的 retake 不带 note，归属只认这个字段
            "voice_clip_stale": s.get("voice_clip_stale"),
            # 台词血缘：这一版配音/片段出自哪个阶段的旧台词。**就地重算**——
            # 判据只是两个短字符串的哈希、不碰磁盘（`stale_refs` 那条要读设定图
            # 文件才落盘存结果）。存一份反而只有 `lineage mark` 会写它，
            # 在 Studio 里改完台词直到有人跑一次 CLI 才看得见
            "stale_text": [st for st in _text_stages if _stale_text(s, st)] or None,
            "missing_refs": missing_refs,
            "design_refs": ref_keys,
            # 角色跨镜一致性判定：**原样透传，scanner 内绝不算相似度**——
            # 那是 ffmpeg 级重计算，会把只读扫描拖成分钟级；判定由 consistency 命令回填
            "consistency": s.get("consistency"),
            "voice": s.get("voice") or (voices.get(spk) if spk else None),
            # 音色锚定预告：本镜哪些说话人的锚定音会随 gen-video 请求附发、
            # 哪些还没选角（None=本镜不适用：非 native/无台词/转场/已关锚定）
            "voice_anchor": _va_view(s),
            "framing": s.get("framing"), "camera": s.get("camera"),
            "dur": s.get("dur"), "status": s.get("status"),
            # 审阅状态机 + 版本栈
            "omitted": (rv.get("shot") or {}).get("state") == "omt",
            "review": {k: (v or {}).get("state") for k, v in rv.items()
                       if k != "shot" and (v or {}).get("state")},
            "review_notes": {k: (v or {}).get("note") for k, v in rv.items()
                             if (v or {}).get("note")},
            "versions": {k: len(v) for k, v in (s.get("versions") or {}).items() if v},
            # 版本历史面板：归档条目（媒体转 URL，含参数快照与归档原因）
            "version_history": {
                stage: [{"v": e.get("v"), "reason": e.get("reason"), "at": e.get("at"),
                         "params": e.get("params"),
                         "files": {k: u for k, u in
                                   ((k, _murl(p)) for k, p in (e.get("files") or {}).items())
                                   if u}}
                        for e in entries]
                for stage, entries in (s.get("versions") or {}).items() if entries},
            "gen": s.get("gen") or {},
            # 锚定评论（像素锚 x/y ∈ 0~1，时间锚 t 秒）
            "comments": s.get("comments") or [],
            "narration": s.get("narration"), "caption": s.get("caption"),
            # 镜内多段台词：下发**归一化后**的句序列（继承已铺平、空段已丢弃），
            # 分镜卡按句展示「谁说了什么」。单段镜恒为 None，前端零分叉
            "lines": (voicecast.shot_lines(s)
                      if isinstance(s.get("lines"), list) and s["lines"] else None),
            "image_prompt": s.get("image_prompt"), "video_prompt": s.get("video_prompt"),
            # 双语提示词（中文主/英文辅）+ 负面约束 + 工业分镜字段
            "image_prompt_en": s.get("image_prompt_en"),
            "video_prompt_en": s.get("video_prompt_en"),
            "negative_prompt": s.get("negative_prompt"),
            "angle": s.get("angle"), "lens": s.get("lens"),
            "lighting": s.get("lighting"), "sfx": s.get("sfx"),
            # 逐镜表现力：情绪/强度/语音指令 + 配音表现力契约（重读/停顿/表演提示）——
            # 镜头表专业视图直读。delivery 必须一并下发，否则专业视图形成新的展示漂移
            "emotion": s.get("emotion"), "emotion_scale": s.get("emotion_scale"),
            "voice_instruction": s.get("voice_instruction"),
            "delivery": s.get("delivery") or None,
            # 结构化叙事元数据（人填·引擎不消费，仅展示与分镜单 lint）
            "shot_intent": s.get("shot_intent"),
            "narrative_role": s.get("narrative_role"),
            "hero_moment": s.get("hero_moment"),
            "transition": s.get("transition"),
            "rank": s.get("rank"), "title": s.get("title"),
            "attribution": s.get("attribution"),
            "characters": s.get("characters"), "props": s.get("props"),
            "scenes": s.get("scenes"),      # 镜级显式取景地（None=只靠文本命中）
            "refs": s.get("refs"),          # 镜级参考库覆盖（None=跟随默认 / []=不用垫图 / [路径…]=精确）
            # 3D 预演挂载：参考片/末帧转 URL 供网页预览；`camera_preset` 让
            # 控制台把这一镜回读重开进 3D 场景。**previz 与 clip 是两个字段**，
            # 前端也绝不许把 previz 当成片播（分镜卡的成片位只认 clip）
            "previz": _murl(s.get("previz")),
            "previz_seconds": (s.get("gen") or {}).get("previz", {}).get("duration"),
            "last_frame_ref": _murl(s.get("last_frame_ref")),
            "camera_preset": s.get("camera_preset"),
            # 首尾帧链态（衔接到哪一镜 / 为什么断）——本镜末帧槽的去向，与
            # `last_frame_ref` 争同一个槽位，网页据此如实描述而不是自己推导
            "chain_next": chain_next, "chain_break": chain_break,
            # 简笔分镜板：board 转 URL 供灯箱预览；`guide_active` 走
            # `sketchboard.active_guide` **单一真源**——前端只消费判定绝不自算仲裁
            # （guide/previz/板三态的组合逻辑各写一份必然分叉）。
            # beats 计数取 `effective_beats`（authored 优先、缺省句读自动拆拍），
            # `auto` 标注拍序列来源——选镜弹层据此显示「9 拍 / 自动拆拍 / 缺运动设计」
            "sketch": _sketch_view(s),
            "guide": s.get("guide"),
            "guide_active": _sk.active_guide(s),
            "image": image, "images": images,
            "audio": audio, "clip": clip, "clips": clips,
        })
    n = counted

    # 阶段进度：脚本 → 分镜图 → 配音 → 动态片段(仅 b/c 模式) → 成片
    outputs = _outputs(work)
    stages = {
        "script": bool(shots),
        "image": img_done, "image_total": n,
        "audio": aud_done, "audio_total": aud_total,
        "clips": clip_done, "clips_total": (n if uses_video else 0),
        "video": bool(outputs),
    }

    # 章节级资产：BGM / 全片配音轨（旁白+角色对白整轨） / 字幕(各比例) / 时间戳
    bgm = next((p for p in (adir / "bgm.mp3", adir / "bgm.wav") if p.is_file()), None)
    narration = adir / "narration.wav"
    ascript = _audio_script_view(proj_view, store, data, adir)   # 内含分段计算，只算一次
    subs = []
    subdir = work / "subs"
    for ass in (sorted(subdir.glob("*.ass")) if subdir.is_dir() else []):
        m = re.search(r"_(\d+x\d+)$", ass.stem)
        subs.append({"aspect": _aspect_from_tag(m.group(1)) if m else None,
                     "name": ass.name, "url": media_url(ass)})
    ts = adir / "timestamps.json"

    style = data.get("style") or {}
    return {
        "project": pid, "project_title": proj.get("title") or pid,
        # 所属项目软删态：章节制作台据此置只读横幅并灰化变更类操作
        "project_deleted": int(proj.get("is_deleted") or 0),
        "project_deleted_at": proj.get("deleted_at"),
        "id": cid, "title": _chapter_title({"id": cid}, data),   # 与列表页同一个解析口
        "theme": data.get("theme"), "profile": data.get("profile"),
        "platform": data.get("platform") or [],
        "aspect": data.get("aspect"), "aspects": aspects,
        "motion": motion, "uses_video": uses_video, "frame_chain": chain_on,
        "native_voiceover": _burn,
        "video_provider": {"alias": data.get("video_provider"),
                           "provider": getattr(vprov, "name", None),
                           "model": getattr(vprov, "model", None)},
        "image_per_aspect": bool(data.get("image_per_aspect")),
        "scene": data.get("scene") or (style.get("scene") or ""),
        "scene_ref": _murl(data.get("scene_ref")),
        "voices": voices, "voice_types": voice_types, "voice_samples": voice_samples,
        "characters": [_character_view(c, store)
                       for c in (data.get("characters") or proj.get("characters", []))],
        "props": [_prop_view(p) for p in (data.get("props") or [])],
        "scenes": [_scene_view(x)
                   for x in (data.get("scenes") or proj.get("scenes") or [])],
        "style": {"character_block": style.get("character_block"),
                  "palette": style.get("palette"), "seed": style.get("seed")},
        "script": data.get("script") or {},
        # 特效两态：生效(章节点名解析后·展示用) / 章节原始覆盖
        "effects": _effects_resolved(store, data.get("profile"), data.get("effects")),
        "effects_override": data.get("effects"),
        "comments": data.get("comments") or [],   # 章节级（成片）时间锚评论
        # 3D 导演控制台的场景编排快照（可重开继续排戏）+ V2V 开关态。
        # **原样透传**：场景是前端自己的数据结构，scanner 不解释、不校验、不改写
        "previz": data.get("previz") or None,
        "previz_v2v": bool(data.get("previz_v2v")),
        # 全片预演（各镜 previz 拼成的一条长片）：**由磁盘 sidecar 推导，不进契约**
        # ——顶层 `previz` 是编排快照的整体替换区，指针写进去下次保存编排就没了
        "previz_reel": _previz_reel_view(work),
        # 简笔分镜台区块统计：beats 就绪数 / 已出板数 / 正镜总数
        "sketch_stats": _sketch_stats(data.get("shots") or []),
        # 音频剧本台：分段表（按转场切·真源 audioscript.plan）+ 逐段剧本与音轨状态
        "audio_script": ascript,
        "shots": shots,
        "stages": stages,
        "outputs": outputs,
        # 成片版本谱系（逐比例各一支）：归档条目转 URL 供逐版回看与回滚。
        # 与分镜 `version_history` 同形，前端复用同一套面板，不为成片另造一份
        "output_versions": {
            asp: [{"v": e.get("v"), "reason": e.get("reason"), "at": e.get("at"),
                   "file": _murl(e.get("file"))}
                  for e in (entries or []) if _murl(e.get("file"))]
            for asp, entries in (data.get("output_versions") or {}).items() if entries},
        "watermark": _watermark_view(data, bool(outputs)),   # 防搬运动态水印（成片后）
        # 字幕样式（放映区面板）：生效值与章节覆盖分开下发，判据与烧录同源 sub_cfg
        "subtitle_style": _subtitle_style_view(store, data),
        # 成片自审报告（verify 命令写入）：原样透传——**不在 scanner 内跑 ffmpeg 探测**，
        # 那是逐帧/整片级重计算，只在显式点「自审」时由后台任务跑
        "verify": data.get("verify") or None,
        # 草稿两段式：全片 Ken Burns 样片 + 章节级节奏审状态
        "animatic": ({
            "files": {a: u for a, u in
                      ((a, _murl(p)) for a, p in
                       ((data.get("animatic") or {}).get("files") or {}).items()) if u},
            "at": (data.get("animatic") or {}).get("at"),
            "state": ((data.get("review") or {}).get("animatic") or {}).get("state", "wfa"),
            "note": ((data.get("review") or {}).get("animatic") or {}).get("note"),
        } if data.get("animatic") else None),
        "duration": _shots_duration(data),
        # 血缘画布：本章继承的设定资产（场景/角色/道具设定图），前端据此连线到分镜
        # 只下发**本章分镜真实挂载**的资产（used_assets=逐镜 required_refs 并集），
        # 且只展示已有设定图的（无图实体连不出线也点不开灯箱）；按 kind 归组排序
        "design_assets": sorted(
            [a for a in used_assets.values() if a.get("thumb")],
            key=lambda a: ({"scene": 0, "character": 1, "prop": 2}.get(a["kind"], 3),)),
        "assets": {
            "bgm": media_url(bgm) if bgm else None,
            "narration": media_url(narration) if narration.is_file() else None,
            # 音频剧本整轨（scored 专有）：与 narration.wav 并列摆进章节资产架，
            # 两者互斥消费但可同时在盘（切过 audio_mode 的章节）
            "score": ascript["score"],
            "timestamps": media_url(ts) if ts.is_file() else None,
            "subtitles": subs,
        },
        "cost": data.get("cost"),
        # 台账合计服务端算好下发（budget.spent_total 单一真源），前端只格式化；
        # 没记过账下发 None——「¥0.00」与「未入账」是两种事实
        "cost_total": spent_total(data) if data.get("cost") else None,
        "updated_at": cf.stat().st_mtime,
    }


def _media_info(ws_root) -> dict:
    """媒体后端信息（local / oss·provider），供大屏页脚展示。"""
    try:
        from ..storage.media import get_media_store
        ms = get_media_store(ws_root)
        return {"backend": ms.backend, "detail": ms.describe()}
    except Exception:  # noqa: BLE001
        return {"backend": "local", "detail": ""}


# ============================================================================
# 审阅队列：跨项目聚合全部「待审」产物，供两键表态连审
# ============================================================================
def recycle_bin(ws_root: Path) -> list[dict]:
    """回收站：已逻辑删除的项目轻量清单（Studio 恢复入口）。"""
    if not ws_root:
        return []
    return [{"id": p.get("id"), "title": p.get("title") or p.get("id"),
             "deleted_at": p.get("deleted_at"),
             "chapters": len(p.get("chapters") or []),
             "profile": p.get("profile")}
            for p in get_storage(ws_root).list_projects() if not _alive(p)]


def review_queue(ws_root: Path) -> list[dict]:
    docs = get_storage(ws_root)
    items = []
    for p in docs.list_projects():
        if not _alive(p):           # 软删过滤
            continue
        pid = p.get("id")
        for ch in p.get("chapters", []):
            cid = ch.get("id")
            data = docs.load_chapter(pid, cid) or {}
            adir = ws_root / pid / "chapters" / f"{cid}_work" / "audio"
            for s in data.get("shots") or []:
                if s.get("kind") == "transition":   # 转场镜零产物，不进待审队列
                    continue
                rv = s.get("review") or {}
                if (rv.get("shot") or {}).get("state") == "omt":
                    continue
                for stage in ("image", "audio", "clip"):
                    ent = rv.get(stage) or {}
                    if ent.get("state") != "wfa":
                        continue
                    cands = []
                    if stage == "image":
                        media, kind = _murl(s.get("image")), "image"
                        prompt = s.get("image_prompt")
                        if not media and s.get("image_candidates"):
                            # 候选待选 → 队列直接给宫格点选
                            kind = "candidates"
                            cands = [{"no": k + 1, "url": u} for k, u in
                                     enumerate(_murl(p) for p in s["image_candidates"]) if u]
                    elif stage == "clip":
                        media, kind = _murl(s.get("clip")), "video"
                        prompt = s.get("video_prompt")
                    else:
                        wav = adir / f"shot_{s.get('id')}.wav"
                        media = media_url(wav) if wav.is_file() else None
                        kind, prompt = "audio", None
                    items.append({
                        "project": pid, "project_title": p.get("title") or pid,
                        "chapter": cid, "chapter_title": _chapter_title(ch, data),
                        "shot": s.get("id"), "stage": stage, "kind": kind,
                        "media": media, "candidates": cands,
                        "narration": s.get("narration"),
                        "speaker": s.get("speaker"), "prompt": prompt,
                        "dur": s.get("dur"),
                        "version": len((s.get("versions") or {}).get(stage) or []) + 1,
                        "at": ent.get("at"),
                    })
    items.sort(key=lambda x: x.get("at") or "", reverse=True)
    return items


# ============================================================================
# 素材检索：按台词/字幕/提示词/角色/标题跨项目定位历史素材
# ============================================================================
# 纯文本包含匹配（大小写不敏感）+ 字段加权排序。本地库是千镜量级，线性扫描
# 毫秒级返回——不值得为此引入索引或向量依赖；语义升级留给未来的嵌入方案。
_SEARCH_FIELDS = [   # (镜字段, 中文名, 权重)
    ("narration", "台词", 5), ("caption", "字幕", 4), ("speaker", "角色", 3),
    ("image_prompt", "画面提示词", 2), ("video_prompt", "运动提示词", 2),
]


def _snippet(text: str, pos: int, qlen: int, radius: int = 28) -> dict:
    lo = max(0, pos - radius)
    hi = min(len(text), pos + qlen + radius)
    return {"pre": ("…" if lo else "") + text[lo:pos],
            "hit": text[pos:pos + qlen],
            "post": text[pos + qlen:hi] + ("…" if hi < len(text) else "")}


def search(ws_root: Path, q: str, limit: int = 40) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 1:
        return []
    ql = q.lower()
    docs = get_storage(ws_root)
    hits = []
    for p in docs.list_projects():
        if not _alive(p):           # 软删过滤
            continue
        pid, ptitle = p.get("id"), p.get("title") or p.get("id")
        for text, field in ((p.get("title"), "项目名"), (p.get("theme"), "主题")):
            pos = (text or "").lower().find(ql)
            if pos >= 0:
                hits.append({"type": "project", "project": pid, "project_title": ptitle,
                             "field": field, "snippet": _snippet(text, pos, len(q)),
                             "href": f"#/project/{pid}", "score": 6})
        for c in p.get("characters") or []:
            pos = (c.get("name") or "").lower().find(ql)
            if pos >= 0:
                hits.append({"type": "character", "project": pid, "project_title": ptitle,
                             "field": "角色", "snippet": _snippet(c["name"], pos, len(q)),
                             "thumb": _murl(c.get("ref_image")),
                             "href": f"#/project/{pid}", "score": 6})
        for ch in p.get("chapters", []):
            cid = ch.get("id")
            data = docs.load_chapter(pid, cid) or {}
            # 标题走唯一解析口（文档赢）：按登记表搜的话，改名后的章按当前名
            # 查不到、按界面上已看不到的旧名才查得到
            ctitle = _chapter_title(ch, data)
            pos = (ctitle or "").lower().find(ql)
            if pos >= 0:
                hits.append({"type": "chapter", "project": pid, "project_title": ptitle,
                             "chapter": cid, "chapter_title": ctitle, "field": "章节名",
                             "snippet": _snippet(ctitle, pos, len(q)),
                             "href": f"#/project/{pid}/{cid}", "score": 5})
            for s in data.get("shots") or []:
                best = None
                for fld, zh, w in _SEARCH_FIELDS:
                    text = s.get(fld) or ""
                    pos = text.lower().find(ql)
                    if pos >= 0 and (best is None or w > best[2]):
                        best = (text, pos, w, zh)
                if best:
                    text, pos, w, zh = best
                    hits.append({
                        "type": "shot", "project": pid, "project_title": ptitle,
                        "chapter": cid, "chapter_title": ctitle,
                        "shot": s.get("id"), "field": zh,
                        "snippet": _snippet(text, pos, len(q)),
                        "thumb": _murl(s.get("image")), "dur": s.get("dur"),
                        "href": f"#/project/{pid}/{cid}", "score": w,
                    })
    hits.sort(key=lambda x: -x["score"])
    return hits[:limit]


# ============================================================================
# 成本页：跨项目成本台账（路由 #/cost；台账数学在引擎域 business.py）
# ============================================================================
def cost(ws_root: Path) -> dict:
    from ..business import project_ledger
    from ..workspace import Workspace
    ws = Workspace.open(str(ws_root), create=False)
    projects = []
    for p in ws.list_projects():
        try:
            projects.append(project_ledger(ws, p.get("id")))
        except Exception:  # noqa: BLE001  单个坏项目不拖垮整页
            continue
    return {"projects": projects}


# ============================================================================
# 看板：跨项目 分镜×产物 状态全景 + 烧钱/废片/重roll 运营统计
# ============================================================================
def board(ws_root: Path) -> dict:
    """items: 每行一个「分镜×产物」卡（看板列按状态分桶）；
    chapters: 每章一行的运营统计（成本 / 废片成本 / 重roll 次数 / 状态汇总）。
    废片成本 = 版本栈归档条目 params.cost 之和（每一版都记了自己的钱，
    被换掉的版本就是沉没成本——AI 生产线独有的运营指标）。"""
    from ..business import chapter_ledger
    docs = get_storage(ws_root)
    items, chapters = [], []
    for p in docs.list_projects():
        if not _alive(p):           # 软删过滤
            continue
        pid = p.get("id")
        for ch in p.get("chapters", []):
            cid = ch.get("id")
            data = docs.load_chapter(pid, cid) or {}
            motion = _motion(data)
            uses_video = _uses_video(data)
            needs_narr = _needs_narration_track(data)
            state_sum: dict = {}
            for s in data.get("shots") or []:
                if s.get("kind") == "transition":   # 转场镜零产物，不进看板
                    continue
                rv = s.get("review") or {}
                if (rv.get("shot") or {}).get("state") == "omt":
                    continue
                vers = s.get("versions") or {}
                for stage in ("image", "audio", "clip"):
                    if stage == "audio" and not (
                            needs_narr and voicecast.narration_shot(s, motion)):
                        continue
                    if stage == "clip" and not uses_video:
                        continue
                    st = (rv.get(stage) or {}).get("state", "todo")
                    state_sum[st] = state_sum.get(st, 0) + 1
                    n_arch = len(vers.get(stage) or [])
                    items.append({
                        "project": pid, "project_title": p.get("title") or pid,
                        "chapter": cid, "chapter_title": _chapter_title(ch, data),
                        "shot": s.get("id"), "stage": stage, "state": st,
                        "thumb": _murl(s.get("image")),
                        "versions": n_arch + 1,
                        "at": (rv.get(stage) or {}).get("at"),
                    })
            # 运营数字（镜数/成本合计/废片/重roll）整块取台账真源 business.chapter_ledger。
            # 口径要点：**卡片是工作项、账是钱，两者刻意不同**——台账把弃用/转场镜
            # 与已隐藏 stage（如改回 kenburns 后的 clip）的版本栈沉没成本照记（钱不随
            # 看板过滤翻篇），故 waste/rerolls 可能大于看板卡片上可见的版本数；
            # states/items 只数当前活跃产物。
            led = chapter_ledger(data)
            chapters.append({
                "project": pid, "project_title": p.get("title") or pid,
                "chapter": cid, "title": _chapter_title(ch, data),
                "motion": motion,
                "shots": led["shots"],
                # 没记过账下发 None；currency 归前端格式化
                "cost_total": led["actual_total"] if data.get("cost") else None,
                "currency": (data.get("cost") or {}).get("currency"),
                "waste": led["waste"],
                "rerolls": led["rerolls"], "states": state_sum,
            })
    return {"items": items, "chapters": chapters}


# ============================================================================
# 总览（全局统计 + profile 清单 + 最近成片）
# ============================================================================
def overview(root: Path, ws_root: Path, store) -> dict:
    projects = workspace_summary(ws_root)
    videos = library(root)

    chapters = sum(len(p["chapters"]) for p in projects)
    rendered = sum(p["rendered"] for p in projects)
    shots = sum(p["shots"] for p in projects)
    duration = round(sum(v.get("duration") or 0 for v in videos), 1)
    docs = get_storage(ws_root) if ws_root else None
    cost: dict = {}
    if docs:
        for p in projects:
            _merge_cost(cost, (docs.load_project(p["id"]) or {}).get("cost"))   # 系列级支出
            for ch in p.get("chapters", []):
                cdata = docs.load_chapter(p["id"], ch["id"]) or {}
                _merge_cost(cost, cdata.get("cost"))

    from .. import skills as _skills
    profiles = []
    for name, p in ((store.data.get("profiles") or {}) if store else {}).items():
        profiles.append({
            "name": name,
            "skill": _skills.skill_for_profile(name),   # 归属指挥层 skill（建项目分组/绑定用）
            "label": p.get("label") or name,   # 中文名由 models.yaml 下发
            "image": (p.get("image") or {}).get("provider"),
            "video": (p.get("video") or {}).get("provider"),
            "tts": (p.get("tts") or {}).get("provider"),
            "music": (p.get("music") or {}).get("provider"),
            "effects": p.get("effects") or [],
            "subtitle_mode": (p.get("subtitle") or {}).get("mode", "caption"),
            "style_prefix": ((p.get("image") or {}).get("style_prefix") or "")[:150],
            # 画风详情弹窗用全量字段：双语前缀不截断 + 节奏备注 + 默认音色
            "style_prefix_full": (p.get("image") or {}).get("style_prefix") or "",
            "style_prefix_en": (p.get("image") or {}).get("style_prefix_en") or "",
            "pacing": (p.get("pacing") or {}).get("shots"),
            "pacing_note": (p.get("pacing") or {}).get("note"),
            "voice": (p.get("tts") or {}).get("voice"),
            "music_mood": (p.get("music") or {}).get("mood"),
        })

    return {
        "stats": {"projects": len(projects), "chapters": chapters, "rendered": rendered,
                  "videos": len(videos), "shots": shots, "duration": duration,
                  # 各币种合计：跨章跨币种的桶形态没有单文档 cost 键，套不进
                  # budget.spent_total，数值口径由 _merge_cost 对齐；分项桶不下发
                  "cost_totals": {cur: round(sum(bucket.values()), 4)
                                  for cur, bucket in cost.items()},
                  "pending": len(review_queue(ws_root)) if ws_root else 0},
        "storage": {"backend": docs.backend if docs else "local",
                    "detail": docs.describe() if docs else "",
                    "media": _media_info(ws_root)},
        "profiles": profiles,
        # 配置源健康块：fallback 非空=内置精简配置在服务（缺 PyYAML / 无 models.yaml），
        # 画风目录是缩水子集——前端新建项目弹层靠它渲染告警条，否则「画风悄悄变少」
        # 只在引擎 stdout 留一行 ⚠，网页用户看不见。
        # overlay 是**第三个独立字段**：source/fallback 说的是「内置精简配置在服务」，
        # overlay 说的是「用户配过的连接段与激活项在生效」，混成一个会让那条告警条
        # 报错状态（它已被 doctor、新建项目弹层与本页三处消费）
        "config": {"source": getattr(store, "source", None) if store else None,
                   "fallback": getattr(store, "fallback", None) if store else None,
                   "overlay": getattr(store, "overlay", None) if store else None},
        "skills": _skills.skill_catalog(),   # 全量 skill 目录（建项目分组 + 绑定展示真源）
        "skill_board": _skills.skill_board(),   # 指挥层全集群（#/skill 只读大屏真源）
        "effects_catalog": fx.catalog(),   # 全量特效目录（选择器/展示中文名+类别的真源）
        "transitions_catalog": _tr.catalog(),      # 全量转场目录（类型选择器真源：方向/主色/描述）
        "transition_sounds": _tr.sound_catalog(),  # 合法转场音效（值+中文名）
        # 3D 导演控制台的两份目录：运镜库（同时是 shots[].camera 的措辞真源）与
        # 舞台资产库（灰模角色/动作/道具体块）。前端零硬编码，全部读这里
        "camera_catalog": _cam.catalog(),
        "director_catalog": _pz.director_catalog(),
        "canvas": (store.data.get("canvas") if store else None) or {},
        "default_profile": store.default_profile if store else "narration",
        "recent": videos[:8],
        "projects": projects,
        "recycle": recycle_bin(ws_root),   # 回收站轻量清单（恢复入口）
    }
