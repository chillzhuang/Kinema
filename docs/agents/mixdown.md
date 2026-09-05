# 混音链

**数值单一真源** `pipeline/mixdown.py` ｜ **唯一落点** `compose.build` 的最终 filtergraph

> **绝不进 `kenburns.fit_clip`**——片段名不含音频参数 = 缓存键不失效，会静默复用旧音轨。

## 1. 信号链总览

```
旁白（0 dB 基准；混烧时先对齐对白，见 §7）
BGM（有旁白 0.3 ／ 独奏 1.0 不衰减）→ 让路 EQ → sidechain 闪避
  └ control_bgm：主音乐，独奏电平、不让路、不闪避；原生音按 NATIVE_BED_GAIN 入混（§6）
环境音 + 转场音效（SFX_GAIN=0.55 母线，不进侧链）
      ↓
   amix normalize=0
      ↓
末级响度归一 + 削波防护（I=-16 LUFS / TP=-1.5 dBTP）
```

### 1.1 BGM 闪避参数

先**让路 EQ** 挖 2kHz，再 **sidechain 闪避**：`threshold=0.05` / `ratio=8` / `attack=25` /
`release=400` / `makeup=1`。实测按旁白 mean -26 dB 标定。

## 2. 末级归一是两步线性

1. `loudnorm print_format=json` —— **只测不改**；
2. 挂静态 `volume=<g>dB` ＋ `alimiter(level=disabled)`。

**刻意不用单遍 loudnorm**：动态变增益会改 LRA，让「没改动就该复用」的确定性假设失真。

### 2.1 确定性的边界

同输入必得同输出，**确定的是增益链本身**：

- 实测两跑增益对账值一致；
- 无 `anoisesrc` 音源的章节两跑 md5 一致；
- 带雨雪雾环境音或合成兜底音效的章节因 `anoisesrc` 随机种子而 md5 必不同——这与混音无关。

## 3. 三模式钳制区间

| 模式 | 钳制区间 | 理由 |
|---|---|---|
| kenburns · dubbed | ±9 dB | 音轨全为引擎侧可控源（dubbed 主音轨是我们的逐镜 TTS） |
| native | ±14 dB | 主音轨是模型回吐的重编码音频，响度不受控 |

### 3.1 无旁白章节的独奏档

**`MASTER_SOLO`（+34 / -9 dB）**。

适用：白噪音/环境音沉浸、金句配乐（kn-quote）这类整章没有旁白的内容。在场的全是
引擎侧确定性源（已归一的 BGM ＋ lavfi 合成环境音），窄幅钳制会把整章卡在目标以下 9 dB——这是
「有的集吵有的轻」在这类内容上的残留。

## 4. BGM 入轨归一

BGM 另在 `providers/music/local.py` **入轨归一**到 -20 LUFS。库内同一情绪目录实测横跨 6.5 dB，
是「有的集 BGM 吵有的轻」的根因。

> ⚠️ **存量章节不会自动享受入轨归一。** `stage_music` 只在 `--force`、盘上没有 bgm、或盘上曲子
> 时长与时间轴相差超过 1s 时重生；时长没变的存量 `audio/bgm.mp3` 重新合成仍用旧文件。要让它生效：
> `music --chapter x/ch --force`（零 API 成本，本地曲库）再 `assemble`。

末级归一是整体推，救不了 BGM 与旁白的**配比**。

## 5. 转场音效提前量

独立常量 `SOUND_LEAD=0.25s`。

**绝不靠改 `TRANSITIONS.edge`**——那会连带改画面淡化时长与片段缓存键。xfade 族 `edge=0`，不提前
的话 scan 的 riser 蓄势音整段落在切点之后。

## 6. 曲库 BGM 的三档形态与合成前的闸

**BGM 母线是单占的**（`compose.build` 里只有一个 `bg_label`），所以「这一章有没有曲库 BGM」
是三档互斥判定，真源在 `compose.use_bgm_for`：

| 路线 | 缺省 | 显式加铺 |
|---|---|---|
| `audio_mode=scored` | 无（剧本自带配乐与音效） | `scored_bgm: true` |
| `motion=native` | 无（片段自带模型原生音） | `native_bgm: true` |
| `motion=native` + 深度捕捉 | 无 | `control_bgm: true`：用源片同一区间的音轨作 BGM，不从曲库选曲（`control/soundtrack.py`，与曲库同一条母线与响度口径）。它是这一章的主音乐而不是配在人声下面的背景乐：`compose.bgm_is_program` 判定后母线取独奏电平、不让路、不闪避，模型原生音按 `NATIVE_BED_GAIN` 退居环境床——走缺省配乐链会先压 10 dB 再由末级推回来，限幅器整段削峰；原生音按 0 dB 入混则它顶到 -1 dBTP 的脚步瞬态会让限幅器随每一步把音乐一起按下去。床在 `music` 阶段铺（`cli._stage_control_bed`）：先由 `control/sync.py` 量成片相对控制段的整体偏移写 `gen.control.sync`，够格的偏移平移该镜起点，再顺排；幂等判据是 `bgm_params.source == "control"` 加段落表指纹（含偏移）而不是片长——重框区间、换素材、对拍变了都不改片长，却已是另一段音乐 |
| kenburns / dubbed | 恒有曲库 BGM | —— |

