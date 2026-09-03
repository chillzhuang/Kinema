/*
  This file is part of Kinema.
  Copyright (C) 2018-2099 BladeX (https://bladex.cn)

  SPDX-License-Identifier: AGPL-3.0-or-later

  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU Affero General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU Affero General Public License for more details.

  You should have received a copy of the GNU Affero General Public License
  along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

/* ============================================================================
   控制台 UI 构件 —— 三栏骨架 / 大纲 / 检查器 / 时间轴

   两条硬约束，都来自"控制台必须像 Studio 的一部分，而不是嵌进来的另一个软件"：

   **① 只用站内组件**：下拉走 `uiSelect`、勾选走 `uiCheck`、档位选择走 `.efx-opt`
   药丸组、检索走 `listSearch`、弹层走 `.dlg` 骨架。**不用任何原生 `<select>` /
   `<input type=checkbox|range>`**——原生控件在深色主题下由操作系统绘制，配色、圆角、
   字体全都对不上，与主界面制式不一致。

   **② 信息密度按需展开，不一次性摊平**（对标 Blender / Unreal Sequencer / Spline
   的共同做法）：场景树按类型折叠成组，资产库与运镜库各收进一个带检索的选择器弹层。
   把它们全平铺在 260px 宽的侧栏里，结果就是三栏各自拥挤，而作为工作主区的
   3D 视口空间反而被侧栏挤压。
   ========================================================================== */

import { listSearch, openShell, uiCheck, uiSelect } from "../app/components.js";
import { h, tipHide } from "../app/core.js";

import { ADVANCED_BUDGET, TIER_CLR, groupPresets, tierNotes } from "./cameras.js";
import { actionThumb, modelThumb, propThumb } from "./preview.js";
import { fmtT } from "./timeline.js";

/* ---------------------------------------------------------------- 基础构件 */
/** 小节标题（等宽微标签＝全站的「控制台语言」）。 */
export const sec = (title, ...extra) =>
  h("div", { class: "dz-sec" }, h("span", { class: "k" }, title), ...extra);

/** 分组卡：检查器的一组相关控件收进一张软卡——顶级工具面板的通用式样，
 *  比一串裸行多出「这几项是一件事」的结构感。null/false 子项自动剔除。 */
export const card = (...children) =>
  h("div", { class: "dz-card" }, ...children.filter(Boolean));

/** 一行属性：左标签右控件。 */
export const row = (label, ...ctrl) =>
  h("div", { class: "dz-row" }, h("span", null, label), h("div", null, ...ctrl));

export const chip = (text, tone) =>
  h("span", { class: "dz-chip" + (tone ? ` ${tone}` : "") }, text);

/** 站内下拉（uiSelect）：统一 change 回调签名。 */
function sel(options, value, onchange, cls = "") {
  const s = uiSelect(options, { value });
  if (cls) s.classList.add(cls);
  s.addEventListener("change", () => onchange(s.value));
  return s;
}

/** 站内勾选（uiCheck）。 */
function check(on, onchange) {
  const c = uiCheck();
  c.checked = !!on;
  c.addEventListener("change", () => onchange(c.checked));
  return c;
}

/** 档位药丸组（复用特效选择器的 `.efx-opt` 语汇）——替代原生 range/number。 */
function pills(options, value, onpick) {
  const box = h("div", { class: "dz-pills" });
  options.forEach((o) => box.append(h("button", {
    class: "efx-opt" + (String(o.value) === String(value) ? " on" : ""),
    type: "button", dataset: o.tip ? { tip: o.tip } : {},
    onclick: () => onpick(o.value),
  }, o.label)));
  return box;
}

/** 文本输入（站内 `.cmt-input` 语汇）。 */
function textInput(value, { placeholder = "", width = null, onchange }) {
  const i = h("input", { class: "cmt-input dz-in", type: "text",
    value: value ?? "", placeholder });
  if (width) i.style.width = width;
  i.addEventListener("change", () => onchange(i.value));
  return i;
}

/** AI 火花图标（内联 SVG，随按钮文字色）。 */
function aiIcon() {
  const i = h("i", { class: "dz-svgi" });
  i.innerHTML = '<svg viewBox="0 0 24 24">'
    + '<path d="M12 3.5l1.8 4.4 4.4 1.8-4.4 1.8L12 15.9l-1.8-4.4-4.4-1.8 4.4-1.8z"/>'
    + '<path d="M18.6 14.6l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>'
    + '<path d="M5.6 15.8l.6 1.5 1.5.6-1.5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6z"/></svg>';
  return i;
}

/* ------------------------------------------------------------------ 三栏骨架 */
export function buildShell() {
  const outliner = h("aside", { class: "dz-outliner" });
  const viewport = h("div", { class: "dz-viewport" });
  const inspector = h("aside", { class: "dz-inspector" });
  const timeline = h("div", { class: "dz-timeline" });
  const main = h("div", { class: "dz-main" }, outliner, viewport, inspector);
  // 忙态蒙版：渲完一镜的帧之后还要等引擎编码+登记（实测约 0.7s）。这段里画面会从
  // 洁净渲染切回带 gizmo 的编辑视图、而时间轴是暂停的，看上去就是「突然跳回初始
  // 画面并卡住」——毛玻璃蒙版把这段切换整个盖住，并明说正在做什么。
  const busy = h("div", { class: "dz-busy", hidden: true },
    h("div", { class: "dz-busy-card" },
      h("div", { class: "dz-busy-ring" }),
      h("b", { class: "dz-busy-title" }, ""),
      h("em", { class: "dz-busy-sub" }, "")));
  const root = h("div", { class: "dz-stage" }, main, timeline, busy);
  return { root, main, outliner, viewport, inspector, timeline, busy };
}

/** 忙态蒙版开关：`showBusy(busy, null)` 关闭，给 {title, sub} 则开启。
 *
 *  淡入淡出都必须**短**（CSS 里 .13s）：这段等待实测才 ~0.7s，按常规 .22s 淡入
 *  的话截到的那一帧 opacity 还是 0——蒙版几乎没真正现身，白留一次闪烁。
 *  淡出走「先摘 .on、动画走完再 hidden」，并用元素上的定时器句柄防竞态：
 *  连着 hide→show 时若不取消上一次的收尾定时器，它会在新一次显示后把 hidden
 *  置回 true，表现成「蒙版偶尔不出现」。 */
export function showBusy(busy, info) {
  if (!busy) return;
  clearTimeout(busy._t);
  if (!info) {
    if (busy.hidden) return;
    busy.classList.remove("on");
    busy._t = setTimeout(() => { busy.hidden = true; }, 140);
    return;
  }
  busy.querySelector(".dz-busy-title").textContent = info.title || "处理中…";
  busy.querySelector(".dz-busy-sub").textContent = info.sub || "";
  if (busy.hidden) {
    busy.hidden = false;
    // 先上 DOM 再加类，过渡才有起点（同帧 hidden→显示+终态 = 没有淡入）
    requestAnimationFrame(() => busy.classList.add("on"));
  } else {
    busy.classList.add("on");     // 已在显示中：只换文案，别重放一遍淡入
  }
}

