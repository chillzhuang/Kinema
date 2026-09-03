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

"""简笔分镜预演板守卫。四条不变量：

1. **互斥仲裁单一真源**：`sketchboard.active_guide` 是 previz/sketch 的唯一裁判——
   guide=sketch 的镜 previz 末帧与 V2V 一律不参与；显式表态恒赢绝不静默回落；
   scanner 的 `guide_active` 必须消费同一函数。
2. **风格防泄漏生命线**：板随请求附上时提示词必带「绝不输出铅笔素描画面」防护句，
   且该声明**与板真的附上了逐字一致**（板没附时绝不声明「所附分镜板」）。
3. **板绝不进 `image`/`clip`**：那是分镜画面与成片位，登记只落 `sketch.sheet`。
4. **能力闸**：provider 不支持 `ref_images` 就一张都不发（`**kwargs` 会静默吞掉），
   时间轴纯文本照发。
"""

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kinema import sketchboard as sk
from kinema.errors import ProjectError
from kinema.pipeline.variation import MULTISHOT_RE
from tests.support import LocalBackendEnv

ASSETS = Path(__file__).resolve().parents[1] / "kinema" / "studio_app"


def _png(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def _beats(n=3):
    return [{"action": f"动作{i}"} for i in range(1, n + 1)]


def _wav(p: Path, seconds=1.0) -> Path:
    """真 WAV（dubbed 真发前 request_seconds 会 ffprobe 它，假字节过不了探测）。"""
    import wave
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        self._env.restore()

    def _project(self, shots=None, **over):
        from kinema.project import Project
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {"id": "p1_ch01", "profile": "anime", "motion": "native",
               "aspect": "16:9", "shots": shots or [
                   {"id": 1, "dur": 5.0, "narration": "台词一", "video_prompt": "转身"}]}
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)


# ============================================================ 一、板提示词契约
class TestBoardPrompt(unittest.TestCase):
    def test_nine_panels_each_carry_a_time_label(self):
        shot = {"id": 1, "dur": 5.4, "sketch": {"beats": _beats(9)}}
        p = sk.board_prompt(shot)
        self.assertIn("3 行 × 3 列", p)
        self.assertIn("恰好 9 个", p)
        self.assertIn("绝不为凑满网格多画空白格", p, "空白格禁令是这条契约的生命线")
        for i in range(1, 10):
            self.assertRegex(p, rf"\n{i}\. 第[\d.]+-[\d.]+秒：动作{i}",
                             f"面板 {i} 必须带秒段标签（timeline 的立身之本）")

    def test_authored_beats_beyond_panel_max_refuse_loud(self):
        """手写 beats 超过单板上限：**报不改写**（authored 是创作资产）——
        生成侧静默截断是「板 12 格、时间轴 15 段」两套事实，Studio 下发全量
        对照表去对一张截过的板，逐格核对必然错行；时间轴不受画板上限约束。"""
        beats = [{"t": f"{i}-{i + 1}s", "action": f"动作{i}"} for i in range(15)]
        s = {"id": 1, "dur": 30.0, "sketch": {"beats": beats}}
        with self.assertRaises(ProjectError) as cm:
            sk.board_prompt(s)
        self.assertIn(str(sk.PANEL_MAX), str(cm.exception))
        self.assertIn("合并", str(cm.exception), "要给出可行动的出路，不是光拒绝")
        # 时间轴（timeline prompting）15 拍照编——受限的只有画板
        self.assertEqual(len(sk.effective_beats(s, 30.0)[0]), 15)

    def test_line_art_color_system_and_bottom_legend_spec(self):
        """五色画法必须在场，图例走**底部大字规格**。判据始终是字号而非图例本身：
        板头窄条 ~20px 必糊成伪汉字、~30px 拍号档基本正确——图例文字是固定语义
        不是身份内容，锚定拍号字号后照样板画即正确；模型侧语义仍走
        board_role_clause（图例的读者是人）。"""
        p = sk.board_prompt({"id": 1, "dur": 3, "sketch": {"beats": _beats(6)}})
        self.assertIn("铅笔素描分镜", p)
        self.assertIn("绝不整幅上色", p, "素描灰阶必须是绝对主体——这是板与成片的身份分界")
        for token in ("红色箭头", "蓝色箭头", "绿色标记", "橙色标记", "紫色波浪线", "黑色文本"):
            self.assertIn(token, p, "五色标注系统一色都不能少")
        self.assertIn("板底横排一条五色图例", p, "图例位置钉在底部——板头窄条是糊字重灾区")
        self.assertIn("字号与拍号标注相当", p, "字号锚定拍号档——那是实测能画对的下限")
        self.assertIn("避免静态站姿", p)
        # 板头标题：样板带标题栏，不钉死标题原文，模型会照搬样板上的人名
        self.assertIn("「镜 1」", p, "板头标题必须是本镜镜号的原文")
        self.assertIn("绝不照搬样板上的标题与人名", p)

    def test_ref_role_sentences_match_the_attachment_flags(self):
        """职责声明与实附组合逐条对应——声明一个没附的参考=向模型索要不存在的东西。"""
        shot = {"id": 1, "dur": 3, "sketch": {"beats": _beats(3)}}
        bare = sk.board_prompt(shot)
        self.assertNotIn("所附样板图", bare)
        self.assertNotIn("所附分镜图", bare)
        self.assertNotIn("所附角色设定图", bare)
        full = sk.board_prompt(shot, with_template=True, with_shot_image=True,
                               char_names=["陆昭"])
        self.assertIn("所附样板图", full)
        self.assertIn("所附分镜图", full)
        self.assertIn("所附角色设定图", full)
        self.assertIn("至多出现一次", full)
        # 样板自带底部图例横条——它属于版式，照样板的位置与字号画
        self.assertIn("图例横条属于版式", full)

    def test_zero_material_raises_with_an_actionable_message(self):
        """连自动拆拍都拆不出（运动设计全空）才拒绝——那种板是九张随机静帧。"""
        with self.assertRaises(ProjectError) as ctx:
            sk.board_prompt({"id": 7, "dur": 3})
        self.assertIn("运动设计", str(ctx.exception))
        self.assertIn("sketch.beats", str(ctx.exception))

    def test_no_style_prefix_slot(self):
        """板是素描基调、不掺成片画风——`board_prompt` 签名里刻意没有 style_prefix 参数。"""
        import inspect
        self.assertNotIn("style_prefix", inspect.signature(sk.board_prompt).parameters)

    def test_regen_note_compiles_into_the_prompt(self):
        """灯箱「重新生成」的意见走驳回闭环同范式：编译进「修正重点」，不带则无。"""
        shot = {"id": 1, "dur": 3, "sketch": {"beats": _beats(3)}}
        withn = sk.board_prompt(shot, note="第4格动作幅度更大")
        self.assertIn("本次修正重点（务必执行）：第4格动作幅度更大", withn)
        self.assertNotIn("修正重点", sk.board_prompt(shot))


