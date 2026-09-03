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

"""音色档案库（Voice Bank）—— 一个实体（角色 / 旁白）用过的每一把声音都是一条档案。

与 `voicecast` 的边界：那边是**镜级配音策略**（每一句用哪把声音、时长与停顿怎么算），
跑在渲染热路径上；本模块是**编辑期的选角**（这把声音怎么来的、还留着哪几把、能不能删）。
依赖单向——本模块用 `voicecast` 解析镜与句，`voicecast` 不反向引用。

## 两级对象

**候选（audition）** 是临时展示物：一批试音音频，整批覆盖、只保留最近几批。它回答
「这几把声音里哪把合适」，不构成任何工程事实，所以刷新页面后不带任何选中状态。

**档案（cast）** 是资产：选中一条候选即立一条档案，音频复制进 `assets/voices/casts/<id>.mp3`
后**永不再写**。它回答「这把声音是什么、从哪来、还在不在用、谁在用它」。

## 三条结构性约束

1. **档案音频不可变。** 定制音色每次演绎都不同，被选中的那条音频**就是**这把音色本身
   （全片每句拿它当参考音合成）。落在按 owner 命名的固定路径上，再选一次就等于把上一把
   声音物理销毁，历史无从回听、下游产物无从溯源。

2. **定制音色的 `voice_type` 按档案唯一**（`custom:<档案号>`）。分镜留痕 `gen.audio.voice_type`
   记的就是它，这是「哪几镜用了哪一把声音」唯一可计算的依据，也是删除前那道引用闸的地基。

3. **不设「当前启用」指针。** 在用 = 实体的 `voice` 指向哪条档案（`cast_for_ref`）。模版音色
   重复选中同一把时复用既有档案而不追加，`(owner, ref)` 因此唯一可解。状态存两处必然漂移，
   这里从结构上不给漂移留位置。

## 落位

    assets/voices/casts/<档案号>.mp3                     档案音频（不可变）
    assets/voices/auditions/<实体>/preset/<批次>/…       模版候选（整批覆盖·滚动清理）
    assets/voices/auditions/<实体>/custom/<批次>/…       定制候选（同上）
"""
from __future__ import annotations

import random
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import voicecast
from .errors import FFmpegError, KinemaError
from .ffmpeg import probe_duration
from .pipeline.asr import speech_chars

NARRATOR = "旁白"           # 旁白在档案库里与角色同权，用它作 owner
DEFAULT_COUNT = 5           # 模版试音一批几条
CUSTOM_COUNT = 3            # 定制一批几条
CUSTOM_PREFIX = "custom:"
CUSTOM_PROVIDER = "doubao"  # 只有 seed-audio-1.0 吃自然语言声线描述与参考音
KEEP_BATCHES = 3            # 候选目录保留最近几批（档案音频另有副本，清理不伤它）

# 试音台词：一句自我介绍 + 一句带情绪的台词，最能听出「角色感」
AUDITION_TEXT = "你好，我是{name}。相逢即是缘分——这一路风雨，就让我陪你一起走下去吧！"
NARRATOR_TEXT = ("夜色渐深，故事从这里开始。有些相遇写在风里，有些告别藏在灯火阑珊处——"
                 "接下来的一切，请听我为你慢慢讲述。")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9一-鿿]+", "_", s or "x").strip("_") or "x"


# ---------------------------------------------------------------------------
# 档案账（读侧）——只吃文档 dict，项目文档与章节文档同形
#
# 章节要能脱离项目文档独立渲染，所以档案随建章与每次启用整份复制进章节。两侧同形
# 意味着解析函数只有一份：`clip_for` / `voice_desc` 在两种文档上行为逐字节一致。
#
# **读侧对字段形状一律不作假设**（`_obj`/`_rows`）：文档是长期演进的用户数据，盘上
# 总会有写坏的、手改过的、上一版留下的形状。读侧崩一次就是整页 500——而这一页恰恰
# 是用户唯一能看见「音色出了什么问题」的地方。判据只有一条：形状不对就当没有。
# ---------------------------------------------------------------------------
def _obj(x) -> dict:
    return x if isinstance(x, dict) else {}


def _rows(x) -> list:
    return [r for r in x if isinstance(r, dict)] if isinstance(x, list) else []


def _slot(parent: dict, key: str, blank):
    """**写侧**槽位：形状不对就地换成空壳，返回的容器保证写得进文档。

    读侧的「形状不对就当没有」返回的是游离副本，写侧照抄会静默丢写；`setdefault`
    也不行——键存在而值是 null/列表时它原样返回旧值，随后 `.get`/`.append` 当场炸。
    盘上确有这类文档：候选块 `audition` 上一版是列表，`{batch, entries}` 化之后
    旧项目一点「试音」就 `'list' object has no attribute 'get'`。
    处置与读侧同一条：旧形状按作废丢弃，绝不翻译旧字段。"""
    v = parent.get(key)
    if type(v) is not type(blank):
        parent[key] = v = blank
    return v


def casts_of(doc: dict) -> list[dict]:
    """文档里的全部音色档案（按登记顺序）。"""
    return _rows(_obj(_obj(doc).get("voice_bank")).get("casts"))


def find_cast(doc: dict, cast_id: str) -> dict | None:
    return next((c for c in casts_of(doc) if c.get("id") == cast_id), None)


def cast_ref(cast: dict) -> str | None:
    """档案 → 写进 `voices` / `narrator_voice` 的音色引用。

    模版写别名（`store.resolve_voice` 认它，且人读得懂），定制写档案唯一的 voice_type。"""
    return cast.get("alias") or cast.get("voice_type")


def cast_for_ref(doc: dict, owner: str, ref: str | None) -> dict | None:
    """某实体当前音色引用对应的档案；引用不出自档案库（手工指派的别名）时 None。"""
    if not ref:
        return None
    return next((c for c in casts_of(doc)
                 if c.get("owner") == owner
                 and ref in (c.get("voice_type"), c.get("alias"))), None)


def is_custom(voice_type: str | None) -> bool:
    return bool(voice_type) and str(voice_type).startswith(CUSTOM_PREFIX)


def cast_for_type(doc: dict, voice_type: str | None) -> dict | None:
    """定制 voice_type → 档案整条（owner/prompt/clip 都在上面）；模版/未入档 None。

    逐镜合成要同时取参考音（`clip`）与声线描述原话（`prompt`）——音色靠参考音
    锁死，气质/语速/口癖靠描述文案钉住，两者同发才是完整的一致性组合；
    分两次查询迟早有人只拿一半。"""
    if not is_custom(voice_type):
        return None
    return next((c for c in casts_of(doc) if c.get("voice_type") == voice_type), None)


def clip_for(doc: dict, voice_type: str | None) -> str | None:
    """定制音色 → 锚定参考音路径；模版音色返回 None（照旧走官方 speaker 参数）。"""
    return (cast_for_type(doc, voice_type) or {}).get("clip")