`native_bgm` 与配音混烧（`native_voiceover` / `--burn-voice`）**互斥**：混烧已经把片段原生音
降为背景床占住 BGM 母线，再放曲库 BGM 进来会把那条床整个顶掉（模型自带的环境与空间感全丢）。
`cli._reject_native_bgm_conflict` 在 run/assemble 前拒绝这个组合，`compose.use_bgm_for` 按同一判据兜住——Studio 直调
`stage_compose` 是绕过 CLI 闸的真实路径。

### 6.1 `_bgm_gate`：只在有决定要做时发问

合成是会被反复重跑的节点，每次都拦着问一遍等于逼人闭着眼按回车。故只有两种情形发问：

1. **本章要用曲库 BGM 而本机曲库是空的**——`local` provider 会退化成合成正弦氛围床
   **并烧进成片**，那是明显的机器音，得在渲之前问要不要先跑 `music/download.py`；
2. **本章一条 BGM 都不会有，且从没就此表过态**（native 未写 `native_bgm`）——问一次，
   表态落盘，此后不再打扰。绑了带音轨的控制视频而没写 `control_bgm` 时，问曲库之前先报一行
   源片音轨这条路：那段音轨才是这支舞的配乐，只提曲库会把人引到一条与动作无关的曲子上。

其余情况只报一行事实。`assemble --bgm/--no-bgm` 预先作答即完全不发问。

### 6.2 非交互红线

`_ask_yes` 在非 TTY 恒取缺省且**绝不读 stdin**；但「缺省值不等于用户的意思」，故上面两处
发问点在落任何后果之前都**先硬判 `sys.stdin.isatty()`**：

- native 的表态**不落盘**——存下去等于替他做了决定，而且从此不再问第二遍；
- 曲库为空时**不自动拉库**——那是一次上百个文件的网络下载，无人值守的进程里自作主张
  开始下载是不能接受的副作用。

Studio 后台任务另在源头断开 stdin（`jobs._stream` 的 `stdin=subprocess.DEVNULL`）：不显式
断开时子进程继承服务端的 stdin，而 Studio 常常是从终端起的——带确认的闸会在那里读到一个
真 TTY 并停下等输入，表现是任务毫无输出地卡到看门狗超时被杀。

守卫 `test_mix.TestBgmGate`。

## 7. 混烧的两路人声对齐

`native_voiceover` 章里人声来自两处：旁白是 TTS 文件（电平随 provider 与音色走，seed-tts
定制音色实测 -27 ~ -31 LUFS），对白是模型回吐的片段音轨（实测 -13 ~ -16 LUFS，峰值已近顶）。
旁白轨若按 0 dB 基准入混，末级归一测到的整片积分响度由更响的对白窗口决定，只推零点几 dB，旁白窗口留在底下
（成片旁白 -31.8 / 对白 -13.2 LUFS，差 18 dB）。末级是整体推，救不了配比，与 §4 的 BGM 入轨归一同一条道理。

做法只落 `compose.build` 的混烧分支：

1. 旁白轨只测**旁白镜窗口**、片段音轨只测**对白镜窗口**（`measure_windows_args`，aselect 放行窗口内采样、
   asetpts 重排后交 loudnorm 分析），两个窗口集合都取自 `voicecast.voice_kind` 分治的 timeline；
2. 差值作旁白轨入混静态增益（`narration_match_gain_db`，钳 `NARRATION_MATCH_RANGE` = -12 ~ +18 dB），
   挂在 `narration_track` 的 sidechain 分叉之前——闪避由对齐后的旁白驱动，床在说话段才按设计压下去
   （TTS 电平常在闪避阈值 -26 dBFS 之下，不对齐则闪避不触发）；
3. 末级照旧整体推。

整章没有对白镜就没有对齐目标，不测不推；对白窗口测不到时打印一行并保持 0 dB。kenburns/dubbed 只有一路人声，
主轨是 0 dB 基准。守卫 `test_mix.TestNarrationMatch`。
