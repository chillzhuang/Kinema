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
   资产缩略图 —— 动作 / 体型 / 道具的离屏渲染

   **与舞台同源**：姿势走 `Actor.update(t, dur)`，与视口预览、逐帧导出是同一个
   函数；布光取 `rig.LIGHT_RIG`，与视口同一组值。缩略图因此不是一份需要单独维护
   的美术资产，格子里的姿势即摆进舞台后的姿势。

   **独立的 WebGL 上下文**：绝不借用舞台渲染器。它的 `setSize` / `setPixelRatio`
   同时受视口布局与逐帧导出支配（导出锁 pixelRatio=1 且按 Seedance 目标分辨率
   渲染），借它画缩略图等于在这两条约束之间插入第三方改动。代价是多一个上下文，
   由 `disposePreview()` 在控制台卸载时释放。

   **绘制策略**：一台离屏渲染器逐格渲染后 `drawImage` 拷进各格的 2D 画布。不为每
   格建上下文（同页 WebGL 上下文数有硬上限），也不缓存动画像素（一格位图在 Retina
   下约 130KB，十七个动作各存一条循环即几十兆常驻）。
   ========================================================================== */

import * as THREE from "three";

import { h } from "../app/core.js";

import { Actor } from "./actors.js";
import { ACTIONS, LIGHT_RIG, MODELS, buildProp } from "./rig.js";

// 缩略图播放帧率。它只影响观感，不参与任何时长契约——12 帧足以读出动作的节奏，
// 再高只是让十几格缩略图同时抢主线程
const PREVIEW_FPS = 12;
// 一次性动作播完后的停顿，读完收势再重播
const HOLD_SEC = 0.6;
// 取景方向：略偏左前的四分之三视角，正面与侧面的信息同时保留（正视会把前后
// 摆动压没，正侧会把左右张开压没）
const VIEW_DIR = new THREE.Vector3(0.62, 0.40, 1).normalize();
const FOV = 30;
// 取景留白系数：包围球贴边会让摆臂最大的相位擦到画框
const FIT_MARGIN = 1.12;
// 阴影视锥半径：覆盖最高人偶（1.78m）与趴姿沿纵深的展开，固定值省去逐动作重算
const SHADOW_EXTENT = 2.2;

let rig = null;
/** 取景参数缓存，键为「模型 + 动作」或「道具」——包围盒采样只需算一次。 */
const FRAMING = new Map();
/** 静止缩略图位图缓存，键为「用途 + 资产 + 边长」。 */
const STATIC = new Map();
/** 正在播放的格子。画布从 DOM 摘除即自动退出，弹层关闭无需逐个注销。 */
const LIVE = new Set();
/** 画布 → 格子，供可见性回调反查。 */
const CELL_OF = new WeakMap();
let ticker = null;
let lastTick = 0;
let observer = null;