def anchor_clip_for(doc_holder, voice_type: str | None,
                    ref_dir=None) -> tuple[str | None, bool]:
    """某把声音锚定参考音的**在盘事实** → `(路径或None, 是否定制档案)`。

    定制音色取档案那条不可变音频（立档即拷贝，正常恒在）；官方音色取项目级
    锚定缓存（路径真源 `voicecast.anchor_ref_path`），还没合成过就 `(None, False)`
    ——由调用方决定是现场预热（发送侧）还是照实说「发送时现合成」（页面）。

    `ref_dir` 供选角侧（持有 Series 而非章节 Project）传 `voicecast.series_ref_dir`
    算出的同一个目录；不传则按章节文档解析。两侧算出的是同一条路径，缓存才共用。
    目录恒 `create=False`：这是读侧判定，绝不在盘上留目录。"""
    clip = clip_for(doc_holder.data, voice_type)
    if clip:
        # URL 形态（oss sync 改写过）视为在盘：本地无从 stat，取回由发送侧负责
        from .storage.media import is_url
        return (clip, True) if is_url(clip) or Path(clip).is_file() else (None, True)
    d = ref_dir if ref_dir is not None else voicecast.voice_ref_dir(
        doc_holder, create=False)
    p = voicecast.anchor_ref_path(d, voice_type)
    return (str(p) if p.is_file() else None), False


def ensure_anchor_clip(series, router, voice_type: str | None) -> str | None:
    """把这把声音的锚定参考音**落到盘上** → 路径（不适用时 None）。

    定制音色的锚定音就是档案那条不可变音频，立档即在盘，直接回它；官方模版音色
    要现合成一句 `ANCHOR_TEXT`。路径与文案取 `voicecast` 单一真源，与 `stage_tts`
    预热、`gen-video` 真发现场预热落**同一个文件**——三处任一先跑到，另外两处直接
    命中缓存，绝不会出现「页面试听的那条不是发出去的那条」。

    为什么值得在选角期就花这一句：锚定音是 native 真发时随请求附发的那条参考音，
    也是页面上「参考音频N」点开听到的东西。它不在盘上，人就只能不试听直接开生视频
    去赌音色——生视频按秒计费，赌错一次重出的钱远多于这一句 TTS。

    合成失败照实抛：调用方要么是用户点了「合成试听」（必须看见失败原因），
    要么是启用收尾（`_warm_anchor` 在那里吞掉，选角本身不受影响）。"""
    if not voice_type:
        return None
    clip = clip_for(series.data, voice_type)
    if clip:
        return clip
    ref = voicecast.anchor_ref_path(voicecast.series_ref_dir(series), voice_type)
    if ref.is_file():
        return str(ref)
    prov, _params = router.resolve("tts", series.data.get("profile"))
    res = prov.synthesize(voicecast.ANCHOR_TEXT, str(ref), voice=voice_type)
    spent = float(getattr(res, "cost", 0.0) or 0.0)
    if spent > 0:
        with series.commit():
            series.add_cost("tts", spent)
    return str(ref) if ref.is_file() else None


def _warm_anchor(series, router, voice_type: str | None) -> str | None:
    """启用收尾的锚定音预热。两条纪律：

    · **在 `commit()` 之外**——这是一次几秒的 TTS 往返，放进锁里等于拿文档锁按住
      整个合成过程，正是并发覆写守则点名的反面（耗时的生成放在 with 之外）；
    · **失败不冒泡**——选角这件事已经写盘成立，锚定音真发时还会再试一次。
    `router=None` 的调用方（纯数据路径）跳过预热，不触发任何合成。"""
    if router is None:
        return None
    try:
        return ensure_anchor_clip(series, router, voice_type)
    except Exception:  # noqa: BLE001  见上：预热失败不该把已成立的选角判成失败
        return None


def line_prompt(cast: dict, text: str, *, instruction: str | None = None,
                emotion: str | None = None) -> str:
    """定制音色**逐镜/逐句合成**的 text_prompt：声线定义行 + 引号体台词。

    与 `_custom_prompt`（定制试音）同一格式家族：seed-audio 把正文当剧本读——
    定义行钉气质、引号体才是要念的话。参考音（随请求另发）锁的是音色本身，
    这一行描述是第二道锚（气质/语速/口癖），描述与参考音同发才是完整组合，
    只发参考音时长句/极端情绪下气质仍会走样。

    `emotion`（分镜 `shots[].emotion`）与 `instruction`（`delivery_instruction`
    编译的表演提示）编进括注——官方模版音色走 `audio_params.emotion` 结构化
    通道，定制生成没有那条通道，表演提示本就该写进剧本正文（该分工在
    `voicecast.shot_expressive_params` 的注释里立过）。"""
    owner = str((cast or {}).get("owner") or "").strip() or "角色"
    desc = str((cast or {}).get("prompt") or "").strip()
    head = f"{owner} 是{desc}" if desc else ""
    hints = "，".join(x for x in ((f"带着{str(emotion).strip()}的情绪" if emotion else ""),
                                  str(instruction or "").strip()) if x)
    speak = f"{owner}{f'（{hints}）' if hints else ''}说道：“{text}”"
    return f"{head}\n\n{speak}" if head else speak


def owner_ref(doc: dict, owner: str) -> str | None:
    """某实体在这份文档里的音色引用。项目文档读实体字段，章节文档读音色表——
    两种文档的指派位置本就不同，判据收在一处免得调用方各写一份。"""
    if owner == NARRATOR:
        if _obj(doc.get("narrator")).get("voice"):
            return doc["narrator"]["voice"]
        return doc.get("narrator_voice")
    for c in _rows(doc.get("characters")):
        if c.get("name") == owner and c.get("voice"):
            return c["voice"]
    return _obj(doc.get("voices")).get(owner)


def voice_desc(doc: dict, owner: str) -> str | None:
    """某实体在用的定制音色是按哪段声线描述造出来的；模版音色或未入档返回 None。"""
    cast = cast_for_ref(doc, owner, owner_ref(doc, owner))
    return (cast or {}).get("prompt") or None


# ---------------------------------------------------------------------------
# 实体定位
# ---------------------------------------------------------------------------
def _entity(series, owner: str) -> dict:
    """实体在项目文档里的可写位置。旁白是顶层 `narrator`，角色是 `characters[]` 条目。"""
    if owner == NARRATOR:
        return _slot(series.data, "narrator", {})
    chars = _rows(series.data.get("characters"))
    c = next((x for x in chars if x.get("name") == owner), None)
    if c is None:
        have = "、".join(x.get("name") or "?" for x in chars) or "无"
        raise KinemaError(f"项目 {series.pid} 没有角色「{owner}」（现有: {have}；先 character add）")
    return c


def _bank(series) -> dict:
    bank = _slot(series.data, "voice_bank", {"seq": 0, "casts": []})
    _slot(bank, "casts", [])
    return bank


def _casts_dir(series) -> Path:
    return series.dir / "assets" / "voices" / "casts"


def _audition_dir(series, owner: str, kind: str, batch: int) -> Path:
    return (series.dir / "assets" / "voices" / "auditions"
            / _safe(owner) / kind / f"b{batch:03d}")


