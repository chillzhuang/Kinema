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

/* ═ Studio 前端模块 · app/ledger.js — 导出中心 · 成本台账 · 片库（原生 ES Module·免构建）═ */

/* ---------------- 导出中心：出片单卡片（按钮态机 idle→working→done） ---------------- */
import { uiSelect } from "./components.js";
import { emptyBlock } from "../app.js";
import { MOTION, REVIEW, api, fmtDur, getOverview, h, post, toast } from "./core.js";
import { secHeader } from "./widgets.js";
import { statCell } from "./overview.js";
import { videoCard } from "./playbook.js";

function exportCard({ title, en, desc, kind, pid, cid, action }) {
  const out = h("div", { class: "exp-out", style: "display:none",
    dataset: { tip: "点击复制完整路径" } });
  out.addEventListener("click", (e) => {
    e.stopPropagation();   // 复制路径不触发整卡的再导出
    navigator.clipboard?.writeText(out.dataset.path || "").then(() => toast("路径已复制"));
  });
  const go = h("span", { class: "exp-go" }, action);
  const card = h("div", { class: "card exp-card", role: "button", tabindex: "0" },
    h("div", { class: "exp-head" },
      h("div", { class: "exp-tt" }, h("b", null, title), h("i", null, en)), go),
    h("div", { class: "exp-desc" }, desc), out);
  const run = async () => {
    if (card.classList.contains("working")) return;
    card.classList.add("working"); go.textContent = "导出中…";
    try {
      const r = await post("/api/export", { kind, project: pid, chapter: cid });
      card.classList.remove("working"); card.classList.add("done");
      go.textContent = "↻ 重新导出";
      out.style.display = ""; out.dataset.path = r.path; out.innerHTML = "";
      const parts = (r.path || "").split("/");
      out.append(h("b", null, parts.pop() || r.path),
        h("span", null, parts.join("/") + "/"),
        r.hint ? h("i", null, r.hint) : null);
      toast("导出完成");
    } catch (err) {
      card.classList.remove("working"); go.textContent = action;
      toast(err.message, true);
    }
  };
  card.onclick = run;
  card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); run(); } };
  return card;
}

/* 交付：章节级审阅包 + 交付包 */
function deliveryCard(d) {
  const card = h("div", { class: "card side-card" });
  card.append(h("span", { class: "k" }, "交付 · DELIVERY"));
  card.append(h("div", { class: "exp-grid", style: "grid-template-columns:1fr;margin-top:10px" },
    exportCard({ title: "静态审阅包", en: "CLIENT REVIEW", kind: "review",
      pid: d.project, cid: d.id,
      desc: "免登录单页审阅书 + 自包含媒体，客户离线可开。",
      action: "⇪ 导出审阅包" }),
    exportCard({ title: "交付包", en: "DELIVERY ZIP", kind: "deliver",
      pid: d.project, cid: d.id,
      desc: "成片(多比例) + 封面 + 双字幕 + 平台文案 + manifest（AI 披露/版权）。",
      action: "⇪ 打包交付" })));
  return card;
}

