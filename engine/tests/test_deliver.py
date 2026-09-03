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

"""kinema.deliver.build_srt 守卫：外挂 SRT 必须与烧录字幕**严格同源**——
事件走 shot_events（多角色镜 lines[] 逐句成条）、语言随 sub_cfg（subtitle 块
lang 覆盖顶层）。同源断言必须拿真渲的 ASS 与 SRT 逐条比对——让 build_srt
自己调文本函数再 assertIn 等于自证，烧录侧换了真源它也不会红。"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from kinema.deliver import build_srt
from kinema.project import Project


def _proj(shots, lang=None):
    data = {"motion": "kenburns", "shots": shots}
    if lang:
        data["subtitle_lang"] = lang
    return Project("x.json", data)


class TestOutDirOwnership(unittest.TestCase):
    """`--out` 指向的目录归用户所有：非空即拒，绝不清空；只有引擎自建的缺省目录才重建。"""

    def test_nonempty_out_dir_is_refused_untouched(self):
        from kinema.deliver import build_delivery
        from kinema.errors import ProjectError
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            final = root / "final.mp4"
            final.write_bytes(b"mp4")
            cf = root / "p" / "chapters" / "ch01.json"
            cf.parent.mkdir(parents=True)
            cf.write_text('{"id": "ch01", "platform": ["douyin"], "shots": [], '
                          '"output": {"16:9": "%s"}}' % final, encoding="utf-8")
            victim = root / "victim"
            (victim / "sub").mkdir(parents=True)
            (victim / "precious.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ProjectError, "非空"):
                build_delivery(Project.load(cf), out_dir=victim, make_zip=False)
            self.assertEqual((victim / "precious.txt").read_text(encoding="utf-8"), "keep")
            self.assertTrue((victim / "sub").is_dir())


class TestBuildSrt(unittest.TestCase):
    def test_narration_wins_over_caption(self):
        # 两字段都填 → SRT 取 narration（与烧录一致），caption 不得抢位
        srt = build_srt(_proj([
            {"id": 1, "dur": 2.0, "narration": "他念的这句", "caption": "画面补位那句"}]))
        self.assertIn("他念的这句", srt)
        self.assertNotIn("画面补位那句", srt)

    def test_caption_fills_silent_shot(self):
        # 无 narration 的纯画面镜 → caption 补位（不留空窗）
        srt = build_srt(_proj([{"id": 1, "dur": 2.0, "caption": "三年后·深夜"}]))
        self.assertIn("三年后·深夜", srt)

    def test_en_lang_emits_english(self):
        # subtitle_lang=en → SRT 出英文位（而非永远吐中文）
        srt = build_srt(_proj(
            [{"id": 1, "dur": 2.0, "narration": "少年出发了",
              "narration_en": "The boy set off."}], lang="en"))
        self.assertIn("The boy set off.", srt)
        self.assertNotIn("少年出发了", srt)

    def test_both_lang_stacks_zh_and_en(self):
        # both → 一条 cue 内中文主行 + 英文副行两行
        srt = build_srt(_proj(
            [{"id": 1, "dur": 2.0, "narration": "少年出发了",
              "narration_en": "The boy set off."}], lang="both"))
        self.assertIn("少年出发了\nThe boy set off.", srt)

    def test_empty_shot_skipped(self):
        # 无文案镜（如转场镜空 narration）不产 cue，序号不空跳
        srt = build_srt(_proj([
            {"id": 1, "dur": 2.0, "narration": "第一句"},
            {"id": 2, "dur": 1.6, "kind": "transition", "narration": ""},
            {"id": 3, "dur": 2.0, "narration": "第二句"}]))
        # 只两条 cue，序号连续 1、2（不因空镜产生 3）
        self.assertIn("1\n", srt)
        self.assertIn("2\n", srt)
        self.assertNotIn("3\n", srt)
        self.assertEqual(srt.count("-->"), 2)

    def test_multi_speaker_lines_emit_per_line_cues(self):
        # lines[] 镜逐句一条 cue（跟着声音换人），按各句实测时长切分
        srt = build_srt(_proj([{"id": 1, "dur": 6.0, "lines": [
            {"speaker": "甲", "text": "你来了。", "dur": 2.0},
            {"speaker": "乙", "text": "我来了。", "dur": 2.0},
            {"speaker": "甲", "text": "坐吧。", "dur": 2.0}]}]))
        self.assertEqual(srt.count("-->"), 3)
        self.assertIn("你来了。", srt)
        self.assertIn("我来了。", srt)
        self.assertIn("00:00:02,000 --> 00:00:04,000", srt)   # 第二句窗口

    def test_lang_follows_sub_cfg_block_override(self):
        """subtitle 块显式 lang 覆盖顶层 subtitle_lang（与烧录同判）：调用方把
        sub_cfg 的解析结果传进来——只认顶层的话，块里写 en 的项目成片烧英文、
        外挂 SRT 却出中文。"""
        from kinema.pipeline.subtitle import sub_cfg

        class _Store:
            def profile(self, name):
                return {}

        p = _proj([{"id": 1, "dur": 2.0, "narration": "少年出发了",
                    "narration_en": "The boy set off."}], lang="zh")
        p.data["subtitle"] = {"lang": "en"}
        lang = sub_cfg(_Store(), p).get("lang")
        self.assertEqual(lang, "en")
        srt = build_srt(p, lang=lang)
        self.assertIn("The boy set off.", srt)
        self.assertNotIn("少年出发了", srt)

    @staticmethod
    def _srt_cues(srt: str) -> list[tuple[float, float, str]]:
        out = []
        for block in srt.strip().split("\n\n"):
            rows = block.split("\n")
            m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", rows[1])
            out.append((int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000,
                        int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000,
                        "".join(rows[2:])))
        return out

    @staticmethod
    def _ass_events(path: Path) -> list[tuple[float, float, str]]:
        out = []
        for ln in path.read_text(encoding="utf-8").splitlines():
            if not ln.startswith("Dialogue:"):
                continue
            f = ln.split(",", 9)
            ts = [f[1], f[2]]
            def _sec(t):
                h, m, s = t.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            text = re.sub(r"\{[^}]*\}", "", f[9]).replace("\\N", "")
            out.append((_sec(ts[0]), _sec(ts[1]), text))
        return out

    def test_same_source_as_burned(self):
        """同源不变量：**真渲一份烧录 ASS，与 SRT 逐条比对**事件数、起止时间码
        与文本。多角色镜（lines[]）必须两边都逐句成条——SRT 侧走 pick_texts
        的话整镜返空被跳过，烧录 4 条外挂只剩 1 条、零告警。"""
        from kinema.pipeline.subtitle import build_from_timeline
        shots = [
            {"id": 1, "dur": 2.0, "narration": "开场旁白"},
            {"id": 2, "dur": 6.0, "lines": [
                {"speaker": "甲", "text": "你来了。", "dur": 2.0},
                {"speaker": "乙", "text": "我来了。", "dur": 2.0},
                {"speaker": "甲", "text": "坐吧。", "dur": 2.0}]},
            {"id": 3, "dur": 2.0, "caption": "三年后·深夜"},
        ]
        p = _proj(shots, lang="zh")
        cues = self._srt_cues(build_srt(p))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "burn.ass"
            build_from_timeline(list(p.timeline()), out,
                                opts={"speaker_tag": False})
            events = self._ass_events(out)
        self.assertEqual(len(cues), len(events), "SRT 与烧录 ASS 的事件数必须一致")
        for (s0, e0, txt0), (s1, e1, txt1) in zip(cues, events):
            self.assertAlmostEqual(s0, s1, delta=0.02)
            self.assertAlmostEqual(e0, e1, delta=0.02)
            self.assertEqual(txt0, txt1)


if __name__ == "__main__":
    unittest.main()
