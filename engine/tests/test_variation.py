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

"""分镜单调度 lint+ art_direction 旋钮守卫。

全纯函数、零 ffmpeg、零工作区——`variation.lint` 只吃一个 dict、只吐 Finding 列表。

守死三件事：
1. **软闸永不抛异常**（TestGateIsSoft）——它挂在 `stage_gen_image` 的花钱主链上，
   空 shots / 全 omt / 全转场 / 字段类型写坏都必须返回列表而不是炸掉；
   输入一律 `data.get("shots") or []`，绝不碰 `Project.shots`/`active_shots`
   （那两个在无分镜与全 omt 时抛 ProjectError）。
2. **跳过判据复用单一真源** `transitions.is_transition` / `review.is_omitted`。
3. **旋钮真的驱动阈值**——同一份分镜单换 art_direction 必须换结论，
   否则 M12 就落成了无人消费的死字段。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from kinema.pipeline import variation as vr


def _shot(no: int, **kw) -> dict:
    s = {"id": no, "dur": 4.0, "narration": f"第{no}句台词，说点什么。",
         "image_prompt": f"镜{no}的画面"}
    s.update(kw)
    return s


def _codes(findings) -> set:
    return {f.code for f in findings}


def _by_code(findings, code):
    return [f for f in findings if f.code == code]


class TestGateIsSoft(unittest.TestCase):
    """软闸底线：任何畸形输入都不许抛异常（打断的是花钱主链）。"""

    def test_empty_shots_returns_empty(self):
        for data in ({}, {"shots": []}, {"shots": None}):
            self.assertEqual(vr.lint(data), [], f"输入 {data} 应返回空列表")

    def test_all_omitted_returns_empty(self):
        data = {"shots": [{"id": i, "narration": "唯美的画面",
                           "image_prompt": "唯美",
                           "review": {"shot": {"state": "omt"}}} for i in (1, 2, 3)]}
        self.assertEqual(vr.lint(data), [], "全 omt 应视同无分镜，不出任何结论")

    def test_all_transitions_returns_empty(self):
        data = {"shots": [{"id": i, "kind": "transition", "dur": 1.6,
                           "transition": {"type": "fade_black"}} for i in (1, 2)]}
        self.assertEqual(vr.lint(data), [])

    def test_garbage_input_does_not_raise(self):
        for data in (None, [], "x", 42,
                     {"shots": "not-a-list"},
                     {"shots": [{"id": 1, "kind": 3}]},
                     {"shots": [None, 1, "x", {"id": 1, "dur": "慢", "camera": 3.5,
                                               "framing": ["中景"], "emotion": {},
                                               "narration": 7}]},
                     {"shots": [_shot(1)], "art_direction": "满血"},
                     {"shots": [_shot(1)], "art_direction": {"variety": "高", "avoid": "光"}}):
            self.assertIsInstance(vr.lint(data), list, f"输入 {data!r} 不该抛异常")

    def test_omitted_and_transition_use_single_source(self):
        # 跳过判据必须与 review.is_omitted / transitions.is_transition 一致
        from kinema import review
        from kinema.pipeline import transitions
        omt = {"id": 1, "review": {"shot": {"state": "omt"}}}
        tr = {"id": 2, "kind": "transition"}
        keep = _shot(3)
        self.assertTrue(review.is_omitted(omt))
        self.assertTrue(transitions.is_transition(tr))
        got = vr.active_shots({"shots": [omt, tr, keep]})
        self.assertEqual([s["id"] for s in got], [3])


class TestNormalizers(unittest.TestCase):
    def test_camera_normalized_by_technique_name(self):
        # 真实数据两种写法（「缓慢推近」与「缓慢推近：镜头缓缓平稳推近至主体…」）是同一运镜
        self.assertEqual(vr.normalize_camera("缓慢推近：镜头缓缓平稳推近至主体，节奏克制"),
                         vr.normalize_camera("缓慢推近"))
        self.assertEqual(vr.normalize_camera(" Dolly In "), "dollyin")
        self.assertEqual(vr.normalize_camera(None), "")

    def test_framing_buckets_cover_real_values(self):
        # 真实工程里常见的 7 种写法必须全部归得了桶
        real = {"中景": "medium", "近景": "close", "全景": "wide",
                "中近景": "medium", "特写": "close", "双人中景": "medium",
                "过肩": "view"}
        for raw, bucket in real.items():
            self.assertEqual(vr.framing_bucket(raw), bucket, f"{raw} 应归 {bucket}")

    def test_framing_size_bucket_wins_over_view(self):
        # 「双人中景」既含「双人」(view) 又含「中景」(medium)：尺寸桶优先
        self.assertEqual(vr.framing_bucket("双人中景"), "medium")

    def test_framing_english_codes_exact_only(self):
        self.assertEqual(vr.framing_bucket("MCU"), "medium")
        self.assertEqual(vr.framing_bucket("ECU"), "close")
        self.assertEqual(vr.framing_bucket("2S"), "view")
        self.assertIsNone(vr.framing_bucket("focus pull"), "英文只认精确匹配，不做包含")

    def test_unknown_framing_only_informs(self):
        shots = [_shot(i, framing="俯瞰奇观镜", camera=f"运镜{i}") for i in range(1, 5)]
        got = vr.lint({"shots": shots})
        unknown = _by_code(got, "framing_unknown")
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].level, "info", "归不了的景别只提示、不判错")
        self.assertNotIn("framing_flat", _codes(got))


class TestSlopTerms(unittest.TestCase):
    def test_table_shape(self):
        self.assertGreaterEqual(len(vr.SLOP_TERMS), 20)
        self.assertLessEqual(len(vr.SLOP_TERMS), 30)
        for term, hint in vr.SLOP_TERMS.items():
            self.assertTrue(term.strip() and hint.strip(), f"{term} 词条不完整")
            self.assertGreaterEqual(len(hint), 8, f"{term} 的改写建议太短，等于没给")

    def test_slop_hit_carries_rewrite_hint(self):
        shots = [_shot(1, image_prompt="唯美的清晨，氛围感拉满"), _shot(2)]
        got = _by_code(vr.lint({"shots": shots}), "slop_term")
        self.assertEqual({f.message.split("「")[1].split("」")[0] for f in got},
                         {"唯美", "氛围感"})
        for f in got:
            self.assertEqual(f.level, "warn")
            self.assertEqual(f.hint, vr.SLOP_TERMS[f.message.split("「")[1].split("」")[0]])
            self.assertEqual(f.shots, (1,))

    def test_narration_is_not_scanned_for_slop(self):
        # 旁白里的「唯美」是台词，不是提示词空词——只扫 image_prompt/video_prompt
        shots = [_shot(1, narration="她说这画面真唯美。", image_prompt="女孩站在窗边")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "slop_term"), [])

    def test_video_prompt_also_scanned(self):
        shots = [_shot(1, image_prompt="女孩站在窗边", video_prompt="镜头推近，电影感")]
        self.assertEqual(len(_by_code(vr.lint({"shots": shots}), "slop_term")), 1)


class TestDimensions(unittest.TestCase):
    def test_adjacent_camera_repeat_warns(self):
        shots = [_shot(i, camera="缓慢推近", framing="中景" if i % 2 else "近景")
                 for i in range(1, 6)]
        got = _by_code(vr.lint({"shots": shots}), "camera_repeat")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")

    def test_distinct_cameras_pass(self):
        shots = [_shot(i, camera=f"运镜{i}") for i in range(1, 6)]
        self.assertNotIn("camera_repeat", _codes(vr.lint({"shots": shots})))

    def test_omitted_shot_does_not_bridge_camera_repeat(self):
        # 中间那镜弃用后，1 与 3 在成片里变成相邻——雷同必须被抓出来
        shots = [_shot(1, camera="缓慢推近"),
                 _shot(2, camera="左移", review={"shot": {"state": "omt"}}),
                 _shot(3, camera="缓慢推近")]
        got = _by_code(vr.lint({"shots": shots}, art_direction={"variety": 10}),
                       "camera_repeat")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].shots, (1, 3))

    def test_transition_shot_breaks_nothing(self):
        # 转场镜不参与统计（零成本本地渲染，没有运镜可言）
        shots = [_shot(1, camera="缓慢推近"),
                 {"id": 2, "kind": "transition", "dur": 1.6, "narration": ""},
                 _shot(3, camera="缓慢推近")]
        got = _by_code(vr.lint({"shots": shots}, art_direction={"variety": 10}),
                       "camera_repeat")
        self.assertEqual(got[0].shots, (1, 3))

    def test_emotion_all_missing_warns(self):
        shots = [_shot(i, camera=f"运镜{i}") for i in range(1, 5)]
        got = _by_code(vr.lint({"shots": shots}), "emotion_missing")
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1, 2, 3, 4))

    def test_emotion_partial_missing_informs(self):
        shots = [_shot(i, camera=f"运镜{i}", emotion=("sad" if i < 4 else None))
                 for i in range(1, 5)]
        got = _by_code(vr.lint({"shots": shots}), "emotion_missing")
        self.assertEqual(got[0].level, "info")
        self.assertEqual(got[0].shots, (4,))

    def test_emotion_monotone_warns_at_high_variety(self):
        shots = [_shot(i, camera=f"运镜{i}", emotion="sad") for i in range(1, 5)]
        self.assertNotIn("emotion_monotone", _codes(vr.lint({"shots": shots})))  # variety=5 放行
        got = vr.lint({"shots": shots}, art_direction={"variety": 9})
        self.assertIn("emotion_monotone", _codes(got))

    def test_emotion_kinds_counted_per_line_not_none(self):
        """单调判据与 _has_emotion 同源逐句取（shot_lines 已做镜级→句级继承）：
        全靠 lines[] 标情绪的章节不得被折成 str(None)="none" 判「只有 1 种」；
        混写时 "none" 也不得凑成一种真情绪把真单调放过。"""
        def lines(*emos):
            return [{"speaker": "甲", "text": f"第{k}句", "emotion": e}
                    for k, e in enumerate(emos)]
        rich = [_shot(1, camera="运镜1", lines=lines("sad", "angry")),
                _shot(2, camera="运镜2", lines=lines("calm", "excited")),
                _shot(3, camera="运镜3", lines=lines("fear", "warm")),
                _shot(4, camera="运镜4", lines=lines("sad", "hopeful"))]
        got = vr.lint({"motion": "kenburns", "shots": rich}, art_direction={"variety": 9})
        self.assertNotIn("emotion_monotone", _codes(got),
                         "逐句 6 种情绪的章节不得被判单调")
        mono = [_shot(1, camera="运镜1", lines=lines("sad", "sad")),
                _shot(2, camera="运镜2", lines=lines("sad")),
                _shot(3, camera="运镜3", lines=lines("sad")),
                _shot(4, camera="运镜4", emotion="sad")]
        got2 = vr.lint({"motion": "kenburns", "shots": mono}, art_direction={"variety": 9})
        self.assertIn("emotion_monotone", _codes(got2),
                      "全 sad 的真单调必须报——镜级 None 不得折成第二种情绪")

    def test_skip_design_downgrades_emotion_noise(self):
        """纯旁白解说档（skip_design：kn-quote/kn-ranking/kn-showcase 的既定工作流）
        一把口播音色贯穿是常态——整体缺 emotion 只 info 不 warn，单一情绪不算单调。
        判据只认显式 skip_design：剧情片在设定单节点前 characters 可能还没登记，
        按「角色表为空」推断会把真该催的 emotion 一起压掉。"""
        shots = [_shot(i, camera=f"运镜{i}") for i in range(1, 5)]
        got = _by_code(vr.lint({"shots": shots, "skip_design": True}), "emotion_missing")
        self.assertEqual(got[0].level, "info", "skip_design 下整体缺 emotion 降 info")
        self.assertEqual(got[0].shots, (1, 2, 3, 4))
        mono = [_shot(i, camera=f"运镜{i}", emotion="calm") for i in range(1, 5)]
        self.assertNotIn("emotion_monotone",
                         _codes(vr.lint({"shots": mono, "skip_design": True},
                                        art_direction={"variety": 9})),
                         "解说腔从头到尾是合法选择，skip_design 下不判单调")
        # 对照组：没设 skip_design（哪怕 characters 为空）行为一字不变
        self.assertEqual(_by_code(vr.lint({"shots": shots}),
                                  "emotion_missing")[0].level, "warn")

    def test_framing_flat_warns(self):
        shots = [_shot(i, camera=f"运镜{i}", framing="中景") for i in range(1, 5)]
        got = _by_code(vr.lint({"shots": shots}), "framing_flat")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")

    def test_framing_varied_passes(self):
        sizes = ["远景", "中景", "近景", "特写"]
        shots = [_shot(i, camera=f"运镜{i}", framing=sizes[i - 1]) for i in range(1, 5)]
        self.assertNotIn("framing_flat", _codes(vr.lint({"shots": shots})))

    def test_placeholder_narration_warns(self):
        shots = [_shot(1, narration="TODO 这里补一句钩子"), _shot(2, narration="待定")]
        got = _by_code(vr.lint({"shots": shots}), "narration_placeholder")
        self.assertEqual(got[0].shots, (1, 2))

    def test_caption_scanned_when_no_narration(self):
        # 无旁白镜扫 caption（同 subtitle.pick_texts 语义）
        shots = [_shot(1, narration="", caption="占位文案"), _shot(2)]
        self.assertIn("narration_placeholder", _codes(vr.lint({"shots": shots})))

    def test_pure_visual_shot_is_legal(self):
        # 空 narration + 空 caption 是合法「纯画面镜」（引擎自动插等长静音），不许报
        shots = [_shot(1, narration="", caption=""), _shot(2)]
        got = _codes(vr.lint({"shots": shots}))
        self.assertNotIn("narration_placeholder", got)
        self.assertNotIn("narration_duplicate", got)

    def test_duplicate_narration_warns(self):
        shots = [_shot(1, narration="他推开了门。"), _shot(2, narration="他推开了门。")]
        got = _by_code(vr.lint({"shots": shots}), "narration_duplicate")
        self.assertEqual(got[0].shots, (1, 2))

    def test_multi_sentence_narration_warns_to_split_into_lines(self):
        # 单段镜里出现第二句 = 一条 Dialogue 横跨整镜，后几句提前剧透
        shots = [_shot(1, narration="少年抬头。遥远宇宙深处，某种存在正缓缓转过头来。"),
                 _shot(2, narration="可迎接他的，只有荒废了十万年的仙界。")]
        got = _by_code(vr.lint({"shots": shots}), "subtitle_dump")
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1,))

    def test_ellipsis_and_closing_quote_do_not_split_a_sentence(self):
        # 省略号是句内停顿；句末标点后的收尾引号并入前句
        shots = [_shot(1, narration="某种沉睡了十万年的存在……正缓缓转过头来。"),
                 _shot(2, narration="「不要继续飞升。」")]
        self.assertNotIn("subtitle_dump", _codes(vr.lint({"shots": shots})))

    def test_shot_split_into_lines_is_exempt(self):
        # 写了 lines[] 的镜由 subtitle.shot_events 逐句成条，本维度不再管
        shots = [_shot(1, narration=""), _shot(2)]
        shots[0]["lines"] = [{"speaker": "旁白", "text": "少年抬头。"},
                             {"speaker": "旁白", "text": "正缓缓转过头来。"}]
        self.assertNotIn("subtitle_dump", _codes(vr.lint({"shots": shots})))

    def test_hero_moment_absent_informs_and_inflation_warns(self):
        shots = [_shot(i, camera=f"运镜{i}") for i in range(1, 5)]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "hero_absent")[0].level, "info")
        for s in shots:
            s["hero_moment"] = True
        got = _by_code(vr.lint({"shots": shots}), "hero_inflation")
        self.assertEqual(got[0].level, "warn")

    def test_hero_moment_consumed_by_lint_only(self):
        # 负向（不许渗进花钱/渲染主链）由 test_shot_meta.TestEngineDoesNotConsume 守；
        # 这里守正向：M2 lint 是 hero_moment 唯一的行为消费方；Agent Gateway 只把
        # 作者字段放进最小上下文，不据此改变渲染、提示词或成本。
        eng = Path(__file__).resolve().parents[1] / "kinema"
        hits = {p.relative_to(eng).as_posix() for p in eng.rglob("*.py")
                if "hero_moment" in p.read_text(encoding="utf-8")}
        # review.py 只是把它登记为「不使产物过期」的空元组（契约白名单全量登记）
        self.assertEqual(hits, {"agent_gateway.py", "pipeline/variation.py",
                                "studio/scanner.py", "review.py"},
                         f"hero_moment 的消费面变了: {sorted(hits)}")


class TestMultishotSyntax(unittest.TestCase):
    """一段 video_prompt 切多镜的反模式（M17 唯一残值·预防性纪律·恒 warn 不拦死）。"""

    def test_english_multishot_syntax_warns(self):
        shots = [_shot(1, camera="推近"),
                 _shot(2, camera="拉远",
                       video_prompt="Shot 1: 她转身；Shot 2: 特写手部")]
        got = _by_code(vr.lint({"shots": shots}), "multishot_syntax")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")          # 告警不拦死
        self.assertEqual(got[0].shots, (2,))
        self.assertIn("拆", got[0].hint)

    def test_chinese_multishot_syntax_warns(self):
        shots = [_shot(1, camera="推近", video_prompt="镜头一：她转身。镜头二：手部特写。")]
        got = _by_code(vr.lint({"shots": shots}), "multishot_syntax")
        self.assertEqual(got[0].shots, (1,))

    def test_single_shot_video_prompt_passes(self):
        shots = [_shot(1, camera="推近", video_prompt="她缓缓转身，衣角被风掀起，发丝滞后半拍"),
                 _shot(2, camera="拉远", video_prompt="")]
        self.assertNotIn("multishot_syntax", _codes(vr.lint({"shots": shots})))

    def test_only_video_prompt_is_scanned(self):
        # image_prompt 是静图，写「Shot 2」不构成多镜切分问题——扫描面只有 video_prompt
        shots = [_shot(1, camera="推近", image_prompt="Shot 2 的构图参考")]
        self.assertNotIn("multishot_syntax", _codes(vr.lint({"shots": shots})))


class TestPromptEcho(unittest.TestCase):
    """复述重合率（M6·d）：video_prompt 抄 image_prompt = 增量编译铁律的反面。

    引擎侧不做「缺 video_prompt 就整条回退 image_prompt」的回退，但作者手写
    复述拦不住——只能量化告警。阈值是保守值，须用真实项目标定（见 variation.py 注释）。"""

    IP = "白发老者立于崖边，青衫猎猎，远山云海翻涌，逆光勾出发丝边缘，中景略仰"

    def _lint(self, vp, motion="native"):
        return _by_code(vr.lint({"motion": motion,
                                 "shots": [_shot(1, camera="推近",
                                                 image_prompt=self.IP, video_prompt=vp)]}),
                        "prompt_echo")

    def test_video_prompt_echoes_image_prompt_warns(self):
        got = self._lint(self.IP + "，镜头缓缓推近")      # 整条粘贴 + 补一句运镜
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1,))
        self.assertIn("增量", got[0].hint)

    def test_pure_motion_delta_passes(self):
        # 只写增量（动作/终态/光线/运镜）——一个字都不复述外貌与场景
        self.assertEqual(self._lint("他缓缓抬手按住剑柄，衣袖被风灌满，镜头缓缓推近"), [])

    def test_short_video_prompt_not_judged(self):
        # 太短的整句都是运镜词，重合无意义（ECHO_MIN_CHARS 地板）
        self.assertEqual(self._lint("云海翻涌"), [])

    def test_kenburns_never_judged(self):
        # kenburns 根本不读 video_prompt，催了也没用（同 emotion 的模式门控口径）
        self.assertEqual(self._lint(self.IP, motion="kenburns"), [])

    def test_echo_ratio_is_pure_and_punctuation_insensitive(self):
        self.assertEqual(vr.echo_ratio("", "随便什么"), 0.0)
        self.assertEqual(vr.echo_ratio("短", "短"), 0.0)          # 不足一个 shingle
        a = "他缓缓抬手按住剑柄，衣袖被风灌满"
        self.assertEqual(vr.echo_ratio(a, a), 1.0)
        self.assertEqual(vr.echo_ratio(a, a.replace("，", "。")), 1.0)   # 标点不算差异

    def test_well_written_shot_has_no_echo(self):
        """按现行纪律写的镜零命中——阈值标定的正样本（口径变化时这里最先红）。

        样本**内联**而不是去扫 `project/`：那是 gitignored 的用户数据
        （AGENTS.md 明列绝不提交），拿它当断言源有两个真故障——
        ① 别人的机器/CI 上目录为空，标定形同虚设；
        ② 用户日后在某个 kenburns 章节里把 image_prompt 顺手粘进 video_prompt
        （该模式下引擎根本不读 video_prompt，完全无害），整条验收闸却会变红，
        红的还是"阈值标定"而非任何代码回归。
        下面这条取自真实章节的写法（运动/机位/光影，零主体复述）。"""
        shot = _shot(1,
                     image_prompt="银发少年立于雨中青石板巷口，手执红伞，远处灯笼晕开暖光",
                     video_prompt="镜头缓慢推近，雨丝加密，伞面水珠沿边缘连成线滑落，"
                                  "灯笼光晕随雨幕轻微摇曳")
        self.assertEqual(_by_code(vr.lint({"shots": [shot], "motion": "native"}),
                                  "prompt_echo"), [])

    def test_pasted_image_prompt_is_caught(self):
        """反向样本：整条粘贴必须被抓住，否则阈值等于没设。"""
        body = "银发少年立于雨中青石板巷口，手执红伞，远处灯笼晕开暖光，青苔爬满墙根"
        shot = _shot(1, image_prompt=body, video_prompt=body)
        got = _by_code(vr.lint({"shots": [shot], "motion": "native"}), "prompt_echo")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")


class TestArtDirection(unittest.TestCase):
    def test_missing_block_falls_back_to_defaults(self):
        self.assertEqual(vr.resolve_art_direction({}), vr.ART_DIRECTION_DEFAULTS)
        self.assertEqual(vr.resolve_art_direction({"art_direction": None}),
                         vr.ART_DIRECTION_DEFAULTS)
        # 写坏/越界只取合法位，其余回落缺省
        got = vr.resolve_art_direction({"art_direction": {"variety": 99, "motion": 0,
                                                          "density": "x", "avoid": None}})
        self.assertEqual(got, {"variety": 10, "motion": 1, "density": 5, "avoid": []})

    def test_explicit_override_beats_document(self):
        data = {"art_direction": {"variety": 1}}
        self.assertEqual(vr.resolve_art_direction(data)["variety"], 1)
        self.assertEqual(vr.resolve_art_direction(data, {"variety": 9})["variety"], 9)

    def test_art_direction_thresholds_drive_lint(self):
        # 同一份分镜单：两处相邻雷同 —— variety=5 允许 2 处（放行）、variety=9 只允许 0 处（告警）
        shots = [_shot(1, camera="推近"), _shot(2, camera="推近"),
                 _shot(3, camera="拉远"), _shot(4, camera="拉远")]
        loose = vr.lint({"shots": shots}, art_direction={"variety": 5})
        tight = vr.lint({"shots": shots}, art_direction={"variety": 9})
        self.assertNotIn("camera_repeat", _codes(loose))
        self.assertIn("camera_repeat", _codes(tight))

    def test_document_block_drives_lint_without_override(self):
        shots = [_shot(1, camera="推近"), _shot(2, camera="推近")]
        self.assertIn("camera_repeat",
                      _codes(vr.lint({"shots": shots, "art_direction": {"variety": 10}})))
        self.assertNotIn("camera_repeat",
                         _codes(vr.lint({"shots": shots, "art_direction": {"variety": 1}})))

    def test_motion_knob_drives_camera_coverage(self):
        shots = [_shot(1, camera="推近"), _shot(2), _shot(3), _shot(4)]   # 1/4 有运镜
        self.assertNotIn("camera_missing",
                         _codes(vr.lint({"shots": shots}, art_direction={"motion": 2})))
        self.assertIn("camera_missing",
                      _codes(vr.lint({"shots": shots}, art_direction={"motion": 8})))

    def test_motion_knob_never_touches_kenburns(self):
        """「只改告警、永不改画面」：**渲染与花钱主链**一行都不许读 art_direction。

        白名单三处各有其职，任何第四处出现都要先问「它会改画面/改成本吗」：
          · pipeline/variation.py —— 旋钮真源与全部映射函数
          · cli.py               —— `lint` 子命令与生图前软闸
          · workspace.py         —— **仅**建章时把系列旋钮拷进章节文档
                                    （与 style_prompt 同待遇；lint 只读章节层，
                                     不拷则系列级设定对新章静默失效）
        注意 compose/kenburns/prompts/providers 必须始终缺席——它们一旦读旋钮，
        「只改告警」的承诺就破了。"""
        eng = Path(__file__).resolve().parents[1] / "kinema"
        hits = sorted(p.relative_to(eng).as_posix() for p in eng.rglob("*.py")
                      if "art_direction" in p.read_text(encoding="utf-8"))
        self.assertEqual(hits, ["cli.py", "pipeline/variation.py", "workspace.py"],
                         f"art_direction 只该被 lint/CLI/Studio/建章继承读取，实际 {hits}")

    def test_density_knob_drives_speech_rate_band(self):
        lines = ("一二三四五六七八九十", "甲乙丙丁戊己庚辛壬癸")
        shots = [_shot(i, camera=f"运镜{i}", narration=lines[i - 1], dur=2.5)
                 for i in range(1, 3)]                     # 10 字 / 2.5 秒 = 4.0 字/秒
        self.assertNotIn("pace_dense", _codes(vr.lint({"shots": shots})))
        self.assertIn("pace_dense",
                      _codes(vr.lint({"shots": shots}, art_direction={"density": 1})))
        self.assertIn("pace_sparse",
                      _codes(vr.lint({"shots": shots}, art_direction={"density": 10})))

    def test_avoid_list_hits_like_slop(self):
        shots = [_shot(1, image_prompt="漫天樱花飘落")]
        got = vr.lint({"shots": shots}, art_direction={"avoid": ["樱花"]})
        hit = _by_code(got, "avoid_term")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].level, "warn")
        self.assertEqual(hit[0].shots, (1,))


class TestFindingShape(unittest.TestCase):
    def test_findings_are_json_serializable(self):
        shots = [_shot(i, camera="推近", framing="中景") for i in range(1, 5)]
        got = vr.lint({"shots": shots})
        self.assertTrue(got)
        payload = {"summary": vr.summarize(got), "findings": [f.to_dict() for f in got]}
        json.loads(json.dumps(payload))          # Studio 条幅要能直接下发
        self.assertEqual(payload["summary"]["total"], len(got))
        self.assertEqual(payload["summary"]["warn"] + payload["summary"]["info"], len(got))

    def test_warnings_sort_before_infos(self):
        shots = [_shot(i, camera="推近", framing="中景") for i in range(1, 5)]
        levels = [f.level for f in vr.lint({"shots": shots})]
        self.assertEqual(levels, sorted(levels, key=lambda x: x != "warn"))

    def test_lint_does_not_mutate_input(self):
        data = {"shots": [_shot(1, camera="推近"), _shot(2, camera="推近")]}
        before = json.dumps(data, sort_keys=True, ensure_ascii=False)
        vr.lint(data)
        self.assertEqual(json.dumps(data, sort_keys=True, ensure_ascii=False), before)


class TestCliWiring(unittest.TestCase):
    """软闸接线：必须在 --only 过滤之前、且 lint 结论不落盘。"""

    def test_gate_runs_before_only_filter(self):
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_image)
        self.assertLess(src.index("_lint_gate("), src.index("if only:"),
                        "软闸必须在 --only 过滤之前——只扫 1 镜会让相邻雷同/分布维度失真")

    def test_gate_propagates_engine_errors(self):
        """lint 是离线纯函数，维度抛错只能是引擎自己的 bug：软闸吞掉它，告警会静默消失而测试照绿。"""
        from kinema import cli

        class _Boom:
            @property
            def data(self):
                raise RuntimeError("文档读炸了")

        with self.assertRaises(RuntimeError):
            cli._lint_gate(_Boom(), only=None)

    def test_lint_is_not_a_stage_command(self):
        import inspect
        from kinema import cli
        body = inspect.getsource(cli.cmd_lint).split('"""')[-1]   # 去掉文档串再查
        self.assertNotIn("_stage_wrapper", body,
                         "lint 不套 _stage_wrapper（那层带 router/store/checkpoint 语义）")
        self.assertNotIn("ensure_tools", body)
        parser = cli.build_parser()
        args = parser.parse_args(["lint", "--project", "x.json", "--strict"])
        self.assertIs(args.func, cli.cmd_lint)
        self.assertTrue(args.strict)

    def test_lint_writes_nothing(self):
        # lint 与 spec check 同为纯计算命令：跑完文档字节不变（结论不落盘）
        import io
        import tempfile
        from argparse import Namespace
        from contextlib import redirect_stdout
        from kinema import cli

        doc = {"chapter": {"title": "体检样章"}, "aspect": "16:9",
               "shots": [_shot(1, camera="推近"), _shot(2, camera="推近")]}
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ch01.json"
            raw = json.dumps(doc, indent=2, ensure_ascii=False)
            f.write_text(raw, encoding="utf-8")
            args = Namespace(project=str(f), chapter=None, workspace=None,
                             config=None, force=False, strict=False)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_lint(args)
            self.assertIn("分镜单 lint", buf.getvalue())
            self.assertEqual(f.read_text(encoding="utf-8"), raw, "lint 不许写回文档")
            # --strict 有警告即非零退出
            args.strict = True
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cli.cmd_lint(args)
            self.assertNotEqual(cm.exception.code, 0)

    def test_gate_prints_one_line_summary_when_only(self):
        import io
        from contextlib import redirect_stdout
        from kinema import cli

        class _Doc:
            data = {"shots": [_shot(1, camera="推近"), _shot(2, camera="推近")]}

        full, brief = io.StringIO(), io.StringIO()
        with redirect_stdout(full):
            cli._lint_gate(_Doc(), only=None)
        with redirect_stdout(brief):
            cli._lint_gate(_Doc(), only="1")
        self.assertEqual(len(brief.getvalue().strip().splitlines()), 1,
                         "--only 时降为一行汇总，不刷屏")
        self.assertGreater(len(full.getvalue().strip().splitlines()), 1)
        self.assertIn("全片口径", brief.getvalue())


