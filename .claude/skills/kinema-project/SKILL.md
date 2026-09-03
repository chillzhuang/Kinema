---
name: kinema-project
description: "管理多集或系列内容的项目、章节、角色、道具、场景与总体设计。用户要先规划再制作、创建系列、继续既有章节或查看项目状态时使用。"
metadata:
  kinema-managed-by: "agent/manifest.json"
  kinema-kind: "project"
  kinema-status: "stable"
  kinema-version: "2.0.0"
  kinema-owner: "Kinema"
  kinema-source: "workspace"
  kinema-trust: "first-party"
  kinema-digest: "sha256:242c0e45ba7102883f77d560fca0d3336a645da63f51934fef6372f2f244327a"
---
# kinema-project · 项目化管理与强规划

把"零散的一条条视频"升级为"**项目(系列) → 章节(视频) → 角色/总体设计**"的强规划结构。
每个实体是一个 JSON 文件（工作区 = 轻量数据库），可 CRUD、可随时进入某项目继续开发，
可用 studio 大屏查看全貌。适合系列剧、多集故事、IP 化内容、需要角色一致与世界观的题材。

## 强约定（务必遵守）

**所有生成产物一律落在仓库根项目库 `project/<项目id>/` 内；未指定项目时默认用
`project/demo` 工程。绝不把文件写到 `/tmp`、仓库根本身、`engine/project/` 或其他随意
位置污染文件系统。** 工作区数据根默认是仓库根 `project/`（可用 `--workspace` /
`KINEMA_WORKSPACE` 覆盖；传仓库根或 `engine/` 时引擎归一到仓库根 `project/`）。local
与 mysql 只切换持久化后端，不能改变工作区路径。

## 结构

```
project/<项目id>/          项目库；默认工程为 project/demo
├── project.json           系列元数据：总体设计 + 角色(含服装/发型/武器) + 道具 + 固定场景 + 章节索引
├── assets/refs/           ★设定集：角色设定图(三区两视) / 场景设定图 / 道具·武器设定图
├── assets/voices/         音色锚定参考音频（如用）
├── chapters/chXX.json     章节=一条视频(继承项目 profile/音色/设定集，待填 script/shots)
└── chapters/chXX_work/    渲染产物（图/配音/字幕/音乐/成片）
```

**关键：章节自动继承项目** —— 新建章节会带上项目的 profile、角色音色表、角色设定块、色板、seed，
**以及设定集（角色/场景/道具设定图路径）**，所以全系列画风/角色/场景/声音天然一致，不用每集重填。

**★设定集（强要求·一致性根基）**：`character add --outfit/--hair/--weapon` + `prop add` + `scene add`（取景地）+
`project set --scene`（全局固定场景）
后跑 **`project refs <项目>`** 生成角色/场景/道具设定图；之后每镜**强制参考**这些图，跨镜跨集统一。
只有用户明说"跳过设定集"才 `project set --skip-design`（退回首镜锚定）。详见 base skill `kinema` 铁律2。

## 工作流

### 1. 创建项目 + 总体设计
```bash
cd engine
python3 -m kinema project new --title "旅人与灯塔" --id lanterns --profile hd2d --theme "寻找与归途"
python3 -m kinema project set lanterns --logline "..." --world "..." --tone "..." --palette "..."
```
先想清楚：一句话故事、世界观、基调、色板、目标平台、用哪个风格档（profile）。

**目标平台明确时优先用平台规格模板**（`--template`，风格/比例/渲染模式/单集规格一键落位，
之后 `spec check` 即交付验收线；模板在 `config/templates.yaml` 可增删改）：
```bash
python3 -m kinema template list                                          # 抖音漫剧/快手星芒/B站中视频…
python3 -m kinema project new --title "旅人与灯塔" --id lanterns --template douyin_manju --theme "寻找与归途"
```

### 2. 角色 / 道具 / 场景设定（**纯文字设定**是一致性的真源）

设定图只是文字设定的渲染结果——**文字写不细，图就抽卡；文字不更新，第 30 章的他还穿着
第 1 章的衣服**。所以设定分两步：`add` 建档、`set` **随剧情实时更新**。

