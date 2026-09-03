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

/* ═ Studio 前端模块 · app/config.js — 模型配置中心（原生 ES Module·免构建）═
   配置这台机器用哪家模型、端点填什么、密钥存哪里，以及生图/生视频/生配音各激活谁。
   写路径与命令行 `config` 动词族同源，落 config/models.local.json 并上行入库；
   没配过的一律回落 config/models.yaml。

   版式取「路由 + 服务商」两层，而不是把十二张表单一次铺开：
     · 上层「能力路由」回答**这台机器现在用谁**——四张牌一眼看完，这是用户来这一页
       最常问的问题；
     · 下层「服务商」是凭据与端点的货架，只呈现状态（有没有配、贵不贵、端点是谁），
       编辑收进右侧抽屉。十二张卡同时展开输入框，等于把"看状态"和"改配置"两件事
       挤在同一屏，两件都做不好。

   两条纪律贯穿本文件：
     · **密钥只写不读**——没有任何接口回读明文，输入框恒空、填过也不回显，
       界面上只呈现「在哪一层配过」的三态。
     · **不自己算合并**——保存后直接用后端回的生效视图重渲，避免出现
       「页面显示的值 ≠ 真正发出去的值」。 */
import { api, h, post, rich, toast } from "./core.js";
import { vendorGlyph, vendorMark } from "./brands.js";
import { chip, openShell, runBusy, uiSelect } from "./components.js";
import { secHeader } from "./widgets.js";

/* 能力清单、中文名与英文角标由后端 `config_overlay.CAPABILITY_META` 经
   `DATA.capabilities` 下发（每项 `{id, zh, en}`），前端不另存一份按 id 索引的名称表。
   这里只按 id 配一枚线框图：沿用侧栏导航那套 16×16 语汇，不引第二种图形语言。
   图是牌面的装饰，不是数据——没配图的能力照常出牌，只是无图。 */
const GLYPH = {
  image: '<svg viewBox="0 0 16 16"><rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6"/><circle cx="5.6" cy="6.4" r="1.1"/><path d="M2.4 11.6l3.2-3 2.4 2.2 2.6-2.8 3 3.6"/></svg>',
  video: '<svg viewBox="0 0 16 16"><rect x="1.6" y="3.4" width="9.2" height="9.2" rx="1.6"/><path d="M10.8 7.2l3.6-2.2v6l-3.6-2.2z"/></svg>',
  tts: '<svg viewBox="0 0 16 16"><rect x="5.6" y="1.6" width="4.8" height="8" rx="2.4"/><path d="M3.2 7.6a4.8 4.8 0 0 0 9.6 0M8 12.4v2"/></svg>',
  music: '<svg viewBox="0 0 16 16"><path d="M6 12V3.2l7.2-1.4V10"/><circle cx="4.2" cy="12.2" r="1.9"/><circle cx="11.4" cy="10.2" r="1.9"/></svg>',
  lipsync: '<svg viewBox="0 0 16 16"><path d="M1.8 8c2.2-2.9 3.9-3.9 6.2-2.6 2.3-1.3 4-.3 6.2 2.6-2.2 2.9-4.2 4.1-6.2 4.1S4 10.9 1.8 8z"/><path d="M1.8 8h12.4"/></svg>',
};
const KEY_META = {
  env: { zh: "环境变量", cls: "ok", tip: "由 export 设置，优先级最高" },
  local: { zh: "本机已配", cls: "ok", tip: "存在 config/secrets.local.json（不入库、不提交）" },
  file: { zh: "已配", cls: "ok", tip: "来自 config/secrets.yaml" },
  unset: { zh: "缺密钥", cls: "bad", tip: "该服务商需要密钥才能调用" },
  none: { zh: "免密钥", cls: "muted", tip: "本地端点或无需鉴权" },
};
/* 有降级分支的服务商（如无密钥自动改用本地曲库）缺密钥不是故障状态——
   标红会把一个刻意的设计报成故障。 */
const keyMeta = (p) => (p.key.state === "unset" && p.key.optional)
  ? { zh: "未配 · 自动降级", cls: "muted", tip: p.key.degrade }
  : (KEY_META[p.key.state] || KEY_META.unset);
/* 连接字段。刻意是固定一小组而不是把整段连接表铺开——连接表里还有鉴权变量名之类
   不该在这里随手改的东西，后端另有同一份白名单把关。 */
