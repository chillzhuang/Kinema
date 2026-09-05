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

"""project.json 读写与 checkpoint。

project.json 是贯穿「主题→成片」全流程的数据契约（schema 见
docs/kinema/project.schema.json）。Skill 层生成并填充 script/style/shots，
执行引擎逐阶段回填 image/audio/output 与 cost，并用 shots[].status 支持断点续跑。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .errors import KinemaError, ProjectError


def aspect_tag(aspect: str) -> str:
    """把比例转成可用于文件名的安全标签，如 9:16 → 9x16。"""
    return aspect.replace(":", "x").replace("/", "x")


# 默认主比例的**唯一真源**（schema 契约同口径：默认横屏，竖屏/方形须用户点名）。
# 历史事故：各处兜底字面量分叉（16:9/9:16 各写一份）+ models.yaml 一个无人消费的
# defaults.aspect: 9:16 死键，误导指挥层把未指定比例的项目建成了竖屏。
# 兜底一律引用本常量，不得再写字面量（源级守卫见 test_workspace 的 aspect 单源扫描）。
DEFAULT_ASPECT = "16:9"


# 引擎长任务运行期间，Studio 可能并发写入的"人类表态"字段——
# 引擎 save 前按加载基线做三方合并，人工点的 done/retake/omt 与评论不会被
# 引擎的旧内存副本静默回滚。表态之外的编辑不进合并面：它们经章节操作锁与
# 任务串行（locking.op_lock），不做三方合并器。
# **两层各自登记**：文档级字段进 _DOC_HUMAN_KEYS，只在分镜层存在的字段进 _SHOT_HUMAN_KEYS。
# `audio_script`/`audio_mode` 同理：整章音频剧本一跑数分钟，期间网页的存稿与
# 路线切换先落盘——不登记的话 score 任务成功收尾那一刻会把它们静默抹掉
_DOC_HUMAN_KEYS = ("review", "comments", "decisions", "audio_script", "audio_mode")
# **追加型**文档字段（append-only 审计日志）：合并时不做整键替换，而是**按 id 取并集**。
# 整键替换对 decisions 是错的——引擎内存副本与磁盘各自 append 过时必然丢掉一边，
# 「按 id 追加去重」这句承诺只有落在合并层才成立（写入层看不见另一侧）。
_DOC_APPEND_KEYS = ("decisions",)
# 逐镜还要保护**版本栈 `versions`**：gen-image 子进程重生时归档旧版（append，只增不减）
# 与 Studio 的表态 save 并发——若 Studio 用重生前的旧内存 save，版本 append 会被静默回滚，
# 分镜卡徽章就丢了「vN」。三方合并同走「磁盘为准」（磁盘上更多的版本必是引擎新归档）。
# `consistency` 同理（M7 角色跨镜一致性判定）：它由**人/指挥层**在长任务运行期间用
# `consistency set` 落盘，不登记的话，正在跑的 gen-image 一 save 就把判定静默抹掉。
# `guide` 同理（previz/sketch 互斥仲裁表态）：Studio 的切换按钮与
# `sketch use` 都可能在 gen-* 后台任务跑着时落盘，是典型的"人在长任务期间的表态"。
# `voice_stale`/`voice_stale_prev` 同理（音色血缘）：换一把声音是编辑期动作，
# 可能落在 gen-image/tts 跑着的时候——不登记的话，那个长任务一 save 就把
# 「这几镜的配音出自已换掉的音色」整块抹掉，而它正是要人去裁决的那件事。
# `voice_clip_stale`/`voice_clip_stale_prev` 是同一条边的片段侧（native 对白镜的
# 人声由模型念出，过期留痕在片段而不在配音上），写入时机与并发形态完全相同。
# `face_visibility` 同理（近景人脸预判）：典型写入时刻正是 gen-video 跑着或
# 刚被人脸拒之后回头标注。
_SHOT_HUMAN_KEYS = ("review", "comments", "versions", "consistency", "guide",
                    "voice_stale", "voice_stale_prev", "voice_clip_stale",
                    "voice_clip_stale_prev", "face_visibility")


def _human_state(data: dict) -> dict:
    """抽取合并相关字段的深拷贝快照：文档级 _DOC_HUMAN_KEYS + 逐镜 _SHOT_HUMAN_KEYS。"""
    snap = {"_doc": {k: copy.deepcopy(data.get(k)) for k in _DOC_HUMAN_KEYS}}
    for s in data.get("shots") or []:
        snap[str(s.get("id"))] = {k: copy.deepcopy(s.get(k)) for k in _SHOT_HUMAN_KEYS}
    return snap


# motion 别名归一的**唯一真源**（storage/variation/scanner 一律 import，绝不再抄）：
# a/b/c 是 CLI 简写（-m a|b|c）。
_MOTION_MAP = {"a": "kenburns", "b": "native", "c": "dubbed"}


def normalize_motion(value) -> str:
    """别名归一。空值不在此补缺省——未表态章节的档位由 `effective_motion` 按内容推导。"""
    m = str(value or "")
    return _MOTION_MAP.get(m, m)


# 章节标题里的序号形态。序号只归 `chapter.id/order` 与封面排版，标题是裸剧情短标题；
# 前缀（第N章：X / 卷二 X / Episode 3 X）与后缀（X·第N集）都算命中。
_TITLE_NUM = r"[一二三四五六七八九十百零〇\d]+"
CHAPTER_NUMBER_RE = re.compile(
    rf"^\s*(第\s*{_TITLE_NUM}\s*[章集回话卷部]|卷\s*{_TITLE_NUM}|(?:chapter|episode|ep|part)\s*\d+)(?=\s|[:：·\-—–、,，]|$)"
    rf"|(第\s*{_TITLE_NUM}\s*[章集回话卷部])\s*$",
    re.I)


def chapter_title_number(title) -> str | None:
    """标题里命中的序号片段（`第二章`、`Episode 3`…），没有则 None。
    `variation` 的 `chapter_title_numbered` 维度与 `chapter new` 的提醒共用。"""
    m = CHAPTER_NUMBER_RE.search(str(title or ""))
    return (m.group(1) or m.group(2)).strip() if m else None


# 渲染模式全集（展示表/export.motion_zh 对拍守卫的真源）。别名表 _MOTION_MAP 的
# values 是全集的子集：退役别名与单字母简写都映射到在册模式。
MOTIONS = ("kenburns", "native", "dubbed")


def default_motion(data: dict | None) -> str:
    """未表态章节的渲染档，按内容推导：`audio_mode=scored` 落 native（整轨人声与对口型
    互斥）；章内任一正镜有对白落 native（对白上镜由模型自声，口型与音色同源）；
    其余（全旁白、无词）落 dubbed（固定音色旁白烧录、闭唇出片）。判据与 lint
    `dubbed_dialogue` 同一谓词 `voicecast.voice_kind`。kenburns 不作缺省：静图形态
    必须显式写 motion。"""
    data = data or {}
    if scored_audio(data):
        return "native"
    from .review import is_omitted
    from .voicecast import voice_kind
    for s in data.get("shots") or []:
        if isinstance(s, dict) and not is_omitted(s) and voice_kind(s) == "dialogue":
            return "native"
    return "dubbed"


def effective_motion(data: dict | None) -> str:
    """章节生效的渲染档：已表态按表态（别名归一），未表态按 `default_motion`。
    全部读侧（`Project.motion`、`uses_seedance`、scanner、lint、库索引）只经此处，
    未表态章节在任何入口都得到同一个档位。"""
    m = (data or {}).get("motion")
    return normalize_motion(m) if m else default_motion(data)


def uses_seedance(data: dict | None) -> bool:
    """dubbed/native 判据的纯函数形态（`Project.uses_seedance` 属性转调本函数）。

    scanner/lint 这类手头只有 dict 的场景直接调用——为读一个字符串构造 Project
    会触发 `_human_state` 对全部镜版本栈的深拷贝，看板一开就是数千次。"""
    return effective_motion(data) in ("dubbed", "native")


def scored_audio(data: dict | None) -> bool:
    """audio_mode=scored 判据的纯函数形态（`Project.audio_mode`/`scored_audio` 转调）。

    与 `uses_seedance` 同制度：scanner/lint 直接用 dict 判，不构造 Project。"""
    return str((data or {}).get("audio_mode") or "") == "scored"


def effective_audio_mode(data: dict | None) -> str:
    """音频路线：`audio_mode=scored` 之外一律 tracks（`Project.audio_mode` 转调）。"""
    return "scored" if scored_audio(data) else "tracks"


# 章级布尔开关里只有音色锚定缺省为开：native 章节默认按选角嗓音开口
_FLAG_DEFAULTS = {"voice_anchor": True}


def chapter_flag(data: dict | None, name: str) -> bool:
    """章级布尔开关的生效值：缺席或 null 按引擎缺省，其余按布尔真值。
    读侧与 Gateway 的失效判定共用，缺省值只在这里有一份。"""
    value = (data or {}).get(name)
    return _FLAG_DEFAULTS.get(name, False) if value is None else bool(value)


class Project:
    # 运行时覆盖的哨兵：区分"磁盘上原本没有这个键"与"原值是 None"
    _ABSENT = object()

    def __init__(self, path: str | Path, data: dict):
        self.path = Path(path).resolve()
        self.data = data
        self._human_baseline = _human_state(data)
        self._runtime: dict = {}     # 运行时覆盖的原值快照（见 override_runtime）

    def override_runtime(self, key: str, value) -> None:
        """**一次性运行时覆盖**：改内存供本次渲染用，`save()` 写盘时还原成磁盘原值。

        `--motion/--aspect/--effects` 这类 flag 表达的是"这一次这么跑"，绝不是
        "把章节改成这样"。失败形态：`assemble --kenburns`（这次想看静图版）经
        `stage_compose` 收尾的 `project.save()` 把 native 章节的 motion **永久**
        改成 kenburns——此后 gen-video 拒发、片段音轨也不再被采用，而全程零提示。
        """
        if key not in self._runtime:
            self._runtime[key] = self.data.get(key, self._ABSENT)
        self.data[key] = value

    def runtime_overridden(self, key: str) -> bool:
        return key in self._runtime

    def commit_runtime(self, key: str) -> None:
        """把运行时覆盖值升格为正式值：随后的 `save()` 不再还原它。"""
        self._runtime.pop(key, None)

    @property
    def motion_declared(self) -> bool:
        """章节文档是否对渲染档表过态（磁盘上有 `motion` 键）。运行时覆盖不算表态。"""
        return "motion" in self.data \
            and self._runtime.get("motion", None) is not self._ABSENT

    # ---- 读写 ----
    @classmethod
    def load(cls, path: str | Path) -> "Project":
        p = Path(path)
        if not p.is_file():
            raise ProjectError(f"找不到 project 文件: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ProjectError(f"project.json 不是合法 JSON: {e}") from e
        return cls(p, data)

    def save(self) -> None:
        """写盘全程持文档写锁：合并所基于的磁盘状态在原子写完成前
        不会被其他写者（Studio 表态 / Gateway apply / 并行 CLI）替换。"""
        from .locking import save_lock
        with save_lock(self.path):
            self._merge_human_edits()
            self._write()

    @classmethod
    def mutate(cls, path, fn, *, retries: int = 3):
        """以磁盘最新内容应用小变更并落盘，返回 fn 的返回值（None 时返回 Project）。

        表态类端点的正确基线是磁盘现状而非调用方内存副本：端点若持有整份
        旧副本再 save，会把引擎长任务刚回填的产物字段一并写回旧值。锁内校验
        读取基线未变才提交，变了以新基线重放 fn。"""
        from .locking import save_lock
        p = Path(path)
        for _ in range(retries):
            raw = p.read_text(encoding="utf-8")
            project = cls(p, json.loads(raw))
            result = fn(project)
            with save_lock(p):
                if p.read_text(encoding="utf-8") != raw:
                    continue
                project._write()
            return result if result is not None else project
        raise KinemaError(f"文档写入竞争持续存在，变更未提交（已重试 {retries} 次）: {p}")

    def _write(self) -> None:
        # 运行时覆盖只作用于本次渲染：写盘前还原磁盘原值，写完再放回内存
        # （后续阶段仍要按覆盖值跑）——顺序不能反，notify_saved 也吃还原后的那份
        live = {k: self.data.get(k, self._ABSENT) for k in self._runtime}
        for k, orig in self._runtime.items():
            if orig is self._ABSENT:
                self.data.pop(k, None)
            else:
                self.data[k] = orig
        try:
            # 原子写与存储层同源：Studio 线程与 spawn_cli 子进程并发读写同一份
            # 文档，半截文件会被读端以「不存在」处理进而丢更新
            from .storage import atomic_write_json
            atomic_write_json(self.path, self.data)
            self._human_baseline = _human_state(self.data)
            # 持久化钩子：mysql 模式下工作区章节同步 upsert 入库（逐镜 checkpoint 即持久化）
            # **必须在还原窗口内**：库行与本地文件写的是同一份，否则 mysql 模式下
            # 覆盖值照样入库、下次 load 又被"新者赢"拉回来
            try:
                from .storage import notify_saved
                notify_saved(self.path, self.data)
            except Exception as e:  # noqa: BLE001  库不可用时保留本地文件并明确提示
                print(f"  ⚠ 数据库同步失败（本地文件已保存）: {e}")
        finally:
            for k, v in live.items():          # 覆盖值放回内存：后续阶段仍按它跑
                if v is self._ABSENT:
                    self.data.pop(k, None)
                else:
                    self.data[k] = v

    def _merge_human_edits(self) -> None:
        """写盘前合并磁盘上的人类表态（review/comments）。

        引擎长任务（gen-image/gen-video/tts）全程持有整份文档的内存副本并
        逐镜 save；同期用户在 Studio 点的 done/retake/omt/评论已先落盘。
        规则：某镜的 review/comments 相对**加载基线**在磁盘上变了 → 磁盘为准
        （人是主管）；否则保留引擎内存值（引擎自己的 wfa/retake 流转不受影响）。
        双方同时变更时以人工表态为准并打印提示。

        `_DOC_APPEND_KEYS`（decisions 审计日志）走另一条规则：**按 id 取并集**——
        它是 append-only 的，两侧各追加过时"磁盘为准"会把内存侧那条丢掉。"""
        base = getattr(self, "_human_baseline", None)
        if base is None or not self.path.is_file():
            return
        try:
            disk = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return                       # 磁盘副本在装载后被移走：没有可合并的表态
        disk_snap = _human_state(disk)
        for k in _DOC_HUMAN_KEYS:  # 文档级（章节 review / 章节评论 / 决策审计）
            if k in _DOC_APPEND_KEYS:   # 追加型：两侧按 id 取并集，谁都不丢
                from .decisions import union_by_id
                merged = union_by_id(self.data.get(k), disk_snap["_doc"].get(k))
                if merged:
                    self.data[k] = merged
                else:
                    self.data.pop(k, None)
                continue
            if disk_snap["_doc"].get(k) != base["_doc"].get(k):
                if disk_snap["_doc"].get(k) is None:
                    self.data.pop(k, None)
                else:
                    self.data[k] = disk_snap["_doc"][k]
        shots_by_id = {str(s.get("id")): s for s in self.data.get("shots") or []}
        for sid, dvals in disk_snap.items():
            if sid == "_doc":
                continue
            tgt = shots_by_id.get(sid)
            if tgt is None:
                continue
            bvals = base.get(sid) or {}
            for k in _SHOT_HUMAN_KEYS:
                if dvals.get(k) == bvals.get(k):
                    continue      # 磁盘没动这项 → 引擎内存值照写
                if k != "versions" and tgt.get(k) not in (bvals.get(k), dvals.get(k)):
                    print(f"  ⚠ 镜 {sid} 的 {k} 引擎与 Studio 同时变更——按人工表态为准")
                if dvals.get(k) is None:
                    tgt.pop(k, None)
                else:
                    tgt[k] = dvals[k]

    # ---- 工作目录 ----
    @property
    def workdir(self) -> Path:
        d = self.path.parent / f"{self.path.stem}_work"
        return d

    @property
    def exports_dir(self) -> Path:
        """导出专用目录（浅层好找）：标准工作区 = project/<项目>/exports/；
        散装 --project 文件回落 <工作目录>/exports/。审阅包/交付包统一落这里。"""
        wd = self.workdir
        base = (wd.parent.parent / "exports"
                if wd.parent.name == "chapters" else wd / "exports")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def subdir(self, name: str) -> Path:
        d = self.workdir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- 便捷访问 ----
    @property
    def id(self) -> str:
        return self.data.get("id") or self.path.stem

    @property
    def aspect(self) -> str:
        """主比例（默认 16:9 横屏），用于非逐比例出图时的画布尺寸与默认单输出。"""
        return self.data.get("aspect", DEFAULT_ASPECT)

    @property
    def aspects(self) -> list[str]:
        """要输出的比例列表（默认只出主比例）。竖屏/横屏/两者都要在此控制。"""
        a = self.data.get("aspects")
        return list(a) if a else [self.aspect]

    @property
    def image_per_aspect(self) -> bool:
        """是否为每个比例单独出图（画质最佳、成本翻倍）；否则出一套主比例图、其余用 Ken Burns 重构图。"""
        return bool(self.data.get("image_per_aspect", False))

    def image_for(self, shot: dict, aspect: str) -> str | None:
        """取某镜在某比例下的图像：优先逐比例图，回退主图。
        已上云（URL）时自动拉回本地（渲染永远吃本地文件）。"""
        val = (shot.get("images") or {}).get(aspect) or shot.get("image")
        if not val:
            return None
        from .storage.media import ensure_local
        return ensure_local(val)

    @property
    def motion(self) -> str:
        """渲染执行档位。对外的形态只有两个——A 静图+烧录、B 视频+模型声，
        本档位是 B 内变体与 A 的落地选择：
          · kenburns —— A：静图 Ken Burns 运镜 + TTS 烧录（零视频成本，须显式写）
          · native   —— B 本体：Seedance 原生音画，**对白上镜的内容一律选它**
                        （配音色锚定）；`native_voiceover` 开关叠出混排变体
          · dubbed   —— B·解说变体：**只用于全旁白章**（闭唇出片、TTS 旁白烧录）
                        ——对白上镜时烧录轨与模型口型两条时间轴不同源，必然失配
                        （lint `dubbed_dialogue` 点名）
        未表态章节按内容定档（`default_motion`）。本值只有章级一个入口（`shots[]`
        无同名覆盖），声源随之是章级制式：同一说话人整章单一声源。别名 a/b/c 见
        `_MOTION_MAP`。"""
        return effective_motion(self.data)

    @property
    def uses_seedance(self) -> bool:
        """dubbed / native 都走 Seedance 图生视频（真动态）。判据实体是模块级
        纯函数 `uses_seedance(data)`——dict 消费方与本属性共用同一份。"""
        return uses_seedance(self.data)

    @property
    def native_audio(self) -> bool:
        """native 模式：音频由 Seedance 原生生成（不叠加我们的旁白）。"""
        return self.motion == "native"

    @property
    def native_voiceover(self) -> bool:
        """native 是否把我们的固定音色配音**烧进**成片（缺省 False = 不烧）。

        native 的片段自带模型原生人声/对白，再叠一层 TTS 就是同一句话两个人在说。
        **不做「盘上有 narration.wav 就自动混烧」的零开关**——被点名的失效
        形态：章节原本是 kenburns/dubbed（这两种模式 tts 是标配），后来切成
        native，narration.wav 原样留在盘上（切 motion 不清它），零开关会照烧不误；
        compose 还会先 `_sync_narration` 把这条陈旧旁白按当前时间轴重拼对齐，
        于是它"跟画面对得上"，只是凭空多一层人声，全程零提示。

        混烧是显式 opt-in：要「固定音色旁白 + 模型原生环境音」的纪录片式混烧，
        `assemble --burn-voice`（本次）或项目/章节写 `native_voiceover: true`（常开）。
        运行时覆盖走 `override_runtime`，绝不落盘。"""
        return bool(self.data.get("native_voiceover", False))

    @property
    def audio_mode(self) -> str:
        """音频路线，与 `motion`（画面路线）正交：

        · `tracks`（缺省）——逐镜 TTS + BGM + 音效三轨，由 `mixdown` 确定性混音。
        · `scored`——整段音频剧本交给音频模型，**它自己把人声、音乐、音效混好**，
          回来的是一条成品轨。合成段因此不再叠 BGM、不做闪避。

        剧本写在章节顶层 `audio_script`，由指挥层按 `kinema-audio` 撰写——引擎内没有
        LLM，绝不从分镜自动生成剧本（与 `sketch.beats` 同制度）。"""
        return effective_audio_mode(self.data)

    @property
    def scored_audio(self) -> bool:
        return scored_audio(self.data)

    @property
    def needs_tts(self) -> bool:
        """kenburns/dubbed 需要我们的固定音色 TTS；native 不需要（模型自配音）。

        `scored` 下一句都不配：人声由音频模型随音乐音效一起生成，再叠逐镜 TTS
        就是同一句台词两个人说——与 native 不叠 TTS 是同一个道理。"""
        return not self.scored_audio and self.motion in ("kenburns", "dubbed")

    @property
    def needs_narration_track(self) -> bool:
        """成片主音轨上的人声是否由我们的固定音色 TTS 产出（**章级**口径）。

        与 `needs_tts` 的分工是量纲：那个属性问的是「**这一镜**必须有 audio
        产物」（合成前审阅闸 `cli._assemble_review_gate` 恒与
        `voicecast.shot_text(s)` 配对使用）；native 混烧的对白镜由模型原生发声，
        按 `voicecast.in_narration_track` 永远拿不到 audio 产物，并进那个口径
        即恒缺、审阅闸随即永久拦死。本属性只回答「这一章要不要产出旁白轨」。

        `scored` 下人声随音乐音效由音频模型混在一条轨里，旁白轨不参与合成。"""
        if self.scored_audio:
            return False
        return self.needs_tts or (self.native_audio and self.native_voiceover)

    @property
    def frame_chain(self) -> bool:
        """首尾帧衔接是否**生效**（已含模式判据，调用方不必再 `and native`）。

        规则与缺省值都在 `pipeline.framechain.active` —— Studio 章节视图读同一个函数，
        判据分家会让页面上的标记与实际发出去的请求对不上。
        """
        from .pipeline import framechain
        return framechain.active(self.data, self.motion)

    @property
    def anchor_frame(self) -> bool:
        """首帧锚定是否**章级**生效（已含模式判据，调用方不必再 `and native`）。

        规则与代价都在 `pipeline.anchorframe.active`；镜级表态走同模块的
        `anchored`，渲染侧统一从那里取，本属性只服务章级读点。
        """
        from .pipeline import anchorframe
        return anchorframe.active(self.data, self.motion)

    def clip_for(self, shot: dict, aspect: str) -> str | None:
        """取某镜在某比例下的图生视频片段（dubbed/native）：优先逐比例片段，回退主片段；无则 None。
        已上云（URL）时自动拉回本地。"""
        val = (shot.get("clips") or {}).get(aspect) or shot.get("clip")
        if not val:
            return None
        from .storage.media import ensure_local
        return ensure_local(val)

    @property
    def profile(self) -> str | None:
        """项目整体风格档（None 表示用配置默认）。逐镜可用 shots[].profile 覆盖。"""
        return self.data.get("profile")

    @property
    def effects(self) -> list | None:
        """项目特效覆盖（None 表示用 profile 的默认特效）。"""
        return self.data.get("effects")

    @property
    def shots(self) -> list[dict]:
        shots = self.data.get("shots")
        if not shots:
            raise ProjectError(
                "project 缺少 shots（分镜）。请先由 Skill 层完成阶段3 分镜切分。"
            )
        return shots

    @property
    def active_shots(self) -> list[dict]:
        """进入生产/时间轴的分镜 = 全部 - 弃用(omt)。渲染各阶段与合成用它。"""
        from .review import is_omitted
        active = [s for s in self.shots if not is_omitted(s)]
        if not active:
            raise ProjectError("所有分镜都已弃用(omt)，无可渲染内容。")
        return active

    @property
    def style(self) -> dict:
        return self.data.setdefault("style", {})

    @property
    def scene(self) -> str:
        """固定场景描述块：同一段对话所有分镜共用的背景/环境/道具/光线。

        写在 project 顶层 `scene` 或 style.scene。所有镜的 image_prompt 都会前置它，
        使背景一致；各镜只描述机位/角色动作，不重写场景，避免"对话中场景乱切"。
        """
        return (self.data.get("scene") or self.style.get("scene") or "").strip()

    @property
    def scene_ref_lock(self) -> bool:
        """首镜整图锚定（强锁）：把首镜生成图当作后续每镜的参考图。

        ⚠ 会连人物构图/姿势一起复刻，导致对话类各镜姿势雷同、缺乏表演。
        默认关闭，仅用于**静态场景**（如固定背景+下雨/下雪，人物基本不动）。
        对话/叙事类靠「固定 scene 文本 + character_block + 固定 seed」保持一致即可，
        无需强锁，这样各镜动作/表情/机位才能随剧情变化。
        开启：project 顶层 `scene_ref_lock: true`。
        """
        return bool(self.data.get("scene_ref_lock", False))

    # ---- 设定集（角色/场景/道具设定图，跨镜强一致的根基）----
    @property
    def characters(self) -> list[dict]:
        """角色设定（继承自项目）：{name, appearance, outfit, hair, weapon, sheet(设定图路径), voice, role}。"""
        return self.data.get("characters") or []

    @property
    def props(self) -> list[dict]:
        """道具/武器设定：{name, desc, sheet}。"""
        return self.data.get("props") or []

    @property
    def scene_ref(self) -> str | None:
        """场景设定图路径（有则所有镜以它统一场景）。"""
        return self.data.get("scene_ref")

    @property
    def skip_design(self) -> bool:
        """跳过设定集：不用角色/场景设定图，退回首镜锚定。"""
        return bool(self.data.get("skip_design", False))

    @property
    def outline(self) -> str:
        """本章叙事大纲（剧本改编 `adapt scaffold` 从系列 episodes[] 写入的派生缓存，
        节点①照它拆 shots）。非改编项目为空，不影响任何渲染。"""
        return self.data.get("outline") or ""

    @property
    def has_design(self) -> bool:
        """是否已有可用的设定集（场景/角色/道具任一设定图存在）。skip_design 时视为无。"""
        if self.skip_design:
            return False
        from .pipeline.checkpoint import has_file
        if has_file(self.scene_ref):
            return True
        if any(has_file(c.get("sheet")) for c in self.characters):
            return True
        return any(has_file(p.get("sheet")) for p in self.props)

    @property
    def scenes(self) -> list[dict]:
        """具名场景（取景地）设定：{name, desc, keywords, sheet}。

        与顶层 `scene`/`scene_ref`（全片同一个地方时的**全局**基准图）是两个概念：
        这里是「这部戏会去的一个个地方」，按 keywords 命中逐镜挂载。
        没有这一档时，场景只能塞进 props[] 冒充道具，于是套上道具的物件版式。"""
        return self.data.get("scenes") or []

    def _matched_entities(self, shot: dict, items: list[dict],
                          explicit_key: str) -> list[dict]:
        """本镜命中的设定实体 = 显式 `shots[<explicit_key>]` ∪ 文本命中。

        道具与具名场景共用这一套（各抄一份的话，两边的
        「≥2 字才算命中 / 只扫本镜语料 / 显式永远命中」三条口径迟早分叉）。"""
        explicit = set(shot.get(explicit_key) or [])
        corpus = " ".join(str(shot.get(k) or "") for k in (
            "narration", "narration_en", "image_prompt",
            "image_prompt_en", "caption", "caption_en"))
        low = corpus.lower()
        out, seen = [], set()
        for it in items:
            name = it.get("name")
            if not name or name in seen:
                continue
            hit = name in explicit
            if not hit:
                terms = ([name] if len(name) >= 2 else []) + list(it.get("keywords") or [])
                hit = any(t and (t in corpus or t.lower() in low) for t in terms)
            if hit:
                seen.add(name)
                out.append(it)
        return out

    def matched_scenes(self, shot: dict) -> list[dict]:
        """本镜应参考的具名场景 = 显式 `shots[].scenes` ∪ 文本命中（与道具同口径）。"""
        return self._matched_entities(shot, self.scenes, "scenes")

    def shot_cast(self, shot: dict) -> tuple[list[dict], bool]:
        """本镜出场角色（**提示词文字锚的取材口径**），返回 (角色列表, 是否全员兜底)。

        三级解析：显式 `shots[].characters`（严格白名单，`[]` 空表=明确无人出场，
        与 design_refs 同语义）> 文本命中（name/keywords，与道具/取景地共用
        `_matched_entities` 三条口径）> 两者皆无 → 全员回落并置 fallback_all=True，
        交调用方按预算裁决（prompts.character_anchor_block）——引擎无从知道谁出场，
        小阵容整块前置无害（策略③的一致性锚），长篇几十人阵容再整块灌就是
        「画一张全员图鉴」的指令。

        **刻意不改 design_refs 的「角色缺省全挂」**：参考图多挂几张只是
        占名额，且有 REF_BASE 契约句声明「未出现者不画」兜底；文字锚多灌一个名字
        却是明确的「把他画进来」指令，两边风险不对称，故取材口径分开。"""
        names = shot.get("characters")
        if names is not None:
            by_name = {c.get("name"): c for c in self.characters if c.get("name")}
            return [by_name[n] for n in names if n in by_name], False
        hit = self._matched_entities(shot, self.characters, "characters")
        if hit:
            return hit, False
        return list(self.characters), True

    def matched_props(self, shot: dict) -> list[dict]:
        """本镜应参考的道具设定 = 显式 `shots[].props` ∪ 文本命中的道具。

        文本命中：道具 `name`（≥2 字，避免「刀」「杯」单字泛匹配）或其
        `keywords` 别名，出现在本镜 image_prompt/_en + narration/_en +
        caption/_en 语料里即算命中。**只扫本镜语料**（不含全局 scene），
        以免把只在场景描述里出现的常驻道具灌进每一镜。显式 `shots[].props`
        永远命中（覆盖/白名单，含短名道具）。

        这是设定图一致性的单一真源——`design_refs`（参考图装配）与
        `lineage.required_refs`（就绪度护栏）都调它，两条链口径永不分叉。"""
        return self._matched_entities(shot, self.props, "props")

    def _assemble_refs(self, shot: dict) -> list[str]:
        """本镜设定图完整清单（未截断）：全局场景 → 命中取景地 → 出场角色 → 命中道具，去重。
        `design_refs` 取其前 8 张；`design_ref_overflow` 是被 8 张上限截断的余量。
        skip_design 项目不走设定集 → 空（与 has_design / required_refs 同口径）。"""
        if self.skip_design:
            return []
        from .storage.media import ensure_local

        def _ok(v):
            if not v:
                return None
            v = ensure_local(v)
            return v if Path(v).is_file() else None

        refs: list[str] = []
        named = self.matched_scenes(shot)         # 显式 scenes ∪ 文本命中取景地
        for sc in named:
            sheet = _ok(sc.get("sheet"))
            if sheet:
                refs.append(sheet)
        # 全局固定场景只在本镜没有具名场景时挂载（与 `lineage._primary_scene` 同一仲裁）
        sref = _ok(self.scene_ref) if not named else None
        if sref:
            refs.append(sref)
        names = shot.get("characters")            # 显式出场角色；None=全部
        for c in self.characters:
            sheet = _ok(c.get("sheet"))
            if sheet and (names is None or c.get("name") in names):
                refs.append(sheet)
        for p in self.matched_props(shot):        # 显式 props ∪ 文本命中道具
            sheet = _ok(p.get("sheet"))
            if sheet:
                refs.append(sheet)
        seen, out = set(), []
        for r in refs:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out

    def character_sheet_refs(self, shot: dict) -> list[str]:
        """本镜出场角色的设定图（**仅角色档**）——`ref_kind="character"` 的图像
        provider（subject_reference 一类）只能吃这一类：设定集清单的顺序是
        场景→取景地→角色→道具，盲取首张恒是场景全景图。skip_design 项目为空。"""
        if self.skip_design:
            return []
        from .storage.media import ensure_local
        names = shot.get("characters")
        out: list[str] = []
        for c in self.characters:
            v = c.get("sheet")
            if not v or (names is not None and c.get("name") not in names):
                continue
            v = ensure_local(v)
            if Path(v).is_file():
                out.append(v)
        return out

    def design_refs(self, shot: dict) -> list[str]:
        """该镜要参考的设定图（Seedream 多图参考，限 8 张）：
        场景设定图 + 出场角色设定图（缺省=全部角色）+ 命中道具设定图
        （显式 `shots[].props` ∪ 提示词/旁白里点名命中的道具，见 `matched_props`）。
        设定图已上云时自动拉回本地。"""
        return self._assemble_refs(shot)[:8]

    def design_refs_for_provider(self, shot: dict, max_refs: int = 0) -> tuple[list[str], list[str]]:
        """按 provider 实际参考图上限选择设定图，并返回 (实际引用, 被省略引用)。

        通用 `design_refs()` 保留既有「全局场景→具名场景→角色→道具」口径，供
        lineage 和支持 8 张的图像 provider 使用。原生 agent 图像工具只有 5 张
        参考位时，不能直接截前 N 张：要优先保留本镜具名场景、出场角色和本镜最
        常被提到的道具；如果具名场景存在，则不用全局场景图占位。返回省略
        清单是为了让工单、日志和后续验收与实际请求保持一致。
        """
        full = self._assemble_refs(shot)
        if not max_refs or len(full) <= max_refs:
            return full, []

        from .storage.media import ensure_local

        def local_sheet(value):
            if not value:
                return None
            value = ensure_local(value)
            return value if Path(value).is_file() else None

        def dedupe(values):
            seen, out = set(), []
            for value in values:
                if value and value not in seen:
                    seen.add(value)
                    out.append(value)
            return out

        explicit_scene_names = shot.get("scenes")
        if explicit_scene_names:
            scene_by_name = {sc.get("name"): sc for sc in self.scenes if sc.get("name")}
            scene_rows = [scene_by_name[name] for name in explicit_scene_names
                          if name in scene_by_name]
        else:
            scene_rows = self.matched_scenes(shot)
        named_scene_refs = dedupe(local_sheet(sc.get("sheet")) for sc in scene_rows)
        global_scene_ref = local_sheet(self.scene_ref)
        character_refs = self.character_sheet_refs(shot)
        prop_rows = []
        corpus = " ".join(str(shot.get(k) or "") for k in (
            "image_prompt", "image_prompt_en", "narration", "narration_en",
            "caption", "caption_en"))
        explicit_props = set(shot.get("props") or [])
        for index, prop in enumerate(self.matched_props(shot)):
            ref = local_sheet(prop.get("sheet"))
            if not ref:
                continue
            terms = [str(prop.get("name") or "")] + [str(x) for x in (prop.get("keywords") or [])]
            score = sum(corpus.count(term) for term in terms if term)
            if prop.get("name") in explicit_props:
                score += 100
            prop_rows.append((-score, index, ref))
        prop_refs = [ref for _, _, ref in sorted(prop_rows)]

        ordered = named_scene_refs or ([global_scene_ref] if global_scene_ref else [])
        ordered = dedupe([*ordered, *character_refs, *prop_refs])
        # 保持全量清单中的顺序，避免某个 provider 选择出不属于本镜的路径。
        ordered = [ref for ref in ordered if ref in set(full)]
        selected = ordered[:max_refs]
        omitted = [ref for ref in full if ref not in selected]
        return selected, omitted

    def design_ref_overflow(self, shot: dict) -> list[str]:
        """超过 8 张上限被丢弃的设定图 basename（供 cli 告警，不静默截断）。"""
        return [Path(x).name for x in self._assemble_refs(shot)[8:]]

    @property
    def audio(self) -> dict:
        return self.data.setdefault("audio", {})

    @property
    def voices(self) -> dict:
        """角色音色表：{角色名: 音色别名或 voice_type}。各镜按 speaker 查表定音色。"""
        return self.data.get("voices") or {}

    @property
    def output(self) -> dict:
        return self.data.setdefault("output", {})

    def style_anchor_refs(self) -> list[str]:
        """旧式全局风格锚点：角色设定图 + 风格板（新工程多走设定集/参考库，通常为空）。"""
        from .storage.media import ensure_local
        refs = []
        for key in ("character_ref", "style_board"):
            v = self.style.get(key)
            if v:
                v = ensure_local(v)
                if Path(v).is_file() and v not in refs:
                    refs.append(v)
        return refs

    def moodboard_refs(self, shot: dict | None = None) -> list[str]:
        """本镜生效的风格垫图（参考库）路径，已归一为存在的本地文件：
        - 镜显式带 `refs`（列表，**[] 表示本镜刻意不用垫图**）→ 精确用这一份
          （网页手动勾选/取消、Claude 写 shots[].refs 的落点）；
        - 否则 → 默认生效集 `style.moodboard`（参考库 on=True 项，经 sync 同步而来）。
        这是「垫图默认全局套用、逐镜可覆盖」的单一真源——除非镜级取消或 CLI --no-moodboard。"""
        from .storage.media import ensure_local
        cand = shot["refs"] if (shot is not None and isinstance(shot.get("refs"), list)) \
            else (self.style.get("moodboard") or [])
        out: list[str] = []
        for v in cand:
            v = ensure_local(v)
            if v and Path(v).is_file() and v not in out:
                out.append(v)
        return out

    def ref_images(self, shot: dict | None = None) -> list[str]:
        """一致性参考图：旧式全局锚点（角色设定图/风格板）+ 风格垫图（参考库，逐镜可覆盖）。"""
        refs = self.style_anchor_refs()
        for v in self.moodboard_refs(shot):
            if v not in refs:
                refs.append(v)
        return refs

    # ---- 成本累计 ----
    def add_cost(self, kind: str, amount: float) -> None:
        """累计成本入台账；项目设了 budget（元）时超限即断。

        本笔先入账再抛错——钱已经花了必须记上，抛错只阻止继续烧。
        断点续跑天然衔接：已产出的镜有产物不重生，提额后从中断处继续。

        **额度归一必须与事前闸同源** `budget.limit()`：事前闸（`gen-video` 开火前
        整批预估）与本事后闸对畸形值一旦分叉，就会出现最坏的一种组合——
        事前闸判「没设限」整批放行，事后闸却在第一笔就抛错。历史分叉实例：
        `budget: -5` 时 `if budget:` 为真而 `total > -5` 恒真（首笔即断）、
        `budget: "abc"` 时 `float()` 直接抛裸 ValueError。统一走 limit() 后，
        这两种写坏的值一律等同「不设限」，与事前闸的判断逐字一致。
        **求和同理走 `budget.spent_total`**——事前/事后两道闸同一份实现，
        不给「各自求和、口径悄悄分叉」留面。"""
        from .budget import limit as _budget_limit
        from .budget import spent_total as _spent_total

        cost = self.data.setdefault("cost", {"currency": "CNY"})
        cost[kind] = round(cost.get(kind, 0.0) + amount, 4)
        cap = _budget_limit(self.data.get("budget"))
        if cap is not None:
            total = _spent_total(self.data)
            if total > cap:
                raise KinemaError(
                    f"项目预算已超：budget ¥{cap:.2f}，累计已花 ¥{total:.2f}"
                    f"（本笔 {kind} ¥{amount:.2f} 已入账）。提高章节 budget"
                    "（chapter set --budget）或删除该字段后重跑即从断点续起。")

    # ---- 时间轴（均排除弃用镜） ----
    def total_duration(self) -> float:
        """有效分镜时长之和（合成后视频长度）。"""
        return round(sum(float(s.get("dur", 0)) for s in self.active_shots), 3)

    def timeline(self) -> list[tuple[float, float, dict]]:
        """返回 [(start, end, shot), ...]，start/end 由有效分镜时长累加得到。"""
        t = 0.0
        out = []
        for s in self.active_shots:
            dur = float(s.get("dur", 0))
            out.append((round(t, 3), round(t + dur, 3), s))
            t += dur
        return out
