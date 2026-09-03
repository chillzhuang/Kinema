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

"""模型配置覆盖层：网页与 CLI 填的连接段和激活项，凌驾于 config/models.yaml 之上。

**为什么挂在 ConfigStore 而不是 ModelRouter**：有六处直读 `store.data` 绕过路由——
音效生成直接建适配器、doctor 的 ready/planned 分组、
overview 的画风目录与画布。挂在路由上，这些会继续用 yaml 旧值，表现为「网页改了
端点，生图走新的、音效走旧的」这类最难查的分裂；挂在 ConfigStore 则全部白拿，
下游一行不改。

**为什么两份文件分家**：
  · `models.local.json`  连接段与激活项 —— 可入库，跨机同步靠它
  · `secrets.local.json` 密钥 —— 永不入库、永不下发、永不打印
分开之后「要上行入库的那份」整份可传，不需要任何「记得剔除某字段」的过滤逻辑；
那种过滤只要漏一次，密钥就会进入数据库文本列并随备份与多机同步扩散。

**为什么是 stdlib json 而不是 yaml**：`ConfigStore` 读 secrets.yaml 要 `import yaml`，
而 PyYAML 只是可选附加依赖；缺它时密钥文件整份读不到。密钥再撞上这个易错点，表现就是
「网页上明明填了 key，跑起来说缺密钥」。json 属标准库，任何环境都读得到。

**`secrets.local.json` 与 `secrets.yaml` 刻意不同步——看到两份不一致不要去"修"它**：
两者是有序的两层而不是两份副本（`secrets.yaml < secrets.local.json`，本机/网页填的更近），
`file_secrets` 在**读取时**合并，所以在哪一层填都生效，本就不需要一致。反过来加同步
会把优先级倒过来：yaml 一被编辑就回写 local.json，用户在网页填的 key 被 yaml 里的旧值
或空值静默冲掉，正是「我明明在网页填了却不生效」。同理也别合并成一份——分层是
上一条入库边界的前提。

模块内**绝不在顶层 import models**——models 在自己的加载路径上引用本模块，
顶层互引即环。需要时在函数内 import。
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets as _secrets
from pathlib import Path

from .errors import ConfigError
from .providers import grades

_rand = lambda: _secrets.token_hex(4)  # noqa: E731

OVERLAY_FILE = "models.local.json"
SECRETS_FILE = "secrets.local.json"

# 显式禁用哨兵：`KINEMA_CONFIG_OVERLAY` 取这些值 = 关掉覆盖层。
# **不能靠"空串是 falsy 所以跳过"**——那样只是落到下一级发现顺序，开发机上真实
# 存在的 models.local.json 照样会被读进去，测试于是随"这台机器配过什么"而变。
_OFF = {"", "0", "off", "no", "none", "false"}
ENV_OVERLAY = "KINEMA_CONFIG_OVERLAY"

# 覆盖层只准动这两个顶层键。**profiles / canvas / voices 刻意不可覆盖**：
# providers 段管「用哪家模型」，profiles 段管「什么美术风格」，是两件事；且画风侧有
# label 必填 / style_prefix_en 成对 / skills.py 恰好覆盖全部画风三条漂移守卫，
# 它们直读 models.yaml 文件、压根不经 ConfigStore——覆盖层若能改 profiles，
# 就是一条守卫完全看不见的后门。
MERGE_KEYS = ("providers", "defaults")

# 连接段可写字段白名单。拦在写入口比拦在 gitignore 有效得多——它在源头就不让
# 密钥值落进这份「可入库」的文件。
_FIELD_WHITELIST = frozenset({
    "kind", "impl", "status", "base_url", "model", "resource_id",
    "resolution", "prompt_lang", "voice", "format", "sample_rate",
    "image_size", "output_format",
    "price_per_image", "price_per_image_hd", "hd_pixels",
    "price_per_second", "price_per_second_4k",
    "price_per_kchar", "price_per_min", "price_per_track",
    # 本地自托管端点用：auth=none 时适配器不发鉴权头；轮询参数按机器性能调
    "auth", "poll_interval", "timeout",
})
# 名字里带这些词却不是 `*_env` 变量名引用的键 = 有人想把密钥本体写进来
_SECRETISH = re.compile(r"(key|secret|token|password|passwd|credential)", re.I)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
# 数值字段必须落成数值。网页表单交上来的一律是字符串，原样存进去的话
# `price_per_second: "0.5"` 会进成本台账与预算闸参与算术——那是一条静默错账的路。
_FLOAT_FIELDS = frozenset({"price_per_image", "price_per_image_hd", "price_per_second",
                           "price_per_second_4k", "price_per_kchar", "price_per_min",
                           "price_per_track"})
_INT_FIELDS = frozenset({"sample_rate", "hd_pixels"})

# impl → 展示元数据。`_ADAPTERS` 的键是技术标识，界面上给人看的名字与服务商归属在这里。
# 服务商是配置中心的**视觉分组依据**（同一家的几个别名共用一块底色与首字标），
# 放这里而不是让前端从名字里猜——猜法会在加别名时静默错位。
# 守卫钉住它恰好覆盖 `_ADAPTERS` 的全部 impl：加了适配器不加名字，界面上就会
# 露出一个光秃秃的英文串。
# console：该服务商创建密钥的控制台入口（界面上的「打开控制台」跳转与密钥弹窗
# 引导共用）。按 impl 而不是按 vendor 存——同是火山，方舟（图/视频）与语音（TTS）
# 是两个控制台，按 vendor 归并无法区分两者、链接会指错。守卫要求凡声明了密钥变量的
# provider 必须给出此项（test_config_center）。
_CONSOLE_ARK = "https://console.volcengine.com/ark"
_CONSOLE_SPEECH = "https://console.volcengine.com/speech"
_CONSOLE_MINIMAX = "https://platform.minimax.io/user-center/basic-information/interface-key"
_CONSOLE_AISTUDIO = "https://aistudio.google.com/apikey"
IMPL_META = {
    # no_endpoint + optional_key：agent 原生生图不发网络请求——工单模式，引擎
    # 开单、驱动引擎的 AI 产图（models.image_route ②级），密钥与端点皆无需，
    # probe 把这两项标红会把刻意的设计说成故障。
    ("image", "agent"): {"label": "Agent 原生生图 · 工单", "vendor": "本地",
                         "optional_key": True, "no_endpoint": True},
    ("image", "minimax_image"): {"label": "image-01", "vendor": "MiniMax",
                                 "console": _CONSOLE_MINIMAX},
    ("video", "minimax_video"): {"label": "H3 · 全模态", "vendor": "MiniMax",
                                 "console": _CONSOLE_MINIMAX},
    ("music", "minimax_music"): {"label": "音乐生成", "vendor": "MiniMax",
                                 "console": _CONSOLE_MINIMAX},
    ("image", "seedream"): {"label": "Seedream", "vendor": "火山引擎",
                            "console": _CONSOLE_ARK},
    ("image", "nano_banana"): {"label": "Nano Banana", "vendor": "Google",
                               "console": _CONSOLE_AISTUDIO},
    ("image", "wan"): {"label": "通义万相", "vendor": "阿里云",
                       "console": "https://bailian.console.aliyun.com"},
    ("video", "seedance"): {"label": "Seedance", "vendor": "火山引擎",
                            "console": _CONSOLE_ARK},
    ("video", "veo"): {"label": "Veo", "vendor": "Google",
                       "console": _CONSOLE_AISTUDIO},
    ("tts", "seedtts"): {"label": "seed-tts · 固定音色", "vendor": "火山引擎",
                         "console": _CONSOLE_SPEECH},
    ("tts", "doubao"): {"label": "seed-audio · 生成式", "vendor": "火山引擎",
                        "console": _CONSOLE_SPEECH},
    ("tts", "minimax"): {"label": "语音合成", "vendor": "MiniMax",
                         "console": _CONSOLE_MINIMAX},
    # optional_key：缺密钥不是错误状态。工厂层对它有降级分支（无 key 直接换成本地
    # 曲库），界面上把它标红会把一个刻意的设计说成故障。
    ("music", "elevenlabs"): {"label": "音乐生成", "vendor": "ElevenLabs",
                              "optional_key": True,
                              "console": "https://elevenlabs.io/app/settings/api-keys",
                              "degrade": "未配密钥时自动改用本地免版权曲库（零成本）"},
    ("music", "local"): {"label": "免版权曲库", "vendor": "本地"},
    # optional_key：口型精修是 dubbed 的增强步，未配置时 stage 点名跳过、对白镜按
    # 底片口型出片（docs/agents/lipsync.md）；缺密钥标红会把增强步的缺席报成故障。
    # console 指向 IAM 密钥管理：鉴权用视觉服务 AK/SK，不是方舟的 API Key。
    ("lipsync", "volc_lipsync"): {"label": "视频改口型", "vendor": "火山引擎",
                                  "optional_key": True,
                                  "console": "https://console.volcengine.com/iam/keymanage/",
                                  "degrade": "未配置时口型精修点名跳过，对白镜按底片口型出片（零成本）"},
}

# 能力清单的唯一声明：id、中文名与英文角标。网页路由牌、筛选钮、自定义接入的
# 能力下拉与 CLI `config show` 都从这里取名，前端不另存一份按 id 索引的名称表。
# 顺序即呈现顺序。
CAPABILITY_META = {
    "image": {"zh": "生图", "en": "IMAGE"},
    "video": {"zh": "生视频", "en": "VIDEO"},
    "tts": {"zh": "生配音", "en": "VOICE"},
    "music": {"zh": "背景音乐", "en": "MUSIC"},
    "lipsync": {"zh": "口型精修", "en": "LIPSYNC"},
}
CAPABILITIES = tuple(CAPABILITY_META)


# ---------------------------------------------------------------------------
# 落点发现
# ---------------------------------------------------------------------------
def config_dir(explicit: str | None = None) -> Path:
    """覆盖层与密钥文件的落点 = models.yaml 所在目录。

    **跟随 `--config`**：显式指定了另一份 models.yaml 时，覆盖层与密钥必须落在
    它旁边——否则「读的是这份配置、覆盖的是另一份配置旁边那层」，两边静默错配。
    找不到 models.yaml 时退回「向上找到的 config/」，再退回 ~/.config/kinema。
    """
    from .models import _find_models_file
    p = _find_models_file(explicit)
    if p is not None:
        return p.parent
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for d in [start, *start.parents]:
            if (d / "config").is_dir():
                return d / "config"
    return Path.home() / ".config" / "kinema"


def _env_target() -> Path | None:
    """`KINEMA_CONFIG_OVERLAY` 指定的覆盖层文件路径；显式禁用时返回 None。"""
    raw = os.environ.get(ENV_OVERLAY)
    if raw is None:
        return None
    if raw.strip().lower() in _OFF:
        return None
    return Path(raw).expanduser()


def disabled() -> bool:
    """覆盖层是否被显式关掉（测试与「只想跑仓库配置」时用）。"""
    raw = os.environ.get(ENV_OVERLAY)
    return raw is not None and raw.strip().lower() in _OFF


def overlay_path(explicit: str | None = None) -> Path | None:
    """覆盖层文件的目标路径（**可能尚不存在**，写入方按需创建）。禁用时 None。"""
    if disabled():
        return None
    return _env_target() or (config_dir(explicit) / OVERLAY_FILE)


def secrets_path(explicit: str | None = None) -> Path | None:
    """本机密钥文件路径。跟随覆盖层的开关——测试关掉覆盖层时密钥也一并不读，
    否则本机真实密钥会渗进用例。"""
    if disabled():
        return None
    env = _env_target()
    return (env.parent / SECRETS_FILE) if env else (config_dir(explicit) / SECRETS_FILE)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _read_json(path: Path | None, *, what: str) -> dict:
    """读一份 JSON 配置。**任何异常都不抛**——覆盖层坏掉时正确的行为是
    退回 models.yaml 继续工作并喊一声，而不是让整个引擎起不来。"""
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  ⚠ {what} {path} 读取失败（{e}），本次按未配置处理、回落 config/models.yaml")
        return {}
    if not isinstance(data, dict):
        print(f"  ⚠ {what} {path} 顶层不是对象，已忽略")
        return {}
    return data


def _dict(v, *, what: str) -> dict:
    """取一个必须是对象的子块。类型不对就当没有并喊一声。

    「覆盖层坏掉 = 回落配置文件 + 喊一声」这条契约必须在**嵌套层**也成立：
    只在顶层做体检的话，`providers` 写成一个字符串就会在合并时抛
    AttributeError，使引擎与 Studio 同时不可用，而产品内没有恢复入口。
    """
    if v is None:
        return {}
    if not isinstance(v, dict):
        print(f"  ⚠ 模型配置覆盖层的 {what} 不是对象（{type(v).__name__}），已忽略该块")
        return {}
    return v


def read(path: Path | None = None, *, explicit: str | None = None) -> dict:
    """读覆盖层文档（不含密钥）。缺文件/坏文件一律返回 {}。"""
    return _read_json(overlay_path(explicit) if path is None else path,
                      what="模型配置覆盖层")


def explicit_default(cap: str, *, explicit: str | None = None) -> str | None:
    """该能力的默认 provider 是否被**显式**激活过（覆盖层原文有键才算）。

    合并后的 ConfigStore 分不出「用户亲手激活」与「跟随 yaml 默认」——这个
    区分只存在于覆盖层原文，是生图三级路由（models.image_route）①级的判据。
    覆盖层跟随 store 的配置路径（`explicit`），与 `ConfigStore.load` 读的是同一份。
    坏文档一律按无激活处理：路由是热路径，这里绝不抛。"""
    try:
        d = read(explicit=explicit).get("defaults")
        p = d.get("providers") if isinstance(d, dict) else None
        v = p.get(cap) if isinstance(p, dict) else None
        return str(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def read_secrets(*, explicit: str | None = None) -> dict:
    """读本机密钥（`{"KEY": "值"}` 扁平表）。值一律转字符串、空值丢弃。"""
    raw = _read_json(secrets_path(explicit), what="本机密钥")
    out = {}
    bag = raw["secrets"] if isinstance(raw.get("secrets"), dict) else raw
    for k, v in bag.items():
        if k == "version" or v is None:
            continue
        s = str(v).strip()
        if s:
            out[str(k)] = s
    return out


def read_yaml_secrets_flat(path: Path) -> dict:
    """不依赖 PyYAML 读 secrets.yaml 的扁平 `KEY: value`。

    PyYAML 是可选附加依赖，若用它解析这份文件，「装没装某个可选包」就会决定
    「密钥读不读得到」。密钥本就是扁平键值，用标准库读同样准确，消除这条不确定性。
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([A-Za-z_][\w]*)\s*:\s*(.*)$', line)
        if not m:
            continue
        val = m.group(2).strip().split(" #")[0].strip().strip('"\'')
        if val:
            out[m.group(1)] = val
    return out


