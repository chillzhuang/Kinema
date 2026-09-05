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

/* ============================================================================
   kinema · Studio — SPA
   hash 路由：#/ 总览 · #/projects 项目 · #/project/<pid> 项目详情
             · #/project/<pid>/<cid> 章节制作台 · #/library 片库 · #/cost 成本
   数据全部来自后端 scanner API，零第三方依赖。
   ========================================================================== */

/* ---------------- 基础工具 ---------------- */
// 基座层零静态依赖（视图函数一律**动态 import 晚绑定**）：core 静态引视图会构成
// 环——视图模块先于 core 求值，其顶层 $() 撞 TDZ 白屏。模块有缓存，运行时零开销。

const $ = (s, r = document) => r.querySelector(s);

/* HTML 布尔属性只要出现即为真：`setAttribute("disabled", false)` 写出
   `disabled="false"`，那是禁用。故这几个键按真假决定设不设，`false` 一律不设。
   其余键原样 `setAttribute`——`spellcheck`/`contenteditable` 这类枚举属性的
   "false" 是有效值，不在此列。 */
const BOOL_ATTRS = new Set(["disabled", "hidden", "readonly", "checked",
                            "selected", "required", "open", "multiple", "autofocus"]);

function h(tag, attrs, ...children) {
  const e = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === "class") e.className = v;
      else if (k === "html") e.innerHTML = v;          // 仅用于内置常量，不用于数据
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(e.dataset, v);
      else if (BOOL_ATTRS.has(k)) { if (v !== false) e.setAttribute(k, ""); }
      else e.setAttribute(k, v);
    }
  }
  for (const c of children.flat(9)) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
}

/* 行内强调 `**…**` → `<b>`。提示条与弹层的文案是成段散文，作者写强调时自然按
   Markdown 落笔，而这些位置渲染的是纯文本——那两颗星就原样落在屏幕上。
   判据放在渲染处而不是逐条文案里：漏改一条就是一处星号。
   拼文本节点而非 innerHTML——这些文案会拼进用户数据（音色描述、文件名）。 */
function rich(text) {
  const frag = document.createDocumentFragment();
  String(text == null ? "" : text).split(/\*\*([^*]+)\*\*/g).forEach((part, i) => {
    if (part) frag.append(i % 2 ? h("b", null, part) : document.createTextNode(part));
  });
  return frag;
}

const ICON = {
  play: '<svg viewBox="0 0 16 16"><path d="M4 2.5v11l9-5.5z"/></svg>',
  pause: '<svg viewBox="0 0 16 16"><path d="M4 2.5h3v11H4zM9 2.5h3v11H9z"/></svg>',
  person: '<svg viewBox="0 0 32 32"><circle cx="16" cy="11" r="5.5"/><path d="M5 28c1.8-6 6-8.5 11-8.5S25.2 22 27 28"/></svg>',
};

const LABEL = {
  // profile 中文名由 models.yaml 的 label 字段经 /api/overview 下发（首拉后填充；
  // 加画风零前端改动），未下发前回退英文原名——单一真源在配置，不在这里
  profile: {},
  // skill 元数据（id → {cmd,label}）由 /api/overview 的 skills 目录下发（kinema/skills.py 单一真源）
  skill: {},
  platform: { douyin: "抖音", kuaishou: "快手", bilibili: "B站", xiaohongshu: "小红书",
              shipinhao: "视频号", weibo: "微博", youtube: "YouTube", tiktok: "TikTok",
              instagram: "Instagram" },
  status: { rendered: "已渲染", scripted: "已编剧", draft: "草稿", missing: "缺失",
            error: "异常", active: "进行中", archived: "已归档" },
};
const MOTION = {
  kenburns: { key: "A", name: "Ken Burns", desc: "静图运镜 · 零视频成本",
    tip: "渲染模式 A · Ken Burns\n静图八种缓动运镜（推/拉/平移/对角/微旋/呼吸，"
      + "按镜号轮换）＋TTS 配音＋BGM——零视频生成成本。" },
  native:   { key: "B", name: "Native",    desc: "模型原生音画",
    tip: "渲染模式 B · Native\n模型原生音画：视频模型自配人声/音效/环境音；"
      + "对白由模型发声并附音色锚定，旁白可显式混烧固定音色（native_voiceover），"
      + "曲库 BGM 只在 native_bgm 显式加铺；缺省全能参考（一镜一片·镜间直拼），"
      + "唯一支持首尾帧衔接（显式 frame_chain / --chain）的模式。" },
  dubbed:   { key: "C", name: "Dubbed",    desc: "图生视频 · 固定音色对口型",
    tip: "渲染模式 C · Dubbed\n图生视频对口型：调用视频模型，以我们的 TTS 音轨驱动口型"
      + "＋BGM——必须先完成配音；按视频秒数计费。" },
};
// 审阅状态机：产物级状态 → 徽章文案与配色。键集与文案由源级守卫钉死等于
// 引擎 review.STATES（test_frontend_integrity）——漏一态，徽章显示裸态名
const REVIEW = {
  wfa:    { zh: "待审", cls: "amber" },
  done:   { zh: "通过", cls: "green" },
  retake: { zh: "重做", cls: "red" },
  wip:    { zh: "生成中", cls: "blue" },
  todo:   { zh: "待办", cls: "" },
  omt:    { zh: "弃用", cls: "" },
};
const STAGE_ZH = { image: "图", audio: "音", clip: "片" };
// 转场类型中文名（转场卡与镜头表共用）
const TRANSITION_ZH = { seamless: "无缝转场", fade: "极简黑场", fade_black: "渐黑字卡",
                        fade_white: "白闪字卡", wipe: "对角翻页", circle: "圆形开合",
                        slide: "横向推移", blur: "柔焦叠化", scan: "轮廓扫描",
                        clip: "素材转场" };

