# Kinema · 架构与视频生成流程

> 本文按数据流描述引擎：一条视频从项目文档到成片经过的每一步，以及每一步的判据、产物、闸
> 与写回。分层与立项取舍见 [`design.md`](./design.md)；工程纪律以 [`AGENTS.md`](../../AGENTS.md)
> 为准，模块地图见 [`DEVELOP.md`](../../DEVELOP.md)。

---

## 一、系统边界

工作被分成两类：需要判断的与可确定性执行的。前者由编码 agent 承担（文案、分镜、提示词、审阅），
后者由本地 Python 引擎承担（生图、配音、图生视频、合成、记账、版本）。两侧只通过 JSON 文档对话，
引擎内不调用 LLM。

- **文档即数据库。** `project/<pid>/project.json` 是系列文档，`chapters/<cid>.json` 是章节文档；
  引擎每一步读写这两份。MySQL 只镜像文档入库，不改变工作区路径。
- **确定性。** 同一份章节文档与配置产生逐字相同的请求体。提示词由 `PromptCompiler` 封成不可变的
  `PromptEnvelope`，dry-run、真发与 `shots[].gen` 留痕共用同一对象。
- **人工关口在主链上。** 缺省流程为文案分镜 → 设定 → 首镜试图 → 全章生图 → 配音 → animatic →
  合成 → 动态化 → 终检，逐步停下审阅；`run` 一条龙仅在用户明确要求全自动时使用。
- **成本前置。** 按秒计费的视频在发送前须 `--dry-run` 审逐镜提示词与报价，发送前过预留额度闸，
  每笔花费先入账再抛错。

---

## 二、三层与真源分工

**指挥层**：`.claude/skills/` 正文承载创作方法，`agent/manifest.json` 与 `agent/contracts.json`
承载元数据与机器契约，`tools/agent_assets.py compile` 编译出各宿主的发现入口与运行时 catalog。
Agent 改章节只有一条入口：`agent context` 取最小上下文与 `revision` → 提交 ChapterPlan →
`plan validate` 纯计算校验 → `plan apply` 在章节锁内 CAS 写入并追加 provenance。

**执行引擎**：`engine/kinema/`。`cli.py` 编排，`pipeline/` 确定性算法，`providers/` 能力适配，
`storage/` 持久化，`studio/` 为只读扫描、HTTP 与写操作三层。Studio 的写操作与 CLI 同走
`Project.load → 领域模块 → Project.save`，长任务派 `python -m kinema …` 子进程，页面不持有
第二套状态机。

**能力层**：`config/models.yaml` 声明 profile → 别名 → 连接段 → 适配器工厂四层解析。换模型改配置，
业务代码不出现厂商名。视频缺省档为 seedance-mini 720p，大模型只在显式点名时使用。

字段归属：创作字段由 agent 写，产物字段由引擎写（schema 中标 `[engine-managed]`），能力参数由配置写。

---

## 三、文档、状态与并发

### 3.1 两份文档

系列文档管设定集（角色、道具、场景及其设定图）、画风快照 `style_prompt(+_en)`、音色档案库、
封面注册表与系列级台账。章节文档管单集全部内容：脚本、分镜、产物路径、审阅状态、版本栈、成本、
血缘留痕。

章节创建时一次性拷贝系列的绑定与设定：profile、skill、平台比例、画风快照、艺术指导与配音表现力
基调、字幕语言、角色音色表与音色档案库、旁白锁、角色道具场景、场景基准图（模板指定的渲染档随之
拷贝）。之后修改系列不回灌已有章节；系列级设定改动经 `character/prop/scene set` 与 `project refs`
的逐章传播对齐。opt-in 行为开关（`native_voiceover`、
`frame_chain`、`anchor_frame`、`tail_relay`、`previz_v2v`、`video_provider`）不继承，每章显式表态。

### 3.2 三套状态

- `shots[].status`：执行态（done / failed / wip），只由生成阶段写。
- `shots[].review.<stage>`：审阅状态机 todo → wip → wfa → done / retake，整镜弃用为 omt。
  `done` 是锁，`--force` 不越过；重做先置 `retake`。版本回滚与宫格换选是人眼定稿动作，
  直接换画布并回到待审。
- 章节状态由盘上产物动态推导，不落盘。

### 3.3 写并发

