# 简笔分镜板 sketch 纪律

**实现真源** `kinema/sketchboard.py` + `kinema-sketchboard` skill

## 1. 与 previz 逐镜互斥

仲裁唯一真源 `sketchboard.active_guide`：

- `guide=sketch` 的镜，previz 末帧与 V2V 一律不参与（`cli._shot_plan` ⓪ 号判据）；
- 显式表态指向空槽也**不静默回落**——引擎告警，而不是替用户改主意；
- 缺省自动仲裁 previz 优先；
- **缺省档（章不衔接）板在盘即随请求附发**（native 缺省=全能参考·一镜一片，
  见 §4.1）；只有衔接参与镜（章级/镜级 `frame_chain`，首帧任务禁混参考图）板不附、
  只发分段时间轴——衔接章里逐镜显式开「板作参考」才强制该镜切回参考孤岛。

## 2. beats 是指挥层写的创作资产，引擎绝不代编

无 LLM 是铁律。但分镜词详细时，引擎按运动设计**句读自动拆拍**：

- `auto_beats`：纯标点切分、零语义改写，authored beats 恒优先；
- 板与时间轴取同一份 `effective_beats`；
- 自动拆拍的时间轴**替代**散文正文，防同句发两遍；
- 只有零运动设计的镜才跳过点名；`clear` 也不动 beats。

## 3. 板绝不进 `image`/`clip`

compose 会把 `clip` 当成片播。板落 `<章节>_work/sketch/`，不走审阅状态机与版本栈——`--force` 直接覆盖。

## 4. 附板必带防泄漏，没附绝不声明

防泄漏**两处同时说**，缺一处压不住（实测：16 镜里 6 镜带板，2 镜把红蓝标注箭头
画进了开头几帧）：

1. **正文头部**——`board_role_clause()` 紧跟画面基准句：「板不是画面参考、只是分镜
   脚本，格线与箭头是标注符号而非画面元素」。位置即效力：这句挂在时间轴尾巴上
   就落在千余字提示词的 55% 处，模型读到「参考图」时早已过了这句；
2. **负面串**——`prompts.BOARD_FLOOR_*` 把同一批词补进「避免出现：」
   （标注箭头/红蓝箭头/绿色取景框/橙色标注箭头/紫色波浪线/分镜格线/多格分格画面/铅笔素描/草图线稿质感/手写标注文字）。
   国产视频模型对这一串的服从度显著高于正文中段的陈述句。

两处都由 `sketch_board` 旗标驱动，而该旗标必须与请求里真的带了板**逐字一致**
（provider 无 `supports_reference_images` 时板不发、时间轴照发）。
**绝不手工改提示词绕过引擎拼装。**

### 4.1 附板的两条合法通道：dubbed 参考媒体 · native 全能参考

官方拒绝 first/last frame 与 reference media 混发（abyss 镜1 实测 400
InvalidParameter）——附板永远走参考类任务，绝不混进首帧任务：

- **dubbed**（参考媒体模式，ref_audio 在场）：板+设定图追加 role=reference_image，照旧；
- **native 缺省档 = 全能参考（参考生视频任务，`reference_only=True`）**：凡不参与
  首尾帧衔接的镜，分镜图/板（在盘即附）/角色·场景·道具设定图（`_video_sheet_refs`，
  引擎侧参考图上限 7 张，板与尾帧真附时各让一席，角色优先）全挂 reference_image、**不发首/末帧**、一镜一片，判据在
  `cli._shot_plan`（native × 非衔接参与 × `supports_reference_images`）；
- **衔接章（`frame_chain: true`）里**衔接参与镜走首帧任务、板只当拍表；逐镜显式
  `sketch.reference`（`kinema sketch ref --shots 3,14 --state on`）才强制该镜切回
  参考孤岛——静态判据唯一真源 `sketchboard.reference_shot`（`sketch.reference` ×
  native × guide=sketch × 板在盘），该镜既不收也不发末帧，两侧接缝自动补无缝转场；
- provider 不支持参考图：退回纯首帧生成，时间轴纯文本照发；
- `seedance.generate` 首帧分支收到 `ref_images` 直接抛错防再犯。

静默丢图 = 提示词声明「所附分镜板」指向不存在的参考，比 400 更坏。

## 5. 秒段与请求秒数同源

板与时间轴的秒段一律按 `voicecast.request_seconds` 铺——`total` 参数贯穿 `board_prompt` /
`timeline_text` / `sketch_total`，两处 `video_prompt` 调用源级钉死。

`dur` 在 kenburns 折着停顿、dubbed 按配音实测；裸用 `dur` 就是给 Seedance 一份对不上片长的假节奏脚本。

板的面板恒带秒标（`_beat_line`）；进视频提示词的那一份按 `VideoProvider.timeline_unit`
分两支——响应秒时间戳的型号发「第0-3秒：…」，其余发不带时间的「第1段：…」。两支的段头都
不用镜号：「镜头 N」是 `variation.MULTISHOT_RE` 判为多镜的写法，而一镜一次调用只取回一段素材。

### 5.1 覆盖体检与漂移判据

- authored `t` 有 `beats_coverage` 覆盖体检（断档 / 重叠 / 收不到位，只告警不改写）；
- `gen.sketch` 记 `seconds` ＋ `dur_at` ＋ **拍序列指纹 `sig`（`beats_sig`）**；
- 板生成后 `dur` 一变报 `stale`（同量纲对拍，绝不拿 `seconds` 比 `dur`）；
- beats / 提示词一变报 `stale_beats`「⚠ 拍序列已变」。

