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

/* ═ Studio 前端模块 · app/shell.js — 白标 · 导航外壳（原生 ES Module·免构建）═ */

/* ---------------- 白标：config/branding.yaml → 名称/口号/主题色 ---------------- */
import { $ } from "./core.js";
import { h, state } from "./core.js";
import { uiSegment } from "./components.js";

function applyBrand(brand) {
  if (!brand) return;
  const bt = document.querySelector(".brand-txt");
  if (bt) {
    bt.querySelector("b").textContent = brand.name || "Kinema";
    bt.querySelector("i").textContent = brand.tagline || "PRODUCTION STUDIO";
  }
  document.title = `BladeX · ${brand.name || "kinema"}`;
  if (brand.accent) {
    const r = document.documentElement.style;
    r.setProperty("--amber", brand.accent);
    r.setProperty("--amber-deep", brand.accent);
    const n = parseInt(brand.accent.slice(1), 16);
    r.setProperty("--amber-soft",
      `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, .13)`);
  }
}

/* ---------------- 侧栏 ---------------- */
/* 项目树的展开态：**模块级内存，刻意不进 localStorage**。
   模块每次页面加载求值一次，于是「刷新即全部折叠」；而软刷新（renderRail 重入）
   不会丢展开态。若展开态持久化且缺省全展开，
   项目一多，侧栏一进来就是一长串章节，得先手动收一遍才看得见别的项目。 */
const railOpen = Object.create(null);
try { localStorage.removeItem("rail-fold"); } catch { /* 隐私模式下不可写，忽略 */ }

function renderRail(ov) {
  applyBrand(ov.brand);
  const qb = $("#queue-badge");
  const pending = ov.stats?.pending || 0;
  // 待审徽标：有数=醒目琥珀圆标（居中计数），无数=与其他导航一致的 EN 小标——不留空
  qb.textContent = pending ? String(pending) : "QUEUE";
  qb.classList.toggle("has", pending > 0);
  qb.title = pending ? `${pending} 项待审` : "待审队列为空";
  qb.hidden = false;
  // 项目树：检索框 + 每项目可折叠（localStorage 持久）+ 多项目滚动（CSS overflow）
  const tree = $("#rail-tree");
  tree.innerHTML = "";
  const list = h("div", { class: "tree-list" });
  const paint = () => {
    const kw = (state.railKw || "").toLowerCase();
    list.innerHTML = "";
    for (const p of ov.projects || []) {
      const pHit = !kw || (p.title || p.id).toLowerCase().includes(kw)
        || p.id.toLowerCase().includes(kw);
      const chs = (p.chapters || []).filter((c) =>
        pHit || (c.title || c.id).toLowerCase().includes(kw));
      if (!pHit && !chs.length) continue;
      const isFold = !kw && !railOpen[p.id];         // 缺省折叠；检索命中时强制展开
      const proj = h("div", { class: "tree-proj" + (isFold ? " closed" : "") },
        h("a", { href: `#/project/${encodeURIComponent(p.id)}`, dataset: { pid: p.id } },
          h("button", { class: "tp-caret", title: isFold ? "展开章节" : "折叠章节",
              onclick: (e) => { e.preventDefault(); e.stopPropagation();
                railOpen[p.id] = !railOpen[p.id]; paint(); } }, "▸"),
          h("span", { class: "tp-title" }, p.title || p.id),
          h("i", { class: "tp-cnt" }, String((p.chapters || []).length))),
        !isFold && h("div", { class: "tree-chapters" }, chs.map((c) =>
          h("a", { class: "tree-ch",
                   href: `#/project/${encodeURIComponent(p.id)}/${encodeURIComponent(c.id)}`,
                   dataset: { pid: p.id, cid: c.id } },
            h("span", { class: "dot " + (c.status || "") }),
            h("span", { class: "tc-title" }, c.title || c.id)))));
      list.append(proj);
    }
    if (!list.children.length)
      list.append(h("div", { class: "tree-empty" }, "无匹配项目"));
    syncRailActive();
  };
  tree.append(
    // 浮层只装「项目树」这一件事：导航项自己就是 `#/projects` 的链接（两种形态
    // 同一行为），浮层不另设「全部项目」入口——同一个目的地只留一个落点。
    h("input", { class: "rail-search", type: "search",
      placeholder: "筛选项目 / 章节", value: state.railKw || "",
      oninput: (e) => { state.railKw = e.target.value.trim(); paint(); } }), list);
  paint();
}

function syncRailActive() {
  const r = state.route || {};
  document.querySelectorAll(".nav-item").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === r.name));
  document.querySelectorAll(".tree-proj > a").forEach((a) =>
    a.classList.toggle("active", r.name === "project" && a.dataset.pid === r.pid));
  document.querySelectorAll(".tree-ch").forEach((a) =>
    a.classList.toggle("active",
      r.name === "chapter" && a.dataset.pid === r.pid && a.dataset.cid === r.cid));
}

