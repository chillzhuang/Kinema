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

"""剧本改编模块单元测试（守卫地图：adaptation.py / workspace.py Series 改编承接）。

两组守卫：
  · TrackA —— 确定性结构切分（Fountain/.fdx 解析·小说章标切分·窗口化·指纹格式）。
  · SeriesAdapt —— 引擎承接的三条契约：
      scaffold 显式 cid（章号==集号·跳集不错位·回填 chapter_id）
      upsert_chapter_outline 幂等（重跑不炸·只写 outline 不碰 shots/review）
      upsert_entities 合并不覆盖（保人工 voice/comments·keywords 取并集）
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from kinema import adaptation as A
from kinema.errors import ProjectError
from tests.support import LocalBackendEnv


# ============================================================================
# TrackA —— 纯 Python 确定性切分
# ============================================================================
class TestTrackA(unittest.TestCase):
    FOUNTAIN = (
        "INT. OFFICE - NIGHT\n\n"
        "A young man collapses over the keyboard.\n\n"
        "LIN DENG\nI remember everything now.\n\n"
        "EXT. STREET - DAY\n\n"
        "A crowd gathers.\n\n"
        "VILLAIN (V.O.)\nToo late.\n"
    )
    NOVEL = ("前情提要，交代背景。\n\n"
             "第一章 猝死\n凌晨三点，林深倒在第 38 版改稿上。\n\n"
             "第二章 重生\n他睁开眼，回到提案前夜。\n\n"
             "第三章 一稿过\n甲方起立鼓掌。\n")

    def test_detect_format_screenplay_and_novel(self):
        self.assertEqual(A.detect_format(self.FOUNTAIN), "screenplay")   # ≥2 场景头
        self.assertEqual(A.detect_format(self.NOVEL), "novel")
        # 后缀强制判剧本，即便正文无场景头
        self.assertEqual(A.detect_format("随便一段散文", filename="x.fdx"), "screenplay")
        self.assertEqual(A.detect_format("随便一段散文", filename="x.fountain"), "screenplay")

    def test_fountain_scene_heading_tri_tuple_and_characters(self):
        scenes = A.parse_screenplay(self.FOUNTAIN)
        self.assertEqual(len(scenes), 2)
        s0 = scenes[0]
        self.assertEqual((s0["int_ext"], s0["location"], s0["time_of_day"]),
                         ("INT", "OFFICE", "NIGHT"))
        self.assertEqual(s0["characters"], ["LIN DENG"])       # 角色 cue 识别
        self.assertEqual(scenes[1]["int_ext"], "EXT")
        self.assertEqual(scenes[1]["characters"], ["VILLAIN"])  # 去 (V.O.) 括注
        # char 偏移单调递增、闭合
        self.assertLess(s0["char_start"], s0["char_end"])
        self.assertEqual(s0["char_end"], scenes[1]["char_start"])

    def test_fdx_parse(self):
        fdx = (
            '<?xml version="1.0"?>\n<FinalDraft DocumentType="Script">\n<Content>\n'
            '<Paragraph Type="Scene Heading"><Text>INT. LAB - DAY</Text></Paragraph>\n'
            '<Paragraph Type="Action"><Text>Machines hum.</Text></Paragraph>\n'
            '<Paragraph Type="Character"><Text>DOCTOR</Text></Paragraph>\n'
            '<Paragraph Type="Dialogue"><Text>It works.</Text></Paragraph>\n'
            '</Content>\n</FinalDraft>\n')
        self.assertEqual(A.detect_format(fdx), "screenplay")
        scenes = A.parse_screenplay(fdx, filename="s.fdx")
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0]["location"], "LAB")
        self.assertEqual(scenes[0]["time_of_day"], "DAY")
        self.assertEqual(scenes[0]["characters"], ["DOCTOR"])

    def test_split_novel_chapter_markers(self):
        units = A.split_novel(self.NOVEL)
        titles = [u["title"] for u in units]
        self.assertEqual(titles[0], "序")                       # 章标前正文=序块
        self.assertTrue(titles[1].startswith("第一章"))
        self.assertEqual([u["index"] for u in units], [0, 1, 2, 3])
        # 每块 char 范围闭合且单调
        for u in units:
            self.assertLess(u["char_start"], u["char_end"])
            self.assertEqual(self.NOVEL[u["char_start"]:u["char_start"] + 1],
                             self.NOVEL[u["char_start"]])       # 偏移落在原文内

    def test_split_novel_no_marker_returns_empty(self):
        self.assertEqual(A.split_novel("一段没有任何章节标记的散文。" * 5), [])

    def test_split_novel_prose_starting_with_marker_words_not_split(self):
        # 行首撞词的叙述句不许当章标——照行首匹配会把长篇切出成批误切块：
        # 「第一节做了四个小时。」（杖的节）「楔子只能进不能退。」（楔这件形态）。
        # 判别特征=真章标从不带句读
        text = ("第1章 渊启日\n正文甲。\n第一节做了四个小时。\n"
                "楔子只能进不能退。\n第二节。五米二。\n"
                "第2章 三十秒\n正文乙。\n")
        units = A.split_novel(text)
        self.assertEqual([u["title"] for u in units],
                         ["第1章 渊启日", "第2章 三十秒"])

    def test_split_novel_bare_prologue_still_splits(self):
        # 无句读的真「楔子」标题行仍然要认
        text = "楔子\n引子正文。\n第1章 开端\n正文。\n"
        units = A.split_novel(text)
        self.assertEqual([u["title"] for u in units], ["楔子", "第1章 开端"])

    def test_window_text_paragraph_aware(self):
        text = "\n\n".join(f"第{i}段。" * 20 for i in range(40))
        wins = A.window_text(text, size=400, overlap=50)
        self.assertGreater(len(wins), 1)
        for w in wins:
            self.assertGreaterEqual(w["char_start"], 0)
            self.assertLessEqual(w["char_end"], len(text))
            self.assertLess(w["char_start"], w["char_end"])
        # overlap 真实生效：第二窗起点回退在第一窗终点之前（有意重叠，非死代码）
        self.assertLess(wins[1]["char_start"], wins[0]["char_end"])
        self.assertGreaterEqual(wins[1]["char_start"], wins[0]["char_end"] - 50)

    def test_decode_source_gbk_bom_utf8(self):
        # 中文小说常见 GBK/GB18030：不得静默替换成 U+FFFD
        text, enc = A.decode_source("第一章 重生\n他睁开眼。".encode("gb18030"))
        self.assertEqual(enc, "gb18030")
        self.assertIn("第一章", text)
        self.assertNotIn("�", text)
        # UTF-8 BOM 被 utf-8-sig 剥掉
        t2, e2 = A.decode_source(b"\xef\xbb\xbf" + "第一章".encode("utf-8"))
        self.assertEqual((t2, e2), ("第一章", "utf-8-sig"))

    def test_decode_source_autodetect_big5_and_utf16(self):
        # Big5 繁体：gb18030 会静默 mojibake(满屏 PUA)，打分择优应选 big5 得干净繁体
        tw = "第一章 重生\n他睜開眼，回到提案前夜，這是嶄新的開始。"
        text, enc = A.decode_source(tw.encode("big5"))
        self.assertIn(enc, ("big5", "big5hkscs"))
        self.assertIn("睜開眼", text)                       # 正确繁体，非 mojibake
        self.assertNotIn("�", text)
        # 无 BOM UTF-16LE：靠打分识别（错编码会满屏乱码，得分远低）
        t2, e2 = A.decode_source("少年提剑而立，山雨欲来。".encode("utf-16-le"))
        self.assertEqual(e2, "utf-16-le")
        self.assertIn("提剑", t2)
        self.assertNotIn("�", t2)

    def test_undecodable_ratio_flags_mojibake(self):
        # 干净中文≈0；U+FFFD 与私用区 PUA（编码误判产物）计为乱码
        self.assertEqual(A.undecodable_ratio("干净中文一段。" * 10), 0.0)
        self.assertEqual(A.undecodable_ratio(""), 0.0)
        mojibake = ("第一章 重生。" * 30).encode("big5").decode("gb18030")
        self.assertGreater(A.undecodable_ratio(mojibake), 0.25)
        self.assertGreater(A.undecodable_ratio("正文" + "�" * 8), 0.5)

    def test_fountain_crlf_offsets_correct(self):
        # CRLF 源：偏移不得逐行左移（keepends 计算）
        scr = "INT. A - DAY\r\n\r\nAction.\r\n\r\nINT. B - NIGHT\r\n\r\nMore.\r\n"
        scenes = A.parse_screenplay(scr)
        self.assertEqual(len(scenes), 2)
        for sc in scenes:
            self.assertEqual(scr[sc["char_start"]:sc["char_start"] + len(sc["heading"])],
                             sc["heading"])

    def test_fingerprint_format_matches_lineage(self):
        fp = A.text_fingerprint("hello world")
        self.assertTrue(fp.startswith("sha256:"))
        self.assertRegex(fp, r"^sha256:[0-9a-f]{16}$")

    def test_structural_digest_switches_by_kind(self):
        d_novel = A.structural_digest(self.NOVEL, "novel")
        self.assertEqual(d_novel["segment_kind"], "chapter")
        self.assertEqual(d_novel["n_segments"], len(d_novel["segments"]))
        d_scr = A.structural_digest(self.FOUNTAIN, "screenplay")
        self.assertEqual(d_scr["segment_kind"], "scene")
        # 无章标小说回落窗口化
        d_win = A.structural_digest("无标记散文。" * 500, "novel")
        self.assertEqual(d_win["segment_kind"], "window")


# ============================================================================
# SeriesAdapt —— 引擎承接的三条契约
# ============================================================================
class SeriesCase(unittest.TestCase):
    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("改编", pid="adp", profile="hd2d")

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _eps(self, *nos):
        self.s.data["episodes"] = [
            {"no": n, "title": f"第{n}集", "logline": f"L{n}", "open_hook": f"H{n}",
             "core_event": f"C{n}", "end_hook": f"E{n}"} for n in nos]
        self.s.save()


class TestSourceIngest(SeriesCase):
    def test_source_dir_and_set_source(self):
        d = self.s.source_dir
        self.assertTrue(d.is_dir() and d.name == "source")
        self.s.set_source(kind="novel", file="source/raw.txt", chars=100,
                          sha256="sha256:abc0000000000000")
        src = self.ws.get_project("adp").data["source"]
        self.assertEqual(src["kind"], "novel")
        self.assertEqual(src["chars"], 100)
        self.assertTrue(src["ingested_at"])

    def test_ingest_source_writes_files_and_pointer(self):
        # CLI adapt import 与 Studio 上传共用此路径；GBK 源不得损坏
        data = "第一章 起\n正文一。\n\n第二章 承\n正文二。\n".encode("gb18030")
        r = self.s.ingest_source(filename="n.txt", data=data)
        self.assertEqual((r["kind"], r["encoding"], r["segment_kind"]),
                         ("novel", "gb18030", "chapter"))
        raw = (self.s.source_dir / "raw.txt").read_text(encoding="utf-8")
        self.assertIn("第一章", raw)
        self.assertNotIn("�", raw)                         # 无 U+FFFD 损坏
        seg = json.loads((self.s.source_dir / "segments.json").read_text(encoding="utf-8"))
        self.assertEqual(seg["n_segments"], r["n_segments"])
        src = self.ws.get_project("adp").data["source"]
        self.assertEqual(src["kind"], "novel")
        self.assertTrue(src["sha256"].startswith("sha256:"))
        self.assertEqual(src["file"], "source/raw.txt")

    def test_ingest_empty_raises(self):
        with self.assertRaises(ProjectError):
            self.s.ingest_source(filename="e.txt", data=b"   \n  \t")

    def test_ingest_autoconverts_big5_to_utf8(self):
        # Big5/繁体源自动识别并转 UTF-8 落盘，不拒收——raw.txt 恒为干净 UTF-8
        big5 = ("第一章 重生\n他睜開眼，回到提案前夜，窗外細雨綿綿。" * 20).encode("big5")
        r = self.s.ingest_source(filename="tw.txt", data=big5)
        self.assertIn(r["encoding"], ("big5", "big5hkscs"))
        raw = (self.s.source_dir / "raw.txt").read_text(encoding="utf-8")
        self.assertIn("睜開眼", raw)                        # 繁体正确
        self.assertNotIn("�", raw)                          # 无 U+FFFD

    def test_ingest_refuses_true_garbage(self):
        # 乱码闸兜底：自动转码后仍满屏控制符/乱码的二进制文件 → 拒收不烧糊
        with self.assertRaises(ProjectError):
            self.s.ingest_source(filename="junk.bin", data=bytes(range(0, 32)) * 40)
        self.assertFalse((self.s.source_dir / "raw.txt").exists())   # 未落盘糊掉的正文

    def test_ingest_kind_override(self):
        r = self.s.ingest_source(filename="x.txt", data="一段散文。".encode("utf-8"),
                                 kind="screenplay")
        self.assertEqual(r["kind"], "screenplay")


class TestUpsertEntities(SeriesCase):
    """upsert_entities：合并不覆盖·保人工字段·keywords 取并集。"""

    def test_add_then_merge_preserves_human_fields(self):
        self.s.upsert_entities(characters=[{"name": "林深", "appearance": "青年设计师"}],
                               props=[{"name": "面板", "desc": "UI", "keywords": ["HUD"]}])
        # 人工调音色 + 逐实体评论 + 追加关键词
        self.s.characters[0]["voice"] = "沉稳青年"
        self.s.characters[0]["comments"] = [{"text": "眼神再锐利"}]
        self.s.props[0]["keywords"].append("金板")
        self.s.save()
        # 二次重抽：改抽取字段、给不同 keywords、voice 缺省
        st = self.s.upsert_entities(
            characters=[{"name": "林深", "appearance": "疲惫青年"}],
            props=[{"name": "面板", "desc": "发光 UI", "keywords": ["HUD", "界面"]}])
        self.assertEqual(st["updated"], 2)
        c = self.ws.get_project("adp").characters[0]
        self.assertEqual(c["appearance"], "疲惫青年")            # 抽取字段更新
        self.assertEqual(c["voice"], "沉稳青年")                 # 人工音色保留
        self.assertEqual(c["comments"], [{"text": "眼神再锐利"}])  # 人工评论保留
        p = self.ws.get_project("adp").props[0]
        self.assertEqual(p["desc"], "发光 UI")
        self.assertEqual(p["keywords"], ["HUD", "金板", "界面"])  # 并集·人工「金板」不丢

    def test_new_entity_added(self):
        st = self.s.upsert_entities(characters=[{"name": "甲方", "appearance": "西装男"}])
        self.assertEqual(st["added"], 1)
        self.assertEqual([c["name"] for c in self.s.characters], ["甲方"])


class TestUpsertChapterOutline(SeriesCase):
    """upsert_chapter_outline：幂等·只写 outline·不碰 shots/review。"""

    def test_outline_only_write_idempotent_and_warn(self):
        self.s.create_chapter("第一集", cid="ch01")
        # 章节放入 shots + review（人类表态），outline 写入不得动它们
        data = self.ws.store.load_chapter("adp", "ch01")
        data["shots"] = [{"id": 1, "narration": "hi", "review": {"image": {"state": "done"}}}]
        self.ws.store.save_chapter("adp", "ch01", data)

        self.assertEqual(self.s.upsert_chapter_outline("ch01", "大纲A"), "updated-warn")  # 已拆镜→warn
        d = self.ws.store.load_chapter("adp", "ch01")
        self.assertEqual(d["outline"], "大纲A")
        self.assertEqual(d["shots"][0]["review"], {"image": {"state": "done"}})  # 人类表态原封不动
        self.assertEqual(self.s.upsert_chapter_outline("ch01", "大纲A"), "noop")   # 幂等
        self.assertEqual(self.s.upsert_chapter_outline("ch01", "大纲B"), "updated-warn")

    def test_missing_chapter_raises(self):
        with self.assertRaises(ProjectError):
            self.s.upsert_chapter_outline("ch99", "x")

    def test_compose_outline_format(self):
        text = self.s.compose_outline({"logline": "本集", "open_hook": "钩子", "end_hook": "尾钩"})
        self.assertIn("【本集】本集", text)
        self.assertIn("【开场钩子】钩子", text)
        self.assertIn("【尾钩】尾钩", text)


class TestScaffold(SeriesCase):
    """scaffold_episodes：显式 cid（章号==集号·跳集不错位·回填 chapter_id）+ 幂等重跑。"""

    def test_explicit_cid_skip_gap_and_backfill(self):
        self._eps(1, 2, 3)
        res = self.s.scaffold_episodes(only=[1, 3])          # 跳集建
        self.assertEqual(res["created"], ["ch01", "ch03"])   # 不是 ch01/ch02
        self.assertEqual(res["mapping"], {1: "ch01", 3: "ch03"})
        eps = self.ws.get_project("adp").episodes
        self.assertEqual([(e["no"], e.get("chapter_id")) for e in eps],
                         [(1, "ch01"), (2, None), (3, "ch03")])   # chapter_id 回填正确
        # 章节 outline 从 episode 编译
        d = self.ws.store.load_chapter("adp", "ch03")
        self.assertIn("【本集】L3", d["outline"])

    def test_order_aligns_to_episode_no(self):
        self._eps(1, 2, 3)
        self.s.scaffold_episodes(only=[1, 3])
        self.s.scaffold_episodes()                            # 补 ch02
        ids = [c["id"] for c in self.s.list_chapters()]       # 按 order 排序
        self.assertEqual(ids, ["ch01", "ch02", "ch03"])       # order==集号→集序排列

    def test_only_empty_builds_nothing(self):
        # 显式空过滤（畸形 --only 解析为空）→ 不建任何集，而非误全建
        self._eps(1, 2)
        res = self.s.scaffold_episodes(only=[])
        self.assertEqual(res["created"], [])
        self.assertEqual(res["mapping"], {})

    def test_idempotent_rerun_no_throw(self):
        self._eps(1, 2)
        self.s.scaffold_episodes()
        res2 = self.s.scaffold_episodes()                     # 幂等：重跑不炸
        self.assertEqual(res2["created"], [])
        self.assertEqual(res2["updated"], [])

    def test_no_death_lock_on_preexisting_chapter(self):
        # 手工先建 ch01，再 scaffold ep1 —— 绝不因「章节已存在」抛错
        self.s.create_chapter("手工", cid="ch01")
        self._eps(1)
        res = self.s.scaffold_episodes()
        self.assertEqual(res["created"], [])
        self.assertEqual(res["updated"], ["ch01"])
        self.assertIn("【本集】L1", self.ws.store.load_chapter("adp", "ch01")["outline"])

    def test_scaffold_updates_outline_on_episode_change(self):
        self._eps(1)
        self.s.scaffold_episodes()
        self.s.episodes[0]["logline"] = "改写后的一句话"
        self.s.save()
        res = self.s.scaffold_episodes()                      # 改 episodes 重跑→刷新 outline
        self.assertEqual(res["updated"], ["ch01"])
        self.assertIn("改写后的一句话", self.ws.store.load_chapter("adp", "ch01")["outline"])


class TestScaffoldChapterParity(SeriesCase):
    """一章一集协议的出声核对（CLI `adapt scaffold`）：源按章标切分时，分集数 ≠
    源章节数要告警——多章压一集会丢关键情节与设定，协议是「小说有多少章，视频就有
    多少章节」。只告警不阻断（例外由用户点名）；窗口化切分没有章数可对，不判。"""

    def _scaffold_out(self):
        import argparse
        import contextlib
        import io
        from kinema.cli import cmd_adapt_scaffold
        ns = argparse.Namespace(workspace=str(self.ws.root), project="adp", only=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_adapt_scaffold(ns)
        return buf.getvalue()

    def _ingest_three_chapters(self):
        text = "\n\n".join(f"第{z}章 事{z}\n正文{z}。" for z in ("一", "二", "三"))
        self.s.ingest_source(filename="n.txt", data=text.encode("utf-8"))

    def test_mismatch_warns(self):
        self._ingest_three_chapters()
        self._eps(1)                                     # 3 章只切了 1 集 → 合并了章节
        out = self._scaffold_out()
        self.assertIn("协议是一章一集", out)
        self.assertIn("分集数（1）≠ 源章节数（3）", out)

    def test_one_to_one_stays_silent(self):
        self._ingest_three_chapters()
        self._eps(1, 2, 3)                               # 一章一集 → 不喊
        self.assertNotIn("协议是一章一集", self._scaffold_out())

    def test_no_source_no_parity_check(self):
        self._eps(1)                                     # 无源文本（原创路径）→ 不判
        self.assertNotIn("协议是一章一集", self._scaffold_out())


class TestScriptDetail(SeriesCase):
    """Studio 剧本工作台负载：目录瘦身（首屏不下发全文）+ 正文按段懒加载。"""

    def _ingest(self):
        # 三章，各章正文不同长度，供偏移切片校验
        novel = ("第一章 晨\n" + "甲。" * 40 + "\n\n"
                 "第二章 昏\n" + "乙。" * 90 + "\n\n"
                 "第三章 夜\n" + "丙。" * 20 + "\n")
        return self.s.ingest_source(filename="bk.txt", data=novel.encode("utf-8"))

    def test_detail_slims_segments_no_fulltext(self):
        from kinema.studio import scanner
        r = self._ingest()
        d = scanner.script_detail(self.ws.root, self.ws.store, "adp")
        self.assertEqual(len(d["segments"]), r["n_segments"])
        self.assertEqual(d["segment_kind"], "chapter")
        for s in d["segments"]:                              # 首屏只有目录：无全文、有段字数
            self.assertNotIn("text", s)
            self.assertIsInstance(s["chars"], int)
        self.assertNotIn("text_truncated", d)                # 截断语义已由懒加载取代
        self.assertEqual(d["source_chars"], r["chars"])      # 取自入库指针，非读全文

    def test_segment_lazyload_slices_raw(self):
        from kinema.studio import scanner
        self._ingest()
        d = scanner.script_detail(self.ws.root, self.ws.store, "adp")
        mid = d["segments"][1]                               # 第二章
        seg = scanner.script_segment(self.ws.root, "adp", mid["index"])
        self.assertEqual(seg["index"], mid["index"])
        self.assertEqual(seg["chars"], mid["chars"])         # 懒加载切片字数 == 目录段字数
        self.assertIn("乙。", seg["text"])
        self.assertNotIn("甲。", seg["text"])                # 只切本段，不越界到上一章

    def test_segment_missing_returns_none(self):
        from kinema.studio import scanner
        self._ingest()
        self.assertIsNone(scanner.script_segment(self.ws.root, "adp", 999))   # 越界段
        self.assertIsNone(scanner.script_segment(self.ws.root, "nope", 0))    # 无项目/无索引

    def test_segment_without_offsets_notes(self):
        # fdx 剧本段无字符偏移 → 正文取不到，给 note 而非报错
        from kinema.studio import scanner
        segfile = self.s.source_dir / "segments.json"
        segfile.write_text(json.dumps({
            "kind": "screenplay", "chars": 0, "n_segments": 1, "segment_kind": "scene",
            "segments": [{"index": 0, "type": "scene", "heading": "INT. 房间",
                          "char_start": None, "char_end": None}]}, ensure_ascii=False),
            encoding="utf-8")
        seg = scanner.script_segment(self.ws.root, "adp", 0)
        self.assertEqual(seg["text"], "")
        self.assertIn("偏移", seg["note"])


class TestClearSource(SeriesCase):
    """清空源文本：剧本创作初期（未建章）可清；已建章硬闸拒绝。"""

    def _ingest(self):
        self.s.ingest_source(filename="b.txt", data="第一章 起\n正文。\n".encode("utf-8"))

    def test_clear_when_no_chapters(self):
        from kinema.studio import actions
        self._ingest()
        self.assertTrue((self.s.source_dir / "raw.txt").is_file())
        r = actions.clear_source(self.ws.root, "adp")
        self.assertTrue(r["cleared"])
        self.assertIn("raw.txt", r["removed"])
        self.assertFalse((self.s.source_dir / "raw.txt").exists())        # 文件已删
        self.assertFalse((self.s.source_dir / "segments.json").exists())
        self.assertIsNone(self.ws.get_project("adp").data.get("source"))   # 指针已清

    def test_blocked_when_chapters_exist(self):
        from kinema.studio import actions
        from kinema.errors import KinemaError
        self._ingest()
        self.s.create_chapter("第一集", cid="ch01")                       # 进入制作期
        with self.assertRaises(KinemaError):
            actions.clear_source(self.ws.root, "adp")
        self.assertTrue((self.s.source_dir / "raw.txt").is_file())        # 拒绝后源文件不动
        self.assertIsNotNone(self.ws.get_project("adp").data.get("source"))


# ============================================================================
# EPUB 拆书 —— 纯 stdlib（zipfile + ElementTree + html.parser）
# ============================================================================
_CH1 = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>第一章 猝死</title></head>"
    "<body><h1>第一章 猝死</h1>"
    "<p>凌晨三点，林深倒在第 38 版改稿上。</p>"
    "<p>窗外霓虹闪烁，他最后看了一眼屏幕。</p></body></html>"
)
_CH2 = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>第二章 重生</title></head>"
    "<body><h1>第二章 重生</h1>"
    "<p>他睁开眼，回到提案前夜，甲方尚未落座。</p></body></html>"
)
_CONTAINER = (
    '<?xml version="1.0"?>'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    "<rootfiles><rootfile full-path=\"OEBPS/content.opf\" "
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
_OPF_NCX = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
    'unique-identifier="bookid">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>改编测试</dc:title></metadata>"
    "<manifest>"
    '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
    "</manifest>"
    '<spine toc="ncx"><itemref idref="ch1"/><itemref idref="ch2"/></spine>'
    "</package>"
)
_NCX = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    "<navMap>"
    '<navPoint id="np1" playOrder="1"><navLabel><text>猝死</text></navLabel>'
    '<content src="ch1.xhtml"/></navPoint>'
    '<navPoint id="np2" playOrder="2"><navLabel><text>重生</text></navLabel>'
    '<content src="ch2.xhtml"/></navPoint>'
    "</navMap></ncx>"
)
_OPF_NAV = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
    'unique-identifier="bookid">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:title>改编测试</dc:title></metadata>"
    "<manifest>"
    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
    'properties="nav"/>'
    '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
    "</manifest>"
    '<spine><itemref idref="ch1"/><itemref idref="ch2"/></spine>'
    "</package>"
)
_NAV = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops">'
    "<head><title>目录</title></head>"
    '<body><nav epub:type="toc"><ol>'
    '<li><a href="ch1.xhtml">猝死</a></li>'
    '<li><a href="ch2.xhtml">重生</a></li>'
    "</ol></nav></body></html>"
)


def _build_epub(*, nav_kind: str = "ncx", encrypted: bool = False) -> bytes:
    """内存里组一本最小合法 EPUB（mimetype 首个 stored + container + OPF + 两章
    xhtml + NCX 或 nav）。nav_kind: "ncx"=EPUB2 / "nav"=EPUB3。"""
    files = {"META-INF/container.xml": _CONTAINER,
             "OEBPS/ch1.xhtml": _CH1, "OEBPS/ch2.xhtml": _CH2}
    if nav_kind == "nav":
        files["OEBPS/content.opf"] = _OPF_NAV
        files["OEBPS/nav.xhtml"] = _NAV
    else:
        files["OEBPS/content.opf"] = _OPF_NCX
        files["OEBPS/toc.ncx"] = _NCX
    if encrypted:
        files["META-INF/encryption.xml"] = (
            '<?xml version="1.0"?><encryption '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestEpub(SeriesCase):
    """EPUB 拆书守卫：EPUB2/NCX·EPUB3/nav 抽正文+章标题、检测、DRM 拒收、入库。"""

    def test_extract_epub2_ncx(self):
        text, segs = A.extract_epub(_build_epub(nav_kind="ncx"))
        # 两章正文都在、且按 spine 顺序（ch1 先于 ch2）
        self.assertIn("凌晨三点，林深倒在第 38 版改稿上。", text)
        self.assertIn("他睁开眼，回到提案前夜", text)
        self.assertLess(text.index("凌晨三点"), text.index("他睁开眼"))
        # 段形状对齐 split_novel
        self.assertEqual(len(segs), 2)
        self.assertEqual([s["type"] for s in segs], ["chapter", "chapter"])
        self.assertEqual([s["index"] for s in segs], [1, 2])
        self.assertEqual([s["title"] for s in segs], ["猝死", "重生"])   # NCX 章标题
        # 偏移递增、相邻衔接、末段收尾到全文尾
        self.assertLess(segs[0]["char_start"], segs[1]["char_start"])
        self.assertEqual(segs[0]["char_end"], segs[1]["char_start"])
        self.assertEqual(segs[-1]["char_end"], len(text))
        self.assertEqual(segs[0]["char_start"], 0)
        self.assertTrue(segs[0]["preview"])

    def test_extract_epub3_nav(self):
        text, segs = A.extract_epub(_build_epub(nav_kind="nav"))
        self.assertEqual([s["title"] for s in segs], ["猝死", "重生"])   # nav 章标题
        self.assertIn("凌晨三点", text)
        self.assertIn("他睁开眼", text)
        self.assertEqual(segs[-1]["char_end"], len(text))

    def test_is_epub_detection(self):
        raw = _build_epub(nav_kind="ncx")
        self.assertTrue(A.is_epub(b"whatever", "Book.EPUB"))          # 后缀命中
        self.assertTrue(A.is_epub(raw, "noext"))                      # ZIP+container 命中
        self.assertFalse(A.is_epub("第一章 起\n正文。".encode("utf-8"), "n.txt"))
        self.assertFalse(A.is_epub(b"PK\x03\x04not-a-real-zip", "x.zip"))   # 坏 ZIP

    def test_encryption_rejected(self):
        with self.assertRaises(ValueError):
            A.extract_epub(_build_epub(nav_kind="ncx", encrypted=True))

    def test_ingest_epub_writes_clean_utf8(self):
        r = self.s.ingest_source(filename="book.epub", data=_build_epub(nav_kind="ncx"))
        self.assertEqual(r["encoding"], "epub")
        self.assertEqual(r["kind"], "novel")
        self.assertEqual(r["segment_kind"], "chapter")
        self.assertEqual(r["n_segments"], 2)
        raw = (self.s.source_dir / "raw.txt").read_text(encoding="utf-8")
        self.assertIn("凌晨三点，林深倒在第 38 版改稿上。", raw)
        self.assertNotIn("�", raw)                                    # 干净 UTF-8
        self.assertNotIn("<p>", raw)                                  # 标签已剥离
        seg = json.loads((self.s.source_dir / "segments.json").read_text(encoding="utf-8"))
        self.assertEqual(seg["segment_kind"], "chapter")
        self.assertEqual(seg["n_segments"], 2)
        src = self.ws.get_project("adp").data["source"]
        self.assertEqual(src["kind"], "novel")
        self.assertEqual(src["file"], "source/raw.txt")
        self.assertTrue(src["sha256"].startswith("sha256:"))


class TestMoodboard(SeriesCase):
    """参考库/风格垫图：库项 {path,on} · 默认全局套用 · 停用留库 · 镜级 refs 覆盖 · 移除清空。"""

    def _mb(self, name):
        mbdir = self.s.dir / "assets" / "refs" / "moodboard"
        mbdir.mkdir(parents=True, exist_ok=True)
        p = mbdir / name
        p.write_bytes(b"\x89PNG\r\n")
        return str(p.resolve())

    def test_add_sync_ref_images_remove(self):
        from kinema.project import Project
        cf = self.s.create_chapter("第一集", cid="ch01")
        abs1 = self._mb("s1.png")
        self.s.add_moodboard(abs1)
        lib = self.ws.get_project("adp").moodboard                           # 库项为 {path,on}
        self.assertEqual(lib, [{"path": abs1, "on": True}])
        self.assertEqual(self.ws.get_project("adp").moodboard_active(), [abs1])
        ch = self.ws.store.load_chapter("adp", "ch01")
        self.assertEqual(ch["style"]["moodboard"], [abs1])                   # 同步进章节 style（仅生效集）
        self.assertIn(abs1, Project.load(cf).ref_images())                   # 分镜图默认套用垫图
        self.assertTrue(self.s.remove_moodboard(abs1))                       # 移除
        self.assertEqual(self.ws.get_project("adp").moodboard, [])
        self.assertEqual(self.ws.store.load_chapter("adp", "ch01")["style"].get("moodboard"), [])
        self.assertNotIn(abs1, Project.load(cf).ref_images())

    def test_legacy_string_items_normalized(self):
        """历史纯字符串路径 → 读时一次性升级为 {path,on:True}。"""
        abs1 = self._mb("legacy.png")
        self.s.data["moodboard"] = [abs1]            # 模拟旧格式落盘
        lib = self.s.moodboard
        self.assertEqual(lib, [{"path": abs1, "on": True}])
        self.assertEqual(self.s.moodboard_active(), [abs1])

    def test_toggle_off_keeps_lib_but_drops_from_active_and_chapter(self):
        from kinema.project import Project
        cf = self.s.create_chapter("第一集", cid="ch01")
        abs1 = self._mb("s1.png")
        self.s.add_moodboard(abs1)
        self.assertTrue(self.s.set_moodboard_on(abs1, False))                # 停用
        self.assertFalse(self.s.set_moodboard_on(abs1, False))               # 幂等：已停用不再变更
        self.assertEqual(self.s.moodboard, [{"path": abs1, "on": False}])    # 仍在库
        self.assertEqual(self.s.moodboard_active(), [])                      # 不在生效集
        self.assertEqual(self.ws.store.load_chapter("adp", "ch01")["style"]["moodboard"], [])
        self.assertNotIn(abs1, Project.load(cf).ref_images())               # 默认不套用

    def test_shot_refs_override(self):
        """镜级 shots[].refs：显式列表精确覆盖，[] 表示本镜刻意不用垫图。"""
        from kinema.project import Project
        cf = self.s.create_chapter("第一集", cid="ch01")
        abs1, abs2 = self._mb("s1.png"), self._mb("s2.png")
        self.s.add_moodboard(abs1)          # 只有 s1 默认生效
        proj = Project.load(cf)
        shot = {"id": 1, "narration": "x", "dur": 2.0}
        self.assertEqual(proj.moodboard_refs(shot), [abs1])                  # 无 refs → 默认生效集
        shot["refs"] = [abs2]
        self.assertEqual(proj.moodboard_refs(shot), [abs2])                  # 显式覆盖
        shot["refs"] = []
        self.assertEqual(proj.moodboard_refs(shot), [])                      # 空列表 = 本镜不用垫图

    def test_studio_toggle_and_scanner_surface(self):
        """Studio：toggle_moodboard 切换默认启用 + scanner 下发 {path,url,on}。"""
        from kinema.studio import actions, scanner
        abs1 = self._mb("s1.png")
        self.s.add_moodboard(abs1)
        r = actions.toggle_moodboard(self.ws.root, "adp", path=abs1, on=False)
        self.assertTrue(r["changed"])
        self.assertEqual(r["moodboard"], [{"path": abs1, "on": False}])
        view = scanner.project_detail(self.ws.root, self.ws.store, "adp")
        self.assertEqual([(Path(m["path"]).name, m["on"]) for m in view["moodboard"]],
                         [("s1.png", False)])                                 # on 态如实下发

    def test_studio_set_shot_refs(self):
        """Studio：set_shot_refs 写 shots[].refs（[路径…]/[] 精确覆盖 · None 清除跟随默认）。"""
        from kinema.studio import actions
        self.s.create_chapter("第一集", cid="ch01")
        abs1 = self._mb("s1.png")
        d = self.ws.store.load_chapter("adp", "ch01")
        d["shots"] = [{"id": 1, "narration": "x", "dur": 2.0}]
        self.ws.store.save_chapter("adp", "ch01", d)
        self.assertEqual(actions.set_shot_refs(self.ws.root, "adp", "ch01",
                                               shot=1, refs=[abs1])["refs"], [abs1])
        self.assertEqual(actions.set_shot_refs(self.ws.root, "adp", "ch01",
                                               shot=1, refs=[])["refs"], [])
        self.assertIsNone(actions.set_shot_refs(self.ws.root, "adp", "ch01",
                                                shot=1, refs=None)["refs"])

    def test_asset_refs_resolution(self):
        """设定图逐张垫图解析 moodboard_refs_for：显式列表精确用（[]=不用）· None=默认生效集。"""
        abs1, abs2 = self._mb("s1.png"), self._mb("s2.png")
        self.s.add_moodboard(abs1)                                            # 只有 s1 默认生效
        self.assertEqual(self.s.moodboard_refs_for(None), [abs1])            # 无 refs → 默认生效集
        self.assertEqual(self.s.moodboard_refs_for([abs2]), [abs2])          # 显式覆盖
        self.assertEqual(self.s.moodboard_refs_for([]), [])                  # 空列表 = 本图不用垫图

    def test_studio_set_asset_refs(self):
        """Studio：set_asset_refs 写角色/道具实体 refs 与场景 scene_refs（[路径…]/[]/None 三态）。"""
        from kinema.studio import actions
        self.s.add_character("林深", appearance="银发少年")
        self.s.add_prop("魔剑", desc="幽蓝长剑", kind="weapon")
        self.s.data["scene"] = "古城墙"
        self.s.save()
        abs1 = self._mb("s1.png")
        r = actions.set_asset_refs(self.ws.root, "adp", kind="character", name="林深", refs=[abs1])
        self.assertEqual(r["refs"], [abs1])
        # 逐张独立：写林深不影响魔王/道具（其余仍跟随默认 None）
        self.assertEqual(self.ws.get_project("adp").characters[0].get("refs"), [abs1])
        self.assertEqual(actions.set_asset_refs(self.ws.root, "adp", kind="prop",
                                                name="魔剑", refs=[])["refs"], [])
        self.assertEqual(actions.set_asset_refs(self.ws.root, "adp", kind="scene",
                                                refs=[abs1])["refs"], [abs1])
        self.assertEqual(self.ws.get_project("adp").data.get("scene_refs"), [abs1])
        self.assertIsNone(actions.set_asset_refs(self.ws.root, "adp", kind="character",
                                                 name="林深", refs=None)["refs"])
        # scanner 下发 refs（对话框据此预选）
        from kinema.studio import scanner
        from kinema.models import ConfigStore
        actions.set_asset_refs(self.ws.root, "adp", kind="character", name="林深", refs=[abs1])
        view = scanner.project_detail(self.ws.root, ConfigStore.load(None), "adp")
        self.assertEqual(view["characters"][0]["refs"], [abs1])
        self.assertEqual(view["scene_refs"], [abs1])
        # 具名取景地：refs 写 scenes[] 实体条目，全局 scene_refs 原样不动
        #（与 refine._asset_refs / gen-refs 读侧同分派——写进全局的话重生时读不到）
        s2 = self.ws.get_project("adp")
        s2.add_scene("古城墙")
        s2.save()
        r = actions.set_asset_refs(self.ws.root, "adp", kind="scene",
                                   name="古城墙", refs=[abs1])
        self.assertEqual(r["refs"], [abs1])
        p3 = self.ws.get_project("adp")
        self.assertEqual(next(x for x in p3.scenes
                              if x["name"] == "古城墙").get("refs"), [abs1])
        self.assertEqual(p3.data.get("scene_refs"), [abs1])   # 全局键未被具名写串改
        view2 = scanner.project_detail(self.ws.root, ConfigStore.load(None), "adp")
        self.assertEqual(view2["scenes"][0]["refs"], [abs1])  # 灯箱回读走这份下发

    def test_studio_set_asset_refs_unknown_rejected(self):
        """未知角色/道具名 → 抛错，不静默写空。"""
        from kinema.studio import actions
        from kinema.errors import KinemaError
        with self.assertRaises(KinemaError):
            actions.set_asset_refs(self.ws.root, "adp", kind="character", name="查无此人", refs=[])

    def test_cli_only_filters_single_asset(self):
        """project refs --only kind:名 只(重)生成单张设定图（设定图垫图重生的落点）。"""
        from kinema.cli import build_parser
        self.s.add_character("林深", appearance="银发少年")
        self.s.add_prop("魔剑", desc="幽蓝长剑", kind="weapon")
        self.s.data["scene"] = "古城墙"
        self.s.save()
        ns = build_parser().parse_args(
            ["project", "refs", "adp", "--only", "prop:魔剑", "--mock",
             "--workspace", str(self.ws.root)])
        ns.func(ns)
        s2 = self.ws.get_project("adp")
        self.assertTrue(s2.props[0].get("sheet"))          # 只有魔剑出了设定图
        self.assertFalse(s2.characters[0].get("sheet"))    # 角色未被触碰
        self.assertFalse(s2.data.get("scene_ref"))         # 场景未被触碰


class TestCharSheetPrompt(SeriesCase):
    """角色设定图【三区两视铁律】提示词：武器与随身物件一律不上本表（走独立的
    道具/武器设定图），项目其他道具（敌物/场景物）更不塞进角色设定图。"""

    def test_weapon_never_enters_the_sheet(self):
        """全身像无条件空手：角色设定图提示词里没有武器名的合法落点——
        出现即意味着模型会把它画到全身像手里。"""
        from kinema.cli import _char_sheet_prompt
        c = {"name": "喵勇者", "appearance": "橘白虎斑猫", "weapon": "锋利小猫爪"}
        p = _char_sheet_prompt(c, "3D，")
        self.assertNotIn("锋利小猫爪", p)         # 武器名不进设定图
        self.assertIn("橘白虎斑猫", p)            # 外貌进
        self.assertIn("不持握", p)                # 全身像明确空手（无条件）
        self.assertIn("武器与随身物件一律走各自独立的设定图", p)

    def test_cmd_excludes_other_props(self):
        """端到端：项目有敌物道具，跑 project refs 捕获角色提示词——武器与其他道具名一律不进。"""
        from kinema.cli import build_parser
        from kinema.providers.image import mock as mockmod
        self.s.add_character("喵勇者", appearance="橘白虎斑猫", weapon="锋利小猫爪")
        self.s.add_prop("水杯魔王", desc="水杯成精")
        self.s.add_prop("纸巾卷魔王", desc="纸巾成精")
        self.s.save()
        seen = []
        orig = mockmod.MockImageProvider.generate
        def spy(self, prompt, out_path, **kw):
            if str(kw.get("label", "")).startswith("CHAR"):
                seen.append(prompt)
            return orig(self, prompt, out_path, **kw)
        mockmod.MockImageProvider.generate = spy
        try:
            ns = build_parser().parse_args(
                ["project", "refs", "adp", "--only", "character:喵勇者", "--mock",
                 "--workspace", str(self.ws.root)])
            ns.func(ns)
        finally:
            mockmod.MockImageProvider.generate = orig
        self.assertEqual(len(seen), 1)
        self.assertNotIn("锋利小猫爪", seen[0])     # 自己的武器也不进（归武器设定图）
        self.assertNotIn("水杯魔王", seen[0])       # 敌物道具不塞进设定图
        self.assertNotIn("纸巾卷魔王", seen[0])

    def test_silhouette_notes_injected(self):
        """剪影辨识度要点拼进设定图提示词，插在外貌之后、收尾段之前。"""
        from kinema.cli import _char_sheet_prompt
        c = {"name": "林深", "appearance": "银发青年",
             "silhouette_notes": "左肩甲高耸、右袖空荡、发尾一撮翘起"}
        p = _char_sheet_prompt(c, "3D，")
        self.assertIn("左肩甲高耸、右袖空荡、发尾一撮翘起", p)
        self.assertIn("剪影辨识度要点", p)
        self.assertLess(p.index("银发青年"), p.index("左肩甲高耸"))      # 紧随外貌之后
        self.assertLess(p.index("左肩甲高耸"),
                        p.index("（含衣领与领口样式）严格一致"))   # 收尾段在后
        self.assertIn("不持握", p)                # 空手铁律不动
        # 不填=不出这一格（不给模型塞空标签）
        self.assertNotIn("剪影辨识度要点", _char_sheet_prompt(
            {"name": "林深", "appearance": "银发青年"}, "3D，"))

    def test_portrait_is_neutral_expression(self):
        """定稿肖像与两个全身像一律中性表情、嘴唇轻闭——表情戏属于分镜。

        不锁的话，appearance 里「笑起来露小虎牙」这类描述会被当成表情指令、
        肖像画成张嘴大笑——定稿表必须锁中性脸，笑容留给 shots[].emotion。"""
        from kinema.cli import _char_sheet_prompt
        p = _char_sheet_prompt({"name": "林深", "appearance": "银发青年"}, "3D，")
        self.assertIn("中性自然表情", p)
        self.assertIn("嘴唇轻闭不露齿", p)
        self.assertIn("面部与肖像同为中性放松表情", p)   # 全身像同口径

    def test_constraints_not_in_sheet_prompt(self):
        """M8 反向纪律：constraints 永不进角色设定图提示词。

        它是用户自由文本禁令，会与「全身像双手空手」这条引擎铁律正面竞争
        （「必须持剑」直接顶撞「双手空手」）。落点在分镜侧的
        shots[].negative_prompt，不在这里。"""
        from kinema.cli import _char_sheet_prompt
        c = {"name": "面具人", "appearance": "灰袍身形", "weapon": "细剑",
             "constraints": "绝不摘面具，必须始终持剑"}
        p = _char_sheet_prompt(c, "3D，")
        self.assertNotIn("绝不摘面具", p)
        self.assertNotIn("必须始终持剑", p)
        self.assertNotIn("constraints", p)
        self.assertIn("不持握", p)                 # 全身空手铁律仍是唯一口径


# M8 五字段样本：三个集合 + 两条自由文本（全部人工/AI 创作，非抽取字段）
_M8 = {"required_emotions": ["平静", "愤怒", "含泪"],
       "required_actions": ["拔剑", "跪地"],
       "required_views": ["俯视"],
       "silhouette_notes": "左肩甲高耸、右袖空荡",
       "constraints": "绝不摘面具"}
_M8_KEYS = tuple(_M8)


class TestCharRoster(SeriesCase):
    """M8 角色清单前置：五个人填字段的三条流转路径必须全通且互不串。

      · 存量章节 —— 走 sync_design_to_chapters 的 char_fields 白名单（漏登记=静默失效）；
      · 新建章节 —— 走 create_chapter 的整份拷贝（与白名单无关）；
      · 重抽实体 —— upsert_entities **绝不能**认这五个字段（否则下次 adapt 清空人工创作）。
    """

    def _chapter_char(self, cid="ch01", name="林深") -> dict:
        data = self.ws.store.load_chapter("adp", cid)
        return next(c for c in data["characters"] if c["name"] == name)

    def test_sync_design_to_chapters_carries_m8_fields(self):
        """存量章节：建章在前、填清单在后，sync 必须把五字段推下去（M7 判定跑在章节层）。"""
        self.s.add_character("林深", appearance="银发青年")
        self.s.create_chapter("第一集", cid="ch01")
        self.assertEqual([k for k in _M8_KEYS if k in self._chapter_char()], [])  # 建章时确实还没有

        self.s.characters[0].update(_M8)
        self.s.save()
        st = self.s.sync_design_to_chapters()
        self.assertEqual(st["chapters"], 1)
        self.assertEqual(st["updated"], 1)
        t = self._chapter_char()
        for k, v in _M8.items():
            self.assertEqual(t[k], v, f"{k} 未同步进存量章节（char_fields 白名单漏登记？）")
        # 系列→章节单向覆盖：章节侧改回来会被下次 sync 冲掉（required_emotions 语义由此定死为系列级）
        t["required_emotions"] = ["只有本集要用的"]
        data = self.ws.store.load_chapter("adp", "ch01")
        data["characters"] = [t]
        self.ws.store.save_chapter("adp", "ch01", data)
        self.s.sync_design_to_chapters()
        self.assertEqual(self._chapter_char()["required_emotions"], _M8["required_emotions"])

    def test_create_chapter_inherits_m8_fields(self):
        """新建章节：整份拷贝天然继承，且是**拷贝**不是共享引用。"""
        self.s.add_character("林深", appearance="银发青年")
        self.s.characters[0].update(_M8)
        self.s.save()
        self.s.create_chapter("第二集", cid="ch02")
        t = self._chapter_char("ch02")
        for k, v in _M8.items():
            self.assertEqual(t[k], v)
        self.assertIsNot(t["required_emotions"], self.s.characters[0]["required_emotions"])

    def test_upsert_entities_preserves_m8_fields(self):
        """重抽实体：抽取字段更新，五字段一律不动——登进那条白名单就等于下次 adapt 清空。"""
        self.s.upsert_entities(characters=[{"name": "林深", "appearance": "青年设计师"}])
        self.s.characters[0].update(_M8)
        self.s.save()
        # 重抽给出新 appearance，并（模拟误传）带上空的 M8 字段
        self.s.upsert_entities(characters=[{"name": "林深", "appearance": "疲惫青年",
                                            "required_emotions": [], "silhouette_notes": "",
                                            "constraints": "抽取器瞎猜的禁令"}])
        c = self.ws.get_project("adp").characters[0]
        self.assertEqual(c["appearance"], "疲惫青年")          # 抽取字段照常更新
        for k, v in _M8.items():
            self.assertEqual(c[k], v, f"{k} 被重抽覆盖了（upsert_entities 白名单误登记）")


class TestAssetVersioning(SeriesCase):
    """设定图（角色/场景/道具）版本栈：重生成/改造前归档旧版、可回滚（与分镜版本谱系对称）。"""

    def _put_sheet(self, name, content):
        d = self.s.refs_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(content)
        return str(p)

    def test_archive_moves_and_registers(self):
        from kinema import refine
        self.s.add_character("林深", appearance="银发")
        self.s.characters[0]["sheet"] = self._put_sheet("char_林深.png", b"v1")
        self.s.save()
        bak = refine.archive_asset_sheet(self.s, "character", "林深", reason="重新生成设定集")
        self.assertEqual(Path(bak).read_bytes(), b"v1")            # 归档=旧内容
        self.assertFalse((self.s.refs_dir / "char_林深.png").is_file())  # 移动而非复制
        self.assertEqual(len(self.s.characters[0]["versions"]), 1)
        self.assertEqual(self.s.characters[0]["sheet"],           # 标准字段字符串不变
                         str(self.s.refs_dir / "char_林深.png"))

    def test_rollback_restores_and_propagates(self):
        from kinema import refine
        self.s.add_character("林深", appearance="银发")
        cf = self.s.create_chapter("第一集", cid="ch01")           # 建章以验血缘传播
        self.s.characters[0]["sheet"] = self._put_sheet("char_林深.png", b"v1")
        self.s.save()
        refine.archive_asset_sheet(self.s, "character", "林深", reason="regen")
        (self.s.refs_dir / "char_林深.png").write_bytes(b"v2")     # 模拟重生成写回
        self.s.save()
        r, f = refine.rollback_asset_sheet(self.ws.get_project("adp"), "character", "林深", 1)
        c = self.ws.get_project("adp").characters[0]
        self.assertEqual(Path(c["sheet"]).read_bytes(), b"v1")     # 内容回到 v1
        self.assertEqual(len(c["versions"]), 2)                    # v1 + rollback-out(v2)
        self.assertTrue(c["versions"][-1]["reason"].startswith("rollback-out"))
        self.assertIsInstance(r, int)                              # 传播返回值

    def test_scene_versions_scoped_separately(self):
        """场景归档落系列文档 scene_ref_versions，与角色/道具的 versions 互不串。"""
        from kinema import refine
        self.s.data["scene"] = "古城"
        self.s.data["scene_ref"] = self._put_sheet("scene.png", b"s1")
        self.s.save()
        refine.archive_asset_sheet(self.s, "scene", reason="regen")
        self.assertEqual(len(self.s.data["scene_ref_versions"]), 1)
        self.assertNotIn("versions", self.s.data)                  # 不污染顶层 versions

    def test_cli_force_archives(self):
        """project refs --force 直出重生前归档旧设定图。"""
        from kinema.cli import build_parser
        self.s.add_character("林深", appearance="银发少年")
        self.s.characters[0]["sheet"] = self._put_sheet("char_林深.png", b"old")
        self.s.save()
        ns = build_parser().parse_args(
            ["project", "refs", "adp", "--only", "character:林深", "--force", "--mock",
             "--workspace", str(self.ws.root)])
        ns.func(ns)
        c = self.ws.get_project("adp").characters[0]
        self.assertGreaterEqual(len(c.get("versions") or []), 1)   # 旧版已归档

    def test_scanner_and_action_rollback(self):
        """scanner 下发 versions/version_history；actions.rollback_asset_version 回滚。"""
        from kinema import refine
        from kinema.studio import actions, scanner
        from kinema.models import ConfigStore
        self.s.add_character("林深", appearance="银发")
        self.s.characters[0]["sheet"] = self._put_sheet("char_林深.png", b"v1")
        self.s.save()
        refine.archive_asset_sheet(self.s, "character", "林深", reason="regen")
        (self.s.refs_dir / "char_林深.png").write_bytes(b"v2")
        self.s.save()
        view = scanner.project_detail(self.ws.root, ConfigStore.load(None), "adp")
        self.assertEqual(view["characters"][0]["versions"], 1)
        self.assertEqual(len(view["characters"][0]["version_history"]), 1)
        r = actions.rollback_asset_version(self.ws.root, "adp", kind="character", name="林深", to=1)
        self.assertEqual(r["now_contains"], "v1")
        self.assertEqual(Path(self.ws.get_project("adp").characters[0]["sheet"]).read_bytes(), b"v1")


class TestGraph(SeriesCase):
    """人物关系 / 世界观图谱（series.graph）：整体替换 + 校验 + 设定图挂载视图。"""

    GOOD = {
        "summary": "少年林深与魔渊墨渊的正邪之仇。",
        "nodes": [
            {"id": "linshen", "name": "林深", "type": "character", "role": "主角"},
            {"id": "weiran", "name": "魏然", "type": "character", "role": "师父"},
            {"id": "moyuan", "name": "墨渊", "type": "character", "role": "反派"},
            {"id": "qingyun", "name": "青云宗", "type": "faction"},
        ],
        "edges": [
            {"source": "weiran", "target": "linshen", "relation": "师徒", "kind": "mentor", "directed": True},
            {"source": "linshen", "target": "moyuan", "relation": "宿敌", "kind": "hostile"},
        ],
    }

    def test_set_graph_replaces_and_counts(self):
        stats = self.s.set_graph(self.GOOD)
        self.assertEqual(stats, {"nodes": 4, "edges": 2})
        g = self.ws.get_project("adp").data["graph"]
        self.assertEqual(g["summary"], "少年林深与魔渊墨渊的正邪之仇。")
        self.assertEqual(len(g["nodes"]), 4)
        self.assertIn("updated_at", g)                      # 引擎回填时间戳
        # 再落一份不同图谱：整体替换（非合并累加）
        self.s.set_graph({"nodes": [{"id": "x", "name": "X", "type": "character"}], "edges": []})
        self.assertEqual(len(self.ws.get_project("adp").data["graph"]["nodes"]), 1)

    def test_set_graph_rejects_dangling_edge(self):
        with self.assertRaises(ProjectError):
            self.s.set_graph({"nodes": [{"id": "a", "name": "A"}],
                              "edges": [{"source": "a", "target": "ghost"}]})

    def test_set_graph_rejects_dup_id_and_empty(self):
        with self.assertRaises(ProjectError):
            self.s.set_graph({"nodes": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}], "edges": []})
        with self.assertRaises(ProjectError):
            self.s.set_graph({"nodes": [{"name": "无id"}], "edges": []})      # 节点缺 id
        with self.assertRaises(ProjectError):
            self.s.set_graph({"nodes": [], "edges": []})                      # 空节点

    def test_graph_view_attaches_sheet_thumb_and_ref(self):
        from kinema.studio import scanner
        # 角色「林深」建设定图（绝对路径，与 gen-image 落盘一致）→ 同名节点自动挂缩略图 + ref
        self.s.add_character("林深", appearance="银发少年")
        refdir = self.s.dir / "assets" / "refs"
        refdir.mkdir(parents=True, exist_ok=True)
        sheet = refdir / "char_林深.png"
        sheet.write_bytes(b"\x89PNG\r\n")
        for c in self.s.characters:
            if c["name"] == "林深":
                c["sheet"] = str(sheet.resolve())
        self.s.save()
        self.s.set_graph(self.GOOD)
        data = json.loads((self.s.dir / "project.json").read_text(encoding="utf-8"))
        view = scanner._graph_view(data)
        lin = next(n for n in view["nodes"] if n["id"] == "linshen")
        self.assertIn("/media?path=", lin["thumb"])                          # 挂上设定图缩略
        self.assertEqual(lin["ref"], {"kind": "character", "name": "林深"})   # 富灯箱定位
        mo = next(n for n in view["nodes"] if n["id"] == "moyuan")
        self.assertIsNone(mo.get("thumb"))                                   # 无设定图角色不挂
        self.assertIsNone(mo.get("ref"))

    def test_graph_view_none_when_empty(self):
        from kinema.studio import scanner
        data = json.loads((self.s.dir / "project.json").read_text(encoding="utf-8"))
        self.assertIsNone(scanner._graph_view(data))                         # 无 graph → None


if __name__ == "__main__":
    unittest.main()


class TestSheetSpecSingleSource(_AdaptBase if "_AdaptBase" in dir() else unittest.TestCase):
    """设定图的「规格与版式规则」必须是**单一真源**（`kinema/sheets.py`）。

    三条路径要产出同一张规格的图：`project refs`（首次/重出）、`refine --asset`
    （局部改造）、Studio 灯箱「↻ 重新生成」。规则只写在其中一条里，另外两条就只剩
    「旧图 + 一句指令」：角色设定图局部改造后四栏版式会塌，还会被按 1:1
    出成方图（如 refine 自己写死 `"1:1" if kind != "scene"`——角色本该 16:9 横版）。
    """

    def test_character_sheet_is_wide_not_square(self):
        from kinema import sheets
        self.assertEqual(sheets.aspect_for("character"), "16:9")
        self.assertEqual(sheets.aspect_for("prop"), "1:1")

    def test_scene_follows_project_aspect(self):
        from kinema import sheets

        class S:
            data = {"aspect": "9:16"}
        self.assertEqual(sheets.aspect_for("scene", S()), "9:16")

    def test_refine_reuses_the_same_rules_and_aspect(self):
        """局部改造必须回喂**该类设定图的完整版式纪律**并走同一套比例。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "refine.py"
               ).read_text(encoding="utf-8")
        seg = src.split("def refine_asset(")[1].split("\ndef ")[0]
        self.assertIn("sheets.rules_for(", seg, "局部改造没有回喂版式规则")
        self.assertIn("sheets.aspect_for(", seg, "局部改造没走统一比例")
        # 只看代码：注释里正引用着该写法作反例，连注释一起查会自己判自己红
        code = "\n".join(ln for ln in seg.splitlines()
                         if not ln.strip().startswith("#"))
        self.assertNotIn('"1:1" if kind != "scene"', code,
                         "又把比例写死了——角色设定图会被出成方图")
        self.assertIn("select_style_prefix", seg, "局部改造丢了画风前缀")

    def test_rules_carry_the_hard_disciplines(self):
        from kinema import sheets
        ch = "，".join(sheets.rules_for("character", {"weapon": "断刃"}))
        for want in ("40% : 30% : 30%", "不持握", "中区正面、右区背面"):
            self.assertIn(want, ch, f"角色版式规则缺「{want}」")
        self.assertNotIn("断刃", ch, "武器名不进角色版式规则")
        pr = "，".join(sheets.rules_for("prop", {"kind": "weapon"}))
        self.assertIn("不出现色板色块、颜色条带", pr)
        self.assertIn("武器设定图", pr)

    def test_regen_without_comments_falls_back_to_a_rule_abiding_rebuild(self):
        """没提意见也要能重出——「没意见」恰恰是最常见的诉求（就是不满意、
        想按原规则再抽一张），直接拒绝会挡掉这条主路径。降级到 `project refs --force`，
        它走的正是设定图的完整版式提示词，比拿空指令去 refine 正确得多。"""
        src = (Path(__file__).resolve().parents[1] / "kinema" / "studio"
               / "actions.py").read_text(encoding="utf-8")
        seg = src.split("def regen_asset(")[1].split("\ndef ")[0]
        self.assertNotIn("还没有提意见——先在灯箱里提", seg, "没意见仍被拒绝")
        self.assertIn('"project", "refs", pid, "--only", asset, "--force"', seg)
        self.assertIn('"mode": "fresh"', seg)
        self.assertIn('"mode": "refine"', seg)


