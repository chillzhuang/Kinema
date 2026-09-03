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

/* ═ Studio 前端模块 · app/chapter.js — 视图：章节制作台（原生 ES Module·免构建）═ */

/* ---------------- 视图：章节制作台 ---------------- */
import { chip, openDirectiveDialog, runBusy, uiCheck, uiConfirm, uiPrompt, uiSelect,
         openShell } from "./components.js";
import { STATE } from "./state.js";
import { emptyBlock } from "../app.js";
import { $, BUST, CUR, GENJOBS, ICON, JOB_ZH, LABEL, MOTION, REVIEW, STAGE_ZH, TRANSITION_ZH, api,
         costTotal, fmtDate, fmtDur, fmtSec, fmtSize, getOverview, h, jobKey, mediaPath,
         pollJob, post, softRefresh, state, toast, trackJob, withBust } from "./core.js";
import { audioPill, closeCinema, directorCard, effectChip, effectsBtn, motionBadge,
         openCinema, openLightbox, profileChip, rebuildBtn, secHeader, titledChip,
         watermarkBtn } from "./widgets.js";
import { setCrumbs } from "./shell.js";

import { deletedBanner } from "./project-new.js";
import { openRefsDialog, openSupplyDialog, refreshAfterWrite } from "./shot-tools.js";
import { Q, openOutputVPanel, openVPanel } from "./panels.js";
import { exportCard } from "./ledger.js";
import { displayEmotion } from "./shot-display.js";

function chapterSignature(d) {
  const s = d.stages || {};
  return [s.script, s.image, s.audio, s.clips, s.video, d.outputs?.length,
          d.animatic?.state || "",
          Math.round(d.updated_at || 0)].join("|");
}

async function viewChapter(view, pid, cid, { silent = false, stale = null } = {}) {
  const d = await api(`/api/chapter?project=${encodeURIComponent(pid)}&id=${encodeURIComponent(cid)}`);
  STATE.chapSig = chapterSignature(d);
  // 忙态对账：后端进行中任务 → 恢复分镜卡「生成中」（刷新页面也不丢）
  try {
    const jb = await api(`/api/jobs?project=${encodeURIComponent(pid)}&chapter=${encodeURIComponent(cid)}`);
    for (const j of jb.jobs || []) {
      const m = j.meta || {};
      if (m.shot != null) trackJob(jobKey(pid, cid, m.shot), j.id, m.kind || "regen", pid, cid);
      // 简笔板批量任务（章节级·meta.shots 带镜号清单）：恢复板条的逐镜「生成中」格
      if ((m.kind || "") === "sketch") trackSketchJob(pid, cid, j.id, m.shots);
      // 「交给 Seedance」批量任务（章节级·meta.shots）：恢复分镜卡的逐镜忙态遮罩
      if ((m.kind || "") === "previz_v2v") trackClipJob(pid, cid, j.id, m.shots);
      // 音频剧本整轨（章节级一条）：恢复剧本台的「生成中」按钮态
      if ((m.kind || "") === "score") trackScoreJob(pid, cid, j.id);
    }
  } catch { /* 对账失败不阻断视图 */ }
  if (stale && stale()) return;   // 用户已切走：迟到视图不清、不写别人的 #view
  if (!silent) setCrumbs([["总览", "#/"], [d.project_title, `#/project/${encodeURIComponent(pid)}`], [d.title]]);
  view.innerHTML = "";

  const ov = await getOverview();
  if (stale && stale()) return;
  const canvas = ov.canvas || {};

  // 所属项目已移至回收站：只读冻结——置顶横幅 + 全部操作禁用（见 .ro-deleted + 拦截器）。
  // 软删态双探测：章节接口 project_deleted（需重启引擎）｜总览 recycle 清单（即时生效，无需重启）
  const recycled = (ov.recycle || []).find((r) => r.id === pid);
  const projDeleted = !!d.project_deleted || !!recycled;
  view.classList.toggle("ro-deleted", projDeleted);
  if (projDeleted)
    view.append(deletedBanner(pid, d.project_title,
      d.project_deleted_at || (recycled && recycled.deleted_at)));

  // 头部
  // ⧉ 打磨本集：弹层里写打磨方向 → 与带定位坐标（chapters/<id>.json）+ 本集现状的
  // 标准指令合并后复制，粘给 Claude Code 调剧情 / 分镜 / 台词 / 运镜 / 逐镜情绪
  // ——剧本与提示词不在网页里改
  const copyChapDirective = () => openDirectiveDialog({
    title: "打磨本集指令", code: "CHAPTER · POLISH",
    meta: `「${d.title}」· 项目 ${d.project} / 章节 ${d.id}`,
    ask: "在此写打磨方向",
    hint: "例：开场加一个悬念钩子；第 3 镜换希区柯克变焦；台词更口语、按情感给每句加 emotion",
    done: `「${d.title}」打磨指令已复制——粘给 AI`,
    directive: [
      `请打磨本集「${d.title}」· 项目 ${d.project} / 章节 ${d.id}`,
      `定位坐标：project/${d.project}/chapters/${d.id}.json`,
      `可调：剧情走向 / 分镜增删与顺序 / 台词旁白 / 运镜 camera / 逐镜情绪 emotion / 图像·运动提示词`,
      `现状：${(MOTION[d.motion] || {}).name || d.motion} 模式 · `
        + `${(d.shots || []).filter((s) => !s.omitted).length} 镜 · ${(d.aspects || []).join(" ")}`,
      `落地：改完按需重跑 gen-image / tts / assemble，逐门过审。`,
    ].join("\n"),
  });
  view.append(h("div", { class: "head-hero" },
    h("h1", null, d.title,
      h("span", { class: "ch-code" }, (d.id || "").toUpperCase()),
      h("button", { class: "act-btn chap-edit",
        dataset: { tip: "⧉ 打磨本集\n打开指令台：写下打磨方向，与带定位坐标（chapters/" + d.id
          + ".json）和本集现状的标准指令合并后复制，粘给 AI 调剧情 / 分镜 / 台词 / 运镜 / "
          + "逐镜情绪——剧本不在网页里改。" },
        onclick: copyChapDirective }, "⧉ 打磨本集")),
    d.theme && h("div", { class: "sub" }, d.theme),
    h("div", { class: "chips" },
      profileChip(d.profile),
      motionBadge(d.motion),
      ...(d.aspects || []).map((a) => {
        const c = canvas[a];
        return chip(c ? `${a} · ${c[0]}×${c[1]}` : a);
      }),
      d.image_per_aspect && chip("逐比例出图", "cyan"),
      d.frame_chain && chip("首尾帧衔接", "cyan"),
      ...(d.effects || []).map(effectChip),        // 生效特效（章节点名·分类彩色 chip）
      ...(d.platform || []).map((x) => chip(LABEL.platform[x] || x)))));

  // 阶段流水线
  const st = d.stages || {};
  // 阶段流水线：每格带 EN 小标 + 底部进度细条 + 步进箭头，点击跳到页内对应区块
  const stageDefs = [
    ["脚本", "SCRIPT", st.script ? "done" : "todo", st.script ? "✓ 已定稿" : "待撰写",
      st.script ? 1 : 0, "sec-script"],
    ["分镜图", "STILLS", prog(st.image, st.image_total), `${st.image}/${st.image_total}`,
      st.image_total ? st.image / st.image_total : 0, "sec-sb"],
    ["配音", "VOICE", st.audio_total ? prog(st.audio, st.audio_total) : "skip",
      // audio_total=0：scored 整章音频剧本 / native 模型原生音画 / 本章没有进旁白轨的台词
      st.audio_total ? `${st.audio}/${st.audio_total}`
                     : ((d.audio_script || {}).mode === "scored" ? "音频剧本"
                        : (d.motion === "native" ? "native 原生" : "无台词")),
      st.audio_total ? st.audio / st.audio_total : 0, "sec-audioscript"],
    ["动态片段", "MOTION", st.clips_total ? prog(st.clips, st.clips_total) : "skip",
      st.clips_total ? `${st.clips}/${st.clips_total}` : "Ken Burns",
      st.clips_total ? st.clips / st.clips_total : 0, "sec-timeline"],
    ["成片", "FINAL", st.video ? "done" : "todo", st.video ? `${d.outputs.length} 支` : "待合成",
      st.video ? 1 : 0, "sec-final"],
  ];
  view.append(h("div", { class: "stagebar" }, stageDefs.map(([zh, en, cls, sub, pct, anchor], i) =>
    h("div", { class: `stage ${cls}`, title: `${zh} · ${en} — 点击跳到对应区块`,
      // 长页导航：格子即锚点。配音格恒锚到 AU 音频剧本台——声音工序的页内落点
      // （逐镜配音散在分镜卡上，没有自己的区块头）
      onclick: () => {
        const el = document.getElementById(anchor);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      } },
      h("span", { class: "stage-ico" }, cls === "done" ? "✓" : String(i + 1)),
      h("div", { class: "stage-txt" },
        h("b", null, zh, h("em", { class: "stage-en" }, en)),
        h("span", null, sub || en)),
      i < stageDefs.length - 1 && h("span", { class: "stage-arrow" }, "›"),
      h("i", { class: "stage-prog", style: `width:${Math.round((pct || 0) * 100)}%` })))));

  // 时间线：分镜按时长横排（缩略图 + 状态色条）+ 音轨对齐——专业分区标题（同分镜脚本制式）
  const strip = timelineStrip(d);
  if (strip) {
    strip.querySelector(":scope > .k")?.remove();     // 内置标题让位给 secHeader（区块头已具名）
    const tlHead = secHeader("TL", "时间线", "TIMELINE", d.duration ? fmtDur(d.duration) : null);
    tlHead.id = "sec-timeline";                        // 阶段条「合成」跳转锚点从卡片移到区块头
    view.append(tlHead, strip);
  }

  // 血缘画布：设定资产 ↔ 分镜 连线（hover 联动 · 过期红线 · 点资产可框选改造）
  const lin = lineageCanvas(d);
  if (lin) {
    lin.querySelector(":scope > .k")?.remove();
    const alHead = secHeader("AL", "资产视图", "ASSET LINEAGE", (d.design_assets || []).length);
    alHead.id = "sec-lineage";
    view.append(alHead, lin);
  }

  // 分镜脚本（镜头表）：施工图级全字段一览，行展开看双语提示词
  const sb = storyboardTable(d);
  if (sb) view.append(secHeader("SL", "分镜脚本", "SHOT LIST", d.shots.length), sb);

  // 运动预演两台（3D 导演台 · 简笔分镜）：**单独一整行**，落在分镜脚本之下、
  // 控制台双栏（放映 / 分镜卡）之上。位置即顺序——先把戏排好（走位/动作/机位/运镜），
  // 再逐镜细看与看成片；塞进任一栏都会被读成那一栏的附属功能，而它们是整个工作台。
  // 两台只在调用视频模型的章节展开，判据见 previzDesks。
  view.append(...previzDesks(d));

  // 音频剧本台：与上面两个台并列的**第三个工作台**，只是它排的是声音而不是画面。
  // 落在这里而不是「剧本与声音」那一栏——那一栏是只读的参考型卡片，而这是要在
  // 上面逐段写字、点生成、听整轨的地方，塞进去会被读成音色表的附属。
  // **与 motion 正交故恒展开**：scored 在 kenburns 与 native 下都成立（真正与它
  // 互斥的是 dubbed，见 audioScriptCard 的 dubLock），而纯 ffmpeg 合成的章节恰恰是
  // 整轨买断最典型的搭配——跟着运动预演一起收起就是收反了。
  const auHead = secHeader("AU", "音频剧本", "AUDIO SCRIPT",
    (d.audio_script || {}).segments?.length || null);
  auHead.id = "sec-audioscript";
  view.append(auHead, audioScriptCard(d));

  // 控制台双栏（成片区块头与「SB 分镜卡」同款制式，徽标位放成片总时长）
  const side = h("div", { class: "console-side" });
  const fcHead = secHeader("FC", "放映", "SCREENING",
    d.duration ? fmtDur(d.duration) : null);
  fcHead.id = "sec-final";
  side.append(fcHead, screenCard(d));
  // 后期模块（特效 + 水印 + 构建 + 自审）：独立区块头；按钮均弹框式、始终两字
  side.append(
    secHeader("FX", "后期", "FINISHING", (d.effects || []).length || null),
    h("div", { class: "screen-actions" },
      effectsBtn(d), watermarkBtn(d), rebuildBtn(d), verifyBtn(d)));

  const sbHead = secHeader("SB", "分镜卡", "STORYBOARD", d.shots.length);
  sbHead.id = "sec-sb";
  const shotsCol = h("div", null,
    sbHead,
    d.shots.length
      ? h("div", { class: "shots-col" },
          d.shots.flatMap((s, i) => [shotCard(d, s, i),
            // 「＋转场」插槽：转场镜后不再叠加（转场不接转场）。
            // 末镜后面那个叫「＋尾帧」——同一功能，位置不同故文案不同
            s.kind !== "transition" && d.shots[i + 1]?.kind !== "transition"
              ? transitionSlot(d, s, i === d.shots.length - 1) : null]).filter(Boolean))
      : emptyBlock("还没有分镜", "由 Skill 层完成文案与分镜（阶段 2/3）后落进本章节 JSON。", null));

  // 生产叙事线（参考型/终点型内容随主流滚动）：剧本与声音 → 章节资产 → 交付
  const hasScript = d.script?.hook || d.script?.body || d.script?.cta;
  const hasVoices = Object.keys(d.voices || {}).length > 0;
  if (hasScript || hasVoices) {
    const svHead = secHeader("SV", "剧本与声音", "SCRIPT & VOICE");
    svHead.id = "sec-script";
    shotsCol.append(svHead, h("div", { class: "flow-grid" },
      hasScript ? scriptCard(d.script) : null,
      hasVoices ? voicesCard(d) : null));
  }
  const asEl = assetsCard(d);
  if (asEl.children.length) {
    asEl.querySelector(":scope > .k")?.remove();   // 区块头已具名，卡内不重复
    shotsCol.append(secHeader("AS", "章节资产", "AUDIO RACK"), asEl);
  }
  // 成本与一致性（QC）在交付之前：先对账再交付
  shotsCol.append(secHeader("QC", "成本与一致性", "COST & CONSISTENCY"),
    h("div", { class: "status-row" },
      d.cost_total != null ? costStrip(d) : null, consStrip(d), verifyStrip(d)));
  const dlHead = secHeader("DL", "交付", "DELIVERY");
  shotsCol.append(dlHead, h("div", { class: "flow-grid" },
    exportCard({ title: "静态审阅包", en: "CLIENT REVIEW", kind: "review",
      pid: d.project, cid: d.id,
      desc: "免登录单页审阅书 + 自包含媒体，客户离线可开。",
      action: "⇪ 导出审阅包" }),
    exportCard({ title: "交付包", en: "DELIVERY ZIP", kind: "deliver",
      pid: d.project, cid: d.id,
      desc: "成片(多比例) + 封面 + 双字幕 + 平台文案 + manifest（AI 披露/版权）。",
      action: "⇪ 打包交付" })));

  view.append(h("div", { class: "console" }, side, shotsCol));
}

const prog = (done, total) => (!total || !done ? "todo" : done >= total ? "done" : "part");

/* 说话人签：空 speaker 与各种旁白别名一律显示「旁白」，角色显示本名。
   别名表与引擎的 voicecast.NARRATOR_NAMES 同一份口径——两边分叉的话，
   引擎按旁白编译提示词、卡片却把 `VO` 当成一个角色名显示出来。 */
const NARRATOR_ALIASES = ["旁白", "narrator", "voiceover", "vo", "画外音"];
const speakerLabel = (spk) => {
  const s = (spk || "").trim();
  return !s || NARRATOR_ALIASES.includes(s.toLowerCase()) ? "旁白" : s;
};

/* 血缘画布：设定资产（上排）连线到引用它的分镜（下排）。
   数据即血缘：镜的出场角色/道具（缺省=全部）+ 场景；过期引用画红色虚线。 */
const LNDRAW = { fn: null };
window.addEventListener("resize", () => LNDRAW.fn && LNDRAW.fn());
function lineageCanvas(d) {
  const assets = d.design_assets || [];
  const shots = (d.shots || []).filter((s) => !s.omitted);
  if (!assets.length || shots.length < 2) return null;
  // 连线唯一数据源 = scanner 逐镜下发的 design_refs（引擎 lineage.required_refs 的 key）。
  // **绝不在前端另写出场推导**——另写必然长出分叉形态：无显式 props=全挂、场景恒连全镜、
  // 文本命中缺席，画出来的线与引擎真实挂载彻底分叉。
  const key = (a) => a.key || `${a.kind}:${a.name}`;
  const byKey = new Map(assets.map((a) => [key(a), a]));
  const edges = [];
  shots.forEach((s) => {
    const stale = (s.stale_refs || []).map(String);
    (s.design_refs || []).forEach((k) => {
      const a = byKey.get(k);
      if (!a) return;
      const isStale = stale.some((f) =>
        a.thumb && decodeURIComponent(a.thumb).includes(f));
      edges.push({ a: k, s: String(s.id), stale: isStale });
    });
  });
  if (!edges.length) return null;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "ln-svg");
  const stage = h("div", { class: "ln-stage" });
  // 资产/分镜缩略图比例跟随 project 创建时的画面比例（无则默认横屏 16:9）
  const lnAsp = ((d.aspects && d.aspects[0]) || "16:9").replace(":", "/");
  stage.style.setProperty("--ln-aspect", lnAsp);
  const node = (a) => h("div", { class: "ln-node",
      dataset: { key: key(a),
        tip: `${a.name} · 设定资产\n连线指向引用它的分镜；点击打开检查器，`
          + "可框选局部改造这张设定图（改后下游分镜未同步则标虚线过期）。" },
      onclick: () => a.thumb && openLightbox([{ src: a.thumb,
        title: `${a.kind.toUpperCase()} · ${a.name}`,
        caption: a.appearance || a.desc || `${a.name} · 设定资产`,
        // 具名取景地必须带 name（后端按 name 分派具名/全局，见 sceneCard 同款纪律）；
        // 只有全局场景 scene:main 才走 name=null
        actx: { pid: d.project, kind: a.kind,
                name: key(a) === "scene:main" ? null : a.name,
                comments: a.comments || [] } }]),
      onmouseenter: () => paint(key(a), null),
      onmouseleave: () => paint() },
    a.thumb ? h("img", { src: a.thumb, loading: "lazy", alt: "" })
            : h("span", { class: "ln-ph" }, (a.name || "?")[0]),
    h("i", null, a.name));
  const arow = h("div", { class: "ln-row ln-assets" }, assets.map(node));
  const srow = h("div", { class: "ln-row ln-shots" }, shots.map((s) => {
    const isTr = s.kind === "transition";
    return h("div", { class: "ln-node ln-shot" + ((s.stale_refs || []).length ? " stale" : ""),
        dataset: { shot: String(s.id),
          tip: isTr ? `转场镜 ${s.id}\n${(s.transition || {}).text || "纯色停顿"} · 点击跳到转场卡`
            : (s.stale_refs || []).length
            ? `镜 ${s.id} · 设定已过期\n引用的设定图已更新——建议按新设定重生成；点击跳到分镜卡`
            : `镜 ${s.id}\n点击跳到分镜卡` },
        onclick: () => document.getElementById(`shot-${s.id}`)
          ?.scrollIntoView({ block: "center", behavior: "smooth" }),
        onmouseenter: () => paint(null, String(s.id)),
        onmouseleave: () => paint() },
      isTr ? transitionThumb(s)
        : s.image ? h("img", { src: s.image, loading: "lazy", alt: "" })
                  : h("span", { class: "ln-ph" }, String(s.id)),
      h("i", null, isTr ? "转场" : `镜${s.id}`));
  }));
  stage.append(svg, arow, srow);

  const draw = () => {
    if (!stage.isConnected) { LNDRAW.fn = null; return; }
    svg.innerHTML = "";
    const box = stage.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    const pos = (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.left - box.left + r.width / 2,
               top: r.top - box.top, bottom: r.bottom - box.top };
    };
    const amap = {}, smap = {};
    arow.querySelectorAll(".ln-node").forEach((el) => (amap[el.dataset.key] = el));
    srow.querySelectorAll(".ln-node").forEach((el) => (smap[el.dataset.shot] = el));
    edges.forEach((e) => {
      const a = amap[e.a], s2 = smap[e.s];
      if (!a || !s2) return;
      const p1 = pos(a), p2 = pos(s2);
      const my = (p1.bottom + p2.top) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d",
        `M ${p1.x} ${p1.bottom - 16} C ${p1.x} ${my}, ${p2.x} ${my}, ${p2.x} ${p2.top}`);
      path.setAttribute("class", "ln-edge" + (e.stale ? " stale" : ""));
      path.dataset.a = e.a;
      path.dataset.s = e.s;
      svg.append(path);
    });
  };
  const paint = (ak = null, sid = null) => {
    const on = ak != null || sid != null;
    stage.classList.toggle("focus", on);
    const hotA = new Set(), hotS = new Set();
    if (on) {
      edges.forEach((e) => {
        if ((ak && e.a === ak) || (sid != null && e.s === sid)) {
          hotA.add(e.a);
          hotS.add(e.s);
        }
      });
    }
    svg.querySelectorAll(".ln-edge").forEach((p) => p.classList.toggle("hot",
      on && ((ak && p.dataset.a === ak) || (sid != null && p.dataset.s === sid))));
    [...arow.children].forEach((el) =>
      el.classList.toggle("hot", on && hotA.has(el.dataset.key)));
    [...srow.children].forEach((el) =>
      el.classList.toggle("hot", on && hotS.has(el.dataset.shot)));
  };
  LNDRAW.fn = draw;
  requestAnimationFrame(draw);

  const staleCnt = shots.filter((s) => (s.stale_refs || []).length).length;
  return h("div", { class: "card ln-card" },
    h("span", { class: "k" }, "资产视图 · ASSET LINEAGE",
      h("span", { class: "sub" },
        `${assets.length} 资产 → ${shots.length} 镜`
        + (staleCnt ? ` · ${staleCnt} 镜引用已过期` : ""))),
    stage);
}

/* ── 关系图谱：力导向节点网 + 缩放/平移/拖拽/选中详情（剧本工作台「图谱」Tab）──
   数据即 series.graph（Claude 指挥层产出）。与 lineageCanvas 的关键区别：节点坐标活
   在「世界坐标系」，缩放/平移只驱动世界层 transform（不逐帧 getBoundingClientRect），
   故可自由 zoom/pan 不失真。character/命中设定图的节点点开即富灯箱（复用 ch01 actx）。 */
const KG_TYPE = {
  character: { zh: "角色", c: "var(--amber)" },
  faction: { zh: "阵营", c: "var(--blue)" },
  location: { zh: "地点", c: "var(--cyan)" },
  item: { zh: "器物", c: "#c08ce0" },
  worldview: { zh: "世界观", c: "#d89bc0" },
};
const KG_KIND = {
  kin: { zh: "亲缘", c: "#e8859e" },
  ally: { zh: "盟友", c: "var(--blue)" },
  mentor: { zh: "师承", c: "var(--amber)" },
  hostile: { zh: "敌对", c: "var(--red)" },
  love: { zh: "情感", c: "#e87ea6" },
  member: { zh: "归属", c: "var(--cyan)" },
  rival: { zh: "竞争", c: "#e0a060" },
  neutral: { zh: "关联", c: "var(--text-3)" },
};
const kgKindKey = (k) => (KG_KIND[k] ? k : "neutral");
const kgType = (t) => KG_TYPE[t] || { zh: t || "其他", c: "var(--text-2)" };
const KG_EMPTY_SVG = '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="13" r="4"/><circle cx="37" cy="13" r="4"/><circle cx="24" cy="35" r="4"/><path d="M15 14.5 33 14.5M13.4 16.4 20.8 31.4M34.6 16.4 27.2 31.4"/></svg>';
// 空态大图标（48×48 线条·与图谱空态同款）——剧本工作台 拆书/分集/设定/图谱 四模块空态统一
const PANE_ICON = {
  bible: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M24 13c-4.5-3-11-3-16-1.2v25c5-1.8 11.5-1.8 16 1.4 4.5-3.2 11-3.2 16-1.4v-25C35 10 28.5 10 24 13z"/><path d="M24 13v25"/></svg>',
  eps: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M24 6 43 15 24 24 5 15z"/><path d="M5 24 24 33 43 24M5 33 24 42 43 33"/></svg>',
  ent: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="17" r="6.5"/><path d="M7 39c0-6.4 4.8-10.5 11-10.5S29 32.6 29 39"/><path d="M31 11.6a6.5 6.5 0 0 1 0 12.6M32 28.9c4.8.9 8 4.5 8 10.1"/></svg>',
  graph: KG_EMPTY_SVG,
  write: '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 39l3-10.5 21-21a4.2 4.2 0 0 1 6 6l-21 21L9 39z"/><path d="M28.5 12.5l7 7M12 28.5l7 7"/></svg>',
};

