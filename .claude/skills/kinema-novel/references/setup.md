# 开新书与接手导入

**只在开新书、或接手一本已有的书时读一次。** 日常写作不读这份。命令全在仓库 `engine/` 下跑。

| 节 | 什么时候用 |
|---|---|
| ① 第 0 步 · 定位 | 立项之前。五项没落在纸上就别开写 |
| ② 从零开工五步 | 新书：立项 → 文风契约 → 卷纲 → 角色 → 宪法与图谱 |
| ③ 续作与接盘导入清单 | 用户带着已有正文来（自己写的续作 / 别人写到一半的书） |
| ④ 多本书的边界 | 用户想在同一个世界观下开第二本 |

---

## ① 第 0 步 · 定位

一本 350 章的书，如果没有任何一处记着「写给谁看、靠什么让人追」，第 40 章之后每一次取舍都只能靠当轮上下文里的残影来判——赛道漂移与爽感稀释就是这么发生的，
而且写出去就收不回。五项，缺一项不开工：

| 项 | 要写到什么程度 | 怎么核对 |
|---|---|---|
| **题材赛道** | 一个词能挂上货架（都市异能直播流 / 硬核修仙种田…） | 说得出同赛道 2~3 本对标作 |
| **一句话卖点 logline** | 主角 + 处境 + 那个非看不可的钩子，40~60 字一句话 | 念给用户听，他能复述出来 |
| **核心爽感与金手指** | 爽点是什么、由什么机制**持续**产出、代价是什么 | 答得出「第 100 章还怎么爽」——答不出就是一次性设定 |
| **目标读者** | 谁在什么场景下读（通勤 / 睡前 / 追更） | 它直接决定章长与断章密度 |
| **篇幅规划** | 总章数 × 每章目标字数带（如 350 章 × 3000~4500 字） | 各卷 `from/to` 加起来能盖满总章数 |

落点（已核对过的写路径，**不要新造契约字段**）：

```bash
cd engine
python3 -m kinema project set <pid> --logline "一句话卖点"
python3 -m kinema project set <pid> --synopsis "赛道 + 爽感机制 + 目标读者 + 篇幅规划，一段话"
python3 -m kinema project show <pid>      # 复核：打印 design 的一句话/梗概/世界观
```

三条必须知道的事实，否则会以为写了就生效：

- **`design.logline` / `design.synopsis` 不进 `novel brief`。** brief 取的是 narrative_style、当前卷纲、
  宪法节、上一章 digest+state、未收伏笔、人设卡——定位只写在 `design` 里，写第 200 章时你**根本看不见它**。
  所以定位必须**同时**写进宪法第一节（第 5 步）；`design` 那两个字段是给用户和 `project show` 看的门面，
  外加 `novel sweep` 能搜到。
- **`adaptation.mainline` 目前没有 CLI 写路径**（引擎侧只有 `adapt show` 打印它、`novel sweep` 扫它，
  没有任何 `--mainline` 参数）。别为了写它去裸改 project.json——主线一句话写进 world_bible 的【零·
  一句话】节即可：那里有命令写、按节可取、每章恒在场。
- 每章目标字数带同样只是**你的**判据：lint 的 `short_chapter`/`long_chapter` 拿的是全书中位数，不认你设的带宽。

---

## ② 从零开工五步

### 1 · 立项 + 立骨架

```bash
cd engine
python3 -m kinema project new --title "书名" --id <pid> --skill kinema-novel
python3 -m kinema novel init <pid> \
  --pov "第三人称有限·跟随主角" --tense "过去时" \
  --voice "冷峻克制·短句快节奏" --diction "现代口语为主·系统词条精确化" \
  --avoid "眸光,不禁,空气仿佛凝固了"
```

`--skill kinema-novel` 让用户此后只报项目编号、AI 查 `project.skill` 就知道该调本 skill——缺省会按画风派生成某个 `kn-*` 视频 skill，
那是错的绑定。`novel init` 同时建出 `manuscript/`。`--profile` 此刻填什么都不影响（写作期一张图都不出）
，改编成片时再定。

### 2 · 与用户确认文风契约 → 补 baseline

顺序：口头敲定 pov/tense/voice/diction 与忌讳词 → **试写一段 800~1200 字给用户看** → 把用户点头的那一段（或用户自带的样章）
摘 2~3 段存进 baseline。