# ============================================================ 二、时间轴与防泄漏句
class TestTimeline(unittest.TestCase):
    def test_segments_follow_beats_and_slots_fill_missing_t(self):
        shot = {"id": 1, "dur": 4.5,
                "sketch": {"beats": [{"action": "起身", "camera": "缓推"},
                                     {"action": "迈步", "t": "2-3s"},
                                     {"action": "回望"}]}}
        tl = sk.timeline_text(shot)
        self.assertIn("时间轴：", tl)
        # 段内字段序照厂商指南：运镜在最前，其后才是主体动作
        self.assertIn("第0-2秒：镜头：缓推，起身", tl)   # 秒段整秒（响应时间戳的型号以 1 秒为单位）
        self.assertIn("第2-3秒：迈步", tl)
        self.assertIn("第3-5秒：回望", tl)

    def test_shot_unit_numbers_segments_without_multishot_syntax(self):
        """不响应精确秒段的那一代按顺序编号分段：厂商指南把精确秒段列为「支持不稳定、
        强行限制时长可能导致生成结果异常」。段头不用镜号——「镜头 N」是本仓
        `multishot_syntax` 判为多镜的写法，而一镜一次调用只取回一段素材。"""
        shot = {"id": 1, "dur": 4.5,
                "sketch": {"beats": [{"action": "起身", "camera": "缓推"},
                                     {"action": "迈步", "t": "2-3s"},
                                     {"action": "回望"}]}}
        tl = sk.timeline_text(shot, unit="shot")
        self.assertTrue(tl.startswith("时间轴：第1段：镜头：缓推，起身"), tl)
        self.assertIn("第2段：迈步", tl)
        self.assertIn("第3段：回望", tl)
        self.assertNotIn("秒", tl, "这一代一个秒段都不该发")
        self.assertIsNone(MULTISHOT_RE.search(tl), tl)
        en = sk.timeline_text(shot, "en", unit="shot")
        self.assertTrue(en.startswith("Timeline: Segment 1: camera: 缓推"), en)
        self.assertIsNone(MULTISHOT_RE.search(en), en)

    def test_sound_beats_reach_the_model_only_in_native(self):
        """`beats[].sound` 若被排除出提示词，同一份 beats 字段覆盖就不对称：
        花钱画的板 PNG 拿到了声音脚本（`_beat_line` 会编「声：」），真正出声的
        视频模型反而拿不到——写好的音效脚本一条也进不了模型，而 native 是
        模型原生出音、**写与不写同价**。

        门控与 `prompts` 的 sfx 同源（native 才发）：dubbed/kenburns 的声音归合成段，
        发了没有消费者。标签必须与 `_beat_line` 逐字同源（zh「声」/ en「sound」）。"""
        shot = {"id": 1, "dur": 4.0,
                "sketch": {"beats": [{"action": "起身", "sound": "椅脚刮地"},
                                     {"action": "回望", "sound": "远处闷雷"}]}}
        off = sk.timeline_text(shot)
        self.assertNotIn("声：", off, "非 native 一个「声：」都不许出现")
        on = sk.timeline_text(shot, native=True)
        self.assertIn("声：椅脚刮地", on)
        self.assertIn("声：远处闷雷", on)
        self.assertNotIn("音：", on, "标签必须与 _beat_line 的「声」同源，别另造一个")
        en = sk.timeline_text(shot, "en", native=True)
        self.assertIn("sound: 椅脚刮地", en)
        self.assertNotIn("声：", en, "en 侧不许掺中文标签")

    def test_timeline_sound_does_not_move_the_board_fingerprint(self):
        """`beats_sig` 哈希的是 beat dict 本身、与时间轴输出零数据依赖——
        复活 sound 绝不能让 `board_drift` 报一次假漂移（板还是那张板）。"""
        shot = {"id": 1, "dur": 4.0,
                "sketch": {"beats": [{"action": "起身", "sound": "椅脚刮地"}]}}
        before = sk.beats_sig(shot)
        sk.timeline_text(shot, native=True)
        self.assertEqual(before, sk.beats_sig(shot))

    def test_timeline_has_sound_reports_what_was_actually_emitted(self):
        """`prompts` 靠它决定「还要不要再发一遍镜级 sfx」——判据必须与实发一致，
        否则会出现「时间轴没带声音、镜级 sfx 又被让位」的静默丢失。"""
        loud = {"id": 1, "dur": 4.0,
                "sketch": {"beats": [{"action": "起身", "sound": "椅脚刮地"}]}}
        mute = {"id": 2, "dur": 4.0, "sketch": {"beats": [{"action": "起身"}]}}
        self.assertTrue(sk.timeline_has_sound(loud, native=True))
        self.assertFalse(sk.timeline_has_sound(loud, native=False))
        self.assertFalse(sk.timeline_has_sound(mute, native=True))

    def test_timeline_never_carries_the_board_clause(self):
        """时间轴只管时间轴——板职责声明归 `board_role_clause`，由 prompts 拼在**头部**。

        声明若追加在时间轴尾巴上，那句最要紧的「板不是画面参考」就落在千余字
        提示词的中后段，压不住标注箭头——红蓝箭头会被画进开头几帧。
        该声明单点维护在头部，时间轴不携带。
        """
        shot = {"id": 1, "dur": 3, "sketch": {"beats": _beats(3)}}
        tl = sk.timeline_text(shot)
        self.assertIn("时间轴：", tl)
        self.assertNotIn("所附", tl, "时间轴里不许再出现板声明")

    def test_board_clause_states_it_is_a_script_not_a_picture(self):
        """防泄漏句的四件必备：不是画面参考 / 箭头是标注符号 / 不出素描质感 /
        不把箭头格线文字画进画面。少一件就是给模型留一条把板当画面抄的路。"""
        z = sk.board_role_clause("zh")
        self.assertIn("不是画面参考", z)
        self.assertIn("标注符号而非画面元素", z)
        self.assertIn("绝不输出铅笔素描", z)
        self.assertIn("绝不把标注箭头、格线与文字画进画面", z)
        # 五色语义随请求发全：板面已撤图例横条，这句是视频模型认识五色的唯一来源，
        # 只讲红蓝，另三色（取景框/灯光/强调）就只能靠模型猜
        for token in ("绿色方框", "橙色箭头", "紫色波浪线"):
            self.assertIn(token, z, "五色语义少一色，视频模型就得猜一色")
        e = sk.board_role_clause("en")
        self.assertIn("not a visual reference", e)
        self.assertIn("annotation marks, not picture elements", e)
        for token in ("green boxes", "orange arrows", "purple squiggles"):
            self.assertIn(token, e)

    def test_empty_beats_yield_empty_text(self):
        self.assertEqual(sk.timeline_text({"id": 1}), "")
        self.assertEqual(sk.timeline_text({"id": 1, "sketch": {"beats": ["坏形态"]}}), "")


# ============================================================ 二.五、自动拆拍回退
class TestAutoBeats(unittest.TestCase):
    def test_clause_split_is_deterministic_and_deduped(self):
        """句读切分（分号/句号）+ 去重保序 + 运镜/光落首拍——绝无语义改写。

        dur 取 9s 是刻意的：本用例验证的是**切分语义**，而拍数另受时长收敛管
        （`auto_beat_cap`，9s 允许 7 拍 > 这里的 5 句），两件事各测各的。"""
        shot = {"id": 1, "dur": 9.0, "camera": "缓推", "light_shift": "暖光恒定",
                "action": "俯身喷涂",
                "video_prompt": "俯身喷涂；雾锥显形。他抬眼核对；夹具转半圈",
                "end_state": "直身收枪"}
        beats = sk.auto_beats(shot)
        self.assertEqual([b["action"] for b in beats],
                         ["俯身喷涂", "雾锥显形", "他抬眼核对", "夹具转半圈", "直身收枪"])
        self.assertEqual(beats[0]["camera"], "缓推")
        self.assertEqual(beats[0]["light"], "暖光恒定")

    def test_authored_beats_always_win(self):
        shot = {"id": 1, "video_prompt": "散文一大段",
                "sketch": {"beats": [{"action": "自定义拍"}]}}
        beats, auto = sk.effective_beats(shot)
        self.assertFalse(auto)
        self.assertEqual(beats[0]["action"], "自定义拍")

    def test_no_material_yields_empty(self):
        self.assertEqual(sk.auto_beats({"id": 1, "dur": 3}), [])

    def test_overflow_merges_adjacent_clauses_into_panel_max(self):
        vp = "。".join(f"动作{i}" for i in range(30))
        beats = sk.auto_beats({"id": 1, "video_prompt": vp})
        self.assertLessEqual(len(beats), sk.PANEL_MAX, "面板上限内并拍，不丢句")
        self.assertIn("动作0", beats[0]["action"])
        self.assertIn("动作29", beats[-1]["action"])