class TestNamedSceneStudioDisplay(unittest.TestCase):
    """项目页必须渲染具名场景的设定图——若页面只认全局 scene_ref，
    具名场景的图生成再多也不渲染，「固定场景」区整块空白，
    用户会直接得出「场景图没生成」。"""

    def test_project_page_renders_named_scene_sheets(self):
        from pathlib import Path as _P

        import kinema
        src = (_P(kinema.__file__).parent / "studio_app" / "app"
               / "project.js").read_text(encoding="utf-8")
        self.assertIn("function sceneCard", src, "项目页缺具名场景卡组件")
        self.assertIn("p.scenes", src, "项目页没读 scenes[]——具名场景设定图无处可看")
        self.assertIn('kind: "scene", name: sc.name', src,
                      "场景灯箱 actx 必须带 name（重生/提意见/版本谱系按 name 分派具名/全局）")

    def test_sheetless_entities_fold_into_a_drawer(self):
        """设定区三块（角色/道具/取景地）都必须把无图实体收进「未生成」抽屉。

        长篇改编登记几十个实体是小说层正典账本（删不得——brief/sweep/检查点从
        这里取料），但空卡平铺会让页面被大量无图卡占满——几十个实体只有几张图时，
        观感就是「很多设定图是空的」。折叠的是展示噪音不是功能：卡片本体
        （调校/试音/生成入口）原样在抽屉里；全无图的新项目保持平铺+空态引导。"""
        from pathlib import Path as _P

        import kinema
        src = (_P(kinema.__file__).parent / "studio_app" / "app"
               / "project.js").read_text(encoding="utf-8")
        self.assertIn("function foldSection", src, "缺「未生成」抽屉组件")
        self.assertGreaterEqual(src.count("foldSection("), 5,
                                "角色/道具/取景地/场景俯视四区都要接抽屉（定义+四处调用）")
        self.assertIn("未生成${zh}", src, "抽屉标题的名词由调用方给（俯视图区不叫设定图）")
        css = (_P(kinema.__file__).parent / "studio_app"
               / "style.css").read_text(encoding="utf-8")
        self.assertIn(".fold-sec", css, "抽屉 CSS 不在位")

    def test_card_name_never_wraps_vertically(self):
        """设定卡头排的名字恒不换行——右侧角色定位长条挤压时该收缩的是 chip。

        名字可收缩时，短名会被长定位文案压成一列竖字。规则：名字列 flex:none
        + h4 nowrap（超长名截断省略号），role chip 是这一排唯一的弹性项。"""
        from pathlib import Path as _P

        import kinema
        css = (_P(kinema.__file__).parent / "studio_app"
               / "style.css").read_text(encoding="utf-8")
        h4 = css.split(".ccard-head h4")[1].split("}")[0]
        self.assertIn("white-space: nowrap", h4, "名字没锁 nowrap——会被挤成竖排")
        self.assertIn("text-overflow: ellipsis", h4, "超长名（取景地）须截断出省略号")
        name = css.split(".ccard-name {")[1].split("}")[0]
        self.assertIn("flex: none", name, "名字列必须 flex:none——可收缩就会被 chip 挤扁")
        role = css.split(".ccard-role {")[1].split("}")[0]
        self.assertIn("min-width: 0", role, "role chip 必须可收缩——否则挤的又变成名字")

    def test_prop_scene_card_name_never_wraps(self):
        """道具/取景地卡的名字同样恒不换行——190px 窄卡里「主角的十二平米（母亲的
        画室）」这类长名会折成两三行，规则与 .ccard-head h4 一致：
        名字截断出省略号，类型 chip 与调校按钮恒完整。"""
        from pathlib import Path as _P

        import kinema
        css = (_P(kinema.__file__).parent / "studio_app"
               / "style.css").read_text(encoding="utf-8")
        # 网格：道具/取景地定量四列（auto-fill 在宽屏会挤到一行五六个、每张缩到
        # 190px 连物件细节都看不清），角色设定图仍是两列（四视图要看得清）
        grid = css.split(".prop-grid {")[1].split("}")[0]
        self.assertIn("repeat(4, minmax(0, 1fr))", grid)
        self.assertNotIn("auto-fill", grid)
        self.assertIn("repeat(2, minmax(0, 1fr))",
                      css.split(".char-grid {")[1].split("}")[0])
        # 三段 grid 版式：宽卡名字一行截断、动作组（素材直供+调校+类型标签）靠右恒完整；
        # 窄卡走**容器查询**（按卡宽判、不看视口）——名字整行完整展示、动作组沉到卡底。
        # 按钮文字任何宽度下绝不折行（「素材直供」折成两行即失败形态）。
        title = css.split(".prcard-title {")[1].split("}")[0]
        self.assertIn("white-space: nowrap", title, "宽卡下道具/取景地名没锁 nowrap")
        self.assertIn("text-overflow: ellipsis", title, "超长名须截断出省略号")
        self.assertIn("min-width: 0", title, "grid 项缺 min-width:0 时 nowrap 会撑爆卡片而非截断")
        self.assertIn('"name actions"', css, "宽卡：名字与动作组同一行")
        self.assertIn("white-space: nowrap",
                      css.split(".prcard-actions .act-btn")[1].split("}")[0],
                      "按钮文字（素材直供/调校设定）绝不折行")
        narrow = css.split("@container (max-width: 340px)")[1].split("@")[0]
        self.assertIn('"name" "desc" "actions"', narrow.replace("\n    ", " "),
                      "窄卡：第一行名字、描述居中、按钮与标签沉底")
        self.assertIn("white-space: normal", narrow, "窄卡名字整行完整展示，不再截断")
        self.assertIn("grid-template-rows: auto 1fr auto", narrow,
                      "描述行必须 1fr 吃掉剩余高度——否则动作组贴着文字而不是钉在卡底，"
                      "并排卡片的按钮高低不齐")
        self.assertIn("container-type: inline-size",
                      css.split(".prcard {")[1].split("}")[0],
                      "容器查询按卡宽生效的前提——缺了整段 @container 静默失效")

    def test_prop_scene_cards_have_tune_directive(self):
        """道具卡/取景地卡/固定场景块都要有「⧉ 调校设定」——复制带定位坐标+现有
        设定+落地命令的标准指令粘给 AI（与角色卡同制度，设定不在网页里改）。
        落地必须点名 set 命令与 --only kind:名 定向重生：scenes[]/props[] 是
        Series.commit() 白名单管的数组，指令若引导直改 JSON，会绕过锁引入并发覆盖。"""
        from pathlib import Path as _P

        import kinema
        src = (_P(kinema.__file__).parent / "studio_app" / "app"
               / "project.js").read_text(encoding="utf-8")
        self.assertIn("function tuneBtn", src, "缺调校按钮组件")
        self.assertIn('} }, "⧉ 调校设定");', src)
        # 版式：名字独占 name 位（.prcard-title），素材直供/调校/类型标签收进动作组
        # （.prcard-actions）——窄卡时整组沉底靠的就是它们在同一个 grid 区
        self.assertIn('class: "prcard-title"', src, "名字必须用 .prcard-title 占 name 区")
        self.assertIn('class: "prcard-actions"', src, "按钮与类型标签必须收进 .prcard-actions 一组")
        self.assertGreaterEqual(src.count("tuneBtn("), 4,
                                "取景地卡/道具卡/固定场景三处都要接调校按钮（定义+三处调用）")
        for needle, why in (
            ("scenes[]（name=", "取景地指令缺定位坐标"),
            ("props[]（name=", "道具指令缺定位坐标"),
            ("顶层 scene", "固定场景指令缺定位坐标"),
            ("scene set ", "取景地落地必须走 scene set 命令"),
            ("prop set ", "道具落地必须走 prop set 命令"),
            ("--only scene:", "取景地重生须定向 --only scene:名"),
            ("--only prop:", "道具重生须定向 --only prop:名"),
        ):
            self.assertIn(needle, src, why)


