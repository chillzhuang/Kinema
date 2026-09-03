# 第三方 vendored 资产 · NOTICE

本目录下的文件**不是本工程代码**，是随 Studio 一起分发的第三方库副本，
**保留其上游原始 license 头、逐字节未改**。

**刻意不盖本工程的 AGPL 头**：这些文件的著作权属于上游作者、按 MIT 授权，
在其上加盖我们的许可声明等于对他人代码主张授权——AGPL 第 7 条允许把 MIT 代码
并入 AGPL 作品（宽松→强 Copyleft 单向兼容），但**并入不改变这些文件自身的许可**，
MIT 的署名与许可声明必须原样保留。源码守卫 `TestLicenseNotices` 因此显式跳过本目录。

## Three.js r185

| 文件 | 上游 | 说明 |
|---|---|---|
| `three.module.js` | `three@0.185.0/build/three.module.min.js` | 主模块（ESM，压缩版） |
| `three.core.min.js` | `three@0.185.0/build/three.core.min.js` | 核心（被 `three.module.js` 以 `./three.core.min.js` 相对导入，**文件名不能改**） |
| `jsm/controls/OrbitControls.js` | `three@0.185.0/examples/jsm/controls/OrbitControls.js` | 导演视角轨道控制器 |
| `jsm/controls/TransformControls.js` | `three@0.185.0/examples/jsm/controls/TransformControls.js` | 摆放/旋转 gizmo |

- **许可证**：MIT（Copyright 2010-2026 Three.js Authors，SPDX-License-Identifier: MIT）。
  完整文本见 <https://github.com/mrdoob/three.js/blob/dev/LICENSE>。
- **为什么 vendored 而不是 CDN**：Studio 是**本地化工作室工具**，必须离线可用；
  且 CDN 版本漂移会让 previz 的确定性承诺失效（同一个场景在不同时间渲出不同结果）。
- **为什么没有 GLTFLoader / SkeletonUtils**：默认角色是**程序化生成的灰模人偶**
  （`director/rig.js` 按骨架表建 SkinnedMesh、按关键帧表建 AnimationClip），不加载
  外部 GLB——既让 previz 逐字节可复现，也避免第三方角色资产的再分发许可问题。
  日后若要支持导入自备 GLB，再按需补 `jsm/loaders/GLTFLoader.js` 与
  `jsm/utils/SkeletonUtils.js`（后者是多角色蒙皮克隆的唯一正确做法，
  `Object3D.clone()` 会坏蒙皮）。

## 升级方式

```bash
V=0.185.0   # 换成目标版本
cd engine/autovideo/studio_app/vendor
curl -sSo three.module.js      https://unpkg.com/three@$V/build/three.module.min.js
curl -sSo three.core.min.js    https://unpkg.com/three@$V/build/three.core.min.js
curl -sSo jsm/controls/OrbitControls.js    https://unpkg.com/three@$V/examples/jsm/controls/OrbitControls.js
curl -sSo jsm/controls/TransformControls.js https://unpkg.com/three@$V/examples/jsm/controls/TransformControls.js
```

升级后务必确认 `three.module.js` 内对核心包的相对导入路径仍是
`./three.core.min.js`（三方在 r160+ 才拆出 core，路径变了就要同步改文件名或
`index.html` 的 import-map），并重跑一次 3D 控制台的渲染冒烟。
