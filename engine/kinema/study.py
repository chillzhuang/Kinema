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

"""参考片读片（study）：把一支本地参考片拆成**可测量量**，供立项前门参考。

「拉片/读片」是导演的立项动作——看一支同题材参考片，量出它的**节奏骨架**
（多久一刀、每镜多长、留白多少），再由指挥层决定自己这条片子的镜数与 motion
模式。本模块只做机械测量，**判定与取舍一律不做**。

## 命名为什么叫 study

`ref` / `reference` 在本工程已被占死两次：`assets/refs/` 是设定图、
`shots[].refs` / `moodboard` 是风格垫图。参考片与这两者语义完全无关（它不进
任何生成请求），撞词会让后续会话把参考片错当垫图喂给模型。`study` 全仓零命中。

## 三条硬护栏（版权 · 别改）

1. **契约里的路径一律是工作区相对路径**（`study/<slug>/ref.mp4`）。
   `storage/media.collect_media` 收录规则是「`/` 开头 + 媒体后缀 + 在工作区内 +
   文件存在」，`oss sync` 随后把命中的文件**传上用户自己的公网 OSS 并生成可访问
   URL**——第三方参考片一旦被收录就是公网转载。相对路径在 `media.py` 的
   `probe()` 第一行即被跳过（先例：`source.file`）。digest / frames_dir / subs
   同理，全部相对。
2. **产物目录不带 `_work` 后缀**。`studio/scanner.py` 的 `root.rglob("*_work")`
   是片库文件扫描的唯一入口，带后缀就会把参考片当成自己的成片收进片库。
   `project/<pid>/study/<slug>/` 天然避开。
3. **v1 完全不吃 URL、不引 yt-dlp**。引擎核心不联网抓第三方内容是既有事实边界
   （`pyproject.toml` 的 `dependencies = []`，urllib 只在 providers/storage/studio）。
   量节奏根本不需要下载器；要 URL 就得同时接受站点 ToS 与版权风险。
   参考片**绝不进交付目录**（`exports/`），读完即可 `study rm` 清掉本地副本。

## 引擎出什么 / 不出什么

**出**（全是 ffmpeg 能直接量的数）：时长 / fps / 分辨率 / 有无音轨、切点全表、
逐镜时长、切点密度（刀/分钟）、每镜平均时长、静音占比、等间隔关键帧。

**不出**：「这片子是不是运动量大」「该用 kenburns 还是 dubbed」——那是
**判定**，属指挥层（铁律「引擎内无 LLM provider」）。判定规则写在
`kinema-project/SKILL.md`「参考片立项模式」，引擎只交数。

## 分层

· **纯函数层**（`parse_scene_cuts` / `parse_silences` / `rhythm` / `frame_times`
  / `media_meta`）：零 IO，离线可测，ffmpeg 文本格式变了只改这一层。
· **探测层**（`probe_*` / `extract_*`）：走 `ffmpeg.run_capture`
  （`run()` 会吞掉分析滤镜的 info 级输出），**永不抛异常**，测不到即 None。
· **落盘层**（`ingest`）：切点全表 / 逐镜清单 / 抽帧索引进 sidecar
  `study/<slug>/digest.json`，契约只留指针 + 计数（同 `source/segments.json`
  先例——巨 blob 进 project.json 会拖垮每一次 `Project.load`）。

## 有限数纪律

落进 project.json 的数值一律过 `_finite`：`NaN`/`Infinity` 不是合法 JSON，
`json.dump` 会照吐，浏览器 `JSON.parse` 当场报错、Studio 整页白屏（本工程已
吃过两次亏）。测不到写 `None`，绝不写 `inf`。
"""
from __future__ import annotations

import json
import math
import re
import shutil
from datetime import datetime
from pathlib import Path

from .errors import ProjectError
from .ffmpeg import probe_json, run_capture

