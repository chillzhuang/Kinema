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

"""原创小说创作层（novel）——「Python + AI 两段式」的 Python 半。

与 adaptation（改编：一次性导入既有全本）互补：小说是**边写边长**的，本模块给
原创正文一个与 `source/` 平级的落点 `manuscript/`，并承接全部机械环节——
章节登记（字数/指纹/版本归档）、精简大纲与章末状态快照的落库、伏笔账本状态机、
里程碑检查点、跨章确定性 lint。**写正文/判文风/判人设的智能一律归 Claude 指挥层**
（铁律「引擎内无 LLM」），协议见 kinema-novel SKILL。

三条目录纪律（与 source/study 同族）：
  · `manuscript/` **刻意不带 `_work` 后缀**——`scanner.rglob('*_work')` 是 Studio
    片库扫描入口，带后缀正文目录会被当成成片产物收录；
  · 契约里的路径一律**工作区相对**（`manuscript/ch0001.md`）；.md 非媒体后缀，
    `collect_media` 天然不收录——正文永不被 `oss sync` 传上公网桶；
  · 全部「读—改—写」走 `Series.commit()`（Studio 是 ThreadingHTTPServer，
    裸 save 是无合并整份覆写，并发写互相抹掉且不报错——见 workspace.commit）。

数据落位（系列文档 project.json）：
  · `novel`            —— 引擎登记块（经 `novel` 命令回填）：章节清单
                          {no,title,file,chars,sha256,digest,state,entities,versions}
  · `threads[]`        —— 伏笔账本（`novel thread-*` 唯一写路径）：
                          {id,title,setup,due,status:open|paid|dropped,paid_in,note}
                          **「超期」是派生判定不落盘**（存了会与最新章号脱钩）
  · `arcs[]`           —— 卷/幕规划（`novel arc` 唯一写路径）：长篇的**大纲落点**。
                          {no,title,from,to,premise,goal,climax,turns[],note}
                          **「写到哪一卷」同样是派生判定不落盘**（同 threads 教训）。
                          检查点问「有没有跑偏大纲」，得先有个大纲可对照。
  · `narrative_style`  —— 文风单点真源（作者/指挥层填；与 style_prompt 同范式）：
                          pov/tense/voice/diction/baseline[]/avoid[]。防漂移的正道
                          是「基线样本 + 偏离检测」而非每章复述风格描述——复述即
                          漂移，这条提示词纪律在文本生成上同样成立。

三个只读取料出口（零成本纯计算，供指挥层每章/每批次消费）：
  · `brief`  写前必读包（文风契约＋在场角色人设卡＋上章 state＋未回收伏笔＋当前卷）
             ——把「五处翻查」压成一次调用，长篇上下文预算的关键；
  · `recap`  批次复核物料（逐章概要表＋伏笔动静＋新登场实体＋缺项＋文体量化）
             ——十章检查点那份《批次报告》的骨架，逐项数不许估；
  · `lint`   跨章确定性体检（断号/缺件/伏笔/缺席/卷覆盖/忌讳词/**文体量化**）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .errors import ProjectError

# 里程碑：每写满 MILESTONE_EVERY 章触发一次「十章检查点」（六门复核 + 批次报告）；
# 逐章提醒则在每次 save 收尾必打。
MILESTONE_EVERY = 10
# 已出场角色连续缺席多少章后 lint 提示（只提示不判——退场可能是剧情安排）
ABSENCE_WINDOW = 10
# 无回收期限的伏笔挂起多少章后 lint 提示
STALE_THREAD_WINDOW = 20
# 文体量化的默认扫描窗口：只回看最近这么多章。**必须有窗**——百章级项目上
# 逐章全扫是 O(全书)，而检查点要的本来就是「本批次」这一段的文体。
PROSE_WINDOW = MILESTONE_EVERY
# 跨章重复短语：REPEAT_N 字滑窗、窗口内出现 ≥REPEAT_MIN 次才报（句内取词，不跨句读）
REPEAT_N, REPEAT_MIN = 6, 3
# 对白占比低于此值 = 整段几乎全是叙述（只提示——冷叙述文体是合法选择）
DIALOGUE_MIN = 0.08
# 单个 AI 味口癖在窗口内命中多少次才进 lint（低于此值属正常用词）· lint 只报最狠的几条
SLOP_MIN_HITS, SLOP_TOP = 3, 6
# 节奏账（第⑦门）：连续无 payoff 多少章即「平推过久」· 同一 payoff_kind/hook 连用几章即单调
PACING_FLAT, PACING_SAME = 3, 3
# 伏笔三档跨度 → 缺省回收期限（章）。长线不设期限但恒进「长期挂起」统计——
# 否则「不填 due」就成了让护栏静音的动作，激励方向恰好反了。
THREAD_TIERS = {"short": 30, "mid": 100, "long": None}
# 角色状态：只有 active 才进「连续缺席」提醒。长篇里永久退场是常态不是异常，
# 恒报即等于不报（350 章的书一次报 21 条，真信号全被淹）。
CHAR_STATUS = ("active", "departed", "dead")

# AI 味口癖表（词 → **物理化改写建议**）。与 pipeline/variation.SLOP_TERMS 同哲学：
# 每条都必须带可执行的改写方向——只报命中而不给改法，指挥层无从执行。引擎只数命中次数，
# 「这一处该不该改」永远是指挥层三门自检的判断。
PROSE_SLOP: dict[str, str] = {
    "不禁": "删掉副词，直接写动作：「他不禁后退」→「他退了半步」",
    "不由得": "同上，让身体反应自己说话",
    "忍不住": "写没忍住之后干了什么，别写「忍不住」这个状态",
    "下意识": "改成具体动作与时序：「手先动了，脑子才跟上」",
    "缓缓": "给速度一个参照物：「像怕碰碎什么似的推开门」",
    "渐渐": "写变化的中间态，别用副词概括过程",
    "微微": "量化幅度或删掉：「眉毛抬了不到一指宽」",
    "淡淡": "用具体语调/动作替代：「他说这话时没抬头」",
    "轻轻": "换成有质地的动作动词（拈、掖、搭、抵）",
    "似乎": "叙述者要么知道要么不知道，含混词是逃避",
    "仿佛": "比喻要给出喻体的具体形象，别用「仿佛」兜底",
    "宛如": "同上；四字比喻套话密度高即 AI 味",
    "心中一凛": "写生理反应：颈后发凉／指节发白／忘了下一句要说什么",
    "心头一震": "同上，情绪落到身体上",
    "深吸一口气": "换个准备动作：整袖口／把杯子推远／看了眼门",
    "嘴角勾起": "面瘫式表情套话，换成对方看到的具体变化",
    "眼中闪过": "闪过的东西看不见——写他随后做了什么",
    "瞳孔骤缩": "网文标配套话，直接删",
    "空气仿佛凝固": "写在场者的具体反应替代环境比喻",
    "时间仿佛静止": "同上",
    "不知过了多久": "给一个模糊但具体的锚：「烟烧到了指头」",
    "难以名状": "说不出就别硬说，写他放弃描述的那一刻",
    "复杂的情绪": "拆成两种打架的具体念头",
    "某种意义上": "议论腔，正文里几乎永远该删",
    "与此同时": "换场用空行或地点词，别用连接词缝合",
    "然而": "转折靠事实反差，不靠连接词（一段最多一个）",
    "总之": "总结腔，叙事正文里删",
    "值得一提的是": "百科腔，删",
    "不得不说": "评论腔，删",
    "命运的齿轮": "宏大收束套话，删",
    "故事才刚刚开始": "同上，章末钩子要写具体的下一步",
    "一切都变了": "写变了的那一件具体的事",
}

# ---------------------------------------------------------------------------
# 文体带区（PROSE_BANDS）——每条指标**两侧都设闸**。
#
# 单向阈值会长出反向 artifact：把明喻词写进口癖禁令后，样本的明喻标记密度中位
# 降到 0.00，一本长篇里一次比喻标记都不出现，同样是可测量的不自然。抑制类指标
# 因此必须配下限。
#
# 标定口径（改数值前照同一条路重跑，不接受未标定的常量）：取一部 350 章长篇全稿，
# 先过 strip_markup，按 lint 的真实口径十章一窗合并成 35 个窗口逐窗计算。
# 窗口级分位：
#     long40_ratio   min .000  中位 .007  p90 .042  max .050
#     simile_per_k   min .00   中位 .00   p90 .06   max .09
#     tri_list_per_k min .24   中位 .64   p90 1.14  max 1.37
#     mattr          min .748  中位 .821  p90 .871  max .885
# emo_punct_per_k 与 sd_ratio 不设带区：两项跨时代/跨题材漂移大，绝对阈值不成立，
# 交给 baseline_metrics 的自基线 z 分（同一作者同一本书前 N 章当标尺）。
#
# 格式 {键: (下限|None, 上限|None, 中文名, 物理化改写建议)}。越界只出一条折叠
# finding（同 SLOP_TOP 纪律），且恒为 info——文体是选择不是错误，引擎只交读数。
# ---------------------------------------------------------------------------
PROSE_BANDS: dict[str, tuple] = {
    "long40_ratio": (0.02, None, "长句占比(≥40字)",
                     "全篇几乎没有一句长句＝节奏只有一档。挑三五处让一句话跟着"
                     "人物的呼吸走完一个完整动作，别切成三段"),
    "simile_per_k": (0.30, 6.0, "明喻标记/千字",
                     "低了＝把「仿佛/似乎」当口癖禁掉之后连正经比喻也一起没了"
                     "（写具体喻体的明喻不是套话）；高了＝四字比喻套话堆积"),
    "tri_list_per_k": (None, 2.0, "三连顿号/千字",
                       "「A、B、C」三项式列举是 LLM 最稳的句法指纹之一——"
                       "拆成两项，或让第三项以动作出现而不是并列名词"),
    "mattr": (0.80, None, "用词多样度(MATTR)",
              "同一批词反复用。先看口癖榜与复读句，再查是不是母题词"
              "（人名/地名/道具名）密度过高——那类属正常，看得出就行"),
}

# ---------------------------------------------------------------------------
# 文体硬规则（PROSE_RULES）——SKILL 第 7 节已经明令、且正则完全能算的那几条。
# 「一条 grep 被当人工步骤写进 skill」是明确的下沉信号：机械检索归引擎，
# 「这一处是不是合法留存」仍归指挥层。格式 {code: (正则, 中文名, 每章上限, 建议)}，
# 每章上限 = 超过它才报（0 表示一次都不许有）。命中一律带章号定位。
# ---------------------------------------------------------------------------
PROSE_RULES: dict[str, tuple] = {
    "self_reference": (
        r"第[一二三四五六七八九十百千零〇两]+章(?!第[一二三四五六七八九十百千零〇两]+[条节款])",
        "章号自指", 0,
        "角色不知道自己活在一本书里。换成世界内锚点：日期、天数、地点、"
        "那件事本身（「第三区那天」「他把铁皮箱打开那天」）"),
    "ascii_quote": (
        r'"[^"\n]{1,200}"|[一-鿿][,;:?!]',
        "半角标点", 0,
        "对白一律中文弯引号「“…”」、标点全角。直引号是机器排版最明显的指纹——"
        "读者不一定说得出，但一眼就觉得不像出版物"),
    "definition_sentence": (
        r"不是[^。！？\n]{1,20}?，是|这叫[^。！？\n]{1,12}",
        "定义句", 1,
        "「X不是Y，是Z」「这叫……」连发＝说明书腔。一章合计 ≤1，"
        "其余换成一件事例让读者自己得出"),
    "parallel_triple": (
        r"(?m)^(.{2,6})[^\n]{0,30}\n\1[^\n]{0,30}\n\1[^\n]{0,30}$",
        "三连同起头", 2,
        "连着三段同一个词起头＝排比腔。打乱段首主语，或把其中一段并进上一段"),
    # 抬价句式：先替读者虚构一个他并没有的误会、再靠推翻它抬高下一句——翻案是**修辞动作**
    # 不是字面，换一套字（你以为/看似/与其说/从来不是）仍是同一个姿势，所以按
    # 句式族拦。与 definition_sentence 分工：那条管「不是X，是Y／这叫」的定义句形，
    # 本条管其余外衣（不是…而是／以为体／让转体／抬价体），同一处不出两条。
    "pivot_rhetoric": (
        r"你以为[^。！？\n]{1,24}[，,]?(?:其实|结果)"
        r"|不是[^。！？\n]{1,20}[，,]而是"
        r"|与其说[^。！？\n]{1,20}[，,]?(?:不如说|毋宁说|倒不如)"
        r"|看似[^。！？\n]{1,20}[，,]?实则"
        r"|表面上[^。！？\n]{1,24}[，,]?(?:其实|实际上)"
        r"|[^。！？\n]{0,12}并?不重要[，,][^。！？\n]{0,12}重要的是"
        r"|从来(?:都)?不是"
        r"|恰恰相反"
        r"|回头(?:再)?(?:看|想)[，,]?才(?:发现|明白|知道)",
        "抬价句式", 1,
        "替读者虚构一个误会再当众推翻＝给下文抬价的翻案腔。判断从正面下、"
        "依据摆在旁边；只有正文真的走过「误解→修正」过程的段落配得上这个形状"),
    # 动词名词化：动词被压成名词挂在「进行/实现」后面，是公文汇报腔渗进叙事的
    # 第一指纹——AI 草稿高发，人写的小说正文几乎不会自然长出这种句子。
    "nominalization": (
        r"(?:进行|开展)(?:了|一次|一场)?[^。！？\n]{0,10}?"
        r"(?:优化|处理|排查|改造|调整|部署|梳理|评估|升级|复盘|沟通|规划)"
        r"|实现了?[^。！？\n]{0,10}?(?:提升|增长|突破|转变|落地|跃升)"
        r"|完成了?对[^。！？\n]{1,12}的"
        r"|起到了?[^。！？\n]{0,8}作用"
        r"|具有[^。！？\n]{0,10}(?:意义|价值)",
        "动词名词化", 1,
        "动词被压成名词挂在「进行/实现」后面是公文腔——还原成动作："
        "「对伤口进行了处理」→「把伤口包了」；「实现了突破」→直接写他破关的那一下"),
}


def _now() -> str:
    from .workspace import _now as wsnow
    return wsnow()


def text_fp(text: str) -> str:
    """正文指纹，与 lineage.fingerprint 同格式（sha256:<hex16>）。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _brief_list(nos, cap: int = 12) -> str:
    nos = list(nos)
    return ", ".join(map(str, nos[:cap])) + ("…" if len(nos) > cap else "")


