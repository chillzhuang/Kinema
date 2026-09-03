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
   Actor —— 舞台上的一个角色：动作 + 路线 + 步态同步

   **确定性是这个类的第一属性**：previz 之所以能当「对照参考」，全靠同一个
   时间点恒得同一个位姿。因此：
     · 绝不用 `mixer.update(dt)` 累加墙钟——改为**直接设 `action.time` 再
       `mixer.update(0)`**（scrub 技法）。累加会因掉帧漂移，导出与预览各走各的。
     · 动作段之间的过渡不走 `crossFade`——它的权重按 mixer 时间累加，同样是墙钟
       依赖。改为在段边界的固定窗口内把两条 action 的权重写成 **t 的纯函数**，
       各自按绝对时间定位后由一次 `mixer.update(0)` 一起求值，过渡本身因此也满足
       「同一时间点恒得同一位姿」。
     · 路线取样一律 `getPointAt(u)`（弧长参数化=匀速）；`getPoint(t)` 会在控制点
       附近聚簇，同一段路走出忽快忽慢。

   **步态同步**：`timeScale = 实际地速 / 动作内建速度`。不同步就是脚滑——
   人以 3m/s 平移而 walk 循环还按 1.35m/s 迈步，脚会在地上蹭着飘。
   ========================================================================== */

import * as THREE from "three";
import { ACTIONS, buildClip, buildMannequin, hipRestY } from "./rig.js";

// timeScale 钳制：超出这个区间的播放速度已经不像人在动，宁可让它脚滑一点也别抽搐
const TS_MIN = 0.35, TS_MAX = 2.6;
// 段间过渡窗口（秒）。24fps 下约五帧：足以抹平换动作时的关节跳变，又不至于吃掉
// 一次性动作（闪避 0.9s、出招 1.0s）的起手拍。
const BLEND_SEC = 0.22;
// 权重曲线取 smoothstep：两端一阶导为零，过渡的进出都没有速度台阶
const smoothstep = (u) => u * u * (3 - 2 * u);
// 双足动作的朝向压平 Y（人不会脸朝天走路）；飞行体用完整切向
const FLAT_LOOK = new Set(["idle", "walk", "run", "jump", "crawl", "prone",
  "sit", "turn", "wave", "fall", "attack",
  "crouch", "dodge", "cover", "enter", "ride"]);

let _seq = 0;

export class Actor {
  /**
   * @param {object} spec `{id,name,model,pos:[x,y,z],rot:number(度,绕Y),
   *                        path:[[x,y,z]…]|null, speed:number|null,
   *                        tracks:[{t0,action}]}`
   */
  constructor(spec = {}) {
    this.id = spec.id || `actor_${++_seq}`;
    this.name = spec.name || `角色${_seq}`;
    this.model = spec.model || "mannequin_n";
    this.object = buildMannequin(this.model);
    this.object.userData.actorId = this.id;
    this.hipRestY = hipRestY(this.model);

    this.mixer = new THREE.AnimationMixer(this.object);
    this._actions = {};

    this.setTransform(spec.pos || [0, 0, 0], spec.rot ?? 0);
    this.setPath(spec.path || null);
    this.speedOverride = spec.speed ?? null;
    this.tracks = (spec.tracks && spec.tracks.length)
      ? spec.tracks.slice() : [{ t0: 0, action: spec.action || "idle" }];
    this.tracks.sort((a, b) => a.t0 - b.t0);
    this._ensureAction(this.tracks[0].action);
  }

  /* ---------------- 基础状态 ---------------- */
  setTransform(pos, rotDeg) {
    this.object.position.set(pos[0] || 0, pos[1] || 0, pos[2] || 0);
    this.object.rotation.y = (rotDeg || 0) * Math.PI / 180;
    this.baseRotY = this.object.rotation.y;
  }

  setPath(points) {
    this.pathPoints = (points && points.length >= 2) ? points.map((p) => [...p]) : null;
    this.curve = this.pathPoints
      ? new THREE.CatmullRomCurve3(
        this.pathPoints.map((p) => new THREE.Vector3(...p)), false, "catmullrom", 0.35)
      : null;
    this.pathLength = this.curve ? this.curve.getLength() : 0;
  }

  setTracks(tracks) {
    this.tracks = (tracks && tracks.length) ? tracks.slice() : [{ t0: 0, action: "idle" }];
    this.tracks.sort((a, b) => a.t0 - b.t0);
    this.tracks.forEach((tr) => this._ensureAction(tr.action));
  }

