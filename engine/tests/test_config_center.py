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

"""模型配置覆盖层与配置中心的守卫。

这批改动**绕过了全部既有漂移守卫**：`test_config_drift` 的用例直接读
config/models.yaml 文件与 `EMBEDDED_DEFAULTS` 比对，全程不经过 `ConfigStore`——
也就是说覆盖层可以把默认 provider 改成任何东西而那边全绿。所以它必须自带守卫，
且守的重点是三件容易静默出错的事：无损回落、深合并不吃掉旁边的东西、密钥不外泄。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kinema import config_overlay as ovl
from kinema.errors import ConfigError
from kinema.models import EMBEDDED_DEFAULTS, ConfigStore
from tests.support import LocalBackendEnv

REPO = Path(__file__).resolve().parent.parent.parent


class _Overlay:
    """把覆盖层指向临时文件的上下文（用完即还原）。"""

    def __init__(self, doc: dict | str | None = None, secrets: dict | None = None):
        self.doc, self.secrets = doc, secrets

    def __enter__(self):
        self._prev = os.environ.get(ovl.ENV_OVERLAY)
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.path = d / ovl.OVERLAY_FILE
        if self.doc is not None:
            self.path.write_text(self.doc if isinstance(self.doc, str)
                                 else json.dumps(self.doc), encoding="utf-8")
        if self.secrets is not None:
            (d / ovl.SECRETS_FILE).write_text(
                json.dumps({"secrets": self.secrets}), encoding="utf-8")
        os.environ[ovl.ENV_OVERLAY] = str(self.path)
        return self

    def __exit__(self, *a):
        if self._prev is None:
            os.environ.pop(ovl.ENV_OVERLAY, None)
        else:
            os.environ[ovl.ENV_OVERLAY] = self._prev
        self._tmp.cleanup()
        return False


def _load() -> ConfigStore:
    return ConfigStore.load()


class TestOverlayIsDisabledInTests(unittest.TestCase):
    """这道闸本身必须真的关得掉——它是「用例不受这台机器配过什么影响」的唯一防线。"""

    def test_sentinel_actually_disables(self):
        """`tests/__init__.py` 里那行必须是**显式哨兵**而不是空串。

        覆盖层的发现顺序里，空值只是「本级没指定」、会继续往下找，磁盘上真实存在的
        那份照样被读进去——那样这行就是个永不触发的摆设，而它防的恰恰是最难查的
        一类失败：开发者在网页上换过激活项之后，默认链相关的用例只在他这台机器上红。
        """
        self.assertEqual(os.environ.get(ovl.ENV_OVERLAY), "off")
        self.assertTrue(ovl.disabled())
        self.assertIsNone(ovl.overlay_path())
        self.assertIsNone(ovl.secrets_path())
        self.assertEqual(ovl.read(), {})
        self.assertEqual(ovl.read_secrets(), {})

    def test_off_sentinels_cover_the_falsy_traps(self):
        for v in ("", "0", "off", "OFF", "no", "none", "false"):
            with patch.dict(os.environ, {ovl.ENV_OVERLAY: v}):
                self.assertTrue(ovl.disabled(), v)


class TestLosslessFallback(unittest.TestCase):
    """「前端不配置就走配置文件兜底」这条硬要求的可执行定义。"""

    def test_no_overlay_means_byte_identical_config(self):
        base = json.dumps(_load().data, sort_keys=True, ensure_ascii=False)
        with _Overlay():                       # 指向一个**不存在**的覆盖层文件
            self.assertEqual(
                json.dumps(_load().data, sort_keys=True, ensure_ascii=False), base)

    def test_broken_overlay_falls_back_instead_of_crashing(self):
        base = json.dumps(_load().data, sort_keys=True, ensure_ascii=False)
        for bad in ("{ not json", "[]", '"a string"', "null"):
            with _Overlay(bad):
                self.assertEqual(
                    json.dumps(_load().data, sort_keys=True, ensure_ascii=False), base,
                    f"坏覆盖层 {bad!r} 应被忽略并回落配置文件")

    def test_overlay_never_mutates_the_embedded_defaults(self):
        """合并必须建新字典。

        两条兜底出口把模块级的 `EMBEDDED_DEFAULTS` 原样交出来，就地改一次就污染
        整个进程：此后同进程内每次 load 都带着上一次的残留（Studio 一次页面加载会
        连打多个接口、一趟 run 里反复 load），而「没有覆盖层时逐字节相同」那条
        守卫恰恰抓不到它。
        """
        before = json.dumps(EMBEDDED_DEFAULTS, sort_keys=True, ensure_ascii=False)
        ov = {"providers": {"seedance-mini": {"base_url": "https://poison/v9"}},
              "defaults": {"providers": {"video": "veo"}}}
        # 直接打这条路：apply 是合并的唯一出口，兜底出口把模块级字典原样交给它
        for _ in range(5):
            ovl.apply(EMBEDDED_DEFAULTS, ov)
        self.assertEqual(
            json.dumps(EMBEDDED_DEFAULTS, sort_keys=True, ensure_ascii=False), before,
            "apply 就地改写了入参")
        # 再走一遍真实的兜底出口（找不到 models.yaml）——就地改写在这条路上才致命：
        # 此后同进程内每次 load 都带着上一次的残留，而"没有覆盖层时逐字节相同"
        # 那条守卫站在 yaml 分支上、根本照不到这里
        with _Overlay(ov), patch("kinema.models._find_models_file", return_value=None):
            for _ in range(5):
                st = _load()
            self.assertEqual(st.provider_conn("seedance-mini")["base_url"], "https://poison/v9")
        self.assertEqual(
            json.dumps(EMBEDDED_DEFAULTS, sort_keys=True, ensure_ascii=False), before,
            "兜底出口污染了模块级的 EMBEDDED_DEFAULTS")


class TestDeepMerge(unittest.TestCase):
    """浅合并是这里最容易犯的错：照抄 ConfigStore 那行，用户改一个地址就抹掉其余别名。"""

    def test_patching_one_field_keeps_every_sibling(self):
        base = _load()
        n_alias = len(base.data["providers"])
        with _Overlay({"providers": {"seedance-mini": {"base_url": "https://gw.local/api/v3"}}}):
            s = _load()
            self.assertEqual(len(s.data["providers"]), n_alias, "别名数量变了=整段被替换")
            sd = s.provider_conn("seedance-mini")
            self.assertEqual(sd["base_url"], "https://gw.local/api/v3")
            # 同别名的其余字段与其余别名一律保持配置文件原值
            self.assertEqual(sd["model"], base.provider_conn("seedance-mini")["model"])
            self.assertEqual(sd["price_per_second"],
                             base.provider_conn("seedance-mini")["price_per_second"])
            for a in base.data["providers"]:
                if a != "seedance-mini":
                    self.assertEqual(s.provider_conn(a), base.provider_conn(a), a)

    def test_activating_one_capability_keeps_the_sibling_defaults(self):
        """`defaults` 里与 providers 并列的还有 profile / fps 两个兄弟键。

        按顶层键替换会把它们一起带走，于是所有不带 --profile 的命令悄悄换了画风、
        全片帧率也可能变，且没有任何告警。（末条断言连未登记的 `aspect` 键一并
        核对：覆盖层不得往 `defaults` 里凭空写入新的兄弟键。）
        """
        base = _load()
        with _Overlay({"defaults": {"providers": {"video": "seedance-2.5"}}}):
            s = _load()
            self.assertEqual(s.default_provider("video"), "seedance-2.5")
            self.assertEqual(s.default_provider("image"), base.default_provider("image"))
            self.assertEqual(s.default_profile, base.default_profile)
            self.assertEqual(s.fps, base.fps)
            self.assertEqual(s.data["defaults"].get("aspect"),
                             base.data["defaults"].get("aspect"))

    def test_new_alias_can_be_created(self):
        with _Overlay({"providers": {"my_gateway": {
                "kind": "image", "impl": "seedream", "status": "ready",
                "base_url": "http://127.0.0.1:8188/v1", "model": "local-sd"}}}):
            conn = _load().provider_conn("my_gateway")
            self.assertEqual(conn["impl"], "seedream")
            self.assertEqual(conn["base_url"], "http://127.0.0.1:8188/v1")

    def test_only_providers_and_defaults_may_be_overridden(self):
        """profiles / canvas 刻意不可覆盖——画风侧的三条一致性守卫直读 yaml 文件，
        覆盖层若能改 profiles 就是一条守卫完全看不见的后门。"""
        base = _load()
        with _Overlay({"profiles": {"anime": {"image": {"style_prefix": "污染"}}},
                       "canvas": {"16:9": [9999, 9999]},
                       "voices": {"解说男": "hacked"}}):
            s = _load()
            self.assertEqual(s.profile("anime"), base.profile("anime"))
            self.assertEqual(s.canvas("16:9"), base.canvas("16:9"))
            self.assertEqual(s.resolve_voice("解说男"), base.resolve_voice("解说男"))

    def test_null_field_is_not_an_override(self):
        """值为 None = 这个字段没被覆盖（三态里的「清除」），而不是把它设成空。"""
        base = _load().provider_conn("seedance-mini")
        with _Overlay({"providers": {"seedance-mini": {"base_url": None, "model": "m9"}}}):
            conn = _load().provider_conn("seedance-mini")
            self.assertEqual(conn["base_url"], base["base_url"], "None 不该抹掉配置文件的值")
            self.assertEqual(conn["model"], "m9")

    def test_alias_mapped_to_null_restores_the_whole_entry(self):
        base = _load().provider_conn("seedance-mini")
        with _Overlay({"providers": {"seedance-mini": None}}):
            self.assertEqual(_load().provider_conn("seedance-mini"), base)


class TestSecretBoundary(unittest.TestCase):
    """密钥的四条红线：不入配置文件、不入库、不下发、不回显。"""

    FAKE = "sk-DO-NOT-LEAK-abcdef0123456789"

    def test_priority_env_over_local_over_yaml(self):
        with _Overlay({}, secrets={"MINIMAX_API_KEY": self.FAKE}):
            s = _load()
            self.assertEqual(s.secret("MINIMAX_API_KEY"), self.FAKE)
            with patch.dict(os.environ, {"MINIMAX_API_KEY": "from-env"}):
                self.assertEqual(_load().secret("MINIMAX_API_KEY"), "from-env")

    def test_secret_never_appears_in_any_downstream_payload(self):
        """把假密钥写进本机密钥文件，断言它一个字符都不出现在下发给前端的整份视图里。"""
        with _Overlay({}, secrets={"MINIMAX_API_KEY": self.FAKE}):
            store = _load()
            self.assertEqual(store.secret("MINIMAX_API_KEY"), self.FAKE)  # 确实读到了
            for payload in (ovl.config_view(store), ovl.provider_view(store),
                            ovl.probe(store, "minimax"), ovl.summary() or {}):
                self.assertNotIn(self.FAKE, json.dumps(payload, ensure_ascii=False,
                                                       default=str))

    def test_key_state_reports_layer_not_value(self):
        with _Overlay({}, secrets={"MINIMAX_API_KEY": self.FAKE}):
            store = _load()
            self.assertEqual(ovl.key_state(store, "MINIMAX_API_KEY"), "local")
            self.assertEqual(ovl.key_state(store, "NOPE_KEY_XYZ"), "unset")
            self.assertEqual(ovl.key_state(store, None), "none")
            with patch.dict(os.environ, {"MINIMAX_API_KEY": "x"}):
                self.assertEqual(ovl.key_state(store, "MINIMAX_API_KEY"), "env")

    def test_secrets_are_refused_by_the_database_layer(self):
        """密钥绝不入库：库行随备份与多机同步走，一进去就等于换机把密钥也带走。"""
        from kinema.storage.mysql import MySQLStorage
        st = MySQLStorage.__new__(MySQLStorage)
        with self.assertRaises(ConfigError):
            MySQLStorage.save_settings(st, "secrets", "x", {"K": "v"})

    def test_media_endpoint_never_serves_the_local_config_files(self):
        """`/media` 是无鉴权 GET，而密钥文件是 .json（放行后缀之一）、又住在缺省
        扫描根之内的 config/ 下——不显式拒绝的话，一个 curl 就能把密钥原文取走，
        文件权限 0600 在这条路上形同虚设（服务端以属主身份读）。
        """
        from kinema.studio import server
        self.assertIn(ovl.SECRETS_FILE, server._DENY_NAMES)
        self.assertIn(ovl.OVERLAY_FILE, server._DENY_NAMES)
        src = (REPO / "engine/kinema/studio/server.py").read_text(encoding="utf-8")
        self.assertIn("p.name in _DENY_NAMES or p.name.startswith(_DENY_PREFIX)", src)
        # 原子写的半截文件同样是明文，前缀匹配要盖住它
        self.assertTrue(f"{ovl.SECRETS_FILE}.1234.abcd.tmp".startswith(
            tuple(server._DENY_PREFIX)))
        # 反向闸：拒绝面不许扩大。`.json` 在放行后缀里本来就是给字幕时间轴、
        # 全片预演清单这类产物用的，把它们一并拒掉等于打断既有功能。
        for ok in ("reel.json", "timestamps.json", "manifest.json", "digest.json"):
            self.assertNotIn(ok, server._DENY_NAMES)
            self.assertFalse(ok.startswith(tuple(server._DENY_PREFIX)), ok)

    def test_local_files_are_gitignored(self):
        txt = (REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("config/secrets.local.json", txt)
        self.assertIn("config/models.local.json", txt)

    def test_write_secret_rejects_bogus_env_names(self):
        with _Overlay({}):
            for bad in ("", "lower_case", "with space", "sk-actual-key"):
                with self.assertRaises(ConfigError, msg=bad):
                    ovl.write_secret(bad, "v")

    def test_write_secret_roundtrip(self):
        """写→清全链路必须真跑一遍：这条路径只有三个真实调用方
        （config secret / setup 向导 / Studio 配置页），单元测试若只测
        参数校验分支，签名与函数体脱节时全量照样全绿。"""
        with _Overlay({}) as ov:
            r = ovl.write_secret("KINEMA_TEST_KEY", " v1 ", explicit=None)
            self.assertEqual(r, {"env": "KINEMA_TEST_KEY", "state": "local"})
            spath = ov.path.parent / ovl.SECRETS_FILE
            data = json.loads(spath.read_text(encoding="utf-8"))
            self.assertEqual(data["secrets"]["KINEMA_TEST_KEY"], "v1")
            r = ovl.write_secret("KINEMA_TEST_KEY", None)
            self.assertEqual(r["state"], "unset")
            data = json.loads(spath.read_text(encoding="utf-8"))
            self.assertNotIn("KINEMA_TEST_KEY", data.get("secrets") or {})

    def test_write_secret_follows_the_explicit_config_path(self):
        """`config secret --config` 与 overlay/secrets_path 同一条 explicit 语义：
        密钥落在被指定 models.yaml 旁边，而不是缺省 config/ 下。"""
        with tempfile.TemporaryDirectory() as d:
            alt = Path(d) / "models.yaml"
            alt.write_text("version: 1\n", encoding="utf-8")
            prev = os.environ.pop(ovl.ENV_OVERLAY, None)
            try:
                ovl.write_secret("KINEMA_TEST_KEY", "v2", explicit=str(alt))
                data = json.loads((Path(d) / ovl.SECRETS_FILE)
                                  .read_text(encoding="utf-8"))
                self.assertEqual(data["secrets"]["KINEMA_TEST_KEY"], "v2")
            finally:
                if prev is not None:
                    os.environ[ovl.ENV_OVERLAY] = prev


class TestWriteWhitelist(unittest.TestCase):
    """写入口的白名单拦在源头，比 gitignore 有效——它不让密钥落进这份会入库的文件。"""

    def test_secret_looking_fields_are_refused(self):
        for k in ("api_key", "secret_key", "access_token", "password"):
            with self.assertRaises(ConfigError, msg=k):
                ovl.validate_fields({k: "sk-xxx"})

    def test_env_reference_must_look_like_an_env_name(self):
        self.assertEqual(ovl.validate_fields({"api_key_env": "MINIMAX_API_KEY"}),
                         {"api_key_env": "MINIMAX_API_KEY"})
        with self.assertRaises(ConfigError):
            ovl.validate_fields({"api_key_env": "sk-this-is-the-real-key"})

    def test_unknown_fields_are_refused(self):
        for k in ("profiles", "canvas", "effects", "style_prefix"):
            with self.assertRaises(ConfigError, msg=k):
                ovl.validate_fields({k: "x"})

    def test_non_finite_prices_are_refused(self):
        """Infinity / NaN 不是合法 JSON。放进去的话，此后每次读配置都失败，
        而它还会一路进成本台账与预算闸参与算术。"""
        for bad in ("inf", "-inf", "Infinity", "nan", "NaN"):
            with self.assertRaises(ConfigError, msg=bad):
                ovl.validate_fields({"price_per_second": bad})

    def test_atomic_write_never_emits_non_finite_json(self):
        import math
        with _Overlay({}) as ctx:
            with self.assertRaises(ValueError):
                ovl._atomic_write(ctx.path, {"x": math.inf})

    def test_price_fields_land_as_numbers_not_strings(self):
        """网页表单交上来的一律是字符串。`price_per_second: "0.5"` 原样存进去，
        就会以字符串形态进成本台账与预算闸参与算术——一条静默错账的路。"""
        got = ovl.validate_fields({"price_per_second": "0.5", "price_per_image": "2"})
        self.assertEqual(got, {"price_per_second": 0.5, "price_per_image": 2.0})
        self.assertIsInstance(got["price_per_second"], float)
        with self.assertRaises(ConfigError):
            ovl.validate_fields({"price_per_second": "贵"})
        with self.assertRaises(ConfigError):
            ovl.validate_fields({"price_per_second": "-1"})


class TestSyncLayerIsSanitized(unittest.TestCase):
    """上库与回流两条路都必须收敛到已知形状。

    「密钥只在本机那份文件里」这条承诺，靠的不该是「用户不会手改
    models.local.json」——手改过的文件同样会被整份 push 上库。
    """

    def test_push_strips_anything_not_whitelisted(self):
        doc = {"version": 1, "updated_at": "x",
               "providers": {"seedance-mini": {"base_url": "https://a/v1",
                                          "api_key": "sk-LEAK", "junk": 1},
                             "bad": {"api_key": "sk-LEAK2"}},
               "defaults": {"providers": {"video": "seedance-mini", "nope": "x"}},
               "profiles": {"anime": {}}}
        out = ovl.sanitized(doc)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("sk-LEAK", blob)
        self.assertNotIn("junk", blob)
        self.assertNotIn("profiles", out)
        self.assertEqual(out["providers"], {"seedance-mini": {"base_url": "https://a/v1"}})
        self.assertEqual(out["defaults"], {"providers": {"video": "seedance-mini"}})

    def test_broken_nested_types_do_not_kill_the_loader(self):
        """「覆盖层坏掉 = 回落配置文件 + 喊一声」这条契约必须在嵌套层也成立：
        只在顶层体检的话，providers 写成字符串会在合并时抛异常，使引擎与 Studio
        同时不可用，而产品内没有恢复入口。"""
        base = json.dumps(_load().data, sort_keys=True, ensure_ascii=False)
        for bad in ({"providers": "oops"}, {"providers": [1, 2]},
                    {"defaults": "oops"}, {"defaults": {"providers": 7}},
                    {"providers": {"seedance-mini": "oops"}}):
            with _Overlay(bad):
                self.assertEqual(
                    json.dumps(_load().data, sort_keys=True, ensure_ascii=False), base,
                    f"{bad} 应被忽略而不是让加载器整个失败")
                ovl.summary()            # 自述面同样不许炸
                ovl.config_view(_load())


class TestMultiCredentialProviders(unittest.TestCase):
    """多凭证厂商不止一把钥匙：只认 api_key_env 会让一半厂商的状态判错。"""

    def test_secret_envs_collects_every_key_slot(self):
        store = _load()
        # minimax 的 GROUP_ID 是第二把钥匙位，不收进来自检就会假绿（真跑才炸）
        envs = ovl.secret_envs(store.provider_conn("minimax"))
        self.assertIn("MINIMAX_GROUP_ID", envs)
        self.assertGreater(len(envs), 1, "多凭证厂商只收到一把钥匙")
        self.assertTrue(all(e.isupper() for e in envs))

    def test_web_can_fill_every_key_slot(self):
        """网页必须按 keys[] 逐个渲染密钥位。只渲染主 key 的话，缺第二把钥匙的厂商
        在界面上一片正常、真跑才炸，而页面上又没有任何地方能补上。"""
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(encoding="utf-8")
        self.assertIn("function keyRows(p)", cfg)
        self.assertIn("p.keys && p.keys.length", cfg)
        self.assertIn("openSecretDialog(p, k.env)", cfg)
        css = (REPO / "engine/kinema/studio_app/style.css").read_text(encoding="utf-8")
        self.assertIn(".cfg-keys", css)


class TestConsoleEntrypoints(unittest.TestCase):
    """「打开控制台」跳转的对拍守卫：凡声明了密钥变量的 provider 必须给出创建
    密钥的控制台入口——否则界面只说「缺密钥」却不说去哪拿。"""

    def test_every_keyed_provider_has_console_url(self):
        store = _load()
        for alias, conn in (store.data.get("providers") or {}).items():
            if not ovl.secret_envs(conn):
                continue        # 免密钥/本地服务商没有「官网」可去
            meta = ovl.IMPL_META.get((conn.get("kind"), conn.get("impl", alias))) or {}
            self.assertTrue(str(meta.get("console") or "").startswith("https://"),
                            f"{alias} 声明了密钥变量却没有控制台入口")

    def test_view_carries_console(self):
        provs = {p["alias"]: p for p in ovl.provider_view(_load())}
        self.assertTrue(str(provs["seedtts"]["console"] or "").startswith("https://"))

    def test_overlay_follows_the_explicit_config_path(self):
        """`--config` 指到另一份 models.yaml 时，覆盖层与密钥必须落在**它旁边**。
        否则读的是这份配置、覆盖的是另一份配置旁边那层，两边静默错配。"""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            alt = Path(d) / "models.yaml"
            alt.write_text("version: 1\n", encoding="utf-8")
            prev = os.environ.pop(ovl.ENV_OVERLAY, None)
            try:
                self.assertEqual(ovl.config_dir(str(alt)), Path(d))
                self.assertEqual(ovl.overlay_path(str(alt)), Path(d) / ovl.OVERLAY_FILE)
                self.assertEqual(ovl.secrets_path(str(alt)), Path(d) / ovl.SECRETS_FILE)
            finally:
                if prev is not None:
                    os.environ[ovl.ENV_OVERLAY] = prev

    def test_provider_view_ships_all_key_slots(self):
        store = _load()
        row = next(p for p in ovl.provider_view(store) if p["alias"] == "minimax")
        self.assertGreater(len(row["keys"]), 1)
        self.assertNotIn("sk-", json.dumps(row))        # 只出变量名与状态，绝无值


class TestThreeStateWrite(unittest.TestCase):
    """三态直接沿用工程既有那套（缺省=不动 / 空=清除 / 非空=覆盖），不发明第四种。"""

    def test_round_trip(self):
        with _Overlay({}) as ctx:
            ovl.save(providers={"seedance-mini": {"base_url": "https://a.local/v1"}})
            self.assertEqual(_load().provider_conn("seedance-mini")["base_url"],
                             "https://a.local/v1")
            # 缺省 = 不动（只写另一个字段，base_url 的覆盖仍在）
            ovl.save(providers={"seedance-mini": {"model": "m2"}})
            conn = _load().provider_conn("seedance-mini")
            self.assertEqual(conn["base_url"], "https://a.local/v1")
            self.assertEqual(conn["model"], "m2")
            # 空串 = 清除该字段
            ovl.save(providers={"seedance-mini": {"base_url": ""}})
            self.assertNotEqual(_load().provider_conn("seedance-mini")["base_url"],
                                "https://a.local/v1")
            self.assertEqual(_load().provider_conn("seedance-mini")["model"], "m2")
            # None = 整条恢复默认，文件里不再留该别名
            ovl.save(providers={"seedance-mini": None})
            self.assertNotIn("seedance-mini", ovl.read(ctx.path).get("providers") or {})

    def test_activation_round_trip(self):
        base = _load().default_provider("video")
        with _Overlay({}):
            ovl.save(defaults={"video": "seedance-2.5"})
            self.assertEqual(_load().default_provider("video"), "seedance-2.5")
            ovl.save(defaults={"video": ""})            # 恢复跟随配置文件
            self.assertEqual(_load().default_provider("video"), base)

    def test_legacy_alias_is_normalized_before_it_lands(self):
        """旧名兼容位**只在读的时候认**。写进覆盖层就等于让它沉淀下去：
        此后每次加载都要再翻译一次，还会随数据库同步到别的机器上。"""
        from kinema.studio import actions
        with _Overlay({}) as ctx:
            actions.set_model_config(None, defaults={"video": "seedance25"})
            got = (ovl.read(ctx.path).get("defaults") or {}).get("providers") or {}
            self.assertEqual(got.get("video"), "seedance-2.5")

    def test_unknown_capability_is_refused(self):
        with _Overlay({}), self.assertRaises(ConfigError):
            ovl.save(defaults={"subtitle": "x"})

    def test_save_is_refused_when_overlay_is_disabled(self):
        with patch.dict(os.environ, {ovl.ENV_OVERLAY: "off"}):
            with self.assertRaises(ConfigError):
                ovl.save(providers={"seedance-mini": {"model": "x"}})


class TestHealthBlockStaysHonest(unittest.TestCase):
    """`source`/`fallback` 是「内置精简配置在服务」的哨兵，被三处消费；
    覆盖层必须新开字段，不能混进去让那条告警条开始说谎。"""

    def test_overlay_is_a_third_field(self):
        with _Overlay({"providers": {"seedance-mini": {"model": "x"}},
                       "defaults": {"providers": {"video": "veo"}}}):
            s = _load()
            self.assertIsNone(s.fallback)                 # 仓库配置健在
            self.assertTrue(str(s.source).endswith("models.yaml"))
            self.assertEqual(s.overlay["providers"], ["seedance-mini"])
            self.assertEqual(s.overlay["defaults"], ["video"])
        with _Overlay():
            self.assertIsNone(_load().overlay)

    def test_scanner_ships_the_overlay_block(self):
        src = (REPO / "engine/kinema/studio/scanner.py").read_text(encoding="utf-8")
        self.assertIn('"overlay": getattr(store, "overlay", None)', src)


class TestAdapterCatalog(unittest.TestCase):
    def test_catalog_matches_the_adapter_registry(self):
        """目录与注册表必须锁步：加了适配器不加名字，配置中心的下拉里就会
        露出一个光秃秃的英文串。"""
        from kinema.models import _ADAPTERS
        got = {(x["capability"], x["impl"]) for x in ovl.adapter_catalog()}
        self.assertEqual(got, set(_ADAPTERS))
        self.assertEqual(set(ovl.IMPL_META), set(_ADAPTERS),
                         "IMPL_META 必须恰好覆盖 _ADAPTERS")
        for k, v in ovl.IMPL_META.items():
            self.assertTrue(v.get("label") and v.get("vendor"), k)
        # vendor 是配置中心的视觉分组依据，前端按它上底色；缺一个就有一张卡没身份
        self.assertTrue(all(x.get("vendor") for x in ovl.adapter_catalog()))


class TestProbeCostsNothing(unittest.TestCase):
    """自检必须零成本——各家没有统一的免费探活端点，随便打一个生成端点就是花钱。"""

    def test_probe_sends_no_request_at_all(self):
        """patch 必须打在**真正的出口**上。

        各适配器在自己的模块里 `from .._util import request_with_retry`，patch
        `_util` 那一份换不掉它们已绑定的引用——那样的守卫只对还没被 import 的
        适配器有效，最贵的那家（seedance）恰好漏网。改打 urlopen 这一层，
        任何形式的网络调用都跑不掉。
        """
        store = _load()
        with patch("urllib.request.urlopen") as urlopen, \
                patch("socket.socket.connect") as connect:
            for alias in sorted(store.data["providers"]):
                ovl.probe(store, alias)
            urlopen.assert_not_called()
            connect.assert_not_called()

    def test_probe_reports_each_check(self):
        store = _load()
        r = ovl.probe(store, "seedance-mini")
        names = [c["name"] for c in r["checks"]]
        for expect in ("别名已登记", "能力已声明", "适配器已实现", "状态可用",
                       "密钥可取", "适配器可实例化"):
            self.assertIn(expect, names)
        bad = ovl.probe(store, "no_such_alias_xyz")
        self.assertFalse(bad["ok"])


class TestStudioAndCliWiring(unittest.TestCase):
    """接线是源级守卫：这些地方接错了不报错，只是「网页上改了但没生效」。"""

    def _src(self, rel: str) -> str:
        return (REPO / rel).read_text(encoding="utf-8")

    def test_server_routes_are_registered(self):
        s = self._src("engine/kinema/studio/server.py")
        for p in ('path == "/api/config"', '"/api/config/set"',
                  '"/api/config/secret"', '"/api/config/test"'):
            self.assertIn(p, s, p)

    def test_provider_calling_paths_never_receive_a_stale_store(self):
        """Studio 进程内**直接调 provider** 的路径必须用当前生效的配置。

        `serve()` 持有的那份 ConfigStore 是启动时的快照、全生命周期不重载，而覆盖层
        是 load 那一刻叠上去的——把它传进去，就是「网页上刚填的密钥/端点，点复刻
        或局部改造却还发旧的」，而报错文案还指向 secrets.yaml（用户根本没碰过的文件）。

        判据做成**结构性**的而不是列举调用点：这些 action 的签名里不许有 store，
        server 层于是**没有机会**传一份过期的进来。清单随进程内直调的路径增减而
        增减——新增一条进程内直调 provider 的 action，必须同步登记进来。
        """
        import inspect
        from kinema.studio import actions
        names = ["refine_image", "voice_audition"]
        for n in names:
            fn = getattr(actions, n)
            params = list(inspect.signature(fn).parameters)
            self.assertNotIn("store", params,
                             f"{n} 不该收 store——调用方会传进启动时的快照")
        src = self._src("engine/kinema/studio/actions.py")
        for n in names:
            body = src[src.index(f"def {n}("):]
            body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
            self.assertIn("_fresh_store()", body, f"{n} 必须自己新读一份配置")
        # server 层也不该再往这些 action 里塞 store。判据按 AST 逐个调用点看**实参名**，
        # 而不是找 "ws_root, store, pid" 这个字符串——后者两头都不准：
        # · 漏判：`actions.refine_image(ws_root, store, pid=...)`、换参数序、跨行折行
        #   全都躲得过一次子串匹配，而它们同样是把过期快照递进去；
        # · 误判：**只读的 scanner 函数拿 serve 闭包里的 store 是正当的**（见
        #   test_studio_entry_uses_the_self_refreshing_store）——
        #   cmd_studio 传入的是 ConfigStore.shared（按 mtime 自失效），scanner 用它
        #   做 resolve_voice 与 effects 解析都能看见磁盘新配置；子串却把
        #   `scanner.chapter_detail(ws_root, store, pid, cid)` 一并判死——照它改掉
        #   会让 `/api/chapter` 少传 store，整条章节路由 500。
        import ast
        srv = self._src("engine/kinema/studio/server.py")
        offenders = []
        for node in ast.walk(ast.parse(srv)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "actions" and node.func.attr in names):
                continue
            passed = [a.id for a in node.args if isinstance(a, ast.Name)]
            passed += [k.value.id for k in node.keywords
                       if isinstance(k.value, ast.Name)]
            if "store" in passed:
                offenders.append(f"server.py:{node.lineno} actions.{node.func.attr}()")
        self.assertEqual(offenders, [],
                         "这些 action 收到了 serve() 的启动快照：" + ", ".join(offenders))
        # /api/overview 同样不能用启动时的快照，否则配置页改完、画风目录与配置健康块
        # 还是旧的；而它必须走 _fresh_store（那里才记着启动时的 --config）
        self.assertIn("cur = actions._fresh_store()", srv)
        self.assertIn("actions.bind_config_path(config)", srv)

    def test_cli_verbs_parse(self):
        """引擎与文档里承诺的每条 config 子命令都必须真的能解析。"""
        from kinema.cli import build_parser
        p = build_parser()
        for argv in (["config", "show"], ["config", "show", "--json"],
                     ["config", "set", "--provider", "seedance-mini", "--set", "model=x"],
                     ["config", "set", "--provider", "seedance-mini", "--reset-all"],
                     ["config", "activate", "--capability", "video", "--provider", "veo"],
                     ["config", "secret", "--env", "MINIMAX_API_KEY"],
                     ["config", "test", "--provider", "seedance-mini"]):
            with self.subTest(argv=argv):
                self.assertTrue(hasattr(p.parse_args(argv), "func"))

    def test_frontend_is_wired_and_uses_house_components(self):
        app = self._src("engine/kinema/studio_app/app.js")
        # 页面挂在 #/model（导航文案「模型」），后端动词族仍叫 config——两边不同名，
        # 所以路由名与 data-route 得逐字对齐，错一个字侧栏高亮就永远点不亮
        self.assertIn('name: "model"', app)
        self.assertIn('viewConfig', app)
        html = self._src("engine/kinema/studio_app/index.html")
        self.assertIn('data-route="model"', html)
        self.assertIn('href="#/model"', html)
        cfg = self._src("engine/kinema/studio_app/app/config.js")
        self.assertIn('api("/api/config")', cfg)
        # 站内组件纪律：原生下拉/勾选一律不用（系统绘制的控件在深色主题下一眼露馅）
        self.assertNotRegex(cfg, r"<select|type: \"checkbox\"")
        css = self._src("engine/kinema/studio_app/style.css")
        for cls in (".cfg-tile", ".cfg-pv", ".cfg-drawer", ".cfg-key",
                    ".cfg-probe-l", ".cfg-dot", ".cfg-mark", ".cfg-opt"):
            self.assertIn(cls, css, f"{cls} 有 DOM 无样式=视觉上隐形")
        # 抽屉必须是 openShell 的变体，而不是另起一套弹层机制（backdrop/Escape/
        # 退场动画只该有一份）
        self.assertIn('card: "cfg-drawer"', cfg)
        self.assertIn(".dlg:has(> .cfg-drawer)", css)

    def test_save_only_submits_what_the_user_changed(self):
        """输入框预填的是当前生效值（多半来自配置文件）。把它原样回传，等于把此刻的
        yaml 值全部冻进本机覆盖层——日后配置文件升级了模型串或调了价，这台机器还钉在
        旧值上，而界面上看不出任何异常：只改一个地址却写进去五个字段。"""
        cfg = self._src("engine/kinema/studio_app/app/config.js")
        self.assertIn("if (v !== seeded[k]) patch[k] = v;", cfg)

    def test_busy_button_restores_when_no_repaint_follows(self):
        """还原判据必须是「按钮还在不在文档里」，不能是调用方的一个开关——开关漏传
        一次就是一个永远转圈、永远点不动的死钮，且不报任何错。
        `isConnected` 为假=已被重渲替换，本就无从还原。"""
        comp = self._src("engine/kinema/studio_app/app/components.js")
        self.assertIn("if (!btn.isConnected) return;", comp)
        self.assertNotIn("restoreOnDone", comp, "还原不该退回成一个可漏传的开关")
        cfg = self._src("engine/kinema/studio_app/app/config.js")
        self.assertNotIn("restore:", cfg, "调用点不必也不该逐个声明还原")


class TestStudioEntryStore(unittest.TestCase):
    def test_studio_entry_uses_the_self_refreshing_store(self):
        """cmd_studio 必须传 ConfigStore.shared（按 mtime 自失效）——load() 的
        冻结快照钉进 serve 闭包后，运行期新增画风：建项目对话框看得见（写路径
        走 _fresh_store）、项目建得成，章节页却因冻结快照解不出 profile 整页
        500。serve 内部那条「长驻进程必须用 shared」的兜底分支因唯一调用方
        总是传 store 而恒不生效，闸必须设在调用方。"""
        import inspect

        from kinema import cli
        src = inspect.getsource(cli.cmd_studio)
        self.assertIn("ConfigStore.shared(", src)
        self.assertNotIn("ConfigStore.load(", src)


class TestGradeCatalog(unittest.TestCase):
    """画质档位目录（`providers/grades.py`）：一份事实喂适配器归一、配置中心下拉与守卫。"""

    def _video_impls(self) -> set:
        from kinema.models import _ADAPTERS
        return {impl for kind, impl in _ADAPTERS if kind == "video"}

    def test_every_video_adapter_declares_its_grades(self):
        """漏一个不报错，只是那家服务商在配置页上没有档位可选、退回什么都没有。"""
        from kinema.providers import grades
        missing = self._video_impls() - set(grades.GRADES) - {"mock"}
        self.assertEqual(missing, set(), "这些视频适配器还没登记档位")

    def test_the_field_a_spec_names_is_the_one_the_adapter_reads(self):
        """目录说某家读哪个字段，那个适配器就必须真有那个属性。

        对不上的话下拉会往一个没人读的键里写值——保存成功、界面显示已改、
        发出去的请求一个字都没变，是最难查的一类。
        """
        from kinema.models import _ADAPTERS, ConfigStore
        from kinema.providers import grades
        store = ConfigStore.load()
        for impl, spec in grades.GRADES.items():
            with self.subTest(impl=impl):
                prov = _ADAPTERS[("video", impl)]({"kind": "video", "impl": impl}, store)
                self.assertTrue(hasattr(prov, spec.field),
                                f"{impl} 没有 {spec.field} 属性")

    def test_the_field_is_writable_through_the_overlay(self):
        """下拉写回的键必须在覆盖层白名单里，否则保存那一刻后端直接拒。"""
        from kinema.providers import grades
        for impl, spec in grades.GRADES.items():
            self.assertIn(spec.field, ovl._FIELD_WHITELIST, impl)

    def test_every_grade_carries_its_provenance(self):
        """厂商档位是抄来的、会过期的事实——没有出处的不许进表。"""
        from kinema.providers import grades
        for impl, spec in grades.GRADES.items():
            for g in spec.grades:
                self.assertTrue(g.source.strip(), f"{impl}/{g.value} 缺出处")
                self.assertTrue(g.label.strip(), f"{impl}/{g.value} 缺显示名")

    def test_shipped_config_never_names_a_grade_outside_its_own_catalog(self):
        """随包配置写的档位必须是自己目录里的。

        **两份配置真源都要查**：`config/models.yaml` 与 `models.EMBEDDED_DEFAULTS`
        （缺 PyYAML 时走的是后者），只查一份等于漏掉整条回退路径。
        """
        from kinema.models import EMBEDDED_DEFAULTS, ConfigStore
        from kinema.providers import grades
        for label, data in (("models.yaml", ConfigStore.load().data),
                            ("EMBEDDED_DEFAULTS", EMBEDDED_DEFAULTS)):
            for alias, conn in (data.get("providers") or {}).items():
                spec = grades.spec_for(conn.get("impl", alias))
                if spec is None or not conn.get(spec.field):
                    continue
                with self.subTest(where=label, alias=alias):
                    self.assertIn(conn[spec.field], grades.values_of(conn.get("impl", alias)))

    def test_the_catalog_only_informs_and_never_rejects(self):
        """目录是说明不是闸。**它一旦能拒，厂商开了新档我们这儿就发不出去**——
        而档位又与计费、时长联动（Veo 非 720p 强制 8 秒计费），本地拒绝的代价是
        把一个今天能跑的调用变成跑不了。裁决权留在服务端。"""
        src = (REPO / "engine/kinema/providers/grades.py").read_text(encoding="utf-8")
        self.assertNotIn("raise", src)
        cli = (REPO / "engine/kinema/cli.py").read_text(encoding="utf-8")
        # 禁的是「拿目录去拦」这个用法，不是「出现 grades 这七个字母」——
        # 整文件禁词会被一句带 upgrades 的注释打红，也表达不出真正的命题
        for use in ("grades.values_of", "grades.spec_for", "grades.GRADES",
                    "from .providers import grades"):
            self.assertNotIn(use, cli, "CLI 不该按本机档位表裁决生成")
        # 显式点名恒赢：`--resolution` 仍是一句裸赋值
        self.assertIn("prov.resolution = resolution", cli)

    def test_h3_normalisation_reads_the_catalog_rather_than_its_own_copy(self):
        """H3 是唯一一个发前必须归一的适配器（档位名与别家完全不同，不归一必被拒）。
        它的白名单必须与下拉同源，否则界面上选得出的档发出去会被自己归一掉。"""
        src = (REPO / "engine/kinema/providers/video/minimax.py").read_text(
            encoding="utf-8")
        self.assertIn('RESOLUTIONS = grades.values_of("minimax_video")', src)
        self.assertNotIn('RESOLUTIONS = ("', src)

    def test_a_value_outside_the_catalog_is_kept_not_rewritten(self):
        """厂商刚开的新档、或这台机器手填的值：界面要如实呈现，不许悄悄换掉。"""
        view = ovl._grade_view({"kind": "video", "impl": "seedance",
                                "resolution": "8k"}, "seedance")
        self.assertEqual(view["current"], "8k")
        self.assertFalse(view["in_catalog"])
        # 没配过的（跟随配置文件）不算「目录外」，否则每张卡都会挂一句告警
        self.assertTrue(ovl._grade_view({"kind": "video", "impl": "seedance"},
                                        "seedance")["in_catalog"])

    def test_the_page_asks_the_backend_which_field_this_provider_uses(self):
        """判据必须是「后端给没给档位块」。

        写死「kind===video 就显示分辨率」是错的——各家的字段名并不保证都叫
        resolution，读别的字段的服务商会显示一格它根本不读的输入框。
        """
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        block = re.search(r"const FIELDS = \[(.*?)\];", cfg, re.S)
        self.assertIsNotNone(block, "找不到 FIELDS 表")
        keys = re.findall(r"""\[\s*["']([a-z_]+)["']""", block.group(1))
        self.assertEqual(keys, ["base_url", "model"],
                         "档位不该回到这张按能力硬猜的固定表里")
        self.assertIn("if (p.grade) conn.push(gradeRow(", cfg)

    def test_every_price_field_flows_from_backend_to_the_form(self):
        """白名单里的每个 price_per_* 都要走完「provider_view 下发 → 前端 PRICE
        表单」两段。少一段的表现是该计费维度在页面上不显示也改不了，
        而其余字段保存一切正常，极难察觉。"""
        price_keys = {k for k in ovl._FIELD_WHITELIST if k.startswith("price_per_")}

        class _S:
            data = {"providers": {"x": {
                "kind": "music", "impl": "minimax_music",
                **{k: 1.0 for k in price_keys}}}}
            secrets = {}
            source = "test"
        rows = ovl.provider_view(_S())
        self.assertEqual(set(rows[0]["price"]), price_keys,
                         "provider_view 的下发键集少了计费维度")
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        block = re.search(r"const PRICE = \[(.*?)\];", cfg, re.S)
        self.assertIsNotNone(block, "找不到 PRICE 表")
        front = set(re.findall(r"""["'](price_per_[a-z0-9_]+)["']""", block.group(1)))
        self.assertEqual(front, price_keys, "前端 PRICE 表与白名单计费维度不齐")

    def test_the_drawer_listens_to_both_control_events(self):
        """输入框发 input、`uiSelect` 发 change。只听一种，另一种控件改了之后
        保存钮永远是灰的——功能看起来完全没做。取值也必须走同一个口径：
        uiSelect 的 value 是裸值、无匹配项时是 undefined，直接 `.trim()` 会炸。"""
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        self.assertIn('el.addEventListener("change", sync)', cfg)
        # 预填基线 / 脏判定 / 提交这三处必须是同一个口径，差一处就是
        # 「按钮亮了但提交的是旧值」或「改了却判定没改」
        self.assertIn("seeded[k] = val(el)", cfg)
        self.assertIn("val(el) !== seeded[k]", cfg)
        self.assertIn("const v = val(el);", cfg)
        # 定义本身也要钉：只钉调用点的话，那句空值防护整段删掉照样全绿
        self.assertRegex(cfg, r"const val = \(el\) => String\(el\.value == null")


class TestGradeCatalogWiring(unittest.TestCase):
    """目录之外的三处约定：后端真的下发、承重的那一份被钉死、提示不被状态吞掉。"""

    def test_the_backend_actually_ships_the_block(self):
        """前端那半（`if (p.grade)`）与后端这半是同一份约定——只钉一头，
        另一头改坏时是「界面上那一格整个消失」而不是报错。"""
        from kinema.providers import grades
        rows = ovl.provider_view(_load())
        for r in rows:
            spec = grades.spec_for(r["impl"])
            with self.subTest(alias=r["alias"]):
                if spec is None:
                    self.assertIsNone(r["grade"])
                else:
                    self.assertEqual(r["grade"]["field"], spec.field)
                    self.assertEqual([o["value"] for o in r["grade"]["options"]],
                                     list(grades.values_of(r["impl"])))

    def test_the_h3_grade_set_is_load_bearing(self):
        """H3 是唯一一家目录**真的会裁决**的：它的发前归一白名单派生自这里，
        删一档等于把用户配好的值静默改写成 768P。官方枚举只有这两个，逐字钉死。"""
        from kinema.providers import grades
        self.assertEqual(grades.values_of("minimax_video"), ("768P", "2K"))

    def test_case_folding_follows_what_the_adapter_itself_does(self):
        """只有自己会归一大小写的适配器才准折叠比对。**全局折叠是错的**——
        seedance 与 veo 把值原样发出去，对它们来说 `720P` 确实不是合法档。"""
        from kinema.providers import grades
        self.assertTrue(grades.spec_for("minimax_video").matches("768p"))
        self.assertFalse(grades.spec_for("seedance").matches("720P"))
        for impl, spec in grades.GRADES.items():
            if not spec.fold:
                continue
            src = (REPO / "engine/kinema/providers/video/minimax.py").read_text(
                encoding="utf-8")
            self.assertIn(".upper()", src, f"{impl} 声明了 fold 却没有归一动作")

    def test_a_local_override_never_swallows_the_caveat(self):
        """「本机已改」是状态、档位提醒是内容，同一个槽位只能拼不能抢——
        让状态短路掉内容，被改过的档位上那句警告即不可见。"""
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        self.assertIn("overridden ? `本机已改 · ${tip}` : tip", cfg)

    def test_sections_with_a_varying_field_count_size_themselves(self):
        """计费的字段数各家不同（视频两条、图像/配音各一条）。固定两栏时单条会占半格、
        右边空着一大片——而那一格本来就不存在，不应渲染空位。"""
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        css = (REPO / "engine/kinema/studio_app/style.css").read_text(
            encoding="utf-8")
        self.assertIn("`--cols:${Math.min(prices.length, 3)}`", cfg)
        self.assertIn(".cfg-form.cfg-cols { grid-template-columns: "
                      "repeat(var(--cols, 2), minmax(0, 1fr)); }", css)
        # 窄屏必须一并降成单栏：`.cfg-form.cfg-cols` 比 `.cfg-form` 更具体，
        # 只改后者的话断点在这一段上不生效
        self.assertIn(".cfg-form, .cfg-form.cfg-cols { grid-template-columns: "
                      "minmax(0, 1fr); }", css)

    def test_the_new_alias_form_agrees_with_the_naming_convention(self):
        """网页建别名的正则必须与命名规范守卫同一份，否则页面能建出一个
        `test_config_drift` 判为非法的别名。"""
        from tests.test_config_drift import TestProviderNamingConvention as N
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        self.assertIn(N.ALIAS_RE.pattern, cfg)
        self.assertNotIn("小写字母/数字/下划线", cfg)


class TestVendorBrandMarks(unittest.TestCase):
    """品牌标是「后端出 vendor、前端出图形」的两地约定，只有源级守卫看得住。"""

    def _brands(self) -> str:
        return (REPO / "engine/kinema/studio_app/app/brands.js").read_text(
            encoding="utf-8")

    def test_every_vendor_has_a_mark(self):
        """`IMPL_META` 里出现过的每个 vendor 都必须有品牌标。

        少一个不会报错，只是那张卡悄悄退回首字母——而首字母恰恰是这次要换掉的东西。
        """
        src = self._brands()
        keys = set(re.findall(r'^  "([^"]+)":', src, re.M))
        vendors = {m["vendor"] for m in ovl.IMPL_META.values() if m.get("vendor")}
        self.assertEqual(vendors - keys, set(), "这些厂商还没有品牌标")

    def test_marks_declare_where_they_came_from(self):
        """商标资产不写出处与许可，日后没人说得清能不能用。"""
        src = self._brands()
        for token in ("Simple Icons", "CC0-1.0", "商标"):
            self.assertIn(token, src, token)

    def test_every_mark_carries_a_colour_and_artwork(self):
        src = self._brands()
        blocks = re.findall(r'^  "[^"]+": \{(.*?)\n', src, re.M | re.S)
        self.assertTrue(blocks)
        for b in blocks:
            self.assertRegex(b, r'title: "', b[:60])
            self.assertRegex(b, r'color: "#[0-9A-Fa-f]{6}"', b[:60])

    def test_letters_survive_only_as_a_fallback(self):
        """自定义接入的别名可能是任何厂商——认不出时留首字母，绝不编一枚假标。"""
        self.assertIn("if (!b) return", self._brands())

    def test_config_page_never_hand_rolls_a_mark(self):
        """标必须由 brands.js 统一分派。config.js 自己拼一枚，就会漏掉品牌色，
        或者在只该出现小标的地方铺出大标。"""
        cfg = (REPO / "engine/kinema/studio_app/app/config.js").read_text(
            encoding="utf-8")
        self.assertIn('import { vendorGlyph, vendorMark } from "./brands.js";', cfg)
        self.assertNotIn("cfg-mark brand", cfg)


class TestCapabilityCatalog(unittest.TestCase):
    """能力清单与名称只有 `CAPABILITY_META` 一处声明：网页路由牌、筛选钮、自定义接入的
    能力下拉与 CLI `config show` 都从下发的 `capabilities[]` 取 id 与名称。前端不再
    按 id 另存名称表——那张表少一项时 `#/model` 不是少一张牌，而是整页炸掉。"""

    def test_view_ships_id_and_names_per_capability(self):
        caps = ovl.config_view(_load())["capabilities"]
        self.assertEqual([c["id"] for c in caps], list(ovl.CAPABILITIES))
        for c in caps:
            self.assertTrue(c["zh"] and c["en"], c)

    def test_frontend_keeps_no_capability_name_table(self):
        src = (REPO / "engine/kinema/studio_app/app/config.js").read_text(encoding="utf-8")
        self.assertNotIn("CAP[", src, "能力名称按 id 在前端另存了一份")
        # 图形是前端自己的设计资产，按 id 配；缺图只是无图不影响出牌，但清单里的能力都该有图
        block = src.split("const GLYPH = {", 1)[1].split("\n};", 1)[0]
        self.assertEqual(set(re.findall(r"^  (\w+): '<svg ", block, re.M)),
                         set(ovl.CAPABILITIES))


class TestMultiSlotKeyProviders(unittest.TestCase):
    """没有 `api_key_env` 的多凭证服务商（火山视觉 AK/SK）：主密钥位取第一把，
    否则 key.state 恒为 none，缺密钥的卡片显示成「免密钥 · 就绪」。"""

    def test_primary_key_slot_falls_back_to_the_first_declared_env(self):
        row = next(p for p in ovl.provider_view(_load()) if p["alias"] == "volc-lipsync")
        self.assertEqual([k["env"] for k in row["keys"]],
                         ["VOLC_ACCESS_KEY", "VOLC_SECRET_KEY"])
        self.assertEqual(row["key"]["env"], "VOLC_ACCESS_KEY")
        self.assertTrue(row["key"]["optional"], "增强步缺密钥是降级不是故障")
        self.assertTrue(row["key"]["degrade"])

    def test_probe_accepts_a_signed_host_as_endpoint(self):
        """签名式接口没有 base_url，端点检按连接段声明的 host 过。"""
        r = ovl.probe(_load(), "volc-lipsync")
        by = {c["name"]: c for c in r["checks"]}
        self.assertTrue(by["端点已填"]["ok"], by["端点已填"])
        self.assertIn("visual.volcengineapi.com", by["端点已填"]["detail"])
        # 适配器自报的真发条件进自检：缺 req_key 要点名，但有降级分支不标红
        self.assertIn("适配器就绪", by)
        self.assertTrue(by["适配器就绪"]["ok"])

    def test_probe_names_the_missing_req_key(self):
        """req_key 只在官方接口文档给出、按机器手填——自检不能对它一无所知。
        用受控连接段而不是本机 models.yaml：开发机填过 req_key 不该让守卫翻红。"""
        store = _load()
        store.data["providers"]["volc-lipsync"].pop("req_key", None)
        r = ovl.probe(store, "volc-lipsync")
        by = {c["name"]: c for c in r["checks"]}
        self.assertIn("req_key", by["适配器就绪"]["detail"])
        self.assertTrue(by["适配器就绪"]["ok"], "有降级分支的增强步缺配置不标红")


class TestSchemaAndDocs(unittest.TestCase):
    def test_setting_table_is_declared(self):
        """新表由 `CREATE TABLE IF NOT EXISTS` 自动建，**不需要**动 `_MIGRATE_COLUMNS`
        （那张表只管给已存在的表加列）。但表本身必须在 _SCHEMA 里，否则存量库没有它。"""
        from kinema.storage import mysql
        self.assertIn("CREATE TABLE IF NOT EXISTS {p}setting", mysql._SCHEMA)
        block = mysql._SCHEMA.split("CREATE TABLE IF NOT EXISTS {p}setting", 1)[1]
        self.assertIn("UNIQUE KEY uk_setting_scope_name (scope, name)", block)
        # 表注释必须把「密钥不入库」写出来——它是这张表的使用前提
        self.assertIn("密钥", block)

    def test_local_storage_treats_the_file_as_the_truth(self):
        """local 后端两个方法都是空操作：本地 JSON 文件就是运行时真源，
        不该再有第二份副本。"""
        from kinema.storage.local import LocalStorage
        st = LocalStorage(Path(tempfile.gettempdir()))
        self.assertIsNone(st.load_settings("models", "overlay"))
        self.assertIsNone(st.save_settings("models", "overlay", {"a": 1}))


class TestSetupJsonContract(unittest.TestCase):
    """`setup --json` 是「绿灯不重复引导配置」纪律的机器可读面（SETUP.md 契约）：
    ①纯 JSON 独占 stdout（检查过程的人读杂音必须静音，混一行就撕破解析面）
    ②ready 与退出码一致 ③密钥只回状态枚举、字段集固定——永不回值。"""

    def test_json_is_pure_and_ready_matches_exit_code(self):
        import contextlib
        import io
        from kinema.cli import build_parser
        ns = build_parser().parse_args(["setup", "--json"])  # --json 隐含 --check
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                ns.func(ns)
            except SystemExit as e:
                code = int(e.code or 0)
        payload = json.loads(buf.getvalue())  # 混进任何人读行，这里当场炸
        self.assertIsInstance(payload["ready"], bool)
        self.assertEqual(payload["ready"], code == 0, "ready 必须与退出码一致")
        self.assertTrue(payload["checks"], "checks 不该为空")
        for c in payload["checks"]:
            self.assertEqual(sorted(c), ["detail", "name", "ok"])
        self.assertTrue(payload["keys"], "keys 不该为空")
        for k in payload["keys"]:
            self.assertEqual(sorted(k), ["key", "label", "state", "state_zh"],
                             "密钥字段集固定——多出的字段可能是值泄漏")
            self.assertIn(k["state"], ("env", "local", "file", "none", "unset"))
        route = payload.get("image_route")   # 生图三级路由：agent 判就绪要看它
        if route is not None:
            self.assertEqual(sorted(route), ["provider", "source"])
            self.assertIn(route["source"], ("explicit", "agent", "default"))


class TestSecretFileSingleReadPath(unittest.TestCase):
    """密钥文件只许经 `config_overlay.file_secrets` 读——不得存在第二个读取口。

    若 `storage.load_storage_config` 的 MySQL 密码与 `storage.media._media_config`
    的 OSS AK/SK 各自 `_read_yaml(secrets.yaml)` 就地读，**向导/网页/`config secret`
    写的 `secrets.local.json` 就整份被跳过**——用户在网页上填好 OSS key，上传照样报
    「缺少密钥」；就地读还会把 PyYAML 这个可选依赖变成密钥能否读到的开关
    （`storage._read_yaml` 缺包直接返 `{}`）。env>local>yaml 的覆盖守卫只盯
    `ConfigStore` 那条路，看不见 storage 这两条，所以本类单独钉源级读取口。
    """

    ENGINE = Path(__file__).resolve().parent.parent / "kinema"

    # 本类会调 load_storage_config：开发机 shell 常年固化 KINEMA_STORAGE_BACKEND=mysql，
    # 不钉住就会拿真库配置来解析（AGENTS.md 的纪律，test_workspace 有守卫盯着）。
    def setUp(self):
        self._backend = LocalBackendEnv()
        self._backend.enable()

    def tearDown(self):
        self._backend.restore()

    def test_no_module_reads_the_secrets_file_on_its_own(self):
        """源级：除 `config_overlay` 外，谁都不许把 secrets.yaml 直接交给读文件调用。"""
        readers = re.compile(r"(_read_yaml|safe_load|read_text|open)\s*\(")
        offenders = []
        for py in sorted(self.ENGINE.rglob("*.py")):
            if py.name == "config_overlay.py":      # 它就是那个唯一读取口
                continue
            for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if "secrets.yaml" in line and readers.search(line):
                    offenders.append(f"{py.relative_to(self.ENGINE)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "密钥文件只许经 config_overlay.file_secrets 读："
                                        "就地读会漏掉 secrets.local.json")

    def _storage_dir(self, tmp: Path, *, storage_yaml: str) -> Path:
        cfg = tmp / "config"
        cfg.mkdir()
        (cfg / "models.yaml").write_text("defaults: {}\n", encoding="utf-8")
        (cfg / "storage.yaml").write_text(storage_yaml, encoding="utf-8")
        return cfg

    def test_mysql_password_and_oss_keys_see_the_local_secrets_file(self):
        """向导落点写的密钥，storage 两条路都必须读得到，且 env > local > yaml。"""
        import kinema.storage as st
        import kinema.storage.media as md
        with tempfile.TemporaryDirectory() as td, \
                _Overlay({}, secrets={"KINEMA_MYSQL_PASSWORD": "pw-local",
                                      "KINEMA_OSS_ACCESS_KEY": "ak-local",
                                      "KINEMA_OSS_SECRET_KEY": "sk-local"}):
            tmp = Path(td)
            cfg = self._storage_dir(tmp, storage_yaml=(
                "backend: mysql\nmysql: {host: h, user: u, database: d}\n"
                "media: {backend: oss, bucket: b, region: r}\n"))
            keep = os.getcwd()
            try:
                os.chdir(td)
                # KINEMA_STORAGE_BACKEND 由 LocalBackendEnv 钉着，这里不碰；
                # 密码/AK/SK 的解析与当前生效后端无关，照样能验。
                with patch.dict(os.environ):     # 整份快照，退出即还原
                    for k in ("KINEMA_MYSQL_PASSWORD", "KINEMA_OSS_ACCESS_KEY",
                              "KINEMA_OSS_SECRET_KEY", "KINEMA_MEDIA_BACKEND"):
                        os.environ.pop(k, None)
                    self.assertEqual(
                        st.load_storage_config(reload=True)["mysql"].get("password"),
                        "pw-local", "MySQL 密码漏读了 secrets.local.json")
                    media = md._media_config()
                    self.assertEqual((media.get("ak"), media.get("sk")),
                                     ("ak-local", "sk-local"),
                                     "OSS AK/SK 漏读了 secrets.local.json")
                    # yaml 同名键存在时，本机那份仍要压过它（网页填的更近）
                    (cfg / "secrets.yaml").write_text(
                        'KINEMA_MYSQL_PASSWORD: "pw-yaml"\n', encoding="utf-8")
                    self.assertEqual(
                        st.load_storage_config(reload=True)["mysql"].get("password"),
                        "pw-local", "local.json 必须压过 secrets.yaml")
                    # 环境变量恒在最前
                    os.environ["KINEMA_MYSQL_PASSWORD"] = "pw-env"
                    self.assertEqual(
                        st.load_storage_config(reload=True)["mysql"].get("password"),
                        "pw-env", "export 必须一定生效")
                    os.environ.pop("KINEMA_MYSQL_PASSWORD")
                    # 只有 yaml 时照样读到（回归：别把老路径修没了）
                    (cfg.parent / "config" / ovl.SECRETS_FILE).unlink(missing_ok=True)
                    with _Overlay({}):
                        self.assertEqual(
                            st.load_storage_config(reload=True)["mysql"].get("password"),
                            "pw-yaml", "仅 secrets.yaml 时必须仍读得到")
            finally:
                os.chdir(keep)
                st.load_storage_config(reload=True)


class TestSecretsTemplateIsAutoProvisioned(unittest.TestCase):
    """`secrets.yaml` 是 gitignore 的，全新 clone 一定没有——引擎自己补上，不让用户手抄。

    手抄那份漏 key、没注释、`KEY: "值"` 格式还容易写错，随后表现成
    「我明明填了却读不到」。模板是仓库跟踪的且每个值都是空串，复制零泄漏风险。
    """

    def test_created_from_template_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config"
            cfg.mkdir()
            (cfg / "secrets.example.yaml").write_text(
                '# 注释要一起带过来\nARK_API_KEY: ""\nWEREAD_API_KEY: ""\n', encoding="utf-8")

            born = ovl.ensure_secrets_yaml(cfg)
            self.assertEqual(born, cfg / "secrets.yaml")
            body = (cfg / "secrets.yaml").read_text(encoding="utf-8")
            self.assertIn("# 注释要一起带过来", body, "注释是填 key 的说明书，必须原样带过来")
            self.assertEqual(ovl.read_yaml_secrets_flat(cfg / "secrets.yaml"), {},
                             "模板必须全空——复制出来的文件里不能有任何值")

            # 幂等：已存在就原样不动，绝不覆盖用户填好的 key
            (cfg / "secrets.yaml").write_text('ARK_API_KEY: "user-filled"\n', encoding="utf-8")
            self.assertIsNone(ovl.ensure_secrets_yaml(cfg))
            self.assertEqual(ovl.read_yaml_secrets_flat(cfg / "secrets.yaml"),
                             {"ARK_API_KEY": "user-filled"}, "绝不覆盖用户已填的值")

    def test_missing_template_is_not_an_error(self):
        """源码包被裁剪过（没带 example）不该挡住整条 setup。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config"
            cfg.mkdir()
            self.assertIsNone(ovl.ensure_secrets_yaml(cfg))
            self.assertFalse((cfg / "secrets.yaml").exists())

    def test_shipped_template_carries_no_real_values(self):
        """随包模板必须全空——哪天有人往 example 里填了真 key，这里当场红。"""
        tpl = REPO / "config" / "secrets.example.yaml"
        self.assertTrue(tpl.is_file(), "随包密钥模板不能缺，setup 靠它生成 secrets.yaml")
        self.assertEqual(ovl.read_yaml_secrets_flat(tpl), {},
                         "secrets.example.yaml 是入库文件，任何非空值都是泄漏")


if __name__ == "__main__":
    unittest.main()
