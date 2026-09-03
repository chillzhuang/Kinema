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

/* ═ Studio 前端模块 · app/shot-display.js — 分镜展示层词汇 ═ */

// emotion 是引擎/TTS 使用的稳定英文枚举，不改盘上值，只在面向用户的分镜表翻译。
// 未登记值原样返回，避免把自由文本或用户明确指定的英文静默改错。
const EMOTION_ZH = Object.freeze({
  neutral: "中性", calm: "平静", curious: "好奇", serious: "严肃",
  surprised: "惊讶", happy: "愉悦", tender: "温柔", sad: "悲伤",
  angry: "愤怒", fear: "恐惧", fearful: "恐惧", excited: "兴奋",
  coldness: "冷峻", gentle: "轻柔", disgusted: "厌恶", fluent: "流畅",
  whisper: "低语",
});

const displayEmotion = (value) => {
  if (value == null || value === "") return value;
  const key = String(value).trim().toLowerCase();
  return EMOTION_ZH[key] || value;
};

export { displayEmotion };
