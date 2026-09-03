# 分镜脚本专业规范（阶段3 · 节点① 的核心交付物）

> 分镜脚本 = 本系统的「施工图」：既是给人审的**镜头表**（影视工业格式），也是给模型吃的
> **结构化提示词源**（双语）。参照 StudioBinder/Celtx 镜头表字段体系与
> Seedance/Veo/Sora 官方提示词规范设计。表演怎么落到身体、机位怎么讲权力、
> 视觉换挡节律——配套读 `references/performance.md`（本文管表格与镜头，那边管戏）。

**语言默认值**：分镜表、结构化镜头字段（`framing` / `angle` / `lens` / `lighting` /
`camera` 等）和中文提示词默认使用中文；只有用户明确要求英文时才改用英文。英文版本
统一放在 `image_prompt_en` / `video_prompt_en`（或 PromptSpec 的 `text_en`），展开镜头
提示词时显示“中文主行 + 英文次行”。`35mm`、`Ken Burns`、`dolly zoom` 等必要专业
术语可以保留，不等于把整列写成英文。

## 每镜字段全集（写进章节 JSON 的 shots[]）

**核心字段（必填）**

| 字段 | 含义 | 规范 |
|---|---|---|
| `id` | 镜号 | 从 1 递增 |
| `framing` | 景别 | 默认使用中文；用下方《景别对照表》值（中文或代码均可）；**引擎不做枚举校验**，「双人中景」这类组合口径照写不误 |
| `camera` | 运镜 | 默认使用中文；**一镜一主运镜**（铁律，见下），默认慢速/平滑 |
| `dur` | 设计时长(秒)——场→镜推导出的**表演窗口**，台词只占一段 | 对齐模型档位（Seedance 2.0 整秒 4~15s）；dubbed 下配音只延不缩（短于窗口时引擎垫静音尾发送），片段生成后回填实测值 |
| `narration_en` | 旁白英文对译 | **项目 subtitle_lang=en/both 时必填**——写分镜时中英两套文案一并产出（信达雅的对译，不是机翻腔）；TTS 仍用中文 narration，本字段只进字幕 |
| `caption_en` | 英文补位字幕 | 与 caption 同语义：仅无 narration_en 的镜生效 |
| `narration` | 旁白/台词 | **不是必填件**（语态见主 SKILL「语态先行」）：lead 解说语态一句一观点；sparse 剧情语态默认留空——对白走 `speaker`/`lines[]`，动作/战斗写纯画面镜（留空＝自动静音占位） |
| `caption` | 补位字幕 | **仅无旁白纯画面镜生效**（音字一致铁律：有旁白时字幕逐字取 narration）；简短 ≤16 字/行 |
| `image_prompt` | 图像提示词·**中文主** | 只写本镜动作/姿态/机位（风格/场景/外貌由引擎前置） |
| `image_prompt_en` | 图像提示词·**英文辅** | 海外模型（provider `prompt_lang: en`）自动选用；缺失回退中文 |
| `video_prompt` | 运动提示词·中文主 | **只写增量**：本镜的运动/运镜/光线变化（画面基底由分镜图给定，见铁律 4） |
| `video_prompt_en` | 运动提示词·英文辅 | 同上 |
| `action` | 动作（delta 骨架位·选填） | 「抬手抹掉眼角，肩膀垮下去」——缺 `video_prompt` 时引擎按「动作：」拼进提示词 |
| `end_state` | 终态（delta 骨架位·选填） | 「手停在半空，目光落在窗外」——首尾帧衔接时就是运动的收束点 |
| `light_shift` | 光线变化（delta 骨架位·选填） | 「屋内由暖黄渐转冷蓝」——写**变化**，静态光线设计写 `lighting` |

**工业字段（选填，专业分镜表建议填）**

| 字段 | 含义 | 示例值 |
|---|---|---|
| `angle` | 机位角度 | 平视 / 仰拍 / 俯拍 / 荷兰角 / 鸟瞰 |
| `lens` | 焦段与焦点语感 | `35mm 浅景深` / `85mm 人像` / `广角 24mm`；**2D 手绘画风（赛璐璐/国漫/水墨/绘本/像素等）留空或只写景别语感（如「近景压缩感」），不写焦段与景深**——手绘作画没有物理镜头，写 `85mm f/1.4` 会把模型往摄影写实拽、坏掉画风；3D/CG/写实档（pixar/disney3d/anime3d/anime_ldr/photoreal3d/virtual_production…）照常写 |
| `lighting` | 本镜光线设计 | `暖橙夕照侧逆光，浮尘光束` |
| `sfx` | 音效提示 | `雨声、远处钟声`（native 模式会进提示词） |
| `negative_prompt` | 负面约束 | `画面抖动, 肢体扭曲, 多余手指`（引擎编译为"避免出现：…"；**别写「字幕/画面文字/水印」——引擎逐词兜底**，见下「防字地板」） |
| `speaker` / `voice` | 说话人 / 音色覆盖 | `speaker` 填角色名，引擎按选角档案解析（缺省 `voice_prompt` 定制，模版为显式例外，见 voice-casting.md）；`voice` 只在单句换嗓时显式覆盖 |
| `emotion` / `emotion_scale` | 本镜情绪档 / 强度 1~5 | `angry` + `5`（爆发镜）·`sad` + `3`（离别镜）——**情绪随剧情逐镜标注**；定制路进 seed-audio 剧本正文，模版路走 `audio_params.emotion` |
| `voice_instruction` | 语音指令（对话式表现力） | `用哽咽的语气说`；语气类 retake 意见（"太平了"）编译到这里。**仅复刻/定制音色（seed-icl）生效**——官方固定音色下引擎不下发（标准版会静默过滤，跑 tts 会打印提示）；复刻音色会自动切表现力增强版（有效果抽卡，抽废置 retake 重合成即可） |
| `delivery.emphasis` | 本镜重读词（≤8 个） | `["一定","回来"]`（写 `"一定、回来"` 也行）——与 `voice_instruction`/`note` 编译成一句语音指令，**仅复刻/定制音色生效** |
| `delivery.pause_before` / `pause_after` | 台词前/后停顿秒数（单侧 ≤5） | `0.5`——**仅 kenburns 生效且已折进 `dur`**（dur = 配音实测 + 停顿，tts 每次重算、幂等）；dubbed/native 下**一律不生效**（那两个模式 dur 是 Seedance 计费秒数，而对口型音频里没有这段无声，折算=无效计费钱） |
| `delivery.note` | 本镜表演提示 | `句末收住，别扬调`——同 `voice_instruction` 一并编译，**仅复刻/定制音色生效** |
| （台词内嵌）`<cot text=情绪>片段</cot>` | 语音标签：单句内分段控语速/情绪 | 仅**复刻音色**支持（引擎自动开启解析）；含标签单句 ≤64 字。**能不用就不用**：标签写进 `narration` 就进了字幕真源，引擎虽会在取字幕时脱标签（`pick_texts`），但按 `len(text)` 计费时标签照样进字数、官方音色还可能念出来。重读/停顿一律走 `delivery`，别塞台词 |
| `characters` | 本镜出场角色 | 控制角色设定图参考范围；**缺省=全部角色全挂**（空列表=不挂角色） |
| `props` | 本镜出场道具 | 控制道具设定图参考范围；**引擎自动挂载 = 本镜 image_prompt/narration 里点名命中的道具（按 `name`/`keywords` 匹配）∪ 本字段显式指定**——用设定集 `name` 措辞点名（或给道具配 `keywords`）即自动锁设定图；措辞不符/重名时用本字段兜底 |
| `bubble_pos` | 气泡水平落点（bubble 字幕模式） | `left / center / right`（按构图标注说话人所在侧，气泡尾巴指向其头顶） |
| `priority` | 优先级 | `essential` / `nice_to_have`（缺预算时 Agent 取舍用；引擎不读） |

