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
   走位路线的可视与交互件 —— 路管 / 数字路点针 / 幽灵落点 / 橡皮线 / 选中圈

   这些全是**编辑器辅助物**：由 stage.js 挂进 `pathLines` / `aids`，导出洁净模式
   （setExportMode）一开即整体隐藏——previz 会作为 reference_video 直接喂给
   Seedance，画面里多一根琥珀路管，模型就会试着把它复现成场景内容。

   曲线口径与 `Actor.setPath` 完全一致（CatmullRom · tension 0.35 · 不闭合）：
   画出来的管子就是人真正会走的那条线——差半米，「previz 对照」就失去意义。

   为什么是「管 + 针」而不是 1px 线 + 小圆点：
     · `THREE.Line` 恒为 1px，俯视角下贴在网格上几乎不可见——用户点了三个点，
       画面里什么都没出现，只能得出「点了没反应」的结论；
     · TubeGeometry 有实际直径、受光照、有方向锥，任何角度都读得出「往哪走」；
     · 路点立成带序号的针（杆 + 头 + 序号贴片），从上帝视角一眼数得出第几点，
       还配一颗放大的隐形拾取球——针本体太细，点不准是必然的。
   ========================================================================== */

import * as THREE from "three";

const TENSION = 0.35;   // 必须与 actors.js Actor.setPath 的 CatmullRom 张力一致

export function pathCurve(points) {
  return new THREE.CatmullRomCurve3(
    points.map((p) => new THREE.Vector3(...p)), false, "catmullrom", TENSION);
}

/* 路点序号贴片：画布画数字 → Sprite。按「数字|主题色」缓存，重复渲染零开销。
   琥珀=角色走位，青色=机位轨道——与全站的对象语义色一致。 */
const NUM_TEX = new Map();
const NUM_THEME = {
  amber: { ring: "#f0a63c", text: "#f6d9a8" },
  cyan: { ring: "#4cc3d9", text: "#d9f3f8" },
};
function numTexture(n, theme = "amber") {
  const key = `${n}|${theme}`;
  if (NUM_TEX.has(key)) return NUM_TEX.get(key);
  const clr = NUM_THEME[theme] || NUM_THEME.amber;
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  g.beginPath();
  g.arc(32, 32, 26, 0, Math.PI * 2);
  g.fillStyle = "rgba(14, 16, 20, .92)";
  g.fill();
  g.lineWidth = 4;
  g.strokeStyle = clr.ring;
  g.stroke();
  g.fillStyle = clr.text;
  g.font = "600 30px ui-monospace, monospace";
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText(String(n), 32, 34);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  NUM_TEX.set(key, t);
  return t;
}

/**
 * 一条走位路线的完整可视：路管 + 方向锥 + 起点站位圈 + 数字路点针。
 * `selected=false` 时只画细路管与起点圈——非当前角色的路线降为背景信息。
 *
 * 返回 group 带 `userData.pins`：各针的**隐形拾取球**（`userData={actorId,index}`），
 * stage.js 用它做「拖针改线」的射线命中。起点（index 0）**没有针**——它被钉死在
 * 角色脚下（路线起点=当前站位是防瞬移的铁律），给针等于诱导用户拖一个不可拖的点。
 */
