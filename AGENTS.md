# AGENTS.md

Kinema 面向编码 Agent 的唯一工程指南，也是所有宿主始终加载的 **Agent Kernel**。
Claude Code、Codex、Cursor、Copilot、Windsurf、Aider、Zed 等都以本文件为纪律真源；
`CLAUDE.md`、`.cursor/rules/`、`.github/copilot-instructions.md` 只承载宿主发现差异，禁止复制规则。

本文件只保留架构边界、不可违背的结论和知识导航。完整安装流程见 [`SETUP.md`](./SETUP.md)，
模块地图、CLI 全表和二开配方见 [`DEVELOP.md`](./DEVELOP.md)。`docs/agents/` 是按需读取的详情层，
不参与自动加载；只在动到对应模块前打开相关文件。

## 1. Agent 启动协议

1. 明确目标、边界、输入、输出和验收标准；局部信息缺失但不影响主路径时可基于明确假设推进。
2. 先运行 Agent 控制平面检查与确定性路由：

   ```bash
   cd engine
   python3 -m kinema agent doctor --json
   python3 -m kinema agent route --project <项目id> --json
   # 新项目改用 --skill <id> 或 --profile <id>
   ```

   路由优先级固定为：项目绑定 > 显式 Skill > 显式 profile > `kinema`。未知显式值直接修正，
   不做关键词猜测、隐藏降级或多 Agent 编排。
   要创作或修改章节时，再按任务读取 Gateway 上下文与机器契约；不要先读整份章节再整文件覆盖：

   ```bash
   python3 -m kinema agent contract prompt --json
   python3 -m kinema agent contract chapter-plan --json
   python3 -m kinema agent context --chapter <项目id>/<章节id> --task storyboard --json
   ```
3. 生产视频前运行 `python3 -m kinema setup --check --json`。先完成原生生图能力握手：当前会话实际
   暴露原生图像工具时可声明 `KINEMA_AGENT_IMAGEGEN=1`；无法确认时询问一次。不得用已安装的
   `codex` 命令或模型名称推断能力。显式 image provider 配置优先，能力确认只切换本次路由，
   不等于用户授权出图。`ready=true` 直接开工；有红项只补红项，不重走向导，也不反复确认已由
   持久化配置证明的事实。
4. 根据当前任务读取 `docs/agents/` 详情和对应 Skill；不要整目录加载，不要把长文档当作默认上下文。
5. 修改引擎后跑全量测试；修改 Agent/Skill 源后先编译再检查：

   ```bash
   python3 tools/agent_assets.py compile
   python3 tools/agent_assets.py check
   cd engine && python3 -m unittest discover -s tests
   ```

## 2. 产品与架构边界

Kinema 是本地化工作室级生产工具：把主题推进成多比例短视频，采用确定性 DAG 与人工审阅节点。
它不做 SaaS、订阅、多租户，也不上多 Agent。

三层分工不可混淆：

1. **Agent 指挥层**：`agent/manifest.json`、`agent/contracts.json` 与 `.claude/skills/` 正文。Agent 负责联网研究、
   创作判断、文案、结构化 PromptSpec / ChapterPlan 和人工节点协作；最终提示词与写盘由引擎确定化。
2. **执行引擎**：`engine/kinema/`。Python 包和 CLI 只做确定性生成、媒体处理、审阅、版本、血缘、
   Studio 与持久化。引擎内没有 LLM provider，不把创作判断塞进 Python。
3. **能力层**：`config/models.yaml` 声明的图像、视频、语音和音乐 provider。换模型优先改配置，
   不在业务代码硬编码厂商和模型。

Agent/Skill 控制平面是单源布局，数据流固定为：

```text
agent/manifest.json + agent/contracts.json + agent/adapters/ + .claude/skills/ 正文
                                  ↓ compile
.claude/skills frontmatter+skill.json+prompt-contract.md + agent_catalog.json + agent_contracts.json + Host Adapter + docs/skills/INDEX.md
```