class TestRenderModeGating(unittest.TestCase):
    """emotion 只在跑 TTS 的模式下才该催——native 由模型原生配音，emotion 是死字段。

    不门控的后果是真误报：native 章节每次 gen-image 都会在花钱主链上打印
    「补齐 emotion 再跑 tts」，而该模式下引擎根本不跑 stage_tts，作者照做也无效。"""

    DOC = {"shots": [{"id": i, "dur": 3, "narration": f"第{i}句台词写得够长了",
                      "camera": f"运镜{i}"} for i in range(1, 5)]}

    def _codes(self, motion):
        return {f.code for f in vr.lint({**self.DOC, "motion": motion})}

    def test_kenburns_and_dubbed_still_warn(self):
        for m in ("kenburns", "dubbed", "a", "c"):
            self.assertIn("emotion_missing", self._codes(m), f"{m} 应催 emotion")

    def test_native_does_not_warn(self):
        for m in ("native", "b"):          # 别名归一同 Project.motion
            self.assertNotIn("emotion_missing", self._codes(m), f"{m} 不该催 emotion")

    def test_undeclared_motion_follows_content(self):
        """未表态章节与引擎同一缺省判据（`project.effective_motion`）：无对白落 dubbed，
        有对白落 native——lint 的模式门与真发口径不得分叉。"""
        self.assertEqual(vr.render_mode({}), "dubbed")
        self.assertEqual(vr.render_mode(None), "dubbed")
        self.assertEqual(vr.render_mode({"shots": [
            {"id": 1, "lines": [{"speaker": "甲", "text": "走。"}]}]}), "native")
        self.assertEqual(vr.render_mode({"audio_mode": "scored"}), "native")


