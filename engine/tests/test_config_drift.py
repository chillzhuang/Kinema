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

"""配置漂移守卫：EMBEDDED_DEFAULTS / EMBEDDED_VOICES 是
models.yaml / voices.yaml 的零配置兜底精简版——两边一旦静默分叉，同一 profile 会
因"装没装 PyYAML"表现出两种行为。本测试把漂移变成红灯：内嵌层的每一项都必须
能在 yaml 真源里找到且口径一致（yaml 可以比内嵌多，内嵌不得与 yaml 矛盾）。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from kinema.config_overlay import CAPABILITIES
from kinema.models import EMBEDDED_DEFAULTS, EMBEDDED_VOICES
from kinema.audio_registry import EMBEDDED_AUDIO

_CONFIG = Path(__file__).resolve().parent.parent.parent / "config"


def _load_yaml(name):
    try:
        import yaml
    except ImportError:
        return None
    f = _CONFIG / name
    if not f.is_file():
        return None
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


class TestProviderNamingConvention(unittest.TestCase):
    """别名与实现名的两套写法是有分工的，且必须被强制。

    · **别名**（providers 段的键）用连字符：它面向用户——出现在 yaml、
      `--video-provider` 的值、项目文档的 `video_provider` 里，与各家模型 ID 的
      写法一致（doubao-seedance-2-0-mini / image-01 / music-3.0 / MiniMax-H3）。
    · **impl** 用下划线：它必须与 Python 模块名同名（providers/image/nano-banana.py），
      模块名不能带连字符。

    只写在注释里的约定会在下一次加 provider 时漂回去，所以钉成用例。
    """

    ALIAS_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-.][a-z0-9]+)*$")
    IMPL_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

    def _providers(self) -> dict:
        return (_load_yaml("models.yaml").get("providers") or {})

    def test_aliases_use_hyphens_never_underscores(self):
        for alias in self._providers():
            self.assertRegex(alias, self.ALIAS_RE,
                             f"别名 `{alias}` 不合规：面向用户的别名用连字符"
                             "（复合词写 minimax-h3 而不是 minimax_h3）")

    def test_impls_use_underscores_never_hyphens(self):
        from kinema.models import _ADAPTERS
        impls = {impl for _, impl in _ADAPTERS}
        for conn in self._providers().values():
            impl = conn.get("impl")
            if impl:
                impls.add(impl)
        for impl in impls:
            self.assertRegex(impl, self.IMPL_RE,
                             f"impl `{impl}` 不合规：它要与 Python 模块名同名，"
                             "模块名不能带连字符")

    def test_alias_declares_impl_whenever_they_differ(self):
        """`impl` 缺省等于别名自身。别名带连字符而实现带下划线时，不显式声明
        就会在解析时找不到适配器——而那个报错发生在**运行期**，不是加别名的时候。"""
        from kinema.models import _ADAPTERS
        for alias, conn in self._providers().items():
            impl = conn.get("impl", alias)
            kind = conn.get("kind")
            if conn.get("status", "ready") != "ready":
                continue
            self.assertIn((kind, impl), _ADAPTERS,
                          f"别名 `{alias}` 的 impl=`{impl}` 没有对应适配器"
                          "（别名与实现名不同时必须显式写 impl:）")

    def test_renamed_aliases_keep_a_compat_entry(self):
        """改过名的别名必须留兼容位：存量 project.json 里可能还点着旧名，
        直接抛「未知 provider」会让老项目在某次升级后突然跑不了。"""
        from kinema.models import LEGACY_ALIASES
        provs = self._providers()
        for old, new in LEGACY_ALIASES.items():
            self.assertNotIn(old, provs, f"`{old}` 既是兼容位又是真别名，语义冲突")
            self.assertIn(new, provs, f"兼容位 `{old}` 指向了一个不存在的别名 `{new}`")


class TestEmbeddedDefaultsDrift(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.yaml_cfg = _load_yaml("models.yaml")
        if cls.yaml_cfg is None:
            raise unittest.SkipTest("缺 PyYAML 或 config/models.yaml，跳过漂移守卫")

    def test_embedded_profiles_exist_in_yaml(self):
        yaml_profiles = self.yaml_cfg.get("profiles") or {}
        for name in (EMBEDDED_DEFAULTS.get("profiles") or {}):
            self.assertIn(name, yaml_profiles,
                          f"内嵌 profile「{name}」在 models.yaml 里不存在（内嵌层漂移）")

    @staticmethod
    def _effective(cfg: dict, profile: dict, cap: str):
        """按 router 解析链算有效 provider：profile 偏离项 > defaults.providers > video 兜底。"""
        e = (profile.get(cap) or {}).get("provider")
        d = ((cfg.get("defaults") or {}).get("providers") or {}).get(cap)
        return e or d or ("seedance" if cap == "video" else None)

    def test_defaults_providers_match(self):
        # 全局总入口（defaults.providers）两边必须一致——这是"换厂商只改一行"的锚点
        y = (self.yaml_cfg.get("defaults") or {}).get("providers") or {}
        e = (EMBEDDED_DEFAULTS.get("defaults") or {}).get("providers") or {}
        self.assertTrue(y, "models.yaml 缺 defaults.providers 全局默认入口")
        self.assertEqual(e, y, "defaults.providers 内嵌与 yaml 分叉")

    def test_provider_bindings_match(self):
        # 同名 profile 的**有效** provider（含默认链）必须一致——
        # 否则同一 profile 会因 PyYAML 有无表现两种行为
        yaml_profiles = self.yaml_cfg.get("profiles") or {}
        for name, emb in (EMBEDDED_DEFAULTS.get("profiles") or {}).items():
            ycfg = yaml_profiles.get(name) or {}
            for cap in CAPABILITIES:
                e = self._effective(EMBEDDED_DEFAULTS, emb, cap)
                y = self._effective(self.yaml_cfg, ycfg, cap)
                if e and y:
                    self.assertEqual(
                        e, y, f"profile「{name}」的 {cap} 有效 provider 分叉：内嵌={e} / yaml={y}")

    def test_every_profile_has_label(self):
        # 每个 profile 必须有中文 label（models.yaml 单一真源，
        # Studio 经 /api/overview 下发）——漏写是红灯，而非静默显示英文原名
        for name, p in (self.yaml_cfg.get("profiles") or {}).items():
            if not isinstance(p, dict):
                continue
            self.assertTrue((p.get("label") or "").strip(),
                            f"profile「{name}」缺 label（Studio 中文名，加画风必填）")

    def test_every_style_prefix_has_english_twin(self):
        # 双语前缀守卫：有中文画风前缀的 profile 必须配 style_prefix_en——
        # 否则切到英文优先模型（prompt_lang: en）时中文前缀混入英文提示词，画风打折
        for name, p in (self.yaml_cfg.get("profiles") or {}).items():
            if not isinstance(p, dict):
                continue
            img = p.get("image") or {}
            if (img.get("style_prefix") or "").strip():
                self.assertTrue(
                    (img.get("style_prefix_en") or "").strip(),
                    f"profile「{name}」有 style_prefix 但缺 style_prefix_en（新画风必须双语）")

    # 图像防字地板 opt-out 的允许清单（新增一档必须在此登记并写明理由）：
    # 只有「画面里本来就该有字」的画风才关地板；气泡/对话框/榜单的字全是合成段
    # ASS 后置烧录，**恰恰要求图本体干净**，是防字地板的受益方而非 opt-out。
    _TEXT_FLOOR_OPTOUT = {"game_sim", "explainer"}

    def test_image_text_floor_optout_is_a_short_registered_list(self):
        off = {name for name, p in (self.yaml_cfg.get("profiles") or {}).items()
               if isinstance(p, dict)
               and (p.get("image") or {}).get("image_text_floor") is False}
        self.assertEqual(
            off, self._TEXT_FLOOR_OPTOUT,
            "image.image_text_floor: false 的画风清单变了——关地板 = 允许模型往分镜图里"
            "画字（合成段还要再烧一层字幕，必然打架）。确要新增请在本测试登记并说明理由")

    def test_image_text_floor_embedded_matches_yaml(self):
        # 内嵌层漏改 → 同一画风会因"装没装 PyYAML"一边画面长字、一边不长
        yaml_profiles = self.yaml_cfg.get("profiles") or {}
        for name, emb in (EMBEDDED_DEFAULTS.get("profiles") or {}).items():
            y = ((yaml_profiles.get(name) or {}).get("image") or {}).get("image_text_floor", True)
            e = (emb.get("image") or {}).get("image_text_floor", True)
            self.assertEqual(e, y,
                             f"profile「{name}」的 image_text_floor 分叉：内嵌={e} / yaml={y}")

    # 照片级人脸档的允许清单（新增一档必须在此登记并说明媒介锚点依据）：
    # identity_sheet: true = 角色身份图走纯文生图吃视频侧受信豁免，代价是失去
    # 蓝图与 moodboard。只有画风前缀带照片级媒介锚点（会撞人脸分类器）的档才配——
    # 给非写实档打开只会白丢版式锚。anime3d 是写实向建模但整体仍是风格化 3D 国漫，
    # 刻意不入册；确认撞闸后再加。
    _IDENTITY_PROFILES = {"photoreal3d", "virtual_production", "cg_noir",
                          "anime_ldr", "cyberpunk"}

    def test_identity_sheet_is_a_short_registered_list(self):
        on = {name for name, p in (self.yaml_cfg.get("profiles") or {}).items()
              if isinstance(p, dict)
              and (p.get("image") or {}).get("identity_sheet") is True}
        self.assertEqual(
            on, self._IDENTITY_PROFILES,
            "image.identity_sheet: true 的画风清单变了——开着它角色设定图不垫蓝图与"
            "moodboard。确要增删请在本测试登记并说明媒介锚点依据")

    def test_identity_sheet_embedded_matches_yaml(self):
        # 内嵌层漏改 → 同一画风会因"装没装 PyYAML"一边受信、一边被人脸闸拒
        yaml_profiles = self.yaml_cfg.get("profiles") or {}
        for name, emb in (EMBEDDED_DEFAULTS.get("profiles") or {}).items():
            y = ((yaml_profiles.get(name) or {}).get("image") or {}).get("identity_sheet", False)
            e = (emb.get("image") or {}).get("identity_sheet", False)
            self.assertEqual(e, y,
                             f"profile「{name}」的 identity_sheet 分叉：内嵌={e} / yaml={y}")

    def test_motion_alias_single_source(self):
        """motion 归一只许 `project.normalize_motion` 一份——各处自抄必然漂移。"""
        from kinema.project import normalize_motion
        self.assertEqual(normalize_motion("a"), "kenburns")
        self.assertEqual(normalize_motion("b"), "native")
        self.assertEqual(normalize_motion("c"), "dubbed")
        self.assertEqual(normalize_motion("dubbed"), "dubbed")
        root = Path(__file__).resolve().parents[1] / "kinema"
        offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                     if "__pycache__" not in p.parts and p.name != "project.py"
                     and '"a": "kenburns"' in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [], "motion 归一表不许再抄第二份")

    def test_skill_docs_effect_mentions_are_registered(self):
        """skills 文档以「特效（…）」括注点名的特效名必须在注册表内——skills 是
        指挥层可执行指令，点名注册表外的名字会被 agent 原样写进章节 effects，
        合成端当场拒绝；文档必须与注册表同步汰换。（只查括注形式：
        提示词语料里的 dust/fire 是正当英文词，不能全文扫。）"""
        import re
        from kinema import effects as fx
        root = Path(__file__).resolve().parents[2] / ".claude" / "skills"
        bad = []
        for md in sorted(root.rglob("*.md")):
            txt = md.read_text(encoding="utf-8")
            for m in re.finditer(r"特效（([^）]*)）", txt):
                # 带点号的是配置路径（如 profile.effects）不是特效名，跳过
                for tok in re.findall(r"(?<![.\w])[a-z][a-z_]+(?![.\w])", m.group(1)):
                    if tok not in fx.EFFECTS:
                        bad.append(f"{md.relative_to(root)}: {tok}")
        self.assertEqual(bad, [], "skills 文档点名了注册表外的特效名：" + "；".join(bad))

    # 画风前缀允许保留的空词：**每条必须是行业专名**，且必须写下理由。
    # 短登记表纪律照 `_TEXT_FLOOR_OPTOUT`——本表必须保持短小，判据在下方断言。
    _STYLE_PREFIX_SLOP_OK = {
        ("anime_ink", "生动"): "「气韵生动」是谢赫六法第一法，水墨画论的固定术语，"
                               "不是评价词——拆开写反而失去这一档的美学坐标",
    }

    def test_style_prefix_carries_no_slop_terms(self):
        """**引擎自己注入的画风前缀，不许含引擎自己判为空词的那些词。**

        这是一处正在生效的双标：`variation.SLOP_TERMS` 把「精致/华丽/张力/唯美/生动/
        梦幻/治愈/极致/诗意/史诗感/电影感」判为 warn，流程真源把它写成铁律并宣告
        「单一真源在引擎」；与此同时引擎在 15/47 档上把**同样这批词**无条件拼到
        每一张分镜图/设定图/封面的提示词最前面（yaml 19 处 + 内嵌表 10 处），
        与上述铁律直接冲突。

        判例已在：`test_prompts.TestMicroMotionTail` 对引擎自注的微动句立的就是同一条
        （「对 SLOP_TERMS 零命中」），本条只是把它铺到画风前缀这一面。

        **两份真源都要扫**：`config/models.yaml` 与 `models.EMBEDDED_DEFAULTS`——
        后者是缺 PyYAML 时真正生效的那一份，只守 yaml 等于漏掉一半。
        """
        from kinema.models import EMBEDDED_DEFAULTS
        from kinema.pipeline.variation import SLOP_TERMS
        sources = [("EMBEDDED_DEFAULTS", EMBEDDED_DEFAULTS)]
        if self.yaml_cfg:
            sources.append(("config/models.yaml", self.yaml_cfg))
        bad = []
        for tag, src in sources:
            for name, p in (src.get("profiles") or {}).items():
                if not isinstance(p, dict):
                    continue
                text = str(((p.get("image") or {}).get("style_prefix")) or "")
                for term in SLOP_TERMS:
                    if term in text and (name, term) not in self._STYLE_PREFIX_SLOP_OK:
                        bad.append(f"{tag} 的 {name}.style_prefix 含空词「{term}」")
        self.assertEqual(bad, [], "引擎一边禁作者写空词、一边自己注入：" + "；".join(bad)
                         + "。改写方向直接用 SLOP_TERMS 每条自带的建议；"
                           "确属行业专名的走 _STYLE_PREFIX_SLOP_OK 并写理由")
        # 白名单必须保持「短登记表」形态，否则一年后它就是全表
        self.assertLessEqual(len(self._STYLE_PREFIX_SLOP_OK), 4, "空词白名单开始膨胀了")
        for key, why in self._STYLE_PREFIX_SLOP_OK.items():
            self.assertGreater(len(why), 20, f"{key} 的例外没写清理由")

    def test_skill_doc_exemplar_prompts_clear_the_engine_floor(self):
        """**范例的字数量级就是产出的字数量级**——skill 文档里作正例给出的
        `video_prompt`/`image_prompt` 不许低于引擎自己的地板。

        **LLM 抄范例不抄清单**——范例若只有二十几字而地板是百字量级，跨项目
        产出的均长会正好复刻范例而不是地板；没有这条守卫，谁写一个 20 字范例，
        仓里没有任何东西会红。

        扫描面**刻意只取 jsonc 代码块里显式的 `"video_prompt": "…"` 键值**，不扫
        行内 ✅ 反引号串：那半边会命中 API 名、字段名这类非提示词内容、扫不到跨行的
        例子，且会把 9 个 kn-* 的公式段范例一并拖红——要覆盖它得先单独扩写那批，
        属另一批次。结构占位（值为「…」）不是范例，跳过。
        """
        from kinema.pipeline import variation as vr
        floors = {"video_prompt": vr.MIN_VIDEO_PROMPT_CHARS,
                  "image_prompt": vr.MIN_IMAGE_PROMPT_CHARS}
        root = Path(__file__).resolve().parents[2] / ".claude" / "skills"
        pat = re.compile(r'"(video_prompt|image_prompt)"\s*:\s*"((?:[^"\\]|\\.)*)"')
        thin, seen = [], 0
        for md in sorted(root.rglob("*.md")):
            for no, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
                for m in pat.finditer(line):
                    field, val = m.group(1), m.group(2).strip()
                    if val in ("…", "...", ""):
                        continue          # 结构占位不是范例
                    seen += 1
                    if len(val) < floors[field]:
                        thin.append(f"{md.relative_to(root)}:{no} {field} "
                                    f"{len(val)} 字 < 地板 {floors[field]}")
        self.assertGreater(seen, 0, "一条 jsonc 范例都没扫到——正则或目录结构变了，"
                                    "守卫已静默失效（比没有更糟）")
        self.assertEqual(thin, [], "skill 文档的正例低于引擎地板，等于在教人写薄提示词："
                                   + "；".join(thin))

    def test_profile_effects_all_registered(self):
        # 特效名守卫（effects 是唯一没进漂移守卫的 profile 字段）：
        # profile.effects 里的名字必须在 effects.EFFECTS 注册表内——否则 build_plan
        # 返回 None 被 compose 静默过滤，特效写错名不生效却无红灯
        from kinema import effects
        valid = set(effects.EFFECTS)
        for name, p in (self.yaml_cfg.get("profiles") or {}).items():
            if not isinstance(p, dict):
                continue
            for e in (p.get("effects") or []):
                self.assertIn(e, valid,
                              f"profile「{name}」用了未注册特效「{e}」（不在 effects.EFFECTS）")

    def test_defaults_and_canvas_match(self):
        y = self.yaml_cfg
        e = EMBEDDED_DEFAULTS
        self.assertEqual((e.get("defaults") or {}), (y.get("defaults") or {}),
                         "defaults（默认 profile/fps/aspect）内嵌与 yaml 分叉")
        self.assertEqual((e.get("canvas") or {}), (y.get("canvas") or {}),
                         "canvas 画布尺寸内嵌与 yaml 分叉")

    def test_every_profile_bound_to_a_skill(self):
        # skill 绑定守卫（建项目落 project.skill 的单一真源）：skills.SKILLS 必须**恰好
        # 覆盖** models.yaml 的每个 profile——多一个（孤儿 profile 名）或少一个（画风
        # 无归属 skill 静默兜底 kinema）都红灯，防 skills.py 与 models.yaml 分叉。
        from kinema import skills
        yaml_profiles = set((self.yaml_cfg.get("profiles") or {}).keys())
        catalog = skills.all_profiles()
        self.assertEqual(catalog, yaml_profiles,
                         f"skills.py 与 models.yaml 画风分叉：多={catalog - yaml_profiles} "
                         f"少={yaml_profiles - catalog}")
        # 目录内部一致：每个画风只归属一个 skill（profiles 无重复）、skill_for_profile 命中
        seen: dict = {}
        for s in skills.SKILLS:
            for p in s["profiles"]:
                self.assertNotIn(p, seen,
                                 f"画风「{p}」重复归属 {seen.get(p)} 与 {s['id']}")
                seen[p] = s["id"]
                self.assertEqual(skills.skill_for_profile(p), s["id"])

    def test_every_skill_ships_a_voiceover_default(self):
        """旁白语态缺省的完备性：SKILLS 每个 id 必有语态、取值合法——缺登记的
        skill 会静默兜底 lead（解说驱动），剧情类少一条登记＝镜镜旁白无人拦。"""
        from kinema import skills
        for s in skills.SKILLS:
            self.assertIn(s["id"], skills._VOICEOVER_DEFAULTS,
                          f"skill「{s['id']}」缺旁白语态缺省")
        self.assertIn("kn-showcase", skills._VOICEOVER_DEFAULTS,
                      "kn-showcase 不在 SKILLS 目录（共享画风特例绑定），须单独登记")
        for k, v in skills._VOICEOVER_DEFAULTS.items():
            self.assertIn(v, skills.VOICEOVER_MODES, f"{k} 的语态「{v}」不合法")
        # 派生链两个锚点：剧情类 sparse / 解说类 lead。none 无 skill 派生来源，
        # 只能由章节顶层显式声明（lint 与 schema 仍消费它，故枚举必须留着）
        self.assertEqual(skills.voiceover_default("anime"), "sparse")
        self.assertEqual(skills.voiceover_default("explainer"), "lead")
        self.assertIn("none", skills.VOICEOVER_MODES)
        # 样本必须能**区分两条路径**：anime 画风派生 sparse、绑定 kn-showcase 的
        # 语态是 lead——(explainer, kn-showcase) 两边都是 lead，删掉 skill 形参照样绿
        self.assertEqual(skills.voiceover_default("anime"), "sparse")
        self.assertEqual(skills.voiceover_default("anime", "kn-showcase"), "lead")


class TestOverviewCatalogs(unittest.TestCase):
    """`/api/overview` 下发的目录必须与各自真源锁步——前端零硬编码，目录少一项
    就是「选择器里没有这个选项」，而这种缺失在前端看起来只像是"没做"。"""

    def _overview_keys(self) -> set[str]:
        """只解析 `scanner.overview()` 的返回字面量，不真的扫工作区（零 IO）。"""
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "kinema" / "studio"
               / "scanner.py").read_text(encoding="utf-8")
        body = src.split("def overview(")[1]
        body = body[body.index("return {"):]
        return set(re.findall(r'^\s{8}"(\w+)":', body, re.M))

    def test_overview_ships_camera_and_director_catalogs(self):
        keys = self._overview_keys()
        for k in ("effects_catalog", "transitions_catalog", "transition_sounds",
                  "camera_catalog", "director_catalog", "canvas"):
            self.assertIn(k, keys, f"/api/overview 少下发 {k}，前端对应选择器会整片空白")

    def test_overview_ships_config_health(self):
        # 缺 PyYAML 回退内置精简配置时画风目录会明显缩水（成组 profile 只剩
        # 少数路线），引擎的 ⚠ 只打 stdout——overview 必须下发 config 健康块，
        # 前端才有材料把回退态亮成告警条而不是让画风"悄悄变少"
        self.assertIn("config", self._overview_keys(),
                      "/api/overview 少下发 config 健康块（source/fallback）")

    def test_new_project_dialog_warns_on_config_fallback(self):
        """project-new.js 必须消费 config.fallback 渲染告警条，且类名有样式——
        DOM 在而 CSS 缺 = 告警在页面上隐形，等于没修。"""
        assets = (Path(__file__).resolve().parents[1] / "kinema"
                  / "studio_app")
        src = (assets / "app" / "project-new.js").read_text(encoding="utf-8")
        self.assertIn("cfg.fallback", src,
                      "新建项目弹层没读 config.fallback——回退态画风缩水无告警")
        self.assertIn("np-cfgwarn", src, "新建项目弹层缺告警条节点 .np-cfgwarn")
        self.assertIn("missing-pyyaml", src,
                      "告警条须区分缺 PyYAML（给修复命令）与无配置文件两种回退")
        css = (assets / "style.css").read_text(encoding="utf-8")
        self.assertIn(".np-cfgwarn", css, "style.css 缺 .np-cfgwarn——告警条视觉上隐形")

    def test_overview_ships_skill_board_covering_full_catalog(self):
        """#/skill 只读大屏的数据契约：overview 必须下发 skill_board，且
        `skills.skill_board()` 恰好覆盖编译 catalog 全量条目（含 route/workflow
        之外的五类 kind）——少一条＝那个 skill 在大屏上凭空消失，而这种缺失在
        前端看起来只像是"没做"。"""
        self.assertIn("skill_board", self._overview_keys(),
                      "/api/overview 少下发 skill_board，#/skill 只剩空态卡")
        from kinema import skills
        from kinema.agent_system import AgentCatalog
        board = skills.skill_board()
        catalog = AgentCatalog.load()
        self.assertTrue(board["catalog_version"])
        self.assertEqual([s["id"] for s in board["skills"]],
                         [s["id"] for s in catalog.all()],
                         "skill_board 与编译 catalog 条目不一致（缺条/乱序）")
        for s in board["skills"]:
            for key in ("cmd", "kind", "status", "description", "source"):
                self.assertTrue(s.get(key), f"skill_board「{s['id']}」缺 {key}")
        kinds = {s["kind"] for s in board["skills"]}
        for kind in ("workflow", "route", "capability", "project", "system"):
            self.assertIn(kind, kinds,
                          f"skill_board 缺 {kind} 类——大屏是全集群视图，不是画风分组目录")

    def test_skill_view_consumes_board_with_kind_fallback(self):
        """skill.js 必须消费 skill_board 且带未知 kind 兜底组，CSS 必须有对应
        样式——前端零硬编码条目页面才随 catalog 自动更新；兜底组缺失时 manifest
        新增 kind 的 skill 会静默不渲染（与 np-cfgwarn 同款「DOM+CSS 都在」判据）。"""
        assets = (Path(__file__).resolve().parents[1] / "kinema" / "studio_app")
        src = (assets / "app" / "skill.js").read_text(encoding="utf-8")
        self.assertIn("skill_board", src, "skill.js 没读 skill_board——大屏没接数据")
        self.assertIn("_other", src, "skill.js 缺未知 kind 兜底组——新 kind 条目会静默消失")
        css = (assets / "style.css").read_text(encoding="utf-8")
        self.assertIn(".sk-card", css, "style.css 缺 .sk-card——skill 卡片视觉上是裸块")

    def test_camera_catalog_keys_are_all_registered(self):
        from kinema.pipeline import camera
        cat = camera.catalog()
        self.assertEqual({c["key"] for c in cat}, set(camera.CAMERA_PRESETS))
        for c in cat:
            self.assertIn(c["rig"], camera.RIGS)
            self.assertIn(c["tier"], camera.TIERS)
            self.assertIn(c["group"], camera.GROUPS)

    def test_director_catalog_keys_are_unique_and_non_empty(self):
        from kinema import previz
        cat = previz.director_catalog()
        for block in ("models", "actions", "props"):
            keys = [x["key"] for x in cat[block]]
            self.assertTrue(keys, f"director_catalog.{block} 不能为空")
            self.assertEqual(len(keys), len(set(keys)), f"{block} 有重复 key")


class TestEmbeddedVoicesDrift(unittest.TestCase):
    def test_embedded_aliases_exist_in_voices_yaml(self):
        data = _load_yaml("voices.yaml")
        if data is None:
            self.skipTest("缺 PyYAML 或 config/voices.yaml")
        presets = {a: (v or {}).get("voice") for a, v in (data.get("presets") or {}).items()}
        for alias, vt in EMBEDDED_VOICES.items():
            self.assertIn(alias, presets,
                          f"内嵌音色别名「{alias}」在 voices.yaml 里不存在——"
                          "voices.yaml 是别名唯一真源，不得倒挂")
            self.assertEqual(presets[alias], vt,
                             f"别名「{alias}」voice_type 分叉：内嵌={vt} / yaml={presets[alias]}")


if __name__ == "__main__":
    unittest.main()


class TestAudioRegistryDrift(unittest.TestCase):
    """config/audio.yaml 与内嵌缺省 EMBEDDED_AUDIO 的一致性守卫（同 models.yaml 哲学）。"""

    @classmethod
    def setUpClass(cls):
        cls.yaml_cfg = _load_yaml("audio.yaml")
        if cls.yaml_cfg is None:
            raise unittest.SkipTest("缺 PyYAML 或 config/audio.yaml，跳过漂移守卫")

    def test_embedded_sfx_keys_and_files_match_yaml(self):
        for cat, items in EMBEDDED_AUDIO["sfx"].items():
            ycat = (self.yaml_cfg.get("sfx") or {}).get(cat) or {}
            for kind, rel in items.items():
                self.assertIn(kind, ycat, f"audio.yaml 缺内嵌键 sfx/{cat}/{kind}")
                yrel = ycat[kind].get("file") if isinstance(ycat[kind], dict) else ycat[kind]
                self.assertEqual(yrel, rel, f"sfx/{cat}/{kind} 文件路径分叉")

    def test_embedded_bgm_moods_match_yaml(self):
        ybgm = self.yaml_cfg.get("bgm") or {}
        for mood, meta in EMBEDDED_AUDIO["bgm"].items():
            self.assertIn(mood, ybgm, f"audio.yaml 缺内嵌情绪 bgm/{mood}")
            self.assertEqual((ybgm[mood] or {}).get("dir"), meta["dir"],
                             f"bgm/{mood} 目录分叉")
            self.assertEqual([str(k) for k in (ybgm[mood] or {}).get("keywords") or []],
                             list(meta["keywords"]), f"bgm/{mood} 关键词分叉")

    def test_yaml_entries_complete(self):
        for mood, v in (self.yaml_cfg.get("bgm") or {}).items():
            for field in ("dir", "desc", "keywords"):
                self.assertTrue((v or {}).get(field), f"bgm/{mood} 缺 {field}")
        for cat, items in (self.yaml_cfg.get("sfx") or {}).items():
            for kind, v in items.items():
                self.assertIsInstance(v, dict, f"sfx/{cat}/{kind} 应为映射（file/desc/license）")
                for field in ("file", "desc", "license"):
                    self.assertTrue(v.get(field), f"sfx/{cat}/{kind} 缺 {field}")
