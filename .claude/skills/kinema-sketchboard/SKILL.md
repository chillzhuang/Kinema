---
name: kinema-sketchboard
description: "为单镜设计简笔分镜预演板和逐秒 beats。用户要草图分镜、九宫格分镜、逐秒运动脚本，或在生成视频前控制动作节奏时使用；与 3D previz、深度捕捉逐镜互斥。"
metadata:
  kinema-managed-by: "agent/manifest.json"
  kinema-kind: "capability"
  kinema-status: "stable"
  kinema-version: "2.0.0"
  kinema-owner: "Kinema"
  kinema-source: "workspace"
  kinema-trust: "first-party"
  kinema-digest: "sha256:5c5dd6bc3990a4c9200803adc4de8600be0c6bae00a6d245e78daeca97aeaeb4"
---
# kinema-sketchboard · 简笔分镜预演板

**先 Read `kinema/SKILL.md`**（流程真源）。本 skill 管一件事：在烧 Seedance
之前，用素描板与分段文本规划复杂动作、走位和机位节奏；是否准确执行仍需看样片。

三条产物、各锁一样东西：
- **beats**（`shots[].sketch.beats`，你写）——按动作阶段和镜头时长设计，编译成**分段时间轴**
  进视频提示词——格式随型号的 `timeline_unit`：`natural` 自然推进，`shot` 顺序编号，
  `second` 保留明确写出的秒段。当前 Seedance 2.0 配顺序段，2.5 配秒段。
  `t` 可省略；需要卡点才写，未定时的拍不会被自动均分；
- **简笔板**（`sketch gen` 生成）——铅笔素描网格（格数=拍数，恰好填满；素描灰阶为主，仅发光体/能量少量点缀色），红箭头=人物运动轨迹、
  蓝箭头=摄影机运动、绿=构图、橙=灯光、紫=声音情感、黑=编号与秒段标签；
  gen-video 时**缺省档板在盘即随请求附发**（native 缺省=全能参考·一镜一片：
  板+分镜图+角色/场景/道具设定图全作参考、不发首/末帧；dubbed 走参考媒体模式同样附板）；
  只有**衔接参与镜**（章级/镜级 `frame_chain`，首帧任务禁混参考图）只发分段时间轴、
  板不附——衔接章里逐镜 `sketch ref --shots N --state on` 才强制该镜切回参考孤岛；
- **末拍收束**——最后一拍必须落在 `end_state` 上（衔接章里它同时与链末帧天然一致）。

## 规划优先：需要预演时先写拍序，再画板

板是动作规划的可视化，适用于复杂调度和需要明确时序的镜：

1. **需要分段控制的镜写 authored beats**（`shots[].sketch.beats`）；
   **beats 单独存在即生效**——gen-video 自动把它编译成
   分段时间轴注入提示词（纯文本零成本，`sketch list` 显示
   「生效=sketch·纯时间轴」，dry-run 逐镜行标「分段时间轴(无板)」）。
2. **板从同一份 beats 生成**（`effective_beats` 单一真源）——先规划后画板，
   板与时间轴使用同一份设计。是否出板按可视化的必要性与预算决定。
3. **没写 beats 也没出板的镜**：引擎不注入时间轴（自动拆拍只在板/beats 语境下
   生效）。简单动作直接写正文即可，不强制分段；正文写法见 `kinema/references/video-prompting.md`。
4. **改了 video_prompt/beats 板不会自动跟上**：板条与 `sketch list` 会报
   「⚠ 拍序列已变」（与「⚠ 时长已变」同制度，判据 `gen.sketch.sig` 拍序列指纹），
   gen-video 附板前也会点名——重点镜记得 `sketch gen --only N --force` 重生，
   否则板与时间轴说的是两套动作，模型会二选一或折中。

## 与 3D 导演台、深度捕捉的互斥（铁律）