class TestFindingShotsAreUnique(unittest.TestCase):
    """Finding.shots 只承载「哪几镜」（次数在 message 里）——重复值会让 Studio 渲染重复 chip。"""

    def test_camera_repeat_ids_deduped(self):
        doc = {"shots": [{"id": i, "dur": 3, "narration": f"第{i}句台词写得够长了",
                          "camera": "缓慢推近"} for i in range(1, 6)]}
        for f in vr.lint(doc):
            self.assertEqual(len(f.shots), len(set(f.shots)),
                             f"{f.code} 的镜号列表有重复: {f.shots}")


class TestKnobDocMatchesImpl(unittest.TestCase):
    """schema 的 art_direction 描述必须与映射函数锁步——文档宣称的档数一旦
    与实现分叉（如说 9→2 类、实为 3 类），指挥层按文档写就会写错。"""

    def test_framing_bucket_floor_boundary(self):
        self.assertEqual(vr._framing_bucket_floor(10), 3)
        self.assertEqual(vr._framing_bucket_floor(9), 3)     # 整除三档的真实拐点
        self.assertEqual(vr._framing_bucket_floor(8), 2)
        self.assertEqual(vr._framing_bucket_floor(1), 2)

    def test_schema_variety_description_states_9(self):
        p = (Path(__file__).resolve().parents[2]
             / "docs" / "kinema" / "project.schema.json")
        if not p.is_file():
            self.skipTest("schema 不在（打包分发）")
        desc = (json.loads(p.read_text(encoding="utf-8"))["properties"]["art_direction"]
                ["properties"]["variety"]["description"])
        self.assertIn("≥9→3", desc.replace(" ", ""),
                      "schema 的景别桶口径与 _framing_bucket_floor 漂移了")


class TestCharacterCoverage(unittest.TestCase):
    """M8 的 required_* 必须有引擎消费点——否则就是第二个 `priority`（写进契约、无人读）。"""

    DOC = {"characters": [{"name": "林深", "required_emotions": ["平静", "愤怒"]}],
           "shots": [{"id": 1, "dur": 3, "narration": "第一句台词写长一点", "emotion": "平静"},
                     {"id": 2, "dur": 3, "narration": "第二句台词写长一点", "emotion": "含泪"},
                     {"id": 3, "dur": 3, "narration": "第三句台词写长一点", "emotion": "狂喜"}]}

    def test_uncovered_emotions_reported(self):
        got = _by_code(vr.lint(self.DOC), "emotion_uncovered")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "info")
        self.assertIn("含泪", got[0].message)
        self.assertIn("狂喜", got[0].message)
        self.assertNotIn("平静", got[0].message)     # 已登记的不报

    def test_fully_covered_is_silent(self):
        doc = json.loads(json.dumps(self.DOC))
        doc["characters"][0]["required_emotions"] = ["平静", "愤怒", "含泪", "狂喜"]
        self.assertEqual(_by_code(vr.lint(doc), "emotion_uncovered"), [])

    def test_character_without_required_is_skipped(self):
        """没填 required_emotions = 作者还没做这项规划，不该催（那是另一回事）。"""
        doc = json.loads(json.dumps(self.DOC))
        doc["characters"][0].pop("required_emotions")
        self.assertEqual(_by_code(vr.lint(doc), "emotion_uncovered"), [])

    def test_engine_actually_consumes_required_fields(self):
        """守死"不是死字段"：required_emotions 必须能在引擎代码里搜到读取点。"""
        eng = Path(__file__).resolve().parents[1] / "kinema"
        hits = [p.name for p in eng.rglob("*.py")
                if "required_emotions" in p.read_text(encoding="utf-8")]
        self.assertIn("variation.py", hits,
                      "required_emotions 没有任何引擎消费点——契约里的空承诺")

    def test_garbage_characters_do_not_raise(self):
        for chars in (None, "x", [None, 1, {"name": "a", "required_emotions": "不是列表"}]):
            doc = {"characters": chars, "shots": self.DOC["shots"]}
            self.assertIsInstance(vr.lint(doc), list)


class TestSceneContinuity(unittest.TestCase):
    """场景连续性维度：跳景事故的机器前哨。

    典型跳景：夜戏中间冒出白天街头、室内特写背景跑进洞窟——
    共同点都是 `shots[].scenes` 未声明且语料点不到任何注册取景地，场景全靠模型抽。"""

    REG = [{"name": "渊口", "keywords": ["检查站"]}, {"name": "画室"}]

    def _codes(self, data):
        return {f.code for f in vr.lint(data)}

    def test_unanchored_shots_warned(self):
        data = {"scenes": self.REG,
                "shots": [{"id": 1, "image_prompt": "夜风吹动马尾"},
                          {"id": 2, "image_prompt": "他望向天空"}]}
        fs = [f for f in vr.lint(data) if f.code == "scene_unanchored"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].shots, (1, 2))

    def test_explicit_empty_list_is_an_anchor(self):
        # 空镜显式 scenes=[] = 作者声明过「无取景地」——绝不算失锚（与 characters 同语义）
        data = {"scenes": self.REG,
                "shots": [{"id": 1, "scenes": [], "image_prompt": "仰拍夜空环形符文"}]}
        self.assertNotIn("scene_unanchored", self._codes(data))

    def test_text_hit_counts_as_anchor(self):
        # 语料点到注册取景地名/keywords 即算有锚（与 matched_scenes 同命中精神）
        data = {"scenes": self.REG,
                "shots": [{"id": 1, "image_prompt": "画室的窗前，他俯身喷漆"},
                          {"id": 2, "image_prompt": "检查站顶端她举起补光灯"}]}
        self.assertNotIn("scene_unanchored", self._codes(data))

    def test_global_scene_suppresses_unanchored(self):
        # 全局固定场景在场（单景短片工作流）——不催逐镜声明
        data = {"scene": "同一间教室", "shots": [{"id": 1, "image_prompt": "回眸"}]}
        self.assertNotIn("scene_unanchored", self._codes(data))

    def test_adjacent_disjoint_scenes_reported_as_jump(self):
        data = {"scenes": self.REG,
                "shots": [{"id": 1, "scenes": ["画室"], "image_prompt": "喷漆"},
                          {"id": 2, "scenes": ["渊口"], "image_prompt": "人海"}]}
        fs = [f for f in vr.lint(data) if f.code == "scene_jump"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].level, "info")       # 直切可能是剧情要的，只提示不告警

    def test_transition_between_breaks_the_jump(self):
        data = {"scenes": self.REG,
                "shots": [{"id": 1, "scenes": ["画室"], "image_prompt": "喷漆"},
                          {"id": 2, "kind": "transition", "dur": 1.0, "narration": "",
                           "transition": {"type": "fade_black", "text": "渊口"}},
                          {"id": 3, "scenes": ["渊口"], "image_prompt": "人海"}]}
        self.assertNotIn("scene_jump", self._codes(data))

    def test_same_scene_run_is_silent(self):
        data = {"scenes": self.REG,
                "shots": [{"id": i, "scenes": ["渊口"], "image_prompt": "夜"}
                          for i in (1, 2, 3)]}
        self.assertNotIn("scene_jump", self._codes(data))

    def test_garbage_scene_fields_do_not_raise(self):
        # 软闸底线：scenes 写成字符串/数字/混入非 dict 注册项都不许炸
        for bad in ("洞窟", 7, {"x": 1}):
            data = {"scenes": [None, "字符串", {"name": ""}],
                    "shots": [{"id": 1, "scenes": bad, "image_prompt": "x"},
                              {"id": 2, "image_prompt": "y"}]}
            self.assertIsInstance(vr.lint(data), list)


