# Seedance 人脸输入限制 · 对接指南

> 面向要给 Seedance（火山方舟 Ark / BytePlus ModelArk）传**参考图或参考视频**的开发者。
> 只讲这一个主题：**什么样的输入图会被拒、判据是什么、合法通道有哪些、引擎侧要配什么。**
> 能力总览见 [`providers.md`](./providers.md)；请求体字段见 `config/models.yaml` 的
> `providers.seedance-*` 与 `engine/kinema/providers/video/seedance.py`。

## 1. 现象

给 `contents/generations/tasks` 传含人脸的图，返回 **HTTP 400**：

```json
{"error":{"code":"InputImageSensitiveContentDetected.PrivacyInformation",
 "message":"The request failed because the input image 'content[2]' may contain real person.",
 "type":"BadRequest"}}
```

`content[N]` 是 `content[]` 数组下标，据此定位是首帧、末帧还是哪张参考图。
**被拒的请求未见计费**——4xx 直接返回、不产生 task id，也就没有 usage 记账；官方计费文档
未就此明示，但据此判断"某张图能不能过"的试错成本可视为零。

适用范围：**Seedance 2.5 与 2.0 全系（含 fast / mini）**。这是模型侧的输入分类器，
不是某个别名的配置问题，改参数绕不开。

## 2. 两条判据：写实度阈值 × 受信产物豁免

一张图能不能过，先看它**像不像一张拍出来的真人照片**（§2.1 的阈值）；超阈值的，再看它
**是不是方舟受信产物**（§2.2 的豁免）。两条都不满足才拒。

### 2.1 判据一：照片级真实度，不是"AI 生成"

网上流传最广的说法是「AI 生成的虚构人像可以过」。**本仓库实测证伪**——两张图出自
**同一个 Seedream、同一条链路、同一批次**，结果相反：

| 输入图 | 人脸在画面中的形态 | 结果 |
|---|---|---|
| 写实 CG 角色近景（`corridor/shot_4`） | 五官清晰、照片级材质与光照 | ✗ 拒 |
| 同角色·中庭鸟瞰（`corridor/shot_9`） | 人只有几像素 | ✓ 过 |

来源相同、结论相反 ⇒ **分类器判的不是"这图哪来的"，是"这看起来像不像一张拍出来的
真人照片"**。官方与第三方分析一致：照片级写实人脸（无论相机拍摄还是 AI 生成）超阈值；
**stylized portrait / 2D 插画 / 明显非写实的 3D 角色渲染**普遍低于阈值。

**对本工程的直接含义**：`style_prefix` 里的媒介锚点会直接决定输入图能不能过。
`cyberpunk` 档写的是「写实 3D CG 渲染，实拍电影摄影质感，照片级材质与真实光照」
——这是在主动往阈值上撞；`cyberpunk_2d` 档的赛璐璐平涂则天然在阈值之下。
**选画风就是在选这条链路通不通**，见主 SKILL 铁律 2⓪「媒介统一铁律」。

### 2.2 判据二：受信产物豁免绑「生成方式」，不绑字节、不绑来源

官方《便利创作含肖像视频》的「信任模型产物作为输入素材」一节写的是「Seedream 5.0 lite/pro **文生图**得到的
含人脸图片」。**那四个字是硬边界**，本仓库实测：

| 输入图 | 生成方式 | 字节 | 结果 |
|---|---|---|---|
| 照片级正脸半身像 | **文生图** | 原始 | ✓ 过（出片成功） |
| 同上 | **文生图** | **重编码**（JPEG 重压，体积掉到 49%） | ✓ 过（出片成功） |
| 同上，仅换西装颜色 | **图生图**（以第一张作参考图） | 原始 | ✗ `InputImageSensitiveContentDetected.PrivacyInformation` |

同账号、同模型、同一张脸、同一 base64 传输、相隔数分钟；第一张与第三张视觉完全一致，
**唯一变量是生成方式**。三条常见推断由此证伪：

1. base64 内联会丢受信 —— **不会**，三次全走 base64，前两次照过。**本仓现有的
   「产物 URL → `download` 原字节落盘 → `file_to_data_url` 转 base64」这条路径不受影响**，
   传输链路一行都不用改；
2. 受信绑来源、必须传原始 URL 或转存 TOS —— **不绑**。官方那句「建议转存至 TOS」是在解决
   产物 URL 24 小时过期，不是受信条件；
3. 受信绑内容指纹、字节不能动 —— **不绑**，重编码掉一半体积仍受信。

以上实测口径：同账号、Seedream 5.0 pro、有效期内。

