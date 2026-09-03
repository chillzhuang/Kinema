<!-- 由 tools/agent_assets.py 生成；请勿手改。 -->

# Kinema Skill Catalog

本索引由 `agent/manifest.json`、`agent/contracts.json` 与 `.claude/skills/` 正文确定性生成。
宿主直接发现 `.claude/skills/`；Codex 等兼容 `.agents/skills` 的工具读取同一目录别名。

选择优先级：项目绑定 > 显式 Skill > 显式 profile > `kinema`。

## 通用工作流

| Skill | 状态 | 用途 | Profile |
|---|---|---|---|
| [`/kinema`](../../.claude/skills/kinema/SKILL.md) | `stable` | 通用主题到成片工作流 | `narration` |

## 画风路由

| Skill | 状态 | 用途 | Profile |
|---|---|---|---|
| [`/kn-anime`](../../.claude/skills/kn-anime/SKILL.md) | `stable` | 十五种 2D 动画传统 | `anime`, `anime_cel`, `anime_80s`, `ghibli`, `shinkai`, `pixar`, `disney3d`, `anime_xianxia`, `anime_mecha`, `anime_jojo`, `anime_american`, `anime_pixel`, `anime_doodle`, `anime_fairytale`, `anime_ink` |
| [`/kn-anime3d`](../../.claude/skills/kn-anime3d/SKILL.md) | `stable` | 国漫年番、写实 CG、数字人、虚拟制片与黑色人像 | `anime3d`, `anime_ldr`, `photoreal3d`, `virtual_production`, `cg_noir` |
| [`/kn-cyberpunk`](../../.claude/skills/kn-cyberpunk/SKILL.md) | `stable` | 写实 CG 或 2D 霓虹未来都市 | `cyberpunk`, `cyberpunk_2d` |
| [`/kn-game`](../../.claude/skills/kn-game/SKILL.md) | `stable` | 十一种游戏画面与叙事机制 | `hd2d`, `gba`, `snes`, `dark_fantasy`, `game_ps1`, `game_voxel`, `game_isometric`, `game_celshade`, `game_vn`, `game_arcade`, `game_sim` |
| [`/kn-clay`](../../.claude/skills/kn-clay/SKILL.md) | `stable` | 粘土、高达、手办与积木定格 | `clay`, `gunpla`, `figure`, `brick` |
| [`/kn-book`](../../.claude/skills/kn-book/SKILL.md) | `stable` | 说书、荐书、拆书与书单内容 | `book` |
| [`/kn-explainer`](../../.claude/skills/kn-explainer/SKILL.md) | `stable` | 知识解说与信息图叙事 | `explainer` |
| [`/kn-quote`](../../.claude/skills/kn-quote/SKILL.md) | `stable` | 语录、励志与治愈卡片 | `quote` |
| [`/kn-ranking`](../../.claude/skills/kn-ranking/SKILL.md) | `stable` | 榜单、盘点与 TopN 倒数 | `ranking` |
| [`/kn-miniature`](../../.claude/skills/kn-miniature/SKILL.md) | `stable` | 移轴摄影与微观世界 | `miniature` |
| [`/kn-storybook`](../../.claude/skills/kn-storybook/SKILL.md) | `stable` | 绘本、睡前故事与寓言 | `storybook` |

## 生产策略覆盖

| Skill | 状态 | 用途 | Profile |
|---|---|---|---|
| [`/kn-showcase`](../../.claude/skills/kn-showcase/SKILL.md) | `stable` | 制作素材复用型解说短片。用户要产品讲解、功能演示、方案宣讲、房产汽车数码展示、PPT 式视频或用少量图片讲清内容时使用；以 explainer 为默认 profile，资产跨分镜复用降低生图成本。 | — |

## 专项能力

| Skill | 状态 | 用途 | Profile |
|---|---|---|---|
| [`/kinema-sketchboard`](../../.claude/skills/kinema-sketchboard/SKILL.md) | `stable` | 为单镜设计简笔分镜预演板和逐秒 beats。用户要草图分镜、九宫格分镜、逐秒运动脚本，或在生成视频前控制动作节奏时使用；与 3D previz 逐镜互斥。 | — |
| [`/kn-audio`](../../.claude/skills/kn-audio/SKILL.md) | `stable` | 设计 seed-audio-1.0 的声线描述和整章音频剧本。用户要定制音色、配乐、音效、逐句演绎，或把人声、音乐和音效一次生成并混好时使用。 | — |

## 项目与长篇

| Skill | 状态 | 用途 | Profile |
|---|---|---|---|
| [`/kinema-novel`](../../.claude/skills/kinema-novel/SKILL.md) | `stable` | 在 Kinema 工作区创作、续写或接手长篇小说。用户提到网文连载、人设一致、文风统一、伏笔回收、卷纲、十章批次或七门复核时使用；Agent 负责写作，引擎负责登记、取料和确定性体检。 | — |
| [`/kinema-project`](../../.claude/skills/kinema-project/SKILL.md) | `stable` | 管理多集或系列内容的项目、章节、角色、道具、场景与总体设计。用户要先规划再制作、创建系列、继续既有章节或查看项目状态时使用。 | — |

## 系统能力

| Skill | 状态 | 用途 | Profile |
|---|---|---|---|
| [`/kinema-setup`](../../.claude/skills/kinema-setup/SKILL.md) | `stable` | 配置或诊断 Kinema 运行环境。第一次使用、换机、重装、ffmpeg/Python/依赖缺失、密钥、MySQL、OSS、BGM 或 doctor 报错时使用；只配置与验收，不执行内容生产。 | — |

## 维护

```bash
python3 tools/agent_assets.py compile
python3 tools/agent_assets.py check
cd engine && python3 -m kinema agent doctor --json
```

Skill 正文与 references 直接在 `.claude/skills/` 修改；名称、描述、权限等元数据只改
`agent/manifest.json`。frontmatter、`skill.json`、本索引、运行时 catalog 与 contracts
由编译器维护，改完必须重新 compile。
