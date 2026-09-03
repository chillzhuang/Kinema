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

/* ═ Studio 前端模块 · app/brands.js — 服务商品牌标（原生 ES Module·免构建）═

   【出处与许可】图形取自 Simple Icons v16.28.0（https://simple-icons.org），
   该项目的 SVG 以 **CC0-1.0** 释出。商标本身归各自所有者：此处仅用于**标识**
   对应的服务商（指名性使用），不表示任何背书、赞助或授权关系。

   【为什么内联而不是当图片下载】一是这一页在离线环境也要能用，运行时再去取
   第三方 CDN 等于给本地工具引一条外网依赖；二是单色路径能直接吃 currentColor，
   一份图形在卡片、抽屉、路由牌三处按不同尺寸与色调复用，换成位图就得备三套。
   六枚合计约 3KB，比一次 HTTP 往返还小。

   【两处刻意的偏离，都不是随手改的】
     · 火山引擎在 Simple Icons 里没有独立条目，用其母公司 ByteDance 的标——
       火山引擎是字节跳动的云服务品牌，用母品牌标识它比退回首字母准确。
     · ElevenLabs 的官方色是纯黑，在本站深色底上对比度只有 1.17，等于看不见；
       深色底取白是其品牌规范给出的反白变体，不是我们自己调的色。
   除此之外一律用官方色原值（最低对比度 4.35，高于图形件 3:1 的门槛）。**刻意不为了
   「统一进琥珀/蓝/青三色体系」把品牌色改掉**：品牌标的辨识度一半在色相上，全部染成
   同一种颜色就从标识退化成装饰，那还不如留着首字母。 */
import { h } from "./core.js";

/* 键 = 后端 `config_overlay.IMPL_META` 里的 vendor 字段，两边必须对齐（有守卫）。 */
const BRANDS = {
  "火山引擎": { title: "ByteDance", color: "#3C8CFF",
    art: '<path d="M19.8772 1.4685L24 2.5326v18.9426l-4.1228 1.0563V1.4685zm-13.3481 9.428l4.115 1.0641v8.9786l-4.115 1.0642v-11.107zM0 2.572l4.115 1.0642v16.7354L0 21.428V2.572zm17.4553 5.6205v11.107l-4.1228-1.0642V9.2568l4.1228-1.0642z"/>' },
  "Google": { title: "Google Gemini", color: "#8E75B2",
    art: '<path d="M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81"/>' },
  "阿里云": { title: "Alibaba Cloud", color: "#FF6A00",
    art: '<path d="M3.996 4.517h5.291L8.01 6.324 4.153 7.506a1.668 1.668 0 0 0-1.165 1.601v5.786a1.668 1.668 0 0 0 1.165 1.6l3.857 1.183 1.277 1.807H3.996A3.996 3.996 0 0 1 0 15.487V8.513a3.996 3.996 0 0 1 3.996-3.996m16.008 0h-5.291l1.277 1.807 3.857 1.182c.715.227 1.17.889 1.165 1.601v5.786a1.668 1.668 0 0 1-1.165 1.6l-3.857 1.183-1.277 1.807h5.291A3.996 3.996 0 0 0 24 15.487V8.513a3.996 3.996 0 0 0-3.996-3.996m-4.007 8.345H8.002v-1.804h7.995Z"/>' },
  "MiniMax": { title: "MiniMax", color: "#E73562",
    art: '<path d="M11.43 3.92a.86.86 0 1 0-1.718 0v14.236a1.999 1.999 0 0 1-3.997 0V9.022a.86.86 0 1 0-1.718 0v3.87a1.999 1.999 0 0 1-3.997 0V11.49a.57.57 0 0 1 1.139 0v1.404a.86.86 0 0 0 1.719 0V9.022a1.999 1.999 0 0 1 3.997 0v9.134a.86.86 0 0 0 1.719 0V3.92a1.998 1.998 0 1 1 3.996 0v11.788a.57.57 0 1 1-1.139 0zm10.572 3.105a2 2 0 0 0-1.999 1.997v7.63a.86.86 0 0 1-1.718 0V3.923a1.999 1.999 0 0 0-3.997 0v16.16a.86.86 0 0 1-1.719 0V18.08a.57.57 0 1 0-1.138 0v2a1.998 1.998 0 0 0 3.996 0V3.92a.86.86 0 0 1 1.719 0v12.73a1.999 1.999 0 0 0 3.996 0V9.023a.86.86 0 1 1 1.72 0v6.686a.57.57 0 0 0 1.138 0V9.022a2 2 0 0 0-1.998-1.997"/>' },
  "ElevenLabs": { title: "ElevenLabs", color: "#E9EBF1",
    art: '<path d="M4.6035 0v24h4.9317V0zm9.8613 0v24h4.9317V0z"/>' },
  /* 本地曲库不是服务商，没有品牌标可用。给一枚与侧栏导航同一套线框语汇的自绘图标，
     取中性文字色——它在这一页的语义正是「不出网、不花钱」。 */
  "本地": { title: "本地曲库", color: "#a6adbd", line: true,
    art: '<path d="M3 7.2A2.2 2.2 0 0 1 5.2 5h3.9l2 2.6h7.7A2.2 2.2 0 0 1 21 9.8v8.4a2.2 2.2 0 0 1-2.2 2.2H5.2A2.2 2.2 0 0 1 3 18.2z"/>'
       + '<path d="M11 17.2v-5.6l4.4-.9v5"/><circle cx="9.6" cy="17.3" r="1.45"/>'
       + '<circle cx="14" cy="16.4" r="1.45"/>' },
};

const brandOf = (vendor) => BRANDS[vendor] || null;

/** 首字标：没有品牌标时的兜底。自定义接入的别名可能是任何厂商，不凭空生成占位标识，缺图时保留文字。 */
const initial = (p) => (p.vendor || p.alias || "?")
  .replace(/[^A-Za-z一-龥]/g, "").slice(0, 1)
  || (p.alias || "?").slice(0, 1).toUpperCase();

const artSvg = (b) => `<svg viewBox="0 0 24 24" aria-hidden="true">${b.art}</svg>`;

/** 卡片与抽屉头部的方形标（38px）。 */
export function vendorMark(p) {
  const b = brandOf(p.vendor);
  if (!b) return h("span", { class: "cfg-mark" }, initial(p));
  return h("span", { class: "cfg-mark brand" + (b.line ? " line" : ""),
    style: `--brand:${b.color}`, title: b.title, html: artSvg(b) });
}

/** 行内小标（12px），跟在路由牌与选项行的厂商名前面。无品牌标时返回 null。 */
export function vendorGlyph(vendor) {
  const b = brandOf(vendor);
  return b ? h("span", { class: "cfg-bmk" + (b.line ? " line" : ""),
    style: `--brand:${b.color}`, title: b.title, html: artSvg(b) }) : null;
}