/* -------------------------------------------------------------------- 大纲栏 */
/** 可折叠分组：组头带计数，点整行开合。 */
function foldGroup(ctx, key, icon, title, items) {
  const open = ctx.folds[key] !== false;
  const head = h("button", { class: "dz-fold" + (open ? " open" : ""), type: "button",
    onclick: () => ctx.toggleFold(key) },
    h("i", { class: "dz-fold-caret" }, "›"),
    h("span", { class: "dz-fold-ico" }, icon),
    h("b", null, title),
    h("em", null, String(items.length)));
  const body = h("div", { class: "dz-foldbody" }, ...items);
  return h("div", { class: "dz-group" + (open ? "" : " closed") }, head, body);
}

export function renderOutliner(el, ctx) {
  el.innerHTML = "";
  const node = (icon, label, meta, active, onclick, onremove) =>
    h("div", { class: "dz-node" + (active ? " on" : ""), onclick },
      h("i", null, icon), h("b", null, label),
      meta ? h("em", null, meta) : null,
      onremove ? h("button", {
        class: "dz-x", type: "button", dataset: { tip: "移除" },
        onclick: (e) => { e.stopPropagation(); onremove(); },
      }, "✕") : null);

  // 工具条固定在栏顶（不随树滚动）：场景树一长，「添加」会被卷出视野，须保持恒可达
  el.append(h("div", { class: "dz-obar" },
    h("button", { class: "dz-add-btn", type: "button",
      dataset: { tip: "添加到舞台\n角色体型与按场景族分组的道具体块，每格带缩略图、可搜索；"
        + "选一项后在地面点一下落位。" },
      onclick: () => openAssetPicker(ctx) },
      h("i", null, "＋"), "添加"),
    h("button", { class: "dz-icobtn", type: "button", dataset: { tip: "全部折叠 / 展开" },
      onclick: () => ctx.toggleFoldAll() }, ctx.allFolded ? "⊞" : "⊟")));

  const tree = h("div", { class: "dz-otree" });
  el.append(tree);
  if (!ctx.cameras.length && !ctx.actors.length && !ctx.props.length) {
    tree.append(h("div", { class: "dz-empty" },
      "空舞台。点「＋ 添加」选一项，再在地面上点一下即可落位。"));
    return;
  }
  // 机位组**按分镜列出**（机位与镜头块 1:1 从属，不可增删——每镜恰好一台）：
  // 点击=跳到该镜并选中机位，画布里机身与轨迹当场现身，按住机身可绕主体转方位
  tree.append(foldGroup(ctx, "cam", "◉", "机位", ctx.cuts.map((cu) => {
    const cam = ctx.cameraById(cu.camera);
    const el2 = node("◉", `镜 ${cu.shot}`, cam ? ctx.presetLabel(cam.preset) : "—",
      ctx.selected?.id === cu.camera,
      () => ctx.pickFromList(cam), null);
    el2.dataset.shot = cu.shot;   // 播放头进到这一镜时由 frame() 点亮 .now
    return el2;
  })));
  tree.append(foldGroup(ctx, "actor", "☗", "角色", ctx.actors.map((a) => node(
    "☗", a.name, ctx.actionLabel(a.segmentAt(0).action), ctx.selected?.id === a.id,
    () => ctx.pickFromList(a), () => ctx.remove(a)))));
  tree.append(foldGroup(ctx, "prop", "▤", "道具", ctx.props.map((p) => node(
    "▤", p.name, null, ctx.selected?.id === p.id,
    () => ctx.pickFromList(p), () => ctx.remove(p)))));
}

/**
 * 资产选择器：角色体型 ＋ 按场景族分组的道具体块，点一项即进入「落位」态。
 *
 * 体块的形状（拱门 / 城门 / 集装箱）用两个字读不出来，而形状决定画面里的遮挡与
 * 视差关系，故每格给缩略图；人偶格共用一套取景，身高差因此可比。
 *
 * 机位刻意不在这里：机位与镜头块 1:1 从属（每镜恰好一台，随镜头块生灭），
 * 「新建机位」只会造出游离机位——大纲按分镜列出即是全部机位。
 */
function openAssetPicker(ctx) {
  tipHide();                     // 触发按钮自己的悬浮提示会盖在弹层之上
  const search = listSearch(".dz-actcell", "搜资产名 / 场景 / 用途…");
  const groups = ctx.dir.prop_groups || [];
  const props = ctx.dir.props || [];

  openShell({
    card: "dz-pkcard",
    build: (close) => {
      const pick = (kind, key) => { close(); ctx.setPlacing({ kind, key }); };
      const body = h("div", { class: "dz-pkbody" });
      const cell = (thumb, label, key, desc, meta, onclick) => h("button", {
        class: "dz-actcell", type: "button", onclick,
      },
        thumb,
        h("div", { class: "dz-actinfo" },
          h("div", { class: "dz-actname" }, h("b", null, label), h("em", null, key)),
          meta ? h("div", { class: "dz-actmeta" }, ...meta) : null,
          h("span", { class: "dz-preset-d" }, desc)));

      const head = (label, count, note) => h("div", { class: "dz-pkgroup" },
        h("span", { class: "k" }, label), h("em", null, String(count)),
        note ? h("i", { class: "dz-pkgnote" }, note) : null);

      body.append(head("角色", (ctx.dir.models || []).length, "灰模替身，身高即尺度参照"));
      const mg = h("div", { class: "dz-actgrid" });
      (ctx.dir.models || []).forEach((m) => mg.append(cell(
        modelThumb(m.key, { size: 84 }), m.label, m.key, m.desc,
        [chip(`${m.height} m`)], () => pick("actor", m.key))));
      body.append(mg);

      // 按场景族分组：目录里没登记分组的道具兜到最后一段，绝不静默丢掉
      const seen = new Set();
      for (const g of groups) {
        const items = props.filter((p) => p.group === g.key);
        if (!items.length) continue;
        items.forEach((p) => seen.add(p.key));
        body.append(head(g.label, items.length, g.desc));
        const grid = h("div", { class: "dz-actgrid" });
        items.forEach((p) => grid.append(cell(
          propThumb(p.key, { size: 84 }), p.label, p.key, p.desc,
          [chip(`${p.size[0]}×${p.size[1]}×${p.size[2]} m`)], () => pick("prop", p.key))));
        body.append(grid);
      }
      const rest = props.filter((p) => !seen.has(p.key));
      if (rest.length) {
        body.append(head("其他", rest.length, "未登记场景族"));
        const grid = h("div", { class: "dz-actgrid" });
        rest.forEach((p) => grid.append(cell(
          propThumb(p.key, { size: 84 }), p.label, p.key, p.desc,
          [chip(`${p.size[0]}×${p.size[1]}×${p.size[2]} m`)], () => pick("prop", p.key))));
        body.append(grid);
      }

      return [
        h("div", { class: "dz-pkhead" },
          h("span", { class: "k" }, "添加到舞台"),
          h("em", { class: "dz-hint mono" }, `${props.length} 件体块`),
          search,
          h("button", { class: "dz-icobtn", type: "button", onclick: close }, "✕")),
        body,
        h("p", { class: "dz-pkfoot" },
          "选一项后在地面点一下即可落位，按 Esc 取消落位。体块只负责占位、挡镜、"
          + "给运镜提供视差与遮挡关系——材质与细节交给生图阶段，previz 不做。"),
      ];
    },
  });
  setTimeout(() => search.focus(), 40);
}

