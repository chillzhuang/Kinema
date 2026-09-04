---
name: kn-book
description: "制作说书、荐书、拆书、书评或读书笔记短片。用户要求讲解一本书、做书单号或使用微信读书取料时使用；优先通过微信读书官方 Agent Gateway 获取书籍信息与热门划线，再组织痛点、精华和行动结构。"
metadata:
  kinema-managed-by: "agent/manifest.json"
  kinema-kind: "route"
  kinema-status: "stable"
  kinema-version: "2.0.0"
  kinema-owner: "Kinema"
  kinema-source: "workspace"
  kinema-trust: "first-party"
  kinema-digest: "sha256:6c67026f6c1d00d5090e01a6a66c3682a0460fa3713978259b2efe349b94c50a"
---
# kn-book · 图书说书（book explainer / 荐书带货）

**把一本书的精华做成片。** 说书是被市场反复验证的重赛道（董宇辉一句推荐让
《额尔古纳河右岸》加印 600 万册；书单号挂车佣金 30~50%），它的生死线是
**书里的具体内容**：观众要听到"书里怎么说"，而不是"这本书真的很好"。
本 skill 的独门供应链是**微信读书官方接口**——热门划线就是几万读者投票出的
金句榜，选材不用猜。

## 直接启动整套流程（`/kn-book <书名或主题>` 即可）

被调用后**按此联动执行，不需要用户再调用任何其他 skill**：
1. **Read [`../kinema/SKILL.md`](../kinema/SKILL.md)**（完整节点工作流与铁律）。
2. 立项：`project new --title "X" --id <英文词> --profile book`（skill 自动派生 kn-book）
   → `project set <id> --skip-design`（说书无固定角色；要打造常驻虚拟说书人 IP
   时才建 character 走常规设定图）。
3. 默认竖屏 9:16（抖音/视频号书单号与带货主场）；深度拆书系列投 B站/YouTube
   用 `--aspect 16:9`。旁白写声线描述定制：`voice custom <id> --narrator --prompt "<声线描述>" --adopt 1`
   （说书腔描述用词：磁性、中低音、语速中等）；要剪映同款官方「磁性解说」再
   `voice audition <id> --narrator` → `voice use`。
4. 动态化不需要：**a 模式（kenburns）即完整体**，全程零视频 API 成本。
5. 全自动 `run` 收尾会自动出系列主视觉与本章封面，画面命题按章节 `cover_prompt` > `theme`
   回落（`--desc` 只有 `cover` 命令有）——所以 **run 前把 `cover_prompt` 写进 ChapterPlan**
   （按下文《图书封面专项协议》写主体、关系、环境、光线与标题安全区）；副标题缺省「第 N 集」，
   要作者或本集裸标题就在 run 前先跑 `cover <id> --chapter <cid> --subtitle "…"`，run 收尾
   见封面已在盘即跳过；已出过的加 `--force` 只重生该章，系列主视觉不动。

### 章节命名硬规则（不可例外）

`chapter.title` 只写本集的内容标题，不写章节序号。序号由章节 `id/order` 和封面排版层管理，绝不把编号混进标题。

- **禁止**：`第二章：嘉靖为什么不上朝`、`第2章 嘉靖为什么不上朝`、`第2集：嘉靖为什么不上朝`、`卷二·嘉靖为什么不上朝`，
  以及任何同义的中英文编号前缀或后缀。
- **正确**：`嘉靖为什么不上朝`、`沉默才是嘉靖的权力`。
- 建章前先把候选名规范化为“裸标题”；如果用户或素材带了 `第N章/集/回`、`第二章/集/回`、`卷N`、`Episode N` 等编号，
  去掉编号及其分隔符后再写入。去掉后为空时，重新创作一个剧情钩子式标题，不能退回 `第一章`、`第二章` 这类占位名。
- 建章后必须核对章节详情中的 `chapter.title`，并在交付前再次检查标题与 `project.json` 登记表一致；
  发现编号前缀立即修正，不得带着错误标题继续生图、合成或交付。

旁白试听、选音色、候选画面定稿或章节审阅如果要让用户在网页完成，先执行 base skill 的
[Studio 交互交接协议](../kinema/references/studio-handoff.md)，自动启动/复用控制台并给出
`项目 → 章节`入口，再停下等待用户；不要只说“请启动 Studio”。

## 取料协议（三级降级，引擎零联网——检索全部发生在指挥层）

