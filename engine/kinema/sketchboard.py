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

"""简笔分镜预演板（sketch board）——previz 之外的第二条运动预演路径。

一镜一板：把一个分镜按拍数拆成铅笔素描面板（网格按拍数恰好填满：4 拍 2×2、
6 拍 2×3、9 拍 3×3，上限 `PANEL_MAX` 格；按时间推进，素描灰阶为绝对主体、
仅发光体/能量可少量点缀色），用五色标注系统画出运动设计——
红=人物运动轨迹、蓝=摄影机运动、绿=取景构图、橙=灯光方向、紫=声音情感、
黑=面板编号与秒段标签。它对 Seedance 的价值是两笔：
① 板图作 `role=reference_image` 附进请求——dubbed 参考媒体模式与 native 缺省档
   （全能参考·一镜一片）板在盘即附；只有**衔接参与镜**（章级/镜级 frame_chain，
   首帧任务禁混参考图）附不了，须逐镜 `reference_opt_in` 才强制切回参考任务；
② beats 编译成**分段时间轴提示词**（timeline prompting，厂商最佳实践：
"0-2s 做什么、2-4s 做什么"的分段结构远胜一大段散文描述）。

## 三条铁律

1. **与 previz 并行互斥**（`active_guide` 是唯一仲裁真源）：一个镜要么走 3D 预演
   （首帧/末帧/V2V 参考视频），要么走简笔板（参考图+时间轴），绝不同时——两者都在
   向模型描述"这几秒怎么运动"，同发必然互相打架。显式 `shots[].guide` 恒赢；
   缺省自动仲裁时 **previz 在场则 previz 赢**（它的末帧/参考视频是更强的像素级锚）。
2. **beats 是指挥层写的，引擎绝不编**（引擎内无 LLM）：`shots[].sketch.beats` 由
   skill 按分镜脚本拆 9 拍；引擎只做确定性拼装（板提示词/时间轴文本）与生成调度。
   缺 beats 的镜 `sketch gen` 跳过并打印可执行的补写指令。
3. **板是预演观看物，绝不进 `image`/`clip`**：那两个槽是分镜图与成片素材位，
   素描板混进去会被 compose 当画面渲进成片（与 previz "绝不写 clip" 同款边界）。
   成片侧的防泄漏在**板真随请求附上时**两处同时说：`board_role_clause` 的职责
   声明由 prompts 拼在提示词头部，`prompts.BOARD_FLOOR_*` 把同批词压进负面串。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .errors import ProjectError

# 板产物目录（<章节>_work/sketch/）与命名；16:9 是板的固定比例——它是阅读物，
# 阅读性优先，与项目主比例无关（竖屏项目的板也横着排 3×3 才摆得下 9 格）。
SKETCH_SUBDIR = "sketch"
BOARD_ASPECT = "16:9"

# 9 格是名义标准版式（3×3）；**实际格数恒随拍数派生**（`_grid_line`），
# 引擎容忍 4~12（短镜少格、长镜多格）。PANEL_DEFAULT 只用于"无时长信息"的秒段均分。
PANEL_DEFAULT = 9
PANEL_MIN, PANEL_MAX = 4, 12

# 自动拆拍的密度目标（秒/拍）：**引擎自己切的拍按它收敛**——一个可辨识动作的舒适
# 量级。与 `MIN_BEAT_SEC`（0.8，告警硬底线）分工明确：
#   · TARGET_BEAT_SEC 管「引擎代切时切多细」——auto_beats 的并拍上限，属确定性行为；
#   · MIN_BEAT_SEC  管「人手写的拍要不要报警」——authored beats 一个字都不动，
#     哪几拍该合是导演决定（同 beats_coverage「只报不改写」纪律）。
# 立论：Seedance 收到超密时间轴时会自行丢拍、只演主事件（见 `MIN_BEAT_SEC`）。
# 句读切出多少拍与镜头有多长本是两件事，按句读定拍数等于让标点决定执行密度。
TARGET_BEAT_SEC = 1.2
AUTO_BEATS_MIN = 2      # 再短的镜也至少「起 + 收」两拍（低于此就不成其为时序脚本）

GUIDES = ("previz", "control", "sketch")


def reference_shot(shot: dict, native: bool) -> bool:
    """本镜是否**显式表态**走「参考生视频」（`shots[].sketch.reference: true`，
    CLI `sketch ref`）——这是**衔接章里的孤岛判据**，不是全能参考的入口。

    全能参考已是 native 的**缺省档**（`cli._shot_plan`：凡不参与首尾帧衔接的镜
    都走参考任务，板在盘即附、无需任何表态）。本函数只服务显式衔接的章
    （章级 `frame_chain: true` / 镜级 `shots[].frame_chain`）：那里的镜缺省走
    首帧任务，此表态把某一镜强制拉回参考任务——代价是这一镜没有首/末帧槽
    （首帧任务与参考媒体官方互斥，实测 400）：不向下一镜发末帧，上一镜也焊不到
    它身上（分镜图降级成众多 reference_image 之一），两侧接缝由
    `pipeline.framechain.sync_seams` 自动补 0.1s 无缝转场（链上孤岛）。

    静态判据（显式开启 × guide=sketch × 板真在盘 × native）——provider 能力位
    （supports_reference_images）由 cli 在此之上再相与；`framechain.island`
    消费同一函数，链图与引擎同口径。dubbed 不经此路（参考媒体模式本就能附板）。"""
    if not native or active_guide(shot) != "sketch":
        return False
    if not reference_opt_in(shot):
        return False
    p = board_of(shot)
    return bool(p and Path(p).is_file())


def reference_opt_in(shot: dict) -> bool:
    """本镜是否显式选择了「板作参考」（`shots[].sketch.reference`）。缺省 False。"""
    return bool(((shot or {}).get("sketch") or {}).get("reference"))


def set_reference(shot: dict, on: bool) -> None:
    """逐镜开关「板作参考」。关闭走纯减法：不留 `false`、不造空 `sketch` 壳——
    文档里多出来的键会让读 JSON 的人猜「显式关过」还是「从没开过」。"""
    if on:
        shot.setdefault("sketch", {})["reference"] = True
        return
    sk = shot.get("sketch")
    if isinstance(sk, dict):
        sk.pop("reference", None)
        if not sk:
            shot.pop("sketch", None)

# 板生成的版式/笔触样板图（引擎内置资产，随仓库分发）：每次生成自动排在参考图
# 最前——"100% 复刻版式"靠垫图，不靠每次赌提示词。三张是同版式的不同示例
# （单人动作 / 双人对话 / 双人战斗，覆盖板要承载的三类调度题材）：多张并排后
# 共同点只剩版面骨架与标注画法，模型才分得出版式与内容
# （与设定图侧 `sheets._TEMPLATES` 同一套多示例教学法）。缺文件时静默降级——
# 提示词已完整描述版式，样板只压方差。
TEMPLATE_DIR = Path(__file__).resolve().parent / "assets" / "blueprints"
TEMPLATE_PATHS = (TEMPLATE_DIR / "sketch1_template.png",
                  TEMPLATE_DIR / "sketch2_template.png",
                  TEMPLATE_DIR / "sketch3_template.png")


def templates() -> list[Path]:
    """在盘的板样板（缺文件静默降级为空列表）。"""
    return [p for p in TEMPLATE_PATHS if p.is_file()]


# ------------------------------------------------------------ beats 读取与校验
def beats_of(shot: dict) -> list[dict]:
    """该镜已登记的 beats（净化副本）：只认 dict 且 `action` 非空的条目。

    beats 是指挥层写在 `shots[].sketch.beats` 的授权字段，长任务/手改都可能
    留下坏形态（字符串混进列表、空对象），这里按"能用的留、不能用的丢"净化，
    绝不抛异常——板生成的失败要落在"哪一镜缺 beats"这种能行动的粒度上。"""
    sk = shot.get("sketch")
    if not isinstance(sk, dict):
        return []
    out = []
    for b in sk.get("beats") or []:
        if isinstance(b, dict) and str(b.get("action") or "").strip():
            out.append(b)
    return out


def beat_slots(dur: float, n: int = PANEL_DEFAULT) -> list[str]:
    """把镜头时长均分成 n 个秒段标签（`0-0.6s` 式，一位小数）。

    beats 条目缺 `t` 时的确定性补位——秒段必须存在（timeline prompting 的
    立身之本就是"第几秒干嘛"），但不该逼指挥层手算 5.0/9 的除法。"""
    try:
        total = float(dur or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total <= 0:
        total = float(n)                 # 无时长镜按 1s/格 给出名义秒段
    step = total / n
    out = []
    for i in range(n):
        a, b = i * step, (i + 1) * step
        out.append(f"{a:.1f}-{b:.1f}s")
    return out


def beat_times(shot: dict, beats: list[dict] | None = None,
               total: float | None = None) -> list[str]:
    """逐拍秒段标签：authored `t` 优先，缺的按 `beat_slots` 均分补位。

    `total` = 消费侧的**实际请求秒数**（`voicecast.request_seconds` 单一真源，
    由 cli 算好传入）。**绝不能缺省裸用 `dur`**：kenburns 下 dur 折着
    `delivery.pause_*`、dubbed 下真相是配音实测——板画着 7 秒的节奏而片子只有
    4 秒，Seedance 拿到的时间轴就是一份对不上片长的假脚本（与 gen-video 读侧
    对称闸同一成因）。未传 total 时才回落 dur（离线场景没有更好的事实）。"""
    bs = beats_of(shot) if beats is None else beats
    base = total if total and total > 0 else (shot.get("dur") or 0)
    slots = beat_slots(base, max(len(bs), 1))
    return [str(b.get("t") or "").strip() or slots[i] for i, b in enumerate(bs)]


# authored `t` 的秒段解析：认 `0-0.6s` / `第0-0.6秒` / `0-0.6` 三种写法；
# 认不出返回 None（自由文本无法体检，宁可跳过不误报）
_SPAN_RE = re.compile(r"^第?\s*([\d.]+)\s*[-~—]\s*([\d.]+)\s*[s秒]?$")


def _span_of(t) -> tuple[float, float] | None:
    m = _SPAN_RE.match(str(t or "").strip())
    if not m:
        return None
    try:
        a, b = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    return (a, b) if b > a else None


# 拍密度下限（秒/拍）：低于它的时间轴是超载脚本。实测 0.56s/拍时 Seedance 只挑
# 两三个主事件演完整、其余拍整段丢弃。0.8s 是「一个可辨识动作」的底线量级。
MIN_BEAT_SEC = 0.8


def beats_density(shot: dict, total: float | None) -> str | None:
    """拍密度体检：`effective_beats`（authored 或自动拆拍）按实际请求秒数摊，
    平均每拍 < MIN_BEAT_SEC 报一句 finding（None=没问题/没料可判）。
    **对 authored beats 只报不并拍**——哪几拍该合是创作决定（同 beats_coverage
    纪律）；自动拆拍已在 `auto_beats` 按时长收敛，正常不会走到这条告警。"""
    beats, auto = effective_beats(shot, total)
    if len(beats) < 2 or not total or total <= 0:
        return None
    per = total / len(beats)
    if per >= MIN_BEAT_SEC:
        return None
    head = (f"{len(beats)} 拍铺进 {total:g}s（平均 {per:.2f}s/拍）超出视频模型可执行"
            f"密度（<{MIN_BEAT_SEC}s/拍模型会自行丢拍只演主事件）——")
    keep = auto_beat_cap(total)
    if auto and len(beats) <= keep:
        # 自动拆拍已收敛到下限仍不达标 = 镜头本身太短，再劝"并拍"就是自相矛盾
        # （告警的价值在于给得出可行动项，给不出就不该说那句话）
        return head + f"本镜自动拆拍已收敛到下限 {len(beats)} 拍，只能加长镜头秒数"
    return head + f"建议并拍到 ≤{keep} 拍（每拍 ≥{TARGET_BEAT_SEC}s），或加长镜头秒数"


def beats_coverage(shot: dict, total: float | None) -> str | None:
    """authored `t` 的覆盖体检：秒段要连续铺满实际请求秒数。返回一句 finding
    或 None（没问题/没有可判的 t）。**引擎只报数不改写**——t 是创作字段，
    错位该由指挥层改（或删掉 t 让引擎按 total 均分）。"""
    beats = beats_of(shot)
    spans = [_span_of(b.get("t")) for b in beats if str(b.get("t") or "").strip()]
    spans = [s for s in spans if s]
    if not spans or not total or total <= 0:
        return None
    probs = []
    if spans[0][0] > 0.15:
        probs.append(f"首拍从 {spans[0][0]:g}s 才开始（前面留了空窗）")
    for prev, cur in zip(spans, spans[1:]):
        gap = cur[0] - prev[1]
        if gap > 0.15:
            probs.append(f"{prev[1]:g}s→{cur[0]:g}s 之间断档 {gap:.1f}s")
        elif gap < -0.15:
            probs.append(f"{cur[0]:g}s 起的拍与上一拍重叠 {-gap:.1f}s")
    off = spans[-1][1] - total
    if abs(off) > 0.5:
        probs.append(f"末拍收在 {spans[-1][1]:g}s 而实际请求秒数是 {total:g}s"
                     f"（{'超出' if off > 0 else '差'} {abs(off):.1f}s）")
    if not probs:
        return None
    return "；".join(probs) + "——板与成片节奏会错位：改 t，或删掉 t 让引擎按实际秒数均分"


# 自动拆拍的句读切分：分号/句号是运动设计里天然的拍间边界（顿号/逗号太细，
# 会把一个连贯动作剁碎）。只切标点、绝不做语义改写——引擎无 LLM 的既有边界。
_CLAUSE_RE = re.compile(r"[；;。!！?？\n]+")


def _merge_evenly(items: list[str], k: int) -> list[str]:
    """把 n 个句子**均匀**并成 k 拍（保时序、一句不丢，前 n%k 拍各多吃一句）。

    刻意不写 `per = ceil(n/k)`：那样会并过头（6 句并 4 拍时 per=2 → 只出 3 拍），
    拍数与上限对不上；均分才谈得上"按时长配拍"。"""
    n = len(items)
    if k >= n or k <= 0:
        return list(items)
    base, extra = divmod(n, k)
    out, i = [], 0
    for g in range(k):
        take = base + (1 if g < extra else 0)
        out.append("，".join(items[i:i + take]))
        i += take
    return out


def auto_beat_cap(total: float | None) -> int:
    """自动拆拍的拍数上限 = 时长 ÷ `TARGET_BEAT_SEC`，钳进 `AUTO_BEATS_MIN~PANEL_MAX`，
    **再向下取到最近的整齐拍数**（`TIDY_PANELS`：能整除成矩形、板面无空格的数）。

    向下取整齐是刻意的：5/7/11 拍只能排成不满行，板面留一截空位（信息密度与
    可读性都差一档）；退到 4/6/10 拍每拍反而更从容（7s 镜 5 拍→4 拍 = 1.75s/拍），
    而拍数是引擎自己定的、本就没有非 5 不可的理由。手写 beats 不受此约束
    （质数拍由 `grid_of` 的不满行居中兜底）。

    无时长信息（total 与 dur 都拿不到）时回落 `PANEL_MAX`——不知道多长就别自作主张
    并拍，交由句读与面板上限决定。"""
    try:
        t = float(total or 0)
    except (TypeError, ValueError):
        t = 0.0
    if t <= 0:
        return PANEL_MAX
    cap = max(AUTO_BEATS_MIN, min(PANEL_MAX, int(t / TARGET_BEAT_SEC)))
    tidy = [p for p in TIDY_PANELS if p <= cap]
    return tidy[-1] if tidy else cap


def auto_beats(shot: dict, total: float | None = None) -> list[dict]:
    """无 authored beats 时的确定性拆拍：把该镜既有的运动设计（action /
    video_prompt / end_state / light_shift / camera）按句读切成时序拍，
    **再按镜头时长收敛拍数**（`auto_beat_cap`：每拍 ≥TARGET_BEAT_SEC）。

    收敛是本函数的职责而非越权：这一份拍序列本就是引擎代切的（不是创作资产），
    "作者这句写了几个分号"不该决定视频模型的执行密度；authored beats 始终优先
    且一个字不动（`effective_beats`），超密只报警不并拍。
    `total` = 实际请求秒数（`voicecast.request_seconds` 同源），缺省回落 `dur`。
    素材为零（连一句运动设计都没有）返回空表——那种镜生成板是无效支出钱。"""
    clauses: list[str] = []
    for field in ("action", "video_prompt", "end_state"):
        v = str(shot.get(field) or "").strip()
        for seg in _CLAUSE_RE.split(v):
            seg = seg.strip().strip("，, 　")
            if seg:
                clauses.append(seg)
    # 去重保序：action/end_state 常已被作者并进 video_prompt
    seen: set = set()
    uniq = [c for c in clauses if not (c in seen or seen.add(c))]
    if not uniq:
        return []
    # 按时长收敛（无时长信息时只受面板上限约束），均匀并拍保时序不丢句
    cap = auto_beat_cap(total if (total and total > 0) else shot.get("dur"))
    if len(uniq) > cap:
        uniq = _merge_evenly(uniq, cap)
    beats = [{"action": c} for c in uniq]
    cam = str(shot.get("camera") or "").strip()
    if cam:
        beats[0]["camera"] = cam
    light = str(shot.get("light_shift") or "").strip()
    if light:
        beats[0]["light"] = light
    return beats


def effective_beats(shot: dict, total: float | None = None) -> tuple[list[dict], bool]:
    """生成（板提示词）与消费（时间轴）共用的拍序列——**单一真源**。

    返回 `(beats, auto)`：authored 优先，缺省回退 `auto_beats`（auto=True）。
    两侧必须取同一份：板按自动拆拍画、时间轴却按别的口径编，秒段对不上。

    **`total` 必须一路传到底**：自动拆拍的拍数按它收敛（`auto_beat_cap`），
    同一个镜用不同 total 调用会得到不同拍数——板按 4 拍画、时间轴按 6 拍编
    就是这条真源被绕开的样子。缺省回落 `dur`（离线场景没有更好的事实）。"""
    bs = beats_of(shot)
    if bs:
        return bs, False
    ab = auto_beats(shot, total)
    return ab, bool(ab)


def beats_sig(shot: dict, total: float | None = None) -> str:
    """当前拍序列的内容指纹（`sha256:<hex16>`，与血缘指纹同格式）——板漂移判据之二。

    取 `effective_beats` 的净化内容（t/action/camera/framing/light/sound 六键）
    序列化哈希：authored beats 改一拍、或自动拆拍的来源字段（video_prompt/action/
    end_state/camera）改一个字，指纹即变。`dur_at` 只盯得住时长漂移；提示词漂移
    （镜面从城中村改成江南古镇后，板仍显"新鲜"、gen-video 照附旧节奏板）
    要靠这枚指纹——板生成时留痕 `gen.sketch.sig`，此后一变即报「⚠ 拍序列已变」。

    `total` 必须与生成那一刻同基准（`gen.sketch.seconds`）：自动拆拍的拍数随时长
    收敛，拿另一个 total 算指纹会把"时长变了"误报成"拍内容变了"（时长漂移自有
    `dur_at` 那条报，两条各司其职不重复喊）。"""
    beats, auto = effective_beats(shot, total)
    rows = [{k: str(b.get(k) or "").strip()
             for k in ("t", "action", "camera", "framing", "light", "sound")}
            for b in beats]
    blob = json.dumps({"auto": auto, "beats": rows}, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# dur 漂移容差（秒）：小于半秒的 probe 抖动不值得重画一张板
DUR_DRIFT_TOL = 0.75


def board_drift(shot: dict) -> dict | None:
    """板生成后的漂移体检——**唯一真源**（scanner 的 `_sketch_view`、CLI 的
    `sketch list` 与 gen-video 的 `_warn_sketch` 都消费这一份，绝不各写一遍判据）。

    返回 `{"dur": {was, now}, "beats": True}` 的子集，无漂移/无板返回 None：
      · dur 漂移：`gen.sketch.dur_at` 与当前 `dur` **同量纲**对拍（绝不拿
        seconds 比 dur——折停顿的项目会恒报假漂移）；
      · 拍序列漂移：`gen.sketch.sig` 与当前 `beats_sig` 对拍——beats/提示词
        改过而板没重生。旧板没留 sig 时不判（宁可漏报不误报）。
    纯文档字段零探测；引擎只报不改写——重生与否是指挥层/用户的决定。"""
    if not board_of(shot):
        return None
    gs = (shot.get("gen") or {}).get("sketch") or {}
    out: dict = {}
    da = gs.get("dur_at")
    try:
        if da and shot.get("dur") and abs(float(shot["dur"]) - float(da)) > DUR_DRIFT_TOL:
            out["dur"] = {"was": da, "now": shot.get("dur")}
    except (TypeError, ValueError):
        pass
    # 拍序列漂移的两条判据（任一成立即报，各自覆盖不同人群）：
    #   · `sig` 内容指纹——**改了字但拍数没变**也抓得住（新板才有）；
    #   · `panels` 格数——存量板（早于 sig 字段）唯一能用的判据，且语义最直白：
    #     板上真画了 N 格而现在要发 M 拍，肉眼可验证的不一致（拍数按时长收敛后，
    #     存量 6 格板对上 4 拍时间轴，不比就静默错位下去）。
    # 基准恒取生成那一刻的 seconds——拿当前 total 算会把"时长变了"误报成
    # "拍内容变了"（时长漂移自有 dur_at 那条报，两条各司其职不重复喊）。
    at = gs.get("seconds")
    sig, panels = gs.get("sig"), gs.get("panels")
    if sig and sig != beats_sig(shot, at):
        out["beats"] = True
    elif panels:
        try:
            if int(panels) != len(effective_beats(shot, at)[0]):
                out["beats"] = True
        except (TypeError, ValueError):
            pass
    return out or None


# ------------------------------------------------------------ 互斥仲裁（单一真源）
def board_of(shot: dict) -> str | None:
    """该镜已登记的简笔板路径（不判在盘——在盘判定归调用方的 has_file）。"""
    sk = shot.get("sketch")
    if not isinstance(sk, dict):
        return None
    p = str(sk.get("sheet") or "").strip()
    return p or None


def active_guide(shot: dict) -> str | None:
    """本镜生效的运动预演路径："previz" / "control" / "sketch" / None（都没配）。

    **这是互斥仲裁的唯一真源**——cli 的 `_shot_plan`、Studio scanner 的
    `guide_active` 都必须调它，绝不另写一份判定——各写一份必然分叉。

    规则：显式 `shots[].guide` 恒赢（即便指向一个空槽——用户点了名就绝不静默
    回落另一条路，那正是"两个都配了互相干扰"的事故形态；空槽的后果由调用方
    打印告警）；缺省自动仲裁按 previz > control > sketch。

    深度控制视频排在 previz 之后、beats 之前：它与 previz 争的是同一个
    `reference_video` 槽，一镜只能发一条，让先登记的 3D 编排胜出与附录判例一致；
    而它必须压过 beats——控制视频是逐帧运动源，拍表只是措辞，让文字顶掉像素锚
    就是整镜白买（`previz.v2v_shot` 的 sketch 一票否决会让它一声不响不发）。"""
    g = str(shot.get("guide") or "").strip().lower()
    if g in GUIDES:
        return g
    lanes = configured_guides(shot)
    return lanes[0] if lanes else None


def configured_guides(shot: dict) -> list[str]:
    """本镜配置了哪几条运动预演路径，按缺省仲裁的优先序排列（`active_guide` 取其首项）。

    Studio 的仲裁徽章据它判「配了几条」：判据必须与仲裁同一份，尤其 sketch 只认登记的
    板或 authored beats——自动拆拍是缺省句读的措辞，每个写了运动提示词的镜都有，按它算
    就是每一镜都"配了简笔板"。"""
    out = []
    if shot.get("previz") or shot.get("last_frame_ref"):
        out.append("previz")
    if shot.get("control"):
        out.append("control")
    if board_of(shot) or beats_of(shot):
        out.append("sketch")
    return out


# ------------------------------------------------------------ 板提示词（生成侧）
# 素描契约与五色标注系统——中英一份语义。**刻意不掺成片画风前缀**（style_prompt 是
# 成片画风，掺进来板就成了成片的彩色预览）；也不挂防字地板（板本来就要写
# 面板编号与标注文字，防字地板会与之打架）。
# 契约升级：随手工定稿样板从"纯黑白线稿"升到"铅笔素描+能量点缀"——素描灰阶
# 仍是绝对主体（这是板与成片的身份分界），点缀只准落在发光体/能量/关键识别色上，
# 一旦整幅上色，视频侧的防泄漏句（"绝不输出素描画面"）就失去了可指认的对象。
_LINE_ART_ZH = ("绘画风格：铅笔素描分镜——以石墨灰阶排线塑形的手绘素描，线条果断、"
                "动势鲜明，细节密度以看清动作与空间为度，轮廓可读性强，"
                "如同资深分镜师的动作预演稿；允许**克制的少量色彩点缀**，"
                "仅限发光体、能量与关键识别色（如刃光、灯带、发色挑染），"
                "画面整体仍以素描灰阶为绝对主体，绝不整幅上色、绝不铺环境色")
_LINE_ART_EN = ("Art style: pencil-sketch storyboard - hand-drawn graphite sketch shaped "
                "with tonal hatching, decisive strokes, strong sense of motion, detail "
                "kept to what reads the action and space, strong silhouette readability, "
                "like a veteran storyboard artist's action pre-vis; restrained color "
                "accents are allowed ONLY on glowing elements, energy effects and key "
                "identity colors (blade glow, light strips, hair streaks) - the image "
                "stays overwhelmingly graphite greyscale, never fully colored, "
                "never washed with ambient color")

# 图例规格：底部整行、字号锚定拍号标注档——字号是糊字与否的分界（实测 ~20px 的
# 板头窄条图例必糊成伪汉字、~30px 的拍号/动作短句基本正确），位置与字号都照样板。
# 图例文字是固定语义而非身份内容，照样板画即正确；视频侧语义仍随请求走
# board_role_clause 正文（图例的读者是人，模型不消费板面小字）。
_LEGEND_ZH = ("标注颜色系统（除画面点缀外唯一允许的彩色元素，每个面板至少一条标注）："
              "红色箭头=人物与物件的运动轨迹，紧贴主体画出；"
              "蓝色箭头=摄影机运动，线条更粗、贴面板边缘——推近用四角向内的箭头，"
              "横移用沿边直箭头，环绕用弧线箭头；"
              "绿色标记=取景/构图笔记；橙色标记=灯光方向；紫色波浪线=声音/情感强调；"
              "黑色文本=面板编号、秒段标签与简短动作标题（每格左上角「1. 0-0.6s」式）。"
              "板底横排一条五色图例（照样板的位置与画法：红=运动轨迹、蓝=摄影机运动、"
              "绿=取景/构图、橙=灯光方向、紫=声音/情感强调），"
              "图例文字**字号与拍号标注相当、逐字准确**，绝不缩成小字")
_LEGEND_EN = ("Annotation color system (besides the artwork accents, the only colored "
              "elements allowed; every panel carries at least one): red arrows = "
              "character/object motion paths drawn close to the subject; blue arrows = "
              "camera moves, thicker and along panel edges - four inward corner arrows "
              "for push-in, straight edge arrows for lateral moves, curved arrows for "
              "orbits; green marks = framing/composition notes; orange marks = lighting "
              "direction; purple squiggles = sound/emotion emphasis; black text = panel "
              "number, time label and a short action title (top-left of each panel, "
              "like '1. 0-0.6s'). A single legend row along the board bottom, matching "
              "the templates' placement: red = motion path, blue = camera move, green = "
              "framing note, orange = lighting direction, purple = sound/emotion. Legend "
              "text must be exact, at the same font size as the panel time labels - "
              "never shrunk to fine print")

_MOTION_RULE_ZH = ("每个面板必须画出可见的运动与身体动量，避免静态站姿；"
                   "相邻面板动作连续推进，同一人物与场景在 9 格间保持一致；"
                   "面板之间以细黑线清晰分隔，内容绝不跨格、绝不重叠")
_MOTION_RULE_EN = ("Every panel must show visible motion and body momentum - no static "
                   "standing poses; action progresses continuously across panels, with the "
                   "same character and scene kept consistent; panels are cleanly separated "
                   "by thin black rules, content never crosses or overlaps panel borders")

# 参考图职责声明（按实附组合注入——研究结论：不声明职责，模型不会自己猜对）
_REF_TEMPLATE_ZH = ("版式、笔触与标注画法以所附样板图为基准"
                    "（只学版式与画法，绝不复制其人物、场景与标题文字；"
                    "板底五色图例横条属于版式，照样板的位置与字号画）")
_REF_TEMPLATE_EN = ("Use the attached template boards only for layout, stroke style and "
                    "annotation manner - never copy their characters, scenes or title "
                    "text; the five-color legend row along the bottom is part of the "
                    "layout - reproduce its placement and font size from the templates")
_REF_SHOT_ZH = ("以所附分镜图为本镜画面基准：同一人物、同一场景、同一构图起点，"
                "将其转译为铅笔素描后再逐格推进动作")
_REF_SHOT_EN = ("Treat the attached storyboard frame as this shot's visual baseline: same "
                "characters, same set, same opening composition, translated into pencil "
                "sketch and then advanced panel by panel")
_REF_SHEET_ZH = ("出场人物的发型、服装、体型与标志配件以所附角色设定图为准，"
                 "转译为素描后仍须可辨认；每个具名角色在单个面板中至多出现一次")
_REF_SHEET_EN = ("Character hair, costume, build and signature props follow the attached "
                 "character sheets and must stay recognizable in sketch form; each named "
                 "character appears at most once per panel")

_NO_MARK_ZH = "无水印、无 logo、无签名"
_NO_MARK_EN = "no watermark, no logo, no signature"


# 版式最扁比（列/行）：超过它的整除分解太扁（1×5 每格是竖条，"电影感面板"不成立），
# 宁可用不满行居中排。
_GRID_MAX_RATIO = 3.0


def grid_of(n: int) -> tuple[int, int, int]:
    """按拍数选网格 → `(行, 列, 末行格数)`；末行格数 == 列数表示恰好填满。

    **恰好填满是硬要求**，故绝不硬编码列数：定死 3 列时 4 拍会算出「2×3 网格共
    4 个面板」——2×3=6 格却只要 4 个，提示词自相矛盾，模型只能自己猜（实测同为
    4 拍：一张猜成 2×2 紧凑排满、另一张照 2×3 画完**留下两个空白框**）。
    规则：① 优先能整除且最接近正方形的分解（板与格同为 16:9 时，行列相等观感最匀）；
    ② 该分解太扁（列/行 > `_GRID_MAX_RATIO`，即 n 是 5/7/11 这类质数）才退回
    `列=ceil(√n)` 的近方网格，末行不满则居中排——绝不留空框。"""
    n = max(1, int(n))
    best = None
    for rows in range(1, int(n ** 0.5) + 1):
        if n % rows:
            continue
        cols = n // rows
        ratio = cols / rows
        if ratio > _GRID_MAX_RATIO:
            continue
        if best is None or ratio < best[0]:
            best = (ratio, rows, cols)
    if best:
        return best[1], best[2], best[2]          # 整除：末行 == 满列
    cols = max(1, int(n ** 0.5 + 0.999))          # ceil(√n)：近方网格
    rows = -(-n // cols)
    last = n - cols * (rows - 1)
    return rows, cols, last


# 自动拆拍优先落在这些拍数上——它们都能整除成矩形，板面恒无空格（由 `grid_of`
# 派生，绝不另手写一份清单）。
TIDY_PANELS = tuple(p for p in range(AUTO_BEATS_MIN, PANEL_MAX + 1)
                    if grid_of(p)[1] == grid_of(p)[2])


def _grid_line(n: int, lang: str) -> str:
    rows, cols, last = grid_of(n)
    full = last == cols
    if lang == "en":
        shape = (f"a {rows}x{cols} grid" if full
                 else f"{rows} rows ({cols} panels per row, the last row holding "
                      f"only {last} centered)")
        return (f"Create a 16:9 professional storyboard pre-vis board: {shape} of exactly "
                f"{n} cinematic panels numbered 1-{n}, reading left to right, top to bottom. "
                f"The layout must contain exactly {n} panels - never add empty or blank "
                f"frames to fill out the grid")
    shape = (f"{rows} 行 × {cols} 列的网格（恰好填满，无空位）" if full
             else f"{rows} 行网格——前 {rows - 1} 行每行 {cols} 格，"
                  f"最后一行只有 {last} 格并居中排列")
    return (f"创建一张 16:9 的专业分镜预演板（storyboard pre-vis board）：{shape}，"
            f"共**恰好 {n} 个**电影感面板，按 1-{n} 编号，从左到右、从上到下排列；"
            f"画面里只能有这 {n} 个面板，**绝不为凑满网格多画空白格**")


def _header_line(sid, lang: str) -> str:
    """板头标题纪律。样板带标题栏（版式的一部分），但**不指定标题文字模型就会
    照搬样板上的人名**——标题原文由此行钉死，脚注只许写通用用途说明。"""
    if lang == "en":
        return (f"Board header: a large title at top-left reading exactly '镜 {sid}' - "
                "never copy the title or character names from the template boards; "
                "an optional one-line usage footnote may sit at the bottom of the board")
    return (f"板头左上以大号字写本板标题「镜 {sid}」——逐字准确，绝不照搬样板上的"
            "标题与人名；板底可留一行小字用途脚注（如「本分镜用于动作设计与镜头调度参考」）")


_SPAN_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*s?\s*$")


def _whole_seconds(t: str) -> str:
    """`a-bs` 形态的秒段标签取整秒；authored 自由文本原样。"""
    m = _SPAN_RE.match(str(t))
    if not m:
        return str(t)
    return f"{int(float(m.group(1)) + 0.5)}-{int(float(m.group(2)) + 0.5)}s"


def _fmt_t(t: str, lang: str) -> str:
    """秒段标签的展示格式：zh 把 `0-1.5s` 归一成 `第0-1.5秒`（authored 自由文本
    已带「秒/第」的原样保留），en 原样。"""
    t = str(t).strip()
    if lang == "en":
        return t
    if "秒" in t or t.startswith("第"):
        return t if t.startswith("第") else f"第{t}"
    return "第" + (t[:-1] if t.endswith("s") else t) + "秒"


def _beat_line(i: int, t: str, b: dict, lang: str) -> str:
    """单个面板的内容行：动作为主体，镜头/构图/光/声按有则附。"""
    if lang == "en":
        parts = [str(b.get("action") or "").strip()]
        for key, label in (("camera", "camera"), ("framing", "framing"),
                           ("light", "light"), ("sound", "sound")):
            v = str(b.get(key) or "").strip()
            if v:
                parts.append(f"{label}: {v}")
        return f"{i}. {t}: " + "; ".join(p for p in parts if p)
    parts = [str(b.get("action") or "").strip()]
    for key, label in (("camera", "镜头"), ("framing", "构图"),
                       ("light", "光"), ("sound", "声")):
        v = str(b.get(key) or "").strip()
        if v:
            parts.append(f"{label}：{v}")
    return f"{i}. {_fmt_t(t, lang)}：" + "｜".join(p for p in parts if p)


def board_prompt(shot: dict, *, lang: str = "zh", with_template: bool = False,
                 with_shot_image: bool = False, char_names: list[str] | None = None,
                 scene_name: str | None = None, note: str | None = None,
                 total: float | None = None) -> str:
    """拼装该镜简笔板的生图提示词（确定性，逐段与所附参考图组合对应）。

    拍序列取 `effective_beats`（authored 优先、缺省按句读自动拆拍）；连自动拆拍
    都拆不出（该镜没有任何运动设计）才抛 ProjectError——那种板只是九张随机静帧，
    生成它是无效支出钱。"""
    beats, _auto = effective_beats(shot, total)
    if not beats:
        raise ProjectError(
            f"镜 {shot.get('id')} 没有任何运动设计（video_prompt/action/end_state 全空，"
            "也没写 sketch.beats）——先补运动设计或按 kinema-sketchboard 写 beats")
    if len(beats) > PANEL_MAX:
        # **报不改写**（authored beats 是创作资产）：静默截断是「板 12 格、时间轴
        # 15 段」的两套事实——Studio 按全量拍下发对照表去对一张截过的板，逐格
        # 核对必然错行；钳在 effective_beats 更糟，会把超出的拍连时间轴一起丢掉
        raise ProjectError(
            f"镜 {shot.get('id')} 写了 {len(beats)} 拍，超过单板上限 {PANEL_MAX} 格"
            "——画板放不下。请把 sketch.beats 合并到 ≤"
            f"{PANEL_MAX} 拍（分段时间轴提示词不受此限，只有画板受限）")
    times = beat_times(shot, beats, total=total)
    en = lang == "en"
    lines: list[str] = [_grid_line(len(beats), lang),
                        _LINE_ART_EN if en else _LINE_ART_ZH,
                        _header_line(shot.get("id"), lang)]
    # 参考图职责声明（顺序与 cli 附图顺序一致：样板 → 分镜图 → 角色设定图）
    refs = []
    if with_template:
        refs.append(_REF_TEMPLATE_EN if en else _REF_TEMPLATE_ZH)
    if with_shot_image:
        refs.append(_REF_SHOT_EN if en else _REF_SHOT_ZH)
    if char_names:
        refs.append(_REF_SHEET_EN if en else _REF_SHEET_ZH)
    lines.extend(refs)
    # 本镜信息（场景/出场只报名字——外观归设定图，复述即漂移）
    info = []
    if scene_name:
        info.append(f"Scene: {scene_name}" if en else f"场景：{scene_name}")
    if char_names:
        info.append(("Cast: " if en else "出场：") + "、".join(char_names))
    if info:
        lines.append("; ".join(info) if en else "；".join(info))
    header = ("Panel contents (each panel advances the action along the timeline):"
              if en else "面板内容（每格按秒段推进动作）：")
    lines.append(header)
    lines.extend(_beat_line(i + 1, t, b, lang)
                 for i, (t, b) in enumerate(zip(times, beats)))
    lines.append(_MOTION_RULE_EN if en else _MOTION_RULE_ZH)
    lines.append(_LEGEND_EN if en else _LEGEND_ZH)
    # 重生成意见（驳回闭环同范式）：用户在灯箱里输入的修改要求直接编译进本次提示词
    fix = str(note or "").strip()
    if fix:
        lines.append((f"Revision focus (must follow): {fix}" if en
                      else f"本次修正重点（务必执行）：{fix}"))
    lines.append(_NO_MARK_EN if en else _NO_MARK_ZH)
    return "\n".join(lines)


def panel_lines(shot: dict, total: float | None = None, lang: str = "zh") -> list[str]:
    """逐拍对照行（灯箱拍表 / 清单用）——与板提示词的「面板内容」**完全同一拼装**
    （`_beat_line` 单一真源）：用户对照板上第 N 格与这行文字即可核对时间与动作。"""
    beats, _auto = effective_beats(shot, total)
    times = beat_times(shot, beats, total=total)
    return [_beat_line(i + 1, t, b, lang)
            for i, (t, b) in enumerate(zip(times, beats))]


# ------------------------------------------------------------ 时间轴文本（消费侧）
# 板随视频请求附上时的**职责声明 + 风格防护句**——成片防泄漏的生命线：不写这句，
# 模型会把"参考图=铅笔素描"理解成画风指令，成片直接变素描（与 V2V 契约句防灰模
# 配色同一成因，同一个解法：点明"这个参考只锁运动，不锁画风"）。
#
# **位置即效力**：这句由 prompts 拼在提示词**最前面**（与首帧/末帧契约句同段），
# 不挂在时间轴文本尾巴上。挂尾巴时它落在千余字提示词的中后段，模型读到"参考图"
# 三个字时早已过了这句——实测 6 个板镜里 2 个把红蓝标注箭头画进了开头几帧。
# 光靠正文一句还不够，`prompts.BOARD_FLOOR_*` 会把同一批词补进「避免出现」串：
# 国产视频模型对负面串的服从度显著高于正文中段的陈述句。两处同时说才压得住。
_BOARD_ROLE_ZH = ("所附铅笔素描分镜板**不是画面参考、只是分镜脚本**：各面板按时间顺序"
                  "对应本镜的分段，红色箭头为人物运动轨迹、蓝色箭头为摄影机运动，"
                  "绿色方框为取景/构图提示、橙色箭头为灯光方向、紫色波浪线为强调节拍，"
                  "格线、方框与箭头都是标注符号而非画面元素；"
                  "只按它执行动作、走位与机位节奏，"
                  "画面风格、色彩、材质与光影一律以本镜画面参考图为准，"
                  "绝不输出铅笔素描或草图质感画面，绝不把标注箭头、格线与文字画进画面")
_BOARD_ROLE_EN = ("The attached pencil-sketch storyboard is **not a visual reference, only a "
                  "shot script**: its panels map in order to this shot's time segments, red "
                  "arrows are character motion paths, blue arrows are camera moves, green "
                  "boxes are framing/composition notes, orange arrows are lighting "
                  "direction, purple squiggles are emphasis beats, and the panel grid, "
                  "boxes and arrows are annotation marks, not picture elements. Follow "
                  "it only for action, blocking and camera rhythm - visual style, color, "
                  "materials and lighting all come from this shot's picture reference. "
                  "Never render pencil-sketch or rough-draft imagery, and never draw the "
                  "annotation arrows, panel grid or labels into the picture")


def board_role_clause(lang: str = "zh", *, base: str | None = None) -> str:
    """板职责声明 + 风格防护句（提示词头部拼装用，见模块内位置说明）。

    `base`：画风与光影的归属参考。缺省指「本镜画面参考图」（分镜图，全能参考的
    @图片1）；降级路线下分镜图整个不进请求、@图片1 换成场景基准图，这句必须
    跟着改指——否则它指向一张没发出去的图。
    """
    if lang == "en":
        return (_BOARD_ROLE_EN.replace("this shot's picture reference", base)
                if base else _BOARD_ROLE_EN)
    return _BOARD_ROLE_ZH.replace("本镜画面参考图", base) if base else _BOARD_ROLE_ZH


def timeline_text(shot: dict, lang: str = "zh", *,
                  total: float | None = None, native: bool = False,
                  unit: str = "second") -> str:
    """把 beats 编译成发给视频模型的分段时间轴（timeline prompting）。

    `unit` 决定分段拿什么标时间（能力位真源 `VideoProvider.timeline_unit`）：
    `second` 发秒段「第0-3秒：…」，`shot` 发不带时间的顺序编号「第1段：…」。
    Seedance 2.0 系列不响应精确秒段，其提示词指南把它列为「支持不稳定，强行限制
    时长可能导致生成结果异常」，且明写「不强制限制每段时长」——那一代发秒段是
    **减分**而不是没用；2.5 才响应整数秒。拍序列本身两代共用，换的只是段头标记。

    **段头恒不带机位义**：「镜头 N」/「Shot N」是本仓判为多镜的写法
    （`variation.MULTISHOT_RE`，lint `multishot_syntax` 用同一条判据）。一镜一次
    调用只取回一段素材，而支持多镜生成的型号读到这个记号会在这一段素材里换机位。

    **只管时间轴**：板的职责声明归 `board_role_clause`，由 prompts 拼在提示词头部。
    职责声明若追加在本函数尾巴上，那句最要紧的"板不是画面参考"就落在千余字提示词
    的中后段，实测压不住标注箭头（见 `_BOARD_ROLE_ZH` 上方说明）。
    该句只在 `board_role_clause` 一处维护，保证它进入提示词头部而非尾部。

    拍序列取 `effective_beats`——与板生成同一份（板按自动拆拍画、时间轴按另一
    口径编，秒段就对不上了）。拆不出返回空串。

    `native=True` 时逐拍附 `sound`——**与 `prompts.py` 的 sfx 注入同一道门**
    （native 由模型原生出音；dubbed/kenburns 的声音归合成段，写了也没有消费者）。
    把这半边排除在提示词之外，同一份 beats 就出现字段覆盖不对称：花钱
    画的板 PNG 拿到了声音脚本（`_beat_line` 恒编「声：」），真正出声的视频模型拿不到。
    标签**与 `_beat_line` 逐字同源**（zh「声」/ en「sound」）——同一个字段在两个消费者
    那里叫两个名字，就是给下一个人埋一次"grep 不到"。
    判据由调用方传入（照 flf2v/ref_video 的既有纪律），本函数不自读章节 motion。"""
    beats, _auto = effective_beats(shot, total)
    if not beats:
        return ""
    by_shot = unit == "shot"
    # 响应时间戳的型号以 1 秒为单位：秒段标签整秒化，与台词时间轴同一粒度
    times = [] if by_shot else [_whole_seconds(t) for t in beat_times(shot, beats, total=total)]
    en = lang == "en"
    segs = []
    for i, b in enumerate(beats):
        # 段内字段序照厂商指南：运镜或镜头切换 → 主体动作与表情 → 位置/空间 → 音频
        parts = []
        cam = str(b.get("camera") or "").strip()
        if cam:
            parts.append(("camera: " if en else "镜头：") + cam)
        parts.append(str(b.get("action") or "").strip())
        lt = str(b.get("light") or "").strip()
        if lt:
            parts.append(("light: " if en else "光：") + lt)
        sd = str(b.get("sound") or "").strip()
        if native and sd:
            parts.append(("sound: " if en else "声：") + sd)
        # 段头只标先后（顺序编号那一支不含任何时间标记），不标机位
        head = (f"Segment {i + 1}: " if en else f"第{i + 1}段：") if by_shot else \
               (f"{times[i]}: " if en else f"{_fmt_t(times[i], lang)}：")
        segs.append(head + ("; " if en else "，").join(p for p in parts if p))
    return ("Timeline: " if en else "时间轴：") + (". " if en else "；").join(segs)


def timeline_has_sound(shot: dict, *, total: float | None = None,
                       native: bool = False) -> bool:
    """本镜的分段时间轴是否真的带出了逐拍声音。

    给 `prompts.video_prompt` 判「还要不要再发一遍镜级 `环境音效：`」用：`sfx` 与
    `beats[].sound` 是同一套声音设计的汇总版与逐拍版，写全的章节两者恒同时非空，
    并存即同一批句子发两遍。同制的先例在 `video_prompt()`——自动拆拍时
    时间轴直接替代正文。逐拍版在场，汇总版让位。"""
    if not native:
        return False
    beats, _auto = effective_beats(shot, total)
    return any(str((b or {}).get("sound") or "").strip() for b in (beats or []))


# ------------------------------------------------------------ 登记 / 清除
def board_out(project, shot: dict) -> Path:
    """该镜板产物的标准落位：`<章节>_work/sketch/shot_<id>_board.png`。"""
    return project.subdir(SKETCH_SUBDIR) / f"shot_{shot['id']}_board.png"


def register_board(project, shot: dict, path, *, prompt: str, provider: str,
                   cost: float = 0.0, refs: int = 0, auto: bool = False,
                   seconds: float | None = None) -> None:
    """把生成好的板登记进 `shots[].sketch.sheet` + `gen.sketch`（主线程专用）。

    **绝不碰 `image`/`clip`/`review`**——板不是分镜画面，不进审阅状态机。
    `auto` 留痕这张板的拍序列来自自动拆拍（复盘与前端标注用）。"""
    sk = shot.setdefault("sketch", {})
    sk["sheet"] = str(path)
    gen = shot.setdefault("gen", {})
    prev = (gen.get("sketch") or {}).get("version") or 0
    gen["sketch"] = {"provider": provider, "cost": round(float(cost or 0), 4),
                     "version": prev + 1,
                     "at": datetime.now().isoformat(timespec="seconds"),
                     "panels": len(effective_beats(shot, seconds)[0]), "refs": refs,
                     "auto": bool(auto),
                     # 时间基准双留痕：seconds=板按哪个总秒画的（request_seconds 同源）；
                     # dur_at=生成时的 dur——漂移判据用它与当前 dur **同量纲**对拍
                     # （拿 seconds 比 dur 会在「dur 折了停顿」的项目上恒报假漂移）
                     "seconds": round(float(seconds), 2) if seconds else None,
                     "dur_at": shot.get("dur"),
                     # 拍序列指纹：beats/提示词此后一改，board_drift 即报「拍序列已变」
                     # （与 seconds 同基准算——自动拆拍的拍数随时长收敛）
                     "sig": beats_sig(shot, seconds),
                     "prompt": prompt[:400]}


def clear_board(project, shot_no: int) -> dict:
    """摘除该镜的板挂载（产物文件保留，beats 不动——那是指挥层写的创作资产）。"""
    s = next((x for x in project.shots if x.get("id") == shot_no), None)
    if s is None:
        raise ProjectError(f"找不到镜 {shot_no}")
    dropped = []
    sk = s.get("sketch")
    if isinstance(sk, dict) and sk.get("sheet"):
        sk.pop("sheet", None)
        dropped.append("sketch.sheet")
    if (s.get("gen") or {}).get("sketch"):
        s["gen"].pop("sketch", None)
        dropped.append("gen.sketch")
    if dropped:
        project.save()
    return {"shot": shot_no, "dropped": dropped}
