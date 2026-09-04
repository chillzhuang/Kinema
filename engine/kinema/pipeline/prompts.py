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

"""提示词拼装策略（从 cli.py 下沉为独立可测模块）。

这是全系统最贵的策略之一：发给 Seedream / Seedance 的每一个字都从这里出。
规则清单（由 tests/test_prompts.py 守护回归）：
  · 双语选材：国产 provider 用中文主字段，prompt_lang=en 用 *_en 字段，缺失互为回退；
  · 摄影字段地板：framing/angle/lens/lighting（图）与 camera/sfx（视频）自动并入——
    工业字段不能只当标注躺在 JSON 里；Skill 已写进提示词的不重复注入；英文体不混中文字段；
  · 防字地板（图 + 视频）：negative 自动追加"字幕、画面文字、水印"——分镜图与视频
    本体永远无字，字幕由合成段按画风样式后置烧录（模型自作主张画进去的字删不掉）。
    **拼装顺序是硬约束：作者写的 negative 在前、地板在后**；作者已自写「字幕/subtitle」
    则不重复注入；图像侧 opt-out 由 profile 的 `image.image_text_floor: false` 声明
    （只给 game_sim 的 HUD 与 explainer 的信息图，见 config/models.yaml）；
  · 负面句式：国产模型用肯定式约束句「避免出现：…」拼进正向提示词，非 API 参数；
  · 驳回闭环（引擎兜底）：retake 的 --note 意见编译进下一版提示词；
  · **图侧剧情画面三件套**：① 角色文字锚**按镜装配**（shot_cast +
    character_anchor_block：只写本镜出场角色、设定图在场者只留绑定句、未点名且
    零命中的镜按 CHAR_BLOCK_BUDGET 裁决全员兜底块）——绝不整块灌全员外貌清单；
    ② 单帧剧情契约句 STORY_FRAME（随 text_floor 画风门）+ 设定图参考契约句
    REF_BASE（随 ref_base 实况）前置声明「这是一格戏、参考图只作外观基准」；
    ③ 防设定表地板 SHEET_FLOOR 并进负面串——分镜图绝不许长成设定资产版式；
  · **增量编译（视频侧铁律）**：视频请求恒带该镜分镜图（native=首帧 / dubbed=参考图），
    画面基底已由图给定，`video_prompt` 只写「在此基础上发生的变化」。故
    ① 提示词首句是**增量契约句**（按 native/dubbed 二分措辞），
    ② `video_prompt` 缺失时**绝不回退整条 `image_prompt`**（复述首帧=最强漂移源），
    改用 `action`/`end_state`/`light_shift` 三个结构化 delta 字段拼骨架、全空才落兜底句
    （delta 骨架**两语种都注入**、只换标签——这三个字段无 `_en` 对位，en 侧丢弃它们
    就等于用兜底句「只做轻微呼吸」顶替作者写的整套运动设计），
    ③ 首尾帧（FLF2V）时追加「只写过渡过程」铁律句。
"""
from __future__ import annotations

import re

from .. import review, voicecast
from ..prompt_contract import (
    AgentContractRegistry,
    PromptContractError,
    PromptEnvelope,
    PromptSpec,
)

# 防字地板文案（图/视频共用一套口径，双语）——改这里即全链生效
TEXT_FLOOR_ZH = "字幕、画面文字、水印"
TEXT_FLOOR_EN = "subtitles, captions, on-screen text, watermark"

# ------------------------------------------------------------ 图侧剧情画面契约
# 防设定表地板（图侧，随 text_floor 同一道画风门）：分镜图是**单帧剧情画面**，
# 绝不是设定资产。参考图喂进来的恰恰是分区版式的设定图（角色是肖像+双立像的
# 三区表，道具是三视+细节框），正文一弱模型就复刻版式——会把 4 张
# 设定图原样拼成 2×2 当分镜交回来。旧词（细节宫格、色板）留在负面地板里：
# 色板槽位虽已从两类设定图删除，模型照设定集惯例仍会自发补画，地板词照拦不误。
SHEET_FLOOR_ZH = ("角色设定表、三视图排版、多视图转台、细节宫格、色板色块、白底立绘拼贴、"
                  "肖像与全身立像并排分区、多画格拼贴排版")
SHEET_FLOOR_EN = ("character design sheet, model sheet, turnaround views, detail grid, "
                  "color palette swatches, white-background lineup, "
                  "portrait-and-full-body split layout, collage layout")

# 单帧剧情契约句（图侧）：把整条提示词定性为「正在发生的一个瞬间」——与视频侧
# 增量契约句同范式（前位声明画面性质）。没有它，风格前缀+角色清单+场景读起来
# 就是一张说明卡，模型会交回「人物与场景的简单合成」而不是一格戏。
# 门控同 text_floor：HUD/信息图画风（game_sim/explainer）的帧本就不是剧情画面。
# **两档措辞**：本镜有具名出场角色用带表演指令的完整版；空出场表/弃锚的镜换无人
# 变体——完整版里「人物有具体动作与情绪神态」这半句会让空镜被凭空塞进一个角色。
STORY_FRAME_ZH = ("电影级单帧剧情画面：定格一个正在发生的瞬间，主体与视线焦点明确，"
                  "前后景有层次，人物有具体动作与情绪神态，画面讲述此刻的故事")
STORY_FRAME_EN = ("a single cinematic story frame frozen mid-action, with a clear subject and "
                  "focal point, layered fore/background depth, characters caught in specific "
                  "acts and emotions")
STORY_FRAME_NOCAST_ZH = ("电影级单帧剧情画面：定格一个正在发生的瞬间，主体与视线焦点明确，"
                         "前后景有层次，**只描绘正文点名的人物与事物、不添加正文之外的人物**，"
                         "画面讲述此刻的故事")
STORY_FRAME_NOCAST_EN = ("a single cinematic story frame frozen mid-action, with a clear "
                         "subject and focal point and layered depth; depict only the people "
                         "and objects named in the description, never invent extra figures")

# 设定图参考契约句（仅当本镜**真的附了设定图**才注入，cli 按 design_refs 实况传入）：
# 与视频侧「以所给首帧为画面基准」同一职责——声明参考媒体的用途边界。三件事一次说死：
# 外观以设定图为准（一致性）、绝不照搬其版式（防设定表融合）、未出场者不画
# （防「阵容图鉴」——设定图缺省全挂，空镜也可能带着 3 张角色设定图进请求）。
REF_BASE_ZH = ("以所附设定图为外观基准：角色的外观、体态与设定中登记的特征、场景的空间结构、道具的造型"
               "均以对应设定图为准；只取外观信息，绝不照搬设定图的排版版式；"
               "设定图上的多视图是**同一个对象**的不同角度展示——每个具名角色与物件"
               "在画面中至多出现一次，绝不画成两个相同的人；"
               "画面中未出现的角色与物件不要画入")
REF_BASE_EN = ("Use the attached design sheets only as appearance reference: subject appearance, "
               "body shape, registered subject features, spatial layout and prop shapes follow their sheets; never copy the "
               "sheet layout itself. Multiple views on a sheet depict the SAME subject from "
               "different angles — each named character or object appears at most once in the "
               "frame, never duplicated. Never depict characters or objects absent from this "
               "shot")

# 全员外貌兜底块的长度预算（字符）——「未点名出场角色」的镜在小阵容项目仍整块前置
# （策略③的一致性锚，2~5 人 ≈ 200~400 字无害），长篇改编的几十人阵容（实测
# 1986 字/33 人）则必须放弃文字锚：那不再是锚而是「画一张全员图鉴」的指令。
CHAR_BLOCK_BUDGET = 600


def flat_text(v) -> str:
    """设定字段归一：可能是单条文本，也可能是多条（`character set` 允许累加）。

    图像侧与视频侧的角色锚都要读这些字段，归一口径必须只有一份——两处各写一份，
    多条约束在一侧被拼成一句、在另一侧只取到第一条，是这类字段最典型的分叉。
    """
    if isinstance(v, (list, tuple)):
        return "；".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def _constraint_terms(v) -> list[str]:
    """把角色硬约束拆成可去重的短禁令，避免整串逗号文本混入正向锚点。"""
    text = flat_text(v)
    return [x.strip(" ，,；;。.") for x in re.split(r"[，,；;]", text) if x.strip(" ，,；;。.")]


SUBJECT_KIND_ALIASES = {
    "人": "human", "人类": "human", "human": "human",
    "动物": "animal", "宠物": "animal", "animal": "animal",
    "生物": "creature", "异兽": "creature", "creature": "creature",
    "机器人": "robot", "机械体": "robot", "robot": "robot",
    "灵体": "spirit", "spirit": "spirit",
    "其他": "other", "other": "other",
}
ANIMAL_KINDS = frozenset(("animal", "creature"))
NO_WEAR_MARKERS = ("不穿", "不穿戴", "不佩戴", "无任何人造", "不携带")


def subject_kind(c: dict) -> str:
    """读取显式主体类型；缺省使用 other，绝不从外貌文本猜测。"""
    raw = str(c.get("subject_kind") or "").strip().lower()
    return SUBJECT_KIND_ALIASES.get(raw, "other")


def _has_no_wear(c: dict) -> bool:
    return any(marker in flat_text(c.get("outfit")) for marker in NO_WEAR_MARKERS)


def _positive_outfit(c: dict) -> str:
    """从 outfit 中保留正向登记的服饰/配件，过滤同字段里的禁止句。"""
    values = [x for x in _constraint_terms(c.get("outfit"))
              if not any(marker in x for marker in NO_WEAR_MARKERS)]
    return "；".join(values)


def _positive_character_details(c: dict) -> str:
    """未附设定图时使用的正向视觉细节；正向要求与视觉禁忌分开。"""
    parts = []
    outfit = _positive_outfit(c)
    if outfit:
        parts.append(f"登记服饰/穿戴：{outfit}")
    if flat_text(c.get("hair")):
        parts.append(f"登记发型/头部特征：{flat_text(c.get('hair'))}")
    if flat_text(c.get("weapon")):
        parts.append(f"登记武器/持物：{flat_text(c.get('weapon'))}")
    if flat_text(c.get("visual_requirements")):
        parts.append(f"必须保留的视觉特征：{flat_text(c.get('visual_requirements'))}")
    return "；".join(parts)


def _reference_scope(c: dict) -> str:
    """给设定图绑定句选择主体类型安全的外观范围。"""
    kind = subject_kind(c)
    if kind == "human":
        return "外观、体态、服饰、发型与登记配件"
    if kind in ANIMAL_KINDS:
        if _has_no_wear(c):
            return "外观、体态、自然特征与登记配件"
        return "外观、体态、自然特征与登记穿戴/配件"
    return "外观、体态与设定中登记的特征"


def character_negative_block(cast: list[dict]) -> str:
    """编译本镜角色级视觉禁令，落点是图像与视频的负面通道。

    `characters[].constraints` 是视觉禁止项；正向视觉要求走 `visual_requirements`，
    服装/发型/武器走各自登记字段。`taboo_lines` 属于台词与行为人设，不进入图片负面，
    防止人类角色的行为禁区污染画面主体。
    """
    lines = []
    for c in cast:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        terms: list[str] = []
        for term in _constraint_terms(c.get("constraints")):
            if term and term not in terms:
                terms.append(term)
        outfit = flat_text(c.get("outfit"))
        if outfit and _has_no_wear(c):
            if "不穿戴任何人造物" in outfit or "无任何人造" in outfit:
                if subject_kind(c) in ANIMAL_KINDS:
                    no_wear = ("不穿任何人类衣物，不戴项圈、吊坠、饰品或鞍具，"
                               "不出现斗篷、连体服、宇航服或其他人造穿戴")
                else:
                    no_wear = "不增加未登记的服饰、穿戴或人造配件"
            else:
                no_wear = ("不增加除登记服饰/穿戴与配件外的其他人造物，"
                           "不出现未登记的斗篷、连体服或宇航服")
            if no_wear not in terms:
                terms.append(no_wear)
        if terms:
            lines.append(f"{name}：{'、'.join(terms)}")
    return "；".join(lines)


def character_anchor_block(cast: list[dict], *, sheeted=frozenset(),
                           fallback_all: bool = False,
                           budget: int = CHAR_BLOCK_BUDGET) -> tuple[str, bool]:
    """按镜装配角色文字锚：**只写本镜出场的角色，且设定图在场者不复述外貌**。

    · 有设定图随请求附上的角色 → 绑定句「名（外观、体态与登记配件以其角色设定图为准）」；
      名字↔参考图的映射必须有人说，但**外貌文本一个字不复述**（复述即漂移在图像侧
      同样成立：文字与像素不一致处全是漂移指令）；
    · 无设定图的出场角色 → 全文外貌「名——外貌」（文字是它唯一的一致性锚）；
    · `fallback_all=True`（镜上没点名、文本也零命中，引擎无从判断谁出场）时整卡
      全员回落，但受 `budget` 裁决：超预算直接弃锚返回空串（宁可不锚也不把
      几十人图鉴灌进一个空镜），返回值第二位报告「发生了收窄」供调用方警告。

    **视觉语义分层**：`constraints` 不在本函数注入，由 `character_negative_block` 编译进
    图像负面通道；`visual_requirements` 与登记服饰/发型/武器属于正向特征。`taboo_lines`
    是行为/台词人设，不进入图像提示词。这样动物专项禁忌不会污染人类角色，人物正向
    外观也不会被误判为负面。
    """
    lines = []
    for c in cast:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        if name in sheeted:
            scope = _reference_scope(c)
            details = flat_text(c.get("visual_requirements"))
            line = f"{name}（{scope}以其角色设定图为准）"
            if details:
                line += f"；必须保留的视觉特征：{details}"
            lines.append(line)
        elif (c.get("appearance") or "").strip():
            details = _positive_character_details(c)
            body = f"{name}——{c['appearance'].strip()}"
            if details:
                body += f"；{details}"
            lines.append(body)
    block = "；".join(lines)
    if fallback_all and len(block) > budget:
        return "", True
    return block, False


