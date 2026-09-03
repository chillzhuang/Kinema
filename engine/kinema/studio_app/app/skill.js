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

/* ═ Studio 前端模块 · app/skill.js — SKILL 指挥层大屏（原生 ES Module·免构建）═
   只读大屏：/api/overview 的 skill_board（编译 catalog 经 skills.py 投影）按 kind
   分组渲染全集群——流程真源、画风入口、专项能力与系统件，条目与字段零硬编码。
   画风档点击复用总览的画风详情弹窗看提示词 DNA。编辑不在网页：真源在 agent/
   源码 + 确定性编译，页尾「自定义与增强」给出正确路径与命令。 */
import { emptyBlock } from "../app.js";
import { chip } from "./components.js";
import { getOverview, h, toast } from "./core.js";
import { secHeader, titledChip } from "./widgets.js";
import { PROFILE_NOTES, openStyleModal } from "./overview.js";

/* kind → 分组门牌（展示层语序与中文名；条目全部来自 skill_board，编译目录出现
   新 kind 时自动落「其他」兜底组——门牌没登记只影响措辞，绝不丢条目） */
const KINDS = [
  ["workflow", "流程真源", "WORKFLOW",
   "集群共用的「主题 → 成片」工作流——生图 / 剧本 / 视频的提示词纪律都在它的 references，全部入口依赖它"],
  ["route", "画风入口", "ROUTE",
   "题材直达入口：对 AI 说斜杠指令即启动整套流程；旗下画风档点击看提示词 DNA"],
  ["overlay", "叠加玩法", "OVERLAY", "叠在既有入口之上的玩法变体，不另立画风"],
  ["capability", "专项能力", "CAPABILITY", "供各入口按需调用的单点能力"],
  ["project", "项目与长篇", "PROJECT", "系列规划、多集管理与长篇创作"],
  ["system", "系统", "SYSTEM", "环境配置与诊断，不生产内容"],
  ["scaffold", "脚手架", "SCAFFOLD", "已立项未完工的能力骨架"],
];
const VO = {
  lead: "解说驱动——旁白铺满叙事主线",
  sparse: "剧情驱动——旁白只在必要处点睛",
  none: "氛围驱动——默认无人声叙述",
};

function skillCard(s, ov, pmap) {
  const profRow = !(s.profiles || []).length ? null : h("div", { class: "sk-profs" },
    h("span", { class: "sk-profk" }, `画风 ${s.profiles.length}`),
    s.profiles.map((name) => {
      const p = pmap[name];
      const isDef = s.profiles.length > 1 && name === s.default_profile;
      if (!p) return h("code", { class: "sk-prof off",
        dataset: { tip: "画风档未在当前配置加载（多半处于内置精简配置回退）" } }, name);
      return h("code", { class: "sk-prof" + (isDef ? " def" : ""),
        dataset: { tip: (p.label && p.label !== name ? p.label + " · " : "")
          + "点击看画风档详情与提示词 DNA" },
        onclick: () => openStyleModal(ov, p, { cmd: s.cmd, zh: s.label || s.id },
          s.cmd, isDef, PROFILE_NOTES[name] || null) },
        p.label || name);
    }));
  return h("div", { class: "card sk-card" },
    h("div", { class: "sk-head" },
      h("code", { class: "sk-cmd" }, s.cmd),
      s.label && h("b", null, s.label),
      s.en && h("span", { class: "sk-en" }, s.en),
      s.status !== "stable" && chip(s.status === "scaffold" ? "规划中" : s.status, "amber")),
    h("p", { class: "sk-desc" }, s.description),
    profRow,
    h("div", { class: "sk-meta" },
      s.voiceover && titledChip(`旁白 ${s.voiceover}`, null, VO[s.voiceover] || ""),
      (s.depends_on || []).length ? titledChip(
        "依赖 " + s.depends_on.map((d) => "/" + d).join(" · "), null,
        "复用被依赖 skill 的流程与纪律——那边的增强自动传导到这里") : null,
      h("code", { class: "sk-src", dataset: {
        tip: "可编辑源码位置——编译进 .claude/skills/ 供宿主发现；生成物手改会被 check 判红" } },
        "agent/" + s.source)));
}