function kgGraph(host, graph, pid, opts = {}) {
  host.textContent = "";
  const NS = "http://www.w3.org/2000/svg";
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const rawNodes = graph.nodes || [], rawEdges = graph.edges || [];
  const n = rawNodes.length;
  // 世界虚拟画布：随节点数放大，避免多节点挤成团（缩放/平移都在此坐标系里进行）。
  // 系数按「节点数 × 标签占位」标定——纯 FR 在 50+ 节点时必然抱团（约 58 节点时
  // 「都挤在一起看不清楚」），画布不给够，后面的分区与消重都推不开。
  // 布局空间 LW/LH 与最终画布 W/H **解耦**：力导向与消重都在 LW/LH 这个「够大的空间」里算，
  // 算完再把画布收到实际包围盒（fitWorld）。一体化的老写法里，relax 把节点推开后会撞上
  // 画布硬边界、被 clamp 压成贴边的一条线，表现为底部排开一整行节点。
  const LW = Math.max(1000, Math.min(2600, Math.round(150 * Math.sqrt(n) + 300)));
  const LH = Math.round(LW * 0.70);
  let W = LW, H = LH, cx = LW / 2, cy = LH / 2;

  const deg = {};
  rawEdges.forEach((e) => { deg[e.source] = (deg[e.source] || 0) + 1; deg[e.target] = (deg[e.target] || 0) + 1; });
  const seed = (s) => { let hh = 0; for (const ch of String(s)) hh = (hh * 31 + ch.charCodeAt(0)) & 0xffff; return hh / 0xffff; };

  // ── 类型分区（决定节点是否成簇的一层）──
  // 纯力导向只认连线，节点不成簇；按 type 切扇区并给一个**弱**锚力，
  // 结果是「成簇但仍相连」：一眼能看出角色群 / 阵营 / 地点 / 器物 / 世界观法则各在哪一片，
  // 而跨簇的连线又不会被硬掰直。扇区角宽按该类型节点数**按比例**分配——
  // 均分会让 29 个角色挤进和 6 个阵营一样宽的扇形里，等于没分区。
  const SECTOR_ORDER = ["character", "faction", "location", "item", "worldview"];
  const tCount = {};
  rawNodes.forEach((nd) => { const t = KG_TYPE[nd.type] ? nd.type : "_other"; tCount[t] = (tCount[t] || 0) + 1; });
  const tKeys = [...SECTOR_ORDER.filter((t) => tCount[t]), ...Object.keys(tCount).filter((t) => !SECTOR_ORDER.includes(t))];
  const sector = {}; let acc = 0;
  tKeys.forEach((t) => {
    const span = (tCount[t] / n) * Math.PI * 2;
    sector[t] = { a0: acc, a1: acc + span, mid: acc + span / 2 };
    acc += span;
  });
  const idxInType = {};
  const anchorR = Math.min(W, H) * 0.34;

  const nodes = rawNodes.map((raw) => {
    const t = KG_TYPE[raw.type] ? raw.type : "_other";
    const s = sector[t], j = (idxInType[t] = (idxInType[t] || 0) + 1) - 1, cnt = tCount[t];
    // 扇区内均匀铺开 + 指纹抖动（确定性，非随机——同一份图谱在任何机器上排法一致）。
    // 锚点**逐节点落在自己那一段弧上**，绝不能用扇区中点：29 个角色共用一个锚点＝29 个节点
    // 被拉向同一处，分区反而制造了一个更密的团。
    const ang = s.a0 + (s.a1 - s.a0) * ((j + 0.5) / cnt);
    const jitter = 0.86 + seed(raw.id) * 0.3;
    // 大类型（节点多）把锚环推远一点，给内部留出铺开的周长
    const spread = 1 + Math.min(0.5, (cnt / n) * 1.1);
    const d = deg[raw.id] || 0;
    return { ...raw, deg: d, r: 19 + Math.min(15, d * 2.4),
      _ax: cx + Math.cos(ang) * anchorR * spread,
      _ay: cy + Math.sin(ang) * anchorR * spread * 0.82,
      x: cx + Math.cos(ang) * anchorR * jitter,
      y: cy + Math.sin(ang) * anchorR * jitter * 0.82, tx: 0, ty: 0 };
  });
  const byId = {}; nodes.forEach((nd) => (byId[nd.id] = nd));
  const edges = rawEdges.map((e, i) => ({ ...e, i, a: byId[e.source], b: byId[e.target] }))
    .filter((e) => e.a && e.b);
  // 同一对节点间的多条关系分「道次」扇开，避免曲线与标签叠加
  const _pairGroups = {};
  edges.forEach((e) => {
    const key = [String(e.source), String(e.target)].sort().join("");
    (_pairGroups[key] ||= []).push(e);
  });
  Object.values(_pairGroups).forEach((g) => {
    g.forEach((e, idx) => { e._grp = g.length; e._lane = idx - (g.length - 1) / 2; });
  });

  // 力导向（Fruchterman-Reingold，确定性同步预演到收敛）——布局稳定可复现
  const simulate = (iters) => {
    const k = 0.82 * Math.sqrt((W * H) / Math.max(1, n));
    let temp = W * 0.12; const cool = temp / (iters + 1);
    for (let it = 0; it < iters; it++) {
      nodes.forEach((v) => { v.dx = 0; v.dy = 0; });
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const v = nodes[i], u = nodes[j];
        let dx = v.x - u.x, dy = v.y - u.y, dist = Math.hypot(dx, dy) || 0.01;
        const rep = (k * k) / dist; dx /= dist; dy /= dist;
        v.dx += dx * rep; v.dy += dy * rep; u.dx -= dx * rep; u.dy -= dy * rep;
      }
      edges.forEach((e) => {
        let dx = e.a.x - e.b.x, dy = e.a.y - e.b.y, dist = Math.hypot(dx, dy) || 0.01;
        const att = (dist * dist) / k; dx /= dist; dy /= dist;
        e.a.dx -= dx * att; e.a.dy -= dy * att; e.b.dx += dx * att; e.b.dy += dy * att;
      });
      // 向心力 + **类型扇区锚力**（弱：0.016，只负责成簇，不硬掰连线）
      nodes.forEach((v) => {
        v.dx += (cx - v.x) * 0.012 + (v._ax - v.x) * 0.016;
        v.dy += (cy - v.y) * 0.012 + (v._ay - v.y) * 0.016;
      });
      nodes.forEach((v) => {
        const dl = Math.hypot(v.dx, v.dy) || 0.01;
        v.x = clamp(v.x + (v.dx / dl) * Math.min(dl, temp), v.r + 12, LW - v.r - 12);
        v.y = clamp(v.y + (v.dy / dl) * Math.min(dl, temp), v.r + 12, LH - v.r - 12);
      });
      temp -= cool;
    }
  };

  // ── 标签感知消重（FR 收敛之后跑）──
  // FR 只把「圆盘」推开，可屏幕上真正互相盖住的是**名字**：`.kg-nlabel` 挂在圆盘正下方、
  // nowrap、最宽 118px。只按 r 判定不重叠，标签照样会叠成一片。
  // 故按「包含标签的轴对齐盒」做分离：半宽取 max(r, 标签半宽)，半高取 r + 标签行高。
  const LBL_H = 15;                                   // 标签行高 + 与圆盘的 4px 间距
  const lblHalf = (nd) => Math.min(59, (String(nd.name || "").length * 6.2 + 10) / 2);
  nodes.forEach((nd) => { nd._hw = Math.max(nd.r, lblHalf(nd)) + 7; nd._hh = nd.r + LBL_H + 5; });
  const relax = (iters) => {
    for (let it = 0; it < iters; it++) {
      let moved = 0;
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const ox = (a._hw + b._hw) - Math.abs(a.x - b.x);
        const oy = (a._hh + b._hh) - Math.abs(a.y - b.y);
        if (ox <= 0 || oy <= 0) continue;             // 盒不相交
        // 沿重叠较小的轴分离（位移最小 = 布局扰动最小）
        if (ox < oy) {
          const s = (a.x <= b.x ? 1 : -1) * ox * 0.5;
          a.x -= s; b.x += s;
        } else {
          const s = (a.y <= b.y ? 1 : -1) * oy * 0.5;
          a.y -= s; b.y += s;
        }
        moved++;
      }
      if (!moved) break;                              // 已无重叠即收敛，不空转
    }
  };
  // 收敛后把画布收到实际包围盒（含标签盒），并把坐标系整体平移到正区间。
  // 锚点与中心一并平移，重排(relayout)才不会在旧坐标系里发力。
  const M = 46;
  const fitWorld = () => {
    let a = 1e9, b = 1e9, c = -1e9, d2 = -1e9;
    nodes.forEach((nd) => {
      a = Math.min(a, nd.x - nd._hw); b = Math.min(b, nd.y - nd._hh);
      c = Math.max(c, nd.x + nd._hw); d2 = Math.max(d2, nd.y + nd._hh);
    });
    const ox = M - a, oy = M - b;
    nodes.forEach((nd) => {
      nd.x += ox; nd.y += oy; nd._ax += ox; nd._ay += oy;
      if (nd._sx !== undefined) { nd._sx += ox; nd._sy += oy; }   // 开场动画起点同坐标系
    });
    cx += ox; cy += oy;
    W = Math.round(c - a + M * 2); H = Math.round(d2 - b + M * 2);
  };

  // 预演：记初始（扇区环）为动画起点，收敛终点存 tx/ty，x/y 复位到起点 → 开场从扇区铺开
  nodes.forEach((nd) => { nd._sx = nd.x; nd._sy = nd.y; });
  simulate(320);
  relax(300);
  fitWorld();
  nodes.forEach((nd) => { nd.tx = nd.x; nd.ty = nd.y; nd.x = nd._sx; nd.y = nd._sy; });

  // ── DOM ──
  const stage = h("div", { class: "kg-stage" });
  const stars = h("div", { class: "kg-stars" });   // 星空背景（缓慢漂移 + 微闪，节点如悬于星海）
  const world = h("div", { class: "kg-world" });
  const applyWorldSize = () => {
    world.style.width = W + "px"; world.style.height = H + "px";
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", W); svg.setAttribute("height", H);
  };
  world.style.width = W + "px"; world.style.height = H + "px";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "kg-edges");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  const defs = document.createElementNS(NS, "defs");
  Object.entries(KG_KIND).forEach(([key, v]) => {
    const m = document.createElementNS(NS, "marker");
    m.setAttribute("id", `kg-arrow-${key}`); m.setAttribute("viewBox", "0 0 8 8");
    m.setAttribute("refX", "7"); m.setAttribute("refY", "4");
    m.setAttribute("markerWidth", "6"); m.setAttribute("markerHeight", "6");
    m.setAttribute("orient", "auto-start-reverse");
    const pt = document.createElementNS(NS, "path");
    pt.setAttribute("d", "M0 0 L8 4 L0 8 Z"); pt.style.fill = v.c;   // 内联 style 才可靠解析 var()
    m.append(pt); defs.append(m);
  });
  svg.append(defs);
  const edgeEls = edges.map((e) => {
    const p = document.createElementNS(NS, "path");
    const kk = kgKindKey(e.kind);
    p.setAttribute("class", "kg-edge" + (kk === "member" || kk === "neutral" ? " dashed" : ""));
    p.style.stroke = KG_KIND[kk].c;
    if (e.directed) p.setAttribute("marker-end", `url(#kg-arrow-${kk})`);
    svg.append(p); return p;
  });
  const labelEls = edges.map((e) => {
    if (!e.relation) return null;
    const t = document.createElementNS(NS, "text");
    t.setAttribute("class", "kg-elabel"); t.setAttribute("text-anchor", "middle");
    t.textContent = e.relation; svg.append(t); return t;
  });

  let selected = null, mode = null, dragNode = null, shown = false;
  const paint = (sel) => {
    const nb = new Set(), litE = new Set();
    if (sel) { nb.add(sel.id); edges.forEach((e, i) => { if (e.a.id === sel.id || e.b.id === sel.id) { nb.add(e.a.id); nb.add(e.b.id); litE.add(i); } }); }
    stage.classList.toggle("sel", !!sel);
    nodes.forEach((nd) => { nd._el.classList.toggle("dim", !!sel && !nb.has(nd.id)); nd._el.classList.toggle("hot", !!sel && nb.has(nd.id)); nd._el.classList.toggle("pick", !!sel && nd.id === sel.id); });
    edgeEls.forEach((p, i) => { p.classList.toggle("hot", litE.has(i)); p.classList.toggle("dim", !!sel && !litE.has(i)); });
    labelEls.forEach((l, i) => l && l.classList.toggle("show", litE.has(i)));
  };
  const nodeEls = nodes.map((nd) => {
    const t = kgType(nd.type);
    const el = h("div", { class: "kg-node t-" + (nd.type || "other"),
      dataset: { id: nd.id, tip: `${nd.name} · ${t.zh}` + (nd.desc ? "\n" + nd.desc : "") },
      onmouseenter: () => { if (!mode) paint(nd); },
      onmouseleave: () => { if (!mode) paint(selected); } });
    el.style.setProperty("--nc", t.c);
    const disc = h("div", { class: "kg-disc" });
    disc.style.width = disc.style.height = nd.r * 2 + "px";
    disc.append(nd.thumb ? h("img", { src: nd.thumb, loading: "lazy", alt: "", draggable: "false" })
      : h("span", { class: "kg-init" }, (nd.name || "?").slice(0, 2)));
    el.append(disc, h("i", { class: "kg-nlabel" }, nd.name));
    nd._el = el; world.append(el); return el;
  });
  world.insertBefore(svg, world.firstChild);   // 连线层压在节点下

  const place = () => {
    nodes.forEach((nd) => { nd._el.style.left = nd.x + "px"; nd._el.style.top = nd.y + "px"; });
    edges.forEach((e, i) => {
      let dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, dist = Math.hypot(dx, dy) || 0.01;
      const ux = dx / dist, uy = dy / dist;
      const x1 = e.a.x + ux * e.a.r, y1 = e.a.y + uy * e.a.r;
      const x2 = e.b.x - ux * (e.b.r + (e.directed ? 4 : 0)), y2 = e.b.y - uy * (e.b.r + (e.directed ? 4 : 0));
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      // 多关系按道次扇开（canon 让反向边也分到物理两侧不重合）；单关系保留轻微弧度
      let bow;
      if (e._grp > 1) {
        const canon = String(e.source) < String(e.target) ? 1 : -1;
        bow = e._lane * Math.min(30, dist * 0.14 + 14) * canon;
      } else {
        bow = Math.min(26, dist * 0.09) * (i % 2 ? 1 : -1);
      }
      const qx = mx - uy * bow, qy = my + ux * bow;
      edgeEls[i].setAttribute("d", `M ${x1} ${y1} Q ${qx} ${qy} ${x2} ${y2}`);
      if (labelEls[i]) { labelEls[i].setAttribute("x", qx); labelEls[i].setAttribute("y", qy - 3); }
    });
  };

  // ── 缩放 / 平移（滚轮走 rAF 缓动 + 光标锚定：慢而顺滑，不一跳一跳）──
  let k = 1, tx = 0, ty = 0, kGoal = 1, zAnchor = null, zRaf = 0, zIdleTimer = 0;
  const zlabel = h("span", { class: "kg-zoom" }, "100%");
  const apply = () => {
    world.style.transform = `translate(${tx}px,${ty}px) scale(${k})`;
    zlabel.textContent = Math.round(k * 100) + "%";
    // 星际穿梭：星层随缩放做分层视差推拉（近层大幅、远层小幅）——放大=前进、缩小=后退
    const dz = k - 1;
    stars.style.setProperty("--warp-near", (1 + dz * 0.72).toFixed(3));
    stars.style.setProperty("--warp-far", (1 + dz * 0.30).toFixed(3));
  };
  const fit = () => {
    const sw = stage.clientWidth, sh = stage.clientHeight;
    if (!sw || !sh) return;
    let a = 1e9, b = 1e9, c = -1e9, dd = -1e9;
    nodes.forEach((nd) => {
      a = Math.min(a, nd.tx - nd._hw); b = Math.min(b, nd.ty - nd._hh);
      c = Math.max(c, nd.tx + nd._hw); dd = Math.max(dd, nd.ty + nd._hh);
    });
    const cw = c - a, ch = dd - b;
    k = clamp(Math.min(sw / cw, sh / ch) * 0.92, 0.3, 1.7);
    tx = (sw - cw * k) / 2 - a * k; ty = (sh - ch * k) / 2 - b * k;
    kGoal = k; cancelAnimationFrame(zRaf); zRaf = 0; apply();   // 同步目标缩放，免下次滚轮从陈旧目标跳变
    world.style.willChange = "auto";                            // 稳定态撤图层缓存→按当前比例重栅格=高清
  };
  // 光标锚定：缩放过程中 (mx,my) 屏幕点对应的世界点恒定在光标下
  const applyZoomAnchored = () => {
    if (zAnchor) { tx = zAnchor.mx - zAnchor.wx * k; ty = zAnchor.my - zAnchor.wy * k; }
    apply();
  };
  const zTick = () => {
    const d = kGoal - k;
    if (Math.abs(d) < 0.0008) {           // 缩放settle：不立刻撤图层——停手 220ms 才重栅格，避免连续缩放反复重栅抖动
      k = kGoal; applyZoomAnchored(); zRaf = 0;
      clearTimeout(zIdleTimer);
      zIdleTimer = setTimeout(() => { world.style.willChange = "auto"; }, 220);   // 稳定后重栅格=文字高清
      return;
    }
    k += d * 0.2;                          // 缓动系数越小越顺（0.2 ≈ 舒适手感）
    applyZoomAnchored();
    zRaf = requestAnimationFrame(zTick);
  };
  const zoomToward = (mx, my, factor) => {
    kGoal = clamp(kGoal * factor, 0.3, 3);
    zAnchor = { mx, my, wx: (mx - tx) / k, wy: (my - ty) / k };   // 锚点按当前渲染 k 推导
    clearTimeout(zIdleTimer);                                    // 又开始缩放→取消待撤图层，保持 GPU 加速
    world.style.willChange = "transform";                        // 缩放中促成 GPU 图层→动画流畅
    if (!zRaf) zRaf = requestAnimationFrame(zTick);
  };
  const zoomBy = (f) => { const r = stage.getBoundingClientRect(); zoomToward(r.width / 2, r.height / 2, f); };
  stage.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const r = stage.getBoundingClientRect();
    let dy = ev.deltaY;                    // 行/页滚设备归一到像素
    if (ev.deltaMode === 1) dy *= 16; else if (ev.deltaMode === 2) dy *= r.height || 600;
    const factor = Math.exp(clamp(-dy, -180, 180) * 0.0012);      // 灵敏度调低 + 每步封顶 = 慢
    zoomToward(ev.clientX - r.left, ev.clientY - r.top, factor);
  }, { passive: false });

  // ── 详情卡 ──
  const detail = h("div", { class: "kg-detail", hidden: true });
  // 详情卡可自由拖动：从卡片空白/头像旁按下拖动，交互元素（按钮/链接/关系行/头像）不触发
  let dDrag = null;
  detail.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest("button, a, img, .kg-hero, .kg-rel")) return;
    const cr = detail.getBoundingClientRect(), sr = stage.getBoundingClientRect();
    const l = cr.left - sr.left, tp = cr.top - sr.top;
    detail.style.transform = "none"; detail.style.left = l + "px"; detail.style.top = tp + "px";
    dDrag = { sx: ev.clientX, sy: ev.clientY, l, t: tp };
    detail.classList.add("dragging"); detail.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  detail.addEventListener("pointermove", (ev) => {
    if (!dDrag) return;
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const nl = clamp(dDrag.l + (ev.clientX - dDrag.sx), 6, Math.max(6, sw - detail.offsetWidth - 6));
    const nt = clamp(dDrag.t + (ev.clientY - dDrag.sy), 6, Math.max(6, sh - detail.offsetHeight - 6));
    detail.style.left = nl + "px"; detail.style.top = nt + "px";
  });
  const dEnd = () => { dDrag = null; detail.classList.remove("dragging"); };
  detail.addEventListener("pointerup", dEnd);
  detail.addEventListener("pointercancel", dEnd);
  const showDetail = (nd) => {
    detail.style.left = ""; detail.style.top = ""; detail.style.transform = "";   // 每次选中复位到左侧中间
    detail.classList.remove("dragging");
    const t = kgType(nd.type);
    const rels = edges.filter((e) => e.a.id === nd.id || e.b.id === nd.id).map((e) => {
      const out = e.a.id === nd.id, other = out ? e.b : e.a, kk = KG_KIND[kgKindKey(e.kind)];
      return h("button", { class: "kg-rel", onclick: () => focusNode(other) },
        h("span", { class: "kg-rel-dot", style: `background:${kk.c}` }),
        h("span", { class: "kg-rel-rel" }, e.relation || kk.zh),
        h("span", { class: "kg-rel-ar" }, e.directed ? (out ? "▸" : "◂") : "·"),
        h("b", null, other.name));
    });
    detail.textContent = "";
    detail.classList.toggle("has-hero", !!nd.thumb);
    const openSheet = () => openLightbox([{ src: nd.thumb, title: `${t.zh} · ${nd.name}`,
      caption: nd.desc || nd.name,
      actx: nd.ref ? { pid, kind: nd.ref.kind, name: nd.ref.name, comments: [] } : null }]);
    // 有设定图的节点：整行英雄图（固定高度）替代小头像。
    // 前景 object-fit:contain——角色设定图是 16:9 三区 model sheet，cover 裁掉的正是
    // 正脸肖像或全身像其中一半；底衬用同图放大模糊做毛玻璃，避免 contain 出现黑边。
    const hero = nd.thumb ? h("div", { class: "kg-hero", dataset: { tip: "点开看完整设定图（可重生成 / 点评）" },
      onclick: openSheet },
      h("div", { class: "kg-hero-bg", style: `background-image:url("${nd.thumb}")` }),
      h("img", { class: "kg-hero-img", src: nd.thumb, alt: "", loading: "lazy", draggable: "false" }),
      h("div", { class: "kg-hero-scrim" }),
      h("span", { class: "kg-hero-zoom", html: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 2.5h-4v4M9.5 13.5h4v-4M13.5 6.5v-4h-4M2.5 9.5v4h4"/></svg>' }),
      h("div", { class: "kg-hero-cap" },
        h("div", { class: "kg-hero-name" }, nd.name),
        h("div", { class: "kg-detail-tags" },
          h("span", { class: "kg-badge", style: `color:${t.c};border-color:${t.c}` }, t.zh),
          nd.faction ? h("span", { class: "kg-detail-fac" }, nd.faction) : null,
          nd.role ? h("span", { class: "kg-detail-fac" }, nd.role) : null))) : null;
    // DOM append(null) 会渲成字面 "null" 文本——条件子节点先过滤（与 h() 的 null 跳过对齐）
    detail.append(...[
      h("button", { class: "kg-detail-x", onclick: () => select(null) }, "×"),
      hero,
      hero ? null : h("div", { class: "kg-detail-hd" },
        h("div", { class: "kg-detail-meta" },
          h("div", { class: "kg-detail-name" }, nd.name),
          h("div", { class: "kg-detail-tags" },
            h("span", { class: "kg-badge", style: `color:${t.c};border-color:${t.c}` }, t.zh),
            nd.faction ? h("span", { class: "kg-detail-fac" }, nd.faction) : null,
            nd.role ? h("span", { class: "kg-detail-fac" }, nd.role) : null))),
      h("div", { class: "kg-detail-body" }, ...[
        nd.desc ? h("p", { class: "kg-detail-desc" }, nd.desc) : null,
        rels.length ? h("div", { class: "kg-detail-rels" }, ...rels) : null,
      ].filter(Boolean)),
    ].filter(Boolean));
    detail.hidden = false;
  };
  const select = (nd) => { selected = nd; paint(nd); if (nd) showDetail(nd); else detail.hidden = true; };
  const focusNode = (nd) => { select(nd); const r = stage.getBoundingClientRect(); tx = r.width / 2 - nd.x * k; ty = r.height / 2 - nd.y * k; apply(); };

  // ── 动画（开场铺开 / 重新布局）：rAF lerp，节点与连线同帧刷新，不脱节 ──
  let raf = 0;
  const animateTo = (dur) => {
    nodes.forEach((nd) => { nd._fx = nd.x; nd._fy = nd.y; });
    const t0 = performance.now(); cancelAnimationFrame(raf);
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      nodes.forEach((nd) => { nd.x = nd._fx + (nd.tx - nd._fx) * e; nd.y = nd._fy + (nd.ty - nd._fy) * e; });
      place(); if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
  };
  const relayout = () => {
    const cur = nodes.map((nd) => ({ x: nd.x, y: nd.y }));
    // 从各自的类型扇区重新起手（而非一圈无差别圆环），随机量只做小扰动换个排法
    nodes.forEach((nd) => {
      const jit = (Math.random() - 0.5) * 0.5;
      nd.x = nd._ax + Math.cos(jit * 6) * 40 * (0.5 + Math.random());
      nd.y = nd._ay + Math.sin(jit * 6) * 40 * (0.5 + Math.random());
    });
    simulate(320);
    relax(300);                               // 与首次布局同源：漏掉这行，重排后标签又会叠回去
    fitWorld(); applyWorldSize();             // 画布重新收到新包围盒，否则连线层与节点层错位
    nodes.forEach((nd, i) => { nd.tx = nd.x; nd.ty = nd.y; nd.x = cur[i].x; nd.y = cur[i].y; });
    fit(); animateTo(700);
  };

  // ── 拖拽（背景平移 / 节点搬动） + 点选 ──
  let sx = 0, sy = 0, moved = 0, stx = 0, sty = 0;
  stage.addEventListener("pointerdown", (ev) => {
    // 悬浮控件（工具条/图例/详情卡/摘要侧栏）自行处理点击——不触发平移与指针捕获，
    // 否则 setPointerCapture 会吞掉按钮的 click（缩放/重布局/全屏按钮点了没反应的根因）
    if (ev.target.closest(".kg-ui")) return;
    const ne = ev.target.closest(".kg-node");
    moved = 0; sx = ev.clientX; sy = ev.clientY;
    if (ne) {
      mode = "node"; dragNode = byId[ne.dataset.id];
      ev.preventDefault();          // 压掉原生图片/选区拖拽：节点上按下即「搬节点」，没有第二种解释
    }
    else { mode = "pan"; stx = tx; sty = ty; world.style.willChange = "transform"; }
    stage.setPointerCapture(ev.pointerId); stage.classList.add("grab");
  });
  stage.addEventListener("pointermove", (ev) => {
    if (!mode) return;
    const ddx = ev.clientX - sx, ddy = ev.clientY - sy; moved += Math.abs(ddx) + Math.abs(ddy);
    if (mode === "pan") { tx = stx + (ev.clientX - sx); ty = sty + (ev.clientY - sy); apply(); }
    else if (dragNode) { dragNode.x += ddx / k; dragNode.y += ddy / k; dragNode.tx = dragNode.x; dragNode.ty = dragNode.y; sx = ev.clientX; sy = ev.clientY; place(); }
  });
  const endDrag = () => {
    if (mode === "node" && dragNode && moved < 5) select(dragNode);
    else if (mode === "pan" && moved < 5) select(null);
    if (mode === "pan" && !zRaf) world.style.willChange = "auto";   // 平移结束回稳态→重栅格高清
    mode = null; dragNode = null; stage.classList.remove("grab");
  };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);

  // ── 关系摘要侧栏（左侧滑入·磨砂）：原「下方文字」搬进画布左侧，工具条图标开合 ──
  const typeGroup = (type) => {
    const arr = nodes.filter((nd) => nd.type === type); if (!arr.length) return null;
    const t = kgType(type);
    return h("div", { class: "kg-dg-group" },
      h("span", { class: "kg-dg-k", style: `color:${t.c}` }, t.zh),
      h("div", { class: "kg-dg-chips" }, arr.sort((x, y) => y.deg - x.deg).map((nd) =>
        h("button", { class: "kg-dg-chip", style: `--nc:${t.c}`, onclick: () => focusNode(nd) },
          nd.name, nd.deg ? h("span", { class: "kg-dg-deg" }, String(nd.deg)) : null))));
  };
  const RELCAP = 60;
  const relRows = edges.slice(0, RELCAP).map((e) => {
    const kk = KG_KIND[kgKindKey(e.kind)];
    return h("button", { class: "kg-dg-rel", onclick: () => focusNode(e.a) },
      h("b", null, e.a.name),
      h("span", { class: "kg-dg-tag", style: `color:${kk.c};border-color:${kk.c}` }, e.relation || kk.zh),
      h("span", { class: "kg-dg-ar" }, e.directed ? "▸" : "—"), h("b", null, e.b.name));
  });
  const side = h("div", { class: "kg-side kg-ui", hidden: true },
    h("div", { class: "kg-side-hd" },
      h("span", { class: "kg-side-t" }, "关系摘要", h("i", null, "DIGEST")),
      h("button", { class: "kg-side-x", dataset: { tip: "收起摘要" }, onclick: () => toggleSide(false) }, "×")),
    h("div", { class: "kg-side-body" }, ...[
      graph.summary ? h("p", { class: "kg-summary" }, graph.summary) : null,
      h("div", { class: "kg-dg-sec" }, h("span", { class: "kg-dg-h" }, "角色 / 阵营 / 地点 / 世界观"),
        ...["character", "faction", "location", "item", "worldview"].map(typeGroup).filter(Boolean)),
      edges.length ? h("div", { class: "kg-dg-sec" }, h("span", { class: "kg-dg-h" }, `核心关系 · ${edges.length}`),
        h("div", { class: "kg-dg-rels" }, ...relRows),
        edges.length > RELCAP ? h("p", { class: "kg-dg-more" }, `… 另有 ${edges.length - RELCAP} 条关系（图中可见全部）`) : null) : null,
    ].filter(Boolean)));
  let sideOpen = false;
  const toggleSide = (on) => {
    sideOpen = on == null ? !sideOpen : on;
    side.hidden = !sideOpen; side.classList.toggle("open", sideOpen);
    sideBtn.classList.toggle("on", sideOpen);
  };

  // ── 全屏（Fullscreen API 作用于舞台，退出即复位）──
  const toggleFull = () => {
    if (document.fullscreenElement === stage) { document.exitFullscreen && document.exitFullscreen(); }
    else if (stage.requestFullscreen) stage.requestFullscreen();
  };
  const onFsChange = () => {
    if (!host.isConnected) { document.removeEventListener("fullscreenchange", onFsChange); return; }
    const on = document.fullscreenElement === stage;
    stage.classList.toggle("kg-full", on);
    fsBtn.classList.toggle("on", on);
    fsBtn.dataset.tip = on ? "退出全屏" : "全屏展示";
    requestAnimationFrame(fit);
  };
  document.addEventListener("fullscreenchange", onFsChange);

  // ── 工具条 / 图例 / 悬浮标题 ──
  const tbtn = (txt, tip, fn) => h("button", { class: "kg-tool", dataset: { tip }, onclick: fn }, txt);
  const sideBtn = tbtn("≡", "关系摘要（角色 / 阵营 / 核心关系一览）", () => toggleSide());
  const fsBtn = tbtn("⛶", "全屏展示", () => toggleFull());
  const toolbar = h("div", { class: "kg-toolbar kg-ui" },
    sideBtn, h("span", { class: "kg-tool-sep" }),
    tbtn("－", "缩小", () => zoomBy(1 / 1.25)), zlabel, tbtn("＋", "放大", () => zoomBy(1.25)),
    h("span", { class: "kg-tool-sep" }),
    tbtn("↻", "重新布局（重新构建并居中）", () => relayout()), fsBtn);
  const titleBar = h("div", { class: "kg-title kg-ui" },
    h("span", { class: "kg-title-t" }, "关系图谱"),
    h("span", { class: "kg-title-sub" }, `${nodes.length} 节点 · ${edges.length} 关系`),
    opts.regenBtn || null);
  const presentTypes = [...new Set(nodes.map((nd) => nd.type))].filter((t) => KG_TYPE[t]);
  const presentKinds = [...new Set(edges.map((e) => kgKindKey(e.kind)))];
  const legend = h("div", { class: "kg-legend kg-ui" },
    presentTypes.map((t) => h("span", { class: "kg-leg" }, h("i", { class: "kg-leg-dot", style: `background:${KG_TYPE[t].c}` }), KG_TYPE[t].zh)),
    presentKinds.length ? h("span", { class: "kg-leg-sep" }) : null,
    presentKinds.map((kk) => h("span", { class: "kg-leg" }, h("i", { class: "kg-leg-ln", style: `background:${KG_KIND[kk].c}` }), KG_KIND[kk].zh)));
  detail.classList.add("kg-ui");
  // stars 垫底 → world 节点/连线 → 悬浮件（标题/工具条/图例/摘要侧栏/详情卡皆 kg-ui 不触发平移）
  stage.append(stars, world, titleBar, toolbar, legend, side, detail);

  host.append(stage);   // 整个模块即画布：摘要、标题、工具皆浮于画布之上
  place();
  const onResize = () => { if (!host.isConnected) { window.removeEventListener("resize", onResize); return; } fit(); };
  window.addEventListener("resize", onResize);
  return {
    onShow: () => requestAnimationFrame(() => {
      fit();
      if (!shown) { shown = true; requestAnimationFrame(() => animateTo(900)); }
    }),
  };
}

