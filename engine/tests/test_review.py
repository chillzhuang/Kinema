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

"""kinema.review 单元测试：五态+omt 状态机、布尔语义、done 锁定、章节级 animatic。"""
from __future__ import annotations

import unittest
from pathlib import Path

from kinema import review
from tests.support import fake_path


class TestReviewStateMachine(unittest.TestCase):
    def test_default_state_is_todo(self):
        self.assertEqual(review.get_state({}, "image"), "todo")
        self.assertIsNone(review.get_note({}, "image"))

    def test_set_and_get_state(self):
        shot = {}
        review.set_state(shot, "image", "wip")
        self.assertEqual(review.get_state(shot, "image"), "wip")
        self.assertIn("at", shot["review"]["image"])       # 落时间戳

    def test_boolean_semantics(self):
        shot = {}
        review.set_state(shot, "image", "done")
        self.assertTrue(review.is_locked(shot, "image"))       # done → 锁定
        self.assertFalse(review.needs_retake(shot, "image"))
        review.set_state(shot, "clip", "retake")
        self.assertTrue(review.needs_retake(shot, "clip"))     # retake → 强制重生
        self.assertFalse(review.is_locked(shot, "clip"))
        review.set_state(shot, "shot", "omt")
        self.assertTrue(review.is_omitted(shot))               # omt → 整镜弃用
        review.set_state(shot, "shot", "todo")
        self.assertFalse(review.is_omitted(shot))              # 恢复

    def test_done_lock_only_for_done(self):
        shot = {}
        for state in ("todo", "wip", "wfa", "retake", "omt"):
            review.set_state(shot, "audio", state)
            self.assertFalse(review.is_locked(shot, "audio"), state)
        review.set_state(shot, "audio", "done")
        self.assertTrue(review.is_locked(shot, "audio"))

    def test_invalid_state_raises(self):
        with self.assertRaises(ValueError):
            review.set_state({}, "image", "approved")

    def test_invalid_stage_raises(self):
        with self.assertRaises(ValueError):
            review.set_state({}, "thumbnail", "done")

    def test_mark_generated_lands_wfa(self):
        shot = {}
        review.mark_generated(shot, "image")
        self.assertEqual(review.get_state(shot, "image"), "wfa")

    def test_retake_note_preserved_then_cleared(self):
        shot = {}
        review.set_state(shot, "clip", "retake", note="第3秒左手穿模")
        review.set_state(shot, "clip", "retake")               # 未给新意见 → 保留旧的
        self.assertEqual(review.get_note(shot, "clip"), "第3秒左手穿模")
        review.set_state(shot, "clip", "wfa")                  # 非 retake → 不再携带旧意见
        self.assertIsNone(review.get_note(shot, "clip"))

    def test_done_consumes_stage_comments(self):
        # 通过即消费批注：image 通过只清 image 批注，audio 的保留；
        # 非 done 表态（wfa/retake）不动批注——审阅期意见必须在场供查验
        shot = {"comments": [
            {"id": "a", "text": "手改小", "stage": "image", "x": 0.9, "y": 0.8},
            {"id": "b", "text": "锚点意见", "stage": "image", "x": None},
            {"id": "c", "text": "语气重些", "stage": "audio"},
        ]}
        review.set_state(shot, "image", "wfa")
        self.assertEqual(len(shot["comments"]), 3)         # 待审不清
        review.set_state(shot, "image", "retake")
        self.assertEqual(len(shot["comments"]), 3)         # 打回不清
        review.set_state(shot, "image", "done")
        self.assertEqual([c["id"] for c in shot["comments"]], ["c"])   # 只吃 image 阶段
        review.set_state(shot, "audio", "done")
        self.assertEqual(shot["comments"], [])             # audio 通过吃掉剩余
        review.set_state(shot, "shot", "todo")             # 非产物阶段（shot）不炸

    def test_regen_note_numbers_multiple_comments(self):
        # 多条意见逐条带序号（防模型只挑第一条执行）；单条保持原格式
        from kinema.studio.actions import _regen_note
        multi = _regen_note({"comments": [
            {"text": "加上kingkong", "x": 0.9, "y": 0.1},
            {"text": "太Q版了改成熟点", "x": 0.5, "y": 0.5},
        ]})
        self.assertIn("共2处，缺一不可", multi)
        self.assertIn("① 加上kingkong（画面上右）", multi)
        self.assertIn("② 太Q版了改成熟点（画面中中）", multi)
        single = _regen_note({"comments": [{"text": "整体太暗"}]})
        self.assertNotIn("①", single)
        self.assertIn("按未解决批注修正：整体太暗", single)

    def test_chapter_level_animatic(self):
        self.assertEqual(review.CHAPTER_STAGES, ("animatic",))
        chapter = {}
        review.set_state(chapter, "animatic", "wfa")
        self.assertEqual(review.get_state(chapter, "animatic"), "wfa")
        review.set_state(chapter, "animatic", "done")
        self.assertTrue(review.is_locked(chapter, "animatic"))

    def test_summary_counts_and_omitted(self):
        s1, s2, s3 = {}, {}, {}
        review.set_state(s1, "image", "done")
        review.set_state(s2, "shot", "omt")                    # 弃用镜单列、不计入各阶段
        out = review.summary([s1, s2, s3])
        self.assertEqual(out["omitted"], 1)
        self.assertEqual(out["image"], {"done": 1, "todo": 1})
        self.assertEqual(out["audio"], {"todo": 2})

    def test_summary_skips_audio_for_shots_without_an_audio_product(self):
        s1, s2 = {"id": 1}, {"id": 2}
        review.set_state(s1, "audio", "done")
        out = review.summary([s1, s2], audio_of=lambda s: s["id"] == 1)
        self.assertEqual(out["audio"], {"done": 1})
        self.assertEqual(out["image"], {"todo": 2})

    def test_native_burn_board_counts_audio_only_for_narration_shots(self):
        """native 混烧：对白镜由模型发声、没有 wav 可审，看板不得挂成永远关不掉的待办；
        scored 整章无旁白轨，audio 一栏为空。"""
        from kinema import voicecast
        from kinema.project import Project
        data = {"motion": "native", "native_voiceover": True,
                "shots": [{"id": 1, "dur": 7, "narration": "旁白句。"},
                          {"id": 2, "dur": 7, "lines": [{"speaker": "甲", "text": "对白。"}]}]}
        view = Project(Path("."), data)
        out = review.summary(data["shots"],
                             audio_of=lambda s: voicecast.has_audio_stage(s, view))
        self.assertEqual(out["audio"], {"todo": 1})
        data["audio_mode"] = "scored"
        view = Project(Path("."), data)
        out = review.summary(data["shots"],
                             audio_of=lambda s: voicecast.has_audio_stage(s, view))
        self.assertEqual(out["audio"], {})