const FIELDS = [
  ["base_url", "接口地址", "以 API 版本号结尾，如 …/v1、…/api/v3", "wide"],
  // 通栏：模型 ID 是各家最长的一个串（doubao-seedance-2-0-mini-260615 这种），
  // 半栏放不下要么被输入框截住、要么贴着边缘读不出尾号
  ["model", "模型串", "厂商的模型 ID", "wide"],
];
const PRICE = [
  ["price_per_image", "每张", "元"],
  ["price_per_image_hd", "每张 · 超阈值像素", "元"],
  ["price_per_second", "每秒", "元"],
  ["price_per_second_4k", "每秒 · 4K", "元"],
  ["price_per_kchar", "每千字符", "元"],
  ["price_per_min", "每分钟", "元"],
  ["price_per_track", "每首", "元"],
];

let DATA = null;
let MOUNT = null;
const UI = { filter: "all", kw: "" };

/* ---------------- 小件 ---------------- */
const glyph = (id) => GLYPH[id] ? h("span", { class: "cfg-ico", html: GLYPH[id] }) : null;
const vendorText = (vendor, label) => [vendor, label].filter(Boolean).join(" · ") || null;
/* 品牌小标只出现在**没有大标可看**的地方（路由牌、选项行）。卡片与抽屉头部
   左边已经立着 38px 的品牌标，再在厂商名前重复一枚就只是噪点。 */
const vendorLine = (vendor, label) => {
  const txt = vendorText(vendor, label);
  return txt ? [vendorGlyph(vendor), txt] : null;
};

/* 状态是这一页的主要信息，统一收在这里：卡片、抽屉、路由牌读同一份判据 */
function statusOf(p) {
  if (p.status !== "ready") return { cls: "off", zh: "未接入" };
  if (p.key.state === "unset" && p.key.optional) return { cls: "muted", zh: "自动降级" };
  if (p.key.state === "unset") return { cls: "bad", zh: "缺密钥" };
  if (p.overridden.length) return { cls: "warn", zh: "本机已改" };
  return { cls: "ok", zh: "就绪" };
}

const dot = (cls) => h("span", { class: "cfg-dot " + cls });

function money(p) {
  const e = Object.entries(p.price || {}).filter(([, v]) => v);
  if (!e.length) return null;
  const [k, v] = e[0];
  const unit = { price_per_image: "/张", price_per_image_hd: "/张", price_per_second: "/秒",
                 price_per_kchar: "/千字", price_per_min: "/分钟",
                 price_per_track: "/首" }[k] || "";
  return `¥${v}${unit}`;
}

function fieldRow(label, ctrl, hint, cls) {
  return h("div", { class: "cfg-f" + (cls ? " " + cls : "") },
    h("label", null, label, hint ? h("i", null, hint) : null), ctrl);
}
const input = (v, ph, type) => h("input", {
  class: "cfg-in", type: type || "text", spellcheck: "false",
  value: v == null ? "" : v, placeholder: ph || "" });
/* 输入框与下拉的取值口径。`uiSelect` 的 value 走 defineProperty，取到的是裸值
   （无匹配项时是 undefined、也没有 .trim），直接当字符串用会在保存那一刻炸。 */
const val = (el) => String(el.value == null ? "" : el.value).trim();

/* 档位下拉（分辨率 / 画质档）：字段名与选项全部由后端按适配器给出——前端不认识
   「哪家厂商有哪些档」，加 provider 时也就没有一处前端要跟着改。 */
