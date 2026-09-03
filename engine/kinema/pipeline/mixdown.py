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

"""末级混音：音轨编织 → 让路 EQ + 闪避 → 相加 → 响度归一 + 削波防护。

**混音数值的单一真源**（闪避四参数 / BGM 电平 / 音效母线 / 响度目标 / 音效提前量），
compose 只负责编排、不散落幻数。

为什么单独成模块：compose.build 里音频构建与视频叠层共用同一个输入索引计数器、
且穿插在 effects 循环中间，混音链内联在那里就一行测试都跑不了。本模块把「输入表
+ 音频 filtergraph」独立成可测单元（InputTable 只做计数与拼串，不碰 ffmpeg 进程），
音频链因此可以脱离视频侧独立构造与断言。

**铁律：所有音频处理只落 compose.build 的最终 filtergraph，绝不进 kenburns.fit_clip。**
片段文件名不含音频参数 = 缓存键不会因混音改动失效，写进片段渲染会静默复用旧音轨的
片段——听感是「改了没生效」，比全量重渲更严重。

—— 数值标定（两支真实成片实测，非估算）——
  样片甲 ch01：I=-23.3 LUFS · LRA=2.2 LU · max_volume=-7.9 dB · 旁白 mean -26.2 dB
  样片乙 ch01：I=-22.5 LUFS · LRA=7.1 LU · max_volume=-0.9 dB · 旁白 mean -24.9 dB
三条结论 → 本模块补的三件缺：
  ① 整体比投放口径低 6~7 dB（"声音太小"）      → 末级响度归一 LOUDNESS_I；
  ② 集与集之间 LRA 差 3 倍（"有的集吵有的轻"）→ BGM 入轨归一 BGM_TARGET_I + 末级归一；
  ③ 样片乙峰值已逼近 0 dBFS                  → 削波防护 LIMIT_PEAK。
   （amix normalize=0 是纯相加：满刻度旁白 + BGM 0.3 + 合成 boom volume=2.2 必然过 1.0）
②的独奏例外：**无旁白章节**（白噪音/环境音沉浸、kn-quote 金句配乐）没有旁白母线，
BGM 就是节目本体——若仍按背景床口径压 14 dB，末级要补的增益远超 +9 的钳制上限，
成片会被截在目标响度以下近 10 dB，①②要解决的问题在这类内容上原样保留。
故独奏路径另定：BGM_GAIN_SOLO 归 1.0 + 末级另立 MASTER_SOLO。

—— 为什么不用单遍 loudnorm ——
loudnorm 单遍是**动态**归一：逐帧改增益 + 内建限幅，同一段素材换个上下文就换一条
增益曲线，且会改写 LRA。本工程是确定性 DAG 文化（compose 片段缓存按源指纹判定
"没改动就该复用"），音轨随手变会让缓存语义失真、也无法对账。故走**两步线性**：
  ① 测：loudnorm 的分析模式（print_format=json，只测不改）拿整段 I/TP/LRA；
  ② 改：算一个**静态**增益 volume=<g>dB 推到目标，末尾挂 alimiter 兜住峰值。
线性增益不动动态范围、同输入必得同输出，可复算、可解释、可回归。

**"确定性"的边界说清楚**：确定的是**本模块这条增益链**——同一条输入音轨必得
同一个增益值与同一段输出。整支成片的字节级可复现还要求**所有音源本身确定**，
而 `effects.py` 的雨/雪/雾环境音与 `transitions.whoosh_audio` 的合成兜底音效
都基于 `anoisesrc`（随机种子，两跑不同）。故：用外置音效库 + 无环境音特效的
章节两次合成 md5 一致；带 anoisesrc 音源的章节 md5 必不同，但**增益对账值
（测得 I → 静态增益 dB）两跑一致**——这才是本模块保证的那一条。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..ffmpeg import run_capture

# ---------------------------------------------------------------------------
# 响度目标（流媒体投放通用口径；实测两支成片均低 6~7 dB，故必须末级归一）
# ---------------------------------------------------------------------------
LOUDNESS_I = -16.0          # 整体响度目标（LUFS）——短视频平台常用档
LOUDNESS_TP = -1.5          # 峰值上限（dB）——留 1.5 dB 给有损编码的过冲；末级 alimiter 按
                            # 采样峰限幅，不是 ITU 真峰量测
LOUDNESS_LRA = 11.0         # 响度范围目标（LU）：仅作**体检上限**，线性增益不做区间压缩；
                            # 实测 2.2 / 7.1 均在目标内，超了才提示（说明集内忽大忽小）
LIMIT_PEAK = round(10 ** (LOUDNESS_TP / 20), 3)   # alimiter 线性上限，与 LOUDNESS_TP 同源

# 按模式**分别定义**（一套参数走天下必失败）：
#   kenburns / dubbed —— 主音轨是我们自己的旁白轨（dubbed 的片段原声不进成片，
#                        见 compose.build 的 use_clip_audio 判定），电平可控，偏差有限 → 窄幅钳制；
#   native            —— 模型原生音画（自带对白/音效/BGM），响度不受控，
#                        只能末级救 → 宽幅；BGM 母线只在 native_bgm 显式加铺或
#                        混烧把片段原生音降成背景床时才有。
# 钳制的意义：越权的偏差多半是素材出了事（整段近静音/爆音），宁可欠推也不要把噪声底抬上来。
MASTER_MODES = {
    "kenburns": {"max_gain": 9.0, "min_gain": -9.0},
    "dubbed": {"max_gain": 9.0, "min_gain": -9.0},
    "native": {"max_gain": 14.0, "min_gain": -14.0},
}

# **无旁白章节（独奏）另立一档**（白噪音/环境音沉浸、kn-quote 金句配乐这类
# 整章没有一句旁白的内容）。三模式的窄幅钳制立论是「主音轨是不受控的外来素材，越权偏差
# 多半是素材出事」——独奏时**根本没有外来主音轨**：在场的全是引擎侧确定性源（BGM 已入轨
# 归一到 BGM_TARGET_I、环境音是 lavfi 现合成、转场音效走 SFX_GAIN 母线），不存在爆音/
# 底噪素材风险，末级归一是唯一的电平权威，钳窄只会把整章卡在目标以下。
# max_gain 由最静的一路引擎母线倒推：环境音床按设计就是「垫在人声之下」的量级
# （独奏实测 -31.6 ~ -50.0 LUFS），把最静的一路推到 LOUDNESS_I 需 +34 dB。
# min_gain 不放宽：引擎侧源不会跑热（BGM 归一后独奏也只需 +4 dB）。
MASTER_SOLO = {"max_gain": 34.0, "min_gain": -9.0}

# ---------------------------------------------------------------------------
# 母线电平（**相对**关系，绝对响度交给末级归一）
# 旁白 = 电平基准（0 dB 不衰减，清晰度优先，峰值交给末级限幅器），其余都相对它定。
# ---------------------------------------------------------------------------
BGM_GAIN_DUCKED = 0.3       # 有旁白：BGM 抬高些让停顿更饱满，说话时由 ducking 压下去
BGM_GAIN_SOLO = 1.0         # 无旁白（纯 BGM 片）：BGM 就是节目本体，**不衰减**——它已在入轨
                            # 归一到 BGM_TARGET_I，绝对电平交给末级（本模块的母线只表达"相对
                            # 谁"，独奏时没有相对方）。取 0.2(-14 dB) 是 BGM 未入轨归一时的口径，
                            # 与末级归一叠加后要末级补 +18.7 dB 才够，正好被钳制截断 → 整章比
                            # 有旁白的低 9 dB（"有的集吵有的轻"在无旁白内容上原样保留）。
NATIVE_BED_GAIN = 0.4       # native 配音混烧：片段原生音轨降为背景床的母线电平。
                            # 比纯音乐床（BGM_GAIN_DUCKED=0.3）略高——原生音轨带对白/
                            # 音效/氛围，是场景的躯体不是伴奏；说话段再由 sidechain 闪避
                            # 压下去（模型自配的同句台词被压成弱底、句间氛围恢复）。
                            # 对白上镜章的人声制式即 native 模型自声；全旁白解说章走 dubbed，那里不存在这路原声。
# native 配音混烧：旁白轨入混前对齐对白镜的模型人声。
# 混烧章里两路人声来源不同：旁白是 TTS 文件，电平随 provider 与音色走（seed-tts
# 定制音色实测 -27 ~ -31 LUFS）；对白是模型回吐的片段音轨（实测 -13 ~ -16 LUFS，
# 峰值已近顶）。旁白轨若按 0 dB 基准入混，末级归一测到的整片积分响度由更响的对白
# 窗口决定，只推零点几 dB，旁白窗口留在底下（成片旁白 -31.8 / 对白 -13.2 LUFS，
# 差 18 dB）。末级是整体推，救不了两路人声的配比（与 BGM 入轨归一同一条道理），
# 故旁白轨在入混处推一个静态增益，目标取对白镜窗口里片段音轨的实测响度；两路人声
# 同响度后末级再整体推。只在混烧走这条：kenburns/dubbed 只有一路人声，绝对电平交给末级。
NARRATION_MATCH_RANGE = (-12.0, 18.0)   # 对齐增益钳制：TTS 侧最深 -31 → 对白 -13 要 +18；
                                        # 负向 -12 兜「TTS 比模型人声还响」的少数 provider
SFX_GAIN = 0.55             # 转场音效母线：把音效整体收一档再入 amix。
                            # **收的理由是"叠加"而非某一路特别响**——`amix normalize=0` 是纯相加，
                            # 0 dB 基准旁白 + BGM + 近满刻度的外置音效源同时在场就会顶到削波
                            # （标定样片乙实测 max -0.9 dB）。在母线上收，不动被单测钉死
                            # 的合成链（`transitions.fit_sound_filter` 的时长公式有硬断言）。
                            # 合成兜底 boom 自带 volume=2.2，是潜在最响的一路，但外置库有文件
                            # 时不会触发，标定样片走的都不是它——它不是实测峰值的成因。

# BGM 入轨归一（根因：providers/music/local.py 只有 afade，实测同一情绪目录里五首的
# 响度就横跨 -16.3 ~ -22.8 LUFS（6.5 dB），于是"有的集 BGM 吵有的轻"。在写盘前把每首
# 拉到同一响度，末级归一才不会被"这集 BGM 特别响"带偏配比）
BGM_TARGET_I = -20.0        # 比成片目标低 4 dB：BGM 是背景床，压在旁白之下
BGM_TARGET_TP = -2.0        # BGM 文件自身的峰值上限（wav/mp3 里削平就救不回来了）
BGM_LIMIT_PEAK = round(10 ** (BGM_TARGET_TP / 20), 3)

# ---------------------------------------------------------------------------
# BGM 自动闪避（ducking）四参数标定
# ---------------------------------------------------------------------------
# 旁白 mean 实测 -26.2 / -24.9 dB，据此定阈值；threshold=0.03(≈-30.5 dB)+ratio=12
# 对着 -26 dB 的语音是"常时深压"，停顿恢复也来得突兀（抽气感）。
DUCK = {
    "threshold": 0.05,      # ≈-26 dBFS，正对实测旁白电平：说话即触发、静场不触发（0.03 过低，常时深压）
    "ratio": 8,             # 说话时 BGM 明显退后又不至于消失（12 过狠，句中忽大忽小）
    "attack": 25,           # ms，起字瞬间就让路，又不切掉音乐的瞬态头（<10ms 会咔）
    "release": 400,         # ms，长于字间隙(~0.15s)才不抽气、短于句间停顿(~0.5s)才"停顿恢复"
    "makeup": 1,            # 不做增益补偿——补偿会把刚压下去的又抬回来，闪避形同虚设
}
# 让路 EQ：在 BGM 上挖掉人声辨识度最高的中频（1~4k 的能量团），人声不必靠"压得更狠"
# 就能浮出来。**必须在 sidechaincompress 之前**：先让路再闪避，闪避量才是对"已经让过路"
# 的音乐做的；反过来（先闪避再 EQ）等于二次改写闪避深度，听感深浅不可控。
VOICE_POCKET_EQ = "equalizer=f=2000:width_type=q:width=0.9:g=-3.5"

# 转场音效提前量（独立常量，绝不靠改 TRANSITIONS.edge 来实现）。
# 把 t0 取成 start-edge 是不成立的：8 种转场里 wipe/circle/slide/blur/scan **五种
# edge=0.0**，提前量恒为 0；scan 缺省配 riser（上升蓄势音）会整段落在切点之后，
# 蓄势感完全失效。
# 改 edge 会连带改画面淡化时长（total_span 断言）与片段缓存键（fsuf → 全量重渲），
# 故音画分离：画面用 edge，声音用 edge + SOUND_LEAD。
SOUND_LEAD = 0.25           # 秒；riser 类蓄势音在切点前起势，落点正好压在切点上

_HEAD = "aresample=44100,aformat=channel_layouts=stereo"


def duck_params() -> str:
    """sidechaincompress 参数串（键序固定，便于断言与 diff）。"""
    return ":".join(f"{k}={v}" for k, v in DUCK.items())


def master_spec(motion: str, *, solo: bool = False) -> dict:
    """按渲染模式取末级归一钳制区间（未知模式按 kenburns 保守处理）。

    solo=True（无旁白/无片段音轨的独奏章节）走 MASTER_SOLO：此时在场的全是引擎侧
    确定性源，模式差异（谁的主音轨不受控）无从谈起，钳制区间只按"独奏"这一条定。"""
    if solo:
        return MASTER_SOLO
    return MASTER_MODES.get(motion, MASTER_MODES["kenburns"])


def sound_start(start: float, edge: float, *, lead: float = SOUND_LEAD) -> float:
    """转场音效起点：与前镜淡出同起点再提前 SOUND_LEAD（edge=0 的 xfade 族也有提前量）。"""
    return max(0.0, float(start) - float(edge) - float(lead))


class InputTable:
    """ffmpeg 输入表 + filtergraph 累加器。

    视频叠层与音频轨**共用同一个输入索引**（-i 的出现顺序即 ffmpeg 输入号），这正是
    混音段过去无法单测的根因。索引收进本类后，音频侧可独立构造；video/audio 两条链
    分开存放还有个硬用途——响度分析要单独跑音频子图，视频链带着未连接输出会让
    ffmpeg 直接报错。
    """

    def __init__(self, first: str | Path):
        self.args: list[str] = ["-i", str(first)]
        self.index = 1                  # 0 号已被 first（无声成片）占用
        self.video: list[str] = []
        self.audio: list[str] = []

    def add_input(self, path: str | Path) -> int:
        self.args += ["-i", str(path)]
        self.index += 1
        return self.index - 1

    def add_lavfi(self, src: str) -> int:
        self.args += ["-f", "lavfi", "-i", src]
        self.index += 1
        return self.index - 1

    @property
    def filters(self) -> list[str]:
        """完整 filtergraph（视频链在前，仅为可读性——filter_complex 与顺序无关）。"""
        return [*self.video, *self.audio]


# ---------------------------------------------------------------------------
# 音轨编织
# ---------------------------------------------------------------------------
def clip_audio_track(tbl: InputTable, *, dur: float) -> str:
    """dubbed/native：Seedance 片段自带音轨（输入 0 的音频流）即主音轨。"""
    tbl.audio.append(f"[0:a]{_HEAD},apad,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[na]")
    return "na"


def narration_track(tbl: InputTable, path: str | Path, *, dur: float,
                    gain_db: float = 0.0) -> str:
    """我们这一侧的成品音轨上主轨：kenburns 与 dubbed 降级走 narration.wav，
    native 混烧走同一条通路，`scored` 走音频模型混好的 score.mp3——后者不叠 BGM
    故这条主轨上既无闪避也无让路 EQ。

    `gain_db` 是入混静态增益：缺省 0（主轨是 0 dB 基准，绝对电平交给末级）；
    混烧传 `narration_match_gain_db` 的结果，把 TTS 旁白推到对白镜模型人声的响度。
    增益落在 sidechain 分叉之前——闪避由对齐后的旁白驱动，床在说话段才按设计压下去
    （TTS 电平常在闪避阈值之下，不对齐则闪避不触发）。"""
    i = tbl.add_input(path)
    vol = f"volume={gain_db:.1f}dB," if abs(gain_db) >= 0.1 else ""
    tbl.audio.append(f"[{i}:a]{_HEAD},{vol}apad,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[na]")
    return "na"


def _windows_expr(windows: list[tuple]) -> str:
    """时间窗集合 → 滤镜 timeline enable 表达式（任一窗口内为真）。"""
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in windows)


def clip_bed_track(tbl: InputTable, *, dur: float,
                   bed_windows: list[tuple] | None = None) -> str:
    """native 配音混烧：片段原生音轨占 BGM 母线的槽位，TTS 旁白上主轨（0 dB）。

    床压制（降 `NATIVE_BED_GAIN` + 让路 EQ）**按窗口门控**：声源按镜分治后，
    这条轨在旁白镜窗口里是环境床、在对白镜窗口里是主人声——整轨静态压制会把
    对白镜的模型人声压低 8dB 还挖掉中频，对白比旁白明显发虚。`bed_windows`
    传旁白镜窗口集合（TTS 真出声的地方才需要让路），窗口外原电平直通；
    不传即整轨压制（调用方拿不出时间轴时的保守形态）。

    sidechain 闪避留在 premix：它由旁白轨驱动，对白镜窗口里旁白轨本就是静音，
    天然不触发，无须门控。"""
    on = f":enable='{_windows_expr(bed_windows)}'" if bed_windows else ""
    eq = f",{VOICE_POCKET_EQ}{on}" if bed_windows else ""
    tbl.audio.append(f"[0:a]{_HEAD},volume={NATIVE_BED_GAIN}{on}{eq},apad,"
                     f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[bg]")
    return "bg"


def bgm_track(tbl: InputTable, path: str | Path, *, dur: float, ducked: bool) -> str:
    """BGM 母线：循环铺满全片 → 母线电平 → 裁到片长。"""
    vol = BGM_GAIN_DUCKED if ducked else BGM_GAIN_SOLO
    i = tbl.add_input(path)
    tbl.audio.append(f"[{i}:a]aloop=loop=-1:size=2147483647,volume={vol},aresample=44100,"
                     f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[bg]")
    return "bg"


def ambient_track(tbl: InputTable, src: str, filt: str, *, dur: float) -> str:
    """特效环境音（雨/风/篝火）：稳定铺底，**不参与闪避**——环境是空间感，被人声压
    一下反而假（只有音乐该让路）。"""
    i = tbl.add_lavfi(src)
    tbl.audio.append(f"[{i}:a]{filt or 'anull'},aformat=channel_layouts=stereo,"
                     f"atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[amb{i}]")
    return f"amb{i}"


def transition_sound_track(tbl: InputTable, *, filt: str, dur: float, delay: float,
                           file: str | Path | None = None, lavfi: str | None = None) -> str:
    """转场短音效：外置素材(file) 或 合成兜底(lavfi) → 母线电平 → 按 delay 落位。

    delay 由 sound_start() 算（含 SOUND_LEAD 提前量）；母线 SFX_GAIN 在此统一收，
    不去动 transitions.whoosh_audio/fit_sound_filter 的合成链（被单测逐字钉死）。"""
    i = tbl.add_input(file) if file is not None else tbl.add_lavfi(lavfi or "anullsrc")
    ms = int(delay * 1000)
    tbl.audio.append(f"[{i}:a]{filt},volume={SFX_GAIN},adelay={ms}|{ms},"
                     f"apad,atrim=0:{dur:.3f},asetpts=PTS-STARTPTS[tw{i}]")
    return f"tw{i}"


# ---------------------------------------------------------------------------
# 混音图：让路 EQ → 闪避 → 相加（premix）→ 静态增益 + 限幅（master）
# ---------------------------------------------------------------------------
def premix_graph(tbl: InputTable, *, narration: str | None, bgm: str | None,
                 ambient: list[str] | None = None, bed_eq: bool = True) -> str | None:
    """把各母线合成一路「未做末级处理」的音轨，返回其标签（无音轨则 None）。

    闪避只在**旁白与 BGM 母线同时在场**时发生：native 缺省没有 BGM 母线（模型
    原生音画）、纯 BGM 片没有旁白，两者都不该走 sidechain；混烧时片段原生音作床，
    由旁白轨驱动闪避。

    `bed_eq=False`：床轨自带了按窗口门控的让路 EQ（`clip_bed_track` 的分治形态），
    这里不再整轨叠一遍——叠了就是窗口内双重挖频。"""
    amb = list(ambient or [])
    if narration and bgm:
        tbl.audio.append(f"[{narration}]asplit=2[na_mix][na_sc]")
        eq = VOICE_POCKET_EQ if bed_eq else "anull"
        tbl.audio.append(f"[{bgm}]{eq}[bg_eq]")          # 让路 EQ 在闪避之前
        tbl.audio.append(f"[bg_eq][na_sc]sidechaincompress={duck_params()}[bg_duck]")
        mix = ["na_mix", "bg_duck", *amb]
    else:
        mix = [l for l in (narration, bgm) if l] + amb
    if not mix:
        return None
    if len(mix) == 1:
        return mix[0]
    tbl.audio.append("".join(f"[{l}]" for l in mix)
                     + f"amix=inputs={len(mix)}:duration=longest:normalize=0[amix]")
    return "amix"


def master_filter(gain_db: float, *, limit: float = LIMIT_PEAK) -> str:
    """「静态增益 → 限幅」原语（成片末级与 BGM 入轨共用，只是 limit 档不同）。

    alimiter 的 `level` **必须显式 disabled**：它默认会把输出自动归一回 0 dB，
    正好把刚算好的静态增益抹掉（听感是"归一化没生效"，极难排查）。
    attack/release 用透明档：只削偶发瞬态（boom/爆点），不做听得出来的压缩。
    先推后限而不是"按峰值少推一点"——实测波峰因子可达 22 dB（安静但峰值顶格的曲子），
    按峰值收等于放弃归一，那正是"有的集轻"的成因。"""
    parts = []
    if abs(gain_db) >= 0.1:
        parts.append(f"volume={gain_db:.1f}dB")
    parts.append(f"alimiter=level_in=1:level_out=1:limit={limit}"
                 f":attack=5:release=50:level=disabled")
    return ",".join(parts)


def master_graph(tbl: InputTable, pre: str, gain_db: float) -> str:
    """给 premix 挂上末级链，返回 -map 用的输出标签。"""
    tbl.audio.append(f"[{pre}]{master_filter(gain_db)}[aout]")
    return "[aout]"


# ---------------------------------------------------------------------------
# 响度测量（只测不改）与静态增益计算
# ---------------------------------------------------------------------------
MEASURE_FILTER = (f"loudnorm=I={LOUDNESS_I}:TP={LOUDNESS_TP}:LRA={LOUDNESS_LRA}"
                  f":print_format=json")


def measure_mix_args(tbl: InputTable, pre: str) -> list[str]:
    """混音分析命令（音频子图 + loudnorm 分析模式 → null）。

    刻意只取 tbl.audio：视频链的输出没人 -map，ffmpeg 会以「未连接输出」直接失败。
    输入表原样复用 → 分析与实发的输入编号严格同源。"""
    graph = ";".join([*tbl.audio, f"[{pre}]{MEASURE_FILTER}[lnmeas]"])
    return [*tbl.args, "-filter_complex", graph, "-map", "[lnmeas]", "-f", "null", "-"]


def measure_file_args(inputs: list[str]) -> list[str]:
    """单素材分析命令（BGM 入轨归一用）：inputs 传实际渲染用的输入段，
    保证测的就是最终写盘的那段（循环/裁剪后），而不是整首原曲。"""
    return [*inputs, "-af", MEASURE_FILTER, "-f", "null", "-"]


def measure_windows_args(path: str | Path, windows: list[tuple]) -> list[str]:
    """单文件按时间窗分析（混烧对齐用：旁白轨只测旁白镜窗口、片段音轨只测对白镜窗口）。

    aselect 只放行窗口内采样、asetpts 重排时间戳让 loudnorm 看到一段连续音频；
    窗口外的静音/环境床不进积分，测得的就是那一路人声的响度。"""
    sel = _windows_expr(windows)
    return ["-i", str(path), "-af", f"aselect='{sel}',asetpts=N/SR/TB,{MEASURE_FILTER}",
            "-f", "null", "-"]


def parse_measurement(stderr: str) -> dict | None:
    """从 loudnorm 分析输出里抠出 JSON 块（其余行是 ffmpeg 日志，不能整体 json.loads）。

    定位靠 input_i 键回溯左花括号——JSON 块后面还有 muxing overhead 等日志行，
    直接取末尾会抓空。解析不出一律 None（调用方按"测不到"处理，绝不抛错中断合成）。"""
    if not stderr:
        return None
    key = stderr.rfind('"input_i"')
    if key < 0:
        return None
    start = stderr.rfind("{", 0, key)
    end = stderr.find("}", key)
    if start < 0 or end < 0:
        return None
    try:
        raw = json.loads(stderr[start:end + 1])
    except (ValueError, TypeError):
        return None
    out = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = v
    return out


def measure_loudness(args: list[str]) -> dict | None:
    """跑一遍分析（info 级 stderr 才有 JSON）。**永不抛错**：任何失败都退化为
    「测不到 → 增益 0 → 只留限幅兜底」，绝不让响度体检把成片卡死。"""
    try:
        rc, _out, err = run_capture(args, loglevel="info", desc="loudness")
    except Exception:  # noqa: BLE001  ffmpeg 不在/被杀都不该中断合成
        return None
    if rc != 0:
        return None
    return parse_measurement(err)


def gain_to_target(measured: dict | None, *, target_i: float,
                   floor: float, ceil: float) -> float:
    """静态增益 = 目标响度 − 实测响度，钳制到区间（峰值由下游限幅器兜，见 master_filter）。

    整段静音（input_i = -inf）或测不到 → 0：对着 -70 LUFS 的空音轨推 +50 dB
    只会把底噪放大成噪音墙。"""
    i = _as_float((measured or {}).get("input_i"))
    if i is None or not math.isfinite(i):
        return 0.0
    return round(max(floor, min(ceil, target_i - i)), 1)


def master_gain_db(measured: dict | None, *, motion: str, solo: bool = False) -> float:
    """成片末级静态增益（钳制区间按渲染模式，见 MASTER_MODES / MASTER_SOLO）。

    solo 与 bgm_track(ducked=…) 同一个判据（主音轨是否在场），由 compose 单点传入，
    两处母线决策绝不各判各的。"""
    spec = master_spec(motion, solo=solo)
    return gain_to_target(measured, target_i=LOUDNESS_I,
                          floor=spec["min_gain"], ceil=spec["max_gain"])


def narration_match_gain_db(narration: dict | None, dialogue: dict | None) -> float:
    """混烧旁白轨入混增益：目标 = 对白镜窗口里片段音轨的实测响度（钳 NARRATION_MATCH_RANGE）。

    对白测不到（近静音/ffmpeg 失败）→ 0：没有可对齐的目标就保持 0 dB 基准，
    交给末级；旁白测不到 → 0（gain_to_target 同一条纪律）。"""
    target = _as_float((dialogue or {}).get("input_i"))
    if target is None or not math.isfinite(target):
        return 0.0
    lo, hi = NARRATION_MATCH_RANGE
    return gain_to_target(narration, target_i=target, floor=lo, ceil=hi)


def narration_match_report(narration: dict | None, dialogue: dict | None,
                           gain_db: float) -> str:
    """一行对账（合成时打印）：旁白入混前后响度与对白参照值。"""
    d = _as_float((dialogue or {}).get("input_i"))
    n = _as_float((narration or {}).get("input_i"))
    if d is None or not math.isfinite(d):
        return "  ♪ 对白镜窗口响度未能测出，旁白轨不对齐（保持 0 dB 基准，交末级整体推）"
    if n is None or not math.isfinite(n):
        return f"  ♪ 旁白轨响度未能测出，不对齐（对白镜人声实测 {d:.1f} LUFS）"
    msg = (f"  ♪ 旁白对齐对白：{n:.1f} → {n + gain_db:.1f} LUFS"
           f"（静态增益 {gain_db:+.1f} dB · 对白镜片段人声实测 {d:.1f} LUFS）")
    lo, hi = NARRATION_MATCH_RANGE
    if gain_db in (lo, hi):
        msg += f"\n  ⚠ 对齐增益触到钳制 {gain_db:+.1f} dB（两路人声相差 {d - n:+.1f} dB，检查 TTS 或片段音轨是否异常）"
    return msg


def bgm_gain_db(measured: dict | None) -> float:
    """BGM 入轨静态增益：把各曲目拉到同一响度床（库内横跨 6.5 dB）。"""
    return gain_to_target(measured, target_i=BGM_TARGET_I, floor=-12.0, ceil=12.0)


def peak_reduction_db(measured: dict | None, gain_db: float) -> float:
    """推完增益后需要限幅器削掉的峰值量（dB）——>6 dB 说明素材波峰因子异常，
    该提醒而不是直接削（听感会发闷）。测不到返回 0。"""
    tp = _as_float((measured or {}).get("input_tp"))
    if tp is None or not math.isfinite(tp):
        return 0.0
    return round(max(0.0, tp + gain_db - LOUDNESS_TP), 1)


def report(measured: dict | None, gain_db: float) -> str:
    """一行标定日志（合成时打印，便于对账：改前改后响度、限幅量、动态范围）。"""
    if not measured:
        return f"  ♪ 响度未能测出，末级只保留限幅（≤{LOUDNESS_TP} dBTP）"
    i = _as_float(measured.get("input_i"))
    lra = _as_float(measured.get("input_lra"))
    if i is None or not math.isfinite(i):
        return "  ♪ 整段近静音，末级不推增益"
    msg = (f"  ♪ 末级响度 {i:.1f} → {i + gain_db:.1f} LUFS"
           f"（静态增益 {gain_db:+.1f} dB，限幅 {LOUDNESS_TP} dBTP）")
    cut = peak_reduction_db(measured, gain_db)
    if cut > 6.0:
        msg += f"\n  ⚠ 峰值需削 {cut:.1f} dB（波峰因子偏大，检查转场音效/爆点是否过响）"
    if lra is not None and math.isfinite(lra) and lra > LOUDNESS_LRA:
        msg += f"\n  ⚠ 响度范围 {lra:.1f} LU 超目标 {LOUDNESS_LRA} LU（集内忽大忽小，线性增益不压缩动态）"
    return msg


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
