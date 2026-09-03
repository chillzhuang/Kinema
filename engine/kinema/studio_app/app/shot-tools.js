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

/* ═ Studio 前端模块 · app/shot-tools.js — 分镜工具弹层：素材直供 · 垫图参考（原生 ES Module·免构建）═ */

import { uiCheck } from "./components.js";
import { $ } from "./core.js";
import { BUST, CSRF, api, getOverview, h, jobKey, mediaPath, pollJob, post, softRefresh, softRefreshProject, state, toast, trackJob, withBust } from "./core.js";
import { reloadLBContext, renderLightbox } from "./widgets.js";
import { renderRail } from "./shell.js";
import { genWait, genWaitOff, viewChapter } from "./chapter.js";

/* ═══════════ ⇪ 素材直供选择器 ═══════════
   把「现成图」登记为本镜画面，跳过 AI 生图（零成本）。两条来源：
   ① 复用本作品已有画面（其他镜的分镜图 / 设定资产）——素材复用型解说
     （kn-showcase）的核心工法：同一张图跨镜复用、只换运镜即成"多机位"；
   ② 上传本地图片（产品图 / 实拍图 / 截图）。
   与 AI 生成同制度：旧版自动归档可回滚、登记后落「待审」、done 锁定镜拒收。 */
function openSupplyDialog(d, s) {
  let picked = null;
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const finish = async (msg, r) => {
    toast(msg);
    // 供料体检的告警（低分/宽高比/alpha）不拦登记，但必须让人看见——留痕在
    // gen.image.inspect，这里额外弹一条，逐条列全（最多两条免刷屏）
    const warn = ((r || {}).inspect || {}).warn || [];
    if (warn.length) toast(`⚠ 素材体检 · ${warn.map((w) => w.msg).slice(0, 2).join("；")}`, true);
    BUST.set(jobKey(d.project, d.id, s.id), Date.now());
    close();
    await softRefresh(d.project, d.id);
  };
  // 跳过体检开关：体检只对「ffprobe 解不出」硬拦，勾上即整个跳过——
  // 不可再生的实拍素材被误拦时的逃生舱（等价 CLI `supply --skip-check`）
  const skipChk = uiCheck();

  const goBtn = h("button", { class: "lb-refine-go", disabled: "" }, "⇪ 直供为本镜画面");
  const tiles = [];
  const tile = (src, label, sub) => {
    const el = h("div", { class: "sup-tile",
        dataset: { tip: `${label}${sub ? " · " + sub : ""}\n点选后按右下「⇪ 直供为本镜画面」完成登记` } },
      h("img", { src, loading: "lazy", alt: "" }),
      h("i", null, label));
    el.onclick = () => {
      picked = picked === src ? null : src;
      tiles.forEach((t) => t.classList.toggle("on", t === el && !!picked));
      goBtn.disabled = !picked;
    };
    tiles.push(el);
    return el;
  };
  // 来源一：本章其他镜的画面（同图换运镜）——排除转场镜与本镜自身
  const reuse = (d.shots || [])
    .filter((x) => x.kind !== "transition" && x.image && String(x.id) !== String(s.id))
    .map((x) => tile(x.image, `镜 ${x.id}`,
      (x.gen || {}).image?.provider === "supplied" ? "直供" : "AI 生成"));
  // 来源二：设定资产（场景/角色/道具设定图）——系列级复用资产跨章直供
  const KIND_ZH = { scene: "场景设定", character: "角色设定", prop: "道具设定" };
  const refs = (d.design_assets || []).filter((a) => a.thumb)
    .map((a) => tile(a.thumb, a.kind === "scene" ? "场景" : a.name, KIND_ZH[a.kind]));

  goBtn.onclick = async () => {
    const p = picked && mediaPath(picked);
    if (!p) return toast("先选一张要复用的画面", true);
    goBtn.disabled = true;
    try {
      const r = await post("/api/shot/supply", { project: d.project, chapter: d.id,
        shot: s.id, path: p, skip_check: skipChk.checked });
      await finish(`镜 ${s.id} 已复用该画面（零生图成本，已落待审）——记得给本镜换一种运镜`, r);
    } catch (err) { goBtn.disabled = false; toast(err.message, true); }
  };
  const uploadBtn = h("button", { class: "act-btn big",
    onclick: () => {
      const fin = h("input", { type: "file", accept: ".png,.jpg,.jpeg,.webp",
        style: "display:none" });
      fin.addEventListener("change", async () => {
        const f = fin.files && fin.files[0];
        fin.remove();
        if (!f) return;
        try {
          const qs = new URLSearchParams({ project: d.project, chapter: d.id,
            shot: String(s.id), name: f.name,
            skip_check: skipChk.checked ? "1" : "" }).toString();
          const res = await fetch(`/api/shot/upload?${qs}`, { method: "POST",
            headers: { "X-Csrf-Token": CSRF }, body: f });
          const r = await res.json();
          if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
          await finish(`镜 ${s.id} 素材已直供（零生图成本，已落待审）`, r);
        } catch (err) { toast(err.message, true); }
      });
      document.body.append(fin);
      fin.click();
    } }, "⬆ 上传本地图片");

  const group = (label, items) => items.length &&
    h("div", { class: "sup-group" },
      h("span", { class: "sup-k" }, label),
      h("div", { class: "sup-grid" }, items));
  const overlay = h("div", { class: "rf-overlay",
      onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "sup-wrap" },
      h("div", { class: "rf-head" },
        h("span", { class: "k" }, `素材直供 · SHOT ${String(s.id).padStart(2, "0")} · SUPPLY`,
          h("span", { class: "chip green", style: "margin-left:10px" }, "零生图成本")),
        h("button", { class: "rf-x", onclick: close }, "✕")),
      h("p", { class: "sup-intro" },
        "把现成图片直接登记为本镜画面，跳过 AI 生图。复用其他镜的画面＝「同图换运镜」",
        "（素材复用型解说的核心工法：推近看细节、拉远看全貌，同图不同动即是多机位）；",
        "也可上传产品图 / 实拍图 / 截图。与 AI 生成同制度：旧版自动归档可回滚，登记后落「待审」。"),
      h("div", { class: "sup-scroll" },
        group("复用本章画面 · 同图换运镜", reuse),
        group("设定资产 · 场景 / 角色 / 道具", refs),
        !reuse.length && !refs.length &&
          h("div", { class: "sup-empty" }, "本章还没有可复用的画面——可直接上传本地图片")),
      h("div", { class: "rf-foot" },
        h("div", null,
          h("span", { class: "rf-cost" }, "与生成同制度 · 旧版归档可回滚 · 登记后落待审"),
          h("label", { class: "vc-ack", style: "margin-top:6px",
              onclick: (e) => { if (!e.target.closest(".us-check")) skipChk.click(); } },
            skipChk, " 跳过素材体检（分辨率 / 宽高比 / alpha 只告警不拦；仅「解不出的图」会被拦下）")),
        h("div", { class: "lb-foot-btns" }, uploadBtn, goBtn))));
  document.addEventListener("keydown", esc);
  document.body.append(overlay);
}