- Skill 正文与 references 直接在发现目录 `.claude/skills/` 编辑，无第二份拷贝；名称、描述、
  类型、状态、权限等元数据只改 `agent/manifest.json`。
- frontmatter、`skill.json`、运行时 catalog/contracts、宿主入口和索引由编译器维护；
  `kinema/references/prompt-contract.md` 是 references 里唯一的编译产物（源在 contracts.json）。
  `kinema-digest` 覆盖正文与 references，抓「改了没重新编译」的漂移。
- `.agents/skills` 只允许指向 `.claude/skills`，不得出现第二份 Skill 实体。
- `engine/kinema/skills.py` 只消费编译 catalog，不维护手写目录。
- 公共 Skill 不声明宿主专属宽权限；来源、信任、状态和摘要由 manifest 与编译器生成。
- 修改正文或元数据后必须重新 compile 并让 `agent assets check` 无 diff。

## 3. 工作区与数据契约

工作区发现顺序：CLI `--workspace` > `KINEMA_WORKSPACE` > 源码仓库根 `project/` > 非源码环境
下从 cwd 向上找已有 `project/` > `./project`。仓库根或 `engine/` 入口会统一归一到仓库根
`project/`；local 与 MySQL 只切换持久化后端，不改变工作区路径。产物只写
`project/<项目id>/`，绝不写 `/tmp`、仓库根本身或其他未登记位置。

```text
project/<pid>/project.json                 系列文档
project/<pid>/chapters/<cid>.json          章节渲染契约
project/<pid>/chapters/<cid>_work/         图像、音频、片段、版本、样片和成片
project/<pid>/assets/                      角色、道具、场景、封面和音色资产
project/<pid>/exports/                     审阅、提案与交付包
```

`docs/kinema/project.schema.json` 是 `project.json` 文档契约；引擎不运行 jsonschema，但新增读写字段必须
同步 schema。`[engine-managed]` 字段只由引擎回填，Agent 不手写。

Agent 改章节的正式入口是 `agent context → ChapterPlan → plan validate → plan apply`。计划只接受
author-owned 白名单字段和 `add/update/omit/restore` 语义操作；Prompt 只提交 PromptSpec，由
PromptCompiler 投影并封装 PromptEnvelope。apply 必须携带 context 返回的 `expected_revision`，冲突后
重读上下文并重算，禁止重放旧计划。`agent_provenance` 与 `shots[].gen.*.envelope` 只由引擎写入。

关键不变量：

- `project.skill` 是指挥层绑定，创建时由显式 Skill 或 profile 确定性派生；后续按项目继续时不重新猜。
- `style_prompt(+_en)` 是项目画风单点真源；不要在每镜重复拼一套风格前缀。
- `characters`、`props`、`scenes` 是并列设定集；人能走进去的地方登记为 scene，不伪装成 prop。
- `shots[].characters/props/scenes/refs` 是镜级显式绑定；名字必须命中项目设定，否则引用不会注入。
- `shots[].id` 与盘上文件、版本和审阅记录绑定。弃镜置 `review=omt`，不要删镜或重排 ID。
- URL 形式的媒体字段视为已产出；不要因本地没有文件就清空。
- 改台词（`narration`/`lines[]`）会让已生成的配音与片段过期，换分镜图会让片段过期：
  `lineage mark` 按内容指纹判定并置 retake（gen-image 落新图时对存量片段就地置 retake），
  不要因盘上还有 wav/mp4 就当作仍然对得上。「改哪个字段失效哪个阶段」的字段名口径是
  `review.STAGE_FIELDS` 单一真源（Gateway 与 batch 共用），与指纹那条边互补。
- 章节继承是创建时拷贝；修改系列不会静默回灌已有章节。
- 三套状态分别是执行状态、镜级 review 状态机和由产物动态推导的章节状态，不得混写。
- 写盘统一 UTF-8、`indent=2`、`ensure_ascii=False`。

