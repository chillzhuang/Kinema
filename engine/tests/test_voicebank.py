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

"""音色档案库：候选是临时物、档案是资产、引用账是删除闸。

守的是三件会静默出错的事：重新试音**不许**动到在用的那把声音（页面上的选中态
必须由档案而不是数组下标决定）；一条档案的音频**不可变**（覆盖=历史那把声音
被物理销毁）；删除**必须**先查引用（删掉一把已经烧进配音的声音，下游产物从此
无从溯源）。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kinema import voicebank
from kinema.errors import KinemaError

from tests.support import LocalBackendEnv


class _Store:
    """音色目录桩：voices=别名→voice_type。"""

    def __init__(self, voices):
        self.voices = voices

    def resolve_voice(self, ref):
        return self.voices.get(ref, ref) if ref else None


class _Prov:
    """TTS 桩：把「合成」落成一个内容可辨的文件，用来验证音频有没有被覆盖。"""

    def __init__(self):
        self.calls = 0

    def synthesize(self, text, out, **kw):
        self.calls += 1
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(f"{self.calls}:{kw.get('voice') or 'custom'}", encoding="utf-8")
        return {"cost": 0.0}


class _Router:
    def __init__(self, prov):
        self.prov = prov

    def resolve(self, kind, profile=None):
        return self.prov, {}

    def resolve_named(self, kind, name):
        return self.prov


VOICES = {
    "热血少年": "ICL_uranus_zh_male_a_tob", "冷酷哥哥": "ICL_uranus_zh_male_b_tob",
    "少年将军": "ICL_uranus_zh_male_c_tob", "霸道总裁": "ICL_uranus_zh_male_d_tob",
    "温柔学长": "ICL_uranus_zh_male_e_tob", "神秘法师": "ICL_uranus_zh_male_f_tob",
    "元气甜妹": "ICL_uranus_zh_female_a_tob", "温柔女神": "ICL_uranus_zh_female_b_tob",
    "可爱女生": "ICL_uranus_zh_female_c_tob", "成熟姐姐": "ICL_uranus_zh_female_d_tob",
    "磁性男嗓": "zh_male_cixing_uranus_bigtts", "云舟": "zh_male_m191_uranus_bigtts",
    "深夜播客": "zh_male_shenyeboke_uranus_bigtts", "知性女声": "zh_female_zhixing_uranus_bigtts",
    "醇厚低音": "ICL_uranus_zh_male_chunhou_tob", "治愈女": "ICL_uranus_zh_female_zhiyu_tob",
    "温柔文雅": "ICL_uranus_zh_female_wenrou_tob", "渊博小叔": "ICL_uranus_zh_male_yuanbo_tob",
    "精灵向导": "ICL_uranus_zh_female_jingling_tob",
    "Vivi": "zh_female_vv_uranus_bigtts",
    "儒雅旁白": "zh_male_ruyaqingnian_uranus_bigtts",   # 播音腔（避）
    "擎苍": "zh_male_qingcang_uranus_bigtts",           # 播音腔（避）
    "沧桑老者": "zh_male_qingcang_uranus_bigtts",       # 同 voice_type 别名
    "Tim": "en_male_tim_uranus_bigtts",                # 多语种（不进中文推荐）
}


class _BankCase(unittest.TestCase):
    """带真实工作区的基例：档案要落文件，路径与覆盖行为都得真验。"""

    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()
        self.tmp = Path(tempfile.mkdtemp())
        self.store = _Store(VOICES)
        self.prov = _Prov()
        self.router = _Router(self.prov)
        from kinema.workspace import Workspace
        self.ws = Workspace.open(str(self.tmp / "project"))
        self.s = self.ws.create_project("选角", pid="vb", profile="anime")
        self.s.add_character("林深")
        self.s.save()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._env.restore()

    def _reload(self):
        from kinema.workspace import Workspace
        return Workspace.open(str(self.tmp / "project"), create=False).get_project("vb")

    def _chapter(self, cid="ch01", shots=None):
        self.s.create_chapter("第一章", cid=cid)
        data = self.ws.store.load_chapter("vb", cid)
        data["shots"] = shots or []
        self.ws.store.save_chapter("vb", cid, data)
        return data


class TestLedger(_BankCase):
    def test_reaudition_touches_nothing_that_is_in_use(self):
        """重新试音只是多出一批候选。它动到在用音色的话，页面上的「已选」就会
        指向一条与实际在用完全不同的音频——正是选中态错乱的根因。"""
        voicebank.audition(self.store, self.router, self.s, "林深")
        first = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.audition(self.store, self.router, self.s, "林深")
        s = self._reload()
        ent = next(c for c in s.characters if c["name"] == "林深")
        self.assertEqual(ent["voice"], first["voice"], "重新试音把在用音色改掉了")
        self.assertEqual(len(voicebank.casts_of(s.data)), 1, "重新试音不该产生档案")
        self.assertEqual(ent["audition"]["batch"], 2, "新一批候选没换批次号")

    def test_custom_candidates_are_claimed_by_where_they_came_from(self):
        """定制每次演绎都不同，只有出处能把候选与档案对上。批次一换，旧编号在新一批里
        指的是另一条音频——把选中态存成下标就是在存一个会失效的指针。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=2)
        voicebank.use_custom(self.s, self.store, "林深", 1)
        got = voicebank.bank_view(self._reload(), "林深")["custom_audition"]["entries"]
        self.assertEqual([e["no"] for e in got if e["cast"]], [1])
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=2)
        view = voicebank.bank_view(self._reload(), "林深")
        self.assertTrue(all(e["cast"] is None for e in view["custom_audition"]["entries"]),
                        "新一批定制不该有任何一条显示成已入档")

    def test_preset_candidates_are_claimed_by_the_voice_itself(self):
        """模版按 voice_type 认：换一批试音再遇到同一把官方音色，它仍然是同一把声音，
        标成「未入档」会诱导人再点一次「用这条」。"""
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        r = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["冷酷哥哥", "热血少年"])
        got = voicebank.bank_view(self._reload(), "林深")["audition"]["entries"]
        self.assertEqual([(e["no"], e["cast"]) for e in got],
                         [(1, None), (2, r["cast"])])

    def test_every_custom_pick_is_its_own_immutable_cast(self):
        """定制音色每次演绎都不同，被选中的那条**就是**这把音色。三次选定要得到
        三条档案、三个身份、三份互不覆盖的音频。"""
        clips, ids = [], []
        for _ in range(3):
            voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                      prompt="中年男性，嗓音低沉", count=2)
            r = voicebank.use_custom(self.s, self.store, "林深", 1)
            ids.append(r["cast"])
            clips.append(Path(r["clip"]))
        s = self._reload()
        casts = voicebank.casts_of(s.data)
        self.assertEqual(len(casts), 3)
        self.assertEqual(len(set(ids)), 3, "档案号复用了")
        self.assertEqual(len({c["voice_type"] for c in casts}), 3,
                         "定制音色的 voice_type 必须按档案唯一，否则分镜留痕无从溯源")
        self.assertEqual(len({p.read_text(encoding="utf-8") for p in clips}), 3,
                         "档案音频被后一次选定覆盖了——上一把声音已被物理销毁")
        self.assertTrue(all(p.is_file() for p in clips))

    def test_same_preset_voice_reuses_its_cast(self):
        """同一把官方音色在同一实体名下只该有一条档案：立两条的话
        `(实体, 音色引用) → 档案` 不再唯一，「在用哪条」随即无解。"""
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        a = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.use_audition(self.s, self.store, "林深", 2)
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年"])
        b = voicebank.use_audition(self.s, self.store, "林深", 1)
        self.assertEqual(a["cast"], b["cast"])
        self.assertEqual(len(voicebank.casts_of(self._reload().data)), 2)

    def test_switching_back_restores_the_old_voice(self):
        """换回历史档案是「互换」不是「重来」：两条档案都还在，指派换过去。"""
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        first = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.use_audition(self.s, self.store, "林深", 2)
        back = voicebank.use_cast(self.s, self.store, first["cast"])
        s = self._reload()
        self.assertEqual(back["voice"], first["voice"])
        self.assertEqual(voicebank.owner_ref(s.data, "林深"), first["voice"])
        self.assertEqual(len(voicebank.casts_of(s.data)), 2, "换回不该删掉另一条")

    def test_active_cast_is_derived_not_stored(self):
        """在用状态只由实体的 `voice` 推导。存第二份指针就一定会与它漂移。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=1)
        r = voicebank.use_custom(self.s, self.store, "林深", 1)
        s = self._reload()
        view = voicebank.bank_view(s, "林深")
        self.assertEqual(view["active"], r["cast"])
        self.assertEqual([c["id"] for c in view["casts"] if c["active"]], [r["cast"]])
        # 手工改指派（character set --voice）→ 落到「未入档」，而不是继续显示已选
        next(c for c in s.characters if c["name"] == "林深")["voice"] = "热血少年"
        s.save()
        self.assertIsNone(voicebank.bank_view(self._reload(), "林深")["active"])

    def test_chapters_carry_the_bank_so_they_resolve_alone(self):
        """章节要能脱离项目文档独立渲染：定制音色的参考音路径只能从随行的档案库解析。"""
        self._chapter()
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=1)
        r = voicebank.use_custom(self.s, self.store, "林深", 1)
        data = self.ws.store.load_chapter("vb", "ch01")
        self.assertEqual(data["voices"]["林深"], r["voice"])
        self.assertEqual(voicebank.clip_for(data, r["voice_type"]), r["clip"])
        self.assertEqual(voicebank.voice_desc(data, "林深"), "低沉")

    def test_old_audition_batches_are_pruned_but_casts_survive(self):
        """候选目录滚动清理；已立档的音频在 casts/ 另有不可变副本，清不到。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="第一版", count=1)
        r = voicebank.use_custom(self.s, self.store, "林深", 1)
        for i in range(voicebank.KEEP_BATCHES + 1):
            voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                      prompt=f"第{i}版", count=1)
        root = self.s.dir / "assets" / "voices" / "auditions" / "林深" / "custom"
        self.assertEqual(len(list(root.iterdir())), voicebank.KEEP_BATCHES)
        self.assertTrue(Path(r["clip"]).is_file(), "已立档的音频被候选清理带走了")