# ---------------------------------------------------------------------------
# 参数缺省（改这里等于改读片口径）
# ---------------------------------------------------------------------------
CUT_THRESHOLD = 0.3      # 场景切点判据：select='gt(scene,T)' 的 T（0~1，越小越敏感）
SILENCE_DB = -30.0       # 静音判据电平（dBFS）
SILENCE_MIN = 0.4        # 静音最短持续（秒）——短于此的呼吸停顿不算留白
DEFAULT_FRAMES = 24      # 缺省抽帧数
MAX_FRAMES = 48          # 抽帧硬上限：再多也读不过来，且撑爆 study 目录
CUT_MERGE_EPS = 0.04     # 切点去重容差（秒）：同一刀被两条滤镜各报一次时合并

# 允许入库的参考片容器（只认视频；音频/图片/文本一律拒收）
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _finite(x) -> float | None:
    """有限数守卫：非数 / NaN / ±Infinity 一律 None（落盘只准出现有限数或 None）。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


# ---------------------------------------------------------------------------
# 纯函数层：解析 / 推导（无 IO，永远可测）
# ---------------------------------------------------------------------------
def parse_scene_cuts(text: str) -> list[float]:
    """ffmpeg 文本输出 → 升序去重的切点时刻表（秒）。

    **同时认两种格式**，因为两条命令都可能被用来取切点：
    · `select='gt(scene,T)',showinfo` → `... pts_time:12.5 duration_time:...`
    · `scdet=t=N`                     → `[scdet @ …] lavfi.scd.score: 15.6, lavfi.scd.time: 12.5`

    正则必须锚死 `pts_time`：showinfo 同一行还有 `duration_time:0.1`，用泛化的
    `time:\\s*([\\d.]+)` 会把每一帧的帧长也当成切点，切点表当场翻倍。
    相隔 < `CUT_MERGE_EPS` 的两点视为同一刀（两条滤镜各报一次时会重）。"""
    if not text:
        return []
    hits: list[float] = []
    for m in re.finditer(r"\bpts_time:\s*(-?[\d.]+)", text):
        try:
            hits.append(float(m.group(1)))
        except ValueError:
            continue
    for m in re.finditer(r"lavfi\.scd\.time:\s*(-?[\d.]+)", text):
        try:
            hits.append(float(m.group(1)))
        except ValueError:
            continue
    out: list[float] = []
    for t in sorted(v for v in hits if math.isfinite(v) and v >= 0):
        if out and t - out[-1] < CUT_MERGE_EPS:
            continue
        out.append(round(t, 3))
    return out


def parse_silences(text: str, duration: float | None = None) -> list[list[float]]:
    """`silencedetect` 的 stderr → 静音区间表 `[[start, end], …]`（秒）。

    形如 `silence_start: 3.2` / `silence_end: 5.7 | silence_duration: 2.5`。
    **收尾悬空的 start 必须补上**：片子在静音中结束时 ffmpeg 只打 start 不打
    end，漏补会让「结尾大段留白」这一最典型的节奏特征直接消失。给了
    `duration` 就补到片尾，没给就丢弃该段（宁可少算不可算出负区间）。"""
    if not text:
        return []
    spans: list[list[float]] = []
    pending: float | None = None
    for m in re.finditer(r"silence_(start|end):\s*(-?[\d.]+)", text):
        kind, raw = m.group(1), m.group(2)
        try:
            t = float(raw)
        except ValueError:
            continue
        if not math.isfinite(t):
            continue
        if kind == "start":
            pending = max(0.0, t)
        elif pending is not None:
            if t > pending:
                spans.append([round(pending, 3), round(t, 3)])
            pending = None
    if pending is not None and duration is not None and duration > pending:
        spans.append([round(pending, 3), round(float(duration), 3)])
    return spans


def silence_ratio(spans: list[list[float]], duration: float | None) -> float | None:
    """静音占比（0~1）。无时长 / 无音轨 → None（**不写 0**，0 表示「测过，全程有声」）。"""
    d = _finite(duration)
    if d is None or d <= 0:
        return None
    total = sum(max(0.0, float(b) - float(a)) for a, b in spans)
    return round(min(1.0, total / d), 3)


def shot_table(cuts: list[float], duration: float | None) -> list[dict]:
    """切点 → 逐镜清单 `[{i, start, end, dur}, …]`（切点是**镜与镜的边界**）。

    N 个切点切出 N+1 镜。越界/倒序的切点（探测抖动）一律丢弃，保证 dur > 0。
    比较**用取整后的值**：切点 5.9996 与片长 6.0 各自 round(,3) 后都是 6.0，
    拿原值比会漏过它、造出一个 dur=0.0 的空镜（还会把 n_shots 多算一个）。"""
    d = _finite(duration)
    if d is None or d <= 0:
        return []
    end = round(d, 3)
    bounds = [0.0]
    for t in cuts:
        v = _finite(t)
        if v is None:
            continue
        rv = round(v, 3)
        if rv <= bounds[-1] or rv >= end:
            continue
        bounds.append(rv)
    bounds.append(end)
    return [{"i": i + 1, "start": bounds[i], "end": bounds[i + 1],
             "dur": round(bounds[i + 1] - bounds[i], 3)}
            for i in range(len(bounds) - 1)]


def rhythm(cuts: list[float], duration: float | None,
           silences: list[list[float]] | None = None,
           *, has_audio: bool = True) -> dict:
    """节奏可测量量（**只出数，不出判定**）。

    · `n_cuts` 切点数 / `n_shots` 镜数（=n_cuts+1）
    · `cuts_per_min` 切点密度（刀/分钟）——最直观的「快慢」标尺
    · `avg_shot_sec` 每镜平均时长 / `min_shot_sec` / `max_shot_sec`
    · `silence_ratio` 静音占比（无音轨 → None）

    「密度多少算快」「该配哪种 motion」不在这里判——见 SKILL「参考片立项模式」。"""
    table = shot_table(cuts, duration)
    d = _finite(duration)
    durs = [s["dur"] for s in table]
    n_cuts = max(0, len(table) - 1)
    return {
        "n_cuts": n_cuts,
        "n_shots": len(table),
        "cuts_per_min": (round(n_cuts / d * 60.0, 2) if d and d > 0 else None),
        "avg_shot_sec": (round(d / len(table), 2) if d and d > 0 and table else None),
        "min_shot_sec": (round(min(durs), 2) if durs else None),
        "max_shot_sec": (round(max(durs), 2) if durs else None),
        "silence_ratio": (silence_ratio(silences or [], d) if has_audio else None),
    }


def frame_times(duration: float | None, n: int) -> list[float]:
    """等间隔抽帧时刻表（确定性）：取 (k+0.5)/n 分位，**硬上限 `MAX_FRAMES`**。

    要的不是首尾帧而是「均匀铺满时间轴的一叠画」，故取区间中点而非端点
    （端点常撞上黑场/片头）。超上限即按时间轴均匀降采样，不是截断前 48 张。
    `n<=0` 明确表示「只要数不要图」（`--frames 0`），返回空表。"""
    d = _finite(duration)
    k = min(int(n or 0), MAX_FRAMES)
    if d is None or d <= 0 or k <= 0:
        return []
    return [round(d * (i + 0.5) / k, 3) for i in range(k)]


def media_meta(probe: dict | None) -> dict:
    """ffprobe JSON → `{dur, fps, width, height, has_audio, has_subs, vcodec}`。

    fps 取 `avg_frame_rate`（`r_frame_rate` 对 VFR 源会报出 1000 这类离谱值）；
    分母为 0（图片流/无帧率）时置 None，绝不 ZeroDivision、绝不写 inf。"""
    streams = (probe or {}).get("streams") or []
    fmt = (probe or {}).get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    fps = None
    if v:
        raw = str(v.get("avg_frame_rate") or "")
        if "/" in raw:
            num, _, den = raw.partition("/")
            try:
                num_f, den_f = float(num), float(den)
                fps = _finite(num_f / den_f) if den_f else None
            except ValueError:
                fps = None
    dur = None
    try:
        dur = _finite(float(fmt.get("duration")))
    except (TypeError, ValueError):
        dur = None
    if dur is None and v is not None:
        try:
            dur = _finite(float(v.get("duration")))
        except (TypeError, ValueError):
            dur = None
    return {
        "dur": (round(dur, 3) if dur is not None else None),
        "fps": (round(fps, 3) if fps is not None else None),
        "width": (int(v.get("width")) if v and v.get("width") else None),
        "height": (int(v.get("height")) if v and v.get("height") else None),
        "vcodec": (v.get("codec_name") if v else None),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_subs": any(s.get("codec_type") == "subtitle" for s in streams),
    }


def slugify(name: str, taken: set[str] | None = None) -> str:
    """文件名 → 目录 slug（ASCII 安全 + 去重）。中文文件名归一后为空即回落 `ref`。"""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:32] or "ref"
    taken = taken or set()
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


# ---------------------------------------------------------------------------
# 探测命令（形态守卫用例钉死这几条 flag）
# ---------------------------------------------------------------------------
def cut_args(path: str, threshold: float) -> list[str]:
    """场景切点探测：`select='gt(scene,T)',showinfo` + `-f null`。

    `-an` 必带（不解音频，快一大截）；showinfo 的行走 **stderr**（kn_log）。"""
    return ["-i", str(path), "-an",
            "-vf", f"select='gt(scene,{float(threshold):.3f})',showinfo",
            "-f", "null", "-"]


def silence_args(path: str, *, db: float = SILENCE_DB,
                 min_dur: float = SILENCE_MIN) -> list[str]:
    """静音探测。**`-vn` 必带**：无音轨时不加 `-vn` 会退出 0 且静默无输出，
    被误判成「测到了但全程有声」；加 `-vn` 才明确失败。"""
    return ["-i", str(path), "-vn",
            "-af", f"silencedetect=n={float(db):g}dB:d={float(min_dur):g}",
            "-f", "null", "-"]


def frame_args(path: str, t: float, out: str | Path) -> list[str]:
    """单帧抽取：`-ss` 前置快进（输入前 = 关键帧快跳）+ 只解一帧。"""
    return ["-ss", f"{max(0.0, float(t)):.3f}", "-i", str(path),
            "-frames:v", "1", "-q:v", "3", str(out)]


def subs_args(path: str, out: str | Path) -> list[str]:
    """内嵌字幕导出（第一条字幕流 → SRT）。无字幕流时 ffmpeg 非零退出，调用方按缺省处理。"""
    return ["-i", str(path), "-map", "0:s:0", str(out)]


# ---------------------------------------------------------------------------
# 探测层（永不抛异常，全部退化为「测不到」）
# ---------------------------------------------------------------------------
def probe_cuts(path: str, threshold: float = CUT_THRESHOLD) -> list[float]:
    try:
        rc, out, err = run_capture(cut_args(path, threshold), loglevel="info",
                                   desc="scene cuts")
    except Exception:            # noqa: BLE001  ffmpeg 不在/被杀不该中断读片
        return []
    return parse_scene_cuts(err + "\n" + out) if rc == 0 else []


def probe_silences(path: str, duration: float | None = None) -> list[list[float]]:
    try:
        rc, _out, err = run_capture(silence_args(path), loglevel="info",
                                    desc="silencedetect")
    except Exception:            # noqa: BLE001
        return []
    return parse_silences(err, duration) if rc == 0 else []


def extract_frames(path: str, times: list[float], outdir: Path) -> list[dict]:
    """按时刻表抽帧，返回 `[{file(相对 outdir), t}, …]`（抽不出的时刻静默跳过）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    got: list[dict] = []
    for i, t in enumerate(times, 1):
        name = f"f{i:02d}.jpg"
        dest = outdir / name
        try:
            rc, _o, _e = run_capture(frame_args(path, t, dest), loglevel="error",
                                     desc="frame")
        except Exception:        # noqa: BLE001
            continue
        if rc == 0 and dest.is_file() and dest.stat().st_size > 0:
            got.append({"file": name, "t": round(float(t), 3)})
    return got