def merged_secrets(yaml_secrets: dict | None, *, explicit: str | None = None) -> dict:
    """密钥解析链的**文件侧**合并：secrets.yaml < secrets.local.json（网页填的更近）。

    环境变量仍由 `ConfigStore.secret` 排在最前，这一层不碰它——否则会出现
    「我 export 了却不生效」这类反直觉行为。
    """
    return {**(yaml_secrets or {}), **read_secrets(explicit=explicit)}


def file_secrets(config_dir: Path, *, explicit: str | None = None) -> dict:
    """**密钥文件侧的唯一读取口**：`secrets.yaml < secrets.local.json`，一次读全。

    存在的意义是让「只读一半」这类漏读无从发生：调用点若各自
    `yaml.safe_load(secrets.yaml)` 就地读一遍，既漏掉 `secrets.local.json`
    （向导/网页/`config secret` 三处写入的正是那一份，表现成「网页填了 OSS key，
    上传还说缺密钥」），又把 PyYAML 这个**可选**依赖变成密钥能否读到的开关。
    凡要读密钥文件的，一律走这里，别各读各的。

    环境变量不在这一层——调用方自己排在最前，保持「export 一定生效」。
    """
    return merged_secrets(read_yaml_secrets_flat(Path(config_dir) / "secrets.yaml"),
                          explicit=explicit)