**叙事元数据（选填 · 自由文本不设枚举 · 引擎一行都不读）**

| 字段 | 含义 | 示例值 |
|---|---|---|
| `shot_intent` | 本镜叙事意图（一句话讲清「这镜为什么存在」） | `点破身份，让观众第一次意识到他在说谎` |
| `narrative_role` | 本镜在全片结构中的位置 | `钩子` / `铺垫` / `转折` / `高潮` / `收束` / `CTA` |
| `hero_moment` | 叙事高光镜标记（布尔） | `true`——一集 1~2 镜足矣，全标等于没标 |

**诚实边界（别误用 `hero_moment`）**：这三个字段**引擎不消费**——标 `true` 不会让引擎多花钱、
不升分辨率、不加候选宫格、不改运镜，它只做两件事：① 写分镜时逼自己讲清每镜的叙事职责；
② 供分镜单 lint 与人工审片核对节奏分布。**要给某一镜更高成本，唯一正道是显式写
`shots[].profile` 指向更贵的画风档**，引擎绝不自动升档。与 `priority` 的分工：
`priority` 管「缺预算时砍谁」，`hero_moment` 管「重点保谁」，两者互不替代。

## 分镜单体检（`lint`）与 `art_direction` 风格圣经旋钮

分镜写完、生图之前跑一次（纯计算·零成本·**结论不落盘**）：

```bash
python3 -m kinema lint --chapter <项目id>/<章节id>      # 加 --strict 则有警告即非零退出
```

维度全览：**相邻镜运镜雷同**（`camera` 取冒号前的技法名归一，「缓慢推近」与
「缓慢推近：镜头缓缓平稳推近至主体」算同一个）/ **有台词镜缺 `emotion` 或情绪单调** /
**景别分布过平**（`framing` 归到远景类·中景类·近景类·视点类四桶；「双人中景」归中景桶，
归不了的写法只提示不判错）/ **反 slop 空词** / **抽象情绪词**（画面描述把情绪写成
名词——`emotion_abstract` 逐词给身体化改写，纪律见 `performance.md` 第一节）/
**画面代词**（画面字段用「他/她/它」——`prompt_pronoun`，设定图挂载按 name/keywords
文本命中，代词挂不上图；纪律见 `performance.md` 第二节）/ **占位旁白与跨镜重复台词** /
**旁白文风**（抬价句式 `narration_pivot`·汇报腔词 `narration_jargon`·名词化
`narration_nominal`·收尾宏大词 `cta_grand`·同连接词开头 `narration_opener`——
写法纪律见 `copywriting.md`）/ **旁白语态**（`voiceover_heavy`：sparse 剧情语态下
旁白镜占比超 40%＝解说腔漫剧，none 语态下有旁白即报；`no_silent_shot`：剧情语态
全片无纯画面镜——语态缺省由画风归属 skill 派生，顶层 `voiceover` 显式声明凌驾）/
**`hero_moment` 分布** / **设定图覆盖度**
（`required_emotions` 差集）/ **`video_prompt` 多镜语法**（见下）/
**`video_prompt` 复述 `image_prompt`**（字符 n-gram 重合率超阈值即告警，
只在 dubbed/native 判——kenburns 根本不读 `video_prompt`；见铁律 4）/
**场景连续性**（`scene_unanchored`/`scene_jump`，见主 SKILL 场景连续性铁律）/
**视觉换挡间距**（`shift_gap`：按 dur 累加时间轴，连续超 30s 无一次可见换挡即点名
区间——info 级，判定只认 scenes/framing/angle/light_shift/转场五个结构化位；
节律方法见 `performance.md` 第五节）。
`gen-image` 生图前会自动跑同一道**软闸**（只提示不阻断；
`--only` 单镜重生时降为一行汇总，但**扫的仍是全片**——只扫一镜的话相邻雷同与分布维度全失真）。

**反 slop 空词**＝说了等于没说的主观形容（唯美 / 精美 / 氛围感 / 电影感 / 高级感 /
史诗感 / 震撼 / 梦幻 / 治愈 / 高质量 / 大师级 / 细节丰富…）。模型无法把评价渲染成像素，
只会退回训练集均值那张"AI 感"的图。**写提示词时一律换成可被镜头拍到的物理描述**：
光线走向、材质与工艺、构图占比、身体姿态、空气介质。lint 会逐条给出改写方向——
**词表与改写建议的单一真源是引擎 `engine/kinema/pipeline/variation.py` 的 `SLOP_TERMS`**，
本文件只讲纪律不抄词表（免得两处分叉）。

**`video_prompt` 多镜语法**＝在一条 `video_prompt` 里排两个以上的镜
（`Shot 1: 她转身；Shot 2: 特写手部` / `镜头一：… 镜头二：…`）。本工程的制度是
**一镜一次调用一个视频文件**（时长由产物回填、字幕与时间轴据此对齐），一条提示词
排两个镜也拆不出第二段素材，只会让一段素材承担两镜的内容。**要两个镜就写两条分镜**，
`video_prompt` 只写本镜的运动。这是**预防性纪律**——模型到底会不会在同一段片子里
硬切、切在哪，离线无从证实，故 lint 恒为告警、绝不拦死生成。

