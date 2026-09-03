# 写实人物合规生产（photoreal face）

照片级人脸怎么合法地走完「设定图 → 分镜图 → 视频请求」。**判据真源**是
[`docs/kinema/seedance-face-policy.md`](../kinema/seedance-face-policy.md)（什么样的图会被拒、
四条官方通道）；本文讲**引擎侧的既定纪律**，不重复判据。

一句话前提：受信豁免绑「**是不是文生图产物**」，不绑字节、不绑来源（face-policy §2.2）。
本仓的图默认是图生图（设定图垫版式蓝图、分镜图挂设定图），默认一张都不受信——
写实档因此把角色身份图单独收成纯文生图。

## 1. 写实档与身份图

`config/models.yaml` 里 `image.identity_sheet: true` 的画风档是**写实档**（照片级媒介
锚点，会撞视频侧人脸分类器）；名单由 `test_config_drift` 的登记清单钉死，增删须
说明媒介锚点依据。

写实档下 `project refs` 的**角色设定图走纯文生图**：不垫蓝图、不垫 moodboard
（挂任何一张参考都变回图生图、受信整条失效），版式全靠 `sheets.char_rules` 文字
契约；非写实档照常垫单张蓝图。两档产出**同一版式**（三区两视）。道具、武器与
场景不含人脸，全档照旧图生图。

**人体比例在这一档只有文字这一条通道。** 蓝图人台按八头身画、胯线落在全高中点
或略高于中点，非写实档可以照它对齐定位线（`sheets.template_role` 放行的就是这一维）；
写实档一张参考都不挂，比例全部落在 `sheets.figure_proportions` 上。故那段文字
逐段给出可数的分段高度（头顶到下巴 1 头…胯线到膝 2 头…）而不是只声明一个头身比，
并排在版式总纲之后的第一条抢前位权重——只写「头身比协调」时模型按训练集均值落笔，
胯线会掉到全高 0.56~0.59、腿长只剩四成。

`characters[].sheet_origin` 是生成方式的**事实记录**（`[engine-managed]`）：
`t2i` / `i2i` / `external`，缺失视为未知。写 `sheet` 的五条路径（gen-refs 直出 /
候选定稿 / refine 局改 / 版本回滚 / 素材直供）逐处同批写；系列 → 章节经
`workspace.char_fields` 白名单与 `refine._propagate` 两条通道同批携带；版本回滚
随版本条目还原。只有 `t2i` 落在受信豁免内。

**refine 对写实档的角色身份图直接拒绝**：局改恒以旧图作主参考，属图生图，产物
当场失去受信。要调整就改角色描述后整张重出
（`project refs --only character:<名> --force`），Studio「↻ 重新生成」的带批注路
同样被拒并给出同一修法。

image provider 须自声明 `trusted_face_source` 能力位（seedream / mock 为真）；
写实档下角色设定图只由具此位的 provider 直出，不具位时拦在归档与计费之前
（不受信产物过不了人脸审核，却会以 t2i 名义给降级路线背书）。

## 2. 视频请求：三级路线阶梯

人脸拒发生在**建任务 HTTP 400，不计费**——所以分镜图不接受任何为过审而加的构图
约束：近景正脸照常画，先按最好的构图试，被拒了再降级。路线仲裁在
`cli._route_for`（dry-run 与真发共用），只在写实档武装：

```text
路线 A（缺省）  image=分镜图 + [板] + [尾帧] + 身份图×N + 场景图 + 俯视图 + 道具图
                    ↓ 被人脸拒（HTTP 400，免费；不停批，其余镜照常派活）
路线 B（降级）  image=场景基准图 + 简笔板 + 身份图×N + 俯视图 + 道具图
                分镜图整个不进请求；场景图从设定清单剔重；缺板就地生板
                （stage_sketch_gen，计入图像台账；审阅锁在场的镜不代买板，
                  按无板形态比对审阅稿）
                    ↓ 板不可能（拆不出拍 / 生板失败）
路线 C（兜底）  B 去掉板，构图交提示词 framing/angle/lens 与俯视图的空间证据
```

- **身份图三条路线都必发**：它是唯一的人脸来源，不发模型只能按训练集均值另造
  一张脸。`_gate_cast_anchor` 按路线判——A 判分镜图的设定图指纹；B/C 判身份图
  在不在真发清单里（被 7 张配额裁掉即拦）。
- 降级只一轮，且有硬前置：出场角色身份图全部 `sheet_origin == t2i`（否则降级
  同样被拒、白买板）、主场景基准图在盘、非 previz 镜。不可降级的镜保持失败并
  逐镜点名原因。
- **B/C 仍被拒是阶梯的死局**：失败回填时经 RefPlan 把官方的 `content[N]` 下标
  翻成具体哪张图逐镜点名，收尾文案给出修法——最常见的原因是身份图不是受信的
  文生图产物，重出即解。