class TestReferences(_BankCase):
    def _voiced_chapter(self, voice_type):
        self._chapter(shots=[
            {"id": 1, "speaker": "林深", "narration": "一句",
             "gen": {"audio": {"voice_type": voice_type}}},
        ])

    def test_in_use_cast_can_never_be_deleted(self):
        voicebank.audition(self.store, self.router, self.s, "林深")
        r = voicebank.use_audition(self.s, self.store, "林深", 1)
        with self.assertRaises(KinemaError) as cm:
            voicebank.delete_cast(self.s, r["cast"])
        self.assertIn("正在使用", str(cm.exception))

    def test_generated_shot_blocks_deletion_and_names_the_shot(self):
        """已经花钱合成过配音的镜是硬引用：删了它，那条音轨从此没有出处。"""
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        used = voicebank.use_audition(self.s, self.store, "林深", 1)
        self._voiced_chapter(used["voice_type"])
        voicebank.use_audition(self.s, self.store, "林深", 2)          # 换走，解除「在用」
        refs = voicebank.cast_references(self._reload(), used["cast"])
        self.assertEqual(refs["generated"], [{"chapter": "ch01", "shot": 1}])
        self.assertFalse(refs["deletable"])
        with self.assertRaises(KinemaError) as cm:
            voicebank.delete_cast(self._reload(), used["cast"])
        self.assertIn("ch01 镜 1", str(cm.exception))

    def test_line_level_assignment_counts_as_a_reference(self):
        """句级显式音色同样是指派——只扫镜级会让多角色镜的引用整个漏掉。"""
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        used = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.use_audition(self.s, self.store, "林深", 2)
        self._chapter(shots=[{"id": 7, "lines": [{"text": "喂", "voice": used["voice"]}]}])
        refs = voicebank.cast_references(self._reload(), used["cast"])
        self.assertIn({"chapter": "ch01", "where": "镜 7"}, refs["assigned"])
        self.assertFalse(refs["deletable"])

    def test_archived_audio_version_still_counts(self):
        """归档版本里的音色也算产出过——版本栈随时可以回滚回去。"""
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        used = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.use_audition(self.s, self.store, "林深", 2)
        self._chapter(shots=[{"id": 3, "versions": {
            "audio": [{"v": 1, "params": {"voice_type": used["voice_type"]}}]}}])
        self.assertFalse(voicebank.cast_references(self._reload(), used["cast"])["deletable"])

    def test_manual_assignment_by_another_entity_blocks_deletion(self):
        """在用面要查全部实体：把另一个角色的 voice 手工写成同一个 custom:vc_*
        是合法形态，只查档案自己的 owner 会让删除闸放行——删掉后那个角色的
        配音在请求期才炸。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=1)
        picked = voicebank.use_custom(self.s, self.store, "林深", 1)
        self.s.add_character("旁人")
        next(c for c in self.s.characters
             if c["name"] == "旁人")["voice"] = picked["voice"]
        self.s.save()
        # 档案 owner 自己换走：只查 owner 的判据会把这条档案判成无人在用
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年"])
        voicebank.use_audition(self.s, self.store, "林深", 1)
        refs = voicebank.cast_references(self._reload(), picked["cast"])
        self.assertIn("旁人", refs["in_use"])
        self.assertFalse(refs["deletable"])

    def test_no_reference_to_the_removed_pick_verb(self):
        """`voice pick` 已重做为 `voice use`：报错文案/注释里残留旧动词，
        会把用户引去一条不存在的命令。"""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "kinema"
        bad = []
        for p in sorted(root.rglob("*.py")):
            src = p.read_text(encoding="utf-8")
            for marker in ("voice pick", "audition/pick"):
                if marker in src:
                    bad.append(f"{p.relative_to(root)}: {marker}")
        self.assertEqual(bad, [])

    def test_audition_text_with_braces_is_legal_input(self):
        # str.format 会把用户台词里的字面花括号当占位符解析——只认 {name} 一个记号
        line = voicebank._audition_line("林深", "今天心情{很好}，{name}上场")
        self.assertEqual(line, "今天心情{很好}，林深上场")

    def test_unreferenced_cast_deletes_cleanly(self):
        """无引用才放行：档案条目与那条音频同时清干净，章节侧的随行副本一并更新。"""
        self._chapter()
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        dead = voicebank.use_audition(self.s, self.store, "林深", 1)
        voicebank.use_audition(self.s, self.store, "林深", 2)
        clip = Path(dead["clip"])
        voicebank.delete_cast(self._reload(), dead["cast"])
        s = self._reload()
        self.assertIsNone(voicebank.find_cast(s.data, dead["cast"]))
        self.assertFalse(clip.is_file(), "档案删了音频还留在盘上")
        data = self.ws.store.load_chapter("vb", "ch01")
        self.assertIsNone(voicebank.find_cast(data, dead["cast"]),
                          "章节侧还留着已删档案，脱机渲染会解析到一条不存在的音色")

    def test_reference_index_is_built_once_for_the_whole_view(self):
        """一次页面渲染要看十几条档案，逐条重扫等于把章节文档读上十几遍。"""
        self._chapter()
        voicebank.audition(self.store, self.router, self.s, "林深")
        voicebank.use_audition(self.s, self.store, "林深", 1)
        s = self._reload()
        loads = []
        real = s.ws.store.load_chapter
        s.ws.store.load_chapter = lambda pid, cid: (loads.append(cid), real(pid, cid))[1]
        voicebank.bank_views(s)
        self.assertEqual(len(loads), 1, f"章节被重复加载 {len(loads)} 次")


class TestMalformedDocsDoNotTakeDownThePage(_BankCase):
    """读侧对字段形状一律不作假设。

    文档是长期演进的用户数据，盘上总会有手改过的、上一版留下的形状。读侧崩一次
    就是整页 500——而项目页恰恰是用户唯一能看见「音色出了什么问题」的地方。
    判据只有一条：形状不对就当没有，绝不试图翻译。
    """

    def test_view_survives_every_wrong_shape(self):
        s = self._reload()
        ent = next(c for c in s.characters if c["name"] == "林深")
        for bad in ([{"no": 1}], "字符串", 7, None):
            ent["audition"] = ent["custom_audition"] = bad
            s.data["voice_bank"] = bad
            v = voicebank.bank_view(s, "林深")
            self.assertEqual(v["casts"], [])
            self.assertEqual(v["audition"]["entries"], [])
            self.assertEqual(v["custom_audition"]["entries"], [])

    def test_resolution_survives_wrong_shapes(self):
        for bad in ("x", 7, [1, 2], None):
            doc = {"voice_bank": bad, "narrator": bad, "characters": bad, "voices": bad}
            self.assertIsNone(voicebank.owner_ref(doc, "林深"))
            self.assertIsNone(voicebank.clip_for(doc, "custom:vc_0001"))
            self.assertIsNone(voicebank.voice_desc(doc, voicebank.NARRATOR))


class TestMalformedDocsStillTakeWrites(_BankCase):
    """写侧同样不对形状作假设——读侧只是「当没有」，写侧还得写得进去。

    候选块 `audition` 上一版是列表，`{batch, entries}` 化之后旧项目一点「试音」
    就 `'list' object has no attribute 'get'`：读侧的 `bank_view` 早已挡住、
    项目页照常打开，于是这条只在**用户点下去那一刻**才现形。旧形状按作废处理，
    新一批就地把它顶掉，绝不翻译旧字段。
    """

    LEGACY = [{"no": 1, "voice": "热血少年",
               "voice_type": "ICL_uranus_zh_male_a_tob", "path": "/nowhere.mp3"}]

    def test_audition_replaces_legacy_list_block(self):
        for owner, seed in (("林深", None), (voicebank.NARRATOR, {})):
            if seed is not None:
                self.s.data["narrator"] = dict(seed)
            ent = voicebank._entity(self.s, owner)
            ent["audition"] = ent["custom_audition"] = list(self.LEGACY)
            self.s.save()
            r = voicebank.audition(self.store, self.router, self._reload(), owner,
                                   candidates=["热血少年", "冷酷哥哥"])
            self.assertEqual(r["batch"], 1, "旧形状没被当作废：批次号从旧块续了")
            ent = voicebank._entity(self._reload(), owner)
            self.assertIsInstance(ent["audition"], dict)
            self.assertEqual(len(ent["audition"]["entries"]), 2)
            c = voicebank.custom_audition(self.store, self.router, self._reload(),
                                          owner, prompt="低沉沙哑", count=1)
            self.assertEqual(c["batch"], 1)

    def test_use_audition_survives_legacy_bank_shapes(self):
        """立档路径：候选块与档案库两处旧形状同时在场也要能选定。"""
        s = self._reload()
        s.data["voice_bank"] = []
        ent = voicebank._entity(s, "林深")
        ent["audition"] = list(self.LEGACY)
        s.save()
        voicebank.audition(self.store, self.router, self._reload(), "林深",
                           candidates=["热血少年"])
        r = voicebank.use_audition(self._reload(), self.store, "林深", 1)
        self.assertEqual(r["voice"], "热血少年")
        self.assertEqual(len(voicebank.casts_of(self._reload().data)), 1)

    def test_legacy_number_is_not_silently_honoured(self):
        """旧块里的编号不该还能选中——那条音频的路径早已不在盘上，
        放行就是拿一个失效指针去立档。"""
        s = self._reload()
        voicebank._entity(s, "林深")["audition"] = list(self.LEGACY)
        s.save()
        with self.assertRaises(KinemaError):
            voicebank.use_audition(self._reload(), self.store, "林深", 1)


class _BoomProv:
    """合成必炸的 TTS 桩：验证预热失败不把已成立的选角判成失败。"""

    def synthesize(self, text, out, **kw):
        raise RuntimeError("TTS 不可用")


class TestAnchorPrewarm(_BankCase):
    """选定即预热：锚定参考音在「选定」那一刻就落盘。

    锚定音是 native 真发时随请求附发的那把嗓子，也是页面「参考音频N」点开听到的
    东西。它不在盘上，人就只能不试听直接开生视频去赌音色——生视频按秒计费，
    赌错一次重出的钱远多于这一句 TTS。
    """

    def _chapter_project(self, cid="ch01"):
        import json

        from kinema.project import Project
        self._chapter(cid)
        p = self._reload().get_chapter_path(cid)
        return Project(p, json.loads(p.read_text("utf-8")))

    def test_selection_warms_the_exact_clip_the_send_path_reads(self):
        """选角期预热的与真发读取的必须是**同一个文件**：两侧各拼一份路径的话，
        页面上试听到的那条根本没被发出去，而锚定要根治的正是这个。"""
        proj = self._chapter_project()
        voicebank.audition(self.store, self.router, self.s, "林深", candidates=["热血少年"])
        r = voicebank.use_audition(self.s, self.store, "林深", 1, router=self.router)
        self.assertTrue(r["anchor"] and Path(r["anchor"]).is_file(), "选定没有把锚定音落盘")
        clip, custom = voicebank.anchor_clip_for(proj, VOICES["热血少年"])
        # resolve：临时目录在 macOS 上带 /private 软链，比的是同一个文件不是同一个串
        self.assertEqual(Path(clip).resolve(), Path(r["anchor"]).resolve(),
                         "选角期预热的与真发读取的不是同一条")
        self.assertFalse(custom, "模版音色被当成了定制档案")

    def test_preset_warms_once_and_custom_needs_no_synthesis(self):
        """两条花钱的判据：模版音色只合成一次（此后命中缓存），
        定制音色一次都不合成（档案那条不可变音频**就是**它的锚定音）。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉沙哑", count=1)
        n = self.prov.calls
        r = voicebank.use_custom(self.s, self.store, "林深", 1, router=self.router)
        self.assertEqual(self.prov.calls, n, "定制音色的锚定音被重新合成了一次")
        self.assertEqual(r["anchor"], r["clip"])

        voicebank.audition(self.store, self.router, self.s, "林深", candidates=["热血少年"])
        n = self.prov.calls
        voicebank.use_audition(self.s, self.store, "林深", 1, router=self.router)
        self.assertEqual(self.prov.calls, n + 1, "选定没有预热锚定音")
        n = self.prov.calls
        voicebank.use_audition(self.s, self.store, "林深", 1, router=self.router)
        self.assertEqual(self.prov.calls, n, "锚定音重复合成——每换回一次就白花一次钱")

    def test_a_failing_preheat_never_fails_the_casting(self):
        """预热是选角的附加动作，不是它的前置条件：TTS 挂了照样把声音选上，
        真发那一刻还会再试一次。"""
        voicebank.audition(self.store, self.router, self.s, "林深", candidates=["热血少年"])
        r = voicebank.use_audition(self.s, self.store, "林深", 1,
                                   router=_Router(_BoomProv()))
        self.assertIsNone(r["anchor"])
        self.assertEqual(voicebank.owner_ref(self._reload().data, "林深"), "热血少年")

    def test_no_router_means_no_synthesis_at_all(self):
        """纯数据路径（不配 provider 的调用方）跳过预热，不触发任何合成。"""
        voicebank.audition(self.store, self.router, self.s, "林深", candidates=["热血少年"])
        n = self.prov.calls
        r = voicebank.use_audition(self.s, self.store, "林深", 1)
        self.assertIsNone(r["anchor"])
        self.assertEqual(self.prov.calls, n)