**对本工程的直接含义**：图生图产物不在受信范围内，故写实档（`image.identity_sheet`）
的**角色身份图走纯文生图**（`cmd_gen_refs._plan` 的 identity 分支：不挂蓝图、不挂
moodboard，`refs` 整体为空），生成方式记录在 `characters[].sheet_origin`
（`t2i` 才受信）；其余设定图与分镜图照常图生图——它们不含人脸或由降级路线处理。
身份图作视频参考时的取材形态也与官方口径合流：官方《Seedance 2.0 系列提示词指南》
建议「人物参考使用大头照 + 全身照即可，不建议使用人物多视图」，角色设定图因此
不设侧视（三区两视：正面肖像特写 + 正面/背面全身）。引擎侧的完整生产纪律
见 `docs/agents/photoreal-face.md`。

## 3. 官方合法通道（四条）

| 通道 | 适用对象 | 前置 | 备注 |
|---|---|---|---|
| **私域虚拟人像库** `asset://<ID>` | **自有虚构角色** | 素材库 API（AK/SK 签名，`open.volcengineapi.com`）+ **公网可访问 URL** | 形象一次入库、全项目复用；与视频生成的 Bearer 鉴权**是两套** |
| **预置虚拟人像库** | 不挑长相 | 控制台开通 + 复制 asset ID（无查询 API） | 平台预置形象库；**产出默认不可对外发布，见 §5** |
| **真人肖像授权** | 真实人物 | 控制台人脸验证 + 逐条接收授权 | 每位演员单独资产组；每次上传做一致性校验 |

第四条是**受信模型产物**（判据见 §2.2）。官方给出的三行范围与生效日：

| 受信范围 | 生效时间 | 有效期 |
|---|---|---|
| Seedance 2.5 / 2.0 系列生成的含人脸**视频** | 2026-03-11 起 | 30 天 |
| 上述视频对应的**尾帧图片** | 2026-04-16 起 | 30 天 |
| **Seedream 5.0 lite/pro 文生图**得到的含人脸图片 | 2026-04-16 起 | 30 天 |

三条共同前提：仅方舟平台产物、仅**同账号**、在有效期内、**仅对输入生效**（输出仍可能被
输出侧审核拦下）。本仓对文生图一行的实测（§2.2）表明该行绑的是**生成方式**而非字节
——重编码不影响，图生图才是出局的那一档。

⚠️ **`asset://` 入库与 `reference_video` 要求公网可访问的 http(s) URL，不收 base64/data
URI**；本工程默认把图转 base64 内联（`seedance._img_url`），走这两条路必须先配对象存储
（OSS/COS/TOS，见 `kinema-setup`）。**受信产物这条不在此列**——它收 base64，无需对象存储，
这是四条通道里唯一零基础设施的一条。

## 4. 明确不做的

社区流传的**网格叠加、线框贴图、裁切遮挡、降分辨率、对抗噪声、自动重试轰炸**等
手段，目标是让安全系统认不出人脸。这类做法与供应商服务条款相抵触，**本仓库不实现、
不提供此类能力**。附带的工程代价也很实在：污染输入 = 出片质量下降。

「换第三方托管（fal.ai / Replicate 等）因为审核更松」同理不作为方案——
该说法来源是推广博客、未经证实，且本质是挑执法更松的通道。

## 4.5 引擎侧的处置（已接进运行时）

判据不能只躺在文档里——建任务 400 若原样抛出、收尾统一打「重跑同一条命令会自动
跳过已成功的」，而这一类**重跑必然同样被拒**，那句话等于在教人反复重出图再试。

| 落点 | 行为 |
|---|---|
| `errors.ProviderError.code` | 厂商结构化错误码随异常上浮，调用方按码分流（不对错误文案做子串匹配）；轮询期任务 `failed` 同样解析 `error.code` |
| `providers/video/seedance._create_error` + `cli` 失败回填 | 识别 `InputImageSensitiveContentDetected*`；官方报的 `content[N]` 下标经参考装配计划（`pipeline/refplan.RefPlan.at`）逐镜翻成具体哪张图 |
| 写实档降级阶梯 | 人脸拒不停批，被拒镜自动换降级装配重发一次（A/B/C 路线，判据见 `docs/agents/photoreal-face.md`） |
| 输出侧审核拒（`OutputVideoSensitiveContentDetected*`，任务 failed） | 判的是渲染出来的成片内容，与输入参考装配无关——不进降级轮、不停其余镜的派活；收尾按「改内容」口径给处置（排查提示词政策类目 / 重出身份图换长相 / 改构图），绝不引导降级或同参数重跑 |
| `cli._retry_advice` | 审核类失败**不给重跑口径**：输入拒未降级的按处置路线给修法，降级后仍被拒的点名被拒图号并给重出身份图的修法，输出拒给改内容路线；混合批次多段口径并存 |

**输出侧的实测口径**（photoreal3d 档 / seedance-2.0-mini）：受信身份图 +
场景基准图的降级装配（路线 B）真发出片 5.09s，**输出审核放行**——写实人脸成片
并非一律被输出侧拦下，该判定是内容相关的（政策类目撞线才拒），与「输入图疑似
真人」是两层独立的审核。

守卫：`tests/test_providers_request.TestFacePolicyError` / `TestSeedancePollErrorCode`、
`tests/test_face_route`（含 `TestOutputPolicyRejection`）。

