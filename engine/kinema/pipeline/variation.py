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

"""分镜单调度 lint（软闸）+ 反 slop 空词表。

**这是一道软闸：只提示、绝不阻断、绝不落盘、绝不抛异常。**
调用方（`lint` 子命令 / `stage_gen_image` 生图前 / Studio 章节详情条幅）拿到的
永远是一份 `Finding` 列表——空列表表示这份分镜单在这几个维度上没话说，
不表示"片子一定好"。lint 结论**不写进 project.json**（与 `spec check` 同为纯计算命令）。

维度清单（都只吃 shots[]/characters[] 的字面统计量，不做语义理解）：
  · 相邻运镜雷同     —— 连续两镜 camera 归一后相同（"缓慢推近：…" 取冒号前的技法名）
  · 情绪缺失/单调     —— 有台词镜没写 emotion（TTS 无情绪起伏），或全片只有一种情绪
  · 景别分布单一     —— framing 归一到桶后种类过少（全片一个景别 = 观感呆板）
  · 反 slop 空词     —— 提示词里的"唯美/氛围感/高级感"等零视觉信息词，逐条给物理化改写建议
  · 抽象情绪词       —— 提示词把情绪写成名词（"愤怒/悲伤/紧张"）而不是身体事实——
                      模型渲染不出情绪标签，只会退回最平庸的那张脸（表演物理化纪律）
  · 画面代词        —— image_prompt/video_prompt/action/end_state 里用「他/她/它」
                      指代——设定图按 name/keywords 命中才自动挂载，代词挂不上图
  · 占位旁白        —— TODO/待定/占位 之类没删干净的草稿文本，以及跨镜完全重复的旁白
  · 旁白文风        —— 抬价句式（"你以为…其实…"）、汇报腔词、名词化公文腔、
                      收尾宏大词、跨镜同连接词开头——机器写作最稳的几个句式指纹
  · 旁白语态        —— 旁白不是必填件：剧情档（sparse 缺省）旁白镜占比超限＝
                      把漫剧写成了解说；语态由 skill 派生、顶层 voiceover 可声明
  · 视觉换挡间距     —— 按 scenes/framing/angle/light_shift/转场五个结构化位累加
                      时间轴，连续超 30s 无一次可见换挡即点名该区间（info 提示）
  · hero_moment    —— 一镜没标（节奏没想过）或标得太多（全标等于没标）
  · 设定图覆盖度    —— 本集用到的 emotion ∖ 角色 required_emotions（M8 三个
                      required_* 字段的唯一引擎消费点，不做它们就是死字段）
  · 多镜语法        —— video_prompt 里写了「Shot 1/Shot 2」「镜头一/镜头二」这类
                      一段提示词切多镜的写法（一镜一文件的制度下拆不出第二段）
  · 复述重合率      —— video_prompt 与 image_prompt 的字符 n-gram 重合率过高
                      （视频请求恒带分镜图，复述首帧=最强漂移源，只该写增量）

阈值由**顶层 `art_direction` 旋钮**驱动：`variety`/`motion`/`density` 三个
1-10 数值 + `avoid[]` 点名回避词。**旋钮只改告警，永不改画面**——不换运镜、
不改提示词、不影响成本。映射函数写死在本模块（`_camera_repeat_budget` 等），
schema 只声明取值范围，实现口径以本模块为准（tests/test_variation.py 守护）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import skills
from .. import sketchboard
from ..project import chapter_title_number, effective_motion, scored_audio, uses_seedance
from .. import voicebank
from .. import voicecast
from .. import review
from . import asr
from . import transitions


# ---------------------------------------------------------------- 反 slop 词表
# 「空词」= 说了等于没说的主观形容——模型无法把它渲染成像素，只会退回训练集均值
# （千篇一律的"AI 感"）。右值是**物理化改写方向**：把主观评价换成可被镜头拍到的
# 光线/材质/构图/动作。词表刻意放模块级常量而不进 config/*.yaml——它是提示词
# 策略的一部分（照 EFFECT_META / TRANSITIONS 惯例），进 yaml 会凭空多一条
# test_config_drift 内嵌一致性维护链。
SLOP_TERMS: dict[str, str] = {
    "唯美": "改写成具体光线与色彩：如「逆光暖调，发丝边缘透光，背景高光散成圆形光斑」",
    "精美": "说清材质与工艺：如「錾刻银饰，表面细密凿痕，边缘磨出包浆」",
    "精致": "说清材质与工艺：如「哑光陶瓷杯身，杯口一圈手绘金线」",
    "华丽": "写纹样密度与材质：如「层叠织金锦缎，缠枝纹密布，走动时暗纹反光」",
    "高质量": "删掉——画质由画风前缀与 --resolution 决定，写进提示词只挤占有效 token",
    "高清": "删掉——分辨率是引擎参数（--resolution），不是画面内容",
    "大师级": "删掉，或换成具体流派/媒介：如「厚涂油画笔触，可见笔刀刮痕」",
    "杰作": "删掉——这类自我夸奖对国产模型无效，只会稀释真正的画面描述",
    "完美": "删掉，或指出到底哪里要严整：如「左右完全对称的中轴构图」",
    "极致": "删掉，改写成可量化的极端值：如「画面九成被阴影吞没，只留一线光」",
    "震撼": "写清视觉冲击的来源：如「前景巨物入画占据三分之二画面，人只到它脚踝」",
    "史诗感": "写尺度对比：如「人物剪影只占画面十分之一，身后是百米高的城墙」",
    "氛围感": "写空气介质：如「雾气与浮尘在斜射光束里翻涌，远处景物被雾吃掉一层」",
    "电影感": "写摄影参数：如「浅景深，主体清晰背景化开，冷调高对比，光比约 4:1」",
    "高级感": "写配色与材质克制度：如「低饱和莫兰迪灰，哑光表面，几乎没有高光」",
    "细节丰富": "点名具体三处细节：如「袖口磨破的线头、指节的旧疤、鞋跟粘的枯叶」",
    "栩栩如生": "写生理细节：如「呼吸时鼻翼微张，眼球有湿润反光，睫毛根根分明」",
    "生动": "写动作瞬间：如「衣角被风掀起的那一瞬，重心还未落回」",
    "美丽": "写五官与神态：如「杏眼下垂，唇角抿成一条线，眉心一点朱砂」",
    "漂亮": "写具体外形特征，或换成可见的装饰物、色彩、光泽",
    "好看": "删掉——这是评价不是描述，模型接不住",
    "神秘": "写遮蔽关系：如「面孔一半没入阴影，只露出一只反光的眼睛」",
    "梦幻": "写光学现象：如「柔焦光晕，粉紫渐变天空，空气里漂浮着发亮的孢子」",
    "治愈": "写暖光与柔和轮廓：如「午后斜射的暖光铺在地板上，家具圆角，低对比」",
    "温馨": "写光源与人物距离：如「一盏橘黄台灯，两人肩挨着肩，杯子还冒热气」",
    "张力": "写身体姿态与构图：如「两人目光交锋，画面留白被挤压在他们中间」",
    "情绪饱满": "直接写表情肌肉动作：如「眉头拧成川字，下颌绷紧，太阳穴青筋凸起」",
    "诗意": "写具体意象：如「一只白鹭掠过水面，涟漪把倒影撕开」",
    "独特": "删掉，改写成到底哪里不同：如「左袖是空的，用银链别在肩头」",
    "超现实": "写具体反常物理：如「书页从桌面向上飘落，影子却朝相反方向拉长」",
}

# ---------------------------------------------------------------- 抽象情绪词表
# 表演物理化纪律：镜头拍得到的是**身体**，拍不到"愤怒"这个名词。提示词里把情绪
# 写成标签，模型只会给出训练集均值的那张脸（微皱眉+抿嘴，什么情绪都长这样）；
# 写成可拍的身体事实——视线、手部、重心、生理应激——表演才立得住。示范句不用
# 叹气、深呼吸、湿眼眶：它们是模型演过头的惯用形态，表演地板按镜压着。
# 与 SLOP_TERMS 分表：那张管"零视觉信息的评价词"，这张管"该演不该说的情绪名词"，
# 扫描面也不同（本表连 action/end_state 两个 delta 骨架位一起扫）。
# 词表单一真源在此，方法论详见 .claude/skills/kinema/references/performance.md。
EMOTION_TERMS: dict[str, str] = {
    "愤怒": "写身体事实：如「下颌绷紧，指节抵着桌面发白，上身前压半寸」",
    "悲伤": "写生理与动作：如「视线落在桌角不动，杯子忘了放下，肩线沉下去」",
    "难过": "写动作与他注意到的实物：如「脚步停了半拍，盯着那只空了的碗」",
    "恐惧": "写应激细节：如「瞳孔收紧，喉结滚动一下，后背抵上墙才停住」",
    "害怕": "写退避动作：如「半步后撤，手在身侧攥住衣角，视线不敢落定」",
    "紧张": "写小动作：如「指尖反复摩挲杯沿，咽了一次口水，视线在门口与桌面之间来回」",
    "绝望": "写姿态塌陷：如「肩线垮下去，手从膝上滑落，视线失焦落在地面」",
    "崩溃": "写失控的那一瞬：如「手里的东西脱手落地，人顺着墙滑坐下去」",
    "委屈": "写强忍的中间态：如「嘴唇抿紧，别过脸去，手指抠着衣角」",
    "失望": "写落空动作：如「伸出的手停在半空又收回，视线落回桌面」",
    "震惊": "写定格反应：如「动作停在半途，双眼睁大，嘴唇微张没发出声音」",
    "痛苦": "写身体收缩：如「弓着背，一只手死死按住肋侧，额角渗汗」",
    "开心": "写笑的层次：如「眼睛先弯起来，肩膀跟着轻颤，露出一点牙」",
    "幸福": "写具体亲密细节：如「两人肩线相抵，她把头搁上去，热气从杯口飘起来」",
}

# ---------------------------------------------------------------- 不可拍摄词
# 与 EMOTION_TERMS 并列的兄弟表，走同一条通道（`_lint_abstract_emotion` 的扫描面），
# 但**发不同的 code**：抽象情绪是"该写身体、写了心情"，不可拍摄词是"根本没有对应画面"
# ——前端与测试按 code 断言，混成一条就分不清是哪类问题。
#
# 选词纪律：**宁可漏报不误报**。初版收过「明白」「感到」两个词，实测全仓唯一命中是
# 「半透明白色属性面板」里的跨词「明白」——真阳性 0、假阳性 1，直接砍掉。
# 剧本改编路径最容易把这类词带进来（小说原句"他意识到危险"照抄进 image_prompt）。
UNFILMABLE_TERMS: dict[str, str] = {
    "意识到": "写可拍的那一瞬：如「瞳孔收缩，手指在桌下攥紧，动作停了半拍」",
    "心里想": "内心戏拍不出来——改写成外化动作：如「张了张嘴又闭上，把纸条揉进掌心」",
    "回忆起": "回忆没有画面：要么给一个具体的触发物（他捏住那枚旧徽章），要么另起一镜闪回",
    "陷入回忆": "同上：镜头拍不出「陷入」——写视线失焦落在哪件实物上，动作停在哪一半",
    "下定决心": "写决心的动作外化：如「把烟摁灭在杯沿，站起身时椅子向后蹭出半尺」",
}

# ---------------------------------------------------------------- 运镜互斥
# 运镜类目表：把 `camera` 与 `video_prompt` 各映射成**类目集合**，两集合都非空
# 且**交集为空**时才判互斥。
#
# 判据经三轮实测收敛，记下淘汰过的两种免得退回去：
#   · 「字面互斥对」（推近↔拉远、环绕↔锁定构图…）漏报——推近↔环绕、推近↔跟随、
#     推进↔下降这几类关系不在对表里；
#   · 「集合不相等就报」误报率近半——`video_prompt` 延展 `camera` 是常态，不是冲突。
# 现行判据额外要求 **vp 侧只取含摄影词的小句**：`static` 预设原文写着「只有画面内的
# 主体与环境在运动」，不筛小句会把「身体往下沉了半寸」这类主体运动当成运镜冲突。
_CAMERA_MOVE_TERMS: dict[str, tuple[str, ...]] = {
    "推进": ("推近", "推进", "推镜", "前推", "拉近"),
    "后拉": ("拉远", "后拉", "后退", "退远", "拉开"),
    "环绕": ("环绕", "绕行", "绕着", "旋转围绕", "转半圈", "四分之一圈"),
    "升": ("升起", "上升", "拔升", "升镜", "航拍升"),
    "降": ("下降", "降下", "落下", "俯冲", "下沉镜"),
    "跟随": ("跟拍", "跟随", "追随", "并行跟"),
    "固定": ("固定机位", "镜头完全静止", "机位锁死", "纹丝不动的机位", "构图居中锁定",
             "大小不变"),
    "摇移": ("摇摄", "横移", "平移", "甩镜", "摇至", "摇到"),
    "变焦": ("变焦", "焦距", "推拉变焦"),
}
# vp 里必须出现这些词，那一小句才算在谈摄影机——否则谈的是主体在动
_CAMERA_SUBJECT_WORDS = ("镜头", "机位", "视角", "摄影机", "仰拍", "俯拍", "航拍",
                         "特写", "全景", "中景", "近景", "远景", "景别",
                         "低角度", "高角度", "第一人称", "构图")
# 小句里排在运镜词前面的否定词：「特写不做任何环绕」是在排除一种运镜，不是在写它。
# 只认明确的排除措辞，不收单字「不」——「幅度不超过百分之三」这类限幅句里的
# 「不」与运镜无关
_CAMERA_NEGATIONS = ("不做", "不要", "不许", "不得", "绝不", "禁止", "避免",
                     "不加", "不带", "不用", "没有", "无")


def _camera_cats(text: str, *, skip_negated: bool = False) -> set[str]:
    """文本命中的运镜类别。`skip_negated` 时跳过小句内被否定词排除的命中。"""
    out: set[str] = set()
    for cat, words in _CAMERA_MOVE_TERMS.items():
        for w in words:
            i = text.find(w)
            while i >= 0:
                if not (skip_negated and any(n in text[:i] for n in _CAMERA_NEGATIONS)):
                    out.add(cat)
                    break
                i = text.find(w, i + 1)
    return out


def _camera_clauses(text: str) -> list[str]:
    """从自由文本里挑出**谈摄影机**的小句（按中文标点切）。"""
    import re as _re
    return [c for c in _re.split(r"[，。；、\n]", text or "")
            if any(w in c for w in _CAMERA_SUBJECT_WORDS)]


def _lint_camera_clash(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """`camera` 与 `video_prompt` 各写了一个互相排斥的运镜。

    两字段被引擎串成一句发出（`运镜：<camera>，<video_prompt>`），两条互斥指令并列时
    模型只能挑一个或各做一半。典型形态：`camera` 写「固定机位，镜头完全静止」而
    `video_prompt` 写「镜头从低处缓缓升起」，或希区柯克变焦（后退+构图锁定）撞上
    「缓慢环绕半圈」。

    **有交集就放过**：`video_prompt` 延展 `camera` 是正常写法，
    「升镜 + 镜头缓缓升起后环绕」不是冲突。"""
    out: list[Finding] = []
    hits, samples = [], []
    for s in shots:
        cam = str(s.get("camera") or "").strip()
        vp = str(s.get("video_prompt") or "").strip()
        if not cam or not vp:
            continue
        a = _camera_cats(cam)
        # 逐小句判否定：否定词只对同一小句内排在它后面的运镜词生效
        b: set[str] = set()
        for clause in _camera_clauses(_strip_quoted(vp)):
            b |= _camera_cats(clause, skip_negated=True)
        if a and b and not (a & b):
            hits.append(s.get("id"))
            if len(samples) < 3:
                samples.append(f"镜{s.get('id')}「{'/'.join(sorted(a))}」"
                               f"↔「{'/'.join(sorted(b))}」")
    if hits:
        out.append(Finding(
            "camera_clash", "warn",
            f"{len(hits)} 镜的 camera 与 video_prompt 各写了一个互斥运镜"
            f"（{'；'.join(samples)}）", tuple(hits),
            "两者会被拼成一句发出，模型只能挑一个或各做一半：留一个主导运镜，"
            "另一个要么删、要么拆成下一镜；确实要两轴（推+摇）就在同一句里写清主次"))
    return out


def _placeholder_presets() -> dict[str, tuple[str, ...]]:
    """预设库里仍带填空位（`X`/`Y`）的档 → 该档的原文片段。

    **从 `camera.CAMERA_PRESETS` 派生、绝不另写词表**：填空位的措辞在 `camera.py` 与
    `references/storyboard.md` 之间已有一道逐字节锁步守卫（`test_camera_presets`），
    再硬编一份就是第三份副本——人工维护的词表迟早漏掉填空位（英文位尤其易漏）。"""
    from .camera import CAMERA_PRESETS
    out: dict[str, tuple[str, ...]] = {}
    for key, p in CAMERA_PRESETS.items():
        frags = tuple(str(p.get(f) or "") for f in ("phrase", "phrase_en")
                      if _has_placeholder(str(p.get(f) or "")))
        if frags:
            out[key] = frags
    return out


def _has_placeholder(text: str) -> bool:
    """裸的大写 X / Y（两侧都不是字母数字）——预设库留的填空位就是这个形态。

    判据刻意做成**结构性**而不是词表匹配：这样 `camera.py` 改填空写法、加档、
    删档，这里都不用跟着改一个字。中文运镜措辞里不会出现裸 X/Y，
    英文侧「from X angle」「past X then」同样命中。"""
    import re as _re
    return bool(_re.search(r"(?<![0-9A-Za-z])[XY](?![0-9A-Za-z])", text or ""))


def _lint_preset_placeholder(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """预设库的填空位原样落盘：`camera` 里还留着 `X角度`/`前景的X`/`穿过X`。

    抄库即带占位符——`references/storyboard.md` 与 `camera.py` 的 phrase 本就写着
    X/Y 等作者替换（如 `从X角度…摆至Y角度`），照抄不改就会原样发给模型。"""
    known = _placeholder_presets()
    hits, samples = [], []
    for s in shots:
        for field in ("camera", "video_prompt"):
            text = str(s.get(field) or "")
            if not _has_placeholder(text):
                continue
            hits.append(s.get("id"))
            if len(samples) < 3:
                src = next((k for k, frags in known.items()
                            if any(f[:12] and f[:12] in text for f in frags)), None)
                samples.append(f"镜{s.get('id')} 的 {field}"
                               + (f"（像是 {src} 预设没填）" if src else ""))
            break
    if not hits:
        return []
    return [Finding(
        "preset_placeholder", "warn",
        f"{len(hits)} 镜残留运镜预设的填空位 X/Y（{'；'.join(samples)}）",
        tuple(dict.fromkeys(hits)),
        "X/Y 是预设库留给作者替换的填空位（当前 "
        f"{'/'.join(sorted(known))} 等档带它），必须换成本镜的实物或角度；"
        "留着不报错，但一旦跑 gen-video（或切 --motion native/dubbed）"
        "就会原样发给模型")]


def _lint_unregistered_entity(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """逐镜显式点名了 `characters`/`props`/`scenes`，却在项目名册里查不到。

    **纯查表、零猜测**：只判「作者显式写了非空清单」这一种形态，不从正文抽专名
    ——那是中文分词活，抽错一个专名就把一条假告警钉在作者身上。
    点名而查不到的代价是静默的：设定图挂不上，模型对着一个它不认识的名字自由发挥。

    `characters` 缺省（=全部出场）与显式空表 `[]`（=明确无人）都跳过：
    前者没点名、后者点的是"没有"，两种都不是"点了名但查不到"。"""
    known = {kind: {str((e or {}).get("name") or "").strip()
                    for e in (ctx.get(kind) or []) if isinstance(e, dict)}
             for kind in ("characters", "props", "scenes")}
    for kind in known:
        known[kind].discard("")
    hits, samples = [], []
    for s in shots:
        for kind in ("characters", "props", "scenes"):
            named = s.get(kind)
            if not isinstance(named, list) or not named:
                continue
            if not known[kind]:
                continue           # 名册整个没建，不是"查不到"
            for n in named:
                n = str(n or "").strip()
                if n and n not in known[kind]:
                    hits.append(s.get("id"))
                    if len(samples) < 4:
                        samples.append(f"镜{s.get('id')} 的 {kind}「{n}」")
    if not hits:
        return []
    return [Finding(
        "unregistered_entity", "warn",
        f"{len(set(hits))} 镜点名了名册里没有的条目（{'；'.join(samples)}）",
        tuple(dict.fromkeys(hits)),
        "要么把它建进设定集（`character add` / `prop add` / `scene add`），"
        "要么改成名册里的注册名——点了一个不存在的名字，设定图挂不上，"
        "模型对着这个名字自由发挥")]


def _CRAFT_LEAK_RE():
    import re as _re
    return _re.compile(r"(v\d+(\.\d+)*\b|上[一次]版|上次[^，。]{0,6}崩|"
                       r"分镜图|简笔板|底片|"
                       r"\.(png|jpg|jpeg|mp4|json|md)\b|[A-Za-z_]+\.py\b|"
                       r"\bJ-\d{2}\b|gotcha#\d+)")


def _lint_craft_leak(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """工艺痕迹漏进交付文本：版本号 / 文件名 / 规则 ID / 工序词 / 「上次…崩坏」句式。

    工序词（分镜图/简笔板/底片）是流水线内部的产物称谓：「分镜图改为无面部构图」
    是写给流水线的工程指令，编译进提示词后模型只能按字面把这些词当画面内容处理。

    交付文本只该含交付内容。`review.note` 会被引擎编译进下一版提示词
    （`prompts.video_prompt` 的「本次修正重点（务必执行）：…」），写「参考 shot_3.png
    那版」等于让模型去找一个它看不见的东西；画面字段里出现 `v2`/`J-07` 同理。

    **引擎自己写的 note 不算漏**：`lineage.mark_stale` 的过期提示不含文件名
    （文件清单留在 `stale_refs` 供 Studio 展示）——过期提示若把文件名拼进 note，
    本维度就会对着引擎自己的产物报警，维度自造噪声比不报还糟。"""
    pat = _CRAFT_LEAK_RE()
    hits, samples = [], []
    for s in shots:
        texts = [_picture_text(s)]
        for stage in ("image", "clip", "shot"):
            note = ((s.get("review") or {}).get(stage) or {}).get("note")
            if note:
                texts.append(str(note))
        for t in texts:
            m = pat.search(t)
            if m:
                hits.append(s.get("id"))
                if len(samples) < 3:
                    samples.append(f"镜{s.get('id')}「{m.group(0)}」")
                break
    if not hits:
        return []
    return [Finding(
        "craft_leak", "warn",
        f"{len(hits)} 镜的交付文本混进了工艺痕迹（{'；'.join(samples)}）",
        tuple(dict.fromkeys(hits)),
        "版本号/文件名/判例号只有我们看得懂——模型看不到上一版，"
        "把意见改写成这一版要什么（「刃光收速再慢一档，末帧停在双手上」）")]


# ---------------------------------------------------------------- 画面代词
# 画面字段（image_prompt/video_prompt/action/end_state）一律用角色名点名，
# 不用「他/她/它」——两层理由：① 模型不知道代词指谁，多人镜必然张冠李戴；
# ② 引擎的设定图自动挂载按 name/keywords 在提示词里**文本命中**（project.py
# 的 _matched_entities），写「她拿起它」命中率为零，等于这一镜没喂设定图。
# 排除「其他/其它/吉他」类合成词（前置字符负断言）；对白（narration/lines）
# 不在扫描面内——台词里的代词是人话。
PRONOUN_RE = re.compile(r"(?<![其吉])[他她它](?:们)?")
# 画面字段里原文引用的台词（说出「她明天出院了。」）是人话不是画面：引号内的
# 代词与运镜词都不属于画面描述，扫描前整段剥掉
_QUOTED_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"")


def _strip_quoted(text: str) -> str:
    return _QUOTED_RE.sub("", text or "")

# ---------------------------------------------------------------- 旁白文风
# 旁白/台词是念出来的文案，有自己的一组机器指纹——与提示词空词完全两回事，
# 因此独立成表、独立走 _lint_narration_style 通道（_lint_slop 的扫描面由测试
# 钉死为提示词，绝不扩）。三类：
#   ① 抬价句式——先替观众虚构一个他并没有的误会，再靠推翻它抬高下一句。这是 AI 写
#      "反常识钩子"的默认句法，也是最容易被观众读出机器味的姿势；换字面
#      仍是同一个动作，所以按**句式**拦而不是按词拦。
#   ② 汇报腔词——商业汇报与议论文的词汇渗进口播（赋能/抓手/闭环…），念出来
#      像开会不像说话。
#   ③ 名词化——动词被压成名词挂在"进行/实现"后面（"实现了收入的增长"），
#      公文腔的第一指纹，口播里尤其致命（听众没有回读能力）。
NARRATION_PIVOT_RE = re.compile(
    r"你以为[^。！？\n]{1,24}[，,]?\s*(?:其实|结果)"
    r"|不是[^。！？\n]{1,20}[，,]\s*而是"
    r"|与其说[^。！？\n]{1,20}[，,]?\s*(?:不如说|倒不如)"
    r"|看似[^。！？\n]{1,20}[，,]?\s*(?:实则|其实)"
    r"|表面上[^。！？\n]{1,24}[，,]?\s*(?:其实|实际上)"
    r"|[^。！？\n]{0,12}并?不重要[，,][^。！？\n]{0,12}重要的是"
    r"|从来(?:都)?不是"
    r"|恰恰相反"
    r"|回头(?:再)?(?:看|想)[，,]?\s*才(?:发现|明白|知道)")

NARRATION_SLOP: dict[str, str] = {
    "赋能": "说清谁帮谁做成了什么事：「让小店也能收线上订单」",
    "抓手": "直接说出那个具体动作或工具本身",
    "闭环": "说清链路两端真实发生了什么：「用户提了意见，三天后功能上线」",
    "底层逻辑": "直接讲那条逻辑本身，一句话说完",
    "顶层设计": "说清是谁定了什么规则",
    "降本增效": "给数字：省了多少钱、快了多少天",
    "全链路": "点名链路上真实的几站，别打包",
    "组合拳": "把几件事各自说清楚，观众记的是事不是拳",
    "方法论": "直接给那个方法的第一步",
    "颗粒度": "说清到多细：按天还是按小时",
    "心智": "说人真实的想法或习惯：「一想到奶茶就想到它」",
    "范式": "说清旧做法和新做法各是什么",
    "打法": "说清具体动作与先后顺序",
    "想象空间": "说清到底还能做成什么事，说不出就删",
    "认知升级": "说清原来以为什么、后来知道了什么",
    "生态位": "说清它在谁和谁之间、靠什么活",
    "值得注意的是": "删掉路标直接说事——注意力由内容本身抓，不靠预告",
    "需要指出的是": "删掉这个路标，直接指出那件事",
    "从某种意义上说": "删掉——要么就是这个意思，要么就不是",
    "不可否认": "删掉，直接陈述那个事实",
    "众所周知": "众所周知的事不用说；不众所周知就给出处",
}

NOMINAL_RE = re.compile(
    r"(?:进行|开展)(?:了|一次|一场)?[^。！？\n]{0,10}?"
    r"(?:优化|处理|排查|改造|调整|部署|梳理|评估|升级|复盘|沟通|规划)"
    r"|实现了?[^。！？\n]{0,10}?(?:提升|增长|突破|转变|落地|跃升)"
    r"|完成了?对[^。！？\n]{1,12}的"
    r"|起到了?[^。！？\n]{0,8}作用"
    r"|具有[^。！？\n]{0,10}(?:意义|价值)")

# 收尾宏大词：结尾升华是机器收尾的默认动作——整条片子从没讲到这个量级的事，
# 末镜突然抬到"时代/人类"只会显空。只扫**最后一个有台词的镜**（CTA 位），
# 且只提示不判错：史诗/纪录题材本来就在这个尺度上，判断权在指挥层。
GRAND_WORDS: tuple[str, ...] = ("时代", "文明", "历史", "世界", "未来", "人类", "奇迹", "所有人")

# 跨镜同连接词开头：一份分镜单里三镜以上用同一个连接词起头（"其实…其实…其实…"），
# 复读感比用词重复更明显——它是整页的形状。只认虚词连接词，不管实词开头
# （旁白连续以主角名起头是正常叙事，不在此列）。
OPENER_MARKS: tuple[str, ...] = (
    "其实", "所以", "但是", "然而", "而且", "然后", "接下来", "更重要",
    "当你", "如果", "没错", "是的", "换句话", "也就是")

# ---------------------------------------------------------------- 旁白语态
# **旁白不是分镜的必填件**。语态三档的缺省由画风归属 skill 派生（单一真源
# skills._VOICEOVER_DEFAULTS：剧情类 sparse、解说类 lead、纯氛围 none），章节
# 顶层 `voiceover` 显式声明凌驾缺省。sparse 档镜镜都配旁白＝把漫剧写成了解说：
# 对白该进 lines[]/speaker，动作与战斗该是纯画面镜。
VOICEOVER_HEAVY_RATIO = 0.4     # sparse 档旁白镜占正镜比的上限


def voiceover_mode(data: dict) -> str:
    """章节生效语态：顶层 voiceover 显式声明 > skill/画风缺省。永不抛异常。"""
    d = data if isinstance(data, dict) else {}
    v = str(d.get("voiceover") or "").strip().lower()
    if v in skills.VOICEOVER_MODES:
        return v
    return skills.voiceover_default(d.get("profile"), d.get("skill"))


_voice_kind = voicecast.voice_kind   # 真源已上移到 voicecast（提示词侧同一判据要用）

# 语态判据的样本下限：3 镜里 2 镜有旁白说明不了任何事，占比对小样本没有意义。
VOICEOVER_MIN_SHOTS = 4


def voiceover_overrun(shots: list[dict], mode: str) -> tuple[int, int] | None:
    """旁白镜是否超出声明语态的容许量 → `(旁白镜数, 正镜数)`；未超/不判返回 None。

    lint 的 `voiceover_heavy` 与 `cli` 的付费前语态闸共用这一个判据。两处各写一份
    会得到「lint 说没超、闸说超了」这种自相矛盾的状态——语态、阈值和样本下限
    都只能有一处说了算。`shots` 取已过滤的正镜（转场与弃用镜不参与）。
    """
    if mode not in ("sparse", "none") or len(shots) < VOICEOVER_MIN_SHOTS:
        return None
    n_vo = sum(1 for s in shots if _voice_kind(s) == "voiceover")
    if not n_vo:
        return None
    if mode == "sparse" and n_vo / len(shots) <= VOICEOVER_HEAVY_RATIO:
        return None
    return n_vo, len(shots)


# ---------------------------------------------------------------- 视觉换挡
# 视觉换挡 = 换取景地、景别大跨（远↔近）、机位角度改变、光线改变、转场镜，
# 任一项计一次可见变化；连续 30s 一次都没有即判为超限区间。
# 判定只认**结构化位**（scenes/framing/angle/
# light_shift/转场镜）——写在提示词正文里的变化引擎看不见，故本维度恒 info：
# 引擎看不见 ≠ 片子没换挡，点名区间请人复核即可。
SHIFT_GAP_MAX = 30.0        # 秒：两次可见换挡的最大间隔
SHIFT_MIN_TOTAL = 45.0      # 秒：全片短于此不判——短片本身就在一个呼吸里

# ---------------------------------------------------------------- 景别归一表
# framing 是自由文本（schema 刻意不设枚举），真实数据里既有「中景」也有
# 「双人中景」还可能写英文代码。归一只为**统计分布**，无法归一的值
# **只提示不判错**——绝不因为作者写了新说法就报错。
_FRAMING_SIZE: dict[str, str] = {           # 尺寸类（三桶）
    # 远 —— 交代环境与尺度
    "远景": "wide", "大远景": "wide", "ews": "wide", "extreme wide": "wide",
    "大全景": "wide", "ws": "wide", "wide": "wide",
    "全景": "wide", "fs": "wide", "full shot": "wide",
    "中全景": "wide", "mls": "wide",
    # 中 —— 交代动作与关系
    "中景": "medium", "ms": "medium", "medium": "medium",
    "中近景": "medium", "mcu": "medium",
    # 近 —— 交代表演与情绪
    "近景": "close", "特写": "close", "cu": "close", "close": "close",
    "大特写": "close", "ecu": "close", "extreme close": "close",
}
_FRAMING_VIEW: dict[str, str] = {           # 视点类（不是尺寸，单独一桶）
    "过肩": "view", "ots": "view",
    "主观": "view", "pov": "view",
    "双人": "view", "2s": "view",
    "插入": "view", "ins": "view", "insert": "view",
}
# 对外单一真源：尺寸桶优先于视点桶——「双人中景」既含「双人」又含「中景」，
# 按尺寸归为 medium（它首先是个中景，"双人"只是内容说明）。
FRAMING_BUCKETS: dict[str, str] = {**_FRAMING_VIEW, **_FRAMING_SIZE}

BUCKET_LABEL = {"wide": "远景类", "medium": "中景类", "close": "近景类", "view": "视点类"}

# 占位文本标记：只收**不可能是正经台词**的字面（"此处/略/文案"这类日常词不收，
# 避免把「此处应有掌声」这种真台词误判成占位）。
PLACEHOLDER_MARKS: tuple[str, ...] = (
    "todo", "tbd", "placeholder", "lorem",
    "待定", "待补", "待写", "待填", "占位", "xxx", "ｘｘｘ", "。。。", "???", "？？？",
)

# 台词断句（`subtitle_dump` 维度用）：句末标点 + 其后紧跟的收尾引号并入前句。
# 省略号刻意不作句末——「某种沉睡了十万年的存在……正缓缓转过头来」是一句话的停顿，
# 断在这里会把每个悬念号都报成一句。末尾的无标点残句单独成段，以免漏判最后一句。
_SENTENCES = re.compile(r'[^。！？!?]*[。！？!?]+[”』」\'"]*|[^。！？!?]+')

# ---------------------------------------------------------------- 多镜语法
# 「一段 video_prompt 切多镜」的写法（Shot 1: … Shot 2: … / 镜头一：… 镜头二：…）。
# 本工程的制度是**一镜一次调用一个文件**：视频适配器成功时只取一个 video_url，
# 时长由产物回填、字幕与时间轴据此对齐——一段提示词里排两个镜，制度上也拆不出
# 第二段素材，只会得到一段素材承担两镜的内容。
# 本维度恒为 warn——拆不拆镜是创作判断，引擎只提醒不拦死生成。
# 引擎自产的分段段头受同一条判据约束：本维度只扫作者写的 `video_prompt`，装配后
# 的那条由 tests/test_prompts 的契约守卫钉（产生端 `sketchboard.timeline_text`）。
MULTISHOT_RE = re.compile(r"Shot\s*\d|镜头\s*[一二三四五六七八九十1-9]", re.IGNORECASE)

# ---------------------------------------------------------------- 复述重合率
# 增量编译铁律的量化告警：视频请求恒带该镜分镜图（native=首帧 / dubbed=参考图），
# `video_prompt` 复述主体外貌与场景 = 要求模型把已经画好的东西再画一遍，是跨镜
# 漂移的头号来源（引擎侧没有 `video_prompt → image_prompt` 的回退，见
# pipeline/prompts.py）。这里只统计**字符级 n-gram 重合率**——中文无分词，
# shingle 是零依赖下唯一稳的相似度量；它测的是"抄了多少字"，不是语义相似度。
ECHO_NGRAM = 6                 # shingle 长度：6 字≈一个完整短句成分，太短会被"镜头缓缓"类套话刷满
ECHO_MIN_CHARS = 16            # 太短的 video_prompt 不判（"缓慢推近"整句都是运镜词，重合无意义）
# ⚠ 阈值须用真实项目标定。**当前是保守值**：本仓两个真实章节（各 8 镜，
# 指挥层按现行纪律写的分镜）实测重合率全部为 0.00，说明写对了
# 就压根不重合；0.35 留出了极宽的容忍带，只抓「整段粘贴/大段复述」这类硬伤。
# 真实项目积累后（尤其是长片与改编章节）应重新分布统计再收紧。
ECHO_RATIO_WARN = 0.35
_ECHO_STRIP = re.compile(r"[\s，。、；：,.;:!?！？“”\"'（）()【】\[\]—…·]+")


def _shingles(text: str, n: int = ECHO_NGRAM) -> set:
    """字符 n-gram 集合（先剥标点与空白——同一句话换个逗号位置不该算两回事）。"""
    t = _ECHO_STRIP.sub("", str(text or ""))
    return {t[i:i + n] for i in range(len(t) - n + 1)} if len(t) >= n else set()


def echo_ratio(video_prompt, image_prompt) -> float:
    """video_prompt 有多大比例是从 image_prompt 抄来的（0~1，分母是 video_prompt）。

    分母刻意取 video_prompt 而非并集：我们问的是"这条运动提示词里有几成是复述"，
    image_prompt 写得多详细都不该拉低这个数。"""
    a = _shingles(video_prompt)
    if not a:
        return 0.0
    return len(a & _shingles(image_prompt)) / len(a)


# ---------------------------------------------------------------- art_direction
# 风格圣经旋钮缺省（中位）。整块缺失 / 写坏 / 越界一律回落到这里，永不报错。
ART_DIRECTION_DEFAULTS: dict = {"variety": 5, "motion": 5, "density": 5, "avoid": []}
KNOBS = ("variety", "motion", "density")


def _camera_repeat_budget(variety: int) -> int:
    """相邻同运镜允许出现的次数上限：variety=10→0（一次都不许） 8→1 5→2 1→4。"""
    return max(0, (10 - variety) // 2)


def _framing_bucket_floor(variety: int) -> int:
    """景别至少要用到几个桶（4 镜以上才判）：variety≥9→3 类，其余→2 类。

    整除三档：9//3=3 起跳，故 **9 与 10 同为 3 类**，6~8 与 ≤5 同为 2 类。
    schema 的 `art_direction.variety` description 必须与本函数锁步
    （tests/test_variation.py 的边界断言守着这条口径）。"""
    return max(2, min(len(BUCKET_LABEL), variety // 3))


def _emotion_kind_floor(variety: int) -> int:
    """有台词镜的情绪至少要有几种：variety≥8→2，其余→1（4 镜以上才判）。"""
    return max(1, variety // 4)


def _camera_coverage_floor(motion: float) -> float:
    """正镜里必须写了 camera 的比例下限：motion=10→1.0 5→0.5 1→0.1。"""
    return motion / 10.0


def _speech_rate_band(density: int) -> tuple[float, float]:
    """旁白语速合理带（字/秒）。density=5→3.0~5.2（真实章节落在 3.4~4.7）；
    density 调高=要求信息更密（说得更快/写得更满），调低=留白更多。"""
    return (round(1.6 + density * 0.28, 2), round(3.6 + density * 0.32, 2))


def resolve_art_direction(data: dict, override: dict | None = None) -> dict:
    """取生效旋钮：显式 override > 文档顶层 art_direction > 缺省。永不抛异常。"""
    raw = override if isinstance(override, dict) else None
    if raw is None:
        raw = (data or {}).get("art_direction")
    out = dict(ART_DIRECTION_DEFAULTS)
    if isinstance(raw, dict):
        for k in KNOBS:
            try:
                v = int(raw.get(k))
            except (TypeError, ValueError):
                continue
            out[k] = min(10, max(1, v))
        av = raw.get("avoid")
        if isinstance(av, (list, tuple)):
            out["avoid"] = [str(x).strip() for x in av if str(x or "").strip()]
    return out


# ---------------------------------------------------------------- Finding
@dataclass(frozen=True)
class Finding:
    """一条 lint 结论。`code` 是维度代码（前端/测试按它断言，中文文案可改）。"""
    code: str
    level: str                                   # warn=该改 / info=知会
    message: str
    shots: tuple = ()                            # 相关镜号（可空）
    hint: str = ""                               # 修复方向

    def to_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "message": self.message,
                "shots": list(self.shots), "hint": self.hint}

    def line(self) -> str:
        where = f"（镜 {'/'.join(str(x) for x in self.shots)}）" if self.shots else ""
        return f"{'⚠' if self.level == 'warn' else 'ⓘ'} {self.message}{where}"


def summarize(findings) -> dict:
    """条幅用汇总：{warn, info, total}。"""
    fs = list(findings or [])
    warn = sum(1 for f in fs if f.level == "warn")
    return {"warn": warn, "info": len(fs) - warn, "total": len(fs)}


# ---------------------------------------------------------------- 归一小工具
def normalize_camera(value) -> str:
    """运镜归一：取技法名（冒号前那截）、去空白、大小写归一。

    真实数据里 camera 既有「缓慢推近」也有「缓慢推近：镜头缓缓平稳推近至主体…」，
    两者是同一个运镜，必须判成雷同。"""
    s = str(value or "").strip()
    for sep in ("：", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return "".join(s.split()).lower()


def framing_bucket(value) -> str | None:
    """景别归一到桶；归不了返回 None（只提示不判错）。"""
    s = "".join(str(value or "").split()).lower()
    if not s:
        return None
    if s in FRAMING_BUCKETS:
        return FRAMING_BUCKETS[s]
    # 组合写法按包含关系归一，**尺寸桶优先于视点桶**——「双人中景」既含「双人」
    # 又含「中景」，它首先是个中景（"双人"只是内容说明），归 medium。
    # 英文代码只认精确匹配（"cu" 会命中 "focus" 这类误伤），中文别名才做包含。
    for table in (_FRAMING_SIZE, _FRAMING_VIEW):
        for key in sorted(table, key=len, reverse=True):
            if key.isascii():
                continue
            if key in s:
                return table[key]
    return None


def _shot_text(shot: dict) -> str:
    """字幕/配音口径的本镜文本：有旁白取 narration、无旁白取 caption
    （同 subtitle.pick_texts 语义，音字一致铁律）。"""
    return (str(shot.get("narration") or "").strip()
            or str(shot.get("caption") or "").strip())


def _prompt_text(shot: dict) -> str:
    """反 slop 的扫描面：只扫**提示词**（发给模型的字），不扫 narration
    ——旁白里出现「唯美」是台词，不是空词。"""
    return "\n".join(str(shot.get(f) or "") for f in ("image_prompt", "video_prompt"))


def _picture_text(shot: dict) -> str:
    """表演物理化维度（抽象情绪词/画面代词）的扫描面：提示词 + delta 骨架位。

    action/end_state 缺 video_prompt 时会被引擎原样编译进视频请求
    （pipeline/prompts.py 的 delta 骨架），所以它们和提示词同罪同罚；
    narration/lines 不在内——台词里说「我怕」是表演，「她」是人话。"""
    return "\n".join(str(shot.get(f) or "")
                     for f in ("image_prompt", "video_prompt", "action", "end_state"))


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def active_shots(data: dict) -> list[dict]:
    """参与统计的正镜：转场镜（零成本本地渲染）与弃用镜（不进成片）都跳过。

    **输入一律 `data.get("shots") or []`**——绝不用 `Project.shots`/`active_shots`
    property：那两个在「无分镜」「全 omt」时抛 ProjectError，会当场打破
    「软闸永不抛异常」的承诺。跳过判据复用 transitions.is_transition /
    review.is_omitted 两个单一真源，不另写字面判断。"""
    raw = (data or {}).get("shots") if isinstance(data, dict) else None
    out = []
    for s in (raw if isinstance(raw, list) else []):
        if not isinstance(s, dict):
            continue
        if transitions.is_transition(s) or review.is_omitted(s):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------- 各维度
def _lint_camera(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    budget = _camera_repeat_budget(ad["variety"])
    pairs: list[tuple] = []
    prev_cam, prev_id = None, None
    for s in shots:
        cam = normalize_camera(s.get("camera"))
        if cam and cam == prev_cam:
            pairs.append((prev_id, s.get("id")))
        prev_cam, prev_id = cam, s.get("id")
    if len(pairs) > budget:
        # 去重保序：pairs 是相邻对，连续三镜雷同会把中间镜号摊出两次
        # （[(1,2),(2,3)] → 1/2/2/3）。shots 只承载「哪几镜」，次数已在 message 里，
        # 重复值还会原样下发给 Studio 条幅渲染成重复 chip。
        ids = tuple(dict.fromkeys(x for p in pairs for x in p))
        out.append(Finding(
            "camera_repeat", "warn",
            f"相邻镜运镜雷同 {len(pairs)} 处（variety={ad['variety']} 允许 {budget} 处）",
            ids, "相邻两镜换一种运镜（推↔拉、摇↔跟、固定↔环绕），"
                 "或给其中一镜换景别拉开观感；进阶运镜见 references/storyboard.md"))
    # 运镜覆盖率（motion 旋钮）——只改告警，永不改画面
    if shots:
        have = [s for s in shots if str(s.get("camera") or "").strip()]
        floor = _camera_coverage_floor(ad["motion"])
        if len(have) / len(shots) < floor:
            miss = tuple(s.get("id") for s in shots if not str(s.get("camera") or "").strip())
            out.append(Finding(
                "camera_missing", "warn",
                f"{len(miss)}/{len(shots)} 镜没写 camera（motion={ad['motion']} 要求 "
                f"≥{floor:.0%} 镜有运镜）", miss,
                "camera 会被引擎置于提示词首位（前位 token 权重最高），"
                "空着等于把运镜交给模型随机发挥"))
    return out


def render_mode(data: dict) -> str:
    """章节的渲染模式（别名归一，与 `Project.motion` 同口径：a→kenburns b→native c→dubbed）。

    lint 需要它，是因为**有些维度只在某些模式下才成立**——最典型的是 `emotion`：
    它的唯一消费方是 TTS 链（`voicecast.shot_expressive_params` → seedtts 的
    `audio_params.emotion`），而 native 由模型原生配音、`Project.needs_tts` 为 False、
    引擎根本不跑 `stage_tts`。对 native 章节催「补 emotion 再跑 tts」是指向一条
    该模式下不存在的阶段，作者照做也完全无效。

    **这条通则有一个具名例外：`_lint_prompt_thin` 的厚度判据不问 motion。**
    判据是「阶段 vs 字段」——emotion 催的是一个该模式下跑不到的**阶段**，而
    `video_prompt` 是**已经写在盘上的字段**：kenburns 章节跑 `gen-video`（或加
    `--motion native`）就把它原样发给模型，那时再体检已经花过钱。
    加维度时按这条分：催**阶段**要问 motion，判**字段内容**不问。

    这里不持有 Project 对象（lint 是接收 dict 的纯函数），只引 effective_motion
    ——自抄一份别名表或缺省判据，未表态章节的体检口径就会与真发分叉。"""
    return effective_motion(data if isinstance(data, dict) else {})


def _lint_emotion(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    if ctx.get("motion") == "native":
        return out          # native 不跑 TTS，emotion 是死字段——催了也没用
    speak = [s for s in shots if voicecast.shot_text(s)]
    if not speak:
        return out
    # skip_design 项目（kn-quote/kn-ranking/kn-showcase 这类纯旁白解说档）一把
    # 口播音色贯穿是常态而非疏漏：整体缺 emotion 降为 info（warn 会常态刷屏、
    # 稀释真告警），单一情绪不算单调（一个解说腔从头到尾是合法选择）。
    solo = bool(ctx.get("solo_narration"))
    # emotion 可以写在镜级，也可以**逐句**写在 lines[]（多角色镜常态：一句怒一句悲）。
    # 只看镜级就会把逐句标注得很细的对白镜误报成「白开水」——催作者去补一个
    # 它已经写过、而且写得更细的字段
    def _has_emotion(sh):
        return any(str(ln.get("emotion") or "").strip() for ln in voicecast.shot_lines(sh))

    miss = tuple(s.get("id") for s in speak if not _has_emotion(s))
    if len(miss) == len(speak):
        out.append(Finding(
            "emotion_missing", "info" if solo else "warn",
            f"全部 {len(speak)} 个有台词镜都没写 emotion——配音会是白开水", miss,
            "解说片可只给钩子镜与收束镜标 emotion，口播起伏立现" if solo else
            "按台词情感逐镜标 emotion + emotion_scale(1~5)："
            "官方角色扮演 ICL 音色直接吃 emotion，无需切模型"))
    elif miss:
        out.append(Finding(
            "emotion_missing", "info",
            f"{len(miss)}/{len(speak)} 个有台词镜没写 emotion", miss,
            "默认每镜都要有感情，补齐 emotion 再跑 tts"))
    else:
        # 与 _has_emotion 同源逐句取（shot_lines 已做镜级→句级继承）：只读镜级
        # 会把 str(None) 折成字符串 "none"——全靠 lines[] 标情绪的章节被判
        # 「只有 1 种」，混写时 "none" 又被当成一种真情绪把真单调放过。空值丢弃
        kinds = {str(ln.get("emotion") or "").strip().lower()
                 for s in speak for ln in voicecast.shot_lines(s)}
        kinds.discard("")
        floor = _emotion_kind_floor(ad["variety"])
        if not solo and len(speak) >= 4 and len(kinds) < floor:
            out.append(Finding(
                "emotion_monotone", "warn",
                f"{len(speak)} 个有台词镜只有 {len(kinds)} 种情绪"
                f"（variety={ad['variety']} 要求 ≥{floor} 种）", (),
                "同一种情绪从头念到尾等于没标；至少让钩子镜与收束镜情绪不同"))
    return out


def _lint_framing(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    if len(shots) < 4:
        return out
    buckets, unknown = {}, []
    for s in shots:
        raw = str(s.get("framing") or "").strip()
        if not raw:
            continue
        b = framing_bucket(raw)
        if b is None:
            unknown.append((s.get("id"), raw))
        else:
            buckets.setdefault(b, []).append(s.get("id"))
    if buckets:
        floor = _framing_bucket_floor(ad["variety"])
        if len(buckets) < floor:
            used = "、".join(BUCKET_LABEL[b] for b in buckets)
            out.append(Finding(
                "framing_flat", "warn",
                f"景别只用了 {len(buckets)} 类（{used}），"
                f"variety={ad['variety']} 要求 ≥{floor} 类", (),
                "远/中/近交替才有呼吸：环境交代用远景、动作关系用中景、情绪落点用近景"))
    if unknown:
        out.append(Finding(
            "framing_unknown", "info",
            "有 {} 个景别写法未收进归一表：{}".format(
                len(unknown), "、".join(sorted({v for _, v in unknown}))),
            tuple(i for i, _ in unknown),
            "自由文本合法（schema 刻意不设枚举），只是不计入分布统计；"
            "要计入就用《景别对照表》12 项措辞"))
    return out


def _lint_slop(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    hits: dict[str, list] = {}
    for s in shots:
        text = _prompt_text(s)
        if not text:
            continue
        for term in SLOP_TERMS:
            if term in text:
                hits.setdefault(term, []).append(s.get("id"))
    for term, ids in hits.items():
        out.append(Finding(
            "slop_term", "warn", f"提示词出现空词「{term}」×{len(ids)} 镜",
            tuple(ids), SLOP_TERMS[term]))
    # art_direction.avoid：作者点名回避的词，与空词同一条通道
    avoid_hits: dict[str, list] = {}
    for term in ad.get("avoid") or []:
        for s in shots:
            if term and term in _prompt_text(s):
                avoid_hits.setdefault(term, []).append(s.get("id"))
    for term, ids in avoid_hits.items():
        out.append(Finding(
            "avoid_term", "warn", f"提示词出现 art_direction.avoid 点名回避的「{term}」×{len(ids)} 镜",
            tuple(ids), "这是本项目风格圣经明令回避的措辞，换一种写法"))
    return out


def _lint_abstract_emotion(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """抽象情绪词：提示词把该演的情绪写成了名词（表演物理化纪律）。

    与 `emotion` **字段**无关——那是给 TTS 的情绪档，该写；这里管的是
    画面描述里的「愤怒地/悲伤的」：镜头拍得到下颌与指节，拍不到"愤怒"。"""
    out: list[Finding] = []
    hits: dict[str, list] = {}
    for s in shots:
        text = _picture_text(s)
        if not text:
            continue
        for term in EMOTION_TERMS:
            if term in text:
                hits.setdefault(term, []).append(s.get("id"))
    for term, ids in hits.items():
        out.append(Finding(
            "emotion_abstract", "warn",
            f"画面描述出现抽象情绪词「{term}」×{len(ids)} 镜",
            tuple(dict.fromkeys(ids)), EMOTION_TERMS[term]))
    # 兄弟表走同一条通道（同一份扫描面），但发不同的 code——两类问题混成一条，
    # 前端与测试就分不清「写了心情」和「根本没有画面」
    unfilmable: dict[str, list] = {}
    for s in shots:
        text = _picture_text(s)
        if not text:
            continue
        for term in UNFILMABLE_TERMS:
            if term in text:
                unfilmable.setdefault(term, []).append(s.get("id"))
    for term, ids in unfilmable.items():
        out.append(Finding(
            "unfilmable_term", "warn",
            f"画面描述出现拍不出来的内心词「{term}」×{len(ids)} 镜",
            tuple(dict.fromkeys(ids)), UNFILMABLE_TERMS[term]))
    return out


def _lint_pronoun(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """画面代词：image_prompt/video_prompt/action/end_state 里的「他/她/它」。

    双重代价：模型不知道代词指谁（多人镜必然张冠李戴）；设定图自动挂载按
    name/keywords 文本命中（project._matched_entities），代词命中率为零——
    写「她拿起它」等于这一镜既没点名角色也没锁道具。"""
    hits: list = []
    samples: list[str] = []
    for s in shots:
        text = _strip_quoted(_picture_text(s))
        if not text:
            continue
        m = PRONOUN_RE.search(text)
        if m:
            hits.append(s.get("id"))
            start = max(0, m.start() - 3)
            samples.append(text[start:m.end() + 3].replace("\n", " "))
    if not hits:
        return []
    brief = "、".join(f"…{x}…" for x in dict.fromkeys(samples[:3]))
    return [Finding(
        "prompt_pronoun", "warn",
        f"{len(hits)} 镜的画面描述用了第三人称代词（{brief}）",
        tuple(dict.fromkeys(hits)),
        "画面字段一律用角色/道具的注册名点名——设定图按 name/keywords 在提示词里"
        "文本命中才自动挂载，写「他/她/它」挂不上任何设定图，跨镜一致性直接掉；"
        "对白（narration/lines）里的代词不受此限")]


def _lint_narration(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    ph, seen = [], {}
    dup: dict[str, list] = {}
    for s in shots:
        text = _shot_text(s)
        if not text:
            continue                       # 纯画面镜合法（引擎自动插等长静音占位）
        low = text.lower()
        if any(m in low for m in PLACEHOLDER_MARKS):
            ph.append(s.get("id"))
        if text in seen:
            dup.setdefault(text, [seen[text]]).append(s.get("id"))
        else:
            seen[text] = s.get("id")
    if ph:
        out.append(Finding(
            "narration_placeholder", "warn",
            f"{len(ph)} 镜的旁白/字幕还是占位文本", tuple(ph),
            "占位文本会被原样念出来并烧进字幕——生图前先补成真台词"))
    for text, ids in dup.items():
        brief = f"{text[:16]}…" if len(text) > 16 else text
        out.append(Finding(
            "narration_duplicate", "warn",
            f"{len(ids)} 镜旁白完全相同：「{brief}」", tuple(ids),
            "复制粘贴没改干净，或该合并成一镜"))
    # 语速带（density 旋钮）：字/秒 落在带外
    lo, hi = _speech_rate_band(ad["density"])
    motion = ctx.get("motion") or ""
    fast, slow = [], []
    for s in shots:
        n = len(voicecast.shot_text(s))
        # 分母只算念白：dur 含 tts 折进去的停顿，气口不是语速
        speech = _num(s.get("dur")) - _shot_pause_total(s, motion)
        if not n or speech <= 0:
            continue
        rate = n / speech
        if rate > hi:
            fast.append(s.get("id"))
        elif rate < lo:
            # 留白只对烧录轨的镜是「拖节奏」：native 对白镜由模型发声、动作驱动，
            # 写了拍表的镜节奏由拍序列给出，字数÷时长不是它们的判据
            if (voicecast.in_narration_track(s, motion)
                    and not ((s.get("sketch") or {}).get("beats"))):
                slow.append(s.get("id"))
    if fast:
        out.append(Finding(
            "pace_dense", "info",
            f"{len(fast)} 镜语速超 {hi} 字/秒（density={ad['density']} 带 {lo}~{hi}）",
            tuple(fast), "念不完会被 TTS 挤压或截断：删字或加 dur"))
    if slow:
        out.append(Finding(
            "pace_sparse", "info",
            f"{len(slow)} 镜语速低于 {lo} 字/秒（density={ad['density']} 带 {lo}~{hi}）",
            tuple(slow), "画面停太久会拖节奏：补内容或压 dur"))
    return out


def _lint_narration_style(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """旁白文风：念出来的文案有自己的机器指纹，与提示词空词分开拦。

    文本口径统一走 voicecast.shot_text（逐句 lines[] 一并可见——只写 lines
    不写 narration 的多角色镜不许在本维度隐身）。台词也可能是刻意的人物
    塑造（角色就爱说黑话），所以逐条给改写方向、判断权留给指挥层。"""
    out: list[Finding] = []
    speak = [(s, voicecast.shot_text(s)) for s in shots]
    speak = [(s, t) for s, t in speak if t]
    if not speak:
        return out
    # ① 抬价句式（按句式拦——换一套字面仍是同一个动作）
    pivots, sample = [], ""
    for s, t in speak:
        m = NARRATION_PIVOT_RE.search(t)
        if m:
            pivots.append(s.get("id"))
            sample = sample or m.group(0)[:20]
    if pivots:
        out.append(Finding(
            "narration_pivot", "warn",
            f"{len(pivots)} 镜旁白用了先立靶再翻案的抬价句式（如「{sample}…」）",
            tuple(dict.fromkeys(pivots)),
            "判断从正面下：把反常识的事实本身直接说出来（「三成人第二年就不再去了」），"
            "不借「你以为/看似/不是…而是」给下文抬价——观众认的是这个姿势不是字面，"
            "换个说法仍是同一句机器话。文章确实走过从误解到修正的过程时可保留"))
    # ② 汇报腔词（逐词带改写方向）
    jargon: dict[str, list] = {}
    for s, t in speak:
        for term in NARRATION_SLOP:
            if term in t:
                jargon.setdefault(term, []).append(s.get("id"))
    for term, ids in jargon.items():
        out.append(Finding(
            "narration_jargon", "warn",
            f"旁白出现汇报腔词「{term}」×{len(ids)} 镜",
            tuple(dict.fromkeys(ids)), NARRATION_SLOP[term]))
    # ③ 名词化（动词压成名词的公文腔，口播里听众没有回读能力）
    nominal, nsample = [], ""
    for s, t in speak:
        m = NOMINAL_RE.search(t)
        if m:
            nominal.append(s.get("id"))
            nsample = nsample or m.group(0)[:16]
    if nominal:
        out.append(Finding(
            "narration_nominal", "warn",
            f"{len(nominal)} 镜旁白把动词压成了名词（如「{nsample}」）",
            tuple(dict.fromkeys(nominal)),
            "还原成动作：「对方案进行了调整」→「把方案改了」；"
            "「实现了收入的增长」→「多挣了三成」——念出来是人话才算口播文案"))
    # ④ 收尾宏大词（只看最后一个有台词的镜——CTA 位）
    last_s, last_t = speak[-1]
    grand = [w for w in GRAND_WORDS if w in last_t]
    if grand:
        out.append(Finding(
            "cta_grand", "info",
            f"收尾镜旁白出现宏大词（{'、'.join(grand)}）",
            (last_s.get("id"),),
            "结尾升华是机器收尾的默认动作——整条片子没讲到这个量级的事时，"
            "把结尾收回到具体的人、动作或数字上；史诗/纪录题材本就在这个尺度则忽略"))
    # ⑤ 跨镜同连接词开头（复读感来自整页的形状，不来自单句）
    openers: dict[str, list] = {}
    for s, t in speak:
        head = t.lstrip("「『“”\"' 　")
        for mark in OPENER_MARKS:
            if head.startswith(mark):
                openers.setdefault(mark, []).append(s.get("id"))
                break
    for mark, ids in openers.items():
        if len(ids) >= 3:
            out.append(Finding(
                "narration_opener", "warn",
                f"{len(ids)} 镜旁白都以「{mark}」开头",
                tuple(dict.fromkeys(ids)),
                "同一个连接词起头的复读感比用词重复更明显——直接从事实、动作或"
                "数字切入；先把这类连接词全删了念一遍，断得开的就不必补回去"))
    return out


def _lint_subtitle_dump(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """一条字幕塞多句：念第一句时，后面几句已经在屏幕上了。

    `subtitle.shot_events` 对**单段**镜只发一条 Dialogue，横跨整镜时长——多句写进
    一个 `narration` 就等于把整段留言一次性摊给观众，反转与停顿全被剧透；文本长到
    要折行时，自动折行按字数切、不认标点边界，还会把收尾引号甩到行首。

    写了 `lines[]` 的镜已逐句成条（`shot_events` 按句切窗口），故本维度只看单段镜。
    句末标点后紧跟的收尾引号并入前句——「…失败了。」是一句，不是两句。"""
    out: list[Finding] = []
    hits = []
    for s in shots:
        if s.get("lines"):
            continue
        if len(_SENTENCES.findall(voicecast.shot_text(s))) >= 2:
            hits.append(s.get("id"))
    if hits:
        out.append(Finding(
            "subtitle_dump", "warn",
            f"{len(hits)} 镜把多句台词塞进了一条字幕", tuple(hits),
            "拆成 shots[].lines[] 逐句一条——说一句显示一句；"
            "画面不用拆，一个镜头照样只出一张图/一段视频"))
    return out


def _lint_voiceover(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """旁白语态：剧情档（sparse）不许把漫剧写成解说，none 档不许有旁白。

    lead（解说驱动）静默——每镜旁白是那类内容的常态，不是病。"""
    mode = ctx.get("voiceover") or "lead"
    if mode == "lead" or len(shots) < VOICEOVER_MIN_SHOTS:
        return []
    out: list[Finding] = []
    kinds = [(s.get("id"), _voice_kind(s)) for s in shots]
    vo = [i for i, k in kinds if k == "voiceover"]
    # 超限与否走 `voiceover_overrun`（付费前的语态闸读同一个），本函数只负责措辞
    over = voiceover_overrun(shots, mode)
    if mode == "none":
        if vo:
            out.append(Finding(
                "voiceover_heavy", "warn",
                f"语态声明为 none（无人声叙述），仍有 {len(vo)} 镜由旁白讲述",
                tuple(dict.fromkeys(vo)),
                "对白进 shots[].lines[]/speaker；纯展示镜把 narration 留空"
                "（引擎自动静音占位，caption 可补无声字幕）；确要旁白就把"
                " voiceover 改成 sparse/lead——语态是创作选择，声明了就按声明判"))
        return out
    # sparse：旁白只做点缀
    if over:
        out.append(Finding(
            "voiceover_heavy", "warn",
            f"剧情语态下 {len(vo)}/{len(shots)} 镜由旁白讲述"
            f"（sparse 上限 {VOICEOVER_HEAVY_RATIO:.0%}）",
            tuple(dict.fromkeys(vo)),
            "漫剧的主轴是对白与动作，不是讲述：对白逐句进 lines[]/speaker；"
            "动作与战斗写成纯画面镜（narration 留空＝合法，引擎自动静音占位）；"
            "旁白只留给时间跳跃与一句话背景，能用画面或转场字卡讲的连这句也省掉。"
            "整片确是解说型就在章节顶层声明 voiceover: lead，lint 即按解说语态判"))
    silent = [i for i, k in kinds if k == "silent"]
    if not silent:
        out.append(Finding(
            "no_silent_shot", "info",
            f"{len(shots)} 个正镜每一镜都有人声，没有一个纯画面镜", (),
            "剧情片要留呼吸位：动作展示、战斗、环境空镜不配人声反而更有力——"
            "挑一两个动作重镜把 narration 留空，声音交给 BGM 与音效"))
    return out


def _lint_hero(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    out: list[Finding] = []
    if len(shots) < 4:
        return out
    hero = tuple(s.get("id") for s in shots if s.get("hero_moment") is True)
    if not hero:
        out.append(Finding(
            "hero_absent", "info", "全片没有一镜标 hero_moment", (),
            "标 1~2 个叙事高光镜（引擎不读，纯供取舍与审片核对节奏）"))
        return out
    cap = max(2, round(len(shots) * 0.25))
    if len(hero) > cap:
        out.append(Finding(
            "hero_inflation", "warn",
            f"{len(hero)}/{len(shots)} 镜标了 hero_moment（建议 ≤{cap}）", hero,
            "全标等于没标——一集 1~2 镜足矣"))
    return out


def _norm_emotion(v) -> str:
    """情绪值归一：剥空白与常见修饰后缀，只做**字面**比较（无语义理解）。

    刻意保守——比不上就当没覆盖，宁可多提示一次，也不要假装"意思差不多"。"""
    return _ECHO_STRIP.sub("", str(v or "")).strip()


def emotion_coverage(characters: list, shots: list[dict]) -> list[tuple[str, tuple]]:
    """设定图表情覆盖差集：逐角色算「本集用到的 emotion ∖ 该角色 required_emotions」。

    这是 M8 三个 `required_*` 字段的**唯一引擎消费点**——没有它，那三个字段就是
    写进契约却无人读的死字段（本工程已有 `priority` 这个前车之鉴）。

    单向数据流（M8 定死的语义）：`required_emotions` 是**系列级常量**
    （"这个角色一共要演到哪些情绪"，随 `sync_design_to_chapters` 系列→章节单向覆盖）；
    "本集所需"**由引擎从 `shots[].emotion` 推导**，绝不反写回设定集——按集填的值
    下次 `project refs` 就会被系列值冲掉。

    只对**填了 required_emotions 的角色**判：没填 = 作者还没做这项规划，
    催他补设定图不合理（那是另一回事，不在 lint 职责内）。"""
    out: list[tuple[str, tuple]] = []
    if not isinstance(characters, list):
        return out
    # 本集实际用到的情绪 → 出现在哪几镜
    used: dict[str, list] = {}
    for s in shots:
        e = _norm_emotion(s.get("emotion"))
        if e:
            used.setdefault(e, []).append(s.get("id"))
    if not used:
        return out
    for c in characters:
        if not isinstance(c, dict):
            continue
        req = c.get("required_emotions")
        if not isinstance(req, list) or not req:
            continue
        have = {_norm_emotion(x) for x in req if _norm_emotion(x)}
        gap = sorted(e for e in used if e not in have)
        if gap:
            ids = tuple(dict.fromkeys(i for e in gap for i in used[e]))
            out.append((str(c.get("name") or "?"), tuple(gap)))
            del ids           # 差集按情绪报，镜号在 message 里给不出稳定语义
    return out


def _lint_character_coverage(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """设定图覆盖度：本集要演的情绪，角色设定单里登记过没有（M8 的消费点）。"""
    gaps = emotion_coverage(ctx.get("characters") or [], shots)
    out: list[Finding] = []
    for name, missing in gaps:
        out.append(Finding(
            "emotion_uncovered", "info",
            f"角色「{name}」本集要演 {'、'.join(missing)}，但 required_emotions 里没登记",
            (),
            "要么把这些情绪补进该角色的 required_emotions（系列级常量，"
            "供设定图补画关键表情），要么确认本集的 emotion 用词与设定单口径一致"))
    return out


def _motion_text(shot: dict) -> str:
    """会被编进视频提示词正文的作者字段：video_prompt、运镜、delta 骨架位与拍表。
    image_prompt 是静图，无多镜问题。"""
    parts = [str(shot.get(f) or "") for f in
             ("video_prompt", "camera", "action", "entry_state", "end_state", "light_shift")]
    for b in ((shot.get("sketch") or {}).get("beats") or []):
        if isinstance(b, dict):
            parts += [str(b.get("action") or ""), str(b.get("camera") or "")]
    return "\n".join(x for x in parts if x)


def _lint_multishot(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """会编进视频提示词的作者字段里的多镜语法。"""
    hits, samples = [], []
    for s in shots:
        text = _motion_text(s)
        if not text:
            continue
        m = MULTISHOT_RE.search(text)
        if m:
            hits.append(s.get("id"))
            samples.append(m.group(0).strip())
    if not hits:
        return []
    brief = "、".join(dict.fromkeys(samples))[:40]
    return [Finding(
        "multishot_syntax", "warn",
        f"{len(hits)} 镜的运动描述写了多镜语法（如「{brief}」）", tuple(hits),
        "一镜一次调用一个视频文件——video_prompt、运镜或拍表里排两个镜，"
        "拆不出第二段素材，时长/字幕也无从分别对齐：把每个镜拆成独立分镜，"
        "只写本镜的运动。支持多镜生成的型号读到这个记号会在"
        "同一段素材里换机位，与一镜一片的合成形态冲突")]


# 双语字段对：中文主源 → 英文对位 → 面向人的称谓。新增语种或字段只改这张表。
_BILINGUAL_PAIRS = (("image_prompt", "image_prompt_en", "画面"),
                    ("video_prompt", "video_prompt_en", "运动"))


def _lint_prompt_echo(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """video_prompt 复述 image_prompt（增量编译铁律的量化告警）。

    只在会真发视频请求的模式下判（kenburns 根本不读 video_prompt，催了没用）。"""
    if not uses_seedance(ctx):
        return []
    hits, worst = [], 0.0
    for s in shots:
        vp = str(s.get("video_prompt") or "").strip()
        ip = str(s.get("image_prompt") or "").strip()
        if len(_ECHO_STRIP.sub("", vp)) < ECHO_MIN_CHARS or not ip:
            continue
        r = echo_ratio(vp, ip)
        if r >= ECHO_RATIO_WARN:
            hits.append(s.get("id"))
            worst = max(worst, r)
    if not hits:
        return []
    return [Finding(
        "prompt_echo", "warn",
        f"{len(hits)} 镜的 video_prompt 在复述 image_prompt（最高重合 {worst:.0%}）",
        tuple(hits),
        "视频请求恒带这一镜的分镜图（native=首帧 / dubbed=参考图），画面基底"
        "已经给定——把主体外貌与场景再写一遍等于要求模型重画，是跨镜漂移的头号"
        "来源。video_prompt 只写增量：动作怎么变、终态停在哪、光线怎么走、"
        "镜头怎么动（结构化位另有 action/end_state/light_shift 可填）")]


# 提示词厚度地板（字符）。取值来自真实分镜单的实测分布：认真写过的章节画面/运动
# 提示词落在 150~460 字（逐镜最短 image_prompt 147 字、最短 video_prompt 178 字），
# 一句话打发的落在 20~70 字，地板卡在两者之间。
# **这是下限不是目标**：范例与 SKILL 教的量级在 250 字以上，地板只拦「写了一条约束
# 就交差」。也是**量化地板不是语义审查**（同 SLOP_TERMS / prompt_echo 的定位）——
# 拦不住灌水。上界由 `test_variation.test_floors_sit_between_real_and_lazy` 双侧夹逼
# 钉住（< 147 / < 150，出处即上面两个逐镜最短值）：**改数字必须同批附新的实测出处**。
MIN_IMAGE_PROMPT_CHARS = 110
MIN_VIDEO_PROMPT_CHARS = 140


# 角色泛称：这些词在提示词里等于没点名。与代词（他/她/它）分开成一维——代词是
# 「指代不明」，泛称是「用了另一个名字」，改法与危害都不同：泛称会让设定图的
# name/keywords 文本命中落空，模型也无从把这句话与那张参考图对上。
GENERIC_CAST = ("主角", "女主角", "男主角", "女主", "男主", "主人公",
                "反派", "boss", "BOSS", "队长", "首领", "对手")


def _lint_generic_name(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """画面/运动提示词里的角色泛称：必须改成设定里的注册名。

    设定图按 `name`/`keywords` 在提示词里**文本命中**才自动挂载（`_matched_entities`），
    写「队长从上跃下」既挂不上那张设定图，模型也不知道这个「队长」长什么样；而同一个
    角色在图像提示词里叫注册名、在运动提示词里叫泛称，两句话说的还是不是同一个人，
    模型只能猜。**只在项目登记过角色时判**——没有设定集就没有"注册名"可言。

    只报**未被注册名覆盖**的泛称：角色本身就叫「守卫队长」时，「队长」二字是它的一部分，
    再报就是纯误伤。
    """
    names = [str((c.get("name") if isinstance(c, dict) else "") or "").strip()
             for c in (ctx.get("characters") or [])]
    names = [n for n in names if n]
    if not names:
        return []
    hits: list = []
    samples: list[str] = []
    for s in shots:
        text = _picture_text(s)
        if not text:
            continue
        for g in GENERIC_CAST:
            if g not in text:
                continue
            # 泛称被注册名包住（「守卫队长」里的「队长」）不算——那是名字本身
            if any(g in n and n in text for n in names):
                continue
            hits.append(s.get("id"))
            samples.append(g)
            break
    if not hits:
        return []
    brief = "、".join(f"「{x}」" for x in dict.fromkeys(samples[:3]))
    return [Finding(
        "generic_name", "warn",
        f"{len(hits)} 镜的提示词用了角色泛称（{brief}）", tuple(dict.fromkeys(hits)),
        f"改成设定里的注册名（本项目已登记：{'、'.join(names[:6])}）——设定图按 "
        f"name/keywords 文本命中才自动挂载，泛称既挂不上图，也让画面与运动两条提示词"
        f"指不到同一个人")]


def _lint_prompt_thin(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """提示词厚度：写了但写得太薄的镜。

    薄提示词的代价不是"效果差一点"——模型拿不到可执行的细节就退回训练集均值，那一镜
    的钱等于白花，而且看起来还挺像回事（构图对、画风对，就是没有戏）。运动提示词尤其：
    动作的力学、物理反馈、次级动画（头发衣摆的滞后惯性）、镜头执行、材质响应，五项里
    缺一项模型就自己编一项。

    **只判非空字段**：该不该写由别的维度管（画面提示词是必填、运动规划由 `motion_plan`
    催），本维度只管"既然写了就得写够"。

    **厚度判据不问 motion——这是 `render_mode()` 那条通则的具名例外**（同批写进它的
    docstring）。通则说"别催该模式下不存在的阶段"，理由是作者照做也无效；但 `video_prompt`
    不是阶段而是**已经写在盘上的字段**：kenburns 章节一旦跑 `gen-video`（或加 `--motion
    native`），这段文字就原样发给模型，那时再体检已经花过钱。写了就判，才拦得住
    「kenburns 章的 video_prompt 从不被检查」这条漏网路径。
    对 kenburns 章另发一条 info 说清"它现在不出片、但切模式就会原样发出"。

    **「写了」的判据同源 `prompts.video_delta_missing`**（两语种正文与 delta 骨架）；`camera`
    只在 dubbed/native 下算——那里它随请求发出，kenburns 下它是 Ken Burns 的运镜风格键
    （`kenburns.style_for`），单独写它不构成会送进视频模型的运动稿。
    """
    # 取材口径与编译端同源：画面正文 + 摄影四字段、运动正文 + delta 骨架 + 运镜
    # 都会拼进同一条请求（pipeline/prompts 的 image/video 装配）。只量单字段，
    # PromptSpec 投影（prompt_contract.project_fields 把主运动落 action、机位落
    # camera）写得再厚也会被误报——量的必须是模型实收的那份。
    from .prompts import DELTA_FIELDS, video_delta_missing

    def _bulk(s, head, extras):
        return "".join(str(s.get(f) or "").strip() for f in (head, *extras))

    def _motion_bulk(s):
        # 中文为主、缺失回落英文；lint 没有 provider 语言上下文，按中文优先口径计量
        body = (str(s.get("video_prompt") or "").strip()
                or str(s.get("video_prompt_en") or "").strip())
        return body + _bulk(s, "camera", tuple(f for f, _zh, _en in DELTA_FIELDS))

    seedance = uses_seedance(ctx)

    def _written(s):
        return not video_delta_missing(s) or bool(seedance and str(s.get("camera") or "").strip())

    img_extra = ("framing", "angle", "lens", "lighting")
    out: list[Finding] = []
    thin_img = [s.get("id") for s in shots
                if str(s.get("image_prompt") or "").strip()
                and len(_bulk(s, "image_prompt", img_extra)) < MIN_IMAGE_PROMPT_CHARS]
    if thin_img:
        out.append(Finding(
            "prompt_thin", "warn",
            f"{len(thin_img)} 镜的画面提示词过薄（不足 {MIN_IMAGE_PROMPT_CHARS} 字）",
            tuple(thin_img),
            "把机位与主体、光源逐个点名（从哪来/什么色/落在哪）、材质与空气介质、"
            "构图占比逐项写出来——写不满说明这一镜还没想清楚，不是省字"))
    written_vid = [s for s in shots if _written(s)]
    thin_vid = [s.get("id") for s in written_vid
                if len(_motion_bulk(s)) < MIN_VIDEO_PROMPT_CHARS]
    if thin_vid:
        out.append(Finding(
            "prompt_thin", "warn",
            f"{len(thin_vid)} 镜的运动提示词过薄（不足 {MIN_VIDEO_PROMPT_CHARS} 字）",
            tuple(thin_vid),
            "只写约束不写内容（「全程对称构图不破」「环绕不超过半圈」）等于没写。"
            "六项至少写满四项：动作的力学（发力顺序/重心/速度曲线）· 物理反馈"
            "（火星/水花/形变的方向与量级）· 次级动画（头发衣摆的滞后与惯性）· "
            "镜头的执行（速度/跟随关系/何时收速）· 材质与光的响应 · "
            "声音设计（native 下 beats[].sound / sfx 会随请求发出，写与不写同价）。"
            "深度档下 beats 管时序、正文管这六项，两者不互相顶替"))
    if written_vid and not seedance:
        out.append(Finding(
            "prompt_thin_mode", "info",
            f"{len(written_vid)} 镜写了运动提示词，但本章是 "
            f"{ctx.get('motion') or 'kenburns'} 模式——现在不出片，"
            f"一旦 `gen-video` 或加 `--motion native/dubbed` 就会原样发给模型",
            tuple(s.get("id") for s in written_vid),
            "要么现在就按运动镜的标准写够（切模式当天不必返工），"
            "要么清空这些字段——留着半成品最贵：切模式时没人会重读它们"))
    return out


# 禁令句的判据词：出现在**分句**里即算这一句在说「不要做什么」。
# 只收无歧义的否定式动词前缀与显式禁止词——「不同」「不再」这类会出现在正向
# 描述里的词一律不收（`不再` 常见于「铜屑不再脱落」这类结果句，那是终态不是禁令）。
_NEGATION_MARKERS = ("不出现", "不要", "不能", "不许", "不得", "不可", "禁止", "严禁",
                     "不让", "不准", "避免", "别让", "勿", "不做", "不加", "不用",
                     "绝不", "切勿", "不应", "不该")
# 分句切分：中英文逗号、顿号、分号、句号
_CLAUSE_SPLIT = re.compile(r"[，,、；;。!！?？]")
# 触发比例：过半分句在说禁令时才报。三分之一是正常的边界约束（本仓自己的范例
# 也带一两条），过半才说明这条 video_prompt 已经从「写运动」变成「列禁区」。
NEGATION_HEAVY_RATIO = 0.5
# 样本下限：只有一两个分句时占比没有意义（写了一句禁令就是 100%）
NEGATION_MIN_CLAUSES = 4


def _lint_prompt_negation(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """`video_prompt` 写成禁令清单：分句过半在说「不要做什么」。

    与 `prompt_thin` 互补而非重复：那一条只数字符，数不出「这 120 字里 70 字是
    禁区」。实测的退化路径是——出片不理想 → 补一条禁令 → 仍不理想 → 再补一条，
    几轮之后正文里再没有力学、物理反馈与镜头执行，只剩一串「不要」。而按国产
    视频模型的既定口径，负面约束本就该走 `negative_prompt`（引擎会编译成肯定式
    约束句拼在末尾），挤在正文里既占掉动作 token，又与紧随其后的运镜抢注意力。

    只判 native/dubbed：kenburns 不调用视频模型，催了没有可行动项（门槛与
    `_lint_prompt_echo` 同源）。
    """
    if not uses_seedance(ctx):
        return []
    hits: list = []
    for s in shots:
        text = str(s.get("video_prompt") or "").strip()
        if not text:
            continue
        clauses = [c for c in _CLAUSE_SPLIT.split(text) if c.strip()]
        if len(clauses) < NEGATION_MIN_CLAUSES:
            continue
        n_neg = sum(1 for c in clauses if any(m in c for m in _NEGATION_MARKERS))
        if n_neg / len(clauses) > NEGATION_HEAVY_RATIO:
            hits.append((s.get("id"), n_neg, len(clauses)))
    if not hits:
        return []
    worst = max(hits, key=lambda x: x[1] / x[2])
    return [Finding(
        "prompt_negation", "warn",
        f"{len(hits)} 镜的运动提示词过半是禁令"
        f"（最重的镜 {worst[0]}：{worst[2]} 个分句里 {worst[1]} 句在说不要做什么）",
        tuple(i for i, _n, _t in hits),
        "禁令搬进 `negative_prompt`（引擎会编译成肯定式约束句拼在提示词末尾，"
        "不与运镜抢正文的高权重位置），正文腾出来写这一镜真正发生的动作链："
        "发力顺序与重心转移 · 物体的物理反馈 · 毛发/布料/液体的滞后与回落 · "
        "镜头何时起速何时收速 · 收在哪个明确终态。"
        "反复补禁令救不回一个没有动作设计的镜头")]


def _lint_motion_plan(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """运动规划的深度档：会送进视频模型的镜要有逐拍时间轴，而不是一段散文。

    分段时间结构（timeline prompting）是视频模型消化长动作的正道——「第几秒干嘛」
    模型能照着演，一整段描述则要它自己猜动作的先后与配速，出片常见的症状就是把
    三件事压成一件、或前半段空转。引擎把 `shots[].sketch.beats` 编译成分段时间轴
    随请求发出（`sketchboard.timeline_text`），没有 beats 就没有这一层。

    **只在 native/dubbed 下判**：kenburns 不调用视频模型，时间轴无处可发，
    催了也没用（门槛与 `_lint_prompt_echo` 同源）。**previz 镜豁免**：3D 预演是
    另一条运动预演路径，与简笔拍序列互斥（仲裁见 `sketchboard.active_guide`）。

    第二条查的是 authored `t` 的秒段是否仍然铺满镜头时长——`dur` 改过而 `t` 没跟着
    改，发出去的时间轴就是一份对不上片长的假脚本。判据复用 `beats_coverage` 单一
    真源；lint 是纯函数，只能按文档里的 `dur` 判（真发时另按实际请求秒数再判一次）。
    """
    if not uses_seedance(ctx):
        return []
    out: list[Finding] = []
    flat, offbeat, shadowed = [], [], []
    for s in shots:
        guide = sketchboard.active_guide(s)
        if guide in ("previz", "control"):
            # beats 写了、缺省仲裁却落到另一条运动路径：时间轴一个字都不会发。
            # 显式 guide 表态的镜不报（用户点过名，那条路就是本意）
            if (str(s.get("guide") or "").strip().lower() not in sketchboard.GUIDES
                    and sketchboard.beats_of(s)):
                shadowed.append(s.get("id"))
            continue
        if not sketchboard.beats_of(s):
            flat.append(s.get("id"))
            continue
        if sketchboard.beats_coverage(s, _num(s.get("dur"))):
            offbeat.append(s.get("id"))
    if shadowed:
        out.append(Finding(
            "sketch_shadowed", "warn",
            f"{len(shadowed)} 镜写了 sketch.beats 但缺省仲裁落到别的运动路径（时间轴不参与生成）",
            tuple(shadowed),
            "previz/last_frame_ref 或深度控制视频在场时，缺省仲裁按 previz > control > sketch。"
            "要用简笔时间轴请显式表态 `sketch use --shot N --guide sketch`；"
            "确认走 previz/控制视频请知悉 beats 不生效（保留不碍事，但别再指望它控制节奏）"))
    if flat:
        out.append(Finding(
            "motion_plan", "warn",
            f"{len(flat)} 镜没有逐拍时间轴（缺 sketch.beats）", tuple(flat),
            "深度档是缺省写法：把这一镜按秒切成几拍写进 `shots[].sketch.beats`，"
            "每拍 `t` 秒段 + `action` 这几秒发生什么（含环境动态）+ `camera` 机位怎么动 "
            "+ `light` 光线怎么变，引擎会编译成分段时间轴随请求发出。"
            "确实只有一个连续动作的镜（纯氛围空镜/单一姿态）可以留一段式散文，"
            "但那是例外不是缺省"))
    if offbeat:
        out.append(Finding(
            "beats_span", "warn",
            f"{len(offbeat)} 镜的拍秒段与镜头时长对不上", tuple(offbeat),
            "authored `t` 不会随 `dur` 自动重算——改过时长就要跟着改秒段，"
            "或删掉 `t` 让引擎按实际请求秒数均分。逐镜详情跑 `sketch list --chapter <项目>/<章节>`"))
    return out


# authored beats 首拍的静止开场标记：入拍即动是拆拍工法的第一条（视频模型对
# 「先静止再动」的执行是前半段空转），机器只查最不含糊的字面形态、不做语义猜测
_BEAT_STATIC_RE = re.compile(r"^(静止|静静地?[站坐立]|保持不动|站立不动|一动不动|定格|画面静止)")


def _lint_beat_rhythm(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """authored beats 的节奏体检：拆拍工法里机器可查的两条硬伤。

    ① 首拍静止开场——动作应在第 1 拍就已进行中（「喷枪已在喷涂中」而不是
      「角色静止站立」），静止首拍换来的是前 1~2 秒空转；
    ② 相邻拍动作逐字重复——每拍要有一个新的主动词递进，重复拍等于把时间轴
      的「第几秒干嘛」退化回一段散文，模型会把两拍并成一拍演。
    只查 authored beats（自动拆拍由 `auto_beats` 的句读切分保证不重复）；
    「连贯不僵硬」的完整工法在 kinema-sketchboard skill，引擎只钉字面可判的底线。
    """
    if not uses_seedance(ctx):
        return []
    out: list[Finding] = []
    static_open, repeats = [], []
    for s in shots:
        beats = sketchboard.beats_of(s)
        if len(beats) < 2:
            continue
        acts = [str(b.get("action") or "").strip() for b in beats]
        if _BEAT_STATIC_RE.match(acts[0]):
            static_open.append(s.get("id"))
        if any(a and a == b for a, b in zip(acts, acts[1:])):
            repeats.append(s.get("id"))
    if static_open:
        out.append(Finding(
            "beat_static_open", "warn",
            f"{len(static_open)} 镜的首拍是静止开场", tuple(static_open),
            "入拍即动：第 1 拍写「动作已在进行中」（如「喷枪已在喷涂中」），"
            "静止起手会让片段前 1~2 秒空转；确需静场请缩短该拍并在拍内给出微动"))
    if repeats:
        out.append(Finding(
            "beat_repeat", "warn",
            f"{len(repeats)} 镜存在相邻拍动作逐字重复", tuple(repeats),
            "每拍一个新的主动词递进（起势→推进→收束），重复拍请并成一拍或"
            "改写出动作的阶段差（同一动作也要写出「刚开始／过半／收尾」的区别）"))
    return out


def _lint_entry_continuity(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """跨镜承接契约的咬合体检：`end_state` 与下一镜 `entry_state` 是成对字段。

    上一镜 end_state 说收在哪、下一镜 entry_state 说从哪接——两者互相咬合才构成
    生成层的镜间连贯（提示词侧的最小可执行定义）。只写一半时点名：单侧承接是
    向模型描述一个另一端没有配合的过渡。两侧都不写不报（硬切是合法创作决定，
    承接契约是 opt-in 不是必填项）；转场镜是合法断点，跨转场不要求咬合。
    只在 native/dubbed 下判（kenburns 不调视频模型，字段无消费者）。
    """
    if not uses_seedance(ctx):
        return []
    raw = ctx.get("raw_shots")
    seq = raw if isinstance(raw, list) else shots
    gaps: list[tuple] = []
    prev, broke = None, False
    for s in seq:
        if not isinstance(s, dict) or review.is_omitted(s):
            continue
        if transitions.is_transition(s):
            broke = True
            continue
        if prev is not None and not broke:
            p_end = str(prev.get("end_state") or "").strip()
            c_entry = str(s.get("entry_state") or "").strip()
            if bool(p_end) != bool(c_entry):
                gaps.append((prev.get("id"), s.get("id")))
        prev, broke = s, False
    if not gaps:
        return []
    ids = tuple(dict.fromkeys(x for pair in gaps for x in pair))
    pairs = "、".join(f"{p}→{q}" for p, q in gaps[:6])
    return [Finding(
        "entry_continuity", "warn",
        f"{len(gaps)} 处相邻镜的承接契约只写了一半（{pairs}"
        + ("…" if len(gaps) > 6 else "") + "）", ids,
        "end_state（收在哪）与下一镜 entry_state（从哪接）成对咬合才有效：补齐"
        "缺的一半（构图/人物位置/光线要能对上），或确认硬切则两侧都不写")]


def _lint_bilingual(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """提示词双语完备性：中文主源写了，英文对位就不能空。

    英文副本不是可选的润色。`prompt_lang=en` 的模型（如 nano-banana / veo）取的是
    `_en` 字段，缺失时按 `pipeline/prompts.py` 的选材规则**静默回落中文**——英文模型
    收到中文提示词，词面控制力与出图质量一起掉，却不报错、不留痕，只有拿两版成片
    对比才看得出来。换 provider 是改一行配置的事，所以缺口必须在写分镜时就补上。

    判据是「有中文才要求英文」：`video_prompt` 本就只在图生视频模式下需要，不写
    中文的镜不该被催英文。
    """
    out: list[Finding] = []
    for zh_field, en_field, label in _BILINGUAL_PAIRS:
        miss = [s.get("id") for s in shots
                if str(s.get(zh_field) or "").strip()
                and not str(s.get(en_field) or "").strip()]
        if not miss:
            continue
        out.append(Finding(
            "prompt_bilingual", "warn",
            f"{len(miss)} 镜的{label}提示词缺英文对位（{en_field}）", tuple(miss),
            f"双语是硬要求：`{zh_field}` 中文真源 + `{en_field}` 英文语义对译（不是"
            f"逐字直译，按英文提示词的词法重写同一画面）。缺了它，路由到 "
            f"prompt_lang=en 的模型时会静默回落中文，全程无提示"))
    return out


def _scene_anchor(shot: dict, registry: list) -> tuple | None:
    """本镜的场景锚集合（纯字典口径，绝不碰 Project）。

    显式 `shots[].scenes` 优先（`[]`=明确无取景地，也算「有锚」——作者声明过了）；
    未声明时按注册取景地的 name（≥2 字）/keywords 扫本镜语料（与 `matched_scenes`
    同一命中精神，此处只作提示不作挂载）。返回 None=完全无锚。"""
    sc = shot.get("scenes")
    if isinstance(sc, list):
        return tuple(str(x) for x in sc)
    corpus = _shot_text(shot) + _prompt_text(shot)
    hits = []
    for it in (registry if isinstance(registry, list) else []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        terms = ([name] if len(name) >= 2 else []) + \
            [str(k) for k in (it.get("keywords") or []) if str(k)]
        if any(t and t in corpus for t in terms):
            hits.append(name)
    return tuple(hits) if hits else None


def _lint_scene_continuity(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """场景连续性：跳景事故的两种前兆。

    ① `scene_unanchored`（warn）——正镜既没写 `shots[].scenes`、语料也点不到任何
      注册取景地、全局 scene 又为空：场景全靠模型自由发挥，相邻镜各抽各的，
      实测同一段戏里夜景旁边冒出白天街头、特写背景跑进
      洞窟全是它。空镜也要显式声明 `[]`。
    ② `scene_jump`（info）——相邻两镜的场景锚完全不相交且中间没有转场镜：
      点名请人复核这几处直切读不读得通。**只报事实，不建议插转场**：缺省档
      一镜一片、镜间硬切本就是常态（转场系统 §0），要不要软化是用户的取舍；
      带上「加个 transition 更顺」的修复建议，等于每跑一次 lint 就劝 Agent
      代插一次——一章里会攒出没人要过的字卡。"""
    out: list[Finding] = []
    registry = ctx.get("scenes") or []
    has_global = bool(ctx.get("scene"))
    # 必须走 ctx 带进来的**原始**镜列表：`active_shots` 在进维度前已把转场镜滤掉，
    # 只看过滤后的列表会把「场景A→转场→场景B」误判成直切。
    raw = ctx.get("raw_shots")
    seq = raw if isinstance(raw, list) else shots
    anchors: list[tuple | None] = []
    reals: list[dict] = []
    for s in seq:
        if not isinstance(s, dict) or review.is_omitted(s):
            continue
        if transitions.is_transition(s):
            anchors.append(("__transition__",))
            reals.append(s)
            continue
        anchors.append(_scene_anchor(s, registry))
        reals.append(s)
    if not has_global:
        loose = tuple(s.get("id") for s, a in zip(reals, anchors) if a is None)
        if loose:
            out.append(Finding(
                "scene_unanchored", "warn",
                f"{len(loose)} 镜未声明取景地（shots[].scenes）且语料点不到任何注册场景",
                loose,
                "场景会由模型自由发挥、相邻镜极易跳景（夜戏旁边会冒出白天街头）。"
                "每镜显式写 shots[].scenes（真没有取景地的空镜写 []），"
                "同一段戏沿用同一个取景地名"))
    jumps = []
    for i in range(1, len(reals)):
        a, b = anchors[i - 1], anchors[i]
        if a is None or b is None:
            continue
        if "__transition__" in a or "__transition__" in b:
            continue
        if not a or not b:      # 显式 [] 的空镜不参与跳景判定
            continue
        if not (set(a) & set(b)):
            jumps.append((reals[i - 1].get("id"), reals[i].get("id")))
    if jumps:
        ids = tuple(dict.fromkeys(x for pair in jumps for x in pair))
        pairs = "、".join(f"{p}→{q}" for p, q in jumps[:6])
        out.append(Finding(
            "scene_jump", "info",
            f"{len(jumps)} 处相邻镜场景直切且中间无转场（{pairs}"
            + ("…" if len(jumps) > 6 else "") + "）",
            ids,
            "请人复核这几处直切读不读得通。**这条只报事实、不建议插转场**："
            "缺省档一镜一片、镜间硬切本就是常态，插转场是用户的取舍"
            "（Studio 槽位「＋转场」/ transition add），Agent 不代插"))
    return out


def _lint_shift(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """视觉换挡间距：按 dur 累加时间轴，找最长的"无可见换挡"区间。

    换挡事件只认五个结构化位（引擎看得见的）：转场镜、取景地切换
    （scenes 锚不相交）、景别大跨（远↔近直切）、机位角度改变、light_shift。
    写在提示词正文里的变化引擎看不见——所以恒 info：点名区间请人复核，
    真有换挡就把它挪进结构化字段（挪进来才参与 lint 与引擎编排）。"""
    raw = ctx.get("raw_shots")
    seq = []
    for s in (raw if isinstance(raw, list) else shots):
        if not isinstance(s, dict):
            continue
        if review.is_omitted(s):
            continue
        seq.append(s)
    if len(seq) < 5:
        return []
    total = sum(max(0.0, _num(s.get("dur"))) for s in seq)
    if total < SHIFT_MIN_TOTAL:
        return []
    registry = ctx.get("scenes") or []
    infos, t = [], 0.0
    for s in seq:
        is_tr = bool(transitions.is_transition(s))
        infos.append({
            "id": s.get("id"), "start": t, "tr": is_tr,
            "anchor": None if is_tr else _scene_anchor(s, registry),
            "bucket": framing_bucket(s.get("framing")),
            "angle": "".join(str(s.get("angle") or "").split()).lower(),
            "light": bool(str(s.get("light_shift") or "").strip()),
        })
        t += max(0.0, _num(s.get("dur")))
    shift_times = [0.0]
    for i in range(1, len(infos)):
        a, b = infos[i - 1], infos[i]
        shifted = (
            a["tr"] or b["tr"]
            or bool(a["anchor"] and b["anchor"] and not (set(a["anchor"]) & set(b["anchor"])))
            or ({a["bucket"], b["bucket"]} == {"wide", "close"})
            or bool(a["angle"] and b["angle"] and a["angle"] != b["angle"])
            or b["light"])
        if shifted:
            shift_times.append(b["start"])
    shift_times.append(total)
    worst_gap, worst_i = 0.0, 1
    for i in range(1, len(shift_times)):
        gap = shift_times[i] - shift_times[i - 1]
        if gap > worst_gap:
            worst_gap, worst_i = gap, i
    if worst_gap <= SHIFT_GAP_MAX:
        return []
    lo, hi = shift_times[worst_i - 1], shift_times[worst_i]
    ids = tuple(dict.fromkeys(
        x["id"] for x in infos if lo <= x["start"] < hi and not x["tr"]))
    return [Finding(
        "shift_gap", "info",
        f"连续 {worst_gap:.0f}s 没有一次可见的视觉换挡（建议每 15~30s 一次）",
        ids,
        "给这段安排一次真换挡：换取景地（shots[].scenes）、景别大跨（远↔近）、"
        "机位角度改变（angle）、光线改变（light_shift）、或插一个转场镜。"
        "判定只认这五个结构化位——变化若只写在提示词正文里，挪进字段才算数")]


def _lint_scored_mix(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """audio_mode=scored 与画面模式的组合体检。

    dubbed 是硬冲突：对口型人声必须由逐镜 TTS 喂入 ref_audio，而 scored 由音频
    模型整轨生成人声，合成时片段音轨会被整轨替换——gen-video 入口另有硬闸，
    这里在写分镜阶段（花钱之前）就点名。
    native 的角色对白是软风险：片段口型对着模型自配（将被整轨替换）的语音动，
    对白越密割裂越明显；旁白/音乐驱动不受影响，kenburns 静图无口型。"""
    if not ctx.get("scored"):
        return []
    motion = ctx.get("motion")
    if motion == "dubbed":
        return [Finding(
            "scored_dubbed_conflict", "warn",
            "audio_mode=scored 与 dubbed 对口型互斥：对口型人声由逐镜 TTS 喂入，"
            "而 scored 由音频模型整轨生成人声，合成时片段音轨会被整轨替换",
            hint="生视频改 motion: native；要对口型改回 audio_mode: tracks")]
    if motion != "native":
        return []
    dialog = tuple(
        s.get("id") for s in shots
        if any(not voicecast.is_narrator(ln.get("speaker"))
               for ln in voicecast.shot_lines(s)))
    if not dialog:
        return []
    return [Finding(
        "scored_native_dialogue", "warn",
        f"{len(dialog)} 镜有角色对白：native 片段的口型对着模型自配的语音动，"
        "而那条音轨会被剧本整轨替换——观众听到的人声与口型不同源，对白越密割裂越明显",
        shots=dialog,
        hint="对白戏改 audio_mode: tracks + dubbed 对口型；"
             "或把对白改写为旁白/字幕，让 scored 保持旁白与音乐驱动")]


def _lint_native_voice_source(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """native 成片人声与字幕不同源的体检。

    native 片段的人声由视频模型按提示词里的台词念出。`native_voiceover` 缺省
    不烧固定音色，于是那条自配人声就是成片唯一的人声轨，而字幕恒按章节文本
    编译（`subtitle.shot_events`，字幕没有关闭开关）。`prompts.native_voice_clause`
    已经把台词逐字发过去，但模型照不照办没有确定性保证——「字幕与人声一致」在这条
    路径上是待核对状态，不是既成事实；核对出口是 verify 的 ASR 人声文字核对
    （`mediacheck.native_voice_check`）。

    章节级一条，不逐镜刷屏：这是模式组合的属性，不是某几个镜写错了。
    scored 下人声由音频剧本整轨替换、片段自配音听不到，那条冲突归
    `scored_native_dialogue`，此处不重复报。

    已被 verify 的 ASR 核过的镜不点名（`ctx["voice_checked"]`）：核对正是本条
    hint 指定的出口，核完还报等于这条警告没有终态。重跑该镜会让快照失配、
    核对结论随之作废，那时它自然重新出现。"""
    if ctx.get("motion") != "native" or ctx.get("scored"):
        return []
    burn = bool(ctx.get("native_voiceover"))
    # 混烧下旁白镜的人声是烧录的 TTS（同源无须核对），只有对白镜仍由模型念
    checked = ctx.get("voice_checked") or set()
    speak = tuple(s.get("id") for s in shots
                  if voicecast.shot_lines(s)
                  and not (burn and voicecast.burn_muted(s))
                  and s.get("id") not in checked)
    if not speak:
        return []
    return [Finding(
        "native_voice_unverified", "warn",
        f"{len(speak)} 镜的人声由视频模型自己念出，而字幕按章节文本烧录"
        "——两者不同源，未核对前不能当作一致",
        shots=speak,
        hint="合成后 verify 会用本地 ASR 比对台词文字"
             "（需 pip install -e \"engine[asr]\"），"
             "或直接听一遍确认念的与字幕是同一句话；旁白要固定音色：章节写 "
             "native_voiceover: true（按镜分治：旁白镜烧 TTS 上主轨、对白镜仍是"
             "模型声；配音由 run/assemble 自行补跑，不必先手动跑 tts）")]


def _lint_burn_mixed_narration(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """native 混烧 × 同镜对白+旁白的组合体检。

    混烧的声源按镜分治：对白镜整镜由模型发声、旁白轨对它插静音。于是对白镜里
    夹带的旁白句会由模型代声——同一个旁白在别的镜是烧录的固定音色、在这一镜
    换成模型嗓音，说话人级单声源在旁白身上断掉。旁白句挪进独立的纯旁白镜即可
    两全（该镜闭声出演、TTS 上主轨）。"""
    if ctx.get("motion") != "native" or not ctx.get("native_voiceover") \
            or ctx.get("scored"):
        return []
    hit = tuple(
        s.get("id") for s in shots
        if voicecast.voice_kind(s) == "dialogue"
        and any(voicecast.is_narrator(ln.get("speaker"))
                for ln in voicecast.shot_lines(s)))
    if not hit:
        return []
    return [Finding(
        "burn_mixed_narration", "warn",
        f"{len(hit)} 镜在对白里夹带旁白句——对白镜整镜由模型发声，这几句旁白"
        "会换成模型嗓音，与其他镜烧录的固定音色旁白不同源",
        shots=hit,
        hint="把旁白句挪进独立的纯旁白镜（闭声出演 + TTS 上主轨），"
             "或改写成角色台词由模型一体出演")]


def _shot_pause_total(shot: dict, motion: str) -> float:
    """tts 折进 dur 的停顿总量：镜级停顿恒有，多段镜再加句间停顿，与 stage_tts 的拼接口径一致。"""
    lines = voicecast.shot_lines(shot)
    total = sum(voicecast.shot_pauses(shot, motion))
    if len(lines) > 1:
        total += sum(sum(voicecast.line_pauses(ln, motion)) for ln in lines)
    return total


def _cast_speech_rate(line: dict, ctx: dict) -> float | None:
    """该句说话人在用音色档案的实测语速（字/秒）；没有带语速的档案返回 None，不以经验字速代替。"""
    bank = {"voice_bank": ctx.get("voice_bank") or {}}
    if voicecast.is_narrator(line.get("speaker")):
        owner, ref = voicebank.NARRATOR, line.get("voice") or ctx.get("narrator_voice")
    else:
        voices = ctx.get("voices") or {}
        owner, ref = line["speaker"], line.get("voice") or voices.get(line["speaker"])
    return (voicebank.cast_for_ref(bank, owner, ref) or {}).get("speech_rate") or None


def _lint_chapter_length(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """全章预计时长：进旁白轨的镜按在用音色档案的实测语速估配音秒数，加上会折进 dur 的
    停顿。引擎不知道目标时长，这条只在花钱前把估算摆出来供作者对照裁稿；任一说话人
    没有带语速的档案就不估，与 `narration_overrun` 同规则。"""
    motion = ctx["motion"]
    speech = pauses = authored = 0.0
    counted = 0
    for s in shots:
        lines = voicecast.shot_lines(s)
        if not lines or not voicecast.in_narration_track(s, motion):
            continue
        for ln in lines:
            rate = _cast_speech_rate(ln, ctx)
            if not rate:
                return []
            speech += asr.speech_chars(ln["text"]) / rate
        pauses += _shot_pause_total(s, motion)
        authored += _num(s.get("dur"))
        counted += 1
    if not counted:
        return []
    return [Finding(
        "chapter_length_estimate", "info",
        f"全章预计 {speech + pauses:.0f}s：{counted} 镜按在用音色实测语速估配音 {speech:.0f}s，"
        f"含停顿 {pauses:.1f}s；作者 dur 合计 {authored:.0f}s",
        (), "对照目标时长裁稿或换声线；tts 后 dur 按实测回填")]


def _lint_narration_overrun(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """台词写太满：按在用音色档案的实测语速（`voice_bank.casts[].speech_rate`）预估
    配音时长，超出画面窗口 `voicecast.FIT_TEMPO_WARN` 倍即报。只查进旁白轨的镜；
    说话人没有带语速的档案时不估。"""
    hits: list[tuple] = []
    for s in shots:
        win = _num(s.get("dur"))
        if win <= 0 or not voicecast.in_narration_track(s, ctx["motion"]):
            continue
        need = 0.0
        for ln in voicecast.shot_lines(s):
            rate = _cast_speech_rate(ln, ctx)
            if not rate:
                need = None
                break
            need += asr.speech_chars(ln["text"]) / rate
        if need is not None and need > win * voicecast.FIT_TEMPO_WARN:
            hits.append((s.get("id"), need, win))
    if not hits:
        return []
    head = "、".join(f"镜{i}({need:.1f}s/{win:.0f}s)" for i, need, win in hits)
    return [Finding(
        "narration_overrun", "warn",
        f"{len(hits)} 镜台词按在用音色实测语速预估超出画面窗口 "
        f">{voicecast.FIT_TEMPO_WARN}×：{head}",
        tuple(i for i, _n, _w in hits),
        "配音会被压快到听感偏赶：精简台词，或加长该镜 dur；引擎不代改词")]


def _lint_dubbed_dialogue(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """dubbed × 对白上镜的组合体检。

    dubbed 把 TTS 台词轨烧进成片，而模型的口型跟随自己的表演节奏——两条时间轴
    不同源，烧录侧的开口对齐只做整体平移、救得了第一句救不了后面（多句/多人镜
    实测必然失配：声音已到下一人、上一人嘴还在动）。dubbed 的领地是全旁白的
    解说章（闭唇出片，无嘴可对）；对白上镜的内容走 native + 音色锚定，音画
    天生同轴。章节级一条：这是模式与内容的错配，不是某几个镜写错了。"""
    if ctx.get("motion") != "dubbed":
        return []
    speak = tuple(s.get("id") for s in shots
                  if voicecast.voice_kind(s) == "dialogue")
    if not speak:
        return []
    return [Finding(
        "dubbed_dialogue", "warn",
        f"{len(speak)} 镜是对白上镜而章节走 dubbed 烧录——烧录轨与模型口型"
        "两条时间轴不同源，多句/多人镜的口型失配无法靠平移对齐修复",
        shots=speak,
        hint="对白上镜的章走 native + 音色锚定（chapter 改 motion: native，"
             "voice use 选角后锚定自动附发）；dubbed 保留给全旁白的解说章。"
             "已生成的片段与配音改模式后须置 retake 重生")]


def _lint_voice_anchor(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """native 的音色漂移体检：已经在用音色锚定的章节里，谁的台词还会被模型
    每镜自选嗓音。

    只在「章节表现出选角意图」（音色表/旁白锁/句级 voice 任一非空）时才说话——
    纯 native 自配音是既定工作流，从没选过角的章节逐镜喊漂移只会淹掉真告警。
    判据与 `voicecast.voice_anchor_plan` 同一套显式绑定口径（角色走音色表，
    旁白额外认旁白锁），这里没有 store 解析不了 voice_type，绑定"存在与否"
    已足够指出缺口。scored 下人声整轨替换，锚不锚定都听不到，不报；
    混烧（native_voiceover）下声源按镜分治，只有对白镜发锚定，旁白/无词镜
    闭声出演，其上的说话人不进判据。"""
    if ctx.get("motion") != "native" or ctx.get("scored"):
        return []
    burn = bool(ctx.get("native_voiceover"))
    voices = ctx.get("voices") or {}
    has_narr = bool(ctx.get("narrator_voice"))
    loose: dict[str, list] = {}
    intent = bool(voices) or has_narr
    for s in shots:
        if burn and voicecast.burn_muted(s):
            continue
        for ln in voicecast.shot_lines(s):
            spk = str(ln.get("speaker") or "").strip()
            is_narr = voicecast.is_narrator(spk)
            if ln.get("voice"):
                intent = True
                continue
            if is_narr and has_narr:
                continue
            if not is_narr and voices.get(spk):
                continue
            who = "画外旁白" if is_narr else spk
            loose.setdefault(who, []).append(s.get("id"))
    if not intent or not loose:
        return []
    ids = tuple(dict.fromkeys(i for v in loose.values() for i in v))
    return [Finding(
        "voice_anchor_gap", "warn",
        f"已选角的 native 章节里，{'、'.join(loose)} 未绑定音色——"
        "这些台词由模型每镜自选嗓音，跨镜/跨集必然漂移（已选角的角色会自动携带锚定音）",
        shots=ids,
        hint="角色 character set --voice-prompt、旁白 voice custom --narrator --adopt 1 "
             "按描述定制立档；要官方模版走 voice audition → voice use；"
             "确要模型自选就保持现状（仅提示不阻断）")]


def _lint_cover_missing(shots, ad, ctx) -> list[Finding]:
    """分镜图已齐、章节封面仍未生成。

    封面不是自动产物（`cover` 是独立命令，不在 `run` 的 stage 链里），提醒只挂
    `stage_gen_image` 收尾的话，图出齐后重跑那条命令会走空计划出口，提醒就
    再也不出现——漏了没有第二次机会。封面只依赖设定图与画风，图齐即可出，所以
    判据钉在「图齐」这一刻：生图之前催封面是噪音，成片之后才想起来则整个制作期的
    Studio 卡片都缺主视觉。

    只看章节文档自己的 `cover` 块（lint 是纯函数，不查盘）；系列主视觉在 hint 里
    连带点名——章节封面以系列封面背景为首张参考，顺序反了锁不住系列感。
    """
    if ctx.get("cover") or not shots:
        return []
    if any(not s.get("image") for s in shots):
        return []                    # 图还没出齐：这时候催封面是催早了
    return [Finding(
        "cover_missing", "warn",
        "本章分镜图已齐，但章节封面尚未生成——Studio 项目卡与章节卡的图源"
        "（封面 → 成片海报帧 → 分镜图）只剩兜底缩略图，主视觉整段制作期都缺位",
        hint='cover <项目> --chapter <章节> --desc "本章画面描述"；'
             "系列主视觉还没出就先 cover <项目>（章节封面拿它作首张参考锁系列感）")]


def _lint_chapter_title(shots, ad, ctx) -> list[Finding]:
    """章节标题带序号。序号只归 `chapter.id/order` 与封面排版：封面会再叠一层
    「第 N 集」，标题里的序号即双重编号。判据 `project.chapter_title_number`。"""
    num = chapter_title_number(ctx.get("chapter_title"))
    if not num:
        return []
    return [Finding(
        "chapter_title_numbered", "warn",
        f"章节标题「{ctx.get('chapter_title')}」含序号「{num}」——序号由章节 id/order 与封面排版管理，"
        "标题应是本集剧情的裸短标题",
        hint="剥离序号与分隔符后重写（chapter set <项目> <章节> --title \"<剧情短标题>\"）；"
             "剥离为空则另起钩子式短标题，不用「第一章」占位")]


def _lint_topview_missing(shots, ad, ctx) -> list[Finding]:
    """取景地有基准图、无俯视布局图。

    视频请求按 `lineage.primary_layout_ref` 每镜附主场景的图纸；缺了它，模型对
    「镜头在这个空间的哪个位置、人物的左右关系」没有依据，运镜一动空间即重编。
    判据钉在「基准图已在盘」这一刻——图纸以基准图为空间取材，基准图未定稿时催不成立。

    只读文档字段、不查盘（lint 是纯函数）；URL 形态的媒体字段同样算已产出。
    """
    if ctx.get("skip_design"):
        return []
    miss = [str(sc.get("name") or "").strip()
            for sc in ctx.get("scenes") or []
            if isinstance(sc, dict) and sc.get("sheet") and not sc.get("topview_sheet")]
    if ctx.get("scene") and ctx.get("scene_ref") and not ctx.get("scene_topview_ref"):
        miss.append("全局固定场景")
    miss = [x for x in miss if x]
    if not miss:
        return []
    return [Finding(
        "topview_missing", "warn",
        f"{len(miss)} 个取景地只有基准图、没有俯视布局图（{'、'.join(miss)}）——"
        "视频请求拿到的空间证据缺一半",
        hint="补出这一批：`project refs <项目>`（已有的设定图会自动跳过，"
             "只补缺的图纸）；单张定向 `--only topview:<取景地名> --force`")]


# 碎切判据：dubbed/native 下 dur < 6s 的镜算短镜。6s 是「一个动作 + 一次反应」
# 的下缘——低于它一镜只装得下半个戏剧节拍，密集出现就是逐镜截断感的来源
_CHOP_SEC = 6.0
_CHOP_RATIO = 0.6


def _lint_montage_chop(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """碎切体检：生成式片段镜间恒硬切，每一次切换都要重新建立画面——短镜占比
    过高时截断感逐镜累积，动作没发展完就进下一镜。

    只在 dubbed/native 判（kenburns 是静图幻灯片，2~3 秒一变是它的节奏律）；
    转场镜与无 dur 的镜不计。warn 级：主戏镜的正路是 8~15s 长镜 + beats 秒段
    承载多拍连续动作，短镜（3~6s）留给特写/反应/揭示这类确有必要的 punch。"""
    if ctx.get("motion") not in ("dubbed", "native"):
        return []
    durs = [(s.get("id"), _num(s.get("dur"))) for s in shots
            if s.get("kind") != "transition" and _num(s.get("dur")) > 0]
    if len(durs) < 4:
        return []
    short = [(i, d) for i, d in durs if d < _CHOP_SEC]
    if len(short) / len(durs) <= _CHOP_RATIO:
        return []
    return [Finding(
        "montage_chop", "warn",
        f"{len(short)}/{len(durs)} 镜短于 {_CHOP_SEC:g}s——生成式片段镜间恒硬切，"
        "短镜密集时动作没发展完就被切走，截断感逐镜累积",
        shots=tuple(i for i, _ in short),
        hint="主戏镜按 8~15s 设计、用 sketch.beats 秒段装下「动作→说话→反应」"
             "整个节拍串；3~6s 只留给特写/反应/揭示等确有必要的 punch 镜。"
             "镜长阶梯与长镜写法见 video-prompting.md 第七节")]


def _lint_caption_voiceless(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """无声镜挂字幕的体检：同一条片子里字幕既当台词轨又当字卡，观众分不开。

    dubbed/native 下有人声的镜字幕逐字取台词，观众两三镜就把「底部出字」读成
    「有人在说话」；轮到无声镜的 `caption`，读到的就是「这句漏配音了」。
    kenburns 不判——静图片本就靠字卡叙事，那里字幕不承担台词轨。"""
    if ctx.get("motion") not in ("dubbed", "native"):
        return []
    voiced = [s for s in shots if voicecast.voice_kind(s) != "silent"]
    if not voiced:
        return []
    mute_captioned = [s.get("id") for s in shots
                      if voicecast.voice_kind(s) == "silent"
                      and str(s.get("caption") or "").strip()]
    if not mute_captioned:
        return []
    return [Finding(
        "caption_voiceless", "warn",
        f"{len(mute_captioned)} 个无人声镜挂了 caption，而本片其余 {len(voiced)} 镜的"
        "字幕是台词轨——同一套字幕承担两种语义，无声那几句会被读成漏了配音",
        tuple(mute_captioned),
        "二选一：这几句能用画面讲就删掉 caption；确要出字就给它配旁白"
        "（`voice use --narrator` 定旁白锁后写进 narration），别让字幕悬空")]


# 空镜措辞锚：镜级 characters 键缺失=全员兜底出场，画面却声明无人时两者必然
# 打架。「无人」排除「无人机」这类合成词
_EMPTY_SHOT_RE = re.compile(r"无人(?!机)|空镜|杳无人|不见人影")


def _lint_empty_shot_cast(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """空镜吃全员兜底的组合体检：画面写「无人/空镜」而镜级 `characters` 键缺失。

    键缺失的语义是「全员出场」——引擎会注入全部角色设定图与绑定句，与画面里的
    「无人」直接冲突，模型通常听设定图的（空镜会被画进两个角色）。显式
    `characters: []` 才是「明确无人」。info 级：判据靠措辞命中，只提醒不拦。"""
    if not ctx.get("characters"):
        return []
    hits = tuple(s.get("id") for s in shots
                 if "characters" not in s
                 and _EMPTY_SHOT_RE.search(_picture_text(s)))
    if not hits:
        return []
    return [Finding(
        "empty_shot_cast", "info",
        f"{len(hits)} 镜的画面写着无人/空镜，镜级却没写 characters"
        "（键缺失=全员出场，角色设定图与绑定句照常注入）",
        shots=hits,
        hint="空镜显式写 characters: []（props/scenes 同理按需显式）——"
             "画面声明与出场注入才不打架")]


# 时段/光线锚（场景 desc 该写死其一）：具名词表——「光」单字会把「灯光稀疏」
# 之类陈设描述也当成时段表态
_DAYPART_RE = re.compile(
    r"清晨|早晨|上午|正午|中午|午后|下午|黄昏|傍晚|日落|日出|夜|凌晨|白天|白昼|晨光|暮色")


def _lint_scene_daypart(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """具名场景的时段缺口：desc 没写死时段/光线基调的取景地。

    场景基准图生成时会自选一个时段画进去，而它是全链路的光线锚：生图阶段作
    参考图，写实档降级路线上更直接顶 `@图片1`（画面与光线基调的基准）——一张
    自选成黄昏的基准图会把白天戏整镜拖成暮色，且路线 A 下分镜图压住它、偏差
    不显形，降级时才暴露。info 级：时段是创作决定，引擎只提醒没表态的。"""
    miss = [str(sc.get("name") or "").strip()
            for sc in ctx.get("scenes") or []
            if isinstance(sc, dict) and str(sc.get("name") or "").strip()
            and not _DAYPART_RE.search(str(sc.get("desc") or ""))]
    if not miss:
        return []
    return [Finding(
        "scene_daypart_missing", "info",
        f"{len(miss)} 个取景地的描述没写死时段（{'、'.join(miss)}）——"
        "基准图会自选一个时段画进去，之后全链路把它当光线基准",
        hint="`scene set --name <取景地> --desc …` 在描述里钉死时段与主光"
             "（如「正午强光、顶光直射」）；已出的基准图跟着"
             "`project refs --only scene:<取景地> --force` 重生")]


# 疲态词表：角色缺省气色健康、神态有精神，这些词只在用户点名要疲态时才该出现在
# 外貌字段里。长词排在短词之前（「布满血丝」先于「血丝」），命中词按原样报出；
# 不收单独的「青黑」——它也是衣料颜色
_FATIGUE_RE = re.compile(
    r"黑眼圈|眼圈发黑|眼下青黑|眼周青黑|眼袋|憔悴|无精打采|疲惫|疲态|疲倦|困倦|"
    r"倦容|倦意|熬夜|熬红|熬得|布满血丝|血丝|蜡黄|病容|萎靡")
_FATIGUE_FIELDS = ("appearance", "role", "outfit", "hair", "silhouette_notes")


def fatigue_look(characters) -> list[tuple[str, tuple[str, ...]]]:
    """角色外貌里的疲态表述：返回 [(角色名, 命中词…)]。

    只扫正向外观字段（`constraints` 是负面通道，写在那里是要避免它）。命中词已
    登记进 `visual_requirements` 的不计：那是「必须保留的正向特征」表，疲态要保留
    就登记在那里，即作者显式表态。lint 维度、`character add/set` 的提醒与
    `project refs` 的出图闸共用这一个判据。"""
    rows = []
    for c in characters or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        txt = " ".join(str(c.get(k) or "") for k in _FATIGUE_FIELDS)
        vreq = " ".join(str(x) for x in (c.get("visual_requirements") or []))
        hits = tuple(dict.fromkeys(m for m in _FATIGUE_RE.findall(txt) if m not in vreq))
        if name and hits:
            rows.append((name, hits))
    return rows


def _lint_fatigue_look(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """角色外貌写了疲态（黑眼圈/眼袋/憔悴…）而用户没点名：warn 级。

    缺省角色是气色健康、神态有精神的人；写实档的「不完美即真实」指皮肤微观纹理
    与衣物磨损，不是疲态；夜班、凌晨一类题材也不推导出疲态。"""
    rows = fatigue_look(ctx.get("characters"))
    if not rows:
        return []
    who = "、".join(f"{n}（{'/'.join(h)}）" for n, h in rows)
    return [Finding(
        "character_fatigue_look", "warn",
        f"{len(rows)} 个角色的外貌写了疲态：{who}——缺省角色气色健康、神态有精神，"
        "疲态只在用户点名时写",
        hint="不是用户要求的就从 appearance/role 里删掉（`character set --appearance …`）"
             "并 `project refs --only character:<名> --force` 重出设定图；确是用户要求的"
             "把该特征登记进 visual_requirements（`character set --add-visual-requirement <词>`）"
             "作为显式表态，lint 与 `project refs` 的闸即放行")]


def _lint_control_inert(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """绑了控制视频却发不出去。

    **零成本的本地 lint 才是省钱闸**——运行时那行 `⚠ 参考视频只在 native 生效`
    打完整章照样烧钱，而这里能在花钱之前拦住。三种成因分开措辞，因为三条的
    修法完全不同；最贵的是第一条：无对白章的 motion 缺省是 dubbed，作者按
    深度复刻的工法建完章却没显式写 native，控制视频**一帧都不会发**，
    而每一镜照常按 native 单价出账。
    """
    bound = tuple(s.get("id") for s in shots if s.get("control"))
    if not bound:
        return []
    out: list[Finding] = []
    if ctx.get("motion") != "native":
        out.append(Finding(
            "control_inert", "warn",
            f"{len(bound)} 镜绑了控制视频，而本章是 {ctx.get('motion')}——"
            "参考视频只在 native 生效，这一章的控制视频一帧都不会发出去",
            bound,
            hint="章节顶层写 `motion: \"native\"`（`chapter set … --motion native`）。"
                 "无对白章的缺省是 dubbed，深度复刻必须显式表态"))
    elif not ctx.get("control_video"):
        out.append(Finding(
            "control_inert", "warn",
            f"{len(bound)} 镜绑了控制视频，但章级开关没开",
            bound,
            hint="章节顶层写 `control_video: true`，或本次加 `gen-video --control`。"
                 "默认关是刻意的——输入视频秒同样入账"))
    return out


def _lint_control_binding(shots: list[dict], ad: dict, ctx: dict) -> list[Finding]:
    """绑了控制视频却被仲裁压掉（previz 在场或显式 guide 指向别处），或段落已与素材脱节。"""
    out: list[Finding] = []
    shadowed = tuple(s.get("id") for s in shots
                     if s.get("control") and sketchboard.active_guide(s) != "control")
    if shadowed:
        out.append(Finding(
            "control_binding", "warn",
            f"{len(shadowed)} 镜绑了控制视频，但生效的运动路径不是它——控制视频不参与生成",
            shadowed,
            hint="缺省仲裁 previz > control > sketch，显式 guide 恒赢：要走控制视频，"
                 "`sketch use --shot N --guide control` 表态，或 `previz clear` 摘掉预演"))
    stale = tuple(s.get("id") for s in shots
                  if s.get("control") and _control_dur_drift(s))
    if stale:
        out.append(Finding(
            "control_binding", "warn",
            f"{len(stale)} 镜的控制段与当前镜长对不上——1:1 是运动不被拉伸或截断的前提",
            stale,
            hint="重跑 `control bind --shot N --asset <素材> --start <起点>` 按新镜长重裁"))
    return out


def _control_dur_drift(shot: dict) -> bool:
    """绑定后镜长被改过。素材重建那条边由 `control list` / Studio 用内容指纹判，
    lint 是纯文档判据、不读盘，故只查这一半。"""
    rec = (shot.get("gen") or {}).get("control") or {}
    want, now = rec.get("dur_at"), shot.get("dur")
    if want is None or now is None:
        return False
    try:
        return float(want) != float(now)
    except (TypeError, ValueError):
        return False


_DIMENSIONS = (_lint_camera, _lint_emotion, _lint_framing,
               _lint_character_coverage,
               _lint_slop, _lint_abstract_emotion, _lint_pronoun,
               _lint_narration, _lint_narration_style, _lint_subtitle_dump, _lint_voiceover,
               _lint_hero, _lint_multishot,
               _lint_prompt_echo, _lint_bilingual, _lint_motion_plan,
               _lint_beat_rhythm, _lint_entry_continuity, _lint_prompt_thin,
               _lint_prompt_negation,
               _lint_scored_mix, _lint_native_voice_source,
               _lint_burn_mixed_narration, _lint_voice_anchor,
               _lint_narration_overrun, _lint_chapter_length, _lint_dubbed_dialogue,
               _lint_cover_missing, _lint_chapter_title, _lint_topview_missing,
               _lint_scene_daypart,
               _lint_fatigue_look,
               _lint_empty_shot_cast, _lint_montage_chop, _lint_caption_voiceless,
               _lint_generic_name,
               _lint_camera_clash, _lint_preset_placeholder,
               _lint_unregistered_entity, _lint_craft_leak,
               _lint_scene_continuity, _lint_shift,
               _lint_control_inert, _lint_control_binding)


# ---------------------------------------------------------------- 入口
def lint(data: dict, *, art_direction: dict | None = None) -> list[Finding]:
    """扫一份章节文档，返回 Finding 列表（纯函数：不落盘、不改 data、不抛异常）。

    `art_direction` 显式传入时覆盖文档顶层同名块（CLI/测试用）。
    无分镜、全 omt、全转场、字段类型写坏一律返回结果而非抛错——这是软闸的底线，
    它挂在 `stage_gen_image` 的花钱主链上，绝不允许因为一条统计把生图打断。"""
    data = data if isinstance(data, dict) else {}
    ad = resolve_art_direction(data, art_direction)
    shots = active_shots(data)
    if not shots:
        return []
    # 文档级事实（不属于旋钮，但有些维度要按它取舍——如 native 不跑 TTS）。
    # solo_narration 只认显式 skip_design（无角色 skill 的既定工作流都会设它）——
    # 刻意不用「characters 为空」推断：剧情片在设定单节点之前跑 lint 时角色表
    # 可能尚未登记，按空表降噪会把真该催的 emotion 也压掉。
    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    ctx = {"motion": render_mode(data), "characters": data.get("characters") or [],
           "chapter_title": (data.get("chapter") or {}).get("title")
           if isinstance(data.get("chapter"), dict) else None,
           "solo_narration": bool(data.get("skip_design")),
           # 旁白语态：顶层 voiceover 声明 > skill/画风缺省（skills.py 单一真源）
           "voiceover": voiceover_mode(data),
           # 音频路线（project.scored_audio 纯函数单一真源）：组合体检维度用
           "scored": scored_audio(data),
           # 音色锚定缺口维度（voice_anchor_gap）的选角事实：音色表与旁白锁
           "voices": data.get("voices") or {},
           "narrator_voice": data.get("narrator_voice"),
           # 台词超窗预估（narration_overrun）取在用档案的实测语速
           "voice_bank": data.get("voice_bank") or {},
           # native 配音混烧开关：决定成片人声是我们的固定音色还是模型自配
           "native_voiceover": bool(data.get("native_voiceover")),
           # 成片自审已核过的人声镜（verify 的 ASR 文字比对结论，engine-managed）：
           # 核对是 native_voice_unverified 自己指定的出口，不消费它那条警告
           # 就永远清不掉——一条清不掉的警告只会训练人忽略整张 lint
           "voice_checked": {r.get("id") for r in
                             (((data.get("verify") or {}).get("voice") or {})
                              .get("rows") or []) if isinstance(r, dict)},
           # 场景连续性维度的取材（纯字典口径，与 matched_scenes 同命中精神）；
           # raw_shots=未过滤原始列表——转场镜要参与「跳景是否经转场」的判定
           "scenes": data.get("scenes") or [],
           # 名册查表维度（unregistered_entity）要的第三张表——ctx 只装
           # characters/scenes 的话，props 判据永远查不到、永远不报
           "props": data.get("props") or [],
           "scene": str(data.get("scene") or style.get("scene") or "").strip(),
           # 章节封面登记块（cover 命令回填）：封面缺口维度只认文档，不查盘
           "cover": data.get("cover"),
           # 俯视布局图缺口维度：全局固定场景那一对图落在文档顶层，具名取景地随 scenes[]
           "scene_ref": data.get("scene_ref"),
           "scene_topview_ref": data.get("scene_topview_ref"),
           # 深度捕捉的章级开关：`control_inert` 维度靠它区分「开关没开」与
           # 「模式不对」，两者的修法完全不同
           "control_video": bool(data.get("control_video")),
           "skip_design": bool(data.get("skip_design")),
           "raw_shots": data.get("shots") or []}
    out: list[Finding] = []
    for fn in _DIMENSIONS:
        out.extend(fn(shots, ad, ctx))
    out.sort(key=lambda f: (f.level != "warn", f.code))
    return out
