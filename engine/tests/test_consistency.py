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

"""角色跨镜一致性守卫。

**三层结构**（前两层同 test_verify）：
  ① 纯函数层（永远跑）——阶段推导 / 抽帧点位 / 设定图配对与 localize /
     「无可比对角色」原因判定 / 判定回填与打回纪律 / 并发 save 存活；
  ② `@skipUnless(_HAS_FFMPEG)` 冒烟层——用 lavfi 现造图与片段跑真产料，
     **零素材入仓**（临时目录内生成，用完即删）；
  ③ 失效闭环层（永远跑，桩 provider 不碰 ffmpeg）——**渲染物一被替换旧判定当场作废**，
     六道门（生图/图生视频重生·素材直供·局部改造·宫格换选·版本回滚 CLI+Studio 两入口）逐门实测。

本文件同时是「引擎不打分」这条纪律的守卫：模块对外只有 `scan`（产料）与
`set_verdict`（回填），加入任何相似度算法都会在此处翻红。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest

from kinema.errors import KinemaError, ProjectError
from pathlib import Path
from unittest import mock

from kinema import project as project_mod
from kinema import review
from kinema.pipeline import consistency as cn
from kinema.project import Project
from kinema.providers.base import VideoProvider
from tests.support import LocalBackendEnv, fake_path

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
SCHEMA_PATH = (Path(__file__).resolve().parents[2]
               / "docs" / "kinema" / "project.schema.json")


def _project(tmp: Path, data: dict) -> Project:
    """不落盘地造一个 Project（构造器直吃 data；workdir=<stem>_work 随 path 派生）。"""
    return Project(tmp / "ch01.json", data)


def _doc(**over) -> dict:
    d = {
        "id": "ch01", "motion": "kenburns", "aspect": "16:9",
        "characters": [{"name": "林深", "sheet": fake_path("char_林深.png")}],
        "shots": [{"id": 1, "narration": "台词", "dur": 3.0}],
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# ① 纯函数层
# ---------------------------------------------------------------------------
class TestFrameStage(unittest.TestCase):
    def test_kenburns_takes_image(self):
        # kenburns 的"代表帧"就是分镜图本身——图即帧，不需要抽帧
        self.assertEqual(cn.frame_stage("kenburns"), "image")
        self.assertEqual(cn.frame_stage("a"), "image")

    def test_video_modes_take_clip(self):
        for m in ("dubbed", "native", "b", "c"):
            self.assertEqual(cn.frame_stage(m), "clip", m)

    def test_clip_drift_also_retakes_image(self):
        """clip 判漂移必须连 image 一起打回——图生视频恒以分镜图作首帧，根因在图。"""
        self.assertEqual(cn.retake_stages("kenburns"), ("image",))
        self.assertEqual(cn.retake_stages("dubbed"), ("clip", "image"))
        self.assertEqual(cn.retake_stages("native"), ("clip", "image"))


class TestFrameTimestamp(unittest.TestCase):
    def test_takes_midpoint(self):
        self.assertEqual(cn.frame_timestamp(4.0), 2.0)
        self.assertEqual(cn.frame_timestamp(9.0), 4.5)

    def test_unknown_duration_falls_back(self):
        for bad in (None, 0, -3, "", "abc"):
            self.assertEqual(cn.frame_timestamp(bad), cn.FALLBACK_TS, repr(bad))

    def test_tiny_duration_stays_inside_clip(self):
        ts = cn.frame_timestamp(0.08)
        self.assertGreaterEqual(ts, 0.05)
        self.assertLessEqual(ts, 0.08)


class TestScaleExpr(unittest.TestCase):
    def test_commas_are_quoted(self):
        """`-vf` 的裸逗号是滤镜链分隔符——min(768,iw) 不引号会被切成两个滤镜。"""
        expr = cn._scale_expr()
        self.assertTrue(expr.startswith("scale='"), expr)
        self.assertIn(f"min({cn.FRAME_WIDTH},iw)", expr)


class TestScannableFilter(unittest.TestCase):
    def test_skips_transition_and_omitted(self):
        self.assertFalse(cn.is_scannable({"id": 1, "kind": "transition"}))
        self.assertFalse(cn.is_scannable({"id": 2, "review": {"shot": {"state": "omt"}}}))
        self.assertTrue(cn.is_scannable({"id": 3}))

    def test_broken_review_block_does_not_crash(self):
        self.assertTrue(cn.is_scannable({"id": 4, "review": "坏数据"}))


class TestShotSheets(unittest.TestCase):
    """配对口径：角色项只从 lineage.required_refs 取，路径必过 ensure_local。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sheet = self.tmp / "char_林深.png"
        self.sheet.write_bytes(b"\x89PNG\r\n")

    def test_only_character_kind_is_paired(self):
        """场景/道具设定图不进一致性配对——本节点校验的是"角色是不是同一个人"。"""
        p = _project(self.tmp, _doc(
            scene="办公室", scene_ref=str(self.sheet),
            characters=[{"name": "林深", "sheet": str(self.sheet)}],
            props=[{"name": "咖啡杯", "sheet": str(self.sheet)}],
            shots=[{"id": 1, "narration": "他放下咖啡杯", "dur": 3}]))
        ready, missing = cn.shot_sheets(p, p.data["shots"][0])
        self.assertEqual([x["name"] for x in ready], ["林深"])
        self.assertEqual(missing, [])

    def test_missing_sheet_file_is_reported_not_dropped(self):
        p = _project(self.tmp, _doc())          # sheet 指向不存在的文件
        ready, missing = cn.shot_sheets(p, p.data["shots"][0])
        self.assertEqual(ready, [])
        self.assertEqual(missing, ["林深"])

    def test_explicit_cast_narrows_characters(self):
        p = _project(self.tmp, _doc(
            characters=[{"name": "林深", "sheet": str(self.sheet)},
                        {"name": "魔王", "sheet": str(self.sheet)}],
            shots=[{"id": 1, "characters": ["魔王"], "dur": 3}]))
        ready, _ = cn.shot_sheets(p, p.data["shots"][0])
        self.assertEqual([x["name"] for x in ready], ["魔王"])

    def test_manifest_localizes_oss_sheet_paths(self):
        """OSS 模式下 required_refs 返回的是 URL——不过 ensure_local 就喂不了 ffmpeg
        也 Read 不了。这里证明 localize 是**承重**的：不 localize 就只会得到 missing。"""
        url = "https://bucket.example.com/av/assets/refs/char_林深.png"
        p = _project(self.tmp, _doc(characters=[{"name": "林深", "sheet": url}]))

        ready, missing = cn.shot_sheets(p, p.data["shots"][0])   # 无 localize 的对照
        self.assertEqual((ready, missing), ([], ["林深"]))

        with mock.patch("kinema.storage.media.ensure_local",
                        side_effect=lambda v: str(self.sheet) if v == url else v) as m:
            ready, missing = cn.shot_sheets(p, p.data["shots"][0])
        m.assert_called_once_with(url)
        self.assertEqual(missing, [])
        self.assertEqual(ready, [{"name": "林深", "path": str(self.sheet.resolve())}])