class TestPropagation(_BankCase):
    """换了声音，已经配过音的镜就过期了。

    `stage_tts` 的重合成判据是「wav 在不在盘」，它看不见音色换没换——不传播的话
    一章会安静地停在一半旧声一半新声。这与设定图换版必须传播过期是同一条纪律。
    """

    def _two_voices(self):
        voicebank.audition(self.store, self.router, self.s, "林深",
                           candidates=["热血少年", "冷酷哥哥"])
        return (voicebank.use_audition(self.s, self.store, "林深", 1),
                lambda: voicebank.use_audition(self.s, self.store, "林深", 2))

    def _shots(self, voice_type, *, state=None):
        s = {"id": 1, "speaker": "林深", "narration": "一句",
             "gen": {"audio": {"voice_type": voice_type}}}
        if state:
            s["review"] = {"audio": {"state": state}}
        return [s]

    def test_unlocked_shot_is_marked_for_retake(self):
        first, switch = self._two_voices()
        self._chapter(shots=self._shots(first["voice_type"]))
        r = switch()
        self.assertEqual((r["voice_retake"], r["voice_stale"]), (1, 0))
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertEqual(shot["review"]["audio"]["state"], "retake")

    def test_approved_shot_is_only_flagged_never_unlocked(self):
        """锁是人给的，机器不越权解锁——只挂标记等人裁决。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._shots(first["voice_type"], state="done"))
        r = switch()
        self.assertEqual((r["voice_retake"], r["voice_stale"]), (0, 1))
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertEqual(shot["review"]["audio"]["state"], "done")
        self.assertEqual(shot["voice_stale"], [first["voice_type"]])

    def test_locked_shot_already_flagged_is_not_recounted(self):
        """再次传播（换的是别人的音色、或同一把重复启用）不把已标同值的锁定镜再算一遍。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._shots(first["voice_type"], state="done"))
        self.assertEqual(switch()["voice_stale"], 1)
        self.assertEqual(switch()["voice_stale"], 0)
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertEqual(shot["voice_stale"], [first["voice_type"]])

    def test_switching_back_and_forth_is_idempotent(self):
        """来回切五次不该越标越多——判据是「现在该用哪把」对不对得上留痕，
        不是「切过几次」。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._shots(first["voice_type"], state="done"))
        for _ in range(5):
            switch()
            voicebank.use_cast(self.s, self.store, first["cast"])
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertNotIn("voice_stale", shot, "换回原来那把之后标记该消失")
        self.assertEqual(shot["review"]["audio"]["state"], "done", "原表态被改掉了")

    def test_reverting_restores_the_authors_verdict_verbatim(self):
        """撤销自己那一笔时**原样**还回去，不替人判成「通过」——那还会顺手消费
        这一阶段的批注。待审的镜换回去就该还是待审。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._shots(first["voice_type"], state="wfa"))
        switch()
        self.assertEqual(self.ws.store.load_chapter("vb", "ch01")["shots"][0]
                         ["review"]["audio"]["state"], "retake")
        voicebank.use_cast(self.s, self.store, first["cast"])
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertEqual(shot["review"]["audio"]["state"], "wfa")
        self.assertNotIn("voice_stale_prev", shot)

    def test_a_human_retake_is_never_undone(self):
        """人自己打回的重做，机器不许撤——判据是本模块写的那句 note。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._shots(first["voice_type"]))
        data = self.ws.store.load_chapter("vb", "ch01")
        data["shots"][0]["review"] = {"audio": {"state": "retake", "note": "念快了"}}
        self.ws.store.save_chapter("vb", "ch01", data)
        switch()
        voicebank.use_cast(self.s, self.store, first["cast"])
        got = self.ws.store.load_chapter("vb", "ch01")["shots"][0]["review"]["audio"]
        self.assertEqual((got["state"], got["note"]), ("retake", "念快了"))

    def test_shots_without_audio_are_left_alone(self):
        """没配过音的镜没有「过期」可言，标它只是制造噪音。"""
        _first, switch = self._two_voices()
        self._chapter(shots=[{"id": 1, "speaker": "林深", "narration": "一句"}])
        r = switch()
        self.assertEqual((r["voice_retake"], r["voice_stale"]), (0, 0))

    # —— 片段侧：native 对白镜的人声由模型念出，过期留痕只在片段上 ——
    def _clip_shot(self, voice_type, *, state=None, version=1):
        """一版 native 对白镜片段：envelope 里留着实发过的那条锚定参考音。"""
        s = {"id": 1, "lines": [{"speaker": "林深", "text": "一句"}],
             "clip": "/x/s1.mp4",
             "gen": {"clip": {"version": version, "envelope": {"references": [
                 {"role": "voice_anchor", "id": f"shot:1:voice:{voice_type}",
                  "sha256": "deadbeef"}]}}}}
        if state:
            s["review"] = {"clip": {"state": state}}
        return [s]

    def test_switching_voice_marks_the_clip_that_burned_the_old_one(self):
        """native 对白镜没有 gen.audio——只看配音那条边的话，换音色后成片人声
        原样不变且零提示。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._clip_shot(first["voice_type"]))
        r = switch()
        self.assertEqual((r["clip_retake"], r["clip_stale"]), (1, 0))
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertEqual(shot["review"]["clip"]["state"], "retake")
        self.assertEqual(shot["voice_clip_stale"], [first["voice_type"]])
        self.assertNotIn("note", shot["review"]["clip"],
                         "clip 的意见会被编进下一版视频提示词，不能拿它当归属判据")

    def test_clip_flag_is_cleared_when_the_voice_comes_back(self):
        first, switch = self._two_voices()
        self._chapter(shots=self._clip_shot(first["voice_type"], state="wfa"))
        switch()
        voicebank.use_cast(self.s, self.store, first["cast"])
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertNotIn("voice_clip_stale", shot)
        self.assertEqual(shot["review"]["clip"]["state"], "wfa", "原表态被改掉了")

    def test_approved_clip_is_only_flagged_never_unlocked(self):
        """锁是人给的，机器不越权解锁；片段侧同样只挂标记等人裁决。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._clip_shot(first["voice_type"], state="done"))
        r = switch()
        self.assertEqual((r["clip_retake"], r["clip_stale"]), (0, 1))
        shot = self.ws.store.load_chapter("vb", "ch01")["shots"][0]
        self.assertEqual(shot["review"]["clip"]["state"], "done")
        self.assertEqual(shot["voice_clip_stale"], [first["voice_type"]])
        self.assertNotIn("voice_clip_stale_prev", shot,
                         "锁定支不暂存原表态——存了说明它走进了会改写审阅态的那一支")

    def test_the_clip_flag_reaches_the_page(self):
        """标记算了写了却没有下发出口，它就只是磁盘上的一个键：clip 的 retake
        按设计不带 note，页面上没有第二处能说出「这镜为什么该重烧」。"""
        from kinema.models import ConfigStore
        from kinema.studio import scanner
        first, switch = self._two_voices()
        self._chapter(shots=self._clip_shot(first["voice_type"], state="done"))
        switch()
        d = scanner.chapter_detail(self.ws.root, ConfigStore.load(None), "vb", "ch01")
        self.assertEqual(d["shots"][0]["voice_clip_stale"], [first["voice_type"]])

    def test_rolled_back_clip_is_not_judged(self):
        """`versioning.rollback` 只搬文件不动 gen——快照描述的是最新生成的那一版，
        不是画布上这一版。拿它判过期就是按秒重买一镜。"""
        first, switch = self._two_voices()
        self._chapter(shots=self._clip_shot(first["voice_type"], version=2))
        r = switch()
        self.assertEqual((r["clip_retake"], r["clip_stale"]), (0, 0))

    def test_other_speakers_losing_their_anchor_slot_is_not_a_voice_change(self):
        """本镜多了个说话人、把参考位挤掉，不等于烧进去的那把声音换了。
        反向判定会在自动选角补位的同一次运行里给正常镜打上按秒重买。"""
        first, _switch = self._two_voices()
        shots = self._clip_shot(first["voice_type"])
        shots[0]["lines"].append({"speaker": "路人", "text": "另一句"})
        self._chapter(shots=shots)
        r = voicebank.assign_voice(self.s, self.store, "林深", "热血少年")
        self.assertEqual((r["clip_retake"], r["clip_stale"]), (0, 0))

    def test_adopting_a_stale_batch_is_refused(self):
        """候选块整批覆盖：两次写盘之间别的写者又生成一批，按编号取到的就是
        另一条音频，而档案还会记成新批次。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉沙哑的中年男声", count=2)
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="清亮少年音", count=2)
        with self.assertRaises(KinemaError):
            voicebank.use_custom(self.s, self.store, "林深", 1, expect_batch=1)

    def test_a_new_take_clears_the_flag(self):
        """新音轨落地=旧标记失效。不清的话卡片上那句「音色已更换」会一直挂着，
        而它说的那件事已经不成立了。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.stage_tts)
        self.assertIn('s.pop("voice_stale", None)', src)
        self.assertLess(src.index('s.pop("voice_stale", None)'),
                        src.index('review.mark_generated(s, "audio")'),
                        "清标记要与登记新产物在同一处，隔开就会有一条路忘了清")


class TestCustomDirectTts(unittest.TestCase):
    """定制音色**逐镜直出**（无音频剧本）的一致性组合：声线描述文案 + 参考音同发。

    参考音（档案那条不可变音频）锁音色本身，描述原话钉气质/语速/口癖——只发其一
    都不算固定音色。官方模版实体在同一章里照旧走 speaker 参数 + 裸台词，
    两条路并存互不污染（旁白与角色同权：narrator_voice 指向定制档案时同样成立）。
    """

    def setUp(self):
        self._env = LocalBackendEnv()
        self._env.enable()

    def tearDown(self):
        self._env.restore()

    def _run(self, doc, tmp):
        import contextlib
        import io
        import json
        from unittest import mock as _mock
        from kinema.cli import stage_tts
        from kinema.models import ConfigStore, ModelRouter
        from kinema.project import Project
        from kinema.providers.tts.mock import MockTTSProvider
        cf = Path(tmp) / "ch01.json"
        cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        project = Project.load(cf)
        store = ConfigStore.load(None)
        calls = []
        real = MockTTSProvider.synthesize

        def spy(self, text, out_path, **kw):
            calls.append({"text": text, **kw})
            return real(self, text, out_path, **kw)

        with _mock.patch.object(MockTTSProvider, "synthesize", spy), \
                contextlib.redirect_stdout(io.StringIO()):
            stage_tts(project, store, ModelRouter(store, force_mock=True))
        return calls

    def test_custom_line_sends_desc_plus_ref_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "vc_0001.mp3"
            clip.write_bytes(b"ID3mock")
            doc = {"id": "ch01", "motion": "kenburns", "aspect": "16:9",
                   "narrator_voice": "custom:vc_0001",
                   "voices": {"林深": "热血少年"},
                   "voice_bank": {"seq": 1, "casts": [
                       {"id": "vc_0001", "owner": "旁白", "mode": "custom",
                        "voice_type": "custom:vc_0001",
                        "prompt": "中年男性，嗓音低沉，略带沙哑，语速偏慢",
                        "clip": str(clip)}]},
                   "shots": [
                       {"id": 1, "dur": 3.0, "narration": "宇宙为什么如此安静。",
                        "emotion": "冷峻"},
                       {"id": 2, "dur": 3.0, "speaker": "林深",
                        "narration": "因为没有人敢回答。"}]}
            calls = self._run(doc, tmp)
            self.assertEqual(len(calls), 2)
            custom = next(c for c in calls if c.get("ref_audio"))
            # 参考音 = 档案那条不可变音频（固定音色的第一道锚）
            self.assertEqual(custom["ref_audio"], str(clip))
            # 声线描述文案随剧本体正文发出（第二道锚），台词恒为引号体
            self.assertIn("中年男性，嗓音低沉", custom["text"])
            self.assertIn(f"说道：“宇宙为什么如此安静。{voicebank.TAIL_GUARD}”", custom["text"])
            self.assertIn("带着冷峻的情绪", custom["text"])
            # 模版实体照旧：speaker 参数 + 裸台词，绝不掺定制那套剧本体
            preset = next(c for c in calls if not c.get("ref_audio"))
            self.assertEqual(preset["text"], "因为没有人敢回答。")
            self.assertTrue(preset.get("voice"), "模版实体必须带 speaker 参数")
            self.assertNotIn("说道", preset["text"])

    def test_custom_cast_without_clip_refuses_loud(self):
        """档案缺参考音=已残——绝不静默退回官方 speaker（那等于换了一把声音）。"""
        with tempfile.TemporaryDirectory() as tmp:
            doc = {"id": "ch01", "motion": "kenburns", "aspect": "16:9",
                   "narrator_voice": "custom:vc_0001",
                   "voice_bank": {"seq": 1, "casts": [
                       {"id": "vc_0001", "owner": "旁白", "mode": "custom",
                        "voice_type": "custom:vc_0001", "prompt": "低沉"}]},
                   "shots": [{"id": 1, "dur": 3.0, "narration": "一句。"}]}
            with self.assertRaises(KinemaError):
                self._run(doc, tmp)


class TestCustomVoiceRouting(_BankCase):
    """定制与模版的分流：认错一边就会让官方音色被当成定制去找参考音，整章配音报错。"""

    def test_clip_only_for_registered_custom_types(self):
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=1)
        r = voicebank.use_custom(self.s, self.store, "林深", 1)
        doc = self._reload().data
        self.assertEqual(voicebank.clip_for(doc, r["voice_type"]), r["clip"])
        self.assertIsNone(voicebank.clip_for(doc, "zh_male_shenyeboke_uranus_bigtts"))
        self.assertIsNone(voicebank.clip_for(doc, None))
        self.assertIsNone(voicebank.clip_for(doc, "custom:vc_9999"))

    def test_preset_cast_has_no_clip_route(self):
        """模版音色照旧走官方 speaker 参数——给它派参考音等于换了一条合成路。"""
        voicebank.audition(self.store, self.router, self.s, "林深")
        r = voicebank.use_audition(self.s, self.store, "林深", 1)
        self.assertIsNone(voicebank.clip_for(self._reload().data, r["voice_type"]))

    def test_voice_desc_follows_the_cast_in_use(self):
        """音频剧本起草取的必须是**在用**那把的原话。按 owner 扫全表会取到一把
        已经不用的嗓子，底稿从此按错误声线写。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="沙哑的老人", count=1)
        old = voicebank.use_custom(self.s, self.store, "林深", 1)
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="清亮的少年", count=1)
        voicebank.use_custom(self.s, self.store, "林深", 1)
        doc = self._reload().data
        self.assertEqual(voicebank.voice_desc(doc, "林深"), "清亮的少年")
        voicebank.use_cast(self.s, self.store, old["cast"])
        self.assertEqual(voicebank.voice_desc(self._reload().data, "林深"), "沙哑的老人")

    def test_imported_cast_gets_a_local_identity(self):
        """跨项目引入：档案号是项目内序列，音频也要另存一份——共用一条路径的话，
        源项目删档会把目标项目的参考音一并带走。"""
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=1)
        src = voicebank.use_custom(self.s, self.store, "林深", 1)
        dst = self.ws.create_project("另一部", pid="vb2", profile="anime")
        dst.add_character("林深")
        got = voicebank.import_cast(dst, voicebank.find_cast(self._reload().data, src["cast"]))
        self.assertNotEqual(got["clip"], src["clip"])
        self.assertEqual(got["voice_type"], f"{voicebank.CUSTOM_PREFIX}{got['id']}")
        self.assertTrue(Path(got["clip"]).is_file())