一个镜要么走 previz（首帧/末帧/V2V），要么走深度控制视频（实拍运动 V2V），要么走
简笔板（时间轴+参考图），**绝不同发**——三者都在向模型描述"这几秒怎么运动"，同发
必然打架。仲裁单一真源 `sketchboard.active_guide`：显式 `shots[].guide` 恒赢（即便
指向空槽也不静默回落，引擎会告警）；缺省自动仲裁 **previz > control > sketch**。
表态入口：`sketch use --shot N --guide sketch|previz|control|auto`，或 Studio 分镜卡的
仲裁徽章（配置了两路及以上时才出现，点击轮换）。缺省档（章不衔接）简笔镜就是全能参考：
时间轴管过程、板与设定图管外观，一镜一片；开了章级衔接（`frame_chain: true`）的章里
简笔镜与链末帧并存（时间轴管过程、链末帧管收束），板只当拍表不附发——要板随请求发
须逐镜「板作参考」表态（`sketch ref`，代价见「边界与成本」）。

## 工作流

1. **写 beats**：Read 分镜脚本（narration/action/video_prompt/end_state/light_shift）
   与原文对应段落，按关键动作阶段安排时间，写进 `shots[].sketch.beats`——正式通道是
   `agent plan` 的 ChapterPlan（`sketch.beats` 白名单，merge 语义不动引擎管的 `sheet`，
   带 `expected_revision` 并发保护）。拆拍工法见
   `references/board-craft.md`。**绝不发明剧情**——细节从原作搬运。
   **没写 beats 也能生成**：分镜词足够详细时，引擎按运动设计句读**自动拆拍**
   （`sketchboard.auto_beats`，纯标点切分零语义改写，authored beats 恒优先）——
   板与时间轴取同一份拍序列，自动拆拍的时间轴会**替代**散文正文（同句不发两遍）。
   自动拆拍只提供顺序，不生成秒数；关键事件需要定时时再手写 `t`。
   **秒段上限=实际请求秒数**（`request_seconds`，不是裸 `dur`）：无效时长、重叠或
   越界须修正；留白和局部定时只提示核对，不自动补满。板生成后镜头时长一变，
   板条与 `sketch list` 会打「⚠ 时长已变」，
   重生即可（灯箱「↻ 重新生成」）。
2. **先 1 镜出板验证**：`sketch gen --chapter x/ch --only 1`，Read 板图做**四查**
   （见下），过了再 `sketch gen` 全量（并发缺省 4）。
3. **质检四查**（引擎无 OCR，必须逐板 Read）：① 格数与编号齐全、按时间推进；
   ② 秒段标签与 beats 对拍；③ 每格姿态与该拍 action 一致，有意的停顿可以静止；
   ④ 箭头与实际动作对应，红/蓝语义不串。坏板 `--force` 重生或改 beats 措辞。
4. **表态 guide**：该镜要走板就 `sketch use --shot N --guide sketch`（若该镜从未
   配 previz，自动仲裁已选 sketch，可省）。
5. **（仅衔接章）挑镜开板作参考**：本章开了 `frame_chain: true` 且某镜的动作调度
   复杂到时间轴文字说不清时，`sketch ref --shots N --state on`——开一镜断一条接缝，
   只给真需要的镜开；缺省章（不衔接）板在盘即自动附发，无需此开关。
6. **审后发**：`gen-video --chapter x/ch --dry-run` 逐镜核对时间轴提示词与附板
   标记（缺省 native 镜显「全能参考(分镜图+简笔板·一镜一片)」；无板显
   「全能参考(分镜图·一镜一片)」；dubbed 显「参考图=分镜图+简笔板」；衔接章的
   首帧任务镜照常标末帧、板不附），无误后去掉 --dry-run 正式生成。

## beats 快则

- 拍数由动作阶段决定，不为填满网格新增动作。次要环境变化可以放在相关主拍，
  持续动作可以跨拍；爆发、停顿与反应按实际需要分配时间，不设手写拍长公式。