/* ------------------------------------------------------------------ 检查器栏 */
export function renderInspector(el, ctx) {
  el.innerHTML = "";
  const s = ctx.selected;
  if (!s) return renderSceneInspector(el, ctx);
  if (s.kind === "camera") return renderCameraInspector(el, ctx, s);
  if (s.kind === "prop") return renderPropInspector(el, ctx, s);
  return renderActorInspector(el, ctx, s);
}

function renderSceneInspector(el, ctx) {
  el.append(card(
    sec("本场设置", h("em", { class: "dz-hint" }, `${ctx.cuts.length} 镜`)),
    row("帧率", pills([
      { value: 12, label: "12", tip: "省算力\n预演只看走位与运镜时够用，导出更快。" },
      { value: 24, label: "24", tip: "与 Seedance 一致\n默认；previz 与最终片长逐帧对得上。" },
      { value: 30, label: "30", tip: "更顺滑\n只影响预演观感，Seedance 仍按 24fps 出片。" },
    ], ctx.fps, (v) => ctx.setFps(+v))),
    row("渲染尺寸",
      h("span", { class: "dz-ro mono" }, `${ctx.canvas[0]}×${ctx.canvas[1]}`)),
    h("p", { class: "dz-note" },
      "previz 与 Seedance 目标帧同分辨率、同时长——这是「成片跟随预演」能逐帧对照的前提。")));

  el.append(card(
    sec("交给 Seedance"),
    h("div", { class: "dz-row dz-switch" },
      h("span", null, "参考视频 V2V"),
      h("div", null, check(ctx.v2v, (on) => ctx.setV2V(on)))),
    h("p", { class: "dz-note" },
      "开启后每镜会把 previz 作参考视频发给 Seedance，迁移运镜、走位与动作节奏。"),
    h("p", { class: "dz-note warn" },
      "按 token 计费，且输入视频秒同样入账——同样一镜，5s previz ≈ 多花 5 秒的钱。默认关。"),
    h("p", { class: "dz-note" },
      `已登记 previz：${ctx.registered} 镜。生成入口在底部时间轴条「▸ 交给 Seedance」`
      + "（弹选镜列表，未勾的镜跳过）。")));

  const adv = ctx.cameras.filter((c) => ctx.presetOf(c.preset)?.tier === "advanced").length;
  const hi = ctx.cameras.filter((c) => ctx.presetOf(c.preset)?.tier === "high-risk").length;
  el.append(card(
    sec("统计"),
    h("div", { class: "dz-kpis" },
      kpi(`${ctx.cuts.length}`, "镜头块"),
      kpi(fmtT(ctx.duration), "总时长"),
      kpi(`${ctx.actors.length}/${ctx.props.length}`, "角色 / 道具"),
      kpi(`${adv}/${hi}`, "进阶 / 高危", adv > ADVANCED_BUDGET || hi > 1 ? "bad" : null)),
    adv > ADVANCED_BUDGET ? h("p", { class: "dz-note warn" },
      `▲ 进阶运镜超过 ${ADVANCED_BUDGET} 个——记忆点会被稀释，建议只留最重的几拍。`) : null));
}

const kpi = (val, label, tone) => h("div", { class: "dz-kpi" + (tone ? " " + tone : "") },
  h("b", null, val), h("span", null, label));

function renderActorInspector(el, ctx, a) {
  const rotNow = ((Math.round((a.baseRotY * 180 / Math.PI) / 45) * 45) + 540) % 360 - 180;
  const models = ctx.dir.models || [];
  const modelGrid = h("div", { class: "dz-modelgrid" });
  models.forEach((m) => modelGrid.append(h("button", {
    class: "dz-modeltile" + (m.key === a.model ? " on" : ""), type: "button",
    dataset: { tip: `${m.label}\n${m.desc}（身高 ${m.height}m）` },
    onclick: () => { if (m.key !== a.model) ctx.setActorModel(a, m.key); },
  }, modelThumb(m.key, { size: 42 }), h("span", null, m.label))));

  el.append(card(
    sec("角色", h("em", { class: "dz-hint" }, a.model)),
    row("名称", textInput(a.name, { onchange: (v) => ctx.rename(a, v) })),
    sec("体型", h("em", { class: "dz-hint" },
      `${(models.find((m) => m.key === a.model) || {}).height ?? "—"}m`)),
    modelGrid,
    sec("朝向", h("em", { class: "dz-hint" }, `${rotNow}° · 0°=面向镜头`)),
    pills(ROT_STEPS.map((d) => ({ value: d, label: `${d}°` })),
      rotNow, (v) => ctx.setActorRot(a, +v)),
    a.pathPoints ? h("p", { class: "dz-note" },
      "已画走位：行进中朝向自动跟随路线方向，这里定的是起点 / 静止时的朝向。") : null));

  const trackBox = h("div", { class: "dz-tracks" });
  a.tracks.forEach((tr, i) => {
    const m = (ctx.dir.actions || []).find((x) => x.key === tr.action) || {};
    trackBox.append(h("div", { class: "dz-track" },
      h("span", { class: "dz-at mono" }, `${(tr.t0 || 0).toFixed(1)}s`),
      h("button", { class: "dz-actbtn", type: "button",
        dataset: { tip: `${m.label || tr.action}\n${m.desc || ""}\n\n点开动作选择器`
          + "（每格实时预览这个动作）" },
        onclick: () => openActionPicker(ctx, a, i) },
        actionThumb(tr.action, { model: a.model, size: 26, play: false }),
        h("b", null, m.label || tr.action),
        h("i", null, "⇅")),
      a.tracks.length > 1
        ? h("button", { class: "dz-x", type: "button", dataset: { tip: "删除这一段" },
            onclick: () => ctx.removeActorTrack(a, i) }, "✕")
        : null));
  });
  trackBox.append(h("button", { class: "dz-add", type: "button",
    dataset: { tip: "在播放头处新增一段动作\n先把播放头拖（或播）到想换动作的时刻，再点这里；"
      + "新段建好后直接弹出动作选择器。" },
    // 新段落在播放头上，故加完按播放头反查就是它——不必依赖 setTracks 排序后的下标
    onclick: () => { ctx.addActorTrack(a); openActionPicker(ctx, a, a.segmentIndexAt(ctx.playhead)); } },
    "＋ 在 ", h("b", { class: "dz-at-now mono" }, fmtT(ctx.playhead)), " 处加一段"));
  el.append(card(
    sec("表演", h("em", { class: "dz-hint" }, `${a.tracks.length} 段`)),
    trackBox));

  // 位移动作却没路线 = 原地踏步。这是最容易被当成"动画坏了"的情形，必须点破
  const actKey = a.segmentAt(0).action;
  const actMeta = (ctx.dir.actions || []).find((x) => x.key === actKey);
  const dur = ctx.duration || 1;
  const gs = a.pathPoints ? a.groundSpeed(dur) : 0;
  const ts = a.pathPoints ? a.timeScaleFor(actKey, dur) : 1;
  const bad = a.pathPoints && (ts <= 0.36 || ts >= 2.5);
  el.append(card(
    sec("走位", h("em", { class: "dz-hint" }, a.pathPoints
      ? `${a.pathPoints.length} 点 · ${a.pathLength.toFixed(2)}m` : "未画")),
    h("div", { class: "dz-btns" },
      h("button", { class: "dz-btn" + (ctx.drawingPathFor === a.id ? " on" : ""),
        type: "button", onclick: () => ctx.togglePathDraw(a) },
        ctx.drawingPathFor === a.id ? "✓ 完成路线" : "✎ 画路线"),
      a.pathPoints
        ? h("button", { class: "dz-btn", type: "button",
            onclick: () => ctx.clearPath(a) }, "清除")
        : null),
    ctx.drawingPathFor === a.id ? h("p", { class: "dz-note warn" },
      "画线中：起点已固定为角色当前站位——在地面依次点出路点（琥珀数字针），"
      + "拖针可微调；右键 / 退格撤销上一点，双击或回车完成，Esc 取消。"
      + "按 T 切顶视图画得最准。") : null,
    (!a.pathPoints && actMeta?.speed) ? h("p", { class: "dz-note warn" },
      `当前动作「${actMeta.label}」是位移动作，但还没画走位——播放时会原地踏步。`
      + "点「✎ 画路线」在地面点出他要走到哪。") : null,
    a.pathPoints ? h("div", { class: "dz-kpis two" },
      kpi(`${gs.toFixed(2)}`, "实际地速 m/s"),
      kpi(`×${ts.toFixed(2)}`, "步态倍速", bad ? "bad" : null)) : null,
    bad ? h("p", { class: "dz-note warn" },
      "步态倍速已到钳制边界——路线太长/太短会脚滑。换个更贴近的动作（走↔跑），"
      + "或调整路线长度与镜头时长。") : null,
    h("p", { class: "dz-note" },
      "走位是 previz 最值钱的信息——Seedance 会照着它安排人物在画面里的移动。"
      + "路线只在**位移动作段**内推进并恰好走完（路线长度 ÷ 位移段时长 = 实际地速，"
      + "引擎按它同步步频）——原地段站定表演，不再被整条时间轴摊薄。")));
}

