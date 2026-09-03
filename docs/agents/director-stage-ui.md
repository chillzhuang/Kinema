# 3D 导演台的 UI 纪律

**适用** 改控制台前必读

> 带圈小节编号是对外引用锚点——`stage.js` 与 `test_previz.py` 按 `director-stage-ui ⑩` 形式引用，改号即断链。

## ① 只用站内组件

| 用途 | 组件 |
|---|---|
| 下拉 | `uiSelect` |
| 勾选 | `uiCheck` |
| 档位选择 | `.efx-opt` 药丸组 |
| 检索 | `listSearch` |
| 弹层 | `.dlg` 骨架 |

**绝不用原生 `<select>` / `<input type=checkbox|range|number>`** ——原生控件由操作系统绘制，深色
主题下配色圆角字体全对不上，一眼就露出「这块是外挂的」。

## ② 信息密度按需展开

对标 Blender / Unreal Sequencer / Spline：

- 场景树按类型折叠成组；
- 资产库收进「＋添加」选择器弹层（搜索 ＋ 场景族分组 ＋ 缩略图）；
- 动作库收进选择器弹层（搜索 ＋ 三桶分组 ＋ 逐格实时预览）；
- 36 个运镜收进选择器弹层（搜索 ＋ 三桶分组 ＋ 风险色边）。

全平铺在 260px 侧栏里的结果是三栏各自拥挤，而**中间的 3D 视口这个真正的主角被挤成一条缝**。

## ③ 左右栏可收起

`[` / `]` 或视口边缘的收栏页签，视口能通栏。

## ④ 角色的 gizmo 必须 `showY = false`

人永远贴地（`onGizmoMove` 把 y 压回 0）。留着那根画在角色正中、最好抓的竖直箭头，就是个「看得见却
永远无效」的假控件——实测会让用户直接得出「拖动根本不生效」的结论。

**道具反过来要保留 Y**（箱子放桌上）。

走位吸附 `translationSnap=0.25`：previz 的走位要喂给 Seedance，得是准的而不是差不多。

## ⑤ 首次进入 = 空台，自动编排已整体删除

**编排是导演的创作，引擎绝不代排。**

章节文档无 previz 快照时只铺结构骨架：每个正镜一个镜头块 ＋ 一个机位（有登记的 `camera_preset`
用之、否则 static），零角色 / 零道具 / 零走位 / 零动作。

**排过并保存过的绝不覆盖。**

### ⑤.1 代排唯一入口 =「AI 编排指令」交指挥层

「照分镜图对位 + 逐镜运镜手法」需要视觉理解，引擎无 LLM 做不了。

指令带定位 / schema / 全套合法 key；指挥层逐镜 `Read` `shots[].image` 后重写 previz 块，刷新即恢复。
协议在 kinema Skill 的 `references/production-playbook.md` 节点 ①.7。

### ⑤.2 机位视角 = 监视模式

- 不选不拖对象；
- 左键拖 = 环视（仅屏显偏移，`sceneAt` 每帧重摆 rig，导出 / 监视器恒纯运镜）；
- 双击回正。

导演视角则有**机位可视**：当前镜头块的青色相机实体可点选可拖，＋ 按 preset × 主体锚点采样的整段
运动轨迹，挂 `aids` 组不入导出。

机位构图偏移 `cameras[].frame`（Cinemachine Screen X/Y · 检查器九宫格 · ±0.167 = 三分线）。

### ⑤.3 机位与镜头块 1:1 从属

大纲按分镜列出，无增删换绑入口，保存时过滤游离机位。

机位自由量 = **方位 `yaw` ＋ 径向距离 `dist`**：拖青色机身同步求解两者、相对起拖点算。

**所拖即所播**——机身放到哪，播放时相机那一刻就在哪；preset 运动形状不变、高度不缩放。

### ⑤.4 动作 × 道具落座吸附

`stage.snapToSeats` 在 `sceneAt` 内，导出同源。

判定：骑乘 / 上车 / 坐下，且 1.8m 内有带 `rig.PROPS[].seat` 锚点的坐骑 / 坐具即吸附落座。拖坐骑
连人走，被拖角色不吸附。

**`seat` 记的是座面高度，不是根节点偏移量。** 落差由引擎现算：

```text
root.y = 道具 y + seat[1] − (hips.position.y − pelvisDrop(model))
```

手调偏移量按某一个体型的某一个坐姿定死，换成儿童人偶或换成骑乘姿就会把人抬高或压低几十厘米；
按姿势反推则一次覆盖全部体型与三种落座姿。`pelvisDrop` 与建模共用 `pelvisBlock` 一处取值，
反推的臀底与画出来的体块不会分叉。