实测坑：改了 `video_prompt` 板仍显「新鲜」，gen-video 照附旧节奏板。

**漂移判据唯一真源 `sketchboard.board_drift`** —— scanner / `sketch list` / gen-video 告警三处
消费同一份。

### 5.2 `effective_beats(shot, total)` 的 total 必须一路传到底

拍数随 `total` 变，漏传一处就是「板按 4 拍画、时间轴按 6 拍编」。源级守卫扫
sketchboard / cli / scanner / prompts 四文件，禁裸调用。

`beats_sig` 与 `board_drift` 按 `gen.sketch.seconds` 同基准对拍，免得把「时长变了」误报成
「拍内容变了」——那条归 `dur_at` 报。

## 6. 规划优先：先秒级描述，再画板

- authored beats **无板也生效**：gen-video 注入分段时间轴，`sketch list` 显「sketch·纯时间轴」、
  dry-run 标「分段时间轴(无板)」；
- 板从同一份 beats 生成，不是每镜都要出板；
- 无 beats 无板的镜，引擎不注入时间轴，`video_prompt` 必须自带先后次序的详细分段（协议在
  kinema-sketchboard「铁律〇」）。

## 7. beats 的正式写入通道与遮蔽告警

- beats 进了 ChapterPlan 白名单（`shot_fields.sketch.beats`，`beat_list` 类型）：Agent 经
  `agent plan validate/apply` 提交可获得 `expected_revision` 并发保护；`sketch` 是 merge
  语义——只覆写提交的子键，engine 管的 `sheet` 原地保留，`sheet` 本身在白名单外（提交即拒）。
  直接编辑章节 JSON 仍合法（authoring 字段），但长任务并发场景优先走 gateway。
- **遮蔽不静默**：板/beats 在盘、缺省仲裁却落 previz（`previz`/`last_frame_ref` 在场且无显式
  `guide`）时，gen-video 逐镜点名「时间轴与板都不参与本次生成」，lint 同步报 `sketch_shadowed`
  （warn）；显式 `guide: previz` 表态不喊。
- 节奏底线的机器面：lint 的 `beat_static_open`（首拍静止开场）与 `beat_repeat`（相邻拍动作
  逐字重复）两条 warn——「连贯不僵硬」的完整拆拍工法仍在 kinema-sketchboard skill，引擎只钉
  字面可判的底线。

## 7. 拍数按时长配，不是固定 9 格

**立论**：abyss 镜1 实测 9 拍铺 5s（0.56s/拍），Seedance 只挑 2–3 个主事件演完整，其余拍整段丢弃
（转夹具 / 报纸颤 / 影子移 / 直身搁枪全没出现）。**这不是引擎发丢了，而是模型的可执行密度上限。**

分两档处理，分工是全部要点：

### 7.1 自动拆拍 = 引擎代切的确定性行为

`auto_beat_cap` 按 `min(PANEL_MAX, 秒数 ÷ TARGET_BEAT_SEC=1.2)` 收敛，再向下取到最近的整齐拍数
`TIDY_PANELS`。

- 下限 `AUTO_BEATS_MIN=2`；
- `_merge_evenly` 均匀并拍，保时序不丢句；
- 实际取值：5s/7s → 4 拍，8s → 6 拍，10s → 8 拍；
- 拿不到时长才退回 `PANEL_MAX`；素材不足绝不凭空造拍。

### 7.2 版式 `grid_of(n)` 按拍数选恰好填满的网格

| 拍数 | 网格 |
|---|---|
| 4 | 2×2 |
| 6 | 2×3 |
| 8 | 2×4 |
| 9 | 3×3 |
| 10 | 2×5 |
| 12 | 3×4 |
| 质数 5/7/11 | 退回近方网格，并明说「末行 N 格居中」，外加空白格禁令 |

列数一旦硬编码成 3，4 拍就算出「2×3 网格共 4 个面板」自相矛盾，模型只能自己猜——**实测同为 4 拍，
一张排成 2×2 满格、另一张照 2×3 画完留下两个空白框**。

`TIDY_PANELS` 由 `grid_of` 派生，绝不另手写一份。

### 7.3 authored beats = 创作资产，一个字不动

只由 `beats_density`（`MIN_BEAT_SEC=0.8`）在 `sketch gen` / `sketch list` / gen-video 三处报
「⚠ 拍密度」——哪几拍该合是导演决定。

自动拆拍已收敛到下限仍不达标时，改口径说「只能加长镜头秒数」——**告警必须给得出可行动项**。

## 7.5 kenburns 下板不参与出片，但照样计费

板与拍序列的唯一去处是 gen-video 请求，而 kenburns 不发（compose 直接走
`pipeline/kenburns.py`）。这一档下：

- `sketch gen` **只告警不拦**——「先排戏、再切 native」是正当顺序，替用户改主意才是
  越界；告警点破「按分镜图同价计费却不参与成片」并给出可行动项（改 motion）；
- Studio 章节页把 3D 导演台与简笔分镜两台收成一条折叠条（判据 `uses_video`，
  见 [`studio-frontend.md`](studio-frontend.md) §9.0），分镜卡的 ▦ 简笔角标仍在。

## 8. 板生成刻意无画风前缀、无 moodboard 垫图

板是素描基调、不掺成片画风；彩色垫图 =「给板上成片色」邀请函。

版式一致性靠内置版式蓝图 `assets/blueprints/sketch1/2/3_template.png` 自动垫——
三张同版式样板（单人动作/双人对话/双人战斗），与 `board_prompt` 同一套契约
（只学版式与标注画法，绝不复制其人物与标题文字）。
