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

"""音频剧本（`audio_mode: scored`）：分段判据 · 剧本契约 · 与三轨混音的互斥。

守的是三件在真跑时才会露头、且每次露头都要花钱的事：
接缝切在哪 · 时间控制的秒基准 · 切到这条路后 tts/BGM 那一整套必须整体让开。
"""
from __future__ import annotations

import inspect
import json
import pathlib
import tempfile
import unittest
from pathlib import Path

from kinema import audioscript, voicecast
from kinema.errors import KinemaError
from kinema.project import Project
from tests.support import fake_path


def _shot(sid, dur, **kw):
    return {"id": sid, "dur": dur, "narration": f"第{sid}句", **kw}


def _tr(sid, dur=1.0):
    return {"id": sid, "dur": dur, "kind": "transition", "transition": {"style": "fade"}}


class _StoreStub:
    """音色目录桩：定制音色不在别名表里，原样落回解析结果。"""

    voices: dict = {}

    def resolve_voice(self, ref):
        return self.voices.get(ref, ref) if ref else None


def _proj(shots, **top):
    return Project(fake_path("never_written_audioscript.json"),
                   {"id": "t", "shots": shots, **top})


class TestSegmentPlan(unittest.TestCase):
    """分段：每个转场镜收一段，接缝永远落在转场上。"""

    def test_every_transition_closes_a_segment(self):
        # 转场即段界：镜 1~2 收第一段（转场是那段戏的收尾）、镜 3 起第二段——
        # 总长远低于上限也一样切，一幕一段、音乐在场景切换处重新起头
        segs = audioscript.plan(_proj([_shot(1, 20), _tr(2), _shot(3, 20)]))
        self.assertEqual([s["shots"] for s in segs], [[1, 2], [3]])
        self.assertAlmostEqual(segs[0]["dur"], 21.0)
        self.assertAlmostEqual(segs[1]["dur"], 20.0)

    def test_the_seam_falls_on_the_transition_not_where_the_limit_lands(self):
        # 上限若起作用会落在镜 5 中间，但接缝只认转场（镜 3）——
        # 音乐在场景切换处重起是正常听感，在一段戏中间断开不是
        shots = [_shot(1, 50), _shot(2, 30), _tr(3), _shot(4, 30), _shot(5, 40)]
        segs = audioscript.plan(_proj(shots))
        self.assertEqual([s["shots"] for s in segs], [[1, 2, 3], [4, 5]])
        self.assertLessEqual(segs[0]["dur"], audioscript.MAX_SEGMENT_SEC)

    def test_a_cold_open_transition_belongs_to_the_first_scene(self):
        # 章节以字卡/黑场冷开场：开场转场前面没有戏可收，不该切出一个
        # 只有转场、一句词都没有的空段——归第一段开头
        segs = audioscript.plan(_proj([_tr(1), _shot(2, 20), _tr(3), _shot(4, 10)]))
        self.assertEqual([s["shots"] for s in segs], [[1, 2, 3], [4]])

    def test_no_transition_means_no_hard_cut_and_check_names_it(self):
        # 两转场之间超限 → **不硬切**（宁可让 check 点名让人补转场）：
        # 段界只取台词间隙，多一次调用可接受，不在一句台词中间断开
        segs = audioscript.plan(_proj([_shot(1, 80), _shot(2, 80)]))
        self.assertEqual(len(segs), 1)
        problems = audioscript.check(_proj([_shot(1, 80), _shot(2, 80)]))
        self.assertEqual(len(problems), 1)
        self.assertIn("转场", problems[0])

    def test_omitted_shots_are_not_in_the_script_timeline(self):
        # 弃镜不进时间轴，剧本自然也不该给它写词
        # 整镜弃用挂在 review.shot 上（`review.is_omitted` 的判据），不是逐阶段的 image
        shots = [_shot(1, 30), _shot(2, 30, review={"shot": {"state": "omt"}}), _shot(3, 30)]
        segs = audioscript.plan(_proj(shots))
        self.assertEqual(segs[0]["shots"], [1, 3])
        self.assertAlmostEqual(segs[0]["dur"], 60.0)


class TestSegmentSpans(unittest.TestCase):
    """时间控制的秒基准——这条路上最贵的一类错（写错要重买整段）。"""

    def test_spans_restart_at_zero_every_segment(self):
        # 每段各自一次请求，模型时间轴每次从 0 起：段内秒段必须重新计时，
        # 照全片秒写会让第二段往后整体偏移一整段的长度
        shots = [_shot(1, 60), _tr(2), _shot(3, 60), _shot(4, 30)]
        segs = audioscript.plan(_proj(shots))
        self.assertEqual(len(segs), 2)
        self.assertGreater(segs[1]["start"], 0)          # 全片秒确实往后走了
        self.assertEqual(segs[1]["spans"][0]["start"], 0.0)   # 段内秒仍从 0 起
        for sg in segs:
            self.assertAlmostEqual(sg["spans"][-1]["end"], sg["dur"])

    def test_spans_cover_every_shot_of_the_segment_in_order(self):
        segs = audioscript.plan(_proj([_shot(1, 10), _shot(2, 20), _shot(3, 5)]))
        self.assertEqual([p["id"] for p in segs[0]["spans"]], [1, 2, 3])
        self.assertEqual([(p["start"], p["end"]) for p in segs[0]["spans"]],
                         [(0.0, 10.0), (10.0, 30.0), (30.0, 35.0)])