```bash
python3 -m kinema character add lanterns --name 洛 --voice-prompt "十六岁少年，清亮，语速快" --role 主角 --appearance "蓝斗篷棕发年轻剑士"
python3 -m kinema character set lanterns --name 洛 --speech-style "短句，从不解释第二遍" \
    --personality "谨慎，先算退路" --arc "求生→求真" --taboo "绝不先动手" --add-keyword "小疤"
python3 -m kinema character show lanterns --name 洛      # 文字设定卡（省 token 的读法）
python3 -m kinema prop  set lanterns --name 断剑 --desc "剑身齐中断裂，缠了布条"
python3 -m kinema scene set lanterns --name 钟楼 --add-keyword 钟塔
```

**角色的文字设定字段全表**（都是「你填」，engine 只读不改）：

| 字段 | CLI | 干什么用 | 落到哪 |
|---|---|---|---|
| `appearance`/`outfit`/`hair`/`weapon` | `--appearance/--outfit/--hair/--weapon` | 长相与装束 | 角色设定图提示词（三区两视铁律） |
| `role` | `--role` | 定位（主角/反派/NPC） | 规划与人审 |
| `voice_prompt` | `--voice-prompt` | 声线描述（年龄段/性别/音区/语速/气质，30 字内，不写情绪词）；`add` 建档即定制立档，`set` 重写即重新定制（旧声烧过的 native 片段置 retake） | 全系列配音与 native 音色锚定；`voice` 是引擎回填的档案引用（显式模版 `--voice <别名>` 直填、不建档案） |
| `keywords` | `--keyword/--add-keyword` | 别名/绰号/尊称 | 正文实体命中统计（不登记就统计不到，会被误报「很久没出场」） |
| `speech_style` | `--speech-style` | **怎么说话**（句长/口头禅/称呼） | 写台词与写小说正文的**人设门盲测判据** |
| `personality` | `--personality` | 性格内核（压力下怎么选） | 人设门「像不像他」 |
| `arc` | `--arc` | 人物弧光（起点→当前→终点） | **转变发生后立刻更新**，否则 AI 按第 1 章的他写第 50 章 |
| `taboo_lines` | `--taboo/--add-taboo` | 绝不说/绝不做的清单 | 人设门**硬判据**，命中即打回（管文字行为） |
| `silhouette_notes` | `--silhouette` | 剪影辨识度要点 | 进角色设定图提示词（跨镜一致性强锚点） |
| `constraints` | `--constraint/--add-constraint` | 画面硬禁忌 | 编译进 `negative_prompt`，**永不进设定图**（会顶撞三区两视铁律） |
| `required_emotions/actions/views` | `--emotion/--action/--view` | 全系列要演到的情绪/动作/视角 | 设定图取景与一致性判据 |

道具 `desc`/`kind`/`keywords`、场景 `desc`/`keywords` 同理（`prop set`/`scene set`）。
列表字段统一口径：**`--x` 整体替换 · `--add-x` 并集追加 · 都不给=不动这个字段**。

**三条纪律**：
- **绝不手改 `project.json` 里的 `characters[]`/`props[]`/`scenes[]`**——`set` 系命令走
  `Series.commit()`（进程锁 + 进锁后重载磁盘）；手改会被引擎长任务的旧内存副本整份覆写、
  mysql 模式下还会被较新的库行在加载前盖掉，**且不报任何错**。
- **`sheet`/`ref_image`/`audition` 这些引擎回填字段不在 `set` 白名单里**（会报错）——换图
  走 `project refs --force` / `refine` / 版本回滚那套版本栈，换音色走 `character set --voice-prompt`
  （重新定制）或 `voice use`（换档案/模版）。
- **章节继承是创建时拷贝**：改了系列级设定要让**已建章节**看见，加 `--sync`（或下次
  `project refs` 收尾自动同步）。`required_*`/`silhouette_notes`/`constraints` 与文字人设
  四件都是**系列级常量**（写全系列的事，不是本集的），按集填会被下次同步冲掉。

角色音色与**旁白**缺省定制：角色 `character add --voice-prompt "<声线描述>"` 建档即立档，
旁白 `voice custom <项目> --narrator --prompt "<声线描述>" --adopt 1`；真发前 `tts`/`gen-video`
逐个点名未选角的说话人。试音只属于模版路（显式例外）：`voice audition <项目> --name 角色 / --narrator`
→ 试听后 `voice use ... --no 编号`（Studio 项目页选角卡点「用这条」）。
选过的每一把留一条音色档案：`voice bank` 看谱系、`voice use --cast vc_NNNN` 换回。
设定图同理可多候选：`project refs <项目> --candidates 3` → 项目页宫格点选定稿。

