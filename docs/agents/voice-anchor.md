# 音色锚定（native 生视频按选角发声）

`motion: native` 下模型原生配音的固有缺陷是**每镜自选嗓音**：同一角色跨镜、跨集
必然漂移。音色锚定把选角链的音色样本作为 `reference_audio` 随生视频请求附发，
提示词按 `@配音N`（与 `@图片N` 同一套按 content[] 附发顺序的位序寻址）把说话人
与样本绑定，模型用该嗓音念提示词里的台词——口型、台词、嗓音三者同源于一次生成。

寻址落在两处、编号同源：逐句台词的说话人挂标记（`凯尔 @配音1 说：“…”`——多角色
镜靠它分清第几秒那句用哪条参考音），绑定句在尾部交代音色归属（单音频保持实测
定稿的「所附参考音频」措辞，多音频按 `@配音N` 逐人点名）。Studio 提示词预览里
`@配音N` 即点即听（含待预热的现场合成），编号映射由引擎按实附顺序下发。

逐句 `@配音N` 标记与多音频绑定句的 `@配音N` 措辞经双锚定小样验证（两句各归
各锚的声区，编号绑定成立，n=1）；三锚定形态未验证。

## 实测基线（seedance-2.0-mini，付费小样；F0 判定用 pyin，60~350Hz 取中位）

- 性别对照（男/女/无锚定/重复四组）：输出人声**声区跟随锚定**（女锚把模型缺省
  男声 140Hz 拉到 195Hz），台词照读、口型同步，人工试听确认音色一致；
- **样本时长影响跟随幅度，但决定项是锚定音落在哪个声区**：4.3s 短试音句只到
  声区跟随（锚 91Hz → 成片 153Hz）；14.8s 长样本对一把 94.2Hz 的低音男声达到
  音高级贴合（锚 94.2Hz → 成片 94.2Hz）。但反例同样确凿——一把 195Hz 的偏亮
  男声，10.8s 未裁样本实测只到 108Hz，反而**差于**同音色 7.4s 裁剪样本的 167Hz
  （clockshop 前身 voicelab3d/ch01，四镜）。`voicecast.ANCHOR_TEXT` 现为 57 字、
  实测合成 10.8~11.9s；不按「越长越贴」加长它，那条结论只在锚定音本就落在
  模型该声别的常规带内时成立；
- 双音频分绑（角色→音频1、旁白→音频2）：前半 151Hz / 后半 200Hz，**编号绑定成立**，
  旁白句闭唇与角色口型并存；@配音N 句级标记形态复验于双锚定对白镜
  （两句实测 110.7Hz / 283.8Hz，声区各归其锚 94.2Hz / 165.8Hz）；
- 参考音频不额外计费（四组 usage 相同）；
- **合计时长超限是建任务 400**（7.2s+12.7s 两条即被拒）——2.0 系列 ≤3 条、合计 ≤15s。

## 数据流与判据（单一真源）

```text
voicecast.voice_anchor_plan   纯计划：谁锚定(who/voice_type/no)、谁未选角(loose)
        ├─ cli.stage_gen_video   dry-run 预览与真发共用；真发另做预热+裁剪+编号重排
        ├─ studio/scanner.py     逐镜 voice_anchor 投影 → chapter.js 「♪ 音色锚定」chip
        │                        （chip 闸 = anchor_ref_task 任务型态 × provider 参考音位：
        │                        衔接/锚定/previz 镜与无音位 provider 不标）
        └─ studio/actions.voice_anchor_warm   预览行「♪ 合成一句试听」的编号解析
                                 （按盘上选角解析编号，预热真发会用的那把）
voicebank.cast_custom         缺省选角：声线描述 → 一条演绎 → 立档启用
                              （`character add/set --voice-prompt`、`voice custom --adopt N` 都走它）
voicebank.assign_voice        模版别名指派：写实体槽位 + 同步已建章节 + 预热锚定音
                              （`character add/set --voice`、网页指派）
cli._cast_gate                tts / gen-video / score / run 花钱前点名没有音色引用的说话人
                              （gen-video `--no-auto-cast` 跳过，嗓音交给模型）
voicebank.anchor_clip_for     在盘事实：定制=档案 clip，官方=锚定缓存（缺则 None）
voicebank.ensure_anchor_clip  落盘：缺则现合成。`voice use` 选定即预热（锁外·失败
                              不阻断选角）+ Studio「♪ 合成一句试听」按需补，
                              目录同源 voicecast.series_ref_dir ≡ voice_ref_dir
prompts.voice_anchor_clause   绑定句（实测定稿措辞，改动须重做小样）
prompts.native_voice_clause   ≥2 句 + total 且 provider 时间轴单位为秒（2.5 系列）时逐句秒段
                              （voicecast.line_spans，与 scored 底稿同一份字数比例切分）；
                              2.0 系列只按顺序逐句列、不带秒段
```