**`art_direction` 旋钮**（章节 json 顶层·选填）——调的是 lint 的**告警松紧**，
**永不改画面**（不换运镜、不改提示词、不加候选、不影响成本）：

```json
"art_direction": { "variety": 8, "motion": 7, "density": 5, "avoid": ["漫天樱花"] }
```

| 旋钮 | 1-10 | 驱动什么 |
|---|---|---|
| `variety` | 越高越不许重复 | 相邻同运镜允许次数（10→0 次 / 8→1 / 5→2 / 1→4）、景别至少几类桶、情绪至少几种 |
| `motion` | 越高越要求运镜 | 正镜里写了 `camera` 的比例下限（10→100% / 5→50% / 1→10%） |
| `density` | 越高越要求信息密 | 旁白语速带（字/秒；5→3.0~5.2，真实章节落在 3.4~4.7），带外只出提示 |
| `avoid[]` | — | 本片点名忌讳的措辞，命中 `image_prompt`/`video_prompt` 即告警（通用空词不必重复登记） |

不写整块就按中位 5 判；写坏/越界自动回落缺省，绝不报错。

**字幕不进画面（铁律）**：所有字幕/对话框/气泡由合成段按 profile 的 `subtitle` 样式
后置烧录——分镜提示词绝不要求模型在画面里写字。引擎给**图像与视频两侧**提示词
都加了"避免出现：字幕、画面文字、水印"地板（`pipeline/prompts.py`）：作者写的
`negative_prompt` **在前**、地板**在后**，作者自己已写「字幕」/`subtitle` 则不重复注入；
关掉地板的只有画面里本来就该有字的两档：`game_sim`(HUD 血条小地图) · `explainer`(信息图标注)
——models.yaml 里写 `image.image_text_floor: false` 声明，**其余画风一律不许关**
（气泡/对话框/榜单的字全是 ASS 后置烧录，恰恰要求图本体干净，是防字地板的受益方）。
样式随画风换装：caption（默认底部，各画风已配色）/
bubble（头顶气泡：对白进气泡、旁白自动退底部）/ dialogue_box（JRPG 框）/
centered / ranking；单项目改风格在 project 顶层写 `"subtitle": {"mode": ...}` 覆盖。
断行遵循 Netflix 简中规范：每行 ≤16 字、至多两行、标点处断、标点不孤行。

## 中英术语对照（写 `_en` 提示词与跨模型迁移用）

**景别 Shot Size**：远景 EWS · 大全景 WS · 全景 FS(全身) · 中全景 MLS · 中景 MS(腰上) ·
中近景 MCU(胸上) · 近景/特写 CU · 大特写 ECU · 过肩 OTS · 主观 POV · 双人 2S · 插入 INS

**角度 Angle**：平视 eye level · 仰拍 low angle · 俯拍 high angle · 鸟瞰 bird's eye ·
荷兰角 dutch angle · 贴地 ground level

**运镜 Movement**（推拉摇移跟升降·官方口径：Seedance 可精准响应推/拉/摇/移/环绕/
跟随/升/降/变焦）：推 slow push-in / dolly in · 拉 pull-out / dolly out ·
摇 pan left/right · 移 truck / crab · 跟 tracking shot · 升 crane up · 降 crane down ·
环绕 slow arc / orbit（限小角度）· 变焦 zoom · 固定 static / locked-off · 手持 handheld（慎用）

## 进阶运镜预设库（知名运镜 · 双语措辞 · 风险分档）

电影史上被验证过千次的"名场面运镜"——`camera` 字段直接取用「中文措辞」列
（引擎自动并入「运镜：…」前缀），`video_prompt_en` 取「英文措辞」列。
**风险档语义**：●稳定=放心用｜▲进阶=一集≤4 次·必须带"缓慢/平稳"修饰·建议 dur≥5s｜
■高危=默认禁用，仅用户点名或全集情绪最高点的一镜才用，且 dry-run 必审：

### 经典技法（电影语法基本盘）

| 运镜 | 中文措辞（camera 字段直用） | 英文措辞（_en 用） | 档 | 何时用 |
|---|---|---|---|---|
| 希区柯克变焦<br>dolly zoom | 希区柯克变焦：镜头缓慢后退并同步放大焦距，背景空间被压缩拉伸产生眩晕感，主体在画面中大小不变、构图居中锁定 | dolly zoom (Hitchcock zoom): camera slowly pulls back while zooming in, background space compresses and stretches with a vertigo feel, subject size locked and centered | ▲ | 真相揭晓/世界观崩塌/恐惧顿悟的**情绪反转镜**——全集只给最重的那一拍 |
| 焦点转移<br>rack focus | 焦点从前景的X缓缓转移到背景的Y，浅景深，焦外光斑柔化，转移平滑无呼吸 | rack focus shifting smoothly from foreground X to background Y, shallow depth of field, soft bokeh, no focus breathing | ● | 双主体关系镜/信息揭示——**对白剧最优雅的运镜**，正反打之外的第三选择 |
| 升镜揭示<br>crane reveal | 镜头从低处缓缓升起越过前景的X，逐层揭示远处的Y全景，前中远三层景深依次展开 | slow crane up from low over foreground X, revealing Y in the distance layer by layer, fore-mid-background unfolding in depth | ● | 开场建立镜/收尾格局镜——"原来世界这么大"的一拍 |
| 缓慢环绕<br>slow orbit | 镜头绕主体缓慢环绕四分之一圈，主体始终居中，背景视差流动，光影随角度渐变 | slow 90-degree orbit around the subject, subject centered throughout, background parallax flowing, light shifting with the angle | ● | 主角高光/器物展示（>90° 崩率陡增，限小角度） |
| 侧向跟拍<br>tracking | 镜头在侧面与主体等速平稳跟随，主体保持三分线位置，背景带速度感流动虚化 | smooth lateral tracking at the subject's pace, subject held on the third line, background streaming past with motion blur | ● | 行走对话/追逐前奏/巡视 |
| FPV 穿越 | 第一人称穿越视角，镜头贴着路径连续飞行穿过X，高度与倾斜随地形起伏，速度感渐强 | FPV drone shot skimming along the path through X, altitude and bank following the terrain, speed building steadily | ▲ | 空间导览/追逐/坠落——**空镜专用**（带主角面部易崩） |
| 一镜到底<br>oner | 一镜到底，镜头连续移动无跳切，先缓缓经过X，再转向抵达Y，节奏先缓后扬 | continuous one-take, no cuts, camera drifts past X then turns and arrives at Y, pacing slow then swelling | ▲ | ≥10s 长镜叙事（native 模式 + 分时段描述才稳） |
| 机械臂扫摆<br>robotic arm | 镜头如机械臂般从X角度平滑弧线摆至Y角度并持续跟随主体，精准稳定无抖动 | robotic-arm style camera sweeping in a smooth precise arc from X angle to Y angle while tracking, zero jitter | ▲ | 产品/法宝/机甲展示、战斗环视 |
| 手持纪实<br>handheld | 轻微手持晃动感，呼吸般的浮动幅度，纪实临场感，晃动始终克制 | subtle handheld sway with a breathing-like float, documentary immediacy, shake kept minimal | ▲ | 冲突/逃亡/伪纪录——幅度必须写"轻微"，否则糊 |
| 甩镜<br>whip pan | 快速甩镜转向X，强烈方向性运动模糊，落点稳定收住新构图 | whip pan to X with strong directional motion blur, landing locked on a stable new composition | ■ | 双场景硬切/喜剧节拍——落点稳是成败关键 |
| 急推<br>crash zoom | 急速推近至面部大特写，末段急停带轻微过冲回弹 | crash zoom into extreme close-up, hard stop with a slight overshoot settle | ■ | 震惊反应/喜剧夸张 |
| 子弹时间<br>bullet time | 时间近乎凝固，尘埃与碎片悬停空中，镜头绕定格的主体匀速弧线移动，光线扫过轮廓 | bullet-time: time nearly frozen, dust and debris suspended, camera arcing evenly around the frozen subject, light sweeping the silhouette | ■ | 动作最高潮的唯一一镜（native 模式） |