def with_sheet_floor(neg: str, lang: str = "zh") -> str:
    """把防设定表地板并进负面约束串（排在防字地板之后，作者原话仍恒在最前）。

    作者自己写了「设定表」/`design sheet` 类字样则不重复注入（同防字地板去重口径）。"""
    neg = (neg or "").strip()
    if ("设定表" in neg) or ("design sheet" in neg.lower()):
        return neg
    floor = SHEET_FLOOR_EN if lang == "en" else SHEET_FLOOR_ZH
    sep = ", " if lang == "en" else "，"
    return f"{neg}{sep}{floor}" if neg else floor

# ------------------------------------------------------------ 视频侧增量编译
# 增量契约句：**按 native/dubbed 二分措辞**——两条路径喂图的角色不同（providers/
# video/seedance.py）：native/首尾帧走 `role=first_frame`（首帧驱动），dubbed 走
# `role=reference_image`（参考媒体模式，末帧被忽略）。对 dubbed 说「以所给首帧为准」
# 是错误措辞，模型收到的根本不是首帧。契约句无条件前置——视频路径恒有图
# （cli.stage_gen_video 缺图直接 ProjectError，不存在"无图也发提示词"的情形）。
# **恒定量收窄到身份，构图与机位一律放开**：首句权重最高，「构图不许变」会与紧随
# 其后的 `运镜：拉远/环绕/升镜揭示` 直接冲突（那些运镜的定义就是改变构图），
# 模型二选一、通常服从首句，运镜幅度被压扁。放开构图不等于放开身份——该锁的是
# 同一个主体/同一套登记外观/同一个场景/同一画风，那才是漂移的来源。
# FLF2V 那支先验证了这条结论（见 CONTRACT_FLF2V_ZH 的注释）。
CONTRACT_FIRST_ZH = ("以所给首帧为画面基准：同一个主体、同一套登记外观与穿戴、同一个场景、同一画风，"
                     "构图与机位按本镜运镜自然变化，以下只描述在此基础上发生的变化")
CONTRACT_REF_ZH = ("以所给参考图为画面基准：同一个主体、同一套登记外观与穿戴、同一个场景、同一画风，"
                   "构图与机位按本镜运镜自然变化，以下只描述在此基础上发生的变化")
CONTRACT_FIRST_EN = ("Treat the given first frame as the visual baseline: same subject, same "
                     "registered appearance and wearables, same scene, same art style, while composition and camera framing "
                     "change naturally with this shot's camera move. Describe only what changes")
CONTRACT_REF_EN = ("Treat the given reference image as the visual baseline: same subject, same "
                   "registered appearance and wearables, same scene, same art style, while composition and camera framing "
                   "change naturally with this shot's camera move. Describe only what changes")
# 首尾帧（FLF2V）专用契约句：**不能沿用上面那两句**。末帧是下一镜的分镜图，
# 按定义就是另一个构图/机位（换构图才叫下一镜），再说"构图保持一致"等于让模型
# 同时满足两个互斥要求。这里把恒定量收窄到**身份**（外观/服装/画风），
# 明确放开构图与机位——它们本就该在两帧之间过渡。
# 参考生视频（全能参考）专用契约句：**native 的缺省档**（衔接参与镜才走首帧任务），
# content[] 里没有首帧——分镜图是**第一张参考图（@图片1）**，措辞按附图顺序点名
# （与 cli 的装配顺序严格对应：分镜图 → 简笔板 → 设定图）。@图片N 是 Seedance
# 官方的素材引用语法（编号=content[] 里图片的顺序）——每个素材都要点名用途，
# 只附不点名，模型对多图的职责分配靠猜。
# **构图与机位随运镜放开**：与 CONTRACT_FIRST 同一条实拍教训——首句权重最高，
# 把「构图基准」钉死会与紧随其后的拉远/环绕/升镜直接冲突，运镜幅度被压扁；
# 该锁的是身份与光线基调，构图显式交还本镜运镜。
# 设定图绑定优先走 `sheet_binding_clause`（逐张 @图片N 点名职责）；调用方没给
# 清单时回落 ALLREF_SHEETS 的「凡随附对应设定图者」泛称句（声明一个不存在的
# 参考=向模型索要幻觉，两种措辞都以「真附了」为前提）。
# 未出场者禁令：设定图缺省全挂，一镜可能带着别处的场景图与不在场的角色图进请求，
# 而逐张职责绑定说的全是「以之为准」——那是无条件的正向指令，模型据此把另一个
# 空间的陈设并进本镜画面是合规执行。图像侧 `REF_BASE_*` 有同款收尾，视频侧
# 只声明「仍须可辨认」时缺的正是这半句边界。
ABSENT_FLOOR_ZH = "；本镜未出现的角色、场景与物件不要画入"
ABSENT_FLOOR_EN = "; never depict characters, locations or objects absent from this shot"
CONTRACT_ALLREF_ZH = ("以 @图片1（本镜画面）为画面与光线基调的基准，"
                      "主体外观、登记穿戴/配件、场景与画风一律与它保持一致，"
                      "构图与机位按本镜运镜自然变化")
ALLREF_SHEETS_ZH = ("；画面中出现的人物、场景与道具，凡随请求附有对应设定图者，"
                    "其外貌、登记穿戴/配件、空间结构与造型一律以设定图为准，"
                    "运动中改画主体时仍须可辨认" + ABSENT_FLOOR_ZH)
CONTRACT_ALLREF_EN = ("Treat @Image 1 (this shot's frame) as the "
                      "baseline for picture and lighting mood; keep subject, registered wearables, scene "
                      "and art style consistent with it, while composition and camera framing "
                      "change naturally with this shot's camera move")
ALLREF_SHEETS_EN = ("; for any character, location or prop that has a design sheet attached, "
                    "its look, registered wearables, spatial layout and shape follow that sheet and must stay "
                    "recognizable while in motion" + ABSENT_FLOOR_EN)
# 降级路线（B/C）的取景地变体：@图片1 不再是本镜画面而是**场景基准图**——它只
# 交代这个地方长什么样，构图与人物由板/提示词与身份图各自承载。沿用「本镜画面」
# 措辞会让模型把一张无人的空景当成品构图照抄。
# 两条参考任务（native 全能参考 / dubbed 参考媒体）共用这一句：措辞只谈基准图
# 的职责，与任务型态无关，各写一份就是同一约束的两个措辞漂移点。
CONTRACT_ALLREF_BASE_ZH = ("以 @图片1（本镜取景地）为陈设、材质与光线基调的基准——"
                           "它是这个地方的空景参考，不是本镜构图，"
                           "本镜的构图、人物与机位由下文与其余参考各自交代，"
                           "画风与它保持一致")
CONTRACT_ALLREF_BASE_EN = ("Treat @Image 1 (this shot's location) as the baseline for set "
                           "dressing, materials and lighting mood - it is an empty plate of "
                           "the place, not this shot's composition; framing, characters and "
                           "camera come from the text and the other references, while the "
                           "art style stays consistent with it")


def allref_base_contract(shot: dict, lang: str = "zh") -> str:
    """降级路线的基准图契约句，按本镜是否写了 `lighting` 二分。

    基准图的时段是生成场景图时定的，与本镜未必同一时刻：若照单全收它的
    「光线基调」，一张黄昏空景会把白天戏整镜拖成夜戏——路线 A 下分镜图压住
    场景图、偏差不显形，降级后基准图就是唯一的光线来源。故 `lighting` 在场时
    把光线权威移交本镜描述，基准图只保留陈设与材质。"""
    light = str(shot.get("lighting") or "").strip()
    if not light:
        return CONTRACT_ALLREF_BASE_EN if lang == "en" else CONTRACT_ALLREF_BASE_ZH
    if lang == "en":
        return ("Treat @Image 1 (this shot's location) as the baseline for set "
                "dressing and materials - it is an empty plate of the place, not "
                "this shot's composition, and its lighting or time of day does not "
                f"apply: light this shot as described - {light}; framing, "
                "characters and camera come from the text and the other "
                "references, while the art style stays consistent with it")
    return ("以 @图片1（本镜取景地）为陈设与材质的基准——它是这个地方的空景参考，"
            f"不是本镜构图，其光线与时段不沿用：本镜光线按「{light}」执行；"
            "构图、人物与机位由下文与其余参考各自交代，画风与它保持一致")

# 设定图逐张职责绑定的措辞表：kind → (类别名, 职责半句)。板与分镜图不在此表——
# 分镜图由契约句点名（@图片1）、板由 board_role_clause 声明职责，各管各的。
# 全局固定场景（`lineage` 的 `scene:main`）走 `scene_main` 单独一支：它的 `name`
# 恒是字面「场景」，套进具名模板会产出「为场景「场景」的设定图」——一条指向
# 无身份资产的正向指令。职责半句仍取 `scene` 的，避免同一句话写两份。
_REF_KIND_ZH = {"character": ("角色", "其外貌、登记穿戴与标志配件以之为准，"
                                     "所处环境、光线与构图不取自该图"),
                "scene": ("场景", "其空间结构、陈设与光线氛围以之为准"),
                "prop": ("道具", "其造型、材质与结构以之为准")}
_REF_KIND_EN = {"character": ("character", "appearance, registered wearables and signature "
                                           "props follow it; its backdrop, lighting and "
                                           "framing do not"),
                "scene": ("location", "spatial layout, set dressing and lighting mood follow it"),
                "prop": ("prop", "shape, material and structure follow it")}

# manifest 里合法的占位 kind（占号不产句）。frame/board/scene_base 由契约句或
# board_role_clause 各自点名，tail 与 scene_top* 在下方各有专写分支。表外 kind
# 一律构造期抛错——静默 continue 的后果是模型收到一张没人交代职责的图，零报错。
_PLACEHOLDER_KINDS = frozenset({"frame", "board", "scene_base"})

# 场景俯视布局图（`scene_top` / `scene_top_main`）：**它不是一张画面，是一张图纸**，
# 故不能套设定图的措辞模板——「其空间结构以之为准」这类正向句会被执行成
# 「照它画」，产出一段俯拍平面图的视频。职责句必须同时说清三件事：取什么（空间
# 关系、朝向、机位站位、可走范围）、不取什么（视角与画风）、以及本镜真正的视角
# 由谁决定（本镜运镜）。第二、三件缺一件，图纸就会被当成构图参考。
_SCENE_TOP_ZH = (
    "（一张正俯视的空间平面图纸，不是画面）：据它确定人物之间的相对位置与朝向、"
    "镜头在这个空间里的站位与视线轴线、以及可走动的范围；"
    "**本镜依旧按本镜自己的机位与运镜拍摄，绝不改成俯视视角**，"
    "该图的线条、平涂色块、箭头与图标一律不出现在画面里，也不影响画风")
_SCENE_TOP_EN = (
    " (an orthographic top-down floor plan, not a frame): use it to fix the "
    "characters' relative positions and facing, the camera's placement in the space "
    "and the eyeline axis, and the walkable area; "
    "**the shot is still filmed from its own camera and move — never switch to a "
    "top-down view**, and none of the plan's linework, flat color blocks, arrows or "
    "icons may appear in the picture or affect the art style")


