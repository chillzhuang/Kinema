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

/* 说话人签：空 speaker 与各种旁白别名一律显示「旁白」，角色显示本名。
   别名表与引擎的 voicecast.NARRATOR_NAMES 同一份口径——两边分叉的话，
   引擎按旁白编译提示词、页面却把 `VO` 当成一个角色名显示出来。 */
const NARRATOR_ALIASES = ["旁白", "narrator", "voiceover", "vo", "画外音"];

const speakerLabel = (spk) => {
  const s = (spk || "").trim();
  return !s || NARRATOR_ALIASES.includes(s.toLowerCase()) ? "旁白" : s;
};

/* 本镜的台词句序列——页面读台词的共用口径。
   写了 `shots[].lines[]` 引擎就只按句走，不再读 narration，也绝不反向回填
   （voicecast.shot_lines / shot_text 同款判据）；只写了句的镜 narration 是空的，
   各处自己去读它就把整镜台词报成了「无台词」。 */
const shotLines = (shot) => {
  const lines = shot.lines || [];
  if (lines.length) return lines;
  const narr = (shot.narration || "").trim();
  return narr ? [{ speaker: shot.speaker, text: narr }] : [];
};

/* 本镜的说话人（去重后的签）。点名要不要出、镜级 speaker 该不该再冠一次，
   看的都是这一个数。 */
const shotSpeakers = (shot) =>
  [...new Set(shotLines(shot).map((ln) => speakerLabel(ln.speaker)))];

/* 一行式台词。点名只在真有不止一个说话人时出——单说话人的镜逐句冠名是噪声，
   「谁在说」自有分镜卡的名牌栏和分镜表的说话人列回答。
   拼接不加分隔符：作者写 lines[] 时 narration 就是各句首尾相接，两种写法得读出同一串。 */
const shotSpeech = (shot) => {
  const lines = shotLines(shot);
  return shotSpeakers(shot).length > 1
    ? lines.map((ln) => `${speakerLabel(ln.speaker)}：${ln.text}`).join(" ／ ")
    : lines.map((ln) => ln.text).join("");
};

export { displayEmotion, shotLines, shotSpeakers, shotSpeech, speakerLabel };
