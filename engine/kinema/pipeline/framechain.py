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

"""首尾帧衔接：把每镜的末帧 pin 到成片里紧接着出现的那一镜的首帧上。

图生视频的首帧驱动只锁住一镜的起点，镜与镜之间仍是硬切。把下一镜的分镜图作为本镜的
末帧发出去，模型就会让画面朝那个构图收束，接缝两端落在同一个画面上。末帧属于输入图、
不计入按秒计费，衔接本身不额外花钱。

**衔接是显式 opt-in，不是缺省**：缺省档是「逐镜全能参考」（一镜一片：分镜图＋简笔板
＋设定图全作 reference_image，见 `cli._shot_plan`），镜与镜之间直接拼接。要衔接有两个
粒度——章节顶层 `frame_chain: true`（全章）或镜级 `shots[].frame_chain: true`
（本镜与成片里紧接着的下一正镜结对，只焊这一处）。代价要说清：参与衔接的两镜都退回
首帧驱动任务，设定图与简笔板随请求附发的通道就没有了（首帧任务与参考媒体官方互斥），
一致性只剩文字锚——所以只在「两镜必须无缝相连」的点上开。

## 焊缝两端

一条焊缝要**两端同时成立**：上游真把末帧发到下游镜的分镜图上（`sends`），
且下游真把那张图当第 0 帧硬锁（`receives`）。走全能参考 / V2V 的镜在 seedance
请求体里没有 `first_frame` 项（分镜图降级成 `role=reference_image`），焊过去只是
朝一张参考图收束、它并不从那张图起步——页面标着「→ 镜N」而成片里是一次形变。

这类镜（`island`）两侧焊缝一律断，并由 `sync_seams` 在那两处落无缝转场。

**本模块是这条链路的唯一判据来源**：渲染阶段（`cli.stage_gen_video`）与 Studio 章节
视图（`studio.scanner`）都从这里取。两侧分头判会出现「页面标着衔接、实际一帧末帧都没
发出去」这类对不上的情况，而这种不一致只有等成片出来才看得见。
"""
from __future__ import annotations

from . import transitions
from .. import review
from .checkpoint import has_file

# 断链原因 → 面向人的措辞。真发日志、dry-run 清单、网页指令三处共用同一份：
# 各写各的会让用户拿着日志里的说法去页面上找、却找不到对应的字眼。
# `no_image` 不由 `scan` 产出（它只看结构）——那是渲染侧与网页侧各自按手上的
# 图信息补判的一种断链，措辞仍归这里管。
BREAK_ZH = {
    "transition": "下一镜是转场·不衔接",
    "end": "末镜·无衔接",
    "no_image": "下一镜缺图·不衔接",
    # 上游端不成立的两种：本镜没有末帧槽（全能参考/V2V），或末帧槽已被 previz 终态占用
    "ref_mode": "本镜走全能参考·不发首尾帧",
    "v2v": "本镜走参考视频(V2V)·不发首尾帧",
    "control": "本镜走深度控制视频(V2V)·不发首尾帧",
    "previz_last": "本镜末帧给 previz 终态·不焊下一镜",
    # 下游端不成立：下一镜的分镜图只是参考图、不是它的第 0 帧，焊过去是假的
    "ref_next": "下一镜走参考模式·接不住末帧",
    # 能力面：该型号根本没有末帧槽（`supports_last_frame`）。**不是结构断点**，
    # 故不由 `scan` 产出——换个 provider 重跑同一份文档，链就回来了
    "no_last_frame": "本模型不支持末帧·不衔接",
}

# 「因生成模式而断」的原因集合。`transition`（已有转场）/`end`（无缝可言）/
# `no_image`（缺图是临时态，补图即复原）三项不在内。
MODE_BREAKS = frozenset({"ref_mode", "v2v", "control", "previz_last", "ref_next"})

