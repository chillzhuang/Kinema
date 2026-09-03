# native 配音混烧与镜内多角色时长语义

## 1. native 配音混烧

把我们的固定音色压上 Seedance 原生音轨。

### 1.1 混音结构

混烧开启后（章节 `native_voiceover: true`，或 `assemble --burn-voice` 只开这一次），`assemble`
把 `tts` 出的旁白轨作**主音轨**（0 dB），片段原生音轨占
**背景床**槽位（BGM 母线）。

床压制（降 `mixdown.NATIVE_BED_GAIN=0.4` + 让路 EQ）**按旁白镜窗口门控**
（`clip_bed_track(bed_windows=)`，窗口取自 timeline 的 voiceover 镜）：声源按镜
分治后这条轨在对白镜窗口里是主人声，整轨静态压制会把它压低 8dB 还挖中频，
对白比旁白明显发虚。sidechain 闪避留在 premix 整轨挂——它由旁白轨驱动，
对白镜窗口里旁白轨本就是静音，天然不触发；床轨自带窗口 EQ 时 premix 以
`bed_eq=False` 关掉整轨 EQ，否则窗口内双重挖频。

### 1.1a 混烧的声源按镜分治

分治判据是 `voicecast.burn_muted`（单一真源）：**旁白/无词镜闭声出演、人声走烧录
轨；对白镜由模型原生发声、锚定照常附发**。同一章内角色恒是模型声、旁白恒是固定
音色——说话人级单声源。整章一刀切闭声不可取：闭声稿的执行没有确定性保证
（同稿实测两发一守一破），而对白镜若靠烧录，烧录轨与模型口型两条时间轴不同源，
开口对齐只做整体平移、多句/多人镜必然失配。

旁白镜若照常发「讲述」措辞，实测形态是成片同一段两个人声：背景床只降 -8 dB
（对环境声正确、对同句台词的另一个演绎不够），sidechain 闪避只在我们的音轨出声时
触发，模型把同一句挪到句间静音段念时一次都压不住；音色锚定又是声区跟随非复刻，
第二个人声必然是另一个音色，听感极差。

配套判据（四处同用 `burn_muted`，缺一处页面/清单与实发就分叉）：

- 提示词编译：`cli` 逐镜传 `native_mute`，`prompts.video_prompt` 内部再按
  `burn_muted` 兜底——对白镜即便被标了 mute 也按发声编译，人声地板不下发；
- 锚定附发：`cli._anchor_plan_for` 与 `scanner` 的 `_va_view` 只排除闭声镜，
  对白镜锚定照常；
- 旁白轨拼接：`voicecast.narration_parts` 对 native 的对白镜恒插等长静音——
  盘上有 wav 也不接入（陈旧产物烧进去即双人声）；`stage_tts` 在 native 下
  不给对白镜合成；
- `assemble` 混烧前扫描**旁白镜**的 `gen.clip.envelope.positive`：正文缺
  `prompts.positive_is_voiceless` 认的闭声记号即**拒合成**
  （`compose._gate_native_double_voice`，双人声成片没有可交付的形态）。白名单
  方向，认不出的稿一律拦——出声句里的 `@配音N` 位序标记会把措辞切开，按措辞
  枚举判在「旁白已选角」这条默认路径上恒放行；dubbed 期生成的旁白镜同样被拦
  （那条片段里模型重演了我们的配音）。两条出路——retake 重生（重生稿按闭声
  出演编译）/ 本次不烧；另对旁白镜
  片段做**输出侧人声探测**（`compose._warn_native_residual_voice`）——振幅判据
  分不清人声与响亮音效，只报不拦；
- `assemble` 混烧前另查旁白轨本身：有镜进旁白轨且有词、而 `audio.narration_file`
  不在盘时**拒合成**（`compose._gate_narration_track`），出路同为「先配音再合成 /
  本次不烧」。`needs_tts` 是镜级口径、不认混烧（混烧的对白镜按设计永远没有逐镜
  wav），配音阶段因此可以整章没跑过——这道闸守的就是那个初值态；盘上已有轨的
  半缺态归 `narration_parts` 的 missing 逐镜点名。

lint 配套：`burn_mixed_narration`（warn）点名对白镜里夹带的旁白句——对白镜整镜
由模型发声，那几句旁白会换成模型嗓音、与烧录旁白不同源，应挪进纯旁白镜；
`dubbed_dialogue`（warn）点名 dubbed 章的对白上镜（dubbed 领地是全旁白解说章）。

