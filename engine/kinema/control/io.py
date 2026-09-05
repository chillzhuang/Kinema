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

"""逐帧管道 —— ffmpeg 是唯一的解码/编码总线。

`run()` 收不下逐帧流（它一次跑完再回收 stdout），故这一层自己开 `Popen`。
两条纪律因此必须在本模块里显式兑现，`run()` 帮不上忙：

· **超时取 `ffmpeg.default_timeout()`**，不自己再读一遍环境变量——超时口径只有一份；
· **argv 上必须出现 `_work/` 路径**，否则孤儿回收器认不出这个进程。父进程被杀时
  一个读着用户桌面文件、写向 `pipe:` 的解码器会永远占着一颗核心。这就是源片
  必须先归一进 `assets/<id>/source.mp4` 再开跑的原因——那不是格式洁癖。

参数构造器与执行分开：命令行形状是能被钉住的契约，而钉住它不需要真跑 ffmpeg。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import FFmpegError
from ..ffmpeg import default_timeout, probe_frames, probe_json
from .params import CRF


def probe_source(path: str | Path) -> dict:
    """源片规格：宽高、帧率、帧数、时长、有无音轨。帧数逐帧数出来（见 `probe_frames`）。"""
    info = probe_json(path)
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise FFmpegError(f"没有视频流: {path}")
    num, _, den = str(v.get("avg_frame_rate") or "0/1").partition("/")
    fps = (float(num) / float(den)) if float(den or 0) else 0.0
    if fps <= 0:
        raise FFmpegError(f"读不出帧率: {path}")
    frames = probe_frames(path)
    return {
        "width": int(v["width"]), "height": int(v["height"]),
        "fps": round(fps, 6), "frames": frames,
        "seconds": round(frames / fps, 3),
        "audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def normalise_args(src: str | Path, dst: str | Path, fps: float) -> list[str]:
    """源片 → 定帧率副本。可变帧率的源片上，「一帧进一帧出」这条不变量在解码端
    就不成立，故先归一成正片源，之后的取帧与取音轨都以归一后的文件为准。"""
    return ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-fps_mode", "cfr",
            "-r", f"{fps:.6f}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(dst)]


def decode_args(src: str | Path, w: int, h: int) -> list[str]:
    """解码成 rgb24 裸流。`-fps_mode passthrough` 是「一帧进一帧出」的执行面：
    没有它，ffmpeg 会按输出帧率增删帧，逐帧结果与源片就此错位。"""
    return ["ffmpeg", "-v", "error", "-i", str(src), "-map", "0:v:0",
            "-fps_mode", "passthrough", "-vf", f"scale={w}:{h}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def encode_args(dst: str | Path, src: str | Path, w: int, h: int, fps: float,
                has_audio: bool) -> list[str]:
    """裸流 → mp4，并把源片音轨原样复制过来。

    **绝不加 `-shortest`**。它看起来像在防「音轨比视频长几十毫秒」，实际是把
    帧数守恒这条硬不变量交给了音频的采样对齐：AAC 一帧 1024 采样，任何时长落不到
    帧边界的片子（也就是大多数）音轨都会比视频短一点点，`-shortest` 于是砍掉最后
    一个视频帧：3.000s 的片子会出来 89/90 帧，整条链直接判失败。

    反过来音轨长一点则完全无害：视频流的帧数由我们喂进去的帧决定，`probe_frames`
    数的是 `v:0`，绑定裁段与它的无声发送副本同样只以视频帧为准。
    """
    args = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{w}x{h}", "-r", f"{fps:.6f}", "-i", "pipe:0"]
    if has_audio:
        args += ["-i", str(src), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
    return args + ["-c:v", "libx264", "-preset", "medium", "-crf", str(CRF),
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]


def fit_filter(fit: str, w: int, h: int) -> str:
    """把一路画面贴合到 `w×h`：`pad` 等比缩小后补黑边，`crop` 等比放大后居中裁。

    裁段与对照片共用这一条：对照片若另拼一份，crop 贴合下两处的裁切范围就会各说各的，
    审看件里出现模型没收到的画面。
    """
    if fit == "crop":
        return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},setsar=1")
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def probe_dims(path: str | Path) -> tuple[int, int]:
    """只取宽高不数帧：拼接前判画幅取向用，成片几百帧不必为此逐帧解码。"""
    info = probe_json(path)
    v = next((s for s in (info.get("streams") or []) if s.get("codec_type") == "video"), None)
    if not v:
        raise FFmpegError(f"没有视频流: {path}")
    return int(v["width"]), int(v["height"])


def probe_audio(path: str | Path) -> bool:
    """有没有音频流：裁段后据此决定要不要另出无声的发送副本。"""
    info = probe_json(path)
    return any(s.get("codec_type") == "audio" for s in info.get("streams") or [])


def stack_args(dst: str | Path, tiles, *, canvas: tuple[int, int], tile: int,
               fps: float, audio_from: int | None = None, fit: str = "pad",
               tail=None) -> list[str]:
    """把几段画面并到一条对照片里 —— 对照片（二合一 / 三合一）的唯一构造口。

    拼接走 ffmpeg 而不是在渲染循环里多开一路编码器：源文件此刻都已在盘，
    纯转码比再走一遍 numpy 逐帧快一个量级，也不拖慢真正吃 CPU 的那两遍。

    `tiles` 每项是 `(路径, 起点秒, 段长秒)`：各路起点本就不同（源片与控制视频要从
    绑定区间起裁，成片本身就是那一段），裁切因此在这里一并做，不落中间文件——
    先裁一遍再拼一遍等于每格编码两次。

    `canvas` 是每格统一到的画幅，**取素材原片的画幅**：不匹配的那一路按 `fit` 装进来，
    各格等大才比得出运动。`fit` 与裁段同一口径（`fit_filter`）：crop 贴合的镜前两格
    得套同一条裁切，否则看到的是模型没收到的画面。`tile` 是被摞的那一维的像素数
    ——对照片是审看件，按「浏览器里拖得动」定档而不是按源片分辨率。

    **摞的方向按画幅长边定**：竖画幅横排、横画幅竖摞。16:9 的两格并排是 32:9、三格
    是 16:3，而灯箱的播放位定宽——画幅越扁它给得出的高度越少，每格反而比换个方向摞
    时小一半。归一的那一维随方向换边：`hstack` 要各路同高、`vstack` 要各路同宽。

    帧率也必须先归一：两个 stack 遇到各路帧率不一致会按最快那一路补帧，出来的对照片
    时长对而帧数翻倍，逐帧比对当场失真——而三路本就来自三处（源片、控制段、厂商
    成片），厂商给的成片帧率不保证跟章节一致。

    `tail` 是画幅取向与 `canvas` 相反的那一路（竖拍素材配 16:9 成片是常态）：塞进
    素材格里只剩中间一条细画面，故不进主拼接，另起一行或一列——竖片横排时落到下一行、
    宽对齐整行；横片竖摞时贴到右侧、高对齐整列。它单独占一行或一列，只等比缩放不裁不补。

    交给 `ffmpeg.run` 执行，故不带 `ffmpeg` 与日志开关——同 `normalise_args`。
    """
    cw, ch = canvas
    vertical = cw > ch
    if vertical:                                    # 横画幅：竖着摞，各格同宽
        w, h = tile, int(round(ch * tile / cw / 2)) * 2
    else:                                           # 竖画幅：横着排，各格同高
        w, h = int(round(cw * tile / ch / 2)) * 2, tile
    fitf = fit_filter(fit, w, h)
    args: list[str] = []
    for path, start, seconds in tiles:
        args += ["-ss", f"{float(start):.3f}", "-t", f"{float(seconds):.3f}",
                 "-i", str(path)]
    n = len(tiles)
    chains = "".join(f"[{i}:v]fps={fps:.6f},{fitf}[t{i}];" for i in range(n))
    labels = "".join(f"[t{i}]" for i in range(n))
    graph = f"{chains}{labels}{'vstack' if vertical else 'hstack'}=inputs={n}"
    if tail is None:
        graph += "[v]"
    else:
        path, start, seconds = tail
        args += ["-ss", f"{float(start):.3f}", "-t", f"{float(seconds):.3f}",
                 "-i", str(path)]
        if vertical:        # 主拼接竖摞（宽 w、高 n×h）：竖的那一路贴右侧，高对齐整列
            scale, join = f"scale=-2:{n * h}", "hstack"
        else:               # 主拼接横排（高 h、宽 n×w）：横的那一路落下一行，宽对齐整行
            scale, join = f"scale={n * w}:-2", "vstack"
        graph += (f"[m];[{n}:v]fps={fps:.6f},{scale},setsar=1[tl];"
                  f"[m][tl]{join}=inputs=2[v]")
    args += ["-filter_complex", graph, "-map", "[v]"]
    if audio_from is not None:
        # `?` 让没有音轨的源片照常出片：对照片是给人看的，静音不是失败
        args += ["-map", f"{audio_from}:a?", "-c:a", "aac", "-b:a", "128k"]
    return args + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]


def decode_frames(src: str | Path, w: int, h: int):
    """逐帧产出 `(h, w, 3)` 的 uint8 RGB。读不满一帧即认为流结束。"""
    import numpy as np
    proc = subprocess.Popen(decode_args(src, w, h), stdout=subprocess.PIPE)
    n = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(n)
            if len(buf) < n:
                return
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        # 生成器可能被提前丢弃（异常/break）：不收掉子进程就会留下一个
        # 写不进管道的解码器一直空转
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
        proc.wait(timeout=default_timeout())


class Encoder:
    """一路编码进程。`write` 收整帧字节，`close` 等它收尾并检查退出码。"""

    def __init__(self, dst, src, w, h, fps, has_audio):
        self.dst = Path(dst)
        self._proc = subprocess.Popen(
            encode_args(dst, src, w, h, fps, has_audio),
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame) -> None:
        self._proc.stdin.write(frame.tobytes())

    def close(self) -> None:
        # communicate() 自己关 stdin 再收流——先手动关会让它在 flush 时撞上已关闭的句柄
        _out, err = self._proc.communicate(timeout=default_timeout())
        if self._proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-15:]
            raise FFmpegError(f"编码失败: {self.dst.name}\n" + "\n".join(tail))