const fmtDur = (s) => {
  if (s == null || isNaN(s)) return "—";
  s = Math.round(s);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const fmtSec = (s) => (s == null ? "—" : `${(+s).toFixed(1)}s`);
/* /media?path=… 形式的资源 URL → 本机绝对路径（指令台把文件定位交给 AI 用）。
   媒体已上云时是普通 http URL、无 path 参数 → 返回 null，调用方退回原样打印 URL。 */
const mediaPath = (url) => {
  try { return new URL(url, location.origin).searchParams.get("path"); }
  catch { return null; }
};
const fmtSize = (b) => (b == null ? "—" : `${(b / 1048576).toFixed(1)} MB`);
const fmtDate = (t) => {
  if (!t) return "—";
  const d = new Date((t > 1e12 ? t : t * 1000));
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const CUR = { CNY: "¥", USD: "$", EUR: "€" };
/* 服务端算好的台账合计（引擎 budget.spent_total 单一真源）→ 展示串。
   台账级数字一律走下发的 cost_total/cost_totals，前端不复算求和 */
const fmtCost = (total, currency) =>
  (total == null ? null : `${CUR[currency || "CNY"] || ""}${(+total).toFixed(2)}`);
function costTotal(cost) {
  if (!cost) return null;
  // 兜底求和只服务「手头只有 cost 分项 dict」的展示位（版本卡等零散小额）；
  // 有服务端合计可用的位置一律用 fmtCost(下发值)
  const sum = Object.entries(cost).reduce(
    (a, [k, v]) => a + (k !== "currency" && typeof v === "number" ? v : 0), 0);
  return fmtCost(sum, cost.currency);
}

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  const j = await r.json();
  // 全部 GET 与轮询都过这里——引擎错配贴条在此单点揭幕，视图层不必各自体检
  if (j && j.engine_stale) flagEngineStale();
  return j;
}

/* 引擎错配贴条：服务端只在盘上引擎代码领先于运行进程时注 engine_stale 键。
   揭开后重启前不收（刷新页面它会再被下一次响应揭开），点击复制重启命令。 */
function flagEngineStale() {
  const el = document.getElementById("engine-stale");
  if (!el || !el.hidden) return;
  el.hidden = false;
  el.onclick = () => navigator.clipboard?.writeText("kinema studio --restart")
    .then(() => toast("已复制：kinema studio --restart"));
}

/* 写 API：表态 / 评论 / 回滚（与 CLI 同一条写路径，文件+数据库自动同步）
   携带启动时注入的 CSRF token——服务端对全部 POST 强校验，防跨站触发付费操作 */
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || "";
/* 异步任务轮询：/api/job?id= 直到 done|failed（重新生成/改造台共用）。
   单次请求异常≠任务失败——任务在后台子进程里照常推进，网络抖动或服务
   重启窗口误报失败会诱导用户重复触发付费操作；连续多次联系不上（服务
   重启后任务表丢失也是这种表现）才交给 onFail。 */
function pollJob(jid, { onDone, onFail, interval = 1600, maxMisses = 8 } = {}) {
  let misses = 0;
  const tick = async () => {
    try {
      const j = await api(`/api/job?id=${encodeURIComponent(jid)}`);
      misses = 0;
      if (j.state === "running") return setTimeout(tick, interval);
      (j.state === "done" ? onDone : onFail)?.(j);
    } catch (e) {
      if (++misses < maxMisses) return setTimeout(tick, interval * 2);
      onFail?.({ tail: e.message });
    }
  };
  setTimeout(tick, interval);
}

/* 忙态真源（数据驱动）：进行中的生成任务 key=pid/cid/shot → {id, kind}。
   分镜卡渲染时据此画「生成中」遮罩与「图·生成中」徽章——遮罩是渲染的一部分
   而非一次性 DOM 叠加，章节 3s 轮询重绘、甚至刷新页面（viewChapter 会拉
   /api/jobs 对账）都不会丢。BUST 记录完成时间戳给图片 URL 做缓存击穿。 */
const GENJOBS = new Map();
const BUST = new Map();
const jobKey = (pid, cid, shot) => `${pid}/${cid}/${shot}`;
const JOB_ZH = { regen: "重新生成中", refine: "局部改造中", previz: "预演渲染中",
                 sketch: "简笔板生成中", clip: "视频生成中",
                 control_build: "深度处理中", control_v2v: "送 Seedance（深度）" };
function trackJob(key, jid, kind, pid, cid) {
  if (GENJOBS.has(key)) return;
  GENJOBS.set(key, { id: jid, kind });
  const shot = key.split("/").pop();
  pollJob(jid, {
    onDone: async () => {
      GENJOBS.delete(key);
      BUST.set(key, Date.now());
      toast(`镜 ${shot} ${kind === "refine" ? "局部改造" : "重新生成"}完成`);
      await softRefresh(pid, cid);
    },
    onFail: async (j) => {
      GENJOBS.delete(key);
      toast(`镜 ${shot} ${JOB_ZH[kind] || "生成"}失败：${(j.tail || "").slice(-160)}`, true);
      await softRefresh(pid, cid);
    },
  });
}
/* 任务收尾的软刷新：仍停在该章节才重渲（静默、免整页 reload），侧栏角标同步 */
async function softRefresh(pid, cid) {
  try {
    await getOverview(true);
    (await import("./shell.js")).renderRail(state.overview);
    const r = state.route || {};
    const same = (a, b) => a === b || decodeURIComponent(a || "") === b;
    if (r.name === "chapter" && same(r.pid, pid) && same(r.cid, cid))
      await (await import("./chapter.js")).viewChapter($("#view"), pid, cid, { silent: true });
  } catch { /* 静默 */ }
}
/* 项目页软刷新（设定图垫图/重生收尾用）：仍停在该项目页才重渲 */
async function softRefreshProject(pid) {
  try {
    await getOverview(true);
    (await import("./shell.js")).renderRail(state.overview);
    const r = state.route || {};
    const same = (a, b) => a === b || decodeURIComponent(a || "") === b;
    if (r.name === "project" && same(r.pid, pid)) {
      const view = $("#view"), tmp = h("div");
      await (await import("./project.js")).viewProject(tmp, pid);
      view.innerHTML = ""; view.append(...tmp.children);
    }
  } catch { /* 静默 */ }
}

async function post(path, body) {
  const r = await fetch(path, { method: "POST",
    headers: { "X-Csrf-Token": CSRF, "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) throw new Error(d.error || `${path} → ${r.status}`);
  return d;
}

/* 消息条（toast）：宿主容器 flex 居中——出现即在正位（若靠 transform 居中，
   入场关键帧会覆盖它造成「先偏后跳中」）；多条纵向堆叠（同屏最多 3 条）、
   同文案去重只续时、错误停留更久、点击即收、离场上浮淡出。API 不变 toast(msg, bad)。 */
function toastDismiss(el) {
  if (el.classList.contains("out")) return;
  clearTimeout(el._timer);
  el.classList.add("out");
  el.addEventListener("animationend", () => el.remove(), { once: true });
  setTimeout(() => el.remove(), 400);   // 动画事件丢失兜底
}
/* 后台递达：用户切走标签页时页内 toast 看不见——给标题加「●」亮点标记，
   标签栏上一眼可见"有任务收尾了"；切回页面由 app.js 的 visibilitychange 钩子
   复位。刻意只做标题标记不弹系统通知（零权限、零打扰）。 */
function notifyAway(msg, bad) {
  if (!document.hidden) return;
  if (!document.title.startsWith("● ")) document.title = "● " + document.title;
}
/* 缓存穿透串的唯一拼法：本地媒体是 /media?path=… 带 ?，云端直链（OSS）常无查询串——
   无条件拼 & 会把 `&t=` 变成路径的一部分（OSS 404 且 BUST 无清除，本会话内永久裂图）。 */
function withBust(url, t) {
  if (!url) return url;
  const base = String(url).split("&t=")[0].split("?t=")[0];
  return `${base}${base.includes("?") ? "&" : "?"}t=${t}`;
}

function toast(msg, bad = false) {
  notifyAway(msg, bad);                 // 完成回调全汇聚于此，一处接入全站覆盖
  const host = $("#toast-host");
  const ttl = bad ? 4200 : 2600;        // 错误要读完，停留更久
  const dup = [...host.children].find((x) => x._msg === msg && !x.classList.contains("out"));
  if (dup) { clearTimeout(dup._timer); dup._timer = setTimeout(() => toastDismiss(dup), ttl); return; }
  const t = h("div", { class: "toast" + (bad ? " bad" : ""),
      onclick: () => toastDismiss(t) },
    h("span", { class: "t-ico" }, bad ? "✕" : "✓"),
    h("span", { class: "t-msg" }, msg));
  t._msg = msg;
  while (host.children.length >= 3) host.firstChild.remove();
  host.append(t);
  t._timer = setTimeout(() => toastDismiss(t), ttl);
}

/* 系统级即时提示（tips）：元素挂 data-tip="标题\n正文" 即生效——
   委托监听零延迟显示（原生 title 有 1s 左右的浏览器延迟且样式不可控），
   首行加粗为标题、余下为正文；定位优先目标上方居中，越界自动翻转/夹取。
   另挂 data-tip-img="url" 则在文字上方内嵌缩略图（悬停即看图，点击才开大图）——
   图片浮层不另造组件，全站只有这一个悬浮体系。 */
const TIP = { el: null, cur: null };
function tipShow(target) {
  const raw = target.dataset.tip || "";
  if (!raw) return;
  if (!TIP.el) TIP.el = h("div", { id: "sys-tip" });
  // 全屏时（如图谱画布 ⛶）挂进全屏元素内，否则浏览器只渲染全屏子树、body 上的提示不显示
  const host = document.fullscreenElement || document.body;
  if (TIP.el.parentNode !== host) host.append(TIP.el);
  const [head, ...rest] = raw.split("\n");
  const body = rest.join("\n").trim();
  TIP.el.innerHTML = "";
  const img = target.dataset.tipImg
    ? h("img", { class: "tip-img", src: target.dataset.tipImg, alt: "" }) : null;
  if (img) TIP.el.append(img);
  if (body) TIP.el.append(h("b", null, rich(head)));
  TIP.el.append(h("p", null, rich(body || head)));
  TIP.cur = target;
  TIP.el.classList.remove("on");
  const place = () => {
    TIP.el.style.left = "0px"; TIP.el.style.top = "0px";   // 先归零量真实尺寸
    const r = target.getBoundingClientRect();
    const w = TIP.el.offsetWidth, ht = TIP.el.offsetHeight;
    const x = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8));
    const y = r.top - ht - 9 >= 8 ? r.top - ht - 9 : r.bottom + 9;   // 上方放不下翻到下方
    TIP.el.style.left = `${x}px`; TIP.el.style.top = `${y}px`;
  };
  place();
  // 图片高度要等加载完才知道，落盘后原地重排一次（仍悬停在同一目标才动）
  if (img && !img.complete) img.onload = () => { if (TIP.cur === target) place(); };
  requestAnimationFrame(() => TIP.el.classList.add("on"));
}
function tipHide() { TIP.cur = null; TIP.el?.classList.remove("on"); }
document.addEventListener("mouseover", (e) => {
  const t = e.target.closest?.("[data-tip]");
  if (t !== TIP.cur) (t ? tipShow(t) : tipHide());
});
document.addEventListener("mouseout", (e) => { if (!e.relatedTarget) tipHide(); });
document.addEventListener("scroll", tipHide, true);
document.addEventListener("mousedown", tipHide, true);

/* ---------------- 全局状态 ---------------- */
const state = { overview: null, route: null, pollTimer: null, live: true };

async function getOverview(force = false) {
  if (!state.overview || force) {
    state.overview = await api("/api/overview");
    // profile 中文名随 overview 下发（models.yaml 单一真源），填充全局标签表
    for (const p of state.overview.profiles || [])
      if (p.label) LABEL.profile[p.name] = p.label;
    // skill 目录随 overview 下发（skills.py 单一真源），填充 id→{cmd,label}
    for (const s of state.overview.skills || [])
      LABEL.skill[s.id] = { cmd: s.cmd, label: s.label };
  }
  return state.overview;
}

/* —— 模块导出 —— */
export { $, BUST, CSRF, CUR, GENJOBS, ICON, JOB_ZH, LABEL, MOTION, REVIEW, STAGE_ZH, TIP,
         TRANSITION_ZH, api, costTotal, fmtCost, fmtDate, fmtDur, fmtSec, fmtSize, getOverview, h,
         jobKey, mediaPath, pollJob, post, softRefresh, softRefreshProject, state, tipHide,
         rich, tipShow, toast, toastDismiss, trackJob, withBust };