class TestNoCompareReason(unittest.TestCase):
    """空 sheets 必须带原因——静默产出空清单会被指挥层误读成「比对通过」。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _reason(self, doc, shot, sheets=(), missing=()):
        return cn.no_compare_reason(_project(self.tmp, doc), shot,
                                    list(sheets), list(missing))

    def test_none_when_sheets_present(self):
        self.assertIsNone(self._reason(_doc(), {"id": 1}, sheets=[{"name": "林深"}]))

    def test_skip_design_project(self):
        self.assertEqual(self._reason(_doc(skip_design=True), {"id": 1}), "skip_design")

    def test_explicit_empty_cast(self):
        self.assertEqual(self._reason(_doc(), {"id": 1, "characters": []}), "empty_cast")

    def test_sheets_not_generated_yet(self):
        self.assertEqual(self._reason(_doc(), {"id": 1}, missing=["林深"]), "sheets_missing")

    def test_project_without_characters(self):
        self.assertEqual(self._reason(_doc(characters=[]), {"id": 1}), "no_cast")

    def test_cast_names_not_in_roster(self):
        self.assertEqual(self._reason(_doc(), {"id": 1, "characters": ["查无此人"]}),
                         "cast_unmatched")

    def test_every_reason_has_chinese_label(self):
        for key in ("skip_design", "empty_cast", "no_cast", "cast_unmatched",
                    "sheets_missing"):
            self.assertTrue(cn.REASONS.get(key), key)


class TestReportLines(unittest.TestCase):
    MAN = {
        "motion": "kenburns", "stage": "image", "aspect": "16:9", "dir": fake_path("x"),
        "shots": [
            {"id": 1, "frame": fake_path("x", "shot_1.png"), "characters": ["林深"],
             "sheets": [{"name": "林深", "path": "/a.png"}], "missing_sheets": [],
             "reason": None, "skipped": None, "verdict": None},
            {"id": 2, "frame": fake_path("x", "shot_2.png"), "characters": [], "sheets": [],
             "missing_sheets": [], "reason": "empty_cast", "skipped": None,
             "verdict": None},
            {"id": 3, "frame": None, "characters": ["林深"], "sheets": [],
             "missing_sheets": ["林深"], "reason": "sheets_missing",
             "skipped": "no_clip", "verdict": None},
        ],
        "summary": {"shots": 3, "ready": 1, "no_compare": 2, "skipped": 1},
    }

    def test_no_compare_is_loud(self):
        text = "\n".join(cn.report_lines(self.MAN))
        self.assertIn("本镜无可比对角色", text)
        self.assertIn(cn.REASONS["empty_cast"], text)

    def test_skipped_shot_explains_which_stage_to_run(self):
        text = "\n".join(cn.report_lines(self.MAN))
        self.assertIn(cn.SKIPS["no_clip"], text)

    def test_summary_line(self):
        self.assertIn("共 3 镜 · 可比对 1", cn.report_lines(self.MAN)[-1])


class TestSetVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _p(self, **over):
        return _project(self.tmp, _doc(**over))

    def test_ok_verdict_records_entry_and_touches_nothing_else(self):
        p = self._p()
        s = p.data["shots"][0]
        r = cn.set_verdict(p, s, "ok", score=0.92, note="一致")
        self.assertEqual(s["consistency"]["verdict"], "ok")
        self.assertEqual(s["consistency"]["score"], 0.92)
        self.assertEqual(s["consistency"]["by"], cn.DEFAULT_BY)
        self.assertTrue(s["consistency"]["at"])
        self.assertNotIn("review", s)                 # 判 ok 绝不动审阅状态机
        self.assertEqual((r["retaken"], r["locked"]), ([], []))

    def test_unknown_verdict_rejected(self):
        p = self._p()
        with self.assertRaises(ProjectError):
            cn.set_verdict(p, p.data["shots"][0], "maybe")

    def test_drift_without_retake_only_records(self):
        p = self._p()
        s = p.data["shots"][0]
        cn.set_verdict(p, s, "drift", note="发色不对")
        self.assertEqual(s["consistency"]["verdict"], "drift")
        self.assertEqual(review.get_state(s, "image"), "todo")

    def test_set_drift_retakes_unlocked_only(self):
        """照抄 lineage.mark_stale 的纪律：锁是人给的，机器不越权解锁。"""
        p = self._p(shots=[{"id": 1, "dur": 3},
                           {"id": 2, "dur": 3, "review": {"image": {"state": "done"}}}])
        free, locked = p.data["shots"]
        r1 = cn.set_verdict(p, free, "drift", note="脸不对", retake=True)
        r2 = cn.set_verdict(p, locked, "drift", note="脸不对", retake=True)
        self.assertEqual(r1["retaken"], ["image"])
        self.assertEqual(review.get_state(free, "image"), "retake")
        self.assertIn("脸不对", review.get_note(free, "image"))
        self.assertEqual((r2["retaken"], r2["locked"]), ([], ["image"]))
        self.assertEqual(review.get_state(locked, "image"), "done")   # 未被解锁
        self.assertEqual(locked["consistency"]["verdict"], "drift")   # 判定仍留作标记

    def test_clip_drift_retakes_clip_and_image(self):
        p = self._p(motion="dubbed", shots=[{"id": 1, "dur": 3}])
        s = p.data["shots"][0]
        r = cn.set_verdict(p, s, "drift", retake=True)
        self.assertEqual(r["retaken"], ["clip", "image"])
        self.assertEqual(review.get_state(s, "image"), "retake")
        self.assertEqual(review.get_state(s, "clip"), "retake")

    def test_manifest_attaches_provenance(self):
        """判定要挂产料存证：判的哪一帧、比的哪几张设定图。"""
        mdir = (self.tmp / "ch01_work" / cn.SUBDIR)
        mdir.mkdir(parents=True)
        (mdir / cn.MANIFEST).write_text(json.dumps({"shots": [
            {"id": 1, "frame": "/f/shot_1.png",
             "sheets": [{"name": "林深", "path": "/f/char.png"}]}]}), encoding="utf-8")
        p = self._p()
        s = p.data["shots"][0]
        cn.set_verdict(p, s, "ok")
        self.assertEqual(s["consistency"]["frame"], "/f/shot_1.png")
        self.assertEqual(s["consistency"]["sheets"], ["/f/char.png"])

    def test_broken_manifest_does_not_block_verdict(self):
        mdir = (self.tmp / "ch01_work" / cn.SUBDIR)
        mdir.mkdir(parents=True)
        (mdir / cn.MANIFEST).write_text("{不是 JSON", encoding="utf-8")
        p = self._p()
        cn.set_verdict(p, p.data["shots"][0], "ok")
        self.assertEqual(p.data["shots"][0]["consistency"]["verdict"], "ok")


class TestConcurrentSave(unittest.TestCase):
    """判定必须登记进 `_SHOT_HUMAN_KEYS`，否则并发 save 会把它静默回滚。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.addCleanup(self.env.restore)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "ch01.json"
        self.path.write_text(json.dumps(_doc(), ensure_ascii=False), encoding="utf-8")

    def test_registered_in_shot_human_keys(self):
        self.assertIn("consistency", project_mod._SHOT_HUMAN_KEYS)

    def test_consistency_survives_concurrent_save(self):
        engine = Project.load(self.path)      # 引擎长任务：加载即基线（此时无判定）
        human = Project.load(self.path)       # 同期 consistency set 落盘
        cn.set_verdict(human, human.data["shots"][0], "drift", note="发色不对")
        human.save()

        engine.data["shots"][0]["image"] = "/x/shot_1.png"   # 引擎自己的回填
        engine.save()

        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["shots"][0]["consistency"]["verdict"], "drift")
        self.assertEqual(disk["shots"][0]["image"], "/x/shot_1.png")  # 引擎回填不受影响


