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

"""大师级运镜 preset 库（3D 导演控制台与 `shots[].camera` 的单一真源）。

每个 preset 同时是**两样东西**，这正是「让成片跟随 previz」的机制核心：

  ① **Three.js 相机装备**——主体相对关键帧（pos/fov/target）+ 缓动 + 跟随模式
     + 可选程序化路径（orbit）/ 滚转 / 手持噪声 / dolly-zoom 主体锁定。
     3D 导演控制台按它飞相机、渲 previz 参考片。
  ② **发给 Seedance 的运镜措辞**（`phrase`/`phrase_en`）——控制台选中 preset 即
     `shots[].camera = preset.phrase`，引擎 `pipeline/prompts.video_prompt()` 把它以
     `运镜：{camera}` 置于**创作正文首位**（前位 token 权重最高）。
     3D 里的运动与文案指令因此天生一致，不存在「3D 推镜、文案写环绕」的分叉。

**措辞不是本模块发明的**：21 条（经典技法 12 + 大师签名 9）**逐字节复用**
`.claude/skills/kinema/references/storyboard.md` 的《进阶运镜预设库》——那里是
Skill 指挥层手写 `camera` 时的取词表，控制台必须与之同源，否则同一个「缓慢环绕」
在手写与 3D 两条路径上会措辞不同、模型表现不同。`tests/test_camera_presets.py`
直接解析该 markdown 表做逐字节比对，漂移即红灯。其余 15 条（11 个基础机位原语
+ 4 个 storyboard 未收录的大师技法）按同一 House 声线自撰。

坐标约定（控制台求值时乘主体世界矩阵）：
  · 右手系 Y-up；主体锚点在原点、脚在 y=0、面向 +Z；看点高度 hL=1.5（胸/面）。
  · 景别距离：CU≈2.0 · MS≈4.0 · WS≈8.0 · 建立镜≈16.0（单位≈米）。
  · 方位角 θ 绕 Y：`pos=(R·sinθ, h, R·cosθ)`，θ=0 正前(+Z)、+90 银幕左(+X)、180 背后。
  · `fov` 是**垂直** FOV 度数，直喂 `PerspectiveCamera.fov`（改后必调
    `updateProjectionMatrix()`）；`roll` 是视轴滚转度（荷兰角，lookAt 之后施加）。
  · `t ∈ [0,1]` 归一时间，墙钟 = t·duration，缓动先重映 t 再采样曲线。

**四戒（storyboard《进阶运镜四戒》的落库形态）**：一个 preset = 一个主运镜；
控制台绝不把两个 rig 叠进同一个 `camera` 串（叠运镜让 Seedance 崩）。`tier`
（●稳定/▲进阶/■高危）驱动前端纪律 UI：▲ 提示 dur≥5s、一集≤4 个；■ 藏在
「情绪峰值镜」显式开关后。
"""
from __future__ import annotations

# ---- 枚举（前端选择器与漂移守卫共用） ----
# 物理机位类型：决定 3D 里怎么装相机，也决定「哪种运动是合法的」。
RIGS: tuple[str, ...] = (
    "locked-off", "dolly", "truck", "pedestal", "pan", "tilt", "roll", "zoom",
    "crane-jib", "orbit-arc", "steadicam-follow", "drone-fpv", "drone-aerial",
    "dolly-zoom", "whip-pan", "crash-zoom", "motion-control", "focus-pull",
)
# 风险档：与 storyboard 的 ●▲■ 一一对应（符号只在 UI 显示，落库用英文枚举）
TIERS: dict[str, str] = {"stable": "●", "advanced": "▲", "high-risk": "■"}
TIER_LABELS: dict[str, str] = {"stable": "稳定", "advanced": "进阶", "high-risk": "高危"}
# 缓动（Three.js 无内置缓动，控制台自带 Penner 助手；名字两端锁步）
EASES: tuple[str, ...] = (
    "linear", "easeInOutSine", "easeInOutCubic", "easeOutCubic",
    "easeInCubic", "easeInExpo", "easeOutExpo", "easeInOutExpo", "easeOutBack",
)
# look 模式：相机每帧看哪里——三者的区别正是 pan/tilt 与 dolly、FPV 与跟拍的分野
#   subject = 每帧重绑活体主体面（跟随）｜keys = 插值 keyed target（摇/甩）
#   path    = 看路径切向（穿越/长镜，前瞻 getPointAt(t+ε)）
LOOKS: tuple[str, ...] = ("subject", "keys", "path")
# 分组（前端三桶：基础机位 / 经典技法 / 大师签名）
GROUPS: dict[str, str] = {"basic": "基础机位", "classic": "经典技法", "master": "大师签名"}


