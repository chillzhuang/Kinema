# 设定图版本栈

**设计** 与分镜版本谱系对称

## 1. 核心规则

**重生成 / 改造 / 回滚前，旧图移入 `assets/refs/versions/` 归档，v 升序。**

## 2. 存放键位

| 对象 | 版本键 | 归属 |
|---|---|---|
| 角色 | `characters[].versions` | 实体 dict |
| 道具 | `props[].versions` | 实体 dict |
| 场景 | `scene_ref_versions` | 顶层 |

角色/道具与场景**互不串**。条目结构统一为 `{v, file, at, reason?, params?}`（`params` 目前只承载角色档的 `sheet_origin`）。

### 2.1 扩展设定图与场景俯视图

各配独立键、同一套归档语义：`expression_versions` / `pose_versions`（角色实体）、
`topview_versions`（`scenes[]` 实体）/ 顶层 `scene_topview_versions`。
`_asset_version_ctx` 已接线。

一个场景两张图、**两条版本栈**：基准图走 `versions` / `scene_ref_versions`，俯视图走
`topview_versions` / `scene_topview_versions`。合成一条的话，回滚基准图会把图纸一起带回旧版
（反之亦然），而两张图本就可以各自重出。意见池同理分家（`comments` vs `topview_comments`）。

> **这几类重生 / 改造不触发血缘传播**——表情表与动作表不进每镜挂载，无下游可作废；
> 俯视图只进**视频**请求（每镜至多一张，判据在 `lineage.primary_layout_ref`）、不进分镜图，
> 传播会把全章分镜无辜置 retake（那是花钱重出）。

## 3. 归档与回滚的语义

**归档** = **移动**旧图（磁盘零冗余）。标准字段（`sheet` / `scene_ref`）**路径字符串不变**——调用方
随后写回同路径。

**回滚** = 当前版先归档（`reason=rollback-out`；角色档把出库版的 `sheet_origin` 记进条目 `params`）→ 把某历史版**拷回**标准路径，
角色档同时换回该版条目记录的 `sheet_origin`（条目没记则摘键、视为未知）→ 血缘传播，下游分镜标过期。

## 4. 触发点

| 触发 | 场景 |
|---|---|
| `cmd_gen_refs --force` | 直出档 ncand ≤ 1 |
| `refine_asset` | 局部改造 |
| `pick_asset_candidate` | 候选换选 |
| `supply_asset_sheet` | 素材直供替换已有图（`reason=素材直供替换`） |
| `rollback_asset_sheet` | 回滚出库（`reason=rollback-out`） |

单一真源：`versioning.archive_asset` / `rollback_asset`，经 `refine.archive_asset_sheet` /
`rollback_asset_sheet` 包装（含 `_asset_version_ctx` 定位）。

直出定稿同时清掉候选三件（`sheet_candidates` / `sheet_candidates_origin` /
`sheet_picked`）：残留候选表会让「已有设定图/候选就跳过」的判据恒短路，且此后
`pick` 会用上一批的来源记录覆盖 `sheet_origin`。

写 `sheet` 的每条路径（直出 / pick / refine / supply / rollback）末尾都经
`refine._propagate` 对齐章节副本：字段白名单复用 `sync_design_to_chapters`
（角色/道具/具名场景/两张全局场景图/表情/动作同一份），`sheet_origin` 的空值
单独补齐（该字段以缺失表达「来源无记录」）。

## 5. 网页入口

设定图卡右上「vN」角标 ＋ 灯箱「⌛ 版本谱系」→ `openAssetVPanel`：自取项目数据渲染历次归档 ＋
一键回滚，主页 / 章节页通吃。

回滚走 `/api/rollback`——带 `asset_kind` 走 `actions.rollback_asset_version`，否则按分镜走
`rollback_version`。
