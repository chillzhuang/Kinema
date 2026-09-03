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

/* ═ Studio 前端模块 · app/widgets.js — 通用组件 · 特效选择器 · 框选局部改造（原生 ES Module·免构建）═ */

/* ---------------- 通用组件 ---------------- */
import { chip, uiConfirm, uiDialog, uiPrompt, openShell } from "./components.js";
import { $ } from "./core.js";
import { STATE } from "./state.js";
import { render } from "../app.js";
import { BUST, GENJOBS, ICON, LABEL, MOTION, STAGE_ZH, api, fmtDur, getOverview, h, jobKey,
         pollJob, post, state, toast, withBust } from "./core.js";
import { genWait, genWaitOff, openSketchFixDialog, trackSketchJob } from "./chapter.js";
import { openRefsDialog } from "./shot-tools.js";
import { openAssetVPanel } from "./panels.js";

const titledChip = (text, kind, tip) => {
  const c = chip(text, kind);
  if (tip) c.dataset.tip = tip;   // 系统级即时提示（零延迟悬浮层）
  return c;
};

/* ---- 合成特效（滤镜）：展示已生效特效 chip + 选择器（写章节 effects → 可选重合成） ---- */
const EFX_CLR = { texture: "amber", game: "cyan", weather: "blue", particle: "red", light: "green" };
const efxCatalog = () => (state.overview && state.overview.effects_catalog) || [];
const efxMeta = (k) => efxCatalog().find((e) => e.key === k)
  // 未注册名不许画成生效 chip：合成端会拒绝它，这里若按原名渲染就是第二处假成功
  || { key: k, label: `${k}（未注册）`, category: "texture", category_label: "", audio: false,
       desc: "特效注册表里没有这个名字，合成时不会被应用——请从章节 effects 移除或换用注册表内的名字" };
function effectChip(k) {
  const m = efxMeta(k);
  return titledChip(m.label, EFX_CLR[m.category] || "blue", m.desc);   // 纯文字，无 icon
}
/* 特效弹框：分类网格勾选 + 取消 / 确认（保存并重合成，与水印/角标同款 .dlg 弹层风格）。
   选项按钮走 data-tip 悬浮说明（#sys-tip z400 > .dlg z210，弹层内也正常浮出），与其他按钮一致。 */
function openEffectsDialog(d) {
  const chosen = new Set(d.effects || []);
  const warn = h("div", { class: "efx-warn" });
  const syncWarn = () => {
    const na = [...chosen].filter((k) => efxMeta(k).audio).length;
    warn.textContent = chosen.size > 3
      ? `⚠ 已选 ${chosen.size} 层，建议 ≤3 层（宁少勿多）`
      : (na > 1 ? `⚠ ${na} 个带环境音特效叠加，声音会混` : "");
  };
  const body = h("div", { class: "efx-dlg-body" });
  const cats = {};
  efxCatalog().forEach((e) => (cats[e.category_label] = cats[e.category_label] || []).push(e));
  Object.entries(cats).forEach(([cat, list]) => {
    body.append(h("div", { class: "efx-cat" },
      h("span", { class: "efx-cat-k" }, cat),
      h("div", { class: "efx-opts" }, ...list.map((e) => {
        const b = h("button", {
          class: "efx-opt" + (chosen.has(e.key) ? " on" : ""),
          dataset: { tip: e.desc }, onclick: () => {   // data-tip 悬浮说明，与全站一致
            chosen.has(e.key) ? chosen.delete(e.key) : chosen.add(e.key);
            b.classList.toggle("on"); syncWarn();
          } }, e.label);                 // 纯文字，无 icon
        return b;
      }))));
  });
  syncWarn();
  // 点击即关弹层、接口后台跑——重合成本就是异步 job，不卡等接口返回
  const save = (recompose) => {
    close();
    post("/api/effects/set",
      { project: d.project, chapter: d.id, effects: [...chosen], recompose })
      .then((r) => {
        // 保存成功才播报：被操作锁拒时只该看到一条拒绝提示
        toast(recompose ? "特效已保存，重新合成中…（约十几秒）" : "特效已保存（下次合成生效）");
        STATE.chapSig = "";                     // 触发章节视图下次轮询重绘（显示新特效）
        if (recompose && r.job)
          pollJob(r.job, {
            onDone: () => { STATE.chapSig = ""; toast("重新合成完成——刷新看成片"); },
            onFail: (j) => toast(`合成失败：${(j.tail || "").slice(-140)}`, true) });
      })
      .catch((e) => toast(e.message, true));
  };
  const close = openShell({ card: "efx-dlg-card", build: (close) => [
      h("span", { class: "k" }, "合成特效"),
      h("p", { class: "dlg-msg" },
        "勾选合成时叠加的滤镜 / 氛围特效（宁少勿多 ≤3 层）——确认后写入本集并重新合成生效。"),
      body, warn,
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "取消"),
        h("button", { class: "dlg-btn primary", onclick: () => save(true) }, "确认"))] });
}

/* 特效按钮（放映区）：点击开弹框选择（与浮动/角标/构建同款风格）。已叠加特效时金色 fx-on。 */
function effectsBtn(d) {
  const nFx = (d.effects || []).length;
  return h("button", { class: "screen-act" + (nFx ? " fx-on" : ""),
    dataset: { tip: nFx                       // 首行=黄色标题、余下=正文（与浮动/角标/构建一致）
      ? `合成特效（已叠加 ${nFx} 层）\n${(d.effects || []).map((k) => efxMeta(k).label).join(" / ")}——点开修改 / 换特效`
      : "合成特效（滤镜 / 氛围）\n选择合成时叠加的特效——保存写入本集，可重新合成生效" },
    onclick: () => openEffectsDialog(d) },
    h("span", { class: "screen-act-ico",
      html: '<svg viewBox="0 0 24 24"><path d="M12 3l2 5.3L19.5 9l-4.2 3.8L16.7 19 12 15.9'
        + ' 7.3 19l1.4-6.2L4.5 9l5.5-.7Z"/></svg>' }),
    "特效");
}

/* 浮动水印控件（放映区）：填文案→后台生成带水印成片；留空提交→删除。防搬运漂移，
   与固定角标可同开。预填文案由 scanner 下发 d.watermark.text（章节 watermark > branding）。 */
/* 水印（放映区）：**一个按钮、一个弹层**同时管漂移水印与固定角标。

   合并的理由不是"少一个按钮"，是**它们本来就是同一件事的两半**：都烧在同一份成片上、
   都靠 `watermark --from-project --force` 从干净原片重烧。分成两个按钮时，用户改完一类
   要再点另一类，而每次点都会起一个重烧任务——两个任务同时改写同一批 `output_wm`，
   后完成的那个以另一类的旧状态为准，就会出现"刚设的角标又没了"。
   现在一次填完、一次提交、一次重烧（后端 `set_watermark` 三态写入）。 */
/* 水印重烧忙态：烧录是后台 job，弹层提交后章节视图会随轮询整页重绘——忙态必须
   活在模块状态里、由 watermarkBtn 渲染时读取，只挂在按钮节点上会被重绘冲掉。
   忙态期间按钮禁点：两个重烧任务同时改写 output_wm，后完成者会以另一任务的旧
   状态为准（与「合并成单按钮」同一根因）。job 结束（成/败）即恢复。 */
let wmBusyKey = "";

function watermarkBtn(d) {
  const wm = d.watermark || {};
  const fx = wm.fixed || {};
  const bt = wm.bottom || {};
  // 徽标数 = 弹层里开着的段数：三类水印 + 字幕样式覆盖（判据与弹层内 hadSub 同源——
  // override 有键即本章启用；漏算字幕会出现「四段全开、外面只显示 3」）
  const subOn = Object.keys((d.subtitle_style || {}).override || {}).length > 0;
  const n = (wm.floating_on ? 1 : 0) + (fx.on ? 1 : 0) + (bt.on ? 1 : 0)
          + (subOn ? 1 : 0);
  const busy = wmBusyKey === `${d.project}/${d.id}`;
  const btn = h("button", { class: "screen-act" + (n ? " wm-on" : "")
      + (busy ? " is-busy" : ""),
    disabled: busy || null,
    dataset: { tip: busy
      ? "水印烧录中——完成后自动恢复可点"
      : (wm.has_output
        ? "水印与字幕 · 放映外观\n漂移水印（防搬运·连续弹性漫游）、底部水印（半透明常驻署名·"
          + "底部居中）、固定角标（品牌署名·贴边）可任意组合；字幕样式（字号/颜色/描边/边距）"
          + "同面板可调。\n水印版与原片并存，不覆盖原片；留空即删除。"
        : "先合成出片，才能调水印与字幕样式") },
    onclick: () => {
      if (busy) return;
      if (!wm.has_output) { toast("先合成出片，才能调水印与字幕样式", true); return; }
      openWatermarkPanel(d, btn);
    } },
    h("span", { class: "screen-act-ico",
      html: '<svg viewBox="0 0 24 24"><path d="M12 3.2s5.5 6 5.5 10.3a5.5 5.5 0 0 1-11 0'
        + 'C6.5 9.2 12 3.2 12 3.2Z"/></svg>' }),
    "水印",
    n ? h("i", { class: "screen-act-n" }, String(n)) : null);
  return btn;
}