上面任一“Studio 项目页点选”动作，先执行 base skill 的 [Studio 交互交接协议](../kinema/references/studio-handoff.md)，
自动确认控制台已运行并把项目/章节入口交给用户；不要只给一条启动命令后就停下。

> **要一章章写原创小说**（不是改编既有全本）→ 走 `kinema-novel`：卷纲 `novel arc`、
> 每章五步闭环、十章一批次的六门复核与《批次报告》都在那本 skill 里。

### 3. 建章节 → 填脚本 → 渲染
```bash
python3 -m kinema chapter new lanterns --title "相遇"          # 生成 ch01.json（已继承）
# 你（Skill）把该章节当一条视频来做：填 script + shots（各镜 speaker 用角色名）
python3 -m kinema run --chapter lanterns/ch01 --mock            # 先离线看
python3 -m kinema run --chapter lanterns/ch01 --native          # 真实图生视频（缺省全能参考·一镜一片）
```
每个章节就是一条视频，用对应 genre skill（如 kn-game）的分镜方法来写 script/shots。

### 3.5 剧本改编模式（可选上游：整本小说/剧本 → 分集 → 自动建章）

**何时用**：用户提供了**完整小说/剧本**，要按原著忠实改编成多集漫剧（IP 改编）。
不用则走上面「建章节 → 填脚本」的原创路径。

**Python + AI 两段式**：
- **Python 半（引擎 `adapt` 承接 · 零 LLM · 确定性）**：正文落盘、结构预切分、幂等建章、实体合并。
- **AI 半（你 = Claude 指挥层）**：拆书 / 分集 / 抽实体 / 拆镜——**智能全在你这**，引擎永不内置抽取器（铁律「引擎内无 LLM provider」）。

**七步工法（每步落盘 · 逐门停下确认）**：

> **贯穿全程的一条：文字先行 · 图片留空**（kinema 铁律9）。改编是**整本书**的量级，
> 设定图十几到几十张、分镜图动辄几百镜——**第 1~6 步一张图都不生成**，全部产出都是文字
> （设定单文字字段 / `episodes[]` / `outline` / `shots[]` 含提示词）。第 7 步出《出图就绪表》
> 交给用户，**用户下令才开始烧图**。`--mock` 同样别跑（占位图会填满 `sheet`/`image`，
> 之后分不清哪些是真出过的）。


1. **入库**（两条路径任选，都落 `source/raw.txt` + `source/segments.json`（剧本按场景 / 小说按章标切分）+ `source` 指针块）：
   - **本地文件**（你直接跑）：`adapt import <项目> --file 小说.txt [--kind auto|novel|screenplay]`——用户把小说/剧本文件路径给你即可。
     支持 `.txt`/`.epub`/`.fountain`/`.fdx`：**EPUB** 纯 stdlib 解析（zipfile+xml.etree+html.parser 抽正文，
     章标题取自 EPUB 自带 nav/NCX 目录，比 txt 章标正则更可靠；加密含 DRM 的 EPUB 拒收，MOBI/AZW3 需先用 Calibre 转 EPUB/TXT）
     。
   - **网页上传**（用户自助）：先按 base skill 的 [Studio 交互交接协议](../kinema/references/studio-handoff.md) 启动控制台并给出项目入口，
     再提醒用户到 **Studio 项目页「改编」区点「⬆ 上传小说/剧本入库」**；上传后引擎自动结构切分，用户回来说「已上传」
     后，你 `adapt show <项目>` 读回 source/切分数据再继续。
   > 建改编项目时**主动问一句**「有完整小说/剧本要导入吗？给我文件路径，或到网页改编区上传」；入库后读 `source/raw.txt` + `source/segments.json` 再进第 2 步。
   > （**编码自动识别转 UTF-8**：UTF-8 / GBK-GB18030 / **Big5-繁体** / 带或不带 BOM 的 UTF-16 都按「中文多、
   > 乱码少」打分择优自动转码落盘，无需用户手动转码。**乱码闸兜底**：只有加密/二进制/未知编码——自动转码后仍有 >25% 乱码或控制符——才**拒收报错**提示「确认是纯文本或另存为 UTF-8」
   > ，正常中文一律 0 不误伤。）
   > **剧本工作台阅读器**（Studio 剧本页）：左章节目录树 + 右正文**按段懒加载**（点哪章载哪章，超长书不撑爆，
   > 无 200 万字上限），配 ⧉ 拆书指令 / ⧉ 问书指令 / 划词抽实体 / ⇄ 集级对照——都走「指令台：写下需求→与带定位坐标的标准指令合并→复制给 Claude Code」
   > 范式（引擎无 LLM）。