export function buildPathViz(points, { color, selected = false, actorId = null } = {}) {
  const g = new THREE.Group();
  g.name = "pathviz";
  g.userData.pins = [];
  if (!points || !points.length) return g;

  const mat = new THREE.MeshBasicMaterial({
    color, transparent: true, opacity: selected ? 0.95 : 0.4, depthWrite: false });

  if (points.length >= 2) {
    const curve = pathCurve(points);
    const len = curve.getLength();
    const tube = new THREE.Mesh(
      new THREE.TubeGeometry(curve, Math.max(32, points.length * 14),
        selected ? 0.042 : 0.022, 8, false), mat);
    tube.position.y = 0.015;
    g.add(tube);
    if (selected) {
      // 方向锥：沿路管每 ~1.6m 一枚——任何角度都读得出「往哪走」
      const n = Math.max(1, Math.floor(len / 1.6));
      for (let i = 1; i <= n; i++) {
        const u = i / (n + 1);
        const p = curve.getPointAt(u);
        const cone = new THREE.Mesh(new THREE.ConeGeometry(0.085, 0.24, 10), mat);
        cone.position.copy(p).setY(p.y + 0.015);
        cone.quaternion.setFromUnitVectors(
          new THREE.Vector3(0, 1, 0), curve.getTangentAt(u).normalize());
        g.add(cone);
      }
      // 终点箭头加大一号——终点是一条走位最重要的点
      const pe = curve.getPointAt(1);
      const end = new THREE.Mesh(new THREE.ConeGeometry(0.13, 0.34, 10), mat);
      end.position.copy(pe).setY(pe.y + 0.02);
      end.quaternion.setFromUnitVectors(
        new THREE.Vector3(0, 1, 0), curve.getTangentAt(1).normalize());
      g.add(end);
    }
  }

  // 起点：站位圈（非针，不可拖——见函数头纪律）
  const start = new THREE.Mesh(new THREE.RingGeometry(0.16, 0.23, 32), mat);
  start.rotation.x = -Math.PI / 2;
  start.position.set(points[0][0], 0.012, points[0][2]);
  g.add(start);

  if (selected) {
    const pinMat = new THREE.MeshBasicMaterial({ color });
    for (let i = 1; i < points.length; i++) {
      const [x, , z] = points[i];
      const pin = new THREE.Group();
      pin.position.set(x, 0, z);
      const pole = new THREE.Mesh(
        new THREE.CylinderGeometry(0.016, 0.016, 0.36, 8), pinMat);
      pole.position.y = 0.18;
      const head = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 10), pinMat);
      head.position.y = 0.38;
      // 隐形拾取球：针本体太细点不准，命中判定放大到 0.2m
      const hit = new THREE.Mesh(new THREE.SphereGeometry(0.2, 8, 6),
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
      hit.position.y = 0.3;
      hit.userData = { actorId, index: i };
      const tag = new THREE.Sprite(new THREE.SpriteMaterial({
        map: numTexture(i), transparent: true, depthTest: false }));
      tag.scale.setScalar(0.26);
      tag.position.y = 0.62;
      pin.add(pole, head, hit, tag);
      g.add(pin);
      g.userData.pins.push(hit);
    }
  }
  return g;
}

/** 释放一条路线可视的几何与材质（序号贴图有全局缓存，刻意不释放）。 */
export function disposeViz(g) {
  g.traverse((o) => {
    o.geometry?.dispose?.();
    if (o.material && !o.material.map) o.material.dispose?.();
  });
}

/** 幽灵落点：落位 / 画线时跟随鼠标的地面光标（圈 + 心点）——点下去之前就看得见
 *  会落在哪，消除「点了却无确认」的不确定。 */
export function buildGhost(color) {
  const g = new THREE.Group();
  g.name = "ghost";
  const mat = new THREE.MeshBasicMaterial({
    color, transparent: true, opacity: 0.8, depthWrite: false });
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.15, 0.21, 32), mat);
  ring.rotation.x = -Math.PI / 2;
  const dot = new THREE.Mesh(new THREE.CircleGeometry(0.045, 16), mat);
  dot.rotation.x = -Math.PI / 2;
  dot.position.y = 0.002;
  g.add(ring, dot);
  g.visible = false;
  return g;
}

/** 橡皮线：画线时从上一个路点拉到鼠标的虚线预览。`userData.update(from, to)` 改端点。 */
export function buildRubber(color) {
  const geo = new THREE.BufferGeometry().setFromPoints(
    [new THREE.Vector3(), new THREE.Vector3()]);
  const line = new THREE.Line(geo, new THREE.LineDashedMaterial({
    color, dashSize: 0.22, gapSize: 0.13, transparent: true, opacity: 0.85 }));
  line.name = "rubber";
  line.frustumCulled = false;
  line.visible = false;
  line.userData.update = (a, b) => {
    geo.setFromPoints([new THREE.Vector3(a[0], 0.03, a[2]),
                       new THREE.Vector3(b.x, 0.03, b.z)]);
    line.computeLineDistances();
  };
  return line;
}

