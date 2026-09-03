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
   时间轴 —— Shot-Cuts 脊柱 + 播放头

   Shot-Cuts 轨**就是** `shots[]`：每个镜头块绑定「哪一镜 · 用哪个机位 · 从几秒到
   几秒」。这条轨是整个控制台的脊柱——角色什么时候做什么、相机怎么飞，都挂在它
   给出的时间坐标上。

   两条纪律（与引擎的契约不变量对齐）：
     · **绝不删镜、绝不重排 shot id**——删一个镜头块 = 把那一镜置 `omt`（弃用），
       由引擎侧的审阅状态机承接；id 与 `shot_<id>.*` 文件、版本栈是绑死的。
     · 镜头块时长按 Seedance 整秒档位（4~15s）**钳制并显示**，让导演在 3D 里排的
       时长与最终片长 1:1——差一秒就是运动被拉伸或截断。

   播放是**墙钟驱动的预览**（rAF），导出是**逐帧步进**（见 exporter.js）——
   两条路径共用同一个 `seek(t)` 求值函数，所以预览看到的就是导出得到的。
   ========================================================================== */

export const SNAP_MIN = 4, SNAP_MAX = 15;

/** 把任意时长钳成 Seedance 整秒档（与 engine/kinema/previz.snap_duration 同口径）。 */
export function snapDuration(d) {
  return Math.max(SNAP_MIN, Math.min(SNAP_MAX, Math.round(Number(d) || 0)));
}

export class Timeline {
  constructor({ cuts = [], fps = 24, onTick = null } = {}) {
    this.fps = fps;
    this.cuts = cuts.map((c) => ({ ...c }));
    this.t = 0;
    this.playing = false;
    this.onTick = onTick;
    this._raf = null;
    this._last = 0;
    this.normalize();
  }

  /** 让镜头块首尾相接、时长合法、按时间排序——UI 改动后总调它一次。
   *
   * **`dur` 是时长的单一真源**，只有它缺失时才从 `t_out - t_in` 反推（那是从章节
   * 文档恢复编排的路径）。反过来写会让 `setCutDur` 变成空操作：先把 `c.dur` 改成 6，
   * 再从还没更新的 `t_out - t_in`（旧的 5）算回去，新时长当场被覆盖——表现为
   * 「换了运镜、镜头块时长却不跟着变」，而且完全不报错。
   */
  normalize() {
    this.cuts.sort((a, b) => (a.t_in || 0) - (b.t_in || 0));
    let t = 0;
    for (const c of this.cuts) {
      const raw = c.dur != null ? c.dur : ((c.t_out ?? 0) - (c.t_in ?? 0));
      c.dur = snapDuration(raw || 5);
      c.t_in = t;
      c.t_out = t + c.dur;
      t = c.t_out;
    }
    this.total = t;
    if (this.t > this.total) this.t = this.total;
    return this.cuts;
  }

  get duration() { return this.total || 0; }

  cutAt(t = this.t) {
    for (const c of this.cuts) if (t >= c.t_in && t < c.t_out) return c;
    return this.cuts[this.cuts.length - 1] || null;
  }

  /** 镜内归一时间 0~1（运镜 preset 按它采样）。 */
  localOf(t = this.t) {
    const c = this.cutAt(t);
    if (!c || c.dur <= 0) return { cut: c, local: 0, tLocal: 0 };
    const tl = Math.max(0, Math.min(c.dur, t - c.t_in));
    return { cut: c, local: tl / c.dur, tLocal: tl };
  }

  seek(t) {
    this.t = Math.max(0, Math.min(this.duration, t));
    this.onTick?.(this.t);
    return this.t;
  }

  /** 逐帧步进（导出与「按帧微调」共用，避免浮点累积误差）。 */
  stepFrame(n = 1) {
    return this.seek(Math.round(this.t * this.fps + n) / this.fps);
  }

  play() {
    if (this.playing || this.duration <= 0) return;
    this.playing = true;
    this._last = performance.now();
    const loop = (now) => {
      if (!this.playing) return;
      const dt = Math.min(0.1, (now - this._last) / 1000);   // 卡顿时最多补 100ms
      this._last = now;
      let t = this.t + dt;
      if (t >= this.duration) t = 0;                         // 循环预览
      this.seek(t);
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  pause() {
    this.playing = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = null;
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  /* ---------------- 镜头块编辑（绝不删镜，见文件头纪律） ---------------- */
  addCut({ shot, camera, dur = 5 }) {
    this.cuts.push({ shot, camera, dur: snapDuration(dur), t_in: this.total, t_out: 0 });
    this.normalize();
    return this.cuts[this.cuts.length - 1];
  }

  setCutDur(shot, dur) {
    const c = this.cuts.find((x) => String(x.shot) === String(shot));
    if (c) { c.dur = snapDuration(dur); this.normalize(); }
    return c;
  }

  moveCut(shot, delta) {
    const i = this.cuts.findIndex((x) => String(x.shot) === String(shot));
    const j = i + delta;
    if (i < 0 || j < 0 || j >= this.cuts.length) return false;
    const [c] = this.cuts.splice(i, 1);
    this.cuts.splice(j, 0, c);
    // 重排后强制按新顺序重算首尾（normalize 按 t_in 排序，故先把 t_in 写成序号）
    this.cuts.forEach((x, k) => { x.t_in = k; x.t_out = k + (x.dur || 5); });
    this.normalize();
    return true;
  }

  removeCut(shot) {
    this.cuts = this.cuts.filter((x) => String(x.shot) !== String(shot));
    this.normalize();
  }

  serialize() {
    return this.cuts.map((c) => ({
      shot: c.shot, camera: c.camera,
      t_in: +c.t_in.toFixed(3), t_out: +c.t_out.toFixed(3),
    }));
  }
}

export const fmtT = (s) => {
  const v = Math.max(0, Number(s) || 0);
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${v.toFixed(1).padStart(4, "0")}`;
};
