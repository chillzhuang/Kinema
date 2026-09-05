# 3D 预演与参考视频 V2V 易错点全集

**实现真源** `previz.py` · `pipeline/camera.py` · `studio_app/director/`

> 带圈小节编号是对外引用锚点——`stage-console.md` 等处按 `previz-v2v ⑦` 形式引用，改号即断链。

## 1. 产物落点

### ① previz 绝不写进 `shots[].clip`

compose 把 `clip` 当最终成片素材直接播，写进去就是把无材质灰模当成片交付。

它只存 `shots[].previz`；前端分镜卡也只当角标——点开是预览灯箱，不是成片位。

### ② 首帧覆盖 `image` 是有条件的

该镜已有图时**默认不覆盖**：灰模盖掉精修图不可逆——虽有版本栈可回滚，但用户下一步就会拿灰模去烧
Seedance。要覆盖须 `--use-first-frame`。

`done` 锁定镜显式覆盖会报错并给解锁路径；auto 档则只说明，不拖垮整条登记。

### ③ 末帧与衔接链争同一个槽，每镜二选一

优先级（只在 `cli._shot_plan` 定死一次，dry-run 与真发共用，**别处不许重算**）：

1. previz 末帧优先——它是这一镜自己的终态位姿，比「下一镜的分镜图」更贴近本镜编排；
2. 否则用衔接链给出的下一镜图；
3. **开了 V2V 就一帧都不发**——seedance 的 V2V 分支发的是 `reference_image + reference_video`，
   根本没有 `last_frame` 槽。

## 2. V2V 的开启条件

### ④ opt-in × 仅 native × provider 必须真支持

| 条件 | 理由 |
|---|---|
| 默认关 | **按 token 计费且输入视频秒同样入账**（5s previz ≈ 每镜多花 5 秒的钱），静默开启 = 静默改成本 |
| 只在 native 走 | dubbed 的对口型音频与运动迁移互相牵制；官方虽放宽互斥但没小样验证过 |
| 能力标志不可省 | `generate(**kwargs)` 会**静默吞掉**不支持的 `reference_video`——发出去的是一次 previz 完全没参与的普通首帧生成，而请求照常计费 |

### ⑤ 视频参考必须公网 URL

base64 / data-url / 本地路径对**视频**一律被 Seedance 拒（图片仍可 data-url）。

CLI 层经 `MediaStore.upload`（阿里云 OSS）预解析，provider 只透传并对本地路径抛错。

**刻意不做 data-url 兜底**——兜底只会把一个能在本地讲清楚的配置问题，换成一次服务端 400。

`--mock` 下跳过上云直接给本地路径：离线彩排不该逼用户配 OSS 密钥。

## 3. 渲染纪律

### ⑥ 渲染前必须切「洁净模式」

previz 会作 `reference_video` 直接喂给模型。画面里混进 gizmo 箭头 / 琥珀站位圈 / 路线样条 /
选中高亮（emissive），Seedance 会当成场景内容试着复现——成片里凭空多出彩色轨迹，选中的角色还会被
读成「另一个人」。

切换必须成对，且恢复写在 `finally`。

### ⑦ 全片预演 reel 是观看物，三条边界

实现 `previz.build_reel`。

1. **不进 `shots[].clip`/`output`**——那是成片与交付位；
2. **不喂模型**——V2V 恒**逐镜**发本镜那一段；
3. **指针不进契约**——章节顶层 `previz` 是编排快照的整体替换区（`save_scene` 只留 `_SCENE_KEYS`），
   写进去下次保存编排就没了。故清单落 sidecar `reel.json`，存在性由磁盘推导
   （scanner `_previz_reel_view`）。

落点仍在 `<work>/previz/`：片库只扫 `*_work/output/*.mp4`，放那儿不会被当成成片；不进契约也就不会
被 `collect_media` 传上 OSS。

归一目标取**众数规格**而非首镜——本仓库真有早期 Retina 未锁 pixelRatio 渲出的 4K 遗留片，按首镜
归一会被它把整条拖成 4K。

### ⑦.5 批量渲染必须串行

实现 `stage.js renderBatch`。

每镜的登记都是一个 `previz build` 子进程，它 load → 改 → save 章节文档。并发跑两个就是经典丢更新：
后写的那个以自己 load 到的旧副本为准，前一镜刚登记的 `previz`/`image` 凭空消失，且不报任何错。

故循环体里必须 `await waitJob(job)`（`pollJob` 的 Promise 包装）等该镜落盘再进下一镜。中止 / 失败
一律**不回滚已登记的镜**——重来的代价是逐帧重渲。

守卫 `test_batch_render_registers_one_shot_at_a_time`。

### ⑦.6 镜与镜之间画面必须活着

渲染循环让位的判据是 `S.exporting`（单镜逐帧导出），而**不是 `S.rendering`（整批）**。

