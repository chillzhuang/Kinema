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

"""MiniMax H3 图生视频（v2 全模态接口）。

与上一代 Hailuo（v1）是**两套完全不同的协议**，不是换个 model 串就能复用：
  · 端点 `/v2/video_generation`，查询 `/v2/query/video_generation/{task_id}`（走 path）
  · 请求体是 `content[]` 多模态数组 + `role` 标注，而不是 v1 那种扁平
    `prompt` + `first_frame_image`
  · 取片少一段——查询直接给 `task.content.url`，不必再拿 file_id 换下载地址
  · 首帧 / 尾帧 / 首尾帧同时全支持（v1 只有首帧）
  · 分辨率枚举只有 768P / 2K；时长 4~15 秒且只收整数
  · 输出自带原生立体声（与画面由同一个模型联合生成）

⚠ 上线前务必核对官方文档并先跑一镜小样：
https://platform.minimax.io/docs/api-reference/video-generation-v2-create
（国内站 https://platform.minimaxi.com/... 路径与契约逐字一致，模型 ID 也相同，
 但两站账号与计费独立，key 不要交叉用。）

⚠ v2 API **没有尾帧回传**：创建接口请求体只有 model/content/resolution/duration/
  ratio/callback_url，无 return_last_frame 类参数（对照官方 API 参考核实）；
  `role=last_frame` 是首尾帧模式的**输入**引导图，不是输出。且官方明载首尾帧模式
  与参考素材模式互斥、本适配器亦未接参考族通道（content 另收 role=reference_image
  ≤9 张 / reference_video ≤3 段 / reference_audio ≤3 段音色跟随，与首尾帧不可同发）
  ——尾帧接力（tail_relay）对 H3 取回与附发两头都不成立，引擎按能力位
  （base 缺省 False）自动失效并点名（cli._warn_no_tail_relay）。

素材地址官方支持三种形态：公网 URL / `mm_file://{file_id}` / `data:image/<格式>;base64,<数据>`。
本适配器用 data-url，于是**不开对象存储也能跑**。代价是 base64 会把体积撑大约三分之一，
而单图上限 30MB、整个请求体上限 64MB——大图多帧时要留意这条。

⚠ 两站的一处真差异：国内站的创建接口另有可选的 `aigc_watermark`（布尔，缺省 false），
国际站没有这个字段。需要平台合规标识时经连接段透传，或在成片侧自行叠加。
"""
from __future__ import annotations

import math
from pathlib import Path

from .. import grades
from ..base import VideoProvider, VideoResult, resolution_prices
from .._util import auth_headers, download, file_to_data_url, poll_task, \
    raise_for_poll, request_with_retry
from ...errors import ProviderError

# 官方枚举：分辨率两档、时长 4~15 的整数、比例七档
# 档位取自 providers/grades.py 那份目录——配置中心的下拉与这里的发前归一必须是
# 同一组值，否则界面上选得出的档，发出去会被自己归一掉
RESOLUTIONS = grades.values_of("minimax_video")
DUR_MIN, DUR_MAX = 4, 15
RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
# 查询返回的状态机（小写，与 v1 的首字母大写不同——照抄 v1 的判断会永远等不到完成）
_DONE, _FAIL = "succeeded", ("failed", "cancelled")


def _ratio_of(width: int, height: int) -> str:
    """画布尺寸 → 官方 ratio 枚举。给不出精确档时回 adaptive（图生视频本就按输入图判定）。"""
    if not width or not height:
        return "adaptive"
    r = width / height
    best = min(RATIOS[1:], key=lambda s: abs(r - (int(s.split(":")[0]) / int(s.split(":")[1]))))
    bw, bh = (int(x) for x in best.split(":"))
    return best if abs(r - bw / bh) < 0.06 else "adaptive"