```bash
python3 -m kinema novel style <pid> --add-baseline 样章片段.md   # 给文件路径或直接给正文；可多次
python3 -m kinema novel style <pid> --add-avoid "眸,不由得"
python3 -m kinema novel baseline <pid> --from 1 --to 3           # 写满 3 章后立数值基线
```

- **baseline（文字样本）与 baseline_metrics（数值基线）两个都要**：前者供第③门自检并排比对（你读得出来的那部分）
  ，后者供 `novel lint` 算 z 分并报 `style_drift`。
- `novel baseline` 至少要 **3 章已登记**的正文，少于 3 章 σ 不稳、算出来的 z 是噪声，引擎直接拒。
- **没有 baseline 的文风门是空转的**——lint 会报 `no_baseline` / `no_baseline_metrics`，此时检查点第⑤门只能记 `unverified`，不能记 pass。

### 3 · 立卷纲

```bash
python3 -m kinema novel arc <pid> --no 1 --title "渊启" --from 1 --to 30 \
  --premise "开局局面" --goal "本卷要达成什么" --climax "怎么收卷" \
  --turn "第6章·主角用最差的画质拿到第一笔打赏·从无人问津变成有人盯着" \
  --turn "第17章·同伴暴露真实身份·合作关系翻转成互相拿捏"
python3 -m kinema novel arcs <pid>        # 复核：当前卷 + 覆盖断档/重叠
```

先只排第一卷，往后每卷开写前补一条（`--to` 可后补，重跑同 `--no` 即修改）。

**`--turn` 必须写成「第 N 章 · 谁做了什么 · 局面怎么变」三段式**——它是检查点第①门唯一的逐章对照物。
写成「主角变强」等于没写：没有章号就对不上账，没有「局面怎么变」就判不出这一章到底推没推进本卷。**没有登记过的大纲，
凭印象记着的那个写到第 30 章必然名存实亡**（跨会话之后它根本不在上下文里），而 `novel brief` 每章都会把当前卷纲原样递给你。

### 4 · 登记主要角色

```bash
python3 -m kinema character add <pid> --name 陆昭 --role 主角 \
  --appearance "外貌" --outfit "服装" --hair "发型" --weapon "断线" \
  --keyword 昭 --keyword "断线的主人"
python3 -m kinema character set <pid> --name 陆昭 \
  --speech-style "短句·反问·从不解释术语" --personality "压力下先算代价再动手" \
  --arc "逆向工程师 → 被迫上播 → 主动掀桌" \
  --add-taboo "绝不先动手" --add-taboo "从不在直播里说谎"
python3 -m kinema character show <pid> --name 陆昭    # 复核：打印文字设定卡
```

文字人设四件的验收判据（写不到这个程度＝没填，第②门会空转）：

| 字段 | 写到什么程度算过 |
|---|---|
| `speech_style` | 遮住名字读他三句台词，**能认出是谁** |
| `personality` | 据此能回答「他在压力下会怎么选」，而不只是一串形容词 |
| `arc` | 三段式「起点 → 当前阶段 → 终点」；剧情推进了就回来改这一条 |
| `taboo_lines` | **一条一梗、可判真假**（「绝不先动手」），不写「要保持神秘感」这种判不了的 |

- `--keyword` 是正文实体命中与缺席判定的兜底：本名不足 2 字、或常以绰号称呼的角色不补 keyword 就**永远命中不了**——`novel save` 的实体清单里没有他，
  lint 的缺席统计也看不见他。
- **NPC 出场即登记，别攒。** 攒着的直接后果是 `novel brief` 报「⚠ 角色表里没有 XXX」，而那时你已经写过他三章了。
  道具 `prop add <pid> --name … --desc … --keyword …`、场景 `scene add <pid> --name … --desc …` 同理。
- **只登记文字，不出图**：`sheet` 空着是正常状态（SKILL §5）。

### 5 · 世界观宪法 + 关系图谱

```bash
python3 -m kinema novel bible <pid> --file 宪法.md
python3 -m kinema novel bible <pid> --section "经济" --file 新经济节.md  # 只换一节（须唯一命中）
python3 -m kinema adapt graph <pid> --file 图谱.json
# 图谱 JSON：{"summary":"…","nodes":[{id,name,type}],"edges":[{source,target,relation,kind}]}
```

**宪法必须分节写。** 引擎按节切、按本章相关性挑几节喂进 `novel brief`（分节预算约 12000 字）；
不分节就只能整份回灌——一本长篇的宪法能长到七万字，每章回灌会直接把取料成本拖垮。

