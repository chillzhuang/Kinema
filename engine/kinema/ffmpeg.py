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

"""FFmpeg / ffprobe 封装。

合成链路的地基。用 subprocess 直接驱动系统 ffmpeg（比 MoviePy 更稳、依赖更少）。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .errors import FFmpegError

# 单次 ffmpeg 调用的超时上限（秒）——防「父进程还活着、ffmpeg 却无限空转」。
# 缺省 1 小时：最重的整章合成也远在其内，而真正的异常（滤镜死循环/管道悬死）
# 会被切断而不是烧一整晚。0 或非法值 = 不设限（KINEMA_FFMPEG_TIMEOUT 覆盖）。
def _default_timeout() -> float | None:
    try:
        v = float(os.environ.get("KINEMA_FFMPEG_TIMEOUT", "3600"))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def ensure_tools() -> None:
    """确认 ffmpeg / ffprobe 可用，否则给出可执行的安装提示。"""
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise FFmpegError(
            f"未找到 {', '.join(missing)}。请安装 FFmpeg：\n"
            "  macOS:  brew install ffmpeg\n"
            "  Ubuntu: sudo apt install ffmpeg"
        )


def run(args: list[str], *, desc: str = "", timeout: float | None = None) -> None:
    """运行一条 ffmpeg 命令；失败时抛出带 stderr 尾部的错误。

    `timeout`（秒）缺省取 `_default_timeout()`——超时时 subprocess 会**杀掉子进程**
    再抛 TimeoutExpired，这里转成 FFmpegError；防线针对「父进程活着而 ffmpeg
    异常空转」。父进程被 SIGKILL 留下的孤儿由 `reap_orphan_ffmpeg` 在下次
    doctor / studio 启动时收割（进程内无法拦 SIGKILL，只能事后清）。
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    tmo = timeout if timeout is not None else _default_timeout()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=tmo)
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(
            f"ffmpeg 超时（>{tmo:.0f}s）已终止{(' · ' + desc) if desc else ''}——"
            "多半是滤镜死循环或输入悬死；可用 KINEMA_FFMPEG_TIMEOUT 调上限") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"ffmpeg 失败{(' · ' + desc) if desc else ''}:\n{tail}")