const ROT_STEPS = [-180, -135, -90, -45, 0, 45, 90, 135];
function renderPropInspector(el, ctx, p) {
  el.append(card(
    sec("道具", h("em", { class: "dz-hint" }, p.prop)),
    row("名称", textInput(p.name, { onchange: (v) => ctx.rename(p, v) })),
    sec("朝向", h("em", { class: "dz-hint" }, `${p.rotY}°`)),
    pills(ROT_STEPS.map((d) => ({ value: d, label: `${d}°` })),
      Math.round(p.rotY / 45) * 45, (v) => ctx.setPropRot(p, +v)),
    h("p", { class: "dz-note" },
      "按住直接拖走，或用 gizmo 摆位（按 R 切移动 / 旋转）。道具只提供体块——占位、"
      + "挡镜、给运镜提供视差与遮挡关系。材质与细节交给 AI 生成阶段，previz 不做。")));
}

/* 构图九宫格：主体在画面里的落点（列=左三分/居中/右三分，行=偏上/中/偏下）。 */
const FRAME_COLS = [-0.167, 0, 0.167];
const FRAME_ROWS = [0.12, 0, -0.12];
const FRAME_TIP = { "-0.167": "左三分", 0: "居中", 0.167: "右三分" };
function anchorGrid(ctx, c) {
  const cur = c.frame || [0, 0];
  const g = h("div", { class: "dz-anchors" });
  FRAME_ROWS.forEach((fy) => FRAME_COLS.forEach((fx) => {
    const on = Math.abs(cur[0] - fx) < 0.03 && Math.abs(cur[1] - fy) < 0.03;
    const tipY = fy > 0 ? "・偏上" : fy < 0 ? "・偏下" : "";
    g.append(h("button", {
      class: "dz-anchor" + (on ? " on" : ""), type: "button",
      dataset: { tip: `主体落在${FRAME_TIP[String(fx)] || "此格"}${tipY}` },
      onclick: () => ctx.setCameraFrame(c, fx, fy),
    }, h("i", null)));
  }));
  return g;
}

const FOV_STEPS = [
  { value: 0.75, label: "0.75×", tip: "更长焦\n视角收窄、背景压缩，人物更「贴」。" },
  { value: 0.9, label: "0.9×", tip: "略长焦" },
  { value: 1, label: "1.0×", tip: "preset 原焦距（推荐）" },
  { value: 1.15, label: "1.15×", tip: "略广角" },
  { value: 1.35, label: "1.35×", tip: "更广角\n视角放宽、纵深夸张，适合小空间。" },
];