/** 机位身体：小机身 + 朝向锥（锥口朝 -Z 视轴）——当前镜头块的相机在三维里的实体。
 *  拾取面=**可见几何本身**（机身盒+镜头锥，经父级 `userData.pickCamera` 归类），
 *  刻意不配放大的隐形拾取球——近景下大球会比可见图元大出一整圈，「在旁边
 *  空白处点击拖动也能把机位拖走」，触发范围过广。远看难抓的
 *  问题交给视锥漏斗弱拾取面（见 buildCamFrustum），不靠大球。 */
export function buildCamBody(color) {
  const g = new THREE.Group();
  g.name = "cambody";
  const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.92 });
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.24, 0.18, 0.34), mat);
  const lens = new THREE.Mesh(new THREE.ConeGeometry(0.11, 0.2, 4), mat);
  lens.rotation.x = Math.PI / 2;       // 锥口（底面）朝 -Z——three 相机的视轴方向
  lens.rotation.y = Math.PI / 4;
  lens.position.z = -0.28;
  g.add(body, lens);
  g.userData.pickCamera = true;
  return g;
}

/**
 * 机位轨道路点针：悬空青珠 + **↕ 升降手柄** + 垂地细线与地面圆点（悬空的点没有
 * 参照物，高度全靠这两样读出来）+ 序号贴片——**每一颗都可拖**（含起点：机位
 * 不像角色被钉在站位上）。与走位针同一交互语言。
 *
 * 两个拾取面（`userData.pins` / `userData.lifts`，均为放大的隐形拾取球）：
 *   · 珠子 `{camPin, index}` —— 水平面拖拽（改 x/z）；
 *   · ↕ 手柄 `{camPinLift, index}` —— **垂直约束拖拽**（改高度）。DCC 的移动
 *     操纵器都靠「看得见的轴向手柄」承载第二自由度——只藏在 ⇧ 修饰键里，
 *     垂直自由度在界面上就没有任何可见痕迹（实测被点名：迈克尔·贝式
 *     低走高的仰拍环绕，没有垂直拖就排不出来）。
 * preset 程序轨道下这五颗针就是「拖一下即烘焙」的落点预览——针在哪，烘焙出的
 * 自定义轨道路点就在哪（视觉即数据，见 stage.dragCamPinTo）。
 */
export function buildCamPins(points, color) {
  const g = new THREE.Group();
  g.name = "campins";
  g.userData.pins = [];
  g.userData.lifts = [];
  const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.95 });
  const liftMat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.78 });
  const dropMat = new THREE.MeshBasicMaterial({
    color, transparent: true, opacity: 0.26, depthWrite: false });
  points.forEach((p, i) => {
    const [x, y, z] = p;
    const pin = new THREE.Group();
    pin.position.set(x, y, z);
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.075, 12, 10), mat);
    // ↕ 升降手柄：头顶的小双锥菱形（上下箭头的三维化）——垂直自由度的可见入口
    const lift = new THREE.Group();
    lift.position.y = 0.21;
    const upCone = new THREE.Mesh(new THREE.ConeGeometry(0.042, 0.085, 8), liftMat);
    upCone.position.y = 0.052;
    const dnCone = new THREE.Mesh(new THREE.ConeGeometry(0.042, 0.085, 8), liftMat);
    dnCone.rotation.x = Math.PI;
    dnCone.position.y = -0.052;
    const stem = new THREE.Mesh(new THREE.CylinderGeometry(0.009, 0.009, 0.1, 6), liftMat);
    lift.add(upCone, dnCone, stem);
    const liftHit = new THREE.Mesh(new THREE.SphereGeometry(0.15, 8, 6),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
    liftHit.position.y = 0.21;
    liftHit.userData = { camPinLift: true, index: i };
    // 垂地细线 + 地面圆点：连续的高度参照（拖高了一眼读得出离地几米）
    const drop = new THREE.Mesh(
      new THREE.CylinderGeometry(0.007, 0.007, Math.max(0.02, y), 6), dropMat);
    drop.position.y = -Math.max(0.02, y) / 2;
    const foot = new THREE.Mesh(new THREE.CircleGeometry(0.045, 12), dropMat);
    foot.rotation.x = -Math.PI / 2;
    foot.position.y = -y + 0.012;
    // 隐形拾取球：珠子太小点不准，命中判定放大到 0.24m
    const hit = new THREE.Mesh(new THREE.SphereGeometry(0.24, 8, 6),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
    hit.userData = { camPin: true, index: i };
    const tag = new THREE.Sprite(new THREE.SpriteMaterial({
      map: numTexture(i + 1, "cyan"), transparent: true, depthTest: false }));
    tag.scale.setScalar(0.2);
    tag.position.y = 0.5;
    pin.add(head, lift, liftHit, drop, foot, hit, tag);
    g.add(pin);
    g.userData.pins.push(hit);
    g.userData.lifts.push(liftHit);
  });
  return g;
}

