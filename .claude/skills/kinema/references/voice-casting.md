# 锁音色——选角流程全解（铁律3 详情）

3. **定音色（声线描述 → 定制立档 → 启用，全系列零漂移）**：立项时给每个说话人写
   `voice_prompt`，引擎按描述定制一把并立档启用。**声线描述按六个槽位写，40~80 字**，
   由人设推、不写情绪词（情绪归句级 `emotion`）、不点名真人或明星：

   | 槽位 | 写什么 | 例 |
   |---|---|---|
   | ① 性别与年龄段 | 一句定声别与年纪 | 六十八岁男性 |
   | ② 音区与明暗 | 低/中/高音区，偏亮/偏暗、偏薄/偏厚 | 低音区偏暗 |
   | ③ 音质质感 | 干哑/清亮/气声多/鼻音/颗粒感/胸腔共鸣强弱 | 嗓音干哑带颗粒感、胸腔共鸣弱 |
   | ④ 语速与节奏 | 慢/中/快；句中停顿习惯、句尾上扬或下沉 | 语速慢、句中常停半拍、句尾下沉 |
   | ⑤ 口音与吐字 | 标准普通话或地域感；咬字清晰或松 | 标准普通话略带北方口音、咬字松 |
   | ⑥ 气质与身份 | 由 `role`/`personality` 推 | 气质沉默克制 |

   合起来：「六十八岁男性，低音区偏暗，嗓音干哑带颗粒感、胸腔共鸣弱，语速慢、句中常停
   半拍、句尾下沉，标准普通话略带北方口音、咬字松，气质沉默克制」。
   **为什么要写满**：这段描述有两个消费者——seed-audio 按它造声，槽位缺一项模型就按
   训练集均值补一项，同一段描述的多条演绎彼此差得越多；native 生视频时同一段描述随
   锚定音进绑定句，视频模型按它校正声区与质感。「中年男性，嗓音低沉」这种十字描述
   两头都锚不住。

   命令——
   - 角色：`character add <项目> --name <角色> --voice-prompt "<声线描述>" …`（建档即定制）；
     重写描述走 `character set <项目> --name <角色> --voice-prompt "<描述>"`（重新定制，
     旧声烧过的 native 片段置 retake）。
   - 旁白：`voice custom <项目> --narrator --prompt "<声线描述>" --adopt 1`。

   三条命令落到同一引擎方法 `voicebank.cast_custom`。启用即**立一条音色档案**（音频复制进
   `assets/voices/casts/`，此后不可变）并**自动同步全部章节**的音色表；各镜 `shots[].speaker`
   只填角色名 → 引擎解析成这把音色，全程不漂移。选中那条音频**就是**这把音色（全片以它作
   参考音）。描述会一路发到视频模型：native 的音色绑定句带上同一段描述，厂商把「音色参考
   不准」列为已知问题、第一条解法正是在提示词里补音色描述。代价：seed-audio 同一段描述每次
   演绎都不同，`--adopt 1` 等于**没人试听就定档**；换一条演绎重跑 `voice custom`，或
   `voice use <项目> --name <角色> --custom --no <编号>` 选本批另一条。

   **真发前选角闸**：`tts` / `gen-video` / `score` / `run` 在花钱前检查每个开口的说话人
   （角色与旁白）都有音色引用，缺的逐个点名并给出上面的命令。引擎不从模版池挑音色，
   profile `tts.voice` 不作缺省旁白音色。`gen-video --no-auto-cast` 是本次跳过选角闸，
   未选角说话人的嗓音交给模型。

   **模版音色（显式例外）**：官方固定音色（seed-tts-2.0 的 uranus 音色，目录见
   `config/voices.yaml`，170+ 含角色扮演 ICL），**确定性、跨项目完全可复现**——要一把官方
   声音作品牌声纹时用它。`voice audition <项目> --name <角色>` 生成一批候选试音（同段台词
   不同音色，候选可 `--candidates` 显式指定或按性别自动补足）→ 试听后
   `voice use <项目> --name <角色> --no <编号>` 启用（或 Studio 角色卡点「用这条」）；旁白同权
   `--narrator`；`--voice <别名>` 直填不建档案。模版档案没有声线描述可发给视频模型（官方
   别名是标签不是描述），表现力只认 `emotion`（`audio_params.emotion`）。

   **档案是资产，候选是临时物**：候选整批可换、页面上不带选中态；选过的每一把声音
   各留一条档案，`voice bank <项目>` 看谱系、`voice use --cast vc_NNNN` 换回任意一版、
   `voice rm --cast vc_NNNN` 删（有分镜用它配过音就删不掉，引擎点名是哪几镜）。
   **换音色会传播过期**：已配过音的镜按新音色重解析，对不上的未锁定镜置重做、
   已通过的只挂标记等你裁决——不传播的话一章会停在一半旧声一半新声且毫无提示。

   **逐镜情绪表现力（默认就要有感情·别白开水）**：**每个有台词/旁白的镜默认都标
   `emotion` + `emotion_scale`(1~5)**——从台词情感推断（爆发→`angry`·5、离别→`sad`·3、
   告白→`tender`·4、惊愕→`surprised`·4、日常陈述→`neutral`/`happy`·2；豆包 2.0 情绪档
   如 happy/sad/angry/surprised/fear/excited/tender/serious/coldness/neutral…）。
   表现力通道按路径分两条：**定制**——`emotion`、`voice_instruction`（自然语言语气
   「用哽咽的语气说」）、`delivery` 与 `<cot text=情绪>` 标签编译进 seed-audio 剧本正文；
   **模版**——只有 `emotion` 走 `audio_params.emotion` 生效，`voice_instruction` 与 `<cot>`
   不下发（官方标准版会静默过滤，跑 tts 会打印提示）。语气类 retake 按路径改对应字段。

   **配音表现力契约 `shots[].delivery{emphasis, pause_before, pause_after, note}`**（选填·全片基调
   写顶层 `voice_performance{pacing, energy_curve}`）：
   · `pause_before/after`（秒·单侧 ≤5）**只在 kenburns 生效**，tts 会把它折进 `shots[].dur`
     （dur = 配音实际时长 + 停顿，每次从实际时长重算、幂等），停顿同时进旁白轨与画面时长，零额外成本；
     **dubbed/native 下一律不生效**——那两个模式的 dur 是向 Seedance 请求的计费秒数（按秒计费，单价见 models.yaml），
     对口型喂进去的音频里没有这段无声，折算等于每镜无效购买 1~2 秒空转。要停顿就用 kenburns。
     **「先 kenburns 出样片过审 → 再切 dubbed 动态化」这条主线也安全**：切模式时盘上的 dur
     虽已折着停顿，但 gen-video 请求秒数按逐镜配音实际时长取（读侧闸），不会按停顿计费、
     也不会在成片里留下等长的静默死区；这一步还会再打印一次「本模式下停顿不生效」。
   · **句尾处理（引擎缺省，不用写）**：seed-audio 只在整段生成的末端截音、末音节的衰减会被切掉，
     引擎给定制路每句台词垫一个句尾保护词，合成后按句级时间戳裁掉，逐镜 wav 本身即完整收尾；
     拼旁白轨时每段配音尾部再淡出 70 ms；kenburns 下每镜 `pause_after` 至少 0.25 s（写得更长
     照写）。尾留白走与停顿同一道门控，dubbed/native 不加。
   · `emphasis`（重读词）/ `note`（表演提示）与 `voice_instruction` 一起由
     `voicecast.delivery_instruction()` 编译成**一句语音指令**，进定制路的 seed-audio 剧本
     正文；模版路不消费它。
   · **绝不把重读/停顿写成台词里的 `<cot>`/SSML/多余逗号**：字幕逐字取 `narration`，标签会被
     烧进画面；官方音色可能把标签念出来；TTS 按字数计费标签白进字数。引擎只把它们编译成
     派生文本喂 provider，`narration` 原文一个字都不动。
   · **生效路径分两条，别记混**：改 `pause_*` → **重跑 `tts` 即可**（零成本，刷新 dur/旁白轨/时间戳）；
     改 `emotion`/`narration`/音色 → **必须 `tts --force`**（或先 `review set --stage audio --state retake`）——
     wav 已存在时普通 `tts` 根本不重合成，而重合成是要花钱的（done 锁定镜 `--force` 也不覆盖）。

   **选角进度断点续接（读 project.json 即知）**：判据是**实体的 `voice` 指不指向一条档案**
   ——`voice_bank.casts[]` 里 `owner` 是这个人、且 `voice_type`/`alias` 与他的 `voice`
   对得上，就是定档（`voice bank <项目>` 直接看）。角色读 `characters[].voice`，
   旁白读 `narrator.voice`，章节侧读 `voices[<名字>]` / `narrator_voice`。
   实体有 `audition`/`custom_audition`（本批候选，临时物）而 `voice` 还没指向档案 =
   待选定；`voice` 是个别名但档案库里没有对应条目 = 手工指派的，能用但没有可回听的音频。
   **档案库不设「当前启用」指针**——状态存两处必然漂移。
