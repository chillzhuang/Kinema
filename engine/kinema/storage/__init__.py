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

"""存储层入口：配置发现 + 后端工厂 + 引擎保存钩子。

配置真源 config/storage.yaml（发现顺序同 models.yaml：KINEMA_STORAGE 环境变量
指定路径 > 从 cwd/包位置向上查找 > 缺省 local）。backend 可用环境变量
KINEMA_STORAGE_BACKEND 临时覆盖（便于测试，不动配置文件）。

MySQL 密码解析优先级：环境变量 KINEMA_MYSQL_PASSWORD > config/secrets.local.json
> config/secrets.yaml 同名键 > storage.yaml 的 mysql.password（密钥文件两层经
config_overlay.file_secrets 合并读取）。
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import Storage, atomic_write_json
from .local import LocalStorage

_PASSWORD_ENV = "KINEMA_MYSQL_PASSWORD"
_cfg_cache: dict | None = None
_instances: dict = {}


def _find_config_file() -> Path | None:
    env = os.environ.get("KINEMA_STORAGE")
    if env and Path(env).is_file():
        return Path(env)
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for d in [start, *start.parents]:
            cand = d / "config" / "storage.yaml"
            if cand.is_file():
                return cand
    return None


def _read_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def load_storage_config(*, reload: bool = False) -> dict:
    """返回 {backend, source, mysql:{...含已解析密码}}。"""
    global _cfg_cache
    if _cfg_cache is not None and not reload:
        return _cfg_cache
    path = _find_config_file()
    raw = _read_yaml(path) if path else {}
    backend = os.environ.get("KINEMA_STORAGE_BACKEND") or raw.get("backend") or "local"
    mysql = dict(raw.get("mysql") or {})
    # 密码：env > secrets.local.json > secrets.yaml > storage.yaml
    # 走 config_overlay.file_secrets 而不是就地读 yaml：向导/网页/`config secret`
    # 写的是 secrets.local.json，就地只读 secrets.yaml 会漏掉那一份。
    pw = os.environ.get(_PASSWORD_ENV)
    if not pw and path:
        from ..config_overlay import file_secrets
        pw = str(file_secrets(path.parent).get(_PASSWORD_ENV) or "") or None
    if pw:
        mysql["password"] = pw
    _cfg_cache = {"backend": backend.strip().lower(),
                  "source": str(path) if path else "<default>",
                  "mysql": mysql}
    return _cfg_cache


def get_storage(root: str | Path) -> Storage:
    """按 (root, backend) 缓存的后端实例。backend=mysql 但 PyMySQL 缺失/连不上会在
    首次库操作时给出清晰报错（不静默回退，避免数据分裂）。"""
    cfg = load_storage_config()
    root = Path(root).resolve()
    key = (str(root), cfg["backend"])
    if key not in _instances:
        if cfg["backend"] == "mysql":
            from .mysql import MySQLStorage
            _instances[key] = MySQLStorage(root, cfg["mysql"])
        else:
            _instances[key] = LocalStorage(root)
    return _instances[key]


def notify_saved(path: Path, data: dict) -> None:
    """引擎逐镜 checkpoint 保存钩子（Project.save 调用）。

    章节文件（<ws>/<pid>/chapters/<cid>.json）在 mysql 模式下同步 upsert 入库；
    local 模式与工作区之外的散装 project.json 为 no-op。"""
    if load_storage_config()["backend"] != "mysql":
        return
    p = Path(path)
    if p.parent.name != "chapters":
        return
    ws_root = p.parent.parent.parent
    store = get_storage(ws_root)
    # 文件已由 Project.save 写好，这里只补数据库
    store.save_chapter(p.parent.parent.name, p.stem, data, write_file=False)


__all__ = ["Storage", "LocalStorage", "atomic_write_json", "get_storage",
           "load_storage_config", "notify_saved"]
