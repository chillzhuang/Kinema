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

"""三个逐帧感知模型的懒加载。

`Bundle` 是本模块对外的唯一形状：姿态、深度、分割各出一个可调用对象，编排层
只认这三个方法，不认它们背后是真模型还是替身。`--mock` 走 `MockBundle`，
于是 io/几何/跟踪/时序/渲染/编码/sidecar 全链路在离线测试里真跑，只有推理是假的。

两条平台约束写死在这里，改之前先复现：
· 姿态固定跑 CPU——走 CoreML 时检测器输出张量的秩对不上，直接崩；
· 分割用 `selfie_multiclass` 而不是更快的 `deeplab_v3`——后者 22ms 但遮罩糊成
  一团、手臂会丢，快出来的时间全赔在轮廓上。
"""
from __future__ import annotations

import numpy as np

from ..errors import KinemaError
from .params import DEPTH_SIZE
from . import weights as weights_mod

# ImageNet 归一化——深度权重导出时就带着它，换一组数值出来的是另一张图
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)

# 人物 = 头发/身体皮肤/脸部皮肤/衣服/饰品（类别 1~5），背景 0 与其余排除
_PERSON_CLASSES = (1, 6)

_deps: tuple[bool, list[str]] | None = None


def deps_missing() -> list[str]:
    """缺哪些依赖（含被覆盖掉的 opencv 构建）。结果记忆化——doctor 与 Studio
    就绪条会反复问，而 `find_spec` 扫盘不是零成本。"""
    global _deps
    if _deps is not None:
        return _deps[1]
    import importlib.util as ilu
    lack = [n for n, m in (("onnxruntime", "onnxruntime"), ("mediapipe", "mediapipe"),
                           ("opencv-contrib-python", "cv2"), ("rtmlib", "rtmlib"))
            if ilu.find_spec(m) is None]
    if "opencv-contrib-python" not in lack:
        # contrib 版的判据是 ximgproc 在不在，不是包名在不在：`pip install rtmlib`
        # 会把普通 opencv-python 装进同一个 cv2 命名空间、盖掉 contrib 构建，
        # 而那时导入照常成功，只有用到引导滤波才会 AttributeError——那已是渲染中途。
        try:
            import cv2
            if not hasattr(cv2, "ximgproc"):
                lack.append("opencv-contrib-python（已被 opencv-python 覆盖，需重装）")
        except Exception:  # noqa: BLE001  可选依赖：任何导入失败都按不可用处理
            lack.append("opencv-contrib-python")
    _deps = (not lack, lack)
    return lack


def readiness() -> tuple[bool, list[str]]:
    """`(是否就绪, 缺失项文案)` —— doctor、`setup` 与 Studio 就绪条共用一份判定。"""
    notes = []
    lack = deps_missing()
    if lack:
        notes.append("依赖缺失：" + "、".join(lack)
                     + "（`pip install -e \"engine[control]\"` 后再 "
                       "`pip install --no-deps rtmlib`）")
    lack_w = weights_mod.missing()
    if lack_w:
        notes.append("权重缺失：" + "、".join(n for n, _u, _t in lack_w)
                     + "（`python3 -m kinema control fetch`）")
    return (not notes), notes


def available() -> bool:
    return readiness()[0]


class Bundle:
    """真模型三件套。构造即加载——调用方按素材持有一次，不逐帧重建。"""

    def __init__(self) -> None:
        lack = deps_missing()
        if lack:
            raise KinemaError(
                "深度捕捉的依赖不齐：" + "、".join(lack)
                + "\n  ① pip install -e \"engine[control]\"\n"
                  "  ② pip install --no-deps rtmlib —— 它同时声明了两个 opencv 发行版，"
                  "直接装会盖掉 contrib 构建")
        weights_mod.require()

        import mediapipe as mp
        import onnxruntime as ort
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        from rtmlib import Body

        self._mp = mp
        self._body = Body(mode="lightweight", to_openpose=True,
                          backend="onnxruntime", device="cpu")
        so = ort.SessionOptions()
        so.log_severity_level = 3
        # 深度是三者里最重的一步，CoreML 后端能省掉约三分之一；不可用时
        # onnxruntime 自己回落到 CPU，无需我们判断平台
        self._depth = ort.InferenceSession(
            str(weights_mod.weight_path("depth_anything_v2_vits.onnx")), so,
            providers=[("CoreMLExecutionProvider",
                        {"ModelFormat": "MLProgram", "MLComputeUnits": "ALL"}),
                       "CPUExecutionProvider"])
        self._depth_in = self._depth.get_inputs()[0].name
        seg = str(weights_mod.weight_path("selfie_multiclass_256x256.tflite"))
        opts = vision.ImageSegmenterOptions
        self._seg_video = vision.ImageSegmenter.create_from_options(opts(
            base_options=mp_python.BaseOptions(model_asset_path=seg),
            running_mode=vision.RunningMode.VIDEO, output_category_mask=True))
        self._seg_image = vision.ImageSegmenter.create_from_options(opts(
            base_options=mp_python.BaseOptions(model_asset_path=seg),
            running_mode=vision.RunningMode.IMAGE, output_category_mask=True))

    def pose(self, bgr):
        """`(N×18×2 关键点, N×18 置信度)`，OpenPose-18 顺序。"""
        return self._body(bgr)

    def depth(self, rgb):
        """正方形 RGB 块 → `DEPTH_SIZE²` 的相对逆深度。"""
        import cv2
        x = cv2.resize(rgb, (DEPTH_SIZE, DEPTH_SIZE),
                       interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255.0
        x = ((x - _MEAN) / _STD).transpose(2, 0, 1)[None]
        out = self._depth.run(None, {self._depth_in: x})[0]
        return out[0] if out.ndim == 3 else out[0, 0]

    def segment(self, rgb, ts_ms: int | None = None):
        """人物遮罩（bool）。给了时间戳走视频模式（模型自带帧间平滑），
        否则按单图——每人裁切复判走的是后者。"""
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB,
                             data=np.ascontiguousarray(rgb))
        r = (self._seg_video.segment_for_video(img, int(ts_ms))
             if ts_ms is not None else self._seg_image.segment(img))
        # category mask 带一条尾随通道轴，取回来要压掉
        cat = r.category_mask.numpy_view().reshape(rgb.shape[:2])
        return (cat >= _PERSON_CLASSES[0]) & (cat < _PERSON_CLASSES[1])

    def close(self) -> None:
        self._seg_video.close()
        self._seg_image.close()