def extract_subs(path: str, dest: Path) -> bool:
    """导出内嵌字幕到 `dest`（SRT）。无字幕流 / 失败 → False（不是错误）。"""
    try:
        rc, _o, _e = run_capture(subs_args(path, dest), loglevel="error", desc="subs")
    except Exception:            # noqa: BLE001
        return False
    if rc == 0 and dest.is_file() and dest.stat().st_size > 0:
        return True
    dest.unlink(missing_ok=True)
    return False


# ---------------------------------------------------------------------------
# 落盘层
# ---------------------------------------------------------------------------
def build_digest(*, slug: str, rel_file: str, source_name: str, sha256: str | None,
                 meta: dict, cuts: list[float], silences: list[list[float]],
                 frames: list[dict], params: dict) -> dict:
    """sidecar 全表（切点 / 逐镜 / 静音 / 抽帧索引）——**只落 digest.json，不进契约**。"""
    return {
        "slug": slug,
        "file": rel_file,
        "source_name": source_name,
        "sha256": sha256,
        "at": _now(),
        "params": params,
        "media": meta,
        "rhythm": rhythm(cuts, meta.get("dur"), silences,
                         has_audio=bool(meta.get("has_audio"))),
        "cuts": cuts,
        "shots": shot_table(cuts, meta.get("dur")),
        "silences": silences,
        "frames": frames,
    }