class TestAbstractEmotionTerms(unittest.TestCase):
    """表演物理化纪律：情绪该演不该说——画面描述里的情绪名词逐条给身体化改写。

    与 SLOP_TERMS 分表分 code：那张管「零视觉信息的评价词」，这张管「该演不该说
    的情绪名词」，扫描面还多出 action/end_state 两个 delta 骨架位。"""

    def test_table_shape(self):
        self.assertGreaterEqual(len(vr.EMOTION_TERMS), 10)
        for term, hint in vr.EMOTION_TERMS.items():
            self.assertTrue(term.strip() and hint.strip(), f"{term} 词条不完整")
            self.assertGreaterEqual(len(hint), 8, f"{term} 的改写建议太短，等于没给")
            self.assertNotIn(term, vr.SLOP_TERMS, f"{term} 与 SLOP_TERMS 重复登记")

    def test_hit_carries_hint(self):
        shots = [_shot(1, image_prompt="少女愤怒地拍桌"), _shot(2)]
        got = _by_code(vr.lint({"shots": shots}), "emotion_abstract")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1,))
        self.assertEqual(got[0].hint, vr.EMOTION_TERMS["愤怒"])

    def test_delta_fields_also_scanned(self):
        # action/end_state 缺 video_prompt 时会被原样编译进视频请求——同罪同罚
        shots = [_shot(1, action="少年悲伤地低下头")]
        self.assertIn("emotion_abstract", _codes(vr.lint({"shots": shots})))

    def test_narration_is_exempt(self):
        # 台词里说「他很难过」是表演内容，不是描述缺陷
        shots = [_shot(1, narration="那天他真的很难过。", image_prompt="窗边的背影")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "emotion_abstract"), [])

    def test_emotion_field_is_exempt(self):
        # emotion 字段是给 TTS 的情绪档，该写——本维度扫的是画面描述不是它
        shots = [_shot(1, emotion="悲伤", image_prompt="少女垂眼望着杯沿")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "emotion_abstract"), [])


class TestPromptPronoun(unittest.TestCase):
    """画面代词禁令：设定图挂载按 name/keywords 文本命中——代词命中率为零。"""

    def test_pronoun_in_prompt_warns(self):
        shots = [_shot(1, image_prompt="她拿起它，转身看向门口")]
        got = _by_code(vr.lint({"shots": shots}), "prompt_pronoun")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1,))
        self.assertIn("name", got[0].hint)

    def test_compound_words_are_exempt(self):
        # 其他/其它/吉他 是合成词不是代词，不许误伤
        shots = [_shot(1, image_prompt="桌上摆着吉他，其他物件推到画面边缘，其它不动")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "prompt_pronoun"), [])

    def test_dialogue_is_exempt(self):
        # 台词里的代词是人话——扫描面只有画面字段
        shots = [_shot(1, narration="「你把它还给他。」", image_prompt="林深递出短刀")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "prompt_pronoun"), [])

    def test_delta_fields_scanned(self):
        shots = [_shot(1, end_state="他的手停在半空")]
        self.assertIn("prompt_pronoun", _codes(vr.lint({"shots": shots})))

    def test_quoted_dialogue_inside_picture_fields_is_exempt(self):
        """action/beats 里原文引用台词（说出「她明天出院了。」）是人话不是画面；
        引号外的代词照判。"""
        shots = [_shot(1, action="老周抬眼说出「她明天出院了。」，手指按住硬币")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "prompt_pronoun"), [])
        shots = [_shot(1, action="老周说出「两杯。」，她的手停住")]
        self.assertIn("prompt_pronoun", _codes(vr.lint({"shots": shots})))


class TestNarrationStyle(unittest.TestCase):
    """旁白文风维度：念出来的文案有自己的机器指纹（与提示词空词分表分通道）。"""

    def test_pivot_rhetoric_warns(self):
        shots = [_shot(1, narration="你以为办卡就会去健身，其实三成人第二年就不再去了。"),
                 _shot(2)]
        got = _by_code(vr.lint({"shots": shots}), "narration_pivot")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1,))

    def test_pivot_covers_multiple_coats(self):
        # 抬价句式换一套字面仍是同一个动作——按句式拦不是按词拦
        coats = ("这不是懒，而是身体在自我保护。",
                 "看似省钱，实则每一步都在多花钱。",
                 "回头再看，才发现那一步就错了。")
        for text in coats:
            shots = [_shot(1, narration=text)]
            self.assertIn("narration_pivot", _codes(vr.lint({"shots": shots})),
                          f"「{text}」应被识别为抬价句式")

    def test_plain_statement_passes(self):
        # 正面写法：反常识的事实本身直接说出来，不立靶不翻案
        shots = [_shot(1, narration="办卡的人里，三成第二年就不再去了。")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "narration_pivot"), [])

    def test_jargon_table_shape_and_hit(self):
        self.assertGreaterEqual(len(vr.NARRATION_SLOP), 15)
        for term, hint in vr.NARRATION_SLOP.items():
            self.assertTrue(term.strip() and hint.strip(), f"{term} 词条不完整")
            self.assertGreaterEqual(len(hint), 8, f"{term} 的改写建议太短，等于没给")
        shots = [_shot(1, narration="这套打法给商家赋能，形成了闭环。")]
        got = _by_code(vr.lint({"shots": shots}), "narration_jargon")
        self.assertEqual({f.message.split("「")[1].split("」")[0] for f in got},
                         {"打法", "赋能", "闭环"})
        for f in got:
            self.assertEqual(f.hint,
                             vr.NARRATION_SLOP[f.message.split("「")[1].split("」")[0]])

    def test_prompts_not_scanned_for_narration_style(self):
        # 提示词与旁白分表分通道——image_prompt 里的「闭环」轮不到本维度管
        shots = [_shot(1, image_prompt="传送带绕成一个闭环")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "narration_jargon"), [])

    def test_nominalization_warns(self):
        shots = [_shot(1, narration="团队对方案进行了优化，实现了效率的提升。")]
        got = _by_code(vr.lint({"shots": shots}), "narration_nominal")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertIn("还原成动作", got[0].hint)

    def test_grand_words_only_on_last_speaking_shot(self):
        # 正文里的宏大词不管（那是题材），只把关收尾镜（CTA 位是机器升华的头号现场）
        shots = [_shot(1, narration="这个时代的浪潮谁也挡不住。"),
                 _shot(2, narration="他把杯子洗了，放回原处。")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "cta_grand"), [])
        shots2 = [_shot(1, narration="他把杯子洗了，放回原处。"),
                  _shot(2, narration="这就是我们这个时代的答案。")]
        got = _by_code(vr.lint({"shots": shots2}), "cta_grand")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "info")       # 史诗/纪录题材合法，判断权在人
        self.assertEqual(got[0].shots, (2,))

    def test_opener_repeat_warns_at_three(self):
        shots = [_shot(i, narration=f"其实第{i}件事没那么简单。") for i in range(1, 4)]
        got = _by_code(vr.lint({"shots": shots}), "narration_opener")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].shots, (1, 2, 3))
        two = [_shot(i, narration=f"其实第{i}件事没那么简单。") for i in range(1, 3)]
        self.assertEqual(_by_code(vr.lint({"shots": two}), "narration_opener"), [])

    def test_lines_are_visible_to_style_lint(self):
        # 只写 lines[] 的多角色镜不许在本维度隐身（voicecast.shot_text 口径）
        shots = [_shot(1, narration="", lines=[
            {"speaker": "甲", "text": "你以为我在开玩笑，其实我说真的。"}])]
        self.assertIn("narration_pivot", _codes(vr.lint({"shots": shots})))

    def test_slop_term_never_fires_on_narration(self):
        # 双向隔离的另一半：旁白维度的存在不许扩大 slop_term 的扫描面
        shots = [_shot(1, narration="这画面真唯美，也真有氛围感。")]
        self.assertEqual(_by_code(vr.lint({"shots": shots}), "slop_term"), [])


class TestVoiceoverMode(unittest.TestCase):
    """旁白语态：旁白不是分镜的必填件——剧情档镜镜旁白＝把漫剧写成了解说。
    语态缺省由画风归属 skill 派生（skills.py 单一真源），
    章节顶层 voiceover 显式声明凌驾缺省。"""

    @staticmethod
    def _mix(n_vo, n_dlg, n_silent):
        shots, i = [], 1
        for _ in range(n_vo):                          # 无 speaker 的 narration＝旁白镜
            shots.append(_shot(i)); i += 1
        for _ in range(n_dlg):                         # 具体角色开口＝对白镜
            shots.append(_shot(i, speaker="林深")); i += 1
        for _ in range(n_silent):                      # 纯画面镜（合法·静音占位）
            shots.append(_shot(i, narration="")); i += 1
        return shots

    def test_profile_drives_default_mode(self):
        self.assertEqual(vr.voiceover_mode({"profile": "anime"}), "sparse")
        self.assertEqual(vr.voiceover_mode({"profile": "explainer"}), "lead")
        # none 没有画风派生来源，只走顶层显式声明这一条路
        self.assertEqual(vr.voiceover_mode({"voiceover": "none"}), "none")
        self.assertEqual(vr.voiceover_mode({}), "lead")
        self.assertEqual(vr.voiceover_mode({"profile": "anime", "voiceover": "lead"}),
                         "lead", "显式声明凌驾画风缺省")
        # 样本要能区分 skill 位打没打中：anime 派生 sparse、kn-showcase 绑 lead
        self.assertEqual(vr.voiceover_mode({"profile": "anime",
                                            "skill": "kn-showcase"}), "lead")
        self.assertEqual(vr.voiceover_mode({"profile": "anime"}), "sparse")

    def test_drama_full_voiceover_warns(self):
        data = {"profile": "anime", "shots": self._mix(6, 0, 0)}
        got = _by_code(vr.lint(data), "voiceover_heavy")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1, 2, 3, 4, 5, 6))
        self.assertIn("lines", got[0].hint)

    def test_drama_dialogue_driven_passes(self):
        # 2 旁白 / 6 对白 / 2 纯画面：正常漫剧形态，一条不报
        got = vr.lint({"profile": "anime", "shots": self._mix(2, 6, 2)})
        self.assertNotIn("voiceover_heavy", _codes(got))
        self.assertNotIn("no_silent_shot", _codes(got))

    def test_lines_shots_count_as_dialogue(self):
        shots = [_shot(i, narration="",
                       lines=[{"speaker": "林深", "text": f"第{i}句台词。"}])
                 for i in range(1, 5)]
        self.assertNotIn("voiceover_heavy",
                         _codes(vr.lint({"profile": "anime", "shots": shots})))

    def test_explainer_full_voiceover_is_silent(self):
        # lead（解说驱动）每镜旁白是常态：两个 code 都不许出现
        got = vr.lint({"profile": "explainer", "shots": self._mix(6, 0, 0)})
        self.assertNotIn("voiceover_heavy", _codes(got))
        self.assertNotIn("no_silent_shot", _codes(got))

    def test_none_mode_rejects_any_voiceover(self):
        data = {"voiceover": "none", "shots": self._mix(1, 0, 4)}
        got = _by_code(vr.lint(data), "voiceover_heavy")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].shots, (1,))

    def test_explicit_declaration_beats_default(self):
        # 剧情画风做说书式内容是合法选择：声明 lead 即免报
        data = {"profile": "anime", "voiceover": "lead", "shots": self._mix(6, 0, 0)}
        self.assertNotIn("voiceover_heavy", _codes(vr.lint(data)))

    def test_all_speaking_drama_gets_breath_hint(self):
        got = _by_code(vr.lint({"profile": "anime", "shots": self._mix(2, 6, 0)}),
                       "no_silent_shot")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "info")

    def test_named_narrator_counts_as_voiceover(self):
        # speaker 写「旁白」不是角色开口——仍是旁白镜
        shots = [_shot(i, speaker="旁白") for i in range(1, 6)]
        self.assertIn("voiceover_heavy",
                      _codes(vr.lint({"profile": "anime", "shots": shots})))

    def test_garbage_immune(self):
        for data in ({"profile": 7, "voiceover": 3, "shots": [_shot(1)]},
                     {"profile": "anime",
                      "shots": [{"id": 1, "lines": "坏", "narration": 5},
                                _shot(2), _shot(3), _shot(4)]}):
            self.assertIsInstance(vr.lint(data), list)


class TestShiftGap(unittest.TestCase):
    """视觉换挡间距：长段无可见换挡只 info 提示——判定只认五个结构化位，
    写在提示词正文里的变化引擎看不见，所以点名区间请人复核而不判错。"""

    @staticmethod
    def _flat(n, dur=10.0):
        return [{"id": i, "dur": dur, "narration": f"第{i}句台词写得够长了",
                 "camera": f"运镜{i}", "framing": "中景", "scenes": ["画室"],
                 "emotion": "calm"}
                for i in range(1, n + 1)]

    def test_long_flat_stretch_informs(self):
        data = {"scenes": [{"name": "画室"}], "shots": self._flat(6)}   # 60s 零换挡
        got = _by_code(vr.lint(data), "shift_gap")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "info")
        self.assertIn("换挡", got[0].message)

    def test_short_piece_not_judged(self):
        data = {"scenes": [{"name": "画室"}], "shots": self._flat(5, dur=8.0)}  # 40s<45s
        self.assertEqual(_by_code(vr.lint(data), "shift_gap"), [])

    def test_scene_switch_resets_the_clock(self):
        shots = self._flat(6)
        for s in shots[3:]:
            s["scenes"] = ["渊口"]                    # 30s 处换取景地
        data = {"scenes": [{"name": "画室"}, {"name": "渊口"}], "shots": shots}
        self.assertEqual(_by_code(vr.lint(data), "shift_gap"), [])

    def test_framing_far_jump_counts_as_shift(self):
        shots = self._flat(6)
        shots[2]["framing"] = "远景"
        shots[3]["framing"] = "特写"                  # 30s 处远↔近大跨
        data = {"scenes": [{"name": "画室"}], "shots": shots}
        self.assertEqual(_by_code(vr.lint(data), "shift_gap"), [])

    def test_transition_counts_as_shift(self):
        shots = self._flat(6)
        shots.insert(3, {"id": 99, "kind": "transition", "dur": 1.0, "narration": ""})
        data = {"scenes": [{"name": "画室"}], "shots": shots}
        self.assertEqual(_by_code(vr.lint(data), "shift_gap"), [])

    def test_dirty_dur_does_not_raise(self):
        shots = self._flat(6)
        shots[0]["dur"] = "慢"
        data = {"scenes": [{"name": "画室"}], "shots": shots}
        self.assertIsInstance(vr.lint(data), list)