生成、合成一类长任务持章节文档的内存副本并逐镜 save；同一时段 Studio 会写审阅表态，agent 会改分镜。
协调机制有三：

1. **操作锁**（`locking.op_lock`，非阻塞、同线程可重入）。生成/合成阶段（锁内装载）、改作者字段
   与结构的编辑（Gateway apply、批量改词、转场增删、章节绑定、Studio 的转场/垫图/特效/水印/字幕
   样式/previz 编排）、移动画布的动作（回滚、直供、局改、宫格点选、previz 登记）与系列→章节的逐章
   传播（`Series.chapter_write`）都先取此锁，被占即拒。
2. **表态三方合并**。`Project.save` 只对固定的人类表态键（review、comments、versions、一致性判定、
   音色过期标记等）按加载基线做磁盘优先合并。作者字段不在合并面，靠操作锁串行。
3. **表态 CAS**（`Project.mutate`）。Studio 的审阅、评论端点与 CLI 的表态命令（`review set`、
   `consistency set`、`decision add`、`lineage mark`、`verify` 结论、BGM 闸的表态）在锁内以磁盘
   现状为基线应用变更。

Gateway apply 在操作锁之上再持文档写锁，并以 `expected_revision`（内容摘要）做 CAS；任何改内容
的 save 都使旧计划失效。

### 3.4 失效判定

产物过期有两条判据：

- **字段名边** `review.STAGE_FIELDS`（镜级）与 `review.CHAPTER_STAGE_FIELDS`（章级）：Gateway、
  批量编辑、Studio 章级开关与 `chapter set` 据此拒绝改动已锁定阶段的字段，并把已产出且未锁定
  的阶段置 retake（`review.retake_produced`）。契约白名单的每个镜级字段都在镜级表登记，守卫钉住
  包含关系。批量编辑的 `undo` 同样是一次编辑，按同一规则置 retake。
- **内容指纹边** `lineage`：设定图指纹（`gen.image.refs`）、分镜图指纹（`gen.clip.refs`）、
  台词文本指纹（`text_fp`）。`lineage mark` 扫描后未锁定置 retake、锁定只挂标记。

换画面的六道门（生图、宫格点选、素材直供、局部改造、版本回滚、过期扫描）对存量片段的处置只有
`lineage.retake_clip_for_image` 一处。配音的音色边由 `stage_tts` 判定：盘上 wav 记录的音色与当前
解析出的音色不同即重合成。

---

## 四、视频生成流程

各步均为独立命令，人工节点插在命令之间；`run` 按同一顺序串行执行。

### 4.1 项目与章节

`project new` 建系列文档。`skill` 绑定由显式值或 `skill_for_profile(profile)` 派生，按项目继续时
不重新推断；`snapshot_style_prompt` 把画风前缀快照进 `style_prompt(+_en)`，全片生图只读此处。
角色、道具、场景经 `character/prop/scene set` 登记；`project refs` 出设定图，费用进系列台账。

`chapter new` 拷贝上述绑定，`script` 与 `shots` 留空待指挥层填写。

### 4.2 分镜

Agent 通过 ChapterPlan 写章节，可写面为 `contracts.json` 白名单。章级：主题、脚本、画风快照、语态、
渲染档、音频制式、混烧与衔接开关、字幕样式、语速。镜级：时长、台词（单段 `narration` 或多段
`lines[]`）、说话人、情绪、表现力契约 `delivery`、出场实体、取景与镜头语言、运动描述
（`video_prompt` 或 `action / entry_state / end_state / light_shift` 骨架）、拍表 `sketch.beats`、
锚定与衔接的镜级开关。PromptSpec 是计划期中间表示，投影为上述字段后不再保留。

`lint` 为软闸：多镜语法、代词、空词、语态错配、`dubbed_dialogue` 类制式错配、
`native_voice_unverified` 类待核对项，只告警不拦；`--strict` 有警告即非零退出。

### 4.3 设定与参考

每镜生图请求携带的参考图由 `project.shot_cast` 与 `design_refs_for_provider` 决定：镜级
`characters/props/scenes` 显式绑定且命中设定集的实体才注入；provider 有参考图上限时按
场景 → 角色 → 本镜高频道具取舍，并点名被省略项。参考库垫图缺省全局套用，镜级 `refs` 可覆盖；
垫图集合改动使画布进重做队列。

