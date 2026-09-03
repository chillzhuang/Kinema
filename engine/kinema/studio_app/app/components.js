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

/* ═ Studio 前端模块 · app/components.js — 站内 UI 组件单一落位（原生 ES Module·免构建）═
   `docs/agents/director-stage-ui.md` ①「只用站内组件」纪律的锚点：
   下拉 uiSelect · 勾选 uiCheck · 检索 listSearch · 徽章 chip · 确认/输入框 uiDialog/uiConfirm · 弹层骨架 openShell ·
   指令弹层 openDirectiveDialog。
   分层纪律：core（基座·零静态依赖）→ components（仅依赖 core 的 h/toast）→ 视图。
   改组件观感只动本文件与 style.css，全站生效。 */
import { h, rich, toast } from "./core.js";

const chip = (text, kind) => h("span", { class: "chip" + (kind ? " " + kind : "") }, text);

function uiSelect(options, { value, placeholder } = {}) {
  let cur = value != null ? value : (options[0] && options[0].value);
  let menu = null;
  const label = () => (options.find((o) => o.value === cur) || {}).label
    || placeholder || "请选择";
  const btnTxt = h("span", { class: "us-label" }, label());
  const close = () => {
    menu?.remove(); menu = null;
    document.removeEventListener("click", close);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", close);
    document.removeEventListener("keydown", onEsc);
  };
  const onEsc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  // 页面滚动收起菜单；菜单内部滚动不算（否则滚菜单即误关）
  const onScroll = (e) => { if (menu && !menu.contains(e.target)) close(); };
  const open = () => {
    const rc = btn.getBoundingClientRect();
    menu = h("div", { class: "us-menu", role: "listbox" });
    options.forEach((o) => menu.append(
      h("button", { class: "us-opt" + (o.value === cur ? " on" : ""), type: "button",
        onclick: (e) => { e.stopPropagation(); cur = o.value;
          btnTxt.textContent = label(); close();
          root.dispatchEvent(new Event("change")); } }, o.label)));
    // 菜单内滚轮自消化，不穿透页面（overscroll-behavior 之外的兜底）
    menu.addEventListener("wheel", (e) => {
      const canScroll = menu.scrollHeight > menu.clientHeight;
      if (!canScroll) { e.preventDefault(); return; }
      const atTop = menu.scrollTop <= 0 && e.deltaY < 0;
      const atEnd = menu.scrollTop + menu.clientHeight >= menu.scrollHeight && e.deltaY > 0;
      if (atTop || atEnd) e.preventDefault();
    }, { passive: false });
    document.body.append(menu);
    const below = window.innerHeight - rc.bottom - 12;
    const maxH = Math.min(300, Math.max(below, rc.top - 12));
    menu.style.maxHeight = `${maxH}px`;
    menu.style.minWidth = `${rc.width}px`;
    // 横向也要夹在视口内：菜单比触发钮宽时（选项文案长）左对齐会溢出右缘被裁。
    // 侧栏形态下控件离右缘远，这个缺陷碰不到；顶栏形态把控件推到最右后必现。
    const mw = Math.max(menu.offsetWidth, rc.width);
    menu.style.left = `${Math.max(8, Math.min(rc.left, window.innerWidth - mw - 8))}px`;
    if (below >= Math.min(menu.scrollHeight, maxH)) {
      menu.style.top = `${rc.bottom + 4}px`;
    } else {                                    // 下方放不下 → 上翻
      menu.style.bottom = `${window.innerHeight - rc.top + 4}px`;
    }
    setTimeout(() => {
      document.addEventListener("click", close);
      window.addEventListener("scroll", onScroll, true);
      window.addEventListener("resize", close);
      document.addEventListener("keydown", onEsc);
    }, 0);
  };
  const btn = h("button", { class: "us-btn", type: "button",
    onclick: (e) => { e.stopPropagation(); menu ? close() : open(); } },
    btnTxt, h("i", { class: "us-caret" }, "▾"));
  const root = h("div", { class: "us" }, btn);
  Object.defineProperty(root, "value", { get: () => cur,
    set: (v) => { cur = v; btnTxt.textContent = label(); } });
  return root;
}

/* 系统风格勾选（替代原生 checkbox）：checked 属性 + change 事件对齐原生用法 */