- **自动拆拍已由引擎按时长收敛**（`auto_beat_cap`：`min(12, 秒数÷1.2)` 再向下取到
  最近的**整齐拍数**，下限 2 拍，均匀并拍保时序不丢句）——不写 beats 直接
  `sketch gen` 时，5s/7s 镜出 4 拍、8s 出 6 拍、10s 出 8 拍，**不是固定 9 格**。
  素材不足不凭空造拍。
- **版式：网格恒按拍数恰好填满**（`grid_of`，引擎自动算，你不用管）——
  2/3 拍=单行、**4 拍=2×2**、6 拍=2×3、8 拍=2×4、9 拍=3×3、10 拍=2×5、12 拍=3×4。
  **优先写整齐拍数 2/3/4/6/8/9/10/12**：5/7/11 拍排不成矩形，末行只能居中留半排
  （引擎会明说「最后一行 N 格居中」并禁止补空框，但满格版面每格更大更好读）。
  历史事故：列数硬编码为 3 时，4 拍算出「2×3 网格共 4 个面板」自相矛盾，模型只能猜——
  同为 4 拍，一张排成 2×2 满格、另一张照 2×3 画完**留了两个空白框**。
- **手写 beats 引擎一个字不动**（创作资产）：超密只报「⚠ 拍密度」给建议，
  该并哪几拍是你的判断——见警必改，别指望引擎替你合。
- 开场与上一镜或参考图的姿态一致，关键拍之间保持动作连续；末拍对应剪辑结束点，
  **与 end_state 一致，可保持运动**。不强制起承收，也不因有意静止或重复动作判错。
- 打斗镜按主要动作、接触与受力反馈安排节奏，连续交锋不强加停顿，
  不把每个肢体变化拆成小数秒指令——完整工法见
  `kinema/references/action-direction.md`（因人设招/拆镜/升格纪律都在那）。
- 每拍 `{"t":"0-2s","action":"…","camera":"…","framing":"…","light":"…","sound":"…"}`
  ——`action` 必填（具体、物理、可画出来的运动），其余按需；`t` 缺省只标顺序。
  动作、开口时机与音效卡点在同一拍设计，准确台词交给 `lines[]`，不另排字数秒表。
- **authored beats 写全后，正文只补必要的镜级信息**，可以简短或省略；同一动作
  不在正文、delta 和拍序里反复描述。`end_state` 是结束状态，可以仍在运动，
  不强制减速或停稳。写作真源见 `kinema/references/video-prompting.md`。
- 板的拍数区间 2~12（`AUTO_BEATS_MIN`/`PANEL_MAX`），九格只是其中一种版式。

## 命令速查

```bash
python3 -m kinema sketch gen   --chapter x/ch [--only 1,3] [--force] [--mock] [--concurrency N]
python3 -m kinema sketch use   --chapter x/ch --shot N|--all --guide sketch|previz|control|auto
python3 -m kinema sketch list  --chapter x/ch          # beats/板/guide/生效路径一览
python3 -m kinema sketch clear --chapter x/ch --shot N # 摘板挂载（文件与 beats 保留）
python3 -m kinema gen-video    --chapter x/ch --dry-run # 审时间轴（板附发标记同源）
```

Studio：章节页「SK 简笔分镜台」区块（3D 导演台正下方）——统计三格 + 「▦ 生成
简笔板」选镜弹层 + 「⧉ 分镜板指令」（交回本 skill 写 beats）+ 板缩略条
（SHOT 签点击跳分镜卡·右上 ⧉ 复制单板改图指令·灯箱内逐拍对照表 + ↻ 重新生成）；
分镜卡「▦ 简笔」角标看板、仲裁徽章切路径。

## 边界与成本

- **板绝不进 `shots[].image`/`clip`**（分镜画面与成片位；板是预演观看物，落
  `<章节>_work/sketch/`，不走审阅状态机、不做版本栈，`--force` 直接覆盖）。
