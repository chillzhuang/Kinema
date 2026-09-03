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

"""成片合成：分镜帧 → Ken Burns 片段 → 拼接 → 特效 → 混音 → 烧字幕 → 输出（按指定比例）。

每次 build 针对一个比例（aspect）产出一支成片：画布、字幕、Ken Burns 取景、特效尺寸都随
该比例计算。上层 stage_compose 会对 project.aspects 里的每个比例各调一次（竖屏/横屏/两者都要）。
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import effects as fx
from .. import parallel
from .. import voicecast
from ..errors import ProjectError
from ..ffmpeg import concat_audio, concat_entry, filter_literal, probe_duration, probe_json, run
from ..project import aspect_tag
from ..storage.media import ensure_local
from . import asr
from . import kenburns
from . import mixdown
from . import prompts as prompts_mod
from . import subtitle as subtitle_mod
from . import transitions as tr_mod
from .checkpoint import has_file


def speech_spans_resolver(project, aspect=None):
    """按镜给出片段音轨里的有声段落，供字幕落点使用；不适用时返回 None。

    仅 dubbed/native 适用；探测源与成片主音轨同源——native 的人声在片段音轨里，
    dubbed 的主音轨是逐镜 TTS（片段里模型重演的人声不进成片，对它探测会把字幕
    对到一条观众听不到的轨上）。kenburns 的 `dur` 是配音实测加生效停顿，人声
    段落由停顿声明直接算出，不探测音轨。

    scored 同样不适用：整章人声由音频剧本一条轨承担，逐镜片段的音轨不进成片，
    量它等于拿一条观众听不到的时间轴给字幕定位（判据与 `cli._anchor_plan_for`
    对 scored 的处置同源）。

    只探有台词的镜，结果在本次渲染内按镜缓存：同一份时间轴要为每个比例各渲一遍字幕。
    """
    from . import speech
    if project.scored_audio:
        return None
    if project.motion == "kenburns":
        # 本地渲染的窗口 = 配音实测 + 生效停顿：字幕要落在人声那一段，
        # 而不是从停顿的第 0 秒就亮起。判据取 shot_pauses 单点，不探测音轨
        def _pauses(shot):
            pb, pa = voicecast.shot_pauses(shot, "kenburns")
            dur = float(shot.get("dur") or 0)
            if not (pb or pa) or dur <= 0:
                return None
            return [(round(pb, 3), round(max(dur - pa, pb), 3))]
        return _pauses
    if project.motion not in ("dubbed", "native"):
        return None
    cache: dict = {}

    def _spans(shot):
        sid = shot.get("id")
        if sid in cache:
            return cache[sid]
        lines = voicecast.shot_lines(shot)
        # 落点测量对象跟着该镜的声源走：烧录承担的镜（kenburns/dubbed 全部、
        # native 混烧的旁白镜）量 TTS wav，模型原生发声的镜量底片自带音轨——
        # 量错一侧就是拿另一条时间轴给字幕定位
        burned = (not project.native_audio
                  or (project.native_voiceover
                      and voicecast.voice_kind(shot) == "voiceover"))
        if burned:
            clip = str(project.workdir / "audio" / f"shot_{sid}.wav")
        else:
            clip = (shot.get("clips") or {}).get(aspect) if aspect else None
            clip = clip or shot.get("clip")
        spans = None
        if lines and clip and has_file(clip):
            local = ensure_local(clip)
            if local and Path(local).is_file():
                # 探测窗口取媒体自身长度：烧录侧的 wav 常短于表演窗口 dur，按 dur 探会把
                # EOF 到窗口尾的空白判成有声，字幕铺到镜尾
                found = speech.speech_windows(local, probe_duration(local), clean=burned)
                # 逐句落点只认语义划界：原生声源的多句镜一律请本地 ASR 按句文本
                # 重新划界。振幅段数恰好等于句数不构成对位依据——句中换气会把一句
                # 切成两段、低音量的句子整句检不出，两者叠加就凑出「段数碰巧相等」
                # 而每段都落在同一句里。划不了（未装 faster-whisper / 转写与稿子
                # 对不上）就收成整体首尾：字幕宁可不换人也要覆盖完整，换在错的
                # 地方比不换更糟。单句镜同样收，否则停顿后的半句会留在窗口外
                if found:
                    aligned = (asr.line_windows(local, lines,
                                                float(shot.get("dur") or 0))
                               if not burned and len(lines) >= 2 else None)
                    found = aligned or [(found[0][0], found[-1][1])]
                spans = found or None
        # dubbed 烧录侧把配音平移到底片开口时点（`dubbed_sync_offset` 单一真源），
        # 字幕随同一偏移走——不随行就是"声音贴了嘴、字幕留在原位"
        if spans and not project.native_audio:
            win = float(shot.get("dur") or 0)
            sync = voicecast.dubbed_sync_offset(shot, clip, shot.get("clip"), win)
            if sync:
                spans = [(round(max(a + sync, 0.0), 3), round(min(b + sync, win), 3))
                         for a, b in spans]
        cache[sid] = spans
        return spans

    return _spans


def _sync_narration(project, narration: Path) -> Path | None:
    """旁白自愈：拼接旁白与有效时间轴出现明显偏差（tts 之后弃用镜/改动分镜
    而未重跑 tts）时，用现有逐镜 wav 按当前有效镜序零成本重拼——否则被弃镜的
    音频会让后续所有镜的音画字整体错位，且被末尾裁切掩盖成"话没说完"。

    拼接序列**必须**取自 `voicecast.narration_parts`（与 tts 同一条单一真源）：
    这里一旦另写一份「无旁白镜插静音」的简化逻辑，就会把 tts 按 `delivery.pause_*`
    插进去的停顿垫片整段抹掉，还打印「已按有效分镜自动重拼」把破坏伪装成修复。

    逐镜 wav 缺失（从未合成过）时无从自愈，保留原文件并明确警告；
    重拼后复核，仍偏差说明逐镜时长与 dur 不符（改 dur 未重跑 tts），如实警告。
    返回 None＝这条轨不该烧进本次成片（调用方据此不上主轨）。"""
    expected = project.total_duration()
    actual = probe_duration(narration)
    drift = abs(actual - expected) > 0.3
    # 图生视频的两档即便零漂移也要重算拼接序列：它们的窗口长度由片段说了算、
    # 与逐镜 wav 可分离，于是盘上那条轨可以与时间轴等长而内容早已不符——
    # dubbed 的对白开口对齐取自底片声轨（tts 拼轨时底片往往还没生成），
    # native 的对白镜整段让位静音（人声由模型承担）而旧轨里可能烧着对白 TTS。
    # 拼接确定性且秒级，恒重拼把陈旧态整类消灭
    if not drift and not project.uses_seedance:
        return narration
    adir = project.workdir / "audio"
    parts, segments, missing = voicecast.narration_parts(project, adir)
    if missing:
        # 缺配音就无从按当前分镜重拼，只能原样保留盘上那条轨——但必须说出口。
        # 混烧章里这条是可达且不带时长偏差的（旁白镜漏跑 tts 时窗口由静音占满），
        # 按偏差门控告警就成了静默烧一条有洞的轨
        ids = "/".join(str(i) for i in missing)
        drift_note = (f"（与时间轴偏差 {abs(actual - expected):.2f}s）" if drift else "")
        print(f"  ⚠ 镜 {ids} 有台词却缺逐镜配音{drift_note}，旁白轨无法重拼——"
              f"先补跑 tts --only {ids}")
        return narration
    if not segments:
        # `narration_parts` 恰在真消费了一条逐镜 wav 时落一条 segment，它是
        # 「这条轨里有没有本章的人声」的产生端见证（按段类型枚举会漏掉压窗的
        # fit 段）。一条都没有 ⇒ 盘上那条轨与当前分镜无关，烧它就是烧陈旧人声
        print("  ⚠ 旁白轨里没有本章任何一条逐镜配音——盘上的 narration.wav "
              "与当前分镜无关，本次不烧录")
        return None
    concat_audio(parts, narration)
    synced = [s for s in segments if s.get("sync")]
    rebuilt = probe_duration(narration)
    if abs(rebuilt - expected) > 0.3:
        print(f"  ⚠ 旁白重拼后仍与时间轴偏差 {abs(rebuilt - expected):.2f}s——"
              f"逐镜配音时长与分镜 dur 不符（改过 dur？），请重跑 tts 校正")
    elif drift:
        print(f"  ⚠ 旁白与时间轴偏差 {abs(actual - expected):.2f}s"
              f"（弃用/改动分镜后未重跑 tts）——已按有效分镜自动重拼旁白")
    if synced:
        moves = "、".join(f"镜{s['shot_id']} {s['sync']:+.2f}s" for s in synced)
        print(f"  ⚙ 对白镜配音已平移至底片开口时点：{moves}（字幕随行）")
    flagged = [s for s in segments if s.get("sync_note")]
    if flagged:
        for s in flagged:
            print(f"  ⚠ 口型残差点名：镜{s['shot_id']} {s['sync_note']}")
        print("     → 配置 lipsync 精修（docs/agents/lipsync.md）· retake 该镜 · "
              "或加长该镜时长后重生")
    return narration


# native 片段音频的边缘平滑秒数：一镜一片各自带环境音，硬切处环境床是硬台阶，
# 头尾各淡这一段把台阶抹平（只动音频不动画面；fit_clip 只在 keep_audio 时消费）
NATIVE_AUDIO_EDGE = 0.15


def _gate_native_double_voice(project) -> None:
    """native 混烧前拦下「烧录承担的镜不是按不出声的稿子生成的」片段——TTS 旁白
    叠上模型自配的同一段就是双人声，不存在可交付的形态，故拒合成而非告警放行。

    混烧把片段原生音降 -8 dB 作背景床，sidechain 闪避只在我们的音轨出声时触发，
    模型把同一句安排在句间静音段时压不住。判据读片段生成快照的实发正文
    （`gen.clip.envelope.positive`）里有没有 `prompts.positive_is_voiceless`
    认的不出声指令：白名单方向，认不出的稿一律拦。dubbed 期生成的旁白镜同样被
    拦——那条片段里模型重演了我们的配音，出路仍是 retake 或本次不烧。
    没有生成快照的片段不进判据：对它们不存在「生成时被要求出声」这个事实可读，
    输出侧另有 `_warn_native_residual_voice` 探测。

    只查旁白镜（`voice_kind == "voiceover"`）：声源按镜分治，对白镜的人声
    本来就由模型承担、旁白轨对其插静音，开口稿在那里是正稿不是事故。"""
    hit = []
    for s in project.active_shots:
        if voicecast.voice_kind(s) != "voiceover":
            continue
        env = ((s.get("gen") or {}).get("clip") or {}).get("envelope")
        if not env:
            continue
        if not prompts_mod.positive_is_voiceless(str(env.get("positive") or "")):
            hit.append(str(s.get("id")))
    if hit:
        raise ProjectError(
            f"native 混烧被拦：旁白镜 {'/'.join(hit)} 的片段不是按闭声稿生成的，"
            "烧录固定音色旁白会出现同一段两个人声。两条出路：\n"
            "   ① 置 retake 重生这些片段（重生稿按闭声出演编译）：review set "
            "--stage clip --state retake → gen-video（章节未写 native_voiceover 时"
            "带 --burn-voice）\n"
            "   ② 本次不烧：去掉 --burn-voice / 章节 native_voiceover: false"
            "（保留模型原生人声作主轨）")


def use_bgm_for(project) -> bool:
    """本章成片要不要叠曲库 BGM（三档互斥的唯一判据）：kenburns/dubbed 恒叠；
    scored 只在 `scored_bgm` 显式加铺；native 只在 `native_bgm` 显式加铺且未混烧——
    混烧已把片段原生音降成背景床占着 BGM 母线，再放曲库 BGM 会把那条床整个顶掉。
    合成、`cli._bgm_gate` 与 `cli._stage_audio_bed` 都从这里取，选曲与用曲同一口径。"""
    if project.scored_audio:
        return bool(project.data.get("scored_bgm"))
    if project.native_audio:
        return bool(project.data.get("native_bgm")) and not project.native_voiceover
    return True


def _gate_narration_track(project, has_narr: bool) -> None:
    """native 混烧前拦下「整条旁白轨不在盘」——旁白镜按闭声稿出演，它们的人声
    就是这条轨；轨不在盘时主音轨整体退回 `clip_audio_track`，成片里那几段的人声
    形态不受控而字幕照烧，且这条分支上没有任何打印。

    只判混烧这一档：kenburns/dubbed 缺整轨归 `cli._assemble_review_gate`（audio
    支按 `needs_tts` 逐镜查）与 animatic 的无音轨渲染，native 缺省不烧本就没有
    这条轨。盘上已有轨的半缺态（跑过一半/跑错镜）由 `_sync_narration` →
    `voicecast.narration_parts` 的 missing 逐镜点名，那条出口以有轨为前提。

    「这一章要不要旁白轨」取 `Project.needs_narration_track` 单点——`scored` 下
    人声随音乐音效由音频模型整轨产出，本函数与 `cli.cmd_run` 的配音门必须同答，
    否则 run 按章级判据跳过配音、合成再按另一套判据拦死，而闸给的出路是去合成
    一条这条路径随后明确丢弃的轨。

    判据带「至少一镜进旁白轨且有词」：全对白章开着 native_voiceover 时没有人声
    要烧，`stage_tts` 也不会登记 narration_file，拦它就是拦一个无害配置。
    谓词取 `voicecast.in_narration_track` 与 `shot_text`（与 stage_tts 合成侧、
    narration_parts 拼接侧同一对），不在闸里另写一份「哪些镜该有 wav」。"""
    if has_narr or not (project.needs_narration_track
                        and project.native_audio and project.native_voiceover):
        return
    ids = [str(s.get("id")) for s in project.active_shots
           if voicecast.in_narration_track(s, project.motion)
           and voicecast.shot_text(s)]
    if not ids:
        return
    raise ProjectError(
        f"native 混烧被拦：旁白镜 {'/'.join(ids)} 的人声由烧录旁白轨承担，而这条轨"
        "不在盘（audio.narration_file 未登记或本地缺失）。两条出路：\n"
        "   ① 先配音再合成：tts --chapter <项目>/<章节>——已合成过的重跑即重新"
        "登记，本地重合成不产生生成费用\n"
        "   ② 本次不烧：去掉 --burn-voice / 章节 native_voiceover: false"
        "（保留模型原生人声作主轨）")


def _warn_native_residual_voice(project, aspect: str) -> None:
    """混烧前对**旁白镜片段**做输出侧人声探测——闭声稿的执行没有确定性保证
    （同一提示词实测两发一守一破），提示词层的闸拦不住模型临场出的那段声；
    旁白镜要叠 TTS 旁白，残留人声与之直接相撞。对白镜的人声由模型承担，
    不在探测范围。

    判据是振幅级语音段检测（`speech_windows`），分不清人声与响亮音效，
    故只点名请人试听、不拦合成。探测对象是**本次合成这个比例**的片段：
    逐比例出片时每个比例都是模型的一次独立采样，出声的可能只有其中一支。
    拉不回本地的片段无从探测，静默跳过。"""
    from .speech import speech_windows
    hit = []
    for s in project.active_shots:
        if voicecast.voice_kind(s) != "voiceover":
            continue
        clip = project.clip_for(s, aspect)
        # 这里刻意不用 has_file()：它把 URL 一律视作存在，会把反解不了的外部
        # 地址送进 ffmpeg 走网络
        if not clip or not Path(str(clip)).is_file():
            continue
        try:
            segs = speech_windows(str(clip), float(s.get("dur") or 0))
        except Exception:  # noqa: BLE001  探测是增强信息，失败不阻断合成
            continue
        if segs:
            a, b = segs[0]
            hit.append(f"镜 {s.get('id')}（{a:.1f}-{b:.1f}s）")
    if hit:
        print(f"  ⚠ 混烧输出侧探测：{'、'.join(hit)} 的片段里检测到疑似人声段"
              "——闭声出演可能没被执行，请试听该片段；确认是模型出了声就置 "
              "retake 重roll（振幅判据分不清人声与响亮音效，此处只点名不拦）")


def _resolve_asset(project, ref: str, *, what: str = "转场素材",
                   hint: str = "先把素材放进 assets/transitions/") -> Path:
    """素材路径解析：绝对 > 项目目录相对 > 工作区相对（与设定图同宽容度）。
    缺文件硬报错——静默丢素材会悄悄改变构图。"""
    p = Path(ref)
    if p.is_absolute() and p.is_file():
        return p
    pdir = project.path.parent.parent   # chapters/<cid>.json → 项目目录
    for base in (pdir, pdir.parent):
        q = base / ref
        if q.is_file():
            return q
    raise ProjectError(f"{what}不存在: {ref}（{hint}）")


def _clip_cache_name(shot: dict, style: int | None, fi: float, fo: float,
                     extra: str = "", fic: str = "", foc: str = "") -> str:
    """片段缓存文件名 = **参数**缓存键：边缘淡化秒数 + Ken Burns 运镜风格号
    （+ 音轨形态等附加分量 `extra`）。

    源指纹（源文件 mtime / dur 偏差）负责**内容**过期，文件名负责**参数**过期——
    两者合起来才成立「同输入同输出」。风格号若不进键：改 `shots[].camera`（或弃镜
    引起轮换位移）时源图 mtime 未变、dur 未变，旧运镜片段会被静默复用进成片。
    图生视频片段（style=None）无 Ken Burns 运镜，不掺风格号避免无谓失效。

    **运镜算法版本同样进键**（`kenburns.ALGO_VERSION`）：源指纹盯的是素材变化，
    盯不住「算法改了」——改完平滑度而文件名不变，用户重合成会静默复用旧片段、
    以为改动没生效（只能靠 --force 全量重渲）。仅静图片段带此分量。"""
    # 秒数保留两位小数并加分隔符（round(x*10) 会把 0.25 与 0.2 折成同键、
    # fade↔fade_black 邻镜共键）；淡化底色同为渲染输入——改转场底色
    # （transition add --color / 直改章节 JSON）不换键就是「改了不生效」
    fsuf = f"_f{fi:.2f}-{fo:.2f}" if (fi or fo) else ""
    if fsuf and (fic or foc):
        fsuf += f"c{fic}-{foc}"
    if style is not None:
        fsuf += f"_k{style}"
        if kenburns.ALGO_VERSION > 1:      # v1 不带后缀：存量片段名不变、不无谓重渲
            fsuf += f"a{kenburns.ALGO_VERSION}"
    return f"shot_{shot['id']}{fsuf}{extra}.mp4"


def _pad_silent_audio(clip: str, dur: float) -> None:
    """给本地渲染的回落片段补一条静音音轨（与转场卡同参：aac 44100 立体声）。

    dubbed/native 时间线上生成片段自带音频，静图回落片段若无音轨，
    concat -c copy 拼出的流布局不一致——回落镜之后所有镜的音频整体前移。
    视频流原样拷贝（-c:v copy），只做封装级补轨，不重编码。"""
    p = Path(clip)
    tmp = p.with_name(p.stem + "_pad.mp4")
    run(["-i", str(p),
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={dur:.3f}",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
         "-shortest", str(tmp)], desc="pad fallback audio")
    os.replace(tmp, p)


def _concat_clips(clip_paths: list[str], out_path: Path) -> None:
    # 清单跟着产物命名（silent_<tag>.concat.txt）：build/ 由多比例/多任务共用，
    # 固定名会让并行的两次 assemble 互踩对方的清单
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text(
        "".join(concat_entry(Path(p).resolve()) for p in clip_paths), encoding="utf-8")
    run(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)],
        desc="concat clips")


def build(project, store, *, aspect: str, effects: list[str] | None = None,
          sub_cfg: dict | None = None, out: str | None = None,
          force: bool = False, fps: int | None = None,
          variant: str | None = None) -> str:
    """variant：产物变体名（如 animatic）。变体用独立的片段/中间产物命名空间，
    不与正式成片的 clips_<tag> 缓存互扰（草稿两段式的 Ken Burns 样片不能
    污染 dubbed/native 正式合成的规整片段缓存）。"""
    shots = project.active_shots     # 弃用(omt)镜不进成片
    fps = fps or store.fps
    w, h = store.canvas(aspect)
    effects = effects or []
    tag = (f"{variant}_" if variant else "") + aspect_tag(aspect)
    # native：Seedance 片段自带模型原生人声与音效，保留并作为主音轨、不叠曲库 BGM。
    # dubbed：片段音轨是模型对参考音的**重演**——嗓音不受锚定、逐镜自选（实测与
    # 发去的 TTS 包络相关性极低，同一角色跨镜换声），固定音色的承诺由我们自己的
    # 配音兑现：片段原声弃用，主音轨=逐镜 TTS 旁白轨（`_sync_narration` 按片段
    # 实测时长对齐）+ 曲库 BGM。kenburns：音频走我们的旁白+BGM。
    use_clip_audio = project.native_audio
    # 音频剧本模式：人声/音乐/音效已由音频模型混在一条轨里，我们既不再配 BGM，
    # 也不去动 Seedance 片段自带的音轨（那会和剧本里的人声撞成两层）
    scored = project.scored_audio
    if scored:
        use_clip_audio = False
    # BGM 母线是单占的（下面 `bg_label` 只有一个槽）；三档互斥判据见 use_bgm_for
    use_bgm = use_bgm_for(project)

    # 动镜档一镜一片：正镜缺片段即拒合成，不落回静图。静图形态是另一档
    # （`assemble -m a` 运行时覆盖），animatic 变体自带 kenburns 覆盖不经此判
    if project.uses_seedance and not variant:
        missing = [str(s.get("id")) for s in shots
                   if not tr_mod.is_transition(s) and not has_file(project.clip_for(s, aspect))]
        if missing:
            raise ProjectError(
                f"[{aspect}] 镜 {'、'.join(missing)} 没有片段——动镜档不落回静图；"
                "先 gen-video 补齐，静图样片走 assemble -m a")

    # 主音轨可用性必须在渲染任何片段/转场卡**之前**判定：转场卡的音轨形态跟着
    # 这个开关走，concat -c copy 要求各段流布局一致。片段（mock/个别模型）不含
    # 音轨时整体降级为旁白+BGM 通路；已上云的 URL 片段信任其自带音频（拉流探测
    # 又慢又可能因网络误降级），拼接后的兜底探测仍在。
    if use_clip_audio:
        sample = next((project.clip_for(s, aspect) for s in shots
                       if not tr_mod.is_transition(s)
                       and has_file(project.clip_for(s, aspect))), None)
        if sample is None or (
                not str(sample).startswith(("http://", "https://"))
                and not any(st.get("codec_type") == "audio"
                            for st in (probe_json(sample) or {}).get("streams") or [])):
            print("  ⚠ 生成片段不含音轨，主音轨降级为旁白+BGM（native 正常应自带音频）")
            use_clip_audio = False

    # 旁白轨的两道闸在渲染任何片段之前判：判据只读文档与盘上产物，等渲完几分钟
    # 再拒绝是白跑。native **默认不烧**：片段自带模型原生人声，叠 TTS = 同一句话
    # 两个人说（见 Project.native_voiceover：零开关自动混烧会坑掉切过 motion 的章节）
    narration = ensure_local(project.audio.get("narration_file"))
    has_narr = bool(narration and has_file(narration))
    _gate_narration_track(project, has_narr)
    if has_narr and use_clip_audio and project.native_audio and project.native_voiceover:
        _gate_native_double_voice(project)

    for s in shots:
        if not tr_mod.is_transition(s) and not has_file(project.image_for(s, aspect)):
            raise ProjectError(f"镜 {s.get('id')} 缺少 {aspect} 图像，请先运行 gen-image。")
        if float(s.get("dur", 0)) <= 0:
            # scored 下没有逐镜 TTS，`dur` 是指挥层写死的时间轴（剧本的 [起s:止s]
            # 反过来对齐它）——指向 tts 会让人去跑一条这个模式根本不走的命令
            raise ProjectError(
                f"镜 {s.get('id')} 时长为 0，" + ("请在分镜里写 dur（音频剧本模式的"
                "时间轴由分镜定，剧本的时间控制对齐它）。" if scored
                else "请先运行 tts 回填时长。"))

    clips_dir = project.subdir(f"clips_{tag}")
    build_dir = project.subdir("build")

    # 1) 逐镜出片段：有图生视频片段（dubbed/native）则规整取用，否则 Ken Burns 静图运镜。
    #    两种形态可在同一项目里逐镜混用，compose 透明处理。
    #    缓存复用按「源指纹」判定而非「文件存在」：retake 重生 / 宫格换选 /
    #    版本回滚后源素材 mtime 变新（或 dur 变化）→ 自动重渲对应片段，
    #    否则成片会静默编入旧画面（零 API 成本，只多一次本地 ffmpeg）。
    # 第一遍：渲普通镜片段（xfade 族转场卡需要相邻片段的冻结帧，须先就绪）。
    # 计划/渲染两段式：参数与缓存判定在主线程算好，过期片段交并发层重渲——
    # 纯本地 ffmpeg、各写各的 shot_*.mp4，天然满足「工作线程只产文件」铁律；
    # `seg` 在计划期就按镜下标登记完毕，完成顺序不影响第二遍与拼接的顺序。
    # 刻意不给幂等护栏（Task.out）：编码中断会留半截 mp4，护栏会把它当成品编进成片。
    seg: dict[int, str] = {}
    stale_jobs: list[dict] = []
    for i, s in enumerate(shots):
        if tr_mod.is_transition(s):
            continue
        dur = float(s["dur"])
        src = project.clip_for(s, aspect)
        # 画面取材**尊重 motion**：本地渲染模式（kenburns）一律走分镜图，
        # 即便盘上有 Seedance 片段也不取——这正是「这次不要 seedance，用最基本的
        # 方式出片」的入口（`assemble --motion a`，运行时覆盖不落盘）。
        # 动镜档的片段齐备已在上方判过；animatic 变体按 kenburns 覆盖走静图。
        use_gen = project.uses_seedance and has_file(src)
        source = src if use_gen else project.image_for(s, aspect)
        # 边缘淡化由相邻转场镜驱动、运镜风格由 camera 语义驱动——两组参数都进
        # 片段文件名 = 缓存键（见 _clip_cache_name）
        fi, fic, fo, foc = tr_mod.edge_fades(shots, i)
        style = None if use_gen else kenburns.style_for(s.get("camera"), i)
        # 回落片段在 dubbed/native 下要补静音轨（见 _pad_silent_audio）；
        # 音轨形态必须进缓存键，否则 kenburns 模式缓存下的无声片段会被
        # dubbed 合成静默复用，同样造成流布局不一致。native 生成片段带音频
        # 边缘平滑（_ae 分量）——不进键的话旧缓存的硬台阶片段会被静默复用
        pad_audio = use_clip_audio and not use_gen
        extra = "_au" if pad_audio else (
            "_ae" if use_clip_audio and use_gen else "")
        clip = clips_dir / _clip_cache_name(s, style, fi, fo, extra,
                                            fic=fic, foc=foc)
        stale = not clip.is_file()
        if not stale:
            try:
                # 源指纹 = 源文件 mtime（换图即重渲；URL 源探不到本地文件，
                # 直接落到时长核对那一支）
                src_file = Path(str(source))
                if src_file.is_file() and src_file.stat().st_mtime > clip.stat().st_mtime:
                    stale = True
                elif abs(probe_duration(clip) - dur) > 0.15:
                    stale = True
            except Exception:  # noqa: BLE001  探测失败一律视为过期，宁可重渲
                stale = True
        seg[i] = str(clip)
        if force or stale:
            stale_jobs.append({"id": s.get("id"), "use_gen": use_gen, "src": src,
                               "image": project.image_for(s, aspect),
                               "dur": dur,
                               "clip": str(clip), "style": style,
                               "pad_audio": pad_audio,
                               "fi": fi, "fic": fic, "fo": fo, "foc": foc})

    def _render_seg(j):
        if j["use_gen"]:
            kenburns.fit_clip(j["src"], j["dur"], j["clip"], width=w, height=h,
                              fps=fps, keep_audio=use_clip_audio,
                              fade_in=j["fi"], fade_in_color=j["fic"],
                              fade_out=j["fo"], fade_out_color=j["foc"],
                              audio_edge=NATIVE_AUDIO_EDGE)
        else:
            # 运镜风格：camera 语义优先（分镜写「拉远揭示」就真的拉远），
            # 无语义回落镜号轮换——风格号在缓存键处算过一次，此处必须复用
            kenburns.render_shot(j["image"], j["dur"], j["clip"],
                                 width=w, height=h, fps=fps,
                                 effect_index=j["style"],
                                 fade_in=j["fi"], fade_in_color=j["fic"],
                                 fade_out=j["fo"], fade_out_color=j["foc"])
        if j.get("pad_audio"):
            _pad_silent_audio(j["clip"], j["dur"])

    if stale_jobs:
        results = parallel.run(
            [parallel.Task(key=f"seg:{j['id']}", run=(lambda it=j: _render_seg(it)),
                           label=f"镜 {j['id']}", meta=j) for j in stale_jobs],
            workers=parallel.resolve_workers(None), retries=0,
            on_progress=parallel.progress_printer("片段渲染"))
        bad = [d for d in results if not d.ok]
        if bad:
            raise ProjectError(
                f"{len(bad)} 个片段渲染失败："
                + "、".join(f"{d.label}（{d.message}）" for d in bad)
            ) from bad[0].error
    # 第二遍：渲转场段（零 API 成本，参数便宜每次现渲不设缓存）
    clip_paths = []
    for i, s in enumerate(shots):
        if not tr_mod.is_transition(s):
            clip_paths.append(seg[i])
            continue
        dur = float(s["dur"])
        spec = tr_mod.spec_of(s)
        clip = clips_dir / f"shot_{s['id']}_tr.mp4"
        prev_c, next_c = seg.get(i - 1), seg.get(i + 1)
        if spec["type"] == "clip" and spec.get("asset"):
            asset = _resolve_asset(project, spec["asset"])
            tr_mod.fit_asset(asset, clip, dur=dur, width=w, height=h, fps=fps,
                             with_audio=use_clip_audio)
        elif spec["family"] == "xfade" and prev_c and next_c:
            # 冻结帧 xfade：前镜尾帧 →（wipe 经色卡两段式）→ 后镜首帧
            tr_mod.render_xfade_card(prev_c, next_c, clip, spec=spec, dur=dur,
                                     width=w, height=h, fps=fps,
                                     with_audio=use_clip_audio,
                                     profile=project.profile)
        else:
            if spec["family"] == "xfade":
                # 章首/章尾（只有单侧邻居）与前后皆空一样退化为字卡并告警——
                # 全体 xfade 型同一条规则，新增类型不搞特例
                print(f"  ⚠ 转场镜 {s.get('id')}: {spec['type']} 需要前后都有普通镜，"
                      "已退化为字卡")
            tr_mod.render_card(clip, spec=spec, dur=dur, width=w, height=h,
                               fps=fps, with_audio=use_clip_audio,
                               profile=project.profile)
        clip_paths.append(str(clip))

    # 2) 拼接为无声视频
    silent = build_dir / f"silent_{tag}.mp4"
    _concat_clips(clip_paths, silent)
    video_dur = probe_duration(silent)
    if use_clip_audio:
        # 片段主音轨必须真实存在才能引用 [0:a]——mock 视频或个别模型可能输出
        # 静音片段，此时降级为旁白+BGM 通路，而不是让 filtergraph 直接失败
        streams = (probe_json(silent) or {}).get("streams") or []
        if not any(st.get("codec_type") == "audio" for st in streams):
            print("  ⚠ 生成片段不含音轨，主音轨降级为旁白+BGM（native 正常应自带音频）")
            use_clip_audio = False

    # 3) 该比例的字幕（按 profile 的 subtitle 模式渲染到正确画布）
    subtitle = project.subdir("subs") / f"sub_{tag}.ass"
    subtitle_mod.render(project.timeline(), subtitle, canvas_w=w, canvas_h=h,
                        sub_cfg=sub_cfg,
                        spans_of=speech_spans_resolver(project, aspect))

    # 4) 组装最终命令。旁白轨只在真要消费时才自愈重拼：kenburns/dubbed（主音轨）
    # 与 native 混烧（需显式开）。dubbed 的重拼基准是 gen-video 买下的画面秒数
    # （回填的 dur）：逐镜 wav 起点与片段段起点对齐，wav 短于片段的余量补静音。
    # `use_clip_audio` 在拼接探测后可能已降级，烧不烧按此刻的值定
    burn_narr = bool(has_narr
                     and (not use_clip_audio
                          or (project.native_audio and project.native_voiceover)))
    # 盘上有配音却不烧时必须说清楚为什么——否则成片里少一条轨而全程零提示
    if has_narr and use_clip_audio and not burn_narr:
        print("  ⓘ native：片段自带原生人声，本次**不烧录**我们的固定音色配音"
              "（盘上的 narration.wav 原样保留）。要纪录片式混烧（旁白上主轨 + "
              "原生音轨在旁白镜窗口降为背景床）请加 `--burn-voice`，"
              "或章节写 native_voiceover: true。")
    # scored 下 narration.wav 不参与：旁白自愈重拼是按逐镜 wav 重建的，
    # 而这条路根本没有逐镜 wav。盘上有配音却不烧必须说清楚为什么——沉默会让人
    # 以为「配音丢了」（与上面 dubbed/native 两条提示同一条纪律）
    if has_narr and scored:
        print("  ⓘ scored：人声已随音乐音效由音频模型混在一条轨里，"
              "盘上的 narration.wav 不参与合成（原样保留）。")
    # 自愈判定这条轨与当前分镜无关时返回 None（它自己已打印原因），本次即按
    # 「没有旁白轨」合成：native 回到片段原生人声，其余档只剩 BGM
    synced = _sync_narration(project, Path(narration)) \
        if (burn_narr and not scored) else None
    narration = str(synced) if synced is not None else None
    bgm = ensure_local(project.audio.get("bgm_file"))

    # 输入表：视频叠层与音频轨共用同一个输入索引（详见 mixdown.InputTable）
    tbl = mixdown.InputTable(silent)
    narr_label = bg_label = None
    bed_windowed = False       # 床轨自带窗口门控 EQ 时，premix 不再整轨叠一遍
    amb_labels = []

    if scored:
        # 成品轨直接上主轨（0 dB 基准，与旁白轨同一条通路）：不叠 BGM 故无闪避，
        # 让路 EQ 也无从谈起——模型已经替我们做完了这一层
        score = ensure_local(project.audio.get("score_file"))
        if not (score and has_file(score)):
            raise ProjectError(
                "audio_mode=scored 但音频剧本整轨尚未生成。\n"
                "   看分段与报价（零成本）：score --chapter <项目>/<章节> --dry-run\n"
                "   生成：score --chapter <项目>/<章节>（assemble/run 也会自动调它）\n"
                "   剧本写在章节顶层 audio_script.segments[]，写法见 kn-audio SKILL 第四节")
        narr_label = mixdown.narration_track(tbl, score, dur=video_dur)
    elif use_clip_audio:
        if narration and project.native_audio:
            # native 配音混烧（**显式 opt-in**：--burn-voice 或 native_voiceover:true，
            # 缺省不烧）：TTS 旁白上主轨（0 dB），片段原生音轨占 BGM 槽位。
            # 床压制（降电平+让路 EQ）只落在旁白镜窗口——声源按镜分治后这条轨在
            # 对白镜窗口里是主人声，整轨静态压制会把它压低 8dB 还挖中频；
            # sidechain 闪避照旧（旁白轨驱动，对白窗口里旁白静音天然不触发）。
            # **只在 native**：dubbed 的片段音轨本来就是我们 TTS 的对口型版，
            # 再叠一层原始 TTS 就是同一句台词的两份叠加（双人声闸已在渲染前判）
            _warn_native_residual_voice(project, aspect)
            vo_wins = [(start, end) for start, end, s in project.timeline()
                       if voicecast.voice_kind(s) == "voiceover"]
            dl_wins = [(start, end) for start, end, s in project.timeline()
                       if voicecast.voice_kind(s) == "dialogue"]
            # 两路人声对齐：TTS 旁白与模型对白来源不同、电平相差可达 18 dB，末级
            # 整体推救不了配比。旁白轨只测旁白镜窗口、片段音轨只测对白镜窗口，
            # 差值作旁白轨入混静态增益（判据与钳制见 mixdown.narration_match_gain_db）。
            # 整章没有对白镜时无对齐目标，旁白按 0 dB 基准交末级。
            match_db = 0.0
            if dl_wins:
                narr_m = mixdown.measure_loudness(mixdown.measure_windows_args(
                    narration, vo_wins or [(0.0, video_dur)]))
                dial_m = mixdown.measure_loudness(mixdown.measure_windows_args(
                    silent, dl_wins))
                match_db = mixdown.narration_match_gain_db(narr_m, dial_m)
                print(mixdown.narration_match_report(narr_m, dial_m, match_db))
            narr_label = mixdown.narration_track(tbl, narration, dur=video_dur,
                                                 gain_db=match_db)
            bg_label = mixdown.clip_bed_track(tbl, dur=video_dur,
                                              bed_windows=vo_wins or None)
            bed_windowed = bool(vo_wins)
        else:
            narr_label = mixdown.clip_audio_track(tbl, dur=video_dur)
    elif narration:
        narr_label = mixdown.narration_track(tbl, narration, dur=video_dur)
    if use_bgm and has_file(bgm):
        bg_label = mixdown.bgm_track(tbl, bgm, dur=video_dur, ducked=bool(narr_label))

    # 特效：逐特效顺序应用（简单滤镜 → 复杂子图 → 图层叠加 → 环境音）
    plans = [p for p in (fx.build_plan(n, w, h, fps) for n in effects) if p]
    cur = "0:v"
    vk = 0
    for p in plans:
        if p.vfilters:
            nl = f"vfx{vk}"; vk += 1
            tbl.video.append(f"[{cur}]" + ",".join(p.vfilters) + f"[{nl}]")
            cur = nl
        if p.subgraph:
            nl = f"vfx{vk}"; vk += 1
            tbl.video.append(p.subgraph.format(IN=cur, OUT=nl))
            cur = nl
        if p.overlay_input:
            oi = tbl.add_lavfi(p.overlay_input)
            nl = f"vfx{vk}"; vk += 1
            ofilt = p.overlay_filter or "null"
            if p.overlay_blend == "overlay":
                # 物理不透明层（花瓣/落雪堆积）：真 alpha 合成，遮挡主画面
                tbl.video.append(f"[{oi}:v]{ofilt},format=yuva420p[ov{oi}]")
                tbl.video.append(f"[{cur}][ov{oi}]overlay[{nl}]")
            else:
                # 发光/加色层（粒子/火焰/光扫/雨雪）：必须在 RGB(gbrp) 空间 blend——
                # 否则 screen/lighten 等模式在 YUV 上会把中性色度(128)也参与运算，
                # 把整幅画面染偏色（星尘→全屏品红、萤火→全屏绿）。
                tbl.video.append(f"[{oi}:v]{ofilt},format=gbrp[ov{oi}]")
                tbl.video.append(f"[{cur}]format=gbrp[vbase{oi}];"
                                 f"[vbase{oi}][ov{oi}]blend=all_mode={p.overlay_blend},"
                                 f"format=yuv420p[{nl}]")
            cur = nl
        if p.audio_input:
            amb_labels.append(mixdown.ambient_track(
                tbl, p.audio_input, p.audio_filter or "anull", dur=video_dur))

    # 转场短音效：外置音效库优先（music/sfx/ + config/audio.yaml，专业素材）、缺文件回落
    # 纯 ffmpeg 合成；按转场起点 adelay 进环境音通道——BGM/旁白之上轻叠，
    # kenburns（片段无音轨）与 dubbed/native 通吃。
    # 起点 = 前镜淡出起点再提前 mixdown.SOUND_LEAD：xfade 族 edge=0，不提前的话
    # 音效整段落在切点之后（scan 的 riser 蓄势音尤其失效）。
    for start, _end, s in project.timeline():
        if not tr_mod.is_transition(s):
            continue
        spec = tr_mod.spec_of(s)
        if spec.get("sound") == "off":
            continue
        span = tr_mod.total_span(s)
        t0 = mixdown.sound_start(start, spec["edge"])
        sfile = tr_mod.resolve_sound_file(spec["sound"])
        if sfile:                                    # B 外置音效（sfx 库）
            amb_labels.append(mixdown.transition_sound_track(
                tbl, filt=tr_mod.fit_sound_filter(span), dur=video_dur, delay=t0,
                file=sfile))
        else:                                        # A 纯 ffmpeg 合成兜底
            src, filt = tr_mod.whoosh_audio(span, kind=spec["sound"])
            amb_labels.append(mixdown.transition_sound_track(
                tbl, filt=filt, dur=video_dur, delay=t0, lavfi=src))

    # 字幕烧录（fontsdir 指向工程内置字体：libass 据此加载阿里普惠体等免费商用字体，
    # 不依赖各机器系统字体安装，字幕跨系统一致——见 fonts.FONTS_DIR / subtitle 默认族名）
    if has_file(subtitle):
        from ..fonts import FONTS_DIR
        tbl.video.append(f"[{cur}]ass={filter_literal(subtitle)}"
                         f":fontsdir={filter_literal(FONTS_DIR)}[vout]")
        vmap = "[vout]"
    else:
        vmap = "0:v" if cur == "0:v" else f"[{cur}]"

    # 音频混合 · 让路 EQ + BGM 自动闪避(ducking)：旁白说话时用 sidechain 压缩自动压低
    # 背景乐、停顿恢复，让人声更清晰专业；环境音(雨/风)不闪避、保持稳定。
    # 末级再做「响度归一 + 削波防护」——两步线性方案，先只测不改拿到实测响度，
    # 再挂静态增益与限幅（选型理由见 mixdown 模块头）。
    premix = mixdown.premix_graph(tbl, narration=narr_label, bgm=bg_label,
                                  ambient=amb_labels, bed_eq=not bed_windowed)
    amap = None
    if premix:
        measured = mixdown.measure_loudness(mixdown.measure_mix_args(tbl, premix))
        # solo = 主音轨不在场（整章无旁白：白噪音/环境音沉浸、金句配乐）——与上面
        # bgm_track(ducked=…) 同一个判据，钳制区间随之换档（见 mixdown.MASTER_SOLO）
        gain_db = mixdown.master_gain_db(measured, motion=project.motion,
                                         solo=not narr_label)
        print(mixdown.report(measured, gain_db))
        amap = mixdown.master_graph(tbl, premix, gain_db)

    # 变体产物落自己的目录（如 animatic/），不进 output/ ——那里只放正式成片
    out_path = Path(out) if out else (project.subdir(variant or "output") / f"{project.id}_{tag}.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    args = [*tbl.args]
    if tbl.filters:
        args += ["-filter_complex", ";".join(tbl.filters)]
    args += ["-map", vmap]
    if amap:
        args += ["-map", amap]
    args += ["-t", f"{video_dur:.3f}", "-r", str(fps),
             "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if amap:
        args += ["-c:a", "aac", "-b:a", "192k"]
    args += ["-movflags", "+faststart", str(out_path)]

    run(args, desc=f"compose {aspect}")
    _sweep_orphan_clips(clips_dir, clip_paths)
    return str(out_path)


def _sweep_orphan_clips(clips_dir: Path, used: list[str]) -> int:
    """清掉本比例缓存目录里**再也命不中**的片段，返回删除数。

    缓存键一变（运镜算法版本 / 风格号 / 淡化参数 / 音轨形态），旧键的片段就
    永远不会被复用，却也从来没人删——一次 `ALGO_VERSION` 升级
    在单章留下 35 个孤儿、39.7MB，而这类升级以后每次都会发生。

    只扫本比例自己的 `clips_<tag>/`（各比例与 animatic 变体天然隔离）、只删
    `shot_*.mp4`，且**必须在合成成功之后**——本次清单是完备的（每个正镜/转场镜
    各一个），删错的代价也只是下次重渲（零 API 成本）。"""
    keep = {Path(p).name for p in used}
    n = 0
    for f in clips_dir.glob("shot_*.mp4"):
        if f.name in keep:
            continue
        try:
            f.unlink()
            n += 1
        except OSError:      # 占用/权限问题不该让已经出片的合成变成失败
            pass
    if n:
        print(f"  ⌫ 清理过期片段缓存 {n} 个（缓存键已变，永不再命中）")
    return n