function uiCheck() {
  const root = h("button", { class: "us-check", type: "button",
    onclick: (e) => { e.preventDefault(); root.classList.toggle("on");
      root.dispatchEvent(new Event("change")); } }, h("i", null, "✓"));
  Object.defineProperty(root, "checked", {
    get: () => root.classList.contains("on"),
    set: (v) => root.classList.toggle("on", !!v) });
  return root;
}

/* 分段控件（胶囊分段）：`.value` 读写 + change 事件，与 uiSelect / uiCheck 同一套用法。
   options 是 [值, 文案] 二元组数组。三个消费者共用：新建项目的比例与字幕语言、
   顶栏的外壳形态开关——第三处出现时抽出来，样式与交互只此一份。 */

function uiSegment(options, { value } = {}) {
  let cur = value != null ? value : (options[0] && options[0][0]);
  const btns = options.map(([val, label]) => h("button", { class: "usegb", type: "button",
    onclick: () => {
      if (cur === val) return;                   // 点当前档不该空放一次 change
      cur = val; paint();
      root.dispatchEvent(new Event("change"));
    } }, label));
  const paint = () => btns.forEach((b, i) => b.classList.toggle("on", options[i][0] === cur));
  const root = h("div", { class: "useg" }, ...btns);
  paint();
  Object.defineProperty(root, "value", {
    get: () => cur, set: (v) => { cur = v; paint(); } });
  return root;
}

/* 列表快速检索框：按 textContent 过滤 itemsSel 元素（零重渲、输入即筛、清空即还原） */

function listSearch(itemsSel, placeholder) {
  return h("input", { class: "fsearch", type: "search", placeholder,
    oninput: (e) => {
      const kw = e.target.value.trim().toLowerCase();
      document.querySelectorAll(itemsSel).forEach((el) => {
        el.hidden = !!kw && !el.textContent.toLowerCase().includes(kw);
      });
    } });
}

function uiDialog({ title, message, input = null, extra = null, danger = false,
                    confirmText = "确认", cancelText = "取消" }) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (val) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey, true);
      overlay.classList.add("closing");
      setTimeout(() => overlay.remove(), 140);
      resolve(val);
    };
    const ok = () => done(input ? inp.value : true);
    const cancel = () => done(input ? null : false);
    const onKey = (e) => {
      if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); cancel(); }
      else if (e.key === "Enter") {
        if (e.target.tagName === "BUTTON") return;   // 焦点在按钮上时交给原生 click
        e.stopPropagation(); e.preventDefault(); ok();
      }
    };
    const inp = input && h("input", { class: "cmt-input", type: "text",
      value: input.value || "", placeholder: input.placeholder || "" });
    const okBtn = h("button", { class: "dlg-btn " + (danger ? "danger" : "primary"),
      onclick: ok }, confirmText);
    const overlay = h("div", { class: "dlg" },
      h("div", { class: "dlg-backdrop", onclick: danger ? null : cancel }),
      h("div", { class: "dlg-card" },
        h("span", { class: "k" }, title),
        message && h("p", { class: "dlg-msg" }, rich(message)),
        inp,
        extra,
        h("div", { class: "dlg-acts" },
          h("button", { class: "dlg-btn", onclick: cancel }, cancelText), okBtn)));
    document.body.append(overlay);
    document.addEventListener("keydown", onKey, true);
    setTimeout(() => (inp || okBtn).focus(), 30);
  });
}

const uiConfirm = (message, { danger = false, title = "请确认" } = {}) =>
  uiDialog({ title, message, danger, confirmText: danger ? "确认执行" : "确认" });
/* 统一弹层（uiConfirm / uiPrompt）：取代原生 confirm/prompt——全站操作确认同一语言。
   遮罩+居中卡片 · ESC=取消 · Enter=确认 · 自动聚焦输入框/确认钮；
   危险操作确认钮红色且点遮罩不关闭（防误触）；进出场 150ms 克制动画。 */
const uiPrompt = (message, { value = "", placeholder = "", title = "请输入" } = {}) =>
  uiDialog({ title, message, input: { value, placeholder } });

/* 弹层骨架工厂：`.dlg` 家族的唯一开启入口——backdrop 点击关闭、Escape 捕获关闭、
   closing 退场动画三件套只写这一份——逐弹层各手搓一遍，改一次动效就要追全部文件。
   build(close) 返回卡片子节点数组；onClose 在关闭发起时调用（软刷新等收尾钩子）；
   Promise 型弹层（如转场选择器）在宿主侧包装：按钮先记结果再 close()，onClose 里
   resolve——Escape/backdrop 走同一条 close，结果保持缺省值即「取消」语义。 */