function gradeRow(g, bind, overridden) {
  const opts = [{ value: "", label: "跟随配置文件" }].concat(
    g.options.map((o) => ({ value: o.value, label: o.label + (o.caveat ? " ⚠" : "") })));
  // 目录之外的当前值原样留成一档：可能是厂商刚开的新档，也可能是这台机器手填的，
  // 两种都不该被下拉悄悄替换掉
  if (!g.in_catalog) {
    opts.push({ value: g.current, label: `${g.current} · 当前值（不在本机档位表内）` });
  }
  const sel = uiSelect(opts, { value: g.current, placeholder: "跟随配置文件" });
  const note = h("i", null, "");
  const paint = () => {
    const hit = g.options.find((o) => o.value === sel.value);
    // 「本机已改」是**状态**、档位提醒是**内容**，两者必须并存：让状态短路掉内容，
    // 档位警告在本机改过值的形态下就不可见了——而那正是最需要它的形态
    const tip = hit && hit.caveat ? hit.caveat
      : sel.value && !hit ? "这一档不在本机档位表内——照发不拦，由厂商裁决"
      : g.hint;
    const txt = overridden ? `本机已改 · ${tip}` : tip;
    // 提示行按单行省略排版（与其余字段一致），而档位提醒往往整句都是要紧的，
    // 故同时挂 title 让悬停看得到全文
    note.textContent = txt;
    note.title = txt;
  };
  sel.addEventListener("change", paint);
  paint();
  // 整行通栏：档位提醒是整句话（「mini 不支持，这一档归 seedance-2.5」这种），
  // 挤在半栏里会把字段名压成两行、提醒本身也被截掉一半
  return h("div", { class: "cfg-f wide" }, h("label", null, g.label, note),
    bind(g.field, sel));
}

/* ---------------- 能力路由（上层） ---------------- */
function routeTile(c) {
  const cap = c.id;
  const alias = DATA.active[cap];
  const p = (DATA.providers || []).find((x) => x.alias === alias);
  const byOverlay = DATA.activated_by[cap] === "overlay";
  const st = p ? statusOf(p) : { cls: "off", zh: "未配置" };
  const dev = (DATA.profile_deviations || []).filter((d) => d.capability === cap);
  const tile = h("button", { class: "cfg-tile" + (byOverlay ? " on" : ""), type: "button",
      onclick: () => openRouteDrawer(c) },
    h("div", { class: "cfg-tile-h" }, glyph(cap),
      h("b", null, c.zh), h("i", null, c.en),
      byOverlay ? h("span", { class: "cfg-pin", title: "本机激活（覆盖配置文件）" }, "●") : null),
    h("div", { class: "cfg-tile-m" },
      h("code", null, alias || "—"),
      p && p.vendor ? h("small", null, vendorGlyph(p.vendor), p.vendor) : null),
    h("div", { class: "cfg-tile-f" }, dot(st.cls), h("span", null, st.zh),
      dev.length ? h("em", { title: dev.map((d) => d.profile).join("、") },
        `${dev.length} 个画风例外`) : null));
  return tile;
}

function openRouteDrawer(c) {
  const cap = c.id;
  const list = (DATA.providers || []).filter((p) => p.kind === cap);
  let pick = DATA.activated_by[cap] === "overlay" ? DATA.active[cap] : "";
  const rows = [];
  const paintPick = () => rows.forEach((r) => r.classList.toggle("on", r.dataset.v === pick));
  const opt = (val, title, sub, right) => {
    const r = h("button", { class: "cfg-opt", type: "button", dataset: { v: val },
        onclick: () => { pick = val; paintPick(); } },
      h("span", { class: "cfg-opt-r" }),
      h("div", null, h("b", null, title), sub ? h("small", null, sub) : null),
      right || null);
    rows.push(r);
    return r;
  };
  const dev = (DATA.profile_deviations || []).filter((d) => d.capability === cap);

  drawer({
    mark: h("span", { class: "cfg-mark" }, glyph(cap)),
    title: c.zh, sub: `能力路由 · ${c.en}`,
    body: [
      h("p", { class: "cfg-note" },
        "这一档由哪个服务商承担。留在「跟随配置文件」时用 config/models.yaml 里的默认值；"
        + "选定某个服务商即写入本机覆盖层，命令行与网页共用同一份。"),
      h("div", { class: "cfg-opts" },
        opt("", "跟随配置文件",
            `当前为 ${(DATA.active[cap] || "—")}`, chip("默认", "")),
        list.map((p) => {
          const st = statusOf(p);
          const m = money(p);
          return opt(p.alias, p.alias, vendorLine(p.vendor, p.label),
            h("span", { class: "cfg-opt-x" }, m ? h("small", null, m) : null,
              dot(st.cls)));
        })),
      dev.length ? h("div", { class: "cfg-warn" },
        h("b", null, "这些画风不受此处影响"),
        h("p", null, "它们在自己的能力块里写了 provider，解析时优先级更高：",
          dev.map((d) => `${d.profile}(${d.provider})`).join("、"))) : null,
    ],
    actions: (close) => [
      h("button", { class: "dlg-btn", onclick: close }, "取消"),
      actBtn("primary", "保存", async () => {
        const r = await post("/api/config/set", { defaults: { [cap]: pick } });
        close(); applyView(r);
        toast(`✓ ${c.zh} → ${pick || "跟随配置文件"}`);
      }),
    ],
    onOpen: paintPick,
  });
}

