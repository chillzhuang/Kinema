# 守卫地图（改动面 → 必过测试）

> **导航图而非闸**——登记随手维护，不设覆盖对拍。

**用法**：动了「改动面」列出的任一处，「必过」列出的测试就必须过。**漂移即红灯。**

任何改动跑全量 `python3 -m unittest discover -s tests`（离线、确定性、零付费）。
**守卫收敛纪律**：新守卫只加在引擎行为层（请求体/提示词装配/剪辑合成与台账/数据契约与
配置语义）；前端只收竞态与资源两类；**不为文档措辞、README 数字、图标、按钮文案加守卫**，
一次性「防复活」断言不进守卫。

---

## 1. 配置、模型与路由

改一处配置或路由，波及的是全部画风与全部厂商——这一组的守卫都在拦「一处改了另一处没跟上」。

- **改动面** `config/models.yaml` / `voices.yaml` / `audio.yaml` / 内嵌表

  **必过** `test_config_drift`
  - 内嵌一致
  - label 必填
  - style_prefix 双语
  - defaults.providers
  - 音频注册表 bgm 情绪/关键词与 sfx 键 desc/license 齐全
  - **`test_style_prefix_carries_no_slop_terms`**：任何 profile 的 `style_prefix` 不得含 `variation.SLOP_TERMS`——引擎一边禁作者写空词、
    一边自己每张图注入是双标；**两份真源都扫**（内嵌表是缺 PyYAML 时真正生效的那份）
    ；例外走 `_STYLE_PREFIX_SLOP_OK` 短登记表（≤4 条·每条写理由，现仅「气韵生动」一条）

- **改动面** `agent/manifest.json` → 编译 catalog → `skills.py` 薄运行时视图（画风→归属 skill 与旁白语态唯一声明链）
  · **退役绑定的 `bound_skill`/`bound_profile`**（落盘绑定专用解析：硬失败照旧、错误里带换绑路径）

  **必过** `test_agent_system`
  - manifest/catalog 来源与路由
  - **退役绑定报错含 `project set --skill` 指引且不与显式输入共用措辞**
  - route／章节上下文／explain 陈旧判定／Envelope 封装**四条通道**各自走 `bound_*`，源级钉死不许漏一处
  - `project set`／`chapter set` 与建项目同闸：未登记值当场失败绝不落盘、`chapter set --inherit` 只删 skill/profile/video_provider 三键（`test_workspace`）

  **并过** `test_config_drift`
  - `SKILLS` 视图恰好覆盖 models.yaml 全部画风
  - 无孤儿/无重复
  - 每个 Skill 的 voiceover 声明完备
  - **`skill_board()` 全集群契约**：overview 必下发且与 catalog 条目逐一对齐（含 route/workflow 之外五类 kind——少一条＝那个 skill 在 #/skill 大屏凭空消失）
  - skill.js 消费 `skill_board` 且带未知 kind 兜底组、CSS 有 `.sk-card`（DOM+CSS 都在判据）

- **改动面** **`config_overlay.py`（模型配置覆盖层单一真源：发现顺序/深合并/三态/白名单/密钥三态/零成本自检）**·
  `ConfigStore.load` 三条出口的叠加与 `ConfigStore.overlay` · `storage/base.load_settings|save_settings` 与 mysql 的 `kn_setting` 表 ·
  `cli` 的 `config` 动词族 · `studio/{server,actions,scanner}` 的配置面 · `studio_app/app/{config,brands}.js` ·
  **`providers/grades.py`（画质档位目录单一真源）**

  **必过** `test_config_center`
  - **无覆盖层时逐字节回落**
  - 深合并只动被写字段（其余别名与 `defaults` 的 profile/fps/aspect 兄弟键一律不动）
  - 坏 JSON 不抛只忽略
  - **绝不就地改写 `EMBEDDED_DEFAULTS`**（两条兜底出口把模块级字典原样交出，就地改一次污染整个进程）
  - profiles/canvas/voices 不可覆盖（画风侧三条守卫直读 yaml，覆盖层能改就是看不见的后门）
  - 密钥优先级 env>local>yaml 且**假密钥不出现在任何下发面**
  - 密钥拒入库
  - 写入口白名单拦密钥本体与非法字段
  - 单价必须落成数值（字符串会静默进台账算术）
  - **前端保存只提交改过的字段**（预填值原样回传＝把当下 yaml 值冻进覆盖层）
  - `runBusy` 的 `restore` 档（结果是弹层而非重渲时不还原＝整组按钮永久禁用）
  - `adapter_catalog` 与 `_ADAPTERS` 锁步
  - probe 期间 `request_with_retry` 零调用
  - **测试禁用哨兵真的关得掉**（空串在发现顺序里只是"本级没指定"，会继续往下读到开发机上真实存在的那份）
  - **服务商品牌标**：`IMPL_META` 里每个 vendor 都必须在 `brands.js` 有标（缺一个不报错、只是那张卡悄悄退回首字母）
  - 图形出处与 CC0 许可必须写在模块头
  - 认不出的厂商留首字母绝不编假标
  - `config.js` 不许自己拼标
  - **能力清单单源**：id 与中英文名只在 `config_overlay.CAPABILITY_META` 声明，随 `capabilities[]` 下发；`config.js` 不按 id 另存名称表（`CAP[` 不得出现），只按 id 配线框图，缺图不影响出牌；路由牌列数随清单
  - 无 `api_key_env` 的多凭证服务商主密钥位取第一把（否则缺密钥显示成「免密钥 · 就绪」）；签名式接口以 `host` 过端点检；适配器自报的 `configured()` 进自检（缺 req_key 点名、有降级分支不标红）
  - **画质档位目录**：每个视频适配器都要登记档位
  - 目录说的字段必须真的存在于适配器实例上（`field` 刻意不假定叫 `resolution`——各家对「画质档」
    的字段名并不统一）
  - 该字段必须在覆盖层白名单里（否则保存那一刻被拒）
  - 每条档位必带出处
  - 随包配置（models.yaml **与** `EMBEDDED_DEFAULTS` 两份）写的档位必须在自己目录内
  - **目录只说明不裁决**（`grades.py` 无 raise、cli 不引它，`--resolution` 仍是裸赋值——厂商开新档时本机表必然滞后，
    做成闸就是发不出去）
  - **H3 是唯一一家目录真的会裁决的**（发前归一白名单派生自目录，删一档=把用户配好的值静默改写成 768P，
    故那组值逐字钉死）
  - 大小写折叠只给「适配器自己会 upper 的」那一家开（全局折叠会把 seedance/veo 的真告警变漏报）
  - 「本机已改」与档位提醒同槽位只能拼不能抢
  - 目录外的当前值原样保留不改写
  - 前端按「后端给没给档位块」渲染而不是按能力硬猜
  - 抽屉 input/change 两种事件都听且三处取值同口径
  - **控制台入口对拍**（凡声明密钥变量的 provider 必须在 `IMPL_META` 登记 console 地址并随 provider_view 下发——前端「打开控制台」
    与密钥弹窗引导都吃它）

- **改动面** `models.py` 路由/工厂 · `resolve_named` 运行时点名（视频双模型策略的解析真源）· `ConfigStore.fallback` 回退标记（缺 PyYAML/无 models.yaml 时非空，
  doctor `[!]` 行与 overview `config` 块共同数据源）

  **必过** `test_router_defaults`
  - 默认链
  - 偏离项
  - impl 别名
  - **seedance 缺省=mini 且 2.5 只有点名才上**（内嵌与 yaml 的 model 串逐字对拍·resolve_named 拒未知别名与错能力·
    mock 态不联网·gen-video 三处解析共用 `_vroute`）
  - **回退必带 fallback 标记且健康环境恒 None**

  **并过** `test_config_drift`（overview 下发 config 块·project-new.js 消费 fallback 渲染告警条且 CSS 在位）

- **改动面** `ModelRouter.resolve` 的缺省链失败路径（视频与其他能力同口径）

  **必过** `test_router_defaults`（`test_video_without_defaults_raises_not_silently_picks_a_vendor`：
  缺 `defaults.providers.video` 报配置错并给指路文案，**绝不静默落到某个厂商别名**——路由器里的厂商硬编码等于废掉「换模型改配置」
  承诺；零配置场景由 `EMBEDDED_DEFAULTS` 的 defaults 兜住走不到这条）

## 2. 供应商请求拼装

请求体是花钱那一刻发出去的东西，拼错不报错、只是结果不对且钱照付。

- **改动面** `providers/` 任何请求拼装（seedtts/seedance/minimax…）

  **必过** `test_providers_request`
  - expressive 四组合
  - SSE 截断
  - 时长钳制
  - 模式互斥
  - **Prompt 不得静默截断**（provider 只声明 `max_prompt_chars`，Compiler 超限在请求前拒绝）
  - **轮询期 429/5xx 走容忍带不得终止**（裁决单点 `_util.raise_for_poll`，四家轮询段源级禁自写 `status_code >= 400`）
  - **`ref_kind="character"` 能力位**（minimax 图像 subject_reference 只吃出场角色设定图：
    设定集首张恒是场景全景，盲取首张=SCENE 被标成 character）
  - **Veo 首尾帧插值强制 8s**（`billable_seconds` 收 `last_frame` 位，dry-run/预留额度/
    真发三处同源取档——720p+lastFrame 发 4/6 必被拒）
  - **Gemini 图像比例/分辨率走 `generationConfig.responseFormat.image`**（modalities 恒
    TEXT+IMAGE；恒不发 `imageConfig`）
  - **MiniMax TTS 缺省音色随站点**（两站音色 ID 命名互不相通，国内拼音短名打国际站必被拒）
  - **ElevenLabs 钉 model/output_format/force_instrumental 且时长钳官方域**
    （music [3s,600s]、sfx [0.5,30]s；缺省 model 落 v1、缺省格式随模型漂）

- **改动面** `providers/video/seedance.py` 的 2.0 顶层参数与 V2V content 项 · `providers/base.VideoProvider.supports_reference_video`/`supports_reference_images`

  **必过** `test_providers_request`
  - **参数走顶层 JSON 不是 1.x `--suffix`**（正文里不许再出现任何 flag，2.0 无 `--fps`）
  - seed 不给就不发字段
  - `camera_fixed` 只在显式要时发
  - V2V 的 `video_url`/`reference_video` content 项与 role 顺序
  - V2V 下不发首/末帧

  **并过** `test_providers_request.TestFacePolicyError`（**输入图审核未通过要走另一条收尾口径**）
  - 建任务 4xx 解析出结构化 `ProviderError.code`；`InputImageSensitiveContentDetected*`
    另把官方报的 `content[N]` 下标翻译成输入图身份（请求体里首帧、设定图与参考音混在
    同一个数组里，下标本身无从对照，不翻译用户只能挨个换图去猜）
  - `cli._retry_advice` **按错误码分流**，不对着错误文案做子串匹配：这一类重跑必然同样被拒
    （判据在模型侧的输入分类器，改参数绕不开），给的必须是处置方案而不是
    「重跑同一条命令会自动跳过已成功的」——后者就是在教用户反复重出图再试，
    每试一次都是一次生图钱。判据与四条官方通道详 `docs/kinema/seedance-face-policy.md`
  - **本地路径必抛错且不做 data-url 兜底**
  - 计费 = 输出秒 + 输入视频秒
  - 只有 seedance 与 mock 声明 `supports_reference_video`
  - **额外参考图（简笔板）**：`ref_images` 挂 role=reference_image 排在首/末帧后、绝不进 V2V 分支、
    钳 7 张、能力旗只有 seedance/mock 为 True

- **改动面** `pipeline/refplan.py` 参考装配单源：manifest / 工作线程 `ref_images` /
  Envelope references / Studio 预览 / provider `content[]` 五处消费一处产出；
  构造期不变量（≤7 张、路径不重复、kind 白名单）；`at(no)` 供 `content[N]` 错误下标翻译

  **必过** `test_refplan`
  - **黄金逐位对拍**：mock 真发下 provider 实收的 image + `ref_images` 顺序、envelope
    references 角色序列、Studio 预览编号与 dry-run 组成行五处同源
  - 超配额 / 重复路径 / 表外 kind 一律构造期抛错，不流到计费
  - 路线 B 的 manifest 首位是 `scene_base`；尾帧在场时 `at()` 不截末项

- **改动面** 写实人脸路线阶梯（`cli._route_for` / `_face_route` / 降级轮 /
  `stage_sketch_gen` / `_gate_cast_anchor` 路线分支；档位名单 `models.yaml`
  `image.identity_sheet` × `test_config_drift._IDENTITY_PROFILES` 登记清单）

  **必过** `test_face_route`
  - identity 档角色设定图 `refs` **整体**为空（纯文生图受信）且 `sheet_origin=t2i`；
    非写实档蓝图/moodboard 分毫不动
  - `sheet_origin` 五路写点（直出/候选定稿/refine 拒绝/版本回滚往返/素材直供）
    一条不漏，系列 → 章节两条搬运通道同批携带
  - A/B/C 仲裁矩阵：不受信 / 无场景基准图 / previz 镜恒 A 且给具名理由；
    closeup 缺板就地生板走 B、不直落 C（dry-run 清单行注记「路线C→B(真发前自动补板)」）；
    多镜缺板在计划期整批并发出板（`stage_sketch_boards`，与降级轮同一出口），循环内
    不再逐镜同步生板；板到位后路线与理由一并重取，日志不得出现「路线B（…无板…）」
    （`test_closeup_boards_batch_before_dispatch` / `test_inline_board_rearbitrates_route_reason`）
  - dubbed 参考媒体与全能参考同进阶梯（任务门槛单点 `cli._ref_task`，计划期与
    降级轮同判据）：路线 B 下 image 位换场景基准图、`ref_audio` 照发对口型、
    取景地契约句共用 `CONTRACT_ALLREF_BASE_*`（`TestDubbedDegrade` / `TestDubbedBaseContract`）
  - 输出侧审核拒（`OutputVideoSensitiveContentDetected*`·任务 failed）单列分流：
    不停派、不进降级轮，收尾按「改内容」口径；轮询期错误码结构化上抛
    （`TestOutputPolicyRejection` / `TestSeedancePollErrorCode`）
  - 人脸拒不停批、降级只一轮、二拒死局点名身份图；预算断闸后一张板都不买
  - 取景地契约句与板职责句 base 变体 ZH/EN 双份、表外 kind 抛 `PromptContractError`
  - 写实档身份图只由 `trusted_face_source` provider 直出（不具位拦在归档与计费之前，
    道具/场景不经此闸）；「角色主体」参考位 provider 下道具样板声明归零、俯视图整波不画
  - 直出定稿清候选三件（candidates/candidates_origin/picked）；`_propagate` 走
    `sync_design_to_chapters` 白名单且补齐 `sheet_origin` 空值
  - 尾帧注入后重验 `_gate_cast_anchor`：降级路线下身份图被配额挤出时承接撤销
    （`TestSendPathPins` / `TestPropagateSyncsChapters` / `TestDirectRegenClearsCandidates`）
  - 降级轮补板成功后路线与理由出自同一次 `_route_for` 仲裁（`TestDegradeLogConsistency`：
    只改 route2 不改 why2 会打出「路线B（…无板…）」的自相矛盾行，把人误导向
    「板没生出来 → 路线 B 不行」的错误处置）
  - 降级路线的光线权威随 `lighting` 移交（`test_prompts.TestAllrefBaseLighting`：
    `allref_base_contract` 在镜写了 `lighting` 时把光线从 @图片1 交给本镜描述——
    基准图的时段是生成场景图时自选的，照单全收会把白天戏拖成基准图的黄昏；
    配套 lint `scene_daypart_missing` info 提醒场景 desc 钉死时段）

## 3. 提示词与画面契约

提示词是引擎唯一能对画面施加的控制手段，每一条契约句的措辞都经过实拍标定。

- **改动面** `pipeline/prompts.py` / `voicecast.py`（两条最贵策略）

  **必过** `test_prompts`
  - 提示词拼装
  - 前缀双语选用
  - 音色解析链
  - `shot_expressive_params` 音色门控

- **改动面** **`voicebank.py`（音色档案库单一真源）**：候选/档案两级 · `_register` 立档（模版按 voice_type 去重·
  定制 voice_type 由新档案号派生）· `cast_for_ref` 在用推导 · `clip_for`/`voice_desc` 章节侧解析 ·
  `reference_index`/`cast_references`/`delete_cast` 引用闸 · `propagate`/`STALE_NOTE`/`_unflag` 音色血缘 ·
  `default_candidates`/`NARRATOR_POOL`/`character_gender` 候选推荐 · `bank_view(s)` 展示模型（CLI 与 Studio 共用）
  · `cli` 的 `voice audition\|custom\|use\|bank\|rm\|list` · `studio/{actions,server}` 四端点 ·
  `studio/scanner._voice_bank_view` · `project.js` 选角卡

  **必过** `test_voicebank`
  - **重新试音不许动在用音色**（选中态错乱的根因）
  - **`character rm` 摘除章节 `voices{}` 指派、删档同步不写回空键**（`TestCharacterRemoval`：
    否则已不存在的角色以「仍指派着它」挡住 `voice rm`）
  - **档案记实测语速**（`TestSpeechRate`：候选块记试音台词 `text`，`_register` 按音频时长得
    `speech_rate`；探不出时长不记；`import_cast` 沿用源值）
  - 定制三次选定=三条档案/三个身份/三份互不覆盖的音频（覆盖=上一把声音物理销毁）
  - 模版重选同一把复用既有档案（否则「在用哪条」无解）
  - 在用态由 `voice` 推导、手工改指派即落「未入档」
  - 候选认领：模版按 voice_type、定制按 (批次,编号)
  - 章节自带档案库故脱机可解析参考音与声线描述
  - 候选目录滚动清理但档案音频不受影响
  - **引用四面**（在用/已产出含归档版本/句级指派/无引用才放行）
  - 删除后档案与音频同时干净且章节侧随行副本更新
  - 引用索引全项目只建一次
  - **血缘**：未锁定置 retake、已通过只挂标记、来回切幂等、撤销时**原样**还回原表态、人自己打回的重做不动
  - `voice_desc` 取在用那把（扫全表会取到已经不用的嗓子）
  - 跨项目引入重发档案号并另存音频
  - **逐镜直出双锚**（`TestCustomDirectTts`：定制实体=声线描述文案拼剧本体+参考音同发·台词恒引号体·
    emotion 编进文本；模版实体照旧 speaker+裸台词；档案缺参考音硬拦绝不静默换声）

- **改动面** `pipeline/prompts.py` 增量编译面：契约句**六族**（首帧/参考图/FLF2V/V2V/全能参考/取景地变体 `CONTRACT_ALLREF_BASE_*`，末者由 `ref_base` 在两条参考任务上二选一——全能参考与 dubbed 参考媒体共用同一句、守卫 `test_face_route.TestPromptContract` / `TestDubbedBaseContract`）·
  **`STRUCT_LOCK_*` 结构锁与 `_wipe_markers()` 门控** ·
  **`MICRO_MOTION_*` 微动恒常尾句**（注入点写死在 `if not body:` 的 **else 分支 + sketch 块之后**：
  注在兜底分支之前会打红`test_no_fallback_to_image_prompt` 与 `test_review.TestFrameChainLastFrameGate` 两条，
  注在 sketch 块之前会被 auto 拆拍的 `vmotion = tl` 静默吞掉；链上镜不注） · **`_zh_join` 标点感知拼接器**（「。
  ，」缝的单一修法，落 sfx/角色锚/负面串/台词句四处） · **`native_voice_clause`**（对白/旁白二分 + `DIALOGUE_VERB_ZH` 确定性查表，
  语态判据走 `voicecast.voice_kind`） · delta 骨架 `DELTA_FIELDS` · 两档兜底句 · `with_text_floor`/`negative_clause` 防字地板与语种连接词 · `with_performance_floor` 表演地板（缺省把叹气/深呼吸/明显的胸肩起伏/流泪压进负面串，本镜正文——`performance_hay`：运动正文、delta 骨架、拍点动作、镜级与句级情绪——点名了哪个才摘哪个，否定写法与台词文本不算；微动尾句与兜底句不再说「呼吸起伏」，守卫 `TestPerformanceFloor`）

  **必过** `test_prompts`
  - **引擎自产提示词不含多镜语法**：判据借 `variation.MULTISHOT_RE`（与 lint `multishot_syntax`
    同一条），覆盖 `timeline_unit` × 语种 × 附板/不附板矩阵——`_lint_multishot` 只扫作者写的
    `video_prompt`，装配后的那条在这里扫
  - 带图只写增量
  - **绝不回退整条 image_prompt**
  - dubbed 说「参考图」不说「首帧」
  - FLF2V 放开构图不与「收束末帧」打架
  - en 提示词不掺中文连接词
  - 地板顺序=作者 negative 在前
  - **`TestContractLocksIdentityNotComposition`**：四支契约句**与两档兜底句一并**反向断言「构图保持一致/构图不变/composition unchanged」
    不许复活、必须显式把构图交给本镜运镜——只改契约句而漏掉 `DELTA_FALLBACK` 等于把矛盾从首句搬到正文
  - **`TestMicroMotionTail`**：写了运动设计才追加
  - **落兜底句的镜必不追加**（唯一挡得住错误注入点的断言——文档声称守着等价关系的 `test_delta_missing_flag_matches_actual_fallback` 实测挡不住）
  - 链上镜不追加
  - 作者自写呼吸时去重
  - auto 拆拍路径不丢句
  - camera 仍是首位 token
  - 措辞不含「保持不变」且对 `SLOP_TERMS`/`EMOTION_TERMS`/`UNFILMABLE_TERMS` 零命中
  - **`TestPunctuationSeam`**：「。，」标点缝集中修（`_zh_join` 落 sfx/角色锚/负面串/台词句四处；**必须写「不带 sfx」
    那一条**——`with_text_floor("")` 恒非空故负面串那一拼无条件执行，只补 sfx 一处会留一半）
  - **`TestNativeVoiceClause`**：对白配口型、旁白发闭唇句（语态判据走 `voicecast.voice_kind` 单一真源，
    与 lint 语态维度同一个）
  - 泛称「角色说：」不许复活
  - 情绪动词确定性查表且键覆盖 schema 的英文情绪档
  - **`TestBeatSoundReachesTheModel`**：逐拍 `sound` 在场时镜级 `sfx` 让位（同一套声音设计不发两遍）
  - **`TestStructuralLock`**：只发一句
  - V2V/ref_mode/板三支零注入
  - `wipe` 预设镜零注入（判据从 `CAMERA_PRESETS` 派生，不留第二份词表）
  - 作者已自写「一镜到底/no cuts」时去重
  - 措辞不含「一镜到底/首帧/末帧/参考图/运镜：」
  - 肯定式不堆否定
  - 契约句仍是绝对首句

