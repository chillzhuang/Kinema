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

"""音色锚定（native 生视频按选角发声）四面守卫。

  · 计划：`voicecast.voice_anchor_plan` 只认显式选角（profile 缺省绝不锚）、
    旁白认旁白锁、同一把声音共享参考位、超限落 over（与未选角的 loose 分列）；
  · 提示词：绑定句三要素（编号点名/同嗓音/「不要复述」）与多段镜台词时间轴，
    无锚定、无 total 时逐字回归旧形态——这两条措辞经付费小样标定
    （性别对照 + 双音频分绑，见 docs/agents/voice-anchor.md），改动须重做实测；
  · 请求体：voice_anchors 只在全能参考合法、逐条挂 reference_audio、超条数限额
    是抛错不是截断（静默截断=绑定句与实附错位，模型拿错声音且无从发现）；
  · lint：voice_anchor_gap 只在章节表现出选角意图时点名未选角说话人。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import voicecast
from kinema.errors import ProviderError
from kinema.pipeline import prompts, variation
from kinema.project import Project
from tests.support import LocalBackendEnv


class _VStore:
    """音色解析桩：别名原样返回（引擎 ConfigStore.resolve_voice 的恒等特例）。"""

    def resolve_voice(self, ref):
        return ref or None

    def secret(self, name, required=True):
        return "test-key"


def _proj(data):
    return Project(Path("chapters") / "x.json", data)


class TestVoiceAnchorPlan(unittest.TestCase):
    def test_explicit_cast_only_profile_default_never_anchors(self):
        """profile 缺省音色不是选角决定——没有任何显式绑定时全员 loose。"""
        p = _proj({"shots": [], "voices": {}})
        shot = {"id": 1, "lines": [{"speaker": "陈昭", "text": "走。"}]}
        plan = voicecast.voice_anchor_plan(p, _VStore(), shot)
        self.assertEqual(plan["anchored"], [])
        self.assertEqual(plan["loose"], ["陈昭"])

    def test_voices_table_binding_anchors_with_stable_no(self):
        p = _proj({"voices": {"陈昭": "v_a", "林晚": "v_b"}})
        shot = {"id": 1, "lines": [{"speaker": "陈昭", "text": "你早就知道了。"},
                                   {"speaker": "林晚", "text": "我不知道！"}]}
        plan = voicecast.voice_anchor_plan(p, _VStore(), shot)
        self.assertEqual([(r["who"], r["voice_type"], r["no"])
                          for r in plan["anchored"]],
                         [("陈昭", "v_a", 1), ("林晚", "v_b", 2)])
        self.assertEqual(plan["loose"], [])

    def test_narrator_uses_the_narrator_voice_lock(self):
        """旁白锁是试音选定的显式决定，与角色选角同权；speaker 空的叙述句同样归旁白。"""
        p = _proj({"voices": {}, "narrator_voice": "v_n"})
        shot = {"id": 1, "lines": [{"speaker": "旁白", "text": "十年后。"},
                                   {"text": "他没有回答。"}]}
        plan = voicecast.voice_anchor_plan(p, _VStore(), shot)
        self.assertEqual([(r["who"], r["voice_type"]) for r in plan["anchored"]],
                         [(voicecast.NARRATOR_DISPLAY, "v_n")])

    def test_shared_voice_shares_one_reference_slot(self):
        """两个角色共用一把音色只占一条参考位（接口按条数限额）。"""
        p = _proj({"voices": {"陈昭": "v_a", "陈母": "v_a"}})
        shot = {"id": 1, "lines": [{"speaker": "陈昭", "text": "娘。"},
                                   {"speaker": "陈母", "text": "回来了。"}]}
        plan = voicecast.voice_anchor_plan(p, _VStore(), shot)
        self.assertEqual([r["no"] for r in plan["anchored"]], [1, 1])

    def test_over_limit_speakers_are_reported_apart_from_uncast_ones(self):
        """参考位不够与没选角是两种处置：前者再选角也没用，得减说话人或换档。
        混进同一个键就会把人指向一个改变不了任何事的动作。"""
        p = _proj({"voices": {"a": "v1", "b": "v2", "c": "v3"}})
        shot = {"id": 1, "lines": [{"speaker": n, "text": "。"} for n in "abcd"]}
        plan = voicecast.voice_anchor_plan(p, _VStore(), shot, max_refs=2)
        self.assertEqual(len(plan["anchored"]), 2)
        self.assertEqual(plan["over"], ["c"], "选了角但参考位满 → over")
        self.assertEqual(plan["loose"], ["d"], "压根没选角 → loose")


class TestVoiceAnchorClause(unittest.TestCase):
    def test_single_anchor_wording_matches_the_calibrated_prompt(self):
        c = prompts.voice_anchor_clause([{"who": "陈昭", "no": 1}])
        self.assertIn("陈昭的说话音色以所附参考音频为准", c)
        self.assertIn("完全相同的嗓音、音色与音高", c)
        self.assertIn("只提供音色，不要复述参考音频里的内容", c)

    def test_multi_anchor_binds_by_number_in_slot_order(self):
        c = prompts.voice_anchor_clause([
            {"who": "陈昭", "no": 1}, {"who": voicecast.NARRATOR_DISPLAY, "no": 2}])
        self.assertIn("陈昭的说话音色以 @配音1 为准", c)
        self.assertIn("画外旁白的说话音色以 @配音2 为准", c)
        self.assertIn("各自用与对应参考音频完全相同", c)

    def test_shared_slot_merges_speakers_into_one_binding(self):
        c = prompts.voice_anchor_clause([
            {"who": "陈昭", "no": 1}, {"who": "陈母", "no": 1}])
        self.assertIn("陈昭与陈母的说话音色以所附参考音频为准", c)

    def test_voice_description_rides_along_on_both_branches(self):
        """只发参考音等于让模型按画面猜这把嗓子多大年纪。声线描述在单锚定与
        多锚定两支都要发——单锚定（一镜一个说话人）才是最常见形态。"""
        d = "62 岁男性，嗓音低沉略带沙哑，语速偏慢"
        one = prompts.voice_anchor_clause([{"who": "陈昭", "no": 1, "desc": d}])
        self.assertIn(f"陈昭的说话音色以所附参考音频为准（{d}）", one)
        two = prompts.voice_anchor_clause([
            {"who": "陈昭", "no": 1, "desc": d},
            {"who": "林晚", "no": 2, "desc": "少女音，清亮"}])
        self.assertIn(f"陈昭的说话音色以 @配音1 为准（{d}）", two)
        self.assertIn("林晚的说话音色以 @配音2 为准（少女音，清亮）", two)

    def test_clause_is_byte_identical_without_a_description(self):
        """模版音色没有声线描述（档案里 prompt 恒空）——那一路的实发稿一个字不变。"""
        self.assertEqual(
            prompts.voice_anchor_clause([{"who": "陈昭", "no": 1, "desc": None}]),
            prompts.voice_anchor_clause([{"who": "陈昭", "no": 1}]))

    def test_description_newlines_are_folded(self):
        """描述是用户自由输入，换行原样进提示词会把绑定句劈成两半。"""
        c = prompts.voice_anchor_clause(
            [{"who": "陈昭", "no": 1, "desc": "中年男声\n语速偏慢"}])
        self.assertIn("（中年男声 语速偏慢）", c)

    def test_multiline_timeline_spans_by_char_ratio(self):
        """≥2 句 + total → 台词时间轴（与 scored 底稿同一份字数比例切分）。
        秒段取整：响应时间戳的那一代官方口径是「以 1 秒为单位」。"""
        shot = {"id": 1, "lines": [{"speaker": "陈昭", "text": "四字台词"},
                                   {"speaker": "林晚", "text": "这句是八个字呀"}]}
        c = prompts.native_voice_clause(shot, total=6.0)
        self.assertTrue(c.startswith("台词时间轴：0-2秒：陈昭说：“四字台词”"), c)
        self.assertIn("；2-6秒：林晚说：“这句是八个字呀”", c)

    def test_shot_unit_lists_lines_in_order_without_seconds(self):
        """不响应时间戳的那一代只按顺序逐句列——秒段在它那里是减分项。"""
        shot = {"id": 1, "lines": [{"speaker": "陈昭", "text": "四字台词"},
                                   {"speaker": "林晚", "text": "这句是八个字呀"}]}
        c = prompts.native_voice_clause(shot, total=6.0, unit="shot")
        self.assertNotIn("台词时间轴", c)
        self.assertNotIn("秒", c)
        self.assertLess(c.index("四字台词"), c.index("这句是八个字呀"), "顺序仍要在")

    def test_no_total_and_no_anchor_is_byte_identical_to_the_old_form(self):
        """回落态锁字节（与 test_dialogue 的钉法互补）：不传 total/锚定参数时输出恒为此形态。"""
        shot = {"speaker": "林深", "emotion": "angry", "narration": "你到底想干什么？"}
        self.assertEqual(prompts.native_voice_clause(shot),
                         "林深嘶声怒喝道：“你到底想干什么？”，口型与台词同步。")

    def test_voiceover_shot_keeps_closed_lips_and_appends_binding(self):
        """旁白锚定的三件必须同时在：念什么、不做口型、用哪把嗓音。

        绑定句的落点是「用与参考音频完全相同的嗓音说出台词」——旁白原文缺席时
        它指向一段从未给出的台词。"""
        shot = {"speaker": "旁白", "narration": "那一晚风很大。"}
        c = prompts.native_voice_clause(
            shot, anchors=[{"who": voicecast.NARRATOR_DISPLAY, "no": 1}])
        self.assertTrue(c.startswith("画外旁白 @配音1 讲述：“那一晚风很大。”"), c)
        self.assertIn(prompts.NARRATION_LIPS_ZH, c)
        self.assertIn("画外旁白的说话音色以所附参考音频为准", c)

    def test_video_prompt_carries_timeline_and_binding(self):
        """整条 native 提示词的集成面：cli 传 sketch_total 与 voice_anchors 后，
        时间轴与绑定句都要出现在实发文本里。"""
        shot = {"id": 1, "dur": 6, "video_prompt": "对峙。",
                "lines": [{"speaker": "陈昭", "text": "你早就知道了。"},
                          {"speaker": "林晚", "text": "我不知道！"}]}
        p = prompts.video_prompt(shot, native=True, sketch_total=6.0,
                                 voice_anchors=[{"who": "陈昭", "no": 1},
                                                {"who": "林晚", "no": 2}])
        self.assertIn("台词时间轴：", p)
        self.assertIn("陈昭 @配音1 说：“你早就知道了。”", p)
        self.assertIn("林晚 @配音2 说：“我不知道！”", p)
        self.assertIn("陈昭的说话音色以 @配音1 为准", p)
        self.assertIn("林晚的说话音色以 @配音2 为准", p)


class TestSeedanceVoiceAnchorRequest(unittest.TestCase):
    def _gen(self, *, voice_anchors=None, reference_only=True, _conn=None):
        from kinema.providers.video import seedance as m
        prov = m.SeedanceProvider({"price_per_second": 1.0, **(_conn or {})}, _VStore())
        captured = {}

        def fake(method, url, **kwargs):
            if method == "POST":
                captured["body"] = kwargs["json"]
                return _FakeResp({"id": "task-1"})
            return _FakeResp({"status": "succeeded",
                              "content": {"video_url": "https://x/v.mp4"}})

        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "f.png"
            img.write_bytes(b"\x89PNG fake")
            clips = []
            for i in range(len(voice_anchors or [])):
                a = Path(d) / f"anchor{i}.mp3"
                a.write_bytes(b"ID3 fake")
                clips.append(str(a))
            with mock.patch.object(m, "request_with_retry", fake), \
                 mock.patch.object(m, "download",
                                   lambda u, o, **k: Path(o).write_bytes(b"v")):
                prov.generate(str(img), f"{d}/out.mp4", prompt="对峙", dur=5,
                              reference_only=reference_only,
                              voice_anchors=clips if voice_anchors else None)
        return captured["body"]

    def test_anchors_append_reference_audio_items_after_images(self):
        b = self._gen(voice_anchors=[1, 2])
        roles = [c.get("role") for c in b["content"][1:]]
        self.assertEqual(roles, ["reference_image", "reference_audio",
                                 "reference_audio"],
                         "编号=附发顺序：音频必须排在全部图片之后、彼此保序")

    def test_first_frame_task_rejects_anchors(self):
        """首帧任务禁混参考媒体（官方铁律）——静默丢弃=绑定句指向不存在的参考。"""
        with self.assertRaises(ProviderError):
            self._gen(voice_anchors=[1], reference_only=False)

    def test_over_limit_raises_instead_of_truncating(self):
        with self.assertRaises(ProviderError) as ctx:
            self._gen(voice_anchors=[1, 2, 3], _conn={"max_ref_audios": 2})
        self.assertIn("限额", str(ctx.exception))


class _FakeResp:
    def __init__(self, jdata):
        self.status_code = 200
        self.text = ""
        self._j = jdata

    def json(self):
        return self._j


class TestDryRunMatchesRealSend(unittest.TestCase):
    def test_both_paths_share_the_anchor_plan(self):
        """dry-run 预览与真发各调同一个计划函数恰好一次（照 test_delivery 对
        request_seconds 的钉法）：第三处调用或各写判据，页面/清单说带锚定而
        实发没带（或反过来）就是按秒计费的那种不一致。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertEqual(src.count("= _anchor_plan_for(s, prov, ref_mode)"), 2)
        self.assertIn('voice_anchors=[c for _vt, c in item["va_refs"]] or None', src,
                      "工作线程必须把落料后的 clip 清单原样交给 provider")

    def test_fit_anchor_does_not_double_the_ffmpeg_prefix(self):
        """`ffmpeg.run` 自带 `ffmpeg -hide_banner -loglevel error -y` 前缀，
        调用方再传一个 "ffmpeg" 就是把可执行名拼成输入参数——裁剪静默失败、
        锚定音按原长发送、服务端按总时长拒绝。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertIn('_ffrun(["-i"', src)
        self.assertNotIn('_ffrun(["ffmpeg"', src)


class TestVoiceAnchorLint(unittest.TestCase):
    def _data(self, **over):
        base = {"motion": "native",
                "voices": {"陈昭": "v_a"},
                "shots": [{"id": 1, "lines": [
                    {"speaker": "陈昭", "text": "走。"},
                    {"speaker": "林晚", "text": "去哪。"}]}]}
        base.update(over)
        return base

    def _codes(self, data):
        return {f.code: f for f in variation.lint(data)}

    def test_gap_named_when_chapter_has_casting_intent(self):
        f = self._codes(self._data()).get("voice_anchor_gap")
        self.assertIsNotNone(f)
        self.assertIn("林晚", f.message)
        self.assertNotIn("陈昭", f.message, "已选角的角色不该被点名")

    def test_silent_when_no_casting_intent_at_all(self):
        """从没选过角的纯 native 章节是既定工作流，逐镜喊漂移只会淹掉真告警。"""
        self.assertNotIn("voice_anchor_gap",
                         self._codes(self._data(voices={})))

    def test_silent_on_dubbed_and_scored(self):
        self.assertNotIn("voice_anchor_gap",
                         self._codes(self._data(motion="dubbed")))
        self.assertNotIn("voice_anchor_gap",
                         self._codes(self._data(audio_mode="scored")))

    def test_narrator_lock_counts_as_cast(self):
        d = self._data(voices={},
                       narrator_voice="v_n",
                       shots=[{"id": 1, "narration": "十年后。", "speaker": "旁白"}])
        self.assertNotIn("voice_anchor_gap", self._codes(d))


if __name__ == "__main__":
    unittest.main()


class TestCastGateOrdering(unittest.TestCase):
    """选角闸必须排在任何一次 PromptEnvelope 编译之前，dry-run 与真发同一道。

    档案决定提示词里有没有音色绑定句；闸排在编译之后，预览、Studio 审阅锁与实发
    就会各拿一份稿：审阅锁按 `_prompt_sha` 比对，通过过的镜在真发时因 sha 变化被
    `⊘ 实发稿与审阅版不一致` 跳过，整镜不生成。
    """

    def test_gate_precedes_every_compile(self):
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        gate = src.index("_cast_gate(")
        self.assertLess(gate, src.index("if dry_run:"))
        self.assertLess(gate, src.index("aplan = _anchor_plan_for"))
        self.assertEqual(src.count("_cast_gate("), 1)
        self.assertIn("skip=no_auto_cast", src[gate:gate + 80])


class TestSeriesLookupHonoursWorkspace(unittest.TestCase):
    """系列文档按章节文件路径反推，不走工作区发现逻辑。

    调用方可能是 `--workspace` 指定的目录；重新发现会解析到另一个根，
    把音色写进同名的另一个项目，而内存回填又打在当前章节上。
    """

    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._env.restore()

    def test_resolves_from_the_chapter_path(self):
        from kinema import cli
        from kinema.project import Project
        from kinema.workspace import Workspace
        ws = Workspace.open(str(self.tmp / "custom"))
        s = ws.create_project("同名项目", pid="dup", profile="anime")
        s.add_character("张三")
        s.save()
        proj = Project.load(str(s.create_chapter("第一章")))
        got = cli._series_of(proj)
        self.assertIsNotNone(got)
        self.assertEqual(got.dir.parent.resolve(), ws.root.resolve())
        self.assertEqual([c["name"] for c in got.characters], ["张三"])

    def test_bare_project_file_has_no_series(self):
        from kinema import cli
        from kinema.project import Project
        f = self.tmp / "loose.json"
        f.write_text('{"shots": []}', encoding="utf-8")
        self.assertIsNone(cli._series_of(Project.load(str(f))))


class TestScannerAnchorScope(unittest.TestCase):
    """页面锚定注记与实发同任务型态。

    锚定参考音只在全能参考任务合法（首帧任务官方禁混参考媒体）——衔接参与镜、
    首帧锚定镜、previz 镜以及无参考图位的 provider 下，页面标「音色锚定」就是
    声称附了一条实发不带的参考音。判据组合真源在 `scanner.anchor_ref_task`，
    与 `cli._shot_plan` 的仲裁同序。
    """

    CAPS = {"refs": True, "last": True, "v2v": True}

    def _task(self, data, s, caps=None):
        from kinema.pipeline import framechain
        from kinema.studio import scanner
        shots = data.get("shots") or []
        chain_on = framechain.active(data, "native")
        cm = framechain.plan(shots, chain_on,
                             v2v=bool(data.get("previz_v2v")), native=True)
        return scanner.anchor_ref_task(
            data, s, motion="native", chain_on=chain_on, chain_map=cm,
            welded=framechain.welded_in_ids(cm), caps=caps or dict(self.CAPS))

    def _two_shots(self, **over):
        d = {"motion": "native",
             "shots": [{"id": 1, "narration": "一", "image": "a.png"},
                       {"id": 2, "narration": "二", "image": "b.png"}]}
        d.update(over)
        return d

    def test_default_chapter_shots_are_in_scope(self):
        d = self._two_shots()
        self.assertTrue(self._task(d, d["shots"][0]))
        self.assertTrue(self._task(d, d["shots"][1]))

    def test_frame_chain_weld_shots_are_out_of_scope(self):
        d = self._two_shots(frame_chain=True)
        self.assertFalse(self._task(d, d["shots"][0]), "出链镜是首帧任务")
        self.assertFalse(self._task(d, d["shots"][1]), "被焊入的镜以第 0 帧硬锁")

    def test_pair_weld_shots_are_out_of_scope(self):
        d = self._two_shots()
        d["shots"][0]["frame_chain"] = True
        self.assertFalse(self._task(d, d["shots"][0]))
        self.assertFalse(self._task(d, d["shots"][1]))

    def test_anchor_frame_chapter_is_out_of_scope(self):
        d = self._two_shots(anchor_frame=True)
        self.assertFalse(self._task(d, d["shots"][0]))

    def test_previz_v2v_shot_is_out_of_scope(self):
        clip = Path(tempfile.gettempdir()) / "kinema-anchor-scope-previz.mp4"
        clip.write_bytes(b"x")
        try:
            d = self._two_shots(previz_v2v=True)
            d["shots"][0]["previz"] = str(clip)
            self.assertFalse(self._task(d, d["shots"][0]))
            self.assertTrue(self._task(d, d["shots"][1]))
        finally:
            clip.unlink(missing_ok=True)

    def test_previz_last_frame_shot_is_out_of_scope(self):
        ref = Path(tempfile.gettempdir()) / "kinema-anchor-scope-last.png"
        ref.write_bytes(b"x")
        try:
            d = self._two_shots()
            d["shots"][0]["last_frame_ref"] = str(ref)
            self.assertFalse(self._task(d, d["shots"][0]))
        finally:
            ref.unlink(missing_ok=True)

    def test_provider_without_reference_slot_is_out_of_scope(self):
        d = self._two_shots()
        caps = dict(self.CAPS, refs=False)
        self.assertFalse(self._task(d, d["shots"][0], caps=caps))


class TestCastPlanHelpers(unittest.TestCase):
    """`speaking_owners` 是渲染侧与网页试听端点共用的选角覆盖面口径。"""

    def test_speaking_owners_normalises_the_narrator(self):
        from kinema import voicebank
        shots = [{"id": 1, "narration": "旁白句"},
                 {"id": 2, "lines": [{"speaker": "narrator", "text": "英文写法"},
                                     {"speaker": "林深", "text": "台词"}]},
                 {"id": 3, "lines": [{"speaker": "林深", "text": "再说一句"}]}]
        self.assertEqual(voicebank.speaking_owners(shots),
                         [voicebank.NARRATOR, "林深"])


class TestPreviewWarmSameCastPlan(unittest.TestCase):
    """试听端点与页面注记的接线钉。

      · 试听端点按盘上选角走 `voice_anchor_plan` 解析编号，与预览行同一份计划；
      · 章节页 chip 的判据必须含任务型态（`anchor_ref_task`）与 provider
        参考音位——页面标锚定而实发不带，是按秒计费链路上最贵的不一致。
    """

    def test_warm_endpoint_resolves_from_on_disk_casting(self):
        import inspect
        from kinema.studio import actions
        src = inspect.getsource(actions.voice_anchor_warm)
        self.assertIn("voice_anchor_plan(", src)

    def test_chip_gate_reads_task_shape_and_provider_slot(self):
        import inspect
        from kinema.studio import scanner
        src = inspect.getsource(scanner.chapter_detail)
        self.assertIn("anchor_ref_task(", src)
        self.assertIn("max_ref_audios", src)


class TestNativeBurnMute(unittest.TestCase):
    """native 混烧（native_voiceover）的声源分治面：旁白/无词镜闭声出演、
    人声走烧录轨；对白镜由模型原生发声、锚定照常附发。闭声决不落到对白镜上
    ——闭声稿执行无确定性，而对白烧录的两条时间轴不同源，平移救不了口型。"""

    def test_mute_is_inert_on_dialogue(self):
        """对白镜即便被传了 mute 也按发声编译——burn_muted 判据在编译端兜底，
        调用方标错旗子不会产出「闭声的对白镜」。"""
        c = prompts.native_voice_clause(
            {"speaker": "凯尔", "narration": "他们找到这儿了。"}, mute=True)
        self.assertIn("凯尔说：“他们找到这儿了。”", c)
        self.assertIn("口型与台词同步", c)
        self.assertNotIn("做口型、不发出", c)

    def test_mute_voiceover_closes_lips_without_content(self):
        """旁白由后期烧录：原文不进提示词，给了内容模型就可能念出来。
        非 mute 路径要求原文在场是因为模型要自己念，该前提在此不成立。"""
        c = prompts.native_voice_clause(
            {"speaker": "旁白", "narration": "那一晚风很大。"}, mute=True)
        self.assertIn(prompts.NARRATION_LIPS_ZH, c)
        self.assertIn(prompts.MUTE_VOICE_MARK_ZH, c)
        self.assertNotIn("那一晚风很大", c)

    def test_dialogue_keeps_binding_under_mute_flag(self):
        """对白镜的绑定句与 @配音 记号不因 mute 旗子丢失——混烧章的对白锚定
        照常附发，绑定句缺席即「音频附了、提示词没点名」的错位。"""
        c = prompts.native_voice_clause(
            {"speaker": "凯尔", "narration": "走。"}, mute=True,
            anchors=[{"who": "凯尔", "no": 1}])
        self.assertIn("@配音1", c)
        self.assertIn("参考音频", c)

    def test_video_prompt_keeps_dialogue_voiced_under_mute(self):
        p = prompts.video_prompt(
            {"id": 1, "dur": 5, "video_prompt": "推近。",
             "speaker": "凯尔", "narration": "走。"},
            native=True, native_mute=True)
        self.assertIn("口型与台词同步", p)
        self.assertNotIn("做口型、不发出", p)

    def test_anchor_plan_excludes_burn_muted_shots(self):
        """真发/预览共用的锚定计划按镜分治：混烧章只有闭声镜（旁白/无词）
        不锚定，对白镜照常（源级钉死判据在场）。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertIn("if project.native_voiceover and voicecast.burn_muted(s):",
                      src)


