/*
  This file is part of Kinema.
  Copyright (C) 2018-2099 BladeX (https://bladex.cn)

  SPDX-License-Identifier: AGPL-3.0-or-later

  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU Affero General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU Affero General Public License for more details.

  You should have received a copy of the GNU Affero General Public License
  along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

/* ============================================================================
   3D 导演控制台 · 灰模骨架与动作库（程序化生成，零外部资产）

   **为什么不下载 GLB**（这是本模块存在的全部理由）：
     ① previz 的核心承诺是**逐字节可复现**——外部资产一换（哪怕只是重新导出一次），
        同一个场景渲出的帧就变了，「成片跟随预演」的对照关系随之失效；
     ② 灰模人偶本就该是**无身份的体块**：身份/材质/光影全部交给 AI 生成阶段与设定图，
        previz 只负责说清「谁在哪、往哪走、做什么、镜头怎么动」；
     ③ 免掉第三方角色资产的再分发许可问题（Mixamo 的 EULA 明令禁止再分发原始资产），
        也免掉几十 MB 二进制入库。

   **实现要点**：不用 `SkinnedMesh`，用**命名 Object3D 层级 + 刚性体块**。
   `AnimationMixer` 按节点名绑定（`PropertyBinding` 解析 `thighL.quaternion`），
   对普通 Object3D 一样成立——省掉整套蒙皮权重，且刚性关节正是木质人偶的观感。
   多角色因此也不需要 `SkeletonUtils.clone()`：每个 actor 各建一份，天然独立。

   **坐标与朝向约定**（与 engine/kinema/pipeline/camera.py 严格一致）：
     · 右手系 Y-up，脚踩 y=0，**角色面向 +Z**；单位＝米。
     · 每根骨骼节点位于**关节处**，其体块沿该骨骼的固定方向画出去。
     · 姿势用**欧拉角（度）**表达，统一记 `swing`：**正 = 这根骨骼的远端向前(+Z)摆**。
       送进 `rotation.x` 时按骨骼的远端指向分两组取号（见 `AXIAL` / `quatOf`）：
       四肢与脚的远端指向 −Y 或 +Z，取负号；躯干链 hips/spine/chest/neck/head 的
       远端指向 +Y，取正号。**两组的符号必须分开算**——`R_x(θ)` 对 (0,−1,0) 与
       (0,+1,0) 的作用方向相反，用同一个符号会让「上身前倾」写出来变成后仰。
     · 关节只朝解剖学允许的方向弯：肘只向前屈（`forearm*` swing ≥ 0）、
       膝只向后屈（`shin*` swing ≤ 0）。守卫 `test_joint_angles_respect_anatomy`。
     · **父级俯仰后，子骨骼填的仍是相对父级的角度**。躯干一旦整体俯仰（`hips`
       非零），腿与臂的世界朝向已跟着转过一次，此时按世界朝向填角度即把同一次
       旋转叠两遍。求法：总角 = hips 角 − 该肢角。
   ========================================================================== */

import * as THREE from "three";

const D = Math.PI / 180;

/* ---------------------------------------------------------------- 资产注册表 */
/* 下面三张表的 key 必须与引擎 `kinema/previz.py` 的
   DIRECTOR_MODELS / DIRECTOR_ACTIONS / DIRECTOR_PROPS 逐一对齐——
   目录经 /api/overview 下发驱动前端 UI，这里少一个 key 就是「选项点了没反应」。
   锁步由 tests/test_previz.py::test_frontend_rig_registry_locksteps_with_catalog
   解析本文件的 @registry 标记守卫。 */

/* @registry:models */
export const MODELS = {
  mannequin_m: { height: 1.78, shoulder: 0.115, hip: 0.050, bulk: 1.00, headScale: 1.00 },
  mannequin_f: { height: 1.66, shoulder: 0.098, hip: 0.054, bulk: 0.88, headScale: 1.02 },
  mannequin_n: { height: 1.72, shoulder: 0.106, hip: 0.052, bulk: 0.94, headScale: 1.00 },
  mannequin_c: { height: 1.24, shoulder: 0.100, hip: 0.050, bulk: 0.92, headScale: 1.28 },
};
/* @end:models */

/* `seat`（可选）＝**座面在道具局部坐标里的位置**，y 为座面高度。角色动作为
   骑乘/上车/坐下 且站位靠近该类道具时，`stage.snapToSeats` 按「座面高度 − 当前姿势
   的臀底偏移」摆放根节点，故不同体型与不同落座姿都落在座面上。
   声明了 `seat` 的道具必须列进 `stage.SEAT_FOR`，否则锚点不会触发。 */
