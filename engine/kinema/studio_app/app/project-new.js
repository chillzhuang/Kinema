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

/* ═ Studio 前端模块 · app/project-new.js — 新建项目弹层（原生 ES Module·免构建）═ */

/* ═══════════ 新建项目弹层（磨砂 · 与 dlg-card 同族）═══════════
   只建**确定性空壳**：标题 + 画风（立项快照 style_prompt）+ 比例 + 字幕语言
   + 选填一句话卖点 / 主角。世界观 / 角色外貌 / 分镜等深度设定交给 AI——创建后落到
   项目页，即可导入剧本或复制指令让 Claude 补全。POST /api/project/create（与 CLI
   project new 同一条 Workspace.create_project 写路径）。 */
import { chip, openShell, uiSegment } from "./components.js";
import { emptyBlock, render } from "../app.js";
import { LABEL, MOTION, api, getOverview, h, post, state, toast } from "./core.js";
import { secHeader } from "./widgets.js";
import { renderRail } from "./shell.js";
import { projFilter, projectGrid } from "./overview.js";

function openNewProjectDialog(ov) {
  const profiles = ov.profiles || [];
  const pmap = {}; profiles.forEach((p) => (pmap[p.name] = p));
  const skillCat = ov.skills || [];
  // 默认选中画风区第一个 chip（首组首个存在的画风 = 动漫·现代新番），落在默认可见组内即高亮
  const firstProfile = ((skillCat[0] || {}).profiles || []).find((n) => pmap[n])
    || ov.default_profile || "narration";
  const state2 = { profile: firstProfile, aspect: "16:9", sub: "zh" };

  const titleInp = h("input", { class: "np-inp", type: "text", maxlength: "40",
    placeholder: "剑与魔法" });
  const idInp = h("input", { class: "np-inp np-inp-mono", type: "text",
    placeholder: "留空自动生成 · 英文 / 数字",
    oninput: (e) => { e.target.value = e.target.value.toLowerCase()
      .replace(/[^a-z0-9-]+/g, "-").replace(/^-+/, ""); } });
  const loglineInp = h("input", { class: "np-inp", type: "text",
    placeholder: "一句话说清讲什么、凭什么好看（可留空，让 AI 补）" });
  const charInp = h("input", { class: "np-inp", type: "text",
    placeholder: "主角名（可留空 · 外貌由 AI 补全）" });

  // 绑定 Skill 实时展示：画风确定性决定归属 skill，报项目名/编号 AI 即自动调用该 skill
  const skillBind = h("div", { class: "np-bind" });
  const curSkill = () => (pmap[state2.profile] || {}).skill || "kinema";
  const paintBind = () => {
    const sid = curSkill(); const m = LABEL.skill[sid] || {};
    skillBind.innerHTML = "";
    skillBind.append(
      h("span", { class: "np-bind-ic" }, "⌘"),
      h("div", { class: "np-bind-tx" },
        h("b", null, "绑定 Skill ", h("code", null, m.cmd || "/" + sid),
          m.label ? h("span", { class: "np-bind-zh" }, " · " + m.label) : null),
        h("p", null, "已随画风自动绑定 · 存入项目基础信息——建成后报项目名 / 编号，"
          + "AI 查 project 即知调此 skill，复制文案时无需再点名画风。")));
  };

  // 画风 chip 云——**按 skill 组织**（ov.skills 目录=单一真源）：组头显 /kn-xxx cmd
  // + 中文名 + 用途，一眼看清「选哪个画风 = 绑哪个 skill」。选中即高亮，悬停看画风前缀。
  const chipEls = {};
  const paintProf = () => { Object.entries(chipEls).forEach(
    ([n, el]) => el.classList.toggle("on", n === state2.profile)); paintBind(); };
  const mkChip = (p) => {
    const el = h("button", { class: "np-prof", type: "button",
      dataset: { tip: (p.style_prefix_full || p.style_prefix || p.label || p.name) },
      onclick: () => { state2.profile = p.name; paintProf(); } }, p.label || p.name);
    chipEls[p.name] = el; return el;
  };
  // 全部 skill 组共用同一版式（组头一行身份+用途 / 下方 chip 云），单画风 skill 只是
  // chip 云里恰好只有一个——刻意不给它另立紧凑单行变体：同一个弹层里两套视觉语言
  // 互相打断，统一版式优先于节省几行高度（多画风组本就靠「更多画风」折叠控高）
  const grpEl = (cmd, label, en, usage, items) => h("div", { class: "np-pgroup" },
    h("div", { class: "np-pglabel" },
      cmd ? h("code", { class: "np-pcmd" }, cmd) : null,
      h("b", null, label), h("span", null, en),
      usage ? h("small", { class: "np-pusage" }, usage) : null),
    h("div", { class: "np-pchips" }, items.map(mkChip)));
  // 先把全部 skill 分组建好（chip 注册进 chipEls，隐藏组也能高亮），再按「默认前 3 组 +
  // 更多展开」渲染——默认只露 动漫/写实3D/赛博朋克 三组，画风区紧凑不占高、常态无滚动条。
  const used = new Set();
  const groupEls = [];
  for (const s of skillCat) {
    const items = (s.profiles || []).map((n) => pmap[n]).filter(Boolean);
    if (!items.length) continue;
    items.forEach((p) => used.add(p.name));
    groupEls.push(grpEl(s.cmd, s.label, s.en, s.usage, items));
  }
  const rest = profiles.filter((p) => !used.has(p.name));
  if (rest.length) groupEls.push(grpEl(null, "独立画风", "UNGROUPED", "", rest));

  // 配置回退告警：引擎没读到 config/models.yaml（缺 PyYAML / 无文件）时用内置精简
  // 配置服务，画风目录是缩水子集（某些 skill 会只剩一个画风可选）。引擎侧的 ⚠
  // 只打在 stdout，网页不亮出来就无从解释画风列表为何缩水。总数取 skills 目录
  // （登记全集），在场数取下发的 profiles——两者都来自 overview，零硬编码。
  const cfg = ov.config || {};
  const profTotal = skillCat.reduce((a, s) => a + ((s.profiles || []).length), 0);
  const cfgWarn = !cfg.fallback ? null : h("div", { class: "np-cfgwarn" },
    h("b", null, `⚠ 画风目录不全（${profiles.length}/${profTotal}）`),
    h("span", null, cfg.fallback === "missing-pyyaml"
      ? "：引擎缺 PyYAML，正在用内置精简配置。对 AI 说「装好 PyYAML 依赖并重启 Studio」"
        + "即可恢复完整画风目录。"
      : "：未找到 config/models.yaml，正在用内置精简配置（仅内置画风可选）。"));

  const VISIBLE = 3;
  const profWrap = h("div", { class: "np-profiles" });
  groupEls.slice(0, VISIBLE).forEach((el) => profWrap.append(el));
  const extra = groupEls.slice(VISIBLE);
  if (extra.length) {
    const moreWrap = h("div", { class: "np-more-groups", hidden: true });
    extra.forEach((el) => moreWrap.append(el));
    const moreBtn = h("button", { class: "np-more", type: "button" });
    const setLabel = () => {
      moreBtn.innerHTML = "";
      moreBtn.append(
        h("span", null, moreWrap.hidden ? "更多画风" : "收起"),
        h("span", { class: "np-more-ic" }, moreWrap.hidden ? "⌄" : "⌃"));
    };
    moreBtn.onclick = () => { moreWrap.hidden = !moreWrap.hidden; setLabel(); };
    setLabel();
    profWrap.append(moreWrap, moreBtn);
  }
  paintProf();

  // 分段控件（比例 / 字幕语言）：走站内 uiSegment，样式与外壳形态开关同一份
  const seg = (key, opts) => {
    const el = uiSegment(opts, { value: state2[key] });
    el.addEventListener("change", () => { state2[key] = el.value; });
    return el;
  };
  const field = (label, ctrl, hint) => h("div", { class: "np-field" },
    h("label", { class: "np-label" }, label,
      hint ? h("small", null, hint) : null), ctrl);

  const err = h("div", { class: "np-err", hidden: true });
  const submit = async () => {
    const title = titleInp.value.trim();
    if (!title) {
      err.textContent = "请先填写项目标题"; err.hidden = false;
      titleInp.classList.add("np-shake"); titleInp.focus();
      setTimeout(() => titleInp.classList.remove("np-shake"), 420);
      return;
    }
    okBtn.disabled = true; okBtn.textContent = "创建中…"; err.hidden = true;
    try {
      const r = await post("/api/project/create", {
        title, id: idInp.value.trim() || null,
        profile: state2.profile, skill: curSkill(), aspect: state2.aspect,
        subtitle_lang: state2.sub,
        logline: loglineInp.value.trim() || null,
        character: charInp.value.trim() || null });
      close();
      toast(`✓ 项目「${title}」已创建——进入项目页导入剧本或让 AI 补全设定`);
      await getOverview(true); renderRail(state.overview);
      location.hash = `#/project/${encodeURIComponent(r.project)}`;
    } catch (e2) {
      err.textContent = e2.message; err.hidden = false;
      okBtn.disabled = false; okBtn.textContent = "创建项目";
    }
  };
  const okBtn = h("button", { class: "dlg-btn primary", onclick: submit }, "创建项目");
  const close = openShell({ card: "np-card",
    keys: (e) => {
      if (e.key === "Enter" && e.target === titleInp) { e.preventDefault(); submit(); }
    },
    build: (close) => [
      h("div", { class: "np-hd" },
        h("span", { class: "k" }, "新建项目"),
        h("small", null, "只建空壳 · 世界观 / 角色 / 分镜等深度设定交给 AI")),
      h("div", { class: "np-body" },
        cfgWarn,
        h("div", { class: "np-row" },
          field("项目标题", titleInp),
          field("项目编号", idInp, "选填 · 网址与目录名")),
        field("画风", profWrap, "立项即快照画风前缀，锁定全片风格"),
        skillBind,
        h("div", { class: "np-row" },
          field("画面比例", seg("aspect",
            [["16:9", "16:9 横屏"], ["9:16", "9:16 竖屏"], ["1:1", "1:1 方形"]])),
          field("字幕语言", seg("sub",
            [["zh", "中文"], ["en", "英文"], ["both", "中英双语"]]))),
        h("div", { class: "np-row" },
          field("一句话卖点", loglineInp, "选填"),
          field("主角", charInp, "选填 · 建一个角色"))),
      h("p", { class: "np-foot" },
        "创建后进入项目页——可上传小说 / 剧本入库，或复制指令让 AI 补全设定、写第一集。"),
      err,
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "取消"), okBtn)] });
  setTimeout(() => titleInp.focus(), 40);
}