class TestAssetsImport(_BankCase):
    def test_force_reimport_does_not_duplicate_the_character(self):
        """--force 的同名剔除必须排在 import_cast 之后：commit() 进锁即从磁盘
        重载整份文档，先在内存里做的剔除会被重载丢弃，append 后新旧两条同名并存
        ——此后按名查找全部命中旧条目，引入形同未生效。"""
        from types import SimpleNamespace

        from kinema.cli import cmd_assets_import
        voicebank.custom_audition(self.store, self.router, self.s, "林深",
                                  prompt="低沉", count=1)
        voicebank.use_custom(self.s, self.store, "林深", 1)
        dst = self.ws.create_project("目标", pid="vb2", profile="anime")
        dst.add_character("林深")
        dst.save()
        cmd_assets_import(SimpleNamespace(
            workspace=str(self.tmp / "project"), src="vb", project="vb2",
            kind="character", name="林深", force=True))
        got = self.ws.get_project("vb2")
        names = [c.get("name") for c in got.characters]
        self.assertEqual(names.count("林深"), 1, "同名角色不许翻倍")
        ent = next(c for c in got.characters if c["name"] == "林深")
        self.assertTrue(str(ent.get("voice") or "").startswith("custom:"),
                        "音色引用要落在随行引入的那条档案上")