class TestBeatCountFollowsDuration(unittest.TestCase):
    """**拍数按时长配**（用户点名「简笔分镜应该根据时间长度来，而不是固定 9 分镜」）。

    分工是本设计的全部要点：
      · 自动拆拍是**引擎代切的**（不是创作资产）→ 按 `TARGET_BEAT_SEC` 收敛，
        "作者这句写了几个分号"不该决定视频模型的执行密度；
      · authored beats 是**创作资产** → 一个字不动，超密只报警（`beats_density`）。
    立论证据：实测九拍铺 5.09s，Seedance 只演 2-3 个主事件、其余整段丢弃。
    """

    VP = "他抬手；喷枪走弧；漆雾沉降；他眯眼核对；转过夹具；直起身收枪"   # 6 句

    def _n(self, dur):
        return len(sk.auto_beats({"id": 1, "dur": dur, "video_prompt": self.VP}))

    def test_same_script_different_duration_yields_different_beat_count(self):
        """同一段分镜词，镜头越长拍越多——拍数随时长收敛，绝不只随句读。"""
        self.assertEqual(self._n(2.0), 2, "2s 镜收敛到下限 2 拍")
        self.assertEqual(self._n(3.0), 2)         # int(3/1.2)=2
        self.assertEqual(self._n(5.0), 4)         # int(5/1.2)=4（只随句读会给到 6 拍/0.83s）
        self.assertEqual(self._n(8.0), 6)         # cap=6 但素材只有 6 句 → 全留
        self.assertGreater(self._n(8.0), self._n(3.0), "长镜必须比短镜拍多")
        # 素材不足时绝不凭空造拍（引擎无 LLM 铁律）：20s 也只有 6 句可切
        self.assertEqual(self._n(20.0), 6)

    def test_every_auto_beat_clears_the_density_floor(self):
        """自动拆拍的产出恒不触发密度告警——收敛的意义就在于此。

        唯一例外是**镜头本身短到 2 拍都挤**（<1.6s）：那时引擎已收敛到下限，
        告警必须改口径说"只能加长镜头"，绝不劝一句自相矛盾的"建议并拍到 ≤2 拍"
        （告警的价值在于给得出可行动项）。"""
        for dur in (2.0, 3.3, 5.0, 7.0, 9.9, 15.0):
            s = {"id": 1, "dur": dur, "video_prompt": self.VP}
            self.assertIsNone(sk.beats_density(s, dur),
                              f"dur={dur} 的自动拆拍不该再报密度")
        tiny = {"id": 1, "dur": 1.5, "video_prompt": self.VP}
        msg = sk.beats_density(tiny, 1.5)
        self.assertIn("只能加长镜头秒数", msg)
        self.assertNotIn("建议并拍", msg, "已到下限还劝并拍=自相矛盾的废话")

    def test_authored_beats_are_never_merged_only_warned(self):
        """创作资产不许被引擎并拍：9 拍手写在 5s 镜上原样保留 + 报密度。"""
        a = {"id": 1, "dur": 5.0,
             "sketch": {"beats": [{"action": f"动作{i}"} for i in range(9)]}}
        beats, auto = sk.effective_beats(a, 5.0)
        self.assertEqual(len(beats), 9, "authored 拍数一个都不许合")
        self.assertFalse(auto)
        self.assertIn("并拍", sk.beats_density(a, 5.0) or "", "超密必须报警给建议")

    def test_merge_is_even_and_loses_no_clause_and_keeps_order(self):
        """并拍=均匀分组：6 句并 4 拍出 2/2/1/1，时序不乱、一句不丢。
        写成 ceil(n/k) 会并过头（6 句并 4 拍只出 3 拍）。"""
        beats = sk.auto_beats({"id": 1, "dur": 5.0, "video_prompt": self.VP})
        self.assertEqual(len(beats), 4)
        joined = "".join(b["action"] for b in beats)
        for clause in self.VP.split("；"):
            self.assertIn(clause, joined, f"并拍丢了句：{clause}")
        self.assertLess(joined.index("他抬手"), joined.index("直起身收枪"), "时序不许乱")
        self.assertEqual(sk._merge_evenly(list("abcdef"), 4), ["a，b", "c，d", "e", "f"])

    def test_grid_always_fills_exactly_and_never_leaves_blanks(self):
        """**网格必须恰好装下拍数**（否则同为 4 拍，一张排成 2×2 满格、
        另一张照 2×3 画完**留下两个空白框**）。

        根因是提示词自相矛盾：列数一旦硬编码成 3，4 拍就算出「2×3 网格共 4 个
        面板」——2×3=6 格却只要 4 个，模型只能自己猜。故按拍数选恰好填满的分解，
        质数（5/7/11）退回近方网格并**明说末行几格居中**，外加一条空白格禁令。"""
        for n in range(2, sk.PANEL_MAX + 1):
            rows, cols, last = sk.grid_of(n)
            self.assertEqual(cols * (rows - 1) + last, n,
                             f"{n} 拍的网格装不下/装多了：{rows}×{cols} 末行 {last}")
            self.assertGreaterEqual(last, 1, "末行不许是 0 格")
            self.assertLessEqual(last, cols)
            self.assertLessEqual(cols / rows, sk._GRID_MAX_RATIO + 1e-9,
                                 f"{n} 拍的网格太扁（每格会变成竖条）")
        self.assertEqual(sk.grid_of(4), (2, 2, 2), "4 拍必须 2×2 满格，绝不是 2×3 留两空")
        self.assertEqual(sk.grid_of(9), (3, 3, 3))
        self.assertEqual(sk.grid_of(6), (2, 3, 3))
        self.assertEqual(sk.grid_of(5)[2], 2, "5 拍：末行 2 格居中（3+2）")

    def test_prompt_states_the_exact_panel_count_and_bans_blanks(self):
        """提示词必须说死「恰好 N 个」并禁止补空格——这是 SHOT 05 空白格的对策。"""
        for n in (4, 5, 9):
            s = {"id": 1, "dur": 12.0,
                 "sketch": {"beats": [{"action": f"动作{i}"} for i in range(n)]}}
            head = sk.board_prompt(s, total=12.0).splitlines()[0]
            self.assertIn(f"恰好 {n} 个", head)
            self.assertIn("绝不为凑满网格多画空白格", head)
            rows, cols, last = sk.grid_of(n)
            if last == cols:
                self.assertIn(f"{rows} 行 × {cols} 列", head)
                self.assertIn("恰好填满", head)
            else:
                self.assertIn(f"最后一行只有 {last} 格并居中", head)
        en = sk.board_prompt({"id": 1, "dur": 6.0, "sketch": {"beats": _beats(4)}},
                             lang="en", total=6.0).splitlines()[0]
        self.assertIn("exactly 4", en)
        self.assertIn("never add empty or blank", en)

    def test_auto_split_only_lands_on_tidy_counts(self):
        """自动拆拍恒落在能整除成矩形的拍数上——引擎自己定的拍数没有非 5 不可的
        理由，退到 4 拍每拍反而更从容（7s 镜 5 拍→4 拍 = 1.75s/拍）且板面无空位。
        `TIDY_PANELS` 必须由 `grid_of` 派生，绝不另手写一份清单。"""
        self.assertEqual(sk.TIDY_PANELS, (2, 3, 4, 6, 8, 9, 10, 12))
        for p in sk.TIDY_PANELS:
            rows, cols, last = sk.grid_of(p)
            self.assertEqual(last, cols, f"{p} 进了整齐表却排不满")
        for t in (2, 3, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30):
            cap = sk.auto_beat_cap(t)
            self.assertIn(cap, sk.TIDY_PANELS, f"{t}s 的 cap={cap} 不是整齐拍数")
        self.assertEqual(sk.auto_beat_cap(7.0), 4, "7s：5 拍不整齐 → 退到 4")
        self.assertEqual(sk.auto_beat_cap(5.0), 4)
        self.assertEqual(sk.auto_beat_cap(10.0), 8)
        # 真实自动拆拍的产出也必须整齐（端到端，不只是 cap 函数）
        for dur in (3.0, 5.0, 8.0, 12.0):
            n = len(sk.auto_beats({"id": 1, "dur": dur, "video_prompt": self.VP}))
            self.assertEqual(sk.grid_of(n)[2], sk.grid_of(n)[1],
                             f"dur={dur} 自动拆出 {n} 拍，板面会留空位")

    def test_no_duration_info_falls_back_to_panel_max(self):
        """拿不到时长时不自作主张并拍（只受面板上限约束）。"""
        self.assertEqual(sk.auto_beat_cap(None), sk.PANEL_MAX)
        self.assertEqual(sk.auto_beat_cap(0), sk.PANEL_MAX)
        self.assertEqual(sk.auto_beat_cap("坏值"), sk.PANEL_MAX)
        self.assertEqual(len(sk.auto_beats({"id": 1, "video_prompt": self.VP})), 6)

    def test_board_grid_and_timeline_agree_on_the_same_total(self):
        """板格数与时间轴段数**同一份拍序列**——真源不许分叉（同 total 同拍数）。"""
        s = {"id": 1, "dur": 5.0, "video_prompt": self.VP}
        p = sk.board_prompt(s, total=5.0)
        m = re.search(r"恰好 (\d+) 个", p.splitlines()[0])
        n_panel = int(m.group(1))
        n_seg = len(re.findall(r"第[\d.]+-[\d.]+秒：", sk.timeline_text(s, total=5.0)))
        n_line = len(sk.panel_lines(s, total=5.0))
        self.assertEqual((n_panel, n_seg, n_line), (4, 4, 4),
                         "板格/时间轴段/拍表行必须逐个对齐")

    def test_total_is_threaded_at_every_call_site(self):
        """源级：拍序列真源 `effective_beats` 的每个调用点都必须带 total——
        漏传一处就是"板按 4 拍画、时间轴按 6 拍编"。"""
        import kinema
        root = Path(kinema.__file__).parent
        for rel in ("sketchboard.py", "cli.py", "studio/scanner.py", "pipeline/prompts.py"):
            src = (root / rel).read_text(encoding="utf-8")
            bad = re.findall(r"effective_beats\((?:shot|s)\)", src)
            self.assertFalse(bad, f"{rel} 有 {len(bad)} 处 effective_beats 漏传 total")

    def test_board_prompt_falls_back_to_auto(self):
        p = sk.board_prompt({"id": 1, "dur": 3, "video_prompt": "起身；迈步；回望"})
        self.assertIn("起身", p)
        self.assertIn("回望", p)

    def test_auto_timeline_replaces_the_prose_body(self):
        """自动拆拍=正文按句读切出来的，时间轴必须**替代**正文——同句发两遍
        既费 token 又让模型在两份一样的话里找差异。"""
        from kinema.pipeline import prompts
        shot = {"id": 1, "dur": 3, "video_prompt": "起身；迈步；回望"}
        vp = prompts.video_prompt(shot, native=True, sketch=True)
        self.assertIn("时间轴：", vp)
        self.assertEqual(vp.count("起身"), 1)
        # authored beats 时正文保留（拍级之外的补充信息仍有价值）
        shot2 = {"id": 1, "dur": 3, "video_prompt": "散文补充",
                 "sketch": {"beats": [{"action": "自定义拍"}]}}
        vp2 = prompts.video_prompt(shot2, native=True, sketch=True)
        self.assertIn("自定义拍", vp2)
        self.assertIn("散文补充", vp2)


# ============================================================ 二.七、时间对齐
class TestTimeAlignment(unittest.TestCase):
    """板/时间轴的秒段必须与 gen-video **实际请求秒数**同源（request_seconds）。
    dur 在 kenburns 折着停顿、dubbed 按配音实测——裸用 dur 画出来的板就是一份
    对不上片长的假节奏脚本（与读侧对称闸同一成因）。"""

    _SHOT = {"id": 1, "dur": 7.0, "video_prompt": "起身；迈步；回望"}

    def test_total_overrides_folded_dur_in_timeline_and_board(self):
        tl = sk.timeline_text(dict(self._SHOT), total=4.0)
        self.assertIn("第3-4秒", tl, "秒段按实际请求秒数 4s 铺（整秒），不按折过停顿的 dur=7")
        self.assertNotIn("7", tl)
        bp = sk.board_prompt(dict(self._SHOT), total=4.0)
        self.assertIn("第2.7-4.0秒", bp)
        # 未传 total 才回落 dur（离线场景没有更好的事实）
        self.assertIn("第5-7秒", sk.timeline_text(dict(self._SHOT)))

    def test_video_prompt_threads_sketch_total(self):
        from kinema.pipeline import prompts
        vp = prompts.video_prompt(dict(self._SHOT), native=True,
                                  sketch=True, sketch_total=4.0)
        self.assertIn("第3-4秒", vp)
        self.assertNotIn("7秒", vp)

    def test_stage_gen_video_passes_request_seconds_at_both_sites(self):
        """源级：dry-run 与真发共用唯一 Envelope 编译入口；该入口把
        request_seconds 作为 sketch_total 传入，避免两处自行拼装后漂移。"""
        src = (Path(__file__).resolve().parents[1]
               / "kinema" / "cli.py").read_text(encoding="utf-8")
        body = src.split("def stage_gen_video")[1].split("\ndef ")[0]
        self.assertEqual(body.count("sketch_total=dur"), 1)
        self.assertEqual(body.count("envelope = _video_envelope("), 2)

    def test_beats_coverage_matrix(self):
        def shot(ts):
            return {"id": 1, "sketch": {"beats": [
                {"action": f"a{i}", "t": t} for i, t in enumerate(ts)]}}
        self.assertIsNone(sk.beats_coverage(shot(["0-2s", "2-4s"]), 4.0), "连续铺满=无告警")
        self.assertIn("断档", sk.beats_coverage(shot(["0-1s", "2-4s"]), 4.0))
        self.assertIn("重叠", sk.beats_coverage(shot(["0-2.5s", "2-4s"]), 4.0))
        self.assertIn("实际请求秒数", sk.beats_coverage(shot(["0-2s", "2-7s"]), 4.0))
        self.assertIn("首拍", sk.beats_coverage(shot(["1-2s", "2-4s"]), 4.0))
        self.assertIsNone(sk.beats_coverage(shot(["开场", "收尾"]), 4.0),
                          "自由文本 t 判不了，宁可跳过不误报")
        self.assertIsNone(sk.beats_coverage({"id": 1}, 4.0))

    def test_panel_lines_match_the_board_prompt(self):
        """灯箱拍表与板上「面板内容」必须同一拼装——对照第 N 格即核对第 N 行。"""
        shot = {"id": 1, "dur": 3.0, "sketch": {"beats": [
            {"action": "起身", "camera": "缓推"}, {"action": "迈步"}, {"action": "回望"}]}}
        lines = sk.panel_lines(shot, total=3.0)
        bp = sk.board_prompt(shot, total=3.0)
        for ln in lines:
            self.assertIn(ln, bp)


