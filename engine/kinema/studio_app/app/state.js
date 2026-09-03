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

/* ═ Studio 前端模块 · app/state.js — 跨模块共享可变状态（原生 ES Module·免构建）
   ESM 的 import 绑定只读，跨模块重赋值的状态必须收敛于此、经属性读写。
   只收「真被多模块赋值」的字段——单模块自有状态留在各自模块内。═ */

export const STATE = {
  chapSig: "",     // 章节视图签名（缓存击穿：置空触发下次轮询整页重绘）
};