/* 四角位置选择器（弹层内）：2×2 网格，选中角高亮；pos 经 onPick 回传闭包。 */
const WM_CORNERS = [["tl", "左上"], ["tr", "右上"], ["bl", "左下"], ["br", "右下"]];
function cornerPicker(initial, onPick) {
  const grid = h("div", { class: "corner-pick" });
  const cells = WM_CORNERS.map(([pos, label]) => h("button", {
    type: "button", class: "corner-cell c-" + pos + (pos === initial ? " on" : ""),
    onclick: () => {
      onPick(pos);
      grid.querySelectorAll(".corner-cell").forEach((x) => x.classList.remove("on"));
      grid.querySelector(".c-" + pos).classList.add("on");
    } }, h("span", { class: "corner-dot" }), label));
  grid.append(...cells);
  return grid;
}

/* 合并放映外观弹层：三类水印与字幕样式各自独立开关，一次提交。
   字幕样式的开关语义与水印一致——开着本章覆盖才生效，关掉提交＝整组回落画风
   缺省（后端 style:null，lang/mode 等行为键不动）；开着时仍逐键 dirty，
   没动的键继续跟随画风。
   刻意自建模态而不复用 `uiDialog`——后者只支持单个输入框，而这里要多段表单；
   但外壳类名（.dlg / .dlg-card / .dlg-acts）全部沿用，观感与全站弹层一致。
   提交分两条路（防竞态，与后端 set_watermark 的合并写入口同一条纪律）：
   只改水印 → /api/watermark 一次写盘一次重烧；动了字幕样式 → 先 /api/watermark
   burn:false 只写盘，再 /api/subtitle/style 走 rebuild 单链（重合成烧字幕 → 刷水印版），
   绝不同时起两个改写 output_wm 的任务。 */
function openWatermarkPanel(d, trigger) {
  const wm = d.watermark || {};
  const fx = wm.fixed || {};
  const bt = wm.bottom || {};
  const ss = d.subtitle_style || {};
  const eff = ss.effective || {};
  // 字幕开关初值 = 本章有没有样式覆盖：面板要如实反映「现在生效的是覆盖还是缺省」
  const hadSub = Object.keys(ss.override || {}).length > 0;
  const st = { fOn: !!wm.floating_on, cOn: !!fx.on, bOn: !!bt.on, sOn: hadSub,
               pos: fx.position || "br" };

  const section = (key, on, title, badge, tone, desc, ...body) => {
    const sw = h("input", { type: "checkbox", class: "wm-sw",
      checked: on ? "checked" : null,
      onchange: (e) => { st[key] = e.target.checked; card.classList.toggle("on", st[key]); } });
    const card = h("div", { class: "wm-sec" + (on ? " on" : "") + " t-" + tone },
      h("label", { class: "wm-sec-head" },
        h("b", null, title), h("em", null, badge), h("span", { class: "wm-spacer" }), sw),
      h("div", { class: "wm-sec-body" }, ...body,
        h("p", { class: "wm-desc" }, desc)));
    return card;
  };

  const fInput = h("input", { class: "cmt-input", type: "text",
    value: wm.text || "", placeholder: "@你的频道",
    oninput: () => { if (fInput.value.trim()) { st.fOn = true; syncSw(); } } });
  const cInput = h("input", { class: "cmt-input", type: "text",
    value: fx.text || "", placeholder: "@你的频道",
    oninput: () => { if (cInput.value.trim()) { st.cOn = true; syncSw(); } } });
  const bInput = h("input", { class: "cmt-input", type: "text",
    value: bt.text || "", placeholder: "@你的频道 · 每周更新",
    oninput: () => { if (bInput.value.trim()) { st.bOn = true; syncSw(); } } });

  const fSec = section("fOn", st.fOn, "漂移水印", "防搬运", "blue",
    "全程在场、匀速漂移、碰到画面边界随机角度反弹——搬运方裁不掉也遮不住。",
    fInput);
  const cSec = section("cOn", st.cOn, "固定角标", "品牌署名", "blue",
    "字幕式烧录、清晰不透明、比字幕小四号、贴边——干净的署名，不打扰画面。",
    cInput, cornerPicker(st.pos, (p) => { st.pos = p; }));
  const bSec = section("bOn", st.bOn, "底部水印", "半透明署名", "blue",
    "固定在画面底部正中、离底边留一小段距离；半透明、无描边无底衬——"
    + "优雅的常驻署名，位于字幕底带下方、互不重叠。",
    bInput);

  // 输入即自动打开对应开关（填了字却忘了开开关是这类表单最常见的空跑）
  function syncSw() {
    for (const [sec, key] of [[fSec, "fOn"], [cSec, "cOn"], [bSec, "bOn"],
                              [sSec, "sOn"]]) {
      sec.querySelector(".wm-sw").checked = st[key];
      sec.classList.toggle("on", st[key]);
    }
  }

  // ── 字幕样式（合成期烧录，改了要重合成整章才可见）──────────────────
  // 逐键 dirty：只把用户动过的键发成章节覆盖，没动的键继续跟随画风缺省；
  // 动了字段就自动打开开关——与水印文案框同一条「填了即启用」纪律。
  const dirty = {};
  const touch = (key, val) => {
    dirty[key] = val;
    if (!st.sOn) { st.sOn = true; syncSw(); }
  };
  const numInput = (key, ph) => h("input", { class: "cmt-input wm-num",
    type: "number", value: eff[key] ?? "", placeholder: ph || "",
    oninput: (e) => {
      // 非法中间态（如只输了负号）时 value 读出来是空串，不该当「清掉这一键」
      if (e.target.validity.badInput) return;
      touch(key, e.target.value === "" ? null : Number(e.target.value));
    } });
  const colorInput = (key) => h("input", { class: "wm-color", type: "color",
    value: /^#[0-9a-fA-F]{6}$/.test(eff[key] || "") ? eff[key] : "#ffffff",
    oninput: (e) => touch(key, e.target.value) });
  const inSize = numInput("size");
  const inOutline = numInput("outline");
  const inMargin = numInput("margin_v", "自适应");
  const inColor = colorInput("text_color");
  const inEdge = colorInput("outline_color");
  const fill = (kv) => {          // 预设 = 一次填充多个键（都算用户动过）
    for (const [k, v] of Object.entries(kv)) {
      touch(k, v);
      const el = { size: inSize, outline: inOutline, margin_v: inMargin,
                   text_color: inColor, outline_color: inEdge }[k];
      if (el) el.value = v;
    }
  };
  const preset = (label, tip, kv) => h("button", { type: "button",
    class: "wm-preset", dataset: { tip }, onclick: () => fill(kv) }, label);
  // 预设 = 完整外观：四个外观键（字号/描边宽/文字色/描边色）每款全部写死，点谁就是谁
  // ——部分键预设会让上一款的残值粘过来（实测：点过「大字清晰」后 66 号字粘在其后每款上）
  const presets = h("div", { class: "wm-presets" },
    preset("柔影细边", "缺省字号、描边收细到 2、近黑描边——治「黑边太粗」",
           { size: 58, outline: 2, text_color: "#ffffff", outline_color: "#1a1a1a" }),
    preset("大字清晰", "字号加大到 66、描边 4——治「字太小」",
           { size: 66, outline: 4, text_color: "#ffffff", outline_color: "#000000" }),
    preset("经典黄字", "综艺/纪录片常用的暖黄字幕",
           { size: 58, outline: 3, text_color: "#ffe14d", outline_color: "#202020" }),
    preset("综艺粗黑", "白字加粗黑边、高对比——热闹综艺感",
           { size: 62, outline: 6, text_color: "#ffffff", outline_color: "#000000" }),
    preset("青蓝科技", "冰蓝字配深海描边——科幻/数码内容",
           { size: 58, outline: 3, text_color: "#baf3ff", outline_color: "#06283a" }),
    preset("暖橙元气", "亮橙字配深棕描边——美食/生活元气感",
           { size: 58, outline: 3, text_color: "#ffb547", outline_color: "#2b1602" }),
    preset("极简细白", "小一号米白细边——文艺纪录片气质",
           { size: 50, outline: 1, text_color: "#f2f2ee", outline_color: "#333333" }));
  const row = (label, ...ctrl) => h("div", { class: "wm-row" },
    h("span", { class: "wm-row-l" }, label), ...ctrl);
  const sSec = section("sOn", st.sOn, "字幕样式", "合成期烧录", "blue",
    "开着才启用本章覆盖，逐键覆盖画风缺省（没动的项继续跟随画风）；关掉提交＝"
      + "全部回落画风缺省。改了字幕样式会重新合成整章成片（约几十秒），"
      + "水印版随后自动刷新。",
    presets,
    row("字号", inSize, h("span", { class: "wm-row-l" }, "描边宽"), inOutline),
    row("文字色", inColor, h("span", { class: "wm-row-l" }, "描边色"), inEdge),
    row("底边距", inMargin,
        h("i", { class: "wm-row-h" }, "留空=横竖屏自适应贴底/避让")));
  // 字幕表单比水印段高：预设行 + 三行控件 + 说明，展开高度要放得下
  sSec.classList.add("wm-tall");

  async function apply() {
    // 开关关掉 = 送空串 = 删除这一类；三类都空则后端删掉整个水印版、还原原片
    const text = st.fOn ? fInput.value.trim() : "";
    const fixedText = st.cOn ? cInput.value.trim() : "";
    const bottomText = st.bOn ? bInput.value.trim() : "";
    if (st.fOn && !text) { fInput.focus(); toast("漂移水印开着但没填文案", true); return; }
    if (st.cOn && !fixedText) { cInput.focus(); toast("固定角标开着但没填文案", true); return; }
    if (st.bOn && !bottomText) { bInput.focus(); toast("底部水印开着但没填文案", true); return; }
    // 字幕开关刚打开却一个键都没动：没有可保存的覆盖，静默关窗与「没保存上」无从区分
    // ——与上面三条「开着但没填文案」同款拦截（已有覆盖时不改键是合法 no-op，不拦）
    if (st.sOn && !hadSub && !Object.keys(dirty).length) {
      toast("字幕样式开着但还没调整任何项——选个预设或改字号/颜色再应用", true); return;
    }
    // 开着 → 只有真动过键才值得重合成；关着 → 本章原有覆盖才需要回落（都没有就别空烧）
    const subChanged = st.sOn ? Object.keys(dirty).length > 0 : hadSub;
    // 忙态从提交起、到后台 job 收尾（成/败）止；settle 置空 chapSig 触发重绘恢复按钮
    const busyKey = `${d.project}/${d.id}`;
    const settle = () => {
      if (wmBusyKey === busyKey) wmBusyKey = "";
      STATE.chapSig = "";
    };
    wmBusyKey = busyKey;
    if (trigger?.isConnected) { trigger.disabled = true; trigger.classList.add("is-busy"); }
    close();
    STATE.chapSig = "";   // 立即触发重绘：视图尽快反映新设置，按钮按 wmBusyKey 渲染忙态
    try {
      const wmBody = { project: d.project, chapter: d.id, text,
                       fixed_text: fixedText, fixed_position: st.pos,
                       bottom_text: bottomText };
      if (subChanged) {
        // 字幕变了：水印只写盘不烧，重烧统一归 rebuild 单链（防两任务抢写 output_wm）
        await post("/api/watermark", { ...wmBody, burn: false });
        const r = await post("/api/subtitle/style", { project: d.project,
          chapter: d.id, style: st.sOn ? dirty : null, rebuild: true });
        toast(r.rewatermark ? "重新合成字幕并刷新水印版中…（约几十秒）"
                            : "重新合成字幕中…（约几十秒）");
        if (r.job) pollJob(r.job, {
          onDone: () => { settle(); toast("✓ 字幕样式已生效——刷新查看成片"); },
          onFail: (j) => { settle(); toast(`重烧失败：${(j.tail || "").slice(-140)}`, true); } });
        else settle();
        return;
      }
      const r = await post("/api/watermark", wmBody);
      if (!r.watermarked) {
        settle();
        // removed=0 说明本来就没有水印版——说「已移除」是在报告一次没发生的删除
        toast(r.removed ? "水印已全部移除——原片保留" : "未设置水印——原片保留");
        return;
      }
      toast(`重烧带水印成片中…（${[r.floating && "漂移", r.fixed && "角标",
        r.bottom && "底部"].filter(Boolean).join(" + ")}，约十几秒）`);
      if (r.job) pollJob(r.job, {
        onDone: () => { settle(); toast("✓ 水印完成——刷新看带水印成片"); },
        onFail: (j) => { settle(); toast(`水印失败：${(j.tail || "").slice(-140)}`, true); } });
      else settle();
    } catch (e) { settle(); toast(e.message, true); }
  }

  const close = openShell({ card: "wm-card",
    keys: (e) => {
      if (e.key === "Enter" && e.target.tagName === "INPUT"
          && e.target.type !== "number" && e.target.type !== "color") {
        e.stopPropagation(); e.preventDefault(); apply();
      }
    },
    build: (close) => [
      h("span", { class: "k" }, "水印与字幕"),
      h("p", { class: "dlg-msg" },
        "三类水印可任意组合，水印版与原片并存（不覆盖原片）；关掉开关提交即删除该类。"),
      // 四段进同一条滚动区（卡片封顶视口高，头/脚钉住）——全开也不会把「应用」顶出屏
      h("div", { class: "wm-scroll" }, fSec, bSec, cSec, sSec),
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "取消"),
        h("button", { class: "dlg-btn primary", onclick: apply }, "应用"))] });
  setTimeout(() => (st.fOn || !st.cOn ? fInput : cInput).focus(), 30);
}