def _k(t, pos, fov, target=(0, 1.5, 0)):
    """关键帧构造器：t 归一时间 · pos 主体相对位置 · fov 垂直度 · target 看点。"""
    return {"t": float(t), "pos": [float(x) for x in pos], "fov": float(fov),
            "target": [float(x) for x in target]}


# ============================================================================
# 36 preset 总表（顺序即前端展示顺序：基础 11 → 经典 12 → 大师 13）
# ============================================================================
CAMERA_PRESETS: dict[str, dict] = {

    # ---------------------------------------------------- A · 基础机位原语（11）
    "static": {
        "label": "固定", "label_en": "locked-off", "group": "basic",
        "rig": "locked-off", "tier": "stable", "look": "keys",
        "duration": 3.0, "ease": "linear",
        "keys": [_k(0, (0, 1.5, 4), 40), _k(1, (0, 1.5, 4), 40)],
        "phrase": "固定机位，镜头完全静止，构图稳定不动，只有画面内的主体与环境在运动",
        "phrase_en": ("locked-off static shot, camera completely still, composition fixed, "
                      "only the subject and environment move within the frame"),
        "desc": "对白正反打、静物、氛围空镜——把注意力全部交给表演本身",
    },
    "push_in": {
        "label": "推近", "label_en": "push-in", "group": "basic",
        "rig": "dolly", "tier": "stable", "look": "subject",
        "duration": 4.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 1.5, 5), 40), _k(1, (0, 1.5, 2.6), 40)],
        "phrase": "镜头平稳缓慢推近主体，景别由中景收拢至近景，主体始终居中，背景逐渐虚化",
        "phrase_en": ("smooth slow dolly push-in toward the subject, framing tightening from "
                      "medium to close, subject centered throughout, background falling away"),
        "desc": "情绪聚拢、揭示细节——最通用的一条「注意这里」",
    },
    "pull_out": {
        "label": "拉远", "label_en": "pull-out", "group": "basic",
        "rig": "dolly", "tier": "stable", "look": "subject",
        "duration": 4.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 1.5, 2.6), 40), _k(1, (0, 1.5, 7), 40)],
        "phrase": "镜头平稳缓慢后拉，景别由近景展开到全景，主体在画面中逐渐变小，环境关系显露",
        "phrase_en": ("smooth slow dolly pull-out, framing opening from close to wide, the "
                      "subject shrinking in frame as the surrounding space is revealed"),
        "desc": "收尾格局镜、孤独感、交代环境——与推近互为一对",
    },
    "pan": {
        "label": "横摇", "label_en": "pan", "group": "basic",
        "rig": "pan", "tier": "stable", "look": "keys",
        "duration": 4.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 1.5, 4), 40, (-2.2, 1.5, 0.3)),
                 _k(1, (0, 1.5, 4), 40, (2.2, 1.5, 0.3))],
        "phrase": "镜头原地水平摇摄，从左侧缓缓摇至右侧，视野横向扫过整个空间，机位不移动",
        "phrase_en": ("camera pans horizontally in place, sweeping slowly from left to right "
                      "across the space, the camera body itself never moving"),
        "desc": "空镜氛围、扫视全场、由 A 引到 B——机位不动只转头",
    },
    "tilt": {
        "label": "俯仰摇", "label_en": "tilt", "group": "basic",
        "rig": "tilt", "tier": "stable", "look": "keys",
        "duration": 4.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 1.5, 4), 40, (0, 0.2, 0)),
                 _k(1, (0, 1.5, 4), 40, (0, 2.6, 0))],
        "phrase": "镜头原地垂直摇摄，视线自下而上缓缓抬起，从脚下细节升到人物面部与上方空间",
        "phrase_en": ("camera tilts vertically in place, the view lifting slowly from the "
                      "ground detail up to the face and the space above"),
        "desc": "自下而上的登场、量体高度、揭示上方——纵向的 pan",
    },
    "truck": {
        "label": "横移", "label_en": "truck", "group": "basic",
        "rig": "truck", "tier": "stable", "look": "keys",
        "duration": 5.0, "ease": "easeInOutSine",
        "keys": [_k(0, (-2.5, 1.5, 4), 40, (-2.5, 1.5, 0)),
                 _k(1, (2.5, 1.5, 4), 40, (2.5, 1.5, 0))],
        "phrase": "镜头整体侧向平移，与被摄面保持平行等距，近景物件横向掠过形成明显视差",
        "phrase_en": ("camera trucks laterally, holding a parallel constant distance to the "
                      "subject plane, foreground objects sliding past with strong parallax"),
        "desc": "巡视排列、走廊队列、货架陈列——机位真的在横着走",
    },
    "pedestal": {
        "label": "升降", "label_en": "pedestal", "group": "basic",
        "rig": "pedestal", "tier": "stable", "look": "keys",
        "duration": 4.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 0.8, 4), 40, (0, 0.8, 0)),
                 _k(1, (0, 2.4, 4), 40, (0, 2.4, 0))],
        "phrase": "镜头整体垂直升起，视线始终保持水平不俯不仰，画面随高度平移而层层换景",
        "phrase_en": ("camera pedestals straight up while the lens stays perfectly level, "
                      "the frame sliding upward through layers of the scene"),
        "desc": "由物到人、由人到天际线——升的是机身，视线不歪",
    },
    "crane_jib": {
        "label": "升镜", "label_en": "crane / jib", "group": "basic",
        "rig": "crane-jib", "tier": "stable", "look": "keys",
        "duration": 6.0, "ease": "easeInOutCubic",
        "keys": [_k(0, (0, 0.8, 4), 46, (0, 1.2, 0)),
                 _k(1, (0, 3.2, 5.5), 46, (0, 1.2, 0))],
        "phrase": "镜头由摇臂托举缓缓升高并后撤，俯角逐渐加大，主体退入更大的空间关系中",
        "phrase_en": ("jib arm lifts the camera slowly up and back, the downward angle "
                      "deepening as the subject settles into a wider spatial context"),
        "desc": "段落收尾、离场、把人放回世界里——升镜的通用款",
    },
    "arc": {
        "label": "小环绕", "label_en": "arc", "group": "basic",
        "rig": "orbit-arc", "tier": "stable", "look": "subject",
        "duration": 5.0, "ease": "easeInOutSine",
        "path": {"type": "orbit", "radius": 4.0, "height": 1.5,
                 "az_start": -30.0, "az_end": 30.0},
        "keys": [_k(0, (-2.0, 1.5, 3.46), 36), _k(1, (2.0, 1.5, 3.46), 36)],
        "phrase": "镜头沿小角度弧线绕主体侧移，主体保持居中，背景发生轻微的角度位移",
        "phrase_en": ("camera arcs a shallow angle around the subject, keeping it centered "
                      "while the background shifts slightly in angle"),
        "desc": "给静止对话一点呼吸——环绕里最安全的一档",
    },
    "zoom": {
        "label": "变焦", "label_en": "zoom", "group": "basic",
        "rig": "zoom", "tier": "stable", "look": "subject",
        "duration": 4.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 1.5, 4), 55), _k(1, (0, 1.5, 4), 24)],
        "phrase": "机位固定不动，仅镜头焦距由广角变长焦，视野收窄、背景被压缩放大，透视关系不变",
        "phrase_en": ("camera body stays put while the lens zooms from wide to long, the view "
                      "narrowing and the background compressing, perspective unchanged"),
        "desc": "监控感、窥视感——与推近的差别正是「透视不变」",
    },
    "dutch": {
        "label": "荷兰角", "label_en": "dutch angle", "group": "basic",
        "rig": "roll", "tier": "advanced", "look": "subject",
        # ▲ 档默认 5s（storyboard 四戒：进阶运镜须「缓慢/平稳」且 dur≥5s）——
        # 荷兰角要慢到观众先信了构图、再察觉地平线歪了，快滚只会变成 MV 特效
        "duration": 5.0, "ease": "easeInOutSine", "roll": 14.0,
        "keys": [_k(0, (0, 1.5, 3.4), 40), _k(1, (0, 1.5, 3.4), 40)],
        "phrase": "镜头缓慢向一侧倾斜滚转，地平线随之倾斜，画面失衡带来不安与失序感",
        "phrase_en": ("the camera slowly rolls to one side, the horizon canting with it, "
                      "the frame tipping into unease and disorder"),
        "desc": "失控/异变/精神压迫——倾斜幅度务必克制，过了就成 MV",
    },

    # ---------------------------------------------------- B · 经典技法（12·复用）
    "dolly_zoom": {
        "label": "希区柯克变焦", "label_en": "dolly zoom", "group": "classic",
        "rig": "dolly-zoom", "tier": "advanced", "look": "subject",
        "duration": 6.0, "ease": "easeInOutSine", "lock_subject_scale": True,
        "keys": [_k(0, (0, 1.5, 3.0), 34), _k(1, (0, 1.5, 6.0), 18)],
        "phrase": ("希区柯克变焦：镜头缓慢后退并同步放大焦距，背景空间被压缩拉伸产生眩晕感，"
                   "主体在画面中大小不变、构图居中锁定"),
        "phrase_en": ("dolly zoom (Hitchcock zoom): camera slowly pulls back while zooming in, "
                      "background space compresses and stretches with a vertigo feel, subject "
                      "size locked and centered"),
        "desc": "真相揭晓/世界观崩塌/恐惧顿悟的情绪反转镜——全集只给最重的那一拍",
    },
    "rack_focus": {
        "label": "焦点转移", "label_en": "rack focus", "group": "classic",
        "rig": "focus-pull", "tier": "stable", "look": "keys",
        "duration": 3.0, "ease": "easeInOutSine",
        "focus": {"near": 1.2, "far": 6.0},
        "keys": [_k(0, (0, 1.5, 3.2), 34), _k(1, (0, 1.5, 3.2), 34)],
        "phrase": "焦点从前景的X缓缓转移到背景的Y，浅景深，焦外光斑柔化，转移平滑无呼吸",
        "phrase_en": ("rack focus shifting smoothly from foreground X to background Y, "
                      "shallow depth of field, soft bokeh, no focus breathing"),
        "desc": "双主体关系镜/信息揭示——对白剧最优雅的运镜，正反打之外的第三选择",
    },
    "crane_reveal": {
        "label": "升镜揭示", "label_en": "crane reveal", "group": "classic",
        "rig": "crane-jib", "tier": "stable", "look": "keys",
        "duration": 6.0, "ease": "easeInOutCubic",
        "keys": [_k(0, (0, 0.5, 5), 50, (0, 0.6, 0)),
                 _k(1, (0, 3.5, 7), 46, (0, 1.5, -6))],
        "phrase": "镜头从低处缓缓升起越过前景的X，逐层揭示远处的Y全景，前中远三层景深依次展开",
        "phrase_en": ("slow crane up from low over foreground X, revealing Y in the distance "
                      "layer by layer, fore-mid-background unfolding in depth"),
        "desc": "开场建立镜/收尾格局镜——「原来世界这么大」的一拍",
    },
    "slow_orbit": {
        "label": "缓慢环绕", "label_en": "slow orbit", "group": "classic",
        "rig": "orbit-arc", "tier": "stable", "look": "subject",
        "duration": 6.0, "ease": "easeInOutSine",
        "path": {"type": "orbit", "radius": 3.5, "height": 1.5,
                 "az_start": -45.0, "az_end": 45.0},
        "keys": [_k(0, (-2.47, 1.5, 2.47), 36), _k(1, (2.47, 1.5, 2.47), 36)],
        "phrase": "镜头绕主体缓慢环绕四分之一圈，主体始终居中，背景视差流动，光影随角度渐变",
        "phrase_en": ("slow 90-degree orbit around the subject, subject centered throughout, "
                      "background parallax flowing, light shifting with the angle"),
        "desc": "主角高光/器物展示——超过 90° 崩率陡增，限小角度",
    },
    "tracking": {
        "label": "侧向跟拍", "label_en": "tracking", "group": "classic",
        "rig": "steadicam-follow", "tier": "stable", "look": "subject",
        "duration": 6.0, "ease": "linear", "follow_offset": [0, 1.5, 4],
        "keys": [_k(0, (0, 1.5, 4), 40), _k(1, (0, 1.5, 4), 40)],
        "phrase": "镜头在侧面与主体等速平稳跟随，主体保持三分线位置，背景带速度感流动虚化",
        "phrase_en": ("smooth lateral tracking at the subject's pace, subject held on the "
                      "third line, background streaming past with motion blur"),
        "desc": "行走对话/追逐前奏/巡视——主体走多远镜头就跟多远",
    },
    "fpv": {
        "label": "FPV 穿越", "label_en": "FPV flythrough", "group": "classic",
        "rig": "drone-fpv", "tier": "advanced", "look": "path",
        "duration": 6.0, "ease": "linear", "bank": 15.0,
        "keys": [_k(0, (0, 2.0, 10), 75), _k(0.5, (0, 1.2, 4), 75),
                 _k(1, (0, 0.8, -2), 75)],
        "phrase": "第一人称穿越视角，镜头贴着路径连续飞行穿过X，高度与倾斜随地形起伏，速度感渐强",
        "phrase_en": ("FPV drone shot skimming along the path through X, altitude and bank "
                      "following the terrain, speed building steadily"),
        "desc": "空间导览/追逐/坠落——空镜专用，带主角面部易崩",
    },
    "oner": {
        "label": "一镜到底", "label_en": "oner", "group": "classic",
        "rig": "steadicam-follow", "tier": "advanced", "look": "path",
        "duration": 10.0, "ease": "linear",
        "noise": {"pos": 0.02, "rot": 0.25, "freq": 0.6},
        "keys": [_k(0, (0, 1.5, 6), 42), _k(0.5, (1.5, 1.5, 3), 42),
                 _k(1, (2.5, 1.6, -1), 42)],
        "phrase": "一镜到底，镜头连续移动无跳切，先缓缓经过X，再转向抵达Y，节奏先缓后扬",
        "phrase_en": ("continuous one-take, no cuts, camera drifts past X then turns and "
                      "arrives at Y, pacing slow then swelling"),
        "desc": "≥10s 长镜叙事——native 模式 + 分时段描述才稳",
    },
    "robotic_arm": {
        "label": "机械臂扫摆", "label_en": "robotic arm", "group": "classic",
        "rig": "motion-control", "tier": "advanced", "look": "subject",
        "duration": 6.0, "ease": "easeInOutCubic",
        "path": {"type": "orbit", "radius": 3.0, "height": 1.2,
                 "az_start": -40.0, "az_end": 40.0, "height_end": 2.0},
        "keys": [_k(0, (-1.93, 1.2, 2.30), 34), _k(1, (1.93, 2.0, 2.30), 34)],
        "phrase": "镜头如机械臂般从X角度平滑弧线摆至Y角度并持续跟随主体，精准稳定无抖动",
        "phrase_en": ("robotic-arm style camera sweeping in a smooth precise arc from X angle "
                      "to Y angle while tracking, zero jitter"),
        "desc": "产品/法宝/机甲展示、战斗环视——「零抖动」是它的签名",
    },
    "handheld": {
        "label": "手持纪实", "label_en": "handheld", "group": "classic",
        "rig": "steadicam-follow", "tier": "advanced", "look": "subject",
        "duration": 5.0, "ease": "linear",
        "noise": {"pos": 0.03, "rot": 0.4, "freq": 1.2},
        "keys": [_k(0, (0, 1.5, 3.5), 40), _k(1, (0, 1.5, 3.5), 40)],
        "phrase": "轻微手持晃动感，呼吸般的浮动幅度，纪实临场感，晃动始终克制",
        "phrase_en": ("subtle handheld sway with a breathing-like float, documentary "
                      "immediacy, shake kept minimal"),
        "desc": "冲突/逃亡/伪纪录——幅度必须写「轻微」，否则糊",
    },
    "whip_pan": {
        "label": "甩镜", "label_en": "whip pan", "group": "classic",
        "rig": "whip-pan", "tier": "high-risk", "look": "keys",
        "duration": 1.5, "ease": "easeInOutExpo",
        "keys": [_k(0, (0, 1.5, 3.6), 40, (-3, 1.5, 0)),
                 _k(1, (0, 1.5, 3.6), 40, (3, 1.5, 0))],
        "phrase": "快速甩镜转向X，强烈方向性运动模糊，落点稳定收住新构图",
        "phrase_en": ("whip pan to X with strong directional motion blur, landing locked on "
                      "a stable new composition"),
        "desc": "双场景硬切/喜剧节拍——落点稳是成败关键",
    },
    "crash_zoom": {
        "label": "急推", "label_en": "crash zoom", "group": "classic",
        "rig": "crash-zoom", "tier": "high-risk", "look": "subject",
        "duration": 1.2, "ease": "easeInCubic", "overshoot": 14.0,
        "keys": [_k(0, (0, 1.5, 3.0), 50), _k(1, (0, 1.5, 2.4), 16)],
        "phrase": "急速推近至面部大特写，末段急停带轻微过冲回弹",
        "phrase_en": ("crash zoom into extreme close-up, hard stop with a slight overshoot "
                      "settle"),
        "desc": "震惊反应/喜剧夸张——一集至多一次",
    },
    "bullet_time": {
        "label": "子弹时间", "label_en": "bullet time", "group": "classic",
        "rig": "orbit-arc", "tier": "high-risk", "look": "subject",
        "duration": 4.0, "ease": "linear", "freeze_subject": True,
        "path": {"type": "orbit", "radius": 3.0, "height": 1.4,
                 "az_start": -60.0, "az_end": 60.0},
        "keys": [_k(0, (-2.60, 1.4, 1.5), 34), _k(1, (2.60, 1.4, 1.5), 34)],
        "phrase": ("时间近乎凝固，尘埃与碎片悬停空中，镜头绕定格的主体匀速弧线移动，"
                   "光线扫过轮廓"),
        "phrase_en": ("bullet-time: time nearly frozen, dust and debris suspended, camera "
                      "arcing evenly around the frozen subject, light sweeping the silhouette"),
        "desc": "动作最高潮的唯一一镜（native 模式）",
    },

    # ---------------------------------------------------- C · 大师签名 & 特种（13）
    "bay_orbit": {
        "label": "迈克尔·贝英雄环绕", "label_en": "Bay hero orbit", "group": "master",
        "rig": "orbit-arc", "tier": "advanced", "look": "subject",
        "duration": 7.0, "ease": "easeInOutSine",
        "path": {"type": "orbit", "radius": 3.2, "height": 0.6,
                 "az_start": -90.0, "az_end": 90.0},
        "keys": [_k(0, (-3.2, 0.6, 0), 24, (0, 1.4, 0)),
                 _k(1, (3.2, 0.6, 0), 24, (0, 1.4, 0))],
        "phrase": ("低角度仰拍缓慢环绕主体半圈，主体缓缓起身或伫立不动，背景旋转流动，"
                   "逆光镜头光晕，慢动作史诗感"),
        "phrase_en": ("low-angle slow half-orbit around the rising hero, background rotating "
                      "past, backlit lens flare, slow-motion epic gravitas"),
        "desc": "主角登场/封神宣言/集结亮相——影史最著名的高光镜（半圈内，全圈崩率高）",
    },
    "spielberg_push": {
        "label": "斯皮尔伯格惊愕推近", "label_en": "Spielberg push-in", "group": "master",
        "rig": "dolly", "tier": "stable", "look": "subject",
        "duration": 5.0, "ease": "easeInOutSine",
        "keys": [_k(0, (0, 1.55, 3.0), 40, (0, 1.55, 0)),
                 _k(1, (0, 1.55, 1.6), 34, (0, 1.55, 0))],
        "phrase": "镜头缓缓推近至面部特写，人物望向镜外逐渐睁大双眼、嘴唇微张，背景缓慢虚化",
        "phrase_en": ("slow push-in to a close-up as the character gazes past camera, eyes "
                      "widening in awe, lips parting, background melting into blur"),
        "desc": "目击奇观/顿悟瞬间的反应镜（Spielberg Face）",
    },
    "kubrick_push": {
        "label": "库布里克对称推进", "label_en": "Kubrick push", "group": "master",
        "rig": "dolly", "tier": "stable", "look": "keys",
        "duration": 7.0, "ease": "linear",
        "keys": [_k(0, (0, 1.5, 10), 40, (0, 1.5, -20)),
                 _k(1, (0, 1.5, 3), 40, (0, 1.5, -20))],
        "phrase": "沿走廊中轴线单点透视对称构图，匀速缓慢推进，冷峻压迫感",
        "phrase_en": ("one-point perspective push down the exact center axis, rigorously "
                      "symmetrical, steady pace, cold and foreboding"),
        "desc": "走廊/隧道/仪式空间——秩序感与不安并存",
    },
    "spike_lee": {
        "label": "斯派克·李滑行", "label_en": "double dolly", "group": "master",
        "rig": "motion-control", "tier": "advanced", "look": "subject",
        "duration": 5.0, "ease": "easeInOutSine", "double_dolly": True,
        "keys": [_k(0, (0, 1.5, 4), 40), _k(1, (0, 1.5, 6), 40)],
        "phrase": "主体如站在移动平台上朝镜头滑行，身体静止而背景后退，梦游般的悬浮感",
        "phrase_en": ("double-dolly glide: subject drifts toward camera as if on a platform, "
                      "body still while the world slides back, dreamlike floating"),
        "desc": "恍惚/下定决心/被命运推着走的时刻——主体与镜头同向等量移动",
    },
    "lubezki_float": {
        "label": "卢贝兹基漂浮", "label_en": "Lubezki float", "group": "master",
        "rig": "steadicam-follow", "tier": "advanced", "look": "subject",
        "duration": 8.0, "ease": "easeInOutSine",
        "noise": {"pos": 0.02, "rot": 0.3, "freq": 0.6}, "bob": 0.1,
        "path": {"type": "orbit", "radius": 1.8, "height": 1.5,
                 "az_start": -20.0, "az_end": 25.0},
        # keys 的 y 恒等于 path.height——上下浮动交给 `bob`（求值时叠加），
        # 写死进关键帧会与 path 打架：支持 path 的求值器根本读不到这 0.1 的差
        "keys": [_k(0, (-0.62, 1.5, 1.69), 38), _k(1, (0.76, 1.5, 1.63), 38)],
        "phrase": "镜头如无重力般贴近主体缓缓漂浮环行，自然光，长镜呼吸感",
        "phrase_en": ("weightless floating camera drifting close around the subject, natural "
                      "light, breathing long-take feel"),
        "desc": "沉浸式情绪戏/自然环境戏（荒野猎人式）",
    },
    "ozu": {
        "label": "小津低机位", "label_en": "Ozu tatami", "group": "master",
        "rig": "locked-off", "tier": "stable", "look": "keys",
        "duration": 4.0, "ease": "linear",
        "keys": [_k(0, (0, 0.7, 3.2), 40, (0, 1.1, 0)),
                 _k(1, (0, 0.7, 3.2), 40, (0, 1.1, 0))],
        "phrase": "低机位榻榻米视角固定镜头，轻微仰角平视人物，构图安定对称",
        "phrase_en": ("static tatami-level shot, slight low angle at seated eye line, serene "
                      "symmetrical composition"),
        "desc": "对坐交谈/家庭戏——静水流深的对白镜",
    },
    "side_scroll": {
        "label": "老男孩横移", "label_en": "side-scroll", "group": "master",
        "rig": "truck", "tier": "stable", "look": "subject",
        "duration": 6.0, "ease": "linear", "follow_offset": [0, 1.5, 6],
        "keys": [_k(0, (0, 1.5, 6), 24), _k(1, (0, 1.5, 6), 24)],
        "phrase": "镜头水平横移平行跟随动作，画面如横版卷轴展开，景深压平",
        "phrase_en": ("flat side-scrolling tracking shot parallel to the action, staged like "
                      "a 2D scroll, compressed depth"),
        "desc": "走廊群战/行进队列——像素与游戏画风天配",
    },
    "wes_whip": {
        "label": "韦斯·安德森甩摇", "label_en": "Wes 90° whip", "group": "master",
        "rig": "whip-pan", "tier": "high-risk", "look": "keys",
        "duration": 1.2, "ease": "easeInOutExpo",
        "keys": [_k(0, (0, 1.5, 4), 40, (0, 1.5, 0)),
                 _k(1, (0, 1.5, 4), 40, (4, 1.5, 0))],
        "phrase": "对称构图中快速90度甩摇到下一主体，落点精准形成新的居中对称构图",
        "phrase_en": ("snap 90-degree whip pan within symmetrical staging, landing precisely "
                      "on the next centered composition"),
        "desc": "喜剧节拍/图鉴式逐个展示——planimetric 正对构图才成立",
    },
    "foreground_wipe": {
        "label": "前景擦镜", "label_en": "foreground wipe", "group": "master",
        "rig": "motion-control", "tier": "advanced", "look": "keys",
        # ▲ 档 ≥5s：擦镜是「两段一镜」——遮蔽前后各要有足够时间读清构图，
        # 3s 只会让观众看见一次闪动而读不出机位已换（同 dutch 的 ▲ 纪律）
        "duration": 5.0, "ease": "easeInOutSine", "wipe": True,
        "keys": [_k(0, (-1.2, 1.5, 3.2), 40, (0, 1.5, 0)),
                 _k(0.5, (0, 1.5, 2.6), 40, (0, 1.5, 0)),
                 _k(1, (1.4, 1.5, 3.2), 40, (0, 1.5, 0))],
        "phrase": "前景物体（人影/立柱/车流）掠过并短暂遮蔽镜头，擦过瞬间机位与景别已无痕切换",
        "phrase_en": ("a foreground object sweeps across and briefly blocks the lens, "
                      "revealing a new angle as it clears"),
        "desc": "长镜内无痕转场——AI 视频独有的优势技法",
    },
    # —— storyboard 未收录的大师技法（House 声线自撰）——
    "fincher_dolly": {
        "label": "芬奇精准推", "label_en": "Fincher slow dolly", "group": "master",
        "rig": "motion-control", "tier": "stable", "look": "subject",
        "duration": 8.0, "ease": "linear",
        "keys": [_k(0, (0, 1.5, 3.4), 36), _k(1, (0, 1.5, 2.9), 36)],
        "phrase": ("芬奇式精准推镜：机械臂控制的极缓慢推进，运动几乎不可察觉，构图冷峻锁定，"
                   "主体大小恒定，全程零抖动"),
        "phrase_en": ("Fincher precise dolly: an imperceptibly slow motion-control push-in, "
                      "cold locked framing, subject scale constant, zero jitter"),
        "desc": "审讯/密谈/压抑的长对白——观众察觉不到镜头在动，只觉得越来越紧",
    },
    "steadicam_follow": {
        "label": "斯坦尼康跟随", "label_en": "Steadicam long-take follow", "group": "master",
        "rig": "steadicam-follow", "tier": "stable", "look": "path",
        "duration": 10.0, "ease": "linear", "follow_offset": [0, 1.5, -3.2],
        "noise": {"pos": 0.02, "rot": 0.2, "freq": 0.5},
        "keys": [_k(0, (0, 1.5, -3.2), 42), _k(1, (0, 1.5, -3.2), 42)],
        "phrase": ("斯坦尼康长镜跟随：镜头如浮空般在主体身后平稳跟随，穿行连续空间无跳切，"
                   "带轻微人体呼吸浮动"),
        "phrase_en": ("Steadicam long-take follow: floating camera trailing the subject "
                      "through continuous space, no cuts, subtle human breathing float"),
        "desc": "带观众走进一个空间——身后尾随视角，人物领路",
    },
    "drone_establish": {
        "label": "航拍揭示", "label_en": "aerial establishing", "group": "master",
        "rig": "drone-aerial", "tier": "advanced", "look": "keys",
        "duration": 7.0, "ease": "easeInOutCubic",
        "keys": [_k(0, (0, 2, 6), 55, (0, 1.4, 0)),
                 _k(1, (0, 14, 22), 60, (0, 1, 0))],
        "phrase": ("航拍升镜揭示：镜头自低处一边升高一边后拉，从主体特写逐层展开到全景地貌，"
                   "建立空间尺度"),
        "phrase_en": ("aerial establishing crane-out: camera rises and pulls back, unfolding "
                      "from the subject to the full landscape, establishing scale"),
        "desc": "开篇建立/章末收束——一镜说清「这是哪里、有多大」",
    },
    "god_eye": {
        "label": "上帝俯视", "label_en": "god's-eye overhead", "group": "master",
        "rig": "crane-jib", "tier": "stable", "look": "keys",
        "duration": 4.0, "ease": "linear",
        "keys": [_k(0, (0, 8, 0.001), 40, (0, 0, 0)),
                 _k(1, (0, 5, 0.001), 40, (0, 0, 0))],
        "phrase": "上帝视角俯拍：镜头正上方垂直向下俯视，平面式对称构图，物件如陈列般铺开",
        "phrase_en": ("god's-eye overhead: camera directly above looking straight down, flat "
                      "top-down symmetrical composition"),
        "desc": "陈列/棋局/命运感——把人变成图案的那个角度",
    },
}


