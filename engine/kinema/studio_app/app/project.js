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

/* ═ Studio 前端模块 · app/project.js — 视图：项目详情 · 剧本工作台（原生 ES Module·免构建）═ */

/* ---------------- 视图：项目详情 ---------------- */
/* ═══════════ 剧本工作台（viewScript · #/project/<id>/script）═══════════
   项目页「剧本」概览卡点进：源文本 + 拆书圣经 + 分集 + 设定登记表 + 正文分段查看器
   + 拆书指令台交 Claude Code。全部改编工作在此页完成。 */
import { chip, openDirectiveDialog, runBusy, uiCheck, uiConfirm, uiPrompt,
         uiSelect } from "./components.js";
import { $ } from "./core.js";
import { emptyBlock, render, routes } from "../app.js";
import { CSRF, ICON, LABEL, api, fmtCost, fmtDur, getOverview, h, post,
         softRefreshProject, state, toast } from "./core.js";
import { audioPill, charInfo, motionBadge, openLightbox, profileChip, secHeader, skillChip,
         statusPill, titledChip } from "./widgets.js";
import { renderRail, setCrumbs } from "./shell.js";

import { deletedBanner } from "./project-new.js";
import { PANE_ICON, kgGraph } from "./chapter.js";
import { assetVerBadge } from "./panels.js";
import { exportCard } from "./ledger.js";

function sourceUpload(pid) {
  const uploadSource = async (f) => {
    if (!f) return;
    if (f.size > 20 * 1024 * 1024) { toast("文件超过 20MB 上限", true); return; }
    toast("上传入库中…");
    try {
      const qs = new URLSearchParams({ project: pid, name: f.name }).toString();
      const res = await fetch(`/api/adapt/upload?${qs}`,
        { method: "POST", headers: { "X-Csrf-Token": CSRF }, body: f });
      const r = await res.json();
      if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
      toast(`✓ 已入库：${r.kind === "screenplay" ? "剧本" : "小说"}${r.encoding === "epub" ? "（EPUB）" : ""} · `
        + `${(r.chars || 0).toLocaleString()} 字 · 切分 ${r.n_segments} 段——回对话让 AI 拆书分集`);
      await getOverview(true); render();
    } catch (err) { toast(err.message, true); }
  };
  const pickFile = () => {
    const fin = h("input", { type: "file",
      accept: ".txt,.fountain,.fdx,.spmd,.md,.epub,text/plain,application/epub+zip", style: "display:none" });
    fin.addEventListener("change", () => {
      const f = fin.files && fin.files[0]; fin.remove(); uploadSource(f);
    });
    document.body.append(fin); fin.click();
  };
  return { uploadSource, pickFile };
}