def ensure_secrets_yaml(config_dir: Path) -> Path | None:
    """缺 `secrets.yaml` 时从随包的 `secrets.example.yaml` 复制一份全空模板。

    **用户不该手工创建这个文件**：手抄的那份没有注释、缺 key、还容易把
    `KEY: "值"` 写错格式，随后表现成「我明明填了却读不到」。模板是仓库跟踪的、
    每个值都是空串，复制出来零泄漏风险，用户只需往里填。

    已存在则**原样不动**——绝不覆盖用户填好的 key。返回新建的路径；已存在或
    模板缺失都返回 None（模板缺失不抛错：源码包被裁剪过也不该挡住整条 setup）。
    """
    config_dir = Path(config_dir)
    target = config_dir / "secrets.yaml"
    if target.exists():
        return None
    template = config_dir / "secrets.example.yaml"
    if not template.is_file():
        return None
    try:
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        return None
    return target


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def apply(data: dict, overlay: dict | None = None) -> dict:
    """把覆盖层合进配置文档，返回**新文档**（入参一个字节不动）。

    绝不就地改写：`ConfigStore` 的两条兜底出口把模块级的 `EMBEDDED_DEFAULTS`
    原样交出来，就地改一次就会污染整个进程——此后同进程内每次 load 都带着上一次
    的残留（Studio 一次页面加载会连打多个接口、一趟 run 里反复 load），且「没有
    覆盖层时逐字节相同」那条守卫抓不到。

    合并规则：
      · providers 按别名**两层**深合并 —— 只覆盖被写到的字段，同别名其余字段与
        其余别名一律保留 yaml 值。**绝不照抄 ConfigStore.load 那行顶层浅合并**，
        照抄的话用户改一个 base_url 就把其余十几个别名整段抹掉。
      · defaults 只认 providers 子键，逐能力覆盖 —— 这样 defaults 里并列的
        profile / fps / aspect 三个兄弟键不会被顺手带走。
      · 值为 None = 该字段没被覆盖（三态里的「清除」，与 shots[].refs、
        set_effects、set_watermark 同一套语义）。
    """
    ov = read() if overlay is None else overlay
    if not ov:
        return data                       # 无覆盖层 = 一个字节都不动
    for k in ov:
        if k not in MERGE_KEYS and k not in ("version", "updated_at"):
            print(f"  ⚠ 模型配置覆盖层不支持覆盖 `{k}`，已忽略"
                  f"（可覆盖：{'、'.join(MERGE_KEYS)}）")
    out = dict(data)

    ov_provs = _dict(ov.get("providers"), what="providers")
    if ov_provs:
        base = data.get("providers") or {}
        merged = dict(base)
        for alias, patch in ov_provs.items():
            if not isinstance(patch, dict):
                continue                  # 别名映射到 null = 整条恢复默认
            fields = {k: v for k, v in patch.items() if v is not None}
            if not fields:
                continue
            merged[alias] = {**(base.get(alias) or {}), **fields}
        out["providers"] = merged

    ov_defaults = _dict(_dict(ov.get("defaults"), what="defaults").get("providers"),
                        what="defaults.providers")
    if ov_defaults:
        base_defaults = data.get("defaults") or {}
        picks = {k: v for k, v in ov_defaults.items()
                 if k in CAPABILITIES and v not in (None, "")}
        if picks:
            out["defaults"] = {**base_defaults,
                               "providers": {**(base_defaults.get("providers") or {}),
                                             **picks}}
    return out