class MiniMaxVideoProvider(VideoProvider):
    name = "minimax_video"

    def __init__(self, conn: dict, store):
        self.conn = conn
        self.store = store
        self.base = conn.get("base_url", "https://api.minimax.io/v2").rstrip("/")
        self.model = conn.get("model", "MiniMax-H3")
        self.api_key_env = conn.get("api_key_env", "MINIMAX_API_KEY")
        res = str(conn.get("resolution", "768P")).upper()
        self.resolution = res if res in RESOLUTIONS else "768P"
        self.poll_interval = float(conn.get("poll_interval", 10))    # 官方建议 10s
        self.timeout_s = float(conn.get("timeout", 900))
        self.price_per_second = float(conn.get("price_per_second", 0.0))
        self.price_per_second_4k = float(conn.get("price_per_second_4k", 0.0))
        self.price_by_resolution = resolution_prices(conn)

    # 时长按整秒钳进官方区间。**不是四舍五入了事**——低于 4s 或高于 15s 会被直接拒，
    # 而分镜时长是创作决定，静默改成别的值会让成片与时间轴对不上，所以钳制要喊一声。
    def billable_seconds(self, dur: float, *, dubbed: bool = False,
                         last_frame: bool = False) -> int:
        # last_frame 不参与取档（H3 首尾帧不改时长档位），仅为通用契约收下
        n = max(1, math.ceil(dur) if dubbed else round(dur))
        return max(DUR_MIN, min(DUR_MAX, n))

    def input_video_seconds(self, ref_seconds: float) -> int:
        """输入视频的秒数**同样入账**（官方与输出同价计）。"""
        return max(0, math.ceil(ref_seconds or 0))

    def _url(self, path: str, *, what: str) -> str:
        """素材地址：公网 URL 直接用，本地文件转 data-url。

        视频参考走公网 URL 是硬要求（与本工程既有的 Seedance V2V 同一形态）；
        图片的 base64 支持尚未核实，见模块头。
        """
        if str(path).startswith(("http://", "https://", "data:")):
            return str(path)
        p = Path(path)
        if not p.is_file():
            raise ProviderError(f"{what}不存在: {path}")
        return file_to_data_url(str(p))

    def generate(self, image, out_path, *, prompt="", dur=5.0, width=1080, height=1920,
                 seed=None, last_frame=None, ref_audio=None, **kwargs) -> VideoResult:
        # dubbed 模式会带一条对口型音轨进来。**必须显式拒**：v2 的音频通道是
        # `role=reference_audio`（音色跟随参考，≤3 段、总时长 ≤15s），语义不是
        # 逐帧对口型；且官方明载 reference_* 与首尾帧两种模式互斥——本适配器走
        # 首尾帧任务，「首帧 + 参考音频」在协议层就不成立。不拒的话它会被
        # `**kwargs` 静默吞掉：不报错、口型对不上、H3 的原生立体声还与我们的
        # TTS 撞轨，而钱照烧（与同目录 veo 同一道闸）。
        if ref_audio:
            raise ProviderError(
                "MiniMax H3 无法承接对口型音轨：v2 的 reference_audio 是音色跟随参考"
                "（非对口型），且与本适配器所走的首尾帧模式互斥（官方明载）。\n"
                "  · 要对口型（dubbed）：用 seedance（`gen-video --video-provider seedance`）\n"
                "  · H3 输出自带原生立体声，适合 native 模式（`-m b`）")
        # auth: none 的自托管端点不发鉴权头（见 _util.auth_headers）
        auth = auth_headers(self.conn, self.store)
        secs = self.billable_seconds(dur, dubbed=bool(kwargs.get("dubbed")))
        if secs != round(dur):
            print(f"  ⚠ MiniMax H3 时长只收 {DUR_MIN}~{DUR_MAX} 秒的整数，"
                  f"本镜 {dur}s 已钳到 {secs}s（成片会与时间轴差这一点，必要时改分镜）")

        # content[] 把提示词、首帧、尾帧、参考素材收进同一个数组、靠 role 区分——
        # 与 Seedance 的扁平 first_frame/last_frame 字段是完全不同的形状。
        # 至少要有一个非空 text 项，这是官方硬要求。
        content = [{"type": "text", "text": prompt or "保持画面主体不变，自然轻微的运动。"}]
        if image:
            content.append({"type": "image_url", "role": "first_frame",
                            "image_url": {"url": self._url(image, what="首帧图")}})
        if last_frame:
            content.append({"type": "image_url", "role": "last_frame",
                            "image_url": {"url": self._url(last_frame, what="末帧图")}})
        # 再过一次白名单：`--resolution` 走的是 `_apply_resolution` 直接赋值，
        # 绕开构造器那道闸，而它的档位名（480p/720p/1080p/4k）**没有一个**是 H3 的
        # 合法值（官方只有 768P / 2K）。不在这里归一，就是一次必被拒的请求。
        res = str(self.resolution or "").upper().replace("P", "P")
        if res not in RESOLUTIONS:
            fallback = "2K" if res in ("1080P", "4K", "2048P") else "768P"
            print(f"  ⚠ MiniMax H3 只有 {'/'.join(RESOLUTIONS)} 两档，"
                  f"'{self.resolution}' 已归一为 {fallback}")
            res = fallback
        body = {"model": self.model, "content": content,
                "resolution": res, "duration": secs}
        ratio = _ratio_of(width, height)
        if not image:                       # 文生视频时 ratio 必填且不能是 adaptive
            body["ratio"] = ratio if ratio != "adaptive" else "16:9"
        # seed 刻意不发：官方创建接口的请求体里没有这个字段（H3 不提供种子复现）。
        # 画面锚定交给首帧图，而不是发一个 schema 之外的参数去赌服务端会忽略它。

        task_id = self._create(body, auth)
        url = self._await(task_id, auth)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        download(url, Path(out_path))
        cost = round(secs * self.effective_price_per_second, 6) \
            if self.effective_price_per_second > 0 else 0.0
        return VideoResult(path=str(out_path), cost=cost,
                           # H3 的输出自带与画面联合生成的原生立体声，官方 API 参考页
                           # 没有关闭音轨的开关，故按有音轨处理
                           has_audio=True,
                           meta={"task_id": task_id, "model": self.model,
                                 "resolution": self.resolution, "seconds": secs})

    def _create(self, body: dict, auth: dict) -> str:
        try:
            resp = request_with_retry(
                "POST", f"{self.base}/video_generation", json=body,
                headers={**auth, "Content-Type": "application/json"},
                timeout=120, desc="minimax h3 create")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"MiniMax H3 建任务失败: {e}") from e
        if resp.status_code >= 400:
            # 把高频成因列出来，省得用户在提示词、时长与素材之间挨个猜
            hint = ("\n  · 常见成因：素材超限（单图 ≤30MB、请求体总计 ≤64MB，"
                    "data-url 会把体积撑大约三分之一）· 图片宽高需在 [256,5760]、"
                    "宽高比 5:2~2:5 · 时长须为 4~15 的整数 · 分辨率只有 768P / 2K"
                    if resp.status_code == 400 else "")
            raise ProviderError(f"MiniMax H3 {resp.status_code}: {resp.text[:500]}{hint}")
        j = resp.json()
        # v2 的成功响应是裸 task_id，没有 v1 那层 base_resp 包装；错误则是
        # {"type":"error","error":{...}}
        tid = j.get("task_id")
        if not tid:
            raise ProviderError(f"MiniMax H3 未返回 task_id: {resp.text[:500]}")
        print(f"  · MiniMax H3 任务号 {tid}（断线可凭它找回，不必重发）")
        return str(tid)

    def _await(self, task_id: str, auth: dict) -> str:
        # 轮询三纪律统一在 _util.poll_task：本厂由此补齐 monotonic 截止、
        # 瞬态容忍（单次抖动不弃掉服务端照常计费的任务）与「首查先于 sleep」
        def check():
            resp = request_with_retry(
                "GET", f"{self.base}/query/video_generation/{task_id}",
                headers=dict(auth), timeout=60, desc="minimax h3 poll")
            raise_for_poll(resp, what="MiniMax H3", task_id=task_id)
            task = (resp.json() or {}).get("task") or {}
            status = str(task.get("status") or "").lower()
            if status == _DONE:
                url = ((task.get("content") or {}).get("url"))
                if not url:
                    raise ProviderError(f"MiniMax H3 任务 {task_id} 完成但没有产物地址")
                return url
            if status in _FAIL:
                raise ProviderError(
                    f"MiniMax H3 任务 {task_id} {status}: {task.get('error') or ''}")
            return None

        return poll_task(check, what="MiniMax H3", task_id=task_id,
                         timeout=self.timeout_s, interval=self.poll_interval,
                         timeout_hint="，不必重新发起（重发会再计一次费）")
