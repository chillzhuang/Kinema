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

"""章节文档的跨进程文件锁（POSIX flock / Windows msvcrt 双实现）。

两种粒度共用同一底座，锁均随文件句柄关闭自动释放，进程异常退出不留残锁：

  · 文档写锁 `save_lock`（阻塞）——只覆盖「合并 → 原子写盘」的毫秒级窗口。
    引擎 save、Studio 表态与 Agent Gateway apply 全部经过它；合并只覆盖表态键，
    其余字段以持有操作锁的一方为准。
  · 操作锁 `op_lock`（非阻塞、同线程可重入）——生成/合成等会改产物或消耗
    预算的整段操作，以及改作者字段与 `shots[]` 结构的编辑（Gateway apply、
    批量改词、Studio 编辑口）独占章节。第二个操作在准入时即失败并给出持有者
    信息，而不是与首个操作各持旧副本交错写盘；预算判定也因此在同一章节内串行。
    可重入是 `run` 的需要：它在同一线程内串多个阶段，每段各申请一次。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .errors import KinemaError


class FileLock:
    """单文件互斥锁。blocking=False 时冲突立即抛 KinemaError(conflict_msg)。"""

    def __init__(self, lock_path: Path, *, blocking: bool,
                 conflict_msg: str = "资源正被其他进程占用"):
        self.path = Path(lock_path)
        self.blocking = blocking
        self.conflict_msg = conflict_msg
        self.handle = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                # LK_LOCK 只重试十次即抛，不是真正的阻塞：阻塞语义靠非阻塞申请循环等
                while True:
                    try:
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if not self.blocking:
                            raise
                        time.sleep(0.05)
            else:
                import fcntl
                flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
                fcntl.flock(self.handle.fileno(), flags)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise KinemaError(self.conflict_msg) from exc
        return self

    def release(self) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None

    __enter__ = acquire

    def __exit__(self, exc_type, exc, tb):
        self.release()


def save_lock(doc_path) -> FileLock:
    """文档写锁（阻塞）。持有窗口只包住合并、原子写与数据库同步回调，
    不得跨越云端生成等长耗时调用。"""
    p = Path(doc_path)
    return FileLock(p.with_suffix(p.suffix + ".lock"), blocking=True,
                    conflict_msg=f"文档写锁不可用: {p}")


# 进程内重入登记：同一线程对同一章节的嵌套申请视为同一持有者。键带线程号——
# Studio 的请求线程各自独立，按路径判重入会让两个线程同时进入同一章节的操作
_HELD: dict[tuple[int, str], int] = {}
_HELD_GUARD = threading.Lock()


def _held_key(path: Path) -> tuple[int, str]:
    # 同一把锁可能经相对/绝对/软链三种写法申请，重入登记按真实文件判
    return threading.get_ident(), str(Path(path).resolve())


class _OpLock:
    def __init__(self, doc_path: Path, kind: str):
        self.doc = Path(doc_path)
        self.kind = kind
        self.path = self.doc.with_suffix(self.doc.suffix + ".oplock")
        self.lock: FileLock | None = None
        self.nested = False

    def _holder(self) -> str:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8"))
            return f"{info.get('kind', '?')}（pid {info.get('pid', '?')}，{info.get('started', '?')} 起）"
        except Exception:  # noqa: BLE001  持有者信息尽力而为，读不出不影响拒绝语义
            return "未知操作"

    def __enter__(self) -> "_OpLock":
        key = _held_key(self.path)
        with _HELD_GUARD:
            if key in _HELD:
                _HELD[key] += 1
                self.nested = True
                return self
        lock = FileLock(self.path, blocking=False)
        try:
            lock.acquire()
        except KinemaError as exc:
            raise KinemaError(
                f"章节已有操作在执行：{self._holder()}——"
                f"同一章节的生成/合成任务串行执行，等它结束后重试") from exc
        self.lock = lock
        if os.name != "nt":     # NT 下第 0 字节被锁定，不可截写持有者信息
            try:                # 持有者信息只服务冲突提示，写失败不影响锁语义
                lock.handle.truncate(0)
                lock.handle.write(json.dumps(
                    {"kind": self.kind, "pid": os.getpid(),
                     "started": datetime.now().isoformat(timespec="seconds")},
                    ensure_ascii=False).encode("utf-8"))
                lock.handle.flush()
            except OSError:
                pass
        with _HELD_GUARD:
            _HELD[key] = 1
        return self

    def __exit__(self, exc_type, exc, tb):
        key = _held_key(self.path)
        with _HELD_GUARD:
            if self.nested:
                _HELD[key] -= 1
                return
            _HELD.pop(key, None)
        if self.lock is not None:
            self.lock.release()


def op_lock(doc_path, kind: str) -> _OpLock:
    """章节操作锁（非阻塞、进程内可重入）。kind 用于冲突提示中的持有者描述。"""
    return _OpLock(Path(doc_path), kind)

