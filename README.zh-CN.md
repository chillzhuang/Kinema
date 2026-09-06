<div align="center">

<h1>Kinema</h1>

**给一个主题，出一条成片。**

Kinema 把检索、文案、分镜、角色设定、生图、配音、字幕、特效和合成串成一条完整流程。你给出主题，它负责把项目推进到成片。

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL%20v3-2E8BFF?logo=gnu&logoColor=white" alt="License: AGPL v3"></a>
  <img src="https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FFmpeg-%E5%94%AF%E4%B8%80%E7%A1%AC%E4%BE%9D%E8%B5%96-007808?logo=ffmpeg&logoColor=white" alt="FFmpeg 唯一硬依赖">
  <img src="https://img.shields.io/badge/%E7%A1%AC%E4%BB%B6-%E7%BA%AF%20CPU-E0A33E" alt="纯 CPU，无需显卡">
</p>
<p>
  <a href=".claude/skills/"><img src="https://img.shields.io/badge/Claude%20Code-skill-D97757?logo=claude&logoColor=white" alt="Claude Code skill"></a>
  <a href="AGENTS.md"><img src="https://img.shields.io/badge/AGENTS.md-Codex%20%C2%B7%20Cursor%20%C2%B7%20Copilot-1F2430?logo=markdown&logoColor=white" alt="AGENTS.md 跨工具原生读取"></a>
  <img src="https://img.shields.io/badge/%E7%94%BB%E9%A3%8E-40%2B%20%E4%B8%AA%E6%A1%A3-9257FF" alt="40+ 个画风档">
  <img src="https://img.shields.io/badge/tests-2000%2B%20passing-4C9A2A" alt="守卫用例全绿">
</p>

[English](README.md) · [简体中文](README.zh-CN.md)


</div>

---

传统 AI 视频创作往往要在多个工具之间来回切换：脚本、分镜、生图、配音、视频和剪辑各自分散，
角色与场景设定也容易遗落在不同会话里。改动一处，后续产物常常要跟着重做。**Kinema** 把这些环节接进同一条制作管线，
从立项一路推进到可持续制作的系列内容；资产可以复用、追踪和回滚。

- ✍️ **长篇小说创作** —— 十章为一批，每批完成后自动经过**七项复核**（设定一致性 · 人设 · 情节连贯 ·
  AI 腔 · 文风 · 伏笔 · 节奏）；角色口吻、道具、卷纲和伏笔账本也会随剧情更新。
  可以从零开始原创，也可以继续一部尚未完成的作品。
- 🎬 **小说改剧本，剧本拆分镜** —— 一章就是一集，每个镜头配好中英文提示词；
  开拍前先跑一遍零成本静态体检，运镜雷同、景别单调、AI 腔都会被标出来。
- 🎥 **3D 导演台** —— 正式生成前先用灰模完成走位、动作和镜头调度，
  **30+ 个运镜预设**覆盖十余种标志性镜头语言，并可渲染为可复现的预演片。
- ✏️ **简笔分镜板** —— 把一个镜头按时间切成几段动作，画成铅笔草图，再附一条逐秒时间轴。
  视频模型拿到的就不是一句笼统描述，而是每一秒该发生什么。
- 🎞️ **深度捕捉** —— 一段实拍片在本机 CPU 上提取成人物深度浮雕加骨骼的控制视频，框出
  4～15 秒绑到镜头，视频模型照着它的运动演，外观来自你的设定图。源片音轨可以作本章配乐，
  成片与源片的偏移由引擎量出，相关够高时自动补偿。可选感知栈，安装见 [`SETUP.md`](SETUP.md)。
- 🎭 **角色设定表** —— 三区两视设定图、道具三视图、取景地主视觉图，按出场逐镜自动挂载。
  一张脸能在几十个镜头里稳住，靠的就是这套。
- 🎨 **常用画风预设** —— 赛博朋克 · 新海诚 · 吉卜力 · 国漫仙侠 · 3D国漫 · 皮克斯 · 迪士尼3D ·
  写实CG · 美漫 · 水墨 · 粘土定格 · 微缩世界 · 像素 · 虚拟制片，切换画风档即可统一调整整套视觉语言。

产物逐阶段落盘，经你确认才进入下一步；云端 AI 与本地部署 AI 可自由配置；**引擎在你的机器上运行，密钥由你保管，成片归你所有。**