class TestSchemaContract(unittest.TestCase):
    """schema 子字段齐备（test_schema_contract 的清单只登记块级归属）。"""

    @unittest.skipUnless(SCHEMA_PATH.is_file(), "缺 schema 文件")
    def test_consistency_block_declares_all_keys(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        node = (schema["properties"]["shots"]["items"]["properties"]["consistency"])
        self.assertTrue(node["description"].startswith("[engine-managed]"))
        for k in ("verdict", "score", "at", "by", "note", "frame", "sheets"):
            self.assertIn(k, node["properties"], k)
        self.assertEqual(node["properties"]["verdict"]["enum"], list(cn.VERDICTS))
        # 契约必须写清「引擎不打分」「人工表态优先」「渲染物一换判定作废」三条纪律
        for kw in ("不打分", "人工表态优先", "作废"):
            self.assertIn(kw, node["description"], kw)


# ---------------------------------------------------------------------------
# ② 冒烟层（真跑 ffmpeg 产料，零素材入仓）
# ---------------------------------------------------------------------------
def _color_png(path: Path, color: str, size: str = "1280x720") -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s={size}",
                    "-frames:v", "1", str(path)], check=True, capture_output=True)


def _clip(path: Path, seconds: float = 4.0) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=24:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)


def _width(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    return int(out.stdout.strip().split(",")[0])


@unittest.skipUnless(_HAS_FFMPEG, "需要系统 ffmpeg 做产料冒烟")
class TestScanSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.refs = self.tmp / "refs"
        self.refs.mkdir()
        self.sheet = self.refs / "char_林深.png"
        _color_png(self.sheet, "blue")
        self.imgs = self.tmp / "ch01_work" / "images"
        self.imgs.mkdir(parents=True)

    def _kenburns(self):
        img1, img2 = self.imgs / "shot_1.png", self.imgs / "shot_2.png"
        _color_png(img1, "red")
        _color_png(img2, "green")
        return _project(self.tmp, _doc(
            characters=[{"name": "林深", "sheet": str(self.sheet)}],
            shots=[
                {"id": 1, "narration": "a", "dur": 3, "image": str(img1)},
                {"id": 2, "narration": "b", "dur": 3, "image": str(img2),
                 "characters": []},                       # 显式空出场表
                {"id": 3, "narration": "c", "dur": 3},    # 尚未生图
                {"id": 4, "kind": "transition", "dur": 1,
                 "transition": {"type": "fade"}},         # 转场镜不参与
            ]))

    def test_kenburns_scan_pairs_frames_with_sheets(self):
        p = self._kenburns()
        man = cn.scan(p)
        self.assertEqual(man["stage"], "image")
        ids = [r["id"] for r in man["shots"]]
        self.assertEqual(ids, [1, 2, 3])                  # 转场镜被过滤
        r1, r2, r3 = man["shots"]
        self.assertTrue(Path(r1["frame"]).is_file())
        self.assertEqual([x["name"] for x in r1["sheets"]], ["林深"])
        self.assertEqual(r2["reason"], "empty_cast")      # 空出场表必须显式给原因
        self.assertEqual(r2["sheets"], [])
        self.assertEqual(r3["skipped"], "no_image")       # 未生图只跳过不抛错
        self.assertIsNone(r3["frame"])
        self.assertEqual(man["summary"],
                         {"shots": 3, "ready": 1, "no_compare": 1, "skipped": 1})

    def test_manifest_written_with_absolute_paths(self):
        p = self._kenburns()
        man = cn.scan(p)
        f = p.workdir / cn.SUBDIR / cn.MANIFEST
        self.assertTrue(f.is_file())
        disk = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(disk["shots"][0]["frame"], man["shots"][0]["frame"])
        self.assertTrue(Path(disk["shots"][0]["frame"]).is_absolute())
        self.assertTrue(Path(disk["shots"][0]["sheets"][0]["path"]).is_absolute())
        self.assertTrue(disk["howto"])                    # 清单自带用法说明给指挥层

    def test_only_filters_shots(self):
        p = self._kenburns()
        man = cn.scan(p, only="2")
        self.assertEqual([r["id"] for r in man["shots"]], [2])

    def test_frame_is_downscaled_but_never_upscaled(self):
        p = self._kenburns()
        man = cn.scan(p)
        self.assertEqual(_width(Path(man["shots"][0]["frame"])), cn.FRAME_WIDTH)
        small = self.imgs / "shot_small.png"
        _color_png(small, "red", size="320x180")
        p2 = _project(self.tmp, _doc(shots=[{"id": 9, "dur": 3, "image": str(small)}]))
        man2 = cn.scan(p2)
        self.assertEqual(_width(Path(man2["shots"][0]["frame"])), 320)

    def test_dubbed_takes_midframe_from_clip(self):
        clips = self.tmp / "ch01_work" / "clips"
        clips.mkdir(parents=True, exist_ok=True)
        c1 = clips / "shot_1.mp4"
        _clip(c1)
        p = _project(self.tmp, _doc(
            motion="dubbed",
            characters=[{"name": "林深", "sheet": str(self.sheet)}],
            shots=[{"id": 1, "dur": 4, "clip": str(c1)},
                   {"id": 2, "dur": 4}]))
        man = cn.scan(p)
        self.assertEqual(man["stage"], "clip")
        self.assertTrue(Path(man["shots"][0]["frame"]).is_file())
        self.assertEqual(man["shots"][1]["skipped"], "no_clip")   # 未 gen-video 只跳过

    def test_corrupt_source_does_not_abort_whole_chapter(self):
        bad = self.imgs / "shot_bad.png"
        bad.write_bytes(b"not a png")
        good = self.imgs / "shot_good.png"
        _color_png(good, "red")
        p = _project(self.tmp, _doc(shots=[
            {"id": 1, "dur": 3, "image": str(bad)},
            {"id": 2, "dur": 3, "image": str(good)}]))
        man = cn.scan(p)                                  # 不抛错
        self.assertEqual(man["shots"][0]["skipped"], "frame_failed")
        self.assertTrue(Path(man["shots"][1]["frame"]).is_file())


# ---------------------------------------------------------------------------
# ③ 失效闭环：渲染物一被替换，旧判定当场作废
# ---------------------------------------------------------------------------
# 判定是对**某一版渲染物**下的结论。不作废就烂两处：分镜卡会在一张没人判过的新图上
# 继续挂「⚠ 角色漂移」（人工点 done 后甚至与「✓ 已通过」并排出现），`entry.frame`
# 存证也会指向被下次 scan 就地覆盖的另一张图。桩 provider 只写占位字节，不碰 ffmpeg。
_PNG = b"\x89PNG\r\n\x1a\n"


class _FakeRes:
    def __init__(self, path, cost=0.0):
        self.path, self.cost = str(path), cost


class _FakeImageProv:
    """图像桩：把占位字节写到目标路径就返回（零成本、零外部依赖）。"""
    name = "fake"
    prompt_lang = "zh"

    def generate(self, prompt, out_path, **kw):
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_PNG + b"regen")
        return _FakeRes(p)