class TestSegmentScript(unittest.TestCase):
    """剧本契约：段数对不上宁可拒发，也不拿错段的文字去烧钱。"""

    def _two_segments(self):
        return [_shot(1, 60), _tr(2), _shot(3, 60), _shot(4, 30)]

    def test_missing_script_is_refused(self):
        with self.assertRaises(KinemaError):
            audioscript.segment_script(_proj([_shot(1, 10)]), {"no": 1})

    def test_single_string_script_only_valid_for_single_segment(self):
        p = _proj([_shot(1, 10)], audio_script="整段剧本")
        self.assertEqual(audioscript.segment_script(p, {"no": 1}), "整段剧本")
        multi = _proj(self._two_segments(), audio_script="整段剧本")
        with self.assertRaises(KinemaError) as cm:
            audioscript.segment_script(multi, {"no": 1})
        self.assertIn("segments", str(cm.exception))

    def test_segment_count_mismatch_is_refused_not_padded(self):
        # 分镜时长/转场改过而剧本没跟着改 → 拒发。硬发出去只会烧钱出错片
        p = _proj(self._two_segments(), audio_script={"segments": ["只写了一段"]})
        with self.assertRaises(KinemaError) as cm:
            audioscript.segment_script(p, {"no": 1})
        self.assertIn("2 段", str(cm.exception))

    def test_each_segment_gets_its_own_text(self):
        p = _proj(self._two_segments(), audio_script={"segments": ["A 段", "B 段"]})
        self.assertEqual(audioscript.segment_script(p, {"no": 1}), "A 段")
        self.assertEqual(audioscript.segment_script(p, {"no": 2}), "B 段")


class TestDraftFromStoryboard(unittest.TestCase):
    """按分镜起草：引擎只做确定性的那部分，而那正是最容易错、错了最贵的两件。

    这条路的正常用法是「引擎起稿 → AI 改写」——空框等人手写既慢又必然写错秒段，
    是被点名的那个设计错误。
    """

    def _proj(self):
        return _proj([
            {"id": 1, "narration": "雨下了整夜。", "dur": 12.0},
            {"id": 2, "dur": 8.0, "lines": [
                {"speaker": "林深", "text": "你还是来了。"},
                {"speaker": "阿箬", "text": "我以为你不会等这么久。"}]},
        ], narrator={"voice": "custom:vc_0001"},
            characters=[{"name": "林深", "voice": "custom:vc_0002"}, {"name": "阿箬"}],
            voice_bank={"seq": 2, "casts": [
                {"id": "vc_0001", "owner": "旁白", "mode": "custom",
                 "voice_type": "custom:vc_0001", "prompt": "中年男性，嗓音低沉"},
                {"id": "vc_0002", "owner": "林深", "mode": "custom",
                 "voice_type": "custom:vc_0002", "prompt": "青年男性，清亮"}]})

    def test_lines_are_copied_verbatim(self):
        text, _ = audioscript.draft_segment(self._proj(), audioscript.plan(self._proj())[0])
        # 字幕逐字取 narration，底稿改一个字成片就是「念的和写的不一样」
        for line in ("雨下了整夜。", "你还是来了。", "我以为你不会等这么久。"):
            self.assertIn(f"“{line}”", text)

    def test_timestamps_are_segment_relative_and_split_by_length(self):
        text, _ = audioscript.draft_segment(self._proj(), audioscript.plan(self._proj())[0])
        self.assertIn("[0.0s:12.0s]", text, "首句必须从段内 0 起")
        # 一镜两句按字数比例分窗：短句不该占掉和长句一样的时间
        import re
        spans = [(float(a), float(b)) for a, b in
                 re.findall(r"\[([\d.]+)s:([\d.]+)s\]", text)]
        self.assertEqual(spans[1][0], 12.0, "第二镜接着第一镜起")
        self.assertLess(spans[1][1] - spans[1][0], spans[2][1] - spans[2][0],
                        "「你还是来了」比「我以为你不会等这么久」短，窗口也该更短")
        self.assertAlmostEqual(spans[-1][1], 20.0, places=1, msg="末句收在段尾")

    def test_voice_desc_comes_from_the_cast_in_use(self):
        """取材是**在用**那把定制音色的原话。档案里还躺着历次选过的声音，
        按 owner 扫全表会取到一把已经不用的嗓子，底稿从此按错误声线写。"""
        p = self._proj()
        p.data["voice_bank"]["casts"].append(
            {"id": "vc_0003", "owner": "林深", "mode": "custom",
             "voice_type": "custom:vc_0003", "prompt": "早就换掉的那把"})
        self.assertEqual(audioscript.speaker_voice_desc(p, "林深"), ("青年男性，清亮", True))
        self.assertEqual(audioscript.speaker_voice_desc(p, "旁白"), ("中年男性，嗓音低沉", True))

    def test_missing_voice_desc_gets_a_usable_neutral_not_a_placeholder(self):
        text, thin = audioscript.draft_segment(self._proj(), audioscript.plan(self._proj())[0])
        self.assertEqual(thin, ["阿箬"], "缺声线描述的人要点名，好交给 AI 补")
        # 底稿会被原样发给模型：写「（待补）」这类占位就是把提示词泄进音轨
        for bad in ("待补", "待写", "TODO", "占位", "xxx"):
            self.assertNotIn(bad, text)
        self.assertIn("阿箬 是嗓音自然", text)

    def test_a_segment_with_no_dialogue_refuses_instead_of_emitting_an_empty_draft(self):
        p = _proj([{"id": 1, "dur": 5.0}])          # 纯画面段
        with self.assertRaises(KinemaError):
            audioscript.draft_segment(p, audioscript.plan(p)[0])

    def test_studio_draft_does_not_write_to_disk(self):
        # 「我点一下看看」不该变成一次静默覆盖：网页起草只回文本、由存稿落盘
        import inspect

        from kinema.studio import actions
        src = inspect.getsource(actions.draft_audio_script)
        self.assertNotIn("project.save()", src)
        self.assertIn("audioscript.draft(project)", src)