## 🎬 系统界面

Kinema 把执行交给引擎，把创作判断和验收留给你。因此它提供的是完整制作台，而不只是进度显示。
下面的界面顺序也对应实际制作流程。

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/project.png" alt="项目页" width="100%">
      <p align="center"><em><b>项目页</b> — 一部剧一页看全：原著、每一集的渲染模式／镜数／时长／实际花费，下面接着角色设定</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/character-sheets.png" alt="角色设定" width="100%">
      <p align="center"><em><b>角色设定</b> — 每个角色一张三区设定图：正面肖像特写与正/背面全身像，锁定的音色就在卡上试听</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/prop-sheets.png" alt="道具设定" width="100%">
      <p align="center"><em><b>道具设定</b> — 每件道具一张结构三视图，配材质与光线说明。同一件东西，在每个镜头里长得一样</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/location-sheets.png" alt="场景设定" width="100%">
      <p align="center"><em><b>场景设定</b> — 每个场景一张主视觉，配材质与光线说明。同一个地方，在每个镜头里长得一样</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/script-workbench.png" alt="剧本工作台" width="100%">
      <p align="center"><em><b>剧本工作台</b> — 先有小说：350 章、130.9 万字，左目录右正文，扩写／拆书／图书／问书指令一键取用</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/character-graph.png" alt="人物关系图谱" width="100%">
      <p align="center"><em><b>人物关系图谱</b> — 角色、阵营、地点、器物和世界观连成一张图，每条关系都标明类型（亲缘／盟友／师承／敌对／情感／归属／竞争）。人物与设定的连贯性可以直接查询，不必全靠记忆</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/chapter-workbench.png" alt="章节制作台" width="100%">
      <p align="center"><em><b>章节制作台</b> — 顶上五道关口（脚本 → 分镜图 → 配音 → 动态片段 → 成片）
      ，中间时间线，下面资产血缘：改一张设定图，下游镜头当场标为过期</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/shot-list.png" alt="分镜脚本" width="100%">
      <p align="center"><em><b>分镜脚本</b> — 一镜一行：景别、运镜、时长、台词、情绪，以及完整的画面与运动提示词</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/director-3d.png" alt="3D 导演台" width="100%">
      <p align="center"><em><b>3D 导演台</b> — 用灰模完成走位、动作和镜头调度，再渲染为可复现的预演片；30+ 个运镜预设覆盖十余种标志性镜头语言</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/sketchboard.png" alt="简笔分镜板" width="100%">
      <p align="center"><em><b>简笔分镜板</b> — 一个镜头切成几拍铅笔草图，五色标注运动轨迹／摄影机运动／取景／灯光／声音，
      配一条逐秒说明交给视频模型</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/depth-capture-bind.png" alt="深度捕捉 · 绑定分镜" width="100%">
      <p align="center"><em><b>深度捕捉 · 绑定分镜</b> — 上传一段实拍视频，本机提取人物深度浮雕与骨骼生成控制视频；在缩略条上框出 4～15 秒绑到镜头，
      段长即成片长度，源片与深度同屏预览</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/depth-capture-compare.png" alt="深度捕捉 · 三列对照" width="100%">
      <p align="center"><em><b>深度捕捉 · 三列对照</b> — 源片、控制视频与生成成片同一区间逐帧并排，声音取源片；引擎量出成片相对控制段的偏移，
      相关够高时配乐随之平移——运动来自实拍，外观来自设定图</em></p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/screenshots/audioscript.png" alt="音频剧本" width="100%">
      <p align="center"><em><b>音频剧本</b> — 把整章声音写成一份可执行脚本：声线、台词和秒级时间轴按段编排，参考音逐条对位；
      可从分镜一键起草，再由生成式音频模型输出整轨</em></p>
    </td>
    <td width="50%" valign="top">
      <img src="assets/screenshots/storyboard.png" alt="分镜卡与放映" width="100%">
      <p align="center"><em><b>分镜卡与放映</b> — 每镜的图、配音、动态片段各自独立审阅，旁边就是拼好的成片；
      点一下复制一条交给 agent 的改镜指令</em></p>
    </td>
  </tr>
</table>

## 🚀 快速开始

唯一的硬依赖是 **FFmpeg**。引擎内核零 Python 依赖，mock 链路完全离线。