角色、道具、场景新建走 `character/prop/scene add`、改写走 `set`；长任务运行时不得手改系列数组，
避免旧内存副本整份覆盖。设定图、分镜图、音色和其他生成资产走版本栈与专用命令。

## 4. 生产与成本纪律

默认按人工节点推进：文案分镜 → 设定 → 首镜试图 → 全章生图 → 配音/音频剧本 → animatic → 合成 →
可选动态化 → 本地终检。只有用户明确要求全自动时才使用 `run`。

核心命令速查：

```bash
cd engine
python3 -m kinema project new --title "X" --id x --profile <profile>
python3 -m kinema chapter new x --title "本集标题"
python3 -m kinema lint --chapter x/<chapter> --strict
python3 -m kinema project refs x
python3 -m kinema gen-image --chapter x/<chapter> --only 1,3
python3 -m kinema tts --chapter x/<chapter>
python3 -m kinema animatic --chapter x/<chapter>
python3 -m kinema assemble --chapter x/<chapter>
python3 -m kinema gen-video --chapter x/<chapter> --dubbed --dry-run
python3 -m kinema verify --chapter x/<chapter>
python3 -m kinema consistency scan --chapter x/<chapter>
```

- 先试首镜和结构差异最大的另一镜，不只看一张就放全章。
- `gen-video` 默认串行且按秒计费；正式生成前必须 `--dry-run` 审逐镜提示词与报价。
- 4K、超单笔预算和平台发布都需要用户明确授权；Agent 不代改预算、不代补授权参数。
- `done` 是锁定，不被 `--force` 覆盖；要重做先置 `retake`，旧版进入版本栈；只解锁不重生置 `wfa`
  （`review set --state wfa`）。人眼定稿的
  两个动作例外：版本回滚与宫格换选直接换画布并回到待审。
- `motion` 只有章级一个入口，声源随之是章级制式、说话人级单声源；未写时引擎按内容定档（有对白 →
  native，全旁白/无词 → dubbed，scored → native），静图 kenburns 须显式写：**对白上镜的动镜章走 native + 音色锚定**（模型自声，
  口型与音色质感天生同轴），dubbed 的领地是全旁白解说章（闭唇出片、无嘴可对），lint
  `dubbed_dialogue` 点名错配。
  native 混烧（`native_voiceover`）按镜分治：旁白/无词镜闭声出演、TTS 上主轨，
  对白镜由模型发声、锚定照常附发——旁白镜若按开口稿生成过，须置 retake 重生，
  否则 assemble 拒合成（双人声无可交付形态）；对白镜夹带旁白句由
  `burn_mixed_narration` 点名。提示词会把台词逐字发给模型，但这不是确定性保证：
  模型声源的镜未核对前，成片人声与字幕只算待核对（lint `native_voice_unverified`）；
  verify 的 ASR 人声文字核对（装 faster-whisper）是核对出口。
- 说话人音色缺省定制：立项时每个角色 `character add --voice-prompt "<声线描述>"`、旁白
  `voice custom --narrator --prompt "<声线描述>" --adopt 1`，引擎按描述造声并立档；声线描述按
  六槽位写 40~80 字（性别年龄段/音区明暗/音质质感/语速节奏/口音吐字/气质，不写情绪词，
  范例见 `.claude/skills/kinema/references/voice-casting.md`）；`tts`/`gen-video`/
  `score`/`run` 在花钱前点名没有音色引用的说话人，不替用户挑模版。官方模版音色只走显式的
  `voice audition` → `voice use`（或 `--voice <别名>`）。