### 1.2 显式 opt-in，默认不烧

| 开关 | 作用域 |
|---|---|
| `assemble --burn-voice` | 本次 |
| 章节写 `native_voiceover: true` | 常开 |
| `tts --only N` | 只补跑/重跑点名的旁白镜；未点名的旁白镜缺 wav 时收尾按缺配音点名拒拼（对白镜本就不进旁白轨） |

混烧章的 `Project.needs_narration_track` 为真，`run` 因此会跑配音阶段；`stage_tts`
按 `voicecast.in_narration_track` 只给旁白镜合成，对白镜不产多余 wav。

### 1.3 为什么不做「零开关」

盘上有 `narration.wav` 不构成混烧依据——「有就自动混烧」的零开关实测被点名。

事故链条：章节原本是 kenburns/dubbed（这两种模式 tts 是标配），后来切成 native，`narration.wav`
原样留在盘上（**切 motion 不清 `audio.narration_file`**——全仓只有 `stage_tts` 写、`compose` 读两处），
assemble 照烧不误；compose 还会先 `_sync_narration` 把这条陈旧旁白按当前时间轴重拼对齐，于是它
「跟画面对得上」，只是凭空多一层人声、全程零提示。

现在盘上有配音却不烧时会打印一行说明（连同怎么开），**不删 `narration.wav`**——TTS 是花过钱的产物，
切回 kenburns 还要用。

### 1.4 四条纪律

1. **只在 native**——dubbed 的主音轨本就是我们的逐镜 TTS（片段里模型重演的人声
   不进成片，见 `compose.build` 里的 `use_clip_audio` 判定），没有可混烧的第二路人声。
   源级判据 `narration and project.native_audio`；
2. **native 下 `stage_tts` 绝不回填 `dur`**——那是向 Seedance 请求的计费秒数 / 片段实测时长，按配音
   实测覆写会把请求秒数与时间轴一起改坏；
3. `narration_parts` 的窗口分支（native/dubbed 共用——两者的窗口都由片段实测秒数决定、与 wav 可分离）
   按**窗口口径**铺（窗口 = `dur`，配音短则垫静音齐窗，超窗变速压入）。缺省不烧的 native 章里，
   **未配音的台词镜按窗口占静音且不进 `missing`**——人声整章由模型承担，那是常态不是错误，进了 missing
   会让 compose 自愈直接放弃重拼；混烧章的旁白镜与 dubbed 章缺 wav 则进 `missing`，由 tts 收尾与 compose 自愈逐镜点名；
4. 混烧保的是模型自带的音效与空间感。对白镜的人声制式是 native 模型自声 + 音色锚定；dubbed 只用于
   全旁白解说章，对白上镜的 dubbed 章由 lint `dubbed_dialogue` 点名。

### 1.5 网页入口

分镜卡「⧉ 配音指令」——单镜 tts 标准指令经指令台交 AI。

章级「⧉ 合成指令」在「native 且盘上已有配音」时会自带一句混烧说明（章节写 `native_voiceover: true`；
盘上的配音轨未开混烧不进成片），免得 agent 以为配音丢了又去重跑 tts。

守卫：`test_mix` 的 native 混烧用例 ＋ `test_delivery`。

## 2. 时长错配：配音变速贴窗，不是裁词也不是放任漂移

### 2.1 根因

分镜按 native 设计——台词写给 Seedance 念（模型自适应语速塞进 5s），我们的 TTS 正常语速念同样字数
要 6.5~10s。

实测 abyss ch01：28/35 镜超窗、整轨攒出 57s 偏差，后半段旁白与画面完全对不上，还会被末尾裁掉。

### 2.2 `narration_parts` 的窗口分支三态（native 混烧与 dubbed 主音轨共用）

| 情形 | 处理 |
|---|---|
| 短于窗口 | 垫静音齐窗 |
| 长于窗口 | 走 `("fit", (路径, 目标秒))`，由 `ffmpeg.concat_audio` **变速不变调** |
| 压缩比超 `FIT_TEMPO_WARN=1.3` | 由 `voicecast.fit_overruns` 在 tts 收尾**点名** |

变速实现：`tempo_chain` 拆合法 atempo 链（单级只收 0.5~2.0，故极端比要串联）；变速后按目标
**硬裁一刀**——atempo 有毫秒级误差，逐镜攒起来又变成整轨漂移。

点名的含义：那是台词写太满。改词或加长镜头归创作，**引擎只报不代改**。