/* ---------------- 服务商卡（下层） ---------------- */
function providerCard(p) {
  const st = statusOf(p);
  const m = money(p);
  return h("button", { class: "cfg-pv", type: "button", // 别名可能带点（seedance-2.5），而点在 CSS 选择器里是类选择符——
      // 这个 id 是给深链与实机测试用的锚点，转义掉省得日后按它 querySelector 时出错
      id: `cfg-${p.alias.replace(/[^\w-]/g, "_")}`,
      dataset: { kind: p.kind || "", state: st.cls },
      onclick: () => openProviderDrawer(p) },
    vendorMark(p),
    h("div", { class: "cfg-pv-b" },
      h("div", { class: "cfg-pv-t" },
        h("b", null, p.alias),
        p.overridden.length ? h("span", { class: "cfg-tag" }, "本机") : null),
      h("div", { class: "cfg-pv-s" },
        vendorText(p.vendor, p.label) || `impl=${p.impl}`),
      p.base_url ? h("div", { class: "cfg-pv-u" }, p.base_url) : null),
    h("div", { class: "cfg-pv-r" },
      m ? h("small", { class: "cfg-price" }, m) : null,
      h("span", { class: "cfg-state " + st.cls }, dot(st.cls), st.zh)));
}

function openProviderDrawer(p) {
  const inputs = {};
  const seeded = {};
  const bind = (k, el) => { inputs[k] = el; seeded[k] = val(el); return el; };
  const st = statusOf(p);
  const km = keyMeta(p);
  const probeBox = h("div", { class: "cfg-probe" });
  let save;

  const dirty = () => Object.entries(inputs).some(([k, el]) => val(el) !== seeded[k]);
  const sync = () => { if (save) save.disabled = !dirty(); };

  const conn = FIELDS.map(([k, zh, hint, cls]) =>
    fieldRow(zh, bind(k, input(p[k], "跟随配置文件")),
      p.overridden.includes(k) ? "本机已改" : hint, cls));
  // 档位那一格由后端给不给 grade 块决定，**绝不按能力硬猜**：「kind===video
  // 就显示分辨率」这类判据在字段名不叫 resolution 的服务商那里，会显示
  // 一格它根本不读的输入框
  if (p.grade) conn.push(gradeRow(p.grade, bind, p.overridden.includes(p.grade.field)));
  const prices = PRICE
    .filter(([k]) => p.price[k] != null || p.overridden.includes(k))
    .map(([k, zh, unit]) => fieldRow(zh, bind(k, input(p.price[k], "0")), unit, ""));
  // 输入框发 input、uiSelect 发 change——只听一种，另一种控件改了保存钮永远是灰的
  Object.values(inputs).forEach((el) => {
    el.addEventListener("input", sync);
    el.addEventListener("change", sync);
  });


  drawer({
    mark: vendorMark(p),
    title: p.alias, sub: vendorText(p.vendor, p.label),
    badges: [chip(p.kind || "?", ""), h("span", { class: "cfg-state " + st.cls },
      dot(st.cls), st.zh)],
    body: [
      sec("连接", "CONNECTION", h("div", { class: "cfg-form" }, conn),
        `适配器 impl=${p.impl}`),
      // 计费的字段数各家不同（视频两条、图像/配音各一条），按实际条数分栏：
      // 固定两栏时单条会占半格、右边空着一大片，读起来像少填了什么
      prices.length ? sec("计费", "PRICING",
        h("div", { class: "cfg-form cfg-cols",
                   style: `--cols:${Math.min(prices.length, 3)}` }, prices),
        "填 0 或留空 = 不入成本台账，预算闸对它不生效") : null,
      sec("密钥", "CREDENTIAL", keyRows(p), km.tip),
      sec("自检", "PROBE",
        h("div", null,
          h("div", { class: "cfg-probe-acts" },
            actBtn("", "运行自检", async () => {
              const r = await post("/api/config/test", { provider: p.alias });
              paintProbe(probeBox, (r.results || [])[0]);
            }),
            // 控制台入口按 impl 下发（同是火山，方舟与语音是两个控制台）——
            // 后端没给就不渲染，本地类服务商没有「官网」可去
            p.console ? h("a", { class: "dlg-btn cfg-console", href: p.console,
                target: "_blank", rel: "noreferrer" }, "打开控制台 ↗") : null),
          probeBox),
        "零成本：只查解析层，一个生成请求都不发"),
    ],
    actions: (close) => {
      save = actBtn("primary", "保存", async () => {
        const patch = {};
        for (const [k, el] of Object.entries(inputs)) {
          const v = val(el);
          if (v !== seeded[k]) patch[k] = v;    // 空串 = 清除该字段的本机覆盖
        }
        const r = await post("/api/config/set", { providers: { [p.alias]: patch } });
        close(); applyView(r);
        toast(`✓ ${p.alias} 已保存：${Object.keys(patch).join("、")}`);
      });
      save.disabled = true;
      return [
        p.overridden.length
          ? actBtn("ghost", "恢复默认", async () => {
              const r = await post("/api/config/set", { providers: { [p.alias]: null } });
              close(); applyView(r);
              toast(`↺ ${p.alias} 已回落 config/models.yaml`);
            })
          : null,
        h("span", { class: "cfg-sp" }),
        h("button", { class: "dlg-btn", onclick: close }, "关闭"),
        save,
      ];
    },
  });
}