def run_capture(args: list[str], *, loglevel: str = "info",
                desc: str = "") -> tuple[int, str, str]:
    """运行一条 ffmpeg 命令并**完整捕获**输出，返回 (returncode, stdout, stderr)。

    与 `run()` 的分工（两者刻意不合并）：
    - `run()` 是**渲染**原语——写死 `-loglevel error`、成功即丢弃输出、失败抛
      `FFmpegError`。合成/运镜/转场/水印全在用，签名与语义是既有契约。
    - `run_capture()` 是**探测**原语——日志级别可调（分析类滤镜 blackdetect /
      volumedetect / scdet 的结论只在 info 级 stderr 里）、**永不抛异常**，
      调用方自行判读 returncode 与文本。

    `-hide_banner -loglevel <level> -y` 由本函数拼，调用方只给输入与滤镜参数。
    `desc` 仅作调用方拼错误信息时的上下文标记（本原语不打印、不抛错）。
    超时同样**不抛**（永不抛异常是本原语的契约）：杀掉子进程后返回 rc=124
    （GNU timeout 的既定语义）+ 超时说明，调用方按失败判读。
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", loglevel, "-y", *args]
    tmo = _default_timeout()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=tmo)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return 124, out or "", f"ffmpeg 超时（>{tmo:.0f}s）已终止"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ---------------------------------------------------------------- 孤儿收割
# 父进程被 SIGKILL（测试被杀 / 会话强退）时，子 ffmpeg 无人认领会一直跑，可持续
# 数天占满 CPU。SIGKILL 在进程内拦不住，唯一可靠的解法是**事后收割**：
# doctor / studio 启动时按双重判据精确识别并清理。
# 判据缺一不可：① PPID=1（父进程已死，被 init/launchd 收养——任何活着的合法
# 渲染，其父都是那个还在跑的 python）；② 命令行带我们的产物路径签名 `_work/`
# （clips/build/animatic/previz 全部落 *_work/ 目录）——绝不误杀用户自己或
# 其他软件的 ffmpeg（截屏录制类常驻 ffmpeg 的 PPID 也是 1，全靠签名区分）。

_ORPHAN_MARK = "_work/"


def find_orphan_ffmpeg(ps_lines: list[str] | None = None) -> list[dict]:
    """找出孤儿 ffmpeg 进程：`[{pid, cmd}]`。`ps_lines` 可注入（测试用）。"""
    if ps_lines is None:
        try:
            out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                                 capture_output=True, text=True, timeout=10)
            ps_lines = out.stdout.splitlines()
        except Exception:
            return []
    orphans = []
    for ln in ps_lines:
        parts = ln.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = parts
        exe = cmd.split(None, 1)[0]
        # 必须是 ffmpeg 二进制本体（不是 grep ffmpeg / 编辑器里打开的路径）
        if exe != "ffmpeg" and not exe.endswith("/ffmpeg"):
            continue
        if ppid != "1":
            continue                     # 父进程还活着——那是别人正跑着的渲染
        if _ORPHAN_MARK not in cmd:
            continue                     # 没有我们的产物路径签名——不是我们的
        try:
            orphans.append({"pid": int(pid), "cmd": cmd})
        except ValueError:
            continue
    return orphans


def reap_orphan_ffmpeg(*, kill: bool = True, ps_lines: list[str] | None = None,
                       _kill=None) -> list[dict]:
    """收割孤儿 ffmpeg。`kill=False` 只侦察不动手（doctor 的报告模式）。"""
    orphans = find_orphan_ffmpeg(ps_lines)
    if kill:
        do_kill = _kill or os.kill
        for o in orphans:
            try:
                do_kill(o["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return orphans


def probe_duration(path: str | Path) -> float:
    """返回媒体时长（秒）。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 无法读取时长: {path}\n{proc.stderr.strip()}")
    try:
        return float(proc.stdout.strip())
    except ValueError as e:
        raise FFmpegError(f"ffprobe 返回的时长无法解析: {proc.stdout!r}") from e


def tempo_chain(ratio: float) -> list[str]:
    """把任意时间伸缩比拆成合法的 `atempo` 链（单级只接受 0.5~2.0，超范围要串联）。

    `atempo` 是**变速不变调**（相位声码器），所以压缩语速不会把人声压成花栗鼠。
    ratio > 1 = 加速（音频变短）。"""
    chain: list[str] = []
    r = float(ratio)
    while r > 2.0:
        chain.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        chain.append("atempo=0.5")
        r /= 0.5
    if abs(r - 1.0) > 1e-3:
        chain.append(f"atempo={r:.4f}")
    return chain