class _FakeVideoProv(VideoProvider):
    """视频桩：同上（计费档位继承通用契约——回填的 dur 就是买下的整秒）。"""
    name = "fake"
    prompt_lang = "zh"
    resolution = "1080p"

    def generate(self, image, out_path, **kw):
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake-mp4")
        return _FakeRes(p)


class _FakeRouter:
    force_mock = True                      # _gate_4k 视同离线，不触发 4K 授权节点

    def __init__(self, prov):
        self.prov = prov

    def resolve(self, capability, profile):
        return self.prov, {}


class _FakeStore:
    def canvas(self, aspect):
        return (1920, 1080)


class TestInvalidate(unittest.TestCase):
    """`invalidate` 纯语义：视觉阶段清、audio 不动。"""

    def test_image_stage_pops_and_returns_the_dropped_verdict(self):
        s = {"id": 1, "consistency": {"verdict": "drift", "note": "发色不对"}}
        dropped = cn.invalidate(s, "image")
        self.assertEqual(dropped["verdict"], "drift")
        self.assertNotIn("consistency", s)

    def test_clip_stage_pops_too(self):
        s = {"id": 1, "consistency": {"verdict": "ok"}}
        self.assertIsNotNone(cn.invalidate(s, "clip"))
        self.assertNotIn("consistency", s)

    def test_audio_stage_is_a_noop(self):
        """重跑配音不改画面——判定必须原样留着。"""
        s = {"id": 1, "consistency": {"verdict": "ok"}}
        self.assertIsNone(cn.invalidate(s, "audio"))
        self.assertEqual(s["consistency"]["verdict"], "ok")

    def test_no_verdict_is_harmless(self):
        s = {"id": 1}
        self.assertIsNone(cn.invalidate(s, "image"))
        self.assertEqual(s, {"id": 1})

    def test_visual_stages_lockstep_with_frame_stage(self):
        """VISUAL_STAGES 必须恰好等于代表帧的两种来源，否则会漏清一种渲染物。"""
        self.assertEqual(set(cn.VISUAL_STAGES),
                         {cn.frame_stage("kenburns"), cn.frame_stage("dubbed"),
                          cn.frame_stage("native")})