/* 3D 导演台入口卡（独立区块，落在「后期」之上）。

   位置是有讲究的：导演台属于**生成之前**——在烧 Seedance 之前把走位、动作、机位、
   运镜排好；而「后期」是生成之后修饰成片。两者混在同一排按钮里，导演台会被当成
   又一个滤镜开关，而它其实是一整个工作台。故给它自己的区块头与一张有分量的卡。 */
function directorCard(d) {
  const shots = (d.shots || []).filter((s) => s.kind !== "transition" && !s.omitted);
  const withPz = shots.filter((s) => s.previz);
  const withCam = shots.filter((s) => (s.camera || "").trim());
  const scene = d.previz || null;
  const href = `#/stage/${encodeURIComponent(d.project)}/${encodeURIComponent(d.id)}`;

  const stat = (val, label, tone) => h("div", { class: "dzc-stat" + (tone ? " " + tone : "") },
    h("b", null, val), h("span", null, label));

  return h("div", { class: "card dzc" },
    h("div", { class: "dzc-grid", "aria-hidden": "true" }),
    h("div", { class: "dzc-body" },
      // 顶行：左标题右主按钮——卡在主列是通栏宽度，标题与 CTA 分居两端才不显空旷
      h("div", { class: "dzc-top" },
        h("div", { class: "dzc-head" },
          // 台标：开拍的场记板——三台共用「拍板·取景·剧本」一套影视工序语汇
          h("span", { class: "dzc-ico",
            html: '<svg viewBox="0 0 28 28">'
              + '<path d="M4.6 12.6h18.8v7.6a2.2 2.2 0 0 1-2.2 2.2H6.8'
              + 'a2.2 2.2 0 0 1-2.2-2.2z"/>'
              + '<g transform="rotate(-15 5 12.6)">'
              + '<path d="M4.6 8.4a1.6 1.6 0 0 1 1.6-1.6h15.6a1.6 1.6 0 0 1 1.6 1.6'
              + 'v4.2H4.6z"/>'
              + '<path d="M9.2 6.8l-1.9 5.8M14.8 6.8l-1.9 5.8M20.4 6.8l-1.9 5.8"/></g>'
              + '<path d="M8.4 17.2h6.4"/></svg>' }),
          h("div", null,
            h("b", null, "3D 导演台"),
            h("em", null, "正式生成之前，先把这一集的戏排出来"))),
        h("div", { class: "dzc-acts" },
          h("a", { class: "dzc-go", href },
            h("span", { class: "dzc-go-ico" }, "◈"),
            h("b", null, "进入导演台"),
            h("i", { class: "dzc-go-arw" }, "→")),
          scene?.updated_at
            ? h("span", { class: "dzc-saved mono" },
                `编排已存 · ${String(scene.updated_at).slice(5, 16).replace("T", " ")}`)
            // 有预演却没有场景快照 = 预演是 CLI 登记的（`previz register`）——
            // 此时说"尚未排过戏"与盘上已有的 previz 产物直接矛盾
            : h("span", { class: "dzc-saved" },
                withPz.length ? "预演经命令行登记 · 编排未保存" : "尚未排过戏"))),
      // 中段左右分栏：左说明右状态——卡片单独成行后有 1400px 宽，
      // 全部左对齐会在右半边空出一大片，读起来像"这块没做完"
      h("div", { class: "dzc-mid" },
      h("p", { class: "dzc-desc" },
        "摆放灰模角色 · 指派动作 · 绘制走位路线 · 布置多机位 · 从 36 个大师运镜中"
        + "选定一个，一键渲染预演参考片。产物回填本集的首帧 / 末帧 / 参考视频 / "
        + "运镜措辞，随后图生视频照常经过成本控制与审阅流程。"),
      h("div", { class: "dzc-foot" },
        h("div", { class: "dzc-stats" },
          stat(`${withPz.length}/${shots.length}`, "镜已排预演",
            withPz.length ? "cyan" : null),
          stat(String(withCam.length), "镜已定运镜", withCam.length ? "amber" : null),
          stat(d.previz_v2v ? "已开" : "未开", "参考视频 V2V",
            d.previz_v2v ? "amber" : null)),
        withPz.length
          ? h("div", { class: "dzc-shots" },
              h("span", { class: "k" }, "已排"),
              ...withPz.map((s) => h("button", {
                class: "dzc-pill",
                dataset: { tip: `镜 ${s.id} 的预演参考片\n${s.camera || "未写运镜"}` },
                onclick: () => openCinema({ video: s.previz,
                  title: `镜 ${s.id} · 3D 预演参考片`,
                  rows: [["运镜", s.camera || "—"], ["preset", s.camera_preset || "—"],
                         ["时长", s.previz_seconds
                           ? `${(+s.previz_seconds).toFixed(1)}s` : "—"]],
                  chips: ["previz", "灰模预演·非成片"] }),
              }, `镜 ${s.id}`)))
          : null)),
      d.motion === "kenburns"
        ? h("p", { class: "dzc-note" },
            "本集当前是 Ken Burns（静图运镜）模式——预演的走位与运镜要真正生效，"
            + "需切到 Native 图生视频。导演台里排好的运镜措辞对两种模式都会写进分镜。")
        : null));
}