/* @registry:props */
export const PROPS = {
  box:     { size: [0.8, 0.8, 0.8], seat: [0, 0.80, 0],
             build: (g) => g.box(0.8, 0.8, 0.8, 0) },
  pillar:  { size: [0.45, 3.2, 0.45], build: (g) => g.box(0.45, 3.2, 0.45, 0) },
  wall:    { size: [4.0, 2.8, 0.2], build: (g) => g.box(4.0, 2.8, 0.2, 0) },
  table:   { size: [1.8, 0.75, 0.8], build: (g) => { g.box(1.8, 0.08, 0.8, 0.71);
             [[-0.82, -0.32], [0.82, -0.32], [-0.82, 0.32], [0.82, 0.32]]
               .forEach(([x, z]) => g.box(0.07, 0.71, 0.07, 0.355, x, z)); } },
  door:    { size: [1.0, 2.1, 0.15], build: (g) => { g.box(0.12, 2.1, 0.15, 1.05, -0.44);
             g.box(0.12, 2.1, 0.15, 1.05, 0.44); g.box(1.0, 0.12, 0.15, 2.04); } },
  tree:    { size: [1.6, 4.0, 1.6], build: (g) => { g.cyl(0.16, 2.0, 1.0);
             g.sphere(0.8, 3.0); } },
  rock:    { size: [1.2, 0.9, 1.1], seat: [0, 0.778, 0],
             build: (g) => g.sphere(0.58, 0.36, 0, 0, [1, 0.72, 0.94]) },
  vehicle: { size: [1.9, 1.5, 4.4], seat: [0, 0.86, -0.2],
             build: (g) => { g.box(1.9, 0.75, 4.4, 0.45);
             g.box(1.62, 0.62, 2.1, 1.12, 0, -0.2); } },
  horse:   { size: [0.6, 1.95, 2.2], seat: [0, 1.325, -0.05],
             build: (g) => { g.box(0.5, 0.55, 1.5, 1.05);
             [[-0.18, -0.55], [0.18, -0.55], [-0.18, 0.55], [0.18, 0.55]]
               .forEach(([x, z]) => g.box(0.12, 0.78, 0.12, 0.39, x, z));
             g.box(0.18, 0.55, 0.24, 1.5, 0, 0.78);
             g.box(0.16, 0.22, 0.5, 1.82, 0, 0.98); } },
  chair:   { size: [0.55, 1.05, 0.55], seat: [0, 0.495, 0.06],
             build: (g) => { g.box(0.5, 0.07, 0.5, 0.46);
             g.box(0.5, 0.58, 0.07, 0.78, 0, -0.22);
             [[-0.2, -0.2], [0.2, -0.2], [-0.2, 0.2], [0.2, 0.2]]
               .forEach(([x, z]) => g.box(0.05, 0.43, 0.05, 0.215, x, z)); } },
  stairs:  { size: [1.2, 1.5, 2.25], build: (g) => { for (let i = 0; i < 5; i++)
             g.box(1.2, 0.3 * (i + 1), 0.45, 0, 0, i * 0.45 - 0.9); } },
  barrier: { size: [1.7, 1.13, 0.36], build: (g) => { g.box(1.6, 1.05, 0.3, 0);
             g.box(1.7, 0.08, 0.36, 1.09); } },
  lamp:    { size: [0.4, 3.1, 0.4], build: (g) => { g.cyl(0.05, 2.9);
             g.sphere(0.15, 2.98); } },
  bed:     { size: [1.5, 0.9, 2.1], seat: [0, 0.505, 0.8],
             build: (g) => { g.box(1.5, 0.35, 2.05, 0);
             g.box(1.42, 0.15, 1.9, 0.43); g.box(1.5, 0.55, 0.08, 0.62, 0, -1.0); } },
  sofa:    { size: [1.8, 0.9, 0.85], seat: [0, 0.40, 0.12],
             build: (g) => { g.box(1.8, 0.4, 0.85, 0);
             g.box(1.8, 0.5, 0.18, 0.62, 0, -0.34);
             g.box(0.18, 0.55, 0.85, 0.28, -0.81, 0);
             g.box(0.18, 0.55, 0.85, 0.28, 0.81, 0); } },
  bench:   { size: [1.6, 0.5, 0.42], seat: [0, 0.485, 0],
             build: (g) => { g.box(1.6, 0.07, 0.42, 0.45);
             g.box(0.08, 0.44, 0.4, 0.22, -0.7, 0);
             g.box(0.08, 0.44, 0.4, 0.22, 0.7, 0); } },
  fence:   { size: [2.4, 0.95, 0.1], build: (g) => { g.box(2.4, 0.06, 0.05, 0.52);
             g.box(2.4, 0.06, 0.05, 0.86);
             [-1.15, 0, 1.15].forEach((x) => g.box(0.08, 0.95, 0.08, 0, x, 0)); } },
  campfire: { size: [0.8, 0.35, 0.8], build: (g) => { g.box(0.7, 0.08, 0.1, 0.05);
             g.box(0.1, 0.08, 0.7, 0.05); g.sphere(0.14, 0.18); } },
  sign:    { size: [0.7, 2.2, 0.2], build: (g) => { g.cyl(0.04, 2.1);
             g.box(0.7, 0.4, 0.06, 1.85); } },
  arch:    { size: [1.9, 2.9, 0.3], build: (g) => { g.box(0.25, 2.6, 0.25, 0, -0.75, 0);
             g.box(0.25, 2.6, 0.25, 0, 0.75, 0); g.box(1.9, 0.3, 0.3, 2.72); } },
  shelf:   { size: [1.2, 1.9, 0.35], build: (g) => {
             g.box(0.06, 1.9, 0.35, 0.95, -0.57); g.box(0.06, 1.9, 0.35, 0.95, 0.57);
             [0.06, 0.5, 0.94, 1.38, 1.84].forEach((y) => g.box(1.2, 0.05, 0.35, y));
             g.box(1.2, 1.9, 0.04, 0.95, 0, -0.17); } },
  house:   { size: [3.6, 3.5, 4.2], build: (g) => {
             g.box(3.6, 2.2, 4.2, 1.1); g.box(3.9, 0.16, 4.5, 2.28);
             g.box(2.9, 0.5, 4.4, 2.61); g.box(2.0, 0.45, 4.3, 3.06);
             g.box(1.0, 0.4, 4.2, 3.45);
             g.box(1.0, 0.25, 0.5, 0.12, 0, 2.32); } },
  rampart: { size: [6.2, 4.4, 1.5], build: (g) => {
             g.box(6.0, 3.6, 1.2, 1.8); g.box(6.2, 0.25, 1.5, 3.72);
             [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5].forEach(
               (x) => g.box(0.6, 0.55, 0.4, 4.12, x, -0.45)); } },
  tower:   { size: [2.9, 6.7, 2.9], build: (g) => {
             g.box(2.6, 3.0, 2.6, 1.5); g.box(2.2, 2.2, 2.2, 4.1);
             g.box(2.9, 0.2, 2.9, 5.3); g.box(1.6, 0.9, 1.6, 5.85);
             g.box(0.9, 0.5, 0.9, 6.45); } },
  gate:    { size: [5.4, 5.4, 2.0], build: (g) => {
             g.box(1.6, 3.4, 1.6, 1.7, -1.7); g.box(1.6, 3.4, 1.6, 1.7, 1.7);
             g.box(5.0, 0.9, 1.6, 3.85); g.box(5.4, 0.2, 2.0, 4.4);
             g.box(3.6, 0.9, 1.4, 4.95); g.box(3.8, 0.18, 1.7, 5.5); } },
  bridge:  { size: [3.0, 2.0, 7.0], build: (g) => {
             g.box(3.0, 0.35, 7.0, 1.2);
             g.box(1.2, 1.2, 1.2, 0.6, 0, -2.4); g.box(1.2, 1.2, 1.2, 0.6, 0, 2.4);
             g.box(0.18, 0.6, 7.0, 1.68, -1.41); g.box(0.18, 0.6, 7.0, 1.68, 1.41); } },
  well:    { size: [1.5, 2.3, 1.5], seat: [0, 0.82, 0], build: (g) => {
             g.cyl(0.62, 0.7); g.cyl(0.5, 0.12, 0.76);
             g.box(0.12, 1.6, 0.12, 1.4, -0.5); g.box(0.12, 1.6, 0.12, 1.4, 0.5);
             g.box(1.3, 0.14, 0.4, 2.2); } },
  pagoda:  { size: [3.1, 8.0, 3.1], build: (g) => {
             [[2.4, 1.5, 0.75], [2.0, 1.4, 2.75], [1.6, 1.3, 4.5], [1.2, 1.1, 6.0]]
               .forEach(([w, hh, y]) => {
                 g.box(w, hh, w, y); g.box(w + 0.7, 0.14, w + 0.7, y + hh / 2 + 0.1); });
             g.cyl(0.12, 1.0, 7.15); g.sphere(0.2, 7.8); } },
  stele:   { size: [1.1, 2.6, 0.7], build: (g) => {
             g.box(1.1, 0.3, 0.7, 0.15); g.box(0.8, 2.0, 0.28, 1.3);
             g.box(0.9, 0.28, 0.34, 2.44); } },
  lantern: { size: [0.7, 2.2, 0.7], build: (g) => {
             g.cyl(0.32, 0.22); g.cyl(0.14, 1.0, 0.72);
             g.box(0.42, 0.4, 0.42, 1.42); g.box(0.68, 0.14, 0.68, 1.69);
             g.cyl(0.24, 0.26, 1.88); g.sphere(0.1, 2.08); } },
  altar:   { size: [2.4, 0.96, 2.4], seat: [0, 0.90, 0], build: (g) => {
             g.cyl(1.2, 0.28); g.cyl(0.95, 0.26, 0.41);
             g.cyl(0.72, 0.24, 0.66); g.cyl(0.5, 0.12, 0.84); } },
  bush:    { size: [1.4, 1.1, 1.4], build: (g) => {
             g.sphere(0.52, 0.5, 0, 0, [1, 0.75, 1]);
             g.sphere(0.36, 0.34, 0.42, 0.2); g.sphere(0.32, 0.3, -0.38, -0.26); } },
  bamboo:  { size: [1.1, 4.6, 1.1], build: (g) => {
             [[0, 0, 4.5], [0.34, 0.18, 4.0], [-0.3, 0.26, 4.2], [0.14, -0.34, 3.6]]
               .forEach(([x, z, hh]) => g.cyl(0.055, hh, hh / 2, x, z));
             [[0.28, 0.36, 2.9], [-0.24, -0.3, 3.4]]
               .forEach(([x, z, y]) => g.sphere(0.2, y, x, z, [1.6, 0.5, 1.6])); } },
  cliff:   { size: [4.0, 2.7, 3.2], build: (g) => {
             g.box(4.0, 1.2, 3.2, 0.6); g.box(3.0, 0.9, 2.4, 1.6, 0.3, -0.2);
             g.box(1.8, 0.7, 1.6, 2.35, -0.5, 0.3); } },
  log:     { size: [0.9, 0.6, 3.4], seat: [0, 0.60, 0], build: (g) => {
             const t = g.cyl(0.29, 3.4, 0.3); t.rotation.x = Math.PI / 2;
             const s = g.cyl(0.12, 0.8, 0.5); s.rotation.set(Math.PI / 2, 0, 0.7);
             s.position.set(0.3, 0.5, 1.1); } },
  container: { size: [2.5, 2.6, 6.1], build: (g) => {
             g.box(2.4, 2.4, 6.0, 1.3); g.box(2.5, 0.14, 6.1, 0.14);
             g.box(2.5, 0.14, 6.1, 2.5);
             [-2.4, -1.2, 0, 1.2, 2.4].forEach(
               (z) => g.box(2.46, 2.2, 0.07, 1.3, 0, z)); } },
};
/* @end:props */