### 大师签名运镜（名场面复刻）

| 运镜 | 中文措辞（camera 字段直用） | 英文措辞（_en 用） | 档 | 何时用 |
|---|---|---|---|---|
| 迈克尔·贝英雄环绕<br>Bay hero orbit | 低角度仰拍缓慢环绕主体半圈，主体缓缓起身或伫立不动，背景旋转流动，逆光镜头光晕，慢动作史诗感 | low-angle slow half-orbit around the rising hero, background rotating past, backlit lens flare, slow-motion epic gravitas | ▲ | **主角登场/封神宣言/集结亮相**——影史最著名的"高光镜"（半圈内；全圈崩率高） |
| 斯皮尔伯格惊愕推近<br>Spielberg push-in | 镜头缓缓推近至面部特写，人物望向镜外逐渐睁大双眼、嘴唇微张，背景缓慢虚化 | slow push-in to a close-up as the character gazes past camera, eyes widening in awe, lips parting, background melting into blur | ● | 目击奇观/顿悟瞬间的**反应镜**（Spielberg Face） |
| 库布里克对称推进<br>Kubrick push | 沿走廊中轴线单点透视对称构图，匀速缓慢推进，冷峻压迫感 | one-point perspective push down the exact center axis, rigorously symmetrical, steady pace, cold and foreboding | ● | 走廊/隧道/仪式空间——秩序感与不安并存 |
| 斯派克·李滑行<br>double dolly | 主体如站在移动平台上朝镜头滑行，身体静止而背景后退，梦游般的悬浮感 | double-dolly glide: subject drifts toward camera as if on a platform, body still while the world slides back, dreamlike floating | ▲ | 恍惚/下定决心/被命运推着走的时刻 |
| 卢贝兹基漂浮<br>Lubezki float | 镜头如无重力般贴近主体缓缓漂浮环行，自然光，长镜呼吸感 | weightless floating camera drifting close around the subject, natural light, breathing long-take feel | ▲ | 沉浸式情绪戏/自然环境戏（荒野猎人式） |
| 小津低机位<br>Ozu tatami | 低机位榻榻米视角固定镜头，轻微仰角平视人物，构图安定对称 | static tatami-level shot, slight low angle at seated eye line, serene symmetrical composition | ● | 对坐交谈/家庭戏——静水流深的对白镜 |
| 老男孩横移<br>side-scroll | 镜头水平横移平行跟随动作，画面如横版卷轴展开，景深压平 | flat side-scrolling tracking shot parallel to the action, staged like a 2D scroll, compressed depth | ● | 走廊群战/行进队列（像素/游戏画风天配） |
| 韦斯·安德森甩摇<br>Wes 90° whip | 对称构图中快速90度甩摇到下一主体，落点精准形成新的居中对称构图 | snap 90-degree whip pan within symmetrical staging, landing precisely on the next centered composition | ■ | 喜剧节拍/图鉴式逐个展示 |
| 前景擦镜<br>foreground wipe | 前景物体（人影/立柱/车流）掠过并短暂遮蔽镜头，擦过瞬间机位与景别已无痕切换 | a foreground object sweeps across and briefly blocks the lens, revealing a new angle as it clears | ▲ | 长镜内**无痕转场**——AI 视频独有的优势技法 |

## 镜型 → 运镜智能推荐（写分镜时按此选择）

**配镜纪律**：一集 8~12 镜里配 **2~4 个▲进阶档**做记忆点（观众为这几镜转发），
其余用●稳定档与基础运镜；■高危档不主动排。逐镜按镜型查表：

| 镜型 | 首选 | 备选 | 忌 |
|---|---|---|---|
| 开场钩子/建立镜 | 升镜揭示 | 库布里克对称推进（通道空间） | 甩镜开场 |
| 对白正反打 | 固定/轻微推近 | 焦点转移（双主体同框）· 小津低机位（对坐戏） | 环绕 |
| 情绪反转/顿悟 | **希区柯克变焦** | 斯皮尔伯格惊愕推近 | 快速运镜 |
| 高光宣言/主角登场 | **迈克尔·贝英雄环绕**（半圈） | 缓慢环绕 · 低角度升镜 | 全圈快环绕 |
| 恍惚/宿命时刻 | 斯派克·李滑行 | 卢贝兹基漂浮 | 手持大晃 |
| 动作高潮 | 侧向跟拍＋主体大幅动作 | 子弹时间（点名才用） | 多运镜叠加 |
| 追逐/穿行 | FPV 穿越（空镜段） | 老男孩横移（走廊/队列） | 手持大晃 |
| 空镜氛围 | 缓慢横摇 slow pan | 卢贝兹基漂浮 · 固定＋环境次级运动 | — |
| 器物/产品特写 | 机械臂扫摆 | 缓慢环绕 | 变焦推拉并用 |
| 长镜内转场 | 前景擦镜 | —（普通镜间转场走引擎转场系统） | 甩镜滥用 |
| 收尾 CTA/格局镜 | 拉远 pull-out | 升镜离场 crane up | — |