- 节标两种写法都认：行首 `【一·灵体】`，或 markdown `## 一 灵体`。一个都认不出时退化成单节（＝整份回灌）
  。复核：跑一次 `novel brief <pid>`，回执里宪法目录只有一行「（全文）」就是没切开。
- **第一节永远写「一句话」**，把第 0 步的定位与主线落在这儿。
- 标题里含 `一句话` / `叙事纪律` / `写法铁律` / `伏笔纪律` / `地名铁律` / `人设` / `禁` / `口径` 的节会被自动认成**常驻节**，
  不靠关键词命中、每章恒在场。硬规则（钱怎么算、能力的代价、地名口径、主角人设与地域律）必须放进这类标题下，
  否则写到某章时它会正好没被选中。

---

## ③ 续作与接盘导入清单

八步，顺序不能换（后面每一步都依赖前面的登记）：

| # | 做什么 | 命令 | 完成判据 |
|---|---|---|---|
| 1 | 逐章导入已有正文 | `novel save <pid> --no N --file chN.md --title "…"` | `novel lint` 的 `gap` 归零 |
| 2 | 只补最近 10~20 章的 digest/state | `novel digest <pid> --no N --text "…"` / `novel state <pid> --no N --file s.json` | 更早的**不必补**——成本高收益低，写下一章用不到 |
| 3 | 人工回填未收伏笔 | `novel thread-add <pid> --title "…" --setup <原章号> --tier short\|mid\|long` | 逐条读最近几章，凡「提了没兑现」的都记 |
| 4 | 立 baseline | `novel style <pid> --add-baseline <最近 3~5 章正文>` → `novel baseline <pid> --from N --to M` | lint 不再报 `no_baseline*` |
| 5 | 立当前卷纲 | `novel arc <pid> --no K --from A --to B --goal "…" --turn "…"` | `novel arcs <pid>` 无断档 |
| 6 | 标掉已退场角色 | `character set <pid> --name X --status departed`（或 `dead`） | lint 的 `char_absent` 只剩真该回来的人 |
| 7 | 验收 | `novel lint <pid>` | 见下两码 |
| 8 | 留痕 | `novel log <pid> --kind note --at M --text "接手导入完成，从第 M+1 章续写"` | 下次跨会话冷启动第一眼就看见 |

导入后第一次 lint，重点只看两个码：

- **`manuscript_drift`** ＝ 磁盘正文与登记块对不上（正文改了没重新登记，`entities`/字数/指纹全是陈旧快照）
  。修：`python3 -m kinema novel reindex <pid> --archive`（缺省全书重算 chars/sha256/entities，`--archive` 把当前磁盘稿另存一份进版本栈留档）
  。
- **`char_absent`** ＝ 角色长期未出场。长篇里永久退场是常态，**不标 `--status departed` 这条提醒会一直响**，
  既容易被误判成「功能没生效」，也会把真正该看的伏笔与复读条目淹掉。

**批次边界从导入后的下一个 10 章整数位起算。** 导到第 63 章就先补写 64~70 收口成一批、在第 70 章做检查点，
别从 64 一路数十章数到 73——对齐十位整数之后，`novel lint <pid> --from 61 --to 70`、`novel recap <pid> --from 61 --to 70` 与《批次报告》
文件名 `plan/batch-61-70.md` 才对得上号，跨会话接手时一眼就知道下一批是哪十章。

---

## ④ 多本书的边界

- **一本书一个 pid。** `novel` 登记块按章号单调编址——`manuscript/chNNNN.md`、`arcs[].from/to`、`threads[].setup/due` 全是裸章号，
  没有第二个维度。
- **绝不把第二本塞进同一个 pid 的 `arcs[]`**：章号一撞就是**不可逆事故**——第二本的 `novel save <pid> --no 12 --file …` 会把第一本的第 12 章正文移进版本栈并覆盖登记，
  两本书的伏笔账、缺席统计、数值基线从此全部串味。
- 共享世界观的正确做法是**复制**：新建 pid，宪法复制一份（`novel bible <pid2> --file 同一份宪法.md`）
  ，角色重新 `character add`。**复制不是引用**——两本书的设定会各自演进（同一个门派在续作里改了规矩、
  同一个角色在外传里还没黑化），共享引用迟早互相污染，而污染发生时**没有任何一处会报错**。
- 反向判据：两本书如果**永远不需要各自改同一条设定**，那它们其实是同一本书的两卷——用 `novel arc` 分卷即可，不必开新 pid。
