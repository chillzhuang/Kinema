# 音色档案库（voicebank）

**单一真源** `engine/kinema/voicebank.py` ｜ **守卫** `tests/test_voicebank.py`

## 1. 两级对象：候选是临时物，档案是资产

| | 候选（audition） | 档案（cast） |
|---|---|---|
| 回答的问题 | 这几把声音里哪把合适 | 这把声音是什么、从哪来、还在不在用、谁在用它 |
| 生命周期 | 整批覆盖，只留最近 `KEEP_BATCHES` 批 | 追加，永不改写 |
| 落位 | `assets/voices/auditions/<实体>/{preset\|custom}/<批次>/` | `assets/voices/casts/<档案号>.mp3` |
| 页面表现 | **绝不画选中态**，只标「未入档 / 已入档」 | 唯一的选中真源 |

选中态一旦挂在候选上，重新试音换掉整批之后，页面就会把另一条音频显示成「已选」——
下标指向的东西变了。这是本模块存在的直接原因。

## 2. 三条结构性约束

1. **档案音频不可变。** 定制音色每次演绎都不同，被选中的那条音频**就是**这把音色本身
   （全片每句拿它当 `ref_audio` 合成）。落在按实体名命名的固定路径上，再选一次就等于把
   上一把声音物理销毁。
2. **定制音色的 `voice_type` 按档案唯一**（`custom:<档案号>`）。分镜留痕 `gen.audio.voice_type`
   记的就是它——这是「哪几镜用了哪一把声音」唯一可计算的依据，也是删除闸的地基。
3. **不设「当前启用」指针。** 在用 = 实体的 `voice` 指向哪条档案（`cast_for_ref`）。模版音色
   重复选中同一把时**复用**既有档案，`(实体, 音色引用)` 因此唯一可解。状态存两处必然漂移。

## 3. 数据契约

项目文档顶层 `voice_bank`（**随建章与每次启用整份复制进章节**——章节要能脱离项目文档
独立渲染，定制音色的参考音路径与声线描述只能从这里解析）：

```jsonc
"voice_bank": {
  "seq": 3,                                  // 档案号发号器
  "casts": [{
    "id": "vc_0002", "owner": "旁白", "mode": "custom",
    "voice_type": "custom:vc_0002",          // 模版=官方音色 ID
    "alias": null,                           // 模版=音色别名
    "prompt": "55 岁左右的中年女性…",          // 定制=造出这把声音的原话
    "clip": "…/assets/voices/casts/vc_0002.mp3",
    "source": {"kind": "custom", "batch": 1, "no": 1},
    "speech_rate": 2.4,                      // 试音台词字数 / 音频时长（归一化字/秒）
    "at": "…", "used_at": "…"
  }]
}
```

`speech_rate` 在立档时按候选音频实测（`_register`），lint `narration_overrun` 据它在花钱前
预估台词能否落进画面窗口；候选块记下试音台词 `text` 才算得出，音频探不出时长不记。
跨项目引入沿用源档案的值。

实体（`characters[]` / `narrator`）只留四个键：`voice`（在用音色引用）· `voice_prompt`（定制路径
写入的声线描述）· `audition` / `custom_audition`（`{batch, at, prompt?, text, entries[]}`，临时物）。

**形状不作假设，读写两侧同一条判据**（`_obj`/`_rows` 读侧、`_slot` 写侧）：文档是长期
演进的用户数据，盘上留着上一版形状（候选块为裸 entries 列表）。读侧「当没有」返回
游离副本即可；写侧必须**就地换成空壳**——`setdefault` 在键存在而值是 null/列表时原样
返回旧值，随后 `.get`/`.append` 当场抛 `'list' object has no attribute 'get'`，
而这条只在用户点「试音」那一刻才现形（项目页早被读侧挡住、照常打开）。
旧形状一律按作废丢弃，绝不翻译：下一次试音就地顶掉，批次号从 1 重新起。

## 4. 引用账：什么情况下不许删

单一真源 `cast_references(series, cast_id, index=None)`，CLI 与 Studio 共用，
**前端只展示不自算**。索引 `reference_index` 全项目扫一遍即可服务所有档案——
逐条重扫等于把章节文档读上十几遍。

| 级别 | 判据 | 处置 |
|---|---|---|
| 在用 | 实体 `voice` 指向它 | 先换成别的档案 |
| 已产出 | `gen.audio.voice_type` / `gen.audio.cast[].voice_type` / `versions.audio[].params.voice_type` / `versions.audio[].params.cast[].voice_type` 命中 | 禁删，点名到镜 |
| 已指派 | 章节 `voices{}` / `narrator_voice` / `shots[].voice` / `lines[].voice` 命中 | 禁删，先改派 |
| 无引用 | 以上全不中 | 摘条目 + 删音频，整段在 `commit()` 内 |

