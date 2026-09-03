# 微信读书 Agent Gateway 实测手册

`kn-book` 取料协议 ① 的接口真源。**全部结论来自 2026-08-22 对 17 个接口的逐个实调**，
不是照抄 `/_list` 自述——自述与实际有多处不符，照抄必然反复试错。

## 端点与调用

```bash
# 取 key 必须按引擎同一优先级 env > secrets.local.json > secrets.yaml。
# 只 grep secrets.yaml 会漏掉向导/网页/`config secret` 写入的 secrets.local.json，
# 表现成「明明配好了却说没 key」。
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
curl -sX POST "https://i.weread.qq.com/api/agent/gateway" \
  -H "Authorization: Bearer $WRK" -H "Content-Type: application/json" \
  -d '{"api_name": "/book/info", "bookId": "3300052405"}'
```

单端点 POST，接口靠 `api_name` 分派，其余参数平铺在同层。key 全程走 shell 变量中转，
不回显、不进日志。多接口连调用 Python 包一层 `call()` 比串 curl 稳。

## 接口分级（17 个全测）

| 级别 | 接口 | 结论 |
|---|---|---|
| **主力** | `/store/search` | 定位 bookId；`scope` 仅 0/10 可用（见下） |
| | `/book/info` | 事实四锚点一站齐 + 完整评分分布 |
| | `/book/chapterinfo` | 目录定骨架 |
| | `/book/bestbookmarks` | **热门划线金句榜——本 skill 王牌** |
| | `/review/list` | 正反口碑分档 + 资深会员推荐率 |
| | `/book/readreviews` | 金句处的读者真实反应 |
| **用户态**<br>（数据随账号，先探测再用） | `/shelf/sync` | 书架，实测 114 本，字段含 `finishReading`/`category` |
| | `/readdata/detail` | 阅读统计，`mode`=weekly/monthly/annually/overall 四档均可用 |
| | `/user/notebooks` | 笔记本概览；**空号返回 `totalBookCount: 0`，不是报错** |
| | `/book/bookmarklist` | 我的划线；同上，空号返回空数组 |
| | `/review/list/mine` | 我的想法；**参数名陷阱见下** |
| | `/book/getprogress` | 阅读进度，含 `progress`/`readingTime`/`summary`（当前位置摘要） |
| | `/discover/interact/type3` | 朋友在读书卡；`book.score` 是另一套 0–100 口径，≠ `newRating` |
| **低价值** | `/review/single` | `/review/list` 已含正文，仅在要评论/点赞明细时才用 |
| | `/book/underlines` | **不含划线文本**，只有 count/score/range，实用价值近零 |
| | `/book/recommend` | 个性化推荐，但 `newRating` 恒 0，要评分须二次 `/book/info` |
| **废** | `/book/similar` | **恒返回 `-2003`**，换三本书复验均失败，不要再试 |

**Gateway 没有榜单接口。** 飙升/新书/神作/总榜只能走公开网页
（`weread.qq.com/web/category/{rising,newbook,all,general_novel_rising}`，WebFetch 可直接解析）。
Gateway 的价值是给榜单书**补推荐值与热门划线**，不是替代榜单。

## 十条试错陷阱（照做可零试错）

1. **`/review/list/mine` 的参数名是 `bookid`（小写 i）**，全 Gateway 唯一不遵循 camelCase 的接口。
   用 `bookId` 直接 `-2003`，报错信息会明说 `缺少必填参数: bookid`。
2. **`/book/bestbookmarks` 必须 `chapterUid=0`（默认值，别传）**。传具体章节 UID 返回
   `items: []`，而 `totalCount` 仍是全书数，极易误判成"这章没人划"。单章热门划线拿不到。
3. **`/review/list` 正文是双层嵌套**：`reviews[i].review.review.content`，
   `star`/`author` 也在最内层；`reviewId`/`likesCount`/`commentsCount` 在中间层
   `reviews[i].review`。只剥一层会看到 `star=None`、`content=None`，误判成"列表不返回正文"。
