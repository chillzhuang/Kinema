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

"""镜级配音策略（**音画同步生命线的单一真源**，改前先跑 test_delivery/test_prompts）。

管的是「每一句用哪把声音、占多长时间」：音色解析链 `resolve_shot_voice`/`resolve_line_voice`
· 镜内多段台词 `shot_lines` · 表现力契约 `shot_expressive_params`+`delivery_instruction`
· 停顿门控 `shot_pauses` 与时长折算 `shot_duration`（写侧）· 请求秒数 `request_seconds`
（读侧）· 全片旁白轨拼接序列 `narration_parts`。

「这把声音是怎么来的」不在这里——试音、档案、引用账归 `voicebank`，它单向依赖本模块。
两件事的节奏完全不同：本模块跑在每一次渲染的热路径上，那边只在编辑期动。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import FFmpegError
from .ffmpeg import probe_duration

# 音色锚定：先用官方音色合成一句参考音，之后该音色的每一句都拿它当 `ref_audio`。
# 生成式模型（seed-audio）不逐句/逐段漂移就靠这一条，逐镜 TTS 与整章音频剧本共用。
# 本句 57 字，实测合成出 10.8~11.9s（4.8~5.3 字/秒，随音色语速浮动），单条上限 15s。
# 样本时长影响跟随幅度但不是决定项：4.3s 短句只到声区跟随（锚 91Hz → 成片 153Hz），
# 而同一把 195Hz 男声 10.8s 未裁样本实测 108Hz、7.4s 裁剪样本实测 167Hz——锚定音
# 本身落在哪个声区比它有多长更要紧（详 docs/agents/voice-anchor.md）。
# 多锚定按总时长预算自动均分裁剪（_fit_anchor）。文本保持角色无关的中性内容
ANCHOR_TEXT = ("你好，这是本角色的声音，用于全程锁定音色，请保持一致。"
               "说话的节奏可快可慢，情绪可高可低，但嗓音与音高始终是这一把。")
MAX_ANCHOR_REFS = 3        # 接口上限：一次最多三条参考音频，对应 @音频1..3


def _voices_dir(pdir, *, create: bool) -> Path:
    """项目级 `assets/voices` 的**唯一**拼法。章节侧与选角侧各拼一份的话，
    选角期预热出来的缓存与真发时读取的就不是同一个文件——页面上试听到的那条
    根本没被发出去，而这正是锚定要根治的问题。"""
    d = Path(pdir) / "assets" / "voices"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def voice_ref_dir(project, *, create: bool = True) -> Path:
    """章节侧入口：属于某项目时用项目级 assets/voices（全系列共用同一把声音），
    否则用章节工作目录 voice_refs。
    Studio 只读侧传 `create=False`——扫描绝不在盘上留目录。"""
    cf = project.path
    if cf.parent.name == "chapters":
        return _voices_dir(cf.parent.parent, create=create)
    d = project.workdir / "voice_refs"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def series_ref_dir(series, *, create: bool = True) -> Path:
    """选角侧入口：与 `voice_ref_dir` 对本项目任一章节算出的是同一个目录。"""
    return _voices_dir(series.dir, create=create)


def anchor_ref_path(ref_dir, voice_type: str) -> Path:
    """官方音色锚定参考音的缓存文件路径——发送侧与页面试听共用的**同一条**命名。

    文件名 = 清洗名 + 内容哈希短尾：只做字符清洗时 `a-b` 与 `a.b`（或纯中文
    音色名）会归并到同一路径，后到的音色静默复用先到者的参考音；命名各拼一份，
    页面给出的试听就可能不是实发那条。

    哈希连同 `ANCHOR_TEXT` 一起算：缓存按音色键命中而参考音的实质是"这把声音
    念这段文本"，锚定文本换版后仅按音色命中会把旧文本的短样本继续发出去——
    音色跟随幅度由样本时长决定，短样本直接把贴合档位拉低。文本进指纹后，
    换版自动落到新文件，旧缓存自然失配。"""
    sig = hashlib.sha256(
        (voice_type + "\n" + ANCHOR_TEXT).encode("utf-8")).hexdigest()[:8]
    return Path(ref_dir) / (re.sub(r"[^A-Za-z0-9_]+", "_", voice_type) + f"_{sig}.mp3")


def anchor_budget_cap(budget: float, n: int) -> float:
    """多锚定的单条时长上限：总预算均分并留 0.2s 余量（provider 按合计秒数拒单，
    压线即 400）。dry-run 注记与真发裁剪必须同用本口径，页面标的时长才是实发的。"""
    return max(2.0, round(budget / n - 0.2, 1))


# ---------------------------------------------------------------------------
# 镜级音色解析（从 cli.stage_tts 下沉，独立可测）——
# 这是决定"每一句计费合成用哪把声音"的策略，回归由 tests/test_prompts.py 守护。
# ---------------------------------------------------------------------------
def resolve_shot_voice(project, store, shot: dict, default_ref: str | None):
    """镜级音色解析。优先级：`shots[].voice`（显式）> 角色音色表 `voices[speaker]`
    > default_ref（旁白锁/profile 默认/项目 voice_id，由调用方拼好）。
    返回 (voice_ref 别名或原值, voice_type 解析后的音色 ID)。

    多角色镜请走 `resolve_line_voice`——本函数是它的单段特例（`shot_lines` 回落态）。"""
    spk = shot.get("speaker")
    ref = shot.get("voice") or (project.voices.get(spk) if spk else None) or default_ref
    return ref, store.resolve_voice(ref)


# ---------------------------------------------------------------------------
# 镜内多段台词（`shots[].lines[]`）—— 一个镜头承载多句、逐句换声音
#
# 一个镜若在链路上是原子的（每镜一次 synthesize、一个时间区间、一条字幕），
# 同镜两人对白就只能用一把声音念完。拆成多个镜会让画面数与计费秒数翻倍、
# 同机位的来回对白也被硬切开，故改为让一个镜承载一串句子：画面仍是一张图/一段
# 视频，音轨与字幕逐句走。
#
# 落位纪律：
#   · `shot_lines` 是「镜 → 句序列」的唯一入口，TTS/时长/字幕/lint 全走它；
#     没写 lines 的镜回落成 narration 单段。
#   · 整镜 wav（shot_<id>.wav）仍是唯一对外产物，分句 wav 只是中间物——
#     review/versioning 与 dubbed 的 ref_audio/`request_seconds` 不受影响。
# ---------------------------------------------------------------------------
def line_text(d: dict) -> str:
    """一段台词的有效文本（`text` 与 `narration` 两种键都认）。公开导出：
    `cli.stage_tts` 的 dur 回填要按同一份判据挑「有台词的段」——各自另写一份
    过滤的话，`zip` 会静默截断错位，形态是音轨全对、字幕换人时间全错。"""
    return str(d.get("text") or d.get("narration") or "").strip()


# 语音标签白名单：`<cot text=情绪>…</cot>`（单句内情绪标签，storyboard.md
# 明文教写）与常见 SSML 标记。**只脱标签、留内容**，且刻意不做通配 `<[^>]+>`——
# 台词里出现「体温<36」这类半角尖括号不该被吃掉。
_VOICE_TAG_RE = re.compile(
    r"</?(?:cot|break|emphasis|prosody|say-as|phoneme|speak|sub|voice|lang|mark)"
    r"\b[^>]*/?>", re.IGNORECASE)


def strip_voice_tags(text: str) -> str:
    """剥离台词里的语音标签，取出真正会被念出来的那部分（不改原文，纯派生）。

    `<cot>` 是**给 TTS 的**单句内情绪控制标签，逐字进字幕会把
    `<cot text=急促>快跑</cot>` 原样烧进画面。字幕真源是 narration（音字一致铁律），
    所以清洗只能发生在取字幕文本这一步，绝不能反向去改写 narration。
    `line_spans` 的字数权重同样按剥离后的文本算：标签不占念白时间，
    按原文长度分会让「一个字加一串标签」的句子吃掉整个窗口。

    **必须无条件 `.strip()`**：取字幕文本处只经过本函数，不另做
    `(... or "").strip()`。快路径上少这一下，纯空白台词就会从「跳过该镜」
    变成「渲染一条空 Dialogue 事件」，首尾空格也会被当成字幕内容排进版面。"""
    if not text:
        return ""
    if "<" not in text:
        return text.strip()
    out = _VOICE_TAG_RE.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def shot_lines(shot: dict) -> list[dict]:
    """镜 → 台词句序列（**全链路单一真源**）。

    写了 `shots[].lines[]` 就逐句返回；没写则把 `narration` 包成单段——于是下游
    只需要认识「句」这一种粒度，不必到处写 `if lines else narration` 的分叉。

    每段归一化为 `{i, speaker, text, text_en, voice, emotion, delivery}`：
      · `i`        句序号（从 0），分句 wav 命名与字幕定位用；
      · `speaker`  说话人（缺省继承镜级 `speaker`——「旁白说完角色接一句」的常见写法里
                   只有角色那句需要点名）；
      · `voice`    该句显式音色（优先于 `voices[speaker]`）；
      · `emotion`  该句情绪（缺省继承镜级 `emotion`——整镜同一情绪时不必逐句抄）。
    空文本段直接丢弃（作者留的空行不该变成一次计费合成）。

    归一化后的「句」与「镜」**同形**（emotion/emotion_scale/voice_instruction/delivery
    四件套齐备），所以 `shot_expressive_params` / `delivery_instruction` 可以直接吃
    一个句 dict——表现力那套逻辑绝不为多角色再写第二份。"""
    raw = shot.get("lines")
    if not isinstance(raw, list) or not raw:
        text = str(shot.get("narration") or "").strip()
        if not text:
            return []
        return [{"i": 0, "speaker": shot.get("speaker"), "text": text,
                 "text_en": str(shot.get("narration_en") or "").strip() or None,
                 "voice": shot.get("voice"), "emotion": shot.get("emotion"),
                 "emotion_scale": shot.get("emotion_scale"),
                 "voice_instruction": shot.get("voice_instruction"),
                 "delivery": _delivery(shot), "dur": shot.get("dur")}]
    out: list[dict] = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        text = line_text(d)
        if not text:
            continue
        out.append({
            "i": len(out),
            # 逐句缺省继承镜级：多数镜是「一个说话人 + 偶尔插一句旁白」，
            # 逼每句都抄一遍 speaker/emotion 只会抄错
            "speaker": d.get("speaker") if d.get("speaker") is not None else shot.get("speaker"),
            "text": text,
            "text_en": str(d.get("text_en") or "").strip() or None,
            # 音色只在**本句没点名别人**时才继承镜级：句写了 speaker 就意味着
            # 「这句是另一个人说的」，镜级 voice 是上一个人的声音，继承过来会把
            # 音色表里那个人的声音整个盖掉（守卫 test_line_voice_priority_…）
            "voice": (d.get("voice") if d.get("voice") is not None
                      else (None if d.get("speaker") is not None else shot.get("voice"))),
            "emotion": d.get("emotion") if d.get("emotion") is not None else shot.get("emotion"),
            "emotion_scale": (d.get("emotion_scale") if d.get("emotion_scale") is not None
                              else shot.get("emotion_scale")),
            "voice_instruction": (d.get("voice_instruction")
                                  if d.get("voice_instruction") is not None
                                  else shot.get("voice_instruction")),
            "delivery": d.get("delivery") if isinstance(d.get("delivery"), dict) else {},
            # [engine-managed] 逐句实测窗口秒数（tts 回填）——字幕逐句切换的时间来源，
            # 原样透传：归一化只补默认值，绝不发明引擎侧的事实
            "dur": d.get("dur"),
        })
    return out


NARRATOR_NAMES = ("旁白", "narrator", "voiceover", "vo", "画外音")

# 旁白在提示词/绑定句里的展示名——视频模型按这个名字理解"这句不做口型"
NARRATOR_DISPLAY = "画外旁白"


def is_narrator(speaker) -> bool:
    """这句是不是旁白（**单一真源**，全链八处消费点共用）。

    判据两半缺一不可：点了旁白别名（大小写不敏感——英文别名写成 `VO`/`Narrator`
    是常见写法），或**没点名**（`speaker` 空在本仓恒等于旁白，角色句必须具名，
    `generic_name` 维度专门在拦无主语写法）。

    各写一份就会分叉：漏后半会让提示词把作者漏填的旁白句编成没有主语的
    「说：“…”」并要求为一句第三人称叙述配口型；漏 `.lower()` 会让 lint 把
    `speaker: "VO"` 的旁白镜报成角色对白，两条结论指向相反的处置。"""
    spk = str(speaker or "").strip()
    return not spk or spk.lower() in NARRATOR_NAMES


def line_spans(lines: list[dict], total: float, *, start: float = 0.0
               ) -> list[tuple[dict, float, float]]:
    """把一段时间窗按各句字数比例切给逐句 → `[(句, 起, 止), …]`。

    按字数分而不是均分：一句三个字、一句三十个字的镜，均分会让短句拖着长尾、
    长句被挤在末尾追着念。确定性近似——native 台词时间轴、scored 底稿秒段
    （audioscript._line_spans 转调）与字幕落点共用这一份实现，各算一份就会出现
    「页面预览的秒段」「提示词里的秒段」「烧进画面的秒段」三者对不上。

    权重按 `strip_voice_tags` 剥离后的文本算：`<cot text=急促>快跑</cot>` 只念两个字，
    按原文 20 个字符分会让它吃掉大半个窗口。"""
    if not lines or not total or total <= 0:
        return []
    weights = [max(1, len(strip_voice_tags(str(ln.get("text") or ""))))
               for ln in lines]
    weight_sum = sum(weights)
    out, t = [], float(start)
    for ln, w in zip(lines, weights):
        span = total * w / weight_sum
        out.append((ln, round(t, 2), round(t + span, 2)))
        t += span
    return out


def voice_anchor_plan(project, store, shot: dict, *, max_refs: int = MAX_ANCHOR_REFS
                      ) -> dict:
    """native 镜的音色锚定计划：谁能带参考音、谁只能任模型自选 →
    `{"anchored": [{who, voice_type, no}], "loose": [名字], "over": [名字]}`。

    `loose` 与 `over` 是两种成因，处置也不同：`loose` 是**没选角**，去选一把就解决；
    `over` 是选了角但参考位不够（接口按条数限额），再选角也没用，只能减少本镜的
    说话人或换限额更高的档。合成一个键就会把「参考位已满」报成「未选角」，
    把人指向一个改变不了任何事的动作。

    **纯判定、零副作用**（与 `audioscript.anchor_plan` 同制度）：dry-run 报价、
    Studio 逐镜标注与真发共用这一份，页面显示才不会与实发对不上。差异只有一条：
    这里**只认显式选角**——角色句走 `lines[].voice` > `voices[speaker]` >
    `shots[].voice`（`resolve_line_voice` 传 default_ref=None），旁白句额外认
    `narrator_voice`（旁白锁是试音选定的显式决定，与角色选角同权）；profile
    缺省音色是"没得选时的兜底"，不是用户的选角决定，按它锚定等于把兜底声
    钉进片子还不告诉人。

    条目只到 `voice_type` 为止，**不含参考音路径**——"这把声音的参考音在哪"归
    `voicebank.anchor_clip_for`（依赖方向：voicebank → voicecast 单向，这里反向
    取档案就是循环引用），发送侧与页面各自去填。同一把声音只占一条参考位
    （`no` 共享），与接口"按条数限额"对齐。"""
    anchored, loose, over = [], [], []
    nos: dict[str, int] = {}
    narrator_ref = project.data.get("narrator_voice")
    for ln in shot_lines(shot):
        who = (NARRATOR_DISPLAY if is_narrator(ln.get("speaker"))
               else str(ln.get("speaker")).strip())
        if any(a["who"] == who for a in anchored) or who in loose or who in over:
            continue
        _ref, vt = resolve_line_voice(
            project, store, shot, ln,
            narrator_ref if who == NARRATOR_DISPLAY else None)
        if not vt:
            loose.append(who)
            continue
        no = nos.get(vt)
        if no is None:
            if len(nos) >= max_refs:
                over.append(who)
                continue
            no = len(nos) + 1
            nos[vt] = no
        anchored.append({"who": who, "voice_type": vt, "no": no})
    return {"anchored": anchored, "loose": loose, "over": over}


def voice_kind(shot: dict) -> str:
    """本镜声音形态：dialogue（有具体角色开口）/ voiceover（旁白讲述）/ silent。

    **单一真源**：口径走 `shot_lines`（lines[] 逐句可见；narration 单段包装），
    与 TTS 链同源；caption 是无声镜的补位字幕，不算人声。

    住在 voicecast 而不是 lint 里，是因为消费方不止一个：`variation._lint_voiceover`
    按它判语态，`pipeline/prompts.video_prompt` 按它决定 native 镜追加的是「对口型」
    还是「画外旁白·口唇闭合」。两处各写一份判据就会分叉成「lint 说这是旁白、
    提示词却让角色对口型」，模型被要求为一句第三人称叙述配口型。"""
    lns = shot_lines(shot)
    if not lns:
        return "silent"
    for ln in lns:
        if not is_narrator(ln.get("speaker")):
            return "dialogue"
    return "voiceover"


def burn_muted(shot: dict) -> bool:
    """混烧章内本镜是否按闭声出演编译（人声由烧录轨承担）。

    声源按镜分治的**唯一判据**：旁白/无词镜闭声出演、TTS 上主轨；对白镜由模型
    原生发声、锚定照常附发——同一章内角色恒是模型声、旁白恒是 TTS，说话人级
    单声源。提示词编译（native_mute）、锚定附发（`cli._anchor_plan_for` 与
    scanner 逐镜标注）三处共用本判据，各写一份就会出现「页面标了锚定、
    实发却按闭声编译」的分叉。"""
    return voice_kind(shot) != "dialogue"


def in_narration_track(shot: dict, motion: str) -> bool:
    """本镜的逐镜 wav 是否进旁白轨（**单一真源**）。

    native 的对白镜由模型原生发声，其 TTS 即便在盘（早年整章合成留下的）也不
    接入——接入即同一句两个人声，且两条时间轴不同源。合成侧（`cli.stage_tts`
    不给这些镜合成）、拼接（`narration_parts` 恒插等长静音）与超窗点名
    （`fit_overruns`）共用本判据：各写一份就会出现「不进轨的 wav 被点名说
    会被压快」这类自相矛盾的告警。"""
    return motion != "native" or voice_kind(shot) != "dialogue"


def default_voice_ref(data: dict, tts_params: dict | None):
    """没有点名音色的句子最终用哪把声音：旁白锁（`narrator_voice`）> profile 的
    tts 缺省 > 项目 `audio.voice_id`。合成、音频剧本锚定与 Studio 锚定标注共用。"""
    return (data.get("narrator_voice") or (tts_params or {}).get("voice")
            or (data.get("audio") or {}).get("voice_id"))


def narration_shot(shot: dict, motion: str) -> bool:
    """本镜是否要有一条烧录旁白 wav：有词且进旁白轨。

    合成前审阅闸、Studio 配音进度分母与 `stage_tts` 的合成对象三处同问这一句；
    章级「要不要旁白轨」另由 `Project.needs_narration_track` 回答。"""
    return bool(shot_text(shot) and in_narration_track(shot, motion))


def has_audio_stage(shot: dict, project) -> bool:
    """本镜有没有 audio 阶段产物：本章要产旁白轨，且本镜有词进旁白轨。
    审阅看板与 Gateway 的审阅统计共用。"""
    return project.needs_narration_track and narration_shot(shot, project.motion)


def shot_text(shot: dict) -> str:
    """本镜的完整台词文本（多段以空格连接）——「这镜有没有话要说」的统一判据。

    读 narration 的老代码换用它即可同时认识 lines[]；**绝不反向回写 narration**
    （那是作者字段，引擎回写会与人工编辑打架）。"""
    return " ".join(ln["text"] for ln in shot_lines(shot)).strip()


def is_multi_voice(project, store, shot: dict, default_ref: str | None = None) -> bool:
    """本镜是否真的用到两把以上的声音（用于 lint / Studio 标注）。
    只有一句、或多句但解析到同一把声音的，都不算——「两个人对话」的判据是
    **声音真的不同**，不是「写了几段」。"""
    seen = {resolve_line_voice(project, store, shot, ln, default_ref)[1]
            for ln in shot_lines(shot)}
    return len(seen) > 1


def resolve_line_voice(project, store, shot: dict, line: dict, default_ref: str | None):
    """句级音色解析。优先级：`lines[].voice` > `voices[lines[].speaker]` >
    `shots[].voice` > `voices[shots[].speaker]` > default_ref。

    前两档由 `shot_lines` 的继承规则铺平（句没写就已填成镜级的值），所以这里
    与 `resolve_shot_voice` 是同一套判据、同一个 `store.resolve_voice` 出口——
    两条链绝不能各写一份，否则「试音选定的音色」在多角色镜上会悄悄失效。"""
    spk = line.get("speaker")
    ref = line.get("voice") or (project.voices.get(spk) if spk else None) or default_ref
    return ref, store.resolve_voice(ref)


def line_pauses(line: dict, motion: str) -> tuple[float, float]:
    """句级前后停顿——**与镜级 `shot_pauses` 同一道模式门控**（仅 kenburns）。

    句间停顿在对白里是「换气」不是「留白」，听感上很想要；但它同样会把拼出来的
    整镜 wav 撑长 → 撑长 dur → dubbed/native 按秒向 Seedance 计费（ceil 取整），
    等于每段对白都无效购买一截无声。既有纪律怎么管镜级停顿，句级就怎么管，
    不给新的计费陷阱开口子。"""
    if motion != "kenburns":
        return 0.0, 0.0
    d = line.get("delivery") if isinstance(line.get("delivery"), dict) else {}
    pb = min(max(_finite(d.get("pause_before")), 0.0), MAX_PAUSE)
    pa = min(max(_finite(d.get("pause_after")), 0.0), MAX_PAUSE)
    return round(pb, 2), round(pa, 2)


def line_wav(shot: dict, line: dict, adir) -> Path:
    """分句 wav 的落位：`<audio>/shot_<镜>_L<句>.wav`。

    **中间物**——整镜 wav（`shot_<id>.wav`）才是对外产物（review 表态、版本栈、
    dubbed 的 ref_audio、`request_seconds` 全认它）。单段镜不额外落分句文件。"""
    return Path(adir) / f"shot_{shot.get('id')}_L{line['i']}.wav"


def shot_expressive_params(shot: dict) -> dict:
    """逐镜表现力参数：emotion / emotion_scale(1~5) → `audio_params.emotion`。

    **模版生成（官方固定音色）只有 emotion 这一条通道**，且对多情感角色扮演
    ICL 音色直接生效（无需切模型），故不门控、恒发。

    `voice_instruction` + `delivery.emphasis/note` 走的是另一条通道，由
    `delivery_instruction()` 编译——**模版生成不消费它**（官方标准版会静默过滤，
    发了等于给请求体凭空加一段噪音）。那条通道归「定制生成」：它是自然语言
    prompt 驱动的，表演提示本来就该写进 prompt 正文，编译器在此备着。"""
    extra: dict = {}
    if shot.get("emotion"):
        extra["emotion"] = shot["emotion"]
        if shot.get("emotion_scale") is not None:
            extra["emotion_scale"] = shot["emotion_scale"]
    return extra


# ---------------------------------------------------------------------------
# 配音表现力契约 delivery{emphasis, pause_before, pause_after, note}
# ---------------------------------------------------------------------------
# 单侧停顿上限（秒）：手滑写 60 会把整片时间轴撑爆，且这段无声要按 dur 进合成。
MAX_PAUSE = 5.0
# 句尾处理。拼轨时每段真实配音收 TAIL_FADE 淡出，段间接缝不留数字硬切；kenburns
# 再给每镜尾部至少 TAIL_ROLL 留白，画面切换不压在末音节上。尾留白与作者停顿同受
# `shot_pauses` 的模式门控：dubbed/native 的窗口按秒计费，不折进去。
TAIL_FADE = 0.07
TAIL_ROLL = 0.25
# 重读词条数上限：指令是一句自然语言，堆二十个词等于没重点（且徒增 token）。
MAX_EMPHASIS = 8


def _finite(x, default: float = 0.0) -> float:
    """数值有限性守卫：NaN/Infinity 不是合法 JSON，落进 `shots[].dur` 会让
    project.json 无法被浏览器 JSON.parse、Studio 整页崩。非数一律回落默认值。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _delivery(shot: dict) -> dict:
    d = shot.get("delivery") if isinstance(shot, dict) else None
    return d if isinstance(d, dict) else {}