/* 时间线视图：分镜按时长横排 + 音轨对齐；点块跳到对应分镜卡 */
function timelineStrip(d) {
  const shots = (d.shots || []).filter((s) => !s.omitted && s.dur > 0);
  if (shots.length < 2) return null;
  const total = shots.reduce((a, s) => a + (+s.dur || 0), 0);
  if (!total) return null;
  const usesVideo = !!d.uses_video;   // 判据服务端下发（Project.uses_seedance）
  let t = 0;
  const blocks = shots.map((s) => {
    const isTr = s.kind === "transition";
    const tr = s.transition || {};
    const start = t; t += +s.dur || 0;
    const el = h("div", { class: "tl-block"
          + (isTr ? " tl-tr" + (tr.type === "fade_white" ? " white" : "") : ""),
        style: `flex-grow:${s.dur};`
          + (!isTr && s.image ? `background-image:url('${s.image}')` : ""),
        dataset: { tip: isTr
          ? `转场镜 ${s.id} · ${TRANSITION_ZH[tr.type] || "转场"}`
            + (tr.text ? `「${tr.text}」` : "（纯色停顿）")
            + `\n${fmtSec(s.dur)} · ${fmtDur(start)}~${fmtDur(t)} · 点击跳到转场卡`
          : `SHOT ${s.id} · ${fmtSec(s.dur)} · ${fmtDur(start)}~${fmtDur(t)}\n`
            + (s.narration || "（纯画面镜·静音占位）") + "\n点击跳到分镜卡" },
        onclick: () => document.getElementById(`shot-${s.id}`)
          ?.scrollIntoView({ block: "center", behavior: "smooth" }) },
      h("span", { class: "tl-tag" }, String(s.id)),
      isTr && h("span", { class: "tl-tr-txt" },
        h("i", { class: "tl-tr-ico" }, "⧗"), tr.text || TRANSITION_ZH[tr.type] || "转场"),
      h("span", { class: "tl-dur" }, fmtSec(s.dur)));
    return el;
  });
  // 音轨行兼作状态条：块底色 = 该镜审阅状态（取代缩略图角标），♪ 表示有旁白
  const audio = h("div", { class: "tl-audio" }, shots.map((s) => {
    const isTr = s.kind === "transition";
    const ast = (s.review || {})[usesVideo ? "clip" : "image"] || "todo";
    const hasN = (s.narration || "").trim();
    return h("div", { class: "tl-ablock"
        + (isTr ? " tl-tr" : (ast !== "todo" ? " " + (REVIEW[ast]?.cls || "") : ""))
        + (ast === "wip" ? " wip" : ""),
        style: `flex-grow:${s.dur}`,
        dataset: { tip: isTr ? "转场（无音轨）"
          : `${REVIEW[ast]?.zh || "待办"} · ${hasN ? "♪ 有旁白" : "无台词（静音占位）"}` } },
      isTr ? "" : (hasN ? "♪" : ""));
  }));
  return h("div", { class: "card tl-card" },
    h("span", { class: "k" }, "时间线 · TIMELINE",
      h("span", { class: "sub" }, `${fmtDur(total)} · ${shots.length} 镜`)),
    h("div", { class: "tl-strip" }, blocks),
    audio,
    d.assets?.bgm && h("div", { class: "tl-bgm" }, h("i"), "BGM"));
}

/* 分镜脚本（镜头表）：施工图级专业视图——
   镜号/景别/角度/运镜/镜头/光线/转场/时长/说话人/台词/字幕/情绪/语音指令全列；
   行点击展开双语提示词（中文主行+英文次行）；转场镜渲染为跨列分隔行；
   弃用镜（omt）整行降透明并标注；斑马行 · 吸顶表头 · 容器内横向滚动。 */
const SB_OPEN = new Set();   // 展开态（项目/章节/镜号）——轮询重绘后保留

/* 实发提示词（/api/video-preview）章级缓存：分镜卡逐镜展开共享同一次整章编译，
   10s 内复用、过期重算——编辑提示词后再展开拿到的恒是当前字段编译出的那句 */
const PV_CACHE = new Map();
/* 提示词折叠区展开态（`pid/cid/镜号`）：表态/审阅写盘会经 updated_at 触发轮询
   整卡重绘（数据驱动的既定行为），不持久化的话折叠区每次都被重绘收起 */
const PF_OPEN = new Set();
function chapterSendPreview(d) {
  const k = `${d.project}/${d.id}`;
  const hit = PV_CACHE.get(k);
  if (hit && Date.now() - hit.t < 10000) return hit.p;
  const p = api(`/api/video-preview?project=${encodeURIComponent(d.project)}`
    + `&id=${encodeURIComponent(d.id)}`);
  PV_CACHE.set(k, { t: Date.now(), p });
  p.catch(() => PV_CACHE.delete(k));   // 失败不占坑，收起再展开即重试
  return p;
}

/* 实发稿里的引用记号（@图片N / 参考音频N）→ 实附文件：编号映射由引擎按实附顺序
   下发（row.refs / row.anchors），点击即看/即听——前端绝不自己数编号，
   数错一位就把场景图当角色图给人看 */
/* kind 全集与 refplan.py 的 _PLACEHOLDER ∪ _SHEET_KINDS 对齐——表漏一项，
   浮窗归属签就露英文码（实测 scene_base / scene_top）。表外新 kind 仍回落原码。 */
const PV_KIND_ZH = { frame: "本镜画面", board: "简笔分镜板", tail: "上镜尾帧",
  character: "角色设定图", scene: "场景设定图", scene_main: "主场景设定图",
  scene_base: "场景基准图", scene_top: "场景俯视布局图",
  scene_top_main: "主场景俯视布局图", prop: "道具设定图" };

function pvPeekImage(tok, ref) {
  openShell({ card: "skb-dlg pv-peek-dlg", build: (close) => [
    h("div", { class: "rf-head" },
      h("span", { class: "k" }, `${tok} · ${PV_KIND_ZH[ref.kind] || ref.kind}`
        + (ref.name ? `「${ref.name}」` : "")),
      h("button", { class: "rf-x", onclick: close }, "✕")),
    h("img", { class: "pv-peek", src: ref.media, alt: tok }),
  ] });
}

/* 锚定音试听。没落盘时给一条「现在合成」——这条参考音就是 native 真发时随请求
   附发的那把嗓子，听不到它就只能不试听直接开生视频去赌音色，而生视频按秒计费。
   编号→说话人→音色的映射由服务端按同一份 voice_anchor_plan 解析，前端只报镜号+编号。 */
function pvPeekAudio(tok, a, ctx) {
  openShell({ card: "skb-dlg", build: (close) => {
    const host = h("div");
    const fill = () => host.replaceChildren(
      a.media ? h("div", { class: "pv-peek-audio" }, audioPill(a.media, "试听"))
              : h("p", { class: "dlg-msg" },
                  "锚定音还没落盘。选角之后合成一句即可在此试听，真发时直接复用；"
                  + "不合成也不影响出片——生视频真发那一刻会自动补上。"));
    fill();
    return [
      h("div", { class: "rf-head" },
        h("span", { class: "k" }, `${tok} · ${a.who} 的音色锚定`),
        h("button", { class: "rf-x", onclick: close }, "✕")),
      host,
      a.media || !ctx ? null : h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn primary", onclick: async (ev) => {
          const btn = ev.currentTarget;
          try {
            const r = await runBusy(btn, "合成中…", () => post("/api/voice/anchor-warm",
              { project: ctx.project, chapter: ctx.chapter, shot: ctx.shot, no: a.no }));
            a.media = r.media;
            PV_CACHE.delete(`${ctx.project}/${ctx.chapter}`);  // 注记下次展开重算「待预热」
            btn.closest(".dlg-acts").remove();                 // 已落盘，这条动作不再成立
            fill();
          } catch (err) { toast(err.message, true); }
        } }, "♪ 合成一句试听")),
    ].filter(Boolean);
  } });
}

/* 引用记号的回写坐标（哪一章的哪一镜）——服务端按它复算编号归属 */
const pvCtx = (d, s) => ({ project: d.project, chapter: d.id, shot: s.id });

const PV_REF_RE = /@图片\s*\d+|@Image\s*\d+|参考音频\s*\d+|@音频\s*\d+|@配音\s*\d+/g;

/* @配音N 试听：dubbed 随请求附发的整镜配音，也是成片烧录的那条主音轨 */
function pvPeekDub(tok, d) {
  openShell({ card: "skb-dlg", build: (close) => [
    h("div", { class: "rf-head" },
      h("span", { class: "k" }, `${tok} · ${d.who} 的配音（成片主音轨）`),
      h("button", { class: "rf-x", onclick: close }, "✕")),
    d.media ? h("div", { class: "pv-peek-audio" }, audioPill(d.media, "试听"))
            : h("p", { class: "dlg-msg" }, "配音还没落盘——先跑 tts 再来试听。"),
  ] });
}