/* 动作库：`dur` 秒（一个循环周期）· `loop` 是否循环 · `speed` 内建位移速度（米/秒，
   0=原地）。`speed` 是**步态同步**的分母：控制台按「路线长度 ÷ 镜头时长」求出实际
   地速后取 `timeScale = 地速 / speed`——不同步就会脚滑（脚在原地蹭而人在飘）。 */
/* @registry:actions */
export const ACTIONS = {
  idle:   { dur: 3.2, loop: true,  speed: 0.0 },
  walk:   { dur: 1.0, loop: true,  speed: 1.35 },
  run:    { dur: 0.62, loop: true, speed: 4.2 },
  jump:   { dur: 1.1, loop: false, speed: 0.0 },
  crawl:  { dur: 1.6, loop: true,  speed: 0.55 },
  prone:  { dur: 4.0, loop: true,  speed: 0.0 },
  fly:    { dur: 2.4, loop: true,  speed: 3.0 },
  sit:    { dur: 4.0, loop: true,  speed: 0.0 },
  turn:   { dur: 1.2, loop: false, speed: 0.0 },
  wave:   { dur: 1.6, loop: false, speed: 0.0 },
  fall:   { dur: 1.4, loop: false, speed: 0.0 },
  attack: { dur: 1.0, loop: false, speed: 0.0 },
  crouch: { dur: 1.1, loop: true,  speed: 0.9 },
  dodge:  { dur: 0.9, loop: false, speed: 0.0 },
  cover:  { dur: 3.0, loop: true,  speed: 0.0 },
  enter:  { dur: 1.6, loop: false, speed: 0.0 },
  ride:   { dur: 2.0, loop: true,  speed: 0.0 },
};
/* @end:actions */

/* 影棚三点布光的色温、强度与方位：主光投影给出立体感，冷调补光让暗部有层次，
   轮廓光把灰模从深色地面上剥离。视口与缩略图共用同一组取值：选择器里挑中的样子
   必须与摆进舞台后一致。
   平行光只取方向，故这组坐标与场景尺度无关；投影范围与阴影贴图分辨率跟随各自
   场景的尺度，不进本表。 */
export const LIGHT_RIG = {
  key: { color: 0xfff0dd, intensity: 2.3, pos: [6, 11, 7] },
  fill: { color: 0x9db8e8, intensity: 0.5, pos: [-7, 5, 2] },
  rim: { color: 0xd4e4ff, intensity: 1.15, pos: [-3, 7, -9] },
  hemi: { sky: 0x76839c, ground: 0x14161b, intensity: 0.6 },
};

/* ------------------------------------------------------------------ 骨骼构造 */
// 骨骼节点名——姿势表与 AnimationClip 的 track 名都引用它们，改名必须同步改姿势表
export const BONES = ["hips", "spine", "chest", "neck", "head",
  "armL", "forearmL", "handL", "armR", "forearmR", "handR",
  "thighL", "shinL", "footL", "thighR", "shinR", "footR"];

// 骨长比例（× 身高）——比例取自常见人体测量均值，previz 只求体块可信，不求解剖精确
const P = {
  hipY: 0.530, spine: 0.130, chest: 0.150, neck: 0.052, head: 0.130,
  thigh: 0.245, shin: 0.235, foot: 0.150,
  arm: 0.165, forearm: 0.155, hand: 0.055,
};