## 3. `request_seconds` 的 native 分支恒取 `dur`

画面秒数由分镜 / Seedance 定，我们的 TTS 只是混烧叠加轨。

**拿配音长度去请求 Seedance，就是让「台词多长」决定「画面多长」**：实测 abyss 镜11 画面 5s 而配音
10.27s，照发即**多花一倍的钱**，且片段节奏与分镜设计完全不符。

（native 下 tts 不回填 `dur`：「跑过 tts 则 `dur` == 配音」的假设在 native 不成立，别按它反推。）

### 3.1 唯一例外

**从 kenburns 切来**的历史 `dur`——那时停顿被折进去过。按「配音 + 声明停顿」反查能对上才认定折过，
扣回净配音。

### 3.2 dubbed 只延不缩

取 `max(dur, 配音实测)`：设计窗口权威，配音超窗才把窗口撑到罩住整句（作 `ref_audio` 发出的素材不能
截断），配音短于窗口的余量是表演时间；kenburns 折算残留按同一停顿规则反查扣回。

## 4. 时长冲突的三条出路

分镜给 5s 而台词要念 8s 时：

| # | 出路 | 说明 |
|---|---|---|
| ① | 精简台词 | 语速随音色走：lint `narration_overrun` 按在用音色档案的实测 `speech_rate` 预估这句能否落进窗口，写分镜按那把声音的实测字/秒配字数，不按固定经验值 |
| ② | `tts --fit-dur` 自动放宽 `dur` 到配音实测 | 「让画面等台词」那条，opt-in |
| ③ | 什么都不做 | 配音已由 `("fit", …)` 变速贴窗能正常出片，只是那几句偏赶 |

### 4.1 `--fit-dur` 为什么是 opt-in

时长是创作决定，且 native 下放宽会让未来的 Seedance 请求更贵。

**已有 clip 的镜绝不动**：画面已生成、钱已付，改 `dur` 只会让片段与时间轴对不上——引擎点名并给出
「先打回 clip 再重烧」的出路。

只放宽不收窄，配音短于窗口是合法留白。

### 4.2 预警与实测是「估」与「测」，不是分叉

| 时机 | 位置 | 口径 |
|---|---|---|
| 事前预警 | `lint` 的 density 带 | 按字数估 |
| 事前预警 | `lint` 的 `narration_overrun` | 按在用音色实测语速估，阈值与 `fit_overruns` 同为 `FIT_TEMPO_WARN` |
| 事后实测 | `voicecast.fit_overruns` | 按 wav 实测 |

## 5. 一镜有几句台词就写几条 `shots[].lines[]`

拆句有**两条互相独立**的理由，命中任一条就必须拆：多说话人（5.1，音色）与多句（5.4，字幕节奏）。

### 5.1 问题

一个镜在全链路上原本是**原子**的：`stage_tts` 每镜只调一次 `synthesize(整段 narration, voice=一把声音)`、
`timeline()` 每镜一个 (start,end)、字幕每镜一条 Dialogue。

于是把对白写进同一个 `narration` 时，引擎无从知道该在第几个字换声音，只能整段一把嗓子念完——
**实测被点名「明明是两个人对话，最终只用了一个角色的配音，很出戏」**。

### 5.2 解法

不是逼作者按句拆镜（画面数与 Seedance 秒数翻倍，同机位来回对白还被硬切），而是让一个镜承载一串
句子：**画面仍是一张图 / 一段视频，音轨与字幕逐句走**。

### 5.3 七条落位纪律

1. **`voicecast.shot_lines` 是「镜 → 句序列」的唯一入口**（TTS / 时长 / 字幕 / lint 全走它）。
   没写 lines 的镜自动回落成 narration 单段，全链路零迁移；
2. 句与镜**同形**——emotion / emotion_scale / voice_instruction / delivery 四件套齐备，
   `shot_expressive_params` / `delivery_instruction` 原样复用，表现力逻辑绝不写第二份；
3. **整镜 wav（`shot_<id>.wav`）仍是唯一对外产物**，分句 wav（`shot_<id>_L<k>.wav`）只是中间物 →
   review 表态 / 版本栈 / dubbed 的 `ref_audio` / `request_seconds` 一行都不用改；
4. **音色只在本句没点名别人时才继承镜级**——句写了 `speaker` 就意味着「这句是另一个人说的」，
   继承镜级 `voice` 会把音色表里那个人的声音整个盖掉；