2. **拆书**（你）：分窗 `Read` `source/raw.txt`（借 `segments.json` 的章 / 场锚点定位原文段），提炼主线 / 贯穿冲突 / 爽点 / 世界观宪法，**直接编辑 `project.json` 写 `adaptation` 块**。
3. **抽设定**（你）：抽角色 / 道具 / 场景 → `character add` / `prop add --keyword` / **`scene add --keyword`**（把同义措辞尽量塞进 keywords 兜底命中）
   。
   **⚠ 分级铁律：一个「地方」一律走 `scene add`，绝不能登记成道具。**道具设定图是**结构三视物件版式**（上部约 2/3 为正/侧/背三视等高等大并列，下部约 1/3 为一排三个局部细节框，无色板，纯色浅灰底）
   ，对「一个地方」根本不成立——「孢子宫殿」会被画成蘑菇战锤、「原始孢子森林」画成防毒面具与齿轮球；
   `scene add` 走环境 key art 版式（广角建立镜头／无人物／前中远三层景物）且比例跟随项目而非 1:1。
   判据：**能被人拿起来/穿戴/挥动的是道具，人走进去的是场景**（塔、大厅、塔顶、街道、森林、商场、
   走廊、直播间…全是场景）。
   场景描述要写成「一个地方」——**别在 desc 里写主体名词**（人／人形／某某人），模型会抓着那个名词去造物件（「几只孢子人在车缝间游荡」
   → 直接渲出一个孢子人形）。
   **疑似同一实体（别名 / 重名）一律停下问用户、不自动合并**（错挂 > 漏挂）；**道具只登记「被角色物理交互且跨镜复现」
   的**，背景陈设不登记（省设定图成本）。
4. **分集**（你）：**按原著章节一一对应：一章 = 一集 = 一个视频章节，绝不合并章节**（小说有多少章，
   `episodes[]` 就有多少集——多章压一集会把该章的关键情节、微反应与设定细节整段挤掉，改编质量塌在源头；
   引擎在 `adapt scaffold` 时对「分集数 ≠ 源章节数」出声告警）。集号与原著章号一致、`source_range` 写「第N章」
   ；每集从**本章正文内**取材写「开场 3 秒钩子 + 核心事件 + 尾钩（留白不给答案 = 付费位）」，**直接编辑 `project.json` 写 `episodes[]`**（字段 `no/title/logline/open_hook/core_event/cool_point/end_hook/source_range/target_dur_s`）
   。单章体量撑不起一集时**问用户**而不是擅自合并（可加长该集时长或让用户点名例外）。→ **停下确认分集方案**。
   随时 `adapt show <项目>` 核对 source / 拆书 / 分集。
5. **建章**（Python · 幂等）：`adapt scaffold <项目> [--only 1,3,5]`
   → 章号 == 集号（`ch01`…）、把每集大纲编译进章节 `outline`、回填 `episodes[].chapter_id`、回灌设定集。**可重跑不炸**（改了 `episodes` 重跑即刷新 outline）
   。也可在 Studio 项目页「改编」区点「建本集 / 全部建集」。
6. **拆镜**（你）：逐章照 `chapter.outline` 拆 `shots[]`（用对应画风 skill 的分镜方法）。
   `image_prompt`/`image_prompt_en` **照样写全写好**——它们是文字资产，写进 JSON 零成本；
   但**到此为止，`gen-image` 不跑**（`shots[].image` 空着是本档正常状态）。
7. **出图就绪表**（你）→ **停下等指令**：文字全部落盘后，Read `project.json` 与各章节 json
   **逐项数**（别估）出这张表交给用户，然后停：

   | 批次 | 待出 | 依据字段 | 建议顺序 |
   |---|---|---|---|
   | 角色设定图 | N 张 | `characters[].sheet` 为空 | ① 先出，一致性根基 |
   | 场景设定图 | K 张 | `scenes[].sheet` 为空 + 顶层 `scene_ref` | ① 与角色同批 |
   | 道具设定图 | M 张 | `props[].sheet` 为空 | ② 按出场频次挑，冷门道具可缓 |
   | 分镜图 | 逐章列 | `shots[].image` 为空 | ③ 设定图全部通过后，**先首镜** |

   乘 `config/models.yaml` 里该 profile 的 `price_per_image` 报出总价量级。
   **只有用户明确下令**（"出图"/"生成设定图"/"先把主角出了"）**才动手**；没点范围时
   **默认只出设定图那一批**，绝不顺手把几百镜分镜图一起烧掉。

