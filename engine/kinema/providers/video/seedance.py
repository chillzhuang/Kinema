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

"""Seedance 图生视频 provider（字节 · 火山方舟 Ark，异步任务）。

推荐主力视频：多镜头人物一致性领先、原生音频、首尾帧，与 Seedream/豆包共用 ARK_API_KEY。
首帧驱动：以 image 为首帧锁定画面与风格，prompt 只写运动/运镜。

流程：建任务(POST tasks) → 轮询(GET tasks/{id}) → 取 video_url 下载。
连接来自 config/models.yaml 的 providers.seedance；密钥 ARK_API_KEY。

**2.0 参数走顶层 JSON，不是 1.x 的 `--suffix` flag**（官方 82379/1366799）：
`resolution` / `ratio` / `duration` / `seed` / `generate_audio` / `watermark`
（`camera_fixed` 下划线；**2.0 无 `--fps`**，固定 24fps）。若对 2.0 模型
仍拼 1.x 后缀串，`--fps 24` 会被静默忽略或误当成提示词正文的一部分。

**content[] 的 type/role 全集**：`text` · `image_url`(first_frame|last_frame|
reference_image) · `video_url`(**reference_video**) · `audio_url`(reference_audio)。
限额 ≤9 图 + 3 视频 + 3 音频；参考视频 mp4/mov · 2–15s · ≤200MB · 24–60fps。

**视频参考（V2V）三条硬约束**（与图片不同，踩了就是无效计费一次建任务）：
  ① **必须公网 URL**——base64/data-url/本地路径对视频一律被拒（图片仍可 data-url）。
     故 `_vid_url()` 只透传 http(s)，本地路径直接抛错引导启用 OSS，**刻意不做
     data-url 兜底**：兜底只会把「拼错请求」变成「服务端 400」，更难查。
     URL 解析的职责在 CLI 层（`stage_gen_video` 经 `MediaStore.upload` 取明文
     公网 URL），provider 只做透传与校验，单一职责。
  ② **计费含输入视频秒**——2.0 是 token 计费（≈(输入秒+输出秒)×W×H×fps/1024），
     而 `billable_seconds()` 按契约只算**输出**秒（它同时是请求体的 `duration`）。
     输入侧另走 `input_video_seconds()`，两者相加才是这一次调用的真实花费；
     只算输出会让台账系统性少记（一段 5s previz ≈ 少记 5 元/次）。
  ③ **参考视频传的是「运动/运镜/编舞/节奏」而非「风格/主体」**——所以 Mode B
     恒同时发 `reference_image`（锁外观/构图）+ `reference_video`（锁运动），
     风格由文案锁。V2V 分支下**不发首/末帧**（角色一致性由 reference_image 承担）。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from ..base import VideoProvider, VideoResult, resolution_prices
from .._util import download, file_to_data_url, poll_task, raise_for_poll, request_with_retry
from ...errors import ProviderError

# 参考视频（V2V）官方时长区间：短于 2s / 长于 15s 会被拒
REF_VIDEO_MIN_SEC = 2
REF_VIDEO_MAX_SEC = 15


def _ratio(w: int, h: int) -> str:
    if w == h:
        return "1:1"
    return "16:9" if w > h else "9:16"


def _img_url(path: str) -> str:
    return path if str(path).startswith(("http", "data:")) else file_to_data_url(path)


def _vid_url(path: str) -> str:
    """参考视频只接受公网 http(s) URL——本地路径/data-url 一律抛错并给出解锁路径。

    **不做 data-url 兜底**：视频参考在服务端就是拒收的，兜底等于把一个可以在
    本地立刻讲清楚的配置问题，换成一次「建任务 400」——那时钱虽没花，但错误
    信息在服务端措辞里，用户不知道该去开 OSS。
    """
    s = str(path)
    if s.startswith(("http://", "https://")):
        return s
    raise ProviderError(
        f"Seedance 参考视频必须是公网 URL，收到本地路径：{s}\n"
        "  ① 启用媒体上云：config/storage.yaml 的 media.backend 设为 oss"
        "（provider=aliyun，桶需 public-read）\n"
        "  ② 或先 `python3 -m kinema oss sync` 把该章节媒体传上去再重跑\n"
        "  （图片可以走 data-url，视频不行——这是 Seedance 侧的限制）")


def _audio_url(path: str) -> str:
    """把本地音频转 data URL（按内容嗅探 mime；provider 可能把 mp3 写进 .wav）。"""
    import base64
    if str(path).startswith(("http", "data:")):
        return path
    b = Path(path).read_bytes()
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        mime = "audio/mpeg"
    elif b[:4] == b"RIFF":
        mime = "audio/wav"
    elif b[:4] == b"OggS":
        mime = "audio/ogg"
    else:
        mime = "audio/mpeg"
    return f"data:{mime};base64," + base64.b64encode(b).decode()


# 输入图被判疑似真人时的错误码前缀。该判据在模型侧的输入分类器上，重跑不会改变
# 结果，调用方据此走处置方案而非重试。判据与官方通道详见
# `docs/kinema/seedance-face-policy.md`。
FACE_POLICY_CODE = "InputImageSensitiveContentDetected"

# 成片渲染后被输出侧审核拦下的错误码前缀（任务 failed，轮询期上抛）。审的是
# 生成出来的画面内容，与输入参考装配无关——降级换参考与同参数重跑都改变不了
# 判定，调用方按「改内容」口径处置，不走降级轮。
OUTPUT_POLICY_CODE = "OutputVideoSensitiveContentDetected"


def _create_error(resp, content: list[dict]) -> ProviderError:
    """建任务 4xx → 带结构化错误码的 ProviderError。

    人脸拦截另附被拒输入图的身份：官方只报 `content[N]` 下标，而首帧、末帧、设定图
    与参考音同处一个数组，下标本身无从对照。"""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    code = str(err.get("code") or "") or None
    body = resp.text[:500]
    if code and code.startswith(FACE_POLICY_CODE):
        hit = _rejected_refs(str(err.get("message") or ""), content)
        where = f"（被拒的输入图：{'、'.join(hit)}）" if hit else ""
        return ProviderError(
            f"Seedance 输入图审核未通过{where}：{body}", code=code)
    return ProviderError(f"Seedance {resp.status_code}: {body}", code=code)


def _rejected_refs(message: str, content: list[dict]) -> list[str]:
    """把 `content[N]` 下标翻译成输入图身份（role 优先，其次 type）。"""
    out = []
    for idx in re.findall(r"content\[(\d+)\]", message):
        i = int(idx)
        if 0 <= i < len(content):
            item = content[i]
            out.append(str(item.get("role") or item.get("type") or f"content[{i}]"))
    return list(dict.fromkeys(out))


class SeedanceProvider(VideoProvider):
    name = "seedance"
    supports_reference_video = True      # 2.0 全面 GA 起支持 role=reference_video
    supports_reference_images = True     # 2.0 起 content[] 支持多张 role=reference_image
    supports_ref_audio = True            # dubbed 对口型（audio_url 参考媒体）唯一支持方
    supports_return_last_frame = True    # 尾帧回传（return_last_frame → last_frame_url）

    def __init__(self, conn: dict, store):
        self.store = store
        self.base = conn.get("base_url", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
        self.model = conn.get("model", "doubao-seedance-2-0-mini-260615")
        self.api_key_env = conn.get("api_key_env", "ARK_API_KEY")
        self.price_per_second = float(conn.get("price_per_second", 1.0))
        self.price_per_second_4k = float(conn.get("price_per_second_4k", 0))  # 0=未配→回落基准价
        self.price_by_resolution = resolution_prices(conn)
        # 兜底档取 **720p**（CLI --resolution 可运行时覆盖）：它是本适配器服务的全部型号
        # 都开放的唯一公共档，而 fast/mini 不开放 1080p——兜底到 1080p 的后果是
        # 新加一个别名忘了写 resolution，第一次调用就换回一个远端 400，且单价还高一档。
        self.resolution = conn.get("resolution", "720p")
        # ── 以下五项是**按别名声明的模型能力**，不在适配器里写死型号 ──
        # 同一份 seedance 实现要服务 2.0 系列与 2.5，两代的合法参数集并不相同；
        # request body 走的是官方**强校验**（参数不合模型即报错），所以差异必须
        # 由 models.yaml 的别名字段承载，新增型号只改 YAML。
        # 单次生成时长档位：2.0/mini `[4,15]`，2.5 `[4,30]`。
        # 钳错不报错、只静默截断——钱按截断后的秒数付，人却以为拿到了更长的片段。
        self.min_duration = int(conn.get("min_duration", 4))
        self.max_duration = int(conn.get("max_duration", 15))
        # 合法分辨率档白名单（空=不校验）。各代开放的档位不同（fast/mini 只到
        # 720p，2.5 到 1080p），配错档换回的是一个远端 400，
        # 本地先拦更省一次往返。**空白名单等于这道闸整条不生效**，别名务必配全。
        self.resolutions = tuple(conn.get("resolutions") or ())
        # 提示词分段的时间标记口径（能力位说明见 providers.base.VideoProvider）：
        # 2.0 系列配 `shot`、2.5 配 `second`。缺省沿用基类的保守值。
        self.timeline_unit = str(
            conn.get("timeline_unit", type(self).timeline_unit)).strip().lower()
        # 宽高比模式：`adaptive` 表示该型号**在受限任务类型上**只接受 adaptive
        # （2.5 的首帧/首尾帧、视频编辑、视频延长三类），并非全局恒发 adaptive
        # ——参考生视频任务官方允许指定 `16:9`，而那一档 adaptive 的语义是
        # 「模型按 prompt 自选比例」，画布锁 16:9 时可能拿回 9:16。约束按任务类型
        # 判（见 generate 里的 ratio 决策），2.0 系列两者皆可、恒走 explicit。
        self.ratio_mode = str(conn.get("ratio_mode", "explicit")).strip().lower()
        # `seed` / `camera_fixed` 仅 1.x~2.0 一侧支持，2.5 的参数表里没有这两项。
        self.supports_seed = bool(conn.get("supports_seed", True))
        self.supports_camera_fixed = bool(conn.get("supports_camera_fixed", True))
        # 末帧槽：`fast` 档接受 first_frame 但**静默丢弃 last_frame**（服务端不报错），
        # 与 body 级参数不同——它藏在 content[] 的 role 里，本地不判就永远发现不了。
        self.supports_last_frame = bool(conn.get("supports_last_frame", True))
        # 音色锚定参考音限额（全模态参考的 audio_url 条目）：2.0 系列 ≤3 条且合计
        # ≤15s，2.5 放宽到 10 条/30s（别名覆写）。**总时长超限是建任务 400**
        # （7.2s+12.7s 两条即被拒），发送侧按 max_ref_audio_seconds 预裁。
        self.max_ref_audios = int(conn.get("max_ref_audios", 3))
        self.max_ref_audio_seconds = float(conn.get("max_ref_audio_seconds", 15))
        self.poll_interval = int(conn.get("poll_interval", 5))
        # 超时按墙钟截止时刻判（_poll 用 monotonic deadline，含 HTTP 往返），
        # 比只累加 sleep 的口径更严——上限相应给足：15s 长镜高峰期排队+渲染本就漫长
        self.timeout = int(conn.get("timeout", 1200))

    def _headers(self, key):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def billable_seconds(self, dur: float, *, dubbed: bool = False,
                         last_frame: bool = False) -> int:
        """**输出**计费秒数（同时是请求体的 `duration`）。整秒，档位按别名配置钳。
        `last_frame` 不参与取档（Seedance 首尾帧不改时长档位），仅为通用契约收下。

        区间取 `min_duration`~`max_duration`（models.yaml 的别名字段，缺省 4~15s
        ＝2.0/mini 档；2.5 配 20）——**别把代次写死在这里**：钳错的后果不是报错而是
        静默截断，请求 20s 拿回 15s 的片段、账单也按 15s 出，人还以为拿到了 20s。
        对口型取上整：5.2s 配音请求 6s——宁多尾帧不截话；首帧驱动仍四舍五入贴近
        分镜节奏。**签名是 provider 通用契约**（veo 同形，cli 的 `_plan_cost`
        逐 provider 调它），V2V 的输入视频秒**不并进这里**——那会把「请求时长」
        和「计费秒数」两个不同的量搅成一个，请求体立刻发错。见 `input_video_seconds`。
        """
        n = math.ceil(dur) if dubbed else round(dur)
        return max(self.min_duration, min(self.max_duration, n))

    def input_video_seconds(self, ref_seconds: float) -> int:
        """V2V **输入**视频的计费秒数（整秒，钳到官方 2~15s 区间）。

        2.0 是 token 计费且输入视频秒同样入账，`billable_seconds` 只算输出——
        两者相加才是一次 V2V 调用的真实花费。给 0/None 直接回 0（未开 V2V）。
        """
        if not ref_seconds or ref_seconds <= 0:
            return 0
        return max(REF_VIDEO_MIN_SEC, min(REF_VIDEO_MAX_SEC, math.ceil(ref_seconds)))

    def generate(self, image, out_path, *, prompt="", dur=5.0, width=1080, height=1920,
                 seed=None, last_frame=None, ref_audio=None,
                 reference_video=None, reference_video_seconds=0.0,
                 ref_images=None, reference_only=False,
                 voice_anchors=None, **kwargs) -> VideoResult:
        """图生视频。五种用法（按 content[] 的 role 组合区分）：
          · 首帧驱动/首尾帧衔接（native）：image=首帧, last_frame=末帧, prompt 含台词则原生配音。
          · 配音对口型（dubbed）：传 ref_audio（我们的固定音色音频）→ 参考媒体模式，
            角色对该音轨口型。⚠ 参考媒体(ref_audio)与首/末帧互斥，dubbed 下 last_frame 忽略。
          · **参考生视频（reference_only=True，全能参考）**：image 与 ref_images 全部
            挂 `role=reference_image`（分镜图/简笔板/角色设定图），无首/末帧槽——
            native 里要附板只有这一条路（首帧任务与参考媒体官方互斥，实测 400），
            台词仍写进 prompt 原生配音。此模式下 last_frame 忽略。
          · **音色锚定（voice_anchors，仅全能参考合法）**：逐条角色音色样本挂
            `role=reference_audio`，模型按提示词里的「参考音频N」绑定用该嗓音念
            台词（与 ref_audio 的对口型语义不同：音频是嗓音样本、不是台词音轨）。
            实测（2.0-mini）：输出人声性别/声区跟随锚定，双音频按编号
            分绑角色与旁白成立；限额 `max_ref_audios` 条、合计
            `max_ref_audio_seconds` 秒（超限=建任务 400，调用方预裁）。
          · **参考视频 V2V（Mode B，3D 导演控制台的主路径）**：传 reference_video
            （**已解析好的公网 URL**）→ image 改挂 `reference_image` 锁外观/构图、
            视频挂 `reference_video` 迁移运镜/走位/节奏。此分支**不发首/末帧**。
          · V2V + 对口型：三者同发（2.0 放宽了互斥），运动迁移与口型可能互相牵制，
            由调用方决定是否启用。

        `reference_video_seconds` 只影响**计费**（输入视频秒同样入账），不进请求体。
        """
        key = self.store.secret(self.api_key_env)
        n = self.billable_seconds(dur, dubbed=bool(ref_audio))
        content = [{"type": "text", "text": (prompt or "").strip()}]
        if reference_video:     # V2V（Mode B）：外观锁于图、运动锁于视频、风格锁于文案
            content.append({"type": "image_url", "image_url": {"url": _img_url(image)},
                            "role": "reference_image"})
            content.append({"type": "video_url",
                            "video_url": {"url": _vid_url(reference_video)},
                            "role": "reference_video"})
            if ref_audio:
                content.append({"type": "audio_url",
                                "audio_url": {"url": _audio_url(ref_audio)},
                                "role": "reference_audio"})
        elif ref_audio:  # dubbed：参考媒体模式（图 + 音频都当参考，模型对口型）
            content.append({"type": "image_url", "image_url": {"url": _img_url(image)},
                            "role": "reference_image"})
            content.append({"type": "audio_url", "audio_url": {"url": _audio_url(ref_audio)},
                            "role": "reference_audio"})
        elif reference_only:   # 参考生视频（全能参考）：分镜图领衔，板/设定图随 ref_images 追加
            content.append({"type": "image_url", "image_url": {"url": _img_url(image)},
                            "role": "reference_image"})
        else:          # native/首尾帧：首帧驱动（可选末帧衔接）
            content.append({"type": "image_url", "image_url": {"url": _img_url(image)},
                            "role": "first_frame"})
            if last_frame:
                # 与首帧模式禁附参考图同款硬拦：能力面的仲裁在 `cli._shot_plan`，
                # 走到这里说明有调用方绕过了它。服务端对不认识的 role 只丢不报，
                # 静默发出去的后果是提示词按「收束在末帧上」写、成片却是自由发挥。
                if not self.supports_last_frame:
                    raise ProviderError(
                        f"{self.model} 不支持末帧（role=last_frame 会被服务端静默丢弃）"
                        "——调用方须先查 `supports_last_frame` 再决定发不发；"
                        "要首尾帧衔接请改用 seedance-mini / seedance-2.5")
                content.append({"type": "image_url", "image_url": {"url": _img_url(last_frame)},
                                "role": "last_frame"})
        # 额外参考图（如简笔分镜板）：追加 role=reference_image 项——**只在参考媒体模式
        # （dubbed·ref_audio 在场）合法**。官方铁律与 ref_audio 同源：first/last frame
        # 不能与 reference media 混发（native 首帧附板实测 400 InvalidParameter），故
        # 首帧分支直接抛错：静默丢图会让提示词里的「所附分镜板」指向一个不存在的
        # 参考（仲裁在 cli._shot_plan，走到这里说明有调用方绕过了它）。
        # **V2V 分支刻意不追加**——sketch 与 previz 互斥（cli._shot_plan 仲裁），V2V
        # 在场时调用方根本不会传 ref_images，这里再挂就是给"互斥"开了个后门。
        # 官方全图限额 ≤9 张，这里钳到 7。提示词必须同步声明每张参考图的职责（板=运动脚本）。
        if ref_images and not reference_video:
            if not (ref_audio or reference_only):
                raise ProviderError(
                    "Seedance 首帧模式禁止附加参考图——官方拒绝 first/last frame "
                    "与 reference media 混发（400 InvalidParameter）。附板走参考媒体"
                    "模式（dubbed 的 ref_audio）或参考生视频模式（reference_only=True）；"
                    "首帧模式只发分段时间轴（纯文本）")
            for r in list(ref_images)[:7]:
                content.append({"type": "image_url", "image_url": {"url": _img_url(r)},
                                "role": "reference_image"})
        # 音色锚定样本：追加在全部图片之后——提示词的「参考音频N」按 audio_url
        # 出现顺序编号，与图片编号互相独立。**只在全能参考合法**：首帧任务禁混
        # 参考媒体（与 ref_images 同一条官方铁律），dubbed 的 ref_audio 是成品
        # 台词音轨、语义为对口型，再叠音色样本会让两条音频互相打架。
        if voice_anchors:
            if not reference_only:
                raise ProviderError(
                    "Seedance 音色锚定（voice_anchors）只在全能参考模式（reference_only）"
                    "下合法——首帧/首尾帧任务官方禁混参考媒体，dubbed 的 ref_audio "
                    "已是成品台词音轨；调用方仲裁在 cli.stage_gen_video")
            if len(voice_anchors) > self.max_ref_audios:
                raise ProviderError(
                    f"音色锚定 {len(voice_anchors)} 条超过 {self.model} 限额 "
                    f"{self.max_ref_audios} 条——多发不是截断而是与提示词绑定句错位；"
                    "调用方须按 provider.max_ref_audios 先裁（cli 侧已有此闸）")
            for a in voice_anchors:
                content.append({"type": "audio_url",
                                "audio_url": {"url": _audio_url(a)},
                                "role": "reference_audio"})

        # 顶层参数（**不是** 1.x 的 `--suffix` 后缀串；2.0/2.5 无 --fps，固定 24fps）。
        # 逐项按别名能力位裁剪：request body 是强校验，多发一个该型号不认的参数即报错。
        if self.resolutions and self.resolution not in self.resolutions:
            raise ProviderError(
                f"{self.model} 不支持分辨率档 {self.resolution}（合法档：{'/'.join(self.resolutions)}）"
                "——改 models.yaml 该别名的 resolution，或用 --resolution 指定合法档")
        # 宽高比按**任务类型**定，不按型号一刀切（官方约束就是分任务类型写的）：
        #   · 首帧/首尾帧、视频编辑/延长 → 受限型号只收 adaptive（输出跟随首帧/原视频）
        #   · 参考生视频（只挂 reference_image / reference_audio）→ 可指定具体比例，
        #     且此档的 adaptive = 模型按 prompt 自选比例，画布锁死时必须显式给，
        #     否则 16:9 的画布可能拿回 9:16。
        restricted = bool(reference_video) or not (ref_audio or ref_images or reference_only)
        ratio = ("adaptive" if (self.ratio_mode == "adaptive" and restricted)
                 else _ratio(width, height))
        body = {"model": self.model, "content": content,
                "resolution": self.resolution, "ratio": ratio,
                "duration": n,
                "generate_audio": bool(kwargs.get("generate_audio", True)),
                "watermark": False}
        if seed is not None and self.supports_seed:
            body["seed"] = int(seed)
        if kwargs.get("camera_fixed") and self.supports_camera_fixed:
            body["camera_fixed"] = True
        if kwargs.get("return_last_frame"):   # 尾帧回传：接力续片用，见 meta["last_frame_url"]
            body["return_last_frame"] = True

        try:
            resp = request_with_retry(
                "POST", f"{self.base}/contents/generations/tasks",
                json=body,
                headers=self._headers(key), timeout=60, desc="seedance create")
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Seedance 建任务失败: {e}") from e
        if resp.status_code >= 400:
            raise _create_error(resp, content)
        task_id = resp.json().get("id")
        if not task_id:
            raise ProviderError(f"Seedance 未返回任务 id: {resp.text[:300]}")
        # 任务号立即可见：任务创建即开始计费，若后续轮询/下载最终失败，
        # 可凭此 id 到控制台找回产物，不至于无效计费
        print(f"    Seedance 任务: {task_id}")

        video_url, last_url = self._poll(task_id, key)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        download(video_url, out_path)
        # V2V 计费含输入视频秒（token 制）——只算输出会让台账每次少记一整段 previz 的钱
        in_n = self.input_video_seconds(reference_video_seconds) if reference_video else 0
        meta = {"provider": "seedance", "model": self.model,
                "resolution": self.resolution, "task_id": task_id}
        if last_url:
            # 官方尾帧回传（`return_last_frame`）：拿它当下一段的首帧即可无缝续片。
            # 尾帧在官方受信模型产物清单内（有效期内含人脸也可作输入，见
            # docs/kinema/seedance-face-policy.md §3）。URL 有时效，要接力就
            # 即时用；落盘副本供跨轮重投。
            meta["last_frame_url"] = last_url
        if reference_video:
            meta.update(reference_video=reference_video,
                        input_seconds=in_n, output_seconds=n)
        return VideoResult(path=str(out_path),
                           cost=round(self.effective_price_per_second * (n + in_n), 4),
                           has_audio=True, meta=meta)

    def _poll(self, task_id, key) -> tuple[str, str | None]:
        """轮询到成功，返回 `(video_url, last_frame_url | None)`。

        尾帧只在建任务时传了 `return_last_frame` 才有；没传就是 None。
        轮询三纪律（monotonic 截止/瞬态容忍/心跳）统一在 `_util.poll_task`，
        此处只保留本厂的状态解析。
        """
        url = f"{self.base}/contents/generations/tasks/{task_id}"

        def check():
            r = request_with_retry("GET", url, headers=self._headers(key),
                                   timeout=30, attempts=2, desc="seedance poll")
            raise_for_poll(r, what="Seedance", task_id=task_id)
            j = r.json()
            status = j.get("status")
            if status == "succeeded":
                content = j.get("content") or {}
                v = content.get("video_url")
                if not v:
                    raise ProviderError(f"Seedance 完成但无 video_url: {j}")
                return v, content.get("last_frame_url")
            if status in ("failed", "cancelled", "expired"):   # 官方枚举拼写
                # 错误码结构化上抛（输出侧审核拒收也走这条）：调用方按码分流
                # 处置，靠错误文案子串匹配的口径迟早随厂商措辞漂移
                err = j.get("error") if isinstance(j.get("error"), dict) else {}
                raise ProviderError(f"Seedance 任务{status}: {j.get('error') or j}",
                                    code=str(err.get("code") or "") or None)
            return None

        return poll_task(check, what="Seedance", task_id=task_id,
                         timeout=self.timeout, interval=self.poll_interval,
                         timeout_hint="/下载，或在 models.yaml 的 "
                                      "providers.seedance.timeout 放宽")
