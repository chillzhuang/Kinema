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

/* ═ Studio 前端模块 · app/panels.js — 版本历史面板 · 待审连审 · 看板（原生 ES Module·免构建）═ */

/* ---------------- 版本历史面板：谱系 · 参数对照 · 回滚 ---------------- */
import { chip, runBusy, uiSelect } from "./components.js";
import { $ } from "./core.js";
import { emptyBlock, render } from "../app.js";
import { MOTION, REVIEW, STAGE_ZH, api, fmtCost, fmtDur, fmtSec, getOverview, h, post,
         softRefresh, softRefreshProject, state, toast } from "./core.js";
import { audioPill, openLightbox, raiseModal, secHeader } from "./widgets.js";
import { renderRail } from "./shell.js";
import { statCell } from "./overview.js";
import { refreshAfterWrite } from "./shot-tools.js";

function openVPanel(d, s, stage) {
  const head = $("#vp-head");
  const body = $("#vp-body");
  head.innerHTML = "";
  body.innerHTML = "";
  const hist = ((s.version_history || {})[stage] || []).slice().reverse();
  const curV = ((s.versions || {})[stage] || 0) + 1;
  const curMedia = stage === "image" ? s.image : stage === "clip" ? s.clip : s.audio;
  const preview = (kind, src, cls) => {
    if (!src) return h("div", { class: "vp-none" }, "无产物");
    if (kind === "image") return h("img", { class: cls, src,
      onclick: () => openLightbox([{ src, title: `SHOT ${s.id}` }]) });
    if (kind === "clip") return h("video", { class: cls, src, controls: "", preload: "metadata" });
    return audioPill(src);
  };
  const params = (p) => p && h("div", { class: "vp-params" },
    p.prompt && h("p", null, h("b", null, "PROMPT "), String(p.prompt).slice(0, 180)),
    h("div", { class: "out-meta" },
      p.seed != null && h("span", null, `seed ${p.seed}`),
      p.provider && h("span", null, p.provider),
      p.voice && h("span", null, `♪ ${p.voice}`),
      p.mode && h("span", null, p.mode)));

  // 头部写进固定区（#vp-head 不随谱系滚动，关闭钮恒在场）
  head.append(
    h("span", { class: "k" }, `版本谱系 · SHOT ${String(s.id).padStart(2, "0")} · ${STAGE_ZH[stage]}`,
      h("span", { class: "chip amber", style: "margin-left:10px" },
        `当前 v${String(curV).padStart(3, "0")}`)));
  body.append(
    h("div", { class: "vp-row current" },
      h("div", { class: "vp-media" }, preview(stage, curMedia, "vp-thumb")),
      h("div", { class: "vp-info" },
        h("b", null, `v${String(curV).padStart(3, "0")} · 当前版`),
        params((s.gen || {})[stage]))));

  if (!hist.length) {
    body.append(h("div", { class: "empty", style: "padding:22px" },
      "暂无归档版本——重生成 / 打回重做 / 回滚时自动产生"));
  }
  hist.forEach((e) => {
    const src = (e.files || {}).main || Object.values(e.files || {})[0];
    body.append(h("div", { class: "vp-row" },
      h("div", { class: "vp-media" }, preview(stage, src, "vp-thumb")),
      h("div", { class: "vp-info" },
        h("b", null, `v${String(e.v).padStart(3, "0")}`),
        h("div", { class: "out-meta" },
          h("span", null, (e.at || "").replace("T", " ")),
          e.reason && h("span", { class: "vp-reason" }, e.reason)),
        params(e.params)),
      src && h("button", { class: "ghost-btn", onclick: async (ev) => {
        const _b = ev.currentTarget;
        try {
          // 回滚要拷回历史版 + 传播血缘，章节一多就有等待感——给忙态
          await runBusy(_b, "回滚中…", () => post("/api/rollback",
            { project: d.project, chapter: d.id, shot: s.id, stage, to: e.v }));
          toast(`已回滚至 v${e.v}（原当前版已归档）`);
          closeVPanel();
          refreshAfterWrite(d);
        } catch (err) { toast(err.message, true); } } }, "回滚到此版")));
  });
  $("#vpanel").hidden = false;
  raiseModal("vpanel");                       // 末次打开置顶（灯箱里点「版本谱系」→ 覆盖灯箱）
  document.body.style.overflow = "hidden";
}
/* 成片版本谱系：与 openVPanel 同一个面板壳，差别只在成片落章节顶层、按比例分谱系、
   且没有审阅阶段——故不复用 openVPanel（它的入参是「某一镜的某个阶段」），而是共用
   同一份 DOM 与回滚端点，避免为成片另开一个弹层。 */