/* 重新构建（放映区）：按当前字幕/特效/水印设置从已生成的图·配音重烧成片。
   烧水印/角标/改字幕换行等「后置设置」不会自动重出成片——改完点这里一键刷新。
   后台串行：assemble --draft（重烧字幕+特效）→（有水印则）watermark --from-project。 */
function rebuildBtn(d) {
  const wm = d.watermark || {};
  const btn = h("button", { class: "screen-act rebuild-act",
    dataset: { tip: wm.has_output
      ? "按当前字幕 / 特效 / 水印设置，重新烧录成片一遍（不重跑生图/配音/图生视频）。\n"
        + "改了字幕、特效、水印后，点它让成片跟上。"
      : "先合成出片，才能重新构建" },
    onclick: async () => {
      if (!wm.has_output) { toast("先合成出片，才能重新构建", true); return; }
      const ok = await uiConfirm(
        "按当前字幕 / 特效 / 水印设置，重新烧录成片？\n复用已生成的图·配音（不重跑 AI 生成），约一到几分钟。",
        { title: "重新构建成片" });
      if (!ok) return;
      try {
        const r = await post("/api/rebuild", { project: d.project, chapter: d.id });
        toast(`重新构建中…（${r.steps} 步${r.rewatermark ? " · 含刷新水印" : ""}，约一到几分钟）`);
        if (r.job) pollJob(r.job, {
          onDone: () => { STATE.chapSig = ""; toast("✓ 重新构建完成——刷新看新成片"); },
          onFail: (j) => toast(`构建失败：${(j.tail || "").slice(-160)}`, true) });
      } catch (e) { toast(e.message, true); }
    } },
    h("span", { class: "screen-act-ico",
      html: '<svg viewBox="0 0 24 24"><path d="M20 11.5a8 8 0 1 1-2.3-5.6"/>'
        + '<path d="M20 3.5v4.2h-4.2"/></svg>' }),
    "构建");                                 // 始终两字
  return btn;
}
const profileChip = (p) => p && chip(LABEL.profile[p] || p, "amber");
// skill 绑定 chip：显示 /kn-xxx cmd + 中文名（tip 点破「报项目名/编号 AI 即自动调用」）
const skillChip = (sid) => {
  if (!sid) return null;
  const m = LABEL.skill[sid] || {};
  return titledChip(`⌘ ${m.cmd || "/" + sid}`, "cyan",
    `绑定指挥层 Skill${m.label ? " · " + m.label : ""}\n`
    + "报项目名 / 编号即可让 AI 查得该调哪个 skill，无需再点名画风。");
};
const statusPill = (s) => h("span", { class: "st " + (s || "") }, LABEL.status[s] || s || "—");

function motionBadge(m) {
  const info = MOTION[m] || MOTION.kenburns;
  return h("span", { class: `mbadge m-${info.key.toLowerCase()}`,
      dataset: { tip: info.tip } },
    h("b", null, info.key), `${info.name}`);
}

/* 区块头。第五参 action 是右端动作位（当前只有「更多」跳转），挂在计数之后——
   计数报的恒是**全量**，动作位负责把被截断的那部分领去它的整页。 */
function secHeader(no, zh, en, count, action) {
  return h("div", { class: "sec" },
    h("span", { class: "no" }, no),
    h("h2", null, zh),
    h("span", { class: "en" }, en),
    h("span", { class: "rule" }),
    count != null && h("span", { class: "cnt" }, String(count)),
    action || null);
}

/* 区块头「更多」：截断展示的区块把余量领到整页去。所有页面共用这一个件——
   各写一份的下场是同一个入口在不同区块长不同的样子。 */
const secMore = (href, label = "更多") =>
  h("a", { class: "sec-more", href }, label, h("i", null, "→"));

/* 音频胶囊：懒加载 <audio>，全局互斥播放 */
const AudioBus = { current: null };
function audioPill(src, label) {
  const btn = h("button", { class: "apill-btn", html: ICON.play, title: "播放" });
  const fill = h("i");
  const track = h("div", { class: "apill-track" }, fill);
  const time = h("span", { class: "apill-time" }, "0:00");
  const root = h("div", { class: "apill" },
    btn, label ? h("span", { class: "apill-label" }, label) : null, track, time);
  let audio = null;
  const ensure = () => {
    if (audio) return audio;
    audio = new Audio(src);
    audio.preload = "metadata";
    audio.addEventListener("timeupdate", () => {
      if (audio.duration) fill.style.width = `${(audio.currentTime / audio.duration) * 100}%`;
      time.textContent = fmtDur(audio.currentTime);
    });
    audio.addEventListener("loadedmetadata", () => { time.textContent = fmtDur(audio.duration); });
    audio.addEventListener("ended", () => { root.classList.remove("playing"); btn.innerHTML = ICON.play; });
    audio.addEventListener("pause", () => { root.classList.remove("playing"); btn.innerHTML = ICON.play; });
    audio.addEventListener("play", () => {
      if (AudioBus.current && AudioBus.current !== audio) AudioBus.current.pause();
      AudioBus.current = audio;
      root.classList.add("playing"); btn.innerHTML = ICON.pause;
    });
    return audio;
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const a = ensure();
    a.paused ? a.play() : a.pause();
  });
  track.addEventListener("click", (e) => {
    e.stopPropagation();
    const a = ensure();
    if (!a.duration) return;
    const r = track.getBoundingClientRect();
    a.currentTime = ((e.clientX - r.left) / r.width) * a.duration;
    if (a.paused) a.play();
  });
  return root;
}

/* 灯箱 ↔ 版本谱系互为上下层：末次打开者置顶（120/121 双档·仍在 rf-overlay z-200 之下）。
   灯箱里点「▤ 版本谱系」→ 谱系盖在灯箱上；谱系里点缩略图放大 → 灯箱盖在谱系上。 */
function raiseModal(which) {
  const lb = $("#lightbox"), vp = $("#vpanel");
  if (which === "vpanel") { vp.style.zIndex = "121"; lb.style.zIndex = "120"; }
  else { lb.style.zIndex = "121"; vp.style.zIndex = "120"; }
}

