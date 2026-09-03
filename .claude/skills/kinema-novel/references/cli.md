# CLI 速查 · kinema-novel

**全部命令在仓库的 `engine/` 目录下跑，前缀 `python3 -m kinema`。**
下文为省版面统一省略前缀，实际敲的时候是：

```bash
cd engine
python3 -m kinema novel show <pid>          # ← 完整形态长这样
```

`<pid>` = 项目 id（位置参数，不是 `--project`）。源码检出中的工作区默认是仓库根
`project/`（从 `engine/` 启动也不会创建 `engine/project/`）；需要隔离时全部命令都吃
`--workspace <数据目录>`。

- [一、立契与立纲（一次性）](#一立契与立纲一次性)
- [二、每章五步](#二每章五步)
- [三、检查点](#三检查点)
- [四、设定实时更新](#四设定实时更新)
- [五、账本：伏笔与创作日志](#五账本伏笔与创作日志)
- [六、维护：检索·重登记·回滚·导出](#六维护检索重登记回滚导出)
- [列表字段的统一口径](#列表字段的统一口径)

---

## 一、立契与立纲（一次性）

```bash
novel init  <pid> [--pov … --tense … --voice … --diction … --avoid a,b]
                                          # manuscript/ 目录 + 文风契约骨架
novel style <pid> [--pov --tense --voice --diction]
                  [--add-baseline 文件或正文]   # 可多次；给路径就读文件
                  [--rm-baseline N]             # 删第 N 段（1 起）
                  [--add-avoid a,b] [--rm-avoid a,b]
novel baseline <pid> --from 1 --to 10     # 在认可的那批**整章**上算数值基线（μ±σ）
                                          # 至少 3 章；此后 lint 才有 z 分可对
novel bible <pid>                         # 不给内容 = 列出节目录与各节字数
novel bible <pid> --file 宪法.md           # 整份替换
novel bible <pid> --file 一节.md --section 六·经济     # 只换这一节（子串须唯一命中）
novel bible <pid> --text "…" --append     # 追加到末尾

novel arc  <pid> --no 1 --title "第一卷·夜行" --from 1 --to 30 \
                 [--premise … --goal … --climax …] [--turn "第7章…"]…  [--note …]
novel arcs <pid> [--json]                 # 派生进度态（已收卷/进行中/未开写）+ 覆盖体检
novel arc-rm <pid> --no 1
```

`--turn` 可给多次，**整体替换**（不是追加）。写成「第 N 章 · 谁做了什么 · 局面怎么变」
三段式——它是检查点第①门唯一的逐章对照物。

---

## 二、每章五步

```bash
# ① 取料
novel brief <pid> [--no N] [--chars 名,名 | --all]
                  [--bible 关键词,关键词 | --bible all | --no-bible] [--json]

# ② 写正文（落成 .md），埋伏笔见第五节

# ③ 三门自检（无命令，见 SKILL §3③）

# ④ 登记
novel save <pid> --no N --file 正文.md [--title …]
                 [--digest "…"] [--state 状态.json]
                 [--payoff minor|medium|major]
                 [--payoff-kind 打脸|升级|解谜|情感|反转]
                 [--hook 决定|发现|误判|代价|险境|逼近|错位]

# ⑤ 回写（--digest/--state 没在 save 里给就用这两条补）
novel digest <pid> --no N --text "本章事件 + 变化 + 尾钩，两三句"
novel state  <pid> --no N --file 状态.json
```

**`brief` 的宪法三态**：缺省 = 打全目录 + 自动选中相关几节（含常驻节）｜
`--bible 关键词` = 只要这几节（一字不截）｜`--bible all` = 全量｜`--no-bible` = 连目录也省
（同一会话连写多章、宪法已在上下文里时用）。

**`state` 只收五个键**，未知键引擎直接报错：
`time` / `location` / `characters`（`{名字: 一句话}`）/ `hooks`（数组）/ `note`。

---

## 三、检查点

```bash
novel recap <pid> --from 66 --to 75 [--json]
      # 逐章概要 markdown 表 + 伏笔动静 + 本批首次登场实体 + 缺项 + 文体量化 + 节奏账
novel lint  <pid> [--from 66 --to 75] [--level warn|all] [--json]
      # 确定性体检。--from/--to 只框**文体扫描窗口**（缺省最近 10 章）；
      # 断号/缺件/伏笔/缺席/卷覆盖这些账目性检查**恒看全书**
novel show  <pid> [--all] [--json]
      # 折叠总览（接手第 1 步）：进度/文风/最近日志/最近 10 章/伏笔/当前卷前后
```

`lint` 的四类**必修**（这批算不算写完的机械判据）：
`gap` · `digest_missing` · `state_missing` · `manuscript_drift`。
`thread_expired` 是**必处置**。分诊表见 `prose-rubric.md`。

---

## 四、设定实时更新

```bash
character add <pid> --name 孙缘 [--role 主角 --appearance … --outfit … --hair … --weapon …]
                                [--keyword 小疤]…          # 绰号，可多次
character set <pid> --name 孙缘 [--appearance … --outfit … --hair … --weapon … --role …]
                                [--speech-style "短句冷淡，从不解释第二遍"]
                                [--personality "谨慎，先算退路"]
                                [--arc "求生→求真"]
                                [--taboo … | --add-taboo "绝不先动手"]
                                [--keyword … | --add-keyword "小疤"]
                                [--status active|departed|dead]
                                [--silhouette … --constraint … --emotion … --action … --view …]
                                [--sync]
character show <pid> [--name 孙缘]        # 文字设定卡（省 token 的读法）
character list <pid>
character rm   <pid> --name 孙缘

prop  add <pid> --name 断剑 [--desc … --kind prop|weapon] [--keyword …]…
prop  set <pid> --name 断剑 [--desc … --kind …] [--keyword … | --add-keyword …] [--sync]
scene add <pid> --name 钟楼 [--desc …] [--keyword …]…
scene set <pid> --name 钟楼 [--desc …] [--keyword … | --add-keyword …] [--sync]
prop  list <pid>
prop  rm   <pid> --name 断剑
scene list <pid>
scene rm   <pid> --name 钟楼

adapt graph <pid> --file 图谱.json         # 关系图谱：**整份替换**，演变时重出快照
```

- **`--status` 不标就会一直报缺席**：角色永久退场/死亡时标 `departed`/`dead`，
  `novel lint` 的「连续缺席」只对 `active` 报。
- **`--keyword` 是防误报的关键项**：本名不足 2 字、或常以绰号称呼的角色，
  没有别名就永远命中不了实体统计。
- **`sheet` / `ref_image` / `audition` 不在白名单**（引擎会拒绝）——换图走版本栈那套。
- `--sync` 把设定推送到**已建的视频章节**（章节继承是创建时拷贝，不推送就看不见）。
  纯写小说阶段没有视频章节，不必加。

---

## 五、账本：伏笔与创作日志

```bash
novel thread-add  <pid> --title "徽章的来历" --setup 12
                        [--tier short|mid|long] [--due 40] [--note …]
novel thread-set  <pid> --id th03 [--title … --setup … --due … --tier … --note …]
novel thread-pay  <pid> --id th03 --in 73 [--note …]      # --in 是回收章号，必给
novel thread-drop <pid> --id th03 [--note …]

novel log <pid> --kind checkpoint|decision|overhaul|note --text "…" [--at N] [--ref 路径]
novel log <pid> [--kind …] [--limit 10] [--json]          # 不给 --text = 列出
```

- **`--tier` 按跨度推缺省 `--due`**：`short`=+30 章 · `mid`=+100 章 ·
  `long`=无期限但恒进「长期挂起」统计。**长线也必须显式声明**——
  旧判据下「不填 due」正好是让告警静音的那个动作，激励方向是反的。
- **超期恒为派生判定，绝不落盘**（存了会与最新章号脱钩）。
- `thread-set` 只改文本与期限；**改状态只能走 `thread-pay` / `thread-drop`**。
- `novel log` 是 **append-only**，同内容重记幂等。检查点做完必须记一条
  `--kind checkpoint --ref plan/batch-N-M.md`——跨会话接手读的就是它。

---

## 六、维护：检索·重登记·回滚·导出

```bash
novel sweep <pid> --term "被废掉的词" [--min-len 2] [--json]
      # 逐七层出命中数与出处：正文 / 设定卡 / digest / state / 卷纲 / 伏笔 / 宪法与图谱
novel normalize <pid> [--no N] [--dry-run]
      # 正文排版规范化：剥掉**非面板**的加粗（执行铁律「粗体只给面板」）。
      # 逐章走 save 故旧稿进版本栈、可 `novel revert` 逐章回滚；同内容重跑幂等。
      # **刻意不碰 `---`**：那条是断场还是节拍停顿要读上下文才判得出，只报数不动手
novel reindex  <pid> [--no N | 缺省全书] [--archive]
      # 按磁盘正文重算字数/指纹/实体命中回写登记块（手改过正文、或后补了 keywords 之后跑）
novel versions <pid> --no 52            # 该章版本谱系
novel revert   <pid> --no 52 [--v 3]    # 回滚到某版（当前稿先归档，可再滚回去）
novel export   <pid> [--from N --to M] [--strip-markup] [--out 路径]
      # 按**登记章序**合并（不是文件名字典序）；--strip-markup 出交稿用纯文本
```

`sweep` 与 `lint` 一样**零落盘**。改完正文一定要 `novel save` 或
`novel reindex --archive` 重登记，否则登记块的字数/指纹/实体命中还停在旧稿上
（`lint` 会报 `manuscript_drift`）。

---

## 列表字段的统一口径

**`--x` 整体替换（给几个就是几个）· `--add-x` 并集追加 · 两个都不给 = 不动这个字段。**
适用于 `--taboo/--add-taboo`、`--keyword/--add-keyword`、`--constraint/--add-constraint`；
`--turn`、`--emotion`、`--action`、`--view` 只有整体替换形态。

## 不存在的命令（别编）

`novel arc add` / `novel arc set` —— **`arc` 的第一个位置参数是 pid**，
正确写法是 `novel arc <pid> --no 1 …`（写错会得到「找不到项目: add」，
报错还会把人往「项目不存在」误导）。
`decision add --chapter <pid>/ch01` 在**纯小说项目**上不成立（它走的是视频章节加载，
小说项目没有 chapters/ 目录）——决策留痕用 `novel log --kind decision`。
