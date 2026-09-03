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

"""供料媒体体检守卫。

**两层结构**（与 test_verify 同范式）：
  ① 纯判定层——吃合成的 ffprobe JSON，查分辨率/宽高比/alpha/无图像流的判据，
     永远跑、不需要 ffmpeg；
  ② 直供闭环层——把 `mediacheck.probe_json` 换成桩（`_probe` 上下文），钉死
     体检在 `supply.supply_image` 内部的**位置**与**处置**：
       · 硬拦发生在 `versioning.archive` 之前（旧图绝不能先被搬进版本栈）；
       · 告警不拦死，一律留痕 `gen.image.inspect`；
       · Studio 那条路（`actions.supply_shot_image`）体检同样生效——体检写在
         CLI 层的话网页上传将完全绕过体检，这条用例专防那种回退。
另附 `@skipUnless(_HAS_FFPROBE)` 冒烟：真 ffprobe 吃真垃圾字节必须硬拦。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from kinema import review, supply
from kinema.errors import FFmpegError, ProjectError
from kinema.pipeline import mediacheck as mc
from kinema.project import Project

_HAS_FFPROBE = shutil.which("ffprobe") is not None

# 1×1 像素合法 PNG（RGBA，零外部依赖；尺寸由 _probe 桩决定，本体只需可拷贝）
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd4000000004945"
    "4e44ae426082")


def _streams(w: int, h: int, pix_fmt: str = "rgb24", codec: str = "png") -> dict:
    """一份 ffprobe JSON 桩（静态图在 ffprobe 里就是 codec_type=video）。"""
    return {"streams": [{"codec_type": "video", "codec_name": codec,
                         "width": w, "height": h, "pix_fmt": pix_fmt}],
            "format": {"format_name": "png_pipe"}}


@contextmanager
def _probe(result):
    """把 probe_json 换成桩：dict=正常返回，Exception 实例=抛出（坏图）。

    同时把 `ffprobe_available` 钉成 True——否则没装 ffmpeg 的机器上体检会
    走「跳过」分支，用例静默失去意义。"""
    def fake(_path):
        if isinstance(result, BaseException):
            raise result
        return result
    with mock.patch.object(mc, "probe_json", fake), \
            mock.patch.object(mc, "ffprobe_available", lambda: True):
        yield


# ---------------------------------------------------------------------------
# ① 纯判定层
# ---------------------------------------------------------------------------
class TestImageInfo(unittest.TestCase):
    def test_picks_first_video_stream(self):
        info = mc.image_info(_streams(1920, 1080, "yuvj420p", "mjpeg"))
        self.assertEqual((info["width"], info["height"]), (1920, 1080))
        self.assertEqual(info["codec"], "mjpeg")

    def test_no_video_stream_is_none(self):
        """改名的假图/纯音频文件——没有图像流 = None = 硬拦。"""
        self.assertIsNone(mc.image_info({"streams": [{"codec_type": "audio"}]}))
        self.assertIsNone(mc.image_info({"streams": []}))
        self.assertIsNone(mc.image_info(None))

    def test_zero_size_stream_rejected(self):
        """文本改名成 .png 时 ffprobe **退出码 0** 且吐出一条
        `codec_type=video` 的流（width=height=0，format_name=image2）——
        只判「有没有 video 流」会把假图放行，必须再判尺寸。"""
        self.assertIsNone(mc.image_info(_streams(0, 0)))


class TestAlpha(unittest.TestCase):
    def test_alpha_families(self):
        for fmt in ("rgba", "bgra", "argb", "abgr", "yuva420p", "ya8",
                    "rgba64le", "gbrap10be"):
            self.assertTrue(mc.has_alpha(fmt), fmt)

    def test_opaque_families(self):
        # pal8 刻意不判 alpha（透明靠 tRNS，pix_fmt 看不出来，宁可漏报不误报）
        for fmt in ("rgb24", "bgr24", "yuv420p", "yuvj444p", "gray",
                    "rgb48be", "pal8", "0rgb", None):
            self.assertFalse(mc.has_alpha(fmt), fmt)


class TestCoverageAndOverflow(unittest.TestCase):
    CANVAS = (1920, 1080)

    def test_exact_canvas_is_clean(self):
        self.assertEqual(mc.cover_coverage((1920, 1080), self.CANVAS), 1.0)
        self.assertEqual(mc.aspect_overflow((1920, 1080), self.CANVAS), 0.0)

    def test_weakest_edge_decides_coverage(self):
        # 宽够高不够：覆盖率取最弱边（cover 缩放的放大倍数由它定）
        self.assertEqual(mc.cover_coverage((3840, 540), self.CANVAS), 0.5)

    def test_portrait_into_landscape_crops_hard(self):
        # 竖图进横画布：cover 取景裁掉约 68%（1080:1920 vs 16:9）
        ov = mc.aspect_overflow((1080, 1920), self.CANVAS)
        self.assertGreater(ov, 0.6)
        self.assertGreater(ov, mc.SUPPLY_ASPECT_TOL)

    def test_degenerate_sizes_do_not_crash(self):
        self.assertEqual(mc.cover_coverage((0, 0), self.CANVAS), 0.0)
        self.assertEqual(mc.aspect_overflow((10, 0), self.CANVAS), 0.0)


class TestImageFindings(unittest.TestCase):
    CANVAS = (1920, 1080)

    def _codes(self, info, canvas=CANVAS):
        hard, warn = mc.image_findings(info, canvas)
        return [f["code"] for f in hard], [f["code"] for f in warn]

    def test_good_image_is_clean(self):
        self.assertEqual(self._codes(mc.image_info(_streams(1920, 1080))), ([], []))

    def test_low_resolution_is_warn_not_hard(self):
        hard, warn = self._codes(mc.image_info(_streams(960, 540)))
        self.assertEqual(hard, [])
        self.assertIn("low_res", warn)

    def test_aspect_mismatch_is_warn_not_hard(self):
        hard, warn = self._codes(mc.image_info(_streams(2160, 2160)))
        self.assertEqual(hard, [])
        self.assertIn("aspect", warn)
        self.assertNotIn("low_res", warn)      # 2160 两边都够，只是不同比

    def test_alpha_is_warn_not_hard(self):
        hard, warn = self._codes(mc.image_info(_streams(1920, 1080, "rgba")))
        self.assertEqual(hard, [])
        self.assertEqual(warn, ["alpha"])

    def test_unreadable_is_the_only_hard_fail(self):
        hard, warn = self._codes(None)
        self.assertEqual(hard, ["unreadable"])
        self.assertEqual(warn, [])

    def test_no_canvas_still_checks_alpha(self):
        hard, warn = self._codes(mc.image_info(_streams(4, 4, "rgba")), None)
        self.assertEqual((hard, warn), ([], ["alpha"]))   # 无基准就不查分辨率


class TestInspectImage(unittest.TestCase):
    def test_report_shape(self):
        with _probe(_streams(1280, 720, "rgba")):
            rep = mc.inspect_image("x.png", canvas=(1920, 1080))
        self.assertTrue(rep["ok"])
        self.assertEqual((rep["width"], rep["height"]), (1280, 720))
        self.assertTrue(rep["alpha"])
        self.assertEqual(rep["canvas"], [1920, 1080])
        self.assertAlmostEqual(rep["coverage"], 0.667, places=2)
        self.assertEqual(rep["crop"], 0.0)                # 同为 16:9
        self.assertEqual([w["code"] for w in rep["warn"]], ["alpha", "low_res"])

    def test_probe_exception_becomes_hard_fail_not_raise(self):
        with _probe(FFmpegError("ffprobe 失败: moov atom not found")):
            rep = mc.inspect_image("fake.png", canvas=(1920, 1080))
        self.assertFalse(rep["ok"])
        self.assertEqual([f["code"] for f in rep["hard_fail"]], ["unreadable"])
        self.assertIn("moov atom", rep["error"])

    def test_missing_ffprobe_skips_instead_of_failing(self):
        """缺工具时体检退化为跳过——护栏不该反过来锁死直供功能。"""
        with mock.patch.object(mc, "ffprobe_available", lambda: False):
            rep = mc.inspect_image("x.png", canvas=(1920, 1080))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["hard_fail"], [])
        self.assertTrue(rep["info"])


# ---------------------------------------------------------------------------
# ② 直供闭环层：体检在 supply_image 内部的位置与处置
# ---------------------------------------------------------------------------
class _Store:
    """ConfigStore 最小替身（体检只用 canvas）。"""

    def __init__(self, canvas=(1920, 1080)):
        self._canvas = canvas

    def canvas(self, aspect):                     # noqa: ARG002  比例无关的固定画布
        return self._canvas


class TestSupplyInspect(unittest.TestCase):
    """`supply.supply_image` 内的体检闸：告警不拦死、硬拦在归档之前、留痕 gen。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        cf = self.d / "ch01.json"
        cf.write_text(json.dumps({
            "id": "t_ch01", "profile": "anime", "aspect": "16:9",
            "shots": [{"id": 1, "narration": "解说"}],
        }, ensure_ascii=False), encoding="utf-8")
        self.proj = Project.load(cf)
        self.store = _Store()

    def tearDown(self):
        self.tmp.cleanup()

    def _png(self, name="asset.png") -> Path:
        p = self.d / name
        p.write_bytes(_PNG_1X1)
        return p

    def test_low_resolution_warns_not_blocks(self):
        with _probe(_streams(640, 360)):
            r = supply.supply_image(self.proj, 1, self._png(), store=self.store)
        s = self.proj.shots[0]
        self.assertTrue(Path(s["image"]).is_file())               # 照样登记
        self.assertEqual(review.get_state(s, "image"), "wfa")
        ins = s["gen"]["image"]["inspect"]
        self.assertTrue(ins["ok"])
        self.assertIn("low_res", [w["code"] for w in ins["warn"]])
        self.assertEqual(r["inspect"]["coverage"], 0.333)          # 留痕可追溯

    def test_aspect_mismatch_warns(self):
        with _probe(_streams(1080, 1920)):                         # 竖图进横画布
            supply.supply_image(self.proj, 1, self._png(), store=self.store)
        ins = self.proj.shots[0]["gen"]["image"]["inspect"]
        self.assertTrue(ins["ok"])
        self.assertIn("aspect", [w["code"] for w in ins["warn"]])
        self.assertGreater(ins["crop"], mc.SUPPLY_ASPECT_TOL)

    def test_corrupt_file_hard_fails(self):
        bad = self.d / "renamed.png"
        bad.write_bytes(b"not an image at all")
        with _probe(FFmpegError("ffprobe 失败: Invalid data found")):
            with self.assertRaises(ProjectError) as cm:
                supply.supply_image(self.proj, 1, bad, store=self.store)
        self.assertIn("体检", str(cm.exception))
        self.assertIn("--skip-check", str(cm.exception))            # 给出逃生舱
        self.assertIsNone(self.proj.shots[0].get("image"))           # 一个字都没写

    def test_inspect_runs_before_archive(self):
        """硬拦必须发生在 versioning.archive 之前——顺序反了这一镜会变成无图。"""
        with _probe(_streams(1920, 1080)):
            supply.supply_image(self.proj, 1, self._png("good.png"), store=self.store)
        s = self.proj.shots[0]
        first = s["image"]
        self.assertTrue(Path(first).is_file())

        bad = self.d / "broken.png"
        bad.write_bytes(b"\x00\x01\x02")
        with _probe(FFmpegError("ffprobe 失败: Invalid data found")):
            with self.assertRaises(ProjectError):
                supply.supply_image(self.proj, 1, bad, store=self.store)
        # 旧图原地未动、版本栈没被写、版本号没涨 —— 归档一步都没跑
        self.assertEqual(s["image"], first)
        self.assertTrue(Path(first).is_file())
        self.assertEqual((s.get("versions") or {}).get("image"), None)
        self.assertEqual(s["gen"]["image"]["version"], 1)
        self.assertFalse((self.proj.workdir / "versions").exists())

    def test_skip_check_bypasses(self):
        """--skip-check：连坏到 ffprobe 解不出的素材也照登（不可再生素材逃生舱）。"""
        bad = self.d / "renamed.png"
        bad.write_bytes(b"not an image at all")
        with _probe(FFmpegError("ffprobe 失败: Invalid data found")):
            r = supply.supply_image(self.proj, 1, bad, store=self.store,
                                    skip_check=True)
        self.assertTrue(r["inspect"]["skipped"])
        self.assertTrue(Path(self.proj.shots[0]["image"]).is_file())
        self.assertEqual(self.proj.shots[0]["gen"]["image"]["inspect"]["skipped"], True)

    def test_ext_gate_still_runs_first(self):
        """后缀闸在体检之前：非图片格式连 ffprobe 都不该跑（体检桩不会被碰）。"""
        gif = self.d / "x.gif"
        gif.write_bytes(b"GIF89a")
        with mock.patch.object(mc, "inspect_image") as spy:
            with self.assertRaises(ProjectError):
                supply.supply_image(self.proj, 1, gif, store=self.store)
        spy.assert_not_called()

    @unittest.skipUnless(_HAS_FFPROBE, "需要 ffprobe")
    def test_real_ffprobe_rejects_garbage(self):
        """冒烟：真 ffprobe 吃真垃圾字节 → 硬拦（桩层之外的一次实证）。"""
        bad = self.d / "fake.png"
        bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)   # 只有魔数的残片
        with self.assertRaises(ProjectError):
            supply.supply_image(self.proj, 1, bad, store=self.store)