# ============================================================ 三、互斥仲裁矩阵
class TestArbitration(unittest.TestCase):
    def test_explicit_guide_always_wins_even_into_an_empty_slot(self):
        """显式表态绝不静默回落——那正是「两个都配了互相干扰」的事故形态。"""
        self.assertEqual(sk.active_guide({"guide": "sketch", "previz": "pz.mp4"}), "sketch")
        self.assertEqual(sk.active_guide(
            {"guide": "previz", "sketch": {"beats": _beats(3)}}), "previz")
        self.assertEqual(sk.active_guide({"guide": "sketch"}), "sketch")

    def test_auto_arbitration_prefers_previz(self):
        both = {"previz": "pz.mp4", "sketch": {"beats": _beats(3), "sheet": "b.png"}}
        self.assertEqual(sk.active_guide(both), "previz",
                         "缺省 previz 优先（末帧/参考视频是像素级锚）")
        self.assertEqual(sk.active_guide({"last_frame_ref": "l.png"}), "previz")
        self.assertEqual(sk.active_guide({"sketch": {"beats": _beats(3)}}), "sketch")
        self.assertIsNone(sk.active_guide({"id": 1}))


# ============================================================ 四、gen-video 接线
class TestGenVideoWiring(_Base):
    def _run(self, project, **kw):
        from kinema import cli as cli_mod
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        # 「只喊一次」的闩锁是模块级的，测试进程内跨用例共享——不清掉，先跑的
        # 用例把消息占走，后跑用例的 assertNotIn 靠字母序巧合通过，守卫形同虚设。
        for latch in (cli_mod._warned_ski, cli_mod._warned_v2v,
                      cli_mod._warned_delta_skeleton, cli_mod._warned_prefix_fallback):
            latch.clear()
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=True, **kw)
        return buf.getvalue()

    def _sketch_project(self, *, board=True, guide="sketch", previz_too=False,
                        ref=False, **over):
        # 附板守卫缺省用 dubbed：附板只在参考媒体模式合法（native 首帧模式官方禁混，
        # 见 test_native_first_frame_mode_never_attaches_the_board）；previz/V2V 相关
        # 用例显式传 motion="native"（那两件套本就 native 专属）。
        # `ref=True` = 该镜显式开了「板作参考」（native 全能参考的 opt-in 开关）
        over.setdefault("motion", "dubbed")
        img = _png(self.tmp / "s1.png")
        s1 = {"id": 1, "dur": 5.0, "image": str(img), "video_prompt": "转身",
              "sketch": {"beats": _beats(3)}}
        if guide:
            s1["guide"] = guide
        if ref:
            s1["sketch"]["reference"] = True
        if board:
            s1["sketch"]["sheet"] = str(_png(self.tmp / "board.png"))
        if previz_too:
            s1["previz"] = str(self.tmp / "pz.mp4")
            s1["last_frame_ref"] = str(_png(self.tmp / "last.png"))
        return self._project([s1], **over)

    def test_native_opt_in_shot_gets_drift_check_on_every_shot(self):
        """板漂移体检对 native+「板作参考」通道同样生效，且是**逐镜**的——
        只查 dubbed 的话，开了 opt-in 的镜会把过期节奏的板发给模型且无任何告警；
        塞进「整章只喊一次」的闩锁里，第二个镜起就没人体检。"""
        img = _png(self.tmp / "s.png")

        def stale_shot(i):
            return {"id": i, "dur": 5.0, "image": str(img), "video_prompt": "转身",
                    "guide": "sketch",
                    "sketch": {"beats": _beats(3), "reference": True,
                               "sheet": str(_png(self.tmp / f"b{i}.png"))},
                    "gen": {"sketch": {"panels": 6, "seconds": 5.0, "dur_at": 5.0}}}
        out = self._run(self._project([stale_shot(1), stale_shot(2)], motion="native"))
        self.assertEqual(out.count("画的是旧拍序列"), 2,
                         "两镜都开了 opt-in 且板过期——漂移体检必须逐镜出声")

    def test_board_ships_as_reference_image_with_timeline(self):
        out = self._run(self._sketch_project())
        self.assertIn("参考图=分镜图+简笔板", out)
        self.assertIn("时间轴：", out)
        self.assertIn("绝不输出铅笔素描", out)

    def test_native_default_walks_reference_mode_with_board(self):
        """**缺省档就是全能参考**：native 章没开衔接时，板在盘即自动随请求附发
        （无需任何表态），分镜图/板全挂 reference_image、不发首/末帧、一镜一片；
        提示词换全能参考契约句 + 板职责声明；设定图没附时那半句绝不出现。"""
        out = self._run(self._sketch_project(motion="native"))
        self.assertIn("时间轴：", out)
        self.assertIn("全能参考(分镜图+简笔板·一镜一片)", out)
        self.assertIn("所附铅笔素描分镜板", out, "板真附上了就必须声明职责")
        self.assertIn("以 @图片1（本镜画面）", out, "契约句必须换全能参考版（官方 @引用语法）")
        self.assertNotIn("以所给首帧为画面基准", out, "缺省档没有首帧任务")
        self.assertNotIn("凡随请求附有对应设定图者", out,
                         "本用例无设定图——没附就不许声明")

    def test_chain_chapter_board_shot_stays_on_first_frame_without_opt_in(self):
        """衔接章（`frame_chain: true`）是对旧行为的整体 opt-in：板在盘也照走
        首帧任务（官方禁混参考图），板只当拍表——要板随请求发须逐镜
        `sketch.reference` 表态（代价是该镜成链上孤岛）。"""
        out = self._run(self._sketch_project(motion="native", frame_chain=True))
        self.assertIn("时间轴：", out, "衔接章里 beats 时间轴照发")
        self.assertNotIn("全能参考", out, "衔接章里没表态就不许切参考任务")
        self.assertNotIn("所附铅笔素描分镜板", out, "板没附就不许声明")
        self.assertIn("以所给首帧为画面基准", out, "衔接章仍是首帧驱动")
        self.assertIn("板只当拍表", out, "板在盘却附不出去要点名，不静默降级")

    def test_chain_chapter_opt_in_switches_to_reference_mode(self):
        """衔接章里显式开启后：板/分镜图全挂 reference_image、不发首/末帧，
        该镜是链上孤岛；提示词换全能参考契约句 + 板职责声明。"""
        out = self._run(self._sketch_project(motion="native", ref=True,
                                             frame_chain=True))
        self.assertIn("时间轴：", out)
        self.assertIn("全能参考(分镜图+简笔板·一镜一片)", out)
        self.assertIn("所附铅笔素描分镜板", out, "板真附上了就必须声明职责")
        self.assertIn("以 @图片1（本镜画面）", out, "契约句必须换全能参考版（官方 @引用语法）")
        self.assertNotIn("凡随请求附有对应设定图者", out,
                         "本用例无设定图——没附就不许声明")

    def test_chain_chapter_opt_in_without_a_board_changes_nothing(self):
        """衔接章里开了开关但没有板：照常首帧/衔接——开关本身不是「假装有板」的
        许可证（`reference_shot` 要求板真在盘）。"""
        out = self._run(self._sketch_project(motion="native", board=False, ref=True,
                                             frame_chain=True))
        self.assertNotIn("全能参考", out)
        self.assertNotIn("所附铅笔素描分镜板", out)
        self.assertIn("以所给首帧为画面基准", out)

    def test_native_sketch_without_board_keeps_timeline_no_board_claim(self):
        """无板的 sketch 镜：缺省档照走全能参考（分镜图作参考），时间轴纯文本照发
        （timeline prompting 独立成立），但绝不声明「所附分镜板」。"""
        out = self._run(self._sketch_project(motion="native", board=False))
        self.assertIn("时间轴：", out)
        self.assertIn("全能参考(分镜图·一镜一片)", out)
        self.assertNotIn("所附铅笔素描分镜板", out, "板没附就绝不许声明")

    def test_live_request_reference_mode_sends_refs_and_no_frames(self):
        """native 全能参考真发路径（mock 拦截）：ref_images=[板]、reference_only=True、
        无 last_frame——与 dry-run 判定同源（`_shot_plan` 唯一裁决点）。"""
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video.mock import MockVideoProvider
        project = self._sketch_project(motion="native", ref=True)
        store = ConfigStore.load(None)
        seen = {}

        def _gen(self, image, out_path, **kw):
            seen.update(kw)
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False, meta={})

        with mock.patch.object(MockVideoProvider, "generate", _gen), \
                contextlib.redirect_stdout(io.StringIO()):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True))
        board = sk.board_of(project.shots[0])
        self.assertEqual(seen.get("ref_images"), [board])
        self.assertTrue(seen.get("reference_only"), "适配器必须走参考生视频分支")
        self.assertIsNone(seen.get("last_frame"), "全能参考没有首/末帧槽")

    def test_scanner_chain_view_marks_reference_mode(self):
        """章节页链态与引擎同口径：开了「板作参考」的镜两侧都标断（自己不发末帧、
        上一镜也接不上它），没开的板镜照常显示衔接——页面上看到的链，就是
        gen-video 真会发的链。

        **判据全部走 `framechain.plan`**：`_chain_view` 只做展示。若它在这里再判
        一遍全能参考，就是同一条规则的第二份抄本；规则一扩到 V2V / previz 末帧，
        抄本立刻漏判。
        """
        from kinema.pipeline import framechain
        from kinema.studio import scanner
        b = _png(self.tmp / "b.png")
        shot = {"id": 1, "guide": "sketch",
                "sketch": {"sheet": str(b), "beats": _beats(2), "reference": True}}
        nxt = {"id": 2, "image": "x.png"}
        shots = [shot, nxt]

        def _view(i, on=True):
            plan = framechain.plan(shots, on)
            return scanner._chain_view(shots[i], *plan[id(shots[i])])

        self.assertEqual(_view(0), (None, framechain.BREAK_ZH["ref_mode"]))
        self.assertEqual(_view(0, on=False), (None, None), "不在衔接态就不挂措辞")
        # 上游端：孤岛镜接不住末帧，前一镜必须标断而不是「→ 镜1」
        shots.insert(0, {"id": 0, "image": "y.png"})
        self.assertEqual(_view(0), (None, framechain.BREAK_ZH["ref_next"]))
        plain = {"id": 1, "guide": "sketch", "sketch": {"sheet": str(b), "beats": _beats(2)}}
        shots = [plain, nxt]
        self.assertEqual(_view(0)[0], 2,
                         "没开开关的板镜留在链上——页面必须与引擎同口径")

    def test_board_leak_guard_sits_up_front_and_in_the_negatives(self):
        """**防泄漏两处同时说**——正文头部 + 负面串。

        只在正文说一次压不住：板随参考任务发出时没有 first_frame，
        开头几帧由几张参考图调和而来，板上的红蓝箭头会在那几帧里渗进画面；
        声明落在提示词中后段更压不住。国产视频模型对「避免出现：」
        这一串的服从度又显著更高。
        故：声明放在头部（紧跟画面基准句），同一批词补进负面串。
        """
        out = self._run(self._sketch_project(motion="native", ref=True))
        p = out.split("提示词：", 1)[1].split("\n", 1)[0]
        # ① 位置：板声明必须落在提示词前三分之一（早于时间轴与正文）
        pos = p.index("所附铅笔素描分镜板") / len(p)
        self.assertLess(pos, 0.34, f"板声明落在 {pos:.0%} 处——太靠后就压不住了")
        self.assertLess(p.index("所附铅笔素描分镜板"), p.index("时间轴："),
                        "板声明必须早于时间轴")
        # ② 负面串：地板词逐个在位，且排在防字地板之后（作者原话→防字→板地板）
        neg = p[p.index("避免出现"):]
        for w in ("标注箭头", "红蓝箭头", "分镜格线", "铅笔素描", "手写标注文字"):
            self.assertIn(w, neg, f"板地板缺词：{w}")
        self.assertLess(neg.index("字幕"), neg.index("标注箭头"),
                        "顺序：作者原话 → 防字地板 → 板地板")

    def test_no_board_no_board_negatives(self):
        """没附板就不许出现板地板——负面串里凭空多出「铅笔素描」会让模型
        以为这镜跟素描有什么关系。"""
        out = self._run(self._sketch_project(motion="native", board=False))
        self.assertNotIn("标注箭头", out)
        self.assertNotIn("分镜格线", out)

    def test_only_filters_shots_and_never_writes_partial_estimate(self):
        """gen-video --only 定向镜号（单镜重roll/断点补渲）：dry-run 只列点名的镜；
        **局部报价绝不写 cost_estimate**——单镜口径覆盖全片预估后，ledger 的
        「预估(video)」列从此对不上任何真实口径。全片 dry-run 照旧落盘。"""
        from kinema.project import Project
        img1, img2 = _png(self.tmp / "a.png"), _png(self.tmp / "b.png")
        project = self._project([
            {"id": 1, "dur": 5.0, "image": str(img1), "video_prompt": "转身"},
            {"id": 2, "dur": 4.0, "image": str(img2), "video_prompt": "抬头"}])
        out = self._run(project, only="2")
        self.assertIn("▸ 镜2", out)
        self.assertNotIn("▸ 镜1", out, "--only 2 不许再列镜1")
        self.assertIn("预估不入台账", out)
        self.assertNotIn("video", Project.load(project.path).data.get("cost_estimate") or {},
                         "--only 的局部报价不许覆盖全片预估")
        self.assertIn("没有匹配", self._run(project, only="9"), "点名不存在的镜要明说")
        self._run(project)   # 全片 dry-run：预估照旧入台账
        self.assertIn("video", Project.load(project.path).data.get("cost_estimate") or {})

    def test_sketch_suppresses_previz_last_frame_and_v2v(self):
        """互斥生命线：guide=sketch 的镜 previz 两件套一律不参与（V2V 是 native
        专属，故本用例走 native——板在 native 下不附，sketch 的生效面是时间轴）。"""
        out = self._run(self._sketch_project(previz_too=True, motion="native"), previz=True)
        self.assertNotIn("末帧=previz", out)
        self.assertNotIn("参考视频=previz", out)
        self.assertIn("时间轴：", out)

    def test_previz_guide_suppresses_sketch_entirely(self):
        out = self._run(self._sketch_project(guide="previz", previz_too=True,
                                             motion="native"))
        self.assertNotIn("时间轴：", out)
        self.assertNotIn("简笔分镜板", out.split("提示词")[1] if "提示词" in out else out)
        self.assertIn("末帧=previz", out)

    def test_timeline_without_board_has_no_attachment_claim(self):
        """板不在盘：时间轴照发（纯文本独立成立），但绝不声明「所附分镜板」。"""
        out = self._run(self._sketch_project(board=False))
        self.assertIn("时间轴：", out)
        self.assertNotIn("所附铅笔素描分镜板", out)
        self.assertNotIn("参考图=简笔分镜板", out)

    def test_capability_gate_blocks_the_attachment_not_the_timeline(self):
        from kinema.providers.video.mock import MockVideoProvider
        with mock.patch.object(MockVideoProvider, "supports_reference_images", False):
            out = self._run(self._sketch_project())
        self.assertNotIn("参考图=简笔分镜板", out)
        self.assertIn("时间轴：", out)
        self.assertIn("不支持额外参考图", out)

    def test_live_request_carries_the_board_in_ref_images(self):
        """真发路径（mock 拦截）：provider 收到 ref_images=[板]，与 dry-run 同源。"""
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video.mock import MockVideoProvider
        project = self._sketch_project()
        _wav(project.subdir("audio") / "shot_1.wav")   # dubbed 真发需配音在盘
        store = ConfigStore.load(None)
        seen = {}

        def _gen(self, image, out_path, **kw):
            seen.update(kw)
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False, meta={})

        with mock.patch.object(MockVideoProvider, "generate", _gen), \
                contextlib.redirect_stdout(io.StringIO()):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True))
        board = sk.board_of(project.shots[0])
        self.assertEqual(seen.get("ref_images"), [board])
        self.assertIn("时间轴：", seen.get("prompt", ""))
        self.assertIn("绝不输出铅笔素描", seen.get("prompt", ""))

    def test_success_clears_a_previous_failed_status(self):
        """断点续跑三态回正：撞过 400 的镜（status=failed）成功重发后必须回 done——
        若失败路径 mark(failed) 有写、成功路径不回写，分镜卡会永远挂着
        「失败」徽章。"""
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video.mock import MockVideoProvider
        project = self._sketch_project()
        project.shots[0]["status"] = "failed"          # 上一轮 400 留下的残留态
        _wav(project.subdir("audio") / "shot_1.wav")
        store = ConfigStore.load(None)

        def _gen(self, image, out_path, **kw):
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False, meta={})

        with mock.patch.object(MockVideoProvider, "generate", _gen), \
                contextlib.redirect_stdout(io.StringIO()):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True))
        self.assertEqual(project.shots[0].get("status"), "done")

    def test_gen_clip_snapshot_records_the_board(self):
        """快照留痕（复盘"这版为什么动得对"）——同样拦截 generate，绝不真渲。"""
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.base import VideoResult
        from kinema.providers.video.mock import MockVideoProvider
        project = self._sketch_project()
        _wav(project.subdir("audio") / "shot_1.wav")   # dubbed 真发需配音在盘
        store = ConfigStore.load(None)

        def _gen(self, image, out_path, **kw):
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False, meta={})

        with mock.patch.object(MockVideoProvider, "generate", _gen), \
                contextlib.redirect_stdout(io.StringIO()):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True))
        snap = (project.shots[0].get("gen") or {}).get("clip") or {}
        self.assertEqual(snap.get("sketch_board"), sk.board_of(project.shots[0]))
        self.assertEqual(snap["envelope"]["prompt"], snap["prompt"])
        self.assertRegex(snap["envelope"]["fingerprint"], r"^sha256:[0-9a-f]{64}$")