def declared_pauses(shot: dict) -> tuple[float, float]:
    """本镜**声明**的前后停顿（秒，已做有限性守卫 + 0~MAX_PAUSE 钳制）。
    这是作者写了什么，不代表生效——生效与否见 `shot_pauses()` 的模式门控。"""
    d = _delivery(shot)
    pb = min(max(_finite(d.get("pause_before")), 0.0), MAX_PAUSE)
    pa = min(max(_finite(d.get("pause_after")), 0.0), MAX_PAUSE)
    return round(pb, 2), round(pa, 2)


def shot_pauses(shot: dict, motion: str) -> tuple[float, float]:
    """本镜**生效**的前后停顿（秒）——**只有本地渲染模式（kenburns）成立**，
    dubbed/native 恒 (0, 0)。kenburns 下 `pause_after` 至少 `TAIL_ROLL`（尾留白），
    作者写得更长照写。

    为什么必须门控：dubbed/native 下 gen-video 的请求时长取自 `shots[].dur`，
    Seedance 按秒计费（ceil 取整），而喂给它对口型的 `ref_audio`
    里根本没有这段停顿——把停顿折进 dur 等于每个带 pause 的镜无效购买 1~2 秒无声空转。
    kenburns 是本地渲染、时长本就由我们定，停顿既进音轨也进画面，零额外成本。"""
    if motion != "kenburns":
        return 0.0, 0.0
    pb, pa = declared_pauses(shot)
    return pb, max(pa, TAIL_ROLL)