class TestBilingualPrompts(unittest.TestCase):
    """提示词双语完备性（`prompt_bilingual`）：中文写了、英文对位不能空。

    没有这个维度时，整章一个英文都没写、lint 也报 0 警告——没有任何维度
    看这两个字段。缺英文的代价是路由到 `prompt_lang=en` 的
    模型时静默回落中文，不报错也不留痕，只有对比成片才看得出来。
    """

    def _shots(self, n=3, **kw):
        return [_shot(i, image_prompt_en=f"shot {i} frame", **kw)
                for i in range(1, n + 1)]

    def test_missing_image_en_is_a_warning(self):
        # 「强制双语」的表达就是 warn：与 slop/代词/复述同档，不是可选提示
        data = {"shots": [_shot(1), _shot(2)]}
        found = _by_code(vr.lint(data), "prompt_bilingual")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "warn")
        self.assertEqual(found[0].shots, (1, 2))
        self.assertIn("image_prompt_en", found[0].message)

    def test_complete_bilingual_is_silent(self):
        data = {"shots": self._shots()}
        self.assertEqual(_by_code(vr.lint(data), "prompt_bilingual"), [])

    def test_image_and_video_reported_separately(self):
        """两条字段各报一条：混成一条就分不清缺的是画面还是运动，改起来得逐镜猜。"""
        shots = self._shots(3)
        shots[0]["video_prompt"] = "转身"          # 只有镜1 写了运动中文
        found = _by_code(vr.lint({"shots": shots}), "prompt_bilingual")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].shots, (1,))
        self.assertIn("video_prompt_en", found[0].message)

    def test_video_prompt_absent_is_not_chased(self):
        """判据是「有中文才要求英文」——kenburns 章节不写 video_prompt，不该被催。"""
        data = {"shots": self._shots()}
        self.assertEqual(_by_code(vr.lint(data), "prompt_bilingual"), [])

    def test_blank_string_counts_as_missing(self):
        # 空串/纯空白与缺键同罪：留个空字段绕过守卫等于守卫不存在
        data = {"shots": [_shot(1, image_prompt_en="   ")]}
        self.assertEqual(_by_code(vr.lint(data), "prompt_bilingual")[0].shots, (1,))

    def test_transition_and_omitted_shots_excluded(self):
        # 转场镜零提示词、弃用镜不进成片，都不该被催英文（复用 active_shots 单一真源）
        data = {"shots": [
            _shot(1, image_prompt_en="shot 1 frame"),
            {"id": 2, "kind": "transition", "dur": 1.0, "transition": {"type": "fade"}},
            _shot(3, review={"shot": {"state": "omt"}})]}
        self.assertEqual(_by_code(vr.lint(data), "prompt_bilingual"), [])


class TestMotionPlanDepth(unittest.TestCase):
    """运动规划深度档（`motion_plan` / `beats_span`）：会送进视频模型的镜要有逐拍时间轴。

    这条守卫的价值在于它盯的是**发出去的提示词有没有时间结构**——散文式 video_prompt
    看起来"写得很详细"，但模型拿不到秒段就得自己猜配速。判据只在 native/dubbed 下成立
    （kenburns 不调视频模型），previz 镜走另一条运动预演路径故豁免。
    """

    def _doc(self, shots, motion="native"):
        return {"motion": motion, "shots": shots}

    def _beat_shot(self, no, ts=("0-2s", "2-4s")):
        s = _shot(no, image_prompt_en=f"shot {no}", dur=4.0)
        s["sketch"] = {"beats": [{"t": t, "action": f"第{i}拍动作"}
                                 for i, t in enumerate(ts, 1)]}
        return s

    def test_flat_prose_shot_is_warned(self):
        found = _by_code(vr.lint(self._doc([_shot(1), _shot(2)])), "motion_plan")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "warn")
        self.assertEqual(found[0].shots, (1, 2))

    def test_beats_present_is_silent(self):
        self.assertEqual(
            _by_code(vr.lint(self._doc([self._beat_shot(1)])), "motion_plan"), [])

    def test_only_video_modes_are_judged(self):
        """kenburns 根本不调用视频模型，时间轴无处可发——催了没有可行动项。"""
        self.assertEqual(
            _by_code(vr.lint(self._doc([_shot(1)], "kenburns")), "motion_plan"), [])

    def test_previz_shot_is_exempt(self):
        """3D 预演与简笔拍序列互斥（active_guide 仲裁）——不能两条路一起催。"""
        s = _shot(1)
        s["guide"] = "previz"
        self.assertEqual(_by_code(vr.lint(self._doc([s])), "motion_plan"), [])

    def test_authored_spans_must_cover_the_shot(self):
        """authored t 不随 dur 重算：改过时长而秒段没跟着改 = 发出去的是假脚本。"""
        s = self._beat_shot(1)
        s["dur"] = 10.0                       # 秒段仍停在 4s
        found = _by_code(vr.lint(self._doc([s])), "beats_span")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "warn")

    def test_spans_matching_duration_are_silent(self):
        self.assertEqual(
            _by_code(vr.lint(self._doc([self._beat_shot(1)])), "beats_span"), [])

    def test_free_text_spans_are_not_second_guessed(self):
        """t 写成自由文本（非秒段）无法体检——宁可跳过也不误报。"""
        s = self._beat_shot(1, ts=("开场", "收尾"))
        self.assertEqual(_by_code(vr.lint(self._doc([s])), "beats_span"), [])


class TestPromptNegation(unittest.TestCase):
    """`video_prompt` 写成禁令清单（`prompt_negation`）。

    与 `prompt_thin` 互补而非重复：那一条只数字符，数不出「这 120 字里 70 字是
    禁区」。典型退化路径：出片不理想就补一条禁令，几轮之后正文里再没有
    力学、物理反馈与镜头执行，只剩一串「不要」，而字数反倒达标了。
    """

    @staticmethod
    def _data(vp, motion="native"):
        return {"motion": motion,
                "shots": [_shot(1, image_prompt="画" * 130, video_prompt=vp)]}

    def test_negation_heavy_prompt_is_warned(self):
        vp = ("不出现风，不要重复上一集的开门，不让主体做大动作，"
              "不能把暗斑实体化，铜管轻微共振")
        found = _by_code(vr.lint(self._data(vp)), "prompt_negation")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "warn")
        self.assertIn("negative_prompt", found[0].hint, "要指明禁令该搬去哪")

    def test_action_prompt_with_a_couple_of_bounds_passes(self):
        """边界约束是正常写法，本仓自己的范例也带一两条——过半才算退化。"""
        vp = ("右前爪先探进浅水试温，重心随之压到前肢，"
              "水面荡开两圈低矮涟漪后回落，最后收爪停在石阶边缘，不要跳跃")
        self.assertEqual(_by_code(vr.lint(self._data(vp)), "prompt_negation"), [])

    def test_short_prompt_is_not_judged(self):
        """一两个分句时占比没有意义——写了一句禁令就是 100%。"""
        self.assertEqual(_by_code(vr.lint(self._data("不要跳跃，不要瞬移")),
                                  "prompt_negation"), [])
        self.assertEqual(vr.NEGATION_MIN_CLAUSES, 4)

    def test_only_judged_when_the_video_model_is_involved(self):
        """kenburns 不调用视频模型，催了没有可行动项（同 prompt_echo 的门槛）。"""
        vp = ("不出现风，不要重复上一集的开门，不让主体做大动作，"
              "不能把暗斑实体化，铜管轻微共振")
        self.assertEqual(
            _by_code(vr.lint(self._data(vp, motion="kenburns")), "prompt_negation"), [])

    def test_result_clauses_are_not_mistaken_for_bans(self):
        """「不再脱落」「与上一镜不同」是终态与对比，不是禁令——误报会逼作者删描述。"""
        vp = ("铜屑从螺纹间成股剥落后不再脱落，光比与上一镜不同，"
              "腕部发力顺序由肩及肘，水花向左溅出半尺")
        self.assertEqual(_by_code(vr.lint(self._data(vp)), "prompt_negation"), [])

    def test_reports_the_worst_shot_with_numbers(self):
        vp = "不出现风，不要开门，不让主体动，不能实体化"
        found = _by_code(vr.lint(self._data(vp)), "prompt_negation")
        self.assertIn("4 个分句里 4 句", found[0].message)