# ============================================================ 五、生成命令与登记边界
class _GenBase(_Base):
    """跑真 cmd_sketch_gen（mock provider）的公共装备——TestCmdSketchGen 与
    TestBoardDrift 共用（helper 上提而非子类继承：继承会把父类用例重跑一遍）。"""

    def _gen(self, project, *, only=None, force=False):
        from kinema import cli

        class _Args:
            pass

        a = _Args()
        a.project = str(project.path)
        a.chapter = None
        a.workspace = None
        a.config = None
        a.only = only
        a.profile = None
        a.force = force
        a.mock = True
        a.concurrency = 1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_sketch_gen(a)
        return buf.getvalue()

    def _p(self):
        img = _png(self.tmp / "s1.png")
        return self._project([
            {"id": 1, "dur": 5.0, "image": str(img), "video_prompt": "转身",
             "sketch": {"beats": _beats(3)}},
            {"id": 2, "dur": 4.0, "video_prompt": "抬头；远望"},
            {"id": 3, "kind": "transition", "dur": 1.0, "narration": ""},
            {"id": 4, "dur": 3.0},                       # 零运动设计：唯一该跳过的形态
        ])


class TestCmdSketchGen(_GenBase):

    def test_all_failures_exit_nonzero(self):
        """全灭必须以异常收场（returncode≠0）：Studio 按退出码映射 done/failed，
        exit 0 让前端弹绿字「生成完成」，而一张板都没落盘——下游 _warn_sketch 的
        告警全都要求板在盘，板没生成就一句都不喊，失败被静默掩盖。"""
        from kinema.errors import KinemaError
        from kinema.providers.image.mock import MockImageProvider
        project = self._p()
        with mock.patch.object(MockImageProvider, "generate",
                               side_effect=RuntimeError("401 Unauthorized")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KinemaError) as cm:
                self._gen(project)
        self.assertIn("失败", str(cm.exception))

    def test_board_lands_in_sketch_sheet_never_in_image_or_clip(self):
        project = self._p()
        img_before = project.shots[0].get("image")
        self._gen(project)
        from kinema.project import Project
        p2 = Project.load(project.path)
        s1 = p2.shots[0]
        sheet = sk.board_of(s1)
        self.assertTrue(sheet and Path(sheet).is_file())
        self.assertIn("_work/sketch/", str(sheet).replace("\\", "/"))
        self.assertEqual(s1.get("image"), img_before, "板绝不覆盖分镜图")
        self.assertIsNone(s1.get("clip"), "板绝不进成片位")
        self.assertEqual((s1.get("gen") or {}).get("sketch", {}).get("provider"), "mock")

    def test_detailed_prompt_generates_without_authored_beats(self):
        """用户主诉的形态：分镜词已足够详细 → 无需手写 beats 直接出板（自动拆拍留痕）。"""
        project = self._p()
        self._gen(project, only="2")
        from kinema.project import Project
        s2 = Project.load(project.path).shots[1]
        self.assertTrue(sk.board_of(s2))
        self.assertIs((s2.get("gen") or {})["sketch"]["auto"], True)

    def test_only_zero_material_shots_are_skipped_and_named(self):
        out = self._gen(self._p())
        self.assertIn("没有任何运动设计", out)
        self.assertIn("镜 4", out)
        self.assertNotIn("镜 2", out.split("已跳过")[1].split("\n")[0],
                         "有运动设计的镜绝不因缺 authored beats 被跳过")

    def test_idempotent_unless_force(self):
        project = self._p()
        self._gen(project)
        from kinema.project import Project
        v1 = (Project.load(project.path).shots[0].get("gen") or {})["sketch"]["version"]
        out = self._gen(Project.load(project.path))
        self.assertIn("跳过", out)
        p3 = Project.load(project.path)
        self.assertEqual((p3.shots[0].get("gen") or {})["sketch"]["version"], v1)
        self._gen(p3, force=True)
        p4 = Project.load(project.path)
        self.assertEqual((p4.shots[0].get("gen") or {})["sketch"]["version"], v1 + 1)

    def test_gen_records_seconds_and_scanner_flags_dur_drift(self):
        """gen.sketch 记录 seconds（按哪个总秒画的）与 dur_at（同量纲漂移判据）；
        板生成后 dur 一变，scanner 出 stale——秒段标签错位要有人喊。"""
        from kinema.studio.scanner import _sketch_view
        project = self._p()
        self._gen(project, only="1")
        from kinema.project import Project
        p2 = Project.load(project.path)
        gs = (p2.shots[0].get("gen") or {})["sketch"]
        self.assertEqual(gs["seconds"], 5.0)
        self.assertEqual(gs["dur_at"], 5.0)
        self.assertIsNone(_sketch_view(p2.shots[0]).get("stale"), "时长没变不许报")
        p2.shots[0]["dur"] = 8.0
        self.assertEqual(_sketch_view(p2.shots[0])["stale"], {"was": 5.0, "now": 8.0})

    def test_dubbed_board_uses_measured_audio_seconds(self):
        """dubbed 项目：板的秒段按配音实测铺（request_seconds 同源），不按折过的 dur。"""
        from kinema import cli as cli_mod
        img = _png(self.tmp / "s1.png")
        project = self._project([{"id": 1, "dur": 7.0, "image": str(img),
                                  "video_prompt": "起身；迈步"}], motion="dubbed")
        with mock.patch.object(cli_mod.voicecast, "request_seconds",
                               return_value=4.2):
            self._gen(project)
        from kinema.project import Project
        s1 = Project.load(project.path).shots[0]
        self.assertEqual((s1.get("gen") or {})["sketch"]["seconds"], 4.2)

    def test_clear_drops_the_mount_but_keeps_file_and_beats(self):
        project = self._p()
        self._gen(project)
        from kinema.project import Project
        p2 = Project.load(project.path)
        sheet = Path(sk.board_of(p2.shots[0]))
        r = sk.clear_board(p2, 1)
        self.assertIn("sketch.sheet", r["dropped"])
        p3 = Project.load(project.path)
        self.assertIsNone(sk.board_of(p3.shots[0]))
        self.assertEqual(len(sk.beats_of(p3.shots[0])), 3, "beats 是创作资产，clear 不动")
        self.assertTrue(sheet.is_file(), "产物文件保留")

    def test_kenburns_run_says_the_boards_will_not_reach_the_film(self):
        """kenburns 章节出板要点破「买来的东西不参与成片」。

        板与拍序列的唯一去处是 gen-video 请求（板绝不进 image/clip），而这一档
        根本不发；每张板却按分镜图同价计费。**只告警不拦**——「先排戏、再切
        native」是正当顺序。
        """
        project = self._p()
        project.data["motion"] = "kenburns"
        project.save()
        out = self._gen(project)
        self.assertIn("不发 gen-video", out)
        self.assertIn("计费", out, "告警必须说清这一档下板照样要钱")
        self.assertIn("motion 改成 native", out, "告警必须给得出可行动项")
        from kinema.project import Project
        self.assertTrue(sk.board_of(Project.load(project.path).shots[0]),
                        "只告警不拦：板照常生成")

    def test_video_motion_run_stays_silent_about_it(self):
        """native/dubbed 下不喊——板本来就参与请求，那句话在这里只是噪音。"""
        out = self._gen(self._p())          # _project 缺省 motion=native
        self.assertNotIn("不发 gen-video", out)