def shot_duration(shot: dict, speech_dur: float, motion: str) -> float:
    """镜时长 = 配音实测时长 + 生效停顿。**必须从 probe 重算、绝不在旧 dur 上累加**——
    每跑一次 tts 都会刷新 dur（`cli.stage_tts` 的回填在「是否重新合成」判断之外），
    累加式写法会让停顿每跑一次就多叠一遍，时间轴自此单调发散。"""
    pb, pa = shot_pauses(shot, motion)
    return round(_finite(speech_dur) + pb + pa, 2)


def dubbed_sync_offset(shot: dict, wav, clip, win: float) -> float:
    """dubbed 对白镜的配音平移量（秒）＝底片开口时点 − 配音语音起点。

    参考媒体模式下模型把开口安排在动作设计允许的时点，不承诺从窗口起点说话
    （"先回头再开口"的镜，嘴比窗口起点晚整秒）；把 wav 钉在窗口起点烧录就会
    声先于嘴。底片声轨里模型重演的人声不进成片，但它是"嘴什么时候动"唯一的
    实测记录——取两侧首个语音段起点之差即得平移量。

    只对白镜适用：旁白/静音镜按闭唇出片，无口型可对；超窗镜走变速贴合，时间
    轴已被压缩。任一侧探测不出（无音轨/环境声压过人声频带/片段未落地）回 0，
    按原位烧录。正值=配音后移（垫头部静音），负值=配音前移（裁等量头部静音，
    钳制保证裁的只是语音起点之前的静音）。烧录与字幕落点共用本函数，各算一份
    就会出现"声音贴了嘴、字幕还留在原位"。"""
    r = dubbed_sync_report(shot, wav, clip, win)
    return r["sync"] if r else 0.0