class TestPromptThickness(unittest.TestCase):
    """提示词厚度地板（`prompt_thin`）：写了就得写够。

    典型失误形态：为了消除「时间轴 + 正文」的重复，把正文砍成
    「全程对称构图不破」这种只有约束没有内容的一句话——**消重不等于删描述**。
    薄提示词的出片构图与画风均正确，但缺少表演内容，且缺陷要到成片才可见。
    """

    def _thin(self, **kw):
        # 画面提示词给足，隔离出"只在测运动提示词厚度"这一件事。
        # **长度必须跟着常量走**：写死数值的夹具会在地板上调时自己变成
        # "薄画面提示词"，把本类用例整批拖红，与被测行为毫无关系。
        return {"motion": "native",
                "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                image_prompt_en="x", **kw)]}

    def test_one_line_video_prompt_is_warned(self):
        found = _by_code(vr.lint(self._thin(video_prompt="全程对称构图不破。")),
                         "prompt_thin")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "warn")
        self.assertIn("运动提示词", found[0].message)

    def test_substantial_video_prompt_passes(self):
        body = "白" * vr.MIN_VIDEO_PROMPT_CHARS
        self.assertEqual(_by_code(vr.lint(self._thin(video_prompt=body)),
                                  "prompt_thin"), [])

    def test_thin_image_prompt_is_warned(self):
        data = {"motion": "kenburns",
                "shots": [{"id": 1, "dur": 4.0, "narration": "x", "image_prompt": "近景"}]}
        found = _by_code(vr.lint(data), "prompt_thin")
        self.assertEqual(len(found), 1)
        self.assertIn("画面提示词", found[0].message)

    def test_empty_video_prompt_is_another_dimension_s_job(self):
        """该不该写由 motion_plan 催，本维度只管"既然写了就得写够"——两条不重复喊。"""
        s = _shot(1, image_prompt="镜1" + "画" * vr.MIN_IMAGE_PROMPT_CHARS,
                  image_prompt_en="x")
        found = _by_code(vr.lint({"motion": "native", "shots": [s]}), "prompt_thin")
        self.assertEqual(found, [])

    def test_video_thinness_is_judged_even_in_kenburns(self):
        """kenburns 章不豁免运动厚度："kenburns 不读 video_prompt"不构成豁免理由。
        豁免的话，整章 video_prompt 停在一句话短稿、与 camera 互相打架，lint 也
        一条不报；可这些字段跑 `gen-video` 或加 `--motion native` 就原样发了出去。

        `render_mode()` 那条"别催该模式下不存在的阶段"的通则仍然成立——它管的是
        **阶段**（如 emotion→TTS），本维度判的是**已经写在盘上的字段内容**。
        """
        data = {"motion": "kenburns",
                "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                video_prompt="推近。")]}
        found = _by_code(vr.lint(data), "prompt_thin")
        self.assertEqual(len(found), 1)
        self.assertIn("运动提示词", found[0].message)

    def test_kenburns_with_video_prompt_gets_a_mode_notice(self):
        """薄不薄之外还要说清「它现在不出片、切模式就会原样发出」，
        否则作者会以为这些字段是死的。恒 info：不是错，是知会。"""
        body = "白" * (vr.MIN_VIDEO_PROMPT_CHARS + 10)
        data = {"motion": "kenburns",
                "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                video_prompt=body)]}
        found = _by_code(vr.lint(data), "prompt_thin_mode")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "info")
        # 视频模式下不重复喊——那里本就该写
        native = {"motion": "native", "shots": data["shots"]}
        self.assertEqual(_by_code(vr.lint(native), "prompt_thin_mode"), [])
        # 没写 video_prompt 的 kenburns 章一个字都不该收到
        bare = {"motion": "kenburns",
                "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10))]}
        self.assertEqual(_by_code(vr.lint(bare), "prompt_thin_mode"), [])

    def test_kenburns_camera_only_is_a_style_key_not_a_motion_prompt(self):
        """静图档的 camera 只是 Ken Burns 的运镜风格键，单独写它不构成运动稿：
        不报薄，也不发切模式知会——没有会被原样发出的文字。"""
        data = {"motion": "kenburns",
                "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                camera="缓慢推近")]}
        found = vr.lint(data)
        self.assertEqual(_by_code(found, "prompt_thin"), [])
        self.assertEqual(_by_code(found, "prompt_thin_mode"), [])

    def test_kenburns_delta_field_still_counts_as_written(self):
        """delta 骨架位（action/end_state…）是会送进视频模型的运动稿，静图档写了照判。"""
        data = {"motion": "kenburns",
                "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                camera="缓慢推近", action="抬手")]}
        found = _by_code(vr.lint(data), "prompt_thin")
        self.assertEqual(len(found), 1)
        self.assertIn("运动提示词", found[0].message)

    def test_seedance_camera_only_is_still_judged(self):
        """动镜档下 camera 随请求发出，单独写它就是一份薄运动稿——口径不变。"""
        for motion in ("dubbed", "native"):
            data = {"motion": motion,
                    "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                    camera="缓慢推近")]}
            found = _by_code(vr.lint(data), "prompt_thin")
            self.assertEqual(len(found), 1, motion)
            self.assertIn("运动提示词", found[0].message)

    def test_english_only_motion_prompt_counts_and_is_measured(self):
        """PromptSpec 只写 text_en 时正文落在 video_prompt_en，模型实收的就是这段英文：
        「写没写」与厚度都按它算，与 prompts.video_prompt 的中缺回英同口径。"""
        body = "w" * (vr.MIN_VIDEO_PROMPT_CHARS + 10)
        for motion in ("kenburns", "dubbed"):
            data = {"motion": motion,
                    "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                    video_prompt_en=body, camera="缓慢推近")]}
            found = vr.lint(data)
            self.assertEqual(_by_code(found, "prompt_thin"), [], motion)
            self.assertEqual(len(_by_code(found, "prompt_thin_mode")),
                             1 if motion == "kenburns" else 0, motion)

    def test_malformed_motion_fields_do_not_raise(self):
        """lint 是挂在生图前的软闸：字段类型写坏按其字符串形态判，返回结果而不是抛错。"""
        for motion in ("kenburns", "dubbed"):
            data = {"motion": motion,
                    "shots": [_shot(1, image_prompt="画" * (vr.MIN_IMAGE_PROMPT_CHARS + 10),
                                    action=["抬手"], video_prompt=123)]}
            found = _by_code(vr.lint(data), "prompt_thin")
            self.assertEqual(len(found), 1, motion)

    def test_floors_sit_between_real_and_lazy(self):
        """地板必须卡在"真实创作"与"一句话打发"之间：过高误伤真实创作，过低失去拦截作用。

        两条上界的出处是**被手把手迭代过、真出过片的章节**的逐镜最短值：
        `video_prompt` 178 字、`image_prompt` 147 字。改地板数字必须
        同批附新的实测出处，不许只改数字（现行值的实测出处写在
        `variation.MIN_*_PROMPT_CHARS` 的注释里）。
        """
        self.assertGreater(vr.MIN_VIDEO_PROMPT_CHARS, 70, "要能拦住 20~70 字的一句话")
        self.assertLess(vr.MIN_VIDEO_PROMPT_CHARS, 150, "真实分镜单最短 178 字，不得误伤")
        self.assertGreater(vr.MIN_IMAGE_PROMPT_CHARS, 40)
        self.assertLess(vr.MIN_IMAGE_PROMPT_CHARS, 147, "真实分镜单最短 147 字")


class TestGenericCastNames(unittest.TestCase):
    """角色泛称（`generic_name`）：提示词里必须点注册名，不许写「主角」「队长」。

    泛称有两重代价：设定图按 name/keywords **文本命中**才自动挂载，写「队长从上跃下」
    挂不上那张设定图；同一个角色在画面提示词里叫注册名、在运动提示词里叫泛称，模型
    无从判断说的还是不是同一个人。与代词维度分开——代词是指代不明，泛称是换了个名字。
    """

    def _doc(self, prompt, names=("白刻", "守卫队长")):
        return {"characters": [{"name": n} for n in names],
                "shots": [_shot(1, image_prompt=prompt, image_prompt_en="x")]}

    def test_generic_term_is_warned(self):
        found = _by_code(vr.lint(self._doc("主角站在门口，画面很暗" * 6)), "generic_name")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "warn")
        self.assertEqual(found[0].shots, (1,))

    def test_registered_name_passes(self):
        self.assertEqual(
            _by_code(vr.lint(self._doc("白刻站在门口，画面很暗" * 6)), "generic_name"), [])

    def test_generic_inside_a_registered_name_is_not_a_hit(self):
        """角色本身就叫「守卫队长」时，「队长」二字是名字的一部分，报它就是纯误伤。"""
        self.assertEqual(
            _by_code(vr.lint(self._doc("守卫队长自上层栈桥跃下" * 5)), "generic_name"), [])

    def test_projects_without_a_cast_are_not_judged(self):
        """没有设定集就没有"注册名"可言，催不出可行动项。"""
        self.assertEqual(
            _by_code(vr.lint(self._doc("主角站在门口" * 8, names=())), "generic_name"), [])

    def test_hint_lists_the_registered_names(self):
        found = _by_code(vr.lint(self._doc("反派登场了，画面很暗" * 6)), "generic_name")
        self.assertIn("白刻", found[0].hint)
        self.assertIn("守卫队长", found[0].hint)


if __name__ == "__main__":
    unittest.main()


class TestCameraClash(unittest.TestCase):
    """`camera` 与 `video_prompt` 各写一个互斥运镜——两者被引擎串成一句发出。

    判据是「两边都映射出运镜类目、且**交集为空**」，vp 侧还只取谈摄影机的小句。
    这两道缺一不可，反例见下面的必不报三条：
      · 有交集 = vp 在延展 camera（chrome/corridor 全章是这形态），不是冲突；
      · 不筛小句 = 「身体往下沉了半寸」被当成降镜（`static` 预设原文自己就写着
        「只有画面内的主体与环境在运动」，主体动 ≠ 镜头动）。
    """

    def _one(self, **kw):
        return vr.lint({"motion": "native", "shots": [_shot(1, **kw)]})

    def test_static_camera_versus_a_crane_in_the_body(self):
        """catquest 镜1 的最小复刻。"""
        f = _by_code(self._one(camera="固定机位，镜头完全静止，构图稳定不动",
                               video_prompt="镜头从低处缓缓升起越过橘猫，逐层揭示远处餐桌全景"),
                     "camera_clash")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "warn")
        self.assertEqual(f[0].shots, (1,))

    def test_push_in_versus_orbit(self):
        """catquest 镜2：字面互斥对写法漏掉的那一类（推近↔环绕）。"""
        self.assertTrue(_by_code(
            self._one(camera="镜头平稳缓慢推近主体，景别由中景收拢至近景",
                      video_prompt="镜头绕玻璃杯缓慢环绕四分之一圈，背景视差流动"),
            "camera_clash"))

    def test_not_reported_when_the_body_extends_the_camera(self):
        """有交集就放过——否则 corridor 这种写得厚的章节 47% 的镜会中招。"""
        self.assertEqual(_by_code(
            self._one(camera="镜头缓缓升起越过屋脊",
                      video_prompt="镜头缓缓升起并持续后仰，十二层天井随高度逐层展开"),
            "camera_clash"), [])

    def test_subject_motion_is_not_camera_motion(self):
        """abyss 那批误报的最小复刻：主体在动不是镜头在动。"""
        for body in ("身体往下沉了半寸，重心压低", "被人流挤着退开两步",
                     "两人在接触点上纹丝不动"):
            self.assertEqual(_by_code(
                self._one(camera="固定机位，镜头完全静止，构图稳定不动",
                          video_prompt=body), "camera_clash"), [], body)

    def test_silent_when_either_side_is_empty(self):
        self.assertEqual(_by_code(self._one(camera="缓慢推近"), "camera_clash"), [])
        self.assertEqual(_by_code(self._one(video_prompt="镜头缓慢推近"), "camera_clash"), [])

    def test_negated_camera_term_is_not_a_clash(self):
        """「特写不做任何环绕或旋转」是在排除一种运镜，不是在写它——否定词
        只对同一小句里排在它后面的运镜词生效，别的小句照判。"""
        self.assertEqual(_by_code(
            self._one(camera="固定机位，缓慢推近",
                      video_prompt="特写不做任何环绕或旋转，机位只推不摇"),
            "camera_clash"), [])
        self.assertTrue(_by_code(
            self._one(camera="固定机位，缓慢推近",
                      video_prompt="特写不做任何旋转，镜头绕主体缓慢环绕半圈"),
            "camera_clash"), "未被否定的环绕仍是冲突")


class TestPresetPlaceholder(unittest.TestCase):
    """运镜预设的填空位 X/Y 原样落盘（catquest 镜4 实盘）。

    判据是**结构性**的（裸 X/Y）而不是词表——`camera.py` 改填空写法、加档删档
    都不用跟着改一个字。人工维护那份英文词表已经漏过两个填空位，这就是为什么
    这里连词表都不建。"""

    def _one(self, **kw):
        return _by_code(vr.lint({"motion": "native", "shots": [_shot(1, **kw)]}),
                        "preset_placeholder")

    def test_reports_the_robotic_arm_leftover(self):
        f = self._one(camera="镜头如机械臂般从X角度平滑弧线摆至Y角度并持续跟随主体")
        self.assertEqual(len(f), 1)
        self.assertIn("robotic_arm", f[0].hint + f[0].message)

    def test_reports_english_placeholders_too(self):
        self.assertTrue(self._one(camera="a continuous flight through X, altitude steady"))

    def test_silent_once_the_author_filled_it_in(self):
        self.assertEqual(self._one(camera="镜头如机械臂般从侧后方平滑弧线摆至正面并持续跟随"), [])

    def test_does_not_fire_on_ordinary_letters(self):
        for cam in ("4X 变焦推近", "XY 轴同步位移", "镜头缓慢推近"):
            self.assertEqual(self._one(camera=cam), [], cam)

    def test_detection_covers_every_placeholder_still_in_the_preset_library(self):
        """**锁步守卫**：预设库里每一条带填空位的 phrase 都必须被这条维度抓住。

        不走「另抄一份填空词表 + 再加一道逐字节比对」——手抄词表会漏
        （`revealing Y in the distance`、`whip pan to X` 这类零散填空位最易漏），
        而这条断言让 `camera.py` 与维度天然锁步。"""
        from kinema.pipeline.camera import CAMERA_PRESETS
        known = vr._placeholder_presets()
        self.assertTrue(known, "预设库里一个填空位都没有了——本维度失去意义，请同批删除")
        for key, p in CAMERA_PRESETS.items():
            for field in ("phrase", "phrase_en"):
                text = str(p.get(field) or "")
                if not vr._has_placeholder(text):
                    continue
                self.assertIn(key, known, f"{key}.{field} 带填空位却没被派生集收录")
                self.assertTrue(self._one(camera=text),
                                f"{key}.{field} 的填空位没被 preset_placeholder 抓住")


class TestUnregisteredEntity(unittest.TestCase):
    """逐镜显式点名却在名册里查不到——纯查表、零猜测（不从正文抽专名）。"""

    def _lint(self, shot, **reg):
        return _by_code(vr.lint({"motion": "native", "shots": [shot], **reg}),
                        "unregistered_entity")

    def test_reports_a_name_absent_from_the_roster(self):
        f = self._lint(_shot(1, characters=["白刻", "无名氏"]),
                       characters=[{"name": "白刻"}])
        self.assertEqual(len(f), 1)
        self.assertIn("无名氏", f[0].message)

    def test_props_are_checked_too(self):
        """`ctx` 若只装 characters/scenes——不带 props 这条判据就永远不报。"""
        self.assertTrue(self._lint(_shot(1, props=["不存在的道具"]),
                                   props=[{"name": "单分子刃"}]))

    def test_silent_on_registered_names(self):
        self.assertEqual(self._lint(_shot(1, characters=["白刻"]),
                                    characters=[{"name": "白刻"}]), [])

    def test_missing_and_empty_lists_are_both_skipped(self):
        """缺省=全部出场（没点名）；显式 `[]`=明确无人（点的是"没有"）。两种都不是"查不到"。"""
        for shot in (_shot(1), _shot(1, characters=[])):
            self.assertEqual(self._lint(shot, characters=[{"name": "白刻"}]), [])

    def test_silent_when_the_roster_itself_is_empty(self):
        """名册整个没建（设定单节点之前跑 lint），不是"查不到"。"""
        self.assertEqual(self._lint(_shot(1, characters=["白刻"])), [])


class TestCraftLeak(unittest.TestCase):
    """工艺痕迹漏进交付文本：`review.note` 会被编译进下一版提示词。"""

    def _lint(self, shot):
        return _by_code(vr.lint({"motion": "native", "shots": [shot]}), "craft_leak")

    def test_reports_a_filename_in_a_retake_note(self):
        s = _shot(1, review={"clip": {"state": "retake", "note": "参考 shot_3.png 那版的收速"}})
        self.assertTrue(self._lint(s))

    def test_reports_a_version_number_in_the_picture_text(self):
        self.assertTrue(self._lint(_shot(1, video_prompt="照 v2 的力度再来一次")))

    def test_silent_on_a_clean_note(self):
        s = _shot(1, review={"clip": {"state": "retake",
                                      "note": "刃光收速再慢一档，末帧停在双手上"}})
        self.assertEqual(self._lint(s), [])

    def test_engine_written_stale_note_is_not_self_inflicted_noise(self):
        """维度自身产生的噪声会成为误报源：`lineage.mark_stale` 的过期提示若把文件名拼进
        note，每一次设定图更新都会让本维度报一条永远修不掉的警告。
        文件清单只归 `stale_refs`、note 保持素净，这条钉住该分工。"""
        from kinema import lineage, review
        s = _shot(1)
        note = "引用的设定图已更新，请按新设定图重生成以保持一致"
        review.set_state(s, "image", "retake", note=note)
        self.assertEqual(self._lint(s), [])
        src = (Path(lineage.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("设定图已更新（", src, "引擎又把文件名拼回 note 了")


class TestCraftLeakProcessWords(unittest.TestCase):
    """工序词（分镜图/简笔板/底片）同属工艺痕迹：「分镜图改为无面部构图」是写给
    流水线的工程指令，编译进提示词后模型只能按字面把这些词当画面内容处理。"""

    def _lint(self, shot):
        return _by_code(vr.lint({"motion": "native", "shots": [shot]}), "craft_leak")

    def test_reports_pipeline_words_in_a_retake_note(self):
        s = _shot(1, review={"clip": {"state": "retake",
                                      "note": "分镜图改为无面部构图，需按新底片重生"}})
        self.assertTrue(self._lint(s))

    def test_engine_stale_note_stays_clean(self):
        """引擎自己的过期提示（「设定图已更新」）不含工序词——词表扩了也不许
        让维度对着引擎产物报警。"""
        from kinema import review
        s = _shot(1)
        review.set_state(s, "image", "retake",
                         note="引用的设定图已更新，请按新设定图重生成以保持一致")
        self.assertEqual(self._lint(s), [])


class TestBurnMixedNarrationLint(unittest.TestCase):
    """混烧 × 同镜对白+旁白：对白镜整镜由模型发声，夹带的旁白句会换成模型
    嗓音，与其他镜烧录的固定音色旁白不同源——说话人级单声源在旁白身上断掉。"""

    def _lint(self, doc):
        return _by_code(vr.lint(doc), "burn_mixed_narration")

    def test_reports_dialogue_shots_with_narrator_lines(self):
        doc = {"motion": "native", "native_voiceover": True,
               "shots": [{"id": 1, "dur": 6.0,
                          "lines": [{"speaker": "凯尔", "text": "走。"},
                                    {"speaker": "旁白", "text": "夜里起了风。"}]},
                         _shot(2, narration="纯旁白句。")]}
        f = self._lint(doc)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "warn")
        self.assertEqual(f[0].shots, (1,))
        self.assertIn("纯旁白镜", f[0].hint)

    def test_silent_on_clean_splits(self):
        pure = {"motion": "native", "native_voiceover": True,
                "shots": [_shot(1, speaker="凯尔", narration="走。"),
                          _shot(2, narration="旁白句。")]}
        self.assertEqual(self._lint(pure), [], "对白镜与旁白镜分开即合规")
        no_burn = {"motion": "native",
                   "shots": [{"id": 1, "dur": 6.0,
                              "lines": [{"speaker": "凯尔", "text": "走。"},
                                        {"speaker": "旁白", "text": "起风了。"}]}]}
        self.assertEqual(self._lint(no_burn), [], "未开混烧时全部模型发声，同源")


class TestDubbedDialogueLint(unittest.TestCase):
    """dubbed × 对白上镜：烧录轨与模型口型两条时间轴不同源，开口对齐只做整体
    平移——多句/多人镜必然失配。dubbed 的领地是全旁白解说章。"""

    def _lint(self, doc):
        return _by_code(vr.lint(doc), "dubbed_dialogue")

    def test_reports_dialogue_shots(self):
        doc = {"motion": "dubbed",
               "shots": [_shot(1, speaker="凯尔", narration="走。"),
                         _shot(2, narration="旁白句。")]}
        f = self._lint(doc)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "warn")
        self.assertEqual(f[0].shots, (1,))
        self.assertIn("native", f[0].hint)

    def test_voiceover_only_dubbed_is_clean(self):
        doc = {"motion": "dubbed",
               "shots": [_shot(1, narration="旁白句。"),
                         _shot(2, speaker="旁白", narration="又一句。")]}
        self.assertEqual(self._lint(doc), [], "全旁白解说章是 dubbed 的正当领地")

    def test_native_chapter_is_out_of_scope(self):
        doc = {"motion": "native",
               "shots": [_shot(1, speaker="凯尔", narration="走。")]}
        self.assertEqual(self._lint(doc), [])


class TestSceneDaypart(unittest.TestCase):
    """取景地时段缺口：基准图会自选一个时段画进去，之后全链路把它当光线基准
    ——写实档降级路线上更直接顶 @图片1。info 级、只点没表态的。"""

    def _lint(self, scenes):
        doc = {"motion": "native", "scenes": scenes,
               "shots": [_shot(1, narration="走。")]}
        return _by_code(vr.lint(doc), "scene_daypart_missing")

    def test_reports_scene_without_daypart(self):
        f = self._lint([{"name": "塔内控制舱",
                         "desc": "弧形舷窗俯瞰沙海，指示灯稀疏地亮着琥珀色"}])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "info")
        self.assertIn("塔内控制舱", f[0].message)

    def test_silent_when_daypart_is_pinned(self):
        self.assertEqual(self._lint([{"name": "塔内控制舱",
                                      "desc": "正午强光从弧形舷窗直射进来"}]), [])

    def test_lamp_words_do_not_count_as_daypart(self):
        """「灯光稀疏」是陈设不是时段——词表按具名时段词拦，不认单字「光」。"""
        f = self._lint([{"name": "机房", "desc": "灯光稀疏，指示灯闪烁"}])
        self.assertEqual(len(f), 1)


class TestFatigueLook(unittest.TestCase):
    """角色外貌缺省不写疲态：黑眼圈/眼袋/憔悴…只在用户点名时写，并登记进
    visual_requirements 作显式表态。判据函数与 `project refs` 的出图闸共用。"""

    def _lint(self, chars):
        doc = {"motion": "native", "characters": chars,
               "shots": [_shot(1, narration="走。")]}
        return _by_code(vr.lint(doc), "character_fatigue_look")

    def test_warns_on_fatigue_wording(self):
        f = self._lint([{"name": "林夏", "appearance": "二十七岁女性，眼下青黑，黑色长发"}])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "warn")
        self.assertIn("林夏", f[0].message)
        self.assertIn("眼下青黑", f[0].message)

    def test_silent_when_registered_as_visual_requirement(self):
        self.assertEqual(self._lint([{"name": "林夏", "appearance": "眼下青黑",
                                      "visual_requirements": ["眼下青黑"]}]), [])

    def test_constraints_are_negative_channel(self):
        """写在 constraints 是要避免它，不算疲态表述。"""
        self.assertEqual(self._lint([{"name": "林夏", "appearance": "鹅蛋脸",
                                      "constraints": ["黑眼圈"]}]), [])

    def test_role_text_counts_and_clothing_color_does_not(self):
        f = self._lint([{"name": "老周", "appearance": "青黑色长袍",
                         "role": "熬夜守灯的老人"}])
        self.assertEqual(len(f), 1)
        self.assertIn("熬夜", f[0].message)
        self.assertNotIn("青黑", f[0].message)


class TestEmptyShotCast(unittest.TestCase):
    """空镜 × 全员兜底：画面写「无人」而镜级 characters 键缺失时，设定图与
    绑定句照常注入、与画面声明打架（空镜会被画进两个角色）。"""

    def _lint(self, shot, characters=({"name": "凯尔"},)):
        doc = {"motion": "native", "characters": list(characters), "shots": [shot]}
        return _by_code(vr.lint(doc), "empty_shot_cast")

    def test_reports_missing_key_on_an_empty_shot(self):
        f = self._lint(_shot(1, image_prompt="无人的控制舱，仪表微光"))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "info")
        self.assertIn("characters: []", f[0].hint)

    def test_explicit_empty_list_is_the_correct_form(self):
        self.assertEqual(
            self._lint(_shot(1, image_prompt="无人的控制舱", characters=[])), [])

    def test_drone_word_is_not_an_empty_shot(self):
        self.assertEqual(
            self._lint(_shot(1, image_prompt="无人机掠过沙丘上空")), [])

    def test_silent_without_a_roster(self):
        """名册为空时兜底本来就注入不了任何设定图，不报。"""
        self.assertEqual(
            self._lint(_shot(1, image_prompt="空镜：走廊尽头"), characters=()), [])


class TestMontageChop(unittest.TestCase):
    """碎切体检：生成式片段镜间恒硬切，短镜密集=截断感逐镜累积。
    只在按秒计费的两个模式判；kenburns 的 2~3 秒一变是静图幻灯片的节奏律。"""

    def _lint(self, durs, motion="dubbed"):
        shots = [_shot(i + 1, dur=d, narration="台词。") for i, d in enumerate(durs)]
        return _by_code(vr.lint({"motion": motion, "shots": shots}), "montage_chop")

    def test_dense_short_shots_are_reported(self):
        f = self._lint([4, 4, 5, 4, 5])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "warn")
        self.assertIn("8~15s", f[0].hint)

    def test_long_take_design_stays_quiet(self):
        self.assertEqual(self._lint([10, 12, 4, 9, 14]), [])

    def test_kenburns_is_out_of_scope(self):
        self.assertEqual(self._lint([3, 3, 3, 3, 3], motion="kenburns"), [])

    def test_tiny_chapters_are_not_judged(self):
        """三镜以内谈不上节奏统计，样本太小只会误报。"""
        self.assertEqual(self._lint([4, 4, 4]), [])


