/* This file is part of Kinema.
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
   along with this program.  If not, see <https://www.gnu.org/licenses/>. */

/* 深度捕捉工作台（章节页 DV 区块）。

   一句话：拖入一段实拍片，引擎在本机把它处理成「人物深度浮雕 + OpenPose 骨骼」的
   控制视频，按框选区间裁段绑到分镜——**运动来自源片，外观来自分镜图**。

   形态与简笔分镜台**逐条同构**，这是刻意的：两张卡是同一类东西（运动预演路径）的
   两个实例，长成两个样子会让章节页读起来像三个软件。样式共用同一段规则
   （style.css 里 `.skb-*` 与 `.cvc-*` 并列在同一条选择器上），交互共用同一种范式
   ——卡上一条缩略带看产物，主按钮开弹层选镜，不另起全幅路由。

   三条纪律：
   · 门只认 `d.uses_video`，与另两台同判据、同折叠条——绝不按 skill/画风推；
   · 「就绪」不是「存在」：依赖缺失、provider 不支持、模式不对一律做成卡内提示与
     禁用，不做可见性门——藏掉等于锁死用户已经花掉的几分钟 CPU；
   · 弹层一律 `openShell`，忙态一律 `runBusy`，不自造第二套壳与忙态。 */
import { chip, openShell, runBusy, uiConfirm } from "./components.js";
import { BUST, CSRF, h, pollJob, post, softRefresh, toast } from "./core.js";
import { openCinema, scrollToShot } from "./widgets.js";

/* 深度处理忙态账本：`pid/cid` → Set(任务 id)。**按 job 分组**而不是一章一条：
   一条素材还在处理时再传一条是完全正常的操作，一章一条会把第二个任务整个吞掉。 */
const CTLJOBS = new Map();

// 台标：取景框里一枚人形剪影 + 一道贴合它的骨骼线（实拍 → 深度与骨架）
const CT_ICO = '<svg viewBox="0 0 28 28">'
  + '<rect x="3" y="4.4" width="22" height="19.2" rx="2.4"/>'
  + '<circle cx="14" cy="10" r="2.2"/>'
  + '<path d="M14 12.2v5.4M14 14.2l-3.2 2M14 14.2l3.2 2'
  + 'M14 17.6l-2.4 3.4M14 17.6l2.4 3.4"/></svg>';

const MAX_MB = 200;
// 参考视频的服务端硬区间。与引擎的 `previz.SNAP_MIN_SEC/SNAP_MAX_SEC` 同值，超了
// `bind` 会拒——前端只做预判、给出可读的原因，真源仍在引擎
const REF_MIN_SEC = 4;
const REF_MAX_SEC = 15;
// 起点吸附步长，与引擎 `params.STRIP_STEP_SEC` 同值：缩略条一格就是这么长，
// 拖到格线上和拖到格中间必须是同一件事
const STEP_SEC = 0.5;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const snapStep = (v) => Math.round(v / STEP_SEC) * STEP_SEC;
const fmt = (v) => (Math.round(v * 10) / 10).toFixed(1);

const assetsOf = (d) => d.control_assets || [];
const doneAssets = (d) => assetsOf(d).filter((a) => a.status === "done");
const activeShots = (d) => (d.shots || [])
  .filter((s) => s.kind !== "transition" && !s.omitted);
const boundShots = (d) => activeShots(d).filter((s) => s.control);

/* 本台此刻能不能真把控制视频发出去。措辞与 CLI 逐字同源——页面与终端不许说两套话。 */
function blockers(d) {
  const out = [];
  const rd = d.control_ready || {};
  // 就绪排最前：依赖没装时后面几条都没意义，而没有这一条的话，用户拖完视频
  // 只会看到一个失败任务加一行 ModuleNotFoundError
  if (rd.ready === false) out.push(...(rd.notes || ["感知栈未就绪"]));
  if (!(d.video_caps || {}).v2v) out.push("当前视频 provider 不支持参考视频(V2V)");
  if (d.motion !== "native") {
    out.push(`参考视频只在 native 模式生效，本集是 ${d.motion || "未定"}`);
  }
  if (!d.control_video && boundShots(d).length > 0) {
    out.push("深度 V2V 的章级开关未开：已绑的镜这一轮不会带控制视频，点「开启深度 V2V」或章节顶层写 control_video");
  }
  return out;
}

/* 章级开关 control_video 的写入口。它是花钱开关——开启后每次 gen-video 都把控制段作参考
   视频发出，输入视频秒叠加在输出秒之上计费，所以开启要确认、关闭不用。片段已通过锁定时
   服务端拒改：开关改变请求形态。 */
async function toggleV2V(d) {
  const on = !d.control_video;
  if (on && !(await uiConfirm(
    "开启后，已绑控制视频的镜在每次生视频时都会把控制段作参考视频发出，"
    + "输入视频秒数叠加在输出秒数之上计费。", { title: "开启深度 V2V" }))) return;
  try {
    await post("/api/control/v2v", { project: d.project, chapter: d.id, on });
    toast(on ? "深度 V2V 已开启" : "深度 V2V 已关闭");
    softRefresh(d.project, d.id);
  } catch (e) { toast(e.message, true); }
}

/* 左上角的签：绑到哪几镜就写哪几镜，与简笔板的 `SHOT 01` 同形——三个预演台的
   缩略格并排出现，签的写法一致才看得出它们是同一类东西。文件名退到悬浮提示与
   灯箱信息栏里（那两处有地方铺开，而 84px 的格子上它只占宽度）。 */