- **改动面** **Prompt 正式契约层**：`agent/contracts.json` 机器真源 · `agent_assets` 契约校验/运行时 JSON/作者参考三件生成 ·
  `prompt_contract.py` 的 `PromptSpec`/`PromptEnvelope`/稳定摘要/profile 能力 revision · `prompts.PromptCompiler` ·
  `cli` 图像/视频计划、dry-run、provider 与 `shots[].gen.*.envelope` 留痕

  **必过** `test_prompt_contract`
  - 未知槽拒绝
  - 投影确定性
  - 现有作者字段回投
  - fingerprint 不含时间且覆盖 references/revisions
  - provider 长度上限超限前置拒绝
  - provider prompt 与 envelope.prompt 逐字同源

  **并过** `test_agent_system`（源码/运行时契约字节语义一致·生成物漂移拒绝）

  **并过** `test_consistency`/`test_sketchboard`（图像/视频快照 prompt=fingerprint Envelope）

- **改动面** **图侧剧情画面三件套**：`prompts.character_anchor_block`/`CHAR_BLOCK_BUDGET`（角色文字锚按镜装配）
  · `STORY_FRAME_*` 单帧剧情契约（**含 `_NOCAST` 无人变体**——完整版「人物有具体动作与情绪神态」
  对空镜是「请画个人」的邀请函，实测空镜被塞进古装女主）· `REF_BASE_*` 设定图参考契约（**含「多视图=同一对象·
  每个具名角色至多出现一次」条款**——实测设定图三视图被实例化成两个姜栀）· `SHEET_FLOOR_*`/`with_sheet_floor` 防设定表地板 ·
  `project.shot_cast`（出场角色三级解析）· `cli.stage_gen_image` 逐镜接线

  **必过** `test_prompts`
  - **整块灌全员外貌清单的旧路径不许复活**（源级：`stage_gen_image` 必须 `shot_cast`+`character_anchor_block`+`ref_base=`+`cast_empty=`、
    绝不 `style.get("character_block")`）
  - 设定图在场角色只留绑定句不复述外貌
  - 全员兜底块超预算弃锚且显式点名永不被裁
  - 空表 `[]`=明确无人绝不回落全员
  - 剧情契约排位在风格前缀后角色锚前
  - **空镜换 NOCAST 变体且无「情绪神态」**
  - REF_BASE 只随真附设定图注入且必含「未出现不画」与「至多出现一次」
  - 防设定表地板排作者 negative 与防字地板之后
  - text_floor=False 三件一并关（HUD/信息图画风）

- **改动面** **`sheets.py`（设定图规格与版式规则单一真源：`aspect_for` 比例 + `prefix_for` 开头声明
  + **场景俯视图规格** `topview_rules`/`_topview_base`/`_topview_space_only`/
  `topview_ref_role`/`scene_topview_prompt`
  （正交掀顶平面制图；**只画空间本身**——机位/视野锥/轴线/站位/走位路线逐类显式否掉；
  **画风跟项目、观察方式不跟**——`prefix_for` 在画风前缀后补
  `topview_style_clause`+`TOPVIEW_MEDIUM` 两句）
  + **扩展设定图两类规格** `expression/pose_rules`+`*_sheet_prompt`（表情 4×3/动作 5×3；
  `_grid_fill` 消费 `required_emotions`/`required_actions`——这两个字段的第二个引擎消费点）
  + `rules_for` 给局部改造回喂 + **`templates_for`/`template_role` 版式蓝图（每类**单张简笔线稿**：
  角色 `char_template.png` 16:9、道具 `prop_template.png` 1:1，**按 `PROP_LAYOUTS` 一式一张**，
  `assets/blueprints/`——每类只此单张，不设多示例灰模；线稿只有版面骨架，
  无脸无发无服装无文字；设定图两侧**都是纯图片**，产出零文字层
  （`test_sheets` 钉 assertNotIn 防复活））**）**· `cli._char_sheet_prompt`（角色三区两视铁律，
  现为 sheets 的别名）· `refine.refine_asset` 消费同一份（**三条路径产同一规格的图**：project refs / refine 局改 / 灯箱重生；
  若 refine 自己写死比例，16:9 横版角色图会出成方图，版式规则也不回喂）

  **必过** `test_adapt`
  - **`TestSheetTemplates`**：只有带分区版式的类型才有样板（**场景刻意没有**——单幅环境 key art 无分区可教，
    垫具体空间只会污染陈设光线）
  - 样板随包分发且 assets 有 README 讲出处与再生路径（搬进 `.claude/skills/` 会让 pip 用户静默失效）
  - 职责声明与实附样板逐字一致
  - 源级样板垫在 moodboard **之前**；`TestSheetSpecSingleSource`：角色 16:9
  - 道具 1:1
  - 场景跟随项目比例
  - refine 必须 `rules_for`+`aspect_for`+画风前缀且不许再写死比例
  - `rules_for` 回喂含三区宽度比/不持握/中正右背且武器名不进；道具回喂含无色板禁令
  - **两类职责声明不得共用一套文案**（角色提共同骨架、道具提槽位纪律并择一执行；
    共用会在角色提示词里讲道具的版面，并把角色那一路的「提取共同骨架」改弱）
  - **无意见重生降级到 `refs --force`** 而非拒绝；**`TestExtensionSheets`**：扩展两类与俯视图恒 16:9 无样板
  - `required_*` 优先入格且去重
  - 版本栈键接线（`expression_sheet`/`pose_sheet`/`topview_sheet`/`scene_topview_ref` 各配独立 versions 键）
  - 恒直出不走候选宫格
  - **重生/改造不触发血缘传播**（不进每镜挂载，无下游可作废）
  - mock 端到端出图回填

  **并过** `test_adapt.TestSceneTopview`（**场景俯视图 = 场景基准图的另一半**，六条契约）
  - **配对出图**：裸 `project refs` 就出，**没有关闭开关**（`--no-topview` 已下线——
    只有基准图的场景，视频请求拿到的空间证据缺一半且缺了没有任何征兆，
    缺口改由 lint 的 `topview_missing` 点名）；
    存量项目重跑只补缺的图纸、不重付基准图的钱；`--only scene[:名] --force` 连带重出图纸
    （重出基准图却留着按旧空间画的平面图 = 视频请求同时挂两份互相矛盾的空间证据）
  - **第二波才排计划**（`cmd_gen_refs` 由此变成两波各两段提交，`test_workspace`/`test_parallel` 同步钉）：
    俯视图以基准图为空间取材，第一波定计划时基准图还没落盘——排进同一批并发＝新建项目的图纸统统
    拿不到参考、各画各的空间
  - **基准图不在盘就不排计划**（`test_candidates_defer_…` / `test_layout_is_backfilled_…` /
    `test_force_without_a_sheet_keeps_…`）：`--candidates N` 下基准图停在 `sheet_candidates`，
    此时画出来的图纸与最终定稿交代的不是同一个空间，而它一落盘就占住 `topview_sheet`——
    跳过判据只看文件在不在，这一对图再没机会对齐。故本轮不画、报出待补的场景名，定稿后
    重跑补出。**就绪判定必须排在 `archive_asset_sheet` 之前**：归档是移动文件且标准字段路径
    不变，顺序写反 = 旧图纸被移进版本栈、字段指向空路径，重生却没发生
  - **`--only kind:名` 的名字过滤对无名档同样成立**（`test_only_named_asset_leaves_the_fixed_scene_alone`
    / `test_bare_only_still_covers_the_fixed_scene`）：全局固定场景与它的俯视图传 `name=None`，
    放过无名档 = 点名重生一个取景地时连带重出并归档全片视觉基线那一张（Studio 灯箱「↻ 重新生成」
    正走这条命令）。判据留在 `_want` 一处，不在各调用点各补一次
  - **进视频不进分镜图，且每镜至多一张**：`_video_sheet_refs` 里主场景那张图纸紧跟它自己的基准图
    （哪一张由 `lineage.primary_layout_ref` 定：镜内书写序 > 命中序 > 全局固定场景，不回落）；
    `design_refs` 不含它——8 张参考位挤掉一张角色设定图换一张平面图，是拿身份一致性换空间提示
  - **不并进 `required_refs`**：那份清单的两个下游都按「分镜图真用了它」立论（`readiness` 报缺图、
    `rebaseline` 记血缘基线），混进去＝存量项目每镜报「设定图不齐」＋改图纸被判成分镜图过期
  - **画风跟项目、观察方式不跟**（`test_layout_takes_the_project_art_style_but_not_its_render_words`）：
    两个极端都试过——整条画风前缀不取（改用固定的「扁平矢量制图」声明）实测三张全部长成
    与项目无关的纸纹水彩，两张图并排不像一家；整条照收又会拿渲染词（PBR/三点布光/浅景深）
    去画平面图，产出三维鸟瞰渲染。故画风照收 + 紧跟一句把适用范围钉死在「线条质感、
    上色方式与配色语言」（同 `template_role` 制度：参考照给、取什么不取什么另说）。
    **`topview_ref_role` 是第二处根因**：把基准图的画风一并否掉，两图就不成套，
    故口径是「两取一不取」——取空间内容、取画风配色，唯独不取视角
  - **不吃风格垫图**（生成侧 / `refine` / `set_asset_refs` 三处同判据），
    `sheet_binding_clause` 的 `scene_top` 职责句必含「绝不改成俯视视角」与「不影响画风」
    ——少了这半句，模型会照图纸出一段俯拍
  - **图纸只画空间，拍摄与调度信息一概不画**（`test_layout_draws_space_only` /
    `test_layout_rules_have_no_variant`）：机位、视野锥、动作轴线、人物站位与走位路线
    都是「这一场戏」的属性，而取景地跨场次复用——画进场景级图纸＝给所有用到它的镜头
    强加同一套调度，第二场戏起视频模型照着排机位就是错的。**逐类显式否掉**：只说
    「只画空间」压不住制图惯例，模型会照平面图的常见样式自行补上相机图标与动线箭头。
    `rules_for` 因此不按 holder 分档——图纸只有一种形态，三条生成路径拿到同一份版式

  **并过** `test_sheets`（契约正文与蓝图对齐，与 `test_adapt` 的分发接线分家）
  - **蓝图画布比例必须与 `aspect_for` 同源**（钉比例不钉像素——重做蓝图换分辨率是常规操作）
  - **`PROP_LAYOUTS` 条数 ↔ prop 蓝图张数**（一式一张；少一张那式没图例、
    `template_role` 的槽位声明当场变成假话）
  - **角色三区两视版式钉住**：三区宽度比/肖像取景/中正右背/只靠留白不画栏框；
    不设侧视、细节格与色板槽位（钉 assertNotIn 防复活），负面禁令显式在
  - **全身像不得继承蓝图的灰模/线稿属性**（蓝图词汇泄进成品契约，立像会被渲成素模）
  - **`visual_requirements` 必须进定稿表**（`test_required_visual_traits_reach_the_defining_sheet`）：
    分镜侧逐镜注入「必须保留的视觉特征」，而定义外观的那张表若收不到，下游每一镜
    都在保留一个设定图上不存在的特征。空白项不许拼出空段
  - **头身比只在两个全身像之间可比**：肖像取景到锁骨，没有身可比——写成
    「肖像与两个全身视图之间头身比…一致」是空转的一维，还把注意力从真正该管它的
    两个立像上分走
  - **腿长由分段高度钉，不由头身比钉**（`figure_proportions` 单一真源，角色定稿表
    与动作表共用）：只给头身比时模型按训练集均值落笔——头身比 7.7 是对的，胯线却
    落在全高 0.56~0.59、腿长只剩四成。契约按转面表逐段给出可数高度（头顶到下巴
    1 头…脐到胯线 1 头…胯线到膝 2 头…膝到脚底 2 头），并附禁形（绝不把胯线画到
    全高一半以下、绝不画成躯干长腿短的体型）。**排在版式总纲之后的第一条**：
    写实档的角色设定图走纯文生图、一张参考都不挂，这段文字是那一档唯一的比例
    通道（`photoreal-face.md`），埋在二十几条版式规则之后就没有权重。
    非写实档另有 `template_role` 放行「立像水平定位线高度」——人台按八头身画、
    胯线落在正中或略高，是这一维的现成图例；脸型/五官/发型/性别/年龄感/体型胖瘦全禁
  - 道具单一版式=**结构三视一式**（上 2/3 三视等高等大并列、下 1/3 一排三个细节框，
    无色板槽位；「两式」「转台式」等称呼不得残留）；完整入画是硬约束、摆正只约束定位视图
  - **拼装口单一**：六个提示词构建器与 `refine_asset` 共用 `sheets.join_prompt`，
    分隔符只由它补——条目自带句尾逗号会叠成「，，」、作者字段只含空白会拼出孤立空段，
    两者都不报错也不被版式断言看见，只稀释相邻硬约束的权重
  - 角色/道具/场景/扩展四类一律零文字层；名称只作对象识别词、不以「名称：」数据行形态出现

  **并过** `test_jobs`
  - regen_asset 双模式 fresh/refine
  - **批注在任务成功后才按 id 消费**：提交不清空（refine 失败时 argv 不落盘、任务表随 Studio 重启即失，
    提交即清=永久丢失）、任务期间新提的意见不受波及
  - 具名取景地意见挂 `scenes[]` 条目与全局池互不串、名字打错报错不落全局

- **改动面** `cli.py` `_char_sheet_prompt`（角色设定图【三区两视铁律】提示词单一真源，现为
  `sheets.char_sheet_prompt` 的别名·**武器与随身物件不上角色表**——归独立的武器/道具设定图，
  **全身像无条件双手空手**（不持握、不佩戴、不背挂；不设细节格槽位，武器名没有任何合法落点）
  ·**`silhouette_notes` 进/`constraints` 永不进**
  ·**肖像与两个全身像一律中性表情嘴唇轻闭**（表情戏属于分镜 emotion）
  ·不设色板与细节格槽位，负面禁令显式在（模型照设定集惯例会自己补）
  ）

  **必过** `test_adapt`
  - `TestCharSheetPrompt`：武器名不进设定图/外貌进
  - 全身「不持握」无条件成立且含「走各自独立的设定图」句
  - 剪影段紧随外貌且不动空手铁律
  - **`test_portrait_is_neutral_expression`**
  - **`test_constraints_not_in_sheet_prompt`**
  - 端到端项目其他道具不塞进角色设定图

- **改动面** `characters[]` 角色清单五字段（`required_emotions`/`required_actions`/`required_views`/`silhouette_notes`/`constraints`）
  · `workspace.sync_design_to_chapters` 的 `char_fields` 白名单（**新增设定字段必须登记，否则存量章节静默失效**）
  · `workspace.upsert_entities` 的同名变量（**语义相反·人工字段绝不登记**）

  **必过** `test_adapt`
  - `TestCharRoster`：存量章节经 sync 拿到五字段且系列→章节单向覆盖
  - 新建章节整份拷贝继承且非共享引用
  - `upsert_entities` 重抽不覆盖五字段

  **并过** `test_schema_contract`（五字段登记 DECLARED_FIELDS 且不得标 `[engine-managed]`）

## 4. 音画同步与音频链

音画一旦错位，整条片子从错位点往后全废——这组是全仓最不能碰坏的一段。