# ============================================================================
# 目录下发与取用（镜像 transitions.catalog() / effects.catalog()）
# ============================================================================
_CATALOG_KEYS = ("key", "label", "label_en", "group", "group_label", "rig", "tier",
                 "tier_mark", "tier_label", "tracks_subject", "look", "duration",
                 "ease", "keys", "path", "roll", "noise", "lock_subject_scale",
                 "focus", "follow_offset", "phrase", "phrase_en", "desc")


def catalog() -> list[dict]:
    """全量运镜目录（按 CAMERA_PRESETS 注册顺序）——`/api/overview` 下发 `camera_catalog`。

    前端**零硬编码**：运镜选择器的分组/风险 chip/参数 chip/3D 求值全部读这份目录。
    与 `transitions.catalog()`/`effects.catalog()` 同哲学：目录是可发现性的单一真源。
    """
    out = []
    for key, p in CAMERA_PRESETS.items():
        tier = p["tier"]
        out.append({
            "key": key,
            "label": p["label"], "label_en": p.get("label_en") or key,
            "group": p["group"], "group_label": GROUPS[p["group"]],
            "rig": p["rig"],
            "tier": tier, "tier_mark": TIERS[tier], "tier_label": TIER_LABELS[tier],
            # tracks_subject 是 look 的派生量（保留字段名以对齐 03 文档的目录契约）
            "tracks_subject": p.get("look") == "subject",
            "look": p.get("look", "keys"),
            "duration": float(p["duration"]), "ease": p["ease"],
            "keys": [dict(k) for k in p["keys"]],
            "path": dict(p["path"]) if p.get("path") else None,
            "roll": float(p.get("roll") or 0.0),
            "noise": dict(p["noise"]) if p.get("noise") else None,
            "lock_subject_scale": bool(p.get("lock_subject_scale")),
            "focus": dict(p["focus"]) if p.get("focus") else None,
            "follow_offset": list(p["follow_offset"]) if p.get("follow_offset") else None,
            "phrase": p["phrase"], "phrase_en": p["phrase_en"],
            "desc": p.get("desc", ""),
        })
    return out


def get(key: str) -> dict | None:
    """按 key 取 preset（未知 key 返回 None——调用方决定是报错还是忽略）。"""
    p = CAMERA_PRESETS.get(key)
    return dict(p, key=key) if p else None


def phrase_of(key: str, lang: str = "zh") -> str:
    """取该 preset 的 Seedance 运镜措辞（写进 `shots[].camera` 的那一句）。

    未知 key 返回空串——控制台/CLI 据此决定「不写 camera」而不是写一句错的。
    """
    p = CAMERA_PRESETS.get(key)
    if not p:
        return ""
    return p["phrase_en"] if lang == "en" else p["phrase"]


def keys_by_tier(tier: str) -> list[str]:
    """某风险档下的全部 preset key（纪律 UI 与守卫用）。"""
    return [k for k, p in CAMERA_PRESETS.items() if p["tier"] == tier]