function openOutputVPanel(d, aspect) {
  const head = $("#vp-head");
  const body = $("#vp-body");
  head.innerHTML = "";
  body.innerHTML = "";
  const hist = ((d.output_versions || {})[aspect] || []).slice().reverse();
  const cur = (d.outputs || []).find((o) => o.aspect === aspect && !o.watermarked);
  head.append(
    h("span", { class: "k" }, `版本谱系 · 成片 · ${aspect}`,
      h("span", { class: "chip amber", style: "margin-left:10px" },
        `当前 v${String(hist.length + 1).padStart(3, "0")}`)));
  body.append(
    h("div", { class: "vp-row current" },
      h("div", { class: "vp-media" },
        cur ? h("video", { class: "vp-thumb", src: cur.video, controls: "",
                           preload: "metadata" })
            : h("div", { class: "vp-none" }, "无产物")),
      h("div", { class: "vp-info" },
        h("b", null, `v${String(hist.length + 1).padStart(3, "0")} · 当前版`),
        cur && h("div", { class: "out-meta" }, h("span", null, cur.name)))));
  hist.forEach((e) => {
    body.append(h("div", { class: "vp-row" },
      h("div", { class: "vp-media" },
        h("video", { class: "vp-thumb", src: e.file, controls: "", preload: "metadata" })),
      h("div", { class: "vp-info" },
        h("b", null, `v${String(e.v).padStart(3, "0")}`),
        h("div", { class: "out-meta" },
          h("span", null, (e.at || "").replace("T", " ")),
          e.reason && h("span", { class: "vp-reason" }, e.reason))),
      h("button", { class: "ghost-btn", onclick: async (ev) => {
        const _b = ev.currentTarget;
        try {
          await runBusy(_b, "回滚中…", () => post("/api/rollback",
            { project: d.project, chapter: d.id, output_aspect: aspect, to: e.v }));
          toast(`成片 ${aspect} 已回滚至 v${e.v}（原当前版已归档；水印版需重打）`);
          closeVPanel();
          refreshAfterWrite(d);
        } catch (err) { toast(err.message, true); } } }, "回滚到此版")));
  });
  $("#vpanel").hidden = false;
  raiseModal("vpanel");
  document.body.style.overflow = "hidden";
}

function closeVPanel() {
  $("#vpanel").hidden = true;
  document.body.style.overflow = "";
}

/* 设定图版本谱系（角色/场景/道具）：自取项目数据渲染历次归档 + 一键回滚——与分镜 openVPanel
   同构，但设定图落系列文档、无 stage。主页设定图卡与灯箱均可打开；回滚后软刷新当前页。 */