**进阶运镜四戒**（官方与各家实践共识）：① 进阶运镜也是"一个"运镜——希区柯克变焦/
一镜到底本身即一镜一主，不再叠加其他运镜；② 忌矛盾描述（"高速推镜同时极度稳定"
类自相矛盾必崩）；③ 「缓慢/平稳/流畅」（slow / smooth / cinematic）是一切运镜的
默认气质词，快系措辞只属于■高危档；④ 运镜只写进 `camera` 字段——引擎会把它置于
**创作正文首位**（模型对前位 token 权重最高），别把运镜埋在 `video_prompt` 句尾。
整条提示词的绝对首句是引擎前置的**增量契约句**（「以所给首帧/参考图为画面基准…」），
运镜紧随其后——这是既定拼装顺序，不必也不该手动模仿。

## 导演意图统一（先定一个感受，再选乐器）

上面的预设库共 **21 条运镜**（经典技法 12 + 大师签名 9），加上景别 12 档、光位、
剪辑节奏、配乐、字幕样式——手上乐器一大把。**乐器多不等于该一起吹**：分镜写崩最常见的
原因不是某一镜差，而是每镜各自"挺好看"、合起来没有一个统一的感受。所以顺序永远是
**先定一个感受，再选乐器**，不是反过来挑技法凑。

**第一步（写第一镜之前）：给整条片子写一句话意图。** 格式：
> 「我要观众看完觉得＿＿＿」——一个形容词或一种身体感受，不是剧情梗概。

例：「觉得憋屈然后突然透气」「觉得这人很危险但克制」「觉得温暖到想给家里打个电话」。
写不出来说明片子的骨还没立，回去改文案，别急着排镜。这句话**必须先跟用户确认**，
确认后 `decision add --chapter <项目id>/<章节id> --choice "全片意图：…" --why "…"` 留痕
（后续会话与 retake 裁决都以它为准绳，见主 SKILL「决策留痕」）。

**第二步：按意图定"基调档"，所有乐器统一服从它。** 三档够用，别自造第四档：

| 基调档 | 意图关键词 | 运镜取向 | 景别取向 | 剪辑节奏 | 光位/色 | 忌 |
|---|---|---|---|---|---|---|
| **静观** | 温暖 / 怅然 / 静水流深 / 治愈 | 固定、缓慢横摇、小津低机位、卢贝兹基漂浮 | 中景与全景为主，特写只给情绪落点 | 慢：每镜 4~6s，少切 | 柔和顺光/侧逆光，暖色 | 甩镜、急推、大幅手持 |
| **推进** | 紧张 / 追逐 / 揭示 / 爽感 | 侧向跟拍、升镜揭示、FPV、前景擦镜 | 景别阶梯明显，全→中→近逐步收紧 | 快：每镜 2~3s，切点密 | 硬光、明暗对比强 | 长时间固定镜（会泄气） |
| **压迫** | 危险 / 荒诞 / 失控 / 顿悟 | 希区柯克变焦、库布里克对称推进、斯派克·李滑行 | 特写与极端景别（大远景/大特写）交替 | 不均匀：长镜憋住 + 突然短切 | 低调光、单侧硬光、冷色 | 环绕（会显得炫技而非危险） |

**第三步：给"记忆点"留唯一一拍。** 全集情绪最高的**一镜**才配▲进阶档运镜
（一集 1~2 个上限见上文配镜纪律）。若某一镜的意图与全片基调档不同（例如"压迫"片里
的一个回忆闪回是"静观"），那是**刻意的对比**——必须在 `shot_intent` 里写明
「与全片基调反向，做对比」，否则下一次会话会当成漂移改回去。

**反过来的错误做法（点名禁止）**：先看着预设库挑几个好看的运镜，再想办法把它们塞进分镜。
这样出来的片子每镜都在炫技，观众记不住任何一个感受。**技法是结果不是起点。**

## 双语提示词规范（中文为主，英文为辅）

1. **`image_prompt`（中文）是唯一真源**：国产模型（Seedream/Seedance）中文理解最深，直接喂中文。
2. **`image_prompt_en` 为辅**：由中文版**语义对译**（不是逐字翻译，用地道电影术语），
   供海外模型（Veo/Nano Banana 等 `prompt_lang: en`）自动选用；引擎缺失时自动回退中文。
3. **中文正文 + 英文术语混排增权**：专业术语在中文提示词里可保留英文
   （`rim light` / `35mm` / `shallow depth of field`），对国产模型同样是高权重词。
4. 六要素公式（各家官方规范的交集）：**主体 + 动作(按时序) + 场景/光线 + 镜头语言 + 风格 + 约束**。
   本系统中「风格/场景/外貌」由引擎前置（style_prefix + character_block + scene），
   所以 `image_prompt` **只写差异**：机位 + 动作 + 表情 + 本镜专属道具/元素
   （**已建设定图的道具，用其设定集 `name`/`keywords` 措辞点名即自动挂设定图；措辞不符/重名时显式挂 `shots[].props`**，见字段表）。

**标准示例（一镜的完整写法）**——取自一条**真出过片的成品镜**（受击反应镜），
`video_prompt` 351 字、`image_prompt` 392 字，两者都在引擎地板之上：

