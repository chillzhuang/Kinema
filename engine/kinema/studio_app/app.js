/**
 * This file is part of Kinema.
 * Copyright (C) 2018-2099 BladeX (https://bladex.cn)
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

/* ═ Studio 前端模块 · app.js — 路由 · 全局事件 · ⌘K 场记检索 · 启动（分片集合入口）（原生 ES Module·免构建）═ */

/* ---------------- 空状态 ---------------- */
// 模块求值顺序锚定：与原单文件段序一致（重复 specifier 不会二次求值）
import { $ } from "./app/core.js";
import "./app/core.js";
import "./app/widgets.js";
import "./app/shell.js";
import "./app/overview.js";
import "./app/project-new.js";
import "./app/project.js";
import "./app/chapter.js";
import "./app/shot-tools.js";
import "./app/panels.js";
import "./app/ledger.js";
import "./app/playbook.js";
import "./app/config.js";
import "./app/skill.js";

import { api, getOverview, h, state } from "./app/core.js";
import { AudioBus, closeCinema, closeLightbox, stepLightbox } from "./app/widgets.js";
import { bindLegalNotice, renderRail, setCrumbs, syncRailActive } from "./app/shell.js";
import { viewOverview } from "./app/overview.js";
import { viewProjects } from "./app/project-new.js";
import { viewProject, viewScript } from "./app/project.js";
import { chapterSignature, viewChapter } from "./app/chapter.js";
import { STATE } from "./app/state.js";
import { closeVPanel, viewBoard, viewQueue } from "./app/panels.js";
import { viewCost, viewLibrary } from "./app/ledger.js";
import { openGuideModal, viewGuide } from "./app/playbook.js";
import { viewConfig } from "./app/config.js";
import { viewSkill } from "./app/skill.js";

function emptyBlock(title, desc, code) {
  return h("div", { class: "empty" },
    h("h3", null, title), h("p", null, desc || ""),
    code && h("pre", null, code));
}

