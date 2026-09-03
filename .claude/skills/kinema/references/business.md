# 交付与生意——算账 · 估值 · 交付 · 提案（详情层）

### 交付与生意（成片之后：算账 · 估值 · 交付 · 提案）
成片通过后按需走交付/生意命令（Studio「成本」页可视化同源数据）。**正式交付/发布前
按 `references/casebook.md` 的独立终检协议过最后一遍**（新上下文 subagent · 六组
检查项逐条带镜号证据 · 三态结论 · 禁全绿），机器四闸（lint/verify/consistency/spec）
先绿再送终检：
```bash
python3 -m kinema ledger <项目>                                  # 成本台账：预估/实际双轨 + 废片/重roll
python3 -m kinema project set <项目> --license exclusive         # 版权标记（进交付包 manifest）
python3 -m kinema deliver --chapter <项目>/<章节>                 # 交付包：成片+封面+双字幕+平台文案+manifest(AI披露/版权)→zip
python3 -m kinema export-pitch <项目>                             # 项目提案书（浏览器打印即 PDF）
python3 -m kinema export-review --chapter <项目>/<章节>           # 静态审阅包发客户
python3 -m kinema verify        --chapter <项目>/<章节>                  # 成片自审（零成本·只读）：黑屏/该响却哑/削波/响度/时长/字幕六项体检
python3 -m kinema consistency scan --chapter <项目>/<章节>               # 角色一致性产料（零成本）：代表帧+设定图配对清单 → **你**读图判定
python3 -m kinema consistency set  --chapter <项目>/<章节> --shot N --verdict ok|drift [--note ...] [--retake]  # 回填判定（引擎不打分）
python3 -m kinema watermark     --chapter <项目>/<章节> [--text "@频道"]  # 成片动态水印（防搬运，可选）
python3 -m kinema cover <项目> --all [--desc "本章画面描述"]              # 封面设计：系列主视觉+每章「第N集」（默认竖3:4+横4:3双套）
python3 -m kinema transition add --chapter <项目>/<章节> --after <镜id> [--text "几天后"]  # 转场镜（**仅用户开口才跑**）：无字=0.5s 极简黑场呼吸｜带字=1s 渐黑字卡（纯 Python 零成本）
python3 -m kinema study import <项目> --file 参考片.mp4 [--cuts 0.3] [--frames 24]      # 参考片读片（立项前门）：量切点密度/每镜时长/静音占比+等间隔抽帧（show/rm 同级；只读节奏不抄内容）
```
- **转场镜（零 API 成本）——缺省一个都不加，只有用户开口才插。**
  缺省档是一镜一片、镜间直接硬切，这是既定形态不是缺陷；替用户加转场既改了他没要过的
  节奏，也可能把本来干净的切换弄脏。**你不主动插、也不主动提议**（分镜写完不做"要不要
  加转场"的自检，lint 的 `scene_jump` 只是请人复核直切读不读得通，不是让你补转场）。
  下面这份类型表只在**用户点名要转场**时查用：
  在 shots[] 里插「转场镜」：`{"kind":"transition","dur":0.5,"narration":"",
  "transition":{"type":"fade_black","text":"几天后"}}`，观感即「画面渐暗 → 黑场（可带
  字卡）→ 渐显下一段」。**八种内置全部纯 Python 零成本**（无需 AI、无需预设素材）：
  字卡族——`fade` 极简黑场呼吸（无字缺省，总 ≈0.5s）｜`fade_black` 渐黑字卡（带字
  缺省，总 ≈1s，最黑处显「几天后」）｜`fade_white`（回忆/闪回·点名可选）——
  **缺省转场一律黑场系（无字 fade / 有字 fade_black），fade_white 属点名可选项**；
  **冻结帧 xfade 族**（取前镜尾帧+后镜首帧做静帧过渡，画面与切点像素连续）——
  `wipe` 对角翻页（色板沿对角**席卷**盖住旧画面再掀开新画面，`--direction tl/tr/bl/br`
  + `--color black/white`）｜`circle` 圆形开合｜`slide` 横向推移｜`blur` 柔焦叠化｜
  `scan` 轮廓扫描（霓虹亮条掠过、扫过处画面解析成发光轮廓线稿，`--color green/blue`
  + `--direction up/down`，科幻/系统觉醒/回忆闪回题材点睛）；
  `clip` 素材转场（**仅用户明确要求 AI 过场动画时**才先用 Seedance 生成无字转场视频
  存 `assets/transitions/` 再引用）。**转场自带短音效**（合成三色板 `whoosh`「呼」缺省｜
  `riser`「吸」蓄势·scan 缺省｜`boom`「咚」落点，另有纯外置扩展键 `swish` 轻扫/
  `deep` 重扫/`glitch` 故障/`shimmer` 微光·fade_white 缺省——按题材气质选配，
  `--sound off` 关闭），
  **声源三级解析**：外置音效库优先（`music/sfx/`＋`config/audio.yaml` 注册表，
  `python music/download.py` 一键拉 BGM+音效两套起始资产，音效为 CC0）→ 缺文件
  纯 ffmpeg 合成兜底（零素材离线可跑）→ 用户点名 AI 生成落库
  `sfx gen --kind <键> --yes`（ElevenLabs 付费）；`sfx list` 查注册表与就位状态。
  **用户开口之后**才用 `transition add --chapter <项目>/<章节> --after <镜id>
  --text "一天后"`（rm/list 同级），或让用户在 Studio 时间线点「＋转场」。
  插入后重跑 assemble 生效。空旁白自动静音占位，时间轴/字幕零错位。
  用户问「哪里适合加」时才按信号回答：① 叙事时间跳跃（次日/三年后）→ fade_black 字卡；
  ② 地点/场景切换 → fade_black 或素材转场；③ 进出回忆/梦境/闪回 → 缺省仍用黑场系
  fade_black，仅用户点名要白闪才用 fade_white；④ 情绪段落收束 → 无字纯色停顿。
  **与 storyboard.md「跨镜连贯性自检」的分工**：那边决定**不断开处怎么接得住**
  （视线/轴线/景别阶梯/光位/出入画），是你该主动做的；**要不要断开**是用户的取舍，
  那边判完发现"怎么接都跳"，如实说明即可，不要顺手插一个转场把它盖过去。
