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

"""provider 抽象基类与返回类型。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------- 返回类型 ----------
@dataclass
class ImageResult:
    path: str
    cost: float = 0.0            # 本次生成成本（币种由 config 决定，默认 CNY）
    meta: dict = field(default_factory=dict)


@dataclass
class TTSResult:
    audio_path: str
    # 分段时间戳：[{"text": str, "start": float, "end": float}]；
    # 若 provider 返回词级则更细，MVP 至少到句/镜级。
    segments: list[dict] = field(default_factory=list)
    cost: float = 0.0


@dataclass
class VideoResult:
    path: str
    cost: float = 0.0
    has_audio: bool = False       # 片段是否自带原生音频（native 作主轨保留；dubbed/kenburns 丢弃，音频走旁白+BGM）
    meta: dict = field(default_factory=dict)


@dataclass
class MusicResult:
    path: str
    cost: float = 0.0


# ---------- 抽象基类 ----------
class ImageProvider(ABC):
    name = "base"

    # 0 表示 provider 未声明字符硬上限。非零上限由 PromptCompiler 在请求前校验；
    # adapter 不得静默截断 Envelope.prompt，否则 dry-run、留痕与真实请求会分叉。
    max_prompt_chars: int = 0

    # 参考图语义档：`any`=普通垫图（风格/一致性混用，顺序张数调用方定）；
    # `character`=只收「角色主体参考」（MiniMax subject_reference 一类，平台会把
    # 图上主体当角色特征学走）。为什么放能力位而不是适配器里判：适配器只拿得到
    # 裸路径，判不了哪张是角色设定图——设定集清单的顺序是场景→取景地→角色→道具，
    # 盲取首张恒是场景全景图，被标成 type=character 发出去。调用方按此位筛。
    ref_kind: str = "any"
    # 单次请求收几张参考图（0=不限）。调用方据此裁剪，而不是适配器静默丢
    max_ref_images: int = 0
    # 该 provider 的**纯文生图产物**是否落在视频侧「受信模型产物」豁免内（同账号
    # 方舟链路，判据见 docs/kinema/seedance-face-policy.md §2.2）。写实档的角色
    # 身份图靠这条豁免过人脸闸；调用方按此位告警，不按 name 前缀猜。
    trusted_face_source: bool = False

    @abstractmethod
    def generate(
        self,
        prompt: str,
        out_path: str,
        *,
        ref_images: list[str] | None = None,
        seed: int | None = None,
        width: int = 1080,
        height: int = 1920,
        **kwargs,
    ) -> ImageResult:
        """生成一张分镜帧到 out_path。ref_images 用于角色/风格一致性。"""


class TTSProvider(ABC):
    name = "base"

    # 是否支持「参考音频锚定」（ref_audio 喂参考音锁定音色，生成式 TTS 专用）。
    # 默认 False：调用方按能力标志判断，绝不按 `name` 字符串猜——换别名/新增
    # 生成式适配器时字符串判据必然失配，锚定静默失效且零提示。
    supports_voice_anchor: bool = False

    @abstractmethod
    def synthesize(
        self,
        text: str,
        out_path: str,
        *,
        voice: str | None = None,
        **kwargs,
    ) -> TTSResult:
        """合成一段配音到 out_path，尽量返回时间戳用于字幕对齐。"""


class VideoProvider(ABC):
    name = "base"

    max_prompt_chars: int = 0

    # 提示词里的分段用什么标记时间：`second` 发秒段（第0-3秒：…），`shot` 发不带
    # 时间的顺序编号（第1段：…）。Seedance 2.0 系列不响应精确秒段，其提示词指南
    # 把它列为「支持不稳定，强行限制时长可能导致生成结果异常」；2.5 才响应整数秒。
    # 两档的段头都不带机位义：「镜头 N」是多镜语法（判据 `variation.MULTISHOT_RE`），
    # 而一镜一次调用只取回一段素材，这个记号在任何一档都不发。
    # 缺省 `second` 是保守选择：其余厂商的时间戳行为本仓没有依据，不替它们改口径。
    timeline_unit: str = "second"

    @property
    def effective_price_per_second(self) -> float:
        """当前分辨率档的每秒单价：连接段按档配了 `price_per_second_<档位>`（如 _1080p、_4k）
        即用之，否则回落基准价 `price_per_second`。dry-run 预估、事前闸与 generate 计费
        必须同源取此值——按档计费的型号（1080p 是 720p 的两倍多）用基准价报价会低估账单。"""
        res = str(getattr(self, "resolution", "") or "")
        tier = float((getattr(self, "price_by_resolution", None) or {}).get(res, 0.0) or 0.0)
        return tier if tier > 0 else getattr(self, "price_per_second", 0.0)

    # ---- 可选输入的能力位 ----
    # `generate` 的签名带 `**kwargs`，适配器对不认识的关键字参数一律静默吞掉；服务端
    # 对不认识的 role 也只是丢弃而不报错。两条路径都不抛异常，所以能力位缺省一律**关**，
    # 由调用方（`cli.stage_gen_video`）先查标志再决定发不发——计划期拦住，比适配器
    # 在 generate 里抛错更早，也早于计费。发错的代价按能力位各不相同，逐条记在下面。
    #
    # 参考视频（V2V / 运动迁移）：发不出去 = 一次普通首帧生成，previz 完全没参与。
    supports_reference_video: bool = False
    # 额外参考图（首帧之外的 role=reference_image，如简笔分镜板）：板没发出去，
    # 而提示词里的「所附分镜板」在向模型索要一个不存在的参考。
    supports_reference_images: bool = False
    # 参考音频对口型（dubbed 的 ref_audio）：口型对不上，原生音轨还与 TTS 撞轨。
    supports_ref_audio: bool = False
    # 「音色锚定」参考音条数上限（native 全能参考下随请求附角色音色样本，模型按编号
    # 绑定用该嗓音念台词）。0=不支持，此时提示词里的绑定句索要的是一条不存在的参考音。
    # 适配器按别名配置覆写（seedance 2.0 系列 3 条）。
    max_ref_audios: int = 0
    # 「音色锚定」参考音**总时长**上限（秒）——接口按"全部参考音频合计"限额
    # （seedance 2.0 系列 15s / 2.5 30s），超限是建任务 400。发送侧按它裁剪。
    max_ref_audio_seconds: float = 0.0
    # 末帧（首尾帧衔接与 previz 终态共用同一个槽）：拿到的是一段没有衔接的片段。
    # **缺省 True 是本组唯一的例外**——首尾帧是主流能力，逐个别名声明才开容易漏配。
    supports_last_frame: bool = True
    # 尾帧回传（请求体 `return_last_frame`，结果附本次片段的最后一帧图 URL，
    # 供尾帧接力 tail_relay）：下一镜一帧都承接不到。
    supports_return_last_frame: bool = False

    def billable_seconds(self, dur: float, *, dubbed: bool = False,
                         last_frame: bool = False) -> int:
        """dur 秒在该厂商的计费秒数——dry-run 预估与 generate 必须同源取此口径，
        防止"预估按 A 家档位、实际按 B 家档位"的系统性偏差（烧钱节点失真）。
        `last_frame` 供档位随首尾帧输入变化的厂商取档（Veo 对插值强制 8s），
        缺省实现不消费。缺省：对口型取上整防截话，首帧驱动四舍五入；
        各厂商按官方档位覆写。"""
        import math
        return max(1, math.ceil(dur) if dubbed else round(dur))

    def input_video_seconds(self, ref_seconds: float) -> int:
        """参考视频的**输入侧**计费秒数（token 计费的厂商覆写；默认不额外计费）。"""
        return 0

    @abstractmethod
    def generate(
        self,
        image: str,
        out_path: str,
        *,
        prompt: str = "",
        dur: float = 5.0,
        width: int = 1080,
        height: int = 1920,
        seed: int | None = None,
        last_frame: str | None = None,
        **kwargs,
    ) -> VideoResult:
        """首帧驱动图生视频：以 image 为首帧、prompt 为运动/运镜，生成一段 dur 秒的视频片段。

        dubbed/native 的核心——用图生视频替代 Ken Burns。image 已锁定画面与风格，prompt 只写运动。
        """


def resolution_prices(conn: dict) -> dict:
    """连接段里按档位配置的每秒单价 `price_per_second_<档位>` → {档位: 单价}；0 视为未配。"""
    prefix = "price_per_second_"
    return {k[len(prefix):]: float(v) for k, v in (conn or {}).items()
            if k.startswith(prefix) and float(v or 0) > 0}


class LipsyncProvider(ABC):
    """视频改口型：输入已生成的底片与最终配音，只重绘口型区域输出新视频。

    与 VideoProvider 的边界：这里不生成画面内容，只做「音频→口型」的后处理。
    底片与配音都以**公网 URL** 传入（视觉服务不收本地路径/base64 视频），
    上云由调用方（`cli.stage_lipsync`）复用媒体上云层完成。"""

    name = "base"
    # 每秒单价（按输出视频秒数计），0 = 未配置不入账
    price_per_second: float = 0.0

    @abstractmethod
    def generate(self, video_url: str, audio_url: str, out_path: str,
                 *, dur: float = 0.0, **kwargs) -> VideoResult:
        """按 audio_url 重绘 video_url 的口型，产物落 out_path。"""


class MusicProvider(ABC):
    name = "base"

    @abstractmethod
    def generate(
        self,
        prompt: str,
        out_path: str,
        *,
        duration: float = 60.0,
        **kwargs,
    ) -> MusicResult:
        """生成/取一段背景音乐到 out_path。"""
