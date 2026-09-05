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

"""音频剧本的分段（`audio_mode: scored` 的前置计算）。

段界就是转场镜：转场本来就是场景切换，一段戏在那里收束，音乐在那里重新起头；
段界落在别处，音乐会在一段连贯的戏中间被切断。
所以**每个转场收一段**、接缝永远落在转场上。单段另有硬上限——音频模型单次最长
输出 120 秒，两转场之间超了上限也不硬切，点名让人补转场。

剧本本身由指挥层按 `kinema-audio` 撰写（引擎内没有 LLM，绝不从分镜自动生成剧本，
与 `sketch.beats` 同制度）。本模块只做机械的三件事：按转场切段、核对段长、
把每段对应的镜号区间与秒段算出来供剧本对齐时间控制。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import KinemaError
from .ffmpeg import concat_audio, probe_duration
from .pipeline import versioning
from .pipeline.transitions import is_transition

# 单次输出上限（秒）。官方口径 120s，留一点余量给模型自己的收尾，
# 避免正好卡在上限上被截断
MAX_SEGMENT_SEC = 115.0


def _dur(shot: dict) -> float:
    try:
        return max(0.0, float(shot.get("dur") or 0))
    except (TypeError, ValueError):
        return 0.0


def plan(project) -> list[dict]:
    """把章节切成若干可生成的段。

    返回 `[{no, shots: [镜号…], start, end, dur, spans}]`：

    · `start/end` 是该段在**全片时间轴**上的秒位置——用来知道这段是整个故事的哪一截。
    · `spans` 是段内逐镜的 `{id, start, end}`，秒位置**从该段自己的 0 开始**。

    **写时间控制只能用 `spans`**：每段是各自一次请求，模型的时间轴每次都从 0 起，
    照 `start/end` 的全片秒去写，第二段往后会整体偏移一整段的长度。

    切分规则：**每个转场镜就是段界**——转场即场景切换，一段戏在那里收束，
    音乐就该在那里重新起头；转场镜留在前一段（它是那段戏的收尾）。
    冷开场例外：段里还没有任何正戏镜时（章节以字卡/黑场开场），转场不收段而
    归后一段开头——否则会切出一个只有转场、一句词都没有的空段。
    两转场之间超过单段上限时不硬切——硬切会把接缝落进一句台词中间，
    超限交给 `check()` 点名让人补转场。"""
    shots = project.active_shots        # 弃镜(omt)不进时间轴，剧本自然也不该有它
    segs: list[dict] = []
    cur: list[dict] = []
    start = 0.0
    staged = False      # 本段是否已有正戏镜（决定转场是「收尾」还是「冷开场」）

    def flush():
        nonlocal cur, start, staged
        if not cur:
            return
        dur = sum(_dur(x) for x in cur)
        spans, t = [], 0.0
        for x in cur:                       # 段内相对秒：每段各自从 0 起
            spans.append({"id": x.get("id"), "start": round(t, 2),
                          "end": round(t + _dur(x), 2)})
            t += _dur(x)
        segs.append({"no": len(segs) + 1,
                     "shots": [s.get("id") for s in cur],
                     "start": round(start, 2),
                     "end": round(start + dur, 2),
                     "dur": round(dur, 2),
                     "spans": spans})
        start += dur
        cur, staged = [], False

    for s in shots:
        cur.append(s)
        if is_transition(s):
            if staged:
                flush()
        else:
            staged = True
    flush()
    if not segs:
        raise KinemaError("章节没有可生成的分镜（全部弃用？）")
    return segs


def check(project) -> list[str]:
    """段长体检：返回超限段的说明（空列表 = 全部可生成）。

    引擎不替用户在戏中间硬切，所以超限只能靠**插一个转场镜**来解决——
    报错必须说清超了多少、该往哪个镜号附近插。"""
    problems = []
    for seg in plan(project):
        if seg["dur"] > MAX_SEGMENT_SEC:
            ids = seg["shots"]
            problems.append(
                f"第 {seg['no']} 段 {seg['dur']:.1f}s 超过单次上限 {MAX_SEGMENT_SEC:.0f}s"
                f"（镜 {ids[0]}~{ids[-1]}）——在这段中间插一个转场镜作为接缝，"
                f"或把这些镜拆成两章")
    return problems


# 没有声线描述的说话人用的中性底：**是一句真能用的描述而不是占位符**——
# 底稿会被原样发给模型，写「（待补）」这类占位就是把提示词泄进音轨里。
_NEUTRAL_VOICE = "嗓音自然，吐字清晰，语速平稳"
_NEUTRAL_NARRATOR = "叙述者，嗓音温润，吐字清晰，语速平稳"
NARRATOR_NAME = "旁白"


def speaker_voice_desc(project, name: str) -> tuple[str, bool]:
    """某个说话人的声线描述 → `(描述, 是否为真实设定)`。

    取材=这个人**在用**那把定制音色的原话（`voicebank.voice_desc`），没有则中性底。
    必须按「在用」取而不是按 owner 扫全表：一个实体的档案里躺着历次选过的声音，
    扫到哪条算哪条会让底稿按一把已经不用的嗓子去写。
    第二个返回值供调用方提示「这几个人还没有声线描述，值得交给 AI 补」。"""
    from .voicebank import voice_desc
    desc = voice_desc(project.data, name)
    if desc:
        return desc.strip(), True
    return (_NEUTRAL_NARRATOR if name == NARRATOR_NAME else _NEUTRAL_VOICE), False


def _line_spans(shot: dict, start: float, dur: float) -> list[tuple[dict, float, float]]:
    """把一镜的时间窗按各句字数比例切给逐句（实现真源 `voicecast.line_spans`，
    native 台词时间轴与 scored 底稿秒段共用同一份切分）。"""
    from .voicecast import line_spans, shot_lines
    return line_spans(shot_lines(shot), dur, start=start)


def segment_cast(project, seg: dict) -> list[tuple[str, dict, dict]]:
    """该段的出场说话人，按出场序去重 → `[(名字, 代表镜, 代表句)]`。

    带上代表镜与代表句是为了让调用方能直接走 `voicecast.resolve_line_voice`
    那条既有解析链拿到音色——音色优先级（句级 > 角色表 > 旁白锁 > profile）
    只该有一份实现。"""
    from .voicecast import shot_lines
    shots = {str(s.get("id")): s for s in project.active_shots}
    out: list[tuple[str, dict, dict]] = []
    seen: set[str] = set()
    for span in seg.get("spans") or []:
        shot = shots.get(str(span["id"]))
        if shot is None:
            continue
        for ln in shot_lines(shot):
            who = _speaker_name(ln)
            if who not in seen:
                seen.add(who)
                out.append((who, shot, ln))
    return out


def _speaker_name(line: dict) -> str:
    """段内说话人名：旁白别名（空、narrator、VO 等）归一为 NARRATOR_NAME，与
    `voicecast.is_narrator` 同一判据。"""
    from .voicecast import is_narrator
    spk = str(line.get("speaker") or "").strip()
    return NARRATOR_NAME if is_narrator(spk) else spk


def anchor_plan(project, store, seg: dict, default_ref: str | None) -> dict:
    """该段谁能带上参考音、谁只能靠文字 → `{anchored: [{who, voice_type, no}], loose: [名字]}`。

    **纯判定、零副作用**：网页要在花钱之前显示「这次带不带音色」，而真发那条路会为
    官方音色现合成参考音。两侧共用本函数，页面显示才不会与实发对不上。
    真发时个别音色的参考音合成失败会再降级一次，那是运行期事实，由发送侧报出。"""
    from .voicecast import MAX_ANCHOR_REFS, resolve_line_voice
    anchored, loose = [], []
    nos: dict[str, int] = {}
    for who, shot, line in segment_cast(project, seg):
        # 缺省音只属于旁白：未选角的角色落 loose 由告警点名，不借旁白锁的声音出演
        _ref, vt = resolve_line_voice(project, store, shot, line,
                                      default_ref if who == NARRATOR_NAME else None)
        if not vt:
            loose.append(who)
            continue
        # 同一把声音只占一条参考位（接口上限按条数算）：两个角色共用一把音色时，
        # 各占一位等于白丢一个真正需要锚定的说话人，还把同一条 clip 发两遍
        no = nos.get(vt)
        if no is None:
            if len(nos) >= MAX_ANCHOR_REFS:
                loose.append(who)
                continue
            no = len(nos) + 1
            nos[vt] = no
        anchored.append({"who": who, "voice_type": vt, "no": no})
    return {"anchored": anchored, "loose": loose}


def voice_def(who: str, desc: str) -> str:
    """声线定义段的一行。起草按它写、发送侧按同一格式判「剧本里写没写过这个人」
    ——格式各写一份的下场是同一句描述在正文里出现两次。"""
    return f"{who} 是{desc}"


def has_voice_def(text: str, who: str) -> bool:
    return f"{who} 是" in (text or "")


def draft_segment(project, seg: dict) -> tuple[str, list[str]]:
    """按分镜拼出这一段的**机械底稿** → `(正文, 缺声线描述的说话人)`。
    确定性、零成本、不调用任何模型。

    引擎内没有 LLM，所以底稿只做确定性的那部分——而那恰好是最容易错、错了最贵的
    两件：**台词逐字取 `narration`/`lines`**（字幕与它同源，差一个字成片就是
    「念的和写的不一样」）与**段内秒段**（每段各自一次请求、模型时间轴从 0 起，
    手算必错）。声线气质、配乐、音效、逐句语气是创作，归指挥层在底稿上改写
    ——网页「⧉ 音频剧本指令」把底稿连同人设一起交给 AI，这才是这条路的正常用法。

    产出即为 kinema-audio 五段式的 ①③④ 段（声线定义 / 逐句演绎 / 时间控制），
    缺的 ② 音乐与句内音效正是要 AI 补的那两段。"""
    from .voicecast import shot_lines
    shots = {str(s.get("id")): s for s in project.active_shots}
    speakers: list[str] = []
    body: list[str] = []
    for span in seg.get("spans") or []:
        shot = shots.get(str(span["id"]))
        if shot is None:
            continue
        for ln, t0, t1 in _line_spans(shot, span["start"], span["end"] - span["start"]):
            who = _speaker_name(ln)
            if who not in speakers:
                speakers.append(who)
            body.append(f"{who}说道：[{t0}s:{t1}s]“{ln['text']}”")
    if not body:
        raise KinemaError(
            f"第 {seg.get('no')} 段没有任何台词（镜 {seg.get('shots')}）——"
            f"纯画面段的剧本只能手写或交 AI 写氛围，引擎起不了稿")
    head, thin = [], []
    for who in speakers:
        desc, real = speaker_voice_desc(project, who)
        head.append(voice_def(who, desc))
        if not real:
            thin.append(who)
    # 声线定义段与逐句演绎段之间空一行——kinema-audio 的分段惯例，模型据此分辨
    # 「这几行在定义谁」与「这几行是台词」
    return "\n".join(head) + "\n\n" + "\n".join(body), thin


def draft(project) -> tuple[list[str], list[str]]:
    """全章逐段底稿 → `(每段正文, 全章缺声线描述的说话人去重表)`。"""
    rows, thin = [], []
    for seg in plan(project):
        text, miss = draft_segment(project, seg)
        rows.append(text)
        for w in miss:
            if w not in thin:
                thin.append(w)
    return rows, thin


def segment_sig(seg: dict, text: str) -> str:
    """段指纹（sha256 内容哈希）= 剧本原文 + 该段覆盖的镜与秒长。

    逐段幂等靠它：盘上有 `score_NN.mp3` 时，只有指纹一致才算"这一段还是那一段"
    而直接复用。少了它，改完剧本重跑会拿旧音轨拼出一条听起来"没生效"的成片；
    多算了镜与秒长，是因为分镜时长一改，同一段文字对应的时间轴就已经不同了。"""
    # 分隔符用 NUL：剧本正文里不可能出现它，换成空格/换行会让
    # 「文字末尾多一空格」与「镜号少一个」哈希成同一个值
    raw = "\x00".join([text, str(seg.get("shots")), str(seg.get("dur"))])
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def segment_script(project, seg: dict) -> str:
    """取某一段的剧本文本。

    `audio_script` 两种写法：整段字符串（单段片子直接写完）或按段落列表
    （`audio_script.segments[]`，长片逐段写）。列表写法下段数必须与分段数一致
    ——对不上说明分镜时长改过而剧本没跟着改，这时候硬发出去只会烧钱出错片。"""
    raw = project.data.get("audio_script")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise KinemaError("音频剧本为空（章节顶层 `audio_script`）——先按 kinema-audio 写音频剧本")
        total = len(plan(project))
        if total > 1:
            raise KinemaError(
                f"本章要切 {total} 段生成，但 audio_script 只写了一整段。"
                f"改成 {{\"segments\": [第1段, 第2段, …]}} 逐段写，"
                f"每段的时间控制按该段的起止秒对齐")
        return text
    if isinstance(raw, dict):
        parts = raw.get("segments")
        if isinstance(parts, list) and parts:
            total = len(plan(project))
            if len(parts) != total:
                raise KinemaError(
                    f"audio_script.segments 有 {len(parts)} 段，而按转场切出来是 "
                    f"{total} 段——分镜时长或转场改过，剧本要跟着改")
            return str(parts[seg["no"] - 1]).strip()
    raise KinemaError("audio_script 缺失或格式不对（字符串，或 {segments: [...]})")


def score_reconcat(project) -> float:
    """按 `gen.score.segments[]` 的当前各段重拼整轨，回填时长；返回总秒数。

    **切换段版本之后必须调**——段文件换了而整轨没重拼，盘上那条音轨还是旧的，
    页面却已经显示切过去了，是最难查的一类"改了没生效"。CLI 与 Studio 共用
    本实体（studio 域不能反向引 cli，实体归领域模块）。"""
    segs = versioning.score_segments(project)
    if not segs:
        raise KinemaError("本章还没有生成过音频剧本，无从重拼")
    parts = []
    for e in sorted(segs, key=lambda x: int(x.get("no") or 0)):
        f = e.get("file")
        if not f or not Path(f).is_file():
            raise KinemaError(f"第 {e.get('no')} 段音频不在盘: {f or '（未登记）'}"
                              f"（跑一次 score 补齐）")
        parts.append(("file", str(f)))
    out = project.subdir("audio") / "score.wav"
    concat_audio(parts, out)
    total = probe_duration(out)
    if total <= 0:
        out.unlink(missing_ok=True)
        raise KinemaError(f"重拼后时长为 0（{len(parts)} 段）——各段音频仍在盘，排查后重试")
    project.audio["score_file"] = str(out)
    project.data.setdefault("gen", {}).setdefault("score", {})["duration"] = round(total, 2)
    project.save()
    return total