# 其中会被 `sync_seams` 自动落软切的只有孤岛那三项。`previz_last` 不在内：那一镜仍以
# 分镜图第 0 帧硬锁、上游照常焊得进来，断的只是出链一侧；且 previz 常整段连着登记
# （一章 8 镜可以全带末帧），自动补等于给那章塞 7 条软切。那一处照实报断因，不动结构。
ISLAND_BREAKS = frozenset({"ref_mode", "v2v", "control", "ref_next"})

# 自动无缝转场的身份标记（写在 `shots[].transition.auto`）——`sync_seams` 凭它区分
# 「自己上一轮插的」与「用户手写的」，后者一个都不碰。
AUTO_MARK = "island"
AUTO_TYPE = "seamless"


def active(data: dict, motion: str) -> bool:
    """本章是否处于**章级**衔接态。`motion` 取**已归一**的模式名（`Project.motion` 口径）。

    仅 native 成立：dubbed 走参考媒体（`ref_audio`）通道，官方规定它与首/末帧互斥；
    kenburns 根本不调用视频模型。把模式并进判据而不是留给各调用方自己再
    `and native`，是因为读侧不止一处（见模块说明）。

    **缺省关闭**：缺省档是逐镜全能参考（一镜一片、镜间直拼），衔接的代价是参与镜
    退回首帧任务、设定图与简笔板附发通道全部让位（见模块说明）——这笔账不该被静默
    换掉。要全章衔接在章节写 `frame_chain: true`（或本次 `--chain`）；只焊某两镜用
    镜级 `shots[].frame_chain: true`（见 `pair_opt_in`）。
    """
    return bool(data.get("frame_chain", False)) and motion == "native"


def pair_opt_in(shot: dict) -> bool:
    """镜级衔接表态（`shots[].frame_chain: true`）：本镜与成片里紧接着出现的下一
    正镜做首尾帧衔接——「用户点名给这两镜焊一道缝」的落点，其余镜照走全能参考。

    只表达**出链**一侧：下游镜被这道焊自动锁成首帧接收方（`cli._shot_plan` 按链图
    反查），不需要也不该在下游镜再写一份。转场断链/下游是显式参考孤岛等结构规则
    与章级衔接完全同一套（`scan`）。
    """
    return bool((shot or {}).get("frame_chain"))


# ---------------------------------------------------------------------------
# 焊缝两端判据（`island` / `sends` / `receives` 是本文件对外的三个原子谓词）
# ---------------------------------------------------------------------------
def island(shot: dict, *, v2v: bool = False, control: bool = False) -> bool:
    """本镜是否「参考态孤岛」：**既不锁第 0 帧、也没有末帧槽**，两侧焊缝一律断。

    两条路都落在 seedance 的同一个分支上（`content[]` 只挂 `role=reference_image`，
    请求体里没有 `first_frame`/`last_frame` 项）：
      · **全能参考**——`shots[].sketch.reference` 逐镜 opt-in × 板真在盘
        （判据取 `sketchboard.reference_shot`，不在这里抄第二份）；
      · **V2V 运动迁移**——章/项目级 `previz_v2v`（或 `gen-video --previz`）×
        本镜有可发的 previz 参考片（判据取 `previz.v2v_shot`）；
      · **深度控制视频**——本镜有可发的控制视频（判据取 `control.control_shot`）
        × native × provider 能力。

    `v2v` / `control` 由调用方各按「总闸 × provider 能力」算好传入：那些是运行时的
    （`--previz` 可覆盖、provider 随路由变），揉进静态谓词会让同一份章节文档在两次调用里
    得出不同的孤岛集合，而这个集合要落盘（见 `sync_seams`）。两个开关分开传而不是
    并成一个，是因为它们真的可以一开一关：并成一个会让关着的那一路上的镜也被判成
    孤岛，链态与真发就此分叉。
    """
    from .. import control as control_mod
    from .. import previz as previz_mod
    from .. import sketchboard as sketch_mod
    # 衔接只在 native 成立（见 `active`），故 `reference_shot` 的 native 形参恒 True
    if sketch_mod.reference_shot(shot, True):
        return True
    if v2v and previz_mod.v2v_shot(shot):
        return True
    return bool(control and control_mod.control_shot(shot))