class TestCandidateRecommendation(unittest.TestCase):
    """模版候选池：角色扮演 ICL（有感情·适配漫剧）优先·避机械播音腔·去重·同性别·**随机**。"""

    def setUp(self):
        self.st = _Store(VOICES)

    def test_current_first_gendered_no_broadcast(self):
        c = voicebank.default_candidates(self.st, {"voice": "热血少年"}, count=5)
        self.assertEqual(c[0], "热血少年")
        self.assertNotIn("儒雅旁白", c)
        self.assertNotIn("擎苍", c)
        self.assertNotIn("Tim", c)
        self.assertTrue(all("male" in self.st.voices[x] and "female" not in self.st.voices[x]
                            for x in c))

    def test_random_varies_across_calls(self):
        runs = {tuple(voicebank.default_candidates(self.st, {}, count=5)) for _ in range(10)}
        self.assertGreater(len(runs), 1)

    def test_dedup_same_voice_type(self):
        cands = voicebank.default_candidates(self.st, {}, count=12)
        vts = [self.st.voices[c] for c in cands]
        self.assertEqual(len(vts), len(set(vts)))

    def test_gender_match(self):
        f = voicebank.default_candidates(self.st, {"voice": "元气甜妹"}, count=4)
        self.assertTrue(all("female" in self.st.voices[c] for c in f))

    def test_unvoiced_character_gender_comes_from_sheet_text(self):
        """性别闸生命线：没选过音色的角色按设定文本推断性别。只看现有音色的话
        这道闸在「还没选音色」的角色上恒失效，男角色会刷出女配音。"""
        m = voicebank.default_candidates(
            self.st, {"appearance": "35岁男性，身形厚实，肩背宽阔"}, count=6)
        self.assertTrue(m and all("female" not in self.st.voices[x] for x in m))
        f = voicebank.default_candidates(
            self.st, {"appearance": "22岁女性，圆脸带点婴儿肥"}, count=6)
        self.assertTrue(f and all("female" in self.st.voices[x] for x in f))
        f2 = voicebank.default_candidates(
            self.st, {"appearance": "19岁，个子很小，马尾",
                      "role": "散人军师·随行；她永远进不了契"}, count=6)
        self.assertTrue(f2 and all("female" in self.st.voices[x] for x in f2))

    def test_gender_chain_priority_and_conservative_fallback(self):
        """判定链次序：显式 gender > 现有音色 > 文本；逐字段判不混判；
        非人类灵体（无任何标记）不过滤——两性都可试本就合理。"""
        self.assertEqual(voicebank.character_gender(
            self.st, {"gender": "female", "appearance": "24岁男性"}), "female")
        self.assertEqual(voicebank.character_gender(self.st, {"gender": "男"}), "male")
        self.assertEqual(voicebank.character_gender(
            self.st, {"voice": "元气甜妹", "appearance": "24岁男性"}), "female")
        self.assertEqual(voicebank.character_gender(
            self.st, {"appearance": "40岁男性，寸头", "role": "父亲；她的女儿失踪了"}), "male")
        self.assertIsNone(voicebank.character_gender(
            self.st, {"appearance": "既有男性面孔也有女性面孔的双面灵体"}))
        self.assertIsNone(voicebank.character_gender(
            self.st, {"appearance": "一只巴掌大小的Q版吉祥物小生灵，圆滚滚三头身"}))
        spirit = voicebank.default_candidates(
            self.st, {"appearance": "Q版方脑袋机器人形灵体"}, count=10)
        self.assertTrue(any("female" in self.st.voices[x] for x in spirit)
                        and any("female" not in self.st.voices[x] for x in spirit),
                        "无性别灵体两性都该可试")

    def test_narrator_pool_no_broadcast(self):
        self.assertNotIn("儒雅旁白", voicebank.NARRATOR_POOL)
        self.assertIn("磁性男嗓", voicebank.NARRATOR_POOL)