判据**只认 `voice_type` 不按说话人细分**：宁可多拦一条，不可误删一把已烧进成片的声音。

`character rm` 连带摘除各章节 `voices{}` 里该实体的指派（`Series.remove_character`）；
`_sync_chapters` 收到空引用时摘键而不是写 null——否则一个已不存在的角色会以「仍指派着它」
挡住删档。

## 5. 音色血缘

`stage_tts` 的重合成判据是「wav 在不在盘」，它看不见音色换没换。故 `propagate` 在每次
启用后按当前指派重解析每一镜应该用哪把声音，与 `gen.audio` 留痕比对：

- 不一致且**未锁定** → 置 `retake`（下次 tts 自动重出并归档旧版）；
- 不一致且**已通过** → 只挂 `voice_stale` 等人裁决（锁是人给的，机器不越权解锁）；
- 解析不出音色（回落 profile 默认）→ **不判**：猜错的代价是让人白花一次重配的钱。

**标与清同一处**：换回原来那把之后盘上音轨重新对得上，标记与那条 retake 一并撤销，
且**原样**还回覆盖前的表态（存在 `voice_stale_prev`）——不替人判成「通过」。
只撤 `STALE_NOTE` 那一笔，人自己打回的重做不动。两个字段进 `project._SHOT_HUMAN_KEYS`。

### 片段侧的同一条边

native 对白镜的人声由视频模型念出、从不跑 tts，`gen.audio` 恒缺席——只比对配音留痕的话，
换音色后成片人声原样不变且零提示，而 native 正是对白上镜章的默认制式。
故 `propagate` 另有一支比对**实发过的锚定参考音**：留痕在 `gen.clip.envelope.references`
里 role=`voice_anchor` 的行，id 形如 `shot:<镜号>:voice:<voice_type>`。分级与撤销同上，
标记字段是 `voice_clip_stale` / `voice_clip_stale_prev`。三条差异必须记住：

- **判定严格单向**：只认「烧进去的这把已不在选角名单里」。反过来问「名单里多了一把
  却没烧」会在刚给某个说话人补上选角时立刻命中，而置 clip retake 是**按秒重买整镜**；
- **重算能锚定哪些音色时参考位给足**：条数上限由 provider 决定、各档不同，用缺省值重算
  会把当初超位没附发的说话人算进来，凑出一组假过期；
- **clip 的 retake 不带 note**：`review.get_note(shot,"clip")` 会被编进下一版视频提示词的
  「本次修正重点」，写 note 等于花钱把「音色换了」当画面意见发给模型。归属只靠标记字段认。

快照版本号与画布对不上时**不判**：`versioning.rollback` 只搬文件不动 `gen`，回滚过的镜
其 envelope 描述的是最新生成的那一版，不是画布上这一版。

## 6. 命令与入口

```
character add/set <pid> --name X --voice-prompt "…"                缺省路径：按描述定制一条并立档启用（voicebank.cast_custom）
voice custom   <pid> [--name X | --narrator] --prompt "…" [--adopt N]  定制生成（seed-audio-1.0）；--adopt 立档启用第 N 条（同上一条实现）
voice audition <pid> [--name X | --narrator] [--candidates a,b]   模版试音（显式例外）
voice use      <pid> [--name X | --narrator] --no N [--custom]     候选立档并启用
voice use      <pid> --cast vc_0003                                换回历史档案
voice bank     <pid> [--name X | --narrator]                       档案 + 引用账
voice rm       <pid> --cast vc_0003                                删除（引用闸拦）
voice list     <pid>                                               全员概览
```

网页入口=项目页角色卡与「旁白选角」卡：在用 / 候选 / 档案三段式，删除按钮有引用时
**置灰而非隐藏**（隐藏会被当成 bug），理由写进提示。

`voice use` 收尾会预热这把声音的锚定参考音（`ensure_anchor_clip`，**在 `commit()`
之外**——那是一次几秒的 TTS 往返，放进锁里就是拿文档锁按住整个合成）：定制音色直接
用档案那条不可变音频、零请求；官方模版音色现合成一句 `ANCHOR_TEXT`。这样章节页的
「参考音频N」当场可试听——听准嗓音再决定开不开按秒计费的生视频。预热失败不冒泡，
选角本身已经写盘成立，真发那一刻还会再试一次。锚定音上线前选定的老项目走章节页
「♪ 合成一句试听」（`/api/voice/anchor-warm`）按需补，不必重走试音。

## 7. 与 voicecast 的边界

`voicecast` 是**镜级配音策略**（每一句用哪把声音、占多长时间），跑在渲染热路径上；
本模块是**编辑期的选角**。依赖单向：voicebank → voicecast，绝不反向。