# ============================================================ 五.5、板漂移（拍序列指纹）
class TestBoardDrift(_GenBase):
    """dur_at 只盯得住时长漂移——提示词漂移（如镜面从城中村改成江南古镇后
    板仍显"新鲜"、gen-video 照附旧节奏板）要靠 `gen.sketch.sig` 拍序列指纹。
    判据唯一真源 `sketchboard.board_drift`：scanner / sketch list / gen-video
    告警三处消费同一份，绝不各写一遍——各写一份必然分叉。"""

    def test_beats_sig_tracks_prompt_and_beats_edits(self):
        s = {"id": 1, "dur": 5.0, "video_prompt": "转身；抬头"}
        sig0 = sk.beats_sig(s)
        self.assertEqual(sig0, sk.beats_sig(dict(s)), "同内容必须同指纹（确定性）")
        self.assertTrue(sig0.startswith("sha256:") and len(sig0) == 7 + 16,
                        "与血缘指纹同格式 sha256:<hex16>")
        self.assertNotEqual(sig0, sk.beats_sig({**s, "video_prompt": "转身；奔跑"}),
                            "自动拆拍来源字段改一个字，指纹必须变")
        authored = {**s, "sketch": {"beats": [{"t": "0-5s", "action": "转身"}]}}
        self.assertNotEqual(sk.beats_sig(authored),
                            sk.beats_sig({**authored,
                                          "sketch": {"beats": [{"t": "0-5s", "action": "疾退"}]}}),
                            "authored beats 改一拍，指纹必须变")

    def test_prompt_edit_after_gen_flags_stale_beats(self):
        from kinema.studio.scanner import _sketch_view
        project = self._p()
        self._gen(project, only="2")           # 镜2 走自动拆拍出板
        from kinema.project import Project
        p2 = Project.load(project.path)
        s2 = next(x for x in p2.shots if x["id"] == 2)
        self.assertIn("sig", (s2.get("gen") or {})["sketch"], "register 必须留拍序列指纹")
        self.assertIsNone(sk.board_drift(s2), "刚生成的板不许报漂移")
        self.assertNotIn("stale_beats", _sketch_view(s2) or {})
        s2["video_prompt"] = "低头；后退；跌坐"   # 板生成后改提示词=拍序列已变
        drift = sk.board_drift(s2)
        self.assertTrue(drift and drift.get("beats"), "提示词改了板没跟上必须报")
        self.assertTrue((_sketch_view(s2) or {}).get("stale_beats"),
                        "scanner 必须把拍序列漂移下发给前端")

    def test_legacy_boards_without_sig_never_false_alarm(self):
        """存量板既没 sig 也没 panels——不判拍序列漂移（宁可漏报不误报），
        dur 漂移照报。"""
        s = {"id": 1, "dur": 8.0, "video_prompt": "转身",
             "sketch": {"sheet": "x/board.png"},
             "gen": {"sketch": {"dur_at": 5.0}}}
        drift = sk.board_drift(s)
        self.assertTrue(drift and drift.get("dur"))
        self.assertNotIn("beats", drift)

    def test_panel_count_mismatch_catches_legacy_boards(self):
        """存量板（无 sig 字段）靠 `panels` 格数判漂移——语义最直白：板上真画了
        N 格而现在要发 M 拍。典型错位：存量 6 格板对上按时长收敛出的
        4 拍时间轴，不比就静默错位下去。"""
        vp = "他抬手；喷枪走弧；漆雾沉降；他眯眼核对；转过夹具；直起身收枪"
        base = {"id": 1, "dur": 5.0, "video_prompt": vp,
                "sketch": {"sheet": "x/board.png"}}
        stale = {**base, "gen": {"sketch": {"panels": 6, "seconds": 5.0, "dur_at": 5.0}}}
        self.assertTrue((sk.board_drift(stale) or {}).get("beats"),
                        "6 格板 vs 现在 4 拍必须报拍序列已变")
        fresh = {**base, "gen": {"sketch": {"panels": 4, "seconds": 5.0, "dur_at": 5.0}}}
        self.assertIsNone(sk.board_drift(fresh), "格数与拍数一致不许误报")
        # 坏值不炸（存量文档什么都可能有）
        bad = {**base, "gen": {"sketch": {"panels": "六", "seconds": 5.0, "dur_at": 5.0}}}
        self.assertIsNone(sk.board_drift(bad))

    def test_drift_judgment_is_single_sourced(self):
        """scanner/_sketch_view、cmd_sketch_list、gen-video 的 _warn_sketch 都必须
        消费 board_drift，且 scanner 不许再留一份本地 0.75 阈值判据。"""
        import inspect

        from kinema import cli as cli_mod
        from kinema.studio import scanner as scanner_mod
        sv = inspect.getsource(scanner_mod._sketch_view)
        self.assertIn("board_drift", sv)
        self.assertNotIn("0.75", sv, "阈值只准活在 sketchboard.DUR_DRIFT_TOL")
        self.assertIn("board_drift", inspect.getsource(cli_mod.cmd_sketch_list))
        cli_src = (Path(cli_mod.__file__)).read_text(encoding="utf-8")
        self.assertIn("拍序列已变", cli_src, "sketch list / gen-video 告警缺拍序列漂移面")

    def test_frontend_and_skill_ship_the_drift_and_timeline_lane(self):
        """chapter.js 消费 stale_beats；skill 文档与引擎的纯时间轴档措辞锁步
        （dry-run 打「分段时间轴(无板)」，SKILL 教条必须指着同一个核对位）。"""
        import kinema
        root = Path(kinema.__file__).parent
        js = (root / "studio_app" / "app" / "chapter.js").read_text(encoding="utf-8")
        self.assertIn("stale_beats", js)
        self.assertIn("拍序列已变", js)
        cli_src = (root / "cli.py").read_text(encoding="utf-8")
        self.assertIn("分段时间轴(无板)", cli_src, "dry-run 缺纯时间轴档标记")
        skill = root.parent.parent / ".claude" / "skills" / "kinema-sketchboard" / "SKILL.md"
        if skill.is_file():   # 引擎单独分发时无 .claude 目录，跳过
            doc = skill.read_text(encoding="utf-8")
            self.assertIn("规划优先", doc, "SKILL 缺「先秒级描述再画板」协议")
            self.assertIn("分段时间轴(无板)", doc, "SKILL 教条与 dry-run 标记脱钩")

    def test_gen_video_directive_buttons_are_wired(self):
        """SB 分镜卡「⧉ 视频指令」（单镜）与 FC 放映的两个整章出口——
        网页只**给纪律化指令**交 Claude Code（--only 定向 + 先 dry-run 审报价、
        经用户确认才真发），绝不直接起真发任务：gen-video 缺省串行且逐秒计费，
        烧钱决定必须留给人。

        整章出口按「要不要调用视频模型」分成并列两个，而不是一个笼统按钮内部分派：
        图片合成（分镜图 + Ken Burns，零视频成本）与模型合成（补齐缺失片段再合成）。
        两条路出的都是 output/ 正式成片，用户在按钮上就能选，不必读完长指令才发现
        还有零成本这条路。三处都走 openDirectiveDialog 指令台（诉求与指令合并后才进剪贴板）。"""
        import kinema
        js = (Path(kinema.__file__).parent / "studio_app" / "app"
              / "chapter.js").read_text(encoding="utf-8")
        self.assertIn('"⧉ 视频指令"', js)
        self.assertIn('"⧉ 图片合成指令"', js)
        self.assertIn("⧉ 模型合成指令", js)
        self.assertIn("openDirectiveDialog", js,
                      "指令按钮必须开指令台收用户诉求，不许一点即复制")
        # 断点续跑分派必须都在（少一条 AI 就无从判断续跑口径，重跑 gen-video 重复烧钱）
        self.assertIn("已全部就位", js, "全有片段时必须明说不用再调视频模型")
        self.assertIn("只补缺的那几镜", js, "缺几镜时必须明说只补缺的")
        self.assertIn("--motion a", js, "必须给出零视频成本的静图出片路径")
        self.assertIn("运行时覆盖不落盘", js, "须声明 --motion a 不会改坏章节 motion")
        self.assertIn("--only ${s.id} --dry-run", js,
                      "单镜指令必须 --only 定向且先 dry-run 审报价")
        self.assertIn("--approved-only", js, "章级指令缺草稿两段式正式档")
        self.assertIn("function copyAssembleStills", js)
        self.assertIn("function copyAnimateChapter", js)
        self.assertIn("--dry-run", js, "整章动态化必须先 dry-run 报价再真发")
        self.assertNotIn("/api/genvideo", js, "网页不许出现直接真发 gen-video 的端点")