function bone(parent, name, offset) {
  const o = new THREE.Object3D();
  o.name = name;
  o.position.set(offset[0], offset[1], offset[2]);
  parent.add(o);
  return o;
}

/** 骨盆体块的尺寸（× 身高）。落座吸附要按它反推臀底，故与建模共用一处取值。 */
function pelvisBlock(modelKey) {
  const spec = MODELS[modelKey] || MODELS.mannequin_n;
  const h = spec.height, b = spec.bulk;
  return { len: P.spine * h * 0.55, w: 0.20 * h * b, d: 0.13 * h * b, dir: [0, 1, 0] };
}

/** 骨盆体块底面相对 `hips` 节点的下沉量（米）。 */
export function pelvisDrop(modelKey) {
  const { len, w, d } = pelvisBlock(modelKey);
  const r = Math.min(w, d) / 2;
  return Math.max(len - r * 2, len * 0.2) / 2 + r - len / 2;
}

/** 给某根骨骼挂一段体块：`dir` 是骨骼指向（体块从关节沿它画出去）。
 *  体块用**胶囊**而非方盒：两端天然圆润，相邻体块在关节处以圆头相接，
 *  转动时不露方角缝——素体木人偶的观感，方盒才是「玩具感」的第一来源。 */
function limb(node, mat, { len, w, d, dir = [0, -1, 0] }) {
  const r = Math.min(w, d) / 2;
  const g = new THREE.CapsuleGeometry(r, Math.max(len - r * 2, len * 0.2), 5, 20);
  const m = new THREE.Mesh(g, mat);
  m.castShadow = true;
  m.scale.set(w / (r * 2), 1, d / (r * 2));   // 圆截面拉成椭圆截面（躯干要有厚薄）
  m.position.set(dir[0] * len / 2, dir[1] * len / 2, dir[2] * len / 2);
  if (dir[2] !== 0) m.rotation.x = Math.PI / 2;      // 脚掌：沿 +Z 平躺
  node.add(m);
  return m;
}

/**
 * 建一个灰模人偶。返回 `THREE.Group`（root，名为 "actor"），其子树含全部命名骨骼节点。
 * `root.userData.bones` 给出名字→节点的映射，供姿势应用与检查器读取。
 */
export function buildMannequin(modelKey = "mannequin_n", opts = {}) {
  const spec = MODELS[modelKey] || MODELS.mannequin_n;
  const h = spec.height;
  const b = spec.bulk;
  // Standard（PBR）而非 Lambert：配合 ACES 色调映射与三点布光，体块才有
  // 「哑光树脂」的材质感——Lambert 的纯漫反射在深色场景里只会糊成一片灰
  const mat = new THREE.MeshStandardMaterial({
    color: opts.color ?? 0x9aa3b2, roughness: 0.58, metalness: 0.08,
    emissive: 0x0a0c10,
  });

  const root = new THREE.Group();
  root.name = "actor";

  const hips = bone(root, "hips", [0, P.hipY * h, 0]);
  limb(hips, mat, pelvisBlock(modelKey));

  const spine = bone(hips, "spine", [0, P.spine * h * 0.4, 0]);
  limb(spine, mat, { len: P.spine * h, w: 0.185 * h * b, d: 0.12 * h * b, dir: [0, 1, 0] });

  const chest = bone(spine, "chest", [0, P.spine * h, 0]);
  limb(chest, mat, { len: P.chest * h, w: 0.225 * h * b, d: 0.135 * h * b, dir: [0, 1, 0] });

  const neck = bone(chest, "neck", [0, P.chest * h, 0]);
  limb(neck, mat, { len: P.neck * h, w: 0.055 * h, d: 0.055 * h, dir: [0, 1, 0] });

  const head = bone(neck, "head", [0, P.neck * h, 0]);
  const hs = P.head * h * spec.headScale;
  // 头用椭球不用方盒——头是人偶最大的一块「脸面」，方盒头 = 乐高，椭球头 = 素体
  const hm = new THREE.Mesh(new THREE.SphereGeometry(hs * 0.5, 28, 22), mat);
  hm.scale.set(0.8, 1.04, 0.9);
  hm.position.y = hs / 2;
  hm.castShadow = true;
  head.add(hm);
  // 朝向标记：鼻尖小楔子——没有它，灰模转身时看不出正反面（previz 最要紧的信息之一）
  const nose = new THREE.Mesh(new THREE.ConeGeometry(hs * 0.10, hs * 0.18, 4),
    new THREE.MeshStandardMaterial({ color: 0xd7dbe4, roughness: 0.5, metalness: 0.05 }));
  nose.rotation.x = Math.PI / 2;
  nose.position.set(0, hs * 0.52, hs * 0.46);
  head.add(nose);

  const sx = spec.shoulder * h;
  const shoulderY = P.chest * h * 0.86;
  for (const side of ["L", "R"]) {
    const s = side === "L" ? 1 : -1;
    const arm = bone(chest, `arm${side}`, [s * sx, shoulderY, 0]);
    // 关节垫球：肩头圆润过渡——臂与躯干的直角接缝是「拼装玩具感」的主要来源
    const pad = new THREE.Mesh(new THREE.SphereGeometry(0.052 * h * b, 14, 12), mat);
    pad.castShadow = true;
    arm.add(pad);
    limb(arm, mat, { len: P.arm * h, w: 0.058 * h * b, d: 0.058 * h * b });
    const fore = bone(arm, `forearm${side}`, [0, -P.arm * h, 0]);
    limb(fore, mat, { len: P.forearm * h, w: 0.050 * h * b, d: 0.050 * h * b });
    const hand = bone(fore, `hand${side}`, [0, -P.forearm * h, 0]);
    limb(hand, mat, { len: P.hand * h, w: 0.052 * h, d: 0.032 * h });
  }

  const hx = spec.hip * h;
  for (const side of ["L", "R"]) {
    const s = side === "L" ? 1 : -1;
    const thigh = bone(hips, `thigh${side}`, [s * hx, 0, 0]);
    const hpad = new THREE.Mesh(new THREE.SphereGeometry(0.048 * h * b, 14, 12), mat);
    hpad.castShadow = true;
    thigh.add(hpad);
    limb(thigh, mat, { len: P.thigh * h, w: 0.082 * h * b, d: 0.082 * h * b });
    const shin = bone(thigh, `shin${side}`, [0, -P.thigh * h, 0]);
    limb(shin, mat, { len: P.shin * h, w: 0.066 * h * b, d: 0.066 * h * b });
    const foot = bone(shin, `foot${side}`, [0, -P.shin * h, 0]);
    limb(foot, mat, { len: P.foot * h, w: 0.070 * h, d: 0.048 * h, dir: [0, 0, 1] });
  }

  const bones = {};
  root.traverse((o) => { if (BONES.includes(o.name)) bones[o.name] = o; });
  root.userData = { kind: "actor", model: modelKey, height: h, bones, material: mat };
  return root;
}

