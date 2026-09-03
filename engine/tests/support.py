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

"""测试公共设施：FakeProject 桩（复刻 Project.subdir/save 接口）与本地存储环境守卫。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def fake_path(*parts: str) -> str:
    """占位路径：只参与字符串/Path 运算、不要求在盘的测试路径统一从这里取。

    落在系统临时目录下的独立命名空间——跨平台成立，且不会指向真实文件；
    POSIX 字面量（/tmp/x 一类）在 Windows 上没有对应语义。"""
    return str(Path(tempfile.gettempdir(), "kinema-fixture", *parts))


class FakeProject:
    """复刻 versioning/candidates/batch 所依赖的 Project 最小接口。

    只提供 subdir(name)（工作目录下建子目录并返回）、save()（计数，不落盘）
    与 data（章节文档 dict）——避免 import 正在并行修改的 kinema.project。
    """

    def __init__(self, workdir, data: dict | None = None):
        self.workdir = Path(workdir)
        self.data = data if data is not None else {}
        self.saved = 0

    def subdir(self, name: str) -> Path:
        d = self.workdir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self) -> None:
        self.saved += 1


class LocalBackendEnv:
    """强制 KINEMA_STORAGE_BACKEND=local 并刷新存储配置缓存的环境守卫。

    storage/__init__ 有模块级 _cfg_cache，setUp/tearDown 各 reload 一次，
    保证测试互不污染、也不受外部 shell 环境影响。
    """

    _KEY = "KINEMA_STORAGE_BACKEND"

    def enable(self) -> None:
        self._prev = os.environ.get(self._KEY)
        os.environ[self._KEY] = "local"
        from kinema.storage import load_storage_config
        load_storage_config(reload=True)

    def restore(self) -> None:
        if self._prev is None:
            os.environ.pop(self._KEY, None)
        else:
            os.environ[self._KEY] = self._prev
        from kinema.storage import load_storage_config
        load_storage_config(reload=True)