# ============================================================ 六、Studio 层
class TestStudioLayer(_Base):
    def test_scanner_ships_sketch_guide_and_stats_from_the_single_source(self):
        from kinema.studio import scanner
        img = _png(self.tmp / "proj" / "p1" / "chapters" / "s1.png")
        board = _png(self.tmp / "proj" / "p1" / "chapters" / "ch01_work" / "sketch"
                     / "shot_1_board.png")
        self._project([
            {"id": 1, "dur": 5.0, "image": str(img), "guide": "sketch",
             "sketch": {"beats": _beats(3), "sheet": str(board)}},
            {"id": 2, "dur": 4.0}])
        (self.tmp / "proj" / "p1" / "project.json").write_text(
            json.dumps({"id": "p1", "title": "P1", "chapters": [{"id": "ch01"}]},
                       ensure_ascii=False), encoding="utf-8")
        d = scanner.chapter_detail(self.tmp / "proj", None, "p1", "ch01")
        s1 = next(x for x in d["shots"] if x["id"] == 1)
        self.assertEqual(s1["sketch"]["beats"], 3)
        self.assertTrue(s1["sketch"]["sheet"])
        self.assertEqual(s1["guide"], "sketch")
        self.assertEqual(s1["guide_active"], "sketch")
        s2 = next(x for x in d["shots"] if x["id"] == 2)
        self.assertIsNone(s2["sketch"])
        self.assertIsNone(s2["guide_active"])
        self.assertEqual(d["sketch_stats"], {"beats": 1, "boards": 1, "total": 2})

    def test_scanner_uses_active_guide_not_a_local_rederivation(self):
        """源级：仲裁判定必须 import 单一真源，scanner 内不许再写一份组合逻辑。"""
        src = (Path(__file__).resolve().parents[1]
               / "kinema" / "studio" / "scanner.py").read_text(encoding="utf-8")
        self.assertIn("_sk.active_guide(s)", src)

    def test_action_guide_writes_and_returns_the_arbitration(self):
        from kinema.studio import actions
        self._project([{"id": 1, "dur": 5.0, "previz": "pz.mp4",
                        "sketch": {"beats": _beats(3)}}])
        (self.tmp / "proj" / "p1" / "project.json").write_text(
            json.dumps({"id": "p1", "chapters": [{"id": "ch01"}]}), encoding="utf-8")
        r = actions.sketch_guide(self.tmp / "proj", "p1", "ch01",
                                 shot=1, guide="sketch")
        self.assertEqual(r["active"], "sketch")
        r2 = actions.sketch_guide(self.tmp / "proj", "p1", "ch01",
                                  shot=1, guide="auto")
        self.assertEqual(r2["guide"], None)
        self.assertEqual(r2["active"], "previz", "auto 仲裁回到 previz 优先")

    def test_generate_action_spawns_the_cli_with_locator_meta(self):
        from kinema.studio import actions, jobs
        self._project([{"id": 1, "dur": 5.0, "sketch": {"beats": _beats(3)}}])
        (self.tmp / "proj" / "p1" / "project.json").write_text(
            json.dumps({"id": "p1", "chapters": [{"id": "ch01"}]}), encoding="utf-8")
        with mock.patch.object(jobs, "spawn_cli", return_value="j1") as sp:
            r = actions.sketch_generate(self.tmp / "proj", "p1", "ch01",
                                        shots=[1], force=True)
        self.assertEqual(r["job"], "j1")
        argv = sp.call_args[0][0]
        self.assertEqual(argv[:4], ["sketch", "gen", "--chapter", "p1/ch01"])
        self.assertIn("--only", argv)
        self.assertIn("--force", argv)
        meta = sp.call_args[1]["meta"]
        self.assertEqual(meta["kind"], "sketch")
        self.assertEqual(meta["shots"], "1",
                         "meta.shots 是前端逐镜「生成中」格与刷新恢复的唯一依据")

    def test_regen_action_forces_with_note_through_the_cli_path(self):
        """灯箱重生成走 CLI 同一条生成路径（--force + --note），网页绝不另写拼装。"""
        from kinema.studio import actions, jobs
        self._project([{"id": 1, "dur": 5.0, "sketch": {"beats": _beats(3)}}])
        (self.tmp / "proj" / "p1" / "project.json").write_text(
            json.dumps({"id": "p1", "chapters": [{"id": "ch01"}]}), encoding="utf-8")
        with mock.patch.object(jobs, "spawn_cli", return_value="j2") as sp:
            r = actions.sketch_regen(self.tmp / "proj", "p1", "ch01",
                                     shot=1, note="第4格更大")
        self.assertEqual(r["job"], "j2")
        argv = sp.call_args[0][0]
        self.assertIn("--force", argv)
        self.assertIn("--note", argv)
        self.assertEqual(argv[argv.index("--note") + 1], "第4格更大")
        self.assertEqual(sp.call_args[1]["meta"]["shots"], "1")


