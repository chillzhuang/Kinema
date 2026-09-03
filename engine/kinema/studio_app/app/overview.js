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

/* ═ Studio 前端模块 · app/overview.js — 视图：总览（风格档分组 · 项目卡片）（原生 ES Module·免构建）═ */

/* ---------------- 视图：总览 ---------------- */
/* 系统风格下拉选择器（替代原生 select）：value 属性 + change 事件对齐原生用法。
   菜单挂 body 层 fixed 弹层——不被卡片容器裁剪、菜单内滚轮不穿透页面、
   空间不足自动上翻、页面滚动/缩放即收起。 */
import { chip } from "./components.js";
import { emptyBlock } from "../app.js";
import { LABEL, api, fmtDur, getOverview, h, toast } from "./core.js";
import { profileChip, secHeader, secMore, statusPill } from "./widgets.js";
import { videoCard } from "./playbook.js";

/* 首页两个区块的截断口径：总览是入口不是清单，余量由区块头「更多」领去
   #/library 与 #/projects 两张整页。两个数各自定——成片卡窄（首屏一行六张铺满），
   项目卡宽（一行四张，八个正好两行齐口）。区块头的计数一律报全量：显示条数与
   总数是两回事，报成截断值会让人以为库里只有这么多。 */
const HOME_RECENT = 6;
const HOME_PROJECTS = 8;

function statCell(value, unit, kLabel) {
  return h("div", { class: "stat" },
    h("b", null, String(value), unit ? h("small", null, unit) : null),
    h("span", { class: "k" }, kLabel));
}

/* 风格档分组真源（展示层）：入口 skill → 旗下画风。加画风时归属既有 skill 就在
   对应 profiles 里补一个名字；漏登记也不会消失——渲染时未匹配的 profile 自动
   落「独立画风」兜底组。cmd 即对 Claude Code 说的斜杠指令。 */
/* 首页「风格档」分组：直接按 /api/overview 下发的 skills 目录渲染（引擎
   skills.py 单一真源）——命名/用途/成员随目录自动更新，前端不维护平行分组表。
   展示编排：多画风入口 skill 各自成组卡；全部单画风 skill 收进一张 /kn-* 集合卡
   ——九档全量完整卡一行两个，每卡带各自 /kn-cmd 与用途说明。 */
const GROUP_DEFAULT = { "kn-anime": "anime" };   // 组内「默认」徽章：未点名画风即用它
/* 总览这面画风墙上不出现的入口：/kinema 是通用兜底工作流（旗下只有「口播」一档），
   摆在墙尾会被读成「另一种画风」。只作用于总览——SKILL 大屏是集群目录，恒全量。 */
const HOME_HIDDEN_SKILL = "kinema";
/* 墙上实际展示的画风数：区块头按它报，不报 ov.profiles 全量。这里是永久隐藏、
   没有「更多」可去，报全量就是与眼前数得出来的卡片对不上。 */
const homeProfiles = (ov) => {
  const hidden = new Set(((ov.skills || [])
    .find((s) => s.id === HOME_HIDDEN_SKILL) || {}).profiles || []);
  return (ov.profiles || []).filter((p) => !hidden.has(p.name));
};
const PROFILE_NOTES = {
  explainer: "同时是 /kn-showcase（素材复用型解说）的默认档",
  book: "有 WEREAD_API_KEY 时自动接微信读书官方接口取榜单/热门划线金句",
};