**声明了 `seat` 的道具必须列进 `SEAT_FOR`**，否则锚点是死的，而目录 desc 仍在承诺自动落座。
守卫 `test_every_seat_anchor_is_reachable` / `test_seat_snap_derives_height_from_the_pose`。

实测 11 件坐具 × 成人/儿童两种体型，臀底与座面误差 ±0.000m；高坐具（箱体 0.80 / 祭台 0.90 /
马鞍 1.325）脚自然悬空，儿童坐成人椅同样是坐稳＋脚悬空。

动作与道具的数量以 `previz.py` 的两张表为准，不在文档里抄一份快照。

### ⑤.5 大纲点选走 `pickFromList`

| 对象 | 行为 |
|---|---|
| 角色 / 道具 | 选中 ＋ 框满 |
| 机位 | 跳镜 ＋ 把「机位 ↔ 主体」框进视口 |

机身在画面外时，「拖机身」无从谈起。

### ⑤.6 监视器画面 = 内嵌画布

先渲角落 → `drawImage` 拷入 → 主渲染覆写。直接 scissor 会从 DOM 圆角四角露出方角。

## ⑥ 画走位的起点必须是角色当前站位

否则 t=0 时角色瞬移到路线起点——用户按自己的视角在地上点几下、人就跑到十几米外而镜头还看着原处，
表现出来正是「人偶永远在最右上角、还拖不动」。

另配 F 键 / ◎「聚焦主体」与 zoom 钳制，转迷路了一键回来。

## ⑦ 排障有现成句柄

控制台挂了只读引用：

```js
window.__director = {scene, S, timeline, gizmo, dirCam, shotCam, orbit, renderer}
```

3D 出问题时「看不见状态」会让排查退化成猜。

## ⑧ 视口手势 v2

### ⑧.1 手势表

| 操作 | 行为 |
|---|---|
| 左键拖动 | 贴地平移（上帝视角，`mouseButtons.LEFT=PAN` + `screenSpacePanning=false`） |
| 右键拖 / **⌥+拖** | 环绕 |
| 方向键 | 平移镜头 |
| `,` / `.` | 逐帧步进 |
| F | 包围球取景聚焦（全身框满居中） |
| T | 顶视图 |
| 双击 | 选中并聚焦 |
| 点空处 | 取消选中 |

⌥ 那条不可省：**触控板没有「右键拖」这个动作**，没它触控板用户永远转不了向。

进门自动选中主角并框满。

### ⑧.2 点选在 pointerup 且位移 <6px 才算数

pointerdown 即选中会让起手转镜头误选到人。

### ⑧.3 对象可直拖挪位

命中即接管 OrbitControls。

**挪角色必须连走位整体平移**——`sceneAt` 每帧按曲线覆写位置，不挪路线人就被拽回原地；gizmo 路径同理。
位移量恒相对起拖点算，防播放中累加漂移。

**拖时连续、松手落格**：拖动中 0.25m 硬吸附会格间横跳「抖动很厉害」。求值收 rAF——触控板 pointermove
可达 120Hz，逐事件重建走位曲线是抖动另一半。起拖即 `timeline.pause()`。

### ⑧.4 走位工具

路管 ＋ **琥珀数字针**（可拖改线；起点钉死在角色脚下无针）。画线有幽灵落点 ＋ 橡皮线；右键 / 退格
撤销、双击 / 回车完成、Esc 恢复快照（`pathtool.js`）。

**新辅助物必须挂 `aids` 组**（`setExportMode` 一把摘）。

### ⑧.5 两块辅助画面

| 位置 | 内容 |
|---|---|
| 右下监视器 =「另一只眼睛」 | 导演视角显示机位画面（借 `setExportMode` 洁净渲染 = 导出即所见）；机位视角显示导演全局。点按互换 |
| 左下分镜参考面板 | 跟播放头换图——3D 摆位照分镜图对照的锚点 |

**竖幅画幅两块面板必须挂 `.tall` 按视口高度定尺寸**（宽度由 inline `aspect-ratio` 反推）——
沿用横幅的宽度百分比时，9:16 的高是宽的 1.78 倍，面板直接盖满视口（实测被点名）。
PIP 渲染缓冲在 stage.js 另存一份尺寸，与 CSS 逐值锁步。

守卫 `test_portrait_aux_panels_are_height_sized_and_lockstepped`。

### ⑧.6 两条渲染像素纪律

