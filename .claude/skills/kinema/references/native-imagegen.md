# 原生生图能力握手

本协议用于在执行任何生图前确定「本次任务由第三方图像 API 还是当前 Agent 的原生图像工具出图」。
它只切换生图路由，不等于用户已经授权开始生成图片。

## 判断顺序

按以下优先级处理，命中后停止继续猜测：

1. 用户或机器配置已经显式激活 image provider：尊重该选择，不自动覆盖。
2. `KINEMA_AGENT_IMAGEGEN=1` 已存在：当前任务使用 `agent` 路由。
3. 当前会话实际暴露了可调用的原生图像生成工具：可自动把本次 Kinema 命令声明为
   `KINEMA_AGENT_IMAGEGEN=1`。
4. 无法确认：向用户询问一次，不要把 Codex 安装状态或模型名称当作能力证明。

`agent doctor` 里的 `host_runtime` 只说明机器上是否安装 `codex`、`claude` 等命令，不能证明当前
会话由哪个宿主驱动，也不能证明当前会话有 ImageGen。模型名称同样不能替代实际工具能力检查。

## 能力询问

能力未知时使用这句短问，不附加多余配置问题：

> 当前会话是否可以直接调用原生 ImageGen 生图？回答“有”我将使用当前 Agent 生图，不需要
> `ARK_API_KEY`；回答“没有”则使用第三方生图 API。这个确认只切换路由，不会自动开始出图。

- 用户回答“有”：本次任务后续所有 Kinema 引擎命令都带 `KINEMA_AGENT_IMAGEGEN=1`。
- 用户回答“没有”：保持默认或用户已选的第三方 provider；只有在用户授权出图时才检查对应密钥。
- 用户回答“用第三方”或已经显式选了 provider：直接尊重该选择，即使当前会话有原生工具。
- 用户回答不确定：保持未知并说明需要用户确认，不要循环追问。

声明只对当前任务有效，默认不要写入 `models.local.json`、`project.json` 或 Skill。只有用户明确要求
“以后这台机器都用 Agent 生图”时，才使用 `config activate --capability image --provider agent`，
并提醒这会影响后续非 Codex/无原生图像工具的会话。

## 路由后的行为

使用 Agent 路由时：

```bash
KINEMA_STORAGE_BACKEND=local KINEMA_AGENT_IMAGEGEN=1 \
  python3 -m kinema gen-image --chapter <项目>/<章节> --only 1
```

`AgentImageProvider` 不会从 Python 进程反向调用宿主工具。它会把最终编译后的提示词、目标路径、
尺寸和参考图写入章节工作目录的 `agent_order.json`，然后以“待 agent 产图”结束。Agent 必须：

1. 读取工单中的最终提示词，不绕过 PromptCompiler 重新拼提示词；
2. 用当前会话的原生图像工具生成图片，并写入工单指定的 `path`；
3. 一次工单中的普通图、逐比例图或候选图全部完成后，确认文件真实存在且可读；
4. 重跑同一条 `gen-image` 命令完成零成本验收，并统一登记 `gen.image`、血缘和待审状态。

章节里已经登记的本地路径或 URL 继续按 checkpoint 视为已产出，不会只因缺少
`gen.image` 元数据而重新开工单；工单验收只认它指定的规范目标文件。

工单生成本身不是出图授权；在用户只确认“有能力”而未说“出图”时，仍停在文字、分镜和成本审查节点。