function pvRich(text, row, ctx) {
  const out = [];
  let last = 0;
  let m;
  PV_REF_RE.lastIndex = 0;
  while ((m = PV_REF_RE.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const no = +(tok.match(/\d+/) || [0])[0];
    const img = tok.includes("图片") || tok.includes("Image");
    const dub = tok.includes("配音");
    if (dub) {
      /* @配音N 两种实体共用一套记号：dubbed 是随请求附发的整镜配音（成片主音轨），
         native 是该说话人的音色锚定参考音。编号先查 row.dub 再查 row.anchors，
         真源都是引擎按 content[] 附发顺序下发 */
      const d = row.dub && row.dub.no === no ? row.dub : null;
      const a = d ? null : (row.anchors || []).find((x) => x.no === no);
      if (d && d.media) {
        out.push(h("a", { class: "pv-ref", dataset: { tip: "点击试听本镜配音（成片主音轨）" },
          onclick: (e) => { e.preventDefault(); e.stopPropagation();
            pvPeekDub(tok, d); } }, tok));
      } else if (a) {
        out.push(h("a", { class: "pv-ref",
          dataset: { tip: `${a.who} 的音色锚定`
            + (a.pending ? "·待预热（点击可先合成试听）" : "，点击试听") },
          onclick: (e) => { e.preventDefault(); e.stopPropagation();
            pvPeekAudio(tok, a, ctx); } }, tok));
      } else {
        out.push(h("span", { class: "pv-ref pv-ref-dim" }, tok));
      }
      last = m.index + tok.length;
      continue;
    }
    const hit = img ? (row.refs || []).find((x) => x.no === no)
                    : (row.anchors || []).find((x) => x.no === no);
    if (hit && (hit.media || !img)) {
      // 图片引用悬停即出缩略图（data-tip-img 走 #sys-tip 的图片槽），点击才开大图
      const cap = img
        ? `${PV_KIND_ZH[hit.kind] || hit.kind}${hit.name ? `「${hit.name}」` : ""}`
        : "点击试听该音色锚定样本";
      out.push(h("a", { class: "pv-ref",
        dataset: img ? { tip: cap, tipImg: hit.media } : { tip: cap },
        onclick: (e) => { e.preventDefault(); e.stopPropagation();
          if (img) pvPeekImage(tok, hit); else pvPeekAudio(tok, hit, ctx); } }, tok));
    } else {
      out.push(h("span", { class: "pv-ref pv-ref-dim" }, tok));
    }
    last = m.index + tok.length;
  }
  out.push(text.slice(last));
  return out;
}
function storyboardTable(d) {
  const shots = d.shots || [];
  if (!shots.length) return null;
  const dash = (v) => (v == null || v === "" ? h("span", { class: "sb-dim" }, "—") : v);
  const head = h("thead", null, h("tr", null,
    ["镜号", "景别", "角度", "运镜", "镜头", "光线", "转场", "时长", "说话人",
     "台词", "字幕", "情绪", "语音指令"].map((t) => h("th", null, t))));
  const body = h("tbody");
  let row_n = 0;
  for (const s of shots) {
    if (s.kind === "transition") {   // 转场镜：跨列分隔行（类型/字卡/音效）
      const t = s.transition || {};
      body.append(h("tr", { class: "sb-row sb-trrow" },
        h("td", { class: "sb-no" },
          h("span", { class: "cev cev-tr" }, "⧗"), String(s.id).padStart(2, "0")),
        h("td", { colspan: "12", class: "sb-tr-txt" },
          `转场 · ${TRANSITION_ZH[t.type] || t.type || "缺省"}`
          + (t.text ? ` · 字卡「${t.text}」` : " · 无字卡")
          + (s.dur ? ` · ${fmtSec(s.dur)}` : "")
          + ` · 音效 ${t.sound || "缺省"}`)));
      continue;
    }
    const key = `${d.project}/${d.id}/${s.id}`;
    const alt = row_n++ % 2 === 1;
    const expandable = !!(s.image_prompt || s.image_prompt_en || s.video_prompt
                          || s.video_prompt_en);
    const open = expandable && SB_OPEN.has(key);
    const emo = s.emotion && h("span", null, displayEmotion(s.emotion),
      s.emotion_scale != null && h("span", { class: "sb-num sb-dim" }, ` ·${s.emotion_scale}`));
    // 配音表现力契约：重读词/表演提示/停顿——与语音指令同格（停顿仅本地渲染模式
    // kenburns 生效，已折进 dur）
    const dv = s.delivery || {};
    // 重读词切分与后端 voicecast._emphasis_words 同口径：字符串按 、,，/| 切、去空去重、限 8
    // ——两边不一致时引擎发的是「重读「一定」「回来」」而网页显示「重读「一定、回来」」
    const emph = (Array.isArray(dv.emphasis) ? dv.emphasis
      : String(dv.emphasis == null ? "" : dv.emphasis).split(/[、,，/|]/))
      .map((w) => String(w).trim()).filter((w, i, a) => w && a.indexOf(w) === i).slice(0, 8);
    const dvBits = [emph.length ? `重读${emph.map((w) => `「${w}」`).join("")}` : null,
      dv.note || null,
      (dv.pause_before || dv.pause_after)
        ? `停顿 ${(+dv.pause_before || 0).toFixed(1)}/${(+dv.pause_after || 0).toFixed(1)}s`
        + (d.motion === "kenburns" ? "" : "（本模式不生效）") : null].filter(Boolean);
    // 子节点条件必须**布尔化**：h() 只跳过 null/false，数字 0 会被 String(c) 渲染成文本「0」
    const instr = (s.voice_instruction || dvBits.length)
      ? h("span", null, s.voice_instruction || "",
          dvBits.length > 0 && h("span", { class: "sb-dim" },
            (s.voice_instruction ? " · " : "") + dvBits.join(" · ")))
      : null;
    const row = h("tr", { class: "sb-row" + (alt ? " alt" : "")
          + (s.omitted ? " sb-omt" : "") + (expandable ? " expandable" : "")
          + (open ? " open" : ""),
        dataset: expandable ? { tip: "点击展开/收起本镜双语提示词与运动设计" } : null },
      h("td", { class: "sb-no" },
        expandable && h("span", { class: "cev" }, "›"),
        String(s.id).padStart(2, "0"),
        s.omitted && h("i", { class: "sb-omt-tag" }, "OMT 弃用")),
      h("td", null, dash(s.framing)),
      h("td", null, dash(s.angle)),
      h("td", null, dash(s.camera)),
      h("td", null, dash(s.lens)),
      h("td", null, dash(s.lighting)),
      h("td", null, dash(typeof s.transition === "string" ? s.transition : null)),
      h("td", { class: "sb-num" }, s.dur != null ? fmtSec(s.dur) : dash(null)),
      h("td", null, dash(s.speaker)),
      h("td", { class: "sb-txt" }, dash(s.narration)),
      h("td", { class: "sb-cap" }, dash(s.caption)),
      h("td", null, dash(emo)),
      h("td", { class: "sb-cap" }, dash(instr)));
    body.append(row);
    if (!expandable) continue;
    const xrow = h("tr", { class: "sb-x" },
      h("td", { colspan: "13" },
        h("div", { class: "prompt-rows" },
          promptRow("IMAGE", s.image_prompt, s.image_prompt_en),
          promptRow("MOTION", s.video_prompt, s.video_prompt_en),
          )));
    xrow.hidden = !open;
    row.addEventListener("click", () => {
      const show = xrow.hidden;
      xrow.hidden = !show;
      row.classList.toggle("open", show);
      show ? SB_OPEN.add(key) : SB_OPEN.delete(key);
    });
    body.append(xrow);
  }
  return h("div", { class: "card sb-card" },
    h("div", { class: "sb-scroll" }, h("table", { class: "sb-table" }, head, body)));
}

/* 放映合卡：成片 FINAL ｜ 样片 ANIMATIC 同卡切换——左栏只保留「陪伴型」内容 */
function screenCard(d) {
  const holder = h("div", { class: "screen-holder" });
  const strip = (el) => { el?.querySelector(":scope > .k")?.remove(); return el; };
  const show = (key) => {
    holder.innerHTML = "";
    holder.append(key === "animatic" ? strip(animaticCard(d)) : finalCard(d));
    tabs?.querySelectorAll(".scr-tab").forEach((b) =>
      b.classList.toggle("on", b.dataset.key === key));
  };
  let tabs = null;
  if (d.animatic) {
    tabs = h("div", { class: "scr-tabs" },
      h("button", { class: "scr-tab on", dataset: { key: "final" },
        onclick: () => show("final") }, "成片 FINAL"),
      h("button", { class: "scr-tab", dataset: { key: "animatic" },
        onclick: () => show("animatic") }, "样片 ANIMATIC"));
  }
  show("final");
  return h("div", { class: "screen-wrap" }, tabs, holder);
}

/* 成本迷你行：一行合计+均价，点击进成本页看台账（重内容下沉，左栏只留数字） */
function costStrip(d) {
  const total = d.cost_total || 0;   // 合计服务端下发（budget.spent_total），不复算
  const n = (d.shots || []).filter((s) => s.kind !== "transition").length;
  return h("a", { class: "cost-strip", href: "#/cost",
      dataset: { tip: "云 API 成本 · 本章实际入账\n合计与单镜均价来自成本台账"
        + "（生图/视频/配音/音乐分项）；点击打开成本页看明细与预估对照。" } },
    h("b", null, `¥${total.toFixed(2)}`),
    h("span", null, "云 API 成本"),
    n ? h("i", null, `${n} 镜 · 单镜 ¥${(total / n).toFixed(2)}`) : null,
    h("em", null, "台账 →"));
}

/* 成片自审条（QC）：verify 命令写进章节 json 的机器体检结论，原样展示。
   通过=绿✓（有「待修」时降为琥珀），硬失败=红警示并列明细。没跑过=引导去点「自审」。 */
/* verify 报告的键分两类：比例键（与 output 同构，含 hard_fail/todo）与比例无关的
 * `voice` 节（音轨全比例共用一份，按 kind 两态：旁白轨落点 / ASR 人声文字核对）。
 * voice 混进比例循环会被当成一个比例渲染出「voice · 全过」的假行。 */
function verifyStrip(d) {
  const v = d.verify || null;
  const aspects = v ? Object.keys(v).filter((k) => k !== "at" && k !== "voice") : [];
  if (!v || !aspects.length) {
    return h("div", { class: "cons-strip",
        dataset: { tip: "成片自审 · 黑屏 / 该响却哑 / 削波 / 响度 / 时长 / 字幕 / 人声等机器体检。\n"
          + "零 API 成本（纯本地探测），点「后期 → 自审」跑一次。" } },
      h("div", { class: "cons-line" },
        h("b", null, "· 尚未自审"),
        h("span", null, "点后期区「自审」跑一遍机器体检（零成本）")));
  }
  const hard = [], todo = [];
  aspects.forEach((a) => {
    (v[a].hard_fail || []).forEach((f) => hard.push(f.msg));
    (v[a].todo || []).forEach((f) => todo.push(f.msg));
  });
  const vo = v.voice && typeof v.voice === "object" ? v.voice : null;
  let voiceNote = "";
  if (vo) {
    (vo.todo || []).forEach((f) => todo.push(f.msg));
    const rows = vo.rows || [];
    if (vo.kind === "asr") {
      if (vo.available === false) voiceNote = "人声核对未跑（缺 faster-whisper）";
      else {
        // 相符与否由引擎判过并逐行写在 note 上（无 score 的行是跳过的片段）；
        // 前端再比一次阈值＝同一判据两份实现，引擎调阈值后页面读数会自己走偏
        const done = rows.filter((r) => typeof r.score === "number");
        const okN = done.filter((r) => !r.note).length;
        voiceNote = `人声核对 ${okN}/${done.length} 片段相符`
          + (rows.length > done.length ? ` · ${rows.length - done.length} 片段未核对` : "");
      }
    } else {
      const hitN = rows.filter((r) => (r.speech || []).length).length;
      voiceNote = `旁白轨落点 ${hitN}/${rows.length} 镜检出语音`;
    }
  }
  const bad = hard.length > 0;
  const tip = [`自审时间 ${v.at || "—"}`,
    ...(voiceNote ? ["♪ " + voiceNote] : []),
    ...hard.map((m) => "⊘ " + m),
    ...todo.map((m) => "⚠ " + m)].join("\n");
  return h("div", { class: "cons-strip" + (bad || todo.length ? " warn" : ""),
      dataset: { tip } },
    h("div", { class: "cons-line" },
      h("b", null, bad ? `⊘ 自审 ${hard.length} 项硬失败`
        : todo.length ? `⚠ 自审通过 · ${todo.length} 项待修` : "✓ 自审通过"),
      h("span", null, bad ? hard.join("；")
        : todo.length ? todo.join("；")
        : `${aspects.join(" / ")} · 体检全过` + (voiceNote ? ` · ${voiceNote}` : ""))));
}

/* 自审（放映区后期）：后台跑 verify——只读体检，不改任何产物。 */
function verifyBtn(d) {
  const wm = d.watermark || {};
  const btn = h("button", { class: "screen-act",
    dataset: { tip: wm.has_output
      ? "对成片跑一遍机器体检：黑屏（已排除转场黑场窗）/ 该响却哑 / 削波 / 响度 /\n"
        + "时长对不对得上分镜时间轴 / 字幕条数 / 人声（旁白轨落点或 ASR 文字核对）。\n"
        + "零 API 成本，只读不改产物。"
      : "先合成出片，才能自审" },
    onclick: async (ev) => {
      if (!wm.has_output) { toast("先合成出片，才能自审", true); return; }
      try {
        const btn = ev.currentTarget;
        const r = await runBusy(btn, "自审中…", () =>
          post("/api/verify", { project: d.project, chapter: d.id }));
        toast("自审中…（抽帧+电平探测，约几秒到几十秒）");
        if (r.job) pollJob(r.job, {
          onDone: () => { STATE.chapSig = ""; toast("✓ 自审通过——刷新看结论"); },
          onFail: (j) => toast(`自审未通过：${(j.tail || "").slice(-200)}`, true) });
      } catch (e) { toast(e.message, true); }
    } },
    h("span", { class: "screen-act-ico",
      html: '<svg viewBox="0 0 24 24"><path d="M9 12.5l2.2 2.2L15.5 10"/>'
        + '<path d="M12 3.5l7 3v5.2c0 4.2-2.9 7-7 8.8-4.1-1.8-7-4.6-7-8.8V6.5z"/></svg>' }),
    "自审");                                 // 始终两字
  return btn;
}

/* 台词血缘徽章里的产物名。**不复用 core.js 的 `STAGE_ZH`**——那是徽章位的
   单字标签（图/音/片），而这句提示要读成一句话。 */
const STALE_TEXT_ZH = { audio: "配音", clip: "片段" };

/* 一致性健康条：健康=一行绿✓（展开看锚点），异常=琥珀警示列明细 */
function consStrip(d) {
  const stale = (d.shots || []).filter((s) => (s.stale_refs || []).length);
  const missing = (d.shots || []).filter((s) => (s.missing_refs || []).length);
  const vstale = (d.shots || []).filter((s) => (s.voice_stale || []).length);
  // 与 vstale 并列不合并：一边重跑配音零成本、一边重烧片段按秒买断，
  // 合成一行必然写出一个对其中一半是错的处置
  const cstale = (d.shots || []).filter((s) => (s.voice_clip_stale || []).length);
  const tstale = (d.shots || []).filter((s) => (s.stale_text || []).length);
  const issues = [];
  stale.length && issues.push(`${stale.length} 镜引用的设定图已过期（镜 ${stale.map((s) => s.id).join(",")}）`);
  missing.length && issues.push(`${missing.length} 镜设定图不齐（镜 ${missing.map((s) => s.id).join(",")}）`);
  vstale.length && issues.push(`${vstale.length} 镜的配音出自已换掉的音色（镜 ${vstale.map((s) => s.id).join(",")}）`);
  cstale.length && issues.push(`${cstale.length} 镜的片段按已换掉的音色烧过人声（镜 ${cstale.map((s) => s.id).join(",")}）`);
  tstale.length && issues.push(`${tstale.length} 镜的配音或片段出自旧台词（镜 ${tstale.map((s) => s.id).join(",")}）`);
  const hint = [
    d.style?.seed != null ? `SEED ${d.style.seed}` : null,
    d.style?.palette ? `色板 ${d.style.palette}` : null,
    d.scene ? `场景 ${d.scene}` : null,
    stale.length || missing.length
      ? "处理：project refs 重生设定图后按提示重跑相关镜" : null,
    vstale.length ? "处理：这几镜已通过审阅故未自动置重做，要跟上新音色请重跑配音" : null,
    cstale.length ? "处理：这条人声只能重烧本镜换掉（跑配音无效）——clip 为「重做」的直接 "
      + "gen-video，已通过审阅的先打回；按秒计费，先 --dry-run 看报价" : null,
    tstale.length ? "处理：成片里念的与烧录的字幕不同源——重跑 tts / gen-video；"
      + "要全章一并置重做用 lineage mark（已通过审阅的镜需你先解锁）" : null,
  ].filter(Boolean).join("\n");
  return h("div", { class: "cons-strip" + (issues.length ? " warn" : ""),
      dataset: { tip: (issues.length
        ? "⚠ 一致性告警 · 全系列画面统一性受影响\n"
        : "✓ 一致性锚点 · 全系列画面统一的三根锚\n") + hint } },
    h("div", { class: "cons-line" },
      h("b", null, issues.length ? "⚠ 一致性告警" : "✓ 一致性锚点齐备"),
      h("span", null, issues.length ? issues.join("；")
        : "SEED / 色板 / 场景 / 角色块 已锁")));
}

/* 章级出口（两个）：弹层里补一句本次诉求 → 与纪律化标准指令合并后复制交 Claude Code。
   按「要不要调用视频模型」分成图片合成与模型合成两条并列的路，各自出 output/ 正式成片。
   与单镜版同一套防烧钱协议：dry-run 报价等确认 → 真发 → 合成过审阅闸；网页只复制指令，
   绝不直接起真发任务（gen-video 恒串行且逐秒计费，烧钱决定留给人）。 */
/* 图片合成指令：用分镜图 + Ken Burns 运镜直接出正式成片，不碰视频模型。
   与「模型合成」的分工是**要不要调用视频模型**——两者出的都是 output/ 里的正式成片，
   区别只在画面是静图运镜还是模型生成的真动态，所以做成两个并列入口而非一个笼统按钮。 */
function copyAssembleStills(d) {
  const shots = (d.shots || []).filter((s) => s.kind !== "transition" && !s.omitted);
  const withCam = shots.filter((s) => (s.camera || "").trim()).length;
  const cid = `${d.project}/${d.id}`;
  const txt = [
    `请用分镜图直接合成本章成片（静图运镜档）· 项目 ${d.project} / 章节 ${d.id}`
      + `（正镜 ${shots.length} · 写了运镜的 ${withCam} 镜）`,
    ``,
    `【这条路做什么】`,
    `· 以每镜的分镜图为画面，由 ffmpeg 施加 Ken Burns 缩放与位移，配上字幕、背景乐与画风特效，`
      + `合成 output/ 里的**正式成片**——**不调用任何视频模型，零视频成本**。`,
    `· 运镜逐镜取 shots[].camera：写了「缓慢推近 / 拉远揭示 / 左移 / 对角斜移」等会驱动对应的`
      + `运镜风格；未写的镜按内置八种风格轮换，不会所有镜同一个推法。`,
    `· \`--motion a\` 是**运行时覆盖、不落盘**：章节仍是 ${d.motion}，盘上已有的模型片段一概不取，`
      + `之后随时可以再走动态化，两者互不影响。`,
    ``,
    `【执行】`,
    `cd engine && python3 -m kinema assemble --chapter ${cid} --motion a`,
    ``,
    `【纪律】`,
    `· 正式成片出片前逐镜查审阅态，有未过审的会被拦下并列出待审镜——先按提示批准，`
      + `或加 \`--draft\` 出明确标注的草稿成片。`,
    `· 只想零成本看节奏、不占 output/：改用 \`animatic --chapter ${cid}\`。`,
  ].join("\n");
  return openDirectiveDialog({
    title: "图片合成指令", code: "CHAPTER · STILLS CUT",
    ask: "在此写本次出片要求",
    meta: `项目 ${d.project} / 章节 ${d.id} · 正镜 ${shots.length} · 零视频成本`,
    directive: txt,
    hint: "例：字幕先不要烧；或：第 5 镜改成缓慢拉远",
    note: "留空即按上面的步骤执行。这条路不调用视频模型，不产生按秒计费。",
    done: "图片合成指令已复制——粘贴给 AI 即可",
  });
}

/* 模型合成指令：把本章尚未生成片段的镜逐镜交给视频模型，已有片段的镜自动跳过，
   最后合成。跳过判据与引擎一致（gen-video 的断点续跑本就不重复计费）。 */
function copyAnimateChapter(d) {
  const shots = (d.shots || []).filter((s) => s.kind !== "transition" && !s.omitted);
  const withClip = shots.filter((s) => s.clip).length;
  const missing = shots.length - withClip;
  const cid = `${d.project}/${d.id}`;
  const hasNarr = !!(d.assets || {}).narration;
  const chainBreaks = shots.filter((s) => s.chain_break)
                           .map((s) => `镜${s.id}（${s.chain_break}）`);
  const txt = [
    `请把本章逐镜动态化并合成成片 · 项目 ${d.project} / 章节 ${d.id}`
      + `（motion=${d.motion} · 正镜 ${shots.length} · 已有片段 ${withClip} · 缺 ${missing}）`,
    ``,
    `【本章实况与分派】`,
    missing === 0
      ? `· ${shots.length} 镜片段已全部就位 → 不必再调用视频模型，直接跳到第 ④ 步合成。`
      : (withClip > 0
          ? `· 已有 ${withClip} 镜、缺 ${missing} 镜 → 只补缺的那几镜：gen-video 会跳过已有片段的镜，`
            + `不重复计费。`
          : `· 本章还没有任何片段 → 全部 ${shots.length} 镜都要生成。`),
    // 链态如实描述：`frame_chain` 与逐镜 `chain_break` 都由引擎下发（判据见
    // pipeline/framechain），网页不自行推导——自行推导会让文案与实发对不上
    d.frame_chain
      ? `· 逐镜一次调用、一镜一个片段；本章开启**章级首尾帧衔接**——下一镜的分镜图作本镜`
        + `末帧，接缝两端落在同一个画面上。末帧是输入图、不计费，衔接不额外花钱。`
      : (d.motion === "native"
          ? `· 逐镜一次调用、一镜一个片段；缺省档=**全能参考**——分镜图+简笔板（在盘即附）`
            + `+设定图全作参考图完整生成一镜，不发首/末帧，合成时镜间直接拼接`
            + `（要过渡特效自己插转场镜）。要衔接：章节顶层 frame_chain: true 全章开，`
            + `或某镜 shots[].frame_chain: true 只焊那一处（该两镜退回首帧任务、附不了参考图）。`
          : `· 逐镜一次调用、一镜一个片段；本章不做首尾帧衔接（motion=${d.motion} 下不适用），`
            + `分镜图+简笔板+设定图作参考图随发，镜与镜之间直接拼接。`),
    d.frame_chain && chainBreaks.length
      ? `· 其中 ${chainBreaks.length} 镜不发末帧：${chainBreaks.join("、")}`
        + `——都是正常结果（转场是设计好的切、末镜没有下一镜），不用去补。`
      : null,
    ``,
    `【逐门执行·每道花钱门前等用户确认】`,
    `① 秒级规划核对：cd engine && python3 -m kinema sketch list --chapter ${cid}`
      + ` ——没有 beats 也没有板的镜，先把 video_prompt 写成有先后次序的分段动作。`,
    d.motion === "dubbed" ? `①′ 对口型需配音在盘：先跑 tts（已跑过可跳）。` : null,
    d.motion === "native" && d.native_voiceover
      ? `①′ 混烧章：先跑 tts（只合成旁白镜，对白由模型发声；已跑过可跳）。` : null,
    d.motion === "native" && !d.native_voiceover
      ? `①′ 片段自带模型原生人声，默认不叠我们的 TTS。要固定音色旁白 + 原生环境音，`
        + `章节写 native_voiceover: true 后先跑 tts`
        + `${hasNarr ? "——本章盘上已有配音轨，未开混烧它不会进成片。" : "。"}` : null,
    `② 审阅与报价：python3 -m kinema gen-video --chapter ${cid} --dry-run`
      + ` ——逐镜提示词与总价报给用户，等明确同意再真发（按秒计费）。`,
    `③ 确认后真发：python3 -m kinema gen-video --chapter ${cid}`
      + `（建议加 --approved-only 只烧分镜图已过审的镜；补单镜用 --only N；`
      + `换模型用 --video-provider，如 seedance-2.5 / minimax-h3`
      + `${d.frame_chain ? "；这一次不要衔接加 --no-chain" : ""}）`,
    `④ 合成：python3 -m kinema assemble --chapter ${cid}`
      + (d.motion === "native" && hasNarr && !d.native_voiceover
         ? "（要把已有配音烧进去先开 native_voiceover）" : ""),
    ``,
    `【不想调用视频模型】改走「图片合成」：assemble --chapter ${cid} --motion a，零视频成本。`,
  ].filter(Boolean).join("\n");
  return openDirectiveDialog({
    title: "模型合成指令", code: "CHAPTER · ANIMATE ALL",
    ask: "在此写本次动态化要求",
    meta: `项目 ${d.project} / 章节 ${d.id} · motion=${d.motion}${d.native_voiceover ? "·混烧" : ""}`
      + `${(d.video_provider || {}).alias ? " · video_provider=" + d.video_provider.alias : ""}`
      + ` · 待生成 ${missing} 镜 · 已有 ${withClip} 镜（自动跳过）`,
    directive: txt,
    hint: "例：只补第 3、7 镜；或：换成 seedance-2.5 生成",
    note: "留空即按上面的步骤执行。视频按秒计费，真发前恒有一次 dry-run 报价等你确认。",
    done: "模型合成指令已复制——粘贴给 AI 即可",
  });
}

function finalCard(d) {
  // 区块头（FC · 成片 · FINAL CUT）由章节页统一渲染，卡内不重复小标题
  const card = h("div", { class: "card side-card" });
  const usesVideo = !!d.uses_video;   // 判据服务端下发（Project.uses_seedance）
  const shotsN = (d.shots || []).filter((s) => s.kind !== "transition" && !s.omitted);
  const missingClip = shotsN.length - shotsN.filter((s) => s.clip).length;
  // 两个并列出口，对应「要不要调用视频模型」这一个决策——出的都是 output/ 正式成片
  const stillsBtn = h("button", { class: "act-btn",
    dataset: { tip: "⧉ 图片合成指令\n用各镜分镜图直接合成正式成片：ffmpeg 施加 Ken Burns 缩放位移，"
      + "运镜逐镜取 camera 字段、未写的按八种风格轮换，另配字幕与背景乐。"
      + "不调用视频模型、零视频成本；--motion a 运行时覆盖不落盘，之后仍可再动态化。" },
    onclick: () => copyAssembleStills(d) }, "⧉ 图片合成指令");
  const animateBtn = usesVideo && h("button", { class: "act-btn",
    dataset: { tip: "⧉ 模型合成指令\n把尚未生成片段的镜逐镜交给视频模型（Seedance / MiniMax H3 等），"
      + "已有片段的镜自动跳过不重复计费；最后合成成片。"
      + (d.frame_chain ? "本章开启章级首尾帧衔接（下一镜的图作末帧）、遇转场断链。"
                       : "缺省全能参考：分镜图+简笔板+设定图作参考、一镜一片、镜间直拼。")
      + "按秒计费，真发前恒有一次 dry-run 报价等你确认。" },
    onclick: () => copyAnimateChapter(d) },
    missingClip > 0 && missingClip < shotsN.length
      ? `⧉ 模型合成指令 · 补 ${missingClip} 镜` : "⧉ 模型合成指令");
  // 两个出口在两条分支里各挂一次（尚未合成→空态块居中排；已有成片→锚点行右端），
  // 同一份节点不能两处 append——DOM 节点会被搬走，故各分支就地成行
  if (!d.outputs.length) {
    card.append(h("div", { class: "empty", style: "padding:22px 16px" },
      usesVideo
        ? `尚未合成成片——可用分镜图直接出片，或先把 ${missingClip} 镜交给视频模型再合成`
        : "尚未合成成片——可用分镜图直接出片",
      h("div", { class: "shot-ops fc-outlets" }, stillsBtn, animateBtn || null)));
    return card;
  }
  const player = h("video", { controls: "", playsinline: "", preload: "metadata" });
  // 画布：随当前比例自适应（data-aspect 驱动 CSS）；全屏走播放器原生控件底部工具栏，不另置按钮
  const playerWrap = h("div", { class: "final-player" }, player);
  const meta = h("div", { class: "out-meta" });
  const tabs = h("div", { class: "aspect-tabs" });
  // 「历史」钉在头行右端，与比例页签同款小 pill——它是这块画面的档案入口，
  // 和「看哪个比例」是同一层级的选择；挂在文件信息行里会被当成一条元数据
  const histSlot = h("div", { class: "fc-head-right" });
  const head = h("div", { class: "fc-head" }, tabs, histSlot);
  const select = (o, btn) => {
    player.src = o.video; player.poster = o.poster || "";
    playerWrap.dataset.aspect = (o.aspect || "").replace(":", "x");
    meta.innerHTML = "";
    meta.append(h("span", null, o.name), h("span", null, fmtSize(o.size)),
      h("span", null, fmtDate(o.mtime)));
    if (o.watermarked) meta.append(h("span", { class: "wm-tag" }, "水印版"));   // 原生 append(null) 会渲染 "null"
    // 成片版本谱系：只挂在原片页签上——水印版是从某一版成片派生的交付物，
    // 它自己没有谱系，回滚后也要重打，挂上去等于宣称水印版能单独回滚
    histSlot.innerHTML = "";
    if (!o.watermarked) {
      const ov = ((d.output_versions || {})[o.aspect] || []);
      if (ov.length) histSlot.append(h("button", { class: "aspect-tab fc-hist",
        dataset: { tip: `成片版本谱系 · ${ov.length} 个历史版本\n历次合成的成片逐版可播；`
          + "回滚即把选中那版拷回标准输出路径，当前版自动归档。水印版需回滚后重打。" },
        onclick: () => openOutputVPanel(d, o.aspect) }, "历史"));
    }
    tabs.querySelectorAll(".aspect-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
  };
  const tabEls = [];
  // 只有一个选项的选择器不是选择器：单 output 页签不上屏（按钮仍建，选中态
  // 与多 output 走同一条 select 路径），头行只剩右端的「历史」
  const multi = d.outputs.length > 1;
  d.outputs.forEach((o) => {
    const label = o.watermarked ? `${o.aspect || ""} 水印版`.trim() : (o.aspect || o.name);
    const btn = h("button", { class: "aspect-tab" + (o.watermarked ? " wm" : ""),
      onclick: () => select(o, btn) }, label);
    if (multi) tabs.append(btn);
    tabEls.push([o, btn]);
  });
  // 头行恒渲染：历史入口挂在头行右端——按 output 数量决定渲不渲整行，会把入口一起藏掉
  card.append(head, playerWrap, meta);

  // 默认展示水印版（防搬运成片=交付版），无水印则第一个
  const defOut = tabEls.find(([o]) => o.watermarked) || tabEls[0];
  if (defOut) queueMicrotask(() => select(defOut[0], defOut[1]));

  // 末行只剩两个出口，居中——「视频锚定」（播放中写意见落锚到当前镜）整体下线：
  // 全站创建「无坐标意见」的入口只有那个输入框，锚点列表也只筛无坐标意见，
  // 删掉输入框列表就永远为空。留着＝一段永远不显示的 UI 加一堆不会执行的代码。
  // 灯箱里的打点/框选意见带 x/y 坐标，走的是另一条路，不受影响。
  card.append(h("div", { class: "shot-ops fc-outlets" }, stillsBtn, animateBtn || null));
  return card;
}

/* 全片样片（草稿两段式）：Ken Burns animatic + 章节级节奏审两键表态 */
function animaticCard(d) {
  const a = d.animatic;
  const st = REVIEW[a.state] || { zh: a.state, cls: "" };
  const src = Object.values(a.files || {})[0];
  const card = h("div", { class: "card side-card" },
    h("span", { class: "k" }, "全片样片 · ANIMATIC",
      h("span", { class: "sub" }, (a.at || "").slice(0, 16).replace("T", " "))),
    h("div", { class: "chips" },
      chip(`节奏审 · ${st.zh}`, st.cls),
      titledChip("Ken Burns 草稿", "cyan", "零视频成本的全片节奏审样片（草稿两段式）")));
  if (src) {
    card.append(h("div", { class: "final-player" },
      h("video", { src, controls: "", playsinline: "", preload: "metadata" })));
  }
  if (a.note) card.append(h("div", { class: "shot-cap" }, "意见 · ", a.note));
  const act = async (state, note) => {
    try {
      await post("/api/review", { project: d.project, chapter: d.id,
        stage: "animatic", state, note });
      toast(state === "done" ? "节奏审通过 · 正式渲染只烧已批准的镜" : "样片已打回重做");
      refreshAfterWrite(d);
    } catch (err) { toast(err.message, true); }
  };
  if (a.state === "done") {
    card.append(h("div", { class: "shot-cap" },
      "已通过 · 正式渲染：gen-video --approved-only（只烧已批准的镜）"));
  } else {
    const noteBox = h("div", { class: "retake-box", hidden: "" });
    const openNote = () => {
      noteBox.hidden = false; noteBox.innerHTML = "";
      const input = h("input", { class: "cmt-input", type: "text",
        placeholder: "节奏问题（哪几镜拖/顺序/时长）…" });
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
    card.append(h("div", { class: "retake-box" },
      h("button", { class: "act-btn ok big", onclick: () => act("done") }, "✓ 节奏审通过"),
      h("button", { class: "act-btn no big", onclick: openNote }, "↻ 重做")), noteBox);
  }
  return card;
}

/* 宫格候选：点选即定稿上画布（原画布自动归档，可反悔换选） */
function candGrid(d, s) {
  const cands = s.image_candidates || [];
  if (!cands.length) return null;
  const doPick = async (no) => {
    try {
      const r = await post("/api/pick", { project: d.project, chapter: d.id,
        shot: s.id, no });
      toast(`镜 ${s.id} 已定稿候选 #${no}` +
        (r.archived ? `（原版已归档 ${r.archived}）` : ""));
      refreshAfterWrite(d);
    } catch (err) { toast(err.message, true); }
  };
  const grid = h("div", { class: "cand-grid" }, cands.map((c) => {
    const picked = s.image_picked === c.no;
    return h("div", { class: "cand" + (picked ? " picked" : ""),
        title: picked ? `当前定稿 #${c.no}` : `点选候选 #${c.no} 定稿（锁定）`,
        onclick: (e) => { e.stopPropagation(); if (!picked) doPick(c.no); } },
      h("img", { src: c.url, loading: "lazy", alt: `候选 ${c.no}` }),
      h("b", null, String(c.no)),
      picked ? h("span", { class: "cand-ok" }, "✓ 定稿") : null);
  }));
  if (!s.image) {   // 未定稿：宫格是主视图
    return h("div", { class: "cand-wrap" },
      h("span", { class: "k" }, `候选宫格 · ${cands.length} 选 1 · 点选即定稿锁定`), grid);
  }
  return h("details", { class: "prompt-fold" },   // 已定稿：折叠可换选
    h("summary", null, `候选宫格 · ${cands.length} 张（点选换版，原版自动归档）`), grid);
}

function scriptCard(sc) {
  return h("div", { class: "card side-card" },
    h("span", { class: "k" }, "文案 · SCRIPT"),
    h("div", { class: "script-block" },
      sc.hook && scriptItem("hook", "HOOK 钩子", sc.hook),
      sc.body && scriptItem("body", "BODY 正文", sc.body),
      sc.cta && scriptItem("cta", "CTA 行动", sc.cta)));
}
const scriptItem = (cls, label, text) =>
  h("div", { class: `script-item ${cls}` }, h("span", { class: "k" }, label), h("p", null, text));

function voicesCard(d) {
  const rows = Object.entries(d.voices).map(([name, alias]) => {
    const sample = d.voice_samples?.[name];
    return h("div", { class: "voice-line" },
      h("b", null, name), h("span", { class: "arrow" }, "→"), chip(`♪ ${alias}`, "amber"),
      sample ? audioPill(sample) : h("code", null, d.voice_types?.[name] || ""));
  });
  return h("div", { class: "card side-card" },
    h("span", { class: "k" }, "角色音色 · VOICE CAST"), ...rows);
}

function consistencyCard(d) {
  const rows = [];
  if (d.style?.seed != null) rows.push(kvRow("SEED", h("code", { class: "val" }, String(d.style.seed))));
  if (d.style?.palette) rows.push(kvRow("色板", d.style.palette));
  if (d.scene) rows.push(kvRow("场景", d.scene));
  if (d.style?.character_block) rows.push(kvRow("角色块", d.style.character_block));
  if (!rows.length) return h("div");
  return h("div", { class: "card side-card" },
    h("span", { class: "k" }, "一致性锚点 · CONSISTENCY"), h("div", { class: "kv" }, rows));
}
const kvRow = (k2, v) => h("div", { class: "kv-row" }, h("span", { class: "k" }, k2),
  h("div", null, v));

/* 语音时间轴查看器：timestamps.json → 系统制式弹层（镜号/起止/时长/台词） */
async function openTimestamps(url) {
  let rows = [];
  try { rows = await (await fetch(url)).json(); }
  catch { return toast("时间戳文件读取失败", true); }
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const tbl = h("table", { class: "sb-table" },
    h("thead", null, h("tr", null,
      ["镜号", "开始", "结束", "时长", "台词"].map((x) => h("th", null, x)))),
    h("tbody", null, rows.map((r) => h("tr", null,
      h("td", { class: "mono" }, String(r.shot_id ?? "—")),
      h("td", { class: "mono" }, fmtDur(r.start || 0)),
      h("td", { class: "mono" }, fmtDur(r.end || 0)),
      h("td", { class: "mono" }, `${((r.end || 0) - (r.start || 0)).toFixed(2)}s`),
      h("td", null, r.text || "—")))));
  const overlay = h("div", { class: "rf-overlay",
      onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "ts-wrap" },
      h("div", { class: "rf-head" },
        h("span", { class: "k" }, "语音时间轴 · TIMESTAMPS",
          h("span", { class: "chip", style: "margin-left:10px" }, `${rows.length} 段`)),
        h("button", { class: "rf-x", onclick: close }, "✕")),
      h("div", { class: "ts-scroll" }, tbl),
      h("div", { class: "rf-foot" },
        h("span", { class: "rf-cost" }, "全片配音轨的逐句起止 · 合成与对轨的时间真源"),
        h("button", { class: "lb-refine-go", onclick: async () => {
          try {
            await navigator.clipboard.writeText(JSON.stringify(rows, null, 2));
            toast("JSON 已复制到剪贴板");
          } catch { toast("复制失败：浏览器未授权剪贴板", true); }
        } }, "复制 JSON"))));
  document.addEventListener("keydown", esc);
  document.body.append(overlay);
}

/* 字幕查看器：ASS → 事件表（起止/样式/文本，剥离覆写标签），同制式弹层 */
async function openSubtitles(url, label) {
  let raw = "";
  try { raw = await (await fetch(url)).text(); }
  catch { return toast("字幕文件读取失败", true); }
  const rows = [];
  raw.split(/\r?\n/).forEach((ln) => {
    if (!ln.startsWith("Dialogue:")) return;
    const parts = ln.slice(9).split(",");
    if (parts.length < 10) return;
    rows.push({ start: parts[1].trim(), end: parts[2].trim(),
      style: parts[3].trim(),
      text: parts.slice(9).join(",").replace(/\{[^}]*\}/g, "")
        .replace(/\\N/g, " ⏎ ").trim() });
  });
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const body = rows.length
    ? h("div", { class: "ts-scroll" }, h("table", { class: "sb-table" },
        h("thead", null, h("tr", null,
          ["#", "开始", "结束", "样式", "文本"].map((x) => h("th", null, x)))),
        h("tbody", null, rows.map((r, i) => h("tr", null,
          h("td", { class: "mono" }, String(i + 1)),
          h("td", { class: "mono" }, r.start),
          h("td", { class: "mono" }, r.end),
          h("td", { class: "mono" }, r.style),
          h("td", null, r.text || "—"))))))
    : h("div", { class: "ts-scroll" },
        h("pre", { class: "ts-raw" }, raw.slice(0, 8000)));
  const overlay = h("div", { class: "rf-overlay",
      onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "ts-wrap" },
      h("div", { class: "rf-head" },
        h("span", { class: "k" }, `字幕 · SUBTITLES`,
          label && h("span", { class: "chip blue", style: "margin-left:10px" }, label),
          rows.length ? h("span", { class: "chip", style: "margin-left:6px" },
            `${rows.length} 条`) : null),
        h("button", { class: "rf-x", onclick: close }, "✕")),
      body,
      h("div", { class: "rf-foot" },
        h("span", { class: "rf-cost" }, "合成烧录所用的 ASS 事件轨 · 音字一致真源"),
        h("button", { class: "lb-refine-go", onclick: async () => {
          try {
            await navigator.clipboard.writeText(raw);
            toast("ASS 原文已复制到剪贴板");
          } catch { toast("复制失败：浏览器未授权剪贴板", true); }
        } }, "复制 ASS"))));
  document.addEventListener("keydown", esc);
  document.body.append(overlay);
}

function assetsCard(d) {
  const a = d.assets || {};
  const items = [];
  if (a.narration) items.push(audioPill(a.narration, "全片配音轨"));
  if (a.score) items.push(audioPill(a.score, "音频剧本整轨"));
  if (a.bgm) items.push(audioPill(a.bgm, "背景音乐 BGM"));
  const chipsRow = h("div", { class: "chips" });
  (a.subtitles || []).forEach((s2) =>
    chipsRow.append(h("button", { class: "chip-link",
      dataset: { tip: "字幕事件轨\n合成烧录所用 ASS 的结构化视图（起止/样式/文本，"
        + "剥离覆写标签）——音字一致真源，可复制原文。" },
      onclick: () => openSubtitles(s2.url, s2.aspect || s2.name) },
      chip(`字幕 ${s2.aspect || s2.name}`, "blue"))));
  if (a.timestamps) chipsRow.append(h("button", { class: "chip-link",
    dataset: { tip: "语音时间轴\n全片配音轨逐句起止的结构化视图——合成与对轨的"
      + "时间真源，可复制 JSON。" },
    onclick: () => openTimestamps(a.timestamps) }, chip("语音时间轴", "amber")));
  if (!items.length && !chipsRow.children.length) return h("div");
  const card = h("div", { class: "card side-card" },
    h("span", { class: "k" }, "音频 · 资产 · AUDIO RACK"), ...items);
  if (chipsRow.children.length) card.append(chipsRow);
  return card;
}

function costCard(cost) {
  const cur = CUR[cost.currency || "CNY"] || "";
  const rows = Object.entries(cost).filter(([k2, v]) => k2 !== "currency" && typeof v === "number");
  const zh = { image: "图像", video: "视频", tts: "配音", music: "音乐", score: "音频剧本" };
  return h("div", { class: "card side-card" },
    h("span", { class: "k" }, "云 API 成本 · COST"),
    h("div", { class: "cost-rows" },
      rows.map(([k2, v]) => h("div", { class: "cost-row" },
        h("span", { class: "lbl" }, zh[k2] || k2), h("span", { class: "dots" }),
        h("span", { class: "val" }, `${cur}${v.toFixed(2)}`))),
      h("div", { class: "cost-row total" },
        h("span", { class: "lbl" }, "合计"), h("span", { class: "dots" }),
        h("span", { class: "val" }, costTotal(cost)))));
}

/* 片段版本号徽章：`versions.clip` 是**归档数**，当前版 = 归档数 + 1（与版本面板同口径，
   算错一位会让人按错的版本号去回滚）。零归档时不显示——只生成过一次没有"版本"可言。 */
function clipVerChip(s) {
  const n = (s.versions || {}).clip || 0;
  return n ? `v${String(n + 1).padStart(3, "0")}` : null;
}

/* 放映窗里的版本谱系入口：有归档才给按钮，没有就说明白「只此一版」——
   避免渲染一个点击无效的控件。 */
function clipVerEntry(d, s) {
  const hist = ((s.version_history || {}).clip || []).length;
  if (!hist) return "只此一版（重生成 / 回滚时自动归档）";
  // 按钮文案恒单行；版本数与可回滚说明进 tip——窄列里长文案会折行
  return h("button", { class: "ghost-btn",
    dataset: { tip: `版本谱系 · ${hist} 个历史版本\n历次生成的片段逐版可播；`
      + "选中某一版回滚即把它拷回当前版，原当前版自动归档——来回切换不丢任何一版。" },
    onclick: () => { closeCinema(); openVPanel(d, s, "clip"); } },
    "⧉ 版本谱系");
}

/* 转场镜缩略瓦片：黑场光带小卡——时间线/列表里代替空图占位，一眼可辨「这是过场」 */
function transitionThumb(s, cls) {
  const t = s.transition || {};
  return h("div", { class: "tr-thumb" + (t.type === "fade_white" ? " white" : "")
                          + (cls ? " " + cls : "") },
    h("span", { class: "tr-thumb-ico" }, "⧗"),
    h("span", { class: "tr-thumb-txt" }, t.text || TRANSITION_ZH[t.type] || "转场"));
}

/* 转场镜卡：时间线上的字卡/素材转场——可插拔、零 API 成本 */
function transitionCard(d, s, i) {
  const t = s.transition || {};
  const label = TRANSITION_ZH[t.type] || "转场";
  // 副标题按 family 说真话：xfade 族 edge=0 不动相邻镜（说「相邻镜自动淡入淡出」
  // 对全部冻结帧型都是错的），字卡族才有边缘淡化
  const isXfade = ((trCatalog().find((x) => x.key === t.type) || {}).family) === "xfade";
  const mech = (isXfade ? "前后帧直接叠化 · 不动相邻镜" : "相邻镜自动淡入淡出")
    // 自动软切必须标明来历：用户没插过它，不说清只会被当成 bug
    + (t.auto ? " · 自动补于孤岛接缝（相邻镜走参考模式，焊不上首尾帧）" : "");
  const delNote = t.auto
    ? (d.frame_chain
        ? "这是引擎为「焊不上的接缝」自动补的软切——本章开着首尾帧衔接，相邻镜只要还走"
          + "参考孤岛/V2V，下次生成会再补回来。要永久换掉，请在同一处手写一个自己的转场。"
        : "这是历史遗留的自动软切——本章未开首尾帧衔接（缺省档镜间直拼），移除后不会被补回来。")
    : (isXfade ? "仅移除过渡段，相邻镜不受影响。"
               : "相邻镜的边缘淡化随之撤销，普通镜不受影响。");
  return h("div", { class: "card shot-card transition-card" + (t.type === "fade_white" ? " white" : ""),
                    id: `shot-${s.id}`,
                    style: `animation-delay:${Math.min(i * 30, 300)}ms` },
    h("div", { class: "tr-body" },
      h("span", { class: "tr-icon" }, "⧗"),
      h("div", { class: "tr-txt" },
        h("b", null, t.text || (t.type === "seamless" ? "（无缝柔切）" : "（纯色停顿）")),
        h("small", null, `${label} · ${s.dur || 0}s · ${mech}`)),
      h("button", { class: "tr-del",
          dataset: { tip: `✕ 移除此转场镜\n${delNote}` },
          onclick: async (e) => { e.stopPropagation();
            if (!(await uiConfirm(`移除转场镜 ${s.id}？${delNote}`,
                                  { danger: true, title: "移除转场" }))) return;
            // 删除全程暂停轮询——避免异步间隙里 3s 轮询整页重绘把页面跳走
            state.live = false;
            try {
              await post("/api/transition/remove", { project: d.project, chapter: d.id, shot: s.id });
              // 原地删节点、同步内存数据，页面不刷新不跳顶（接口删成功即可）；
              // STATE.chapSig 对齐最新服务端签名，恢复轮询后不因此次删除触发整页重绘
              const node = document.getElementById(`shot-${s.id}`);
              const arr = d.shots || [];
              const idx = arr.findIndex((x) => String(x.id) === String(s.id));
              // 把「＋转场」槽位补回原位——**删卡必须补槽**：槽位的渲染判据是
              // 「下一镜不是转场」（见分镜列渲染），有转场时前一镜压根没生成过槽位，
              // 只删卡不补槽那一格就永久空着，再也点不出转场；而轮询又被上面的
              // chapSig 对齐按住不重绘，空缺会一直留到用户手动刷新。
              const prev = idx > 0 ? arr[idx - 1] : null;
              if (node && prev && prev.kind !== "transition") {
                node.replaceWith(transitionSlot(d, prev, idx === arr.length - 1));
              } else {
                node?.remove();
              }
              if (idx >= 0) arr.splice(idx, 1);
              try {
                STATE.chapSig = chapterSignature(await api(
                  `/api/chapter?project=${encodeURIComponent(d.project)}`
                  + `&id=${encodeURIComponent(d.id)}`));
              } catch { STATE.chapSig = ""; }   // 取签名失败则退回下次轮询自然重绘
              toast(`转场镜 ${s.id} 已移除`);
            } catch (err) { toast(err.message, true); }
            finally { state.live = true; }
          } }, "✕")));
}

/* 「＋转场」插槽：悬停出现在每个镜卡下方，一键插入「一天后」式字卡 */
/* 转场目录访问器（overview 下发的单一真源，前端零硬编码类型/方向/音效） */
const trCatalog = () => (state.overview && state.overview.transitions_catalog) || [];
const trSounds = () => (state.overview && state.overview.transition_sounds) || [];