**① 微信读书官方 Agent Gateway（有 `WEREAD_API_KEY` 时首选，合规正道）**：

**每次立项先探测 key（三处都查，探测与使用全程绝不回显 key 值）**。少查一处就会
把「用户已经配好了」误判成没配——`secrets.local.json` 正是 `setup` 向导、网页
配置中心和 `config secret` 三个入口的落点，且优先级高于 `secrets.yaml`：

```bash
test -n "$WEREAD_API_KEY" && echo env-ok || echo env-missing          # ① 环境变量
python3 -c 'import json,sys,pathlib
p=pathlib.Path("config/secrets.local.json")
d=json.loads(p.read_text()) if p.is_file() else {}
b=d.get("secrets") or d
sys.exit(0 if str(b.get("WEREAD_API_KEY","")).startswith("wrk-") else 1)' \
  && echo local-ok || echo local-missing                              # ② 本机密钥文件
grep -qE '^WEREAD_API_KEY:\s*"?wrk-' config/secrets.yaml 2>/dev/null \
  && echo secrets-ok || echo secrets-missing                          # ③ 仓库密钥文件
```

**三处都缺不阻塞、也不静默**：**直接走 ② 公开页取料把活干下去，同一轮回复里附一次
配置提示**。不要停下来等用户表态，也不要每轮复读提示——**同一会话提示一次即可**，
用户说过不配就不再提。提示要**如实交代差异**：公开页照样有推荐值、评价人数、出版社
与目录（事实四锚点能闭环），没 key 真正丢的是**完整热门划线榜、划线处读者想法、
中差评分档、资深会员推荐率和书架/笔记/阅读统计**——即拆书最吃劲的选材与判据。
提示内容就是下面三步：

1. 登录 https://weread.qq.com/r/weread-skills 申请个人 API Key（`wrk-` 前缀，免费，绑定微信读书账号）；
2. 写入任一处（**都不需要用户手工建文件**）：
   - `cd engine && python3 -m kinema config secret WEREAD_API_KEY wrk-...` —— 落
     `secrets.local.json`，优先级最高，文件由引擎自动创建；
   - 或直接编辑 `config/secrets.yaml` 的 `WEREAD_API_KEY:` 那行（该文件由
     `setup` 从模板自动生成，已在 `.gitignore`）；
   - 或 fish 固化 `set -Ux WEREAD_API_KEY wrk-...`。
3. 回到对话说"配好了"，AI 重新探测即接入。

**官方 weread-skills 包不自动安装**（本协议 curl 直连已闭环，装与不装取料能力
等价）：首次配好 key 后**问用户一句**要不要顺带装官方封装（价值在做视频之外的
日常用途：书架管理/笔记导出/阅读统计），用户点头才装、且装到**全局**
`~/.claude/skills/`——第三方产物绝不放进本仓库的 `.claude/skills/`（会进 git）：

```bash
curl -L https://cdn.weread.qq.com/skills/weread-skills.zip -o /tmp/weread-skills.zip \
  && unzip -o /tmp/weread-skills.zip -d ~/.claude/skills/
```

取用写法（**按引擎同一优先级 env > local.json > yaml 取**，借 shell 变量中转，日志零泄漏）：

```bash
WRK=$(python3 -c 'import json,os,pathlib,re
v = os.environ.get("WEREAD_API_KEY", "")
p = pathlib.Path("config/secrets.local.json")
if not v and p.is_file():
    d = json.loads(p.read_text()); v = (d.get("secrets") or d).get("WEREAD_API_KEY", "")
p = pathlib.Path("config/secrets.yaml")
if not v and p.is_file():
    m = re.search(r"^WEREAD_API_KEY:\s*\"?(wrk-[^\"\s]+)", p.read_text(), re.M)
    v = m.group(1) if m else ""
print(v)')
```

探测通过后，指挥层直接 Bash curl 调用（单端点 POST，接口靠 `api_name` 分派）：

```bash
curl -sX POST "https://i.weread.qq.com/api/agent/gateway" \
  -H "Authorization: Bearer $WRK" -H "Content-Type: application/json" \
  -d '{"api_name": "/store/search", "keyword": "纳瓦尔宝典 纳瓦尔·拉维坎特", "scope": 10, "count": 3}'
```

**取料六步链路（照打即可，不要自己摸索接口）**：