**`exportFrames` 必须锁 `setPixelRatio(1)`** ——否则 Retina 屏导出帧是目标分辨率的 2×，
「与 Seedance 同分辨率 / 跨机器逐字节一致」双双被破。

**`setViewport`/`setScissor` 必须喂 CSS 像素**（`renderer.getSize()`，绝不是 `domElement.width`）
——它们内部会乘 pixelRatio，喂 drawingBuffer 像素等于再乘一次。

后果：Retina 上视口翻倍、画布只露左下四分之一，「主体永远在右上角、点选 / 拖拽全对不上看得见的
像素」全是它。dpr=1 的环境（含 headless Playwright 默认）完全复现不出来——**验 3D 视口必须带
`device_scale_factor=2` 再跑一遍**。

### ⑧.7 类名前缀

视口浮层玻璃件用 `dz-vseg` / `dz-refpanel`。

**别用 `dz-seg`** ——那是时间轴动作段的既有类名，撞了两边样式互相污染。

### ⑧.7.5 道具按场景族分组

`previz.DIRECTOR_PROP_GROUPS` 是分组真源（通用体块 / 室内陈设 / 城镇建筑 / 古风建筑 /
自然地貌 / 现代器物），`DIRECTOR_PROPS[].group` 必须命中其中一个 key。

**分组只影响检索路径，不影响体块本身**——一堵墙在城镇戏与室内戏里是同一块几何。
落不进任何组的道具会掉进选择器的「其他」段：不静默丢弃，但用户实际上找不到它，
故守卫 `test_catalog_shape_and_json_safe` 强制每件道具都归组。

### ⑧.8 加动作 / 道具三处锁步

1. `previz.py` 的 `DIRECTOR_ACTIONS` / `DIRECTOR_PROPS`；
2. `rig.js` 的 `ACTIONS` / `PROPS`（注册表一行一条，供守卫正则解析）；
3. 姿势表 / 体块 builder。

位移动作还要进 `test_previz` 的 moving 集合与 actors `FLAT_LOOK`。

改完必须 `studio --restart`——目录在 scanner 进程内存里。

动作 / 道具清单以 `previz.py`（`DIRECTOR_ACTIONS` / `DIRECTOR_PROPS`）为准。

## ⑨ 机位轨道两层模型

| 层 | 内容 |
|---|---|
| 缺省 | preset 程序轨道（× yaw / dist） |
| 自定义 | 选中机位后轨迹线上有 5 枚青色路点针，拖任意一枚即把轨迹「烘焙」成同形状的自定义轨道 `cameras[].path`（世界坐标） |

自定义态下位置改走 `camPathCurve` 弧长参数化 `getPointAt(ease(local))`，**匀速且镜头块结束帧必达
轨道终点**——「时间到了镜头才走一半」从机制上不可能。盯主体 / 焦距曲线 / 手持噪声照旧
（Cinemachine Body/Aim 拆分）。

自定义态下：拖机身 = 整条轨道平移；⇧+拖针 = 调高度；检查器 ↺ 或**换 preset 必清 `path`**
（不清 = 新运镜在位置上完全不生效且不报错）。

### ⑨.1 针 / 机身拾取必须按屏幕空间像素距离

`pickCamPin` 恒定 14px。

射线打固定世界尺寸的拾取球时，轨道沿视线纵深展开，近针的球投影大好几倍且深度排序恒赢——实测
「按在机身图元上却拖动了 30px 外的远针」。

建曲线只有 `camPathCurve` 一个入口（rig 求值与轨迹可视共用；分叉 = 画的线不是飞的线）。

守卫 `test_custom_camera_track_completes_within_the_cut` / `test_camera_pin_drag_bakes_then_edits`。

### ⑨.2 垂直自由度必须有看得见的入口

路点针带 ↕ 升降手柄（拾取面 `camPinLift`，光标 `ns-resize`）——只藏在 ⇧ 修饰键里，用户会得出
「镜头只能水平改位置」（实测被点名）。

垂直换算走 `liftDelta`：**世界 Y 轴的屏幕投影**，px → 米精确跟手，正俯视投影退化有除零护栏。

⇧+拖机身 = 整条轨道升降（preset 先 `ensureCamPath` 烘焙；静止机位明确拒绝，而不是烘一条零长度的
坏轨道）。

守卫 `test_camera_track_has_a_vertical_degree_of_freedom`。

### ⑨.3 视觉件

轨迹暗 → 亮时间渐变 ＋ 选中辉光 / 方向箭 / 地面投影虚线；机身挂视锥线框（每帧按 `tan(fov/2)` 张合）；
机位 DOM 名牌 `.dz-camtag` 与监视器三分线 `.dz-thirds`（DOM 浮签不进画布，导出 / 监视器天然干净）。

