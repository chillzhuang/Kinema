# 决策审计 decisions[]

**位置** 章节文档顶层，append-only ｜ **用途** 把制作中的取舍逐条留痕，后续会话不再反复推翻

## 1. 唯一写入路径

```bash
python3 -m kinema decision add --chapter x/ch01 --choice "…" [--alt …] [--why …] [--confidence high|medium|low]
python3 -m kinema decision list [--json]
```

**必须走 `decision add`，绝不裸改 JSON。**

## 2. 为什么禁止裸改：两条静默吞没路径

1. 引擎长任务持有旧内存副本，逐镜 save 时整份覆写；
2. mysql 模式下库行较新会直接覆写本地文件——这发生在 `Project.load` 之前就没了，`_DOC_HUMAN_KEYS` 救不到。

## 3. 合并规则

已登记进 `_DOC_HUMAN_KEYS` + `_DOC_APPEND_KEYS`，合并规则是**按 id 取并集**（非整键替换），
两侧同时追加都不丢。

### 3.1 缺 id 的回退与自愈

去重键缺 id 时回退内容派生键 `sha256:<hex16>` 并就地补进条目（`decisions.entry_key`/`derived_id`，
首次 save 自愈）。

裸改写出的无 id 条目若没这层回退，会在 union 的内存/磁盘两侧各留一份，**每 save 翻倍**——实测
8 镜一趟 `run` 后 1,048,576 条 / 109 MB，Studio 的 `JSON.parse` 直接崩，且 append-only 取并集
使其无法经引擎收回。

## 4. 修订方式

只增不改不删。记错了再记一条覆盖性的。