```text
1. /store/search  keyword="书名 作者" scope=10  → 核对回包 title 再取 bookId
2. /book/info     bookId    → 事实四锚点一次拿全 + intro 定主题
3. /book/chapterinfo bookId → 目录定骨架
4. /book/bestbookmarks bookId（chapterUid 不传）→ top20 热门划线金句
5. /review/list   bookId reviewListType=1/4/2 → 正面·中立·差评三档口碑
6. /book/readreviews  用第 4 步的 (chapterUid, range) → 金句处读者真实反应
```

- **热门划线是本 skill 的王牌素材**：划线密度＝读者共鸣图谱，被划最多的句子
  就是市场验证过的金句，直接进精华点；回包 `chapters[]` 给出金句所属章节，可口播出处。
- **事实四锚点由第 2 步闭环**：`/book/info` 一次给全 title/author/publisher/publishTime/
  isbn/newRating（千分制，`836`=83.6%）/newRatingCount，不必再去公开检索交叉验证。
- **三条硬闸**（细则与判据见 [`references/weread-gateway.md`](references/weread-gateway.md)）：
  `newRatingCount < 300` 时推荐值不可采信；`/review/list/mine` 参数名是小写 `bookid`；
  `/store/search` 有累积配额（约 20 次触 `-2014`），**搜索间 sleep ≥1s、一本书只搜一次**。
- **Gateway 没有榜单接口**——飙升/新书/神作/总榜走公开网页 `weread.qq.com/web/category/*`
  （WebFetch 可直接解析），Gateway 负责给榜单书补推荐值与划线。
- 用户自己的书架与笔记是**独家素材**（"我今年划线最多的一本书"人设向选题），但
  **空号是常态**：`/user/notebooks` 返回 `totalBookCount: 0` 是正常回包不是报错，
  先探测再承诺，别把用户不存在的阅读史写进文案。

接口分级（17 个逐个实调）、十条试错陷阱、错误码字典与数据化三过滤判据，全部在
[`references/weread-gateway.md`](references/weread-gateway.md)。`/_list` 自述与实际有多处
不符（`/book/similar` 恒废、`scope` 八选二、嵌套层级），**以该 reference 为准，不要照抄自述**。

**② 无 key 降级（探测缺 key 即自动走这条，不等用户确认）**——四步照打：

```text
1. WebSearch "<书名> <作者> 微信读书"  → 同时捞出两个直达 URL（见下方陷阱）
2. WebFetch  weread.qq.com/web/bookDetail/<v>   → 书名/作者/出版社/出版年/
                                                   推荐值%/评价人数/简介/目录
3. WebFetch  book.douban.com/subject/<id>       → ISBN/页数/豆瓣评分/星级分布/短评正文
4. 榜单      weread.qq.com/web/category/{rising,newbook,all,general_novel_rising}
```

- **两套评分口径交叉验证**（微信读书推荐值 % 与豆瓣 10 分制），比单一来源硬。
- **陷阱一**：`bookDetail` URL 里的 `<v>` 是不透明 token，**bookId 拼不出来**
  （`/web/bookDetail/<bookId>` 恒 404），只能靠 WebSearch 捞现成链接。
- **陷阱二**：豆瓣**搜索页**是 JS 渲染，WebFetch 抓不到结果；**条目页无反爬无登录**，
  必须 WebSearch 拿到 `subject/<id>` 后直达。
- **拿不到**：完整热门划线榜（公开页只挂"去 App 查看全部"）、读者点评正文、
  中差评分档。金句只能退回自己转述观点，不引原句。

**③ 用户直给**：点名书目、贴自己的读书笔记、自供实拍书影。

无论哪级，**事实四锚点必须核查后写进 `script`**：书名＋作者＋出版社/年份＋
评分或印量（说书失败多在张冠李戴）。引擎不联网是铁律，取料只在指挥层。

## 选题协议（三过滤）

- **选题源**：微信读书榜单（飙升/神作/新书，走公开网页）→ 热点勾连（影视化上映、
  社会情绪、游戏带火的文化母题）→ 用户书架反选（留言想要什么书单就拆什么）。
- **三过滤，全过才立项**：① 有痛点共鸣（书回答了一个观众正在疼的问题）；
  ② 有反常识增量（书里至少一个"你以为 X 其实 Y"）；③ 有可执行行动（观众今天
  就能做的一件事）。三者缺一 = 只能夸书不能拆书，换书。