  /** t 秒时生效的动作轨段序号（最后一个 t0 ≤ t 的）。 */
  segmentIndexAt(t) {
    let idx = 0;
    for (let i = 0; i < this.tracks.length; i++) {
      if ((this.tracks[i].t0 || 0) <= t + 1e-6) idx = i;
    }
    return idx;
  }

  /** t 秒时生效的动作轨段。 */
  segmentAt(t) { return this.tracks[this.segmentIndexAt(t)]; }

  _keyOf(i) { return (this.tracks[i] || {}).action || "idle"; }

  _ensureAction(key) {
    if (this._actions[key]) return this._actions[key];
    const meta = ACTIONS[key] || ACTIONS.idle;
    const clip = buildClip(key, this.hipRestY);
    const a = this.mixer.clipAction(clip);
    a.setLoop(meta.loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
    a.clampWhenFinished = !meta.loop;
    a.enabled = false;
    a.weight = 0;
    a.play();                    // 必须 play 才会进 mixer 的活动表；权重 0 = 不产生影响
    this._actions[key] = a;
    return a;
  }

  /* ---------------- 段间过渡（权重是 t 的纯函数） ---------------- */
  /**
   * 第 `i` 段的相位原点：向前并入指派了同一动作的连续段。
   *
   * 相邻两段指派同一个动作时，若各自从本段 t0 重起相位，边界处会看到循环被打断
   * （走到一半的步子跳回起步相位）。并入之后这几段是一次连续表演。
   */
  _phaseOrigin(i) {
    let j = i;
    while (j > 0 && this._keyOf(j - 1) === this._keyOf(j)) j--;
    return this.tracks[j].t0 || 0;
  }

  /**
   * 第 `i` 段起点处的过渡窗口长度（秒）。
   *
   * 上界同时受前后两段约束：窗口越过前一段起点会取到该段尚未开始时的相位；
   * 越过本段一半则整段几乎都停在过渡里，指派的动作本身反而看不清。
   */
  _blendWindow(i, dur) {
    if (i <= 0) return 0;
    const t0 = this.tracks[i].t0 || 0;
    const prevLen = t0 - (this.tracks[i - 1].t0 || 0);
    const end = this.tracks[i + 1] ? (this.tracks[i + 1].t0 || 0) : Math.max(dur, t0);
    return Math.max(0, Math.min(BLEND_SEC, prevLen, (end - t0) / 2));
  }

  /** 把某个动作的播放头按绝对时间定位到 t（不推进 mixer 时间）。 */
  _scrub(key, origin, t, dur) {
    const a = this._ensureAction(key);
    const meta = ACTIONS[key] || ACTIONS.idle;
    const ts = this.timeScaleFor(key, dur);
    a.timeScale = ts;
    const local = Math.max(0, t - origin) * ts;
    a.time = meta.loop ? (local % meta.dur) : Math.min(local, meta.dur - 1e-4);
  }

  /** 指定本帧参与求值的动作与权重。未列出的必须显式关掉——mixer 会把所有
   *  enabled 的 action 按权重累加，留一条权重非零的旧动作就是两个姿势叠在一起。 */
  _weigh(pairs) {
    for (const a of Object.values(this._actions)) { a.enabled = false; a.weight = 0; }
    for (const [key, w] of pairs) {
      const a = this._ensureAction(key);
      a.enabled = true;
      a.weight = w;
    }
  }

  /* ---------------- 步态同步 ---------------- */
  /** 位移段窗口：`speed>0` 的动作段 `[起, 止]` 列表。没有任何位移段但画了路线时
   *  回落为全程一段（路线不该因为动作全是原地而永远走不动）。 */
  _locoSpans(dur) {
    const spans = [];
    for (let i = 0; i < this.tracks.length; i++) {
      const tr = this.tracks[i];
      if (!((ACTIONS[tr.action] || {}).speed > 0)) continue;
      const end = this.tracks[i + 1] ? this.tracks[i + 1].t0 : dur;
      if (end > (tr.t0 || 0)) spans.push([tr.t0 || 0, end]);
    }
    return spans.length ? spans : [[0, Math.max(dur, 1e-6)]];
  }

  locoTotal(dur) {
    return this._locoSpans(dur).reduce((s, [a, b]) => s + (b - a), 0);
  }

  /**
   * 路线进度 u(t)∈[0,1]：**只在位移段内推进，且恰好在位移时间内走完全程**——
   * 「4 秒的走戏走完 4 秒的路」：位移段外原地表演、段内匀速推进、段尽路尽。
   * 若按整条时间轴摊薄（u = t/总时长），一条 34s 时间轴里 4s 的走戏只走完
   * 路线的 12%，会被读成「跑到一半就跳下一镜」「最后 1/3 没跑完」。
   * 纯函数（只依赖 t），预览与导出同源。
   */
  pathProgress(t, dur) {
    const spans = this._locoSpans(dur);
    let total = 0;
    let done = 0;
    for (const [a, b] of spans) total += b - a;
    if (total <= 0) return 1;
    for (const [a, b] of spans) done += Math.min(Math.max(t - a, 0), b - a);
    return Math.min(1, Math.max(0, done / total));
  }

  /** 实际地速（米/秒）＝路线长度 ÷ **位移段总时长**（不是整条时间轴）。 */
  groundSpeed(dur) {
    if (this.speedOverride != null) return this.speedOverride;
    if (!this.curve || dur <= 0) return 0;
    const lt = this.locoTotal(dur);
    return lt > 0 ? this.pathLength / lt : 0;
  }

  timeScaleFor(actionKey, dur) {
    const authored = (ACTIONS[actionKey] || ACTIONS.idle).speed;
    if (!authored) return 1;                       // 原地动作不做步态同步
    const g = this.groundSpeed(dur);
    if (!g) return 1;
    return THREE.MathUtils.clamp(g / authored, TS_MIN, TS_MAX);
  }

  /* ---------------- 每帧求值（确定性） ---------------- */
  /**
   * 把角色摆到镜头内 t 秒（`dur` = 本镜总时长）的状态。
   * **纯函数式**：只依赖 (t, dur)，不依赖上一帧，故任意顺序、任意次数调用结果一致。
   */
  update(t, dur) {
    const i = this.segmentIndexAt(t);
    const key = this._keyOf(i);
    const prevKey = i > 0 ? this._keyOf(i - 1) : null;
    const win = this._blendWindow(i, dur);
    // 过渡进度只由 t 与轨段表决定，不读墙钟——这是预览与逐帧导出同源的前提
    const blendU = win > 0 ? (t - (this.tracks[i].t0 || 0)) / win : 1;

    if (prevKey && prevKey !== key && blendU < 1) {
      const w = smoothstep(Math.max(0, blendU));
      this._weigh([[prevKey, 1 - w], [key, w]]);
      // 出场动作在窗口内继续推进自己的相位，否则过渡的一端是冻住的姿势
      this._scrub(prevKey, this._phaseOrigin(i - 1), t, dur);
      this._scrub(key, this.tracks[i].t0 || 0, t, dur);
    } else {
      this._weigh([[key, 1]]);
      this._scrub(key, this._phaseOrigin(i), t, dur);
    }
    this.mixer.update(0);        // delta=0：只应用绑定，不推进时间（scrub）

    // 路线跟随（进度只在位移段内推进——见 pathProgress）
    if (this.curve && dur > 0) {
      const u = this.pathProgress(t, dur);
      const p = this.curve.getPointAt(u);
      this.object.position.copy(p);
      // 前瞻朝向：看向路径下一点（末端夹取，避免最后一帧回头）
      const ahead = this.curve.getPointAt(Math.min(1, u + 0.01));
      const dir = ahead.clone().sub(p);
      if (FLAT_LOOK.has(key)) dir.y = 0;
      if (dir.lengthSq() > 1e-8) {
        this.object.rotation.y = Math.atan2(dir.x, dir.z);
      }
    }
    return this.object;
  }

  /** 主体锚点（运镜 preset 的关键帧是主体相对坐标，求值时乘它）。 */
  anchor() {
    return {
      origin: this.object.position.clone(),
      quaternion: new THREE.Quaternion().setFromEuler(
        new THREE.Euler(0, this.object.rotation.y, 0)),
    };
  }

  /** 看点：胸/面高度（与 camera.py 的 hL=1.5 同一约定，按实际身高等比）。 */
  lookPoint() {
    const h = this.object.userData.height || 1.72;
    return this.object.position.clone().add(new THREE.Vector3(0, h * 0.87, 0));
  }

  serialize() {
    return {
      id: this.id, name: this.name, model: this.model,
      pos: this.object.position.toArray().map((v) => +v.toFixed(4)),
      rot: +(this.baseRotY * 180 / Math.PI).toFixed(2),
      path: this.pathPoints ? this.pathPoints.map((p) => p.map((v) => +v.toFixed(4))) : null,
      speed: this.speedOverride,
      tracks: this.tracks.map((tr) => ({ t0: +(tr.t0 || 0).toFixed(3), action: tr.action })),
    };
  }

  dispose() {
    this.mixer.stopAllAction();
    this.object.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
    });
    this.object.parent?.remove(this.object);
  }
}

/* 路线可视在 pathtool.js（路管 + 数字路点针 + 方向锥）：1px 线 + 小圆点在
   俯视角下贴着网格几乎不可见，会直接造成「点了路点却看不见」。 */