class MockBundle:
    """确定性替身：一个正弦摆动的站姿人偶 + 径向深度 + 椭圆遮罩。

    第二个人偶只在画幅够宽时出现（竖屏素材里两个人会重叠成一团，反而测不出
    跟踪）。姿态随帧号解析给出，故整条链路逐字节可复现。
    """

    def __init__(self, *, people: int = 2) -> None:
        self._people = people
        self._t = 0

    def _figure(self, w: int, h: int, cx: float, phase: float):
        """18 点站姿：颈/肩/肘/腕/髋/膝/踝 + 五官，手臂随相位摆动。"""
        s = min(w, h) / 6.0
        sway = np.sin(phase) * s * 0.35
        top = h * 0.18
        pt = {
            0: (cx, top + s * 0.55), 1: (cx, top + s * 1.1),
            2: (cx - s * 0.5, top + s * 1.1), 3: (cx - s * 0.5 - sway * 0.4, top + s * 1.8),
            4: (cx - s * 0.5 - sway, top + s * 2.4),
            5: (cx + s * 0.5, top + s * 1.1), 6: (cx + s * 0.5 + sway * 0.4, top + s * 1.8),
            7: (cx + s * 0.5 + sway, top + s * 2.4),
            8: (cx - s * 0.3, top + s * 2.6), 9: (cx - s * 0.3, top + s * 3.4),
            10: (cx - s * 0.3, top + s * 4.2),
            11: (cx + s * 0.3, top + s * 2.6), 12: (cx + s * 0.3, top + s * 3.4),
            13: (cx + s * 0.3, top + s * 4.2),
            14: (cx - s * 0.12, top + s * 0.45), 15: (cx + s * 0.12, top + s * 0.45),
            16: (cx - s * 0.22, top + s * 0.5), 17: (cx + s * 0.22, top + s * 0.5),
        }
        return np.array([pt[i] for i in range(18)], np.float32)

    def pose(self, bgr):
        h, w = bgr.shape[:2]
        n = self._people if w >= h else 1
        kps = [self._figure(w, h, w * (i + 1) / (n + 1), self._t * 0.25 + i * 1.7)
               for i in range(n)]
        self._t += 1
        return np.stack(kps), np.ones((n, 18), np.float32)

    def depth(self, rgb):
        y, x = np.mgrid[0:DEPTH_SIZE, 0:DEPTH_SIZE].astype(np.float32)
        c = (DEPTH_SIZE - 1) / 2.0
        r = np.hypot(x - c, y - c) / c
        return np.clip(1.0 - r, 0.0, 1.0)

    def segment(self, rgb, ts_ms: int | None = None):
        h, w = rgb.shape[:2]
        n = self._people if w >= h else 1
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        m = np.zeros((h, w), bool)
        for i in range(n):
            cx, cy = w * (i + 1) / (n + 1), h * 0.55
            m |= (((x - cx) / (w * 0.16 / n)) ** 2 + ((y - cy) / (h * 0.42)) ** 2) < 1.0
        return m

    def close(self) -> None:
        return None


def load(*, mock: bool = False):
    """按需给出模型三件套。`mock` 是**显式参数**而不是环境变量——进程级开关会在
    同进程跑的多个用例之间泄漏，而引擎里所有替身路由都是显式传参。"""
    return MockBundle() if mock else Bundle()