def sheet_binding_clause(manifest, lang: str = "zh") -> str:
    """逐张设定图职责绑定（官方 @图片N 引用语法）。

    `manifest` = 按 content[] 图片顺序的 `[(kind, label), …]`——**必须含 frame/board
    占位**（编号从 1 起数，占位不产句、只占号）：分镜图恒 @图片1，板占下一号。
    只给绑定句不换编号真源：编号错一位，模型就把场景图当角色图用。
    返回以「；」起头的半句（拼在契约句之后），没有设定图返回空串。"""
    zh = lang != "en"
    table = _REF_KIND_ZH if zh else _REF_KIND_EN
    parts = []      # 设定图与俯视图的职责句，**恒按 @图片N 编号顺序**
    tails = []
    n_sheets = 0    # 其中「锁主体外观」的那几张（俯视图不算，见收尾处）
    for i, (kind, label) in enumerate(manifest or (), start=1):
        if kind == "tail":
            # 尾帧接力参考：只承接构图/位置/光线，身份与画风仍归其余参考管——
            # 不能套设定图的措辞模板（它不是谁的设定图），职责句单独成文
            tails.append(
                (f"@图片{i} 为上一镜的收尾画面，本镜开场的构图、人物位置与光线"
                 "从它自然延续，再按本镜运镜推进") if zh else
                (f"@Image {i} is the closing frame of the previous shot: open by "
                 "continuing its composition, character positions and lighting, "
                 "then progress with this shot's camera move"))
            continue
        if kind == "scene_main":
            _cat, duty = table["scene"]
            n_sheets += 1
            parts.append(f"@图片{i} 为本片固定场景的设定图，{duty}" if zh else
                         (f"@Image {i} is the design sheet for the film's fixed "
                          f"location: {duty}"))
            continue
        if kind in ("scene_top", "scene_top_main"):
            who = str(label or "").strip()
            named = kind == "scene_top" and who
            if zh:
                parts.append(
                    (f"@图片{i} 为取景地「{who}」的俯视布局图" if named
                     else f"@图片{i} 为本片固定场景的俯视布局图") + _SCENE_TOP_ZH)
            else:
                parts.append(
                    (f"@Image {i} is the top-down layout plan of location '{who}'"
                     if named
                     else f"@Image {i} is the top-down layout plan of the film's "
                          "fixed location") + _SCENE_TOP_EN)
            continue
        if kind in _PLACEHOLDER_KINDS:
            continue
        if kind not in table:
            from ..prompt_contract import PromptContractError
            raise PromptContractError(
                f"manifest 里出现未登记的参考 kind「{kind}」——它会占用一个 @图片N "
                "编号却没有任何职责句，模型只能猜这张图是谁。新 kind 须登记进 "
                "_REF_KIND_ZH/_EN（产句）或 _PLACEHOLDER_KINDS（占位）")
        cat, duty = table[kind]
        name = str(label or "").strip()
        n_sheets += 1
        if zh:
            parts.append(f"@图片{i} 为{cat}「{name}」的设定图，{duty}" if name
                         else f"@图片{i} 为{cat}设定图，{duty}")
        else:
            parts.append((f"@Image {i} is the design sheet for {cat} '{name}': {duty}"
                          if name else f"@Image {i} is a {cat} design sheet: {duty}"))
    if not parts and not tails:
        return ""
    # 尾帧句在前（编号顺序=附图顺序：frame → board → tail → sheets）；其后各句
    # **一律按 @图片N 递增**——编号跳着走的一串引用，读者与模型都得多解析一层。
    # 「仍须可辨认」的收尾**只对锁主体外观的那几张成立**，故不贴在最后一句尾巴上
    # （最后一句可能是俯视布局图——它压根不进画面，谈不上改画时可辨认），而是作
    # 一句回指全部设定图的独立收尾。
    keep = ("；以上各张设定图所锁定的主体，在运动中改画时仍须可辨认" if zh
            else "; the subjects locked by those design sheets must stay recognizable "
                 "while in motion")
    sep = "；" if zh else "; "
    # 未出场者禁令跟着「附了任何一张参考」走（与改写前同一判据）：只挂到一张俯视图
    # 的镜（场景基准图还没出）同样需要它——逐张职责句说的都是「以之为准」。
    return (sep + sep.join(tails + parts)
            + (keep if n_sheets else "")
            + ((ABSENT_FLOOR_ZH if zh else ABSENT_FLOOR_EN) if parts else ""))
CONTRACT_FLF2V_ZH = ("以所给首帧为起点、末帧为终点：主体外观、登记穿戴与画风在两帧之间"
                     "保持同一主体与同一套登记外观，构图与机位则按两帧自然过渡")
CONTRACT_FLF2V_EN = ("Move from the given first frame to the given last frame: keep the same "
                     "subject identity, registered wearables and art style throughout, while composition and "
                     "camera framing transition naturally between the two frames")
# 参考视频（V2V / Seedance 2.0）专用契约句：**又一次三分之外的第四种措辞**。
# V2V 分支下图走 `role=reference_image`（不是首帧）、另带一段参考视频，三条通道
# 各锁一样东西：**外观锁于图、运动锁于视频、风格锁于文案**。措辞必须把这个分工
# 说明白，否则模型会拿参考视频里的灰模配色当画面风格照抄（previz 是无材质灰模，
# 抄过去就是一片水泥色）。定位提及用 cn-beijing 路由的纯文本序号 `图片1`/`视频1`
# ——按 content[] 顺序绑定，与 seedance.py 的 V2V 分支拼装顺序严格对应。
CONTRACT_V2V_ZH = ("以所给图片1为画面基准，主体外观、登记穿戴/配件、场景与画风保持一致；"
                   "严格跟随参考视频1的运镜、走位与动作节奏，"
                   "但不要采用参考视频的画风、配色与材质（它只是灰模预演）；"
                   "以下只描述在此基础上发生的变化")
CONTRACT_V2V_EN = ("Treat image 1 as the visual baseline: keep subject, registered wearables, scene and art "
                   "style unchanged. Strictly follow the camera movement, blocking and motion "
                   "rhythm of reference video 1, but do NOT adopt its look, palette or "
                   "materials (it is an untextured grey-model previz). Describe only what "
                   "changes on top of that")

# 首尾帧（FLF2V）过渡专写铁律句：只在 native + 实发末帧时追加（dubbed 无末帧概念）。
FLF2V_ZH = ("本镜同时给定末帧：只写首帧到末帧之间的过渡过程，不要复述末帧画面本身，"
            "运动须自然收束在末帧上")
FLF2V_EN = ("A last frame is also given: describe only the transition from the first frame "
            "to it, do not restate the last frame's content, and let the motion settle on it")

# delta 骨架字段（视频侧的"本镜差异"结构化位）：标签进提示词，值由指挥层填。
# 与摄影地板同范式——工业字段不能只当标注躺在 JSON 里。
# **三元组 (字段, 中文标签, 英文标签)**：delta 骨架**不按语种门控**——这三个字段没有
# `_en` 对位（指挥层只填一份），按本模块"双语选材、缺失互为回退"的既定口径，en 侧用
# 英文标签发同一批值。这与 camera/sfx 的"英文体不混中文字段"不是一回事：那两个在 en
# 下只是被省略，而 delta 三字段一旦丢弃就会被 DELTA_FALLBACK_EN 那句"基本别动"顶替，
# 等于把作者写的整套运动设计换成一条反向指令再花钱发出去。
DELTA_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("entry_state", "开场承接", "Opens from"),
    ("action", "动作", "Action"),
    ("end_state", "终态", "Ends on"),
    ("light_shift", "光线变化", "Light shift"))

# video_prompt 与 delta 三字段全空时的兜底句。**刻意不回退 image_prompt**：
# 那会把整条首帧描述再发一遍，等于要求模型重画一遍主体与场景——正是跨镜漂移的
# 头号来源。兜底句只声明"身份不变 + 轻微自然运动"，宁可平淡也不漂移。
# **构图这一项与契约句同口径地放开**：兜底不等于锁镜——落兜底的镜绝大多数仍写了
# `camera`，再说一句「构图不变」只是把契约句刚消除的矛盾搬进正文。要锁死机位走
# 适配器的 `camera_fixed`，不靠一句会与运镜打架的兜底文案。
DELTA_FALLBACK_ZH = ("画面内主体、登记穿戴/配件与场景保持不变，构图与机位按本镜运镜自然变化，"
                     "只做轻微自然的生命感微动与环境流动")
DELTA_FALLBACK_EN = ("keep the subject, registered wearables and scene unchanged, letting composition and "
                     "camera framing follow this shot's camera move; "
                     "only subtle natural lifelike micro-motion and ambient drift")
# FLF2V 专用兜底：两端都被 pin 时，"只做微动与环境流动"与"收束到末帧"互斥。
# 兜底该说的是怎么走过去，而不是别动。
FLF2V_FALLBACK_ZH = "沿最短自然路径从首帧过渡到末帧，运动连贯匀速、不做额外表演"
FLF2V_FALLBACK_EN = ("move along the shortest natural path from the first frame to the last "
                     "frame, with smooth continuous motion and no added performance")

# 微动恒常尾句（动力学地板）：**写了运动设计的镜**才追加，治「主动作演完就整体
# 停死」与「动作像逐帧摆拍的动态漫画」。三层动作分级里的第 1 层（全程持续的
# 微动/浮动/环境流动）几乎没有作者会写，而恰恰是写满了大动作的镜最缺这层保底。
# 措辞不说「呼吸起伏」：模型会把它演成看得见的深呼吸与叹气；生命感用「细微微动」
# 表达，呼吸是否可见由表演地板裁决；
# 惯性与衣发跟随是图生视频「像视频而不像动图」的分水岭——动作只写主体位移时，
# 模型会把中间帧插成匀速平移，落点无缓冲、布料无跟随，观感即僵硬。
#
# **必须与 `DELTA_FALLBACK_*` 分开两个常量、且两者互斥注入**：兜底句是给"一笔运动
# 设计都没有"的镜用的，它自己就说了"只做轻微自然的呼吸与环境流动"；追加在它后面
# 是同一句话说两遍。互斥用**结构**表达（走 `if not body:` 的 else 分支），
# 不用字符串比对——作者手写出一句同款措辞就会误判。
#
# **随动附属物那半句按主体类型选词**：「发丝衣料随动作摆动」发给一个动物主体，
# 说的是它身上没有的东西，而错的名词比不写更糟。名词取 `characters[].subject_kind` 的登记值
# （`subject_kind` 的既定纪律是「不填不猜测」），未登记就只发力学地板那两半。
MICRO_MOTION_HEAD_ZH = "动作连贯衔接、带重量与惯性，起落有加速与缓冲"
MICRO_MOTION_TAIL_ZH = "全程保留细微的生命感微动与环境流动"
MICRO_MOTION_HEAD_EN = "movements chain smoothly with weight and inertia, easing in and out"
MICRO_MOTION_TAIL_EN = "keep subtle lifelike micro-motion and ambient motion alive throughout"
_FOLLOW_ZH = {"human": "发丝与衣料", "animal": "毛发", "creature": "毛发与皮膜",
              "robot": "线缆与外挂配重", "spirit": "身周流光"}
_FOLLOW_EN = {"human": "hair and clothing", "animal": "fur", "creature": "fur and membranes",
              "robot": "cables and hanging counterweights", "spirit": "the light around it"}


def micro_motion(kinds=(), lang: str = "zh") -> str:
    """动力学地板：力学半句 + 随动附属物半句（后者按主体类型选词）+ 微动半句。

    `kinds` = 本镜出场角色的 `subject_kind` 序列（调用方从 `Project.shot_cast` 取）。
    多类型同框按登记顺序合并，不猜谁是主角；一个都认不出时**只发力学与微动两半**——
    宁可少说一层，也不要给动物发「衣料」这种它身上没有的东西。
    """
    zh = lang != "en"
    table = _FOLLOW_ZH if zh else _FOLLOW_EN
    seen, nouns = set(), []
    for k in kinds or ():
        if k in table and k not in seen:
            seen.add(k)
            nouns.append(table[k])
    if zh:
        follow = (f"，{'、'.join(nouns)}随动作自然跟随摆动后回落" if nouns else "")
        return MICRO_MOTION_HEAD_ZH + follow + "；" + MICRO_MOTION_TAIL_ZH
    # 分词短语而非「<名词> follow …」：`fur` 之类不可数名词在后者下主谓不一致，
    # 而名词是查表来的、数不固定，分词式对单复数都成立
    follow = (f", {' and '.join(nouns)} following the motion and settling naturally"
              if nouns else "")
    return MICRO_MOTION_HEAD_EN + follow + "; " + MICRO_MOTION_TAIL_EN


# 主体类型未登记时的形态（只有力学与微动两半）——调用方与守卫的缺省参照
MICRO_MOTION_ZH = micro_motion()
MICRO_MOTION_EN = micro_motion(lang="en")
# 去重锚：作者自己写过呼吸/起伏/微动就不再追加（85 条有正文的真实镜里 55 条已自写）
_MICRO_ECHO_ZH = ("呼吸", "起伏", "微动")
_MICRO_ECHO_EN = ("breath", "breathing", "shoulder drift", "micro-motion")

# 结构锁：契约句放开构图之后配套的那一句。放开构图 = 允许机位随运镜变，但不等于
# 允许模型自行切镜——缺这一句时 Seedance 会把「构图可以变」读成「可以换机位重新
# 起一个镜头」，一条 5 秒的镜被切成两三段。经验丰富的作者本就会在 `camera` 里手写
# 「一镜到底」「无跳切」，这里把它变成所有镜都有的地板。
#
# 措辞三条纪律：
#   · **写成肯定式**——本仓的权重模型是前位 token 权重最高（`video_prompt` 的
#     「镜头语言地板」段同据此把 camera 前置），把一串「不切/不分屏/不插视角」放进
#     最高权重位，正是本仓在别处否掉「负面串前置」时用的同一条反面理据。
#     肯定式说同一件事、不点名不想要的东西。
#   · **不许出现「一镜到底」**——那是 `camera.py` `oner` 预设的 label，会被读成
#     一条具体运镜指令（连续移动无跳切），而不是结构约束。
#   · **不许出现「机位」二字的否定式**（如"不切换机位"）——中文里它既可读成 no cut
#     也可读成 camera position 不变，后者与刚放开构图的契约句当场对撞。
STRUCT_LOCK_ZH = ("本镜是一段连续拍摄、只有一个主导运镜，"
                  "画面自始至终由同一台摄影机不间断记录")
