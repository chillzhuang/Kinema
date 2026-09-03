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

"""ffmpeg 双原语守卫：探测原语 `run_capture`（可调日志级别·永不抛异常）
与渲染原语 `run`（写死 -loglevel error·失败抛 FFmpegError）的契约互不侵犯。

`run()` 被 compose/kenburns/transitions/watermark 全线使用，其签名与语义是
稳定契约——本模块的回归断言就是那道红线：改 run_capture 时不许顺手动 run。"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from kinema import ffmpeg
from kinema.errors import FFmpegError

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class _FakeProc:
    """subprocess.run 的最小返回桩（只带被读取的三项）。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunCaptureContract(unittest.TestCase):
    """命令行拼装与返回值形状（不依赖系统 ffmpeg）。"""

    def test_default_loglevel_is_info_and_flags_prefixed(self):
        with mock.patch.object(subprocess, "run",
                               return_value=_FakeProc()) as m:
            ffmpeg.run_capture(["-i", "a.mp4", "-vf", "blackdetect", "-f", "null", "-"])
        cmd = m.call_args.args[0]
        # 前缀由本函数拼：探测默认 info 级（分析类滤镜的结论只在 info 级 stderr）
        self.assertEqual(cmd[:5], ["ffmpeg", "-hide_banner", "-loglevel", "info", "-y"])
        self.assertEqual(cmd[5:], ["-i", "a.mp4", "-vf", "blackdetect", "-f", "null", "-"])
        self.assertTrue(m.call_args.kwargs["capture_output"])
        self.assertTrue(m.call_args.kwargs["text"])

    def test_loglevel_is_overridable(self):
        with mock.patch.object(subprocess, "run", return_value=_FakeProc()) as m:
            ffmpeg.run_capture(["-i", "a.mp4"], loglevel="verbose")
        self.assertEqual(m.call_args.args[0][:5],
                         ["ffmpeg", "-hide_banner", "-loglevel", "verbose", "-y"])

    def test_returns_triple_and_never_raises_on_failure(self):
        with mock.patch.object(subprocess, "run",
                               return_value=_FakeProc(1, "out", "boom")):
            rc, out, err = ffmpeg.run_capture(["-i", "nope.mp4"], desc="探测")
        self.assertEqual((rc, out, err), (1, "out", "boom"))

    def test_none_streams_normalized_to_empty_str(self):
        # 某些平台/参数组合下 stdout/stderr 可能为 None，调用方一律拿到 str
        with mock.patch.object(subprocess, "run",
                               return_value=_FakeProc(0, None, None)):
            rc, out, err = ffmpeg.run_capture(["-i", "a.mp4"])
        self.assertEqual((rc, out, err), (0, "", ""))