5. 句间停顿 `lines[].delivery.pause_*` 与镜级**同一道模式门控**（仅 kenburns），dubbed/native
   恒 0——否则等于按秒向 Seedance 买无声；
6. 字幕逐句一条事件（`subtitle.shot_events`）。演出型版式（对话框 / 气泡 / 居中）靠
   `subtitle.expand_timeline` 在**进循环前**把镜摊平成句——三个版式的循环体形态各异，逐个塞嵌套
   循环既要重排缩进又容易改漏；
7. **「有没有台词」的判据统一走 `voicecast.shot_text` / `shot_lines`**（cli / mediacheck /
   variation / scanner / voicecast / **pipeline.prompts** 六处）。漏一处，多角色镜就会在那处
   被当成纯画面镜——后果最重的是 `narration_parts`，它会把整镜配音换成等长静音且不报任何错。
   `prompts.native_voice_clause` 若裸读 `narration`，只写 `lines[]` 的镜在 native
   提示词里就没有人声句，Seedance 收不到该说什么、全程无报错。

### 5.4 同一个说话人的多句也要拆：说一句显示一句

`subtitle.shot_events` 对**单段**镜只发一条 Dialogue、横跨整镜时长。于是把多句写进一个
`narration` 时，第一句还没念到，后面几句已经摊在屏幕上——留言的反转、停顿与收尾全被剧透。

实测（voidheaven ch01 镜7，玉简三句留言写成一段）：字幕不但一次性全摊开，文本超行宽后
自动折行按字数硬切、不认标点边界，把收尾引号甩到了下一行行首——
`「后来者。若你听到这段话——说明我们失败了` / `」「不要继续飞升。」「他们会发现你。」`。

拆成三条 `lines[]` 后即三张独立字幕卡。**未跑 tts 时（缺省 native 不跑，混烧只给旁白镜跑）按字数比例切镜窗口**
（`shot_events` 的 `total <= 0` 分支转调 `voicecast.line_spans`，与提示词里的台词时间轴
同一份切分），所以**拆出的句数与 `sketch.beats` 的拍数对齐**、每拍的长短按台词长短给，
字幕就落在念白的拍点上。

预防走 `lint` 的 `subtitle_dump` 维度（警告级，判据见 docs/agents/lint.md）。

### 5.5 `native_voice_clause` 的三条语态纪律

同一个逐句循环编译三种语态，只有尾句分路。三条纪律各对着一种失败形态：

1. **旁白镜同样要发台词原文。** 旁白镜若在循环之前就返回一句不带文本的闭唇句
   （「画外旁白讲述，人物口唇保持闭合」），提示词里就有「有人在画外讲述」这个设定、
   却没有讲述内容：模型只能自行编造旁白。而 `native_voiceover` 缺省不烧固定音色，
   那条自编人声就是成片主音轨，与按 `narration` 烧录的字幕不同源。旁白锚定音的绑定句
   「用与参考音频完全相同的嗓音说出台词」也会因此指向一段从未给出的台词。
   同类漏的形态见 `sketchboard.timeline_text` 的 `beats[].sound`——板拿到了声音脚本、
   真正出声的视频模型拿不到。旁白句与混合镜里的旁白句同一措辞
   （`画外旁白讲述：“…”`），闭唇约束退为尾句。
2. **`voice_kind` 是整镜口径，句级要再判一次。** 混合镜（角色对白里插一句旁白）在它眼里
   恒是 dialogue，不在句级复判就会要求模型给第三人称叙述配口型。
3. **「这句是不是旁白」只有一个判据：`voicecast.is_narrator`。** 两半缺一不可——
   点了旁白别名（大小写不敏感，`VO`/`Narrator` 是常见写法），或**没点名**
   （`speaker` 空恒等于旁白，角色句必须具名）。全链八处消费点共用它：漏「没点名」
   那半会让提示词编出没有主语的「说：“…”」并要求为第三人称叙述配口型，漏
   `.lower()` 会让 lint 把英文别名的旁白镜报成角色对白。`voice_kind` 是它的整镜
   聚合（没有任何一句非旁白即 voiceover），混合镜要在句级再判一次。

提示词已经把台词逐字发过去不等于确定性保证：`lint` 的 `native_voice_unverified`
维度是把「一致」与「待核对」区分开的那一句，核对本身由 verify 的 ASR 人声文字核对完成。

守卫 `test_dialogue`（回落态钉死 + prompts 侧行为断言）、`test_prompts` 的
`TestNativeVoiceClause`、`test_voice_anchor` 的旁白锚定用例。
