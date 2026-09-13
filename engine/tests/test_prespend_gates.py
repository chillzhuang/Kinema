# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
# SPDX-License-Identifier: AGPL-3.0-or-later

"""gen-video 计费前的两道质量闸：分镜图比例 与 旁白语态。

**两层结构**：
  ① 纯判定层（永远跑）——语态超限判据与 lint 同源、闸的取舍与非交互行为；
  ② `@skipUnless(_HAS_FFMPEG)` 冒烟层——用 lavfi 现造真实尺寸的图跑比例闸，
     **零素材入仓**（临时目录内生成，用完即删）。

两道闸都**不硬拦**：比例不符仍能出片、说书式剧情片是合法选择，属于要不要接受的
取舍而非买不得的组合。它们守的是「花钱前必须把后果说清」，以及「非交互环境里
既不替用户中止、也不替用户确认」。
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema.pipeline import variation
from tests.support import LocalBackendEnv

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def _real_png(p: Path, w: int, h: int) -> Path:
    """用 lavfi 现造一张真实尺寸的图（ffprobe 读得出宽高）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"color=c=gray:s={w}x{h}", "-frames:v", "1", str(p)],
                   check=True)
    return p


class TestVoiceoverOverrunIsSingleSource(unittest.TestCase):
    """闸与 lint 共用同一个判据——两处各写一份就会「lint 说没超、闸说超了」。"""

    @staticmethod
    def _vo(n):      # 旁白镜（无具名说话人）
        return [{"id": i, "narration": "他走进了那扇门"} for i in range(1, n + 1)]

    @staticmethod
    def _line(n, start=100):   # 对白镜
        return [{"id": start + i, "lines": [{"speaker": "阿甲", "text": "走"}]}
                for i in range(1, n + 1)]

    def test_lead_never_overruns(self):
        self.assertIsNone(variation.voiceover_overrun(self._vo(9), "lead"),
                          "解说驱动的常态就是镜镜旁白，不是病")

    def test_small_sample_is_not_judged(self):
        """3 镜里 2 镜有旁白说明不了任何事——占比对小样本没有意义。"""
        self.assertIsNone(variation.voiceover_overrun(self._vo(3), "sparse"))
        self.assertEqual(variation.VOICEOVER_MIN_SHOTS, 4)

    def test_sparse_threshold(self):
        shots = self._vo(4) + self._line(6)          # 4/10 = 40%，恰在上限
        self.assertIsNone(variation.voiceover_overrun(shots, "sparse"))
        shots = self._vo(5) + self._line(5)          # 5/10 = 50%，超
        self.assertEqual(variation.voiceover_overrun(shots, "sparse"), (5, 10))

    def test_none_mode_rejects_any_voiceover(self):
        self.assertEqual(variation.voiceover_overrun(self._vo(1) + self._line(9),
                                                     "none"), (1, 10))
        self.assertIsNone(variation.voiceover_overrun(self._line(10), "none"))

    def test_lint_and_gate_agree_on_every_sample(self):
        """同一份文档，lint 报不报与闸问不问必须恒等——这是本组存在的理由。"""
        from kinema.cli import _voiceover_gap
        from kinema.project import Project
        for n_vo, n_line in ((9, 0), (5, 5), (4, 6), (1, 9), (0, 10), (2, 1)):
            data = {"id": "c", "profile": "anime", "voiceover": "sparse",
                    "shots": self._vo(n_vo) + self._line(n_line)}
            lint_hit = any(f.code == "voiceover_heavy" for f in variation.lint(data))
            gate_hit = _voiceover_gap(Project(Path("x.json"), data)) is not None
            self.assertEqual(lint_hit, gate_hit, f"{n_vo} 旁白 / {n_line} 对白")


class _Base(unittest.TestCase):
    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self.addCleanup(self._env.restore)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _project(self, shots, **over):
        from kinema.project import Project
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "profile": "anime", "motion": "native",
               "aspect": "16:9", "shots": shots}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    def _run(self, project, *, dry_run=True, **kw):
        from kinema import cli as cli_mod
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        for latch in (cli_mod._warned_ski, cli_mod._warned_v2v,
                      cli_mod._warned_lf, cli_mod._warned_tail):
            latch.clear()
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=dry_run, **kw)
        return buf.getvalue()