def _prune_batches(series, owner: str, kind: str, keep: int = KEEP_BATCHES) -> None:
    """只留最近几批候选目录。档案音频在 casts/ 另有不可变副本，清理动不到它。"""
    root = series.dir / "assets" / "voices" / "auditions" / _safe(owner) / kind
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    for d in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 候选取材：模版音色推荐池
# ---------------------------------------------------------------------------
def _gender(voice_type: str | None) -> str | None:
    vt = voice_type or ""
    if "female" in vt:
        return "female"
    if "male" in vt:
        return "male"
    return None


# 角色性别的文本推断词表（**组合词**，刻意不用单字「男/女」——「女儿失踪」会把
# 父亲判成女性；也不收「他」——「其他/他们/他人」全是假阳性；「她」几乎只指女性，
# 收。「少年/青年」性别中立不收）。实测锚点：设定卡 appearance 恒以「NN岁男性/女性」开头。
_GENDER_WORDS = {
    "female": ("女性", "女主", "少女", "女子", "女孩", "女人", "姑娘", "母亲", "妈妈",
               "奶奶", "婆婆", "姐姐", "妹妹", "阿姨", "大婶", "女帝", "女王", "公主",
               "皇后", "王妃", "她"),
    "male": ("男性", "男主", "男子", "男孩", "男人", "小伙", "父亲", "爸爸", "爷爷",
             "哥哥", "弟弟", "大叔", "老汉", "王子", "皇帝", "少爷"),
}


def _gender_from_text(text) -> str | None:
    """单段文本的确定性性别推断：恰好命中一侧词表才判，两侧都中/都不中=不判。"""
    blob = str(text or "")
    hit = {g for g, words in _GENDER_WORDS.items() if any(w in blob for w in words)}
    return hit.pop() if len(hit) == 1 else None


def character_gender(store, character: dict) -> str | None:
    """角色性别判定链（试音候选过滤的**唯一真源**，命中即停）：
      ① 显式 `characters[].gender`（male/female/男/女，`character set --gender` 写）；
      ② 现有音色 voice_type 里的性别标记（已在用男声=想要男声）；
      ③ 设定文本逐字段推断（appearance → role → desc，**逐段判不混判**——
         appearance 的「24岁男性」绝不该被 role 里提到的其他人搅浑）。
    全链不中返回 None=不过滤（非人类灵体/吉祥物本就任何音色都可试）。
    只看②的话，没选过音色的角色性别闸整个失效——男角色会刷出女配音。"""
    g = str(character.get("gender") or "").strip().lower()
    if g in ("male", "男", "男性", "m"):
        return "male"
    if g in ("female", "女", "女性", "f"):
        return "female"
    cur = character.get("voice")
    if cur:
        vg = _gender(store.resolve_voice(cur))
        if vg:
            return vg
    for field in ("appearance", "role", "desc"):
        g2 = _gender_from_text(character.get(field))
        if g2:
            return g2
    return None


def _is_roleplay(vtype: str | None) -> bool:
    """有感情·适配漫剧的**角色扮演音色**（豆包/猫箱 ICL 多情感，专为剧情对白设计）。"""
    return bool(vtype) and str(vtype).startswith("ICL_")


# 有声阅读 / 播音腔（戏剧朗读，日常对白偏**机械**）——不进默认推荐（voices.yaml ⚠ 段）。
_AVOID_FOR_DIALOGUE = frozenset({
    "擎苍", "沧桑老者", "少年梓辛", "少年", "悬疑解说", "霸气青叔",
    "儒雅青年", "儒雅旁白", "译制腔",
})

# 旁白候选池：温暖·有感情的旁白气质（磁性深情/醇厚沉稳/温柔治愈/知性）——**避开有声书播音腔**；
# 池子放宽到 ~10，每批随机取 5，每批不同又都好听。
NARRATOR_POOL = ["磁性男嗓", "醇厚低音", "温柔女神", "温柔文雅", "治愈女",
                 "云舟", "深夜播客", "渊博小叔", "精灵向导", "知性女声"]


def dialogue_pools(store, want_gender: str | None,
                   exclude: set[str] | None = None) -> tuple[list[str], list[str]]:
    """对白可用的音色别名，分 `(角色扮演 ICL, 通用口语)` 两档返回。

    过滤口径：只中文角色音、避开有声书播音腔、同一把声音的多个别名只出一次、
    性别不冲突。分档返回，试音候选按档取材。
    """
    skip = set(exclude or ())
    icl_pool, gen_pool = [], []
    seen_vt: set[str] = set()
    for alias in skip:
        vt = store.resolve_voice(alias) or alias
        seen_vt.add(str(vt))
    for alias, vtype in store.voices.items():
        if alias in skip or alias in _AVOID_FOR_DIALOGUE:
            continue
        vt = str(vtype)
        if not (vt.startswith("ICL_") or vt.startswith("zh_")):
            continue                        # 多语种/英日韩不进中文漫剧推荐
        if vt in seen_vt:
            continue
        if want_gender and _gender(vt) and _gender(vt) != want_gender:
            continue
        seen_vt.add(vt)
        (icl_pool if _is_roleplay(vt) else gen_pool).append(alias)
    return icl_pool, gen_pool


def default_candidates(store, character: dict, count: int = DEFAULT_COUNT) -> list[str]:
    """角色试音候选：**角色扮演 ICL（多情感·有感情·适配漫剧）优先**，避开播音腔的机械感；
    只中文角色音、同一把声音只出一次、同性别；角色现有音色永远排第一。
    **每次调用从合适池里随机抽**——重新试音给新的一批。指挥层按人设显式指定时不走这里。"""
    current = character.get("voice")
    out: list[str] = [current] if current else []
    icl_pool, gen_pool = dialogue_pools(store, character_gender(store, character),
                                        exclude=set(out))
    random.shuffle(icl_pool)
    random.shuffle(gen_pool)
    for alias in icl_pool + gen_pool:       # 角色扮演优先随机取，不够再随机补通用
        if len(out) >= count:
            break
        out.append(alias)
    return out[:count]


def narrator_candidates(store, series, count: int = DEFAULT_COUNT) -> list[str]:
    """旁白候选：现任旁白排第一，再从旁白气质池**随机取**（每批不同），
    不够时从音色目录随机补（避机械播音腔）。"""
    cur = owner_ref(series.data, NARRATOR)
    out = [cur] if cur and not is_custom(cur) else []
    pool = [a for a in NARRATOR_POOL if a in store.voices and a not in out]
    random.shuffle(pool)
    for alias in pool:
        if len(out) >= count:
            break
        out.append(alias)
    if len(out) < count:
        extra = [a for a in store.voices if a not in out and a not in _AVOID_FOR_DIALOGUE]
        random.shuffle(extra)
        out.extend(extra[:count - len(out)])
    return out[:count]


def _audition_line(owner: str, text: str | None) -> str:
    # 只认 {name} 一个记号：用户 --text 里可能有字面花括号，str.format 会把它
    # 当占位符解析、当场 KeyError
    return (text or (NARRATOR_TEXT if owner == NARRATOR
                     else AUDITION_TEXT)).replace("{name}", owner)


