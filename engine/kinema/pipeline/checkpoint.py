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

"""断点续跑的产物判据：字段指向的产物在盘即视为已产出。

「被改」由审阅状态机（retake）与血缘指纹判，不在这里。
OSS 适配：字段值为 URL（已上云）视为「已产出」——避免误重生成烧钱；
真正渲染时由 media.ensure_local 拉回本地。
"""
from __future__ import annotations

from pathlib import Path


def has_file(path: str | None) -> bool:
    if not path:
        return False
    from ..storage.media import is_url, localize
    if is_url(path):
        return True                      # 已上云 → 视为存在（本地缺失时渲染前会拉回）
    return Path(localize(path)).is_file()


def mark(shot: dict, status: str) -> None:
    shot["status"] = status
