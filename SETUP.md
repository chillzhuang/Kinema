<!--
  This file is part of Kinema.
  Copyright (C) 2018-2099 BladeX (https://bladex.cn)
  SPDX-License-Identifier: AGPL-3.0-or-later
-->

# SETUP · 安装、首跑与就绪判定

**就绪与否永远问机器，不靠回忆**（在 `engine/` 目录下）：

```bash
python3 -m kinema setup --check          # 人读版：✓/✗ 逐项清单
python3 -m kinema setup --check --json   # 机器版：agent 解析 ready 字段即可
```

## 三行决策树

1. **`ready: true`（全绿）** → 到此为止，直接开工。**任何 agent 不得再走配置引导。**
2. **有红项** → 只补红项（ffmpeg / 密钥 / 存储哪个红补哪个），补完重跑 `--check`，不重走全流程。
3. **第一次用 / 换机 / 大面积红** → 让你的 AI 走 `kinema-setup` 向导（Claude Code /
   Codex / Cursor / Copilot / Gemini 均能自动发现该 skill），或人工按下方
   「安装三件套」装齐中间件后按 [`config/README.md`](./config/README.md) 逐项配置。

## 安装三件套（人工路径 · macOS / Windows / Ubuntu·WSL）

不用 AI 向导时按此表装齐。先探测——报「找不到命令」的才需要装：

```bash
python3 --version                  # 需 ≥ 3.10；Windows 下命令是 python --version
ffmpeg -version; ffprobe -version
```

| 组件 | macOS | Windows | Ubuntu / Debian / WSL |
|---|---|---|---|
| Python ≥ 3.10 | `brew install python` | `winget install Python.Python.3.12` | `sudo apt install -y python3 python3-pip python3-venv` |
| ffmpeg + ffprobe | `brew install ffmpeg` | `winget install Gyan.FFmpeg` | `sudo apt update && sudo apt install -y ffmpeg` |
| 引擎（`engine/` 下） | `python3 -m pip install -e ".[cloud,yaml]"` | `python -m pip install -e ".[cloud,yaml]"` | 同 macOS 列 |

- **macOS**：没有 `brew` 先装 <https://brew.sh>。表里刻意用不带版本号的
  `brew install python`——它会把 `python3` 链进 PATH；带版本号的 `python@3.12`
  装出来的命令是 `python3.12`，`python3` 仍指旧解释器，正是最难查的多解释器陷阱。
- **报 `externally-managed-environment`**（brew 的 Python 与 Ubuntu 23.04+ / Debian 12
  默认拒绝 pip 装进系统环境）：先建虚拟环境再装引擎——
  `python3 -m venv .venv && source .venv/bin/activate`（Windows：`python -m venv .venv`
  后 `.venv\Scripts\activate`），激活后重跑上表引擎那条命令。
- **Windows 两条差异**：命令一律用 `python`（没有 `python3` 别名）；winget 装完**重开
  终端** PATH 才生效。引擎与 CLI 在 Windows 原生可用（跨进程文件锁为 POSIX/Windows
  双实现），也可整套走 WSL 按 Ubuntu 列操作。
- **深度捕捉（可选）**：把实拍视频的人物运动复刻到本项目角色上，需要一组本机感知栈——
  `pip install -e ".[control]"`，装完**再跑一行** `pip install --no-deps rtmlib`。
  第二行不能省也不能并进第一行：rtmlib 同时声明了 `opencv-python` 与
  `opencv-contrib-python`，两者装进同一个 `cv2` 命名空间、前者会盖掉后者的
  `ximgproc`（引导滤波），而 PEP 621 的 extra 表达不了 `--no-deps`。
  权重约 115MB，`python3 -m kinema control fetch` 显式下载；就绪状态看
  `python3 -m kinema doctor` 的「可选依赖 control」一行。不装不影响其他任何功能。
- **参考视频要上云，但不必整份工作区改档**：Seedance 的参考视频（3D 预演 / 深度捕捉）
  与口型精修在协议层只收公网 URL。桶名、区域与密钥一并写进 gitignore 的
  `config/secrets.yaml`（`KINEMA_OSS_BUCKET` / `KINEMA_OSS_REGION` /
  `KINEMA_OSS_ACCESS_KEY` / `KINEMA_OSS_SECRET_KEY`，或同名环境变量）——
  `config/storage.yaml` 随仓库分发，桶名填在那里会跟着提交。填完
  **`backend` 保持 `local`**，其余媒体照常留在本地，只有这两条路按需上传。桶需 public-read。
- MySQL / OSS / BGM 属**可选件**，默认 local/本地曲库零依赖；数据库与云桶服务端用户
  自备，引擎只填连接（见 [`config/README.md`](./config/README.md)）。
- 其余发行版（Fedora / Arch）、多解释器陷阱与逐项排错表单源在 `kinema-setup` 向导，
  人工路径卡住随时切回向导，本表不复制。

装完回到顶部 `setup --check`，绿灯即就绪。

## 持久化在哪（为什么绿灯可信）

配置文件本身就是持久层，**没有也不需要**另外的“已配置”标记文件：

| 配置 | 落点 | 进 git？ |
|---|---|---|
| 密钥 | 环境变量 或 本机密钥文件（`setup` 向导 / 网页配置页写入） | 绝不 |
| 模型激活与连接 | `config/models.local.json`（Studio「配置」页 / `config` 命令写入） | 绝不 |
| 存储 / 媒体选型 | `config/storage.yaml` | 是（默认 local 零依赖） |

`setup --check` 只读实测这些落点，秒级、幂等——绿灯即事实，不存在“配好了
还提醒配一遍”；反之密钥吊销、换机后它会如实变红，标记文件做不到这一点。

## 生图路由（为什么可能不需要生图密钥）

生图走哪条路由由引擎自动判定（命中即停）：**① models 页显式激活的生图 API**
→ **② agent 原生生图**（带原生生图能力的 agent 声明 `KINEMA_AGENT_IMAGEGEN=1`，
生图密钥判“不适用”，工单模式详见 AGENTS.md §1 第 3 条）→ **③ 默认 provider**
（此时才检测 `ARK_API_KEY`）。配音密钥（`ARK_TTS_API_KEY`）不受此影响。

---

工程纪律与契约真源在 [`AGENTS.md`](./AGENTS.md)；全景架构与二开配方在
[`DEVELOP.md`](./DEVELOP.md)。本文件只管“从零到绿灯”，不承载其余内容。
