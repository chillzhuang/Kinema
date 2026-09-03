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

"""火山引擎智能视觉「视频改口型」适配器（visual.volcengineapi.com · Service=cv）。

协议骨架（官方通用异步框架）：
  提交  POST ?Action=CVSync2AsyncSubmitTask&Version=2022-08-31 → data.task_id
  查询  POST ?Action=CVSync2AsyncGetResult&Version=2022-08-31  → status/产物 URL
  鉴权  Volcano OpenAPI Signature V4（AK/SK，X-Date + 凭证范围 date/region/cv/request）

**`req_key` 必须在 providers 段显式配置**：该值标识具体算法档，只在官方接口
文档（文档中心 → 智能视觉服务 → 视觉内容生成 → 视频改口型 → 接口文档）给出，
每档一值且会随版本更替——不配就报错并给出文档路径，绝不猜一个发出去。
视频与音频均以**公网 URL** 传入（字段名可按文档在 conn 覆盖，缺省
`video_url`/`audio_url`），上云由调用方完成。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import urllib.parse

from pathlib import Path

from .._util import download, poll_task, request_with_retry
from ..base import LipsyncProvider, VideoResult
from ...errors import ConfigError, ProviderError

_DOC_HINT = ("req_key 在官方接口文档给出（文档中心 → 智能视觉服务 → 视觉内容生成 → "
             "视频改口型 → 接口文档），拷贝到 config/models.yaml 的 "
             "providers.volc-lipsync.req_key")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sign(ak: str, sk: str, *, host: str, region: str, service: str,
          query: dict, body: bytes, now: datetime.datetime) -> dict:
    """Volcano OpenAPI Signature V4：返回随请求发出的鉴权头。

    与 AWS SigV4 同构，差异只在凭证范围的 service 段与 `X-Date` 头名。
    Canonical query 按键排序且值须 URL 编码——Action/Version 都是安全字符，
    但排序不能省：服务端按字典序重算。"""
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    date = xdate[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    cq = "&".join(f"{k}={urllib.parse.quote(str(v), safe='-_.~')}"
                  for k, v in sorted(query.items()))
    headers = {"host": host, "x-date": xdate,
               "x-content-sha256": payload_hash,
               "content-type": "application/json"}
    signed = ";".join(sorted(headers))
    ch = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    creq = f"POST\n/\n{cq}\n{ch}\n{signed}\n{payload_hash}"
    scope = f"{date}/{region}/{service}/request"
    to_sign = ("HMAC-SHA256\n" + xdate + "\n" + scope + "\n"
               + hashlib.sha256(creq.encode()).hexdigest())
    key = _hmac(_hmac(_hmac(_hmac(sk.encode(), date), region), service), "request")
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"X-Date": xdate, "X-Content-Sha256": payload_hash,
            "Content-Type": "application/json",
            "Authorization": (f"HMAC-SHA256 Credential={ak}/{scope}, "
                              f"SignedHeaders={signed}, Signature={sig}")}


class VolcLipsyncProvider(LipsyncProvider):
    name = "volc_lipsync"

    def __init__(self, conn: dict, store):
        self.store = store
        self.req_key = str(conn.get("req_key") or "").strip()
        self.host = conn.get("host", "visual.volcengineapi.com")
        self.region = conn.get("region", "cn-north-1")
        self.version = conn.get("version", "2022-08-31")
        self.video_field = conn.get("video_field", "video_url")
        self.audio_field = conn.get("audio_field", "audio_url")
        self.price_per_second = float(conn.get("price_per_second", 0) or 0)
        self.poll_interval = int(conn.get("poll_interval", 5))
        self.timeout = int(conn.get("timeout", 600))
        self.ak_env = conn.get("ak_env", "VOLC_ACCESS_KEY")
        self.sk_env = conn.get("sk_env", "VOLC_SECRET_KEY")

    def configured(self) -> tuple[bool, str]:
        """是否具备真发条件 →（可用, 缺什么）。调用方据此**跳过并点名**而不是
        半路抛错——口型精修是增强步，不配置不该拦住出片主链。"""
        if not self.req_key:
            return False, f"缺 req_key（{_DOC_HINT}）"
        try:
            self.store.secret(self.ak_env)
            self.store.secret(self.sk_env)
        except Exception:  # noqa: BLE001  缺密钥与密钥层报错同一处置：不可用
            return False, (f"缺视觉服务密钥（{self.ak_env}/{self.sk_env}，"
                           "火山控制台「密钥管理」生成，与 ARK_API_KEY 不是同一套）")
        return True, ""

    def _call(self, action: str, payload: dict) -> dict:
        ok, why = self.configured()
        if not ok:
            raise ConfigError(f"lipsync provider 未就绪：{why}")
        ak = self.store.secret(self.ak_env)
        sk = self.store.secret(self.sk_env)
        query = {"Action": action, "Version": self.version}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = _sign(ak, sk, host=self.host, region=self.region, service="cv",
                        query=query, body=body,
                        now=datetime.datetime.now(datetime.timezone.utc))
        url = f"https://{self.host}/?" + urllib.parse.urlencode(query)
        resp = request_with_retry("POST", url, headers=headers, data=body,
                                  timeout=60, desc=f"lipsync {action}")
        try:
            j = resp.json()
        except ValueError:
            raise ProviderError(f"视频改口型返回非 JSON（{action}）: {resp.text[:300]}")
        if resp.status_code >= 400 or (j.get("code") not in (None, 10000)):
            raise ProviderError(
                f"视频改口型 {action} 失败: {json.dumps(j, ensure_ascii=False)[:400]}",
                code=str(j.get("code") or resp.status_code))
        return j.get("data") or {}

    def generate(self, video_url: str, audio_url: str, out_path: str,
                 *, dur: float = 0.0, **kwargs) -> VideoResult:
        for label, u in (("视频", video_url), ("音频", audio_url)):
            if not str(u).startswith(("http://", "https://")):
                raise ProviderError(
                    f"视频改口型的{label}必须是公网 URL，收到：{u}\n"
                    "  启用媒体上云（config/storage.yaml media.backend=oss）后由引擎自动上传")
        data = self._call("CVSync2AsyncSubmitTask", {
            "req_key": self.req_key,
            self.video_field: video_url,
            self.audio_field: audio_url,
        })
        task_id = data.get("task_id")
        if not task_id:
            raise ProviderError(f"视频改口型未返回 task_id: {data}")

        def check():
            d = self._call("CVSync2AsyncGetResult",
                           {"req_key": self.req_key, "task_id": task_id})
            status = str(d.get("status") or "").lower()
            if status in ("done", "success", "succeeded"):
                # 产物 URL 的字段名跨档位有差：resp_data 内嵌 JSON 与顶层 video_url
                # 两种形态都见于该服务族，逐一探测、全落空按业务失败上抛
                url = d.get("video_url") or d.get("url")
                if not url and d.get("resp_data"):
                    try:
                        url = (json.loads(d["resp_data"]) or {}).get("video_url")
                    except (ValueError, TypeError):
                        url = None
                if not url:
                    raise ProviderError(f"视频改口型完成但未见产物 URL: {d}")
                return url
            if status in ("failed", "not_found", "expired"):
                raise ProviderError(f"视频改口型任务{status}: {d}",
                                    code=str(d.get("code") or ""))
            return None

        url = poll_task(check, what="视频改口型", task_id=str(task_id),
                        timeout=self.timeout, interval=self.poll_interval)
        download(url, out_path, desc="lipsync 产物")
        cost = round(self.price_per_second * max(dur, 0.0), 4)
        return VideoResult(path=str(out_path), cost=cost, has_audio=True,
                           meta={"provider": self.name, "task_id": task_id})
