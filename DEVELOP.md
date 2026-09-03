<!--
  This file is part of Kinema.
  Copyright (C) 2018-2099 BladeX (https://bladex.cn)
  SPDX-License-Identifier: AGPL-3.0-or-later
-->

# DEVELOP · 全景架构与二开手册

**分工**：[`AGENTS.md`](./AGENTS.md) 是作战地图——每个 agent 会话自动载入的纪律、
契约、守卫地图与易错点，**永远以它为准**；本文件是深读手册——首次接手或做大
改造前读一遍的全景叙事、逐模块索引与二开配方，按需加载不占会话常驻上下文。
首跑与环境就绪见 [`SETUP.md`](./SETUP.md)。本文与代码树的一致性由
`engine/tests/test_delivery.py` 的守卫强制（模块清单双向比对 · CLI 命令实测可解析 ·
路径存在性），所以它不会漂移成一份过期地图。

---

## 一、三层架构

**Kinema：把一个「主题」自动做成短视频成片。** 三层分工是全仓最重要的一条设计线：

1. **指挥层**（`.claude/skills/` 正文单源 + `agent/` 元数据与契约）——驱动引擎的 **agent 本人**。
   一切需要智能的环节都在这层：联网检索、写文案、拆分镜、产出 PromptSpec / ChapterPlan、
   调度引擎和管理人工节点。跨工具发现以 `.claude/skills/` 为唯一实体（正文原地编辑，
   frontmatter/skill.json 由编译器按 `agent/manifest.json` 维护），仓库根
   `.agents/skills` 是同一目录的 symlink 别名；宿主业务差异止于 adapter。
2. **执行引擎**（`engine/kinema/`，Python 包 + CLI）——确定性环节：
   生图、配音、字幕、图生视频、FFmpeg 合成、审阅/版本/血缘、Studio 可视化、
   Prompt 编译、Agent 计划校验与持久化。**引擎内没有 LLM——文案与分镜的智能由指挥层提供**；引擎追求
   确定性、可断点续跑、mock 全链路离线可跑。
3. **能力层**（云 API，`config/models.yaml` 声明）——图像/视频/语音/音乐的可插拔
   provider。换模型 = 改 YAML；特例是 `agent` 生图 provider（工单模式）：能力长在
   指挥层 agent 身上，引擎开单、agent 产图、重跑验收。

**Studio**（`python3 -m kinema studio`）横跨二三层：stdlib 三层（scanner 只读扫描 /
server HTTP / studio_app 原生 ESM 前端），所有写操作与 CLI 走同一条
`Project.load → 领域模块 → Project.save` 路径，无第二套状态机。

## 二、一条数据流走读（project.json → 成片）

**装配**（`cli.py` 的 `_stage_wrapper`）：`ConfigStore.load` 读 `config/models.yaml`
（+ `config/models.local.json` 覆盖层 + `config/voices.yaml` + secrets 三层）→
`Project.load` 读章节 JSON → 比例/motion 运行时覆盖 → `ModelRouter` 四层解析
（profile → 别名 → 连接段 → `_ADAPTERS[(capability, impl)]` 工厂）→ `PromptCompiler`
按解析出的语言与能力把作者语义封装为不可变 PromptEnvelope → 执行
`stage_*` → `project.save()`。

**阶段链**（`run` 一条龙 = 依次触发）：

| 阶段 | 入口 | 协作模块 |
|---|---|---|
| 生图 | `stage_gen_image` | `pipeline/prompts.py` 拼装提示词（最贵资产）· `pipeline/variation.py` 软闸 lint · `pipeline/checkpoint.py` 断点跳过 · `pipeline/candidates.py` 宫格候选 · `parallel.py` 三段式并发 · 生图三级路由见 `models.image_route` |
| 配音 | `stage_tts` | `voicecast.py` 解析镜级音色 → TTS provider → `ffmpeg.py` 拼旁白轨 |
| 图生视频 | `stage_gen_video` | `budget.py` 事前闸 · `providers/grades.py` 画质档 · `previz.py`/`sketchboard.py` 运动预演条件件 · `pipeline/refplan.py` 参考装配 |
| 口型精修 | `stage_lipsync` | dubbed 档的可选增强步，底片出齐后按最终配音重绘对白镜口型；`providers/lipsync/` 适配，未配凭证点名跳过 |
| 字幕 | `stage_subtitle` | `pipeline/subtitle.py` 时间轴 → ASS |
| 音乐 | `stage_music` | music provider 或 `audio_registry.py` 本地曲库 |
| 音频剧本 | `stage_score` | `audioscript.py` 按转场切段 → seed-audio 逐段生成 → `ffmpeg.py` 拼整轨（`audio_mode: scored` 专有，与配音+音乐两轨互斥） |
| 合成 | `stage_compose` | `pipeline/compose.py` 总编织（见下） |

**合成内部**（`pipeline/compose.py` 依次编织）：先判主音轨可用性与混烧闸，再渲染
片段（`kenburns.py` 静图运镜或片段贴合）→ `transitions.py` 转场段 → 拼接为无声视频
→ 同一张 filtergraph 内视频链走 `effects.py` 特效与 `subtitle.py` 烧字幕、音频链走
`mixdown.py` 分轨混音（特效自带的环境音与转场音效并入）与响度归一 → `ffmpeg.py` 落盘，
逐比例产出写 `project.output`。

**持久化**：`Project.save()` 合并人工改动后写 JSON，`storage/` 按
`config/storage.yaml` 分发——`local.py`（JSON 即数据库，默认）或 `mysql.py`
（库为真源、JSON 为工作副本），媒体经 `storage/media.py` 可上云。

**Agent 正式写入**：宿主先调用 `agent context` 获取任务最小上下文与章节 revision，提交
ChapterPlan 给 `agent plan validate` 纯计算校验，再由 `plan apply` 在章节短锁内重读、CAS、
应用 semantic patch 并追加 provenance。PromptSpec 只作为计划期 IR，由 Gateway 投影为章节
author-owned 字段；生成阶段再由 PromptCompiler 产生唯一 PromptEnvelope，dry-run、真实请求和
`shots[].gen` 留痕共享同一对象。完整跨命令 OperationCoordinator 仍属于后续事务层。

**工作区路径**：产物一律落 `project/<项目id>/`；章节工作目录 = 章节 JSON 同名 +
`_work` 后缀（`Project.workdir`），生图落 `_work/images/shot_<id>.png`，成片落
`_work` 下的 output 子目录。`_work` 是 Studio 片库的扫描签名，新目录别乱带。

## 三、引擎模块地图（`engine/kinema/`）

### 核心域

| 模块 | 职责 |
|---|---|
| `__init__.py` | 包根，导出版本号 |
| `__main__.py` | `python -m kinema` 入口 |
| `adaptation.py` | 剧本改编 Track A：确定性结构切分（零 LLM） |
| `agent_assets.py` | Agent manifest/contracts/Skill/adapter 的确定性编译与漂移检查 |
| `agent_gateway.py` | 多宿主最小上下文、ChapterPlan 校验、章节 revision CAS 与语义写入 |
| `agent_system.py` | 编译后 Skill catalog、确定性路由与 Agent doctor |
| `audio_registry.py` | 音频资产注册表（`config/audio.yaml` 读取层：BGM 情绪 + 音效语义键） |
| `batch.py` | 跨镜批量编辑（--set/--append/--replace，锁定保护） |
| `branding.py` | 白标品牌配置 |
| `budget.py` | 预留额度：花钱前把整批请求算清（事前闸） |
| `business.py` | 成本运营台账（预估/实际双轨） |
| `cli.py` | CLI 统一入口（顶级子命令全表以 `python3 -m kinema -h` 为准，命令行为终极真源） |
| `config_overlay.py` | 模型配置覆盖层（网页/CLI 的激活项与连接段，凌驾 models.yaml；`explicit_default` 是生图路由①级判据） |
| `decisions.py` | 决策审计 `decisions[]`（指挥层取舍留痕） |
| `deliver.py` | 交付包一键导出 |
| `effects.py` | 视觉/听觉特效框架（`EFFECTS`/`EFFECT_META` 双表） |
| `errors.py` | 统一异常（KinemaError/ConfigError/ProjectError/ProviderError） |
| `export.py` | 静态审阅页导出 |
| `ffmpeg.py` | FFmpeg/ffprobe 封装（run/probe/concat 原语） |
| `fonts.py` | 排版字体风格库 |
| `lineage.py` | 资产血缘（参考图登记与失效追踪） |
| `locking.py` | 跨进程文件锁（章节操作锁/写锁底座，flock 与 msvcrt 双实现） |
| `models.py` | 配置真源加载 + 能力路由（`ConfigStore`/`ModelRouter`/`_ADAPTERS`/`image_route` 生图三级路由） |
| `novel.py` | 原创小说创作层的 Python 半（登记/取料/确定性体检） |
| `parallel.py` | 并发执行层（主线程排计划→工作线程产文件→主线程回填） |
| `previz.py` | 3D 预演参考片登记（导演台在引擎侧的落点） |
| `project.py` | project.json 读写与 checkpoint（`Project` 类） |
| `prompt_contract.py` | PromptSpec、PromptEnvelope、机器契约注册表与稳定摘要 |
| `refine.py` | 框选局部改造 + 设定图候选定稿 |
| `review.py` | 分镜审阅状态机（五态 + omt） |
| `sheets.py` | 设定图规格与版式规则单一真源 |
| `sketchboard.py` | 简笔分镜预演板（previz 之外第二条运动预演路径） |
| `skills.py` | 画风 → 归属 skill 的单一真源 |
| `study.py` | 参考片读片（拆成可测量量） |
| `supply.py` | 素材直供 BYO（现成图登记为镜画面，跳过生图） |
| `templates.py` | 项目模板 / 平台规格预设 |
| `voicebank.py` | 音色档案库（声线描述 → 定制立档 → 启用为缺省；试音为模版例外；引用账管删除） |
| `voicecast.py` | 镜级配音策略（每一句用哪把声音、占多长时间） |
| `audioscript.py` | 音频剧本分段（按转场镜切，单次生成上限） |
| `workspace.py` | 工作区/项目管理（文档式 CRUD，后端可插拔） |

### 合成流水线（`engine/kinema/pipeline/`）

| 模块 | 职责 |
|---|---|
| `__init__.py` | 流水线包根 |
| `anchorframe.py` | 首帧锚定判据（显式 opt-in：章级 `anchor_frame`/镜级/`--anchor-frame`，分镜图作 `first_frame` 硬锁第 0 帧、不发末帧、镜间硬切） |
| `camera.py` | 大师级运镜 preset 库（36 preset，3D 导演台与 `shots[].camera` 单一真源） |
| `candidates.py` | 宫格候选选优 |
| `checkpoint.py` | 断点续跑判定（已产出即跳过） |
| `compose.py` | 成片合成总编织 |
| `consistency.py` | 角色跨镜一致性（引擎产料→指挥层判定→CLI 回填） |
| `cover.py` | 封面系统（key visual + 章节封面） |
| `framechain.py` | 首尾帧衔接判据（显式 opt-in：章级 `frame_chain`/`--chain`，缺省关闭 · 遇转场断链） |
| `kenburns.py` | Ken Burns 静图运镜 |
| `mediacheck.py` | 媒体体检（verify 成片自审 + inspect 供料体检） |
| `speech.py` | 片段音轨里的有声段落探测（字幕落点跟随真实说话时间） |
| `asr.py` | 本地语音转写（faster-whisper 可选依赖：native 人声文字核对 + 字幕逐句划界） |
| `mixdown.py` | 末级混音（让路 EQ/闪避/响度归一） |
| `prompts.py` | PromptCompiler 与提示词策略（双语选材/摄影地板/防字地板/驳回闭环） |
| `refplan.py` | 视频参考装配单源（RefPlan：manifest/ref_images/envelope/预览/content[] 五处消费一处产出） |
| `subtitle.py` | 字幕 ASS 生成 |
| `tailrelay.py` | 尾帧接力判据（显式 opt-in：章级 `tail_relay`/`--tail-relay`，上一镜真实末帧作下一镜参考图） |
| `transitions.py` | 转场系统（转场镜过渡） |
| `variation.py` | 分镜单调度 lint（软闸）+ 反 slop 空词表 |
| `versioning.py` | 产物版本栈（归档+回滚） |
| `watermark.py` | 动态水印（弹性漫游防搬运） |

### 能力层（`engine/kinema/providers/`）

基座 4：`__init__.py` 包根 · `_util.py` HTTP 重试/下载共享工具 · `base.py`
抽象基类与统一 Result（ImageResult/TTSResult/VideoResult/MusicResult，失败一律抛
ProviderError）· `grades.py` 视频画质档位目录。

| 能力 | 适配器 |
|---|---|
| image（7） | `__init__.py` · `seedream.py` 火山方舟 · `nano_banana.py` Google Gemini · `wan.py` 阿里通义万相 · `minimax.py` image-01 · `agent.py` **agent 原生生图工单模式**（不发网络请求：缺图开单抛 pending、有图零成本验收） · `mock.py` ffmpeg 占位帧 |
| video（5） | `__init__.py` · `seedance.py` 火山方舟 · `veo.py` Google Veo · `minimax.py` H3 · `mock.py` Ken Burns 占位段 |
| tts（5） | `__init__.py` · `seedtts.py` seed-tts-2.0 固定音色 · `doubao.py` seed-audio 生成式 · `minimax.py` 海螺 · `mock.py` 正弦占位人声 |
| music（5） | `__init__.py` · `elevenlabs.py` ElevenLabs（缺 key 自动降级本地曲库） · `local.py` 本地免版权曲库 · `minimax.py` music-3.0 · `mock.py` 正弦背景床 |
| lipsync（3） | `__init__.py` · `volc.py` 火山视频改口型（dubbed 对白镜口型精修，req_key 按官方文档配置） · `mock.py` 底片复制桩 |

### 存储（`engine/kinema/storage/`）

`__init__.py` 配置发现+后端工厂+保存钩子 · `base.py` 接口与派生元数据 ·
`local.py` 本地 JSON（默认零依赖） · `mysql.py` 库为真源 JSON 为工作副本 ·
`media.py` 媒体上云（OSS/COS/TOS 双真源） · `snowflake.py` 雪花主键。

### Studio 后端（`engine/kinema/studio/`）

`__init__.py` 分层说明 · `scanner.py` 只读数据层（组装展示模型） · `server.py`
HTTP 层（路由/静态资源/Range 媒体流，约 70 条 `/api/*`） · `actions.py` 写操作
领域层（复用 review/versioning/consistency） · `jobs.py` 异步任务器（后台
`python -m kinema` 子进程，网页与 CLI 同一条写路径）。

## 四、Studio 前端（`engine/kinema/studio_app/`）

原生 ES Module、免构建、零三方依赖（3D 内置 three.js 副本于 `vendor/`）。
入口 `index.html` + `app.js`（路由/启动/段序锚定）+ `style.css`（全站样式）。

**`app/` 17 模块**：`core.js` 基座（零静态依赖，视图动态 import 防 TDZ）·
`components.js` 站内 UI 组件单一落位（openShell 弹层骨架工厂）· `widgets.js`
通用件 · `state.js` 跨模块可变状态 · `shell.js` 导航外壳 · `brands.js` 服务商
品牌标 · `overview.js` 总览 · `project-new.js` 新建项目弹层 · `project.js` 项目
详情/剧本工作台 · `chapter.js` 章节制作台 · `shot-tools.js` 分镜工具弹层 ·
`shot-display.js` 分镜枚举展示层 · `panels.js` 版本/待审/看板 · `ledger.js` 导出/成本/片库 · `config.js` 模型配置
中心 · `playbook.js` 指令集 · `skill.js` SKILL 指挥层只读大屏（`skill_board` 按 kind 分组）。

**`director/` 9 模块（3D 导演台，懒加载）**：`stage.js` 入口 · `rig.js` 灰模骨架 ·
`actors.js` 角色动作/路线 · `cameras.js` 运镜 preset 求值器 · `pathtool.js` 走位
路线交互 · `timeline.js` 时间轴 · `exporter.js` 确定性逐帧导出 · `preview.js` 资产
缩略图离屏渲染 · `ui.js` 三栏骨架。

**五条前端纪律**：依赖显式 import；core 零静态依赖回调走动态 import；跨模块重赋值
状态只进 `state.js`；源级守卫读全模块拼接文本；筛选工具条两条（项目维度一律
`uiSelect`·控件初值回填持久化 filter）。新弹层一律 `openShell`。

## 五、CLI 命令全表

| 分组 | 命令 |
|---|---|
| 生成阶段 | `gen-image` `gen-video` `tts` `subtitle` `music` `score` `lipsync` `compose` |
| 组合一条龙 | `assemble` `run` `animatic` |
| 环境配置 | `doctor` `setup` `config` `oss` `db` |
| Agent 控制面 | `agent`（catalog / route / doctor / assets / contract / context / plan / explain） |
| 项目 CRUD | `init` `project` `chapter` `character` `scene` `prop` |
| 审阅质量 | `review` `pick` `versions` `lineage` `consistency` `lint` `verify` `milestones` `decision` |
| 内容素材 | `batch` `refine` `supply` `transition` `sfx` |
| 运动预演 | `previz` `sketch` |
| 音色 | `voice` |
| 成本交付 | `ledger` `watermark` `cover` `deliver` `export-review` `export-pitch` |
| 长文创作 | `adapt` `study` `novel` |
| 模板规格 | `template` `spec` `assets` |
| 可视化 | `studio` |

命令行为的终极真源是 `cli.py`（文档权威顺序：cli.py > AGENTS.md/README/SKILL >
其余 docs）。`novel` 族是全 CLI 最大的子命令族（子命令全表 `python3 -m kinema novel -h`）。

## 六、测试守卫地图（`engine/tests/`）

基础设施：`support.py`（FakeProject 桩 + 本地存储环境守卫）·
`jsscope.py`（前端 ESM 静态分析器：词法去噪 / 声明面 / 引用面 / import 图，纯 stdlib 不需要 node）。

| 守卫 | 守什么 |
|---|---|
| `test_adapt.py` | 剧本改编与设定图样板分发 |
| `test_sheets.py` | 设定图契约正文（三区两视表/道具三视式/纯图片纪律）与蓝图对齐（画布比例、一式一张） |
| `test_anchorframe.py` | 首帧锚定（判据真源/gen-video 接线/三条让位通道/配额裁剪点名） |
| `test_agent_gateway.py` | 最小上下文、ChapterPlan 白名单、review 锁、revision CAS 与零写入失败语义 |
| `test_agent_system.py` | Agent manifest/contracts 编译、Skill catalog、路由、发现与上下文预算 |
| `test_audioscript.py` | 音频剧本分段（接缝落转场·段内相对秒）与 scored 三路让开 |
| `test_lipsync.py` | 口型精修：语态三闸（只修对白镜）· 源指纹幂等与换音色重算 · volc 提交体/签名 · 未配置优雅跳过 · dubbed 缺省接线 |
| `test_batch.py` | 批量编辑四操作/锁定/undo（撤销按同规则置 retake） |
| `test_budget.py` | 预留额度事前闸 |
| `test_business.py` | 台账数学（双轨成本/废片/运营指标） |
| `test_camera_presets.py` | 运镜库与 storyboard.md 逐字节对拍 |
| `test_candidates.py` | 宫格候选命名/seed/定稿 |
| `test_checkpoint.py` | 断点续跑判定语义 |
| `test_config_center.py` | 覆盖层/配置中心/probe/`setup --json` 契约 |
| `test_config_drift.py` | EMBEDDED 内置默认与磁盘配置对拍（含画风→skill 覆盖） |
| `test_consistency.py` | 角色跨镜一致性 |
| `test_cover.py` | 封面提示词拼装与排版 |
| `test_decisions.py` | 决策审计 |
| `test_deliver.py` | 外挂 SRT 与烧录字幕同源 |
| `test_delivery.py` | 配音表现力契约 + agent 文档纪律（AGENTS 阅读地图 · skills 双轨 · 本文三重对拍守卫在此） |
| `test_design_refs.py` | 设定图挂载与血缘锁步 |
| `test_dialogue.py` | 镜内多段台词 |
| `test_directive_dialog.py` | 指令台弹层源级守卫 |
| `test_effects.py` | 特效双表一致 |
| `test_ffmpeg_quoting.py` | filtergraph 引号上下文（真渲染）/ 付费 POST 读超时不重 / 下载原子落地 |
| `test_face_route.py` | 写实人物合规链路（face_visibility 登记与并发存活 · 身份图纯文生图与 sheet_origin 五路写点 · A/B/C 路线仲裁矩阵与路线 B 请求形状 · 人脸拒自动降级轮/不停批/二拒死局 · 取景地契约句 ZH/EN） |
| `test_refplan.py` | 视频参考装配单源（五处消费的黄金逐位对拍 + RefPlan 构造期不变量） |
| `test_ffmpeg_capture.py` | ffmpeg 探测原语 |
| `test_frontend_integrity.py` | Studio 前端静态可解析性（悬空引用/import 图/孤儿分片；分析器 `jsscope.py` 及其自测：最小悬空样例必报、常见合法语法不误报） |
| `test_jobs.py` | Studio 异步任务器 |
| `test_locking.py` | 跨进程文件锁（冲突报持有者/进程内重入）与 Project.mutate 竞写重试 |
| `test_mix.py` | 混音闪避/让路 EQ |
| `test_motion_default.py` | 渲染档决策点（未表态按内容定档：有对白 native / 全旁白 dubbed / scored native·真发落盘表态·运行时覆盖升格·只读不写·显式 kenburns 拒发） |
| `test_novel.py` | 小说创作层全家（含 SKILL 文档与 CLI 对拍先例） |
| `test_parallel.py` | 并发生成不错得悄无声息 |
| `test_previz.py` | 3D 预演登记与 V2V |
| `test_prespend_gates.py` | 计费前质量闸（分镜图比例·旁白语态；判据与 lint 同源、非交互不替用户决定） |
| `test_prompt_contract.py` | PromptSpec 投影、Envelope 指纹、引用摘要与 provider 长度硬边界 |
| `test_prompts.py` | 提示词拼装两条最贵策略 |
| `test_providers_request.py` | Provider 请求体回归（含 agent 工单两态契约） |
| `test_review.py` | 审阅状态机 |
| `test_run_gate.py` | 动镜档片段收口（run 中止 / compose 拒合成 / verify 硬判）·native 混烧与 BGM 互斥单点·局部改造先产出再归档 |
| `test_router_defaults.py` | 模型路由三根支柱 + 生图三级路由 |
| `test_schema_contract.py` | project.schema.json 与实现零漂移 |
| `test_shell_layout.py` | 导航外壳双形态 |
| `test_shot_meta.py` | 结构化分镜元数据 |
| `test_sketchboard.py` | 简笔分镜板四不变量 |
| `test_snowflake.py` | 雪花主键（并发唯一/时钟回拨/序列耗尽借毫秒） |
| `test_studio_routes.py` | Studio HTTP 与数据层接线 |
| `test_study.py` | 参考片读片 |
| `test_subtitle.py` | 断行/时间码/模式分发 |
| `test_supply.py` | 供料媒体体检 |
| `test_tailrelay.py` | 尾帧接力（判据真源/串行注入/衔接与转场边界） |
| `test_templates.py` | 内置模板与区间校验 |
| `test_transitions.py` | 转场 spec 归一化 |
| `test_variation.py` | 分镜 lint 与 art_direction |
| `test_verify.py` | 成片自审 |
| `test_asr.py` | 本地 ASR（划界纯函数 / verify 人声文字核对 / 合成侧回落链） |
| `test_versioning.py` | 归档不可变/回滚可反复 |
| `test_video_preview.py` | Studio 实发提示词预览（dry-run 同路/零落盘/三层同源） |
| `test_voice_anchor.py` | 音色锚定（显式选角计划/绑定句措辞/请求体/lint 缺口） |
| `test_voicebank.py` | 音色档案不可变/在用态可推导/删除前查引用 |
| `test_watermark.py` | 弹性漫游物理正确性 |
| `test_workspace.py` | slug/章节 id/继承拷贝 |

## 七、二开配方（六条高频扩展路径）

**1. 新增能力 provider**（先例：`agent.py` 工单模式、`mock.py` 离线占位）
- 同厂新模型：`config/models.yaml` providers 段加别名 + `impl` 指已有适配器，**零代码**。
- 新厂商四步：① `providers/<capability>/<name>.py` 写适配器（构造 `(conn, store)`，
  返回统一 Result，失败抛 ProviderError，base_url 以 API 版本号结尾）②
  `models.py` 的 `_ADAPTERS` 登记一行 ③ `config_overlay.py` 的 `IMPL_META` 加
  label/vendor（守卫强制与 `_ADAPTERS` 恰好同集；无密钥/无端点的加
  `optional_key`/`no_endpoint`）④ `test_providers_request.py` 补请求体用例。
- models.yaml 与 `models.py` 的 EMBEDDED 内置默认若同时登记，须字段对拍
  （`test_config_drift.py` 与 `test_router_defaults.py` 盯着）。

**2. 加画风 profile**：`config/models.yaml` profiles 段加一段（`label` 中文名与
`style_prefix_en` 必填）→ 在 `agent/manifest.json` 的唯一 route/workflow Skill 上登记 profile；
若是新题材，先在 `.claude/skills/kn-<x>/` 创建正文包并在 manifest 登记，再运行
Agent assets 编译与检查。运行时 `skills.py` 只消费生成 catalog，不维护第二份映射。

**3. 加特效**：`effects.py` 返回 EffectPlan 并注册 `EFFECTS` + 同步 `EFFECT_META`
元数据（`test_effects.py`/`test_config_drift.py` 强制两表键一致）。

**4. 新 CLI 子命令**：`cli.py` 里 `add_parser` + `cmd_*` 函数 + `set_defaults(func=)`；
改任何命令行为必须同步 AGENTS.md 命令表与相关 SKILL 文档（`test_novel.py` 的
文档对拍是先例，本文件的命令表也被守卫盯着）。

**5. 新 Studio 面板/前端模块**：后端 `studio/scanner.py` 下发数据（只读）或
`studio/actions.py` 写操作（复用领域模块，绝不长第二套写路径）；前端在
`app/` 加语义命名模块、`app.js` 锚定 import，UI 一律用 `components.js` 站内
组件与 `openShell` 弹层骨架；源级守卫进 `test_shell_layout.py`/
`test_directive_dialog.py` 同款拼接文本范式。

**6. 新 skill**：在 `.claude/skills/<名>/` 创建 SKILL.md 正文包（单源无拷贝），并在
`agent/manifest.json` 登记类型、状态、触发、依赖、权限和 profile；长表/模板下沉
references。随后运行 `python3 tools/agent_assets.py compile` 与 `check`。frontmatter、
`skill.json`、`docs/skills/INDEX.md` 与运行时 catalog/contracts 由编译器维护，禁止手改。

## 八、配置面速查（`config/`）

| 文件 | 作用 |
|---|---|
| `models.yaml` | 五段：version/defaults（含 `defaults.providers` 能力级默认——换厂商改一行）/canvas 画布/providers 连接段（别名清单）/profiles（画风档全表） |
| `models.local.json` | 覆盖层（网页/CLI 写的激活项与连接段，gitignore，凌驾 yaml；`config activate` 即生图路由①级的"显式激活"） |
| `voices.yaml` | 170+ 音色别名 → 火山 voice_type |
| `secrets.yaml` / `secrets.local.json` | 密钥（绝不入库；解析优先级 env > local > yaml） |
| `storage.yaml` | backend local/mysql + media 本地/上云 |
| `templates.yaml` | 平台规格模板（`project new --template`） |
| `branding.yaml` | 白标三键 |
| `audio.yaml` | BGM 情绪目录 + 音效语义键 |
| `README.md` | 配置中心总览与关联总图 |

发现规则：从当前目录与包位置向上找 `config/<名>.yaml`；`KINEMA_MODELS`/
`KINEMA_STORAGE` 可显式指路；缺 PyYAML 回退引擎内置默认。
临时切换存储：`KINEMA_STORAGE_BACKEND=local|mysql`（不产生 git 改动）。
生图路由声明：`KINEMA_AGENT_IMAGEGEN=1`（三级解析见 AGENTS.md §1 第 3 条）。

## 九、文档纪律（写文档前必读）

- **真源顺序**：`cli.py`（命令行为）> AGENTS.md / README / kinema SKILL.md /
  `config/README.md` > 其余 docs。改代码行为必须同步这些文档。
- 本文件被守卫钉住：模块清单与 `engine/kinema/` 双向全量比对（多写少写都红灯）、
  命令表逐条喂 argparse、反引号路径逐个查存在。**加模块/命令时按守卫报错更新
  本文对应表格即可**，不需要背规则。
- 每个代码文件头部带标准 AGPL 声明块（照抄同目录既有文件，勿删改）；许可头里
  不写产品定位语；提交规范单行 gitmoji（详见 AGENTS.md 开发规范）。