- **有 key 时三过滤用回包字段判，不靠感觉**：热门划线 top5 多为祈使句/练习题
  → 拆得动；多为抒情金句 → 只能氛围荐书。反常识增量到 `reviewListType=4/2`
  的中差评里找（正面书评只复述，争议点在中差评）。判据全表见
  [`references/weread-gateway.md`](references/weread-gateway.md)。
- 带货向选书 20~50 元价位转化最好（市场口径）；冷门佳作配"首印仅 N 册"
  式反差钩子。

## 精华提炼方法论（拆书公式）

读料顺序：**简介定主题 → 目录定骨架 → 热门划线定金句**。然后压进固定结构：

1. **痛点钩子镜（≤3s）**：把书的核心议题翻成一句扎心问题或反常识结论——
   "你不是懒，你只是被这本书说中了"，绝不以"今天推荐一本书"开头。
2. **书卡镜**：书名＋作者＋一句定位（"一个硅谷投资人的人生算法"）。
3. **精华点 ×3**：每点＝**场景故事**（书中案例讲成 15 秒小故事）＋**金句**
   （热门划线原句）＋**行动**（今天就能用的一步）。一点讲不透就砍，绝不摊平。
4. **收尾升华＋CTA 镜**：回扣钩子一句话，行动号召要具体——"这本书在下方链接/
   评论区，先看第三章"，别用万金油"点赞关注"。

**引用纪律（版权红线）**：narration 以**你自己的转述**为主；直接引用原文单条
≤50 字、全片 ≤3 条，口播带出处（"书里写道……"）。大段搬运原文＝侵权；
金句超长时压缩转述，观点保留、句子重写。

## 视觉语言 DNA（书卷静物＋概念意象双语言）

profile 前缀已带暖纸书卷基因，逐镜 `image_prompt` 只写差异：

- **锚点镜（书卡/开场/收尾）**：书房暖光静物——精装书、台灯、咖啡、亚麻桌布，
  定全片调性。
- **内容镜（精华点）**：概念隐喻插画——把观点画成可视场景（"注意力是资产"→
  `一枚金色沙漏立于书页翻开的山谷之间，沙粒是细小的金币`）。忌真实人物肖像，
  作者用象征物（书桌/钢笔/剪影）。
- **金句镜**：大留白氛围画面，金句进 `narration` 由字幕逐字承载（音字一致铁律），
  **不把金句烤进图**——长句进图必出错字。
- **书影纪律**：**AI 不复刻真实书封**（封面是出版社版权物，模型也写不对书名字）。
  两条路：① 生成"意象书卡"（无字精装书+主题意象环绕）；② **实拍书影 `supply`
  直供**（带货片需要真书露出——自拍或用出版社授权物料，登记为分镜画面与 AI
  意象镜混排，正是市面书单号的标准形态）。

## 分镜法则

8~14 镜，45~90s：钩子 ≤3s → 书卡 1 镜 → 每个精华点 2~3 镜（故事镜+金句镜）→
行动清单 1 镜 → CTA 1 镜。`narration` ≤30 字/镜（口播密度）；`emotion` 给钩子、
金句、收尾镜标注。运镜安全档：`缓慢推近`（金句凝视）、`拉远揭示`（书卡全貌）、
`凝视呼吸`（静物停留）——书卷气质忌快运动，进阶运镜（▲/■ 档）不用。
转场**不主动加**（缺省无转场，精华点之间直接硬切）；用户点名要分段字卡时才插，
本档合用 `fade_black`（"第二把钥匙"）或 `wipe --color` 配暖纸色。

## 声音设计

- 音色描述用词按书选：**磁性解说**（通用说书）· **悬疑解说**（故事性强的小说/
  历史）· **深夜播客**（情感疗愈/散文）——写进 `voice custom --narrator --prompt … --adopt 1`
  定制立档，一个账号锁一把声音即品牌声纹；要官方模版音色再 `voice audition` → `voice use`。
- **金句前留气口**：金句镜 `delivery.pause_before: 0.6`（kenburns 下生效，
  折进 dur）——说书的呼吸感全在金句前那半秒。
- BGM calm 低音量钢琴/lofi；语速中等（比知识解说慢半档，说书要"讲给你听"
  不要"念给你听"）。

## 系列化与商业化