def manuscript_dir(series) -> Path:
    d = series.dir / "manuscript"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _versions_dir(series) -> Path:
    d = manuscript_dir(series) / "versions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chapter_relpath(no: int) -> str:
    return f"manuscript/ch{int(no):04d}.md"


def registry(series) -> dict:
    return series.data.setdefault("novel", {"chapters": [], "total_chars": 0})


def find_entry(series, no: int) -> dict | None:
    for c in registry(series).get("chapters") or []:
        if int(c.get("no") or 0) == int(no):
            return c
    return None


def read_chapter_text(series, no: int) -> str | None:
    p = series.dir / chapter_relpath(no)
    return p.read_text(encoding="utf-8") if p.is_file() else None


# ---------------------------------------------------------------------------
# 实体命中（确定性统计，供「设定回写提醒」与缺席 lint）
# 口径对齐 Project._matched_entities：名字 ≥2 字才算命中（防「刀」「杯」单字
# 泛匹配）、keywords 别名兜底。这里只统计**已登记实体**——「本章冒出的新实体」
# 引擎认不出（无分词无 NER），由指挥层在回写节点补登记（SKILL 纪律）。
# ---------------------------------------------------------------------------
def entity_mentions(series, text: str) -> dict:
    def _hits(items) -> list[str]:
        out = []
        for it in items or []:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            terms = ([name] if len(name) >= 2 else []) + [
                k for k in (it.get("keywords") or []) if k]
            if any(t in text for t in terms):
                out.append(name)
        return out

    return {"characters": _hits(series.data.get("characters")),
            "props": _hits(series.data.get("props")),
            "scenes": _hits(series.data.get("scenes"))}


# ---------------------------------------------------------------------------
# 章节正文登记
# ---------------------------------------------------------------------------
PAYOFF_LEVELS = ("minor", "medium", "major")
PAYOFF_KINDS = ("打脸", "升级", "解谜", "情感", "反转")
HOOK_KINDS = ("决定", "发现", "误判", "代价", "险境", "逼近", "错位")


def save_chapter(series, *, no: int, text: str, title: str | None = None,
                 digest: str | None = None, state: dict | None = None,
                 payoff: str | None = None, payoff_kind: str | None = None,
                 hook: str | None = None) -> dict:
    """登记一章正文：落盘 + 版本归档 + 字数/指纹/实体命中 + 里程碑判定。

    幂等：同内容重跑不叠版本（按 sha 比对判 noop，仍可顺带更新 title/digest）。
    版本归档与设定图同哲学：**归档 = 移动旧稿**进 manuscript/versions/（磁盘零
    冗余），标准路径字符串不变（新稿随后写回同路径）。
    """
    no = int(no)
    if no < 1:
        raise ProjectError(f"章号须为正整数：{no}")
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ProjectError("正文为空——novel save 只登记写完的章")
    for v, allow, what in ((payoff, PAYOFF_LEVELS, "payoff"),
                           (payoff_kind, PAYOFF_KINDS, "payoff-kind"),
                           (hook, HOOK_KINDS, "hook")):
        if v is not None and v not in allow:
            raise ProjectError(f"--{what} 只收 {'/'.join(allow)}：{v}")
    if state is not None:
        _check_state(state)
    sha = text_fp(text)
    dest = series.dir / chapter_relpath(no)
    dest.parent.mkdir(parents=True, exist_ok=True)
    old = dest.read_text(encoding="utf-8") if dest.is_file() else None
    noop = old is not None and text_fp(old) == sha

    archived = None
    if old is not None and not noop:
        # 旧稿移动归档（v 升序），随后新稿写回标准路径。版本号从**磁盘**派生
        # 而非内存 registry（commit 进锁前的内存副本可能过期，磁盘文件数不会骗人）
        v = len(list(_versions_dir(series).glob(f"ch{no:04d}_v*.md"))) + 1
        vfile = _versions_dir(series) / f"ch{no:04d}_v{v:03d}.md"
        dest.rename(vfile)
        archived = {"v": v, "file": f"manuscript/versions/{vfile.name}",
                    "at": _now(), "chars": len(old)}
    if not noop:
        dest.write_text(text, encoding="utf-8")

    ents = entity_mentions(series, text)
    with series.commit():
        reg = registry(series)
        entry = find_entry(series, no)
        if entry is None:
            entry = {"no": no, "created_at": _now()}
            reg["chapters"].append(entry)
            reg["chapters"].sort(key=lambda c: int(c.get("no") or 0))
        if title:
            entry["title"] = title
        if digest is not None and str(digest).strip():
            entry["digest"] = str(digest).strip()
        if state is not None:
            entry["state"] = state
        for k, v in (("payoff", payoff), ("payoff_kind", payoff_kind),
                     ("hook", hook)):
            if v is not None:
                entry[k] = v
        if archived:
            entry.setdefault("versions", []).append(archived)
        entry.update({"file": chapter_relpath(no), "chars": len(text),
                      "sha256": sha, "entities": ents, "updated_at": _now()})
        reg["total_chars"] = sum(int(c.get("chars") or 0) for c in reg["chapters"])
        reg["updated_at"] = _now()
        count, total = len(reg["chapters"]), reg["total_chars"]

    return {"no": no, "noop": noop, "chars": len(text), "sha256": sha,
            "file": chapter_relpath(no), "archived": archived, "entities": ents,
            "count": count, "total_chars": total,
            # 满档按**章号**判（第 70 章满档复核 61~70）：按章数判的话，接盘
            # 导入的书会在与章号无关的时刻触发，回执给出的复核窗口全是空章
            "checkpoint": no % MILESTONE_EVERY == 0,
            "missing_digest": not (find_entry(series, no) or {}).get("digest"),
            "missing_state": not (find_entry(series, no) or {}).get("state")}


def set_digest(series, no: int, digest: str) -> dict:
    """登记一章的精简大纲（两三句：本章事件 + 变化 + 尾钩）——十章检查点连读
    digest 审连贯的物料，也是 Studio 创作 Tab 的章节卡摘要。"""
    if not (digest or "").strip():
        raise ProjectError("digest 为空")
    with series.commit():
        entry = find_entry(series, int(no))
        if entry is None:
            raise ProjectError(f"第 {no} 章尚未登记正文（先 novel save）")
        entry["digest"] = str(digest).strip()
        entry["updated_at"] = _now()
    return {"no": int(no), "digest": str(digest).strip()}


_STATE_KEYS = ("time", "location", "characters", "hooks", "note")


def _check_state(state: dict) -> None:
    """章末状态快照的宽松校验（save --state 与 state 两条路径共用）。"""
    if not isinstance(state, dict) or not state:
        raise ProjectError('state 须为非空对象，如 {"time":"第三天夜里","location":"…",'
                           '"characters":{"孙缘":"负伤，藏起徽章"},"hooks":["木马起疑"]}')
    unknown = [k for k in state if k not in _STATE_KEYS]
    if unknown:
        raise ProjectError(f"state 只收 {'/'.join(_STATE_KEYS)}，未知键: {unknown}")
    if "characters" in state and not isinstance(state["characters"], dict):
        raise ProjectError("state.characters 须为 {名字: 状态一句话} 映射")
    if "hooks" in state and not isinstance(state["hooks"], list):
        raise ProjectError("state.hooks 须为数组")


def set_state(series, no: int, state: dict) -> dict:
    """登记章末状态快照（谁在哪/什么情绪/悬念栈）——下一章写前必读的物料。
    宽松校验：只收白名单键；characters 须为 {名字: 状态一句话} 映射。"""
    _check_state(state)
    with series.commit():
        entry = find_entry(series, int(no))
        if entry is None:
            raise ProjectError(f"第 {no} 章尚未登记正文（先 novel save）")
        entry["state"] = state
        entry["updated_at"] = _now()
    return {"no": int(no), "state": state}


# ---------------------------------------------------------------------------
# 伏笔账本（threads）：open → paid | dropped；「超期」恒为派生判定
# ---------------------------------------------------------------------------
def _threads(series) -> list[dict]:
    return series.data.setdefault("threads", [])