class TestVoiceAnchoringSurvivesTheScoredRoute(unittest.TestCase):
    """选定/定制过的音色必须随请求发出去，否则整章白做。

    `prompt_only` 是"按文字描述凭空造一把声音"——生成式模型每段各造一把，
    两段之间的旁白就是两个人，外面选定的音色也完全不参与。整章剧本若全程
    `prompt_only=True`，声线描述再对、音色也会每段都在漂。
    """

    @staticmethod
    def _src():
        import inspect

        from kinema import cli
        return inspect.getsource(cli.stage_score) + inspect.getsource(cli._score_anchor)

    def test_reference_audio_is_sent_not_just_the_description(self):
        src = self._src()
        self.assertIn("ref_audios=row[\"refs\"]", src, "参考音必须随请求发出")
        self.assertIn("prompt_only=not row[\"refs\"]", src,
                      "只有一把声音都没锚定的段才退回纯描述生成")

    def test_anchor_resolution_reuses_the_one_voice_chain(self):
        src = self._src()
        self.assertIn("anchor_plan", src, "谁能锚上只该有一份判据")
        self.assertIn("voicebank.clip_for", src, "定制音色用档案里那条不可变音频")
        self.assertIn("voicecast.default_voice_ref(", src, "默认音色回落链只有一份实现")
        chain = inspect.getsource(voicecast.default_voice_ref)
        self.assertLess(chain.index("narrator_voice"), chain.index('get("voice")'),
                        "旁白选定的音色优先于 profile 默认")
        plan_src = inspect.getsource(audioscript.anchor_plan)
        self.assertIn("resolve_line_voice", plan_src, "音色优先级只该有一份实现")
        self.assertNotIn("synthesize", plan_src,
                         "计划是纯判定：网页要在花钱之前显示它，不能顺手合成参考音")

    def test_the_page_and_the_request_read_the_same_plan(self):
        """页面说带音色而实发没带，是这条路最贵的一种不一致（按秒计费）。"""
        from kinema.studio import scanner
        self.assertIn("audioscript.anchor_plan",
                      inspect.getsource(scanner._audio_script_view))

    def test_the_page_builds_default_ref_on_the_same_fallback_chain(self):
        """anchor_plan 函数同一份而输入不同源照样分叉：default_ref 的回落链
        （旁白锁 > profile 默认 > 项目 voice_id）页面必须与真发一致，否则
        未选旁白音色的章节会被误报「⚠ 无参考音」。"""
        from kinema.studio import scanner
        self.assertIn("voicecast.default_voice_ref(", inspect.getsource(scanner._audio_script_view))

    def test_the_page_audition_plays_the_same_clip_the_request_sends(self):
        """「♪ 参考音」的试听必须是实发那条：在盘事实统一走
        `voicebank.anchor_clip_for`（定制=档案 clip、官方=锚定缓存，缓存命名取
        `voicecast.anchor_ref_path` 单一真源）——命名各拼一份，页面试听的就
        可能不是发出去的那条。"""
        from kinema import cli, voicebank
        from kinema.studio import scanner
        self.assertIn("voicebank.anchor_clip_for",
                      inspect.getsource(scanner._audio_script_view))
        helper = inspect.getsource(voicebank.anchor_clip_for)
        self.assertIn("anchor_ref_path", helper)
        self.assertIn("clip_for", helper)
        self.assertIn("anchor_ref_path", inspect.getsource(cli._anchor_clip),
                      "发送侧与页面必须共用同一条命名")

    def test_the_page_judges_segment_presence_with_url_awareness(self):
        """oss sync 会把 `gen.score.segments[].file` 改写成 URL：按 Path.is_file
        判会把已买断的段翻成「未生成」，生成弹窗还会默认勾选重买一遍。"""
        from kinema.studio import scanner
        src = inspect.getsource(scanner._audio_script_view)
        self.assertIn("has_file(part)", src)
        self.assertNotIn("part.is_file()", src)
        self.assertIn('get("score_file")', src, "整轨要回落 audio.score_file（可能是 URL）")

    def test_binding_line_does_not_restate_what_the_script_already_says(self):
        """绑定行只负责把名字与 `@音频N` 对上。剧本的声线定义段已经写了这个人，
        再补一份就是同一句描述在正文里出现两次，还白占 3000 字符额度。"""
        from kinema import cli
        # clip 指向本文件：解析只要求它在盘上，内容不参与判定
        p = _proj([{"id": 1, "narration": "一句", "dur": 4.0}],
                  narrator={"voice": "custom:vc_0001"},
                  voice_bank={"seq": 1, "casts": [
                      {"id": "vc_0001", "owner": "旁白", "mode": "custom",
                       "voice_type": "custom:vc_0001", "prompt": "低沉的中年男声",
                       "clip": __file__}]})
        seg = audioscript.plan(p)[0]
        store = _StoreStub()
        drafted = audioscript.draft_segment(p, seg)[0]
        self.assertIn("旁白 是低沉的中年男声", drafted)

        _refs, bind, _loose = cli._score_anchor(
            p, store, None, seg, script=drafted,
            ref_dir=fake_path(), default_ref="custom:vc_0001")
        self.assertEqual(bind.strip(), "旁白 的饰演者为@音频1。")
        # 手写剧本没写声线定义时才补描述——参考音必须附带声线绑定说明
        _refs2, bind2, _ = cli._score_anchor(
            p, store, None, seg, script="旁白说道：“喂。”",
            ref_dir=fake_path(), default_ref="custom:vc_0001")
        self.assertEqual(bind2.strip(), "旁白 是低沉的中年男声，饰演者为@音频1。")

    def test_the_request_wires_the_script_into_the_anchor(self):
        """接线守卫：`plan()` 的段字典不含剧本键，真发调用必须显式把 `r["text"]`
        传给 `_score_anchor`——断了这根线 `has_voice_def` 恒假，每段的声线描述
        都会重复发一遍（只测助手函数抓不住这一断口）。"""
        from kinema import cli
        src = inspect.getsource(cli.stage_score)
        self.assertIn('script=r["text"]', src)

    def test_a_custom_voice_without_its_clip_is_an_error_not_a_downgrade(self):
        """定制音色的参考音就是音色本身：缺了静默退回纯文字描述等于换人配音，
        且这一段照发照扣费——必须显式报错（与 stage_tts 同一条纪律）。"""
        from kinema import cli
        p = _proj([{"id": 1, "narration": "一句", "dur": 4.0}],
                  narrator={"voice": "custom:vc_0404"},
                  voice_bank={"seq": 1, "casts": [
                      {"id": "vc_0404", "owner": "旁白", "mode": "custom",
                       "voice_type": "custom:vc_0404", "prompt": "低沉的中年男声",
                       "clip": fake_path("vc_0404.mp3")}]})
        seg = audioscript.plan(p)[0]
        with self.assertRaises(KinemaError):
            cli._score_anchor(p, _StoreStub(), None, seg, script="",
                              ref_dir=fake_path(), default_ref="custom:vc_0404")

    def test_two_speakers_sharing_one_voice_occupy_one_reference_slot(self):
        """接口上限按参考音**条数**算：两个角色共用一把音色时各占一位，等于白丢
        一个真正需要锚定的说话人，还把同一条 clip 发两遍。多个名字绑同一个
        `@音频N`，编号必须等于该 clip 在 refs 数组里的下标。"""
        from kinema import cli
        p = _proj([{"id": 1, "dur": 4.0, "narration": "甲的话", "speaker": "甲"},
                   {"id": 2, "dur": 4.0, "narration": "乙的话", "speaker": "乙"}],
                  voices={"甲": "custom:vc_0001", "乙": "custom:vc_0001"},
                  voice_bank={"seq": 1, "casts": [
                      {"id": "vc_0001", "owner": "甲", "mode": "custom",
                       "voice_type": "custom:vc_0001", "prompt": "低沉的中年男声",
                       "clip": __file__}]})
        seg = audioscript.plan(p)[0]
        got = audioscript.anchor_plan(p, _StoreStub(), seg, None)
        self.assertEqual([r["no"] for r in got["anchored"]], [1, 1])
        refs, bind, loose = cli._score_anchor(p, _StoreStub(), None, seg, script="",
                                              ref_dir=fake_path())
        self.assertEqual(len(refs), 1, "同一把声音只发一条参考音")
        self.assertEqual(loose, [])
        self.assertEqual(bind.count("@音频1"), 2, "两个名字都要绑到同一条参考音上")

    def test_a_custom_clip_rewritten_to_a_url_is_still_anchored(self):
        """oss sync 会把档案 clip 改写成 URL——适配器的参考音条目本就 URL 感知
        （audio_url），对 URL 抛「找不到参考音」等于劝用户把定制音色重买一遍。"""
        from kinema import cli
        url = "https://oss.example.com/assets/voices/casts/vc_0001.mp3"
        p = _proj([{"id": 1, "narration": "一句", "dur": 4.0}],
                  narrator={"voice": "custom:vc_0001"},
                  voice_bank={"seq": 1, "casts": [
                      {"id": "vc_0001", "owner": "旁白", "mode": "custom",
                       "voice_type": "custom:vc_0001", "prompt": "低沉的中年男声",
                       "clip": url}]})
        seg = audioscript.plan(p)[0]
        refs, _bind, loose = cli._score_anchor(p, _StoreStub(), None, seg, script="",
                                               ref_dir=fake_path(),
                                               default_ref="custom:vc_0001")
        self.assertEqual(refs, [url])
        self.assertEqual(loose, [])

    def test_anchoring_runs_on_the_main_thread_not_per_worker(self):
        # 官方音色的参考音要现合成且全项目共用一个缓存文件，
        # 放进工作线程 = 几段并发各合成一遍同一段参考音
        src = self._src()
        self.assertLess(src.index("_score_anchor("), src.index("parallel.run"))

    def test_over_the_interface_cap_is_reported_not_silently_dropped(self):
        from kinema import voicecast
        self.assertEqual(voicecast.MAX_ANCHOR_REFS, 3)
        self.assertIn("loose", self._src(), "锚不上的说话人要点名")

    def test_every_reference_audio_is_bound_to_a_name(self):
        """一段音频不说明是谁的，模型无从把它派给哪个说话人。气质约束由剧本的
        声线定义段给，描述取档案里那一份、不另写一版。"""
        from kinema import cli
        src = inspect.getsource(cli._score_anchor)
        self.assertIn("饰演者为@音频", src)
        self.assertIn("speaker_voice_desc", src, "补描述时取档案那一份，不另写一版")
        self.assertIn("has_voice_def", src, "剧本写没写过这个人，判据只有一份")


