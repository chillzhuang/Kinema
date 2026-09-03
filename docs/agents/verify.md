# 成片自审 verify

**阈值单一真源** `pipeline/mediacheck.py`

```bash
python3 -m kinema verify --chapter x/<ch> [--aspect 16:9] [--samples N] [--json]
```

## 1. 定位：自愿闸，不接进 `assemble`/`run`

与 `--draft` 逃生舱同一哲学：**合成该出片就出片，体检另开一道**。要查就显式跑。

零 API 成本、纯本地 ffmpeg 探测、**只读产物**，结论写 project.json 顶层 `verify`（engine-managed）。

## 2. 检查项与失败等级

| 项 | 等级 |
|---|---|
| 成片缺失（`output` 为空或文件不在） | 硬失败（非零退出） |
| 容器无效 | 硬失败 |
| 黑屏（已排除转场黑场窗） | 硬失败 |
| 该响却哑 | 硬失败 |
| 时长对不上时间轴 | 硬失败 |
| 字幕文件缺失 | 硬失败 |
| 有词章未登记旁白轨 | 硬失败 |
| 动镜档正镜缺片段 | 硬失败 |
| 削波 | 待修 |
| 响度偏离 | 待修 |
| 字幕少条 | 待修 |
| 旁白轨语音落点异常 | 待修 |
| 人声文字与台词不符 | 待修 |
| 单句漏念（`voice_line_dropped`） | 待修 |

「待修」不拦出片，重合成即修。

报告顶层 `voice` 节与比例无关（音轨全比例共用一份），按声源两态：

**旁白轨语音落点**（dubbed/kenburns）：逐镜比对时间轴窗口与旁白轨里实测的
语音段——有词镜窗口内须检出语音（`voice_missing`），无词镜窗口内超过 0.4s 的
语音段点名复听（`voice_stray`，容忍窗口边界的续音）。开口对齐会把语音起点安排
在窗口中段，故只判有无、不判头部位置。判据对象恒是 assemble 重拼后的
narration.wav：它是烧录真源且不含 BGM；对带 BGM 的成片本体做振幅级语音检测
无法区分人声与音乐。

**人声文字核对**（native，`kind="asr"`）：模型声源的镜（混烧下只查对白镜）用
本地 ASR 转写片段自带音轨、与章节台词比对，**稿面召回**低于
`VOICE_TEXT_RECALL_MIN=0.6` 记 `voice_text_drift`——这是 lint
`native_voice_unverified`「待核对」的核对出口。用单向召回而不是双向相似度：
问的是「这一稿念了多少」，长句念一半就转场是模型声源的主要失效形态，而对称
口径在转写只有稿子前 43% 时仍会放行。判据对象是 gen_clips 底片而非成片
（成片混了 BGM/环境床只会稀释判据；底片音轨即烧进成片的那条），逐比例出片时
各比例的片段逐条转写。报告里的分母只数真正转写过的片段，跳过的另行说明。
整镜召回达标后再按 `lines[]` 逐句摊回（`asr.line_recalls`：整稿一次匹配、匹配块按句区间
分摊，逐句独立比对会让短句的字被别句的转写认领），单句召回低于
`VOICE_LINE_RECALL_MIN=0.5` 记 `voice_line_dropped`——整镜阈值是全稿字数的比例，
两字句整句漏念只掉 9%，而字幕按稿面烧录，漏念句会成为一条无声字幕。
faster-whisper 未装时照实标 `available: false`（装：`pip install -e "engine[asr]"`），
不装不拦。scored 的人声整轨替换，本节不产出。

**`voice_text_drift` 是复听清单，不是重生指令。** ASR 是转写不是耳朵，两类假警在
`pipeline/asr.py` 收口：
- **VAD 吞轻声**：Silero 对压着嗓子说的台词整段判负，转写为空 → 报「念出 0%」。
  判空即无 VAD 复解一次（`asr.transcribe`；`_decode` 另把静音上复述引导句的转写清空）；闭声镜没有稿面文字、不进核对，
  幻听字对不上台词，分数照样低。
- **数词字形**：字形引导句压不住阿拉伯数字（「零七，报数」→「07 报数」，召回
  掉到 0.5）。`asr._norm` 把数词折成汉字再比。

同音异调（答/打）这一类转写层解决不了，漏判与误报都会有。**发现即写审阅意见交人
裁决，不自动重投**：重生按秒计费，据一条可能错的转写重投就是拿钱赌 ASR。

## 3. 三个最容易做错的点

### 3.1 黑帧判据必须双条件

**`YAVG≤20` 且 `YMAX≤24`。**

YMAX 是关键判别量：真实夜戏实测 YAVG 才 40 但 YMAX=255。只看 YAVG 会把 cyberpunk 雨夜 /
dark_fantasy 夜戏整片误判成黑屏。

### 3.2 抽样必须排除转场黑场窗

排除范围：转场镜整段 ± 两侧 `edge`。

否则每条带转场的片子都误报——`fade`/`fade_black` 的字卡本身就是满屏纯黑（实测 16/16）。

### 3.3 结论块里只准出现有限数或 `null`

`verify` 的结论要 `project.save()` 落进 project.json，而 `Infinity`/`NaN` 不是合法 JSON。

事故链条：整段静音（正是 verify 要抓的 `mute` 事故）时 loudnorm 报 `input_i="-inf"` →
`mixdown.parse_measurement` 如实转成 `float('-inf')` → 原样写进结论 → `json.dump` 吐 `-Infinity`
→ project.json 从此 `JSON.parse` 失败 → Studio 章节页 `res.json()` 抛错整页「加载失败」，只能手工
删 verify 块救。

**故凡从 ffmpeg 数值输出来的量，写 rep 前一律过 `math.isfinite`**（非有限 → None，纪律与
`mixdown.gain_to_target` 同源）。新增数值字段照办。

## 4. 改阈值的前提

改阈值必须先在真实成片上重新标定。

## 5. 网页入口

章节页「后期 → 自审」按钮（后台任务）＋ QC 区自审条。