- 提示词随路线换口径：B/C 的 @图片1 是**取景地基准**（`CONTRACT_ALLREF_BASE`）
  而非「本镜画面」；板的画风归属改指场景基准图；身份图沿用 `character` 职责句并
  带边界「所处环境、光线与构图不取自该图」。装配五处（manifest / ref_images /
  envelope / Studio 预览 / content[]）由 `pipeline/refplan.RefPlan` 单源产出。
- **场景基准图是降级路线的光线真源**：路线 A 下分镜图压住它、时段问题不显形，
  一降级 @图片1 就是它——一张自选成黄昏的基准图会把白天戏整镜拖成暮色。两道
  保护：登记场景时在 desc 钉死时段与主光（lint `scene_daypart_missing` info 提醒
  没表态的）；镜写了 `lighting` 时 `prompts.allref_base_contract` 把光线权威从
  基准图移交本镜描述，基准图只保留陈设与材质。

`shots[].face_visibility`（可选，自由文本）：标 `closeup` 表示作者预判本镜必含
可辨识正脸，直接从 B 起步、省一次免费往返，缺板会先生板；不标完全正确。
它不产生任何构图约束、不使任何产物过期。缺板的 closeup 镜在计划循环之前
整批并发出板（`stage_sketch_boards`，与降级轮同一出口），循环内的单镜同步
出口只兜整批落空的那一镜——逐镜串行每张约两分钟，四镜就让整批空等六分钟。

**处置纪律（对指挥层）**：人脸拒的全部处置都在阶梯内、由引擎自动完成。绝不为
绕闸把有脸构图改写成无脸/背影/局部——那是删掉正面表演，且没有必要（人脸拒
针对的是输入参考装配，不是构图本身；路线 B 带脸照常出片）。路线 B 成片与设计
有出入时按证据排查：@图片1 换成了场景基准图（光线/时段随它，desc 没钉死就修
场景图重出）、降级日志的路线与理由、`gen.clip.face_route` 留痕——不动
`image_prompt` 的表演设计。

## 3. 模式仲裁

| | 写实人物档 | 理由 |
|---|---|---|
| 缺省档（`ref_mode` 全能参考） | 阶梯的宿主 | 能同时带分镜图/场景图与受信身份图的参考任务 |
| dubbed（参考媒体） | 同进阶梯 | 图与音频都是参考、板与设定图随 `ref_audio` 合法附发——人脸敞口与降级形态都与全能参考同构，取景地契约句共用 `CONTRACT_ALLREF_BASE_*`，`ref_audio` 照发对口型。任务门槛判据单点在 `cli._ref_task` |
| `frame_chain` / `anchor_frame` | 路线 A（首帧任务） | 官方禁附参考图，降级装配无从谈起；例外是衔接章里显式 `sketch.reference` 的参考孤岛镜——它走全能参考，照常参与阶梯 |
| 尾帧接力 `tail_relay` | 与阶梯并行 | 尾帧是 Seedance 自产、官方受信；注入后 RefPlan 整体重建、配额重算 |
| V2V / previz | 恒路线 A | previz 参考视频非写实；previz 镜不降级（运动预演与降级装配互斥） |

## 4. 成本性质

- 人脸拒本身不计费（无 task id、无 usage）；降级重发花的是新一次的视频费——
  输入变了，是确定性补救，与「同参数自动重试」有本质区别。
- 板按分镜图同价计入图像台账；`gen-video --dry-run` 报写实档的降级敞口与最坏
  板费上限。
- 降级出片的版本在 `gen.clip.face_route` 留痕（B/C；无此键即路线 A），
  `sketch_board` 留痕跟的哪张板。
- 输出侧审核拒（成片渲染后被拦，任务 failed）在阶梯之外：判的是生成内容，
  降级与同参数重跑都改变不了判定——不停派、不进降级轮，收尾按「改内容」
  口径处置（口径与实测判例见 `docs/kinema/seedance-face-policy.md` §4.5/§6）。

## 5. 真源指针

| 事项 | 真源 |
|---|---|
| 受信判据与官方通道 | `docs/kinema/seedance-face-policy.md` |
| 写实档名单 | `config/models.yaml`（`identity_sheet`）+ `tests/test_config_drift` |
| 身份图纯文生图分支 | `cli.cmd_gen_refs._plan` |
| `sheet_origin` 五路写点 | `cli.cmd_gen_refs`（直出/候选）+ `refine.py` 的 `refine_asset` / `supply_asset_sheet` / `pick_asset_candidate` / `rollback_asset_sheet`；章节搬运在 `_propagate` 与 `workspace.char_fields` |
| 路线仲裁与降级编排 | `cli._route_for` / `cli.stage_gen_video`（降级轮）/ `cli.stage_sketch_gen` |
| 参考装配单源 | `engine/kinema/pipeline/refplan.py` |
| 取景地契约句与占位 kind | `pipeline/prompts.py`（`CONTRACT_ALLREF_BASE_*` / `_PLACEHOLDER_KINDS`） |
| 守卫 | `tests/test_face_route.py` / `tests/test_refplan.py` |
