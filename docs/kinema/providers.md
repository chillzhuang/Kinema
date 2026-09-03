# 能力层 API 速查（providers.md）

> 调研日 2026-07-15，2026-07-16 复核四家备选并接入。价格/条款处于快速变动期，**落地前务必以各家官方定价页/Console 实时值复核，
> 并用自己的中文素材实测**。
> ✅=推荐默认 · ◎=国产/省钱备选 · △=特定场景 · ✗=不采用
>
> **已接入引擎的 provider 别名（`status: ready`，清单与单价以 `config show` 输出为准）**：图 `seedream` / `nano-banana` / `wan` /
> `minimax-image` / `agent` · 视频 `seedance-mini` / `seedance-2.0-fast` / `seedance-2.5` / `veo` /
> `minimax-h3` · 配音 `seedtts` / `doubao` / `minimax` · 音乐 `elevenlabs` / `minimax-music` / `local` · 口型 `volc-lipsync`；
> 另有 `minimax-h3-local` 为 `status: planned` 登记位（点名会给出清晰报错，不会静默）。
> ⚠️ dubbed 的配音参考音（ref_audio）仅 seedance 系列别名支持——`veo`/`minimax-h3` 收到 ref_audio
> 会显式报错导向 seedance。

---

## 1. 图像生成（分镜帧 · 风格一致性）

| 模型 | 官方 API | 单图价(约) | 多图参考 | 角色一致 | 商用授权 | 选型 |
|---|---|---|---|---|---|---|
| **Nano Banana Pro**（Gemini 3 Pro Image） | ✅ Google | $0.134(1-2K)/$0.24(4K) | 6物+5人+3风格 | **最强之一** | 付费档商用，SynthID 隐形水印 | ✅ 效果优先 · **已接入 `nano_banana`**（模型 ID 用 gemini-3-pro-image，-preview 串 2026-06-25 已关停） |
| **Seedream 4.x**（字节） | ✅ BytePlus/火山 | 平台定价，~¥0.2 级 | 6~10 | 强（一次≤9张一致组图，2K≈1.8s） | 平台付费含商用，中文渲染最强 | ✅◎ MVP 默认 |
| FLUX.2 pro（BFL） | ✅ BFL | 按 MP，~$0.07 | ≤10 | 强 | **API 全量商用授权最干净**，支持 LoRA | ✅ 编辑迭代 |
| FLUX.1 Kontext | ✅ BFL | 信用制 | 单图为主 | 强（指令编辑） | API 全量商用 | △ 局部改图 |
| 通义万相 wan2.7 | ✅ 阿里云百炼 | 0.2 元/张，新用户 50 张免费 | 0~9 张参考图 | 中强 | 国产合规商用，4096²，异步 | ◎ 国产性价比 · **已接入 `wan`**（wan2.7-image 新版 messages 协议，产物 URL 24h 时效） |
| Nano Banana 2 Lite | ✅ Google | ~$0.034 | ✅ | 中强 | 付费档商用 | △ 便宜量产 |
| 混元生图 Hy3 | ✅ 腾讯云 | ~0.5 元/张 | 弱 | 中 | 国产商用（正迁移 TokenHub） | △ |
| Ideogram v3/v4 | ✅ | $0.03~0.20 | 角色参考(加价) | 中强 | 付费商用，排版/文字最强 | △ 海报/文字 |
| Recraft v3/v4 | ✅ | 光栅$0.04/矢量$0.08 | 品牌风格训练 | 品牌风格强 | 付费商用，原生矢量 | △ 品牌系统 |
| SD 3.5 + 开源权重 | ✅ + 权重 | $0.035~0.08 | 靠 IP-Adapter 等 | 靠 LoRA | <$1M 免费商用；SDXL/1.5 无上限 | △ 自建管线 |
| Adobe Firefly | ✅ 企业($1k/月起) | $0.02~0.10 | 一般 | 一般 | **合规训练+企业IP赔付+C2PA** | △ 版权零容忍兜底 |
| Midjourney | ✗ 无公开 API（仅企业申请） | — | sref/cref | 强（仅UI） | 第三方封装违反 TOS，封号风险 | ✗ 不采用 |

---

## 2. 视频生成（image-to-video · 运动）