- **改动面** **音画同步生命线**：`voicecast.narration_parts`（旁白轨拼接序列**单一真源**，`cli.stage_tts` 与 `compose._sync_narration` 共用；**窗口分支=native 混烧与 dubbed 主音轨共用**：
  窗口按 dur（片段实测秒数）铺、配音短则垫齐、超窗变速压入——kenburns 的 dur 本就等于 wav 长，
  只有这两种模式窗口与 wav 可分离；**缺省不烧的 native** 下未配音的台词镜按窗口占静音且不进 missing——混烧（`native_voiceover`）则相反，旁白镜的人声就是那条 wav，缺了与 kenburns/dubbed 同属错误态、走同一条点名出口，守卫 `test_delivery.TestNarrationParts.test_a_burn_shot_without_audio_is_named_not_silently_skipped`）· `shot_pauses`/`shot_duration`（写侧停顿 motion 门控 + dur 幂等折算；kenburns 尾留白地板 `TAIL_ROLL` 与拼轨淡出 `TAIL_FADE` 同受门控，守卫 `test_delivery.TestTailTreatment`；provider 回吐音频落盘即归一 PCM（`ffmpeg.to_pcm`，无 Xing 头 mp3 的估算时长每镜多一帧），守卫 `test_delivery.TestProviderAudioIsPcm`；
  **native 下 stage_tts 绝不回填 dur**——那是 Seedance 计费/片段实测秒数；**dubbed 的回填带「片段已在盘」豁免**——片段一出，dur 真源移交片段实测，
  换音色 `tts --force` 若按配音覆写，assemble 会把每镜视频尾部裁掉，守卫 `test_delivery.TestDurIdempotent.test_tts_backfill_yields_to_clip_measured_dur`；
  **gen-video 回填的是买下的整秒**（`prov.billable_seconds`，与 dry-run 报价同一条）——厂商产物的容器恒比请求多约一帧，
  按实测回填会让 dubbed 的 ceil 逐轮进位多买一秒；版本回滚只对 audio 重探时长，片段回滚不动 dur，
  守卫 `test_delivery.TestClipDurWritebackIsWhatWeBought`）
  · **`dubbed_sync_offset`（对白镜开口对齐单一真源）**：参考媒体模式下模型把开口安排在动作设计允许的时点（实测嘴比窗口起点晚整秒），
  wav 钉在窗口起点烧录即声先于嘴；平移量=底片声轨首个语音段起点−wav 语音起点，钳在 [−wav 头部静音, 窗口−配音长]。
  烧录（`narration_parts` 的 silence/cut 垫裁）与字幕落点（`compose.speech_spans_resolver`）**必须共用本函数**，各算一份即声画字分家；
  `_sync_narration` 对 dubbed/native 两档恒重拼（判据 `project.uses_seedance`）：这两档的窗口贡献恒等于 dur、与逐镜 wav 可分离，
  盘上那条轨可以与时间轴等长而内容早已不符（dubbed 是 retake 换底片后的旧对齐垫片，native 是旧制式烧进去的对白 TTS）；
  重拼后一条 segment 都没有＝这条轨与当前分镜无关，返回 None 不烧。
  **残差点名单源 `dubbed_sync_report`/`dubbed_sync_note`**：钳制没吸收掉的开口差（>0.3s）与口型/台词净长失配（>三成，
  模型没把整句演完——平移到哪都对不上，只能 lipsync 或 retake）都逐镜点名，烧录告警与 lipsync 跳过提示共用同一份判词。
  守卫 `test_delivery.TestDubbedMouthSync`（九面：垫头/裁头/窗口钳制/旁白镜恒原位/字幕同源 pin/concat cut 段 atrim+asetpts 归零/
  残差判词阈值/小差静默/两处点名出口 pin）· **`request_seconds`/`shot_audio_path`（读侧请求秒数单一真源，
  `cli.stage_gen_video` 的 dry-run 报价与真发共用）**· `delivery_instruction`（表现力编译）
  · **`voicecast.line_text`（「有台词的段」判据单一真源：`shot_lines` 过滤与 `cli.stage_tts` 的 `lines[].dur` 回填共用，
  认 `text`/`narration` 两种键）** · **`shot_audio_path` 候选链（`narration_parts` 同用：audio_file 在盘用它、
  否则回落约定路径——已上云的 URL 字段绝不当缺失）** · `workspace.create_chapter` 的 `voice_performance` 继承 ·
  Studio 镜头表「语音指令」格（`app.js` delivery 编译）

  **必过** `test_delivery`
  - **两处同一序列**（patch `compose.concat_audio` 断言与 `narration_parts` 逐项相等，停顿不被抹）
  - **dur 幂等**（连跑五次 tts 不叠加）
  - **dubbed/native 停顿恒 0**（对 `seedance.billable_seconds` 实测 6s vs 7s = 每镜 1 元）
  - **读侧对称闸**（kenburns 折算过的 dur 进 dubbed 必须回到纯配音秒数 4s 而非 7s·native 切换同理·
    无 wav 时 dur 原样·连跑五次不缩短·无停顿项目口径一字不改·`stage_gen_video` 内 `request_seconds` 恰好出现 2 次且无裸取 dur 残留）
  - **dubbed 设计窗口权威**（场→镜长镜：dur 是表演窗口、台词只占一段——配音实测
    不许把窗口拉回台词长度，配音超窗才撑大；写侧 `stage_tts` 回填只延不缩；
    发送侧配音短于请求秒数时垫静音尾到窗口（`_win{n}s.wav` 按窗口秒数命名幂等、
    wav 变新自动重垫）——参考媒体模式下片段时长跟随音频，不垫窗口就被拉回台词
    长度。守卫 `test_dubbed_design_window_survives_short_voice` /
    `test_tts_backfill_never_shrinks_a_dubbed_window` / `TestRefAudioWindowPadding`）
  - NaN/Inf 守卫
  - timestamps 段含停顿且首尾接续 Σdur
  - **narration 原文一字不改**
  - 官方音色不发 instruction
  - 建章拷贝 `voice_performance` 且 `sync_design_to_chapters` 不回灌
  - **app.js 全文件禁 `.length && h(`**（`h()` 只跳过 null/false，数字 0 会渲染成文本「0」
    ）+ emphasis 字符串切分前后端同口径
  - **`TestStageTtsEndToEnd`**：缺 wav 的台词镜让收尾**点名拒拼**（绝不写出缺镜的短轨覆盖 narration.wav，
    已合成镜的产物与费用照常登记）
  - `lines[].dur` 回填对 `narration` 键的句同样生效（判据分叉的形态最隐蔽：音轨全对、字幕换人时间全错）
  - **缺配音恒点名**（`_sync_narration` 的 missing 告警不按时长偏差门控：漏配音的旁白镜由静音占满窗口、总时长分毫不差，按偏差门控就成了静默烧一条有洞的轨）
  - **URL 字段回落约定路径不进 missing**

  **并过** `test_voice_anchor.TestCastGateOrdering` / `TestSeriesLookupHonoursWorkspace`
  / `TestScannerAnchorScope` / `TestPreviewWarmSameCastPlan`
  （**选角闸必须排在任何一次 PromptEnvelope 编译之前**：档案决定提示词里有没有音色绑定句，
  排在编译之后，预览、Studio 审阅锁与实发各拿一份稿——审阅锁按 `_prompt_sha` 比对，
  通过过的镜会因 sha 变化被判「实发稿与审阅版不一致」而整镜跳过。dry-run 与真发过同一道闸，
  `--no-auto-cast` 是唯一跳过口。页面锚定 chip 的闸 = `voice_anchor_plan` ×
  `scanner.anchor_ref_task`（任务型态）× provider 参考音位——衔接/锚定/previz 镜
  与无参考音位的 provider 不标；试听端点按盘上选角解析编号。**系列文档按章节文件
  路径反推**，不走 `Workspace.open(None)` 的发现逻辑——`--workspace` 指定的目录会被
  重新发现解析成另一个根，把音色写进同名的另一个项目）

  **并过** `test_voicebank.TestAssignVoice` / `TestCastCustom` / `TestCastGate`
  （**缺省选角是定制**：`cast_custom` 把声线描述写进实体 `voice_prompt`、生成一条演绎、
  立档启用，`character add/set --voice-prompt` 与 `voice custom --adopt N` 同一实现；
  `uncast_owners` 按音色引用判——显式别名算选角、profile 缺省不算、只出现在台词里的
  说话人一并列出。`_cast_gate` 在 tts / gen-video / score / run 花钱前逐个点名并给出命令，
  mock 与 `--project <文件>` 直渲染放行。
  **模版指派只有一个出口** `assign_voice`：写实体槽位 + 同步已建章节 + 预热锚定音。
  三件事分家的后果实测过——`character add/set --voice` 只写 `characters[].voice` 时，
  项目页选角面板拿不到任何可试听的样本（`casts` 空、锚定音也没落盘），章节页却已按
  同一把声音标了「♪ 音色锚定」，同一件事两处各有一份事实。`bank_view` 因此要下发
  `anchor`——直接指派不建档案，可试听的样本只能走锚定音缓存）

- **改动面** `voicecast.burn_muted` / `prompts.native_voice_clause(mute=)` /
  `voice_anchor_clause` / `cli._anchor_plan_for` / `scanner._va_view` /
  `voicecast.narration_parts` / `compose._gate_native_double_voice`
  （**混烧声源按镜分治**：旁白/无词镜闭声出演、TTS 上主轨——模型出声时成片
  同一段两个人声，背景床只降 -8 dB、闪避只在我们的音轨出声时触发、锚定是声区
  跟随非复刻，三条同时成立；对白镜由模型发声、锚定照常附发——闭声稿执行无
  确定性，且对白烧录的两条时间轴不同源、平移救不了口型。`burn_muted` 是唯一
  判据，提示词编译、锚定附发、页面标注、旁白轨拼接四处共用。**@配音N 位序
  寻址**：逐句说话人挂 `@配音{no}` 标记、多音频绑定句按同一编号点名，与实附
  reference_audio 同源；无台词镜恒发无人声地板——不发时提示词里没有任何东西
  拦着模型自配人声）

  **必过** `test_voice_anchor.TestNativeBurnMute` / `TestBurnStagePreview`
  - mute 只落在旁白/无词镜（旁白镜闭唇且原文不进提示词）；对白镜即便被标了
    mute 也按发声编译、绑定句与 @配音 照发（编译端 `burn_muted` 兜底）
  - 混烧章的对白镜锚定照常附发、正文照常要求对口型（`_anchor_plan_for` 与
    页面标注同判据）
  - 逐句 @配音N 标记与多音频绑定句编号同源（措辞未做付费小样，首发多参考音须补对照）
  - 正文点名的 @配音N 集合 ⊆ 实附编号集合（点名一条不存在的参考音，模型会去找
    一个不存在的东西）
  - 零设定集章节（RefPlan=None）的预览仍下发 @图片1 映射（`no=1, kind=frame`）：
    参考任务恒附本镜画面且契约句写着 @图片1，映射缺位时页面把它渲染成不可点的
    失效记号（`test_bare_chapter_still_maps_image_one`）

  **并过** `test_mix.TestBurnDoubleVoiceGate`（混烧前扫描**旁白镜**的
  `gen.clip.envelope.positive`：正文缺 `prompts.positive_is_voiceless` 认的闭声
  记号即**拒合成**——双人声成片没有可交付的形态，报错带两条出路。白名单方向，
  认不出的稿一律拦：出声措辞里的 `@配音N` 位序标记会把措辞切开，按措辞枚举判
  在「旁白已选角」这条默认路径上恒放行。对白镜的出声稿是正稿不进闸；没有生成
  快照的片段查无实据不拦。守卫的快照正文由 `PromptCompiler.video` 真发编译产出，
  带旁白锚定的开口稿必须被拦）
  ＋ `test_delivery`（`narration_parts` 对 native 对白镜恒插等长静音——盘上有
  wav 也不接入；`stage_tts` 在 native 下不给对白镜合成）
  ＋ `test_mix.test_bed_suppression_is_gated_to_voiceover_windows`（混烧床压制
  按旁白镜窗口门控：降电平与让路 EQ 同窗 enable，对白镜窗口原电平直通——
  整轨静态压制会把对白模型人声压低 8dB 还挖中频；premix `bed_eq=False` 防
  窗口内双重挖频；sidechain 整轨保留，对白窗口旁白静音天然不触发）
  ＋ `test_variation.TestBurnMixedNarrationLint`（`burn_mixed_narration` warn：
  对白镜夹带旁白句会由模型代声、与烧录旁白不同源）
  ＋ `test_variation.TestDubbedDialogueLint`（`dubbed_dialogue` warn：dubbed 章的
  对白上镜——烧录轨与口型两条时间轴不同源，dubbed 领地是全旁白解说章）
  ＋ `test_variation.TestNarrationOverrun`（`narration_overrun` warn：进旁白轨的镜按在用
  档案 `speech_rate` 预估 > `dur × FIT_TEMPO_WARN`；无语速不估；模型自声对白镜不估，
  同一镜切到 dubbed 即估）
  ＋ `test_review`（`summary(audio_of=)`：audio 只数有旁白 wav 的镜——native 混烧对白镜
  与 scored 章不挂永远关不掉的待办；`voicecast.has_audio_stage` 是 CLI 看板与 Gateway
  审阅统计共用的判据）
  ＋ `test_variation.TestEmptyShotCast`（`empty_shot_cast` info：画面写「无人/空镜」
  而镜级 `characters` 键缺失＝全员兜底注入设定图与绑定句，与画面声明打架——
  显式 `characters: []` 才是「明确无人」；「无人机」负断言排除）
  ＋ `test_variation.TestMontageChop`（`montage_chop` warn：dubbed/native 下短镜
  （<6s）占比 >60% 即碎切——生成式镜间恒硬切，短镜密集=截断感逐镜累积；
  kenburns 与 ≤3 镜样本不判。缺省镜长纪律的 Skill 侧真源在
  `video-prompting.md` 第七节与 `storyboard.md`《切分原则》：主戏镜 8~12s
  节拍串、场→镜设计、总时长自下而上推导）
  ＋ `test_variation.TestCaptionVoiceless`（`caption_voiceless` warn：dubbed/native
  下片内既有人声镜又有挂 caption 的无声镜——字幕在这两个模式里是台词轨，
  无声镜的 caption 会被读成漏了配音；整片无人声与 kenburns 不判）
  ＋ `test_voice_anchor.TestVoiceAnchorLintBurnScope`（`voice_anchor_gap` 按镜
  分治——闭声镜的说话人不进判据，混烧章的对白镜未选角照样催）
  ＋ `test_voice_anchor.TestMuteVoiceFloor`（闭声镜负面串带人声地板
  `MUTE_VOICE_FLOOR_*`：正向「不发声」指令实测仍漏出 ~0.3s 哼声级残留；
  `video_prompt` 与 Envelope.negative 同源两处，作者写过「人声」不重复注入；
  对白镜恒不落地板——发声稿上压人声地板即自相矛盾）
  ＋ `test_mix.TestBurnResidualVoiceProbe`（混烧前对**旁白镜片段**做输出侧人声
  探测——闭声稿执行实测无确定性（同稿两发一守一破，破的那次整句念出），提示词层
  的闸拦不住临场出的那段声；振幅判据分不清人声与音效，只点名试听不拦合成；
  对白镜的人声由模型承担，不探测）
  ＋ 真发基线：旁白/无台词镜闭声成立（成片 vs 旁白轨包络相关 0.985、语音间隙
  残余 -32 dB），对白镜闭声两发一守一破——硬闸/地板/血缘锁定分支均经付费实测

- **改动面** `kenburns.fit_clip(audio_edge=)` / `compose.NATIVE_AUDIO_EDGE` 与片段
  缓存键 `_ae` 分量（**native 片段音频边缘平滑**：一镜一片各自带环境音，硬切处
  环境床是硬台阶——keep_audio 下头尾各淡 0.15s 抹平，只动音频不动画面；短镜钳半。
  参数不进缓存键的话旧硬台阶片段会被静默复用）

  **必过** `test_mix.TestNativeAudioEdge`（afade 双向在场·dur/2 钳制·compose 传参
  与 `_ae` 键分量源级钉死）

- **改动面** `pipeline/subtitle.py` `strip_voice_tags`/`pick_texts`（字幕文本清洗：`<cot …>` 等语音标签只脱标签留内容）

  **必过** `test_subtitle`
  - `test_voice_tags_stripped_from_subtitle`：中英双语位都剥
  - narration 原文不动
  - `体温<36` 不误伤

  **并过** `test_subtitle.TestSpeechSyncedEvents` / `TestSpeechSpanContract` /
  `TestSpeechWindows` / `TestRankingFollowsSpeech` ＋ `pipeline/speech.py`
  （**字幕落点跟随主音轨里实测的有声段落**）
  - native 的人声由视频模型自己念，`dur` 是计费秒数、`lines[].dur` 只在跑过 TTS 后才有值
  - 探测源与档位随主音轨来源：dubbed 探逐镜 TTS wav（`clean` 档，峰下探 25 dB——
    干净语音峰均差大，模型片段档的 8 dB 会把语句体整段判成静音）；native 探片段音轨
  - `speech_windows` **全量返回不裁段、也不回报对位资格**（不收句数）：段界是能量
    边界、与句界无因果关系，调用方拿它判「这镜有没有出声」并在回落时收整体首尾
    （单句镜同样收——裁段会把停顿后的半句留在字幕窗口之外）
    ——两者都答不出「这一镜第几秒才开口」。没有这个事实，字幕从第一帧铺到最后一帧，
    一镜五秒的片子里提前量能有三四秒
  - 探测是**人声频带 + 相对峰值阈值**两层：不滤频带时引擎轰鸣与爆燃把整条音轨顶在阈值之上
    （等于没检测）；写死 dB 值则在另一档响度的镜上全中或全不中
  - **探测不出一律回落**（无音轨/ffmpeg 不可用/整段过响）：宁可提前出字幕，
    也不能因为探测失败就不出字幕；`spans=None` 时逐字节保持改造前行为
  - **逐句落点只认语义划界**：原生声源的多句镜恒请 `asr.line_windows` 按句文本对齐，
    划不了就收整体首尾，绝不按振幅段逐句对号入座（错位比铺满窗口错得更离谱）
  - **可读性下限**：实测有声段是「说话」时长不是「读字」时长，短句按 `MIN_EVENT_SEC`
    与阅读速率补足，且不越出镜窗口
  - **补足必须单调**：逐句以「上一句终点」为下界、「下一段起点」为上界。各补各的时，
    两条挨得近的短句会各自撑到可读下限而互相压过去，同屏出现两条字幕——正是
    `shot_events` 逐句拆分本要消除的形态
  - **对应关系要有依据**：振幅段数等于句数是巧合不是证据。两段可以全落在第二句
    内部（第一句因音量低整句检不出）而段数照样等于句数，据此对号入座会把 3.4s 的
    句子压成 1.26s。故 `compose.speech_spans_resolver` 对原生声源多句镜
    恒走 ASR，`len(spans)==len(norm)` 单独不构成证据
    （守卫 `test_asr.TestComposeAsrFallback`，四条：语义优先/划不了收首尾/
    烧录镜不问 ASR（`lines[].dur` 是确定性真源）/单句镜不问 ASR）
  - **ranking 版式分两层**：说明文本（`RText`）跟语音，徽章/序号/标题是常驻叠加层、
    恒铺满整镜。参数收下却不用是更坏的一种——签名在说谎且无任何信号

- **改动面** `deliver.build_srt`/`build_delivery` 交付外挂字幕 · `pipeline/subtitle.sub_cfg`（字幕样式/语言判据单一真源，
  cli `_sub_cfg` 是它的别名）· `cli.cmd_deliver` 与 `studio/actions.export_artifact` 的 lang 接线

  **必过** `test_deliver`
  - **`test_same_source_as_burned`：真渲一份烧录 ASS 与 SRT 逐条比对事件数/时间码/文本**——SRT 自己调文本函数再 assertIn 等于自证，
    烧录侧换真源它不会红
  - 多角色镜 `lines[]` 逐句成 cue 且按各句 dur 切窗
  - subtitle 块 lang 覆盖顶层（只认顶层=成片烧英文外挂却中文）
  - narration>caption 音字一致
  - 空镜不产 cue 序号连续

- **改动面** `pipeline/compose.py` 音频段（ducking/让路EQ/末级响度/削波）· `SOUND_LEAD` 转场音效提前量

  **必过** `test_mix`
  - 旁白+BGM 才 ducking
  - native 无 BGM 无闪避
  - 环境音不被压
  - 末级限幅在位
  - edge=0 的 wipe/scan 也有提前量
  - **音频改动绝不进 `kenburns.fit_clip`**：片段缓存键不含音频参数会静默复用旧音轨
  - **dubbed 主音轨=逐镜 TTS**（`use_clip_audio = project.native_audio`）：Seedance
    参考媒体是重演不是嵌入，片段人声嗓音逐镜自选、不进成片；字幕落点同源改探
    逐镜 wav（`TestDubbedMainTrack`）
  - 章节封面登记锁内重读（`cli.cmd_cover` · `test_cover.TestChapterCoverWriteLock`）：
    生成期以分钟计，旧副本整份 save 会抹掉 tts/gen-video 并发写入的登记

- **改动面** 口型精修 `cli.stage_lipsync` / `providers/lipsync/`（dubbed 缺省档增强步：
  底片+最终配音 → 只重绘对白镜口型；clips 切 lips、clips_base 保底片）

  **必过** `test_lipsync`
  - 只修 `voice_kind == dialogue` 的镜（旁白/静音镜闭唇出片，无口型可修）
  - 幂等按源指纹（lips 比底片与 wav 都新即跳过）；换音色 = wav 变新即重算
  - 未配置（req_key/视觉密钥/OSS）点名跳过，不拦出片主链
  - volc 提交体字段与 Signature V4 头齐备；本地路径拒收并给上云指引
  - gen-video 收尾缺省自动进入（`--no-lipsync` 才关）

- **改动面** `pipeline/mixdown.py`（**混音数值单一真源**：闪避四参数/母线电平/响度目标/钳制区间/`SOUND_LEAD`/`NATIVE_BED_GAIN`/`clip_bed_track`/`InputTable`）
  · `compose.build` 音频段（**native 配音混烧接线**：`narration and project.native_audio` 才走 TTS 上主轨+原生降床）
  · `providers/music/local.py` 入轨归一

  **必过** `test_mix`
  - 闪避只在旁白+BGM 同时在场
  - 让路 EQ 必在 sidechaincompress 之前
  - native 无 BGM 无闪避
  - **native 混烧：原生音轨降 `NATIVE_BED_GAIN` 占 BGM 槽位并被闪避、**没有旁白镜要烧**时才走
  - **混烧两路人声对齐**（`TestNarrationMatch`）：旁白轨只测旁白镜窗口、片段音轨只测对白镜窗口，差值作旁白入混静态增益（钳 `NARRATION_MATCH_RANGE`）且落在 sidechain 分叉之前；只在混烧分支、整章无对白镜不测不推；kenburns/dubbed 主轨 0 dB
    clip_audio_track 直通、混烧只认 native 不认 dubbed**
  - **混烧缺整条旁白轨即拒合成**（`TestBurnMissingNarrationTrackGate`：只点名进旁白轨的镜、
    全对白章与其余 motion 不误伤、闸复用 `voicecast` 那对谓词）；
    `needs_narration_track` 是章级口径，`needs_tts` 的镜级语义不动
    （`TestNarrationTrackIsAChapterLevelQuestion`）
  - 环境音与转场音效不进侧链
  - 末级增益+限幅在最后且 `level=disabled`
  - 三模式钳制区间不同
  - **无旁白独奏档 `MASTER_SOLO`+`BGM_GAIN_SOLO=1.0` 真能推到 `LOUDNESS_I`**（纯 BGM/纯环境音章节，
    含真机端到端复测）
  - `SOUND_LEAD` 与 `TRANSITIONS.edge` 解耦（edge=0 的 wipe/scan 也有提前量）
  - loudnorm JSON 解析
  - **混音链真机冒烟渲染**