function styleGroups(ov) {
  const pmap = {};
  (ov.profiles || []).forEach((p) => (pmap[p.name] = p));
  const used = new Set();
  const openModal = (p, g, isDef) =>
    openStyleModal(ov, p, g, g.cmd, isDef, PROFILE_NOTES[p.name] || null);
  const profCard = (p, isDef, g, opts = {}) => h("div", { class: "sp-card",
      dataset: { tip: "点击查看画风详情与对话示例" },
      onclick: () => openModal(p, g, isDef) },
    h("div", { class: "sp-head" },
      h("b", null, p.label || p.name),
      isDef && h("span", { class: "chip amber" }, "默认"),
      p.name === ov.default_profile && h("span", { class: "chip" }, "引擎缺省"),
      h("code", { class: "sp-id" }, p.name)),
    p.style_prefix && h("p", { class: "sp-desc" }, p.style_prefix + "…"),
    h("div", { class: "sp-meta" },
      opts.cmd && h("code", { class: "sp-cmd" }, opts.cmd),
      (p.effects || []).length ? chip(`✦ ${p.effects.join(" · ")}`) : null,
      p.subtitle_mode && chip(`字幕 ${p.subtitle_mode}`),
      p.pacing && chip(`${p.pacing} 镜`)),
    (opts.note || PROFILE_NOTES[p.name]) &&
      h("p", { class: "sp-note" }, opts.note || PROFILE_NOTES[p.name]));
  const miniCard = (p, g) => h("div", { class: "sp-card sp-mini",
      dataset: { tip: (p.style_prefix ? p.style_prefix + "…\n" : "")
        + (PROFILE_NOTES[p.name] ? PROFILE_NOTES[p.name] + "\n" : "")
        + "点击查看画风详情与对话示例" },
      onclick: () => openModal(p, g, false) },
    h("b", null, p.label || p.name),
    p.name === ov.default_profile && h("span", { class: "chip" }, "引擎缺省"),
    h("code", { class: "sp-id" }, g.cmd || p.name),
    p.subtitle_mode && chip(`字幕 ${p.subtitle_mode}`),
    p.pacing && chip(`${p.pacing} 镜`));
  const groupEl = (g, items) => {
    const solo = items.length === 1 && !g.rest;
    return h("div", { class: "card sp-group" + (solo ? " solo" : "") },
      h("div", { class: "sp-ghead" },
        g.cmd && h("code", { class: "sp-gcmd" }, g.cmd),
        h("b", { class: "sp-gzh" }, g.zh),
        h("span", { class: "sp-gen" }, g.en),
        !solo && h("span", { class: "sp-gcnt" }, `${items.length} 画风`),
        h("span", { class: "sp-gusage" }, g.usage)),
      h("div", { class: "sp-grid" }, items.map((p) => solo
        ? miniCard(p, g)
        : profCard(p, GROUP_DEFAULT[g.id] === p.name, g))));
  };
  // /kn-* 集合卡：九档全量完整卡一行两个，每卡带各自 /kn-cmd 与用途说明
  const collectiveEl = (entries) => h("div", { class: "card sp-group" },
    h("div", { class: "sp-ghead" },
      h("code", { class: "sp-gcmd" }, "/kn-*"),
      h("b", { class: "sp-gzh" }, "单画风 skill"),
      h("span", { class: "sp-gen" }, "ONE-STYLE SKILLS"),
      h("span", { class: "sp-gcnt" }, `${entries.length} 画风`),
      h("span", { class: "sp-gusage" },
        "一档一绑，直接调用即启动整套流程——多为「静帧+运镜+旁白」的图文/氛围形态："
        + "零视频成本先出片，可随时升级 dubbed/native 动态化")),
    h("div", { class: "sp-grid sp-duo" }, entries.map((e) =>
      profCard(e.p, false, e.g, { cmd: e.g.cmd, note: e.g.usage }))));
  const wrap = h("div", { class: "sp-groups" });
  const solos = [];
  for (const s of ov.skills || []) {
    const items = (s.profiles || []).map((n) => pmap[n]).filter(Boolean);
    if (!items.length) continue;
    // 先记 used 再判隐藏：被隐藏那档的画风也算「已归组」，否则它会从下面的
    // 「独立画风」兜底组里原样冒出来——那只是换个位置继续显示
    items.forEach((p) => used.add(p.name));
    if (s.id === HOME_HIDDEN_SKILL) continue;
    const g = { id: s.id, cmd: s.cmd, zh: s.label, en: s.en, usage: s.usage };
    if (items.length > 1) wrap.append(groupEl(g, items));
    else solos.push({ g, p: items[0] });
  }
  if (solos.length) wrap.append(collectiveEl(solos));
  const rest = (ov.profiles || []).filter((p) => !used.has(p.name));
  if (rest.length) wrap.append(groupEl(
    { zh: "独立画风", en: "UNGROUPED", usage: "尚未归入任何 skill 的画风档",
      rest: true }, rest));
  return wrap;
}

/* 画风详情弹窗：全量画风基因（双语前缀不截断）+ 制作参数 + 对话示例——
   点风格卡即开，与时间轴/直供弹层同制式（遮罩淡入·面板 pop·头固定体滚动） */
