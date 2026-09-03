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

"""媒体体检：成片自审（verify，出片之后）＋ 供料体检（inspect，素材进门之前）。

两套判据共用「只读 + 绝不冒泡 + 阈值真机标定」三条纪律，故同住一个模块。
**成片自审**只查「机器查得准」的事故面：黑屏/该响却哑/时长/字幕/旁白轨缺席/
动镜档片段缺席六类硬判据，外加旁白轨语音落点（todo 级，见 `voice_placement`）与 native 的
ASR 人声文字核对（`native_voice_check`，装 faster-whisper 才生效）；
**供料体检**只查素材直供图片的四类硬伤（见文件末尾 M16 段）。

# 一、成片自审（verify）

**模块名刻意叫 mediacheck 而不是 inspect**——后者与 Python 标准库同名，一旦有人
直接执行 pipeline 目录下的文件，`sys.path[0]` 会被塞进本目录，unittest/第三方库的
`import inspect` 就会命中本文件。改名零成本，别改回去。

四类判据（阈值全部在**真实成片**上标定，不是拿合成样片估）：

| 判据 | 标定依据 | 定值 |
|---|---|---|
| 黑帧 | 真黑 YAVG=16/YMAX=16；极暗非黑(0x0a0a0a) 25/25；真实夜戏 40.2/**255** | `YAVG≤20 且 YMAX≤24` |
| 静音 | 真实成片 mean −26.2 / −24.9 dB | `mean ≤ −50 dB` 判「该响却哑」 |
| 削波 | 归一后 max −3.1 dB（归一前 −0.9） | `max ≥ −0.5 dB` 告警 |
| 响度 | 末级归一已把成片推到 −16.1 LUFS（目标 −16） | 偏离 ±3 LUFS 记「待修」，**不硬拦** |

**YMAX 是黑帧的关键判别量**：任何一帧真实画面（哪怕最暗的夜戏）都会有高光把
YMAX 顶到 255，只有合成黑场才会 YMAX≤24。单看 YAVG 会把夜戏、暗调画风
（cyberpunk 雨夜 / dark_fantasy 夜戏）整片误判成黑屏。

**必须排除转场时间窗**：`fade`(bg=black,edge=0.2)/`fade_black`(edge=0.25) 的转场镜
本身就是满屏纯黑，相邻正镜另有 0.2~0.3s 淡黑边缘——不排除的话每条带转场的片子
都会误报黑屏。禁区由 `black_windows()` 从 `project.timeline()` + `transitions`
的 spec 算出（转场镜整段 ± 两侧 edge），抽样点用 `sample_points()` 在禁区之外
按可用时长等分，绝不落进禁区。

设计纪律：
· **绝不冒泡**——ffprobe/ffmpeg 的任何异常都转成「容器无效=硬失败」条目，
  否则第一支坏片就中断整条 verify（`probe_duration`/`probe_json` 对 0 字节
  与缺失文件均抛 FFmpegError）。
· **只读**——本模块不改任何产物，结论由 CLI 写进 project.json 顶层 `verify`。
· **结论块必须是合法 JSON**——落盘目的地是 project.json，而 `Infinity`/`NaN`
  不在 JSON 规范里：Python 的 `json.dump` 默认会照吐 `-Infinity`，`JSON.parse`
  与 Studio 的 `res.json()` 当场报错、章节页整页打不开。故凡是从 ffmpeg 数值
  输出来的量，写进 rep 之前一律过有限性判断（非有限→None），见 `loudness_i`。
  形态守卫用例用 `json.dumps(rep, allow_nan=False)` 钉死这条。
· 分析类滤镜的输出走 `ffmpeg.run_capture`（`run()` 吞输出）。
"""
from __future__ import annotations

import math
import re
import shutil
from datetime import datetime
from pathlib import Path

from .. import voicecast
from ..ffmpeg import probe_duration, probe_json, run_capture
from ..project import aspect_tag
from . import asr
from . import mixdown
from . import subtitle as subtitle_mod
from . import transitions as tr_mod

# ---------------------------------------------------------------------------
# 阈值（真实成片标定，改这里等于改验收口径——必须重新在真片上标定）
# ---------------------------------------------------------------------------
BLACK_YAVG = 20.0        # 帧平均亮度上限
BLACK_YMAX = 24.0        # 帧最高亮度上限（关键判别量：真实画面必有高光顶到 255）
SILENT_MEAN_DB = -50.0   # 「该响却哑」判据：整片平均电平低于此 = 事故
CLIP_MAX_DB = -0.5       # 削波告警：峰值贴顶
LOUDNESS_TOL = 3.0       # 响度容差（LUFS），目标取 mixdown.LOUDNESS_I（-16）
DEFAULT_SAMPLES = 8      # 黑帧抽样点数
EDGE_MARGIN = 0.05       # 首尾各留的安全边（避开第一帧/最后一帧的编码边界）


# ---------------------------------------------------------------------------
# 纯函数层：解析 / 阈值判定 / 抽样点推导（无 IO，永远可测）
# ---------------------------------------------------------------------------
def parse_signalstats(stdout: str) -> dict | None:
    """`signalstats,metadata=print:file=-` 的 stdout → {yavg, ymax, ymin, …}（小写去前缀）。

    形如 `lavfi.signalstats.YAVG=42.0888` 的行；一帧一组，多帧时**后来者覆盖**
    （本模块每次只抽一帧）。一条都没解出返回 None（调用方按「测不到」处理）。"""
    if not stdout:
        return None
    out: dict[str, float] = {}
    for m in re.finditer(r"lavfi\.signalstats\.([A-Za-z_]+)=(-?[\d.]+)", stdout):
        try:
            out[m.group(1).lower()] = float(m.group(2))
        except ValueError:      # 非数字值（不应出现）直接跳过，不污染判定
            continue
    return out or None


