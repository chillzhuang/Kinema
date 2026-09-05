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

"""工作区 / 项目管理（文档式 CRUD，后端可插拔）。

在"单条视频"之上加两层组织，实现强规划：
  Workspace 工作区
   └─ Project 项目/系列（project.json：总体设计 + 角色预设 + 章节索引）
       └─ Chapter 章节（chapters/<id>.json：一条视频，继承项目的 profile/角色/设计）

持久化走 storage 层（config/storage.yaml）：
  · local（默认）—— JSON 即数据库，零依赖；
  · mysql —— 数据库为唯一真源，本地 JSON 自动同步为渲染工作缓存，媒体只存路径。
章节文件始终是引擎可直接渲染的视频 project.json。
默认工作区目录：仓库根 `project/`（或环境变量 KINEMA_WORKSPACE、CLI --workspace 指定）。
从 `engine/` 启动也必须回到同一个仓库根 `project/`；local 与 mysql 只切换文档
持久化后端，不改变工作区根。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .errors import ProjectError
from .project import DEFAULT_ASPECT
from .storage import get_storage


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slug(title: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or fallback


def _is_source_checkout(path: Path) -> bool:
    """是否为一份完整的 Kinema 源码检出根。"""
    return ((path / "engine" / "kinema").is_dir()
            and (path / "config" / "storage.yaml").is_file())


def _source_checkout_root() -> Path | None:
    """返回当前源码检出的仓库根；找不到时交给 cwd 规则处理。

    ``workspace.py`` 位于 ``<repo>/engine/kinema``。优先识别源码仓库，才能在
    ``engine/`` 下启动时仍把数据落到 ``<repo>/project``，不被一个历史残留的
    ``engine/project`` 目录劫持。
    """
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if _is_source_checkout(candidate):
            return candidate
    return None


def _normalize_workspace_path(path: str | Path) -> Path:
    """把仓库根/engine 根入口归一为仓库根 ``project/`` 数据目录。

    普通自定义目录（例如测试临时目录）保持原语义，仍然直接作为工作区根。
    这样既修正 CLI/Studio 从仓库根传参的常见入口，也不破坏显式隔离工作区。
    """
    raw = Path(path)
    absolute = raw.resolve()
    if _is_source_checkout(absolute):
        return absolute / "project"
    if absolute.name == "engine" and _is_source_checkout(absolute.parent):
        return absolute.parent / "project"
    if (absolute.name == "project" and absolute.parent.name == "engine"
            and _is_source_checkout(absolute.parent.parent)):
        return absolute.parent.parent / "project"
    return raw


def find_workspace(explicit: str | None = None) -> Path:
    """工作区（项目库）根目录：默认仓库根 ``project/``（可用 --workspace /
    KINEMA_WORKSPACE 覆盖）。

    ``--workspace``/``KINEMA_WORKSPACE`` 可传数据根，也可传源码仓库根或 ``engine/``；
    后两者确定性归一到仓库根 ``project/``。所有生成产物都落在
    ``project/<项目>/`` 内；未指定项目时用 project/demo。
    """
    if explicit:
        return _normalize_workspace_path(explicit)
    env = os.environ.get("KINEMA_WORKSPACE")
    if env:
        return _normalize_workspace_path(env)
    checkout = _source_checkout_root()
    if checkout:
        return checkout / "project"
    # 从 cwd 向上找已存在的 project/；找不到则用 cwd/project
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "project").is_dir():
            return d / "project"
    return Path("project")


# ----------------------------------------------------------------------------
class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store = get_storage(self.root)

    @classmethod
    def open(cls, explicit: str | None = None, *, create: bool = True) -> "Workspace":
        root = find_workspace(explicit)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return cls(root)

    def _pdir(self, pid: str) -> Path:
        return self.root / pid

    def exists(self, pid: str) -> bool:
        # 含已逻辑删除的项目——id 占用判定必须看全量，防重名"复活"撞目录
        return self.store.project_exists(pid)

    def list_projects(self, *, include_deleted: bool = False) -> list[dict]:
        """项目清单——软删过滤的**单点**：全部消费方（CLI list / Studio 聚合 /
        成本页）都经这里，is_deleted=1 的项目默认不出现。"""
        rows = self.store.list_projects()
        if include_deleted:
            return rows
        return [p for p in rows if not int(p.get("is_deleted") or 0)]

    def _unique_id(self, base: str) -> str:
        pid, i = base, 2
        while self.exists(pid):
            pid, i = f"{base}-{i}", i + 1
        return pid

    def create_project(self, title, *, theme="", profile="narration",
                        platform=None, aspect=None, pid=None,
                        template=None, skill=None) -> "Series":
        from .skills import skill_for_profile, validate_skill
        pid = pid or self._unique_id(_slug(title, "project"))
        if self.exists(pid):
            raise ProjectError(f"项目已存在: {pid}")
        # skill 绑定（指挥层入口）：显式给定（含 kn-showcase 这类共享画风的 skill）
        # 原样采纳，否则由画风确定性派生——立项即落库，此后报项目名/编号 AI 查
        # project.skill 即知调哪个 skill（画风解析链 ④ 之上多一条项目级绑定）。
        explicit_skill = skill.strip() if isinstance(skill, str) and skill.strip() else None
        bound_skill = validate_skill(explicit_skill, bind=True) if explicit_skill else skill_for_profile(profile)
        data = {
            "id": pid, "title": title, "theme": theme, "profile": profile,
            "skill": bound_skill,
            # 平台不做默认绑定（默认 ["douyin"] 会诱导指挥层把未指定比例的
            # 项目猜成竖屏）；比例未指定恒落横屏——竖屏/方形必须显式传入
            "platform": platform or [], "aspect": aspect or DEFAULT_ASPECT,
            "status": "active", "created_at": _now(), "updated_at": _now(),
            "design": {"logline": "", "synopsis": "", "world": "",
                       "tone": "", "palette": "", "style_notes": ""},
            "characters": [], "chapters": [],
        }
        if template:   # 平台规格模板：风格/比例/平台/渲染模式 + 规格快照一键落位
            from .templates import apply_to_project
            apply_to_project(data, template)
            if not explicit_skill:   # 模板可能改 profile → skill 跟随最终画风重派生
                data["skill"] = skill_for_profile(data.get("profile"))
        d = self._pdir(pid)
        (d / "chapters").mkdir(parents=True, exist_ok=True)
        (d / "assets").mkdir(exist_ok=True)
        self.store.save_project(pid, data)
        return Series(self, pid, data)

    def get_project(self, pid: str, *, include_deleted: bool = False) -> "Series":
        """读取项目。已逻辑删除的项目默认**拒绝返回**——这是全局写路径的总闸：
        全部 stage 命令（gen-image/tts/gen-video/assemble/watermark/supply…）
        经 `_project_path` → 本方法解析章节，删态项目一律在此拦下；
        恢复/详情/删除本身走 include_deleted=True。"""
        data = self.store.load_project(pid)
        if data is None:
            raise ProjectError(f"找不到项目: {pid}")
        if not include_deleted and int(data.get("is_deleted") or 0):
            raise ProjectError(
                f"项目 {pid} 已逻辑删除（{data.get('deleted_at') or '时间未知'}）——"
                f"数据完整保留，恢复后再操作：kinema project restore {pid}")
        return Series(self, pid, data)

    def delete_project(self, pid: str, *, archive: bool = False) -> None:
        """删除只有一种语义：**逻辑删除**（is_deleted=1 + deleted_at，文档与
        数据库同步落位）。目录、产物、库行全部原样保留，随时可恢复；
        archive=True 是另一语义（status=archived，仍在清单中）。"""
        s = self.get_project(pid, include_deleted=True)
        if archive:
            s.data["status"] = "archived"
            s.save()
            return
        s.data["is_deleted"] = 1
        s.data["deleted_at"] = _now()
        s.save()

    def restore_project(self, pid: str) -> "Series":
        """恢复逻辑删除的项目：清 is_deleted / deleted_at，立即回到全部清单与流程。"""
        s = self.get_project(pid, include_deleted=True)
        s.data["is_deleted"] = 0
        s.data.pop("deleted_at", None)
        s.save()
        return s


def snapshot_style_prompt(series: "Series", store) -> None:
    """立项画风快照的唯一落位：把 profile 的画风前缀写进项目文档顶层
    `style_prompt`/`style_prompt_en`——此后分镜图/设定图/封面全部取该字段
    （解析链见 pipeline/prompts.select_style_prefix），手改该字段=全局换画风。

    CLI `project new` 与 Studio 建项目弹层共用本函数；store 为 None 或
    profile 未知时落空串——无配置不阻断建项目。调用方负责 save。"""
    img: dict = {}
    if store is not None:
        try:
            img = store.profile(series.data.get("profile")).get("image") or {}
        except Exception:   # noqa: BLE001 —— 未知 profile/配置损坏同样不阻断
            img = {}
    series.data.setdefault("style_prompt", (img.get("style_prefix") or "").strip())
    series.data.setdefault("style_prompt_en", (img.get("style_prefix_en") or "").strip())


# ----------------------------------------------------------------------------
# 系列文档「读—改—写」的进程内互斥锁。Studio 是 ThreadingHTTPServer，
# 而 `Series.save()` 是整份覆写——并发的长写操作之间会互相抹掉（见 Series.commit）。
# 用可重入锁：commit 块内再调到别的 commit（如 pick 里同步章节）不会自锁死。
_DOC_LOCK = threading.RLock()
# 本线程已持有文件锁的系列文档路径。flock 按「打开文件描述」判归属，同一线程再开
# 一个句柄申请同一把锁会自己等自己——嵌套只准最外层真去申请。
_HELD_DOCS = threading.local()


@contextmanager
def _doc_lock(path):
    """系列文档写锁：进程内互斥 + 跨进程文件锁，与章节文档同一底座。

    竞争面跨进程：Studio 把生成类操作派成 `python -m kinema …` 子进程
    （studio/jobs.spawn_cli），它与 Studio 线程写的是同一份 project.json。
    """
    from .locking import save_lock
    held = getattr(_HELD_DOCS, "paths", None)
    if held is None:
        held = _HELD_DOCS.paths = set()
    key = str(path)
    with _DOC_LOCK:
        if key in held:
            yield
            return
        with save_lock(path):
            held.add(key)
            try:
                yield
            finally:
                held.discard(key)


class Series:
    """一个项目/系列。"""

    def __init__(self, ws: Workspace, pid: str, data: dict):
        self.ws = ws
        self.pid = pid
        self.data = data

    @property
    def dir(self) -> Path:
        return self.ws._pdir(self.pid)

    @property
    def _doc_path(self) -> Path:
        """写锁挂靠的文档路径（取自存储层，理由见 `Storage.project_path`）。"""
        return self.ws.store.project_path(self.pid)

    def save(self) -> None:
        """整份写盘，**不合并**：手里这份 `data` 是什么就写什么。

        只服务「刚加载就改、立刻写回」的短操作。加载与写回之间隔着生成、上传
        或任何等待的，一律走 `commit()`——否则写回的是一份过期快照。"""
        with _doc_lock(self._doc_path):
            self.data["updated_at"] = _now()
            self.ws.store.save_project(self.pid, self.data)

    @contextmanager
    def chapter_write(self, cid: str):
        """系列侧改写某一章节文档的准入：与该章的生成/合成任务同一把操作锁，被占即拒。
        这类写入（设定字段对齐、音色表、垫图、大纲）不在 `Project.save` 的合并面，
        任务收尾写盘会把它们写回旧值。"""
        from .locking import op_lock
        with op_lock(self.ws.store.chapter_path(self.pid, cid), kind="series-sync"):
            yield

    @contextmanager
    def commit(self):
        """把「读—改—写」收进一把写锁，**且进锁后重新加载文档**。

        为什么必须有这个：`save()` 是**无合并的整份覆写**，而 Studio 是
        `ThreadingHTTPServer`（并发处理请求）、且好几个写操作要跑十几秒
        （角色试音 5 条 TTS 等），设定图重生一类更是派成子进程跑几分钟。
        两个写者各自 `get_project` 拿到一份副本、各跑各的、再各自整份写回——
        **后写的那个把先写的改动整段抹掉**，而且不报任何错。

        典型失败序列：给角色 A 生成试音（mp3 已落盘）→ 期间给角色 B 也点了试音 →
        B 后完成、用它那份没有 A 试音的旧副本覆盖 → A 的音频文件都在，
        `characters[].audition` 却是空的 → 点「选定」报「没有编号 1 的试音」。

        用法契约（**违反就白加了**）：
          · **耗时的生成放在 with 之外**（锁里只做登记，否则两个试音串成两倍时长）；
          · **进块之后重新定位实体**——`self.data` 已被换成磁盘上的最新副本，
            块外拿到的 `c = series.characters[i]` 是旧对象，改它写不进去。

            entries = [...synthesize...]          # 慢，锁外
            with series.commit():
                c = _find_character(series, name)  # 重新定位（必须）
                c["audition"] = {"batch": n, "at": ..., "entries": entries}
        """
        with _doc_lock(self._doc_path):
            fresh = self.ws.store.load_project(self.pid)
            if isinstance(fresh, dict) and fresh:
                self.data = fresh
            yield self
            self.save()

    # ---- 台账 ----
    def add_cost(self, kind: str, amount: float) -> None:
        """系列级支出（设定图、系列主视觉、试音、资产局改、锚定预热）入 `cost`。
        形态与章节 `Project.add_cost` 相同；额度闸只挂在章节生产链上，系列侧只记账。
        只改内存，落盘由调用方的 `commit()` 块或随后的 `save()` 负责。"""
        cost = self.data.setdefault("cost", {"currency": "CNY"})
        cost[kind] = round(float(cost.get(kind, 0.0) or 0.0) + float(amount), 4)

    # ---- 总体设计 ----
    def set_design(self, **fields) -> None:
        d = self.data.setdefault("design", {})
        for k, v in fields.items():
            if v is not None:
                d[k] = v
        self.save()

    # ---- 角色预设 ----
    @property
    def characters(self) -> list[dict]:
        return self.data.setdefault("characters", [])

    def add_character(self, name, *, voice=None, appearance="", role="", ref_image=None,
                      outfit="", hair="", weapon="", keywords=None, gender=None,
                      subject_kind=None, visual_requirements=None) -> None:
        # keywords 是防「缺席误报」的关键项：本名不足 2 字或常以绰号称呼的角色，
        # 没有别名就永远命中不了实体统计，会被 lint 报成「很久没出场」。
        # add_prop / add_scene 一直有这个参数，只有角色没有——于是每个 NPC 都要
        # 两条命令（add 完再 set），而人恰恰是三类实体里最常用绰号的那一类。
        if any(c["name"] == name for c in self.characters):
            raise ProjectError(f"角色已存在: {name}")
        # sheet=角色设定图(正脸肖像＋装备细节＋全身三视 正/侧/背/服装/持武器/发型)，由 gen-refs 生成后回填，各镜强制参考
        with self.commit():
            self.data.setdefault("characters", []).append(
                {"name": name, "voice": voice, "appearance": appearance,
                 "role": role, "outfit": outfit, "hair": hair, "weapon": weapon,
                 "keywords": list(keywords or []), "status": "active",
                 **({"gender": gender} if gender else {}),
                 **({"subject_kind": subject_kind} if subject_kind else {}),
                 **({"visual_requirements": list(visual_requirements)}
                    if visual_requirements else {}),
                 "sheet": None, "ref_image": ref_image})

    # ---- 设定的**实时更新**（纯文字字段）------------------------------------
    # 为什么必须有这三个 setter：设定不是立项时写死的常量，是**边写边长**的
    #（弧光推进、换装、断剑、新绰号、新规则）。手改 project.json 不是可行路径：
    # 引擎长任务持有旧内存副本逐镜 save 会整份覆写，mysql 模式下库行较新还会在
    # load 之前就把文件盖掉（同 decisions/threads 的既定纪律）。故走 `commit()`：进程锁 + 进锁后
    # 重新加载文档。**只收文字字段**：sheet/ref_image/audition 等由引擎回填的
    # 产物字段一律不在白名单里（要换图走 refs/refine/回滚那套版本栈）。
    CHAR_SETTABLE = ("voice", "appearance", "role", "outfit", "hair", "weapon",
                     "keywords", "speech_style", "personality", "arc",
                     "taboo_lines", "required_emotions", "required_actions",
                     "required_views", "silhouette_notes", "constraints",
                     "subject_kind", "visual_requirements", "status", "gender")
    PROP_SETTABLE = ("desc", "kind", "keywords")
    SCENE_SETTABLE = ("desc", "keywords")

    def _set_entity(self, bucket: str, name: str, fields: dict,
                    allowed: tuple[str, ...], label: str) -> dict:
        bad = [k for k in fields if k not in allowed]
        if bad:
            raise ProjectError(f"{label}不支持字段 {bad}（可设: {'/'.join(allowed)}；"
                               "设定图与音色等引擎回填字段走各自的命令）")
        with self.commit():
            rows = self.data.setdefault(bucket, [])     # 进锁后重新定位（必须）
            t = next((x for x in rows
                      if isinstance(x, dict) and x.get("name") == name), None)
            if t is None:
                have = "、".join(x.get("name") or "?" for x in rows) or "无"
                raise ProjectError(f"没有{label}「{name}」（现有: {have}）")
            for k, v in fields.items():
                if v is not None:
                    t[k] = v
            t["updated_at"] = _now()
            out = dict(t)
        return out

    def set_character(self, name: str, **fields) -> dict:
        return self._set_entity("characters", name, fields,
                                self.CHAR_SETTABLE, "角色")

    def set_prop(self, name: str, **fields) -> dict:
        return self._set_entity("props", name, fields, self.PROP_SETTABLE, "道具")

    # ⚠ 名字刻意不叫 `set_scene`——那个已被「全局固定场景文本」占用（`data["scene"]`，
    # 与具名取景地 `scenes[]` 是两个概念，见 add_scene 注释）。同名会静默覆盖。
    def set_named_scene(self, name: str, **fields) -> dict:
        return self._set_entity("scenes", name, fields, self.SCENE_SETTABLE, "场景")

    # ---- 设定集（角色/场景/道具设定图）----
    @property
    def refs_dir(self) -> Path:
        d = self.dir / "assets" / "refs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set_scene(self, scene: str) -> None:
        self.data["scene"] = scene
        self.save()

    @property
    def props(self) -> list[dict]:
        return self.data.setdefault("props", [])

    def add_prop(self, name, *, desc="", kind="prop", keywords=None) -> None:
        """道具/武器设定：kind=prop|weapon。sheet 由 gen-refs 生成后回填。
        keywords=提示词里可能用到的别名（如 name『青铜酒樽』配 ['酒樽','铜樽']）——
        image_prompt/narration 命中 name 或任一 keyword 即自动挂该道具设定图。"""
        if any(p["name"] == name for p in self.props):
            raise ProjectError(f"道具已存在: {name}")
        self.props.append({"name": name, "desc": desc, "kind": kind,
                           "keywords": list(keywords or []), "sheet": None})
        self.save()

    def remove_prop(self, name) -> None:
        self.data["props"] = [p for p in self.props if p["name"] != name]
        self.save()

    # ---- 具名场景（取景地）：与「全局固定场景」是两个概念，别混 ----------------
    # `scene`(文本)+`scene_ref`(一张) = 全片就发生在同一个地方时的基准图；
    # `scenes[]`                      = 这部戏会去的一个个取景地，按 keywords 命中挂载。
    # 两者不可互换：道具设定图走**物件版式**（完整视图+局部细节框+接缝/机构+
    # 浅灰底），套到「一个地方」上会把场景画成一件手持器物。
    @property
    def scenes(self) -> list[dict]:
        return self.data.setdefault("scenes", [])

    def add_scene(self, name, *, desc="", keywords=None) -> None:
        """具名场景设定：sheet 由 gen-refs 用**环境 key art 版式**生成后回填。
        keywords 同 props——image_prompt/narration 命中即自动挂该场景设定图。"""
        if any(x["name"] == name for x in self.scenes):
            raise ProjectError(f"场景已存在: {name}")
        self.scenes.append({"name": name, "desc": desc,
                            "keywords": list(keywords or []), "sheet": None})
        self.save()

    def remove_scene(self, name) -> None:
        self.data["scenes"] = [x for x in self.scenes if x["name"] != name]
        self.save()

    def remove_character(self, name) -> None:
        self.data["characters"] = [c for c in self.characters if c["name"] != name]
        self.save()
        # 章节 voices{} 由音色档案库同步写入，随实体一起摘除；留着的话引用账把它
        # 算作一处指派，这把声音在实体已不存在后仍不可删
        for ch in self.chapters:
            cid = ch["id"]
            with self.chapter_write(cid):
                data = self.ws.store.load_chapter(self.pid, cid)
                if data and name in (data.get("voices") or {}):
                    del data["voices"][name]
                    self.ws.store.save_chapter(self.pid, cid, data)

    def voices_map(self) -> dict:
        return {c["name"]: c["voice"] for c in self.characters if c.get("voice")}

    def character_block(self) -> str:
        parts = [f"{c['name']}——{c['appearance']}" for c in self.characters if c.get("appearance")]
        d = self.data.get("design", {})
        parts += [x for x in (d.get("palette"), d.get("tone")) if x]
        return "；".join(parts)

    # ---- 章节 ----
    @property
    def chapters(self) -> list[dict]:
        return self.data.setdefault("chapters", [])

    def _chapter_file(self, cid: str) -> Path:
        return self.dir / "chapters" / f"{cid}.json"

    def get_chapter_path(self, cid: str) -> Path:
        # mysql 模式会先把库中文档补写到磁盘（rehydrate），保证引擎按路径可渲染
        if self.ws.store.load_chapter(self.pid, cid) is None:
            raise ProjectError(f"找不到章节: {self.pid}/{cid}")
        return self.ws.store.chapter_path(self.pid, cid)

    def create_chapter(self, title, *, cid=None, theme="") -> Path:
        cid = cid or f"ch{len(self.chapters) + 1:02d}"
        cf = self._chapter_file(cid)
        # 本地文件只是工作副本，缺文件不等于该章号空闲——mysql 模式下章节
        # 要点名 load_chapter 才 rehydrate（`scaffold_episodes` 同判据）
        if self.ws.store.chapter_exists(self.pid, cid):
            raise ProjectError(f"章节已存在: {cid}")
        seed = int(hashlib.md5(f"{self.pid}/{cid}".encode()).hexdigest()[:6], 16)
        # 章节视频继承项目的 profile / 角色音色表 / 角色设定 / 色板
        video = {
            "id": f"{self.pid}_{cid}",
            "theme": theme or title,
            "profile": self.data.get("profile"),
            # 绑定 skill 一并拷贝：`skills.voiceover_default(profile, skill)` 的
            # skill 位靠它供料——kn-showcase 这类共享画风的 skill 语态与画风派生
            # 值不同，章节不带 skill 的话这条 override 在 lint 里永远打不中
            "skill": self.data.get("skill"),
            "platform": self.data.get("platform"),
            "aspect": self.data.get("aspect", DEFAULT_ASPECT),
            # 渲染模式继承（项目模板落位；渲染时仍可 --motion 覆盖）
            **({"motion": self.data["motion"]} if self.data.get("motion") else {}),
            # 字幕语言继承（zh/en/both，建项目时定；both 要求分镜带 narration_en）
            **({"subtitle_lang": self.data["subtitle_lang"]}
               if self.data.get("subtitle_lang") else {}),
            # 画风单点继承（立项快照，分镜图/设定图/封面统一取此字段）
            **({"style_prompt": self.data["style_prompt"]}
               if self.data.get("style_prompt") else {}),
            **({"style_prompt_en": self.data["style_prompt_en"]}
               if self.data.get("style_prompt_en") else {}),
            # 风格圣经旋钮（lint 告警松紧）——与 style_prompt 同待遇：建章时拷贝一份。
            # lint 只读**章节文档**顶层，不继承则系列级写的旋钮对新章静默失效、
            # 每章都回落中位 5。建时拷贝而非运行时回溯，对齐「章节继承是创建时拷贝」铁律。
            **({"art_direction": copy.deepcopy(self.data["art_direction"])}
               if isinstance(self.data.get("art_direction"), dict) else {}),
            # 配音表现力基调（pacing/energy_curve）——同 art_direction：建章时拷贝一份。
            # **刻意不进 sync_design_to_chapters**（那是设定集白名单，只管角色/道具/场景），
            # 所以改系列文档不回灌已建章节，要改存量章节直接改章节文档。
            **({"voice_performance": copy.deepcopy(self.data["voice_performance"])}
               if isinstance(self.data.get("voice_performance"), dict) else {}),
            "chapter": {"project": self.pid, "id": cid, "title": title},
            "voices": self.voices_map(),
            # 旁白音色（选角选定后全系列一致；未选过则走 profile 默认旁白）。
            # 正门是 `voice use` 立档写 `narrator.voice`；系列顶层直写的
            # `narrator_voice` 同样认——只认前者时手写顶层键是个静默死键，
            # 旁白落回 profile 默认且全程无提示
            **({"narrator_voice": nv} if (nv := (
                (self.data.get("narrator") or {}).get("voice")
                or self.data.get("narrator_voice"))) else {}),
            # 音色档案库随行：章节要能脱离项目文档独立渲染，定制音色的锚定参考音
            # 路径与声线描述都只能从这里解析
            **({"voice_bank": copy.deepcopy(self.data["voice_bank"])}
               if self.data.get("voice_bank") else {}),
            # 继承设定集：角色/道具设定图 + 场景（设定图）→ 各镜强制参考，跨镜跨集强一致
            "characters": [dict(c) for c in self.characters],
            "props": [dict(p) for p in self.props],
            "scenes": [dict(x) for x in self.scenes],      # 具名取景地（与全局 scene 并存）
            "scene": self.data.get("scene", ""),
            "scene_ref": self.data.get("scene_ref"),
            # 全局场景的俯视图与基准图成对继承——只继承一张等于让本章的空间证据缺一半
            # （具名取景地那一份随 scenes[] 整份拷贝过来）
            "scene_topview_ref": self.data.get("scene_topview_ref"),
            "skip_design": self.data.get("skip_design", False),
            "style": {"character_block": self.character_block(),
                      "palette": self.data.get("design", {}).get("palette", ""),
                      "seed": seed,
                      # 参考库默认生效垫图随建章继承（分镜图 ref_images 默认套用；后续增删经 sync 同步）
                      **({"moodboard": self.moodboard_active()} if self.moodboard_active() else {})},
            "script": {}, "shots": [],
        }
        self.ws.store.save_chapter(self.pid, cid, video)
        self.chapters.append({"id": cid, "title": title,
                              "order": len(self.chapters) + 1, "created_at": _now()})
        self.save()
        return cf

    def sync_design_to_chapters(self) -> dict:
        """把系列级设定集（角色/道具/场景与设定图）按名 upsert 进全部已建章节。

        章节继承是**创建时拷贝**——官方节点顺序是"先建章节、①.5 再补设定集"，
        不同步会让已建章节设定为空：生图不带参考、gen-video 就绪度节点静默失效
        （最贵的一致性事故）。本方法由 `project refs` 收尾自动调用。

        合并规则：缺失的角色/道具整体补入；同名的只更新**设计字段**与设定图路径
        （音色字段不动——音色同步是 voice use 的职责）；scene 文本仅在章节为空时
        回填；scene_ref 始终对齐系列最新；characters 变化时同步刷新
        style.character_block（seed 等其余风格锚点不动）。
        返回 {"chapters": 同步章节数, "added": 补入实体数, "updated": 更新实体数}。

        **`char_fields`/`prop_fields` 是「系列→章节单向覆盖」的白名单**：新增设定字段
        必须登记，否则对**存量章节**永远静默失效（新建章节走 `create_chapter` 的整份
        拷贝、天然继承，白名单只管存量）。一致性判定与调度 lint 都跑在章节层，漏登记
        =「系列填了、章节看不见」。
        """
        char_fields = ("appearance", "role", "outfit", "hair", "weapon", "voice_prompt",
                       # sheet_origin 与 sheet 同批走：gen-video 的受信告警读章节副本，
                       # 只搬路径不搬来源 = 告警永远盯着旧来源
                       "sheet", "sheet_origin", "ref_image",
                       # 扩展设定图（表情/动作）与主设定图同一条对齐通路：
                       # 章节层经 shots[].refs 显式挂用，路径停在旧版就是挂旧图
                       "expression_sheet", "pose_sheet",
                       # M8 角色清单前置（系列级常量：该角色一共要演到的表情/动作/视角、
                       # 剪影辨识度要点、硬约束）——按集填会被系列值冲掉，见 schema。
                       "required_emotions", "required_actions", "required_views",
                       "silhouette_notes", "constraints",
                       # 视觉语义分层：主体类型显式登记，正向视觉要求不进 negative。
                       "subject_kind", "visual_requirements",
                       # 文字人设四件（台词口吻/性格内核/人物弧光/行为禁区）——
                       # 写分镜台词与小说正文的人设门判据，章节层也要看得见
                       "speech_style", "personality", "arc", "taboo_lines",
                       # 角色别名（绰号/尊称/代称）：正文实体命中统计
                       # 与跨章缺席判定的兜底口径，与 props/scenes 的 keywords 同构
                       "keywords",
                       # 在场状态 active|departed|dead：长篇里永久退场
                       # 是常态不是异常，lint 的「连续缺席」只对 active 报
                       "status",
                       # 角色性别 male|female（试音候选过滤 character_gender 判定链
                       # 的第一环·人工字段，upsert_entities 绝不登记）
                       "gender")
        prop_fields = ("desc", "kind", "keywords", "sheet")
        # 具名场景与道具同构（desc/keywords/sheet），新增字段同样必须登记进来。
        # `topview_sheet` 必须在列：俯视图与基准图配对出图，而存量章节靠这份白名单
        # 更新——漏登记就是「系列里图纸都在、出视频时一张都挂不上」，且全程零告警
        scene_fields = ("desc", "keywords", "sheet", "topview_sheet")
        stats = {"chapters": 0, "added": 0, "updated": 0}
        for ch in self.chapters:
            with self.chapter_write(ch["id"]):
                data = self.ws.store.load_chapter(self.pid, ch["id"])
                if data is None:
                    continue
                changed = chars_changed = False
                chars = data.setdefault("characters", None) or []
                data["characters"] = chars
                by_name = {c.get("name"): c for c in chars if isinstance(c, dict)}
                for c in self.characters:
                    t = by_name.get(c["name"])
                    if t is None:
                        chars.append(dict(c))
                        stats["added"] += 1
                        changed = chars_changed = True
                    elif any(c.get(k) is not None and t.get(k) != c.get(k)
                             for k in char_fields):
                        for k in char_fields:
                            if c.get(k) is not None:
                                t[k] = c.get(k)
                        stats["updated"] += 1
                        changed = chars_changed = True
                props = data.setdefault("props", None) or []
                data["props"] = props
                by_name_p = {p.get("name"): p for p in props if isinstance(p, dict)}
                for p in self.props:
                    t = by_name_p.get(p["name"])
                    if t is None:
                        props.append(dict(p))
                        stats["added"] += 1
                        changed = True
                    elif any(p.get(k) is not None and t.get(k) != p.get(k)
                             for k in prop_fields):
                        for k in prop_fields:
                            if p.get(k) is not None:
                                t[k] = p.get(k)
                        stats["updated"] += 1
                        changed = True
                scenes = data.setdefault("scenes", None) or []
                data["scenes"] = scenes
                by_name_s = {x.get("name"): x for x in scenes if isinstance(x, dict)}
                for x in self.scenes:
                    t = by_name_s.get(x["name"])
                    if t is None:
                        scenes.append(dict(x))
                        stats["added"] += 1
                        changed = True
                    elif any(x.get(k) is not None and t.get(k) != x.get(k)
                             for k in scene_fields):
                        for k in scene_fields:
                            if x.get(k) is not None:
                                t[k] = x.get(k)
                        stats["updated"] += 1
                        changed = True
                if self.data.get("scene") and not (data.get("scene") or "").strip():
                    data["scene"] = self.data["scene"]
                    changed = True
                for k in ("scene_ref", "scene_topview_ref"):   # 全局场景的两张图成对对齐
                    if self.data.get(k) and data.get(k) != self.data[k]:
                        data[k] = self.data[k]
                        changed = True
                if chars_changed:
                    data.setdefault("style", {})["character_block"] = self.character_block()
                mb = self.moodboard_active()      # 参考库默认生效图 → 章节 style（分镜图 ref_images 读取）
                if (data.get("style") or {}).get("moodboard") != mb:
                    data.setdefault("style", {})["moodboard"] = mb
                    changed = True
                if changed:
                    self.ws.store.save_chapter(self.pid, ch["id"], data)
                    stats["chapters"] += 1
        return stats

    # ---- 风格垫图 / 参考库（项目级参考图，默认注入每张设定图/分镜图生成的 ref_images）----
    #   库项形状 {path, on}：on=True → 默认全局套用到所有设定图/分镜图；on=False → 留库但不默认套用。
    #   逐镜可用 shots[].refs 精确覆盖（网页勾选/取消的落点，见 Project.moodboard_refs）；
    #   一次性「不要垫图」走 CLI --no-moodboard。同步进各章 style.moodboard 的只是**默认生效集**。
    @staticmethod
    def _norm_mb_item(it) -> dict:
        """库项归一：兼容历史纯字符串路径 → {path, on:True}；缺路径丢弃。"""
        if isinstance(it, str):
            return {"path": it, "on": True}
        if isinstance(it, dict) and it.get("path"):
            return {"path": str(it["path"]), "on": it.get("on", True) is not False}
        return {}

    @staticmethod
    def _same_path(a: str, b: str) -> bool:
        """路径等价判定：先精确、再 resolve 归一（吃掉 /var↔/private/var 符号链、相对/绝对差异）。"""
        if str(a) == str(b):
            return True
        try:
            return Path(a).resolve() == Path(b).resolve()
        except Exception:  # noqa: BLE001
            return False

    @property
    def moodboard(self) -> list[dict]:
        """参考库全量（含停用项），就地归一为 {path, on} 列表（历史纯字符串一次性升级）。"""
        raw = self.data.setdefault("moodboard", [])
        norm = [x for x in (self._norm_mb_item(i) for i in raw) if x]
        if norm != raw:
            self.data["moodboard"] = norm
        return self.data["moodboard"]

    def moodboard_active(self) -> list[str]:
        """默认生效的参考图路径（库里 on=True 的项）——注入全局所有生成。"""
        return [x["path"] for x in self.moodboard if x.get("on", True)]

    def moodboard_refs_for(self, refs) -> list[str]:
        """设定图（角色/场景/道具）垫图解析——`Project.moodboard_refs`（逐镜）的设定图对应物：
        显式 refs(list) → 精确用（`[]`=本图不用垫图）；None/缺 → 默认生效集 moodboard_active()。
        单一真源：每张设定图各自挑各自的垫图，互不影响（网页设定图灯箱「垫图」的落点）。"""
        return [str(x) for x in refs] if isinstance(refs, list) else self.moodboard_active()

    # 参考库开关与「重出设定图」在 Studio 同一页上，后者派的是跑几分钟的子进程。
    # `sync_moodboard` 留在块外：它写的是章节文件，不该延长系列文档的持锁时间。
    def add_moodboard(self, path: str) -> list[dict]:
        """登记一张风格垫图（绝对路径，默认启用），同步各章 style.moodboard。"""
        p = str(path)
        added = False
        with self.commit():
            if not any(self._same_path(x["path"], p) for x in self.moodboard):
                self.moodboard.append({"path": p, "on": True})
                added = True
        if added:
            self.sync_moodboard()
        return self.moodboard

    def set_moodboard_on(self, path: str, on: bool) -> bool:
        """切换某库项的「默认启用」态（保留库项与文件，仅改是否默认套用）；改变即同步各章。"""
        want, changed = bool(on), False
        with self.commit():
            hit = next((x for x in self.moodboard
                        if self._same_path(x["path"], path)), None)
            if hit is not None and bool(hit.get("on", True)) != want:
                hit["on"] = want
                changed = True
        if changed:
            self.sync_moodboard()
        return changed

    def remove_moodboard(self, path: str) -> bool:
        removed = False
        with self.commit():
            kept = [x for x in self.moodboard if not self._same_path(x["path"], path)]
            if len(kept) != len(self.moodboard):
                self.data["moodboard"] = kept
                removed = True
        if removed:
            self.sync_moodboard()
        return removed

    def sync_moodboard(self) -> int:
        """把参考库的**默认生效**图（on=True）同步进各章 style.moodboard（分镜图 ref_images 的默认读取源）。"""
        mb = self.moodboard_active()
        n = 0
        for ch in self.chapters:
            with self.chapter_write(ch["id"]):
                data = self.ws.store.load_chapter(self.pid, ch["id"])
                if data is None:
                    continue
                style = data.setdefault("style", {})
                if style.get("moodboard") != mb:
                    style["moodboard"] = mb
                    self.ws.store.save_chapter(self.pid, ch["id"], data)
                    n += 1
        return n

    def chapter_status(self, cid: str) -> str:
        v = self.ws.store.load_chapter(self.pid, cid)
        if v is None:
            return "missing"
        from .storage.base import chapter_status
        return chapter_status(self.dir / "chapters" / f"{cid}_work", v)

    def list_chapters(self) -> list[dict]:
        out = []
        for ch in sorted(self.chapters, key=lambda c: c.get("order", 0)):
            out.append({**ch, "status": self.chapter_status(ch["id"]),
                        "file": str(self._chapter_file(ch["id"]))})
        return out

    def delete_chapter(self, cid: str) -> None:
        self.ws.store.delete_chapter(self.pid, cid)
        self.data["chapters"] = [c for c in self.chapters if c["id"] != cid]
        self.save()

    # ---- 剧本改编（源文本入库 → 分集 → 建章）----
    #
    # 「Python + AI 两段式」的 Python 半（引擎承接）：源文本落盘 + 结构切分 +
    # 幂等建章 + 实体合并入库。**拆书/分集/抽实体/拆镜的智能一律由 Claude 指挥层
    # 完成**（铁律「引擎内无 LLM provider」）；本段只做机械承接。
    #   · adaptation（拆书产物）/ episodes（分集大纲）由 Claude 直接写进系列文档；
    #   · source 指针块由 adapt import 回填（正文只落盘、契约存路径）。

    @property
    def source_dir(self) -> Path:
        """源剧本/小说正文落盘目录（与 assets/ 同级）；正文只存文件、契约存路径。"""
        d = self.dir / "source"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def set_source(self, *, kind: str, file: str, chars: int, sha256: str) -> None:
        """回填源文本指针块（正文真身在 source/ 文件，project.json 只存路径 + 指纹）。"""
        self.data["source"] = {"kind": kind, "file": file, "chars": chars,
                               "sha256": sha256, "ingested_at": _now()}
        self.save()

    def ingest_source(self, *, filename: str, data: bytes,
                      kind: str | None = None) -> dict:
        """源文本入库（机械承接·零 LLM）：稳健解码 → 归一换行 → 正文落盘
        `source/raw.txt` → Track A 结构切分落 `source/segments.json` → 回填 source
        指针。`kind=None` 自动判定（novel/screenplay）。**CLI `adapt import` 与
        Studio「改编」区上传共用此单一路径**。返回入库摘要 dict。
        """
        from . import adaptation as A
        from . import lineage
        # EPUB 分支（先于文本解码）：EPUB 是 ZIP 容器，不能当纯文本 decode——拆书抽
        # 正文 + 章标题（纯 stdlib），落盘与非 EPUB 路径同制度（raw.txt + segments.json
        # + source 指针），encoding 记 "epub"。加密/无正文等抛 ValueError → ProjectError。
        if A.is_epub(data, filename):
            try:
                text, segs = A.extract_epub(data)
            except ValueError as e:
                raise ProjectError(str(e)) from e
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if not text.strip():
                raise ProjectError("EPUB 未解析出正文")
            dest = self.source_dir / "raw.txt"
            dest.write_text(text, encoding="utf-8")
            digest = {"kind": "novel", "chars": len(text), "n_segments": len(segs),
                      "segment_kind": "chapter", "segments": segs}
            (self.source_dir / "segments.json").write_text(
                json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")
            sha = lineage.fingerprint(str(dest)) or A.text_fingerprint(text)
            self.set_source(kind="novel", file="source/raw.txt", chars=len(text), sha256=sha)
            return {"kind": "novel", "encoding": "epub", "chars": len(text),
                    "segment_kind": "chapter", "n_segments": len(segs), "file": "source/raw.txt"}
        text, enc = A.decode_source(data)
        text = text.replace("\r\n", "\n").replace("\r", "\n")   # 归一换行：偏移/切分稳定
        if not text.strip():
            raise ProjectError("源文件为空")
        # 乱码闸（自动转码后的最终兜底）：decode_source 已尽力识别 UTF-8/GBK/Big5/
        # UTF-16 并转 UTF-8；若最优候选仍满是乱码（U+FFFD+PUA>25%），说明是加密/二进制/
        # 未知编码而非文本——拒收，绝不把糊掉的正文烧进 raw.txt/segments（放行就是把
        # 大面积 U+FFFD 静默烧进正文）。常规中文编码此处一律 0，不误伤。
        ratio = A.undecodable_ratio(text)
        if ratio > 0.25:
            raise ProjectError(
                f"源文件无法作为文本解析（自动识别多种编码后仍有约 {ratio * 100:.0f}% 乱码）——"
                "疑似加密 / 二进制 / 非文本文件或已损坏。请确认是纯文本小说/剧本，"
                "或用编辑器另存为 UTF-8 后重新上传。")
        kind = kind or A.detect_format(text, filename=filename)
        dest = self.source_dir / "raw.txt"
        dest.write_text(text, encoding="utf-8")
        digest = A.structural_digest(text, kind, filename=filename)
        (self.source_dir / "segments.json").write_text(
            json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")
        sha = lineage.fingerprint(str(dest)) or A.text_fingerprint(text)
        self.set_source(kind=kind, file="source/raw.txt",
                        chars=digest["chars"], sha256=sha)
        return {"kind": kind, "encoding": enc, "chars": digest["chars"],
                "segment_kind": digest["segment_kind"],
                "n_segments": digest["n_segments"], "file": "source/raw.txt"}

    # ---- 参考片读片（study）：立项前门，只量节奏不抄内容 ----
    #
    # 与 source/ 同族的「外部供料指针块」：参考片本体落 `study/<slug>/`，契约里
    # **只存工作区相对路径**——绝对路径会被 `storage/media.collect_media` 收进
    # 上传清单，`oss sync` 把第三方片子传上用户自己的公网 OSS。测量与解析全在
    # `kinema/study.py`，本段只是薄承接（同 ingest_source 的分工）。

    @property
    def study(self) -> list[dict]:
        """读片记录（引擎回填，指挥层只读）。条目形态见 study.contract_entry。"""
        return self.data.setdefault("study", [])
    def ingest_study(self, src, **kw) -> dict:
        """参考片入库（拷贝 → 量节奏 → 抽帧 → 落 digest → 回填契约）。见 study.ingest。"""
        from . import study as S
        return S.ingest(self, Path(src), **kw)

    def remove_study(self, slug: str) -> dict:
        """删读片记录 + 整个 `study/<slug>/`（版权卫生：读完即删）。见 study.remove。"""
        from . import study as S
        return S.remove(self, slug)

    @property
    def adaptation(self) -> dict:
        return self.data.setdefault("adaptation", {})

    @property
    def episodes(self) -> list[dict]:
        return self.data.setdefault("episodes", [])

    @property
    def graph(self) -> dict:
        """人物关系 / 世界观图谱（Claude 指挥层填，引擎不消费）——与 adaptation/episodes
        平级的**跨章一致性宪法**类数据。结构 `{summary, nodes:[{id,name,type,…}],
        edges:[{source,target,relation,kind,…}]}`；节点五类 character/faction/location/
        item/worldview，边 kind 驱动前端配色。图谱只服务规划与可视化，绝不进渲染管线。"""
        return self.data.setdefault("graph", {"summary": "", "nodes": [], "edges": []})

    def set_graph(self, graph: dict) -> dict:
        """整体落库关系图谱（**replace 非 merge**）——图谱是一份自洽快照、无任何人工子
        字段（不像 upsert_entities 要保 voice/keywords/comments），每次分析产出整份，
        整体替换才正确、也更简洁。

        校验：nodes 为非空数组、节点 id 非空且唯一；edges 为数组且每条边端点必须指向
        已存在节点（**悬空边直接拒收**，绝不让前端渲染出断头连线）。节点/边的额外字段
        （desc/faction/role/aka/directed…）原样透传。返回 {"nodes": n, "edges": n}。
        """
        if not isinstance(graph, dict):
            raise ProjectError('图谱 JSON 顶层须为对象：{"nodes":[...],"edges":[...]}')
        nodes = graph.get("nodes")
        edges = graph.get("edges") or []
        if not isinstance(nodes, list) or not nodes:
            raise ProjectError("图谱 nodes 须为非空数组")
        if not isinstance(edges, list):
            raise ProjectError("图谱 edges 须为数组")
        ids, clean_nodes = set(), []
        for n in nodes:
            if not isinstance(n, dict):
                raise ProjectError("每个节点须为对象")
            nid = str(n.get("id") or "").strip()
            if not nid:
                raise ProjectError(f"节点缺 id：{n.get('name') or n}")
            if nid in ids:
                raise ProjectError(f"节点 id 重复：{nid}")
            ids.add(nid)
            n["id"] = nid
            clean_nodes.append(n)
        clean_edges = []
        for e in edges:
            if not isinstance(e, dict):
                raise ProjectError("每条边须为对象")
            src, tgt = str(e.get("source") or ""), str(e.get("target") or "")
            if src not in ids or tgt not in ids:
                raise ProjectError(f"边端点指向不存在的节点：{e.get('source')} → {e.get('target')}")
            clean_edges.append(e)
        self.data["graph"] = {"summary": str(graph.get("summary") or "").strip(),
                              "nodes": clean_nodes, "edges": clean_edges,
                              "updated_at": _now()}
        self.save()
        return {"nodes": len(clean_nodes), "edges": len(clean_edges)}

    def upsert_entities(self, *, characters=None, props=None) -> dict:
        """重抽实体**合并入库（合并不覆盖，保人工字段）**。

        与 `sync_design_to_chapters` 的整体替换**刻意不同**（切勿照抄那套循环）：
          · 新增补入；
          · 同名只更新「抽取拥有字段」（角色 appearance/role/outfit/hair/weapon；
            道具 desc/kind）；
          · **保留「人工拥有字段」**：voice/audition/custom_audition/sheet/ref_image、
            M8 角色清单（required_emotions/required_actions/required_views/
            silhouette_notes/constraints）、逐实体 comments 一律不动；
          · **keywords 取并集（union）**，绝不整体替换（用户手动追加的别名不丢）。
        返回 {"added": n, "updated": n}。
        """
        # ⚠ 这份白名单是「抽取拥有」的字段，与 sync_design_to_chapters 的同名变量**语义相反**，
        # 切勿互抄：登进来的字段会在下次 `adapt` 重抽时被原文覆盖/清空。M8 五字段与
        # 文字人设四件（speech_style/personality/arc/taboo_lines）属人工创作，
        # 一律不得登记。
        char_fields = ("appearance", "role", "outfit", "hair", "weapon")
        prop_fields = ("desc", "kind")
        stats = {"added": 0, "updated": 0}

        by_c = {c.get("name"): c for c in self.characters if isinstance(c, dict)}
        for c in characters or []:
            name = c.get("name")
            if not name:
                continue
            t = by_c.get(name)
            if t is None:
                self.characters.append({
                    "name": name, "voice": c.get("voice"),
                    "appearance": c.get("appearance") or "", "role": c.get("role") or "",
                    "outfit": c.get("outfit") or "", "hair": c.get("hair") or "",
                    "weapon": c.get("weapon") or "", "sheet": None,
                    "ref_image": c.get("ref_image")})
                by_c[name] = self.characters[-1]
                stats["added"] += 1
            else:
                changed = False
                for k in char_fields:
                    v = c.get(k)
                    if v not in (None, "") and t.get(k) != v:
                        t[k] = v
                        changed = True
                stats["updated"] += 1 if changed else 0

        by_p = {p.get("name"): p for p in self.props if isinstance(p, dict)}
        for p in props or []:
            name = p.get("name")
            if not name:
                continue
            kws = [k for k in (p.get("keywords") or []) if k]
            t = by_p.get(name)
            if t is None:
                self.props.append({"name": name, "desc": p.get("desc") or "",
                                   "kind": p.get("kind") or "prop",
                                   "keywords": list(kws), "sheet": None})
                by_p[name] = self.props[-1]
                stats["added"] += 1
            else:
                changed = False
                for k in prop_fields:
                    v = p.get(k)
                    if v not in (None, "") and t.get(k) != v:
                        t[k] = v
                        changed = True
                if kws:                                   # keywords 取并集，保人工追加
                    merged = list(t.get("keywords") or [])
                    for kw in kws:
                        if kw not in merged:
                            merged.append(kw)
                            changed = True
                    t["keywords"] = merged
                stats["updated"] += 1 if changed else 0

        if stats["added"] or stats["updated"]:
            self.save()
        return stats

    @staticmethod
    def compose_outline(ep: dict) -> str:
        """把一集的规划字段编译成章节 `outline` 自由文本（episodes[] 的派生缓存）。"""
        rows = [("本集", ep.get("logline")), ("开场钩子", ep.get("open_hook")),
                ("核心事件", ep.get("core_event")), ("爽点/反转", ep.get("cool_point")),
                ("尾钩", ep.get("end_hook")), ("原文", ep.get("source_range"))]
        return "\n".join(f"【{label}】{val}" for label, val in rows if val)

    def upsert_chapter_outline(self, cid: str, outline: str) -> str:
        """幂等写章节 `outline`（**只写 outline，不碰 shots/review/comments**）。

        返回 "noop"（无变化）/ "updated" / "updated-warn"（该集已拆镜，改大纲不会自动
        重拆，交人工核对）。章节不存在时报错（由 `scaffold_episodes` 负责先建）。
        """
        with self.chapter_write(cid):
            data = self.ws.store.load_chapter(self.pid, cid)
            if data is None:
                raise ProjectError(f"找不到章节: {self.pid}/{cid}")
            if data.get("outline") == outline:
                return "noop"
            warn = bool(data.get("shots"))
            data["outline"] = outline
            self.ws.store.save_chapter(self.pid, cid, data)
            return "updated-warn" if warn else "updated"

    def scaffold_episodes(self, *, only=None) -> dict:
        """据 `episodes[]` **幂等**批量建章 + 拷 outline，回填 chapter_id。

        `cid = f"ch{ep.no:02d}"`（**章号 == 集号**，跳集不错位）；章节已存在则只刷新
        outline（绝不重建、不炸、不毁产物）。返回
        ``{"created":[cid...], "updated":[cid...], "warned":[cid...], "mapping":{no:cid}}``。
        """
        result = {"created": [], "updated": [], "warned": [], "mapping": {}}
        # None=不过滤全建；空列表=显式过滤后为空→不建任何集（区分二者，防畸形输入误全建）
        want = None if only is None else {int(x) for x in only}
        for ep in self.episodes:
            no = ep.get("no")
            if no is None:
                continue
            if want is not None and int(no) not in want:
                continue
            cid = f"ch{int(no):02d}"
            outline = self.compose_outline(ep)
            if self.ws.store.load_chapter(self.pid, cid) is None:
                self.create_chapter(ep.get("title") or f"第{no}集", cid=cid,
                                    theme=ep.get("logline") or "")
                self.upsert_chapter_outline(cid, outline)
                result["created"].append(cid)
            else:
                st = self.upsert_chapter_outline(cid, outline)
                if st != "noop":
                    result["updated"].append(cid)
                if st == "updated-warn":
                    result["warned"].append(cid)
            # 章节索引 order 对齐集号——跳集/乱序 scaffold 后章节列表仍按集序排列
            for ch in self.chapters:
                if ch.get("id") == cid:
                    ch["order"] = int(no)
                    break
            ep["chapter_id"] = cid
            result["mapping"][no] = cid
        self.save()
        return result