```jsonc
{
  "id": 10, "dur": 6.05,
  "framing": "中景", "angle": "平视", "lens": "", "transition": "切",
  "camera": "侧向跟拍：镜头在侧面与被击飞的主体等速跟随，落地后停住成固定",
  "scenes": ["产线中庭"],
  "characters": ["白刻", "守卫队长"],
  "props": ["腕甲战术屏"],   // 点名了设定集注册名就自动挂设定图；措辞不符/重名时才需显式挂 props（见字段表）
  "shot_intent": "受击反应镜+读数：系统彻底死了，身后是不能挥刃的培养舱——绝境成立",  // 叙事元数据·引擎不读
  "narrative_role": "转折",
  "emotion": "隐忍",
  "lighting": "腕甲残屏爆出的火花与熄灭是本镜关键光变，背景培养舱光墙占满画面右侧",
  "light_shift": "腕甲战术屏爆火花后彻底熄灭",   // ④ 光影变化层的结构化位——lighting 是首帧的静态快照，它才是"这几秒光怎么变"
  "image_prompt": "中景平视，白刻受击落地的狼狈瞬间：白刻背部撞上栈桥立柱后弹落、正沿湿地跪滑——双膝与右手血肉之手撑地，犁出三道并行水痕，高马尾散乱贴在颈侧、发尾品红黏着水光；铬合金左臂垂着，白热已退成暗红余温、肘部液压杆裂纹清晰，**腕甲战术屏正在爆出最后一蓬电火花——屏面彻底黑掉、进度环熄灭**，火花照亮白刻低垂的侧脸半秒；白刻身后半步就是培养舱光墙的底层舱列——舱内悬浮人形的轮廓在白刻肩后一排排亮着，近得白刻的刃已不敢抡圆；画面左侧景深处，守卫队长拖着电磁战戟缓步逼近，戟刃在地面刮出一条持续的火花线，面罩双灯带的品红反光一步步压进白刻跪滑终点的水面里",
  "image_prompt_en": "medium shot, eye level, the ungraceful landing: Baike rebounds off a catwalk pillar and knee-slides across the wet floor—both knees and her gloved flesh hand plowing three parallel furrows, ponytail scattered against her neck, magenta tip slicked with water; the chrome arm hangs, white-heat faded to ember red, elbow rod visibly cracked, the wrist screen spitting its final burst of sparks—then dead black, integrity ring extinguished, the burst lighting her lowered profile for half a second; half a step behind her the incubator wall's lowest tier glows, suspended figures close enough that her blade can no longer swing wide; deep left, the Captain drags an electromagnetic halberd forward, its edge scraping a continuous line of sparks, twin magenta visor bands creeping into the water at the end of her slide",
  "video_prompt": "受击反应镜——打击感的一半在这里兑现，节奏是「爆—停—停—亮相」。爆：白刻背部撞上立柱的钝响、弹落、跪滑三米，三道水痕犁开，撑地的血肉右手虎口在湿地上打滑半寸又咬住；滑行末端腕甲战术屏爆出最后一蓬电火花后**彻底黑屏**——那一线常亮了三场戏的红光第一次消失，白刻左臂彻底成了没有仪表的哑铁。停一：白刻保持撑地姿势两秒不起身，肩背随急促呼吸起伏，滴水从发尾连成线——镜头在这两秒里让观众看清白刻身后半步就是培养舱：舱内人形近得能数清手指，白刻的刃抡不圆。停二：景深处守卫队长拖戟逼近的脚步与刮地火花线一步一步逼近，品红反光淹进白刻面前的水洼。亮相：白刻抬起头——不是看守卫队长，是看了一眼黑掉的屏，随后视线越过黑屏、落在自己那只血肉右手上。情绪质感：白刻脸上没有一丝崩的成分——只是在清点：还剩什么能用",
  "video_prompt_en": "lateral tracking with her flight, locking to a fixed frame once she lands. The reaction shot—where the hit's other half gets paid: burst, pause, pause, pose. Burst: the dull slam into the pillar, the rebound, a three-meter knee-slide plowing triple furrows, her gloved hand slipping half an inch before it grips; at the slide's end the wrist screen spits one last spark burst and goes dead black—the red line that survived three scenes is gone, the chrome arm now instrumentless iron. First pause: two seconds braced and unmoving, shoulders heaving with hard breath, water stringing off her hair—two seconds that let the audience see the incubator wall half a step behind her, figures close enough to count fingers, no room to swing. Second pause: deep in frame the Captain's dragged halberd scrapes its spark line nearer, magenta wash flooding the puddle before her. Pose: she lifts her head—not at him, at the dead screen, then past it, down to her own flesh hand. Emotional texture: nothing in her face has broken; she is taking inventory of what still works",
  "end_state": "白刻撑地抬头、视线落在自己血肉右手上，黑屏义肢垂在身侧，守卫队长的品红反光已进画面",   // 首尾帧衔接时=运动的收束点
  "negative_prompt": "白天, 明亮阳光, 崭新整洁, 低对比, 彩虹色霓虹, 无来源环境光, 面部义体, 目镜, 与白刻不同的脸, 头身比失调, 多余手指, 表情崩溃, 起身过快",
  "sfx": "背撞立柱的钝响，躯体落地与跪滑水声，屏体爆裂的电火花，拖戟刮地的长音"
}
```

**照着抄这三件事**（也是这条范例与"一句话打发"的全部差距所在）：

1. **`video_prompt` 用拍来组织**（「爆—停—停—亮相」），每一拍写清**这一拍新增了什么可见事实**；
   不是形容词堆叠，是一串能在画面上查证的物理事件（水痕三道、虎口打滑半寸、屏体黑掉）。
2. **五层齐备**：主体表演（撞→弹→跪滑）· 微表情（"没有一丝崩的成分"）· 次级动画（马尾散乱、
   滴水连成线、火花线）· 光影变化（残屏爆闪后熄灭，同时进 `light_shift`）· 收束（`end_state`）。
3. **仍是纯增量**：通篇一个字不复述白刻长什么样、场景长什么样——那些由设定图与引擎前置负责，
   复述会被 `prompt_echo` 抓，且是跨镜漂移的头号来源。

> **这一镜没有台词**（`speaker`/`narration`/`caption` 三字段缺省），因为它是纯动作镜。
> 有台词的镜再补这三个字段，写法见 `references/prompt-templates.md`。
>
> **字数不是凑出来的**：引擎地板是 `image_prompt` 110 字 / `video_prompt` 140 字
> （真源 `pipeline/variation.py` 的 `MIN_IMAGE_PROMPT_CHARS`/`MIN_VIDEO_PROMPT_CHARS`，
> lint 的 `prompt_thin` 维度按它报警），那是**下限不是目标**——本例这一档
> （300~400 字）才是「写到位」的实际量级。写不满通常不是没话可写，
> 而是五层里漏了层：先自查哪一层是空的，再补那一层。

## 铁律（各家官方提示词规范的共同结论）

**裁决顺序（条款打架时按此序让路，低序为高序让路）**：
内容安全 ＞ 成本闸（主 SKILL 防烧钱五铁律）＞ 故事事实与台词本意 ＞ 资产与场景
连续性 ＞ 表演物理真实 ＞ 镜头语法 ＞ 风格标签。例：为凑一个好看的运镜改台词
（镜头语法凌驾剧情事实）、为风格统一让角色换装（风格凌驾资产连续性），都是反例——
拿不准让谁让路时，顺着这条链往上找答案。

> **动作链写法的完整规范（强制）详 → `references/video-prompting.md`**：八层分工总图、
> 「起承收」动词链十条铁则（治僵硬/动态漫画感）、运镜纪律、@图片N 多参考图绑定、
> 反模式清单与字段分工速查——写任何 `video_prompt` 前先过一遍那份清单。

1. **一镜一主运镜**：两个运镜叠加必坏（Seedance 官方规范明写）；一镜到底/组合长镜头
   仅按《进阶运镜预设库》受控使用（▲档纪律：native + 分时段描述 + dur≥10s）。