/**
 * 机位视锥线框：从机身沿视轴张开的 4 棱 + 远端画幅矩形——「这是一台相机、
 * 它拍到多宽」在三维里直接看得见（所有 DCC 的相机实体都有这件）。
 * 单位几何（远端角点在 z=-1 的 ±1），stage 每帧按 `tan(fov/2)` 缩放——
 * 焦距一变视锥就张合，dolly-zoom 的「机身后退视角变广」肉眼可见。
 */
export function buildCamFrustum(color) {
  const c = [[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1]];
  const pts = [];
  for (const k of c) pts.push(0, 0, 0, ...k);
  for (let i = 0; i < 4; i++) pts.push(...c[i], ...c[(i + 1) % 4]);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  const line = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
    color, transparent: true, opacity: 0.42, depthWrite: false }));
  // 线框绝不参与拾取：Raycaster 对 Line 的命中阈值默认 1 **米**——留着它，
  // 距视锥棱线一米内的点击全会被判成命中，比隐形大球还失控
  line.raycast = () => {};
  // 漏斗拾取面：不可见的实心四棱锥+远端盖，正是「镜头前面的漏斗形区域」——
  // 机身图元太小、远看难抓，用视锥体积当第二拾取面形状精确又不越界。
  // 标记 `pickFrustum`（弱命中）：漏斗常正对主体，强命中会让「点主体」
  // 隔着漏斗误选机位——stage.pickObject 只在没点到任何实体时才认它。
  const vol = new THREE.BufferGeometry();
  vol.setAttribute("position", new THREE.Float32BufferAttribute([
    0, 0, 0, -1, -1, -1, 1, -1, -1,
    0, 0, 0, 1, -1, -1, 1, 1, -1,
    0, 0, 0, 1, 1, -1, -1, 1, -1,
    0, 0, 0, -1, 1, -1, -1, -1, -1,
    -1, -1, -1, 1, -1, -1, 1, 1, -1,
    -1, -1, -1, 1, 1, -1, -1, 1, -1,
  ], 3));
  const volMesh = new THREE.Mesh(vol, new THREE.MeshBasicMaterial({
    transparent: true, opacity: 0, depthWrite: false, side: THREE.DoubleSide }));
  volMesh.userData.pickFrustum = true;
  const g = new THREE.Group();
  g.name = "camfrustum";
  g.add(line, volMesh);
  return g;
}

/** 机位运动轨迹：本镜头块内相机真实走过的世界路径。
 *  点序由 stage 逐帧采样 rig 而来——preset 是主体相对坐标，主体走位时轨迹随之弯曲，
 *  这里画的就是最终会发生的那条运动。
 *
 *  视觉语言（对标 Unreal Sequencer 的运动轨迹）：
 *  · **暗→亮的时间渐变**（顶点色）——起点沉、终点亮，方向与时序一眼可读；
 *  · 选中时叠一层加色辉光管 + 稀疏方向箭 + **地面投影虚线段**（轨道悬空时
 *    在地上留一条影子线，高度与横向走向分开读）；
 *  · 非选中降为细暗管——背景信息不抢戏。
 */