class TestCaptionVoiceless(unittest.TestCase):
    """无声镜挂字幕：dubbed/native 下有人声的镜字幕逐字取台词，观众两三镜就把
    「底部出字」读成「有人在说话」，轮到无声镜的 caption 就读成漏了配音。"""

    def _lint(self, shots, motion="native"):
        return _by_code(vr.lint({"motion": motion, "shots": shots}),
                        "caption_voiceless")

    def _mixed(self):
        return [_shot(1, narration="", caption="机库还剩一台能动的机"),
                _shot(2, narration="出击。", speaker="江迟"),
                _shot(3, narration="", caption="三架，一次齐射"),
                _shot(4, narration="够了。", speaker="江迟")]

    def test_mixed_chapter_names_the_voiceless_captions(self):
        f = self._lint(self._mixed())
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].level, "warn")
        self.assertEqual(f[0].shots, (1, 3))

    def test_all_silent_chapter_is_out_of_scope(self):
        """整片无人声时字幕只有一种语义，不存在混读。"""
        self.assertEqual(self._lint([_shot(1, narration="", caption="字卡一"),
                                     _shot(2, narration="", caption="字卡二")]), [])

    def test_kenburns_is_out_of_scope(self):
        """静图片本就靠字卡叙事，那里字幕不承担台词轨。"""
        self.assertEqual(self._lint(self._mixed(), motion="kenburns"), [])

    def test_silent_shots_without_captions_stay_quiet(self):
        self.assertEqual(self._lint([_shot(1, narration=""),
                                     _shot(2, narration="出击。", speaker="江迟")]), [])


class TestUnfilmableTerms(unittest.TestCase):
    """拍不出来的内心词——与抽象情绪走同一条通道，但发不同的 code。"""

    def _lint(self, **kw):
        return vr.lint({"motion": "native", "shots": [_shot(1, **kw)]})

    def test_reports_an_inner_state_with_no_picture(self):
        f = _by_code(self._lint(image_prompt="他意识到危险正在逼近"), "unfilmable_term")
        self.assertEqual(len(f), 1)
        self.assertIn("瞳孔收缩", f[0].hint)

    def test_does_not_fire_across_word_boundaries(self):
        """「半透明白色」里嵌着「明白」——子串命中即误报，故词表不含
        「明白」「感到」这类易嵌进他词的条目。"""
        self.assertEqual(_by_code(self._lint(image_prompt="半透明白色属性面板悬在身前"),
                                  "unfilmable_term"), [])

    def test_kept_separate_from_abstract_emotion(self):
        """两类问题不许混成一条 code——前端与测试按 code 断言。"""
        found = self._lint(image_prompt="他意识到危险，脸上写满恐惧")
        self.assertTrue(_by_code(found, "unfilmable_term"))
        self.assertTrue(_by_code(found, "emotion_abstract"))


class TestScoredMix(unittest.TestCase):
    """audio_mode=scored 与画面模式的组合体检（_lint_scored_mix）。

    dubbed 是语义硬冲突（对口型人声与整轨人声互斥），native 的角色对白是
    口型不同源的软风险——两者都要在写分镜阶段（花钱之前）点名，
    而不是等 gen-video 报「缺配音需先 tts」这句在 scored 下指错路的错误。"""

    def _codes(self, data):
        return [f.code for f in vr.lint(data)]

    def test_dubbed_under_scored_is_a_conflict(self):
        data = {"audio_mode": "scored", "motion": "dubbed",
                "shots": [{"id": 1, "dur": 4, "narration": "第一句。"}]}
        hits = [f for f in vr.lint(data) if f.code == "scored_dubbed_conflict"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].level, "warn")
        self.assertIn("tracks", hits[0].hint, "指路必须给出两条出路之一")
        self.assertIn("native", hits[0].hint)

    def test_native_dialogue_under_scored_names_the_shots(self):
        data = {"audio_mode": "scored", "motion": "native",
                "shots": [
                    {"id": 1, "dur": 4, "narration": "旁白句。"},
                    {"id": 2, "dur": 4,
                     "lines": [{"text": "你来了", "speaker": "林深"}]},
                    # 旁白 speaker 不算对白——画外音没有口型可对
                    {"id": 3, "dur": 4, "speaker": "旁白", "narration": "画外音。"},
                ]}
        hits = [f for f in vr.lint(data) if f.code == "scored_native_dialogue"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].shots, (2,), "只点名真有角色开口的镜")

    def test_narration_driven_scored_native_stays_silent(self):
        data = {"audio_mode": "scored", "motion": "native",
                "shots": [{"id": 1, "dur": 4, "narration": "旁白句。"}]}
        codes = self._codes(data)
        self.assertNotIn("scored_native_dialogue", codes)
        self.assertNotIn("scored_dubbed_conflict", codes)

    def test_tracks_mode_never_fires(self):
        data = {"motion": "dubbed",
                "shots": [{"id": 1, "dur": 4,
                           "lines": [{"text": "你来了", "speaker": "林深"}]}]}
        self.assertNotIn("scored_dubbed_conflict", self._codes(data))