STRUCT_LOCK_EN = ("This is one continuous take driven by a single dominant camera move, "
                  "recorded by the same camera without interruption")

# 播放速率地板：动作按真实速度演。
#
# 慢放多数不是模型主动加的，是被提示词算出来的：「前肢交替各三个循环」写在一个
# 10 秒镜上，等于把步频指定成 0.3Hz，而该物种冲刺的真实步频是 2.5~3Hz；同一镜里
# 「甩动全程约三秒」把一次 1~1.5 秒的动作摊薄了一倍。这类错误从字面看不出来——
# 它藏在「次数 ÷ 镜长」这个除法里，而**越听话的型号越会照着压慢**：同题对照下
# mini 会自行回到常识速度，2.5 严格执行、慢放当场现形。
#
# 速率是每一镜都成立的物理约束，写没写都该成立，故做成地板。点名了慢放/升格/
# 子弹时间/延时的镜不注入——那时快慢是有意的技法，地板会与它对撞。V2V 同样不
# 注入：运动节奏归参考视频管，再压一条速率指令是两个并列的运动权威。
PACE_ZH = ("画面内的动作以真实速度进行，不做整体慢放或快进，"
           "速度变化只来自动作本身的发力、惯性与收势")
PACE_EN = ("Action plays at real-world speed with no overall slow motion or speed-up; "
           "any change of pace comes only from the effort, inertia and settle of the "
           "motion itself")
# 去重锚：作者点名了变速技法就不再压这条（与 camera/sfx/结构锁同制）
_PACE_ECHO_ZH = ("慢放", "慢动作", "升格", "降格", "子弹时间", "延时", "快进", "变速", "抽帧")
_PACE_ECHO_EN = ("slow motion", "slow-mo", "slowmo", "bullet time", "speed ramp",
                 "timelapse", "time-lapse", "fast forward", "undercrank", "overcrank")
# 结构锁的去重锚：作者已经自己写过同款约束就不重复发（与 camera/sfx/cast_anchor 同制）。
_STRUCT_LOCK_ECHO_ZH = ("一镜到底", "无跳切", "不跳切", "连续拍摄", "长镜头")
_STRUCT_LOCK_ECHO_EN = ("one continuous take", "one-take", "continuous take", "no cuts",
                        "no cut", "single take", "long take")


def _wipe_markers() -> tuple[str, ...]:
    """镜内有意切一刀的技法标记，**从 `camera.CAMERA_PRESETS` 派生、不另写词表**。

    「长镜内无痕转场」（`foreground_wipe`）的 phrase 逐字写着「擦过瞬间机位与景别已
    无痕切换」——给这类镜发结构锁等于否掉作者刚点名的技法。判据跟着预设库走：
    预设加删 `wipe` 旗标时这里自动跟，不留第二份词表。"""
    from .camera import CAMERA_PRESETS
    out: list[str] = []
    for key, p in CAMERA_PRESETS.items():
        if not p.get("wipe"):
            continue
        # phrase 一并纳入：Skill 教作者抄进 camera 的是预设的中文措辞列，不是 key/label
        out += [key, str(p.get("label") or ""), str(p.get("label_en") or ""),
                str(p.get("phrase") or ""), str(p.get("phrase_en") or "")]
    return tuple(x for x in out if x)


# native 台词句的情绪化动词表。**确定性查表、缺省「说」**——引擎不造措辞，
# 只把作者已经填在旁边的 `emotion` 翻成一个口型模型看得懂的动词。
# 键按 `docs/kinema/project.schema.json` 声明的英文情绪档取（happy/sad/angry/
# surprised/fear/excited/coldness…）；中文键是存量写法的兼容位——两种写法实盘都有，
# 只认一种等于对另一半空转。
# 匹配一律小写归一后精确查表：模糊匹配会把「calm」在「becalmed」里命中。
DIALOGUE_VERB_ZH: dict[str, str] = {
    "angry": "嘶声怒喝道", "sad": "哽着声音说", "fear": "发着颤说",
    "surprised": "失声道", "surprise": "失声道", "excited": "扬声道",
    "coldness": "冷冷地说", "happy": "笑着说", "gentle": "放软了声音说",
    "愤怒": "嘶声怒喝道", "悲伤": "哽着声音说", "恐惧": "发着颤说",
    "紧张": "压着嗓子说", "震惊": "失声道", "委屈": "带着哭腔说",
    "痛苦": "咬着牙说", "开心": "笑着说", "凶悍": "嘶声怒喝道",
    "冷峻": "冷冷地说", "隐忍": "压着嗓子说", "决绝": "一字一顿地说",
}
# 旁白镜（无具体角色开口）的闭唇约束。**只是旁白约束的一半**——另一半是「这句
# 旁白到底念什么」，由逐句的 `画外旁白讲述：“…”` 给出（与混合镜里的旁白句同一
# 措辞）。此常量本身不带「画外旁白讲述」四字：句体已经说过一次，尾句再说一遍
# 就是同一句指令发两遍。
#
# 「哪些镜算旁白」**不是"speaker 空就算旁白"**——`speaker` 在 schema 里是游戏
# 对话框的名牌位，空值不等于旁白。判据走 `voicecast.voice_kind` 这个全仓单一
# 真源（lint 的语态维度用的是同一个），两处分叉就会出现「lint 说这是旁白、
# 提示词却让角色对口型」。
NARRATION_LIPS_ZH = "画面中人物口唇保持闭合、不做说话口型"
NARRATION_LIPS_EN = "on-screen characters keep their lips closed with no speaking mouth shapes"

# 「引擎明确要求模型不出声」的三种指令记号，分别由本模块三条 return 拼出：
# 无台词的 native 镜 / 混烧闭声的 native 旁白镜 / 无台词的 dubbed 镜。
# 片段快照按它们反查「这一条是不是按不出声的稿子生成的」
# （`compose._gate_native_double_voice`）——判据与实发正文共用同一份字面量，
# 措辞改动不会让下游静默放行。
NO_LINE_VOICE_MARK_ZH = "不加旁白或念白"
MUTE_VOICE_MARK_ZH = "模型不生成任何人声"
NO_VOICE_MARK_ZH = "本镜无人声"
NO_LINE_VOICE_MARK_EN = "no narration or voice-over"
MUTE_VOICE_MARK_EN = "the model generates no human voice"
NO_VOICE_MARK_EN = "no human voice in this shot"
_VOICELESS_MARKS = (NO_LINE_VOICE_MARK_ZH, MUTE_VOICE_MARK_ZH, NO_VOICE_MARK_ZH,
                    NO_LINE_VOICE_MARK_EN, MUTE_VOICE_MARK_EN, NO_VOICE_MARK_EN)


def positive_is_voiceless(positive: str) -> bool:
    """实发正文里有没有「不要出声」这条指令。

    白名单方向：认不出的稿一律当作开口稿。黑名单（枚举出声措辞）漏一种就静默
    放行，而出声句里的 `@配音N` 位序标记本就会把措辞切开。"""
    return any(m in positive for m in _VOICELESS_MARKS)


def _integer_spans(spans: list, total: float) -> list:
    """台词秒段整秒化：逐段边界取整、累计单调、每句至少 1 秒（总长允许时）——
    按字数比例切出的短句两端取整到同一秒会得到零长段，模型无处安放那句。"""
    out, prev, end_total = [], 0, int(total + 0.5)
    n = len(spans)
    for k, (_ln, _a, b) in enumerate(spans):
        # 末段收到总长；其余段取整后钳在 [上段末+1, 总长−剩余句数]，给后面每句留 1 秒
        end = end_total if k == n - 1 else min(max(prev + 1, int(b + 0.5)), end_total - (n - 1 - k))
        end = max(end, prev)
        out.append((prev, end))
        prev = end
    return out


def _fmt_sec(x: float) -> str:
    """秒段标签取整秒：响应时间戳的那一代（Seedance 2.5）官方口径是「以 1 秒为单位」，
    小数秒既不被响应也白占 token。`line_spans` 的秒段首尾相接，逐个取整后仍相接。"""
    return str(int(round(float(x))))


def voice_anchor_clause(anchors: list[dict], lang: str = "zh") -> str:
    """音色绑定句：把「说话人 ↔ 所附参考音」写进提示词（单音频措辞在
    seedance-2.0-mini 上经性别对照与双音频分角色绑定验证；多音频的 @配音N
    编号寻址与 @图片N 同一套官方位序语法，改动措辞须重做小样——见
    docs/agents/voice-anchor.md）。

    三个不可省的成分：① 多条参考音按 @配音{no} 编号点名（与逐句台词里的
    说话人标记同一套编号，模型靠它对位）；② 「只提供音色，不要复述参考音频
    里的内容」——缺这半句模型可能把参考音理解成对口型素材，复述锚定音里的
    试音句而不念台词；③ 同一 `no` 的多个说话人合并成一句（同一把声音只占
    一条参考位，见 `voicecast.voice_anchor_plan`）。

    条目带 `desc`（造出这把声音的那段声线描述）时补一段括注。厂商把「音色参考
    不准」列为已知问题，给的第一条解法就是在提示词里补音色特征描述——只发参考音
    等于让模型按画面去猜这把嗓子多大年纪。括注是**纯追加**：既有措辞一字不动，
    没有描述（模版音色恒无）时输出与不带 desc 时逐字节相同。"""
    if not anchors:
        return ""
    by_no: dict[int, list[str]] = {}
    desc_of: dict[int, str] = {}
    for a in anchors:
        no = int(a["no"])
        by_no.setdefault(no, []).append(str(a["who"]))
        # 描述是用户自由文本，可能带换行；同一 no 恒同一把声音，取第一个非空
        d = " ".join(str(a.get("desc") or "").split())
        if d and no not in desc_of:
            desc_of[no] = d

    if lang == "en":
        def _paren_en(no: int) -> str:
            return f" ({desc_of[no]})" if no in desc_of else ""
        if len(by_no) == 1:
            no, whos = next(iter(by_no.items()))
            binds = f"{' and '.join(whos)} speak with the voice of the attached reference audio{_paren_en(no)}"
            each = "deliver the lines in exactly the same voice, timbre and pitch as the reference audio. "
        else:
            binds = ", ".join(f"{' and '.join(whos)} speak with the voice of @Audio {no}{_paren_en(no)}"
                              for no, whos in sorted(by_no.items()))
            each = "each delivers the lines in exactly the same voice, timbre and pitch as the matching reference audio. "
        return f"{binds}: {each}The reference audio only provides the voice; do not repeat its content."

    def _paren(no: int) -> str:
        return f"（{desc_of[no]}）" if no in desc_of else ""

    if len(by_no) == 1:
        no, whos = next(iter(by_no.items()))
        binds = f"{'与'.join(whos)}的说话音色以所附参考音频为准{_paren(no)}"
        each = "用与参考音频完全相同的嗓音、音色与音高说出台词。"
    else:
        binds = "，".join(f"{'与'.join(whos)}的说话音色以 @配音{no} 为准{_paren(no)}"
                          for no, whos in sorted(by_no.items()))
        each = "各自用与对应参考音频完全相同的嗓音、音色与音高说出台词。"
    return f"{binds}：{each}参考音频只提供音色，不要复述参考音频里的内容。"