4. **`scope` 八选二**：`0`（全部）和 `10`（电子书，默认）返回真条目；`6`(作者)/`13`(书单)
   **静默退化**成等同 `0`；`12`(全文)/`14`(听书) 只回 `scopeCount` 计数、`books` 字段整个缺失；
   `2`(公众号)/`4`(文章) 直接 `-2041`。想要全文检索原句——**没有**，只能靠 bestbookmarks。
5. **搜索按书名易串书**。`鹅之书` 曾搜出同作者的 `我该走了吗`。**keyword 一律写「书名 作者」**，
   并核对回包 `title` 再取 `bookId`。
6. **评分分布只有 `/book/info` 给**。`/store/search` 回包的 `newRatingDetail` 只有 `{title}`
   一个字段；`/book/info` 才给 `{good, fair, poor, recent, deepV, myRating, title}`。
7. `/book/info` 的 `response_fields` 声明里有 `wordCount`，**实际回包没有这个字段**。
8. **`/book/chapterinfo` 前几章是噪声**（封面 / 版权信息 / 内容简介，wordCount 个位数），
   取骨架时从 `wordCount` 上千的章节起算。
9. **限流 `-2014` 是累积配额，不是突发阈值**。`/store/search` 实测约 20 次触顶；触顶后
   等 ~25s 恢复。`/book/info` 类只读接口 12 发无间隔全过、不受限。
   → 纪律：**搜索类调用之间 sleep ≥1s，全片取料把搜索压到 5 次以内**（一本书只搜一次）。
10. **错误码字典**：`-2003` 参数错误或接口已废 · `-2014` 频率超限 · `-2041` 该 scope 不支持。
    错误码在 HTTP 200 的 body 里，必须判 `errcode` 才知道成败，不能只看 HTTP 状态。

## 说书取料标准链路

一本书六步，除第 1 步外都不受限流影响：

```text
1. /store/search   keyword="书名 作者" scope=10 count=3   → 核对 title 取 bookId
2. /book/info      bookId                                  → 事实四锚点 + intro 定主题
3. /book/chapterinfo bookId                                → 目录定骨架（跳噪声章）
4. /book/bestbookmarks bookId （chapterUid 不传）          → top20 金句，王牌素材
5. /review/list    bookId reviewListType=1 / 4 / 2         → 正面·中立·差评三档口碑
6. /book/readreviews bookId chapterUid=<4的> reviews=[{range:<4的>,maxIdx:0,count:5,synckey:0}]
                                                            → 金句处读者真实反应
```

**第 2 步一次拿全事实四锚点**：`title` + `author` + `publisher`/`publishTime` +
`newRating`（千分制，`836` = 83.6%）与 `newRatingCount`，另有 `isbn`、`category`、`intro`。
SKILL.md 要求的四锚点核查到此闭环，不需要再去公开检索交叉验证。

**第 4 步回包结构**：`items[]` 每条含 `markText`（划线原文）、`totalCount`（划线人数）、
`chapterUid`、`range`；`totalCount`（顶层）是全书划线总数。回包另有 `chapters[]` 给
`chapterUid → title` 映射，**可以标注每条金句出自哪一章**，直接进 narration 的出处口播。

**第 6 步是隐藏金矿**：拿第 4 步某条金句的 `(chapterUid, range)` 去查，返回
`bookMarkCount`（该处划线人数）+ `totalCount`（该处想法数）+ `pageReviews[]`，
每条含 `content`（读者原创想法）与 `abstract`（被划原文）。实测《人生十年》
「请列举10项你现有的身份资本」一处：12816 人划线、928 条想法，读者把作业直接贴在下面
——**这是"可执行行动"最硬的证据**，比任何书评都能证明这本书拆得动。

## 用数据跑三过滤

SKILL.md 的三过滤（痛点共鸣 / 反常识增量 / 可执行行动）可以直接用回包字段判定，不靠感觉：

- **样本闸（先过这道）**：`newRatingCount < 300` 时推荐值不可采信。实测见过新书
  91.8% 但只有 65 人评——冷启动噪声，不是好书信号。够不够格看人数，不看百分比。
