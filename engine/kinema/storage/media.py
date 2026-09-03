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

"""媒体持久层（图片/音频/视频/字幕上云）—— 与数据库层对称的双真源设计。

  本地工作区 = 渲染工作副本（ffmpeg/生成引擎永远吃本地文件）
  对象存储   = 媒体持久层与恢复源（`oss sync` 确认后上传并把文档路径改写为 URL，
               `oss pull` 按 URL 拉回本地——与 db sync/pull 完全对称）

核心设计：**对象 Key = 前缀/工作区相对路径**，URL 与本地路径互为纯函数映射，
不需要任何映射表；换域名/换 CDN 也能按 Key 前缀反解。

providers（均为可选依赖，按需安装）：
  aliyun      阿里云 OSS   pip install -e "engine[oss-aliyun]"   (oss2)
  tencent     腾讯云 COS   pip install -e "engine[oss-tencent]"  (cos-python-sdk-v5)
  volcengine  火山引擎 TOS pip install -e "engine[oss-volc]"     (tos)
  mock        离线测试（复制进 <ws>/.oss_mock/，URL 用 https://oss-mock.local/）
"""
from __future__ import annotations

import os
import shutil
import urllib.parse
from pathlib import Path

from ..errors import ConfigError

# 参与上云的媒体扩展名（文档字符串值命中 + 位于工作区内 + 文件存在 才会被同步）
MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif",
              ".mp4", ".mov", ".webm",
              ".wav", ".mp3", ".m4a", ".aac", ".ogg",
              ".ass", ".srt"}

_AK_ENV, _SK_ENV = "KINEMA_OSS_ACCESS_KEY", "KINEMA_OSS_SECRET_KEY"
_stores: dict = {}


def is_url(v) -> bool:
    return isinstance(v, str) and (v.startswith("http://") or v.startswith("https://"))


def _media_config() -> dict:
    """storage.yaml 的 media 段（backend 可用 KINEMA_MEDIA_BACKEND 临时覆盖）。"""
    from . import _find_config_file, _read_yaml
    path = _find_config_file()
    raw = (_read_yaml(path) if path else {}).get("media") or {}
    backend = os.environ.get("KINEMA_MEDIA_BACKEND") or raw.get("backend") or "local"
    cfg = {**raw, "backend": backend.strip().lower()}   # env 覆盖必须在展开之后
    # AK/SK：环境变量 > secrets.local.json > secrets.yaml
    # 同 load_storage_config：密钥文件只许经 file_secrets 读，就地读 yaml 会漏掉
    # 向导/网页写入的 secrets.local.json。
    ak, sk = os.environ.get(_AK_ENV), os.environ.get(_SK_ENV)
    if path and (not ak or not sk):
        from ..config_overlay import file_secrets
        secrets = file_secrets(path.parent)
        ak = ak or str(secrets.get(_AK_ENV) or "") or None
        sk = sk or str(secrets.get(_SK_ENV) or "") or None
    cfg["ak"], cfg["sk"] = ak, sk
    return cfg


# ============================================================================
# provider 适配器（upload/download/head 三件事；URL 规则各家拼接）
# ============================================================================
class _Aliyun:
    """阿里云 OSS（oss2 ≥2.18.4，V4 签名——2025-09 起新 Bucket 不再支持 V1 签名）。
    endpoint 缺省 https://oss-{region}.aliyuncs.com；region 为 V4 签名必填。"""

    def __init__(self, cfg):
        try:
            import oss2
        except ImportError as e:
            raise ConfigError("provider=aliyun 需要 oss2：pip install -e \"engine[oss-aliyun]\"") from e
        self.cfg = cfg
        endpoint = cfg.get("endpoint") or f"https://oss-{cfg['region']}.aliyuncs.com"
        auth = oss2.AuthV4(cfg["ak"], cfg["sk"])
        self.bucket = oss2.Bucket(auth, endpoint, cfg["bucket"], region=cfg["region"])
        host = endpoint.split("://", 1)[-1]
        self.base = cfg.get("public_base") or f"https://{cfg['bucket']}.{host}"

    def upload(self, local: Path, key: str) -> None:
        self.bucket.put_object_from_file(key, str(local))

    def download(self, key: str, local: Path) -> None:
        self.bucket.get_object_to_file(key, str(local))

    def head(self) -> str:
        info = self.bucket.get_bucket_info()
        return f"bucket={self.cfg['bucket']} location={info.location}"