def native_voice_clause(shot: dict, *, total: float | None = None,
                        anchors: list[dict] | None = None,
                        mute: bool = False, unit: str = "second",
                        lang: str = "zh") -> str:
    """native 镜尾部的人声句：对白走「<角色><情绪动词>：“…”」，旁白走
    「画外旁白讲述：“…”」+ 闭唇约束。

    不区分语态的写法（`<speaker 或"角色">说：“…”，口型与台词同步`）有两处错：
    `speaker` 为空时编出泛称「角色」（`generic_name` 维度专门在拦的写法），
    并且把第三人称叙述当台词、要求模型为它配口型。

    **三种语态共用同一个逐句循环，只有尾句分三路**。旁白镜若在循环之前就返回
    一句不带文本的闭唇句，native 旁白镜发出去的提示词里就有「有人在画外
    讲述」这个设定、却没有讲述内容：模型只能自行编造旁白，而 `native_voiceover`
    缺省不烧固定音色，那条自编人声就是成片主音轨，与按 `narration` 烧录的字幕
    不同源。同一类漏在 `sketchboard.timeline_text` 的 `beats[].sound` 上出现过
    （板拿到了声音脚本、真正出声的视频模型拿不到）。
    旁白锚定音的绑定句「用与参考音频完全相同的嗓音说出台词」同理必须指向
    真发出的台词，不得指向一段从未给出的文本。

    逐句形态与秒段切分对三种语态一致：混合镜里的旁白句本就编译成
    「画外旁白讲述：“…”」，纯旁白镜复用同一措辞，不另造一套。

    台词取 `voicecast.shot_lines`——`docs/agents/native-voiceover.md` §5.3 纪律 7
    「有没有台词的判据统一走 voicecast」对本函数同样成立：读裸 `narration` 的话，
    只写 `lines[]` 的镜在这里退化成无台词镜，Seedance 收不到任何该说什么的指令
    且全程无报错。

    逐句成条而非拼成一段：句级 speaker/emotion 已由 `shot_lines` 归一化（未点名
    才继承镜级），一段一个说话人与动词才对得上镜内换人。`voice_kind` 是**整镜**
    口径，混合镜（对白里插一句旁白）在它眼里恒是 dialogue，故旁白句要在句级
    再判一次——否则那一句也会被要求配口型，正是本函数开头列的第二处错。

    情绪只在 `emotion` 命中表时替换动词，否则仍是「说」——引擎不猜、不造词。
    句级 `voice_instruction` 以括注跟在动词后：native 对白不经 TTS，这是作者
    语气指令抵达发声模型的唯一通道；`emotion_scale` 仍只归 TTS 表现力参数。

    `total`（本次请求秒数，与 `request_seconds` 同源传入）在场且镜内 ≥2 句时，
    逐句前置字数比例秒段（`voicecast.line_spans`，与 scored 底稿同一份切分）——
    多角色镜"第几秒轮到谁开口"因此有据可依；单句镜与未传 total 的调用不加秒段
    前缀（守卫钉死这条回落态）。

    `anchors`（`voice_anchor_plan` 的 anchored 清单）在场时追加音色绑定句，
    与随请求附发的 reference_audio 逐条对位——**句子与实附必须同真同假**，
    由 cli 侧保证（声明了一条不存在的参考音，模型会去找一个不存在的东西）。
    逐句的说话人同时挂 `@配音{no}` 标记（与 @图片N 同一套按 content[] 附发
    顺序的位序寻址）：绑定句交代音色归属，句级标记把编号钉到台词上，
    多角色镜靠它区分第几秒的句子用哪条参考音。

    `mute=True`（native 混烧的旁白/无词镜，`voicecast.burn_muted` 单一判据）：
    这些镜的人声由烧录轨（TTS）承担，模型再出声成片就是同一句两个人声——
    背景床只降 -8 dB，sidechain 闪避只在我们的音轨出声时触发，模型把同一句
    安排在句间静音段时压不住。故旁白镜整镜闭唇、不给讲述内容。对白镜**恒不
    闭声**：声源按镜分治，对白由模型原生发声、锚定照常附发——闭声出演的执行
    没有确定性保证（同稿实测两发一守一破），而对白镜的烧录轨与模型口型两条
    时间轴不同源，平移对齐只救得了第一句。

    无台词镜恒返回无人声地板：缺了这句，提示词里没有任何约束阻止模型自配人声。"""
    lines = voicecast.shot_lines(shot)
    en = lang == "en"
    if not lines:
        if en:
            return f"No dialogue in this shot; {NARRATION_LIPS_EN}; {NO_LINE_VOICE_MARK_EN}."
        return f"本镜无台词，{NARRATION_LIPS_ZH}，{NO_LINE_VOICE_MARK_ZH}。"
    voiceover = voicecast.voice_kind(shot) == "voiceover"
    if mute and voiceover:
        if en:
            return (f"The narration of this shot is dubbed in post; {MUTE_VOICE_MARK_EN}: "
                    f"{NARRATION_LIPS_EN}; ambient sound as usual.")
        return (f"本镜旁白由后期固定音色配音承担，{MUTE_VOICE_MARK_ZH}："
                f"{NARRATION_LIPS_ZH}，环境音效照常。")
    bind = voice_anchor_clause(anchors or [], lang)
    no_of = {str(a["who"]): int(a["no"]) for a in (anchors or [])}
    # 秒段只在响应时间戳的那一代发（`unit="second"`）：2.0 系列不响应秒段，
    # 且厂商指南把强行限制时长列为可能致生成异常——那一代只按顺序逐句列。
    # 秒段整秒化：响应时间戳的型号以 1 秒为单位，与拍表时间轴同一粒度
    spans = (_integer_spans(voicecast.line_spans(lines, total), total)
             if unit == "second" and total and len(lines) >= 2 else None)
    said, mixed = [], False
    for k, ln in enumerate(lines):
        spk = str(ln.get("speaker") or "").strip()
        text = ln.get("text") or ""
        at = ((f"{spans[k][0]}-{spans[k][1]}s: " if en else f"{spans[k][0]}-{spans[k][1]}秒：")
              if spans else "")
        # 整镜判为旁白时**每一句都是旁白**，与 `speaker` 是否写了名字无关：
        # `voice_kind` 返回 voiceover 的充要条件就是没有任何一句点名了非旁白
        # 说话人。混排镜（对白里插一句旁白）里那句旁白只能在句级认出来，判据
        # 与 `dubbed_voice_clause`、`voice_anchor_plan`、语态两条 lint 一致：
        # 点了旁白别名，或**没点名**。缺任一半都会编出没有主语的「说：“…”」
        # （`generic_name` 维度在拦的同一类写法）并要求为一句第三人称叙述配口型。
        instr = str(ln.get("voice_instruction") or "").strip()
        how = (f" ({instr})" if en else f"（{instr}）") if instr else ""
        if voiceover or voicecast.is_narrator(spk):
            mixed = True
            n = no_of.get(voicecast.NARRATOR_DISPLAY)
            if en:
                tag = f" @Audio {n}" if n else ""
                said.append(f'{at}off-screen narration{tag}{how}: “{text}”')
            else:
                tag = f" @配音{n} " if n else ""
                said.append(f'{at}画外旁白{tag}讲述{how}：“{text}”')
            continue
        emo = str(ln.get("emotion") or "").strip().lower()
        if en:
            tag = f" @Audio {no_of[spk]}" if spk in no_of else ""
            said.append(f'{at}{spk}{tag} says{how}: “{text}”')
        else:
            tag = f" @配音{no_of[spk]} " if spk in no_of else ""
            said.append(f'{at}{spk}{tag}{DIALOGUE_VERB_ZH.get(emo, "说")}{how}：“{text}”')
    if en:
        body = ("Dialogue timeline: " + "; ".join(said)) if spans else ", ".join(said)
        if voiceover:
            tail = f"; {NARRATION_LIPS_EN}."
        elif mixed:
            tail = "; characters lip-sync their own lines, the off-screen narration line has no lip-sync."
        else:
            tail = "; lip-sync matches the dialogue."
        return body + tail + (f" {bind}" if bind else "")
    body = ("台词时间轴：" + "；".join(said)) if spans else "，".join(said)
    # 尾句三路：整镜无人开口只需闭唇；镜内既有角色又有旁白要分别交代；
    # 纯对白照旧。`mixed` 是句级统计量，整镜旁白时它必为真，故先判 voiceover。
    if voiceover:
        tail = f"，{NARRATION_LIPS_ZH}。"
    elif mixed:
        tail = "，角色台词与口型同步，画外旁白那句不做口型。"
    else:
        tail = "，口型与台词同步。"
    return body + tail + (bind if bind else "")


def dubbed_voice_clause(shot: dict, lang: str = "zh") -> str:
    """dubbed 镜尾部的音频处置句：告诉模型所给参考音是谁的、谁该对口型。

    与 `native_voice_clause` 同一套语态判据（`voice_kind` 整镜口径 + 句级旁白
    再判），只是指令对象换成随请求附发的音频：native 是「模型自己念」，这里是
    「音频已给出、只做口型与身体」。不区分语态的单句写法（恒发「角色对口型」）
    有两处错：旁白镜会让画面里的人把旁白念出来——观众看到的是角色在说一段
    第三人称叙述；纯画面镜的参考音是等长静音，对着静音找口型只会张合乱动。

    对白句**具名绑定**：多人镜里只写「角色对口型」时，模型自选一张脸开口。
    说话与动作显式声明并行——对口型指令会抢占执行预算，不声明并行时模型
    倾向站定说完（三拍走位设计会被整包顶掉）。"""
    lines = voicecast.shot_lines(shot)
    if lang == "en":
        return _dubbed_voice_clause_en(lines)
    if not lines:
        return f"{NO_VOICE_MARK_ZH}，{NARRATION_LIPS_ZH}"
    voiceover = voicecast.voice_kind(shot) == "voiceover"
    if voiceover:
        return (f"@配音1 是画外旁白，不属于画面中的任何人物：{NARRATION_LIPS_ZH}，"
                "绝不与音频对口型")
    # @配音1 按 content[] 里 audio_url 的出现顺序编号（与 @图片N 同一套官方寻址）；
    # dubbed 每镜恒发一条整镜配音，故编号恒为 1。台词逐字随句列出：模型据此预知
    # 音节数与断句，口型贴合度高于只听音频盲对；人工审阅也能逐句核对谁说了什么
    parts, speakers, mixed = [], [], False
    for ln in lines:
        spk = str(ln.get("speaker") or "").strip()
        text = voicecast.line_text(ln)
        if voicecast.is_narrator(spk):
            mixed = True
            parts.append(f"画外旁白“{text}”（无人对口型）")
            continue
        if spk not in speakers:
            speakers.append(spk)
        parts.append(f"{spk}说：“{text}”")
    if mixed:
        return ("@配音1 按序为：" + "、".join(parts)
                + "——角色各自只在自己那句对口型，旁白句与非说话时刻"
                + f"{NARRATION_LIPS_ZH}；说话期间身体动作照常按运动设计进行")
    if len(speakers) == 1:
        text = "；".join(voicecast.line_text(ln) for ln in lines
                         if voicecast.line_text(ln))
        return (f"{speakers[0]} @配音1 说：“{text}”——由{speakers[0]}对口型说出"
                "@配音1 的内容，口型与音频严格同步，画面中其余人物口唇保持闭合；"
                "说话期间身体动作照常按运动设计进行，不因说话而静止")
    return ("@配音1 按序为：" + "、".join(parts)
            + "——各自只在自己那句对口型、口型与音频严格同步，其余时刻口唇闭合；"
            "说话期间身体动作照常按运动设计进行")


def _dubbed_voice_clause_en(lines: list) -> str:
    """`dubbed_voice_clause` 的英文体，语态判据与中文体同一份。"""
    if not lines:
        return f"{NO_VOICE_MARK_EN}; {NARRATION_LIPS_EN}"
    if all(voicecast.is_narrator(str(ln.get("speaker") or "").strip()) for ln in lines):
        return (f"@Audio 1 is off-screen narration belonging to nobody on screen: "
                f"{NARRATION_LIPS_EN}; never lip-sync to the audio")
    parts, speakers, mixed = [], [], False
    for ln in lines:
        spk = str(ln.get("speaker") or "").strip()
        text = voicecast.line_text(ln)
        if voicecast.is_narrator(spk):
            mixed = True
            parts.append(f"off-screen narration “{text}” (nobody lip-syncs)")
            continue
        if spk not in speakers:
            speakers.append(spk)
        parts.append(f"{spk} says: “{text}”")
    if mixed:
        return ("@Audio 1 in order: " + ", ".join(parts)
                + " — each character lip-syncs only their own line; during narration and "
                + f"when not speaking, {NARRATION_LIPS_EN}; body motion follows the motion design while speaking")
    if len(speakers) == 1:
        text = "; ".join(voicecast.line_text(ln) for ln in lines if voicecast.line_text(ln))
        return (f"{speakers[0]} @Audio 1 says: “{text}” — {speakers[0]} lip-syncs the content of "
                "@Audio 1 in strict sync with the audio while everyone else keeps their lips closed; "
                "body motion follows the motion design while speaking, no freezing to talk")
    return ("@Audio 1 in order: " + ", ".join(parts)
            + " — each lip-syncs only their own line in strict sync with the audio, lips closed "
            "otherwise; body motion follows the motion design while speaking")


def video_delta_missing(shot: dict) -> bool:
    """该镜是否**连一笔运动设计都没有**：两语种 video_prompt 与 delta 三字段全空。

    判据必须与 `video_prompt()` 真正落 `DELTA_FALLBACK_*` 的条件逐字一致——只看
    video_prompt 会让调用方对着"写了 action/end_state 的镜"喊兜底，日志与实发内容
    相反，用户反而看不出哪一镜真的没设计。单独成函数也是为了让 cli 的提示与本模块的
    取材口径同源，别在调用方另写一遍字段名，字段一改就分叉。"""
    # 与 lint 同为软判据：字段类型写坏按其字符串形态判，不在这里抛错
    return not any(str(shot.get(f) or "").strip()
                   for f in ("video_prompt", "video_prompt_en", *(f for f, _zh, _en in DELTA_FIELDS)))