/* 转场类型选择弹层：类型网格 → 条件方向/主色 chip → 文案 → 音效，数据驱动自转场目录。
   resolve 一个 {type,text,direction,color,sound} 提交给 /api/transition/add，或 null（取消）。
   目录未下发时退回纯文案输入，不阻断。 */
function transitionDialog(afterId, tail = false) {
  // 抬头随位置措辞：末镜之后插的那一条是「片尾帧」，用途是收尾而不是两镜之间过渡
  const title = tail ? `片尾帧（接在末镜 ${afterId} 之后）` : `镜 ${afterId} 后插入转场`;
  const cat = trCatalog();
  if (!cat.length) {
    return uiPrompt("转场字卡文案（留空=极简黑场呼吸）",
      { placeholder: "几天后", title })
      .then((text) => (text === null ? null : { text: text.trim() }));
  }
  return new Promise((resolve) => {
    let result = null;
    const done = (val) => { result = val; close(); };
    const sel = { type: cat[0].key, direction: "", color: "", dur: null };
    const meta = () => cat.find((t) => t.key === sel.type) || cat[0];

    const typeBtns = {};
    const typeGrid = h("div", { class: "tr-types" }, ...cat.map((t) => {
      const b = h("button", { class: "tr-type", type: "button", dataset: { tip: t.desc },
        onclick: () => { sel.type = t.key; sel.direction = ""; sel.color = ""; syncType(); } },
        t.label);
      typeBtns[t.key] = b;
      return b;
    }));
    const desc = h("p", { class: "tr-desc" });
    const dirRow = h("div", { class: "tr-opt-row", hidden: true });
    const colRow = h("div", { class: "tr-opt-row", hidden: true });
    const durRow = h("div", { class: "tr-opt-row", hidden: true });
    const textInp = h("input", { class: "cmt-input", type: "text" });
    // 字卡文案区整体（label + 输入框）——非字卡型（一次性无停顿的 xfade / 极简黑场）整段隐藏
    const textSec = h("div", { class: "tr-text-sec" },
      h("div", { class: "tr-sec-k" }, "字卡文案"), textInp);
    const soundSel = uiSelect(trSounds(), {});

    // 方向/主色/柔度 chip 行构造：auto=true 时首项「自动」=不指定（后端用该型缺省）；
    // 柔度行无「自动」——三档本身就是显式选择，默认高亮该型缺省时长
    const buildChips = (row, list, key, title, { auto = true } = {}) => {
      const chips = [];
      const paint = () => chips.forEach((c) =>
        c.el.classList.toggle("on", (sel[key] || "") === c.val));
      row.textContent = "";
      row.append(h("span", { class: "tr-opt-k" }, title));
      (auto ? [{ value: "", label: "自动" }, ...list] : list).forEach((o) => {
        const el = h("button", { class: "tr-chip", type: "button",
          onclick: () => { sel[key] = o.value; paint(); } }, o.label);
        chips.push({ el, val: o.value });
        row.append(el);
      });
      paint();
    };
    const syncType = () => {
      const m = meta();
      Object.entries(typeBtns).forEach(([k, b]) => b.classList.toggle("on", k === sel.type));
      desc.textContent = m.desc || "";
      m.directions.length
        ? (buildChips(dirRow, m.directions, "direction", "方向"), (dirRow.hidden = false))
        : (dirRow.hidden = true);
      m.colors.length
        ? (buildChips(colRow, m.colors, "color", "主色"), (colRow.hidden = false))
        : (colRow.hidden = true);
      if (m.durations.length) {
        sel.dur = m.dur;
        buildChips(durRow, m.durations, "dur", "柔度", { auto: false });
        durRow.hidden = false;
      } else { sel.dur = null; durRow.hidden = true; }
      // 仅能加字的类型（字卡型有停顿显字）显示文案框；其余（一次性无停顿）整段隐藏并清空
      textSec.hidden = !m.text_ok;
      if (!m.text_ok) textInp.value = "";
      textInp.placeholder = "字卡文字，如「几天后」";
      soundSel.value = m.sound;                    // 该型缺省音效（可改）
    };
    const submit = () => done({
      type: sel.type, text: meta().text_ok ? textInp.value.trim() : "",
      direction: sel.direction || undefined,
      color: sel.color || undefined,
      sound: soundSel.value || undefined,
      dur: sel.dur || undefined,
    });
    const close = openShell({ card: "tr-dlg",
      onClose: () => resolve(result),
      keys: (e) => {
        if (e.key === "Enter" && e.target === textInp) {
          e.stopPropagation(); e.preventDefault(); submit();
        }
      },
      build: () => [
        h("span", { class: "k" }, title),
        h("div", { class: "tr-sec-k" }, "转场类型"),
        typeGrid, desc, dirRow, colRow, durRow,
        textSec,
        h("div", { class: "tr-sound-row" },
          h("span", { class: "tr-opt-k" }, "音效"), soundSel),
        h("div", { class: "dlg-acts" },
          h("button", { class: "dlg-btn", onclick: () => done(null) }, "取消"),
          h("button", { class: "dlg-btn primary", onclick: submit }, "插入转场"))] });
    syncType();
    setTimeout(() => { if (!textSec.hidden) textInp.focus(); }, 30);
  });
}

/* 「＋转场」插槽。末镜后面那一个叫「＋尾帧」——**同一个功能、同一条接口**，
   只是那个位置的用途不一样：后面没有下一镜可衔接，插在那儿是给全片收个尾
   （黑场字卡打一行字最常用）。冻结帧族（无缝/翻页/圆开合/推移/柔焦/扫描）需要
   前后都有普通镜，插在末位会退化成字卡并告警——故末位提示直说「用字卡型」。 */
function transitionSlot(d, s, tail = false) {
  return h("div", { class: "tr-slot" },
    h("button", { class: "tr-add",
        // 措辞纪律：这是一个**插入入口**，不是本章的状态。槽位一镜一个，
        // 「默认无缝转场」之类的措辞与逐镜插入语义不符。缺省是**没有转场**：
        // 不点就是连贯硬切。
        dataset: { tip: tail
          ? "＋ 片尾帧\n在全片最后接一段收尾：黑场/白闪字卡可打一行字（「三年后」「完」）。"
            + "缺省没有——只有在这里亲自插入才有。冻结帧型（无缝/翻页/圆开合等）需要"
            + "前后都有镜，插在末位会退化为字卡，故这里选字卡型最稳。纯本地渲染零 API 成本。"
          : "＋ 插入转场\n本章缺省没有转场——只有在这里亲自插入，这一处才有。"
            + "可选无缝柔切/黑场/翻页/圆开合/推移/柔焦/轮廓扫描等，字卡型可加文案。"
            + "纯本地渲染零 API 成本，可随时移除。" },
        onclick: async () => {
          const params = await transitionDialog(s.id, tail);
          if (!params) return;
          // 与移除侧同一条反馈纪律：post 失败必须弹红条说明（典型：Studio 重启后
          // CSRF 失效，GET 轮询照常、页面看着是活的，但所有 POST 全废）——
          // 静默失败必须显式报出；刷新走章节视图静默重渲，不整页 reload
          try {
            await post("/api/transition/add",
              { project: d.project, chapter: d.id, after: s.id, ...params });
            toast(tail ? "已插入片尾帧" : "已插入转场");
            await viewChapter($("#view"), d.project, d.id, { silent: true });
          } catch (err) {
            toast(`转场插入失败：${err.message || err}`, true);
          }
        } }, tail ? "＋ 尾帧" : "＋ 转场"));
}

/* 提示词行（分镜卡折叠区与镜头表展开行共用）：中文主行 + 英文次行小字 */
const promptRow = (tag, zh, en, cls) =>
  zh && h("div", { class: "prompt-row" + (cls ? " " + cls : "") },
    h("span", { class: "tag" }, tag),
    h("p", null, zh, en && h("span", { class: "p-en" }, en)));

/* ---- 运动预演两台的章节级门 ---------------------------------------------

   3D 导演台与简笔分镜都只服务 gen-video：末帧、V2V 参考视频、分段时间轴与附板
   全部落在视频请求里，而 kenburns 这一档根本不发 gen-video（compose 直接走
   `pipeline/kenburns.py` 的 ffmpeg zoompan 出片）。两台在这一档配置了也不生效，
   其中「▦ 生成简笔板」还按 image provider 计费——挂在那里等于请人花钱买不参与
   成片的产物。

   判据**恒取服务端下发的 `uses_video`**（= `Project.uses_seedance`，scanner 随章节
   视图下发），与逐镜生视频入口同一个判据。两条不许走的路：

   1. 按 skill / 画风推。motion 是**章节级**字段，同一项目里 ch01 走 native、
      ch02 走 kenburns 是常态（盘上就有），按项目类型判会逐章判错；而 kn-anime
      的 2D 档、kn-game 的过审档本来就推荐 kenburns，映射根本不成立。
   2. 在前端另建一张「哪些类型要视频」的表——那是第二真源，每加一个 skill 都得回来改。

   kenburns 下收成一条折叠条而**不是不渲染**：盘上真有排完 previz 又改回 kenburns
   的章节，产物还在（分镜卡的 ◈ 预演 / ▦ 简笔角标也还在），硬藏等于让它在网页上
   彻底不可达。条上给读数，点开就是原来那两张卡，一个字没改。 ---- */
const PVZ_OPEN = new Set();     // 折叠条展开态（`pid/cid`）：轮询整页重绘后保留

// 折叠条台标：两层取景框（＝两条预演路径）＋一道带箭头的动势线，与简笔台 SK_ICO
// 同一套线性语汇。刻意不把场记板与取景框两个字形硬叠——26px 里两个方框会糊成一块
const PVZ_ICO = '<svg viewBox="0 0 28 28">'
  + '<path d="M7.6 2.6h15a2.4 2.4 0 0 1 2.4 2.4v11.2"/>'
  + '<rect x="2.6" y="6.4" width="19" height="15" rx="2.2"/>'
  + '<path d="M6.4 17.6c1.9-4.7 5.4-6.6 10.1-5.7"/>'
  + '<path d="M13.3 10.1l3.2 1.5-1.3 3.1"/></svg>';

function previzDesks(d) {
  // 展开态与 uses_video 分支共用同一份构造，两处不许各写一套
  const desks = () => {
    const dzHead = secHeader("3D", "导演台", "DIRECTOR",
      (d.shots || []).filter((s) => s.previz).length || null);
    dzHead.id = "sec-stage";
    // 简笔分镜：与 3D 导演台并行的第二条运动预演路径——**逐镜互斥**
    // （引擎 sketchboard.active_guide 仲裁，前端只消费 guide_active 绝不自算）
    const skHead = secHeader("SK", "简笔分镜", "SKETCH BOARD",
      (d.sketch_stats || {}).boards || null);
    skHead.id = "sec-sketch";
    return [dzHead, directorCard(d), skHead, sketchCard(d)];
  };
  if (d.uses_video) return desks();

  const key = `${d.project}/${d.id}`;
  const pz = (d.shots || []).filter((s) => s.previz).length;
  const bd = (d.sketch_stats || {}).boards || 0;
  const mode = MOTION[d.motion] || MOTION.kenburns;
  // 区块头徽标给的是**已有产物数**而不是分镜数：这一档下「排了多少」才是要点
  const head = secHeader("PV", "运动预演", "MOTION PREVIZ", pz + bd || null);
  head.id = "sec-stage";

  const slot = h("div", { class: "pvz-slot" });
  const actTxt = h("b", null, "展开");
  const bar = h("button", { class: "card pvz-fold", type: "button",
      "aria-expanded": "false",
      dataset: { tip: "3D 导演台与简笔分镜只服务图生视频——末帧、参考视频与分段时间轴"
        + "都随 gen-video 请求发出，本集不调用视频模型，配置了也不进成片。\n"
        + "仍可展开排戏对照；把本集渲染模式改成 native / dubbed 后自动恢复常驻。\n"
        + "渲染模式不在网页里改：它是章节 JSON 顶层的 motion，交给 AI 改（ChapterPlan "
        + "白名单字段）或直接编辑该文件。" },
      onclick: () => setOpen(!PVZ_OPEN.has(key), true) },
    h("span", { class: "pvz-ico", html: PVZ_ICO }),
    h("div", { class: "pvz-txt" },
      h("b", null, "3D 导演台 · 简笔分镜"),
      h("em", null, `本集为 ${mode.key} · ${mode.name}（${mode.desc}）`,
        h("span", { class: "pvz-sep" }, "—"), "不调用视频模型")),
    // 有产物才出这枚读数：0/0 的空读数只是噪音，而有产物时它是「这里还有东西」的唯一线索
    (pz || bd) ? h("span", { class: "pvz-kept" },
      [pz ? `${pz} 镜已排预演` : null, bd ? `${bd} 张简笔板` : null]
        .filter(Boolean).join(" · ")) : null,
    h("span", { class: "pvz-act" }, actTxt, h("i", { class: "pvz-caret" }, "▾")));

  const setOpen = (on, animate = false) => {
    if (on) PVZ_OPEN.add(key); else PVZ_OPEN.delete(key);
    bar.classList.toggle("open", on);
    bar.setAttribute("aria-expanded", on ? "true" : "false");
    actTxt.textContent = on ? "收起" : "展开";
    slot.replaceChildren(...(on ? desks() : []));
    // 入场动画只给**点开**这一次：展开态下每次轮询重绘都重放一遍，页面会无故抖
    slot.classList.toggle("in", on && animate);
  };
  setOpen(PVZ_OPEN.has(key));
  return [head, bar, slot];
}

/* ---- 简笔分镜台：beats（指挥层写）→ 9 格素描板（引擎生）→
   Seedance 时间轴+参考图（gen-video 消费）。与 3D previz 逐镜互斥。 ---- */

/* 简笔板生成忙态：`pid/cid` → Map(任务 id → 该任务负责的镜号集合)。板条按它渲
   「生成中」格；页面刷新经 /api/jobs 的 meta.shots 恢复（任务是章节级一条，
   逐镜格全靠这份清单）。**按 job 分组**而不是一章一条：批量生成还没跑完时又从
   灯箱重生某镜是完全正常的操作，一章一条会把第二个任务整个忽略（忙态不记、
   也不重绘），那一镜就静默无反应直到下次刷新。 */
const SKGEN = new Map();

/* 「交给 Seedance」批量忙态：一条章节级任务点亮 meta.shots 里每一镜的分镜卡遮罩
   （GENJOBS 按镜 key 各挂一条 kind=clip），但**只起一个轮询**——按镜各起一个会对
   同一 job 打出 N 倍请求。收尾统一摘遮罩 + 缓存击穿 + 软刷新。 */
const CLIPJOBS = new Set();

function trackClipJob(pid, cid, jid, shots) {
  if (CLIPJOBS.has(jid)) return;       // 对账每次进视图都会调，任务级去重
  CLIPJOBS.add(jid);
  const ids = String(shots || "").split(",").map((x) => x.trim()).filter(Boolean);
  const keys = ids.map((id) => jobKey(pid, cid, id))
                  .filter((k) => !GENJOBS.has(k));   // 单镜已有别的任务在跑就不抢
  for (const k of keys) GENJOBS.set(k, { id: jid, kind: "clip" });
  softRefresh(pid, cid);               // 登记即重绘：忙态是渲染的一部分
  const done = (msg, bad) => {
    CLIPJOBS.delete(jid);
    for (const k of keys) {
      GENJOBS.delete(k);
      BUST.set(k, Date.now());
    }
    if (msg) toast(msg, bad);
    return softRefresh(pid, cid);
  };
  pollJob(jid, {
    onDone: () => done(`视频生成完成（${ids.length} 镜）——片段已入待审`),
    onFail: (j) => done(`视频生成失败：${(j.tail || "").slice(-160)}`, true),
  });
}

function trackSketchJob(pid, cid, jid, shots) {
  const key = `${pid}/${cid}`;
  const ids = String(shots || "").split(",").map((x) => x.trim()).filter(Boolean);
  const byJob = SKGEN.get(key) || new Map();
  if (byJob.has(jid)) return;          // 同一任务不重复登记（页面对账每次进视图都会调）
  byJob.set(jid, new Set(ids));
  SKGEN.set(key, byJob);
  // **登记即重绘**：忙态是渲染的一部分（板格子渲染时才读 SKGEN），只改内存 Map
  // 用户看不到任何反应——历史缺口正是灯箱「↻ 重新生成」关掉弹层后毫无动静，
  // 而选镜弹层那条路各自写了一遍 softRefresh。重绘收进本函数，调用方不必再记。
  softRefresh(pid, cid);
  const done = (msg, bad) => {
    const m = SKGEN.get(key);
    if (m) {
      m.delete(jid);
      if (!m.size) SKGEN.delete(key);
    }
    if (msg) toast(msg, bad);
    return softRefresh(pid, cid);
  };
  pollJob(jid, {
    onDone: () => done("简笔分镜板生成完成"),
    onFail: (j) => done(`简笔板生成失败：${(j.tail || "").slice(-160)}`, true),
  });
}

function sketchDirective(d) {
  const missing = (d.shots || []).filter((s) => s.kind !== "transition" && !s.omitted
    && !((s.sketch || {}).beats > 0)).map((s) => s.id);
  return [
    `请为项目 ${d.project} 章节 ${d.id} 写「简笔分镜板 beats」（先 Read kinema-sketchboard skill）：`,
    `- 文件：project/${d.project}/chapters/${d.id}.json`,
    `- 待写镜号：${missing.length ? missing.join("、") : "（全部已写，可按意见精修）"}`,
    '- 每镜在 shots[].sketch.beats 写 9 拍，每拍 {"t":"0-0.6s","action":"该秒段的具体动作（可见运动）",'
      + '"camera":"机位/运镜","framing":"构图","light":"光","sound":"声/情绪"}'
      + "（action 必填其余按需；t 缺省按 dur 均分）",
    "- 依据该镜 narration/action/video_prompt/end_state/light_shift 与原作细节拆解：拍与拍动作连续推进、"
      + "入拍即动、末拍收在 end_state 上，绝不发明剧情",
    `- 写完跑：cd engine && python3 -m kinema sketch gen --chapter ${d.project}/${d.id}`,
  ].join("\n");
}

function openSketchGenDialog(d) {
  const rows = (d.shots || []).filter((s) => s.kind !== "transition" && !s.omitted);
  const checks = new Map();
  const items = rows.map((s) => {
    const ck = uiCheck();
    const sk = s.sketch || {};
    const ready = sk.beats > 0;              // 拆拍就绪：自定义 beats 或自动拆拍
    const hasBoard = !!sk.sheet;
    ck.checked = ready && !hasBoard;         // 缺省勾「就绪且未出板」
    checks.set(s.id, ck);
    return h("label", { class: "skb-row" + (ready ? "" : " off") }, ck,
      h("b", null, `镜 ${s.id}`),
      h("span", null, !ready ? "缺运动设计"
        : (sk.auto ? `自动拆拍 ${sk.beats} 拍` : `${sk.beats} 拍`)),
      hasBoard ? chip("已出板", "green") : null,
      sk.warn ? h("i", { class: "skb-warn", dataset: { tip: `⚠ 秒段体检\n${sk.warn}` } }, "⚠") : null,
      sk.stale ? h("i", { class: "skb-warn",
        dataset: { tip: `⚠ 时长已变（${sk.stale.was}s→${sk.stale.now}s）——建议重生` } }, "⏱") : null);
  });
  const force = uiCheck();
  openShell({ card: "skb-dlg", build: (close) => [
    h("span", { class: "k" }, "▦ 生成简笔分镜板"),
    h("p", { class: "dlg-msg" },
      "每块板生成一张图（按图像生成计费，约等于一张分镜图）。运动描述完整的分镜"
      + "可直接生成（按运动设计自动拆拍）；需要精确控制每一拍时，"
      + "先用「⧉ 分镜板指令」交由 Claude Code 编写拍序列。"),
    h("div", { class: "skb-list" }, ...items),
    h("div", { class: "skb-tools" },
      h("button", { class: "act-btn",
        onclick: () => rows.forEach((s) =>
          (checks.get(s.id).checked = (s.sketch || {}).beats > 0)) }, "全选可生成"),
      h("button", { class: "act-btn",
        onclick: () => rows.forEach((s) => (checks.get(s.id).checked = false)) }, "全不选"),
      h("label", { class: "skb-force" }, force, "已出板也重生（--force）")),
    h("div", { class: "dlg-acts" },
      h("button", { class: "dlg-btn", onclick: close }, "取消"),
      h("button", { class: "dlg-btn primary", onclick: async (ev) => {
        const btn = ev.currentTarget;
        const sel = rows.filter((s) => checks.get(s.id).checked).map((s) => s.id);
        if (!sel.length) { toast("未选择任何分镜", true); return; }
        try {
          // runBusy 防连点：POST 在途再点一下就是第二个后台任务、双份按张计费
          const r = await runBusy(btn, "启动中…", () =>
            post("/api/sketch/gen", { project: d.project, chapter: d.id,
              shots: sel, force: force.checked }));
          toast(`简笔板生成已启动 · ${sel.length} 镜`);
          close();
          trackSketchJob(d.project, d.id, r.job, sel.join(","));  // 登记即重绘（内含 softRefresh）
        } catch (err) { toast(err.message, true); }
      } }, "开始生成")),
  ] });
}

// 五色图例固定行（灯箱 caption 首行）：板面不画图例横条——画上去会是全板最小的字，
// 图像模型必画糊，且模型侧语义走视频请求的 board_role_clause，图例的读者只有人。
const SKB_LEGEND = "图例：红=运动轨迹 · 蓝=摄影机运动 · 绿=取景/构图 · 橙=灯光方向 · 紫=强调/能量";

function sketchBoardItems(d) {
  return (d.shots || []).filter((s) => (s.sketch || {}).sheet).map((s) => ({
    shot: s.id,
    src: s.sketch.sheet,
    title: `SHOT ${String(s.id).padStart(2, "0")} · 简笔分镜板（${s.sketch.beats || "?"} 拍`
      + `${s.sketch.auto ? "·自动拆拍" : ""}）`,
    // 逐拍对照表（引擎 panel_lines 下发·与板上「面板内容」同一拼装）：
    // 对照第 N 格与第 N 行即可核对秒段与动作是否匹配；首行恒为五色图例
    caption: SKB_LEGEND + "\n" + ((s.sketch.lines || []).join("\n")
      || [s.action, s.video_prompt].filter(Boolean).join("｜") || s.narration || ""),
    // 灯箱「↻ 重新生成」的定位名片：后端凭它直达该镜（意见走 /api/sketch/regen --note）；
    // directive=改板指令全文——格签与灯箱按钮同一份文本，两处绝不各拼一版
    skctx: { pid: d.project, cid: d.id, shot: s.id,
             directive: sketchFixDirective(d, s) },
  }));
}

// 复制图标（双层圆角矩形·描边随 currentColor）：⧉ 字形在 84px 缩略上太糊，
// 换成与站内 SVG 图标同语言的矢量件
const SKB_COPY_SVG = '<svg viewBox="0 0 14 14" width="12" height="12" fill="none" '
  + 'stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">'
  + '<rect x="4.6" y="4.6" width="7.4" height="7.4" rx="1.6"/>'
  + '<path d="M9.4 2.4H3.6A1.6 1.6 0 0 0 2 4v5.8"/></svg>';

// 单板改板指令（格子右上角 ⧉）：带定位坐标交 Claude Code——要改拍序列本身时用；
// 只提意见不改拍走灯箱「↻ 重新生成」（--note 直达）即可，无需指令。
// 需求行不在这里拼——指令台统一补末行「需求：<…>」（灯箱与格签共用
// skctx.directive 同一份文本，需求行自然也长一个样）。
function sketchFixDirective(d, s) {
  const sk = s.sketch || {};
  return [
    `请精修项目 ${d.project} 章节 ${d.id} 镜 ${s.id} 的简笔分镜板（先 Read kinema-sketchboard skill）：`,
    `- beats 位置：project/${d.project}/chapters/${d.id}.json → shots[].sketch.beats`
      + `（当前 ${sk.auto ? "自动拆拍" : "自定义"} ${sk.beats || "?"} 拍）`,
    `- 按下述要求改写 beats（9 拍·秒段连续·末拍收在 end_state）后重生成：`,
    `  cd engine && python3 -m kinema sketch gen --chapter ${d.project}/${d.id} --only ${s.id} --force`,
  ].join("\n");
}

/* 改板指令台（格签 ⧉ 与灯箱按钮共用）：pid/cid/shot 只用于抬头与 toast 文案，
   指令正文恒取 directive——两处绝不各拼一版（守卫 test_sketchboard）。 */
function openSketchFixDialog({ pid, cid, shot, directive }) {
  return openDirectiveDialog({
    title: "改板指令", code: "SKETCH · REVISE",
    meta: `镜 ${shot} · 项目 ${pid} / 章节 ${cid}`,
    directive, ask: "在此写改板要求",
    hint: "例：第 4 拍动作幅度更大，人物冲出画面右侧；蓝色运镜箭头改为环绕弧线",
    done: `镜 ${shot} 改板指令已复制`,
  });
}

function sketchCard(d) {
  const st = d.sketch_stats || { beats: 0, boards: 0, total: 0 };
  const boards = sketchBoardItems(d);
  const activeN = (d.shots || []).filter((s) => s.guide_active === "sketch").length;
  const stat = (n, label) => h("div", { class: "skb-stat" },
    h("b", null, String(n)), h("span", null, label));
  // 板条格子 = 已出板的镜 ∪ 正在生成的镜（按分镜顺序）：每格左上角 SHOT 编号签；
  // 生成中的格子当场出现（提交即入账 SKGEN），已有板重生时旧图压暗盖转圈
  // 在跑的镜 = 本章全部在途任务负责的镜号并集（批量与单镜重生可并存，见 SKGEN）
  const gen = SKGEN.get(`${d.project}/${d.id}`);
  const genSet = new Set(gen ? [...gen.values()].flatMap((s) => [...s]) : []);
  const cells = [];
  (d.shots || []).forEach((s) => {
    if (s.kind === "transition") return;
    const sheet = (s.sketch || {}).sheet;
    const busy = genSet.has(String(s.id));
    if (!sheet && !busy) return;
    const tag = h("span", { class: "skb-tag",
      dataset: { tip: "点击跳到对应分镜卡" },
      onclick: (e) => { e.stopPropagation();
        const el = document.getElementById(`shot-${s.id}`);
        if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" });
          el.classList.remove("sb-flash"); void el.offsetWidth;
          el.classList.add("sb-flash"); } } },
      `SHOT ${String(s.id).padStart(2, "0")}`);
    // 两类漂移共用一枚角标（判据真源 sketchboard.board_drift，scanner 下发）：
    // stale=时长已变 · stale_beats=拍序列已变（beats/提示词改过而板画的还是旧节奏）
    const skv = s.sketch || {};
    // 漂移标收成一枚角点：84px 缩略里两行警示文字会把画面挤没，详情全给 tip
    const stale = (skv.stale || skv.stale_beats)
      ? h("span", { class: "skb-stale",
          dataset: { tip: (skv.stale
              ? `⚠ 时长已变（${skv.stale.was}s → ${skv.stale.now}s）\n板生成之后镜头时长改了，板上的秒段标签可能错位。\n`
              : "")
            + (skv.stale_beats
              ? "⚠ 拍序列已变\nbeats/提示词在板生成之后改过，板画的还是旧节奏——时间轴按新 beats 编译，板若附发（缺省全能参考档与 dubbed 板在盘即附）会向模型同时传入两套动作。\n"
              : "")
            + "建议重新生成（灯箱「↻ 重新生成」或勾选 --force）。" } })
      : null;
    if (busy) {
      cells.push(h("div", { class: "skb-cell gen" },
        sheet ? h("img", { src: sheet, loading: "lazy" }) : null,
        h("span", { class: "gw-ring" }), h("i", null, "生成中"), tag));
    } else {
      const idx = boards.findIndex((x) => x.shot === s.id);
      cells.push(h("div", { class: "skb-cell",
        title: `SHOT ${s.id} · 点开查看 / 重新生成`,
        onclick: () => openLightbox(boards, Math.max(0, idx)) },
        h("img", { src: sheet, loading: "lazy" }), tag, stale,
        h("span", { class: "skb-copy", html: SKB_COPY_SVG,
          dataset: { tip: "改板指令\n打开指令台写要求，与带定位坐标的标准指令合并后复制，"
            + "交 Claude Code 改写拍序列后重生；只提意见不改拍，点开图用「↻ 重新生成」更快。" },
          onclick: (e) => { e.stopPropagation();
            openSketchFixDialog({ pid: d.project, cid: d.id, shot: s.id,
              directive: sketchFixDirective(d, s) }); } })));
    }
  });
  const strip = cells.length
    ? h("div", { class: "skb-strip" }, ...cells)
    : h("p", { class: "skb-empty" },
        "还没有简笔板。运动描述完整的分镜可直接生成（自动拆拍）；"
        + "需要精确控制每一拍时，先用「⧉ 分镜板指令」把拍序列交由 AI 编写。");
  // 与 3D 导演台入口卡同一套视觉语言：网格渐变底纹 + 描边软填主按钮（掠光 hover）
  return h("div", { class: "card skb-card" },
    h("div", { class: "skb-grid", "aria-hidden": "true" }),
    h("div", { class: "skb-body" },
      // 说明在左、主钮与读数瓦片在右——与 3D 导演台 `.dzc-mid`/`.dzc-foot` 同骨架
      h("div", { class: "skb-main" },
        h("div", { class: "skb-lead" },
          deskHead(SK_ICO, "简笔分镜板", "生成视频之前，先把动作编排到秒"),
          h("p", { class: "skb-desc" },
            "把一镜按时长拆成若干拍，生成一张「铅笔素描 + 五色标注箭头」的分镜预演板。",
            h("br"),
            "生成视频时拍序列编译为分段时间轴随请求提交；dubbed 模式下分镜板"
            + "作为参考图一并提交，native 首帧模式不附板、仅作排戏对照。")),
        h("div", { class: "skb-side" },
      h("div", { class: "skb-acts" },
        // .cy = 与「进入导演台」同一支青：两条预演路径的主行动是一类事
        h("button", { class: "skb-go cy", onclick: () => openSketchGenDialog(d) },
          h("span", { class: "skb-go-ico" }, "▦"),
          h("b", null, "生成简笔板"),
          h("i", { class: "skb-go-arw" }, "→")),
        h("button", { class: "act-btn",
          dataset: { tip: "⧉ 分镜板指令\n打开指令台：写下这一章的运动节奏诉求，与带定位坐标"
            + "的标准指令合并后复制，交 Claude Code 逐镜写 9 拍 beats。" },
          onclick: () => openDirectiveDialog({
            title: "分镜板指令", code: "SKETCH · BEATS",
            ask: "在此写运动节奏要求",
            meta: `项目 ${d.project} / 章节 ${d.id}`,
            directive: sketchDirective(d),
            hint: "例：整章动作节奏再快一档，每镜前两拍就要有明确位移；打斗镜多给近景",
            done: "分镜板指令已复制——粘贴给 Claude Code 逐镜写 beats",
          }) },
          "⧉ 分镜板指令")),
          h("div", { class: "skb-stats" },
            stat(`${st.beats}/${st.total}`, "拆拍就绪"),
            stat(`${st.boards}/${st.total}`, "已出板"),
            stat(String(activeN), "生效镜 · 走简笔板")))),
      strip,
      // 与 3D 导演台的状态行同款：说清「当前这一集，这套东西会不会真的生效」。
      // kenburns 这一档的话必须先说——板按 image provider 计费，而它一格也不进成片
      h("p", { class: "skb-note" }, !d.uses_video
        ? "本集为 Ken Burns 静图运镜，不发 gen-video：板与拍序列都不参与出片，"
          + "生成的板只作排戏对照，而每张板按分镜图同价计费。"
          + "切到 native / dubbed 后拍序列才编译进请求。"
        : activeN
        ? `本集有 ${activeN} 个分镜按简笔板生成——两种预演都配置过的分镜默认采用 3D 预演，`
          + "可在分镜卡的徽章处逐镜切换。"
        : "与 3D 导演台逐镜二选一：两种预演都配置过的分镜默认采用 3D 预演，"
          + "可在分镜卡的徽章处切换为简笔板。")));
}