function openShell({ card = "", build, onClose = null, backdropClose = true,
                    keys = null }) {
  let closed = false;
  let overlay;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    overlay.classList.add("closing");
    setTimeout(() => overlay.remove(), 140);
    if (onClose) onClose();
  };
  const onKey = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); }
    else if (keys) keys(e, close);
  };
  overlay = h("div", { class: "dlg" },
    h("div", { class: "dlg-backdrop", onclick: backdropClose ? close : null }),
    h("div", { class: "dlg-card" + (card ? " " + card : "") }, ...build(close)));
  document.body.append(overlay);
  document.addEventListener("keydown", onKey, true);
  return close;
}

/* —— 模块导出 —— */
/* 按钮忙态：点下去就转圈、禁掉同组按钮，收尾时按需还原。

   为什么值得收成一个组件：这类"点了要等几秒"的按钮全站十几个（选定音色要同步全部
   章节、试音要 5 条 TTS、按新垫图重出…），各写各的
   `btn.disabled = true; btn.textContent = "…"` 必然出两类通病——
     · **还原文案写死**：「↻ 重新试音」失败后被还原成「♪ 生成 5 个试音」（文案错位）；
     · **只禁自己**：等待期间还能点同一张卡里的另一个「选定」，连发两次请求。
   `group` 给一个祖先选择器，整组一起禁，松开时一起放开。 */
/* 还不还原的判据是**按钮还在不在文档里**，不是调用方的一个开关：随后重渲的
   调用点，按钮已被替换掉（`isConnected` 为假），没有可还原的东西；没重渲的调用点
   按钮还在原地，就必须还原——否则它永远转圈且永远点不动，只能刷新整页。
   做成开关的话，漏传一次就是一个死钮，而且不报任何错（定制生成与自检都这么死过）。 */
function runBusy(btn, busyText, fn, { group = null } = {}) {
  const prevHtml = btn.innerHTML, prevDis = btn.disabled;
  const peers = group
    ? [...(btn.closest(group)?.querySelectorAll("button") || [])].filter((b) => b !== btn)
    : [];
  const peerState = peers.map((b) => b.disabled);
  btn.disabled = true;
  btn.classList.add("is-busy");
  btn.textContent = busyText;      // 转圈由 .is-busy::before 画，不插子节点
  peers.forEach((b) => (b.disabled = true));
  const settle = () => {
    if (!btn.isConnected) return;
    btn.classList.remove("is-busy");
    btn.innerHTML = prevHtml;
    btn.disabled = prevDis;
    peers.forEach((b, i) => (b.disabled = peerState[i]));
  };
  return Promise.resolve()
    .then(fn)
    .then((v) => { settle(); return v; })
    .catch((e) => { settle(); throw e; });
}

/* ═══ 指令弹层 openDirectiveDialog — 全站「把指令交给 AI」的唯一交互 ═══
   为什么不做「一点即复制」：这类按钮拼出的都是**半成品模板**，正文里留着
   `<在此填写>` 这样的槽——用户复制完还得切到对话框里找到那一行、删掉尖括号、
   把诉求敲进去。两种失败模式：① 占位符未替换，「<在此写打磨方向>」原样提交给
   AI；② 绕过按钮手打指令，缺定位坐标，AI 需要全仓检索目标文件。
   本弹层的形态：点开即见指令全文，需求在弹层里写，**合并后**才进剪贴板。

   参数：
     title     中文标题（如「改镜指令」）           code   等宽眉标（缺省 DIRECTIVE）
     meta      副行定位（如「镜 03 · 项目 x / 章节 y」）
     directive 基础指令（**不含需求行**，末行由组件统一补）
     ask       需求行占位词，渲染成 `需求：<在此写打磨方向>`（缺省「在此写你的需求」）
     hint      textarea 的 placeholder（写给用户看的例子）
     note      脚注（可选，补一句落地纪律）
     done      复制成功的 toast 文案

   **需求行由组件唯一生成，调用点不许自己写**（`DQ_NEED` + `<ask>`）——一旦留两条
   路（指令自带槽走就地替换、没槽的追加成末尾【我的需求】块），同一个弹层就会
   在不同按钮下长两个样（一处行内琥珀高亮、一处底部整块面板）；
   而且「槽文案」与「指令正文」分两处写，改一处忘一处就
   静默漂移。单路生成下：全站十七个弹层结构逐像素一致，
   槽与正文不可能对不上——因为正文里根本没有槽。
   需求留空 = 复制「基础指令 + 需求：<占位>」原样模板（占位留给用户自己填）。 */