def dubbed_sync_report(shot: dict, wav, clip, win: float) -> dict | None:
    """开口对齐的完整测量 →（不适用/探测不出时 None）：
      · sync   —— 钳制后的平移量（`dubbed_sync_offset` 的返回值）；
      · gap    —— 钳制没吸收掉的开口残差（平移到窗口边界仍差的秒数）；
      · mouth / speech —— 底片口型与台词各自的语音净长。两者失配说明模型没把
        整句演完（4.9s 台词只配了 2.1s 口型）——这种镜平移到哪都对不上，
        只能 lipsync 重画口型或 retake。残差与失配都要点名，静默出片会被当成
        "已对齐"。"""
    if voice_kind(shot) != "dialogue":
        return None
    if not (wav and clip and Path(str(clip)).is_file()):
        return None
    from .pipeline import speech as speech_mod
    speech_dur = probe_duration(wav)
    if win <= 0 or speech_dur <= 0 or speech_dur > win + 0.05:
        return None
    wsp = speech_mod.speech_windows(str(wav), speech_dur, clean=True)
    csp = speech_mod.speech_windows(str(clip), win)
    if not wsp or not csp:
        return None
    want = csp[0][0] - wsp[0][0]
    p = max(-wsp[0][0], min(want, win - speech_dur))
    if abs(p) < 0.1:
        p = 0.0
    return {"sync": round(p, 3), "gap": round(want - p, 3),
            "mouth": round(sum(b - a for a, b in csp), 2),
            "speech": round(sum(b - a for a, b in wsp), 2)}


