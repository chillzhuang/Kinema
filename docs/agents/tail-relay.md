# 尾帧接力（tail_relay）

上一镜片段的**真实末帧**作为下一镜的参考图随请求发出，让镜与镜的开场构图、人物
位置与光线有像素级依据。判据单一真源 `engine/kinema/pipeline/tailrelay.py`；
渲染接线在 `cli.stage_gen_video`（`_relay_plan` / `_relay_inject`）。

## 承接阶梯（三档按镜自选）

| 档 | 机制 | 得到 | 代价 |
|---|---|---|---|
| 强焊接 | `frame_chain` 首帧任务（framechain.py） | 像素级无缝 | 参与镜丢参考图通道（官方禁混），板/设定图不发 |
| **软承接** | `tail_relay`：上一镜尾帧作一张 `reference_image` | 分镜图+板+设定图+时间轴+尾帧同发 | 占 7 张参考限额一席；承接强度弱于首帧驱动 |
| 文本承接 | `entry_state` 字段（prompt 契约 delta 骨架） | 零成本兜底，任何 provider 可用 | 纯文字引导 |

`entry_state` 与上一镜 `end_state` 成对咬合（`lint` 的 `entry_continuity` 查单侧
缺失）；`tail_relay` 是在文字咬合之上追加像素参考，两层互不替代。

## 数据流

1. 章级 `tail_relay: true`（或 `gen-video --tail-relay`）× native/dubbed 生效；
   provider 须同时支持 `supports_return_last_frame` 与 `supports_reference_images`
   （当前 seedance/mock）。minimax-h3 两位皆无——官方 v2 API 无尾帧回传参数、
   首尾帧模式与参考素材模式互斥（minimax.py 模块头留档）；veo 无
   参考图通道。路由到能力不齐的 provider 时接力**自动失效**：不发回传请求、
   不注入承接、每 provider 点名一次，且不强制串行（接不了力不剥夺并发）。
2. 每镜请求带 `return_last_frame`，回传的 `last_frame_url` 在回填时即刻落盘
   `<章节>_work/tail/shot_<id>_<比例>_tail.png`，登记 `gen.clip.tail_frames`
   （逐比例，engine-managed，不进版本栈——接力素材，重生成同名覆写）。
3. **串行注入**：整批强制 workers=1（`parallel.run` 单并发是主线程内联串行——
   跑完一件、回填一件、才派下一件）。上一镜回填时把尾帧注入下一个计划项并
   重编译其 PromptEnvelope（`@图片N` 职责句声明「上一镜的收尾画面，开场从它
   延续」，附图顺序：分镜图 → 板 → 尾帧 → 设定图）。
4. 同批接力优先用**新鲜 URL**——它是模型自身产物、走受信素材口径且免落盘往返；
   跨轮重投（`--only` 单镜 retake）用盘上副本。
5. 快照留痕：`gen.clip.tail_relay_from` 记承接来源镜号；Envelope `references[]`
   含 `role=tail_frame` 条目，参与过期判定。

## 边界与降级

- **衔接参与镜不接力**：首帧任务官方禁混参考图；V2V 镜同样不参与（运动权威冲突）。
- **遇转场断开**（场景切换标记，跨转场承接=把两个场景缝起来）；弃用镜跳过往前找。
- **全称量词**：本次要出的每个比例都有尾帧才接力，不齐即整镜降级为文本承接。
- **回传落空不静默**：provider 未回传 `last_frame_url`（型号/任务类型不支持）时
  逐 provider 点名一次，后续镜按无承接生成（时间轴/板/设定图照发）。
- **旧尾帧撤销**：上一镜本轮重生成却没拿到尾帧时，下一镜计划期按旧版尾帧编入的
  承接被撤销（重编译回无承接提示词）——旧收尾画面已随版本失效，带着它声称
  「从它延续」比不承接更糟；上一镜本轮未重生成（跳过/断点捡回）则计划期尾帧
  仍对应当前片段版本，照常生效。
- 注入/撤销共用同一重编译出口（提示词声明、@图片N 编号、实附 ref_images 三者
  一次改齐）；重编译失败（如超 provider 字数上限）保留计划项现状，不断批——
  提示词与实附参考恒一致。
- dry-run 与真发同一计划期口径：盘上有尾帧即入提示词，否则注记「生成时注入」。
- 尾帧在官方受信模型产物清单内（`docs/kinema/seedance-face-policy.md` §3）；
  盘上副本重投被输入闸拦下（如超出受信有效期）时，该镜按无承接的基线提示词
  重发，或重生上一镜取新鲜尾帧。
- `return_last_frame` 在参考生视频/参考媒体任务类型上的服务端接受度**未经真实
  provider 验证**（引擎测试全离线）；首次启用按「首镜试跑」纪律先验一镜。

## 守卫

`engine/tests/test_tailrelay.py`：判据真源边界（opt-in/模式/转场/弃用/全称量词）、
dry-run 注记与计划期注入、真发闭环（回传→落盘→注入→Envelope 留痕）、非接力批
不发 `return_last_frame`、并发强制串行。