const DQ_NEED = "需求：";
const dqBase = (directive) => String(directive || "").replace(/\s+$/, "");
const dqSlot = (ask) => `<${ask || "在此写你的需求"}>`;
function dqMerge(directive, ask, need) {
  const t = (need || "").trim();
  return `${dqBase(directive)}\n${DQ_NEED}${t || dqSlot(ask)}`;
}

function openDirectiveDialog({ title, code = "DIRECTIVE", meta = "", directive,
                               ask = "", hint = "", note = "", done = "" }) {
  const base = dqBase(directive);
  const slot = dqSlot(ask);
  const ta = h("textarea", { class: "dq-ta", rows: "4", spellcheck: "false",
    placeholder: hint || "写下这次的具体要求——留空则原样复制上面的模板" });
  const pre = h("div", { class: "dq-pre" });
  const stat = h("span", { class: "dq-stat mono" });
  const go = h("button", { class: "dlg-btn primary dq-go" }, "⧉ 复制指令");

  /* 实时预览：需求落在哪一行、合并后长什么样，一眼看得见（琥珀高亮那块就是你写的）。
     只读展示不给编辑——指令主干是带定位坐标的纪律文本，能改反而会被改坏。
     需求行恒是最后一行，正对着下方「你的需求」输入框，一路顺下来不用跳读。 */
  const paint = () => {
    const t = ta.value.trim();
    const mark = h("mark", { class: "dq-slot" + (t ? " on" : "") }, t || slot);
    pre.textContent = "";
    pre.append(base, "\n", DQ_NEED, mark);
    stat.textContent = `${dqMerge(base, ask, ta.value).length.toLocaleString()} 字`;
    // 长指令里需求行在折叠区外时，键入却看不见落点等于没预览——移出视野才追一下
    const top = mark.offsetTop, bot = top + mark.offsetHeight;
    if (top < pre.scrollTop || bot > pre.scrollTop + pre.clientHeight)
      pre.scrollTop = Math.max(0, top - (pre.clientHeight - mark.offsetHeight) / 2);
  };
  ta.addEventListener("input", paint);

  let copied = false;
  const sec = (n, zh, en) => h("div", { class: "dq-seck" },
    h("i", { class: "mono" }, n), h("b", null, zh),
    h("span", { class: "mono" }, en), h("span", { class: "dq-rule" }));
  const close = openShell({ card: "dq-card", backdropClose: true,
    keys: (e, kclose) => {                      // ⌘/Ctrl+Enter = 复制并关闭
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault(); e.stopPropagation(); copy(kclose);
      }
    },
    build: (kclose) => [
      h("div", { class: "dq-hd" },
        h("span", { class: "dq-eyebrow mono" }, code),
        h("h3", null, title),
        meta && h("p", { class: "dq-meta" }, meta),
        h("button", { class: "dq-x", type: "button", onclick: () => kclose(),
          dataset: { tip: "关闭（Esc）" } }, "✕")),
      h("div", { class: "dq-body" },
        sec("01", "指令原文", "BASE"),
        pre,
        sec("02", "你的需求", "YOUR BRIEF"),
        ta,
        h("p", { class: "dq-note" }, note
          || "写下的要求会填进上面末行「需求：」的位置；留空则原样复制模板。")),
      h("div", { class: "dq-ft" },
        stat,
        h("div", { class: "dq-ft-acts" },
          h("button", { class: "dlg-btn", type: "button", onclick: () => kclose() }, "取消"),
          go)),
    ] });
  const copy = async (kclose) => {
    if (copied) return;
    try { await navigator.clipboard.writeText(dqMerge(base, ask, ta.value)); }
    catch { toast("复制失败：浏览器未授权剪贴板", true); return; }
    copied = true;
    // 手指还在按钮上，反馈得落在按下去的那个点——收起前先亮一下「已复制」
    go.classList.add("ok");
    go.textContent = "✓ 已复制";
    toast(done || `${title}已复制——粘给 AI 即可`);
    setTimeout(() => kclose(), 460);
  };
  go.onclick = () => copy(close);
  paint();
  setTimeout(() => ta.focus(), 60);
  return close;
}

export { chip, uiSelect, uiCheck, uiSegment, listSearch, uiDialog, uiConfirm, uiPrompt,
         openShell, openDirectiveDialog, runBusy };
