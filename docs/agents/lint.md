# 分镜单调度体检 lint——维度全表与判据

```bash
python3 -m kinema lint --chapter <项目id/章节id> [--strict]
```

纯计算 · 不落盘 · 零成本。

## 1. 主要维度

本表只列与生产纪律直接相关的维度；全部 40 维的告警码与阈值以 `pipeline/variation.py` 的 `_DIMENSIONS` 为准，`lint` 的实际输出即完整清单。

### 1.1 场景与镜头语言

| 维度 | 告警码 | 判据 |
|---|---|---|
| 场景连续性 | `scene_unanchored` | 无取景地锚 |
| 场景连续性 | `scene_jump` | 无转场直切 |
| 相邻运镜雷同 | — | 相邻镜运镜重复 |
| 情绪缺失单调 | — | 情绪字段缺失或全片同一情绪 |
| 景别分布 | — | 景别过于集中 |
| `hero_moment` 分布 | — | 高光镜分布失衡 |

### 1.2 画面描述

| 维度 | 告警码 | 判据 |
|---|---|---|
| 反 slop 空词 | — | 命中空词表，给物理化改写建议 |
| 抽象情绪词 | `emotion_abstract` | 画面描述把情绪写成名词——表演物理化纪律 |
| 画面代词 | `prompt_pronoun` | 画面字段用「他/她/它」——设定图挂载按 name/keywords 文本命中，代词挂不上图。扫描前剥掉引号内文本：画面字段原文引用台词（说出「她明天出院了。」）是人话不是画面 |
| 运镜互斥 | `camera_clash` | `camera` 与 `video_prompt` 谈摄影机的小句各写了一个互斥运镜。小句内排在运镜词前面的排除措辞（不做/不要/绝不/禁止/避免…）使该命中不计——「特写不做任何环绕」是在排除运镜；只认明确的排除词，不收单字「不」 |
| 提示词过薄 | `prompt_thin` | 画面提示词连同 framing/angle/lens/lighting 不足 `MIN_IMAGE_PROMPT_CHARS`，或运动提示词连同 delta 骨架与 camera 不足 `MIN_VIDEO_PROMPT_CHARS` 即报（warn）：写不满说明这一镜还没想清楚。静图档只写 `camera` 不算写了运动稿（那是 Ken Burns 的风格键），写了 `video_prompt`/delta 才判并另出 `prompt_thin_mode`（info）提醒它一旦切动镜档就会原样发出 |
| 名册外实体 | `unregistered_entity` | 镜级 `characters/props/scenes` 点名了设定集里没有的名字即报（warn）：设定图挂不上，模型对着这个名字自由发挥——建进设定集或改用注册名 |
| 预设填空位残留 | `preset_placeholder` | 运镜预设里留给作者替换的 X/Y 填空位原样残留在提示词里即报（warn） |
| 工艺痕迹外泄 | `craft_leak` | 交付文本里混进版本号、文件名、判例号等只有制作方看得懂的内容即报（warn）：模型看不到上一版，意见要改写成这一版要什么 |
| 运动提示词写成禁令清单 | `prompt_negation` | `video_prompt` 过半分句在说「不要做什么」——与 `prompt_thin` 互补：那条只数字符，数不出「120 字里 70 字是禁区」。禁令该走 `negative_prompt`（引擎编译成肯定式约束句拼在末尾），挤在正文里既占动作 token 又与运镜抢注意力。只在 native/dubbed 判 |

### 1.3 旁白

| 维度 | 告警码 | 判据 |
|---|---|---|
| 占位旁白 | — | 旁白仍是占位文本 |
| 旁白文风 · 抬价句式 | `narration_pivot` | — |
| 旁白文风 · 汇报腔 | `narration_jargon` | — |
| 旁白文风 · 名词化 | `narration_nominal` | — |
| 旁白文风 · 收尾宏大词 | `cta_grand` | — |
| 旁白文风 · 同连接词开头 | `narration_opener` | — |
| 旁白语态 · 解说腔漫剧 | `voiceover_heavy` | 剧情语态下 sparse 旁白镜占比 > 40% |
| 旁白语态 · none 语态 | `voiceover_heavy` | none 语态有旁白即报 |
| 旁白语态 · 无纯画面镜 | `no_silent_shot` | 全片无纯画面镜 |
| 字幕一次摊完 | `subtitle_dump` | 单段镜（未写 `lines[]`）的台词里出现第二句即报（warn） |
| 台词超窗 | `narration_overrun` | 进旁白轨的镜按在用音色档案的实测语速（`voice_bank.casts[].speech_rate`）预估配音时长，超出 `dur × voicecast.FIT_TEMPO_WARN` 即报（warn）；说话人档案无语速时不估，模型自声的对白镜不估 |
| 全章预计时长 | `chapter_length_estimate` | 进旁白轨的镜按同一份实测语速估配音秒数，加上会折进 dur 的停顿，汇总为全章预计并附作者 dur 合计（info）；引擎不知道目标时长，只在花钱前把估算摆出来。任一说话人无带语速档案不估。语速带 `pace_dense`/`pace_sparse` 的分母同样扣掉停顿——气口不是语速 |

