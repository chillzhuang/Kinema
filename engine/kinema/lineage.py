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

"""资产血缘。

三条依赖边。前两条方向都是「设定图 → 分镜」：

  就绪度节点（readiness gate，省钱闸）
    每镜按出场表推导**必需设定图**（场景 + 出场角色 + 出场道具）。
    设定图不齐的镜禁止进入图生视频（gen-video 逐镜硬拦），生图阶段仅警示——
    没有设定参考就烧 Seedance，是最贵的一致性事故。

  过期传播（staleness，血缘追踪）
    生成时把「本次实际参考的设定图指纹」记进 gen 快照（refs）。设定图出 v2
    （文件被重生成）→ 指纹变化 → 所有下游分镜自动判定**已过期需重生成**：
    未锁定的镜直接置 retake（下次运行强制重生+归档），已通过锁定(done)的镜
    只挂 stale_refs 标记等人裁决——done 由人工置定，引擎不自动解除。

第三条是「台词文本 → 配音与片段」（`gen.<阶段>.text_fp`）：

  台词是 audio 与 clip 两个阶段的输入——TTS 念的是它，native 把它写进视频
  提示词，dubbed 的 ref_audio 由它合成。改了台词而旧产物还在盘上时，成片里
  听到的与章节文档写的就不是同一句话，而字幕恒按文档编译。

  与 `review.STAGE_FIELDS` 分工：那张表按**字段名**判，只在编辑经过 Gateway
  或 batch 时生效；本条按**内容指纹**判，手改 JSON、Studio 落盘、`lines[]`
  结构变动一样认得出。两者互补，不是同一件事写两遍。

指纹一律 sha256 短哈希（见 `fingerprint`——(size, mtime) 口径在换机恢复重写
mtime 时会全量误报「设定已更新」）。旧数据没有快照时视为无从判定（不误报），
下次重生成后自动纳入血缘。
"""
from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def fingerprint(path: str | None) -> str | None:
    """设定图当前指纹（sha256 内容哈希）；文件缺失（或仅在云端）返回 None。

    用内容哈希而非 (size, mtime)：`oss pull` / `db pull` 换机恢复会重写文件
    mtime，内容未变却全量误报"设定已更新"，`lineage mark` 一跑就为零变更
    烧一整轮重生费。设定图小且哈希低频，成本可忽略。"""
    if not path:
        return None
    from .storage.media import localize
    p = Path(localize(path))
    if not p.is_file():
        return None
    import hashlib
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 必需设定图（就绪度）
# ---------------------------------------------------------------------------
def required_refs(project, shot: dict) -> list[dict]:
    """该镜必需的设定图清单：[{key, kind, name, path}]。

    · 全局场景：本镜没有任何具名场景且项目有固定场景（scene 文本或 scene_ref）时必需
      （key=scene:main，仲裁与 `_primary_scene` 同一份）；
    · 具名场景：显式 shots[].scenes ∪ 命中的取景地（与 design_refs 同源 matched_scenes）；
    · 角色：shot.characters 点名的（缺省=全部角色）；
    · 道具：显式 shots[].props ∪ 提示词/旁白里命中的道具（与 design_refs 同源 matched_props）。
    skip_design 的项目不走设定集 → 无必需项。
    """
    if project.skip_design:
        return []
    out: list[dict] = []
    _sc, is_main = _primary_scene(project, shot)
    if is_main:
        out.append({"key": "scene:main", "kind": "scene", "name": "场景",
                    "path": project.data.get("scene_ref")})
    for sc in project.matched_scenes(shot):   # 显式 shots[].scenes ∪ 文本命中取景地
        out.append({"key": f"scene:{sc.get('name')}", "kind": "scene",
                    "name": sc.get("name"), "path": sc.get("sheet")})
    names = shot.get("characters")            # None = 全部角色出场
    for c in project.characters:
        if names is None or c.get("name") in names:
            out.append({"key": f"character:{c.get('name')}", "kind": "character",
                        "name": c.get("name"), "path": c.get("sheet")})
    for p in project.matched_props(shot):     # 显式 props ∪ 文本命中道具（与 design_refs 同源）
        out.append({"key": f"prop:{p.get('name')}", "kind": "prop",
                    "name": p.get("name"), "path": p.get("sheet")})
    return out


