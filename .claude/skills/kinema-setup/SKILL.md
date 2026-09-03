---
name: kinema-setup
description: "配置或诊断 Kinema 运行环境。第一次使用、换机、重装、ffmpeg/Python/依赖缺失、密钥、MySQL、OSS、BGM 或 doctor 报错时使用；只配置与验收，不执行内容生产。"
metadata:
  kinema-managed-by: "agent/manifest.json"
  kinema-kind: "system"
  kinema-status: "stable"
  kinema-version: "2.0.0"
  kinema-owner: "Kinema"
  kinema-source: "workspace"
  kinema-trust: "first-party"
  kinema-digest: "sha256:cd496f166c693c2e7ba506484217c0492b666782d3003789ac0e372c567e15eb"
---
# kinema-setup · 前置配置向导

把「拿到这个仓库」到「能出片」之间的所有配置一次问清、配好、验通。
六道节点，**逐节点确认后再往下**；每道节点都以一条可复验的命令收尾。

产出：一张**就绪度总表**（每项 ✓/✗ + 处置）＋ 下一步该调哪个 skill。

原生生图能力的判断、询问和会话级路由声明遵循
[原生生图能力握手](../kinema/references/native-imagegen.md)；不要把 `ARK_API_KEY` 是否存在当作唯一判断。

---

## 铁律（Agent 纪律，先读）

1. **绝不跑交互式 `setup`**（不带 `--check`）——它 `input()` 等 stdin，在 agent 会话里会把整个
   命令挂死。自检一律用 `python3 -m kinema setup --check` 与 `doctor`。
2. **绝不擅自装系统包、绝不 sudo**。探测缺什么 → **先认平台**（`uname`/`os-release`）→ 给出
   该平台的确切命令 → 让用户自己在会话里用 `! <命令>` 跑（输出会直接回到对话），或明确点头
   后由你执行。装 pip 包属可逆操作，征得同意后可直接跑；`brew/apt/dnf/pacman/winget` 这类
   改系统的一律请用户执行。
3. **服务端一概不装、不启、不改**：MySQL 与对象存储都由用户自己准备（自建/公司的/云托管），
   本 skill **只收集连接信息并验证连通性**——不跑 `apt install mysql-server`、不 `docker run
   mysql`、不改 my.cnf、不建库建用户改权限、不代开通 OSS/代建桶。要建库就把 SQL 交给用户执行。
   唯一会装的是**客户端驱动**（PyMySQL / oss2 等 pip 包）。
4. **密钥只报状态，不回显值**。永远只说「env 已设 / secrets.yaml 已设 / 未设」；不 `cat`
   `secrets.yaml`、不把 key 写进对话、不写进任何 md/log。
5. **优先用环境变量固化，别动仓库里的配置默认值**。`config/storage.yaml` 提交的是
   `backend: local`（零依赖开箱即用），把它改成 mysql 会产生 git 改动并影响别的机器；
   本机启用走 `KINEMA_STORAGE_BACKEND=mysql`。用户明确说「改文件」才改文件。
6. **一次只推进一道节点**，每道跑完复验命令并把结果原样贴给用户；失败就地处置，不往下滚。
7. 不做内容生产。配完就把用户交给 `kinema`（或对应 `kn-*` 风格 skill）。

---

## 配置面全景（决策 → 落在哪 → 怎么验）

| 决策 | 落点 | 复验 |
|---|---|---|
| 系统依赖 | 系统包管理器（ffmpeg/ffprobe 是**唯一硬依赖**） | `python3 -m kinema doctor` |
| 引擎与可选依赖 | `engine/` 下 `pip install -e ".[…]"` | `python3 -m kinema --version` |
| 数据存储 local/mysql | `config/storage.yaml` 顶层 `backend` 或 env `KINEMA_STORAGE_BACKEND`（**数据库服务端用户自备，这里只填连接**） | `db status` |
| 媒体存放 local/oss | `config/storage.yaml` 的 `media` 段（**桶用户自己开通，这里只填连接**） | `oss status` |
| 云能力密钥 | `config/secrets.yaml`（**`setup` 自动生成，不用手建**）或 `config secret` 写本机密钥文件；优先级 env > `secrets.local.json` > `secrets.yaml` | `setup --check` |
| 音频库（BGM/音效） | `music/`（媒体不入 git） | `sfx list` · `doctor` 的「音乐库: N 首」 |
| 字体 | **无需配置**——字幕/水印/封面字体全部工程内置（`engine/kinema/assets/fonts/`） | — |