class _Tencent:
    """腾讯云 COS（cos-python-sdk-v5）。bucket 需带 appid 后缀（如 name-1250000000）。"""

    def __init__(self, cfg):
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as e:
            raise ConfigError("provider=tencent 需要 cos-python-sdk-v5："
                              "pip install -e \"engine[oss-tencent]\"") from e
        self.cfg = cfg
        conf = CosConfig(Region=cfg["region"], SecretId=cfg["ak"],
                         SecretKey=cfg["sk"], Scheme="https")
        self.client = CosS3Client(conf)
        self.base = cfg.get("public_base") or \
            f"https://{cfg['bucket']}.cos.{cfg['region']}.myqcloud.com"

    def upload(self, local: Path, key: str) -> None:
        self.client.upload_file(Bucket=self.cfg["bucket"], Key=key,
                                LocalFilePath=str(local))

    def download(self, key: str, local: Path) -> None:
        self.client.download_file(Bucket=self.cfg["bucket"], Key=key,
                                  DestFilePath=str(local))

    def head(self) -> str:
        self.client.head_bucket(Bucket=self.cfg["bucket"])
        return f"bucket={self.cfg['bucket']} region={self.cfg['region']}"


class _Volcengine:
    """火山引擎 TOS（tos）。endpoint 缺省 tos-{region}.volces.com。"""

    def __init__(self, cfg):
        try:
            import tos
        except ImportError as e:
            raise ConfigError("provider=volcengine 需要 tos：pip install -e \"engine[oss-volc]\"") from e
        self.cfg = cfg
        endpoint = cfg.get("endpoint") or f"tos-{cfg['region']}.volces.com"
        self.client = tos.TosClientV2(cfg["ak"], cfg["sk"], endpoint, cfg["region"])
        self.base = cfg.get("public_base") or f"https://{cfg['bucket']}.{endpoint}"

    def upload(self, local: Path, key: str) -> None:
        self.client.put_object_from_file(self.cfg["bucket"], key, str(local))

    def download(self, key: str, local: Path) -> None:
        self.client.get_object_to_file(self.cfg["bucket"], key, str(local))

    def head(self) -> str:
        self.client.head_bucket(self.cfg["bucket"])
        return f"bucket={self.cfg['bucket']} region={self.cfg['region']}"


class _Mock:
    """离线测试：<ws>/.oss_mock/ 目录扮演 bucket，URL 用 https://oss-mock.local/。"""

    def __init__(self, cfg, ws_root: Path):
        self.root = ws_root / ".oss_mock"
        self.base = cfg.get("public_base") or "https://oss-mock.local"

    def upload(self, local: Path, key: str) -> None:
        dst = self.root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dst)

    def download(self, key: str, local: Path) -> None:
        src = self.root / key
        if not src.is_file():
            raise FileNotFoundError(f"mock 对象不存在: {key}")
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local)

    def head(self) -> str:
        return f"mock bucket={self.root}"


_PROVIDERS = {"aliyun": _Aliyun, "tencent": _Tencent, "volcengine": _Volcengine}