/** 道具体块（灰模同色系，稍暗，避免与角色抢注意力）。 */
export function buildProp(kind = "box", opts = {}) {
  const spec = PROPS[kind] || PROPS.box;
  const mat = new THREE.MeshStandardMaterial({ color: opts.color ?? 0x6f7787,
    roughness: 0.85, metalness: 0.04 });
  const root = new THREE.Group();
  root.name = "prop";
  const api = {
    box(w, hh, d, y = 0, x = 0, z = 0) {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, hh, d), mat);
      m.position.set(x, y + hh / 2 * (y === 0 ? 1 : 0), z);
      if (y !== 0) m.position.y = y;
      m.castShadow = true; m.receiveShadow = true;
      root.add(m); return m;
    },
    cyl(r, hh, y = 0, x = 0, z = 0) {
      const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r * 1.15, hh, 10), mat);
      m.position.set(x, y === 0 ? hh / 2 : y, z);
      m.castShadow = true; root.add(m); return m;
    },
    sphere(r, y = 0, x = 0, z = 0, scale = null) {
      const m = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 8), mat);
      m.position.set(x, y, z);
      if (scale) m.scale.set(scale[0], scale[1], scale[2]);
      m.castShadow = true; root.add(m); return m;
    },
  };
  spec.build(api);
  root.userData = { kind: "prop", prop: kind, size: spec.size };
  return root;
}

/* ------------------------------------------------------------------ 动作片段 */
/* 姿势表记法：`{骨骼: [swing, twist, spread]}`（**度**）
     swing  = 绕 X（**正 = 向前 +Z**，落表时取负送 rotation.x，见文件头约定）
     twist  = 绕 Y（正 = 向角色左侧转）
     spread = 绕 Z（正 = 向外张开）
   `hipsY` 是髋部相对站立高度的位移（米），做步态起伏；`hipsZ` 前后微移。 */

const STAND = {};

// 走：接触—经过—接触—经过（四相位一循环），手脚反向摆。
// 胸腔带反向扭转（chest twist 与迈出的腿相反）——没有它步态就是「机器人平移」，
// 有了它肩膀跟着步子微摆，这是真人步态最显眼的一条特征
const WALK = [
  { t: 0.00, p: { thighL: [26], shinL: [-8], footL: [8], thighR: [-22], shinR: [-24], footR: [16],
                  armL: [-26], forearmL: [22], armR: [26], forearmR: [22],
                  spine: [2], chest: [1, -6], head: [-1, 2] }, hipsY: -0.010 },
  { t: 0.25, p: { thighL: [4], shinL: [-16], footL: [4], thighR: [-4], shinR: [-6], footR: [2],
                  armL: [-8], forearmL: [18], armR: [8], forearmR: [18],
                  spine: [2], chest: [1, 0], head: [-1, 0] }, hipsY: 0.022 },
  { t: 0.50, p: { thighL: [-22], shinL: [-24], footL: [16], thighR: [26], shinR: [-8], footR: [8],
                  armL: [26], forearmL: [22], armR: [-26], forearmR: [22],
                  spine: [2], chest: [1, 6], head: [-1, -2] }, hipsY: -0.010 },
  { t: 0.75, p: { thighL: [-4], shinL: [-6], footL: [2], thighR: [4], shinR: [-16], footR: [4],
                  armL: [8], forearmL: [18], armR: [-8], forearmR: [18],
                  spine: [2], chest: [1, 0], head: [-1, 0] }, hipsY: 0.022 },
  { t: 1.00, p: null },   // null = 回到第一帧（闭环，避免手抄两遍抄歪）
];

// 跑：步幅更大、前倾、有腾空相（经过相双脚离地 → 髋部抬高更多）
// 摆臂肘屈约 80~90°：跑步的前臂恒在屈曲位，`forearm` 必须是正号（向前屈）
const RUN = [
  { t: 0.000, p: { thighL: [44], shinL: [-32], footL: [10], thighR: [-40], shinR: [-58], footR: [22],
                   armL: [-56], forearmL: [88], armR: [42], forearmR: [76],
                   spine: [12], chest: [4, -10], head: [-8, 3] }, hipsY: -0.026 },
  { t: 0.155, p: { thighL: [16], shinL: [-84], footL: [6], thighR: [-16], shinR: [-14], footR: [6],
                   armL: [-24], forearmL: [84], armR: [24], forearmR: [84],
                   spine: [12], chest: [4, 0], head: [-8, 0] }, hipsY: 0.048 },
  { t: 0.310, p: { thighL: [-40], shinL: [-58], footL: [22], thighR: [44], shinR: [-32], footR: [10],
                   armL: [42], forearmL: [76], armR: [-56], forearmR: [88],
                   spine: [12], chest: [4, 10], head: [-8, -3] }, hipsY: -0.026 },
  { t: 0.465, p: { thighL: [-16], shinL: [-14], footL: [6], thighR: [16], shinR: [-84], footR: [6],
                   armL: [24], forearmL: [84], armR: [-24], forearmR: [84],
                   spine: [12], chest: [4, 0], head: [-8, 0] }, hipsY: 0.048 },
  { t: 0.620, p: null },
];

// 待机：呼吸起伏 + 极轻的重心转移（不动的角色也不该是雕像）
const IDLE = [
  { t: 0.0, p: { spine: [1], chest: [1], armL: [0, 0, 4], armR: [0, 0, -4] }, hipsY: 0 },
  { t: 1.6, p: { spine: [3], chest: [3], armL: [2, 0, 6], armR: [2, 0, -6],
                 head: [-1] }, hipsY: 0.012 },
  { t: 3.2, p: null },
];

// 跳：下蹲蓄力 → 蹬伸腾空 → 收腿 → 落地缓冲
// 蓄力与落地缓冲都是**屈髋**（大腿向前、膝在脚前），故 `thigh` 取正
const JUMP = [
  { t: 0.00, p: STAND, hipsY: 0 },
  { t: 0.22, p: { thighL: [46], shinL: [-78], thighR: [46], shinR: [-78], footL: [30], footR: [30],
                  spine: [18], armL: [-44], forearmL: [26],
                  armR: [-44], forearmR: [26] }, hipsY: -0.13 },
  { t: 0.44, p: { thighL: [-8], shinL: [-4], thighR: [-8], shinR: [-4], footL: [-22], footR: [-22],
                  spine: [-4], armL: [128], forearmL: [10],
                  armR: [128], forearmR: [10] }, hipsY: 0.12 },
  { t: 0.66, p: { thighL: [50], shinL: [-84], thighR: [50], shinR: [-84],
                  spine: [8], armL: [78], forearmL: [30],
                  armR: [78], forearmR: [30] }, hipsY: 0.34 },
  { t: 0.88, p: { thighL: [36], shinL: [-62], thighR: [36], shinR: [-62], footL: [26], footR: [26],
                  spine: [16], armL: [-22], forearmL: [30],
                  armR: [-22], forearmR: [30] }, hipsY: -0.13 },
  { t: 1.10, p: STAND, hipsY: 0 },
];

