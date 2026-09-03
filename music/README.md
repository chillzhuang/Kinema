# 音频资产库（music/）

引擎的**音频资产总库**，两个子库：`bgm/` 背景音乐 + `sfx/` 音效。
注册表在 **`config/audio.yaml`**（场景 → 目录/标准文件名一张表），媒体文件不入 git（`.gitignore`）。

```
music/
├── bgm/                背景音乐（ELEVENLABS_API_KEY 为空时自动启用本地库）
│   ├── calm/           舒缓/治愈（语录、绘本）
│   ├── upbeat/         欢快/活力（口播、知识、解说）
│   ├── cinematic/      电影感/史诗（HD-2D、游戏故事、动画、写实 CG）
│   └── ambient/        氛围/空灵（环境陪伴、白噪音）
├── sfx/                音效（18 枚 · 峰值统一归一到 -3 dBFS，整套听感齐平）
│   └── transitions/    转场基础七键：whoosh 呼·横扫 / riser 吸·蓄势 / boom 咚·落点 /
│                       swish 唰·轻扫 / deep 浑·重扫 / glitch 滋·故障 / shimmer 铃·微光
│                       内容型打点十一键：pop 啵·弹出 / ding 叮·提示铃 / page 翻页 /
│                       paper 撕纸 / impact 砰·重击 / slash 刃·挥砍 / heartbeat 心跳 /
│                       wind 风声 / magic 术·魔法闪光 / clock 嗒·钟表滴答 / camera 咔·相机快门
├── download.py         一键拉取两套起始资产（**全部 CC0 / 公共领域**：BGM 103 首 + 音效 18 枚）
└── ATTRIBUTION.md      授权登记（全库免署名可商用；判定四条标准也在其中）
```

## 选取逻辑（引擎自动）

- **BGM**：profile 的 `music.mood` 显式指定情绪 → 直取 `bgm/<mood>/`；未指定按提示词/画风
  关键词兜底匹配（关键词表在 `config/audio.yaml` bgm 段）→ 目录内确定性挑一首，
  循环/裁剪到视频时长并加淡入淡出。库为空退化合成氛围床（会大声提示补库）。
- **音效**：三级解析——注册表有键且文件在（B 外置）→ 直接混入；缺文件（A）→ 纯 ffmpeg
  合成兜底；用户点名（C）→ `kinema sfx gen --kind <键> --yes` AI 生成落库。
  `kinema sfx list` 查看注册表与就位状态。

## 获取与替换

- **起始资产**：`python music/download.py`（BGM 与音效两套一次拉齐）。
- **换成你自己的**：正规授权音频按 `config/audio.yaml` 的目录/文件名放置即可，
  随时增删；来源与授权务必登记 `ATTRIBUTION.md`。
- **整体改址**：设 `KINEMA_MUSIC_DIR` 指向任意目录（bgm 与 sfx 都在其下）。

## 授权提醒

- **全库 CC0 / 公共领域——免署名、可商用、可修改、可随成片分发**：`bgm/` 103 首（FreePD 95 +
  freesound 8）+ `sfx/` 18 枚。署名类（CC-BY）与付费源**不进库、`download.py` 里也不留
  下载逻辑**——引擎按情绪确定性选曲，库里混一首 CC-BY 就等于哪一集会随机背上署名义务。
- 自己加曲子照四条标准过一遍：① 免费商用 ② **免署名** ③ 允许当背景音嵌入并随成片分发
  ④ 不限平台。逐条判据与已排除的源见 `ATTRIBUTION.md`。
- FreePD（freepd.com）2025 年已关站，`download.py` 从 Internet Archive 存档取同一批文件——
  CC0 不可撤回，站关了授权不变。