# ============================================================ 七、前端源级契约
class TestFrontendContract(unittest.TestCase):
    def _src(self, name):
        return (ASSETS / "app" / name).read_text(encoding="utf-8")

    def test_chapter_view_wires_the_sketch_console(self):
        src = self._src("chapter.js")
        for token in ("sketchCard", "openSketchGenDialog", "/api/sketch/gen",
                      "/api/sketch/guide", "sketchDirective", "skmark",
                      "trackSketchJob", "SKGEN", "skb-tag", "skb-copy",
                      "sketchFixDirective", "skctx", "skb-go", "skb-grid"):
            self.assertIn(token, src)
        # 仲裁徽章只消费 scanner 下发的 guide_active，绝不自算 previz/sketch 优先级
        self.assertIn("s.guide_active", src)
        self.assertIn("sec-sketch", src)
        # 忙态恢复：章节级任务凭 meta.shots 清单重建逐镜「生成中」格（刷新不丢）
        self.assertIn("m.shots", src)
        # 时间对齐三件：灯箱拍表消费引擎下发的 lines（不自拼）·时长漂移角标·
        # SHOT 签点击跳转分镜卡（落点闪一下）
        self.assertIn("s.sketch.lines", src)
        self.assertIn("skb-stale", src)
        self.assertIn("sb-flash", src)

    def test_previz_desks_are_gated_by_uses_video_not_by_project_type(self):
        """运动预演两台的门只认 `uses_video`（= `Project.uses_seedance`）。

        3D 导演台与简笔分镜的产物只有一个去处——gen-video 请求，而 kenburns 根本
        不发。按 skill / 画风推是错的两层：motion 是**章节级**字段（同项目 ch01
        native、ch02 kenburns 是常态），而在前端另建一张「哪些类型要视频」的映射
        表就是第二真源，每加一个 skill 都得回来改。
        """
        src = self._src("chapter.js")
        body = src.split("function previzDesks(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("d.uses_video", body, "判据必须是服务端下发的 uses_video")
        for wrong in ("d.skill", "d.profile", "LABEL.skill", "LABEL.profile"):
            self.assertNotIn(wrong, body, f"门不许按 {wrong} 推——那是第二真源")
        # 收起而不是不渲染：盘上真有排完 previz 又改回 kenburns 的章节，
        # 硬藏会让已有产物在网页上彻底不可达
        self.assertIn("PVZ_OPEN", body, "折叠态要在重绘之间留存")
        self.assertIn("pvz-fold", body)
        self.assertIn("desks()", body, "展开路径必须复用同一份构造，不许各写一套")

    def test_audio_script_desk_is_not_gated_by_uses_video(self):
        """音频剧本与 motion **正交**：scored 在 kenburns 与 native 下都成立。

        跟着运动预演一起按 uses_video 收起就是收反了——纯 ffmpeg 合成的章节
        恰恰是整轨买断最典型的搭配。真正与它互斥的只有 dubbed（引擎硬闸）。
        """
        src = self._src("chapter.js")
        card = src.split("function audioScriptCard(", 1)[1].split("\nfunction ", 1)[0]
        self.assertNotIn("uses_video", card,
                         "音频剧本台不许挂在视频判据上")
        self.assertIn('d.motion === "dubbed"', card, "互斥的那一档是 dubbed")

    def test_lightbox_regen_goes_through_the_backend_locator(self):
        """灯箱重生成按钮消费 skctx 定位名片直达后端——不靠用户复制粘贴指令。"""
        src = self._src("widgets.js")
        self.assertIn("it.skctx", src)
        self.assertIn("/api/sketch/regen", src)
        self.assertIn("trackSketchJob", src, "重生启动即入忙态账本（板条出「生成中」格）")
        # 灯箱内也有「⧉ 改板指令」——与格签同一份文本（skctx.directive 单一来源）
        # 且同一个指令台（openSketchFixDialog），两处绝不各写一套交互
        self.assertIn("it.skctx.directive", src)
        self.assertIn("⧉ 改板指令", src)
        self.assertIn("openSketchFixDialog(it.skctx)", src)
        chapter = self._src("chapter.js")
        self.assertIn("SKB_COPY_SVG", chapter, "格签用矢量复制图标（字形在缩略上太糊）")
        self.assertIn("directive: sketchFixDirective(d, s)", chapter,
                      "指令文本随板项下发，格签与灯箱绝不各拼一版")
        self.assertIn("function openSketchFixDialog", chapter)

    def test_busy_state_repaints_and_survives_concurrent_jobs(self):
        """**登记忙态即重绘**：灯箱点「↻ 重新生成」关掉弹层后界面不能毫无动静。

        忙态是渲染的一部分（板格子渲染时才读 SKGEN），"同一件事在两处各写一半"
        必然漏：重绘若靠调用方各自写 softRefresh，哪条路忘了写，就只在内存 Map
        里改了个值、界面零反应。故重绘收进 `trackSketchJob` 自己，调用方不必记。

        同时钉住**多任务并存**：批量生成没跑完时又从灯箱重生某镜是正常操作，
        `if (SKGEN.has(key)) return` 式的按章节去重会把第二个任务整个忽略
        （忙态不记也不重绘）。
        """
        chapter = self._src("chapter.js")
        body = chapter.split("function trackSketchJob(")[1].split("\nfunction ")[0]
        self.assertIn("softRefresh(pid, cid)", body,
                      "登记忙态后必须重绘，否则只是内存改了个 Map、界面零反应")
        self.assertIn("byJob.has(jid)", body, "幂等只按任务 id（页面对账每次进视图都会调）")
        self.assertNotIn("if (SKGEN.has(key)) return", body,
                         "按章节去重会把并发的第二个任务整个吞掉")
        # 板条渲染取全部在途任务的镜号并集
        self.assertIn("gen.values()", chapter, "在跑的镜=本章全部在途任务的并集")
        # 调用方不另写 softRefresh（重绘统一由 trackSketchJob 发起）
        dlg = chapter.split("/api/sketch/gen")[1][:400]
        self.assertNotIn("softRefresh(d.project", dlg,
                         "重绘已收进 trackSketchJob，调用方不许再各写一遍")

    def test_styles_exist(self):
        css = (ASSETS / "style.css").read_text(encoding="utf-8")
        for cls in (".skmark", ".skb-card", ".skb-strip", ".skb-cell",
                    ".skb-tag", ".skb-copy", ".skb-dlg", ".skb-go", ".skb-grid",
                    ".skb-stale", ".sb-flash"):
            self.assertIn(cls, css)
        # 灯箱拍表按行排（pre-line），单行文案不受影响
        self.assertIn("pre-line", css.split(".lb-cap")[1].split("}")[0])
        # 画面左下角标（◉意见/◈预演/▸动态片段/▦简笔）由 .vmarks flex 列容器统一堆叠——
        # 任意组合不重叠不悬空（兄弟选择器逐对偏移只覆盖得了固定组合）
        self.assertIn(".vmarks", css)
        js = (ASSETS / "app" / "chapter.js").read_text(encoding="utf-8")
        self.assertIn('"vmarks"', js, "分镜卡角标必须进堆栈容器")
        self.assertIn("clip-play", js, "动态片段播放入口必须在画面内（紧贴 ▦ 简笔上方）")
        # 主按钮=描边+软填+掠光（与 .dzc-go 同一套语言），不另造实心强调
        self.assertIn(".skb-go::after", css)
        self.assertIn(".skb-go:hover", css)


# ============================================================ 七.五、参考孤岛判据
class TestReferenceShot(unittest.TestCase):
    """`reference_shot` 静态判据（**显式开启** × native × guide=sketch × 板在盘）——
    它是**衔接章里的孤岛判据**（framechain.island 消费），不是全能参考的入口：
    全能参考已是 native 缺省档（`cli._shot_plan` 按「非衔接参与」判），本表态只在
    章级衔接开启的章里把某镜强制拉回参考任务。"""

    def _shot(self, td, *, ref=True, **over):
        s = {"id": 1, "guide": "sketch",
             "sketch": {"beats": _beats(2), "sheet": str(_png(Path(td) / "b.png"))}}
        if ref:
            s["sketch"]["reference"] = True
        s.update(over)
        return s

    def test_opt_in_is_required(self):
        """**缺省不表态**——板在盘、guide=sketch、motion=native 全中也不算显式孤岛。

        衔接章里这条开关守的是接缝：走参考任务=本镜既不收也不发末帧=两处接缝改软切。
        表态必须显式，framechain 才能把「用户点名的孤岛」与「缺省档的参考镜」分开
        ——前者要在衔接章里断链补缝，后者根本没有链。
        """
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(sk.reference_shot(self._shot(td, ref=False), True))
            self.assertTrue(sk.reference_shot(self._shot(td), True))

    def test_predicate_requires_native_guide_and_board_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            shot = self._shot(td)
            self.assertTrue(sk.reference_shot(shot, True))
            self.assertFalse(sk.reference_shot(shot, False),
                             "dubbed 不经此路——参考媒体模式本就能附板")
            self.assertFalse(sk.reference_shot({**shot, "guide": "previz"}, True),
                             "guide 仲裁恒赢——previz 镜不走全能参考")
            missing = {"id": 2, "guide": "sketch",
                       "sketch": {"beats": _beats(2), "reference": True,
                                  "sheet": str(Path(td) / "nope.png")}}
            self.assertFalse(sk.reference_shot(missing, True),
                             "板不在盘 → 留在首帧/衔接路（没有「传不过去」的问题）")

    def test_set_reference_toggles_and_cleans_up(self):
        """关闭走纯减法：不留 `false`、不造空 `sketch` 壳——文档里多出来的键
        会让读 JSON 的人猜「显式关过」还是「从没开过」（`sketch ref --all
        --state off` 扫过全章时，没配过 sketch 的镜一镜留一个空壳）。"""
        s = {"id": 1}
        sk.set_reference(s, True)
        self.assertTrue(s["sketch"]["reference"])
        self.assertTrue(sk.reference_opt_in(s))
        sk.set_reference(s, False)
        self.assertNotIn("sketch", s, "开→关须回到「从没开过」的原样")
        self.assertFalse(sk.reference_opt_in(s))
        bare = {"id": 2}
        sk.set_reference(bare, False)
        self.assertNotIn("sketch", bare, "对没配过 sketch 的镜关闭不许造空壳")
        keeper = {"id": 3, "sketch": {"beats": [{"action": "转身"}], "reference": True}}
        sk.set_reference(keeper, False)
        self.assertNotIn("reference", keeper["sketch"])
        self.assertEqual(len(keeper["sketch"]["beats"]), 1, "减法只减开关，不碰 beats/板")


# ============================================================ 八、内置样板图
class TestTemplateAsset(unittest.TestCase):
    def test_templates_are_bundled(self):
        """三张同版式样板随仓库分发（单人动作/双人对话/双人战斗，与 fonts 同款
        内置资产）——多示例对比让「版式是不变量、内容是变量」自证，
        它们是「100% 复刻版式」的锚。"""
        got = sk.templates()
        self.assertEqual([p.name for p in sk.TEMPLATE_PATHS], [p.name for p in got],
                         "板样板清单与在盘文件不一致")
        for p in got:
            self.assertGreater(p.stat().st_size, 10_000, f"{p.name} 是空壳")
            self.assertIn("kinema/assets/blueprints", str(p).replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