/* 工作台卡头（图标 + 名字 + 一句话）——与 3D 导演台 `.dzc-head` 同一制式。
   三个工作台并排展示，卡头制式必须统一：不能只有 3D 那张有卡头、另外两张
   直接从正文开始。图标走同一套线性语汇（28 视框·描边·无填充）。 */
function deskHead(ico, name, tagline) {
  return h("div", { class: "skb-head" },
    // 图标恒青色（`.skb-ico` 缺省）：三个工作台是同一类东西，用同一个色标；
    // 琥珀在这张卡里已经是"读数与主行动"的色，图标再占一份会跟它抢
    h("span", { class: "skb-ico", html: ico }),
    h("div", null, h("b", null, name), h("em", null, tagline)));
}

// 简笔分镜：取景框里一道带箭头的动势线 + 斜过右下角的铅笔（拆拍=把运动画进框）
const SK_ICO = '<svg viewBox="0 0 28 28">'
  + '<rect x="3" y="4.4" width="22" height="16.4" rx="2.4"/>'
  + '<path d="M7.4 16.6c2.2-5.4 6.2-7.6 11.6-6.6"/>'
  + '<path d="M15.8 8.2l3.4 1.6-1.4 3.5"/>'
  + '<path d="M25.2 16.8l-6.2 6.2-2.7.8.8-2.7 6.2-6.2z"/></svg>';
// 音频剧本：折角剧本页 + 一条连续声波（一段话变成一条已混好的整轨）
const AU_ICO = '<svg viewBox="0 0 28 28">'
  + '<path d="M6.2 2.8h9.6l6 6V23a2.2 2.2 0 0 1-2.2 2.2H6.2A2.2 2.2 0 0 1 4 23V5'
  + 'a2.2 2.2 0 0 1 2.2-2.2z"/>'
  + '<path d="M15.8 2.8v6h6"/>'
  + '<path d="M7.6 17.2c1-3.4 2-3.4 3 0s2 3.4 3 0 2-3.4 3 0 1.6 2.4 2.4 1"/></svg>';

/* ---- 音频剧本台：一段自然语言同时定人声/音乐/音效，音频模型一次输出「已混好」的
   整轨。与「逐镜 TTS + BGM + 音效三轨确定性混音」是互斥的两条路，章节 audio_mode
   仲裁；接缝按转场镜切（真源 audioscript.plan，前端只消费不自算）。 ---- */

// 整轨生成忙态：`pid/cid` → job id。一条章节级任务，不按段各起一条轮询
const SCOREJOBS = new Map();

/* 未存手稿暂存 + 光标名片。textarea 只活在渲染闭包里，而重渲不止 3s 轮询一条路
   （别的任务收尾 softRefresh、镜过审 refreshAfterWrite、插转场……都直接重建整卡）
   ——手稿不存在渲染之外，任何一次静默重渲都会把没存的字整体冲掉。存稿成功即清。 */
const AUS_DRAFTS = new Map();   // `pid/cid` → Map(段号 → 正文)
let AUS_FOCUS = null;           // { key, no, start, end }：重渲后把光标放回正在编辑的段
// 段折叠态：`pid/cid` → Set(展开的段号)。缺省全收起（分段多时列表一屏放不下），
// 展开态存在渲染之外，重渲/轮询不丢
const AUS_OPEN = new Map();

function trackScoreJob(pid, cid, jid) {
  const key = `${pid}/${cid}`;
  if (SCOREJOBS.get(key) === jid) return;      // 对账每次进视图都会调，任务级去重
  SCOREJOBS.set(key, jid);
  softRefresh(pid, cid);                       // 登记即重绘：忙态是渲染的一部分
  const done = (msg, bad) => {
    SCOREJOBS.delete(key);
    if (msg) toast(msg, bad);
    return softRefresh(pid, cid);
  };
  pollJob(jid, {
    onDone: () => done("音频剧本整轨已生成"),
    onFail: (j) => done(`音频剧本生成失败：${(j.tail || "").slice(-160)}`, true),
  });
}

function audioScriptDirective(d) {
  const a = d.audio_script || {};
  const segs = a.segments || [];
  // 逐镜秒段直接摊在指令里（引擎算的段内相对秒）——让写剧本的人手算「这一镜从第几秒
  // 到第几秒」，是这条路上最容易错且最贵的一步（错了要重买整段）
  const rows = segs.flatMap((g) => [
    `  · 第 ${g.no} 段（${g.dur}s · 全片 ${g.start}s→${g.end}s）`
    + `${(g.script || "").trim() ? " 已写" : " 待写"}，段内秒段：`,
    "      " + (g.spans || []).map((p) => `镜${p.id} [${p.start}s:${p.end}s]`).join("　"),
  ]);
  const drafted = segs.filter((g) => (g.script || "").trim()).length;
  return [
    `请为项目 ${d.project} 章节 ${d.id} 写「音频剧本」（先 Read kn-audio skill 第四节）：`,
    `- 文件：project/${d.project}/chapters/${d.id}.json 顶层 audio_script.segments[]`,
    drafted
      ? `- **底稿已在里面**（引擎按分镜起草：声线定义段 + 逐句「谁·[段内秒段]·台词」）。`
        + "你的活是**在底稿上改写**，不是重写："
      : "- 还没有底稿。先在网页「AU 音频剧本」台点「✎ 按分镜起草」"
        + `（或跑 python3 -m kinema score --chapter ${d.project}/${d.id} --draft）`
        + "把台词与秒段填好，再按下面改写：",
    "  ① 声线定义段——按 characters[] 的人设把每个说话人那行改具体"
      + "（年龄/性别/口音/音质/气质五维，用可听见的物理属性不用「好听」这类评价词）",
    "  ② 补一段音乐描述（配器＋情绪）——**先读项目的整体设定与场景**"
      + "（style/scene/scenes 与本段各镜的画面、情绪弧线），配乐要贴这一段的戏，"
      + "不写泛泛的背景音；情绪变化点单独起句并说清落在哪句前后",
    "  ③ 逐句补语气与生理细节（吸气/停顿/音量变化），音效按**本段场景**取材"
      + "（环境声/动作声/氛围声），用「伴随着/紧接着/句尾」缝进台词，不另起清单",
    "- 音频剧本自带配乐与音效，本路线合成时**不再叠加曲库 BGM**"
      + "（确需额外铺一层 BGM，在章节顶层显式写 `scored_bgm: true`）",
    "- **两条不许动**：台词原文（字幕与 narration 同源，改一个字成片就是「念的和写的不一样」）；"
      + "[起s:止s] 的**段内相对秒**基准（每段各自一次请求，模型时间轴从 0 起）",
    `- 本章按转场镜切成 ${segs.length} 段（单段上限 ${a.limit || 115}s，正文 ≤3000 字符）：`,
    ...rows,
    `- 写完在网页点「♪ 生成整轨」，或先零成本核对：cd engine && `
      + `python3 -m kinema score --chapter ${d.project}/${d.id} --dry-run`,
  ].join("\n");
}

function openScoreGenDialog(d) {
  const a = d.audio_script || {};
  const segs = a.segments || [];
  const checks = new Map();
  const items = segs.map((g) => {
    const ck = uiCheck();
    const written = !!(g.script || "").trim();
    ck.checked = written && !g.generated;      // 缺省勾「剧本已写且没出过音轨」
    checks.set(g.no, ck);
    return h("label", { class: "aus-pick" + (written ? "" : " off") }, ck,
      h("b", null, `第 ${g.no} 段`),
      h("span", null, `镜 ${g.shots[0]}~${g.shots[g.shots.length - 1]} · ${g.dur}s`),
      written ? null : chip("剧本待写", "gray"),
      g.generated ? chip("已生成", "green") : null);
  });
  const force = uiCheck();
  openShell({ card: "skb-dlg", build: (close) => [
    h("span", { class: "k" }, "♪ 生成音频剧本整轨"),
    h("p", { class: "dlg-msg" },
      "按音频时长计费（单段上限 "
      + `${a.limit || 115}s`
      + "），勾选的段各提交一次生成请求。已生成的段不勾选则直接复用、不重复计费；"
      + "修改过剧本的段需勾选后重新生成。"),
    h("div", { class: "skb-list" }, ...items),
    h("div", { class: "skb-tools" },
      h("button", { class: "act-btn",
        onclick: () => segs.forEach((g) =>
          (checks.get(g.no).checked = !!(g.script || "").trim())) }, "全选可生成"),
      h("button", { class: "act-btn",
        onclick: () => segs.forEach((g) => (checks.get(g.no).checked = false)) }, "全不选"),
      h("label", { class: "skb-force" }, force, "全部重新生成（--force）")),
    h("div", { class: "dlg-acts" },
      h("button", { class: "dlg-btn", onclick: close }, "取消"),
      h("button", { class: "dlg-btn primary", onclick: async (ev) => {
        const btn = ev.currentTarget;
        const sel = segs.filter((g) => checks.get(g.no).checked).map((g) => g.no);
        if (!sel.length && !force.checked) { toast("未选择任何分段", true); return; }
        try {
          // runBusy 防连点：POST 在途再点一下就是第二个后台任务、双份按秒计费
          const r = await runBusy(btn, "启动中…", () =>
            post("/api/score/gen", { project: d.project, chapter: d.id,
              only: force.checked ? [] : sel, force: force.checked }));
          toast("音频剧本生成已启动");
          close();
          trackScoreJob(d.project, d.id, r.job);
        } catch (err) { toast(err.message, true); }
      } }, "开始生成")),
  ] });
}

/* 段谱系弹层：同一段剧本连出几版挑一版。生成式模型每次演绎都不同，所以这不是
   "防手滑的备份"而是创作工具——与设定图宫格候选是同一件事，只是候选来自不同时刻。
   切换是**互换**（当前版先入栈），来回切不丢任何一版；切完引擎自动重拼整轨。 */
function segVersionsBtn(d, g) {
  const hist = g.versions || [];
  return h("button", { class: "act-btn sm",
    dataset: { tip: `第 ${g.no} 段演绎过 ${hist.length + 1} 版——点开逐版试听、切回任意一版` },
    // 生成期间不切版：切换写的 gen.score 不在长任务合并白名单里，
    // 任务收尾的 save 会用开跑时的旧副本把这次切换覆盖掉
    onclick: () => SCOREJOBS.has(`${d.project}/${d.id}`)
      ? toast("整轨生成中——完成后再切换版本，否则会被本次生成结果覆盖", true)
      : openShell({ card: "skb-dlg", build: (close) => [
      h("span", { class: "k" }, `♪ 第 ${g.no} 段 · 演绎谱系`),
      h("p", { class: "dlg-msg" },
        "同一段剧本每次生成都是一次新的演绎，可在历史版本间试听、切换。"
        + "切换为互换操作（当前版本自动归档），不会丢失任何版本；"
        + "切换后整轨自动重新拼接，重新合成后进入成片。"),
      h("div", { class: "aus-vers" },
        h("div", { class: "aus-ver cur" },
          h("b", null, `v${String(g.current_v || 1).padStart(3, "0")}`),
          chip("当前", "green"),
          g.media ? audioPill(g.media, "试听") : h("span", { class: "shot-cap" }, "音频文件缺失")),
        ...hist.slice().reverse().map((e) => h("div", { class: "aus-ver" },
          h("b", null, `v${String(e.v).padStart(3, "0")}`),
          h("span", { class: "shot-cap" },
            [String(e.at || "").slice(5, 16).replace("T", " "), e.reason]
              .filter(Boolean).join(" · ")),
          e.media ? audioPill(e.media, "试听") : null,
          h("button", { class: "act-btn sm ok", onclick: async (ev) => {
            const btn = ev.currentTarget;
            try {
              await runBusy(btn, "切换中…", () => post("/api/score/switch",
                { project: d.project, chapter: d.id, no: g.no, to_v: e.v }));
              toast(`第 ${g.no} 段已切换到 v${e.v}，整轨已重新拼接——重新合成后生效`);
              close();
              refreshAfterWrite(d);
            } catch (err) { toast(err.message, true); }
          } }, "切到这版")))),
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "关闭")),
    ] }) }, `⏱ ${hist.length + 1} 版`);
}

/* 参考音读数：这一段发请求时会把谁的声音一并带上。

   带不带音色决定这段听起来是不是同一个人，而绑定行
   （`旁白 的饰演者为@音频1。`）是发送时引擎补的，框里那份剧本上没有——不读出来
   它在页面上完全看不见。按秒计费的一步，看不见就只能生成完听了才知道。
   判据由引擎下发（与真发同一个函数）。 */
function anchorChip(d, g) {
  const a = g.anchor || {};
  const on = a.anchored || [], off = a.loose || [];
  if (!on.length && !off.length) return null;
  const who = on.map((x) => `${x.who}→@音频${x.no}`).join(" · ");
  // 读数按**参考音条数**（去重后的 no），不按说话人数：两个角色共用一把声音时
  // 实发只有一条参考音
  const clips = new Set(on.map((x) => x.no)).size;
  const c = titledChip(on.length ? `♪ 参考音 ${clips}` : "⚠ 无参考音",
    on.length ? "" : "amber",
    (on.length ? `♪ 本段携带 ${clips} 条参考音频——点击查看对应关系并试听\n${who}\n`
               : "⚠ 本段没有可用的参考音频\n")
    + (off.length ? `未锁定音色：${off.join("、")}——仅按文字描述生成，段与段之间音色可能不一致。\n`
                  : "")
    + "参考音频＝该说话人选定的音色样本，随生成请求一并提交以锁定音色（接口最多 3 条）。");
  c.classList.add("clickable");
  c.onclick = (e) => { e.stopPropagation(); openAnchorDialog(g); };
  return c;
}

/* 参考音频对应关系弹层：@音频N → 说话人 → 可试听的音色样本，附系统提交时
   自动生成的对应语句。剧本正文里**不写** @音频N——编号在提交时按参考音频的
   数组顺序确定，手写必然错位。 */
function openAnchorDialog(g) {
  const on = (g.anchor || {}).anchored || [];
  const off = (g.anchor || {}).loose || [];
  openShell({ card: "skb-dlg", build: (close) => [
    h("span", { class: "k" }, `♪ 第 ${g.no} 段 · 参考音频`),
    h("p", { class: "dlg-msg" },
      "生成本段时，以下音色样本将作为参考音频随请求提交，编号即正文中的 @音频N。"
      + "说话人与参考音频的对应语句由系统在提交时自动附加，无需写入剧本。"),
    h("div", { class: "aus-vers" },
      ...on.map((x) => h("div", { class: "aus-anchor" },
        h("div", { class: "aus-anchor-top" },
          h("b", null, `@音频${x.no}`),
          h("span", { class: "shot-cap" }, x.who),
          x.media ? audioPill(x.media, "试听")
                  : h("span", { class: "shot-cap" }, "首次生成时自动合成，此后可在此试听")),
        // 对应语句随行展示；后端未下发（引擎待重启）时整行不渲染，不留半成品区块
        x.bind ? h("i", { class: "aus-anchor-bind" }, x.bind) : null)),
      off.length ? h("p", { class: "shot-cap" },
        `未锁定音色：${off.join("、")}——仅按剧本中的文字描述生成`) : null),
    h("div", { class: "dlg-acts" },
      h("button", { class: "dlg-btn", onclick: close }, "关闭")),
  ] });
}