// 爬：手膝四点支撑，对角手脚交替（低姿潜行/受伤/穿越低矮空间）
// 膝跪地 → 髋高＝大腿长；肩略高于髋，躯干因此是 14° 的上仰斜面。
// 大腿的 `swing` 要把 `hips` 的俯仰**减回去**才落得到地面：总角 = hips − thigh。
const CRAWL = [
  { t: 0.0, p: { hips: [76], spine: [2], chest: [2], head: [-58],
                 thighL: [88], shinL: [-102], footL: [-90],
                 thighR: [62], shinR: [-110], footR: [-90],
                 armL: [42, 0, 10], forearmL: [4], armR: [20, 0, -10], forearmR: [10] },
    hipsY: -0.46, hipsZ: 0.04 },
  { t: 0.8, p: { hips: [76], spine: [2], chest: [2], head: [-58],
                 thighL: [62], shinL: [-110], footL: [-90],
                 thighR: [88], shinR: [-102], footR: [-90],
                 armL: [20, 0, 10], forearmL: [10], armR: [42, 0, -10], forearmR: [4] },
    hipsY: -0.46, hipsZ: 0.04 },
  { t: 1.6, p: null },
];

// 趴：俯卧贴地，只有呼吸起伏。`hips` 转平躯干后腿已经跟着躺下，
// `thigh` 再给角度就是把同一次旋转叠两遍——故双腿留 0。
const PRONE = [
  { t: 0.0, p: { hips: [90], spine: [2], chest: [4], head: [-72],
                 footL: [75], footR: [75],
                 armL: [6, 0, 35], forearmL: [18], armR: [6, 0, -35], forearmR: [18] },
    hipsY: -0.783 },
  { t: 2.0, p: { hips: [90], spine: [4], chest: [6], head: [-74],
                 footL: [75], footR: [75],
                 armL: [8, 0, 35], forearmL: [16], armR: [8, 0, -35], forearmR: [16] },
    hipsY: -0.763 },
  { t: 4.0, p: null },
];

// 飞：身体近水平，双臂后掠贴身，双腿并拢后拖（御剑/浮空/超能力）
// 躯干转平后肩轴的「下方」已经指向身后，故双臂 `swing` 接近 0 就是后掠。
const FLY = [
  { t: 0.0, p: { hips: [82], spine: [4], chest: [4], head: [-60],
                 armL: [-6, 0, 22], forearmL: [14], armR: [-6, 0, -22], forearmR: [14],
                 thighL: [-10], thighR: [-16], shinL: [-6], shinR: [-12] }, hipsY: 0.55 },
  { t: 1.2, p: { hips: [84], spine: [4], chest: [4], head: [-62],
                 armL: [-10, 0, 18], forearmL: [10], armR: [-10, 0, -18], forearmR: [10],
                 thighL: [-16], thighR: [-10], shinL: [-12], shinR: [-6] }, hipsY: 0.63 },
  { t: 2.4, p: null },
];

// 坐：大腿前伸、小腿下垂（对坐戏/餐桌/办公）
const SIT = [
  { t: 0.0, p: { thighL: [86], shinL: [-88], footL: [10], thighR: [86], shinR: [-88], footR: [10],
                 spine: [4], armL: [-16, 0, 8], forearmL: [40],
                 armR: [-16, 0, -8], forearmR: [40] }, hipsY: -0.34, hipsZ: -0.06 },
  { t: 2.0, p: { thighL: [86], shinL: [-88], footL: [10], thighR: [86], shinR: [-88], footR: [10],
                 spine: [6], head: [-2], armL: [-14, 0, 8], forearmL: [42],
                 armR: [-14, 0, -8], forearmR: [42] }, hipsY: -0.33, hipsZ: -0.06 },
  { t: 4.0, p: null },
];

// 转身：原地 180°（最常见的「回头」表演）——转的是髋部 Y 轴，头先行、身体跟上
const TURN = [
  { t: 0.0, p: { hips: [0, 0], head: [0, 0] } },
  { t: 0.3, p: { hips: [0, 12], chest: [0, 16], head: [0, 46] } },
  { t: 0.9, p: { hips: [0, 150], chest: [0, 22], head: [0, 30] } },
  { t: 1.2, p: { hips: [0, 180], chest: [0, 0], head: [0, 0] } },
];

// 挥手：抬右臂 + 前臂左右摆两次
const WAVE = [
  { t: 0.0, p: STAND },
  { t: 0.35, p: { armR: [0, 0, -128], forearmR: [28, 0, -20], chest: [0, -6] } },
  { t: 0.70, p: { armR: [0, 0, -134], forearmR: [24, 0, 22], chest: [0, -6], head: [0, -8] } },
  { t: 1.05, p: { armR: [0, 0, -134], forearmR: [24, 0, -22], chest: [0, -6], head: [0, -8] } },
  { t: 1.60, p: STAND },
];

// 倒下：失衡后仰 → 髋部落地 → 完全躺平（受击/力竭）
// 向后倒 = 上身向后 → `hips`/`spine` 取负；躺平后腿沿 +Z 伸直，故 `thigh` 归零
const FALL = [
  { t: 0.0, p: STAND, hipsY: 0 },
  { t: 0.35, p: { hips: [-20], spine: [-8], head: [-12], armL: [-70], forearmL: [24],
                  armR: [-70], forearmR: [24], thighL: [16], thighR: [14],
                  shinL: [-20], shinR: [-18] }, hipsY: -0.10 },
  { t: 0.85, p: { hips: [-58], spine: [-12], head: [-16], armL: [-96, 0, 34], forearmL: [30],
                  armR: [-96, 0, -34], forearmR: [30], thighL: [34], shinL: [-52],
                  thighR: [30], shinR: [-48] }, hipsY: -0.50 },
  { t: 1.40, p: { hips: [-90], spine: [-4], head: [-4], armL: [4, 0, 70], forearmL: [10],
                  armR: [4, 0, -70], forearmR: [10], thighL: [0], shinL: [-8],
                  thighR: [0], shinR: [-6], footL: [-70], footR: [-70] }, hipsY: -0.77 },
];

