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
   运镜 preset 求值器 —— 把 `camera_catalog` 的一行变成每帧的相机位姿

   目录来自引擎 `pipeline/camera.py`（经 /api/overview 下发），**本文件不硬编码
   任何 preset**：36 个运镜的关键帧、缓动、跟随模式、fov 曲线全部读目录。
   这样「3D 里怎么飞」与「写进 shots[].camera 发给 Seedance 的那句话」永远同源——
   两者本就是同一个 preset 的两个面。

   求值的四条规则（按优先级）：
     ① `path.type === "orbit"` → 位置走精确圆弧（`pos=(R·sinθ, h, R·cosθ)`），
        比拿两个端点做样条更准（样条会把圆弧抹成一条鼓起来的弦）；
     ② 否则位置走 `CatmullRomCurve3` + **`getPointAt(u)`**（弧长参数化=匀速）；
        `getPoint(t)` 会在控制点附近聚簇，同样的 t 走出忽快忽慢的运动；
     ③ `lock_subject_scale`（希区柯克变焦）→ fov **由距离推导**而非插值，
        使主体在画面里大小恒定，这是 vertigo 效果的全部机理；
     ④ `look`：subject 每帧重绑活体主体（跟随）｜keys 插值 keyed target（摇/甩）｜
        path 看路径切向（穿越/长镜）。

   全程**无随机、无墙钟**：手持/斯坦尼康的浮动用**确定性的正弦叠加**代替 Perlin
   噪声，同一个 t 恒得同一个位姿——previz 逐字节可复现是它能作为「对照参考」的前提。
   ========================================================================== */

import * as THREE from "three";

/* 缓动（Penner）——Three.js 无内置缓动，名字与 pipeline/camera.py 的 EASES 锁步 */
export const EASE = {
  linear: (t) => t,
  easeInOutSine: (t) => -(Math.cos(Math.PI * t) - 1) / 2,
  easeInOutCubic: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  easeOutCubic: (t) => 1 - Math.pow(1 - t, 3),
  easeInCubic: (t) => t * t * t,
  easeInExpo: (t) => (t === 0 ? 0 : Math.pow(2, 10 * t - 10)),
  easeOutExpo: (t) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t)),
  easeInOutExpo: (t) => (t === 0 ? 0 : t === 1 ? 1
    : t < 0.5 ? Math.pow(2, 20 * t - 10) / 2 : (2 - Math.pow(2, -20 * t + 10)) / 2),
  easeOutBack: (t) => 1 + 2.70158 * Math.pow(t - 1, 3) + 1.70158 * Math.pow(t - 1, 2),
};

const clamp01 = (x) => Math.max(0, Math.min(1, x));
const lerp = (a, b, t) => a + (b - a) * t;
const UP = new THREE.Vector3(0, 1, 0);

/** 机位自定义轨道曲线（可编辑 dolly track）——**唯一建曲线入口**：rig 求值与
 *  stage 的轨迹可视都调它。张力/闭合参数只此一份，两边各建一条就会出现
 *  「画出来的线」与「相机真正飞的线」不是同一条的分叉。 */
export function camPathCurve(points) {
  return new THREE.CatmullRomCurve3(
    points.map((p) => new THREE.Vector3(...p)), false, "catmullrom", 0.5);
}

/** 在关键帧序列里按 t 找到相邻两帧与段内比例。 */
function span(keys, t) {
  for (let i = 0; i < keys.length - 1; i++) {
    if (t <= keys[i + 1].t) {
      const a = keys[i], b = keys[i + 1];
      const w = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
      return [a, b, w];
    }
  }
  return [keys[keys.length - 2], keys[keys.length - 1], 1];
}

/** 确定性「手持/斯坦尼康」浮动：三个不通约频率的正弦叠加，形如噪声但完全可复现。 */
function floatOffset(t, freq, seedPhase) {
  const w = 2 * Math.PI * freq;
  return (Math.sin(w * t + seedPhase) * 0.6
        + Math.sin(w * 2.37 * t + seedPhase * 1.7) * 0.28
        + Math.sin(w * 4.13 * t + seedPhase * 2.9) * 0.12);
}

/* 兜底 preset：单个 key 认不出来时顶上，绝不让一个拼错的运镜名把整个场景打崩。
   刻意是「固定机位」——静止机位是无副作用的兜底，画面不动即表明运镜没生效。 */