- **图书封面专项协议**：图书封面不是“古风人物 + 城市背景 + 一行大字”的泛用海报，
  而是一个缩小后仍能读懂的出版物信息封面。每张封面先确定一个视觉命题：一个核心书籍
  对象、一个冲突关系、一个环境层；历史/政治类优先使用权力关系的象征物（空置龙椅、封口
  奏疏、朱批、账册、宫门、棋局、秤与印），不要无依据地生成英俊主角肖像来代替书的观点。
  大主体不超过 1 个，辅助主体不超过 2 个，装饰纹理不能抢主标题和视觉命题。
- **信息层级**：章节封面主标题必须是微信读书核实过的实际书名（如《大明王朝1566》），
  可用一行小字补作者（刘和平）或本集裸标题（嘉靖为什么不上朝）；项目品牌名只能做很小的
  角标，不能覆盖书名。章节序号属于元数据和封面组件，不得写进 `chapter.title`，也不能
  代替书名。封面上的书名、作者、章节标题必须与脚本事实一致，禁止泛化成“图书解说”。
- **构图与缩略图**：默认 3:4 竖版 + 4:3 横版双套；3:4 画面上半部承载视觉命题，底部约
  20%~28% 保留干净的标题安全区，标题不压脸、不压关键道具、不贴边。至少做一次 84px
  缩略图检查：缩小后仍能分辨书名轮廓、主色对比和唯一主体；缩小后只剩“一个人站在场景里”
  就判为失败，重写视觉命题而不是继续堆粒子。
- **色彩与字体**：一主色 + 一撞色，标题只用一种主字体和一种强调色；中文标题优先后置排版
  （`--typeset-title`，字体从 song/kai/hei/yuan 选择），不让模型直接生成书名。背景必须留
  出真实的低密度地板，标题与背景保持足够对比，禁止白字黑描边的默认贴图感。
- **系列与章节分离**：系列封面可以表达“图书解说”品牌，但章节封面必须有自己的书名、视觉
  命题和无字背景。封面生成后逐项核对 `project.cover` 与 `chapters/<cid>.json.cover`：
  章节应有独立 `*_bg` 和成品文件，路径不能指向 `series_*`；若项目同时承载多本书，而引擎
  只能从项目级标题排版，不能静默沿用品牌名，应先按单书建项目或补齐章节级封面标题能力，
  不得把泛品牌封面当作章节封面交付。
- **两段式封面流程**：先生成无字 key visual，再以无字图为唯一画面基准后置排版；章节背景
  可以参考系列无字背景承接色板，但必须在 `desc` 中写出本章独有的视觉命题，至少包含主体、
  关系、空间、光线和标题安全区。生成后同时 Read 原图与缩略图，检查错字、信息层级、主体
  是否单一、标题是否清晰；任一失败就重做封面，不带问题进入合成或交付。
- **本章示例**：`《大明王朝1566》` 的章节封面应围绕“皇帝不露面却控制所有人”构图：
  空置龙椅作为核心、封口奏疏和层层宫门形成权力链、朱红印记作为唯一撞色，画面留出标题区；
  不应使用无来源的男主近景、泛古城和“图书解说”大字。
- **系列封面**：`cover` 命令三锚点（同版式+系列背景参考图+同前缀同 seed）锁系列感；
  系列品牌名与期数由排版层后置，不靠模型写字，但不得因此替代章节实际书名。
- **变现组合拳**（市场验证口径）：视频挂车＋评论区置顶＋主页橱窗，佣金 30~50%；
  发布侧合规与排期不在集群范围内，成片交付后由用户自行上传。

## 成本口径

生图＝镜数 ×¥0.3 ＋ TTS 按字；视频 API **零调用**。60s/10 镜典型 ≈ ¥3~5；
接官方 Gateway 取料零成本（个人 key 免费额度内）。

## 重做意见词典（review retake 用）

| 症状 | `--note` 建议写法 |
|---|---|
| 出成了真实书封复刻 | `改为无字精装书意象书卡：合上的布面精装书+主题象征物环绕，书脊无文字` |
| 插画太具象/像课件 | `改为单一隐喻场景：一个视觉主体承载观点，留白40%，暖纸色调` |
| 出现真人面孔 | `人物改为背影或剪影，或用书桌钢笔等象征物替代` |
| 画面冷硬无书卷气 | `加入暖光台灯与纸张织物质感，色温压暖，对比度放柔` |
| 静物太摆拍 | `加入使用痕迹：翻开的书页、压痕便签、喝了一半的咖啡` |

## 何时不用

讲概念不讲书 → `kn-explainer`；纯金句卡 → `kn-quote`；TopN 强榜单感书单 →
`kn-ranking`（序号徽章字幕）。