class TestStageFieldsCoverContract(unittest.TestCase):
    """契约白名单能写的每个镜级字段都在 `STAGE_FIELDS` 登记：白名单扩面而失效表不动，
    Gateway 改了字段既不置 retake 也不受 done 锁。不使产物过期的字段登记为空元组。"""

    def test_every_contract_shot_field_is_registered(self):
        import json
        from pathlib import Path
        contracts = json.loads((Path(__file__).resolve().parents[2] / "agent" / "contracts.json")
                               .read_text(encoding="utf-8"))
        shot_fields = contracts["chapter_plan"]["shot_fields"]
        missing = sorted(set(shot_fields) - set(review.STAGE_FIELDS))
        self.assertEqual(missing, [])
        for f in ("voice_instruction", "emotion_scale", "delivery", "voice"):
            self.assertIn("audio", review.STAGE_FIELDS[f])
        for f in ("anchor_frame", "frame_chain"):
            self.assertEqual(review.STAGE_FIELDS[f], ("clip",))

    # 章级白名单里合法地不使任何产物过期的字段：封面、字幕样式、合成期特效与
    # 主题脚本这类只在合成或另一条产线消费的表态。镜级用空元组表达同一件事，
    # 章级表按阶段索引、没有空槽可填，故在此显式列出。
    CHAPTER_FIELDS_WITHOUT_STAGE = frozenset({
        "art_direction", "control_bgm", "cover_prompt", "effects", "native_bgm", "scored_bgm",
        "script", "subtitle", "subtitle_lang", "theme", "voice_performance",
        "voiceover",
    })

    def test_every_contract_chapter_field_is_registered(self):
        """章级白名单的每个字段要么登记进 `CHAPTER_STAGE_FIELDS`、要么显式列进上面的
        不失效集。缺这条守卫时新加的章级开关既不撞 done 锁也不置 retake——
        `previz_v2v` 有登记、`control_video` 漏登记，两者行为会静默分叉。"""
        import json
        from pathlib import Path
        contracts = json.loads((Path(__file__).resolve().parents[2] / "agent" / "contracts.json")
                               .read_text(encoding="utf-8"))
        chapter_fields = set(contracts["chapter_plan"]["chapter_fields"])
        registered = set().union(*review.CHAPTER_STAGE_FIELDS.values())
        unclassified = sorted(chapter_fields - registered - self.CHAPTER_FIELDS_WITHOUT_STAGE)
        self.assertEqual(unclassified, [])
        self.assertEqual(sorted(self.CHAPTER_FIELDS_WITHOUT_STAGE - chapter_fields), [])
        for f in ("previz_v2v", "control_video"):
            self.assertIn(f, review.CHAPTER_STAGE_FIELDS["clip"])

    def test_chapter_locked_and_retake_produced(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "s.png"
            img.write_bytes(b"png")
            shots = [{"id": 1, "image": str(img), "review": {"image": {"state": "done"}}},
                     {"id": 2, "image": str(img), "review": {"image": {"state": "wfa"}}},
                     {"id": 3}]
            self.assertEqual(review.chapter_locked(shots, {"style_prompt"}), ["image"])
            self.assertEqual(review.chapter_locked(shots, {"speech_rate"}), [])
            self.assertEqual(review.chapter_locked(shots, {"profile"}), ["image"])
            self.assertEqual(review.retake_produced(shots[0], ["image"]), [], "锁定不动")
            self.assertEqual(review.retake_produced(shots[1], ["image", "clip"]), ["image"])
            self.assertEqual(review.get_state(shots[1], "image"), "retake")
            self.assertEqual(review.retake_produced(shots[2], ["image"]), [], "无产物不动")
            self.assertEqual(review.retake_produced(shots[1], ["image"]), [], "已在重做不重置")


class _FakeProject:
    """最小项目替身：只承载 shots 数据并计数 save 调用。"""

    def __init__(self, shots):
        self.data = {"shots": shots}
        self.saved = 0

    def save(self):
        self.saved += 1


class TestAutoApproveReviews(unittest.TestCase):
    """run 一条龙收尾自动过审（cli._auto_approve_reviews）：只动 wfa，不吞人工表态。"""

    def test_wfa_becomes_done_and_saves_once(self):
        from kinema.cli import _auto_approve_reviews
        shot = {"id": 1}
        review.set_state(shot, "image", "wfa")
        review.set_state(shot, "audio", "wfa")
        review.set_state(shot, "clip", "wfa")
        proj = _FakeProject([shot])
        _auto_approve_reviews(proj)
        for stage in review.STAGES:
            self.assertEqual(review.get_state(shot, stage), "done")
        self.assertEqual(proj.saved, 1)                        # 有变更只落盘一次

    def test_non_wfa_states_untouched(self):
        from kinema.cli import _auto_approve_reviews
        shot = {"id": 1}
        review.set_state(shot, "image", "todo")
        review.set_state(shot, "audio", "wip")
        review.set_state(shot, "clip", "retake", note="第3秒穿模")
        _auto_approve_reviews(_FakeProject([shot]))
        self.assertEqual(review.get_state(shot, "image"), "todo")
        self.assertEqual(review.get_state(shot, "audio"), "wip")
        self.assertEqual(review.get_state(shot, "clip"), "retake")
        shot2 = {"id": 2}
        review.set_state(shot2, "image", "done")               # 已通过=人工锁定，不重写时间戳
        before = shot2["review"]["image"]
        _auto_approve_reviews(_FakeProject([shot2]))
        self.assertIs(shot2["review"]["image"], before)

    def test_omitted_shot_skipped(self):
        from kinema.cli import _auto_approve_reviews
        shot = {"id": 1}
        review.set_state(shot, "shot", "omt")                  # 弃用镜整镜跳过
        review.set_state(shot, "image", "wfa")
        _auto_approve_reviews(_FakeProject([shot]))
        self.assertEqual(review.get_state(shot, "image"), "wfa")


class TestAssembleReviewGate(unittest.TestCase):
    """合成前审阅闸（cli._assemble_review_gate）：正式成片须全部镜过审。
    视觉阶段随模式（kenburns→image / dubbed·native→clip），旁白镜另查 audio；
    转场镜与弃用镜跳过。run/--auto 不经此闸（收尾自动过审）。"""

    def _proj(self, shots, motion="kenburns"):
        from kinema.project import Project
        return Project("x.json", {"motion": motion, "shots": shots})

    def _mk(self, sid, *, image=None, audio=None, clip=None,
            narration="旁白", omt=False, transition=False):
        s = {"id": sid, "narration": narration}
        if transition:
            s["kind"] = "transition"
        if omt:
            review.set_state(s, "shot", "omt")
        for stage, st in (("image", image), ("audio", audio), ("clip", clip)):
            if st:
                review.set_state(s, stage, st)
        return s

    def test_all_done_passes(self):
        from kinema.cli import _assemble_review_gate
        shots = [self._mk(1, image="done", audio="done"),
                 self._mk(2, image="done", audio="done")]
        self.assertEqual(_assemble_review_gate(self._proj(shots)), [])

    def test_unreviewed_image_blocks(self):
        from kinema.cli import _assemble_review_gate
        shots = [self._mk(1, image="done", audio="done"),
                 self._mk(2, image="wfa", audio="done")]
        self.assertIn((2, "image"), _assemble_review_gate(self._proj(shots)))

    def test_audio_gated_only_when_narrated(self):
        from kinema.cli import _assemble_review_gate
        narrated = self._mk(1, image="done", audio="wfa", narration="有词")
        silent = self._mk(2, image="done", audio=None, narration="")   # 纯画面镜无旁白
        missing = _assemble_review_gate(self._proj([narrated, silent]))
        self.assertIn((1, "audio"), missing)
        self.assertNotIn((2, "audio"), missing)

    def test_omitted_and_transition_skipped(self):
        from kinema.cli import _assemble_review_gate
        omt = self._mk(1, image="wfa", omt=True)
        trans = self._mk(2, transition=True, narration="")
        self.assertEqual(_assemble_review_gate(self._proj([omt, trans])), [])

    def test_mixed_native_gates_narration_shots_only(self):
        """native 混烧：旁白镜的 wav 要过审，对白镜由模型发声、按设计没有 audio 产物。"""
        from kinema.cli import _assemble_review_gate
        from kinema.project import Project
        narr = self._mk(1, clip="done", audio="wfa", narration="旁白一句")
        dlg = self._mk(2, clip="done", audio=None, narration="")
        dlg["lines"] = [{"speaker": "甲", "text": "台词"}]
        proj = Project("x.json", {"motion": "native", "native_voiceover": True,
                                  "shots": [narr, dlg]})
        missing = _assemble_review_gate(proj)
        self.assertIn((1, "audio"), missing)
        self.assertNotIn((2, "audio"), missing)
        proj.data["native_voiceover"] = False       # 不烧：native 章一律不查 audio
        self.assertEqual(_assemble_review_gate(proj), [])

    def test_dubbed_checks_clip_not_image(self):
        from kinema.cli import _assemble_review_gate
        s = self._mk(1, image="done", audio="done", clip="wfa")        # image 过审但 clip 未审
        missing = _assemble_review_gate(self._proj([s], motion="dubbed"))
        self.assertIn((1, "clip"), missing)
        self.assertNotIn((1, "image"), missing)

    def test_transition_shot_skipped(self):
        from kinema.cli import _auto_approve_reviews
        shot = {"id": 2, "kind": "transition"}
        review.set_state(shot, "image", "wfa")
        _auto_approve_reviews(_FakeProject([shot]))
        self.assertEqual(review.get_state(shot, "image"), "wfa")

    def test_no_wfa_no_save(self):
        from kinema.cli import _auto_approve_reviews
        shot = {"id": 1}
        review.set_state(shot, "image", "done")
        proj = _FakeProject([shot, {"id": 2}])                 # 无待审项 → 不触发落盘
        _auto_approve_reviews(proj)
        self.assertEqual(proj.saved, 0)

    def test_run_no_approve_flag_wiring(self):
        from kinema.cli import build_parser
        ns = build_parser().parse_args(["run"])
        self.assertFalse(ns.no_approve)                        # 缺省自动过审
        ns = build_parser().parse_args(["run", "--no-approve"])
        self.assertTrue(ns.no_approve)                         # 显式保留待审


class TestFrameChainTransitionBoundary(unittest.TestCase):
    """首尾帧衔接跨转场断链（framechain.scan）：转场=场景切换标记，不跨转场硬 morph
    （否则上一张图在家、末帧突兀跳到外面）。"""

    def _shots(self):   # [正镜1, 正镜2, 转场, 正镜3]
        return [{"id": 1, "kind": "shot"}, {"id": 2, "kind": "shot"},
                {"id": 9, "kind": "transition", "transition": {"type": "fade_black"}},
                {"id": 3, "kind": "shot"}]

    def test_chains_to_adjacent_real_shot(self):
        from kinema.pipeline.framechain import scan
        self.assertEqual(scan(self._shots(), 0, True)[0]["id"], 2)   # 镜1→镜2 同场景衔接

    def test_no_chain_across_transition(self):
        from kinema.pipeline.framechain import scan
        self.assertIsNone(scan(self._shots(), 1, True)[0])   # 镜2 下一个是转场→断链

    def test_no_chain_when_disabled(self):
        from kinema.pipeline.framechain import scan
        self.assertIsNone(scan(self._shots(), 0, False)[0])

    def test_last_shot_has_no_next(self):
        from kinema.pipeline.framechain import scan
        self.assertIsNone(scan(self._shots(), 3, True)[0])   # 末镜无下一个

    def test_omitted_shot_is_skipped_not_pinned(self):
        """弃用镜不进成片——末帧 pin 到它上面等于朝一个观众看不到的画面收束。
        正解是**跳过它继续往后找**（成片里紧接着出现的是更后面那镜）。"""
        from kinema.pipeline.framechain import scan
        shots = [{"id": 1}, {"id": 2, "review": {"shot": {"state": "omt"}}}, {"id": 3}]
        self.assertEqual(scan(shots, 0, True)[0]["id"], 3)

    def test_chain_scan_reports_break_reason(self):
        from kinema.pipeline.framechain import scan
        self.assertEqual(scan(self._shots(), 1, True), (None, "transition"))
        self.assertEqual(scan(self._shots(), 3, True), (None, "end"))
        self.assertEqual(scan(self._shots(), 0, False), (None, "off"))


class TestLastFrameCapabilityGate(unittest.TestCase):
    """**provider 不支持末帧时，链必须当场断掉**（`supports_last_frame`）。

    `doubao-seedance-2-0-fast` 实测接受 first_frame、静默丢弃 last_frame——
    服务端对不认识的 role 只丢不报。照发的后果：成片首帧与分镜图一致，
    末帧却与下一镜分镜图毫不相干（SSIM 与随机两张同级），而日志、dry-run 清单、
    页面三处都标着「末帧→镜N」，提示词还写着
    「运动须自然收束在末帧上」——模型被要求收束到一张它从没收到的图。

    这一条守的是「能力面进判据」：不支持就别发、别写、别标，并且喊出来。
    """

    def setUp(self):
        from tests.support import LocalBackendEnv
        self._env = LocalBackendEnv()
        self._env.enable()

    def tearDown(self):
        self._env.restore()

    def _run(self, *, supports: bool):
        import contextlib
        import io
        import json
        import tempfile
        from unittest import mock as _mock
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        from kinema.providers.video.mock import MockVideoProvider
        with tempfile.TemporaryDirectory() as tmp:
            imgs = []
            for i in (1, 2):
                q = Path(tmp) / f"shot_{i}.png"
                q.write_bytes(b"\x89PNG")
                imgs.append(str(q))
            doc = {"id": "ch01", "motion": "native", "aspect": "16:9",
                   "frame_chain": True, "skip_design": True, "duration": 4,
                   "shots": [{"id": i, "dur": 4.0, "image": imgs[i - 1],
                              "video_prompt": f"镜{i}的运动设计" * 12,
                              "image_prompt": "画" * 140} for i in (1, 2)]}
            cf = Path(tmp) / "ch01.json"
            cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            project = Project.load(cf)
            store = ConfigStore.load(None)
            buf = io.StringIO()
            with _mock.patch.object(MockVideoProvider, "supports_last_frame", supports), \
                    contextlib.redirect_stdout(buf):
                stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                                dry_run=True)
            return buf.getvalue()

    def test_supported_provider_welds_and_says_so(self):
        """对照组：能力位为真时一切照旧——链标出去向、提示词带末帧铁律句。"""
        out = self._run(supports=True)
        self.assertIn("末帧=镜2", out)
        self.assertIn("收束在末帧上", out)

    def test_unsupported_provider_breaks_the_chain_everywhere_at_once(self):
        """三处必须同时改口：不标末帧 · 提示词不写末帧铁律句 · 出声说明原因。

        三处必须同步，单独改任何一处都会造出「日志说没衔接、提示词却在讲末帧」
        这种自相矛盾的请求。
        """
        out = self._run(supports=False)
        self.assertNotIn("末帧=镜2", out, "不支持末帧就不许标「末帧→镜N」")
        self.assertNotIn("收束在末帧上", out, "末帧没发，提示词就不许写末帧铁律句")
        self.assertIn("不支持末帧", out, "静默降级=用户要等成片出来才发现")
        self.assertIn("seedance-mini", out, "喊话必须给出可执行的出路")
        from kinema.pipeline import framechain
        self.assertIn(framechain.BREAK_ZH["no_last_frame"], out,
                      "断因要说「本模型没有末帧槽」——落到「下一镜缺图」的措辞是误导，"
                      "会让人去补一张本来就在盘上的图")