/* 灯箱（像素锚定评论——点击画面打点，坐标 0~1 相对值） */
const LB = { items: [], idx: 0, pending: null };
function openLightbox(items, idx = 0) {
  LB.items = items; LB.idx = idx; LB.pending = null; LB.refine = null;
  LB.mode = "note";                          // 缺省=◉ 提意见模式
  renderLightbox();
  $("#lightbox").hidden = false;
  raiseModal("lightbox");                     // 末次打开置顶（谱系里点图放大 → 覆盖谱系）
  document.body.style.overflow = "hidden";
}
function lbCtx(it) { return it.ctx || it.actx || {}; }   // 意见容器：分镜(ctx) 或 设定图(actx)
function lbTarget(it) {                                    // POST 目标：分镜带 chapter/shot；设定图带 asset_kind/name
  if (it.ctx) return { project: it.ctx.pid, chapter: it.ctx.cid, shot: it.ctx.shot };
  return { project: it.actx.pid, asset_kind: it.actx.kind, asset_name: it.actx.name || null };
}
function lbComments(it) {
  const stage = it.ctx?.stage || "image";
  return (lbCtx(it).comments || []).filter((c) => (c.stage || "image") === stage && c.x != null);
}
function charInfo(c) {   // 角色设定图 INFO：外貌 + 全部设定字段（对齐分镜 INFO 的信息量）
  return [c.appearance, c.role && `身份：${c.role}`, c.outfit && `服装：${c.outfit}`,
          c.hair && `发型：${c.hair}`, c.weapon && `武器：${c.weapon}`,
          c.voice && `音色：${c.voice}`].filter(Boolean).join("　｜　") || "—";
}
/* 信息区「复制」：一次性绑定（钮是 index.html 里的静态节点，行为恒定；
   正文在点下去那一刻从 DOM 现读，故不必随每次翻页重绑）。
   取 innerText 而不是 textContent——简笔板的逐拍表是 `white-space: pre-line` 的
   多行文本，innerText 拿到的是**渲染后的换行**，粘出去与眼前所见逐行一致。 */
const COPY_LABEL = "⧉ 复制";     // 常态文案（与 index.html 的静态文本同一份）
let lbCopyTimer = null;
function bindCapCopy() {
  const btn = $("#lb-cap-copy");
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = "1";
  btn.onclick = async (e) => {
    e.stopPropagation();
    const text = ($("#lb-cap").innerText || "").trim();
    if (!text || text === "—") return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      toast("复制失败（浏览器剪贴板权限）", true);
      return;
    }
    toast("信息全文已复制");
    // 就地瞬时态：toast 在屏幕另一头，手指还在按钮上，反馈得落在按下去的那个点
    clearTimeout(lbCopyTimer);
    btn.classList.add("ok");
    btn.textContent = "✓ 已复制";
    lbCopyTimer = setTimeout(() => {
      btn.classList.remove("ok");
      btn.textContent = COPY_LABEL;
    }, 1400);
  };
}

function renderLightbox() {
  const it = LB.items[LB.idx];
  if (!it) return;
  const img = $("#lb-img");
  img.src = it.src;
  // 头部：标题徽标（SHOT 05 / CHARACTER·社畜林深…）
  const tt = $("#lb-title");
  tt.innerHTML = "";
  tt.append(h("b", null, it.title || "预览"));   // 原生 append(false) 会渲染出 "false" 文本——单图时不追加计数徽标
  if (LB.items.length > 1)
    tt.append(h("i", null, `${LB.idx + 1} / ${LB.items.length}`));
  // 信息区：提示词/描述全文（复制钮随「有没有正文」显隐，翻页即复位）
  const cap = $("#lb-cap");
  cap.innerHTML = "";
  cap.append(it.caption || "—");
  bindCapCopy();
  const capBtn = $("#lb-cap-copy");
  if (capBtn) {
    capBtn.hidden = !String(it.caption || "").trim();
    clearTimeout(lbCopyTimer);            // 上一张的「已复制」不许跟着翻页留在这儿
    capBtn.classList.remove("ok");
    capBtn.textContent = COPY_LABEL;
  }
  // 操作区：局部改造 / 重新生成 / 下载原图（+ 改造输入插槽）
  const ops = $("#lb-actions");
  ops.innerHTML = "";
  const act = (label, desc, onclick, cls) =>
    h("button", { class: "lb-act" + (cls ? " " + cls : ""), onclick },
      h("b", null, label), desc ? h("span", null, desc) : null);
  const canRefine = (it.ctx && it.ctx.stage === "image" && it.ctx.shot != null) || it.actx;
  ops.append(act("⬇ 下载原图", null, () => {
    const a = h("a", { href: it.src, download: "" });
    document.body.append(a); a.click(); a.remove();
  }));
  const isShotImg = it.ctx && it.ctx.stage === "image" && it.ctx.shot != null;
  if (isShotImg) {
    // 分镜图：重新生成 + 垫图 同排各半（垫图＝从参考库勾选本镜风格垫图，与分镜卡「⛭ 垫图」同一界面）
    const regenBtn = act("↻ 重新生成", "整镜重出 · 携带全部改造意见 · 旧版可回滚", () => lbRegen(it));
    const refBtn = act("⛭ 垫图参考", "从参考库勾选本镜风格垫图 · 可保存并重生", () =>
      openRefsDialog({ project: it.ctx.pid, id: it.ctx.cid },
                     { id: it.ctx.shot, refs: it.ctx.refs }));
    ops.append(h("div", { class: "lb-act-row" }, regenBtn, refBtn));
  } else if (it.actx) {
    // 设定图：重新生成（提意见→refine 全图应用）+ 垫图（逐张设定图各自挑垫图，按新垫图重出）同排各半
    const regenBtn = act("↻ 重新生成",
      "有标注意见→按意见整图应用；没有→按该类设定图的原版式规则整张重出。"
      + "旧版备份 · 血缘传播下游分镜", () => lbRegen(it));
    // 场景俯视图不吃风格垫图（它是制图，参考只有该场景的基准图，后端同判据），
    // 故不给它这个入口——摆一个点了必报错的按钮比没有更糟
    const refBtn = it.actx.kind === "topview" ? null
      : act("⛭ 垫图参考", "为这张设定图单独挑垫图 · 保存并按新垫图重出", () =>
        openRefsDialog({ project: it.actx.pid },
                       { asset: { kind: it.actx.kind, name: it.actx.name || null }, it }));
    ops.append(refBtn ? h("div", { class: "lb-act-row" }, regenBtn, refBtn) : regenBtn);
    ops.append(act("▤ 版本谱系", "查看这张设定图的历次归档 · 一键回滚历史版", () =>
      openAssetVPanel(it.actx.pid, it.actx.kind, it.actx.name || null)));
  } else if (it.skctx) {
    // 简笔分镜板：改板指令台（与格签同一份文本、同一个弹层）+ 输入要求整板重出
    // （模版垫图 + 原拍序列 + 新要求合并，走 `sketch gen --force --note` 同一条
    // CLI 路径——引擎侧拼装，网页只传意见）
    if (it.skctx.directive)
      ops.append(act("⧉ 改板指令", "写下要求 · 合并带定位坐标的指令后复制交 Claude Code 改拍序列",
        () => openSketchFixDialog(it.skctx)));
    ops.append(act("↻ 重新生成", "输入修改要求 · 按样板版式+原拍序列+新要求整板重出", async () => {
      const note = await uiPrompt(
        `镜 ${it.skctx.shot} 简笔板的修改要求（直接编译进本次板提示词的「修正重点」；`
        + "留空=按原拍序列重滚一版）：",
        { placeholder: "例：第4格动作幅度更大；蓝色运镜箭头改为环绕弧线", title: "↻ 重新生成简笔板" });
      if (note === null) return;
      try {
        const r = await post("/api/sketch/regen", { project: it.skctx.pid,
          chapter: it.skctx.cid, shot: it.skctx.shot, note: (note || "").trim() });
        toast(`镜 ${it.skctx.shot} 简笔板重生已启动`);
        closeLightbox();      // 先关灯箱，再登记忙态——trackSketchJob 内含 softRefresh，
        // 关掉就能看见该镜的「生成中」格（旧图压暗盖转圈），完成后自动换新板
        trackSketchJob(it.skctx.pid, it.skctx.cid, r.job, String(it.skctx.shot));
      } catch (err) { toast(err.message, true); }
    }));
  }
  // 「修改」区：双模式分段选择器 + 输入槽（两种画布交互显式互斥，零歧义）
  const mod = $("#lb-mod");
  mod.innerHTML = "";
  const modSec = $("#lb-mod-sec");
  if (modSec) modSec.hidden = !(it.ctx || it.actx);
  if (it.ctx || it.actx) {
    const seg = (key, ico, zh, desc) => h("button", {
      class: "lb-seg" + (LB.mode === key ? " on" : ""),
      onclick: () => {
        if (LB.mode === key) return;
        LB.mode = key;
        LB.refine = key === "refine" ? { rect: null, drag: null } : null;
        LB.pending = null;
        renderLightbox();
      } }, h("b", null, `${ico} ${zh}`), h("span", null, desc));
    const segs = [];
    if (it.ctx || it.actx) segs.push(seg("note", "◉", "标注意见",
      "打点 / 划线圈范围 · 攒着重生成时带上"));
    if (canRefine) segs.push(seg("refine", "✂", "局部改造",
      "框选 + 指令 · 立即改这一处"));
    mod.append(h("div", { class: "lb-segbar" }, ...segs),
               h("div", { id: "lb-refine-slot" }));
  }
  // 元信息：分辨率 + 定位坐标
  const meta = $("#lb-meta");
  meta.innerHTML = "";
  const dim = h("span", null, "…");
  const setDim = () => { dim.textContent = `${img.naturalWidth} × ${img.naturalHeight}`; };
  img.complete && img.naturalWidth ? setDim()
    : img.addEventListener("load", setDim, { once: true });
  meta.append(dim);
  if (it.ctx) meta.append(h("span", null,
    `${it.ctx.pid} / ${it.ctx.cid} · 镜${it.ctx.shot} · ${STAGE_ZH[it.ctx.stage] || it.ctx.stage}`));
  else if (it.actx) meta.append(h("span", null,
    `${it.actx.pid} · ${it.actx.kind}${it.actx.name ? " · " + it.actx.name : ""}`));
  // 当前模式不在画面上再标一次：右栏的模式卡本就高亮着，输入框占位符也写着该怎么操作，
  // 而画布左上角恰好是构图主体最常落的位置——一块常驻胶囊把它压在底下。
  $("#lb-canvas").classList.toggle("refining", LB.mode === "refine");
  requestAnimationFrame(drawStrokes);
  const many = LB.items.length > 1;
  $("#lb-prev").style.display = many ? "" : "none";
  $("#lb-next").style.display = many ? "" : "none";
  renderPins();
}

