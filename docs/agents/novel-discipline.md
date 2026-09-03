# 原创小说层 novel 纪律

**实现真源** `kinema/novel.py` + `kinema-novel` SKILL

## 1. 十条纪律

### 1.1 契约块绝不裸改 JSON

涉及 `threads[]` / `arcs[]` / `novel` 块与 `characters[]` / `props[]` / `scenes[]`。

一律走 `novel` / `character set` / `prop set` / `scene set` 命令——写路径全经 `Series.commit()`
进程锁 ＋ 进锁后重载。

裸改的后果（**且不报任何错**，同 decisions 教训）：

- 被 Studio 并发长写或引擎长任务的旧内存副本整份覆写；
- mysql 模式下还会被较新的库行在 `Project.load` 之前盖掉。

**「伏笔超期」与「写到哪一卷」都是现算的派生判定**——往条目里写 `expired`/`state` 字段 = 与最新
章号脱钩的僵尸标记。

### 1.2 文风防漂靠基线比对

靠 `narrative_style.baseline`，**绝不逐章复述风格描述**——复述即漂移，这条提示词纪律在文本生成上同样成立。

baseline 为空时文风门空转，lint 会提示。

### 1.3 文字人设四件与 M8 五字段同流转

字段：`speech_style` / `personality` / `arc` / `taboo_lines` ＋ `keywords`。

- **进** `sync_design_to_chapters` 的 `char_fields`——改了要让存量章节看见，须 `--sync` 或重跑
  `project refs`；
- **绝不进** `upsert_entities`——重抽携带也不覆盖。

### 1.4 `novel save` 的实体命中清单是回写提醒，不是 NER

只统计**已登记**实体（≥2 字 / keywords 口径）。

正文里反复出现却不在清单里的名字 = 设定漏登记，指挥层必须当场 `character add` / `scene add` /
`prop add` 补。**别指望引擎认出新实体。**

### 1.5 文体量化只出数，不判 AI 味

`prose_slop` / `prose_repeat` / `prose_bands` 是可测量信号（口癖计数、复读原句、带区越界）；句长离散比只出数不设闸。
「这一处该不该改」永远是指挥层第 ④ 门的判断。

每条口癖必须带**物理化改写建议**（同 `variation.SLOP_TERMS`）——只说「这里有 AI 味」是句废话。

### 1.6 文体扫描必须有窗

缺省最近 10 章。百章级项目上逐章全扫是 O(全书)，而检查点要的本来就是本批次那一段。

账目类检查（断号 / 缺件 / 伏笔 / 缺席 / 卷覆盖）恒看全书。

### 1.7 markdown 记号不是作者写的字

正文是 `.md`，而 `**` 与 `---` 既违反 SKILL 自己的铁律「粗体只给面板」，又**污染引擎的指标**——
实测 350 章里 40,916 / 96,708 段整段加粗、段首雷同榜首被 `**"` ×21273 刷屏、章标题行被当段落。

故文体面一律先过 `strip_markup`。

> **而登记的 `chars` 与 `sha256` 绝不过它**——指纹是账目、剥离是度量，顺序反了 = 全书一次性判为
> 改稿并触发一轮版本归档。

### 1.8 抑制类指标必须配下限

把「仿佛 / 似乎 / 宛如」列进口癖禁令后，abyss 全书明喻密度中位 0.00——「一本没有任何比喻的长篇」
是另一种可测量的不自然。

`PROSE_BANDS` 因此**两侧都有闸**，且**绝不合成「AI 味总分」**——分数一落地必然被当 gate，然后为了
过 gate 去改文风。

同理**恒不触发的闸比没有闸更坏**：旧 `UNIFORM_SD_RATIO=0.5` 实测一次都没响过，真信号在「从不写
长句」而非「句子一样长」。

### 1.9 复核结论必须落盘

`novel log` 是跨会话唯一的载体。两条旧路径都是坏的：

- `decision add --chapter` 在纯小说项目上会报「找不到章节」——它走视频章节加载；
- `arc --note` 是单值字符串，第二次覆盖第一次。

流程：报告先落 `plan/batch-N-M.md`，再 `novel log --kind checkpoint --ref`。

