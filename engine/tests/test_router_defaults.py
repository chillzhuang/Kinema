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

"""模型路由"全局统一配置"回归守护（换厂商自动适配的三根支柱）：
① defaults.providers 能力级默认别名（总入口，换厂商改一行）；
② profile 偏离项优先于默认（画风档在能力块里写 provider 的例外仍生效）；
③ 工厂 impl 别名分发（同厂新模型 = 加别名指 impl，零代码）。"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from kinema.errors import ConfigError, ProviderError
from kinema.models import ConfigStore, ModelRouter, image_route


def _store(profiles=None, defaults_providers=None, providers=None):
    data = {
        "defaults": {"profile": "x",
                     **({"providers": defaults_providers} if defaults_providers else {})},
        "canvas": {"9:16": [1080, 1920]},
        "providers": providers or {
            "seedream": {"kind": "image", "status": "ready",
                         "base_url": "https://x", "model": "m-img",
                         "api_key_env": "ARK_API_KEY"},
            "seedtts": {"kind": "tts", "status": "ready",
                        "api_key_env": "ARK_TTS_API_KEY"},
            "minimax": {"kind": "tts", "status": "ready",
                        "api_key_env": "MINIMAX_API_KEY"},
            "seedance-mini": {"kind": "video", "status": "ready", "impl": "seedance",
                         "api_key_env": "ARK_API_KEY"},
        },
        "profiles": profiles or {"x": {}},
    }
    return ConfigStore(data, source="<test>")


class TestDefaultsChain(unittest.TestCase):
    def test_profile_without_provider_uses_defaults(self):
        st = _store(profiles={"x": {"image": {"style_prefix": "p"}}},
                    defaults_providers={"image": "seedream"})
        prov, params = ModelRouter(st).resolve("image", "x")
        self.assertEqual(type(prov).__name__, "SeedreamProvider")
        self.assertEqual(params.get("style_prefix"), "p")   # 风格参数照常透传

    def test_profile_deviation_beats_defaults(self):
        st = _store(profiles={"x": {"tts": {"provider": "minimax"}}},
                    defaults_providers={"tts": "seedtts"})
        prov, _ = ModelRouter(st).resolve("tts", "x")
        self.assertEqual(type(prov).__name__, "MiniMaxTTSProvider")

    def test_video_without_defaults_raises_not_silently_picks_a_vendor(self):
        """视频与其他能力同一条失败路径：缺 defaults.providers.video 报配置错，
        绝不静默落到某个具体厂商别名——路由器里的厂商硬编码等于把「换模型改
        配置」的承诺废掉一半（零配置场景由 EMBEDDED_DEFAULTS 的 defaults 兜住，
        走不到这里）。"""
        st = _store(profiles={"x": {}})          # 无 defaults.providers、无偏离项
        with self.assertRaises(ConfigError) as ctx:
            ModelRouter(st).resolve("video", "x")
        self.assertIn("defaults.providers", str(ctx.exception))

    def test_missing_everywhere_raises_with_guidance(self):
        st = _store(profiles={"x": {}})
        with self.assertRaises(ConfigError) as ctx:
            ModelRouter(st).resolve("image", "x")
        self.assertIn("defaults.providers", str(ctx.exception))

    def test_force_mock_short_circuits(self):
        st = _store(profiles={"x": {}})
        prov, _ = ModelRouter(st, force_mock=True).resolve("image", "x")
        self.assertEqual(type(prov).__name__, "MockImageProvider")


class TestImplAliasFactory(unittest.TestCase):
    def test_alias_with_impl_reuses_adapter(self):
        # 同厂新模型：加别名 + impl 指向已有适配器 → 零代码切换
        st = _store(
            profiles={"x": {"image": {"provider": "seedream_v6"}}},
            providers={"seedream_v6": {"kind": "image", "status": "ready",
                                       "impl": "seedream", "base_url": "https://y",
                                       "model": "m-v6", "api_key_env": "ARK_API_KEY"}})
        prov, _ = ModelRouter(st).resolve("image", "x")
        self.assertEqual(type(prov).__name__, "SeedreamProvider")
        self.assertEqual(prov.model, "m-v6")     # 别名自己的连接段生效

    def test_unknown_impl_raises_actionable_error(self):
        st = _store(
            profiles={"x": {"image": {"provider": "mystery"}}},
            providers={"mystery": {"kind": "image", "status": "ready"}})
        with self.assertRaises(ProviderError) as ctx:
            ModelRouter(st).resolve("image", "x")
        self.assertIn("impl", str(ctx.exception))

    def test_planned_status_still_blocked(self):
        st = _store(
            profiles={"x": {"image": {"provider": "veo_img"}}},
            providers={"veo_img": {"kind": "image", "status": "planned",
                                   "impl": "seedream"}})
        with self.assertRaises(ProviderError):
            ModelRouter(st).resolve("image", "x")


class TestConfigFallbackFlag(unittest.TestCase):
    """回退内置精简配置必须带机器可读标记 `fallback`——缺了它的失败链：brew 升
    Python 后 PyYAML 随旧版本路径失联，引擎静默用 30 画风的内置子集服务，Studio
    新建项目 kn-anime3d 只剩 1 个画风，唯一的 ⚠ 打在无人看的 stdout。这个标记是
    doctor `[!]` 行与 /api/overview config 块（前端告警条）共同的数据源。"""

    def test_missing_pyyaml_sets_fallback(self):
        import contextlib
        import io
        import sys
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "models.yaml"
            f.write_text("profiles: {}\n", encoding="utf-8")
            old = sys.modules.get("yaml")
            sys.modules["yaml"] = None      # 令 `import yaml` 抛 ImportError
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    store = ConfigStore.load(str(f))
            finally:
                if old is None:
                    sys.modules.pop("yaml", None)
                else:
                    sys.modules["yaml"] = old
        self.assertEqual(store.fallback, "missing-pyyaml")
        self.assertIn("embedded", store.source or "")

    def test_missing_config_sets_fallback(self):
        from unittest import mock

        from kinema import models
        with mock.patch.object(models, "_find_models_file", return_value=None):
            store = ConfigStore.load()
        self.assertEqual(store.fallback, "missing-config")

    def test_real_yaml_has_no_fallback(self):
        # 健康环境（PyYAML 在位 + 真源可读）fallback 必须为 None——否则前端会
        # 对正常环境常挂告警条，该告警即成恒真误报
        import importlib.util
        from pathlib import Path
        if importlib.util.find_spec("yaml") is None:
            raise unittest.SkipTest("本机缺 PyYAML")
        real = Path(__file__).resolve().parent.parent.parent / "config" / "models.yaml"
        if not real.is_file():
            raise unittest.SkipTest("仓库 config/models.yaml 不在")
        store = ConfigStore.load(str(real))
        self.assertIsNone(store.fallback)
        self.assertEqual(store.source, str(real))


if __name__ == "__main__":
    unittest.main()


class TestVideoResolver(unittest.TestCase):
    """视频路由只有 `ModelRouter.resolve_video` 一处：本次点名 > 章节 `video_provider` > profile 链。"""

    def test_precedence(self):
        st = ConfigStore.load(None)
        if st.fallback is not None:
            self.skipTest("需要 config/models.yaml")
        from kinema.models import ModelRouter, resolve_video
        r = ModelRouter(st)
        mini = st.provider_conn("seedance-mini")["model"]
        big = st.provider_conn("seedance-2.5")["model"]
        self.assertEqual(resolve_video(r, st, {}, "anime")[0].model, mini)
        self.assertEqual(resolve_video(r, st, {"video_provider": "seedance-2.5"},
                                       "anime")[0].model, big)
        self.assertEqual(resolve_video(r, st, {"video_provider": "seedance-2.5"}, "anime",
                                       override="seedance-mini")[0].model, mini)
        self.assertEqual(resolve_video(r, st, {"video_provider": "seedance-2.5"}, "anime")[1],
                         dict(st.profile("anime").get("video") or {}))

    def test_chapter_set_writes_and_validates_the_alias(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path

        from kinema.cli import build_parser
        from kinema.errors import ConfigError
        from kinema.workspace import Workspace
        from tests.support import LocalBackendEnv
        st = ConfigStore.load(None)
        if st.fallback is not None:
            self.skipTest("需要 config/models.yaml")
        env = LocalBackendEnv()
        env.enable()
        self.addCleanup(env.restore)
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace.open(str(Path(d) / "ws"))
            s = ws.create_project("路由", pid="vp")
            s.create_chapter("第一章", cid="ch01")

            def run(*argv):
                ns = build_parser().parse_args([*argv, "--workspace", ws.root.as_posix()])
                with contextlib.redirect_stdout(io.StringIO()):
                    return ns.func(ns)

            run("chapter", "set", "vp", "ch01", "--video-provider", "seedance-2.5")
            cf = ws.store.chapter_path("vp", "ch01")
            self.assertEqual(json.loads(Path(cf).read_text(encoding="utf-8"))["video_provider"],
                             "seedance-2.5")
            with self.assertRaises(ConfigError):
                run("chapter", "set", "vp", "ch01", "--video-provider", "seedream")
            run("chapter", "set", "vp", "ch01", "--inherit")
            self.assertNotIn("video_provider", json.loads(Path(cf).read_text(encoding="utf-8")))


class TestVideoDualModelStrategy(unittest.TestCase):
    """视频双模型策略：seedance 别名=2.0 mini（缺省主力），2.5 大模型只有
    **显式点名**（`resolve_named` / `gen-video --video-provider seedance-2.5` /
    章节文档顶层 `video_provider`）才上——大模型绝不静默升级。"""

    def test_default_video_alias_is_the_mini_model(self):
        """两处真源逐字对拍：EMBEDDED_DEFAULTS 与 config/models.yaml 的 seedance
        model 必须都是 mini——漂移守卫只比 provider 绑定不比 model 串，这里补上。"""
        from kinema.models import EMBEDDED_DEFAULTS
        emb = EMBEDDED_DEFAULTS["providers"]["seedance-mini"]["model"]
        self.assertEqual(emb, "doubao-seedance-2-0-mini-260615")
        st = ConfigStore.load(None)
        if st.fallback is None:   # 有 yaml 时连 yaml 一起对
            self.assertEqual(st.provider_conn("seedance-mini").get("model"), emb)
            self.assertEqual(st.provider_conn("seedance-2.5").get("model"),
                             "doubao-seedance-2-5-260628")
            self.assertEqual(st.provider_conn("seedance-2.5").get("impl"), "seedance",
                             "2.5 别名必须复用 seedance 适配器（零代码）")
            # 单价两处锁步（mini 0.5 / 2.5 大模型 2.0）——
            # 预估与计费同源，内嵌回退态跑出另一份报价就是台账分叉
            from kinema.models import EMBEDDED_DEFAULTS as _E
            for alias in ("seedance-mini", "seedance-2.5"):
                for k in ("price_per_second", "price_per_second_4k"):
                    self.assertEqual(_E["providers"][alias][k],
                                     st.provider_conn(alias).get(k),
                                     f"{alias}.{k} 内嵌与 yaml 分叉")

    def test_embedded_capability_bits_match_yaml(self):
        """两个 seedance 别名的**全部能力位**内嵌与 yaml 逐项一致——内嵌表是缺
        PyYAML 的回退态，能力位缺一项就回落适配器默认：mini/2.5 都会被塞进官方
        已不支持的 seed，2.5 还会发 explicit ratio、把 20s 镜静默截到 15s
        （这条与远端行为无关，本地钳的，一定发生）。"""
        from kinema.models import EMBEDDED_DEFAULTS
        st = ConfigStore.load(None)
        if st.fallback is not None:
            self.skipTest("无 models.yaml，无从对拍")
        caps = ("resolution", "resolutions", "min_duration", "max_duration",
                "ratio_mode", "supports_seed", "supports_camera_fixed",
                "supports_last_frame", "max_ref_audios", "max_ref_audio_seconds",
                "timeline_unit")
        for alias in ("seedance-mini", "seedance-2.5"):
            emb = EMBEDDED_DEFAULTS["providers"][alias]
            yml = st.provider_conn(alias)
            for k in caps:
                self.assertEqual(emb.get(k), yml.get(k),
                                 f"{alias}.{k} 内嵌={emb.get(k)!r} / yaml={yml.get(k)!r} 分叉")

    def test_resolve_named_picks_the_25_model(self):
        st = _store(providers={
            "seedance-mini": {"kind": "video", "status": "ready", "impl": "seedance",
                         "api_key_env": "ARK_API_KEY", "model": "mini-m"},
            "seedance-2.5": {"kind": "video", "status": "ready", "impl": "seedance",
                           "api_key_env": "ARK_API_KEY", "model": "m-25"}})
        prov = ModelRouter(st).resolve_named("video", "seedance-2.5")
        self.assertEqual(type(prov).__name__, "SeedanceProvider")
        self.assertEqual(prov.model, "m-25")
        # 缺省链照旧解析到 mini（点名与缺省互不污染）
        st2 = _store(defaults_providers={"video": "seedance-mini"}, providers={
            "seedance-mini": {"kind": "video", "status": "ready", "impl": "seedance",
                         "api_key_env": "ARK_API_KEY", "model": "mini-m"}})
        prov2, _ = ModelRouter(st2).resolve("video", "x")
        self.assertEqual(prov2.model, "mini-m")

    def test_resolve_named_rejects_unknown_and_wrong_kind(self):
        st = _store()
        with self.assertRaises(ConfigError):
            ModelRouter(st).resolve_named("video", "nope")
        with self.assertRaises(ConfigError) as ctx:
            ModelRouter(st).resolve_named("video", "seedream")   # image 别名指给 video
        self.assertIn("不能用于 video", str(ctx.exception))

    def test_resolve_named_stays_offline_under_mock(self):
        prov = ModelRouter(_store(), force_mock=True).resolve_named("video", "seedance-2.5")
        self.assertEqual(type(prov).__name__, "MockVideoProvider",
                         "离线彩排不该因点名而联网")

    def test_image_default_is_seedream_5_pro(self):
        from kinema.models import EMBEDDED_DEFAULTS
        self.assertEqual(EMBEDDED_DEFAULTS["providers"]["seedream"]["model"],
                         "doubao-seedream-5-0-pro-260628")
        st = ConfigStore.load(None)
        if st.fallback is None:
            self.assertEqual(st.provider_conn("seedream").get("model"),
                             "doubao-seedream-5-0-pro-260628")


class TestLegacyAliasCompat(unittest.TestCase):
    """别名改过名，而存量 project.json 的顶层 `video_provider` 可能还点着旧名。

    直接抛「未知 provider」会让老项目在某次升级后突然跑不了，且报错指向的是
    一个用户从没改过的字段。所以认旧名、按新名解析、并说一声让人去改。
    """

    def test_old_names_still_resolve_to_the_new_ones(self):
        from kinema.models import LEGACY_ALIASES
        st = ConfigStore.load()
        self.assertGreaterEqual(len(LEGACY_ALIASES), 2)
        # 兼容位的值必须都是真别名、键必须都不是（下面逐条验），这里只钉两个核心项
        self.assertEqual(LEGACY_ALIASES["seedance"], "seedance-mini")
        self.assertEqual(LEGACY_ALIASES["seedance25"], "seedance-2.5")
        for old, new in LEGACY_ALIASES.items():
            self.assertEqual(st.provider_conn(old).get("model"),
                             st.provider_conn(new).get("model"), old)
            # 解析出的 name 必须是**新名**——留着旧名会让下游日志与快照继续传播它
            self.assertEqual(st.provider_conn(old)["name"], new)

    def test_unknown_alias_still_raises(self):
        """兼容位只认这两个旧名，别把它变成「什么名字都收」。"""
        with self.assertRaises(ConfigError):
            ConfigStore.load().provider_conn("seedance99")


class TestGenVideoProviderOverride(unittest.TestCase):
    """gen-video 的点名通道：flag / 章节文档顶层 `video_provider` → `resolve_named`。
    dry-run 报价、事前闸、逐镜真发共用 `_vroute` 一个解析口（分叉=报价与账单对不上）。"""

    def test_dry_run_resolves_named_provider_and_prints_the_model(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path

        from kinema.cli import stage_gen_video
        from kinema.project import Project
        from tests.support import LocalBackendEnv
        st = ConfigStore.load(None)
        if st.fallback is not None:
            self.skipTest("无 models.yaml（内置回退态），别名对拍在上一组已覆盖")
        env = LocalBackendEnv()
        env.enable()
        try:
            with tempfile.TemporaryDirectory() as d:
                img = Path(d) / "s1.png"
                img.write_bytes(b"\x89PNG\r\n\x1a\n")
                cf = Path(d) / "ch01.json"

                def _write(extra=None):
                    doc = {"id": "t", "profile": "anime", "motion": "native",
                           "aspect": "16:9",
                           "shots": [{"id": 1, "dur": 5.0, "image": str(img),
                                      "video_prompt": "转身"}]}
                    doc.update(extra or {})
                    cf.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

                def run(**kw):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        stage_gen_video(Project.load(cf), st, ModelRouter(st),
                                        dry_run=True, **kw)
                    return buf.getvalue()

                _write()
                self.assertIn("doubao-seedance-2-0-mini-260615", run(),
                              "缺省必须是 mini 主力")
                out = run(video_provider="seedance-2.5")
                self.assertIn("doubao-seedance-2-5-260628", out)
                self.assertIn("点名 provider=seedance-2.5", out)
                _write({"video_provider": "seedance25"})   # 章节文档顶层持久档同样生效
                self.assertIn("doubao-seedance-2-5-260628", run())
        finally:
            env.restore()


class TestImageRoute(unittest.TestCase):
    """生图三级路由（models.image_route，命中即停）：①models 显式激活（覆盖层
    原文有键才算）> ②KINEMA_AGENT_IMAGEGEN 声明 > ③默认链。声明凌驾 profile
    偏离项——那是画风作者的 API 偏好，不是用户本人的选择；唯 ①级显式激活与
    --mock 压得过声明。覆盖层一律打桩：路由读的是本机文件，不打桩就不封闭。"""

    PROVIDERS = {
        "seedream": {"kind": "image", "status": "ready", "base_url": "https://x",
                     "model": "m-img", "api_key_env": "ARK_API_KEY"},
        "minimax": {"kind": "tts", "status": "ready",
                    "api_key_env": "MINIMAX_API_KEY"},
        "agent": {"kind": "image", "status": "ready", "impl": "agent"},
    }

    def _st(self, profiles=None, defaults=None):
        return _store(profiles=profiles or {"x": {}},
                      defaults_providers=defaults or {"image": "seedream"},
                      providers=dict(self.PROVIDERS))

    def _env(self, declared):
        if declared:
            return mock.patch.dict(os.environ, {"KINEMA_AGENT_IMAGEGEN": "1"})
        patch = mock.patch.dict(os.environ)
        return _PopKey(patch, "KINEMA_AGENT_IMAGEGEN")

    def test_no_signals_means_default(self):
        with mock.patch("kinema.config_overlay.read", return_value={}), \
                self._env(False):
            self.assertEqual(image_route(self._st()),
                             {"provider": "seedream", "source": "default"})

    def test_declaration_beats_profile_deviation(self):
        st = self._st(profiles={"x": {"image": {"provider": "seedream",
                                                "style_prefix": "p"}}})
        with mock.patch("kinema.config_overlay.read", return_value={}), \
                self._env(True):
            self.assertEqual(image_route(st)["source"], "agent")
            prov, params = ModelRouter(st).resolve("image", "x")
        self.assertEqual(type(prov).__name__, "AgentImageProvider")
        self.assertEqual(params.get("style_prefix"), "p",
                         "改走 agent 只换出图人，风格参数照常透传")

    def test_explicit_activation_beats_declaration(self):
        st = self._st()
        with mock.patch("kinema.config_overlay.read",
                        return_value={"defaults": {"providers": {"image": "seedream"}}}), \
                self._env(True):
            self.assertEqual(image_route(st)["source"], "explicit")
            prov, _ = ModelRouter(st).resolve("image", "x")
        self.assertEqual(type(prov).__name__, "SeedreamProvider")

    def test_mock_beats_declaration(self):
        with mock.patch("kinema.config_overlay.read", return_value={}), \
                self._env(True):
            prov, _ = ModelRouter(self._st(), force_mock=True).resolve("image", "x")
        self.assertEqual(type(prov).__name__, "MockImageProvider",
                         "离线彩排不该因声明而改道")

    def test_declaration_never_touches_other_capabilities(self):
        st = self._st(defaults={"image": "seedream", "tts": "minimax"})
        with mock.patch("kinema.config_overlay.read", return_value={}), \
                self._env(True):
            prov, _ = ModelRouter(st).resolve("tts", "x")
        self.assertEqual(type(prov).__name__, "MiniMaxTTSProvider")


class TestVideoResolutionDefaults(unittest.TestCase):
    """视频模型的缺省分辨率一律不高于 720p 档。

    两个理由，都不是画质偏好：① 视频按秒计费且高档单价成倍，缺省高一档等于每次调用都
    多付钱，而"没人点名分辨率"恰恰是最常见的调用方式；② Seedance 2.0 系列与 2.5 的合法
    档只到 720p，兜底到更高档换回的是一个远端 400。**适配器兜底档同样要守**——别名忘写
    `resolution` 时落的就是它，而那种疏漏没有任何报错提示。
    """

    # 各厂档位名不同：MiniMax H3 只有 768P/2K 两档，768P 就是它的低档
    LOW_TIER = {"480p", "720p", "768p"}

    def _video_aliases(self):
        from pathlib import Path
        import yaml
        cfg = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        return {k: v for k, v in (data.get("providers") or {}).items()
                if isinstance(v, dict) and str(v.get("kind") or "") == "video"}

    def test_every_alias_declares_a_low_tier_default(self):
        for alias, conn in self._video_aliases().items():
            res = str(conn.get("resolution") or "").strip().lower()
            if not res:
                continue                      # 未声明 → 由适配器兜底档负责，见下一条
            self.assertIn(res, self.LOW_TIER,
                          f"{alias} 缺省 {res}：视频缺省档不得高于 720p 档")

    def test_adapter_fallbacks_are_low_tier(self):
        """别名不写 `resolution` 时落的兜底档——疏漏无报错，只能靠这条守。"""
        from kinema.models import ConfigStore as _CS
        from kinema.providers.video.minimax import MiniMaxVideoProvider
        from kinema.providers.video.seedance import SeedanceProvider
        from kinema.providers.video.veo import VeoVideoProvider
        store = _CS.load(None)
        for cls in (SeedanceProvider, VeoVideoProvider):
            res = str(getattr(cls({}, store), "resolution", "")).lower()
            self.assertIn(res, self.LOW_TIER, f"{cls.__name__} 兜底 {res} 高于 720p 档")
        # MiniMax 的档位名自成一套，构造器归一后再比
        mm = MiniMaxVideoProvider({}, store)
        self.assertIn(str(getattr(mm, "resolution", "768P")).lower(), self.LOW_TIER)


class _PopKey:
    """mock.patch.dict(os.environ) 只会快照还原，不会替你删键——包一层，
    进场先摘掉指定键，出场由 patch.dict 恢复原状。"""

    def __init__(self, patch, key):
        self.patch, self.key = patch, key

    def __enter__(self):
        self.patch.__enter__()
        os.environ.pop(self.key, None)
        return self

    def __exit__(self, *exc):
        return self.patch.__exit__(*exc)
