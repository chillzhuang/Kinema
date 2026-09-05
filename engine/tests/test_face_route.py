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

"""写实人物合规链路守卫（photoreal face）。

三块：① `face_visibility` 登记与并发存活（字段横跨五处，schema/contracts 对拍在
test_schema_contract）；② 写实档（identity_sheet）的身份图必须纯文生图——受信豁免
绑「是不是文生图产物」，蓝图与 moodboard 挂一张都失效；③ `sheet_origin` 是生成
方式的事实记录，写 `sheet` 的五条路径（gen-refs 直出 / 候选定稿 / refine 局改 /
版本回滚 / 素材直供）一条都不许漏，且系列 → 章节的两条搬运通道都要携带。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import unittest.mock

from pathlib import Path

import contextlib
import io

from kinema import batch, review
from kinema import project as project_mod
from kinema.errors import KinemaError, ProviderError
from kinema.project import Project
from tests.support import LocalBackendEnv, fake_path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082"))
    return path


def _doc(**over) -> dict:
    d = {
        "id": "ch01", "motion": "native", "aspect": "16:9",
        "characters": [{"name": "林深", "sheet": fake_path("char_林深.png")}],
        "shots": [{"id": 1, "narration": "台词", "dur": 3.0}],
    }
    d.update(over)
    return d


class _WsCase(unittest.TestCase):
    """带工作区的基座（同 test_adapt.SeriesCase，profile 由用例自定）。"""

    PROFILE = "cyberpunk"          # EMBEDDED_DEFAULTS 里唯一的 identity 档，离线可用

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.tmp = tempfile.TemporaryDirectory()
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(Path(self.tmp.name) / "ws"))
        self.s = self.ws.create_project("写实", pid="pf", profile=self.PROFILE)

    def tearDown(self):
        self.tmp.cleanup()
        self.env.restore()

    def _put_sheet(self, name, content=b"v1"):
        d = self.s.refs_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(content)
        return str(p)

    def _gen_refs(self, only):
        from kinema.cli import build_parser
        ns = build_parser().parse_args(
            ["project", "refs", "pf", "--only", only, "--mock",
             "--workspace", str(self.ws.root)])
        ns.func(ns)


class TestIdentityNoRefs(_WsCase):
    """identity 档的角色设定图：`refs` 整体为空（不是「没有 moodboard」——只掐
    后半段，蓝图仍在就仍是图生图），职责声明同步不注入，来源记 t2i。"""

    def _spy_gen(self, only):
        from kinema.providers.image import mock as mockmod
        seen = {}
        orig = mockmod.MockImageProvider.generate

        def spy(prov, prompt, out_path, **kw):
            if str(kw.get("label", "")).startswith("CHAR"):
                seen["refs"] = list(kw.get("ref_images") or [])
                seen["prompt"] = prompt
            return orig(prov, prompt, out_path, **kw)
        mockmod.MockImageProvider.generate = spy
        try:
            self._gen_refs(only)
        finally:
            mockmod.MockImageProvider.generate = orig
        return seen

    def test_identity_character_is_pure_t2i(self):
        self.s.add_character("林深", appearance="银发青年")
        self.s.save()
        seen = self._spy_gen("character:林深")
        self.assertEqual(seen["refs"], [], "受信要求一张参考都不挂")
        self.assertNotIn("版式样板图", seen["prompt"], "没附样板就不许声明样板")
        c = self.ws.get_project("pf").characters[0]
        self.assertEqual(c.get("sheet_origin"), "t2i")

    def test_identity_prop_keeps_the_blueprint(self):
        """identity 只收窄 character 一类：道具不含人脸，蓝图照垫。"""
        from kinema.providers.image import mock as mockmod
        self.s.add_prop("断刃", desc="一柄长刃")
        self.s.save()
        seen = {}
        orig = mockmod.MockImageProvider.generate

        def spy(prov, prompt, out_path, **kw):
            if str(kw.get("label", "")).startswith("PROP"):
                seen["refs"] = list(kw.get("ref_images") or [])
            return orig(prov, prompt, out_path, **kw)
        mockmod.MockImageProvider.generate = spy
        try:
            self._gen_refs("prop:断刃")
        finally:
            mockmod.MockImageProvider.generate = orig
        self.assertTrue(any("prop_template" in r for r in seen["refs"]),
                        "道具蓝图不该被 identity 撤掉")


class TestNonIdentityUnchanged(_WsCase):
    PROFILE = "hd2d"

    def test_character_still_i2i_with_blueprint(self):
        self.s.add_character("林深", appearance="银发青年")
        self.s.save()
        from kinema.providers.image import mock as mockmod
        seen = {}
        orig = mockmod.MockImageProvider.generate

        def spy(prov, prompt, out_path, **kw):
            if str(kw.get("label", "")).startswith("CHAR"):
                seen["refs"] = list(kw.get("ref_images") or [])
                seen["prompt"] = prompt
            return orig(prov, prompt, out_path, **kw)
        mockmod.MockImageProvider.generate = spy
        try:
            self._gen_refs("character:林深")
        finally:
            mockmod.MockImageProvider.generate = orig
        self.assertTrue(any("char_template" in r for r in seen["refs"]),
                        "非写实档的蓝图垫图不受影响")
        self.assertIn("版式样板图", seen["prompt"])
        c = self.ws.get_project("pf").characters[0]
        self.assertEqual(c.get("sheet_origin"), "i2i")


class TestSheetOriginPaths(_WsCase):
    """写 `sheet` 的另外四条路径：refine 拒绝（identity）/ 素材直供 / 候选定稿 /
    版本回滚，以及系列 → 章节的搬运。"""

    def test_refine_refuses_identity_character_before_archive(self):
        from kinema import refine
        from kinema.models import ConfigStore, ModelRouter
        std = self._put_sheet("char_林深.png")
        self.s.add_character("林深", appearance="银发青年")
        self.s.characters[0].update({"sheet": std, "sheet_origin": "t2i"})
        self.s.save()
        store = ConfigStore.load(None)
        router = ModelRouter(store, force_mock=True)
        with self.assertRaises(KinemaError) as ctx:
            refine.refine_asset(self.ws, store, router, pid="pf",
                                kind="character", name="林深", instruction="衣服换黑色")
        self.assertIn("--force", str(ctx.exception), "拒绝必须给修法")
        # 判在归档之前：图仍在标准路径、版本栈没动
        self.assertTrue(Path(std).is_file())
        self.assertFalse(self.ws.get_project("pf").characters[0].get("versions"))

    def test_supply_marks_external(self):
        from kinema import refine
        self.s.add_character("林深", appearance="银发青年")
        self.s.characters[0].update({"sheet": self._put_sheet("char_林深.png"),
                                     "sheet_origin": "t2i"})
        self.s.save()
        ext = Path(self.tmp.name) / "outside.png"
        # 1×1 PNG（体检要能解码）
        ext.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d4944415478da63fcffff3f030005fe02fea72d1a1a0000000049454e44ae426082"))
        refine.supply_asset_sheet(self.ws, "pf", kind="character", name="林深",
                                  src=str(ext), skip_check=True)
        self.assertEqual(self.ws.get_project("pf").characters[0].get("sheet_origin"),
                         "external")

    def test_pick_candidate_adopts_the_batch_origin(self):
        from kinema import refine
        self.s.add_character("林深", appearance="银发青年")
        cand = self._put_sheet("cand_char_林深_1.png")
        self.s.characters[0].update({"sheet_candidates": [cand],
                                     "sheet_candidates_origin": "t2i",
                                     "sheet_origin": "i2i"})
        self.s.save()
        refine.pick_asset_candidate(self.ws, "pf", kind="character", name="林深", no=1)
        self.assertEqual(self.ws.get_project("pf").characters[0].get("sheet_origin"),
                         "t2i")

    def test_rollback_round_trips_origin(self):
        from kinema import refine
        std = self._put_sheet("char_林深.png", b"t2i-bytes")
        self.s.add_character("林深", appearance="银发青年")
        c = self.s.characters[0]
        c.update({"sheet": std, "sheet_origin": "t2i"})
        self.s.save()
        refine.archive_asset_sheet(self.s, "character", "林深", reason="重出")
        self._put_sheet("char_林深.png", b"i2i-bytes")
        c["sheet_origin"] = "i2i"
        self.s.save()
        refine.rollback_asset_sheet(self.s, "character", "林深", 1)
        s2 = self.ws.get_project("pf")
        c2 = s2.characters[0]
        self.assertEqual(c2.get("sheet_origin"), "t2i", "回滚要还原那一版的生成方式")
        self.assertEqual(Path(c2["sheet"]).read_bytes(), b"t2i-bytes")
        # rollback-out 条目记下了出库版的来源，二次回滚仍可还原
        last = c2["versions"][-1]
        self.assertEqual((last.get("params") or {}).get("sheet_origin"), "i2i")

    def test_origin_travels_to_chapters(self):
        from kinema import refine
        self.s.add_character("林深", appearance="银发青年")
        self.s.create_chapter("第一集", cid="ch01")
        self.s.characters[0].update({"sheet": self._put_sheet("char_林深.png"),
                                     "sheet_origin": "t2i"})
        self.s.save()
        # 两条搬运通道各验一条：sync 白名单 + refine._propagate
        self.s.sync_design_to_chapters()
        cc = next(c for c in self.ws.store.load_chapter("pf", "ch01")["characters"]
                  if c["name"] == "林深")
        self.assertEqual(cc.get("sheet_origin"), "t2i", "sync 白名单漏了 sheet_origin")
        self.s.characters[0]["sheet_origin"] = "i2i"
        self.s.save()
        refine._propagate(self.s)
        cc = next(c for c in self.ws.store.load_chapter("pf", "ch01")["characters"]
                  if c["name"] == "林深")
        self.assertEqual(cc.get("sheet_origin"), "i2i", "_propagate 只搬路径不搬来源")


class _RouteBase(unittest.TestCase):
    """写实档（cyberpunk）全能参考镜的路线仲裁布景。"""

    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.img = str(_png(self.tmp / "shot1.png"))
        self.board = str(_png(self.tmp / "board.png"))
        self.char_sheet = str(_png(self.tmp / "char_林深.png"))
        self.scene_sheet = str(_png(self.tmp / "scene_书店.png"))
        self.top_sheet = str(_png(self.tmp / "scene_top_书店.png"))
        self.prop_sheet = str(_png(self.tmp / "prop_断刃.png"))

    def tearDown(self):
        self._tmp.cleanup()
        self._env.restore()

    def _project(self, shots=None, *, origin="t2i", **over):
        cdir = self.tmp / "proj" / "p1" / "chapters"
        cdir.mkdir(parents=True, exist_ok=True)
        doc = {
            "id": "p1_ch01", "profile": "cyberpunk", "motion": "native",
            "aspect": "16:9",
            "characters": [{"name": "林深", "sheet": self.char_sheet,
                            **({"sheet_origin": origin} if origin else {})}],
            "props": [{"name": "断刃", "sheet": self.prop_sheet}],
            "scenes": [{"name": "书店", "sheet": self.scene_sheet,
                        "topview_sheet": self.top_sheet}],
            "shots": shots or [self._shot(1)],
        }
        doc.update(over)
        cf = cdir / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return Project.load(cf)

    def _shot(self, no, **over):
        s = {"id": no, "dur": 5.0, "image": self.img, "video_prompt": "转身",
             "characters": ["林深"], "props": ["断刃"], "scenes": ["书店"],
             "sketch": {"sheet": self.board, "beats": [
                 {"action": "起身"}, {"action": "迈步"}, {"action": "回望"}]}}
        s.update(over)
        return s

    def _run(self, project, *, dry_run=True, preview_sink=None):
        from kinema.cli import stage_gen_video
        from kinema.models import ConfigStore, ModelRouter
        store = ConfigStore.load(None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stage_gen_video(project, store, ModelRouter(store, force_mock=True),
                            dry_run=dry_run, preview_sink=preview_sink)
        return buf.getvalue()


class TestRouteArbitration(_RouteBase):
    def _route(self, project, shot, *, identity=True, ref_task=True,
               board=None, base=None, v2v=False):
        from kinema.cli import _route_for
        return _route_for(project, shot, identity=identity, ref_task=ref_task,
                          board=board, scene_base=base, v2v=v2v)

    def test_matrix(self):
        p = self._project()
        s = p.data["shots"][0]
        self.assertEqual(self._route(p, s, identity=False)[0], "A")
        self.assertEqual(self._route(p, s, ref_task=False)[0], "A")
        # 未标 closeup：先按分镜图路线试（人脸拒免费）
        self.assertEqual(self._route(p, s, base=self.scene_sheet,
                                     board=self.board)[0], "A")
        s["face_visibility"] = "closeup"
        self.assertEqual(self._route(p, s, base=self.scene_sheet,
                                     board=self.board)[0], "B")
        self.assertEqual(self._route(p, s, base=self.scene_sheet)[0], "C")
        # 场景基准图不在盘：降级无 image 位，恒 A 且给具名理由
        route, why = self._route(p, s, board=self.board, base=None)
        self.assertEqual(route, "A")
        self.assertIn("基准图", why)

    def test_untrusted_identity_never_degrades(self):
        p = self._project(origin="i2i")
        s = p.data["shots"][0]
        s["face_visibility"] = "closeup"
        route, why = self._route(p, s, base=self.scene_sheet, board=self.board)
        self.assertEqual(route, "A", "身份图不受信时降级只会白买板")
        self.assertIn("--force", why, "理由必须给修法")

    def test_control_v2v_reaches_the_ladder_and_says_why(self):
        """写实档的复刻镜必须能降级。分镜图是挂着设定图生的（图生图、天然不受信），
        人脸拒之后若没有第二形态，V2V + 写实人物就是死局——而受信身份图只有降级
        路线送得进请求。无板不算缺口：控制视频逐帧给定走位与景别，比板还硬。"""
        p = self._project()
        s = p.data["shots"][0]
        s["control"] = "seg.mp4"
        s["face_visibility"] = "closeup"
        route, why = self._route(p, s, base=self.scene_sheet, v2v=True)
        self.assertEqual(route, "C")
        self.assertIn("控制视频", why)

    def test_dry_run_names_the_image_actually_sent(self):
        """报价行的 `图=` 必须是真占 image 位的那一张。降级路线上分镜图整个不进
        请求，照报分镜图就是拿一张没发出去的图给报价背书。"""
        p = self._project(shots=[self._shot(1, face_visibility="closeup")])
        log = self._run(p)
        self.assertIn(f"图={Path(self.scene_sheet).name}", log)
        self.assertNotIn(f"图={Path(self.img).name}", log)

    def test_control_v2v_never_takes_a_board(self):
        """板与控制视频是两个并列的运动权威——盘上恰好有板也不挂（同 previz 那道
        闸的理由）。互斥判在仲裁层：provider 层看到的全是 `ref_images`，分不出
        哪张是板、哪张是身份图。"""
        p = self._project()
        s = p.data["shots"][0]
        s["control"] = "seg.mp4"
        s["face_visibility"] = "closeup"
        route, _why = self._route(p, s, base=self.scene_sheet,
                                  board=self.board, v2v=True)
        self.assertEqual(route, "C", "有板也不许落到 B")

    def test_previz_shot_never_degrades(self):
        p = self._project()
        s = p.data["shots"][0]
        s["guide"] = "previz"
        s["face_visibility"] = "closeup"
        route, why = self._route(p, s, base=self.scene_sheet, board=self.board)
        self.assertEqual(route, "A")
        self.assertIn("previz", why)


class TestRouteBAssembly(_RouteBase):
    """closeup 镜的路线 B 端到端（mock 真发）：image 位=场景基准图、分镜图整个
    不进请求、场景图从设定清单剔重、契约句换取景地口径。"""

    def _dispatch(self, project):
        from kinema.providers.video import mock as vmock
        from kinema.providers.base import VideoResult
        calls = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            calls.append({"image": image, **kw})
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = spy
        try:
            with unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=5.0):
                self.last_log = self._run(project, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        return calls

    def test_route_b_request_shape(self):
        p = self._project(shots=[self._shot(1, face_visibility="closeup")])
        calls = self._dispatch(p)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["image"], self.scene_sheet, "image 位应是场景基准图")
        self.assertEqual(c["ref_images"],
                         [self.board, self.char_sheet, self.top_sheet,
                          self.prop_sheet],
                         "分镜图不进请求；场景基准图从设定清单剔重")
        self.assertIn("本镜取景地", c["prompt"])
        self.assertIn("@图片1 的场景基准图", c["prompt"], "板的画风归属改指场景图")
        self.assertNotIn("本镜画面参考图", c["prompt"])
        snap = p.data["shots"][0]["gen"]["clip"]
        self.assertEqual(snap.get("face_route"), "B")
        roles = [r["role"] for r in snap["envelope"]["references"]]
        self.assertNotIn("shot_frame", roles, "没发出去的分镜图不得进血缘快照")
        self.assertIn("scene_base", roles)

    def test_closeup_without_board_generates_one_then_walks_b(self):
        """closeup 缺板不许直落 C——与降级轮同一个出口就地生板再走 B，
        否则作者的正确表态反而换来更弱的一档。"""
        shot = self._shot(1, face_visibility="closeup")
        shot["sketch"] = {}      # 无板；beats 由 video_prompt 句读自动拆
        p = self._project(shots=[shot])
        calls = self._dispatch(p)
        c = calls[0]
        self.assertEqual(c["image"], self.scene_sheet)
        self.assertEqual(len(c["ref_images"]), 4, "板+身份图+俯视+道具")
        self.assertTrue(c["ref_images"][0].endswith("_board.png"),
                        "就地生成的板占第一席")
        self.assertEqual(p.data["shots"][0]["gen"]["clip"].get("face_route"), "B")
        self.assertTrue(p.data["shots"][0].get("gen", {}).get("sketch"),
                        "板须经 register_board 登记")

    def test_closeup_boards_batch_before_dispatch(self):
        """多镜 closeup 缺板：计划期整批并发出板（与降级轮同一批量出口），不许
        逐镜同步生板——串行每张约两分钟，整批空等。板到位后按 B 仲裁，日志里
        不得再出现「路线B（…无板…）」这种自相矛盾的理由。"""
        a = self._shot(1, face_visibility="closeup")
        b = self._shot(2, face_visibility="closeup",
                       image=str(_png(self.tmp / "shot2.png")))
        a["sketch"] = {}
        b["sketch"] = {}
        p = self._project(shots=[a, b])
        calls = self._dispatch(p)
        log = self.last_log
        self.assertIn("closeup 预判 2 镜缺简笔板", log)
        self.assertNotIn("已就地生成简笔板", log, "整批出板后循环内不得再逐镜补板")
        self.assertNotIn("无板", log)
        self.assertIn("构图由板驱动", log)
        self.assertEqual(len(calls), 2)
        for c, s in zip(calls, p.data["shots"]):
            self.assertTrue(c["ref_images"][0].endswith("_board.png"))
            self.assertEqual(s["gen"]["clip"].get("face_route"), "B")

    def test_inline_board_rearbitrates_route_reason(self):
        """整批出板落空（如批量出口无产出）时循环内仍就地补板，且路线与理由一并
        重取：只改 route 不改理由会打印「路线B（…无板…）」。"""
        shot = self._shot(1, face_visibility="closeup")
        shot["sketch"] = {}
        p = self._project(shots=[shot])
        empty = {"boards": {}, "failed": [], "no_beats": [], "budget_err": None}
        with unittest.mock.patch("kinema.cli.stage_sketch_boards", return_value=empty):
            calls = self._dispatch(p)
        log = self.last_log
        self.assertIn("已就地生成简笔板", log)
        self.assertIn("降级路线B（作者预判近景正脸，构图由板驱动）", log)
        self.assertNotIn("无板", log)
        self.assertTrue(calls[0]["ref_images"][0].endswith("_board.png"))


class TestPromptContract(unittest.TestCase):
    """降级路线的提示词面：表外 kind 抛错、取景地契约句 ZH/EN 双份、
    character 职责句带环境边界。"""

    def test_unknown_kind_raises(self):
        from kinema.pipeline import prompts
        from kinema.prompt_contract import PromptContractError
        with self.assertRaises(PromptContractError):
            prompts.sheet_binding_clause([("frame", ""), ("identity", "林深")])
        # 占位档照旧占号不产句
        self.assertEqual(prompts.sheet_binding_clause(
            [("frame", ""), ("board", ""), ("scene_base", "")]), "")

    def test_base_contract_both_langs(self):
        from kinema.pipeline import prompts
        shot = {"id": 1, "dur": 5.0, "video_prompt": "转身"}
        for lang, token, old in (("zh", "本镜取景地", "本镜画面"),
                                 ("en", "this shot's location", "this shot's frame")):
            p = prompts.video_prompt(shot, native=True, lang=lang,
                                     ref_mode=True, ref_base=True,
                                     ref_manifest=[("scene_base", ""),
                                                   ("character", "林深")])
            self.assertIn(token, p, f"{lang} 取景地契约句缺失")
            self.assertNotIn(old, p, f"{lang} 不得再说 @图片1 是本镜画面")

    def test_board_role_base_variant_both_langs(self):
        """板职责句的画风归属在降级路线下改指场景基准图，ZH/EN 双份——
        子串替换的目标措辞一旦改动就静默 no-op，两侧都要钉。"""
        from kinema.sketchboard import board_role_clause
        zh = board_role_clause("zh", base="@图片1 的场景基准图")
        self.assertIn("@图片1 的场景基准图", zh)
        self.assertNotIn("本镜画面参考图", zh)
        en = board_role_clause("en", base="the location base plate (@Image 1)")
        self.assertIn("the location base plate (@Image 1)", en)
        self.assertNotIn("this shot's picture reference", en)

    def test_character_duty_carries_the_boundary(self):
        from kinema.pipeline import prompts
        clause = prompts.sheet_binding_clause([("frame", ""), ("character", "林深")])
        self.assertIn("所处环境、光线与构图不取自该图", clause)
        en = prompts.sheet_binding_clause([("frame", ""), ("character", "林深")],
                                          lang="en")
        self.assertIn("backdrop, lighting and framing do not", en)


class TestAutoFallback(_RouteBase):
    """降级轮：路线 A 被人脸拒后自动换 B 重发一次；不可降级不买板；
    降级形态仍被拒是死局、点名身份图。"""

    def _spy_pair(self, behavior):
        """behavior(call_no, image) → 'ok' | 'face'。返回 (calls, runner)。"""
        from kinema.providers.video import mock as vmock
        from kinema.providers.base import VideoResult
        calls = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            calls.append({"image": image, **kw})
            if behavior(len(calls), image) == "face":
                raise ProviderError(
                    "The input image 'content[1]' may contain real person",
                    code="InputImageSensitiveContentDetected.PrivacyInformation")
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                               meta={"provider": "mock"})

        def run(project, expect_raise):
            vmock.MockVideoProvider.generate = spy
            try:
                with unittest.mock.patch("kinema.cli.probe_duration",
                                         return_value=5.0):
                    if expect_raise:
                        with self.assertRaises(KinemaError) as ctx:
                            self._run(project, dry_run=False)
                        return ctx
                    self._run(project, dry_run=False)
                    return None
            finally:
                vmock.MockVideoProvider.generate = orig
        return calls, run

    def test_rejected_shot_degrades_and_resends(self):
        calls, run = self._spy_pair(
            lambda n, img: "face" if img == self.img else "ok")
        p = self._project()
        run(p, expect_raise=False)
        self.assertEqual(len(calls), 2, "被拒后必须按降级装配重发一次")
        self.assertEqual(calls[0]["image"], self.img)
        self.assertEqual(calls[1]["image"], self.scene_sheet)
        self.assertEqual(calls[1]["ref_images"][0], self.board, "板驱动路线B")
        snap = p.data["shots"][0]["gen"]["clip"]
        self.assertEqual(snap.get("face_route"), "B")

    def test_untrusted_identity_buys_no_board(self):
        shot = self._shot(1)
        shot["sketch"] = {}          # 无板：可降级的话就得买板
        p = self._project(shots=[shot], origin="i2i")
        calls, run = self._spy_pair(lambda n, img: "face")
        with unittest.mock.patch("kinema.cli.stage_sketch_boards") as gen_board:
            ctx = run(p, expect_raise=True)
        gen_board.assert_not_called()
        self.assertEqual(len(calls), 1, "不可降级的镜不得重发")
        self.assertIn("--force", str(ctx.exception), "收尾必须给重出身份图的修法")

    def test_budget_stop_skips_the_degrade_round(self):
        """预算断闸后降级轮整轮不进：预处理会买板（付费），而重发注定被停派——
        白花板钱还把镜从收尾清单里挪走。"""
        from kinema.providers.video import mock as vmock
        from kinema.providers.base import VideoResult
        from kinema.project import Project as _P
        calls = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            calls.append(image)
            if len(calls) == 1:
                raise ProviderError(
                    "content[1] may contain real person",
                    code="InputImageSensitiveContentDetected.PrivacyInformation")
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=1.0, has_audio=False,
                               meta={"provider": "mock"})
        shot1 = self._shot(1)
        shot1["sketch"] = {}          # 无板：可降级的话就得买板
        p = self._project(shots=[shot1, self._shot(2)])
        vmock.MockVideoProvider.generate = spy
        try:
            with unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=5.0), \
                 unittest.mock.patch.object(
                     _P, "add_cost",
                     side_effect=KinemaError("预算超限")) as _ac, \
                 unittest.mock.patch("kinema.cli.stage_sketch_boards") as gen_board:
                with self.assertRaises(KinemaError):
                    self._run(p, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        gen_board.assert_not_called()
        self.assertEqual(len(calls), 2, "断闸后不得再有降级重发")

    def test_second_rejection_is_a_deadlock(self):
        p = self._project(shots=[self._shot(1, face_visibility="closeup")])
        calls, run = self._spy_pair(lambda n, img: "face")
        ctx = run(p, expect_raise=True)
        self.assertEqual(len(calls), 1, "降级形态仍被拒不再有第三轮")
        self.assertIn("仍被拒", str(ctx.exception))
        self.assertIn("身份图", str(ctx.exception))


class TestFaceFailDoesNotStopBatch(_RouteBase):
    def test_face_reject_does_not_stop_dispatch(self):
        """镜 1 被人脸拒（建任务 400，不计费）后，镜 2 仍照常派活；
        镜 1 随后进降级轮补发成功——整批零失败收尾。"""
        from kinema.providers.video import mock as vmock
        from kinema.providers.base import VideoResult
        sent = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            sent.append(image)
            if len(sent) == 1:
                raise ProviderError(
                    "input image may contain real person",
                    code="InputImageSensitiveContentDetected.PrivacyInformation")
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = spy
        try:
            p = self._project(shots=[self._shot(1), self._shot(2)])
            with unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=5.0):
                self._run(p, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        self.assertEqual(len(sent), 3, "镜1拒→镜2照发→镜1降级补发")
        self.assertEqual(sent[1], self.img, "镜 2 不受镜 1 人脸拒影响")
        self.assertEqual(sent[2], self.scene_sheet, "降级轮 image 位=场景基准图")
        self.assertEqual(p.data["shots"][0]["gen"]["clip"].get("face_route"), "B")
        self.assertFalse(p.data["shots"][1]["gen"]["clip"].get("face_route"))


class TestFaceVisibilityRegistration(unittest.TestCase):
    def test_in_shot_human_keys(self):
        self.assertIn("face_visibility", project_mod._SHOT_HUMAN_KEYS)

    def test_stage_fields_empty(self):
        # 只决定路线起点，不使任何已产出物过期
        self.assertEqual(review.stages_for("face_visibility"), ())

    def test_batch_editable(self):
        self.assertIn("face_visibility", batch.EDITABLE_FIELDS)


class TestFaceVisibilityConcurrentSave(unittest.TestCase):
    """gen-video 收尾的 save 不得用旧内存副本抹掉期间落盘的表态。"""

    def setUp(self):
        self.env = LocalBackendEnv()
        self.env.enable()
        self.addCleanup(self.env.restore)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "ch01.json"
        self.path.write_text(json.dumps(_doc(), ensure_ascii=False), encoding="utf-8")

    def test_survives_concurrent_save(self):
        engine = Project.load(self.path)          # 长任务：加载即基线
        human = Project.load(self.path)           # 期间的人工表态
        human.data["shots"][0]["face_visibility"] = "closeup"
        human.save()
        engine.save()                             # 旧内存副本收尾
        final = Project.load(self.path)
        self.assertEqual(final.data["shots"][0].get("face_visibility"), "closeup")


if __name__ == "__main__":
    unittest.main()


class TestUntrustedProviderGate(_WsCase):
    """写实档身份图只由受信文生图 provider 直出：闸在归档与计费之前。

    不受信 provider 的 t2i 产物不落在视频侧受信豁免内，却会以 `t2i` 名义
    给降级路线 B/C 背书——放行等于让一张必被人脸审核拒收的图带着受信记录
    流进视频请求。
    """

    def test_identity_character_requires_a_trusted_provider(self):
        from kinema.errors import ProjectError
        from kinema.providers.image import mock as mockmod
        self.s.add_character("林深", appearance="银发青年")
        self.s.save()
        orig = mockmod.MockImageProvider.trusted_face_source
        mockmod.MockImageProvider.trusted_face_source = False
        try:
            with self.assertRaises(ProjectError):
                self._gen_refs("character:林深")
        finally:
            mockmod.MockImageProvider.trusted_face_source = orig
        c = self.ws.get_project("pf").characters[0]
        self.assertFalse(c.get("sheet"), "拦截在生成之前，不许落半张图")
        self.assertFalse(c.get("sheet_origin"), "更不许留下受信来源记录")

    def test_faceless_kinds_do_not_hit_the_gate(self):
        from kinema.providers.image import mock as mockmod
        self.s.add_prop("断刃", desc="一柄长刃")
        self.s.save()
        orig = mockmod.MockImageProvider.trusted_face_source
        mockmod.MockImageProvider.trusted_face_source = False
        try:
            self._gen_refs("prop:断刃")
        finally:
            mockmod.MockImageProvider.trusted_face_source = orig
        self.assertTrue(self.ws.get_project("pf").props[0].get("sheet"),
                        "道具不含人脸，受信闸只管身份图")


class TestRestrictedRefProvider(_WsCase):
    """「角色主体」参考位（ref_kind=character）下的声明与产出对齐。

    该类 provider 非角色档一张参考都附不上：样板声明必须同步归零（声明一张
    没发出去的样板 = 向模型索要不存在的参考），俯视图整波不画（它的唯一参考
    是场景基准图，附不上就画不出同一个空间）。
    """

    PROFILE = "hd2d"

    def _with_restricted(self, fn):
        from kinema.providers.image import mock as mockmod
        orig = getattr(mockmod.MockImageProvider, "ref_kind", "any")
        mockmod.MockImageProvider.ref_kind = "character"
        try:
            return fn()
        finally:
            mockmod.MockImageProvider.ref_kind = orig

    def test_prop_prompt_drops_the_template_claim(self):
        from kinema.providers.image import mock as mockmod
        self.s.add_prop("断刃", desc="一柄长刃")
        self.s.save()
        seen = {}
        orig = mockmod.MockImageProvider.generate

        def spy(prov, prompt, out_path, **kw):
            if str(kw.get("label", "")).startswith("PROP"):
                seen["refs"] = list(kw.get("ref_images") or [])
                seen["prompt"] = prompt
            return orig(prov, prompt, out_path, **kw)
        mockmod.MockImageProvider.generate = spy
        try:
            self._with_restricted(lambda: self._gen_refs("prop:断刃"))
        finally:
            mockmod.MockImageProvider.generate = orig
        self.assertEqual(seen["refs"], [])
        self.assertNotIn("版式样板图", seen["prompt"], "没附样板就不许声明样板")

    def test_topview_wave_is_skipped(self):
        self.s.data["scene"] = "霓虹雨巷"
        self.s.save()
        self._with_restricted(lambda: self._gen_refs("scene"))
        p = self.ws.get_project("pf")
        self.assertTrue(p.data.get("scene_ref"), "场景基准图照常生成")
        self.assertFalse(p.data.get("scene_topview_ref"),
                         "基准图附不上，俯视图不画——占住字段会让跳过判据永远短路")


class TestDirectRegenClearsCandidates(_WsCase):
    """直出定稿清候选三件：残留候选表让跳过判据恒短路，且此后 pick 会用
    上一批的 `sheet_candidates_origin` 覆盖 `sheet_origin`。"""

    PROFILE = "hd2d"

    def _gen_refs_force(self, only):
        from kinema.cli import build_parser
        ns = build_parser().parse_args(
            ["project", "refs", "pf", "--only", only, "--force", "--mock",
             "--workspace", str(self.ws.root)])
        ns.func(ns)

    def test_force_direct_write_clears_stale_candidate_records(self):
        self.s.add_character("林深", appearance="银发青年")
        c = self.s.characters[0]
        c["sheet"] = self._put_sheet("char_林深.png")
        c["sheet_candidates"] = [self._put_sheet("cand_char_林深_1.png")]
        c["sheet_candidates_origin"] = "i2i"
        c["sheet_picked"] = "cand_char_林深_1.png"
        self.s.save()
        self._gen_refs_force("character:林深")
        c2 = self.ws.get_project("pf").characters[0]
        self.assertTrue(c2.get("sheet"))
        self.assertNotIn("sheet_candidates", c2)
        self.assertNotIn("sheet_candidates_origin", c2)
        self.assertNotIn("sheet_picked", c2)


class TestPropagateSyncsChapters(_WsCase):
    """`_propagate` 走 `sync_design_to_chapters` 的白名单：具名场景、道具与
    角色扩展图同一条对齐通路；`sheet_origin` 的空值单独落到章节——该字段
    以缺失表达「来源无记录」，章节里留旧值等于替来历不明的图背书受信。"""

    PROFILE = "hd2d"

    def test_named_scene_sheet_reaches_existing_chapters(self):
        from kinema import refine
        self.s.add_scene("旧书店", desc="临河的旧书店")
        self.s.save()
        self.s.create_chapter("第一章", cid="ch01")
        self.s.scenes[0]["sheet"] = self._put_sheet("scene_旧书店.png")
        self.s.save()
        refine._propagate(self.s)
        data = self.s.ws.store.load_chapter("pf", "ch01")
        got = next(x for x in data["scenes"] if x["name"] == "旧书店")
        self.assertEqual(got.get("sheet"), self.s.scenes[0]["sheet"])

    def test_missing_series_origin_clears_the_chapter_copy(self):
        from kinema import refine
        self.s.add_character("林深", appearance="银发青年")
        self.s.characters[0]["sheet"] = self._put_sheet("char_林深.png")
        self.s.characters[0]["sheet_origin"] = "t2i"
        self.s.save()
        self.s.create_chapter("第一章", cid="ch01")
        self.s.characters[0].pop("sheet_origin", None)
        self.s.save()
        refine._propagate(self.s)
        data = self.s.ws.store.load_chapter("pf", "ch01")
        cc = next(x for x in data["characters"] if x["name"] == "林深")
        self.assertNotIn("sheet_origin", cc)


class TestSendPathPins(unittest.TestCase):
    """真发路径的两处接线钉。

      · 尾帧注入会占一席参考配额：降级路线 B/C 下身份完全由随发身份图承载，
        注入后必须重验身份图仍在实发清单（挤出时承接让位）；
      · closeup 预判镜缺板时真发就地补板升 B，dry-run 按在盘形态编译——
        清单行必须注记升级，否则审的是无板 C 的稿、发出的却是 B 的稿。
    """

    def test_relay_inject_reruns_the_identity_gate(self):
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        seg = src[src.index("def _relay_inject"):]
        seg = seg[:seg.index("def _apply")]
        self.assertIn("_gate_cast_anchor", seg)
        self.assertIn("_relay_recompile(nxt_item, None)", seg,
                      "身份图被配额挤出时承接必须撤销，不是带病发出")

    def test_dry_run_names_the_pending_board_upgrade(self):
        import inspect
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        self.assertIn("路线C→B(真发前自动补板)", src)


class TestDubbedDegrade(_RouteBase):
    """dubbed 参考媒体与 native 全能参考同进路线阶梯。

    两档的人脸敞口相同（image 位都是含人脸的分镜图、都挂参考装配），降级形态
    也相同：image 位换场景基准图、板与受信身份图承载构图与身份，`ref_audio`
    照发对口型。任务门槛的判据单点在 `cli._ref_task`——首帧/衔接/V2V 任务
    协议禁混参考图，仍恒 A。
    """

    def _wav(self, project):
        adir = Path(project.path).parent / f"{Path(project.path).stem}_work" / "audio"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "shot_1.wav").write_bytes(b"RIFFxxxxWAVE")

    def _dispatch(self, project):
        from kinema.providers.video import mock as vmock
        from kinema.providers.base import VideoResult
        calls = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            calls.append({"image": image, **kw})
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=True,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = spy
        try:
            with unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=5.0):
                self._log = self._run(project, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        return calls

    def test_dubbed_closeup_walks_route_b_with_ref_audio(self):
        p = self._project(shots=[self._shot(1, face_visibility="closeup")],
                          motion="dubbed")
        self._wav(p)
        calls = self._dispatch(p)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["image"], self.scene_sheet, "image 位应是场景基准图")
        self.assertTrue(str(c.get("ref_audio", "")).endswith("shot_1.wav"),
                        "对口型音频照发，与降级装配无关")
        self.assertEqual(c["ref_images"],
                         [self.board, self.char_sheet, self.top_sheet,
                          self.prop_sheet])
        self.assertIn("本镜取景地", c["prompt"])
        self.assertNotIn("以所给参考图为画面基准", c["prompt"],
                         "取景地变体在场时不得再按分镜图口径立基准")
        self.assertEqual(p.data["shots"][0]["gen"]["clip"].get("face_route"), "B")
        self.assertIn("路线B·场景图(取景基准)", self._log,
                      "成功行必须按实发组成播报——把 B 路说成分镜图领衔即口径分叉")

    def test_ref_task_is_the_single_gate_for_both_call_sites(self):
        import inspect
        import re
        from kinema import cli
        src = inspect.getsource(cli.stage_gen_video)
        # 计划期与降级轮各自的仲裁调用：判据只有 `_ref_task` 一处，各写一份即
        # dubbed 或 V2V 单侧失效。形参放宽（V2V 判据后加），只钉「经过它」
        self.assertRegex(src, r"ref_task=_ref_task\(prov, ref_mode[,)]",
                         "计划期路线仲裁必须经 _ref_task")
        self.assertRegex(src, r'ref_task=_ref_task\(prov, item\["ref_mode"\][,)]',
                         "降级轮重仲裁必须经同一判据——各写一份即 dubbed 单侧失效")
        self.assertEqual(len(re.findall(r"ref_task=", src)), 3,
                         "仲裁入口就这三处（计划期 / 降级预判 / 板到位后重取）")


class TestControlV2VAssembly(_RouteBase):
    """写实档复刻镜的端到端形态：**深度视频 + 场景基准图 + 受信身份图**，
    分镜图整个不进请求。

    这一条是 V2V + 写实人物能不能出片的分水岭：分镜图挂着设定图生成、属图生图，
    人脸豁免天然不成立；把 V2V 排除在阶梯之外时它恒走路线 A，被拒后无处可退。
    """

    def _v2v_shot(self, no=1, **over):
        seg = str(_png(self.tmp / "seg.mp4"))          # 只要在盘，内容不参与判定
        s = self._shot(no, control=seg, face_visibility="closeup",
                       gen={"control": {"asset": "a", "seconds": 5, "start": 0.0}})
        s.update(over)
        return s

    def test_request_carries_the_depth_video_scene_and_identity_sheet(self):
        p = self._project(shots=[self._v2v_shot()], control_video=True)
        calls = TestRouteBAssembly._dispatch(self, p)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["image"], self.scene_sheet, "image 位是场景基准图")
        self.assertTrue(str(c.get("reference_video", "")).endswith("seg.mp4"),
                        "控制视频照发——运动权威在它身上")
        self.assertIn(self.char_sheet, c["ref_images"], "受信身份图必须进请求")
        self.assertNotIn(self.img, [c["image"], *c["ref_images"]],
                         "分镜图整个不进请求")
        self.assertNotIn(self.board, c["ref_images"],
                         "板与控制视频是两个并列的运动权威，不同发")
        self.assertEqual(p.data["shots"][0]["gen"]["clip"].get("face_route"), "C")
        # 图发了就得告诉模型每一张是谁：图片1 是空景基准图而非本镜画面，身份图与
        # 俯视图各有职责句——缺了职责句，身份图只是一张无名参考，俯视图的线条会被画进画面
        prompt = c["prompt"]
        self.assertIn("@图片1（本镜取景地）", prompt, "画面基准半句换取景地变体")
        self.assertNotIn("以所给@图片1为画面基准", prompt)
        self.assertIn("@图片2 为角色「林深」的设定图", prompt)
        self.assertIn("俯视布局图", prompt)
        self.assertIn("@视频1", prompt, "运动半句照旧指向控制视频")

    def test_route_a_v2v_names_every_attached_sheet(self):
        """非写实档的控制视频镜走路线 A：分镜图领衔、设定图随发，随发的每一张同样要有
        职责句；画面基准半句仍是分镜图那一句。"""
        p = self._project(shots=[self._v2v_shot()], control_video=True, profile="anime")
        calls = TestRouteBAssembly._dispatch(self, p)
        c = calls[0]
        self.assertEqual(c["image"], self.img)
        self.assertIn(self.char_sheet, c["ref_images"])
        self.assertTrue(str(c.get("reference_video", "")).endswith("seg.mp4"))
        self.assertIn("以所给@图片1为画面基准", c["prompt"])
        self.assertIn("@图片2 为角色「林深」的设定图", c["prompt"])

    def test_face_rejection_index_skips_the_video_slot(self):
        """V2V 的 content[] 是 [text, image, video, refs…]：官方报 content[3] 指的是
        视频之后的第一张参考图（@图片2），按图片连续编号翻会点错一张。"""
        from kinema.cli import stage_gen_video
        from kinema.errors import KinemaError, ProviderError
        from kinema.models import ConfigStore, ModelRouter
        from kinema.providers.video import mock as vmock
        p = self._project(shots=[self._v2v_shot()], control_video=True)
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            raise ProviderError("The input image 'content[3]' may contain real person",
                                code="InputImageSensitiveContentDetected.PrivacyInformation")
        vmock.MockVideoProvider.generate = spy
        store = ConfigStore.load(None)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                    unittest.mock.patch("kinema.cli.probe_duration", return_value=5.0):
                with self.assertRaises(KinemaError):
                    stage_gen_video(p, store, ModelRouter(store, force_mock=True),
                                    dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        self.assertIn("被拒的是 @图片2：角色身份图「林深」", buf.getvalue())

    def test_clip_lineage_records_each_shots_own_segment(self):
        """回填在计划循环结束后才跑，取循环变量就是拿最后一镜的控制段给每一镜记账：
        重裁区间后 `lineage mark` 对错镜报过期、对本镜漏报。"""
        seg_a = str(_png(self.tmp / "seg_a.mp4"))
        seg_b = str(_png(self.tmp / "seg_b.mp4"))
        shots = [self._v2v_shot(1, control=seg_a),
                 self._shot(2),
                 self._v2v_shot(3, control=seg_b)]
        p = self._project(shots=shots, control_video=True)
        TestRouteBAssembly._dispatch(self, p)
        refs = [list(s["gen"]["clip"].get("refs") or {}) for s in p.data["shots"]]
        self.assertIn(seg_a, refs[0])
        self.assertNotIn(seg_b, refs[0])
        self.assertFalse([r for r in refs[1] if r.endswith(".mp4")], "未绑的镜不记控制段")
        self.assertIn(seg_b, refs[2])

    def test_dry_run_prices_no_board_for_v2v_shots(self):
        """控制视频的降级恒不挂板，报价里不能给它算「被拒时才补板」的板费；
        近景预判镜本就从降级形态起步，也不该说成「可能触发降级轮」。"""
        p = self._project(shots=[self._v2v_shot()], control_video=True)
        out = self._run(p)
        self.assertIn("直接以降级形态发出", out)
        self.assertNotIn("补板", out)


class TestDubbedBaseContract(unittest.TestCase):
    """dubbed 降级稿的契约句：取景地变体与全能参考共用同一句（单源），
    ZH/EN 双份；ref_base 不在场时逐字回归参考媒体基线句。"""

    SHOT = {"id": 1, "dur": 5.0, "video_prompt": "转身"}

    def test_base_variant_both_langs(self):
        from kinema.pipeline import prompts
        for lang, token, old in (("zh", "本镜取景地", "以所给参考图为画面基准"),
                                 ("en", "this shot's location",
                                  "Treat the given reference image")):
            p = prompts.video_prompt(dict(self.SHOT), native=False, lang=lang,
                                     ref_base=True,
                                     ref_manifest=[("scene_base", ""),
                                                   ("character", "林深")])
            self.assertIn(token, p, f"{lang} 取景地契约句缺失")
            self.assertNotIn(old, p, f"{lang} 不得再按分镜图口径立基准")

    def test_no_base_keeps_the_reference_baseline(self):
        from kinema.pipeline import prompts
        p = prompts.video_prompt(dict(self.SHOT), native=False, lang="zh",
                                 ref_base=False,
                                 ref_manifest=[("frame", ""),
                                               ("character", "林深")])
        self.assertIn("以所给参考图为画面基准", p)
        self.assertNotIn("本镜取景地", p)


class TestOutputPolicyRejection(_RouteBase):
    """输出侧审核拒收（任务 failed·OutputVideoSensitiveContentDetected）的分流。

    判的是渲染出来的成片内容：与输入参考装配无关，降级换装配与同参数重跑都
    改变不了判定——不进降级轮；一镜的内容判定也不停其余镜的派活（与人脸拒
    同款分流）；收尾按「改内容」口径给处置，绝不引导重跑或降级。
    """

    CODE = "OutputVideoSensitiveContentDetected.PolicyViolation"

    def test_one_rejection_does_not_stop_the_batch_or_degrade(self):
        from kinema.errors import ProviderError, KinemaError
        from kinema.providers.video import mock as vmock
        from kinema.providers.base import VideoResult
        p = self._project(shots=[self._shot(1), self._shot(2)])
        calls = []
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            calls.append(image)
            if len(calls) == 1:
                raise ProviderError("Seedance 任务failed: output blocked",
                                    code=self.CODE)
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=True,
                               meta={"provider": "mock"})
        vmock.MockVideoProvider.generate = spy
        try:
            with unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=5.0):
                with self.assertRaises(KinemaError) as ctx:
                    self._run(p, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        self.assertEqual(len(calls), 2, "输出拒不停派——第 2 镜照常发出")
        msg = str(ctx.exception)
        self.assertIn("输出侧审核", msg)
        self.assertIn("改内容", msg)
        self.assertIn("降级换装配与同参数重跑都改变不了判定", msg,
                      "处置必须说明降级与重跑都救不了，防止读成可重试")
        self.assertTrue(p.data["shots"][1].get("clip"), "第 2 镜成片照常登记")
        self.assertFalse(p.data["shots"][0].get("clip"), "被拒镜不留产物")

    def test_advice_splits_by_error_code(self):
        from types import SimpleNamespace as NS
        from kinema.cli import _retry_advice
        from kinema.errors import ProviderError

        def done(code, route="A"):
            return NS(error=ProviderError("x", code=code), meta={"route": route},
                      label="镜", message="x")
        out = _retry_advice([done(self.CODE),
                             done("InputImageSensitiveContentDetected.Privacy")])
        self.assertIn("输出侧审核", out)
        self.assertIn("输入图审核未通过", out)
        self.assertNotIn("已成功的镜已登记落盘", out,
                         "两类都已给专属口径时不附通用重跑尾注")
        out2 = _retry_advice([done(None)])
        self.assertIn("已成功的镜已登记落盘", out2)


class TestDegradeLogConsistency(_RouteBase):
    """降级轮里补板成功后，路线与理由必须出自同一次仲裁——只改 route2 不改
    why2 会打出「路线B（…无板…）」这种自相矛盾的行，把人误导向「板没生出来」，
    再误导向「路线 B 不行」的错误处置。"""

    def test_bought_board_updates_route_and_reason_together(self):
        from kinema.providers.base import VideoResult
        from kinema.providers.video import mock as vmock
        shot = self._shot(1)
        shot["sketch"] = {}                       # 无板：降级须现场补板
        p = self._project(shots=[shot])
        board2 = str(_png(self.tmp / "board2.png"))
        orig = vmock.MockVideoProvider.generate

        def spy(prov, image, out_path, **kw):
            if image == self.img:
                raise ProviderError(
                    "content[1] may contain real person",
                    code="InputImageSensitiveContentDetected.PrivacyInformation")
            Path(out_path).write_bytes(b"clip")
            return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                               meta={"provider": "mock"})

        vmock.MockVideoProvider.generate = spy
        try:
            with unittest.mock.patch("kinema.cli.stage_sketch_boards",
                                     return_value={"boards": {"1": board2}, "failed": [],
                                                   "no_beats": [], "budget_err": None}), \
                 unittest.mock.patch("kinema.cli.probe_duration",
                                     return_value=5.0):
                out = self._run(p, dry_run=False)
        finally:
            vmock.MockVideoProvider.generate = orig
        self.assertIn("降级路线B（人脸拒后降级，构图由板驱动）", out)
        self.assertNotIn("路线B（人脸拒后降级·无板", out)
