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

"""本地 JSON 后端（默认，零依赖）—— JSON 即数据库。

目录约定（与 workspace.md 一致）：
  <root>/<pid>/project.json           项目文档
  <root>/<pid>/chapters/<cid>.json    章节文档（引擎可直接渲染）
"""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import DocumentCorruptError
from .base import Storage, atomic_write_json


def _read(p: Path) -> dict | None:
    """读文档；缺失返回 None，损坏必须抛 DocumentCorruptError。

    损坏与缺失是两种状态：损坏文档按 None 返回时，存在性判断与「章节已存在」
    创建闸都会失守，同 ID 的新建操作会把仍可人工修复的文件整份覆盖。"""
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise DocumentCorruptError(p, str(e)) from e
    if not isinstance(data, dict):
        raise DocumentCorruptError(p, "顶层不是 JSON 对象")
    return data


def _write(p: Path, data: dict) -> None:
    atomic_write_json(p, data)


class LocalStorage(Storage):
    backend = "local"

    def _pfile(self, pid: str) -> Path:
        return self.project_path(pid)

    def _cfile(self, pid: str, cid: str) -> Path:
        return self.chapter_path(pid, cid)

    # ---- 项目 ----
    def list_projects(self) -> list[dict]:
        """清单跳过损坏条目并警告——单个坏文档不该让整个工作区不可用；
        损坏项目的写路径仍由 `_read` 抛错拦住。"""
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            try:
                data = _read(d / "project.json")
            except DocumentCorruptError as e:
                print(f"  ⚠ {e}")
                continue
            if data:
                out.append(data)
        return out

    def project_exists(self, pid: str) -> bool:
        """按文件存在判定，不解析内容——损坏文档必须算「存在」，
        否则同 ID 新建会覆盖它。"""
        return self._pfile(pid).is_file()

    def load_project(self, pid: str) -> dict | None:
        return _read(self._pfile(pid))

    def save_project(self, pid: str, data: dict) -> None:
        _write(self._pfile(pid), data)

    # ---- 章节 ----
    def chapter_exists(self, pid: str, cid: str) -> bool:
        """与 `project_exists` 同理按文件存在判定：损坏章节必须算「存在」，
        按内容判会抛 DocumentCorruptError 而非返回 None，建章闸会把仍可人工
        修复的文件当空位覆盖。"""
        return self._cfile(pid, cid).is_file()

    def load_chapter(self, pid: str, cid: str) -> dict | None:
        return _read(self._cfile(pid, cid))

    def save_chapter(self, pid: str, cid: str, data: dict, *, write_file: bool = True) -> None:
        if write_file:
            _write(self._cfile(pid, cid), data)

    def delete_chapter(self, pid: str, cid: str) -> None:
        cf = self._cfile(pid, cid)
        for p in (cf, cf.with_name(cf.name + ".lock"), cf.with_name(cf.name + ".oplock")):
            if p.is_file():
                p.unlink()