# ---------------------------------------------------------------------------
# 候选生成
#
# 合成放在 `commit()` 之外、登记放在里面：一批试音要跑十几秒，锁里做合成会让并发的
# 另一个实体串成两倍时长；而整份覆写的登记不进锁就会互相抹掉（见 Series.commit）。
# ---------------------------------------------------------------------------
def audition(store, router, series, owner: str, *,
             candidates: list[str] | None = None, text: str | None = None) -> dict:
    """模版试音：同一段台词、若干把官方音色，落一批候选。

    返回里带 `cost`（本批合计，元）。选角发生在系列层，那里没有章节台账可挂，
    所以只随命令播报——单价未配置时恒 0（`config/models.yaml` 的 tts 价位）。"""
    ent = _entity(series, owner)
    if candidates:
        aliases = [a.strip() for a in candidates if a and a.strip()]
    elif owner == NARRATOR:
        aliases = narrator_candidates(store, series)
    else:
        aliases = default_candidates(store, ent)
    if not aliases:
        raise KinemaError("没有可用的候选音色（检查 config/voices.yaml）")
    prov, _params = router.resolve("tts", series.data.get("profile"))
    batch = int(_obj(ent.get("audition")).get("batch") or 0) + 1
    adir = _audition_dir(series, owner, "preset", batch)
    adir.mkdir(parents=True, exist_ok=True)
    line = _audition_line(owner, text)
    entries, cost = [], 0.0
    for no, alias in enumerate(aliases, start=1):
        vtype = store.resolve_voice(alias)
        out = adir / f"{no}_{_safe(alias)}.mp3"
        res = prov.synthesize(line, str(out), voice=vtype)
        cost += getattr(res, "cost", 0.0) or 0.0
        entries.append({"no": no, "voice": alias, "voice_type": vtype, "path": str(out)})
    with series.commit():
        ent = _entity(series, owner)        # data 已换成磁盘最新副本，必须重新定位
        ent["audition"] = {"batch": batch, "at": _now(), "text": line, "entries": entries}
        if cost > 0:
            series.add_cost("tts", cost)
    _prune_batches(series, owner, "preset")
    return {"owner": owner, "batch": batch, "entries": entries, "cost": round(cost, 4)}


def custom_audition(store, router, series, owner: str, *, prompt: str,
                    count: int = CUSTOM_COUNT, text: str | None = None) -> dict:
    """定制试音：一段声线描述 → 若干次演绎。每条都是一把不同的声音，选中即为音色本身。"""
    desc = (prompt or "").strip()
    if not desc:
        raise KinemaError("定制生成需要一段声线描述——按六槽位写 40~80 字："
                          "性别年龄段/音区明暗/音质质感/语速节奏/口音吐字/气质"
                          "（如「五十岁男性，低音区偏暗，嗓音略带沙哑、胸腔共鸣强，"
                          "语速偏慢、句尾下沉，标准普通话，气质沉稳」），不写情绪词")
    ent = _entity(series, owner)            # 角色必须已登记，早失败早报错
    prov = router.resolve_named("tts", CUSTOM_PROVIDER)
    batch = int(_obj(ent.get("custom_audition")).get("batch") or 0) + 1
    adir = _audition_dir(series, owner, "custom", batch)
    adir.mkdir(parents=True, exist_ok=True)
    line = _audition_line(owner, text)
    body = _custom_prompt(owner, desc, line)
    entries, cost = [], 0.0
    for no in range(1, max(1, int(count)) + 1):
        out = adir / f"{no}.mp3"
        res = prov.synthesize(body, str(out), prompt_only=True)
        cost += getattr(res, "cost", 0.0) or 0.0
        entries.append({"no": no, "path": str(out)})
    with series.commit():
        ent = _entity(series, owner)
        ent["custom_audition"] = {"batch": batch, "at": _now(), "prompt": desc,
                                  "text": line, "entries": entries}
        if cost > 0:
            series.add_cost("tts", cost)
    _prune_batches(series, owner, "custom")
    return {"owner": owner, "batch": batch, "prompt": desc, "entries": entries,
            "cost": round(cost, 4)}


def _custom_prompt(owner: str, desc: str, line: str) -> str:
    """声线描述 + 台词 → text_prompt。格式对齐 seed-audio 的音频剧本写法：
    先一行声线定义，空行后是这个人说的话——模型据此决定音色，再念台词。"""
    return f"{owner} 是{desc.strip()}\n\n{owner}说道：“{line}”"


# ---------------------------------------------------------------------------
# 立档与启用
# ---------------------------------------------------------------------------
def _next_id(bank: dict) -> str:
    seq = int(bank.get("seq") or 0) + 1
    bank["seq"] = seq
    return f"vc_{seq:04d}"


def _speech_rate(clip: Path, text: str) -> float | None:
    """档案音频的实测语速（归一化字/秒）；音频探不出时长时不记。"""
    try:
        secs = probe_duration(clip)
    except FFmpegError:
        return None
    n = speech_chars(text)
    return round(n / secs, 2) if secs > 0 and n else None


def _register(series, *, owner: str, mode: str, alias: str | None,
              voice_type: str | None, prompt: str | None, src: Path,
              source: dict | None = None, text: str | None = None,
              speech_rate: float | None = None) -> dict:
    """把一条候选立成档案：音频复制进 casts/ 后不再改动。

    `text` 是候选音频念的那句，据它与音频时长得出 `speech_rate`，lint 按它预估
    台词能否落进画面窗口；跨项目引入时直接沿用源档案的 `speech_rate`。

    模版音色重复选中同一把时复用既有档案——同一把官方音色在同一实体名下再立一条，
    `(owner, ref) → 档案` 就不再唯一，「在用哪条」随即无解。

    定制音色的 `voice_type` 由这里按新发的档案号派生，**不接受调用方传入**：
    编号与身份出自两处就会错位，跨项目引入一条定制音色时尤其明显（那边的编号
    在这边可能已经属于别人）。"""
    if not src.is_file():
        raise KinemaError(f"候选音频已不在: {src}")
    bank = _bank(series)
    if mode == "preset":
        hit = next((c for c in _rows(bank["casts"])
                    if c.get("owner") == owner and c.get("voice_type") == voice_type), None)
        if hit is not None:
            hit["used_at"] = _now()
            return hit
    cid = _next_id(bank)
    if mode != "preset":
        voice_type = f"{CUSTOM_PREFIX}{cid}"
    clip = _casts_dir(series) / f"{cid}.mp3"
    clip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, clip)
    cast = {"id": cid, "owner": owner, "mode": mode, "voice_type": voice_type,
            "alias": alias, "prompt": prompt, "clip": str(clip),
            "source": source or {}, "at": _now(), "used_at": _now()}
    rate = speech_rate or (_speech_rate(clip, text) if text else None)
    if rate:
        cast["speech_rate"] = rate
    bank["casts"].append(cast)
    return cast


def _activate(series, store, cast: dict) -> dict:
    """把档案定为该实体在用的音色，同步全部已建章节，并传播音色血缘。"""
    owner = cast["owner"]
    ref = cast_ref(cast)
    ent = _entity(series, owner)
    ent["voice"] = ref
    cast["used_at"] = _now()
    synced = _sync_chapters(series, owner, ref)
    stale = propagate(series, store)
    return {"owner": owner, "cast": cast["id"], "mode": cast["mode"],
            "voice": ref, "voice_type": cast["voice_type"], "clip": cast["clip"],
            "chapters_synced": synced, **stale}