/* 输入框预填的是**当前生效值**（多半来自 config/models.yaml）。保存时只提交用户
   真的改过的字段——把预填值原样回传，等于把此刻的 yaml 值全部冻进本机覆盖层：
   日后配置文件升级了模型串或调了价，这台机器还钉在旧值上，而界面上看不出异常。 */

function paintProbe(box, r) {
  box.innerHTML = "";
  if (!r) return;
  box.append(h("div", { class: "cfg-probe-l" }, r.checks.map((c) =>
    h("div", { class: "cfg-chk" + (c.ok ? "" : " bad") },
      h("span", { class: "cfg-chk-i" }, c.ok ? "✓" : "✕"),
      h("span", { class: "cfg-chk-n" }, c.name),
      c.detail ? h("small", null, c.detail) : null))));
}

/* ---------------- 密钥（只写不读） ----------------
   多凭证厂商不止一把钥匙（MiniMax 的 GROUP_ID、火山 TTS 的旧版双头）。
   只渲染主 key 的话，缺第二把的厂商在界面上一片正常、真跑才炸，而网页上又没有
   任何地方能补上——所以按后端下发的 keys[] 逐个渲染。 */
function keyRows(p) {
  const slots = (p.keys && p.keys.length) ? p.keys : (p.key.env ? [p.key] : []);
  if (!slots.length) return h("div", { class: "cfg-key" }, h("code", null, "该服务商无需密钥"));
  return h("div", { class: "cfg-keys" }, slots.map((k, i) => {
    const meta = (i === 0) ? keyMeta(p) : (KEY_META[k.state] || KEY_META.unset);
    return h("div", { class: "cfg-key" },
      h("code", null, k.env),
      h("span", { class: "cfg-state " + meta.cls }, dot(meta.cls), meta.zh),
      h("button", { class: "dlg-btn", type: "button",
        onclick: () => openSecretDialog(p, k.env) },
        k.state === "unset" ? "填写" : "更换"));
  }));
}

function openSecretDialog(p, env) {
  env = env || p.key.env;
  // 密钥框恒空：后端没有任何回读明文的出口，这里也不做「显示已存的值」。
  const inp = h("input", { class: "cfg-in", type: "password", autocomplete: "new-password",
                           placeholder: "粘贴密钥" });
  openShell({
    build: (close) => [
      h("span", { class: "k" }, `密钥 · ${env}`),
      h("p", { class: "dlg-msg" },
        "只写不读：写进本机密钥文件（不提交、不入库、不回显）。"
        + "若同名环境变量已设置，它的优先级更高。"),
      p.console ? h("p", { class: "dlg-msg" }, "还没有这把密钥？先去",
        h("a", { class: "cfg-console-a", href: p.console, target: "_blank",
                 rel: "noreferrer" }, "服务商控制台 ↗"), "创建。") : null,
      inp,
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "取消"),
        actBtn("danger", "清除", async () => {
          const r = await post("/api/config/secret", { env, value: "" });
          close(); applyView(r.config); toast(`✓ ${env} 已清除`);
        }),
        actBtn("primary", "保存", async () => {
          if (!inp.value.trim()) return toast("请先粘贴密钥", true);
          const r = await post("/api/config/secret", { env, value: inp.value });
          close(); applyView(r.config); toast(`✓ ${env} 已写入本机`);
        })),
    ],
  });
  setTimeout(() => inp.focus(), 30);
}

