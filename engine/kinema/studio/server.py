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

"""Studio HTTP 层：路由 + 静态资源 + Range 媒体流 + poster 缓存。

零额外依赖（stdlib ThreadingHTTPServer）。数据组装全部在 scanner.py，
本层只负责：URL → 数据/文件，以及四条安全边界：
  1. /media /poster 只放行 root / 工作区之内的**媒体扩展名**文件
     （防目录穿越 + 防读取 secrets.yaml/源码等非媒体文件）
  2. /assets 只放行前端资源目录内的文件
  3. Host 头必须是本机回环地址（防 DNS rebinding 把恶意网页变成同源）
  4. 全部 POST 需携带启动时生成的随机 X-Csrf-Token（防跨站触发付费操作）

API：
  GET /                      前端 SPA（hash 路由，所有视图共用一个入口）
  GET /assets/<file>         前端静态资源
  GET /api/overview          全局统计 + profiles + 最近成片 + 项目列表
  GET /api/projects          项目卡片摘要
  GET /api/project?id=       单项目全量（设计/角色设定图/道具/场景/章节表）
  GET /api/script?id=        剧本工作台（project_detail + 源正文分段目录/segments，仅元数据）
  GET /api/script/segment?id=&index=   源正文按段懒加载（逐段展开时切 raw.txt）
  GET /api/chapter?project=&id=   章节制作快照（轮询，边生成边看）
  GET /api/library           片库（全部成片）
  GET /api/cost              成本页（跨项目台账）
  GET /media?path=ABS        带 Range 的媒体流（视频可拖动进度）
  GET /poster?path=ABS       视频海报缩略图（ffmpeg 生成并缓存）
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import signal
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import actions, scanner

ASSETS = Path(__file__).resolve().parent.parent / "studio_app"


# ---- 引擎代码指纹（运行进程 vs 磁盘）----
# Studio 是常驻进程：Python 模块在启动瞬间定型，而 studio_app 逐请求读盘。
# 引擎代码更新后不重启，页面就在用新前端配旧后端——这种错配不报任何错，只以
# 「功能不生效」或「加载失败」的面目出现，且每次都要人肉排查到进程年龄才破案。
# 解法：启动时记一份源码指纹，请求时与盘上现值比对，不一致就在响应里亮牌。
_ENGINE_ROOT = Path(__file__).resolve().parent.parent
_FP_TTL_SEC = 5.0        # 比对精度只需跟上页面轮询节拍，不必每个请求都全包 stat
_fp_cache = {"at": 0.0, "fp": ""}


def engine_fingerprint(pkg_root: Path | None = None) -> str:
    """引擎 Python 源码的状态摘要（相对路径 × mtime × size 聚合）。

    只看 `*.py`：前端资源与配置本就即时生效，计入只会催人做无意义的重启。
    mtime+size 而非读内容——签出/保存必然更新 mtime，代价最多是一次多余的
    重启提示，换来的是全包遍历只花 stat 的钱。"""
    root = pkg_root or _ENGINE_ROOT
    parts = []
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        parts.append(f"{p.relative_to(root)}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()


def _engine_stale(boot_fp: str) -> bool:
    """盘上代码是否已领先于本进程。TTL 缓存内多线程并发重算无害（结果一致）。"""
    now = time.monotonic()
    if now - _fp_cache["at"] > _FP_TTL_SEC:
        _fp_cache["fp"] = engine_fingerprint()
        _fp_cache["at"] = now
    return bool(boot_fp) and _fp_cache["fp"] != boot_fp


# ---- 单例 pidfile + 残留检测（防 Studio 进程乱起：一工作区只保一实例）----
def _pidfile(ws_root) -> Path:
    """本工作区 Studio 的 pidfile（落在 gitignore 的 .studio_cache/ 里）。"""
    return Path(ws_root).resolve() / ".studio_cache" / "studio.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def running_instance(ws_root) -> dict | None:
    """本工作区在跑的 Studio 实例：读 pidfile 并校验进程存活，返回
    {pid, port, host, url}；进程已死则清理陈旧 pidfile 返回 None。"""
    pf = _pidfile(ws_root)
    try:
        info = json.loads(pf.read_text(encoding="utf-8"))
        pid = int(info["pid"])
    except Exception:
        return None
    if pid != os.getpid() and _alive(pid):
        info["url"] = f"http://{info.get('host', '127.0.0.1')}:{info.get('port')}"
        return info
    try:                                      # 陈旧 pidfile（进程已退）→ 清理
        pf.unlink()
    except OSError:
        pass
    return None


def other_studio_pids(exclude: int | None = None) -> list:
    """尽力扫描本机其它 Studio 服务进程（`python -m kinema studio`）——
    **只认 python 解释器进程**，排除 shell 包装/自身；无 ps 时静默返回空。"""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    me, pids = os.getpid(), []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, cmd = int(parts[0]), parts[1]
        if pid in (me, exclude):
            continue
        exe = cmd.split(None, 1)[0].lower()          # 首 token=可执行；zsh 包装被排除
        if "-m kinema studio" in cmd and "python" in exe:
            pids.append(pid)
    return sorted(set(pids))

# /media /poster 只服务这些扩展名——密钥/源码/配置永远不可被读取，
# 即使它们位于扫描根之内（扫描根默认是工作区父目录，可能是仓库根）。
_MEDIA_EXTS = frozenset({
    ".mp4", ".webm", ".mov", ".m4v",                      # 视频
    ".png", ".jpg", ".jpeg", ".gif", ".webp",             # 图片
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac",      # 音频
    ".ass", ".srt", ".vtt", ".json",                      # 字幕/时间戳
})

# 永不经 /media /poster 出口的文件名：本机密钥与模型配置覆盖层。
# 它们是 .json（放行后缀之一）且住在仓库的 config/ 下（缺省扫描根之内），
# 所以必须按名字显式拒绝，而不能指望后缀表与路径闸挡住。
# `.tmp` 前缀一并拒：原子写留下的半截文件同样是明文。
_DENY_NAMES = frozenset({"secrets.local.json", "models.local.json", "secrets.yaml"})
_DENY_PREFIX = ("secrets.local.json", "models.local.json")

# Host 头白名单（防 DNS rebinding：恶意域名解析到 127.0.0.1 后其页面会带自己的 Host）
_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_MIME = {".mp4": "video/mp4", ".webm": "video/webm",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8",
         ".html": "text/html; charset=utf-8",
         ".json": "application/json; charset=utf-8",
         ".ass": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
         ".woff2": "font/woff2"}


def _poster(video: Path, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{video}|{video.stat().st_mtime}".encode()).hexdigest()
    out = cache_dir / f"{key}.jpg"
    if not out.is_file():
        # 先写进程私有临时名再 replace：ThreadingHTTPServer 下两请求可能同时
        # 触发同一 key 的抽帧，直写会让另一边读到半截 jpg
        tmp = cache_dir / f"{key}.{os.getpid()}.tmp.jpg"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "0.8", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=540:-1", str(tmp)],
            capture_output=True)
        if tmp.is_file():
            os.replace(tmp, out)
    return out


def flat_name(s: str) -> bool:
    """定位参数（项目/章节/分镜号）是否为单段名。这些值会拼进文件路径，
    含路径分隔或相对段的值必须在入口拒收，而不是等它进到路径运算里生效。"""
    return bool(s) and s not in (".", "..") and Path(s).name == s


def project_dir(ws_root: Path, pid: str) -> Path | None:
    """把请求里的项目号解析成工作区内的项目目录；解析不出即 None。

    上传端点的定位参数由请求方任意给定，直接拼路径可被相对段带出工作区；
    统一先验形状再验包含关系（防符号链接绕行），并要求项目文档已存在——
    项目目录不该由上传端点无中生有地建出来。"""
    if not flat_name(pid):
        return None
    p = (ws_root / pid).resolve()
    try:
        p.relative_to(ws_root.resolve())
    except ValueError:
        return None
    return p if (p / "project.json").is_file() else None


def _make_handler(root: Path, store, ws_root: Path, csrf_token: str,
                  boot_fp: str = ""):
    cache_dir = root / ".studio_cache"
    media_roots = [root] + ([ws_root] if ws_root and ws_root != root else [])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音默认访问日志
            pass

        # ---- 安全边界 ----
        def _host_ok(self) -> bool:
            """Host 必须是本机回环地址（含端口皆可），否则拒绝——防 DNS rebinding。"""
            host = (self.headers.get("Host") or "").strip().lower()
            if host.startswith("["):                       # IPv6 形如 [::1]:8787
                name = host[: host.index("]") + 1] if "]" in host else host
            else:
                name = host.rsplit(":", 1)[0] if ":" in host else host
            return name in _ALLOWED_HOSTS

        def _safe_media(self, raw: str) -> Path | None:
            """只放行 root / 工作区之内、媒体扩展名、非隐藏路径的文件。"""
            p = Path(urllib.parse.unquote(raw)).resolve()
            if p.suffix.lower() not in _MEDIA_EXTS:
                return None
            # 本机配置与密钥文件绝不经此出口。`.json` 在放行后缀里（字幕时间轴要用），
            # 而扫描根缺省是仓库根、`config/` 正好落在里面：不显式拒绝的话，
            # 一个无鉴权的 GET 就能把密钥原文取走，文件权限 0600 在这条路上形同虚设
            # （服务端是以属主身份读的）。
            if p.name in _DENY_NAMES or p.name.startswith(_DENY_PREFIX):
                return None
            for base in media_roots:
                try:
                    rel = p.relative_to(base)
                except ValueError:
                    continue
                # 隐藏段只看工作区之内的相对路径：工作区自身的祖先目录带点
                # （如用户主目录下的点目录）不该让整个媒体出口失效
                if any(part.startswith(".") for part in rel.parts):
                    return None                            # 拒绝 .git/.studio_cache 等隐藏路径
                return p if p.is_file() else None
            return None

        def _safe_asset(self, name: str) -> Path | None:
            p = (ASSETS / name).resolve()
            try:
                p.relative_to(ASSETS)
            except ValueError:
                return None
            return p if p.is_file() else None

        def _shot_upload(self, u):
            """素材直供上传：body=图片原始字节，query 带定位与文件名。
            落 project/<pid>/assets/supply/ 后走 supply 同一条登记路径。"""
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            pid, cid, shot = q("project"), q("chapter"), q("shot")
            name = Path(urllib.parse.unquote(q("name") or "asset.png")).name
            ext = Path(name).suffix.lower()
            from ..supply import IMAGE_EXTS
            if ext not in IMAGE_EXTS:
                return self._json({"ok": False,
                                   "error": f"不支持的图片格式 {ext}"}, code=400)
            ln = int(self.headers.get("Content-Length") or 0)
            if not (0 < ln <= 30 * 1024 * 1024):
                return self._json({"ok": False,
                                   "error": "文件为空或超过 30MB 上限"}, code=400)
            proj = project_dir(ws_root, pid)
            if proj is None or not flat_name(cid) \
                    or not (proj / "chapters" / f"{cid}.json").is_file():
                return self._json({"ok": False,
                                   "error": f"找不到章节 {pid}/{cid}"}, code=404)
            try:
                raw = self.rfile.read(ln)
                sup = proj / "assets" / "supply"
                sup.mkdir(parents=True, exist_ok=True)
                dst = sup / name
                i = 2
                while dst.exists():                     # 不覆盖既有素材，缀号新存
                    dst = sup / f"{Path(name).stem}-{i}{ext}"
                    i += 1
                dst.write_bytes(raw)
                r = actions.supply_shot_image(
                    ws_root, pid, cid, shot=shot, path=dst,
                    aspect=q("aspect") or None,
                    skip_check=q("skip_check") in ("1", "true"))
                return self._json({"ok": True, **r, "stored": str(dst)})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, code=400)

        def _asset_supply(self, u):
            """设定图素材直供：body=图片原始字节，query 带 project/kind/name。

            与 `_shot_upload` 同一条原始字节通道（不走 base64 JSON：一张设定图动辄
            几 MB，base64 白白多传三分之一）。落 `assets/supply/` 后交
            `refine.supply_asset_sheet` 完成"归档旧版→落标准路径→血缘传播"。
            """
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            pid, kind, name = q("project"), q("kind"), q("name") or None
            fname = Path(urllib.parse.unquote(q("filename") or "sheet.png")).name
            ext = Path(fname).suffix.lower()
            from ..supply import IMAGE_EXTS
            if ext not in IMAGE_EXTS:
                return self._json({"ok": False,
                                   "error": f"不支持的图片格式 {ext}"}, code=400)
            ln = int(self.headers.get("Content-Length") or 0)
            if not (0 < ln <= 30 * 1024 * 1024):
                return self._json({"ok": False,
                                   "error": "文件为空或超过 30MB 上限"}, code=400)
            proj = project_dir(ws_root, pid)
            if proj is None:
                return self._json({"ok": False, "error": f"找不到项目 {pid}"}, code=404)
            try:
                raw = self.rfile.read(ln)
                sup = proj / "assets" / "supply"
                sup.mkdir(parents=True, exist_ok=True)
                dst = sup / fname
                i = 2
                while dst.exists():                     # 不覆盖既有素材，缀号新存
                    dst = sup / f"{Path(fname).stem}-{i}{ext}"
                    i += 1
                dst.write_bytes(raw)
                r = actions.supply_asset_sheet(
                    ws_root, pid, kind=kind, name=name, path=dst,
                    skip_check=q("skip_check") in ("1", "true"))
                return self._json({"ok": True, **r, "stored": str(dst)})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, code=400)

        def _previz_frame(self, u):
            """3D 控制台逐帧上传：body=PNG 原始字节，query 带定位与帧号。

            仿 `_shot_upload` 的原始字节通道（而非 base64 JSON）——一段 5s@24fps
            的 previz 是 120 张 PNG，base64 会白白多传 33% 且每帧都要在两端做一次
            字符串转换。落 `<work>/previz/_frames/shot_<id>/f%05d.png`，`previz build`
            编完即删。

            `i=0` 视为一次新渲染的开始，**先清空该镜的帧目录**：不清的话上一次
            渲了 120 帧、这次只渲 96 帧，尾部 24 张旧帧会被 ffmpeg 一并编进去，
            成片末尾多出一段上一版的运动——这种错很难看出是"没清目录"。
            """
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            pid, cid, shot = q("project"), q("chapter"), q("shot")
            try:
                idx = int(q("i"))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "缺少或非法的帧号 i"}, code=400)
            if not (flat_name(pid) and flat_name(cid) and flat_name(shot)) \
                    or idx < 0 or idx > 99999:
                return self._json({"ok": False, "error": "缺少定位参数或帧号越界"}, code=400)
            ln = int(self.headers.get("Content-Length") or 0)
            if not (0 < ln <= 12 * 1024 * 1024):
                return self._json({"ok": False, "error": "帧为空或超过 12MB"}, code=400)
            try:
                raw = self.rfile.read(ln)
                if not raw.startswith(b"\x89PNG"):
                    return self._json({"ok": False, "error": "帧必须是 PNG"}, code=400)
                from ..previz import frames_dir
                from ..project import Project
                proj = project_dir(ws_root, pid)
                cf = (proj / "chapters" / f"{cid}.json") if proj else None
                if cf is None or not cf.is_file():
                    return self._json({"ok": False, "error": f"找不到章节 {pid}/{cid}"},
                                      code=404)
                fdir = frames_dir(Project.load(cf), shot)
                if idx == 0:
                    for old in fdir.glob("f*.png"):
                        old.unlink()
                (fdir / f"f{idx:05d}.png").write_bytes(raw)
                return self._json({"ok": True, "i": idx})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, code=400)

        def _previz_upload(self, u):
            """外部 previz 视频上传（不走 3D 控制台的手工路径）：body=mp4/mov 原始字节。

            落 `project/<pid>/assets/previz/` 后走 `previz register` 同一条登记路径。
            """
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            pid, cid, shot = q("project"), q("chapter"), q("shot")
            name = Path(urllib.parse.unquote(q("name") or "previz.mp4")).name
            ext = Path(name).suffix.lower()
            from ..previz import VIDEO_EXTS, register_previz
            if ext not in VIDEO_EXTS:
                return self._json({"ok": False,
                                   "error": f"不支持的视频格式 {ext}"
                                            f"（可选 {', '.join(sorted(VIDEO_EXTS))}）"},
                                  code=400)
            ln = int(self.headers.get("Content-Length") or 0)
            if not (0 < ln <= 200 * 1024 * 1024):
                return self._json({"ok": False, "error": "文件为空或超过 200MB 上限"},
                                  code=400)
            proj = project_dir(ws_root, pid)
            cf = (proj / "chapters" / f"{cid}.json") if proj and flat_name(cid) else None
            if cf is None or not cf.is_file():
                return self._json({"ok": False, "error": f"找不到章节 {pid}/{cid}"},
                                  code=404)
            try:
                raw = self.rfile.read(ln)
                d = proj / "assets" / "previz"
                d.mkdir(parents=True, exist_ok=True)
                dst, i = d / name, 2
                while dst.exists():                 # 不覆盖既有素材，缀号新存
                    dst = d / f"{Path(name).stem}-{i}{ext}"; i += 1
                dst.write_bytes(raw)
                from ..models import ConfigStore
                from ..project import Project
                r = register_previz(Project.load(cf), shot, dst,
                                    camera_preset=q("camera") or None,
                                    use_first_frame=(True if q("first") == "1" else
                                                     (False if q("first") == "0" else None)),
                                    store=ConfigStore.shared(None))
                return self._json({"ok": True, **r, "stored": str(dst)})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, code=400)

        def _adapt_upload(self, u):
            """源剧本/小说上传入库：body=正文原始字节，query 带 project 与 name。
            落 project/<pid>/source/raw.txt 并走 Track A 结构切分（与 CLI adapt import
            同一条 Series.ingest_source 路径）。拆书/分集仍由 Claude 指挥层承接。"""
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            pid = q("project")
            name = Path(urllib.parse.unquote(q("name") or "source.txt")).name
            ln = int(self.headers.get("Content-Length") or 0)
            if not (0 < ln <= 20 * 1024 * 1024):
                return self._json({"ok": False,
                                   "error": "文件为空或超过 20MB 上限"}, code=400)
            try:
                raw = self.rfile.read(ln)
                from ..workspace import Workspace
                series = Workspace.open(str(ws_root), create=False).get_project(pid)
                r = series.ingest_source(filename=name, data=raw)
                return self._json({"ok": True, **r})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, code=400)

        def _moodboard_upload(self, u):
            """风格垫图上传：body=图片原始字节 → assets/refs/moodboard/ → 登记系列 moodboard
            （注入每张设定图/分镜图生成的 ref_images；本地图转 base64，无需 OSS）。"""
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731
            pid = q("project")
            name = Path(urllib.parse.unquote(q("name") or "ref.png")).name
            ext = Path(name).suffix.lower()
            from ..supply import IMAGE_EXTS
            if ext not in IMAGE_EXTS:
                return self._json({"ok": False, "error": f"不支持的图片格式 {ext}"}, code=400)
            ln = int(self.headers.get("Content-Length") or 0)
            if not (0 < ln <= 30 * 1024 * 1024):
                return self._json({"ok": False, "error": "文件为空或超过 30MB 上限"}, code=400)
            proj = project_dir(ws_root, pid)
            if proj is None:
                return self._json({"ok": False, "error": f"找不到项目 {pid}"}, code=404)
            try:
                raw = self.rfile.read(ln)
                mbdir = proj / "assets" / "refs" / "moodboard"
                mbdir.mkdir(parents=True, exist_ok=True)
                dst, i = mbdir / name, 2
                while dst.exists():                 # 不覆盖既有垫图，缀号新存
                    dst = mbdir / f"{Path(name).stem}-{i}{ext}"; i += 1
                dst.write_bytes(raw)
                r = actions.add_moodboard(ws_root, pid, path=str(dst.resolve()))
                return self._json({"ok": True, **r, "stored": str(dst)})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, code=400)


        # ---- 路由 ----
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send_bytes(b"forbidden host", "text/plain", code=403)
            u = urllib.parse.urlparse(self.path)
            path = u.path
            try:
                return self._route_get(u, path)
            except Exception as e:  # noqa: BLE001  存储/配置异常给出指引而非裸栈崩掉仪表盘
                if path.startswith("/api/"):
                    return self._json({"ok": False, "error": str(e)}, code=500)
                return self._send_bytes(f"server error: {e}".encode(), "text/plain", code=500)

        def _route_get(self, u, path):
            qs = urllib.parse.parse_qs(u.query)
            q = lambda k: (qs.get(k) or [""])[0]  # noqa: E731

            if path == "/" or path == "/index.html":
                return self._send_index()
            if path.startswith("/assets/"):
                # **静态资源一律 no-store**：这是本地开发工具，改完前端代码刷新页面就该
                # 生效。不给缓存头时浏览器会启发式缓存 js/css，于是出现最难查的一类现象
                # ——"我明明改了、你也重启了，可界面行为还是旧的"（3D 控制台的 gizmo
                # 就这样让人对着一份缓存住的旧代码怎么拖都不动）。
                f = self._safe_asset(path[len("/assets/"):])
                return self._send_file(f, cache=False) if f else self._404()

            if path == "/api/overview":
                from ..branding import load_branding
                # **新读一份配置**而不是用 serve() 持有的进程级单例：配置中心刚改完
                # 的连接段与激活项必须当场反映到画风目录与配置健康块上，否则用户
                # 会看到「保存成功但没变」，而那要等 studio --restart 才好。
                from ..models import ConfigStore
                cur = actions._fresh_store()
                return self._json({
                    "workspace": str(ws_root) if ws_root else None,
                    "root": str(root), "config": getattr(cur, "source", None),
                    "brand": load_branding(),
                    **scanner.overview(root, ws_root, cur)})
            if path == "/api/config":
                return self._json({"ok": True, **actions.config_view(ws_root)})
            if path == "/api/projects":
                return self._json({"projects": scanner.workspace_summary(ws_root)})
            if path == "/api/project":
                d = scanner.project_detail(ws_root, store, q("id")) if q("id") else None
                return self._json(d) if d else self._404_json()
            if path == "/api/script":
                d = scanner.script_detail(ws_root, store, q("id")) if q("id") else None
                return self._json(d) if d else self._404_json()
            if path == "/api/script/segment":
                # 源正文按段懒加载（剧本工作台正文查看器逐段展开时拉取，不随首屏下发）
                try:
                    idx = int(q("index"))
                except (TypeError, ValueError):
                    idx = None
                d = (scanner.script_segment(ws_root, q("id"), idx)
                     if q("id") and idx is not None else None)
                return self._json(d) if d else self._404_json()
            if path == "/api/novel/chapter":
                # 原创正文按章懒加载（创作 Tab 阅读器；目录随 /api/script 首屏下发）
                try:
                    no = int(q("no"))
                except (TypeError, ValueError):
                    no = None
                d = (scanner.novel_chapter(ws_root, q("id"), no)
                     if q("id") and no is not None else None)
                return self._json(d) if d else self._404_json()
            if path == "/api/chapter":
                pid, cid = q("project"), q("id") or q("chapter")
                d = scanner.chapter_detail(ws_root, store, pid, cid) if (pid and cid) else None
                return self._json(d) if d else self._404_json()
            if path == "/api/video-preview":
                # 逐镜「实发提示词」：与 gen-video --dry-run 同一条编译路径，
                # 按需计算（编译整章有毫秒级成本，不进 /api/chapter 的 3s 轮询）
                pid, cid = q("project"), q("id") or q("chapter")
                d = scanner.video_preview(ws_root, store, pid, cid) if (pid and cid) else None
                return self._json(d) if d else self._404_json()
            if path == "/api/library":
                return self._json({"videos": scanner.library(root)})
            if path == "/api/queue":
                return self._json({"items": scanner.review_queue(ws_root)})
            if path == "/api/board":
                return self._json(scanner.board(ws_root))
            if path == "/api/search":
                return self._json({"items": scanner.search(ws_root, q("q"))})
            if path == "/api/cost":
                return self._json(scanner.cost(ws_root))
            if path == "/api/job":
                # 异步任务轮询（重新生成/局部改造）：running → done|failed
                from . import jobs
                j = jobs.status(q("id"))
                return self._json(j) if j else self._404_json()
            if path == "/api/jobs":
                # 进行中任务对账：章节视图凭 meta(project/chapter/shot/kind)
                # 恢复分镜卡忙态——轮询重绘/刷新页面后「生成中」不丢
                from . import jobs
                return self._json({"jobs": jobs.active(
                    q("project") or None, q("chapter") or None)})

            if path == "/media":
                f = self._safe_media(q("path"))
                return self._send_range(f) if f else self._404()
            if path == "/poster":
                f = self._safe_media(q("path"))
                return self._send_file(_poster(f, cache_dir)) if f else self._404()
            return self._404()

        # ---- 写操作（表态 / 锚定评论 / 版本回滚，本地读写制作台） ----
        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._json({"ok": False, "error": "forbidden host"}, code=403)
            if self.headers.get("X-Csrf-Token") != csrf_token:
                return self._json(
                    {"ok": False, "error": "缺少或错误的 X-Csrf-Token（请从 Studio 页面发起操作）"},
                    code=403)
            u = urllib.parse.urlparse(self.path)
            path = u.path
            if path == "/api/shot/upload":
                return self._shot_upload(u)
            if path == "/api/asset/supply":       # 设定图素材直供（原始字节）
                return self._asset_supply(u)
            if path == "/api/previz/frame":       # 3D 控制台逐帧 PNG（原始字节，高频）
                return self._previz_frame(u)
            if path == "/api/previz/upload":      # 外部 previz 视频（原始字节）
                return self._previz_upload(u)
            if path == "/api/adapt/upload":
                return self._adapt_upload(u)
            if path == "/api/moodboard/upload":
                return self._moodboard_upload(u)
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}")
                pid, cid = body.get("project"), body.get("chapter")
                if path == "/api/prompt-approval":
                    r = actions.approve_shot_prompt(
                        ws_root, pid, cid, shot=body.get("shot"),
                        sha=body.get("sha"))
                elif path == "/api/review":
                    r = actions.set_review(
                        ws_root, pid, cid,
                        shots=body.get("shots") if body.get("shots") is not None
                              else body.get("shot"),
                        stage=body.get("stage"), state=body.get("state"),
                        note=body.get("note"))
                elif path == "/api/comment":
                    r = actions.add_comment(
                        ws_root, pid, cid, shot=body.get("shot"),
                        stage=body.get("stage") or "image", text=body.get("text"),
                        x=body.get("x"), y=body.get("y"), t=body.get("t"),
                        path=body.get("path"),
                        asset_kind=body.get("asset_kind"), asset_name=body.get("asset_name"))
                elif path == "/api/comment/update":
                    r = actions.update_comment(
                        ws_root, pid, cid, comment_id=body.get("comment_id"),
                        text=body.get("text"), delete=bool(body.get("delete")),
                        asset_kind=body.get("asset_kind"), asset_name=body.get("asset_name"))
                elif path == "/api/export":
                    r = actions.export_artifact(
                        ws_root, store, kind=body.get("kind"), pid=pid, cid=cid)
                elif path == "/api/refine":
                    if body.get("async"):
                        # 改造台走异步：立即返回 job_id，前端轮询 + 转动等待特效
                        from . import jobs
                        _kw = dict(pid=pid, cid=cid, shot=body.get("shot"),
                                   asset_kind=body.get("asset_kind"),
                                   asset_name=body.get("asset_name"),
                                   rect=body.get("rect"),
                                   instruction=body.get("instruction"),
                                   mock=bool(body.get("mock")))
                        _meta = {"project": pid, "kind": "refine"}
                        if body.get("shot") is not None:
                            _meta.update(chapter=cid, shot=str(body.get("shot")))
                        else:   # 设定图改造：无镜号，凭 asset 定位
                            _meta.update(asset_kind=body.get("asset_kind"),
                                         asset_name=body.get("asset_name"))
                        r = {"job": jobs.run_fn(
                            lambda: actions.refine_image(ws_root, **_kw),
                            label=f"{pid}/{cid} 镜{body.get('shot')} 局部改造",
                            meta=_meta)}
                    else:
                        r = actions.refine_image(
                            ws_root, pid=pid, cid=cid, shot=body.get("shot"),
                            asset_kind=body.get("asset_kind"),
                            asset_name=body.get("asset_name"),
                            rect=body.get("rect"), instruction=body.get("instruction"),
                            mock=bool(body.get("mock")))
                elif path == "/api/project/create":   # 网页「＋ 新建项目」：建确定性空壳
                    r = actions.create_project(
                        ws_root, title=body.get("title"), profile=body.get("profile"),
                        skill=body.get("skill"),
                        aspect=body.get("aspect"), platform=body.get("platform"),
                        subtitle_lang=body.get("subtitle_lang"),
                        logline=body.get("logline"), character=body.get("character"),
                        pid=body.get("id"))
                elif path == "/api/project/delete":
                    r = actions.delete_project(ws_root, pid)
                elif path == "/api/project/restore":
                    r = actions.restore_project(ws_root, pid)
                elif path == "/api/effects/set":   # 特效选择器：写章节 effects + 可选重合成
                    r = actions.set_effects(
                        ws_root, pid, cid, effects=body.get("effects"),
                        recompose=bool(body.get("recompose")), mock=bool(body.get("mock")))
                elif path == "/api/regen":
                    if body.get("asset_kind"):     # 设定图重生（提意见→refine 全图应用）
                        r = actions.regen_asset(ws_root, pid, kind=body.get("asset_kind"),
                                                name=body.get("asset_name"),
                                                mock=bool(body.get("mock")))
                    else:
                        r = actions.regen_shot(ws_root, pid, cid, shot=body.get("shot"),
                                               mock=bool(body.get("mock")))
                elif path == "/api/shot/supply":
                    r = actions.supply_shot_image(
                        ws_root, pid, cid, shot=body.get("shot"),
                        path=body.get("path"), aspect=body.get("aspect"),
                        skip_check=bool(body.get("skip_check")))
                elif path == "/api/pick":
                    r = actions.pick_image(
                        ws_root, pid, cid, shot=body.get("shot"),
                        no=body.get("no"), keep_open=bool(body.get("keep_open")))
                elif path == "/api/rollback":
                    if body.get("asset_kind"):     # 设定图版本回滚（系列文档·无章节/阶段）
                        r = actions.rollback_asset_version(
                            ws_root, pid, kind=body.get("asset_kind"),
                            name=body.get("asset_name"), to=body.get("to"))
                    elif body.get("output_aspect"):   # 成片版本回滚（章节顶层·按比例分谱系）
                        r = actions.rollback_output_version(
                            ws_root, pid, cid, aspect=body.get("output_aspect"),
                            to=body.get("to"))
                    else:
                        r = actions.rollback_version(
                            ws_root, pid, cid, shot=body.get("shot"),
                            stage=body.get("stage"), to=body.get("to"))
                elif path == "/api/transition/add":
                    r = actions.transition_add(
                        ws_root, pid, cid, after=body.get("after"),
                        ttype=body.get("type"), text=body.get("text"),
                        asset=body.get("asset"), dur=body.get("dur"),
                        edge=body.get("edge"), direction=body.get("direction"),
                        color=body.get("color"), sound=body.get("sound"))
                elif path == "/api/transition/remove":
                    r = actions.transition_remove(ws_root, pid, cid,
                                                  shot=body.get("shot"))
                elif path == "/api/voice/audition":
                    r = actions.voice_audition(
                        ws_root, pid, owner=body.get("owner"),
                        candidates=body.get("candidates"), mock=bool(body.get("mock")))
                elif path == "/api/voice/custom":
                    r = actions.voice_custom(
                        ws_root, pid, owner=body.get("owner"), prompt=body.get("prompt"),
                        count=body.get("count"), mock=bool(body.get("mock")))
                elif path == "/api/voice/use":
                    r = actions.voice_use(ws_root, pid, owner=body.get("owner"),
                                          no=body.get("no"), custom=bool(body.get("custom")),
                                          cast=body.get("cast"), mock=bool(body.get("mock")))
                elif path == "/api/voice/anchor-warm":
                    r = actions.voice_anchor_warm(ws_root, pid, cid,
                                                  shot=body.get("shot"), no=body.get("no"),
                                                  mock=bool(body.get("mock")))
                elif path == "/api/voice/delete":
                    r = actions.voice_delete(ws_root, pid, cast=body.get("cast"))
                elif path == "/api/refpick":
                    r = actions.pick_ref(ws_root, pid, kind=body.get("kind"),
                                         name=body.get("name"), no=body.get("no"))
                elif path == "/api/adapt/scaffold":
                    r = actions.adapt_scaffold(ws_root, pid, only=body.get("only"))
                elif path == "/api/adapt/clear":
                    r = actions.clear_source(ws_root, pid)
                elif path == "/api/novel/thread":   # 伏笔账本状态标记（pay/drop/open 记账，非创作）
                    r = actions.novel_thread(
                        ws_root, pid, tid=body.get("tid"),
                        status=body.get("status"),
                        paid_in=body.get("paid_in"), note=body.get("note"))
                elif path == "/api/moodboard/remove":
                    r = actions.remove_moodboard(ws_root, pid, path=body.get("path"))
                elif path == "/api/moodboard/toggle":   # 切换库项默认启用（不删文件）
                    r = actions.toggle_moodboard(ws_root, pid, path=body.get("path"),
                                                 on=bool(body.get("on")))
                elif path == "/api/shot/refs":          # 镜级参考库覆盖：勾选/取消垫图 → 写 shots[].refs
                    r = actions.set_shot_refs(
                        ws_root, pid, cid, shot=body.get("shot"),
                        refs=None if body.get("clear") else (body.get("refs") or []))
                elif path == "/api/asset/refs":         # 设定图逐张垫图覆盖 → 写实体 refs/scene_refs
                    r = actions.set_asset_refs(
                        ws_root, pid, kind=body.get("asset_kind"),
                        name=body.get("asset_name"),
                        refs=None if body.get("clear") else (body.get("refs") or []))
                elif path == "/api/asset/regen-refs":   # 设定图按新垫图重生（project refs --only --force）
                    r = actions.regen_asset_refs(
                        ws_root, pid, kind=body.get("asset_kind"),
                        name=body.get("asset_name"), mock=bool(body.get("mock")))
                elif path == "/api/watermark":
                    # 三类水印一次写完、只重烧一次（分多次 POST 会让多个 watermark
                    # 任务同时改写同一批 output_wm，后完成的那个以其他类的旧状态为准）。
                    # 三态：字段缺省=不动 · ""=清除 · 非空=设置；burn=false 只写盘不烧
                    # （同一次提交还改了字幕样式时，重烧归 rebuild_final 一条链）
                    r = actions.set_watermark(
                        ws_root, pid, cid, text=body.get("text"),
                        fixed_text=body.get("fixed_text"),
                        fixed_position=body.get("fixed_position"),
                        bottom_text=body.get("bottom_text"),
                        burn=bool(body.get("burn", True)))
                elif path == "/api/subtitle/style":
                    # 字幕样式白名单覆盖（章节 subtitle 块）→ rebuild_final 一条链
                    # 重烧字幕与水印（字幕是合成期烧录，改完必须重合成才可见）
                    r = actions.set_subtitle_style(
                        ws_root, pid, cid, style=body.get("style"),
                        rebuild=bool(body.get("rebuild", True)),
                        mock=bool(body.get("mock")))
                elif path == "/api/rebuild":
                    r = actions.rebuild_final(ws_root, pid, cid,
                                              mock=bool(body.get("mock")))
                elif path == "/api/previz/save":     # 3D 场景编排快照（整体替换）
                    r = actions.previz_save(ws_root, pid, cid, scene=body.get("scene"))
                elif path == "/api/previz/render":   # 帧序列 → mp4 → 登记（后台任务）
                    r = actions.previz_render(
                        ws_root, pid, cid, shot=body.get("shot"),
                        fps=body.get("fps") or 24, camera=body.get("camera"),
                        use_first_frame=body.get("use_first_frame"),
                        mock=bool(body.get("mock")))
                elif path == "/api/previz/reel":     # 全片预演（各镜 previz → 一条长片）
                    r = actions.previz_reel(ws_root, pid, cid)
                elif path == "/api/previz/clear":    # 摘除挂载（保留产物，不动分镜图）
                    r = actions.previz_clear(ws_root, pid, cid, shot=body.get("shot"))
                elif path == "/api/previz/v2v":      # 参考视频 V2V 开关（**花钱开关**）
                    r = actions.previz_set_v2v(ws_root, pid, cid, on=bool(body.get("on")))
                elif path == "/api/previz/seedance":  # 交给 Seedance（native + V2V·可选镜）
                    r = actions.previz_to_seedance(ws_root, pid, cid,
                                                   only=body.get("only"),
                                                   mock=bool(body.get("mock")))
                elif path == "/api/sketch/gen":      # 简笔分镜板批量生成（后台任务·可选镜）
                    r = actions.sketch_generate(ws_root, pid, cid,
                                                shots=body.get("shots"),
                                                force=bool(body.get("force")),
                                                mock=bool(body.get("mock")))
                elif path == "/api/sketch/regen":    # 灯箱重生成（--force + --note 意见）
                    r = actions.sketch_regen(ws_root, pid, cid,
                                             shot=body.get("shot"),
                                             note=body.get("note"),
                                             mock=bool(body.get("mock")))
                elif path == "/api/sketch/guide":    # previz/sketch 互斥仲裁表态
                    r = actions.sketch_guide(ws_root, pid, cid,
                                             shot=body.get("shot"),
                                             guide=body.get("guide"))
                elif path == "/api/sketch/clear":    # 摘除板挂载（板文件保留·beats 不动）
                    r = actions.sketch_clear(ws_root, pid, cid, shot=body.get("shot"))
                elif path == "/api/score/save":    # 音频剧本存稿 + audio_mode 路线切换
                    r = actions.save_audio_script(ws_root, pid, cid,
                                                  segments=body.get("segments"),
                                                  mode=body.get("mode"))
                elif path == "/api/score/draft":   # 按分镜起草（零成本·不落盘，回给前端填框）
                    r = actions.draft_audio_script(ws_root, pid, cid)
                elif path == "/api/score/switch":  # 段版本切换（零成本·互换 + 重拼整轨）
                    r = actions.switch_score_segment(ws_root, pid, cid,
                                                     no=body.get("no"),
                                                     to_v=body.get("to_v"))
                elif path == "/api/score/gen":     # 音频剧本整轨生成（后台任务·可选段号）
                    r = actions.score_generate(ws_root, pid, cid,
                                               only=body.get("only"),
                                               force=bool(body.get("force")),
                                               mock=bool(body.get("mock")))
                elif path == "/api/verify":       # 成片自审（零成本本地探测，后台任务）
                    r = actions.verify_final(ws_root, pid, cid,
                                             samples=body.get("samples"))
                # 模型配置中心。**刻意拆成三条不合并成一个 save**：密钥端点必须永远
                # 不在返回体里带出值，混进通用 save 里迟早有人顺手回显。
                elif path == "/api/config/set":       # 连接段 + 激活项（三态）
                    r = actions.set_model_config(ws_root,
                                                 providers=body.get("providers"),
                                                 defaults=body.get("defaults"))
                elif path == "/api/config/secret":    # 只写不读
                    r = actions.set_provider_secret(ws_root, env=body.get("env"),
                                                    value=body.get("value"))
                elif path == "/api/config/test":      # 零成本自检
                    r = actions.test_provider(ws_root, alias=body.get("provider"))
                else:
                    return self._404_json()
                return self._json({"ok": True, **r})
            except Exception as e:  # noqa: BLE001  业务错误以 400 + 明确文案返回前端
                return self._json({"ok": False, "error": str(e)}, code=400)

        # ---- 响应助手 ----
        def _ctype(self, p: Path) -> str:
            suf = p.suffix.lower()
            if suf in (".wav", ".mp3", ".m4a", ".aac", ".ogg"):
                # 音频按内容嗅探（provider 可能把 mp3 写进 .wav 文件）
                try:
                    with open(p, "rb") as f:
                        head = f.read(12)
                    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
                        return "audio/mpeg"
                    if head[:4] == b"RIFF":
                        return "audio/wav"
                    if head[:4] == b"OggS":
                        return "audio/ogg"
                    if head[4:8] == b"ftyp":
                        return "audio/mp4"
                except Exception:  # noqa: BLE001
                    pass
                return "audio/mpeg" if suf == ".mp3" else "audio/wav"
            return _MIME.get(suf, "application/octet-stream")

        def _json(self, data, code=200):
            # 引擎错配亮牌：只在盘上代码领先于本进程时注键——干净态载荷一字节
            # 不多，读侧（core.api）按「键在即真」消费。挂在 _json 这个唯一出口
            # 而非某几条路由上：任何页面的任何轮询都能把牌翻出来
            if isinstance(data, dict) and _engine_stale(boot_fp):
                data = {**data, "engine_stale": True}
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", code=code,
                             headers={"Cache-Control": "no-store"})

        def _404_json(self):
            self._json({"error": "not found"}, code=404)

        def _send_bytes(self, body: bytes, ctype: str, code=200, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_file(self, p: Path | None, cache=True):
            if not p or not p.is_file():
                return self._404()
            headers = {} if cache else {"Cache-Control": "no-store"}
            self._send_bytes(p.read_bytes(), self._ctype(p), headers=headers)

        def _send_index(self):
            """首页注入本次启动的 CSRF token（前端 post() 统一携带）。"""
            html = (ASSETS / "index.html").read_text(encoding="utf-8")
            meta = f'<meta name="csrf-token" content="{csrf_token}">'
            html = html.replace("</head>", f"  {meta}\n</head>", 1)
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8",
                             headers={"Cache-Control": "no-store"})

        def _send_range(self, p: Path):
            """支持 Range 的媒体流，视频/音频可拖动进度。"""
            size = p.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            code = 200
            # 单区间：`bytes=a-b` / `bytes=a-`（到文件尾）/ `bytes=-n`（最后 n 字节）；
            # 多区间与非法值按无 Range 整体返回，播放器会自行按 200 处理
            if rng and rng.startswith("bytes=") and size > 0 and "," not in rng:
                s, _, e = rng[6:].partition("-")
                try:
                    if s == "":
                        start, end = max(0, size - int(e)), size - 1
                    else:
                        start = int(s)
                        end = min(int(e) if e else size - 1, size - 1)
                    code = 206
                except ValueError:
                    start, end, code = 0, size - 1, 200
            if size == 0:
                self.send_response(200)
                self.send_header("Content-Type", self._ctype(p))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if start > end:
                # 起点越界（如播放器带着旧 size 请求已被重渲变短的文件）：
                # 按规范回 416，而不是发出负 Content-Length 的 206
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", self._ctype(p))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(p, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)

        def _404(self):
            self._send_bytes(b"not found", "text/plain", code=404)

    return Handler


def _graceful_term(*_):
    """SIGTERM → 以 0 收场。`--restart`/`--stop` 用 SIGTERM 接管本实例，是单例
    纪律下的正常换班而非故障；不装处理器时进程死于默认信号处置、退出码 143，
    包着本进程的后台任务/编排器会把每次重启都记成一次失败。抛 SystemExit(0)
    让主线程从 serve_forever 正常退栈——finally 销 pidfile 的收尾照走。"""
    raise SystemExit(0)


def serve(root: str = ".", port: int = 8787, store=None, workspace: str | None = None,
          config: str | None = None) -> None:
    if store is None:
        from ..models import ConfigStore
        # 长驻进程必须用 shared：它按 mtime 自失效，磁盘改了配置无需重启 Studio。
        # 用 load() 会把启动瞬间的配置钉死在闭包里（新画风 500、网页存的密钥不生效）
        store = ConfigStore.shared(config)
    actions.bind_config_path(config)      # 后续每次新读都读同一份配置文件
    root_path = Path(root).resolve()
    ws_root = Path(workspace).resolve() if workspace else root_path
    # 启动即收割孤儿 ffmpeg（父进程被 SIGKILL 后无人认领·实测烧过两天 CPU）：
    # Studio 是日常总入口，此刻任何合法渲染的父进程都活着（PPID≠1），收割零误伤
    from ..ffmpeg import reap_orphan_ffmpeg
    reaped = reap_orphan_ffmpeg(kill=True)
    for o in reaped:
        print(f"✕ 已收割孤儿 ffmpeg pid {o['pid']}（父进程已死仍在烧 CPU）: …{o['cmd'][-64:]}")
    csrf_token = _secrets.token_hex(16)   # 每次启动一换，注入首页、POST 必带
    handler = _make_handler(root_path, store, ws_root, csrf_token,
                            boot_fp=engine_fingerprint())
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    pf = _pidfile(ws_root)                    # 单例登记：谁在跑、哪个端口（供复用/停止/残留检测）
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"pid": os.getpid(), "port": port, "host": "127.0.0.1"}),
                  encoding="utf-8")
    n = len(scanner.library(root_path))
    np = len(scanner.workspace_summary(ws_root))
    print("▸ kinema studio")
    print(f"   扫描根目录: {root_path}")
    print(f"   工作区: {ws_root}")
    print(f"   发现: {np} 个项目 · {n} 支成片")
    print(f"   打开: http://127.0.0.1:{port}")
    print("   Ctrl+C 停止")
    signal.signal(signal.SIGTERM, _graceful_term)   # --restart/--stop 的接管以 0 收场
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()
    except SystemExit:
        print("已被 --restart/--stop 接管，正常退出。")
        raise
    finally:
        try:                                  # 退出即销 pidfile，不留陈旧登记
            pf.unlink()
        except OSError:
            pass