def _sync_chapters(series, owner: str, ref: str | None) -> int:
    """把指派与档案库推进已建章节。

    章节要能脱离项目文档独立渲染，所以档案库整份随行——否则定制音色的参考音路径
    在章节侧解析不出来。指派只改这一个实体那一格，不整表覆写（章节可能有本地追加）。"""
    bank = _obj(series.data.get("voice_bank"))
    synced = 0
    for ch in series.chapters:
        cid = ch.get("id")
        with series.chapter_write(cid):
            data = series.ws.store.load_chapter(series.pid, cid)
            if not data:
                continue
            data["voice_bank"] = {"seq": bank.get("seq") or 0,
                                  "casts": [dict(c) for c in _rows(bank.get("casts"))]}
            if owner == NARRATOR:
                data["narrator_voice"] = ref
            elif ref is None:
                _slot(data, "voices", {}).pop(owner, None)
            else:
                _slot(data, "voices", {})[owner] = ref
                for cc in _rows(data.get("characters")):
                    if cc.get("name") == owner:
                        cc["voice"] = ref
            series.ws.store.save_chapter(series.pid, cid, data)
            synced += 1
    return synced


def use_audition(series, store, owner: str, no: int, *, router=None) -> dict:
    """选中模版候选第 N 条：立档（或复用同音色的既有档案）并启用。"""
    with series.commit():
        ent = _entity(series, owner)
        block = _obj(ent.get("audition"))
        entries = _rows(block.get("entries"))
        hit = next((e for e in entries if int(e.get("no", 0)) == int(no)), None)
        if hit is None:
            raise KinemaError(f"「{owner}」没有编号 {no} 的试音"
                              f"（现有: {[e.get('no') for e in entries] or '无，先跑 voice audition'}）")
        cast = _register(series, owner=owner, mode="preset", alias=hit.get("voice"),
                         voice_type=hit.get("voice_type"), prompt=None,
                         src=Path(hit["path"]),
                         source={"kind": "preset", "batch": block.get("batch"), "no": int(no)},
                         text=block.get("text"))
        r = _activate(series, store, cast)
    return {**r, "anchor": _warm_anchor(series, router, cast.get("voice_type"))}


def use_custom(series, store, owner: str, no: int, *, router=None,
               expect_batch: int | None = None) -> dict:
    """选中定制候选第 N 条：立档并启用。每次都是新档案——那条音频本身就是这把音色。

    `expect_batch` 给「生成后紧接着立档」的调用方（`voice custom --adopt`）核对批次：
    候选块是整批覆盖的，两次写盘之间若有别的写者给同一实体又生成一批，
    按编号取到的就是另一条音频，而档案还会记成新批次。"""
    with series.commit():
        ent = _entity(series, owner)
        block = _obj(ent.get("custom_audition"))
        if expect_batch is not None and int(block.get("batch") or 0) != int(expect_batch):
            raise KinemaError(
                f"「{owner}」的定制候选已被第 {block.get('batch')} 批覆盖"
                f"（要立档的是第 {expect_batch} 批）——重新试听后用 voice use 指定编号")
        entries = _rows(block.get("entries"))
        hit = next((e for e in entries if int(e.get("no", 0)) == int(no)), None)
        if hit is None:
            raise KinemaError(f"「{owner}」没有编号 {no} 的定制试听"
                              f"（现有: {[e.get('no') for e in entries] or '无，先跑 voice custom'}）")
        cast = _register(series, owner=owner, mode="custom", alias=None,
                         voice_type=None, prompt=block.get("prompt") or "",
                         src=Path(hit["path"]),
                         source={"kind": "custom", "batch": block.get("batch"), "no": int(no)},
                         text=block.get("text"))
        r = _activate(series, store, cast)
    # 定制音色的锚定音就是档案那条音频（立档即在盘），这一步不发请求、只是照实回填
    return {**r, "anchor": _warm_anchor(series, router, cast.get("voice_type"))}


def cast_custom(series, store, router, owner: str, prompt: str, *,
                count: int = 1, no: int = 1, text: str | None = None) -> dict:
    """按声线描述造 `count` 条演绎并把第 `no` 条立档启用：`character add/set
    --voice-prompt`、`voice custom --adopt N` 与选角闸给出的修法都落到这里。

    描述先写进实体的 `voice_prompt`，再生成；档案的 `prompt` 记录造声时用的那段，
    之后改卡片不回写档案。"""
    desc = (prompt or "").strip()
    if not desc:
        raise KinemaError(f"「{owner}」的声线描述为空——按六槽位写 40~80 字："
                          "性别年龄段/音区明暗/音质质感/语速节奏/口音吐字/气质，不写情绪词")
    with series.commit():
        _entity(series, owner)["voice_prompt"] = desc
    r = custom_audition(store, router, series, owner, prompt=desc, count=count, text=text)
    return use_custom(series, store, owner, no, router=router, expect_batch=r["batch"])


# ---------------------------------------------------------------------------
# 直接指派：不经试音，按别名把一把声音钉给某个实体
# ---------------------------------------------------------------------------
def assign_voice(series, store, owner: str, ref: str, *, router=None) -> dict:
    """把音色别名 `ref` 指派给实体并落地锚定音
    → `{owner, voice, voice_type, anchor}` + `propagate` 的血缘计数。

    **指派模版音色的唯一出口**：`character add/set --voice` 与网页指派共用。
    指派与落料必须同一步完成——只写 `characters[].voice` 的话，已建章节拿的仍是
    建章时的拷贝，而可试听的样本要到生视频时才现合成，选角状态在项目页与章节页
    就成了两份事实。

    不建 `voice_bank` 档案：档案的语义是「试音选出来的那一条音频」，直接指派官方
    别名没有这样的来源。可试听的样本走锚定音缓存（`bank_view` 的 anchor 位）。

    锚定音预热在 `commit()` 之外：那是一次数秒的 TTS 往返，放进锁里会拿文档锁按住
    整个合成过程；失败不冒泡，真发时会再试一次（同 `_warm_anchor`）。
    """
    ref = str(ref or "").strip()
    if not ref:
        raise KinemaError(f"「{owner}」的音色别名为空——指派音色必须给一个 voices.yaml 里的别名")
    with series.commit():
        ent = _entity(series, owner)
        ent["voice"] = ref
        _sync_chapters(series, owner, ref)
        stale = propagate(series, store)
    vt = store.resolve_voice(ref) or ref
    return {"owner": owner, "voice": ref, "voice_type": vt,
            "anchor": _warm_anchor(series, router, vt), **stale}


def speaking_owners(shots) -> list[str]:
    """这批镜里开口说话的实体（角色与旁白，按首次出现排序）：选角覆盖面的取材口径。

    渲染侧与网页侧都以它取材——旁白的各种写法（narrator/旁白/voiceover）统一归一到
    `NARRATOR`，与 `uncast_owners` 的 owner 口径一致。"""
    owners: list[str] = []
    for s in shots:
        for ln in voicecast.shot_lines(s):
            who = (NARRATOR if voicecast.is_narrator(ln.get("speaker"))
                   else str(ln.get("speaker")).strip())
            if who not in owners:
                owners.append(who)
    return owners