function setCrumbs(parts) {
  const c = $("#crumbs");
  c.innerHTML = "";
  parts.forEach(([text, href], i) => {
    if (i) c.append(h("span", { class: "sep" }, "/"));
    c.append(href ? h("a", { href }, text) : h("span", { class: "here" }, text));
  });
}

/* ---------------- 法律声明（AGPL v3 第 5(d) 条要求的 Appropriate Legal Notices） ----------------
   协议第 0 条把这项义务定义为「便捷且显眼地」展示四件事：版权声明 · 无担保 ·
   可按本协议分发 · 如何查看协议全文。侧栏页脚的一行签是这个"显眼条目"，点开即四件齐备。
   **绝不能省**——AGPL 下缺这块声明，交互式界面本身就不合规。 */
function bindLegalNotice() {
  const btn = $("#rail-legal");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.onclick = async () => {
    const { uiDialog } = await import("./components.js");
    await uiDialog({
      title: "许可证与法律声明",
      /* 三段式：产品签 → 协议正文 → 商业授权，只靠两道发丝线与留白分层。
         **全篇仅一处加粗**（不附带任何担保——免责是这份声明里唯一的要害）；
         色块、码片、彩色链接一概不用：法律声明要的是克制与可读，不是层次感。 */
      extra: h("div", { class: "legal-note" },
        h("div", { class: "legal-head" },
          h("div", { class: "legal-title" },
            h("span", { class: "legal-brand" }, "KINEMA"),
            h("span", { class: "legal-tag" }, "全能的影像创作智能体")),
          h("div", { class: "legal-copy" }, "Copyright (C) 2018-2099 BladeX")),
        h("div", { class: "legal-body" },
          h("div", {}, "本程序为自由软件。你可依据自由软件基金会颁布的 ",
            "GNU Affero 通用公共许可证（AGPL）第 3 版，或任择其后的更新版本，",
            "对本程序进行再分发与修改。"),
          /* 免责句照 FSF 原句结构走法律语域：`in the hope that it will be useful`
             取「希望其能有所助益」（表意愿而非承诺），`implied warranty` 是民法典
             口径的「默示担保」而非坊间的「隐含担保」，分号也保留原句的断法。
             FSF 只认英文原文有法律效力，故末句显式让位给许可证全文。 */
          h("div", {}, "本程序的分发是希望其能有所助益，但", h("b", {}, "不附带任何担保"),
            "；亦不含对适销性或特定用途适用性的默示担保。一切以许可证全文为准。"),
          /* 与下方商业授权同一句式：一句引子以冒号收，链接另起一行 */
          h("div", { class: "legal-pair" },
            h("div", {}, "许可证全文见仓库根目录的 ", h("code", {}, "LICENSE"), " 文件，或参阅："),
            h("div", {},
              h("a", { href: "https://www.gnu.org/licenses/agpl-3.0.html",
                       target: "_blank", rel: "noopener" }, "gnu.org/licenses/agpl-3.0")))),
        h("div", { class: "legal-comm" },
          h("div", { class: "k" }, "商业授权"),
          /* 「闭源」这两个字不能省：AGPL 下开源合规的商用本就无需再授权，
             写成「商业交付一律需授权」与 README 许可证节的口径自相矛盾。 */
          h("div", {}, "个人学习、研究与评估免费，闭源商用需另行取得商业授权："),
          /* 联系方式与上面 gnu.org 那条同为正文链接，不另设字体与色阶 */
          h("div", {},
            h("a", { href: "https://bladex.cn", target: "_blank", rel: "noopener" },
              "bladex.cn"),
            h("span", { class: "sep" }, "／"),
            h("a", { href: "mailto:bladejava@qq.com" }, "bladejava@qq.com")))),
      confirmText: "知道了",
      cancelText: "关闭",
    });
  };
}

/* ---------------- 外壳形态：左侧栏 ⇄ 顶部导航 ----------------
   形态只是 <html data-shell> 一个属性加两个 CSS 让位令牌，**DOM 一个节点都不重排**：
   全站有四处按 id/class 直抓 rail 子节点（app.js 绑 .rail-brand、本文件取
   #rail-tree 与 #queue-badge、syncRailActive 的三条 querySelectorAll），重排它们
   不会报错，只会静默失效。首帧前的赋值在 index.html 的启动脚本里。 */
const SHELL_KEY = "kn-shell";
const NARROW = window.matchMedia("(max-width: 900px)");
let laySeg = null;

/* 缺省顶栏：判据是「显式存了 side 才用 side」，未表过态（含隐私模式读不到）一律顶栏。
   **必须与 index.html 首帧引导脚本同一句式**——那段脚本先按自己的默认画第一帧，
   这里再算一次；两处默认值分叉的表现不是报错，而是每次刷新闪一下形态。
   表过态就一直算数：切换时写 localStorage（见 mountShellSwitch），关掉浏览器再来照旧。 */