**铁律**：
- **文字先行·图片留空**：拆书/抽设定/分集/拆镜四步的交付物全是文字，**图一张都不出**；
  收口是《出图就绪表》+ 停下等指令，不是"顺手把设定图跑了给用户看看"。整本书的量级下
  「先跑了再说」和「先算清楚再问」差的是几百块钱和一次不可撤销的抽卡。
- **台词导演视角具象化**：「她很生气」→「眉头紧锁、拳头攥紧」；台词即字幕（音字一致），单句 ≤15 字、杜绝水台词。
- **每集拆完人审**：自动抽取有天花板（阅文 ~80% / 影视 ~86%），默认停在设定单与分集方案让用户核对，不做全自动黑盒。
- **集尾钩子写在末镜**：末镜 narration 承载 cliffhanger。别顺手插一个字卡转场去托它——转场只由用户主动插。
- **重抽实体禁止整体覆写**：二次拆书要更新角色 / 道具时走 `adapt merge-entities <项目> --file 候选.json`（合并不覆盖 ·
  保人工 voice/keywords/comments · keywords 取并集），或先读现有实体再增量改；**绝不整体重写 `characters[]`/`props[]`**（会抹掉用户手调）
  。
- **`outline` 是 `episodes[]` 的派生缓存**：要改某集大纲，改 `episodes[]` 再 `adapt scaffold` 重刷，
  不要手改章节 `outline`（会被下次 scaffold 覆盖）。

### 3.6 关系图谱（人物关系 + 世界观可视化 · 系列文档）

**何时用**：拆书 / 分集后（或用户在 Studio 剧本工作台「图谱」Tab 点「⧉ 图谱指令」时），把散在原文里的人物关系与世界观法则**结构化成节点 + 连线**，
供导演一眼看清全局、也作跨集一致性宪法。与 `adaptation`/`episodes` 平级，**引擎不消费**，纯规划与可视化。

**工法（你 = Claude）**：`Read` `source/raw.txt`（借 `segments.json` 定位），梳理主要角色 / 阵营 / 地点 / 世界观法则及其关联，
产出图谱 JSON 后 `adapt graph <项目> --file 图谱.json` 落库 → 用户在剧本工作台「图谱」Tab 看可视化关系网 + 下方核心知识点缩写。

**图谱 JSON 契约**（与 `docs/kinema/project.schema.json` 的 `graph` 块对齐）：
```json
{
  "summary": "少年林深自青云宗崛起，与魔渊墨渊结下贯穿全书的正邪之仇。",
  "nodes": [
    {"id": "linshen", "name": "林深", "type": "character", "role": "主角", "faction": "青云宗", "desc": "天才少年剑修"},
    {"id": "qingyun", "name": "青云宗", "type": "faction", "desc": "正道领袖门派"},
    {"id": "tiandao", "name": "天道轮回", "type": "worldview", "desc": "支配修真界的根本法则"}
  ],
  "edges": [
    {"source": "weiran", "target": "linshen", "relation": "师徒", "kind": "mentor", "directed": true},
    {"source": "linshen", "target": "moyuan", "relation": "宿敌", "kind": "hostile"}
  ]
}
```
- **节点 `type` 五类**：`character`（角色）/ `faction`（阵营）/ `location`（地点）/ `item`（器物）
  / `worldview`（世界观法则）——驱动前端配色与图例。
- **边 `kind` 八类**：`kin` 亲缘 / `ally` 盟友 / `mentor` 师承 / `hostile` 敌对 / `love` 情感 / `member` 归属 / `rival` 竞争 / `neutral` 关联（缺省）
  ——驱动连线配色；`relation` 是自由文案标签，`directed:true` 画箭头（如师父→徒弟）。
- **节点 `name` 命中已建设定图的角色/道具时**，前端**自动挂上该设定图缩略图**，点节点即开富灯箱（可重生成 / 点评，
  与 ch01 资产同制度）——所以图谱里角色 `name` 尽量与设定集 `characters[].name` 一致。