// 「＋ 新建项目」主按钮（项目页头部 / 空态共用）
const newProjBtn = (ov) => h("button", { class: "np-open",
    dataset: { tip: "在网页里直接开一个新项目——建空壳后进项目页导入剧本 / 让 AI 补全设定" },
    onclick: () => openNewProjectDialog(ov) },
  h("span", { class: "np-open-ic" }, "＋"), "新建项目");

// 无项目空态：网页新建引导（底行给一句可直接说给 AI 的立项指令做兜底）
function npEmpty(ov) {
  return h("div", { class: "np-empty" },
    h("div", { class: "np-empty-ic" }, "✦"),
    h("h3", null, "还没有项目"),
    h("p", null, "点下面开一个新项目——只填标题和画风即可建好空壳，",
      h("br"), "进项目页导入剧本，或复制指令让 AI 补全设定、写第一集。"),
    newProjBtn(ov),
    h("pre", { class: "np-empty-say" },
      "也可对 AI 说：帮我立项《我的系列》——选好画风、配好主角，写出第一集"));
}

async function viewProjects(view) {
  const ov = await getOverview();
  const projects = ov.projects || [];
  view.append(secHeader("01", "全部项目", "ALL PROJECTS", projects.length));

  // 工具栏：左＝「＋ 新建项目」主行动，右＝检索框 + A/B/C 运动模式筛选（靠右对齐成一组）
  const bar = h("div", { class: "filter-bar" });
  const gridWrap = h("div");
  const fchip = (label, val, tip) => h("button", {
    class: "fchip", dataset: tip ? { val: val || "", tip } : { val: val || "" },
    onclick: () => { projFilter.motion = projFilter.motion === val ? null : val; apply(); } }, label);
  const apply = () => {
    const kw = (projFilter.kw || "").toLowerCase();
    const hay = (p) => [p.title, p.id, p.logline, p.theme, p.profile, LABEL.profile[p.profile],
                        p.skill, (LABEL.skill[p.skill] || {}).cmd]
      .filter(Boolean).join(" ").toLowerCase();
    const list = projects.filter((p) =>
      (!projFilter.motion || (p.motions || []).includes(projFilter.motion)) &&
      (!kw || hay(p).includes(kw)));
    gridWrap.innerHTML = "";
    gridWrap.append(
      !projects.length ? npEmpty(ov)                           // 无项目：网页新建引导
      : list.length ? projectGrid(list)                        // 有匹配
      : emptyBlock("没有匹配的项目", "换个关键词或筛选条件试试。", null));
    bar.querySelectorAll(".fchip").forEach((b) =>
      b.classList.toggle("active", (b.dataset.val || null) === projFilter.motion));
  };
  if (projects.length) {
    // 全部靠左紧贴：新建项目 + 检索框 + A/B/C 渲染模式筛选（各 chip 带说明 tip）
    bar.append(newProjBtn(ov));
    bar.append(h("input", { class: "fsearch", type: "search",
      placeholder: "检索项目（标题 / 编号 / 简介 / 画风 / Skill）…", value: projFilter.kw || "",
      oninput: (e) => { projFilter.kw = e.target.value.trim(); apply(); } }));
    bar.append(fchip("全部", null, "不限渲染模式\n显示全部项目（A/B/C/D 四种运动模式都要）。"));
    bar.append(h("span", { class: "fsep" }));
    Object.entries(MOTION).forEach(([m, info]) =>
      bar.append(fchip(`${info.key} · ${info.name}`, m, info.tip)));
    view.append(bar);
  }
  view.append(gridWrap);
  apply();

  // 回收站：已逻辑删除的项目（数据完整保留）——查看 / 一键恢复
  const rec = ov.recycle || [];
  if (rec.length) {
    view.append(secHeader("RB", "回收站", "RECYCLE BIN", rec.length));
    view.append(h("div", { class: "card recycle-list" }, rec.map((r) =>
      h("div", { class: "recycle-row" },
        h("b", null, r.title),
        h("code", null, r.id),
        h("span", { class: "rc-meta" },
          `${r.chapters} 章 · 删除于 ${(r.deleted_at || "—").slice(0, 16).replace("T", " ")}`),
        h("a", { class: "act-btn", href: `#/project/${encodeURIComponent(r.id)}`,
          dataset: { tip: "查看项目详情（数据完整保留，只读浏览）" } }, "查看"),
        h("button", { class: "act-btn ok",
          dataset: { tip: "恢复项目\n清除删除标记，立即回到全部清单与流程。" },
          onclick: async () => {
            try {
              await post("/api/project/restore", { project: r.id });
              toast(`项目「${r.title}」已恢复`);
              await getOverview(true); renderRail(state.overview); render();
            } catch (err) { toast(err.message, true); }
          } }, "↺ 恢复")))));
  }
}

/* 软删项目只读横幅（项目详情 / 章节制作台共用）：置顶提示 + 一键恢复（恢复钮 ro-keep 不被灰化） */
function deletedBanner(pid, title, deletedAt) {
  return h("div", { class: "recycle-banner" },
    h("b", null, "此项目已移至回收站"),
    h("span", null,
      `删除于 ${(deletedAt || "—").slice(0, 16).replace("T", " ")} · `
      + "数据、产物与库行完整保留（只读浏览）；恢复后即回到全部清单与流程。"),
    h("button", { class: "act-btn ok ro-keep", onclick: async () => {
      try {
        await post("/api/project/restore", { project: pid });
        toast(`项目「${title}」已恢复`);
        await getOverview(true); renderRail(state.overview); render();
      } catch (err) { toast(err.message, true); }
    } }, "↺ 恢复项目"));
}

/* —— 模块导出 —— */
export { deletedBanner, newProjBtn, npEmpty, openNewProjectDialog, viewProjects };