# 防字地板逐词的同义锚：判「作者是不是已经写过这个词」。
# **跨语种**——中文 negative 里写英文词（或反之）在本仓是常见写法，只认本语种
# 就会把同一件事发两遍。
_TEXT_FLOOR_ALIASES: dict[str, tuple[str, ...]] = {
    "字幕": ("字幕", "subtitle"),
    "画面文字": ("画面文字", "画面内文字", "on-screen text", "on screen text"),
    "水印": ("水印", "watermark"),
    "subtitles": ("subtitle", "字幕"),
    "captions": ("caption", "字幕"),
    "on-screen text": ("on-screen text", "on screen text", "画面文字"),
    "watermark": ("watermark", "水印"),
}


# 闭声镜（native 混烧）的负面地板：正文的「不发出任何人声」是正向指令，负面串
# 是这类模型更听话的位置，两处同时说。实测残留形态：闭声稿下模型不念台词，但
# 「做口型」的表演可能带出零点几秒的哼声/气声，混烧后虽被 TTS 掩蔽，仍属杂质
MUTE_VOICE_FLOOR_ZH = "任何人声、说话声、念白、哼唱"
MUTE_VOICE_FLOOR_EN = "any human voice, speech, narration or humming"


def with_mute_voice_floor(neg: str, lang: str = "zh") -> str:
    """把闭声人声地板并进负面串（作者 negative 在前、地板在后，与防字地板同序）。"""
    neg = (neg or "").strip()
    floor = MUTE_VOICE_FLOOR_EN if lang == "en" else MUTE_VOICE_FLOOR_ZH
    anchor = "human voice" if lang == "en" else "人声"
    if anchor in (neg.lower() if lang == "en" else neg):
        return neg
    sep = ", " if lang == "en" else "，"
    return sep.join(x for x in (neg, floor) if x)


# 表演地板：缺省不叹气、不深呼吸、不流泪——视频模型把「有生命感」演过头的三种
# 惯用形态，不写也会自发出现。只有本镜正文点名了才算剧情要求：动作/终态/拍点/镜级
# 与句级情绪里明写了叹气、流泪、喘气之类，对应的词从地板里摘掉，其余照拦；否定写法
# （不落泪/没叹气/never cries）不算点名；台词文本不算（说「我哭了」不等于画面要流泪）。
PERFORMANCE_FLOOR: tuple[tuple[str, str, str, str], ...] = (
    # (中文地板词, 英文地板词, 中文点名措辞, 英文点名措辞)
    ("叹气", "sighing",
     r"叹气|叹息|叹了|长叹|叹一口气|叹口气",
     r"sigh"),
    ("深呼吸", "deep breaths",
     r"深呼吸|深吸|吸了一口气|吸一口气|大口喘|喘气|喘着|喘息",
     r"deep breath|breathes? in deeply|gasp|pant"),
    ("明显的胸肩起伏", "visible heaving of the chest or shoulders",
     r"胸口起伏|胸膛起伏|胸口剧烈|肩膀起伏|肩背.{0,3}起伏|肩.{0,2}耸动|喘气|喘着|喘息|急促呼吸|呼吸急促|呼吸沉重",
     r"chest heav|shoulders? heav|heaving|panting|breathing hard|breathes? hard"),
    ("流泪", "tears",
     r"流泪|落泪|眼泪|泪水|泪珠|泪光|泪痕|含泪|哭",
     r"tear|cry|cries|crying|weep|sob"),
)
_PERF_NEG_ZH = "(?<![不没别无未非])(?<!没有)(?<!不会)(?<!不再)(?<!不要)(?<!绝不)(?<!不许)(?<!禁止)(?<!不能)(?<!不该)"
_PERF_NEG_EN = r"(?<!no )(?<!not )(?<!never )(?<!without )(?<!don't )(?<!doesn't )(?<!won't )"
_PERF_ASK = tuple((zh, en, re.compile(_PERF_NEG_ZH + "(?:" + pz + ")"),
                   re.compile(_PERF_NEG_EN + "(?:" + pe + ")"))
                  for zh, en, pz, pe in PERFORMANCE_FLOOR)
_PERF_FIELDS = ("video_prompt", "video_prompt_en", "action", "end_state", "emotion")


def performance_hay(shot: dict) -> str:
    """本镜里算作「点名要它」的正文：运动正文与 delta 骨架、拍点动作、镜级与句级情绪。
    两语种字段都收——作者用哪种语言写都能点名。"""
    shot = shot if isinstance(shot, dict) else {}
    parts = [str(shot.get(f) or "") for f in _PERF_FIELDS]
    sk = shot.get("sketch") if isinstance(shot.get("sketch"), dict) else {}
    for b in sk.get("beats") or []:
        if isinstance(b, dict):
            parts.append(str(b.get("action") or ""))
    for ln in shot.get("lines") or []:
        if isinstance(ln, dict):
            parts.append(str(ln.get("emotion") or ""))
    return " ".join(p for p in parts if p)


def performance_floor(hay: str, lang: str = "zh") -> list[str]:
    """按正文求本镜的表演地板词：作者点名了的摘掉，其余保留。"""
    hay = hay or ""
    low = hay.lower()
    return [(en if lang == "en" else zh)
            for zh, en, pz, pe in _PERF_ASK
            if not (pz.search(hay) or pe.search(low))]


def with_performance_floor(neg: str, hay: str, lang: str = "zh") -> str:
    """把表演地板并进负面串（作者 negative 在前、地板在后；作者 negative 已写的词不重复）。"""
    neg = (neg or "").strip()
    low = neg.lower()
    terms = [t for t in performance_floor(hay, lang)
             if t not in (low if lang == "en" else neg)]
    if not terms:
        return neg
    floor = ", ".join(terms) if lang == "en" else "、".join(terms)
    sep = ", " if lang == "en" else "，"
    return f"{neg}{sep}{floor}" if neg else floor


def with_text_floor(neg: str, lang: str = "zh") -> str:
    """把防字地板并进负面约束串：**作者的 negative 在前、地板在后**。

    顺序是硬约束——下游（和测试）按「避免出现：<作者原话>」做子串定位。

    **去重是逐词的，不是整块的。** 若整块去重、只认「字幕」/`subtitle` 一个锚：作者
    写了它就整块跳过（连带丢掉「画面文字/水印」的保护），没写它就整块注入（哪怕作者
    已经写过「水印」）。而本仓自己的字段范例（`references/storyboard.md` 的
    `negative_prompt` 那一格）教作者写的恰恰是「文字水印」——不含「字幕」二字，
    锚接不住，写了 negative 的正镜会成批把「水印」发两遍；同一份文档一处教作者写、
    另一处又说「引擎已兜底不必手写」。
    逐词去重同时解掉两头：作者写过的词不重复发，没写的词照样补齐。"""
    neg = (neg or "").strip()
    parts = TEXT_FLOOR_EN.split(", ") if lang == "en" else TEXT_FLOOR_ZH.split("、")
    low = neg.lower()
    missing = [p for p in parts
               if not any(a in (low if a.isascii() else neg) for a in _TEXT_FLOOR_ALIASES[p])]
    if not missing:
        return neg
    floor = ", ".join(missing) if lang == "en" else "、".join(missing)
    sep = ", " if lang == "en" else "，"
    return f"{neg}{sep}{floor}" if neg else floor


# 板地板：简笔板随请求附上时补进负面串的词。逐词各对着一种泄漏形态——
# 标注箭头（红蓝两色画进画面）、格线/分格版面、铅笔素描质感、手写标注。
# 绿框/橙箭头/紫波浪与 sketchboard.board_role_clause 的五色语义同批补齐：
# 正文陈述句 + 负面串两处同时说才压得住（该模块「位置即效力」说明的既有结论），
# 正文补了三色而负面串不补就是半套。
# 与防字地板同制：作者的 negative 在前、地板在后，作者自己写过就不重复注入。
BOARD_FLOOR_ZH = ("标注箭头, 红蓝箭头, 绿色取景框, 橙色标注箭头, 紫色波浪线, "
                  "分镜格线, 多格分格画面, 铅笔素描, 草图线稿质感, 手写标注文字")
BOARD_FLOOR_EN = ("annotation arrows, red or blue arrows, green framing boxes, "
                  "orange annotation arrows, purple squiggles, storyboard panel grid, "
                  "multi-panel layout, pencil sketch, rough line-art texture, handwritten labels")


def with_board_floor(neg: str, lang: str = "zh") -> str:
    """把板地板并进负面约束串（只在**板真的随请求附上**时调用）。

    正文头部的职责声明说明「板是脚本不是画面」，这里是同一件事的负面表达——
    实测证明只说正面压不住：模型在没有 first_frame 的参考任务里，开头几帧由几张
    参考图调和而来，板上的箭头就在那几帧里渗进画面。"""
    neg = (neg or "").strip()
    floor = BOARD_FLOOR_EN if lang == "en" else BOARD_FLOOR_ZH
    if ("标注箭头" in neg) or ("annotation arrow" in neg.lower()):
        return neg
    sep = ", " if lang == "en" else "，"
    return f"{neg}{sep}{floor}" if neg else floor


_ZH_SENTENCE_END = ("。", "！", "？", "…", "”", "」", "》")


def zh_join_all(parts, sep: str = "，") -> str:
    """按标点感知把多段中文拼起来（`_zh_join` 的多段公开版，供 cli 侧共用）。

    调用方**不许自己写 `sep.join(...)`**：同一个病灶在本仓已经出现过两个拼接点
    （`prompts` 的 sfx/负面串/台词句用「，」、`cli._cast_anchor_text` 的剪影锚点
    用「；」），各写一份判断就会各修一半。"""
    out = ""
    for p in parts:
        p = str(p or "").strip()
        if not p:
            continue
        if not out:
            out = p
            continue
        out = out.rstrip() + ("" if out.rstrip().endswith(_ZH_SENTENCE_END) else sep) + p
    return out


def _zh_join(head: str, tail: str) -> str:
    """中文段拼接：`head` 已经用句末标点收尾时**不再补逗号**。

    这是「。，」标点缝的单一修法。裸的 `"，".join([...])` 在作者的 `video_prompt`
    以句号收尾时会产出「…的一刻。，环境音效：…」。

    **必须集中修、不能只补 sfx 那一处**：`with_text_floor("")` 恒返回非空地板，
    所以负面串那一拼是**无条件**执行的——没写 sfx、只要正文以句号收尾，
    照样产出「。，避免出现：…」。落点四处：sfx / 角色锚 / 负面串 / 台词句。
    en 侧不走这里（英文体本就用 ". " 连接，且末尾不带中文句号）。"""
    head = (head or "").rstrip()
    tail = (tail or "").strip()
    if not head:
        return tail
    if not tail:
        return head
    return head + ("" if head.endswith(_ZH_SENTENCE_END) else "，") + tail


def negative_clause(neg: str, lang: str = "zh") -> str:
    """把负面串包成**肯定式约束句**（国产模型口径：负向不是 API 参数，是正向句子）。

    连接词随语种走——地板缺省常开，若硬拼中文「。避免出现：」，
    每一条英文提示词（nano-banana / veo 走 Gemini，`prompt_lang: en`）末尾都会
    挂一截中文，白占 token 还可能让模型误以为要画中文字。"""
    neg = (neg or "").strip()
    if not neg:
        return ""
    return f"Avoid: {neg}" if lang == "en" else f"避免出现：{neg}"


def select_style_prefix(params: dict, prompt_lang: str = "zh",
                        doc: dict | None = None) -> tuple[str, bool]:
    """按 provider 语言偏好选画风前缀：en 模型优先 `style_prefix_en`。

    画风解析链（单点可控）：**项目/章节文档顶层 `style_prompt`/`style_prompt_en`**
    （立项时从 profile 快照落位，一处设计全片取用——分镜图/设定图/封面同源，
    手改文档该字段即全局换画风）> profile 的 style_prefix/style_prefix_en
    （models.yaml）。无该字段的项目直接落到 profile 前缀档。

    换厂商的隐性失效点之一：中文画风前缀拼进英文优先模型的提示词会打折——
    每个 profile 都配了双语前缀（models.yaml），这里按 prompt_lang 选用。
    返回 (前缀, 是否发生"缺英文回退中文"的降级)——调用方据此警告一次。"""
    doc = doc or {}
    zh = ((doc.get("style_prompt") or "").strip()
          or (params.get("style_prefix") or "")).rstrip("，,。、 ")
    en = ((doc.get("style_prompt_en") or "").strip()
          or (params.get("style_prefix_en") or "")).strip().rstrip(",. ")
    if prompt_lang == "en":
        return (en, False) if en else (zh, bool(zh))
    return zh, False