/* ------------------------------------------------------------------ 离屏舞台 */
function ensureRig() {
  if (rig) return rig;
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(1);        // 画布尺寸已是设备像素，再乘一次就是两倍开销
  renderer.setClearAlpha(0);        // 透明底：缩略图直接坐在面板底色上，无需配色对齐
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;

  const scene = new THREE.Scene();
  const key = new THREE.DirectionalLight(LIGHT_RIG.key.color, LIGHT_RIG.key.intensity);
  key.position.set(...LIGHT_RIG.key.pos);
  key.castShadow = true;
  key.shadow.mapSize.set(512, 512);
  const sc = key.shadow.camera;
  Object.assign(sc, {
    left: -SHADOW_EXTENT, right: SHADOW_EXTENT,
    top: SHADOW_EXTENT, bottom: -SHADOW_EXTENT, near: 1, far: 32,
  });
  sc.updateProjectionMatrix();      // 只赋值不重算，阴影仍按默认视锥投影
  key.shadow.bias = -0.0006;
  key.shadow.normalBias = 0.02;
  const fill = new THREE.DirectionalLight(LIGHT_RIG.fill.color, LIGHT_RIG.fill.intensity);
  fill.position.set(...LIGHT_RIG.fill.pos);
  const rim = new THREE.DirectionalLight(LIGHT_RIG.rim.color, LIGHT_RIG.rim.intensity);
  rim.position.set(...LIGHT_RIG.rim.pos);
  scene.add(key, fill, rim, new THREE.HemisphereLight(
    LIGHT_RIG.hemi.sky, LIGHT_RIG.hemi.ground, LIGHT_RIG.hemi.intensity));

  // 只收阴影不画自身的地面：给出脚下的接触关系，又不在透明底上留一块方形色块
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(12, 12),
    new THREE.ShadowMaterial({ opacity: 0.34 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  rig = {
    renderer, scene, ground,
    camera: new THREE.PerspectiveCamera(FOV, 1, 0.1, 60),
    actor: null, actorModel: null, actorKey: null,
    prop: null, propKind: null,
    sizeW: 0, sizeH: 0,
  };
  return rig;
}

/** 换体型要重建人偶——体块尺寸在建模时按身高定死，不是可缩放的整体。 */
function ensureActor(modelKey) {
  const g = ensureRig();
  if (g.actor && g.actorModel === modelKey) return g.actor;
  g.actor?.dispose();
  // 显式给 id：缺省 id 会消耗 Actor 的全局序号，舞台上新建角色的缺省名随之跳号
  g.actor = new Actor({ id: "preview", name: "preview", model: modelKey,
    tracks: [{ t0: 0, action: "idle" }] });
  g.actor.object.traverse((o) => { o.castShadow = true; });
  g.actorModel = modelKey;
  g.actorKey = null;
  g.scene.add(g.actor.object);
  return g.actor;
}

function ensureProp(kind) {
  const g = ensureRig();
  if (g.prop && g.propKind === kind) return g.prop;
  if (g.prop) {
    g.scene.remove(g.prop);
    g.prop.traverse((o) => o.geometry?.dispose?.());
  }
  g.prop = buildProp(kind);
  g.prop.traverse((o) => { o.castShadow = true; });
  g.propKind = kind;
  g.scene.add(g.prop);
  return g.prop;
}

/** 把相机摆到能框住 `box` 的位置。取景只与包围球有关，故与画幅比例无关。 */
function frameBox(box) {
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 0.2);
  return {
    center: sphere.center.clone(),
    dist: radius / Math.sin((FOV / 2) * Math.PI / 180) * FIT_MARGIN,
  };
}

function applyFraming(f) {
  const g = ensureRig();
  g.camera.position.copy(f.center).addScaledVector(VIEW_DIR, f.dist);
  g.camera.lookAt(f.center);
}

/** 一个动作在整段循环里的取景：按相位采样求包围盒并集，
 *  否则腾空相（跳、飞）或倒地相（趴、倒下）会在某些帧被切掉。 */
function actionFraming(modelKey, actionKey) {
  const ck = `${modelKey}|${actionKey}`;
  if (FRAMING.has(ck)) return FRAMING.get(ck);
  const g = ensureRig();
  const actor = ensureActor(modelKey);
  actor.setTracks([{ t0: 0, action: actionKey }]);
  g.actorKey = actionKey;
  const dur = (ACTIONS[actionKey] || ACTIONS.idle).dur;
  const box = new THREE.Box3();
  for (let i = 0; i <= 8; i++) {
    actor.update(dur * (i / 8), dur);
    box.expandByObject(actor.object);
  }
  box.min.y = Math.min(box.min.y, 0);        // 地面接触点始终在画内，人才不像悬着
  const f = frameBox(box);
  FRAMING.set(ck, f);
  return f;
}

/** 体型格共用一套取景（按最高的人偶定），身高差异因此在格子之间直接可读。 */
function modelFraming() {
  const ck = "models";
  if (FRAMING.has(ck)) return FRAMING.get(ck);
  const tallest = Object.entries(MODELS)
    .sort((a, b) => b[1].height - a[1].height)[0][0];
  const actor = ensureActor(tallest);
  actor.setTracks([{ t0: 0, action: "idle" }]);
  actor.update(0, ACTIONS.idle.dur);
  const box = new THREE.Box3().expandByObject(actor.object);
  box.min.y = Math.min(box.min.y, 0);
  const f = frameBox(box);
  FRAMING.set(ck, f);
  return f;
}

function propFraming(kind) {
  const ck = `prop|${kind}`;
  if (FRAMING.has(ck)) return FRAMING.get(ck);
  const prop = ensureProp(kind);
  const box = new THREE.Box3().expandByObject(prop);
  box.min.y = Math.min(box.min.y, 0);
  const f = frameBox(box);
  FRAMING.set(ck, f);
  return f;
}

/** 渲染当前场景并拷进目标画布。画布的位图尺寸就是渲染尺寸，不做二次缩放。 */
function blit(canvas) {
  const g = ensureRig();
  const w = canvas.width, hgt = canvas.height;
  if (g.sizeW !== w || g.sizeH !== hgt) {
    g.renderer.setSize(w, hgt, false);
    g.camera.aspect = w / hgt;
    g.camera.updateProjectionMatrix();
    g.sizeW = w;
    g.sizeH = hgt;
  }
  g.renderer.render(g.scene, g.camera);
  const cx = canvas.getContext("2d");
  cx.clearRect(0, 0, w, hgt);
  cx.drawImage(g.renderer.domElement, 0, 0, w, hgt);
}

/** 画布只在场上留一件：道具格与人偶格轮流渲染，另一件必须先退场。 */
function soloActor(modelKey) {
  const g = ensureRig();
  ensureActor(modelKey);
  g.actor.object.visible = true;
  if (g.prop) g.prop.visible = false;
}

function soloProp(kind) {
  const g = ensureRig();
  ensureProp(kind);
  g.prop.visible = true;
  if (g.actor) g.actor.object.visible = false;
}

/* -------------------------------------------------------------------- 播放 */
function ensureObserver() {
  if (observer) return observer;
  // 弹层可滚动，滚出视野的格子不必再渲——十七个动作同时在画是主线程最大的一笔
  observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const cell = CELL_OF.get(e.target);
      if (cell) cell.visible = e.isIntersecting;
    }
  }, { threshold: 0.05 });
  return observer;
}