/* ---------------- 路由 ---------------- */
const routes = [
  { re: /^#?\/?$/, name: "overview", crumb: () => [["总览"]], fn: viewOverview },
  { re: /^#\/projects$/, name: "projects", crumb: () => [["总览", "#/"], ["项目"]], fn: viewProjects },
  { re: /^#\/library$/, name: "library", crumb: () => [["总览", "#/"], ["片库"]], fn: viewLibrary },
  { re: /^#\/queue$/, name: "queue", crumb: () => [["总览", "#/"], ["待审队列"]], fn: viewQueue },
  { re: /^#\/board$/, name: "board", crumb: () => [["总览", "#/"], ["看板"]], fn: viewBoard },
  { re: /^#\/cost$/, name: "cost", crumb: () => [["总览", "#/"], ["成本"]], fn: viewCost },
  { re: /^#\/guide$/, name: "guide", crumb: () => [["总览", "#/"], ["指令集"]], fn: viewGuide },
  { re: /^#\/model$/, name: "model", crumb: () => [["总览", "#/"], ["模型配置"]], fn: viewConfig },
  { re: /^#\/skill$/, name: "skill", crumb: () => [["总览", "#/"], ["SKILL"]], fn: viewSkill },
  { re: /^#\/project\/([^/]+)$/, name: "project", fn: viewProject },
  { re: /^#\/project\/([^/]+)\/script$/, name: "script", fn: viewScript },
  { re: /^#\/project\/([^/]+)\/([^/]+)$/, name: "chapter", fn: viewChapter },
  // 3D 导演控制台：**懒加载独立路由**——进入时才 import() vendored 的 three.js
  // 与 director/*.js，其他视图一个字节都不下载（three 有 750KB，内联进 app.js
  // 会让全站每次加载都解析它）。视图自管 DOM 与生命周期，见 stage.js 的 dispose()。
  { re: /^#\/stage\/([^/]+)\/([^/]+)$/, name: "stage", fn: viewStage },
];

/* 控制台的卸载钩子：路由切走时必须调，否则 WebGL context / rAF / 键盘监听会留下来，
   来回切换导致浏览器内存持续增长（SPA 里最常见的泄漏）。 */
let stageDispose = null;
async function viewStage(view, pid, cid) {
  // pid/cid 由 render() 统一 decodeURIComponent 过一次，这里**不许再解一次**
  stageDispose?.();
  stageDispose = null;
  const e = encodeURIComponent;
  setCrumbs([["总览", "#/"], [pid, `#/project/${e(pid)}`],
             [cid, `#/project/${e(pid)}/${e(cid)}`], ["3D 导演"]]);
  view.classList.add("view-stage");
  try {
    const m = await import("/assets/director/stage.js");
    stageDispose = await m.mount(view, pid, cid);
  } catch (err) {
    view.innerHTML = "";
    view.append(emptyBlock("3D 导演控制台加载失败", String(err.message || err),
      "常见原因：浏览器不支持 WebGL2，或 assets/vendor/ 下的 three.js 缺失"));
    throw err;
  }
}

async function render() {
  const hash = location.hash || "#/";
  const view = $("#view");
  stopPoll();
  // 离开 3D 控制台先卸载（WebGL context 是稀缺资源，浏览器只给十几个）
  if (state.route?.name === "stage" && !hash.startsWith("#/stage/")) {
    stageDispose?.();
    stageDispose = null;
  }
  for (const r of routes) {
    const m = hash.match(r.re);
    if (!m) continue;
    const args = m.slice(1).map(decodeURIComponent);
    // **同路由重渲 = 刷新，不是导航**：全站十几处「改完东西 → getOverview(true); render()」
    // 走的都是这条路（角色试音、锁音色、设定图重生、水印、特效…）。它们要的是
    // "原地更新"，可 `view.innerHTML=""` 会让页面高度瞬间塌成 0、浏览器把滚动夹到顶，
    // 于是每次操作都被弹回页首（长项目页上角色卡等区块位于深处）。
    // 在**这里**判一次同路由并保位，比把十几个调用点各自改成局部刷新既小又稳。
    const routeKey = `${r.name}|${args.join("|")}`;
    const sameRoute = state.routeKey === routeKey;
    // 轮询暂停（音频剧本敲字等）是**本视图内**的态：带着 live=false 跨页会让全站
    // 章节轮询静默停摆，而 live-ind 在 live=false 时自隐藏，用户连恢复入口都没有
    if (!sameRoute) state.live = true;
    const keepY = sameRoute ? window.scrollY : 0;
    // 清空前先把当前高度钉住：不钉的话内容一空高度归零，滚动位置当场被夹掉，
    // 之后再 scrollTo 也只能"跳回去"（看得见一次闪动），钉住则全程不动
    const prevH = sameRoute ? view.offsetHeight : 0;
    if (prevH) view.style.minHeight = prevH + "px";
    state.route = { name: r.name, pid: args[0], cid: args[1] };
    state.routeKey = routeKey;
    if (r.crumb) setCrumbs(r.crumb());
    view.classList.remove("enter");
    view.classList.remove("ro-deleted");   // 软删只读态每次导航复位（进入软删项目的视图会再置上）
    view.classList.remove("view-app");     // 剧本工作台全屏两栏态每次导航复位（rich 模式会再置上）
    view.classList.remove("view-stage");   // 3D 控制台满幅态同理
    view.innerHTML = "";
    view.append(h("div", { class: "loading" }, "读取产物…"));
    // 过期守卫：每个 await 回来都可能已经不是这条路由了（快导航离开 / 连点侧栏）。
    // 慢视图（章节 1.3~34ms 服务端 + 两次 fetch + 大段 DOM 构建）迟到时直写 #view
    // 会覆盖已切走的页面；迟到的 startPoll 还会给旧章节复活 3s 定时器——第二次
    // render 开头的 stopPoll 早就跑完了。与 core.js softRefresh 的守卫同一条纪律。
    const stale = () => state.routeKey !== routeKey;
    let gone = false;
    try {
      const tmp = h("div");
      if (r.name === "chapter") {
        await viewChapter(view, args[0], args[1], { stale });  // 章节视图自管 innerHTML（轮询复用）
        gone = stale();
        if (!gone) startPoll(args[0], args[1]);
      } else if (r.name === "stage") {
        // 控制台必须直接挂进 #view：在离屏 tmp 里挂载会让 clientWidth 为 0，
        // 渲染器按 0×0 建 drawingBuffer，搬进来之后画面是黑的
        await viewStage(view, args[0], args[1]);
        gone = stale();
      } else {
        await r.fn(tmp, ...args);
        gone = stale();
        if (!gone) {
          view.innerHTML = "";
          view.append(...tmp.children);
        }
      }
      // 入场动画只给真正的导航；原地刷新再放一次淡入=整页闪一下，反而像"跳了"
      if (!gone && !sameRoute) view.classList.add("enter");
    } catch (err) {
      if (!stale()) {           // 迟到的报错块同样不覆盖新页面
        view.innerHTML = "";
        view.append(emptyBlock("加载失败", String(err.message || err), null));
      }
    }
    if (prevH) view.style.minHeight = "";
    if (gone) return;           // 滚动/侧栏/指示灯的复位归属新页面那次 render
    if (sameRoute && window.scrollY !== keepY) window.scrollTo(0, keepY);
    syncRailActive();
    $("#live-ind").hidden = !(r.name === "chapter" && state.live);
    return;
  }
  location.hash = "#/";
}

/* 章节实时轮询：生成中边跑边刷新 */
function startPoll(pid, cid) {
  stopPoll();
  state.pollTimer = setInterval(async () => {
    if (!state.live || document.hidden) return;
    if (AudioBus.current && !AudioBus.current.paused) return;      // 别打断试听
    const playing = [...document.querySelectorAll("#view video")].some((v) => !v.paused && !v.ended);
    if (playing || !$("#cinema").hidden || !$("#lightbox").hidden) return;
    try {
      const d = await api(`/api/chapter?project=${encodeURIComponent(pid)}&id=${encodeURIComponent(cid)}`);
      if (chapterSignature(d) !== STATE.chapSig) {
        await viewChapter($("#view"), pid, cid, { silent: true });
      }
    } catch { /* 服务重启间隙静默 */ }
  }, 3000);
}
function stopPoll() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/* ---------------- 全局事件 ---------------- */
// 移至回收站的项目=只读冻结：捕获阶段拦截 #view 内一切点击，彻底禁用所有操作
// （连 onclick 的 div 卡片也拦下）；仅放行导航链接 / 章节行 / 「恢复」钮。CSS 另做灰化。
$("#view").addEventListener("click", (e) => {
  if (!$("#view").classList.contains("ro-deleted")) return;
  if (e.target.closest("a[href], .chap-row, .ro-keep")) return;
  e.preventDefault();
  e.stopPropagation();
}, true);

// 切回标签页：标题亮点标记复位（后台期间任务完成由 toast 的 notifyAway 置上）
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && document.title.startsWith("● "))
    document.title = document.title.slice(2);
});
window.addEventListener("hashchange", render);
$("#btn-refresh").addEventListener("click", async () => {
  await getOverview(true);
  renderRail(state.overview);
  render();
});
// 指令集：弹层浮在当前视图上，**不动路由**——查完关掉，原来看到哪还在哪
$("#btn-guide").addEventListener("click", openGuideModal);
$("#live-ind").addEventListener("click", () => {
  state.live = !state.live;
  $("#live-ind").style.opacity = state.live ? "" : ".4";
});
document.querySelector(".rail-brand").addEventListener("click", () => (location.hash = "#/"));
document.querySelectorAll("[data-close=lb]").forEach((e) => e.addEventListener("click", closeLightbox));
document.querySelectorAll("[data-close=cin]").forEach((e) => e.addEventListener("click", closeCinema));
document.querySelectorAll("[data-close=vp]").forEach((e) => e.addEventListener("click", closeVPanel));
$("#lb-prev").addEventListener("click", () => stepLightbox(-1));
$("#lb-next").addEventListener("click", () => stepLightbox(1));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeLightbox(); closeCinema(); closeVPanel(); closePalette(); }
  if (!$("#lightbox").hidden && e.target.tagName !== "INPUT") {
    if (e.key === "ArrowLeft") stepLightbox(-1);
    if (e.key === "ArrowRight") stepLightbox(1);
  }
  if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
    e.preventDefault();
    $("#palette").hidden ? openPalette() : closePalette();
  }
});

/* ---------------- ⌘K 场记检索：跨项目定位素材 ---------------- */
const PAL = { items: [], sel: 0, timer: null, last: null };
const PAL_TAG = { shot: "镜", chapter: "章", project: "项", character: "角" };
function openPalette() {
  $("#palette").hidden = false;
  document.body.style.overflow = "hidden";
  const inp = $("#pal-input");
  inp.value = ""; PAL.items = []; PAL.sel = 0; PAL.last = null;
  renderPal();
  setTimeout(() => inp.focus(), 20);
}
function closePalette() {
  if ($("#palette").hidden) return;
  $("#palette").hidden = true;
  document.body.style.overflow = "";
}
function renderPal() {
  const body = $("#pal-body");
  body.innerHTML = "";
  $("#pal-count").textContent = PAL.items.length ? `${PAL.items.length} 条` : "";
  if (!PAL.items.length) {
    body.append(h("div", { class: "pal-empty" },
      $("#pal-input").value.trim()
        ? "没有匹配的素材——台词 / 字幕 / 提示词 / 角色 / 标题都能搜"
        : "输入关键词，跨项目定位历史素材"));
    return;
  }
  PAL.items.forEach((it, i) => {
    const sn = it.snippet || {};
    const row = h("div", { class: "pal-row" + (i === PAL.sel ? " sel" : ""),
        style: `animation-delay:${Math.min(i * 16, 200)}ms`,
        onclick: () => palGo(i) },
      it.thumb ? h("img", { src: it.thumb, loading: "lazy", alt: "" })
               : h("span", { class: "pal-ph" }, PAL_TAG[it.type] || "·"),
      h("div", null,
        h("div", { class: "pal-snip" }, sn.pre || "",
          h("mark", null, sn.hit || ""), sn.post || ""),
        h("div", { class: "pal-where" },
          [it.project_title, it.chapter_title,
           it.shot != null ? `镜${it.shot}` : null].filter(Boolean).join(" / "))),
      h("span", { class: "pal-field" }, it.field || ""));
    row.addEventListener("mousemove", () => {
      if (PAL.sel !== i) { PAL.sel = i; paintPalSel(false); }
    });
    body.append(row);
  });
}
function paintPalSel(scroll = true) {
  [...$("#pal-body").children].forEach((el, i) =>
    el.classList.toggle("sel", i === PAL.sel));
  if (scroll) $("#pal-body").children[PAL.sel]?.scrollIntoView({ block: "nearest" });
}
async function palQuery(q) {
  if (q === PAL.last) return;
  PAL.last = q;
  if (!q.trim()) { PAL.items = []; PAL.sel = 0; renderPal(); return; }
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (q !== $("#pal-input").value) return;   // 过期响应丢弃（快速连打时只认最新）
    PAL.items = r.items || []; PAL.sel = 0; renderPal();
  } catch { /* 输入过程中的检索失败不打扰 */ }
}
function palGo(i) {
  const it = PAL.items[i];
  if (!it) return;
  closePalette();
  location.hash = it.href;
  if (it.type === "shot") flashShot(it.shot);
}
function flashShot(id, tries = 24) {
  const el = document.getElementById(`shot-${id}`);
  if (el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("flash");
    void el.offsetWidth;               // 重启动画
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1900);
    return;
  }
  if (tries > 0) setTimeout(() => flashShot(id, tries - 1), 125);   // 等章节视图渲染完
}
$("#btn-search").addEventListener("click", openPalette);
document.querySelectorAll("[data-close=pal]").forEach((e) =>
  e.addEventListener("click", closePalette));
$("#pal-input").addEventListener("input", (e) => {
  clearTimeout(PAL.timer);
  PAL.timer = setTimeout(() => palQuery(e.target.value), 160);
});
$("#pal-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") {
    e.preventDefault();
    PAL.sel = Math.min(PAL.sel + 1, Math.max(PAL.items.length - 1, 0));
    paintPalSel();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    PAL.sel = Math.max(PAL.sel - 1, 0);
    paintPalSel();
  } else if (e.key === "Enter") {
    palGo(PAL.sel);
  } else if (e.key === "Escape") {
    closePalette();
  }
  e.stopPropagation();
});

/* ---------------- 启动 ---------------- */
(async function boot() {
  // 法律声明先绑：它是 AGPL 第 5(d) 条的合规件，**不能取决于 /api/overview 是否成功**
  //（后端挂了、配置坏了都要照样能查到许可证），故排在首屏取数之前。
  bindLegalNotice();
  try {
    const ov = await getOverview();
    renderRail(ov);
  } catch { /* 首屏失败时视图层会给出错误块 */ }
  render();
})();

/* —— 模块导出 —— */
export { PAL, PAL_TAG, closePalette, emptyBlock, flashShot, openPalette, paintPalSel, palGo,
         palQuery, render, renderPal, routes, stageDispose, startPoll, stopPoll, viewStage };
