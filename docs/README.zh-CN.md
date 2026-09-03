# Kinema 文档目录

[English](README.md) · [简体中文](README.zh-CN.md)

本目录承载仓库的**参考文档**：只放 Markdown 与规范文本。素材（图片、视频、字体）
一律落 `assets/`——这条分界由测试守卫钉死。

## 布局

| 路径 | 内容 |
|---|---|
| [`agents/`](agents/) | **工程指南的详情层**——按模块一篇。由根 [`AGENTS.md`](../AGENTS.md) §7 阅读地图索引；编码 Agent 只在动到对应模块前按需打开，不作为默认上下文。完整地图见下 |
| [`kinema/`](kinema/) | **架构与数据契约**——文件地图见下 |
| [`skills/`](skills/) | **工具中立 Skill 索引** `INDEX.md`——给未采标工具（Windsurf / Aider / Zed）和 GitHub 网页读者。由 `tools/agent_assets.py` 生成，勿手改 |
| [`sql/`](sql/) | **MySQL 建库建表脚本** `kinema.sql`，由 `python3 -m kinema db schema` 从 `engine/kinema/storage/mysql.py::_SCHEMA` 生成。勿手改，重新生成 |

### `agents/` 文件地图

根 `AGENTS.md` §7 管的是**什么时候读**，本表管的是**里面写了什么**。按领域分组，不按文件名排序。

**测试与运行纪律**

| 文件 | 内容 |
|---|---|
| [`guard-map.md`](agents/guard-map.md) | 改动面 → 必过测试。明确是**导航图而非闸**：随手维护，不设覆盖对拍。另载新守卫只能加在哪一层的收敛纪律 |
| [`concurrency.md`](agents/concurrency.md) | `project refs` / `gen-image` / `tts` 的并发纪律（`--concurrency N`，缺省 4）。风险全在「错得悄无声息」，故有写回纪律。真源 `kinema/parallel.py` |

**资产与一致性**

| 文件 | 内容 |
|---|---|
| [`asset-versioning.md`](agents/asset-versioning.md) | 设定图版本栈：重生成 / 改造 / 回滚前，旧图先归档进 `assets/refs/versions/`。与分镜版本谱系对称 |
| [`character-fields.md`](agents/character-fields.md) | `characters[]` 的五个**系列级常量**字段——`required_emotions` / `required_actions` / `required_views` / `silhouette_notes` / `constraints`。按集填会被下次覆盖 |
| [`moodboard.md`](agents/moodboard.md) | 参考库 / 风格垫图。项目一旦有垫图就**默认注入后续一切生成**；关闭只有三条路 |
| [`photoreal-face.md`](agents/photoreal-face.md) | 写实人物怎么合法走完设定图 → 分镜图 → 视频请求。视频请求里**有两个含脸位**；受信豁免绑「是不是文生图产物」。判据真源在 [`kinema/seedance-face-policy.md`](kinema/seedance-face-policy.md) |
| [`consistency.md`](agents/consistency.md) | 角色跨镜一致性。协议是引擎产料 → 指挥层判定 → CLI 回填；引擎**一行分数都不算**（逐条理由在文内） |
| [`cover.md`](agents/cover.md) | 封面设计：系列主视觉 + 章节封面，以及 Studio 卡片依赖的图源三级回落（封面 → 成片海报帧 → 首个正镜分镜图） |

**声音**

| 文件 | 内容 |
|---|---|
| [`mixdown.md`](agents/mixdown.md) | 混音信号链。数值单一真源 `pipeline/mixdown.py`，**只**落在 `compose.build` 的最终 filtergraph——绝不进 `kenburns.fit_clip`，否则缓存键不失效会静默复用旧音轨 |
| [`native-voiceover.md`](agents/native-voiceover.md) | native 配音混烧与镜内多角色时长语义。native 缺省**不叠**我们的 TTS 的理由，以及显式混烧后按镜分治的形态：旁白镜闭声出演、TTS 上主轨，对白镜仍由模型发声 |
| [`voice-anchor.md`](agents/voice-anchor.md) | 音色锚定：把选角链的音色样本作为 `reference_audio` 随生视频请求附发，模型用该嗓音念提示词里的台词——口型、台词、嗓音同源于一次生成 |
| [`voice-bank.md`](agents/voice-bank.md) | 音色档案库：候选是临时物、档案是资产。引用闸挡住「删掉仍被分镜引用的音色」 |
| [`lipsync.md`](agents/lipsync.md) | 口型精修：dubbed 章对白镜的可选增强步，接在 `gen-video` 收尾，未配厂商凭证时点名跳过 |

**剪辑与特效**

| 文件 | 内容 |
|---|---|
| [`transitions.md`](agents/transitions.md) | 转场系统。**缺省一个转场都没有**——镜间直接拼接；只有显式开了 `frame_chain: true` 的章才首尾帧衔接 |
| [`tail-relay.md`](agents/tail-relay.md) | 尾帧接力：上一镜片段的**真实末帧**作为下一镜的参考图随请求发出，让镜与镜的开场构图、人物位置与光线有像素级依据 |
| [`anchor-frame.md`](agents/anchor-frame.md) | 首帧锚定。分镜图以 `role=first_frame` 硬锁片段第 0 帧，**不发末帧、镜间硬切**——补的是全能参考缺省档与 `frame_chain` 之间的空档 |