function audioScriptCard(d) {
  const a = d.audio_script || {};
  const segs = a.segments || [];
  const scored = a.mode === "scored";
  // dubbed 是唯一与整轨互斥的渲染模式（kenburns/native 两档都成立）——判据与引擎
  // 硬闸同一条，不在这里另立「纯 ffmpeg 才配整轨」之类的口径
  const dubLock = d.motion === "dubbed";
  const busy = SCOREJOBS.has(`${d.project}/${d.id}`);
  const boxes = new Map();
  const stat = (n, label) => h("div", { class: "skb-stat" },
    h("b", null, String(n)), h("span", null, label));

  const dKey = `${d.project}/${d.id}`;
  const drafts = AUS_DRAFTS.get(dKey) || new Map();
  // 两个状态分工不同，不合并：
  //  · unsaved = 框里的 ≠ 盘上的（含预填底稿）→ 拦住「生成整轨」，它发的是盘上那份
  //  · dirty   = 用户敲过字 → 暂停 3s 轮询（值已进暂存冲不掉，但重绘会打断输入）
  let unsaved = false;                    // 初值在 rows 建完后统一刷进 UI（见卡尾）
  let dirty = false;
  const dirtyEls = [];                    // 跟着 unsaved 变脸的节点（存稿钮/主钮/未存标）
  const markDirty = (on, { typed = true } = {}) => {
    if (typed) {
      dirty = on;
      state.live = !on;                   // 手敲过就别让轮询打断输入；存稿即恢复
    }
    // typed=false（预填/初始化）不碰 state.live：那是别的流程也在用的全局开关。
    // 不做同值早退：初始 unsaved 可为 true 而 UI 按缺省画着——UI 同步职责全在这里，
    // 早退会让「未存改动拦生成」在有底稿的章节上永远不亮
    unsaved = on;
    dirtyEls.forEach((f) => f(on));
  };

  // 逐段一条：分段元信息（引擎算的）+ 剧本正文（人写的）。段落之间刻意不合并成
  // 一个大编辑框——接缝落在哪一镜、这一段有几秒，正是写时间控制时最需要在眼前的东西
  const open = AUS_OPEN.get(dKey) || new Set();
  const openers = new Map();              // 段号 → 展开函数（光标回位前先展开目标段）
  const rows = segs.map((g) => {
    const ta = h("textarea", { class: "aus-ta", rows: 6, spellcheck: false });
    // 取值优先级：暂存的手稿 > 已存稿 > 引擎底稿预填（确定性派生自分镜，零成本）。
    // 预填不落盘，头部挂「底稿·未存」标明身份
    const kept = drafts.get(g.no);
    const prefill = kept == null && !(g.script || "").trim() && (g.draft || "").trim();
    ta.value = kept != null ? kept : (g.script || g.draft || "");
    boxes.set(g.no, ta);
    ta.placeholder = `第 ${g.no} 段（${g.dur}s · 镜 `
      + `${g.shots[0]}~${g.shots[g.shots.length - 1]}）——点上方「✎ 按分镜起草」`
      + "自动填入台词与秒段，再通过「⧉ 音频剧本指令」交由 AI 补写声线、配乐与音效";
    // 整轨正在生成时**锁住这些框**：此刻改的是"下一次要发的剧本"，而正在跑的是
    // "盘上那一版"，改完回来会分不清哪一版对应哪条音轨（起草那一步刻意不锁——
    // 它是毫秒级本地计算，且起草的全部意义就是给你一个底稿去改）
    if (busy) {
      ta.readOnly = true;
      ta.dataset.tip = "整轨生成中，剧本已锁定——生成期间的修改无法对应到"
        + "当前任务的产出，生成结束后可继续编辑";
    }
    const count = h("i", { class: "aus-count" });
    const peek = h("p", { class: "aus-peek" });
    const sync = () => {
      const n = ta.value.length;
      count.textContent = `${n}/3000`;
      count.classList.toggle("over", n > 3000);   // 接口硬上限，超了服务端直接拒
      // 收起态的一行摘要：取正文首个非空行（通常是声线定义），给「这段写的是什么」
      const first = (ta.value || "").split("\n").find((l) => l.trim());
      peek.textContent = first ? first.trim() : "（本段尚无内容——展开编辑）";
    };
    ta.addEventListener("input", () => {
      drafts.set(g.no, ta.value);
      AUS_DRAFTS.set(dKey, drafts);
      AUS_FOCUS = { key: dKey, no: g.no, start: ta.selectionStart, end: ta.selectionEnd };
      sync();
      markDirty(true);
    });
    ta.addEventListener("focus", () => {
      AUS_FOCUS = { key: dKey, no: g.no, start: ta.selectionStart, end: ta.selectionEnd };
    });
    sync();
    const seg = h("div",
      { class: "aus-seg" + (busy ? " locked" : "") + (open.has(g.no) ? "" : " fold") },
      h("div", { class: "aus-head",
        // 头行即开关：点交互件（试听/切版/参考音）不触发折叠
        onclick: (e) => {
          if (e.target.closest("button, .apill, .chip.clickable, audio")) return;
          setOpen(seg.classList.contains("fold"));
        } },
        h("i", { class: "aus-caret", "aria-hidden": "true" }),
        h("b", null, `第 ${g.no} 段`),
        h("span", null, `镜 ${g.shots[0]}~${g.shots[g.shots.length - 1]}`
          + `（${g.shots.length} 镜）· 全片 ${g.start}s→${g.end}s · ${g.dur}s`),
        prefill ? h("span", { class: "chip amber",
          dataset: { tip: "按分镜自动生成的底稿（台词与段内秒段已填入），**尚未写入文档**。\n"
            + "可通过「⧉ 音频剧本指令」补写声线/配乐/音效，或直接编辑，完成后存稿。" } },
          "底稿·未存") : null,
        g.generated ? chip(`音轨 ${g.actual != null ? `${g.actual}s` : "已生成"}`, "green")
                    : chip("未生成", "gray"),
        anchorChip(d, g),
        g.media ? audioPill(g.media, `v${g.current_v || 1}`) : null,
        (g.versions || []).length ? segVersionsBtn(d, g) : null,
        count),
      peek, ta);
    const setOpen = (on) => {
      seg.classList.toggle("fold", !on);
      if (on) open.add(g.no); else open.delete(g.no);
      AUS_OPEN.set(dKey, open);
    };
    openers.set(g.no, setOpen);
    return seg;
  });

  const saveAll = async (btn) => {
    // 没有可分段的分镜 = 一个编辑框都没渲染，此时提交空数组会把盘上已写的剧本
    // 整块清掉（端点按「全空=撤回」处理）。分镜被删/全弃用时点一下存稿就丢稿，
    // 是最不该有的那种数据丢失——这里直接拦住
    if (!segs.length) { toast("本章还没有可分段的分镜，先补分镜再存稿", true); return; }
    const payload = segs.map((g) => boxes.get(g.no).value);
    try {
      await runBusy(btn, "存稿中…", () => post("/api/score/save",
        { project: d.project, chapter: d.id, segments: payload }));
      // 只清「这次真的发出去」的那份：POST 在途敲的字已重新入暂存，一并清会丢
      payload.forEach((txt, i) => {
        if (drafts.get(segs[i].no) === txt) drafts.delete(segs[i].no);
      });
      if (drafts.size) {
        AUS_DRAFTS.set(dKey, drafts);
      } else {
        AUS_DRAFTS.delete(dKey);
        AUS_FOCUS = null;          // 没有未存手稿就不再抢焦点（回位只服务中途打断）
      }
      markDirty(drafts.size > 0);
      toast("音频剧本已存稿——「♪ 生成整轨」现在发的就是这一版");
      refreshAfterWrite(d);
    } catch (err) { toast(err.message, true); }
  };

  // 按分镜起草：引擎确定性拼出台词与段内秒段（零成本），**填进框但不落盘**——
  // 让人看过改过再点存稿。直接写文档会把「我点一下看看」变成一次静默覆盖。
  const draftAll = async (btn) => {
    if (!segs.length) { toast("本章还没有可分段的分镜", true); return; }
    // 确认判据是「框里的内容 ≠ 引擎底稿」——已存稿与手写未存都算数；
    // 只有原样躺着的预填底稿才免问（覆盖它没有任何区别）
    const touched = segs.filter((g) => {
      const cur = (boxes.get(g.no)?.value || "").trim();
      return cur && cur !== (g.draft || "").trim();
    }).length;
    if (touched && !(await uiConfirm(
      `有 ${touched} 段内容与自动底稿不同（已保存或手动修改过），`
      + "重新起草将覆盖当前编辑内容（存稿前不会写入文档）。是否继续？",
      { title: "按分镜起草" }))) return;
    try {
      const r = await runBusy(btn, "起草中…", () => post("/api/score/draft",
        { project: d.project, chapter: d.id }));
      (r.segments || []).forEach((text, i) => {
        const ta = boxes.get(segs[i]?.no);
        if (ta) { ta.value = text; ta.dispatchEvent(new Event("input")); }
      });
      toast((r.thin || []).length
        ? `已起草 ${(r.segments || []).length} 段 · ${(r.thin || []).join("、")} 尚无声线描述`
          + "——可通过「⧉ 音频剧本指令」补写，或先为其定制音色"
        : `已起草 ${(r.segments || []).length} 段，台词与秒段已填入`
          + "——声线、配乐与音效可通过「⧉ 音频剧本指令」补写，确认后请存稿");
    } catch (err) { toast(err.message, true); }
  };

  // 存稿钮与「未存」标：unsaved 时转主色，把「框里的 ≠ 盘上的」摆到明面上
  const saveBtn = h("button", { class: "act-btn", disabled: busy,
    dataset: { tip: "存稿\n将各段剧本写入章节文档（audio_script.segments）。\n"
      + "**「♪ 生成整轨」使用已保存的版本**，修改后需先存稿再生成；\n"
      + "存稿不产生费用，也不会触发生成。" },
    onclick: (e) => saveAll(e.currentTarget) }, "存稿");
  const dirtyTag = h("span", { class: "aus-dirty", hidden: true }, "● 未存改动");
  dirtyEls.push((on) => {
    dirtyTag.hidden = !on;
    saveBtn.classList.toggle("hot", on);
  });

  // 未存改动的吸底工具条：段列表可能十几屏长，顶部那颗存稿钮滚出视野就看不见。
  // 它与工具条的「存稿」是**同一动作的同一入口**，只在工具条滚出视野时浮现——
  // 同屏出现两颗存稿钮是冗余
  const dockN = h("i");
  const dock = h("div", { class: "aus-dock", hidden: true },
    h("span", { class: "aus-dock-k" }, "● 未存改动"), dockN,
    h("button", { class: "act-btn", onclick: (e) => saveAll(e.currentTarget) }, "存稿"));
  let toolsSeen = true;
  const syncDock = () => {
    dock.hidden = !unsaved || busy || toolsSeen;
    if (!dock.hidden) {
      const n = segs.filter((g) =>
        (boxes.get(g.no)?.value || "").trim() !== (g.script || "").trim()).length;
      dockN.textContent = `${n} 段修改未保存 · 生成整轨使用已保存的版本`;
    }
  };
  dirtyEls.push(syncDock);

  // 「生成整轨」发的是**盘上**那份剧本，故 unsaved 时拦住并说明。
  // 软禁用而非 disabled：disabled 元素不派发鼠标事件，「为什么点不动」的
  // tip 全都哑掉——不可用时点击 toast 指路
  const goBtn = h("button", { class: "skb-go" + (scored ? "" : " off"),
    onclick: () => {
      if (busy) { toast("整轨生成中——完成后可再次生成"); return; }
      if (dubLock && !scored) {
        toast("dubbed 对口型与音频剧本互斥——要走整轨请先把本集渲染模式改成 native", true);
        return;
      }
      if (!scored) { toast("请先切换到音频剧本路线（点击右上「当前路线」指标）", true); return; }
      if (!segs.length) { toast("本章还没有可分段的分镜", true); return; }
      if (unsaved) {
        toast("存在未保存的修改——生成整轨使用已保存的剧本版本，请先存稿", true);
        return;
      }
      openScoreGenDialog(d);
    } },
    h("span", { class: "skb-go-ico" }, busy ? "◔" : "♪"),
    h("b", null, busy ? "生成中…" : "生成整轨"),
    h("i", { class: "skb-go-arw" }, "→"));
  const syncGo = (on) => {
    goBtn.classList.toggle("off", !scored || busy || !segs.length || on);
    goBtn.dataset.tip = on
      ? "存在未保存的修改。生成整轨使用**已保存**的剧本版本——请先存稿，"
        + "否则本次将按旧版本计费生成"
      : scored ? ""
      : dubLock ? "C · Dubbed 对口型模式下音频剧本不可用（引擎拒发）——"
          + "要走整轨先把本集渲染模式改成 native"
      : "当前为三轨混音路线——点击右上「当前路线」指标可切换";
  };
  dirtyEls.push(syncGo);
  // 初始态入册：预填底稿/暂存手稿都算「框里的 ≠ 盘上的」。只有暂存里真有手稿
  // 才按「敲过字」恢复轮询暂停——预填不暂停轮询（守卫钉此语义）
  markDirty(segs.some((g) =>
    (boxes.get(g.no)?.value || "").trim() !== (g.script || "").trim()),
    { typed: drafts.size > 0 });

  const toggle = async () => {
    // 生成期间不切路线：正在跑的任务按 scored 收尾，切走的表态会被就地作废
    if (SCOREJOBS.has(`${d.project}/${d.id}`)) {
      toast("整轨生成中——完成后再切换音频路线", true);
      return;
    }
    // scored × dubbed 互斥（引擎硬闸 `cli.stage_gen_video`，dry-run 同拦）：口型由
    // 逐镜 TTS 的 ref_audio 驱动，而 scored 的人声出自音频模型整轨，合成时片段音轨
    // 会被整轨整个替换——口型与观众听到的人声不是同一份，两道钱都白花。
    // **只拦 tracks→scored 这一侧**：盘上已是 scored 的 dubbed 章节要能切得回来。
    if (dubLock && !scored) {
      toast("dubbed 对口型与音频剧本互斥——要走整轨请先把本集渲染模式改成 native", true);
      return;
    }
    const to = scored ? "tracks" : "scored";
    const msg = scored
      ? "切回三轨混音？此后按「逐镜配音 + 背景音乐 + 音效」三轨混音，"
        + "音频剧本与已生成的整轨均会保留，可随时切回。"
      : "切换到音频剧本？此后本章**不再逐镜配音**——人声、音乐、音效由音频模型"
        + "按剧本一次生成并混合，合成阶段不再叠加背景音乐。已有的旁白音轨保留。";
    if (!(await uiConfirm(msg, { title: "音频路线" }))) return;
    try {
      await post("/api/score/save", { project: d.project, chapter: d.id, mode: to });
      toast(to === "scored" ? "已切到音频剧本路线" : "已切回三轨混音");
      refreshAfterWrite(d);
    } catch (err) { toast(err.message, true); }
  };

  // 编辑区自己的工具条：起草与存稿都是"对着这些框做的事"，
  // 放顶行动作条里离它们操作的对象隔了半张卡
  const toolsRow = h("div", { class: "aus-tools" },
    h("span", { class: "aus-tools-k" }, "逐段剧本"),
    dirtyTag,
    h("button", { class: "act-btn", disabled: busy,
      dataset: { tip: "✎ 按分镜起草\n按分镜自动生成底稿：逐句填入「说话人 · 段内秒段 · "
        + "台词原文」——台词与分镜旁白逐字一致（与字幕同源），秒段按各句字数比例分配。\n"
        + "不调用模型、不产生费用；**仅填入编辑框不写入文档**，确认后请手动存稿。\n"
        + "声线、配乐、音效与逐句语气可通过「⧉ 音频剧本指令」交由 AI 补写。" },
      onclick: (e) => draftAll(e.currentTarget) }, "✎ 按分镜起草"),
    saveBtn);
  // 吸底条的显隐盯着工具条进出视野（回调里自检 isConnected，重渲后旧观察者自行退场）
  const io = new IntersectionObserver((es) => {
    if (!toolsRow.isConnected) { io.disconnect(); return; }
    toolsSeen = es[0].isIntersecting;
    syncDock();
  });
  io.observe(toolsRow);

  // 光标回位：softRefresh 可能在敲字中途重建整卡——值由暂存接住，光标在这里放回
  // （挂载后才聚焦得上；仅在焦点空置时回放，不从别的输入框手里抢）。
  // 只在**真有未存手稿**时回位：存稿后/无手稿时再进本章，focus 会把视口
  // 直接拽到页面深处的剧本框
  if (AUS_FOCUS && AUS_FOCUS.key === dKey && drafts.size && boxes.has(AUS_FOCUS.no)) {
    const f = AUS_FOCUS;
    openers.get(f.no)?.(true);   // 正在编辑的段重渲后保持展开，光标才有处可回
    setTimeout(() => {
      const ta = boxes.get(f.no);
      const idle = !document.activeElement || document.activeElement === document.body;
      if (ta && ta.isConnected && idle && !ta.readOnly) {
        ta.focus();
        ta.setSelectionRange(Math.min(f.start, ta.value.length),
                             Math.min(f.end, ta.value.length));
      }
    }, 0);
  }
  return h("div", { class: "card skb-card" },
    h("div", { class: "skb-grid", "aria-hidden": "true" }),
    h("div", { class: "skb-body" },
      // 骨架与 3D 导演台 `.dzc-mid`/`.dzc-foot` 一致：说明在左，主钮与读数瓦片在右
      h("div", { class: "skb-main" },
        h("div", { class: "skb-lead" },
          deskHead(AU_ICO, "音频剧本", "整章的声音，一段话说清楚"),
          // 注意：h() 不解析 markdown，强调要用真的 <strong>
          h("p", { class: "skb-desc" },
            "一段自然语言同时确定人声、配乐与音效，音频模型一次输出",
            h("strong", null, "已经混合完成"), "的整轨。",
            h("br"),
            "剧本无需手写：「✎ 按分镜起草」自动填入台词与段内秒段，"
            + "「⧉ 音频剧本指令」将底稿连同人物设定交由 AI 补写声线、配乐与逐句语气。"),
          (a.problems || []).length
            ? h("div", { class: "skb-alert" },
                ...(a.problems || []).map((p) => h("span", null, p)))
            : null),
        h("div", { class: "skb-side" },
          h("div", { class: "skb-acts" },
            goBtn,
            h("button", { class: "act-btn",
              dataset: { tip: "⧉ 音频剧本指令\n打开指令台：写下这一章的声音诉求，与带分段坐标"
                + "和逐镜秒段的标准指令合并后复制，交 Claude Code 按 kn-audio 逐段写剧本。" },
              onclick: () => openDirectiveDialog({
                title: "音频剧本指令", code: "AUDIO · SCRIPT",
                ask: "在此写声音要求",
                meta: `项目 ${d.project} / 章节 ${d.id}`,
                directive: audioScriptDirective(d),
                hint: "例：整章配乐用弦乐加钢琴，压抑；旁白中年男声偏冷；第 2 段结尾要一声远处的雷",
                done: "音频剧本指令已复制——粘贴给 Claude Code 逐段写剧本",
              }) },
              "⧉ 音频剧本指令")),
          h("div", { class: "skb-stats" },
            // 路线瓦片本身可点即切换：一个已经在显示当前路线的东西，再配一个
            // 「切到音频剧本」按钮是同一件事占两处位置
            // 软禁用而非摘掉 onclick：dubbed 下点它 toast 出理由，比一个点不动
            // 又不说话的瓦片有用（同 goBtn 的口径）
            h("div", { class: "skb-stat" + (scored ? " amber" : "") + " clickable",
              dataset: { tip: scored ? "点击切回三轨混音（逐镜 TTS + BGM + 音效）"
                : dubLock ? "本集是 C · Dubbed 对口型：口型由逐镜 TTS 的参考音驱动，"
                    + "与音频模型整轨互斥（引擎拒发）——要走整轨先把渲染模式改成 native"
                : "点击切到音频剧本路线" },
              onclick: toggle },
              h("b", null, scored ? "音频剧本" : "三轨混音"),
              h("span", null, dubLock && !scored ? "当前路线 · dubbed 下不可切"
                                                 : "当前路线 · 点击切换")),
            stat(`${segs.length}`, `分段 · 上限 ${a.limit || 115}s`),
            // 分母是分段数，分子只数**已落盘**的段：预填的底稿不计入
            h("div", { class: "skb-stat",
              dataset: { tip: "已写入章节文档的段数。\n"
                + "自动底稿已预填在下方编辑框中（标注「底稿·未存」），"
                + "保存前不计入；点「存稿」后计入。\n"
                + "「♪ 生成整轨」使用已保存的内容。" } },
              h("b", null, `${a.written || 0}/${segs.length}`),
              h("span", null, "已存稿")),
            stat(a.duration != null ? `${a.duration}s` : "—", "整轨时长")))),
      a.score ? h("div", { class: "aus-play" }, audioPill(a.score, "整轨")) : null,
      rows.length
        ? h("div", null, toolsRow,
            h("div", { class: "aus-list" }, ...rows),
            dock)
        : h("p", { class: "skb-empty" },
            "还没有可分段的分镜。先在分镜脚本里把旁白与时长写好，"
            + "分段会按转场镜自动切出，「✎ 按分镜起草」即可拿到底稿。"),
      // dubbed × scored 的两种在场姿态分开说：已经是 scored 的 dubbed 章节是**盘上
      // 的错配**（gen-video 会当场拒发），得给出两条出路；还没切的只需说清为什么切不了
      dubLock && scored
        ? h("div", { class: "skb-alert" },
            h("span", null, "⚠ 本集渲染模式为 dubbed，与音频剧本互斥——"
              + "gen-video 会拒发（对口型人声由逐镜 TTS 喂入，而整轨的人声出自音频模型，"
              + "合成时片段音轨会被整轨替换）。"),
            h("span", null, "两条出路：把渲染模式改成 native，"
              + "或点右上「当前路线」切回三轨混音。"))
        : null,
      h("p", { class: "skb-note" }, scored
        ? "本章当前为音频剧本路线：不再逐镜配音、合成阶段不叠加背景音乐，"
          + "成片声音全部来自这条整轨。字幕仍逐字取自分镜旁白，剧本台词须与分镜一字不差。"
        : dubLock
        ? "本章当前为三轨混音路线（逐镜配音 + 背景音乐 + 音效）。C · Dubbed 对口型模式下"
          + "音频剧本不可用——以上剧本可以先写，渲染模式改成 native 后即可切过去。"
        : "本章当前为三轨混音路线（逐镜配音 + 背景音乐 + 音效）——以上剧本暂不参与成片，"
          + "点击右上「当前路线」指标可切换到音频剧本路线。")));
}