def summary(overlay: dict | None = None, *, explicit: str | None = None) -> dict | None:
    """覆盖层生效面的自述，供 doctor 与网页显示。

    **新开一个字段，绝不复用 `source`/`fallback`**——那两个是「内置精简配置正在
    服务」的告警数据源，被 doctor、overview、新建项目弹层三处消费，混进覆盖层的
    含义会让那条告警条报错状态。
    """
    ov = read() if overlay is None else overlay
    if not ov:
        return None
    provs = [k for k, v in _dict(ov.get("providers"), what="providers").items()
             if isinstance(v, dict) and v]
    acts = [k for k, v in _dict(_dict(ov.get("defaults"), what="defaults").get("providers"),
                                what="defaults.providers").items()
            if k in CAPABILITIES and v]
    if not provs and not acts:
        return None
    p = overlay_path(explicit)
    return {"path": str(p) if p else None,
            "providers": sorted(provs), "defaults": sorted(acts),
            "updated_at": ov.get("updated_at")}


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, payload: dict, *, private: bool = False) -> None:
    """原子写：先写同目录临时文件再 os.replace，避免半截文件被读到。

    临时名带随机后缀而不是固定的 `.tmp`：固定名下，两个并发写会往同一个临时文件
    交叉写入，谁先 replace 谁把对方的半截内容扶正。
    private=True 时**创建即 0600**（O_CREAT 带 mode），不留「先 0644 再 chmod」
    那一段密钥可被同机读取的窗口。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False：非有限数值不是合法 JSON，宁可写盘这一刻就炸，
    # 也不要落一份此后每次读都失败的配置
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{_rand()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600 if private else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()            # 失败别把半截文件（密钥场景是明文）留在盘上
        except OSError:
            pass
        raise


def validate_fields(fields: dict) -> dict:
    """连接段字段白名单校验。返回清洗后的字段表，非法键直接抛。

    `*_env` 结尾的键放行，但值必须长得像**环境变量名**——它们是密钥的
    *引用*而不是密钥本身；不校验的话，一个把真 key 填进 `api_key_env` 的用户
    就把密钥写进了这份会上行入库的文件。
    """
    out = {}
    for k, v in (fields or {}).items():
        if k.endswith("_env"):
            if v is None or v == "":
                out[k] = None
                continue
            if not _ENV_NAME.match(str(v)):
                raise ConfigError(
                    f"`{k}` 要填的是环境变量名（如 MINIMAX_API_KEY）而不是密钥本身；"
                    "密钥请走密钥入口单独保存，它不会进入这份可同步的配置文件。")
            out[k] = str(v)
            continue
        if _SECRETISH.search(k):
            raise ConfigError(
                f"字段 `{k}` 看起来是密钥本体。模型配置文件会同步进数据库，"
                "密钥只能走密钥入口存在本机。")
        if k not in _FIELD_WHITELIST:
            raise ConfigError(
                f"不支持的字段 `{k}`（可配置：{', '.join(sorted(_FIELD_WHITELIST))}）")
        if v not in (None, "") and (k in _FLOAT_FIELDS or k in _INT_FIELDS):
            cast = float if k in _FLOAT_FIELDS else int
            try:
                v = cast(str(v).strip())
            except (TypeError, ValueError):
                raise ConfigError(f"`{k}` 需要一个数值（收到 {v!r}）") from None
            if not math.isfinite(v):
                # Infinity/NaN 不是合法 JSON：落盘后每次读配置都炸，且它会一路
                # 进成本台账与预算闸参与算术
                raise ConfigError(f"`{k}` 不是一个有限数值（收到 {v!r}）")
            if k in _FLOAT_FIELDS and v < 0:
                raise ConfigError(f"`{k}` 不能为负数")
        out[k] = v
    return out


def save(*, providers: dict | None = None, defaults: dict | None = None,
         ws_root=None, now: str | None = None, explicit: str | None = None) -> dict:
    """写覆盖层（三态：字段缺省=不动 · None/空串=清除该覆盖 · 非空=覆盖）。

    先落 JSON 文件（**运行时真源**：引擎热路径只读文件、永不连库——`spawn_cli`
    起的每个子进程都要加载一次配置，为读配置连库既慢又给命令行平添数据库依赖），
    再在工作区可用时上行入库作跨机同步层。
    """
    path = overlay_path(explicit)
    if path is None:
        raise ConfigError(
            f"模型配置覆盖层已被环境变量 {ENV_OVERLAY} 显式关闭，本次不写入。")
    doc = read(path)
    doc["providers"] = _dict(doc.get("providers"), what="providers")
    doc["defaults"] = {"providers": _dict(
        _dict(doc.get("defaults"), what="defaults").get("providers"),
        what="defaults.providers")}
    doc.setdefault("version", 1)
    if now:
        doc["updated_at"] = now

    if providers:
        cur = dict(doc.get("providers") or {})
        for alias, patch in providers.items():
            if patch is None:                       # 整条恢复默认
                cur.pop(alias, None)
                continue
            fields = validate_fields(patch)
            entry = dict(cur.get(alias) or {})
            for k, v in fields.items():
                if v is None or v == "":
                    entry.pop(k, None)              # 清除该字段的覆盖
                else:
                    entry[k] = v
            if entry:
                cur[alias] = entry
            else:
                cur.pop(alias, None)                # 清空即等于没配过
        doc["providers"] = cur

    if defaults:
        cur = dict((doc.get("defaults") or {}).get("providers") or {})
        for cap, alias in defaults.items():
            if cap not in CAPABILITIES:
                raise ConfigError(f"未知能力 `{cap}`（可选：{', '.join(CAPABILITIES)}）")
            if alias in (None, ""):
                cur.pop(cap, None)                  # 跟随配置文件
            else:
                cur[cap] = alias
        doc["defaults"] = {"providers": cur}

    for k in ("providers", "defaults"):
        if k in doc and not doc[k]:
            doc.pop(k)
    _atomic_write(path, doc)
    push(doc, ws_root)
    return doc


def write_secret(env_name: str, value: str | None, *,
                 explicit: str | None = None) -> dict:
    """写/清一个密钥到本机密钥文件。**只写不读**——没有任何出口回读明文。

    与 `save()` 不同，这里没有 ws_root：密钥绝不上行入库，落点只由
    `secrets_path(explicit)` 决定（`--config` 指了另一份 models.yaml 时跟着走）。"""
    if not _ENV_NAME.match(env_name or ""):
        raise ConfigError(f"密钥变量名不合法: {env_name!r}（应形如 MINIMAX_API_KEY）")
    path = secrets_path(explicit)
    if path is None:
        raise ConfigError(f"覆盖层已被 {ENV_OVERLAY} 关闭，本次不写入密钥。")
    doc = _read_json(path, what="本机密钥")
    doc.setdefault("version", 1)
    bag = dict(doc.get("secrets") or {})
    if value is None or not str(value).strip():
        bag.pop(env_name, None)
    else:
        bag[env_name] = str(value).strip()
    doc["secrets"] = bag
    _atomic_write(path, doc, private=True)
    # 密钥**绝不上行入库**：密钥一旦入库即随备份与多机同步扩散。
    return {"env": env_name, "state": "local" if env_name in bag else "unset"}


# ---------------------------------------------------------------------------
# 双写：JSON（运行时真源）↔ MySQL（跨机同步层）
# ---------------------------------------------------------------------------
_SCOPE = "models"
_NAME = "overlay"


def sanitized(doc: dict) -> dict:
    """收敛成已知形状：只留 version / updated_at / defaults.providers，以及每个别名
    经白名单过滤后的连接字段。

    上库与回流两条路都过它。「密钥只在本机那份文件里」这条承诺，靠的不该是
    「用户不会手改 models.local.json」——手改过的文件同样会被 push 整份上传。
    """
    out = {"version": doc.get("version", 1)}
    if doc.get("updated_at"):
        out["updated_at"] = doc["updated_at"]
    provs = {}
    for alias, patch in _dict(doc.get("providers"), what="providers").items():
        if not isinstance(patch, dict):
            continue
        fields = {}
        for k, v in patch.items():
            # 逐字段过滤而不是整条丢弃：一个非法字段不该连累同别名的合法配置，
            # 尤其"非法"的典型形态就是有人手写了一把密钥进来
            try:
                clean = validate_fields({k: v})
            except ConfigError as e:
                print(f"  ⚠ 覆盖层里 {alias}.{k} 不合法（{e}），同步时已丢弃该字段")
                continue
            fields.update({kk: vv for kk, vv in clean.items() if vv is not None})
        if fields:
            provs[alias] = fields
    if provs:
        out["providers"] = provs
    acts = {k: v for k, v in _dict(
        _dict(doc.get("defaults"), what="defaults").get("providers"),
        what="defaults.providers").items() if k in CAPABILITIES and v}
    if acts:
        out["defaults"] = {"providers": acts}
    return out


def push(doc: dict, ws_root) -> None:
    """本地覆盖层上行入库。库不可用不算失败——文件才是运行时真源。"""
    if ws_root is None:
        return
    doc = sanitized(doc)
    try:
        from .storage import get_storage
        get_storage(ws_root).save_settings(_SCOPE, _NAME, doc)
    except Exception as e:  # noqa: BLE001  同步层挂了不该挡住本地保存
        print(f"  ⚠ 模型配置未能同步到数据库（{e}），本地文件已保存、功能不受影响")


def pull(ws_root) -> bool:
    """库 → 文件的回流（换机继承）。库行比本地文件新才覆盖，返回是否刷新过。

    判据复用项目/章节那套「新者赢」：库行明显更新（容差 2 秒）才为准，平手偏向
    文件——保守不打断本地正在进行的修改。
    """
    if ws_root is None or disabled():
        return False
    path = overlay_path()
    if path is None:
        return False
    try:
        from .storage import get_storage
        row = get_storage(ws_root).load_settings(_SCOPE, _NAME, local_file=path)
    except Exception:  # noqa: BLE001  同步层不可用不该挡住本地工作
        return False
    if not row or not isinstance(row, dict):
        return False
    doc = row.get("data")
    if not isinstance(doc, dict):
        return False
    doc = sanitized(doc)
    if path.is_file() and (not row.get("newer") or doc == read(path)):
        return False
    _atomic_write(path, doc)
    print(f"  ⚠ 模型配置：数据库比本地新，已刷新 {path}")
    return True


# ---------------------------------------------------------------------------
# 目录与自检
# ---------------------------------------------------------------------------
def adapter_catalog() -> list[dict]:
    """已实现的适配器目录（能力 + impl + 中文名）。

    这是配置中心「新增别名」表单里 impl 选择器的数据源，也是它的**能力上限**：
    `_ADAPTERS` 是模块级字面量表，没有运行时注册机制——网页能加的只是指向已有
    适配器的别名，凭空接一家没有适配器的厂商做不到。界面上必须写明这一限制。
    """
    from .models import _ADAPTERS
    out = []
    for (cap, impl) in sorted(_ADAPTERS):
        meta = IMPL_META.get((cap, impl)) or {}
        out.append({"capability": cap, "impl": impl,
                    "label": meta.get("label", impl), "vendor": meta.get("vendor", "")})
    return out


def secret_envs(conn: dict) -> list[str]:
    """连接段里全部密钥变量名。多凭证厂商不止一把钥匙（MiniMax 的
    API Key + GroupId），只看 api_key_env 会把它们的状态判错。"""
    return [str(v) for k, v in (conn or {}).items()
            if k.endswith("_env") and v]


def key_state(store, env_name: str | None) -> str:
    """密钥三态：`env` / `local`（本机密钥文件）/ `file`（secrets.yaml）/ `unset`。

    **只回状态、永不回值**——照 setup 向导那条既有先例。配置中心不提供任何
    读回明文的出口，输入框恒空，填过也不回显。
    """
    if not env_name:
        return "none"
    if os.environ.get(env_name):
        return "env"
    if env_name in read_secrets():
        return "local"
    if store is not None and (store.secrets or {}).get(env_name):
        return "file"
    return "unset"


def probe(store, alias: str) -> dict:
    """连通性自检：**零成本，一个生成请求都不发**。

    各家没有统一的免费探活端点，随便打一个生成端点就是真花钱——所以这里只查
    解析层查得到的那几件。真实计费校准（发一次最小请求对账单价）要花钱、要二次
    确认，不在这个出口里。
    """
    checks: list[dict] = []

    def note(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    conn = None
    try:
        conn = store.provider_conn(alias)
        note("别名已登记", True, f"来自 {store.source}")
    except Exception as e:  # noqa: BLE001
        note("别名已登记", False, str(e))
        return {"alias": alias, "ok": False, "checks": checks}

    kind = conn.get("kind")
    note("能力已声明", bool(kind), kind or "providers 段缺 kind，路由无法校验能力")

    impl = conn.get("impl", alias)
    from .models import _ADAPTERS
    has_impl = (kind, impl) in _ADAPTERS if kind else False
    note("适配器已实现", has_impl,
         f"impl={impl}" if has_impl else
         f"impl={impl} 没有对应适配器（能加的只是指向已有适配器的别名）")

    status = conn.get("status", "ready")
    note("状态可用", status == "ready",
         "ready" if status == "ready" else f"status={status}（已登记但不可调用）")

    meta = IMPL_META.get((kind, impl)) or {}
    optional = bool(meta.get("optional_key"))
    envs = secret_envs(conn)
    if not envs:
        note("密钥可取", True, "无需密钥")
    else:
        states = {e: key_state(store, e) for e in envs}
        got = [e for e, st in states.items() if st != "unset"]
        # any-of 语义：多凭证厂商里存在可选位（MiniMax 的 GroupId 仅历史网关
        # 需要），全按"每一把都要有"判会一片假红；主 key 在即视为可取，
        # 缺可选位的细节仍随 detail 逐把列出。
        note("密钥可取", bool(got) or optional,
             (meta.get("degrade") if optional and not got else None)
             or "、".join(f"{e}:{st}" for e, st in states.items()))

    base = (conn.get("base_url") or "").rstrip("/")
    if meta.get("no_endpoint"):
        note("端点已填", True, "无需端点（不发网络请求）")
    elif base:
        # 端点必须带 API 版本号：具体路径写在适配器里，base_url 少一段版本号的
        # 表现是 404，而那种 404 看起来像"服务挂了"，很难联想到配置写少一段。
        ok = bool(re.search(r"/v\d+[a-z0-9]*$", base))
        note("端点带 API 版本号", ok, base if ok else f"{base}（应以 /v1、/api/v3 之类结尾）")
    elif conn.get("host"):
        # 签名式接口（火山视觉 CV 服务）：路径与版本由适配器固定，连接段只声明 host
        note("端点已填", True, f"host={conn['host']}（签名接口，路径由适配器固定）")
    else:
        note("端点已填", False, "base_url 为空")

    if has_impl and status == "ready":
        try:
            inst = _ADAPTERS[(kind, impl)](conn, store)
            note("适配器可实例化", True)
        except Exception as e:  # noqa: BLE001
            note("适配器可实例化", False, str(e))
        else:
            # 适配器自报的真发条件（lipsync 的 req_key 之类，连接段里密钥与端点
            # 之外的必填项）：有降级分支的适配器缺配置不算故障，但缺什么要点名
            ready = getattr(inst, "configured", None)
            if callable(ready):
                ok, why = ready()
                note("适配器就绪", bool(ok) or optional, why if not ok else "")

    return {"alias": alias, "ok": all(c["ok"] for c in checks), "checks": checks}


def _grade_view(conn: dict, impl: str) -> dict | None:
    """该 provider 的画质档位块：认哪个字段、有哪些档、当前是哪一档。

    键 `field` 是适配器**真正读的**那个字段名——界面上那一格该叫什么、写回哪个键，
    都由它决定，前端不按能力硬猜（各家的字段名不保证都是 `resolution`）。目录之外的当前值原样带出（`in_catalog: False`），**绝不悄悄替用户改掉**：
    那可能是厂商刚开的新档，也可能是这台机器手填的值，两种都不该被界面吞掉。
    """
    spec = grades.spec_for(impl)
    if spec is None:
        return None
    cur = conn.get(spec.field)
    cur = "" if cur is None else str(cur)
    return {
        "field": spec.field, "label": spec.label, "hint": spec.hint,
        "current": cur,
        # 大小写按适配器自己的归一行为判（见 GradeSpec.fold）——minimax 会 upper，
        # 手填的 `768p` 是完全合法的配置，判成「目录外」就是在正确的值上挂告警
        "in_catalog": (not cur) or spec.matches(cur),
        "options": [{"value": g.value, "label": g.label, "caveat": g.caveat}
                    for g in spec.grades],
    }


def provider_view(store) -> list[dict]:
    """配置中心的 provider 清单（**逐字段白名单摘取，绝不整段下发连接表**）。

    整段下发的风险：连接段里一旦出现明文密钥，就会被顺路吐给前端。
    """
    ov = read(explicit=getattr(store, "_explicit", None))
    ov_provs = _dict(ov.get("providers"), what="providers")
    out = []
    for alias, conn in sorted((store.data.get("providers") or {}).items()):
        patch = ov_provs.get(alias) if isinstance(ov_provs.get(alias), dict) else {}
        # 主密钥位：没有 api_key_env 的多凭证厂商（火山视觉 AK/SK）取第一把——
        # 否则 key.state 恒为 none，缺密钥的服务商在卡片上显示成「免密钥 · 就绪」
        envs = secret_envs(conn)
        env = conn.get("api_key_env") or (envs[0] if envs else None)
        meta = IMPL_META.get((conn.get("kind"), conn.get("impl", alias))) or {}
        out.append({
            "alias": alias,
            "kind": conn.get("kind"),
            "impl": conn.get("impl", alias),
            "label": meta.get("label"),
            "vendor": meta.get("vendor"),
            "status": conn.get("status", "ready"),
            "base_url": conn.get("base_url"),
            "model": conn.get("model") or conn.get("resource_id"),
            "grade": _grade_view(conn, conn.get("impl", alias)),
            "prompt_lang": conn.get("prompt_lang"),
            "price": {k: conn.get(k) for k in
                      ("price_per_image", "price_per_image_hd", "price_per_second",
                       "price_per_second_4k", "price_per_kchar", "price_per_min",
                       "price_per_track")
                      if conn.get(k) is not None},
            "key": {"env": env, "state": key_state(store, env),
                    "optional": bool(meta.get("optional_key")),
                    "degrade": meta.get("degrade")},
            "console": meta.get("console"),
            # 多凭证厂商的其余密钥位（MiniMax 的 GroupId）：
            # 不列出来，网页上就没有地方填第二把钥匙
            "keys": [{"env": e, "state": key_state(store, e)} for e in envs],
            "overridden": sorted(patch.keys()),
        })
    return out


def config_view(store, ws_root=None) -> dict:
    """配置中心的整份只读视图（网页 GET 与 `config show` 共用一个出口）。"""
    profiles = store.data.get("profiles") or {}
    # profile 能力块里的 provider 是解析链的**第一跳**，优先级高于这里的全局激活项。
    # 不列出来，全局激活项对这些 profile 不生效的原因在界面上无从解释。
    deviations = []
    for name, p in profiles.items():
        for cap in CAPABILITIES:
            prov = (p.get(cap) or {}).get("provider")
            if prov:
                deviations.append({"profile": name, "capability": cap, "provider": prov})
    active = {cap: store.default_provider(cap) for cap in CAPABILITIES}
    explicit = getattr(store, "_explicit", None)
    ovp = overlay_path(explicit)
    return {
        "capabilities": [{"id": cap, **CAPABILITY_META[cap]} for cap in CAPABILITIES],
        "active": active,
        "activated_by": {cap: ("overlay" if cap in _dict(
            _dict(read(explicit=explicit).get("defaults"), what="defaults").get("providers"),
            what="defaults.providers") else "yaml")
                         for cap in CAPABILITIES},
        "providers": provider_view(store),
        "adapters": adapter_catalog(),
        "profile_deviations": deviations,
        "fields": sorted(_FIELD_WHITELIST),
        "source": getattr(store, "source", None),
        "fallback": getattr(store, "fallback", None),
        "overlay": summary(),
        "overlay_path": str(ovp) if ovp else None,
        "workspace": str(ws_root) if ws_root else None,
    }