def sends(shot: dict, *, v2v: bool = False, control: bool = False) -> bool:
    """本镜能否把末帧焊到**下游镜的分镜图**上（上游端）。

    两种不能：孤岛镜没有末帧槽；previz 末帧镜的槽已被自己的终态位姿占用
    （「previz 末帧压过衔接链」见 `previz.py` 模块头第 3 条）——它收束到自己的终态，
    下游镜从自己的分镜图起步，这条缝同样不是焊的。
    """
    if island(shot, v2v=v2v, control=control):
        return False
    return not has_file(shot.get("last_frame_ref"))


def receives(shot: dict, *, v2v: bool = False, control: bool = False) -> bool:
    """本镜能否**接住**上游发来的末帧 = 分镜图是不是它的第 0 帧硬锁（下游端）。

    这一端必须单独判：`scan` 若只问「下一镜是不是转场/弃用」，上游会照常把末帧
    pin 到走全能参考的下游镜的分镜图上——那张图在下游只是众多 `reference_image`
    之一，不是第 0 帧，「切点仍近似连续」并不成立。
    """
    return not island(shot, v2v=v2v, control=control)


def scan(shots: list, i: int, on: bool, *, v2v: bool = False, control: bool = False,
         native: bool = False) -> tuple[dict | None, str]:
    """下标 `i` 这一镜的衔接对象：返回 `(下一镜 | None, 断链原因)`。

    原因取值：`""`（衔接成立）/ `"transition"`（下一个是转场镜）/ `"end"`（后面没镜了）
    / `"off"`（本镜不参与衔接）/ `MODE_BREAKS` 四项（因生成模式而断，见 `BREAK_ZH`）
    ——日志要按原因分别措辞，故与结果一起返回，不让调用方再扫一遍（第二遍必然与本
    函数分叉）。

    参与判据 = 章级 `on` **或** 镜级 `pair_opt_in`（后者只在 native 成立，`native`
    由调用方按已归一的 motion 传入——章级那半的模式判据已在 `active` 里做过，
    镜级表态若不在这里再验一次，dubbed 章里写了 `frame_chain` 的镜就会被当成衔接镜）。

    四条纪律：
      · **遇转场即断链**——转场镜=场景切换标记，跨转场衔接会把"家里→外面"两个场景
        硬 morph 成一段（上一张图在家、末帧突兀跳到外面）。转场镜本就无 image，
        显式判断=意图清晰 + 防 clip 素材转场等未来带图类型误连。
      · **跳过弃用镜(omt)往后找**——弃用镜不进成片，把末帧 pin 到它上面等于让本镜
        朝一个观众永远看不到的画面收束，成片里下一个出现的是更后面那镜。
      · **上游端不成立就断**（`sends`）——孤岛镜/previz 末帧镜发不出这一焊。
      · **下游端接不住也断**（`receives`）——结构上相邻、图也齐，但下游镜不把那张
        图当第 0 帧；这一条最隐蔽，只有等成片出来才看得见。
    """
    cur = shots[i]
    if not (on or (native and pair_opt_in(cur))):
        return None, "off"
    for nxt in shots[i + 1:]:
        if transitions.is_transition(nxt):
            return None, "transition"
        if review.is_omitted(nxt):
            continue
        # 两端同时不成立时先报本镜——要改的是本镜自己的配置
        if not sends(cur, v2v=v2v, control=control):
            if not island(cur, v2v=v2v, control=control):
                return None, "previz_last"
            from .. import control as control_mod
            from .. import sketchboard as sketch_mod
            if sketch_mod.reference_shot(cur, True):
                return None, "ref_mode"
            return None, "control" if control_mod.control_shot(cur) else "v2v"
        if not receives(nxt, v2v=v2v, control=control):
            return None, "ref_next"
        return nxt, ""
    return None, "end"


