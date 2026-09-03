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

"""Studio 异步任务器（stdlib 线程 + 进程内任务表）。

网页端触发的长任务（重新生成 / 局部改造）不挂在 HTTP 长请求上：
POST 立即返回 job_id，任务转后台线程执行，前端 `/api/job?id=` 轮询进度。
两种任务形态：
  · spawn_cli —— 子进程跑一条 kinema CLI（argv 列表拼装，无 shell 注入面；
    继承 Studio 进程环境 → 存储后端/工作区与大屏一致，重生门/血缘/版本栈全生效）；
  · run_fn   —— 进程内函数任务（refine 走这里：同解释器省启动，结果直接入表）。
单用户本地工具的克制实现：内存表 + 有界保留（不做持久化队列），
Studio 重启后进行中任务的产物由 CLI 幂等断点续跑兜底。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_KEEP = 60          # 有界保留：只留最近 N 条已结束任务（running 永不清）
_TIMEOUT = 1800     # 单任务上限（秒）——单镜生图/局改远低于此，防僵尸
_PER_SHOT = 600     # 批量任务的逐镜放宽量（图生视频单镜轮询上限 20 分钟量级）


def _job_timeout(meta: dict | None) -> int:
    """章节级批量任务按 meta.shots 的镜数放宽超时——1800s 是单镜任务的防僵尸
    上限，「交给 Seedance」多镜串行跑会被它腰斩在半途（已花的钱不退）。"""
    shots = str((meta or {}).get("shots") or "")
    n = len([x for x in shots.split(",") if x.strip()])
    return max(_TIMEOUT, _PER_SHOT * n)


def _engine_dir() -> Path:
    return Path(__file__).resolve().parents[2]   # studio/ → kinema/ → engine/


def _new(label: str, meta: dict | None = None) -> str:
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        done = [k for k, v in _JOBS.items() if v["state"] != "running"]
        for k in done[: max(0, len(done) - _KEEP)]:
            _JOBS.pop(k, None)
        _JOBS[jid] = {"id": jid, "label": label, "state": "running",
                      "started": time.time(), "tail": "",
                      "meta": dict(meta or {})}
    return jid


def _finish(jid: str, *, state: str, tail: str = "", **extra) -> None:
    with _LOCK:
        j = _JOBS.get(jid)
        if j is not None:
            j.update(state=state, tail=tail[-2000:],
                     elapsed=round(time.time() - j["started"], 1), **extra)


def _stream(argv: list[str], env: dict | None, jid: str,
            timeout: int = _TIMEOUT) -> tuple[int, str]:
    """跑子进程并**边跑边把输出写回任务表**——`/api/job` 轮询在 running 期间就能
    看到最近的日志尾，而不是等收口才有内容（长任务全程 tail 为空 = 前端只能转圈）。

    stdout/stderr 合流按行读（引擎打印全是行式中文），tail 恒只留最后 2000 字符；
    超时不能靠阻塞读的循环自查（子进程长时间不输出时读不返回），用看门狗计时器
    到点杀进程——与旧 subprocess.run(timeout=…) 的兜底语义一致。"""
    # **stdin 必须断开**：后台任务没有人在键盘前。不显式给 DEVNULL 时子进程继承
    # 服务端的 stdin，而 Studio 常常是从终端起的——引擎里凡是带确认的闸
    #（合成前的 BGM 闸、OSS 上传确认）都会在这里读到一个真 TTY 并停下等输入，
    # 表现是任务毫无输出地卡到看门狗超时被杀。
    p = subprocess.Popen(argv, cwd=_engine_dir(), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         start_new_session=hasattr(os, "setsid"))
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        # CLI 子进程会再派生 ffmpeg 与 provider 轮询等下级进程，只终止直接
        # 子进程会留下继续占用 CPU 的孤儿；起进程时已自立进程组，超时按组
        # 整树终止。非 POSIX 平台或进程组已不存在时退回单进程终止。
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            p.kill()

    dog = threading.Timer(timeout, _kill)
    dog.start()
    buf = ""
    try:
        assert p.stdout is not None
        for line in p.stdout:
            buf = (buf + line)[-2000:]
            with _LOCK:
                j = _JOBS.get(jid)
                if j is not None and j["state"] == "running":
                    j["tail"] = buf
        code = p.wait()
    finally:
        dog.cancel()
    if timed_out.is_set():
        buf = (buf + f"\n[任务超时（>{timeout}s）已被终止]")[-2000:]
    return code, buf


def spawn_argv(argv: list[str], *, label: str, env: dict | None = None,
               meta: dict | None = None, on_success=None) -> str:
    """后台跑任意 argv（列表拼装）。返回 job_id，状态经 status() 轮询。
    meta 是任务的定位名片（project/chapter/shot/kind）——前端凭 active()
    对账恢复忙态：轮询重绘/刷新页面后「生成中」遮罩不丢。

    on_success 只在子进程退出码为 0 时调用（时序在子进程收尾之后，与其
    落盘写入不并发）——给「产物确认生成后才可销毁的凭据」用：批注这类
    输入在提交时清空的话，任务失败即永久丢失。回调自身出错不翻任务状态
    （生成已成功是事实），备注入 tail 供排查。"""
    jid = _new(label, meta)

    def run():
        try:
            code, out = _stream(argv, env, jid, timeout=_job_timeout(meta))
            if code == 0 and on_success is not None:
                try:
                    on_success()
                except Exception as e:   # noqa: BLE001
                    out += f"\n⚠ 任务收尾处理失败（生成本身已成功）：{e}"
            _finish(jid, state=("done" if code == 0 else "failed"),
                    tail=out, code=code)
        except Exception as e:   # noqa: BLE001 —— 任务失败入表，不炸服务线程
            _finish(jid, state="failed", tail=str(e), code=-1)

    threading.Thread(target=run, daemon=True).start()
    return jid


def spawn_cli(args: list[str], *, label: str, ws_root: Path | None = None,
              meta: dict | None = None, on_success=None) -> str:
    """后台跑一条 `python -m kinema <args>`；ws_root 经 KINEMA_WORKSPACE
    显式下发，保证子进程与 Studio 指向同一工作区（与存储后端）。"""
    env = dict(os.environ)
    if ws_root is not None:
        env["KINEMA_WORKSPACE"] = str(ws_root)
    return spawn_argv([sys.executable, "-m", "kinema", *args],
                      label=label, env=env, meta=meta, on_success=on_success)


def spawn_seq(arg_lists: list[list[str]], *, label: str,
              ws_root: Path | None = None, meta: dict | None = None) -> str:
    """顺序跑多条 `python -m kinema <args>`：前一条成功（returncode 0）才跑下一条，
    任一失败即整体 failed（tail 留失败那条输出）。一个 job 覆盖多步——
    「重新构建」= assemble（重烧字幕/特效）→ watermark（刷新水印）串成一条进度。"""
    env = dict(os.environ)
    if ws_root is not None:
        env["KINEMA_WORKSPACE"] = str(ws_root)
    jid = _new(label, meta)

    def run():
        tail = ""
        try:
            for args in arg_lists:
                argv = [sys.executable, "-m", "kinema", *args]
                # tail 逐条覆盖=当前这条的输出
                code, tail = _stream(argv, env, jid, timeout=_job_timeout(meta))
                if code != 0:
                    _finish(jid, state="failed", tail=tail, code=code)
                    return
            _finish(jid, state="done", tail=tail, code=0)
        except Exception as e:   # noqa: BLE001 —— 任务失败入表，不炸服务线程
            _finish(jid, state="failed", tail=str(e), code=-1)

    threading.Thread(target=run, daemon=True).start()
    return jid


def run_fn(fn, *, label: str, meta: dict | None = None) -> str:
    """进程内函数任务：fn() 的返回值（dict）入 job.result；异常入 tail。"""
    jid = _new(label, meta)

    def run():
        try:
            _finish(jid, state="done", result=fn() or {})
        except Exception as e:   # noqa: BLE001
            _finish(jid, state="failed", tail=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jid


def status(jid: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(jid)
        return dict(j) if j else None


def active(project: str | None = None, chapter: str | None = None) -> list[dict]:
    """进行中任务清单（按 meta.project/chapter 过滤）——忙态对账真源。
    章节视图每次渲染拉一次，据此恢复分镜卡的「生成中」状态。"""
    with _LOCK:
        rows = [dict(v) for v in _JOBS.values() if v["state"] == "running"]
    return [j for j in rows
            if (not project or j["meta"].get("project") == project)
            and (not chapter or j["meta"].get("chapter") == chapter)]