---

## 节点① 系统依赖体检

先跑探测，一次问全：

```bash
python3 -V                       # 需 ≥ 3.10
which ffmpeg ffprobe             # 唯一硬依赖，两个都要有
python3 -c "import sys, importlib.util as u; print(sys.executable,
  '| yaml', bool(u.find_spec('yaml')), '| kinema', bool(u.find_spec('kinema')))"
```

⚠️ **多解释器陷阱（最容易漏、后果最隐蔽）**：机器上常有好几个 python（系统自带 / homebrew
3.12 / 3.13 / venv），extras 装在**哪一个**里就只有那一个能用。必须查「实际跑引擎的那个
解释器」有没有 `yaml`——**缺 PyYAML 时引擎会显式告警并回退内置默认配置**，
同一 profile 可能换 provider、丢字幕样式，真跑非 mock 时才看得出差异。
（**密钥不受影响**：`config_overlay.read_yaml_secrets_flat` 用标准库读 `secrets.yaml`，
装没装 PyYAML 都读得到。）
真实案例：本仓库开发机 `python3`→3.13 无 PyYAML、`python3.12` 有，
同一条命令换个解释器结论完全相反。处置：给那个解释器补 `pip install -e ".[yaml]"`，
或统一固定用装好的那个（写进说明/别名）。

### ①-a 先认平台，再给命令

**不要凭印象猜平台**——同一条 `apt` 在 Fedora 上白费，`brew` 在 Linux 上路径也不同。先探：

```bash
uname -s -m                                  # Darwin arm64 / Linux x86_64 …
cat /etc/os-release 2>/dev/null | head -3    # Linux 发行版与版本（ID/VERSION_ID）
echo $SHELL                                  # 后面写环境变量要按 shell 给语法
```

Windows（PowerShell）：`$PSVersionTable.PSVersion`、`winget -v`、`where.exe python ffmpeg`。
**WSL 里一律按其发行版（多为 Ubuntu）处理**，不要给 Windows 的命令。

### ①-b ffmpeg / ffprobe（唯一硬依赖）

| 平台 | 安装命令 | 备注 |
|---|---|---|
| macOS | `brew install ffmpeg` | 没有 brew：先装 <https://brew.sh>；Apple Silicon 装在 `/opt/homebrew/bin`、Intel 在 `/usr/local/bin`，装完确认在 PATH |
| Debian / Ubuntu / WSL | `sudo apt update && sudo apt install -y ffmpeg` | — |
| Fedora | `sudo dnf install -y ffmpeg` | 需先启 RPM Fusion（`sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm`） |
| RHEL / Rocky / CentOS | `sudo dnf install -y ffmpeg` | 同样需 RPM Fusion + EPEL；企业机常无权限 → 走下面「无 root」 |
| Arch / Manjaro | `sudo pacman -S ffmpeg` | — |
| openSUSE | `sudo zypper install ffmpeg` | 可能需加 Packman 源 |
| Alpine | `sudo apk add ffmpeg` | — |
| Windows | `winget install Gyan.FFmpeg` ｜ `scoop install ffmpeg` ｜ `choco install ffmpeg` | 装完**重开终端**（winget 不刷新当前会话 PATH；choco 可 `refreshenv`） |
| **无 root / 不想动系统** | 静态构建解包到 `~/bin` 并加 PATH：Linux <https://johnvansickle.com/ffmpeg/> ｜ macOS <https://evermeet.cx/ffmpeg/> ｜ 或 `conda install -c conda-forge ffmpeg` | 静态包自带 ffmpeg + ffprobe 两个可执行，**两个都要放进 PATH** |

复验（两个都必须有输出，只有 ffmpeg 没 ffprobe 也不行）：

```bash
ffmpeg -version | head -1 && ffprobe -version | head -1
```

### ①-c python ≥ 3.10