/* 灯箱内整镜重生成：确认 → 后台任务 → 画布转动等待 → 完成原位换图 */
async function lbRegen(it) {
  const isAsset = !it.ctx;
  const ASSET_ZH = { scene: "场景设定图", topview: "场景俯视图",
                     character: "角色设定图", prop: "道具设定图" };
  const label = isAsset
    ? `${it.actx.name ? `「${it.actx.name}」的` : ""}`
      + (ASSET_ZH[it.actx.kind] || `${it.actx.kind}设定图`)
    : `镜 ${it.ctx.shot} 的分镜图`;
  if (!(await uiConfirm(
    `重新生成${label}？` + (isAsset
      ? "有标注意见就按意见整图应用；没有意见则按该类设定图的原版式规则整张重出"
        + "（三区两视等版式规则不变）。旧版备份，并按血缘把下游用到它的分镜标过期。"
      : "旧版自动归档进版本栈可回滚。") + "（真实生图按张计费）",
    { title: `${label} · 重新生成` }))) return;
  try {
    const r = await post("/api/regen", lbTarget(it));
    // 分镜忙态入册（分镜卡凭此显示「生成中」）；设定图无镜号，只灯箱内自轮询收尾
    const key = it.ctx ? jobKey(it.ctx.pid, it.ctx.cid, it.ctx.shot) : null;
    if (key) GENJOBS.set(key, { id: r.job, kind: "regen" });
    genWait($("#lb-canvas"), "重新生成中");
    pollJob(r.job, {
      onDone: async () => {
        if (key) { GENJOBS.delete(key); BUST.set(key, Date.now()); }
        genWaitOff($("#lb-canvas"));
        it.src = withBust(it.src, Date.now());
        toast(`${label}已重新生成` + (isAsset ? "（下游分镜已按血缘标过期）" : ""));
        renderLightbox();
        await reloadLBContext(it);
      },
      onFail: (j) => { if (key) GENJOBS.delete(key); genWaitOff($("#lb-canvas"));
        toast(`重生成失败：${(j.tail || "").slice(-160)}`, true); },
    });
  } catch (err) { toast(err.message, true); }
}

/* ---- 框选局部改造：拖拽画框 → 指令 → /api/refine（"只改这一处"） ---- */
function toggleRefine() {
  LB.mode = LB.mode === "refine" ? "note" : "refine";
  LB.refine = LB.mode === "refine" ? { rect: null, drag: null } : null;
  LB.pending = null;
  renderLightbox();
}
function regionLabel(r) {
  const cols = ["左", "中", "右"], rows = ["上", "中", "下"];
  const pos = rows[Math.min(2, (r.y + r.h / 2) * 3 | 0)]
            + cols[Math.min(2, (r.x + r.w / 2) * 3 | 0)];
  return `${pos} · ${Math.max(1, Math.round(r.w * r.h * 100))}%`;
}
function renderRect() {
  let el = document.getElementById("lb-rect");
  const r = LB.refine && LB.refine.rect;
  if (!r) { el && el.remove(); return; }
  if (!el) {
    el = h("div", { id: "lb-rect" });
    $("#lb-canvas").append(el);
  }
  Object.assign(el.style, { left: `${r.x * 100}%`, top: `${r.y * 100}%`,
    width: `${r.w * 100}%`, height: `${r.h * 100}%` });
  el.dataset.label = regionLabel(r);
}
function _lbNorm(e) {
  const rc = $("#lb-img").getBoundingClientRect();
  return { x: Math.min(1, Math.max(0, (e.clientX - rc.left) / rc.width)),
           y: Math.min(1, Math.max(0, (e.clientY - rc.top) / rc.height)) };
}
/* 双模式指针逻辑：◉ 提意见=点击打点/按住划线；✂ 局部改造=拖拽框选 */
let _lbStroke = null;   // 提意见模式的进行中笔迹（归一化点列）
$("#lb-canvas").addEventListener("mousedown", (e) => {
  const it = LB.items[LB.idx];
  if (e.target.closest("#lb-rect") || e.target.closest(".lb-pin")) return;
  if (LB.mode === "refine" && LB.refine) {
    e.preventDefault();
    LB.refine.drag = _lbNorm(e);
    LB.refine.rect = null;
    renderRect();
  } else if (it?.ctx || it?.actx) {          // 提意见模式（缺省）——分镜 ctx 与设定图 actx 通吃
    e.preventDefault();
    _lbStroke = [_lbNorm(e)];
  }
});
document.addEventListener("mousemove", (e) => {
  if (LB.mode === "refine" && LB.refine?.drag) {
    const a = LB.refine.drag, b = _lbNorm(e);
    LB.refine.rect = { x: +Math.min(a.x, b.x).toFixed(4), y: +Math.min(a.y, b.y).toFixed(4),
      w: +Math.abs(b.x - a.x).toFixed(4), h: +Math.abs(b.y - a.y).toFixed(4) };
    renderRect();
  } else if (_lbStroke) {
    _lbStroke.push(_lbNorm(e));
    drawStrokes();
  }
});
document.addEventListener("mouseup", () => {
  if (LB.mode === "refine" && LB.refine?.drag) {
    LB.refine.drag = null;
    const r = LB.refine.rect;
    if (r && r.w * r.h < 0.003) { LB.refine.rect = null; renderRect(); return; }  // 误点不算框
    renderPins();
    return;
  }
  if (!_lbStroke) return;
  const pts = _lbStroke; _lbStroke = null;
  const span = Math.max(...pts.map((p2) => p2.x)) - Math.min(...pts.map((p2) => p2.x))
             + Math.max(...pts.map((p2) => p2.y)) - Math.min(...pts.map((p2) => p2.y));
  if (span < 0.012 || pts.length < 4) {      // 视为单击 → 打点
    const a = pts[0];
    if (a.x < 0 || a.x > 1 || a.y < 0 || a.y > 1) return;
    LB.pending = { x: +a.x.toFixed(4), y: +a.y.toFixed(4) };
  } else {                                   // 划线 → 笔迹质心为锚点，路径随意见入库
    const cx = pts.reduce((s2, p2) => s2 + p2.x, 0) / pts.length;
    const cy = pts.reduce((s2, p2) => s2 + p2.y, 0) / pts.length;
    const path = pts.filter((_, k) => k % Math.ceil(pts.length / 120) === 0)
      .map((p2) => [+p2.x.toFixed(3), +p2.y.toFixed(3)]);
    LB.pending = { x: +cx.toFixed(4), y: +cy.toFixed(4), path };
  }
  renderPins();
});