async function openAssetVPanel(pid, kind, name) {
  let proj;
  try { proj = await api(`/api/project?id=${encodeURIComponent(pid)}`); }
  catch (err) { return toast(err.message, true); }
  // 分派与后端 `refine._asset_version_ctx` 同判据：kind 定字段族、name 定实体。
  // **具名取景地必须按 name 取自己那条**——丢了 name 就落到全局固定场景那一支，
  // 谱系面板里显示的是另一张图的历次归档，回滚按钮打在正确的资产上却看着错误的缩略图。
  let sheet, versions = 0, history = [];
  const pick = (e, mk, vk) => {
    sheet = e[mk]; versions = e[vk] || 0; history = e[`${vk.replace(/s$/, "")}_history`] || [];
  };
  if (kind === "character") {
    pick((proj.characters || []).find((c) => c.name === name) || {}, "sheet", "versions");
  } else if (kind === "prop") {
    pick((proj.props || []).find((p) => p.name === name) || {}, "sheet", "versions");
  } else if (kind === "topview") {
    if (name) pick((proj.scenes || []).find((x) => x.name === name) || {},
                   "topview", "topview_versions");
    else pick(proj, "scene_topview", "scene_topview_versions");
  } else if (name) {                       // 具名取景地
    pick((proj.scenes || []).find((x) => x.name === name) || {}, "sheet", "versions");
  } else {
    pick(proj, "scene_ref", "scene_versions");
  }
  const label = (name || (kind === "topview" ? "固定场景俯视" : "固定场景"))
    + (kind === "topview" && name ? " · 俯视" : "");
  const head = $("#vp-head"), body = $("#vp-body");
  head.innerHTML = ""; body.innerHTML = "";
  const curV = versions + 1;
  const preview = (src) => src
    ? h("img", { class: "vp-thumb", src, onclick: () => openLightbox([{ src, title: label }]) })
    : h("div", { class: "vp-none" }, "无产物");
  const meta = (e) => h("div", { class: "out-meta" },
    h("span", null, (e.at || "").replace("T", " ")),
    e.reason && h("span", { class: "vp-reason" }, e.reason));
  head.append(h("span", { class: "k" }, `版本谱系 · ${label} · 设定图`,
    h("span", { class: "chip amber", style: "margin-left:10px" }, `当前 v${String(curV).padStart(3, "0")}`)));
  body.append(h("div", { class: "vp-row current" },
    h("div", { class: "vp-media" }, preview(sheet)),
    h("div", { class: "vp-info" }, h("b", null, `v${String(curV).padStart(3, "0")} · 当前版`))));
  if (!history.length)
    body.append(h("div", { class: "empty", style: "padding:22px" },
      "暂无归档版本——重新生成 / 改造 / 回滚设定图时自动产生"));
  history.slice().reverse().forEach((e) => {
    body.append(h("div", { class: "vp-row" },
      h("div", { class: "vp-media" }, preview(e.url)),
      h("div", { class: "vp-info" }, h("b", null, `v${String(e.v).padStart(3, "0")}`), meta(e)),
      e.url && h("button", { class: "ghost-btn", onclick: async (ev) => {
        const _b = ev.currentTarget;
        try {
          await runBusy(_b, "回滚中…", () => post("/api/rollback",
            { project: pid, asset_kind: kind, asset_name: name || null, to: e.v }));
          toast(`已回滚至 v${e.v}（原当前版已归档，下游分镜按血缘标过期）`);
          closeVPanel();
          const r = state.route || {};
          if (r.name === "chapter") await softRefresh(r.pid, r.cid);
          else await softRefreshProject(pid);
        } catch (err) { toast(err.message, true); } } }, "回滚到此版")));
  });
  $("#vpanel").hidden = false;
  raiseModal("vpanel");                       // 末次打开置顶（灯箱里点「版本谱系」→ 覆盖灯箱）
  document.body.style.overflow = "hidden";
}

/* 设定图卡角标：归档≥1 时显「vN」→ 点开版本谱系（与分镜卡版本徽章同义）。versions<1 返 null。 */
function assetVerBadge(pid, kind, name, versions) {
  if (!(versions >= 1)) return null;
  return h("button", { class: "ver-badge",
    dataset: { tip: "版本谱系\n查看这张设定图的历次归档、一键回滚。" },
    onclick: (e) => { e.stopPropagation(); openAssetVPanel(pid, kind, name); } },
    `v${versions + 1}`);
}