class TestScriptSurvivesEngineSave(unittest.TestCase):
    """三方合并白名单：`audio_script`/`audio_mode` 是「人在长任务期间的表态」。

    score 整轨一跑数分钟，期间网页的存稿/路线切换先落盘——不进
    `_DOC_HUMAN_KEYS`，任务收尾的 save 会用开跑时的旧内存副本把它们静默抹掉。
    """

    def setUp(self):
        from tests.support import LocalBackendEnv
        self._env = LocalBackendEnv()
        self._env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ch01.json"

    def tearDown(self):
        self._tmp.cleanup()
        self._env.restore()

    def test_studio_save_survives_a_running_score_job(self):
        doc = {"id": "ch01", "audio_mode": "scored",
               "audio_script": {"segments": ["旧剧本"]},
               "shots": [{"id": 1, "narration": "开场", "dur": 4.0}]}
        self.path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        engine = Project.load(self.path)      # T0：score 任务开跑，持有旧副本
        side = Project.load(self.path)        # T1：网页存稿 + 切回三轨
        side.data["audio_script"] = {"segments": ["改过的剧本"]}
        side.data.pop("audio_mode", None)
        side.save()
        engine.data.setdefault("gen", {})["score"] = {"provider": "doubao"}
        engine.save()                         # T2：任务收尾
        disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["audio_script"]["segments"], ["改过的剧本"],
                         "score 任务收尾把网页刚存的剧本吞了")
        self.assertNotIn("audio_mode", disk, "路线切换被任务收尾回滚")
        self.assertEqual(disk["gen"]["score"]["provider"], "doubao",
                         "引擎自己的登记不该被合并丢掉")