class TestNativeVoiceSource(unittest.TestCase):
    """native 成片人声与字幕是否同源（_lint_native_voice_source）。

    缺省的 native 不跑 TTS、不烧混音，成片里唯一的人声是视频模型按提示词念出
    的那条，而字幕恒按章节文本编译且没有关闭开关。提示词已经把台词逐字发过去，
    但模型照不照办没有确定性保证、链路里也没有转写核对——这条软闸就是把
    「一致」与「待核对」区分开的那一句。"""

    NATIVE = {"motion": "native",
              "shots": [{"id": 1, "dur": 4, "narration": "第一句。"},
                        {"id": 2, "dur": 4, "narration": "第二句。"}]}

    def _hits(self, data):
        return [f for f in vr.lint(data) if f.code == "native_voice_unverified"]

    def test_default_native_names_the_speaking_shots(self):
        hits = self._hits(self.NATIVE)
        self.assertEqual(len(hits), 1, "章节级事实，只报一条")
        self.assertEqual(hits[0].level, "warn")
        self.assertEqual(hits[0].shots, (1, 2))
        self.assertIn("native_voiceover", hits[0].hint, "指路必须给出两条出路之一")

    def test_burning_fixed_voice_settles_it(self):
        """开了混烧，主音轨是我们按同一份文本合成的 TTS——不再有不同源问题。"""
        self.assertEqual(self._hits({**self.NATIVE, "native_voiceover": True}), [])

    def test_other_render_modes_never_fire(self):
        """kenburns/dubbed 的人声都出自我们的 TTS，与字幕同源。"""
        for motion in ("kenburns", "dubbed"):
            self.assertEqual(self._hits({**self.NATIVE, "motion": motion}), [], motion)

    def test_scored_defers_to_the_dialogue_conflict(self):
        """scored 把人声整轨替换，片段自配音听不到——那条冲突归
        scored_native_dialogue，此处不重复报。"""
        self.assertEqual(self._hits({**self.NATIVE, "audio_mode": "scored"}), [])

    def test_verified_shots_drop_out_of_the_warning(self):
        """ASR 文字核对是本条 hint 自己指定的出口。核完还报＝这条警告没有终态，
        而一条清不掉的警告只会训练人忽略整张 lint。"""
        verified = {**self.NATIVE,
                    "verify": {"voice": {"ok": True, "kind": "asr",
                                         "rows": [{"id": 1, "score": 1.0},
                                                  {"id": 2, "score": 0.93}]}}}
        self.assertEqual(self._hits(verified), [], "两镜都核过就不该再报")
        half = {**self.NATIVE,
                "verify": {"voice": {"rows": [{"id": 1, "score": 1.0}]}}}
        self.assertEqual(self._hits(half)[0].shots, (2,), "只报还没核过的那镜")

    def test_silent_chapter_has_nothing_to_mismatch(self):
        self.assertEqual(
            self._hits({"motion": "native",
                        "shots": [{"id": 1, "dur": 4, "caption": "只有底部字卡"}]}),
            [])


class TestSketchShadowed(unittest.TestCase):
    """beats 写了、缺省仲裁落 previz——lint 侧同样点名（gen-video 之前的零成本发现点）。"""

    def _doc(self, **shot_over):
        s = _shot(1, video_prompt="转身",
                  sketch={"beats": [{"action": "抬手"}, {"action": "转身"}]},
                  previz="pz.mp4")
        s.update(shot_over)
        return {"motion": "native", "shots": [s]}

    def test_shadowed_beats_fire(self):
        self.assertIn("sketch_shadowed", _codes(vr.lint(self._doc())))

    def test_explicit_guide_previz_is_silent(self):
        self.assertNotIn("sketch_shadowed", _codes(vr.lint(self._doc(guide="previz"))),
                         "显式表态=用户点过名，previz 就是本意")

    def test_sketch_guide_shot_is_not_shadowed(self):
        self.assertNotIn("sketch_shadowed", _codes(vr.lint(self._doc(guide="sketch"))))


class TestBeatRhythm(unittest.TestCase):
    """authored beats 的两条字面硬伤：静止开场与相邻拍重复。"""

    def _doc(self, beats, motion="native"):
        return {"motion": motion, "shots": [_shot(1, sketch={"beats": beats})]}

    def test_static_open_fires(self):
        beats = [{"action": "静止站立"}, {"action": "抬手"}]
        self.assertIn("beat_static_open", _codes(vr.lint(self._doc(beats))))

    def test_action_in_progress_open_is_fine(self):
        beats = [{"action": "喷枪已在喷涂中"}, {"action": "沿肩甲走出一道弧"}]
        self.assertNotIn("beat_static_open", _codes(vr.lint(self._doc(beats))))

    def test_adjacent_repeat_fires(self):
        beats = [{"action": "抬手"}, {"action": "抬手"}, {"action": "收势"}]
        self.assertIn("beat_repeat", _codes(vr.lint(self._doc(beats))))

    def test_kenburns_never_fires(self):
        beats = [{"action": "静止站立"}, {"action": "静止站立"}]
        codes = _codes(vr.lint(self._doc(beats, motion="kenburns")))
        self.assertNotIn("beat_static_open", codes)
        self.assertNotIn("beat_repeat", codes)


class TestEntryContinuity(unittest.TestCase):
    """承接契约的咬合体检：只写一半点名，两侧都写或都不写沉默。"""

    def _doc(self, shots, motion="native"):
        return {"motion": motion, "shots": shots}

    def test_half_written_pair_fires(self):
        shots = [_shot(1, end_state="手停在半空"), _shot(2)]
        found = _by_code(vr.lint(self._doc(shots)), "entry_continuity")
        self.assertTrue(found)
        self.assertIn("1→2", found[0].message)

    def test_entry_without_prev_end_fires(self):
        shots = [_shot(1), _shot(2, entry_state="镜头停在空椅上")]
        self.assertIn("entry_continuity", _codes(vr.lint(self._doc(shots))))

    def test_matched_pair_is_silent(self):
        shots = [_shot(1, end_state="手停在半空"),
                 _shot(2, entry_state="从手停在半空的构图接起")]
        self.assertNotIn("entry_continuity", _codes(vr.lint(self._doc(shots))))

    def test_hard_cut_both_empty_is_silent(self):
        shots = [_shot(1), _shot(2)]
        self.assertNotIn("entry_continuity", _codes(vr.lint(self._doc(shots))),
                         "硬切是合法创作决定——承接契约是 opt-in 不是必填项")

    def test_transition_is_a_legal_break(self):
        shots = [_shot(1, end_state="手停在半空"),
                 {"id": 9, "kind": "transition", "dur": 1.6,
                  "transition": {"type": "fade_black"}},
                 _shot(2)]
        self.assertNotIn("entry_continuity", _codes(vr.lint(self._doc(shots))))

    def test_kenburns_is_silent(self):
        shots = [_shot(1, end_state="手停在半空"), _shot(2)]
        self.assertNotIn(
            "entry_continuity",
            _codes(vr.lint(self._doc(shots, motion="kenburns"))))


class TestCoverMissing(unittest.TestCase):
    """封面缺口维度：判据钉在「分镜图已齐」这一刻。

    封面不是自动产物，提醒若只挂在 `stage_gen_image` 收尾，图出齐后重跑那条
    命令会走空计划出口，提醒就再也不出现——lint 这道闸每轮体检都在。
    """

    @staticmethod
    def _doc(shots, cover=None):
        d = {"shots": shots}
        if cover is not None:
            d["cover"] = cover
        return d

    def test_fires_when_images_done_without_cover(self):
        shots = [_shot(i, image=f"/w/shot_{i}.png") for i in (1, 2)]
        got = _by_code(vr.lint(self._doc(shots)), "cover_missing")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertIn("cover", got[0].hint)

    def test_silent_before_images_are_done(self):
        # 生图之前/生图途中催封面是催早了——软闸每轮生图都跑，噪音会淹掉真告警
        shots = [_shot(1, image="/w/shot_1.png"), _shot(2)]
        self.assertNotIn("cover_missing", _codes(vr.lint(self._doc(shots))))

    def test_silent_once_cover_registered(self):
        shots = [_shot(i, image=f"/w/shot_{i}.png") for i in (1, 2)]
        doc = self._doc(shots, cover={"primary": "x/assets/covers/ch01_3x4.png"})
        self.assertNotIn("cover_missing", _codes(vr.lint(doc)))

    def test_omitted_and_transition_shots_do_not_block(self):
        # 弃镜/转场镜没有分镜图是常态，不该让「图齐」永远判不成立
        shots = [_shot(1, image="/w/shot_1.png"),
                 {"id": 2, "kind": "transition", "dur": 1.6,
                  "transition": {"type": "fade_black"}},
                 {"id": 3, "review": {"shot": {"state": "omt"}}}]
        self.assertIn("cover_missing", _codes(vr.lint(self._doc(shots))))


class TestTopviewMissing(unittest.TestCase):
    """俯视布局图缺口维度：判据钉在「基准图已在盘」这一刻。

    视频请求每镜附主场景的图纸；缺了它，模型对镜头在这个空间的位置没有依据。
    该缺口不影响出图、不报错、不挡下一步，除本维度外没有观测点。
    """

    @staticmethod
    def _doc(scenes=None, **top):
        return {"shots": [_shot(1), _shot(2)], "scenes": scenes or [], **top}

    def test_reports_scene_with_sheet_but_no_layout(self):
        got = [f for f in vr.lint(self._doc(
            [{"name": "书店", "sheet": "/w/scene_书店.png"}]))
            if f.code == "topview_missing"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertIn("书店", got[0].message)
        self.assertIn("project refs", got[0].hint)

    def test_silent_when_paired(self):
        self.assertNotIn("topview_missing", _codes(vr.lint(self._doc(
            [{"name": "书店", "sheet": "/w/scene_书店.png",
              "topview_sheet": "/w/scene_top_书店.png"}]))))

    def test_silent_before_the_base_sheet_exists(self):
        # 图纸以基准图为空间取材，基准图未定稿时判据不成立
        self.assertNotIn("topview_missing",
                         _codes(vr.lint(self._doc([{"name": "书店"}]))))

    def test_covers_the_fixed_scene_slot(self):
        got = _codes(vr.lint(self._doc(scene="全片同一间旧书店",
                                       scene_ref="/w/scene.png")))
        self.assertIn("topview_missing", got)

    def test_silent_when_design_is_skipped(self):
        # skip_design 的项目不走设定集，本维度对它无意义
        self.assertNotIn("topview_missing", _codes(vr.lint(self._doc(
            [{"name": "书店", "sheet": "/w/scene_书店.png"}], skip_design=True))))


class TestPaceSparseScope(unittest.TestCase):
    """语速偏低只对进烧录轨的镜是「拖节奏」：native 对白镜由模型发声、动作驱动，
    写了拍表的镜节奏由拍序列给出。"""

    def _codes(self, doc):
        return [f.code for f in vr.lint(doc)]

    def test_native_dialogue_with_beats_is_not_sparse(self):
        shot = {"id": 1, "dur": 8.0, "camera": "缓推",
                "lines": [{"speaker": "甲", "text": "走。"}],
                "sketch": {"beats": [{"action": "起身"}, {"action": "迈步"}, {"action": "回望"}]}}
        self.assertNotIn("pace_sparse", self._codes({"motion": "native", "shots": [shot]}))

    def test_burned_narration_is_still_judged(self):
        shot = {"id": 1, "dur": 8.0, "camera": "缓推", "narration": "走。"}
        self.assertIn("pace_sparse", self._codes({"motion": "dubbed", "shots": [shot]}))
        self.assertIn("pace_sparse", self._codes({"motion": "kenburns", "shots": [shot]}))


class TestNarrationOverrun(unittest.TestCase):
    """台词超窗预估：按在用档案的实测语速在花钱前点名；没有语速的档案不估，
    模型自声的对白镜不进旁白轨、不估。"""

    _BANK = {"seq": 1, "casts": [{"id": "vc_0001", "owner": "旁白", "mode": "custom",
                                  "voice_type": "custom:vc_0001", "clip": "vc_0001.mp3",
                                  "speech_rate": 2.4}]}
    _LONG = "凌晨三点，还醒着的人，都在等点什么，等一台烘干机停下来。"   # 24 字 → 10.0s

    def _doc(self, text, dur=7, rate=True):
        bank = json.loads(json.dumps(self._BANK))
        if not rate:
            del bank["casts"][0]["speech_rate"]
        return {"motion": "native", "native_voiceover": True,
                "narrator_voice": "custom:vc_0001", "voice_bank": bank,
                "shots": [_shot(1, dur=dur, narration=text)]}

    def test_overlong_narration_warns_with_the_estimate(self):
        got = _by_code(vr.lint(self._doc(self._LONG)), "narration_overrun")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].level, "warn")
        self.assertEqual(got[0].shots, (1,))
        self.assertIn("10.0s/7s", got[0].message)

    def test_narration_inside_the_window_passes(self):
        doc = self._doc("零点四十，末班车，有人坐过了站。")          # 14 字 → 5.8s
        self.assertNotIn("narration_overrun", _codes(vr.lint(doc)))

    def test_a_cast_without_a_rate_is_not_estimated(self):
        self.assertNotIn("narration_overrun", _codes(vr.lint(self._doc(self._LONG, rate=False))))

    def test_model_voiced_dialogue_is_not_estimated(self):
        doc = self._doc("")
        doc["voice_bank"]["casts"][0]["owner"] = "甲"
        doc["voices"] = {"甲": "custom:vc_0001"}
        doc["shots"] = [_shot(1, dur=4, narration="",
                              lines=[{"speaker": "甲", "text": self._LONG}])]
        self.assertNotIn("narration_overrun", _codes(vr.lint(doc)))
        doc["motion"] = "dubbed"                       # 对白由 TTS 烧录，进旁白轨
        self.assertIn("narration_overrun", _codes(vr.lint(doc)))