**铁律**：
- **整体替换**（非合并）：`adapt graph` 每次用完整快照覆盖 `graph` 块（图谱无人工子字段，重新分析产出整份才自洽）
  。**边端点必须指向已存在节点 id**——引擎校验悬空边直接拒收。
- **只落 series 文档**：图谱是跨章宪法，**绝不进章节文档或 `characters[]` 逐条**（关系是全局视图，不是角色属性）。

### 3.7 参考片立项模式（study · 读片定节奏，不抄内容）

**何时用**：用户说「我要做成 XX 那样的」并给了一支**本地参考片**（自己下载的对标视频、竞品、往期作品）。
读片的目的是**量出节奏骨架**（多久一刀、每镜多长、留白多少）来定我们这条片子的**镜数、镜长与 motion 模式**，
不是照着它的画面复刻。不给参考片则跳过——原创路径本来就完整。

**Python + AI 两段式**（与 3.5 剧本改编同范式）：
- **Python 半（引擎 `study` 承接 · 零 LLM · 确定性）**：拷进工作区 → ffmpeg 量切点/静音 → 等间隔抽帧 → 落 `digest.json`。
- **AI 半（你 = Claude 指挥层）**：看数看帧 → **判定** motion 模式与镜数 → 出「保留 / 必改」两栏 → 写分镜。
  引擎**只交数不交结论**（铁律「引擎内无 LLM provider」）。
  **动手读片前先 Read `references/shot-analysis.md`**——读片顺序、切点复核（引擎的
  已知误报/漏报源）、逐镜记录口径与证据纪律（只记帧里看得见的、声音侧只用静音区间
  与外挂字幕、疑似/无法判定原样进结论）都在那份手册里；本节只管判定规则与铁律。

```bash
python3 -m kinema study import <项目> --file 参考片.mp4 [--cuts 0.3] [--frames 24] [--subs 外挂.srt] [--title 备注名]
python3 -m kinema study show <项目>                       # 回看全部读片记录
python3 -m kinema study rm <项目> --slug <slug>           # 读完即删（版权卫生）
```
产物落 `project/<项目>/study/<slug>/`：`ref.mp4`（副本）+ `digest.json`（切点全表 / 逐镜清单 / 静音区间 / 抽帧索引）+
`frames/`（等间隔关键帧，上限 48 张）+ 可选 `subs.srt`。
`project.json` 的 `study[]` **只留指针 + 计数**，要看全表自己 `Read` digest.json。
**v1 只吃本地文件，不吃 URL、不下载**——引擎核心不联网抓第三方内容；用户要读某个链接的片子，请他自己下载后给本地路径。

**引擎交给你的可测量量**（`rhythm` 块）：

| 量 | 含义 | 你拿它干什么 |
|---|---|---|
| `cuts_per_min` | 切点密度（刀/分钟） | 换算我们这条片子的**镜数** = 密度 × 目标时长 |
| `avg_shot_sec` / `min` / `max` | 每镜平均/最短/最长时长 | 定每镜 `dur`；`max` 特别长 = 对方有长镜头做呼吸 |
| `silence_ratio` | 静音占比（`null`=无音轨，`0`=全程有声） | 高 = 靠画面与配乐叙事（少写旁白）；低 = 旁白密集口播型 |
| `media.dur/fps/width/height` | 容器元数据 | 核对比例与目标时长量级 |

**判定规则（这一步归你，引擎不做）**——由节奏骨架反推 motion 模式：

| 参考片节奏 | 判定 | 理由 |
|---|---|---|
| `avg_shot_sec ≤ 2` 或 `cuts_per_min ≥ 30` | **kenburns** | 快切蒙太奇每镜一闪而过，静图 + 运镜完全撑得住；这种节奏上 Seedance 是纯烧钱（镜多 × 每镜都要钱） |
| `2 < avg_shot_sec ≤ 5` 且以**角色说话**为主（`silence_ratio` 低、有对白字幕） | **dubbed** | 镜长足够看清口型，对口型的收益最大化 |
| `avg_shot_sec > 5` 的长镜 / 环境镜 / `silence_ratio` 高 | **native** 或 **kenburns + 环境特效** | 长镜要真运动才不呆；预算紧就用 kenburns 慢推 + rain/snow/fog 环境特效兜住 |