- 板生成**刻意不带画风前缀、不注入 moodboard 垫图**——板是素描基调、不掺成片画风，
  彩色垫图混入就是"给板上成片色"的邀请函；版式一致性靠引擎内置版式蓝图自动垫
  （`engine/kinema/assets/blueprints/sketch1/2/3_template.png` 三张同版式样板：
  单人动作/双人对话/双人战斗，只学版式与标注画法、绝不复制其人物与标题文字）。
  **蓝图放在引擎包内而不是本 skill 目录**是刻意的：它是引擎生成时自动垫的**运行时
  资产**（转 base64 塞进 ref_images），搬进 `.claude/skills/` 后 `pip install` 的用户
  那里根本不存在、垫图会静默失效。设计归属仍在 skill（契约由 `sketchboard.board_prompt`
  定义），出处与重做方法见 `engine/kinema/assets/README.md`。设定图侧有同款蓝图
  （角色/道具各一张简笔线稿 `char_template`/`prop_template`，见 `sheets.templates_for`）。
- 成片防泄漏：板附发时引擎**两处同时防**——正文头部注入「板不是画面参考、只是分镜
  脚本，格线与箭头是标注符号而非画面元素」，同一批词再补进负面串（标注箭头/红蓝
  箭头/分镜格线/铅笔素描…）。只在正文说一次压不住：16 镜里 6 镜带板，2 镜把
  红蓝箭头画进了开头几帧。绝不手工删改提示词绕过引擎拼装。
- 成本：板=一张图价（Seedream 0.3 元）；时间轴文本零成本。
- **附板是逐镜取舍，缺省不附**：能力闸（只有 seedance/mock 支持参考图）× 两条通道：
  dubbed 参考媒体模式与 native 缺省档（全能参考·一镜一片）板在盘即附；只有
  **衔接章**（`frame_chain: true`）里板让位给首帧任务，须 `sketch ref --shots N
  --state on` 显式切回参考孤岛（判据真源 `sketchboard.reference_shot`）。衔接章里的
  取舍要点：换来板与设定图入场（板级走位与机位节奏），**代价是这一镜没有首/末帧槽
  ——它既不向下一镜发末帧、上一镜也焊不到它身上**（分镜图降级成众多参考图之一，
  不再是第 0 帧硬锁），开头几帧由几张参考图调和而来、两侧接缝改软切。

### 开了「板作参考」＝这一镜是链上孤岛（强制要求 · 仅衔接章）

**开关一开，这一镜的两侧焊缝就都断了**——不是只断后面那一条。引擎会在**上下两处**
各自动落一个 `seamless` 无缝转场（0.1s ≈3 帧柔切、静音、`transition.auto="island"`），
成片里的形态就是：**正常衔接 → 软切 → 这一镜独立成段 → 软切 → 正常衔接**。

指挥层三条硬规矩：

1. **不要手工在孤岛镜两侧再加转场**。同一处已有转场时引擎不再补软切，你手写的
   `fade_black` 会原样保留——那是「叙事标点 + 参考模式断缝」两件事叠在一处，
   看上去像是章节在这里跳了两次。用户点名要叙事转场时才插，并告诉他它替代掉的是软切。
2. **不要连开一片**。相邻两镜都开 = 中间那条缝两端都不成立，整段被切得七零八落。
   板级调度是给「这一镜的走位必须精确」的单镜用的，不是全章档位。
3. **开完必须重看链态**：`gen-video --dry-run` 会打印每一处「自动插入无缝转场」
   与逐镜链态；或跑 `kinema transition sync` 单独同步一次（零成本）。
   **禁止在没看链态的情况下直接 `gen-video` 烧钱**——开关改的是整章的接缝结构。

细则（拆拍工法 / 箭头标注规范 / 板提示词契约逐段 / 艺术身份板选用）：
`references/board-craft.md`。