SYNC_GAP_WARN = 0.3        # 平移到窗口边界后开口仍差这么多秒即点名
MOUTH_MISMATCH = 0.3       # 口型净长与台词净长相差三成即判"没演完整句"


def dubbed_sync_note(report: dict | None) -> str:
    """残差点名判词（空串=无需点名）。烧录告警与 lipsync 跳过提示共用——
    判据或措辞各写一份，两处就会一处报一处不报。"""
    if not report:
        return ""
    bits = []
    if abs(report["gap"]) > SYNC_GAP_WARN:
        bits.append(f"开口仍差 {report['gap']:+.2f}s（平移已到窗口边界）")
    sp = report["speech"]
    if sp > 0 and abs(report["mouth"] - sp) > MOUTH_MISMATCH * sp:
        bits.append(f"底片口型 {report['mouth']:.1f}s ≠ 台词 {sp:.1f}s")
    return "、".join(bits)


def shot_audio_path(shot: dict, adir=None) -> Path | None:
    """本镜逐镜配音 wav 的落位（在盘才返回，否则 None）。

    解析规则与 `narration_parts` 同源：`shots[].audio_file` 优先，回落
    `<audio>/shot_<id>.wav` 约定路径。两处一旦分叉，读侧就会看不见写侧的产物。
    字段值是 OSS URL（已上云）时 `is_file()` 为假，自然回落约定路径。"""
    for cand in (shot.get("audio_file"),
                 (Path(adir) / f"shot_{shot.get('id')}.wav") if adir is not None else None):
        if not cand:
            continue
        p = Path(str(cand))
        if p.is_file():
            return p
    return None