- **特效提议（模式 a 合成前主动问一次）**：a 模式（静图运镜）合成前，按题材给出
  推荐并列出编号菜单让用户选（可多选/换选/跳过，AI 推荐项标 ★）：
  ```
  可用氛围特效（共 14 个内置·回复序号，可多选）：
  ① light_sweep 斜光扫过（通用质感 ★）   ② fireflies 萤火虫（夏夜/田园/治愈）
  ③ sparkles 星辰（星空/魔法/梦幻·固定不动只闪烁）
  ④ rain 雨｜⑤ snow 雪｜⑥ fog 雾（带环境音）
  ⑦ 游戏层：hud 血条HUD / scanlines CRT扫描线（game_* 画风缺省自带）
  ⑧ 质感层：vignette 暗角 / film_grain 胶片颗粒 / bloom 泛光 / warm 暖调
  ⑨ 手作层：paper_grain 纸纹（静止卡纸纤维·纸艺/拼贴画风点名开）/ stopmotion 定格顿挫
    （12fps 拍二格·kn-clay 定格系强风格项，整片开或不开）
  ```
  推荐规则：画风缺省特效（profile.effects）打底，按题材再荐 1~2 个粒子层，
  **宁少勿多（总计 ≤3 层）·带环境音的多个（rain/snow/fog）别叠太多免声音混**；
  用户确认后写章节 json 顶层 `"effects": [...]`（覆盖 profile 缺省）→ 重跑 assemble 生效。
  用户点名特效时直接照办不再问。**特效目录/元数据真源 = `effects.EFFECT_META`/`catalog()`**；
  **Studio 章节详情「✎ 特效」选择器**也可让用户自己勾选换特效并重合成——只有匹配题材的画风
  才默认带特效，其余画风缺省不叠（干净直出），用户要就点名/前端加。