### 1.10 `set_named_scene` 刻意不叫 `set_scene`

后者已被「全局固定场景文本 `data["scene"]`」占用，与具名取景地 `scenes[]` 是两个概念。同名会
**静默覆盖**掉先定义的那个。

## 2. 命令族逐命令详解

### 2.1 `novel save` · 正文登记

```bash
python3 -m kinema novel save x --no 1 --file 正文.md [--title …] [--digest …] [--state s.json] [--payoff …]
```

正文登记（幂等 · 旧版归档 `manuscript/versions/` · 字数指纹 · 实体命中统计）＋ 收尾打「本章必做 /
实体回写」提醒，每满 10 章打 ★ 检查点（七门复核）。

同组：`init` / `digest` / `state` / `thread-add|-set|-pay|-drop`（伏笔账本，`--tier short|mid|long`
推缺省 due，超期恒派生不落盘）/ `show`（缺省折叠，接手第 1 步）。

### 2.2 `novel brief` · 写前必读包

```bash
python3 -m kinema novel brief x [--no N] [--chars 名,名] [--bible 关键词|all]
```

一次取齐：文风契约（含 baseline）＋ 当前卷纲**与节拍** ＋ **按本章相关性选出的宪法节** ＋ 上章
digest 与 state ＋ 未回收伏笔（到期升序）＋ 上一章在场角色人设卡 ＋ 分段取料账。

这是长篇的上下文生命线。宪法按 `【…】`/`##` 无损切节后确定性打分 ＋ 常驻节 ＋ 预算，实测
195KB → 50KB。**别 `Read` 整份 project.json。**

### 2.3 `novel recap` · 批次复核物料

```bash
python3 -m kinema novel recap x [--from 66 --to 75]
```

逐章概要 markdown 表（**逐项数不许估**，直接进《批次报告》）＋ 伏笔动静 ＋ 本批首次登场实体 ＋
缺项 ＋ 文体量化（带区 / markdown 污染 / 节奏账）。

`novel lint --from/--to` 同窗口；`--level warn` 只看待办。

### 2.4 `novel arc` · 卷/幕规划

```bash
python3 -m kinema novel arc x --no 1 --title … --from 1 --to 30 [--goal --climax --turn …]
```

长篇的**大纲落点**，是检查点第一门「有没有跑偏大纲」的对照物。

`novel arcs` 看派生进度态（done / writing / planned）与覆盖体检（断档 / 重叠）。**进度恒派生不
落盘**，同伏笔超期。

### 2.5 `novel normalize` · 正文排版规范化

```bash
python3 -m kinema novel normalize x [--dry-run]
```

剥掉**非面板**的加粗，执行铁律「粗体只给面板」——面板按 `【】` 覆盖率 ≥0.6 判，引用块系统播报
整行放过。

逐章走 `save`，故旧稿进版本栈可 `novel revert` 回滚，同内容重跑幂等。

**刻意不碰 `---`**——那条是断场还是节拍停顿，要读上下文才判得出，引擎只报数。

### 2.6 `novel sweep` · 改设定的收工判据

```bash
python3 -m kinema novel sweep x --term "被废掉的词"
```

逐层出命中数与出处，共七层：正文 / 设定卡 / digest / state / 卷纲 / 伏笔 / 宪法与图谱。

**三、四、七层最容易漏**——它们不出现在正文里，而 `brief`/`recap`/检查点恰恰从它们取料。

同组：`reindex`（按磁盘重算字数指纹实体，lint 的 `manuscript_drift` 提示它）/ `revert` + `versions`
（章级回滚）/ `export`（按登记章序合并，`--strip-markup` 出交稿纯文本）。

### 2.7 `novel log` · 跨会话唯一载体

```bash
python3 -m kinema novel log x --kind checkpoint --at 75 --ref plan/batch-66-75.md --text "…"
```

append-only，记录「上次是怎么判的」。

同组：`style`（文风契约唯一写路径，增删 baseline 与 avoid）/ `bible`（宪法整份 | 按节 | 追加）/
`baseline`（在认可的整章上算 μ±σ 数值基线，此后 lint 报 z 分）。