## 5. 工程侧对接清单

**已在引擎里的**（`providers/video/seedance.py` + `config/models.yaml`）：

| 能力位 | 作用 | 2.5 | 2.0 fast/mini |
|---|---|---|---|
| `resolutions` | 合法分辨率白名单，本地先拦省一次远端 400 | `480p/720p` | `480p/720p` |
| `min_duration`/`max_duration` | 时长档位。**配小了不报错、只静默截断**，钱按截断后的秒数付 | `4~30` | `4~15` |
| `ratio_mode` | `adaptive`=该型号在**受限任务类型**上只收 adaptive；按任务类型分别发，不一刀切 | `adaptive` | `explicit` |
| `supports_seed` | 官方 seed 支持列表只到 1.5 pro / 1.0 系列 | `false` | `false` |
| `supports_camera_fixed` | 同上 | `false` | `false` |

**受信产物这条已接进引擎**（写实档缺省形态，`docs/agents/photoreal-face.md` 是
纪律真源）：角色身份图纯文生图（`identity_sheet` 档的 identity 分支）；生成视频
按镜走 A/B/C 路线阶梯——分镜图照常画，被人脸拒后自动换「场景基准图 + 简笔板 +
受信身份图」的降级装配重发一次；`_gate_cast_anchor` 按路线判身份来源。
这条路**不需要**对象存储、不需要 AK/SK 素材库客户端、不受公共素材的商用限制。

**要走 `asset://` 或 `reference_video` 还缺的**：

1. 对象存储（把本地图/片传上去拿公网 URL）——`kinema-setup` 有这一节
2. `_img_url()` / `_vid_url()` 放行 `asset://` 前缀（前者现只认 `http(s)` 与 `data:`，
   后者只认 `http(s)`）
3. 素材库客户端：AK/SK 签名（HMAC-SHA256 V4）+ 素材组/素材两级 CRUD

⚠️ **公共（预置）虚拟人像的产出默认不可对外发布**：《资产功能使用规则》与
《方舟体验中心服务规则和免责声明》限定参考素材及其生成内容「仅可用于模型效果体验和内部
使用目的」「不可以进行任何商业化目的使用」。要发布只能走私域入库或已授权真人。

## 6. 排错速查

| 报错 | 含义 | 处置 |
|---|---|---|
| `InputImageSensitiveContentDetected.PrivacyInformation` | 输入图疑似真人 | 换低写实度画风 / 换无脸帧 / 走 §3 合法通道。**若该图出自 Seedream 却仍被拒，先确认它是不是图生图产物**——图生图不在受信范围（§2.2） |
| `OutputVideoSensitiveContentDetected.PolicyViolation` | 成片未过输出侧审核（判生成内容，任务 failed） | 改内容后重生该镜：排查提示词/台词的政策类目（近似真实公众人物、制服执法、未成年观感、敏感组合）、重出身份图换长相、改构图（背身/远景/剪影）。降级与同参数重跑都改变不了判定 |
| `ModelNotOpen` | 该模型未在本账号开通 | 控制台开通；注意开通条件（余额 > 200 元 或 节省计划 或 资源包余量）与**区域必须是 cn-beijing** |
| `InvalidParameter`（ratio/resolution） | 参数不合该型号 | 对 §5 能力位表，改 `models.yaml` 别名字段 |
| 拿回的片子比请求短 | 时长被 `max_duration` 静默截断 | 核对别名的 `max_duration` 与控制台档位 |

## 7. 参考

- [火山方舟 · 便利创作含肖像视频](https://www.volcengine.com/docs/82379/2608626)（四条通道总入口，§2.2 / §3 的官方依据）
- [火山方舟 · 创建视频生成任务 API](https://www.volcengine.com/docs/82379/1520757)
- [火山方舟 · Seedance 2.0 系列提示词指南](https://www.volcengine.com/docs/82379/2222480)（「不建议使用人物多视图」出处）
- [火山方舟 · 虚拟人像库](https://www.volcengine.com/docs/82379/2223965)
- [火山方舟 · 私域虚拟人像库](https://www.volcengine.com/docs/82379/2333565)
- [火山方舟 · 录入真人形象素材](https://www.volcengine.com/docs/82379/2315856)
- [火山方舟 · 资产功能使用规则](https://www.volcengine.com/docs/82379/2275639)（公共素材的授权范围）
- [火山方舟 · 错误码](https://www.volcengine.com/docs/82379/1299023)
- [火山引擎真人认证与同人认证的人脸信息处理规则](https://www.volcengine.com/docs/82379/2307807)
- [BytePlus ModelArk · Seedance 2.0 series tutorial](https://docs.byteplus.com/en/docs/ModelArk/2291680)

> 取料备注：`volcengine.com/docs` 是客户端渲染，WebFetch 取不到正文；改用
> `https://www.volcengine.com/api/doc/getDocDetail?DocumentID=<id>&LibID=82379`，正文在返回体的
> `MDContent` 字段。