## ⑩ 播放 = 看戏，操纵件全收

播放中：gizmo detach（暂停时按对象**当前**位置重新 attach）、选中 / 悬停圈隐藏、机位路点针收起。

实现：`syncAids` / `syncCamViz` 各查 `timeline.playing`，`select()` 播放中不 attach。

### ⑩.1 `setExportMode` 的恢复侧必须写真值

```js
helper.visible = on ? false : !!gizmo.object
```

PiP 洁净渲染每帧成对调它。无条件 `= !on` 会把 detach 后本应消失的 root 逐帧复活成**幽灵 gizmo**
（冻在最后挂载位置）——实测「人沿走位走远了，手柄留在原地」的真凶就是它，不是 gizmo 跟不上人。

守卫 `test_playback_hides_manipulation_aids`。

### ⑩.2 自定义轨道下拖机身缺省只挪起点

`mode:"start"` 只碰 `path[0]`。

若缺省整条平移，「想摆个开拍位」一拖就把后续路点全带走。**大动作必须显式修饰键**：整条平移 =
⌘/Ctrl+拖机身，整条升降 = ⇧+拖机身。

机位名牌只写「镜号·运镜名」且**选中该机位即隐藏**——塞轨道态会撑宽到盖住机身。

守卫 `test_body_drag_defaults_to_start_point_not_whole_track`。

### ⑩.3 机位拾取面 = 可见几何 + 视锥漏斗弱命中，绝不用隐形大球

0.34m 球在近景比图元大一圈 =「空白处点拖也把机位拖走」。

视锥线框必须 `line.raycast=()=>{}` ——Raycaster 对 Line 的命中阈值默认 **1 米**。

漏斗 `pickFrustum` 判定先于父级攀爬、且只在没点到实体时才认——它常正对主体，强命中会隔着漏斗抢
「点主体」。

拖拽读数走 `setDragHint`（`.dz-draghint`，视角胶囊正下方居中）。

守卫 `test_camera_pick_surface_is_the_glyph_not_a_bubble`。

### ⑩.4 轨迹采样绝不许踩到 `t_out`

`cutAt(t_out)` 属于**下一个镜头块**（区间左闭右开），故 `sampleCamTraj` 末样本必须
`Math.min(…, cut.t_out - 1e-3)`。

不夹的话第 49 个样本（= 5 号针）是下一镜机位的起始位姿：预设轨迹「走到 4 号针就切镜」，烘焙的
自定义轨道结尾真的飞向别的机位——希区柯克变焦首先撞上，实测点名。

守卫 `test_traj_sampling_never_crosses_the_cut_boundary`。

## ⑪ 表演：段间过渡、姿势曲线与预览选择器

### ⑪.1 过渡权重必须是 t 的纯函数

`actors.js` 在段边界的窗口内让两条 action 同时参与求值，权重 `smoothstep(局部进度)`，
两条各自按绝对时间 scrub 后由一次 `mixer.update(0)` 一起算。

**绝不用 `crossFade`**——它的权重按 mixer 时间累加，掉一帧两侧权重就不同，预览与逐帧导出会在
同一时间点得到不同姿势。previz 要作参考视频喂给模型，「所见即所渲」是它的立身之本。

窗口 `BLEND_SEC` 上界同时受前后两段约束（越过前一段起点会取到该段尚未开始时的相位；越过本段一半
则指派的动作本身看不清）。未参与本帧的 action 必须显式 `enabled=false`——mixer 累加所有 enabled
的 action，留一条权重非零的旧动作就是两个姿势叠在一起。

相邻两段指派同一动作时按 `_phaseOrigin` 向前并入，不重起相位。

实测边界处单帧最大关节变化：硬切 13.5° / 25.4° → 过渡后 1.3° / 2.4°。

守卫 `test_action_blend_stays_a_pure_function_of_time`。

### ⑪.1.5 姿势表的三条物理口径

**① 躯干链与四肢的 `swing` 分开取号。** 约定统一为「正 = 这根骨骼的**远端**向前(+Z)摆」；
送进 `rotation.x` 时，四肢与脚（远端 −Y / +Z）取负号，躯干链 `AXIAL = {hips, spine,
chest, neck, head}`（远端 +Y）取正号。`R_x(θ)` 对 (0,−1,0) 与 (0,+1,0) 的作用方向相反，
用同一个符号会让「上身前倾 20°」渲成后仰 20°——而且这在 3D 视口里不容易一眼看出。

