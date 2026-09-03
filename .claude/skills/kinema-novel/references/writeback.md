# 回写与改设定

每章第⑤步的清单在 §1，推翻一条老设定的收工判据在 §3。全部命令在 `engine/` 下跑：

```bash
cd engine   # 以下命令一律 python3 -m kinema …
```

- [§1 回写映射表](#1-回写映射表)
- [§2 三条纪律](#2-三条纪律)
- [§3 改设定＝改七层（收工判据）](#3-改设定改七层收工判据)
- [§4 改大方向四步](#4-改大方向四步)

---

## 1 · 回写映射表

**判据**：读一遍刚写完的正文，凡是「下一章的我需要知道」或「这条与设定卡上写的不一样了」，
就在下表找一行执行。**回写是独立步骤**，不许并进写正文那一步顺手做——顺手做的必然漏。

| 正文里发生了什么 | 立刻回写哪里 | 命令 |
|---|---|---|
| 本章事件与尾钩 | `novel.chapters[].digest` | `novel digest <pid> --no N --text "两三句：谁做了什么·代价·结尾停在哪"` |
| 章末谁在哪·什么状态·悬念栈 | `novel.chapters[].state` | `novel state <pid> --no N --file s.json`（五键写法见 SKILL §3） |
| 新角色/NPC 登场 | `characters[]` | `character add <pid> --name 名 --role 定位 --appearance … --keyword 绰号`<br>**必带 `--keyword`**：实体命中按 ≥2 字/别名走，本名短或正文常用绰号称呼的人不补别名**永远命不中**，随后会被 lint 的 `char_absent` 误报成「很久没出场」 |
| 新角色的口吻/性格/弧光定下来了 | `speech_style` / `personality` / `arc` | `character set <pid> --name 名 --speech-style … --personality … --arc "起点→当前→终点"`（`character add` 不收这四件，建卡后必须补——不补则人设门盲测是空转） |
| 角色转变·破戒·立新禁忌 | `arc` / `taboo_lines` | `character set <pid> --name 名 --arc "…→当前阶段→…" --add-taboo "绝不先动手"` |
| 换装·受伤留疤·换武器·变发型 | `appearance` / `outfit` / `hair` / `weapon` | `character set <pid> --name 名 --appearance "左颧新添一道旧疤" --outfit … --weapon …` |
| 有了新绰号/尊称/代号 | `keywords` | `character set <pid> --name 名 --add-keyword "哨长,老陆"` |
| 角色永久退场或死亡 | `status` | `character set <pid> --name 名 --status departed`（或 `dead`）——不标的话 `char_absent` 缺席提醒会一直报，恒报即等于不报 |
| 新道具登场·易主·损毁·改名 | `props[]` | `prop add <pid> --name 名 --desc … --keyword 别名` / `prop set <pid> --name 名 --desc "已断为两截" --add-keyword …` |
| 新地点·老地点变样（被烧/改建/易主） | `scenes[]` | `scene add <pid> --name 名 --desc …` / `scene set <pid> --name 名 --desc … --add-keyword …` |
| 关系变化：结盟·反目·身份揭穿·血缘揭示 | `graph` | `adapt graph <pid> --file graph.json`——**整份快照重出**（nodes+edges 一次替换），不是增量补丁；连贯门核关系口径读的就是它 |
| 世界观出了新规则或新代价 | `adaptation.world_bible` | `novel bible <pid> --section "代价" --text "…"`（子串须唯一命中该节）；全新一节用 `novel bible <pid> --append --text "## 灵体制\n…"` |
| 卷的起止/目标/高潮/节拍调整了 | `arcs[]` | `novel arc <pid> --no K --to M --goal … --climax … --turn "…" --turn "…"`（`--turn` 是整体替换，要给全） |
| 埋了新伏笔 / 收了 / 作废 / 标题写歪了 | `threads[]` | `novel thread-add <pid> --title … --setup N --tier short\|mid\|long` · `novel thread-pay <pid> --id thNN --in N` · `novel thread-drop <pid> --id thNN --note 理由` · `novel thread-set <pid> --id thNN --title "新标题" --due M` |
| 文风演进（**须用户同意**，不是自己漂了就改契约） | `narrative_style` | `novel style <pid> --voice … --add-avoid "…" [--add-baseline 新样本.md]`，改完 `novel baseline <pid> --from A --to B` 重立数值基线——否则 lint 的 z 分还在拿旧基线判新文风 |

---

## 2 · 三条纪律

**① 绝不裸改 `project.json`——上表每一行都有命令。**
所有写路径都进 `Series.commit()` 进程锁，**并且进锁后重新加载文档**。裸改（Edit/Write 那份
JSON）有两条静默吞没路径：引擎某个长任务手里的旧内存副本随后整份写回、mysql 模式下较新的
库行在 `Project.load` 之前就把本地文件盖掉。两条都**不报任何错**——你以为改完了，下一章
`novel brief` 取回来的还是旧值，而你不会知道。同理，「伏笔超期」「卷进度」是现算的派生
判定，往条目里手写 `expired`/`state` 只会得到一个与最新章号脱钩的僵尸标记。

**② 设定图与音色字段不从这里改。**
`sheet` / `ref_image` / `audition` / `custom_audition` 不在 `character set`/`prop set`/`scene set`
的白名单里，给了直接报错。它们是引擎回填字段，换图走版本栈那套（写作期本来就一张图都不出）。
你在这份文档里能改的只有**文字**。

**③ `characters[]` 的 M8 五字段是系列级常量，不是本章的。**
`required_emotions` / `required_actions` / `required_views` / `silhouette_notes` /
`constraints`（`character set` 的 `--emotion/--action/--view/--silhouette/--constraint`）
写的是**这个角色全系列要演到的东西**——按本章填，下次同步就被冲掉，而且会污染将来的设定图
提示词。写小说阶段基本用不到它们，真要填就按全书口径填一次。
改完若已有视频章节，加 `--sync` 才推送到存量章节（存量章节持有的是创建时的拷贝）。

---

## 3 · 改设定＝改七层（收工判据）

一条设定被推翻——改口径、废机制、删角色、换人设——**必须逐层扫到底**，一层不漏：

| 层 | 是什么 | 为什么会漏 |
|---|---|---|
| ① 正文 `manuscript/` | 已写章节里的原句 | 唯一看得见的一层，通常只改了这层就以为完事 |
| ② 设定卡 `characters[]`/`props[]`/`scenes[]` | 角色/道具/场景的文字字段 | 改了正文忘了改卡，下一章 brief 又把旧卡喂回来 |
| ③ `novel.chapters[].digest` | 逐章精简大纲 | **不出现在正文里** |
| ④ `novel.chapters[].state` | 章末状态快照 | **不出现在正文里** |
| ⑤ `arcs[]` 卷纲 | 含 `goal`/`climax`/`turns[]` | 节拍里常写死已被推翻的机制名 |
| ⑥ `threads[]` 伏笔账本 | 伏笔标题与备注 | 标题里的旧词会一路带到检查点第⑥门 |
| ⑦ 宪法与全局 | `world_bible`/`mainline`/`logline`/`narrative_style`/`graph` | **不出现在正文里**，却是每章 brief 的取料源 |

**③④⑦ 是重灾区**：它们不在正文里，人肉复读发现不了，而 `brief`/`recap`/检查点恰恰从它们
取料——于是出现最难查的一类事故：**正文早改完了，AI 下一章又照着旧设定写**。

**这一步有命令，别靠人肉找**：

```bash
python3 -m kinema novel sweep <pid> --term "被废掉的词"      # 逐层出命中数与出处
python3 -m kinema novel sweep <pid> --term "旧机制" --min-len 2 --json
```

**收工判据**：`sweep` 七层命中归零；或每一条留下来的都能**显式说出「为什么可以留」**
（例：第 12 章那句是角色当时的误认，正是后文要打脸的点）。说不出来的一律改掉。
处置完记一条痕：

```bash
python3 -m kinema novel log <pid> --kind overhaul --at N --text "废除 X 机制，七层扫描 42→0 处"
```

**改完正文必须重登记**——`novel save`（改动大、要留旧稿）或 `novel reindex <pid> --archive`
（批量改了多章）。不重登记的话登记块的字数/sha256/实体命中还停在旧稿上，`novel lint` 会报
`manuscript_drift`，而 `recap` 与检查点会拿着旧快照给你算账。

---

## 4 · 改大方向四步

用户中途说「主角性格我不喜欢」「这条线我不要了」「换个走向」时，走这四步，顺序不换：

**1 · 立即停批。** 当前批次就地打住，别把按旧方向写的章节继续往下摊——每多写一章，
第 2 步的数字就大一截。

**2 · 出影响面清单，用命令数出来，不许估。**

```bash
python3 -m kinema novel sweep <pid> --term "要废的设定词"      # 七层各命中几处、在哪
python3 -m kinema novel recap <pid> --from A --to B            # 逐章概要+字数，圈出要重写的章
python3 -m kinema novel arcs <pid>                             # 哪几卷的 goal/turns 失效
```

清单必须落到四个数：**要重写哪几章（章号）· 哪些伏笔作废（thNN）· 哪些卷纲失效（卷号）·
涉及多少字、预计几个批次**。

**3 · 两条路明码标价，交用户拍板。**

| 方案 | 做什么 | 代价 |
|---|---|---|
| **就地改** | 只改人设卡/宪法 + 从下一章起按新口径写，已写的不动 | 便宜；但前 N 章与后文口径不一致，读者能看出来 |
| **回溯改** | 重写已写的 N 章 + 七层同步 | = 第 2 步数出来的那个数（章数/字数/批次数） |

**不给代价就让用户批准一次不可逆改动，是这一步最容易犯的错。** 报了价再让他选。

**4 · 执行并留痕。** 按 §3 七层扫到底 → `novel reindex <pid> --archive` 重登记 →
出《手术报告》落 `project/<pid>/plan/overhaul-<批次>.md`（写清改了什么、扫了哪七层、
sweep 前后命中数、哪些章重写过）→ 记痕：

```bash
python3 -m kinema novel log <pid> --kind overhaul --at N --ref plan/overhaul-7.md --text "主角性格回溯改，重写 61-70"
```

**回改旧章三件套**（改任何一章已写的正文都走这三下，缺一不可）：
① 改正文 → ② 同步**该章**的 `digest`/`state`（旧的必然与新正文对不上）→
③ 连读**下一章开头**验衔接（上一章 state 一变，下一章的第一段常常就穿帮）→ `novel save` 重登记。

章级回滚的入口在这两条——改坏了或改了一半想退回来：

```bash
python3 -m kinema novel versions <pid> --no 66        # 看这一章有哪几版存档
python3 -m kinema novel revert <pid> --no 66 --v 2    # 回滚（当前稿先归档再拷回，缺省回最近一版）
```