def primary_layout_ref(project, shot: dict) -> dict | None:
    """该镜**唯一**的俯视布局图 {key, kind, name, path}，kind 恒 `scene_top`；无则 None。

    **每镜至多一张**：seedance 全图限额 ≤9、引擎侧 ref_images 钳 7
    （`providers/video/seedance.py`），逐个命中场景各挂一张会把场景席位翻倍，把作者在
    `shots[].props` 里点名的道具挤出配额——图纸在 `cli._video_sheet_refs` 的定序里排在
    道具之前。且提示词对每张图纸都说「据它确定镜头在这个空间里的站位与视线轴线」，
    同镜发多张即多条互相冲突的机位指令。

    主场景**按镜取、不按项目取**：显式 `shots[].scenes` 的书写顺序第一个 >
    `matched_scenes` 的第一个 > 全局固定场景。显式那一档必须回 shot 单独扫——
    `matched_scenes` 遍历的是系列 `scenes[]`，返回声明顺序，与作者在镜内写的先后不是
    一回事。`if n in by_name` 是命中判据：`shots[].scenes` 里可能留着改名前的旧名字，
    或本章副本还没有的取景地。

    主场景没有图纸时 `path` 为 None（消费侧按 path 过滤），**不回落到别的场景**：
    平面图是「镜头此刻所在空间」的证据，发一张别处的图纸比不发更坏。

    **刻意不并进 `required_refs`**：那份清单是「分镜图必需的设定图」，它的两个下游
    都按「image 阶段真的用了它」立论——`readiness` 据此报缺图，`rebaseline` 据此记
    血缘基线。俯视图只随视频请求发出、不进分镜图，混进去会有两个后果：存量项目每一镜
    都报「设定图不齐」，以及「俯视图改了」被判成分镜图过期（那是要花钱重出的）。
    """
    if project.skip_design:
        return None
    sc, is_main = _primary_scene(project, shot)
    if sc is not None:
        return {"key": f"scene_top:{sc.get('name')}", "kind": "scene_top",
                "name": sc.get("name"), "path": sc.get("topview_sheet")}
    if is_main:
        return {"key": "scene_top:main", "kind": "scene_top", "name": "场景",
                "path": project.data.get("scene_topview_ref")}
    return None


def _primary_scene(project, shot: dict):
    """主场景仲裁的唯一实现 → `(具名场景 dict|None, 是否落到全局固定场景)`。

    优先级：显式 `shots[].scenes` 的书写顺序第一个 > `matched_scenes` 的第一个 >
    全局固定场景。基准图与俯视图**在同一请求里配对消费**，两份手抄的仲裁一旦
    漂移，就是基准图与图纸指向两个不同的空间。
    """
    matched = project.matched_scenes(shot)
    if matched:
        by_name = {sc.get("name"): sc for sc in matched}
        return next((by_name[n] for n in (shot.get("scenes") or [])
                     if n in by_name), matched[0]), False
    return None, bool((project.scene or "").strip()
                      or project.data.get("scene_ref"))


def primary_scene_ref(project, shot: dict) -> dict | None:
    """该镜主场景的**基准图** {key, kind, name, path}，kind 恒 `scene`；无则 None。

    与 `primary_layout_ref` 共用 `_primary_scene` 仲裁，取的是 `sheet` 而不是
    `topview_sheet`——降级路线要拿它顶 `image` 位当取景地基准，俯视图纸放进去
    会与提示词「绝不改成俯视视角」直接对撞。主场景没有基准图时 `path` 为 None，
    不回落到别的场景（别处的画面比不发更坏）。
    """
    if project.skip_design:
        return None
    sc, is_main = _primary_scene(project, shot)
    if sc is not None:
        return {"key": f"scene:{sc.get('name')}", "kind": "scene",
                "name": sc.get("name"), "path": sc.get("sheet")}
    if is_main:
        return {"key": "scene:main", "kind": "scene", "name": "场景",
                "path": project.data.get("scene_ref")}
    return None


def readiness(project, shot: dict) -> tuple[bool, list[str]]:
    """就绪度：必需设定图是否齐备。返回 (ok, 缺失清单)。已上云(URL)视为在。"""
    from .pipeline.checkpoint import has_file
    missing = [f"{r['kind']}:{r['name']}" for r in required_refs(project, shot)
               if not has_file(r.get("path"))]
    return (not missing, missing)


# ---------------------------------------------------------------------------
# 血缘登记 / 过期判定
# ---------------------------------------------------------------------------
def record_refs(shot: dict, stage: str, ref_paths: list[str]) -> None:
    """生成完成后登记本次实际参考的设定图指纹（挂进 gen 快照，随版本栈入册）。"""
    refs = {p: fingerprint(p) for p in ref_paths}
    refs = {p: f for p, f in refs.items() if f}
    if refs:
        shot.setdefault("gen", {}).setdefault(stage, {})["refs"] = refs