/* ---------------- 视图：待审队列连审 · 两键表态 · 键盘 A/R ---------------- */
const Q = { items: [], focus: 0, restat: null };
async function viewQueue(view) {
  const q = await api("/api/queue");
  Q.items = q.items || []; Q.focus = 0; Q.restat = null;
  if (!Q.items.length) {
    view.append(secHeader("Q", "待审队列", "REVIEW QUEUE", 0));
    view.append(emptyBlock("没有待审产物", "生成阶段完成后产物会自动进入待审队列。", null));
    return;
  }
  // 工具条置顶（与看板同构）：队列是跨项目收件箱，默认全量，下拉可收窄到单个项目
  const projs = [{ value: "", label: "全部项目" }].concat(
    [...new Map(Q.items.map((x) => [x.project, x.project_title || x.project])).entries()]
      .filter(([k]) => k).map(([value, label]) => ({ value, label })));
  const flt = { pid: "", kw: "" };
  const projSel = uiSelect(projs, { value: "" });
  projSel.addEventListener("change", () => { flt.pid = projSel.value; paint(); });
  const kwBox = h("input", { class: "fsearch", type: "search",
    placeholder: "检索待审（项目 / 章节 / 镜号 / 阶段）…",
    oninput: () => { flt.kw = kwBox.value.trim().toLowerCase(); paint(); } });
  view.append(h("div", { class: "kb-toolbar" },
    h("span", { class: "bf-k" }, "项目"), projSel, kwBox));

  const hay = (it) => [it.project_title, it.chapter_title, `镜${it.shot}`, `shot ${it.shot}`,
    STAGE_ZH[it.stage], it.speaker, it.narration, it.prompt]
    .filter(Boolean).join(" ").toLowerCase();
  // 表过态的（_done）不回流；筛选口径只此一份，统计与卡片列表共用
  const passes = (it) => !it._done
    && (!flt.pid || it.project === flt.pid)
    && (!flt.kw || hay(it).includes(flt.kw));
  const lane = (it) => (it.kind === "candidates" ? "cand"
    : it.stage === "image" ? "image" : it.stage === "clip" ? "clip" : "audio");

  // 统计带 + 分章节积压：口径随工具条筛选走，表过一张即时回落
  const band = h("div", { class: "statband" });
  const distHead = secHeader("B", "积压分布", "BACKLOG BY CHAPTER", 0);
  const distBody = h("tbody");
  const distCard = h("div", { class: "card", style: "overflow:auto; margin-bottom:26px" },
    h("table", { class: "ptable heat" },
      h("thead", null, h("tr", null,
        ["章节", "画面", "动态", "配音", "宫格", "复审", "合计", "时长"]
          .map((t) => h("th", null, t)))),
      distBody));
  view.append(band, distHead, distCard);

  const head = secHeader("Q", "待审队列", "REVIEW QUEUE", Q.items.length);
  view.append(head);
  view.append(h("div", { class: "shot-cap", style: "margin:-6px 0 14px" },
    "键盘：↑↓ 切换 · A 通过 · R 打回（弹出意见输入）· 点击画面可放大打点评论"));
  const list = h("div", { class: "queue-list" });
  view.append(list);

  const paintStats = () => {
    const I = Q.items.filter(passes);
    const byLane = (k) => I.filter((it) => lane(it) === k).length;
    const resub = I.filter((it) => (it.version || 1) > 1).length;
    const secs = I.reduce((a, it) => a + (Number(it.dur) || 0), 0);
    band.innerHTML = "";
    band.append(
      statCell(I.length, "", "待审 · PENDING"),
      statCell(byLane("image"), "", "画面 · IMAGE"),
      statCell(byLane("clip"), "", "动态 · MOTION"),
      statCell(byLane("audio"), "", "配音 · VOICE"),
      statCell(byLane("cand"), "", "宫格待选 · CANDIDATES"),
      statCell(resub, "", "复审 · RESUBMITS"),
      statCell(fmtDur(secs), "", "素材时长 · RUNTIME"));

    const groups = new Map();
    I.forEach((it) => {
      const key = `${it.project}/${it.chapter}`;
      const g = groups.get(key) || { title: `${it.project_title} / ${it.chapter_title}`,
        kw: it.chapter_title || "", image: 0, clip: 0, audio: 0, cand: 0,
        resub: 0, n: 0, secs: 0 };
      g[lane(it)]++; g.n++; g.secs += Number(it.dur) || 0;
      if ((it.version || 1) > 1) g.resub++;
      groups.set(key, g);
    });
    const rows = [...groups.values()].sort((a, b) => b.n - a.n);
    distHead.querySelector(".cnt").textContent = String(rows.length);
    distHead.hidden = distCard.hidden = !rows.length;   // 空筛选下整块收起，别留一张空表
    distBody.innerHTML = "";
    rows.forEach((g) => {
      const cell = (n, cls) => {
        const td = h("td", { class: "heat-cell " + (n ? cls : "") }, String(n || "—"));
        if (n) td.style.opacity = String(0.45 + 0.55 * Math.min(1, n / g.n));
        return td;
      };
      distBody.append(h("tr", { class: "heat-row",
          dataset: { tip: "点击把检索收窄到该章节——清空检索框即回全量。" },
          onclick: () => { kwBox.value = g.kw; flt.kw = g.kw.toLowerCase(); paint(); } },
        h("td", { class: "pname" }, g.title),
        cell(g.image, "amber"), cell(g.clip, "amber"), cell(g.audio, "amber"),
        cell(g.cand, "amber"), cell(g.resub, "red"),
        h("td", { class: "mono" }, String(g.n)),
        h("td", { class: "mono" }, fmtDur(g.secs))));
    });
    head.querySelector(".cnt").textContent = String(I.length);
  };
  // 卡片下标恒用 Q.items 原序，键盘焦点与 gone() 才对得上
  const paint = () => {
    list.innerHTML = "";
    let shown = 0;
    Q.items.forEach((it, n) => {
      if (!passes(it)) return;
      list.append(queueCard(it, n));
      shown++;
    });
    if (!shown)
      list.append(h("div", { class: "empty", style: "padding:26px" }, "没有匹配的待审产物"));
    paintStats();
    syncQueueFocus();
  };
  Q.restat = paintStats;                     // 表过一张，统计与计数即时回落（不重排列表）
  paint();
}
function queueCard(it, n) {
  const gone = () => {
    const card = $(`[data-qi="${n}"]`);
    if (card) { card.classList.add("gone"); setTimeout(() => card.remove(), 260); }
    Q.items[n]._done = true;
    Q.restat && Q.restat();
    getOverview(true).then(() => renderRail(state.overview));
  };
  const doPick = async (no) => {
    try {
      await post("/api/pick", { project: it.project, chapter: it.chapter,
        shot: it.shot, no });
      toast(`${it.chapter_title} · 镜${it.shot} 已定稿候选 #${no}`);
      gone();
    } catch (err) { toast(err.message, true); }
  };
  const mediaEl =
    it.kind === "candidates"
      ? h("div", { class: "cand-grid q" }, (it.candidates || []).map((c) =>
          h("div", { class: "cand", title: `点选候选 #${c.no} 定稿`,
              onclick: () => doPick(c.no) },
            h("img", { src: c.url, loading: "lazy", alt: `候选 ${c.no}` }),
            h("b", null, String(c.no)))))
      : it.kind === "image" && it.media
      ? h("img", { src: it.media, loading: "lazy",
          onclick: () => openLightbox([{ src: it.media,
            title: `SHOT ${String(it.shot).padStart(2, "0")}`, caption: it.prompt,
            ctx: { pid: it.project, cid: it.chapter, shot: it.shot,
                   stage: it.stage, comments: [] } }]) })
      : it.kind === "video" && it.media
      ? h("video", { src: it.media, controls: "", preload: "metadata" })
      : it.media ? audioPill(it.media, "配音") : h("div", { class: "ph" }, "无媒体");
  const act = async (state, note) => {
    try {
      await post("/api/review", { project: it.project, chapter: it.chapter,
        shots: [it.shot], stage: it.stage, state, note });
      toast(`${it.chapter_title} · 镜${it.shot} ${STAGE_ZH[it.stage]} → ${state === "done" ? "通过" : "重做"}`);
      gone();
    } catch (err) { toast(err.message, true); }
  };
  const noteBox = h("div", { class: "retake-box", hidden: "" });
  const openNote = () => {
    noteBox.hidden = false; noteBox.innerHTML = "";
    const input = h("input", { class: "cmt-input", type: "text",
      placeholder: "重做意见（将编译进下一版提示词）…" });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.value.trim()) act("retake", input.value.trim());
      e.stopPropagation();
    });
    noteBox.append(input,
      h("button", { class: "act-btn no",
        onclick: () => input.value.trim() && act("retake", input.value.trim()) }, "↻ 确认打回"),
      h("button", { class: "act-btn", onclick: () => { noteBox.hidden = true; } }, "取消"));
    input.focus();
  };
  const card = h("div", { class: "card qcard", dataset: { qi: n }, tabindex: "0",
    onfocus: () => { Q.focus = n; syncQueueFocus(); } },
    h("div", { class: "q-media" }, mediaEl),
    h("div", { class: "q-info" },
      h("div", { class: "chips" },
        chip(`SHOT ${String(it.shot).padStart(2, "0")}`, "amber"),
        it.kind === "candidates"
          ? chip(`宫格待选 · ${(it.candidates || []).length} 选 1`, "cyan")
          : chip(`${STAGE_ZH[it.stage]} · v${it.version}`),
        it.dur && chip(fmtSec(it.dur))),
      h("a", { class: "q-src", href: `#/project/${encodeURIComponent(it.project)}/${encodeURIComponent(it.chapter)}` },
        `${it.project_title} / ${it.chapter_title} →`),
      it.narration && h("div", { class: "shot-narr" },
        it.speaker ? `${it.speaker}：${it.narration}` : it.narration),
      it.prompt && h("div", { class: "shot-cap" }, "提示词 · ", it.prompt),
      noteBox),
    h("div", { class: "q-actions" },
      it.kind !== "candidates" &&
        h("button", { class: "act-btn ok big", onclick: () => act("done") }, "✓ 通过"),
      h("button", { class: "act-btn no big", onclick: openNote },
        it.kind === "candidates" ? "↻ 全部重出" : "↻ 重做")));
  card._openNote = openNote;
  card._approve = it.kind === "candidates" ? null : () => act("done");
  return card;
}
function syncQueueFocus() {
  document.querySelectorAll(".qcard").forEach((c) =>
    c.classList.toggle("focus", Number(c.dataset.qi) === Q.focus));
}
function queueKeys(e) {
  if (state.route?.name !== "queue") return;
  if (e.target.tagName === "INPUT") return;
  const cards = [...document.querySelectorAll(".qcard:not(.gone)")];
  if (!cards.length) return;
  const cur = cards.find((c) => Number(c.dataset.qi) === Q.focus) || cards[0];
  const idx = cards.indexOf(cur);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const nx = cards[Math.min(cards.length - 1, Math.max(0, idx + (e.key === "ArrowDown" ? 1 : -1)))];
    Q.focus = Number(nx.dataset.qi);
    syncQueueFocus();
    nx.scrollIntoView({ block: "center", behavior: "smooth" });
    e.preventDefault();
  } else if (e.key === "a" || e.key === "A") {
    cur._approve && cur._approve();
  } else if (e.key === "r" || e.key === "R") {
    cur._openNote && cur._openNote();
  }
}
document.addEventListener("keydown", queueKeys);