const shellPref = () => {
  try { return localStorage.getItem(SHELL_KEY) === "side" ? "side" : "top"; }
  catch { return "top"; }         // 隐私模式下不可读
};
/* 窄屏恒顶栏：900px 以下侧栏本来就摆不下，若把侧栏整条 display:none，
   五个主菜单与项目树会一并不可达——强制顶栏形态保住全部导航入口。 */
const effectiveShell = () => (NARROW.matches ? "top" : shellPref());

function applyShell(mode) {
  document.documentElement.dataset.shell = mode;
  if (mode !== "top") closeTree();          // 浮层只属于顶栏形态
  if (laySeg && laySeg.value !== mode) laySeg.value = mode;
  // 剧本工作台的两栏高度是 JS 实测距顶算出来的（project.js 的 fit），只在 resize 时重算；
  // 形态切换不触发 resize，不补这一下，页面会多出或少掉一条导航条的高度。
  window.dispatchEvent(new Event("resize"));
}

function mountShellSwitch() {
  const host = $("#lay-seg");
  if (!host || host.dataset.bound) return;
  host.dataset.bound = "1";
  laySeg = uiSegment([["side", "▤ 左栏"], ["top", "▬ 顶栏"]], { value: shellPref() });
  laySeg.classList.add("useg-sm");
  laySeg.addEventListener("change", () => {
    try { localStorage.setItem(SHELL_KEY, laySeg.value); } catch { /* 隐私模式下不可写 */ }
    applyShell(effectiveShell());
  });
  host.append(laySeg);
}

const treeOpen = () => !!$("#rail")?.classList.contains("tree-open");
const closeTree = () => $("#rail")?.classList.remove("tree-open");

/* 浮层左缘跟随「项目」导航项并夹在视口内——导航条可横滑，这个位置不是常量 */
function placeTree() {
  const anchor = document.querySelector(".nav-item.nav-sec");
  const tree = $("#rail-tree");
  if (!anchor || !tree) return;
  const r = anchor.getBoundingClientRect();
  const w = tree.offsetWidth || 360;
  tree.style.left = `${Math.max(12, Math.min(r.left, window.innerWidth - w - 12))}px`;
}

function toggleTree() {
  const rail = $("#rail");
  if (!rail) return;
  const open = !rail.classList.contains("tree-open");
  rail.classList.toggle("tree-open", open);
  if (!open) return;
  placeTree();
  // 浮层是按需展开的，展开时不同步一次高亮，用户就永远看不出"我正在这一章"
  syncRailActive();
}

let hoverTimer = null;
const topShell = () => document.documentElement.dataset.shell === "top";

function bindTreeFlyout() {
  const sec = document.querySelector(".nav-item.nav-sec");
  const tree = $("#rail-tree");
  if (!sec || sec.dataset.bound) return;
  sec.dataset.bound = "1";
  // 「项目」**两种形态都是普通导航项**：点击 = 跳 `#/projects`（原生 <a> 行为，
  // 这里一个字都不拦）。顶栏形态只是额外在 hover 时展开项目树浮层——浮层是加法，
  // 不改变点击语义。若顶栏下 preventDefault 掉跳转、只开浮层，代价有三：
  // 五个主菜单里唯独它点了没反应、`#/projects` 得在浮层里另开一个落点、
  // 无 hover 的设备根本够不着这一栏。跳转后 hashchange 会自动收起浮层。
  // 悬停即展开。离开要留一段宽限：菜单项与浮层之间隔着一条边框，指针经过那道缝时
  // 两边的 mouseleave 都会触发，不缓冲就会闪一下关掉，用户根本够不到浮层。
  const enter = () => {
    clearTimeout(hoverTimer);
    if (topShell() && !treeOpen()) toggleTree();
  };
  const leave = () => {
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => {
      if (!document.querySelector(".nav-item.nav-sec:hover, .rail-tree:hover")) closeTree();
    }, 220);
  };
  [sec, tree].forEach((el) => {
    if (!el) return;
    el.addEventListener("mouseenter", enter);
    el.addEventListener("mouseleave", leave);
  });
  document.addEventListener("click", (e) => {
    if (!treeOpen()) return;
    if (e.target.closest(".rail-tree, .nav-item.nav-sec")) return;
    closeTree();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeTree(); });
  window.addEventListener("hashchange", closeTree);      // 选了章节即收起
  window.addEventListener("resize", () => { if (treeOpen()) placeTree(); });
}

mountShellSwitch();
bindTreeFlyout();
applyShell(effectiveShell());
NARROW.addEventListener("change", () => applyShell(effectiveShell()));

/* —— 模块导出 —— */
export { applyBrand, applyShell, bindLegalNotice, effectiveShell, renderRail, setCrumbs,
         syncRailActive };
