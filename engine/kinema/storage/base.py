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

"""存储层基座：接口定义 + 两个后端共用的派生元数据计算。

数据模型是「文档式」的：项目/章节各是一份 JSON 文档（与 project.schema.json 一致），
后端只决定文档存哪里（本地文件 / MySQL），不改变文档结构。
媒体文件（图/音/视频）永远在磁盘工作区，文档与数据库里只存**路径**。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..project import DEFAULT_ASPECT, effective_motion


def atomic_write_json(path: str | Path, data: dict) -> None:
    """整份 JSON 文档的原子落盘：同目录临时文件写完再 os.replace。

    项目/章节文档由 Studio 线程与 spawn_cli 子进程并发读写，非原子写的
    半截文件会被读端当「损坏/不存在」处理，进而以陈旧内存副本整份回写。
    临时名带进程号与随机后缀，防两个并发写者共用同一个临时文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def chapter_title(ch: dict, cdata: dict | None) -> str:
    """章节标题的唯一解析口：**章节文档赢，project.json 的登记表兜底**。

    标题落在两处——`chapters/<cid>.json` 的 `chapter.title`（改名时改的就是它）与
    `project.json` 的 `chapters[].title`（建章时写下的索引）。两处各读各的，结果是
    章节页已经改名、待审队列/看板/搜索/成本页还挂着旧名（搜索按当前名还查不到）。
    文档是人编辑的那一份，故它赢；登记表只在章节文件缺失（未建/被删）时兜底。
    放存储层共用：studio scanner 与引擎域 business 都要用，前者不能被后者反向引。"""
    doc = ((cdata or {}).get("chapter") or {}).get("title")
    return str(doc or ch.get("title") or ch.get("id") or "")


def chapter_status(workdir: Path, data: dict) -> str:
    """章节状态推导的唯一判据：有成片 → rendered，有分镜 → scripted，否则 draft。

    状态不落盘、纯由产物推导——判据在 workspace（CLI 清单）、mysql 索引列与
    studio scanner 三处消费，各抄一份的下场与 `chapter_title` 收敛前相同：
    某处改了判据（比如认了新容器格式），其余清单页还按旧口径分桶。
    「章节文件缺失 → missing」是调用方语义（文件层的事），不进本判据。"""
    outdir = workdir / "output"
    if outdir.is_dir() and any(outdir.glob("*.mp4")):
        return "rendered"
    return "scripted" if (data or {}).get("shots") else "draft"


def chapter_meta(root: Path, pid: str, cid: str, data: dict) -> dict:
    """从章节文档 + 磁盘产物推导可检索元数据（状态/时长/成片路径等，入库用作索引列）。"""
    workdir = root / pid / "chapters" / f"{cid}_work"
    outdir = workdir / "output"
    mp4s = sorted(outdir.glob("*.mp4")) if outdir.is_dir() else []
    shots = data.get("shots") or []
    status = chapter_status(workdir, data)
    anim_files = (data.get("animatic") or {}).get("files") or {}
    return {
        "title": (data.get("chapter") or {}).get("title") or data.get("theme") or cid,
        "status": status,
        "motion": effective_motion(data),
        "shots": len(shots),
        "duration": round(sum(float(s.get("dur") or 0) for s in shots), 2),
        "video_path": str(mp4s[0]) if mp4s else None,
        "cost": data.get("cost"),
        # 草稿两段式：全片 Ken Burns 样片与其审阅状态
        "animatic_path": anim_files.get(data.get("aspect") or DEFAULT_ASPECT)
                         or next(iter(anim_files.values()), None),
        "animatic_state": ((data.get("review") or {}).get("animatic") or {}).get("state"),
    }


class Storage:
    """项目/章节文档的读写接口；root 是已解析的工作区数据目录。

    源码检出的规范 root 为 ``<repo>/project``，显式隔离工作区则使用调用方给出的
    数据目录。local 与 mysql 只改变文档持久化后端；同一工作区切换后端时 root
    必须保持不变，不能迁移到 ``engine/project`` 或另一份目录。
    """

    backend = "base"

    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- 项目 ----
    def list_projects(self) -> list[dict]:
        raise NotImplementedError

    def project_exists(self, pid: str) -> bool:
        return self.load_project(pid) is not None

    def load_project(self, pid: str) -> dict | None:
        raise NotImplementedError

    def save_project(self, pid: str, data: dict) -> None:
        raise NotImplementedError

    def project_path(self, pid: str) -> Path:
        """项目文档路径。写锁挂在它上面，故路径只能由存储层给出：
        `Workspace.root` 可能未经 resolve，调用方自拼会得到第二个锁文件。"""
        return self.root / pid / "project.json"

    # ---- 章节 ----
    def load_chapter(self, pid: str, cid: str) -> dict | None:
        raise NotImplementedError

    def save_chapter(self, pid: str, cid: str, data: dict, *, write_file: bool = True) -> None:
        raise NotImplementedError

    def delete_chapter(self, pid: str, cid: str) -> None:
        raise NotImplementedError

    def chapter_exists(self, pid: str, cid: str) -> bool:
        """章节是否已占用该 id。与 `project_exists` 同口径：本地文件只是工作副本，
        缺文件不等于该 id 空闲，故判据须由后端给出。"""
        return self.load_chapter(pid, cid) is not None

    def chapter_path(self, pid: str, cid: str) -> Path:
        """章节文件路径（引擎按路径渲染）。mysql 后端会先把库中文档补写到磁盘（rehydrate）。"""
        return self.root / pid / "chapters" / f"{cid}.json"

    # ---- 工作区级配置（模型覆盖层等，不属于任何项目）----
    # 这一族与项目/章节的分工是刻意的：本地 JSON 文件是**运行时真源**（引擎热路径
    # 只读文件，spawn 出去的每个子进程都要加载一次配置，为读配置连库既慢又给命令行
    # 平添数据库依赖），数据库只作**跨机同步层**。故 local 后端两个方法都是空操作。
    def load_settings(self, scope: str, name: str, *,
                      local_file: Path | None = None) -> dict | None:
        """读持久层里的一份工作区级配置。

        返回 `{"data": dict, "newer": bool}`——`newer` 表示库中这行是否明显比
        `local_file` 新。「新者赢」的判定留在存储层，与项目/章节同源，调用方不必
        自己比时间戳。local 后端恒 None（文件已是真源，没有第二份副本可比）。
        """
        return None

    def save_settings(self, scope: str, name: str, data: dict) -> None:
        """把一份工作区级配置写进持久层。local 后端为空操作。"""
        return None

    # ---- 自述 ----
    def describe(self) -> str:
        return f"{self.backend} · {self.root}"