/* ---------------- 自定义接入 ---------------- */
function openNewAliasDrawer() {
  const alias = input("", "如 minimax-h3");
  const capSel = uiSelect((DATA.capabilities || []).map((c) => ({ value: c.id, label: c.zh })),
    { value: "video" });
  const base = input("", "https://…/v1");
  const model = input("", "厂商的模型 ID");
  const keyEnv = input("", "如 MINIMAX_API_KEY");
  // 适配器选择器随能力重建：适配器是按 (能力, impl) 注册的，换了能力，上一档的
  // 选项一个都不合法。整只重建而不是改选项，是因为 uiSelect 的取值挂在实例上。
  const implBox = h("div", { class: "cfg-slot" });
  let implSel = null;
  const paintImpl = () => {
    const opts = (DATA.adapters || []).filter((a) => a.capability === capSel.value)
      .map((a) => ({ value: a.impl,
                     label: [a.impl, [a.vendor, a.label].filter(Boolean).join(" ")]
                       .filter(Boolean).join(" · ") }));
    implSel = uiSelect(opts, { value: opts[0] && opts[0].value, placeholder: "选适配器" });
    implBox.replaceChildren(implSel);
  };
  drawer({
    mark: h("span", { class: "cfg-mark" }, "＋"),
    title: "自定义接入", sub: "新增一个 provider 别名",
    body: [
      h("p", { class: "cfg-note" }, rich(
        "新别名必须指向一个**已实现的适配器**——网页能做的是「换端点、换模型、换计费」，"
        + "凭空接一家没有适配器的厂商做不到，那需要在 providers/ 下写一个适配器并登记一行。"
        + "本地自托管端点也走这里：填你自己的地址即可。")),
      sec("基本", "BASIC", h("div", { class: "cfg-form" },
        fieldRow("别名", alias, "小写字母开头，词间用连字符", ""),
        fieldRow("能力", capSel, "决定它出现在哪一档路由里", ""),
        fieldRow("适配器", implBox, "请求怎么拼由它决定", "wide")), null),
      sec("连接", "CONNECTION", h("div", { class: "cfg-form" },
        fieldRow("接口地址", base, "以 API 版本号结尾", "wide"),
        fieldRow("模型串", model, "", ""),
        fieldRow("密钥变量名", keyEnv, "只写变量名，密钥另填", "")), null),
    ],
    actions: (close) => [
      h("span", { class: "cfg-sp" }),
      h("button", { class: "dlg-btn", onclick: close }, "取消"),
      actBtn("primary", "创建", async () => {
        const name = alias.value.trim();
        // 与命名规范守卫 `TestProviderNamingConvention.ALIAS_RE` 逐字一致：别名用
        // 连字符（与各家模型 ID 同源），下划线留给 impl（它必须与 Python 模块名同名）。
        // 两边分叉的话，网页能建出一个守卫判为非法的别名
        if (!/^[a-z][a-z0-9]*(?:[-.][a-z0-9]+)*$/.test(name)) {
          return toast("别名请用小写字母开头，词间用连字符（如 minimax-h3）", true);
        }
        if (!implSel || !implSel.value) return toast("请先选适配器", true);
        const patch = { kind: capSel.value, impl: implSel.value, status: "ready",
                        base_url: base.value.trim(), model: model.value.trim() };
        if (keyEnv.value.trim()) patch.api_key_env = keyEnv.value.trim().toUpperCase();
        const r = await post("/api/config/set", { providers: { [name]: patch } });
        close(); applyView(r); toast(`✓ 已新增 ${name}`);
      }),
    ],
    onOpen: paintImpl,
  });
  capSel.addEventListener("change", paintImpl);
}

