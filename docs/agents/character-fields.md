# 角色清单字段与视觉语义

## 1. 系列级字段是单一真源

`characters[]` 的 `required_emotions` / `required_actions` / `required_views` /
`silhouette_notes` / `constraints` / `subject_kind` / `visual_requirements` / `voice_prompt`
全是**你填**（非 engine-managed）。

`voice_prompt` 是声线描述：六槽位 40~80 字——性别年龄段、音区明暗、音质质感、语速节奏、
口音吐字、气质，不写情绪词（情绪归句级 `emotion`），槽位表与范例在
`.claude/skills/kinema/references/voice-casting.md` 铁律 3。描述同时是 seed-audio 造声的
条件文本与 native 绑定句里的音色特征，写薄了两头都锚不住。`character add/set --voice-prompt` 写入即按它定制一把音色并立档启用；档案的
`prompt` 记造声时用的那段，之后改这里不回写档案。写实档另按声区对表：男声常规带约
85~155Hz、女声约 180~260Hz，落在异性重叠带的描述锚不住。

语义定死为「这个角色**全系列**要演到的情绪/动作/视角 ＋ 剪影特征 ＋ 硬禁忌」。

设定集是**系列 → 章节单向覆盖**（`sync_design_to_chapters`）：按集填的值下次 `project refs` 就被
冲掉。「本集所需」由引擎从 `shots[].emotion` 推导。

角色视觉字段再增加两项，专门解决“正向特征被当成禁词”和“动物规则误伤人类”的问题：

- `subject_kind`：显式主体类型，只认 `human` / `animal` / `creature` / `robot` /
  `spirit` / `other`。不从 `appearance`、`role` 或角色名字猜测；缺省按 `other` 处理。
  只有 `animal` / `creature` 会启用动物专项的无穿戴负面规则。
- `visual_requirements`：必须保留的正向视觉特征，如「左臂义体」「圆框眼镜」「红色军装」。
  它同时进入角色设定图与分镜图/视频的正向角色锚，不进入 `negative_prompt`——
  定义外观的那张表必须先画出它，下游逐镜的「必须保留」才有可核对的来源。

角色缺省是气色健康、神态有精神的人：`appearance` / `role` / `outfit` / `hair` /
`silhouette_notes` 不写黑眼圈、眼袋、血丝、憔悴、无精打采这类疲态，夜班、凌晨一类题材
也不推导出疲态；只有用户明确要求时才写，并把该特征登记进 `visual_requirements`——
这既是「必须保留」的语义，也是 lint `character_fatigue_look`、`character add/set` 提醒与
`project refs` 出图闸的放行判据（判据单源 `variation.fatigue_look`）。

三类信息必须分开：

| 字段 | 语义 | 落点 |
|---|---|---|
| `outfit` / `hair` / `weapon` | 已登记的服饰、发型、武器或持物 | 角色正向外观；有设定图时主要由像素参考承载 |
| `visual_requirements` | 必须出现的正向视觉特征 | 角色设定图 ＋ 分镜图/视频正向角色锚 |
| `constraints` | 画面禁止项 | 分镜图/视频 negative 通道 |
| `taboo_lines` | 台词与行为人设禁区 | 文本/人设检查，不进入图片或视频画面负面 |
| `voice_prompt` | 声线描述 | 定制音色的生成剧本与 native 锚定绑定句的描述括注 |

因此，人类角色的服装、眼镜、义体等不会因为动物专项规则被删掉；动物角色的“不穿戴”也不会
凭空扩散到所有角色。旧数据没有 `subject_kind` 时不会猜测，若需要动物专项效果，应显式补登记。

## 2. 三条流转路径各走各的

| 路径 | 机制 | 注意 |
|---|---|---|
| 存量章节 | `char_fields` 白名单 | **新增设定字段必须登记** |
| 新建章节 | `create_chapter` 整份拷贝 | — |
| `upsert_entities`（`adapt` 重抽） | **绝不认这些系列级人工字段** | 登进去 = 下次重抽清空人工创作 |

## 3. 落点分裂是刻意的

| 字段 | 落点 | 理由 |
|---|---|---|
| `silhouette_notes` | **进** `sheets.char_sheet_prompt`（插在 appearance 之后） | 跨镜一致性强锚点 |
| `visual_requirements` | **进** `char_sheet_prompt`（appearance/剪影之后）＋ 分镜图/视频正向角色锚 | 必须保留的正向特征不能写进负面；设定图上没画出来的特征，下游每一镜都在保留一个不存在的东西 |
| `constraints` | **永不进** 正向设定图/视频角色锚 | 见下 |

用户自由文本禁令会与两条引擎铁律正面竞争——「武器不上角色设定图（归武器设定图）」「全身像双手空手」；
「必须持剑」直接顶撞「双手空手」。

`constraints` 的正确落点：写分镜时编译进图像与视频的负面通道，并当角色一致性判据。
`taboo_lines` 不属于视觉约束，不能用来驱动画面生成；如果某条行为禁区同时要求画面形态，
应把对应的视觉部分另写进 `constraints`。

## 4. 填了字段，存量设定图不会自动重生

**改了 `silhouette_notes` 要让图变，必须**：

```bash
python3 -m kinema project refs <项目> --only character:<名> --force
```

同名角色已有 sheet 时 `project refs` 直接跳过。`--force` 花钱、需用户授权，旧图自动进版本栈。

别把「填了字段但设定图没变」误判成功能没生效。