- **可执行行动**：`bestbookmarks` top5 若多为祈使句/练习题（"请列举…"、"先试着…"、
  "拆分的诀窍有三种…"）→ 拆书片能成立；若多为抒情金句 → 只能做氛围荐书，
  硬拆会变成"只夸不拆"。这条判据比读简介准得多。
- **反常识增量**：`/review/list` `reviewListType=4`（一般）与 `=2`（不行）里最常出现
  "推翻大众误区""其实没那么神"式提炼——正面书评只会复述，**争议点在中差评里**。
  `star` 三档：`100`=推荐 · `60`=一般 · `20`=不行。
- **口碑真伪**：`/review/list` 回包 `deepVRecommendInfo` 给资深会员口径，实测形如
  `{title: "1118 个资深会员点评", subtitle: "其中 935 人(83.6%)推荐本书"}`。
  资深会员推荐率与大众推荐值背离时，以资深会员为准。

## 无 key 降级路线的实际边界（取料协议 ②）

同日实测公开页，**降级没有想象中残**——事实四锚点能闭环，丢的是选材与判据：

| 数据 | 有 key | 无 key（公开页） |
|---|---|---|
| 书名 / 作者 / 出版社 / 出版年 | `/book/info` | ✅ `bookDetail` 页有 |
| ISBN / 页数 | `/book/info` | ✅ 豆瓣条目页有 |
| 推荐值 % + 评价人数 | `/book/info` | ✅ `bookDetail` 页有（实测 83.6% / 3060 人） |
| 第二套评分口径 | 无 | ✅ 豆瓣 10 分制 + 星级分布（实测 7.9/1361 人） |
| 目录骨架 | `/book/chapterinfo` | ✅ `bookDetail` 页有（实测 19 章） |
| 评分分布 good/fair/poor/deepV | `/book/info` | ❌ |
| **完整热门划线 + 划线人数** | `/book/bestbookmarks` | ❌ 只挂"去 App 查看全部" |
| **划线处读者想法** | `/book/readreviews` | ❌ |
| **中差评分档（star 20/60）** | `/review/list` | ❌ 点评正文整块不露出 |
| 资深会员推荐率 | `/review/list` | ❌ |
| 书架 / 笔记 / 阅读统计 | 用户态四接口 | ❌ |
| 榜单排名 | **本来就没有** | ✅ 公开榜单页，两边一样 |

**降级两条陷阱**：① `bookDetail` URL 的 `<v>` 是不透明 token，`bookId` 拼不出来
（`/web/bookDetail/3300225591` 实测 404），只能 WebSearch 捞现成链接；
② 豆瓣**搜索页** JS 渲染抓不到结果，**条目页无反爬无登录可直抓**——必须先
WebSearch 拿 `subject/<id>` 再直达，别去 fetch `douban.com/search?q=`。

**降级后的创作代价**：金句没有划线人数背书 → 选材从"读者投票"退回"自己判断"；
中差评拿不到 → 反常识增量只能靠读简介和目录推，三过滤的②③两关都变软。
**代价不构成阻塞**：缺 key 直接按上表走公开页把活干完，同一轮附一次配置提示即可
（同会话不复读）。但**降级产出必须标注口径**——金句是转述而非划线原句、
选材依据是简介与目录而非读者投票，别让降级结果冒充有 key 的成色。

## 用户态素材（人设向选题）

`/shelf/sync` + `/user/notebooks` + `/readdata/detail` 支撑"我今年划线最多的一本书"
这类人设向选题。**但空号是常态**：实测账号书架 114 本、笔记 0 条、年度阅读仅 2 天。
**先探测再承诺**——`totalBookCount: 0` 是正常回包不是报错，拿不到笔记就换选题源，
别把用户不存在的阅读史写进文案。`/readdata/detail` 的 `preferCategoryWord`
（形如「偏好阅读影视原著」）只在 `mode=overall` 出现，weekly/monthly 没有。
