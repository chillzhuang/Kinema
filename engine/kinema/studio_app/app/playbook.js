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

/* ═ Studio 前端模块 · app/playbook.js — 指令集 PLAYBOOK（原生 ES Module·免构建）═ */

/* ---------------- 视图：指令集（PLAYBOOK）----------------
   怎么与 Claude Code 配合：协作模型 → skill 入口 → 网页↔Claude 协作范式 →
   CLI 速查 → 漫剧从 0 到成片实战。全部静态内容，命令口径与 cli.py 对齐。 */
import { chip } from "./components.js";
import { costTotal, fmtDate, fmtDur, fmtSize, h, rich, state, toast } from "./core.js";
import { motionBadge, openCinema, profileChip, secHeader } from "./widgets.js";

function viewGuide(view) {
  const copyBtn = (text) => h("button", { class: "cmt-act",
    dataset: { tip: "复制到剪贴板" },
    onclick: async (e) => { e.stopPropagation();
      try { await navigator.clipboard.writeText(text); toast("已复制——粘贴给 AI 或终端"); }
      catch { toast("复制失败：浏览器未授权剪贴板", true); } } }, "⧉");
  const say = (text, desc) => h("div", { class: "gd-say" },
    h("code", null, text), desc && h("span", null, desc), copyBtn(text));
  const cli = (cmd, desc) => h("div", { class: "gd-cmd" },
    h("code", null, cmd), desc && h("span", null, desc), copyBtn(cmd));

  // 01 协作模型
  view.append(secHeader("01", "协作模型", "HOW IT WORKS"));
  view.append(h("div", { class: "gd-flow" },
    h("div", { class: "card gd-node" }, h("span", { class: "k" }, "① 你 · 说需求"),
      h("p", null, "在 AI 里用自然语言或斜杠指令描述想做的视频——一句话即可，",
        "细节交给 AI 追问或自由发挥。")),
    h("span", { class: "gd-arrow" }, "→"),
    h("div", { class: "card gd-node" }, h("span", { class: "k" }, "② AI · 指挥层"),
      h("p", null, "一切需要智能的环节：联网调研、写文案、拆分镜、写双语提示词、",
        "选画风与音色、调度引擎、按你的意见迭代。")),
    h("span", { class: "gd-arrow" }, "→"),
    h("div", { class: "card gd-node" }, h("span", { class: "k" }, "③ 引擎 · 确定性执行"),
      h("p", null, "生图 / 配音 / 字幕 / 图生视频 / FFmpeg 合成 / 版本栈与成本台账——",
        "机械环节零智能全可控，产物落 project/ 项目库。")),
    h("span", { class: "gd-arrow" }, "→"),
    h("div", { class: "card gd-node" }, h("span", { class: "k" }, "④ 本大屏 · 审阅与表态"),
      h("p", null, "你在 Studio 看进度、过审打回、打点提意见、直供素材——",
        "表态自动回流给 AI 与引擎，闭环迭代。"))));

  // 02 对话即生产（自然语言优先，斜杠指令只是快捷方式）——你说/Claude 对仗排布
  view.append(secHeader("02", "对话即生产", "JUST SAY IT"));
  const talk = (title, quote, does) => h("div", { class: "card gd-talk" },
    h("b", { class: "gd-talk-t" }, title),
    h("div", { class: "gd-quote" }, h("i", null, "你说"),
      h("code", null, quote), copyBtn(quote)),
    h("div", { class: "gd-resp" }, h("i", null, "AI"),
      h("p", null, does)));
  view.append(h("p", { class: "gd-lead", style: "margin:0 0 12px" },
    "不需要记命令、不需要懂参数——对 AI 用自然语言把需求说清楚就行，",
    "它会自动匹配最合适的画风、替你跑完全部命令并逐步汇报；斜杠指令只是精确直达的快捷方式。"),
    h("div", { class: "gd-talks" },
    talk("从零开始一条片",
      "帮我把《重生之我是甲方》做成一集 30 秒职场漫剧",
      "自动匹配画风（剧情→动漫·现代新番）、联网调研热梗、写文案拆分镜、立项开跑——"
      + "默认在文案、设定集、首镜、配音、合成每个节点停下来等你确认。"),
    talk("全自动不打扰",
      "做一条赛博朋克风的雨夜巡逻短片，直接一条龙跑完 --auto",
      "不再逐门确认：文案→设定→生图→配音→合成一次跑完并自动过审，你只看成片；"
      + "有不满意的镜再逐个打回。"),
    talk("中途改需求",
      "第 3 镜换成雨夜，全片色调再冷一点",
      "镜级修改直接改该镜提示词重生成；全片级修改走 batch edit 批量编译执行——"
      + "已通过锁定的镜不会被误伤，改动全部进版本栈可回滚。"),
    talk("对着画面挑毛病",
      "镜 5 的手画崩了，右下角的平板换成牛皮纸文件袋",
      "把意见定位到镜与区域后重生成或局部改造（只重绘那一块）；"
      + "你也可以直接在本大屏点开图打点写意见，重新生成时自动带上。"),
    talk("先看效果再花钱",
      "先别烧钱，全部用样片跑一遍看看节奏",
      "用 mock / animatic 零成本出全片样片；要动态化时先 dry-run 报价单给你过目，"
      + "点头才正式生成，且只跑已过审的镜。"),
    talk("整体换风格重做",
      "感觉不对，整体换成水墨风重来",
      "换画风快照（project set --style-prompt）后按新风格重生全片画面——"
      + "文案、配音、分镜结构全部保留，只换视觉层。")));

  // 03 skill 入口
  view.append(secHeader("03", "SKILL 入口", "ENTRYPOINTS", null));
  view.append(h("div", { class: "card gd-card" },
    h("p", { class: "gd-lead" },
      "对 AI 输入斜杠指令＋主题即启动整套「主题 → 成片」流程；",
      "不点名画风用各 skill 的默认档，点名（如「宫崎骏那种」）自动换档。"),
    say("/kn-anime 一个转校生隐藏身份的小剧场，30 秒对话短剧", "动漫 15 画风 · 默认现代新番"),
    say("/kn-anime 用吉卜力画风讲一个夏日乡间故事", "点名画风 → ghibli"),
    say("/kn-game 勇者迷宫寻宝小剧场", "游戏叙事 11 风格 · HD-2D/GBA/暗黑奇幻…"),
    say("/kn-clay 一只粘土小猫的早餐日常", "定格 4 质感 · 粘土/高达/手办/积木"),
    say("/kn-anime3d 一只橘猫的午后", "写实 3D 五路线 · 国漫｜爱死机｜数字人与宠物｜虚拟制片｜黑色写实"),
    say("/kn-showcase 三页图讲清我们的新产品", "素材复用型解说 · 生图按资产张数计费"),
    say("/kinema 三分钟讲明白郑和下西洋", "通用口播 / 图文（不限画风）"),
    h("p", { class: "gd-note" }, "更多单画风入口见总览「风格档」分组——",
      "/kn-cyberpunk 赛博朋克 · /kn-quote 语录 · /kn-ranking 榜单 · ",
      "/kn-miniature 微缩 · /kn-storybook 绘本 · /kn-explainer 图解")));

  // 04 网页 ↔ Claude 协作范式
  view.append(secHeader("04", "网页 ↔ AI 协作范式", "FEEDBACK LOOPS"));
  view.append(h("div", { class: "gd-loops" },
    h("div", { class: "card gd-loop" }, h("b", null, "⧉ 指令台"),
      h("p", null, rich("提示词与剧本不在网页改。分镜卡「⧉ 改镜指令」这类按钮点开的是"
        + "**指令台**：上半是带定位坐标（项目/章节/镜号/JSON 路径/当前双语提示词）的标准指令，"
        + "下半写你这次的要求——合并后一次复制给 AI，秒定位改本镜并重生成。"))),
    h("div", { class: "card gd-loop" }, h("b", null, "◉ 提意见 → ↻ 重新生成"),
      h("p", null, "点开分镜图打点 / 划线圈范围写意见，可连提多条零成本；",
        "「↻ 重新生成」时全部意见自动带九宫格方位词编译进下一版提示词。",
        "审核通过后意见自动清理，不留到新图上。")),
    h("div", { class: "card gd-loop" }, h("b", null, "⇪ 素材直供"),
      h("p", null, "现成图直接登记为分镜画面（零生图成本）：复用本章其他镜画面＝",
        "「同图换运镜」多机位工法，或上传产品图 / 实拍图 / 截图。",
        "与 AI 生成同制度：旧版归档可回滚、登记后落待审。")),
    h("div", { class: "card gd-loop" }, h("b", null, "⏎ 视频锚定"),
      h("p", null, "审成片时暂停在问题画面按回车，意见落锚到当前镜；点击锚点即跳转复看，",
        "重新生成该镜时意见自动进提示词。重新剪辑后时间自动跟随，永不漂移。"))));

  // 05 CLI 速查（Claude 替你跑，列此供核对与手动兜底）
  view.append(secHeader("05", "命令速查", "CLI CHEATSHEET"));
  view.append(h("p", { class: "gd-lead", style: "margin:0 0 12px" },
    "以下命令通常由 AI 替你执行——列在这里供核对进度、理解产物与手动兜底；",
    "日常使用只需要上面的自然语言对话。"));
  const cliGroup = (title, rows) => h("div", { class: "card gd-card" },
    h("span", { class: "k" }, title), ...rows);
  view.append(h("div", { class: "gd-cli" },
    cliGroup("立项与设定（引擎在 engine/ 目录运行）", [
      cli("python3 -m kinema project new --title \"X\" --id x --profile anime", "立项（画风快照进 style_prompt）"),
      cli("python3 -m kinema chapter new x --title \"本集标题\"", "建章节（钩子式短标题）"),
      cli("python3 -m kinema project refs x --candidates 4", "设定集：角色三视图/场景图候选宫格"),
    ]),
    cliGroup("生成与合成", [
      cli("python3 -m kinema gen-image --chapter x/ch01 --only 1", "先首镜确认风格，再续跑其余"),
      cli("python3 -m kinema voice custom x --narrator --prompt \"<声线描述>\" --adopt 1", "旁白按描述定制立档（缺省路径）"),
      cli("python3 -m kinema tts --chapter x/ch01", "全片配音轨"),
      cli("python3 -m kinema assemble --chapter x/ch01", "合成：字幕 → BGM → 成片"),
      cli("python3 -m kinema animatic --chapter x/ch01", "零成本全片样片（节奏审）"),
    ]),
    cliGroup("动态化（烧钱前必先 dry-run）", [
      cli("python3 -m kinema gen-video --chapter x/ch01 --dubbed --dry-run", "逐镜提示词＋成本预估"),
      cli("python3 -m kinema gen-video --chapter x/ch01 --dubbed --approved-only", "只跑已过审镜"),
      cli("python3 -m kinema assemble --chapter x/ch01 --dubbed", "以动态片段重合成"),
    ]),
    cliGroup("审阅与迭代", [
      cli("python3 -m kinema review set --chapter x/ch01 --shots 3 --stage image --state retake --note \"手部画崩\"", "打回重做（意见进驳回闭环）"),
      cli("python3 -m kinema refine --chapter x/ch01 --shot 3 --rect \"0.6,0.1,0.3,0.3\" --note \"把平板换成文件袋\"", "局部改造只改一处"),
      cli("python3 -m kinema supply --chapter x/ch01 --shot 4 --file 图.png", "素材直供为分镜画面"),
      cli("python3 -m kinema transition add --chapter x/ch01 --after 3 --text \"几天后\"", "插转场字卡（零成本）"),
    ]),
    cliGroup("封面与交付", [
      cli("python3 -m kinema cover x --all", "封面：竖 3:4＋横 4:3 双套"),
      cli("python3 -m kinema watermark --chapter x/ch01", "动态水印（防搬运）"),
      cli("python3 -m kinema studio --port 8787", "启动本大屏"),
    ])));

  // 06 漫剧实战：从 0 到成片
  view.append(secHeader("06", "漫剧实战 · 从 0 到成片", "FROM ZERO TO FILM"));
  const WHO = { you: ["你 → AI", "amber"], st: ["你 · Studio", "green"],
                cl: ["AI → 引擎", "blue"] };
  const step = (n, who, title, body, code) => h("div", { class: "card gd-step" },
    h("div", { class: "gd-step-head" },
      h("b", { class: "gd-step-no" }, String(n).padStart(2, "0")),
      h("span", { class: `chip ${WHO[who][1]}` }, WHO[who][0]),
      h("b", null, title)),
    h("p", null, body),
    code && (Array.isArray(code) ? code : [code]).map((c) =>
      typeof c === "string" ? cli(c) : c));
  view.append(h("div", { class: "gd-steps" },
    step(1, "you", "一句话发起",
      "对 AI 说出主题即可——AI 联网调研热梗、写钩子文案、拆 8~12 镜、"
      + "写双语提示词并立项（未点名画风默认现代新番，画风快照全片统一）。",
      say("/kn-anime 帮我做一集 30 秒职场逆袭漫剧《重生之我是甲方》", "默认逐节点停下等你确认；加 --auto 一条龙跑完")),
    step(2, "cl", "设定集 · 一致性根基",
      "AI 生成角色三视图模型单与场景设定图的候选宫格——这是全片角色不崩脸的根基。",
      "python3 -m kinema project refs jiafang --candidates 4"),
    step(3, "st", "宫格点选定稿",
      "在 Studio 项目页的候选宫格里点选最像的一张即锁定（原版自动归档可回滚）；"
      + "之后每次生图引擎都会带上设定图做参考。"),
    step(4, "cl", "生图 · 先首镜后全量",
      "先只出第 1 镜确认画风立住了，再续跑其余——省下整批重roll 的钱。", [
      "python3 -m kinema gen-image --chapter jiafang/ch01 --only 1",
      "python3 -m kinema gen-image --chapter jiafang/ch01"]),
    step(5, "st", "分镜审阅 · 提意见闭环",
      "分镜卡逐镜表态：✓ 通过锁定 ｜ ◉ 点图打点提意见 → ↻ 重新生成自动带意见重画 ｜ "
      + "点开画面可框选局部手术 ｜ 画面要改点 ⧉ 改图指令（提示词+图片一次改到位）、"
      + "运镜要改点 ⧉ 改镜指令（只改文案），写清要求后复制粘给 AI。"),
    step(6, "cl", "配音 · 选角定制",
      "每个说话人写一段声线描述，引擎按描述定制一把并立档；要官方模版再试音五选一。随后一键出全片配音轨。", [
      "python3 -m kinema character set jiafang --name 师父 --voice-prompt \"六十岁男性，低沉沙哑，语速慢\"",
      "python3 -m kinema voice custom jiafang --narrator --prompt \"四十岁男性，中低音，纪录片腔\" --adopt 1",
      "python3 -m kinema tts --chapter jiafang/ch01"]),
    step(7, "cl", "合成 · 节奏审",
      "合成自动完成字幕烧录（音字一致）、BGM、转场衔接；animatic 出零成本样片先审节奏，"
      + "镜头拖沓就弃镜/改时长再重合成。", [
      "python3 -m kinema assemble --chapter jiafang/ch01",
      "python3 -m kinema animatic --chapter jiafang/ch01"]),
    step(8, "cl", "动态化 · 先报价后烧钱（可选）",
      "静图运镜已可交付；要对口型动画就走 Seedance——先 dry-run 看逐镜报价，"
      + "满意再只跑已过审的镜。", [
      "python3 -m kinema gen-video --chapter jiafang/ch01 --dubbed --dry-run",
      "python3 -m kinema gen-video --chapter jiafang/ch01 --dubbed --approved-only"]),
    step(9, "cl", "封面 · 水印 · 交付",
      "竖横双套封面（Studio 与投放平台自动适配）、防搬运动态水印，"
      + "最后在章节页一键导出审阅包 / 交付包。", [
      "python3 -m kinema cover jiafang --all",
      "python3 -m kinema watermark --chapter jiafang/ch01"])));
  view.append(h("p", { class: "gd-note", style: "margin-top:6px" },
    "以上每一步 AI 都会替你执行并汇报——你只需要在 Studio 里看、点、表态；",
    "想全自动就在最初那句话后面加 --auto。"));
}