语态缺省由 `skills.py` 派生，顶层 `voiceover` 声明凌驾。

`subtitle_dump` 的由来：`subtitle.shot_events` 对单段镜只发一条 Dialogue、横跨整镜时长，
多句塞一段即"念第一句时后几句已在屏幕上"，且超行宽后自动折行按字数硬切、会把收尾引号
甩到行首。处置是拆 `shots[].lines[]`（画面不拆），详 `native-voiceover.md` §5.4。
断句真源 `variation._SENTENCES`：句末标点后紧跟的收尾引号并入前句，**省略号不作句末**
（「某种沉睡了十万年的存在……正缓缓转过头来」是一句话的停顿，断在这里会把每个悬念号都报成一句）。

### 1.4 节奏

| 维度 | 告警码 | 判据 |
|---|---|---|
| 视觉换挡间距 | `shift_gap` | 连续超 30s 无结构化位换挡即点名区间（info 级） |
| 碎切 | `montage_chop` | dubbed/native、正镜 ≥4 且 dur<6s 占比 >60% 即报（warn）：生成式片段镜间恒硬切，短镜密集=截断感逐镜累积；主戏镜正路是 8~15s 长镜 + beats 节拍串，3~6s 留给 punch |
| 悬空字幕 | `caption_voiceless` | dubbed/native、片内既有人声镜又有挂 `caption` 的无声镜即报（warn）：有人声的镜字幕逐字取台词，观众两三镜就把「底部出字」读成「有人在说话」，无声镜的 caption 于是读成漏了配音。二选一——删 caption 或给它配旁白。kenburns 不判，静图片本就靠字卡叙事 |

### 1.5 模式组合

| 维度 | 告警码 | 判据 |
|---|---|---|
| scored × dubbed | `scored_dubbed_conflict` | 硬冲突：对口型人声由逐镜 TTS 喂入，scored 由音频模型整轨生成——合成时片段音轨被整轨替换；`gen-video` 入口另有硬闸拒发 |
| scored × native 对白 | `scored_native_dialogue` | 有非旁白 speaker 的镜即点名：片段口型对着模型自配（将被整轨替换）的语音动，观众听到的人声与口型不同源 |
| native 人声来源 | `native_voice_unverified` | native 且非 scored、模型声源的镜有台词即报（章节级一条；混烧下旁白镜的人声是烧录 TTS 与字幕同源，只数对白镜）：人声由视频模型念出而字幕按章节文本编译，未核对不能当一致——verify 的 ASR 人声文字核对是核对出口 |
| 混烧 × 同镜对白+旁白 | `burn_mixed_narration` | native 且 `native_voiceover` 开、对白镜里夹带旁白句即报（warn）：对白镜整镜由模型发声，那几句旁白换成模型嗓音、与烧录的固定音色旁白不同源——旁白句挪进纯旁白镜 |
| dubbed × 对白上镜 | `dubbed_dialogue` | dubbed 且有对白镜即报（warn·章节级一条）：烧录轨与模型口型两条时间轴不同源，开口对齐只做整体平移、多句/多人镜必然失配——对白上镜章走 native+锚定，dubbed 领地是全旁白解说章 |
| 已选角 × 未绑定说话人 | `voice_anchor_gap` | native 章节已有说话人绑定音色，仍有开口的角色或旁白没有音色引用即报（warn）：这些台词由模型每镜自选嗓音，跨镜必然漂移——`character set --voice-prompt` / `voice custom --narrator --adopt 1` 补齐 |
| 空镜 × 全员兜底 | `empty_shot_cast` | 画面写「无人/空镜」而镜级 `characters` 键缺失即报（info）：键缺失=全员出场，设定图与绑定句照常注入、与画面声明打架——显式 `characters: []` 才是「明确无人」 |