- **只认显式选角**：角色句走 `lines[].voice` > `voices[speaker]` > `shots[].voice`，
  旁白句额外认 `narrator_voice`（旁白锁）；profile 缺省音色不锚定。
- **选角先于锚定**：`_cast_gate` 在 dry-run 与真发前都跑，没有音色引用的说话人
  （角色、只在台词里出现的 NPC、旁白）逐个点名并给出 `--voice-prompt` /
  `voice custom --narrator` 的修法；引擎不代选。换声走重新定制或显式换模版。
- **锚定 ≠ 音色复刻**：实测基线是**声区跟随锚定**（女锚把模型缺省男声 140Hz 拉到 195Hz），
  不是把参考音的音色一比一还原。CLI 与页面的措辞按这个口径写，别让「音色锚定 ✓」
  被读成保证；成片人声与字幕是否一致，仍要合成后听一遍（lint `native_voice_unverified`）。
- **选角前对一次声区**：跟随是往模型对该声别的常规带上收，锚定音落在异性重叠带
  时救不回来。实测一把 F0 中位 195Hz 的「男声」（男声常规带约 85~155Hz，女声约
  180~260Hz）给写实中年男角色，四镜实发 108/129/167/145Hz、跨镜摆动 57Hz；
  同章一把 222Hz 女声给中年女角色则精确命中 222Hz。画面形象不是原因——同章
  无形象的画外旁白（锚 113.5Hz）照样被拉到 90~94Hz。故选角时按锚定音的 F0
  中位数对表，别选中位落在异性带里的那一把。
- **生效面**：native × 全能参考缺省档 × provider `max_ref_audios > 0` ×
  章节 `voice_anchor`（缺省 true）。首尾帧衔接镜（首帧任务协议禁混参考媒体）、
  V2V、dubbed、scored 不参与；混烧（`native_voiceover`）按镜分治——闭声的
  旁白/无词镜不锚定（模型不出声，绑定句与闭声指令打架），对白镜照常
  （`voicecast.burn_muted` 单一判据）。
- **绑定句与实附恒同源**：预热失败某音色时，`cli._anchor_clips` 对存活项重排编号，
  绑定句与 `audio_url` 顺序由同一次重排产出——绝不声明一条没发出去的参考音。
- 官方音色的锚定缓存有三处落盘口，共用 `voicecast.anchor_ref_path` 一条命名：
  **选定时**（`voice use` → `ensure_anchor_clip`，让人先听准再决定开不开生视频）、
  `stage_tts` 批量预热、真发现场预热。谁先跑到都行，另两处直接命中缓存。
  文件名哈希连同 `ANCHOR_TEXT` 一起算：锚定文本换版后旧缓存自然失配、下次使用
  自动重预热——只按音色键命中的话，旧文本的短样本会被继续发出去，音色跟随
  档位被静默拉低（样本时长直接决定贴合幅度）。
  dry-run 与页面只读在盘事实、标「待预热」，绝不落盘。
- 超总时长的样本按预算均分裁剪（`voicecast.anchor_budget_cap`，dry-run 注记
  逐条标实发时长与它同口径），产物落项目级 `assets/voices/fit_*.mp3` 幂等复用。

## 排错

| 症状 | 先查 |
|---|---|
| 建任务 400 | 参考音合计时长超限（能力位 `max_ref_audio_seconds`）；单条须 ≥2s |
| 复述锚定音内容而非台词 | 绑定句缺「只提供音色，不要复述参考音频里的内容」半句 |
| 某角色仍漂移 | lint `voice_anchor_gap`：该说话人未显式选角；或预热失败已点名告警 |
| 页面标锚定、实发没带 | scanner 与 cli 必须同走 `voice_anchor_plan`，chip 闸另含 `scanner.anchor_ref_task`（任务型态）与 provider 参考音位——分叉即 bug（守卫 `TestScannerAnchorScope`） |

守卫：`tests/test_voice_anchor.py`（计划/绑定句/请求体/lint 四面）。