后者会让镜间那段编码等待里循环也停，而 `exportFrames` 收尾的 `setSize` 恰好把画布清成透明黑，
表现就是「渲完一镜卡住一两秒 + 半黑屏」——A/B 实测：空档内 16 次采样 13 次整幅全黑 → 收窄后零全黑。

同理**洁净模式也只包单镜**：循环画 PiP 时每帧成对 `setExportMode(true→false)`，撑到整批会被循环
当场关掉（见 ⑩）。收尾必须 `resize()` 让循环立刻重画。

另两笔延迟：

| 项 | 处理 |
|---|---|
| `waitJob` | 要显式调快轮询——`pollJob` 缺省 1600ms 且首次检查也等满一轮，是给 Seedance 分钟级任务定的；而 previz 登记实测约 0.7s（子进程 0.1s + 编码 0.36s + 首末帧抽取） |
| `refreshChapter` | 整批只重取一次——它会 `paintAll` 重建三栏 DOM，逐镜刷只换来 ◈ 角标早几秒出现 |

实测镜间空档 1560ms → 720ms。守卫 `test_viewport_stays_alive_between_shots` /
`test_short_job_is_not_billed_the_long_job_poll_interval`。

### ⑦.7 等待期盖毛玻璃忙态蒙版

实现 `.dz-busy` + `ui.showBusy`，覆盖 `.dz-stage` 整个工作台。

光把循环救活还不够：那段里画面从洁净渲染切回带 gizmo 的编辑视图、而时间轴是暂停的，看着仍像
「突然跳回初始画面并卡住」。

**三条时序是硬要求**：

1. **蒙版先于 `setExportMode(false)` 盖上**——顺序反了会露出一帧 gizmo / 站位圈 / 路线闪回，
   下一镜又切回洁净；
2. **撤在下一镜开渲的瞬间**而非任务完成时——中间的 `refreshChapter` 会再露一次静止编辑视图；
3. **Esc 中止当场撤**——留着像卡死。

淡入淡出必须短（`.13s`）：等待期才 ~0.7s，按常规 `.22s` 蒙版几乎没真正现身（实测截帧 opacity 仍是 0）。

`.dz-stage` 因此必须 `position: relative`，否则 absolute 蒙版会挂到页面级祖先上，盖住整个 Studio。

合成全片同样是等待期，共用这套蒙版。守卫 `test_busy_veil_covers_the_mode_switch` /
`test_busy_veil_fades_fast_enough_to_be_seen`。

## 4. 前端接线

### ⑧ 控制台 `refreshChapter` 是白名单式浅拷贝

scanner 新下发的章节级字段（`previz*` 与 `shots` 这类会被后台任务改写的）必须在那里逐一回填。

**漏一个不报错、只是界面纹丝不动**——实测 `previz_reel` 漏过一次：全片合成完成、文件也在盘上，
按钮却始终停在「合成全片」。

守卫 `test_refresh_chapter_backfills_every_shipped_field`。

### ⑨ `director/*.js` 用到的宿主 App 符号必须显式 import

ESM 化后全局作用域没了，漏 import 就是运行期 ReferenceError，而它**只在点到那一步才炸**。

实测：逐帧上传那条手写 fetch（帧体是 image/png 二进制，走不了恒发 JSON 的 `post()`）漏了 `CSRF`，
戏排完保存完全程无异样，按下「渲染」才报「CSRF is not defined」。

守卫 `test_director_imports_every_host_symbol_it_uses` 全量静态扫描。

### ⑨.5 kenburns 章节的入口卡收进折叠条

previz 的四个产物里，末帧、V2V 参考视频与首帧全部落在 gen-video 请求上，而 kenburns
不发；`--use-first-frame` 在这一档还会把灰模盖成分镜图。只剩运镜措辞仍然生效
（`shots[].camera` → `pipeline/kenburns.py` 的 `style_for` 选推/拉/摇），而它有更便宜的
入口（分镜卡「⧉ 改镜指令」/ ChapterPlan）。

故章节页按 `uses_video` 把导演台、深度捕捉台与简笔分镜收成一条 `.pvz-fold`（判据与形态见
[`studio-frontend.md`](studio-frontend.md) §9.0）。**收起不是不渲染**——排完 previz 又
改回 kenburns 的章节盘上是有的，产物必须仍然够得着：折叠条给读数、点开就是原来那三张
卡，分镜卡的 ◈ 预演角标也照旧在（tip 随 motion 改口，不再声称首/末帧会生效）。

导演台路由 `#/stage/<pid>/<cid>` 不受门的影响，URL 直达照常可用。

## 5. 另有两条工程口径

| 口径 | 内容 |
|---|---|
| 运镜措辞的真源是 `storyboard.md` | `camera.py` 里 21 条复用运镜逐字节复刻，`test_camera_presets` 直接解析那份 markdown 比对 |
| previz 时长必须与最终片长 1:1 | 前端 `snapDuration` 与 `SeedanceProvider.billable_seconds` 的 native 口径逐值对拍——差一秒就是运动被拉伸或截断 |