class _MutePng:
    BYTES = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082")


class TestBurnStagePreview(unittest.TestCase):
    """stage 级集成：同一章节只翻 native_voiceover 一个开关，预览的锚定清单
    与实发正文必须同步换向（页面/清单说带锚定而实发是闭声稿，正是最贵的那种
    不一致）。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_ctx.name)
        self.img = self.tmp / "shot1.png"
        self.img.write_bytes(_MutePng.BYTES)

    def tearDown(self):
        self.tmp_ctx.cleanup()
        self.env.restore()

    def _rows(self, burn: bool):
        import contextlib
        import io
        import json as _json

        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        cdir = self.tmp / "proj" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "ch01", "motion": "native", "aspect": "16:9",
               "voices": {"凯尔": "zh_male_test"},
               "shots": [{"id": 1, "dur": 5.0, "image": str(self.img),
                          "video_prompt": "回身。",
                          "speaker": "凯尔", "narration": "他们找到这儿了。"}]}
        if burn:
            doc["native_voiceover"] = True
        cf = cdir / "ch01.json"
        cf.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        store = ConfigStore.load(None)
        sink: list = []
        with contextlib.redirect_stdout(io.StringIO()):
            stage_gen_video(Project.load(cf), store,
                            ModelRouter(store, force_mock=True),
                            dry_run=True, preview_sink=sink)
        return sink

    def test_default_chapter_anchors_and_addresses(self):
        row = self._rows(burn=False)[0]
        self.assertTrue(row["anchors"], "选角在场的 native 镜必须带锚定")
        self.assertIn("凯尔 @配音1 说：“他们找到这儿了。”", row["positive"])

    def test_bare_chapter_still_maps_image_one(self):
        """零设定集章节 RefPlan 为 None，但参考任务仍附本镜画面且契约句写着
        @图片1——预览映射必须补上这一位，缺了页面把它渲染成不可点的失效记号。"""
        row = self._rows(burn=False)[0]
        self.assertTrue(row["refs"], "无设定集的参考任务也要有 @图片1 映射")
        self.assertEqual(row["refs"][0]["no"], 1)
        self.assertEqual(row["refs"][0]["kind"], "frame")
        self.assertTrue(row["refs"][0]["path"])

    def test_addressed_numbers_all_have_attached_refs(self):
        """提示词里点名的 @配音N 必须都有实附对象——点名一条不存在的参考音，
        模型会去找一个不存在的东西。"""
        import re
        row = self._rows(burn=False)[0]
        nos = {int(m) for m in re.findall(r"@配音(\d+)", row["positive"])}
        attached = {a["no"] for a in row["anchors"]}
        self.assertTrue(nos, "锚定镜的正文必须出现 @配音 记号")
        self.assertLessEqual(nos, attached)

    def test_burn_chapter_keeps_dialogue_anchored(self):
        """混烧章的对白镜声源在模型侧：锚定照常附发、正文照常要求对口型。
        整章一刀切闭声即把角色台词交给了一份不会被烧录的静音。"""
        row = self._rows(burn=True)[0]
        self.assertTrue(row["anchors"], "混烧章的对白镜锚定必须照常附发")
        self.assertIn("@配音", row["positive"])
        self.assertIn("口型与台词同步", row["positive"])


class TestVoiceAnchorLintBurnScope(unittest.TestCase):
    """混烧章的选角缺口按镜分治：闭声镜（旁白/无词）不锚定、其说话人不进判据；
    对白镜锚定照常附发，未选角照样催。"""

    def test_burn_muted_shot_is_out_of_scope(self):
        doc = {"motion": "native", "native_voiceover": True,
               "voices": {"阿岩": "v_a"},
               "shots": [{"id": 1, "dur": 4.0, "speaker": "旁白",
                          "narration": "夜里起了风。"}]}
        self.assertEqual([f for f in variation.lint(doc)
                          if f.code == "voice_anchor_gap"], [])

    def test_burn_dialogue_shot_still_reports(self):
        doc = {"motion": "native", "native_voiceover": True,
               "voices": {"阿岩": "v_a"},
               "shots": [{"id": 1, "dur": 4.0, "speaker": "阿树",
                          "narration": "跟紧我。"}]}
        self.assertTrue([f for f in variation.lint(doc)
                         if f.code == "voice_anchor_gap"])

    def test_same_chapter_without_burn_still_reports(self):
        doc = {"motion": "native", "voices": {"阿岩": "v_a"},
               "shots": [{"id": 1, "dur": 4.0, "speaker": "旁白",
                          "narration": "夜里起了风。"}]}
        self.assertTrue([f for f in variation.lint(doc)
                         if f.code == "voice_anchor_gap"])


class TestMuteVoiceFloor(unittest.TestCase):
    """闭声镜的负面人声地板：正文的「不生成任何人声」是正向指令，负面串是这类
    模型更听话的位置——两处同时说。实测残留形态：闭声稿下模型不念台词，但
    表演仍带出过零点几秒的哼声/气声。地板只落在烧录承担的镜上：压在对白镜的
    发声稿上就是自相矛盾的提示词。"""

    VO_SHOT = {"id": 1, "dur": 4, "video_prompt": "镜头扫过空屋。",
               "speaker": "旁白", "narration": "夜里起了风。"}
    DLG_SHOT = {"id": 2, "dur": 4, "video_prompt": "回身说话。",
                "speaker": "阿岩", "narration": "跟紧我。"}

    def test_mute_voiceover_negative_carries_the_floor(self):
        p = prompts.video_prompt(dict(self.VO_SHOT), native=True, native_mute=True)
        self.assertIn(prompts.MUTE_VOICE_FLOOR_ZH, p)

    def test_dialogue_never_gets_the_floor(self):
        p = prompts.video_prompt(dict(self.DLG_SHOT), native=True, native_mute=True)
        self.assertNotIn("哼唱", p, "对白镜要发声，人声地板压上去=禁台词")

    def test_non_mute_shot_stays_clean(self):
        p = prompts.video_prompt(dict(self.VO_SHOT), native=True)
        self.assertNotIn("哼唱", p, "非闭声镜禁人声=禁台词，地板只属于 mute")

    def test_envelope_negative_same_source(self):
        """Envelope.negative 与正文尾部的负面句必须同源——只改一处，Studio
        负面块与实发文本就不是同一份。"""
        from kinema.pipeline.prompts import PromptCompiler
        env = PromptCompiler().video(dict(self.VO_SHOT), native=True,
                                     native_mute=True)
        self.assertIn(prompts.MUTE_VOICE_FLOOR_ZH, env.negative)
        self.assertIn(prompts.MUTE_VOICE_FLOOR_ZH, env.prompt)

    def test_author_written_voice_ban_is_not_doubled(self):
        p = prompts.video_prompt({**self.VO_SHOT, "negative_prompt": "人声"},
                                 native=True, native_mute=True)
        self.assertNotIn(prompts.MUTE_VOICE_FLOOR_ZH, p)


class TestAnchorTextLength(unittest.TestCase):
    """锚定试音句的长度带：参考音时长直接决定音色跟随幅度（短句只到声区跟随、
    贴着 15s 上限的长句实测音高级贴合），过长会被单条预算裁剪、过短就回到声区档。"""

    def test_anchor_text_fills_the_reference_budget(self):
        n = len(voicecast.ANCHOR_TEXT)
        self.assertGreaterEqual(n, 50, "短于 50 字 ≈12s 以下，音色跟随幅度回落")
        self.assertLessEqual(n, 68, "长于 68 字 ≈16s，超出单条 15s 预算白合成")

    def test_cache_path_carries_the_text_fingerprint(self):
        """锚定文本换版必须让缓存自然失配：路径只按音色键命中时，旧文本的短
        样本会被继续发出去，音色跟随档位被静默拉低。"""
        p1 = voicecast.anchor_ref_path("/tmp/refs", "v_a")
        with mock.patch.object(voicecast, "ANCHOR_TEXT", "换了一版文本"):
            p2 = voicecast.anchor_ref_path("/tmp/refs", "v_a")
        self.assertNotEqual(p1, p2)
        with mock.patch.object(voicecast, "ANCHOR_TEXT", "换了一版文本"):
            self.assertEqual(p2, voicecast.anchor_ref_path("/tmp/refs", "v_a"),
                             "同文本同音色恒定路径，预热与实发才落同一个文件")

    def test_budget_cap_is_the_shared_split_rule(self):
        """dry-run 注记与真发裁剪同用一份均分口径——各算各的话，页面标的时长
        与实发的不是同一条。"""
        self.assertEqual(voicecast.anchor_budget_cap(15.0, 1), 14.8)
        self.assertEqual(voicecast.anchor_budget_cap(15.0, 2), 7.3)
        self.assertEqual(voicecast.anchor_budget_cap(15.0, 3), 4.8)
        self.assertEqual(voicecast.anchor_budget_cap(4.0, 3), 2.0,
                         "下限 2s：更短的参考音服务端直接拒收")


class TestBurnMutedPredicate(unittest.TestCase):
    """burn_muted 是混烧声源分治的唯一判据：提示词编译、锚定附发、页面标注
    三处共用——各写一份就会出现「页面标了锚定、实发却按闭声编译」。"""

    def test_kinds(self):
        self.assertFalse(voicecast.burn_muted(
            {"speaker": "凯尔", "narration": "走。"}), "对白镜发声，不闭声")
        self.assertTrue(voicecast.burn_muted(
            {"speaker": "旁白", "narration": "起风了。"}), "旁白镜闭声，人声走烧录")
        self.assertTrue(voicecast.burn_muted({"narration": ""}), "无词镜闭声")