角色文字锚按镜装配：随请求附了设定图的角色只保留绑定句，无设定图的角色才落全文外貌。

### 4.4 生图 `gen-image`

计划期在主线程完成：逐镜过 `_regen_gate`（弃用跳过、`done` 跳过、`retake` 或 `--force` 重生、
缺图生成），解析 provider（显式激活 > agent 声明 > profile 缺省），拼参考图与文字锚，
`PromptCompiler.image` 封 Envelope。计划全部成功后置 wip；API provider 的重生先落临时名，回填时才归档
旧版并替换画布（生成期间画布与参考图都在盘，失败时画布原地不动），agent 工单路由在开单前归档。

工作线程只产文件：复用在盘的图、捡回上一轮已付费但回填前中断的图、出候选宫格、或发请求。
agent 路由下缺图不发请求，而是把最终提示词与目标路径写入工单；agent 完成后重跑同一命令即验收，
验收按工单尺寸体检，比例不符或解不出图像流则拒收并保留工单。

主线程按提交顺序回填：画布路径、`gen.image` 快照（含 Envelope）、设定图指纹、作废旧一致性判定、
存量片段置 retake、落待审、入账、逐镜 save。候选批只登记待选品与出候选时的设定图指纹，`pick` 定稿
上画布时才执行这一套。`project refs` 与 `cover` 的设定图/封面并发出图（封面按系列背景 → 系列题字
+章节背景 → 章节题字三个波次），gen-video 降级轮的简笔板同样并发出板后再串行重发。

### 4.5 配音 `tts`

渲染档由 `project.effective_motion` 单点给出：已表态按表态；未表态按内容定档——scored 为 native，
任一正镜有对白为 native，全旁白/无词为 dubbed；kenburns 须显式写。配音、图生视频、合成、终检、
Studio 与库索引全部读它。`run` 与 gen-video 真发把定档写入章节，`-m/--native` 运行时覆盖随之升格为表态。

配音前先过选角闸：开口的说话人都要有音色引用，缺的逐个点名（`character set --voice-prompt`、
`voice custom --narrator --adopt 1`），不替用户挑模版。逐镜处理：弃用镜跳过；无词镜按 `dur`
占静音；native 的对白镜由模型发声，不进旁白轨。音色链为镜 `voice` > 角色音色表 > 旁白锁。
定制音色用档案那条不可变参考音加声线描述走生成式模型；模版音色先预热锚定参考音
（项目级缓存，入账）。
`done` 不重合成；`retake`、`--force` 或音色变更时归档重合成。多句镜逐句合成后拼为整镜 wav。

时长回填语义按档位分叉：kenburns 跟随配音加停顿，dubbed 只延不缩（`dur` 是表演窗口），
native 不动，片段已在盘时不覆写。收尾 `narration_parts` 拼整轨，缺镜拒拼。

### 4.6 图生视频 `gen-video`

**入口**：定档并写入章节；kenburns 拒发；provider 按本次点名 > 章节 `video_provider` > profile 链
解析（`models.resolve_video`，报价、真发、`explain` 与 Studio 标注同一处）。

**链图**：`framechain.plan` 按章级 `frame_chain` 或镜级结对算出末帧的发送方与被焊入方；参考孤岛
（全能参考或 V2V 的镜）两侧自动补无缝转场。V2V 总闸为 opt-in × native × provider 能力位，
镜级再过 guide 仲裁（`previz.v2v_shot`）。

**逐镜任务型态**：`_shot_plan` 按固定顺序仲裁——guide（sketch / previz 二选一）→ V2V →
previz 末帧（仅 native）→ 首尾帧衔接 → 首帧锚定 → 缺省全能参考。前四档为首帧任务，参考槽位只有
分镜图与末帧；缺省档把分镜图、简笔板、设定图全部挂 `reference_image`，一镜一片、镜间硬切。

**提示词**：`prompts.video_prompt` 拼头部契约句、板职责句、结构锁（「一段连续拍摄」；V2V 与作者已
自写的镜不发）、速率地板，正文为 `video_prompt` 或 delta 骨架，拍表编为分段时间轴——2.0 系列发
不带时间的「第N段」，2.5 发秒段；段头不带机位记号（支持多镜的型号读到「镜头N」会在一段素材内换机位）。
声源句按镜分治：native 对白镜开口、旁白镜闭声，dubbed 闭唇；台词逐字进正文。