class TestRunUnchanged(unittest.TestCase):
    """`run()` 的签名与语义零变化（M1/M7/M10/M16 只许加原语，不许改它）。"""

    def test_render_primitive_still_pins_loglevel_error(self):
        with mock.patch.object(subprocess, "run", return_value=_FakeProc()) as m:
            ret = ffmpeg.run(["-i", "a.mp4", "out.mp4"], desc="合成")
        self.assertIsNone(ret)                                   # 成功返回 None
        self.assertEqual(m.call_args.args[0][:5],
                         ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"])

    def test_failure_raises_with_last_15_stderr_lines(self):
        stderr = "\n".join(f"line{i}" for i in range(1, 21))      # 20 行
        with mock.patch.object(subprocess, "run",
                               return_value=_FakeProc(1, "", stderr)):
            with self.assertRaises(FFmpegError) as ctx:
                ffmpeg.run(["-i", "a.mp4", "out.mp4"], desc="合成")
        msg = str(ctx.exception)
        self.assertIn("合成", msg)
        self.assertIn("line20", msg)
        self.assertIn("line6", msg)                              # 尾 15 行 = 6..20
        self.assertNotIn("line5", msg)


@unittest.skipUnless(_HAS_FFMPEG, "需要系统 ffmpeg 做探测原语冒烟")
class TestRunCaptureSmoke(unittest.TestCase):
    """真机冒烟：失败不抛 + info 级能拿到分析滤镜的结论行。"""

    def test_missing_input_returns_nonzero_text(self):
        rc, _out, err = ffmpeg.run_capture(
            ["-i", "__no_such_file__.mp4", "-f", "null", "-"], desc="探测")
        self.assertNotEqual(rc, 0)
        self.assertTrue(err.strip())                             # 报错文本拿得到

    def test_blackdetect_line_visible_at_info_level(self):
        # 纯黑合成源 → blackdetect 必出 black_start；这行正是 run() 丢弃的东西
        rc, _out, err = ffmpeg.run_capture([
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=10",
            "-vf", "blackdetect=d=0.2:pic_th=0.98",
            "-f", "null", "-",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("black_start", err)


class TestTimeout(unittest.TestCase):
    """单次调用超时上限：防「父进程活着、ffmpeg 异常空转」（孤儿另有收割）。"""

    def test_run_timeout_kills_and_raises_ffmpeg_error(self):
        # -re 按实时读速：3s 素材至少要跑 3s → 1s 超时必触发；subprocess 会先杀子进程
        t0 = time.monotonic()
        with self.assertRaises(FFmpegError) as cm:
            ffmpeg.run(["-re", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3:r=10",
                        "-f", "null", "-"], timeout=1)
        self.assertLess(time.monotonic() - t0, 6, "超时后必须立刻返回，不许等素材跑完")
        self.assertIn("超时", str(cm.exception))

    def test_run_capture_timeout_returns_124_never_raises(self):
        os.environ["KINEMA_FFMPEG_TIMEOUT"] = "1"
        try:
            rc, _out, err = ffmpeg.run_capture(
                ["-re", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=3:r=10",
                 "-f", "null", "-"])
        finally:
            os.environ.pop("KINEMA_FFMPEG_TIMEOUT", None)
        self.assertEqual(rc, 124, "超时按 GNU timeout 语义回 124（永不抛异常契约不变）")
        self.assertIn("超时", err)

    def test_zero_or_bad_env_means_no_limit(self):
        for v in ("0", "-3", "abc"):
            os.environ["KINEMA_FFMPEG_TIMEOUT"] = v
            try:
                self.assertIsNone(ffmpeg._default_timeout(), v)
            finally:
                os.environ.pop("KINEMA_FFMPEG_TIMEOUT", None)


class TestOrphanReaper(unittest.TestCase):
    """孤儿 ffmpeg 收割：父进程被 SIGKILL 后子进程无人认领，一只渲染孤儿
    就能在临时目录连烧几天 CPU。识别判据**双重且缺一不可**：
    PPID=1（父已死）× 命令行带产物路径签名 `_work/`（不是我们的绝不动手——
    截屏录制类软件的常驻 ffmpeg PPID 也是 1，全靠签名区分）。"""

    LINES = [
        "  76384     1 ffmpeg -hide_banner -y -i /tmp/x/ch01_work/clips/s1.png out.mp4",
        "  76385   500 ffmpeg -hide_banner -y -i /tmp/x/ch01_work/clips/s2.png out.mp4",
        "  76386     1 ffmpeg -f avfoundation -i 1 /Users/x/Movies/screen.mp4",
        "  76387     1 grep ffmpeg _work/",
        "  76388     1 /opt/homebrew/bin/ffmpeg -i /w/proj/ch02_work/build/a.mp4 b.mp4",
    ]

    def test_detection_requires_ppid1_and_work_signature(self):
        got = ffmpeg.find_orphan_ffmpeg(ps_lines=self.LINES)
        self.assertEqual([o["pid"] for o in got], [76384, 76388])
        # 76385=父进程活着（合法渲染）；76386=别人的常驻 ffmpeg（无签名）；
        # 76387=不是 ffmpeg 二进制本体（grep）——一个都不许碰

    def test_reap_kills_only_matched_and_swallows_gone(self):
        killed = []
        def fake_kill(pid, sig):
            killed.append((pid, sig))
            if pid == 76388:
                raise ProcessLookupError   # 收割瞬间自己退了——不算错
        got = ffmpeg.reap_orphan_ffmpeg(kill=True, ps_lines=self.LINES, _kill=fake_kill)
        self.assertEqual([p for p, _ in killed], [76384, 76388])
        self.assertEqual(len(got), 2)

    def test_report_mode_never_kills(self):
        killed = []
        ffmpeg.reap_orphan_ffmpeg(kill=False, ps_lines=self.LINES,
                                  _kill=lambda *a: killed.append(a))
        self.assertEqual(killed, [], "doctor 的报告模式只侦察不动手")

    def test_studio_startup_reaps_and_doctor_only_reports(self):
        """接线守卫：studio 启动=自动收割（此刻合法渲染的父进程都活着，零误伤）；
        doctor=只报告（检查命令不该有副作用）。"""
        srv = (Path(__file__).resolve().parents[1] / "kinema" / "studio"
               / "server.py").read_text(encoding="utf-8")
        seg = srv.split("def serve(")[1]
        self.assertIn("reap_orphan_ffmpeg(kill=True)", seg)
        cli = (Path(__file__).resolve().parents[1] / "kinema"
               / "cli.py").read_text(encoding="utf-8")
        doc = cli.split("def cmd_doctor(")[1].split("\ndef ")[0]
        self.assertIn("find_orphan_ffmpeg()", doc)
        self.assertNotIn("reap_orphan_ffmpeg(kill=True)", doc)


if __name__ == "__main__":
    unittest.main()