// 出招：拧腰蓄力 → 上身发力挥击 → 收招（战斗节拍）
const ATTACK = [
  { t: 0.0, p: STAND },
  { t: 0.28, p: { hips: [0, 26], chest: [0, 22], head: [0, -10],
                  armR: [-64, 0, -34], forearmR: [96], armL: [30, 0, 16],
                  forearmL: [40] } },
  { t: 0.50, p: { hips: [0, -30], chest: [0, -28], head: [0, 6], spine: [10],
                  armR: [96, 0, -8], forearmR: [10], armL: [-46, 0, 22],
                  forearmL: [30] } },
  { t: 1.00, p: STAND },
];

// 蹲行：屈膝低姿的交替步（掩体间转移/潜近）——上身前倾稳定，步幅小
const CROUCH = [
  { t: 0.00, p: { spine: [22], chest: [8], head: [-16],
                  thighL: [78], shinL: [-74], footL: [12], thighR: [20], shinR: [-100], footR: [10],
                  armL: [-20], forearmL: [34], armR: [28], forearmR: [30] }, hipsY: -0.20 },
  { t: 0.28, p: { spine: [22], chest: [8], head: [-16],
                  thighL: [46], shinL: [-90], footL: [36], thighR: [46], shinR: [-90], footR: [36],
                  armL: [4], forearmL: [30], armR: [4], forearmR: [30] }, hipsY: -0.18 },
  { t: 0.55, p: { spine: [22], chest: [8], head: [-16],
                  thighL: [20], shinL: [-100], footL: [10], thighR: [78], shinR: [-74], footR: [12],
                  armL: [28], forearmL: [30], armR: [-20], forearmR: [34] }, hipsY: -0.20 },
  { t: 0.83, p: { spine: [22], chest: [8], head: [-16],
                  thighL: [46], shinL: [-90], footL: [36], thighR: [46], shinR: [-90], footR: [36],
                  armL: [4], forearmL: [30], armR: [4], forearmR: [30] }, hipsY: -0.18 },
  { t: 1.10, p: null },
];

// 闪避：向侧方急闪压低重心再回正（躲攻击/躲障碍物）
const DODGE = [
  { t: 0.00, p: STAND, hipsY: 0 },
  { t: 0.18, p: { hips: [0, 0, 14], spine: [12, 0, 10], head: [0, 0, -8],
                  thighL: [38, 0, 18], shinL: [-52], footL: [14],
                  thighR: [8, 0, -12], shinR: [-34], footR: [26],
                  armL: [-40, 0, 24], forearmL: [34], armR: [48, 0, -18], forearmR: [44] },
    hipsY: -0.08 },
  { t: 0.45, p: { hips: [0, 0, 22], spine: [16, 0, 14], head: [0, 0, -12],
                  thighL: [58, 0, 26], shinL: [-78], footL: [20],
                  thighR: [40, 0, -16], shinR: [-85], footR: [45],
                  armL: [-52, 0, 30], forearmL: [40], armR: [58, 0, -22], forearmR: [52] },
    hipsY: -0.20 },
  { t: 0.90, p: STAND, hipsY: 0 },
];

// 进掩体：深蹲贴壁隐蔽，中段探头张望（配「掩体」道具）
const COVER = [
  { t: 0.0, p: { spine: [26], chest: [10], head: [-18],
                 thighL: [98], shinL: [-106], footL: [16], thighR: [98], shinR: [-106], footR: [16],
                 armL: [-52, 0, 10], forearmL: [78], armR: [-52, 0, -10], forearmR: [78] },
    hipsY: -0.50 },
  { t: 1.5, p: { spine: [22], chest: [8], head: [-8, 26],
                 thighL: [98], shinL: [-106], footL: [16], thighR: [98], shinR: [-106], footR: [16],
                 armL: [-52, 0, 10], forearmL: [78], armR: [-52, 0, -10], forearmR: [78] },
    hipsY: -0.48 },
  { t: 3.0, p: null },
];

// 上车：抬腿跨入 → 弯身 → 坐落（clampWhenFinished 保持坐姿收尾）
const ENTER = [
  { t: 0.0, p: STAND, hipsY: 0 },
  { t: 0.4, p: { spine: [14], head: [-8], thighL: [64], shinL: [-64], footL: [10],
                 armL: [-36], forearmL: [30], armR: [22], forearmR: [26] }, hipsY: -0.03 },
  { t: 0.9, p: { spine: [26], head: [-12], thighL: [78], shinL: [-76], footL: [10],
                 thighR: [30], shinR: [-56], footR: [26], armL: [-48], forearmL: [36],
                 armR: [30], forearmR: [26] },
    hipsY: -0.14, hipsZ: 0.14 },
  { t: 1.6, p: { spine: [8], head: [-2], thighL: [86], shinL: [-88], footL: [10],
                 thighR: [86], shinR: [-88], footR: [10],
                 armL: [-18, 0, 6], forearmL: [42], armR: [-18, 0, -6], forearmR: [42] },
    hipsY: -0.34, hipsZ: 0.10 },
];

// 骑乘：分腿骑姿、髋部抬到鞍高、双手前伸握缰，轻微颠簸（叠放在马/车体上）
const RIDE = [
  { t: 0.0, p: { thighL: [76, 0, 28], shinL: [-76], footL: [8], thighR: [76, 0, -28], shinR: [-76], footR: [8],
                 spine: [10], head: [-4], armL: [-44, 0, 8], forearmL: [72], armR: [-44, 0, -8], forearmR: [72] },
    hipsY: 0.24 },
  { t: 1.0, p: { thighL: [76, 0, 28], shinL: [-76], footL: [8], thighR: [76, 0, -28], shinR: [-76], footR: [8],
                 spine: [13], head: [-7], armL: [-46, 0, 8], forearmL: [70], armR: [-46, 0, -8], forearmR: [70] },
    hipsY: 0.28 },
  { t: 2.0, p: null },
];

const POSE_TABLES = {
  idle: IDLE, walk: WALK, run: RUN, jump: JUMP, crawl: CRAWL, prone: PRONE,
  fly: FLY, sit: SIT, turn: TURN, wave: WAVE, fall: FALL, attack: ATTACK,
  crouch: CROUCH, dodge: DODGE, cover: COVER, enter: ENTER, ride: RIDE,
};

/* 远端指向 +Y 的骨骼（躯干链）。`R_x(θ)` 对 (0,+1,0) 与对 (0,−1,0) 的作用方向相反，
   故这一组的 swing 送进 rotation.x 时不取负——否则「上身前倾」写出来是后仰。 */
const AXIAL = new Set(["hips", "spine", "chest", "neck", "head"]);