if __name__ == "__main__":
    unittest.main()


class TestAssignVoice(_BankCase):
    """指派音色的唯一出口：写实体槽位 + 同步已建章节 + 落地可试听的锚定音。

    三件事必须同一步完成。只写 `characters[].voice` 时，已建章节持有的仍是建章时
    的拷贝，可试听样本要到生视频才现合成——选角状态在项目页与章节页成为两份事实。
    """

    def test_assign_writes_slot_and_lands_the_sample(self):
        r = voicebank.assign_voice(self.s, self.store, "林深", "热血少年",
                                   router=self.router)
        s2 = self.ws.get_project("vb")
        self.assertEqual(voicebank.owner_ref(s2.data, "林深"), "热血少年")
        self.assertEqual(r["voice_type"], VOICES["热血少年"])
        self.assertTrue(Path(r["anchor"]).is_file(), "锚定音没有落盘，页面无从试听")

    def test_assign_syncs_existing_chapters(self):
        self.s.create_chapter("第一章")
        voicebank.assign_voice(self.s, self.store, "林深", "热血少年",
                               router=self.router)
        data = self.ws.store.load_chapter("vb", "ch01")
        self.assertEqual((data.get("voices") or {}).get("林深"), "热血少年")

    def test_bank_view_exposes_the_sample_without_a_cast(self):
        """直接指派不建档案（档案的语义是「试音选出来的那条音频」），可试听样本
        走锚定音缓存；不下发它，页面对这类实体只有别名而无可播放的音频。"""
        voicebank.assign_voice(self.s, self.store, "林深", "热血少年",
                               router=self.router)
        v = voicebank.bank_views(self.ws.get_project("vb"), self.store)["林深"]
        self.assertEqual(v["casts"], [])
        self.assertTrue(v["anchor"] and Path(v["anchor"]).is_file())

    def test_empty_alias_is_rejected(self):
        with self.assertRaises(KinemaError):
            voicebank.assign_voice(self.s, self.store, "林深", "  ")