def is_black_frame(stats: dict | None) -> bool:
    """双条件黑帧判据：`YAVG≤20 且 YMAX≤24`。测不到 → 不判黑（宁可漏报不误报）。"""
    if not stats:
        return False
    yavg, ymax = stats.get("yavg"), stats.get("ymax")
    if yavg is None or ymax is None:
        return False
    return yavg <= BLACK_YAVG and ymax <= BLACK_YMAX


def parse_volumedetect(stderr: str) -> dict | None:
    """volumedetect 的 stderr → {mean_db, max_db}。

    实测同一次运行会打印**两组** `[Parsed_volumedetect_0]`（首组 n_samples: 0），
    故一律取**最后一次**出现的值。任一项缺失返回 None。"""
    if not stderr:
        return None

    def _last(key: str) -> float | None:
        hits = re.findall(rf"{key}:\s*(-?[\d.]+)\s*dB", stderr)
        try:
            return float(hits[-1]) if hits else None
        except ValueError:
            return None

    mean, mx = _last("mean_volume"), _last("max_volume")
    if mean is None and mx is None:
        return None
    return {"mean_db": mean, "max_db": mx}


def black_windows(project) -> list[tuple[float, float]]:
    """黑场禁区 = 每个转场镜的 [start−edge, end+edge]（合并重叠段，按时间升序）。

    转场镜自身满屏底色（fade/fade_black 是纯黑），相邻正镜另有 `edge` 秒的
    边缘淡化（`transitions.edge_fades`）——两侧各扩 edge 即完整覆盖。
    白场（fade_white）与 xfade 族（edge=0）本不会误判为黑，但一并排除更稳：
    转场窗内的画面不是「成片内容」，抽样本就该跳过。"""
    wins: list[tuple[float, float]] = []
    for start, end, s in project.timeline():
        if not tr_mod.is_transition(s):
            continue
        edge = float(tr_mod.spec_of(s).get("edge") or 0.0)
        wins.append((max(0.0, start - edge), end + edge))
    return merge_windows(wins)