/* ---------------- 指令集弹层：顶栏「指令集」的开启入口 ----------------
   内容与 `#/guide` 路由**同一个 viewGuide**——只是换个容器，两处永不漂移。
   宿主给足宽度（gd-talks / gd-loops 是 340 / 300px 的 auto-fit 网格，窄了会塌成
   单列、六张卡拉成一根长条），头固定、体滚动、足留一行提示。 */
function openGuideModal() {
  if (document.querySelector(".gdm-overlay")) return;     // 连点两下不叠两层
  const prevOverflow = document.body.style.overflow;
  const close = () => {
    document.removeEventListener("keydown", esc, true);
    document.body.style.overflow = prevOverflow;
    overlay.remove();
  };
  // 捕获阶段拦 Escape：与 uiDialog 同制式，别让全局那条（关灯箱/关检索）也跟着响
  const esc = (e) => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    e.preventDefault();
    close();
  };
  const body = h("div", { class: "gdm-scroll" });
  viewGuide(body);
  const overlay = h("div", { class: "rf-overlay gdm-overlay",
      onclick: (e) => { if (e.target === overlay) close(); } },
    h("div", { class: "gdm-wrap" },
      h("div", { class: "rf-head" },
        h("span", { class: "k gdm-title" }, "指令集",
          h("i", { class: "gdm-en" }, "PLAYBOOK")),
        h("button", { class: "rf-x", onclick: close, title: "关闭（Esc）" }, "✕")),
      body,
      h("div", { class: "rf-foot" },
        h("span", { class: "rf-cost" },
          "点任意一行的 ⧉ 复制，粘给 AI 即可开工 · Esc 关闭"))));
  document.body.style.overflow = "hidden";               // 弹层滚动时页面不跟着滚
  document.addEventListener("keydown", esc, true);
  document.body.append(overlay);
}