def stale_refs(shot: dict, stage: str = "image") -> list[str]:
    """该镜该阶段引用的设定图，自生成以来是否已变化。返回变化文件名列表。"""
    recorded = ((shot.get("gen") or {}).get(stage) or {}).get("refs") or {}
    changed = []
    for path, fp in recorded.items():
        cur = fingerprint(path)
        if cur is not None and cur != fp:  # 内容变了 → 过期；文件消失不误报
            changed.append(Path(path).name)
    return changed


def sweep(project) -> list[tuple[dict, list[str]]]:
    """全章节扫过期：[(shot, 变化的设定图名), ...]（仅有效镜）。"""
    out = []
    for s in project.data.get("shots") or []:
        from .review import is_omitted
        if is_omitted(s):
            continue
        changed = stale_refs(s, "image")
        if changed:
            out.append((s, changed))
    return out


def mark_stale(project) -> tuple[int, int]:
    """把过期镜标「已过期需重生成」：未锁定 → 置 retake（下次运行自动重生+归档）；
    已通过锁定(done) → 只挂标记/点数，等人决定是否解锁。
    返回 (置retake数, 仅标记数)。调用方负责 project.save()。

    两条边一起扫：设定图→image（`sweep`），与画面基准图→clip——片段以分镜图
    （降级路线下是场景基准图）为首帧/画面参考，图换底片段即过期；缺后一条边，
    `gen-image --force` 换图后 `gen-video` 会对旧片段静默跳过。
    """
    from . import review
    n_retake = n_flag = 0
    for s, changed in sweep(project):
        s["stale_refs"] = changed
        if review.is_locked(s, "image"):
            n_flag += 1
            continue
        # note 会被 `prompts.video_prompt` 编译进下一版**提示词**（「本次修正重点」），
        # 所以它是交付文本、不是日志：文件名对模型毫无意义（它看不见 shot_3.png），
        # 只会占 token 并触发 lint 的 `craft_leak`。要看是哪几张变了读 `stale_refs`
        # （上一行刚写好，Studio 的分镜卡就是从那里取的）。
        note = "引用的设定图已更新，请按新设定图重生成以保持一致"
        review.set_state(s, "image", "retake", note=note)
        n_retake += 1
    from .review import is_omitted
    for s in project.data.get("shots") or []:
        if not isinstance(s, dict) or is_omitted(s) or not stale_refs(s, "clip"):
            continue
        outcome = retake_clip_for_image(s)
        if outcome == "locked":
            n_flag += 1
        elif outcome == "retake":
            n_retake += 1
    return n_retake, n_flag


def retake_clip_for_image(shot: dict) -> str | None:
    """分镜图换底后对存量片段的处置：片段以分镜图为首帧/画面参考，图换了片段即
    过期。未锁定置 retake（gen-video 按正常重生+归档）；已通过(done)只交人裁决。
    返回 "retake"（本次置位）/ "already"（早已是重做）/ "locked" / None（无片段）。
    六道换画面的门（生图、宫格点选、素材直供、局部改造、版本回滚、设定图过期
    扫描）共用这一处，不各自判。

    不写重做意见：clip 的意见会被编译进下一版视频提示词，「图换了」对模型零
    信息量，还会盖掉作者自己写的意见。"""
    from . import review
    if not (shot.get("clip") or shot.get("clips")):
        return None
    if review.is_locked(shot, "clip"):
        return "locked"
    if review.needs_retake(shot, "clip"):
        return "already"
    review.set_state(shot, "clip", "retake")
    return "retake"


def clear_stale(shot: dict) -> None:
    """重生成完成后清除过期标记（gen 快照里的新指纹即新基线）。"""
    shot.pop("stale_refs", None)


# ---------------------------------------------------------------------------
# 台词文本血缘
# ---------------------------------------------------------------------------
# 吃台词的产物阶段：TTS 念的就是这段话；native 把它写进视频提示词，
# dubbed 的 ref_audio 由它合成。image 不吃台词，不判。
TEXT_STAGES = ("audio", "clip")


def text_fingerprint(shot: dict) -> str | None:
    """本镜台词文本的指纹（格式同 `fingerprint`）；无台词返回 None。

    取 `voicecast.shot_text`——「这镜要说什么」的全链路单一真源，它同时认得
    `narration` 单段与 `lines[]` 逐句。另算一份文本口径就会出现「改了 lines[]
    而指纹没动」。"""
    from .voicecast import shot_text
    text = shot_text(shot)
    if not text:
        return None
    import hashlib
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def record_text(shot: dict, stage: str) -> None:
    """生成完成后登记本次实际念/写进提示词的台词指纹（挂进 gen 快照）。"""
    fp = text_fingerprint(shot)
    if fp:
        shot.setdefault("gen", {}).setdefault(stage, {})["text_fp"] = fp