def request_seconds(shot: dict, motion: str, *, adir=None,
                    speech_dur: float | None = None) -> float:
    """向视频 provider 请求的**画面秒数**——`gen-video` 读侧的**单一真源**
    （`--dry-run` 报价与真发共用；这条口径一分叉，报的价就不是烧的钱）。

    **为什么不能直接用 `shots[].dur`**：dur 是持久化字段，kenburns 下 `stage_tts`
    会把 `delivery.pause_*` 折进去（见 `shot_duration`）。而本工程主推的节点顺序
    恰恰是「先 kenburns 出样片过节奏审 → 再 gen-video --dubbed 动态化」——切模式
    那一刻盘上的 dur 已经含停顿，照发就是按含停顿的秒数向 Seedance 计费
    （ceil 取整），而喂进去对口型的 `ref_audio` 里根本没有这段无声：
    成片里多出等长的静默死区，随后回填「买下的整秒」还把死区固化成正式时长、
    字幕窗口一起被拉长。写侧的 `shot_pauses` 只管得住「写 dur 那一刻」，
    读侧必须有这道对称的闸。

    口径（两模式统一，画面秒数由**设计 dur** 说了算）：
      · dubbed/native 的 dur 是场→镜设计出的表演窗口，台词只占窗口一段
        （长镜里说话前后都是表演）；配音实测只在两种情形改写窗口——
        ① dur 里折过 kenburns 时代的声明停顿（按「配音+停顿」反查对上才认定）
        → 扣回净配音；② dubbed 配音比 dur 还长 → 窗口必须罩住整句
        （对口型素材不能被截断）。
      · wav 不在盘 → **dur 原样**。没跑过 tts ⇒ 停顿从未折进 dur，此时再扣
        声明停顿只会让镜每重跑一次就短一截（非幂等）。

    返回**净画面秒数**、不做档位取整——各厂档位统一由 `provider.billable_seconds`
    钳制（seedance 4~15 整秒 / veo 4|6|8），避免第二份取整逻辑。
    `speech_dur` 显式传入时跳过探测（调用方已量过 / 测试注入）。"""
    dur = _finite(shot.get("dur"))
    if motion == "kenburns":   # 防御：本地渲染模式不烧图生视频，真调到了就按盘上 dur 原样
        return dur
    if motion == "native":
        # **native 的画面秒数由 dur 说了算**：模型原生配音，我们的 TTS 只是混烧的
        # 叠加轨（详 `docs/agents/native-voiceover.md`），拿配音长度去请求 Seedance
        # 就是让「台词多长」决定「画面多长」——画面 5s 配音 10s 的镜照发即多花
        # 一倍的钱，生成的片段节奏也与分镜设计完全不符。
        # 唯一例外是**从 kenburns 切来**的历史 dur（那时 tts 会把停顿折进去）：
        # 按「配音 + 声明停顿」反查能对上才认定确实折过，扣回净配音秒数。
        pb, pa = declared_pauses(shot)
        if pb or pa:
            if speech_dur is None:
                wav = shot_audio_path(shot, adir)
                if wav is not None:
                    try:
                        speech_dur = probe_duration(wav)
                    except FFmpegError:
                        speech_dur = None
            sp = _finite(speech_dur)
            if sp > 0 and abs(dur - (sp + pb + pa)) < 0.15:
                return round(sp, 2)
        return dur
    if speech_dur is None:
        wav = shot_audio_path(shot, adir)
        if wav is not None:
            try:
                speech_dur = probe_duration(wav)
            except FFmpegError:   # 半截/损坏的 wav 不该让整轮渲染炸掉，退回 dur
                speech_dur = None
    speech = _finite(speech_dur)
    if speech <= 0:
        return dur
    # dubbed：设计窗口权威。kenburns 折算残留按停顿反查扣回净配音（与 native
    # 同一判据）；其余情形窗口取 max(dur, 配音)——配音超窗必须罩住整句，
    # 配音短于窗则余量是表演时间（发送侧把 ref_audio 垫到窗口）
    pb, pa = declared_pauses(shot)
    if (pb or pa) and abs(dur - (speech + pb + pa)) < 0.15:
        return round(speech, 2)
    return round(max(dur, speech), 2)