def contract_entry(digest: dict, *, title: str, rel_dir: str,
                   subs: str | None) -> dict:
    """digest → 契约条目（**只留指针 + 计数**）。

    切点全表 / 逐镜清单 / 抽帧索引一律不进 project.json——它们随片长线性膨胀，
    进契约就是每次 `Series.load` 都要解析一遍的死重量（`source/segments.json`
    先例）。要看全表读 `digest` 指的那个文件。"""
    return {
        "slug": digest["slug"],
        "title": title or digest["slug"],
        "file": digest["file"],                       # 工作区相对路径（版权护栏）
        "digest": f"{rel_dir}/digest.json",
        "frames_dir": f"{rel_dir}/frames",
        "subs": subs,
        "sha256": digest.get("sha256"),
        "media": digest["media"],
        "rhythm": digest["rhythm"],
        "n_frames": len(digest.get("frames") or []),
        "imported_at": digest["at"],
    }


def ingest(series, src: Path, *, title: str = "", slug: str | None = None,
           cuts: float = CUT_THRESHOLD, frames: int = DEFAULT_FRAMES,
           subs: str | Path | None = None) -> dict:
    """参考片入库（机械承接·零 LLM）：拷进工作区 → 量节奏 → 抽帧 → 落 digest
    → 回填契约条目。返回入库摘要 dict（= 契约条目）。

    `series` 只需提供 `dir` / `data` / `save()`（同 `Series`）。同 slug 重复导入
    = 覆盖重算（幂等），不堆版本栈——参考片是只读参照物，不是我们的产物。"""
    src = Path(src)
    if not src.is_file():
        raise ProjectError(f"找不到参考片: {src}")
    ext = src.suffix.lower()
    if ext not in VIDEO_EXTS:
        raise ProjectError(
            f"不是支持的参考片容器: {src.name}（认 {'/'.join(sorted(VIDEO_EXTS))}）"
            "——study 只读视频，图片走 `project moodboard`、剧本走 `adapt import`")
    try:
        probe = probe_json(src)
    except Exception as e:       # noqa: BLE001  ffprobe 解不开 = 不是可读视频
        raise ProjectError(f"参考片无法解析（不是可读视频或已损坏）: {src.name}\n{e}") from e
    meta = media_meta(probe)
    if not meta.get("vcodec") or not meta.get("dur"):
        raise ProjectError(f"参考片没有可读视频流: {src.name}")
    # 外挂字幕**必须在拷贝之前校验**：拷完再报错会在盘上留一份没登记的第三方片子，
    # 那正是版权卫生最不该出现的状态（无主副本、下次会话不知道它是哪来的）。
    subs_src = Path(subs) if subs else None
    if subs_src is not None and not subs_src.is_file():
        raise ProjectError(f"找不到外挂字幕: {subs_src}")

    entries: list[dict] = series.data.setdefault("study", [])
    taken = {e.get("slug") for e in entries if isinstance(e, dict)}
    if slug:
        sl = slugify(slug)
    else:
        sl = slugify(src.stem, taken)
    root = Path(series.dir) / "study" / sl
    frames_dir = root / "frames"
    if root.exists():                      # 幂等重导：先清干净再重算，不留上一版残帧
        shutil.rmtree(root)
    frames_dir.mkdir(parents=True, exist_ok=True)

    dest = root / f"ref{ext}"
    shutil.copyfile(src, dest)
    rel_dir = f"study/{sl}"
    rel_file = f"{rel_dir}/ref{ext}"       # ← 相对路径：collect_media 不收，绝不上云

    from . import lineage
    sha = lineage.fingerprint(str(dest))
    cut_list = probe_cuts(str(dest), cuts)
    sil = probe_silences(str(dest), meta.get("dur")) if meta.get("has_audio") else []
    fr = extract_frames(str(dest), frame_times(meta.get("dur"), frames), frames_dir)

    rel_subs: str | None = None
    if subs_src is not None:
        shutil.copyfile(subs_src, root / "subs.srt")
        rel_subs = f"{rel_dir}/subs.srt"
    elif meta.get("has_subs") and extract_subs(str(dest), root / "subs.srt"):
        rel_subs = f"{rel_dir}/subs.srt"

    digest = build_digest(
        slug=sl, rel_file=rel_file, source_name=src.name, sha256=sha, meta=meta,
        cuts=cut_list, silences=sil, frames=fr,
        params={"cut_threshold": round(float(cuts), 3), "silence_db": SILENCE_DB,
                "silence_min": SILENCE_MIN, "frames": len(fr)})
    (root / "digest.json").write_text(
        json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

    entry = contract_entry(digest, title=title, rel_dir=rel_dir, subs=rel_subs)
    with series.commit():
        kept = [e for e in series.data.get("study") or []
                if isinstance(e, dict) and e.get("slug") != sl]
        series.data["study"] = kept + [entry]
    return entry


def remove(series, slug: str) -> dict:
    """移除一条读片记录 + 整个 `study/<slug>/` 目录（**版权卫生：读完即删**）。

    参考片是第三方素材，节奏量抽完就没有继续留在盘上的理由。整目录删净（片子 +
    digest + 抽帧）而非只删片体：半留状态最容易在下次会话里被误当自有素材。
    要留数据请先自行拷走 `digest.json`。"""
    entries: list[dict] = series.data.setdefault("study", [])
    hit = next((e for e in entries if isinstance(e, dict) and e.get("slug") == slug), None)
    if hit is None:
        raise ProjectError(f"没有这条读片记录: {slug}"
                           f"（现有: {', '.join(e.get('slug', '') for e in entries) or '—'}）")
    root = Path(series.dir) / "study" / slug
    if root.is_dir():
        shutil.rmtree(root)
    with series.commit():
        series.data["study"] = [e for e in series.data.get("study") or []
                                if not (isinstance(e, dict) and e.get("slug") == slug)]
    return hit