def stale_text(shot: dict, stage: str) -> bool:
    """该镜该阶段的产物，自生成以来台词是否已改。

    台词被整段删空时按**无从判定**处理（与设定图那条「文件消失不误报」同制）：
    旧产物此时是不再被使用，不是内容不符——`narration_parts` 对未配音镜按窗口
    占静音是常态，把它判成过期会留下一个 tts 永远清不掉的 retake。"""
    recorded = ((shot.get("gen") or {}).get(stage) or {}).get("text_fp")
    if not recorded:
        return False               # 旧数据尚无登记 → 无从判定
    cur = text_fingerprint(shot)
    return cur is not None and cur != recorded


def text_sweep(project) -> list[tuple[dict, list[str]]]:
    """全章节扫台词过期：[(shot, 过期阶段名列表), ...]（仅有效镜）。"""
    from .review import is_omitted
    out = []
    for s in project.data.get("shots") or []:
        if not isinstance(s, dict) or is_omitted(s):
            continue
        stages = [st for st in TEXT_STAGES if stale_text(s, st)]
        if stages:
            out.append((s, stages))
    return out


def mark_text_stale(project) -> tuple[int, int]:
    """把台词已改的镜标出来。返回 (置retake数, 仅标记数)。调用方负责 project.save()。

    分级与设定图那条一致：未锁定 → 置 retake；已通过锁定(done) → 引擎不动它，
    等人裁决。计数按**镜**而非按阶段——一镜的 audio 与 clip 同时过期是常态
    （同一段台词喂两个产物），按阶段计会把镜数报成两倍。

    **不落过期标记字段**（设定图那条有 `stale_refs`，这里没有对应物）：那边必须
    落盘是因为判定要读设定图文件算哈希，只读扫描承担不起；台词哈希不碰磁盘，
    `stale_text()` 随时能重算，再存一份就是同一事实的第二个真源，而且只有
    `lineage mark` 会写它——Studio 里改完台词直到有人跑一次 CLI 才看得见标记，
    恰好错过最需要它的时候。`gen.<阶段>.text_fp` 就是基线，重生成时被覆写，
    判定自然回到干净。

    **不写重做意见**，理由两条：clip 的意见会被 `prompts.video_prompt` 编译进
    下一版视频提示词（「本次修正重点」），而「台词改了」对模型毫无信息量——
    新台词本来就在同一条提示词里，多发一句只是按秒计费的噪声；且 `set_state`
    在不给新意见时保留旧的重做意见，写了反而会盖掉作者自己写的那条。"""
    from . import review
    n_retake = n_flag = 0
    for s, stages in text_sweep(project):
        retook = False
        for stage in stages:
            if review.is_locked(s, stage):
                continue
            review.set_state(s, stage, "retake")
            retook = True
        n_retake += retook
        n_flag += not retook
    return n_retake, n_flag


def rebaseline(project, shot: dict, stage: str = "image") -> list[str]:
    """**人直接落地一版新产物**时重设血缘基线：按当前必需设定图记指纹 + 清过期标记。
    返回记进基线的设定图文件名（供调用方打印）。

    与 `record_refs`+`clear_stale`（生成路径）分工：那一对记的是「这次生成实际喂进去
    的参考图」，此处记的是「此刻这一镜依赖的设定图长什么样」——素材直供与 previz
    首帧登记拿不到前者（图是人在引擎外做的），后者同样是一条成立的基线。

    记基线与清标记必须同时做：只清不记 → 这一镜从此没有 refs 快照，`stale_refs`
    恒返回空，设定图再改多少版都不报警（隐性失去血缘）；只记不清 → 卡片上的
    「⚠ 设定已更新」只能靠再走一次 API 生成才擦得掉。

    **不给 `refine` 局部改造用**：那是拿旧图改一块矩形、输入侧设定图没有重新进过场，
    图并没有因此符合新设定；它要做的只是别把已有的 `refs` 抹掉。
    """
    refs = {}
    for r in required_refs(project, shot):
        p = r.get("path")
        fp = fingerprint(p)
        if fp:
            refs[str(p)] = fp
    if refs:
        shot.setdefault("gen", {}).setdefault(stage, {})["refs"] = refs
    clear_stale(shot)
    return [Path(p).name for p in refs]