/* ---------------- 视图：看板状态全景 · 烧钱 · 重roll ---------------- */
const BOARD_COLS = ["todo", "wip", "wfa", "retake", "done"];
async function viewBoard(view) {
  const b = await api("/api/board");
  const items = b.items || [], chapters = b.chapters || [];
  if (!items.length && !chapters.length) {
    view.append(emptyBlock("看板还是空的", "跑一条章节流水线后，产物按状态在此分桶。", null));
    return;
  }
  // 项目维度分组：看板一次只看一个项目（下拉切换，不再全量堆积）
  const projs = [...new Map(items.concat(chapters)
    .map((x) => [x.project, x.project_title || x.project])).entries()]
    .filter(([k]) => k).map(([value, label]) => ({ value, label }));
  const flt = { pid: projs[0]?.value || "", kw: "" };
  const projSel = uiSelect(projs, { value: flt.pid });
  projSel.addEventListener("change", () => { flt.pid = projSel.value; paint(); });
  view.append(h("div", { class: "kb-toolbar" },
    h("span", { class: "bf-k" }, "项目"), projSel,
    h("input", { class: "fsearch", type: "search",
      placeholder: "检索卡片（章节 / 镜号 / 阶段）…",
      oninput: (e) => { flt.kw = e.target.value.trim().toLowerCase(); paint(); } })));
  const bodyEl = h("div");
  view.append(bodyEl);

  const paint = () => {
    const body = bodyEl;
    body.innerHTML = "";
    const I = items.filter((it) => it.project === flt.pid && (!flt.kw
      || `${it.chapter_title} 镜${it.shot} ${STAGE_ZH[it.stage] || ""}`
        .toLowerCase().includes(flt.kw)));
    const C = chapters.filter((c) => c.project === flt.pid);
    const byState = Object.fromEntries(BOARD_COLS.map((s) => [s, []]));
    I.forEach((it) => (byState[it.state] || (byState[it.state] = [])).push(it));
    const waste = C.reduce((a, c) => a + (c.waste || 0), 0);
    const rerolls = C.reduce((a, c) => a + (c.rerolls || 0), 0);
    // 逐章合计由服务端下发（business.chapter_ledger），这里只把各章相加
    const spent = C.reduce((a, c) => a + (c.cost_total || 0), 0);

    body.append(h("div", { class: "statband" },
      statCell(I.length, "", "产物 · DELIVERABLES"),
      statCell((byState.wfa || []).length, "", "待审 · PENDING"),
      statCell((byState.retake || []).length, "", "重做 · RETAKE"),
      statCell((byState.done || []).length, "", "已锁定 · LOCKED"),
      statCell(`¥${spent.toFixed(1)}`, "", "累计成本 · SPENT"),
      statCell(`¥${waste.toFixed(1)}`, "", "废片成本 · WASTE"),
      statCell(`${rerolls}`, "", "重ROLL · RE-ROLLS")));

    // 状态看板：五列，卡片=分镜×产物
    body.append(secHeader("01", "状态看板", "STATUS KANBAN", I.length));
    body.append(h("div", { class: "kanban" }, BOARD_COLS.map((st) => {
      const list = byState[st] || [];
      const r = REVIEW[st] || { zh: st, cls: "" };
      // 拖拽表态：卡片拖入目标列 = review set（与两键表态同一条写路径）
      const col = h("div", { class: "kb-col",
        ondragover: (e) => { e.preventDefault(); col.classList.add("dropping"); },
        ondragleave: () => col.classList.remove("dropping"),
        ondrop: async (e) => {
          e.preventDefault();
          col.classList.remove("dropping");
          try {
            const it = JSON.parse(e.dataTransfer.getData("text/plain") || "{}");
            if (!it.project || it.state === st) return;
            await post("/api/review", { project: it.project, chapter: it.chapter,
              shots: [it.shot], stage: it.stage, state: st });
            toast(`镜${it.shot}·${STAGE_ZH[it.stage]} → ${r.zh}`);
            render();
          } catch (err) { toast(err.message, true); }
        } },
        h("div", { class: "kb-head" },
          h("span", { class: `chip ${r.cls}` }, r.zh),
          h("span", { class: "kb-cnt" }, String(list.length))),
        h("div", { class: "kb-list" }, list.slice(0, 60).map((it) =>
          h("a", { class: "kb-card", draggable: "true",
              href: `#/project/${encodeURIComponent(it.project)}/${encodeURIComponent(it.chapter)}`,
              dataset: { tip: `${it.project_title} / ${it.chapter_title}\n`
                + "拖拽到其他列即改审阅状态（与 CLI 同一条写路径）；点击打开章节。" },
              ondragstart: (e) => e.dataTransfer.setData("text/plain", JSON.stringify({
                project: it.project, chapter: it.chapter, shot: it.shot,
                stage: it.stage, state: it.state })) },
            it.thumb ? h("img", { src: it.thumb, loading: "lazy", alt: "" })
                     : h("span", { class: "kb-ph" }, STAGE_ZH[it.stage]),
            h("span", { class: "kb-meta" },
              h("b", null, `镜${it.shot}·${STAGE_ZH[it.stage]}`),
              h("i", null, it.chapter_title),
              it.versions > 1 ? h("em", null, `v${it.versions}`) : null))),
          list.length > 60 ? h("div", { class: "kb-more" }, `… 共 ${list.length} 项`) : null));
      return col;
    })));

    // 章节热力：每章 × 产物阶段 完成度 + 烧钱/废片/重roll
    body.append(secHeader("02", "章节热力 · 运营", "HEAT & BURN", C.length));
    const heat = h("table", { class: "ptable heat" },
      h("thead", null, h("tr", null,
        ["章节", "模式", "分镜", "待审", "重做", "通过", "成本", "废片", "重roll"]
          .map((t) => h("th", null, t)))),
      h("tbody", null, C.map((c) => {
        const stt = c.states || {};
        const total = Object.values(stt).reduce((a, v) => a + v, 0) || 1;
        const cell = (n, cls) => {
          const td = h("td", { class: "heat-cell " + (n ? cls : "") }, String(n || "—"));
          if (n) td.style.opacity = String(0.45 + 0.55 * Math.min(1, n / total));
          return td;
        };
        return h("tr", { class: "heat-row",
            onclick: () => (location.hash =
              `#/project/${encodeURIComponent(c.project)}/${encodeURIComponent(c.chapter)}`) },
          h("td", { class: "pname" }, `${c.project_title} / ${c.title}`),
          h("td", null, MOTION[c.motion]?.key || "—"),
          h("td", null, String(c.shots)),
          cell(stt.wfa, "amber"), cell(stt.retake, "red"), cell(stt.done, "green"),
          h("td", { class: "mono" }, c.cost_total != null ? fmtCost(c.cost_total, c.currency) : "—"),
          h("td", { class: "mono" + (c.waste ? " bad" : "") },
            c.waste ? `¥${c.waste.toFixed(2)}` : "—"),
          h("td", { class: "mono" }, c.rerolls ? `${c.rerolls}` : "—"));
      })));
    body.append(h("div", { class: "card", style: "overflow:auto" }, heat));

    // 重roll 榜：被反复重做的镜（每一次都是钱）
    const top = I.filter((x) => x.versions > 1)
      .sort((a, b) => b.versions - a.versions).slice(0, 8);
    if (top.length) {
      body.append(secHeader("03", "重 ROLL 榜", "RE-ROLL LEADERBOARD", top.length));
      body.append(h("div", { class: "reroll-list" }, top.map((it) =>
        h("a", { class: "reroll-row",
            href: `#/project/${encodeURIComponent(it.project)}/${encodeURIComponent(it.chapter)}` },
          it.thumb ? h("img", { src: it.thumb, alt: "" }) : h("span", { class: "kb-ph" }, "—"),
          h("b", null, `v${it.versions}`),
          h("span", null, `${it.project_title} / ${it.chapter_title} · 镜${it.shot} · ${STAGE_ZH[it.stage]}`),
          h("span", { class: `chip ${REVIEW[it.state]?.cls || ""}` }, REVIEW[it.state]?.zh || it.state)))));
    }
  };
  paint();
}

/* —— 模块导出 —— */
export { BOARD_COLS, Q, assetVerBadge, closeVPanel, openAssetVPanel,
         openOutputVPanel, openVPanel, queueCard,
         queueKeys, syncQueueFocus, viewBoard, viewQueue };