export function buildCamTraj(points, color, { selected = false } = {}) {
  const g = new THREE.Group();
  g.name = "camtraj";
  if (!points || points.length < 2) return g;
  const curve = new THREE.CatmullRomCurve3(points, false, "catmullrom", 0);
  const segs = Math.max(48, points.length);
  const base = new THREE.Color(color);
  const dim = base.clone().multiplyScalar(0.32);

  const gradTube = (radius, opacity, blending) => {
    const geo = new THREE.TubeGeometry(curve, segs, radius, 6, false);
    const ring = 6 + 1;
    const cnt = geo.attributes.position.count;
    const cols = new Float32Array(cnt * 3);
    const c = new THREE.Color();
    for (let i = 0; i < cnt; i++) {
      const t = Math.min(1, Math.floor(i / ring) / segs);
      c.copy(dim).lerp(base, 0.25 + 0.75 * t);
      cols[i * 3] = c.r; cols[i * 3 + 1] = c.g; cols[i * 3 + 2] = c.b;
    }
    geo.setAttribute("color", new THREE.BufferAttribute(cols, 3));
    return new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      vertexColors: true, transparent: true, opacity,
      depthWrite: false, blending: blending || THREE.NormalBlending }));
  };
  g.add(gradTube(selected ? 0.026 : 0.018, selected ? 0.95 : 0.55));
  if (selected) {
    g.add(gradTube(0.085, 0.1, THREE.AdditiveBlending));   // 辉光包层
    // 稀疏方向箭（起末段留白：起点珠与终点箭头已各占一端）
    const len = curve.getLength();
    const mat = new THREE.MeshBasicMaterial({
      color, transparent: true, opacity: 0.55, depthWrite: false });
    const n = Math.min(5, Math.max(1, Math.floor(len / 2.2)));
    for (let i = 1; i <= n; i++) {
      const u = 0.12 + (i / (n + 1)) * 0.76;
      const cone = new THREE.Mesh(new THREE.ConeGeometry(0.05, 0.15, 8), mat);
      cone.position.copy(curve.getPointAt(u));
      cone.quaternion.setFromUnitVectors(
        new THREE.Vector3(0, 1, 0), curve.getTangentAt(u).normalize());
      g.add(cone);
    }
    // 地面投影虚线：轨道的「影子」——读横向走向，高度交给路点针的垂地线
    const shadowPts = [];
    for (let i = 0; i <= segs; i++) {
      const p = curve.getPointAt(i / segs);
      shadowPts.push(new THREE.Vector3(p.x, 0.014, p.z));
    }
    const shadow = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(shadowPts),
      new THREE.LineDashedMaterial({ color, dashSize: 0.16, gapSize: 0.12,
        transparent: true, opacity: 0.28, depthWrite: false }));
    shadow.computeLineDistances();
    g.add(shadow);
  }
  const capMat = new THREE.MeshBasicMaterial({
    color, transparent: true, opacity: selected ? 0.95 : 0.6, depthWrite: false });
  const start = new THREE.Mesh(new THREE.SphereGeometry(0.05, 10, 8), capMat);
  start.position.copy(points[0]);
  const pe = points[points.length - 1];
  const tan = pe.clone().sub(points[points.length - 2]);
  const end = new THREE.Mesh(new THREE.ConeGeometry(0.07, 0.18, 8), capMat);
  end.position.copy(pe);
  if (tan.lengthSq() > 1e-8) {
    end.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), tan.normalize());
  }
  g.add(start, end);
  return g;
}

/** 选中 / 悬停圈：半径 1 的扁环，stage 用 scale 适配对象尺寸、逐帧贴到脚下。 */
export function ringMesh(inner, color, opacity) {
  const m = new THREE.Mesh(
    new THREE.RingGeometry(inner, 1, 48),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthWrite: false }));
  m.rotation.x = -Math.PI / 2;
  m.visible = false;
  return m;
}