function openStyleModal(ov, p, g, cmd, isDef, note) {
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const copy = (text) => h("button", { class: "cmt-act", dataset: { tip: "复制这句话" },
    onclick: async () => {
      try { await navigator.clipboard.writeText(text); toast("已复制——对 AI 说这句话即可"); }
      catch { toast("复制失败：浏览器未授权剪贴板", true); } } }, "⧉");
  const say = (label2, text) => h("div", { class: "gd-quote stm-say" },
    h("i", null, label2), h("code", null, text), copy(text));
  const row = (k2, v) => v && h("div", { class: "stm-row" },
    h("span", null, k2), h("b", null, v));
  const secK = (t) => h("span", { class: "stm-k" }, t);

  const entry = cmd || g.cmd || "/kinema";
  const ex1 = p.name === "narration"
    ? "/kinema 三分钟讲明白丝绸之路的前世今生"
    : isDef ? `${entry} 一个转校生藏着秘密身份的小剧场，30 秒短剧`
    : cmd ? `${entry} 一集 30 秒的短片，主题你来定个惊喜`
    : `${entry} 用「${p.label}」画风做一集 30 秒短剧`;
  const ex2 = p.name === "narration"
    ? "帮我做一条三分钟口播视频：<你的主题>"
    : `帮我用「${p.label || p.name}」画风做一条 30 秒短片：<你的主题>`;

  const overlay = h("div", { class: "rf-overlay",
      onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "stm-wrap" },
      h("div", { class: "rf-head" },
        h("span", { class: "k stm-title" }, p.label || p.name,
          isDef && h("span", { class: "chip amber", style: "margin-left:10px" }, "默认"),
          p.name === ov.default_profile &&
            h("span", { class: "chip", style: "margin-left:10px" }, "引擎缺省"),
          h("code", { class: "stm-id" }, p.name)),
        h("button", { class: "rf-x", onclick: close }, "✕")),
      h("div", { class: "stm-scroll" },
        secK("画风基因 · STYLE DNA"),
        h("p", { class: "stm-dna" }, p.style_prefix_full || p.style_prefix || "—"),
        p.style_prefix_en && h("p", { class: "stm-en" }, p.style_prefix_en),
        note && h("p", { class: "sp-note" }, note),
        secK("制作参数 · SPEC"),
        h("div", { class: "stm-rows" },
          row("所属入口", `${g.cmd ? g.cmd + " · " : ""}${g.zh}`),
          row("氛围特效", (p.effects || []).join(" · ") || "无（干净直出）"),
          row("字幕模式", p.subtitle_mode),
          row("分镜节奏", p.pacing
            ? `${p.pacing} 镜${p.pacing_note ? " · " + p.pacing_note : ""}` : null),
          row("默认音色", p.voice),
          row("配乐情绪", p.music_mood),
          row("模型链路", [p.image && `图 ${p.image}`, p.video && `视频 ${p.video}`,
            p.tts && `音 ${p.tts}`].filter(Boolean).join(" · "))),
        secK("怎么开口 · SAY IT"),
        say("直达", ex1),
        say("你说", ex2)),
      h("div", { class: "rf-foot" },
        h("span", { class: "rf-cost" },
          "画风前缀经 project.style_prompt 快照全片统一 · 中途换风对 AI 说一句即可"))));
  document.addEventListener("keydown", esc);
  document.body.append(overlay);
}