function renderCameraInspector(el, ctx, c) {
  const preset = ctx.presetOf(c.preset);
  const hasPath = !!(c.path && c.path.length >= 2);
  el.append(card(
    sec("机位", h("em", { class: "dz-hint mono" }, c.id)),
    row("名称", textInput(c.name, { onchange: (v) => ctx.rename(c, v) })),
    row("跟随主体", sel(
      [{ value: "", label: "第一个角色（缺省）" },
       ...ctx.actors.map((a) => ({ value: a.id, label: a.name }))],
      c.subject || "", (v) => ctx.setCameraSubject(c, v))),
    row("运动轨道", h("div", { class: "dz-stepper" },
      h("b", { class: "mono" }, hasPath ? `自定义 · ${c.path.length} 点` : "preset 程序轨道"),
      hasPath ? h("button", { class: "dz-icobtn", type: "button",
        dataset: { tip: "丢弃自定义轨道，回到运镜 preset 的程序轨道" },
        onclick: () => ctx.setCameraPath(c, null) }, "↺") : null)),
    hasPath ? null : row("方位偏转", h("div", { class: "dz-stepper" },
      h("b", { class: "mono" }, `${c.yaw || 0}°`),
      c.yaw ? h("button", { class: "dz-icobtn", type: "button",
        dataset: { tip: "回到 preset 原方位" },
        onclick: () => ctx.setCameraYaw(c, 0) }, "↺") : null)),
    hasPath ? null : row("机位距离", h("div", { class: "dz-stepper" },
      h("b", { class: "mono" }, `×${(c.dist || 1).toFixed(2)}`),
      (c.dist && c.dist !== 1) ? h("button", { class: "dz-icobtn", type: "button",
        dataset: { tip: "回到 preset 原距离" },
        onclick: () => ctx.setCameraDist(c, 1) }, "↺") : null)),
    h("p", { class: "dz-note" }, hasPath
      ? "自定义轨道：相机沿青色路点线**匀速走完本镜**（弧长参数化，结束帧必达终点）；"
        + "盯主体 / 焦距曲线 / 手持感照旧。**拖机身＝只挪起点（开拍位置）**，"
        + "拖珠子＝水平挪该点，↕ 手柄或 ⇧＝垂直调高（低起幅高收幅的仰拍环绕就靠它）；"
        + "**⌘(Mac)/Ctrl(Win)＋拖机身＝整条平移**，⇧＋拖机身＝整条升降。换运镜会回到预设轨道。"
      : "按住青色机身拖动＝同时定方位与距离——**放到哪，播放时相机那一刻就在哪**；"
        + "选中机位后轨迹线上有 5 枚路点针，**拖任意一枚即把轨迹变成可自由编辑的"
        + "自定义轨道**（形状原样保留；↕ 手柄或 ⇧＝垂直调高，⇧＋拖机身＝整条升降）。"),
    sec("焦距", h("em", { class: "dz-hint" }, `×${c.fovScale.toFixed(2)}`)),
    pills(FOV_STEPS, c.fovScale, (v) => ctx.setFovScale(c, +v)),
    sec("构图", h("em", { class: "dz-hint" }, "主体落点")),
    anchorGrid(ctx, c),
    h("p", { class: "dz-note" },
      "主体不必钉死在正中——三分线 / 头顶留白 / 视线预留全靠它"
      + "（Cinemachine Screen X/Y 同义）。对话戏把主体放到视线反侧的三分格。")));

  // 镜头块：这台机位拍的是哪一镜、拍几秒、渲出的 previz 在哪看——排戏最常碰的三件事
  const cut = ctx.cutOfCamera(c);
  if (cut) {
    const shot = ctx.shotOf(cut.shot) || {};
    el.append(card(
      sec("镜头块", h("em", { class: "dz-hint" }, `镜 ${cut.shot}`)),
      row("本镜时长", h("div", { class: "dz-stepper" },
        h("button", { class: "dz-icobtn", type: "button", dataset: { tip: "减 1 秒（下限 4s）" },
          onclick: () => ctx.setCutDur(cut.shot, cut.dur - 1) }, "−"),
        h("b", { class: "mono" }, `${cut.dur}s`),
        h("button", { class: "dz-icobtn", type: "button", dataset: { tip: "加 1 秒（上限 15s）" },
          onclick: () => ctx.setCutDur(cut.shot, cut.dur + 1) }, "＋"))),
      shot.previz ? h("div", { class: "dz-btns" },
        h("button", { class: "dz-btn", type: "button",
          dataset: { tip: "播放这一镜已渲出的 previz 参考片" },
          onclick: () => ctx.viewPreviz(cut.shot) }, "▶ 查看 previz"),
        h("a", { class: "dz-btn", href: shot.previz,
          download: `shot_${cut.shot}_previz.mp4`,
          dataset: { tip: "下载 previz 参考片（mp4）" } }, "⬇ 下载"))
      : null,
      h("p", { class: "dz-note" },
        (shot.previz ? "" : "「⏺ 渲染 previz」后这里会出现播放与下载。")
        + "时长按 Seedance 整秒档钳制（4~15s）——previz 与成片 1:1，差一秒运动就被拉伸或截断。")));
  }

  // 运镜：**当前一张卡 + 点开选择器**，不再把 36 个按钮堆进侧栏
  const notes = preset ? tierNotes(preset, {
    advancedUsed: ctx.cameras.filter(
      (x) => ctx.presetOf(x.preset)?.tier === "advanced").length,
    dur: ctx.cutDurOfCamera(c),
  }) : [];
  el.append(card(
    sec("运镜", preset
      ? chip(`${preset.tier_mark} ${preset.tier_label}`, TIER_CLR[preset.tier]) : null),
    h("button", { class: "dz-presetcard", type: "button",
      dataset: { tip: "点开运镜选择器\n36 个大师运镜，按基础 / 经典 / 大师三桶分组，可搜索。" },
      onclick: () => openPresetPicker(ctx, c) },
      h("div", null,
        h("b", null, preset ? preset.label : "未选运镜"),
        // 只显示英文名与建议时长——`rig` 是内部机位类型，对导演没有信息量，
        // 且多数 preset 的 label_en 与 rig 同名（locked-off · locked-off 读着像坏了）
        h("em", null, preset ? `${preset.label_en} · 建议 ${preset.duration}s`
          : "点这里从 36 个大师运镜里挑一个")),
      h("i", null, "⇅")),
    preset ? h("p", { class: "dz-phrase" }, preset.phrase) : null,
    ...notes.map((n) =>
      h("p", { class: "dz-note" + (n.level === "warn" ? " warn" : "") }, n.text)),
    h("p", { class: "dz-note" },
      "一镜只配一个主运镜——叠两个 rig 会让 Seedance 崩，所以这里是单选。"
      + "上面这句措辞会原样写进 shots[].camera 发给模型。")));
}

/* --------------------------------------------------------- 运镜选择器（弹层） */
export function openPresetPicker(ctx, cam) {
  tipHide();                     // 同上：不然触发按钮的提示会浮在弹层之上
  let settled = false;
  const close = () => {
    if (settled) return;
    settled = true;
    document.removeEventListener("keydown", onKey, true);
    overlay.classList.add("closing");
    setTimeout(() => overlay.remove(), 140);
  };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };

  const grid = h("div", { class: "dz-pkbody" });
  groupPresets(ctx.catalog).forEach((g) => {
    grid.append(h("div", { class: "dz-pkgroup" },
      h("span", { class: "k" }, g.label), h("em", null, `${g.items.length}`)));
    const cells = h("div", { class: "dz-pkgrid" });
    g.items.forEach((p) => cells.append(h("button", {
      class: "dz-preset" + (p.key === cam.preset ? " on" : "") + ` t-${TIER_CLR[p.tier]}`,
      type: "button",
      dataset: { tip: `${p.label}（${p.label_en}）\n${p.desc}\n\n运镜措辞：${p.phrase}` },
      onclick: () => { ctx.setPreset(cam, p.key); close(); },
    },
      h("span", { class: "dz-preset-t" }, p.tier_mark),
      h("b", null, p.label),
      h("em", null, `${p.duration}s`),
      h("span", { class: "dz-preset-d" }, p.desc))));
    grid.append(cells);
  });

  const overlay = h("div", { class: "dlg" },
    h("div", { class: "dlg-backdrop", onclick: close }),
    h("div", { class: "dlg-card dz-pkcard" },
      h("div", { class: "dz-pkhead" },
        h("span", { class: "k" }, `运镜 · ${cam.name}`),
        listSearch(".dz-preset", "搜运镜名 / 英文名 / 用途…"),
        h("button", { class: "dz-icobtn", type: "button", onclick: close }, "✕")),
      grid,
      h("p", { class: "dz-pkfoot" },
        `● 稳定放心用　▲ 进阶一集 ≤${ADVANCED_BUDGET} 个、建议 dur≥5s　■ 高危仅情绪最高点那一镜。`
        + "选中的措辞会原样写进 shots[].camera 发给 Seedance。")));
  document.body.append(overlay);
  document.addEventListener("keydown", onKey, true);
  setTimeout(() => overlay.querySelector(".fsearch")?.focus(), 40);
}