def plan(shots: list, on: bool, *, v2v: bool = False, control: bool = False,
         native: bool = False) -> dict:
    """全片链图：`id(镜对象) → (下一镜 | None, 断链原因)`，一次扫完供各处备查。

    **必须基于未过滤的完整 shots 预计算**：链邻居是「成片里紧接着出现的那一镜」，
    与本次渲染谁、按什么顺序渲染无关。从过滤后的列表取邻居会把成片里并不相邻的两镜
    pin 到一起，比跨转场更隐蔽——没有场景突变提示，成片里只是莫名其妙地变形一下。

    以 `id()` 作键：调用方的过滤只是筛引用，镜 dict 仍是同一个对象。
    `native` 供镜级 `pair_opt_in` 判定（见 `scan`）——渲染侧与 Studio 都要传，
    否则页面看不见结对衔接的镜。
    """
    return {id(s): scan(shots, i, on, v2v=v2v, control=control, native=native)
            for i, s in enumerate(shots)}


def welded_in_ids(chain_map: dict) -> set[int]:
    """从链图反查**被焊入**的镜集合（`id(镜对象)`）：上游真会朝它的分镜图收束的镜。

    被焊入的镜必须以分镜图第 0 帧硬锁（首帧任务），否则上游的末帧就 pin 在一张
    只是参考图的图上——所以它不能走缺省的全能参考。判据只认 `why == ""`（焊缝
    两端已由 `scan` 判成立）；上游因下游缺图而临时退回时（`no_image` 由渲染侧补判）
    下游仍按接收方待命——反正它缺图也发不出请求，补齐图后两侧口径即刻一致。
    """
    return {id(nxt) for nxt, why in chain_map.values()
            if nxt is not None and why == ""}


# ---------------------------------------------------------------------------
# 孤岛接缝 → 自动无缝转场
# ---------------------------------------------------------------------------
def _is_auto(shot: dict) -> bool:
    """是不是 `sync_seams` 自己插的那种转场镜。"""
    return (transitions.is_transition(shot)
            and ((shot.get("transition") or {}).get("auto") == AUTO_MARK))


def seam_plan(shots: list, on: bool, *, v2v: bool = False,
              control: bool = False) -> list[dict]:
    """**孤岛造成**的接缝清单：`[{"at": 插入下标, "prev": 上游镜, "next": 下游镜,
    "why": 原因}]`，按插入下标升序。

    只收 `ISLAND_BREAKS` 三项；`transition` 那处已经有人管，`no_image` 是补齐图就
    复原的临时态，`end`/`off` 不是缝。

    **插入位贴着孤岛镜**：上游发不出末帧 → 插在它后面，否则（下游接不住）插在它
    前面——孤岛镜挪位置时两侧软切跟着走。弃用镜夹在中间不影响：`compose` 走
    `active_shots`，转场镜在成片序列里仍与那两个正镜相邻。
    """
    out: list[dict] = []
    for i, cur in enumerate(shots):
        if transitions.is_transition(cur) or review.is_omitted(cur):
            continue
        nxt, why = scan(shots, i, on, v2v=v2v, control=control)
        if why not in ISLAND_BREAKS:
            continue
        # 下游正镜：scan 因模式断链时不返回它，按同一条纪律再走一遍
        j = next((k for k in range(i + 1, len(shots))
                  if not transitions.is_transition(shots[k])
                  and not review.is_omitted(shots[k])), None)
        if j is None:                     # 后面只剩弃用镜 → 本镜即成片末镜，无缝
            continue
        at = i + 1 if not sends(cur, v2v=v2v, control=control) else j
        out.append({"at": at, "prev": cur, "next": shots[j], "why": why})
    return out


def _neighbour(shots: list, k: int, step: int) -> dict | None:
    """从下标 `k` 沿 `step` 方向找最近的正镜（跳过转场镜与弃用镜）。"""
    j = k + step
    while 0 <= j < len(shots):
        s = shots[j]
        if not transitions.is_transition(s) and not review.is_omitted(s):
            return s
        j += step
    return None