class TestVoiceoverGateWiring(_Base):
    def _shots(self, n_vo, n_line):
        out = []
        for i in range(1, n_vo + 1):
            out.append({"id": i, "dur": 5.0, "video_prompt": "迈步",
                        "image": str(_png(self.tmp / f"v{i}.png")),
                        "narration": "他走进了那扇门"})
        for j in range(1, n_line + 1):
            out.append({"id": 100 + j, "dur": 5.0, "video_prompt": "迈步",
                        "image": str(_png(self.tmp / f"l{j}.png")),
                        "lines": [{"speaker": "阿甲", "text": "走"}]})
        return out

    def test_dry_run_states_the_fact_without_asking(self):
        out = self._run(self._project(self._shots(9, 1), voiceover="sparse"))
        self.assertIn("9/10 镜由旁白讲述", out)
        self.assertIn("voiceover: lead", out, "要给出两条明确出路")

    def test_within_declaration_stays_silent(self):
        out = self._run(self._project(self._shots(3, 7), voiceover="sparse"))
        self.assertNotIn("镜由旁白讲述", out)

    def test_lead_declaration_stays_silent(self):
        out = self._run(self._project(self._shots(9, 1), voiceover="lead"))
        self.assertNotIn("镜由旁白讲述", out)

    def test_non_tty_states_and_continues(self):
        """非交互环境既不替用户中止、也不替用户确认——说明一句就放行。

        直接调闸函数而不跑完整条链：放行意味着后面会真去渲染，而 mock provider
        会对着桩图跑 ffmpeg。本用例要证的就是「不抛」，跑不跑得完渲染是另一回事。
        """
        from kinema.cli import _gate_voiceover
        project = self._project(self._shots(9, 1), voiceover="sparse")
        buf = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), \
             contextlib.redirect_stdout(buf):
            _gate_voiceover(project, dry_run=False)      # 不抛即放行
        self.assertIn("非交互环境：本次照常发出", buf.getvalue())

    def test_interactive_decline_aborts_before_spending(self):
        """拒绝就地中止——闸排在计划循环之前，一次 provider 调用都不会发生。"""
        project = self._project(self._shots(9, 1), voiceover="sparse")
        from kinema.errors import ProjectError
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"):
            with self.assertRaises(ProjectError) as cm:
                self._run(project, dry_run=False)
        self.assertIn("旁白", str(cm.exception))

    def test_interactive_accept_lets_it_through(self):
        from kinema.cli import _gate_voiceover
        project = self._project(self._shots(9, 1), voiceover="sparse")
        buf = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             contextlib.redirect_stdout(buf):
            _gate_voiceover(project, dry_run=False)      # 不抛即放行


@unittest.skipUnless(_HAS_FFMPEG, "需要系统 ffmpeg 现造真实尺寸的图")
class TestFrameAspectGateWiring(_Base):
    def _shots(self, sizes):
        return [{"id": i, "dur": 5.0, "video_prompt": "迈步",
                 "image": str(_real_png(self.tmp / f"s{i}.png", w, h))}
                for i, (w, h) in enumerate(sizes, start=1)]

    def test_matching_aspect_stays_silent(self):
        out = self._run(self._project(self._shots([(1920, 1080), (1280, 720)])))
        self.assertNotIn("与画布不同比", out, "同比（含不同分辨率）不该报")

    def test_mismatched_aspect_is_named_with_the_consequence(self):
        """3:2 的分镜图喂 16:9 请求——必须说清「审过的那一帧不会是成片画面」。"""
        out = self._run(self._project(self._shots([(1536, 1024)])))
        self.assertIn("与画布不同比", out)
        self.assertIn("重新构图", out)
        self.assertIn("1536×1024", out, "要报出实际尺寸，不能只说不符")
        self.assertIn("gen-image --force", out, "要给出修法")

    def test_each_shot_reported_once(self):
        out = self._run(self._project(self._shots([(1536, 1024), (1536, 1024)])))
        self.assertEqual(out.count("与画布 1920×1080 不同比"), 2)

    def test_non_tty_states_and_continues(self):
        """同语态闸：非交互只说明、不中止（放行后的渲染不在本用例范围内）。"""
        from kinema.cli import _gate_frame_aspect
        from kinema.models import ConfigStore
        project = self._project(self._shots([(1536, 1024)]))
        buf = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), \
             contextlib.redirect_stdout(buf):
            _gate_frame_aspect(project, ConfigStore.load(None), project.shots,
                               ["16:9"], dry_run=False)
        self.assertIn("非交互环境：本次照常发出", buf.getvalue())

    def test_interactive_decline_aborts_before_spending(self):
        project = self._project(self._shots([(1536, 1024)]))
        from kinema.errors import ProjectError
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"):
            with self.assertRaises(ProjectError) as cm:
                self._run(project, dry_run=False)
        self.assertIn("比例", str(cm.exception))

    def test_missing_probe_degrades_to_silence(self):
        """体检是护栏，不该反过来把没装全工具的机器锁死。"""
        from kinema.pipeline import mediacheck
        project = self._project(self._shots([(1536, 1024)]))
        with mock.patch.object(mediacheck, "ffprobe_available", return_value=False):
            out = self._run(project)
        self.assertNotIn("与画布不同比", out)


if __name__ == "__main__":
    unittest.main()