class TestSegmentSig(unittest.TestCase):
    """段指纹：逐段幂等的判据（复用一段已付费的音轨，前提是它还对得上）。"""

    def test_sig_changes_with_script_shots_or_duration(self):
        base = {"shots": [1, 2], "dur": 30.0}
        s0 = audioscript.segment_sig(base, "剧本")
        self.assertNotEqual(s0, audioscript.segment_sig(base, "剧本改了"))
        self.assertNotEqual(s0, audioscript.segment_sig({**base, "shots": [1, 2, 3]}, "剧本"))
        self.assertNotEqual(s0, audioscript.segment_sig({**base, "dur": 31.0}, "剧本"))

    def test_sig_is_stable_for_identical_input(self):
        base = {"shots": [1], "dur": 5.0}
        self.assertEqual(audioscript.segment_sig(base, "同一段"),
                         audioscript.segment_sig(base, "同一段"))

    def test_separator_prevents_field_bleed(self):
        # 「文字末尾多一截」与「镜号少一个」绝不能哈希成同一个值
        self.assertNotEqual(audioscript.segment_sig({"shots": [1], "dur": 2.0}, "a"),
                            audioscript.segment_sig({"shots": [], "dur": 2.0}, "a[1]"))


class TestScoredModeIsExclusive(unittest.TestCase):
    """切到音频剧本后，逐镜 TTS + BGM 那一整套必须整体让开——叠上去就是两层人声。"""

    def test_audio_mode_defaults_to_tracks_and_only_scored_switches(self):
        self.assertEqual(_proj([_shot(1, 5)]).audio_mode, "tracks")
        self.assertFalse(_proj([_shot(1, 5)]).scored_audio)
        for bad in ("", None, "SCORED", "score", "tracks"):
            self.assertEqual(_proj([_shot(1, 5)], audio_mode=bad).audio_mode, "tracks",
                             f"只有精确的 'scored' 才切路线，收到 {bad!r}")
        self.assertTrue(_proj([_shot(1, 5)], audio_mode="scored").scored_audio)

    def test_scored_never_needs_per_shot_tts(self):
        # 人声由音频模型随音乐音效一起生成，再叠逐镜 TTS = 同一句台词两个人说
        for motion in ("kenburns", "dubbed"):
            self.assertTrue(_proj([_shot(1, 5)], motion=motion).needs_tts)
            self.assertFalse(_proj([_shot(1, 5)], motion=motion,
                                   audio_mode="scored").needs_tts)

    def test_tracks_mode_is_untouched_by_the_new_route(self):
        # 回归钉子：缺省路线的两个判据一个字都不该变
        p = _proj([_shot(1, 5)], motion="kenburns")
        self.assertEqual((p.audio_mode, p.needs_tts, p.scored_audio),
                         ("tracks", True, False))


class TestAssembleQuotesBeforeBuyingTheTrack(unittest.TestCase):
    """`assemble` 会经 `_stage_audio_bed` 直接调 `stage_score`，那条路按秒买断整章。
    审阅闸拦不住它——scored 章 needs_tts 恒假，audio 支整个不参与判定。"""

    class _Args:
        yes = False

    class _Router:
        """报价只读 provider 的单价，不解析真凭证。"""

        def resolve_named(self, _kind, _name):
            return type("P", (), {"price_per_second": 0.0167})()

    def _run_gate(self, project):
        from kinema import cli
        return cli._score_gate(project, None, self._Router(), self._Args())

    def test_missing_track_is_refused_with_a_quote(self):
        p = _proj([_shot(1, 5)], audio_mode="scored",
                  audio_script={"segments": [{"no": 1, "text": "一段"}]})
        with self.assertRaises(KinemaError) as cm:
            self._run_gate(p)
        self.assertIn("按秒买断", str(cm.exception))
        self.assertIn("--dry-run", str(cm.exception), "得给出零成本预览的出口")

    def test_existing_track_passes_without_touching_the_router(self):
        """整轨在盘时 stage_score 自身按段幂等复用，不该再解析 provider 拦一道。"""
        p = _proj([_shot(1, 5)], audio_mode="scored",
                  audio={"score_file": __file__})
        self.assertIsNone(self._run_gate(p))

    def test_tracks_mode_is_never_gated(self):
        self.assertIsNone(self._run_gate(_proj([_shot(1, 5)], motion="kenburns")))