```bash
brew install ffmpeg       # macOS · Debian: sudo apt install ffmpeg

cd engine
python3 -m kinema doctor  # 自检 ffmpeg / 配置 / providers / 存储后端

# 离线端到端、零成本：占位图与合成音跑通真实管线的每一段
cp examples/sample_project.json /tmp/demo.json
python3 -m kinema run --project /tmp/demo.json --mock

python3 -m kinema studio  # 打开制作台 → http://127.0.0.1:8787
```

## 🎞️ 一集怎么做出来

在编码 agent 中调用对应的画风能力包，就可以开始制作：

```
/kn-cyberpunk 义体佣兵突入财阀塔 47 层：一条走廊，十二个守卫，战术系统损坏度 80%
```

实际执行流程如下。**每道关口都会先将产物落盘并等待确认**；只有显式执行 `kinema run` 才会连续跑完整条管线：

```bash
python3 -m kinema project new --title "剑与雨" --id bladerain --profile cyberpunk
python3 -m kinema chapter new bladerain --title "第四十七层"  # → ch01
#   ↑ agent 在这里接手：写文案、拆分镜、写双语提示词

python3 -m kinema project refs bladerain                       # 设定集 —— 一致性的根基
python3 -m kinema lint      --chapter bladerain/ch01           # 零成本静态体检：运镜雷同、景别单调、反 slop
python3 -m kinema gen-image --chapter bladerain/ch01 --only 1  # 只出首镜，先把风格定死再花钱
python3 -m kinema tts       --chapter bladerain/ch01           # 配音（按角色卡声线描述定制的音色）
python3 -m kinema animatic  --chapter bladerain/ch01           # 全片 Ken Burns 样片过节奏审，零视频成本

python3 -m kinema gen-video --chapter bladerain/ch01 --dry-run        # 花钱前逐镜报价
python3 -m kinema gen-video --chapter bladerain/ch01 --approved-only  # 只烧点过头的镜
python3 -m kinema assemble  --chapter bladerain/ch01                  # 动态版成片
#   渲染档按内容定（有对白 → native，全旁白 → dubbed）；--native / --dubbed / --kenburns 可为本次覆盖
```

## 🧭 为什么是 Kinema

| 主张 | 依据 |
|---|---|
| 💰 **生成前先算清成本** | `--dry-run` 逐镜报价；`done` 的镜被锁定，`--force` 也不覆盖；整批预估超过 `budget` 时，事前闸不会发出任何请求；预估与实际花费分别记录。 |
| 🎭 **用资产与血缘管理一致性** | 角色三区两视设定图、道具三视图和取景地主视觉图按出场逐镜挂载；固定 seed；设定图一旦变化，资产血缘会立即标记受影响的下游镜头。角色身份先于运动生成确定。 |
| 🏭 **工作室级审阅流程** | 五态审阅 × 版本栈 × 像素锚定评论 × 宫格候选选优 × 局部框选改造 × 跨镜批量编辑。agent 提出方案，你负责通过、修改或回滚。 |
| 💻 **普通笔记本就够** | 重活全在云端 API，本地只做 FFmpeg 合成、字幕与运镜——**纯 CPU，无需显卡**。 |
| 🔌 **换模型不改制作管线** | 代码绑定图像、视频、语音和音乐能力，而不是具体厂商。在 `models.yaml` 中增加模型别名或切换默认 provider，所有画风档沿用同一套路由。 |
| 🤖 **支持多种编码 agent** | `AGENTS.md` 为 Claude Code、Codex、Cursor、Copilot、Windsurf、Aider 和 Zed 提供统一工程规范；各工具的专属文件只保留入口，不复制规则。 |

## 🎛️ 三种渲染模式

渲染模式按章设定，一章一个入口。不写时引擎按内容定档：有对白上镜的章走 **native**，
全旁白的解说章走 **dubbed**，用音频剧本整轨的章走 native。Ken Burns 不作缺省，
要零成本静图版就显式加 `--kenburns`。

| 模式 | 画面 | 声音 | 视频成本 |
|---|---|---|---|
| **kenburns** | 静图缓动运镜 | Kinema 配音 + BGM | **零** |
| **dubbed** | Seedance 图生视频，闭唇出片，表演跟随配音节奏 | Kinema 配音 + BGM | 按秒计费 |
| **native** | Seedance 原生音画；每位说话人的选角音色作参考音随请求附发，口型、台词、嗓音出自同一次生成 | 模型自声上主轨；旁白镜要混烧 TTS 旁白时按章打开 `native_voiceover`（单次可用 `assemble --burn-voice`） | 按秒计费 |