const FALLBACK_PRESET = {
  key: "__fallback", label: "固定（兜底）", rig: "locked-off", tier: "stable",
  look: "keys", duration: 5, ease: "linear", phrase: "",
  keys: [{ t: 0, pos: [0, 1.5, 4], fov: 40, target: [0, 1.5, 0] },
         { t: 1, pos: [0, 1.5, 4], fov: 40, target: [0, 1.5, 0] }],
};

/**
 * 一个机位的运镜装备。
 *
 * `preset` 是 `camera_catalog` 的一行；`anchorFn()` 每帧返回主体锚点
 * （`{origin: Vector3, quaternion: Quaternion}`——preset 的关键帧是**主体相对**
 * 坐标，求值时乘主体世界矩阵，所以同一个 preset 能套到任意已摆好的角色身上）。
 */
export class CameraRig {
  constructor(preset, opts = {}) {
    if (!preset || !preset.keys?.length) {
      console.warn("[director] 未知运镜 preset，已回落固定机位:", preset);
      preset = FALLBACK_PRESET;
    }
    this.preset = preset;
    this.fovScale = opts.fovScale ?? 1;      // 机位焦距微调（检查器的焦距滑块）
    this.frame = opts.frame ?? null;         // 构图偏移 [fx, fy]（主体落点，0=正中）
    this.yawOffset = 0;                      // 方位偏转（弧度）：整段运镜绕主体 Y 轴旋转
    this.distScale = 1;                      // 径向距离缩放：整段运镜相对主体推远/拉近
    this.anchorFn = opts.anchorFn || (() => ({
      origin: new THREE.Vector3(), quaternion: new THREE.Quaternion(),
    }));
    this._curve = null;
    this._customCurve = null;    // 自定义轨道（世界坐标）：设了它 Body 就不走 preset 轨道
    this._pathKey = "";
    this._tmp = { p: new THREE.Vector3(), t: new THREE.Vector3(), q: new THREE.Quaternion() };
  }

  /**
   * 自定义轨道（世界坐标路点，Cinemachine dolly track 同义）。设了它，Body 从
   * 「preset 程序轨道 × yaw/dist」切到「可编辑轨道」：位置 = 弧长参数化
   * `getPointAt(ease(t))`——**匀速，且镜头块结束的那一帧恰好走到轨道终点**，
   * 拖多长多短都自动按时间比例控速（「时间到了才走一半」从机制上不可能）。
   * Aim（盯主体 / 看切向 / 键控目标）、fov 曲线、dolly-zoom 锁定、手持噪声
   * 全部保留——位置走轨道、镜头盯主体，正是 Cinemachine 的 Body/Aim 拆分。
   */
  setCustomPath(points) {
    const key = points && points.length >= 2 ? JSON.stringify(points) : "";
    if (key === this._pathKey) return;
    this._pathKey = key;
    this._customCurve = key ? camPathCurve(points) : null;
  }

  get duration() { return this.preset.duration || 5; }

  _ease(t) {
    const f = EASE[this.preset.ease] || EASE.linear;
    return clamp01(f(clamp01(t)));
  }