function videoCard(v) {
  const src = v.project ? `${v.project} / ${v.chapter}` : (v.theme || "");
  return h("div", { class: "card vcard", onclick: () => openCinema({
      video: v.video, poster: v.poster, title: v.title || v.theme,
      // 制作规格进 chips（画风/模式/比例一眼可辨），台账细节留行
      chips: [profileChip(v.profile), motionBadge(v.motion),
              v.aspect && chip(v.aspect)],
      size: v.size || null,
      rows: [
        ["出处", src || null],
        ["分镜", v.shots_count ? `${v.shots_count} 镜` : null],
        ["体积", fmtSize(v.size)], ["成本", costTotal(v.cost)],
        ["渲染于", fmtDate(v.mtime)], ["文件", v.name],
      ],
      link: v.project && v.chapter
        ? `#/project/${encodeURIComponent(v.project)}/${encodeURIComponent(v.chapter)}` : null,
    }) },
    h("div", { class: "vposter" },
      h("img", { src: v.poster, loading: "lazy", alt: "", onerror: (e) => e.target.remove() }),
      h("div", { class: "playhint" }, h("span", null, "▶")),
      v.aspect && h("span", { class: "asp" }, v.aspect),
      h("span", { class: "tc" }, fmtDur(v.duration))),
    h("div", { class: "vcard-body" },
      h("h4", null, v.title || v.theme),
      h("div", { class: "vcard-sub" },
        h("span", { class: "src" }, src),
        motionBadge(v.motion))));
}

/* —— 模块导出 —— */
export { openGuideModal, videoCard, viewGuide };