/* 笔迹层：已入库意见的 path + 进行中笔迹（琥珀描线） */
function drawStrokes() {
  const cvId = "lb-strokes";
  let cv = document.getElementById(cvId);
  const host = $("#lb-canvas"), img = $("#lb-img");
  if (!img || !host) return;
  if (!cv) {
    cv = h("canvas", { id: cvId });
    host.insertBefore(cv, $("#lb-pins"));
  }
  const rc = img.getBoundingClientRect();
  cv.width = rc.width; cv.height = rc.height;
  const g = cv.getContext("2d");
  g.clearRect(0, 0, cv.width, cv.height);
  g.lineWidth = 2.5; g.lineJoin = g.lineCap = "round";
  g.strokeStyle = "rgba(240,166,60,.9)";
  g.shadowColor = "rgba(240,166,60,.45)"; g.shadowBlur = 6;
  const paint = (pts) => {
    if (!pts || pts.length < 2) return;
    g.beginPath();
    pts.forEach((pt, k) => {
      const x = (pt[0] ?? pt.x) * cv.width, y = (pt[1] ?? pt.y) * cv.height;
      g[k ? "lineTo" : "moveTo"](x, y);
    });
    g.stroke();
  };
  const it = LB.items[LB.idx];
  if (LB.mode !== "refine") {
    lbComments(it || {}).forEach((c) => paint(c.path));
    if (LB.pending?.path) paint(LB.pending.path);
  }
  if (_lbStroke) paint(_lbStroke);
}
async function submitRefine(it, inp) {
  const text = inp.value.trim();
  if (!text) return;
  inp.disabled = true;
  inp.value = "改造中…（调用图像模型编辑重生，约十几秒）";
  const body = { instruction: text, rect: LB.refine.rect, async: true };
  if (it.ctx) {
    Object.assign(body, { project: it.ctx.pid, chapter: it.ctx.cid, shot: it.ctx.shot });
  } else {
    Object.assign(body, { project: it.actx.pid, asset_kind: it.actx.kind,
                          asset_name: it.actx.name || null });
  }
  try {
    const r = await post("/api/refine", body);
    // 镜级改造入忙态册（分镜卡同步显示「局部改造中」）；设定图改造无镜号不入
    const key = it.ctx ? jobKey(it.ctx.pid, it.ctx.cid, it.ctx.shot) : null;
    if (key) GENJOBS.set(key, { id: r.job, kind: "refine" });
    genWait($("#lb-canvas"), "局部改造中");
    pollJob(r.job, {
      onDone: async (j) => {
        if (key) { GENJOBS.delete(key); BUST.set(key, Date.now()); }
        genWaitOff($("#lb-canvas"));
        const res = j.result || {};
        toast(`局部改造完成（${res.region || "整图"}）`
              + (res.stale_retaken ? ` · 下游 ${res.stale_retaken} 镜已标重做` : ""));
        LB.refine = null;
        it.src = `/media?path=${encodeURIComponent(res.image)}&t=${Date.now()}`;
        renderLightbox();
        if (it.ctx) await reloadLBContext(it);
        else { await getOverview(true); render(); }   // 设定图变了 → 背景视图重拉
      },
      onFail: (j) => {
        if (key) GENJOBS.delete(key);
        genWaitOff($("#lb-canvas"));
        toast(`改造失败：${(j.tail || "未知错误").slice(-160)}`, true);
        inp.disabled = false;
        inp.value = text;
      },
    });
  } catch (err) {
    toast(err.message, true);
    inp.disabled = false;
    inp.value = text;
  }
}
function renderPins() {
  const it = LB.items[LB.idx];
  const pins = $("#lb-pins"), box = $("#lb-comments");
  const slot = $("#lb-refine-slot");
  const notesSec = $("#lb-notes-sec");
  pins.innerHTML = ""; box.innerHTML = "";
  if (slot) slot.innerHTML = "";
  renderRect();
  if (notesSec) notesSec.hidden = LB.mode === "refine" || !(it?.ctx || it?.actx);
  if (LB.mode === "refine" && LB.refine) {   // 手术模式：文本域 + 提交按钮挂修改区插槽
    const has = !!LB.refine.rect;
    const inp = h("textarea", { class: "rf-note", rows: "3",
      placeholder: has ? "这块区域要改成什么？写清楚材质/颜色/形态…（⌘/Ctrl+Enter 开始改造）"
                       : "先在画面拖拽框选要改的区域，再描述改造内容…" });
    // 必须「框选区域 + 描述」齐备才可开始——缺任一都禁用（空跑手术没有靶区/指令）
    const ready = () => !!LB.refine?.rect && !!inp.value.trim();
    const goRefine = () => {
      if (!ready()) return toast("先框选要改的区域，并写清楚改成什么样", true);
      submitRefine(it, inp);
    };
    const goBtn = h("button", { class: "lb-refine-go",
      dataset: { tip: "开始改造 · 局部手术\n只重绘框选区域，其余像素保持原样；"
        + "需先框选＋写清改造描述。按一次图像编辑计费，旧版归档可回滚。"
        + "快捷键 ⌘/Ctrl+Enter。" },
      onclick: goRefine }, "开始改造 ⏎");
    const syncGo = () => { goBtn.disabled = !ready(); };
    inp.addEventListener("input", syncGo);
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) goRefine();
      if (e.key === "Escape") toggleRefine();
    });
    (slot || box).append(h("div", { class: "lb-refine col" },
      inp,
      h("div", { class: "lb-refine-foot" },
        h("span", { class: "lb-refine-tag" }, has ? regionLabel(LB.refine.rect) : "未框选"),
        goBtn)));
    syncGo();
    if (has) setTimeout(() => inp.focus(), 30);
    return;
  }
  if (!it?.ctx && !it?.actx) return;   // 无任何上下文（纯看图，如封面/血缘缩略）→ 只看不评
  const list = lbComments(it);
  list.forEach((c, n) => {
    // 钉点悬浮说明走系统级 data-tip 组件（首行=编号，正文=意见原文）
    pins.append(h("span", { class: "lb-pin",
      dataset: { tip: `◉ 改造意见 ${n + 1}\n${c.text}` },
      style: `left:${c.x * 100}%;top:${c.y * 100}%` }, String(n + 1)));
  });
  if (LB.pending) {
    pins.append(h("span", { class: "lb-pin new",
      style: `left:${LB.pending.x * 100}%;top:${LB.pending.y * 100}%` }, "+"));
  }
  // 评论列表 + 输入行
  list.forEach((c, n) => {
    const txt = h("span", { class: "cmt-text editable",
      dataset: { tip: "点击直接修改这条意见" },
      onclick: () => beginEdit() }, c.text);
    const row = h("div", { class: "cmt-row" },
      h("b", null, String(n + 1)), txt,
      h("button", { class: "cmt-act del", dataset: { tip: "删除这条意见（改好了就删掉）" },
        onclick: () => cmtUpdate(it, c.id, { delete: true }) }, "✕"));
    const beginEdit = () => {
      const ed = h("input", { class: "cmt-input", type: "text", value: c.text });
      ed.addEventListener("keydown", async (e) => {
        e.stopPropagation();
        if (e.key === "Enter" && ed.value.trim())
          await cmtUpdate(it, c.id, { text: ed.value.trim() });
        if (e.key === "Escape") renderPins();
      });
      row.replaceChild(ed, txt);
      ed.focus(); ed.select();
    };
    box.append(row);
  });
  // 提意见编写器（与局改同款制式）：文本域 + 左锚点标签 + 右提交 chip，可连续多条
  const noteTag = LB.pending
    ? (LB.pending.path ? "已圈范围" : `点位 ${(LB.pending.x * 100) | 0},${(LB.pending.y * 100) | 0}`)
    : "整体";
  const inp = h("textarea", { class: "rf-note", rows: "3",
    placeholder: LB.pending
      ? (LB.pending.path ? "圈住的这块要怎么改？（⌘/Ctrl+Enter 直接开始改造）"
                         : "这个点要怎么改？（⌘/Ctrl+Enter 直接开始改造）")
      : "点击打点 / 划线圈范围后描述，或直接写整体意见…" });
  const submitNote = async () => {
    const text = inp.value.trim();
    if (!text) { inp.focus(); return toast("先写意见内容", true); }
    try {
      await post("/api/comment", { ...lbTarget(it), stage: it.ctx?.stage || "image", text,
        x: LB.pending ? LB.pending.x : 0.5, y: LB.pending ? LB.pending.y : 0.06,
        path: LB.pending?.path || null });
      toast("改造意见已记下——可继续打点提下一条，重新生成时全部带上");
      LB.pending = null;
      await reloadLBContext(it);
    } catch (err) { toast(err.message, true); }
  };
  inp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    // ⌘/Ctrl+Enter = 开始改造（连提交带重画）；提交意见只走点击
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startWithNotes();
    if (e.key === "Escape" && LB.pending) { LB.pending = null; renderPins(); }
  });
  // 「开始改造」＝把攒下的意见（含输入框里没发的这条）全部带上，整镜重新生图
  // 无意见（列表空且输入框空）时禁用——空跑重生没有修正依据
  const hasNotes = () => lbComments(it).length > 0 || !!inp.value.trim();
  const startWithNotes = async () => {
    if (!hasNotes()) return toast("先提至少一条改造意见（打点/划线后描述）", true);
    if (inp.value.trim()) await submitNote();
    lbRegen(it);
  };
  const goBtn = h("button", { class: "lb-refine-go",
    dataset: { tip: "开始改造\n把已记下的全部意见（含输入框里没发的这条）自动编译进"
      + "提示词，整镜重新生成；旧版归档可回滚。快捷键 ⌘/Ctrl+Enter。" },
    onclick: startWithNotes }, "开始改造 ⏎");
  if (slot) slot.append(h("div", { class: "lb-refine col" },
    inp,
    h("div", { class: "lb-refine-foot" },
      h("span", { class: "lb-refine-tag" }, noteTag),
      h("div", { class: "lb-foot-btns" },
        h("button", { class: "lb-refine-go",
          dataset: { tip: "提交意见 · 只记录不生成\n意见连同钉点/笔迹入库攒着（可连续提"
            + "多条，零成本）；「开始改造」或「↻ 重新生成」时全部自动编译进提示词。" },
          onclick: submitNote }, "提交意见"),
        goBtn))));
  const syncGo = () => { goBtn.disabled = !hasNotes(); };
  inp.addEventListener("input", syncGo);
  syncGo();
  if (LB.pending) setTimeout(() => inp.focus(), 30);
  drawStrokes();
}
async function reloadLBContext(it) {
  if (it.ctx) {
    const d = await api(`/api/chapter?project=${encodeURIComponent(it.ctx.pid)}&id=${encodeURIComponent(it.ctx.cid)}`);
    const s = (d.shots || []).find((x) => String(x.id) === String(it.ctx.shot));
    if (s) it.ctx.comments = s.comments || [];
    STATE.chapSig = "";   // 让章节视图下次轮询时重绘
  } else if (it.actx) {                       // 设定图：从项目详情重取该资产的意见
    const d = await api(`/api/project?id=${encodeURIComponent(it.actx.pid)}`);
    it.actx.comments = _assetComments(d, it.actx);
  }
  renderPins();
}
/* 意见池分派：与后端 `actions._asset_comment_pool` 同判据（kind 定字段、name 定实体）。
   场景的基准图与俯视图各有一池——共用一池的话，「墙的位置画错了」这类只对图纸成立的
   批注会被 `regen_asset` 编译进基准图的重生指令。 */