function shotTag(used) {
  if (!used.length) return "未绑定";
  return `SHOT ${String(used[0]).padStart(2, "0")}`
    + (used.length > 1 ? ` +${used.length - 1}` : "");
}

function progressText(a) {
  const p = a.progress || {};
  if (a.status === "failed") return "处理失败";
  if (!p.total) return a.status === "queued" ? "排队中" : (a.status || "处理中");
  return `${p.pass === 2 ? "渲染" : "分析"} ${p.done || 0}/${p.total}`;
}

/* ---------------------------------------------------------------- 入口卡 */
function controlCard(d) {
  const assets = assetsOf(d);
  const bound = boundShots(d);
  const shots = activeShots(d);
  const inflight = CTLJOBS.get(`${d.project}/${d.id}`)?.size || 0;
  const busy = assets.filter((a) => a.status !== "done" && a.status !== "failed");
  const ready = (d.control_ready || {}).ready !== false;
  const notes = blockers(d);
  const stat = (n, label) => h("div", { class: "cvc-stat" },
    h("b", null, String(n)), h("span", null, label));

  // 这条素材绑给了哪几镜。素材是可以切给多个镜的（一段 60 秒切 5 镜），
  // 格子上不标的话，几条素材摆在一起就分不出哪条已经用上、哪条还是白放着
  const boundTo = (a) => shots
    .filter((s) => s.control && (s.control_meta || {}).asset === a.id)
    .map((s) => s.id);

  // 缩略带：每条素材一格，对照图作缩略，点开是视频预览灯箱
  const cells = assets.map((a) => {
    if (a.status !== "done") {
      return h("div", { class: "cvc-cell gen" },
        a.sheet ? h("img", { src: a.sheet, loading: "lazy" }) : null,
        a.status === "failed" ? null : h("span", { class: "gw-ring" }),
        h("i", null, progressText(a)),
        h("span", { class: "cvc-tag idle" }, "处理中"));
    }
    // 点开放的是**二合一**（左源片、右深度）：单看深度判不出骨骼有没有跟住动作，
    // 得和源片同屏。发给模型的仍是纯控制视频，两个字段各管各的
    const used = boundTo(a);
    return h("div", { class: "cvc-cell" + (used.length ? " bound" : ""),
        title: `${a.name || a.id} · 点开对照预览（左源片 · 右深度）`
          + (used.length ? `\n已绑：镜 ${used.join("、")}` : "\n还没绑到任何镜"),
        onclick: () => openCinema({
          video: (a.video || {}).compare || (a.video || {}).control, poster: a.sheet,
          title: `${a.name || a.id} · 源片 ｜ 深度对照`,
          rows: [["素材 id", a.id], ["人数", String(a.people ?? "—")],
                 ["时长", a.seconds ? `${(+a.seconds).toFixed(1)}s` : "—"],
                 ["帧率", a.fps ? `${a.fps}fps` : "—"],
                 ["发给模型的", "仅右半边的纯控制视频，对照片不进请求"]],
          chips: [chip("compare", "cyan"), chip("源片｜深度·非成片")] }) },
      a.sheet ? h("img", { src: a.sheet, loading: "lazy" }) : null,
      h("span", { class: "cvc-tag" + (used.length ? "" : " idle"),
        dataset: { tip: used.length
          ? `${a.name || a.id}\n已绑：镜 ${used.join("、")} —— 点击跳到第一镜的分镜卡`
          : `${a.name || a.id}\n还没绑到任何镜——用「◇ 绑定分镜」框一段` },
        onclick: (e) => { if (used.length) { e.stopPropagation(); scrollToShot(used[0]); } } },
        shotTag(used)),
      h("span", { class: "cvc-del", dataset: { tip: "删除素材（仍有镜绑着会被拒）" },
        onclick: (e) => { e.stopPropagation(); delAsset(d, a); } }, "✕"));
  });

  // 已入账但服务端还没建档的任务：补占位格。`control build` 要先哈希整个文件、
  // 再整段解码数帧才写 asset.json，这中间有好几秒；而它**从不碰章节文档**，
  // 所以在那之前服务端一无所有。没有这几格，用户按下上传后界面纹丝不动。
  const pending = Math.max(0, (CTLJOBS.get(`${d.project}/${d.id}`)?.size || 0)
    - assets.filter((a) => a.status !== "done" && a.status !== "failed").length);
  for (let i = 0; i < pending; i += 1) {
    cells.push(h("div", { class: "cvc-cell gen" },
      h("span", { class: "gw-ring" }), h("i", null, "读取源片…")));
  }

  const strip = cells.length > 0
    ? h("div", { class: "cvc-strip" }, ...cells)
    : h("p", { class: "cvc-empty" },
        "还没有素材。拖一段实拍表演片进来：单人或双人、全身入画、段内无剪辑点、"
        + "不超过 30 秒。处理全程本机 CPU，不产生任何调用费用。");

  return h("div", { class: "card cvc-card" },
    h("div", { class: "cvc-grid", "aria-hidden": "true" }),
    h("div", { class: "cvc-body" },
      h("div", { class: "cvc-main" },
        h("div", { class: "cvc-lead" },
          h("div", { class: "cvc-head" },
            h("span", { class: "cvc-ico", html: CT_ICO }),
            h("div", null, h("b", null, "深度捕捉"),
              h("em", null, "运动来自实拍源片，外观来自你自己的分镜图"))),
          h("p", { class: "cvc-desc" },
            "把一段实拍表演片处理成只含人物深度浮雕与 OpenPose 骨骼的控制视频——"
            + "场景、面容与服装全部剥除，多人分别成骨；再按框选的区间裁段绑到分镜。",
            h("br"),
            "生成视频时控制段作参考视频随请求提交：运动逐帧跟随源片，外观仍由本镜"
            + "分镜图决定。处理全程本机 CPU 不计费，随后的图生视频按输出秒数与"
            + "输入视频秒数一并计价。")),
        h("div", { class: "cvc-side" },
          h("div", { class: "cvc-acts" },
            h("button", { class: "cvc-go cy", disabled: !ready,
                dataset: { tip: ready ? "" : "感知栈未就绪——见下方安装命令" },
                onclick: () => openControlBuildDialog(d) },
              h("span", { class: "cvc-go-ico" }, "◆"),
              h("b", null, "开启深度捕捉"),
              h("i", { class: "cvc-go-arw" }, "→")),
            h("button", { class: "act-btn", disabled: doneAssets(d).length === 0,
              dataset: { tip: doneAssets(d).length === 0
                ? "先处理一段实拍片，才有可绑的素材"
                : "◇ 绑定分镜\n选一镜，在缩略条上框出要用的那一段并绑定；"
                  + "框定的长度即该镜的成片长度。" },
              onclick: () => openControlBindDialog(d) }, "◇ 绑定分镜"),
            h("button", { class: "act-btn",
              dataset: { tip: d.control_video
                ? "关闭后 gen-video 不再发控制视频；已通过锁定的片段须先置 retake 才能改"
                : "章级开关 control_video：开启后已绑的镜随 gen-video 发控制段作参考视频，"
                  + "输入视频秒叠加在输出秒之上计费" },
              onclick: () => toggleV2V(d) },
              d.control_video ? "◆ 关闭深度 V2V" : "◆ 开启深度 V2V")),
          h("div", { class: "cvc-stats" },
            stat(assets.length, "条素材"),
            stat(`${bound.length}/${shots.length}`, "镜已绑定"),
            stat(d.control_video ? "已开" : "未开", "深度 V2V")))),
      strip,
      Math.max(busy.length, inflight) > 0
        ? h("p", { class: "cvc-note" },
            `${Math.max(busy.length, inflight)} 条素材处理中——每源秒约十几秒 CPU，`
            + "可以离开页面，回来自动恢复进度。")
        : null,
      notes.length > 0 ? h("p", { class: "cvc-note" }, notes.join("；") + "。") : null,
      !ready
        ? h("pre", { class: "cvc-cmd" },
            'pip install -e "engine[control]"\n'
            + "pip install --no-deps rtmlib\n"
            + "python3 -m kinema control fetch")
        : null));
}

