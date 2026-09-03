# config/ · 配置中心总览

本目录是 kinema 的**配置真源**：模型/画风/音色/密钥/存储/模板/品牌全部在这里，
代码零写死。改配置 = 改行为，无需动 Python。本文说明每个文件是干嘛的、字段含义、
它们之间怎么关联，以及改完之后怎么验证。

## 文件地图

| 文件 | 作用 | 被谁读取 | 入库 |
|---|---|---|---|
| [`models.yaml`](models.yaml) | **模型与画风唯一真源**：厂商别名注册表 + 全局默认入口 + 44 个画风档 + 画布 | 引擎 `ConfigStore`/`ModelRouter`（所有生成阶段）、Studio 风格档矩阵 | ✅ |
| [`voices.yaml`](voices.yaml) | 音色别名表（170+ 别名 → 火山 voice_type，以中文为主含少量英/日/韩） | 随 models.yaml **同目录自动加载**（`store.resolve_voice`） | ✅ |
| `secrets.yaml` | 真实密钥（**gitignore，绝不入库**）；缺文件时由 `setup` 从模板自动生成，用户不用手建 | 随 models.yaml 同目录自动加载；优先级 **环境变量 > secrets.local.json > 本文件** | ❌ |
| [`secrets.example.yaml`](secrets.example.yaml) | 密钥模板（**全空值**，`setup` 据此生成 secrets.yaml） | `config_overlay.ensure_secrets_yaml` | ✅ |
| [`storage.yaml`](storage.yaml) | 持久化：文档层（local/mysql）+ 媒体层（本地/OSS） | `storage/` 模块（工作区所有读写） | ✅ |
| [`templates.yaml`](templates.yaml) | 平台规格模板（抖音漫剧等 6 个：比例/时长/集数/分账口径） | `project new --template`、`spec check`、Studio 规格达标卡 | ✅ |
| [`branding.yaml`](branding.yaml) | 白标三键（name/tagline/accent），卖断交付换牌用 | Studio 品牌区、提案书、审阅包页脚 | ✅ |
| [`audio.yaml`](audio.yaml) | 音频资产总注册表：`music/` 库两子库——bgm 段（情绪→目录+场景关键词，BGM 选曲）+ sfx 段（语义键→文件，三级解析） | `audio_registry.py`（BGM 选曲 + 转场音效 + `sfx list/gen`） | ✅ |
| `models.local.json` | **模型配置覆盖层**（gitignore）：网页配置中心与 `config` 命令写的连接段与激活项，凌驾于 models.yaml 之上；没配的字段一律回落 models.yaml | `config_overlay` → `ConfigStore.load` 三条出口 | ✅ 跨机同步层（`kn_setting` 表） |
| `secrets.local.json` | **本机密钥**（gitignore，**绝不入库、绝不下发**）：网页填的 key 落这里；优先级 **环境变量 > 本文件 > secrets.yaml** | 随覆盖层同目录加载（`ConfigStore.secret`） | ❌ |

**发现规则**：引擎从当前目录与包位置**向上查找** `config/<名>.yaml`（所以从仓库任意子目录
运行 CLI 都能命中本目录）；`KINEMA_MODELS` / `KINEMA_STORAGE` 环境变量可显式指路。
磁盘有 models.yaml 但缺 PyYAML 时回退引擎内置精简默认并**打印警告**；完全找不到文件则静默回退（`doctor` 的"配置源"会显示 `<embedded>`）
。mock 离线因此始终可跑。回退态有机器可读标记 `ConfigStore.fallback`（`missing-pyyaml`/`missing-config`）
：`doctor` 会追加一行 `[!]` 点名「仅 N 个内置画风在服务」，Studio 新建项目弹层也会亮告警条（画风目录缩水正是回退的最痛后果——实测 kn-anime3d 五路线只剩 1 个）
。

## 关联总图