class TestNeverPaysTwiceForTheSameTrack(unittest.TestCase):
    """整轨已经有了就到此为止——这条路按秒计费，重复购买是最贵的一类回归。"""

    @staticmethod
    def _src():
        import inspect

        from kinema import cli
        return inspect.getsource(cli.stage_score)

    def test_ready_check_accepts_a_cloud_url_not_just_a_local_file(self):
        # `score_NN.mp3` 是中间物、不进契约也不上云：只按段文件判的话，换台机器
        # （或 sync 完清过工作目录）会判成「一段都没生成」，整章按秒重买一遍
        src = self._src()
        self.assertIn("is_url", src, "已上云的 score_file 是 URL，必须认")
        self.assertIn("has_file(project.audio.get(\"score_file\"))", src)
        self.assertIn("not force and not only", src,
                      "只有没人点名重生时才早退——--force/--only 是显式购买意图")

    def test_ready_check_runs_before_the_per_segment_burn_list(self):
        # 顺序错了这道闸就是死代码：段文件不在盘时 burn 早已全为真
        src = self._src()
        self.assertLess(src.index("if ready and not force"),
                        src.index("burn = [r for r in rows"),
                        "整轨判据必须排在逐段 burn 清单之前")

    def test_empty_concat_product_is_deleted_not_left_behind(self):
        src = self._src()
        self.assertIn("unlink(missing_ok=True)", src,
                      "拼出空文件要就地删——留着下次会被当成「已在盘」拿去合成")

    def test_anchor_validation_runs_before_the_archive_moves_files(self):
        # 定制参考音缺失在锚定处抛错；归档已经搬走盘上的现存段，抛在它
        # 后面 = 尚未生成就已搬动现存段（与 _regen_gate 的「预演不许有副作用」同款纪律）
        src = self._src()
        self.assertLess(src.index("_score_anchor("),
                        src.index("archive_score_segment"))

    def test_archive_ledger_is_persisted_before_the_burn_starts(self):
        # 归档移动了盘上文件，版本账只活在内存时，任一段生成失败即整批丢账：
        # versions/ 留下无账孤儿，下次归档按 len(hist)+1 重发同名文件，
        # shutil.move 会把已付费的旧演绎静默覆盖
        src = self._src()
        i_archive = src.index("archive_score_segment")
        i_burn = src.index("parallel.run")
        self.assertLess(i_archive, i_burn)
        self.assertIn("project.save()", src[i_archive:i_burn],
                      "归档后、真发前必须落盘一次版本账")

    def test_successful_segments_are_registered_before_any_failure_is_raised(self):
        # 登记与记账先于抛错（并发纪律 1.3）：钱已经花了，任一段失败不能让
        # 成功段丢 sig——否则下次重跑报「剧本已改」诱导 --only 再买一遍
        src = self._src()
        i_reg = src.index('setdefault("gen", {}).setdefault("score"')
        i_raise = src.index("已生成的段已登记入账")
        self.assertLess(i_reg, i_raise, "gen.score 登记必须排在失败抛错之前")
        self.assertLess(src.index('add_cost("score"'), i_raise,
                        "费用入账必须排在失败抛错之前")

    def test_run_and_assemble_do_not_inherit_force_into_score(self):
        # run/assemble 的 --force 说的是「重跑本地产物」；score 的 force 是整章
        # 按秒重新买断——两者语义不同，顺路继承等于把重渲成片放大成重复购买
        from kinema import cli
        src = inspect.getsource(cli._stage_audio_bed)
        call = src[src.index("stage_score("):]
        call = call[:call.index(")")]
        self.assertNotIn("force", call, "选曲不得把 force 下传给 stage_score")
        # run/assemble 共用同一段选曲：各写一份就会一边继承 force、一边不继承
        for fn in (cli.cmd_run, cli.cmd_assemble):
            body = inspect.getsource(fn)
            self.assertIn("_stage_audio_bed(", body)
            self.assertNotIn("stage_score(", body)