## 5. 渲染、合成与特效

渲染层的错误多数「不报错但画面不对」，故守卫大量依赖真机冒烟渲染。

- **改动面** **`audioscript.py`（音频剧本分段单一真源：`plan` 按转场镜切/段内相对秒 `spans`/`check` 超限点名/`segment_script` 段数契约/`segment_sig` 段指纹/**`draft_segment` 按分镜起草**（台词逐字复制·
  段内秒段按字数比例切·声线取材两级：在用档案原话→中性底））**· `Project.audio_mode`+`scored_audio`+`needs_tts` 门控 ·
  `cli` 的 `stage_score`/`_score_rows`/`_score_quote`/`_score_dry_run` 与 `score` 子命令 ·
  `compose.build` 的 scored 分支（三路让开）· `studio/scanner._audio_script_view` · `actions.save_audio_script`/`score_generate` ·
  chapter.js 音频剧本台

  **必过** `test_audioscript`
  - **接缝落在转场镜上**：短章不切
  - 上限落在戏中间要回退到最后一个转场之后
  - 段内无转场则拒切并让 `check` 点名补转场
  - 弃镜不进时间轴
  - **`spans` 每段从 0 重新计时**（照全片秒写时间控制会让第二段起整体偏移一整段）
  - 段数对不上宁可拒发不补齐
  - 段指纹随剧本/镜号/时长三面变化且分隔符防串字段
  - **只有精确的 `scored` 才切路线**且 `needs_tts` 恒假
  - tracks 缺省路线判据零变化
  - CLI 与 scanner 都不许自己再切一遍
  - **起草**：台词一字不改
  - 首句从段内 0 起
  - 一镜多句按字数分窗
  - 缺声线描述给可用中性底而非占位符（底稿会原样发给模型）
  - 纯画面段拒绝起草而不是产出空稿
  - 网页起草不落盘

  **并过** `test_mix`
  - **scored 三路让开**：不叠 BGM
  - 片段原生音轨让开
  - 逐镜旁白轨不参与
  - 无闪避无让路 EQ
  - 缺音轨报错而非静默出哑片
  - 盘上有 narration 不用时必须出声且绝不删文件

  **并过** `test_delivery`（DEVELOP 命令表含 `score`）

- **改动面** `pipeline/kenburns.py` 的 `SRC_SCALE`（平滑度杠杆）/`ALGO_VERSION`（进片段缓存键）· `compose._clip_cache_name`

  **必过** `test_mix`
  - `test_render_algo_version_busts_the_cache`：算法版本变则键必变
  - 图生视频片段不带运镜不受牵连
  - v1 不带后缀保存量片段名

- **改动面** `pipeline/transitions.py` / `kenburns.py` 边缘淡化 · 转场目录 `catalog()`/`sound_catalog()` ·
  **seamless 无缝柔切（注册表第一行=弹层**预选**·缺省静音·柔度档 `_DURATIONS`·`resolve_dur` 钳制·
  短过渡路径 `SHORT_MAX_FRAMES`：先 `frame_aligned` 吸附整帧再走均匀帧阶梯）**

  **必过** `test_transitions`
  - spec 归一/边缘淡化推导/底色衔接/字卡呼吸曲线
  - **catalog 键与 TRANSITIONS 锁步
  - 方向/主色/音效元数据
  - Studio 加转场 type/direction/color/sound 透传**
  - **`TestSeamless`：第一位+静音+edge0
  - 柔度档注册表
  - resolve_dur 钳制两写路径同源
  - 阶梯 filtergraph 零重复帧
  - **既有八型逐字节不变**（`test_long_xfades_untouched` 钉住通用公式且缺省段长全在 8 帧线之上 ·
    `test_card_family_is_not_frame_aligned` 字卡族与 scan 不吸附 · `test_xfade_still_requires_both_neighbours` 章首尾照旧退化字卡）**
  - **`TestNoImplicitTransitions`：没有孤岛镜就一个转场都没有——AST 判定全引擎只有 `cli.cmd_transition`、
    `actions.transition_add`、`framechain.sync_seams` 三处构造转场镜，且第三处（`test_engine_side_maker_is_recyclable_seamless_only`）
    **只准造 `AUTO_TYPE`(seamless) 且必带 `AUTO_MARK` 标记**（没标记＝引擎往用户章节里塞了个删不干净的东西）
    ；槽位提示不许写「默认「无缝转场」」（一镜一槽，这话会被读成每镜都已加上，实测投诉）**

- **改动面** `effects.py` 加特效/改 `EFFECT_META` · `models.yaml` profile.effects

  **必过** `test_effects`
  - EFFECT_META↔EFFECTS 键一致
  - catalog 元数据
  - 粒子仅星辰/萤火虫
  - **三铁律守卫**：lut 无时变表达式/阈值用 lut 不用 lutyuv/发光层非 alpha
  - **运动方向守卫**：下落物 scroll vertical<0
  - 上升物>0
  - 星辰=0（实测标定 负=下落·正=上升·0=静止）
  - **全表特效 filtergraph 冒烟渲染**（遍历 `EFFECTS`，不写死条数）
  - **set_effects 未知名当场拒绝**（静默过滤=前端 chip 与合成端两处假成功）
  - **`models.effects_for` 是合成/animatic 的唯一收口，未知名报错**
  - scanner 展示路径降级原样下发由前端标「未注册」

  **并过** `test_config_drift`
  - profile.effects 名合法
  - **skills 文档「特效（…）」括注点名的必须在注册表内**
  - **motion 归一只许 `project.normalize_motion` 一份**（源级禁再抄 + video→dubbed 兼容）

- **改动面** `project.normalize_motion`/`MOTIONS` 渲染模式真源 · 8 处 motion 散点（project 谓词/voicecast 停顿门/consistency `frame_stage`/scanner 别名/core.js MOTION 表/mixdown 钳制档/export.motion_zh/cli argparse choices）
  · **`project.effective_motion`（未表态缺省档的唯一判据，按内容定档：任一正镜有对白 native、全旁白/无词 dubbed、scored native；kenburns 须显式）**——
  `stage_gen_video` 决策点与 `stage_tts` 的 dur 回填语义共用（未表态按内容定档；kenburns 口径下片段不参与合成、请求秒数折停顿，计费与合成必须同口径；真发落盘表态、dry-run/preview 只作用于本次；显式 kenburns 拒发并给改道路径。
  tts 不走同一判据的实测形态：未表态章节按裸缺省 kenburns 回填，场→镜设计出的 58s 表演窗口在配音一步被缩成 30.5s 台词长度——而配音正是 dubbed 流程里 gen-video 之前的节点）

  **必过** `test_motion_default`
  - 未表态 dry-run：播报「按缺省 <定档结果>」+「不写章节」，报价按定档口径，文档零落盘
  - 未表态真发：定档结果落盘为 `motion`（否则 assemble 弃用片段）
  - scored 未表态：缺省 native（不得撞 scored×dubbed 硬闸）
  - 显式 kenburns 拒发（含改道路径）· 显式 native 零播报 · runtime 覆盖（`-m`）视同已表态
  - `TestTtsFollowsTheSameDefault`：判据单源 pin + 未表态章节过 tts 后设计窗口原样（12s 不缩），并播报口径

  **必过** `test_config_drift`（**别名归一只许一份**：`video`→dubbed 与退役 `cutout`/`d`→kenburns 两条兼容位·
  除 project.py 外无第二份别名表）

  **并过** `test_frontend_integrity`
  - core.js MOTION 键集 ≡ `MOTIONS`
  - 别名表 values ⊆ `MOTIONS`
  - `export.motion_zh` 键集 ≡ `MOTIONS`

- **改动面** `pipeline/watermark.py`（漂移+固定角标+**底部水印 `build_bottom_filter`**（底部居中·半透明·
  无描边·柔影·不侵入字幕底带）·`_wm_font`）· `studio/actions.set_watermark`（**三类水印的唯一写入口**·
  `burn=False` 只写盘不烧）· `actions.set_subtitle_style`（字幕样式白名单写入口·重烧走 rebuild_final 单链）
  · scanner `_watermark_view`/`_subtitle_style_view`

  **必过** `test_watermark`
  - 连续/在场/界内/随机反弹物理不变量
  - 角标四角/贴边/细描边
  - **底部水印**：居中/半透明/无描边无底衬/字号+底距不侵入横屏字幕底带
  - **内置字体在位+四链首位=内置+`_FONT_ALIAS` 系统字名归一**
  - **三态语义**：字段缺省时那一类原样留着
  - **三类一次写完只起一个重烧任务**（分多次会让多个任务抢同一批 output_wm）
  - `burn=False` 一个任务都不起
  - 三类都清则删掉水印版还原原片
  - **字幕样式**：白名单键合并/逐键回落/style=None 整组回落但不碰 lang 行为键/未知键拒绝/rebuild 必经 assemble/**字段全空时 rebuild 清残留水印版**（「清水印+改字幕」
    组合走 burn=False 只写盘，删除收口归 rebuild）/scanner 下发生效值+覆盖原文
  - **branding 三段透传**：watermark/watermark_fixed/watermark_bottom 整段过 loader（漏哪段哪段的全局默认链静默断）
    +底部预填「章节 > branding」回落且 on 只认章节

- **改动面** `fonts.py` 内置字体链/常量 · `pipeline/subtitle.py` `_FONT_ALIAS`（工程内置免费商用字体单一真源）

  **必过** `test_watermark`
  - `TestBundledFont`：字库文件在位
  - hei/song/kai/display 四链首位=内置
  - 系统字名→内置族名

- **改动面** `pipeline/cover.py`

  **必过** `test_cover`（key visual 提示词防字地板/标题安全区·排版同参同版式）

- **改动面** **封面缺口的三处点名**（封面不是自动产物，漏做无硬性后果，只靠点名暴露）：
  `cli._warn_cover_missing` 的调用点（`stage_gen_image` 正常收尾 **＋ `if not plan: return` 空计划出口**）·
  lint 维度 `cover_missing`（`variation._lint_cover_missing`，判据＝正镜图齐且章节 `cover` 空）·
  `studio/scanner` 的图源三级回落（封面 → 成片海报帧 → `_shot_thumb` 分镜图）与 `cover_missing` 标记

  **必过** `test_cover.TestCoverReminderReachesEveryExit`
  - 空计划重跑仍点名系列与本章（**这条出口漏了等于 agent 出图模式永远收不到提醒**：
    首轮抛「工单已开」到不了末尾，画完重跑正好落空计划）
  - 封面齐了就闭嘴

  **并过** `test_variation.TestCoverMissing`（图未齐不催 / 已登记不催 / 弃镜转场不挡「图齐」判定）
  ＋ `test_variation.TestTopviewMissing`（**俯视布局图缺口**：基准图在盘而图纸空即报；
  基准图还没定稿时不催；`skip_design` 静默。这个缺口没有任何征兆——不影响出图、
  不报错、不挡下一步，而视频请求每镜少了一半空间证据）
  ＋ `test_studio_routes.TestCoverFallbackChain`（**兜底图源不冒充封面**：
  `cover_missing` 与章节 `cover` 只认真封面，弃镜的图不作门面）

- **改动面** `ffmpeg.py` `run()`（渲染原语·`-loglevel error` 写死·失败抛 FFmpegError·**单次调用超时上限 `_default_timeout` 缺省 1h**）
  与 `run_capture()`（探测原语·日志级别可调·永不抛异常·超时回 rc=124）· **孤儿收割 `find_orphan_ffmpeg`/`reap_orphan_ffmpeg`**（判据双重缺一不可：
  PPID=1 × 命令行带 `_work/` 签名——绝不误杀别人的常驻 ffmpeg；studio 启动自动收割、doctor 只报告）

  **必过** `test_ffmpeg_capture`
  - run_capture 前缀拼装/loglevel 可覆盖/非零不抛/None 流归一 + **`run()` 签名语义零变化回归**：
    仍 error 级、失败抛尾 15 行 + blackdetect 真机冒烟 + **超时**：run 杀子进程转 FFmpegError
  - capture 回 124 不抛
  - 0/非法 env=不设限 + **孤儿判据**：PPID 活着不碰/无签名不碰/grep 不碰
  - 报告模式零 kill
  - serve 接 kill=True 而 doctor 只 find

- **改动面** **scored × 画面模式组合**：`variation._lint_scored_mix`（dubbed 硬冲突 / native 角色对白口型不同源，
  均 warn 级在花钱前点名）· `cli.stage_gen_video` 入口硬闸（scored+dubbed 拒发且 dry-run 同拦——否则用户先撞「缺配音需先 tts」
  这句在 scored 下指错路的错误，照做花两道白钱）· `compose` 的 `use_bgm`（scored 分支先于 native 短路，
  `scored_bgm: true` 在 native 下同样生效——片段音轨已整体让开，例外与画面模式无关）· `project.scored_audio` 纯函数（scanner/lint 判据单源）

  **必过** `test_variation`
  - `TestScoredMix`：dubbed 必报且指路双出路
  - native 对白镜点名（旁白 speaker 不算）
  - 纯旁白与 tracks 缺省不响

  **并过** `test_audioscript`（`TestScoredDubbedGate`：gen-video 拒发并给两条正路）

  **并过** `test_mix`（`use_bgm` 分支序源级钉点）

## 6. 设定图、资产与血缘

跨镜一致性的根基。设定图一改，下游全部分镜的有效性都要跟着重算。

- **改动面** `project.py` `scenes`/`matched_scenes`/`_matched_entities`（**具名场景分档**）· `workspace` 的 `add_scene`/`scene_fields` 白名单/`create_chapter` 拷贝 ·
  `sheets.scene_rules`/`aspect_for("scene")` · `cli` 的 `scene add|list|rm` 与 gen-refs 场景段 ·
  `refine` 四处 kind=scene 按 name 分派（有名=取景地/无名=全局）

  **必过** `test_design_refs`
  - `NamedSceneTierCase`：命中/显式白名单/全局与具名并存/短名不泛匹配/**场景图不许出现 `prop design sheet`
  - `主视图居中`
  - `纯色浅灰底`**/比例跟项目/`rules_for` 与生成同源

  **并过** `test_adapt`
  - `TestNamedSceneInheritance`：建章拷贝
  - sync 白名单推送
  - **场景不许漏进 props[]**
  - `TestSceneTopview` 的两条继承用例：**一个场景两张图，两条通路都得成对**——
    建章拷贝要带上 `scene_topview_ref`（具名那份随 `scenes[]` 整份拷贝过来），
    `scene_fields` 白名单要带上 `topview_sheet`。漏一处的形态是「系列里图纸都在、
    出视频时一张都挂不上」，且全程零告警

- **改动面** `project.py` `design_refs`/`matched_props` · `lineage.py` `required_refs`（设定图一致性单一真源）
  · **`lineage.rebaseline`（人直接落地新产物时重设血缘基线＋清过期标记，`supply.supply_image` 唯一调用点）**

  **必过** `test_design_refs`
  - 道具默认挂载=显式 props ∪ 文本命中
  - 就绪度与实际参考锁步
  - 8 图截断可见
  - skip_design 空；**`TestStaleMarkClearsOnEveryNewImage`：素材直供＝这一镜重新出过图**——「⚠ 设定已更新」
    当场消掉（不判直供的话只有 API 生成那一条路径清得掉，用户自己传了新图也擦不干净）
  - 同时按当前设定图**重记指纹**（只清标记不记基线＝这一镜从此脱离血缘，设定图再改多少版都不报警，
    隐性且更贵）
  - `refine` 局部改造反向归档：**留着旧基线、不清标记**（只重画一块矩形，输入侧设定图一张都没重新进过场）
    ，但必须把 `refs` 带过整块替换
  - **AST 穷举全引擎写 `shots[].image` 的四条路径**（cli/supply/candidates/refine，providers 是请求体不算）
    ，新增第五条即红

- **改动面** **image→clip 血缘边**：`cli.stage_gen_video` 成功登记时
  `lineage.record_refs(s, "clip", 实发 image 清单)`（路线 A=各比例分镜图，降级
  路线=场景基准图）· `lineage.mark_stale` 的 clip 扫描（未锁定置 retake 且**不写
  重做意见**——意见会被编译进下一版视频提示词；锁定只计数）· `cli.stage_gen_image`
  落新图即置存量 clip retake（事件边，不等人跑 `lineage mark`）· `cmd_lineage_status`
  的「片段出自旧版画面」行
  （缺这条边的实测症状：`gen-image --force` 换底后 `gen-video` 输出「完成 · 用时 0s」
  静默跳过，成片新图旧片并存）

  **必过** `test_design_refs.TestImageToClipLineage` / `TestGenImageExpiresClip`
  - 图变 → clip retake、无 note；锁定只标记；图没变零动作
  - gen-image 重生后存量 clip 当场置 retake，done 锁只点名不动

- **改动面** `versioning.py` `archive_output`/`rollback_output`/`restore_last_output`（**成片版本栈**：
  逐比例各一支谱系，落章节顶层 `output_versions`；归档必须在 `compose.build` **之前**——合成写同一路径，
  等它跑完再归档归的已是新片子）· `cli.stage_compose` 的归档钩子与失败回填 · `studio/actions.rollback_output_version`（不动 `output_wm`：
  水印版是派生物，回滚后须重打）· `/api/rollback` 的 `output_aspect` 分支 · scanner `output_versions` 下发 ·
  `panels.openOutputVPanel`

  **必过** `test_versioning`
  - `TestOutputVersionStack`：首次合成无可归档
  - 归档是移动且标准路径腾空
  - 逐比例谱系互不串
  - 回滚真换内容且原当前版进栈
  - 未知版号与无成片各抛
  - **合成失败回填并撤销条目**
  - 无历史时回填是空操作

- **改动面** `refine.supply_asset_sheet`（**设定图素材直供**：归档旧版→落标准路径→`_propagate` 血缘传播，
  与候选换选同一条通路；体检只硬拦「解不出」，`skip_check` 是不可再生素材的逃生舱）· `studio/actions.supply_asset_sheet` ·
  `studio/server._asset_supply`（原始字节通道，仿 `_shot_upload`）· `project.js supplySheetBtn`（角色/道具武器/取景地三处卡片）
  · **`versioning` 归档条目落绝对路径**（回滚时 cwd 可能已变，存相对路径会当场 FileNotFoundError 而文件其实在盘上）

  **必过** `test_versioning`（`TestArchivePathsAreAbsolute`：分镜与资产两侧归档路径必绝对·换 cwd 后回滚仍能取回）

- **改动面** `versioning.py` `archive_asset`/`rollback_asset` · `refine.py` `archive_asset_sheet`/`rollback_asset_sheet`/`_asset_version_ctx`（设定图版本栈单一真源）
  · `cli.cmd_gen_refs`（--force 直出重生前归档）· `studio/actions.rollback_asset_version` ·
  scanner 设定图 `versions`/`version_history` 下发

  **必过** `test_adapt`
  - `TestAssetVersioning`：archive 移动+登记
  - 标准字段字符串不变
  - rollback 拷回内容+当前版归档(rollback-out)+血缘传播
  - 场景 scene_ref_versions 与角色/道具 versions 互不串
  - --force 归档旧版
  - scanner 下发+action 回滚

- **改动面** `versioning.py` `_current_files`/`archive`/`rollback`（分镜版本栈 image/audio/clip；**回滚先解析并校验目标（含 URL 落地）
  再归档当前版**，`rollback_asset`/`restore_last_output` 的历史读侧同过 `ensure_local`）· `cli._regen_gate`（**纯只读状态机判定**）
  + `cli._archive_regen`（整批计划成功后、`_mark_wip` 前统一归档）· `cli.cmd_versions_rollback` ·
  `studio/actions.rollback_version`

  **必过** `test_versioning`
  - `TestVersioning`：归档移动+登记+自增
  - 回滚归档不可变可反复
  - **主字段与逐比例字典共指同一文件时按路径去重、只归档一次且条目必落账**（引擎回填约定 `s["clip"]=clips[主比例]`——不去重则第二次 move 崩在登记之前，
    clip 版本栈永远写不成）
  - 回滚后 clip/clips 字段仍指画布路径不悬挂
  - **夹具必须用引擎真实产出形状**（「主字段与主比例各是一个文件」引擎产不出来，拿它当夹具等于没守）
  - **`TestRollbackTargetFirst`：目标缺失时画布原地不动、不追加 rollback-out；URL 历史条目经 ensure_local 落地后可回滚（分镜/资产/成片三路）
    **
  - **`TestRegenGateDefersArchive`：闸门不移文件不写条目，归档只在 `_archive_regen` 且只动 regen 镜**

## 7. 预演、导演台与分镜板