```
project.json ──"profile"──▶ models.yaml:profiles ──偏离项 provider──┐
     │                            │（未写 provider 时）              │
     │                            ▼                                  ▼
     │                    models.yaml:defaults.providers ──▶ models.yaml:providers.<别名>
     │                    （全局默认入口，换厂商改这里）        │ base_url/model/api_key_env/impl
     │                                                         │
     ├──"voices"──▶ voices.yaml（别名→voice_type）              ├─ api_key_env ──▶ 环境变量 > secrets.local.json > secrets.yaml
     └──落盘位置──▶ storage.yaml（local/mysql + OSS）           └─ impl ──▶ 引擎 _ADAPTERS 适配器（代码层）

project new --template ──▶ templates.yaml（profile/aspect/motion/规格快照 落进项目）
Studio / export-pitch / export-review ──▶ branding.yaml（换牌）
BGM 选曲 / compose 转场音效 ──▶ audio.yaml（music/ 库：bgm 情绪目录+关键词 · sfx 键→文件）
```

---

## models.yaml —— 四层解析链（核心）

一次生成调用（如生图）按此链路决定"用哪家模型怎么调"：

```
① 选 profile      shots[].profile > CLI --profile > project.json "profile" > defaults.profile
② 选别名          profiles.<名>.<能力>.provider（偏离项，可选）
                   └─ 未写 → defaults.providers.<能力>（全局默认入口 ★换厂商只改这一行）
③ 取连接段        providers.<别名>：base_url、model、api_key_env、计费、prompt_lang、impl
④ 工厂实例化      impl（缺省=别名本身）→ 引擎 _ADAPTERS[(能力, impl)] 适配器类
                   → 统一返回 ImageResult/TTSResult/VideoResult/MusicResult
```

**三个换模型场景的操作手册**：

| 场景 | 操作 | 改动量 |
|---|---|---|
| 同厂换模型版本 | `providers.<别名>.model` 改一行；或新加别名 `impl:` 指向同一适配器 | 1 行，零代码 |
| 全局换厂商 | `defaults.providers.<能力>` 改指向新别名 | 1 行，零代码 |
| **只想改这台机器**（不动仓库配置） | Studio「配置」页，或 `config set --provider X --set base_url=… ` / `config activate --capability video --provider X` | 落 `models.local.json`，仓库工作树零改动 |
| 接入新厂商 | `engine/kinema/providers/<能力>/<名>.py` 写适配器（构造签名 `(conn, store)`，返回统一 Result）+ `models.py` 的 `_ADAPTERS` 登记一行 + 本文件 providers 段加别名 | 一个类 + 两行 |

### providers 段（厂商别名注册表）

| 字段 | 说明 |
|---|---|
| `kind` | 能力类型：`image` / `video` / `tts` / `music` / `lipsync` |
| `status` | `ready`=已接入可用｜`planned`=已登记待接入（调用会给出清晰报错，不会静默）。**当前清单不手抄在此**——跑 `python3 -m kinema config show` 看「连接段」那张表，它直接读本文件与覆盖层，不会与实际脱节 |
| — | **命名分工（有守卫强制）**：**别名**（providers 段的键）用**连字符**——它面向用户，出现在 yaml、`--video-provider` 的值与项目文档的 `video_provider` 里，与各家模型 ID 的写法一致（`doubao-seedance-2-0-mini` / `image-01` / `music-3.0` / `MiniMax-H3`）；**impl** 用**下划线**——它必须与 Python 模块名同名（`providers/image/nano_banana.py`），模块名不能带连字符。两者不同时**必须显式写 `impl:`**（缺省 impl=别名自身，不写会在运行期才报「没有对应适配器」）。改过名的别名在 `models.LEGACY_ALIASES` 留兼容位：读时认旧名并提示，**写时一律落新名**（旧名沉淀进覆盖层会随数据库同步到别的机器） |
| `impl` | **可选**。指向适配器实现名（缺省=别名自身）——同厂新模型/新区域端点加别名时用它复用已有适配器 |
| — | **视频双模型策略**：`seedance-mini`（doubao-seedance-2-0-mini，**缺省主力**，日常量产全走它）/ `seedance-2.5`（doubao-seedance-2-5 大模型，`impl: seedance` 零代码复用）。2.5 只有**显式点名**才用：单次 `gen-video --video-provider seedance-2.5`，持久档写章节文档顶层 `video_provider`（`chapter set <项目> <章节> --video-provider seedance-2.5`；flag > 章节字段 > profile 链，解析真源 `ModelRouter.resolve_video`，系列 `project.json` 不读此键）——大模型绝不静默升级。两个别名的单价独立配置，**务必分别按控制台核对**（mini 与 2.5 价差直接进台账与报价） |
| `base_url` | API 基地址，**统一以版本号结尾**（`…/api/v3`、`…/v1`）；端点路径由适配器拼接，属调用协议 |
| `model` | 模型 ID |
| `auth` | 填 `none` = 该端点无需鉴权，适配器不发 Authorization 头（自托管 SGLang/vLLM/Ollama 这类本地端点用；不填即按 `api_key_env` 取密钥） |
| `api_key_env` | 密钥的环境变量名（**本文件永远只写变量名不写 key**）；火山 TTS 另有旧版双头回退：seedtts 用 `app_id_env`、doubao 用 `app_key_env`（两家接口头名不同，勿混）+ `access_key_env` |
| `prompt_lang` | 提示词语言偏好：缺省 `zh`；`en` 时引擎自动改用 `*_en` 提示词字段与 `style_prefix_en` |
| `price_per_image` / `price_per_second` / `price_per_kchar` / `price_per_min` | 计费单价（CNY），入成本台账；**0 = 未配置单价 → 不入账**（避免"肯定性零"低估台账），请按控制台实时价填写 |
| `resolution` / `format` / `sample_rate` / `voice` / `resource_id` | 厂商专属参数（见各段行内注释）。视频 `resolution` 是**默认档**，CLI `gen-video --resolution` 可临时覆盖——选 4k 须 `--yes` 二次授权 |
| `price_per_second_<档位>` | 视频按分辨率档的独立单价（如 `price_per_second_1080p`、`price_per_second_4k`）；`--resolution` 落在哪一档就按哪一档报价、过闸、入账；0/不配 = 回落 `price_per_second`（该档报价会被低估，换档前务必配真值） |