前两条只在 `audio_mode=scored` 下判。

### 1.6 交付缺口

| 维度 | 告警码 | 判据 |
|---|---|---|
| 章节封面缺位 | `cover_missing` | 全部正镜都已有 `image`、章节文档 `cover` 仍为空即报（章节级一条）：Studio 卡片图源退到成片海报帧或分镜图兜底 |
| 取景地缺俯视图 | `topview_missing` | 具名场景只有基准图、没有俯视布局图即报（warn）：视频请求拿到的空间证据缺一半——`project refs <项目>` 只补缺的图纸 |
| 取景地时段缺口 | `scene_daypart_missing` | 具名场景 `desc` 未命中时段词表即点名（info 级）：基准图会自选一个时段画进去，之后全链路把它当光线基准——写实档降级路线上更直接顶 `@图片1` |
| 角色外貌疲态 | `character_fatigue_look` | 章节 `characters[]` 的 appearance/role/outfit/hair/silhouette_notes 命中疲态词表（黑眼圈/眼袋/憔悴/血丝…）即点名（warn 级）：缺省角色气色健康、神态有精神，疲态只在用户点名时写；命中词登记进 `visual_requirements` 视为显式表态、不计。判据 `variation.fatigue_look` 与 `character add/set` 提醒、`project refs` 出图闸共用 |

判据钉在「图齐」这一刻：生图之前催是催早了（软闸每轮生图都跑，噪音淹真告警），
成片之后才想起来则整个制作期的项目卡与章节卡都缺主视觉。lint 是纯函数只认文档
`cover` 块，系列主视觉的缺口由 `gen-image` 收尾的 `_warn_cover_missing` 查盘点名
（两处判据互补，详见 [`cover.md`](cover.md)）。

`scored_dubbed_conflict` 现在有第三道更早的闸：Studio 章节页的音频剧本台在**表态那一刻**
就拦住 tracks→scored（`audioScriptCard` 的 `dubLock`，判据与 `gen-video` 硬闸同源），
免得切过去、写完剧本、点生成才撞墙。只拦这一侧——盘上已经错配成 scored 的 dubbed 章节
要能切得回来，此时改由卡内告警给出两条出路。

`native_voice_unverified` 的定位：提示词已经把台词逐字发给模型
（`prompts.native_voice_clause`），但模型照不照办没有确定性保证。核对出口是 verify 的
ASR 人声文字核对（`mediacheck.native_voice_check`），也可以合成后听一遍确认。
全旁白章开 `native_voiceover: true` 后旁白改由固定音色烧录，本条随之归零；含对白的章
只有旁白镜脱离点名集合，对白镜仍由模型发声。字幕没有关闭开关，故此条不受字幕配置影响。
verify 核过的镜（`verify.voice.rows[].id`）不再计入，这条告警才有终态；该镜重生后快照失配、
结论作废时它自然重新出现。

## 2. 阈值来源

阈值由章节顶层 `art_direction{variety, motion, density, avoid}` 旋钮驱动——**只改告警永不改画面**。
旋钮 → 阈值映射的单一真源是 `pipeline/variation.py`。

## 3. 与 gen-image 的关系

`gen-image` 派活前自动跑一次同款软闸（只提示不阻断）。

## 4. 与 gen-video 计费前闸的关系

lint 是分镜阶段的软闸，报了不拦。`gen-video` 在计费前另有两道**同源**的质量闸
（`cli._gate_frame_aspect` / `cli._gate_voiceover`，守卫 `tests/test_prespend_gates.py`）：

- **旁白语态**与 lint 的 `voiceover_heavy` 共用判据 `variation.voiceover_overrun`
  （语态、阈值 `VOICEOVER_HEAVY_RATIO`、样本下限 `VOICEOVER_MIN_SHOTS` 都只有一处说了算）——
  两处各写一份就会出现「lint 说没超、闸说超了」；
- **分镜图比例**复用素材体检的 `mediacheck.aspect_overflow` + `SUPPLY_ASPECT_TOL`，
  只有后果措辞不同：Ken Burns 下是 cover 取景裁掉主体，图生视频下是模型必须重新构图、
  审过的那一帧不会是成片画面。

两道闸都**不硬拦**（都属于要不要接受的取舍，不是买不得的组合），且**非交互环境里
既不替用户中止、也不替用户确认**——只把事实说清并照常发出。