def sync_seams(shots: list, on: bool, *, v2v: bool = False,
               control: bool = False) -> dict:
    """按孤岛规则**幂等同步**自动无缝转场：缺的补、过时的撤。原地改 `shots`，
    返回 `{"added": [(镜号, 上游镜号, 下游镜号, 原因)], "removed": [镜号]}`。

    **只在章级衔接开启时才有「缝」的概念**（`on` 取 `active` 口径，刻意不看镜级
    `pair_opt_in`）：缺省的全能参考档里硬切就是常态，为个别结对失败的镜插软切
    反而制造「只有那一处有特效」的突兀。`on=False` 时凡在位的自动软切一律撤走
    ——用户手写的转场照旧一个不碰，没写转场的接缝就是直接拼接。

    **在位且仍正确的一个都不动**（不重新分配镜号）——重排会让 `compose` 的转场段
    缓存名 `shot_<id>_tr.mp4` 每跑一次就换一次。期望的缝集合按「把自动转场全摘掉
    之后的形状」算（`seam_plan` 跑在剥离副本上），再与在位的逐一对账。

    **用户手写的转场一个都不碰**，那一处也不再补（已有转场即已有过渡，叠软切等于
    过渡两次）——由 `seam_plan` 天然保证：那一缝的断因是 `transition`，不在
    `ISLAND_BREAKS` 里。

    落 `seamless` 而不是别的型：edge=0 不动相邻镜、0.1s 均匀阶梯柔切、缺省静音，
    只磨掉硬边、不引入叙事标点。章首/章尾天然不会被插——接缝按定义要有上下游两个
    正镜，而 xfade 族单侧邻居会退化成字卡（在这里就是黑闪一下）。
    """
    stripped = [s for s in shots if not _is_auto(s)]
    want: dict[tuple[int, int], dict] = {}
    if on:
        for seam in seam_plan(stripped, on, v2v=v2v, control=control):
            want[(id(seam["prev"]), id(seam["next"]))] = seam
    # ① 在位的自动软切逐一对账
    keep: set[tuple[int, int]] = set()
    drop: set[int] = set()
    removed: list = []
    for k, s in enumerate(shots):
        if not _is_auto(s):
            continue
        prev, nxt = _neighbour(shots, k, -1), _neighbour(shots, k, 1)
        key = (id(prev), id(nxt)) if (prev is not None and nxt is not None) else None
        if key is not None and key in want and key not in keep:
            keep.add(key)
            continue
        drop.add(id(s))
        removed.append(s.get("id"))
    if drop:
        shots[:] = [s for s in shots if id(s) not in drop]
    # ② 缺的补上。逐条插入会让后面的下标右移，故按插入位**从后往前**插
    todo = [seam for key, seam in want.items() if key not in keep]
    nid = max((int(x) for x in (s.get("id") for s in shots)
               if str(x).isdigit()), default=0)
    placed = []
    for seam in todo:
        # 下标在**当前列表**上重算：`seam_plan` 跑的是剥离副本，两边下标不通用
        ip = next(k for k, s in enumerate(shots) if s is seam["prev"])
        jn = next(k for k, s in enumerate(shots) if s is seam["next"])
        placed.append((ip + 1 if not sends(seam["prev"], v2v=v2v) else jn, seam))
    placed.sort(key=lambda t: t[0])
    added = []
    for at, seam in placed:  # 先按成片顺序发号，镜号才随时间轴递增
        nid += 1
        added.append((nid, seam["prev"].get("id"), seam["next"].get("id"), seam["why"]))
    for (at, _seam), (nid_, *_) in zip(reversed(placed), reversed(added)):
        # 插入必须从后往前：逐条插会让后面的下标右移
        shots.insert(at, {
            "id": nid_, "kind": "transition",
            "dur": transitions.default_dur(AUTO_TYPE), "narration": "",
            "transition": {"type": AUTO_TYPE, "auto": AUTO_MARK},
        })
    return {"added": added, "removed": removed}