  _curveOf() {
    if (this._curve) return this._curve;
    const pts = this.preset.keys.map((k) => new THREE.Vector3(...k.pos));
    // tension 0.5 = 电影级缓弧；closed=false。控制点少于 2 个时退化成静止点
    this._curve = pts.length >= 2
      ? new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0.5)
      : null;
    return this._curve;
  }

  /** 局部（主体相对）位置。 */
  _localPos(te) {
    const path = this.preset.path;
    if (path && path.type === "orbit") {
      const az = lerp(path.az_start, path.az_end, te) * Math.PI / 180;
      const r = path.radius;
      const h = lerp(path.height, path.height_end ?? path.height, te);
      return this._tmp.p.set(r * Math.sin(az), h, r * Math.cos(az));
    }
    const c = this._curveOf();
    if (!c) return this._tmp.p.set(...(this.preset.keys[0]?.pos || [0, 1.5, 4]));
    // getPointAt = 弧长参数化（匀速）；getPoint 会在控制点附近聚簇成忽快忽慢
    return c.getPointAt(te, this._tmp.p);
  }

  /** 局部 look-at 点（仅 look==="keys" 用）。 */
  _localTarget(te) {
    const [a, b, w] = span(this.preset.keys, te);
    return this._tmp.t.set(lerp(a.target[0], b.target[0], w),
                           lerp(a.target[1], b.target[1], w),
                           lerp(a.target[2], b.target[2], w));
  }

  _fovAt(te) {
    const [a, b, w] = span(this.preset.keys, te);
    return lerp(a.fov, b.fov, w);
  }

  /** 位置（世界系·噪声前）：自定义轨道直取曲线；否则 preset 局部轨道 ×
   *  dist/yaw × 主体锚点。look/bank 的前瞻点也走这里——两种轨道一套口径，
   *  各写一份就会在自定义轨道下「人在轨道上飞、头却按旧轨道转」。 */
  _worldPos(te, anchor) {
    if (this._customCurve) return this._customCurve.getPointAt(te, this._tmp.p).clone();
    const local = this._localPos(te).clone();
    if (this.distScale !== 1) { local.x *= this.distScale; local.z *= this.distScale; }
    if (this.yawOffset) local.applyAxisAngle(UP, this.yawOffset);
    return local.applyQuaternion(anchor.quaternion).add(anchor.origin);
  }

  /**
   * 把相机摆到 t（**归一时间 0~1**）对应的位姿。返回 `{pos, target, fov, roll}` 供调试。
   *
   * `cam.fov` 改动后必须 `updateProjectionMatrix()`——漏掉这一句是 Three.js 相机
   * 最常见的坑：数值变了而投影矩阵还是旧的，画面看起来「变焦不生效」。
   */
  apply(cam, t, { subjectPos = null } = {}) {
    const te = this._ease(t);
    const anchor = this.anchorFn();
    // 位置走 `_worldPos`：preset 模式=方位偏转 + 径向缩放（局部坐标先缩放/旋转
    // 再乘主体朝向——「换一侧拍」「离远点拍」不破坏 preset 的运动设计，且
    // **所拖即所播**：拖机身把机位放到哪，播放时相机就在那一刻精确经过哪，
    // 见 stage.dragCamTo）；自定义轨道模式=弧长参数化直取曲线（匀速·终点必达）。
    const world = this._worldPos(te, anchor);

    // 手持/斯坦尼康浮动（确定性正弦，非随机）
    const nz = this.preset.noise;
    if (nz) {
      world.x += floatOffset(t * this.duration, nz.freq, 0.0) * nz.pos;
      world.y += floatOffset(t * this.duration, nz.freq * 0.83, 1.7) * nz.pos * 0.7;
      world.z += floatOffset(t * this.duration, nz.freq * 1.19, 3.4) * nz.pos * 0.5;
    }
    // 卢贝兹基漂浮的呼吸起伏
    if (this.preset.bob) {
      world.y += Math.sin(2 * Math.PI * 0.35 * t * this.duration) * this.preset.bob;
    }
    cam.position.copy(world);

    // look-at 目标
    let target;
    if (this.preset.look === "subject" && subjectPos) {
      target = subjectPos.clone();
    } else if (this.preset.look === "path") {
      // 看路径切向：取前瞻点（末端夹到 1，避免越界回到起点造成一帧回头）
      const ahead = this._worldPos(Math.min(1, te + 0.008), anchor);
      target = ahead.distanceToSquared(world) < 1e-8
        ? world.clone().add(new THREE.Vector3(0, 0, -1)) : ahead;
    } else {
      target = this._localTarget(te).clone()
        .applyQuaternion(anchor.quaternion).add(anchor.origin);
    }
    cam.lookAt(target);

    // fov：dolly-zoom 由**距离推导**（锁主体大小），其余按关键帧插值
    let fov = this._fovAt(te);
    if (this.preset.lock_subject_scale) {
      const k0 = this.preset.keys[0];
      const d0 = Math.hypot(k0.pos[0] - k0.target[0],
                            k0.pos[1] - k0.target[1], k0.pos[2] - k0.target[2]) || 1;
      const halfW = d0 * Math.tan(k0.fov * Math.PI / 360);   // 恒定的场景半宽
      const d = Math.max(0.05, cam.position.distanceTo(target));
      fov = 2 * Math.atan(halfW / d) * 180 / Math.PI;
    }
    // crash zoom 的末段过冲回弹（先冲过头再收住，是它「急停」观感的来源）
    if (this.preset.overshoot && t > 0.78) {
      const k = EASE.easeOutBack((t - 0.78) / 0.22);
      fov -= this.preset.overshoot * (1 - k) * 0.5;
    }
    cam.fov = Math.max(5, Math.min(120, fov * this.fovScale));
    cam.updateProjectionMatrix();

    // 构图偏移（Cinemachine「Screen X/Y」同义）：`lookAt` 把主体钉死在画面正中，
    // 而三分构图 / 头顶留白 / 视线预留全都要求主体**离开**正中——在 lookAt 之后
    // 按目标落点反解相机的偏航/俯仰角。fx>0 = 主体落画面右侧，fy>0 = 偏上。
    const fr = this.frame;
    if (fr && (fr[0] || fr[1])) {
      const vRad = cam.fov * Math.PI / 180;
      const hRad = 2 * Math.atan(Math.tan(vRad / 2) * cam.aspect);
      cam.rotateY(Math.atan(2 * fr[0] * Math.tan(hRad / 2)));
      cam.rotateX(-Math.atan(2 * fr[1] * Math.tan(vRad / 2)));
    }

    // 荷兰角 / FPV banking：lookAt 之后再绕视轴滚
    let roll = 0;
    if (this.preset.roll) roll = this.preset.roll * this._ease(t);
    if (this.preset.bank) {
      // 转弯方向 → 侧倾：用轨道的水平转率近似（自定义轨道同样生效）
      const a = this._worldPos(Math.max(0, te - 0.02), anchor);
      const b = this._worldPos(Math.min(1, te + 0.02), anchor);
      roll += THREE.MathUtils.clamp(
        Math.atan2(b.x - a.x, Math.abs(b.z - a.z) + 1e-6) * 180 / Math.PI,
        -this.preset.bank, this.preset.bank);
    }
    if (roll) cam.rotateZ(roll * Math.PI / 180);

    return { pos: cam.position.clone(), target, fov: cam.fov, roll };
  }
}

