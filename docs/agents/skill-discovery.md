# Skill 发现路径双轨

## 1. 实体只有一份

skill 实体永远在 `.claude/skills/<名>/SKILL.md`——这是 Claude Code 的硬性发现路径，**根目录禁
`skills/`**。仓库根 `.agents/skills` 是指向它的 **symlink 别名**。

**两边都不放第二份实体**：实体目录顶替链接 = 两份真源各自漂移。

单源布局：正文与 references 就在该目录原地编辑；name/description 等元数据在
`agent/manifest.json`，frontmatter 与 `skill.json` 由编译器维护（`kinema-digest` 覆盖
正文，改了没重新 compile 会被 `check` 判红）。不设 `agent/skills/` 正文拷贝：
两树逐字节几乎相同的双树布局只剩漂移面，单源不得复活为双树。

## 1.1 单源收敛时的三项评估存档

- **frontmatter 机器回写方向（与生态「frontmatter 即手写源」相反）——已评估，不修**。
  零依赖编译器对 frontmatter 只写不解析；翻转方向就得零依赖逐份解析手写 YAML，
  解析器本身即脆弱面。坑自带告示牌（`kinema-managed-by` 首行可见 + check 秒红）。
- **status 枚举不设 `quarantine` 档——已评估，不设**。单源布局下正文就在发现目录，
  宿主必然加载，「登记但隔离」不可兑现（双树布局靠「不拷贝」才隔离得住）。零实例
  故不设该档（`STATUSES`/schema/编译器分支均无），将来真需要隔离再按单源布局
  设计能兑现的语义。
- **根目录不设 `skills/`（人读 INDEX 与 `.claude/skills/` 名近会混淆）**。
  INDEX 在 `docs/skills/INDEX.md`（与 `docs/agents/`、`docs/kinema/` 同级分类）；
  没有任何工具从根目录做发现，纯人读索引归 docs 更名副其实。

## 2. 各工具的发现方式

| 工具 | 发现路径 |
|---|---|
| Claude Code | `.claude/skills/`（硬性路径） |
| Codex / Gemini CLI / Amp / OpenCode | 仓库根 `.agents/skills` symlink，按 agentskills.io 开放标准收敛到该路径 |
| Cursor / GitHub Copilot | 官方兼容，直读 `.claude/skills/` |
| Windsurf / Aider / Zed 等未采标工具 | `docs/skills/INDEX.md` 人工索引 |

## 3. Windows 开发者

git 未开 `core.symlinks` 的检出会把链接落地成文本残根。

**影响**：无任何破坏——Claude Code / Cursor / Copilot 照常，仅 Codex/Gemini 一族在该机发现降级。

**修复**：在仓库根跑一次 `python tools/agents_alias.py`，即换成**免管理员的 NTFS junction**
（不需要开发者模式），并自动 `git skip-worktree` 保持工作区干净。

想用真 symlink：开发者模式 + `git config core.symlinks true` 后重检出。

## 4. frontmatter 硬约束

| 字段 | 约束 |
|---|---|
| `name` | 小写连字符 ≤64，且与目录同名 |
| `description` | **≤1024 字符** |

超限**不报错**——严格实现（skills-ref / Gemini CLI）只是静默跳过该 skill。

## 4.1 退役一个 Skill

从 manifest 与发现目录删掉它、连同 `config/models.yaml` 里它独占的 profile
（编译器强制两者集合恒等），再重新 compile。**存量项目不会自动跟着走**：
`project.json` / 章节文档里的 `skill`、`profile` 是落盘的历史事实，退役后
`agent route --project` 与 `agent context --chapter` 会硬失败——这是刻意的，
绑定降级等于让项目在另一套画风 DNA 下继续产出。两个入口都走
`AgentCatalog.bound_skill`/`bound_profile`，错误里带换绑命令：

```bash
python3 -m kinema project set <项目id> --skill <新 id> --profile <新画风>
```

章节文档在建章时**拷贝**了一份 `skill`/`profile`，`project set` 不回灌已有章节；
这两个字段也不在 ChapterPlan 的 author-owned 白名单里（画风是项目级单点真源，
章节副本只为可复现）。存量章节走 `chapter set`：

```bash
python3 -m kinema chapter set <项目id> <章节id> --skill <新 id> --profile <新画风>
python3 -m kinema chapter set <项目id> <章节id> --inherit   # 删掉 skill/profile/video_provider 三键，回落项目派生
```

它只开 skill / profile / video_provider 三个绑定与 budget / budget_per_call 两项额度：章节其余作者字段的唯一入口仍是 ChapterPlan
（`agent context` → `plan validate` → `plan apply`），在 CLI 复刻第二份可写面
就是两条写路径各改一半。

工作区不止一个（`KINEMA_WORKSPACE` / 其他检出 / MySQL 后端的库行）时，
退役前的存量扫描要逐个工作区做，只查当前 `project/` 不足以下结论。

## 5. 守卫

由 `test_delivery` 统一强制：

- 链接指向（Windows 残根只 skip 并报修复命令；指错方向 / 实体顶替才红灯）；
- 修复工具三契约（残根替换 / 实体拒删 / 幂等）；
- 根目录禁区与规范符合性。