/* ═══════════ ⛭ 垫图参考选择器（镜级参考库覆盖）═══════════
   本镜生图默认套用项目参考库里「默认启用（✓）」的垫图。这里从参考库快捷勾选/取消
   本镜要用哪些（无需重新上传）：默认预勾选启用项，取消后本镜只靠提示词。
   写 shots[].refs 覆盖默认；「恢复默认」清除覆盖回落参考库默认集。 */
async function openRefsDialog(d, s) {
  let proj;
  try { proj = await api(`/api/project?id=${encodeURIComponent(d.project)}`); }
  catch (err) { return toast(err.message, true); }
  const lib = proj.moodboard || [];
  // 两种模式：分镜图（s.id / shots[].refs）｜设定图（s.asset={kind,name} / 实体 refs·scene_refs）
  const asset = s.asset || null;
  const unit = asset ? "本图" : "本镜";
  const aLabel = asset
    ? (asset.kind === "scene" ? "场景" : (asset.name || asset.kind)) + "设定图"
    : `镜 ${s.id}`;
  const aTitle = asset
    ? `垫图参考 · ${asset.kind === "scene" ? "场景" : (asset.name || asset.kind)} · MOODBOARD`
    : `垫图参考 · SHOT ${String(s.id).padStart(2, "0")} · MOODBOARD`;
  // 当前生效选择：本镜/本图有显式 refs → 用它；否则 = 参考库默认启用项
  let curRefs = s.refs;
  if (asset && !Array.isArray(curRefs)) {   // 设定图：从项目数据解析该张 refs
    curRefs = asset.kind === "character" ? (proj.characters || []).find((c) => c.name === asset.name)?.refs
      : asset.kind === "prop" ? (proj.props || []).find((p) => p.name === asset.name)?.refs
      : asset.name ? (proj.scenes || []).find((sc) => sc.name === asset.name)?.refs
      : proj.scene_refs;   // 具名取景地读实体 refs，与后端 _asset_ref_holder 同分派
  }
  const hasOwn = Array.isArray(curRefs);
  const close = () => { overlay.remove(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const picked = new Set(hasOwn ? curRefs
    : lib.filter((m) => m.on !== false).map((m) => m.path));
  const tiles = [];
  const syncTiles = () => tiles.forEach((t) => t.el.classList.toggle("on", picked.has(t.path)));
  const mkTile = (m) => {
    const el = h("div", { class: "sup-tile ref-tile",
        dataset: { tip: (m.on === false ? "参考库中已停用（默认不套用）" : "参考库默认启用")
          + `\n点选＝${unit}使用这张垫图，再点取消` } },
      h("img", { src: m.url, loading: "lazy", alt: "" }),
      h("span", { class: "ref-check" }, "✓"),
      h("i", null, m.on === false ? "停用" : "默认"));
    el.onclick = () => { picked.has(m.path) ? picked.delete(m.path) : picked.add(m.path); syncTiles(); };
    tiles.push({ el, path: m.path });
    return el;
  };
  const grid = h("div", { class: "sup-grid" }, ...lib.map(mkTile));
  syncTiles();

  const uploadBtn = h("button", { class: "act-btn",
    onclick: () => {
      const fin = h("input", { type: "file", accept: "image/*", style: "display:none" });
      fin.addEventListener("change", async () => {
        const f = fin.files && fin.files[0]; fin.remove();
        if (!f) return;
        if (f.size > 30 * 1024 * 1024) return toast("图片超过 30MB 上限", true);
        toast("上传到参考库…");
        try {
          const qs = new URLSearchParams({ project: d.project, name: f.name }).toString();
          const res = await fetch(`/api/moodboard/upload?${qs}`,
            { method: "POST", headers: { "X-Csrf-Token": CSRF }, body: f });
          const r = await res.json();
          if (!res.ok || r.error) throw new Error(r.error || `HTTP ${res.status}`);
          const fresh = await api(`/api/project?id=${encodeURIComponent(d.project)}`);
          (fresh.moodboard || []).filter((m) => !lib.some((o) => o.path === m.path))
            .forEach((m) => { lib.push(m); picked.add(m.path); grid.append(mkTile(m)); });
          syncTiles();
          toast(`已加入参考库并选中${unit}`);
        } catch (err) { toast(err.message, true); }
      });
      document.body.append(fin); fin.click();
    } }, "＋上传到参考库");
  const allBtn = h("button", { class: "act-btn", onclick: () => {
    lib.forEach((m) => picked.add(m.path)); syncTiles(); } }, "全选");
  const noneBtn = h("button", { class: "act-btn", onclick: () => {
    picked.clear(); syncTiles(); } }, `${unit}不用垫图`);

  // 写覆盖：设定图 → /api/asset/refs（实体 refs/scene_refs）；分镜 → /api/shot/refs
  const writeRefs = (clear) => asset
    ? post("/api/asset/refs", clear
        ? { project: d.project, asset_kind: asset.kind, asset_name: asset.name || null, clear: true }
        : { project: d.project, asset_kind: asset.kind, asset_name: asset.name || null, refs: [...picked] })
    : post("/api/shot/refs", clear
        ? { project: d.project, chapter: d.id, shot: s.id, clear: true }
        : { project: d.project, chapter: d.id, shot: s.id, refs: [...picked] });
  const afterWrite = () => asset ? softRefreshProject(d.project) : softRefresh(d.project, d.id);
  const resetBtn = h("button", { class: "act-btn",
    dataset: { tip: `清除${unit}的单独选择，回到「跟随参考库默认」——之后参考库开关变化${unit}自动跟随` },
    onclick: async () => {
      try { await writeRefs(true); close();
        toast(`${aLabel} 垫图已恢复默认（跟随参考库）`); await afterWrite(); }
      catch (err) { toast(err.message, true); } } }, "恢复默认");
  const saveBtn = h("button", { class: "act-btn",
    onclick: async () => {
      try { await writeRefs(false); close();
        toast(`${aLabel} 垫图选择已保存（${picked.size} 张）——下次重生按此`); await afterWrite(); }
      catch (err) { toast(err.message, true); } } }, "仅保存选择");
  const goBtn = h("button", { class: "lb-refine-go",
    onclick: async () => {
      goBtn.disabled = true;
      try {
        await writeRefs(false);
        if (asset) {   // 设定图：project refs --only --force 全新出图，灯箱内轮询收尾
          const r = await post("/api/asset/regen-refs",
            { project: d.project, asset_kind: asset.kind, asset_name: asset.name || null });
          close();
          toast(`${aLabel} 按新垫图重新生成中…`);
          const it = s.it;
          if (it) {
            genWait($("#lb-canvas"), "重新生成中");
            pollJob(r.job, {
              onDone: async () => { genWaitOff($("#lb-canvas"));
                it.src = withBust(it.src, Date.now());
                toast(`${aLabel} 已按新垫图重生`); renderLightbox();
                await reloadLBContext(it); await softRefreshProject(d.project); },
              onFail: (j) => { genWaitOff($("#lb-canvas"));
                toast(`重生失败：${(j.tail || "").slice(-160)}`, true); },
            });
          } else { await softRefreshProject(d.project); }
        } else {
          const r = await post("/api/regen", { project: d.project, chapter: d.id, shot: s.id });
          trackJob(jobKey(d.project, d.id, s.id), r.job, "regen", d.project, d.id);
          close();
          toast(`镜 ${s.id} 按新垫图选择重新生成中…`);
          await softRefresh(d.project, d.id);
        }
      } catch (err) { goBtn.disabled = false; toast(err.message, true); }
    } }, "保存并重新生成");

  const overlay = h("div", { class: "rf-overlay",
      onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "sup-wrap" },
      h("div", { class: "rf-head" },
        h("span", { class: "k" }, aTitle,
          hasOwn
            ? h("span", { class: "chip", style: "margin-left:10px" }, `${unit}已单独设定`)
            : h("span", { class: "chip green", style: "margin-left:10px" }, "跟随参考库默认")),
        h("button", { class: "rf-x", onclick: close }, "✕")),
      h("p", { class: "sup-intro" },
        `${asset ? "这张设定图" : "本镜"}生图默认套用项目参考库里「默认启用」的垫图（转 base64 随请求提交）。`,
        `在此勾选/取消${unit}要用哪些——取消勾选＝${unit}不套用该张；全不选＝${unit}只靠提示词；`,
        `「恢复默认」＝清除${unit}单独设定、跟随参考库。`),
      h("div", { class: "ref-quick" }, allBtn, noneBtn, uploadBtn),
      h("div", { class: "sup-scroll" },
        lib.length ? grid
          : h("div", { class: "sup-empty" },
              "参考库为空——先到项目页「参考库」上传参考图，或用上方「＋上传到参考库」直接上传。")),
      h("div", { class: "rf-foot" },
        h("span", { class: "rf-cost" },
          asset ? "写设定图 refs 覆盖默认 · 不改参考库本身" : "写 shots[].refs 覆盖默认 · 不改参考库本身"),
        h("div", { class: "lb-foot-btns" }, resetBtn, saveBtn, goBtn))));
  document.addEventListener("keydown", esc);
  document.body.append(overlay);
}

/* 写操作后的统一刷新：章节视图静默重渲 + 侧栏角标更新 */
async function refreshAfterWrite(d) {
  try {
    await getOverview(true);
    renderRail(state.overview);
    // 路由校验带 pid/cid（与 app.js 渲染守卫同纪律）：只判 name 的话，
    // 用户已切到别的章节时，这次迟到的重渲会把别章内容画进当前页
    const r = state.route;
    if (r?.name === "chapter" && r.pid === d.project && r.cid === d.id) {
      await viewChapter($("#view"), d.project, d.id, { silent: true });
    }
  } catch { /* 静默 */ }
}

/* —— 模块导出 —— */
export { openRefsDialog, openSupplyDialog, refreshAfterWrite };