/* ---------------- 视图：成本成本台账 ---------------- */
async function viewCost(view) {
  const b = await api("/api/cost");
  const all = (b.projects || []).filter((p) => (p.chapters || []).length);
  if (!all.length) {
    view.append(emptyBlock("还没有可记账的项目",
      "跑一条章节流水线后，这里出现成本台账。",
      "对 AI 说：帮我把《你的主题》做成一集短片——每笔生成开销会自动记到这里"));
    return;
  }
  // 筛选：项目 × 时间区间（按章节创建时间），统计带随筛选实时重算
  const flt = { pid: "", days: 0 };
  const projSel = uiSelect([{ value: "", label: "全部项目" },
    ...all.map((p) => ({ value: p.project, label: p.title }))]);
  const timeSel = uiSelect([{ value: "0", label: "全部时间" },
    { value: "7", label: "近 7 天" }, { value: "30", label: "近 30 天" },
    { value: "90", label: "近 90 天" }]);
  projSel.addEventListener("change", () => { flt.pid = projSel.value; paint(); });
  timeSel.addEventListener("change", () => { flt.days = +timeSel.value; paint(); });
  // 页级标题与侧栏菜单同名同英文（成本 · COST）；项目小节的 LEDGER 是下一级台账
  view.append(secHeader("¥", "成本", "COST", all.length));
  view.append(h("div", { class: "biz-filter" },
    h("span", { class: "bf-k" }, "筛选"), projSel, timeSel));
  const body = h("div");
  view.append(body);

  const paint = () => {
    body.innerHTML = "";
    const cutoff = flt.days ? Date.now() - flt.days * 864e5 : null;
    const projects = all
      .filter((p) => !flt.pid || p.project === flt.pid)
      .map((p) => {
        const chs = (p.chapters || []).filter((r) => !cutoff
          || (r.created_at && new Date(r.created_at).getTime() >= cutoff));
        if (!chs.length) return null;
        const tt = chs.reduce((a, r) => ({
          actual: a.actual + (r.actual_total || 0), waste: a.waste + (r.waste || 0),
          rerolls: a.rerolls + (r.rerolls || 0), duration: a.duration + (r.duration || 0),
          shots: a.shots + (r.shots || 0),
          estimate_video: a.estimate_video + (r.estimate_video || 0),
        }), { actual: 0, waste: 0, rerolls: 0, duration: 0, shots: 0, estimate_video: 0 });
        // 系列级支出（设定图/主视觉/试音/资产局改/锚定预热）不按章节分摊、不随时间筛选，
        // 服务端 totals.series_total 单列并计入实际；单镜均价仍按章节支出算
        const series = (p.totals || {}).series_total || 0;
        return { ...p, chapters: chs, totals: { ...tt, actual: tt.actual + series,
          series_total: series,
          waste_ratio: tt.actual ? tt.waste / tt.actual : 0,   // 与 business.py 同式（waste/actual）
          cost_per_shot: tt.shots ? tt.actual / tt.shots : 0,
          rerolls_per_shot: tt.shots ? tt.rerolls / tt.shots : 0 } };
      }).filter(Boolean);
    const T = projects.reduce((a, p) => ({
      actual: a.actual + p.totals.actual, waste: a.waste + p.totals.waste,
      est: a.est + (p.totals.estimate_video || 0), rerolls: a.rerolls + p.totals.rerolls,
      minutes: a.minutes + p.totals.duration / 60,
    }), { actual: 0, waste: 0, est: 0, rerolls: 0, minutes: 0 });
    const shotsN = projects.reduce((a, p) => a + (p.totals.shots || 0), 0);
    const spent = statCell(`¥${T.actual.toFixed(1)}`, "", "实际成本 · SPENT");
    spent.classList.add("amber");
    const waste = statCell(`¥${T.waste.toFixed(1)}`, "", "废片成本 · WASTE");
    if (T.waste) waste.classList.add("red");
    body.append(h("div", { class: "statband" },
      spent,
      statCell(T.est ? `¥${T.est.toFixed(1)}` : "—", "", "预估(video) · ESTIMATED"),
      waste,
      statCell(String(T.rerolls), "", "重ROLL · RE-ROLLS"),
      statCell(`${T.minutes.toFixed(1)}m`, "", "总时长 · RUNTIME"),
      statCell(shotsN ? `¥${(T.actual / shotsN).toFixed(2)}` : "—", "", "单镜均价 · PER SHOT"),
      statCell(T.minutes ? `¥${(T.actual / T.minutes).toFixed(1)}` : "—", "", "分钟成本 · PER MINUTE")));
    if (!projects.length) {
      body.append(emptyBlock("筛选区间内没有台账记录", "换个时间区间或项目试试。", null));
      return;
    }
    projects.forEach((p, i) => {
      body.append(h("div", { class: "biz-block" },
        secHeader(String(i + 1).padStart(2, "0"), p.title, `LEDGER · ${p.project}`),
        h("div", { class: "biz-proj" }, ledgerCard(p))));
    });
  };
  paint();
}

function ledgerCard(p) {
  const t = p.totals;
  const scale = Math.max(t.estimate_video || 0, t.actual + t.waste, 1e-4);
  return h("div", { class: "card val-card" },
    h("span", { class: "k" }, "成本台账 · COST LEDGER",
      p.template && h("span", { class: "chip" }, p.template)),
    h("table", { class: "ledger" },
      h("thead", null, h("tr", null,
        ["章节", "时长", "镜", "预估", "实际", "废片", "重roll"].map((x) => h("th", null, x)))),
      h("tbody", null, p.chapters.map((r) => h("tr", null,
        h("td", null, r.title || r.chapter),
        h("td", null, fmtDur(r.duration)),
        h("td", null, String(r.shots)),
        h("td", { class: r.estimate_video ? "" : "dim" },
          r.estimate_video ? `¥${r.estimate_video.toFixed(2)}` : "—"),
        h("td", null, `¥${r.actual_total.toFixed(2)}`),
        h("td", { class: r.waste ? "red" : "dim" }, r.waste ? `¥${r.waste.toFixed(2)}` : "—"),
        h("td", { class: r.rerolls ? "" : "dim" }, r.rerolls ? String(r.rerolls) : "—")))),
      h("tfoot", null,
        t.series_total ? h("tr", null,
          h("td", null, "系列（设定图/主视觉/试音）"),
          h("td", null, "—"), h("td", null, "—"), h("td", { class: "dim" }, "—"),
          h("td", null, `¥${t.series_total.toFixed(2)}`),
          h("td", { class: "dim" }, "—"), h("td", { class: "dim" }, "—")) : null,
        h("tr", null,
        h("td", null, "合计"),
        h("td", null, fmtDur(t.duration)),
        h("td", null, String(t.shots)),
        h("td", null, t.estimate_video ? `¥${t.estimate_video.toFixed(2)}` : "—"),
        h("td", null, `¥${t.actual.toFixed(2)}`),
        h("td", { class: t.waste ? "red" : "" }, t.waste ? `¥${t.waste.toFixed(2)}` : "—"),
        h("td", null, String(t.rerolls))))),
    h("div", { class: "burnwrap" },
      h("div", { class: "burnbar" },
        h("i", { class: "act", style: `width:${(t.actual / scale) * 100}%` }),
        h("i", { class: "wst", style: `width:${(t.waste / scale) * 100}%` })),
      t.estimate_video ? h("i", { class: "burn-est",
        style: `left:${Math.min(100, (t.estimate_video / scale) * 100)}%` }) : null),
    h("div", { class: "burn-legend" },
      h("span", null, h("i", { style: "background:var(--amber)" }), "实际"),
      h("span", null, h("i", { style: "background:var(--red)" }),
        `废片（占 ${(t.waste_ratio * 100).toFixed(0)}%）`),
      t.estimate_video ? h("span", null,
        h("i", { style: "background:var(--cyan)" }), "预估线") : null,
      h("span", null,
        `单镜 ¥${t.cost_per_shot.toFixed(3)} · 重roll ${t.rerolls_per_shot.toFixed(2)}/镜`)));
}