### defaults 段（全局默认）

```yaml
defaults:
  profile: narration     # 未指定 profile 时的默认风格档
  fps: 30
  providers:             # ★ 能力级默认别名（总入口）：换厂商只改这里
    image: seedream
    video: seedance-mini
    tts:   seedtts
    music: elevenlabs
    lipsync: volc-lipsync   # dubbed 对白镜的口型精修增强步；未配凭证点名跳过
```

比例**不是**配置项：默认主比例是引擎常量 `project.DEFAULT_ASPECT`（16:9 横屏），
建项时 `--aspect` 显式指定才用竖屏/方形；本段不收 `aspect` 键——无人消费的键
会误导指挥层把未指定比例的项目建成竖屏。画布像素尺寸在 `canvas` 段。

### profiles 段（43 个画风档 ＋ 通用兜底 `narration`）

一个 profile 把「模型偏离项 + 画风前缀 + 默认特效 + 字幕样式 + 节奏」绑成一个名字，
skill 只引用名字。**只在偏离 defaults.providers 时才写 `provider`**（当前全库无一处
偏离，全部走 `defaults.providers`；机制本身由 `test_router_defaults` 守卫）。

| 字段 | 说明 |
|---|---|
| `label` | **中文名（必填）**——Studio 经 /api/overview 下发（本文件单一真源，加画风零前端改动；漂移守卫强制） |
| `image.style_prefix` | 中文画风前缀，引擎自动拼到每镜 image_prompt 前（统一美术） |
| `image.style_prefix_en` | **英文画风前缀（必填成对）**——`prompt_lang: en` 的模型自动选用；漂移守卫测试强制"有中文位必有英文位" |
| `image.style_prefix` / `style_prefix_en` | 画风前缀（引擎无条件拼在每张分镜图/设定图/封面提示词最前面）。**不得含 `pipeline/variation.py` 的 `SLOP_TERMS` 空词**——引擎一边用 lint 禁作者写、一边自己注入是双标；确属行业专名的例外走 `test_config_drift._STYLE_PREFIX_SLOP_OK` 短登记表并写理由。改这两列**必须中英成对改**（`test_every_style_prefix_has_english_twin` 守着成对，语义分叉它看不见）；**改动只影响新项目**——立项时 `style_prompt` 已快照进 `project.json`（`cli.py` 的建项流程）。 |
| `image.image_text_floor` | 图像**防字地板**开关（缺省 `true` 即开，绝大多数画风不必写）：开时引擎给每镜 image_prompt 的负面约束追加"字幕、画面文字、水印"（作者的 `negative_prompt` 在前、地板在后；**逐词去重**——作者自己写过的那一个词不重复注入、没写的照样补齐，跨中英同义词都认），保证分镜图本体干净——字幕全由合成段 ASS 后置烧录。**填 `false` 关掉的只有两档：`game_sim`(HUD 血条/小地图) · `explainer`(信息图标注)**（画面里本来就该有字）；bubble/dialogue_box/ranking 等画风的字是后置烧录，**恰恰要求图本体干净，绝不能关**。opt-out 清单与内嵌一致性由 `test_config_drift` 守卫 |
| `tts.voice` | 模版旁白音色（voices.yaml 别名或 voice_type），只供 `voice audition --narrator` 取缺省候选；缺省路径是按 `voice custom --narrator --prompt` 的声线描述定制，没有选角的旁白在真发前被闸点名，不回落到这里 |
| `music.mood` | BGM 情绪（本地曲库按 `music/<mood>/` 子目录选曲）：calm / upbeat / cinematic / ambient |
| `effects` | 画风候选特效目录（**不自动叠加**——特效是显式创作决定，仅章节/项目 `effects` 点名生效；**14 个内置**，按类分五组：**质感** `vignette` 暗角 · `film_grain` 胶片颗粒 · `paper_grain` 纸纹 · `stopmotion` 定格顿挫 · `warm` 暖调 · `bloom` 泛光；**游戏** `scanlines` CRT 扫描线 · `hud` 游戏 HUD；**天气** `rain` 雨 · `snow` 雪 · `fog` 雾；**光** `light_sweep` 斜光扫过；**粒子** `fireflies` 萤火明灭 · `sparkles` 星尘闪落——粒子类**只有这两个**（目录封闭由 `test_effects` 断言钉死）；全部零成本「活」层；**元数据与目录真源 = `effects.EFFECT_META`/`catalog()`**），生效来源只有 project.json `effects` 点名，Studio「✎ 特效」选择器可视化勾选 |
| `subtitle` | 画风字幕样式（合成段 ASS 后置烧录，视频本体永远无字）。`mode`: `caption` 底部字幕 / `bubble` 头顶气泡 / `dialogue_box` 游戏对话框 / `centered` 语录居中 / `ranking` 榜单徽章；caption 参数：font/size/text_color/outline_color/outline/shadow/bold/spacing/margin_v/accent/speaker_tag；dialogue_box 参数：box/box_alpha/border/name_color/text_color。项目级可用 project.json 顶层 `subtitle` 块覆盖。**字幕语言**由项目文档顶层 `subtitle_lang`（zh/en/both，`project new --subtitle-lang` 设定）下发——both=中文主行+英文副行（分镜需带 `narration_en`），subtitle 块写 `lang` 可做章节级覆盖 |
| `pacing` | 建议分镜数与节奏说明（供 Skill 层写分镜参考，引擎不消费） |

