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

"""离线 mock 图生视频 provider：用 Ken Burns 运镜把首帧图做成一段片段，并打 i2v 角标。

用于离线验证图生视频全链路（gen-video → 片段 → 合成）。真实观感由 Seedance 等提供。
"""
from __future__ import annotations

from pathlib import Path

from ..base import VideoProvider, VideoResult


class MockVideoProvider(VideoProvider):
    name = "mock"
    # mock 是**整条链路的离线替身**，能力面必须与主力 provider 同形：报 False 会让
    # `--mock` 与 `--dry-run --mock` 这两条主要验证路径**永远走不到 V2V 分支**——
    # 提示词、末帧取舍、报价三处都与真跑不同，离线全链路就失去了预演的意义。
    supports_reference_video = True
    supports_reference_images = True     # 同上：离线也要能走"附简笔分镜板"分支
    supports_ref_audio = True            # 离线彩排也要能走 dubbed 分支
    supports_return_last_frame = True    # 同上：离线也要能走尾帧接力分支
    max_ref_audios = 3                   # 同上：离线也要能走音色锚定分支（与 2.0 系列同额）
    max_ref_audio_seconds = 15.0

    def input_video_seconds(self, ref_seconds: float) -> int:
        """与 seedance 同口径（整秒·2~15 钳制），只为让离线报价的**形状**对得上。
        mock 单价为 0，这些秒数最终乘不出钱来，不会污染台账。"""
        import math
        if not ref_seconds or ref_seconds <= 0:
            return 0
        return max(2, min(15, math.ceil(ref_seconds)))

    def generate(self, image, out_path, *, prompt="", dur=5.0, width=1080, height=1920,
                 seed=None, last_frame=None, ref_images=None, reference_only=False,
                 **kwargs) -> VideoResult:
        # ref_images / reference_only 显式收下并回显进 meta.request：离线守卫要能
        # 断言参考装配的真实形状（塞进 **kwargs 就是静默漏测——路线判定改了、
        # 请求形状变了，mock 链路一声不吭全绿）
        from ...pipeline import kenburns
        # 片段时长是 provider 契约的一部分：引擎按买下的整秒排时间轴，而
        # `fit_clip` 的 keep_audio 分支不补帧——替身若按净请求秒渲染，离线成片
        # 会逐镜短一截
        secs = float(self.billable_seconds(
            float(dur), dubbed=bool(kwargs.get("ref_audio"))))
        kenburns.render_shot(image, secs, str(out_path),
                             width=width, height=height, fps=30,
                             effect_index=(int(seed) if seed else 0),
                             label="i2v (mock)")
        meta = {"provider": "mock",
                "request": {"image": str(image) if image else None,
                            "ref_images": [str(r) for r in (ref_images or [])],
                            "reference_only": bool(reference_only),
                            "last_frame": str(last_frame) if last_frame else None}}
        if kwargs.get("return_last_frame"):
            # 尾帧回传的离线替身：真跑回的是模型末帧图 URL，mock 用首帧图副本占位
            # ——形状（meta 键、落盘路径、下一镜接力）与真跑一致即可
            import shutil
            tail = Path(str(out_path)).with_suffix(".tail.png")
            shutil.copyfile(image, tail)
            meta["last_frame_url"] = str(tail)
        return VideoResult(path=str(out_path), cost=0.0, has_audio=False,
                           meta=meta)