async function viewSkill(view) {
  const ov = await getOverview();
  const board = ov.skill_board || {};
  const skills = board.skills || [];
  view.append(secHeader("01", "SKILL 指挥层", "AGENT SKILLS", skills.length || null));
  if (!skills.length) {
    view.append(emptyBlock("指挥层目录还没送到",
      "服务端没有下发 skill_board——多半是 Studio 还在跑旧进程，重启后刷新即可。",
      "对 AI 说：重启 Kinema Studio 控制台"));
    return;
  }
  view.append(h("p", { class: "gd-lead" },
    "AI 指挥层的能力单元总览：每个 skill 是一份编译进宿主发现目录的作业指导书——",
    "对 AI 说斜杠指令或直接说需求即可调用。本页只读，条目与字段全部来自编译 catalog；",
    "真源在 agent/ 源码，怎么增强见页尾。"));
  view.append(h("p", { class: "sk-catline" },
    h("code", null, `catalog ${board.catalog_version || "—"}`),
    board.manifest_digest && h("code", { dataset: { tip: board.manifest_digest } },
      board.manifest_digest.slice(0, 20) + "…"),
    h("code", null, `${skills.length} skills`),
    h("code", null, `${(ov.profiles || []).length} 画风`)));

  const pmap = {};
  (ov.profiles || []).forEach((p) => (pmap[p.name] = p));
  const known = new Set(KINDS.map((k) => k[0]));
  const byKind = {};
  for (const s of skills) {
    const k = known.has(s.kind) ? s.kind : "_other";
    (byKind[k] = byKind[k] || []).push(s);
  }
  const wrap = h("div", { class: "sp-groups" });
  const groups = [...KINDS,
    ["_other", "其他", "OTHER", "编译目录新增而本页门牌未登记的类型（兜底显示）"]];
  for (const [kind, zh, en, usage] of groups) {
    const items = byKind[kind];
    if (!items?.length) continue;
    wrap.append(h("div", { class: "card sp-group" },
      h("div", { class: "sp-ghead" },
        h("b", { class: "sp-gzh" }, zh),
        h("span", { class: "sp-gen" }, en),
        items.length > 1 && h("span", { class: "sp-gcnt" }, `${items.length} skill`),
        h("span", { class: "sp-gusage" }, usage)),
      h("div", { class: "sk-grid" }, items.map((s) => skillCard(s, ov, pmap)))));
  }
  view.append(wrap);

  // 02 自定义与增强：编辑入口不在网页——指路单一真源与确定性编译，口径与
  // AGENTS.md / DEVELOP.md 二开配方对齐（与 playbook 的命令速查同一维护约定）。
  // 版式与 01 同构：两块内容各自装进 .sp-group 门牌面板，组间距由 .sp-groups 统一给
  const copyBtn = (text) => h("button", { class: "cmt-act",
    dataset: { tip: "复制到剪贴板" },
    onclick: async (e) => { e.stopPropagation();
      try { await navigator.clipboard.writeText(text); toast("已复制——粘贴给 AI 或终端"); }
      catch { toast("复制失败：浏览器未授权剪贴板", true); } } }, "⧉");
  const say = (text, desc) => h("div", { class: "gd-say" },
    h("code", null, text), desc && h("span", null, desc), copyBtn(text));
  const cli = (cmd, desc) => h("div", { class: "gd-cmd" },
    h("code", null, cmd), desc && h("span", null, desc), copyBtn(cmd));
  // 门牌面板骨架：与上方各 kind 组同一个 .sp-ghead 语序（中文名 · EN · 计数 · 用途句）
  const panel = (zh, en, cnt, usage, body) => h("div", { class: "card sp-group" },
    h("div", { class: "sp-ghead" },
      h("b", { class: "sp-gzh" }, zh),
      h("span", { class: "sp-gen" }, en),
      cnt && h("span", { class: "sp-gcnt" }, cnt),
      h("span", { class: "sp-gusage" }, usage)),
    body);
  const extCard = (title, ...body) => h("div", { class: "sk-card" },
    h("div", { class: "sk-head" }, h("b", null, title)),
    h("p", { class: "sk-desc" }, ...body));
  view.append(secHeader("02", "自定义与增强", "EXTEND"));
  view.append(h("div", { class: "sp-groups" },
    panel("增强路径", "EXTEND PATHS", "3 条",
      "改哪里能让全集群生效——正文纪律、画风档与 skill 三条口子，都在 agent/ 源码里",
      h("div", { class: "sk-grid" },
        extCard("✎ 增强集群纪律",
          "生图提示词契约、分镜方法、视频运动纪律与文案笔法都在 ",
          h("code", null, ".claude/skills/kinema/references/"),
          " ——全部画风入口都依赖 /kinema，改一处全集群生效。想让所有片子换个章法，从这里下手。"),
        extCard("✦ 加画风档",
          h("code", null, "config/models.yaml"),
          " 的 profiles 段登记双语风格前缀与节奏，再到 ",
          h("code", null, "agent/manifest.json"),
          " 挂到入口 skill；编译后新档自动出现在总览「风格档」与建项目分组，网页零改动。"),
        extCard("⌘ 新增 / 改造 skill",
          h("code", null, ".claude/skills/<名>/"),
          " 建 SKILL.md 正文包（单源无拷贝），manifest 登记 kind、触发、依赖、权限与画风；",
          "长表沉 references/。编译后本页与运行时 catalog 自动更新。"))),
    panel("落地命令", "COMMANDS", null,
      "改完正文或元数据这样落地（仓库根目录运行）——点 ⧉ 复制，或整句说给 AI",
      h("div", { class: "sk-cmds" },
        cli("python3 tools/agent_assets.py compile", "规范化 frontmatter/skill.json 并产运行时 catalog"),
        cli("python3 tools/agent_assets.py check", "漂移检查：改了正文或 manifest 没编译会判红"),
        cli("cd engine && python3 -m kinema agent doctor --json", "控制平面体检：目录 / 契约 / 预算 / 宿主"),
        say("帮我增强生图纪律：以后每镜提示词都必须写明光源方向与景深层次",
          "或者整句说给 AI——它替你改源码、编译并回归"),
        h("p", { class: "gd-note" },
          "Skill 正文在 .claude/skills/ 原地编辑；名称、描述与权限等元数据只改 agent/manifest.json，",
          "frontmatter、skill.json、运行时 catalog 与 INDEX.md 由编译器维护，改后必须重新 compile。")))));
}

export { KINDS, VO, skillCard, viewSkill };