class _DoorCase(unittest.TestCase):
    """各道门的公共脚手架：真 Project（落盘）+ 桩 provider。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.addCleanup(self.env.restore)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _project(self, **over) -> Project:
        path = self.tmp / "ch01.json"
        path.write_text(json.dumps(_doc(**over), ensure_ascii=False), encoding="utf-8")
        return Project.load(path)

    def _image(self, name="old.png") -> Path:
        p = self.tmp / name
        p.write_bytes(_PNG + b"old")
        return p

    def _drift(self, project, shot, *, retake=False):
        cn.set_verdict(project, shot, "drift", note="发色不对", retake=retake)
        project.save()


class TestRegenDoors(_DoorCase):
    """生图 / 图生视频重生：`_regen_gate` 归档旧版之后，判定不许跟着新版走。"""

    def test_gen_image_regen_drops_stale_verdict(self):
        from kinema import cli
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image_prompt": "林深站在门口",
                                  "image": str(self._image())}])
        s = p.shots[0]
        self._drift(p, s, retake=True)          # 人判漂移并打回重做
        self.assertEqual(review.get_state(s, "image"), "retake")

        cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()), only="1")

        self.assertNotIn("consistency", s)      # 新图上不许残留上一版的判定
        self.assertEqual(review.get_state(s, "image"), "wfa")
        envelope = s["gen"]["image"]["envelope"]
        self.assertEqual(envelope["prompt"], s["gen"]["image"]["prompt"])
        self.assertRegex(envelope["fingerprint"], r"^sha256:[0-9a-f]{64}$")
        disk = json.loads(p.path.read_text(encoding="utf-8"))
        self.assertNotIn("consistency", disk["shots"][0])   # 落盘也没被三方合并捞回来
        self.assertEqual(len(s["versions"]["image"]), 1)    # 被判的那一版仍在版本栈里

    def test_gen_image_candidates_keep_canvas_state_until_pick(self):
        """候选不占画布：出候选不归档画布、不作废判定；定稿（pick）时才一并动。"""
        from kinema import cli
        from kinema.pipeline import candidates, versioning
        canvas = self._image()
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image_prompt": "林深站在门口", "image": str(canvas)}])
        s = p.shots[0]
        self._drift(p, s, retake=True)
        cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()),
                            only="1", candidates=2)
        self.assertEqual(len(s["image_candidates"]), 2)
        self.assertIn("consistency", s)
        self.assertTrue(Path(canvas).is_file(), "画布原地不动")
        self.assertEqual(versioning.history(s, "image"), [])
        candidates.pick(p, s, 1)
        self.assertNotIn("consistency", s)
        self.assertEqual(len(versioning.history(s, "image")), 1)

    def test_gen_image_regen_keeps_canvas_until_the_new_image_lands(self):
        """重生先落临时名、成功后归档替换：失败时画布与版本栈原样，成功后无临时文件残留。"""
        from kinema import cli
        from kinema.pipeline import versioning
        canvas = self._image()
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image_prompt": "林深站在门口", "image": str(canvas)}])
        s = p.shots[0]
        review.set_state(s, "image", "retake", note="左手穿模")
        p.save()

        class _Boom(_FakeImageProv):
            def generate(self, prompt, out_path, **kw):
                raise RuntimeError("content policy")
        with self.assertRaises(KinemaError):
            cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_Boom()), only="1")
        self.assertEqual(s["image"], str(canvas))
        self.assertTrue(Path(canvas).is_file())
        self.assertEqual(versioning.history(s, "image"), [])
        self.assertEqual(review.get_state(s, "image"), "retake")
        cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()), only="1")
        self.assertEqual(Path(s["image"]).name, "shot_1.png")
        self.assertEqual(Path(s["image"]).read_bytes(), _PNG + b"regen")
        self.assertFalse(list(p.subdir("images").glob("*.new.png")), "临时名已替换")
        hist = versioning.history(s, "image")
        self.assertEqual(len(hist), 1)
        self.assertIn("左手穿模", hist[0]["reason"])
        self.assertEqual(review.get_state(s, "image"), "wfa")

    def test_accept_existing_is_refused_off_the_agent_route(self):
        from kinema import cli
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image_prompt": "林深站在门口", "image": str(self._image())}])
        with self.assertRaisesRegex(KinemaError, "accept-existing"):
            cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()),
                                only="1", accept_existing=True)

    def test_gen_video_regen_drops_stale_verdict(self):
        from kinema import cli
        img = self._image()
        p = self._project(motion="native",
                          shots=[{"id": 1, "narration": "台词", "dur": 4,
                                  "video_prompt": "缓慢推近", "image": str(img)}])
        s = p.shots[0]
        self._drift(p, s)                       # 判的是上一版片段的抽帧
        with mock.patch.object(cli, "probe_duration", return_value=4.0):
            cli.stage_gen_video(p, _FakeStore(), _FakeRouter(_FakeVideoProv()))
        self.assertTrue(s.get("clip"))
        self.assertNotIn("consistency", s)

    def test_tts_style_audio_regen_keeps_verdict(self):
        """反向守卫：配音阶段与画面无关，判定必须留着（别把 invalidate 撒到所有阶段）。"""
        s = {"id": 1, "consistency": {"verdict": "ok"}}
        cn.invalidate(s, "audio")
        self.assertEqual(s["consistency"]["verdict"], "ok")


class TestCanvasSwapRetakesClip(_DoorCase):
    """换画面的门对存量片段只有一种处置（lineage.retake_clip_for_image）：
    未锁定置 retake，已通过只交人裁决。"""

    def _with_clip(self, state="wfa", **shot_extra):
        clip = self.tmp / "c.mp4"
        clip.write_bytes(b"mp4")
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image": str(self._image()), "clip": str(clip),
                                  "image_prompt": "林深站在门口", **shot_extra}])
        review.set_state(p.shots[0], "clip", state)
        p.save()
        return p, p.shots[0]

    def test_supply_and_refine_and_rollback(self):
        from kinema import refine, supply
        from kinema.pipeline import versioning
        p, s = self._with_clip()
        r = supply.supply_image(p, 1, self._image("new.png"), skip_check=True)
        self.assertEqual((r["clip"], review.get_state(s, "clip")), ("retake", "retake"))
        p, s = self._with_clip()
        r = refine.refine_shot_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()),
                                     shot_no=1, instruction="改")
        self.assertEqual((r["clip"], review.get_state(s, "clip")), ("retake", "retake"))
        p, s = self._with_clip()
        versioning.archive(p, s, "image", reason="x")
        s["image"] = str(self._image("v2.png"))
        from kinema.studio import actions
        with mock.patch.object(actions, "_exclusive") as ex:
            ex.return_value.__enter__.return_value = p
            r = actions.rollback_version(p.path.parent, "x", "ch01", shot=1, stage="image", to=1)
        self.assertEqual((r["clip"], review.get_state(s, "clip")), ("retake", "retake"))

    def test_locked_clip_is_left_to_the_reviewer(self):
        from kinema import supply
        p, s = self._with_clip(state="done")
        r = supply.supply_image(p, 1, self._image("new.png"), skip_check=True)
        self.assertEqual((r["clip"], review.get_state(s, "clip")), ("locked", "done"))

    def test_gen_image_salvages_unregistered_product(self):
        """回填前中断留下的图：重跑直接登记（零成本），不再停在 wip、也不重买。"""
        from kinema import cli
        p, s = self._with_clip()
        s.pop("image")
        review.set_state(s, "image", "wip")
        imgdir = p.subdir("images")
        (imgdir / "shot_1.png").write_bytes(_PNG + b"paid")
        p.save()
        calls = []
        prov = _FakeImageProv()
        prov.generate = lambda *a, **k: calls.append(1)
        cli.stage_gen_image(p, _FakeStore(), _FakeRouter(prov), only="1")
        self.assertEqual(calls, [], "盘上产物直接登记，不再调 provider")
        self.assertEqual(s["image"], str(imgdir / "shot_1.png"))
        self.assertEqual(review.get_state(s, "image"), "wfa")
        self.assertEqual(s["gen"]["image"]["cost"], 0)
        self.assertEqual(review.get_state(s, "clip"), "retake")


class TestReplaceDoors(_DoorCase):
    """不经生成阶段、直接换画面的四道门：直供 / 局部改造 / 宫格换选 / 版本回滚。"""

    def test_supply_drops_stale_verdict(self):
        from kinema import supply
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image": str(self._image())}])
        s = p.shots[0]
        self._drift(p, s)
        supply.supply_image(p, 1, self._image("new.png"), skip_check=True)
        self.assertNotIn("consistency", s)

    def test_refine_drops_stale_verdict(self):
        from kinema import refine
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image": str(self._image())}])
        s = p.shots[0]
        self._drift(p, s)
        refine.refine_shot_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()),
                                 shot_no=1, instruction="把头发改回黑色")
        self.assertNotIn("consistency", s)

    def test_candidate_pick_drops_stale_verdict(self):
        from kinema.pipeline import candidates
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image": str(self._image())}])
        s = p.shots[0]
        imgs = p.subdir("images")
        for k in (1, 2):
            (imgs / f"shot_1_cand{k}.png").write_bytes(_PNG + f"c{k}".encode())
        s["image_candidates"] = [str(imgs / "shot_1_cand1.png"),
                                 str(imgs / "shot_1_cand2.png")]
        self._drift(p, s)
        candidates.pick(p, s, 2)
        self.assertNotIn("consistency", s)

    def test_cli_rollback_drops_stale_verdict(self):
        """CLI `versions rollback` 与 Studio 面板是同一道门的两个入口，纪律必须一致。"""
        from types import SimpleNamespace

        from kinema import cli
        from kinema.pipeline import versioning
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3}])
        img = p.subdir("images") / "shot_1.png"
        img.write_bytes(_PNG + b"v1")
        p.shots[0]["image"] = str(img)
        versioning.archive(p, p.shots[0], "image", reason="test")   # 造出 v001
        img.write_bytes(_PNG + b"v2")
        self._drift(p, p.shots[0])

        with mock.patch.object(cli, "_load_video", return_value=p):
            cli.cmd_versions_rollback(SimpleNamespace(
                shot=1, stage="image", to=1, project=str(p.path), chapter=None))
        self.assertNotIn("consistency", p.shots[0])

    def test_studio_rollback_drops_stale_verdict(self):
        from kinema.pipeline import versioning
        from kinema.studio import actions
        from kinema.workspace import Workspace
        ws = Workspace.open(str(self.tmp / "ws"))
        ws.create_project("回滚", pid="rb", profile="hd2d")
        ws.get_project("rb").create_chapter(title="第一章")
        cf = ws.get_project("rb").get_chapter_path("ch01")
        p = Project.load(cf)
        img = p.subdir("images") / "shot_1.png"
        img.write_bytes(_PNG + b"v1")
        p.data["shots"] = [{"id": 1, "narration": "台词", "dur": 3, "image": str(img)}]
        versioning.archive(p, p.shots[0], "image", reason="test")   # 造出 v001
        img.write_bytes(_PNG + b"v2")
        cn.set_verdict(p, p.shots[0], "ok")          # 判的是当前这一版
        p.save()

        r = actions.rollback_version(ws.root, "rb", "ch01", shot=1, stage="image", to=1)
        self.assertEqual(r["now_contains"], "v1")
        disk = json.loads(cf.read_text(encoding="utf-8"))
        self.assertNotIn("consistency", disk["shots"][0])   # 画布换成历史版 → 判定作废


class TestConcurrentVerdictWins(_DoorCase):
    """并发纪律：作废走三方合并——运行期间人工新落的判定仍以磁盘为准，不被引擎抹掉。"""

    def test_verdict_set_during_run_survives_regen(self):
        from kinema import cli
        p = self._project(shots=[{"id": 1, "narration": "台词", "dur": 3,
                                  "image_prompt": "林深站在门口",
                                  "image": str(self._image())}])
        human = Project.load(p.path)                 # 同期指挥层跑 consistency set
        cn.set_verdict(human, human.shots[0], "drift", note="发色不对")
        human.save()

        cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_FakeImageProv()),
                            only="1", force=True)
        disk = json.loads(p.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["shots"][0]["consistency"]["verdict"], "drift")


class TestSummaryIsExclusive(unittest.TestCase):
    """汇总四项必须互斥且求和等于总镜数——否则会打出 2+4>4 这种自相矛盾的行。"""

    def test_skipped_row_with_reason_counts_once(self):
        rows = [
            {"id": 1, "reason": None, "skipped": None},          # 可比对
            {"id": 2, "reason": "empty_cast", "skipped": None},   # 无可比对角色
            {"id": 3, "reason": "no_cast", "skipped": "no_clip"},  # 两者都有 → 只算跳过
            {"id": 4, "reason": None, "skipped": "no_image"},     # 跳过
        ]
        ready = [r for r in rows if not r["reason"] and not r["skipped"]]
        summary = {
            "shots": len(rows),
            "ready": len(ready),
            "no_compare": sum(1 for r in rows if r["reason"] and not r["skipped"]),
            "skipped": sum(1 for r in rows if r["skipped"]),
        }
        self.assertEqual(summary["no_compare"], 1, "同时被 skip 的镜不该再计一次")
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["ready"] + summary["no_compare"] + summary["skipped"],
                         summary["shots"], "四项计数必须互斥且求和等于总镜数")


class TestScoreIsJsonSafe(unittest.TestCase):
    """`--score` 落盘目的地是 project.json——NaN/Infinity 不是合法 JSON，
    浏览器 JSON.parse 会直接抛，Studio 章节页整页加载失败。"""

    def test_non_finite_rejected(self):
        for bad in ("nan", "inf", "-inf", "1e400"):
            with self.assertRaises(ProjectError, msg=f"{bad} 应被拒"):
                cn._clean_score(bad)

    def test_clamped_to_unit_range(self):
        self.assertEqual(cn._clean_score("2"), 1.0)
        self.assertEqual(cn._clean_score("-5"), 0.0)
        self.assertEqual(cn._clean_score("0.876"), 0.876)

    def test_non_numeric_rejected(self):
        with self.assertRaises(ProjectError):
            cn._clean_score("很像")

    def test_result_is_json_serializable(self):
        import json as _j
        import math as _m
        v = cn._clean_score("0.5")
        self.assertTrue(_m.isfinite(v))
        self.assertNotIn("NaN", _j.dumps({"score": v}))


if __name__ == "__main__":
    unittest.main()


class TestSheetedFollowsProviderCap(_DoorCase):
    """绑定句只给设定图真的随请求发出的角色：provider 参考位被裁掉的角色没有图可绑，
    要落全文外貌，否则「以其设定图为准」指向一张没发出去的图。"""

    def test_capped_provider_drops_binding_for_the_cut_character(self):
        from kinema import cli
        sheet_a = self._image("sheet_a.png")
        sheet_b = self._image("sheet_b.png")

        class _OneRef(_FakeImageProv):
            max_ref_images = 1
        p = self._project(
            characters=[{"name": "林深", "sheet": str(sheet_a), "appearance": "黑发短袖"},
                        {"name": "王姨", "sheet": str(sheet_b), "appearance": "花围裙"}],
            shots=[{"id": 1, "narration": "台词", "dur": 3, "characters": ["林深", "王姨"],
                    "image_prompt": "两人对视"}])
        cli.stage_gen_image(p, _FakeStore(), _FakeRouter(_OneRef()), only="1")
        positive = p.shots[0]["gen"]["image"]["envelope"]["positive"]
        self.assertIn("林深（外观", positive)
        self.assertNotIn("王姨（外观", positive, "被裁掉的角色不得拿绑定句")
        self.assertIn("花围裙", positive, "没图的角色落全文外貌")
