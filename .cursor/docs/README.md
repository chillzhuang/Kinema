# Cursor 宿主目录说明

`.cursor/` 是 Kinema 面向 Cursor 的宿主目录。Kinema 的工程纪律只有仓库根 `AGENTS.md`
这一份，各宿主目录里只放把宿主引到那里的入口指针，以及宿主自己的本机状态，不承载
第二套规则。

## 目录构成

- `rules/kinema.mdc`：Cursor 的入口规则，`alwaysApply` 常驻，内容只有一件事：开工前
  完整读取 `AGENTS.md`。它由 `agent/adapters/cursor.mdc` 经
  `python3 tools/agent_assets.py compile` 生成；要调整宿主差异请改源文件，手改这份会在
  下一次编译时被覆盖，并被 `check` 判为漂移。
- `docs/`：本目录，用途见下一节。

Skill 实体只有一份，在 `.claude/skills/`。Cursor 官方兼容该路径并直接读取，`.cursor/`
不复制任何 Skill 正文。

## 这个 docs 目录

这里存放与 Cursor 协作期间在本机产生的工作文档：架构复审报告、全自动实测记录、
交接书、批次计划、问题清单一类。它们记录的是某次改动是怎么做的，服务于当下这一轮
工作，不是工程事实的正式载体。

工作文档只留在产生它的机器上，git 只追踪本说明。结论一旦稳定，就归入 `AGENTS.md`、
`docs/agents/` 或对应 Skill 的 references。正式文档引用这里的某个文件时，标注的是
决策来源，并不要求读者拥有同一份副本。

这里的内容不会自动进入 Agent 上下文，需要时把具体文件指给宿主即可。