花钱之前的三条预演路径，共同的纪律是「预演物绝不冒充成片」。

- **改动面** `pipeline/camera.py` 的 `CAMERA_PRESETS`(36)/`catalog()`（**运镜单一真源**：3D 相机装备 + `shots[].camera` 措辞，
  两个面同一行数据）

  **必过** `test_camera_presets`
  - **头号用例逐字节比对 `.claude/skills/kinema/references/storyboard.md`**：21 条复用运镜的 label/label_en/phrase/phrase_en/tier 一字不差——两边分叉会让同一个「缓慢环绕」
    在手写与 3D 两条路径上给模型两种指令
  - tier/rig/ease/look/group 枚举合法
  - keys 关于 t 单调且 t0=0/tN=1
  - fov∈[10,90]
  - orbit 的 path 与首末关键帧自洽
  - `lock_subject_scale` 只有 dolly_zoom
  - 措辞不叠第二个主运镜
  - catalog 返回副本且键齐备

- **改动面** `previz.py`（previz 登记/体检/场景快照/导演目录）· `ffmpeg.first_frame/last_frame` ·
  `cli` 的 `_shot_plan`/`_v2v_*`/`previz` 动词 ·
  `studio/{scanner,actions,server}` 的 previz 面 · `studio_app/director/*.js`

  **必过** `test_previz`
  - **previz 绝不写进 `shots[].clip`**（compose 视 clip 为成片会直接播灰模）
  - 首帧只在该镜无图时才登记且必走 supply 轨（provider=supplied/待审/版本栈）
  - **previz 末帧优先于衔接链的下一镜图**、**V2V 开启则一帧都不发**
  - V2V 是 opt-in × 仅 native × provider 必须真支持（`generate(**kwargs)` 会静默吞掉不支持的 reference_video）
  - 报价含输入视频秒
  - 体检只硬拦「解不出」
  - `snap_duration` 与 `SeedanceProvider.billable_seconds` 逐值对拍
  - **前端契约**：rig.js 注册表与 `director_catalog` 键/loop/speed 锁步、cameras.js 缓动名覆盖全部 preset、
    导出前后成对切洁净模式、`sceneAt` 预览与导出同一个函数、路由注册与 dispose、竖幅辅助画面按高度定尺寸并与 PIP 渲染缓冲逐值锁步

- **改动面** `sketchboard.py`（**简笔分镜预演板单一真源**：`active_guide` 互斥仲裁 / `beats_of`/`beat_times` / `board_prompt` 板提示词契约 / `timeline_text` 分段时间轴（**只管时间轴**；
  段头随 `VideoProvider.timeline_unit` 分秒段/顺序编号两支，两支都不带机位义；
  `native=True` 才逐拍附 `sound`，标签与 `_beat_line` 逐字同源）/ `timeline_has_sound`（供 prompts 判「镜级 sfx 要不要让位」
  ）/ `board_role_clause` 板职责声明（由 prompts 拼**头部**，挂时间轴尾巴会落在提示词 55% 处压不住箭头）
  / `reference_opt_in`+`set_reference`（**衔接章里的孤岛表态·缺省档下与缺省行为重合**） / `register_board`/`clear_board` / **`beats_sig`+`board_drift` 板漂移判据（dur+拍序列+格数三面）
  ** / **`beats_density`+`MIN_BEAT_SEC` 拍密度体检** / **`auto_beat_cap`+`TIDY_PANELS` 拍数按时长收敛** / **`grid_of` 版式恰好填满** / **`reference_shot` 参考孤岛静态判据（显式开启×native×guide=sketch×板在盘——衔接章里强制该镜切回参考任务；
  全能参考本身已是 native 缺省档，缺省档判据在 `cli._shot_plan`，`framechain.island` 消费同一函数）**）
  · `cli` 的 `sketch gen|use|clear|list` 与 `_shot_plan` 仲裁位（ref_mode 缺省档判据 + `_video_sheet_refs` 角色/场景+主场景俯视图/道具设定图组合（配额=7−板·
  角色优先·**主场景那张图纸紧跟它自己的基准图、每镜至多一张**·kind 随行供 @图片N 绑定）·`prompts.sheet_binding_clause` 逐张职责绑定
  （编号=content[] 附图顺序、各句恒按编号递增，frame/board 占位只占号；`scene_top` 职责句声明「是图纸不是画面、
  绝不改成俯视视角」，「仍须可辨认」的收尾回指全部设定图而不粘在末句尾巴上）·`_cast_sheet_refs` 板生成专用）· `providers/video/seedance.py` 的 `reference_only` 参考生视频分支 ·
  `prompts.video_prompt` 的 `sketch`/`sketch_board`/`ref_mode`/`ref_sheets` 四参数（`CONTRACT_ALLREF` 契约句·
  设定图半句与实附张数一致） · `project._SHOT_HUMAN_KEYS` 的 `guide` · scanner `sketch`/`guide_active`/`sketch_stats` ·
  chapter.js 简笔分镜台

  **必过** `test_sketchboard`
  - **段头不是多镜记号**：顺序编号那一支发「第N段」且 `variation.MULTISHOT_RE` 零命中，秒段仍然不发
  - **互斥生命线**：guide=sketch 时 previz 末帧与 V2V 一律不参与、guide=previz 时时间轴与板零注入、
    显式表态指向空槽也不静默回落
  - **防泄漏句与「板真的附上了」逐字一致**（附板必带「绝不输出铅笔素描」、没附绝不声明「所附分镜板」
    ）
  - 板绝不进 `image`/`clip`
  - 能力闸拦附板不拦时间轴
  - 真发 `ref_images=[板]` 与 dry-run 同源
  - 幂等/--force/clear 保文件保 beats
  - scanner 走 `_sk.active_guide` 源级
  - 前端消费 `s.guide_active` 不自算
  - 板提示词九格秒标/五色画法在场且**图例走底部大字规格**（`test_line_art_color_system_and_bottom_legend_spec`：
    判据是字号不是图例本身——板头窄条 ~20px 必糊、拍号档 ~30px 能画对，图例文字是固定语义照样板画即正确；
    视频侧语义仍走 `board_role_clause` 五色全讲 + `BOARD_FLOOR` 三色同步，灯箱 caption 另有固定行 `SKB_LEGEND`；
    附样板时职责声明点名「图例横条属于版式，照样板位置与字号画」）/职责声明与附图旗标逐条对应
  - 内置样板图在位
  - **缺省档板在盘即附、衔接章缺省不附**（`TestReferenceShot.test_opt_in_is_required`：孤岛表态必须显式——framechain 才分得开「用户点名的孤岛」
    与「缺省档参考镜」；`test_native_default_walks_reference_mode_with_board`：缺省章板自动随请求附发；
    `test_chain_chapter_board_shot_stays_on_first_frame_without_opt_in`：衔接章首帧任务禁混参考图、
    板只当拍表）
  - **防泄漏两处同时说**（`test_board_leak_guard_sits_up_front_and_in_the_negatives`：板声明必须落在提示词前 1/3 且早于时间轴 ＋ `prompts.BOARD_FLOOR_*` 逐词进「避免出现」
    并排在防字地板之后；没附板则一个板地板词都不许出现）
  - **板漂移体检覆盖板真会附发的两条通道且逐镜**（`test_native_opt_in_shot_gets_drift_check_on_every_shot`：
    native+「板作参考」的镜与 dubbed 一样体检——只查 dubbed 会让 opt-in 镜拿旧节奏板骗过模型零告警；
    塞进「整章只喊一次」闩锁则第二镜起没人体检）
  - **sketch gen 全灭必须以异常收场**（`test_all_failures_exit_nonzero`：Studio 按退出码映射 done/failed，
    exit 0=前端弹绿字而板一张没落盘）
  - **手写 beats 超 `PANEL_MAX` 报不改写**（`test_authored_beats_beyond_panel_max_refuse_loud`：
    生成侧静默截断=「板 12 格、时间轴 15 段」两套事实；时间轴不受画板上限约束）

- **改动面** **运动预演两台的章节级门**：chapter.js `previzDesks`（3D 导演台 + 简笔分镜按
  `d.uses_video` 收成折叠条 `.pvz-fold`／`PVZ_OPEN` 展开态）· `audioScriptCard` 的 `dubLock`
  （scored × dubbed 互斥在表态处拦，与引擎硬闸 `cli.stage_gen_video` 同一条判据）·
  `cmd_sketch_gen` 的 kenburns 告警（只告警不拦）

  **必过** `test_sketchboard`
  - `test_previz_desks_are_gated_by_uses_video_not_by_project_type`：门只认 `uses_video`
    （= `Project.uses_seedance`），出现 `d.skill`/`d.profile` 即红——motion 是章节级字段，
    按项目类型判会逐章判错，而前端另建映射表就是第二真源
  - `test_audio_script_desk_is_not_gated_by_uses_video`：音频剧本与 motion 正交，不许跟着收起
  - `test_kenburns_run_says_the_boards_will_not_reach_the_film` / `test_video_motion_run_stays_silent_about_it`：
    kenburns 出板要点破「按分镜图同价计费却不参与成片」并给可行动项，native/dubbed 下不喊

  **并过** `test_audioscript`
  - `test_dubbed_chapters_cannot_be_switched_onto_the_scored_route`：只拦 tracks→scored 一侧
    且拦在写盘请求之前（已是 scored 的 dubbed 章节要切得回来）
  - `test_scored_route_is_available_to_kenburns`：kenburns × scored 是合法组合（对拍
    `Project.needs_tts`），判据里不许出现 `"kenburns"`

- **改动面** `providers/base.py` 的 `supports_last_frame`（**逐别名能力位·缺省 True**，`config/models.yaml` 三个 seedance 别名各自声明）
  · `providers/video/seedance.py` 的连接段读取与 generate 硬拦 · `cli._shot_plan` 的 `can_last`（同时关掉 previz 终态与链上末帧两个来源）
  · `cli._warn_no_last_frame` · `BREAK_ZH["no_last_frame"]`

  **必过** `test_providers_request`
  - `TestSeedanceRequest`：能力位随别名声明且缺省 True
  - 不支持时**抛错而不是静默发出去**
  - 首帧照发只关末帧那一个槽

  **并过** `test_review`
  - **`TestLastFrameCapabilityGate`：三处必须同时改口**——不标「末帧→镜N」
  - 提示词不写末帧铁律句
  - 出声说明原因并给出出路；断因是「本模型不支持末帧」而不是「下一镜缺图」（后者会让人去补一张本来就在盘上的图）
    ；对照组证明能力位为真时一切照旧

  **并过** `test_router_defaults`（内嵌 caps 元组含 `supports_last_frame`，yaml↔EMBEDDED_DEFAULTS 锁步）

- **改动面** `prompts.PACE_ZH/EN` 播放速率地板 + `_PACE_ECHO_*`（扫描面与结构锁共用 `struct_src`：变速技法既可能写在 camera 也可能写在正文）
  · `prompts.micro_motion()` 按 `characters[].subject_kind` 选随动附属物名词 + `_FOLLOW_ZH/EN` ·
  `MICRO_MOTION_ZH/EN` 降级为「未登记类型」的缺省形态 · `cli._video_subject_kinds`（取材走 `Project.shot_cast`，
  与设定图在不在盘无关）

  **必过** `test_prompts`（`TestPaceFloor`：缺省注入／**点名升格慢放延时快进即让位**（正文与运镜两处都扫）
  ／V2V 不注入（运动权威冲突）／排在运镜之前／措辞不撞 lint 词表；`TestMicroMotionFollowsSubjectKind`：
  逐类型选对名词／**未登记就丢掉这半句而不是猜**／多类型按登记顺序合并且同类只说一次／英文用分词式（`fur follow` 主谓不一致）
  ／video_prompt 真发出选中的名词）

- **改动面** `pipeline/anchorframe.py`（**首帧锚定判据单一真源**：`active` 章级 / `shot_opt_in` 镜级 / `anchored` 逐镜，
  显式 opt-in × 仅 native）· `Project.anchor_frame` · `cli._shot_plan` 的 ③′ 支（**只否决缺省档那一支**：
  V2V/previz 末帧优先、衔接参与镜不算锚定否则同一行自相矛盾、显式参考孤岛仍赢）· `cli._warn_anchor_tradeoff`（章级一次，**闭包局部而非模块级 `_warned_*`**——那几个跨调用不重置，
  同进程连跑两章第二章整章不出声）· `gen-video --anchor-frame` · `_shot_plan` 返回元组第 9 位（4 处解包同改）

  **必过** `test_anchorframe`（判据边界／章级镜级 CLI 三入口各自作用范围／CLI 覆盖不落盘／dubbed 章不标一个不会生效的锚定／三条让位通道点名**一次**且不提用户没要过的尾帧接力／锚定镜一张设定图都不进请求／衔接章不受影响）

- **改动面** `cli._video_sheet_refs` 的**配额裁剪点名**（`dropped` 第二返回位）· `_sheets_for` 三返回位（3 处调用同改）
  · `_ref_note(dropped=)`

  **必过** `test_anchorframe`（`TestQuotaTruncationIsNamed`：配额内静默／超配额指名道姓不只报个数／**裁剪注记与张数同行**——「设定图×7」
  单独出现读起来像「都发出去了」，而被丢的可能正是作者显式写进 `shots[].props` 的那一个）

- **改动面** `cli._gate_frame_aspect`／`_frame_aspect_gaps`（复用 `mediacheck.aspect_overflow` + `SUPPLY_ASPECT_TOL`，
  只有后果措辞是视频路径专有）· `cli._gate_voiceover`／`_voiceover_gap`（判据整条走 `variation.voiceover_overrun`）
  · `variation.voiceover_overrun` + `VOICEOVER_MIN_SHOTS`（**lint 的 `voiceover_heavy` 与本闸共用**）

  **必过** `test_prespend_gates`（**lint 报不报与闸问不问在每个样本上恒等**——本组存在的理由／两道闸都不硬拦／**非交互既不替用户中止也不替用户确认**／ffprobe 缺席退化为静默，
  护栏不许反过来锁死机器／比例闸要报出实际尺寸并给出修法）

- **改动面** `variation._lint_prompt_negation` + `_NEGATION_MARKERS`/`NEGATION_HEAVY_RATIO`/`NEGATION_MIN_CLAUSES`

  **必过** `test_variation`（`TestPromptNegation`：过半禁令 warn／带一两条边界约束的正常写法静默／分句数不足不判／kenburns 不判／**「不再脱落」
  「与上一镜不同」这类终态与对比不许误判成禁令**——误报会逼作者删描述）

- **改动面** `pipeline/framechain.py`（衔接态判据/链图/断链措辞·**衔接=显式 opt-in**（章级 `active` 缺省关 + 镜级 `pair_opt_in` 结对 + `welded_in_ids` 被焊入集合）
  ·**焊缝两端谓词 `island`/`sends`/`receives`**·**孤岛接缝同步 `seam_plan`/`sync_seams`（`MODE_BREAKS`·
  `AUTO_MARK`·只在衔接章补缝）**·**单一真源**，`Project.frame_chain` 与 `studio/scanner` 同读）
  · `previz.v2v_shot`（「这一镜有没有可发的参考片」唯一判据，`cli._v2v_shot` 只是别名）· `cli.py` 的 `chain_map`/`_flf2v`/`_chain_break_note`/`_sync_island_seams`（末帧解析与软切落盘）
  · `transition sync` 子命令

  **必过** `test_review`
  - `TestFrameChainDefaultState`：native 缺省关闭（缺省档=全能参考）
  - 显式 `frame_chain`/`--chain` 才衔接
  - 镜级 `pair_opt_in` 只焊点名那一处且仅 native
  - `--no-chain` 压过 `--chain` 且不落盘；`TestFrameChainStudioDownlink`：下发的是有效链态 + 逐镜去向/断因；
    `TestFrameChainLastFrameGate`：跳 omt 弃用镜
  - `--approved-only` 不跨接
  - 转场断链
  - 次比例缺图退回常规
  - dry-run 与实发同源
  - 两档兜底句日志与实发一致；**`TestFrameChainWeldEnds`：焊缝两端都判——上游绝不焊进全能参考/V2V 镜（`ref_next`，
    只判上游端会漏掉这一半，「切点仍近似连续」是想当然）
  - V2V 总闸关着时 previz 镜照常衔接
  - previz 末帧镜发不出这一焊（`previz_last`）
  - 断因与措辞锁步**；**`TestIslandSeams`：孤岛两侧自动补 `seamless`——无孤岛则一个转场都不插
  - 幂等且不重排镜号（否则 `shot_<id>_tr.mp4` 缓存每跑一次就换）
  - 手写转场不碰也不叠
  - 章首/章尾不插（xfade 单侧邻居退化字卡=黑闪）
  - 配置一撤自己撤走
  - 弃用镜夹在中间仍紧贴孤岛**

  **并过** `test_sketchboard`（`test_scanner_chain_view_marks_reference_mode`：**页面链态一个字都不重判**，
  全走 `framechain.plan`——`_chain_view` 若在那里自抄一份全能参考判定，
  规则一扩到 V2V/previz 末帧抄本立刻漏判）

## 8. 闸门、台账与体检

这一组决定「什么时候不许往下走」与「钱怎么记」，错一条就是白烧钱或漏拦。

- **改动面** `cli.py` `_assemble_review_gate` / `cmd_assemble`（合成前审阅闸）

  **必过** `test_review`
  - `TestAssembleReviewGate`：未过审拦截
  - 模式选视觉阶段
  - 旁白才查 audio
  - 转场/弃用跳过
  - --draft 逃生舱

- **改动面** `budget.py`（额度裁决单一真源 `limit`/`spent_total`/`verdict`）· `cli.py` `_will_burn`/`_plan_cost`/`_preflight_spend`（花钱前预留额度事前闸）
  · `--confirm-spend` 三处登记（argparse + `_stage_wrapper` kw 转发 + `stage_gen_video` 形参）
  · dry-run 报价的比例乘法

  **必过** `test_budget`
  - **`test_preflight_has_no_side_effects`：patch `versioning.archive` 断言零调用**——预演绝不复用 `_regen_gate`
  - `test_will_burn_skips_done_and_existing_clips`（done 锁定/omt/转场/已有片段四闸 + `--force` 也不破 done）
  - `test_parallel.TestWiring.test_preflight_and_real_run_share_the_salvage_predicate`：**盘上待登记片段的判据
    （`_salvageable_clip`）事前闸与真跑共用一份**——各写一份时预估会把不需要再买的比例算进报价，
    预算够的批次反而被硬拦；该判据只拼路径不建目录（预演层零副作用）。
    实发稿审阅锁那道差异刻意不补：算 sha 要先编译整条提示词，途中会上传 previz、预热锚定音，
    故 `_will_burn` 按契约是**上界**而非等式（虚高只会少发不会多发）
  - `test_will_burn_multiplies_by_aspect_count`（双比例=2 次调用=2 份秒数）
  - `test_preflight_does_not_write_cost_estimate`
  - `TestTtsBillingPrecedesEarlyExits`：**本批 TTS 的实付额必须在任何早退之前入账、且只有一个落点**
    ——`total_cost` 在 `parallel.run` 收尾时已是终值，而「部分镜失败」与「缺镜拒拼旁白轨」
    两条早退都会带着已合成那几句的实付额抛出去，台账少记会让两道额度闸按偏低的已花额放行
  - `test_preflight_blocks_when_batch_exceeds_budget`（台账零变动）
  - **镜级子集 dry-run（--only/--approved-only）绝不覆写全片预估**
  - 单笔阈 `--confirm-spend` 拦/放行与 `auto=True` 告警放行
  - 额度归一 NaN/Infinity/≤0=不设限
  - `test_add_cost_still_charges_then_raises` 事后闸不变