function shotCard(d, s, i) {
  if (s.kind === "transition") return transitionCard(d, s, i);
  // 忙态与缓存击穿都按镜 key 取——遮罩/徽章是渲染的一部分，重绘不丢
  const bustSrc = (shot, src) => {
    const t = BUST.get(jobKey(d.project, d.id, shot));
    return t ? withBust(src, t) : src;   // 云端直链无查询串，接符由 withBust 统一裁决
  };
  const busy = GENJOBS.get(jobKey(d.project, d.id, s.id));
  const lbItems = d.shots.filter((x) => x.image).map((x) => ({
    src: bustSrc(x.id, x.image), title: `SHOT ${String(x.id).padStart(2, "0")}`,
    caption: [x.image_prompt, x.video_prompt && `｜运动：${x.video_prompt}`].filter(Boolean).join(""),
    ctx: { pid: d.project, cid: d.id, shot: x.id, stage: "image",
           comments: x.comments || [], refs: x.refs },   // refs → 灯箱内「⛭ 垫图」入口
  }));
  const lbIdx = lbItems.findIndex((x) => x.src === bustSrc(s.id, s.image || ""));
  const pinCount = (s.comments || []).length;

  const visual = h("div", { class: "shot-visual",
      // 检查器能力说明落在画面本身（入口就是点击画面，说明跟着入口走）
      dataset: s.image ? { tip: "点开检查器工作台\n◉ 打点 / 划线提意见（攒着，重新生成时"
        + "全部带上）；✂ 框选一块区域＋指令立即局部改造（只改这一处）；"
        + "也可整镜重生成、下载原图。" } : null,
      onclick: s.image ? () => openLightbox(lbItems, lbIdx) : null },
    s.image ? h("img", { src: bustSrc(s.id, s.image), loading: "lazy", alt: `shot ${s.id}` })
            : h("div", { class: "ph" }, "PENDING", h("span", null, "待生成")),
    h("span", { class: "slate" + (s.omitted ? " omt" : "") },
      s.omitted ? `SHOT ${String(s.id).padStart(2, "0")} · OMT` : `SHOT ${String(s.id).padStart(2, "0")}`),
    s.clip && h("span", { class: "clipmark" }, "▸ CLIP"),
    // 左下角标堆栈（flex 列容器，绝不悬空占位）：◉意见 / ◈预演 / ▸动态片段 / ▦简笔
    // 从上到下排——播放按钮紧贴「▦ 简笔」上方，堆叠由容器管、
    // 不靠 .pzmark ~ .skmark 这类兄弟偏移规则逐组合手排
    (pinCount > 0 || s.previz || s.clip || (s.sketch || {}).sheet) &&
    h("div", { class: "vmarks" },
      pinCount > 0 && h("span", { class: "pinmark",
        dataset: { tip: `◉ ${pinCount} 条改造意见\n在检查器里打点/划线记下的意见——`
          + "「↻ 重新生成」时自动带九宫格方位词编译进提示词。" } }, `◉ ${pinCount}`),
      // 3D 预演挂载：**只标不播**——分镜卡的播放位只认 clip（成片），
      // previz 是灰模参考片，点这里进控制台看，不在这里当成片放
      s.previz && h("span", { class: "pzmark",
        onclick: (e) => { e.stopPropagation();
          openCinema({ video: s.previz, title: `镜 ${s.id} · 3D 预演参考片`,
            rows: [["运镜", s.camera || "—"], ["preset", s.camera_preset || "—"],
                   ["时长", s.previz_seconds ? `${(+s.previz_seconds).toFixed(1)}s` : "—"]],
            chips: ["previz", "灰模预演·非成片"] }); },
        // 角标恒在（产物还在盘上就得够得着），但「它到底管不管用」随 motion 变：
        // kenburns 不发 gen-video，首帧/末帧/V2V 三个去处一个都不存在
        dataset: { tip: "◈ 3D 预演已挂载\n本镜的走位/运镜来自 3D 导演控制台。"
          + (d.uses_video
            ? "点开预览参考片；它同时提供视频请求的首帧与末帧，"
              + "开了 V2V 还会作参考视频迁移运动。"
            : "点开预览参考片；本集为 Ken Burns 静图运镜、不发 gen-video，"
              + "首帧/末帧/V2V 均不生效，运镜措辞仍会写进分镜。") } }, "◈ 预演"),
      // ▸ 视频：成片素材的播放入口（Seedance 片段），画面即播放位——与 ▦ 简笔
      // （它的运动脚本）上下相邻互为对照；版式与 ▦ 简笔逐值一致，只有颜色区分
      s.clip && h("button", { class: "clip-play",
        onclick: (e) => { e.stopPropagation();
          // 运动提示词与折叠区同源现算（/api/video-preview）：逐镜各是各的实发稿，
          // 引用记号照样可点。生成期快照只作首帧占位——字段改过后它是旧一版的实发
          const snap = ((s.gen || {}).clip || {}).prompt;
          const pvCin = h("div", { class: "cin-stack pv-cin" },
            snap ? h("pre", { class: "pv-prompt" }, snap)
                 : h("span", { class: "shot-cap" }, "编译中…（与实发同源）"));
          chapterSendPreview(d).then((pv) => {
            const row = ((pv || {}).shots || [])
              .find((x) => String(x.id) === String(s.id)) || {};
            const vr = row.video;
            if (!vr || !(vr.positive || vr.prompt)) return;   // 无预览行维持快照
            pvCin.replaceChildren(...[
              h("pre", { class: "pv-prompt" },
                pvRich(vr.positive || vr.prompt, vr, pvCtx(d, s))),
              vr.negative ? h("div", { class: "pv-neg" },
                h("span", { class: "pv-neg-tag" }, "避免出现"),
                h("span", { class: "pv-neg-txt" }, vr.negative)) : null,
            ].filter(Boolean));
          }).catch(() => {});
          openCinema({
          video: s.clip, title: `${d.title} · SHOT ${s.id} 动态片段`,
          chips: [motionBadge(d.motion), clipVerChip(s)],
          rows: [["设计时长", fmtSec(s.dur)],
                 // dubbed 片段自带的人声是模型对参考音的重演（嗓音逐镜自选），
                 // 成片烧的是选角配音——不注明的话，这里听到的声音会被当成成片音色
                 ...(d.motion === "dubbed"
                     ? [["片段原声", "模型重演素材，不进成片（成片主音轨=选角配音）"]]
                     : []),
                 ["运动提示词", pvCin],
                 // 版本谱系入口：放映窗只播当前版，历次归档在版本面板里逐版可播、可回滚。
                 // 两处共用 openVPanel（分镜图/配音/片段同一个面板），不为片段另造一套。
                 ["版本谱系", clipVerEntry(d, s)]] }); },
        dataset: { tip: "▸ 播放本镜视频\n视频模型生成的动态片段（成片素材位）。"
          + "信息栏里可进版本谱系：逐版回看、一键回滚。" } },
        "▸ 视频"),
      // 简笔分镜板挂载：同 previz 一样只标不当成片——点开灯箱看板
      (s.sketch || {}).sheet && h("span", { class: "skmark",
        onclick: (e) => { e.stopPropagation();
          const items = sketchBoardItems(d);
          openLightbox(items, Math.max(0, items.findIndex((x) => x.src === s.sketch.sheet))); },
        dataset: { tip: "▦ 简笔分镜板已生成\n铅笔素描 + 五色标注（红=身体运动/蓝=运镜），网格按拍数恰好填满。"
          + (d.uses_video
            ? "\ngen-video 时 beats 编译成分段时间轴；板在盘即随请求附发（缺省全能参考/dubbed），仅衔接章的首帧任务镜不附。"
            : "\n本集为 Ken Burns 静图运镜、不发 gen-video：板与拍序列都不参与出片，只作排戏对照。") } },
        "▦ 简笔")),
    s.dur != null && h("span", { class: "dur-tag" }, fmtSec(s.dur)),
    busy && h("div", { class: "gen-wait" },
      h("span", { class: "gw-ring" }),
      h("b", null, JOB_ZH[busy.kind] || "生成中"),
      h("i", null, busy.kind === "clip" ? "视频模型渲染约 2~5 分钟 · 完成自动替换"
                                        : "模型出图约 15~60s · 完成自动替换")));

  // 表态区：每个产物一组 [状态徽章(点开版本面板)] [✓通过] [↻重做]
  const noteBox = h("div", { class: "retake-box", hidden: "" });
  const openNote = (stage) => {
    noteBox.hidden = false;
    noteBox.innerHTML = "";
    const input = h("input", { class: "cmt-input", type: "text",
      placeholder: `打回「${STAGE_ZH[stage]}」的重做意见（将编译进下一版提示词）…` });
    const submit = async () => {
      if (!input.value.trim()) return;
      try {
        await post("/api/review", { project: d.project, chapter: d.id,
          shots: [s.id], stage, state: "retake", note: input.value.trim() });
        toast(`镜 ${s.id} · ${STAGE_ZH[stage]} 已打回重做`);
        refreshAfterWrite(d);
      } catch (err) { toast(err.message, true); }
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    noteBox.append(input,
      h("button", { class: "act-btn no", onclick: submit }, "↻ 确认打回"),
      h("button", { class: "act-btn", onclick: () => { noteBox.hidden = true; } }, "取消"));
    input.focus();
  };
  const stageCell = (st, state) => {
    const r = REVIEW[state] || { zh: state, cls: "" };
    const v = (s.versions || {})[st];
    const badge = chip(`${STAGE_ZH[st] || st}·${r.zh}${v ? ` v${v + 1}` : ""}`, r.cls);
    if (v || (s.version_history || {})[st]) {
      badge.classList.add("clickable");
      badge.dataset.tip = "版本谱系\n点击查看此产物的历次归档：参数对照、归档原因、一键回滚。";
      badge.onclick = (e) => { e.stopPropagation(); openVPanel(d, s, st); };
    }
    const cell = h("span", { class: "stage-cell" }, badge);
    if (state === "wfa" || state === "retake") {
      cell.append(
        h("button", { class: "act-btn ok",
          dataset: { tip: "✓ 通过并锁定\n锁定后重生成/直供一律拒改（--force 也不覆盖）；"
            + "要重做先打回。" },
          onclick: async (e) => { e.stopPropagation();
            try {
              await post("/api/review", { project: d.project, chapter: d.id,
                shots: [s.id], stage: st, state: "done" });
              toast(`镜 ${s.id} · ${STAGE_ZH[st]} 已通过锁定`);
              refreshAfterWrite(d);
            } catch (err) { toast(err.message, true); } } }, "✓"),
        h("button", { class: "act-btn no",
          dataset: { tip: "↻ 打回重做\n附一句意见——自动编译进下一版提示词（驳回闭环）；"
            + "语气/情绪问题写进意见同样生效。" },
          onclick: (e) => { e.stopPropagation(); openNote(st); } }, "↻"));
    }
    return cell;
  };

  const meta = h("div", { class: "shot-meta" },
    s.speaker && h("span", { class: "spk" }, s.speaker,
      s.voice && h("i", null, `♪ ${s.voice}`)),
    s.framing && chip(s.framing),
    s.angle && chip(s.angle),
    s.camera && chip(`◎ ${s.camera}`),
    s.lens && chip(`◎ ${s.lens}`),
    s.transition && s.transition !== "切" && chip(`→ ${s.transition}`),   // 默认硬切不显示，只显特殊转场
    s.rank != null && chip(`No.${s.rank}`, "amber"),
    s.status === "failed" && chip("失败", "red"),
    s.omitted && chip("已弃用", "red"),
    // previz/sketch 互斥仲裁徽章：**两路都配置了才显示**——单路时
    // 自动仲裁已是唯一解，摆开关只添噪音。点击一键切到另一条（写 shots[].guide）。
    // 不发 gen-video 的章节两路都不生效，「哪一路胜出」也就无从谈起（产物角标照旧在）
    d.uses_video && s.previz && ((s.sketch || {}).beats > 0 || (s.sketch || {}).sheet) && (() => {
      const onSketch = s.guide_active === "sketch";
      const c = titledChip(onSketch ? "▦ 走简笔板" : "◈ 走3D预演", onSketch ? "amber" : "",
        "本镜同时配置了 3D 预演与简笔分镜板（互斥，同发会互相打架）\n"
        + `当前生效：${onSketch ? "简笔板（分段时间轴；缺省档板在盘即附发，衔接章首帧任务不附）" : "3D 预演（末帧 / V2V）"}`
        + "\n点击切换到另一条预演路径");
      c.classList.add("clickable");
      c.onclick = async (e) => { e.stopPropagation();
        const next = onSketch ? "previz" : "sketch";
        try {
          await post("/api/sketch/guide", { project: d.project, chapter: d.id,
            shot: s.id, guide: next });
          toast(`镜 ${s.id} · 预演路径已切到${next === "sketch" ? "简笔板" : " 3D 预演"}`);
          refreshAfterWrite(d);
        } catch (err) { toast(err.message, true); }
      };
      return c;
    })(),
    // 血缘徽章：设定图缺失（就绪度）/ 设定图已更新（过期）
    (s.missing_refs || []).length > 0 &&
      titledChip("⊘ 缺设定图", "red", `⊘ 缺设定图\n缺失：${s.missing_refs.join("、")}`
        + "——gen-video 将拦截此镜；先跑 project refs 生成设定图。"),
    (s.stale_refs || []).length > 0 &&
      titledChip("⚠ 设定已更新", "amber", `⚠ 设定已更新\n已变化：${s.stale_refs.join("、")}`
        + "——画面可能与新设定不符，建议重新生成本镜。"),
    (s.voice_stale || []).length > 0 &&
      titledChip("⚠ 音色已更换", "amber", "⚠ 音色已更换\n本镜配音出自 "
        + `${s.voice_stale.join("、")}，该音色已停用`
        + "——本镜已通过审阅故只作标记；要跟上新音色请重跑本镜配音。"),
    (s.voice_clip_stale || []).length > 0 &&
      titledChip("⚠ 片段音色已换", "amber", "⚠ 片段音色已换\n本镜片段的人声由视频模型按 "
        + `${s.voice_clip_stale.join("、")} 念出，该音色已停用`
        + "——这条人声不在配音轨上，重跑 tts 不会改变它。"
        + "\n要跟上新音色只能重烧本镜：clip 已是「重做」的直接 gen-video；"
        + "已通过审阅的先打回（review set --stage clip --state retake，"
        + "或在看板把卡片拖到「重做」列）。按秒计费，先 --dry-run 看报价。"),
    (s.stale_text || []).length > 0 &&
      titledChip("⚠ 台词已改", "amber", "⚠ 台词已改\n本镜的 "
        + `${s.stale_text.map((x) => STALE_TEXT_ZH[x] || x).join("、")} 出自旧台词`
        + "——成片里念的与烧录的字幕不是同一句；重跑对应阶段即可，"
        + "或用 lineage mark 把全章一并置重做。"),
    // 音色锚定预告（native 原生配音）：已选角说话人的锚定音随 gen-video 附发，
    // 模型按「参考音频N」绑定用该嗓音念台词；数据由 scanner 与真发同源产出
    ((s.voice_anchor || {}).anchored || []).length > 0 &&
      titledChip(`♪ 音色锚定×${new Set(s.voice_anchor.anchored.map((a) => a.no)).size}`,
        "green", "♪ 音色锚定（native 按选角发声、口型对台词）\n随生视频请求附发："
        + s.voice_anchor.anchored.map((a) => `${a.who}→参考音频${a.no}`).join("、")
        + ((s.voice_anchor.loose || []).length
          ? `\n⚠ 未选角（模型自选嗓音）：${s.voice_anchor.loose.join("、")}` : "")
        + ((s.voice_anchor.over || []).length
          ? `\n⚠ 参考位已满，未附发：${s.voice_anchor.over.join("、")}` : "")),
    (s.voice_anchor || {}).anchored && !s.voice_anchor.anchored.length
      && (s.voice_anchor.loose || []).length > 0 &&
      titledChip("⚠ 未选角", "amber", "⚠ 本镜说话人未绑定音色\n"
        + `${s.voice_anchor.loose.join("、")} 的嗓音由模型每镜自选，跨镜会漂移`
        + "\n——voice 试音立档或章节 voices 表登记后，锚定音自动随请求附发。"),
    /* 参考位已满与未选角是两回事：这个说话人选过角，只是本镜参考音条数到顶了。
       再去选角不会改变任何事——要么减少本镜说话人，要么换限额更高的档 */
    ((s.voice_anchor || {}).over || []).length > 0 &&
      titledChip("⚠ 参考位已满", "amber", "⚠ 本镜参考音条数已到接口上限\n"
        + `${s.voice_anchor.over.join("、")} 已选角但本镜不附发参考音，嗓音由模型自选`
        + "\n——减少本镜说话人，或换参考音条数上限更高的视频档。"),
    // 角色跨镜一致性判定：引擎只产料不打分，这里显示指挥层回填的结论
    (s.consistency || {}).verdict === "drift" &&
      titledChip("⚠ 角色漂移", "red", "⚠ 角色跨镜一致性：判定为漂移"
        + `${s.consistency.score != null ? `（分 ${s.consistency.score}）` : ""}`
        + `${s.consistency.note ? `\n${s.consistency.note}` : ""}`
        + `\n判定人 ${s.consistency.by || "-"} · ${(s.consistency.at || "").slice(0, 16)}`
        + "\n本镜角色与设定图不是同一个人——按设定图重生成本镜。"),
    (s.consistency || {}).verdict === "ok" &&
      titledChip("✓ 角色一致", "green", "✓ 角色跨镜一致性：判定为一致"
        + `${s.consistency.score != null ? `（分 ${s.consistency.score}）` : ""}`
        + `\n判定人 ${s.consistency.by || "-"} · ${(s.consistency.at || "").slice(0, 16)}`
        + "\n判定来自 consistency scan 产料 + 指挥层多模态比对（引擎不打分）。"),
    // 生成期间对应产物徽章显示实况（wip=生成中），✓/↻ 表态钮随之隐藏——完成后回落待审；
    // clip 任务（交给 Seedance）点亮「片」徽章，其余任务点亮「图」徽章
    ...Object.entries(s.review || {}).map(([st, state]) =>
      stageCell(st, busy && st === (busy.kind === "clip" ? "clip" : "image")
        ? "wip" : state)));
  const notes = Object.entries(s.review_notes || {});
  const noteRow = notes.length ? h("div", { class: "shot-cap" },
    "审阅意见 · ", notes.map(([st, n]) => `${STAGE_ZH[st] || st}：${n}`).join("；")) : null;

  // 片段的播放入口在画面左下（.vmarks 的 ▸ 视频）——卡身 media 行只放配音条，
  // 两处都放是同一产物两个播放位
  const media = h("div", { class: "shot-media" },
    s.audio && audioPill(s.audio, "配音"));

  // 提示词只读展示；要改提示词走 Claude Code——两条指令台分工明确：
  // 「⧉ 改镜指令」只管运镜/动作文案（camera/video_prompt 双语，不碰图片）；
  // 「⧉ 改图指令」管画面（image_prompt 增量融合 + 重生图片，一条指令两件事）。
  const copyDirective = (e) => {
    e.stopPropagation();
    openDirectiveDialog({
      title: "改镜指令", code: "SHOT · MOTION",
      meta: `镜 ${s.id} · 项目 ${d.project} / 章节 ${d.id}`,
      ask: "在此写运镜/动作要求",
      hint: "例：镜头改为缓慢推入，人物动作放慢半拍；结尾停在回眸",
      done: `镜 ${s.id} 改镜指令已复制——粘贴给 AI 即可`,
      directive: [
        `请修改分镜运镜描述：项目 ${d.project} / 章节 ${d.id} / 镜 ${s.id}`,
        `（文件 project/${d.project}/chapters/${d.id}.json，shots[].id=${s.id}）`,
        s.camera ? `当前 camera（作者字段）：${s.camera}` : null,
        // 只引中文作者字段：_en 由 AI 改写时同步（它有文件权限自行读取），
        // 引进来只会让工单中英夹杂难读
        s.video_prompt ? `当前 video_prompt（作者字段，改写时中英同步）：${s.video_prompt}` : null,
        d.uses_video && ((s.sketch || {}).sheet || s.clip)
          ? "注意：本镜已有简笔板/动态片段——改完 video_prompt 请一并说明是否重出板"
            + "（sketch gen --force）或重发视频，否则板与片段仍是旧节奏。" : null,
        "落地：本指令只改文案（camera / video_prompt 中英同步），不重生图片；"
          + "画面要改走「⧉ 改图指令」。",
        "改完后实发效果在分镜卡「提示词 · PROMPTS」展开即见（与真发同源编译，"
          + "含契约句/时间轴/音色绑定）——那里才是模型实收的那句。",
      ].filter(Boolean).join("\n"),
    });
  };
  // 改图指令：一条指令同时改 image_prompt 与图片本身（先词后图）。核心纪律是
  // 增量融合——保留原提示词的画风与一致性锚点，只叠加局部改动，绝不推倒重写
  const copyImageDirective = (e) => {
    e.stopPropagation();
    const img = s.image ? (mediaPath(s.image) || s.image.split("&v=")[0]) : null;
    const rvState = (s.review || {}).image;
    const rv = REVIEW[rvState];
    openDirectiveDialog({
      title: "改图指令", code: "SHOT · IMAGE",
      meta: `镜 ${s.id} · 项目 ${d.project} / 章节 ${d.id}`,
      ask: "在此写画面改造要求",
      hint: "例：把背景霓虹换成暖黄路灯，人物表情改为强忍情绪；其余保持不变",
      note: "指令会要求 AI 先把要求融合进现有 image_prompt（保留原画风与一致性锚点、"
          + "只叠局部改动），再按新提示词重生本镜画面——提示词与图片一次改到位。"
          + "改完后实发效果在分镜卡「提示词 · PROMPTS」展开即见（与真发同源编译）。",
      done: `镜 ${s.id} 改图指令已复制——粘贴给 AI 即可`,
      directive: [
        `请修改镜 ${s.id} 的画面：项目 ${d.project} / 章节 ${d.id}（motion=${d.motion}）`,
        `（文件 project/${d.project}/chapters/${d.id}.json，shots[].id=${s.id}）`,
        `当前画面文件：${img || "未生成"}`,
        // 只引中文作者字段（_en 由 AI 同步改写、自行读文件）——工单不做中英夹杂
        s.image_prompt ? `当前 image_prompt（作者字段，改写时中英同步）：${s.image_prompt}` : null,
        rv ? `本镜画面审阅态：${rv.zh}`
          + (rvState === "done" ? "（已通过锁定：重生前必须先 retake，--force 也不覆盖）" : "")
          : null,
        "执行纪律（先词后图，两步都做；需求明确说明只改提示词时可只做第①步）：",
        "① 改提示词=增量融合，绝不推倒重写：保留原 image_prompt 的画风、构图、景别、"
          + "光线与角色/场景一致性锚点，只把上述需求当作局部改动叠加进去，整合成一条"
          + "完整的新提示词；原文没被需求点到的部分一个字都不动。image_prompt_en 按同一"
          + "口径对位改写（中英必须说同一件事，否则英文 provider 会照旧图跑）。改完写回章节 JSON。",
        "② 再按新提示词重生本镜画面：",
        `   cd engine && python3 -m kinema review set --chapter ${d.project}/${d.id}`
          + ` --shots ${s.id} --stage image --state retake --note "<一句话写明本次改造要点>"`,
        `   cd engine && python3 -m kinema gen-image --chapter ${d.project}/${d.id} --only ${s.id}`,
        "   （旧图自动归档进版本栈可回滚；生图按张计费，不加批量参数）",
        "③ 出图后本镜落「待审」，请用户在 Studio 看图确认；通过后再 review set --state done。",
        "本指令不改 video_prompt / 运镜描述——那是「⧉ 改镜指令」的事。",
      ].filter(Boolean).join("\n"),
    });
  };
  // 提示词折叠区：展开即显「实发提示词」——生图与生视频两路都取自与真发同一条
  // 编译路径（/api/video-preview 双子进程：gen-image / gen-video --preview-json）。
  // IMAGE 含风格前缀/角色锚/防字地板/负面句式，MOTION 含契约句/分段时间轴/台词/
  // 情绪/音色绑定。作者字段 IMAGE/MOTION 归「分镜脚本」表编辑视角：卡上摆作者
  // 字段，与 API 实收的那句必然两样，照着它调整等于对不上靶。
  let pvLoaded = false;
  const pvHost = h("div", { class: "prompt-rows" });
  // 每块一节：mono 眉行（左 TAG · 右模型/秒数）压 hairline + 正文 + 独立负面块。
  // 正文放 positive（不含负面尾句）、负面串单独成 AVOID 块——全文与负面同屏
  // 就是同一批词显示两遍（positive/negative 分列由 PromptEnvelope 契约保证）
  const pvBlock = (tag, row) => {
    if (!row) return [];
    // note 形如「▸ 镜1 · 5s · 图=shot_1.png · 台词内嵌 prompt · 全能参考(…)」，
    // 镜号在卡上是冗余信息，剥掉只留取材注记
    const note = String(row.note || "").replace(/^▸\s*镜\S+\s*·\s*/, "");
    const meta = [row.model || row.provider,
                  row.seconds != null ? `${row.seconds}s` : null]
      .filter(Boolean).join(" · ");
    // 中英对照：正文/负面按当前语种取（alt 为引擎另编的一版，实发恒是主语种）
    const texts = (alt) => (alt && row.alt
      ? { p: row.alt.positive, n: row.alt.negative }
      : { p: row.positive || row.prompt, n: row.negative });
    const bodyHost = h("div");
    const render = (alt) => {
      const t = texts(alt);
      bodyHost.replaceChildren(...[
        t.p ? h("pre", { class: "pv-prompt" }, pvRich(t.p, row, pvCtx(d, s))) : null,
        t.n ? h("div", { class: "pv-neg" },
          h("span", { class: "pv-neg-tag" }, alt && row.lang !== "en" ? "AVOID" : "避免出现"),
          h("span", { class: "pv-neg-txt" }, t.n)) : null,
      ].filter(Boolean));
    };
    render(false);
    const hasAlt = !!(row.alt && (row.alt.positive || row.alt.negative));
    const zhFirst = (row.lang || "zh") !== "en";
    let cur = false;
    let bMain; let bAlt;
    const sync = () => { bMain.classList.toggle("on", !cur);
      bAlt.classList.toggle("on", cur); };
    bMain = h("a", { class: "pv-lang on",
      onclick: (e) => { e.stopPropagation();
        if (cur) { cur = false; render(cur); sync(); } } }, zhFirst ? "中" : "EN");
    bAlt = h("a", { class: "pv-lang",
      onclick: (e) => { e.stopPropagation();
        if (!cur) { cur = true; render(cur); sync(); } } }, zhFirst ? "EN" : "中");
    // 审阅锁（仅 MOTION，紧随 TAG）：通过=存正文 sha，真发前引擎重编译比对一致
    // 才发。sha 取自本行预览——用户刚看过的那份编译产物。表态后原位重绘控件，
    // 不重取整章预览（缓存内的 row.approval 同步改，保持同一份事实）
    const apprBox = h("span", { class: "pv-appr-box" });
    const renderAppr = () => {
      const bits = [];
      if (row.approval === "ok") {
        bits.push(h("span", { class: "pv-appr ok" }, "✓ 已审"));
      } else if (row.approval === "stale") {
        bits.push(h("span", { class: "pv-appr stale" }, "⚠ 审后有变"));
      }
      bits.push(h("a", {
        class: "pv-appr-act" + (row.approval === "ok" ? " dim" : ""),
        dataset: { tip: row.approval === "ok"
          ? "撤销审阅锁：真发不再校验一致性"
          : "通过实发稿：真发前引擎重编译比对正文一致才放行——审过的就是发出的；"
            + "审后字段有变会拦下该镜并点名" },
        onclick: async (e) => { e.stopPropagation();
          const revoke = row.approval === "ok";
          try {
            await post("/api/prompt-approval", { project: d.project, chapter: d.id,
              shot: s.id, sha: revoke ? null : row.prompt_sha });
            row.approval = revoke ? null : "ok";
            renderAppr();
            toast(revoke ? `镜 ${s.id} 审阅锁已撤销`
              : `镜 ${s.id} 实发稿已通过——真发前将校验一致性`);
          } catch (err) { toast(err.message, true); }
        } }, row.approval === "ok" ? "撤销审阅" : "✓ 通过实发稿"));
      apprBox.replaceChildren(...bits);
    };
    if (tag === "MOTION" && row.prompt_sha) renderAppr();
    return [h("div", { class: "pv-sec" },
      h("div", { class: "pv-head" },
        h("span", { class: "pv-head-l" },
          h("span", { class: "tag" }, tag), apprBox),
        h("span", { class: "pv-head-r" },
          hasAlt && h("span", { class: "pv-langs",
            dataset: { tip: "中英对照（引擎同源编译的另一语种版本）——实发给 API 的恒是"
              + (zhFirst ? "中文" : "英文") + "版" } }, bMain, " / ", bAlt),
          meta && h("span", { class: "pv-meta" }, meta))),
      note && h("p", { class: "pv-note" }, note),
      row.error ? h("p", { class: "pv-note" }, `⚠ 编译失败：${row.error}`) : null,
      row.gate ? h("p", { class: "pv-note" }, `⚠ 正式生成将被拦截：${row.gate}`) : null,
      bodyHost,
      /* 锚定音的入口在正文里：@配音N 记号即点即听（含待预热的现场合成），
         此处只补「没进正文的缺口」——未选角的说话人在提示词里没有记号可点 */
      (row.loose || []).length > 0
        ? h("p", { class: "pv-note pv-anchor-note" },
            `♪ 音色未选角：${row.loose.join("、")}——台词由模型自选嗓音、跨镜会漂移；`
            + "voice 试音立档（旁白设 narrator_voice）后锚定音自动随请求附发")
        : null,
      (row.over || []).length > 0
        ? h("p", { class: "pv-note pv-anchor-note" },
            `♪ 音色参考位已满：${row.over.join("、")}已选角但本镜不附发——`
            + "参考音条数到了接口上限，减少本镜说话人或换上限更高的视频档")
        : null)];
  };
  const loadSendPreview = async () => {
    if (pvLoaded) return;
    pvLoaded = true;
    pvHost.replaceChildren(h("p", { class: "shot-cap" }, "编译中…（与真发同源）"));
    let pv;
    try { pv = await chapterSendPreview(d); }
    catch (err) {
      pvLoaded = false;                       // 收起再展开即重试
      pvHost.replaceChildren(h("p", { class: "shot-cap" }, `⚠ ${err.message}`));
      return;
    }
    const row = ((pv || {}).shots || []).find((x) => String(x.id) === String(s.id));
    if (!row) {
      pvHost.replaceChildren(h("p", { class: "shot-cap" },
        (pv || {}).error || `镜 ${s.id} 不发生成请求（直供画面/转场镜不出图）`));
      return;
    }
    const parts = [...pvBlock("IMAGE", row.image), ...pvBlock("MOTION", row.video),
                   (pv || {}).error ? h("p", { class: "shot-cap" }, `⚠ ${pv.error}`) : null]
      .filter(Boolean);
    pvHost.replaceChildren(...parts);
  };
  const pfKey = `${d.project}/${d.id}/${s.id}`;
  // 初始即展开（重绘恢复态）时 toggle 事件不触发，主动补一次加载（缓存 10s 内秒回填）
  if ((d.uses_video || s.image_prompt) && PF_OPEN.has(pfKey)) {
    queueMicrotask(loadSendPreview);
  }
  const prompts = (d.uses_video || s.image_prompt) &&
    h("details", { class: "prompt-fold", open: PF_OPEN.has(pfKey) ? "" : null,
        ontoggle: (e) => {
          if (e.target.open) { PF_OPEN.add(pfKey); loadSendPreview(); }
          else PF_OPEN.delete(pfKey);
        } },
      h("summary", { dataset: { tip: "实发提示词\n发给图像/视频模型的完整编译产物"
        + "（与真发同一条路径）：IMAGE 含风格前缀+角色锚+负面约束，MOTION 含契约句+"
        + "分段时间轴+台词/情绪/音色绑定。展开即算、零成本，中英对照可切换"
        + "（实发给 API 的恒是主语种）。作者字段在「分镜脚本」表里看与改。" } },
        "提示词 · PROMPTS", " · 中/EN"),
      pvHost);

  // ⧉ 视频指令：带纪律的单镜 gen-video 标准指令交 Claude Code——先 dry-run
  // 审提示词与报价、经用户确认才真发（Seedance 逐秒计费，网页绝不直接起真发任务）。
  // 指令按本镜实况拼装：运动预演走哪条道（sketch 板/纯时间轴/previz/无）、是否已有
  // 片段（重生须先 retake）、dubbed 是否要先 tts——agent 拿到即知道缺哪道前置。
  const copyGenVideo = (e) => {
    e.stopPropagation();
    const sk = s.sketch || {};
    // 附板只在 dubbed 参考媒体模式合法（native 首帧模式官方禁混首/末帧与参考媒体，
    // 板不附、时间轴照发）——指令措辞必须与引擎真实行为一致，别让 agent 去核对一个不会出现的标记
    const beatsTag = `${sk.beats || "?"} 拍${sk.auto ? "·自动拆拍" : "·手写"}`;
    const lane = s.guide_active === "sketch"
      ? (sk.sheet
          ? (d.motion === "dubbed"
              ? `简笔板+分段时间轴（${beatsTag}）`
              : (d.frame_chain
                  ? `分段时间轴（${beatsTag}·衔接章首帧任务不附板，板作人工对照物）`
                  : `简笔板随请求附发+分段时间轴（${beatsTag}·缺省全能参考档）`))
          : `分段时间轴·无板（beats ${beatsTag}）`)
      : s.guide_active === "previz" ? "3D previz（末帧/V2V 按配置）"
      : "无运动预演——video_prompt 需自带先后次序的详细分段，或按 kinema-sketchboard 铁律〇补 beats（无板也生效）";
    const txt = [
      `请为镜 ${s.id} 生成视频片段 · 项目 ${d.project} / 章节 ${d.id}（motion=${d.motion}）`,
      `当前运动规划：${lane}`,
      // 本镜末帧的去向：链态由引擎下发（chain_next / chain_break），单镜生成同样吃这条
      // 判据——`--only` 只筛渲染对象，链邻居仍按成片顺序算，所以补单镜也照样衔接
      d.frame_chain
        ? (s.chain_next
            ? `首尾帧：本镜末帧 = 镜 ${s.chain_next} 的分镜图，画面朝那个构图收束（末帧不计费）`
            : `首尾帧：本镜不发末帧（${s.chain_break}），按纯首帧生成`)
        : (d.motion === "native"
            ? (s.chain_next
                ? `结对衔接：本镜末帧 = 镜 ${s.chain_next} 的分镜图（镜级 frame_chain 表态）`
                : `全能参考（缺省档）：分镜图+简笔板（有则附）+设定图全作参考图，`
                  + `一镜一片、不发首/末帧`)
            : null),
      s.clip ? `本镜已有片段——重生须先 \`review set --chapter ${d.project}/${d.id} --shots ${s.id} --stage clip --state retake\`（旧版归档可回滚）` : null,
      d.motion === "dubbed" ? "前置：dubbed 对口型需该镜配音在盘（缺则先 tts）" : null,
      // 音色锚定实况（native 全能参考自动附发）：数据与页面 chip/实发预览同源
      ((s.voice_anchor || {}).anchored || []).length > 0
        ? `音色锚定：${s.voice_anchor.anchored.map((a) => `${a.who}→@配音${a.no}`).join("、")}`
          + `（gen-video 自动随请求附发`
          + ((s.voice_anchor.loose || []).length
             ? `；未选角任模型自选嗓音：${s.voice_anchor.loose.join("、")}` : "")
          + ((s.voice_anchor.over || []).length
             ? `；参考位已满不附发：${s.voice_anchor.over.join("、")}` : "")
          + "）"
        : null,
      `① 先审后发：cd engine && python3 -m kinema gen-video --chapter ${d.project}/${d.id} --only ${s.id} --dry-run`,
      `② 把提示词与费用报给用户，确认后去掉 --dry-run 真发（绝不擅自烧钱，视频按秒计费）`,
      `③ 完成后本镜「▸ 动态片段」可播——请用户审看，通过后 review set --stage clip --state done`,
    ].filter(Boolean).join("\n");
    openDirectiveDialog({
      title: "视频指令", code: "SHOT · GEN VIDEO",
      ask: "在此写本镜视频要求",
      meta: `镜 ${s.id} · 项目 ${d.project} / 章节 ${d.id} · motion=${d.motion}`,
      directive: txt,
      hint: "例：这一镜镜头别晃，人物动作放慢；或：先只报价，别真发",
      note: "留空即按上面的纪律执行；写下的要求会填进末行「需求：」。"
        + "无论怎么写，指令都要求先 --dry-run 报价等你确认——视频按秒计费。"
        + "实发提示词可先在分镜卡「提示词 · PROMPTS」展开核对（与 dry-run 同源）。",
      done: `镜 ${s.id} 视频指令已复制——粘贴给 AI 即可`,
    });
  };

  // ⧉ 配音指令：单镜 tts 标准指令交 Claude Code——固定音色配音。
  // native 章节的混烧是显式 opt-in（assemble --burn-voice 或章节 native_voiceover）：
  // TTS 旁白上主轨、模型原生音轨只在旁白镜窗口降为背景床；对白镜恒由模型原生
  // 发声，旁白轨对它插静音、tts 也不为它合成。只想配部分镜就逐镜 --only。
  const copyGenAudio = (e) => {
    e.stopPropagation();
    // 台词判据认识 lines[]：多角色镜的台词在句里、narration 是空的——只读
    // narration 会把整镜台词报成空串（引擎侧同款判据是 voicecast.shot_text）
    const lineTxt = (s.lines || []).length
      ? s.lines.map((ln) => `${ln.speaker || "旁白"}：${ln.text}`).join(" ／ ")
      : s.narration;
    const txt = [
      `请为镜 ${s.id} 生成固定音色配音 · 项目 ${d.project} / 章节 ${d.id}（motion=${d.motion}）`,
      `台词（逐字合成）：${lineTxt}`,
      (s.lines || []).length > 1
        ? "多段台词逐句解析音色（句级 speaker → 章节 voices 表），逐句合成后拼成整镜 wav"
        : null,
      s.audio ? `本镜已有配音——改音色/情绪/文案后须 \`tts --only ${s.id} --force\` 重合成`
        + `（wav 已在盘时普通 tts 不进合成分支）` : null,
      d.motion === "native"
        ? "native 混烧要显式开：配音在盘后跑 `assemble --burn-voice`（要常开就写章节 "
          + "native_voiceover: true）才烧进成片，TTS 旁白上主轨、模型原生音轨只在旁白镜"
          + "窗口降为背景床；对白镜恒由模型原生发声，旁白轨对它插静音"
        : "配音在盘后走标准链：kenburns 作主音轨合成，dubbed 调用视频模型对口型",
      `① 音色确认：角色/旁白已锁音色直接用；未锁先 voice audition 试音五选一（候选已按角色性别过滤）`,
      `② 合成：cd engine && python3 -m kinema tts --chapter ${d.project}/${d.id} --only ${s.id}`,
      `③ 重跑 assemble（或 animatic）验听；情绪微调用 shots[].emotion（模版生成只吃这一条通道）`,
    ].filter(Boolean).join("\n");
    openDirectiveDialog({
      title: "配音指令", code: "SHOT · TTS",
      ask: "在此写配音要求",
      meta: `镜 ${s.id} · 项目 ${d.project} / 章节 ${d.id} · motion=${d.motion}`,
      directive: txt,
      hint: "例：这句要压低声音、句尾放慢；或：换一个更年轻的音色再试音",
      done: `镜 ${s.id} 配音指令已复制——粘贴给 AI 即可`,
    });
  };

  // ↻ 重新生成：置 retake → 后台跑 gen-image --only → 忙态入 GENJOBS
  // （渲染即出遮罩，轮询重绘/刷新页面都不丢）→ 完成软刷新换图
  const startRegen = async (e) => {
    e.stopPropagation();
    if (!(await uiConfirm(
      `重新生成镜 ${s.id} 的分镜图？旧版自动归档进版本栈可回滚（真实生图按张计费）。`,
      { title: `镜 ${s.id} · 重新生成` }))) return;
    try {
      const r = await post("/api/regen", { project: d.project, chapter: d.id, shot: s.id });
      trackJob(jobKey(d.project, d.id, s.id), r.job, "regen", d.project, d.id);
      await softRefresh(d.project, d.id);
    } catch (err) { toast(err.message, true); }
  };
  const ops = !s.omitted && h("div", { class: "shot-ops" },
    h("button", { class: "act-btn", disabled: busy ? "" : null,
      dataset: { tip: "↻ 重新生成\n按当前提示词整镜重出一张，镜上锚定意见自动编"
        + "译进重生要求；旧版自动归档进版本栈可回滚。真实生图按张计费。" },
      onclick: startRegen }, "↻ 重新生成"),
    h("button", { class: "act-btn", disabled: busy ? "" : null,
      dataset: { tip: "⇪ 素材直供\n不花生图费：把现成图直接登记为本镜画面——复用本章"
        + "其他镜画面或设定资产（同图换运镜＝多机位），也可上传产品图 / 实拍图 / 截图。"
        + "与 AI 生成同制度：旧版归档可回滚、登记后落待审。" },
      onclick: (e) => { e.stopPropagation(); openSupplyDialog(d, s); } }, "⇪ 素材直供"),
    h("button", { class: "act-btn" + (Array.isArray(s.refs) ? " ref-pinned" : ""),
      disabled: busy ? "" : null,
      dataset: { tip: "⛭ 垫图参考\n本镜生图默认套用项目参考库的风格垫图。点开可从参考库快捷"
        + "勾选/取消本镜要用哪些（无需重新上传）：默认全选启用项，取消后本镜只靠提示词，"
        + "可「保存并重新生成」立即按新选择重出。" },
      onclick: (e) => { e.stopPropagation(); openRefsDialog(d, s); } },
      "⛭ 垫图参考" + (Array.isArray(s.refs) ? `·${s.refs.length}` : "")),
    (s.image || s.image_prompt) && h("button", { class: "act-btn",
      dataset: { tip: "⧉ 改图指令\n一条指令同时改 image_prompt 与图片：先把要求增量融合进"
        + "现有提示词（保留画风与一致性锚点、只叠局部改动），再按新词重生本镜。"
        + "局部小修可直接点开画面框选改造；提示词与画面要一起换代才用本指令。" },
      onclick: copyImageDirective }, "⧉ 改图指令"),
    (s.video_prompt || s.camera) && h("button", { class: "act-btn",
      dataset: { tip: "⧉ 改镜指令\n只改运镜/动作文案（camera / video_prompt 中英同步），"
        + "不碰图片——打开指令台写要求，与带定位坐标的标准指令合并后复制交 AI。"
        + "画面要改走「⧉ 改图指令」。" },
      onclick: copyDirective }, "⧉ 改镜指令"),
    (s.narration || (s.lines || []).length > 0) && h("button", { class: "act-btn",
      dataset: { tip: "⧉ 配音指令\n单镜 tts 标准指令交 AI：固定音色配音（TTS 按字计费·"
        + "量级远低于视频）。多角色镜逐句换声自动解析；native 章节的配音要 "
        + "`assemble --burn-voice` 才混烧进成片，且只烧旁白镜。"
        + "可另附本次诉求一起复制。" },
      onclick: copyGenAudio }, "⧉ 配音指令"),
    d.uses_video && s.image && h("button", { class: "act-btn",
      dataset: { tip: "⧉ 视频指令\n带纪律的单镜 gen-video 标准指令交 AI：先 --only 本镜"
        + " dry-run 审提示词与报价，经你确认后才真发（视频按秒计费）；"
        + "指令自带本镜运动预演/片段/配音实况，可另附本次诉求一起复制。" },
      onclick: copyGenVideo }, "⧉ 视频指令"));

  return h("div", { class: "card shot-card" + (s.omitted ? " omitted" : ""),
                    id: `shot-${s.id}`,
                    style: `animation-delay:${Math.min(i * 30, 300)}ms` },
    visual,
    h("div", { class: "shot-main" },
      meta,
      // 台词：不论镜内写的是 lines[] 还是单段 narration，都逐句带说话人签。
      // 两种写法在引擎侧等价（voicecast.shot_lines 归一），卡上只有多段镜带签
      // 的话，同一章里两种镜读起来像两种东西
      (s.lines || []).length
        ? h("div", { class: "shot-narr shot-lines" }, (s.lines || []).map((ln) =>
            h("p", { class: "sl-row" },
              h("i", { class: "sl-who" }, speakerLabel(ln.speaker)),
              h("span", null, ln.text))))
        : (s.narration && h("div", { class: "shot-narr shot-lines" },
            h("p", { class: "sl-row" },
              h("i", { class: "sl-who" }, speakerLabel(s.speaker)),
              h("span", null, s.narration)))),
      s.caption && s.caption !== s.narration &&
        h("div", { class: "shot-cap" }, "字幕 · ", s.caption),
      noteRow,
      noteBox,
      candGrid(d, s),
      s.audio && media,
      prompts,
      ops));
}

/* 生成等待遮罩：转动光环 + 阶段文案（灯箱画布用；分镜卡的忙态遮罩
   由 shotCard 按 GENJOBS 渲染，数据驱动、重绘不丢） */
function genWait(host, label) {
  genWaitOff(host);
  host.append(h("div", { class: "gen-wait" },
    h("span", { class: "gw-ring" }),
    h("b", null, label || "生成中"),
    h("i", null, "模型出图约 15~60s · 完成自动替换")));
}
function genWaitOff(host) { host.querySelector(".gen-wait")?.remove(); }

/* —— 模块导出 —— */
export { KG_EMPTY_SVG, KG_KIND, KG_TYPE, LNDRAW, PANE_ICON, SB_OPEN, animaticCard, assetsCard,
         candGrid, chapterSignature, consStrip, consistencyCard, costCard, costStrip,
         finalCard, genWait, genWaitOff, kgGraph, kgKindKey, kgType, kvRow, lineageCanvas,
         openSketchFixDialog, openSubtitles, openTimestamps, prog, promptRow, screenCard,
         scriptCard, scriptItem,
         shotCard, storyboardTable, timelineStrip, trCatalog, trSounds, trackSketchJob,
         transitionCard, transitionDialog, transitionSlot, transitionThumb, verifyBtn,
         verifyStrip, viewChapter, voicesCard };
