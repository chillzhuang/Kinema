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

// CSRF 是逐帧上传那条**手写 fetch** 的依赖：帧体是 image/png 二进制，走不了
// 统一的 `post()`（它恒发 JSON），故必须自带 token——服务端对全部 POST 强校验。
import { CSRF, post } from "../app/core.js";
/* ============================================================================
   previz 导出 —— 确定性逐帧渲染 → PNG 序列 → 引擎 ffmpeg 收尾

   **为什么不用 MediaRecorder / captureStream**（已否决，别再回头）：那是**墙钟
   实时**录制——掉一帧就少一帧，输出的 fps 与时长都不可控。而 previz 的全部价值
   在于「它与最终 Seedance 片长 1:1、运动可逐帧对照」，时长对不齐就什么都不是。

   本模块改为**同步 for 循环逐帧步进**：
     N = round(dur × fps)，第 i 帧对应绝对时间 t = i/fps
     → `sceneAt(t)`（所有 actor 用 `action.time=` 绝对定位、相机按 preset 在 t 采样）
     → `renderer.render()` → `canvas.toBlob('image/png')` → POST 一帧
   全程不读 `performance.now()`、不用随机数（手持浮动是确定性正弦叠加），
   因此**同一个场景在任何机器上渲出的帧逐字节一致**，可按场景哈希缓存。

   渲染尺寸取 `canvas[章节比例]`（如 1920×1080），与 Seedance 目标帧严格一致；
   `preserveDrawingBuffer` 必须为 true，否则 `toBlob` 在某些浏览器上拿到空图。
   ========================================================================== */

/** 一帧一帧地渲并上传。返回 `{frames, aborted}`。 */
export async function exportFrames({
  renderer, scene, camera, sceneAt, dur, fps = 24, width, height,
  project, chapter, shot, onProgress = null, shouldAbort = null,
}) {
  const N = Math.max(1, Math.round(dur * fps));
  // 直接改 drawingBuffer 尺寸而不动 CSS 尺寸（setSize 第三参 updateStyle=false）：
  // 视口该多大还多大，导出按 Seedance 目标分辨率渲——两者一致才谈得上「逐帧对照」。
  // **pixelRatio 必须锁 1**：setSize 会偷偷乘 devicePixelRatio，Retina 屏上导出的
  // 帧就是目标分辨率的 2×——「与 Seedance 同分辨率」与「任何机器渲出的帧一致」
  // 两条承诺同时被打破（视网膜屏与普通屏各渲各的尺寸）。
  const oldRatio = renderer.getPixelRatio();
  const oldW = renderer.domElement.width, oldH = renderer.domElement.height;
  renderer.setPixelRatio(1);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();

  let sent = 0;
  try {
    for (let i = 0; i < N; i++) {
      if (shouldAbort?.()) return { frames: sent, aborted: true };
      const t = i / fps;
      sceneAt(t);                       // ← 与预览共用的同一个求值函数
      renderer.render(scene, camera);
      const blob = await new Promise((res) => renderer.domElement.toBlob(res, "image/png"));
      if (!blob) throw new Error(`第 ${i} 帧抓取失败（canvas.toBlob 返回空）`);
      const q = new URLSearchParams({
        project, chapter, shot: String(shot), i: String(i),
      });
      const r = await fetch(`/api/previz/frame?${q}`, {
        method: "POST",
        headers: { "X-Csrf-Token": CSRF, "Content-Type": "image/png" },
        body: blob,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.error || `帧 ${i} 上传失败（HTTP ${r.status}）`);
      }
      sent++;
      onProgress?.(sent, N);
      // 每 8 帧让出一次主线程：否则长镜头导出期间整页卡死，连「中止」都点不动
      if (i % 8 === 7) await new Promise((res) => setTimeout(res, 0));
    }
  } finally {
    renderer.setPixelRatio(oldRatio);
    renderer.setSize(Math.round(oldW / oldRatio), Math.round(oldH / oldRatio), false);
    camera.aspect = oldW / oldH;
    camera.updateProjectionMatrix();
  }
  return { frames: sent, aborted: false };
}

/**
 * 触发引擎侧收尾：帧序列 → mp4 → 走 `previz register` 登记（首帧/末帧/参考片/运镜）。
 * 返回后台 job id，调用方用既有 `pollJob` 轮询——与全站长任务同一套忙态基建。
 */
export async function buildPreviz({ project, chapter, shot, fps, camera, useFirstFrame }) {
  const r = await post("/api/previz/render", {
    project, chapter, shot, fps, camera,
    use_first_frame: useFirstFrame ?? null,
  });
  return r.job;
}