function quatOf(bone, [swing = 0, twist = 0, spread = 0]) {
  const rx = AXIAL.has(bone) ? swing : -swing;
  return new THREE.Quaternion().setFromEuler(
    new THREE.Euler(rx * D, twist * D, spread * D, "XYZ"));
}

/* ---------------------------------------------------------------- 姿势曲线加密 */
/* 姿势表记的是**极值姿势**（走是四相位、跳是五拍），而 three 的
   `QuaternionKeyframeTrack` 只提供线性插值——它没有平滑插值实现
   （`InterpolantFactoryMethodSmooth` 在该轨类型上是 undefined）。两个极值之间走
   直线，关节就是匀速转到底再折返，角速度在每个关键帧处突变。

   故在编译成四元数之前，先在**欧拉分量这一层**按关键帧时间做一次加密：非均匀
   Catmull-Rom（有限差分切线），使关节角速度在关键帧处连续。姿势表的书写方式不变。 */

// 加密目标频率取导出帧率：低于它的采样在逐帧导出时仍会被看成折线。
// 上限用于抑制长时保持段（趴下、进掩体）产生无意义的密集轨。
const CURVE_HZ = 24, SUB_MIN = 2, SUB_MAX = 24;

/**
 * 通道在归一化区间 `[i, i+1]` 的 `s` 处取值。
 *
 * `wrap` 用于循环动作：末关键帧与首关键帧是同一姿势，端点切线跨接缝求，
 * 循环接缝因此不是折角。一次性动作端点按夹取处理（起止两拍没有前后文）。
 *
 * 取值一律夹在相邻两个关键帧的区间内：样条的自然过冲会把作者写定的极值再推出去
 * 一截，等同于替作者改表演；髋部高度通道上过冲还会让脚穿过地面。
 */
function curveAt(vals, times, i, s, wrap) {
  const n = vals.length;
  const period = times[n - 1] - times[0];
  const at = (arr, j, offLo, offHi) => {
    if (j >= 0 && j < n) return arr[j];
    if (!wrap) return arr[j < 0 ? 0 : n - 1];
    return j < 0 ? arr[n - 2] + offLo : arr[1] + offHi;
  };
  const t0 = at(times, i - 1, -period, 0), t1 = times[i];
  const t2 = times[i + 1], t3 = at(times, i + 2, 0, period);
  const p0 = at(vals, i - 1, 0, 0), p1 = vals[i];
  const p2 = vals[i + 1], p3 = at(vals, i + 2, 0, 0);
  const h = t2 - t1;
  const m1 = t2 - t0 > 1e-9 ? (p2 - p0) / (t2 - t0) : 0;
  const m2 = t3 - t1 > 1e-9 ? (p3 - p1) / (t3 - t1) : 0;
  const s2 = s * s, s3 = s2 * s;
  const v = (2 * s3 - 3 * s2 + 1) * p1 + (s3 - 2 * s2 + s) * h * m1
    + (-2 * s3 + 3 * s2) * p2 + (s3 - s2) * h * m2;
  return Math.min(Math.max(v, Math.min(p1, p2)), Math.max(p1, p2));
}

/** 加密后的采样点列表 `[{t, i, s}]`——每个原关键帧都原样保留在其中。 */
function denseSamples(times) {
  const out = [];
  for (let i = 0; i < times.length - 1; i++) {
    const span = times[i + 1] - times[i];
    const sub = Math.min(SUB_MAX, Math.max(SUB_MIN, Math.ceil(span * CURVE_HZ)));
    for (let k = 0; k < sub; k++) {
      const s = k / sub;
      out.push({ t: times[i] + span * s, i, s });
    }
  }
  const last = times.length - 2;
  out.push({ t: times[times.length - 1], i: last, s: 1 });
  return out;
}

/**
 * 把姿势表编译成 `THREE.AnimationClip`。
 *
 * 每根骨骼一条 `QuaternionKeyframeTrack`（未在任何关键帧出现的骨骼**完全不建轨**，
 * 省得给 17 根骨骼都塞一条恒等曲线）；髋部另有一条 `VectorKeyframeTrack` 做起伏。
 * track 名 `骨骼名.quaternion` 由 `PropertyBinding` 按**节点名**在 root 子树里解析，
 * 因此这套 clip 对任何用 `buildMannequin` 造出来的 actor 都通用。
 *
 * 关键帧在建轨前先经 `denseSamples` + `curveAt` 加密，理由见上一节。
 */
export function buildClip(actionKey, hipRestY) {
  const table = POSE_TABLES[actionKey] || IDLE;
  const meta = ACTIONS[actionKey] || ACTIONS.idle;
  const keys = table.map((k) => (k.p === null ? { ...table[0], t: k.t } : k));
  const used = new Set();
  keys.forEach((k) => Object.keys(k.p || {}).forEach((b) => used.add(b)));

  const times = keys.map((k) => k.t);
  const wrap = !!meta.loop;
  const samples = denseSamples(times);
  const denseTimes = samples.map((p) => p.t);
  // 通道 = 一根骨骼的一个欧拉分量。稀疏表里没写到的骨骼按 0（站立姿）计，
  // 与 `quatOf` 对缺省分量的处理一致。
  const sampled = (pick) => {
    const vals = keys.map(pick);
    return samples.map((p) => curveAt(vals, times, p.i, p.s, wrap));
  };

  const tracks = [];
  for (const b of used) {
    const axes = [0, 1, 2].map(
      (c) => sampled((k) => ((k.p || {})[b] || [])[c] || 0));
    const vals = [];
    for (let j = 0; j < samples.length; j++) {
      const q = quatOf(b, [axes[0][j], axes[1][j], axes[2][j]]);
      vals.push(q.x, q.y, q.z, q.w);
    }
    tracks.push(new THREE.QuaternionKeyframeTrack(`${b}.quaternion`, denseTimes, vals));
  }
  if (keys.some((k) => k.hipsY || k.hipsZ)) {
    const ys = sampled((k) => k.hipsY || 0);
    const zs = sampled((k) => k.hipsZ || 0);
    const pos = [];
    for (let j = 0; j < samples.length; j++) pos.push(0, hipRestY + ys[j], zs[j]);
    tracks.push(new THREE.VectorKeyframeTrack("hips.position", denseTimes, pos));
  }
  return new THREE.AnimationClip(actionKey, meta.dur, tracks);
}

/** 该模型的髋部静止高度（米）——姿势表的 `hipsY` 是相对它的位移。 */
export function hipRestY(modelKey) {
  return P.hipY * (MODELS[modelKey] || MODELS.mannequin_n).height;
}

export const RIG_INFO = { bones: BONES, proportions: P };