| 平台 | 安装/升级 | 备注 |
|---|---|---|
| macOS | `brew install python@3.12` | 系统自带 `/usr/bin/python3` 版本旧且不好装包，别用它 |
| Debian / Ubuntu | `sudo apt install -y python3 python3-pip python3-venv` | 20.04 等老 LTS 只有 3.8 → 上 `deadsnakes` PPA 装 `python3.12` |
| Fedora / RHEL | `sudo dnf install -y python3 python3-pip` | — |
| Arch | `sudo pacman -S python python-pip` | — |
| Windows | `winget install Python.Python.3.12` | 命令是 **`python`** 不是 `python3`；勾/确认「Add to PATH」 |
| 跨平台版本管理 | `pyenv install 3.12` ｜ `uv python install 3.12` ｜ `conda create -n av python=3.12` | 多版本共存首选，避免污染系统 python |

### ①-d 引擎与可选依赖

**装在哪个解释器里，就只有那个解释器能用**——建议直接用「要跑引擎的那个 python」调 pip：

```bash
cd engine
python3 -m pip install -e ".[cloud,yaml]"     # 用 python3 -m pip，避免 pip 指向别的解释器
# Windows: python -m pip install -e ".[cloud,yaml]"
```

想彻底隔离（推荐给洁癖/多项目机器）：

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -e "engine[cloud,yaml]"        # 之后所有 kinema 命令都在这个 venv 里跑
```

extras 按需装，别一上来全装：`cloud`（真实 provider 走 HTTP）· `yaml`（读 `config/*.yaml`，
**强烈建议装**，见上方陷阱）· `mysql`（PyMySQL 驱动，节点②要）· `oss-aliyun`/`oss-tencent`/
`oss-volc`/`oss`（节点③要）· `asr`（本地语音转写：verify 的 native 人声文字核对与字幕
逐句划界，CPU 推理零 API 成本，权重首跑自动下载）。`vision`/`vision-clip` 是**登记位、
一期不启用，装了也不会被调用——别装**（后者是 GB 级 torch）。

收尾复验：

```bash
python3 -m kinema doctor
```

`doctor` 会一次报出：ffmpeg 版本 · 配置源 · 存储后端 · 默认 profile · providers ready/planned ·
音乐库首数 · 孤儿 ffmpeg 进程。把输出贴给用户，逐行解释哪项还没配。

---

## 节点② 数据存储：本地 JSON vs MySQL

**先问，别替用户默认**：

| 你的情况 | 选 |
|---|---|
| 单机单人、就想快点出片 | **local**（默认，零依赖：`project.json` 即数据库） |
| 多台机器/换机继续、要跨项目 SQL 直查看板、要正式落库 | **mysql** |

- **选 local**：什么都不用做（仓库默认就是）。跳到节点③。
- **选 mysql**：**只配置连接，绝不代装数据库**。

> 🚫 **本节点不装、不启、不改任何 MySQL 服务端**——不跑 `brew install mysql` / `apt install
> mysql-server` / `docker run mysql`，不改 my.cnf，不建用户、不改权限、不动别的库。
> 数据库该由用户自己准备好（自建、公司的、云 RDS 都行），这里**只要四样东西**：
> **地址 host · 端口 port · 账号 user · 密码**（外加库名，默认 `kinema`）。
> 唯一会装的是 **PyMySQL —— Python 客户端驱动**（可逆 pip 包，不是数据库本体）。

问用户拿到四要素后：

```bash
# 1) 客户端驱动（唯一的安装动作）
python3 -m pip install -e "engine[mysql]"

# 2) 连接参数：编辑 config/storage.yaml 的 mysql 段
#      host / port / user / database / charset / table_prefix
#    ⚠ 密码不写这里 —— 放 config/secrets.yaml 的 KINEMA_MYSQL_PASSWORD
#      （env 同名变量优先级更高；你不要代读、代打印密码）

# 3) 本机启用（推荐 env：不产生 git 改动、不影响别人的机器）
export KINEMA_STORAGE_BACKEND=mysql          # bash / zsh 当次会话
set -Ux KINEMA_STORAGE_BACKEND mysql         # fish 通用变量（永久固化）
$env:KINEMA_STORAGE_BACKEND = "mysql"        # PowerShell 当次会话
[Environment]::SetEnvironmentVariable("KINEMA_STORAGE_BACKEND","mysql","User")  # PowerShell 永久

# 4) 通、再建表、再登记
python3 -m kinema db status                   # 先过连通性这关，报错就地看错在哪
python3 -m kinema db init                     # 在**用户给的那个库里**建 8 张 kn_* 表
python3 -m kinema db sync                     # 本地已有项目 JSON → 库（换机恢复反向用 db pull）
```

**库不存在时不要代跑 DDL**，把语句给用户，让他用自己的客户端/DBA 执行（建库通常需要更高权限）：

```sql
CREATE DATABASE IF NOT EXISTS kinema DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
-- 只给这个库的权限即可，不需要 root：
-- GRANT ALL PRIVILEGES ON kinema.* TO '你的账号'@'%';
```

要交给 DBA 审的完整建表脚本：`python3 -m kinema db schema --out docs/sql/kinema.sql`（生成物，别手改）。

⚠️ 三条经验：① mysql 模式下**本地 JSON 仍是渲染工作副本**，媒体文件永远在磁盘，库里只存路径与元数据；
② `db init` 只在指定库里建 `av_` 前缀的表，不碰同库里别的表——但仍建议给它一个**专用库**；
③ 连不上就先 `db status` 定位（多是端口/账号/远程访问未放开），临时回退
`KINEMA_STORAGE_BACKEND=local` 立刻能继续干活，数据不会丢。

---

## 节点③ 媒体存放：本地 vs 对象存储

**默认 local，别劝用户上云**。只有两种情况真需要 OSS：

1. **参考视频 V2V**（`gen-video --previz`）——视频参考必须是**公网 URL**，本地路径与 data-url 一律被拒；
2. 多机/协作要共享媒体，或成片要给外部直链。

同节点②的纪律：**桶由用户自己在云控制台开通/创建，你只填连接信息**（不代开服务、不代建桶、
不代改跨域/公读策略——那都是要计费和授权的动作）。选了就填 `config/storage.yaml` 的 `media` 段：

```yaml
media:
  backend: oss                  # local | oss
  provider: aliyun              # aliyun(OSS) | tencent(COS) | volcengine(TOS) | mock(离线测试)
  bucket: "你的桶"
  region: "cn-hangzhou"         # 腾讯 ap-guangzhou / 火山 cn-beijing
  endpoint: ""                  # 可选，覆盖默认
  prefix: av                    # 对象 Key 前缀（Key = 前缀/工作区相对路径）
  public_base: ""               # 可选，自定义域名/CDN
```

依赖按家装：`pip install -e "engine[oss-aliyun]"`（或 `oss-tencent` / `oss-volc` / `oss` 全装）。
AK/SK 进 secrets：`KINEMA_OSS_ACCESS_KEY` / `KINEMA_OSS_SECRET_KEY`（env 优先）。

```bash
python3 -m kinema oss status                      # 媒体后端与连通性自检
python3 -m kinema oss sync --project <项目id>      # 确认后上传并把路径改写成 URL
python3 -m kinema oss pull                        # 换机：按 URL 拉回本地继续渲染
```

⚠️ **第三方参考片（`project/<pid>/study/`）绝不上云**——那是版权红线，护栏靠「契约里路径恒为
工作区相对」这一条，别手工把它改成绝对路径。

---

## 节点④ 云能力密钥（按需，取最小集）

先讲清三档，让用户自己选起点：

| 档 | 需要的 key | 能干什么 |
|---|---|---|
| 零成本彩排 | 不需要任何 key | `run --mock` 端到端跑通全链路（占位素材，验证环境） |
| **正式出片（第三方图像链）** | `ARK_API_KEY` + `ARK_TTS_API_KEY` | 图（Seedream）+ 视频（Seedance）+ 配音（火山语音）——两把 key 就够 |
| **正式出片（Agent 原生生图）** | `ARK_TTS_API_KEY`；若用 Seedance 再加 `ARK_API_KEY` | 图片由当前 Agent 生成，不需要生图 key；配音和视频仍按实际节点配置 |
| 加配乐 | `ELEVENLABS_API_KEY` | AI 生成 BGM；**不填会自动降级本地免费曲库**（节点⑤），多数场景不必配 |

备选 provider（都是「填 key 即用」，不填不影响）：`GEMINI_API_KEY`（Nano Banana 图 / Veo 视频）·
`DASHSCOPE_API_KEY`（通义万相）· `MINIMAX_API_KEY`+`MINIMAX_GROUP_ID`。

如果当前会话实际暴露了原生图像生成工具（例如 Codex ImageGen），按
[原生生图能力握手](../kinema/references/native-imagegen.md)自动或询问后声明
`KINEMA_AGENT_IMAGEGEN=1`。此时生图不需要 `ARK_API_KEY`；`ARK_API_KEY` 只在后续使用
Seedance 等视频 API 时需要。用户或机器已显式激活的第三方 image provider 优先于该声明，
`setup --check --json` 的 `image_route` 是最终判据。

配置方式：

**不要让用户手抄这个文件，也不要自己 `cp`**——`setup`（含 `--check`）在缺文件时
已经从 `secrets.example.yaml` 自动生成一份全空的 `config/secrets.yaml`，
每把 key 的申请地址就写在对应条目上方的注释里（Studio 模型页各服务商抽屉里也有
「打开控制台」直达）。请用户自己编辑填值，**你不要代读、代打印文件内容**。

不想编辑文件的用户走命令写本机密钥文件（同样不用手建文件）：

```bash
cd engine && python3 -m kinema config secret ARK_API_KEY <值>
```

三层优先级 **env > `secrets.local.json` > `secrets.yaml`**，临时试可以
`export ARK_API_KEY=…`。两份密钥文件都已在 `.gitignore`，**绝不提交**。

复验（只输出「已设/未设」，不泄漏值）：

```bash
python3 -m kinema setup --check
```

---

## 节点⑤ 音频库：免费商用 BGM + 音效

问一句「要不要拉免费商用音频库？」——**默认建议拉**，理由讲清：

- 不配 `ELEVENLABS_API_KEY` 时，BGM 走本地曲库；**库为空会退化成合成正弦氛围床**（就是那种
  嗡嗡声，`doctor` 会警告）。用户听到「BGM 怎么是嗡嗡声」十有八九是这一条。
- 拉下来是 **103 首 BGM（四情绪目录）+ 18 枚音效**，全部 **CC0 / 公共领域：免费商用、免署名、
  可改编、可随成片分发**。媒体不入 git，换机重跑即重建。

```bash
python music/download.py            # 一键两套；单文件失败不致命，重跑会跳过已存在的
```

复验：

```bash
python3 -m kinema sfx list        # 18 枚应全为「✓ 外置」
python3 -m kinema doctor          # 应出现「音乐库: 103 首」
```

要再加曲子/音效：脚本收尾会打印 Pixabay 手动下载指路（授权达标但站方禁脚本抓取）。
**收音频进库的四条硬标准**（缺一条别进库）：① 免费商用 ② **免署名** ③ 允许作背景音嵌入并
随成片分发 ④ 不限平台。原因是引擎按情绪目录**确定性选曲**——库里混进一首要署名的，
就会随机被某一集选中，那一集便默默背上署名义务。授权登记见 `music/ATTRIBUTION.md`。

---

## 节点⑥ 端到端验收 + 起大屏

```bash
python3 -m kinema setup --check     # 非交互验收：有未就绪项会非零退出并列出来
python3 -m kinema doctor            # 完整体检
```

零成本 mock 彩排（**这是"环境真的通了"的唯一凭据**——生图/配音/字幕/合成全跑一遍）：

```bash
python3 -m kinema project new --title "环境自检" --id setup_demo
python3 -m kinema chapter new setup_demo --title "链路自检"
# 编辑 project/setup_demo/chapters/ch01.json，填两镜最简 shots（narration + image_prompt 即可）
python3 -m kinema run --chapter setup_demo/ch01 --mock
```

跑通后起大屏（**单例纪律**：本工作区已在跑就复用并把 URL 打给用户，绝不新起进程；
换端口/重启用 `--restart`、停用 `--stop`、查实例用 `--status`）：

```bash
python3 -m kinema studio --port 8787      # → http://127.0.0.1:8787
```

如果后续节点需要用户点选、上传或审阅，不要只把上面的命令转给用户；先按
[Studio 交互交接协议](../kinema/references/studio-handoff.md) 自动查状态、启动并给出具体入口。

最后给用户一张总表，例如：

```
就绪度总表
  ✓ python3 3.13 / ffmpeg 7.1
  ✓ 引擎 pip -e 已装（extras: cloud, yaml）
  ✓ 数据存储 local（JSON 即数据库）
  ✓ 媒体存放 local
  ✓ 密钥 ARK_API_KEY 已设 · ARK_TTS_API_KEY 已设 · ELEVENLABS 未设（走本地曲库，无需配）
  ✓ 音频库 103 首 BGM + 18 枚音效（CC0 免署名）
  ✓ mock 全链路跑通 setup_demo/ch01 · 大屏 http://127.0.0.1:8787
下一步：说「做一条 XX 主题的视频」→ 走 /kinema（未点画风会按题材自动定档），
        或点名风格 /kn-anime、/kn-explainer…；要做多集系列先走 /kinema-project。
```

自检工程 `setup_demo` 可留着当回归样本，也可 `python3 -m kinema project rm setup_demo` 收进回收站。

---

## 故障速查（症状 → 根因 → 处置）

| 症状 | 根因 | 处置 |
|---|---|---|
| `No module named kinema` | 不在 `engine/` 下跑，或没 `pip install -e` | `cd engine && pip install -e ".[cloud,yaml]"` |
| `⚠ 找到配置 config/models.yaml 但缺 PyYAML，已回退内置默认配置` | 跑引擎的那个解释器没装 yaml extra——**静默用内置默认，provider/字幕样式可能与文件不一致** | 给**该解释器** `pip install -e ".[yaml]"` |
| 密钥填在 `secrets.yaml` 里，`setup --check` 却报「未设」 | **不是 PyYAML 的锅**（密钥走标准库读，与 extras 无关）：多半是值没加引号/写在注释行/写错变量名，或另有一份 `secrets.local.json` 同名键把它压住了（本机那份优先级更高） | `config secret <KEY> <值>` 写到高优先级那层；或临时 `export ARK_API_KEY=…` 验证 |
| 单测本地报 `ConfigError: 缺少密钥 ARK_TTS_API_KEY` | 同一个根因——换成装了 PyYAML 的解释器即全绿 | `python3.12 -m unittest discover -s tests`（或补 extras） |
| BGM 是正弦嗡嗡声 | 音乐库为空且没配 ElevenLabs | `python music/download.py` |
| 合成/抽帧报 ffmpeg 相关错 | ffmpeg/ffprobe 不在 PATH | 见节点① 安装表，装完重开终端 |
| MySQL 连不上、命令直接报错 | 密码/端口/库不对，或没装 PyMySQL | `db status` 看具体错；临时 `KINEMA_STORAGE_BACKEND=local` 先干活 |
| 明明说了用 local，却连上了 MySQL | 环境里固化了 `KINEMA_STORAGE_BACKEND=mysql`（fish 通用变量常见） | 命令前显式加 `KINEMA_STORAGE_BACKEND=local`，或 `set -e KINEMA_STORAGE_BACKEND` |
| Studio 打不开 / 端口被占 / 一堆残留进程 | 重复启动 | `studio --status` 查 → `--restart` 换端口重起 → `--stop` 停；清残留 `pkill -f 'kinema studio'` |
| 改了 Studio 的 Python 层没生效 | `scanner/actions/server` 是进程启动时载入内存 | `studio --restart`（前端静态资源每次请求读盘，总是新的） |
| CPU 一直高、风扇狂转 | 孤儿 ffmpeg（父进程被杀后无人认领） | `doctor` 会列 pid → `kill -9 <pid>`，或起 `studio` 自动收割 |
| 视频 V2V 报「参考视频必须公网 URL」 | 媒体后端还是 local | 见节点③ 配 OSS，或先不用 `--previz` |
| Windows 上 `python3` 找不到 | Windows 用 `python` | 用 `python -m kinema …`；ffmpeg 走 winget/scoop 并确认 PATH |

---

## 边界

- 本 skill **只管配置与验收**，不写文案、不拆分镜、不生图。
- 配完的下一步：`kinema`（通用直达，未点画风按题材定档）· `kn-*`（点名画风）·
  `kinema-project`（多集系列/强规划）。
- 花钱的操作一概不在本 skill 里触发（生图/生视频/配音/音色复刻/AI 音效）——
  验收只用 `--mock` 与本地零成本命令。