def uncast_owners(series, owners) -> list[str]:
    """没有音色引用的说话人：旁白与已登记角色按 `owner_ref` 判，只出现在台词里、
    未登记成角色的说话人同样列出——引擎不代建实体。profile 缺省音色不算选角。"""
    out: list[str] = []
    for owner in owners:
        owner = str(owner or "").strip()
        if owner and owner not in out and not owner_ref(series.data, owner):
            out.append(owner)
    return out


# ---------------------------------------------------------------------------
# 音色血缘：换了声音，已经配过音的镜就过期了
#
# `stage_tts` 的重合成判据是「wav 在不在盘」，它看不见音色换没换。不传播的话
# 一章会停在一半旧声一半新声，且全程零提示——与设定图换版必须传播过期是同一条纪律。
# ---------------------------------------------------------------------------
class _ChapterView:
    """镜级音色解析要的最小章节视图（只有 data 与 voices 两件）。

    刻意不构造 `Project`：那要求章节文件在盘、还会连带一堆渲染期字段，
    而这里只是拿现成的文档 dict 问一句「这镜现在该用哪把声音」。"""

    def __init__(self, data: dict):
        self.data = data

    @property
    def voices(self) -> dict:
        return self.data.get("voices") or {}


def _expected_types(store, data: dict, shot: dict) -> set:
    """按当前指派，这一镜**应该**用哪些音色。"""
    view = _ChapterView(data)
    default_ref = data.get("narrator_voice")
    return {voicecast.resolve_line_voice(view, store, shot, ln, default_ref)[1]
            for ln in voicecast.shot_lines(shot)}


def recorded_voice_types(shot: dict) -> list:
    """盘上那版配音实际用的音色：逐句留痕 `cast[]` 在则按句取，否则取镜级 `voice_type`。
    镜级值在多句对白镜里是缺省音（旁白锁），与句级并集会把每次选角动作都判成
    「音色已更换」。`stage_tts` 的重合成判据与 `propagate` 的过期判据共用此读法。"""
    gen = (shot.get("gen") or {}).get("audio") or {}
    cast = [c for c in gen.get("cast") or [] if isinstance(c, dict)]
    if cast:
        return [c.get("voice_type") for c in cast]
    return [gen["voice_type"]] if gen.get("voice_type") else []


def _recorded_types(shot: dict) -> set:
    """这一镜**当前那版**配音实际用了哪些音色（不含归档版本）。"""
    return {str(t) for t in recorded_voice_types(shot) if t}


def _burned_anchor_types(shot: dict) -> set:
    """这一版片段**实际附发过**哪几把音色的参考音。

    唯一留痕是 `gen.clip.envelope.references` 里 role=voice_anchor 的行，
    id 形如 `shot:<镜号>:voice:<voice_type>`（写入口 `cli._video_envelope` 单一真源，
    形状由 `PromptReference` 硬校验成 {role,id,sha256}）。sha256 是参考音**文件**
    摘要，重预热与按预算裁剪都会变，不能拿它当音色身份。

    切分必须按 `:voice:` 整串切：定制音色的 voice_type 自带冒号（`custom:vc_0001`），
    按冒号 rsplit 会把它切碎。

    快照与画布不同源时返回空——`versioning.rollback` 只搬文件不动 `gen`，回滚过的镜
    其 envelope 描述的是最新生成的那一版而非在盘那一版，拿它判过期会误判成换过音色。"""
    from .pipeline import versioning
    snap = ((shot.get("gen") or {}).get("clip") or {})
    env = snap.get("envelope") or {}
    if not env or snap.get("version") != versioning.current_version(shot, "clip"):
        return set()
    out = set()
    for r in env.get("references") or []:
        if isinstance(r, dict) and r.get("role") == "voice_anchor":
            _head, sep, vt = str(r.get("id") or "").partition(":voice:")
            if sep and vt:
                out.add(vt)
    return out


def _anchorable_types(store, data: dict, shot: dict) -> set:
    """按当前选角，这一镜的说话人**能**锚定到哪几把音色。

    `max_refs` 刻意给足：条数上限由 provider 决定且各档不同，用缺省值重算会把
    当初因超位而没附发的说话人算进来，凑出一组「当初烧的那把已不在名单里」的假过期。
    参考位是发送侧的限额，不是音色身份的一部分。"""
    plan = voicecast.voice_anchor_plan(_ChapterView(data), store, shot,
                                       max_refs=len(voicecast.shot_lines(shot)) or 1)
    return {r["voice_type"] for r in plan["anchored"]}


CLIP_STALE_KEY = "voice_clip_stale"          # 这一版片段里已被换掉的那几把音色
CLIP_STALE_PREV_KEY = "voice_clip_stale_prev"

STALE_NOTE = "音色已更换，请按新音色重跑配音"


def _unflag(shot: dict, review) -> bool:
    """盘上那条音轨重新对得上了，把本模块留下的痕迹撤干净。返回是否动过。

    只撤自己写的那一笔（认 `STALE_NOTE`）：人工写入的表态不由引擎撤销。"""
    changed = shot.pop("voice_stale", None) is not None
    if review.needs_retake(shot, "audio") \
            and review.get_note(shot, "audio") == STALE_NOTE:
        prev = shot.pop("voice_stale_prev", None)
        node = shot.setdefault("review", {})
        if prev:
            node["audio"] = prev
        else:
            node.pop("audio", None)
        changed = True
    else:
        changed = shot.pop("voice_stale_prev", None) is not None or changed
    return changed


def _propagate_audio(s, store, data: dict, review) -> str | None:
    """烧录轨那条边：盘上的 wav 出自哪几把声音，与现在该用哪几把对不上就标出来。
    返回 `"retake"` / `"stale"` / None（未动）。"""
    was = _recorded_types(s)
    if not was:
        return None
    now = _expected_types(store, data, s)
    if not now or None in now:
        return None
    if now == was:
        return "unflag" if _unflag(s, review) else None
    if review.is_locked(s, "audio"):
        stale = sorted(x for x in was if x)
        if s.get("voice_stale") == stale:
            return None            # 已标同值：本次换的是别人的音色，不重计
        s["voice_stale"] = stale
        return "stale"
    if not (review.needs_retake(s, "audio")
            and review.get_note(s, "audio") == STALE_NOTE):
        # 覆盖前把原表态整条留起来：换回原音色时要**原样**还给用户，
        # 而不是替他判成「通过」（`set_state` 到 done 还会顺手消费批注）
        s["voice_stale_prev"] = (s.get("review") or {}).get("audio")
        review.set_state(s, "audio", "retake", note=STALE_NOTE)
        return "retake"
    return "dirty"