- 角色缺省气色健康、神态有精神：外貌字段不写黑眼圈/眼袋/血丝/憔悴等疲态，题材也不推导出
  疲态；只有用户明确要求时才写，并登记进 `visual_requirements`（lint `character_fatigue_look`
  与 `project refs` 出图闸据此放行）。表演缺省也不叹气、不深呼吸、不流泪：引擎把三者压进
  每镜的负面地板，只有本镜正文明写时才放开（否定写法不算）。
- 旁白是语态：`lead` 解说驱动、`sparse` 剧情驱动、`none` 氛围驱动，不是每镜必填项。
- 曲库 BGM 三档互斥：kenburns/dubbed 恒有，scored 与 native 缺省无、各由 `scored_bgm` /
  `native_bgm` 显式加铺；`native_bgm` 与配音混烧互斥（BGM 母线已被原生音背景床占住）。
  `assemble` 前的闸只在「曲库为空」或「native 从没表过态」时发问，非交互一律不替用户决定。
- 字幕后置合成，不让图像或视频模型画字幕。字号缺省按画布三档分治（竖屏 80 / 横屏 66 /
  方屏 58）：16:9 画布 1920 宽而另两档都是 1080 宽，横屏那一档是画布宽度修正、对画风
  profile 字号是硬覆盖；章节 `subtitle` 块写了 size 即明确表态，三种画布都不动它。
- 转场镜只由用户主动插入；缺省一镜一片、镜间硬切是既定形态，Agent 不代加也不主动提议
  （唯一例外是衔接章的孤岛接缝，由引擎自动落 `transition.auto="island"`）。
- 长文本任务在用户批准出图前只做文字、结构和 lint，不触发付费视觉生成。
- 下一步需要用户在 Studio 试听、点选、上传或审阅时，先按 `.claude/skills/kinema/references/studio-handoff.md` 自动启动或复用控制台，
  再提示具体操作；纯聊天确认不启动。

完整生产节点、提示词纪律和判例在 `.claude/skills/kinema/references/production-playbook.md`。

## 5. 配置、密钥与存储

配置文件的唯一说明见 [`config/README.md`](config/README.md)。`config/models.yaml` 是模型和 profile 真源：

```text
profile → 可选 profile.provider → defaults.providers → providers.<alias>.impl → adapter
```

配置覆盖必须挂在 `ConfigStore.load` 的统一出口；不要只改 `ModelRouter`，否则直读 `store.data` 的路径会分裂。
`profiles`、`canvas`、`voices` 不进入本机覆盖层。

密钥优先级：环境变量 > `config/secrets.local.json` > `config/secrets.yaml`。密钥文件都必须 gitignore，
不得打印、提交、入库、下发到前端或写入 Prompt/Skill/project.json。

存储默认 local；MySQL 模式下 JSON 是工作副本，库与文件按既有“新者赢”协调。Schema 真源在
`engine/kinema/storage/mysql.py` 的 `_SCHEMA`，加列必须同步 `_MIGRATE_COLUMNS` 并重新导出 SQL。
项目删除只有逻辑删除语义，所有 stage 入口统一受 `Workspace.get_project` 总闸约束。

## 6. 开发与验证纪律

- 修改前先读同模块实现、测试和对应详情；优先解决根因，不为未来假想需求加兼容层、重试或兜底。
- 新行为只有一个真源。provider 请求体、提示词装配、状态机、版本、配置和字段语义不得在 CLI、Studio、
  Skill 和前端各写一份。
- 引擎函数返回结构化结果或抛明确领域错误；不要吞异常后假装成功。
- mock 与合成链保持零 Python 硬依赖；真实 provider 依赖通过 extras 安装。
- 所有测试离线、确定性、零付费；不得调用真实生成 API。
- 碰 `Workspace` 或 storage 的测试必须启用 `tests/support.py` 的 `LocalBackendEnv`
  （setUp/tearDown 组合调用 enable/restore），防止开发机环境连接真库。