class TestCastCustom(_BankCase):
    """缺省选角路径：一段声线描述 → 一条演绎 → 立档启用，角色与旁白同一条实现。"""

    def test_generates_one_and_activates_it(self):
        r = voicebank.cast_custom(self.s, self.store, self.router, "林深",
                                  "二十岁男性，清亮，语速快")
        s = self._reload()
        ref = voicebank.owner_ref(s.data, "林深")
        self.assertTrue(ref.startswith("custom:"))
        self.assertEqual(r["voice"], ref)
        cast = voicebank.cast_for_ref(s.data, "林深", ref)
        self.assertEqual(cast["mode"], "custom")
        self.assertEqual(cast["prompt"], "二十岁男性，清亮，语速快")
        ent = next(c for c in s.characters if c["name"] == "林深")
        self.assertEqual(ent["voice_prompt"], "二十岁男性，清亮，语速快")
        view = voicebank.bank_view(s, "林深")
        self.assertEqual(view["voice_prompt"], "二十岁男性，清亮，语速快")
        self.assertEqual(len(view["custom_audition"]["entries"]), 1)
        self.assertTrue(Path(cast["clip"]).is_file())

    def test_narrator_lands_in_the_narrator_slot(self):
        voicebank.cast_custom(self.s, self.store, self.router, voicebank.NARRATOR,
                              "四十岁男性，低沉，语速慢")
        s = self._reload()
        self.assertTrue(s.data["narrator"]["voice"].startswith("custom:"))
        self.assertEqual(s.data["narrator"]["voice_prompt"], "四十岁男性，低沉，语速慢")
        self.assertFalse(voicebank.uncast_owners(s, [voicebank.NARRATOR]))

    def test_adopts_the_nth_of_a_batch(self):
        voicebank.cast_custom(self.s, self.store, self.router, "林深", "少年", count=3, no=2)
        s = self._reload()
        cast = voicebank.cast_for_ref(s.data, "林深", voicebank.owner_ref(s.data, "林深"))
        self.assertEqual(cast["source"]["no"], 2)
        self.assertEqual(len(voicebank.bank_view(s, "林深")["custom_audition"]["entries"]), 3)

    def test_recast_replaces_the_active_cast(self):
        a = voicebank.cast_custom(self.s, self.store, self.router, "林深", "少年")["voice"]
        b = voicebank.cast_custom(self._reload(), self.store, self.router, "林深", "青年")["voice"]
        s = self._reload()
        self.assertNotEqual(a, b)
        self.assertEqual(voicebank.owner_ref(s.data, "林深"), b)
        self.assertEqual({c["voice_type"] for c in voicebank.casts_of(s.data)}, {a, b})

    def test_blank_prompt_is_rejected(self):
        with self.assertRaises(KinemaError):
            voicebank.cast_custom(self.s, self.store, self.router, "林深", "  ")

    def test_uncast_owners_judges_by_voice_ref(self):
        owners = ["林深", voicebank.NARRATOR, "路人甲"]
        self.assertEqual(voicebank.uncast_owners(self.s, owners), owners)
        voicebank.assign_voice(self.s, self.store, "林深", "冷酷哥哥")
        self.assertEqual(voicebank.uncast_owners(self._reload(), owners),
                         [voicebank.NARRATOR, "路人甲"])
        voicebank.cast_custom(self._reload(), self.store, self.router,
                              voicebank.NARRATOR, "旁白")
        self.assertEqual(voicebank.uncast_owners(self._reload(), owners), ["路人甲"])