class TestFrameChainWeldEnds(unittest.TestCase):
    """**焊缝两端**：一条首尾帧焊缝只在「上游真发末帧到下游分镜图」且「下游真把那张图
    当第 0 帧硬锁」时才成立（`framechain.sends` / `receives`）。

    历史上只判上游那一端（旧注释按「切点仍近似连续」处理下游端）。走全能参考 / V2V
    的镜在 seedance 请求体里没有 `first_frame` 项，分镜图降级成 `role=reference_image`
    ——上游朝那张图收束、它却不从那张图起步：页面标着「→ 镜N」、日志印着「末帧=镜N」，
    成片里是一次形变。
    """

    def setUp(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.board = Path(self._d.name) / "board.png"
        self.board.write_bytes(b"\x89PNG")
        self.pz = Path(self._d.name) / "previz.mp4"
        self.pz.write_bytes(b"\x00")

    def _ref(self, i):        # 全能参考孤岛镜：板在盘 × 逐镜 opt-in
        return {"id": i, "sketch": {"sheet": str(self.board), "reference": True}}

    def _v2v(self, i):        # V2V 孤岛镜：previz 参考片在盘
        return {"id": i, "previz": str(self.pz)}

    def test_reference_shot_is_an_island_on_both_sides(self):
        """孤岛镜既发不出末帧、也接不住末帧——两侧焊缝一律断。"""
        from kinema.pipeline import framechain as fc
        isl = self._ref(2)
        self.assertTrue(fc.island(isl))
        self.assertFalse(fc.sends(isl))
        self.assertFalse(fc.receives(isl))

    def test_upstream_never_welds_into_a_reference_shot(self):
        """上游镜不得把末帧 pin 到走全能参考的下游镜身上。"""
        from kinema.pipeline.framechain import scan
        shots = [{"id": 1}, self._ref(2), {"id": 3}]
        self.assertEqual(scan(shots, 0, True), (None, "ref_next"))

    def test_v2v_is_an_island_only_when_the_switch_is_on(self):
        """V2V 是运行时可覆盖的总闸——关着的时候 previz 镜照常走首帧，链不该断。"""
        from kinema.pipeline.framechain import scan
        shots = [{"id": 1}, self._v2v(2), {"id": 3}]
        self.assertEqual(scan(shots, 0, True)[0]["id"], 2)          # 闸关：照常衔接
        self.assertEqual(scan(shots, 0, True, v2v=True), (None, "ref_next"))
        self.assertEqual(scan(shots, 1, True, v2v=True), (None, "v2v"))

    def test_previz_end_frame_shot_sends_nothing_downstream(self):
        """previz 末帧压过衔接链（既定规则）——那一镜收束到自己的终态，
        下游镜从自己的分镜图起步，这条缝同样不是焊的，措辞要说实话。"""
        from kinema.pipeline.framechain import scan, sends
        s = {"id": 2, "last_frame_ref": str(self.board)}
        self.assertFalse(sends(s))
        self.assertEqual(scan([{"id": 1}, s, {"id": 3}], 1, True), (None, "previz_last"))

    def test_break_reasons_all_have_wording(self):
        """断链原因与面向人的措辞锁步——日志/dry-run/网页三处共用同一份，
        漏一个键就会出现「原因有、说法没有」的空串。"""
        from kinema.pipeline import framechain as fc
        for why in ("transition", "end", "no_image", *fc.MODE_BREAKS):
            self.assertTrue(fc.BREAK_ZH.get(why), why)


class TestIslandSeams(unittest.TestCase):
    """孤岛接缝自动落无缝转场（`framechain.sync_seams`）：参考模式镜两侧焊不上，
    引擎补一个 0.1s 软切，让它成为「自己独立一镜、再切到下一个」而不是两次裸硬切。

    四条边界：幂等（在位且仍正确的不动镜号）· 用户手写的转场不碰也不叠 ·
    章首/章尾不插（xfade 单侧邻居会退化成黑闪字卡）· 配置一撤自己撤走。
    """

    def setUp(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.board = Path(self._d.name) / "board.png"
        self.board.write_bytes(b"\x89PNG")

    def _ref(self, i):
        return {"id": i, "sketch": {"sheet": str(self.board), "reference": True}}

    def _trs(self, shots):    # 成片顺序上的转场镜号
        from kinema.pipeline.transitions import is_transition
        return [s["id"] for s in shots if is_transition(s)]

    def test_no_islands_means_no_transitions_at_all(self):
        """缺省一个转场都没有——这条老不变量必须原样成立。"""
        from kinema.pipeline.framechain import sync_seams
        shots = [{"id": 1}, {"id": 2}, {"id": 3}]
        r = sync_seams(shots, True)
        self.assertEqual((r["added"], r["removed"]), ([], []))
        self.assertEqual(len(shots), 3)

    def test_island_gets_a_soft_cut_on_both_sides(self):
        from kinema.pipeline.framechain import sync_seams
        from kinema.pipeline.transitions import spec_of
        shots = [{"id": 1}, self._ref(2), {"id": 3}]
        sync_seams(shots, True)
        self.assertEqual([s["id"] for s in shots], [1, 4, 2, 5, 3])
        for s in (shots[1], shots[3]):
            self.assertEqual(spec_of(s)["type"], "seamless")
            self.assertEqual(spec_of(s)["sound"], "off")   # 三帧柔切上挂音效是事故

    def test_chapter_head_and_tail_are_never_padded(self):
        """孤岛在章首/章尾时只补朝内的那一侧——xfade 族单侧邻居会退化成字卡，
        在这里就是黑闪一下，正是这条边界要防的事故。"""
        from kinema.pipeline.framechain import sync_seams
        head = [self._ref(1), {"id": 2}, {"id": 3}]
        sync_seams(head, True)
        self.assertEqual([s["id"] for s in head], [1, 4, 2, 3])
        tail = [{"id": 1}, {"id": 2}, self._ref(3)]
        sync_seams(tail, True)
        self.assertEqual([s["id"] for s in tail], [1, 2, 4, 3])

    def test_is_idempotent_and_does_not_renumber(self):
        """重复同步不增不减、镜号一个都不动——否则 compose 的转场段缓存名
        `shot_<id>_tr.mp4` 每跑一次就换一次，用户盯着的镜号也无故跳动。"""
        from kinema.pipeline.framechain import sync_seams
        shots = [{"id": 1}, self._ref(2), {"id": 3}]
        sync_seams(shots, True)
        before = [s["id"] for s in shots]
        r = sync_seams(shots, True)
        self.assertEqual((r["added"], r["removed"]), ([], []))
        self.assertEqual([s["id"] for s in shots], before)

    def test_hand_written_transition_is_neither_touched_nor_doubled(self):
        """用户在孤岛旁边手写过转场 → 那一缝已有过渡，不再叠一层软切。"""
        from kinema.pipeline.framechain import sync_seams
        hand = {"id": 9, "kind": "transition", "narration": "",
                "transition": {"type": "fade_black", "text": "一天后"}}
        shots = [{"id": 1}, hand, self._ref(2), {"id": 3}]
        sync_seams(shots, True)
        self.assertEqual(self._trs(shots), [9, 10])      # 手写的留着，只补右侧
        self.assertIs(shots[1], hand)

    def test_turning_the_mode_off_takes_the_soft_cut_away(self):
        """配置一撤，引擎自己插的也撤走——留在那儿就是一段没人认领的 0.1s。"""
        from kinema.pipeline.framechain import sync_seams
        isl = self._ref(2)
        shots = [{"id": 1}, isl, {"id": 3}]
        sync_seams(shots, True)
        isl["sketch"].pop("reference")
        r = sync_seams(shots, True)
        self.assertEqual(self._trs(shots), [])
        self.assertEqual(sorted(r["removed"]), [4, 5])

    def test_previz_end_frame_seam_is_reported_but_not_padded(self):
        """previz 末帧镜断的只是**出链**那一侧（它自己仍以分镜图第 0 帧硬锁，上游焊得
        进来），且 previz 通常整段连着登记——把它也自动补软切等于给那一章塞一串
        转场，把既有形态大改了样。那一处照实报断因、不动结构。"""
        from kinema.pipeline import framechain as fc
        pz = Path(self._d.name) / "end.png"
        pz.write_bytes(b"\x89PNG")
        shots = [{"id": 1, "last_frame_ref": str(pz)}, {"id": 2}]
        self.assertEqual(fc.scan(shots, 0, True)[1], "previz_last")   # 断因照实说
        self.assertIn("previz_last", fc.MODE_BREAKS)
        self.assertNotIn("previz_last", fc.ISLAND_BREAKS)
        self.assertEqual(fc.sync_seams(shots, True)["added"], [])
        self.assertEqual(len(shots), 2)

    def test_chain_off_chapter_is_left_alone(self):
        """整章不衔接（dubbed/kenburns 或 `--no-chain`）时全片本就是硬切——
        引擎不该借这条规则往里塞一堆软切。"""
        from kinema.pipeline.framechain import sync_seams
        shots = [{"id": 1}, self._ref(2), {"id": 3}]
        self.assertEqual(sync_seams(shots, False)["added"], [])
        self.assertEqual(len(shots), 3)

    def test_soft_cut_sits_next_to_the_island_across_omitted_shots(self):
        """弃用镜夹在中间不影响：成片序列（active_shots）里软切仍紧贴孤岛两侧。"""
        from kinema.pipeline.framechain import sync_seams
        from kinema.pipeline.transitions import is_transition
        from kinema.review import is_omitted
        omt = {"id": 9, "review": {"shot": {"state": "omt"}}}
        shots = [{"id": 1}, omt, self._ref(2), {"id": 3}]
        sync_seams(shots, True)
        active = [s for s in shots if not is_omitted(s)]
        kinds = ["T" if is_transition(s) else str(s["id"]) for s in active]
        self.assertEqual(kinds, ["1", "T", "2", "T", "3"])


class TestFrameChainDefaultState(unittest.TestCase):
    """衔接态判据：**缺省关闭（缺省档=逐镜全能参考）× 显式 opt-in × 仅 native**，
    且 `Project` 与 Studio 读同一个函数。

    判据分家过一次代价很大：网页写着「相邻镜自动首尾帧衔接」、命令里却没开衔接，
    整章按硬切生成完才发现。故这里既守规则本身，也守「只有一个规则来源」。
    """

    def test_native_does_not_chain_by_default(self):
        from kinema.pipeline.framechain import active
        self.assertFalse(active({}, "native"))          # 字段缺席=缺省档（全能参考）
        self.assertTrue(active({"frame_chain": True}, "native"))    # 显式才衔接

    def test_explicitly_disabled_stays_off(self):
        from kinema.pipeline.framechain import active
        self.assertFalse(active({"frame_chain": False}, "native"))

    def test_only_native_can_chain(self):
        from kinema.pipeline.framechain import active
        # dubbed 走参考媒体通道、与首/末帧互斥；kenburns 根本不调用视频模型
        for m in ("dubbed", "kenburns"):
            self.assertFalse(active({"frame_chain": True}, m), m)

    def test_pair_opt_in_welds_named_seam_only(self):
        """镜级 `shots[].frame_chain: true`：缺省章里只焊点名那一处，且仅 native。"""
        from kinema.pipeline.framechain import pair_opt_in, plan, scan, welded_in_ids
        shots = [{"id": 1, "frame_chain": True}, {"id": 2}, {"id": 3}]
        self.assertTrue(pair_opt_in(shots[0]))
        self.assertEqual(scan(shots, 0, False, native=True)[0]["id"], 2)
        self.assertEqual(scan(shots, 1, False, native=True), (None, "off"))
        # 非 native（dubbed 等）镜级表态不生效——参考媒体模式与首/末帧官方互斥
        self.assertEqual(scan(shots, 0, False, native=False), (None, "off"))
        # 被焊入集合：下游端要以分镜图第 0 帧硬锁，不能走缺省全能参考
        m = plan(shots, False, native=True)
        self.assertEqual(welded_in_ids(m), {id(shots[1])})

    def test_pair_opt_in_respects_structural_breaks(self):
        """结对衔接与章级同一套结构规则：转场断链、末镜无缝。"""
        from kinema.pipeline.framechain import scan
        shots = [{"id": 1, "frame_chain": True},
                 {"id": 9, "kind": "transition", "transition": {"type": "fade_black"}},
                 {"id": 2, "frame_chain": True}]
        self.assertEqual(scan(shots, 0, False, native=True), (None, "transition"))
        self.assertEqual(scan(shots, 2, False, native=True), (None, "end"))

    def test_project_property_delegates_to_single_source(self):
        """`Project.frame_chain` 已含模式判据——调用方不必也不该再 `and native`。"""
        import json
        import tempfile
        from kinema.project import Project
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "ch01.json"
            for motion, want in (("native", True), ("dubbed", False), ("kenburns", False)):
                cf.write_text(json.dumps({"id": "c", "motion": motion,
                                          "frame_chain": True, "shots": []}),
                              encoding="utf-8")
                self.assertIs(Project.load(cf).frame_chain, want, motion)
            # 字段缺席时 native 也不衔接——缺省档是全能参考
            cf.write_text(json.dumps({"id": "c", "motion": "native", "shots": []}),
                          encoding="utf-8")
            self.assertIs(Project.load(cf).frame_chain, False)

    def test_chain_flag_turns_it_on(self):
        """`--chain` 是缺省关闭后真正改变行为的开关；`--no-chain` 压过同时给出的
        `--chain`（显式要求"这次别衔接"的一方更可能是当下的意图）。"""
        import json
        import tempfile
        from kinema.cli import _apply_aspect_args, build_parser
        from kinema.project import Project
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "ch01.json"
            cf.write_text(json.dumps({"id": "c", "motion": "native", "shots": []}),
                          encoding="utf-8")
            for argv, want in ((["--no-chain"], False),
                               (["--chain", "--no-chain"], False),
                               (["--chain"], True),
                               ([], False)):
                project = Project.load(cf)
                ns = build_parser().parse_args(
                    ["gen-video", "--chapter", "x/ch01", *argv])
                _apply_aspect_args(project, ns)
                self.assertIs(project.frame_chain, want, argv)

    def test_runtime_override_never_reaches_disk(self):
        """`--no-chain` 表达的是"这一次别衔接"，不是"把章节改成永不衔接"。"""
        import json
        import tempfile
        from kinema.cli import _apply_aspect_args, build_parser
        from kinema.project import Project
        with tempfile.TemporaryDirectory() as d:
            cf = Path(d) / "ch01.json"
            cf.write_text(json.dumps({"id": "c", "motion": "native", "shots": []}),
                          encoding="utf-8")
            project = Project.load(cf)
            _apply_aspect_args(project, build_parser().parse_args(
                ["gen-video", "--chapter", "x/ch01", "--no-chain"]))
            project.save()
            self.assertNotIn("frame_chain",
                             json.loads(cf.read_text(encoding="utf-8")))


class TestFrameChainStudioDownlink(unittest.TestCase):
    """Studio 下发的链态必须是**有效**链态，且逐镜链态由引擎给、网页不自算。

    网页若自算链态，页面文案与实发请求就会对不上：页面写着「相邻镜自动首尾帧衔接」，
    命令却根本没开衔接。故下发的既是渲染侧同一个判据，也带上每一镜的去向/断因。
    """

    def setUp(self):
        import tempfile
        from tests.support import LocalBackendEnv
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("Chain", pid="chain")
        self.cf = self.s.create_chapter("第一章", cid="ch01")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _detail(self, motion, shots, **over):
        import json
        data = json.loads(self.cf.read_text(encoding="utf-8"))
        data["motion"] = motion
        data["shots"] = shots
        data.update(over)
        self.cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        from kinema.studio import scanner
        return scanner.chapter_detail(self.ws.root, self.ws.store, "chain", "ch01")

    def _shots(self):   # 镜1 → 镜2 →(转场)→ 镜3(无图)
        img = Path(self.tmp.name) / "s.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        return [{"id": 1, "dur": 3.0, "narration": "", "image": str(img)},
                {"id": 2, "dur": 3.0, "narration": "", "image": str(img)},
                {"id": 9, "kind": "transition", "dur": 1.0,
                 "transition": {"type": "fade_black"}},
                {"id": 3, "dur": 3.0, "narration": ""}]

    def test_chapter_flag_is_the_effective_state(self):
        # 缺省关闭（缺省档=全能参考）：native 章没写开关就不衔接
        self.assertIs(self._detail("native", self._shots())["frame_chain"], False)
        self.assertIs(self._detail("native", self._shots(),
                                   frame_chain=True)["frame_chain"], True)
        # kenburns 章节即便写了开关也不衔接——页面据此不显示衔接标记
        self.assertIs(self._detail("kenburns", self._shots(),
                                   frame_chain=True)["frame_chain"], False)

    def test_default_chapter_reports_nothing_per_shot(self):
        """缺省档（全能参考）逐镜不挂链态标记——每镜都标一句「不衔接」是噪音。"""
        for s in self._detail("native", self._shots())["shots"]:
            self.assertIsNone(s["chain_next"])
            self.assertIsNone(s["chain_break"])

    def test_pair_opt_in_shows_on_the_page(self):
        """镜级结对衔接页面要看得见——引擎给、网页不自算（同一条纪律）。"""
        shots = self._shots()
        shots[0]["frame_chain"] = True
        by_id = {s["id"]: s for s in self._detail("native", shots)["shots"]}
        self.assertEqual(by_id[1]["chain_next"], 2)
        self.assertIsNone(by_id[2]["chain_next"])

    def test_per_shot_chain_target_and_break_reason(self):
        from kinema.pipeline import framechain
        d = self._detail("native", self._shots(), frame_chain=True)
        by_id = {s["id"]: s for s in d["shots"]}
        self.assertEqual(by_id[1]["chain_next"], 2)          # 镜1 末帧→镜2
        self.assertIsNone(by_id[1]["chain_break"])
        self.assertIsNone(by_id[2]["chain_next"])            # 镜2 下一个是转场
        self.assertEqual(by_id[2]["chain_break"], framechain.BREAK_ZH["transition"])
        self.assertEqual(by_id[3]["chain_break"], framechain.BREAK_ZH["end"])
        self.assertIsNone(by_id[9]["chain_break"])           # 转场镜零画面，不参与

    def test_next_shot_without_image_reads_as_broken(self):
        """下一镜没图时引擎会退回纯首帧生成——页面不能还标着衔接。"""
        from kinema.pipeline import framechain
        shots = self._shots()
        shots.pop(2)                                          # 去掉转场：镜2 → 镜3(无图)
        by_id = {s["id"]: s
                 for s in self._detail("native", shots, frame_chain=True)["shots"]}
        self.assertIsNone(by_id[2]["chain_next"])
        self.assertEqual(by_id[2]["chain_break"], framechain.BREAK_ZH["no_image"])

    def test_non_chaining_chapter_reports_nothing_per_shot(self):
        for s in self._detail("kenburns", self._shots())["shots"]:
            self.assertIsNone(s["chain_next"])
            self.assertIsNone(s["chain_break"])


class TestFrameChainLastFrameGate(unittest.TestCase):
    """首尾帧实发口径：链图预计算 + 末帧 has_file 护栏 +
    提示词/请求/日志三方同源。走 `stage_gen_video --dry-run`（零 API、零计费）验。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self._env = LocalBackendEnv()
        self._env.enable()

    def tearDown(self):
        self._env.restore()

    def _shot(self, no, *, image=None, images=None, state="done"):
        s = {"id": no, "dur": 4.0, "narration": f"第{no}句台词",
             "video_prompt": f"镜{no}的运动", "review": {"image": {"state": state}}}
        if image:
            s["image"] = str(image)
        if images:
            s["images"] = {k: str(v) for k, v in images.items()}
        return s

    def _run(self, tmp, doc, **kw):
        """建章节 → dry-run → 返回 (打印文本, project)。"""
        import contextlib
        import io
        import json
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        cf = Path(tmp) / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        project = Project.load(cf)
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=True, **kw)
        return buf.getvalue(), project

    def _img(self, tmp, name):
        p = Path(tmp) / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        return p

    def _doc(self, tmp, **over):
        # 成片顺序：镜1 → 镜2 → 镜3 →(转场)→ 镜4
        doc = {"id": "t_ch01", "profile": "anime", "motion": "native",
               "frame_chain": True, "aspect": "16:9",
               "shots": [self._shot(1, image=self._img(tmp, "s1.png")),
                         self._shot(2, image=self._img(tmp, "s2.png"), state="todo"),
                         self._shot(3, image=self._img(tmp, "s3.png")),
                         {"id": 9, "kind": "transition", "dur": 1.0,
                          "transition": {"type": "fade_black"}},
                         self._shot(4, image=self._img(tmp, "s4.png"))]}
        doc.update(over)
        return doc

    def test_approved_only_does_not_cross_link(self):
        """`--approved-only` 只是**本次渲染谁**的过滤，不改变成片相邻关系。
        链图从过滤后的列表取会把镜1 pin 到镜3（成片里它们并不相邻，且没有
        场景突变提示，比跨转场更隐蔽）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d, self._doc(d), approved_only=True)
            self.assertIn("末帧=镜2", out)          # 镜1 的末帧仍是成片里的下一镜
            self.assertNotIn("末帧=镜3", out)

    def test_dry_run_prompt_is_same_source_as_live_path(self):
        """预览提示词 = 实发提示词：dry-run 与主循环查同一张 chain_map。"""
        import tempfile
        from kinema.pipeline import prompts
        with tempfile.TemporaryDirectory() as d:
            out, project = self._run(d, self._doc(d))
            s1, s3 = project.shots[0], project.shots[2]
            self.assertIn(prompts.video_prompt(s1, native=True, flf2v=True), out)
            self.assertIn(prompts.video_prompt(s3, native=True, flf2v=False), out)
            self.assertIn(prompts.FLF2V_ZH, out)     # 镜1/镜2 衔接 → 写过渡

    def test_no_chain_across_transition_in_dry_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d, self._doc(d))
            self.assertNotIn("末帧=镜4", out)         # 镜3 的下一个是转场 → 断链

    def test_chapter_without_the_field_walks_reference_default(self):
        """章节文档没写 `frame_chain` 时**不衔接**——缺省档是逐镜全能参考
        （一镜一片、镜间直拼），且这是端到端成立的：不发末帧、提示词不写过渡、
        逐镜标「全能参考」。衔接是显式 opt-in（章级/镜级/--chain）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc.pop("frame_chain")
            out, _ = self._run(d, doc)
            self.assertNotIn("末帧=镜2", out)
            self.assertNotIn("末帧=镜3", out)
            from kinema.pipeline import prompts
            self.assertNotIn(prompts.FLF2V_ZH, out)
            self.assertIn("全能参考", out)

    def test_reference_default_attaches_design_sheets(self):
        """缺省全能参考的设定图附发（真发路径·mock 拦截）：角色/场景/道具设定图
        全挂 reference_image（角色优先序）、reference_only=True、无 last_frame——
        「一张分镜图+简笔画+设定图+场景图+道具图完整生成一镜」的端到端判据。"""
        import contextlib
        import io
        import json
        import tempfile
        from unittest import mock as _mock
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        from kinema.providers.base import VideoResult
        from kinema.providers.video.mock import MockVideoProvider
        with tempfile.TemporaryDirectory() as d:
            img = self._img(d, "s1.png")
            cs = self._img(d, "char.png")
            ss = self._img(d, "scene.png")
            ps = self._img(d, "prop.png")
            doc = {"id": "t_ch01", "motion": "native", "aspect": "16:9",
                   "characters": [{"name": "林深", "sheet": str(cs)}],
                   "scenes": [{"name": "废墟", "sheet": str(ss)}],
                   "props": [{"name": "怀表", "sheet": str(ps)}],
                   "shots": [{"id": 1, "dur": 4.0, "narration": "台词",
                              "characters": ["林深"], "scenes": ["废墟"],
                              "props": ["怀表"], "video_prompt": "镜1的运动",
                              "image": str(img),
                              "review": {"image": {"state": "done"}}}]}
            cf = Path(d) / "ch01.json"
            cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            project = Project.load(cf)
            store = ConfigStore.load(None)
            seen: dict = {}

            def _gen(self, image, out_path, **kw):
                seen.update(kw)
                return VideoResult(path=str(out_path), cost=0.0,
                                   has_audio=True, meta={})

            with _mock.patch.object(MockVideoProvider, "generate", _gen), \
                    contextlib.redirect_stdout(io.StringIO()):
                stage_gen_video(project, store, ModelRouter(store, force_mock=True))
            self.assertEqual(seen.get("ref_images"), [str(cs), str(ss), str(ps)])
            self.assertTrue(seen.get("reference_only"), "缺省档必须走参考生视频分支")
            self.assertIsNone(seen.get("last_frame"), "全能参考没有首/末帧槽")
            # 提示词逐张 @图片N 职责绑定与实附顺序同源（分镜图=@图片1，无板故
            # 设定图从 @图片2 起）——编号错位=场景图被当角色图用
            self.assertIn("@图片2 为角色「林深」的设定图", seen.get("prompt", ""))
            self.assertIn("@图片3 为场景「废墟」的设定图", seen.get("prompt", ""))
            self.assertIn("@图片4 为道具「怀表」的设定图", seen.get("prompt", ""))

    def test_per_shot_pair_opt_in_welds_one_seam_only(self):
        """镜级 `shots[].frame_chain: true`：缺省章里只焊点名那一处，
        其余镜照走全能参考——「用户要求做两个分镜的首尾帧」的落点。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc.pop("frame_chain")
            doc["shots"][0]["frame_chain"] = True     # 镜1 ↔ 镜2 结对
            out, project = self._run(d, doc)
            self.assertIn("末帧=镜2", out)             # 点名那道缝真发末帧
            self.assertNotIn("末帧=镜3", out)          # 其余镜不衔接
            from kinema.pipeline import prompts
            # 出链镜按首尾帧措辞拼装，其余镜按全能参考拼装——两套契约句并存
            self.assertIn(prompts.video_prompt(project.shots[0], native=True,
                                               flf2v=True), out)
            self.assertIn(prompts.CONTRACT_ALLREF_ZH, out)

    def test_break_reasons_are_spelled_out(self):
        """没发末帧的镜要说清原因，措辞取 `framechain.BREAK_ZH` 单一真源——
        一串「末帧=镜N」里夹着一镜没有、又不给理由，看起来就像漏了一镜。"""
        import tempfile
        from kinema.pipeline import framechain
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d, self._doc(d))
            self.assertIn(framechain.BREAK_ZH["transition"], out)   # 镜3：下一个是转场
            self.assertIn(framechain.BREAK_ZH["end"], out)          # 镜4：末镜

    def test_no_break_reason_when_chapter_does_not_chain(self):
        """不衔接的章节逐镜挂一句「不衔接」是噪音——原因只在衔接态下才有意义。"""
        import tempfile
        from kinema.pipeline import framechain
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["frame_chain"] = False
            out, _ = self._run(d, doc)
            for why in ("transition", "end", "no_image"):
                self.assertNotIn(framechain.BREAK_ZH[why], out, why)

    def test_missing_secondary_aspect_image_falls_back(self):
        """多比例保守判据：下一镜缺次比例图时整镜退回常规首帧生成——
        绝不出现「提示词按只写过渡瘦身、请求里却没末帧」的口径分叉。"""
        import tempfile
        from kinema.pipeline import prompts
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["image_per_aspect"] = True
            doc["aspects"] = ["16:9", "9:16"]
            doc["shots"][1] = self._shot(2, images={"16:9": self._img(d, "s2w.png")},
                                         state="todo")     # 缺 9:16
            # 镜3 两个比例都真有图——对照组，证明收紧后正常链路照旧走得通
            doc["shots"][2] = self._shot(3, images={"16:9": self._img(d, "s3w.png"),
                                                    "9:16": self._img(d, "s3t.png")})
            out, project = self._run(d, doc)
            self.assertNotIn("末帧=镜2", out)          # 镜1 整镜退回，不发末帧
            # 镜1 的提示词也随之退回常规 delta（不写过渡）——两者恒同源
            self.assertIn(prompts.video_prompt(project.shots[0], native=True, flf2v=False),
                          out)
            self.assertIn("末帧=镜3", out)             # 镜2→镜3 两比例都有图，照常衔接

    def test_secondary_aspect_falls_back_even_when_a_top_level_image_exists(self):
        """上一条构造的是「没有顶层 image」的形状，恰好绕开了真正的洞。

        `project.image_for` 的语义是「优先逐比例图、**回退主图**」——那对渲染是对的，
        对末帧是错的。下一镜若同时有顶层 `image` 与 `images["16:9"]`、独缺 `9:16`，
        旧判据会拿主比例那张图去收束一条 9:16 的请求，而
        `docs/agents/guard-map.md` 登记的不变量写的是「次比例缺图退回常规」——
        承诺与行为不符，且没有任何守卫看得见。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["image_per_aspect"] = True
            doc["aspects"] = ["16:9", "9:16"]
            wide = self._img(d, "s2w.png")
            doc["shots"][1] = self._shot(2, image=wide, images={"16:9": wide},
                                         state="todo")     # 有顶层图，仍缺 9:16
            out, _ = self._run(d, doc)
            self.assertNotIn("末帧=镜2", out,
                             "顶层 image 不该替次比例充数——那是拿 16:9 收束 9:16")

    def test_primary_aspect_still_uses_the_top_level_image(self):
        """收紧不许误伤默认路径：单比例项目根本不写 `images`，顶层 `image`
        按定义就是主比例图——那一支必须照旧走得通，否则整条链全断。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            out, _ = self._run(d, doc)
            self.assertIn("末帧=镜2", out)

    def test_omitted_neighbour_skipped_in_dry_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["shots"][1]["review"]["shot"] = {"state": "omt"}
            out, _ = self._run(d, doc)
            self.assertIn("末帧=镜3", out)             # 跳过弃用的镜2
            self.assertNotIn("末帧=镜2", out)

    def test_no_motion_design_warned_and_falls_back(self):
        import tempfile
        from kinema.pipeline import prompts
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["shots"][0].pop("video_prompt")
            doc["shots"][0]["image_prompt"] = "白发老者立于崖边，青衫猎猎"
            out, _ = self._run(d, doc)
            self.assertIn("镜 1: 无运动设计", out)
            self.assertNotIn("白发老者立于崖边", out)   # 绝不回退整条 image_prompt
            # 镜1 在本用例里是链上镜（末帧=镜2），落的是首尾帧兜底句而不是"保持不变"——
            # 两句互斥：末帧按定义是另一个构图，说"构图不变"会和"收束到末帧"打架。
            self.assertIn(prompts.FLF2V_FALLBACK_ZH, out)
            self.assertNotIn(prompts.DELTA_FALLBACK_ZH, out)
            self.assertIn("沿最短自然路径过渡到末帧", out)   # 日志与实发同源

    def test_unchained_shot_falls_back_to_hold(self):
        """非链上镜才落"保持不变"那句（与上面那条互为对照，防两句被写反）。"""
        import tempfile
        from kinema.pipeline import prompts
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["frame_chain"] = False
            doc["shots"][0].pop("video_prompt")
            out, _ = self._run(d, doc)
            self.assertIn(prompts.DELTA_FALLBACK_ZH, out)
            self.assertNotIn(prompts.FLF2V_FALLBACK_ZH, out)

    def test_delta_only_shot_is_not_reported_as_empty(self):
        """填了 delta 骨架位的镜**不该**被点名——日志与实发提示词必须一致，
        否则用户分不出哪一镜真的一笔运动设计都没有。"""
        import tempfile
        from kinema.pipeline import prompts
        with tempfile.TemporaryDirectory() as d:
            doc = self._doc(d)
            doc["shots"][0].pop("video_prompt")
            doc["shots"][0]["action"] = "从伏案缓缓直起上身"
            doc["shots"][0]["end_state"] = "手停在半空"
            out, _ = self._run(d, doc)
            self.assertNotIn("无运动设计", out)
            self.assertIn("动作：从伏案缓缓直起上身", out)
            self.assertIn("终态：手停在半空", out)
            self.assertNotIn(prompts.DELTA_FALLBACK_ZH, out)