/** 把离屏舞台摆成「某体型在某动作的 t 秒」。逐格渲染前恒调它一次。 */
function poseAction(modelKey, actionKey, t) {
  const g = ensureRig();
  soloActor(modelKey);
  // 取景要按相位采样，采样会改动轨段——先取景后指派，顺序不可换
  const f = actionFraming(modelKey, actionKey);
  if (g.actorKey !== actionKey) {
    g.actor.setTracks([{ t0: 0, action: actionKey }]);
    g.actorKey = actionKey;
  }
  applyFraming(f);
  g.actor.update(t, (ACTIONS[actionKey] || ACTIONS.idle).dur);
}

function drawCell(cell) {
  poseAction(cell.model, cell.key, Math.min(cell.t, cell.dur));
  blit(cell.canvas);
}

function tick(now) {
  if (!LIVE.size) { ticker = null; return; }
  ticker = requestAnimationFrame(tick);
  if (now - lastTick < 1000 / PREVIEW_FPS) return;
  lastTick = now;
  for (const cell of [...LIVE]) {
    if (!cell.canvas.isConnected) {
      observer?.unobserve(cell.canvas);
      LIVE.delete(cell);
      continue;
    }
    if (!cell.visible) continue;
    cell.t = (cell.t + 1 / PREVIEW_FPS) % cell.span;
    drawCell(cell);
  }
}

function startTicker() {
  if (ticker == null) ticker = requestAnimationFrame(tick);
}