# ============================================================================
# MediaStore：Key ↔ 本地路径 ↔ URL 的纯函数映射 + 上传/下载
# ============================================================================
class MediaStore:
    def __init__(self, ws_root: Path, cfg: dict):
        self.ws_root = Path(ws_root).resolve()
        self.cfg = cfg
        self.backend = cfg.get("backend", "local")
        self.prefix = (cfg.get("prefix") or "kn").strip("/")
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.backend == "oss"

    def _cli(self):
        if self._client is None:
            prov = self.cfg.get("provider", "aliyun")
            if prov == "mock":
                self._client = _Mock(self.cfg, self.ws_root)
            elif prov in _PROVIDERS:
                if not self.cfg.get("ak") or not self.cfg.get("sk"):
                    raise ConfigError(
                        f"OSS 缺少密钥 {_AK_ENV} / {_SK_ENV}：请在 config/secrets.yaml "
                        f"填写，或用 `kinema config secret` 写入本机密钥文件，"
                        f"或 export 同名环境变量")
                if not self.cfg.get("bucket"):
                    raise ConfigError("OSS 缺少 bucket（config/storage.yaml 的 media 段）")
                self._client = _PROVIDERS[prov](self.cfg)
            else:
                raise ConfigError(f"未知 OSS provider: {prov}"
                                  f"（可选: {', '.join(_PROVIDERS)}, mock）")
        return self._client

    # ---- 纯函数映射 ----
    def key_for(self, local) -> str | None:
        try:
            rel = Path(local).resolve().relative_to(self.ws_root)
        except ValueError:
            return None
        return f"{self.prefix}/{rel.as_posix()}"
    def local_for(self, url: str) -> Path | None:
        """URL → 本地路径：按 Key 前缀反解（host 无关，换域名/CDN 也能恢复）。"""
        try:
            path = urllib.parse.unquote(urllib.parse.urlparse(url).path).lstrip("/")
        except Exception:  # noqa: BLE001
            return None
        if not path.startswith(self.prefix + "/"):
            return None
        return self.ws_root / path[len(self.prefix) + 1:]

    # ---- 传输 ----
    def upload(self, local) -> str:
        key = self.key_for(local)
        if key is None:
            raise ConfigError(f"文件不在工作区内，无法上云: {local}")
        self._cli().upload(Path(local), key)
        return f"{self._cli().base}/{urllib.parse.quote(key)}"

    def download(self, url: str) -> Path | None:
        local = self.local_for(url)
        if local is None:
            return None
        if not local.is_file():
            key = f"{self.prefix}/{local.relative_to(self.ws_root).as_posix()}"
            self._cli().download(key, local)
        return local

    def describe(self) -> str:
        if not self.enabled:
            return "local"
        return (f"oss · {self.cfg.get('provider')} · bucket={self.cfg.get('bucket') or '-'}"
                f" · prefix={self.prefix}")


def get_media_store(ws_root=None) -> MediaStore:
    if ws_root is None:
        from ..workspace import find_workspace
        ws_root = find_workspace()
    key = str(Path(ws_root).resolve())
    if key not in _stores:
        _stores[key] = MediaStore(Path(ws_root), _media_config())
    return _stores[key]


# ============================================================================
# 读路径适配（引擎/合成/版本栈/导出统一入口）
# ============================================================================
def localize(value, *, download: bool = False):
    """文档字段值 → 引擎可用的本地路径。

    非 URL 原样返回；URL 反解为本地路径（download=True 时缺失则从 OSS 拉回，
    实现媒体 rehydrate——与 db 层「文件缺失从库恢复」对称）。
    反解失败（外部 URL）返回原值，由调用方自行处理。
    """
    if not is_url(value):
        return value
    store = get_media_store()
    local = store.local_for(value)
    if local is None:
        return value
    if download and not local.is_file() and store.enabled:
        try:
            store.download(value)
        except Exception:  # noqa: BLE001  拉取失败按缺失处理，调用方报错更明确
            pass
    return str(local)


def ensure_local(value):
    """localize(download=True) 的语义化别名：渲染前保证媒体在本地。"""
    return localize(value, download=True)


# ============================================================================
# 文档遍历：收集/改写媒体字段（oss sync 的核心，字段无关、向前兼容）
# ============================================================================
def _walk_strings(node, fn):
    """深度遍历 dict/list，对每个字符串值调用 fn(old)->new|None（None=不改）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                nv = fn(v)
                if nv is not None:
                    node[k] = nv
            else:
                _walk_strings(v, fn)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                nv = fn(v)
                if nv is not None:
                    node[i] = nv
            else:
                _walk_strings(v, fn)


def collect_media(doc: dict, ws_root: Path) -> list[Path]:
    """找出文档中所有「工作区内、媒体扩展名、真实存在」的本地文件引用。"""
    ws = Path(ws_root).resolve()
    found: dict[str, Path] = {}

    def probe(v):
        if is_url(v) or not v.startswith("/"):
            return None
        p = Path(v)
        if p.suffix.lower() in MEDIA_EXTS and p.is_file():
            try:
                p.resolve().relative_to(ws)
                found[str(p.resolve())] = p.resolve()
            except ValueError:
                pass
        return None

    _walk_strings(doc, probe)
    return list(found.values())


def rewrite_media(doc: dict, mapping: dict[str, str]) -> int:
    """把文档中的本地路径替换为已上传的 URL，返回替换次数。"""
    count = 0

    def swap(v):
        nonlocal count
        url = mapping.get(v) or (mapping.get(str(Path(v).resolve()))
                                 if v.startswith("/") else None)
        if url:
            count += 1
            return url
        return None

    _walk_strings(doc, swap)
    return count