class TestCustomCount(unittest.TestCase):
    """`voice custom` 的定制条数：`--adopt N` 不试听直接立档第 N 条，多生成的
    候选没有消费者——未显式 `--count` 时只生成 N 条。"""

    def _args(self, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(**{"count": None, "adopt": None, **kw})

    def test_adopt_without_count_generates_only_n(self):
        from kinema.cli import _custom_count
        self.assertEqual(_custom_count(self._args(adopt=1)), 1)
        self.assertEqual(_custom_count(self._args(adopt=2)), 2)

    def test_explicit_count_wins_and_default_is_batch(self):
        from kinema.cli import _custom_count
        self.assertEqual(_custom_count(self._args(adopt=1, count=3)), 3)
        self.assertEqual(_custom_count(self._args()), voicebank.CUSTOM_COUNT)


class TestCastGate(_BankCase):
    """真发前的选角闸：开口的说话人都要有音色引用，缺的逐个点名并给出修法。"""

    def _project(self, shots):
        from kinema.project import Project
        self._chapter(shots=shots)
        return Project.load(self.tmp / "project" / "vb" / "chapters" / "ch01.json")

    def test_names_every_uncast_speaker_with_a_fix(self):
        from kinema import cli
        p = self._project([{"id": 1, "dur": 4, "narration": "开场。"},
                           {"id": 2, "dur": 4, "lines": [
                               {"speaker": "林深", "text": "走。"},
                               {"speaker": "路人甲", "text": "站住。"}]}])
        with self.assertRaises(KinemaError) as cm:
            cli._cast_gate(p, self.router)
        msg = str(cm.exception)
        for token in ("旁白", "林深", "路人甲", "voice custom vb --narrator",
                      "character set vb --name 林深 --voice-prompt",
                      "character add vb --name 路人甲 --voice-prompt"):
            self.assertIn(token, msg)

    def test_passes_once_every_speaker_has_a_voice(self):
        from kinema import cli
        voicebank.cast_custom(self.s, self.store, self.router, "林深", "青年")
        voicebank.cast_custom(self.s, self.store, self.router, voicebank.NARRATOR, "旁白")
        p = self._project([{"id": 1, "dur": 4, "narration": "开场。"},
                           {"id": 2, "dur": 4, "speaker": "林深", "narration": "走。"}])
        cli._cast_gate(p, self.router)

    def test_skip_and_mock_bypass(self):
        from kinema import cli
        p = self._project([{"id": 1, "dur": 4, "narration": "开场。"}])
        cli._cast_gate(p, self.router, skip=True)
        cli._cast_gate(p, type("R", (), {"force_mock": True})())


class TestRecordedVoiceTypes(unittest.TestCase):
    """盘上音色留痕只有一种读法：多句对白镜的镜级 voice_type 是缺省音（旁白锁），
    与逐句留痕并集会让任何一次选角动作都把这类镜判成「音色已更换」重合成。"""

    def test_cast_wins_over_shot_level_default(self):
        from kinema import voicebank
        shot = {"id": 1, "gen": {"audio": {"voice_type": "旁白默认音",
                                            "cast": [{"voice_type": "A"}, {"voice_type": "B"}]}}}
        self.assertEqual(voicebank.recorded_voice_types(shot), ["A", "B"])
        self.assertEqual(voicebank._recorded_types(shot), {"A", "B"})
        single = {"id": 2, "gen": {"audio": {"voice_type": "旁白默认音"}}}
        self.assertEqual(voicebank.recorded_voice_types(single), ["旁白默认音"])
        self.assertEqual(voicebank.recorded_voice_types({"id": 3}), [])

    def test_unrelated_casting_does_not_retake_a_dialogue_shot(self):
        from kinema import review, voicebank
        store = _Store({"甲声": "A", "乙声": "B", "旁白声": "N"})
        shot = {"id": 1, "lines": [{"speaker": "甲", "text": "走。"}, {"speaker": "乙", "text": "好。"}],
                "review": {"audio": {"state": "wfa"}},
                "gen": {"audio": {"voice_type": "N", "cast": [{"voice_type": "A"}, {"voice_type": "B"}]}}}
        data = {"voices": {"甲": "甲声", "乙": "乙声"}, "narrator_voice": "旁白声",
                "characters": [], "shots": [shot]}
        self.assertIsNone(voicebank._propagate_audio(shot, store, data, review))
        self.assertEqual(review.get_state(shot, "audio"), "wfa")
        data["voices"]["乙"] = "旁白声"                      # 真换了乙的音色才重做
        self.assertEqual(voicebank._propagate_audio(shot, store, data, review), "retake")


class _WavProv(_Prov):
    """把「合成」落成 2 秒真实音频：语速要按音频时长实测。"""

    def synthesize(self, text, out, **kw):
        self.calls += 1
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-f", "wav", str(out)],
                       check=True)
        return {"cost": 0.0}


class TestCharacterRemoval(_BankCase):
    def test_character_rm_clears_chapter_assignment_so_the_cast_can_be_deleted(self):
        """角色移除后章节 voices{} 里的指派随之摘除；否则引用账把它算作一处指派，
        这把声音在实体已不存在后仍不可删。删档同步也不得写回一个空键。"""
        self._chapter()
        r = voicebank.cast_custom(self.s, self.store, self.router, "林深",
                                  "二十岁男性，清亮，语速快")
        self.assertEqual(self.ws.store.load_chapter("vb", "ch01")["voices"]["林深"], r["voice"])
        self._reload().remove_character("林深")
        self.assertNotIn("林深", self.ws.store.load_chapter("vb", "ch01").get("voices") or {})
        voicebank.delete_cast(self._reload(), r["cast"])
        self.assertNotIn("林深", self.ws.store.load_chapter("vb", "ch01").get("voices") or {})
        self.assertEqual(voicebank.casts_of(self._reload().data), [])


class TestSpeechRate(_BankCase):
    """档案记实测语速：lint 据它在花钱前预估台词能否落进画面窗口。"""

    def test_cast_records_the_rate_measured_on_the_audition_line(self):
        from kinema.pipeline.asr import speech_chars
        r = voicebank.cast_custom(self.s, self.store, _Router(_WavProv()), "林深",
                                  "二十岁男性，清亮，语速快")
        cast = voicebank.find_cast(self._reload().data, r["cast"])
        line = voicebank.AUDITION_TEXT.replace("{name}", "林深")
        self.assertEqual(cast["speech_rate"], round(speech_chars(line) / 2, 2))

    def test_unprobeable_audio_leaves_no_rate(self):
        r = voicebank.cast_custom(self.s, self.store, self.router, "林深",
                                  "二十岁男性，清亮，语速快")
        self.assertNotIn("speech_rate", voicebank.find_cast(self._reload().data, r["cast"]))

    def test_import_keeps_the_source_rate(self):
        r = voicebank.cast_custom(self.s, self.store, _Router(_WavProv()), "林深",
                                  "二十岁男性，清亮，语速快")
        src = voicebank.find_cast(self._reload().data, r["cast"])
        new = voicebank.import_cast(self._reload(), src, owner="旁白")
        self.assertEqual(new["speech_rate"], src["speech_rate"])