- **改动面** `decisions.py`（决策审计 append-only 单一真源 `add`/`entries`/`union_by_id`/**去重键 `entry_key`+`derived_id`**）
  · `project.py` `_DOC_HUMAN_KEYS` + `_DOC_APPEND_KEYS`（合并层按 id 取并集）· `cli` `decision add/list`

  **必过** `test_decisions`
  - **核心守卫 `test_decisions_survive_engine_save`**：加载→磁盘追加一条→引擎 save→断言仍在
  - `test_engine_save_unions_both_sides` 钉并集非整键替换
  - 连 save 五次幂等不叠加
  - 从未用过不凭空写空数组
  - confidence 枚举校验+条目 JSON 合法
  - **指数膨胀守卫**：无 id 手写条目连 save 五次条数恒 1、内容派生键 `sha256:<hex16>` 就地补进条目、
    不同内容不塌缩、并发 save 不丢不翻倍

- **改动面** `pipeline/variation.py`（分镜单调度 lint 软闸 + 反 slop 词表 `SLOP_TERMS` + `FRAMING_BUCKETS` + `art_direction` 旋钮→阈值映射，**四者单一真源**；
  **增 `_lint_scene_continuity` 场景连续性维度**；**增表演物理化/画面代词/旁白文风/视觉换挡四族维度**——`EMOTION_TERMS` 抽象情绪词表（扫描面含 action/end_state 两个 delta 骨架位）
  · `PRONOUN_RE` 代词（其他/其它/吉他 负断言排除）· `NARRATION_SLOP`+`NARRATION_PIVOT_RE`+`NOMINAL_RE`+`GRAND_WORDS`+`OPENER_MARKS` 旁白文风族（**统一走 `voicecast.shot_text` 口径故 lines[] 可见；
  绝不扩 `_lint_slop` 的提示词扫描面**）· `shift_gap` 换挡时间轴（只认 scenes/framing/angle/light_shift/转场五个结构化位，
  恒 info）· **五条互斥/查表族维度**（`camera_clash` camera↔video_prompt 运镜互斥（类目集合**交集为空**才报 + vp 侧只取含摄影词的小句——两道缺一就是刷屏或漏报）
  · `preset_placeholder` 预设填空位 X/Y 残留（**结构性判据 + 从 `CAMERA_PRESETS` 派生**，不建第二份词表）
  · `unregistered_entity` 显式点名查不到（纯查表零猜测，`characters` 缺省与显式 `[]` 一律跳过；
  ctx 需装 characters/props/scenes 三张表）· `craft_leak` 版本号/文件名/判例号/工序词（分镜图/简笔板/底片）漏进交付文本（扫描面含 `review.note`——它会被编译进下一版提示词；
  **引擎自己写的 note 不许带文件名也不许含工序词**，守卫 `TestCraftLeakProcessWords`）· `burn_mixed_narration` 混烧×同镜对白夹旁白 warn（旁白句会由模型代声，挪进纯旁白镜）· `dubbed_dialogue` dubbed×对白上镜 warn（烧录轨与口型两条时间轴不同源）· `scene_daypart_missing` 取景地时段缺口 info（`_DAYPART_RE` 具名时段词表，不认单字「光」——基准图自选时段后全链路把它当光线基准，守卫 `TestSceneDaypart`）· `character_fatigue_look` 角色外貌疲态 warn（`_FATIGUE_RE` 只扫正向外观字段，命中词登记进 `visual_requirements` 即放行，守卫 `TestFatigueLook`；同判据 `fatigue_look` 在 `project refs` 拦在计费之前、`character add/set` 建档即提醒，守卫 `test_adapt.TestFatigueGate`）· `UNFILMABLE_TERMS` 拍不出的内心词，
  走 `_lint_abstract_emotion` 同通道但发独立 code）· **`_lint_generic_name` 角色泛称**（`GENERIC_CAST` 词表；
  只在项目登记过角色时判；泛称被注册名包住不算命中——「守卫队长」里的「队长」是名字的一部分）
  · **`_lint_prompt_thin` 提示词厚度地板**（`MIN_IMAGE_PROMPT_CHARS`/`MIN_VIDEO_PROMPT_CHARS` 单一真源；
  只判非空字段——该不该写归 `motion_plan`，本维度只管「既然写了就得写够」；**厚度判据不问 motion**——`render_mode` 那条「别催该模式下不存在的阶段」
  的通则管的是**阶段**，`video_prompt` 是已写在盘上的**字段**，kenburns 章切模式就原样发出
  ；kenburns 下只写 `camera` 不算写了运动稿（Ken Burns 风格键），`video_prompt`/delta 骨架写了才判并另发一条 `prompt_thin_mode` info）· **`_lint_motion_plan` 运动规划深度档**（`motion_plan`：
  native/dubbed 下缺 `sketch.beats` 即 warn，previz 镜按 `active_guide` 豁免；`beats_span`：
  把休眠的 `sketchboard.beats_coverage` 接进 lint——authored `t` 不随 `dur` 重算，改过时长而秒段没跟着改就是一份对不上片长的假脚本）
  · **`_lint_bilingual` 提示词双语完备性**（`_BILINGUAL_PAIRS` 字段对单一真源；判据「有中文才要求英文」
  故 kenburns 章节不被催 video_prompt_en；两条字段各报一条 warn——缺英文对位时 `prompt_lang=en` 的 provider 会静默回落中文）
  · **`_lint_voiceover` 旁白语态**（`_voice_kind` 走 `voicecast.shot_lines` 三分 dialogue/voiceover/silent；
  语态=顶层 `voiceover` 声明 > `skills.voiceover_default(profile, skill)`；lead 静默·sparse 旁白镜>40% warn·
  none 有旁白即 warn·剧情语态无纯画面镜 info）· `cli._lint_gate`/`cmd_lint` · `studio/scanner._lint_view`

  **必过** `test_variation`
  - **`TestGateIsSoft`：空 shots/全 omt/全转场/字段写坏一律不抛异常**——输入只用 `data.get("shots") or []`，
    绝不碰 `Project.shots`/`active_shots`（那两个会抛 ProjectError）
  - 跳过判据复用 `transitions.is_transition`/`review.is_omitted`
  - `SLOP_TERMS` 20~30 条且每条带物理化改写建议
  - framing 归一「双人中景→中景桶」且归不了只提示
  - **软闸必须在 `--only` 过滤之前**（`--only` 降一行汇总）
  - 三旋钮真驱动阈值
  - `art_direction` 只出现在 cli/variation/scanner 三处（永不改画面）
  - lint 不落盘 + `--strict` 非零退出
  - 条幅只在 `chapter_detail` 算不进 overview/board
  - **`TestSceneContinuity`**：无锚正镜 warn 且显式 `[]`=有锚
  - 语料点到注册取景地=有锚
  - 全局 scene 在场不催
  - 相邻场景不相交且无转场=info 直切
  - **转场判定必须走 ctx.raw_shots**（`active_shots` 已滤掉转场镜，只看过滤后列表会把「画室→转场→渊口」
    误判直切）
  - garbage 字段不炸
  - **`TestCameraClash`**（必报=static↔升镜；**必不报三条**：交集非空的延展写法／「身体往下沉了半寸」
    这类主体运动／单边为空）
  - **`TestPresetPlaceholder`**（**锁步断言**：`CAMERA_PRESETS` 里每一条带填空位的 phrase 都必须被抓住，
    源侧改写法/加删档自动跟；`4X 变焦`不误报）
  - **`TestUnregisteredEntity`**（props 也查；缺省与 `[]` 都跳过；名册没建不判）
  - **`TestCraftLeak`**（含一条钉住「引擎不自造噪声」的源级断言）
  - **`TestUnfilmableTerms`**（「半透明白色」不误中「明白」；与 `emotion_abstract` 不混 code）
  - **`TestGenericCastNames`**：泛称 warn
  - 注册名静默
  - 泛称被注册名包住不误伤
  - 无设定集不判
  - 提示里列出已登记的名字
  - **`TestPromptThickness`**：一句话运动提示词 warn
  - 写够即静默
  - 画面提示词同款地板
  - 空字段不由本维度催
  - kenburns 不判运动厚度
  - **地板必须卡在真实创作（三个出过片的章节逐镜最短 147/178 字）与一句话打发（20~70 字）之间**
  - **改地板数字必须同批附新的实测出处**（现为 110/140，实测：该档下 chrome/chrome2/corridor 零告警，
    而 250/150 会误伤 corridor 9 镜）
  - **`TestMotionPlanDepth`**：散文镜 warn
  - 有 beats 静默
  - 只在 native/dubbed 判（kenburns 催了没有可行动项）
  - previz 镜豁免
  - authored 秒段不覆盖时长即报
  - 自由文本 t 不误报
  - **`TestBilingualPrompts`**：缺英文=warn 非 info
  - 两条字段各报一条（混报就分不清缺画面还是缺运动）
  - 空串与缺键同罪
  - 未写中文的字段不被催
  - 转场/弃用镜走 `active_shots` 排除
  - **情绪单调判据与 `_has_emotion` 同源逐句取**（`shot_lines` 已做镜级→句级继承；只读镜级会把 `str(None)` 折成 "none"——全靠 lines[] 标情绪的章节被判 1 种、
    混写时 "none" 又凑成一种真情绪，空值必须丢弃）

- **改动面** `pipeline/mediacheck.py`（**成片自审阈值单一真源**：黑帧 `BLACK_YAVG/BLACK_YMAX`、静音 `SILENT_MEAN_DB`、
  削波 `CLIP_MAX_DB`、响度 `LOUDNESS_TOL`/`loudness_i`（**有限性守卫**）、黑场禁区 `black_windows`/`sample_points`、
  探测命令 `frame_stats_args`/`volume_args`）· `cli.cmd_verify` · `studio` 三处（scanner 透传 / `actions.verify_final` / `/api/verify`）

  **必过** `test_verify`
  - **真片标定的四条黑帧断言**：真黑 16/16 判黑
  - 极暗非黑 25/25 不判
  - 真实夜戏 40.2/**255** 不判
  - 低 YAVG 带高光不判｜**转场黑场禁区**：窗=转场镜整段±两侧 edge
  - 抽样点绝不落进窗
  - 同一支中段涂黑的片子「有转场→通过 / 无转场→硬失败」双向验｜时长容差帧量化 `max(0.5,n/fps)`｜**「该响却哑」
    含 native**（无音轨即硬失败）
  - 纯画面无 BGM 的 kenburns 只记 info｜`volume_args` 钉死 `-vn`（不加时无音轨会退出 0 且静默无输出→误判「测到了但值为空」
    ）
  - 帧探测 `-ss` 前置 + `-an`｜probe 异常一律转「容器无效」条目**绝不冒泡**｜output 为 URL 时必过 `ensure_local`、
    拉取失败记 info 不判坏片｜字幕数 `Dialogue:` 行、corner_note-only 镜按现状不计｜**结论块必为合法 JSON**：
    整段静音时 loudnorm 报 `-inf`、`parse_measurement` 转成真 `float('-inf')`，`loudness_i`/`loudness_off` 必须写 None，
    静音冒烟用例与形态用例双双 `json.dumps(rep, allow_nan=False)` 钉死｜lavfi 冒烟层零素材入仓
  - **`voice_placement` 旁白轨逐镜语音落点**（`TestVoicePlacement`：判据对象恒是
    重拼后的 narration.wav——带 BGM 的成片本体做振幅级语音检测分不清人声与音乐，
    包络相关同样失灵；有词镜窗口须有语音段、无词镜窗口超 0.4s 语音才点名、
    边界续音容忍；开口对齐允许语音起点落在窗口中段故不判头部位置；native/scored
    不在辖区；恒 todo 级不硬拦）
  - **期望字幕条数认得 lines[]**（`TestExpectedSubtitleCountsLines`：裸 `pick_texts`
    只读 narration/caption，逐句字幕章节的期望值会塌到只剩 caption 补位、
    「不少于」检查形同虚设——有词判据走 `voicecast.shot_text`）
  - **`native_voice_check` ASR 人声文字核对**（`test_asr.TestNativeVoiceCheck`：
    native 声源的「字幕与人声一致」从待核对收成实测——判据对象是 gen_clips 底片
    （成片混 BGM 稀释判据）；混烧章只查对白镜（旁白是烧录 TTS 与字幕同源）；
    逐比例出片时各比例的片段逐条转写（按路径去重）；片段取 `Project.clip_for`
    ——裸读 clip 会让跑过 `oss sync` 的章节整章跳过；稿面召回
    < `VOICE_TEXT_RECALL_MIN=0.6` 记 `voice_text_drift` todo；整镜达标后按
    `lines[]` 逐句摊回，单句 < `VOICE_LINE_RECALL_MIN=0.5` 记 `voice_line_dropped`
    且该镜不计入「相符」（`test_a_dropped_line_is_reported_even_when_shot_recall_passes`）；
    faster-whisper 未装照实标 `available: false` 不拦；report_lines 的分母只数
    真正转写过的行，跳过数另行说明）
  - **转写侧的两类假警**（`test_asr.TestTranscribeDecoding` 两条 + `TestTextRecall`
    数词折叠一条）：VAD 对轻声台词整段判负 → 判空即无 VAD 复解一次，闭声镜不进
    核对故不受影响，正常片段也不付第二次解码；字形引导句压不住阿拉伯数字
    （「零七，报数」→「07 报数」召回掉到 0.5）→ `_norm` 把数词折成汉字再比。
    缺任一条，verify 都会为一段正确的片段建议付费重投

- **改动面** `pipeline/asr.py`（**本地 ASR 单一真源**：faster-whisper 可选依赖、
  惰性加载失败记忆化、任何入口失败回 None 绝不中断调用方；`line_windows` 按句
  字数配额切词流——ASR 误字不动摇比例，整镜相合度低于 `_ALIGN_MATCH_MIN=0.5`
  拒绝划界；`text_match` 归一化后字符级双向相似度，`text_recall` 是稿面单向召回
  ——verify 问「这一稿念了多少」用后者，对称口径对漏念不敏感（转写只有稿子前
  43% 时仍给 0.6）；`line_recalls` 整稿一次匹配后按句区间摊回（`TestLineRecalls`：
  两字句整句漏念召回 0、整镜仍 ≥0.9；句内误字仍 >0.8）；`speech_chars` 是语速实测与
  预估共用的字数口径；标点/空白/分词差异不进任一判据）
  · `compose.speech_spans_resolver` 对原生声源多句镜恒先 ASR 划界，划不了再收
  整体首尾（未装 faster-whisper 时即退到「不换人但覆盖完整」）

  **必过** `test_asr`（划界配额切分/相合闸/钳位单调 · verify 核对节目标镜筛选与
  阈值 · compose 回落链源级钉死 · 全部 mock `transcribe`，离线零模型下载）

- **改动面** `voicecast.anchor_ref_path` / `anchor_budget_cap` / `cli._anchor_note`
  （**锚定缓存键含文本指纹**：锚定文本换版后旧缓存自然失配、下次使用自动重预热
  ——只按音色键命中会把旧文本的短样本继续发出去，音色跟随档位被静默拉低；
  **预算均分口径单源**：dry-run 注记逐条标实发时长，与真发裁剪同用
  `anchor_budget_cap`，页面标的时长才是实发的）

  **必过** `test_voice_anchor.TestAnchorTextLength`（长度带 50~68 字 + 文本指纹
  换版换路径、同文本恒定路径 + 均分口径 15s/1→14.8、/2→7.3、/3→4.8、下限 2s）

- **改动面** `pipeline/consistency.py`（**角色跨镜一致性 M7 单一真源**：`frame_stage`/`retake_stages`、
  `frame_timestamp` 抽帧点位、`shot_sheets` 配对+localize、`no_compare_reason` 空清单原因、
  `scan` 产料、`set_verdict` 回填与打回、**`invalidate` 失效**）· `cli` 两子命令 `consistency scan|set` ·
  **凡是换画面的门都必须调 `invalidate`**（`cli.stage_gen_image` 直出/宫格两路 · `cli.stage_gen_video` ·
  `supply.supply_image` · `refine.refine_shot_image` · `candidates.pick` · 版本回滚两入口 `cli.cmd_versions_rollback`+`studio.actions.rollback_version`）
  · `project._SHOT_HUMAN_KEYS` 登记 `consistency` · scanner 透传 + app.js 分镜卡角标

  **必过** `test_consistency`（**引擎恒不打分**：模块只有产料与回填两个出口｜角色项只取 `lineage.required_refs` 的 kind=character，
  场景/道具不进配对（与 design_refs 同源，不另写出场推导）｜**设定图路径必过 `ensure_local`**：
  OSS 模式下 sheet 是 URL，不 localize 就只会得到 missing（正反双验）｜**空 sheets 必带原因并成行打印**：
  skip_design / 显式空出场表 / 未建角色 / 出场名不在角色表 / 设定图未生成——静默空清单会被指挥层误读成「比对通过」
  ｜未生成产物的镜只计数跳过、坏源只记 frame_failed，绝不中断整章扫描｜drift+`--retake` 未锁定才置 retake、
  done 只留判定当标记（同 `lineage.mark_stale` 纪律），clip 漂移连 image 一并打回｜判定登记进 `_SHOT_HUMAN_KEYS`，
  并发 save 不被静默回滚｜**渲染物一被替换判定当场作废**：六道门逐门实测（生图直出/宫格·图生视频·
  直供·改造·换选·回滚），`audio` 阶段是空操作（重跑配音不改画面），`VISUAL_STAGES` 与 `frame_stage` 取值锁步，
  运行期间人工新落的判定仍以磁盘为准｜帧宽 ≤768 且**绝不放大**·scale 表达式逗号必须引号包裹｜lavfi 冒烟层零素材入仓）

- **改动面** `supply.py` `supply_image`/`_inspect`（供料体检闸位）· `pipeline/mediacheck.py` 供料段（`SUPPLY_MIN_COVERAGE`/`SUPPLY_ASPECT_TOL`/`image_info`/`has_alpha`/`cover_coverage`/`aspect_overflow`/`image_findings`/`inspect_image`）
  · `cli.cmd_supply --skip-check` · `studio` 三处（`actions.supply_shot_image` 传 ConfigStore / `server` 两个入口透传 skip_check / app.js 直供选择器勾选）

  **必过** `test_supply`（**低分/宽高比/alpha 一律告警不拦死**·**只有「ffprobe 解不出」硬拦**且给出 `--skip-check` 出路｜**`test_inspect_runs_before_archive`**：
  硬拦时旧图原地未动、版本栈没写、version 没涨（体检必须在 `versioning.archive` 之前）｜**`test_studio_path_also_inspected`**：
  `actions.supply_shot_image` 同样体检且画布基准到位（防体检回退到 CLI 层、网页侧完全不做体检）｜后缀闸仍在体检之前（非图片连 ffprobe 都不跑）
  ｜**`image_info` 必判 `width/height>0`**——实测文本改名成 .png 时 ffprobe 退出 0 且吐 0×0 的 video 流｜pal8 不判 alpha（tRNS 看不出，
  宁可漏报不误报）｜缺 ffprobe 退化为跳过而非判坏图）

- **改动面** **Studio 前端 ESM 全量源码**（`studio_app/app/*.js` + `studio_app/app.js`）——**改名/删码/搬函数/加分片都算改动面**

  **必过** `test_frontend_integrity`
  - 分析器 `tests/jsscope.py`，纯 stdlib 不需要 node：**悬空引用**——引用的名字必须有出处（本文件声明/import/浏览器内建）
    ，免构建的前端没有编译期，写错一个名字只在那行**真的被执行时**才炸成「加载失败」，而前端大量代码挂在「有了某个产物才渲染」
    的分支上，可以躺很久才第一次走到（改名漏改一处只在**已有成片**分支上的引用，
    用户合成完点开章节页就当场白屏）
  - **import 图解析得通**——文件存在且对面真的 export 了这个名字（具名 import 对不上是加载期错误，
    整个模块图挂掉＝全站白屏，不是某个视图坏掉）
  - **无孤儿分片**——app/ 下每个文件都得有人 import
  - **分析器自身必须有牙**（`TestAnalyzerActuallyBites` 逐条钉住必报与必不报的形态：悬空引用最小样例要报，
    解构/默认参数/类方法/可选链/标签模板/指数字面量/插值里套正则一律不许误报——分析器静默失效比没有更糟）
  - **保守是设计**：整文件平铺不建作用域树，宁可漏报绝不误报——误报会逼着后来人往 `jsscope.GLOBALS` 白名单里塞东西，
    白名单塞进过一次真 bug，这套守卫就等于没有

- **改动面** `ledger.viewLibrary` 片库工具条（项目下拉 + 检索框 + 渲染模式 chip · `galFilter` 回填）·
  `style.css` 的 `.lib-bar` 度量与**片库/待审队列/看板共用的检索框长度规则**

  **必过** `test_frontend_integrity`
  - **`TestLibraryFilterBar`**：项目维度走**与待审队列/看板/成本同一个 `uiSelect`**——一项目一枚 chip 在项目多起来后铺满两行，
    且四个页面各长一个样
  - **控件初值回填 `galFilter`**（它跨视图存活，不回填＝从别的页回来「筛选生效着、控件显示全量」
    ，看起来就是筛选坏了）
  - `.lib-bar` 两条度量必须在样式表里（`.us` 本体 width:100%，不定宽会被 flex 拉走；DOM 侧看不出来）
  - **检索框长度合成一条选择器**（`.lib-bar .fsearch, .kb-toolbar .fsearch`——三处搜同量级的东西，
    分两条写必然只改一处，同一个框在三个页面长短不一）

- **改动面** `core.js` 三张静态展示表（`REVIEW`/`MOTION`/`TRANSITION_ZH`）与 `export.py` 的 `motion_zh`

  **必过** `test_frontend_integrity`
  - `TestPresentationTablesMatchEngineEnums`：与引擎 `review.STATES`（键+中文 label）
  - `project._MOTION_MAP` 值集
  - `transitions.TRANSITIONS` label 逐项对拍——同步渲染的静态常量准许硬编码，代价是与引擎枚举锁步：
    缺一键（如 `omt`），弃用镜徽章就显示裸态名

- **改动面** **成本求和单一真源 `budget.spent_total`**：`business.chapter_ledger` 调用之·scanner 在 chapter/board/project_detail/overview 四路下发 `cost_total`/`cost_totals`·
  board 运营数字整块取 `chapter_ledger`（自算一份会让弃用镜沉没成本口径与成本页分叉）·前端只格式化（`fmtCost`）
  ，台账级数字零复算

  **必过** `test_budget`（`spent_total` 双闸共用）+ `test_business`（`chapter_ledger` 台账数学）——**下发键名与前端消费点无独立守卫**：
  改 scanner 负载字段（`cost_total`/`currency`/`cost_totals`）时人工核对 chapter/board/project_detail/overview 四路与 `fmtCost` 消费

## 9. 并发执行

并发的风险全在「错得悄无声息」。

- **改动面** `parallel.py`（并发执行层单一真源：`Task`/`Done`/`run`/`is_transient`/`resolve_workers`）
  · `cli.stage_gen_image`/`stage_tts`/`cmd_gen_refs` 的三段式（计划→并发→回填）

  **必过** `test_parallel`
  - **回调按提交顺序不按完成顺序**
  - workers≤1 零线程
  - 一件失败不拖垮整批
  - 瞬时重试而业务错误一次不重试
  - 产物已在盘走幂等护栏不重复付费
  - should_stop 停派
  - **工作线程零文档写**（save/add_cost/mark/review/lineage 一个都不许出现）
  - **记账排在登记之后**（add_cost 先入账再抛，反了会丢掉已付费那张的登记）
  - 首镜强锚必须单独串行
  - **串行与并发产出同一份文档**
  - gen-video 并发为显式 opt-in（缺省串行·不吃环境变量·retries=0·4K 强制串行）
  - 三阶段派活前置 wip/收尾恢复（Studio 忙态数据源）
  - 轮询心跳按墙钟节流

## 10. 契约、持久化与人类表态

人工表态与契约字段一旦被机器覆写，用户的判断就白做了。

- **改动面** **Agent Gateway 与计划式写入**：`agent_gateway.py` 的最小 context / contract discovery / ChapterPlan validate+apply / `add|update|omit|restore` 语义操作 / 内容 revision CAS / 非阻塞章节短锁 / provenance / explain / 章级失效只看生效值（写明引擎推导缺省不触发重做与锁） ·
  `cli agent contract|context|plan|explain`

  **必过** `test_agent_gateway`
  - context 白名单不泄密
  - registry 字段全在 project schema
  - validate 字节零写
  - 未知字段/实体/profile/非有限或非正时长/非法现有镜头/无效变化/未消费 note 零写
  - 镜级与章节级 review done 锁保护
  - PromptSpec 投影不双存
  - add 单调镜号
  - omit/restore 只走 review
  - 成功追加 provenance
  - revision 冲突与锁冲突文件字节不变
  - explain 检出 contract/compiler/spec/skill/profile/reference 漂移
  - CLI 四入口
  - **`lines[]` 白名单**（`line_list` 类型：`text` 必填非空、说话人/音色/情绪/英文
    对位可选、engine-managed 的 `dur` 与拼错键拒收——镜内多角色逐句换声的唯一
    形态，白名单不收它，长镜里的对白交换只能并进 narration 一把声音念完；
    `review.STAGE_FIELDS` 同步登记 `lines → audio/clip`）

- **改动面** `storage/mysql.py` 的 `_SCHEMA` 加列

  **必过** `test_providers_request` 的迁移幂等用例 + 同步 `_MIGRATE_COLUMNS`（**给 project.json/章节文档加新字段通常不需要动这里**——`kn_chapter.data`/`kn_shot.data` 是全量 JSON 列且 upsert 带 `data=VALUES(data)`，
  新字段自动落库；只有要 SQL 直查（跨项目看板类）才加列，那时才同步 `_MIGRATE_COLUMNS` + 迁移幂等用例 + 重导 `docs/sql`）

- **改动面** `storage/local.py` `_read`/`project_exists`/`list_projects` · `errors.DocumentCorruptError`（**损坏与缺失是两种状态**：
  损坏抛错、缺失返 None；存在性按文件在不在判，损坏项目仍算存在，清单跳过坏条目不整体失效）

  **必过** `test_workspace`
  - `delete_chapter` 连同 `.lock`/`.oplock` 一起删（`test_delete_chapter_removes_its_lock_files`）
  - `TestCorruptDocumentFailClosed`：损坏章节 load 抛错且同 ID 创建拒绝、文件原样保留
  - 损坏项目保 ID
  - 清单幸存；`TestAtomicDocumentWrite.test_read_raises_on_corrupt`

- **改动面** `storage/mysql.py` 读路径的冲突协调（`list_projects`/`load_project`/`load_chapter`/`load_settings` 与 `_row_newer`）
  · `cli.cmd_db_pull` · `studio/scanner.workspace_summary`（开首页第一条读路径）

  **必过** `test_workspace`
  - `TestMysqlReadCoordinationSingleSource`：**库行较新时 `list_projects` 必须库为准回写本地、
    绝不拿本地旧文档上行**（覆盖会连 `updated_at` 一并写旧，事后无从检测；`_upsert_project` 连带的资产清理还会删掉另一台机器新建的角色/声纹）
  - 文件较新时仍上行入库——冲突判据只许 `load_project` 一份实现
  - 离线打桩 `_db`/`_exec` 不连真库

- **改动面** `storage` 的存在性判据（`Storage.project_exists`/`chapter_exists` ·
  `LocalStorage` 的文件特化 · `MySQLStorage` 的查库覆写）· `workspace.Series.create_chapter` 的建章闸

  **必过** `test_workspace.TestMysqlExistenceCoversTheDatabase`
  - 库中有行而盘上无文件时判「已占用」——`project/` 是 gitignored 工作副本，
    按文件判存在会放行同名新建，`_upsert_project` 随即覆盖 data 列、
    `_sync_assets`/`_sync_voice_casts` 连带删光资产与音色档案行
  - `create_project`/`create_chapter` 被拒且不留半个项目目录
  - 建章闸必须走 `store.chapter_exists`，不得裸用 `cf.is_file()`——那条会与
    `scaffold_episodes` 的 `store.load_chapter` 判据分叉

- **改动面** `storage/mysql.py` 参与「新者赢」判据的 `updated_at` 列取值（`_sync_at`）

  **必过** `test_workspace.TestMysqlSyncClockSingleSource`
  - `_upsert_chapter` 与 `save_settings` 的 SQL 里不得出现 `NOW()`：该列要与本地文件
    `st_mtime` 比大小，而 PyMySQL 还回的 naive DATETIME 恒按客户端时区解释，
    混进 MySQL 会话时钟就整体偏一个时区差，两个方向都会覆盖掉较新的一侧
  - 写入值与 `_row_newer` 闭环：同刻写下的文件判「不算新」、一小时前的判「库更新」
  - 派生行（asset/chapter_asset/shot）的 `NOW()` 只是登记时刻，不在此约束内

- **改动面** `workspace.Series.save`/`commit` 的文档写锁 · 长窗口写者的两段提交
  （`cli.cmd_gen_refs` · `cli.cmd_cover` · `refine.refine_asset`）

  **必过** `test_workspace.TestSeriesDocumentWriteLock` + `test_parallel`
  - 写锁必须是**跨进程**文件锁：Studio 把生成类操作派成 `python -m kinema` 子进程
    （`studio/jobs.spawn_cli`，无队列无准入），进程内 RLock 对它们之间的竞争无效
  - `commit()` 块内嵌套 `save()` 不得自锁——flock 按打开文件描述判归属，
    同线程二次申请会自己等自己
  - 三条长任务的收尾必须锁内重读后按身份合并，源码里不得再出现整份 `save()`；
    `gen-refs` 为两段提交（计划期归档 + 回填期合并），归档条目须随计划期落盘，
    否则回填期的重读会丢掉它、下次归档按 `len(hist)+1` 重编号撞上已有归档文件名

- **改动面** `cli.stage_music` 的 BGM 幂等判据 · `project.audio.bgm_params` 快照

  **必过** `test_mix`
  - `TestBgmStaleness`：**时长与时间轴不符必须重生**（旧曲 aloop 铺满片长会在成片中段出现「淡出到静音再淡入」
    断层，而日志打印的是新参数）
  - 相符不重复计费
  - 参数快照落 project.audio

- **改动面** `pipeline/transitions.py` 的 `spec_of` 色彩语义（`_CARD_COLORS` 成对档 / `neon` 独立字段）
  · `compose._clip_cache_name` 淡化键

  **必过** `test_transitions`
  - **`--color` 三种归宿各归各位**：字卡族底/字成对换档（只覆盖 bg=白底白字）
  - scan=独立霓虹字段（挤占 bg 则尾帧退化字卡渲整屏纯色）
  - wipe 不消费

  **并过** `test_mix`（**淡化秒数两位小数+底色都进片段缓存键**：0.25 与 0.2 不共键、换底色必换键）

- **改动面** `pipeline/cover.py` `cover_prompt(aspect)` 方向词 · `_cast(names=)` 显式阵容 ·
  `cli.cmd_cover` 逐比例拼提示词与章节封面记账 · `--cast` 点名/排除（错名硬拦）

  **必过** `test_cover`
  - **构图方向词随画幅**（竖版/横版/方形/EN）
  - 两条路都 `aspect=asp` 逐比例拼
  - **章节封面费用必须 `add_cost` 入台账**（不入账则额度闸对封面失明且外溢到后续生成）
  - 系列封面无台账只进汇总打印
  - `TestCoverCast`：显式点名全收且按给定顺序、被撵走的角色外观全文不进提示词、
    `--cast none` 空阵容不留悬空标点、错名硬拦——缺省 `role` 只排序不筛人，
    desc 的否定句压不住引擎注入的阵容句，撵人只有显式点名这一条路

- **改动面** `cli.cmd_studio` 的 store 形态 · `studio_app/app.js` 渲染过期守卫 · `core.withBust`

  **必过** `test_config_center`（`TestStudioEntryStore`：**必须传 ConfigStore.shared**——load() 冻结快照钉进 serve 闭包，
  运行期新画风建得成项目、章节页 500）+ `test_shell_layout`（`TestFrontendRaceGuards`：**await 后核对 routeKey**、
  迟到 startPoll 不装定时器、`refreshAfterWrite` 判 pid/cid·**缓存穿透接符只许 withBust 一份**（云端直链无查询串，
  裸拼 &t= = OSS 404 永久裂图））

- **改动面** 面向用户的模块级异常（versioning/cover/decisions/consistency）

  **必过** `test_delivery`（`TestUserFacingErrorsAreKinemaErrors`：**中文用户提示必须抛 KinemaError 子类**——main() 只友好化它，
  ValueError 在命令行吐裸栈；刻意不给 main() 开 ValueError 通用通道）

- **改动面** `workspace.create_chapter` 的 `narrator_voice` 继承（`narrator.voice`
  立档值优先，系列顶层直写 `narrator_voice` 兜底——只认前者时顶层键是静默死键，
  旁白落回 profile 默认且无提示）

  **必过** `test_workspace.TestNarratorVoiceInheritance`（顶层键到章 + 立档值优先）

- **改动面** `workspace.create_chapter` 的 skill 继承 · `skills.voiceover_default(profile, skill)`

  **必过** `test_workspace`（建章拷贝 skill）+ `test_config_drift`/`test_variation`（**样本必须能区分 skill 位打没打中**：
  anime 派生 sparse、绑 kn-showcase 得 lead——两边同值的样本删掉形参照样绿）

- **改动面** **`project.DEFAULT_ASPECT`（默认主比例单一真源）**· `workspace.create_project` 缺省比例/平台 ·
  `deliver.build_delivery` 平台闸

  **必过** `test_workspace`
  - `TestCreateDefaultsHorizontal`：**未指定比例恒 16:9 横屏、平台不做默认绑定**（lastcar 实案：
    models.yaml 无人消费的 `defaults.aspect: "9:16"` 死键 + 建项默认 `["douyin"]` 诱导指挥层建成竖屏，
    `--both` 双出时 16:9 成片由竖图裁切构图全毁）
  - 显式值不被覆盖
  - 章节继承同口径
  - **源级扫描：引擎内比例兜底禁写字面量**，一律引用 `DEFAULT_ASPECT`；`TestDeliverRequiresPlatform`：**未绑定平台抛明确领域错误**，
    绝不静默打成抖音交付包

- **改动面** `cli.cmd_project_list` 的逐字段容错

  **必过** `test_workspace.TestProjectListRobustness`（存量项目缺 `title` 之类字段时
  整张列表不许被一条 KeyError 打断——列表是排查入口，坏一条就全瞎）

- **改动面** `docs/kinema/project.schema.json` 契约字段（新增读写字段必须同步 schema）

  **必过** `test_schema_contract`（**显式清单 `DECLARED_FIELDS` 驱动**：路径存在 + `[engine-managed]` 归属标注 + 不许再造 `shot_size` 与 `framing` 重复；
  每批把本批字段追加进清单。刻意不做全量 description 校验——存量 38 项无 description）

- **改动面** `locking.py`（`FileLock`/`save_lock`/`op_lock`——章节写入协调单一真源：文档写锁阻塞包住「合并→原子写」
  、操作锁非阻塞+进程内可重入、锁随句柄关闭自动释放）· `project.py` `save`（锁内合并写盘）
  /`mutate`（表态基线=磁盘现状，锁内校验基线未变才提交、变了重放）· `cli._stage_wrapper` 章节操作锁准入 + `cli._op_locked`（versions-rollback/supply/watermark/pick/previz-register/sketch-gen/previz-build/batch-edit/batch-undo 与 `cmd_refine` 的分镜分支同闸；
  `cmd_run`/`cmd_assemble` 整段收锁；表态类命令不占操作锁；重入键含线程号与真实路径）·
  `agent_gateway._ChapterLock`（先持操作锁，再与 save 共用同一把文档写锁·毫秒级占用有限重试吸收）· `studio/actions._mutate`（set_review/add_comment/update_comment/save_audio_script/sketch_guide 五端点）
  + `actions._exclusive`（rollback_version/rollback_output_version/switch_score_segment/refine_image/supply_shot_image/pick_image 与
  set_effects/previz_save/previz_set_v2v/transition_add/transition_remove/set_watermark/set_subtitle_style/set_shot_refs 锁内装载——锁先于装载，
  装载后再锁拿到的仍是过期副本；派子进程的端点在锁外派）

  **必过** `test_locking`
  - 同进程双句柄互斥
  - op 锁嵌套重入且外部仍拿不到
  - 冲突报持有者 kind
  - **mutate 以磁盘现状为基线**（引擎回填字段不被表态写回旧值）
  - **基线变了重放且竞争双方字段都保留**
  - fn 抛错零写盘
  - 被占章节第二个操作准入即失败
  - **直连命令在触碰文档前拒绝**（含 batch edit/undo、pick、previz register、refine）
  - Studio `_exclusive` 同判；transition_add/set_effects/set_shot_refs 被占即拒、空闲放行
  - 操作锁跨线程互斥、同线程重入

  **并过** `test_agent_gateway`（文档写锁占用时 apply 零写盘；操作锁占用时 apply 零写盘）

- **改动面** `project.py` `_DOC_HUMAN_KEYS`/`_SHOT_HUMAN_KEYS`/`_DOC_APPEND_KEYS`（人类表态三方合并的两层登记面 + 追加型审计字段）
  · **`override_runtime`/`save` 的运行时覆盖还原窗口**（`--motion/--aspect/--effects` 等 flag 只作用于本次渲染）
  · `cli._apply_aspect_args`/`cmd_animatic` · `compose.build` 的 `has_file(src) and project.uses_seedance` 取材闸

  **必过** `test_review`
  - 边渲染边审片不丢表态：磁盘人工表态赢
  - 逐镜 versions 只增不减
  - **运行时覆盖不落盘**：磁盘 motion 一字不改而内存仍是覆盖值
  - 键原本不存在则还原成「没有」而非写 None
  - 连跑多比例每次都还原
  - 源级禁裸写 `project.data[`
  - **kenburns 下即便片段在盘也不取**

  **并过** `test_decisions`（追加型走并集不走整键替换）

- **改动面** `studio/server.py` 单例收场：`serve` 的 SIGTERM 优雅接管（`--restart`/`--stop` 换班以 0 退出，
  不被外层后台任务记成失败）

  **必过** `test_studio_routes`（`TestGracefulSigterm`：处理器抛 SystemExit(0) + serve 接线在位）

- **改动面** `studio/server.py` 的 `engine_fingerprint`/`_engine_stale` 与 `_json` 亮牌注入（**常驻进程 vs 磁盘引擎代码错配检测**：
  serve 启动记 boot 指纹，盘上 *.py 领先即在任意 API 响应注 `engine_stale`）· `index.html` 顶栏贴条 ·
  `core.js` `api()`/`flagEngineStale`

  **必过** `test_studio_routes`
  - `TestEngineStaleDetection`：指纹只认 *.py 且随 mtime/size 变
  - 非 .py 与 `__pycache__` 不惊动
  - boot 指纹→`_json` 唯一出口的接线在位

  **并过** `test_shell_layout`（`TestEngineStaleBanner`：贴条元素/`api()` 单点消费/样式三件同时在位——缺任何一件都不报错，
  合起来才是一条能被看见的提示）

- **改动面** **跨工具 agent 文档布局**：`AGENTS.md` §7 阅读地图 ↔ `docs/agents/` 详情层 · `.claude/skills/` 单源实体（正文原地编辑·
  frontmatter/skill.json 由编译器按 manifest 维护）· `.agents/skills` 别名 · 宿主指针

  **必过** `test_delivery`
  - `TestAgentDocsAreSingleSourced`：**阅读地图与详情层双向对齐**——地图指到的 `docs/agents/*.md` 必须在盘上、
    每篇详情文档必须被地图索引（详情层整目录从根 `agents/` 迁入 `docs/` 的判例：漏改不报错，
    只是导航静默断链或新文档没人读到）
  - kinema/SKILL.md 行预算与 references 双向对齐
  - 实体唯一在 `.claude/skills/`
  - `.agents/skills` 别名指向
  - frontmatter 规范符合性

  **并过** `test_agent_system`
  - manifest 登记与发现目录双向对齐
  - **`agent/skills` 不得复活**——那正是被收敛掉的第二份正文拷贝
  - `check` 的 digest 抓「改了正文/references 没重新 compile」

- **改动面** **Studio 章节写路径的删态总闸**：`actions._gate`（`_load`/`_mutate`/`_exclusive` 三个装载口）
  走 `Workspace.get_project`——storage 的 `load_chapter`/`chapter_path` 不查 `is_deleted`，
  只靠它们装载＝软删项目仍可经 `/api/review` 等端点写入（前端 `.ro-deleted` 只拦页面点击，
  拦不住 API）

  **必过** `test_studio_routes`
  - `TestDeletedProjectRejectsChapterWrites`：软删后表态拒绝
  - 恢复即可写
  - **凡触碰章节装载原语（chapter_path/load/mutate）的函数源级必过 `_gate`**（新增旁路同样红，
    不点名装载口）；`TestStudioNeverImportsCli`：studio 域不得反向 import cli——共享实体一律下沉领域模块（先例 `sub_cfg`、
    `score_reconcat`）

- **改动面** `storage/base.chapter_status`（章节状态推导单一判据：workspace 清单 · mysql 索引列 · studio scanner 三处消费同源）

  **必过** `test_workspace`（清单状态经 `Series.chapter_status` 走共享判据）——**无防内联回抄守卫**：
  判据变更只改 `storage/base` 一处，改后人工确认 workspace/mysql/scanner 三处消费仍在转调

- **改动面** `studio/scanner.workspace_summary` 的展示序（`created_at` 倒序 = 新建在前；`/api/projects`、
  总览与侧栏项目树共用同一份，存储层仍按 id 排）

  **必过** `test_workspace.TestWorkspaceSummaryOrder`
  - id 升序与时间序不一致时按时间倒序
  - 缺 `created_at` 的老文档垫底不报错
  - 同秒创建靠稳定排序保留 id 升序

- **改动面** **工作区路径单一真源**：`workspace.find_workspace` 的仓库根/`engine/`/历史 `engine/project` 归一 ·
  `cli.cmd_studio` 的项目列表与片库扫描根 · `storage.Storage.root` 的 local/mysql 共用根

  **必过** `test_workspace.TestWorkspaceDiscovery`
  - 真实构造残留 `engine/project`
  - 当前与另一份源码检出的显式入口
  - 定制目录不改写
  - local/mysql 同根

  **并过** `test_studio_routes.TestStudioWorkspacePath`
  - 仓库入口归一
  - 自定义工作区保留
  - 默认片库根对齐
  - 显式 `--root` 不改写

- **改动面** **Agent 原生生图工单闭环**：`providers/image/agent.py` 两态 provider · `cli.stage_gen_image` 的普通图/逐比例图/候选图统一验收登记；
  已登记 URL 仍按 checkpoint 视为产出

  **必过** `test_providers_request.TestAgentImageOrder`
  - 缺图开全工单
  - 单图/逐比例/候选图完成后零成本 ingest 并写 `gen.image`/待审
  - 同 path 工单去重
  - 已登记 URL 无 provenance 也不重开工单

- **改动面** **`config_overlay.file_secrets`（密钥文件读取唯一口：`secrets.yaml < secrets.local.json`）**·
  `config_overlay.ensure_secrets_yaml`（缺文件时从随包模板生成）· `models.ConfigStore.load` 三条出口 ·
  `storage.load_storage_config` 的 MySQL 密码 · `storage/media._media_config` 的 OSS AK/SK ·
  `cli.cmd_setup`

  **必过** `test_config_center`
  - `TestSecretFileSingleReadPath`：**源级——除 config_overlay 外没有任何模块把 secrets.yaml 直接交给 `_read_yaml`/`safe_load`/`read_text`/`open`**（谁各读各的，
    向导/网页/`config secret` 写的 `secrets.local.json` 就整份被跳过，表现成「网页填了 OSS key，
    上传还说缺密钥」，还会把 PyYAML 这个可选依赖变成密钥能否读到的开关）
  - MySQL 密码与 OSS AK/SK 都必须看得见 `secrets.local.json`
  - 四级优先级 env > local > yaml 且仅 yaml 时不回归——**这条同时是「别给两份密钥文件加同步」
    的行为闸**：一旦有人让编辑 yaml 回写 local.json，local 压过 yaml 的断言当场红（两份文件不同步是设计，
    理由写在 `config_overlay` 模块头与 `config/README.md`）；`TestSecretsTemplateIsAutoProvisioned`：
    缺文件从模板生成且**注释一并带过来**（注释就是每把 key 的申请地址）
  - 幂等不覆盖用户已填的值
  - 模板缺失不抛错
  - **随包 `secrets.example.yaml` 必须全空**（它是入库文件，任何非空值都是泄漏）

- **改动面** `storage/snowflake.py`（全仓数据库主键唯一生成点）

  **必过** `test_snowflake`
  - 并发唯一
  - 时钟回拨不倒退（NTP 校时回拨时钉在上一毫秒按序列续发）
  - 毫秒内序列耗尽借下一毫秒——绕回即重复主键，要等 mysql upsert 两行变一行才暴露

- **改动面** **DEVELOP.md 全景地图**（引擎五段模块表 · §四前端模块 · §六测试清单 · §五命令表 · 全文反引号仓库路径）

  **必过** `test_delivery`
  - `TestDevelopMapMatchesRepo` 三重对拍：模块/测试清单与代码树**双向**比对（多写少写都红，
    落地当天即抓到漏列的 `test_locking.py`）
  - 命令表逐条喂 argparse 双向比对
  - 反引号路径逐个查存在——该文件自称「被守卫钉住」若无对应断言就是空话，恒绿的假闸比没有闸更坏

  **并过** ；`.agents/skills` 别名不变量另进 `agent_assets.check`（本地 check 即验）

- **改动面** **README 中英「工程结构」树**（新读者认路的第一张图；实测漂移形态：`agent/` 整个目录长期缺席、
  `studio_assets → studio_app` 改名后树里留旧名、`tools/` 两个脚本只列了一个）

  **必过** `test_delivery`（`TestReadmeLayoutTreeMatchesRepo`：按缩进还原父级后逐条查存在（嵌套改名同样红，
  `project/` 作为 gitignored 运行期目录豁免）＋ **中英两棵树同形**——各自维护，改一棵漏另一棵是这里最常见的失手）
  。**只钉路径与树形，不钉注释里的数字**：按 AGENTS.md §6，README 数字不设脆弱断言，画风档/子命令/模块数改动后靠人工刷新

## 11. 创作层：小说 · 改编 · 读片

指挥层的取料面。共同纪律是「引擎只出可测量量，判断留给人」。

- **改动面** `novel.py`（原创小说创作层单一真源：`save_chapter` 登记/归档 · `set_digest`/`set_state` ·
  `thread_add`/`thread_mark`/`thread_set` 伏笔状态机与 `THREAD_TIERS` · `arc_upsert`/`arcs_view`/`arc_at` 卷纲 ·
  **`strip_markup`/`markup_stats`（markdown 剥离＝文体面单一入口，账目面绝不走）** · **`prose_stats`/`PROSE_SLOP`/`PROSE_BANDS`/`PROSE_RULES`/`mattr`/`band_findings`/`repeat_phrases` 文体量化** ·
  **`baseline_metrics`/`_style_drift` 自基线 z 分** · **`_pacing_findings` 节奏账（opt-in）** ·
  **`bible_sections`/`pick_bible` 宪法分节取料** · **`log_add`/`log_view` 创作日志** · **`sweep` 七层检索** ·
  **`normalize_markup`/`normalize`（正文排版规范化：剥非面板加粗·面板按`【】`覆盖率 ≥0.6 判·
  迭代到不动点·刻意不碰 `---`）** · **`reindex`/`revert`/`version_files` 稿件重登记与章级回滚** ·
  **`style_update`/`bible_set` 文风与宪法写路径** · **`export` 按登记章序合并** · `brief` 写前必读包 / `recap` 批次复核物料 ·
  `lint` 跨章体检（文体面窗口化）· `view` scanner 只读视图）· `workspace.sync_design_to_chapters` 的文字人设四件＋`keywords`＋`status`（进 `char_fields`）
  · `upsert_entities`（四件与 status 绝不登记）· **`workspace.set_character`/`set_prop`/`set_named_scene` 与三张 `*_SETTABLE` 白名单**（设定实时更新的唯一写路径）
  · `cli` 的 `character add|set|show`/`prop set`/`scene set`/`_list_arg` 与 `novel` 全部子命令＋`_print_arc_body` ·
  `studio/actions.novel_thread` · scanner `script_detail` novel 块/`novel_chapter` · **`.claude/skills/kinema-novel/{SKILL.md,references/*.md}`**（文档里的命令与参数必须真的存在）

  **必过** `test_novel`
  - 登记幂等不叠版本
  - 旧稿移动归档且路径工作区相对
  - manuscript 无 `_work` 后缀
  - **伏笔超期与卷进度态恒派生绝不落盘**
  - lint 纯计算 project.json 字节级不变且各类 code 齐发
  - **文体扫描必须有窗**
  - **口癖榜有上限 `SLOP_TOP`**
  - **复读句必须最大延伸**
  - `PROSE_SLOP`/`PROSE_BANDS`/`PROSE_RULES` 每条必带物理化改写建议
  - **对白识别认直角/弯/直双三套引号**
  - **纯数字窗口不进复读榜**
  - **`brief` 里上一章 state 点到却没登记的名字必须过滤后当漏登记报**
  - 里程碑第 10 章触发
  - 实体命中 ≥2 字/keywords 口径
  - **四件与 keywords 进 sync 白名单 + upsert_entities 恶意携带也不覆盖**
  - **setter 走 `commit()` 两手柄并发不丢更新 + `sheet`/`ref_image`/`audition` 一律不可设**
  - `novel.view` 不 mutate 入参
  - `test_markup_strip_never_touches_the_fingerprint`（剥离绝不碰 chars/sha256——反了就是全书一次性判改稿）
  - `test_chapter_heading_is_not_a_paragraph`
  - **带区两侧都有闸**（零明喻报下限、刷屏报上限）
  - `test_mattr_is_length_stable`
  - **`UNIFORM_SD_RATIO` 不许再出现在 `lint` 里**（恒绿的闸比没有闸更坏）
  - 缺席按 `status` 过滤且折叠
  - 篇幅检查窗口化
  - findings 带章号定位
  - avoid∩slop 不双报
  - `manuscript_drift` 三码与 `reindex`/`revert` 往返
  - **sweep 七层各埋一词逐层对齐且零落盘**
  - **`novel.log` append-only 连 save 五次不翻倍且两手柄取并集**
  - **宪法分节无损**（各节 body 拼回等于原文·认不出节标回落整份）
  - `brief` 缺省不整份回灌且 `--bible` 点名一字不截
  - **`brief` 与 `arcs` 共用 `_print_arc_body`**
  - `style`/`bible` 双手柄并发不丢
  - baseline<3 章拒绝
  - **检查点/满档/复核窗口按章号派生**（样本用接盘书 51~63→70，章数口径给出的是反向区间与空窗复核）
  - **整窗全 0 的计数型指标不落基线**（μ=0/σ=0.001=恒响闸，还与 prose_bands 下限建议对喊）
  - 节奏账 opt-in 且**模块出口无任何合成分**
  - `export` 按登记序不按字典序
  - **伏笔定档走 CLI 全链路**（`thread-add --tier` 经 build_parser 落 tier 并按档推导 due——模块函数全对而 CLI 漏传实参时只有这种用例会红；
    `thread-set --tier` 与登记同一套推导、显式 due 优先、long 恒不造期限）
  - **`test_engine_never_prints_an_unparsable_command`**（引擎打印的每条 `novel <verb>` 喂 argparse）
  - **`TestSkillDocsMatchTheCli`**（SKILL 与 references 里的每条命令与参数都必须存在·引用只准一层深·
    主文件 <9000 字符）
  - **normalize 四条**（面板留粗体而以面板词开头的叙述必剥·`---` 与标题一个字不碰·`****嵌套****` 必须剥到不动点否则幂等当场失效·
    逐章旧稿必进版本栈·`--dry-run` 零写盘）

- **改动面** `adaptation.py`（Track A 切分 / `undecodable_ratio` 乱码闸 / `extract_epub`）· `workspace.py` Series 改编承接（`ingest_source`/`upsert_entities`/`upsert_chapter_outline`/`scaffold_episodes`/`moodboard` 风格垫图同步）
  · `project.moodboard_refs`/`ref_images`（参考库垫图注入·逐镜 refs 覆盖）· `workspace.moodboard_refs_for`（设定图逐张垫图解析·
  `cli.cmd_gen_refs --only`/`refine.refine_asset` 消费·`characters[].refs`/`props[].refs`/`scene_refs`）
  · `studio/actions` `toggle_moodboard`/`set_shot_refs`/`set_asset_refs`/`regen_asset_refs`（网页切默认启用/镜级勾选/设定图逐张勾选与按新垫图重生）
  · `studio/scanner.py` `script_detail`/`script_segment`（剧本工作台阅读器）· `workspace.set_graph`/`scanner._graph_view`（关系图谱落库 + 设定图挂载）

  **必过** `test_adapt`
  - Fountain/.fdx 解析
  - 小说章标切分
  - 窗口化
  - 指纹格式
  - **scaffold 显式 cid 章号==集号+回填 chapter_id**
  - **upsert_outline 幂等只写 outline 不碰 shots/review**
  - **upsert_entities 合并不覆盖
  - 保人工 voice/comments
  - keywords 取并集**
  - **decode_source 打分择优自动识别 UTF-8/GBK/Big5/UTF-16 并转 UTF-8
  - 乱码闸(U+FFFD+PUA+控制符)>25% 只拒真乱码/二进制不误伤中文**
  - **EPUB 解析 is_epub/extract_epub 纯 stdlib(spine 拼正文+nav/NCX 章标题·加密拒收)**
  - **script_detail 目录瘦身/script_segment 按段懒加载切片
  - 越界 None
  - fdx 无偏移给 note**
  - **clear_source 未建章可清/已建章硬闸拒绝不误删**
  - **参考库垫图：库项 {path,on}
  - 历史纯字符串归一
  - add/remove/set_moodboard_on 同步各章 style.moodboard（仅 on=True 生效集）
  - moodboard_refs 逐镜解析（镜有 refs→精确/[]→不用·否则默认生效集）
  - 建章继承默认集
  - toggle_moodboard/set_shot_refs 网页写路径
  - scanner 下发 on 态
  - 设定图逐张垫图 moodboard_refs_for（显式 list 精确·[]不用·None 默认集）
  - set_asset_refs 写角色/道具实体 refs 与场景 scene_refs（三态·未知名拒绝）
  - 具名取景地 refs 写 `scenes[]` 实体条目、全局 scene_refs 不动（与 `refine._asset_refs`/gen-refs 读侧同分派——写进全局的话重生时读不到，
    勾选形同虚设）
  - scanner 下发 refs（含逐 scene）
  - `project refs --only kind:名` 单张过滤重生**
  - **set_graph 整体替换+校验悬空边/重复 id/空节点
  - _graph_view 同名节点挂设定图 thumb+ref**

- **改动面** `study.py`（参考片读片：`parse_scene_cuts`/`parse_silences`/`rhythm`/`frame_times`/`media_meta` 纯函数层 + `cut_args`/`silence_args` 探测命令 + `ingest` 落盘）
  · `workspace.Series.study`/`study_dir`/`ingest_study`/`remove_study` · `cli` `study import/show/rm`

  **必过** `test_study`
  - **头号用例 `TestRelativePathOnly`**：`collect_media` 不收录参考片 + 对照组「绝对路径必被收」
    证明护栏是唯一防线
  - 产物路径无 `_work` 后缀
  - showinfo/scdet 双格式解析且 `duration_time` 不误当切点
  - 静音悬空 start 补到片尾
  - 切点越界丢弃
  - `rhythm` 不出任何判定键
  - 抽帧上限 48 均匀降采样
  - fps 分母 0 不写 inf
  - digest/契约 `json.dumps(allow_nan=False)`
  - 全表不进契约
  - 非视频拒收零副作用
  - 同 slug 重导幂等清残帧

- **改动面** `cli._settle_motion`（未表态章节的缺省档在本次作业内定档：`run` 与 gen-video 真发写入章节、tts 只覆盖本次）
  · `deliver.build_delivery`（`--out` 非空即拒，只清理引擎自建缺省目录）· `cli._archive_regen`（agent 路由待验收图不归档）
  · `voicecast.narration_shot`（镜级「要不要旁白 wav」单谓词：审阅闸、scanner 分母、board 共用）
  · `previz.v2v_shot`（吸收 guide 仲裁）+ `cli.stage_gen_video` 的 `v2v_on` 含 provider 能力位 · `_shot_plan` 的 previz 末帧仅 native
  · `review.CHAPTER_STAGE_FIELDS`（章级 done 锁表单源，含 audio 阶段与混烧/锚定字段）· `contracts.json` motion enum
  · `ffmpeg.filter_literal/drawtext_text/concat_entry`（filtergraph 两级解析的引号上下文编码，drawtext/ass/fontfile/concat 六处消费点共用）
  · `providers/_util.request_with_retry`（读超时缺省只对 GET 重试）+ `download`（临时名落地后 `os.replace`）
  · `lineage.retake_clip_for_image`（换画面六道门对存量片段的唯一处置）· `stage_gen_image` 候选不占画布态 / 盘上捡回登记 / `--accept-existing` 未变不退化
  · `compose.speech_spans_resolver`（kenburns 停顿窗口）· `actions.bind_config_path`（经 `KINEMA_MODELS` 下发子进程）
  · `seedream.cost_for`（按出图像素落档）· `models.resolve_video`（flag > 章节 `video_provider` > profile 链，三处消费）· `chapter set --video-provider`
  · `contracts.json` 白名单补引擎实读作者字段（镜级 delivery/voice_instruction/emotion_scale/voice/dialogue/attribution/rank/title/corner_note/bubble_pos/priority/anchor_frame/frame_chain，章级 frame_chain/scored_bgm/native_bgm/voice_anchor/cover_prompt/effects/subtitle/speech_rate）；context 章级可读面由契约派生
  · `agent_gateway._retake_stale_products`（update 改到的字段按 STAGE_FIELDS 把已产出未锁定阶段置 retake）· `batch.undo` 跳过已锁定镜
  · `providers/image/agent._accept`（交付物经 `mediacheck.inspect_image` 体检：不可读/比例不符拒收且保留工单；工单提示词漂移随 meta 登记）· `cmd_gen_refs` 待 agent 产图时非零退出
  · `Series.add_cost` + `business.project_ledger.totals.series`（设定图、系列主视觉、试音、资产局改、锚定预热入系列台账）
  · `mediacheck.verify_aspect` 的 `narration_missing`（有词章未登记旁白轨即硬判）· `compose.build` 两道混烧闸提到渲染前 · `compose.use_bgm_for`（BGM 三档唯一判据，`_bgm_gate`/`_stage_audio_bed` 共用）
  · `deliver._registered_cover`（交付包优先取 `cover` 产物，抽帧只作回落）· `stage_tts` 记录音色与解析音色不同即重合成 · `actions.set_shot_refs` 改垫图集合置 image retake（`STAGE_FIELDS.refs`）
  · `variation._motion_text`（多镜语法扫描面含运镜/delta/拍表）· `previz.register_previz` 查 clip 锁 · `voicecast.default_voice_ref`（默认音色回落链三处共用）
  · `prompts.video_prompt` 附板镜照发结构锁（实发验收见 `.claude/docs/901`）· Studio Range 后缀区间/非法值/零字节 · `_safe_media` 隐藏段按工作区相对路径判
  · MySQL 项目行 `updated_at` 缺失取 `_sync_at()` · Windows 阻塞锁真阻塞 · `oss sync --chapter` 过软删闸

  **必过** `test_motion_default`（三入口同经 `_settle_motion`；tts 后内存档位为 dubbed 且磁盘不写）· `test_deliver`（非空 `--out` 拒绝且原样）
  · `test_providers_request`（agent 返工轮验收；`--accept-existing` 未变保片段；候选批不动画布态、pick 才退化）
  · `test_review`（混烧章只拦旁白镜 audio）· `test_audioscript`（scanner 分母去弃用镜；镜级判据只有 `narration_shot` 一处）
  · `test_previz`（dubbed 不标 previz 末帧；guide=sketch 的 previz 镜不是孤岛）· `test_agent_gateway`（章级锁表含 clip/audio 新字段；motion 非枚举值拒收）
  · `test_ffmpeg_quoting`（真渲染：`%` `'` `:` 等在 -vf 与 -filter_complex 都渲出；ass/concat 路径含引号可打开；POST 读超时不重、GET 重；断流零半截文件）
  · `test_consistency`（直供/局改/回滚退化片段、锁定片段只交人裁决、盘上捡回登记）· `test_mix`（kenburns 停顿窗口）· `test_router_defaults`（`resolve_video` 三级优先）
  · `test_voicebank`（锁定镜已标同值不重计）
  · `test_agent_gateway`（update 置 retake 只对已产出阶段；契约可写面全部可读；枚举与范围校验）· `test_batch`（undo 跳过锁定镜）
  · `test_providers_request`（交付物比例不符/不可读拒收且工单保留；提示词漂移入 meta）· `test_business`（系列台账单列并入总额）
  · `test_verify`（有词章缺旁白轨硬判、无词章不判）· `test_mix`（BGM 判据只有 `use_bgm_for` 一份，scored 先于 native 短路）
  · `test_prompts`（附板发结构锁、V2V 不发）· `test_studio_routes`/`test_locking`（Studio 非表态写端点被占即拒）
  · `test_variation.TestPromptPronoun.test_quoted_dialogue_inside_picture_fields_is_exempt`（画面字段引号内的台词不判代词）· `test_variation.TestCameraClash.test_negated_camera_term_is_not_a_clash`（小句内被排除措辞否定的运镜词不计冲突）
  · `test_parallel.TestProgressFeedback`（有失败的批次收尾行分报成功/失败数，`run` 把失败数交给 `close`）· `test_sheets.PropSheetContractTests.test_brand_marks_are_forbidden`（道具标识须虚构中性图形）
  · `test_voicebank.TestCustomCount`（`voice custom --adopt N` 未显式 `--count` 时只生成 N 条）· `test_cover`（背景回落 `scenes[]`、写实档电影海报工法栈、章节命题回落 `theme`）

---

## 验证口径

无 CI/lint 配置。完整验证 = 单测 + `doctor` + `--mock` 离线全链路 + 真实小样（先 1 镜）。