/* ---------------- 抽屉骨架 ---------------- */
/* 走 openShell（`.dlg` 家族唯一开启入口，backdrop/Escape/退场动画只此一份），
   靠 `.cfg-drawer` 变体把居中卡片改成右侧全高抽屉——不另起一套弹层机制。 */
/* `mark` 收成一个现成的 `.cfg-mark` 元素而不是「底色 + 内容 + 是不是文字」三个参数：
   品牌标要带自己的品牌色，那已经不是三色令牌能表达的东西了。 */
function drawer({ mark, title, sub, badges, body, actions, onOpen }) {
  const close = openShell({
    card: "cfg-drawer",
    build: (cl) => [
      h("div", { class: "cfg-dh" }, mark,
        h("div", { class: "cfg-dh-t" },
          h("b", null, title), sub ? h("small", null, sub) : null),
        h("div", { class: "cfg-dh-b" }, badges || []),
        h("button", { class: "lb-x", onclick: cl, title: "关闭（Esc）" }, "✕")),
      h("div", { class: "cfg-db" }, body),
      h("div", { class: "cfg-df" }, actions(cl)),
    ],
  });
  if (onOpen) onOpen();
  return close;
}

function sec(zh, en, node, foot) {
  return h("section", { class: "cfg-sec" },
    h("div", { class: "cfg-sec-h" }, h("b", null, zh), h("i", null, en)),
    node, foot ? h("p", { class: "cfg-sec-f" }, foot) : null);
}

/* 按钮 + 忙态。还原与否由 runBusy 按「按钮是否还在文档里」自行判定，
   这里不必也不该逐个动作声明。 */
function actBtn(kind, label, fn) {
  const b = h("button", { class: "dlg-btn" + (kind && kind !== "ghost" ? " " + kind : ""),
    type: "button" });
  b.textContent = label;
  b.onclick = () => runBusy(b, "处理中…", fn, { group: ".cfg-drawer, .dlg-card" })
    .catch((e) => toast(e.message, true));
  return b;
}

/* ---------------- 页面 ---------------- */
function applyView(view) {
  if (view && view.providers) DATA = view;
  paint();
}

function statBar() {
  const ov = DATA.overlay;
  const provs = DATA.providers || [];
  const ready = provs.filter((p) => statusOf(p).cls === "ok").length;
  const cell = (k, v, tip) => h("div", { class: "cfg-stat", dataset: tip ? { tip } : {} },
    h("span", { class: "k" }, k), h("b", null, v));
  return h("div", { class: "cfg-bar" },
    cell("READY", `${ready}/${provs.length}`, `就绪 · 服务商\n可用 = 已接入且密钥已配`),
    cell("OVERRIDES", ov ? String(ov.providers.length) : "0",
      "本机覆盖\n改过连接段的服务商数量；其余全部跟随 config/models.yaml"),
    cell("ROUTES", ov ? String(ov.defaults.length) : "0",
      "本机激活\n由本机指定承担者的能力档数量"),
    h("div", { class: "cfg-bar-p" },
      h("div", null, h("span", { class: "k" }, "配置真源"),
        h("code", null, DATA.source || "—")),
      h("div", null, h("span", { class: "k" }, "本机覆盖层"),
        h("code", null, DATA.overlay_path || "（已关闭）"))));
}

function filterBar() {
  const counts = { all: (DATA.providers || []).length };
  for (const c of DATA.capabilities || []) {
    counts[c.id] = (DATA.providers || []).filter((p) => p.kind === c.id).length;
  }
  const mk = (v, zh) => h("button", {
    class: "cfg-fil" + (UI.filter === v ? " on" : ""), type: "button",
    onclick: () => { UI.filter = v; paint(); } },
    zh, h("i", null, String(counts[v] || 0)));
  const search = h("input", { class: "cfg-search", type: "search", value: UI.kw,
    placeholder: "筛选服务商 / 端点",
    oninput: (e) => { UI.kw = e.target.value.trim().toLowerCase(); paintGrid(); } });
  return h("div", { class: "cfg-filbar" },
    h("div", { class: "cfg-fils" }, mk("all", "全部"),
      (DATA.capabilities || []).map((c) => mk(c.id, c.zh))),
    search,
    actBtn("", "全部自检", async () => {
      const r = await post("/api/config/test", {});
      openProbeAll(r.results || []);
    }));
}