**预演**

| 文件 | 内容 |
|---|---|
| [`previz-v2v.md`](agents/previz-v2v.md) | 3D 预演与参考视频 V2V 易错点全集。⚠ 带圈小节编号被他处按 `previz-v2v ⑦` 引用，改号即断链 |
| [`sketchboard.md`](agents/sketchboard.md) | 简笔分镜板纪律；与 previz **逐镜互斥**，仲裁真源 `sketchboard.active_guide` |
| [`study.md`](agents/study.md) | 参考片读片护栏。性质是**版权护栏**——三条，一条都不能松 |

**质量闸**

| 文件 | 内容 |
|---|---|
| [`lint.md`](agents/lint.md) | 分镜单调度体检：维度全表与逐条判据 |
| [`verify.md`](agents/verify.md) | 成片自审：硬判（成片缺失 / 容器 / 黑屏 / 该响却哑 / 时长 / 字幕 / 旁白轨 / 片段）与待修（削波 / 响度 / 落点 / ASR 逐句人声核对）。阈值单一真源 `pipeline/mediacheck.py` |

**Studio 前端**

| 文件 | 内容 |
|---|---|
| [`studio-frontend.md`](agents/studio-frontend.md) | Studio 三层与前端模块架构——全 stdlib，无构建步骤 |
| [`stage-console.md`](agents/stage-console.md) | 3D 导演控制台：在烧 Seedance **之前**先在浏览器里把戏排出来 |
| [`director-stage-ui.md`](agents/director-stage-ui.md) | 该控制台的 UI 纪律，改前必读。⚠ 带圈小节编号被 `stage.js` 与 `test_previz.py` 按 `director-stage-ui ⑩` 引用，改号即断链 |
| [`directive-dialog.md`](agents/directive-dialog.md) | 指令台：「把指令交给 AI」的按钮一律走 `openDirectiveDialog`，不做一点即复制 |

**创作与控制平面**

| 文件 | 内容 |
|---|---|
| [`decisions.md`](agents/decisions.md) | 决策审计——章节文档顶层、append-only、只经 `decision add` 写入。让后续会话不再反复推翻已定的取舍 |
| [`novel-discipline.md`](agents/novel-discipline.md) | 原创小说层十条纪律；真源 `kinema/novel.py` + `kinema-novel` SKILL |
| [`skill-discovery.md`](agents/skill-discovery.md) | Skill 发现路径双轨。实体**只有一份**，在 `.claude/skills/<名>/SKILL.md`；`.agents/skills` 是指向它的 symlink 别名 |

### `kinema/` 文件地图

| 文件 | 内容 |
|---|---|
| [`design.md`](kinema/design.md) | 一页式架构地图：分层、管线、一致性、声音与成本，附立项取舍存档 |
| [`video-pipeline.md`](kinema/video-pipeline.md) | 系统边界、文档/状态/并发模型，以及按数据流展开的视频生成流程：项目 → 章节 → 分镜 → 生图 → 配音 → 图生视频 → 合成 → 终检，逐步写清判据、产物、闸与写回 |
| [`project.schema.json`](kinema/project.schema.json) | `project.json` 文档契约。引擎运行时不跑 jsonschema，但新增读写字段必须同步到这里 |
| [`providers.md`](kinema/providers.md) | 能力层 API 速查：图像/视频/语音/音乐厂商、价格备注与接入状态 |
| [`workspace.md`](kinema/workspace.md) | 工作区与项目管理：本地 JSON ⇄ MySQL 持久化模型 |
| [`seedance-face-policy.md`](kinema/seedance-face-policy.md) | Seedance 参考输入人脸政策：什么会被拒、判据是什么、合法通道有哪些 |

## `agents/` 文档的写作约定

在 `agents/` 下新增或修改文件，遵守三条：

1. **开篇先写真源。** 标题之后一行粗体，写实现真源、守卫测试或适用范围（有哪项写哪项）。
   正文是规则全文；`AGENTS.md` §7 只链过来，不复述。
2. **带圈小节编号绝不重排。** `previz-v2v.md` 与 `director-stage-ui.md` 被代码和测试
   按 `previz-v2v ⑦` / `director-stage-ui ⑩` 的形式引用，改号会静默断链。
3. **文档不能覆盖代码事实。** 文档与实现分叉时，修正单一真源并补行为守卫，
   不在这里糊过去。

## 各主题的文档归属（单一真源规则）

每个主题只有一个家，本目录不与其他文档重复：

- 工程纪律与稳定结论 → 根 [`AGENTS.md`](../AGENTS.md)（Agent Kernel）
- 模块地图、CLI 全表、二开配方 → [`DEVELOP.md`](../DEVELOP.md)
- 首跑与就绪判定 → [`SETUP.md`](../SETUP.md)
- 配置 → 只在 [`config/README.md`](../config/README.md)
- Agent/Skill 控制平面与编译管线 → 只在 [`agent/README.md`](../agent/README.md)
- 生产节点、提示词纪律与判例 → 编译后的 `.claude/skills/kinema/references/`（此处不复制）