function scriptDropzone(uploadSource, pickFile) {
  const drop = h("div", { class: "adapt-drop", tabindex: "0", role: "button",
    onclick: pickFile,
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickFile(); } } },
    h("div", { class: "adapt-drop-ico",
      html: '<svg viewBox="0 0 24 24"><path d="M12 15.5V4M12 4 7.5 8.5M12 4l4.5 4.5'
        + 'M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15"/></svg>' }),
    h("div", { class: "adapt-drop-title" }, "上传完整小说 / 剧本入库"),
    h("div", { class: "adapt-drop-sub" }, "点击选择，或把 .txt / .epub / .fountain / .fdx 文件拖到这里"),
    h("div", { class: "adapt-drop-hint" },
      "上传后引擎自动结构切分，再回对话让 AI 拆书分集、一键建本集"));
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  drop.addEventListener("dragover", (e) => { stop(e); drop.classList.add("drag"); });
  drop.addEventListener("dragenter", (e) => { stop(e); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", (e) => { stop(e); drop.classList.remove("drag"); });
  drop.addEventListener("drop", (e) => {
    stop(e); drop.classList.remove("drag");
    uploadSource(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
  });
  return drop;
}

function entCard(e, kind, pid) {
  const isChar = kind === "character";
  const isScene = kind === "scene";   // 具名场景（取景地）
  const sub = isChar ? (e.appearance || "") : (e.desc || "");
  const badge = isChar ? (e.role || "角色")
    : isScene ? "场景" : (e.kind === "weapon" ? "武器" : "道具");
  // 传 actx → 灯箱获得与 ch01 分镜图相同的 ↻重生成/◉提意见/✂局部改造（后端已泛化，零改）
  const akind = isChar ? "character" : isScene ? "scene" : "prop";
  const open = () => openLightbox([{ src: e.sheet,
    title: (isChar ? "CHARACTER · " : isScene ? "SCENE · " : "PROP · ") + (e.name || ""),
    caption: isChar ? charInfo(e) : (e.desc || ""),
    actx: pid ? { pid, kind: akind, name: e.name, comments: e.comments || [] } : undefined }]);
  return h("div", { class: "ent-card" },
    e.sheet
      ? h("figure", { class: "ent-thumb", onclick: open },
          h("img", { src: e.sheet, alt: e.name, loading: "lazy" }))
      : h("div", { class: "ent-thumb ent-nothumb" }, "无设定图"),
    h("div", { class: "ent-body" },
      h("div", { class: "ent-name" }, e.name || "—",
        h("span", { class: "ent-badge" + (isChar ? "" : isScene ? " scene" : " prop") }, badge)),
      sub ? h("p", { class: "ent-sub" }, sub) : null,
      isChar && e.voice ? h("span", { class: "ent-voice" }, "♪ " + e.voice) : null));
}

/* 万位缩写（数据带大数字要一眼读得出量级：1,196,040 → 119.6万）；返回 h() 子节点数组 */
const fmtWan = (n) => (n || 0) >= 10000
  ? [(n / 10000).toFixed(1).replace(/\.0$/, ""), h("small", null, "万")]
  : [(n || 0).toLocaleString()];

function segLabel(s) {
  if (s.type === "novel") return s.title || `第 ${s.index} 章`;
  return s.type === "scene"
    ? [s.int_ext, s.location].filter(Boolean).join(". ") + (s.time_of_day ? " · " + s.time_of_day : "")
    : (s.title || `第 ${s.index} 段`);
}

/* 章级分段（原创章稿 novel / 小说源切分 chapter）——目录序号列与章扉眉线已带章号，
   标题里再念一遍「第N章」就重了；场景段（screenplay）不适用 */
const isChapterSeg = (s) => s.type === "novel" || s.type === "chapter";
const bareTitle = (s) => {
  const t = segLabel(s) || "";
  if (!isChapterSeg(s)) return t;
  const bare = t.replace(/^第\s*[\d零一二三四五六七八九十百千]+\s*[章回][·\s：:—-]*/, "").trim();
  return bare || t;
};

/* 原创小说 → 阅读器/目录所需的「段」形状。两栏阅读工作台的原生输入是改编项目的
   source/segments.json，而原创项目没有源、章稿全在 project.json 的 novel 块里——
   不做映射，几百章只能挤在创作 Tab 的手风琴列表里。这里把章稿映射成同一副形状（index=章号），
   正文改走 /api/novel/chapter，两栏读法对原创同样成立。 */
function novelSegs(d) {
  const chs = ((d.novel || {}).chapters || []);
  if (d.source || !chs.length) return [];               // 有源仍以源目录为准
  return chs.slice().sort((a, b) => (a.no || 0) - (b.no || 0))
    .map((c) => ({ index: c.no, title: c.title || "", chars: c.chars || 0, type: "novel" }));
}

/* 正文阅读器（▤ 正文 tab）：一次一章按段懒加载 + 上/下一章 + 段落式高级排版。
   独立滚动容器（.reader-scroll，overscroll:contain，不牵动左目录/页面）；翻章/点目录
   触发 opts.onNavigate（进阅读模式）。返回 { panel, scroll, loadByIndex, loadFirst, setNavHook }。 */
function buildReader(segs, pid, opts = {}) {
  const indices = segs.map((s) => s.index);
  // 取正文的那一下是可替换的：改编走 source 分段，原创走 novel 章稿（同一副 seg 形状）
  const fetchSeg = opts.fetch
    || ((s) => api(`/api/script/segment?id=${encodeURIComponent(pid)}&index=${s.index}`));
  const truncNote = opts.truncNote || "…… 单段过长已截断，完整正文见 source/raw.txt";
  const titleEl = h("div", { class: "reader-title" }, "从左侧目录选择章节");
  const metaEl = h("span", { class: "reader-meta" });
  const prevB = h("button", { class: "reader-nav-btn", disabled: true }, "‹ 上一章");
  const nextB = h("button", { class: "reader-nav-btn", disabled: true }, "下一章 ›");
  const body = h("article", { class: "reader-body" });          // 段落容器（居中窄栏）
  // 篇末翻章：读完顺手翻，不必滚回顶部（与顶部按钮同一对 load 走线）
  const prevF = h("button", { class: "reader-nav-btn", disabled: true }, "‹ 上一章");
  const nextF = h("button", { class: "reader-nav-btn", disabled: true }, "下一章 ›");
  const footMid = h("span", { class: "reader-foot-mid" });
  const foot = h("div", { class: "reader-foot", hidden: true }, prevF, footMid, nextF);
  const scroll = h("div", { class: "reader-scroll" }, body, foot);    // 独立滚动区
  const head = h("div", { class: "reader-head" },
    h("div", { class: "reader-head-l" }, titleEl, metaEl),
    h("div", { class: "reader-nav" }, prevB, nextB));
  const panel = h("div", { class: "reader" }, head, scroll);
  // 章扉：编号眉线 + 衬线章题——章号已上眉线，标题里再带「第N章」就重了，剥掉
  const chapHead = (s, tt) => h("header", { class: "reader-chap" },
    h("div", { class: "reader-chap-no" },
      isChapterSeg(s) ? `第 ${s.index} 章` : `SECTION ${String(s.index).padStart(2, "0")}`),
    h("h2", { class: "reader-chap-t" }, isChapterSeg(s) ? bareTitle(s) : tt),
    h("hr", { class: "reader-chap-rule" }));
  let pos = -1, tok = 0, onSel = () => {};
  const note = (t) => { body.innerHTML = ""; body.append(h("p", { class: "reader-note" }, t)); };
  // 原创正文存的是 markdown（`# 第N章 · 标题` 抬头行、`---` 分隔线、`**加粗**` 面板/强调）。
  // 按纯文本渲染会把星号与横线原样印在页面上，所以只认这三样做**最小**解析，其余照旧不动
  // ——不引 markdown 库、不拼 innerHTML（正文是用户自己的文件，仍按 DOM 节点构造）。
  const mdLine = (line) => line.split("**")
    .map((seg, i) => (seg ? (i % 2 ? h("b", null, seg) : seg) : null)).filter(Boolean);
  const paint = (r, tt, s) => {
    if (r.note) { note(r.note); return; }
    let lines = (r.text || "").split("\n");
    let i = 0; while (i < lines.length && !lines[i].trim()) i++;   // 跳前导空行
    // 去正文里与抬头重复的标题行：显示名 tt 经 segLabel 重排过，故并比原始 heading/title
    const cands = [tt, r.title, r.heading].filter(Boolean).map((x) => x.trim());
    const first = i < lines.length ? lines[i].trim() : "";
    if (first && (cands.includes(first) || (opts.md && /^#{1,4}\s/.test(first))))
      lines = lines.slice(i + 1);
    const paras = lines.map((l) => l.trim()).filter(Boolean);
    if (!paras.length) { note("（此段为空）"); return; }
    body.innerHTML = "";
    body.append(chapHead(s, tt));
    paras.forEach((p) => {
      if (opts.md && /^(-{3,}|\*{3,}|_{3,})$/.test(p)) { body.append(h("hr", { class: "reader-hr" })); return; }
      body.append(opts.md ? h("p", { class: "reader-p" }, ...mdLine(p)) : h("p", { class: "reader-p" }, p));
    });
    if (r.seg_truncated) body.append(h("p", { class: "reader-note" }, truncNote));
    foot.hidden = false;
  };
  const load = async (p, nav) => {
    if (p < 0 || p >= segs.length) return;
    pos = p;
    const s = segs[p];
    const tt = segLabel(s) || `第 ${s.index} 段`;
    titleEl.textContent = tt;
    metaEl.textContent = (s.chars ? s.chars.toLocaleString() + " 字 · " : "") + `${p + 1} / ${segs.length}`;
    prevB.disabled = prevF.disabled = p <= 0;
    nextB.disabled = nextF.disabled = p >= segs.length - 1;
    footMid.textContent = `${p + 1} / ${segs.length}`;
    onSel(s.index);
    if (nav && opts.onNavigate) opts.onNavigate();       // 翻章/点目录 → 进阅读模式（第4/8点）
    const my = ++tok;
    foot.hidden = true;                                  // 载入/失败态不给翻章脚条
    note("载入正文…"); scroll.scrollTop = 0;
    try {
      const r = await fetchSeg(s);
      if (my !== tok) return;                            // 快速翻章防竞态：只认最后一次
      paint(r, tt, s);
    } catch (e) { if (my === tok) note("载入失败：" + (e.message || e)); }
  };
  prevB.addEventListener("click", () => load(pos - 1, true));
  nextB.addEventListener("click", () => load(pos + 1, true));
  prevF.addEventListener("click", () => load(pos - 1, true));
  nextF.addEventListener("click", () => load(pos + 1, true));
  return {
    panel, scroll,
    loadByIndex: (idx) => { const p = indices.indexOf(idx); if (p >= 0) load(p, true); },
    loadFirst: () => { if (segs.length) load(0, false); },
    setNavHook: (fn) => { onSel = fn; },
  };
}

/* 左栏章节目录树：点击 → onSelect(index)。返回 { node, setActive } 供阅读器反向高亮。
   groups（可选，原创长篇的卷纲 arcs）给出时按卷分组：卷头吸顶、正在写的卷点亮；
   不属于任何卷的章收进「未入卷」尾组——三百多章的平铺列表从此有了书的脊柱。 */
function scriptToc(segs, onSelect, groups) {
  const kchars = (n) => n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  const rows = segs.map((s) => h("div", { class: "stoc-item", tabindex: "0", role: "button",
    dataset: { seg: String(s.index) },
    onclick: () => onSelect(s.index),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(s.index); } } },
    h("span", { class: "stoc-idx" }, String(s.index).padStart(2, "0")),
    h("span", { class: "stoc-title" }, bareTitle(s) || "（无标题）"),   // 序号列已带章号，标题剥前缀
    s.chars ? h("span", { class: "stoc-meta" }, kchars(s.chars)) : null));
  let nodes = rows;
  const vols = [];                                 // { head, members: rows 下标 }——filter 联动卷头显隐
  if ((groups || []).length) {
    const used = new Set(); nodes = [];
    const volHead = (no, t, m, now) => h("div", { class: "stoc-vol" + (now ? " now" : "") },
      h("span", { class: "stoc-vol-no" }, no),
      h("span", { class: "stoc-vol-t" }, t),
      h("span", { class: "stoc-vol-m" }, m));
    groups.slice().sort((a, b) => (a.from || 0) - (b.from || 0)).forEach((g) => {
      const members = [];
      segs.forEach((s, i) => {
        if (used.has(i) || s.index < (g.from || 1) || s.index > (g.to ?? Infinity)) return;
        used.add(i); members.push(i);
      });
      if (!members.length) return;                 // 未开写的空卷不占目录
      const head = volHead("卷 " + String(g.no).padStart(2, "0"), g.title || "（未命名）",
        `${g.from}~${g.to ?? "?"}`, g.state === "writing");
      vols.push({ head, members });
      nodes.push(head, ...members.map((i) => rows[i]));
    });
    const rest = segs.map((_, i) => i).filter((i) => !used.has(i));
    if (rest.length) {
      if (vols.length) {
        const head = volHead("——", "未入卷", String(rest.length), false);
        vols.push({ head, members: rest });
        nodes.push(head);
      }
      nodes.push(...rest.map((i) => rows[i]));
    }
  }
  const setActive = (idx) => {
    let hit = null;
    rows.forEach((el) => { const on = el.dataset.seg === String(idx); el.classList.toggle("on", on); if (on) hit = el; });
    if (hit) hit.scrollIntoView({ block: "nearest" });
  };
  const labels = segs.map((s) => (segLabel(s) || "").toLowerCase());
  const filter = (q) => {                          // 按标题/序号过滤目录
    q = (q || "").trim().toLowerCase();
    let shown = 0;
    rows.forEach((el, i) => {
      const hit = !q || labels[i].includes(q) || String(segs[i].index).includes(q);
      el.hidden = !hit; if (hit) shown++;
    });
    vols.forEach((v) => { v.head.hidden = !v.members.some((i) => !rows[i].hidden); });
    return shown;
  };
  return { node: h("div", { class: "script-toc" }, ...nodes), setActive, filter };
}

/* AI 问书：内联输入 → 生成「据原文作答」指令复制给 Claude Code（引擎无 LLM，问答在对话侧跑）。 */
function scriptAsk(pid, title) {
  const input = h("input", { class: "ask-input", type: "text",
    placeholder: "问这本书任何问题：人物关系 / 伏笔 / 爽点分布 / 某角色动机…" });
  const gen = async () => {
    const qv = input.value.trim();
    if (!qv) { input.focus(); return; }
    const cmd = `关于「${title}」原著回答：${qv}\n`
      + `读 project/${pid}/source/raw.txt（借 project/${pid}/source/segments.json 目录定位相关章节），据原文作答。`;
    try { await navigator.clipboard.writeText(cmd); toast("问书指令已复制——粘给 AI 即可回答"); }
    catch { toast("复制失败：浏览器未授权剪贴板", true); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") gen(); });
  const panel = h("div", { class: "ask-panel", hidden: true }, input,
    h("button", { class: "act-btn sm", onclick: gen }, "⧉ 问书指令"));
  return { panel, toggle: () => { panel.hidden = !panel.hidden; if (!panel.hidden) input.focus(); } };
}

/* 划词抽实体：在正文阅读器（.reader-body）里选中文本 → 浮出工具条 → 开指令台，
   写下这次的抽取侧重（可留空）后连同选中原文一起复制。
   事件挂在右栏容器上，随视图重渲染自然回收（不泄漏 document 级监听）。 */
function scriptSelectTools(container, pid, title) {
  let picked = "";
  const bar = h("div", { class: "sel-tools", hidden: true },
    ...[["角色", "character"], ["道具", "prop"], ["场景", "scene"]].map(([zh, kind]) =>
      h("button", { class: "sel-tool", onclick: () => {
        if (!picked) return;
        bar.hidden = true;
        openDirectiveDialog({
          title: `抽${zh}指令`, code: "SCRIPT · EXTRACT",
          ask: `在此写抽${zh}的侧重`,
          meta: `项目 ${pid} · 选中 ${picked.length} 字原文`,
          hint: `例：只抽出场超过一次的${zh}；连别名与绰号一并记进 keyword`,
          done: `抽${zh}指令已复制——粘给 AI`,
          directive: `为「${title}」从这段原文抽取${zh}实体：\n"""\n${picked}\n"""\n`
            + `按 kinema-project SKILL「3.5 剧本改编模式」步骤③识别${zh}（外貌/关键词/别名），`
            + `疑似与已有实体同一先问我确认、不自动合并；产候选 JSON 后 `
            + `adapt merge-entities ${pid} --file 候选.json 合并入库（不覆盖人工字段）。`,
        });
      } }, "⧉ 抽为" + zh)));
  container.append(bar);
  const hide = () => { bar.hidden = true; };
  container.addEventListener("mouseup", () => setTimeout(() => {
    const sel = window.getSelection();
    const txt = sel ? sel.toString().trim() : "";
    let node = sel && sel.rangeCount ? sel.getRangeAt(0).commonAncestorContainer : null;
    if (node && node.nodeType === 3) node = node.parentElement;
    if (txt.length >= 2 && node && node.closest && node.closest(".reader-body")) {
      picked = txt.slice(0, 2000);
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      bar.style.top = Math.max(8, rect.top - 42) + "px";
      bar.style.left = Math.min(window.innerWidth - 232, Math.max(8, rect.left)) + "px";
      bar.hidden = false;
    } else { hide(); }
  }, 10));
  // 浮条固定、不因滚动消失：不挂 scroll 隐藏，只在「点到浮条外」时收起
  // （点空白/目录/其它 tab 都算）。捕获相 document mousedown，先于按钮 click；容器移除后自摘。
  const onDown = (e) => {
    if (!container.isConnected) { document.removeEventListener("mousedown", onDown, true); return; }
    if (!bar.contains(e.target)) hide();
  };
  document.addEventListener("mousedown", onDown, true);
}

/* 集级对照：左「本集要讲」(episodes 规划字段) / 右「拆了哪些镜」(fetch 章节 shots)。
   源↔镜无字符级映射（source_range 为自由文本），故按集粗粒度对照——回答「拆镜是否忠于本集」。 */
async function renderEpisodeCompare(host, pid, ep) {
  host.innerHTML = ""; host.hidden = false;
  host.append(h("div", { class: "loading" }, "载入对照…"));
  let ch = null;
  try { ch = await api(`/api/chapter?project=${encodeURIComponent(pid)}&id=${encodeURIComponent(ep.chapter_id)}`); }
  catch { /* 章节可能被删/未建，右栏给空态 */ }
  host.innerHTML = "";
  const left = h("div", { class: "cmp-col" },
    h("div", { class: "cmp-k" }, "原文侧 · 本集要讲"),
    ep.logline ? h("p", { class: "cmp-log" }, ep.logline) : null,
    h("div", { class: "adapt-hooks" },
      ep.open_hook && h("span", null, "↗ " + ep.open_hook),
      ep.core_event && h("span", null, "◆ " + ep.core_event),
      ep.cool_point && h("span", null, "✦ " + ep.cool_point),
      ep.end_hook && h("span", { class: "hook-end" }, "⛓ " + ep.end_hook)),
    ep.source_range ? h("p", { class: "cmp-src" }, "原文范围 · " + ep.source_range) : null);
  const shots = (ch && ch.shots) || [];
  const right = h("div", { class: "cmp-col" },
    h("div", { class: "cmp-k" }, `分镜侧 · ${shots.length} 镜`),
    shots.length
      ? h("div", { class: "cmp-shots" }, shots.map((sh, i) => h("div", { class: "cmp-shot" },
          h("span", { class: "cmp-no" }, "#" + (sh.id != null ? sh.id : i + 1)),
          h("div", { class: "cmp-body" },
            (sh.camera || sh.framing) ? h("span", { class: "cmp-cam" }, sh.camera || sh.framing) : null,
            h("p", null, sh.narration || sh.caption || "（无台词）")))))
      : emptyBlock("本章尚无分镜", "回对话让 AI 按本集大纲拆 shots。", null));
  host.append(h("div", { class: "cmp-wrap" },
    h("button", { class: "cmp-close", onclick: () => { host.hidden = true; host.innerHTML = ""; },
      dataset: { tip: "收起对照" } }, "×"),
    left, right));
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function viewScript(view, pid) {
  let d;
  try { d = await api(`/api/script?id=${encodeURIComponent(pid)}`); }
  catch { setCrumbs([["总览", "#/"], ["剧本"]]); view.append(emptyBlock("找不到项目", "该项目可能已删除。", null)); return; }
  setCrumbs([["总览", "#/"], [d.title || pid, `#/project/${encodeURIComponent(pid)}`], ["剧本"]]);
  const title = d.title || pid;
  let secNo = 0;
  const no = () => String(++secNo).padStart(2, "0");
  const eps = d.episodes || [];
  const chapIds = new Set((d.chapters || []).map((c) => c.id));
  const builtCount = (d.chapters || []).length;              // 已建章数（清空硬闸）
  const pubCount = (d.chapters || []).filter((c) => c.video).length;   // 已出片章数
  const KIND = { novel: "小说", screenplay: "剧本", outline: "大纲" };
  const { uploadSource, pickFile } = sourceUpload(pid);

  // 清空源文本按钮：仅剧本创作初期（尚未建章）可用；已进入制作期「软禁用」——
  // 不用 disabled（会吞掉悬浮事件导致 data-tip 不弹），改灰化 + 悬浮说明 + 点击给 toast
  const clearBlockMsg = `已建 ${builtCount} 章${pubCount ? `、${pubCount} 章已出片` : ""}，`
    + "项目已进入制作期，无法清空源文本——如需推倒重来请先删除章节或删除项目";
  const clearSourceBtn = () => builtCount
    ? h("button", { class: "act-btn sm no soft-off", dataset: { tip: clearBlockMsg },
        onclick: () => toast(clearBlockMsg, true) }, "✕ 清空资源")
    : h("button", { class: "act-btn sm no",
        dataset: { tip: "删除源文本与结构目录，项目退回未入库态（仅剧本创作初期可用）" },
        onclick: async () => {
          if (!(await uiConfirm(
            `清空「${title}」的源文本？\n将删除 raw.txt 与结构目录（segments），`
            + "项目退回未入库的剧本创作初期态。\n（拆书 / 分集草稿保留；此操作不可撤销）",
            { danger: true, title: "清空源文本" }))) return;
          try {
            await post("/api/adapt/clear", { project: pid });
            toast("已清空源文本——项目退回未入库态");
            await getOverview(true); render();
          } catch (err) { toast(err.message, true); }
        } }, "✕ 清空资源");

  // 「未入库」只对**改编**项目成立（等着上传源文本）。原创小说自始至终没有源，
  // 却被这枚 chip 说成「未入库」，读起来像「章稿没进库所以看不到」——实际章稿在
  // project.json 的 novel 块里、创作区就在下面。有章稿无源时改标「原创」。
  const KINDCHIP = () => d.source
    ? chip(KIND[d.source.kind] || d.source.kind, "cyan")
    : (((d.novel || {}).chapters || []).length
        ? chip("原创 · 无源", "amber") : chip("未入库"));
  const mkReup = () => h("button", { class: "act-btn sm adapt-reup", onclick: pickFile,
    dataset: { tip: "重新上传替换正文（覆盖 raw.txt 与结构切分）" } }, "↻ 重新上传");
  const copyBriefBtn = () => h("button", { class: "mast-act",
    dataset: { tip: "开指令台：写下拆书侧重，与「读源文本继续拆书/分集/建章」的标准指令"
      + "合并后复制给 AI" },
    onclick: () => openDirectiveDialog({
      title: "拆书指令", code: "ADAPT · BRIEF", meta: `「${title}」· 项目 ${pid}`,
      ask: "在此写拆书侧重",
      hint: "例：主线只留复仇这一条，感情线压成暗线；每集结尾都要留钩子",
      done: "拆书指令已复制——粘贴给 AI 即可",
      directive: `继续「${title}」的剧本改编：读 project/${pid}/source/raw.txt`
        + ` 与 project/${pid}/source/segments.json，按 kinema-project SKILL「3.5 剧本改编模式」`
        + `拆书写 adaptation、分集写 episodes（按原著章节一一对应：一章=一集，绝不合并章节）、`
        + `再 adapt scaffold 建章。`,
    }) }, "⧉ 拆书指令");
  // 问书：问题写在指令台里、合并后才复制——模板留尖括号回头再改的话，
  // 占位文本容易被原样提交
  const askChip = () => d.source ? h("button", { class: "mast-act",
    dataset: { tip: "开指令台：写下你的问题，与「据原文作答」的定位指令合并后复制给 AI" },
    onclick: () => openDirectiveDialog({
      title: "问书指令", code: "SCRIPT · ASK", meta: `「${title}」· 项目 ${pid}`,
      ask: "在此写你的问题",
      hint: "问这本书任何问题：人物关系 / 伏笔 / 爽点分布 / 某角色的动机…",
      done: "问书指令已复制——粘给 AI 即可回答",
      directive: `关于「${title}」原著回答我的问题：\n`
        + `读 project/${pid}/source/raw.txt（借 project/${pid}/source/segments.json 目录定位相关章节），据原文作答。`,
    }) }, "⧉ 问书指令") : null;
  // 让 Claude Code 自创（无源）/ 扩写续写（有源）正文 → 开指令台附诉求后复制
  const createBtn = () => h("button", { class: "mast-act",
    dataset: { tip: d.source ? "开指令台：写下扩写方向，合并「扩写/续写正文后重新入库」的指令给 AI"
                             : "开指令台：写下创作方向，合并「原创正文并入库」的指令给 AI（无需现成文件）" },
    onclick: () => openDirectiveDialog({
      title: d.source ? "扩写正文指令" : "自创正文指令",
      code: d.source ? "SOURCE · EXPAND" : "SOURCE · ORIGINAL",
      meta: `「${title}」· 项目 ${pid}`,
      ask: d.source ? "在此写扩写方向" : "在此写创作方向",
      hint: d.source ? "例：从第 12 章往后续三章，把配角的支线补完，每章 3000 字左右"
                     : "例：先写 6 章、每章 3000 字；双男主，节奏快、每章末尾留钩子",
      done: d.source ? "扩写指令已复制——粘给 AI" : "自创指令已复制——粘给 AI",
      directive: d.source
        ? `扩写/续写「${title}」：读 project/${pid}/source/raw.txt 与 segments.json，`
          + `依世界观与既有情节扩写/续写章节，产出完整正文 .txt 后 `
          + `adapt import ${pid} --file <新正文.txt> 重新入库、再拆书分集。`
        : `为「${title}」原创正文：按项目主题${d.theme ? `（${d.theme}）` : ""}创作完整多章小说/剧本，`
          + `写成 UTF-8 .txt 后 adapt import ${pid} --file <正文.txt> 入库，再按 3.5 剧本改编模式拆书分集。`,
    }) }, d.source ? "⧉ 扩写正文" : "✎ 自创正文");

  // ── 统一空态：拆书 / 分集 / 设定 / 图谱四模块同款空态；
  //    涉及 AI 联动的给「指令」按钮（需先入库源文本），否则只给引导文案。
  const paneEmpty = (iconKey, title, desc, action) =>
    h("div", { class: "pane-empty" },
      h("div", { class: "pane-empty-ic", html: PANE_ICON[iconKey] || "" }),
      h("h3", null, title),
      h("p", null, desc),
      action || null);
  /* 空态指令钮：点开指令台（写诉求 → 与标准指令合并 → 复制），不做「一点即复制」。
     opts 透传给 openDirectiveDialog（code / hint / meta …）。 */
  const copyInstrBtn = (label, tip, cmd, opts = {}) => h("button", { class: "pane-empty-act",
    dataset: { tip },
    onclick: () => openDirectiveDialog({
      title: label, meta: `「${title}」· 项目 ${pid}`, directive: cmd,
      done: `${label}已复制——粘贴给 AI 即可`, ...opts,
    }) }, h("span", { class: "pane-empty-act-ic", html: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5a1.5 1.5 0 0 0-1.5-1.5H3.5A1.5 1.5 0 0 0 2 3.5V9a1.5 1.5 0 0 0 1.5 1.5h2"/></svg>' }),
    label);
  const bibleInstr = () => copyInstrBtn("拆书指令",
    "开指令台：让 AI 读源文本，提炼主线/贯穿冲突/世界观宪法/爽点写入 adaptation",
    `拆书「${title}」：读 project/${pid}/source/raw.txt（借 project/${pid}/source/segments.json 目录定位），`
    + `提炼主线 / 贯穿冲突 / 世界观宪法 / 爽点 / 名场面，按 kinema-project SKILL「3.5 剧本改编模式」写入 adaptation。`,
    { code: "ADAPT · BIBLE", ask: "在此写拆书侧重",
      hint: "例：主线按「复仇—夺权—和解」三段走；世界观只保留修真体系，删掉现代线" });
  const epsInstr = () => copyInstrBtn("分集指令",
    "开指令台：让 AI 按原著章节一一对应分集（一章=一集）写入 episodes[]，再回来一键建本集",
    `分集「${title}」：读 project/${pid}/source/raw.txt 与 project/${pid}/source/segments.json`
    + `（有 adaptation 则一并参考），按原著章节一一对应切集——一章 = 一集 = 一个视频章节，`
    + `绝不合并章节（多章压一集会丢关键情节与设定）；每集从本章正文取材写 logline / 开场钩子 / `
    + `核心事件 / 爽点 / 结尾钩子，写入 episodes[]，再 adapt scaffold ${pid} 建章。`,
    { code: "ADAPT · EPISODES", ask: "在此写分集要求",
      hint: "例：每集 60 秒左右；第 1 集开场钩子直接用主角被逐出师门那一幕" });
  const entInstr = () => copyInstrBtn("设定指令",
    "开指令台：让 AI 从原文抽取角色 / 道具 / 场景写入设定集",
    `抽设定「${title}」：读 project/${pid}/source/raw.txt（借 project/${pid}/source/segments.json 目录定位），`
    + `抽取主要角色（外貌 / 服装 / 发型 / 武器）与关键道具 / 场景，用 character add / prop add 写入设定集，`
    + `疑似同名/别名先停下问我、不自动合并。`,
    { code: "ADAPT · ENTITIES", ask: "在此写抽设定的侧重",
      hint: "例：只抽出场三次以上的角色；道具重点抽法宝与信物，场景按门派分组" });

  // ── 右栏内容构造：源文本 / 拆书 / 分集(+集级对照) / 设定 / 创作
  const ad = d.adaptation || {};
  const chars = d.characters || [], props = d.props || [];
  const scenes = d.scenes || [];              // 具名场景（取景地）——设定 Tab 同渲染
  const segs = d.segments || [];
  const nvCount = ((d.novel || {}).chapters || []).length;   // 原创章稿数（创作 Tab）

  const buildSource = () => {
    if (!d.source) return scriptDropzone(uploadSource, pickFile);
    const S = d.source;
    const row = (k, v) => v ? h("div", { class: "src-row" },
      h("span", { class: "src-k" }, k), h("span", { class: "src-v" }, v)) : null;
    return h("div", { class: "src-info" },
      row("类型", KIND[S.kind] || S.kind || "—"),
      row("字数", (S.chars || 0).toLocaleString() + " 字"),
      row("文件", S.file || ""),
      row("入库", (S.ingested_at || "").slice(0, 16).replace("T", " ") || "—"),
      row("指纹", S.sha256 || ""));
  };

  const buildBible = () => {
    const ADF = [["mainline", "主线", "MAINLINE"], ["core_conflict", "贯穿冲突", "CONFLICT"],
                 ["world_bible", "世界观宪法", "WORLD BIBLE"], ["cut_unit", "分集单位", "CUT UNIT"]];
    if (!Object.keys(ad).length)
      return paneEmpty("bible", "尚未拆书",
        d.source
          ? "让 AI 读源文本，提炼主线 / 贯穿冲突 / 世界观宪法 / 爽点与名场面，落成这部作品的「圣经」。"
          : "先上传源文本入库，再让 AI 拆书提炼主线 / 冲突 / 世界观宪法。",
        d.source ? bibleInstr() : null);
    // 长文字段一律整行铺开 + 报纸式分栏（column-width 保证每栏行宽可读）；
    // 正文走 pre-wrap——拆书文本用 \n 分节（【一·…】【二·…】），塌成一坨就没法读了。
    // 爽点/名场面是整句而非短标签，**绝不能用 .chip**：nowrap 会把句子横向裁断。
    const bibleCell = (zh, en, body, extraCls) =>
      h("div", { class: "design-cell wide bible-cell" + (extraCls ? " " + extraCls : "") },
        h("span", { class: "k" }, `${zh} · ${en}`), body);
    // 契约里 cool_points/set_pieces 是**字符串数组**，可手写字段常被写成一整段带
    // ①②③ 的文本。**`x && x.length` 拦不住字符串**（字符串也有 length），于是穿过守卫
    // 在 .map 上抛 "items.map is not a function"，整个剧本工作台变成「加载失败」——
    // 一处数据违约白掉整页。故渲染前一律归一成数组（顺带剥掉行首自带的序号，
    // 否则会和下面 padStart 出来的 01/02 撞成「01 ① …」）。
    const asLines = (v) => Array.isArray(v)
      ? v.filter((x) => typeof x === "string" && x.trim())
      : typeof v === "string"
        ? v.split(/\r?\n+/)
           .map((s) => s.trim().replace(/^(?:[①-⑳]|\d+\s*[.、)]|[-–—·•])\s*/, ""))
           .filter(Boolean)
        : [];
    const longList = (items, tone) =>
      h("div", { class: "bible-list" }, items.map((c, i) =>
        h("div", { class: "bible-li " + tone },
          h("span", { class: "bible-li-n" }, String(i + 1).padStart(2, "0")),
          h("span", { class: "bible-li-t" }, c))));
    const cps = asLines(ad.cool_points), sps = asLines(ad.set_pieces);
    return h("div", { class: "design-grid bible-grid" },
      ADF.filter(([k]) => ad[k]).map(([k, zh, en]) =>
        bibleCell(zh, en, h("p", { class: "bible-prose" }, ad[k]))),
      cps.length ? bibleCell("爽点", "COOL POINTS", longList(cps, "amber")) : null,
      sps.length ? bibleCell("名场面", "SET PIECES", longList(sps, "cyan")) : null);
  };

  const buildEpisodes = () => {
    if (!eps.length)
      return paneEmpty("eps", "尚无分集大纲",
        d.source
          ? "让 AI 按剧情节拍把原著切分为若干集（每集一个钩子 + 核心事件 + 爽点），再回这里一键建本集。"
          : "先上传源文本入库，再让 AI 按剧情节拍分集，之后一键建本集。",
        d.source ? epsInstr() : null);
    const cmpHost = h("div", { class: "cmp-host", hidden: true });
    const anyUnbuilt = eps.some((e) => !(e.chapter_id && chapIds.has(e.chapter_id)));
    const doScaffold = async (ev, only) => {
      const b = ev.currentTarget, was = b.textContent;
      b.disabled = true; b.textContent = "建章中…";
      try {
        const r = await post("/api/adapt/scaffold", only ? { project: pid, only } : { project: pid });
        const s = r.scaffold || {};
        toast(`建章完成：新建 ${(s.created || []).length} · 刷新 ${(s.updated || []).length}`
          + ((s.warned || []).length ? ` · ⚠ ${s.warned.length} 章已拆镜需核对` : ""));
        render();
      } catch (err) { toast(err.message, true); b.disabled = false; b.textContent = was; }
    };
    return h("div", { class: "eps-wrap" },
      h("div", { class: "adapt-eps" }, eps.map((e) => {
        const built = e.chapter_id && chapIds.has(e.chapter_id);
        return h("div", { class: "adapt-ep" + (built ? " built" : "") },
          h("div", { class: "adapt-ep-hd" },
            h("span", { class: "adapt-no" }, "EP" + String(e.no).padStart(2, "0")),
            h("b", null, e.title || `第${e.no}集`),
            built
              ? h("a", { class: "chip green",
                  href: `#/project/${encodeURIComponent(pid)}/${encodeURIComponent(e.chapter_id)}` },
                  "已建 " + e.chapter_id + " →")
              : h("button", { class: "act-btn sm", onclick: (ev) => doScaffold(ev, [e.no]) }, "建本集")),
          e.logline && h("p", { class: "adapt-log" }, e.logline),
          h("div", { class: "adapt-hooks" },
            e.open_hook && h("span", null, "↗ " + e.open_hook),
            e.core_event && h("span", null, "◆ " + e.core_event),
            e.cool_point && h("span", null, "✦ " + e.cool_point),
            e.end_hook && h("span", { class: "hook-end" }, "⛓ " + e.end_hook)),
          built ? h("button", { class: "cmp-btn", onclick: () => renderEpisodeCompare(cmpHost, pid, e),
            dataset: { tip: "原文侧 ↔ 分镜侧 对照本集" } }, "⇄ 对照") : null);
      })),
      cmpHost,
      h("div", { style: "margin-top:12px" },
        h("button", { class: "act-btn ok", disabled: !anyUnbuilt,
          onclick: (ev) => doScaffold(ev, null) }, anyUnbuilt ? "全部建 / 更新本集" : "全部已建")));
  };

  const buildEntities = () => (chars.length || props.length || scenes.length)
    ? h("div", { class: "ent-grid" },
        ...chars.map((c) => entCard(c, "character", pid)),
        ...scenes.map((sc) => entCard(sc, "scene", pid)),
        ...props.map((pr) => entCard(pr, "prop", pid)))
    : paneEmpty("ent", "尚无设定",
        d.source
          ? "让 AI 从原文抽取主要角色 / 道具 / 场景写入设定集（正文里划词也可抽）——设定图一致性的根基。"
          : "先上传源文本入库，再让 AI 抽取角色 / 道具 / 场景设定。",
        d.source ? entInstr() : null);

  // ── ✎ 创作（原创小说层）：进度里程碑 + 文风契约 + 章节稿 + 伏笔账本。
  //    创作智能全在指挥层（kinema-novel SKILL 每章五步闭环）——本 Tab 是
  //    登记态的**驾驶舱**：读引擎回填的 novel/threads/narrative_style，写路径只有
  //    伏笔记账（回收/弃置，/api/novel/thread），其余一律「复制指令给 Claude Code」。
  const buildNovel = () => {
    const nv = d.novel || {};
    const nvCh = (nv.chapters || []).slice();
    const nvT = nv.threads || { open: [], paid: [], dropped: [], expired: [], current_no: 0 };
    const st = nv.narrative_style || {};
    const every = nv.milestone_every || 10;
    const nextNo = (nvT.current_no || 0) + 1;
    const batchTo = (nv.next_checkpoint || every);
    const writeInstr = `续写「${title}」第 ${nextNo}~${batchTo} 章（原创小说·kinema-novel SKILL 批次协议）：`
      + `每章先 novel brief ${pid} 取写前必读包（文风契约/宪法相关节/当前卷纲与节拍/上章状态/未回收伏笔/在场角色人设卡），`
      + `写正文前读 references/craft.md 按一章四拍搭 → 过 风格·人设·连贯 三门自检 → `
      + `novel save ${pid} --no N --file 正文.md --digest … --state … 登记 → `
      + `回写 character set / prop set / scene set 等设定变更（新 NPC 记得带 --keyword 绰号）。`
      + `批次内不要逐章问我；写满第 ${batchTo} 章即进检查点（七门复核 + 批次报告落盘 + novel log 留痕），`
      + `报告给我之后停下等指令。这一轮上下文写不完就明说写到第几章、我回「继续」。`;
    const ckInstr = `执行「${title}」检查点（kinema-novel SKILL 七门复核，判据整份读 references/checkpoint.md）：`
      + `① 取料 novel recap ${pid} + novel lint ${pid} → `
      + `② 七门逐门判，结论用 pass|fail|unverified 三态并给证据出处：合宪(world_bible/卷纲) · `
      + `人设(speech_style 归属盲测) · 连贯(state 按七类扫) · AI 味(口癖/带区/复读/markdown) · `
      + `文风(对 baseline 与 z 分) · 伏笔清账 · 节奏(payoff 间隔) → `
      + `③ 设定对账回写（character set / prop set / scene set / adapt graph / novel bible）→ `
      + `④《批次报告》先落盘 project/${pid}/plan/batch-N-M.md 再 novel log ${pid} --kind checkpoint --ref 它，`
      + `然后贴给我并停下问是否继续。`;
    const startInstr = `为「${title}」开始原创小说：先 novel init ${pid} 与我确认文风契约 narrative_style`
      + `（pov/tense/voice/diction + baseline 基线样本 2~3 段）与卷纲（novel arc ${pid} --no 1 …），`
      + `登记主要角色（character add + character set 补 speech_style 台词口吻与 taboo_lines 行为禁区），`
      + `再按 kinema-novel SKILL 批次协议写第一批并在批次末出《批次报告》。`;
    if (!nvCh.length)
      return paneEmpty("write", "尚未开始创作",
        "原创小说从这里起步：文风契约 → 角色口吻 → 每章五步闭环（写前读状态 · 三门自检 · 登记回写）。"
        + "引擎管登记与体检，创作交给 AI。",
        copyInstrBtn("开写指令", "开指令台：让 AI 初始化文风契约并写第 1 章", startInstr,
          { code: "NOVEL · KICKOFF", ask: "在此写开篇方向",
            hint: "例：第一人称、冷硬短句；开篇直接进冲突，别铺设定" }));

    const payThread = async (t) => {
      const v = await uiPrompt(`「${t.title}」在第几章回收？`,
        { title: "标记伏笔回收", placeholder: "回收章号", value: String(nvT.current_no || "") });
      if (v == null || !String(v).trim()) return;
      const inNo = parseInt(v, 10);
      if (!Number.isFinite(inNo) || inNo < 1) { toast("回收章号须为正整数", true); return; }
      try {
        await post("/api/novel/thread", { project: pid, tid: t.id, status: "paid", paid_in: inNo });
        toast(`已记账：${t.id} 第 ${inNo} 章回收`); await getOverview(true); render();
      } catch (err) { toast(err.message, true); }
    };
    const dropThread = async (t) => {
      if (!(await uiConfirm(`弃置伏笔「${t.title}」？\n记录在案不再追讨（记错了可让 AI 重新登记）。`,
        { title: "弃置伏笔" }))) return;
      try {
        await post("/api/novel/thread", { project: pid, tid: t.id, status: "dropped" });
        toast(`已弃置 ${t.id}`); await getOverview(true); render();
      } catch (err) { toast(err.message, true); }
    };
    const threadRow = (t) => {
      const expired = !!t.expired;
      const meta = `埋于第 ${t.setup} 章`
        + (t.due ? ` · 期限第 ${t.due} 章` : " · 无期限")
        + (t.status === "paid" ? ` · 第 ${t.paid_in} 章已回收` : "")
        + (t.status === "dropped" ? " · 已弃置" : "") + (expired ? " · ⚠ 超期" : "");
      return h("div", { class: "nv-th" + (t.status === "paid" ? " paid" : "")
          + (t.status === "dropped" ? " off" : "") + (expired ? " expired" : "") },
        h("span", { class: "nv-th-dot" }, t.status === "paid" ? "●" : t.status === "dropped" ? "×" : "○"),
        h("div", { class: "nv-th-main" },
          h("b", null, (t.id || "") + " · " + (t.title || "")),
          h("span", { class: "nv-dim" }, meta),
          t.note ? h("span", { class: "nv-dim" }, t.note) : null),
        t.status === "open" ? h("span", { class: "nv-th-acts" },
          h("button", { class: "act-btn sm", dataset: { tip: "标记已在某章回收（记账）" },
            onclick: () => payThread(t) }, "✓ 回收"),
          h("button", { class: "act-btn sm no", dataset: { tip: "弃置：记录在案不再追讨" },
            onclick: () => dropThread(t) }, "✕ 弃置")) : null);
    };
    const chCard = (c) => {
      const body = h("div", { class: "nv-read", hidden: true });
      let loaded = false;
      const toggle = async () => {
        if (!body.hidden) { body.hidden = true; return; }
        if (!loaded) {
          try {
            const r = await api(`/api/novel/chapter?id=${encodeURIComponent(pid)}&no=${c.no}`);
            body.append(h("div", { class: "nv-read-text" }, r.text || "（正文文件缺失）"));
            loaded = true;
          } catch { toast("正文加载失败", true); return; }
        }
        body.hidden = false;
      };
      const en = c.entities || {};
      const ents = (en.characters || []).concat(en.scenes || [], en.props || []);
      const vs = (c.versions || []).length;
      return h("div", { class: "nv-ch" },
        h("div", { class: "nv-ch-hd", onclick: toggle },
          h("span", { class: "adapt-no" }, "CH" + String(c.no).padStart(2, "0")),
          h("b", null, c.title || "（无题）"),
          h("span", { class: "nv-dim" }, (c.chars || 0).toLocaleString() + " 字"),
          vs ? h("span", { class: "chip" }, `v${vs + 1}`) : null,
          chip(c.digest ? "✓ 大纲" : "✗ 大纲", c.digest ? "green" : undefined),
          chip(c.state ? "✓ 状态" : "✗ 状态", c.state ? "green" : undefined)),
        (c.digest || "").trim()
          ? h("p", { class: "nv-digest" }, c.digest)
          : h("p", { class: "nv-digest miss" }, "缺精简大纲——十章检查点连读审连贯全靠它"),
        ents.length ? h("div", { class: "nv-ents" }, ...ents.slice(0, 10).map((n) => chip(n))) : null,
        body);
    };
    const fill = nv.checkpoint_due ? every : ((nv.current_no || nv.count || 0) % every);
    const pips = [...Array(every)].map((_, i) =>
      h("i", { class: "nv-pip" + (i < fill ? " on" : "") }));
    const allThreads = [...(nvT.open || []), ...(nvT.paid || []), ...(nvT.dropped || [])];
    // 数据带：章稿/字数/检查点/伏笔四件事各占一格大数字，本批次指令挂带尾
    const openN = (nvT.open || []).length, expN = (nvT.expired || []).length;
    const kpi = (val, label, cls) => h("div", { class: "nv-kpi" + (cls ? " " + cls : "") },
      h("b", null, ...val), h("span", null, label));
    return h("div", { class: "nv-wrap" },
      h("div", { class: "nv-kpis" },
        kpi([String(nv.count || 0)], "章稿"),
        kpi(fmtWan(nv.total_chars || 0), "总字数"),
        h("div", { class: "nv-kpi" + (nv.checkpoint_due ? " due" : "") },
          h("b", null, nv.checkpoint_due ? "满档"
            : String((nv.next_checkpoint || every) - (nv.current_no || nv.count || 0)) + " 章"),
          h("div", { class: "nv-pips", dataset: { tip: `每满 ${every} 章一个检查点` } }, ...pips),
          h("span", null, "距检查点")),
        kpi([String(openN)], "伏笔未回收", openN ? "due" : ""),
        expN > 0 ? kpi([String(expN)], "伏笔超期", "bad") : null,
        h("div", { class: "nv-kpi-acts" },
          copyInstrBtn(`续写第 ${nextNo}~${batchTo} 章`,
            "开指令台：本批次续写指令（批次内不逐章问 · 满档即进检查点），可另附本批次诉求",
            writeInstr,
            { code: "NOVEL · BATCH", ask: "在此写本批次剧情要求",
              hint: `例：这一批把 ${nextNo} 章的悬念收掉；反派要正式登场，别再只闻其名`,
              done: `第 ${nextNo}~${batchTo} 章续写指令已复制——粘给 AI` }),
          copyInstrBtn("检查点指令", "开指令台：七门复核 + 批次报告指令", ckInstr,
            { code: "NOVEL · CHECKPOINT", ask: "在此写本次复核侧重",
              hint: "例：这次重点盯人设漂移与 AI 味，文风门放宽" }))),
      nv.checkpoint_due ? h("p", { class: "nv-dim nv-due" },
        "★ 检查点已满档——先做七门复核出批次报告，再续写") : null,
      (() => {
        // 创作日志：跨会话接手唯一的「上次是怎么判的」载体（append-only·novel log 写）。
        // 空态要说话——没有它，检查点结论一到新会话就没了，而这条从界面上看不出来。
        const lg = nv.log || [];
        const KIND = { checkpoint: "检查点", decision: "决策", overhaul: "手术", note: "备注" };
        return h("div", { class: "nv-style" },
          h("span", { class: "k" }, "创作日志 · LOG"),
          lg.length
            ? h("div", { class: "nv-style-rows" }, ...lg.slice().reverse().map((e) =>
                h("div", { class: "src-row" },
                  chip((KIND[e.kind] || e.kind) + (e.at_chapter ? ` 第${e.at_chapter}章` : ""),
                    e.kind === "checkpoint" ? "cyan" : e.kind === "overhaul" ? "amber" : undefined),
                  h("span", { class: "src-v" }, e.text || "",
                    e.ref ? h("span", { class: "nv-dim" }, "  → " + e.ref) : null))))
            : h("p", { class: "nv-dim" },
                "还没有一条——检查点做完必须 novel log 记一条（报告落 plan/ 后带 --ref 指过去）。"
                + "跨会话接手时，新会话读的就是它；不记＝这次复核没发生过。"));
      })(),
      (() => {
        const rows = [["pov", "视角"], ["tense", "时态"], ["voice", "声音"], ["diction", "语域"]]
          .filter(([k]) => st[k]);
        const nb = (st.baseline || []).length, na = (st.avoid || []).length;
        if (!rows.length && !nb && !na)
          return h("div", { class: "nv-style" },
            h("span", { class: "k" }, "文风契约 · NARRATIVE STYLE"),
            h("p", { class: "nv-dim" },
              "未立契——文风门没有锚点。让 AI 跑 novel init 并补 baseline 基线样本"
              + "（防漂靠基线比对，不靠每章复述风格）。"));
        return h("div", { class: "nv-style" },
          h("span", { class: "k" }, "文风契约 · NARRATIVE STYLE"),
          rows.length ? h("div", { class: "nv-style-rows" }, ...rows.map(([k, zh]) =>
            h("div", { class: "src-row" },
              h("span", { class: "src-k" }, zh), h("span", { class: "src-v" }, st[k])))) : null,
          h("div", { class: "chips" },
            chip(`基线样本 ${nb} 段`, nb ? "cyan" : undefined),
            ...(st.avoid || []).slice(0, 8).map((w) => chip("忌·" + w))));
      })(),
      (() => {
        // 卷/幕规划 = 长篇的大纲落点；进度态是引擎按最新章号**派生**的（不落盘）
        const av = nv.arcs || { arcs: [], gaps: [], overlaps: [] };
        if (!(av.arcs || []).length)
          return h("div", { class: "nv-style" },
            h("span", { class: "k" }, "卷 / 幕规划 · STORY ARCS"),
            h("p", { class: "nv-dim" },
              "未立纲——检查点第一门「有没有跑偏大纲」将无对照物。"
              + "让 AI 跑 novel arc 登记每卷的起止章与本卷目标。"));
        const MARK = { done: ["✔ 已收卷", "green"], writing: ["▶ 进行中", "cyan"],
                       planned: ["○ 未开写", undefined] };
        const arcRow = (a) => h("div", { class: "nv-arc" + (a.state === "writing" ? " now" : ""),
            dataset: { tip: a.goal || "" } },
          h("span", { class: "nv-arc-no" }, `卷${a.no} · ${a.from}~${a.to || "？"}`),
          h("div", { class: "nv-arc-main" },
            h("span", { class: "nv-arc-t" }, a.title || "（未命名）"),
            a.goal ? h("span", { class: "nv-arc-g" }, a.goal) : null),
          chip(...MARK[a.state]));
        // 三十几卷全摊开会把「章节稿」推到页面很深的地方——
        // 默认只展开**正在写的那一卷前后各两卷**，其余一键展开。
        const arcs = av.arcs, cur = Math.max(0, arcs.findIndex((a) => a.state === "writing"));
        const near = arcs.length <= 8 ? arcs
          : arcs.slice(Math.max(0, cur - 2), Math.min(arcs.length, Math.max(cur + 3, 5)));
        const box = h("div", { class: "nv-arcs" }, ...near.map(arcRow));
        const more = near.length < arcs.length
          ? h("button", { class: "act-btn sm nv-arc-more", onclick: (ev) => {
              box.replaceChildren(...arcs.map(arcRow)); ev.currentTarget.remove();
            } }, `展开全部 ${arcs.length} 卷`)
          : null;
        // 卷轴时间线：整部书按各卷章数等比铺一条带——已收卷实、在写发光、未开写虚；
        // 悬浮看卷名与起止。列表只展开当前卷附近，这条带补上「全书进行到哪」的一眼全景。
        const spanOf = (a) => Math.max(1, ((a.to || a.from || 1) - (a.from || 1)) + 1);
        const covered = arcs.reduce((n, a) => n + spanOf(a), 0);
        const track = h("div", { class: "arc-track" }, ...arcs.map((a) =>
          h("i", { class: "arc-seg " + (a.state || "planned"), style: `flex:${spanOf(a)}`,
            dataset: { tip: `卷${a.no} · ${a.title || "（未命名）"} · 第${a.from}~${a.to ?? "？"}章` } })));
        const nowA = arcs.find((a) => a.state === "writing");
        const caption = h("div", { class: "arc-caption" },
          h("span", null,
            nowA ? h("b", null, `卷${nowA.no} · ${nowA.title || "（未命名）"}`) : "—",
            nowA ? ` 第 ${nowA.from}~${nowA.to ?? "？"} 章` : ""),
          h("span", null, `${arcs.length} 卷 · 覆盖 ${covered} 章`));
        return h("div", { class: "nv-style" },
          h("span", { class: "k" }, `卷 / 幕规划 · ${arcs.length} 卷`),
          track, caption,
          box, more,
          ...(av.gaps || []).map((g) => h("p", { class: "nv-dim" },
            `· 断档：第 ${g.at[0]}~${g.at[1]} 章不属于任何一卷`)),
          ...(av.overlaps || []).map((o) => h("p", { class: "nv-dim nv-due" },
            `⚠ 重叠：卷${o.a} 与 卷${o.b} 同覆盖第 ${o.at[0]}~${o.at[1]} 章`)));
      })(),
      h("div", { class: "nv-sec-h" }, `章节稿 · ${nvCh.length}`),
      h("div", { class: "nv-list" },
        ...nvCh.sort((a, b) => (b.no || 0) - (a.no || 0)).map(chCard)),   // 新章在前
      h("div", { class: "nv-sec-h" },
        `伏笔账本 · 未回收 ${(nvT.open || []).length}`
        + ((nvT.expired || []).length ? ` · ⚠ 超期 ${nvT.expired.length}` : "")),
      allThreads.length
        ? h("div", { class: "nv-threads" }, ...allThreads.map(threadRow))
        : h("p", { class: "nv-dim" },
            "尚无伏笔登记——埋设时让 AI novel thread-add 记一条，lint 与检查点会盯回收。"));
  };

  // 图谱：Claude 分析原文产出 nodes+edges（走「指令交指挥层」铁律），adapt graph 落库后可视化
  const graphInstr = `为「${title}」构建人物关系 / 世界观图谱：读 project/${pid}/source/raw.txt`
    + `（借 project/${pid}/source/segments.json 目录定位），梳理主要角色 / 阵营 / 地点 / 世界观法则`
    + ` 及其关联，按 kinema-project SKILL「3.6 关系图谱」产出 `
    + `{"summary":"…","nodes":[{"id","name","type":"character|faction|location|item|worldview","desc","faction"}],`
    + `"edges":[{"source","target","relation","kind":"kin|ally|mentor|hostile|love|member|rival","directed"}]} JSON，`
    + ` 再 adapt graph ${pid} --file 图谱.json 落库。`;
  const GRAPH_DLG = { title: "图谱指令", code: "ADAPT · GRAPH", ask: "在此写梳理侧重",
    hint: "例：只画主角这一支的血缘与师承；阵营敌对关系要标明起因",
    done: "图谱指令已复制——粘给 AI 分析原文生成" };
  const graphChip = (label) => d.source ? h("button", { class: "mast-act",
    dataset: { tip: "开指令台：写下梳理侧重，合并「读原文→梳理→adapt graph 落库」的指令给 AI" },
    onclick: () => openDirectiveDialog({ ...GRAPH_DLG, meta: `「${title}」· 项目 ${pid}`,
      directive: graphInstr }) }, label) : null;
  let graphMount = null;   // 交互图谱初始化延到首次进 Tab（需真实容器尺寸做 fit）
  const buildGraph = () => {
    const g = d.graph;
    if (!g || !(g.nodes || []).length)
      return paneEmpty("graph", "尚无关系图谱",
        d.source
          ? "让 AI 读源文本，梳理人物关系与世界观法则，产出节点 + 连线图谱后在此可视化查看。"
          : "先上传源文本入库，再让 AI 梳理人物关系与世界观图谱。",
        d.source ? copyInstrBtn("图谱指令",
          "开指令台：让 AI 读原文梳理人物关系与世界观，adapt graph 落库后可视化",
          graphInstr, GRAPH_DLG) : null);
    const mount = h("div", { class: "kg-mount" });
    graphMount = { host: mount, g };
    return mount;
  };

  // ── 组装
  const nvSegs = novelSegs(d);                 // 原创章稿 → 目录/阅读器可用的「段」
  const asNovel = !(d.source && segs.length) && nvSegs.length > 0;
  const tocSegs = asNovel ? nvSegs : segs;
  if ((d.source && segs.length) || asNovel) {
    // 全屏两栏「阅读工作台」：抬头(可折叠) + 左目录(独立滚) + 右 Tab(独立滚)
    $("#view").classList.add("view-app");
    const app = h("div", { class: "script-app" });
    let reading = false;
    const setReading = (on) => { if (on === reading) return; reading = on; app.classList.toggle("reading", on); };

    const reader = buildReader(tocSegs, pid, { onNavigate: () => setReading(true),
      fetch: asNovel
        ? ((s) => api(`/api/novel/chapter?id=${encodeURIComponent(pid)}&no=${s.index}`))
        : undefined,
      truncNote: asNovel ? "…… 本章过长已截断，完整正文见 manuscript/" : undefined,
      md: asNovel });                                    // 原创正文是 markdown
    // 卷分组：原创章稿天然有卷纲；「原创 + 已入库」混合态里源切分与章稿 1:1 同号，
    // 卷纲同样适用；真改编项目没有 novel.arcs，自动回落平铺目录。
    const chapterToc = tocSegs.length > 0 && isChapterSeg(tocSegs[0]);
    const arcGroups = chapterToc ? ((((d.novel || {}).arcs) || {}).arcs || []) : [];
    const toc = scriptToc(tocSegs, (idx) => { activate("text"); reader.loadByIndex(idx); }, arcGroups);
    reader.setNavHook((idx) => toc.setActive(idx));
    const tocScrollEl = h("div", { class: "toc-scroll" }, toc.node);
    // 吸顶（阅读模式）：下滚即进入并保持——切章、上滑看内容都不退出（滚动从不主动退出）；
    // 只有「已在正文顶部再上滑一次」（wheel 上滑且 scrollTop≤0）才退出、展开全部（用户口径）。
    const enterReading = (el) => () => { if (el.scrollTop > 30) setReading(true); };
    reader.scroll.addEventListener("scroll", enterReading(reader.scroll));
    tocScrollEl.addEventListener("scroll", enterReading(tocScrollEl));
    reader.scroll.addEventListener("wheel", (e) => {
      if (e.deltaY < 0 && reader.scroll.scrollTop <= 0) setReading(false);
    }, { passive: true });

    const panels = {
      text: h("div", { class: "script-panel reader-panel" }, reader.panel),
      write: h("div", { class: "script-panel scroll-panel" }, buildNovel()),
      source: h("div", { class: "script-panel scroll-panel" }, buildSource()),
      bible: h("div", { class: "script-panel scroll-panel" }, buildBible()),
      eps: h("div", { class: "script-panel scroll-panel" }, buildEpisodes()),
      ent: h("div", { class: "script-panel scroll-panel" }, buildEntities()),
      graph: h("div", { class: "script-panel kg-panel" }, buildGraph()),
    };
    const gnodes = (d.graph && d.graph.nodes) ? d.graph.nodes.length : 0;
    const TABS = [["text", "正文", null], ["write", "创作", nvCount || null],
                  ["source", "信息", null],
                  // 原创项目的 adaptation 里装的是世界观宪法，Tab 名随之改口；
                  // 「分集」是成片侧的事，纯小说没有源也没有分集，整枚不挂（空 Tab 的
                  // 空态文案还会写「先上传源文本」，对原创是错的指路）
                  ["bible", asNovel ? "宪法" : "拆书", Object.keys(ad).length || null],
                  ...((asNovel && !eps.length) ? [] : [["eps", "分集", eps.length || null]]),
                  ["ent", "设定", (chars.length + props.length + scenes.length) || null],
                  ["graph", "图谱", gnodes || null]];
    const tabBtns = {};
    const activate = (key) => {
      Object.keys(panels).forEach((k) => { panels[k].hidden = k !== key; });
      Object.keys(tabBtns).forEach((k) => tabBtns[k].classList.toggle("on", k === key));
      if (key !== "text") setReading(false);                // 离开正文 → 恢复抬头
      if (key === "graph" && graphMount) {                  // 交互图谱惰性初始化 + 每次进入 refit
        if (!graphMount.ctrl)
          graphMount.ctrl = kgGraph(graphMount.host, graphMount.g, pid,
            { title, regenBtn: graphChip("↻ 重新生成图谱") });
        graphMount.ctrl.onShow();
      }
    };
    // 编辑部式编号页签（01 正文 · 02 创作 …）——编号是版面语言，不挂图标
    const tabBar = h("div", { class: "scr-tabs script-tabs" }, ...TABS.map(([k, label, cnt], i) => {
      const b = h("button", { class: "scr-tab", onclick: () => activate(k) },
        h("span", { class: "scr-tab-no" }, String(i + 1).padStart(2, "0")),
        h("span", { class: "scr-tab-txt" }, label),
        cnt != null ? h("span", { class: "scr-tab-cnt" }, String(cnt)) : null);
      tabBtns[k] = b; return b;
    }));
    const pane = h("div", { class: "script-pane" },
      tabBar,   // 页签自带通栏 hairline，右栏不另立「工作台」分区标题
      h("div", { class: "script-panels" }, panels.text, panels.write, panels.source, panels.bible, panels.eps, panels.ent, panels.graph));
    const side = h("div", { class: "console-side script-side" },
      secHeader("◧", "目录",
        (!asNovel && d.segment_kind === "scene") ? "SCENES" : "CHAPTERS",
        tocSegs.length + (asNovel ? " 章" : " 段")),
      h("input", { class: "toc-search", type: "text", placeholder: "搜索章节标题…",
        oninput: (e) => toc.filter(e.target.value) }),
      tocScrollEl);

    // 抬头 · 书封题头：书名做主角（衬线大字），右侧数据带给全书一眼可读的量级，
    // 下排指令行收拢「复制指令给 AI」这组核心交互；上传/清空是**源文本**的工具，
    // 原创项目没有源，挂在这儿只会请人去做一件会把自己变成改编项目的事
    const entN = chars.length + props.length + scenes.length;
    const openTh = ((((d.novel || {}).threads) || {}).open || []).length;
    const arcN = ((((d.novel || {}).arcs) || {}).arcs || []).length;
    // 有章稿的一律是「原创长篇」（源文本只是自己书稿的入库镜像，不是别人的书拿来改）
    const kindTxt = nvCount
      ? "原创长篇" + (d.source ? " · 已入库" : "")
      : (d.source ? (KIND[d.source.kind] || d.source.kind || "") + " · 改编" : "未入库");
    const stat = (val, label, cls) => h("div", { class: "mast-stat" + (cls ? " " + cls : "") },
      h("b", null, ...val), h("span", null, label));
    const stats = nvCount
      ? [stat([String(nvCount)], "章稿"),
         stat(fmtWan((d.novel || {}).total_chars || 0), "总字数"),
         arcN ? stat([String(arcN)], "卷") : null,
         stat([String(openTh)], "伏笔未回收", openTh ? "warn" : ""),
         eps.length ? stat([String(eps.length)], "分集") : null,
         entN ? stat([String(entN)], "设定") : null]
      : [stat(fmtWan((d.source || {}).chars || 0), "源字数"),
         stat([String(eps.length)], "分集"),
         stat([String(builtCount)], "已建章"),
         entN ? stat([String(entN)], "设定") : null,
         gnodes ? stat([String(gnodes)], "图谱节点") : null];
    app.append(
      h("div", { class: "script-head" },
        h("div", { class: "script-mast" },
          h("div", { class: "mast-main" },
            h("div", { class: "mast-eyebrow" },
              h("span", null, "剧本工作台"),
              h("span", { class: "dim" }, `${(pid || "").toUpperCase()} · ${kindTxt}`)),
            h("div", { class: "mast-title" }, title)),
          h("div", { class: "mast-stats" }, ...stats)),
        h("div", { class: "mast-tools" },
          createBtn(), copyBriefBtn(), graphChip("⧉ 图谱指令"), askChip(),
          h("span", { class: "spring" }),
          ...(d.source ? [mkReup(), clearSourceBtn()] : []))),
      h("div", { class: "console script-console" }, side, pane));
    view.append(app);
    activate("text");
    reader.loadFirst();                                     // 默认进正文 tab 即载入首章
    scriptSelectTools(panels.text, pid, title);             // 划词抽实体挂正文面板
    // .main 是 min-height 不封顶，纯 CSS flex 链拿不到确定高度→内部无法滚；用 JS 按
    // app 距顶实测把高度钉死，两栏才有确定高度、各自 overflow 滚动+吸顶才生效
    const fit = () => {
      if (!app.isConnected) { window.removeEventListener("resize", fit); return; }
      app.style.height = Math.max(360, window.innerHeight - app.getBoundingClientRect().top) + "px";
    };
    requestAnimationFrame(() => { fit(); requestAnimationFrame(fit); });
    window.addEventListener("resize", fit);
  } else {
    // 未入库 / 无结构切分：常规页面滚动 + 上传投放区 + 空态
    view.append(h("div", { class: "head-hero" },
      h("h1", null, "剧本工作台", h("span", { class: "ch-code" }, (pid || "").toUpperCase())),
      h("div", { class: "sub" }, title),
      h("div", { class: "chips" }, KINDCHIP(),
        d.source ? chip((d.source.chars || 0).toLocaleString() + " 字") : null,
        // 纯小说项目「集」恒为 0（分集是成片侧的事）——挂个 0 是在报一个不存在的缺口
        (eps.length || d.source) ? chip(`${eps.length} 集`) : null,
        nvCount ? chip(`${nvCount} 章稿`, "cyan") : null,
        h("span", { class: "chip-sep" }), createBtn(), copyBriefBtn(), graphChip("⧉ 图谱指令"), askChip())));
    // 原创小说项目可能自始至终没有「源文本」——创作区在无源布局里置顶，
    // 开写/续写/检查点/伏笔账本全程可用（有源两栏布局里它是「创作」Tab）
    view.append(secHeader(no(), "创作", "WRITING", nvCount || null), buildNovel());
    view.append(secHeader(no(), "源文本", "SOURCE",
      d.source ? (d.source.chars || 0).toLocaleString() + " 字" : null));
    if (d.source) {
      view.append(h("div", { class: "card adapt-src" },
        h("span", { class: "k" }, "源文本 · SOURCE"),
        h("div", { class: "adapt-src-row" }, chip(KIND[d.source.kind] || d.source.kind || "—", "cyan"),
          h("b", null, (d.source.chars || 0).toLocaleString() + " 字"),
          h("code", null, d.source.file || ""),
          d.source.ingested_at ? h("span", { class: "adapt-dim" }, "入库 "
            + (d.source.ingested_at || "").slice(0, 16).replace("T", " ")) : null,
          h("span", { class: "adapt-sha" }, d.source.sha256 || ""), mkReup(), clearSourceBtn())));
    } else {
      view.append(scriptDropzone(uploadSource, pickFile));
    }
    view.append(secHeader(no(), "拆书", "STORY BIBLE", Object.keys(ad).length || null), buildBible());
    view.append(secHeader(no(), "分集", "EPISODES", eps.length || null), buildEpisodes());
    view.append(secHeader(no(), "设定", "ENTITIES", (chars.length + props.length) || null), buildEntities());
    view.append(secHeader(no(), "正文 · 结构", "SOURCE TEXT", null),
      emptyBlock(d.source ? "无结构切分" : "未入库",
        d.source ? "重新上传以生成结构索引（segments.json）。"
                 : "上传小说 / 剧本后，正文会在这里按章 / 场分段阅读。", null));
  }
}

async function viewProject(view, pid) {
  const [p, ov] = await Promise.all([api(`/api/project?id=${encodeURIComponent(pid)}`), getOverview()]);
  setCrumbs([["总览", "#/"], [p.title || pid]]);
  let secNo = 0;
  const no = () => String(++secNo).padStart(2, "0");

  // 软删态：只读冻结——回收站横幅置顶 + 变更类操作按钮统一灰化禁用（见 .ro-deleted）；未删则给删除入口
  $("#view").classList.toggle("ro-deleted", !!p.is_deleted);
  if (p.is_deleted) view.append(deletedBanner(pid, p.title || pid, p.deleted_at));
  // 删除项目 → 移至回收站。按钮从右上角挪到页面最底部独立「危险区」模块（见文末），
  // 避免误触；此处只留处理函数供危险区调用。
  const doDelete = async () => {
    if (!(await uiConfirm(
      `将项目「${p.title || pid}」移至回收站？\n数据与产物完整保留，可随时从回收站恢复。`,
      { danger: true, title: "移至回收站" }))) return;
    try {
      await post("/api/project/delete", { project: pid });
      toast(`项目「${p.title || pid}」已移入回收站——项目页可恢复`);
      await getOverview(true); renderRail(state.overview);
      location.hash = "#/projects";
    } catch (err) { toast(err.message, true); }
  };

  view.append(h("div", { class: "head-hero with-cover" },
    // 系列封面未出：主视觉位放占位而不是塞兜底图——这个位置放海报帧/分镜图，
    // 看起来就像封面已经做过了，缺口再没人发现（卡片缩略图才走三级回落）
    !p.cover && h("div", { class: "series-cover ph",
      dataset: { tip: "系列主视觉未生成——项目卡横幅退到海报帧/分镜图兜底\n"
                      + `对 AI 说：给《${p.title || p.id}》做系列主视觉封面` } },
      "NO KEY VISUAL"),
    // 系列封面主视觉：竖容器展示 3:4 竖版；灯箱内竖/横双版可翻页
    p.cover && h("figure", { class: "series-cover",
        onclick: () => openLightbox([
          { src: p.cover, title: "KEY VISUAL · 竖版 3:4",
            caption: `${p.title || p.id} · 系列主视觉` },
          ...((p.covers || {})["4:3"]
            ? [{ src: p.covers["4:3"], title: "KEY VISUAL · 横版 4:3",
                 caption: `${p.title || p.id} · 系列主视觉（横版）` }] : []),
        ]) },
      h("img", { src: p.cover, alt: "系列封面", loading: "lazy" })),
    h("div", { class: "head-hero-txt" },
      h("h1", null, p.title || p.id),
      p.theme && h("div", { class: "sub" }, p.theme),
      h("div", { class: "chips" },
        profileChip(p.profile), skillChip(p.skill), statusPill(p.status),
        p.template && titledChip(`▤ ${p.template.label || p.template.name}`, "cyan",
          p.template.notes || "平台规格模板"),
        chip(p.aspect || "9:16"),
        ...(p.platform || []).map((x) => chip(LABEL.platform[x] || x)),
        h("span", { class: "chip" }, `更新 ${(p.updated_at || "").slice(0, 16).replace("T", " ")}`)))));

  // 剧本区：项目页只给概览入口卡，点进「剧本工作台」详情页做全部改编工作
  {
    const eps = p.episodes || [];
    const chapIds = new Set((p.chapters || []).map((c) => c.id));
    const nBuilt = eps.filter((e) => e.chapter_id && chapIds.has(e.chapter_id)).length;
    const KIND = { novel: "小说", screenplay: "剧本", outline: "大纲" };
    view.append(secHeader(no(), "剧本", "ADAPTATION", eps.length || null));
    const goScript = () => { location.hash = `#/project/${encodeURIComponent(p.id)}/script`; };
    view.append(h("div", { class: "studio-entry", role: "button", tabindex: "0",
        onclick: goScript,
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); goScript(); } } },
      h("div", { class: "studio-sub" },
        p.source
          ? `${KIND[p.source.kind] || p.source.kind} · ${(p.source.chars || 0).toLocaleString()} 字 · 分集 ${eps.length}（${nBuilt} 已建）`
          : "上传完整小说 / 剧本，拆书分集，一键建本集"),
      h("span", { class: "studio-go" }, "进入工作台",
        h("i", { class: "studio-arrow" }, "→"))));
  }

  // 章节
  view.append(secHeader(no(), "章节", "EPISODES", p.chapters.length));
  if (!p.chapters.length) {
    view.append(emptyBlock("还没有章节", "创建章节后由 Skill 填分镜、引擎渲染：",
      `对 AI 说：给《${p.title || p.id}》开第一章——写好文案分镜，按审阅节点走到成片`));
  } else {
    const hdr = h("div", { class: "chap-row hdr" },
      ["画面", "#", "章节", "状态", "模式", "分镜", "时长", "成本", "比例"].map((t) => h("span", null, t)));
    const rows = p.chapters.map((c) => h("div", { class: "chap-row",
        onclick: () => (location.hash = `#/project/${encodeURIComponent(p.id)}/${encodeURIComponent(c.id)}`) },
      h("div", { class: "chap-thumb" },
        // 16:9 小缩略——4:3 横版无字真源优先（84px 下排版字只剩噪点），回落 poster
        (c.covers_bg || {})["4:3"] || (c.covers || {})["4:3"] || c.poster
          ? h("img", { src: (c.covers_bg || {})["4:3"] || (c.covers || {})["4:3"] || c.poster,
                       loading: "lazy", alt: "" })
          : h("div", { class: "ph" }, "—")),
      h("span", { class: "chap-ord" }, String(c.order ?? "")),
      h("div", { class: "chap-title" }, h("b", null, c.title || c.id), h("small", null, c.id)),
      statusPill(c.status),
      motionBadge(c.motion),
      h("span", { class: "chap-num" }, `${c.shots}`, h("span", { class: "u" }, " 镜")),
      h("span", { class: "chap-num" }, c.duration ? fmtDur(c.duration) : "—"),
      h("span", { class: "chap-num" },
        c.cost_total != null ? fmtCost(c.cost_total, (c.cost || {}).currency) : "—"),
      h("span", { class: "chap-num" }, (c.aspects || []).join(" "))));
    view.append(h("div", { class: "chap-table" }, hdr, rows));
  }

  // 平台规格达标：每集时长对照模板区间
  if (p.template && p.chapters.length) {
    view.append(secHeader(no(), "平台规格", "PLATFORM SPEC"));
    view.append(specCard(p));
  }

  // 角色设定——**按需折叠**：有设定图的直接显示，没图的收进「未生成」抽屉。
  // 长篇改编（小说层）登记几十个实体是正典账本，删不得；但全部平铺时页面会被
  // 大量无图空卡占满——长篇系列常见几十个角色只有零星几张图。
  // 全无图的新项目保持旧行为（平铺+空态引导），有图后才开始折叠。
  const chWith = p.characters.filter((c) => c.sheet);
  const chWithout = p.characters.filter((c) => !c.sheet);
  view.append(secHeader(no(), "角色设定", "CHARACTER SHEETS", p.characters.length));
  if (chWith.length && chWithout.length) {
    view.append(characterGrid(chWith, p.id, p.voice_bank));
    view.append(foldSection(characterGrid(chWithout, p.id, p.voice_bank),
                            chWithout.length));
  } else {
    view.append(characterGrid(p.characters, p.id, p.voice_bank));
  }

  // 旁白选角（与角色同构：试音/定制 → 立档 → 启用）
  view.append(secHeader(no(), "旁白选角", "NARRATOR"));
  view.append(narratorCard(p));

  // 道具 / 武器（折叠制度同角色区）
  if (p.props.length) {
    const prWith = p.props.filter((pr) => pr.sheet);
    const prWithout = p.props.filter((pr) => !pr.sheet);
    view.append(secHeader(no(), "道具 · 武器设定", "PROPS & WEAPONS", p.props.length));
    if (prWith.length && prWithout.length) {
      view.append(h("div", { class: "prop-grid" }, prWith.map((pr) => propCard(pr, p.id))));
      view.append(foldSection(
        h("div", { class: "prop-grid" }, prWithout.map((pr) => propCard(pr, p.id))),
        prWithout.length));
    } else {
      view.append(h("div", { class: "prop-grid" }, p.props.map((pr) => propCard(pr, p.id))));
    }
  }

  // 取景地 / 具名场景（scenes[]，与 props 并列的一档：人走得进去的地方）。
  // 具名场景的设定图必须在项目页有落点——只渲染全局 scene_ref 的话（实测 abyss
  // 22 个取景地、2 张已生成），「固定场景」空态整块不渲染，用户直接得出
  // 「场景图没生成」。具名场景卡与道具卡同版式（灯箱/重生/版本谱系同制度）
  if ((p.scenes || []).length) {
    const scWith = p.scenes.filter((sc) => sc.sheet);
    const scWithout = p.scenes.filter((sc) => !sc.sheet);
    view.append(secHeader(no(), "取景地 · 场景设定", "LOCATIONS", p.scenes.length));
    if (scWith.length && scWithout.length) {
      view.append(h("div", { class: "prop-grid" }, scWith.map((sc) => sceneCard(sc, p.id, p.aspect))));
      view.append(foldSection(
        h("div", { class: "prop-grid" }, scWithout.map((sc) => sceneCard(sc, p.id, p.aspect))),
        scWithout.length));
    } else {
      view.append(h("div", { class: "prop-grid" },
        p.scenes.map((sc) => sceneCard(sc, p.id, p.aspect))));
    }
  }

  // 取景地 · 场景俯视：与上面的基准图一一配对的另一半。基准图交代「看上去什么样」，
  // 俯视图交代「空间怎么摆」——墙与出入口、可通行范围、三个建议机位与走位动线
  // （本场 180° 轴线与站位缺省不画，逐个取景地开）。出视频时每镜附一张：这一镜在哪
  // 就发哪张（引擎侧 lineage.primary_layout_ref 定主场景），模型才同时拿到外观与
  // 空间两组约束。**独立成区而不是塞进上面的取景地卡**：一张
  // 16:9 图纸压进 190px 窄卡的次要位置就读不出来了，而缺了哪个场景的图纸也一眼看不见。
  {
    // 全局固定场景那一对图并排在下面的「固定场景」区，这里只管具名取景地——
    // 同一张图在两个区各出现一次，读者会以为是两张
    const rows = p.scenes || [];
    if (rows.length) {
      const withPlan = rows.filter((x) => x.topview);
      const without = rows.filter((x) => !x.topview);
      view.append(secHeader(no(), "取景地 · 场景俯视", "TOP-DOWN LAYOUTS",
                            `${withPlan.length}/${rows.length}`));
      view.append(h("p", { class: "bible-intro" },
        "每个取景地配一张正俯视平面图纸：掀顶后的墙体与出入口、门窗与高差、",
        "可通行范围、三个建议机位的视野锥与出入通行路径。",
        "出视频时每镜附一张图纸——这一镜在哪就发哪张，跟着它自己的基准图走；",
        "镜头在屋里的站位与人物左右关系才不会每镜重编一遍。",
        "（本场 180° 动作轴线与人物站位缺省不画：取景地跨场次复用，",
        "轴线属于「这一场戏」——要钉的话对 AI 说，逐个取景地开。）"));
      // 折叠制度与上面的取景地区同一套：有图的平铺、没图的收进抽屉。**抽屉恒在**
      // ——一张都还没出时它就是缺口清单（缺哪几个地方、每张卡的素材直供与调校入口
      // 都在里面）；缺省收起是因为 22 张「NO LAYOUT」占位卡平铺一屏，
      // 信息量与一行字相同却要滚过整整一屏才看得到下一区。
      if (withPlan.length) {
        view.append(h("div", { class: "prop-grid" },
          withPlan.map((sc) => topviewCard(sc, p.id))));
      } else {
        view.append(emptyBlock(`${rows.length} 个取景地都还没有俯视图`,
          "补齐后出视频自动带上，分镜与提示词一行都不用改；已有基准图的取景地"
          + "会以它为空间取材，两张图对得上。",
          `对 AI 说：给《${p.title || p.id}》补齐取景地的场景俯视图`));
      }
      // 抽屉之外**不另补一条待办**：区块头计数（1/22）与抽屉标题（未生成俯视图 21 项）
      // 已经把缺口说了两遍，第三遍只是噪点。全缺那一档才有空态——那时抽屉是收起的，
      // 需要一句话说清这块是干什么的。
      if (without.length) {
        view.append(foldSection(h("div", { class: "prop-grid" },
          without.map((sc) => topviewCard(sc, p.id))), without.length, "俯视图"));
      }
    }
  }

  // 固定场景（全片就发生在同一个地方时的那一档：文本进每条分镜提示词、图恒挂每一镜）。
  // **基准图与它的俯视图并排放在这里**，不拆去上面的俯视区——一个场景的两张图分到
  // 两个区，读者得自己把它们配起来；而通栏大图那半屏本来就够放两张。
  if (p.scene || p.scene_ref || (p.scene_ref_candidates || []).length) {
    view.append(secHeader(no(), "固定场景", "SCENE REFERENCE"));
    // 两格共用一个构件：有图=可点开的富灯箱（重生/提意见/局部改造/版本谱系齐全），
    // 缺图=与设定卡同一套斜纹占位（不是一大块灰底空框），缺口说明压在 tooltip 里，
    // 版面上只留一行 mono 角标
    const sceneFig = (cls, src, code, title, kind, comments, versions, alt, tip) =>
      h(src ? "figure" : "div", { class: cls,
          onclick: src ? () => openLightbox([{ src, title, caption: p.scene,
            actx: { pid: p.id, kind, comments: comments || [] } }]) : null },
        src ? h("img", { src, alt, loading: "lazy" })
            : h("div", { class: "nosheet", dataset: { tip } },
                h("span", null, code === "KEY ART" ? "NO KEY ART" : "NO LAYOUT")),
        src ? h("span", { class: "scene-cap" }, code) : null,
        src ? assetVerBadge(p.id, kind, null, versions) : null);
    view.append(h("div", { class: "scene-wrap" },
      sceneFig("scene-fig", p.scene_ref, "KEY ART", "SCENE", "scene",
               p.scene_comments, p.scene_versions, "场景设定图",
               `固定场景的基准图还没生成——它是全片的光线与陈设基准，恒挂每一镜。\n`
               + `对 AI 说：给《${p.title || p.id}》生成固定场景的设定图`),
      h("div", { class: "scene-body" },
        h("div", { class: "scene-txt" },
        h("div", { class: "scene-txt-head" }, h("span", { class: "k" }, "SCENE PROMPT"),
          tuneBtn("固定场景", () => [
            `请调校全局固定场景的设定 · 项目 ${p.id}`,
            `定位坐标：project/${p.id}/project.json → 顶层 scene（全片同景的基准文案，人工字段）`,
            `现有设定 —— SCENE PROMPT：${p.scene || "未写"}`,
            `落地：改顶层 scene 后重跑一次 \`project refs ${p.id}\` 同步各章；要重生成设定图`
              + `须加 --force（⚠ 不带名字的 \`--only scene\` / \`--only topview\` 会连带`
              + `全部具名取景地，先确认重生范围）；只重画俯视图用 \`--only topview --force\`。`,
          ].join("\n"), { code: "SCENE · FIXED",
                          ask: "在此写要精修的空间结构 / 氛围光影 / 标志元素",
                          meta: `全局固定场景 · 项目 ${p.id}`,
                          hint: "例：整体压暗一档，只留窗口一束冷光；墙面加剥落的旧标语" })),
          p.scene || "—"),
        sceneFig("scene-plan", p.scene_topview, "TOP-DOWN", "LAYOUT · 固定场景",
                 "topview", p.scene_topview_comments, p.scene_topview_versions,
                 "场景俯视图",
                 `固定场景的俯视图还没生成——这一镜没有具名取景地时，出视频发的就是它，\n`
                 + "跟着左边的基准图走：一张给外观、一张给空间。\n"
                 + `对 AI 说：给《${p.title || p.id}》补齐固定场景的俯视图`))));
    const sg = refCandGrid(p.id, "scene", null, p.scene_ref_candidates, p.scene_ref_picked);
    if (sg) view.append(h("div", { class: "card", style: "margin-top:10px" }, sg));
  }

  // 参考库 / 风格垫图：上传参考图 → 默认注入每张设定图/分镜图/封面（本地图转 base64，无需 OSS）；
  // ✓/○ 逐张切换是否默认套用（停用留库不删）；上传/移除/切换均局部刷新本模块、不整页重渲不跳顶（Task 3）。
  {
    const mbNo = no();
    // mb-section 补回区块上间距：内部 .sec 是 mbHost 首子会被 .sec:first-child 归零，故间距挪到外层
    const mbHost = h("div", { class: "mb-section" });
    view.append(mbHost);
    const refreshMb = async () => {   // 局部刷新：只重拉 project 取新 url/on 态，原地重绘本模块
      try {
        const fresh = await api(`/api/project?id=${encodeURIComponent(p.id)}`);
        renderMbInto(fresh.moodboard || []);
      } catch (err) { toast(err.message, true); }
    };
    const mbUpload = async (f) => {
      if (!f) return;
      if (f.size > 30 * 1024 * 1024) { toast("图片超过 30MB 上限", true); return; }
      toast("上传垫图中…");
      try {
        const qs = new URLSearchParams({ project: p.id, name: f.name }).toString();
        const res = await fetch(`/api/moodboard/upload?${qs}`,
          { method: "POST", headers: { "X-Csrf-Token": CSRF }, body: f });
        const r = await res.json();
        if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
        toast(`✓ 已加入参考库（共 ${(r.moodboard || []).length} 张）——默认套用后续所有生成`);
        await refreshMb();
      } catch (err) { toast(err.message, true); }
    };
    const mbPick = () => {
      const fin = h("input", { type: "file", accept: "image/*", style: "display:none" });
      fin.addEventListener("change", () => { const f = fin.files && fin.files[0]; fin.remove(); mbUpload(f); });
      document.body.append(fin); fin.click();
    };
    const mbDel = async (path) => {
      if (!(await uiConfirm("移除这张参考图？（彻底删除；只是不想默认套用可点 ✓ 切换为停用）",
        { danger: true, title: "移除参考图" }))) return;
      try { await post("/api/moodboard/remove", { project: p.id, path }); toast("已移除参考图"); await refreshMb(); }
      catch (err) { toast(err.message, true); }
    };
    const mbToggle = async (m) => {   // 切换默认启用（不删文件）
      try {
        await post("/api/moodboard/toggle", { project: p.id, path: m.path, on: m.on === false });
        await refreshMb();
      } catch (err) { toast(err.message, true); }
    };
    const renderMbInto = (mb) => {
      const nOn = mb.filter((m) => m.on !== false).length;
      mbHost.innerHTML = "";
      mbHost.append(
        secHeader(mbNo, "参考库", "MOODBOARD", mb.length || null),
        h("div", { class: "mb-wrap" },
          h("div", { class: "mb-grid" },
            ...mb.map((m) => h("figure", { class: "mb-item" + (m.on === false ? " mb-off" : "") },
              h("img", { src: m.url, alt: "参考图", loading: "lazy",
                onclick: () => openLightbox([{ src: m.url, title: "MOODBOARD · 参考图" }]) }),
              h("button", { class: "mb-tog", dataset: { tip: m.on === false
                  ? "已停用：留在库中但不默认套用（本镜可在分镜卡手动勾选）——点击恢复默认套用"
                  : "默认启用：所有设定图/分镜图/封面自动套用此风格——点击停用" },
                onclick: () => mbToggle(m) }, m.on === false ? "○" : "✓"),
              h("button", { class: "mb-del", dataset: { tip: "彻底移除这张参考图" },
                onclick: () => mbDel(m.path) }, "×"))),
            h("button", { class: "mb-add", onclick: mbPick,
              dataset: { tip: "上传参考图，默认套用到全片所有设定图/分镜图/封面的风格；本地图自动转 base64 随请求提交，无需配置 OSS" } },
              h("span", { class: "mb-add-ico" }, "＋"), h("span", null, "上传参考图"))),
          h("p", { class: "mb-hint" },
            mb.length
              ? `${nOn}/${mb.length} 张默认套用全局生成（✓）——所有设定图/分镜图/封面自动带上此风格；`
                + "点 ✓/○ 切换是否默认，本地图转 base64 随请求提交、无需 OSS。想让某一镜临时改用别的参考，"
                + "去分镜卡「⛭ 垫图」单独勾选。"
              : "上传参考图后默认套用到全片每张设定图/分镜图/封面（垫图统一模块风格）；"
                + "本地图直接转 base64 随请求提交，无需 OSS。")));
    };
    renderMbInto(p.moodboard || []);
  }

  // 总体设计：全系列的创作准绳——六个字段各带用途说明，
  // 缺失字段也占位显示（看得见全貌，知道还差什么、找谁补）
  const BIBLE = [
    ["logline", "一句话卖点", "LOGLINE",
      "一句话说清这部片讲什么、凭什么好看——选题的灵魂，宣发文案与提案书的源头"],
    ["synopsis", "梗概", "SYNOPSIS",
      "三五句话的整片故事走向——写新章节时的剧情基准，防止越写越偏"],
    ["world", "世界观", "WORLD",
      "故事发生的世界规则与设定——跨章节不打架的背景契约"],
    ["tone", "基调", "TONE",
      "整片的情绪气质（治愈 / 热血 / 悬疑…）——文案口吻与配乐情绪的准绳"],
    ["palette", "色板", "PALETTE",
      "全片主色调约定——与画风前缀、设定图共同锚定色彩一致性"],
    ["style_notes", "风格备注", "NOTES",
      "画风执行细节的补充约定——生图提示词拼装时的附加指令"],
  ];
  const filled = BIBLE.filter(([k2]) => (p.design || {})[k2]).length;
  view.append(secHeader(no(), "总体设计", "SERIES BIBLE", filled || null));
  view.append(h("p", { class: "bible-intro" },
    "全系列的创作准绳，立项时由 AI 写定——之后每一章的文案口吻、画面色板、",
    "世界观都从这里对表。要补全或修订，对 AI 说一句「完善 ",
    p.title || p.id, " 的总体设计」即可。"));
  view.append(h("div", { class: "design-grid" }, BIBLE.map(([k2, zh, en, use]) => {
    const val = (p.design || {})[k2];
    // 风格备注是逐条罗列的执行细则、篇幅最长——独占整行（其余五格照旧自适应列宽），
    // 长文读得下，末行也不会露出 design-grid 的 1px 线底空格
    return h("div", { class: "design-cell" + (val ? "" : " miss")
        + (k2 === "style_notes" ? " wide" : "") },
      h("span", { class: "k" }, `${zh} · ${en}`),
      h("small", { class: "design-use" }, use),
      val ? h("p", null, val)
          : h("p", { class: "design-empty" }, "未填写"));
  })));

  // 导出中心：项目级提案书——放在最下（整卡可点，见 exportCard）
  view.append(secHeader(no(), "导出", "EXPORTS"));
  view.append(h("div", { class: "exp-grid" }, exportCard({
    title: "项目提案书", en: "PITCH DECK", kind: "pitch", pid: p.id,
    desc: "梗概 / 角色设定图 / 分集样张 / 规格与合规，单页 HTML——浏览器打印即 PDF，发平台或客户。",
    action: "⇪ 生成提案书" })));

  // 危险区（页面最底部独立模块）：删除项目从头部右上角挪到这里，二次确认 + 明确后果说明
  if (!p.is_deleted) {
    view.append(secHeader(no(), "危险区", "DANGER ZONE"));
    view.append(h("div", { class: "danger-zone" },
      h("div", { class: "dz-txt" },
        h("b", null, "删除此项目"),
        h("p", null,
          "移至回收站——数据、产物与库行完整保留，仅从清单与流程中隐去；",
          "项目页回收站可随时恢复（也可对 AI 说「恢复 ", p.title || pid, " 项目」）。")),
      h("button", { class: "dz-btn", onclick: doDelete,
        dataset: { tip: "移至回收站\n可随时从回收站一键恢复，数据不会丢失。" } },
        h("span", { class: "dz-btn-ic",
          html: '<svg viewBox="0 0 16 16"><path d="M2.5 4.5h11M6.5 2.5h3M4.5 4.5l.6 9h5.8l.6-9M6.7 7.2v4M9.3 7.2v4"/></svg>' }),
        "删除项目")));
  }
}

/* 平台规格达标卡：每集时长条 vs 模板区间 + 系列体量进度 */
function specCard(p) {
  const t = p.template;
  const ep = t.episode || {};
  const mins = ep.minutes;
  const hi = mins ? mins[1] * 1.25 : 1;
  const rows = (p.chapters || []).map((c) => {
    const m = (c.duration || 0) / 60;
    const ok = !mins || m === 0 ? null : (m >= mins[0] && m <= mins[1]);
    const shotsOk = !ep.shots || !c.shots ? null
      : (c.shots >= ep.shots[0] && c.shots <= ep.shots[1]);
    return h("div", { class: "spec-row" },
      h("span", { class: "spec-ch" }, c.id),
      h("div", { class: "spec-bar" },
        mins && h("i", { class: "spec-range",
          style: `left:${(mins[0] / hi) * 100}%;width:${((mins[1] - mins[0]) / hi) * 100}%` }),
        h("i", { class: "spec-fill" + (ok === false ? " bad" : ""),
          style: `width:${Math.min(100, (m / hi) * 100)}%` })),
      h("span", { class: "spec-val" + (ok === false ? " bad" : "") },
        m ? `${m.toFixed(1)} 分钟` : "—",
        c.shots ? h("i", { class: shotsOk === false ? "bad" : "" }, ` · ${c.shots} 镜`) : null));
  });
  const se = t.series || {};
  const totalM = (p.chapters || []).reduce((a, c) => a + (c.duration || 0), 0) / 60;
  const foot = h("div", { class: "spec-foot" },
    mins && h("span", null, `单集目标 ${mins[0]}–${mins[1]} 分钟`),
    ep.shots && h("span", null, `${ep.shots[0]}–${ep.shots[1]} 镜/集`),
    se.episodes && h("span", null, `目标 ${se.episodes[0]}–${se.episodes[1]} 集（现 ${p.chapters.length}）`),
    se.total_minutes && h("span", null,
      `总量 ${totalM.toFixed(1)} / ${se.total_minutes[0]}–${se.total_minutes[1]} 分钟`
      + `（${Math.min(100, (totalM / se.total_minutes[0]) * 100).toFixed(0)}%）`));
  return h("div", { class: "card spec-card" }, rows, foot,
    t.notes && h("div", { class: "shot-cap" }, "ⓘ ", t.notes));
}

/* 设定图候选宫格：点一下=定稿（旧稿自动备份、血缘传播、随时换选） */
function refCandGrid(pid, kind, name, cands, picked) {
  if (!(cands || []).length) return null;
  return h("div", { class: "refcand" }, cands.map((c) =>
    h("div", { class: "refcand-cell" + (picked === c.no ? " picked" : ""),
        title: picked === c.no ? "当前定稿（点其他候选可换选）" : `点选定稿候选 ${c.no}`,
        onclick: async (ev) => {
          ev.stopPropagation();
          const btn = ev.currentTarget;   // currentTarget 只在派发期有值，await 后恒 null
          try {
            const r = await runBusy(btn, "定稿中…", () =>
              post("/api/refpick", { project: pid, kind, name, no: c.no }));
            toast(`设定图已定稿（候选 ${c.no}）`
                  + (r.stale_retaken ? ` · 下游 ${r.stale_retaken} 镜标重做` : ""));
            await getOverview(true);
            render();
          } catch (err) { toast(err.message, true); }
        } },
      h("img", { src: c.url, loading: "lazy", alt: "" }),
      h("b", null, String(c.no)),
      picked === c.no && h("span", { class: "refcand-badge" }, "✓ 定稿"))));
}


/* 设定区「未生成」抽屉：没图的实体收起来、点击展开——卡片本体（调校设定/试音/
   生成入口）原样保留在抽屉里，折叠的是展示噪音不是功能。缺省收起、会话内不持久化。 */
function foldSection(content, n, zh = "设定图") {
  const body = h("div", { class: "fold-body", style: "display:none" });
  body.append(content);
  const btn = h("button", { class: "act-btn fold-toggle" },
    `▸ 未生成${zh} ${n} 项 · 点击展开（正典登记保留，按需再生成）`);
  btn.onclick = () => {
    const opened = body.style.display === "none";
    body.style.display = opened ? "" : "none";
    btn.textContent = (opened ? "▾ " : "▸ ")
      + `未生成${zh} ${n} 项` + (opened ? " · 点击收起" : " · 点击展开（正典登记保留，按需再生成）");
  };
  return h("div", { class: "fold-sec" }, btn, body);
}

/* ⇪ 素材直供（设定图）：选一张现成图直接替换标准设定图，**不调用任何模型**。
   旧图自动进版本栈（换错了可从版本谱系回滚），并向下游分镜传播血缘——设定图一换，
   引用它的分镜就过期了，不传播的话下游还挂着旧脸却仍显示"已通过"，是最坏的静默错误。
   走 /api/asset/supply 的原始字节通道（不 base64：设定图动辄几 MB）。 */
function supplySheetBtn(pid, kind, name, zh) {
  const who = name ? `「${name}」的` : "";   // 全局固定场景那一档无名（后端凭 kind 定位）
  const pick = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp",
                            style: "display:none" });
  const btn = h("button", { class: "act-btn",
    dataset: { tip: `⇪ 素材直供\n选一张现成图直接替换${who}${zh}，不调用模型、不花钱。`
      + "旧图自动进版本栈可回滚；下游引用它的分镜会被标为过期待重出。" },
    onclick: (e) => { e.stopPropagation(); pick.click(); } }, "⇪ 素材直供");
  pick.onchange = async () => {
    const f = pick.files && pick.files[0];
    pick.value = "";                       // 清空：同一张图连选两次也要能触发
    if (!f) return;
    try {
      const q = new URLSearchParams({ project: pid, kind, filename: f.name });
      if (name) q.set("name", name);
      const r = await runBusy(btn, "上传中…", async () => {
        const res = await fetch(`/api/asset/supply?${q}`, { method: "POST",
          headers: { "X-Csrf-Token": CSRF }, body: f });
        const j = await res.json();
        if (!res.ok || j.error) throw new Error(j.error || `HTTP ${res.status}`);
        return j;
      });
      const w = ((r.inspect || {}).warn || []).map((x) => x.msg).slice(0, 2);
      toast(`${who}${zh}已替换（旧版已归档）`
        + (r.stale_flagged ? ` · ${r.stale_flagged} 镜标为过期` : ""));
      if (w.length) toast(`⚠ 素材体检 · ${w.join("；")}`, true);
      await softRefreshProject(pid);
    } catch (err) { toast(err.message, true); }
  };
  return h("span", { class: "sheet-supply" }, btn, pick);
}

function characterGrid(characters, pid, bank) {
  if (!characters.length) {
    return emptyBlock("暂无角色预设", "角色即音色：一处定义，全系列一致。",
      `对 AI 说：为「${pid}」设计主要角色——外形、性格与音色一次定齐，再出角色设定图`);
  }
  const sheets = characters.filter((c) => c.sheet)
    .map((c) => ({ src: c.sheet, title: `CHARACTER · ${c.name}`, caption: charInfo(c),
                   actx: { pid, kind: "character", name: c.name, comments: c.comments || [] } }));
  return h("div", { class: "char-grid" }, characters.map((c) => {
    const idx = sheets.findIndex((s2) => s2.title.endsWith(c.name));
    // ⧉ 调校设定：诉求写在指令台里，与带定位坐标（project.json / characters[]）+ 现有
    // 设定的标准指令合并后复制，粘给 Claude Code 精修外貌 / 服装 / 武器 / 音色人设
    // ——设定不在网页里改
    const copyCharDirective = (e) => {
      e.stopPropagation();
      openDirectiveDialog({
        title: "调校设定指令", code: "CHARACTER · TUNE",
        meta: `角色「${c.name}」· 项目 ${pid}`,
        ask: "在此写要精修的外貌气质 / 服装配饰 / 武器道具 / 音色人设",
        hint: "例：眼神再冷一点，左眉留一道旧疤；外袍换成玄色暗纹，腰间挂玉牌",
        done: `「${c.name}」调校指令已复制——粘给 AI 精修设定`,
        directive: [
          `请调校角色「${c.name}」的设定 · 项目 ${pid}`,
          `定位坐标：project/${pid}/project.json → characters[]（name=${c.name}）`,
          `现有设定 —— 定位：${c.role || "未设"}｜外貌：${c.appearance || "未写"}`
            + `｜服装：${c.outfit || "—"}｜发型：${c.hair || "—"}｜武器：${c.weapon || "—"}`
            + (c.voice ? `｜音色：${c.voice}` : ""),
          `落地：改 characters[] 后——涉及外貌/服装/武器请重跑 \`project refs ${pid}\``
            + ` 更新设定图并同步各章；涉及音色走 voice 选角 / 定制。`,
        ].join("\n"),
      });
    };
    return h("div", { class: "card ccard" },
      h("div", { class: "ccard-sheet", onclick: c.sheet ? () => openLightbox(sheets, idx) : null },
        c.sheet ? h("img", { src: c.sheet, loading: "lazy", alt: c.name })
                : h("div", { class: "nosheet", html: ICON.person + "<span>NO SHEET · 待 project refs 生成</span>" }),
        assetVerBadge(pid, "character", c.name, c.versions)),
      refCandGrid(pid, "character", c.name, c.sheet_candidates, c.sheet_picked),
      h("div", { class: "ccard-body" },
        h("div", { class: "ccard-head" },
          h("div", { class: "ccard-name" }, h("h4", null, c.name),
            h("button", { class: "act-btn ccard-edit",
              dataset: { tip: "⧉ 调校本角色设定\n打开指令台：写下精修诉求，与带定位坐标"
                + "（project.json / characters[]）和现有设定的标准指令合并后复制，粘给 AI 精修"
                + "外貌 / 服装 / 武器 / 音色人设——设定不在网页里改。" },
              onclick: copyCharDirective }, "⧉ 调校设定"),
            supplySheetBtn(pid, "character", c.name, "角色设定图")),
          c.role && h("span", { class: "ccard-role" }, c.role)),
        c.appearance && h("div", { class: "ccard-desc" }, c.appearance),
        (c.outfit || c.hair || c.weapon) && h("div", { class: "ccard-attrs" },
          c.outfit && attrRow("服装", c.outfit),
          c.hair && attrRow("发型", c.hair),
          c.weapon && attrRow("武器", c.weapon)),
        voiceCasting(pid, (bank || {})[c.name])));
  }));
}

/* ═══ 选角卡（旁白与角色同构）═══════════════════════════════════════════════
   三段式，对应三件不同的事：

     在用    这个人现在是哪把声音——只此一行，写人话（定制写声线描述、模版写别名），
             绝不显示 `custom:vc_0003` 这类内部标识。
     候选    本批试音，**临时物**：刷新页面后不带任何选中状态，只标「未入档 /
             已入档」。选中态一旦挂在候选上，重新试音换掉整批之后，页面就会把
             另一条音频显示成「已选」（下标指向的东西变了）。
     档案    选过的每一把声音各一条，可试听、可换回、无引用才可删。这是唯一的
             选中真源。

   判据（在用哪条 / 引用账 / 候选有没有入档）全部由后端 `voicebank` 下发，
   前端只展示不自算——同一条规则写两份必然分叉。 */

const CAST_MODE_ZH = { custom: "定制", preset: "模版" };
/* 档案的人读名：定制没有别名，用那段造出它的声线描述（截断，全文进 title） */
const castLabel = (c) => c.alias || (c.prompt || "").trim() || "未命名音色";

/* 启用一把声音之后的播报：同步了几章、有几镜的配音与片段因此过期。
   过期数不报的话，一章会安静地停在一半旧声一半新声（`stage_tts` 只看 wav 在不在盘，
   它看不见音色换没换）。配音与片段两笔分开报、不合并成一个数：重跑配音零成本，
   重烧片段按秒计费，合并后无论写哪种处置对另一半都是错的。 */
function usedToast(r) {
  const zh = CAST_MODE_ZH[r.mode] || r.mode;
  let msg = `「${r.owner}」已启用${zh}音色，同步 ${r.chapters_synced} 个章节`;
  if (r.voice_retake) msg += ` · ${r.voice_retake} 镜配音已置重做`;
  if (r.voice_stale) msg += ` · ${r.voice_stale} 镜已通过审阅、只标过期`;
  if (r.clip_retake) msg += ` · ${r.clip_retake} 镜片段已置重做（重烧按秒计费）`;
  if (r.clip_stale) msg += ` · ${r.clip_stale} 镜片段已通过审阅、只标过期`;
  toast(msg);
}

function castRow(pid, c, no, impacted = 0) {
  const label = castLabel(c);
  const acts = [];
  if (!c.active) {
    acts.push(h("button", { class: "act-btn sm ok",
      dataset: { tip: `⇄ 换回这把声音\n此后这个人的每一句都用它合成，`
        + `并同步全部已建章节。档案不会消失，随时还能换回来。` },
      onclick: async (ev) => {
        ev.stopPropagation();
        const btn = ev.currentTarget;
        if (!(await confirmVoiceSwitch(impacted))) return;
        try {
          await runBusy(btn, "切换中…", async () => {
            usedToast(await post("/api/voice/use", { project: pid, cast: c.id }));
            await getOverview(true); render();
          }, { group: ".vb-casts" });
        } catch (err) { toast(err.message, true); }
      } }, "换回"));
  }
  const blocked = !c.refs.deletable;
  // 软禁用而非 disabled：disabled 元素不派发鼠标事件，「为什么删不了」的理由
  // 永远弹不出来——理由恰恰是这颗钮最需要传达的信息
  acts.push(h("button", { class: "act-btn sm no" + (blocked ? " soft-off" : ""),
    dataset: { tip: blocked ? `✕ 不能删除\n${c.refs.reason}`
      : "✕ 删除这条档案\n没有任何分镜用过它、也没有任何地方指派着它，删掉不影响任何产物。" },
    onclick: async (ev) => {
      ev.stopPropagation();
      if (blocked) { toast(c.refs.reason || "这条档案有引用，先解除引用再删", true); return; }
      // currentTarget 只在事件派发期有值：uiConfirm 一 await 回来它就是 null，
      // 后续 runBusy 读它直接抛错——删除永远发不出去
      const btn = ev.currentTarget;
      if (!(await uiConfirm(`删除音色档案「${label}」？这条音频将从盘上移除，不可恢复。`,
                            { title: "删除音色档案", danger: true }))) return;
      try {
        await runBusy(btn, "删除中…", async () => {
          await post("/api/voice/delete", { project: pid, cast: c.id });
          toast(`音色档案「${label}」已删除`);
          await getOverview(true); render();
        }, { group: ".vb-casts" });
      } catch (err) { toast(err.message, true); }
    } }, "删除"));
  return h("div", { class: "vb-cast" + (c.active ? " on" : "") },
    h("b", { class: "vb-no" }, `#${no}`),
    h("div", { class: "vb-cast-m" },
      h("span", { class: "vb-cast-name", title: c.prompt || c.alias || "" },
        h("i", null, CAST_MODE_ZH[c.mode] || c.mode), h("span", null, label)),
      h("span", { class: "shot-cap" }, String(c.at || "").slice(5, 16).replace("T", " "),
        c.refs.generated ? ` · ${c.refs.generated} 镜配音出自它` : "")),
    c.clip ? audioPill(c.clip) : h("span", { class: "shot-cap" }, "音频不在盘"),
    c.active ? h("span", { class: "chip green" }, "在用") : null,
    h("div", { class: "vb-cast-acts" }, acts));
}

/* 档案区：缺省收起（它是历史不是当前），在用那条已经钉在卡片顶部 */
function castLedger(pid, v) {
  if (!v.casts.length) return null;
  const body = h("div", { class: "fold-body", style: "display:none" },
    h("div", { class: "vb-casts" },
      v.casts.map((c, i) => castRow(pid, c, i + 1, voiceImpact(v)))));
  const label = (open) => `${open ? "▾" : "▸"} 音色档案 ${v.casts.length} 条`
    + (open ? " · 点击收起" : " · 点击展开（可试听 · 可换回 · 无引用可删）");
  const btn = h("button", { class: "act-btn fold-toggle" }, label(false));
  btn.onclick = () => {
    const open = body.style.display === "none";
    body.style.display = open ? "" : "none";
    btn.textContent = label(open);
  };
  return h("div", { class: "fold-sec" }, btn, body);
}

/* 在用行：卡片第一眼要回答的就是「这个人现在是哪把声音」 */
function activeLine(v, empty) {
  const cur = v.casts.find((c) => c.active);
  if (!cur) {
    return h("div", { class: "shot-cap" },
      v.voice ? `当前指派 ${v.voice}（未入档：手工写在设定里，没有可回听的音频）` : empty);
  }
  // 定制音色不重复那段声线描述：同一段文字就在下方「定制生成」的输入框里，
  // 而它长到会把这一行撑成一段。模版音色的别名是短标签，且不在别处出现，留着
  return h("div", { class: "voice-line" },
    chip(`✓ 在用 · ${CAST_MODE_ZH[cur.mode]}`, "green"),
    cur.mode === "custom"
      ? null
      : h("span", { class: "vb-active-name", title: cur.prompt || "" },
          castLabel(cur)),
    cur.clip ? audioPill(cur.clip) : null);
}

/* 当前在用声音已产出的配音镜数——启用/换回把它换掉时，这些镜会被血缘传播
   置重做或标过期，重配按字数计费。事前确认只在这个数非零时弹（选定是选角的
   主流程，无代价时多问一次是纯摩擦）。 */
function voiceImpact(v) {
  return (((v.casts || []).find((c) => c.active) || {}).refs || {}).generated || 0;
}

async function confirmVoiceSwitch(impacted) {
  if (!impacted) return true;
  return uiConfirm(
    `当前在用的声音已合成 ${impacted} 镜配音——启用新声音后，未锁定的镜会标记重做、`
    + "已通过审阅的只标过期（重配按字数计费）。继续？",
    { title: "启用音色" });
}

/* 候选行：**没有选中态**。已入档的只标状态（那条档案在档案区可试听可换回），
   其余给一个「选定」。 */
function auditionRows(pid, owner, entries, { custom, impacted = 0 }) {
  return entries.map((e) => h("div", { class: "vc-row" },
    h("b", { class: "vc-no" }, String(e.no)),
    e.media ? audioPill(e.media, custom ? `定制 ${e.no}` : e.voice)
            : h("span", { class: "shot-cap" }, custom ? `定制 ${e.no}` : e.voice),
    e.cast ? h("span", { class: "act-btn state",
                 dataset: { tip: "已入档\n这条已经立成音色档案（在下面的档案区可试听、可换回）。" } },
                "已入档")
           : h("button", { class: "act-btn ok",
               dataset: { tip: custom
                 ? "✓ 选定\n这条音频将成为这个人的音色本身：全片每一句都拿它当参考音"
                   + "合成，并立成一条可回听、可换回的档案。"
                 : "✓ 选定\n此后这个人的每一句都用这把官方音色，并立成一条档案；"
                   + "同步全部已建章节。" },
               onclick: async (ev) => {
                 ev.stopPropagation();
                 const btn = ev.currentTarget;
                 if (!(await confirmVoiceSwitch(impacted))) return;
                 try {
                   await runBusy(btn, "启用中…", async () => {
                     usedToast(await post("/api/voice/use",
                       { project: pid, owner, no: e.no, custom }));
                     await getOverview(true); render();
                   }, { group: ".vc-box" });
                 } catch (err) { toast(err.message, true); }
               } }, "选定")));
}

/* 模版生成：从官方音色目录里挑一把（候选按性别与角色扮演气质自动补足） */
function presetBox(pid, v) {
  const box = h("div", { class: "vc-box" });
  const entries = v.audition.entries || [];
  const rows = h("div", { class: "vc-rows" });
  // 头行随 paint 走：首批生成是就地换行不重绘，构建期一次性判断会让它永远不出现。
  // 另：`Element.append(null)` 会落一个字面 "null" 文本节点，条件节点不进 append
  const head = h("div", { class: "shot-cap" });
  const paint = (list) => {
    rows.replaceChildren(...auditionRows(pid, v.owner, list,
      { custom: false, impacted: voiceImpact(v) }));
    head.textContent = list.length ? `本批试音 ${list.length} 条` : "";
    head.hidden = !list.length;
  };
  paint(entries);
  box.append(head, rows);
  const gen = h("button", { class: "act-btn" },
    entries.length ? "↻ 重新试音" : "♪ 生成 5 条试音");
  gen.onclick = async () => {
    try {
      await runBusy(gen, "试音生成中…（约 10-30s）", async () => {
        const r = await post("/api/voice/audition", { project: pid, owner: v.owner });
        // 就地换候选行、不重绘：重绘会把人从当前页签甩走，而这一步刚花过钱
        paint(r.entries || []);
        v.audition = { batch: r.batch, entries: r.entries || [] };
        toast("试音已生成，逐条试听后点「选定」");
        getOverview(true);          // 后台刷新缓存，**不重绘**
      }, { group: ".vc-box" });
      // runBusy 收尾会把按钮还原成进场文案——首批生成后的改名必须排在它之后
      gen.textContent = "↻ 重新试音";
    } catch (err) { toast(err.message, true); }
  };
  box.append(gen);
  return box;
}

/* 定制生成：一段声线描述 → N 条演绎。与模版的区别在候选的含义——那边是「哪一把
   官方音色」，这边是「同一段描述的哪一次演绎」，选中的那条音频本身就是这把音色
   （全片每句拿它当参考音合成，生成式模型才不会逐句漂移）。 */
function customBox(pid, v) {
  const box = h("div", { class: "vc-box" });
  const block = v.custom_audition || {};
  const entries = block.entries || [];
  const ta = h("textarea", { class: "vd-desc", rows: 3,
    placeholder: "声线描述：年龄/性别/口音/音质/气质，如「少年男性，无方言口音，"
      + "标准普通话，嗓音清亮干净，朝气十足，少年感」" });
  ta.value = block.prompt || v.voice_prompt || "";
  box.append(ta);
  const rows = h("div", { class: "vc-rows" });
  const paint = (list) => rows.replaceChildren(
    ...auditionRows(pid, v.owner, list, { custom: true, impacted: voiceImpact(v) }));
  paint(entries);
  const hint = h("div", { class: "shot-cap" },
    entries.length ? `本批定制 ${entries.length} 条` : "");
  box.append(hint, rows);
  const gen = h("button", { class: "act-btn" },
    entries.length ? "↻ 重新生成定制" : "♪ 按描述生成 3 条定制");
  gen.onclick = async () => {
    const prompt = ta.value.trim();
    if (!prompt) { toast("先写一段声线描述", true); return; }
    try {
      await runBusy(gen, "声线生成中…（约 10-30s）", async () => {
        const r = await post("/api/voice/custom", { project: pid, owner: v.owner, prompt });
        paint(r.entries || []);
        hint.textContent = `本批定制 ${(r.entries || []).length} 条`;
        v.custom_audition = { batch: r.batch, prompt, entries: r.entries || [] };
        toast("定制已生成，试听后点「选定」");
        getOverview(true);          // 同上：后台刷新缓存，不重绘
      }, { group: ".vc-box" });
      // runBusy 收尾会把按钮还原成进场文案——首批生成后的改名必须排在它之后
      gen.textContent = "↻ 重新生成定制";
    } catch (err) { toast(err.message, true); }
  };
  box.append(gen);
  return box;
}

/* 两条路的页签容器。缺省落点=**在用那把声音是从哪条路来的**：页签回答的正是
   「这个人的声音怎么来的」。还没有在用音色时停在定制生成——那是缺省路径。

   落点绝不跟着候选跑：刚在模版页签点了「选定」，若因为定制那边还躺着几条旧候选
   就把人弹到定制生成，等于每次选定都换一次页。生成候选本身不重绘，所以正在挑的
   那一刻不会被打断。 */
function voiceRoutes(pid, v) {
  const wrap = h("div", { class: "vroutes" });
  const boxes = [{ key: "custom", label: "定制生成", el: customBox(pid, v) },
                 { key: "preset", label: "模版生成", el: presetBox(pid, v) }];
  const active = v.casts.find((c) => c.active);
  let mode = active ? active.mode : "custom";
  const tabEls = [];
  const tabs = h("div", { class: "vroute-tabs" });
  boxes.forEach((b) => {
    const el = h("button", { class: "vroute-tab" + (mode === b.key ? " on" : ""),
      onclick: () => {
        mode = b.key;
        tabEls.forEach((t, i) => t.classList.toggle("on", boxes[i].key === mode));
        boxes.forEach((bb) => (bb.el.hidden = bb.key !== mode));
      } }, b.label);
    tabEls.push(el); tabs.append(el);
  });
  boxes.forEach((b) => (b.el.hidden = b.key !== mode));
  wrap.append(tabs, ...boxes.map((b) => b.el));
  return wrap;
}

/* 一个实体的完整选角块（角色卡内嵌 / 旁白卡整卡） */
function castingBlock(pid, v, empty) {
  if (!v) return null;
  return h("div", { class: "vb" }, activeLine(v, empty), voiceRoutes(pid, v),
    castLedger(pid, v));
}

function narratorCard(p) {
  const v = (p.voice_bank || {})["旁白"];
  // 包一层 char-grid：与上面角色卡同一栅格列宽（minmax 340），宽度一致
  return h("div", { class: "char-grid" },
    h("div", { class: "card side-card" },
      h("span", { class: "k" }, "旁白选角 · NARRATOR CASTING"),
      castingBlock(p.id, v,
        "旁白还没有音色——按描述定制一把（缺省路径），或试音选官方模版。")));
}

function voiceCasting(pid, v) {
  return castingBlock(pid, v, "这个角色还没有音色——按描述定制一把（缺省路径），或试音选官方模版。");
}

const attrRow = (k2, v) => h("div", { class: "ccard-attr" }, h("span", { class: "k" }, k2), h("span", null, v));

/* ⧉ 调校（取景地/道具/固定场景通用，与角色卡的「⧉ 调校设定」同制度——窄卡
   190px 放不下四个字，缩成两字给名字让位）：需求写在指令台里，与带定位坐标
   + 现有设定 + 落地命令的标准指令合并后复制，粘给 Claude Code 修文字设定
   ——设定不在网页里改。
   落地必须点名 set 命令：scenes[]/props[] 是 Series.commit() 白名单管的数组，
   裸改 JSON 会被引擎长任务的旧内存副本整份覆写且不报错。
   需求行（末行「需求：<…>」）由指令台统一补，这里只管指令主干。 */
function tuneBtn(name, buildTxt, { code = "ASSET · TUNE", meta = "", ask = "",
                                    hint = "" } = {}) {
  return h("button", { class: "act-btn ccard-edit prcard-edit",
    dataset: { tip: "⧉ 调校本项设定\n打开指令台：写下精修需求，与带定位坐标和现有设定的"
      + "标准指令合并后复制，粘给 AI 修改文案 / 关键词并重生设定图——设定不在网页里改。" },
    onclick: (e) => {
      e.stopPropagation();
      openDirectiveDialog({
        title: "调校设定指令", code, meta: meta || name,
        ask: ask || "在此写要精修的方向",
        hint: hint || "写下要精修的方向——留空则原样复制模板",
        directive: buildTxt(),
        done: `「${name}」调校指令已复制——粘给 AI 修改设定`,
      });
    } }, "⧉ 调校设定");
}

/* 取景地卡：与 propCard 同版式；灯箱 actx 走 kind=scene + name（重生成/提意见/
   局部改造与版本谱系四处后端都按 name 分派，具名/全局互不串） */
function sceneCard(sc, pid, aspect) {
  // 卡片头图按**生成规格**显示（sheets.aspect_for 单一真源）：场景跟项目比例、
  // 道具恒 1:1 方图——统一成一个框就必然裁掉一边，而设定图正是拿来看全貌的
  const ar = String(aspect || "16:9").replace(":", "/");
  return h("div", { class: "card prcard", style: `--sheet-ar:${ar}` },
    h("div", { class: "ccard-sheet",
        onclick: sc.sheet ? () => openLightbox([{ src: sc.sheet, title: `SCENE · ${sc.name}`,
          caption: sc.desc,
          actx: { pid, kind: "scene", name: sc.name, comments: sc.comments || [] } }]) : null },
      sc.sheet ? h("img", { src: sc.sheet, loading: "lazy", alt: sc.name })
               : h("div", { class: "nosheet" }, h("span", null, "NO SHEET")),
      assetVerBadge(pid, "scene", sc.name, sc.versions)),
    refCandGrid(pid, "scene", sc.name, sc.sheet_candidates, sc.sheet_picked),
    h("div", { class: "prcard-body" },
      h("h5", { class: "prcard-title" }, sc.name),
      h("div", { class: "prcard-actions" },
          supplySheetBtn(pid, "scene", sc.name, "取景地设定图"),
          tuneBtn(sc.name, () => [
          `请调校取景地「${sc.name}」的设定 · 项目 ${pid}`,
          `定位坐标：project/${pid}/project.json → scenes[]（name=${sc.name}）`,
          `现有设定 —— 描述：${sc.desc || "未写"}｜关键词：${(sc.keywords || []).join("、") || "—"}`,
          `落地：走 \`scene set ${pid} --name ${sc.name} --desc "…"\`（关键词 --keyword，`
            + `绝不裸改 JSON 数组）；要更新设定图再跑 `
            + `\`project refs ${pid} --only scene:${sc.name} --force\`（旧图自动进版本栈）。`,
        ].join("\n"), { code: "SCENE · TUNE",
                        ask: "在此写要精修的空间结构 / 氛围光影 / 标志元素 / 命中关键词",
                        meta: `取景地「${sc.name}」· 项目 ${pid}`,
                        hint: "例：改成雨后黄昏，青石板反光；加一座半塌的牌坊作标志物" }),
        chip("场景")),
      sc.desc && h("p", null, sc.desc)));
}

/* 场景俯视卡：与取景地卡同版式，画布恒 16:9（`sheets.aspect_for("topview")` 单一
   真源——图纸不跟项目比例走，竖屏项目把平面图压成 9:16 就读不出左右站位）。
   灯箱 actx 走 kind=topview + name：重生成 / 提意见 / 局部改造 / 版本谱系四处后端
   都按 (kind, name) 分派，基准图与俯视图各走各的池，互不串。
   `sc.name` 为 null 即全局固定场景那一张（后端凭 kind 落 scene_topview_ref）。 */
function topviewCard(sc, pid) {
  const who = sc.name || "固定场景";
  const url = sc.topview;
  return h("div", { class: "card prcard", style: "--sheet-ar:16/9" },
    h("div", { class: "ccard-sheet",
        onclick: url ? () => openLightbox([{ src: url, title: `LAYOUT · ${who}`,
          caption: sc.desc,
          actx: { pid, kind: "topview", name: sc.name || null,
                  comments: sc.topview_comments || [] } }]) : null },
      url ? h("img", { src: url, loading: "lazy", alt: `${who} 俯视布局图` })
          : h("div", { class: "nosheet" }, h("span", null, "NO LAYOUT")),
      assetVerBadge(pid, "topview", sc.name || null, sc.topview_versions)),
    h("div", { class: "prcard-body" },
      h("h5", { class: "prcard-title" }, who),
      h("div", { class: "prcard-actions" },
        supplySheetBtn(pid, "topview", sc.name, "场景俯视图"),
        // 调校落在**空间**那一半：图纸画得准不准，取决于描述里有没有交代边界、
        // 出入口与陈设方位。文案坐标仍是 scenes[].desc（与基准图共用一份描述），
        // 但重生只重画图纸，不动已定稿的基准图。
        tuneBtn(who, () => [
          `请调校取景地「${who}」的空间结构 · 项目 ${pid}`,
          sc.name
            ? `定位坐标：project/${pid}/project.json → scenes[]（name=${sc.name}）的 desc`
            : `定位坐标：project/${pid}/project.json → 顶层 scene（全片同景的基准文案）`,
          `现有描述：${sc.desc || "未写"}`,
          "俯视图画的是这段描述里的空间信息——墙与出入口的位置、窗朝哪、"
            + "主要陈设各占哪一块、从哪进哪出。描述里没有的，图纸只能编。",
          sc.name
            ? `落地：走 \`scene set ${pid} --name ${sc.name} --desc "…"\`（绝不裸改 JSON 数组）；`
              + `只重画图纸跑 \`project refs ${pid} --only topview:${sc.name} --force\`，`
              + `连基准图一起重出用 \`--only scene:${sc.name} --force\`（两张成对，旧版自动入版本栈）。`
            : `落地：改顶层 scene 后，`
              + `只重画图纸跑 \`project refs ${pid} --only topview --force\`；`
              + "连基准图一起重出用 `--only scene --force`"
              + "（⚠ 两条不带名字，都会连带全部具名取景地）。",
        ].join("\n"), { code: "LAYOUT · TUNE",
                        ask: "在此写要精修的空间结构 —— 墙与出入口、窗朝向、陈设占位与朝向、可通行范围",
                        meta: `取景地「${who}」俯视 · 项目 ${pid}`,
                        hint: "例：里间与外间之间只有一道拱门；长桌靠东窗，收银台在门右手" }),
        chip("俯视图")),
      sc.desc && h("p", null, sc.desc)));
}

function propCard(p, pid) {
  const kindZh = p.kind === "weapon" ? "武器" : "道具";
  // 道具设定图恒 1:1（结构三视版式：上 2/3 正/侧/背三视并列 + 下 1/3 一排细节框）
  return h("div", { class: "card prcard", style: "--sheet-ar:1/1" },
    h("div", { class: "ccard-sheet",
        onclick: p.sheet ? () => openLightbox([{ src: p.sheet, title: `PROP · ${p.name}`, caption: p.desc,
                                                 actx: { pid, kind: "prop", name: p.name, comments: p.comments || [] } }]) : null },
      p.sheet ? h("img", { src: p.sheet, loading: "lazy", alt: p.name })
              : h("div", { class: "nosheet" }, h("span", null, "NO SHEET")),
      assetVerBadge(pid, "prop", p.name, p.versions)),
    refCandGrid(pid, "prop", p.name, p.sheet_candidates, p.sheet_picked),
    h("div", { class: "prcard-body" },
      h("h5", { class: "prcard-title" }, p.name),
      h("div", { class: "prcard-actions" },
          supplySheetBtn(pid, "prop", p.name, `${kindZh}设定图`),
          tuneBtn(p.name, () => [
          `请调校${kindZh}「${p.name}」的设定 · 项目 ${pid}`,
          `定位坐标：project/${pid}/project.json → props[]（name=${p.name}）`,
          `现有设定 —— 描述：${p.desc || "未写"}｜类型：${p.kind || "prop"}`
            + `｜关键词：${(p.keywords || []).join("、") || "—"}`,
          `落地：走 \`prop set ${pid} --name ${p.name} --desc "…"\`（类型 --kind · 关键词 --keyword，`
            + `绝不裸改 JSON 数组）；要更新设定图再跑 `
            + `\`project refs ${pid} --only prop:${p.name} --force\`（旧图自动进版本栈）。`,
        ].join("\n"), { code: "PROP · TUNE",
                        ask: "在此写要精修的形制材质 / 结构机构 / 尺度细节 / 命中关键词",
                        meta: `${kindZh}「${p.name}」· 项目 ${pid}`,
                        hint: "例：材质改成乌木包铜，刃口有缺；柄尾坠一枚旧铜铃" }),
        chip(kindZh)),
      p.desc && h("p", null, p.desc)));
}

/* —— 模块导出 —— */
export { attrRow, buildReader, castingBlock, characterGrid, customBox, entCard,
         narratorCard, presetBox, propCard, refCandGrid, renderEpisodeCompare, scriptAsk,
         scriptDropzone, scriptSelectTools, scriptToc, segLabel, sourceUpload, specCard,
         viewProject, viewScript, voiceCasting, voiceRoutes };