function openProbeAll(results) {
  const bad = results.filter((r) => !r.ok);
  openShell({
    card: "cfg-drawer",
    build: (cl) => [
      h("div", { class: "cfg-dh" },
        h("span", { class: "cfg-mark" }, "⛭"),
        h("div", { class: "cfg-dh-t" }, h("b", null, "全部自检"),
          h("small", null, `${results.length - bad.length}/${results.length} 项通过`)),
        h("button", { class: "lb-x", onclick: cl, title: "关闭（Esc）" }, "✕")),
      h("div", { class: "cfg-db" },
        h("p", { class: "cfg-note" },
          "零成本：只查解析层（别名/能力/适配器/状态/密钥/端点版本号/可实例化），"
          + "一个生成请求都不发，所以不会产生任何费用。"),
        results.map((r) => h("section", { class: "cfg-sec" },
          h("div", { class: "cfg-sec-h" },
            dot(r.ok ? "ok" : "bad"), h("b", null, r.alias)),
          h("div", { class: "cfg-probe-l" }, r.checks.filter((c) => !c.ok).map((c) =>
            h("div", { class: "cfg-chk bad" }, h("span", { class: "cfg-chk-i" }, "✕"),
              h("span", { class: "cfg-chk-n" }, c.name),
              c.detail ? h("small", null, c.detail) : null))),
          r.ok ? h("p", { class: "cfg-sec-f" }, "全部通过") : null))),
      h("div", { class: "cfg-df" }, h("span", { class: "cfg-sp" }),
        h("button", { class: "dlg-btn primary", onclick: cl }, "知道了")),
    ],
  });
}

let GRID = null;
function paintGrid() {
  if (!GRID) return;
  GRID.innerHTML = "";
  const list = (DATA.providers || []).filter((p) => {
    if (UI.filter !== "all" && p.kind !== UI.filter) return false;
    if (!UI.kw) return true;
    return [p.alias, p.vendor, p.label, p.base_url, p.impl]
      .filter(Boolean).join(" ").toLowerCase().includes(UI.kw);
  });
  list.forEach((p) => GRID.append(providerCard(p)));
  GRID.append(h("button", { class: "cfg-pv cfg-add", type: "button",
      onclick: openNewAliasDrawer },
    h("span", { class: "cfg-mark" }, "＋"),
    h("div", { class: "cfg-pv-b" },
      h("div", { class: "cfg-pv-t" }, h("b", null, "自定义接入")),
      h("div", { class: "cfg-pv-s" }, "指向已有适配器的新别名 · 也用于本地自托管端点"))));
  if (!list.length) {
    GRID.prepend(h("div", { class: "cfg-empty" }, "没有匹配的服务商"));
  }
}

function paint() {
  if (!MOUNT) return;
  MOUNT.innerHTML = "";
  GRID = h("div", { class: "cfg-grid" });
  MOUNT.append(
    // 抬头一行标题一行导语（导语不换行），统计条另起一条通栏带压在「能力路由」之上：
    // 挤在标题右侧时它只有半屏宽，两条路径全被省略号截掉——而"配置真源在哪"恰恰是
    // 这一页最该一眼看全的信息；通栏之后路径完整可读，三个计数也读作全页的总账。
    h("div", { class: "cfg-head" },
      h("h1", null, "模型配置"),
      h("p", null, "这台机器用哪家模型、端点与密钥填什么。没配的一律回落 ",
        h("code", null, "config/models.yaml"), "；密钥只存本机、不入库不下发。")),
    statBar(),
    secHeader("01", "能力路由", "ROUTING", null),
    // 列数随后端能力数走（CSS 只读 --caps），桌面宽度下恒一行看完
    h("div", { class: "cfg-tiles", style: `--caps:${(DATA.capabilities || []).length}` },
      (DATA.capabilities || []).map(routeTile)),
    secHeader("02", "服务商", "PROVIDERS", (DATA.providers || []).length),
    filterBar(), GRID);
  paintGrid();
}

async function viewConfig(view) {
  DATA = await api("/api/config");
  MOUNT = h("div", { class: "cfg-page" });
  view.append(MOUNT);
  paint();
}

export { viewConfig };