class TestSheetTemplates(unittest.TestCase):
    """设定图**版式样板图**（骨架参考图）——与简笔分镜板同一个解法：版式靠垫图
    不靠每次赌提示词（代码注释记录的失败：色板被画成缩略图墙 / 三视挤成一团 /
    色板横跨全幅底部，全都是"提示词写对了、模型没照做"）。

    每类**一张简笔线稿**：线稿无身份、无材质、无明暗、无配色，几乎没有内容可抄，
    单张即可教版式（早期用多张灰模并排靠「共同点只剩骨架」消泄漏，线稿把泄漏面
    压到近零，多示例的防御对象消失）。三条设计约束缺一条就从「教版式」变成
    「教内容」：去身份 · 必须同时注入职责声明 · 样板自身不带画风前缀。

    本类只守**样板资产与职责声明的分发接线**；契约正文本身在 `test_sheets`。
    """

    def test_only_layout_bearing_kinds_have_templates(self):
        """**场景刻意没有样板**：它是单幅环境 key art，没有分区结构可教，
        垫一张具体空间只会把陈设与光线污染进每张场景图——产出会退化成
        一张普通空房间照片。"""
        from kinema import sheets
        self.assertEqual(1, len(sheets.templates_for("character")))
        self.assertEqual(1, len(sheets.templates_for("prop")))
        self.assertEqual([], sheets.templates_for("scene"), "场景不许有版式样板")
        self.assertEqual([], sheets.templates_for("不存在的类型"))

    def test_templates_are_bundled_with_the_package(self):
        """样板是**引擎运行时资产**（生成时转 base64 塞进 ref_images），必须随
        Python 包分发——搬进 `.claude/skills/` 后 pip 用户那里不存在、垫图静默失效。"""
        from kinema import sheets
        for kind, names in (
            ("character", ["char_template.png"]),
            ("prop", ["prop_template.png"]),
        ):
            got = sheets.templates_for(kind)
            self.assertEqual(names, [p.name for p in got], f"{kind} 样板清单与在盘文件不一致")
            for p in got:
                self.assertTrue(p.is_file() and p.stat().st_size > 10000, f"{p.name} 不在位或是空壳")
                self.assertIn("kinema/assets/blueprints", str(p).replace("\\", "/"))
        readme = sheets.TEMPLATE_DIR.parent / "README.md"
        self.assertTrue(readme.is_file(), "assets 必须有 README 说明出处与再生路径")
        doc = readme.read_text(encoding="utf-8")
        self.assertIn("怎么重做样板图", doc)

    def test_role_sentence_ships_only_when_templates_are_really_attached(self):
        """职责声明与实附样板**逐字一致**（同简笔板防泄漏纪律）：不声明职责，
        模型会连样板的版面构件一起复制；声明了却没附，就是向模型索要不存在的参考。"""
        from kinema import sheets
        c = {"name": "阿甲", "appearance": "少年"}
        self.assertNotIn("版式样板图", sheets.char_sheet_prompt(c, "画风"))
        self.assertIn(sheets.template_role("character"),
                      sheets.char_sheet_prompt(c, "画风", n_templates=1))
        pr = {"name": "碗", "desc": "陶碗"}
        self.assertIn(sheets.template_role("prop"),
                      sheets.prop_sheet_prompt(pr, "画风", n_templates=1))
        self.assertNotIn("版式样板图", sheets.prop_sheet_prompt(pr, "画风"))
        # 措辞件套（缺一件防护就弱一档）；纯图片版式也必须显式拒绝文字层
        for token, why in (
            ("第一张是版式样板图", "必须点名是哪张（同时有 moodboard 垫图时不至于混淆）"),
            ("只准参考布局", "布局-only 是硬裁决——布局之外的一切都不许学"),
            ("只以开头的风格描述为准", "画风归属必须明确回指开头的 style_prefix"),
            ("严禁复刻", "对画风的禁令要够硬"),
            ("不是画风垫图", "要给它一个身份，否则模型按垫图对待"),
            ("线稿轮廓、空框与占位图形只是纯图形版式占位", "样板占位件不能被当作成品内容"),
            ("成品各槽位一律用对象本身的图像填满", "每个槽位必须回到对象本身"),
            ("整张设定图不生成标题、简介、信息栏、编号、尺寸数字、色号", "纯图片设定图必须拒绝文字层"),
        ):
            self.assertIn(token, sheets.template_role("character"), why)

    def test_role_sentence_is_written_per_kind(self):
        """两类样板的槽位不同，声明必须按类各写各的。共用一套文案的代价是
        串味——角色提示词里讲道具的版面。"""
        from kinema import sheets
        self.assertNotIn("那个人台的形体", sheets.template_role("prop"),
                         "道具样板上没有人，形体条款是纯噪声")
        for kind, slot in (("character", "正面与背面全身立像"),
                           ("prop", "细节框是它的局部放大")):
            self.assertIn(slot, sheets.template_role(kind))
        # 分家的真源是措辞表：两侧任何一条都不许提到另一类，否则出角色图时
        # 提示词里凭空多一句道具的版面（反之亦然）
        for kind, foreign in (("character", "道具"), ("prop", "角色")):
            for field, text in sheets._ROLE_WORDING[kind].items():
                self.assertNotIn(foreign, text,
                                 f"{kind} 的 {field} 串进了另一类的措辞")

    def test_role_sentence_names_the_face_dimensions_one_by_one(self):
        """泛称「人物」压不住脸：带脸的样板会把**那张脸**教成版式的一部分
        ——黑发白袍少年也能被画成样板上的女性面孔。样板本体是无五官线稿，
        这条属兜底；但换用带脸主体时它是唯一的拦截点，故不撤。

        媒介条款在线稿样板下反而更重：线稿是强烈的 2D 手绘信号，不显式否掉
        「把成品画成线稿」，写实 3D 项目的设定图就会被样板拉成素描。"""
        from kinema import sheets
        role = sheets.template_role("character")
        for token in ("脸型", "五官", "发型", "性别", "年龄感", "体型胖瘦"):
            self.assertIn(token, role, f"形体禁令必须逐维点名，缺「{token}」")
        self.assertIn("一笔都不许从样板取材", role)
        # 水平定位线是禁令里唯一的放行项（人台胯线正在正中，比例本身是对的），
        # 身份维度不受影响——守卫见 test_sheets.test_leg_length_is_pinned_by_landmark_lines
        self.assertIn("可以且只可以对齐它的水平定位线高度", role)
        for kind in ("character", "prop"):
            r = sheets.template_role(kind)
            self.assertIn("绝不把成品画成线稿、素描、涂鸦或未上色草图", r,
                          "媒介条款必须显式否掉线稿这一维")
            self.assertIn("同一种媒介、同一套画风语言", r)

    def test_role_sentence_sits_right_after_the_style_prefix(self):
        """**位置即权重**：职责声明必须紧跟画风前缀（第 2 句），不能埋在版式规则之后。
        埋进几十句版式规则之后时，与画风声明相隔太远，
        "别学画风"的锚定力被稀释——而样板恰恰带着完整的画风信息。"""
        from kinema import sheets
        role = sheets.template_role("character")
        p = sheets.char_sheet_prompt({"name": "甲", "appearance": "少年"},
                                     "【画风】3D国漫渲染", n_templates=1)
        head = p[:p.index(role)]
        self.assertLessEqual(head.count("，"), 2, "职责声明必须紧跟画风前缀")
        prop_role = sheets.template_role("prop")
        pr = sheets.prop_sheet_prompt({"name": "碗", "desc": "陶碗"}, "【画风】水墨",
                                      n_templates=1)
        self.assertLessEqual(pr[:pr.index(prop_role)].count("，"), 2)

    def test_gen_refs_puts_the_templates_first_and_keeps_counts_in_sync(self):
        """源级接线：样板整组垫在参考图**最前**（在 moodboard 之前，与提示词里职责
        声明的位置一致），且 `n_templates` 张数与真附上的同源。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_gen_refs)
        self.assertIn('tpls = sheets.templates_for(kind)', src)
        self.assertIn("mb_refs = [*map(str, tpls), *mb_refs]", src, "样板必须整组排在垫图之前")
        self.assertIn('n_templates=len(sheets.templates_for("character"))', src)
        self.assertIn('n_templates=0 if restricted', src,
                      "受限参考位下样板附不上，声明张数必须同步归零")
        self.assertIn('else len(sheets.templates_for("prop"))', src)
        self.assertNotIn('templates_for("scene")', src,
                         "场景没有样板，不该有这个旗标")


class TestNamedSceneInheritance(SeriesCase):
    """具名场景（`scenes[]`）的两条继承路径——与角色/道具同制度。

      · 存量章节 —— sync_design_to_chapters 的 **scene_fields 白名单**（漏登记=静默失效）；
      · 新建章节 —— create_chapter 的整份拷贝。

    这一档存在的理由：场景若塞进 props[] 冒充道具，就会套上物件转台版式——
    「孢子宫殿」能被画成蘑菇战锤。继承漏掉的话，章节层的 design_refs 看不见它，
    等于「系列登记了、每一镜都挂不上」。
    """

    def _chapter_scenes(self, cid="ch01") -> list:
        return self.ws.store.load_chapter("adp", cid).get("scenes") or []

    def test_create_chapter_copies_scenes(self):
        self.s.add_scene("神之塔大厅", desc="九根盘龙柱", keywords=["金銮殿"])
        self.s.create_chapter("第一集", cid="ch01")
        got = self._chapter_scenes()
        self.assertEqual(["神之塔大厅"], [x["name"] for x in got])
        self.assertEqual(["金銮殿"], got[0]["keywords"])
        got[0]["desc"] = "被章节改过"          # 拷贝而非共享引用
        self.assertEqual("九根盘龙柱", self.s.scenes[0]["desc"])

    def test_sync_pushes_scenes_into_existing_chapters(self):
        """建章在前、登记场景在后：sync 必须把整条推下去（新增=added）。"""
        self.s.create_chapter("第一集", cid="ch01")
        self.assertEqual([], self._chapter_scenes())
        self.s.add_scene("蘑菇森林", desc="巨型菌菇成片", keywords=["甘露菇"])
        st = self.s.sync_design_to_chapters()
        self.assertEqual(1, st["chapters"])
        self.assertEqual(["蘑菇森林"], [x["name"] for x in self._chapter_scenes()])

    def test_sync_updates_sheet_and_keywords(self):
        """设定图与关键词回填后必须同步——scene_fields 白名单漏一个就静默失效。"""
        self.s.add_scene("蘑菇森林", desc="巨型菌菇成片")
        self.s.create_chapter("第一集", cid="ch01")
        self.s.scenes[0].update({"sheet": "/x/scene_蘑菇森林.png",
                                 "keywords": ["甘露菇", "孢子森林"],
                                 "desc": "改过的描述"})
        self.s.save()
        self.s.sync_design_to_chapters()
        t = self._chapter_scenes()[0]
        self.assertEqual("/x/scene_蘑菇森林.png", t["sheet"])
        self.assertEqual(["甘露菇", "孢子森林"], t["keywords"])
        self.assertEqual("改过的描述", t["desc"])

    def test_scenes_do_not_leak_into_props(self):
        """分级铁律：具名场景绝不寄生 props[]（否则会套上物件转台版式）。"""
        self.s.add_scene("蘑菇森林", desc="巨型菌菇成片")
        self.s.add_prop("爆裂球棒", desc="金属棒球棍")
        self.s.create_chapter("第一集", cid="ch01")
        data = self.ws.store.load_chapter("adp", "ch01")
        self.assertEqual(["爆裂球棒"], [p["name"] for p in data["props"]])
        self.assertEqual(["蘑菇森林"], [x["name"] for x in data["scenes"]])

    def test_remove_scene(self):
        self.s.add_scene("蘑菇森林")
        self.s.remove_scene("蘑菇森林")
        self.assertEqual([], self.s.scenes)


# ============================================================================
# 扩展设定图（表情表 / 动作表，`project refs --expressions/--poses`）
# 与场景俯视图（与场景基准图配对出图，见 TestSceneTopview）
# ============================================================================
class TestExtensionSheets(SeriesCase):
    """扩展设定图三条契约：规格真源在 sheets.py、required_* 真被消费、版本栈接线。

    v1 刻意的边界（逐条有断言）：恒直出单张不走候选宫格；不进每镜自动挂载
    （design_refs 有 8 张硬上限）；重生/改造不触发血缘传播（无下游分镜可作废，
    传播会把全章无辜置 retake——那是要花钱重生的）。"""

    def test_aspect_and_template_registry(self):
        from kinema import sheets
        for kind in ("expression", "pose", "topview"):
            self.assertEqual(sheets.aspect_for(kind), "16:9")
            self.assertEqual([], sheets.templates_for(kind),
                             f"{kind} 无样板图（网格/制图版式由规则文本钉死）")

    def test_rules_for_covers_extension_kinds(self):
        from kinema import sheets
        expr = "，".join(sheets.rules_for("expression"))
        self.assertIn("12 格", expr)
        self.assertIn("只有面部表情不同", expr)
        pose = "，".join(sheets.rules_for("pose", {"weapon": "青霜剑"}))
        self.assertIn("15 格", pose)
        self.assertIn("青霜剑", pose, "武器要点名进差异锁（出鞘取中间态）")
        self.assertIn("中间态", pose)
        top = "，".join(sheets.rules_for("topview"))
        self.assertIn("视野锥", top)
        self.assertIn("不画机位", top)
        self.assertNotIn("**动作轴线（180° 线）**", top, "轴线是 opt-in，缺省不画")

    def test_grid_fill_required_first_and_dedup(self):
        from kinema import sheets
        got = sheets._grid_fill(["愤怒", "平静", "愤怒"], sheets.EXPRESSION_SET, 12)
        self.assertEqual(got[:2], ["愤怒", "平静"], "登记项优先入格且去重")
        self.assertEqual(len(got), 12)
        self.assertEqual(len(set(got)), 12)
        self.assertEqual(sheets._grid_fill(None, sheets.POSE_SET, 15),
                         list(sheets.POSE_SET), "未登记时默认集原样补满")

    def test_expression_prompt_consumes_required_emotions(self):
        from kinema import sheets
        c = {"name": "洛", "appearance": "银发少年", "hair": "高马尾",
             "required_emotions": ["狂喜", "隐忍"]}
        p = sheets.expression_sheet_prompt(c, "国漫画风")
        self.assertTrue(p.startswith("国漫画风"))
        grid = p.split("十二格表情从左到右", 1)[1]      # 只看网格枚举段（规则文本里也有情绪词）
        self.assertIn("狂喜", grid)
        self.assertLess(grid.index("狂喜"), grid.index("平静"),
                        "required_emotions 必须排在默认集之前——字段真被消费")

    def test_pose_prompt_arms_only_when_weapon(self):
        from kinema import sheets
        base = {"name": "洛", "appearance": "银发少年", "required_actions": ["拔剑"]}
        bare = sheets.pose_sheet_prompt(dict(base), "画风")
        self.assertNotIn("持其武器", bare)
        armed = sheets.pose_sheet_prompt({**base, "weapon": "青霜剑"}, "画风")
        self.assertIn("青霜剑", armed)
        self.assertIn("拔剑", armed)

    def test_version_ctx_supports_extension_kinds(self):
        from kinema import refine
        self.s.data["characters"] = [{"name": "洛"}]
        self.s.data["scenes"] = [{"name": "书店"}]
        _, mk, vk, _ = refine._asset_version_ctx(self.s, "expression", "洛")
        self.assertEqual((mk, vk), ("expression_sheet", "expression_versions"))
        _, mk, vk, _ = refine._asset_version_ctx(self.s, "pose", "洛")
        self.assertEqual((mk, vk), ("pose_sheet", "pose_versions"))
        _, mk, vk, _ = refine._asset_version_ctx(self.s, "topview", "书店")
        self.assertEqual((mk, vk), ("topview_sheet", "topview_versions"))
        holder, mk, vk, _ = refine._asset_version_ctx(self.s, "topview", None)
        self.assertEqual((mk, vk), ("scene_topview_ref", "scene_topview_versions"))
        self.assertIs(holder, self.s.data)

    def test_gen_refs_wiring_source_level(self):
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.cmd_gen_refs)
        # 扩展图恒直出：走 _plan_direct 而不是 _plan（候选宫格只接主设定图三类）
        for kind in ("expression", "pose", "topview"):
            self.assertIn(f'_plan_direct(top_plan, "{kind}"' if kind == "topview"
                          else f'_plan_direct(plan, "{kind}"', src)
        # 血缘只认主设定图三类——扩展图重生不许把下游分镜置 retake
        self.assertIn('if item["kind"] in ("character", "prop", "scene"):', src)
        parser = cli.build_parser()
        args = parser.parse_args(["project", "refs", "x", "--expressions", "--poses"])
        self.assertTrue(args.expressions and args.poses)
        self.assertFalse(hasattr(args, "no_topview"),
                         "俯视图与场景图恒配对出，不该再有关闭开关")

    def test_refine_skips_propagation_for_extension_kinds(self):
        import inspect
        from kinema import refine
        src = inspect.getsource(refine.refine_asset)
        self.assertIn('if kind in ("character", "prop", "scene"):', src)

    def test_gen_refs_generates_extension_sheets_with_mock(self):
        import io
        from argparse import Namespace
        from contextlib import redirect_stdout
        from kinema import cli
        self.s.data["characters"] = [{"name": "洛", "appearance": "银发少年",
                                      "required_emotions": ["狂喜"],
                                      "required_actions": ["拔剑"],
                                      "weapon": "青霜剑"}]
        self.s.data["scenes"] = [{"name": "书店", "desc": "旧书店里间"}]
        self.s.save()
        args = Namespace(id="adp", profile=None, force=False, only=None,
                         candidates=1, no_moodboard=True, concurrency=1,
                         mock=True, config=None,
                         workspace=str(Path(self.tmp.name) / "ws"),
                         expressions=True, poses=True)
        with redirect_stdout(io.StringIO()):
            cli.cmd_gen_refs(args)
        s2 = self.ws.get_project("adp")
        c = s2.characters[0]
        self.assertTrue(c.get("sheet") and Path(c["sheet"]).is_file())
        self.assertIn("char_expr_", c.get("expression_sheet") or "")
        self.assertTrue(Path(c["expression_sheet"]).is_file())
        self.assertIn("char_pose_", c.get("pose_sheet") or "")
        self.assertTrue(Path(c["pose_sheet"]).is_file())


class TestFatigueGate(SeriesCase):
    """`project refs` 的疲态闸：外貌写了黑眼圈/眼袋…而未登记 visual_requirements
    的角色不出设定图（拦在计费之前）；登记了即放行。判据与 lint 共用 fatigue_look。"""

    def _run(self, chars):
        import io
        from argparse import Namespace
        from contextlib import redirect_stdout
        from kinema import cli
        self.s.data["characters"] = chars
        self.s.save()
        args = Namespace(id="adp", profile=None, force=False, only="character",
                         candidates=1, no_moodboard=True, concurrency=1,
                         mock=True, config=None,
                         workspace=str(Path(self.tmp.name) / "ws"),
                         expressions=False, poses=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_gen_refs(args)
        return buf.getvalue(), self.ws.get_project("adp").characters

    def test_unregistered_fatigue_blocks_sheet(self):
        out, chars = self._run([{"name": "林夏", "appearance": "眼下青黑的夜班护士"}])
        self.assertIn("疲态", out)
        self.assertFalse(chars[0].get("sheet"))

    def test_registered_fatigue_passes(self):
        out, chars = self._run([{"name": "林夏", "appearance": "眼下青黑的夜班护士",
                                 "visual_requirements": ["眼下青黑"]}])
        self.assertNotIn("疲态", out)
        self.assertTrue(chars[0].get("sheet") and Path(chars[0]["sheet"]).is_file())


# ============================================================================
# 场景俯视图（与场景基准图配对：`project refs` 缺省一起出、随视频请求一起发）
# ============================================================================
class TestSceneTopview(SeriesCase):
    """一个场景两张图：基准图交代「看上去什么样」，俯视图交代「空间怎么摆」。

    六条契约各有断言：① 配对出图（不是 opt-in，`--no-topview` 才关）；② 俯视图以
    基准图为参考、故必须排在基准图落盘之后；③ 它进视频请求而不进分镜图请求，
    且**每镜至多一张**（附图配额是硬的）；④ 它不并进 `required_refs`——那份清单的
    两个下游按「分镜图真用了它」立论；⑤ `--only kind:名` 的名字过滤对无名档
    （全局固定场景）同样成立；⑥ 基准图不在盘就不排计划，缺口留给下次补。
    """

    def _refs(self, **over):
        from argparse import Namespace
        base = dict(id="adp", profile=None, force=False, only=None,
                    candidates=1, no_moodboard=True, concurrency=1,
                    mock=True, config=None,
                    workspace=str(Path(self.tmp.name) / "ws"),
                    expressions=False, poses=False)
        return Namespace(**{**base, **over})

    def _run_refs(self, **over):
        import io
        from contextlib import redirect_stdout
        from kinema import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_gen_refs(self._refs(**over))
        return buf.getvalue()

    def _seed_scene(self):
        self.s.data["scenes"] = [{"name": "书店", "desc": "旧书店里间，三排到顶的木书架"}]
        self.s.save()

    def _spy_refs(self, run):
        """跑一次生成并录下每张图的 (提示词, 实附参考图)，键为产物文件名。

        判据只能落在 provider 真正收到的入参上：提示词声明与实附清单是两处装配，
        中间还隔着按 provider 能力裁剪的一道闸。"""
        seen = {}

        class _Spy:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, k):
                return getattr(self.inner, k)

            def generate(self, prompt, out, **kw):
                seen[Path(out).name] = (prompt, list(kw.get("ref_images") or []))
                return self.inner.generate(prompt, out, **kw)

        from kinema.models import ModelRouter
        orig = ModelRouter.resolve

        def patched(self, task, profile=None, **kw):
            prov, params = orig(self, task, profile, **kw)
            return _Spy(prov), params

        ModelRouter.resolve = patched
        try:
            run()
        finally:
            ModelRouter.resolve = orig
        return seen

    # ---- ① 配对出图 ----------------------------------------------------
    def test_plain_refs_pairs_every_scene_with_a_layout_plan(self):
        self._seed_scene()
        self._run_refs()
        sc = self.ws.get_project("adp").scenes[0]
        self.assertTrue(Path(sc["sheet"]).is_file(), "基准图未出")
        self.assertIn("scene_top_", sc.get("topview_sheet") or "")
        self.assertTrue(Path(sc["topview_sheet"]).is_file(),
                        "裸 project refs 就该把俯视图一起出（不是 opt-in）")

    def test_layout_has_no_opt_out(self):
        """图纸与基准图恒配对，无关闭开关：只有基准图的场景，视频请求缺一半空间证据。"""
        from kinema import cli
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["project", "refs", "adp", "--no-topview"])

    def test_backfills_layout_for_scenes_that_already_have_a_sheet(self):
        """存量项目：基准图已在盘，裸重跑只补缺的俯视图，不重付基准图的钱。"""
        self._seed_scene()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        sheet = s2.scenes[0]["sheet"]
        s2.scenes[0].pop("topview_sheet")
        s2.save()
        self._run_refs()
        s3 = self.ws.get_project("adp")
        self.assertEqual(s3.scenes[0]["sheet"], sheet, "基准图被无谓重出了")
        self.assertTrue(Path(s3.scenes[0]["topview_sheet"]).is_file())

    def test_only_scene_regenerates_the_pair(self):
        """`--only scene:名 --force` 连带重出俯视图：重出基准图却留着按旧空间画的
        平面图，等于让视频请求同时挂两份互相矛盾的空间证据。"""
        self._seed_scene()
        self._run_refs()
        self.assertEqual(self.ws.get_project("adp").scenes[0].get("topview_versions"),
                         None, "首出不该产生归档版本")
        self._run_refs(only="scene:书店", force=True)
        sc = self.ws.get_project("adp").scenes[0]
        self.assertEqual(len(sc.get("versions") or []), 1, "基准图旧版未入版本栈")
        self.assertEqual(len(sc.get("topview_versions") or []), 1,
                         "俯视图没有跟着重出（旧版本该入栈）")

    # ---- ② 以基准图为参考，且排在它之后 --------------------------------
    def test_layout_request_carries_the_scene_sheet_as_its_reference(self):
        """两张图必须是同一个空间。让它们对齐的唯一办法是把基准图发进去——
        故第二波的计划必须在基准图落盘之后才成立。"""
        from kinema import cli, sheets
        real = cli.sheets.scene_topview_prompt

        self._seed_scene()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        sheet = s2.scenes[0]["sheet"]
        s2.scenes[0].pop("topview_sheet")
        s2.save()

        seen = self._spy_refs(self._run_refs)
        self.assertIs(real, sheets.scene_topview_prompt)
        key = next(k for k in seen if k.startswith("scene_top_"))
        prompt, refs = seen[key]
        self.assertEqual(refs, [sheet], "俯视图请求没带上该场景的基准图")
        self.assertIn("所附的**第一张图是本场景的基准图**", prompt,
                      "真附了参考却没声明职责，模型会照抄它的视角")
        # 参考图的职责是**两取一不取**：取空间内容、取画风配色，唯独不取视角。
        self.assertIn("画风与配色语言", prompt, "把基准图的画风也否掉＝两张图不成套")
        self.assertIn("唯独视角不许取", prompt)

    def test_layout_takes_the_project_art_style_but_not_its_render_words(self):
        """画风必须跟项目——两张图并排要看得出是一套（用户判据）。但画风前缀里混着
        渲染指令（写实 3D / PBR / 三点布光 / 浅景深），照单执行就是一张三维鸟瞰渲染。
        故画风照收，紧跟一句把适用范围钉死在「线条与配色」上（同 template_role 制度）。
        """
        from kinema import sheets
        pre = sheets.prefix_for("topview", "写实 3D CG 渲染")
        self.assertTrue(pre.startswith("写实 3D CG 渲染"), "画风没跟项目走")
        self.assertIn("画风绝不决定观察方式", pre, "缺适用范围声明＝拿渲染指令画平面图")
        self.assertIn("没有透视的正交平面制图", pre)
        self.assertIn(sheets.TOPVIEW_MEDIUM, pre)
        self.assertEqual(sheets.prefix_for("scene", "写实 3D CG 渲染"), "写实 3D CG 渲染",
                         "其余各类裸用画风前缀，不该被连带改")
        p = sheets.scene_topview_prompt("旧书店里间", "水彩画风")
        self.assertTrue(p.startswith("水彩画风"))
        self.assertIn("旧书店里间", p)
        for token in ("nadir view", "orthographic", "掀顶", "可通行地面",
                      "人体占地", "无文字标注"):
            self.assertIn(token, p, f"俯视图契约缺「{token}」")

    def test_layout_takes_no_style_moodboard(self):
        """垫图锁的是成片画风，图纸不吃它——生成侧与改造侧同判据。

        生成侧按行为钉：参考库生效时，图纸请求里仍只有该场景的基准图一张。"""
        import inspect

        from kinema import refine
        from kinema.studio import actions
        self.assertIn('if not no_moodboard and kind != "topview":',
                      inspect.getsource(refine.refine_asset))

        mbdir = self.s.dir / "assets" / "refs" / "moodboard"
        mbdir.mkdir(parents=True, exist_ok=True)
        mb = mbdir / "style.png"
        mb.write_bytes(b"\x89PNG\r\n")
        self.s.add_moodboard(str(mb.resolve()))
        self._seed_scene()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        sheet = s2.scenes[0]["sheet"]
        s2.scenes[0].pop("topview_sheet")
        s2.save()
        seen = self._spy_refs(lambda: self._run_refs(no_moodboard=False))
        key = next(k for k in seen if k.startswith("scene_top_"))
        self.assertEqual(seen[key][1], [sheet], "图纸吃进了画风垫图")

        with self.assertRaises(Exception):
            actions._asset_ref_holder(self.s, "topview", "书店")

    # ---- ③ 进视频请求、不进分镜图请求，且每镜至多一张 --------------------
    def test_video_refs_pair_the_primary_scene_with_its_plan(self):
        from kinema import cli, lineage
        self._seed_scene()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        proj = self._chapter_like(s2)
        shot = {"id": 1, "image_prompt": "书店里两人对峙", "characters": []}
        rows, dropped = cli._video_sheet_refs(proj, shot, cap=6)
        kinds = [k for k, _n, _p in rows]
        self.assertEqual(kinds, ["scene", "scene_top"],
                         "图纸必须紧跟它自己的基准图")
        self.assertEqual(dropped, [])
        # 图侧刻意不挂：8 张参考位挤掉一张角色设定图换一张平面图，是拿身份换空间
        self.assertNotIn(s2.scenes[0]["topview_sheet"], proj.design_refs(shot))
        self.assertEqual(lineage.primary_layout_ref(proj, shot)["key"], "scene_top:书店")

    def test_primary_layout_follows_the_shot_writing_order(self):
        """一镜绑多个取景地时，主场景取**镜内写在最前**那个。`matched_scenes` 遍历的是
        系列 `scenes[]`，返回声明顺序——两者不是一回事。
        取错的后果是给这一镜发了另一个空间的平面图，比不发更坏。"""
        from kinema import cli, lineage
        self.s.data["scenes"] = [{"name": "书店", "desc": "旧书店里间"},
                                 {"name": "码头", "desc": "夜里的货运码头"}]
        self.s.save()
        self._run_refs()
        proj = self._chapter_like(self.ws.get_project("adp"))
        shot = {"id": 1, "image_prompt": "对峙", "characters": [],
                "scenes": ["码头", "书店"]}          # 书写序与声明序刻意相反
        self.assertEqual(lineage.primary_layout_ref(proj, shot)["name"], "码头")
        rows, _dropped = cli._video_sheet_refs(proj, shot, cap=7)
        plans = [(k, n) for k, n, _p in rows if k == "scene_top"]
        self.assertEqual(plans, [("scene_top", "码头")], "图纸不是每镜一张，或取错了空间")

    def test_primary_layout_never_falls_back_to_another_scene(self):
        """主场景没有图纸时一张都不发：平面图是「镜头此刻所在空间」的证据。"""
        from kinema import cli
        self.s.data["scenes"] = [{"name": "书店", "desc": "旧书店里间"},
                                 {"name": "码头", "desc": "夜里的货运码头"}]
        self.s.save()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        next(x for x in s2.scenes if x["name"] == "码头").pop("topview_sheet")
        s2.save()
        proj = self._chapter_like(s2)
        # 同镜也命中书店（它的图纸在盘）——主场景缺图时必须一张不发，不是顺位取下一张
        shot = {"id": 1, "image_prompt": "对峙", "characters": [],
                "scenes": ["码头", "书店"]}
        rows, _dropped = cli._video_sheet_refs(proj, shot, cap=7)
        self.assertEqual([k for k, _n, _p in rows if k == "scene_top"], [],
                         "拿别处的平面图顶了主场景的缺口")

    def test_primary_layout_falls_back_to_the_fixed_scene(self):
        """一个具名取景地都没命中的镜，主场景就是全局固定场景。它的 `name` 恒是字面
        「场景」，故单独派生 `scene_top_main` 一档——套进具名模板会产出「为取景地
        「场景」的俯视布局图」这种指向无身份资产的指令。"""
        from kinema import cli
        self.s.data["scene"] = "全片同一间旧书店"
        self.s.save()
        self._run_refs()
        proj = self._chapter_like(self.ws.get_project("adp"))
        shot = {"id": 1, "image_prompt": "两人对峙", "characters": []}
        kinds = [k for k, _n, _p in cli._video_sheet_refs(proj, shot, cap=7)[0]]
        self.assertEqual(kinds, ["scene_main", "scene_top_main"])

    def test_multi_scene_shot_keeps_named_props_in_quota(self):
        """图纸在定序里排在道具之前。逐个命中场景各挂一张，作者在 `shots[].props` 里
        点名的道具就会被挤出配额，而请求照发、账照计。"""
        from kinema import cli
        self.s.data["scenes"] = [{"name": "书店", "desc": "旧书店里间"},
                                 {"name": "码头", "desc": "夜里的货运码头"}]
        self.s.data["scene"] = "全片同一座旧城"
        self.s.data["props"] = [{"name": "铜钥匙", "desc": "一枚旧铜钥匙"}]
        self.s.data["characters"] = [{"name": "洛", "desc": "灰衣少年"}]
        self.s.save()
        self._run_refs()
        proj = self._chapter_like(self.ws.get_project("adp"))
        shot = {"id": 1, "image_prompt": "对峙", "scenes": ["书店", "码头"],
                "props": ["铜钥匙"]}
        rows, dropped = cli._video_sheet_refs(proj, shot, cap=7)
        self.assertIn(("prop", "铜钥匙"), [(k, n) for k, n, _p in rows],
                      "点名的道具被图纸挤出了配额")
        self.assertEqual(dropped, [])

    # ---- ④ 不并进 required_refs -----------------------------------------
    def test_layout_never_enters_required_refs(self):
        """`required_refs` 的两个下游都按「分镜图真用了它」立论：`readiness` 据此
        报缺图、`rebaseline` 据此记血缘基线。混进去 = 存量项目每镜报「设定图不齐」，
        且「俯视图改了」被判成分镜图过期（那是要花钱重出的）。"""
        from kinema import lineage
        self._seed_scene()
        self._run_refs(no_topview=True)
        proj = self._chapter_like(self.ws.get_project("adp"))
        shot = {"id": 1, "image_prompt": "书店里两人对峙", "characters": []}
        self.assertNotIn("scene_top",
                         [r["kind"] for r in lineage.required_refs(proj, shot)])
        ok, missing = lineage.readiness(proj, shot)
        self.assertTrue(ok, f"缺俯视图不该让就绪度报缺：{missing}")

    def test_existing_chapters_receive_backfilled_layouts(self):
        """存量项目的正路：章节早就建了，之后才补图纸。章节继承是**创建时拷贝**，
        靠 `sync_design_to_chapters` 的白名单单向覆盖——`topview_sheet` 漏登记就是
        「系列里图纸都在、出视频时一张都挂不上」，且全程零告警。"""
        self._seed_scene()
        self.s.data["scene"] = "全片同一间旧书店"
        self.s.save()
        self.ws.get_project("adp").create_chapter("先建的章")
        self._run_refs()
        data = self.ws.store.load_chapter("adp", "ch01")
        sc = next(x for x in data["scenes"] if x["name"] == "书店")
        self.assertTrue(Path(sc["topview_sheet"]).is_file(), "取景地图纸没同步进存量章节")
        self.assertTrue(Path(data["scene_topview_ref"]).is_file(),
                        "全局场景图纸没同步进存量章节")

    def test_new_chapters_inherit_both_halves(self):
        self._seed_scene()
        self.s.data["scene"] = "全片同一间旧书店"
        self.s.save()
        self._run_refs()
        self.ws.get_project("adp").create_chapter("后建的章")
        data = self.ws.store.load_chapter("adp", "ch01")
        self.assertTrue(data.get("scene_ref") and data.get("scene_topview_ref"),
                        "建章拷贝只继承了基准图，本章的空间证据缺一半")

    # ---- 图纸只画空间：拍摄与调度信息一概不画 ----
    def test_layout_draws_space_only(self):
        """机位、视野锥、动作轴线、人物站位与走位路线属于「这一场戏」，而取景地跨场次
        复用——固化进场景级图纸即给所有用到它的镜头强加同一套调度。排除项须逐类显式
        点名：只声明「只画空间」压不住制图惯例，模型会按平面图的常见样式补上相机图标
        与动线箭头。"""
        from kinema import sheets
        txt = sheets.scene_topview_prompt("旧书店", "水彩画风")
        for banned in ("标出三个建议机位", "空间建立位", "人物关系位",
                       "主要出入通行路径", "把本场两个主要站位"):
            self.assertNotIn(banned, txt, f"图纸里还留着拍摄/调度指令「{banned}」")
        self.assertIn("不画机位、不画相机图标、不画视野锥或视线扇形", txt)
        self.assertIn("不画走位或通行的路线与箭头", txt)
        # 空间那一半必须齐全——去掉调度层不是把图纸掏空
        for keep in ("掀顶", "人体占地", "无文字标注", "可通行地面"):
            self.assertIn(keep, txt, f"空间条款「{keep}」被误删")

    def test_layout_rules_have_no_variant(self):
        """图纸只有一种形态：`rules_for` 不按 holder 分档，三条生成路径拿到同一份版式。"""
        from kinema import sheets
        self.assertEqual(sheets.rules_for("topview", {}),
                         sheets.rules_for("topview", {"topview_axis": True}))

    def test_dry_run_note_counts_plans_apart_from_sheets(self):
        """审阅清单把图纸单列：合成「设定图×4」时，「基准图与图纸配没配齐」在清单上
        完全看不出来，而两张缺一张正是这条路上唯一会静默发生的偏差。"""
        import inspect

        from kinema import cli
        seg = inspect.getsource(cli.stage_gen_video)
        note = seg.split("def _ref_note(")[1].split("\n    def ")[0]
        self.assertIn('f"场景俯视×{n_plans}"', note)
        self.assertIn('scene_top', note, "俯视图必须按 kind 单独计数，不能混进设定图")
        self.assertNotIn("len(sheets0)", seg, "还有调用点在按张数报，kind 丢了")
        self.assertNotIn("len(item['sheet_refs'])", seg)

    def test_only_topview_regenerates_just_the_layout(self):
        """`--only topview[:名]` 只重画图纸、不动基准图（Studio 灯箱「↻ 重新生成」走此）。"""
        self._seed_scene()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        sheet = s2.scenes[0]["sheet"]
        s2.scenes[0].pop("topview_sheet")
        s2.save()
        self._run_refs(only="topview:书店")
        sc = self.ws.get_project("adp").scenes[0]
        self.assertTrue(Path(sc["topview_sheet"]).is_file())
        self.assertEqual(sc["sheet"], sheet, "只点名图纸却连带重出了基准图")

    # ---- ⑤ --only 的名字过滤对无名档同样成立 ----------------------------
    def _seed_both(self):
        """全局固定场景与具名取景地并存——`--only kind:名` 的名字过滤只在这种项目上
        才有观测点。"""
        self._seed_scene()
        s = self.ws.get_project("adp")
        s.data["scene"] = "全片同一间旧书店"
        s.save()
        self._run_refs()

    def test_only_named_asset_leaves_the_fixed_scene_alone(self):
        """全局固定场景传 name=None。名字过滤放过无名档，就是点名重生一个取景地时
        连带重出并归档全片视觉基线那一张——Studio 灯箱的「↻ 重新生成」正走这条命令。"""
        self._seed_both()
        for only in ("topview:书店", "scene:书店"):
            with self.subTest(only=only):
                self._run_refs(only=only, force=True)
                s = self.ws.get_project("adp")
                self.assertEqual(s.data.get("scene_topview_versions") or [], [],
                                 f"--only {only} 连带重出了全局固定场景的俯视图")
                self.assertEqual(s.data.get("scene_ref_versions") or [], [],
                                 f"--only {only} 连带重出了全局固定场景的基准图")

    def test_bare_only_still_covers_the_fixed_scene(self):
        """反向：不带冒号的 `--only topview` 是「这一类全出」，无名档仍在其中。"""
        self._seed_both()
        self._run_refs(only="topview", force=True)
        s = self.ws.get_project("adp")
        self.assertEqual(len(s.data.get("scene_topview_versions") or []), 1)
        self.assertEqual(len(s.scenes[0].get("topview_versions") or []), 1)

    # ---- ⑥ 基准图不在盘就不画：盲画那张会永久占位 ------------------------
    def test_candidates_defer_the_layout_instead_of_drawing_blind(self):
        """候选模式下基准图停在 `sheet_candidates`、`sheet` 为空。此时画出来的图纸
        与最终定稿的基准图交代的不是同一个空间，而它一旦落盘就占住 `topview_sheet`
        ——跳过判据只看文件在不在，这一对图再没有对齐的机会。"""
        self._seed_scene()
        out = self._run_refs(candidates=2)
        self.assertIsNone(self.ws.get_project("adp").scenes[0].get("topview_sheet"),
                          "基准图还没定稿就画了图纸")
        self.assertIn("本轮不画", out, "跳过了却不说，用户无从知道要补")

    def test_candidates_force_does_not_redraw_from_the_outgoing_sheet(self):
        """`--force --candidates N` 重出基准图时不归档、也不改 `sheet`，旧文件照样在盘。
        只问「基准图在不在」就会拿一张正要被替换掉的图去画图纸——画完占住
        `topview_sheet`，定稿后再也不会自动重画。"""
        self._seed_scene()
        self._run_refs()
        top = self.ws.get_project("adp").scenes[0]["topview_sheet"]
        out = self._run_refs(only="scene:书店", force=True, candidates=3)
        s2 = self.ws.get_project("adp")
        self.assertEqual(s2.scenes[0].get("topview_versions") or [], [],
                         "拿即将被替换的旧基准图重画了图纸")
        self.assertEqual(s2.scenes[0]["topview_sheet"], top)
        self.assertIn("保持原样", out, "跳过了却不说，用户不知道要 --force 补")

    def test_layout_is_backfilled_after_the_sheet_is_picked(self):
        """接上条:定稿后裸重跑就把图纸补出来,且真拿定稿那张当参考——引擎打印的
        补救口径必须为真,否则用户照做一次仍然拿不到对齐的两张图。"""
        from kinema import refine
        self._seed_scene()
        self._run_refs(candidates=2)
        refine.pick_asset_candidate(self.ws, "adp", kind="scene", name="书店", no=1)
        sheet = self.ws.get_project("adp").scenes[0]["sheet"]
        seen = self._spy_refs(self._run_refs)
        key = next(k for k in seen if k.startswith("scene_top_"))
        self.assertEqual(seen[key][1], [sheet], "补出来的图纸没拿定稿基准图当参考")
        self.assertTrue(Path(self.ws.get_project("adp").scenes[0]["topview_sheet"]).is_file())

    def test_force_without_a_sheet_keeps_the_existing_layout(self):
        """归档是移动文件且标准字段路径不变。就绪判定写在归档之后，等于把在盘的
        旧图纸移进版本栈、字段指向一个已不存在的文件——重生没发生，资产没了。"""
        self._seed_scene()
        self._run_refs()
        s2 = self.ws.get_project("adp")
        top = s2.scenes[0]["topview_sheet"]
        s2.scenes[0].pop("sheet")                     # 基准图丢了（换机未同步/云端未拉取）
        s2.save()
        self._run_refs(only="topview:书店", force=True)
        s3 = self.ws.get_project("adp")
        self.assertTrue(Path(top).is_file(), "旧图纸被归档移走了，而重生并没有发生")
        self.assertEqual(s3.scenes[0].get("topview_versions") or [], [])

    def test_binding_clause_forbids_filming_from_above(self):
        """图纸不是画面。少了「本镜仍按自己的机位拍」这半句，模型会照它出一段俯拍。"""
        from kinema.pipeline import prompts
        zh = prompts.sheet_binding_clause(
            [("frame", ""), ("scene", "书店"), ("scene_top", "书店")])
        self.assertIn("@图片3 为取景地「书店」的俯视布局图", zh)
        self.assertIn("绝不改成俯视视角", zh)
        self.assertIn("不影响画风", zh)
        en = prompts.sheet_binding_clause(
            [("frame", ""), ("scene_top_main", "场景")], lang="en")
        self.assertIn("top-down layout plan of the film's fixed location", en)
        self.assertIn("never switch to a top-down view", en)

    @staticmethod
    def _chapter_like(series):
        """把系列文档包成 `Project` 形状供 lineage/cli 取材（章节继承的就是这份拷贝）。"""
        from kinema.project import Project
        return Project(series.ws.root / "adp" / "project.json", series.data)