/* ------------------------------------------------------------------ 目录取用 */
export function findPreset(catalog, key) {
  return (catalog || []).find((c) => c.key === key) || null;
}

/** 按三桶分组（基础/经典/大师）——选择器的分区顺序即目录顺序。 */
export function groupPresets(catalog) {
  const out = [];
  for (const c of catalog || []) {
    let g = out.find((x) => x.key === c.group);
    if (!g) out.push((g = { key: c.group, label: c.group_label, items: [] }));
    g.items.push(c);
  }
  return out;
}

/** 风险档 → 设计令牌色（●稳定=蓝 / ▲进阶=琥珀 / ■高危=红），与全站语义色一致。 */
export const TIER_CLR = { stable: "blue", advanced: "amber", "high-risk": "red" };

/** ▲ 进阶运镜的一集配额——**控制台侧提示的单一真源**。
 *  三处 UI 共用它（检查器纪律提示 / 统计 KPI 变红 / 统计条幅），分开写死过一次，
 *  结果是 KPI 在第 3 个就变红而条幅要到第 5 个才出声，两个数字互相打架。 */
export const ADVANCED_BUDGET = 4;

/**
 * 纪律提示（storyboard《进阶运镜四戒》在 UI 里的落地）。
 * 返回 `[{level, text}]`——**只提示不阻断**：与 `lint` 软闸同一哲学，
 * 运镜是创作决定，引擎给纪律不替人决定。
 */
export function tierNotes(preset, { advancedUsed = 0, dur = null } = {}) {
  const out = [];
  if (!preset) return out;
  if (preset.tier === "advanced") {
    out.push({ level: "info",
      text: `▲ 进阶档：一集建议 ≤${ADVANCED_BUDGET} 个，且措辞须含「缓慢/平稳」` });
    if (dur != null && dur < 5) {
      out.push({ level: "warn", text: `▲ 进阶运镜建议 dur≥5s（当前 ${dur}s），太短读不出运动` });
    }
    if (advancedUsed > ADVANCED_BUDGET) {
      out.push({ level: "warn",
        text: `本章已用 ${advancedUsed} 个 ▲ 进阶运镜——超过 ${ADVANCED_BUDGET} 个会稀释记忆点` });
    }
  }
  if (preset.tier === "high-risk") {
    out.push({ level: "warn", text: "■ 高危档：仅全集情绪最高点的那一镜才用，且 dry-run 必审" });
  }
  return out;
}