class TestEditingInteractionCannotBurnTheWrongScript(unittest.TestCase):
    """框里的 ≠ 盘上的，而生成发的是**盘上**那份——这条不点破就是按秒计费烧旧稿。

    起草完直接点「生成整轨」是最容易撞上的一条路径（底稿刚填进框，一个字都还没存）。
    """

    @staticmethod
    def _src():
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[1]
                / "kinema/studio_app/app/chapter.js").read_text(encoding="utf-8")

    def test_unsaved_edits_disable_the_paid_button(self):
        src = self._src()
        card = src.split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("markDirty", card)
        # 软禁用：主钮拦在 onclick 里并 toast 说明——disabled 元素不派发鼠标事件，
        # 「为什么点不动」的提示会整个哑掉
        self.assertIn("if (unsaved)", card, "有未存改动时主钮必须拦住并说明")
        self.assertIn("盘上", card, "提示必须说清生成发的是哪一份")

    def test_typing_pauses_the_poll_but_engine_prefill_does_not(self):
        """暂停轮询只认「用户真敲过字」。

        预填的底稿是每次重绘都会重新算出来的同一份，重绘不会丢它——
        为它停掉全页轮询是过度反应（页面从此不再自动跟进后台任务进度）。
        而手敲的内容重绘一次就没了，必须停。"""
        card = self._src().split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("state.live = !on", card, "手敲过要停轮询、存稿即恢复")
        self.assertIn("typed = true", card, "两个状态必须分开：unsaved ≠ dirty")
        # 初始 unsaved 由预填决定，但初始 dirty 恒 false（没人敲过）
        self.assertIn("let dirty = false", card)
        # 手稿必须暂存在渲染之外：重渲不止轮询一条路（softRefresh/refreshAfterWrite
        # 都直接重建整卡），只靠停轮询接不住
        self.assertIn("AUS_DRAFTS", card, "未存手稿要暂存在渲染闭包之外")

    def test_engine_prefills_the_draft_instead_of_showing_an_empty_box(self):
        """能确定性算出来的东西不该以空框的形态等着人手写。

        底稿由 scanner 随分段一起下发（纯函数·零成本），前端预填并标「底稿·未存」
        ——**只填框不落盘**，写进文档仍然只有存稿这一条路。"""
        card = self._src().split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("g.script || g.draft", card, "空段要用引擎底稿预填")
        self.assertIn("底稿·未存", card, "预填必须标身份，不能假装已经写好了")

        import inspect

        from kinema.studio import scanner
        src = inspect.getsource(scanner._audio_script_view)
        self.assertIn("draft_segment", src)
        self.assertIn("if not str(script).strip()", src, "写过的段不许被底稿盖掉")

    def test_only_generation_locks_the_boxes_not_drafting(self):
        """锁框只在**整轨生成期间**，起草那一步刻意不锁。

        起草是毫秒级本地计算，且它的全部意义就是给你一个底稿去改——
        锁到存稿之后才能改，等于「先存一份没改过的稿才能开始改」。"""
        card = self._src().split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        lock = card.split("if (busy) {", 1)[1].split("}", 1)[0]
        self.assertIn("readOnly = true", lock)
        draft = card.split("const draftAll", 1)[1].split("};", 1)[0]
        self.assertNotIn("readOnly", draft, "起草不许锁框")

    def test_dubbed_chapters_cannot_be_switched_onto_the_scored_route(self):
        """scored × dubbed 互斥要在**表态那一刻**说，而不是等 gen-video 拒发。

        引擎硬闸在 `cli.stage_gen_video`（dry-run 同拦）：对口型人声由逐镜 TTS 的
        ref_audio 驱动，而 scored 的人声出自音频模型整轨，合成时片段音轨会被整轨
        整个替换——口型与观众听到的人声不是同一份，两道钱都白花。切过去再撞墙，
        中间还隔着写剧本与点生成两步。

        **只拦 tracks→scored 这一侧**：盘上已是 scored 的 dubbed 章节要能切得回来。
        """
        card = self._src().split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('const dubLock = d.motion === "dubbed"', card,
                      "判据与引擎硬闸同一条：互斥的是 dubbed，不是「纯 ffmpeg」")
        toggle = card.split("const toggle = async", 1)[1].split("\n  };", 1)[0]
        self.assertIn("if (dubLock && !scored)", toggle,
                      "只拦切过去那一侧，切回三轨混音必须放行")
        self.assertIn("/api/score/save", toggle)
        self.assertLess(toggle.index("if (dubLock && !scored)"),
                        toggle.index("/api/score/save"),
                        "拦截必须先于写盘请求")
        # 软禁用而非摘掉 onclick：点不动又不说话的瓦片比拦一下更难懂（同 goBtn 口径）
        self.assertIn("dubbed 下不可切", card)

    def test_scored_route_is_available_to_kenburns(self):
        """kenburns 不在互斥名单里——纯 ffmpeg 合成的章节恰恰是整轨最典型的搭配。

        引擎侧 `Project.needs_tts` 就是这么写的（`not scored_audio and motion in
        (kenburns, dubbed)`）：scored 关掉的是逐镜 TTS，而不是 kenburns 这一档。
        前端不许另立「不调用视频模型就不配整轨」的口径。
        """
        import pathlib

        from kinema.project import Project
        p = Project(pathlib.Path("x.json"),
                    {"motion": "kenburns", "audio_mode": "scored"})
        self.assertTrue(p.scored_audio, "kenburns × scored 是合法组合")
        self.assertFalse(p.needs_tts, "整轨接管人声，逐镜 TTS 让位")

        card = self._src().split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        self.assertNotIn('"kenburns"', card, "kenburns 不是互斥项，判据里不该有它")
        self.assertNotIn("uses_video", card, "音频剧本台不许挂在视频判据上")


class TestSegmentVersionStack(unittest.TestCase):
    """段谱系：生成式模型每次演绎都不同，同一段连出几版挑一版是正常用法。"""

    def test_switch_is_a_swap_so_nothing_is_ever_lost(self):
        import inspect

        from kinema.pipeline import versioning
        src = inspect.getsource(versioning.rollback_score_segment)
        self.assertIn("rollback_asset", src, "复用全仓库同一套版本栈原语")
        self.assertIn("互换", src)

    def test_archive_happens_before_regeneration_not_after(self):
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_score)
        self.assertLess(src.index("archive_score_segment"), src.index("parallel.run"),
                        "归档必须排在生成之前——生成写的是同一个路径，"
                        "等它跑完再归档，归进去的已经是新的那版")

    def test_switching_reconcats_the_whole_track(self):
        import inspect

        from kinema import cli
        from kinema.studio import actions
        self.assertIn("score_reconcat", inspect.getsource(cli._score_switch))
        # 网页与 CLI 走同一条重拼路径：段换了而整轨没重拼，盘上那条还是旧的
        # 而页面已显示切过去了，是最难查的一类「改了没生效」
        self.assertIn("score_reconcat", inspect.getsource(actions.switch_score_segment))