/* ---------------------------------------------------------------- 对外接口 */
/** 缩略图画布：`size` 是 CSS 像素，位图按设备像素率放大，Retina 下才不糊。 */
function thumbCanvas(size) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const canvas = h("canvas", { class: "dz-thumb",
    width: Math.round(size * dpr), height: Math.round(size * dpr) });
  canvas.style.width = `${size}px`;
  canvas.style.height = `${size}px`;
  return canvas;
}

/**
 * 静止缩略图：首次渲染后留一份位图，同图再取直接拷贝。
 *
 * 检查器在拖拽路点这类操作里会按帧整块重建，静止缩略图若逐次重渲，就是把一次
 * WebGL 绘制绑在了每一帧界面刷新上。位图按尺寸分键——同一动作在检查器与弹层里
 * 用的是两种边长。
 */
function staticThumb(cacheKey, size, pose) {
  const canvas = thumbCanvas(size);
  const ck = `${cacheKey}|${canvas.width}`;
  const hit = STATIC.get(ck);
  if (hit) {
    canvas.getContext("2d").drawImage(hit, 0, 0);
    return canvas;
  }
  try {
    pose();
    blit(canvas);
    const keep = document.createElement("canvas");
    keep.width = canvas.width;
    keep.height = canvas.height;
    keep.getContext("2d").drawImage(canvas, 0, 0);
    STATIC.set(ck, keep);
  } catch { /* WebGL 不可用时留一格空白，不拖垮整个选择器 */ }
  return canvas;
}

/** 一格动作缩略图。`play` 为真时接入共享播放轮询，画布离开 DOM 即自动停播。 */
export function actionThumb(actionKey, { model = "mannequin_n", size = 92, play = true } = {}) {
  const meta = ACTIONS[actionKey] || ACTIONS.idle;
  if (!play) {
    return staticThumb(`act|${actionKey}|${model}`, size,
      () => poseAction(model, actionKey, 0));
  }
  const canvas = thumbCanvas(size);
  const cell = { canvas, key: actionKey, model, t: 0, dur: meta.dur,
    span: meta.loop ? meta.dur : meta.dur + HOLD_SEC, visible: true };
  // 先画一帧：等到第一次轮询才出图，格子会空一下
  try { drawCell(cell); } catch { /* 同上 */ }
  LIVE.add(cell);
  CELL_OF.set(canvas, cell);
  ensureObserver().observe(canvas);
  startTicker();
  return canvas;
}

/** 一格体型缩略图（静止的待机姿）。四种体型共用取景，身高差因此直接可比。 */
export function modelThumb(modelKey, { size = 46 } = {}) {
  return staticThumb(`model|${modelKey}`, size, () => {
    const f = modelFraming();
    soloActor(modelKey);
    const g = ensureRig();
    g.actor.setTracks([{ t0: 0, action: "idle" }]);
    g.actorKey = "idle";
    g.actor.update(0, ACTIONS.idle.dur);
    applyFraming(f);
  });
}

/** 一格道具缩略图。道具尺度跨度大（篝火 0.35m ↔ 树 4m），逐件取景。 */
export function propThumb(propKind, { size = 46 } = {}) {
  return staticThumb(`prop|${propKind}`, size, () => {
    const f = propFraming(propKind);
    soloProp(propKind);
    applyFraming(f);
  });
}

/** 释放离屏上下文与全部缓存。控制台卸载时必须调用。 */
export function disposePreview() {
  if (ticker != null) cancelAnimationFrame(ticker);
  ticker = null;
  LIVE.clear();
  observer?.disconnect();
  observer = null;
  FRAMING.clear();
  STATIC.clear();
  if (!rig) return;
  rig.actor?.dispose();
  rig.prop?.traverse((o) => o.geometry?.dispose?.());
  rig.scene.traverse((o) => { o.geometry?.dispose?.(); });
  rig.renderer.dispose();
  rig.renderer.forceContextLoss?.();
  rig = null;
}