| 模型 | 官方API | 首尾帧 | 参考图 | 原生音频 | 最长/最高 | ~5s片 | 异步 | 选型 |
|---|---|---|---|---|---|---|---|---|
| **Veo 3.1 Fast/Lite**（Google） | ✅ Gemini/Vertex, SDK | ✅ | ≤3 | ✅ | 8s(可延)/**4K** | **Lite $0.05/s** | 仅轮询 | ✅ 带音频性价比第一（Pre-GA，商用条款需复核）· **已接入 `veo`**（3.1-fast；时长枚举 4/6/8s，非 720p 强制 8s，产物 2 天时效，无 dubbed） |
| **Seedance**（字节） | ✅ 火山/BytePlus | ✅(三模式) | ✅多镜头 | 1.5/2.0 ✅ | 15s/4K(2.0)·30s/1080p(2.5) | **1080p ¥5（¥1/s）** | 轮询 | ✅◎ 主力已接入 `seedance`；2.0 控制台实价 1080p ¥1/s·4K ¥2/s（4K 并发独享 1）；企业商用需申请授权(3~7天)；**参考音频可作音色锚定**（2026-08 mini 实测：嗓音跟随样本、双音频按编号分绑角色/旁白成立，2.0 系列 ≤3 条/合计 15s，超限 400——见 `docs/agents/voice-anchor.md`） |
| Vidu | ✅ REST | ✅ | **≤7** | Q3 ✅ | 16s/1080p | $0.40 | 轮询+**webhook** | △ 固定角色跨镜头一致最佳 |
| Sora 2 / Pro（OpenAI） | ✅ Videos API, SDK | ✗(有续接) | ✅ | ✅ | 20s/1080p | $0.10/s | 轮询+**webhook** | △ 长片+IP赔付；真人肖像限制最严 |
| Wan 通义万相 | ✅ DashScope, Py/Java SDK | ✅(kf2v) | ✅ VACE | 2.5+ ✅ | 15s/1080p | ¥0.6~1.0/s | 仅轮询 | ◎ **2.1/2.2 开源 Apache2.0 可自托管** |
| Runway Gen-4.5 | ✅ 独立门户, SDK | ✅ | ✅ | ✗ | 10s/720p(升4K) | $0.25(Turbo) | **仅轮询** | △ 被审仍扣费、默认拿数据训练 |
| Luma Ray 3.2 | ✅ REST, SDK | ✅ | ✅ | ✗ | 10s/4K | Flash $0.24 | 轮询+webhook+**失败退款** | △ |
| Hailuo 海螺 | ✅(双区域, key 不互通) | ✅ | ✅ S2V | H3 ✅ 立体声 | H3 15s/2K | H3 2K $0.13/s | 轮询+webhook | △ H3 已接入 `minimax-h3`（全模态 v2：参考图 ≤9/视频 ≤3/音频 ≤3，音频官方语义即音色参考） |
| Hunyuan 混元 | ✅腾讯云+开源 | 仅首帧 | ✅ Avatar | ✗(需 Foley) | 10s/1080p | fal $0.40 | 仅轮询 | ✗ 社区许可**排除 EU/UK/韩** |
| Pika 2.2 | ✗ 无官方，走 fal | ✅(2~5帧) | ✅ | ✗ | 10s/1080p | $0.20 | fal队列+webhook | △ |

**要点：** 原生音频（单次生成即带对白/音效）仅 Veo 3.x、Sora 2、Seedance 1.5+/2.0/2.5、Wan 2.5+、
Vidu Q3、MiniMax H3 支持；其余静音。**kenburns 档不接视频生成，用 FFmpeg Ken Burns 运镜。**

> ⚠️ **Seedance 拒收含照片级人脸的参考图/首尾帧**（2.5 与 2.0 全系同策略，报
> `InputImageSensitiveContentDetected.PrivacyInformation`）。判据是**写实度**不是来源——
> AI 生成的写实人脸照样被拦，2D/风格化则普遍可过。合法通道、能力位配置与排错
> 全在 → [`seedance-face-policy.md`](./seedance-face-policy.md)。**接 Seedance 前先读它。**

---

## 3. 语音合成 TTS（配音）

| 模型 | 中文自然度 | 克隆 | 词级时间戳 | API/计费 | 商用 | 选型 |
|---|---|---|---|---|---|---|
| 豆包音频生成 seed-audio-1.0 | 顶级（有表现力） | ✅ 声线描述/参考音频/参考图 | ✅ subtitle.sentences | 非流式 `/api/v3/tts/create`；≤120s | 邀测中，官方未公布单价 | ✅ **主力（定制音色：角色与旁白缺省路径；音频剧本）** |
| 豆包语音合成大模型 seed-tts-2.0 | 顶级（固定音色） | ✅ speaker 固定音色 | ✅ enable_subtitle | **HTTP SSE** `/api/v3/tts/unidirectional/sse`；`X-Api-Resource-Id: seed-tts-2.0` | 字符版按量 3 元/万字符 | ◎ 模版音色（显式例外：`voice audition` → `voice use`） |
| **MiniMax Speech 2.8** | 顶级（中英混读<250ms） | 5s 零样本 | ✅ subtitle_enable | t2a_v2；~$60~100/M字符 | 明确，可私有化 | ◎ 备选 |
| 阿里 CosyVoice 2/3 | 强（含方言） | 3s 零样本 | 部分 | **本地部署免推理费** | **Apache-2.0 可商用** | ◎ 自托管零授权 |
| Azure AI Speech | 强 | Custom Neural | ✅ | Neural $16/M；HD $22/M；F0 免费50万字/月 | 企业级 SLA | △ |
| OpenAI gpt-4o-mini-tts | 一般 | ✗ | 部分 | ~$0.015/分钟（最便宜之一） | 可用，音色固定 | △ |
| ElevenLabs v3/Flash | **中文偏弱** | Instant+Pro | ✅ | $0.10/千字(v2/v3)、$0.05(Flash)；免费档无商用 | 付费档商用 | △ 英文才用 |

> 两条路的分工：**定制音色**走 `seed-audio-1.0`（provider `doubao`），档案那条不可变音频作参考音、
> 描述原话进剧本正文，逐句合成靠这两道锚定；它同时是 native 锚定音与绑定句描述的唯一来源。
> **模版音色**走 `seed-tts-2.0`（provider `seedtts`）：uranus 2.0 固定 speaker、同 speaker 零漂移，
> 接口 `POST /api/v3/tts/unidirectional/sse`，SSE 事件流，`event=352` 的 `data.data` 为 base64 音频分片按序拼接；
> resource-id 走 **`seed-tts-2.0`（字符版，不占并发额度）**，不是 `volc.service_type.10048`（并发版）。
> 鉴权：新版控制台单头 `X-Api-Key`(ARK_TTS_API_KEY)，旧版双头 `X-Api-App-Id`+`X-Api-Access-Key`。
> **TTS 用独立语音凭证，不是 ARK_API_KEY**(控制台 console.volcengine.com/speech)。
> 音色列表见 `config/voices.yaml`（170+ 个 2.0 固定音色）；语速用 `req_params.audio_params.speech_rate`（project 顶层 `speech_rate`）。

**结论：中文口播优先国产（豆包/MiniMax），英文角色配音才考虑 ElevenLabs。** 字幕对齐优先用 TTS 返回的词级时间戳。

---

## 4. 字幕对齐

| 方案 | 说明 | 选型 |
|---|---|---|
| **TTS 词级时间戳 → SRT/ASS** | 豆包/MiniMax/Edge-TTS/ElevenLabs 均支持；无需 GPU、与配音天然同步 | ✅ 首选 |
| WhisperX | faster-whisper + wav2vec2 强制对齐，词级 ±50ms；比原版 Whisper 快 ~70×；`--highlight_words` 出逐字高亮；BSD-2 | △ faster-whisper 已接入（可选依赖 `pipeline/asr.py`）：native 多句镜字幕逐句划界与 verify 人声文字核对；WhisperX 词级强制对齐未接入 |
| faster-whisper / stable-ts | 单用，段级或更稳时间戳 | △ |
| MFA（Montreal Forced Aligner） | 学术级强制对齐 | △ |

---

## 5. 背景音乐

| 方案 | 官方 API | 商用/版权 | 选型 |
|---|---|---|---|
| **ElevenLabs Music** | ✅ REST/WS | 源头授权(Merlin+Kobalt)，付费档商用清洁；~$0.80/分钟；Starter $6/月 | ✅ |
| 正规免版税曲库 | — | 明确授权，避开 Content ID | ✅◎ |
| MiniMax Music 2.5 | ✅(经 fal) | 需查条款；~$0.035/次 | △ 平价 |
| Suno v5.x | ✗ 无官方 API | 编曲最佳但 Sony 诉讼未结、归属不确定、免费档零商用权 | ✗ |
| Udio | 围墙花园 | 内容不可离开平台 | ✗ |
| 字节海绵音乐 | ✗ 无对外 API | 个人作品普遍不可商用 | ✗ |

**通用合规：** 主流流媒体 2025 末起要求披露 AI 生成，不披露可能下架。

---

## 6. 视频合成引擎

| 引擎 | 模板化 | 可编程 | Ken Burns | 授权风险 | 选型 |
|---|---|---|---|---|---|
| MoviePy 2.x + FFmpeg | 中 | ★★★★ | zoompan/手写插值 | 低(MIT) | △ 立项候选，未采用——引擎保持零 Python 依赖 |
| **FFmpeg（裸）** | 弱 | 中(filter_complex) | **zoompan** | 低(注意 GPL) | ✅ 实际落地：运镜、转场、混音、字幕全走 filtergraph |
| Remotion | ★★★★★(React) | ★★★★★ | interpolate | **高：≥4 人公司必付费**（Automator $100/月起） | △ 需极致模板化时 |
| json2video/Creatomate 云 API | 强 | 强(REST,有 MCP) | 关键帧 | 按量+厂商锁定 | △ 不想运维 |
| Editly | 中 | 中 | 内置 | 低但近乎停更 | ✗ |