### canvas 段

各比例画布尺寸：`9:16`=1080×1920 / `16:9`=1920×1080 / `1:1`=1080×1080。

---

## voices.yaml —— 音色别名表

`presets: {中文别名: {voice: voice_type, desc: 说明}}`。只收录 seed-tts-2.0 音色
（`ICL_uranus_*_tob` 角色扮演 + `*_uranus_bigtts`），三个分层语义：

- **★精选·角色扮演（ICL）**：带人设、多情感，剧情对白**默认首选**（猫箱/豆包同款）；
- **旁白/独白·口播**：Vivi/云舟等通用口语音色，人设弱，只做旁白/解说；
- **⚠️有声阅读/播音腔**：擎苍/少年梓辛等番茄朗读腔，慎用于日常对话（会显机械）。

关联：project.json 的 `voices` 表把角色名 → 这里的别名；`shots[].speaker` 填角色名即自动
解析。别名不在表里时按 voice_type 原样透传。**引擎内嵌了一份精简别名兜底表——两边一致性
由 `engine/tests/test_config_drift.py` 强制守卫**（改本文件后跑一次单测即知是否分叉）。
⚠️ 本表是火山体系专属：若把模版 TTS 换到 MiniMax，用了别名的角色要重新选角
（引擎对火山系音色误传 MiniMax 有自动降级防呆，但音色会变）；定制音色不经此表。

## secrets.yaml —— 密钥（gitignore）

**不用自己创建这个文件**：`setup`（含 `--check`）发现缺失时会从 `secrets.example.yaml`
复制一份全空的 `secrets.yaml`，打开填值即可（`config_overlay.ensure_secrets_yaml`，
已存在则原样不动，绝不覆盖已填的 key）。不想编辑文件就用
`python3 -m kinema config secret <KEY> <值>` 写进 `secrets.local.json`。