def concat_audio(parts: list[tuple[str, object]], out_path: str | Path,
                 *, sample_rate: int = 44100, tail_fade: float = 0.0) -> None:
    """拼接旁白音轨。parts 为有序段列表：
      · `("file", 路径)`            —— 真实配音，原样接入；
      · `("silence", 秒数)`         —— 静音段（无旁白的"纯画面镜"占位、停顿垫片）；
      · `("cut", (路径, 头部秒数))` —— 裁去头部静音后接入（dubbed 配音前移贴合
        底片开口时点，裁剪量由 `voicecast.dubbed_sync_offset` 钳在语音起点之前）；
      · `("fit", (路径, 目标秒数))` —— 配音**变速不变调**贴合给定窗口（native 混烧：
        画面时长由 Seedance 片段/计费秒数定死，配音只能去适配它）。

    用 filter concat（每个输入独立解码）而非 concat demuxer——各家 provider 的
    音频编码/容器不一（甚至 mp3 字节写进 .wav），demuxer 要求同构会出错；
    静音段用 anullsrc 即时生成，无旁白的"纯画面镜"由此占住时间轴，
    保证后续所有镜的音画字对位。

    `tail_fade` > 0 时每段真实配音（file/cut/fit）尾部淡出这么多秒，静音段不动。"""
    inputs: list[str] = []
    fc: list[str] = []
    labels: list[str] = []
    for idx, (kind, val) in enumerate(parts):
        extra = ""
        if kind == "file":
            inputs += ["-i", str(val)]
        elif kind == "cut":
            path, head = val
            inputs += ["-i", str(path)]
            # asetpts 归零是 concat 的前提：atrim 保留原时间戳，不归零则该段
            # 在拼接图里带着裁掉的偏移入场，整轨从这一段起错位
            extra = f",atrim=start={float(head):.3f},asetpts=PTS-STARTPTS"
        elif kind == "fit":
            path, target = val
            inputs += ["-i", str(path)]
            src = probe_duration(path)
            if src > 0 and target and target > 0:
                ch = tempo_chain(src / float(target))
                if ch:
                    # 变速后再按目标硬裁一刀：atempo 的输出长度有毫秒级误差，
                    # 逐镜攒起来又会变成整轨漂移（这条轨的全部意义就是对位）
                    extra = "," + ",".join(ch) + f",atrim=0:{float(target):.3f}"
        else:
            inputs += ["-f", "lavfi", "-t", f"{float(val):.3f}",
                       "-i", f"anullsrc=r={sample_rate}:cl=mono"]
        if kind != "silence" and tail_fade > 0:
            # 用 areverse 而不用 afade 的 st= 定位：mp3 容器头的时长比解码样本数多一帧，
            # 按头时长定位的淡出会落在音频之外
            extra += f",areverse,afade=t=in:d={float(tail_fade):.3f},areverse"
        fc.append(f"[{idx}:a]aresample={sample_rate},"
                  f"aformat=channel_layouts=mono{extra}[a{idx}]")
        labels.append(f"[a{idx}]")
    graph = ";".join(fc) + ";" + "".join(labels) + f"concat=n={len(parts)}:v=0:a=1[out]"
    run([*inputs, "-filter_complex", graph, "-map", "[out]",
         "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le", str(out_path)],
        desc="concat narration")


def to_pcm(path: str | Path, *, end: float | None = None) -> None:
    """把 provider 回吐的音频原地转成 PCM wav，`end` 给了就同时裁到该秒数。
    无 Xing 头的 mp3 按码率估时长，比解码样本数多一帧（24 kHz 下 48 ms），
    逐镜累计就是整轨漂移；PCM 的时长即样本数。"""
    src = Path(path)
    tmp = src.with_name(src.stem + ".pcm.wav")
    cut = ["-t", f"{float(end):.3f}"] if end else []
    run(["-i", str(src), *cut, "-c:a", "pcm_s16le", str(tmp)], desc="pcm normalize")
    tmp.replace(src)


def first_frame(src: str | Path, out: str | Path) -> None:
    """抽取视频**第 0 帧**为静图（previz → Seedance first_frame / reference_image）。

    用 `select=eq(n,0)` 按**帧号**取而非 `-ss 0` 按时间戳取：容器起始时间戳不一定
    是 0（B 帧/编辑列表都会让 `-ss 0` 落到别的帧上），而 previz 的第 0 帧正是控制台
    渲染循环里 `t=0` 那一帧——它必须与 3D 场景的起始构图逐像素对得上，否则「首帧锁
    构图」这条承诺就断了。`-frames:v 1` 只写一张，`-an` 免得给图片流塞音频。
    """
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run(["-i", str(src), "-vf", r"select=eq(n\,0)", "-frames:v", "1", "-an",
         "-update", "1", str(out)], desc="extract first frame")