## 🎨 模型与画风

模型和画风统一配置在 **`config/models.yaml`**，并内置十余个 provider 别名：

| 能力 | 主力 | 备选 |
|---|---|---|
| 图像 | Seedream | Nano Banana · 通义万相 · MiniMax |
| 视频 | Seedance 2.0 mini / 2.5 | Veo · MiniMax H3 |
| 语音 | seed-audio-1.0 按声线描述定制音色（缺省）· seed-tts-2.0 模版音色 | MiniMax |
| 音乐 | ElevenLabs | MiniMax · 内置 CC0 曲库（无密钥自动降级） |

此外还有 **40+ 个画风档**、**10+ 个特效**、**零成本转场**（配 CC0 音效）、
**30+ 个运镜预设**（覆盖十余种标志性镜头语言），以及随画风走的字幕版式。

## 📚 能力包

Kinema 的创作流程整理为一组 **能力包**，统一放在 [`.claude/skills/`](.claude/skills/)；
内容覆盖故事拆镜、不同画风的提示词写法和配音表演指导。

- **Claude Code** 自动发现，直接斜杠调用：`/kn-anime`、`/kn-explainer`、`/kinema-novel`…
- **其他 agent** 走 [`docs/skills/INDEX.md`](docs/skills/INDEX.md)，同一批内容的中立索引。

`kinema` 定义通用制作流程，其他能力包在此基础上扩展。

## 🗂️ 工程结构

```text
Kinema/
├── .claude/skills/          # 能力包唯一实体 —— 单源原地编辑（frontmatter 由编译器维护）
├── .agents/skills           # → .claude/skills 的别名链接（Codex · Gemini CLI · Amp · OpenCode）
├── .cursor/ · .github/      # Cursor 与 Copilot 的薄指针，只指回 AGENTS.md，不承载内容
├── agent/                   # 指挥层控制平面单源（编译管线说明见 agent/README.md）
│   ├── manifest.json        # skill 注册表：名称 · 描述 · 类型 · 状态 · 权限（元数据只改这里）
│   ├── contracts.json       # 机器契约源：PromptSpec / ChapterPlan
│   └── adapters/            # 宿主入口模板 → CLAUDE.md · .cursor/rules · copilot-instructions
├── assets/                  # 仓库资产集合
├── config/                  # models 模型与画风 · voices 音色 · audio · templates · storage · branding
├── docs/
│   ├── agents/              # 工程指南的详情层 —— 由 AGENTS.md 索引，动到对应模块前才读
│   ├── kinema/              # 架构总览 design.md · 流程走读 video-pipeline.md · 数据契约 project.schema.json · 厂商矩阵
│   ├── skills/              # 能力包工具中立索引 INDEX.md（生成物，勿手改）
│   └── sql/                 # MySQL 建库建表脚本（`db schema` 的生成物，勿手改）
├── engine/
│   ├── kinema/              # 100+ 个 Python 模块 · 执行引擎（内部没有 LLM）
│   │   ├── assets/          # 内置字体 · 设定图与简笔板版式样板
│   │   ├── control/         # 深度捕捉：实拍片 → 控制视频 → 绑定 · 对照片 · 源片配乐 · 对拍
│   │   ├── pipeline/        # 生图 · 配音 · 字幕 · 运镜 · 转场 · 混音 · 合成
│   │   ├── providers/       # 厂商适配器，按「能力 × 厂商」一文件一个
│   │   ├── storage/         # 本地 JSON ⇄ MySQL ⇄ 对象存储
│   │   ├── studio/          # 制作台后端（scanner · server · jobs · actions）
│   │   ├── studio_app/      # 制作台前端，原生 ESM 免构建（app/ 制作台 · director/ 3D 导演台）
│   │   └── cli.py           # 50+ 个子命令 · 命令行为以此实现为准
│   ├── examples/            # 可直接跑的样例 project.json
│   └── tests/               # 2000+ 个离线守卫用例
├── music/                   # 内置 CC0 曲库与音效库（媒体不入 git，`python music/download.py` 重建）
├── tools/                   # agent_assets.py 控制平面编译器 · agents_alias.py 修 Windows 别名链接
├── project/                 # 工作区产物落点 —— 你的项目数据在这里（gitignored）
├── AGENTS.md · CLAUDE.md    # 工程指南（所有编码 agent 的唯一真源）· Claude Code 入口指针
├── SETUP.md · DEVELOP.md    # 首跑与就绪判定 · 全景架构与二开配方
└── LICENSE                  # GNU AGPL v3
```