class TestSupplyImage(unittest.TestCase):
    """素材直供（supply.py）：现成图登记为分镜画面——直供解说模式的地基。
    守卫点：拷入 images/ 同名规则、落待审、provider=supplied、覆盖前归档、
    done 锁定拒改、转场镜拒收、坏格式拒收。"""

    def _project(self, tmp):
        import json
        from kinema.project import Project
        cf = Path(tmp) / "ch01.json"
        cf.write_text(json.dumps({
            "id": "t_ch01", "profile": "anime",
            "shots": [{"id": 1, "narration": "解说"},
                      {"id": 2, "kind": "transition", "dur": 1.0,
                       "transition": {"type": "fade"}}],
        }, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    def _png(self, tmp, name="asset.png"):
        # 1×1 像素合法 PNG（无外部依赖）
        raw = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd4000000004945"
            "4e44ae426082")
        p = Path(tmp) / name
        p.write_bytes(raw)
        return p

    def test_supply_registers_and_marks_wfa(self):
        import tempfile
        from kinema import review, supply
        with tempfile.TemporaryDirectory() as d:
            proj = self._project(d)
            r = supply.supply_image(proj, 1, self._png(d))
            s = proj.shots[0]
            self.assertTrue(Path(s["image"]).is_file())
            self.assertIn("images", str(Path(s["image"]).parent))
            self.assertEqual(s["gen"]["image"]["provider"], "supplied")
            self.assertEqual(s["gen"]["image"]["cost"], 0.0)
            self.assertEqual(review.get_state(s, "image"), "wfa")   # 直供也落待审
            self.assertIsNone(r["archived"])                        # 首供无旧版

    def test_resupply_archives_previous(self):
        import tempfile
        from kinema import supply
        with tempfile.TemporaryDirectory() as d:
            proj = self._project(d)
            supply.supply_image(proj, 1, self._png(d, "a.png"))
            r2 = supply.supply_image(proj, 1, self._png(d, "b.png"))
            self.assertEqual(r2["archived"], 1)                     # 旧版进版本栈
            self.assertEqual(len(proj.shots[0]["versions"]["image"]), 1)
            self.assertEqual(proj.shots[0]["gen"]["image"]["version"], 2)

    def test_locked_transition_and_badext_rejected(self):
        import tempfile
        from kinema import review, supply
        from kinema.errors import ProjectError
        with tempfile.TemporaryDirectory() as d:
            proj = self._project(d)
            png = self._png(d)
            with self.assertRaises(ProjectError):                   # 转场镜拒收
                supply.supply_image(proj, 2, png)
            bad = Path(d) / "x.gif"
            bad.write_bytes(b"GIF89a")
            with self.assertRaises(ProjectError):                   # 坏格式拒收
                supply.supply_image(proj, 1, bad)
            review.set_state(proj.shots[0], "image", "done")        # done 锁定拒改
            with self.assertRaises(ProjectError):
                supply.supply_image(proj, 1, png)

    def test_aspect_variant_goes_to_images_map(self):
        import tempfile
        from kinema import supply
        with tempfile.TemporaryDirectory() as d:
            proj = self._project(d)
            supply.supply_image(proj, 1, self._png(d), aspect="9:16")
            s = proj.shots[0]
            self.assertIn("9:16", s.get("images") or {})
            self.assertTrue(Path(s["images"]["9:16"]).name.startswith("shot_1_9x16"))


class TestRuntimeOverride(unittest.TestCase):
    """**运行时覆盖绝不落盘**（`Project.override_runtime`）。

    `--motion/--aspect/--effects` 这类 flag 说的是"这一次这么跑"，不是"把章节改成
    这样"。覆盖若落盘：`assemble --kenburns`（这次想看静图版）经 stage_compose 收尾的
    `project.save()` 会把 native 章节的 motion **永久**改成 kenburns——此后 gen-video
    拒发、片段音轨不再被采用，而全程零提示。
    """

    def _proj(self, d, data):
        import json
        from kinema.project import Project
        p = Path(d) / "p.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project.load(p)

    def test_override_never_reaches_disk_but_stays_in_memory(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            proj = self._proj(d, {"motion": "native", "aspect": "16:9", "shots": []})
            proj.override_runtime("motion", "kenburns")
            self.assertEqual(proj.motion, "kenburns", "本次渲染必须按覆盖值跑")
            proj.data["output"] = {"16:9": fake_path("x.mp4")}   # 模拟 stage_compose 回填
            proj.save()
            disk = json.loads(proj.path.read_text(encoding="utf-8"))
            self.assertEqual(disk["motion"], "native", "磁盘上的 motion 一个字都不许改")
            self.assertEqual(disk["output"], {"16:9": fake_path("x.mp4")}, "真产物照常落盘")
            self.assertEqual(proj.motion, "kenburns", "save 之后内存仍是覆盖值")

    def test_absent_key_is_not_invented_on_disk(self):
        """磁盘上原本没有这个键 → 还原成"没有"，而不是写个 None 进去。"""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            proj = self._proj(d, {"shots": []})
            proj.override_runtime("motion", "native")
            proj.save()
            self.assertNotIn("motion", json.loads(proj.path.read_text(encoding="utf-8")))

    def test_repeated_saves_keep_restoring(self):
        """连跑多个比例=多次 save，每次都要还原（不是只挡住第一次）。"""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            proj = self._proj(d, {"motion": "native", "shots": []})
            proj.override_runtime("motion", "kenburns")
            for _ in range(3):
                proj.save()
            self.assertEqual(json.loads(proj.path.read_text(encoding="utf-8"))["motion"],
                             "native")
            self.assertEqual(proj.motion, "kenburns")

    def test_cli_flags_all_go_through_the_override(self):
        """源级：`_apply_aspect_args` 与 animatic 都不许再裸写 project.data。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli._apply_aspect_args)
        self.assertNotIn("project.data[", src, "运行时 flag 不许裸写 data")
        self.assertIn("override_runtime", src)
        self.assertIn('project.override_runtime("motion", "kenburns")',
                      inspect.getsource(cli.cmd_animatic),
                      "animatic 的强制静图也走同一套机制（不再手写 try/finally 还原）")


class TestComposeHonorsMotionForVisuals(unittest.TestCase):
    """画面取材尊重 motion：本地渲染模式（kenburns）**一律走分镜图**，
    即便盘上有 Seedance 片段也不取——这是「这次不要 seedance、用最基本的方式出片」
    的入口（`assemble --motion a`）。dubbed/native 下仍是逐镜混用（有片段用片段、
    没有回落静图），那是刻意的既有设计。"""

    def test_kenburns_ignores_existing_clips_but_native_uses_them(self):
        import json
        import tempfile

        from kinema.pipeline.checkpoint import has_file
        from kinema.project import Project
        with tempfile.TemporaryDirectory() as d:
            clip = Path(d) / "shot_1.mp4"
            clip.write_bytes(b"fake")
            p = Path(d) / "p.json"
            p.write_text(json.dumps({"motion": "native", "aspect": "16:9", "shots": [
                {"id": 1, "dur": 5.0, "clip": str(clip), "clips": {"16:9": str(clip)}}]}),
                encoding="utf-8")
            proj = Project.load(p)
            src = proj.clip_for(proj.shots[0], "16:9")
            self.assertTrue(has_file(src) and proj.uses_seedance, "native 下取片段")
            proj.override_runtime("motion", "kenburns")
            self.assertFalse(has_file(src) and proj.uses_seedance,
                             "kenburns 下即便片段在盘也不取（compose 同一条判据）")


class TestSaveMerge(unittest.TestCase):
    """Project.save 三方合并：磁盘上人工表态(review/comments) + 引擎子进程 append 的版本栈
    (versions) 都不被旧内存 save 静默回滚——分镜卡「vN」徽章因此不丢。"""

    def _proj(self, d, data):
        import json
        from kinema.project import Project
        p = Path(d) / "p.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Project.load(p)

    def test_disk_version_append_survives_stale_save(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            proj = self._proj(d, {"shots": [
                {"id": 1, "versions": {}, "review": {"image": {"state": "wfa"}}}]})
            # 另一进程（gen-image 重生时归档）在磁盘上 append 了 image v1
            disk = json.loads(proj.path.read_text())
            disk["shots"][0]["versions"] = {"image": [{"v": 1, "files": {}, "at": "x"}]}
            proj.path.write_text(json.dumps(disk), encoding="utf-8")
            # 引擎/Studio 用重生前的旧内存（versions 空）+ 人工 done 保存
            proj.shots[0].setdefault("review", {})["image"] = {"state": "done"}
            proj.save()
            saved = json.loads(proj.path.read_text())["shots"][0]
            # 版本 append 保留（磁盘为准）——徽章能显 v2
            self.assertEqual(len(saved["versions"]["image"]), 1)
            # 人工 done 也保留（磁盘没改 review → 引擎内存值照写）
            self.assertEqual(saved["review"]["image"]["state"], "done")

    def test_disk_human_review_wins_over_stale_engine(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            proj = self._proj(d, {"shots": [
                {"id": 1, "review": {"image": {"state": "wfa"}}}]})
            # 用户在 Studio 点了 done（落磁盘）
            disk = json.loads(proj.path.read_text())
            disk["shots"][0]["review"]["image"] = {"state": "done"}
            proj.path.write_text(json.dumps(disk), encoding="utf-8")
            # 引擎旧内存仍是 wfa 保存 → 人工 done 不被回滚
            proj.save()
            saved = json.loads(proj.path.read_text())["shots"][0]
            self.assertEqual(saved["review"]["image"]["state"], "done")


class TestAnimaticSkipsTransitions(unittest.TestCase):
    """`animatic` 的缺图预检必须与 `compose.build` 同源地跳过转场镜。

    转场镜按设计就没有分镜图（生图/配音/图生视频全跳过，字卡由合成段本地渲染）。
    漏了这道过滤，**任何加过转场的章节都跑不了 animatic**——零成本节奏审
    （节点④.5）直接不可达，而 compose/assemble 却能正常出片，表现极具迷惑性。"""

    def setUp(self):
        from tests.support import LocalBackendEnv
        self._env = LocalBackendEnv()
        self._env.enable()

    def tearDown(self):
        self._env.restore()

    def test_transition_shot_does_not_trip_missing_image_check(self):
        import inspect as _inspect

        from kinema import cli
        from kinema.pipeline import compose as compose_mod

        src = _inspect.getsource(cli.cmd_animatic)
        self.assertIn("is_transition", src,
                      "cmd_animatic 的缺图预检漏了转场过滤——带转场的章节将永远跑不了 animatic")
        # 与 compose.build 的同类预检同源（那边是正确范式，两处不许分叉）
        self.assertIn("is_transition", _inspect.getsource(compose_mod.build))

    def test_transition_chapter_passes_precheck(self):
        """行为级：带转场的章节跑 animatic，不得再报「缺分镜图的镜: [转场镜号]」。

        技巧：给正镜 `dur=0`，让命令在**紧随其后的时长预检**处停下——
        既证明了缺图预检没误拦转场镜，又不会真的跑进 ffmpeg 渲染（快、无产物）。"""
        import json
        import tempfile
        import types
        from pathlib import Path as _P

        from kinema import cli
        with tempfile.TemporaryDirectory() as d:
            img = _P(d) / "s1.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")
            doc = {"id": "t_ch01", "profile": "anime", "aspect": "16:9",
                   "shots": [
                       {"id": 1, "dur": 0, "narration": "台词", "image": str(img)},
                       {"id": 2, "kind": "transition", "dur": 1.0, "narration": "",
                        "transition": {"type": "fade_black", "text": "一天后"}},
                   ]}
            cf = _P(d) / "ch01.json"
            cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            args = types.SimpleNamespace(project=str(cf), chapter=None, workspace=None,
                                         config=None, profile=None, aspect=None,
                                         aspects=None, both=False, image_per_aspect=False,
                                         out=None, effects=None, no_effects=False,
                                         force=False, fps=None)
            with self.assertRaises(Exception) as ctx:
                cli.cmd_animatic(args)
            msg = str(ctx.exception)
            self.assertNotIn("缺分镜图", msg,
                             "转场镜被当成缺图镜拦下了——animatic 对带转场的章节不可达")
            self.assertIn("缺时长", msg, "应当走到时长预检，说明缺图预检已放行")


if __name__ == "__main__":
    unittest.main()
