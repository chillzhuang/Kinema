# 口型精修（lipsync）——dubbed 章对白镜的增强步

## 1. 定位

对白上镜的章按纪律走 native（模型自声 + 音色锚定，口型与发声同源），dubbed 的领地是全旁白
解说章；全旁白的 dubbed 章没有对白镜，本步无事可做。dubbed 章里仍留有对白镜时（lint
`dubbed_dialogue` 会点名），本步作为 `gen-video` 收尾缺省接线的增强步只在这些镜上生效。

dubbed 的声轨承诺由旁白轨兑现（选角配音烧成主音轨，见 `mixdown.md`），但底片的
口型是 Seedance 对参考音"对"出来的，精度一般。口型精修在**底片出齐后**调视频改
口型服务（输入=底片+该镜最终配音，只重绘口型区域），把对白镜的口型对齐到真正
会被听到的那条音轨上。

```text
gen-video（dubbed）→ 底片出齐 → stage_lipsync（缺省自动）→ assemble 烧配音
换音色：voice use → tts --force → lipsync → assemble     （Seedance 底片零重生）
```

对齐分两层：assemble 恒做**开口对齐**（`voicecast.dubbed_sync_offset`——配音在窗口内
平移到底片声轨的开口时点，字幕随行，零成本），修的是头部错位；本步修的是句内节奏与
口型形状，也兜住平移被窗口钳制的残差（台词几乎占满窗口时移不满）。lipsync 产物的
音轨即最终配音，开口对齐在其上自然测得零偏移，两层不打架。

## 2. 范围三闸（`cli.stage_lipsync`）

| 闸 | 判据 | 理由 |
|---|---|---|
| 模式 | 仅 `dubbed` | native 口型与发声同源；kenburns 无动态人像 |
| 语态 | 仅 `voice_kind == dialogue` | 旁白/静音镜按闭唇出片（`prompts.dubbed_voice_clause`），无口型可修 |
| 在盘 | 底片 + 该镜 wav 都在 | 缺料点名跳过，不拦主链 |

## 3. 产物与指针

- 产物 `gen_clips/shot_<id>_<tag>_lips.mp4` 是**派生物、不进版本栈**——底片与 wav
  在盘即可随时重算（同尾帧/简笔板纪律）。
- `shots[].clips` 切到 lips 文件（compose/verify/scanner 零改动照常消费）；
  `shots[].clips_base` 保 Seedance 底片，**重算恒以底片为源**——对 lips 再改口型
  会逐代劣化。
- 幂等按源指纹：lips 比底片与 wav 都新即跳过；`--force` 推平。

## 4. Provider（`providers/lipsync/volc.py`）

火山智能视觉「视频改口型」：`visual.volcengineapi.com` · Service `cv` ·
`CVSync2AsyncSubmitTask`/`CVSync2AsyncGetResult` · Volcano Signature V4。

- **`req_key` 必须显式配置**（官方接口文档给出，随算法档更替）；鉴权是视觉服务
  AK/SK（`VOLC_ACCESS_KEY`/`VOLC_SECRET_KEY`，与 ARK_API_KEY 不是同一套），变量名由
  providers 段的 `ak_env`/`sk_env` 声明，配置中心的密钥位、自检与 `#/model` 的
  「口型精修」能力牌都按它取；`host` 缺省 `visual.volcengineapi.com`、可在 providers 段覆盖，签名式接口没有 `base_url`。
- 视频与音频只收**公网 URL**——上云由 stage 复用媒体上云层（OSS）完成。
- 未配置（req_key/密钥/OSS）时 stage **点名跳过**：增强步不拦出片主链，底片按
  闭口型出片；`--no-lipsync` 本次显式关。
- 计费 `price_per_second` × 底片秒数，入台账 kind=`lipsync`。

## 5. 守卫

`tests/test_lipsync.py`：语态三闸（只修对白镜）· 幂等与换音色重算（wav 变新即
重跑）· 指针切换与底片保留 · 未配置优雅跳过 · volc 提交体/签名头/本地路径拒收 ·
缺省接线（gen-video 收尾自动进入，`--no-lipsync` 才关）。