2. **一镜一主动作链**：动作按时序写成「起→承→收」的连续动词链（"右肩先沉带动转身，
   裙摆荡开半拍回落，两步后在门口停住"），具体到部位+幅度+速度、每个动作有终点；
   单个抽象动词（走/看/笑）是僵硬「动态漫画感」的第一成因。
3. **慢运镜远优于快运镜**：slow push-in / pull-out / pan 是安全集；快速环绕/甩镜属
   ■高危档（见预设库），默认不排。
4. **增量编译：`video_prompt` 只写"变了什么"**。视频请求**恒带这一镜的分镜图**
   （native/首尾帧=首帧驱动，dubbed=参考图），画面基底已经给定——引擎会自动前置一句
   增量契约句（措辞按喂图角色二分），你只需要写增量：动作怎么变、终态停在哪、
   光线怎么走、镜头怎么动。**复述主体外貌与场景 = 要求模型重画一遍 = 跨镜漂移头号来源**，
   `lint` 的「复述重合率」维度会量化告警。
   - **缺笔时引擎不回退 `image_prompt`**：没写 `video_prompt`
     就按 `action`/`end_state`/`light_shift` 拼 delta 骨架，三者也空才落固定兜底句
     （"保持不变 + 轻微自然运动"）——宁可平淡也不漂移，但那一镜等于没有运动设计，
     `gen-video` 会逐镜点名提醒，看到就补。
   - **首尾帧衔接的镜要写过渡**（章级/镜级 `frame_chain` 显式开启才有）：引擎发出末帧时会追加「只写首帧到末帧
     之间的过渡过程、不复述末帧、运动收束在末帧上」的铁律句，你的 `video_prompt`
     照此写成**过渡句式**（「陆昭从伏案的姿势缓缓直起上身，视线抬向门口」），
     并把 `end_state` 写成下一镜首帧的姿态；末帧长什么样不用写，模型看得到那张图。
     `gen-video --dry-run` 里带 `末帧=镜N` 标记的就是这类镜（**跨转场断链、
     弃用镜跳过、下一镜缺图则整镜退回常规生成**，标记与实际请求恒同源）。
5. **负面约束走 `negative_prompt` 字段**：引擎按模型适配（国产编译为"避免出现：…"肯定式约束句），
   **作者原话恒在前、引擎地板（防字）追加在后**；不必手写"无字幕/无水印"，引擎已兜底。
6. **反主观词 → 视觉成因**（写提示词的第一纪律，不是审完再补救）：提示词里**只写镜头拍得到的
   物理事实**——光线走向、材质工艺、构图占比、身体姿态、空气介质。主观评价词（唯美/精致/氛围感/
   电影感/高级感/史诗感/震撼/梦幻/治愈/栩栩如生…）**必须先翻译成视觉成因再落笔**：
   「唯美」→「逆光暖调，发丝边缘透光，背景高光散成圆形光斑」；「氛围感」→「雾气与浮尘在斜射光束里
   翻涌，远处景物被雾吃掉一层」；「情绪饱满」→「眉头拧成川字，下颌绷紧，太阳穴青筋凸起」。
   **「高质量/高清/大师级/杰作」一律直接删**——画质由画风前缀与 `--resolution` 决定，
   这类自夸对国产模型无效，只会稀释真正的画面描述、挤占有效 token。
   **词表与逐条改写建议的单一真源 = `engine/kinema/pipeline/variation.py` 的 `SLOP_TERMS`**
   （`lint` 的"反 slop 空词"维度即由它驱动，见上文）——本文件与 SKILL.md 只讲纪律、
   **不复制词表**；要增删空词改 `variation.py` 一处，全链（lint + 两处纪律）同步生效。
7. **情绪只能演，不能说**：画面四字段（`image_prompt`/`video_prompt`/`action`/
   `end_state`）里不许出现「愤怒/悲伤/紧张」这类情绪名词——镜头拍得到下颌与指节，
   拍不到概念，模型接到情绪标签只会退回均值脸。按 `performance.md` 第一节的身体
   六轴改写（普通镜 ≥2 轴、情绪重镜 ≥4 轴）；`emotion` **字段**照写（那是 TTS 的
   情绪档，管声音不管画面）。词表真源同上在 `variation.py`（`EMOTION_TERMS`）。
8. **画面点名，不用代词**：画面四字段一律用注册名点名角色与道具，不写「他/她/它」
   ——模型不知道代词指谁，且设定图自动挂载按 `name`/`keywords` 文本命中，代词
   命中率为零（写「她拿起它」＝这一镜没喂任何设定图）。对白不受此限。
   机制详解见 `performance.md` 第二节；lint 的 `prompt_pronoun` 会点名。

## 切分原则（场→镜，先定戏再落镜）

- **设计次序是「剧本→场→镜」，绝不是直接铺镜头清单**：先把剧本拆成场
  （scene——同一时空的一段完整戏，有目标、有转折、有出口），每场列节拍
  （beat sheet：谁做什么→局势怎么变），再决定这场戏用几镜盖（coverage）。
  一场 20~30s 的戏通常 **2~3 镜**：一条 8~12s 主戏镜承载节拍串 + 至多一两个
  punch 插入。跳过「场」直接排镜，产出的必然是互不咬合的镜头清单——每镜各说
  各话、割裂感就是这么来的。
- **章节总时长自下而上推导，绝不预设再砍戏**：总时长 = Σ场时长，场时长 =
  Σ(拍 2~3s + 台词字数÷4.3 + 呼吸 0.5~1s)。剧本按节拍推出来要 60s 就做 60s，
  为凑一个预设的 30s 砍设定、并句、删反应镜是本末倒置——被砍掉的恰恰是
  观众用来理解世界观的那部分。预算紧张时砍**场**（整场拿掉、剧情降级），
  不砍场内的节拍密度。
- **`native`/`dubbed` 的镜长缺省是长镜**：主戏镜 8~12s、揭示/环境镜 10~15s，
  3~6s 只留给确有必要的 punch（特写冲击/反应插入/揭示瞬间）——镜间恒硬切，
  每一次切换都是一次截断，碎切密集时 lint `montage_chop` 按 warn 拦。
  长短相间才是节奏：一场戏里长镜承载、短镜点睛，匀质 5s 与匀质 15s 同样单调。
  分档与长镜写法见 `video-prompting.md` 第七节。
  **kenburns 例外**：静图幻灯片按 2~3 秒一个画面变化、30~60s 切 5~10 镜。