/* ---------------------------------------------------------------- 忙态 */
/* 「登记忙态」与「立刻重绘」绑成一件事、收在跟踪函数内部——分散到各调用方就会
   出现「这条路记得刷新、那条忘了写」。 */
function trackControlJob(pid, cid, jid) {
  const key = `${pid}/${cid}`;
  if (!CTLJOBS.has(key)) CTLJOBS.set(key, new Set());
  const bag = CTLJOBS.get(key);
  if (bag.has(jid)) return;
  bag.add(jid);
  softRefresh(pid, cid);
  const done = (msg, bad) => {
    bag.delete(jid);
    if (!bag.size) CTLJOBS.delete(key);
    BUST.set(key, Date.now());
    toast(msg, bad);
    softRefresh(pid, cid);
  };
  pollJob(jid, {
    onDone: () => done("深度捕捉完成"),
    onFail: (e) => done(`深度捕捉失败：${e || "见任务日志"}`, true),
  });
}

function reconcileControlJobs(pid, cid, jobs) {
  (jobs || []).forEach((j) => {
    if (((j.meta || {}).kind || "") === "control_build") {
      trackControlJob(pid, cid, j.id);
    }
  });
}

/* ---------------------------------------------------------------- 处理弹层 */
/* 上传即入队处理，拖进来就开工。手写 fetch（body 是原始字节，走不了恒发 JSON 的
   post()）必须显式带 CSRF。 */
async function uploadSource(pid, cid, file, shot) {
  if (file.size > MAX_MB * 1024 * 1024) throw new Error(`文件超过 ${MAX_MB}MB 上限`);
  const qs = new URLSearchParams({ project: pid, chapter: cid, name: file.name });
  if (shot != null) qs.set("shot", String(shot));
  const res = await fetch(`/api/control/upload?${qs}`, {
    method: "POST", headers: { "X-Csrf-Token": CSRF }, body: file,
  });
  const r = await res.json().catch(() => ({}));
  if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
  return r;
}

