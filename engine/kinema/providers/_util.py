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

"""真实云 provider 的共享工具（HTTP 重试 / 下载 / 本地文件转 data URL / 轮询心跳）。"""
from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path

# 视为瞬态、值得重试的 HTTP 状态码（限流/网关/服务端抖动）
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def poll_heartbeat(desc: str, *, interval: float = 30.0):
    """异步任务轮询期间的心跳打印器：每 `interval` 秒最多落一行「已等待 Ns」。

    视频任务单镜要在服务端渲染数分钟，轮询循环若全程静默，等待就会被读成
    卡死（任务号打完之后终端可能几分钟没有任何输出）。节流按墙钟而非轮询
    次数：轮询间隔各家不同（5s/10s），按次数节流会让心跳频率跟着 provider 走。
    输出恒为整行（不用 `\\r` 原地刷新）——这些行会进 Studio 任务日志与并发
    模式下的混排输出，整行才不会互相踩踏。
    """
    t0 = time.monotonic()
    state = {"last": t0}

    def beat() -> None:
        now = time.monotonic()
        if now - state["last"] < interval:
            return
        state["last"] = now
        print(f"    {desc} · 已等待 {int(now - t0)}s（服务端渲染中）", flush=True)

    return beat


def raise_for_poll(resp, *, what: str, task_id: str) -> None:
    """轮询响应的状态码裁决（各家 `check()` 共用，防「同一条纪律各抄一份」漂移）。

    `_RETRY_STATUS`（429/5xx）是瞬态：抛**非** ProviderError，走 `poll_task` 的
    连接类容忍带——任务在服务端照常渲染，单次抖动即弃单等于把已计费的生成变成
    无效支出（视频侧 retries=0 弃单即整批停派；图侧自动重跑则为同一张图重复计费）。
    其余 4xx 才是业务性失败，抛 ProviderError 终止轮询。"""
    if resp.status_code < 400:
        return
    msg = f"{what} 轮询 {resp.status_code}（任务 {task_id}）: {resp.text[:300]}"
    if resp.status_code in _RETRY_STATUS:
        raise RuntimeError(msg)
    from ..errors import ProviderError
    raise ProviderError(msg)


def poll_task(check, *, what: str, task_id: str, timeout: float,
              interval: float, timeout_hint: str = ""):
    """异步生成任务的轮询骨架（图/视频各家共用，防「同一条纪律各抄一份」漂移）。

    `check()` 做一次查询：完成返回产物（非 None）；进行中返回 None；业务性失败
    自行抛 ProviderError（不吃容忍带）。骨架统一三条纪律：
    · 超时按 monotonic **截止时刻**判——「只累加 sleep」的口径不含 HTTP 往返，
      网络抖动时实际等待可远超名义上限；
    · 连接类异常连续容忍 5 次——任务在服务端照常渲染，单次抖动即弃单等于把
      已计费的生成变成无效支出；
    · 心跳节流 + 首查先于任何 sleep——秒级短任务不用白等一整个 interval。"""
    from ..errors import ProviderError

    deadline = time.monotonic() + timeout
    transient = 0
    beat = poll_heartbeat(f"{what} 任务 {task_id}")
    while time.monotonic() < deadline:
        beat()
        try:
            out = check()
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            transient += 1
            if transient >= 5:
                raise ProviderError(
                    f"{what} 轮询连续失败（任务 {task_id} 仍在服务端，"
                    f"可稍后凭 id 查询）: {e}") from e
            time.sleep(interval)
            continue
        transient = 0
        if out is not None:
            return out
        time.sleep(interval)
    raise ProviderError(
        f"{what} 生成超时（>{timeout:g}s，任务 {task_id} 仍在服务端，"
        f"可稍后凭 id 查询{timeout_hint}）")


def request_with_retry(method: str, url: str, *, attempts: int = 3,
                       base_delay: float = 2.0, desc: str = "",
                       retry_read_timeout: bool | None = None, **kwargs):
    """带有界指数退避的 HTTP 请求。

    重试的是请求**未被服务端受理**的失败：连接错误、连接超时、429，以及 GET 的 5xx。
    读超时与 POST 的 5xx 语义不同——请求已送达，服务端可能已经建任务或完成生成并计费，
    盲目重发就是第二笔账。缺省只对 GET（幂等查询）重试读超时与 5xx；创建付费任务
    与同步生成的 POST 一律不重，由调用方决定是否凭任务号补查。4xx 业务错误
    立即返回给调用方自行解析（保留错误体与 X-Tt-Logid 等排障锚点）。"""
    import requests  # 惰性导入：仅真实 provider 用

    if retry_read_timeout is None:
        retry_read_timeout = method.upper() == "GET"
    # 5xx 对 POST 不重：网关 502/504 时上游可能已完成生成并计费，重发即第二笔；
    # 只有 429（服务端明确未受理）对任何方法都可重
    retry_status = _RETRY_STATUS if method.upper() == "GET" else frozenset({429})
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in retry_status and attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            return resp
        except requests.exceptions.ReadTimeout:
            if not retry_read_timeout or attempt >= attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise
    raise last_exc  # pragma: no cover  防御分支，正常流程不可达