class TestPriceByResolution(unittest.TestCase):
    """按档单价：`--resolution` 落在哪一档，报价、事前闸与台账就按哪一档；没配的档回落基准价。"""

    def test_effective_price_follows_resolution_tier(self):
        from kinema.providers.video.seedance import SeedanceProvider as SeedanceVideoProvider
        conn = {"price_per_second": 1.51, "price_per_second_1080p": 3.74,
                "price_per_second_4k": 0, "resolution": "720p"}
        prov = SeedanceVideoProvider(conn, None)
        self.assertEqual(prov.effective_price_per_second, 1.51)
        prov.resolution = "1080p"
        self.assertEqual(prov.effective_price_per_second, 3.74)
        prov.resolution = "480p"
        self.assertEqual(prov.effective_price_per_second, 1.51, "未配的档回落基准价")
        conn4k = {"price_per_second": 1.0, "price_per_second_4k": 2.0, "resolution": "4k"}
        self.assertEqual(SeedanceVideoProvider(conn4k, None).effective_price_per_second, 2.0)


class TestLintGateAtPaidVideoStage(_Base):
    """调度软闸的第二个挂点：`gen-video` 计费前。

    在这之前它只挂 `stage_gen_image`，而 lint 里由 `uses_seedance` 门控的那一批
    维度（`motion_plan`/`beats_span`/`prompt_echo`/`entry_continuity`/
    `montage_chop`/`control_inert` …）只在 dubbed/native 下成立——**渲染档到
    `gen-video` 入口才定死**（未表态章节由 `_settle_motion` 现场写入，`-m b/-m c`
    运行时覆盖同样只在这一刻生效）。同一份文档在生图那一遍按 kenburns 判是
    0 警告，按秒计费这一刻按 native 判就是一串警告，本组钉的就是这个差。

    仍是软闸：只提示、不阻断、不落盘（硬事实由比例闸/语态闸/`_cast_gate`/
    readiness 拦，统计量不拦付费阶段）。"""

    def _flat_shots(self, n=4):
        """没有 sketch.beats 的镜——native 下 `motion_plan` 报、kenburns 下不报。"""
        return [{"id": i, "dur": 8.0, "narration": "他推开那扇门。",
                 "image": str(_png(self.tmp / f"f{i}.png")),
                 "video_prompt": "推开门，跨过门槛，回头看了一眼身后的走廊，"
                                 "手仍搭在门框上，脚下的灰被带起一小片"}
                for i in range(1, n + 1)]

    def test_motion_gated_dimension_is_invisible_until_the_paid_stage(self):
        """同一份文档：kenburns 口径不报 `motion_plan`，native 口径报。"""
        shots = self._flat_shots()
        self.assertFalse(
            [f for f in variation.lint({"motion": "kenburns", "shots": shots})
             if f.code == "motion_plan"])
        self.assertTrue(
            [f for f in variation.lint({"motion": "native", "shots": shots})
             if f.code == "motion_plan"])

    def test_gen_video_surfaces_the_findings_before_spending(self):
        out = self._run(self._project(self._flat_shots()))
        self.assertIn("分镜单 lint", out)
        self.assertIn("逐拍时间轴", out, "视频侧维度必须在按秒计费这一刻可见")
        self.assertIn("提示词审阅", out, "软闸不阻断——报价照常走完")

    def test_preview_stays_silent(self):
        """Studio 的实发提示词预览是只读面板，软闸一个字都不许打进去。"""
        rows: list = []
        out = self._run(self._project(self._flat_shots()), preview_sink=rows)
        self.assertNotIn("分镜单 lint", out)
        self.assertTrue(rows)

    def test_only_degrades_to_one_line_but_still_scans_the_whole_chapter(self):
        """`--only` 时降一行汇总，但统计口径仍是全片（同生图侧的理由）。"""
        out = self._run(self._project(self._flat_shots()), only="1")
        self.assertIn("全片口径", out)
        self.assertNotIn("逐拍时间轴", out)

    def test_gate_reports_exactly_what_lint_reports(self):
        """付费阶段不另立维度子集——`gen-video` 与 `kinema lint` 是同一张单子。"""
        project = self._project(self._flat_shots())
        want = variation.summarize(variation.lint(project.data))
        out = self._run(project)
        self.assertIn(f"{want['warn']} 警告 / {want['info']} 提示", out)

    def test_clean_chapter_prints_nothing(self):
        shots = [{"id": i, "dur": 8.0, "narration": "他推开那扇门。",
                  "image": str(_png(self.tmp / f"c{i}.png")),
                  "camera": ["缓慢推近", "横移", "固定机位", "缓慢拉远"][i - 1],
                  "framing": ["全景", "中景", "近景", "特写"][i - 1],
                  "sketch": {"beats": [{"t": "0-4s", "action": "推门"},
                                       {"t": "4-8s", "action": "跨过门槛回头"}]},
                  "video_prompt": "推开门，跨过门槛，回头看了一眼身后的走廊，"
                                  "手仍搭在门框上，脚下的灰被带起一小片，"
                                  "门轴发出一声长响后归于安静，衣角在门风里翻了一下"}
                 for i in range(1, 5)]
        findings = variation.lint({"motion": "native", "shots": shots})
        self.assertFalse([f for f in findings if f.code == "motion_plan"])