def _emphasis_words(raw) -> list[str]:
    """重读词归一：字符串按中英文顿号/逗号/斜杠切；列表逐项取。去重保序、限长。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[、,，/|]", str(raw))
    out: list[str] = []
    for x in items:
        w = str(x).strip()
        if w and w not in out:
            out.append(w)
    return out[:MAX_EMPHASIS]


def delivery_instruction(shot: dict) -> str:
    """把镜级表现力标注编译成**一句自然语言语音指令**（派生纯函数）。

    合成顺序：`voice_instruction`（整体语气）→ `delivery.emphasis`（重读词）→
    `delivery.note`（补充表演提示），用「；」连成一句。

    **铁律：编译结果只喂 provider，绝不回写 `shots[].narration`。**
    往台词里塞 `<cot>`/SSML/多余逗号会同时炸三处：① `subtitle.pick_texts` 逐字取
    narration → 标签被烧进画面字幕；② 官方音色不开标签解析 → 极可能把标签念出来；
    ③ seed-tts 按 `len(text)` 计费 → 标签白进字数。"""
    bits: list[str] = []
    base = str(shot.get("voice_instruction") or "").strip()
    if base:
        bits.append(base)
    words = _emphasis_words(_delivery(shot).get("emphasis"))
    if words:
        bits.append("重读" + "".join(f"「{w}」" for w in words))
    note = str(_delivery(shot).get("note") or "").strip()
    if note:
        bits.append(note)
    return "；".join(bits)


# ---------------------------------------------------------------------------
# 全片旁白轨拼接序列（**单一真源**）——tts 与 compose 自愈重拼共用
# ---------------------------------------------------------------------------
def narration_parts(project, adir) -> tuple[list[tuple[str, object]], list[dict], list]:
    """按有效镜序产出全片旁白轨的拼接序列，返回 `(parts, segments, missing)`。

    · `parts`  —— `("file", wav)` 真实配音 ｜ `("silence", 秒)` 静音段 ｜
      `("cut", (wav, 头部秒))` 裁头接入（dubbed 开口对齐的前移形态）。静音有三种：
      无旁白「纯画面镜」的等长占位（否则其后所有镜的语音整体前移）、有旁白镜的
      `delivery.pause_before/after` 停顿垫片（仅 kenburns，见 `shot_pauses`）、
      dubbed 开口对齐的后移垫片与齐窗尾垫。
    · `segments` —— timestamps.json 的逐镜起止，**窗口口径（含本镜停顿）**：
      start=本镜窗口起点、end=窗口终点，与 parts 走同一条 offset 累加，
      绝不另写第二份 offset 逻辑。kenburns 下窗口长度恒等于 `shots[].dur`，
      于是 timestamps 就是视频时间轴在音频域的投影。
    · `missing` —— 有台词却缺逐镜 wav 的镜号（compose 自愈据此放弃并告警）。

    **为什么必须是单一真源**：`cli.stage_tts` 拼 narration.wav，
    `pipeline.compose._sync_narration` 在偏差 >0.3s 时按同一规则重拼。两边规则一旦
    分叉（典型：这边插了停顿垫片、那边没有），合成阶段会用「不含停顿」的序列把停顿
    整段抹掉，还打印「已按有效分镜自动重拼」把破坏伪装成修复——静默且不可察觉。"""
    from .review import is_omitted
    adir = Path(adir)
    motion = project.motion
    parts: list[tuple[str, object]] = []
    segments: list[dict] = []
    missing: list = []
    offset = 0.0
    for s in project.shots:
        if is_omitted(s):
            continue
        text = shot_text(s)          # 认识 lines[]：多角色镜的台词在句里，不在 narration
        if not text:
            gap = _finite(s.get("dur"))
            if gap > 0:
                parts.append(("silence", round(gap, 3)))
                offset += gap
            continue
        if not in_narration_track(s, motion):
            # native 的旁白轨只收旁白镜：对白镜由模型原生发声，其 TTS 即便在盘
            # （早年整章合成留下的）也绝不烧——烧进去就是同一句两个人声，且两条
            # 时间轴不同源。按窗口插等长静音，保持后续镜的音画对位。
            gap = _finite(s.get("dur"))
            if gap > 0:
                parts.append(("silence", round(gap, 3)))
                offset += gap
            continue
        # 候选链与逐镜读侧同一份（shot_audio_path）：audio_file 在盘用它，不在盘
        # （典型：已上云后字段是 OSS URL）回落约定路径。在这里自写 or 短路的话，
        # URL 字段会把明明在盘的 wav 判成缺失，整镜被踢出旁白轨、后续镜集体前移
        wav = shot_audio_path(s, adir)
        if wav is None:
            if motion == "native" and not project.native_voiceover:
                # 缺省不烧的 native：人声整章由模型承担，旁白镜没有 wav 是常态，
                # 按窗口占静音即可。此时报 missing 会让 compose 的自愈误判成
                # 「无从重拼」而整章放弃
                gap = _finite(s.get("dur"))
                if gap > 0:
                    parts.append(("silence", round(gap, 3)))
                    offset += gap
                continue
            # 混烧下旁白镜的人声就是这条 wav：缺了即成片那一段无人说话而字幕
            # 照烧，与 kenburns/dubbed 缺配音是同一类错误态，走同一条点名出口
            missing.append(s.get("id"))
            continue
        if motion in ("native", "dubbed"):
            # native 混烧与 dubbed 主音轨：窗口=dur（Seedance 片段实测/计费秒数，
            # **画面说了算**），配音是叠加轨、只能去适配它——kenburns 的 dur 本就
            # 由配音实测回填（窗口恒等于 wav），只有这两种模式的窗口与 wav 可分离：
            #   · 短于窗口 → 垫静音齐窗（不垫的话后续所有镜的配音整体前移）；
            #   · 长于窗口 → **变速不变调压进窗口**（`("fit", …)`）。裁词最糟、
            #     放任不管则逐镜累积成整轨漂移（几十镜可攒出近一分钟的偏差，
            #     后半段旁白与画面完全对不上还会被末尾裁掉）。压缩比超
            #     `FIT_TEMPO_WARN` 的镜由 `fit_overruns` 点名——那是台词写太满，
            #     该改词或加长镜头，引擎不替人做这个创作决定。
            speech = probe_duration(wav)
            win = _finite(s.get("dur")) or speech
            sync, note = 0.0, ""
            if speech > win + 0.05:
                parts.append(("fit", (str(wav), round(win, 3))))
            else:
                # dubbed 对白镜按底片开口时点平移（`dubbed_sync_report` 单一真源，
                # 字幕落点同用）：后移垫头部静音、前移裁头部静音，尾部补齐到窗口
                if motion == "dubbed":
                    report = dubbed_sync_report(s, wav, s.get("clip"), win)
                    sync = report["sync"] if report else 0.0
                    note = dubbed_sync_note(report)
                if sync > 0:
                    parts.append(("silence", round(sync, 3)))
                    parts.append(("file", str(wav)))
                elif sync < 0:
                    parts.append(("cut", (str(wav), round(-sync, 3))))
                else:
                    parts.append(("file", str(wav)))
                tail = win - speech - sync
                if tail > 0.01:
                    parts.append(("silence", round(tail, 3)))
            seg = {"shot_id": s.get("id"), "text": text,
                   "start": round(offset, 3), "end": round(offset + win, 3)}
            if sync:
                seg["sync"] = sync
            if note:
                seg["sync_note"] = note
            segments.append(seg)
            offset += win
            continue
        pb, pa = shot_pauses(s, motion)
        if pb > 0:
            parts.append(("silence", pb))
        parts.append(("file", str(wav)))
        if pa > 0:
            parts.append(("silence", pa))
        win = _finite(s.get("dur"))          # 窗口长度 = 配音 + 停顿（tts 已折算）
        seg = {"shot_id": s.get("id"), "text": text,
               "start": round(offset, 3), "end": round(offset + win, 3)}
        if pb or pa:                          # 停顿留痕：窗口口径的可读性全靠它
            seg["pause_before"], seg["pause_after"] = pb, pa
        segments.append(seg)
        offset += win
    return parts, segments, missing


# native 混烧的压缩比告警线：超过它就不是"贴合窗口"而是"念得像快进"——
# 1.3× 是中文语速的听感拐点（正常 5 字/秒 → 6.5 字/秒仍可懂，再快就赶）。
FIT_TEMPO_WARN = 1.3


def fit_overruns(project, adir, *, warn: float = FIT_TEMPO_WARN) -> list[tuple]:
    """native 混烧下**台词写太满**的镜：`[(镜号, 配音秒, 窗口秒, 压缩比), …]`。

    引擎只报不改词——压到窗口保证音画对位是机械活（`("fit", …)`），但"这句该
    删几个字还是该把镜头拉长"是创作决定。返回按压缩比降序，调用方点名前几镜。"""
    from .review import is_omitted
    adir = Path(adir)
    out = []
    for s in project.shots:
        if is_omitted(s) or not shot_text(s):
            continue
        if not in_narration_track(s, project.motion):
            # 不进轨的 wav 不会被压进窗口，点名它等于让人去改一句根本不会被烧的台词
            continue
        wav = shot_audio_path(s, adir)
        win = _finite(s.get("dur"))
        if not wav or win <= 0:
            continue
        speech = probe_duration(wav)
        if speech > win * warn:
            out.append((s.get("id"), round(speech, 2), round(win, 2),
                        round(speech / win, 2)))
    return sorted(out, key=lambda x: -x[3])