def download(url: str, out_path: str | Path, *, timeout: int = 180,
             attempts: int = 3, headers: dict | None = None) -> None:
    """流式下载产物到临时名，完整落地后再替换到目标路径。headers 供需要鉴权头
    的产物直链（如 Veo 的 video.uri 必须带 x-goog-api-key 并跟随 302）；
    requests 默认跟随重定向。

    目标路径上只会出现完整文件：断流后留下半截产物会被断点续跑与并发层的
    「产物已在盘」判据当成成品登记。"""
    import os
    import requests  # 惰性导入：仅真实 provider 用

    out = Path(out_path)
    tmp = out.with_name(out.name + ".part")
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            os.replace(tmp, out)
            return
        except (requests.ConnectionError, requests.Timeout) as e:
            tmp.unlink(missing_ok=True)
            last_exc = e
            if attempt < attempts:
                time.sleep(2.0 * (2 ** (attempt - 1)))
                continue
            raise
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    raise last_exc  # pragma: no cover


def save_bytes(data: bytes, out_path: str | Path) -> None:
    with open(out_path, "wb") as f:
        f.write(data)


def auth_headers(conn: dict, store, *, env_key: str = "api_key_env",
                 scheme: str = "Bearer") -> dict:
    """鉴权头。连接段声明 `auth: none` 时**一个鉴权头都不发**。

    自托管端点（Ollama / ComfyUI / vLLM / SGLang 之类）通常不鉴权，而各家适配器
    一律裸调 `store.secret(required=True)`——缺 key 直接抛，于是接一个本地端点
    必须先编一把假密钥填进去。把"要不要鉴权"做成连接段的显式声明，
    本地端点无需伪造密钥即可接入。
    """
    if str(conn.get("auth") or "").lower() == "none":
        return {}
    key = store.secret(conn.get(env_key))
    return {"Authorization": f"{scheme} {key}"}


# data URL 缓存：设定集项目每镜最多 8 张参考图、逐镜把同一批设定图重复编码上传
# （10 镜 × 8 张 = 同一文件被编 80 次）。键必须含 mtime+size——用户重画设定图后
# 旧缓存立刻失效，否则症状是「改了设定图但一致性没变」，极难排查。
_DATA_URL_CACHE: dict[tuple, str] = {}
_DATA_URL_CACHE_MAX = 64      # 有界：8 张 2K 设定图的 base64 ≈ 30MB，不无限攒


def _image_mime(p: Path) -> str:
    """图片的 data URL mime：先按内容嗅探，认不出才回落扩展名。

    扩展名会说谎——生图 provider 把 JPEG 字节写进 `.png` 是常见形态，而接口按
    声明的 mime 解码。同款嗅探在参考音那条路上已经存在（`seedance._audio_url`
    的注释记着「provider 可能把 mp3 写进 .wav」），图片这侧同样的暴露面。"""
    head = p.read_bytes()[:12]
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return mimetypes.guess_type(p.name)[0] or "image/png"


def file_to_data_url(path: str | Path) -> str:
    """本地图片转 data URL，用作参考图（多数云图像编辑接口接受 data URL）。
    并发说明：dict 读写在 GIL 下原子，竞态最坏是同一文件编两次，结果一致无害。"""
    p = Path(path)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None                      # 读不到元数据（将在 read_bytes 处如实报错）
    if key is not None and key in _DATA_URL_CACHE:
        return _DATA_URL_CACHE[key]
    mime = _image_mime(p)
    b64 = base64.b64encode(p.read_bytes()).decode()
    url = f"data:{mime};base64,{b64}"
    if key is not None:
        if len(_DATA_URL_CACHE) >= _DATA_URL_CACHE_MAX:
            _DATA_URL_CACHE.clear()
        _DATA_URL_CACHE[key] = url
    return url
