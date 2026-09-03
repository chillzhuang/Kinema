# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""filtergraph 引号上下文与 HTTP 工具层的行为守卫。

drawtext 的 text、ass/fontfile 路径与 concat 清单各经不同层级的解析，字面编码
只能用真实渲染验证：裸 `%` 让 drawtext 整段不渲染而退出码为 0，字符串断言
抓不住这种失败。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from kinema import ffmpeg as ff
from kinema.providers import _util

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_TRICKY = "增长 300% Don't a:b\\c [x],y;z"


def _render(vf_or_fc: str, *, complex_graph: bool, out: Path) -> tuple[int, str]:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "lavfi", "-i", "color=black:s=480x160:d=0.1"]
    if complex_graph:
        args += ["-filter_complex", f"[0:v]{vf_or_fc}[v]", "-map", "[v]"]
    else:
        args += ["-vf", vf_or_fc]
    args += ["-frames:v", "1", str(out)]
    r = subprocess.run(args, capture_output=True, text=True)
    return r.returncode, r.stderr


def _mean_luma(png: Path) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(png), "-vf",
                        "signalstats,metadata=print:file=-", "-f", "null", "-"],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "YAVG" in line:
            return float(line.rsplit("=", 1)[1])
    raise AssertionError("signalstats 未输出 YAVG")


@unittest.skipUnless(_HAS_FFMPEG, "需要 ffmpeg")
class TestDrawtextLiteral(unittest.TestCase):
    """text 值里 `%` `'` `:` `\\` `,` `[` `]` `;` 都要真的渲出来，且在 -vf 与
    -filter_complex 两种图形态下一致。"""

    def _assert_renders(self, text: str, complex_graph: bool):
        font = ff.find_font_cjk()
        fontopt = f":fontfile={ff.filter_literal(font)}" if font else ""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "t.png"
            rc, err = _render(f"drawtext=text={ff.drawtext_text(text)}{fontopt}"
                              ":fontsize=40:fontcolor=white:x=10:y=40",
                              complex_graph=complex_graph, out=out)
            self.assertEqual(rc, 0, err)
            self.assertNotIn("Stray", err)
            self.assertGreater(_mean_luma(out), 17.0, "文字未渲染（画面仍是纯黑）")

    def test_tricky_text_in_vf(self):
        self._assert_renders(_TRICKY, complex_graph=False)

    def test_tricky_text_in_filter_complex(self):
        self._assert_renders(_TRICKY, complex_graph=True)

    def test_consumers_share_the_literal_helpers(self):
        """四处 drawtext 消费点与 ass/fontfile 路径都经同一对助手，不再各自转义。"""
        root = Path(__file__).resolve().parents[1] / "kinema"
        for rel in ("pipeline/transitions.py", "pipeline/watermark.py",
                    "pipeline/cover.py", "pipeline/kenburns.py",
                    "providers/image/mock.py"):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("text='{", src, rel)
            self.assertNotIn("fontfile='{", src, rel)
            self.assertNotIn("fontfile={font}", src, rel)
        compose = (root / "pipeline/compose.py").read_text(encoding="utf-8")
        self.assertIn("ass={filter_literal(subtitle)}", compose)
        self.assertIn("concat_entry(", compose)
        self.assertIn("concat_entry(", (root / "previz.py").read_text(encoding="utf-8"))


@unittest.skipUnless(_HAS_FFMPEG, "需要 ffmpeg")
class TestPathLiterals(unittest.TestCase):
    """路径含单引号、冒号、方括号、分号、CJK 时 ass= 与 concat 清单都要能打开。"""

    def test_ass_path_with_quote(self):
        with tempfile.TemporaryDirectory() as d:
            sub_dir = Path(d) / "it's [1],x;y"
            sub_dir.mkdir()
            ass = sub_dir / "字幕.ass"
            ass.write_text("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
                           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                           "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                           "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                           "Alignment, MarginL, MarginR, MarginV, Encoding\n"
                           "Style: Default,Arial,120,&H00FFFFFF,&H000000FF,&H00000000,"
                           "&H00000000,0,0,0,0,100,100,0,0,1,2,0,5,10,10,10,1\n\n"
                           "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
                           "MarginR, MarginV, Effect, Text\n"
                           "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,HELLO\n",
                           encoding="utf-8")
            out = Path(d) / "o.png"
            rc, err = _render(f"ass={ff.filter_literal(ass)}", complex_graph=False, out=out)
            self.assertEqual(rc, 0, err)
            self.assertGreater(_mean_luma(out), 17.0, "字幕未烧进画面")

    def test_concat_entry_with_quote(self):
        with tempfile.TemporaryDirectory() as d:
            clip_dir = Path(d) / "it's"
            clip_dir.mkdir()
            clip = clip_dir / "a.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "color=black:s=64x64:d=0.2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                           check=True, capture_output=True)
            lst = Path(d) / "list.txt"
            lst.write_text(ff.concat_entry(clip) * 2, encoding="utf-8")
            out = Path(d) / "o.mp4"
            ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
            self.assertGreater(out.stat().st_size, 0)


class _FakeRequests(types.ModuleType):
    """最小 requests 桩：按预设序列返回响应或抛异常，记录调用次数。"""

    class ConnectionError(Exception):
        pass

    class Timeout(Exception):
        pass

    class ReadTimeout(Timeout):
        pass

    def __init__(self, script):
        super().__init__("requests")
        self.exceptions = types.SimpleNamespace(ReadTimeout=self.ReadTimeout)
        self.script = list(script)
        self.calls = 0

    def request(self, method, url, **kw):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestRequestRetryPolicy(unittest.TestCase):
    """读超时意味着请求已送达、服务端可能已受理并计费：创建类 POST 不得盲重。"""

    def _run(self, method, script, **kw):
        fake = _FakeRequests(script)
        with mock.patch.dict(sys.modules, {"requests": fake}), \
                mock.patch("time.sleep"):
            try:
                return _util.request_with_retry(method, "http://x", **kw), fake.calls
            except BaseException as exc:  # noqa: BLE001
                return exc, fake.calls

    def test_post_read_timeout_is_not_retried(self):
        result, calls = self._run("POST", [_FakeRequests.ReadTimeout("rt"),
                                           types.SimpleNamespace(status_code=200)])
        self.assertIsInstance(result, Exception)
        self.assertEqual(calls, 1)

    def test_get_read_timeout_is_retried(self):
        ok = types.SimpleNamespace(status_code=200)
        result, calls = self._run("GET", [_FakeRequests.ReadTimeout("rt"), ok])
        self.assertIs(result, ok)
        self.assertEqual(calls, 2)

    def test_post_connection_failure_is_retried(self):
        ok = types.SimpleNamespace(status_code=200)
        result, calls = self._run("POST", [_FakeRequests.ConnectionError("down"), ok])
        self.assertIs(result, ok)
        self.assertEqual(calls, 2)


class TestAtomicDownload(unittest.TestCase):
    """目标路径上只出现完整文件：断流不得留下半截产物。"""

    def test_interrupted_stream_leaves_no_partial_file(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"head"
                raise fake.ConnectionError("cut")

        fake = _FakeRequests([])
        fake.get = lambda *a, **k: _Resp()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"requests": fake}), \
                mock.patch("time.sleep"):
            out = Path(d) / "img.png"
            with self.assertRaises(fake.ConnectionError):
                _util.download("http://x", out, attempts=2)
            self.assertFalse(out.exists())
            self.assertEqual([p.name for p in Path(d).iterdir()], [])


if __name__ == "__main__":
    unittest.main()