class TestPlanIsTheOnlySourceOfSegmentation(unittest.TestCase):
    """分段只有一条真源——CLI 计划表、Studio 面板、报价都从 plan 取。"""

    def test_scanner_does_not_reimplement_the_narration_track_question(self):
        """页面的「配音 n/总」不许自己按 motion 判——scored 一句都不配。

        抄一份的下场：一条走音频剧本的章节，阶段条上永远挂着「配音 0/9 待办」，
        成本看板也把这 9 项算成缺口，而这条路根本不跑 tts。"""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "kinema/studio/scanner.py").read_text(encoding="utf-8")
        self.assertNotIn('motion in ("kenburns", "dubbed")', src,
                         "scanner 里出现判据的抄本——真源只有 Project 那个属性一处")
        self.assertIn("def _needs_narration_track(", src)
        self.assertIn("Project(Path(\".\"), data).needs_narration_track", src)

    def test_scanner_narration_track_follows_audio_mode(self):
        from kinema.studio import scanner
        base = {"motion": "kenburns", "shots": [_shot(1, 5)]}
        self.assertTrue(scanner._needs_narration_track(base))
        self.assertFalse(scanner._needs_narration_track({**base, "audio_mode": "scored"}))

    def test_scanner_counts_only_the_shots_that_reach_the_narration_track(self):
        """native 混烧的对白镜由模型原生发声、永远没有逐镜 wav——把它算进分母
        就是一条永远做不完的待办；而旁白镜必须算进去，那是合成期会硬拦的工序。"""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "kinema/studio/scanner.py").read_text(encoding="utf-8")
        self.assertIn("voicecast.narration_shot(s, motion)", src,
                      "配音分母不认「这一镜进不进旁白轨」，混烧章就会挂永远做不完的待办")
        self.assertNotIn("voicecast.shot_text(s) and voicecast.in_narration_track", src,
                         "镜级判据只有 voicecast.narration_shot 一处，scanner 不许再拼一份")

    def test_progress_denominators_exclude_omitted_shots(self):
        """弃用镜不参与 run/assemble 的任何产物要求，阶段条分母同样不计它。"""
        import tempfile
        from kinema.studio import scanner
        from kinema.workspace import Workspace
        from tests.support import LocalBackendEnv
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace.open(str(pathlib.Path(d) / "ws"))
            s = ws.create_project("弃用", pid="omt")
            cf = s.create_chapter("第一章", cid="ch01")
            data = json.loads(cf.read_text(encoding="utf-8"))
            data["motion"] = "dubbed"
            data["shots"] = [_shot(1, 4), _shot(2, 4), _shot(3, 4)]
            data["shots"][2]["review"] = {"shot": {"state": "omt"}}
            cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            st = scanner.chapter_detail(ws.root, ws.store, "omt", "ch01")["stages"]
        self.assertEqual((st["image_total"], st["audio_total"], st["clips_total"]), (2, 2, 2))

    def test_cli_and_scanner_do_not_reimplement_segmentation(self):
        import ast
        import pathlib
        for rel in ("kinema/cli.py", "kinema/studio/scanner.py"):
            src = pathlib.Path(__file__).resolve().parents[1] / rel
            tree = ast.parse(src.read_text(encoding="utf-8"))
            names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            # 两处都必须调 audioscript.plan；谁按 dur 自己累加切一遍，
            # 切点就会与真跑分叉——用户照页面写好时间控制，真跑却切在别处
            self.assertIn("plan", names, f"{rel} 未见 audioscript.plan 调用")
            self.assertIn("audioscript", src.read_text(encoding="utf-8"), rel)


class TestScoredDubbedGate(unittest.TestCase):
    """gen-video 入口的 scored × dubbed 硬闸（dry-run 同拦）。

    没有这道闸，用户会先撞「缺配音(dubbed 需先 tts)」——在 scored 下照做等于
    花两道白钱：TTS 不参与合成、对口型音轨又会被整轨替换。"""

    def test_gen_video_rejects_dubbed_under_scored(self):
        from kinema.cli import stage_gen_video
        from kinema.errors import ProjectError
        project = Project("gate.json", {"audio_mode": "scored", "motion": "dubbed",
                                        "shots": [{"id": 1, "dur": 4}]})
        # 闸在任何 store/router 访问之前触发，None 占位即可（不发任何请求）
        with self.assertRaises(ProjectError) as ctx:
            stage_gen_video(project, None, None, dry_run=True)
        msg = str(ctx.exception)
        self.assertIn("scored", msg)
        self.assertIn("native", msg, "必须给出生视频的正路")
        self.assertIn("tracks", msg, "必须给出对口型的正路")


if __name__ == "__main__":
    unittest.main()


class TestAnchorPlanDefaultOnlyForNarrator(unittest.TestCase):
    """缺省音（旁白锁 / profile）只属于旁白：未选角的角色落 loose 由告警点名，
    不借旁白的声音出演；旁白别名（VO/narrator）与空 speaker 同归旁白。"""

    def test_uncast_character_is_loose_and_aliases_fold_to_narrator(self):
        p = _proj([{"id": 1, "dur": 6.0, "lines": [
            {"speaker": "VO", "text": "夜深了。"},
            {"speaker": "阿箬", "text": "你来了。"}]}],
            voice_bank={"seq": 1, "casts": [
                {"id": "vc_0001", "owner": "旁白", "mode": "custom",
                 "voice_type": "custom:vc_0001", "prompt": "低沉", "clip": __file__}]})
        seg = audioscript.plan(p)[0]
        plan = audioscript.anchor_plan(p, _StoreStub(), seg, default_ref="custom:vc_0001")
        self.assertEqual([a["who"] for a in plan["anchored"]], [audioscript.NARRATOR_NAME])
        self.assertEqual(plan["loose"], ["阿箬"])
        drafted = audioscript.draft_segment(p, seg)[0]
        self.assertIn("旁白说道", drafted)
        self.assertNotIn("VO说道", drafted)
