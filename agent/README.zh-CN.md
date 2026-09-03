# Kinema Agent 控制平面

[English](README.md) · [简体中文](README.zh-CN.md)

本目录承载 Kinema Agent/Skill 控制平面的**元数据与契约**。Skill 正文只有一份实体——
发现目录 `.claude/skills/`——直接在那里编辑（单源无拷贝）。本目录负责告诉编译器和
引擎这些 skill「是什么」：注册表、权限、路由、机器契约与宿主入口指针。

```text
agent/manifest.json + agent/contracts.json + agent/adapters/ + .claude/skills/（正文）
                         │
                         ▼
              python3 tools/agent_assets.py compile
                         │
          ┌──────────────┼──────────────────┐
          ▼              ▼                  ▼
 .claude/skills/    运行时 catalog       宿主 adapter
 frontmatter +      + contracts          + docs/skills/INDEX.md
 skill.json         （引擎侧）
```

## 这里放什么（可编辑源码）

| 路径 | 角色 |
|---|---|
| `manifest.json` | **全部 skill 的清单与路径**，外加治理元数据：名称、描述、类型、状态、依赖、抽象权限、预算与输出映射。每条的 `source` 指向 `.claude/skills/<id>` |
| `manifest.schema.json` | 公共结构说明；零依赖编译器执行同等且更严格的跨字段语义校验 |
| `contracts.json` | Agent Gateway 下发的机器契约（PromptSpec / ChapterPlan） |
| `adapters/` | 宿主专属入口指针（Claude Code / Cursor / Copilot）；只做指针，不承载规则内容 |

Skill 正文（`SKILL.md` 与 `references/`）**不在**这里——直接改 `.claude/skills/<id>/`。

## 机器维护的产物——不可手改

以下内容由编译器从 `manifest.json`、`contracts.json` 与 skill 正文派生；手改会被
下一次 `compile` 覆盖，并被 `check` 判为漂移拒绝。

| 生成物 | 是什么 | 谁消费 |
|---|---|---|
| `.claude/skills/<id>/SKILL.md` 的 **frontmatter** | name/description/kind/status + `kinema-owner/source/trust/digest`，全部由 manifest 派生；digest 覆盖正文与 `references/`，改了没编译会被抓 | 所有采标宿主 |
| `.claude/skills/<id>/skill.json` | manifest 条目 + digest 的逐 skill 投影 | 外部工具 |
| `engine/kinema/_generated/agent_catalog.json` | 运行时 Skill/profile catalog | `engine/kinema/skills.py` · `agent_system.py`（路由与 Studio SKILL 大屏） |
| `engine/kinema/_generated/agent_contracts.json` | 编译后的机器契约 | `kinema agent contract` 网关 |
| `.claude/skills/kinema/references/prompt-contract.md` | `contracts.json` 的人读渲染 | Skill 作者 |
| `CLAUDE.md`（仓库根） | 宿主指针，源自 `adapters/CLAUDE.md` | Claude Code |
| `.cursor/rules/kinema.mdc` | 宿主指针，源自 `adapters/cursor.mdc` | Cursor |
| `.github/copilot-instructions.md` | 宿主指针，源自 `adapters/copilot-instructions.md` | GitHub Copilot |
| `docs/skills/INDEX.md` | 工具中立的人读 Skill 索引 | 未采标工具 |

`.agents/skills` 是指向 `.claude/skills` 的 symlink 别名，供 Codex / Gemini CLI /
Amp / OpenCode（agentskills.io 开放标准）发现；Windows 修复：
`python tools/agents_alias.py`。

## 编辑协议

1. Skill 正文与 `references/`：直接改 `.claude/skills/<id>/`。
2. Skill 的名称、描述、类型、状态、profile、依赖、权限，以及增删 skill：
   只改 `manifest.json`；全部 frontmatter 与 `skill.json` 由编译器生成。
3. 宿主差异只改 `agent/adapters/`；工程纪律只在根 `AGENTS.md`；模块详情文档在
   `docs/agents/`。
4. 无论改了哪一侧，之后运行：

   ```bash
   python3 tools/agent_assets.py compile
   python3 tools/agent_assets.py check
   cd engine && python3 -m unittest discover -s tests
   ```

## 版本策略

`manifest.json` 的 `catalog_version` 是产品版本决策，当前锁定 `2.0.0`：
正式发布前不因架构演进或 Skill 内容变化而升版；升版仅由产品负责人显式决定，
并在提交里单独说明理由。全部生成物（frontmatter、`agent_catalog.json`）的版本号
由它派生，不得在生成物内单独改。

## 架构边界

- Agent 负责检索、创作判断、分镜与人工节点协作；引擎负责确定性目录、路由、
  校验和媒体执行，不内置 LLM provider。
- 路由优先级固定为：项目绑定 > 显式 Skill > 显式 profile > `kinema`。
- 公共 manifest 只声明抽象最小权限，不写 Claude/Codex/Cursor 的宿主专属权限字段。
- `stable` 只接收本仓 first-party source；混入发现目录的未登记包、路径逃逸和
  symlink 在编译期直接拒绝。
