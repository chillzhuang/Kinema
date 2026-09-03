# 工作区 / 项目管理（强规划层）

在"单条视频"之上加两层组织，把工具升级为**制作管理系统**。文档式 CRUD，
持久化后端由 `config/storage.yaml` 决定：**local**（默认，JSON 即数据库）或
**mysql**（库为持久层与恢复源，本地 JSON 是工作副本：保存双写、读取时文件较新则上行
入库、文件缺失则从库恢复；媒体只存路径）。管理命令：`kinema db status|init|sync|pull|schema`。

## 三层结构

```
Workspace 工作区（默认仓库根 project/，或 KINEMA_WORKSPACE / --workspace）
└── Project 项目/系列  project/<id>/project.json
    ├── design       总体设计：logline/synopsis/world/tone/palette/style_notes
    ├── characters   角色预设：name / voice_prompt / appearance / role …（全字段见 project.schema.json）
    └── chapters     章节索引 → chapters/<cid>.json（一条视频）
```

- **工作区路径不变量**：源码仓库默认始终是仓库根 `project/`。从仓库根或 `engine/`
  启动，结果必须相同；`--workspace` / `KINEMA_WORKSPACE` 传仓库根或 `engine/` 时，
  引擎确定性归一到仓库根 `project/`；显式传入任一可识别源码检出的历史
  `engine/project/` 也会归一到该检出的根 `project/`。自定义临时工作区仍可直接传数据目录。
- **工作区发现顺序**：显式 `--workspace` > 环境变量 `KINEMA_WORKSPACE` > 源码仓库根
  `project/` > 非源码环境下从 cwd 向上找已存在的 `project/` > `./project`（见
  `engine/kinema/workspace.py`）。
- **后端不改变路径**：`local` 把 JSON 作为真源，`mysql` 把数据库作为持久层并保留
  JSON 工作副本，但二者共用同一个工作区数据目录，不能一边落仓库根、一边落 `engine/`。
- **Project（项目/系列）**：一个 IP/系列，含总体设计 + 角色预设 + 章节列表。
- **Chapter（章节）**：一条视频，即引擎能渲染的 project.json（shots 结构）。
- **逻辑删除不移目录**：`project rm` 只写 `is_deleted=1` 并进入 Studio 回收站，物理项目目录
  会保留以便恢复；它仍是项目目录，不是跑到项目库根的章节。章节只存在于
  `project/<pid>/chapters/`。
- **章节继承项目**：新建章节自动带上 profile、skill、比例、画风快照、艺术指导与配音表现力基调、
  字幕语言、角色音色表与档案库、旁白锁、角色/道具/场景、场景基准图、角色设定块、色板、seed → 全系列一致。

## project.json（系列）字段

| 字段 | 说明 |
|---|---|
| `id` / `title` / `theme` | 标识与主题 |
| `profile` / `platform` / `aspect` | 默认风格档/平台/比例（章节继承） |
| `status` | active / archived |
| `created_at` / `updated_at` | 时间戳 |
| `design` | 总体设计（logline/synopsis/world/tone/palette/style_notes） |
| `characters[]` | 角色预设：`name`、`voice_prompt`（声线描述，缺省选角路径）或 `voice`（模版音色别名，显式例外）、`appearance`、`role`，以及系列级常量字段；全字段见 `project.schema.json` 与 `docs/agents/character-fields.md` |
| `chapters[]` | 章节索引 {id, title, order, created_at} |

章节文件 = 视频 project.json（见 `project.schema.json`），额外带 `chapter: {project, id, title}`。
章节状态动态计算：`draft`（无分镜）/ `scripted`（有分镜未渲染）/ `rendered`（有成片）。

## CLI（JSON 即数据库 CRUD）

```bash
# 项目
kinema project new --title "旅人与灯塔" --id lanterns --profile hd2d
kinema project list | show <id> | set <id> --logline ... --world ... | rm <id> [--archive]
# 角色
kinema character add <项目> --name 洛 --voice-prompt "<声线描述>" --role 主角 --appearance "..."
kinema character list <项目> | rm <项目> --name 洛
# 章节
kinema chapter new <项目> --title "相遇"
kinema chapter list <项目> | show <项目> <章节> | rm <项目> <章节>
# 渲染某章节（等价于对其视频文件跑流水线）
kinema run --chapter <项目>/<章节> [--native | --dubbed | --kenburns]   # 不写按内容定档
# 大屏可视化（项目仪表盘 + 成片画廊）
kinema studio
```

## Studio 制作管理平台

`kinema studio` 启动本地 web（hash 路由 SPA；外壳缺省顶栏、可切左栏，顶栏形态下项目树是浮层），核心视图：

- **总览**：全局统计（项目/章节/成片/分镜/总时长/云成本）+ 最近成片 + 项目卡 +
  风格档（按 skill 分组，画风卡点击看详情与对话示例）。
- **项目详情**：章节表（状态/渲染模式/分镜/时长/成本/比例）、平台规格、角色设定（灯箱+音色试听）、
  旁白选角、道具/武器设定、取景地设定图与俯视图、固定场景、总体设计、导出与危险区；
  接书项目另有创作/源文本/拆书/分集/设定/剧本工作台。
- **章节制作台**：五道关口（脚本→分镜图→配音→动态片段→成片）、时间线与资产血缘、分镜脚本、
  3D 导演台与简笔分镜（kenburns 下折叠为运动预演）、音频剧本、放映与后期（特效/水印/构建/自审）、
  分镜卡（图/台词/配音/片段/提示词）、剧本与声音、章节资产、成本与一致性、交付。区块顺序即制作阶段顺序，
  详见 `docs/agents/studio-frontend.md` §9。
- **片库**（`#/library`）：全部成片，按项目与渲染模式过滤，放映厅模态可跳回章节。
- 另有**项目列表 / 待审队列 / 看板 / 成本 / 指令集 / 模型配置中心 / SKILL 大屏**
  （`#/projects` · `#/queue` · `#/board` · `#/cost` · `#/guide` · `#/model` · `#/skill`），
  剧本工作台 `#/project/<pid>/script`，3D 导演台 `#/stage/<pid>/<cid>`。
- **实时模式**：章节制作台 3s 轮询，渲染中边生成边看。

API 以 `engine/kinema/studio/server.py` 的 `/api/*` 路由表为准（七十余条），常用的几条：
`GET /api/overview`、`/api/projects`、`/api/project?id=`、`/api/chapter?project=&id=`、
`/api/library`、`/media?path=`（Range 流）、`/poster?path=`（缩略图缓存）。
实现分层见 `engine/kinema/studio/`（scanner 数据层 + server HTTP 层 + jobs 异步 + actions 写操作）
与 `studio_app/`（前端），详见 `docs/agents/studio-frontend.md`。