先按上表出结论，再**用镜数 × 单价当场算一笔账**（单价取 `models.yaml` 里该视频别名的 `price_per_second`，缺省主力档 0.5 元/秒）报给用户；
真跑前照常 `gen-video --dry-run` 复核。**结论必须写成一句人话交给用户确认**，别默默按参考片抄。

**反照抄铁律：读片产出必须列「保留什么 / 必须改什么」两栏**，缺一栏不许进节点①。

| 保留（可借鉴的**结构**） | 必须改（不许沿用的**内容**） |
|---|---|
| 节奏骨架：镜数、每镜时长、切点密度、留白位置 | 具体画面构图与镜头内容——一律按本项目世界观/角色重写 |
| 信息结构：开场钩子在第几秒、转折点位置、结尾收束方式 | 台词原文、旁白措辞、金句——一个字都不许抄 |
| 声音节奏：旁白密度、静音留白比例、配乐进出点 | 音乐曲目、音效素材本体 |
| 情绪曲线：哪儿紧、哪儿松、高光落在哪 | 角色设定、造型、名字、专有名词 |

**版权铁律（硬约束）**：
- **参考片只是尺子，不是素材**。它**绝不进任何生成请求**——不做垫图（那是 `project moodboard`）、不做首帧、不进 `shots[].refs`。
- **绝不进交付目录**：`exports/` 交付包与提案里不许出现参考片、其抽帧或字幕；`deliver`/`export-*` 只收我们自己的产物。
- **绝不上云**：`study[]` 里的路径一律工作区相对路径，`oss sync` 因此不会收录它（引擎已有守卫用例钉死）。任何时候都别手改成绝对路径。
- **读完即删**：结论写进分镜后跑 `study rm <项目> --slug <slug>` 清掉本地副本，`digest.json` 里的数要留就先自己拷走。

### 4. CRUD 查看 / 管理
```bash
python3 -m kinema project list            # 所有项目
python3 -m kinema project show lanterns    # 项目详情(设计/角色/章节及状态)
python3 -m kinema chapter list lanterns    # 章节及渲染状态(draft/scripted/rendered)
python3 -m kinema chapter set lanterns ch01 --skill kn-anime --profile ghibli   # 只改章节绑定（建章拷贝）；--inherit 回落项目派生
python3 -m kinema spec check lanterns      # 绑定模板后：逐章时长/分镜数/比例 + 系列体量达标核对
python3 -m kinema ledger lanterns          # 成本台账：预估/实际双轨 + 废片/重roll 运营指标
python3 -m kinema export-pitch lanterns    # 项目提案书（浏览器打印即 PDF）
python3 -m kinema project rm lanterns --archive   # 归档
```

`project rm` 是逻辑删除：项目进入 Studio 回收站，目录与产物保留用于恢复。项目库根下仍能
看到该目录不代表章节跑到了外层；章节只允许位于 `project/<项目id>/chapters/`。

### 5. 大屏可视化
```bash
python3 -m kinema studio                         # 从 engine/ 启动也自动使用仓库根 project/
```
需要用户进入大屏时，必须先执行 [Studio 交互交接协议](../kinema/references/studio-handoff.md)；这里只保留命令入口，
不把“启动控制台”的责任推给用户。
"项目"标签页 = 项目仪表盘（每个项目→总体设计/角色预设/章节及成片，绑定模板的项目带**平台规格达标卡**与**导出中心**）；
"看板"标签页 = 跨项目状态全景（五态看板**支持拖拽表态** + 章节热力 + 烧钱/废片/重roll 运营统计）；
"生意"标签页 = 成本台账（预估/实际双轨）；章节页带**时间线视图**、
**血缘画布**（设定资产连线到引用它的分镜，hover 联动、过期红线）与**交付卡**
（一键导出审阅包/交付包）；"成片"标签页 = 视频画廊；`⌘K` 全局素材检索（台词/提示词/角色跨项目定位）；
灯箱内**✂ 框选局部改造**——设定图/分镜图框选一块区域输入指令即可让模型只改这一处。

## 给用户/Claude 的规划要点
- **先项目后章节**：先定总体设计与角色，再逐章推进——这样全系列一致、不乱。
- **角色即音色**：角色名同时是对话框名牌 + 配音音色（一处定义，全系列一致）。
- **进入某项目继续**：随时 `project show <id>` 看到进度，挑 draft 章节继续填脚本。
- 章节脚本/分镜方法：按该项目 profile 对应的 genre skill（kn-game / kn-explainer / …）。