def last_frame(src: str | Path, out: str | Path) -> None:
    """抽取视频**末帧**为静图（previz → Seedance last_frame，锁终态位姿）。

    `-sseof -0.1` 从文件末尾回退 0.1s 起解，取该段第一帧——比「先 probe 时长再
    `-ss 时长-ε`」少一次 ffprobe 且不受时长精度影响。`-sseof` 是**输入选项**，
    必须写在 `-i` 之前。极短/异常容器上 `-sseof` 可能取不到帧，此时回退到按时长
    定位；两条都失败才让 FFmpegError 冒泡（那是真的坏片，不该静默出一张空图）。
    """
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["-sseof", "-0.1", "-i", str(src), "-frames:v", "1", "-an",
             "-update", "1", str(out)], desc="extract last frame")
        if Path(out).is_file() and Path(out).stat().st_size > 0:
            return
    except FFmpegError:
        pass
    dur = probe_duration(src)
    run(["-ss", f"{max(0.0, dur - 0.08):.3f}", "-i", str(src), "-frames:v", "1", "-an",
         "-update", "1", str(out)], desc="extract last frame (fallback)")


def default_timeout() -> float | None:
    """单次 ffmpeg 调用的超时上限（秒），`None` = 不设限。

    `run()` 内部自己会取它，但逐帧管道走的是 `subprocess.Popen`、用不上 `run()`——
    那条路必须能拿到同一个值，否则要么各读一次环境变量（超时口径就有两份），
    要么干脆不设限（解码端 stdout 管道写满而无人读时会永久悬死）。
    """
    return _default_timeout()


def probe_frames(path: str | Path) -> int:
    """逐帧解码数出的视频帧数（`nb_read_frames`）。

    容器头里的 `nb_frames` 对很多源片是 0 或干脆没有，而「输入多少帧就输出多少帧」
    是控制视频的硬不变量——只能实数。代价是完整解一遍视频流。
    """
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
           "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=_default_timeout())
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 数帧失败: {path}\n{proc.stderr.strip()}")
    try:
        return int(proc.stdout.strip().rstrip(","))
    except ValueError:
        raise FFmpegError(f"ffprobe 数不出帧数: {path}（{proc.stdout.strip()!r}）") from None


def probe_json(path: str | Path) -> dict:
    """返回 ffprobe 的完整 JSON（streams + format）。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 失败: {path}\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def find_font() -> str | None:
    """返回一个可用的 drawtext 字体路径（找不到返回 None）。"""
    for f in _FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    return None


# 中文水印/角标用的 CJK 字体候选（macOS 系统字体 / Linux Noto·文泉驿）
_CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def find_font_cjk() -> str | None:
    """返回一个支持中文的 drawtext 字体路径（找不到回退 find_font）。"""
    for f in _CJK_FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    return find_font()


# filtergraph 的选项值经两级解析：图级把单引号内的内容原样交给滤镜，滤镜级再按
# `:` 拆键值并处理 `\` 转义。单引号不能出现在引号串内，须断开引号、在引号外
# 以 `\\` `\'` 写出反斜杠与引号（滤镜级读到 `\'`）再续上引号。
_QUOTE_BREAK = "'\\\\\\''"


def filter_literal(value) -> str:
    """把文本编成 filtergraph 选项值的单引号字面量（含外层引号）：文件路径、
    fontsdir/fontfile 一类直接送给滤镜的值。"""
    out = []
    for ch in str(value):
        if ch == "'":
            out.append(_QUOTE_BREAK)
        elif ch == "\\":
            out.append("\\\\")
        elif ch == ":":
            out.append("\\:")
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def drawtext_text(value) -> str:
    """drawtext 的 text 值字面量（含外层引号）。text 在两级解析之上还经 drawtext
    自身的展开：反斜杠转义下一字符、`%{…}` 是函数——裸 `%` 使整段不渲染而退出码
    为 0，故 `%` 与反斜杠要多编一级。"""
    out = []
    for ch in str(value):
        if ch == "'":
            out.append(_QUOTE_BREAK)
        elif ch == "\\":
            out.append("\\\\\\\\")
        elif ch == ":":
            out.append("\\:")
        elif ch == "%":
            out.append("\\\\%")
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def concat_entry(path) -> str:
    """concat demuxer 清单的一行：单引号内以 `'\\''` 断开再续上。"""
    return "file '" + str(path).replace("'", "'\\''") + "'\n"