function _assetComments(d, actx) {
  if (!d) return [];
  const isTop = actx.kind === "topview";
  if (!actx.name && (actx.kind === "scene" || isTop))
    return (isTop ? d.scene_topview_comments : d.scene_comments) || [];
  const pool = actx.kind === "character" ? d.characters
    : (actx.kind === "scene" || isTop) ? d.scenes : d.props;
  const a = (pool || []).find((x) => x.name === actx.name);
  return (a && (isTop ? a.topview_comments : a.comments)) || [];
}
async function cmtUpdate(it, id, patch) {
  try {
    await post("/api/comment/update", { ...lbTarget(it), comment_id: id, ...patch });
    await reloadLBContext(it);
  } catch (err) { toast(err.message, true); }
}
/* 滚动接力（检查器范式增强）：页面滚到底后继续下滚 → 左栏自动续滚；
   在底部回滚时先把左栏滚回去再放行页面——无需把鼠标挪到左栏，交接顺滑对称 */
window.addEventListener("wheel", (e) => {
  const side = document.querySelector(".console-side");
  if (!side || side.scrollHeight <= side.clientHeight + 1) return;
  if (e.target.closest?.(
    ".console-side, .lightbox, .rf-overlay, .us-menu, .vpanel, .palette, .cinema")) return;
  const doc = document.documentElement;
  const atBottom = window.innerHeight + window.scrollY >= doc.scrollHeight - 2;
  if (!atBottom) return;
  if (e.deltaY > 0) {                       // 底部继续下滚 → 接力给左栏
    const before = side.scrollTop;
    side.scrollTop += e.deltaY;
    if (side.scrollTop !== before) e.preventDefault();
  } else if (e.deltaY < 0 && side.scrollTop > 0) {   // 底部回滚 → 先还左栏
    side.scrollTop += e.deltaY;
    e.preventDefault();
  }
}, { passive: false });

function stepLightbox(d) {
  LB.idx = (LB.idx + d + LB.items.length) % LB.items.length;
  LB.pending = null;
  LB.refine = null;
  renderLightbox();
}
function closeLightbox() {
  $("#lightbox").hidden = true;
  $("#lb-img").src = "";
  LB.pending = null;
  LB.refine = null;
  document.body.style.overflow = "";
}

/* 放映厅：NOW PLAYING = 标题 + 规格 chips + 实时技术读出（时间码/分辨率/码率
   由播放器 metadata 回填，时间码随播放跳动——放映间的机房仪表感）+ 台账行 */
function openCinema({ video, poster, title, rows = [], chips = [], size = null, link }) {
  const v = $("#cin-video");
  v.src = video; if (poster) v.poster = poster;
  const meta = $("#cin-meta");
  meta.innerHTML = "";
  meta.append(h("span", { class: "k" }, "NOW PLAYING"), h("h3", null, title || ""));
  const chipEls = chips.filter(Boolean);
  if (chipEls.length) meta.append(h("div", { class: "chips cin-chips" }, chipEls));
  const rowk = (k2, el) => h("div", { class: "rowk" }, h("span", null, k2), el);
  const tc = h("code", { class: "cin-tc" }, "0:00 / —");
  const res = h("code", null, "…");
  const br = size ? h("code", null, "…") : null;
  meta.append(rowk("时间码 · TC", tc), rowk("分辨率", res));
  if (br) meta.append(rowk("码率", br));
  rows.filter(([, val]) => val != null && val !== "").forEach(([k2, val]) => {
    // 长字符串值上下堆叠（.rowk.stack）：标签独占一行、内容整宽换行；
    // DOM 节点值默认并排，带 .cin-stack 类的节点显式要求堆叠
    // ——长文节点（实发提示词）并排会把标签压成竖排单字
    const stack = val.nodeType ? val.classList.contains("cin-stack")
                               : String(val).length > 24;
    meta.append(h("div", { class: "rowk" + (stack ? " stack" : "") },
      h("span", null, k2), h("code", null, val.nodeType ? val : String(val))));
  });
  v.onloadedmetadata = () => {
    res.textContent = v.videoWidth ? `${v.videoWidth} × ${v.videoHeight}` : "—";
    if (br && v.duration) br.textContent = `${(size * 8 / v.duration / 1e6).toFixed(1)} Mbps`;
    tc.textContent = `0:00 / ${fmtDur(v.duration)}`;
  };
  v.ontimeupdate = () => {
    tc.textContent = `${fmtDur(v.currentTime)} / ${fmtDur(v.duration || 0)}`;
  };
  if (link) {
    meta.append(h("a", { class: "ghost-btn cin-open", href: link,
      onclick: () => closeCinema() }, "打开章节制作台 →"));
  }
  $("#cinema").hidden = false;
  document.body.style.overflow = "hidden";
  v.play().catch(() => {});
}
function closeCinema() {
  const v = $("#cin-video");
  v.pause(); v.removeAttribute("src"); v.load();
  v.onloadedmetadata = v.ontimeupdate = null;   // 单例播放器，换片清读出钩子
  $("#cinema").hidden = true;
  document.body.style.overflow = "";
}

/* —— 模块导出 —— */
export { AudioBus, EFX_CLR, LB, WM_CORNERS, _assetComments, _lbNorm, _lbStroke, audioPill,
         charInfo, closeCinema, closeLightbox, cmtUpdate, cornerPicker, directorCard,
         drawStrokes, effectChip, effectsBtn, efxCatalog, efxMeta, lbComments, lbCtx, lbRegen,
         lbTarget, motionBadge, openCinema, openEffectsDialog, openLightbox,
         openWatermarkPanel, profileChip, raiseModal, rebuildBtn, regionLabel, reloadLBContext,
         renderLightbox, renderPins, renderRect, secHeader, secMore, skillChip, statusPill,
         stepLightbox, submitRefine, titledChip, toggleRefine, watermarkBtn };