- **怎么对应文案随语态走**：lead 解说语态每镜对应文案里的一个观点；sparse 剧情
  语态按**戏**拆——一场戏一组镜，对白/动作/反应各占其位（长镜缺省下多数反应
  收进主戏镜内，独立反应镜只给需要 punch 的时刻），旁白镜只做时空跳接
  （主 SKILL「语态先行」）。
- 分镜顺序严格跟随文案 Hook→Body→CTA；对白剧用正反打+反应镜文法——什么事件必配
  反应镜、反应镜三要素、反应不递归，见 `performance.md` 第三节（各风格 skill 的
  分镜法则叠加其上）。
- **场内相邻镜默认写承接契约**：上一镜 `end_state` ↔ 下一镜 `entry_state` 成对
  （lint `entry_continuity` 查单侧缺失）——硬切要「接得住」，靠的是两镜在动作与
  空间上的显式咬合，不是运气；跨场跳接不写（那是有意的断）。像素级承接再开
  `tail_relay`。

## 跨镜连贯性自检

**本节回答的是「画面本身对不对得上」**（→ 让硬切**接**得住）。**「要不要断开」不归你判**：
缺省档一镜一片、镜间硬切是既定形态，插转场是用户的取舍，你不代插也不主动提议。
判完发现"怎么接都跳"，如实说明是哪两镜、为什么接不住即可——先试着用改分镜解决
（换景别阶梯、调机位、补一句环境连续性），实在解决不了就把情况交给用户定夺。

分镜写完、跑 `lint` 之后、生图之前，**逐对相邻正镜**（跳过转场镜与 omt 镜）过一遍这六条。
命中即改 `image_prompt`/`camera`/`framing`，改完再进节点②：

1. **视线方向对不对**：A 镜人物朝画右看，B 镜的对象就该在画右侧（或 B 是他的主观视角）。
   对白正反打时两人视线必须**相向**——都朝同一侧看，观众会以为他们在看第三个人。
   写法：在 `image_prompt` 里显式写「目光朝画面右侧」而不是笼统的「看着对方」。
2. **不跳轴（180° 线）**：一场戏里两人的相对左右关系**全场不许调换**。A 在左 B 在右，
   下一镜就不能变成 A 在右。要过轴必须给一个过渡——**用分镜手段解决**：加一个中性正面镜，
   或让运镜带过（转场镜不算你的选项，那是用户才能插的）。
   多人场景以「谁和谁在对话」这条线为准，别按站位重排。
3. **景别阶梯**：相邻两镜的景别**跨一档最稳**（全景→中景→近景），**同档相接会像穿帮**
   （中景接中景，观众看着像同一镜卡了一下），**跨三档会跳**（大远景直接切大特写要有理由，
   通常是"揭示"或"冲击"时才用）。景别取值见上文《中英术语对照·景别 Shot Size》12 档。
4. **光位与色温延续**：同一场景连续镜的光从哪来、什么色温，必须一致——上一镜暖黄侧逆光，
   下一镜不能变成冷白顶光。要变必须是**剧情驱动的变化**（云遮日/开灯/进洞），
   且写进那一镜的 `light_shift`（这正是该字段存在的意义）。跨场景则随场景重设，不算漂移。
5. **主体出入画方向**：A 镜人物从画右出画，B 镜就该从画左入画（同向运动 = 继续走；
   反向 = 折返回来，是另一层意思）。这条对 `dubbed`/`native` 尤其要紧——
   动起来之后方向错位比静图明显得多。
6. **承接契约咬合（dubbed/native 章）**：要求两镜连续承接时，把承接写成一对结构化
   字段——上一镜 `end_state`（收在哪）+ 本镜 `entry_state`（从哪接），两者描述同一个
   画面状态（构图/人物位置/光线要能对上）。引擎把 `entry_state` 编译为提示词 delta
   骨架的首句「开场承接：」（英文模型 `Opens from:`），`lint` 的 `entry_continuity`
   查单侧缺失。硬切镜两侧都不写——承接契约是 opt-in，不是每对相邻镜的必填项。
   要像素级承接再开章级 `tail_relay: true`（上一镜片段真实末帧作下一镜参考图，
   与板/设定图/时间轴同发，详见 `docs/agents/tail-relay.md`）：文字咬合管语义、
   尾帧参考管画面，双保险互不替代。

**自检产物**：发现问题**当场改分镜**，不要留到出图后再 retake（图已经烧钱了）。
若某一处是**刻意破例**（例如故意跳轴制造不安），写进那一镜的 `shot_intent` 注明原因，
否则下次会话会当成漂移"修"回去。

## 画面比例与安全区（硬约束）

支持三种画布（`--aspect` / `--both` / `--aspects` 选择，可一次出多比例）：
**16:9 横屏 1920×1080（默认）· 9:16 竖屏 1080×1920 · 1:1 方形 1080×1080**。
字幕/特效引擎按各比例画布分别排版；默认出一套**主比例**图、其余比例用 Ken Burns
重构取景（省成本），`--image-per-aspect` 则每比例原生出图（构图最佳、成本翻倍）。

**竖屏 9:16（点名竖版投放时）构图约束：**
- **主体居中**（不用三分法），人物躯干居中，多人竖向堆叠。
- **视线在上 1/3**（约顶部 30~35%），头顶留 8~12% 空间。
- **顶部 10~15% / 底部 20~25%** 会被平台 UI 遮挡；**右侧约 120px** 是点赞/评论按钮簇 → 关键元素勿放这些区域。
- 字幕全程落**底部安全区之上**（引擎已按 MarginV 排版）。Hook 文字可放中央大号，CTA 放中下部但在 UI 之上。
- **原生竖构图**远优于横屏裁剪：`image_prompt` 里显式写"竖屏 9:16 / 竖版构图"。

**横屏 16:9（默认主比例）/ 方形 1:1 时**：比例标注随主比例改写（"横屏 16:9 电影构图"/"方形 1:1 居中构图"）；
横屏可用三分法与更宽的场景铺陈，方形保持主体绝对居中。

## 角色/风格一致性（每镜都要做）

- 首选**设定集机制**（角色/场景/道具设定图，节点①.5）——场景图与出场角色图**每镜强制参考**；
  道具设定图按本镜提示词命中 `name`/`keywords` **自动挂载 ∪ 显式 `shots[].props`**（见主 SKILL 铁律2）。
- 兜底三件套：`style.character_block`（50~80 词角色+美术块，所有分镜复用）、
  `style.seed`（固定整数）、`style.character_ref` / `style_board`（参考图）。
- 范式：**身份创建与运动创建解耦**——图像锁死画面，视频提示词只描述运动。
  细节仍会游走，每段视觉 ≤10s 最稳。
