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

"""并发执行层 —— 把「一张接一张」的 API 生成压成「一批一起」。

## 唯一铁律：**工作线程只产文件，主线程只改文档**

设定图与分镜图的生成天然可并行（各写各的产物路径、provider 是 `__init__` 之后
只读的无状态对象），真正不能并行的是**回填**：`c["sheet"] = path` / `add_cost` /
`review.mark_generated` / `project.save()` 全都在改同一份内存文档并整份写盘。
两个线程各改一半再各自 save，就是经典的丢更新——而且不报错，只是某个角色的
设定图"生成成功了但没登记上"。

所以本模块的契约是：`Task.run` **只准做纯生成**（喂提示词、落自己的产物文件、
返回结果对象），**一行文档都不许碰**；回调 `on_done` 在**主线程**里按**提交顺序**
被调用，回填、记账、落盘全在那里做。

## 一一对应

结果恒携带 `key`（`"character:林深"` 这种身份串），主线程按 key 回填，
**绝不依赖完成顺序**——并发下完成顺序是乱的，按顺序对应就是把甲的图登记到乙头上。
`run()` 返回值与 `on_done` 回调也都按**提交顺序**，故日志与串行时一字不差。

## 重试

`providers/_util.request_with_retry` 已在 HTTP 层退避重试 429/5xx/连接错误，
本层只兜住**逃出那一层的异常**（连接被重置、读超时、偶发写盘失败），且**只重试
明显瞬时的错误**：业务性 4xx（提示词违规、余额不足、参数非法）重试多少次都一样，
只会白白多花钱多等时间。判据见 `is_transient`。

**幂等护栏**：重试前若产物文件已经在盘且非空，直接当成功——有的 provider 会
"文件已经落好了、收尾记账时才抛"，盲目重试等于为同一张图付两次钱。

## 退化路径

`workers <= 1` 时**完全不起线程**，逐个同步执行——mock/单元测试与既有串行行为
保持逐字一致，出问题时可以用 `--concurrency 1` 一键退回老路径对拍。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# 并发上限：再高的收益被 API 限流吃掉，还会把一次失败的爆炸半径放大到十几张图
MAX_WORKERS = 16
DEFAULT_WORKERS = 4
_ENV_WORKERS = "KINEMA_CONCURRENCY"


def resolve_workers(value: int | None) -> int:
    """并发度解析链：显式参数 > 环境变量 `KINEMA_CONCURRENCY` > 缺省 4，钳到 1~16。

    缺省取 4 而不是 1：设定图/分镜图动辄十几张，串行是用户点名的痛点；
    也不取 10：图像 API 普遍有并发配额，冲太高会撞 429 反而更慢（退避在等），
    且一次失败的爆炸半径更大。要更快显式 `--concurrency 10`。
    """
    if value is None:
        raw = os.environ.get(_ENV_WORKERS, "").strip()
        try:
            value = int(raw) if raw else DEFAULT_WORKERS
        except ValueError:
            value = DEFAULT_WORKERS
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_WORKERS
    return max(1, min(MAX_WORKERS, n))


@dataclass
class Task:
    """一件可并行的生成活。

    `run` 必须是**纯生成**：只碰自己的产物路径，不读不写共享文档。
    `out` 给幂等护栏用（重试前先看这个文件在不在）；没有产物文件的活可不填。
    """
    key: str
    run: Callable[[], Any]
    label: str = ""
    out: str | Path | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Done:
    key: str
    ok: bool
    value: Any = None
    error: BaseException | None = None
    attempts: int = 1
    label: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def message(self) -> str:
        return "" if self.error is None else f"{type(self.error).__name__}: {self.error}"


# 明显瞬时、值得重试的错误类型名（按名字判而不是 import，避免为了判类型
# 把 requests/urllib3 变成本模块的硬依赖——引擎核心的 dependencies 是空的）
# 只认**连接层**失败（请求未送达）。读超时与 5xx 不在列：付费请求已送达，
# 服务端可能已受理并计费，业务层再重发就是第二笔账——与 `_util.request_with_retry`
# 同一口径，两层任一放宽都会双付
_TRANSIENT_NAMES = frozenset({
    "ConnectionError", "ConnectTimeout", "ChunkedEncodingError", "ProtocolError",
    "IncompleteRead", "RemoteDisconnected", "SSLError",
})
# 文本里出现这些片段 = 服务端明确未受理（限流/不可用），值得再试一次
_TRANSIENT_HINTS = ("429", "503", "temporarily", "rate limit", "too many requests",
                    "connection reset", "connection aborted")
# 出现这些 = 业务性错误，重试一万次也一样（还会重复计费/重复触发风控）
_FATAL_HINTS = ("api key", "unauthorized", "forbidden", "invalid parameter",
                "content policy", "sensitive", "违规", "余额", "quota exceeded",
                "insufficient", "not found",
                # 鉴权/授权类拒绝（资源未开通、密钥无权）——重试三轮只是多等
                "permission denied", "access denied", "45000000")


def is_transient(exc: BaseException) -> bool:
    """这个异常值不值得再试一次。

    **宁可不重试，也不要重试业务错误**：图像 API 是按次计费的，对着一个
    "提示词违规"重试三次 = 白付三次钱、还多触发两次风控。
    """
    name = type(exc).__name__
    text = str(exc).lower()
    if any(h in text for h in _FATAL_HINTS):
        return False
    if name in _TRANSIENT_NAMES:
        return True
    return any(h in text for h in _TRANSIENT_HINTS)


def _has_output(out) -> bool:
    try:
        p = Path(out)
        return p.is_file() and p.stat().st_size > 0
    except (OSError, TypeError, ValueError):
        return False


def _attempt(task: Task, retries: int, backoff: float) -> Done:
    """在工作线程里跑一件活（含重试）。**永不抛异常**——失败也如实装进 Done，
    否则一张图炸掉会顺手带走整批，而并发批量的价值恰恰是"其余的照常出"。"""
    last: BaseException | None = None
    for i in range(retries + 1):
        try:
            return Done(task.key, True, task.run(), attempts=i + 1,
                        label=task.label, meta=task.meta)
        except (KeyboardInterrupt, SystemExit):
            # 用户中断不是「这件活失败」：装进 Done 会让串行退化路径把剩余的
            # 活照常派下去继续计费，必须原样上抛终止整批
            raise
        except BaseException as e:      # noqa: BLE001  失败要如实上报，不是吞掉
            last = e
            # 幂等护栏：产物已经落好了（provider 在收尾记账时才炸），别再买一次
            if task.out is not None and _has_output(task.out):
                return Done(task.key, True, None, attempts=i + 1,
                            label=task.label, meta={**task.meta, "salvaged": True})
            if i >= retries or not is_transient(e):
                break
            time.sleep(backoff * (2 ** i))
    return Done(task.key, False, error=last, attempts=retries + 1,
                label=task.label, meta=task.meta)


def _fmt_elapsed(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def progress_printer(what: str = "生成", *, stream=None):
    """缺省心跳打印器：TTY 上原地刷新一行，非 TTY（Studio 任务日志）只在数字变化时落一行。

    非 TTY 走"变化才打印"是必须的：Studio 的后台任务把 stdout 存成日志文件，
    每 5 秒刷一次 `\\r` 会在日志里堆成一大坨看不懂的乱码。
    """
    st = stream or sys.stdout
    tty = bool(getattr(st, "isatty", lambda: False)())
    last = {"n": -1}

    def emit(n, total, elapsed, inflight=()):
        if not tty and n == last["n"]:
            return                       # 非 TTY：数字没变就别刷屏（只证明"还活着"没意义）
        last["n"] = n
        tail = ""
        if inflight:
            names = list(inflight)[:3]
            tail = "  进行中: " + "、".join(names) + ("…" if len(inflight) > 3 else "")
        line = f"  ◔ {what} {n}/{total} · 已用 {_fmt_elapsed(elapsed)}{tail}"
        if tty:
            st.write("\r\x1b[2K" + line); st.flush()
        else:
            st.write(line + "\n"); st.flush()

    def close(n, total, elapsed, failed: int = 0):
        if tty:
            st.write("\r\x1b[2K"); st.flush()   # 擦掉心跳行，把版面让给逐项结果
        elif n:
            # 「完成」只在全部成功时说；有失败的批次把成功/失败数分开报——
            # 无人值守的日志里「5/5 完成」会被读成五件都出了
            tally = (f"处理完 · 成功 {n - failed} · 失败 {failed}" if failed
                     else "完成")
            st.write(f"  ◔ {what} {n}/{total} {tally} · 用时 {_fmt_elapsed(elapsed)}\n")
            st.flush()

    emit.close = close
    return emit


def run(tasks: Sequence[Task] | Iterable[Task], *, workers: int = 1,
        retries: int = 2, backoff: float = 1.5,
        on_done: Callable[[Done], Any] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[..., Any] | None = None,
        tick: float = 3.0) -> list[Done]:
    """并发跑一批活；`on_done` 在**主线程**按**提交顺序**回调，返回值同序。

    `should_stop()` 在每次派新活之前问一次——预算断闸这类"别再往下烧了"的信号
    走它：已在飞的活会跑完（钱已经花了，结果要收），但不再派新的。

    **`on_progress` 是这套"按提交顺序消费"设计的必要组成**：
    第一件活慢的时候，后面早已跑完的结果都排在队里不打印，用户看到的就是
    "一直卡着毫无反馈"。故主线程等 head 时按 `tick` 秒醒一次，
    报「已完成 n/总数 · 已用时 · 正在跑哪几件」——顺序化的是**回填**，
    不该顺带把**进度感**也一起顺序化了。
    """
    items = list(tasks)
    out: list[Done] = []
    if not items:
        return out
    total = len(items)
    t0 = time.monotonic()
    state = {"n": 0}
    lock = threading.Lock()
    inflight: dict[str, None] = {}        # 有序去重（dict 保序），用作"正在跑哪几件"

    def _tracked(t: Task) -> Done:
        with lock:
            inflight[t.label or t.key] = None
        try:
            return _attempt(t, retries, backoff)
        finally:
            with lock:
                state["n"] += 1
                inflight.pop(t.label or t.key, None)

    def _beat():
        if on_progress:
            with lock:
                n, live = state["n"], list(inflight)
            on_progress(n, total, time.monotonic() - t0, live)

    # 退化路径：单并发时一个线程都不起，与既有串行行为逐字一致
    if workers <= 1:
        for t in items:
            if should_stop and should_stop():
                break
            d = _tracked(t)
            out.append(d)
            if on_done:
                on_done(d)
        _finish(on_progress, out, total, time.monotonic() - t0)
        return out

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="kn-gen") as ex:
        it = iter(items)
        pending: list[Future] = []

        def _submit_next() -> bool:
            if should_stop and should_stop():
                return False
            t = next(it, None)
            if t is None:
                return False
            pending.append(ex.submit(_tracked, t))
            return True

        for _ in range(workers):
            if not _submit_next():
                break
        while pending:
            # **按提交顺序取**（不是 as_completed）：主线程的回填与日志因此与串行
            # 同序，人看日志、机器对账都不会被"谁先跑完"打乱。
            # 等待期间按 tick 醒来播报进度——否则 head 慢时全场静默。
            fut = pending[0]
            while True:
                try:
                    d = fut.result(timeout=tick)
                    break
                except _FutureTimeout:
                    _beat()
            pending.pop(0)
            out.append(d)
            if on_done:
                on_done(d)
            _submit_next()
    _finish(on_progress, out, total, time.monotonic() - t0)
    return out


def _finish(on_progress, results: list[Done], total, elapsed) -> None:
    close = getattr(on_progress, "close", None)
    if close:
        close(len(results), total, elapsed,
              failed=sum(1 for d in results if not d.ok))


def summarize(results: Sequence[Done]) -> dict:
    """批次小结：成功/失败/重试过的/被幂等护栏救回的。"""
    ok = [d for d in results if d.ok]
    return {
        "total": len(results),
        "ok": len(ok),
        "failed": [d for d in results if not d.ok],
        "retried": [d for d in ok if d.attempts > 1],
        "salvaged": [d for d in ok if d.meta.get("salvaged")],
    }