/* --------------------------------------------------------- 动作选择器（弹层） */
/** 动作分三桶。判据直接取目录字段（`speed` / `loop`），控制台不另存一份动作清单——
 *  目录是唯一真源，这里自行列举就会与引擎分叉成「选了没反应」。 */
function groupActions(actions) {
  const buckets = [
    { key: "move", label: "位移", note: "配走位路线才会真的移动", items: [] },
    { key: "loop", label: "原地循环", note: "站定表演，可铺满整段", items: [] },
    { key: "once", label: "一次性", note: "播完保持收势，落在节拍点上", items: [] },
  ];
  for (const a of actions) {
    (a.speed > 0 ? buckets[0] : (a.loop ? buckets[1] : buckets[2])).items.push(a);
  }
  return buckets.filter((b) => b.items.length);
}

/**
 * 动作选择器：每格实时播放该动作。
 *
 * 缩略图与舞台共用同一条求值路径与同一套布光，格子里的姿势即摆进舞台后的姿势。
 */
export function openActionPicker(ctx, actor, index) {
  const seg = actor.tracks[index];
  if (!seg) return;
  tipHide();                     // 触发按钮自己的悬浮提示会盖在弹层之上
  const search = listSearch(".dz-actcell", "搜动作名 / 键名 / 用途…");

  openShell({
    card: "dz-pkcard",
    build: (close) => {
      const body = h("div", { class: "dz-pkbody" });
      groupActions(ctx.dir.actions || []).forEach((g) => {
        body.append(h("div", { class: "dz-pkgroup" },
          h("span", { class: "k" }, g.label), h("em", null, `${g.items.length}`),
          h("i", { class: "dz-pkgnote" }, g.note)));
        const cells = h("div", { class: "dz-actgrid" });
        g.items.forEach((x) => cells.append(h("button", {
          class: "dz-actcell" + (x.key === seg.action ? " on" : "") + ` k-${g.key}`,
          type: "button",
          onclick: () => { ctx.setActorTrack(actor, index, { action: x.key }); close(); },
        },
          actionThumb(x.key, { model: actor.model, size: 84 }),
          h("div", { class: "dz-actinfo" },
            h("div", { class: "dz-actname" },
              h("b", null, x.label), h("em", null, x.key)),
            h("div", { class: "dz-actmeta" },
              chip(x.loop ? "循环" : "一次性"),
              chip(x.speed ? `${x.speed} m/s` : "原地")),
            h("span", { class: "dz-preset-d" }, x.desc)))));
        body.append(cells);
      });
      return [
        h("div", { class: "dz-pkhead" },
          h("span", { class: "k" }, `动作 · ${actor.name}`),
          // 时间读数不进 `.k`——那个语汇会把单位一并转成大写（0.0s → 0.0S）
          h("em", { class: "dz-hint mono" }, `${(seg.t0 || 0).toFixed(1)}s 起`),
          search,
          h("button", { class: "dz-icobtn", type: "button", onclick: close }, "✕")),
        body,
        h("p", { class: "dz-pkfoot" },
          "一段只有一个动作，段与段之间由引擎按固定窗口过渡，不会看到姿势跳变。"
          + (actor.pathPoints
            ? "已画走位：位移动作段内沿路线推进并恰好走完，原地动作段站定表演。"
            : "本角色还没画走位——位移动作会原地打拍子，选完记得在检查器点「✎ 画路线」。")),
      ];
    },
  });
  setTimeout(() => search.focus(), 40);
}

/* --------------------------------------------------------- 操作速查（弹层） */
/** 鼠标 / 触控板 / 键盘全表。常态视口不挂教程横幅——要查的时候点 ⌨ 一眼看全。 */
export function openKeysPop(anchor) {
  tipHide();
  const rc = anchor.getBoundingClientRect();
  let pop = null;
  const close = () => {
    pop?.remove(); pop = null;
    document.removeEventListener("click", close);
    document.removeEventListener("keydown", onEsc);
  };
  const onEsc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const rows = [
    ["点击", "选择（同一点位再点 = 轮选被挡住的对象）"],
    ["拖对象", "移动（角色连同走位路线一起挪）"],
    ["拖机位路点针", "改机位轨道（珠子=水平 · ↕ 手柄或 ⇧=垂直调高；一拖即转自定义）"],
    ["拖机身（自定义轨道）", "只挪起点（开拍位置）——后续路点不动"],
    ["⌘/Ctrl＋拖机身", "整条轨道水平平移（Mac 用 ⌘ · Win 用 Ctrl）"],
    ["⇧＋拖机身", "整条轨道垂直升降"],
    ["拖空处", "平移画布（上帝视角）"],
    ["⌥ + 拖 / 右键拖", "环绕旋转（触控板请用 ⌥ + 拖）"],
    ["滚轮 / 双指", "缩放"],
    ["双击对象", "选中并框满视口"],
    ["空格", "播放 / 暂停"],
    [", · .", "上一帧 / 下一帧（Shift ×10）"],
    ["方向键", "平移镜头（Shift ×3）"],
    ["F · T · G", "聚焦主体 / 顶视图 / 视角切换"],
    ["R", "gizmo 移动 ⇄ 旋转"],
    ["[ · ]", "收起左栏 / 右栏"],
    ["Esc", "取消（选中 / 落位 / 画线）"],
  ];
  const grid = h("div", { class: "dz-keys" });
  rows.forEach(([k, v]) => grid.append(h("b", null, k), h("span", null, v)));
  pop = h("div", { class: "dz-pop dz-pop-keys", onclick: (e) => e.stopPropagation() },
    h("div", { class: "dz-pop-sec" }, h("span", { class: "k" }, "操作速查")), grid);
  document.body.append(pop);
  pop.style.left = `${Math.max(12, Math.min(rc.left, innerWidth - pop.offsetWidth - 12))}px`;
  pop.style.top = `${Math.min(rc.bottom + 8, innerHeight - pop.offsetHeight - 12)}px`;
  setTimeout(() => {
    document.addEventListener("click", close);
    document.addEventListener("keydown", onEsc);
  }, 0);
}

/* -------------------------------------------------------------------- 时间轴 */
/** 秒刻度尺：每 5s 一根长刻度带数字，1s 一根短刻度——排戏要按秒读，不能靠估。 */
function ruler(total) {
  const el = h("div", { class: "dz-ruler" });
  const step = total > 60 ? 10 : 5;
  for (let t = 0; t <= total + 0.001; t += 1) {
    const major = t % step === 0;
    el.append(h("span", {
      class: "dz-tick" + (major ? " major" : ""),
      style: `left:${(t / Math.max(total, 1)) * 100}%`,
    }, major ? `${t}s` : ""));
  }
  return el;
}

/* 分镜块的悬浮放大预览：单例浮层，跟随目标块定位。
   底部那条脊柱缩略图只有几十像素高，看不清是哪一镜；悬停给一张大图，
   与章节页「TL 时间线」的读图习惯一致。 */