def image_prompt(shot: dict, *, style_prefix: str = "", character_block: str = "",
                 character_negative: str = "",
                 scene: str = "", prompt_lang: str = "zh",
                 text_floor: bool = True, ref_base: bool = False,
                 cast_empty: bool = False, include_negative: bool = True) -> str:
    """构造该镜发给图像模型的完整提示词。

    `character_block` 是**本镜的角色文字锚**——调用方（cli 生图段）用
    `Project.shot_cast` + `character_anchor_block` 按镜装配：只含本镜出场角色、
    设定图在场者只留绑定句。**绝不整块灌全员外貌清单**（实测 33 人 1986 字
    的图鉴块把 60 字正文压死，26 镜里大片被画成人物设定总表）。

    `ref_base=True` 表示本次请求**真的附带了设定图参考**（cli 按 design_refs 实况
    传入）——注入设定图参考契约句（外观基准/版式禁令/未出场不画），与视频侧
    「以所给首帧为画面基准」同范式：参考媒体在场，就必须声明它的用途边界。

    `text_floor=False` 关掉防字地板——只给「画面里本来就该有字」的画风
    （game_sim 的 HUD、explainer 的信息图），由 profile 的
    `image.image_text_floor: false` 声明，调用方（cli 生图段）读出后传入。
    这一档同时关掉**单帧剧情契约句与防设定表地板**：HUD/信息图的帧本就不是
    剧情画面，硬灌「叙事张力」反而与画风打架。
    气泡/对话框/榜单等画风的字全是合成段 ASS 后置烧录，**恰恰要求图本体干净**，
    是防字地板的受益方，不在 opt-out 之列。"""
    # 双语提示词：中文为主（image_prompt）、英文为辅（image_prompt_en）——
    # 按 provider 语言偏好选主字段（国产 zh / 海外 en），缺失自动回退另一语言
    body_zh = (shot.get("image_prompt") or shot.get("narration") or "").strip()
    body_en = (shot.get("image_prompt_en") or "").strip()
    body = (body_en or body_zh) if prompt_lang == "en" else (body_zh or body_en)
    # 摄影字段地板：景别/角度/焦段/光线自动并入镜头语言块——这些结构化字段
    # 是分镜表的专业性所在，必须真正进提示词。Skill 已写进 body 的不重复注入；
    # 英文体不混中文字段（英文版由 Skill 用地道术语自带）。
    if prompt_lang != "en":
        cine = "，".join(v for v in ((shot.get(f) or "").strip()
                                     for f in ("framing", "angle", "lens", "lighting"))
                         if v and v not in body)
        if cine:
            body = "，".join(x for x in [cine, body] if x)
    # 单帧剧情契约句（随 text_floor 画风门）+ 设定图参考契约句（随 ref_base 实况）：
    # 两句都排在风格前缀之后、角色锚之前——先定性「这是一格戏」再报出场表，
    # 角色锚才会被读成「此刻在画面里的人」而不是「要画的图鉴条目」。
    # `cast_empty=True`（本镜无具名出场角色）换无人变体，避免完整版的「人物有具体
    # 动作与情绪神态」把角色凭空塞进空镜（cli 按角色锚实况传入）。
    if text_floor:
        if cast_empty:
            story = STORY_FRAME_NOCAST_EN if prompt_lang == "en" else STORY_FRAME_NOCAST_ZH
        else:
            story = STORY_FRAME_EN if prompt_lang == "en" else STORY_FRAME_ZH
    else:
        story = ""
    refc = (REF_BASE_EN if prompt_lang == "en" else REF_BASE_ZH) if ref_base else ""
    # 风格前缀 + 剧情契约 + 参考契约 + 本镜角色锚 + 固定场景 + 镜头语言块 + 本镜动作
    prompt = (". " if prompt_lang == "en" else "，").join(
        x for x in [style_prefix, story, refc, character_block, scene, body] if x)
    neg = "，".join(x for x in ((shot.get("negative_prompt") or "").strip(),
                                (character_negative or "").strip()) if x)
    # 防字地板：分镜图本体必须无字——字幕由合成段按画风样式后置烧录，
    # 模型自作主张画进画面的字/水印删不掉（局部改造要另花钱），必须在提示词层拦死。
    # 防设定表地板紧随其后（作者原话恒在最前）：分镜图绝不许长成设定资产的版式。
    if text_floor:
        neg = with_text_floor(neg, prompt_lang)
        neg = with_sheet_floor(neg, prompt_lang)
    if neg and include_negative:   # 负面约束：国产模型用肯定式约束句（Seedance 2.0 惯例）
        prompt += (". " if prompt_lang == "en" else "。") + negative_clause(neg, prompt_lang)
    # 驳回→提示词闭环（引擎兜底）：重做意见直接编译进本次提示词；
    # Skill 层可先按意见改写 image_prompt（更优），改写后应把状态置回 wfa/todo。
    fix = review.get_note(shot, "image") if review.needs_retake(shot, "image") else None
    if fix:
        prompt += (f". Revision focus (must apply): {fix}" if prompt_lang == "en"
                   else f"。本次修正重点（务必执行）：{fix}")
    return prompt