- **封面设计（系列 key visual + 章节封面，默认竖 3:4 + 横 4:3 双套）**：与字幕/水印同一条
  「本体无字」路线——模型只画**无字背景**（竖版海报主视觉构图：主角居中群像纵深、
  仰角机位、体积光粒子、底部三分之一留标题安全区），主标题（小说/动漫名，全系列不变）
  与「— 第 N 集 —」由 ffmpeg 排版**后置合成**。系列感三锚点引擎内置：同一排版模板 +
  章节背景以**系列封面背景**为首张参考图 + 同画风前缀同 seed——一眼看出同一部动漫的
  第几集。**你的职责**：设定集出来后先 `cover <项目>` 出系列主视觉给用户过目，
  通过后 `cover <项目> --all` 铺全章节；每章可在章节 json 写 `cover_prompt`
  （或 `--chapter chNN --desc "..."`）精写本章角色姿势/情绪——不写则引擎按章节标题
  自动拼氛围句。**比例铁律：缺省即竖 3:4＋横 4:3 双套全出，勿用 `--aspects` 缩成单版**
  （Studio 各处按容器形状自动适配——项目卡横幅/章节缩略取 4:3 横版、详情页主视觉取
  3:4 竖版，缺横版会退化为竖版硬裁切）；用户点名特殊比例才加 `--aspects 21:9,1:1`
  （短边 1080 自动推导）或 `--size 1200x1600` 直给像素；字体 `--font song|kai|hei|yuan`
  （宋衬线/楷古风/粗黑/圆体，缺省按画风自动——拒绝机械黑体）。产物 `assets/covers/`
  （无字 `*_bg_<比例>.png` 真源与成品并存，重生只需 `--force`）；Studio 项目卡/
  详情页头部（灯箱竖横双版可翻）/章节列表自动展示。
- **成片自审（出片后一道机器体检，零成本可选）**：`verify --chapter <项目>/<章节>`
  纯本地 ffmpeg 探测、**只读不改产物**，六项：黑屏（抽样帧 `YAVG≤20 且 YMAX≤24`——
  **已自动排除转场黑场窗**，`fade`/`fade_black` 字卡本身就是满屏纯黑）/ 该响却哑
  （整片 `mean ≤ -50 dB`；**native 无音轨必抓**——它不跑 TTS 也不叠 BGM，片段丢音轨
  被降级后成片没有任何音频兜底）/ 削波（`max ≥ -0.5 dB`）/ 响度（偏离 -16 LUFS 超
  ±3）/ 时长对不对得上分镜时间轴（容差帧量化 `max(0.5, 镜数/fps)`）/ 字幕条数
  （数 ASS 的 `Dialogue:` 行与旁白镜数比对）。**硬失败非零退出**（容器无效/黑屏/
  该响却哑/时长对不上/字幕缺失），削波·响度·字幕少条只记「待修」不拦。结论写
  project.json 顶层 `verify`。**默认不接进 `assemble`/`run`**——它是自愿闸，交付前
  或用户说「帮我检查下成片有没有问题」时跑一次即可（Studio 章节页「后期 → 自审」同款）。
- **动态水印（防搬运，成片后可选）**：`watermark` 给成片叠加**连续弹性漫游**的半透明文案
  （入场后全程在场、匀速漂移、碰画面边界随机角度反弹——位置连续且不可预测，
  裁切/delogo 无固定靶区）——**水印版 `<id>_wm_<比例>.mp4` 与无水印原片
  并存**（发布用水印版、留档/交付用原片，自选）。文案解析链：`--text`（当场输入）>
  project.json 顶层 `"watermark"`（项目默认）> `config/branding.yaml` 的 `watermark.text`
  （全局默认）。成片确认后**问一句用户要不要打水印/用什么文案**即可执行；路线 seed 确定性
  派生，重打同款、幂等跳过。纯本地 ffmpeg 零 API 成本。
- **发布文案前置**：节点①写文案时顺手填 `script.per_platform.{平台:{title,caption,hashtags}}`
  （deliver 直接取用；缺省回退 hook/body/cta）。
- **卖断交付**：`setup`（安装向导；`--check` 验收自检）· `config/branding.yaml`
  白标（名称/口号/主题色）。

### 发布
不承接：成片交付即终点，分发、平台合规与排期都不在集群范围内，由用户自行上传。