def _propagate_clip(s, store, data: dict, review) -> str | None:
    """模型原生发声那条边：这一版片段里烧进去的音色，现在还在不在选角名单里。

    native 对白镜的人声由视频模型念出、从不跑 tts，所以烧录轨那条边看不见它们
    （`gen.audio` 恒缺席）。判据换成实发过的锚定参考音，留痕在 envelope 里。

    **单向**：只问「烧进去的这把是不是已经不在名单里」。反过来问「名单里多了一把
    却没烧」会在刚给某个说话人补上选角时立刻命中——那一镜本来就没问题，
    而置 clip retake 是按秒重买整镜的动作。

    clip 的 retake **不带 note**：`review.get_note(shot,"clip")` 会被编进下一版视频
    提示词的「本次修正重点」，把「音色换了」当成画面修改意见花钱发给模型。
    归属因此另立标记字段，不复用 audio 那条 note。"""
    burned = _burned_anchor_types(s)
    if not burned:
        return None
    gone = sorted(burned - _anchorable_types(store, data, s))
    if not gone:
        if s.pop(CLIP_STALE_KEY, None) is None:
            return None
        prev = s.pop(CLIP_STALE_PREV_KEY, None)
        if review.needs_retake(s, "clip"):
            node = s.setdefault("review", {})
            if prev:
                node["clip"] = prev
            else:
                node.pop("clip", None)
        return "unflag"
    if s.get(CLIP_STALE_KEY) == gone:
        return None
    if review.is_locked(s, "clip"):
        s[CLIP_STALE_KEY] = gone
        return "stale"
    s[CLIP_STALE_PREV_KEY] = (s.get("review") or {}).get("clip")
    s[CLIP_STALE_KEY] = gone
    review.set_state(s, "clip", "retake")
    return "retake"


def propagate(series, store) -> dict:
    """换过音色之后，把对不上的镜标出来：
    `{voice_retake, voice_stale, clip_retake, clip_stale}`。

    两条边各判各的，因为两种制式的人声出自不同产物：烧录轨看逐镜 wav
    （`_propagate_audio`），模型原生发声看片段实发过的锚定参考音（`_propagate_clip`）。
    只看前者的话，native 对白镜换了音色后既不置 retake 也不告警——那正是 830 裁决
    之后对白镜的默认制式。

    未锁定的镜置 retake（下次跑对应阶段自动重出并归档旧版）；已通过锁定的镜只挂
    过期标记等人裁决——done 由人工置定，引擎不自动解除（与设定图血缘同一条纪律）。
    解析不出音色的镜（回落到 profile 默认）不判：那要连模型路由一起算，
    而猜错的代价是让人白花一次重配的钱。

    **标与清必须同一处**：换回原来那把之后盘上产物重新对得上，标记就不该还挂着
    （否则页面永远显示「音色已更换」，而那条 retake 会白烧一次重做）。"""
    from . import review
    tally = {"voice_retake": 0, "voice_stale": 0, "clip_retake": 0, "clip_stale": 0}
    for ch in series.chapters:
        cid = ch.get("id")
        with series.chapter_write(cid):
            data = series.ws.store.load_chapter(series.pid, cid)
            if not data:
                continue
            dirty = False
            for s in data.get("shots") or []:
                if not isinstance(s, dict):
                    continue
                for prefix, fn in (("voice", _propagate_audio), ("clip", _propagate_clip)):
                    r = fn(s, store, data, review)
                    if r in ("retake", "stale"):
                        tally[f"{prefix}_{r}"] += 1
                    dirty = bool(r) or dirty
            if dirty:
                series.ws.store.save_chapter(series.pid, cid, data)
    return tally


def import_cast(series, cast: dict, *, owner: str | None = None) -> dict:
    """把另一个项目的一条档案登记进本项目（跨项目复用角色时音色随行）。

    档案号是项目内序列，所以进来要重新发号、音频另存一份——两个项目共用一条 clip
    路径的话，源项目删档就会把目标项目的参考音一并带走。"""
    with series.commit():
        return _register(series, owner=owner or cast["owner"], mode=cast.get("mode") or "preset",
                         alias=cast.get("alias"), voice_type=cast.get("voice_type"),
                         prompt=cast.get("prompt"), src=Path(cast.get("clip") or ""),
                         speech_rate=cast.get("speech_rate"))


def use_cast(series, store, cast_id: str, *, router=None) -> dict:
    """换回档案里的某一把声音。"""
    with series.commit():
        cast = find_cast(series.data, cast_id)
        if cast is None:
            raise KinemaError(f"没有音色档案 {cast_id}"
                              f"（现有: {[c.get('id') for c in casts_of(series.data)] or '无'}）")
        r = _activate(series, store, cast)
    return {**r, "anchor": _warm_anchor(series, router, cast.get("voice_type"))}


# ---------------------------------------------------------------------------
# 引用账
#
# 判据只认 `voice_type` 不按说话人细分：宁可多拦一条，也不能误删一把已经烧进成片的
# 声音。定制音色的 voice_type 按档案唯一，所以「哪几镜用了哪一把」是可计算的。
# ---------------------------------------------------------------------------
def _shot_generated_types(shot: dict) -> set[str]:
    """这一镜的配音产物**实际**用过哪些音色（含逐句留痕与归档版本）。"""
    out: set[str] = set()
    gen = (shot.get("gen") or {}).get("audio") or {}
    if gen.get("voice_type"):
        out.add(str(gen["voice_type"]))
    for c in gen.get("cast") or []:
        if isinstance(c, dict) and c.get("voice_type"):
            out.add(str(c["voice_type"]))
    for v in (shot.get("versions") or {}).get("audio") or []:
        p = (v or {}).get("params") or {}
        if p.get("voice_type"):
            out.add(str(p["voice_type"]))
        for c in p.get("cast") or []:
            if isinstance(c, dict) and c.get("voice_type"):
                out.add(str(c["voice_type"]))
    return out


def _shot_assigned_refs(shot: dict) -> set[str]:
    """这一镜**指名**了哪些音色（镜级与句级的显式 voice）。"""
    out = {str(shot["voice"])} if shot.get("voice") else set()
    for ln in shot.get("lines") or []:
        if isinstance(ln, dict) and ln.get("voice"):
            out.add(str(ln["voice"]))
    return out


def reference_index(series) -> dict:
    """全项目章节扫一遍，得到「音色 → 出处」的索引：`{generated: {...}, assigned: {...}}`。

    引用账的每一面都从它派生。一次页面渲染要看十几条档案，逐条重扫等于把整个项目
    的章节文档读上十几遍。"""
    gen: dict[str, list] = {}
    asg: dict[str, list] = {}

    def put(table, key, item):
        if key:
            table.setdefault(str(key), []).append(item)

    for ch in series.chapters:
        cid = ch.get("id")
        data = series.ws.store.load_chapter(series.pid, cid)
        if not data:
            continue
        put(asg, data.get("narrator_voice"), {"chapter": cid, "where": NARRATOR})
        for name, v in _obj(data.get("voices")).items():
            put(asg, v, {"chapter": cid, "where": name})
        for s in data.get("shots") or []:
            if not isinstance(s, dict):
                continue
            for vt in _shot_generated_types(s):
                put(gen, vt, {"chapter": cid, "shot": s.get("id")})
            for r in _shot_assigned_refs(s):
                put(asg, r, {"chapter": cid, "where": f"镜 {s.get('id')}"})
    return {"generated": gen, "assigned": asg}