**读取优先级：环境变量 > `secrets.local.json` > 本文件**，三层由
`config_overlay.file_secrets` 统一合并——**凡读密钥文件的代码都必须走它**，
就地 `yaml.safe_load(secrets.yaml)` 会漏掉本机那一份（守卫见 `test_config_center`）。

> **两份密钥文件不同步是设计，不是 bug。** 它们是有序的两层而不是两份副本：读取时
> 已经合并，所以在哪一层填都生效，不需要保持一致。加同步会把优先级倒过来——
> 编辑 yaml 就回写 local.json，网页填的 key 被旧值静默冲掉。也别合并成一份，
> 分层是「可入库的那份整份可传、不需要剔除密钥字段」的前提。

| 密钥 | 用途 |
|---|---|
| `ARK_API_KEY` | 图像 Seedream + 视频 Seedance（火山方舟，一把通用） |
| `ARK_TTS_API_KEY` | 配音 seed-tts-2.0 + 音频生成 seed-audio-1.0（火山语音，**独立凭证，不是 ARK_API_KEY**） |
| `ELEVENLABS_API_KEY` | BGM（**为空自动降级本地曲库 `music/`**，零成本） |
| `VOLC_ACCESS_KEY` + `VOLC_SECRET_KEY` | 口型精修（火山智能视觉 AK/SK，**与 ARK_API_KEY 不是同一套**；为空时增强步点名跳过，对白镜按底片口型出片） |
| `MINIMAX_API_KEY` + `MINIMAX_GROUP_ID` | 配音备选 |
| `GEMINI_API_KEY` | 图像 Nano Banana + 视频 Veo 3.1（Google，一把通用；无国内端点） |
| `DASHSCOPE_API_KEY` | 图像通义万相（阿里云百炼；国内/国际站 key 相互隔离） |
| `KINEMA_MYSQL_PASSWORD` / `KINEMA_OSS_ACCESS_KEY` / `KINEMA_OSS_SECRET_KEY` | 存储层凭证（见 storage.yaml） |

## storage.yaml —— 持久化

**文档层** `backend:`：`local`（默认，JSON 即数据库，零依赖）/ `mysql`（库为持久层+恢复源，
本地 JSON 是工作副本，保存双写；读取协调"新者赢"）。源码仓库内两种后端始终共用仓库根
`project/` 工作区，不因切换后端改变路径。临时切换不改文件：
`KINEMA_STORAGE_BACKEND=local|mysql`。
**媒体层** `media:`：`local` / `oss`（aliyun/tencent/volcengine/mock），对象 Key=前缀/工作区
相对路径，`oss sync` 上传改写 URL、`oss pull` 拉回。管理命令：`db status|init|sync|pull|schema`、
`oss status|sync|pull`。

## templates.yaml —— 平台规格模板

`templates: {模板名: {label, platform, aspect, motion(a/b/c), profile, episode{minutes,shots 区间},
series{episodes 或 total_minutes 区间}, notes(分账口径)}}`。
用法：`project new --template douyin_manju` 一键落位（规格快照入项目）→ 交付前 `spec check <项目>`
逐章验收。与引擎内置同名模板以本文件为准。

## branding.yaml —— 白标 + 动态水印

三键换牌：`name`（品牌名）/ `tagline`（口号）/ `accent`（#RRGGBB 主题色，非法值自动忽略）——
Studio 大屏、提案书、审阅包页脚同步生效，代码零改动。

`watermark:` 段是**防搬运动态水印**的全局默认（`kinema watermark` 命令消费）：
`text`（默认文案，留空=无全局默认）/ `opacity`（透明度）/ `size`（字号，缺省按画面高度
2.2% 自适应）/ `speed`（漫游基准速度，画面尺寸的 %/秒；碰壁反弹在 0.6~1.3 倍内随机变速）/
`fade`（入场淡入秒数，仅一次；此后全程在场连续漫游）/ `color`（黑色柔和投影，亮暗场景皆可读）。
运动模型：连续弹性漫游——匀速直线、碰画面边界随机角度反弹，永不消失、零瞬移。文案解析链：
CLI `--text` > project.json `watermark` > 此处 `text`；产出 `<id>_wm_<比例>.mp4` 与原片并存。