class TestStudioSupplyPath(unittest.TestCase):
    """Studio 那条路也必须体检——体检写在 CLI 层的话网页上传将完全绕过体检。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv(); self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.wsp = str(Path(self.tmp.name) / "ws")
        self.ws = Workspace.open(self.wsp)
        self.s = self.ws.create_project("S", pid="sup", profile="anime")
        self.s.create_chapter("第一章")                     # ch01
        cf = self.ws.store.chapter_path("sup", "ch01")
        proj = Project.load(cf)
        proj.data["shots"] = [{"id": 1, "narration": "解说", "dur": 3.0}]
        proj.save()
        self.src = Path(self.wsp) / "sup" / "assets" / "supply" / "up.png"
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.src.write_bytes(_PNG_1X1)

    def tearDown(self):
        self.tmp.cleanup(); self.env.restore()

    def test_studio_path_also_inspected(self):
        from kinema.studio import actions
        with _probe(_streams(320, 180)):
            r = actions.supply_shot_image(self.wsp, "sup", "ch01", shot=1,
                                          path=self.src)
        ins = r["inspect"]
        self.assertTrue(ins["ok"])                            # 告警不拦死
        self.assertIn("low_res", [w["code"] for w in ins["warn"]])
        self.assertEqual(ins["canvas"], [1920, 1080])         # ConfigStore 画布已到位
        proj = Project.load(self.ws.store.chapter_path("sup", "ch01"))
        self.assertTrue(proj.shots[0]["gen"]["image"]["inspect"]["warn"])

    def test_studio_hard_fail_blocks_registration(self):
        from kinema.errors import KinemaError
        from kinema.studio import actions
        with _probe(FFmpegError("ffprobe 失败: Invalid data found")):
            with self.assertRaises((ProjectError, KinemaError)):
                actions.supply_shot_image(self.wsp, "sup", "ch01", shot=1,
                                          path=self.src)
        proj = Project.load(self.ws.store.chapter_path("sup", "ch01"))
        self.assertIsNone(proj.shots[0].get("image"))

    def test_studio_skip_check_bypasses(self):
        from kinema.studio import actions
        with _probe(FFmpegError("ffprobe 失败: Invalid data found")):
            r = actions.supply_shot_image(self.wsp, "sup", "ch01", shot=1,
                                          path=self.src, skip_check=True)
        self.assertTrue(r["inspect"]["skipped"])


if __name__ == "__main__":
    unittest.main()