def merge_windows(wins: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并重叠/相接的时间窗（背靠背转场时两个禁区会叠在一起）。"""
    out: list[tuple[float, float]] = []
    for a, b in sorted(wins):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def allowed_spans(duration: float, windows: list[tuple[float, float]],
                  *, margin: float = EDGE_MARGIN) -> list[tuple[float, float]]:
    """可抽样区间 = [margin, duration−margin] 减去全部黑场禁区。"""
    lo, hi = margin, duration - margin
    if hi <= lo:
        return []
    spans = [(lo, hi)]
    for wa, wb in merge_windows(windows):
        nxt: list[tuple[float, float]] = []
        for a, b in spans:
            if wb <= a or wa >= b:          # 不相交
                nxt.append((a, b))
                continue
            if wa > a:
                nxt.append((a, wa))
            if wb < b:
                nxt.append((wb, b))
        spans = nxt
    return [(a, b) for a, b in spans if b - a > 1e-6]


def sample_points(duration: float, n: int, windows: list[tuple[float, float]],
                  *, margin: float = EDGE_MARGIN) -> list[float]:
    """在**禁区之外**按可用总时长等分取 n 个抽样时刻（确定性、绝不落进禁区）。

    做法：把所有可用区间首尾相接成一条虚拟时间线，取 (k+0.5)/n 分位，再映射
    回真实时刻——比「均分后遇禁区推移」稳（不会推着推着挤成一堆）。"""
    spans = allowed_spans(duration, windows, margin=margin)
    total = sum(b - a for a, b in spans)
    if n <= 0 or total <= 0:
        return []
    pts: list[float] = []
    for k in range(n):
        u = total * (k + 0.5) / n
        for a, b in spans:
            if u <= b - a:
                pts.append(round(a + u, 3))
                break
            u -= b - a
        else:                                # 浮点兜底：落在最后一段末尾
            pts.append(round(spans[-1][1], 3))
    return pts


def duration_tolerance(n_clips: int, fps: int) -> float:
    """时长容差（**帧量化感知**）：逐片段 `frames = round(dur*fps)`，每镜最多差
    半帧，N 镜累计合法误差可达 N/fps；地板 0.5s 兜住容器/编码器的收尾误差。"""
    return round(max(0.5, n_clips / float(fps or 30)), 3)


def audio_expected(project) -> bool:
    """本片「该不该有声」：

    · dubbed/native 走 Seedance，片段自带音轨 → 必须有声；
      **native 尤其要抓**——它 needs_tts=False、不叠 BGM，片段一旦丢音轨被
      compose 降级后成片**没有任何音频兜底**，正是最该抓的事故；
    · 有旁白的有效镜 > 0 → 必须有声（我们的 TTS 轨）；
    · 非 native 且配了 BGM → 必须有声（纯画面 + 配乐的语录/氛围片）。
    三者皆无 = 纯画面无 BGM 的 kenburns，无声属正常，只记 info。"""
    if project.uses_seedance:
        return True
    if any(voicecast.shot_text(s) for s in project.active_shots):
        return True
    if not project.native_audio:
        from .checkpoint import has_file
        return has_file(project.audio.get("bgm_file"))
    return False


def expected_subtitle_events(project, lang: str) -> int:
    """期望字幕条数 = 有词镜数（`voicecast.shot_text` 认得 lines[]）+ 纯 caption
    补位镜数（音字一致铁律同源）。

    这是**下限口径**：多角色镜逐句成条、演出型模式一镜多条、`corner_note`
    另出角标事件——实际 Dialogue 只会更多，体检比对因此只查「不少于」。
    此处刻意不数这些加项：数了就把下限抬成一个既不准也不稳的点估计。
    「有没有词」不走裸 `pick_texts`：它只读 narration/caption，只写 lines[] 的
    镜在它眼里是无词镜，逐句字幕的章节期望值会塌到只剩 caption 补位那几条，
    「不少于」检查随之形同虚设。"""
    n = 0
    for _start, _end, s in project.timeline():
        if voicecast.shot_text(s):
            n += 1
            continue
        main, sub = subtitle_mod.pick_texts(s, lang)
        if main or sub:
            n += 1
    return n


def count_dialogues(ass_text: str) -> int:
    """数 ASS 的 `Dialogue:` 事件行——「文件存在且非空」恒真（ASS 无条件写盘），
    等于没查；条数才是真判据。"""
    return sum(1 for ln in (ass_text or "").splitlines()
               if ln.lstrip().startswith("Dialogue:"))


def loudness_i(measured: dict | None) -> float | None:
    """整片实测响度（LUFS）。**非有限值一律 None**——整段静音时 loudnorm 报
    `input_i = "-inf"`，`parse_measurement` 会如实转成 `float('-inf')`，原样落进
    结论块就会让 `json.dump` 吐出 `-Infinity`（**不是合法 JSON**）：project.json
    从此 `JSON.parse` 失败，Studio 章节页直接白屏。纪律与 `mixdown.gain_to_target`
    的 `math.isfinite` 守卫同源——**结论块里只准出现有限数或 None**。"""
    i = (measured or {}).get("input_i")
    if not isinstance(i, (int, float)) or isinstance(i, bool):
        return None
    i = float(i)
    return i if math.isfinite(i) else None


def loudness_off_target(measured: dict | None) -> float | None:
    """整片响度偏离目标（LUFS，正=偏响）。测不到 / 整段静音返回 None。"""
    i = loudness_i(measured)
    return None if i is None else round(i - mixdown.LOUDNESS_I, 1)


# ---------------------------------------------------------------------------
# 探测命令（形态守卫用例钉死这几条 flag）
# ---------------------------------------------------------------------------
def frame_stats_args(path: str, t: float) -> list[str]:
    """单帧亮度探测：`-ss` 前置快进 + 只解一帧 + signalstats 打 stdout。
    `-an` 必带（不解音频，快一大截）。"""
    return ["-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
            "-vf", "signalstats,metadata=print:file=-", "-an", "-f", "null", "-"]


def volume_args(path: str) -> list[str]:
    """整片电平探测。**`-vn` 必带**：实测「无音轨且不加 -vn」时 ffmpeg 退出 0 且
    静默无输出，会被误判成「测到了但值为空」；加 -vn 才退出 234 明确失败。"""
    return ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]


# ---------------------------------------------------------------------------
# 探测层（永不抛异常，全部退化为「测不到」）
# ---------------------------------------------------------------------------
def probe_frame(path: str, t: float) -> dict | None:
    try:
        rc, out, _err = run_capture(frame_stats_args(path, t), loglevel="error",
                                    desc="signalstats")
    except Exception:            # noqa: BLE001  ffmpeg 不在/被杀不该中断体检
        return None
    return parse_signalstats(out) if rc == 0 else None


def probe_volume(path: str) -> dict | None:
    try:
        rc, _out, err = run_capture(volume_args(path), loglevel="info",
                                    desc="volumedetect")
    except Exception:            # noqa: BLE001
        return None
    return parse_volumedetect(err) if rc == 0 else None


def probe_loudness(path: str) -> dict | None:
    """整片响度（复用混音链的 loudnorm 分析原语，口径与合成末级完全同源）。"""
    return mixdown.measure_loudness(mixdown.measure_file_args(["-i", str(path), "-vn"]))


def has_audio_stream(path: str) -> bool | None:
    """是否含音频流。None = 容器读不出（调用方按硬失败处理，别当成「无音轨」）。"""
    try:
        streams = (probe_json(path) or {}).get("streams") or []
    except Exception:            # noqa: BLE001  FFmpegError / JSON 解析失败
        return None
    return any(st.get("codec_type") == "audio" for st in streams)


# ---------------------------------------------------------------------------
# 体检主流程（单比例）
# ---------------------------------------------------------------------------
def _fail(code: str, msg: str) -> dict:
    return {"code": code, "msg": msg}


def verify_aspect(project, store, *, aspect: str, samples: int = DEFAULT_SAMPLES,
                  sub_lang: str = "zh") -> dict:
    """对某比例的成片跑一遍体检，返回该比例的结论块。

    结论块：`{ok, hard_fail[], todo[], info[], file, duration{}, black_samples[],
    audio{}, subtitle{}}`。`ok = not hard_fail`；todo 是「待修但不拦」。
    **成片路径先过 `storage.media.ensure_local`**——值可能是 OSS URL，直接喂
    ffprobe 会在无网/私有桶下把好片判成硬失败。"""
    from ..storage.media import ensure_local

    hard: list[dict] = []
    todo: list[dict] = []
    info: list[str] = []
    rep = {"aspect": aspect, "file": None, "ok": False,
           "hard_fail": hard, "todo": todo, "info": info,
           "duration": {}, "black_samples": [], "audio": {}, "subtitle": {}}

    raw = (project.data.get("output") or {}).get(aspect)
    if not (isinstance(raw, str) and raw):        # output.approved 是 boolean，滤掉
        hard.append(_fail("missing", f"[{aspect}] 没有成片（output 为空）——先 assemble 出片"))
        return rep
    # 该产旁白轨的章（kenburns/dubbed/混烧）没有登记旁白轨：成片里没有固定音色人声，
    # 而 BGM 或片段原生音会让整片均值远高于静音阈值，靠「该响却哑」抓不住
    from .checkpoint import has_file
    if (project.needs_narration_track
            and any(voicecast.narration_shot(s, project.motion) for s in project.active_shots)
            and not has_file(project.audio.get("narration_file"))):
        hard.append(_fail("narration_missing",
                          "本章制式要求烧录旁白轨，但 audio.narration_file 未登记或不在盘"
                          "——成片没有固定音色人声；先 tts 再重新 assemble"))
    # 动镜档成片里每个正镜都该是买回的片段；缺片段的镜在成片里只能是静图回落
    if project.uses_seedance:
        no_clip = [str(s.get("id")) for s in project.active_shots
                   if not tr_mod.is_transition(s) and not has_file(project.clip_for(s, aspect))]
        if no_clip:
            hard.append(_fail("clip_missing",
                              f"[{aspect}] 动镜档但镜 {'、'.join(no_clip)} 没有片段"
                              "——先 gen-video 补齐再重新 assemble"))
    try:
        path = ensure_local(raw)
    except Exception as e:                        # noqa: BLE001  OSS 拉取失败不判坏片
        info.append(f"成片在云端且拉取失败（{e}）——本比例跳过体检")
        rep["file"] = raw
        rep["ok"] = True
        return rep
    rep["file"] = str(path)
    if not Path(str(path)).is_file():
        hard.append(_fail("missing", f"[{aspect}] 成片文件不存在: {path}"))
        return rep

    # ---- 1) 容器 + 时长（三方比对：期望 Σdur / 无声源片 / 成片）----
    try:
        actual = probe_duration(path)
    except Exception as e:                         # noqa: BLE001  FFmpegError/0 字节/损坏容器
        hard.append(_fail("container", f"[{aspect}] 容器无效，ffprobe 读不出时长：{e}"))
        return rep
    expected = project.total_duration()
    n_clips = len(project.active_shots)
    tol = duration_tolerance(n_clips, store.fps)
    silent = project.workdir / "build" / f"silent_{aspect_tag(aspect)}.mp4"
    src_dur = None
    if silent.is_file():
        try:
            src_dur = round(probe_duration(silent), 3)
        except Exception:                          # noqa: BLE001  中间产物坏了不算成片的账
            src_dur = None
    rep["duration"] = {"expected": expected, "actual": round(actual, 3),
                       "source": src_dur, "tolerance": tol,
                       "delta": round(actual - expected, 3)}
    if abs(actual - expected) > tol:
        hard.append(_fail("duration",
                          f"[{aspect}] 成片时长 {actual:.2f}s 与分镜时间轴 {expected:.2f}s "
                          f"偏差 {abs(actual - expected):.2f}s（容差 {tol:.2f}s = "
                          f"max(0.5, {n_clips}镜/{store.fps}fps)）——多为改 dur 后未重合成"))

    # ---- 2) 黑屏抽样（跳过转场黑场禁区）----
    wins = black_windows(project)
    pts = sample_points(actual, samples, wins)
    if not pts:
        info.append("可抽样区间为空（全片都在转场禁区内？）——黑屏体检跳过")
    blacks = []
    for t in pts:
        st = probe_frame(str(path), t)
        black = is_black_frame(st)
        rep["black_samples"].append({
            "t": t,
            "yavg": (st or {}).get("yavg"), "ymax": (st or {}).get("ymax"),
            "black": black})
        if st is None:
            info.append(f"t={t:.2f}s 抽帧失败（测不到亮度）")
        elif black:
            blacks.append(t)
    if blacks:
        hard.append(_fail("black",
                          f"[{aspect}] {len(blacks)} 个抽样点是黑帧（"
                          + "、".join(f"{t:.2f}s" for t in blacks[:5])
                          + f"）——判据 YAVG≤{BLACK_YAVG:g} 且 YMAX≤{BLACK_YMAX:g}；"
                          "已排除转场黑场窗，说明是正片黑屏"))

    # ---- 3) 音频（该响却哑 / 削波 / 响度）----
    want_audio = audio_expected(project)
    has_a = has_audio_stream(str(path))
    aud: dict = {"expected": want_audio, "has_stream": has_a}
    if has_a is None:
        hard.append(_fail("container", f"[{aspect}] 容器无效，ffprobe 读不出流信息"))
    elif not has_a:
        if want_audio:
            hard.append(_fail("no_audio",
                              f"[{aspect}] 该响却哑：成片没有音频流"
                              + ("（native 片段丢音轨被降级后无任何音频兜底）"
                                 if project.native_audio else "")))
        else:
            info.append("成片无音频流（纯画面无 BGM 的 kenburns，属正常）")
    else:
        vol = probe_volume(str(path)) or {}
        aud.update(mean_db=vol.get("mean_db"), max_db=vol.get("max_db"))
        mean, mx = vol.get("mean_db"), vol.get("max_db")
        if mean is None:
            info.append("电平测不到（volumedetect 无输出）")
        elif want_audio and mean <= SILENT_MEAN_DB:
            hard.append(_fail("mute",
                              f"[{aspect}] 该响却哑：整片平均电平 {mean:.1f} dB "
                              f"≤ {SILENT_MEAN_DB:g} dB"))
        if mx is not None and mx >= CLIP_MAX_DB:
            todo.append(_fail("clipping",
                              f"[{aspect}] 峰值 {mx:.1f} dB 贴顶（≥{CLIP_MAX_DB:g} dB）"
                              "，可能削波——重合成会走末级限幅归一"))
        loud = probe_loudness(str(path))
        lufs = loudness_i(loud)
        off = loudness_off_target(loud)
        aud["loudness_i"] = lufs
        aud["loudness_off"] = off
        if lufs is None and loud is not None:
            info.append("整片响度测不到（整段静音，loudnorm 报 -inf）")
        if off is not None and abs(off) > LOUDNESS_TOL:
            todo.append(_fail("loudness",
                              f"[{aspect}] 整片响度 {aud['loudness_i']:.1f} LUFS 偏离目标 "
                              f"{mixdown.LOUDNESS_I:g} LUFS 达 {off:+.1f}（容差 "
                              f"±{LOUDNESS_TOL:g}）——重合成即按末级归一修正"))
    rep["audio"] = aud

    # ---- 4) 字幕在位（数 Dialogue 行，不是「文件存在且非空」）----
    ass = project.workdir / "subs" / f"sub_{aspect_tag(aspect)}.ass"
    want_lines = expected_subtitle_events(project, sub_lang)
    sub: dict = {"file": str(ass), "expected": want_lines, "dialogues": None,
                 "lang": sub_lang}
    if not ass.is_file():
        if want_lines:
            hard.append(_fail("subtitle",
                              f"[{aspect}] 字幕文件缺失: {ass.name}（期望 {want_lines} 条）"))
        else:
            info.append("本片无字幕文本，字幕体检跳过")
    else:
        got = count_dialogues(ass.read_text(encoding="utf-8", errors="replace"))
        sub["dialogues"] = got
        # 演出型模式（气泡/对话框/榜单）一镜可出多条事件，故只查「不少于」
        if got < want_lines:
            todo.append(_fail("subtitle",
                              f"[{aspect}] 字幕只有 {got} 条 Dialogue，少于期望 "
                              f"{want_lines} 条——重跑 assemble 重烧字幕"))
    rep["subtitle"] = sub

    rep["ok"] = not hard
    return rep


def voice_placement(project) -> dict | None:
    """旁白轨的逐镜语音落点体检（narration 作主音轨的两档：dubbed/kenburns）。

    对象是 assemble 重拼后的 narration.wav：它是烧录真源且不含 BGM，成片主音轨
    = 它与 BGM 的混合，落点在轨内即在成片内（concat 构造保证）。不对带 BGM 的
    成片本体检测——silencedetect 是振幅判据，无法区分人声与音乐。

    逐镜只判「有词镜窗口内有语音段、无词镜窗口内没有」：开口对齐会把语音起点
    安排在窗口中段（模型把开口排在动作之后是常态），故不判头部位置。
    结论恒 todo 级不硬拦：窗口边界的续音与响亮气声同样触发振幅判据，
    误报只能靠人复听裁决。"""
    from ..storage.media import ensure_local
    from .speech import speech_windows
    if project.motion not in ("dubbed", "kenburns") or project.scored_audio:
        return None
    raw = project.audio.get("narration_file")
    if not raw:
        return None
    try:
        narr = ensure_local(raw)
    except Exception:  # noqa: BLE001  云端拉取失败不判坏，跳过本节
        return None
    if not Path(str(narr)).is_file():
        return None
    try:
        total = probe_duration(narr)
        segs = speech_windows(str(narr), total, clean=True)
    except Exception:  # noqa: BLE001  探测失败是环境问题，不进硬失败
        return None
    rows: list[dict] = []
    todo: list[dict] = []
    for start, end, s in project.timeline():
        worded = bool(voicecast.shot_text(s))
        hits = [(round(max(a, start), 2), round(min(b, end), 2))
                for a, b in segs if a < end - 0.05 and b > start + 0.05]
        covered = sum(b - a for a, b in hits)
        row = {"id": s.get("id"), "window": [round(start, 2), round(end, 2)],
               "speech": hits}
        if worded and not hits:
            row["note"] = "有词镜窗口内检不到语音"
            todo.append(_fail("voice_missing",
                              f"镜 {s.get('id')} 有台词，但旁白轨该窗口"
                              f"（{start:.1f}~{end:.1f}s）内检不到语音——"
                              "先核对 tts 产物与该镜 review 状态"))
        elif not worded and covered > 0.4:
            row["note"] = "无词镜窗口内检出语音段"
            todo.append(_fail("voice_stray",
                              f"镜 {s.get('id')} 无台词，旁白轨窗口"
                              f"（{start:.1f}~{end:.1f}s）却检出 {covered:.1f}s "
                              "语音——常见成因是相邻镜续音或陈旧音轨，请复听裁决"))
        rows.append(row)
    return {"ok": True, "file": str(narr), "rows": rows, "todo": todo}


# 稿面文字被念出来的最低比例：低于它判为「没按这一稿念」。0.6 容得下 ASR 的
# 个别误字与语气词增删（small 档中文词错率量级），压不住整句换词与半句漏念
VOICE_TEXT_RECALL_MIN = 0.6
# 单句召回的下限：整镜召回是全稿字数的比例，两字句整句漏念只掉 9%，压不到
# 整镜阈值之下；逐句摊回后漏念句召回为 0，误字句仍在 0.5 以上
VOICE_LINE_RECALL_MIN = 0.5


def native_voice_check(project, *, aspects: list[str] | None = None) -> dict | None:
    """native 声源的人声文字核对（`voice` 节的 native 形态，`kind="asr"`）。

    native 的人声由视频模型生成，提示词逐字给了台词但执行没有确定性保证——
    lint `native_voice_unverified` 点名的就是这层。本节用本地 ASR 逐镜转写
    片段自带音轨、与章节台词比对，把「待核对」收成实测结论。判据对象是
    gen_clips 底片而非成片：成片混了 BGM/环境床，转写它只会稀释判据；
    底片音轨即烧进成片的那条（native 保留片段自声）。

    混烧章只查对白镜（旁白镜的人声是烧录 TTS，与字幕同源无须核对）。
    整镜召回达标后再按 `lines[]` 逐句摊回（`asr.line_recalls`），整句漏念记
    `voice_line_dropped`：字幕按稿面烧录，漏念句会成为一条无声字幕。
    逐比例出片时每个比例都是模型的一次独立采样，各比例的片段逐条核对
    （缺省共用一条片段时按路径去重，只转写一次）。
    faster-whisper 未装时照实说测不了（`available: False`），不装不拦。"""
    if project.motion != "native" or project.scored_audio:
        return None
    burn = project.native_voiceover
    targets = [s for s in project.active_shots
               if voicecast.shot_text(s)
               and not (burn and voicecast.burn_muted(s))]
    if not targets:
        return None
    if not asr.available():
        return {"ok": True, "kind": "asr", "available": False,
                "note": "faster-whisper 未装，人声文字未核对"
                        "（装：pip install -e \"engine[asr]\" 后重跑 verify）"}
    rows: list[dict] = []
    todo: list[dict] = []
    for s in targets:
        seen: set[str] = set()
        for asp in (aspects or project.aspects):
            # 片段取 clip_for：已上云的路径在这里拉回本地（本地在盘时零下载）。
            # 裸读 clip 会让跑过 oss sync 的章节整章判成「不在本地」
            clip = project.clip_for(s, asp)
            if not clip or str(clip) in seen:
                continue
            seen.add(str(clip))
            row = {"id": s.get("id")}
            if len(project.aspects) > 1:
                row["aspect"] = asp
            if not Path(str(clip)).is_file():
                row["note"] = "片段不在本地，跳过"
                rows.append(row)
                continue
            res = asr.transcribe(clip)
            if res is None:
                row["note"] = "转写失败，跳过"
                rows.append(row)
                continue
            expected = voicecast.shot_text(s)
            heard = res.get("text") or ""
            score = asr.text_recall(expected, heard)
            row["score"] = round(score, 2)
            row["heard"] = heard[:80]
            if score < VOICE_TEXT_RECALL_MIN:
                row["note"] = "实发人声与台词不符"
                todo.append(_fail("voice_text_drift",
                                  f"镜 {s.get('id')} 台词「{expected[:30]}…」，"
                                  f"ASR 听到「{heard[:30]}…」（念出 {score:.0%}）"
                                  "——转写与稿面不符。ASR 不是耳朵：同音异调、"
                                  "轻声与数词字形都会误判，先试听该片段，"
                                  "确认真念错了再由人裁决重生或按实发改写台词"))
            else:
                texts = [ln["text"] for ln in voicecast.shot_lines(s)]
                per = asr.line_recalls(texts, heard)
                dropped = [t for t, r in zip(texts, per) if r < VOICE_LINE_RECALL_MIN]
                if dropped:
                    row["note"] = "有台词未念出"
                    todo.append(_fail("voice_line_dropped",
                                      f"镜 {s.get('id')} 有 {len(dropped)} 句未念出："
                                      f"「{'｜'.join(dropped)[:40]}」——整镜召回达标，"
                                      "但这几句在转写里没有落点；先试听，确认漏念后"
                                      "从 lines 删句重合成字幕，或置 retake 重生"))
            rows.append(row)
    return {"ok": True, "kind": "asr", "available": True,
            "rows": rows, "todo": todo}


def verify(project, store, *, aspects: list[str] | None = None,
           samples: int = DEFAULT_SAMPLES, sub_lang: str = "zh") -> dict:
    """全比例体检，返回可直接写进 project.json 顶层 `verify` 的报告。

    形如 `{at, voice, "16:9": {...}, "9:16": {...}}`——比例键与 `output` 同构；
    `voice` 与比例无关（音轨全比例共用），按声源两态：dubbed/kenburns 是
    旁白轨语音落点，native 是 ASR 人声文字核对（`kind="asr"`）。"""
    out: dict = {"at": datetime.now().isoformat(timespec="seconds")}
    vp = voice_placement(project) or native_voice_check(project, aspects=aspects)
    if vp is not None:
        out["voice"] = vp
    for asp in (aspects or project.aspects):
        out[asp] = verify_aspect(project, store, aspect=asp, samples=samples,
                                 sub_lang=sub_lang)
    return out


def report_lines(rep: dict) -> list[str]:
    """报告 → 逐行中文摘要（CLI 打印用；`--json` 走原始 dict）。"""
    lines: list[str] = []
    vp = rep.get("voice")
    if isinstance(vp, dict) and vp.get("kind") == "asr":
        if not vp.get("available", True):
            lines.append(f"[人声核对] {vp.get('note', 'ASR 不可用，未核对')}")
        else:
            rows = vp.get("rows") or []
            # 分母只数真正转写过的行：跳过的（片段不在本地/转写失败）没有 score，
            # 计进分母会让「0/6 相符」被读成体检结论
            done = [r for r in rows if isinstance(r.get("score"), (int, float))]
            n_ok = sum(1 for r in done
                       if r["score"] >= VOICE_TEXT_RECALL_MIN and "note" not in r)
            n_skip = len(rows) - len(done)
            lines.append(f"[人声核对] ASR 文字比对 · {n_ok}/{len(done)} 片段与台词相符"
                         + (f" · {n_skip} 片段未核对" if n_skip else "")
                         + ("" if not vp.get("todo") else f" · {len(vp['todo'])} 项待修"))
            for f in vp.get("todo") or []:
                lines.append(f"    ⚠ 待修 · {f['msg']}")
    elif isinstance(vp, dict):
        n_hit = sum(1 for r in vp.get("rows") or [] if r.get("speech"))
        lines.append(f"[旁白轨] 语音落点 · {n_hit}/{len(vp.get('rows') or [])} 镜检出语音段"
                     + ("" if not vp.get("todo") else f" · {len(vp['todo'])} 项待修"))
        for f in vp.get("todo") or []:
            lines.append(f"    ⚠ 待修 · {f['msg']}")
    for asp, blk in rep.items():
        if asp in ("at", "voice") or not isinstance(blk, dict):
            continue
        mark = "✓ 通过" if blk.get("ok") else "⊘ 硬失败"
        d = blk.get("duration") or {}
        lines.append(f"[{asp}] {mark}"
                     + (f" · 时长 {d['actual']:.2f}s/期望 {d['expected']:.2f}s"
                        if d.get("actual") is not None else ""))
        a = blk.get("audio") or {}
        if a.get("has_stream"):
            lines.append(f"    音频 mean {a.get('mean_db')} dB · max {a.get('max_db')} dB"
                         + (f" · {a['loudness_i']:.1f} LUFS"
                            if isinstance(a.get("loudness_i"), (int, float)) else ""))
        s = blk.get("subtitle") or {}
        if s.get("dialogues") is not None:
            lines.append(f"    字幕 {s['dialogues']}/{s['expected']} 条")
        bs = blk.get("black_samples") or []
        if bs:
            nb = sum(1 for x in bs if x.get("black"))
            lines.append(f"    黑屏抽样 {len(bs)} 点 · 命中 {nb}")
        for f in blk.get("hard_fail") or []:
            lines.append(f"    ⊘ {f['msg']}")
        for f in blk.get("todo") or []:
            lines.append(f"    ⚠ 待修 · {f['msg']}")
        for m in blk.get("info") or []:
            lines.append(f"    · {m}")
    return lines


# ---------------------------------------------------------------------------
# 二、供料体检：现成素材进门之前的一道体检
#
# 只对素材直供的图片（supply.IMAGE_EXTS = png/jpg/jpeg/webp）体检——音视频判据
# （单声道/时长过短）在此不适用。四项判据：
#
# | 判据 | 后果 | 处置 |
# |---|---|---|
# | ffprobe 解不出 / 无图像流 | 后续 kenburns/compose 必炸 | **硬拦** |
# | 分辨率低于画布 | cover 放大 → 全片糊 | 告警 |
# | 宽高比偏差 | cover 裁掉主体（人头出画） | 告警 |
# | 带 alpha 通道 | 合成后透明区变纯黑 | 告警 |
#
# 失败策略「告警不拦死」：只有第一项硬拦，其余打印 ⚠ 并写进
# `shots[].gen.image.inspect` 留痕。硬拦可用 `--skip-check` 跳过（网页同款开关），
# 供不可再生的实拍素材登记。
# ---------------------------------------------------------------------------
SUPPLY_MIN_COVERAGE = 0.9    # 图片覆盖画布的最弱边比例下限（未按真机素材标定，故只告警）
SUPPLY_ASPECT_TOL = 0.12     # 宽高比偏差容差：cover 取景裁掉 >12% 即告警

# 带 alpha 的 pix_fmt 家族（前缀匹配即可覆盖 rgba64le/gbrap10be 等全部变体）。
# **刻意不含 pal8**：索引色 PNG 的透明靠 tRNS 块，pix_fmt 看不出来——宁可漏报
# 不误报（与黑帧判据同一条纪律），否则每张索引色截图都要报一次假警。
_ALPHA_PIX_FMT = re.compile(r"^(ya|yuva|rgba|bgra|argb|abgr|gbra|ayuv)")


def image_info(probe: dict | None) -> dict | None:
    """ffprobe JSON → 首个**图像流** `{width, height, pix_fmt, codec}`。

    静态图在 ffprobe 里就是 `codec_type=video`（codec_name = png/mjpeg/webp）。
    没有可用图像流（改名的假图、损坏文件、纯音频）返回 None = 硬拦。

    **`width/height > 0` 这一层必须有**：把一段文本改名成 .png 后，ffprobe
    **退出码 0**、照样吐出一条 `codec_type=video` 的流，只是 `width=height=0`
    （format_name=image2、stderr 才有 "Invalid PNG signature"）。只判「有没有
    video 流」会把这种假图放行，后面 ffmpeg 渲染必炸。"""
    for st in (probe or {}).get("streams") or []:
        if st.get("codec_type") != "video":
            continue
        w, h = st.get("width"), st.get("height")
        if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
            continue
        return {"width": w, "height": h, "pix_fmt": st.get("pix_fmt"),
                "codec": st.get("codec_name")}
    return None


def has_alpha(pix_fmt: str | None) -> bool:
    """pix_fmt 是否带 alpha 通道（透明区在 yuv420p 合成后会变纯黑）。"""
    return bool(pix_fmt) and bool(_ALPHA_PIX_FMT.match(str(pix_fmt)))


def cover_coverage(size: tuple[int, int], canvas: tuple[int, int]) -> float:
    """图片对画布的覆盖率（取**最弱边**）：<1 = 该边要被放大，画面必糊。

    Ken Burns 走 `scale=…:force_original_aspect_ratio=increase` 再 crop
    （`kenburns.render_shot`），等价于 cover 取景——最弱边决定放大倍数。"""
    iw, ih = size
    cw, ch = canvas
    if min(iw, ih, cw, ch) <= 0:
        return 0.0
    return round(min(iw / cw, ih / ch), 3)


def aspect_overflow(size: tuple[int, int], canvas: tuple[int, int]) -> float:
    """cover 取景下被裁掉的画面占比（0=完全同比；0.25=四分之一被切出画）。"""
    iw, ih = size
    cw, ch = canvas
    if min(iw, ih, cw, ch) <= 0:
        return 0.0
    r = (iw / ih) / (cw / ch)
    r = max(r, 1 / r)
    return round(1 - 1 / r, 3)


def image_findings(info: dict | None, canvas: tuple[int, int] | None,
                   *, why: str = "") -> tuple[list[dict], list[dict]]:
    """纯判定层：`(hard_fail[], warn[])`。**无 IO**，阈值守卫直接吃这一层。"""
    hard: list[dict] = []
    warn: list[dict] = []
    if info is None:
        hard.append(_fail("unreadable",
                          "ffprobe 解不出图像流（改名的假图 / 文件损坏 / 不是图片）"
                          + (f"：{why}" if why else "")
                          + "——这张素材后续 ffmpeg 渲染必炸，已拦下"))
        return hard, warn
    if has_alpha(info.get("pix_fmt")):
        warn.append(_fail("alpha",
                          f"素材带 alpha 通道（pix_fmt={info['pix_fmt']}）——"
                          "合成走 yuv420p，透明区会变成纯黑；建议先压平背景"))
    if not canvas:
        return hard, warn
    size = (info["width"], info["height"])
    cov = cover_coverage(size, canvas)
    if cov < SUPPLY_MIN_COVERAGE:
        warn.append(_fail("low_res",
                          f"素材 {size[0]}×{size[1]} 低于画布 {canvas[0]}×{canvas[1]}"
                          f"（最弱边只覆盖 {cov * 100:.0f}%，下限 "
                          f"{SUPPLY_MIN_COVERAGE * 100:.0f}%）——Ken Burns 还要再推近，"
                          "成片会糊"))
    ov = aspect_overflow(size, canvas)
    if ov > SUPPLY_ASPECT_TOL:
        warn.append(_fail("aspect",
                          f"素材宽高比 {size[0]}:{size[1]} 与画布 "
                          f"{canvas[0]}:{canvas[1]} 不符——cover 取景会裁掉约 "
                          f"{ov * 100:.0f}%（容差 {SUPPLY_ASPECT_TOL * 100:.0f}%），"
                          "主体可能出画"))
    return hard, warn


def ffprobe_available() -> bool:
    """ffprobe 在不在 PATH（体检的前置条件，也是测试的注入缝）。"""
    return shutil.which("ffprobe") is not None


def inspect_image(path, *, canvas: tuple[int, int] | None = None) -> dict:
    """给一张待直供的图片做体检，返回可直接写进 `gen.image.inspect` 的报告。

    `{at, file, ok, width, height, pix_fmt, codec, alpha, canvas, coverage,
    crop, hard_fail[], warn[], info[]}`；`ok = not hard_fail`。
    **只读、绝不冒泡**：ffprobe 的任何异常都收敛成 `unreadable` 硬失败条目。
    ffprobe 不在 PATH 时不判坏图，退化为「体检跳过」的 info（体检是护栏，
    不该反过来把没装全工具的机器上的直供功能整个锁死）。"""
    rep: dict = {"at": datetime.now().isoformat(timespec="seconds"),
                 "file": str(path), "ok": True,
                 "canvas": list(canvas) if canvas else None,
                 "hard_fail": [], "warn": [], "info": []}
    if not ffprobe_available():
        rep["info"].append("ffprobe 不在 PATH——本次跳过素材体检（doctor 可自检）")
        return rep
    why = ""
    try:
        info = image_info(probe_json(str(path)))
    except Exception as e:            # noqa: BLE001  FFmpegError / JSON 解析失败
        info = None
        why = (str(e).splitlines() or [""])[0]
    if info:
        rep.update(width=info["width"], height=info["height"],
                   pix_fmt=info.get("pix_fmt"), codec=info.get("codec"),
                   alpha=has_alpha(info.get("pix_fmt")))
        if canvas:
            rep["coverage"] = cover_coverage((info["width"], info["height"]), canvas)
            rep["crop"] = aspect_overflow((info["width"], info["height"]), canvas)
    else:
        rep["error"] = why
    if not canvas:
        rep["info"].append("无画布基准（未传 ConfigStore）——只查了可解码性与 alpha")
    hard, warn = image_findings(info, canvas, why=why)
    rep["hard_fail"], rep["warn"] = hard, warn
    rep["ok"] = not hard
    return rep