def _next_tid(series) -> str:
    mx = 0
    for t in _threads(series):
        m = re.fullmatch(r"th(\d+)", str(t.get("id") or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"th{mx + 1:02d}"


def thread_add(series, *, title: str, setup: int, due: int | None = None,
               note: str = "", tier: str | None = None) -> dict:
    """埋一条伏笔。`tier` 按跨度给缺省 due（短线 +30 / 中线 +100 / 长线不设期限）。

    为什么要有 tier：旧判据是「有 due 且过期→warn，无 due→info」，于是
    **不填 due 正好是让告警静音的那个动作**——实测某长篇 28 条未收伏笔里 8 条
    无 due，而这 8 条恰恰是全书最核心的主线悬念（挂了 330~349 章）。激励方向
    掰回来：长线也必须显式声明是长线，然后恒进「长期挂起」统计。
    """
    if not (title or "").strip():
        raise ProjectError("伏笔 title 为空")
    setup = int(setup)
    if setup < 1:
        raise ProjectError(f"埋设章号须为正整数：{setup}")
    if tier is not None and tier not in THREAD_TIERS:
        raise ProjectError(f"--tier 只收 {'/'.join(THREAD_TIERS)}：{tier}")
    if due is None and tier and THREAD_TIERS[tier]:
        due = setup + THREAD_TIERS[tier]
    if due is not None and int(due) < setup:
        raise ProjectError(f"回收期限（第 {due} 章）不能早于埋设章（第 {setup} 章）")
    with series.commit():
        t = {"id": _next_tid(series), "title": str(title).strip(), "setup": setup,
             "due": int(due) if due is not None else None,
             "tier": tier, "status": "open", "paid_in": None,
             "note": str(note or "").strip(), "at": _now()}
        _threads(series).append(t)
    return t


_THREAD_SETTABLE = ("title", "setup", "due", "tier", "note")


def thread_set(series, tid: str, **fields) -> dict:
    """改伏笔的文本与期限字段（`status`/`paid_in` 只能走 thread_mark）。

    七层扫描的第⑤层正是「改伏笔标题」——一条设定被推翻时伏笔的措辞跟着作废。
    走这条命令而不是裸改几 MB 的 project.json：后者会被长任务的旧内存副本整份
    覆写且不报错（同 decisions 教训）。
    """
    bad = [k for k in fields if k not in _THREAD_SETTABLE]
    if bad:
        raise ProjectError(
            f"thread-set 只收 {'/'.join(_THREAD_SETTABLE)}，未知字段: {bad}"
            "（改状态走 thread-pay / thread-drop）")
    if fields.get("tier") is not None and fields["tier"] not in THREAD_TIERS:
        raise ProjectError(f"--tier 只收 {'/'.join(THREAD_TIERS)}：{fields['tier']}")
    with series.commit():
        t = next((x for x in _threads(series) if x.get("id") == tid), None)
        if t is None:
            have = ", ".join(x.get("id") or "?" for x in _threads(series)) or "无"
            raise ProjectError(f"没有伏笔 {tid}（现有: {have}）")
        for k in ("title", "note", "tier"):
            if fields.get(k) is not None:
                t[k] = (str(fields[k]).strip() if k != "tier" else fields[k])
        for k in ("setup", "due"):
            if fields.get(k) is not None:
                t[k] = int(fields[k])
        # 与 thread_add 同一套推导：lint 对无期限伏笔的出路提示正是
        # 「thread-set --tier 定档给它一个期限」——只记档不给期限的话，
        # short/mid 照做后告警依旧、提示还因 tier 已填而消失，原地打转。
        # 显式给过 due 的不动（明确意图优先），long 无跨度恒不推。
        if (fields.get("tier") is not None and fields.get("due") is None
                and t.get("due") is None and THREAD_TIERS.get(t["tier"])):
            t["due"] = int(t.get("setup") or 1) + THREAD_TIERS[t["tier"]]
        if (t.get("due") is not None
                and int(t["due"]) < int(t.get("setup") or 1)):
            raise ProjectError(
                f"回收期限（第 {t['due']} 章）不能早于埋设章（第 {t.get('setup')} 章）")
        t["updated_at"] = _now()
        out = dict(t)
    return out


def thread_mark(series, tid: str, *, status: str, paid_in: int | None = None,
                note: str | None = None) -> dict:
    if status not in ("paid", "dropped", "open"):
        raise ProjectError(f"status 只收 paid/dropped/open：{status}")
    if status == "paid" and paid_in is None:
        raise ProjectError("标记回收必须给 --in 回收章号（伏笔账本的意义就在这条对账线）")
    with series.commit():
        t = next((x for x in _threads(series) if x.get("id") == tid), None)
        if t is None:
            have = ", ".join(x.get("id") or "?" for x in _threads(series)) or "无"
            raise ProjectError(f"没有伏笔 {tid}（现有: {have}）")
        t["status"] = status
        t["paid_in"] = int(paid_in) if paid_in is not None else t.get("paid_in")
        if note is not None:
            t["note"] = str(note).strip()
        t["updated_at"] = _now()
    return dict(t)


def _latest_no(data: dict) -> int:
    nos = [int(c.get("no") or 0)
           for c in ((data.get("novel") or {}).get("chapters") or [])]
    return max(nos) if nos else 0
def _threads_view(data: dict) -> dict:
    """账本三态 + 派生超期：expired = open 且有 due 且最新登记章号已越过 due。
    纯只读（不 mutate 入参）——Studio scanner 与 Series 两侧共用。"""
    cur = _latest_no(data)
    view = {"open": [], "paid": [], "dropped": [], "expired": []}
    for t in data.get("threads") or []:
        st = t.get("status") or "open"
        row = dict(t)
        if st == "open" and t.get("due") is not None and cur > int(t["due"]):
            row["expired"] = True
            view["expired"].append(row)
        view.setdefault(st, []).append(row)
    view["current_no"] = cur
    return view


def threads_view(series) -> dict:
    return _threads_view(series.data)


# ---------------------------------------------------------------------------
# 卷/幕规划（arcs）：长篇的「大纲」落点
# 检查点第一门问的是「有没有跑偏大纲」——没有登记过的大纲，这一门就只能靠
# 上下文里那点残留印象，写到第 60 章必然名存实亡。故与 threads 同制度：
# CLI 唯一写路径、**进度是派生判定绝不落盘**（存了会与最新章号脱钩）。
# ---------------------------------------------------------------------------


def _arcs(series) -> list[dict]:
    return series.data.setdefault("arcs", [])


def arc_upsert(series, *, no: int, title: str | None = None,
               frm: int | None = None, to: int | None = None,
               premise: str | None = None, goal: str | None = None,
               climax: str | None = None, turns: list[str] | None = None,
               note: str | None = None) -> dict:
    """登记/更新一卷（按卷号 upsert）。新建时 title 与起始章必给——缺这两项
    的卷不构成可执行大纲。"""
    no = int(no)
    if no < 1:
        raise ProjectError(f"卷号须为正整数：{no}")
    if frm is not None and int(frm) < 1:
        raise ProjectError(f"起始章号须为正整数：{frm}")
    if frm is not None and to is not None and int(to) < int(frm):
        raise ProjectError(f"终止章（第 {to} 章）不能早于起始章（第 {frm} 章）")
    with series.commit():
        rows = _arcs(series)
        a = next((x for x in rows if int(x.get("no") or 0) == no), None)
        fresh = a is None
        if fresh:
            if not (title or "").strip():
                raise ProjectError(f"新建第 {no} 卷必须给 --title")
            if frm is None:
                raise ProjectError(f"新建第 {no} 卷必须给 --from 起始章号")
            a = {"no": no, "at": _now()}
            rows.append(a)
            rows.sort(key=lambda x: int(x.get("no") or 0))
        for k, v in (("title", title), ("premise", premise), ("goal", goal),
                     ("climax", climax), ("note", note)):
            if v is not None:
                a[k] = str(v).strip()
        if frm is not None:
            a["from"] = int(frm)
        if to is not None:
            a["to"] = int(to)
        if turns is not None:
            a["turns"] = [str(t).strip() for t in turns if str(t).strip()]
        a["updated_at"] = _now()
        out = dict(a)
    return out | {"created": fresh}


def arc_rm(series, no: int) -> dict:
    with series.commit():
        rows = _arcs(series)
        a = next((x for x in rows if int(x.get("no") or 0) == int(no)), None)
        if a is None:
            have = ", ".join(str(x.get("no")) for x in rows) or "无"
            raise ProjectError(f"没有第 {no} 卷（现有: {have}）")
        rows.remove(a)
    return dict(a)


def _arcs_view(data: dict) -> dict:
    """卷规划视图：**进度态派生**（done/writing/planned）+ 覆盖体检（断档/重叠）。
    纯只读不 mutate 入参（scanner 与 Series 两侧共用，同 _threads_view）。"""
    cur = _latest_no(data)
    rows = sorted((data.get("arcs") or []), key=lambda a: int(a.get("no") or 0))
    out, current = [], None
    for a in rows:
        frm, to = a.get("from"), a.get("to")
        row = dict(a)
        if frm is None:
            row["state"] = "planned"
        elif to is not None and cur >= int(to):
            row["state"] = "done"
        elif cur >= int(frm):
            row["state"] = "writing"
        else:
            row["state"] = "planned"
        if row["state"] == "writing" or (
                frm is not None and to is not None
                and int(frm) <= cur + 1 <= int(to) and current is None):
            current = row
        out.append(row)
    # 覆盖体检：按起始章排序后两两比对（只看给全了起止章的卷）
    spans = sorted(((int(a["from"]), int(a["to"]), a) for a in rows
                    if a.get("from") is not None and a.get("to") is not None),
                   key=lambda s: s[0])
    gaps, overlaps = [], []
    for (af, at, aa), (bf, bt, bb) in zip(spans, spans[1:]):
        if bf <= at:
            overlaps.append({"a": aa.get("no"), "b": bb.get("no"),
                             "at": [max(af, bf), min(at, bt)]})
        elif bf > at + 1:
            gaps.append({"after": aa.get("no"), "before": bb.get("no"),
                         "at": [at + 1, bf - 1]})
    tail = spans[-1][1] if spans else 0
    return {"arcs": out, "current": current, "gaps": gaps, "overlaps": overlaps,
            "current_no": cur, "uncovered_from": tail + 1 if cur > tail else None}


def arcs_view(series) -> dict:
    return _arcs_view(series.data)


def arc_at(data: dict, no: int) -> dict | None:
    """第 no 章落在哪一卷（给 brief/recap 定位；无规划返回 None）。"""
    for a in sorted((data.get("arcs") or []), key=lambda x: int(x.get("no") or 0)):
        frm, to = a.get("from"), a.get("to")
        if frm is None:
            continue
        if int(frm) <= int(no) and (to is None or int(no) <= int(to)):
            return dict(a)
    return None


# ---------------------------------------------------------------------------
# 文体量化（AI 味的可测量面）——**只出数，绝不判「像不像 AI」**
# 「有没有 AI 味」是文学判断，归指挥层；引擎能做的是把那几条一直被口头讨论的
# 信号变成可对账的数：口癖命中、句长离散度、对白占比、段首雷同、跨章复读。
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"[。！？!?…]+[”」』）)\"]*|\n+")
# 对白识别必须认**直角引号、弯引号与直双引号**三套：中文小说里 "…" 用得极多
# （真实书稿一章 294 个 `"` 而 0 个 「」），只认前两套会把对白占比算成 2%
# 并误报「几乎全是叙述」。直双引号左右同形，靠「不跨行的成对匹配」自左向右配对。
_QUOTES = re.compile(r"[“「『][^”」』\n]{0,400}[”」』]|\"[^\"\n]{0,400}\"")
_NON_CJK = re.compile(r"[^一-鿿A-Za-z0-9]+")
_CJK = re.compile(r"[一-鿿]")


# --- markdown 剥离：文体面的**单一入口**（账目面刻意不走）-------------------
# 正文是 .md，而 markdown 记号不是作者写的字。不剥的三重后果都实测过：
#   ① 污染引擎自己的指标——350 章样本的段首雷同榜首是 `**“`×21273，真口癖被
#      完全淹没；章标题行 `# 第三百四十一章 · 无价` 被当段落，刷出「9 个段落以
#      「第三百四」开头」这种纯噪声；dialogue_ratio 的分母含语法字符被系统性压低；
#   ② markdown 本身就是要处理的问题——全书 ** 记号 90,002 个、--- 分隔行 33,353 行、
#      96,708 段里 40,916 段整段加粗（42.3%），直接违反 SKILL 铁律「粗体只给面板」；
#   ③ 这是要交出去的稿件，语法字符约占全书字数的 19%。
# **生死线：登记的 chars 与 sha256 绝不过这里**。指纹是账目，剥离是度量；
# 顺序搞反会让全书每一章一次性判为「改稿」并触发一轮版本归档。
_MD_DROP_LINE = re.compile(r"^\s*(?:#{1,6}\s|(?:-{3,}|\*{3,}|_{3,})\s*$)")
_MD_QUOTE = re.compile(r"^\s*>+\s?")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_STRONG = re.compile(r"\*\*(.+?)\*\*")
_MD_EM = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]*)`")


def strip_markup(text: str) -> str:
    """剥掉 markdown 记号只留作者写的字（行数不变，便于回指行号）。"""
    out = []
    for ln in (text or "").split("\n"):
        if _MD_DROP_LINE.match(ln):
            out.append("")
            continue
        ln = _MD_QUOTE.sub("", ln)
        ln = _MD_LINK.sub(r"\1", ln)
        ln = _MD_STRONG.sub(r"\1", ln)
        ln = _MD_EM.sub(r"\1", ln)
        out.append(_MD_CODE.sub(r"\1", ln))
    return "\n".join(out)


# 面板/系统文本的判据：**括号覆盖率**——`【…】` 段落占整行 PANEL_COVER 以上才算面板。
# 三档写法都试过，只有这一档站得住：
#   · 「含 【」→ 叙述句顺带提一句【壹·引】就被放过，大半加粗留下来，等于没治；
#   · 「行首是 【」→ 仍然漏掉 82 行「以技能名开头的叙述」（实测
#     `**【不落】缠住的是"损坏"，可它缠住的方式是把那一段时间借走…**` 覆盖率仅 0.08）；
#   · 覆盖率 ≥0.6 → 真面板（`【收到打赏 · 神币 ×5】` 覆盖率 1.0）全留，那 82 行全剥。
# 引用块（`> **「…」**` 系统播报）整行放过——那是它该在的地方。
_PANEL_SPAN = re.compile(r"[【\[][^】\]]{0,80}[】\]]")
_BLOCKQUOTE = re.compile(r"^\s*>")
PANEL_COVER = 0.6


def _is_panel(inner: str) -> bool:
    body = len(inner.strip()) or 1
    return sum(len(m.group(0)) for m in _PANEL_SPAN.finditer(inner)) / body >= PANEL_COVER


def normalize_markup(text: str) -> tuple[str, dict]:
    """正文排版规范化：**剥掉非面板的加粗**，其余一个字不动。

    执行的是 SKILL 第 7 节铁律 6「粗体只给面板/系统文本，强调靠写法不靠字体」——
    所以这不是替作者做文学判断，是把一条已经写死的规则落到文本上。

    **刻意不碰 `---`**：那条规则是「只在真的要断场时用」，而「这一条是断场还是
    节拍停顿」必须读上下文才判得出（同一章里两种都有），没有安全的机械判据。
    引擎只出数（`markup_stats` 的 rules 计数），判定与处置留给指挥层。
    同样不碰标题行、引号、段落结构。
    """
    out, n_para, n_inline, kept = [], 0, 0, 0
    for ln in (text or "").split("\n"):
        s = ln.strip()
        # 面板行与引用块（系统播报）里的粗体是它该在的地方，整行放过
        if _BLOCKQUOTE.match(s) or _is_panel(s.replace("**", "")):
            kept += ln.count("**") // 2
            out.append(ln)
            continue
        n = ln.count("**") // 2
        if n:
            whole = s.startswith("**") and s.endswith("**") and s.count("**") == 2
            n_para += 1 if whole else 0
            n_inline += 0 if whole else n
            # **迭代到不动点**：实测真书稿里有 `****整段****` 这种嵌套写法
            # （四星一层套一层），一遍非贪婪替换只剥得掉一层，剩下的那层会让
            # 「同内容重跑幂等」当场失效——整轮操作就不再是可对账的了。
            for _ in range(4):
                nxt = _MD_STRONG.sub(r"\1", ln)
                if nxt == ln:
                    break
                ln = nxt
        out.append(ln)
    return "\n".join(out), {"paragraph_bold": n_para, "inline_bold": n_inline,
                            "kept_panel": kept}


def markup_stats(text: str) -> dict:
    """markdown 污染读数（在**原文**上算，不在剥离后的文本上算）。
    只出三个数不判：整段加粗率 / 分隔线密度 / `**` 不成对的行号。"""
    lines = (text or "").split("\n")
    paras = [p.strip() for p in lines if p.strip()]
    body = [p for p in paras if not _MD_DROP_LINE.match(p)]
    # **面板行不算污染**——粗体本来就该给它们（铁律「粗体只给面板」）。
    # 不排除的话，一本已经规范化过的书仍会因为留着的面板而报警，
    # 该指标即成为恒真误报。
    bold = sum(1 for p in body
               if p.startswith("**") and p.endswith("**")
               and not _BLOCKQUOTE.match(p) and not _is_panel(p.replace("**", "")))
    panels = sum(1 for p in body if _is_panel(p.replace("**", "")))
    rules = sum(1 for ln in lines
                if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", ln))
    odd = [i + 1 for i, ln in enumerate(lines) if ln.count("**") % 2]
    chars = len(_NON_CJK.sub("", strip_markup(text))) or 1
    return {"paragraphs": len(body), "panel_paragraphs": panels,
            "bold_para_ratio": round(bold / len(body), 3) if body else 0.0,
            "bold_paragraphs": bold,
            "rules_per_k": round(rules * 1000.0 / chars, 2), "rules": rules,
            "unpaired_lines": odd[:10], "unpaired": len(odd)}


def _sentences(text: str) -> list[str]:
    return [s for s in (x.strip() for x in _SENT_SPLIT.split(text or "")) if s]


_TRI_LIST = re.compile(r"[^，。！？、\s]{1,6}、[^，。！？、\s]{1,6}、[^，。！？、\s]{1,6}")
_SIMILE = re.compile(r"仿佛|似乎|宛如|如同|好像|犹如|彷佛")
_ELLIPSIS = re.compile(r"…{2,}|\.{6,}")
MATTR_W = 500          # 移动平均窗口（字符 bigram 数）


def mattr(text: str, window: int = MATTR_W) -> float | None:
    """字符 bigram 的 MATTR（移动平均型符比）——中文词汇多样度的**免分词**替身。

    为什么不是 TTR：TTR 随文本变长必然下降，跨长度不可比。为什么不是 jieba 分词
    后的 MTLD：引擎 `pyproject.dependencies = []` 是既有事实边界，且分词器本身会
    引入版本漂移。相邻二字组在中文里近似词/词素单元，跳窗取均值后长度无关。

    **短于一个窗口就返回 None 而不是退化成 TTR**：退化值系统性偏高（几十个 bigram
    几乎不重复，算出来接近 1.0），与真正窗口化的读数根本不可比——混在一起会让
    「基线 1.0 / 实测 0.25」这种荒唐的偏离报出来。测不了就说测不了，同
    consistency 的「没有料可比绝不等于比对通过」纪律。
    """
    ch = _CJK.findall(text or "")
    bg = [ch[i] + ch[i + 1] for i in range(len(ch) - 1)]
    if len(bg) < window:
        return None
    vals = [len(set(bg[i:i + window])) / window
            for i in range(0, len(bg) - window + 1, 100)]
    return round(sum(vals) / len(vals), 3)


def prose_stats(text: str, *, raw: str | None = None) -> dict:
    """单段文本的文体可测量量（纯计算·无外部依赖）。

    `text` 应当**已经过 strip_markup**；传 `raw` 则一并给出 markdown 污染读数。
    """
    text = text or ""
    sents = _sentences(text)
    lens = [len(_NON_CJK.sub("", s)) for s in sents]
    lens = [n for n in lens if n]
    n = len(lens)
    mean = sum(lens) / n if n else 0.0
    var = sum((x - mean) ** 2 for x in lens) / n if n else 0.0
    sd = var ** 0.5
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    heads: dict[str, int] = {}
    for p in paras:
        k = _NON_CJK.sub("", p)[:4]
        if len(k) >= 3:
            heads[k] = heads.get(k, 0) + 1
    dial = sum(len(m.group(0)) for m in _QUOTES.finditer(text))
    slop = [{"term": t, "n": text.count(t), "hint": h}
            for t, h in PROSE_SLOP.items() if text.count(t)]
    slop.sort(key=lambda x: -x["n"])
    body = len(_NON_CJK.sub("", text)) or 1
    for it in slop:
        it["per_k"] = round(it["n"] * 1000.0 / body, 2)
    out = {
        "chars": len(text), "sentences": n,
        "avg_sentence_len": round(mean, 1), "sd_sentence_len": round(sd, 1),
        "sd_ratio": round(sd / mean, 2) if mean else 0.0,
        "long_ratio": round(sum(1 for x in lens if x >= 35) / n, 2) if n else 0.0,
        "short_ratio": round(sum(1 for x in lens if x <= 8) / n, 2) if n else 0.0,
        "dialogue_ratio": round(dial / len(text), 2) if text else 0.0,
        "paragraphs": len(paras),
        "head_repeats": sorted(((k, v) for k, v in heads.items() if v >= 3),
                               key=lambda kv: -kv[1])[:5],
        "slop": slop,
        # —— 带区指标（PROSE_BANDS 消费）：全部 O(n) 纯计数，零依赖
        "long40_ratio": round(sum(1 for x in lens if x >= 40) / n, 3) if n else 0.0,
        "emo_punct_per_k": round(
            (text.count("！") + text.count("？")
             + len(_ELLIPSIS.findall(text))) * 1000.0 / body, 2),
        "tri_list_per_k": round(len(_TRI_LIST.findall(text)) * 1000.0 / body, 2),
        "simile_per_k": round(len(_SIMILE.findall(text)) * 1000.0 / body, 2),
        "mattr": mattr(text),
    }
    if raw is not None:
        out["markup"] = markup_stats(raw)
    return out


# 带区判定与 baseline z 分共用的指标集（顺序即打印顺序）
BAND_KEYS = ("long40_ratio", "simile_per_k", "tri_list_per_k", "mattr")
METRIC_KEYS = BAND_KEYS + ("avg_sentence_len", "sd_ratio", "emo_punct_per_k",
                           "dialogue_ratio")


def band_findings(ps: dict) -> list[dict]:
    """按 PROSE_BANDS 判越界——只出读数与方向，绝不合成任何「AI 味总分」。

    刻意不给合成分：权重是凭经验定的、跨语料不可移植；合成分一旦落地会被
    当作阈值门槛使用，而它不具备这种精度。
    """
    out = []
    for k, (lo, hi, zh, hint) in PROSE_BANDS.items():
        v = ps.get(k)
        if v is None:
            continue
        if lo is not None and v < lo:
            out.append({"key": k, "label": zh, "value": v, "side": "low",
                        "band": (lo, hi), "hint": hint})
        elif hi is not None and v > hi:
            out.append({"key": k, "label": zh, "value": v, "side": "high",
                        "band": (lo, hi), "hint": hint})
    return out


def repeat_phrases(texts: list[str], *, n: int = REPEAT_N,
                   minimum: int = REPEAT_MIN, top: int = 6,
                   nos: list[int] | None = None) -> list[dict]:
    """跨章复读检测：句内 n 字滑窗定位 → **最大延伸**成完整复读句。

    两条刻意的选择：
      · **不跨句读**——跨句的 n-gram 只会拼出无意义的碎片；
      · **不过滤人名**——「孙缘深吸一口气」这种连人名一起复读的句式正是要抓的。
    最大延伸不可省：只报 6 字滑窗会让同一句话以「仿佛凝固时间」
    「佛凝固时间仿」「凝固时间仿佛」这种位移一字的碎片刷满整张表，人根本看不出
    复读的是哪句话。故取到滑窗后按「所有出现位置的下一个字都相同」向两侧长满，
    再把它覆盖的全部子窗口标记吃掉。
    """
    docs, owner = [], []
    for ti, t in enumerate(texts):
        for s in _sentences(t):
            docs.append(_NON_CJK.sub("", s))
            owner.append(nos[ti] if nos and ti < len(nos) else ti + 1)
    pos: dict[str, list[tuple[int, int]]] = {}
    for di, body in enumerate(docs):
        for i in range(len(body) - n + 1):
            pos.setdefault(body[i:i + n], []).append((di, i))
    hot = sorted(((g, p) for g, p in pos.items() if len(p) >= minimum),
                 key=lambda gp: (-len(gp[1]), gp[0]))
    out: list[dict] = []
    taken: set[str] = set()
    for g, ps in hot:
        # 纯数字/字母窗口一律不报：标点被 _NON_CJK 剥掉后「07:33」「11:00」这类
        # 时间戳会粘成 07331100，在带面板/时钟的题材里天天重复却毫无文体意义
        if g in taken or len(_CJK.findall(g)) < 4:
            continue
        r = l = 0
        while True:
            nxt = {docs[d][i + n + r] if i + n + r < len(docs[d]) else None for d, i in ps}
            if len(nxt) == 1 and None not in nxt:
                r += 1
            else:
                break
        while True:
            prv = {docs[d][i - l - 1] if i - l - 1 >= 0 else None for d, i in ps}
            if len(prv) == 1 and None not in prv:
                l += 1
            else:
                break
        d0, i0 = ps[0]
        phrase = docs[d0][i0 - l:i0 + n + r]
        for k in range(len(phrase) - n + 1):
            taken.add(phrase[k:k + n])
        if any(phrase in o["phrase"] or o["phrase"] in phrase for o in out):
            continue
        # 章号定位：内部本来就握着每个出现位置的 doc index，用完就扔等于让
        # 「350 章的书上一条没有出处的告警」——而 SKILL 第④门明确要求带出处
        where = sorted({owner[d] for d, _ in ps})
        out.append({"phrase": phrase, "n": len(ps), "chapters": where})
        if len(out) >= top:
            break
    return out


def _window(series, frm: int | None, to: int | None) -> list[dict]:
    """取 [frm, to] 区间的已登记章（缺省 = 最近 PROSE_WINDOW 章）。"""
    chapters = sorted((registry(series).get("chapters") or []),
                      key=lambda c: int(c.get("no") or 0))
    if not chapters:
        return []
    hi = int(to) if to else int(chapters[-1]["no"])
    lo = int(frm) if frm else max(1, hi - PROSE_WINDOW + 1)
    return [c for c in chapters if lo <= int(c.get("no") or 0) <= hi]


def view(data: dict) -> dict:
    """创作总览的**纯只读**视图（Studio scanner 消费；绝不 mutate 入参——
    scanner 是只读扫描层，setdefault 式的就地补键会污染下发缓存语义）。"""
    reg = data.get("novel") or {}
    chapters = sorted((reg.get("chapters") or []),
                      key=lambda c: int(c.get("no") or 0))
    count = len(chapters)
    return {
        "chapters": chapters,
        "count": count,
        "total_chars": int(reg.get("total_chars") or 0),
        "threads": _threads_view(data),
        "arcs": _arcs_view(data),
        "narrative_style": data.get("narrative_style") or {},
        "log": log_view(data, limit=8),
        # 检查点按**章号**派生（与 brief() 同口径）：按章数算的话，接盘导入
        # 51~63 章后 count=13，「下一检查点第 20 章」在最新章号 63 面前是反向区间，
        # 前端会生成「续写 64~20 章」直接发给 agent 执行
        "current_no": _latest_no(data),
        "next_checkpoint": (_latest_no(data) // MILESTONE_EVERY + 1) * MILESTONE_EVERY,
        "checkpoint_due": (_latest_no(data) > 0
                           and _latest_no(data) % MILESTONE_EVERY == 0),
        "milestone_every": MILESTONE_EVERY,
    }


# ---------------------------------------------------------------------------
# 跨章确定性 lint（纯计算·不落盘·零成本）——只出可测量量与提醒，
# 「文风崩没崩/人设 OOC 没有」是指挥层三门自检的活，这里绝不判。
# ---------------------------------------------------------------------------
def lint(series, *, frm: int | None = None, to: int | None = None) -> dict:
    """跨章确定性体检。`frm`/`to` 只框**文体扫描窗口**（忌讳词/口癖/复读/离散度）——
    断号、缺件、伏笔、缺席、卷覆盖这些账目性检查恒看全书（它们本来就是 O(章数)）。
    缺省窗口 = 最近 PROSE_WINDOW 章，正好对上一次检查点覆盖的批次。"""
    findings: list[dict] = []
    reg = registry(series)
    chapters = sorted((reg.get("chapters") or []),
                      key=lambda c: int(c.get("no") or 0))
    nos = [int(c.get("no") or 0) for c in chapters]
    cur = nos[-1] if nos else 0

    def add(level, code, msg):
        findings.append({"level": level, "code": code, "msg": msg})

    if not chapters:
        add("info", "empty", "尚无登记章节（novel save 登记后再 lint）")
        return {"findings": findings, "chapters": 0, "total_chars": 0}

    # 断号
    gaps = [n for n in range(nos[0], nos[-1] + 1) if n not in set(nos)]
    if gaps:
        add("info", "gap", f"章号断档: {', '.join(map(str, gaps))}")
    # 逐章必做缺失
    no_digest = [c["no"] for c in chapters if not (c.get("digest") or "").strip()]
    if no_digest:
        add("warn", "digest_missing",
            f"{len(no_digest)} 章缺精简大纲: 第 {', '.join(map(str, no_digest[:12]))} 章"
            + ("…" if len(no_digest) > 12 else ""))
    no_state = [c["no"] for c in chapters if not c.get("state")]
    if no_state:
        add("info", "state_missing",
            f"{len(no_state)} 章缺章末状态快照: 第 {', '.join(map(str, no_state[:12]))} 章"
            + ("…" if len(no_state) > 12 else ""))
    # 稿件漂移：磁盘正文与登记块对不上（手改正文后没 novel save 的直接后果——
    # lint 的文体面读磁盘、账目面读 registry，两边就此不同源；entities 也停在
    # 陈旧快照上，缺席判据与「首次登场」跟着一起失真）
    drift, orphan, missing = [], [], []
    for c in chapters:
        p = series.dir / chapter_relpath(int(c["no"]))
        if not p.is_file():
            missing.append(int(c["no"]))
            continue
        raw = p.read_text(encoding="utf-8")
        if text_fp(raw) != (c.get("sha256") or "") or len(raw) != int(c.get("chars") or 0):
            drift.append(int(c["no"]))
    known = {int(c.get("no") or 0) for c in chapters}
    for p in sorted(manuscript_dir(series).glob("ch[0-9]*.md")):
        m = re.fullmatch(r"ch(\d+)", p.stem)
        if m and int(m.group(1)) not in known:
            orphan.append(int(m.group(1)))
    if drift:
        add("warn", "manuscript_drift",
            f"{len(drift)} 章磁盘正文与登记块对不上（改了正文没重新登记）: "
            f"第 {_brief_list(drift)} 章 → `novel reindex {series.pid} --archive`")
    if missing:
        add("warn", "manuscript_missing",
            f"{len(missing)} 章登记了但文件不在: 第 {_brief_list(missing)} 章")
    if orphan:
        add("info", "manuscript_orphan",
            f"{len(orphan)} 份正文在盘上但没登记: 第 {_brief_list(orphan)} 章"
            f"（`novel save` 登记，否则 brief/recap/lint 都看不见它）")

    # 伏笔账本：超期逐条报（有期限就是承诺过），长期挂起只报最久的几条 + 一行余量
    tv = threads_view(series)
    for t in tv["expired"]:
        add("warn", "thread_expired",
            f"伏笔超期未回收: {t['id']}「{t.get('title')}」埋于第 {t.get('setup')} 章，"
            f"期限第 {t.get('due')} 章，现已写到第 {cur} 章")
    stale = sorted((t for t in tv["open"]
                    if t.get("due") is None
                    and cur - int(t.get("setup") or cur) >= STALE_THREAD_WINDOW),
                   key=lambda t: int(t.get("setup") or cur))
    for t in stale[:3]:
        add("warn" if (t.get("tier") or "") != "long" else "info", "thread_stale",
            f"伏笔长期挂起（无期限）: {t['id']}「{t.get('title')}」"
            f"埋于第 {t.get('setup')} 章，已挂 {cur - int(t['setup'])} 章"
            + ("" if t.get("tier") else "——`novel thread-set --tier short|mid|long` "
               "定档给它一个期限，不填 due 正好是让告警静音的那个动作"))
    if len(stale) > 3:
        add("info", "thread_stale_more",
            f"另有 {len(stale) - 3} 条无期限伏笔长期挂起（"
            + "、".join(f"{t['id']}挂{cur - int(t['setup'])}章" for t in stale[3:8])
            + ("…" if len(stale) > 8 else "") + "）")

    # 角色缺席（出场过才提示；从 entities 登记统计，确定性）。三刀降噪：
    # ① status 非 active 的一律不报（长篇里永久退场是常态不是异常）；
    # ② 阈值按该角色自己的历史出场间隔自适应（配角本来就隔很久出一次）；
    # ③ 只逐条报最近跨阈的几个，其余折叠一行——实测 350 章一次报 21 条，
    #    把真正要看的 8 条伏笔和 6 条复读全淹了，恒报即等于不报。
    roster = {c.get("name"): c for c in (series.data.get("characters") or [])
              if isinstance(c, dict) and c.get("name")}
    seen_at: dict[str, list[int]] = {}
    for c in chapters:
        for name in ((c.get("entities") or {}).get("characters") or []):
            seen_at.setdefault(name, []).append(int(c["no"]))
    absent = []
    for name, hits in sorted(seen_at.items()):
        if (roster.get(name, {}).get("status") or "active") != "active":
            continue
        last = hits[-1]
        gaps = [b - a for a, b in zip(hits, hits[1:])] or [0]
        typical = sorted(gaps)[len(gaps) // 2]
        if cur - last >= max(ABSENCE_WINDOW, typical * 3):
            absent.append((cur - last, name, last))
    absent.sort(reverse=True)
    for n, name, last in absent[:3]:
        add("info", "char_absent",
            f"角色「{name}」自第 {last} 章后连续 {n} 章未出场"
            f"（已退场就 `character set {series.pid} --name {name} --status departed`，"
            "标了就不再报）")
    if len(absent) > 3:
        add("info", "char_absent_more",
            f"另有 {len(absent) - 3} 个角色长期未出场（"
            + "、".join(f"{name}({n}章)" for n, name, _ in absent[3:10])
            + ("…" if len(absent) > 10 else "")
            + "）——用 --status departed 逐个标掉退场的，这张表才有意义")
    # 卷/幕规划覆盖（大纲落点体检：断档/重叠/写出规划外）
    av = arcs_view(series)
    if not av["arcs"]:
        add("info", "arc_missing",
            "尚无卷/幕规划（`novel arc <pid> --no 1 --title … --from 1 --to 30`）——检查点第一门「有没有跑偏大纲」"
            "将无对照物，长篇上这一门等于空转")
    for g in av["gaps"]:
        add("info", "arc_gap",
            f"卷规划断档: 第 {g['at'][0]}~{g['at'][1]} 章不属于任何一卷"
            f"（在第 {g['after']} 卷与第 {g['before']} 卷之间）")
    for o in av["overlaps"]:
        add("warn", "arc_overlap",
            f"卷区间重叠: 第 {o['a']} 卷与第 {o['b']} 卷同时覆盖"
            f"第 {o['at'][0]}~{o['at'][1]} 章")
    if av["uncovered_from"] is not None:
        add("info", "arc_uncovered",
            f"已写到第 {cur} 章，但卷规划只排到第 {av['uncovered_from'] - 1} 章"
            "——下一卷该立纲了")
    for a in av["arcs"]:
        if a["state"] == "done" and a.get("to") is not None and int(a["to"]) == cur:
            add("info", "arc_done",
                f"第 {a['no']} 卷「{a.get('title') or ''}」本章收卷"
                f"（第 {a.get('from')}~{a.get('to')} 章）——该做卷末复盘了")

    # ---- 节奏账（第⑦门 · opt-in）：只算间隔与连用，一个字都不判「爽不爽」
    #      分工照视频侧「英雄时刻」标记的既有先例（引擎不读、只供 lint 与人审取舍；
    #      此处刻意不写那个字段名——test_variation 用源级扫描钉死它的消费面）：
    #      等级与钩型由指挥层声明（novel save --payoff/--hook），没声明就整段不报。
    findings.extend(_pacing_findings(chapters, series.data, cur))

    # ---- 文体面（窗口化）：全部在 **strip_markup 之后**算
    style = series.data.get("narrative_style") or {}
    avoid = [w for w in (style.get("avoid") or []) if w]
    win = _window(series, frm, to)
    wnos = [int(c["no"]) for c in win]
    raws = [(read_chapter_text(series, n) or "") for n in wnos]
    texts = [strip_markup(t) for t in raws]
    span = (f"第 {wnos[0]}~{wnos[-1]} 章" if len(wnos) > 1
            else (f"第 {wnos[0]} 章" if wnos else "—"))
    joined, joined_raw = "\n".join(texts), "\n".join(raws)

    def _where(term: str, top: int = 3) -> str:
        """命中落在哪几章——350 章的书上没有出处的告警等于没有告警。"""
        hits = sorted(((t.count(term), n) for t, n in zip(texts, wnos) if term in t),
                      reverse=True)
        if not hits:
            return ""
        return ("（第 " + "/".join(str(n) for _, n in hits[:top]) + " 章"
                + (f" 等 {len(hits)} 章" if len(hits) > top else "") + "）")

    for w in avoid:
        n = joined.count(w)
        if n:
            add("warn", "style_avoid", f"{span}命中忌讳词「{w}」×{n}{_where(w)}")
    if joined.strip():
        ps = prose_stats(joined, raw=joined_raw)
        # markdown 污染：正文不是排版，`**` 与 `---` 不是作者写的字
        mk = ps["markup"]
        if mk["bold_para_ratio"] > 0.05 or mk["unpaired"]:
            add("warn", "prose_markup",
                f"{span}正文里有 markdown 排版: **非面板**整段加粗 "
                f"{mk['bold_paragraphs']} 段（{int(mk['bold_para_ratio'] * 100)}%；"
                f"另有 {mk['panel_paragraphs']} 段面板行属正常）· 分隔线 {mk['rules']} 行"
                + (f" · `**` 不成对 {mk['unpaired']} 行（行 "
                   + ",".join(map(str, mk["unpaired_lines"])) + "）" if mk["unpaired"] else "")
                + f" → `novel normalize {series.pid}` 一键剥掉非面板加粗"
                  "（逐章走 save，旧稿进版本栈可回滚）；`---` 它刻意不碰——"
                  "那条是断场还是节拍停顿要读上下文才判得出")
        # 忌讳词与口癖同源时只报一次（同一处出两条＝把一个问题报成两个）
        hot = [it for it in ps["slop"]
               if it["n"] >= SLOP_MIN_HITS
               and not any(w in it["term"] or it["term"] in w for w in avoid)]
        for it in hot[:SLOP_TOP]:
            add("warn", "prose_slop",
                f"{span} AI 味口癖「{it['term']}」×{it['n']}"
                f"（每千字 {it['per_k']}）{_where(it['term'])} → {it['hint']}")
        if len(hot) > SLOP_TOP:
            add("info", "prose_slop_more",
                f"{span}另有 {len(hot) - SLOP_TOP} 个口癖命中（"
                + "、".join(f"{it['term']}×{it['n']}" for it in hot[SLOP_TOP:])
                + "）——全表见 `novel recap`")
        # 文体带区：越界只出**一条**折叠 finding（各报各的会把其余结论淹掉）
        bands = band_findings(ps)
        if bands:
            add("info", "prose_bands",
                f"{span}文体读数越界 {len(bands)} 项 · "
                + " ｜ ".join(
                    f"{b['label']} {b['value']}"
                    f"（{'低于' if b['side'] == 'low' else '高于'}带区 "
                    f"{b['band'][0] if b['side'] == 'low' else b['band'][1]}）→ {b['hint']}"
                    for b in bands))
        # 自基线 z 分（有 baseline_metrics 才算；z 恒现算不落盘，同伏笔超期纪律）
        per_ch = [(n, prose_stats(t)) for n, t in zip(wnos, texts) if t.strip()]
        for d in _style_drift(style.get("baseline_metrics") or {}, per_ch):
            add("warn", "style_drift", f"{span}{d}")
        if not (style.get("baseline") or []):
            add("info", "no_baseline",
                "narrative_style.baseline 为空——文风门没有基线样本可锚，**这一门是空转**"
                "（不是通过）：从已认可正文摘 2~3 段 `novel style --add-baseline`，"
                f"再 `novel baseline {series.pid} --from 1 --to 10` 立数值基线")
        elif not (style.get("baseline_metrics") or {}):
            add("info", "no_baseline_metrics",
                "有基线样本但没有数值基线——文风门只能靠肉眼比对，"
                f"跑一次 `novel baseline {series.pid} --from N --to M` 就有 z 分了")
        if ps["dialogue_ratio"] < DIALOGUE_MIN and ps["chars"] > 1500:
            add("info", "prose_narration_heavy",
                f"{span}对白占比 {int(ps['dialogue_ratio'] * 100)}%"
                "——几乎全是叙述（冷叙述是合法文体，只是提请确认这是有意为之）")
        for k, n in ps["head_repeats"][:3]:
            add("info", "prose_head_repeat",
                f"{span}有 {n} 个段落以「{k}」开头{_where(k)}"
                "——段首主语雷同是复读感的来源")
        for r in repeat_phrases(texts, nos=wnos):
            add("info", "prose_repeat",
                f"{span}复读句「{r['phrase'][:36]}"
                + ("…" if len(r["phrase"]) > 36 else "") + f"」×{r['n']}"
                + "（第 " + "/".join(map(str, r["chapters"][:4]))
                + ("…" if len(r["chapters"]) > 4 else "") + " 章）")
        # 硬规则：SKILL 已明令、正则完全能算的那几条（逐章判，带章号）
        for code, (pat, zh, cap, hint) in PROSE_RULES.items():
            rx = re.compile(pat)
            per = [(len(rx.findall(t)), n) for t, n in zip(texts, wnos)]
            bad = sorted(((k, n) for k, n in per if k > cap), reverse=True)
            if bad:
                add("warn", code,
                    f"{span}{zh} 超限（每章上限 {cap}）: 共 {sum(k for k, _ in bad)} 处 / "
                    f"{len(bad)} 章 · 最多的是第 "
                    + "、".join(f"{n}章({k}处)" for k, n in bad[:3])
                    + f" → {hint}")

    # 篇幅节奏：中位数取全书，但只报窗口内的章（否则第 75 章会一直报到第 1000 章）
    lens = sorted(int(c.get("chars") or 0) for c in chapters)
    med = lens[len(lens) // 2]
    if med > 0:
        for c in win:
            n = int(c.get("chars") or 0)
            if n < med * 0.5:
                add("info", "short_chapter", f"第 {c['no']} 章 {n} 字（<中位 {med} 的一半）")
            elif n > med * 2:
                add("info", "long_chapter", f"第 {c['no']} 章 {n} 字（>中位 {med} 两倍）")

    return {"findings": findings, "chapters": len(chapters),
            "total_chars": int(reg.get("total_chars") or 0), "current_no": cur,
            "window": [wnos[0], wnos[-1]] if wnos else None,
            "counts": _count_by(findings, "code"),
            "levels": _count_by(findings, "level")}


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _style_drift(base: dict, per_chapter: list) -> list[str]:
    """对自基线的 z 分。|z|>2 才报，并点名是**哪一章的哪个分量**在漂。

    绝对带区有一个必须诚实说明的缺陷：可得的公版人类语料是民国白话，拿它当
    现代网文的标尺不公平。用作者自己认可的前 N 章当标尺则完全公平——这把
    「防漂靠 baseline 比对，绝不逐章复述风格」那条纪律从「指挥层脑内比对」
    落成了一个可对账的数。

    **必须逐章比，绝不拿整窗合并文本比**：baseline 是「逐章算完再取 μ±σ」，
    而十章合并后的文本在若干指标上根本不是同一个量纲——尤其词汇多样度，
    合并把跨章重复也计了进去，读数必然下滑。两个口径对着比会报出
    「基线 1.0 / 实测 0.25 / z=-749」这种荒唐的偏离。
    逐章比顺带还多给一样东西：**出处**。
    """
    n = int(base.get("n_chapters") or 0)
    if n < 3:                      # σ 不稳，宁可不报（少于 3 章的基线没有分布）
        return []
    out = []
    for k in METRIC_KEYS:
        mu, sd = base.get(k), base.get(k + "_sd")
        if mu is None or sd is None:
            continue
        # σ 地板取**相对量**：基线各章恰好一致时 σ→0，绝对地板会让任何微小
        # 偏差炸成三位数 z。这样至少要偏离 2% 才可能触发。
        sd = max(float(sd), abs(float(mu)) * 0.02, 1e-6)
        hits = [(no, ps[k]) for no, ps in per_chapter
                if ps.get(k) is not None and abs((ps[k] - mu) / sd) > 2]
        if hits:
            worst = max(hits, key=lambda x: abs(x[1] - mu))
            out.append(
                f"{k} 在 {len(hits)} 章偏离文风基线（基线 {mu}±{round(sd, 3)}）"
                f"——最远的是第 {worst[0]} 章 {worst[1]}"
                f"（z={(worst[1] - mu) / sd:+.1f}）")
    return out


def _pacing_findings(chapters: list[dict], data: dict, cur: int) -> list[dict]:
    """节奏账：**只出间隔与连用，绝不判「爽不爽」**（引擎零 LLM 铁律）。

    没有任何一章声明过 payoff 时整段静默——这是 opt-in 的第⑦门，不给
    「为了填表而填表」的摩擦；一旦开始声明，账就必须算全。
    """
    out: list[dict] = []
    declared = [c for c in chapters if c.get("payoff")]
    if not declared:
        return out

    def add(level, code, msg):
        out.append({"level": level, "code": code, "msg": msg})

    lo = int(declared[0]["no"])
    run, last_kind, kind_run, last_hook, hook_run = 0, None, 0, None, 0
    for c in chapters:
        no = int(c["no"])
        if no < lo:
            continue
        pay, kind, hook = c.get("payoff"), c.get("payoff_kind"), c.get("hook")
        run = 0 if pay else run + 1
        if run == PACING_FLAT:
            add("info", "pacing_flat",
                f"第 {no - run + 1}~{no} 章连续 {run} 章没有任何 payoff——"
                "平推超过三章，读者会掉；下一章给一个小兑现（哪怕只是一个疑问被证实）")
        kind_run = kind_run + 1 if (kind and kind == last_kind) else 1
        if kind and kind_run == PACING_SAME:
            add("info", "payoff_kind_repeat",
                f"第 {no - kind_run + 1}~{no} 章连着 {kind_run} 章都是「{kind}」型爽点"
                "——换一种（打脸/升级/解谜/情感/反转），同型连用即套路化")
        last_kind = kind or last_kind
        hook_run = hook_run + 1 if (hook and hook == last_hook) else 1
        if hook and hook_run == PACING_SAME:
            add("info", "hook_monotone",
                f"第 {no - hook_run + 1}~{no} 章连着 {hook_run} 章都用「{hook}」型断章"
                "——断章七型轮着来（决定/发现/误判/代价/险境/逼近/错位）")
        last_hook = hook or last_hook
    if run >= PACING_FLAT:
        add("info", "pacing_flat_tail",
            f"最近 {run} 章没有 payoff（写到第 {cur} 章）")
    for a in _arcs_view(data)["arcs"]:
        if a["state"] != "done" or a.get("from") is None:
            continue
        rng = [c for c in declared
               if int(a["from"]) <= int(c["no"]) <= int(a.get("to") or cur)]
        if rng and not any(c.get("payoff") == "major" for c in rng):
            add("info", "arc_no_major",
                f"第 {a['no']} 卷「{a.get('title') or ''}」整卷没有一个 major 爽点"
                "——一卷至少要有一个够分量的兑现，否则收卷收不住")
    return out


def overview(series) -> dict:
    """创作总览（CLI show 消费）——与 scanner 的 view(data) 同一口径。"""
    return view(series.data)


# ---------------------------------------------------------------------------
# 写前必读包（brief）——把「五处翻查」压成一次调用
# 长篇写崩的头号机制不是模型不行，是**写第 60 章时前 59 章的约束根本没进上下文**。
# 全书回灌撑爆预算、凭印象写必崩，唯一的出路是每章只取该取的那几样：
# 文风契约 / 当前卷的纲 / 在场角色的人设卡 / 上一章的收束态 / 还欠着的伏笔。
# 引擎只负责**确定性地把这几样凑齐**，读完怎么写仍然全是指挥层的活。
# ---------------------------------------------------------------------------
_PERSONA_KEYS = ("name", "role", "appearance", "outfit", "hair", "weapon",
                 "keywords", "speech_style", "personality", "arc", "taboo_lines")
BRIEF_CHAR_CAP = 12


def persona_card(c: dict) -> dict:
    """角色的**文字**设定卡（写正文/写台词要的那几样，不含设定图与音色状态）。"""
    return {k: c.get(k) for k in _PERSONA_KEYS if c.get(k)}


PERSONA_MUST = ("speech_style", "personality", "arc", "taboo_lines")


def brief(series, *, no: int | None = None, chars: list[str] | None = None,
          all_chars: bool = False, bible: list[str] | None = None) -> dict:
    """第 no 章（缺省=下一章）的写前必读包。纯只读，零成本。

    在场角色的默认口径：上一章 `state.characters` 的键 ∪ 上一章命中实体——
    「上一章谁在场」是下一章最可能要写的人，比「全书角色表」精准得多。
    点名 `chars` 覆盖默认；`all_chars` 取全表（角色多时会很长）。
    """
    data = series.data
    cur = _latest_no(data)
    nxt = int(no) if no else cur + 1
    prev = find_entry(series, nxt - 1)
    roster = {c.get("name"): c for c in (data.get("characters") or [])
              if isinstance(c, dict) and c.get("name")}
    if chars:
        unknown = [n for n in chars if n not in roster]
        want = [n for n in chars if n in roster]
    else:
        seen = list(((prev or {}).get("state") or {}).get("characters") or {})
        seen += [n for n in (((prev or {}).get("entities") or {}).get("characters") or [])
                 if n not in seen]
        # 上一章 state/命中里点到、却不在角色表里的名字 = 漏登记的设定（同 consistency
        # 的「名字写错」一档）。**必须过滤后再取卡**——直接 roster[n] 会 KeyError 把整条
        # 取料链整条中断，而这类名字（星神 ID、临时 NPC）在长篇里几乎必然出现。
        unknown = [n for n in seen if n not in roster]
        seen = [n for n in seen if n in roster]
        want = seen if (seen and not all_chars) else list(roster)
    capped = not all_chars and not chars and len(want) > BRIEF_CHAR_CAP
    if capped:
        want = want[:BRIEF_CHAR_CAP]
    tv = _threads_view(data)
    recent = [c for c in sorted((registry(series).get("chapters") or []),
                                key=lambda c: int(c.get("no") or 0))
              if int(c.get("no") or 0) < nxt][-3:]
    # 未回收伏笔按「快到期的排前面」——登记序在 28 条平铺时等于没有排序，
    # 而写这一章时真正要顾的是本章前后到期的那几条
    opens = sorted(tv["open"], key=lambda t: (
        t.get("due") is None, int(t.get("due") or 10 ** 9), int(t.get("setup") or 0)))
    cards = [persona_card(roster[n]) for n in want]
    thin = [c["name"] for c in cards
            if sum(1 for k in PERSONA_MUST if c.get(k)) < len(PERSONA_MUST)]
    # 宪法按本章相关性取节：关键词 = 上一章在场者 ∪ 上一章命中实体 ∪ 本卷目标与
    # 节拍 ∪ 未收伏笔标题。全量 195KB 的取料包在第 350 章已经贵到只剩全读/全不读
    wb = (data.get("adaptation") or {}).get("world_bible") or ""
    a = arc_at(data, nxt)
    terms = set(want) | set(((prev or {}).get("entities") or {}).get("props") or [])
    terms |= set(((prev or {}).get("entities") or {}).get("scenes") or [])
    for s in ([(a or {}).get("goal"), (a or {}).get("climax"), (a or {}).get("premise")]
              + list((a or {}).get("turns") or [])
              + [t.get("title") for t in opens[:8]]
              + [(prev or {}).get("digest")]):
        terms |= set(re.findall(r"[一-鿿]{2,6}", s or ""))
    bib = pick_bible(wb, terms, want=(bible if bible and bible != ["all"] else None))
    if bible == ["all"]:
        bib = {"sections": bible_sections(wb), "toc": bib["toc"],
               "picked": [s["title"] for s in bible_sections(wb)],
               "total": len(wb), "used": len(wb)}
    out = {
        "no": nxt,
        "narrative_style": data.get("narrative_style") or {},
        "arc": a,
        "world_bible": wb,
        "bible_toc": bib["toc"],
        "bible_sections": bib["sections"],
        "bible_picked": bib["picked"],
        "bible_total": bib["total"],
        "prev": ({"no": prev.get("no"), "title": prev.get("title"),
                  "digest": prev.get("digest"), "state": prev.get("state")}
                 if prev else None),
        "recent_digests": [{"no": c.get("no"), "title": c.get("title"),
                            "digest": c.get("digest")} for c in recent],
        "characters": cards,
        "thin_personas": thin,
        "unknown_chars": unknown,
        "chars_capped": capped,
        "open_threads": opens,
        "expired_threads": tv["expired"],
        "checkpoint_due": cur > 0 and cur % MILESTONE_EVERY == 0,
        "current_no": cur,
    }
    # 分段字数账：取料成本必须看得见，否则「为什么这么贵」永远查不出来
    sz = lambda v: len(json.dumps(v, ensure_ascii=False)) if v else 0   # noqa: E731
    out["budget"] = {
        "文风契约": sz(out["narrative_style"]), "卷纲": sz(a),
        "宪法(已选节)": sum(s["chars"] for s in bib["sections"]),
        "宪法(全量)": bib["total"],
        "上章digest+state": sz(out["prev"]),
        "未收伏笔": sz(opens), "人设卡": sz(cards),
    }
    out["budget"]["合计"] = sum(v for k, v in out["budget"].items()
                                if k != "宪法(全量)")
    return out


# ---------------------------------------------------------------------------
# 批次复核物料（recap）——十章检查点那份《批次报告》的骨架
# 「把每一章的概要给用户看」必须是**逐项数出来的**而不是凭印象复述：漏一章、
# 记错一处伏笔，用户拿到的就是一份看着完整实则失真的报告。
# ---------------------------------------------------------------------------
def recap(series, *, frm: int | None = None, to: int | None = None) -> dict:
    data = series.data
    win = _window(series, frm, to)
    if not win:
        return {"chapters": [], "from": None, "to": None, "count": 0}
    lo, hi = int(win[0]["no"]), int(win[-1]["no"])
    texts = [(read_chapter_text(series, int(c["no"])) or "") for c in win]
    # 新登场实体：首次命中章落在窗口内的（首次命中 = 全书扫一遍取 min 章号）
    first: dict[tuple[str, str], int] = {}
    for c in sorted((registry(series).get("chapters") or []),
                    key=lambda c: int(c.get("no") or 0)):
        for kind in ("characters", "props", "scenes"):
            for n in ((c.get("entities") or {}).get(kind) or []):
                first.setdefault((kind, n), int(c.get("no") or 0))
    fresh = {k: [] for k in ("characters", "props", "scenes")}
    for (kind, n), fno in sorted(first.items(), key=lambda kv: kv[1]):
        if lo <= fno <= hi:
            fresh[kind].append({"name": n, "no": fno})
    tv = _threads_view(data)
    inrange = lambda v: v is not None and lo <= int(v) <= hi   # noqa: E731
    rows = [{"no": int(c["no"]), "title": c.get("title") or "",
             "chars": int(c.get("chars") or 0),
             "digest": (c.get("digest") or "").strip(),
             "has_state": bool(c.get("state")),
             "entities": c.get("entities") or {},
             "arc": (arc_at(data, int(c["no"])) or {}).get("no")}
            for c in win]
    lens = [r["chars"] for r in rows]
    return {
        "from": lo, "to": hi, "count": len(rows), "chapters": rows,
        "total_chars": sum(lens),
        "avg_chars": int(sum(lens) / len(lens)) if lens else 0,
        "missing_digest": [r["no"] for r in rows if not r["digest"]],
        "missing_state": [r["no"] for r in rows if not r["has_state"]],
        "new_entities": fresh,
        "threads": {
            "opened": [t for t in (data.get("threads") or [])
                       if inrange(t.get("setup"))],
            "paid": [t for t in (data.get("threads") or [])
                     if t.get("status") == "paid" and inrange(t.get("paid_in"))],
            "open": tv["open"], "expired": tv["expired"],
        },
        "arcs": [a for a in _arcs_view(data)["arcs"]
                 if a.get("from") is not None
                 and int(a["from"]) <= hi
                 and (a.get("to") is None or int(a["to"]) >= lo)],
        "prose": prose_stats("\n".join(strip_markup(t) for t in texts),
                             raw="\n".join(texts)),
        "bands": band_findings(prose_stats(
            "\n".join(strip_markup(t) for t in texts))),
        "repeats": repeat_phrases([strip_markup(t) for t in texts],
                                  nos=[int(c["no"]) for c in win]),
        "pacing": {
            # 与 by_level/hooks 同源取 win（登记条目）——rows 是精简视图、没有 payoff 键
            "declared": sum(1 for c in win if c.get("payoff")),
            "by_level": _count_by(
                [{"k": c.get("payoff")} for c in win if c.get("payoff")], "k"),
            "hooks": _count_by(
                [{"k": c.get("hook")} for c in win if c.get("hook")], "k"),
        },
    }


# ---------------------------------------------------------------------------
# 世界观宪法的分节读取——长篇取料成本里**唯一随书长线性膨胀**的那一项
# `novel brief <项目>` = 195,327 字节，`--no-bible` = 16,725 字节，差额几乎
# 全是 world_bible 的 72,739 字。到第 350 章，「写前必读包」已经贵到只剩「全读」
# 与「全不读」两个选项——而这份宪法本身就是分好节的。按节取即可，且完全确定性
# （关键词命中 + 常驻节 + 预算），不违反引擎零 LLM。
# 刻意**不做字数截断**：只按节边界取舍——截半的节会把不完整的规则当完整规则交出去。
# ---------------------------------------------------------------------------
_BIBLE_HEAD = re.compile(
    r"^[ \t]*(?:#{1,4}[ \t]*)?【([^】\n]{1,40})】"
    r"|^[ \t]*#{1,3}[ \t]+(.{1,40})$", re.M)


def bible_sections(text: str) -> list[dict]:
    """把宪法切成 [{title, body, chars}]（**无损**：各节 body 拼回等于原文）。

    双格式认标题：`【零·一句话】` 与 markdown `## 标题`——前者是本仓库既有书稿
    的写法，后者是通用写法。一个都认不出时返回单节（回落整份，绝不猜）。
    """
    text = text or ""
    ms = list(_BIBLE_HEAD.finditer(text))
    if not ms:
        return ([{"title": "（全文）", "body": text, "chars": len(text)}]
                if text.strip() else [])
    out = []
    if ms[0].start() > 0:
        head = text[:ms[0].start()]
        if head.strip():
            out.append({"title": "（前言）", "body": head, "chars": len(head)})
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.start():end]
        out.append({"title": (m.group(1) or m.group(2) or "").strip(),
                    "body": body, "chars": len(body)})
    return out


# 常驻节：不管本章写什么都必须在场的那几类（写法纪律与硬禁令）。按标题子串匹配，
# 匹配不到就没有——拿不准的节不进结果。
BIBLE_ALWAYS = ("叙事纪律", "写法铁律", "伏笔纪律", "地名铁律", "人设",
                "一句话", "禁", "口径")
BIBLE_BUDGET = 12000          # 分节取料的字数上限（约 8k token）


def pick_bible(text: str, terms, *, budget: int = BIBLE_BUDGET,
               want: list[str] | None = None) -> dict:
    """按本章相关性挑宪法节。确定性打分：命中词数 → 常驻加权 → 字数升序。

    `want` 点名要哪几节（标题子串）时只按点名取，忽略打分。
    """
    secs = bible_sections(text)
    if not secs:
        return {"sections": [], "toc": [], "picked": [], "total": 0}
    toc = [{"title": s["title"], "chars": s["chars"]} for s in secs]
    if want:
        picked = [s for s in secs
                  if any(w and w in s["title"] for w in want)]
        return {"sections": picked, "toc": toc,
                "picked": [s["title"] for s in picked],
                "total": sum(s["chars"] for s in secs)}
    terms = {t for t in (terms or []) if t and len(t) >= 2}
    scored = []
    for s in secs:
        hits = sum(1 for t in terms if t in s["body"])
        always = any(k in s["title"] for k in BIBLE_ALWAYS)
        scored.append((always, hits, -s["chars"], s))
    scored.sort(key=lambda x: (-int(x[0]), -x[1], x[2]))
    picked, used = [], 0
    for always, hits, _, s in scored:
        if not always and hits < 2:
            continue
        if used + s["chars"] > budget and picked:
            continue
        picked.append(s)
        used += s["chars"]
    order = {id(s): i for i, s in enumerate(secs)}
    picked.sort(key=lambda s: order[id(s)])
    return {"sections": picked, "toc": toc,
            "picked": [s["title"] for s in picked],
            "total": sum(s["chars"] for s in secs), "used": used}


# ---------------------------------------------------------------------------
# 创作日志（novel.log[]）——append-only，跨会话的记忆锚
# 复核结论必须有落点，而另两条看起来可用的路径都不成立：`decision add --chapter`
# 在纯小说项目上报「找不到章节」（它走 video 章节加载），`arc --note` 是单值字符串、
# 第二次直接覆盖第一次。两条载体都会丢记录，故另立 append-only 字段。
# 去重纪律照抄 decisions.py：缺 id 时用内容派生键并**就地补进条目**，否则
# 无 id 条目会在合并的内存/磁盘两侧各留一份，每 save 翻倍。
# ---------------------------------------------------------------------------
LOG_KINDS = ("checkpoint", "decision", "overhaul", "note")


def log_entry_key(e: dict) -> str:
    eid = str(e.get("id") or "").strip()
    if eid:
        return eid
    seed = "|".join(str(e.get(k) or "") for k in ("kind", "at_chapter", "text"))
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def log_add(series, *, kind: str, text: str, at: int | None = None,
            ref: str | None = None) -> dict:
    if kind not in LOG_KINDS:
        raise ProjectError(f"--kind 只收 {'/'.join(LOG_KINDS)}：{kind}")
    if not (text or "").strip():
        raise ProjectError("创作日志 text 为空——记一条没内容的日志等于没记")
    with series.commit():
        reg = registry(series)
        rows = reg.setdefault("log", [])
        e = {"kind": kind, "text": str(text).strip(), "at": _now()}
        if at is not None:
            e["at_chapter"] = int(at)
        if ref:
            e["ref"] = str(ref).strip()
        e["id"] = log_entry_key(e)
        if not any(log_entry_key(x) == e["id"] for x in rows):
            rows.append(e)
        out = dict(e)
    return out


def log_view(data: dict, *, kind: str | None = None, limit: int = 0) -> list[dict]:
    rows = [dict(e) for e in ((data.get("novel") or {}).get("log") or [])
            if not kind or e.get("kind") == kind]
    return rows[-limit:] if limit else rows


# ---------------------------------------------------------------------------
# 跨层检索（sweep）——「改设定＝改七层」的收工判据
# 一条设定被推翻时，正文与角色卡是最显眼的两层，而 digest/state 台账、arcs 卷纲、
# graph 关系图谱照样留着旧措辞——手工检索必漏其中几层，这种纯机械的活正该下沉进
# 引擎；**判定（哪条是合法留存）仍归指挥层**，引擎只出清单。
# ---------------------------------------------------------------------------
SWEEP_LAYERS = ("manuscript", "entities", "digest", "state", "arcs",
                "threads", "bible")
_SWEEP_TOP = 5


def _walk_strings(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_strings(v, f"{path}[{i}]")


def sweep(series, term: str, *, min_len: int = 2) -> dict:
    """逐层检索一个词，出命中数与出处。纯只读、零落盘。"""
    term = (term or "").strip()
    if len(term) < min_len:
        raise ProjectError(
            f"检索词至少 {min_len} 字（短词会把七层都刷满；--min-len 可放宽）：{term!r}")
    data = series.data
    layers = {k: [] for k in SWEEP_LAYERS}

    def hit(layer, where, line):
        layers[layer].append({"where": where, "line": line.strip()[:120]})

    for c in sorted((registry(series).get("chapters") or []),
                    key=lambda c: int(c.get("no") or 0)):
        no = int(c.get("no") or 0)
        raw = read_chapter_text(series, no)
        if raw:
            for i, ln in enumerate(strip_markup(raw).split("\n"), 1):
                if term in ln:
                    hit("manuscript", f"第{no}章:{i}", ln)
        if term in (c.get("digest") or ""):
            hit("digest", f"第{no}章", c["digest"])
        for p, s in _walk_strings(c.get("state") or {}):
            if term in s:
                hit("state", f"第{no}章 state.{p}", s)
    for kind, zh in (("characters", "角色"), ("props", "道具"), ("scenes", "场景")):
        for it in (data.get(kind) or []):
            for p, s in _walk_strings(it):
                if term in s:
                    hit("entities", f"{zh}「{it.get('name')}」.{p}", s)
    for a in (data.get("arcs") or []):
        for p, s in _walk_strings(a):
            if term in s:
                hit("arcs", f"卷{a.get('no')}.{p}", s)
    for t in (data.get("threads") or []):
        for p, s in _walk_strings(t):
            if term in s:
                hit("threads", f"{t.get('id')}.{p}", s)
    bible = {"world_bible": (data.get("adaptation") or {}).get("world_bible") or "",
             "mainline": (data.get("adaptation") or {}).get("mainline") or "",
             "logline": (data.get("design") or {}).get("logline") or "",
             "narrative_style": data.get("narrative_style") or {},
             "graph": data.get("graph") or {}}
    for p, s in _walk_strings(bible):
        if term not in s:
            continue
        if p.startswith("world_bible"):
            for i, ln in enumerate(s.split("\n"), 1):
                if term in ln:
                    hit("bible", f"world_bible:{i}", ln)
        else:
            hit("bible", p, s)
    total = sum(len(v) for v in layers.values())
    return {"term": term, "total": total,
            "layers": {k: {"n": len(v), "rows": v[:_SWEEP_TOP],
                           "more": max(0, len(v) - _SWEEP_TOP)}
                       for k, v in layers.items()}}


# ---------------------------------------------------------------------------
# 稿件重登记与章级回滚
# 七层扫描的第①层要求直接改正文，而改完不 `novel save` 就会留下一份对不上的账：
# 登记块的 sha256 与字数停在改稿前，旧稿既没进版本栈也没重登记，entities 跟着停在
# 陈旧快照上。反向路径同样要有——save_chapter 一直在归档旧稿，得有「拿回来」的入口
# 与之配对（对应视频侧的 versions rollback）。
# ---------------------------------------------------------------------------
def version_files(series, no: int) -> list[dict]:
    out = []
    for p in sorted(_versions_dir(series).glob(f"ch{int(no):04d}_v*.md")):
        m = re.search(r"_v(\d+)\.md$", p.name)
        out.append({"v": int(m.group(1)) if m else 0,
                    "file": f"manuscript/versions/{p.name}",
                    "chars": len(p.read_text(encoding="utf-8"))})
    return sorted(out, key=lambda x: x["v"])


def reindex(series, *, no: int | None = None, archive: bool = False) -> dict:
    """按磁盘正文重算 chars/sha256/entities 回写登记块。

    `archive=True` 时把「登记块认为的那一版」不可得（磁盘已被改写）这一事实
    如实处理：只把**当前磁盘稿**另存一份进版本栈当留档，绝不伪造历史版本。
    顺带解决「后补 keywords / 后加角色不回溯旧章 entities」——实体命中是快照。
    """
    reg = registry(series)
    chapters = sorted((reg.get("chapters") or []),
                      key=lambda c: int(c.get("no") or 0))
    todo = [c for c in chapters
            if no is None or int(c.get("no") or 0) == int(no)]
    if no is not None and not todo:
        raise ProjectError(f"第 {no} 章尚未登记")
    fixed, kept, gone, snaps = [], [], [], []
    for c in todo:
        n = int(c["no"])
        p = series.dir / chapter_relpath(n)
        if not p.is_file():
            gone.append(n)
            continue
        raw = p.read_text(encoding="utf-8")
        sha, ents = text_fp(raw), entity_mentions(series, raw)
        same = sha == (c.get("sha256") or "") and len(raw) == int(c.get("chars") or 0)
        if same and ents == (c.get("entities") or {}):
            kept.append(n)
            continue
        if archive and not same:
            v = len(version_files(series, n)) + 1
            vf = _versions_dir(series) / f"ch{n:04d}_v{v:03d}.md"
            vf.write_text(raw, encoding="utf-8")
            snaps.append({"no": n, "v": v,
                          "file": f"manuscript/versions/{vf.name}"})
        fixed.append(n)
    with series.commit():
        reg = registry(series)
        for n in fixed:
            e = find_entry(series, n)
            raw = (series.dir / chapter_relpath(n)).read_text(encoding="utf-8")
            e.update({"chars": len(raw), "sha256": text_fp(raw),
                      "entities": entity_mentions(series, raw),
                      "file": chapter_relpath(n), "updated_at": _now()})
        for s in snaps:
            find_entry(series, s["no"]).setdefault("versions", []).append(
                {"v": s["v"], "file": s["file"], "at": _now(),
                 "reason": "reindex-snapshot"})
        reg["total_chars"] = sum(int(c.get("chars") or 0)
                                 for c in (reg.get("chapters") or []))
        reg["updated_at"] = _now()
    return {"fixed": fixed, "kept": kept, "missing": gone, "archived": snaps}


def normalize(series, *, no: int | None = None, dry_run: bool = False) -> dict:
    """按 `normalize_markup` 规范化正文排版，逐章走 `save_chapter` 登记。

    走 save 而不是直接写盘是必需的：每一章的旧稿因此自动进 `manuscript/versions/`，
    整轮操作**可逐章回滚**（`novel revert --no N`）。同内容的章幂等跳过。
    """
    rows = sorted((registry(series).get("chapters") or []),
                  key=lambda c: int(c.get("no") or 0))
    todo = [c for c in rows if no is None or int(c.get("no") or 0) == int(no)]
    if no is not None and not todo:
        raise ProjectError(f"第 {no} 章尚未登记")
    changed, agg, rules = [], {"paragraph_bold": 0, "inline_bold": 0, "kept_panel": 0}, 0
    for c in todo:
        n = int(c["no"])
        raw = read_chapter_text(series, n)
        if raw is None:
            continue
        new, st = normalize_markup(raw)
        rules += markup_stats(raw)["rules"]
        for k in agg:
            agg[k] += st[k]
        if new == raw:
            continue
        changed.append({"no": n, "before": len(raw), "after": len(new), **st})
        if not dry_run:
            save_chapter(series, no=n, text=new)
    return {"changed": changed, "scanned": len(todo), "totals": agg,
            "rules": rules, "dry_run": dry_run}


def revert(series, *, no: int, v: int | None = None) -> dict:
    """章级回滚：当前稿先归档（reason=rollback-out）→ 历史版拷回 → 重登记。
    纪律照 versioning.rollback_asset（归档是移动/留档，回滚是拷回）。"""
    no = int(no)
    vers = version_files(series, no)
    if not vers:
        raise ProjectError(f"第 {no} 章没有历史版本（manuscript/versions/ 为空）")
    pick = (next((x for x in vers if x["v"] == int(v)), None)
            if v is not None else vers[-1])
    if pick is None:
        raise ProjectError(f"第 {no} 章没有 v{v}（现有: "
                           + ", ".join(f"v{x['v']}" for x in vers) + "）")
    dest = series.dir / chapter_relpath(no)
    out_v = None
    if dest.is_file():
        out_v = len(vers) + 1
        dest.rename(_versions_dir(series) / f"ch{no:04d}_v{out_v:03d}.md")
    text = (series.dir / pick["file"]).read_text(encoding="utf-8")
    dest.write_text(text, encoding="utf-8")
    with series.commit():
        e = find_entry(series, no)
        if e is None:
            raise ProjectError(f"第 {no} 章尚未登记")
        if out_v:
            e.setdefault("versions", []).append(
                {"v": out_v, "file": f"manuscript/versions/ch{no:04d}_v{out_v:03d}.md",
                 "at": _now(), "reason": "rollback-out"})
        e.update({"chars": len(text), "sha256": text_fp(text),
                  "entities": entity_mentions(series, text), "updated_at": _now()})
        reg = registry(series)
        reg["total_chars"] = sum(int(c.get("chars") or 0)
                                 for c in (reg.get("chapters") or []))
    return {"no": no, "restored": pick, "archived_v": out_v, "chars": len(text)}


# ---------------------------------------------------------------------------
# 数值文风基线（baseline_metrics）——把「防漂靠基线比对」从脑内动作落成数
# ---------------------------------------------------------------------------
def baseline_metrics(series, *, frm: int, to: int) -> dict:
    """在作者认可的那批**整章**上算指标向量（μ 与 σ），落 narrative_style。"""
    win = [c for c in sorted((registry(series).get("chapters") or []),
                             key=lambda c: int(c.get("no") or 0))
           if int(frm) <= int(c.get("no") or 0) <= int(to)]
    if len(win) < 3:
        raise ProjectError(
            f"第 {frm}~{to} 章只有 {len(win)} 章已登记——基线至少要 3 章"
            "（少于 3 章的 σ 不稳，算出来的 z 分是噪声）")
    per = []
    for c in win:
        t = strip_markup(read_chapter_text(series, int(c["no"])) or "")
        if t.strip():
            per.append(prose_stats(t))
    out = {"n_chapters": len(per), "from": int(frm), "to": int(to), "at": _now()}
    for k in METRIC_KEYS:
        xs = [p[k] for p in per if p.get(k) is not None]
        if not xs:
            continue          # 整窗测不出（None）的指标没有分布，不落基线
        mu = sum(xs) / len(xs)
        sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5
        if mu == 0 and sd == 0:
            # 计数型指标（明喻/超长句/三连排比）在正常章节常整窗为 0——落成
            # μ=0/σ=0.001 的基线后，此后任何一章出现一次即 z≈+400 恒响，
            # 还与 prose_bands「该多写比喻」的下限建议正面冲突。无分布不落基线，
            # 该管的仍由带区（PROSE_BANDS）管，z 分只管「偏离自己的常态」
            continue
        out[k] = round(mu, 3)
        out[k + "_sd"] = round(sd, 3) or 0.001      # σ=0 会让 z 爆炸
    with series.commit():
        series.data.setdefault("narrative_style", {})["baseline_metrics"] = out
    return out


# ---------------------------------------------------------------------------
# 文风契约与世界观宪法的写路径
# 这两块是第①⑤门的判据本体，也是最贵的两块，故与 threads/arcs 同制度走命令写入：
# 没有 setter 就只剩「裸改 project.json」一条路，而那条路是明令禁止的。
# ---------------------------------------------------------------------------
_STYLE_SETTABLE = ("pov", "tense", "voice", "diction")


def style_update(series, *, add_baseline: list[str] | None = None,
                 rm_baseline: int | None = None,
                 add_avoid: list[str] | None = None,
                 rm_avoid: list[str] | None = None, **fields) -> dict:
    bad = [k for k in fields if k not in _STYLE_SETTABLE]
    if bad:
        raise ProjectError(f"novel style 只收 {'/'.join(_STYLE_SETTABLE)}，未知字段: {bad}")
    with series.commit():
        st = series.data.setdefault("narrative_style", {})
        for k, v in fields.items():
            if v is not None:
                st[k] = str(v).strip()
        base = st.setdefault("baseline", [])
        if rm_baseline is not None:
            i = int(rm_baseline)
            if not 1 <= i <= len(base):
                raise ProjectError(
                    f"没有第 {i} 段基线样本（现有 {len(base)} 段）")
            base.pop(i - 1)
        for seg in (add_baseline or []):
            if seg.strip() and seg.strip() not in base:
                base.append(seg.strip())
        av = st.setdefault("avoid", [])
        for w in (rm_avoid or []):
            if w in av:
                av.remove(w)
        for w in (add_avoid or []):
            if w and w not in av:
                av.append(w)
        out = dict(st)
    return out


def bible_set(series, text: str, *, section: str | None = None,
              append: bool = False) -> dict:
    """写世界观宪法：整份替换 / 按节替换 / 追加。节标题按子串唯一匹配。"""
    text = (text or "").rstrip() + "\n"
    with series.commit():
        ad = series.data.setdefault("adaptation", {})
        cur = ad.get("world_bible") or ""
        if section:
            secs = bible_sections(cur)
            hit = [s for s in secs if section in s["title"]]
            if len(hit) != 1:
                have = "、".join(s["title"] for s in secs) or "（宪法为空）"
                raise ProjectError(
                    f"节标题「{section}」匹配到 {len(hit)} 节，必须唯一。现有节: {have}")
            new = "".join(text if s is hit[0] else s["body"] for s in secs)
        elif append:
            new = (cur.rstrip() + "\n\n" + text) if cur.strip() else text
        else:
            new = text
        ad["world_bible"] = new
        n, secs = len(new), len(bible_sections(new))
    return {"chars": n, "sections": secs, "mode":
            ("section" if section else "append" if append else "replace")}


# ---------------------------------------------------------------------------
# 导出（export）——正文合并成一份 .txt/.md。
# 手工 `cat *.md` 走的是**文件名字典序**，断号时静默错位，还会把 versions/
# 旧稿一起收进去。按 registry 章序拼，断号只跳过并在末尾如实列出。
# ---------------------------------------------------------------------------
def export(series, *, frm: int | None = None, to: int | None = None,
           strip: bool = False, out: str | None = None) -> dict:
    rows = sorted((registry(series).get("chapters") or []),
                  key=lambda c: int(c.get("no") or 0))
    rows = [c for c in rows
            if (frm is None or int(c["no"]) >= int(frm))
            and (to is None or int(c["no"]) <= int(to))]
    if not rows:
        raise ProjectError("窗口内没有已登记章节")
    parts, missing = [], []
    for c in rows:
        n = int(c["no"])
        raw = read_chapter_text(series, n)
        if raw is None:
            missing.append(n)
            continue
        body = strip_markup(raw) if strip else raw
        if strip:
            body = f"第{n}章 {c.get('title') or ''}\n\n" + body.strip()
        parts.append(body.strip())
    text = "\n\n\n".join(parts) + "\n"
    d = series.dir / "exports"
    d.mkdir(parents=True, exist_ok=True)
    p = (Path(out) if out
         else d / f"{series.pid}_ch{int(rows[0]['no'])}-{int(rows[-1]['no'])}"
                  f"{'_plain' if strip else ''}.txt")
    p.write_text(text, encoding="utf-8")
    return {"file": str(p), "chapters": len(parts), "chars": len(text),
            "missing": missing,
            "range": [int(rows[0]["no"]), int(rows[-1]["no"])]}
