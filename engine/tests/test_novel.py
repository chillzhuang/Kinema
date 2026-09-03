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

"""原创小说创作层（novel）守卫。

钉死的口径：
  · 登记幂等（同内容不叠版本）+ 旧稿归档（移动·v 升序·相对路径）；
  · 伏笔「超期」恒为派生判定**绝不落盘**（落了会与最新章号脱钩）；
  · lint 纯计算零落盘（project.json 字节级不变）；
  · 里程碑按已登记章数每 10 章触发；
  · 实体命中沿用「≥2 字才命中 / keywords 兜底」口径（与 Project._matched_entities 同族）；
  · 文字人设四件（speech_style/personality/arc/taboo_lines）**进** sync 白名单
    （系列→章节推送存量章节）、**绝不进** upsert_entities（重抽不覆盖）；
  · scanner 消费的 novel.view 纯只读（不 mutate 入参）；
  · manuscript 目录纪律：无 `_work` 后缀（防 Studio 片库误收）、契约路径工作区相对。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import LocalBackendEnv


def _mkseries(tmp: Path, pid: str = "nv"):
    from kinema.workspace import Workspace
    ws = Workspace.open(str(tmp))
    return ws, ws.create_project("小说守卫", pid=pid)


def _write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


class NovelCase(unittest.TestCase):
    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()
        self._env.restore()


class TestSaveChapter(NovelCase):
    def test_save_registers_and_is_idempotent(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        r1 = N.save_chapter(s, no=1, text="第一章正文。", title="开局", digest="事件+尾钩")
        self.assertFalse(r1["noop"])
        self.assertEqual(r1["file"], "manuscript/ch0001.md")   # 契约路径工作区相对
        self.assertTrue(r1["sha256"].startswith("sha256:"))
        # 同内容重跑 = 幂等：不叠版本、registry 不长
        r2 = N.save_chapter(s, no=1, text="第一章正文。")
        self.assertTrue(r2["noop"])
        entry = N.find_entry(s, 1)
        self.assertNotIn("versions", entry)
        self.assertEqual(len((s.data.get("novel") or {}).get("chapters")), 1)

    def test_rewrite_archives_old_version(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="初稿。")
        r = N.save_chapter(s, no=1, text="重写稿，更长一些。")
        self.assertIsNotNone(r["archived"])
        self.assertEqual(r["archived"]["v"], 1)
        vfile = s.dir / r["archived"]["file"]
        self.assertTrue(vfile.is_file())
        self.assertEqual(vfile.read_text(encoding="utf-8"), "初稿。")
        # 标准路径仍是新稿；registry 记录归档
        self.assertEqual((s.dir / "manuscript" / "ch0001.md").read_text(encoding="utf-8"),
                         "重写稿，更长一些。")
        self.assertEqual(len(N.find_entry(s, 1)["versions"]), 1)
        # 归档路径必须工作区相对（同 source/study 纪律，绝对路径会被 oss 收录）
        self.assertFalse(r["archived"]["file"].startswith("/"))

    def test_manuscript_dir_has_no_work_suffix(self):
        # scanner.rglob("*_work") 是片库扫描入口——正文目录带 _work 会被当成片收录
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        d = N.manuscript_dir(s)
        self.assertFalse(d.name.endswith("_work"))

    def test_empty_text_rejected(self):
        from kinema import novel as N
        from kinema.errors import ProjectError
        ws, s = _mkseries(self.tmp)
        with self.assertRaises(ProjectError):
            N.save_chapter(s, no=1, text="   \n ")

    def test_milestone_fires_every_ten(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for i in range(1, 10):
            r = N.save_chapter(s, no=i, text=f"第{i}章。")
            self.assertFalse(r["checkpoint"], f"第 {i} 章不该触发检查点")
        r = N.save_chapter(s, no=10, text="第10章。")
        self.assertTrue(r["checkpoint"])


class TestEntityMentions(NovelCase):
    def test_two_char_rule_and_keywords(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘", appearance="黑发少年")
        s.add_character("刀")                       # 单字名：绝不裸命中
        s.add_prop("爆裂球棒", keywords=["球棒"])
        hits = N.entity_mentions(s, "孙缘挥起球棒，刀光一闪。")
        self.assertIn("孙缘", hits["characters"])
        self.assertNotIn("刀", hits["characters"])   # 「刀光」不算命中角色「刀」
        self.assertIn("爆裂球棒", hits["props"])     # keywords 兜底


class TestThreads(NovelCase):
    def test_cli_thread_add_passes_tier_through(self):
        """走 argparse 的全链路守卫：`--tier short` 必须落 tier 且按档推导 due。
        模块函数全对而 CLI 漏传实参时，只有走 build_parser 的用例会红——
        指挥层照 SKILL 文档跑的正是这条命令行，丢参的落盘结果与回执
        （「无期限」）恰好与用户输入相反。"""
        from kinema.cli import build_parser
        ws, s = _mkseries(self.tmp)
        args = build_parser().parse_args(
            ["novel", "thread-add", "nv", "--title", "主角身世",
             "--setup", "3", "--tier", "short", "--workspace", str(self.tmp)])
        args.func(args)
        t = ws.get_project("nv").data["threads"][0]
        self.assertEqual((t["tier"], t["due"]), ("short", 33))

    def test_thread_set_tier_derives_due(self):
        """thread-set 定档与登记同一套推导——lint 对无期限伏笔的出路提示正是
        「thread-set --tier 定档给它一个期限」，只记档不给期限即原地打转。
        显式给过 due 的不动，long 无跨度恒不造期限。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.thread_add(s, title="远山的名字", setup=5)          # 登记时没定档
        t = N.thread_set(s, "th01", tier="short")
        self.assertEqual((t["tier"], t["due"]), ("short", 35))
        N.thread_add(s, title="旧约", setup=1, due=9)
        t2 = N.thread_set(s, "th02", tier="mid")
        self.assertEqual((t2["tier"], t2["due"]), ("mid", 9))  # 显式期限优先
        N.thread_add(s, title="终局悬念", setup=2)
        t3 = N.thread_set(s, "th03", tier="long")
        self.assertIsNone(t3["due"])                           # 恒进长期挂起统计

    def test_lifecycle_and_derived_expiry(self):
        from kinema import novel as N
        from kinema.errors import ProjectError
        ws, s = _mkseries(self.tmp)
        t = N.thread_add(s, title="木马起疑", setup=1, due=2)
        self.assertEqual((t["id"], t["status"]), ("th01", "open"))
        with self.assertRaises(ProjectError):        # 回收必须给章号
            N.thread_mark(s, "th01", status="paid")
        with self.assertRaises(ProjectError):        # 期限不能早于埋设
            N.thread_add(s, title="旧约", setup=5, due=3)
        # 写到第 3 章 → 派生超期；但**落盘的条目里绝不出现 expired 键**
        for i in (1, 2, 3):
            N.save_chapter(s, no=i, text=f"第{i}章。")
        tv = N.threads_view(s)
        self.assertEqual([x["id"] for x in tv["expired"]], ["th01"])
        stored = json.loads((s.dir / "project.json").read_text(encoding="utf-8"))
        self.assertNotIn("expired", stored["threads"][0])
        # 回收后不再超期
        N.thread_mark(s, "th01", status="paid", paid_in=3)
        tv2 = N.threads_view(s)
        self.assertEqual(tv2["expired"], [])
        self.assertEqual(tv2["paid"][0]["paid_in"], 3)

    def test_studio_action_same_write_path(self):
        from kinema import novel as N
        from kinema.studio import actions
        ws, s = _mkseries(self.tmp)
        N.thread_add(s, title="网页记账", setup=1)
        r = actions.novel_thread(self.tmp, "nv", tid="th01", status="paid", paid_in=2)
        self.assertEqual(r["thread"]["status"], "paid")
        fresh = json.loads((s.dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(fresh["threads"][0]["paid_in"], 2)


class TestLint(NovelCase):
    def test_lint_is_pure_and_fires_expected_codes(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘", appearance="黑发少年")
        with s.commit():
            s.data["narrative_style"] = {"avoid": ["总之"], "baseline": []}
        # 第 1 章提及孙缘，此后连写 11 章不再出场 → char_absent；末章带忌讳词
        N.save_chapter(s, no=1, text="孙缘登场。", digest="d")
        for i in range(2, 12):
            N.save_chapter(s, no=i, text=f"第{i}章无主角。", digest="d")
        N.save_chapter(s, no=12, text="总之，结束了。")   # 无 digest + 忌讳词
        N.thread_add(s, title="超期伏笔", setup=1, due=2)
        before = (s.dir / "project.json").read_bytes()
        rep = N.lint(s)
        after = (s.dir / "project.json").read_bytes()
        self.assertEqual(before, after, "lint 必须纯计算零落盘")
        codes = {f["code"] for f in rep["findings"]}
        for want in ("digest_missing", "thread_expired", "style_avoid",
                     "char_absent", "no_baseline", "state_missing"):
            self.assertIn(want, codes, f"lint 应报 {want}: {rep['findings']}")
        self.assertTrue(json.dumps(rep, ensure_ascii=False))   # 结论必为合法 JSON


class TestStateSnapshot(NovelCase):
    def test_state_whitelist(self):
        from kinema import novel as N
        from kinema.errors import ProjectError
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="第一章。")
        with self.assertRaises(ProjectError):
            N.set_state(s, 1, {"未知键": 1})
        with self.assertRaises(ProjectError):
            N.set_state(s, 1, {"characters": ["不是映射"]})
        N.set_state(s, 1, {"time": "当晚", "characters": {"孙缘": "负伤"},
                           "hooks": ["徽章异动"]})
        self.assertEqual(N.find_entry(s, 1)["state"]["time"], "当晚")


class TestPersonaFieldsContract(NovelCase):
    """文字人设四件的两条流转纪律（与 M8 五字段同构）。"""

    FIELDS = ("speech_style", "personality", "arc", "taboo_lines")

    def test_sync_pushes_to_existing_chapters(self):
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘", appearance="黑发少年")
        s.create_chapter("第一集", cid="ch01")        # 先建章（此时无文字人设）
        with s.commit():
            c = s.characters[0]
            c["speech_style"] = "短句冷淡，从不解释第二遍"
            c["personality"] = "谨慎"
            c["arc"] = "求生→求真"
            c["taboo_lines"] = ["绝不先动手"]
        s.sync_design_to_chapters()
        ch = ws.store.load_chapter("nv", "ch01")
        got = next(x for x in ch["characters"] if x["name"] == "孙缘")
        for k in self.FIELDS:
            self.assertEqual(got[k], s.characters[0][k],
                             f"{k} 必须经 char_fields 白名单推送存量章节")

    def test_upsert_entities_never_clobbers_persona(self):
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘", appearance="黑发少年")
        with s.commit():
            s.characters[0]["speech_style"] = "短句冷淡"
            s.characters[0]["taboo_lines"] = ["绝不先动手"]
        # 重抽带同名角色，甚至恶意携带 persona 字段——一律不覆盖人工创作
        s.upsert_entities(characters=[{"name": "孙缘", "appearance": "重抽外貌",
                                       "speech_style": "话痨", "taboo_lines": []}])
        c = s.characters[0]
        self.assertEqual(c["appearance"], "重抽外貌")        # 抽取拥有字段正常更新
        self.assertEqual(c["speech_style"], "短句冷淡")       # 人工字段纹丝不动
        self.assertEqual(c["taboo_lines"], ["绝不先动手"])


class TestScannerView(NovelCase):
    def test_view_is_readonly_and_shapes(self):
        from kinema import novel as N
        data = {"threads": [{"id": "th01", "title": "x", "setup": 1, "due": 1,
                             "status": "open"}]}
        v = N.view(data)
        self.assertNotIn("novel", data, "view 绝不 mutate 入参（scanner 只读纪律）")
        self.assertEqual(v["count"], 0)
        self.assertEqual(v["next_checkpoint"], N.MILESTONE_EVERY)
        # 无登记章 → current_no=0 → due=1 未越过，不算超期
        self.assertEqual(v["threads"]["expired"], [])

    def test_script_detail_ships_novel_block(self):
        from kinema import novel as N
        from kinema.studio import scanner
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="第一章。", digest="d")
        d = scanner.script_detail(self.tmp, None, "nv")
        self.assertEqual(d["novel"]["count"], 1)
        self.assertIn("arcs", d["novel"], "创作 Tab 要下发卷规划（大纲进度条）")
        c = scanner.novel_chapter(self.tmp, "nv", 1)
        self.assertEqual(c["text"], "第一章。")
        self.assertIsNone(scanner.novel_chapter(self.tmp, "nv", 99))


# ---------------------------------------------------------------------------
# 设定的实时更新（character/prop/scene set）
# 设定是边写边长的（弧光推进/换装/断剑/新绰号），在此之前更新既有实体只有
# 「手改 project.json」一条路——那正是本仓库付过学费的写法（引擎长任务的旧内存
# 副本整份覆写 / mysql 库行较新在 load 之前就把文件盖掉）。
# ---------------------------------------------------------------------------
class TestEntitySetters(NovelCase):
    def test_updates_text_fields_and_rejects_unknown(self):
        from kinema.errors import ProjectError
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘", appearance="黑发少年")
        s.set_character("孙缘", speech_style="短句冷淡", arc="求生→求真",
                        taboo_lines=["绝不先动手"], keywords=["小疤"])
        c = next(x for x in s.characters if x["name"] == "孙缘")
        self.assertEqual(c["speech_style"], "短句冷淡")
        self.assertEqual(c["taboo_lines"], ["绝不先动手"])
        self.assertEqual(c["appearance"], "黑发少年", "没点名的字段不许被清空")
        with self.assertRaises(ProjectError):
            s.set_character("查无此人", arc="x")
        with self.assertRaises(ProjectError):
            s.set_character("孙缘", 外号="野路子")

    def test_engine_managed_fields_are_not_settable(self):
        """sheet/ref_image/audition 是引擎回填的产物字段——换图走版本栈那套
        （refs --force / refine / rollback），绝不能从设定命令上开个后门。"""
        from kinema.errors import ProjectError
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘")
        s.add_prop("断剑")
        s.add_scene("钟楼")
        for kw in ({"sheet": "x.png"}, {"ref_image": "y.png"}, {"audition": []},
                   {"voice_locked": True}, {"versions": []}):
            with self.assertRaises(ProjectError, msg=f"{kw} 不该可设"):
                s.set_character("孙缘", **kw)
        with self.assertRaises(ProjectError):
            s.set_prop("断剑", sheet="x.png")
        with self.assertRaises(ProjectError):
            s.set_named_scene("钟楼", sheet="x.png")

    def test_write_goes_through_commit_not_bare_save(self):
        """两个手柄各持一份副本时都不许丢更新——这是 commit()（进程锁 + 进锁后
        重新加载）与裸 save（无合并整份覆写）的分水岭。"""
        ws, s1 = _mkseries(self.tmp)
        s1.add_character("孙缘")
        s1.add_character("木马")
        s2 = ws.get_project("nv")          # 第二个手柄：此刻两份副本都是干净的
        s1.set_character("孙缘", arc="求生→求真")
        s2.set_character("木马", arc="忠仆→叛徒")   # s2 手里的是改前的旧副本
        fresh = json.loads((s1.dir / "project.json").read_text(encoding="utf-8"))
        got = {c["name"]: c.get("arc") for c in fresh["characters"]}
        self.assertEqual(got, {"孙缘": "求生→求真", "木马": "忠仆→叛徒"},
                         "后写的那次不许把先写的整段抹掉")

    def test_keywords_reach_existing_chapters(self):
        """角色别名进 sync 白名单——漏登记就是「系列填了、章节看不见」。"""
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘")
        s.create_chapter("第一集", cid="ch01")
        s.set_character("孙缘", keywords=["小疤", "那小子"])
        s.sync_design_to_chapters()
        ch = ws.store.load_chapter("nv", "ch01")
        got = next(x for x in ch["characters"] if x["name"] == "孙缘")
        self.assertEqual(got["keywords"], ["小疤", "那小子"])

    def test_prop_and_scene_setters(self):
        ws, s = _mkseries(self.tmp)
        s.add_prop("断剑", desc="半截铁剑")
        s.add_scene("钟楼", desc="城中最高处")
        s.set_prop("断剑", desc="剑身齐中断裂，缠了布条", keywords=["半截剑"])
        s.set_named_scene("钟楼", keywords=["钟塔"])
        self.assertEqual(s.props[0]["desc"], "剑身齐中断裂，缠了布条")
        self.assertEqual(s.scenes[0]["keywords"], ["钟塔"])
        self.assertEqual(s.scenes[0]["desc"], "城中最高处")

    def test_cli_list_arg_semantics(self):
        """列表字段的统一口径：`--x` 整体替换 · `--add-x` 并集追加 · 都没给=不动。"""
        from kinema.cli import _list_arg
        self.assertIsNone(_list_arg(None, None, ["旧"]))
        self.assertEqual(_list_arg(["新"], None, ["旧"]), ["新"])
        self.assertEqual(_list_arg(None, ["新"], ["旧"]), ["旧", "新"])
        self.assertEqual(_list_arg(None, ["旧"], ["旧"]), ["旧"], "并集不重复")
        self.assertEqual(_list_arg(["a", "a", "b"], None, None), ["a", "b"])


# ---------------------------------------------------------------------------
# 卷/幕规划（arcs）：长篇的「大纲」落点
# ---------------------------------------------------------------------------
class TestArcs(NovelCase):
    def test_upsert_validation_and_partial_update(self):
        from kinema import novel as N
        from kinema.errors import ProjectError
        ws, s = _mkseries(self.tmp)
        with self.assertRaises(ProjectError):
            N.arc_upsert(s, no=1, frm=1)                 # 新建缺 title
        with self.assertRaises(ProjectError):
            N.arc_upsert(s, no=1, title="第一卷")        # 新建缺 from
        with self.assertRaises(ProjectError):
            N.arc_upsert(s, no=1, title="第一卷", frm=10, to=3)
        a = N.arc_upsert(s, no=1, title="第一卷", frm=1, to=10, goal="查徽章")
        self.assertTrue(a["created"])
        b = N.arc_upsert(s, no=1, climax="钟楼对峙")     # 局部更新不清空既有字段
        self.assertFalse(b["created"])
        self.assertEqual(b["title"], "第一卷")
        self.assertEqual(b["goal"], "查徽章")
        self.assertEqual(len(s.data["arcs"]), 1)

    def test_progress_is_derived_never_persisted(self):
        """与伏笔「超期」同纪律：写到哪一卷由最新章号现算，落盘=与章号脱钩的僵尸标记。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.arc_upsert(s, no=1, title="第一卷", frm=1, to=3)
        N.save_chapter(s, no=1, text="一。")
        raw = json.loads((s.dir / "project.json").read_text(encoding="utf-8"))
        self.assertNotIn("state", raw["arcs"][0], "进度态绝不落盘")
        self.assertEqual(N.arcs_view(s)["arcs"][0]["state"], "writing")
        for i in (2, 3):
            N.save_chapter(s, no=i, text=f"{i}。")
        self.assertEqual(N.arcs_view(s)["arcs"][0]["state"], "done")

    def test_coverage_gaps_and_overlaps(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.arc_upsert(s, no=1, title="一", frm=1, to=10)
        N.arc_upsert(s, no=2, title="二", frm=13, to=20)     # 断档 11~12
        N.arc_upsert(s, no=3, title="三", frm=18, to=25)     # 与卷二重叠 18~20
        v = N.arcs_view(s)
        self.assertEqual([g["at"] for g in v["gaps"]], [[11, 12]])
        self.assertEqual([o["at"] for o in v["overlaps"]], [[18, 20]])
        self.assertEqual(N.arc_at(s.data, 5)["no"], 1)
        self.assertIsNone(N.arc_at(s.data, 11))

    def test_lint_reports_arc_coverage(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="一。", digest="d")
        codes = {f["code"] for f in N.lint(s)["findings"]}
        self.assertIn("arc_missing", codes, "无卷规划 = 检查点第一门无对照物")
        N.arc_upsert(s, no=1, title="一", frm=1, to=1)
        codes = {f["code"] for f in N.lint(s)["findings"]}
        self.assertIn("arc_done", codes, "本章收卷要提示做卷末复盘")


# ---------------------------------------------------------------------------
# 文体量化（AI 味的可测量面）——引擎只出数，判定权在指挥层
# ---------------------------------------------------------------------------
class TestProseStats(NovelCase):
    def test_every_slop_term_ships_a_rewrite_hint(self):
        """同 variation.SLOP_TERMS 纪律：只指出「这里有 AI 味」不构成可执行反馈，
        每条都必须带可执行的物理化改写方向。"""
        from kinema import novel as N
        self.assertGreaterEqual(len(N.PROSE_SLOP), 20)
        for term, hint in N.PROSE_SLOP.items():
            self.assertTrue(term.strip() and len(hint.strip()) >= 2,
                            f"{term} 缺改写建议")

    def test_stats_shape_and_finite(self):
        from kinema import novel as N
        ps = N.prose_stats("他不禁后退。\n\n「走。」她说。\n\n夜很长，长得像一条没有尽头的巷子。")
        for k in ("avg_sentence_len", "sd_ratio", "dialogue_ratio", "long_ratio"):
            self.assertTrue(isinstance(ps[k], float) and ps[k] == ps[k])
        self.assertTrue(any(x["term"] == "不禁" for x in ps["slop"]))
        self.assertGreater(ps["dialogue_ratio"], 0, "引号内计入对白占比")
        self.assertEqual(N.prose_stats("")["sentences"], 0)      # 空文本不炸
        json.dumps(ps, ensure_ascii=False, allow_nan=False)      # 结论必为合法 JSON

    def test_dialogue_counts_all_three_quote_styles(self):
        """真实书稿里一章 294 个直双引号、0 个「」——只认直角/弯引号会把
        对白占比算成 2% 并误报「几乎全是叙述」。"""
        from kinema import novel as N
        for pair in ('"你为什么开播。"', "「你为什么开播。」", "“你为什么开播。”"):
            ps = N.prose_stats(f"他停了一下。\n\n{pair}他说。")
            self.assertGreater(ps["dialogue_ratio"], 0.2, f"{pair} 没被算成对白")
        # 落单的引号不许吞掉整篇（成对匹配且不跨行）
        self.assertLess(N.prose_stats('他说了一句"。\n\n然后走了。')["dialogue_ratio"], 0.2)

    def test_repeat_skips_digit_only_windows(self):
        """标点被剥掉后「07:33」「11:00」会粘成 07331100——带面板/时钟的题材里
        天天重复却毫无文体意义。"""
        from kinema import novel as N
        line = "当前时间 07:33，剩余 11:00。"
        out = N.repeat_phrases([line + "\n" + line + "\n" + line])
        self.assertFalse([x for x in out if not x["phrase"].strip("0123456789")],
                         f"纯数字窗口不该进榜: {out}")

    def test_repeat_phrases_extend_to_whole_sentence(self):
        """位移一字的滑窗碎片不许刷满整张表——报的必须是「复读的是哪句话」。"""
        from kinema import novel as N
        line = "他缓缓抬起头，空气仿佛凝固。"
        out = N.repeat_phrases([line + "\n" + line, line + "\n别的句子在这里。"])
        self.assertEqual(len(out), 1, f"同一句只该报一条: {out}")
        self.assertEqual(out[0]["n"], 3)
        self.assertIn("空气仿佛凝固", out[0]["phrase"])
        self.assertGreater(len(out[0]["phrase"]), N.REPEAT_N, "必须最大延伸")
        self.assertEqual(N.repeat_phrases(["就一句话在这里出现一次。"]), [])

    def test_lint_window_is_bounded_and_pure(self):
        """文体扫描必须有窗（百章级项目上全书扫是 O(全书)）；账目类检查恒看全书。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        with s.commit():
            s.data["narrative_style"] = {"avoid": ["总之"], "baseline": ["样"]}
        N.save_chapter(s, no=1, text="总之，很久以前。", digest="d")
        for i in range(2, 14):
            N.save_chapter(s, no=i, text=f"第{i}章干净的正文。", digest="d")
        before = (s.dir / "project.json").read_bytes()
        rep = N.lint(s)
        self.assertEqual(before, (s.dir / "project.json").read_bytes(),
                         "lint 必须纯计算零落盘")
        self.assertEqual(rep["window"], [4, 13])
        self.assertNotIn("style_avoid", {f["code"] for f in rep["findings"]},
                         "第 1 章在默认窗口外，不该被扫到")
        rep2 = N.lint(s, frm=1, to=1)
        self.assertIn("style_avoid", {f["code"] for f in rep2["findings"]})
        self.assertEqual(rep2["chapters"], 13, "章数统计恒看全书，不随窗口缩水")

    def test_slop_findings_are_capped(self):
        """口癖榜刷屏会把其余体检结论淹掉——lint 只报最狠的几条 + 一行余量汇总。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        text = "。".join(f"他{t}地做了什么" for t in list(N.PROSE_SLOP)[:12]) + "。"
        N.save_chapter(s, no=1, text=text * 3, digest="d")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertLessEqual(codes.count("prose_slop"), N.SLOP_TOP)
        self.assertIn("prose_slop_more", codes)


# ---------------------------------------------------------------------------
# 两个只读取料出口：写前必读包 brief / 批次复核物料 recap
# ---------------------------------------------------------------------------
class TestBriefAndRecap(NovelCase):
    def _seed(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘", appearance="黑发少年")
        s.add_character("木马", appearance="独眼老兵")
        s.set_character("孙缘", speech_style="短句冷淡", taboo_lines=["绝不先动手"])
        N.arc_upsert(s, no=1, title="第一卷", frm=1, to=10, goal="查徽章")
        for i in range(1, 4):
            N.save_chapter(s, no=i, text=f"第{i}章：孙缘走进巷子。", digest=f"第{i}章梗概")
        N.set_state(s, 3, {"time": "当夜", "characters": {"孙缘": "负伤"},
                           "hooks": ["钟为何三响"]})
        N.thread_add(s, title="徽章来历", setup=1, due=2)
        return ws, s

    def test_brief_packs_what_the_next_chapter_needs(self):
        from kinema import novel as N
        ws, s = self._seed()
        b = N.brief(s)
        self.assertEqual(b["no"], 4)
        self.assertEqual(b["arc"]["no"], 1)
        self.assertEqual(b["prev"]["no"], 3)
        self.assertEqual(b["prev"]["state"]["time"], "当夜")
        self.assertEqual([c["name"] for c in b["characters"]], ["孙缘"],
                         "默认只取上一章在场者，不是整张角色表")
        self.assertTrue(b["expired_threads"], "超期伏笔要顶到写前必读里")
        self.assertEqual(b["characters"][0]["taboo_lines"], ["绝不先动手"])
        self.assertNotIn("sheet", b["characters"][0], "人设卡只装文字设定")
        b2 = N.brief(s, chars=["木马", "查无此人"])
        self.assertEqual([c["name"] for c in b2["characters"]], ["木马"])
        self.assertEqual(b2["unknown_chars"], ["查无此人"])
        json.dumps(b, ensure_ascii=False, allow_nan=False)

    def test_unregistered_name_in_state_is_reported_not_fatal(self):
        """上一章 state 里点到、角色表里却没有的名字（临时 NPC/星神 ID）在长篇里
        几乎必然出现——直接取卡会 KeyError 中断整条取料链。必须过滤后当漏登记报。"""
        from kinema import novel as N
        ws, s = self._seed()
        N.set_state(s, 3, {"characters": {"孙缘": "负伤", "不打烊的灯": "只打了一枚"}})
        b = N.brief(s)
        self.assertEqual([c["name"] for c in b["characters"]], ["孙缘"])
        self.assertEqual(b["unknown_chars"], ["不打烊的灯"])

    def test_brief_does_not_mutate(self):
        from kinema import novel as N
        ws, s = self._seed()
        before = (s.dir / "project.json").read_bytes()
        N.brief(s)
        N.recap(s)
        self.assertEqual(before, (s.dir / "project.json").read_bytes())

    def test_recap_counts_every_chapter_in_range(self):
        """《批次报告》的逐章概要必须是数出来的——漏一章，用户拿到的就是一份
        看着完整实则失真的报告。"""
        from kinema import novel as N
        ws, s = self._seed()
        N.thread_mark(s, "th01", status="paid", paid_in=3)
        r = N.recap(s, frm=1, to=3)
        self.assertEqual([c["no"] for c in r["chapters"]], [1, 2, 3])
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["missing_state"], [1, 2])
        self.assertEqual([t["id"] for t in r["threads"]["opened"]], ["th01"])
        self.assertEqual([t["id"] for t in r["threads"]["paid"]], ["th01"])
        self.assertEqual([x["name"] for x in r["new_entities"]["characters"]], ["孙缘"])
        self.assertEqual(r["arcs"][0]["no"], 1)
        json.dumps(r, ensure_ascii=False, allow_nan=False)

    def test_recap_pacing_counts_declared_payoffs(self):
        """节奏账第⑦门吃的是登记条目的 payoff——精简视图 rows 没这个键，
        从它取数会让「声明数」恒为 0、《批次报告》整门静默消失。"""
        from kinema import novel as N
        ws, s = self._seed()
        N.save_chapter(s, no=2, text="第2章：巷战重写。", payoff="major",
                       payoff_kind="反转")
        r = N.recap(s, frm=1, to=3)
        self.assertEqual(r["pacing"]["declared"], 1)
        self.assertEqual(r["pacing"]["by_level"], {"major": 1})

    def test_recap_new_entities_are_first_appearance_only(self):
        from kinema import novel as N
        ws, s = self._seed()
        N.save_chapter(s, no=4, text="孙缘又走进巷子，木马拦住了他。", digest="d")
        r = N.recap(s, frm=4, to=4)
        names = [x["name"] for x in r["new_entities"]["characters"]]
        self.assertEqual(names, ["木马"], "孙缘早在第 1 章登场，不算本批新登场")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 商用化守卫
# ---------------------------------------------------------------------------
class TestStripMarkup(NovelCase):
    """markdown 剥离：文体面的单一入口，**账目面绝不走**。"""

    def test_chapter_heading_is_not_a_paragraph(self):
        from kinema import novel as N
        # 错误复现：连着十章都带 `# 第三百四十X章`，标题行被当段落时会刷出
        # 「9 个段落以「第三百四」开头」这种纯噪声，把真口癖挤出榜单
        texts = [f"# 第三百四十{i} 章 · 无价\n\n"
                 f"---\n\n他把碗推过去，第{i}次。\n\n她没接。\n"
                 for i in range(1, 11)]
        joined = "\n".join(N.strip_markup(t) for t in texts)
        ps = N.prose_stats(joined)
        heads = [k for k, _ in ps["head_repeats"]]
        self.assertFalse([k for k in heads if "第三百四" in k],
                         f"章标题行仍被当成段落: {ps['head_repeats']}")
        # `---` 不计段
        self.assertNotIn("---", N.strip_markup("---\n正文"))

    def test_bold_paragraph_gets_the_same_head_key_as_plain(self):
        from kinema import novel as N
        plain = "他把碗推过去。\n她没接。\n"
        bold = "**他把碗推过去。**\n**她没接。**\n"
        self.assertEqual(N.prose_stats(N.strip_markup(plain))["head_repeats"],
                         N.prose_stats(N.strip_markup(bold))["head_repeats"])
        self.assertEqual(N.strip_markup(bold), plain)

    def test_markup_strip_never_touches_the_fingerprint(self):
        """生死线：指纹与字数是**账目**，剥离是**度量**。

        顺序搞反会让全书每一章一次性判为「改稿」并触发一轮版本归档。
        """
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        raw = "# 第一章 · 起\n\n---\n\n**他把碗推过去。**\n"
        r1 = N.save_chapter(s, no=1, text=raw)
        self.assertEqual(r1["chars"], len(raw))          # 含 markdown 记号
        self.assertNotEqual(r1["chars"], len(N.strip_markup(raw)))
        r2 = N.save_chapter(s, no=1, text=raw)
        self.assertTrue(r2["noop"], "同一份原文重存必须幂等（否则全书一次性归档）")
        self.assertIsNone(r2["archived"])

    def test_markup_stats_counts_pollution(self):
        from kinema import novel as N
        raw = "**整段加粗。**\n\n没加粗。\n\n---\n\n**又一段。**\n"
        mk = N.markup_stats(raw)
        self.assertEqual(mk["bold_paragraphs"], 2)
        self.assertEqual(mk["rules"], 1)
        self.assertEqual(mk["unpaired"], 0)
        self.assertEqual(N.markup_stats("**没闭合\n")["unpaired"], 1)


class TestProseBands(NovelCase):
    """带区必须**两侧都有闸**——单向禁令会长出反向 artifact。"""

    def test_bands_are_two_sided(self):
        from kinema import novel as N
        lo, hi, _, _ = N.PROSE_BANDS["simile_per_k"]
        self.assertIsNotNone(lo, "抑制类指标没有下限＝纵容「一本没有任何比喻的长篇」")
        self.assertIsNotNone(hi)
        zero = "他把碗推过去。她没接。风停了。" * 40
        sides = {b["key"]: b["side"] for b in N.band_findings(N.prose_stats(zero))}
        self.assertEqual(sides.get("simile_per_k"), "low")
        flood = "他仿佛在笑，似乎又不是，宛如一块石头。" * 40
        sides = {b["key"]: b["side"] for b in N.band_findings(N.prose_stats(flood))}
        self.assertEqual(sides.get("simile_per_k"), "high")

    def test_every_band_and_rule_ships_a_hint(self):
        from kinema import novel as N
        for k, (lo, hi, zh, hint) in N.PROSE_BANDS.items():
            self.assertTrue(zh and len(hint) > 12, f"{k} 缺物理化改写建议")
            self.assertTrue(lo is not None or hi is not None, f"{k} 两侧都没有闸")
        for code, (pat, zh, cap, hint) in N.PROSE_RULES.items():
            self.assertTrue(zh and len(hint) > 12, f"{code} 缺物理化改写建议")
            __import__("re").compile(pat)          # 正则必须能编译
            self.assertGreaterEqual(cap, 0)

    def test_uniform_sd_no_longer_drives_a_finding(self):
        """恒返回绿灯的检查会掩盖「实际没查」的事实，故 lint 不设该维度。"""
        import inspect
        from kinema import novel as N
        src = inspect.getsource(N.lint)
        self.assertNotIn("UNIFORM_SD_RATIO", src)
        self.assertFalse(hasattr(N, "UNIFORM_SD_RATIO"),
                         "已退役的闸值不该以死常量形式留在模块里")

    def test_mattr_is_length_stable(self):
        from kinema import novel as N
        seed = "他把碗推过去她没接风停了灯抬了一下守夜人的火认得走夜的"
        a, b = N.mattr(seed * 40), N.mattr(seed * 400)
        self.assertLess(abs(a - b), 0.05, "MATTR 必须长度无关（这正是不用 TTR 的理由）")


class TestPivotAndNominalRules(NovelCase):
    """抬价句式/名词化两条硬规则：按修辞动作拦（换一套字面仍是同一个姿势），
    与 definition_sentence 分工——那条管「不是X，是Y／这叫」定义句形，
    这两条管其余外衣与公文腔，同一处不出两条。"""

    def _lint_codes(self, text):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text=text, digest="x", state={"time": "第1天"})
        got = N.lint(s, frm=1, to=1)
        return {f["code"] for f in got["findings"]}

    def test_pivot_rule_reports_beyond_cap(self):
        text = ("他早就想过了，你以为躲进山里就安全，其实山里人更认得生人。\n"
                "走到半路他又改了主意——这不是逃，而是把命换个地方放。")
        codes = self._lint_codes(text)
        self.assertIn("pivot_rhetoric", codes)
        self.assertNotIn("definition_sentence", codes, "「不是…而是」归抬价句式，不许两条重复报")

    def test_single_pivot_within_cap_passes(self):
        # 每章上限 1：真走过「误解→修正」的段落偶用一次是合法的，连发才是翻案腔
        codes = self._lint_codes("他想，你以为躲进山里就安全，其实山里人更认得生人。")
        self.assertNotIn("pivot_rhetoric", codes)

    def test_nominalization_reports_beyond_cap(self):
        codes = self._lint_codes("队里对旧渠进行了排查，又对水车进行了改造。")
        self.assertIn("nominalization", codes)

    def test_plain_prose_is_silent(self):
        codes = self._lint_codes("他把碗推过去。她没接。风从门缝里进来，灯抖了一下。")
        self.assertNotIn("pivot_rhetoric", codes)
        self.assertNotIn("nominalization", codes)


class TestLintDenoise(NovelCase):
    def _book(self, s, n=12):
        from kinema import novel as N
        for i in range(1, n + 1):
            N.save_chapter(s, no=i, text=f"第{i}章。他把碗推过去。她没接。",
                           digest="x", state={"time": f"第{i}天"})
        return N

    def test_absence_is_status_aware_and_folded(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for name in ("甲一", "乙二", "丙三", "丁四", "戊五"):
            s.add_character(name)
        N.save_chapter(s, no=1, text="甲一乙二丙三丁四戊五都在。", digest="x")
        for i in range(2, 26):
            N.save_chapter(s, no=i, text=f"第{i}章只有别人。", digest="x")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertLessEqual(codes.count("char_absent"), 3, "缺席必须折叠，恒报即等于不报")
        self.assertIn("char_absent_more", codes)
        # 标了 departed 就不再报
        for name in ("甲一", "乙二", "丙三", "丁四", "戊五"):
            s.set_character(name, status="departed")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertNotIn("char_absent", codes)
        self.assertNotIn("char_absent_more", codes)

    def test_chapter_length_check_is_windowed(self):
        """篇幅检查恒扫全书的话，第 75 章会一直报到第 1000 章。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="超长。" * 4000, digest="x")
        for i in range(2, 16):
            N.save_chapter(s, no=i, text="正常长度的一章。" * 60, digest="x")
        codes = [f["code"] for f in N.lint(s, frm=6, to=15)["findings"]]
        self.assertNotIn("long_chapter", codes)
        self.assertIn("long_chapter",
                      [f["code"] for f in N.lint(s, frm=1, to=15)["findings"]])

    def test_findings_carry_chapter_locators(self):
        """350 章的书上，不带章节出处的告警无法定位，实际不可用。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for i in range(1, 4):
            N.save_chapter(s, no=i, digest="x",
                           text=f"第{i}章。他不禁后退。他不禁后退。他不禁后退了一步。")
        rows = {f["code"]: f["msg"] for f in N.lint(s)["findings"]}
        self.assertIn("prose_slop", rows)
        self.assertRegex(rows["prose_slop"], r"第 \d+")
        rep = [f["msg"] for f in N.lint(s)["findings"] if f["code"] == "prose_repeat"]
        self.assertTrue(rep and "第" in rep[0])

    def test_avoid_and_slop_do_not_double_report(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.style_update(s, add_avoid=["不禁"])
        for i in range(1, 3):
            N.save_chapter(s, no=i, text=f"第{i}章。他不禁后退。他不禁笑。他不禁停下。",
                           digest="x")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertIn("style_avoid", codes)
        slop = [f["msg"] for f in N.lint(s)["findings"] if f["code"] == "prose_slop"]
        self.assertFalse([m for m in slop if "不禁" in m], "同一处不许出两条")

    def test_lint_is_still_side_effect_free(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        self._book(s)
        p = s.dir / "project.json"
        before = p.read_bytes()
        N.lint(s)
        N.sweep(s, "推过")
        self.assertEqual(before, p.read_bytes(), "lint/sweep 必须零落盘")


class TestManuscriptDrift(NovelCase):
    def test_lint_detects_manuscript_drift(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="原文。", digest="x")
        (s.dir / "manuscript" / "ch0001.md").write_text("被手改过的正文。",
                                                        encoding="utf-8")
        p = s.dir / "project.json"
        before = p.read_bytes()
        rows = {f["code"]: f for f in N.lint(s)["findings"]}
        self.assertIn("manuscript_drift", rows)
        self.assertEqual(rows["manuscript_drift"]["level"], "warn")
        self.assertEqual(before, p.read_bytes())
        # 盘上有、没登记
        (s.dir / "manuscript" / "ch0009.md").write_text("野生章。", encoding="utf-8")
        self.assertIn("manuscript_orphan",
                      [f["code"] for f in N.lint(s)["findings"]])

    def test_reindex_reconciles_disk_and_registry(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘")
        N.save_chapter(s, no=1, text="原文。", digest="x")
        (s.dir / "manuscript" / "ch0001.md").write_text("孙缘走进来。",
                                                        encoding="utf-8")
        r = N.reindex(s, archive=True)
        self.assertEqual(r["fixed"], [1])
        e = N.find_entry(s, 1)
        self.assertEqual(e["sha256"], N.text_fp("孙缘走进来。"))
        self.assertEqual(e["entities"]["characters"], ["孙缘"])
        self.assertNotIn("manuscript_drift",
                         [f["code"] for f in N.lint(s)["findings"]])
        self.assertTrue(r["archived"] and r["archived"][0]["file"].startswith(
            "manuscript/versions/"), "归档路径必须工作区相对")

    def test_revert_restores_and_reindexes(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="第一版。", digest="x")
        N.save_chapter(s, no=1, text="第二版，改坏了。")
        r = N.revert(s, no=1)
        self.assertEqual((s.dir / "manuscript" / "ch0001.md").read_text("utf-8"),
                         "第一版。")
        e = N.find_entry(s, 1)
        self.assertEqual(e["sha256"], N.text_fp("第一版。"))
        self.assertEqual(e["chars"], len("第一版。"))
        self.assertTrue(r["archived_v"], "回滚前那一版必须先归档（才能再滚回去）")
        self.assertTrue(any(h.get("reason") == "rollback-out"
                            for h in e.get("versions") or []))


class TestSweep(NovelCase):
    def test_sweep_covers_every_layer(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘"); s.set_character("孙缘", arc="旧口径→新")
        s.add_prop("断剑", desc="旧口径的剑")
        s.add_scene("钟楼", desc="旧口径的楼")
        N.save_chapter(s, no=1, text="正文里写着旧口径。", digest="大纲里也有旧口径",
                       state={"note": "状态里也有旧口径"})
        N.arc_upsert(s, no=1, title="卷一", frm=1, goal="卷纲里的旧口径")
        N.thread_add(s, title="伏笔里的旧口径", setup=1)
        N.bible_set(s, "【零】宪法里的旧口径\n")
        r = N.sweep(s, "旧口径")
        for layer in N.SWEEP_LAYERS:
            self.assertGreater(r["layers"][layer]["n"], 0, f"第 {layer} 层漏扫")
        self.assertTrue(r["layers"]["manuscript"]["rows"][0]["where"].startswith("第1章:"))
        with self.assertRaises(Exception):
            N.sweep(s, "旧")                      # 短词拦截
        self.assertGreater(N.sweep(s, "旧", min_len=1)["total"], 0)


class TestNovelLog(NovelCase):
    def test_log_is_append_only_and_does_not_multiply(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="正文。")
        N.log_add(s, kind="checkpoint", text="第一次复核", at=10,
                  ref="plan/batch-1-10.md")
        N.log_add(s, kind="decision", text="改用第三人称")
        for _ in range(5):
            s.save()
        self.assertEqual(len(N.log_view(s.data)), 2, "连 save 五次条数必须恒定")
        N.log_add(s, kind="checkpoint", text="第一次复核", at=10,
                  ref="plan/batch-1-10.md")
        self.assertEqual(len(N.log_view(s.data)), 2, "同内容重记必须幂等")
        self.assertEqual(len(N.log_view(s.data, kind="decision")), 1)
        with self.assertRaises(Exception):
            N.log_add(s, kind="不存在的类型", text="x")

    def test_log_survives_a_stale_in_memory_copy(self):
        """两个手柄各记一条，取并集不丢——同 decisions 的并发教训。"""
        from kinema import novel as N
        from kinema.workspace import Workspace
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="正文。")
        other = Workspace.open(str(self.tmp)).get_project("nv")
        N.log_add(s, kind="note", text="A 手柄")
        N.log_add(other, kind="note", text="B 手柄")
        fresh = Workspace.open(str(self.tmp)).get_project("nv")
        texts = {e["text"] for e in N.log_view(fresh.data)}
        self.assertEqual(texts, {"A 手柄", "B 手柄"})


class TestBriefBible(NovelCase):
    def _bible(self):
        return ("【一·力量】" + "力量体系正文。" * 60 + "\n"
                "【二·货币】" + "神币怎么算的正文。" * 60 + "\n"
                "【三·叙事纪律】" + "写法纪律正文。" * 60 + "\n")

    def test_sections_are_lossless(self):
        from kinema import novel as N
        wb = self._bible()
        secs = N.bible_sections(wb)
        self.assertEqual(len(secs), 3)
        self.assertEqual("".join(x["body"] for x in secs), wb, "切分必须无损")
        # 认不出节标时回落整份，绝不猜
        flat = N.bible_sections("没有任何节标的一整份宪法。")
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["title"], "（全文）")

    def test_brief_bible_is_sectioned_not_dumped(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.bible_set(s, self._bible())
        s.add_character("孙缘")
        N.save_chapter(s, no=1, text="孙缘算了一笔神币的账。", digest="神币",
                       state={"characters": {"孙缘": "在算账"}})
        b = N.brief(s)
        self.assertEqual([x["title"] for x in b["bible_toc"]],
                         ["一·力量", "二·货币", "三·叙事纪律"])
        picked = set(b["bible_picked"])
        self.assertIn("三·叙事纪律", picked, "常驻节（写法纪律）必须恒在场")
        self.assertLess(sum(x["chars"] for x in b["bible_sections"]),
                        b["bible_total"], "缺省绝不是整份回灌")
        # 点名取节：一字不截
        one = N.brief(s, bible=["二·货币"])
        self.assertEqual([x["title"] for x in one["bible_sections"]], ["二·货币"])
        self.assertIn("神币怎么算的正文。" * 60, one["bible_sections"][0]["body"])
        self.assertEqual(len(N.brief(s, bible=["all"])["bible_sections"]), 3)

    def test_brief_carries_arc_turns(self):
        """第①门唯一的逐章对照物——写前必读包丢掉它，第①门就没有对照依据。"""
        import inspect
        from kinema import novel as N
        from kinema import cli
        ws, s = _mkseries(self.tmp)
        N.arc_upsert(s, no=1, title="卷一", frm=1, to=30,
                     turns=["第7章遇袭", "第22章反水"])
        N.save_chapter(s, no=1, text="正文。")
        self.assertEqual(N.brief(s)["arc"]["turns"], ["第7章遇袭", "第22章反水"])
        # 渲染同源：brief 与 arcs 共用一个函数，各写一份必分叉
        for fn in (cli.cmd_novel_brief, cli.cmd_novel_arc_list):
            self.assertIn("_print_arc_body", inspect.getsource(fn))

    def test_brief_flags_thin_personas_and_orders_threads(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        s.add_character("孙缘")               # 四件全缺
        s.add_character("木马")
        s.set_character("木马", speech_style="绕圈子", personality="怯",
                        arc="从→到", taboo_lines=["绝不先动手"])
        N.save_chapter(s, no=1, text="孙缘和木马。", digest="x",
                       state={"characters": {"孙缘": "在", "木马": "在"}})
        N.thread_add(s, title="远期", setup=1, due=900)
        N.thread_add(s, title="近期", setup=1, due=3)
        N.thread_add(s, title="无期限", setup=1)
        b = N.brief(s)
        self.assertEqual(b["thin_personas"], ["孙缘"])
        self.assertEqual([t["title"] for t in b["open_threads"]],
                         ["近期", "远期", "无期限"], "快到期的必须排前面")
        self.assertIn("合计", b["budget"])


class TestStyleAndBibleWritePath(NovelCase):
    def test_style_and_bible_have_a_write_path(self):
        from kinema import novel as N
        from kinema.workspace import Workspace
        ws, s = _mkseries(self.tmp)
        other = Workspace.open(str(self.tmp)).get_project("nv")
        N.style_update(s, pov="第三人称有限", add_baseline=["样本一"],
                       add_avoid=["总之", "渐渐"])
        N.style_update(other, add_baseline=["样本二"])   # 旧副本
        fresh = Workspace.open(str(self.tmp)).get_project("nv")
        st = fresh.data["narrative_style"]
        self.assertEqual(st["baseline"], ["样本一", "样本二"], "两个手柄都不许丢")
        self.assertEqual(st["pov"], "第三人称有限")
        N.style_update(fresh, rm_avoid=["渐渐"])
        self.assertEqual(fresh.data["narrative_style"]["avoid"], ["总之"])
        N.style_update(fresh, rm_baseline=1)
        self.assertEqual(fresh.data["narrative_style"]["baseline"], ["样本二"])
        with self.assertRaises(Exception):
            N.style_update(fresh, rm_baseline=9)
        with self.assertRaises(Exception):
            N.style_update(fresh, 未知字段="x")

    def test_bible_set_modes(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.bible_set(s, "【一·力量】旧的力量体系。\n【二·货币】货币。\n")
        N.bible_set(s, "【一·力量】新的力量体系。\n", section="一·力量")
        wb = s.data["adaptation"]["world_bible"]
        self.assertIn("新的力量体系", wb)
        self.assertNotIn("旧的力量体系", wb)
        self.assertIn("【二·货币】", wb)
        N.bible_set(s, "【三·规则】追加的。\n", append=True)
        self.assertEqual(len(N.bible_sections(s.data["adaptation"]["world_bible"])), 3)
        with self.assertRaises(Exception):
            N.bible_set(s, "x", section="不存在的节")


class TestBaselineMetrics(NovelCase):
    def _fill(self, s, text_of):
        from kinema import novel as N
        for i in range(1, 6):
            N.save_chapter(s, no=i, text=text_of(i), digest="x")

    def test_baseline_drives_z_not_absolute(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        self._fill(s, lambda i: f"第{i}章。他把碗推过去。她没接。风停了。" * 12)
        m = N.baseline_metrics(s, frm=1, to=5)
        self.assertEqual(m["n_chapters"], 5)
        # 有分布的指标必须 μ/σ 成对落盘；句长类恒有分布
        self.assertIn("avg_sentence_len", m)
        for k in N.METRIC_KEYS:
            if k in m:
                self.assertIn(k + "_sd", m)
        self.assertEqual(s.data["narrative_style"]["baseline_metrics"]["n_chapters"], 5)
        with self.assertRaises(Exception):
            N.baseline_metrics(s, frm=1, to=2)      # 少于 3 章的 σ 不稳

    def test_checkpoint_derives_from_chapter_numbers_not_count(self):
        """检查点按**章号**派生（样本刻意用接盘书：从第 51 章导入，章数≠章号）。
        按章数算的话：view 给「下一检查点第 20 章」对最新章号 63 是反向区间
        （前端会生成「续写 64~20 章」发给 agent 执行）；写到第 70 章的满档回执
        还会让 agent 拿章数当章号去 recap 11~20 的空窗口做七门复核。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for no in range(51, 64):                        # 接盘导入 51~63（章数 13）
            N.save_chapter(s, no=no, text=f"第{no}章正文。" * 30, digest="x")
        v = N.view(s.data)
        self.assertEqual(v["current_no"], 63)
        self.assertEqual(v["next_checkpoint"], 70, "检查点必须在最新章号之后")
        self.assertFalse(v["checkpoint_due"])
        for no in range(64, 71):                        # 续写到第 70 章
            r = N.save_chapter(s, no=no, text=f"第{no}章正文。" * 30, digest="x")
        self.assertTrue(r["checkpoint"], "第 70 章满档（章号口径）")
        self.assertEqual(r["no"], 70)                   # 回执窗口 61~70 由 no 派生
        self.assertTrue(N.view(s.data)["checkpoint_due"])

    def test_all_zero_metrics_never_enter_the_baseline(self):
        """整窗全 0 的计数型指标（明喻/超长句在正常章节常整批为 0）**不落基线**：
        μ=0/σ=0.001 的基线会让此后任何一章出现一个明喻就 z≈+400 恒响，
        且与 prose_bands「该多写比喻」的下限建议正面冲突（同一次 lint 两头喊）；
        测不出的 None 也不许折成 0 参与 μ/σ。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        body = "他把碗推过去。她没接。风停了。" * 12
        self._fill(s, lambda i: f"第{i}章。{body}")
        m = N.baseline_metrics(s, frm=1, to=5)
        per = N.prose_stats(body)
        zeroed = [k for k in N.METRIC_KEYS if not per.get(k)]
        self.assertTrue(zeroed, "夹具必须真的存在整窗为 0 的计数型指标")
        for k in zeroed:
            self.assertNotIn(k, m, f"{k} 整窗为 0，落成基线就是恒响的闸")

    def test_style_gate_says_it_is_idle_when_baseline_missing(self):
        """「没有料可比」绝不等于「比对通过」——同 consistency 的 REASONS 纪律。"""
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="正文。" * 30, digest="x")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertIn("no_baseline", codes)
        N.style_update(s, add_baseline=["样本"])
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertIn("no_baseline_metrics", codes, "有文字样本没数值基线也要说清是空转")

    def test_drift_is_per_chapter_and_names_the_chapter(self):
        from kinema import novel as N
        base = {"n_chapters": 5, "avg_sentence_len": 10.0, "avg_sentence_len_sd": 1.0}
        self.assertFalse(N._style_drift(base, [(7, {"avg_sentence_len": 11.0})]))
        msg = N._style_drift(base, [(7, {"avg_sentence_len": 11.0}),
                                    (9, {"avg_sentence_len": 30.0})])
        self.assertTrue(msg and "avg_sentence_len" in msg[0])
        self.assertIn("第 9 章", msg[0], "必须点名是哪一章（350 章上没出处等于没告警）")
        self.assertNotIn("第 7 章", msg[0])
        self.assertFalse(N._style_drift({"n_chapters": 2}, [(1, {"avg_sentence_len": 99})]))

    def test_sigma_floor_is_relative_not_absolute(self):
        """基线各章恰好一致时 σ→0，绝对地板会让任何微小偏差炸成三位数 z。"""
        from kinema import novel as N
        base = {"n_chapters": 5, "mattr": 0.85, "mattr_sd": 0.0}
        self.assertFalse(N._style_drift(base, [(1, {"mattr": 0.86})]))   # 偏 1.2%
        msg = N._style_drift(base, [(1, {"mattr": 0.60})])               # 偏 29%
        self.assertTrue(msg)
        z = float(msg[0].split("z=")[1].rstrip("）"))
        self.assertLess(abs(z), 100, f"z 爆炸了: {z}")

    def test_mattr_says_it_cannot_measure_short_text(self):
        """退化成 TTR 的读数系统性偏高，与窗口化读数不可比——测不了就说测不了。"""
        from kinema import novel as N
        self.assertIsNone(N.mattr("他把碗推过去。她没接。"))
        self.assertIsNotNone(N.mattr("他把碗推过去她没接风停了" * 200))
        self.assertNotIn("mattr", {b["key"] for b in
                                   N.band_findings(N.prose_stats("短短一句。"))})


class TestThreadTier(NovelCase):
    def test_tier_derives_due_and_no_due_is_not_silent(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        t = N.thread_add(s, title="短线", setup=10, tier="short")
        self.assertEqual(t["due"], 40)
        self.assertIsNone(N.thread_add(s, title="长线", setup=1, tier="long")["due"])
        with self.assertRaises(Exception):
            N.thread_add(s, title="x", setup=1, tier="不存在")
        for i in range(1, 26):
            N.save_chapter(s, no=i, text=f"第{i}章。", digest="x")
        rows = {f["code"]: f for f in N.lint(s)["findings"]}
        self.assertEqual(rows["thread_stale"]["level"], "info",
                         "显式声明过 long 的不该被当成漏填")
        N.thread_add(s, title="忘了定档", setup=1)
        rows = [f for f in N.lint(s)["findings"] if f["code"] == "thread_stale"]
        self.assertTrue(any(f["level"] == "warn" for f in rows),
                        "没定档的无期限伏笔必须是 warn——不填 due 不能成为让告警静音的动作")

    def test_thread_set_updates_text_fields_only(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        t = N.thread_add(s, title="旧标题", setup=5, due=40)
        N.thread_mark(s, t["id"], status="paid", paid_in=30)
        r = N.thread_set(s, t["id"], title="新标题", tier="mid")
        self.assertEqual(r["title"], "新标题")
        self.assertEqual(r["status"], "paid")      # 状态不许被顺手改掉
        self.assertEqual(r["paid_in"], 30)
        with self.assertRaises(Exception):
            N.thread_set(s, t["id"], status="open")
        with self.assertRaises(Exception):
            N.thread_set(s, t["id"], due=1)        # 期限早于埋设章


class TestPacing(NovelCase):
    def test_pacing_only_counts_never_judges(self):
        import inspect
        from kinema import novel as N
        src = inspect.getsource(N._pacing_findings)
        for word in ("score", "总分", "评分", "打分"):
            self.assertNotIn(word, src, "引擎绝不合成质量分")

    def test_pacing_is_opt_in(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for i in range(1, 9):
            N.save_chapter(s, no=i, text=f"第{i}章。", digest="x")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertFalse([c for c in codes if c.startswith("pacing")],
                         "一章都没声明时整段必须静默（不给填表摩擦）")

    def test_flat_and_monotone_need_three_in_a_row(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="第1章。", digest="x", payoff="minor",
                       payoff_kind="打脸", hook="决定")
        for i in (2, 3, 4):
            N.save_chapter(s, no=i, text=f"第{i}章。", digest="x")
        codes = [f["code"] for f in N.lint(s)["findings"]]
        self.assertIn("pacing_flat", codes)
        ws2, s2 = _mkseries(self.tmp, pid="nv2")
        for i in (1, 2):
            N.save_chapter(s2, no=i, text=f"第{i}章。", digest="x",
                           payoff="minor", payoff_kind="打脸", hook="决定")
        self.assertNotIn("hook_monotone",
                         [f["code"] for f in N.lint(s2)["findings"]])
        N.save_chapter(s2, no=3, text="第3章。", digest="x", payoff="minor",
                       payoff_kind="打脸", hook="决定")
        codes = [f["code"] for f in N.lint(s2)["findings"]]
        self.assertIn("hook_monotone", codes)
        self.assertIn("payoff_kind_repeat", codes)
        with self.assertRaises(Exception):
            N.save_chapter(s2, no=4, text="x", payoff="超级大")


class TestSaveOneCall(NovelCase):
    def test_save_accepts_digest_and_state_in_one_call(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        r = N.save_chapter(s, no=1, text="正文。", digest="大纲",
                           state={"time": "第一天", "hooks": ["悬念"]})
        self.assertFalse(r["missing_digest"])
        self.assertFalse(r["missing_state"])
        self.assertEqual(N.find_entry(s, 1)["state"]["time"], "第一天")
        with self.assertRaises(Exception):
            N.save_chapter(s, no=2, text="x", state={"未知键": 1})


class TestExport(NovelCase):
    def test_export_concatenates_in_registry_order(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for i in (1, 2, 10, 11):
            N.save_chapter(s, no=i, text=f"# 第{i}章\n\n**第{i}章正文。**\n",
                           title=f"标题{i}")
        r = N.export(s)
        text = Path(r["file"]).read_text(encoding="utf-8")
        # 文件名字典序会把 ch0010 排在 ch0002 前面；登记序不会
        self.assertLess(text.index("第2章正文"), text.index("第10章正文"))
        self.assertIn("**", text)
        plain = Path(N.export(s, strip=True)["file"]).read_text(encoding="utf-8")
        self.assertNotIn("**", plain)
        self.assertIn("第1章正文。", plain)
        self.assertEqual(N.export(s, frm=10)["chapters"], 2)


class TestNoPhantomCommands(NovelCase):
    def test_engine_never_prints_an_unparsable_command(self):
        """引擎回执是比 SKILL 正文更强的行为锚——它每天被打印几十次。

        典型错误形如 `novel arc add …`——该写法不存在（arc 的第一个位置参数是
        pid），实跑得到「✗ 找不到项目: add」，报错还把人往「项目不存在」误导。
        """
        import re
        from kinema import cli
        blob = "\n".join(
            (Path(cli.__file__).read_text(encoding="utf-8"),
             (Path(cli.__file__).parent / "novel.py").read_text(encoding="utf-8")))
        parser = cli.build_parser()
        nv = parser._subparsers._group_actions[0].choices["novel"] \
            ._subparsers._group_actions[0].choices
        bad = []
        # 反引号里的整条命令：动词与**参数**都要验。只验动词不够——
        # 像 `novel reindex <pid> --all --archive` 这类串动词对、而 `--all` 根本
        # 不存在，照抄的下游读者都会撞上。`novel thread-*` 这类 glob 简写跳过。
        for m in re.finditer(r"`novel ([a-z][a-z0-9-]*)([^`]*)`", blob):
            verb, rest = m.group(1), m.group(2)
            if verb.endswith("-"):
                continue
            if verb not in nv:
                bad.append(f"子命令 novel {verb}")
                continue
            opts = {o for a in nv[verb]._actions for o in a.option_strings}
            for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", rest):
                if flag not in opts and flag != "--workspace":
                    bad.append(f"`novel {verb}` 没有参数 {flag}")
        self.assertFalse(sorted(set(bad)),
                         f"引擎打印了跑不通的命令: {sorted(set(bad))}")


class TestSkillDocsMatchTheCli(NovelCase):
    """SKILL 与 references 里出现的每条命令都必须真的能跑。

    先例是 test_camera_presets 逐字节比对 `.claude/skills/kinema/references/
    storyboard.md`：指挥层文档与引擎分叉时，按文档操作即失败，而这类错误没有任何
    运行期信号——只在用户侧暴露。
    """

    SKILL = (Path(__file__).resolve().parents[2] / ".claude" / "skills"
             / "kinema-novel")
    GROUPS = ("novel", "character", "prop", "scene", "adapt")

    def _choices(self):
        from kinema import cli
        top = cli.build_parser()._subparsers._group_actions[0].choices
        return {g: top[g]._subparsers._group_actions[0].choices
                for g in self.GROUPS}, top

    def test_skill_files_are_present_and_one_level_deep(self):
        self.assertTrue((self.SKILL / "SKILL.md").is_file())
        refs = sorted(p.name for p in (self.SKILL / "references").glob("*.md"))
        self.assertEqual(refs, ["checkpoint.md", "cli.md", "craft.md",
                                "prose-rubric.md", "setup.md", "writeback.md"])
        # 引用只准一层深：reference 之间不许互相指（全部由 SKILL.md 直接指）
        for p in (self.SKILL / "references").glob("*.md"):
            for other in refs:
                if other == p.name:
                    continue
                self.assertNotIn(f"references/{other}", p.read_text("utf-8"),
                                 f"{p.name} 指向了另一份 reference（引用只准一层深）")

    def test_every_command_in_the_docs_really_exists(self):
        import re
        groups, _ = self._choices()
        bad = []
        for f in sorted(self.SKILL.rglob("*.md")):
            text = f.read_text(encoding="utf-8")
            text = re.split(r"^##+ 不存在的命令", text, flags=re.M)[0]  # 反例区
            for m in re.finditer(
                    r"\b(" + "|".join(self.GROUPS) + r")[ \t]+([a-z][a-z0-9-]*)", text):
                g, verb = m.group(1), m.group(2)
                if verb.endswith("-"):          # `thread-*` 这类 glob 简写
                    continue
                if verb not in groups[g]:
                    bad.append(f"{f.name}: `{g} {verb}`")
        self.assertFalse(sorted(set(bad)), f"文档里的命令不存在: {sorted(set(bad))}")

    def test_every_flag_in_the_docs_really_exists(self):
        import re
        groups, top = self._choices()
        bad = []
        for f in sorted(self.SKILL.rglob("*.md")):
            text = re.split(r"^##+ 不存在的命令", f.read_text("utf-8"), flags=re.M)[0]
            for blk in re.findall(r"```bash\n(.*?)```", text, re.S):
                for line in blk.split("\n"):
                    line = line.split("#")[0].strip().rstrip("\\").strip()
                    m = re.match(r"^(?:python3 -m kinema\s+)?"
                                 r"(" + "|".join(self.GROUPS) + r")\s+"
                                 r"([a-z][a-z0-9-]*)\s*(.*)$", line)
                    if not m or m.group(2) not in groups[m.group(1)]:
                        continue
                    sub = groups[m.group(1)][m.group(2)]
                    opts = {o for a in sub._actions for o in a.option_strings}
                    for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", m.group(3)):
                        if flag not in opts and flag != "--workspace":
                            bad.append(f"{f.name}: `{m.group(1)} {m.group(2)}` "
                                       f"没有 {flag}")
        self.assertFalse(sorted(set(bad)), f"文档里的参数不存在: {sorted(set(bad))}")

    def test_main_file_stays_under_the_context_budget(self):
        """主文件必须落在压缩保留线内。

        Claude Code 压缩后每个 skill 只保留开头一段——主文件一旦超出，写在后面的
        判据（回写清单、七门判据、速查）在真正需要它们的时刻已经不在上下文里了，
        而检查点按定义发生在第 10 章、几乎必然跨过一次压缩。
        """
        n = len((self.SKILL / "SKILL.md").read_text(encoding="utf-8"))
        self.assertLess(n, 9000, f"SKILL.md {n} 字符——超了就该往 references/ 拆")


class TestNormalize(NovelCase):
    """正文排版规范化：执行铁律「粗体只给面板」，不做文学判断。"""

    def test_panel_stays_bold_narration_does_not(self):
        from kinema import novel as N
        src = ("**【属性面板 · 已绑定】**\n"
               "**【收到打赏 · 神币 ×5】**\n"
               "> **「你们的观测期已结束。」**\n"
               "**他把碗推过去。她没接。**\n"
               "他数页数，**几乎每一页都有记号**，一共一千零四十三页。\n"
               "**【贰·枢】起转，链身一缠，转停了——不是打停的，是缠停的，"
               "像大人按住孩子转陀螺的手，一直缠到它自己停下来为止。**\n")
        out, st = N.normalize_markup(src)
        self.assertIn("**【属性面板 · 已绑定】**", out)
        self.assertIn("> **「你们的观测期已结束。」**", out)
        self.assertIn("他把碗推过去。她没接。", out)
        self.assertNotIn("**他把碗推过去", out)
        self.assertIn("几乎每一页都有记号", out)
        self.assertNotIn("**几乎", out)
        # 以面板词开头的**叙述**必须剥——按覆盖率判，不按行首判
        self.assertNotIn("**【贰·枢】", out)
        self.assertIn("【贰·枢】起转", out)
        self.assertEqual(st["kept_panel"], 3)
        self.assertEqual(st["paragraph_bold"], 2)     # 整段加粗两段
        self.assertEqual(st["inline_bold"], 1)

    def test_nested_bold_is_stripped_to_a_fixed_point(self):
        """真书稿里有 `****整段****` 这种嵌套写法——一遍只剥一层，幂等当场失效。"""
        from kinema import novel as N
        out, _ = N.normalize_markup("****队形是在第十一分钟散的。****\n")
        self.assertEqual(out.strip(), "队形是在第十一分钟散的。")
        self.assertEqual(N.normalize_markup(out)[0], out, "必须是不动点")

    def test_normalize_never_touches_rules_or_headings(self):
        """`---` 是断场还是节拍停顿必须读上下文——没有安全的机械判据，一律不碰。"""
        from kinema import novel as N
        src = "# 第一章 · 起\n\n---\n\n**正文。**\n\n---\n"
        out, _ = N.normalize_markup(src)
        self.assertEqual(out.count("---"), 2)
        self.assertIn("# 第一章 · 起", out)
        self.assertNotIn("**", out)

    def test_normalize_archives_every_chapter_and_is_idempotent(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        for i in (1, 2):
            N.save_chapter(s, no=i, text=f"# 第{i}章\n\n**加粗的一段。**\n\n**【面板】**\n")
        r = N.normalize(s)
        self.assertEqual(len(r["changed"]), 2)
        self.assertEqual(r["totals"]["kept_panel"], 2)
        for i in (1, 2):
            body = (s.dir / N.chapter_relpath(i)).read_text("utf-8")
            self.assertIn("加粗的一段。", body)
            self.assertNotIn("**加粗", body)
            self.assertIn("**【面板】**", body)
            # 旧稿必须进版本栈——整轮操作因此可逐章回滚
            self.assertTrue(N.version_files(s, i), f"第 {i} 章旧稿没归档，回不去了")
        self.assertEqual(len(N.normalize(s)["changed"]), 0, "同内容重跑必须幂等")
        # 登记块与磁盘不许脱钩（normalize 走 save，不是裸写盘）
        self.assertNotIn("manuscript_drift",
                         [f["code"] for f in N.lint(s)["findings"]])

    def test_dry_run_writes_nothing(self):
        from kinema import novel as N
        ws, s = _mkseries(self.tmp)
        N.save_chapter(s, no=1, text="**加粗。**\n")
        before = (s.dir / "project.json").read_bytes()
        body = (s.dir / N.chapter_relpath(1)).read_text("utf-8")
        r = N.normalize(s, dry_run=True)
        self.assertEqual(len(r["changed"]), 1)
        self.assertEqual(before, (s.dir / "project.json").read_bytes())
        self.assertEqual(body, (s.dir / N.chapter_relpath(1)).read_text("utf-8"))