## 📄 文档

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Agent Kernel**——架构边界、不可违背的结论与按模块阅读导航。所有 agent 始终加载 |
| [`DEVELOP.md`](DEVELOP.md) | **开发手册**——模块地图、完整 CLI 参考和二次开发说明，并由测试校验其与代码结构一致 |
| [`SETUP.md`](SETUP.md) | 首跑安装与就绪判定 |
| [`docs/kinema/design.md`](docs/kinema/design.md) | **架构总览**——三层/管线/一致性/声音/成本一页看全，附立项取舍存档 |
| [`docs/kinema/video-pipeline.md`](docs/kinema/video-pipeline.md) | **流程走读**——文档/状态/并发模型，再按数据流逐步写清每一步的判据、产物、闸与写回 |
| [`docs/skills/INDEX.md`](docs/skills/INDEX.md) | 能力包的中立索引 |
| [`config/README.md`](config/README.md) | 全部配置文件的字段级说明与换模型手册 |
| [`docs/kinema/project.schema.json`](docs/kinema/project.schema.json) | `project.json` 数据契约 |
| [`docs/kinema/providers.md`](docs/kinema/providers.md) | 各厂商能力、计费与限制 |
| [`engine/kinema/cli.py`](engine/kinema/cli.py) | 文档与代码打架时，**命令行为以它为准** |

## 📜 致谢

- **[FFmpeg](https://ffmpeg.org/)**——唯一的硬依赖，本地合成、运镜、字幕烧录与响度处理全靠它。
- **[Three.js](https://threejs.org/)**——以 MIT 许可 vendored 进来驱动 3D 导演台，
  出处见 [`engine/kinema/studio_app/vendor/NOTICE.md`](engine/kinema/studio_app/vendor/NOTICE.md)。
- **[FreePD](https://freepd.com/)** 与 **[Freesound](https://freesound.org/)**——内置
  100+ 首 BGM 与 18 枚音效的 CC0 来源，逐条出处登记在 [`music/ATTRIBUTION.md`](music/ATTRIBUTION.md)。
- **[深度捕捉](.claude/skills/kinema-depth/SKILL.md)**——把实拍片在本机 CPU 上处理成人物深度浮雕
  加 OpenPose-18 骨骼的控制视频，只有这段控制视频作为运动参考交给视频模型。基于三个开源
  感知模型，均为 Apache-2.0：
  - **[RTMPose](https://github.com/open-mmlab/mmpose/tree/main/projects/rtmpose)**（经 **[rtmlib](https://github.com/Tau-J/rtmlib)** 调用）——2D 姿态估计与骨骼绑定。
  - **[Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2)** Small（[ONNX 导出件](https://github.com/fabio-sim/Depth-Anything-ONNX)）——单目相对深度。
  - **[MediaPipe](https://github.com/google-ai-edge/mediapipe)** 人像多类分割——人物遮罩。

  推理运行在 **[ONNX Runtime](https://onnxruntime.ai/)**（MIT）与 **[OpenCV](https://opencv.org/)**
  （Apache-2.0）之上，均以 pip wheel 安装；安装步骤见 [`SETUP.md`](SETUP.md)。

## ⚖️ 许可证

Kinema 以 [**GNU AGPL v3**](LICENSE) 开源。

- **个人免费**——个人使用、学习、研究与评估完全免费，不需要任何额外授权。
- **闭源商用**——对外 SaaS、嵌入闭源产品或 OEM 交付、不打算开源的内部平台，需购买商业授权。

**商业授权与智能体定制咨询** ｜ [bladex.cn](https://bladex.cn) ｜ bladejava@qq.com

---

<div align="center">

**Kinema** · Copyright (C) 2018-2099 [BladeX](https://bladex.cn) · [AGPL v3](LICENSE)

</div>