`watermark_fixed:` 段是**固定角标水印**（品牌署名·字幕式烧录·清晰不透明·比字幕小四号·钉四角）：
`text`（角标文案）/ `position`（tl/tr/bl/br 四角）/ `size`（缺省=字幕字号×0.52）/ `opacity`/`color`/
`font`（字体路径）。与漂移水印可同开。解析链：CLI `--corner-text/--corner-pos` >
project.json `watermark_fixed` > 此处。

**字体全内置·免费商用·零系统依赖**：字幕/水印/角标/封面字卡都用工程内置字体
（`engine/kinema/assets/fonts/`，真源 `fonts.py` + 该目录 `NOTICE.md`）——阿里普惠体（黑体）+
思源宋体 SC（宋·国风）+ 霞鹜文楷 Lite（楷·古风）+ 得意黑（展示体），全部免费可商用、换机/换系统
一致。换字体：`watermark_fixed.font` 填字体绝对路径 / profile `subtitle.font` 填族名。

---

## 环境变量速查

| 变量 | 作用 |
|---|---|
| `KINEMA_MODELS` | 显式指定 models.yaml 路径 |
| `KINEMA_CONFIG_OVERLAY` | 模型配置覆盖层文件路径；取 `""`/`0`/`off`/`no`/`none`/`false` 则**整层禁用**（单元测试与「只想跑仓库配置」时用；密钥文件跟随同一开关） |
| `KINEMA_WORKSPACE` | 工作区数据目录（默认仓库根 `project/`；传仓库根或 `engine/` 会归一到该目录） |
| `KINEMA_STORAGE` / `KINEMA_STORAGE_BACKEND` | storage.yaml 路径 / 文档层后端临时覆盖 |
| `KINEMA_MEDIA_BACKEND` | 媒体层后端临时覆盖 |
| `KINEMA_MUSIC_DIR` | 本地音乐库位置（默认仓库 `music/`） |
| 各 `*_API_KEY` 等 | 见 secrets.example.yaml（优先级恒为 环境变量 > secrets.local.json > secrets.yaml） |
| `WEREAD_API_KEY` | 微信读书官方 Agent Gateway（**kn-book 指挥层取料专用，引擎不读**）；申请入口 weread.qq.com/r/weread-skills，未配置时 kn-book 不阻塞，自动降级公开页取料并附一次配置提示 |

## audio.yaml —— 音频资产总注册表

「场景 → 目录/标准文件名」一张表：`music/` 库两子库（`bgm/` 背景音乐 + `sfx/` 音效）。
- **bgm 段**：情绪 → `dir`（子目录，目录内任意 mp3 确定性选曲）/ `desc` / `keywords`
  （profile 未指定 `music.mood` 时按提示词/画风关键词兜底匹配）——三字段必填。
- **sfx 段**：分类 → 语义键 → `file`（标准文件名）/ `desc`（检索场景说明）/ `license`
  （合规登记）——三字段必填。**三级声源解析**：B 注册且文件存在 → 直接混入；
  A 缺文件 → 纯 ffmpeg 合成兜底（零依赖内核不破）；C 用户点名
  `sfx gen --kind <键> --yes` AI 生成落库后回到 B。
顶层 `root` 定库根（缺省 music；`KINEMA_MUSIC_DIR` 整体改址、`KINEMA_AUDIO_CONFIG`
换注册表）。起始资产 `python music/download.py` 一键两套（**全部 CC0/公共领域·免署名可商用**：
BGM 103 首 + 音效 18 枚；署名类/付费源不进库也不留下载逻辑），授权登记 `music/ATTRIBUTION.md`。与内嵌缺省 `EMBEDDED_AUDIO`
（engine/kinema/audio_registry.py）的一致性由 `test_config_drift` 守卫——
**加情绪/加键请两处同改**；仅换素材文件则只动 music/ 目录，零代码。

## 改完配置后怎么验证

```bash
cd engine
python3 -m kinema doctor                        # 配置源/后端/providers/音乐库一屏自检
python3 -m unittest tests.test_config_drift -v     # 漂移守卫：内嵌一致性/双语前缀/默认入口
python3 -m kinema run --chapter demo/<章节> --mock   # 零成本全链路验证
```

> 注意：模块内有配置缓存——同一进程里改环境变量后需重启进程（或 `load_storage_config(reload=True)`）。