function openControlBuildDialog(d) {
  const pid = d.project;
  const cid = d.id;
  // 处理要跑几分钟，跑完人多半已经离开页面。这里选一个镜，引擎处理完直接绑上；
  // 区间从 0 起、按该镜秒数，回头在「绑定分镜」里再框细。不选就只入库不绑。
  let target = null;
  const list = shotList(d, {
    selected: null,
    // 已绑的镜不作候选：一镜只收一条控制视频，而这条绑定要等几分钟才落下——
    // 到那时才顶掉它原来的运动源，人已经不在这个页面上了
    blocked: (s) => !!s.control,
    onPick: (s) => { target = target?.id === s.id ? null : s; list.pick(target?.id ?? null); },
    onUnbind: (s) => unbindShot(d, s),
  });

  const input = h("input", { type: "file", accept: ".mp4,.mov", class: "cvc-file",
    onchange: (e) => { const f = e.target.files?.[0]; if (f) send(f); e.target.value = ""; } });
  const zone = h("div", { class: "cvc-drop", onclick: () => input.click() },
    h("b", null, "拖入实拍视频，或点击选择"),
    h("em", null, `mp4 / mov · ≤30 秒 · ≤${MAX_MB}MB · 上传即开始处理`), input);
  let dismiss = null;
  const send = async (f) => {
    zone.classList.add("busy");
    try {
      const r = await uploadSource(pid, cid, f, target?.id);
      toast(target
        ? `已入队处理——完成后自动绑到镜 ${target.id}`
        : "已入队处理——可以关掉这个窗口，回来自动恢复进度");
      trackControlJob(pid, cid, r.job);
      dismiss?.();
    } catch (e) {
      toast(e.message, true);
      zone.classList.remove("busy");
    }
  };
  ["dragover", "dragenter"].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault(); e.stopPropagation(); zone.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault(); e.stopPropagation(); zone.classList.remove("drag");
  }));
  zone.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0]; if (f) send(f);
  });

  openShell({ card: "cvc-dlg wide", build: (close) => {
    dismiss = close;
    return [
      h("span", { class: "k" }, "◆ 开启深度捕捉"),
      h("p", { class: "dlg-msg" },
        "引擎在本机逐帧跑三个小模型（姿态 / 深度 / 人物分割），把源片转成黑底的"
        + "人物深度浮雕与彩色骨骼。不产生调用费用，主要使用 CPU 处理。"),
      h("div", { class: "cvc-tips" },
        h("b", null, "源片准入四条"),
        h("ul", null,
          h("li", null, "单人或双人 —— 三人以上遮罩会互相吃掉轮廓"),
          h("li", null, "全身或至少到膝 —— 大特写凑不够检测所需的骨骼点"),
          h("li", null, "段内没有剪辑点 —— 跳切处骨架会整帧跳位"),
          h("li", null, "非自拍镜像 —— 镜像源片需先 ffmpeg -vf hflip，否则成片换手换脚"))),
      zone,
      h("p", { class: "dlg-msg tight" },
        "处理完成后自动绑到（可不选，之后再绑；再点一次取消选中）。"
        + "已绑的镜要先解绑——一镜只收一条控制视频："),
      list.el,
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "关闭")),
    ];
  } });
}