/* ---------------- 视图：片库 ---------------- */
const galFilter = { project: null, motion: null, kw: "" };
async function viewLibrary(view) {
  const [g, ov] = await Promise.all([api("/api/library"), getOverview()]);
  const videos = g.videos || [];
  const projNames = [...new Set(videos.map((v) => v.project).filter(Boolean))];
  const pTitle = (pid) => ov.projects?.find((p) => p.id === pid)?.title || pid;
  // 上次选中的项目可能已经没有成片了（删项目/清产物）：留着它就是一屏空结果配一个
  // 显示「请选择」的下拉——它不在选项里，连点回全部都得先想明白发生了什么
  if (galFilter.project && !projNames.includes(galFilter.project)) galFilter.project = null;

  const bar = h("div", { class: "filter-bar lib-bar" });
  const grid = h("div", { class: "gal-grid" });
  /* 项目维度用站内下拉（与待审队列/看板/成本同一个选择器），不做一项目一枚 chip：
     项目一多，chip 铺满两行，眼睛得逐枚扫过去才找得到自己那个。控件的初值一律取
     galFilter——它跨视图存活，只画控件不回填的话，从别的页回来会出现「筛选生效着、
     控件却显示全量」的对不上。 */
  const projSel = uiSelect([{ value: "", label: "全部项目" },
    ...projNames.map((pid) => ({ value: pid, label: pTitle(pid) }))],
    { value: galFilter.project || "" });
  projSel.addEventListener("change",
    () => { galFilter.project = projSel.value || null; apply(); });
  const kwBox = h("input", { class: "fsearch", type: "search", value: galFilter.kw || "",
    placeholder: "检索片库（标题 / 项目 / 章节 / 风格 / 文件名）…",
    oninput: () => { galFilter.kw = kwBox.value.trim(); apply(); } });
  const apply = () => {
    grid.innerHTML = "";
    const kw = (galFilter.kw || "").toLowerCase();
    const hay = (v) => [v.title, v.theme, v.project, v.chapter, v.name, v.profile]
      .filter(Boolean).join(" ").toLowerCase();
    const list = videos.filter((v) =>
      (!galFilter.project || v.project === galFilter.project) &&
      (!galFilter.motion || v.motion === galFilter.motion) &&
      (!kw || hay(v).includes(kw)));
    if (!list.length) {
      grid.append(emptyBlock("没有匹配的成片", "调整过滤条件，或先渲染一条。", null));
    } else {
      list.forEach((v) => grid.append(videoCard(v)));
    }
    bar.querySelectorAll(".fchip").forEach((b) =>
      b.classList.toggle("active", b.dataset.val === galFilter.motion));
  };
  const mchip = (label, val, tip) => h("button", {
    class: "fchip", dataset: { val, tip },
    onclick: () => { galFilter.motion = galFilter.motion === val ? null : val; apply(); } },
    label);

  bar.append(h("span", { class: "bf-k" }, "项目"), projSel, kwBox,
    h("span", { class: "fsep" }));
  Object.entries(MOTION).forEach(([m, info]) =>
    bar.append(mchip(`${info.key} · ${info.name}`, m, info.tip)));

  view.append(secHeader("01", "片库", "LIBRARY", videos.length));
  view.append(bar, grid);
  apply();
}

/* —— 模块导出 —— */
export { deliveryCard, exportCard, galFilter, ledgerCard, viewCost, viewLibrary };