def cast_references(series, cast_id: str, index: dict | None = None) -> dict:
    """一条档案的引用账：在用 / 已产出 / 已指派，三面各自点名到镜。

    · `in_use`     实体当前指向它——换成别的档案才谈得上删；
    · `generated`  已经花钱合成过配音的镜，删了就无从溯源；
    · `assigned`   指派到位但还没产出的镜与音色表位置，要先改派。
    """
    cast = find_cast(series.data, cast_id)
    if cast is None:
        raise KinemaError(f"没有音色档案 {cast_id}")
    idx = index if index is not None else reference_index(series)
    vtype = cast.get("voice_type")
    ref = cast_ref(cast)
    keys = {x for x in (vtype, ref) if x}
    # 在用面查**全部实体**而不只查档案自己的 owner：手工把另一个实体的 voice
    # 写成同一个 custom:vc_* 是合法形态，只查 owner 会让删除闸放行、
    # 那个实体的配音在请求期才炸
    owners = {NARRATOR}
    owners.update(c.get("name") for c in _rows(series.data.get("characters"))
                  if c.get("name"))
    owners.update(_obj(series.data.get("voices")).keys())
    in_use = sorted(o for o in owners if owner_ref(series.data, o) in keys)
    generated = [x for k in keys for x in idx["generated"].get(k, [])]
    # 已产出的镜同时也挂在指派面上（音色表里那一格），点两次名只会让理由变啰嗦
    seen = {(x["chapter"], x["shot"]) for x in generated}
    assigned = [x for k in keys for x in idx["assigned"].get(k, [])
                if (x["chapter"], str(x["where"]).removeprefix("镜 ")) not in
                {(c, str(s)) for c, s in seen}]
    return {"cast": cast_id, "in_use": in_use, "generated": generated,
            "assigned": assigned,
            "deletable": not (in_use or generated or assigned)}


def delete_cast(series, cast_id: str) -> dict:
    """删除一条音色档案（连同它那条不可变音频）。有任何引用一律拒绝。"""
    with series.commit():
        refs = cast_references(series, cast_id)
        if not refs["deletable"]:
            raise KinemaError(_undeletable_reason(refs))
        bank = _bank(series)
        cast = find_cast(series.data, cast_id)
        bank["casts"] = [c for c in bank.get("casts") or [] if c.get("id") != cast_id]
        clip = Path(cast.get("clip") or "")
        if clip.is_file():
            clip.unlink()
        # 档案库随行章节，删完要把新的一份推下去，否则章节侧还留着已删条目
        synced = _sync_chapters(series, cast["owner"], owner_ref(series.data, cast["owner"]))
        return {"cast": cast_id, "owner": cast["owner"], "chapters_synced": synced}


def _undeletable_reason(refs: dict) -> str:
    bits = []
    if refs["in_use"]:
        # 点名是谁在用：在用面查的是全部实体，只说「正在使用」不说是谁，
        # 档案 owner 早换了声时就无从核对这道闸拦的到底是哪一处引用
        bits.append(f"{'、'.join(refs['in_use'])} 正在使用（先给他们换成别的音色）")
    if refs["generated"]:
        n = len(refs["generated"])
        where = "、".join(f"{r['chapter']} 镜 {r['shot']}" for r in refs["generated"][:3])
        bits.append(f"{n} 个分镜的配音出自这把声音（{where}{'…' if n > 3 else ''}）")
    if refs["assigned"]:
        n = len(refs["assigned"])
        where = "、".join(f"{r['chapter']} {r['where']}" for r in refs["assigned"][:3])
        bits.append(f"{n} 处仍指派着它（{where}{'…' if n > 3 else ''}）")
    return f"音色档案 {refs['cast']} 不能删除：" + "；".join(bits)


# ---------------------------------------------------------------------------
# 展示模型（CLI 与 Studio 共用同一份视图，前端不自算引用账）
# ---------------------------------------------------------------------------
def owners(series) -> list[str]:
    """项目里所有需要选角的实体：旁白恒在，角色按登记顺序。"""
    return [NARRATOR] + [c.get("name") for c in series.characters if c.get("name")]


def _cast_of_source(mine: list[dict], kind: str, batch, entry: dict) -> str | None:
    """这条候选对应的档案（供页面标「已入档」而不是标成「已选」）。

    模版按 `voice_type` 认：那是这把官方音色的身份，同一实体名下只会有一条档案，
    换一批试音再遇到它仍然是同一把声音。定制按「哪一批的第几条」认：每次演绎都不同，
    只有出处能把候选与档案对上。"""
    for c in mine:
        if kind == "preset":
            if c.get("mode") == "preset" and c.get("voice_type") == entry.get("voice_type"):
                return c["id"]
            continue
        src = c.get("source") or {}
        if src.get("kind") == kind and src.get("batch") == batch and src.get("no") == entry.get("no"):
            return c["id"]
    return None


def bank_view(series, owner: str, index: dict | None = None, store=None) -> dict:
    """某实体的选角全貌：在用的那把 + 档案（含引用账）+ 两路候选。

    引用账在这里压成计数与一句理由：页面要的是「能不能删、为什么不能」，
    逐镜清单挂在每次总览下发里只是把体积翻几倍。要明细走 `cast_references`。"""
    ent = _entity(series, owner)
    idx = index if index is not None else reference_index(series)
    ref = owner_ref(series.data, owner)
    active = cast_for_ref(series.data, owner, ref)
    mine = [c for c in casts_of(series.data) if c.get("owner") == owner]
    rows = []
    for c in mine:
        r = cast_references(series, c["id"], idx)
        rows.append({**c, "active": bool(active and active["id"] == c["id"]),
                     "refs": {"generated": len(r["generated"]),
                              "assigned": len(r["assigned"]),
                              "in_use": bool(r["in_use"]),
                              "deletable": r["deletable"],
                              "reason": "" if r["deletable"] else _undeletable_reason(r)}})

    def _batch(block, kind: str) -> dict:
        b = dict(_obj(block))
        b["entries"] = [{**e, "cast": _cast_of_source(mine, kind, b.get("batch"), e)}
                        for e in _rows(b.get("entries"))]
        return b

    # 在用音色的可试听样本。直接指派（`character set --voice`）不建档案，
    # 样本只存在于锚定音缓存里；不下发它，页面对这类实体就只有别名而无可播放的音频。
    # 取值与真发同源（`anchor_clip_for`）。
    vt = (store.resolve_voice(ref) or ref) if (store and ref) else ref
    anchor = (anchor_clip_for(
        series, vt, voicecast.series_ref_dir(series, create=False))[0]
        if vt else None)
    return {"owner": owner, "voice": ref, "active": (active or {}).get("id"),
            "voice_prompt": ent.get("voice_prompt") or "",
            "anchor": anchor, "casts": rows,
            "audition": _batch(ent.get("audition"), "preset"),
            "custom_audition": _batch(ent.get("custom_audition"), "custom")}


def bank_views(series, store=None) -> dict:
    """全项目选角视图 `{实体: bank_view}`——索引只建一次。

    `store` 供别名 → voice_type 解析（可试听样本的取值要用它）；缺省 None 时
    只是拿不到官方音色的锚定音，其余照旧。"""
    idx = reference_index(series)
    return {who: bank_view(series, who, idx, store) for who in owners(series)}