async function delAsset(d, a) {
  try {
    await post("/api/control/delete",
      { project: d.project, chapter: d.id, asset: a.id });
    toast(`素材 ${a.name || a.id} 已删除`);
    softRefresh(d.project, d.id);
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- 对照回看 */
/* 分镜卡上「◆ 深度」的去处，随本镜产物到哪一步而变：

     只有控制段        → 两列对照：左源片段、右深度
     控制段 + 成片段    → 三合一：成片与素材同画幅时排在右侧，取向相反（竖拍素材、
                          横屏成片）时另起一行——塞进素材格里只剩中间一条细画面

   **不直接播 `s.control`**：那一份是要发给视频模型的文件，是哑的（native 章的声音
   由模型生成，把实拍背景音一并发过去等于拿账单赌模型不拿它做文章）。而审看要听的
   正是源片的原始节奏——这一段起没起在拍点上，光看深度浮雕判不出来。

   对照片是按需转码的（几秒），故第一次点开要现拼一次；拼好的留在盘上，下次直接播。
   区间一改绑定就会把它删掉，不存在播到旧区间那一版的可能。 */
/* 成片相对控制段的整体偏移（music 阶段量出、写在 gen.control.sync）。够格的偏移已经
   用在配乐起点上；没量到就不占行——对照片本身逐帧可比，肉眼看得出差几帧 */
function syncText(sync) {
  if (!sync) return null;
  const lag = +sync.lag || 0;
  const aligned = Math.abs(lag) < 1e-9;
  const head = aligned ? "成片与控制段逐帧对齐"
    : `成片比控制段${lag > 0 ? "晚" : "早"} ${Math.abs(lag).toFixed(2)}s`;
  const tail = !sync.applied ? "相关不足，配乐不平移"
    : (aligned ? "配乐照原区间铺" : "配乐随之平移");
  return `${head}（相关 ${(+sync.corr || 0).toFixed(2)}）——${tail}`;
}

async function openControlCompare(d, s) {
  const meta = s.control_meta || {};
  const three = !!s.clip;
  const rows = [["素材", meta.asset || "—"],
                ["区间", meta.start != null
                  ? `${fmt(meta.start)}~${fmt(meta.end ?? meta.start + (meta.seconds || 0))}s`
                  : "—"],
                ["段长", meta.seconds ? `${meta.seconds}s · 与成片 1:1` : "—"],
                ["对拍", syncText(meta.sync)],
                ["读法", three
                  ? "左=实拍源片，右=发给模型的控制视频；模型出的成片与素材同画幅时排在最右，"
                    + "画幅取向不同时另起一行；声音是源片那一路"
                  : "左=实拍源片，右=发给模型的控制视频；声音是源片那一路"]];
  let url = s.control_compare;
  if (!url) {
    toast(three ? "正在拼三列对照…" : "正在拼两列对照…");
    try {
      // 接口回的就是可播的媒体 URL，前端不自己拼路径——那份映射只该有一处
      ({ compare: url } = await post("/api/control/compare",
        { project: d.project, chapter: d.id, shot: s.id }));
    } catch (e) { toast(e.message, true); return; }
    softRefresh(d.project, d.id);
  }
  openCinema({ video: url,
    title: `镜 ${s.id} · 源片 ｜ 深度${three ? " ｜ 成片" : ""}`,
    rows,
    chips: [chip("compare", "cyan"), chip(`${three ? "三" : "两"}列对照·非成片`)] });
}

/* ---------------------------------------------------------------- 镜列表 */
/* 两个弹层共用的一份分镜列表：**单选**目标镜，已绑的行上就地解绑。

   单选而不是多选，是因为区间现在逐镜可调——多选就只能给所有镜一套区间，
   那等于把「自己框哪一段」这件事又收了回去。一次绑一镜、当场看效果。

   `blocked(s)` 判这一行此刻能不能选，判据由调用方给（两个弹层的口径不同，
   见各自的调用处）。不可选的行压暗但**不禁用解绑按钮**——那颗按钮正是这一行的出口。 */
function shotList(d, { selected, blocked, onPick, onUnbind }) {
  const box = h("div", { class: "cvc-list" });
  const paint = () => box.replaceChildren(...activeShots(d).map((s) => {
    const meta = s.control_meta || {};
    const on = String(s.id) === String(selected);
    const off = blocked(s);
    // 行的骨架与简笔分镜板的 `.skb-row` 同构：扁平 flex、镜号定宽、状态走 chip。
    // 分镜图缩略是本台多出的一格——选的是「这段运动放到哪一镜」，看得见那一帧才判得准。
    // **行必须是 div 不能是 label**：label 会把点击转发给内部第一个可标注控件，
    // 而「解绑」是 button（可标注），于是点行里任何地方都等于按了解绑——
    // 事件冒泡拦不住这条路，它不走冒泡。
    return h("div", { class: "cvc-row" + (on ? " on" : "") + (off ? " off" : ""),
      dataset: off
        ? { tip: `镜 ${s.id} 已绑素材 ${meta.asset || "—"}——一镜只收一条控制视频，`
            + "换一条得先解绑" }
        : null,
      onclick: () => { if (off) return; selected = s.id; onPick(s); paint(); } },
      h("span", { class: "cvc-radio" + (on ? " on" : "") }, h("i", null, "✓")),
      s.image
        ? h("img", { class: "cvc-thumb", src: s.image, loading: "lazy", alt: "" })
        : h("span", { class: "cvc-thumb none" }),
      h("b", null, `镜 ${s.id}`),
      h("span", null, `${fmt(s.dur || 0)}s`),
      s.control
        ? chip(`已绑 ${fmt(meta.start || 0)}~${fmt(meta.end || 0)}s`, "cyan")
        : (s.previz ? chip("已有 3D 预演", "amber") : null),
      s.control
        ? h("button", { class: "cvc-un", type: "button",
            dataset: { tip: "解绑：摘掉这一镜的控制视频（段落文件保留）" },
            // 重绘收在列表内部：解绑改的是这份快照，页面级 `softRefresh` 够不着
            // 覆盖层，各调用方自己记得重画迟早会漏掉一处
            onclick: async (e) => {
              e.preventDefault(); e.stopPropagation();
              await onUnbind(s);
              paint();
            } },
          "解绑")
        : null);
  }));
  paint();
  return { el: box, repaint: paint, pick: (id) => { selected = id; paint(); } };
}

/* 解绑成功后**就地改这份快照**：弹层是照打开那一刻的章节数据渲染的，
   `softRefresh` 重绘的是它背后的页面，覆盖层里的行不会跟着变——
   不改快照的话，服务端已经解绑了，列表还挂着「已绑 / 解绑」。 */
async function unbindShot(d, s) {
  try {
    await post("/api/control/unbind", { project: d.project, chapter: d.id, shot: s.id });
    delete s.control;
    delete s.control_meta;
    delete s.control_compare;
    delete (s.gen || {}).control;
    toast(`镜 ${s.id} 已解绑`);
    softRefresh(d.project, d.id);
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- 区间选择 */
/* 在素材的缩略条上框一段，上方预览播的是**二合一**（左源片、右深度）——要判断的是
   「这一段动作值不值得复刻、骨骼贴不贴得住」，两路必须同屏。

   三种手势各管一件事，互不串台：
     · 拖左/右把手 —— **只动这一端**，另一端钉住，段长随之变（这就是修剪）
     · 拖选区本体 —— 整体平移，段长不变（换一段同样长的来演）
     · 点条上任意处 —— 把播放头移过去看那一帧，不动选区

   段长恒取整秒：引擎按 `round(终点-起点)` 定段，把手若能停在半秒上，用户看到的窗口
   和真发出去的段就差半秒。取整时**只动正在拖的那一端**，顶到素材两端时另一端才让步。 */
function rangePicker(asset, { onChange }) {
  const total = +(asset.seconds || 0);
  let start = 0;
  let end = Math.min(total, REF_MIN_SEC);
  let raf = 0;

  const src = (asset.video || {}).compare || (asset.video || {}).control;
  // 带声播：框一段舞蹈要的正是「起在哪个拍点上」，静音框只能靠猜。
  // 只有按下播放键才出声（没有自动播放这条路），不会有人被突然吵到
  const video = h("video", { class: "cvc-prev", src, playsinline: "" });
  const bar = h("div", { class: "cvc-bar" });
  const win = h("div", { class: "cvc-win" });
  const hL = h("span", { class: "cvc-h l" }, h("i", { class: "cvc-h-t mono" }));
  const hR = h("span", { class: "cvc-h r" }, h("i", { class: "cvc-h-t mono" }));
  const head = h("span", { class: "cvc-head-l" });
  const ruler = h("div", { class: "cvc-ruler" });
  win.append(hL, hR);
  if (asset.strip) bar.style.backgroundImage = `url("${asset.strip}")`;
  bar.append(win, head);

  // 刻度：按总长挑一个让标签落在 5~9 个的步长，密了糊成一片、疏了读不出位置
  const step = [1, 2, 5, 10, 15, 30, 60].find((s) => total / s <= 8) || 60;
  const marks = [];
  // 留出 0.6 格的余量再收尾：片尾那一格恒要（不标就读不出素材多长），
  // 但它离前一格太近时两个标签会叠在一起
  for (let t = 0; t <= total - step * 0.6; t += step) marks.push(t);
  marks.push(total);
  marks.forEach((t, i) => ruler.append(h("i", {
    class: "cvc-tick" + (i === 0 ? " first" : "") + (i === marks.length - 1 ? " last" : ""),
    style: `left:${(t / total) * 100}%` }, `${+t.toFixed(1)}s`)));

  const nStart = h("input", { class: "cvc-num", type: "number", step: String(STEP_SEC), min: "0" });
  const nEnd = h("input", { class: "cvc-num", type: "number", step: String(STEP_SEC), min: "0" });
  const badge = h("span", { class: "cvc-span mono" });
  const clock = h("span", { class: "cvc-clock mono" });
  const play = h("button", { class: "cvc-play", type: "button",
    dataset: { tip: "试播选中的这一段（循环）" },
    onclick: () => (video.paused ? video.play() : video.pause()) });

  const pct = (t) => `${(t / total) * 100}%`;
  const seconds = () => Math.round(end - start);

  const sync = () => {
    win.style.left = pct(start);
    win.style.width = pct(end - start);
    hL.firstChild.textContent = fmt(start);
    hR.firstChild.textContent = fmt(end);
    nStart.value = fmt(start);
    nEnd.value = fmt(end);
    const n = seconds();
    badge.textContent = `${n}s`;
    badge.classList.toggle("bad", n < REF_MIN_SEC || n > REF_MAX_SEC);
    if (video.currentTime < start || video.currentTime > end) video.currentTime = start;
    tick();
    onChange({ start, end, seconds: n, valid: n >= REF_MIN_SEC && n <= REF_MAX_SEC });
  };

  const tick = () => {
    head.style.left = pct(Math.min(Math.max(video.currentTime, start), end));
    clock.textContent = `${fmt(video.currentTime - start)} / ${seconds()}.0s`;
  };

  /* 播放头走 rAF 而不是 timeupdate：后者约每秒 4 次，在几百像素宽的条上是一跳一跳的。
     视频被移出 DOM（弹层关掉）时自停——不然一条脱离文档的 video 会一直播下去。 */
  const follow = () => {
    if (!video.isConnected) { video.pause(); raf = 0; return; }
    if (video.currentTime >= end - 0.03) video.currentTime = start;
    tick();
    raf = video.paused ? 0 : requestAnimationFrame(follow);
  };
  video.addEventListener("play", () => { play.classList.add("on"); if (!raf) follow(); });
  video.addEventListener("pause", () => play.classList.remove("on"));
  video.addEventListener("loadedmetadata", () => { video.currentTime = start; tick(); });

  const setStart = (t) => {
    const n = clamp(Math.round(end - snapStep(t)), REF_MIN_SEC, REF_MAX_SEC);
    start = end - n;
    if (start < 0) { start = 0; end = n; }              // 顶到片头，终点让步
    sync();
  };
  const setEnd = (t) => {
    const n = clamp(Math.round(snapStep(t) - start), REF_MIN_SEC, REF_MAX_SEC);
    end = start + n;
    if (end > total) { end = total; start = Math.max(0, end - n); }   // 顶到片尾
    sync();
  };
  const moveTo = (t) => {
    const n = end - start;
    start = clamp(snapStep(t), 0, Math.max(0, total - n));
    end = start + n;
    sync();
  };

  const atX = (clientX) => {
    const r = bar.getBoundingClientRect();
    return clamp((clientX - r.left) / r.width, 0, 1) * total;
  };
  const drag = (el, apply, grab) => el.addEventListener("pointerdown", (e) => {
    e.preventDefault(); e.stopPropagation();
    el.setPointerCapture(e.pointerId);
    el.classList.add("drag");
    const off = grab ? atX(e.clientX) - start : 0;
    const x0 = e.clientX;
    let moved = false;
    // 位移阈值只管选区本体（要分辨「按一下看帧」和「拖着换位置」）；
    // 把手按下即跟手，那里没有第二种意图
    const move = (ev) => {
      if (Math.abs(ev.clientX - x0) > 3) moved = true;
      if (moved || !grab) apply(atX(ev.clientX) - off);
    };
    if (!grab) apply(atX(e.clientX));
    const up = (ev) => {
      // 在选区里按一下没拖动 = 想看那一帧。挪选区有拖的手势，两者不必抢同一次点击
      if (grab && !moved) { video.currentTime = clamp(atX(ev.clientX), start, end); tick(); }
      el.classList.remove("drag");
      el.releasePointerCapture(e.pointerId);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
  });
  drag(hL, setStart);
  drag(hR, setEnd);
  drag(win, moveTo, true);          // 抓哪儿动哪儿：不把选区跳到指针中心
  // 条上空白处点一下只移播放头——看那一帧是最常做的事，而挪选区有专门的手势
  bar.addEventListener("pointerdown", (e) => {
    if (e.target !== bar && e.target !== head) return;
    video.currentTime = clamp(atX(e.clientX), start, end);
    tick();
  });

  nStart.addEventListener("change", () => setStart(+nStart.value || 0));
  nEnd.addEventListener("change", () => setEnd(+nEnd.value || 0));

  /* 键盘微调：拖到大概位置后用方向键对齐到动作停顿那一帧。
     ←/→ 平移选区，Shift 改成拉伸终点——两种意图各占一组键，不必先去点某个把手。 */
  const onKey = (e) => {
    const d = { ArrowLeft: -STEP_SEC, ArrowRight: STEP_SEC }[e.key];
    if (d === undefined || e.metaKey || e.ctrlKey) return false;
    e.preventDefault();
    if (e.shiftKey) setEnd(end + d); else moveTo(start + d);
    return true;
  };

  const el = h("div", { class: "cvc-range" },
    h("div", { class: "cvc-screen" }, video,
      (asset.video || {}).compare
        ? null
        : h("p", { class: "cvc-noprev" }, "这条素材没有二合一对照，只能单看深度——重跑一次处理即可补上"),
      h("div", { class: "cvc-hud" }, play, clock)),
    h("div", { class: "cvc-track" }, bar, ruler),
    h("div", { class: "cvc-nums" },
      h("label", null, "起点", nStart, h("i", null, "s")),
      h("label", null, "终点", nEnd, h("i", null, "s")),
      h("span", { class: "cvc-hint" }, "拖两端改段长 · 拖选区换位置 · ←→ 微调"),
      h("span", { class: "cvc-lab" }, "段长"), badge));

  const set = (a, b) => {
    const n = clamp(Math.round(b - a), REF_MIN_SEC, REF_MAX_SEC);
    start = clamp(a, 0, Math.max(0, total - n));
    end = start + n;
    sync();
  };

  return { el, sync, onKey, set,
           stop: () => { video.pause(); if (raf) cancelAnimationFrame(raf); },
           get: () => ({ start, end, seconds: seconds() }),
           fit: (n) => set(0, clamp(n, REF_MIN_SEC, REF_MAX_SEC)) };
}

/* ---------------------------------------------------------------- 绑定弹层 */
/* 一次绑一镜：选镜 → 在缩略条上框区间 → 二合一预览当场确认 → 绑。
   区间长度即这一镜的成片长度（引擎会把 `dur` 对齐过去），故这里改的不只是
   「用素材的哪一段」，也是「这一镜多长」——摘要行必须把这件事说出来。 */
function openControlBindDialog(d, preset) {
  const assets = doneAssets(d);
  let asset = assets.find((a) => a.id === preset) || assets[0];
  let picked = null;
  let range = null;

  const stage = h("div", { class: "cvc-stage" });
  const summary = h("span", { class: "cvc-sum mono" });
  const go = h("button", { class: "dlg-btn primary", disabled: true }, "绑定");

  // 绑着**别的**素材的镜不可选——一镜只收一条控制视频，换素材得先解绑。
  // 绑着当前这条的仍可选：那是回来改区间，最常做的一件事
  const blocked = (s) => !!s.control && (s.control_meta || {}).asset !== asset.id;
  const firstFree = () => activeShots(d).find((s) => !s.control) || null;
  const usedBy = (a) => activeShots(d)
    .filter((s) => (s.control_meta || {}).asset === a.id).map((s) => s.id);

  const refreshSummary = () => {
    const r = range?.get();
    if (!picked || !r) {
      summary.textContent = activeShots(d).some((s) => !blocked(s))
        ? "先选一个分镜"
        : "每一镜都绑着别的素材——先解绑一镜再绑这条";
      go.disabled = true;
      return;
    }
    const bad = r.seconds < REF_MIN_SEC || r.seconds > REF_MAX_SEC;
    const durChange = Math.round(+picked.dur || 0) !== r.seconds
      ? `，镜 ${picked.id} 时长 ${fmt(picked.dur)}s → ${r.seconds}s` : "";
    // 一镜只发一条参考视频：绑到有 3D 预演的镜等于换运动源，预演登记随之摘除。
    // 后果要在按钮按下之前写在摘要行里，而不是事后从分镜卡上少了个角标去猜
    const dropPreviz = picked.previz ? "，将摘除该镜的 3D 预演" : "";
    summary.textContent = bad
      ? `段长 ${r.seconds}s 超出 ${REF_MIN_SEC}~${REF_MAX_SEC}s`
      : `镜 ${picked.id} ← ${asset.name || asset.id} 的 ${fmt(r.start)}~${fmt(r.end)}s${durChange}${dropPreviz}`;
    summary.classList.toggle("bad", bad);
    go.disabled = bad;
  };

  // 选段器只在有目标镜时挂：区间的默认长度取自那一镜，没有镜就没有默认可言
  const mountRange = () => {
    range?.stop();
    if (!picked) { range = null; stage.replaceChildren(); return; }
    range = rangePicker(asset, { onChange: refreshSummary });
    stage.replaceChildren(range.el);
    const meta = picked.control_meta || {};
    // 已绑这条素材的镜：窗口摆回它原来那一段，改一改比从零重框快。
    // 否则框「这一镜现在要多长」那么长——多数时候人只想换位置，不想重定长度。
    if (picked.control && meta.asset === asset.id && meta.seconds) {
      range.set(meta.start || 0, meta.end ?? (meta.start || 0) + meta.seconds);
    } else {
      range.fit(Math.round(+picked.dur || REF_MIN_SEC));
    }
  };

  // 开局就落到最靠前的未绑定镜上：进这个弹层就是来绑一镜的，让人先对着一句
  // 「先选一个分镜」再点一次是白走一步
  picked = firstFree();
  const list = shotList(d, {
    selected: picked?.id ?? null,
    blocked,
    onPick: (s) => {
      picked = s;
      mountRange();
      refreshSummary();
    },
    onUnbind: async (s) => {
      await unbindShot(d, s);
      paintPick();            // 素材卡上的「已绑 N 镜」跟着变
      refreshSummary();
    },
  });

  /* 素材条：封面 + 名字 + 时长 + 已绑镜数。几条素材常常都叫 IMG_0421.mp4，
     只印文件名等于让人凭记忆选。封面用的是处理时留下的对照图（左源片右深度），
     一眼能认出是哪条表演。 */
  const pick = h("div", { class: "cvc-pick" });
  const paintPick = () => pick.replaceChildren(...assets.map((a) => {
    const used = usedBy(a);
    return h("button", { class: "cvc-ast" + (a.id === asset.id ? " on" : ""), type: "button",
      dataset: { tip: `${a.name || a.id}\n${fmt(a.seconds)}s · ${a.people ?? "?"} 人`
        + (used.length ? `\n已绑：镜 ${used.join("、")}` : "\n还没绑到任何镜") },
      onclick: () => pickAsset(a) },
      a.sheet ? h("img", { src: a.sheet, loading: "lazy", alt: "" })
              : h("span", { class: "cvc-ast-none" }),
      // 卡上只印主名：几条素材的区别恰恰在名字中段（IMG_0421 / IMG_0422），
      // 扩展名占掉的四个字符正好把它挤成省略号
      h("b", null, String(a.name || a.id).replace(/\.[^.]+$/, "")),
      h("i", null, `${fmt(a.seconds)}s`),
      used.length ? h("em", null, `${used.length} 镜`) : null);
  }));

  // 换素材时选中的镜要重判：它可能正绑着刚被换掉的那条素材（在新素材下不可选）。
  // 那就往下落到最靠前的未绑定镜，没有空镜就清空舞台等人先解绑
  const pickAsset = (a) => {
    asset = a;
    if (!picked || blocked(picked)) picked = firstFree();
    list.pick(picked?.id ?? null);
    mountRange();
    refreshSummary();
    paintPick();
  };

  paintPick();
  mountRange();
  refreshSummary();
  openShell({ card: "cvc-dlg wide", onClose: () => range?.stop(),
    // 方向键交给选段器；没挂选段器时不拦，Esc 等壳自己的键照常
    keys: (e) => range?.onKey(e),
    build: (close) => {
    go.onclick = async (ev) => {
      const btn = ev.currentTarget;
      const r = range.get();
      try {
        await runBusy(btn, "裁段中…", () => post("/api/control/bind", {
          project: d.project, chapter: d.id, shot: picked.id, asset: asset.id,
          start: r.start, end: r.end, replace_previz: !!picked.previz,
        }));
        toast(`镜 ${picked.id} 已绑定 ${fmt(r.start)}~${fmt(r.end)}s`
          + (picked.previz ? "，3D 预演已摘除" : ""));
        close();
        softRefresh(d.project, d.id);
      } catch (err) { toast(err.message, true); }
    };
    return [
      h("span", { class: "k" }, "◇ 绑定分镜"),
      h("p", { class: "dlg-msg" },
        `选一镜，在缩略条上框出要用的那一段（${REF_MIN_SEC}~${REF_MAX_SEC} 秒）。`
        + "预览左边是源片、右边是深度，两路同屏才判得出骨骼贴不贴得住动作。",
        h("br"),
        "框定的长度就是这一镜的成片长度——控制段与成片 1:1，运动才不被拉伸。"),
      assets.length > 1
        ? h("div", { class: "cvc-pick-wrap" },
            h("span", { class: "cvc-pick-k" }, "素材"), pick)
        : null,
      list.el,
      stage,
      h("div", { class: "cvc-tools" }, summary),
      h("div", { class: "dlg-acts" },
        h("button", { class: "dlg-btn", onclick: close }, "取消"), go),
    ];
  } });
}

export { controlCard, openControlCompare, reconcileControlJobs, trackControlJob };