async function viewOverview(view) {
  const ov = await getOverview();
  const s = ov.stats;
  const cny = s.cost_totals?.CNY || 0;   // 各币种合计服务端下发，不在前端复算

  view.append(h("div", { class: "statband" },
    statCell(s.projects, "", "项目 · PROJECTS"),
    statCell(s.chapters, "", "章节 · EPISODES"),
    statCell(s.videos, "", "成片 · RENDERS"),
    statCell(s.shots, "", "分镜 · SHOTS"),
    statCell(fmtDur(s.duration), "", "总时长 · RUNTIME"),
    statCell(`¥${cny.toFixed(1)}`, "", "云成本 · API COST")));

  // 最近成片：只露最新 6 条，网格自适应列数、永不出横向滚动条
  const recent = ov.recent || [];
  view.append(secHeader("01", "最近成片", "LATEST RENDERS", s.videos,
    recent.length ? secMore("#/library") : null));
  if (recent.length) {
    view.append(h("div", { class: "recent-grid" },
      recent.slice(0, HOME_RECENT).map(videoCard)));
  } else {
    view.append(emptyBlock("还没有成片", "跑一条流水线，产物会自动出现在这里。",
      "对 AI 说：帮我做一集 30 秒动漫短片《你的主题》"));
  }

  // 项目：只露前 8 个（一行四张、两行齐口），全量在 #/projects
  const projects = ov.projects || [];
  view.append(secHeader("02", "项目", "PROJECTS", projects.length,
    projects.length ? secMore("#/projects") : null));
  view.append(projectGrid(projects.slice(0, HOME_PROJECTS)));

  // 风格档：按 overview 下发的 skills 目录分组渲染（引擎 skills.py 单一真源，
  // 与新建项目弹层同序；未归组的 profile 自动落「独立画风」兜底组，加画风永不失踪）
  view.append(secHeader("03", "风格档", "STYLE PROFILES", homeProfiles(ov).length));
  view.append(styleGroups(ov));

  // 运行环境（原侧栏系统信息移居于此）：工作区 / 配置 / 存储后端
  const mediaOss = ov.storage?.media?.backend === "oss";
  view.append(secHeader("04", "运行环境", "RUNTIME"));
  view.append(h("div", { class: "card sysinfo" },
    h("div", { class: "sys-row" }, h("span", { class: "k" }, "WORKSPACE"),
      h("code", { title: ov.workspace || "" }, ov.workspace || "—")),
    h("div", { class: "sys-row" }, h("span", { class: "k" }, "CONFIG"),
      h("code", { title: ov.config || "" }, ov.config || "—")),
    h("div", { class: "sys-row" }, h("span", { class: "k" }, "STORAGE"),
      h("code", { title: [ov.storage?.detail, ov.storage?.media?.detail]
          .filter(Boolean).join("\n") },
        (ov.storage?.backend === "mysql" ? "MySQL 持久化" : "本地 JSON")
        + (mediaOss ? " · 媒体 OSS" : "")))));
}

/* ---------------- 项目卡片 ---------------- */
function projectGrid(projects) {
  if (!projects.length) {
    return emptyBlock("还没有项目", "创建一个项目开始强规划：",
      "对 AI 说：帮我立项一个系列《我的系列》——选好画风，配主角人设与音色，再开第一章");
  }
  return h("div", { class: "proj-grid" }, projects.map((p) => {
    const chs = p.chapters || [];
    // 项目卡横幅是宽容器（21:9）——取 4:3 横版；卡片自绘标题浮层，
    // 优先无字背景真源（防封面成品的排版标题与浮层重字）
    const banner = (p.covers_bg || {})["4:3"] || (p.covers || {})["4:3"] || p.cover;
    const cover = h("div", { class: "pcard-cover" },
      banner
        ? h("img", { src: banner, loading: "lazy", alt: "",
                     onerror: (e) => e.target.remove() })
        : h("div", { class: "noimg" }, "NO FOOTAGE"),
      // 封面缺位点名：横幅此时是成片海报帧或分镜图兜底，不说就看不出封面还欠着
      p.cover_missing && h("i", { class: "pcard-nocover",
        dataset: { tip: "系列封面未生成——横幅是海报帧/分镜图兜底\n"
                        + `对 AI 说：给《${p.title || p.id}》做系列主视觉封面` } },
        "NO COVER"),
      h("div", { class: "pcard-over" },
        h("div", null, h("h3", null, p.title || p.id), h("div", { class: "pid" }, p.id)),
        profileChip(p.profile)));
    return h("div", { class: "card pcard",
                      onclick: () => (location.hash = `#/project/${encodeURIComponent(p.id)}`) },
      cover,
      h("div", { class: "pcard-body" },
        (p.logline || p.theme) && h("div", { class: "pcard-theme" }, p.logline || p.theme),
        chs.length ? h("div", { class: "segbar" },
          chs.map((c) => h("i", { class: c.status,
            dataset: { tip: `${c.title} · ${LABEL.status[c.status] || c.status}` } }))) : null,
        h("div", { class: "pcard-stats" },
          pStat(`${p.rendered}/${chs.length}`, "章节"),
          pStat(p.characters, "角色"),
          pStat(p.shots, "分镜"),
          pStat(p.has_refs ? "✓" : "—", "设定集"),
          h("div", { class: "pcard-stat", style: "margin-left:auto" }, statusPill(p.status)))));
  }));
}
const pStat = (v, l) => h("div", { class: "pcard-stat" }, h("b", null, String(v)), h("span", null, l));

const projFilter = { kw: "", motion: null };

/* —— 模块导出 —— */
export { GROUP_DEFAULT, PROFILE_NOTES, openStyleModal, pStat, projFilter, projectGrid,
         statCell, styleGroups, viewOverview };