**声源附件**：native 附音色锚定参考音（每镜至多 `max_ref_audios` 条，来自选角档案或预热缓存）；
dubbed 附本镜 TTS wav 作 `ref_audio` 并按窗口补齐。

**闸与账**：`--dry-run` 列逐镜提示词与报价；真发前 `_preflight_spend` 按同一份计划预留额度，
4K 与超单笔阈值需显式授权（`run` 只放行单笔告警）。请求串行发送、轮询、原子下载；回填 `clip`、
买下的整秒 `dur`、`gen.clip` Envelope、分镜图指纹、台词指纹，作废旧判定，落待审，入账。

### 4.7 合成 `assemble`

审阅闸：正式成片要求视觉阶段（kenburns 查 image，动镜查 clip）全部 `done`；要产旁白轨的章
再查进旁白轨的台词镜的 audio；`--draft` 绕过。音频底：scored 出整轨；曲库 BGM 按
`compose.use_bgm_for` 三档互斥——kenburns/dubbed 恒有，scored 与 native 只在显式加铺且未混烧时有。

`compose.build` 逐比例执行：先判主音轨可用性与两道混烧闸（旁白轨缺席、旁白镜按开口稿生成过的
双人声），判据只读文档与盘上产物，在渲染之前完成；再渲染片段（静图运镜或片段贴合）、编织转场、
拼接、烧字幕、混音、特效、水印；旧成片归档后写 `output`。

主音轨选择：

| 制式 | 主轨 | 床 / BGM |
|---|---|---|
| scored | 音频剧本整轨 | 无；`scored_bgm` 显式加铺 |
| native 缺省 | 片段原生音轨 | 无；`native_bgm` 显式加铺 |
| native 混烧 | TTS 旁白轨 | 片段原生音降为床，只在旁白镜窗口压制 |
| kenburns / dubbed | TTS 旁白轨 | 曲库 BGM，随旁白闪避 |

字幕落点按声源取：kenburns 由停顿声明算窗口；dubbed 取 TTS 有声段并随同步偏移；native 取片段音轨，
多句镜由本地 ASR 逐句划界，无法划界时收为整体首尾。

### 4.8 终检 `verify`

硬判六类：黑屏、该响却哑、时长、字幕条数、有词章缺旁白轨、动镜档正镜缺片段；另有旁白轨落点与
native 的 ASR 人声文字核对。结论写入 `verify`，只落审阅意见，不自动重生。

---

## 五、`run` 与人工节点

`run` 全程持操作锁，顺序为生图 → 配音（章级判据 `needs_narration_track`）→ 图生视频（动镜档）→
片段收口（正镜缺片段即中止，不落回静图）→ 字幕 → 音频底 → 合成 → 自动过审（`--no-approve` 关闭）。
`run` 是真发：未表态章节在准入后写入渲染档。`compose.build` 对动镜档缺片段同样拒合成，静图形态
只经显式 `assemble -m a`。

缺省工作方式为逐步推进：先试首镜与结构差异最大的另一镜，再放全章；`gen-video` 先 `--dry-run`；
需要在 Studio 试听、点选、上传时先启动控制台。

---

## 六、设计取舍

- 对白上镜的动镜章走 native 加音色锚定，dubbed 用于全旁白解说章：模型自声的口型与音色同源；
  烧录轨与模型口型两条时间轴不同源，无法对齐。
- native 缺省不烧 TTS、不叠曲库 BGM：片段自带人声与环境声，叠加即同一句话两个声源、一条床被顶掉。
  混烧为显式开关，开启后旁白镜须按闭声稿重生。
- 缺省一镜一片、镜间硬切：衔接、锚定、接力、V2V 全部显式 opt-in；转场只由用户插入，唯一自动转场
  是参考孤岛两侧的接缝。
- 段头不带机位义：分段时间轴是拍序，不是分镜；结构锁是附板镜唯一的连续性约束。
- 候选不占画布：宫格为待选品，定稿时才归档旧图、搬快照、退化片段。
- 锁只由人解：引擎与 agent 均不越过 `done`；回滚与换选为人眼动作，允许直接换画布。
- 系列级支出单列：设定图、主视觉、试音进系列台账并计入总额，单位产出成本仍按章节口径。