let CUTPOP = null;
function cutPopShow(target, { img, title, meta, phrase }) {
  if (!CUTPOP) CUTPOP = h("div", { class: "dz-cutpop" });
  CUTPOP.innerHTML = "";
  CUTPOP.append(
    img ? h("img", { src: img, alt: "" })
        : h("div", { class: "dz-cutpop-ph" }, "尚无分镜图"),
    h("div", { class: "dz-cutpop-t" }, h("b", null, title), h("em", null, meta)),
    phrase ? h("p", null, phrase) : null);
  (document.fullscreenElement || document.body).append(CUTPOP);
  const r = target.getBoundingClientRect();
  CUTPOP.classList.remove("on");
  CUTPOP.style.left = "0px"; CUTPOP.style.top = "0px";
  const w = CUTPOP.offsetWidth, ht = CUTPOP.offsetHeight;
  CUTPOP.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8))}px`;
  CUTPOP.style.top = `${Math.max(8, r.top - ht - 10)}px`;
  requestAnimationFrame(() => CUTPOP.classList.add("on"));
}
function cutPopHide() { CUTPOP?.classList.remove("on"); }

/** 全片预演控件：没合过=一个直接开合的按钮；合过=「▤ 全片预演」+ 悬停下拉
 *  （播放 / 下载 / 重新合成）。
 *
 *  为什么是悬停而不是点击展开：这里三项全是**次级动作**，点击展开会多一次交互、
 *  还要处理「点别处收起」；悬停即现、移开即收，代价最小。菜单**向上弹**——工具条
 *  在时间轴顶部、下方紧挨着镜头脊柱，向下弹会盖住正在看的分镜条。 */
function reelControl(ctx) {
  const reel = ctx.reel;
  const busy = ctx.reeling;
  if (!reel) {
    return h("button", {
      class: "dz-btn", type: "button",
      disabled: (ctx.registered && !busy) ? null : "disabled",
      dataset: { tip: "把各镜 previz 按分镜顺序拼成一条长片\n可直接播放/下载，用来审整场戏"
        + "连起来的节奏（逐镜点开看不出上一镜的收势接不接得住下一镜的起势）。\n"
        + "零 API 成本·纯本地 ffmpeg，不是成片。"
        + (ctx.registered ? "" : "\n（先渲染至少一镜 previz 才可用）") },
      onclick: () => ctx.buildReel(),
      // 图标一律单色符号，别用彩色 emoji——全站按钮都是 ⏺ ▸ ↻ ⌘ ✂ ◈ 这一套，
      // 混进一枚彩色的就与主界面制式不一致
    }, busy ? "合成中…" : "▤ 合成全片");
  }
  const n = (reel.shots || []).length;
  const secs = Number(reel.duration || 0).toFixed(1);
  const miss = (reel.skipped || []).filter((x) => x.why === "no_previz").length;
  const item = (label, attrs, sub) => h(
    attrs.href ? "a" : "button",
    { class: "dz-menu-item", type: attrs.href ? null : "button", ...attrs },
    h("b", null, label), sub ? h("em", null, sub) : null);
  return h("div", { class: "dz-mwrap" },
    h("button", {
      // **有菜单就不挂 tip**：两者都由 hover 触发、又都朝同一侧弹，tip 气泡会正好
      // 盖住菜单项（实测把「下载」「重新合成」两行糊掉）。信息已在菜单里说全了。
      class: "dz-btn dz-mtrig", type: "button", disabled: busy ? "disabled" : null,
      onclick: () => ctx.viewReel(),        // 直接点=播放（最常用的那一项）
    }, busy ? "合成中…" : `▤ 全片预演 ${secs}s`),
    h("div", { class: "dz-menu" },
      item("▶ 播放", { onclick: () => ctx.viewReel() }, `${n} 镜 · ${reel.fps || 24}fps`),
      item("⬇ 下载 mp4", { href: reel.video, download: reel.name || "previz_reel.mp4" },
           reel.size ? `${(reel.size / 1048576).toFixed(1)} MB` : null),
      item("↻ 重新合成", { onclick: () => ctx.buildReel() },
           miss ? `还有 ${miss} 镜未渲` : "新渲了镜就再合一次")));
}

/**
 * 播放头前进时只改会变的那几处，不重建时间轴结构。
 *
 * `renderTimeline` 是整块 `innerHTML = ""` 重建。挂到每帧的 `onTick` 上，工具条按钮
 * 会在 mousedown 与 mouseup 之间被换掉，而 `click` 要求两者落在同一元素上——播放
 * 期间整条工具条、每个镜头块与动作段都无法点击，只有键盘快捷键仍有效。
 */
export function syncTimelineHead(el, ctx) {
  const tl = ctx.timeline;
  const cut = tl.cutAt();
  const play = el.querySelector(".dz-play");
  if (play) play.textContent = tl.playing ? "❚❚" : "▶";
  const time = el.querySelector(".dz-time");
  if (time) time.textContent = `${fmtT(tl.t)} / ${fmtT(tl.duration)}`;
  const now = el.querySelector(".dz-now");
  if (now) {
    now.textContent = cut
      ? `镜 ${cut.shot} · ${ctx.cameraName(cut.camera)} · ${ctx.presetLabel(
        ctx.cameraById(cut.camera)?.preset) || "未选运镜"}`
      : "无镜头块";
  }
  const head = el.querySelector(".dz-playhead");
  if (head) head.style.left = `${(tl.t / Math.max(tl.duration, 1)) * 100}%`;
  const onShot = cut ? String(cut.shot) : null;
  el.querySelectorAll(".dz-cut").forEach((b) => {
    b.classList.toggle("on", b.dataset.shot === onShot);
  });
}

export function renderTimeline(el, ctx) {
  el.innerHTML = "";
  cutPopHide();
  const tl = ctx.timeline;
  const cut = tl.cutAt();
  const total = Math.max(tl.duration, 1);

  const bar = h("div", { class: "dz-tbar" });
  bar.append(h("button", { class: "dz-play", type: "button",
    dataset: { tip: "播放 / 暂停（空格）" }, onclick: () => ctx.togglePlay() },
    tl.playing ? "❚❚" : "▶"));
  bar.append(h("span", { class: "dz-time mono" },
    `${fmtT(tl.t)} / ${fmtT(tl.duration)}`));
  bar.append(h("span", { class: "dz-now" },
    cut ? `镜 ${cut.shot} · ${ctx.cameraName(cut.camera)} · ${ctx.presetLabel(
      ctx.cameraById(cut.camera)?.preset) || "未选运镜"}` : "无镜头块"));
  bar.append(h("div", { class: "dz-spacer" }));
  // 视角切换已上浮为视口顶部的胶囊（G 键仍在）——时间轴条只留时间轴与编排动作
  bar.append(h("button", { class: "dz-btn", type: "button",
    dataset: { tip: "复制 AI 编排指令\n打开指令台：带定位 + 场景 schema + 全部合法 key 的"
      + "标准指令，可另附本场诉求一起复制。粘贴给 Claude Code：它逐镜 Read 分镜图与叙事，"
      + "写出站位 / 走位 / 动作 / 机位构图与逐镜运镜手法，完成后刷新本页即恢复。" },
    onclick: () => ctx.copyAiPlan() }, aiIcon(), "AI 编排指令"));
  bar.append(h("button", { class: "dz-btn", type: "button",
    dataset: { tip: "保存这一场的编排\n角色/道具/机位/镜头块全部落进章节文档，下次进来自动恢复。" },
    onclick: () => ctx.saveScene() }, "⌘ 保存编排"));
  // 渲染 previz：点开选镜弹层（可勾若干镜或全选），忙态显示「镜N (i/n) 42%」
  const jb = ctx.renderJob;
  bar.append(h("button", {
    class: "dz-cta sm", type: "button", disabled: ctx.rendering ? "disabled" : null,
    dataset: { tip: "渲染 previz（弹出选镜列表）\n可勾单镜、多镜或全选；逐帧确定性导出 → "
      + "引擎编码 → 自动登记为该镜的首帧 / 末帧 / 参考片 / 运镜（落章节 _work/previz/，"
      + "制作台分镜卡 ◈ 角标可回放）。渲染中按 Esc 中止，已完成的镜保留。" },
    onclick: () => ctx.renderPreviz(),
  }, ctx.rendering
    ? (jb ? `镜${jb.shot} ${jb.i}/${jb.n} · ${ctx.renderPct}%` : `渲染中 ${ctx.renderPct}%`)
    : "⏺ 渲染 previz"));
  // 全片预演：**一个按钮 + 悬停下拉**。三个平铺按钮（重合/看/下载）在这条已经很挤
  // 的工具条上把「排戏 → 渲染 → 出片」的主干淹了——次级动作收进菜单，主干留在台面。
  // 位置刻意在「渲染 previz」与「交给 Seedance」之间：先逐镜渲、再整体过一遍节奏、
  // 确认没问题才去花钱，正是这条工作流的顺序。
  bar.append(reelControl(ctx));
  bar.append(h("button", {
    class: "dz-cta sm", type: "button", disabled: ctx.registered ? null : "disabled",
    dataset: { tip: "交给 Seedance 生成（弹出选镜列表）\n勾选要生成的镜（native 模式；"
      + "已预演的镜在 V2V 开启时自动带参考视频迁移运镜与走位）。\n"
      + "花钱操作——预算断闸 / 单笔超阈 / 4K 确认等既有成本闸照常生效。"
      + (ctx.registered ? "" : "\n（先渲染至少一镜 previz 才可用）") },
    onclick: () => ctx.toSeedance(),
  }, `▸ 交给 Seedance`));
  el.append(bar);
  el.append(ruler(tl.duration));

  // Shot-Cuts 脊柱：**默认就显示该镜的分镜图**（一眼认出是哪一镜），悬停给大图
  const track = h("div", { class: "dz-cuts" });
  tl.cuts.forEach((c) => {
    const on = cut && cut.shot === c.shot;
    const shot = ctx.shotOf(c.shot) || {};
    const img = shot.image || null;
    const cam = ctx.cameraById(c.camera);
    const block = h("div", {
      class: "dz-cut" + (on ? " on" : "") + (img ? " has-img" : ""),
      // 播放头跨镜时由 syncTimelineHead 按它就地换 .on，不重建整条脊柱
      dataset: { shot: String(c.shot) },
      style: `flex:0 0 ${(c.dur / total) * 100}%`
        + (img ? `;background-image:url('${img}')` : ""),
      onclick: () => ctx.selectCut(c),
      onmouseenter: (e) => cutPopShow(e.currentTarget, {
        img, title: `镜 ${c.shot}`,
        meta: `${c.dur}s · ${ctx.cameraName(c.camera)}`,
        phrase: ctx.presetOf(cam?.preset)?.phrase || shot.narration || "",
      }),
      onmouseleave: cutPopHide,
    },
      h("span", { class: "dz-cut-grad" }),
      h("b", null, `镜 ${c.shot}`),
      h("em", null, `${c.dur}s`),
      h("span", { class: "dz-cutcam" }, ctx.presetLabel(cam?.preset) || ctx.cameraName(c.camera)),
      shot.previz ? h("span", { class: "dz-cutpz",
        dataset: { tip: "已渲 previz——点击播放" },
        onclick: (e) => { e.stopPropagation(); ctx.viewPreviz(c.shot); } }, "◈") : null);
    track.append(block);
  });
  if (!tl.cuts.length) {
    track.append(h("div", { class: "dz-empty" },
      "还没有镜头块——本章的每个正镜都会自动出现在这里。"));
  }
  el.append(track);

  const scrub = h("div", { class: "dz-scrub", onmousedown: (e) => ctx.scrubStart(e) },
    h("div", { class: "dz-playhead", style: `left:${(tl.t / total) * 100}%` }));
  el.append(scrub);

  // 角色动作轨：左侧固定名牌（含走位标记），右侧按动作族着色的时间段
  if (ctx.actors.length) {
    const lanes = h("div", { class: "dz-lanes" });
    ctx.actors.forEach((a) => {
      const sel = ctx.selected?.id === a.id;
      // 说明与「路线只在位移段内推进并恰好走完」的时序模型对齐：
      // 位移段 tip 报**实际地速**（路线长 ÷ 位移段总时长），不是动作内建速度——
      // 报内建速度会与检查器的「实际地速」两个数对不上（旧文案实测漂移）
      const gs = a.pathPoints ? a.groundSpeed(tl.duration) : 0;
      const name = h("div", { class: "dz-laneid" + (sel ? " on" : ""),
        onclick: () => ctx.select(a),
        dataset: { tip: `${a.name}\n${a.pathPoints
          ? `已画走位 ${a.pathLength.toFixed(1)}m——只在位移动作段内推进并恰好走完`
          : "未画走位（原地表演）"}` } },
        h("i", null, "☗"), h("b", null, a.name),
        a.pathPoints ? h("span", { class: "dz-lanepath" }, "⤳") : null);
      const strip = h("div", { class: "dz-strip" });
      a.tracks.forEach((tr, i) => {
        const t1 = a.tracks[i + 1] ? a.tracks[i + 1].t0 : tl.duration;
        const meta = (ctx.dir.actions || []).find((x) => x.key === tr.action) || {};
        strip.append(h("div", {
          class: "dz-seg" + (meta.speed ? " move" : (meta.loop ? "" : " once")),
          style: `left:${(tr.t0 / total) * 100}%;width:${((t1 - tr.t0) / total) * 100}%`,
          dataset: { tip: `${meta.label || tr.action}\n${tr.t0.toFixed(1)}s → ${t1.toFixed(1)}s`
            + (meta.speed
              ? (a.pathPoints
                ? `\n位移段：走位在此推进（实际地速 ${gs.toFixed(2)} m/s·引擎自动同步步频）`
                : "\n位移动作（原地打拍子）——画走位路线才会真的移动")
              : "")
            + "\n\n点击换动作（选择器每格实时预览）" },
          // 先选中该角色：检查器与弹层随即指向同一段，改完两处读数一致
          onclick: () => { ctx.select(a); openActionPicker(ctx, a, i); },
        }, h("span", null, meta.label || tr.action)));
      });
      lanes.append(h("div", { class: "dz-lane" }, name, strip));
    });
    el.append(lanes);
  }
}