def video_prompt(shot: dict, *, native: bool, lang: str = "zh",
                 flf2v: bool = False, ref_video: bool = False,
                 sketch: bool = False, sketch_board: bool = False,
                 sketch_total: float | None = None,
                 cast_anchor: str = "", subject_kinds=(), ref_mode: bool = False,
                 ref_base: bool = False,
                 ref_sheets: int = 0, ref_manifest=None,
                 voice_anchors: list[dict] | None = None,
                 native_mute: bool = False,
                 character_negative: str = "",
                 timeline_unit: str = "second",
                 include_negative: bool = True) -> str:
    """构造该镜发给视频模型的创作提示词（不含引擎追加的 --resolution/--ratio/--duration 后缀）。

    双语：video_prompt 中文为主、video_prompt_en 英文为辅，按 provider 语言偏好选用。

    **增量编译**：视频请求恒带该镜分镜图，画面基底已经给定，提示词只是增量——
    首句是增量契约句（native=首帧 / dubbed=参考图，措辞二分），正文取 `video_prompt`
    并前置 `action`/`end_state`/`light_shift` 的 delta 骨架（**两语种都注入**，en 换
    英文标签发同一批值），六者全空才落兜底句。**绝不回退 `image_prompt`**（整条复述
    首帧=最强漂移源）。

    `flf2v=True` 表示本镜**实际发出了末帧**（首尾帧衔接成立），追加「只写过渡过程」
    铁律句。该判据由 cli 侧算好传入——**本模块绝不自读 `project.frame_chain` 或
    `shot["last_frame"]`**：项目顶层写了 frame_chain 也可能因 dubbed / 下一镜缺图
    而根本没发末帧，自读必然与实际请求分叉。

    `ref_video=True` 表示本镜**实际发出了参考视频**（Seedance 2.0 V2V / previz
    运动迁移）。此时契约句换 V2V 版并**压过 native/flf2v 两个措辞分支**——V2V 下
    图挂的是 `role=reference_image` 而非首帧、且不发末帧（见 seedance.py 的分支），
    再说「以所给首帧为基准」或「收束到末帧」都是在描述一个没发出去的东西。
    同 flf2v，判据由 cli 侧算好传入，本模块不自读 `shot["previz"]`。

    `sketch=True` 表示本镜走简笔分镜板路径（guide=sketch 且 beats 在）：把
    `shots[].sketch.beats` 编译成**分段时间轴**（timeline prompting）前置在正文头，
    `sketch_board=True` 再叠加板的职责声明与风格防护句——**该旗标必须与"板真的
    随请求附上了"逐字一致**（由 cli 按 `_shot_plan` 算好传入）：板没附上却声明
    「所附分镜板」，模型会去找一个不存在的参考。sketch 与 ref_video 互斥
    （`sketchboard.active_guide` 仲裁），两真同传属调用方 bug，本函数按 V2V 优先。

    `ref_manifest`=按 content[] 图片顺序的 `[(kind, label), …]` 全清单（含
    frame/board 占位）——在场时设定图绑定走逐张 `@图片N` 点名（官方引用语法，
    见 `sheet_binding_clause`），缺省回落 `ref_sheets` 计数的泛称半句。"""
    # 「本镜真的是首尾帧衔接」的唯一判据——**必须在正文兜底之前算好**：兜底句也
    # 按它二分（链上落"沿最短路径过渡到末帧"、非链上落"保持不变"），V2V 下没有末帧
    # 却落了链上兜底句，就会向模型要求一个根本不存在的收束目标。
    chained = bool(native and flf2v) and not ref_video
    zh = (shot.get("video_prompt") or "").strip()
    en = (shot.get("video_prompt_en") or "").strip()
    body = (en or zh) if lang == "en" else (zh or en)
    # delta 骨架地板：结构化"本镜差异"字段并入正文（照摄影地板的 `v not in body`
    # 去重范式——指挥层已写进 video_prompt 的不重复注入）。**两语种都注入**：这三个
    # 字段没有 `_en` 对位，en 侧按"缺失互为回退"取同一批值、只换英文标签；丢弃它们
    # 会让正文落到 DELTA_FALLBACK_EN 那句"只做轻微呼吸"上，把作者的运动设计反着发出去。
    sep = ". " if lang == "en" else "，"
    delta = sep.join(f"{en_label}: {v}" if lang == "en" else f"{zh_label}：{v}"
                     for f, zh_label, en_label in DELTA_FIELDS
                     if (v := (shot.get(f) or "").strip()) and v not in body)
    if delta:
        body = sep.join(x for x in [delta, body] if x)
    if not body:
        # 全空兜底：既没写 video_prompt 也没写 delta 三字段。**这里刻意不回退
        # image_prompt**——那是整条首帧复述，会要求模型把主体与场景重画一遍。
        # FLF2V 另走一句：两端已 pin，此时说"只做轻微自然的呼吸与环境流动"会和
        # "收束到末帧"直接打架——该说的是"沿最短自然路径过渡过去"。
        if chained:
            body = FLF2V_FALLBACK_EN if lang == "en" else FLF2V_FALLBACK_ZH
        else:
            body = DELTA_FALLBACK_EN if lang == "en" else DELTA_FALLBACK_ZH
        micro = False       # 落了兜底句 = 本就在说"只做轻微呼吸"，不再追加微动尾句
    else:
        # 链上镜也不追加：`FLF2V_ZH` 已经在要求「运动须自然收束在末帧上」，
        # 再说一句「全程保留…」是给收束动作加一条并行的持续指令，两者会互相稀释。
        micro = not chained
    vmotion = body
    # 简笔板时间轴（timeline prompting）：分段时间结构是 Seedance 2.0 消化长动作的
    # 正道。authored beats 时前置在 delta/正文之前（正文仍有拍级之外的补充信息）；
    # **自动拆拍时直接替代正文**——时间轴本就是正文按句读切出来的，两者并存等于
    # 同一批句子发两遍。V2V 在场不注入（互斥仲裁在 cli._shot_plan，按 V2V 优先）。
    tl_sound = False
    if sketch and not ref_video:
        from ..sketchboard import effective_beats, timeline_has_sound, timeline_text
        # sketch_total = 实际请求秒数（cli 从 request_seconds 算好传入）——时间轴的
        # 秒段必须与片长同源，缺省裸用 dur 会在折停顿/配音实测的项目上给出假脚本
        # native 透传给时间轴：逐拍 `sound` 与镜级 sfx 同一道门（模型原生出音才发）
        tl = timeline_text(shot, lang, total=sketch_total, native=native,
                           unit=timeline_unit)
        if tl:
            _bs, auto = effective_beats(shot, sketch_total)
            vmotion = tl if auto else (". " if lang == "en" else "。").join([tl, vmotion])
            tl_sound = timeline_has_sound(shot, total=sketch_total, native=native)
    # 微动恒常尾句：**注入点必须在 sketch 块之后**——自动拆拍那一支是
    # `vmotion = tl`（整体替代正文），注在 body 上会被它静默吞掉。
    # 仍在 camera 前置之前，所以运镜依旧是创作正文的首位 token。
    # 去重与 camera/sfx/cast_anchor 同制：作者自己写过呼吸/起伏就不重复发。
    if micro:
        echoes = _MICRO_ECHO_EN if lang == "en" else _MICRO_ECHO_ZH
        hay = vmotion.lower() if lang == "en" else vmotion
        if not any(e in hay for e in echoes):
            floor = micro_motion(subject_kinds, lang)
            vmotion = (". ".join([vmotion, floor]) if lang == "en"
                       else _zh_join(vmotion, floor))
    # 镜头语言地板：运镜是视频模型最听话的指令，不能只当标注躺在 JSON 里；
    # 它是**创作内容里的首位 token**（只让位于增量契约句这条画面基准声明）
    if lang != "en":
        cam = (shot.get("camera") or "").strip()
        if cam and cam not in vmotion:
            vmotion = "，".join(x for x in [f"运镜：{cam}", vmotion] if x)
        if native:
            # 逐拍 sound 已经把声音设计写进时间轴时不再发镜级汇总版——同一套设计
            # 两者并存时发两遍，即复述
            sfx = (shot.get("sfx") or "").strip()
            if sfx and not tl_sound and sfx not in vmotion:
                vmotion = _zh_join(vmotion, f"环境音效：{sfx}")
    # 角色绑定句：本镜出场角色的名字与外观锚点。
    # 图生视频的三种模式互斥——首帧/首尾帧模式在协议上**不能附带参考图**
    # （官方明写不可混用，适配器亦硬拦），所以角色设定图进不了这次请求。
    # 但设定的**文字部分**能进：把名字与剪影锚点、硬约束写进提示词，模型在
    # 运动中改画主体时有据可依。缺了这句，只要首帧里角色不够清晰（远景、背影、
    # 或角色在本镜中途才入画），模型就会按训练集均值另造一个人。
    if cast_anchor and cast_anchor not in vmotion:
        vmotion = (". ".join([cast_anchor, vmotion]) if lang == "en"
                   else _zh_join(cast_anchor, vmotion))
    # 结构锁：契约句放开构图之后的配套地板，**一句、且只在真正的单机位连续镜上发**。
    # 两道门（少一道都会发出一句与本镜设计打架的话）：
    #   · ref_video(V2V)  → CONTRACT_V2V_ZH 已在说「严格跟随参考视频1的运镜」，
    #                       再压一句"同一台摄影机不间断"是两条并列的运动权威。
    #   · 镜内擦镜        → `foreground_wipe` 一族是本仓正在教的「长镜内无痕转场」，
    #                       判据从 CAMERA_PRESETS 的 `wipe` 旗标派生（见 `_wipe_markers`）。
    # ref_mode（全能参考）**不整体豁免**：它是 native 缺省档，一镜一片的语义
    # 恰恰要求"一段连续拍摄"——缺这句时模型会把「构图可以变」读成「可以换机位重新
    # 起一个镜头」，5 秒的镜被切成两三段。附板同样要发：板只管拍序，分段时间轴
    # 对支持多镜的型号本就有切镜压力，结构锁是附板镜唯一的连续性约束。
    # 另加一道去重：作者已经自己写过「一镜到底/无跳切/one continuous take」就不重复发。
    struct_src = " ".join([str(shot.get("camera") or ""), str(shot.get("camera_preset") or ""),
                           vmotion])
    echoes = _STRUCT_LOCK_ECHO_EN if lang == "en" else _STRUCT_LOCK_ECHO_ZH
    hay = struct_src.lower() if lang == "en" else struct_src
    struct_lock = not ref_video
    if struct_lock:
        struct_lock = not any(m and m in struct_src for m in _wipe_markers())
    if struct_lock:
        struct_lock = not any(e in hay for e in echoes)
    # 播放速率地板：扫描面与结构锁同一份 `struct_src`（camera / camera_preset / 正文）
    # ——变速技法既可能写在运镜里也可能写在正文里，只扫一边就会漏。
    pace_echoes = _PACE_ECHO_EN if lang == "en" else _PACE_ECHO_ZH
    pace_lock = not ref_video and not any(e in hay for e in pace_echoes)
    # 增量契约句（无条件前置）：声明画面基准，把整条提示词定性为"增量"。
    # 措辞**六分**而非二分（各支有各自的实拍标定，改一支不要顺手改另一支）：
    #   · native 首帧  → 衔接参与镜（章级/镜级 frame_chain）：首帧为基准；
    #                    身份锁定，构图与机位随本镜运镜放开
    #   · dubbed      → 参考图为基准（seedance 走 role=reference_image，不是首帧）；
    #                    真附了设定图（缺省即附）拼设定图那半句
    #   · native+FLF2V → 首尾两端都被 pin，**构图必须放开**（末帧是下一镜的分镜图，
    #                    本就是另一个构图）——沿用单帧措辞会让"构图保持一致"与
    #                    "收束到末帧"两句自相矛盾，模型只能二选一。
    #   · V2V（参考视频） → 图是参考图、另有一段参考视频，且**不发末帧**：措辞
    #                    压过上面两支，另外必须点明「只抄运动、别抄灰模的画风」。
    #   · 全能参考（ref_mode·native 缺省档） → 无首/末帧槽，分镜图是第一张参考图；
    #                    真附了设定图才拼设定图那半句。
    #                    判据由 cli 侧算好传入（与 sketch_board 同款「与实附一致」纪律）。
    #   · 取景地变体（ref_mode × ref_base，写实档降级路线） → @图片1 是场景基准图
    #                    而非本镜画面：它只交代陈设与光线，构图与人物由板、提示词
    #                    与身份图各自承载——沿用「本镜画面」措辞会让模型把一张
    #                    无人空景当成品构图照抄。
    if sketch_board:
        from ..sketchboard import board_role_clause
    # 设定图绑定半句：manifest 在场走逐张 @图片N 点名（编号真源=附图顺序），
    # 否则回落计数泛称——两种措辞都以「真附了」为前提，一张没附恒为空串
    bind = (sheet_binding_clause(ref_manifest, lang) if ref_manifest
            else ((ALLREF_SHEETS_EN if lang == "en" else ALLREF_SHEETS_ZH)
                  if ref_sheets else ""))
    if lang == "en":
        head = [CONTRACT_V2V_EN if ref_video
                else (CONTRACT_FLF2V_EN if chained
                      else ((allref_base_contract(shot, lang) if ref_base
                             else CONTRACT_ALLREF_EN) + bind
                            if ref_mode
                            else (CONTRACT_FIRST_EN if native
                                  else (allref_base_contract(shot, lang) if ref_base
                                        else CONTRACT_REF_EN) + bind)))]
        if chained:
            head.append(FLF2V_EN)
        # 板的职责声明紧跟画面基准句——**位置即效力**，见 sketchboard._BOARD_ROLE_ZH。
        # 降级路线下分镜图不进请求，板的画风归属改指 @图片1 场景基准图
        if sketch_board:
            head.append(board_role_clause(lang, base=(
                "the location base plate (@Image 1)" if ref_base else None)))
        if struct_lock:
            head.append(STRUCT_LOCK_EN)
        if pace_lock:
            head.append(PACE_EN)
        vmotion = ". ".join(head) + ". " + vmotion
    else:
        head = [CONTRACT_V2V_ZH if ref_video
                else (CONTRACT_FLF2V_ZH if chained
                      else ((allref_base_contract(shot, lang) if ref_base
                             else CONTRACT_ALLREF_ZH) + bind
                            if ref_mode
                            else (CONTRACT_FIRST_ZH if native
                                  else (allref_base_contract(shot, lang) if ref_base
                                        else CONTRACT_REF_ZH) + bind)))]
        if chained:
            head.append(FLF2V_ZH)
        if sketch_board:
            head.append(board_role_clause(lang, base=(
                "@图片1 的场景基准图" if ref_base else None)))
        if struct_lock:
            head.append(STRUCT_LOCK_ZH)
        if pace_lock:
            head.append(PACE_ZH)
        vmotion = "。".join(head) + "。" + vmotion
    neg = "，".join(x for x in (
        (shot.get("negative_prompt") or "").strip(),
        character_negative.strip(),
    ) if x)
    # 防字地板：字幕由合成段按画风样式后置烧录，视频本体必须是干净画面——
    # 模型自作主张画进去的字幕/水印删不掉，必须在提示词层拦死（与图像侧同一套口径）
    # 表演地板排在防字地板之前：「水印」恒是整条提示词的尾词，下游与守卫按它定位负面串末尾
    neg = with_performance_floor(neg, performance_hay(shot), lang)
    neg = with_text_floor(neg, lang)
    # 板地板：附了板才加。正文那句「板不是画面参考」压不住标注箭头（实测 6 镜里
    # 2 镜把红蓝箭头画进开头几帧）——负面串是这类模型最听话的位置，两处同时说。
    if sketch_board:
        neg = with_board_floor(neg, lang)
    # 闭声只作用于烧录承担的镜（burn_muted 单一判据）：对白镜即便被传了 mute
    # 也按发声编译——人声地板压在发声稿上就是自相矛盾的提示词
    native_mute = native_mute and voicecast.burn_muted(shot)
    if native and native_mute:
        neg = with_mute_voice_floor(neg, lang)
    # 驳回→提示词闭环（引擎兜底）：重做意见编译进运动提示词
    fix = review.get_note(shot, "clip") if review.needs_retake(shot, "clip") else None
    if fix:
        vmotion = ((vmotion + f". Revision focus (must apply): {fix}.").lstrip(". ") if lang == "en"
                   else (vmotion + f"。本次修正重点（务必执行）：{fix}。").lstrip("。"))
    if native:
        # total 与请求秒数同源（sketch_total 即 cli 算好的 request_seconds）：
        # 多段镜的台词时间轴按它铺秒段；voice_anchors 与实附 reference_audio
        # 同真同假（cli 侧仲裁），绑定句绝不声明一条没发出去的参考音。
        # native_mute（混烧的旁白/无词镜）由 cli 按 native_voiceover ×
        # voicecast.burn_muted 逐镜传入，本模块不自读——运行时 --burn-voice
        # 覆盖只有调用方看得见；对白镜恒 False（声源按镜分治，不闭声）
        voice = native_voice_clause(shot, total=sketch_total, anchors=voice_anchors,
                                    mute=native_mute, unit=timeline_unit, lang=lang)
        vmotion = f"{vmotion.rstrip()} {voice}" if lang == "en" else _zh_join(vmotion, voice)
    else:
        voice = dubbed_voice_clause(shot, lang)
        vmotion = f"{vmotion.rstrip()} {voice}" if lang == "en" else _zh_join(vmotion, voice)
    # **负面串恒是最后一句**（与生图侧同口径）。它先拼、人声句后拼的话，
    # `_zh_join` 会用「，」把台词接在负面枚举的尾巴上：
    #   「避免出现：夸张表情，手持长剑，字幕、画面文字、水印，林深说：“…”，口型与台词同步」
    # ——要念的台词与对口型指令一起落进「避免出现」的枚举里，语义整个反过来，
    # 而这一步按秒计费。dubbed 的对口型句同理。
    if neg and include_negative:
        clause = negative_clause(neg, lang)
        vmotion = (". ".join(x for x in [vmotion, clause] if x) if lang == "en"
                   else _zh_join(vmotion, clause))
    return vmotion


class PromptCompiler:
    """把 PromptSpec 与既有成熟策略编译成不可变 PromptEnvelope。"""

    def __init__(self, registry: AgentContractRegistry | None = None):
        self.registry = registry or AgentContractRegistry.load()

    def _spec(self, shot: dict, spec: PromptSpec | dict | None) -> PromptSpec:
        if spec is None:
            return PromptSpec.from_shot(shot, registry=self.registry)
        if isinstance(spec, PromptSpec):
            return spec
        return PromptSpec.parse(spec, registry=self.registry)

    @staticmethod
    def _enforce_limit(prompt: str, max_chars: int, stage: str) -> None:
        """provider 声明的硬上限在封装 Envelope 前检查，绝不静默改写。"""
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 0:
            raise PromptContractError(f"{stage} provider max_prompt_chars 必须是非负整数")
        if max_chars and len(prompt) > max_chars:
            raise PromptContractError(
                f"{stage} 提示词 {len(prompt)} 字符，超过 provider 声明上限 "
                f"{max_chars}；请收敛 PromptSpec，未发送请求")

    def image(self, shot: dict, *, spec: PromptSpec | dict | None = None,
              references=(), skill_revision: str = "", profile_revision: str = "",
              max_chars: int = 0, **options) -> PromptEnvelope:
        normalized = self._spec(shot, spec)
        projected = dict(shot)
        projected.update(normalized.project_fields())
        language = options.get("prompt_lang", "zh")
        positive = image_prompt(projected, include_negative=False, **options)
        prompt = image_prompt(projected, include_negative=True, **options)
        self._enforce_limit(prompt, max_chars, "image")
        negative = "，".join(x for x in ((projected.get("negative_prompt") or "").strip(),
                                         (options.get("character_negative") or "").strip())
                             if x)
        if options.get("text_floor", True):
            negative = with_text_floor(negative, language)
            negative = with_sheet_floor(negative, language)
        contract = self.registry.prompt
        return PromptEnvelope.create(
            contract_version=contract["version"],
            compiler_version=contract["compiler_version"],
            stage="image",
            language=language,
            positive=positive,
            negative=negative,
            prompt=prompt,
            references=references,
            spec_revision=normalized.revision,
            skill_revision=skill_revision,
            profile_revision=profile_revision,
        )

    def video(self, shot: dict, *, native: bool, spec: PromptSpec | dict | None = None,
              references=(), skill_revision: str = "", profile_revision: str = "",
              max_chars: int = 0, **options) -> PromptEnvelope:
        normalized = self._spec(shot, spec)
        projected = dict(shot)
        projected.update(normalized.project_fields())
        language = options.get("lang", "zh")
        positive = video_prompt(
            projected, native=native, include_negative=False, **options)
        prompt = video_prompt(
            projected, native=native, include_negative=True, **options)
        self._enforce_limit(prompt, max_chars, "video")
        negative = with_text_floor(with_performance_floor(
            "，".join(x for x in (
                (projected.get("negative_prompt") or "").strip(),
                (options.get("character_negative") or "").strip(),
            ) if x), performance_hay(projected), language), language)
        if options.get("sketch_board", False):
            negative = with_board_floor(negative, language)
        if native and options.get("native_mute", False) \
                and voicecast.burn_muted(projected):
            negative = with_mute_voice_floor(negative, language)
        contract = self.registry.prompt
        return PromptEnvelope.create(
            contract_version=contract["version"],
            compiler_version=contract["compiler_version"],
            stage="video",
            language=language,
            positive=positive,
            negative=negative,
            prompt=prompt,
            references=references,
            spec_revision=normalized.revision,
            skill_revision=skill_revision,
            profile_revision=profile_revision,
        )
