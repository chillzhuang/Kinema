# Claude Code 宿主目录说明

`.claude/` 是 Kinema 面向 Claude Code 的宿主目录。Claude Code 只从这个固定路径发现
Skill，Kinema 的 Agent 指挥层因此把它当作唯一实体：仓库里全部 Skill 的正文都住在
这里，其他编码宿主要么直接读取它，要么经由仓库根 `.agents/skills` 这条符号链接
指向它，任何地方都不保留第二份拷贝。

## 目录构成

- `skills/`：各个 Kinema Skill 的 `SKILL.md` 与 `references/`，直接在此编辑。名称、
  描述、类型、状态与权限等元数据只改 `agent/manifest.json`；`SKILL.md` 的 frontmatter
  和 `skill.json` 由 `python3 tools/agent_assets.py compile` 生成，其中的 digest 覆盖
  正文与 references，改了正文没有重新编译会被 `check` 判为漂移。
- `docs/`：本目录，用途见下一节。
- `settings.local.json` 等运行期文件：Claude Code 自己的本机状态，只属于这台机器。

仓库根的 `CLAUDE.md` 是 Claude Code 的入口指针，由 `agent/adapters/CLAUDE.md` 编译
而来，只负责把宿主引到 `AGENTS.md`。工程纪律只有 `AGENTS.md` 这一份，`.claude/` 里
不另写规则。

## 这个 docs 目录

这里存放与 Claude Code 协作期间在本机产生的工作文档：架构复审报告、全自动实测记录、
交接书、批次计划、问题清单一类。它们记录的是某次改动是怎么做的，服务于当下这一轮
工作，不是工程事实的正式载体。

工作文档只留在产生它的机器上，git 只追踪本说明。结论一旦稳定，就归入 `AGENTS.md`、
`docs/agents/` 或对应 Skill 的 references。正式文档引用这里的某个文件时，标注的是
决策来源，并不要求读者拥有同一份副本。

这里的内容不会自动进入 Agent 上下文，需要时把具体文件指给宿主即可。