- `project/` 是 gitignored 用户数据，不能当测试 fixture 或断言真源。
- 守卫只钉行为、契约、竞态和资源，不为 README 数字、按钮文案或一次性防复活断言加脆弱测试。
- 修改引擎后执行：

  ```bash
  cd engine
  python3 -m unittest discover -s tests
  ```

未执行编译、测试、联调或真实 provider 验证时，交付说明必须明确标记“未执行”或“未验证”。

## 7. 详情层阅读地图

只在改到对应模块时读取：

- 测试与守卫：[`docs/agents/guard-map.md`](docs/agents/guard-map.md)
- 并发与写回：[`docs/agents/concurrency.md`](docs/agents/concurrency.md)
- 资产版本：[`docs/agents/asset-versioning.md`](docs/agents/asset-versioning.md)
- 混音：[`docs/agents/mixdown.md`](docs/agents/mixdown.md)
- 口型精修：[`docs/agents/lipsync.md`](docs/agents/lipsync.md)
- 原生人声：[`docs/agents/native-voiceover.md`](docs/agents/native-voiceover.md)
- 音色锚定：[`docs/agents/voice-anchor.md`](docs/agents/voice-anchor.md)
- 音色库：[`docs/agents/voice-bank.md`](docs/agents/voice-bank.md)
- 角色字段：[`docs/agents/character-fields.md`](docs/agents/character-fields.md)
- 设定垫图：[`docs/agents/moodboard.md`](docs/agents/moodboard.md)
- 写实人物合规：[`docs/agents/photoreal-face.md`](docs/agents/photoreal-face.md)
- 一致性：[`docs/agents/consistency.md`](docs/agents/consistency.md)
- lint：[`docs/agents/lint.md`](docs/agents/lint.md)
- 成片验证：[`docs/agents/verify.md`](docs/agents/verify.md)
- 封面：[`docs/agents/cover.md`](docs/agents/cover.md)
- 转场：[`docs/agents/transitions.md`](docs/agents/transitions.md)
- 尾帧接力：[`docs/agents/tail-relay.md`](docs/agents/tail-relay.md)
- 首帧锚定：[`docs/agents/anchor-frame.md`](docs/agents/anchor-frame.md)
- 参考片研究：[`docs/agents/study.md`](docs/agents/study.md)
- 3D 预演：[`docs/agents/previz-v2v.md`](docs/agents/previz-v2v.md)
- 简笔预演：[`docs/agents/sketchboard.md`](docs/agents/sketchboard.md)
- 决策日志：[`docs/agents/decisions.md`](docs/agents/decisions.md)
- Studio 前端：[`docs/agents/studio-frontend.md`](docs/agents/studio-frontend.md)
- 舞台控制台：[`docs/agents/stage-console.md`](docs/agents/stage-console.md)
- 指令弹层：[`docs/agents/directive-dialog.md`](docs/agents/directive-dialog.md)
- 3D 导演台：[`docs/agents/director-stage-ui.md`](docs/agents/director-stage-ui.md)
- Skill 发现：[`docs/agents/skill-discovery.md`](docs/agents/skill-discovery.md)
- 长篇纪律：[`docs/agents/novel-discipline.md`](docs/agents/novel-discipline.md)

## 8. 文档边界

- `AGENTS.md` 只写稳定结论与导航，保持在 `agent/manifest.json` 声明的上下文预算内。
- `DEVELOP.md` 放模块地图、CLI 全表和二开路径；`SETUP.md` 放安装、首跑与就绪判定。
- 配置只在 `config/README.md` 解释；Skill 创作方法只在 `.claude/skills/` 维护。
- 控制平面编译管线只在 `agent/README.md` 解释；`docs/` 布局只在 `docs/README.md` 解释（均英文默认、`README.zh-CN.md` 对照）。
- 快照型数字、价格、profile 清单和 Skill 数量由命令或生成物给出，不手抄进 Agent Kernel。
- 文档不能覆盖代码事实；发现分叉时修正单一真源并补行为守卫。