**② 父级俯仰后，子骨骼填的仍是相对父级的角度。** `hips` 一转，腿与臂的世界朝向已经
跟着转过一次；此时按「我要胳膊指向地面」的世界直觉再填一次，就是把同一次旋转叠两遍。
趴下会因此把腿转回垂直、整个人沉到地面以下近 1m。求法：`总角 = hips 角 − 该肢角`。

**③ 关节只朝解剖学允许的方向弯。** 肘 `forearm*` swing ≥ 0，膝 `shin*` swing ≤ 0。
守卫 `test_joint_angles_respect_anatomy` / `test_torso_and_limb_swings_use_separate_signs`。

**验收口径**：全部地面动作在整个循环内的最低点落在 **−0.02 ~ 0.12m**（站立姿的脚底
本就在 0.046m，不是 0）；`fly` / `ride` 按设计离地。这条只能实跑测量，Python 侧复算
一份正向运动学就是把骨骼比例存两份，故不做自动守卫。

### ⑪.2 姿势曲线在编译期加密

姿势表记的是**极值姿势**（走四相位、跳五拍），而 three 的 `QuaternionKeyframeTrack` 只有线性插值
——该轨类型没有平滑插值实现。两个极值之间走直线，关节角速度在每个关键帧处突变。

`buildClip` 因此先在**欧拉分量**这一层按关键帧时间做非均匀 Catmull-Rom 加密（目标 24Hz，每区间
2~24 个采样），再编译成四元数。循环动作的端点切线跨接缝求，接缝不是折角。

**采样一律夹在相邻两个关键帧的取值区间内**：样条过冲会把作者写定的极值再推出去一截，等于替作者
改表演；髋部高度通道上过冲还会让脚穿过地面。

姿势表书写方式不变，仍然只写极值。

守卫 `test_pose_curves_are_densified_without_moving_authored_extremes`。

### ⑪.3 动作 / 体型 / 道具三处选择器都带缩略图

| 位置 | 形态 |
|---|---|
| 检查器「表演」段 | 当前动作卡（静止缩略图 ＋ 名称 ＋ ⇅），点开选择器 |
| 检查器「体型」 | 四格人偶，**共用一套取景**——身高差因此在格与格之间直接可读 |
| ＋添加 | 弹层：人偶 ＋ **按场景族分组**的道具体块，逐件缩略图（尺度跨度大，逐件取景）＋ 尺寸读数 ＋ 搜索 |
| 时间轴动作段 | 点段即换动作 |
| 动作选择器弹层 | 十七格**各自实时演一遍**，三桶分组 ＋ 搜索 |

分桶判据取目录字段本身（`speed>0` / `loop`），控制台不另存一份动作清单——自行列举就会在目录增删
时分叉成「选了没反应」。守卫 `test_action_picker_is_driven_by_the_shipped_catalog`。

### ⑪.4 缩略图渲染器独立且随卸载释放

`preview.js` 自建离屏 `WebGLRenderer`，**绝不借用舞台渲染器**：后者的 `setSize` / `setPixelRatio`
同时受视口布局与逐帧导出支配（导出恒锁 `pixelRatio=1` 且按 Seedance 目标分辨率渲染），插入第三方
改动会同时破坏这两条（见 ⑧.6）。

布光取 `rig.LIGHT_RIG`（与视口同一组值），姿势走 `Actor.update`（与视口、导出同一条求值路径）——
格子里看到的就是摆进舞台后的样子，缩略图不是另一份需要维护的美术资产。

三条资源纪律：

1. **一台渲染器逐格渲染后 `drawImage` 拷进各格 2D 画布**——不给每格建 WebGL 上下文（同页上下文数
   有硬上限），也不缓存动画位图（十七条循环几十兆常驻）；
2. **播放轮询按 `canvas.isConnected` 自净**，弹层关闭不留孤儿 rAF；
3. **`dispose()` 必须调 `disposePreview()`**——它是控制台之外的第二个上下文，同样跨路由留存。

静止缩略图留位图缓存：检查器在拖路点这类操作里按帧整块重建，逐次重渲等于把一次 WebGL 绘制绑在
每一帧界面刷新上。

**模态弹层开着时视口不渲染**（`frame()` 的第二道闸，与「后台标签页一帧不画」同源）：遮罩已经盖住
整个工作台，那一帧渲了也没人看得见，而选择器里十七格正在实时演动作——开销该留给看得见的那一侧。
实测（headless 软件渲染）选择器开着时 rAF 5.6 → 20.5 帧/秒。

守卫 `test_thumbnail_renderer_is_isolated_and_released` / `test_idle_viewport_stops_burning_frames`。
