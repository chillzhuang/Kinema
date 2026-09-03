/*
  This file is part of Kinema.
  Copyright (C) 2018-2099 BladeX (https://bladex.cn)

  SPDX-License-Identifier: AGPL-3.0-or-later

  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU Affero General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU Affero General Public License for more details.

  You should have received a copy of the GNU Affero General Public License
  along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

/* ============================================================================
   3D 导演控制台 · 入口（`#/stage/<pid>/<cid>` 路由懒加载本模块）

   它做的事只有一件：**给现有 `shots[]` 契约加一个「排 3D 戏」的可视化入口**。
   导演在这里摆角色、指派动作、画走位、设机位、选运镜，一键渲出 previz 参考片；
   产物经 `/api/previz/*` 落回 `shots[].image / last_frame_ref / previz / camera`，
   随后 gen-video 照常走既有的成本双控、审阅闸、版本栈与血缘——**零并行状态机**。

   两个「同一个函数」是本模块的骨架，也是它能被信任的原因：
     · `sceneAt(t)` —— 预览与导出共用的唯一求值函数（所见即所渲）；
     · `/api/previz/render` → `previz build` → `register_previz` —— 网页与 CLI
       共用的唯一登记路径（网页绝不另写一份，否则版本栈/待审纪律迟早在网页失效）。
   ========================================================================== */

import { chip, openDirectiveDialog, openShell, uiCheck } from "../app/components.js";
import { api, h, pollJob, post, toast } from "../app/core.js";
import { openCinema } from "../app/widgets.js";

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";

import { Actor } from "./actors.js";
import { CameraRig, camPathCurve, findPreset } from "./cameras.js";
import { buildCamBody, buildCamFrustum, buildCamPins, buildCamTraj, buildGhost, buildPathViz,
         buildRubber, disposeViz, ringMesh } from "./pathtool.js";
import { disposePreview } from "./preview.js";
import { LIGHT_RIG, PROPS, buildProp, pelvisDrop } from "./rig.js";
import { Timeline, fmtT, snapDuration } from "./timeline.js";
import { buildPreviz, exportFrames } from "./exporter.js";
import { openKeysPop, renderInspector, renderOutliner, renderTimeline, buildShell,
  showBusy, syncTimelineHead } from "./ui.js";

const token = (name, fallback) => {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
};
const hexOf = (name, fallback) => new THREE.Color(token(name, fallback)).getHex();
// 灰模自发光：常态几乎为零，选中时叠一层极暗琥珀（看得出被选中，又不改变"它是灰模"）
const BASE_TINT = 0x0a0c10;
const SELECT_TINT = 0x33240c;

/**
 * 挂载控制台。返回 `dispose()`——路由切走时必须调，否则 WebGL context、rAF 与
 * 全局键盘监听会留下来，反复切换导致浏览器内存持续增长（SPA 最常见的泄漏）。
 */
export async function mount(view, pid, cid) {
  const [ov, ch] = await Promise.all([
    api("/api/overview"),
    api(`/api/chapter?project=${encodeURIComponent(pid)}&id=${encodeURIComponent(cid)}`),
  ]);
  const catalog = ov.camera_catalog || [];
  const dir = ov.director_catalog || {};
  const canvas = (ov.canvas || {})[ch.aspect] || [1920, 1080];
  // 竖幅画幅下，监视器与分镜参考两块辅助画面必须按视口**高度**定尺寸：
  // 沿用横幅的「宽度百分比」时，9:16 的高是宽的 1.78 倍，两块面板会直接盖满视口。
  // 该开关同时控制 CSS（.tall 类）与 PIP 渲染缓冲尺寸，两处必须同源。
  const portrait = canvas[1] > canvas[0];

  // 目录缺失在此抛错并说明成因；若放到 `preset.ease` 才抛
  // "Cannot read properties of undefined"，看不出与目录有关。
  // 最常见成因是 Studio 进程比代码旧：静态资源（本文件）每次请求都从磁盘读，
  // 前端总是新的；scanner.py 在进程启动时加载进内存，服务不重启就不会下发
  // 新目录，于是新前端配旧后端、目录为空。
  if (!catalog.length || !(dir.models || []).length) {
    const miss = [!catalog.length && "camera_catalog（运镜库）",
                  !(dir.models || []).length && "director_catalog（舞台资产库）"]
      .filter(Boolean).join(" 与 ");
    throw new Error(
      `/api/overview 没有下发 ${miss}。\n`
      + "多半是 Studio 进程比代码旧——静态资源每次从磁盘读（前端已是新的），"
      + "但 scanner.py 在进程启动时就加载进内存了，不重启永远不会下发新目录。\n"
      + "在终端跑：python3 -m kinema studio --restart");
  }

  const { root, main, outliner, viewport, inspector, timeline: tlEl, busy } = buildShell();
  view.innerHTML = "";
  view.append(buildHeader(pid, cid, ch), root);

  /* ------------------------------------------------------------ 场景与渲染器 */
  const scene = new THREE.Scene();
  // 天幕：竖向渐变（顶部微亮冷灰蓝 → 底部页面底色）代替整片纯黑——地平线有了
  // 「远处还有空间」的纵深暗示，画面就不再是贴在黑纸上的模型
  scene.background = makeBackdrop(hexOf("--bg", "#0b0c0f"));
  // 雾区间 28~76m：地面半边 80m，雾必须在 80m **之前**吃完，否则地平线是一条硬边；
  // 起点也不能太近，不然网格提前糊掉，导演就失去「这个人离那堵墙多远」的距离感——
  // 那恰恰是走位编排最需要的判断。雾色取天幕中段色，衔接处无缝
  scene.fog = new THREE.Fog(0x1b212c, 28, 76);   // 与天幕地平线辉光同调，衔接无缝

  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    preserveDrawingBuffer: true,   // toBlob 抓帧的前提，缺它导出会拿到空图
  });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  // 电影级色调映射：ACES 把高光滚降压进胶片曲线，灰模的体块立刻有「材质感」；
  // 线性直出（默认 NoToneMapping）正是「玩具感」的另一半来源
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;   // r185 起 PCFSoft 已弃用并回落到它
  viewport.append(renderer.domElement);
  // 视口浮层（纯 DOM，pointer-events:none）：左上角状态条 + 右下角机位监视器边框。
  // 监视器边框刻意不用 WebGL 画——scissor 渲染画不出圆角/描边/标签，而"这是一台监视器"
  // 的观感全靠那圈边和角标；DOM 叠上去还能跟着设计令牌换肤。
  // 状态条文案随模式切换（默认 / 落位 / 画线），单一出口 updateHud()
  const hudTip = h("span", { class: "dz-hudtip" }, "");
  const pipLabel = h("span", { class: "dz-piplabel" }, "机位画面");
  // 监视器画面走**内嵌画布**（每帧从主画布拷贝，与左下角参考面板同构）——
  // 直接 scissor 渲在主画布上时，DOM 圆角框只是描边叠层，方角图会从四角露出来。
  // **监视器永远显示「另一只眼睛」**：导演视角时放机位画面（成片构图·洁净渲染），
  // 机位视角时放导演全局——点按即互换（虚拟制片工具「点监视器看镜头」的通例）。
  const pipCv = h("canvas", { class: "dz-pipcv" });
  // 三分构图线：专业监视器的标配，与检查器的构图九宫格同一套语义——
  // 只在「机位画面」上有意义（导演全局时隐藏）
  const pipThirds = h("i", { class: "dz-thirds" });
  const pip = h("div", { class: portrait ? "dz-pip tall" : "dz-pip",
    style: `aspect-ratio:${canvas[0]}/${canvas[1]}`,
    dataset: { tip: "监视器：另一只眼睛\n导演视角时=这一镜实际拍到的画面（洁净·gizmo 与路线不入镜）；\n机位视角时=导演全局。点按互换视角（G）。" },
    onclick: () => ctx.toggleView() },
    pipCv, pipThirds, pipLabel, h("span", { class: "dz-piprec" }));
  // 分镜参考：当前镜的分镜图贴在左下——3D 摆位「照着分镜摆」的对照物。
  // 没有它，排出来的机位和 AI 画好的构图互不对应（实测被点名「牛头不对马嘴」）。
  const refImg = h("img", { alt: "", style: `aspect-ratio:${canvas[0]}/${canvas[1]}` });
  const refLbl = h("span", { class: "dz-reflbl" }, "分镜参考");
  const refPanel = h("div", { class: portrait ? "dz-refpanel tall" : "dz-refpanel", hidden: true,
    dataset: { tip: "当前镜的分镜图\n照着它摆机位与站位；点按放大对照。" },
    onclick: () => refPanel.classList.toggle("big") }, refLbl, refImg);
  // 视角切换胶囊（顶部居中·玻璃件）：把 G 键的双态摆到明面上
  const segDir = h("button", { class: "dz-vseg on", type: "button",
    onclick: () => { if (S.viewMode !== "director") ctx.toggleView(); } }, "◈ 导演视角");
  const segCam = h("button", { class: "dz-vseg", type: "button",
    onclick: () => { if (S.viewMode !== "camera") ctx.toggleView(); } }, "◉ 机位视角");
  const viewSeg = h("div", { class: "dz-viewseg" }, segDir, segCam);
  // 监视模式提示：挂在视角胶囊正下方居中（模式的说明跟着模式的开关走）
  const modeHint = h("div", { class: "dz-modehint", hidden: true },
    "监视模式：拖动 环视（不改运镜）· 双击 回正 · G / 点监视器 返回导演视角");
  // 拖拽读数：与监视模式提示同槽位（视角胶囊正下方居中）——左上 HUD 离视口的
  // 视觉中轴太远，拖拽时读数就该悬在正在看的地方（实测点名）。两者互斥：
  // 拖拽只在导演视角发生，监视提示只在机位视角出现。
  const dragHint = h("div", { class: "dz-modehint dz-draghint", hidden: true });
  function setDragHint(txt) {
    if (!txt) { if (!dragHint.hidden) dragHint.hidden = true; return; }
    if (dragHint.textContent !== txt) dragHint.textContent = txt;
    if (dragHint.hidden) dragHint.hidden = false;
  }
  function syncViewSeg() {
    segDir.classList.toggle("on", S.viewMode === "director");
    segCam.classList.toggle("on", S.viewMode === "camera");
    modeHint.hidden = S.viewMode !== "camera";
  }
  // 栏收起按钮：视口是这个工作台的主角，需要时应当能让它通栏
  const paneBtn = (side, label, tip) => h("button", {
    class: `dz-pane dz-pane-${side}`, type: "button", dataset: { tip },
    onclick: () => { S.panels[side] = !S.panels[side]; syncPanes(); },
  }, label);
  const paneL = paneBtn("left", "‹", "收起 / 展开大纲栏（[）");
  const paneR = paneBtn("right", "›", "收起 / 展开检查器栏（]）");
  const fullBtn = h("button", { class: "dz-vpbtn", type: "button",
    dataset: { tip: "全屏工作台（F11）" }, onclick: () => toggleFull() }, "⤢");
  const focusBtn = h("button", { class: "dz-vpbtn", type: "button",
    dataset: { tip: "聚焦主体（F）\n把选中项 / 第一个角色完整框进视口——转迷路了按它。" },
    onclick: () => focusSubject() }, "◎");
  const topBtn = h("button", { class: "dz-vpbtn", type: "button",
    dataset: { tip: "顶视图（T）\n正上方俯瞰全场——画走位、排站位最准的视角；再按一次回到原视角。" },
    onclick: () => toggleTop() }, "⊚");
  // 视口缩放按钮：滚轮之外的第二条路（触控板双指不顺手时点它）
  const zoomBy = (f) => {
    const dir2 = dirCam.position.clone().sub(orbit.target);
    dir2.setLength(THREE.MathUtils.clamp(
      dir2.length() * f, orbit.minDistance, orbit.maxDistance));
    dirCam.position.copy(orbit.target).add(dir2);
    orbit.update();
  };
  const zinBtn = h("button", { class: "dz-vpbtn", type: "button",
    dataset: { tip: "放大（滚轮 / 双指同效）" }, onclick: () => zoomBy(0.72) }, "＋");
  const zoutBtn = h("button", { class: "dz-vpbtn", type: "button",
    dataset: { tip: "缩小" }, onclick: () => zoomBy(1.4) }, "−");
  const refBtn = h("button", { class: "dz-vpbtn on", type: "button",
    dataset: { tip: "分镜参考图\n左下角贴当前镜的分镜图，照着摆位与运镜。" },
    onclick: () => { S.refOpen = !S.refOpen; refBtn.classList.toggle("on", S.refOpen); } }, "▣");
  const helpBtn = h("button", { class: "dz-vpbtn", type: "button",
    dataset: { tip: "操作速查\n鼠标 / 触控板 / 键盘的全部操作。" },
    onclick: (e) => openKeysPop(e.currentTarget) }, "⌨");
  viewport.append(h("div", { class: "dz-hud" },
    h("div", { class: "dz-hudbar" }, h("span", { class: "dz-hudk" }, "STAGE"), hudTip),
    viewSeg, modeHint, dragHint,
    h("div", { class: "dz-vptools" }, zinBtn, zoutBtn, focusBtn, topBtn, refBtn, helpBtn, fullBtn),
    paneL, paneR, refPanel, pip));

  const dirCam = new THREE.PerspectiveCamera(45, 16 / 9, 0.1, 400);
  dirCam.position.set(4.6, 2.9, 6.4);   // 一进来就是「看得清一个人的四分之三侧」
  const shotCam = new THREE.PerspectiveCamera(40, canvas[0] / canvas[1], 0.05, 400);

  const orbit = new OrbitControls(dirCam, renderer.domElement);
  orbit.target.set(0, 1.1, 0);
  orbit.enableDamping = true;
  orbit.dampingFactor = 0.08;
  orbit.maxPolarAngle = Math.PI * 0.495;   // 不许翻到地面以下（previz 没有地下场景）
  // 缩放钳制：舞台工作半径就十几米，不夹住的话滚轮一甩就缩进人偶内部或退到看不见
  // 任何东西——而那时视口里只剩一片网格，与一次渲染失败无从区分
  orbit.minDistance = 1.6;
  orbit.maxDistance = 42;
  // 「上帝视角」操作方案：**左键拖动 = 平移画布**（贴地滑动，像拖一张地图），
  // 右键拖动 = 环绕，滚轮 = 缩放（沿视线推拉=整体放大缩小，维持原实现）。
  // 原地点击才是点选——与平移用 6px 位移阈值区分（见下方「视口手势」）。
  // screenSpacePanning=false 让平移永远平行于地面：镜头俯得再低，拖动也不会
  // 把舞台拖上天——上帝视角的「地图感」全靠这一行。
  orbit.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY,
                         RIGHT: THREE.MOUSE.ROTATE };
  orbit.screenSpacePanning = false;
  orbit.panSpeed = 1.15;
  orbit.zoomSpeed = 2.4;   // 默认 1.0 在十几米的工作半径里明显偏慢，滚半天不见动

  // 灯光：影棚三点布光——主光投影（立体感）+ 冷调补光（暗部不死黑而有层次）
  // + 轮廓光（把灰模从深色地面上「剥离」出来，是画面可读性的分水岭）
  const key = new THREE.DirectionalLight(LIGHT_RIG.key.color, LIGHT_RIG.key.intensity);
  key.position.set(...LIGHT_RIG.key.pos);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  // 阴影相机改完 left/right/top/bottom **必须 updateProjectionMatrix()**——只赋值
  // 不重算矩阵，阴影仍按默认 ±5 的旧视锥算，投影落在错误的范围里。
  // `normalBias` 也不能省：大平面在斜射光下会整片自阴影（shadow acne），表现为
  // 「阴影相机覆盖范围内的地面一片死黑、范围外反而是亮的」——正是最容易被误判成
  // 「网格没画出来」的那种坏图。
  const sc = key.shadow.camera;
  Object.assign(sc, { left: -18, right: 18, top: 18, bottom: -18, near: 1, far: 48 });
  sc.updateProjectionMatrix();
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.04;
  const fill = new THREE.DirectionalLight(LIGHT_RIG.fill.color, LIGHT_RIG.fill.intensity);
  fill.position.set(...LIGHT_RIG.fill.pos);
  const rim = new THREE.DirectionalLight(LIGHT_RIG.rim.color, LIGHT_RIG.rim.intensity);
  rim.position.set(...LIGHT_RIG.rim.pos);
  scene.add(key, fill, rim, new THREE.HemisphereLight(
    LIGHT_RIG.hemi.sky, LIGHT_RIG.hemi.ground, LIGHT_RIG.hemi.intensity));

  // 地面网格**画进地面纹理**，而不是叠一层 `GridHelper`。
  // 原因是实测出来的：线条网格与地面近乎共面，在俯视角下近处格线会被地面整片吃掉，
  // 只在地平线附近留下一条带——看起来就像"网格没渲染"。抬高几毫米、polygonOffset
  // 都压不住（线本身只有 1px，斜射时覆盖率不足）。做成纹理后网格**就是**地面：
  // 没有共面冲突，还白拿各向异性过滤——远处自然收敛成平滑灰面而不是摩尔纹。
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(160, 160),
    new THREE.MeshStandardMaterial({ color: 0xffffff, map: makeGridTexture(renderer),
                                     roughness: 0.96, metalness: 0 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  ground.name = "ground";
  scene.add(ground);
  // 舞台光池：原点一圈极淡的暖光（叠加混合），把表演区从大地里「点亮」出来——
  // 注意力天然落在中央，远处地面自然沉下去。确定性纹理，导出同样带着（它是舞台
  // 观感的一部分，不是编辑器辅助物）
  const pool = new THREE.Mesh(
    new THREE.CircleGeometry(21, 48),
    new THREE.MeshBasicMaterial({ map: makePoolTexture(), transparent: true,
      opacity: 0.16, blending: THREE.AdditiveBlending, depthWrite: false }));
  pool.rotation.x = -Math.PI / 2;
  pool.position.y = 0.004;
  scene.add(pool);
  // 原点站位圈：给「主体锚点」一个看得见的位置——运镜 preset 的关键帧全是相对它的
  const mark = new THREE.Mesh(
    new THREE.RingGeometry(0.44, 0.5, 48),
    new THREE.MeshBasicMaterial({ color: hexOf("--amber", "#f0a63c"),
                                  transparent: true, opacity: 0.5 }));
  mark.rotation.x = -Math.PI / 2;
  mark.position.y = 0.006;
  scene.add(mark);
  // 朝向基准：+Z 是角色正面（与 pipeline/camera.py 的坐标约定一致），画出来免得记错
  const axis = new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 0.012, 0),
    1.55, hexOf("--cyan", "#4cc3d9"), 0.24, 0.15);
  axis.line.material.transparent = axis.cone.material.transparent = true;
  axis.line.material.opacity = axis.cone.material.opacity = 0.72;
  scene.add(axis);

  const gizmo = new TransformControls(dirCam, renderer.domElement);
  gizmo.setSize(0.78);
  // 走位吸附到 25cm、旋转吸附到 15°：舞台是 1m 网格，摆位吸附能让"他站在门左边
  // 一米"这种关系是**准的**而不是看着差不多——previz 的走位是要喂给 Seedance 的
  gizmo.translationSnap = 0.25;
  gizmo.rotationSnap = Math.PI / 12;
  gizmo.addEventListener("dragging-changed", (e) => {
    orbit.enabled = !e.value;
    // 拖角色前记下「起拖时的位置 + 整条走位」——路线要跟着人整体平移，
    // 位移量必须相对起拖点算，逐事件累加会在播放中把路线越推越远
    const it = S.selected;
    if (it?.kind === "actor") {
      S.gizmoBase = e.value
        ? { pos: it.object.position.clone(),
            path: it.pathPoints ? it.pathPoints.map((p) => [...p]) : null }
        : null;
      if (!e.value && it.pathPoints) refreshPathViz(it);
    }
  });
  gizmo.addEventListener("objectChange", () => { onGizmoMove(); });
  scene.add(gizmo.getHelper ? gizmo.getHelper() : gizmo);

  // 编辑器辅助物（选中圈 / 悬停圈 / 幽灵落点 / 橡皮线）：统一挂一个组，
  // 导出洁净模式一把关——少摘一件都会被烧进喂给 Seedance 的参考片
  const aids = new THREE.Group();
  aids.name = "editor-aids";
  const selRing = ringMesh(0.82, hexOf("--amber", "#f0a63c"), 0.8);
  const hoverRing = ringMesh(0.88, 0xd7dee9, 0.22);
  const ghost = buildGhost(hexOf("--amber", "#f0a63c"));
  const rubber = buildRubber(hexOf("--amber", "#f0a63c"));
  // 机位可视（导演视角）：当前镜头块的相机实体 + 本镜运动轨迹——
  // 「机位在哪、怎么动」在三维里直接看得见，这是导演视角该有的全部信息
  const camViz = new THREE.Group();
  camViz.name = "camviz";
  const camBody = buildCamBody(hexOf("--cyan", "#4cc3d9"));
  // 视锥挂机身（继承位姿），每帧按 shotCam 的 fov/aspect 缩放——焦距一变就张合
  const camFrustum = buildCamFrustum(hexOf("--cyan", "#4cc3d9"));
  camBody.add(camFrustum);
  camViz.add(camBody);
  aids.add(selRing, hoverRing, ghost, rubber, camViz);
  scene.add(aids);
  // 机位名牌（DOM 浮签，跟随机身）：Cinemachine 相机头顶的那块名牌——
  // 「现在这台机器是哪一镜、用什么运镜」不用点开检查器就读得到。
  // pointer-events:none 不抢视口手势；DOM 不进画布，导出/监视器天然干净。
  const camTag = h("span", { class: "dz-camtag", hidden: true });
  viewport.append(camTag);

  /* ------------------------------------------------------------------ 状态 */
  const S = {
    actors: [], props: [], cameras: [], rigs: new Map(),
    selected: null, placing: null, drawingPathFor: null, pathBuf: [],
    hover: null, dragPin: null, dragObj: null, dragCam: null, dragCamPin: null,
    hoverCamPin: null, gizmoBase: null,
    pathSnapshot: null, topSaved: null, refOpen: true,
    camLook: { yaw: 0, pitch: 0 }, dragLook: null,   // 机位视角的环视偏移（仅屏显）
    viewMode: "director", fps: 24, v2v: !!ch.previz_v2v,
    folds: { cam: true, actor: true, prop: true },   // 大纲分组开合
    panels: { left: true, right: true },             // 左右栏是否展开
    rendering: false, exporting: false, renderPct: 0, renderJob: null, abort: false,
    lastInput: 0,          // 空闲降频用（见渲染循环）
    dirty: false, reeling: false,
    pathLines: new Map(),
  };
  // **必须声明在 restore() 之前**：restore 会调 addCamera/addProp，它们读 seq。
  // `let` 有 TDZ，声明写在下面会在恢复既有编排时直接抛
  // "Cannot access 'seq' before initialization"（函数声明会提升，`let` 不会）。
  let seq = { cam: 0, prop: 0 };

  const activeShots = (ch.shots || []).filter((s) => s.kind !== "transition" && !s.omitted);
  // 播放头每帧前进只同步读数与高亮；重建整条时间轴会让工具条按钮在 mousedown 与
  // mouseup 之间被换掉，播放期间无法点击（见 ui.syncTimelineHead）
  const timeline = new Timeline({ cuts: [], fps: S.fps, onTick: () => { syncPlayhead(); } });

  restore(ch.previz);

  /** 从章节文档恢复编排（或首次进入时按 shots[] 铺一遍脊柱）。
   *  **全程 quiet=true**：这一步跑在 `ctx` 与绘制函数定义之前，任何一次
   *  `paintAll()` 都会撞上 `const ctx` 的 TDZ（"Cannot access 'ctx' before
   *  initialization"）。恢复只建数据，画面统一由末尾那一次 `paintAll()` 出。 */
  function restore(doc) {
    if (doc && (doc.cuts || []).length) {
      S.fps = doc.fps || 24;
      (doc.cameras || []).forEach((c) => addCamera(c, true));
      (doc.actors || []).forEach((a) => addActor(a.model, a.pos, a, true));
      (doc.props || []).forEach((p) => addProp(p.kind, p.pos, p, true));
      timeline.cuts = doc.cuts.map((c) => ({ ...c, dur: (c.t_out - c.t_in) || 5 }));
      timeline.fps = S.fps;
      timeline.normalize();
      // 章节新增了镜（编排之后又加了分镜）→ 补齐镜头块，绝不静默丢镜
      for (const s of activeShots) {
        if (!timeline.cuts.some((c) => String(c.shot) === String(s.id))) {
          const cam = addCamera({ name: `镜${s.id} 机位`,
                                  preset: s.camera_preset || "static" }, true);
          timeline.addCut({ shot: s.id, camera: cam.id, dur: snapDuration(s.dur || 5) });
        }
      }
    } else {
      // 首次进入（还没排过戏）→ **空台**：只铺时间轴必需的结构骨架——每个正镜
      // 一个镜头块 + 一个机位（有登记的 camera_preset 就用、否则静止），零角色、
      // 零道具、零走位、零动作。编排是导演的创作，全部由用户亲手搭建，或经
      // 「⧉ 复制 AI 编排指令」交指挥层按分镜图写入——引擎绝不代排。
      for (const s of activeShots) {
        const cam = addCamera({ name: `镜${s.id} 机位`,
                                preset: s.camera_preset || "static" }, true);
        timeline.addCut({ shot: s.id, camera: cam.id, dur: snapDuration(s.dur || 5) });
      }
    }
    timeline.normalize();
  }

  /* -------------------------------------------------------------- 场景增删 */
  function addCamera(spec = {}, quiet = false) {
    const id = spec.id || `cam_${++seq.cam}`;
    seq.cam = Math.max(seq.cam, parseInt(String(id).replace(/\D/g, ""), 10) || seq.cam);
    const c = {
      kind: "camera", id, name: spec.name || `机位 ${S.cameras.length + 1}`,
      preset: spec.preset || "static", fovScale: spec.fovScale ?? 1,
      // 跟随主体：运镜 preset 的关键帧是**主体相对**坐标，锚在谁身上决定了整段运动
      // 落在哪。null = 场上第一个角色（单人戏的常态），多人戏必须能点名
      subject: spec.subject || null,
      // 构图偏移 [fx, fy]：主体在画面里的落点（0=正中，±0.167=三分线）
      frame: Array.isArray(spec.frame) ? [+spec.frame[0] || 0, +spec.frame[1] || 0] : null,
      yaw: +spec.yaw || 0,    // 方位偏转（度）：整段运镜绕主体转到哪一侧拍
      dist: +spec.dist || 1,  // 径向距离缩放：整段运镜相对主体推远/拉近
      // 自定义轨道（世界坐标路点，null=跟随 preset 程序轨道）：拖轨迹路点即烘焙生成
      path: Array.isArray(spec.path) && spec.path.length >= 2
        ? spec.path.map((p) => [+p[0] || 0, +p[1] || 0, +p[2] || 0]) : null,
    };
    S.cameras.push(c);
    if (!quiet) { markDirty(); paintAll(); }
    return c;
  }

  function addActor(model, pos, spec = {}, quiet = false) {
    const a = new Actor({
      ...spec, model: model || spec.model, pos: pos || spec.pos || [0, 0, 0],
    });
    a.kind = "actor";
    scene.add(a.object);
    S.actors.push(a);
    refreshPathViz(a);
    if (!quiet) { select(a); markDirty(); paintAll(); }
    return a;
  }

  /** 道具的缺省名：目录里的中文标签 ＋ 同类序号。
   *
   *  大纲与时间轴按 `name` 显示，内部 key（`stairs` / `rampart`）在中文界面里读不
   *  出是什么；key 另在检查器标题陈列。序号避开已用值，同类道具才区分得开。 */
  function defaultPropName(kind) {
    const label = (dir.props || []).find((x) => x.key === kind)?.label || kind;
    const used = new Set(S.props.map((x) => x.name));
    let i = 1;
    while (used.has(`${label}${i}`)) i++;
    return `${label}${i}`;
  }

  function addProp(kind, pos, spec = {}, quiet = false) {
    const obj = buildProp(kind);
    obj.position.set(...(pos || spec.pos || [0, 0, 0]));
    obj.rotation.y = ((spec.rot || 0) * Math.PI) / 180;
    scene.add(obj);
    const p = {
      kind: "prop", id: spec.id || `prop_${++seq.prop}`,
      // 旧编排里自动生成的名字就是 kind 本身，一并升级为标签；用户改过的名字不动
      name: (spec.name && spec.name !== kind) ? spec.name : defaultPropName(kind),
      prop: kind, object: obj,
      get rotY() { return +(obj.rotation.y * 180 / Math.PI).toFixed(1); },
    };
    obj.userData.propId = p.id;
    S.props.push(p);
    if (!quiet) { select(p); markDirty(); paintAll(); }
    return p;
  }

  function remove(item) {
    if (S.hover === item) S.hover = null;
    if (item.kind === "actor") {
      const viz = S.pathLines.get(item.id);
      if (viz) { viz.removeFromParent(); disposeViz(viz); }
      S.pathLines.delete(item.id);
      if (S.drawingPathFor === item.id) { S.drawingPathFor = null; S.pathSnapshot = null; }
      item.dispose();
      S.actors = S.actors.filter((x) => x !== item);
    } else if (item.kind === "prop") {
      item.object.removeFromParent();
      S.props = S.props.filter((x) => x !== item);
    } else {
      // 删机位：把引用它的镜头块改挂到剩下的第一个机位——**绝不删镜头块**
      S.cameras = S.cameras.filter((x) => x !== item);
      const fallback = S.cameras[0]?.id;
      timeline.cuts.forEach((c) => { if (c.camera === item.id) c.camera = fallback; });
    }
    if (S.selected === item) select(null);
    markDirty();
    paintAll();
  }

  /* ---------------------------------------------------------------- 选择 */
  function select(item) {
    S.actors.forEach((a) => a.object.userData.material.emissive.setHex(BASE_TINT));
    S.selected = item || null;
    if (S.hover === item) S.hover = null;
    if (!item) {
      gizmo.detach();
      refreshAllPathViz();     // 取消选中 → 该角色的路点针收起
      setCursor();
      paintPanels();
      return;
    }
    if (item.kind === "actor") {
      // 高亮 = 脚下琥珀选中圈（syncAids 逐帧贴过去）+ **极暗的琥珀底色**。
      // 不把灰模刷成亮橙：previz 的角色是无身份体块，一旦有明显色差就会被读成
      // "另一个角色"（导出虽会摘掉，但预览期间一直误导）；圈在体外，不碰这层语义
      item.object.userData.material.emissive.setHex(SELECT_TINT);
      if (!timeline.playing) gizmo.attach(item.object);   // 播放中选中不弹手柄，暂停时 frame 补挂
      gizmo.setMode("translate");
      // **角色不给 Y 轴手柄**：人永远站在地面上，`onGizmoMove` 会把 y 压回 0。
      // 留着那根竖直箭头是个"看得见却永远无效"的假控件——而它恰恰画在角色正中、
      // 最容易被抓到，于是用户拖半天没反应，得出"拖动根本不生效"的结论（实测如此）。
      gizmo.showY = false;
    } else if (item.kind === "prop") {
      if (!timeline.playing) gizmo.attach(item.object);
      gizmo.setMode("translate");
      gizmo.showY = true;    // 道具可以架高（箱子放桌上），Y 轴是真有用的
    } else {
      gizmo.detach();          // 机位没有可拖的实体：它的运动由 preset 决定，不手摆
    }
    refreshAllPathViz();       // 选中角色 → 展开它的路点针（可拖改线）
    setCursor();
    paintPanels();
  }

  function onGizmoMove() {
    const it = S.selected;
    if (it?.kind === "actor") {
      it.object.position.y = 0;                   // 角色永远踩在地面上
      it.baseRotY = it.object.rotation.y;
      // **挪人连走位一起挪**：路线整体平移同样的位移量。不挪的话 `sceneAt` 下一帧
      // 按原曲线把位置写回原地（曲线每帧覆写位置），拖动不生效
      const gb = S.gizmoBase;
      if (gb && gb.path) {
        const dx = it.object.position.x - gb.pos.x;
        const dz = it.object.position.z - gb.pos.z;
        it.setPath(gb.path.map(([x, y, z]) =>
          [+(x + dx).toFixed(3), y, +(z + dz).toFixed(3)]));
        throttledPathViz(it);
      }
    } else if (it?.kind === "prop") {
      it.object.position.y = Math.max(0, it.object.position.y);
    }
    markDirty();
  }

  /* ------------------------------------------------ 视口手势（点选/平移/画线） */
  // 左键身兼两职：**拖动 = 平移画布（上帝视角），原地点击 = 点选/落位/加路点**。
  // 判据是 6px 位移阈值——低于阈值的平移肉眼不可察，点选优先；超过阈值就当拖动，
  // 绝不触发点选。若在 pointerdown 一落就选中，起手转个镜头都会误选到人——
  // 点击必须放到 pointerup 且确认没拖过才算数。
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const gesture = { down: null, moved: false };

  function eventRay(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1,
            -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, activeCamera());
    return ray;
  }

  function pickGround(ev) {
    const hit = eventRay(ev).intersectObject(ground, false)[0];
    return hit ? hit.point : null;
  }

  let cyc = { x: 0, y: 0, keys: "", i: 0 };
  function pickObject(ev, { cycle = false } = {}) {
    eventRay(ev);
    const targets = [...S.actors.map((a) => a.object), ...S.props.map((p) => p.object)];
    if (camViz.visible) targets.push(camBody);   // 机位实体可点选（选中该机位）
    const items = [];
    let weakCam = null;
    for (const hit of ray.intersectObjects(targets, true)) {
      let o = hit.object;
      // 视锥漏斗=**弱命中**：漏斗常正对主体，按强命中算会让「点主体」隔着
      // 漏斗误选机位——只有这一下没点到任何实体时才认它（先于父级攀爬判断，
      // 否则爬到 camBody 的 pickCamera 就成强命中了）
      if (o.userData.pickFrustum) {
        const cut = timeline.cutAt();
        weakCam = weakCam || (cut ? cameraById(cut.camera) : null);
        continue;
      }
      while (o && !o.userData.actorId && !o.userData.propId
             && !o.userData.pickCamera) o = o.parent;
      if (!o) continue;
      if (o.userData.pickCamera) {
        const cut = timeline.cutAt();
        const it = cut ? cameraById(cut.camera) : null;
        if (it && !items.includes(it)) items.push(it);
        continue;
      }
      const it = S.actors.find((a) => a.id === o.userData.actorId)
              || S.props.find((p) => p.id === o.userData.propId);
      if (it && !items.includes(it)) items.push(it);
    }
    if (!items.length && weakCam) return weakCam;
    if (!items.length) {
      // 邻近拾取：灰模四肢很细，点在体块缝隙里是常态——取距射线最近的角色兜底
      let best = null, bd = 0.55;
      for (const a of S.actors) {
        const c = a.object.position.clone().setY((a.object.userData.height || 1.7) / 2);
        const d = ray.ray.distanceToPoint(c);
        if (d < bd) { bd = d; best = a; }
      }
      return best;
    }
    if (!cycle) return items[0];
    // 同一点位反复点击 → 轮选重叠对象：人挡住箱子时，点第二下就选到后面的箱子
    const keys = items.map((x) => x.id).join(",");
    const near = Math.hypot(ev.clientX - cyc.x, ev.clientY - cyc.y) < 8;
    const i = near && keys === cyc.keys ? (cyc.i + 1) % items.length : 0;
    cyc = { x: ev.clientX, y: ev.clientY, keys, i };
    return items[i];
  }

  /** 路点针命中（针只在选中/画线中的角色路线上才有，见 pathtool）。 */
  function pickPin(ev) {
    const pins = [];
    for (const g of S.pathLines.values()) pins.push(...(g.userData.pins || []));
    if (!pins.length) return null;
    const hit = eventRay(ev).intersectObjects(pins, false)[0];
    return hit ? { ...hit.object.userData } : null;   // {actorId, index}
  }

  /** 机位轨道路点命中（针只在「选中的机位正是当前镜头块机位」时才有）。
   *  **屏幕空间拾取**（像素距离，恒定 14px 半径），不走射线打拾取球：轨道沿视线
   *  纵深展开时，各针离观察相机的距离差好几倍——固定世界尺寸的拾取球近大远小，
   *  且射线深度排序恒偏向近处，实测「按在机身上却拖动了 30px 外的远针」。手柄类
   *  控件按像素拾取是 DCC 通例（Blender 顶点 / gizmo 手柄同理）：谁在屏上离指针
   *  最近谁接管，机身同一口径竞争——指针落在机身图元上=挪整条，落在针珠上=改点。 */
  const PIN_PX = 14;
  function pickCamPin(ev) {
    if (!camPins || !camViz.visible) return null;
    const r = renderer.domElement.getBoundingClientRect();
    const px = (o) => {
      const v = o.getWorldPosition(new THREE.Vector3()).project(dirCam);
      return v.z > 1 ? null : [r.left + (v.x * 0.5 + 0.5) * r.width,
                               r.top + (1 - (v.y * 0.5 + 0.5)) * r.height];
    };
    // 珠子（水平拖）与 ↕ 手柄（垂直拖）同场竞争，谁在屏上离指针近谁接管
    let best = null;
    let bd = PIN_PX;
    const scan = (list, lift) => {
      for (const hit of list || []) {
        const s = px(hit);
        if (!s) continue;
        const d = Math.hypot(ev.clientX - s[0], ev.clientY - s[1]);
        if (d < bd) { bd = d; best = { index: hit.userData.index, lift }; }
      }
    };
    scan(camPins.userData.pins, false);
    scan(camPins.userData.lifts, true);
    if (!best) return null;
    const bs = px(camBody);
    if (bs && Math.hypot(ev.clientX - bs[0], ev.clientY - bs[1]) < bd) return null;
    return best;
  }

  /** 世界 Y 轴在屏幕上的投影（px/米）：垂直拖拽的换算基。指针沿这根轴投影走了
   *  多少像素，就升降多少米——任何视角都精确跟手（Blender 轴约束移动的同款数学；
   *  粗糙的「dy×固定系数」在俯仰角变化时会忽快忽慢）。 */
  function yAxisPx(worldPos) {
    const r = renderer.domElement.getBoundingClientRect();
    const a = worldPos.clone().project(dirCam);
    const b = worldPos.clone().add(new THREE.Vector3(0, 1, 0)).project(dirCam);
    return { x: (b.x - a.x) * 0.5 * r.width, y: -((b.y - a.y) * 0.5) * r.height };
  }
  function liftDelta(fromEv, toEv, worldPos) {
    const ax = yAxisPx(worldPos);
    const l2 = ax.x * ax.x + ax.y * ax.y;
    const dx = toEv.clientX - fromEv.x, dy = toEv.clientY - fromEv.y;
    // 正俯视时 Y 轴投影退化成一个点（l2→0）——退回固定系数，别除零
    if (l2 < 16) return -dy * 0.02;
    return (dx * ax.x + dy * ax.y) / l2;
  }

  // 捕获阶段监听：拖路点 / 拖对象必须在 OrbitControls 看到事件**之前**接管，
  // 否则它会把这次拖拽当成平移，对象和画布一起跑
  viewport.addEventListener("pointerdown", (ev) => {
    if (ev.target !== renderer.domElement || S.rendering) return;
    if (gizmo.axis) return;                     // 指针在 gizmo 手柄上，这一下归它
    // ⌥/Alt + 左键 = 环绕。触控板没有「右键拖」这个动作（两指按是短促的
    // 右键点击，按住拖极别扭），没有这条路触控板用户就永远转不了向——
    // Maya/Blender/C4D 全家都用 Alt 系修饰键，肌肉记忆现成
    if (ev.button === 0) {
      orbit.mouseButtons.LEFT = ev.altKey ? THREE.MOUSE.ROTATE : THREE.MOUSE.PAN;
    }
    gesture.down = { x: ev.clientX, y: ev.clientY, btn: ev.button };
    gesture.moved = false;
    if (ev.button !== 0 || ev.altKey) return;
    // 机位视角=**监视模式**：不选不拖对象——左键拖动改为「环视」（观察偏移
    // 叠在运镜之上；sceneAt 每帧重摆 rig，导出与监视器永远是纯运镜画面）
    if (S.viewMode !== "director") {
      ev.stopPropagation();
      S.dragLook = { x: ev.clientX, y: ev.clientY,
                     yaw: S.camLook.yaw, pitch: S.camLook.pitch };
      return;
    }
    const pin = pickPin(ev);
    if (pin) {
      ev.stopPropagation();
      timeline.pause();          // 拖拽期间画面不许自己动
      S.dragPin = pin;
      orbit.enabled = false;
      setCursor();
      return;
    }
    // 机位轨道路点：珠子=水平拖，↕ 手柄=垂直拖（屏幕空间拾取，见 pickCamPin）
    const cpin = pickCamPin(ev);
    if (cpin) {
      const cut = timeline.cutAt();
      const cam = cut ? cameraById(cut.camera) : null;
      if (cam && camViz.userData.bake) {
        ev.stopPropagation();
        timeline.pause();
        orbit.enabled = false;
        const wp = (cam.path || camViz.userData.bake)[cpin.index] || [0, 1.5, 0];
        S.dragCamPin = { cam, index: cpin.index, lift: cpin.lift,
                         mode: cpin.lift || ev.shiftKey,
                         m0: { x: ev.clientX, y: ev.clientY }, y0: wp[1],
                         bake: camViz.userData.bake.map((p) => [...p]) };
        setCursor();
        return;
      }
    }
    // **直接拖对象挪位**（角色连走位一起）：命中即接管。只给 gizmo 箭头不给
    // 直拖，用户的第一反应「按住人拖走」就永远落空——实测被点名「无法拖动」
    if (!S.placing && !S.drawingPathFor) {
      const it = pickObject(ev);
      if (it && it.kind === "camera") {
        // 拖机身：preset 模式=**绕主体转方位+定距离**（机位位置由 preset×主体锚点
        // 求值，能自由摆的量是「从哪一侧拍、离多远」）；自定义轨道模式=**整条轨道
        // 平移**（与「挪角色连走位一起挪」同一语言——形状是你拖出来的，不该被机身
        // 一拖就变形）。两种都**在机身高度的水平面上拖**，不是地面：机身悬在
        // 1.5~8m 高，用「指针下的地面点」求角度/半径会带巨大视差（俯角越小地面点
        // 甩得越远），一动就 yaw/dist 乱跳，轨迹速度随之错乱
        ev.stopPropagation();
        timeline.pause();
        orbit.enabled = false;
        const camY = shotCam.position.y;
        const gp = pickPlane(ev, camY);
        if (ev.shiftKey) {
          // ⇧+拖机身 = **整条轨道垂直升降**（迈克尔·贝式低走高：先整体抬起或
          // 压低，再用路点针的 ↕ 手柄把起末端拉出高度差）。preset 轨道先烘焙；
          // 静止机位没有轨道可升——直说，别静默烘一条零长度的坏轨道
          const cut = timeline.cutAt();
          const had = !!it.path;
          if (!cut || !ensureCamPath(it, cut)) {
            toast("固定机位没有轨道可升降——先换一个运动运镜（如环绕/升降）", true);
            select(it);
            orbit.enabled = true;
            return;
          }
          S.dragCam = { cam: it, mode: "lift", didBake: !had,
                        base: it.path.map((p) => [...p]),
                        m0: { x: ev.clientX, y: ev.clientY },
                        ref: camBody.position.clone() };
        } else if (it.path && (ev.metaKey || ev.ctrlKey)) {
          // ⌘/Ctrl+拖机身 = 整条轨道水平平移——「挪整条」是大动作，必须是
          // 显式意图（修饰键），缺省拖机身只挪起点（见下）
          S.dragCam = { cam: it, mode: "path", y: camY,
                        base: it.path.map((p) => [...p]),
                        grab: gp ? [gp.x, gp.z] : [0, 0] };
        } else if (it.path) {
          // 拖机身（自定义轨道·无修饰键）= **只挪起点（1 号路点=开拍位置）**。
          // 若缺省整条平移，「想摆个开拍位置，一拖把后续路点全带走」会是
          // 高频误操作；从大纲点选机位时播放头正停在镜头起点、机身就压在
          // 起点针上，「按住机身拖」的直觉本来就是「挪这个开拍位」
          const sy = it.path[0][1];
          const gs = pickPlane(ev, sy);
          S.dragCam = { cam: it, mode: "start", y: sy,
                        base: it.path[0].slice(),
                        grab: gs ? [gs.x, gs.z] : [0, 0] };
        } else {
          const o = subjectOf(it).anchor().origin;
          S.dragCam = {
            cam: it, mode: "orbit", cx: o.x, cz: o.z, y: camY,
            startYaw: it.yaw || 0, startDist: it.dist || 1,
            r0: gp ? Math.max(0.2, Math.hypot(gp.x - o.x, gp.z - o.z)) : 1,
            grabAng: gp ? Math.atan2(gp.x - o.x, gp.z - o.z) : 0,
          };
        }
        select(it);
        setCursor();
        return;
      }
      if (it) {
        ev.stopPropagation();
        timeline.pause();        // 播放中拖人=曲线求值和手互相抢，先停表
        orbit.enabled = false;
        const gp = pickGround(ev);
        S.dragObj = {
          item: it,
          grab: gp ? [gp.x, gp.z] : [it.object.position.x, it.object.position.z],
          base: it.object.position.clone(),
          basePath: (it.kind === "actor" && it.pathPoints)
            ? it.pathPoints.map((p) => [...p]) : null,
          y: it.object.position.y,
        };
      }
    }
  }, true);

  const onWinMove = (ev) => {
    if (gesture.down
        && Math.hypot(ev.clientX - gesture.down.x, ev.clientY - gesture.down.y) > 6) {
      gesture.moved = true;
    }
    if (S.dragLook) {
      const dx = ev.clientX - S.dragLook.x, dy = ev.clientY - S.dragLook.y;
      S.camLook.yaw = S.dragLook.yaw - dx * 0.0032;
      S.camLook.pitch = THREE.MathUtils.clamp(
        S.dragLook.pitch - dy * 0.0032, -0.9, 0.9);
      return;
    }
    if (S.dragPin) { dragPinTo(ev); return; }
    if (S.dragCamPin) { dragCamPinTo(ev); return; }
    if (S.dragObj) { dragObjTo(ev); return; }
    if (S.dragCam) { dragCamTo(ev); return; }
    if (ev.target === renderer.domElement && ev.buttons === 0) hoverAt(ev);
  };
  window.addEventListener("pointermove", onWinMove);

  const onWinUp = (ev) => {
    if (S.dragLook) { S.dragLook = null; return; }
    if (S.dragCam) {
      const d = S.dragCam;
      S.dragCam = null;
      orbit.enabled = true;
      setCursor(); markDirty(); paintPanels(); updateHud(); setDragHint(null);
      if (d.didBake) toast("已转为自定义轨道——↕ 手柄/⇧ 调高度，检查器 ↺ 恢复预设");
      return;
    }
    if (S.dragPin) {
      S.dragPin = null;
      orbit.enabled = true;
      markDirty(); setCursor(); paintAll();
      return;
    }
    if (S.dragCamPin) {
      const d = S.dragCamPin;
      S.dragCamPin = null;
      orbit.enabled = true;
      setCursor(); markDirty(); paintPanels(); updateHud(); setDragHint(null);
      if (d.didBake) toast("已转为自定义轨道——↕ 手柄/⇧ 调高度，检查器 ↺ 恢复预设");
      return;
    }
    if (S.dragObj) {
      const d = S.dragObj;
      S.dragObj = null;
      orbit.enabled = true;
      setCursor();
      if (gesture.moved) {
        // 松手落格：0.25m（与 gizmo 吸附同格）——拖时顺滑，落点仍是准的
        const snap = (v) => Math.round(v / 0.25) * 0.25;
        applyObjDrag(d, snap(d.item.object.position.x), snap(d.item.object.position.z));
        if (d.item.kind === "actor") refreshPathViz(d.item);
        gesture.down = null;
        markDirty(); paintAll();
        return;
      }
      // 没拖动 → 落到下面的通用点击流程（选中 / 轮选）
    }
    const down = gesture.down;
    gesture.down = null;
    if (!down || down.btn !== ev.button || gesture.moved) return;
    if (ev.target !== renderer.domElement || gizmo.dragging) return;
    if (ev.button === 2) { if (S.drawingPathFor) undoPathPoint(); return; }
    if (ev.button !== 0) return;
    clickAt(ev);
  };
  window.addEventListener("pointerup", onWinUp);

  /** 对象直拖：沿对象自身高度的水平面移动。
   *  **拖动中不吸附**——0.25m 硬吸附会让对象一格格蹦、在格界处随手抖来回横跳
   *  （实测被点名「抖动很厉害」），松手时再一次落格（与 gizmo 同格，落点仍是准的）。
   *  求值收进 rAF：pointermove 在触控板上可到 120Hz，逐事件重建走位曲线是
   *  拖动发抖的另一半原因——每帧只算最后一个事件。 */
  const dragPlane = new THREE.Plane();
  const planeHit = new THREE.Vector3();
  function pickPlane(ev, y) {
    dragPlane.set(new THREE.Vector3(0, 1, 0), -y);
    return eventRay(ev).ray.intersectPlane(dragPlane, planeHit) ? planeHit : null;
  }
  let dragEv = null;
  let dragRaf = false;
  function dragObjTo(ev) {
    dragEv = ev;
    if (dragRaf) return;
    dragRaf = true;
    requestAnimationFrame(() => {
      dragRaf = false;
      const e = dragEv, d = S.dragObj;
      if (!e || !d || !gesture.moved) return;   // <6px 还算点击，先不动
      if (S.selected !== d.item) select(d.item);   // 拖=操纵意图，顺带选中
      const p = pickPlane(e, d.item.kind === "actor" ? 0 : d.y);
      if (!p) return;
      applyObjDrag(d, d.base.x + (p.x - d.grab[0]), d.base.z + (p.z - d.grab[1]));
    });
  }
  function applyObjDrag(d, nx, nz) {
    d.item.object.position.x = nx;
    d.item.object.position.z = nz;
    if (d.item.kind === "actor") {
      d.item.object.position.y = 0;
      if (d.basePath) {
        // 位移量相对起拖点：路线整体平移，人与路线的相对关系一丝不变
        const dx = nx - d.base.x, dz = nz - d.base.z;
        d.item.setPath(d.basePath.map(([x, y, z]) =>
          [+(x + dx).toFixed(3), y, +(z + dz).toFixed(3)]));
        throttledPathViz(d.item);
      }
    }
    markDirty();
  }

  let vizRaf = false;
  function throttledPathViz(a) {
    if (vizRaf) return;
    vizRaf = true;
    requestAnimationFrame(() => { vizRaf = false; refreshPathViz(a); });
  }

  /** 拖机身（rAF 节流；一切相对起拖点算，播放中也不累加漂移）：
   *  preset 模式=绕主体转方位+定距离；自定义轨道模式=整条轨道平移。 */
  let camEv = null;
  let camRaf = false;
  function dragCamTo(ev) {
    camEv = ev;
    if (camRaf) return;
    camRaf = true;
    requestAnimationFrame(() => {
      camRaf = false;
      const d = S.dragCam;
      if (!d) return;
      if (d.mode === "lift") {
        // 整条轨道垂直升降：Δ 沿世界 Y 轴的屏幕投影换算（任何视角都跟手）
        const delta = liftDelta(d.m0, camEv, d.ref);
        d.cam.path = d.base.map(([x, y, z]) =>
          [x, +THREE.MathUtils.clamp(y + delta, 0.05, 40).toFixed(3), z]);
        setDragHint(`轨道整体升降 · Δ ${delta >= 0 ? "+" : ""}${delta.toFixed(2)}m`);
        markDirty();
        return;
      }
      const p = pickPlane(camEv, d.y);   // 与起拖同一水平面——视差归零
      if (!p) return;
      if (d.mode === "start") {
        // 只挪起点：后续路点纹丝不动（运动形状保留，换个开拍位置）
        const pt = d.cam.path[0];
        pt[0] = +(d.base[0] + (p.x - d.grab[0])).toFixed(3);
        pt[2] = +(d.base[2] + (p.z - d.grab[1])).toFixed(3);
        setDragHint(`轨道起点 · x ${pt[0].toFixed(2)} · z ${pt[2].toFixed(2)}`
          + "（⌘/Ctrl 拖=整条平移 · ⇧=整条升降）");
        markDirty();
        return;
      }
      if (d.mode === "path") {
        // 整条轨道平移（xz）：位移量相对起拖点，形状与各点高度一丝不变
        const dx = p.x - d.grab[0], dz = p.z - d.grab[1];
        d.cam.path = d.base.map(([x, y, z]) =>
          [+(x + dx).toFixed(3), y, +(z + dz).toFixed(3)]);
        setDragHint(`轨道整条平移 · Δx ${(p.x - d.grab[0]).toFixed(2)} · `
          + `Δz ${(p.z - d.grab[1]).toFixed(2)}`);
        markDirty();
        return;
      }
      const ang = Math.atan2(p.x - d.cx, p.z - d.cz);
      let yaw = Math.round(d.startYaw + ((ang - d.grabAng) * 180) / Math.PI);
      yaw = ((yaw + 540) % 360) - 180;
      d.cam.yaw = yaw;
      // 径向距离同步跟手：**所拖即所播**——把机身放到哪，播放时那一刻相机就在哪
      const r = Math.hypot(p.x - d.cx, p.z - d.cz);
      d.cam.dist = +THREE.MathUtils.clamp(
        d.startDist * (r / d.r0), 0.3, 3).toFixed(2);
      markDirty();
    });
  }

  /** 拖机位轨道路点（rAF 节流）：**第一下真实位移时烘焙**——preset 程序轨道原样
   *  转成显示中的 5 个世界路点（形状不变，只是从「程序算的」变成「你可编辑的」），
   *  随后逐点自由改。珠子拖=该点自身高度的水平面（pickPlane，与拖机身同一套视差
   *  修正）；**↕ 手柄或 ⇧ = 垂直约束拖**（沿世界 Y 轴屏幕投影精确换算——相机要能
   *  上天，走位针没有这条是因为人贴地）。⇧ 中途按下/松开会重新锚定，不跳变。 */
  let cpinEv = null;
  let cpinRaf = false;
  function dragCamPinTo(ev) {
    cpinEv = ev;
    if (cpinRaf) return;
    cpinRaf = true;
    requestAnimationFrame(() => {
      cpinRaf = false;
      const d = S.dragCamPin;
      const e = cpinEv;
      if (!d || !e || !gesture.moved) return;   // <6px 还算点击：不烘焙也不动点
      if (!d.cam.path) {
        d.cam.path = d.bake.map((p) => [...p]);   // 烘焙：所见即所得
        d.didBake = true;
      }
      const pt = d.cam.path[d.index];
      if (!pt) return;
      const vertical = d.lift || e.shiftKey;
      if (vertical !== d.mode) {          // ⇧ 状态变了 → 重新锚定，避免跳变
        d.mode = vertical;
        d.m0 = { x: e.clientX, y: e.clientY };
        d.y0 = pt[1];
      }
      if (vertical) {
        pt[1] = +THREE.MathUtils.clamp(
          d.y0 + liftDelta(d.m0, e, new THREE.Vector3(pt[0], pt[1], pt[2])),
          0.05, 40).toFixed(3);
      } else {
        const p = pickPlane(e, pt[1]);
        if (p) { pt[0] = +p.x.toFixed(3); pt[2] = +p.z.toFixed(3); }
      }
      // 数值回显（Blender 拖点时页眉显示坐标的同款反馈）——拖到哪心里有数
      setDragHint(`轨道点 ${d.index + 1} · x ${pt[0].toFixed(2)} · `
        + `z ${pt[2].toFixed(2)} · 高 ${pt[1].toFixed(2)}m`
        + (vertical ? "（垂直中）" : "（↕ 手柄或 ⇧ = 垂直）"));
      markDirty();
    });
  }

  renderer.domElement.addEventListener("dblclick", (ev) => {
    if (S.viewMode !== "director") {            // 监视模式：双击 = 环视回正
      S.camLook.yaw = S.camLook.pitch = 0;
      return;
    }
    if (S.drawingPathFor) { finishPathDraw(true); return; }
    const it = pickObject(ev);
    if (it && it.kind === "camera") { select(it); return; }
    if (it) { select(it); focusSubject(it); }   // 双击 = 选中并把它框满视口
  });

  function clickAt(ev) {
    if (S.drawingPathFor) { addPathPoint(ev); return; }
    if (S.placing) {
      const p = pickGround(ev);
      if (!p) return;
      const pos = [+p.x.toFixed(3), 0, +p.z.toFixed(3)];
      if (S.placing.kind === "actor") addActor(S.placing.key, pos);
      else addProp(S.placing.key, pos);
      S.placing = null;
      ghost.visible = false;
      setCursor();
      paintAll();
      return;
    }
    const it = pickObject(ev, { cycle: true });
    if (it) select(it);
    else if (S.selected) select(null);          // 点空处 = 取消选中（DCC 通例）
  }

  /** 悬停反馈：可选对象亮悬停圈 + pointer 光标；落位/画线时改为幽灵落点跟随。 */
  function hoverAt(ev) {
    if (S.viewMode !== "director") {            // 监视模式不与对象交互
      ghost.visible = rubber.visible = false;
      if (S.hover) { S.hover = null; setCursor(); }
      return;
    }
    if (S.placing || S.drawingPathFor) {
      const p = pickGround(ev);
      ghost.visible = !!p;
      if (p) ghost.position.set(p.x, 0.012, p.z);
      if (p && S.drawingPathFor && S.pathBuf.length) {
        rubber.userData.update(S.pathBuf[S.pathBuf.length - 1], p);
        rubber.visible = true;
      } else rubber.visible = false;
      if (S.hover) { S.hover = null; setCursor(); }
      return;
    }
    ghost.visible = rubber.visible = false;
    const it = pickObject(ev);
    if (S.hover !== it) { S.hover = it; setCursor(); }
    // 机位轨道针的悬停反馈：放大 + pointer / ns-resize 光标（frame 应用缩放）
    const hp = pickCamPin(ev);
    if ((hp?.index ?? -1) !== (S.hoverCamPin?.index ?? -1)
        || !!hp?.lift !== !!S.hoverCamPin?.lift) {
      S.hoverCamPin = hp;
      setCursor();
    }
  }

  /** 光标语义单一出口：默认抓手（可平移）· 悬停可选对象 pointer · 落位/画线十字
   *  · ↕ 手柄与垂直拖 ns-resize（光标就是操作说明——DCC 的第一层教学）。 */
  function setCursor() {
    viewport.classList.toggle("aim", !!(S.placing || S.drawingPathFor));
    viewport.classList.toggle("pick",
      !(S.placing || S.drawingPathFor) && !!(S.hover || S.hoverCamPin));
    viewport.classList.toggle("dragpin",
      !!(S.dragPin || S.dragObj || S.dragCam || S.dragCamPin));
    viewport.classList.toggle("lift",
      !!(S.hoverCamPin?.lift || S.dragCamPin?.lift
         || (S.dragCam && S.dragCam.mode === "lift")));
  }

  /* ------------------------------------------------------ 走位路线（画/改/撤） */
  function addPathPoint(ev) {
    const p = pickGround(ev);
    const a = S.actors.find((x) => x.id === S.drawingPathFor);
    if (!p || !a) return;
    S.pathBuf.push([+p.x.toFixed(3), 0, +p.z.toFixed(3)]);
    applyPathBuf(a);
    paintAll();          // 走位一变，检查器的地速/步态与动作轨的 ⤳ 都要跟着变
  }

  function undoPathPoint() {
    const a = S.actors.find((x) => x.id === S.drawingPathFor);
    if (!a || S.pathBuf.length <= 1) return;    // 起点是角色站位，不可撤销
    S.pathBuf.pop();
    applyPathBuf(a);
    paintAll();
  }

  function applyPathBuf(a) {
    a.setPath(S.pathBuf.length >= 2 ? S.pathBuf : null);
    refreshPathViz(a);
    updateHud();
  }

  /** 结束画线并落定路线；`fromDbl` 时双击的第二下不算路点。 */
  function finishPathDraw(fromDbl = false) {
    const a = S.actors.find((x) => x.id === S.drawingPathFor);
    if (!a) { S.drawingPathFor = null; return; }
    if (fromDbl && S.pathBuf.length >= 2) {
      const [x1, , z1] = S.pathBuf[S.pathBuf.length - 1];
      const [x0, , z0] = S.pathBuf[S.pathBuf.length - 2];
      if (Math.hypot(x1 - x0, z1 - z0) < 0.12) S.pathBuf.pop();
    }
    if (S.pathBuf.length >= 2) { a.setPath(S.pathBuf); markDirty(); }
    else if (S.pathSnapshot) a.setPath(S.pathSnapshot);   // 没画出新点 → 保留原路线
    else a.setPath(null);
    exitPathDraw(a);
  }

  /** Esc 取消：整条恢复到进画线模式之前的样子——画错了不该留下半条残线。 */
  function cancelPathDraw() {
    const a = S.actors.find((x) => x.id === S.drawingPathFor);
    if (a) a.setPath(S.pathSnapshot || null);
    exitPathDraw(a);
  }

  function exitPathDraw(a) {
    S.drawingPathFor = null;
    S.pathSnapshot = null;
    S.pathBuf = [];
    ghost.visible = rubber.visible = false;
    if (a) refreshPathViz(a);
    setCursor(); updateHud(); paintAll();
  }

  /** 拖针改线（rAF 节流：TubeGeometry 重建别跟着 pointermove 的频率跑）。 */
  let pinRaf = false;
  function dragPinTo(ev) {
    if (pinRaf) return;
    pinRaf = true;
    requestAnimationFrame(() => {
      pinRaf = false;
      const pin = S.dragPin;
      if (!pin) return;
      const p = pickGround(ev);
      const a = S.actors.find((x) => x.id === pin.actorId);
      if (!p || !a) return;
      const pt = [+p.x.toFixed(3), 0, +p.z.toFixed(3)];
      if (S.drawingPathFor === a.id) {
        S.pathBuf[pin.index] = pt;
        applyPathBuf(a);
      } else if (a.pathPoints && a.pathPoints[pin.index]) {
        const pts = a.pathPoints.map((q) => [...q]);
        pts[pin.index] = pt;
        a.setPath(pts);
        refreshPathViz(a);
        paintPanels();      // 路线长度 / 地速 / 步态倍速跟着变
      }
    });
  }

  function refreshPathViz(a) {
    const old = S.pathLines.get(a.id);
    if (old) { old.removeFromParent(); disposeViz(old); S.pathLines.delete(a.id); }
    const drawing = S.drawingPathFor === a.id;
    const pts = drawing ? S.pathBuf : a.pathPoints;
    if (!pts || pts.length < (drawing ? 1 : 2)) return;   // 画线中单点也画（起点圈）
    const viz = buildPathViz(pts, {
      color: hexOf("--amber", "#f0a63c"),
      selected: drawing || S.selected?.id === a.id,
      actorId: a.id,
    });
    scene.add(viz);
    S.pathLines.set(a.id, viz);
  }

  function refreshAllPathViz() { for (const a of S.actors) refreshPathViz(a); }

  /* --------------------------------------------------- 唯一求值函数 sceneAt */
  function rigFor(cam) {
    const p = findPreset(catalog, cam.preset);
    let rig = S.rigs.get(cam.id);
    if (!rig || rig.preset.key !== p?.key) {
      rig = new CameraRig(p || catalog[0], {
        anchorFn: () => subjectOf(cam).anchor(),
        fovScale: cam.fovScale,
      });
      S.rigs.set(cam.id, rig);
    }
    rig.fovScale = cam.fovScale;
    rig.frame = cam.frame;
    rig.yawOffset = ((cam.yaw || 0) * Math.PI) / 180;
    rig.distScale = cam.dist || 1;
    rig.setCustomPath(cam.path);   // 内部按内容缓存，没变不重建曲线
    return rig;
  }

  /** 运镜的「主体」= 第一个角色（没有角色时用原点替身，运镜照样能预览）。 */
  const ORIGIN_SUBJECT = {
    anchor: () => ({ origin: new THREE.Vector3(), quaternion: new THREE.Quaternion() }),
    lookPoint: () => new THREE.Vector3(0, 1.5, 0),
  };
  function subjectOf(cam) {
    if (cam?.subject) {
      const a = S.actors.find((x) => x.id === cam.subject);
      if (a) return a;                 // 指名的角色被删了就静默回落，不让整条渲染崩
    }
    return S.actors[0] || ORIGIN_SUBJECT;
  }

  /**
   * **预览与导出共用的唯一求值函数**：给定绝对时间 t，把整个场景摆好。
   * 纯函数式（只依赖 t），因此逐帧导出与实时预览得到完全一样的画面。
   */
  function sceneAt(t) {
    for (const a of S.actors) a.update(t, timeline.duration);
    snapToSeats(t);
    const { cut, local } = timeline.localOf(t);
    if (cut) {
      const cam = cameraById(cut.camera);
      if (cam) rigFor(cam).apply(shotCam, local, { subjectPos: subjectOf(cam).lookPoint() });
    }
  }

  /* --------------------------------------------- 动作 × 道具（落座/骑乘吸附） */
  // 「上马要有马、上车要有车」——动作到位且附近有对应坐骑/坐具，就把角色根节点
  // 吸附到座面（rig.PROPS[].seat，随道具朝向旋转）：骑乘姿落在鞍上、上车坐进舱里、
  // 坐下挨着椅凳。确定性纯函数（只看 t 时刻的动作与距离），导出同源。
  // 拖动坐骑=连人一起走（吸附每帧重算）；正被拖动的角色不吸附（手优先）。
  // 本表必须覆盖所有声明了 `seat` 的道具，守卫 test_every_seat_anchor_is_reachable。
  const SEAT_FOR = {
    ride: ["horse", "vehicle"],
    enter: ["vehicle"],
    sit: ["chair", "sofa", "bench", "box", "rock", "bed", "altar", "log", "well"],
  };
  const SEAT_V = new THREE.Vector3();
  const SEAT_UP = new THREE.Vector3(0, 1, 0);
  function snapToSeats(t) {
    for (const a of S.actors) {
      if (S.dragObj && S.dragObj.item === a) continue;
      const kinds = SEAT_FOR[a.segmentAt(t).action];
      if (!kinds) continue;
      let best = null;
      let bd = 1.8;
      for (const p of S.props) {
        if (!kinds.includes(p.prop) || !PROPS[p.prop]?.seat) continue;
        const d = Math.hypot(p.object.position.x - a.object.position.x,
                             p.object.position.z - a.object.position.z);
        if (d < bd) { bd = d; best = p; }
      }
      if (!best) continue;
      const seat = PROPS[best.prop].seat;
      SEAT_V.set(seat[0], 0, seat[2])
        .applyAxisAngle(SEAT_UP, best.object.rotation.y)
        .add(best.object.position);
      // 高度按「座面 − 该姿势的臀底相对根节点高度」求，而不是逐道具手调偏移：
      // 骑乘与坐下的髋高不同、儿童与成人的骨盆高度不同，都由这一步吸收
      const butt = a.object.userData.bones.hips.position.y - pelvisDrop(a.model);
      a.object.position.set(SEAT_V.x, best.object.position.y + seat[1] - butt, SEAT_V.z);
      a.object.rotation.y = best.object.rotation.y;   // 与坐骑/坐具同朝向
    }
  }

  /* ------------------------------------------------------------ 导出洁净模式 */
  /**
   * 渲染 previz 前把**所有编辑器辅助物**摘掉，渲完恢复。
   *
   * 这不是"顺手清理"，是硬要求：previz 会作为 `reference_video` 直接喂给 Seedance
   * 迁移运动，画面里多一根绿色 gizmo 箭头、一圈琥珀站位环、一条路线样条，模型就会
   * 当成场景内容试着复现——成片里凭空多出几道彩色轨迹。选中高亮（emissive）同理：
   * 被选中的角色会渲成橙色，而 previz 是**无身份灰模**，一有颜色差异 Seedance 就
   * 可能把它读成两个不同的角色。
   */
  function setExportMode(on) {
    // gizmo 的恢复**不是无条件点亮**：root 可见性的真值=「挂着对象」。写成
    // `= !on` 时，PiP 洁净渲染每帧成对调本函数，一帧内就把 detach 后本应消失的
    // root 重新点亮——留下一支冻在最后挂载位置的**幽灵 gizmo**（实测截图：选中
    // 机位/点空处后 detach，人沿走位走远，手柄却留在原地）。
    const helper = gizmo.getHelper ? gizmo.getHelper() : gizmo;
    helper.visible = on ? false : !!gizmo.object;
    mark.visible = !on;
    axis.visible = !on;
    aids.visible = !on;      // 选中圈 / 悬停圈 / 幽灵落点 / 橡皮线，一组全摘
    for (const line of S.pathLines.values()) line.visible = !on;
    for (const a of S.actors) {
      a.object.userData.material.emissive.setHex(
        on ? BASE_TINT : (S.selected === a ? SELECT_TINT : BASE_TINT));
    }
  }

  /* ---------------------------------------------------------------- 渲染循环 */
  let alive = true;
  let raf = 0;
  function resize() {
    const w = viewport.clientWidth || 960, hh = viewport.clientHeight || 540;
    renderer.setSize(w, hh, false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    dirCam.aspect = w / hh;
    dirCam.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(viewport);
  resize();

  function activeCamera() { return S.viewMode === "camera" ? shotCam : dirCam; }

  /**
   * 把选中项（否则第一个角色）**完整框进视口**——3D 工作台的必备逃生舱。
   *
   * 按包围球 + 视锥角求「刚好装下」的距离（28% 余量），仰角钳进 15°~38° 的
   * 舒适带：全身可见、居中、不贴地、不俯冲；方位角保持现状（用户选好的观察
   * 方向不该被抢走）。若只挪 target、把距离夹死在固定区间，人会被裁半身、
   * 或缩成远处一点——「白模看不全、不在正中」。
   */
  function focusSubject(item = null) {
    const t = item || S.selected || S.actors[0] || null;
    let center = new THREE.Vector3(0, 1.1, 0), radius = 2.4;
    if (t && t.object) {
      const bs = new THREE.Box3().setFromObject(t.object)
        .getBoundingSphere(new THREE.Sphere());
      center = bs.center.clone();
      radius = Math.max(0.7, bs.radius);
    }
    const vFov = THREE.MathUtils.degToRad(dirCam.fov);
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * dirCam.aspect);
    const dist = (radius * 1.28) / Math.sin(Math.min(vFov, hFov) / 2);
    const dir = dirCam.position.clone().sub(orbit.target);
    if (dir.lengthSq() < 1e-6) dir.set(0.9, 0.66, 1.35);
    const sph = new THREE.Spherical().setFromVector3(dir);
    sph.phi = THREE.MathUtils.clamp(sph.phi, Math.PI * 0.29, Math.PI * 0.42);
    sph.radius = THREE.MathUtils.clamp(dist, orbit.minDistance, orbit.maxDistance);
    S.topSaved = null;                       // 聚焦即离开顶视，别把旧视角再弹回来
    topBtn.classList.remove("on");
    orbit.target.copy(center);
    dirCam.position.copy(center).add(new THREE.Vector3().setFromSpherical(sph));
    orbit.update();
  }

  /** 顶视图：正上方俯瞰全场（画走位 / 排站位最准的视角），再按一次回到原视角。 */
  function toggleTop() {
    if (S.topSaved) {
      dirCam.position.copy(S.topSaved.pos);
      orbit.target.copy(S.topSaved.tgt);
      S.topSaved = null;
    } else {
      S.topSaved = { pos: dirCam.position.clone(), tgt: orbit.target.clone() };
      const c = orbit.target.clone().setY(0);
      const d = THREE.MathUtils.clamp(
        dirCam.position.distanceTo(orbit.target) * 1.2, 9, 34);
      // z 偏移一丝：正上方竖直向下会让 lookAt 的 up 向量退化（画面突然翻转）
      dirCam.position.set(c.x, d, c.z + 0.01);
      orbit.target.copy(c);
    }
    orbit.update();
    topBtn.classList.toggle("on", !!S.topSaved);
  }

  /** 左右栏开合：改类名即可（栅格列宽走 CSS），随后 resize 让渲染器跟上新宽度。 */
  function syncPanes() {
    main.classList.toggle("no-left", !S.panels.left);
    main.classList.toggle("no-right", !S.panels.right);
    paneL.textContent = S.panels.left ? "‹" : "›";
    paneR.textContent = S.panels.right ? "›" : "‹";
    requestAnimationFrame(resize);
  }

  /** 选中圈 / 悬停圈逐帧贴到对象脚下——角色会沿走位移动，圈必须跟着。
   *  **播放中一律隐藏**：操纵件是编辑辅助，播放时脚下一圈琥珀
   *  跟着角色满场跑（或被甩在原地）都是画面干扰，暂停即恢复（实测被点名）。 */
  function ringRadius(it) {
    if (it.kind === "actor") return 0.55;
    const s = it.object?.userData?.size;
    return s ? Math.hypot(s[0], s[2]) / 2 + 0.16 : 0.7;
  }
  function syncAids() {
    const sel = (!timeline.playing && S.selected && S.selected.kind !== "camera")
      ? S.selected : null;
    if (sel) {
      selRing.position.set(sel.object.position.x, 0.008, sel.object.position.z);
      selRing.scale.setScalar(ringRadius(sel));
    }
    selRing.visible = !!sel;
    const hov = (!timeline.playing && S.hover && S.hover !== S.selected && S.hover.object)
      ? S.hover : null;
    if (hov) {
      hoverRing.position.set(hov.object.position.x, 0.007, hov.object.position.z);
      hoverRing.scale.setScalar(ringRadius(hov) * 1.04);
    }
    hoverRing.visible = !!hov;
  }

  /* ------------------------------------------------ 机位可视（导演视角） */
  /** 机位实体贴着 rig 求值后的 shotCam 位姿；轨迹按整个镜头块采样真实世界路径
   *  （自定义轨道直接采曲线——位置本来就是世界坐标，48 次 sceneAt 是白跑）。
   *  轨迹带缓存键（镜/机位/preset/焦距/构图/主体与其走位/自定义轨道/选中态）——
   *  主体或路点一动轨迹就重算，静止机位不画轨迹（一个点的"轨迹"只会误导）。
   *  **选中该机位时轨迹线上立起 5 枚可拖路点针**（与「选中角色展开走位针」同规）：
   *  preset 模式下针的落点 = 弧长均分的烘焙预览，拖任意一枚即转自定义轨道。 */
  let trajLine = null;
  let camPins = null;
  let trajKey = "";
  function syncCamViz(cutNow) {
    const show = S.viewMode === "director" && !!cutNow;
    if (camViz.visible !== show) camViz.visible = show;
    if (!show) return;
    camBody.position.copy(shotCam.position);
    camBody.quaternion.copy(shotCam.quaternion);
    const cam = cameraById(cutNow.camera);
    const a = cam ? subjectOf(cam) : null;
    // 播放中收起路点针（编辑手柄），轨迹降为细暗管——编辑辅助不遮挡回放画面
    const sel = !!cam && S.selected === cam && !timeline.playing;
    const key = [cutNow.shot, cutNow.camera, cam?.preset, cam?.fovScale,
      cam?.yaw, cam?.dist, (cam?.frame || []).join(","), cam?.subject, cutNow.dur,
      sel ? "S" : "-", cam?.path ? JSON.stringify(cam.path) : "-",
      a?.pathPoints ? a.pathLength.toFixed(2) : "-",
      a?.object ? a.object.position.toArray().map((v) => (2 * v).toFixed(0)).join(",") : "-",
    ].join("|");
    // 视锥每帧随焦距张合（dolly-zoom 的「后退变广」直接看得见）
    const vRad = (shotCam.fov * Math.PI) / 360;
    const fh = Math.tan(vRad) * 1.15;
    camFrustum.scale.set(fh * shotCam.aspect, fh, 1.15);
    if (key === trajKey) return;
    trajKey = key;
    if (trajLine) { trajLine.removeFromParent(); disposeViz(trajLine); trajLine = null; }
    if (camPins) { camPins.removeFromParent(); disposeViz(camPins); camPins = null; }
    camViz.userData.bake = null;
    const pts = cam?.path ? sampleCustomPath(cam) : sampleCamTraj(cutNow);
    const len = pts.reduce((s, p, i) => (i ? s + p.distanceTo(pts[i - 1]) : 0), 0);
    if (len < 0.3) return;            // 静止机位：无轨迹也无针（拖机身调方位即可）
    trajLine = buildCamTraj(pts, hexOf("--cyan", "#4cc3d9"), { selected: sel });
    camViz.add(trajLine);
    if (sel) {
      // 针的落点即数据：preset 模式=烘焙预览（弧长均分 5 点），自定义模式=路点本身
      const wp = cam.path ? cam.path.map((p) => [...p]) : bakeWaypoints(pts);
      camViz.userData.bake = wp;
      camPins = buildCamPins(wp, hexOf("--cyan", "#4cc3d9"));
      camViz.add(camPins);
    }
  }

  function sampleCustomPath(cam) {
    const curve = camPathCurve(cam.path);   // 与 rig 求值同一个建曲线入口
    const pts = [];
    for (let i = 0; i <= 48; i++) pts.push(curve.getPointAt(i / 48).clone());
    return pts;
  }

  /** 程序轨道的真实世界采样（49 点）：逐帧 sceneAt 求值——主体走位/落座吸附
   *  全都算进去，画的就是最终会发生的那条运动。采样完把场景摆回播放头。
   *  **末样本必须夹在 t_out 之内**：`cutAt(t_out)` 属于**下一个镜头块**（区间是
   *  左闭右开），不夹的话第 49 个点是下一镜机位的起始位姿——预设轨迹表现为
   *  「相机走到 4 号针就切镜、5 号针永远到不了」（5 号针根本不是本镜的点），
   *  烘焙出的自定义轨道更是会在结尾真的飞向别的机位（实测被点名）。 */
  function sampleCamTraj(cut) {
    const pts = [];
    for (let i = 0; i <= 48; i++) {
      sceneAt(Math.min(cut.t_in + (i / 48) * cut.dur, cut.t_out - 1e-3));
      pts.push(shotCam.position.clone());
    }
    sceneAt(timeline.t);
    return pts;
  }

  /** 程序轨道 → 5 路点（弧长均分 0/25/50/75/100%）：「拖一下即烘焙」的形状来源。
   *  5 点的 CatmullRom 已足够还原环绕/升降的弧（previz 精度），再多针只会缠手。 */
  function bakeWaypoints(pts) {
    const curve = new THREE.CatmullRomCurve3(pts, false, "catmullrom", 0);
    return [0, 0.25, 0.5, 0.75, 1].map((u) => {
      const p = curve.getPointAt(u);
      return [+p.x.toFixed(3), +p.y.toFixed(3), +p.z.toFixed(3)];
    });
  }

  /** 需要时把当前镜头块的机位烘焙成自定义轨道（⇧+拖机身整体升降的入口）。
   *  静止机位轨迹长 <0.3m，烘出来是一堆重合点（曲线长度为零 getPointAt 直接
   *  NaN）——返回 false 并由调用方提示，绝不静默烘一条坏轨道。 */
  function ensureCamPath(cam, cut) {
    if (cam.path) return true;
    const pts = sampleCamTraj(cut);
    const len = pts.reduce((s, p, i) => (i ? s + p.distanceTo(pts[i - 1]) : 0), 0);
    if (len < 0.3) return false;
    cam.path = bakeWaypoints(pts);
    return true;
  }

  const SIZE_V2 = new THREE.Vector2();
  let lastPlaying = false;   // gizmo 的播放态追踪（frame 轮询，免得挂满 play/pause 调用点）

  /* ---- 空闲降频：这个循环每帧要做「全场景求值 + 两次完整渲染」（主画面 +
     右下监视器），若无条件跑满 60fps——时间轴暂停、用户一动不动时也照跑——
     真机上是风扇长转，headless（WebGL 退化成 SwiftShader 软件渲染）可把
     chrome-headless-shell 顶到 900% CPU。

     画面**静止时没有任何东西需要重画**，所以：后台标签页一帧不画；前台但没在
     播放也没人操作时降到 ~12fps（仍然「活着」，守住「镜间画面不能死」
     的纪律）。任何交互后先满帧跑一段，保证阻尼/悬停/拖拽跟手。
     **与导出无关**：`exportFrames` 完全绕开本循环（自己 sceneAt + render），
     previz 的逐帧确定性一个字都不受影响。 */
  const IDLE_AFTER_MS = 420;      // 最后一次交互之后仍按满帧渲这么久
  const IDLE_FRAME_MS = 80;       // 静止态的渲染间隔（≈12fps）
  let lastDraw = 0;
  const markInput = () => { S.lastInput = performance.now(); };
  viewport.addEventListener("pointermove", markInput, { passive: true });
  viewport.addEventListener("pointerdown", markInput, { passive: true });
  viewport.addEventListener("wheel", markInput, { passive: true });
  window.addEventListener("keydown", markInput);
  orbit.addEventListener("change", markInput);   // 阻尼滑行也算「在动」

  function frame(ts) {
    if (!alive) return;
    raf = requestAnimationFrame(frame);
    // 让位的是**逐帧导出**（它独占渲染器、改 drawingBuffer 尺寸），不是整批渲染——
    // 拿 `S.rendering`（整批标志）当判据的话，镜与镜之间那段编码等待里循环也不跑，
    // 而 `setSize` 恰好把画布清成透明黑：表现就是「渲完一镜卡住一两秒 + 半黑屏」
    if (S.exporting) return;
    if (document.hidden) return;               // 后台标签页：一帧都不画
    // 模态弹层的遮罩盖住整个工作台，本帧渲染不可见；而弹层里的缩略图仍在逐格
    // 渲染，两者同开只是挤占后者。收口态（.closing 淡出中）立刻恢复，
    // 弹层关闭不留一段静止画面。
    if (document.querySelector(".dlg:not(.closing)")) return;
    const now = ts || performance.now();
    const busyIdle = !timeline.playing && !S.placing && !S.drawingPathFor
      && !S.dragObj && !S.dragCam && !S.dragCamPin && !S.dragPin && !S.dragLook
      && (now - (S.lastInput || 0)) > IDLE_AFTER_MS;
    if (busyIdle && (now - lastDraw) < IDLE_FRAME_MS) return;
    lastDraw = now;
    orbit.update();
    sceneAt(timeline.t);
    // 播放 ⇄ 暂停切换 gizmo：播放即 **detach**（不是藏 visible——helper 有过
    // 跟不上走位角色、被甩在路线起点的实测案例，detach 一了百了），暂停时在
    // 对象**当前**位置重新挂回（attach 按现位重算，天然消除任何陈旧位姿）。
    if (timeline.playing !== lastPlaying) {
      lastPlaying = timeline.playing;
      if (lastPlaying) {
        gizmo.detach();
      } else if (S.selected && S.selected.kind !== "camera") {
        gizmo.attach(S.selected.object);
        // 角色贴地无 Y 手柄（`director-stage-ui.md` ④）
        gizmo.showY = S.selected.kind !== "actor";
      }
    }
    syncAids();
    const cutNow = timeline.cutAt();
    syncCamViz(cutNow);
    // 路点针悬停/拖拽反馈：放大 1.28×（手柄类控件的标准反馈——「摸得到」）
    if (camPins) {
      const hot = S.dragCamPin ? S.dragCamPin.index : (S.hoverCamPin?.index ?? -1);
      camPins.children.forEach((pin, i) => {
        const s = i === hot ? 1.28 : 1;
        if (pin.scale.x !== s) pin.scale.setScalar(s);
      });
    }
    syncCamTag(cutNow);
    if (S.viewMode === "camera" && (S.camLook.yaw || S.camLook.pitch)) {
      // 环视偏移只作用于屏显——sceneAt 每帧重摆 rig，导出与监视器永远是纯运镜
      shotCam.rotateY(S.camLook.yaw);
      shotCam.rotateX(S.camLook.pitch);
    }
    // **setViewport/setScissor 必须喂 CSS 像素**——three 内部会乘 pixelRatio。
    // 喂 `domElement.width`（drawingBuffer 像素，已 ×dpr）等于再乘一次：Retina 上
    // 视口翻倍、画布只露出整幅画面的左下四分之一——主体被推到右上角、缩放向右上
    // 漂移、点选射线全对不上「看得见的像素」。该缺陷仅在 dpr>1 的环境出现，
    // dpr=1 下完全复现不出来。
    const size = renderer.getSize(SIZE_V2);
    const w = size.x, hh = size.y;
    // 画中画监视器：先渲小图 → 拷进监视器 DOM 画布（圆角由 CSS 裁切）→
    // 主渲染随后整幅覆写画布，把角落这张临时方角小图擦掉——顺序就是机制
    const showPip = !!cutNow;
    if (showPip) {
      const ratio = renderer.getPixelRatio();
      // 渲染缓冲尺寸与 CSS 显示尺寸配对：横幅 = .dz-pip 的 width:26%，
      // 竖幅 = .dz-pip.tall 的 height:44%（宽度按画幅比反推）
      const pw = portrait
        ? Math.round(hh * 0.44 * canvas[0] / canvas[1])
        : Math.round(w * 0.26);
      const ph = Math.round(pw * canvas[1] / canvas[0]);
      const pipCam = S.viewMode === "director" ? shotCam : dirCam;
      const clean = pipCam === shotCam;   // 机位画面=洁净渲染（导出会得到的帧）
      let mainAspect = 0;
      if (clean) setExportMode(true);
      else {
        mainAspect = dirCam.aspect;
        dirCam.aspect = canvas[0] / canvas[1];   // 监视器框是章节比例，别把全局挤扁
        dirCam.updateProjectionMatrix();
      }
      renderer.setScissorTest(true);
      renderer.setViewport(0, 0, pw, ph);
      renderer.setScissor(0, 0, pw, ph);
      renderer.render(scene, pipCam);
      renderer.setScissorTest(false);
      if (clean) setExportMode(false);
      else { dirCam.aspect = mainAspect; dirCam.updateProjectionMatrix(); }
      const sw = Math.round(pw * ratio), sh = Math.round(ph * ratio);
      if (pipCv.width !== sw || pipCv.height !== sh) { pipCv.width = sw; pipCv.height = sh; }
      pipCv.getContext("2d").drawImage(renderer.domElement,
        0, renderer.domElement.height - sh, sw, sh, 0, 0, sw, sh);
    }
    renderer.setViewport(0, 0, w, hh);
    renderer.render(scene, activeCamera());
    // 「＋ 在 xx:xx.x 处加一段」的时间随播放头走（检查器不整段重绘，只改这一个数）
    const atNow = inspector.querySelector(".dz-at-now");
    if (atNow) {
      const tt = fmtT(timeline.t);
      if (atNow.textContent !== tt) atNow.textContent = tt;
    }
    // 播放头进到哪一镜，大纲机位组同步点亮那一行（青色=正在播，琥珀=手选）
    const nowShot = cutNow ? String(cutNow.shot) : null;
    if (nowShot !== lastNowShot) {
      lastNowShot = nowShot;
      outliner.querySelectorAll(".dz-node.now").forEach((n) => n.classList.remove("now"));
      if (nowShot != null) {
        const n = outliner.querySelector(`.dz-node[data-shot="${nowShot}"]`);
        if (n) {
          n.classList.add("now");
          if (timeline.playing) n.scrollIntoView({ block: "nearest" });
        }
      }
    }
    if (pip.hidden !== !showPip) pip.hidden = !showPip;
    const lbl = !cutNow ? "" : (S.viewMode === "director"
      ? `● 机位画面 · 镜 ${cutNow.shot}` : "◈ 导演全局");
    if (pipLabel.textContent !== lbl) pipLabel.textContent = lbl;
    // 三分线只在「机位画面」上有意义（导演全局不是构图对象）
    const thirdsOff = S.viewMode !== "director";
    if (pipThirds.hidden !== thirdsOff) pipThirds.hidden = thirdsOff;
    syncRefPanel(cutNow);
  }

  /** 机位名牌：跟随机身的 DOM 浮签（只写「镜号 · 运镜名」——轨道态看针和检查器，
   *  塞进浮签只会把牌子撑宽到盖住机身，实测被点名「把镜头都遮住了」）。
   *  **选中该机位时整个隐藏**：选中=编辑态，针/轨迹/检查器已经给全信息，浮签
   *  这时只剩遮挡值；机身出画或监视模式同样藏。文本按 key 缓存，逐帧只挪位置。 */
  let camTagKey = "";
  const TAG_V = new THREE.Vector3();
  function syncCamTag(cutNow) {
    const cam = cutNow ? cameraById(cutNow.camera) : null;
    const show = S.viewMode === "director" && !!cam && camViz.visible
      && !S.rendering && S.selected !== cam;
    if (!show) { if (!camTag.hidden) { camTag.hidden = true; } camTagKey = ""; return; }
    TAG_V.copy(camBody.position);
    TAG_V.y += 0.56;
    TAG_V.project(dirCam);
    if (TAG_V.z > 1 || Math.abs(TAG_V.x) > 1.08 || Math.abs(TAG_V.y) > 1.08) {
      if (!camTag.hidden) camTag.hidden = true;
      return;
    }
    const size = renderer.getSize(SIZE_V2);
    camTag.style.left = `${((TAG_V.x * 0.5 + 0.5) * size.x).toFixed(1)}px`;
    camTag.style.top = `${((1 - (TAG_V.y * 0.5 + 0.5)) * size.y).toFixed(1)}px`;
    const key = `${cutNow.shot}|${cam.preset}`;
    if (key !== camTagKey) {
      camTagKey = key;
      camTag.textContent = `镜 ${cutNow.shot} · ${presetLabel(cam.preset) || "机位"}`;
    }
    if (camTag.hidden) camTag.hidden = false;
  }

  /** 分镜参考面板：跟随播放头换图；没有分镜图时明说，别留一块空黑框。 */
  let refKey = "";
  function syncRefPanel(cutNow) {
    const open = S.refOpen && !!cutNow;
    if (refPanel.hidden !== !open) refPanel.hidden = !open;
    if (!open) return;
    const shot = ctx.shotOf(cutNow.shot) || {};
    const img = shot.image || "";
    const key = `${cutNow.shot}|${img}`;
    if (key === refKey) return;
    refKey = key;
    refLbl.textContent = `分镜参考 · 镜 ${cutNow.shot}`;
    refImg.hidden = !img;
    if (img) refImg.src = img;
    refPanel.classList.toggle("noimg", !img);
  }

  /* -------------------------------------------------------------- 交互上下文 */
  // 这四个查表器刻意用**函数声明**而非箭头 const：它们被渲染循环 `sceneAt` 与
  // 各处回调调用，写成 const 就要求「声明必须在第一次调用之前」——而渲染循环的
  // 启动时机是会变的，一挪就是一串 TDZ 报错（"Cannot access X before
  // initialization"），且只在运行时才炸。函数声明提升，从根上没有这个问题。
  function cameraById(id) { return S.cameras.find((c) => c.id === id) || null; }
  function presetOf(key) { return findPreset(catalog, key); }
  function actionLabel(k) { return (dir.actions || []).find((a) => a.key === k)?.label || k; }
  function presetLabel(k) { return presetOf(k)?.label || null; }

  function cutOfCamera(cam) {
    return timeline.cuts.find((c) => c.camera === cam.id) || null;
  }

  const ctx = {
    get actors() { return S.actors; },
    get props() { return S.props; },
    get cameras() { return S.cameras; },
    get cuts() { return timeline.cuts; },
    get selected() { return S.selected; },
    get placing() { return S.placing; },
    get drawingPathFor() { return S.drawingPathFor; },
    get viewMode() { return S.viewMode; },
    get fps() { return S.fps; },
    get v2v() { return S.v2v; },
    get rendering() { return S.rendering; },
    get renderPct() { return S.renderPct; },
    get duration() { return timeline.duration; },
    get playhead() { return timeline.t; },
    get folds() { return S.folds; },
    get allFolded() { return !Object.values(S.folds).some(Boolean); },
    toggleFold: (k) => { S.folds[k] = !S.folds[k]; paintOutliner(); },
    toggleFoldAll: () => {
      const to = Object.values(S.folds).some(Boolean) ? false : true;
      for (const k of Object.keys(S.folds)) S.folds[k] = to;
      paintOutliner();
    },
    // 已登记 previz 的镜数：既是「交给 Seedance」按钮的可用判据，也是给导演的进度感
    get registered() { return (ch.shots || []).filter((s) => s.previz).length; },
    // 全片预演：scanner 从磁盘 sidecar 推导（不进契约），刷新章节快照即更新
    get reel() { return ch.previz_reel || null; },
    get reeling() { return S.reeling; },
    timeline, catalog, dir, canvas,
    presetOf, presetLabel, actionLabel, cameraById,
    cameraName: (id) => cameraById(id)?.name || "—",
    // 分镜条要按镜号取图与台词：章节快照是唯一来源（scanner 已把路径转成 URL）
    shotOf: (id) => (ch.shots || []).find((s) => String(s.id) === String(id)) || null,
    cutOfCamera: (cam) => cutOfCamera(cam),
    cutDurOfCamera: (cam) => cutOfCamera(cam)?.dur ?? null,
    cutDurOf: (a) => timeline.duration,
    setCutDur: (shot, dur) => { timeline.setCutDur(shot, dur); markDirty(); paintAll(); },
    // 大纲点选：画布同步选中——机位跳到它的镜头块（实体/轨迹当场现身），
    // 角色/道具框到眼前（拿起来就能拖）
    pickFromList: (item) => {
      if (!item) return;
      if (item.kind === "camera") {
        const cut = cutOfCamera(item);
        if (cut) timeline.seek(cut.t_in + 0.001);
        select(item);
        sceneAt(timeline.t);   // 立刻按新播放头求值——下面要用机位的真实位姿取景
        // 把「机位 ↔ 主体」这对关系框进视口：点大纲看机位，得看得见它在哪、
        // 从哪一侧拍谁——机身在画面外的话「拖机身转方位」根本无从谈起
        const o = subjectOf(item).anchor().origin.clone().setY(1);
        const cp = shotCam.position.clone();
        const mid = o.clone().add(cp).multiplyScalar(0.5);
        const dir = dirCam.position.clone().sub(orbit.target);
        if (dir.lengthSq() < 1e-6) dir.set(1, 0.8, 1);
        const sph = new THREE.Spherical().setFromVector3(dir);
        sph.phi = THREE.MathUtils.clamp(sph.phi, Math.PI * 0.24, Math.PI * 0.42);
        sph.radius = THREE.MathUtils.clamp(
          Math.max(4, o.distanceTo(cp) * 1.6), orbit.minDistance, orbit.maxDistance);
        S.topSaved = null;
        topBtn.classList.remove("on");
        orbit.target.copy(mid);
        dirCam.position.copy(mid).add(new THREE.Vector3().setFromSpherical(sph));
        orbit.update();
        paintTimeline();
      } else {
        select(item);
        focusSubject(item);
      }
    },
    setCameraYaw: (c, v) => { c.yaw = v || 0; markDirty(); paintPanels(); },
    setCameraDist: (c, v) => { c.dist = v || 1; markDirty(); paintPanels(); },
    // 自定义轨道置空=回到 preset 程序轨道（检查器「↺ 恢复预设轨道」走这里）
    setCameraPath: (c, pts) => {
      c.path = Array.isArray(pts) && pts.length >= 2 ? pts : null;
      markDirty(); paintPanels();
    },
    setActorRot: (a, deg) => {
      a.setTransform(a.object.position.toArray(), deg);
      markDirty(); paintPanels();
    },
    select, remove, addCamera,
    setPlacing: (p) => {
      if (S.drawingPathFor) cancelPathDraw();
      S.placing = p;
      setCursor(); paintAll();
    },
    rename: (it, v) => { it.name = (v || "").trim() || it.name; markDirty(); paintAll(); },
    setActorModel: (a, model) => {
      const spec = { ...a.serialize(), model };
      remove(a);
      const na = addActor(model, spec.pos, spec);
      select(na);
    },
    // 动作轨改动必须 paintAll：时间轴底部那条动作轨也按 tracks 画，只刷检查器
    // 会让两处对同一份数据显示不同的动作，而动作轨本身就是换动作的入口之一
    setActorTrack: (a, i, patch) => {
      const tracks = a.tracks.map((t, k) => (k === i ? { ...t, ...patch } : t));
      a.setTracks(tracks);
      markDirty(); paintAll();
    },
    addActorTrack: (a) => {
      a.setTracks([...a.tracks, { t0: Math.min(timeline.duration, timeline.t), action: "idle" }]);
      markDirty(); paintAll();
    },
    removeActorTrack: (a, i) => {
      a.setTracks(a.tracks.filter((_, k) => k !== i));
      markDirty(); paintAll();
    },
    togglePathDraw: (a) => {
      if (S.drawingPathFor === a.id) {
        finishPathDraw();
        focusSubject(a);           // 画完把镜头带过去——路线可能拉出很远
      } else {
        // **路线起点 = 角色当前站位**，不是第一次点击的地方。
        // 否则角色会在 t=0 瞬移到路线起点：用户按自己的视角在地上点了几下，
        // 人就"嗖"地跑到十几米外，而镜头还看着原来的位置——表现出来就是
        // 「人偶永远在最右上角、还拖不动」（实测用户就是这么踩的）。
        S.drawingPathFor = a.id;
        S.pathSnapshot = a.pathPoints ? a.pathPoints.map((p) => [...p]) : null;
        S.pathBuf = [a.object.position.toArray().map((v) => +v.toFixed(3))];
        S.placing = null;
        refreshAllPathViz();
        setCursor();
      }
      paintAll();
    },
    clearPath: (a) => {
      // 清路线时把人留在他此刻站的地方（路线跟随期间 object.position 一直被改写），
      // 不然清完人会停在最后一帧的落点上，与"回到原位"的直觉相反
      const here = a.object.position.toArray();
      a.setPath(null);
      a.setTransform(here, a.baseRotY * 180 / Math.PI);
      refreshAllPathViz();
      markDirty(); paintAll();
    },
    setPropRot: (p, deg) => { p.object.rotation.y = deg * Math.PI / 180; markDirty(); },
    setFovScale: (c, v) => { c.fovScale = v; markDirty(); paintPanels(); },
    setCameraSubject: (c, id) => { c.subject = id || null; markDirty(); paintPanels(); },
    setCameraFrame: (c, fx, fy) => {
      c.frame = (fx || fy) ? [fx, fy] : null;
      markDirty(); paintPanels();
    },
    setPreset: (c, key) => {
      c.preset = key;
      // 换运镜=回到新 preset 的程序轨道。旧自定义轨道是旧运镜的形状——留着它，
      // 新选的运镜在位置上就完全不生效，表现为「换了运镜画面却一点没变」
      c.path = null;
      const p = presetOf(key);
      const cut = cutOfCamera(c);
      // preset 自带建议时长——换运镜时同步镜头块，让 3D 里的运动跑得完整
      if (cut && p) { timeline.setCutDur(cut.shot, p.duration); }
      markDirty(); paintAll();
    },
    setFps: (v) => { S.fps = v; timeline.fps = v; markDirty(); paintPanels(); },
    setV2V: async (on) => {
      S.v2v = on;
      try {
        await post("/api/previz/v2v", { project: pid, chapter: cid, on });
        toast(on ? "已开启参考视频 V2V（会多计输入视频秒）" : "已关闭参考视频 V2V");
      } catch (e) { toast(e.message, true); S.v2v = !on; }
      paintPanels();
    },
    selectCut: (c) => { timeline.seek(c.t_in + 0.001); select(cameraById(c.camera)); },
    // 查看某镜已渲出的 previz（走全站同一个影院播放器）
    viewPreviz: (shotId) => {
      const s = ctx.shotOf(shotId);
      if (!s || !s.previz) { toast("这一镜还没渲染 previz", true); return; }
      const cam = cameraById(timeline.cuts.find(
        (c) => String(c.shot) === String(shotId))?.camera);
      openCinema({
        video: s.previz,
        poster: s.image || null,
        title: `镜 ${shotId} · previz 预演`,
        chips: [presetLabel(cam?.preset) && h("span", { class: "chip" },
          presetLabel(cam?.preset))].filter(Boolean),
      });
    },
    // 把各镜 previz 拼成一条长片：逐镜点开看不出整场戏连起来的节奏，而节奏正是
    // 排戏要审的（上一镜的收势接不接得住下一镜的起势）。零 API 成本纯本地 ffmpeg。
    buildReel: async () => {
      if (S.reeling) return;
      S.reeling = true; paintTimeline();
      showBusy(busy, { title: "合成全片预演中", sub: "各镜 previz 按分镜顺序拼接" });
      const stop = () => { S.reeling = false; showBusy(busy, null); paintTimeline(); };
      try {
        const r = await post("/api/previz/reel", { project: pid, chapter: cid });
        showBusy(busy, { title: "合成全片预演中", sub: `${r.shots} 镜拼接与编码` });
        // 拼接同样是短任务（流拷贝近乎瞬时），别吃 pollJob 的长任务缺省轮询
        pollJob(r.job, {
          interval: 250,
          onDone: async () => {
            await refreshChapter();          // reel 进快照 → 「▶ 看全片」当场可用
            stop();
            toast("全片预演已合成——「▶ 看全片」播放 · 「⬇」下载");
          },
          onFail: (j) => { stop(); toast(`合成失败：${(j.tail || "").slice(-160)}`, true); },
        });
      } catch (e) {
        stop();
        toast(e.message, true);
      }
    },
    viewReel: () => {
      const r = ch.previz_reel;
      if (!r) { toast("还没合成全片预演", true); return; }
      const miss = (r.skipped || []).filter((x) => x.why === "no_previz");
      openCinema({
        video: r.video, poster: r.poster || null, size: r.size || null,
        title: `全片预演 · ${(r.shots || []).length} 镜`,
        chips: [
          h("span", { class: "chip" }, `${Number(r.duration || 0).toFixed(1)}s`),
          // 漏了哪几镜必须当面说——不说的话「看了一遍全片」会被当成全片都看过了
          miss.length ? h("span", { class: "chip amber" },
            `${miss.length} 镜未渲：${miss.map((x) => x.id).join("/")}`) : null,
        ].filter(Boolean),
        rows: [["镜序", (r.shots || []).map((x) => `镜${x}`).join(" · ")]],
      });
    },
    // 播放/暂停只换一个字形，同样走就地同步——重建会把刚被点中的按钮换掉
    togglePlay: () => { timeline.toggle(); syncPlayhead(); },
    toggleView: () => {
      S.viewMode = S.viewMode === "camera" ? "director" : "camera";
      S.camLook.yaw = S.camLook.pitch = 0;         // 换视角环视归零
      orbit.enabled = S.viewMode === "director";   // 监视模式不许动导演相机
      S.hover = null;
      setCursor();
      paintTimeline();
      updateHud();
    },
    scrubStart: (ev) => {
      const bar = ev.currentTarget;
      const move = (e) => {
        const r = bar.getBoundingClientRect();
        timeline.pause();
        timeline.seek(((e.clientX - r.left) / r.width) * timeline.duration);
      };
      move(ev);
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    saveScene: () => saveScene(true),
    renderPreviz: () => openRenderPicker(),
    get renderJob() { return S.renderJob; },
    // 「和分镜图对位」需要**视觉理解**——引擎内无 LLM 是铁律，关键词映射到不了
    // 「看图摆位」。智能归指挥层：把一份带定位与 schema 的标准指令（可另附本场诉求）
    // 复制给 Claude Code，它逐镜 Read 分镜图后直接写章节文档的 previz 场景快照，
    // 控制台刷新即恢复（与「⧉ 改镜指令」同一交互范式、同一条 restore 路径）。
    copyAiPlan: () => openDirectiveDialog({
      title: "AI 编排指令", code: "PREVIZ · STAGING",
      meta: `项目 ${pid} / 章节 ${cid} · 正镜 ${activeShots.length}`,
      directive: buildAiPlanInstruction(),
      ask: "在此写本场编排要求",
      hint: "例：这场戏在窄巷里，两人始终保持三步距离；第 4 镜给一个越肩正反打",
      note: "留空即按上面的 schema 与纪律编排；写下的要求会填进末行「需求：」"
        + "——场景氛围、人物距离、镜头调度这类只有你知道的意图写在这里。",
      done: "已复制 AI 编排指令——粘贴给 Claude Code，它会照分镜图逐镜编排",
    }),
    toSeedance: () => openSeedancePicker(),
  };

  /** 交给 Seedance · 选镜弹层：**不是每一镜都值得花 previz 的钱**——只有调度复杂、
   *  非精确控制不可的镜才需要带参考视频生成，简单镜留给常规流程。缺省勾选=已预演
   *  的镜；未勾选的镜本次直接跳过（后端 gen-video --only）。 */
  function openSeedancePicker() {
    const sel = new Map(activeShots.map((s) => [String(s.id), !!s.previz]));
    let settled = false;
    const close = () => { if (settled) return; settled = true; overlay.remove(); };
    const rows = activeShots.map((s) => {
      const c = uiCheck();
      c.checked = !!s.previz;
      c.addEventListener("change", () => sel.set(String(s.id), c.checked));
      return h("label", { class: "dz-shotrow" },
        c,
        h("b", null, `镜 ${s.id}`),
        h("em", { class: "mono" }, `${snapDuration(s.dur || 5)}s`),
        s.previz ? h("span", { class: "dz-chip blue" }, "◈ 已预演") : null,
        h("span", { class: "dz-shotrow-t" },
          (s.narration || s.caption || s.action || "").slice(0, 26)));
    });
    const go = async () => {
      const only = activeShots.map((s) => String(s.id)).filter((id) => sel.get(id));
      if (!only.length) { toast("一个镜都没勾", true); return; }
      close();
      try {
        const r = await post("/api/previz/seedance", { project: pid, chapter: cid, only });
        toast(`已发起 ${r.shots} 镜生成——在制作台看进度`);
        pollJob(r.job, {
          onDone: () => toast("Seedance 生成完成——回制作台看片段"),
          onFail: (j) => toast(`生成失败：${(j.tail || "").slice(-200)}`, true),
        });
      } catch (e) { toast(e.message, true); }
    };
    const overlay = h("div", { class: "dlg" },
      h("div", { class: "dlg-backdrop", onclick: close }),
      h("div", { class: "dlg-card dz-sdcard" },
        h("div", { class: "dz-pkhead" },
          h("span", { class: "k" }, "交给 SEEDANCE · 选镜"),
          h("div", { class: "dz-spacer" }),
          h("button", { class: "dz-icobtn", type: "button", onclick: close }, "✕")),
        h("p", { class: "dz-note" },
          "勾选本次要生成的镜（native 模式"
          + (S.v2v ? "；已预演的镜自动带参考视频迁移运镜与走位" : "")
          + "）。复杂调度镜才值得带 previz 精确控制，简单镜可留给常规流程。"
          + "花钱操作——预算断闸 / 单笔超阈 / 4K 确认等既有成本闸照常生效。"),
        h("div", { class: "dz-sdlist" }, ...rows),
        h("div", { class: "dz-btns", style: "justify-content:flex-end;margin-top:10px" },
          h("button", { class: "dz-btn", type: "button", onclick: close }, "取消"),
          h("button", { class: "dz-cta sm", type: "button", onclick: go }, "▸ 开始生成"))));
    document.body.append(overlay);
  }

  /** AI 编排指令：定位 + 场景快照 schema + 全部合法 key——让指挥层看着分镜图写编排。 */
  function buildAiPlanInstruction() {
    const shots = activeShots.map((s) => `镜${s.id}`).join("、");
    return [
      `请为 3D 导演台智能编排一场戏（照分镜图对位）。`,
      ``,
      `定位：项目 ${pid} / 章节 ${cid}（章节文档 project/${pid}/chapters/${cid}.json）。`,
      `任务：逐镜 Read 分镜图（shots[].image）与 narration/action/camera/framing，`,
      `把章节文档顶层 previz 场景快照重写为与分镜构图对位的编排（角色站位/朝向/`,
      `走位/动作轨/机位构图），完成后告诉我刷新导演台查看。`,
      ``,
      `previz 场景快照 schema（与控制台 serialize 同构）：`,
      `{ fps: 24,`,
      `  actors: [{id, name, model, pos:[x,0,z], rot:度(0=面向+Z即镜头侧),`,
      `            path:[[x,0,z]…]|null（起点必须=pos）, speed:null, tracks:[{t0:秒, action}]}],`,
      `  props:  [{id, name, kind, pos:[x,0,z], rot:度}],`,
      `  cameras:[{id, name, preset, fovScale:0.75~1.35, subject:actor_id|null,`,
      `            frame:[fx,fy]|null（构图偏移：主体落点，±0.167=三分线，0=居中）,`,
      `            yaw:度（方位偏转：整段运镜绕主体转到哪一侧拍，0=preset 原方位）,`,
      `            dist:0.3~3（径向距离缩放：整段运镜相对主体推远/拉近，1=原距离）,`,
      `            path:[[x,y,z]…]|null（自定义机位轨道·世界坐标·≥2 点：设了它位置就`,
      `            沿这条轨道弧长匀速走完本镜（结束帧必达终点），盯主体/焦距照旧而`,
      `            yaw/dist 失效；不设=preset 程序轨道。仅特殊调度才用，别处处自定义）}],`,
      `  cuts:   [{shot, camera, t_in, t_out}]（正镜=${shots}；时长 4~15 整秒，首尾相接；`,
      `           **机位与镜头块 1:1**——每镜恰好一个机位、不复用不游离）}`,
      ``,
      `合法 key（越界会被回落）：`,
      `· model: ${(dir.models || []).map((m) => m.key).join(" / ")}`,
      `· action: ${(dir.actions || []).map((a) => a.key).join(" / ")}`,
      `· prop kind: ${(dir.props || []).map((p) => p.key).join(" / ")}`,
      `· camera preset: ${catalog.map((c) => `${c.key}(${c.label}${
        c.tier === "advanced" ? "▲" : c.tier === "high-risk" ? "■" : ""})`).join(" / ")}`,
      ``,
      `运镜手法要逐镜设计（这场戏的特色所在，别一路 static）：按叙事强度选 preset——`,
      `建立镜用升降/广角、对话正反打用 push_in 或 static+frame 三分构图（视线侧留白）、`,
      `位移镜用跟拍/横移、情绪峰值那一镜才动用 ▲/■ 档（▲ 一集 ≤2 个、■ 至多 1 个且`,
      `建议 dur≥5s）；相邻镜避免同 preset；机位 name 直接写手法名（如「镜3 缓慢环绕」）；`,
      `fovScale 按景别（特写 0.75 / 中景 1.0 / 远景 1.35）。`,
      ``,
      `纪律：米制（1 格=1m）；角色 y 恒 0；不删镜不改 shot id；只写 previz 块，别动其他字段。`,
    ].join("\n");
  }

  /* ---------------------------------------------------------------- 绘制 */
  let lastNowShot = "__init__";   // 大纲「正在播」行的追踪（重绘后由 frame 重挂）
  function paintOutliner() { renderOutliner(outliner, ctx); lastNowShot = "__init__"; }
  function paintPanels() { renderInspector(inspector, ctx); paintOutliner(); }
  function paintTimeline() { renderTimeline(tlEl, ctx); syncViewSeg(); }
  function syncPlayhead() { syncTimelineHead(tlEl, ctx); }
  function paintAll() { paintPanels(); paintTimeline(); updateHud(); }

  /** 视口状态条：按当前模式给「下一步怎么操作」——模式一变文案就变。 */
  function updateHud() {
    let txt;
    if (S.drawingPathFor) {
      const a = S.actors.find((x) => x.id === S.drawingPathFor);
      const len = a && a.pathLength ? ` · ${a.pathLength.toFixed(1)}m` : "";
      txt = `画走位（${S.pathBuf.length} 点${len}）：点地面加路点 · 拖数字针微调 · `
        + "右键/退格 撤销 · 双击或回车 完成 · Esc 取消";
    } else if (S.placing) {
      const cat = S.placing.kind === "actor" ? (dir.models || []) : (dir.props || []);
      const label = (cat.find((x) => x.key === S.placing.key) || {}).label || S.placing.key;
      txt = `落位「${label}」：在地面点一下放置 · Esc 取消`;
    } else {
      txt = "";   // 常态不挂教程横幅——操作速查收进 ⌨；监视模式提示在视角胶囊下方
    }
    if (hudTip.textContent !== txt) hudTip.textContent = txt;
  }
  paintAll();
  // 调试句柄：本地单人工具，暴露内部状态利大于弊——3D 出问题时"看不见状态"会让
  // 排查退化成猜（gizmo 到底 attach 上没有、射线打到谁、actor 位置是多少）。
  // 只读引用，不提供任何写操作。
  window.__director = { scene, S, timeline, gizmo, dirCam, shotCam, orbit, renderer };
  sceneAt(timeline.t);     // 先按 t=0 摆好场——带走位的角色此刻才站到路线起点
  if (S.actors.length) select(S.actors[0]);   // 进门即选中主角：圈亮着、gizmo 就位、检查器有内容
  focusSubject();          // 再把镜头对在主体上（居中框满）；顺序反了会聚焦到保存时的旧站位
  // 渲染循环**最后**才起：它每帧调 sceneAt → cameraById/rigFor，提前启动就要求
  // 整条依赖链都已初始化。放在这里，"什么时候能开始画"这个问题只有一个答案。
  frame();

  /* --------------------------------------------------------------- 持久化 */
  function markDirty() { S.dirty = true; }

  function serialize() {
    return {
      fps: S.fps,
      actors: S.actors.map((a) => a.serialize()),
      props: S.props.map((p) => ({
        id: p.id, name: p.name, kind: p.prop,
        pos: p.object.position.toArray().map((v) => +v.toFixed(4)),
        rot: p.rotY,
      })),
      // 只序列化被镜头块引用的机位——机位与镜头块 1:1 从属（#7），
      // 游离机位（旧数据的「机位9/10/11」）在保存时自动清理
      cameras: S.cameras
        .filter((c) => timeline.cuts.some((cu) => cu.camera === c.id))
        .map((c) => ({
          id: c.id, name: c.name, preset: c.preset,
          fovScale: c.fovScale, subject: c.subject, frame: c.frame,
          yaw: c.yaw || 0, dist: c.dist || 1,
          path: c.path || null,
        })),
      cuts: timeline.serialize(),
    };
  }

  /** 渲染/登记完成后重取章节快照——`previz`/`image`/已登记数都在里面，
   *  不刷新的话「▶ 查看」与「交给 Seedance」的可用态要等整页重进才对。
   *
   *  **新加的章节级字段必须在这里逐一回填**：这是个白名单式浅拷贝（`ch` 是进门时
   *  取的那份快照，各处 getter 都读它），漏一个的表现是「后台任务明明成功了，
   *  界面却纹丝不动」——实测 `previz_reel` 就这么漏过一次：全片合成完成、文件也
   *  在盘上，按钮却始终停在「合成全片」。 */
  async function refreshChapter() {
    try {
      const fresh = await api(`/api/chapter?project=${encodeURIComponent(pid)}`
        + `&id=${encodeURIComponent(cid)}`);
      ch.shots = fresh.shots;
      ch.previz = fresh.previz;
      ch.previz_v2v = fresh.previz_v2v;
      ch.previz_reel = fresh.previz_reel;
      paintAll();
    } catch (e) {
      console.warn("[director] 章节快照刷新失败：", e);
    }
  }

  async function saveScene(loud = false) {
    try {
      const r = await post("/api/previz/save",
        { project: pid, chapter: cid, scene: serialize() });
      S.dirty = false;
      if (loud) toast(`编排已保存（${r.cuts} 个镜头块）`);
      return r;
    } catch (e) { toast(`保存失败：${e.message}`, true); throw e; }
  }

  /* ------------------------------------------------------------ 渲染 previz */
  /** 渲染选镜弹层：默认勾**还没渲的镜**——排一场戏是整场排的，逐镜点开渲一遍
   *  再手工记住哪些渲过，本就该是机器干的活。已渲的镜留着不勾（重渲要显式选）。*/
  function openRenderPicker() {
    if (!timeline.cuts.length) return toast("没有镜头块可渲染", true);
    if (S.rendering) return toast("正在渲染中，Esc 可中止", true);
    const done = (id) => !!(ctx.shotOf(id) || {}).previz;
    const sel = new Map(timeline.cuts.map((c) => [String(c.shot), !done(c.shot)]));
    const boxes = new Map();
    let autoReel = true;

    const stat = h("span", { class: "dz-hint" });
    const sync = () => {
      const picked = timeline.cuts.filter((c) => sel.get(String(c.shot)));
      const secs = picked.reduce((a, c) => a + c.dur, 0);
      stat.textContent = picked.length
        ? `${picked.length} 镜 · ${secs.toFixed(0)}s · ${Math.round(secs * S.fps)} 帧`
        : "未选镜";
    };
    const setAll = (fn) => {
      timeline.cuts.forEach((c) => {
        const v = fn(c);
        sel.set(String(c.shot), v);
        boxes.get(String(c.shot)).checked = v;
      });
      sync();
    };

    const rows = timeline.cuts.map((c) => {
      const shot = ctx.shotOf(c.shot) || {};
      const box = uiCheck();
      box.checked = sel.get(String(c.shot));
      box.addEventListener("change", () => { sel.set(String(c.shot), box.checked); sync(); });
      boxes.set(String(c.shot), box);
      const preset = presetOf(cameraById(c.camera)?.preset);
      return h("label", { class: "dz-shotrow" },
        box,
        h("b", null, `镜 ${c.shot}`),
        h("em", { class: "mono" }, `${c.dur}s`),
        done(c.shot) ? h("span", { class: "dz-chip blue" }, "◈ 已渲") : null,
        h("span", { class: "dz-shotrow-t" },
          (preset ? preset.label + " · " : "") + (shot.narration || shot.caption || "").slice(0, 22)));
    });
    sync();

    const reelBox = uiCheck();
    reelBox.checked = autoReel;
    reelBox.addEventListener("change", () => { autoReel = reelBox.checked; });

    openShell({
      card: "dz-sdcard",
      build: (close) => [
        h("div", { class: "dz-pkhead" },
          h("span", { class: "k" }, "渲染 PREVIZ · 选镜"),
          h("div", { class: "dz-spacer" }),
          stat,
          h("button", { class: "dz-icobtn", type: "button", onclick: close }, "✕")),
        h("p", { class: "dz-note" },
          `渲染尺寸 ${canvas[0]}×${canvas[1]} @ ${S.fps}fps。逐镜确定性导出 → 引擎编码 → `
          + "自动登记为该镜的首帧 / 末帧 / 参考片；已有分镜图的镜默认不覆盖。"
          + "渲染期间别切走页面，Esc 可随时中止（已完成的镜保留）。"),
        h("div", { class: "dz-btns" },
          h("button", { class: "dz-btn", type: "button", onclick: () => setAll(() => true) }, "全选"),
          h("button", { class: "dz-btn", type: "button",
            onclick: () => setAll((c) => !done(c.shot)) }, "只选未渲"),
          h("button", { class: "dz-btn", type: "button", onclick: () => setAll(() => false) }, "清空")),
        h("div", { class: "dz-sdlist" }, ...rows),
        h("label", { class: "dz-shotrow", style: "margin-top:8px" },
          reelBox, h("b", null, "完成后合成全片预演"),
          h("span", { class: "dz-shotrow-t" }, "把各镜串成一条长片，直接看整场戏的节奏")),
        h("div", { class: "dz-btns", style: "justify-content:flex-end;margin-top:10px" },
          h("button", { class: "dz-btn", type: "button", onclick: close }, "取消"),
          h("button", { class: "dz-cta sm", type: "button",
            onclick: () => {
              const picked = timeline.cuts.filter((c) => sel.get(String(c.shot)));
              if (!picked.length) { toast("一个镜都没勾", true); return; }
              close();
              renderBatch(picked, autoReel);
            } }, "⏺ 开始渲染")),
      ],
    });
  }

  /** 逐镜渲染并登记。**必须串行**：每镜的登记都是一个 `previz build` 子进程，
   *  它 load → 改 → save 章节文档；并发跑两个就是经典的丢更新（后写的那个以自己
   *  load 到的旧副本为准，前一镜的 previz/image 登记凭空消失）。
   *  中止（Esc）只停在镜与镜之间与当前镜的帧循环里——已登记的镜一律保留。 */
  async function renderBatch(cuts, autoReel) {
    timeline.pause();
    await saveScene();
    S.rendering = true; S.abort = false; S.renderPct = 0;
    S.renderJob = { i: 0, n: cuts.length, shot: cuts[0].shot };
    paintTimeline();
    const okShots = [];
    try {
      for (let k = 0; k < cuts.length; k++) {
        const cut = cuts[k];
        S.renderJob = { i: k + 1, n: cuts.length, shot: cut.shot };
        S.renderPct = 0; paintTimeline();
        // 蒙版**撤在下一镜开渲的这一刻**，而不是上一镜任务完成时——中间那段
        // （refreshChapter / 起下一镜）撤了就又露出一次静止的编辑视图
        showBusy(busy, null);
        let r;
        // 洁净模式与「渲染循环让位」都**只包住这一镜的帧导出**，不覆盖整批：
        // ① 让位是必须的（导出要独占渲染器改 drawingBuffer 尺寸），但一旦把它撑到
        //    整批，镜与镜之间那段编码等待里画面就是死的——而 `setSize` 恰好会把
        //    画布清成透明黑，用户看到的正是「卡住一两秒然后半黑屏」；
        // ② 洁净模式也不能撑到整批：渲染循环画 PiP 时每帧都成对 setExportMode
        //    (true→false)，循环一恢复就会把整批的洁净态给关掉（`director-stage-ui.md` ⑩）。
        S.exporting = true;
        setExportMode(true);
        try {
          r = await exportFrames({
            renderer, scene, camera: shotCam,
            sceneAt: (tl) => sceneAt(cut.t_in + tl),
            dur: cut.dur, fps: S.fps, width: canvas[0], height: canvas[1],
            project: pid, chapter: cid, shot: cut.shot,
            onProgress: (i, n) => { S.renderPct = Math.round((i / n) * 100); paintTimeline(); },
            shouldAbort: () => S.abort,
          });
        } finally {
          // **蒙版必须先于退出洁净模式盖上**：否则 gizmo/站位圈/路线会闪回一帧，
          // 而下一镜又立刻切回洁净——那一下闪烁正是「割裂感」的来源
          if (!S.abort) {
            showBusy(busy, {
              title: `镜 ${cut.shot} 编码并登记中`,
              sub: cuts.length > 1
                ? `第 ${k + 1} / ${cuts.length} 镜 · 完成后自动继续下一镜`
                : "帧序列 → mp4 → 登记为首帧 / 末帧 / 参考片",
            });
          }
          setExportMode(false);
          S.exporting = false;
          resize();          // 恢复 drawingBuffer 尺寸后画布是空的，让循环立刻重画
        }
        if (r.aborted) break;
        const job = await buildPreviz({
          project: pid, chapter: cid, shot: cut.shot, fps: S.fps,
          camera: cameraById(cut.camera)?.preset || null, useFirstFrame: null,
        });
        // 等这一镜登记落盘再渲下一镜（串行的那一半在这里，不 await 就并发写文档了）
        const jr = await waitJob(job);
        if (!jr.ok) {
          toast(`镜 ${cut.shot} 登记失败：${(jr.tail || "").slice(-140)}`, true);
          break;
        }
        okShots.push(cut.shot);
        toast(`镜 ${cut.shot} 已登记（${k + 1}/${cuts.length}）`);
      }
    } catch (e) {
      toast(`渲染失败：${e.message}`, true);
    } finally {
      setExportMode(false);
      S.exporting = false;
      S.rendering = false; S.renderPct = 0; S.renderJob = null;
      resize();
      paintTimeline();
      // 章节快照**整批只重取一次**：`refreshChapter` 会 paintAll（三栏 DOM 全重建），
      // 逐镜刷的话每渲完一镜就卡一下，而它只换来 ◈ 角标早几秒出现
      await refreshChapter();
      // 收尾还要接着合全片时**不撤蒙版**（buildReel 随即改写它的文案），
      // 撤了就是「蒙版闪一下没了又立刻回来」
      if (!(autoReel && okShots.length && !S.abort)) showBusy(busy, null);
    }
    if (S.abort) toast(`已中止——已完成 ${okShots.length} 镜，保留不撤`);
    else if (okShots.length) {
      toast(okShots.length === 1 ? `镜 ${okShots[0]} 的 previz 已登记`
        : `${okShots.length} 镜 previz 已全部登记`);
      if (autoReel) await ctx.buildReel();
    }
  }

  /** `pollJob` 的 Promise 包装——批量渲染要「等这一镜落盘再渲下一镜」，
   *  回调式轮询在 for 循环里串不起来。
   *
   *  **必须调快轮询**：`pollJob` 缺省 1600ms 且**第一次检查也要等满这一轮**，
   *  那是给 Seedance 那种分钟级任务定的；而 previz 登记实测总共约 0.7s
   *  （子进程启动 0.1s + 编码 0.36s + 首末帧抽取），照缺省轮询就是每渲完一镜
   *  白等 1~1.6 秒——「渲染成功后卡住一两秒」的整个体感都在这里。 */
  function waitJob(id) {
    return new Promise((res) => {
      pollJob(id, {
        interval: 250,
        onDone: () => res({ ok: true }),
        onFail: (j) => res({ ok: false, tail: j.tail || "" }),
      });
    });
  }

  /* --------------------------------------------------------------- 全屏 */
  /** 全屏整块工作台（含三栏与时间轴），而不是只全屏 canvas——排戏要的是整套工具。 */
  function toggleFull() {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else root.requestFullscreen?.().catch(() => toast("浏览器拒绝了全屏请求", true));
  }
  const onFsChange = () => {
    const on = document.fullscreenElement === root;
    root.classList.toggle("full", on);
    fullBtn.textContent = on ? "⤡" : "⤢";
    fullBtn.dataset.tip = on ? "退出全屏（F11 / Esc）" : "全屏工作台（F11）";
    requestAnimationFrame(resize);
  };
  document.addEventListener("fullscreenchange", onFsChange);

  /* ------------------------------------------------------------- 快捷键 */
  // 方向键给了镜头平移（上帝视角的键盘走图），逐帧步进让位给 , / .（NLE 通例）
  function onKey(e) {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    if (e.key === " ") { e.preventDefault(); ctx.togglePlay(); }
    else if (e.key.startsWith("Arrow")) { e.preventDefault(); panByKey(e); }
    else if (e.key === "." || e.key === ">") { timeline.pause(); timeline.stepFrame(e.shiftKey ? 10 : 1); }
    else if (e.key === "," || e.key === "<") { timeline.pause(); timeline.stepFrame(e.shiftKey ? -10 : -1); }
    else if (e.key === "Enter" && S.drawingPathFor) { finishPathDraw(); }
    else if ((e.key === "Backspace" || e.key === "Delete") && S.drawingPathFor) {
      e.preventDefault();
      undoPathPoint();
    }
    else if (e.key === "Escape") {
      // 中止后当前镜的编码任务仍会跑完（子进程已发出），但蒙版立刻撤——
      // 留着它会让「按了 Esc 却还是动不了」，而实际已经不再往下渲了
      if (S.rendering) { S.abort = true; showBusy(busy, null); }
      else if (S.drawingPathFor) cancelPathDraw();
      else if (S.placing) { S.placing = null; ghost.visible = false; setCursor(); paintAll(); }
      else { select(null); paintAll(); }
    } else if (e.key === "g") { ctx.toggleView(); }
    else if (e.key === "t") { toggleTop(); }
    else if (e.key === "[") { S.panels.left = !S.panels.left; syncPanes(); }
    else if (e.key === "]") { S.panels.right = !S.panels.right; syncPanes(); }
    else if (e.key === "f") { focusSubject(); }
    else if (e.key === "F11") { e.preventDefault(); toggleFull(); }
    else if (e.key === "r" && S.selected?.kind !== "camera") {
      gizmo.setMode(gizmo.mode === "translate" ? "rotate" : "translate");
    }
  }
  window.addEventListener("keydown", onKey);

  /** 方向键平移：沿地面前后左右滑（步长随观察距离缩放，Shift ×3）。
   *  只动导演视角——机位视角由运镜 preset 求值，键盘不该能拽它。 */
  function panByKey(e) {
    if (S.viewMode !== "director") return;
    const step = THREE.MathUtils.clamp(
      dirCam.position.distanceTo(orbit.target) * 0.07, 0.2, 2.4) * (e.shiftKey ? 3 : 1);
    const fwd = new THREE.Vector3();
    dirCam.getWorldDirection(fwd);
    fwd.y = 0;
    if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1);   // 顶视图正俯时的退化兜底
    fwd.normalize();
    const right = new THREE.Vector3().crossVectors(fwd, dirCam.up).normalize();
    const mv = { ArrowUp: fwd, ArrowDown: fwd.clone().negate(),
                 ArrowLeft: right.clone().negate(), ArrowRight: right }[e.key];
    if (!mv) return;
    const d = mv.multiplyScalar(step);
    orbit.target.add(d);
    dirCam.position.add(d);
    orbit.update();
  }

  const beforeUnload = (e) => {
    if (!S.dirty) return;
    e.preventDefault();
    e.returnValue = "";
  };
  window.addEventListener("beforeunload", beforeUnload);

  /* --------------------------------------------------------------- 卸载 */
  return function dispose() {
    alive = false;
    cancelAnimationFrame(raf);
    ro.disconnect();
    window.removeEventListener("keydown", onKey);
    // 空闲降频的 window 级监听同样要摘：viewport 上那几个随 DOM 一起没了，
    // 但挂在 window 上的会跨路由留存，来回进出导演台就叠成一串
    window.removeEventListener("keydown", markInput);
    window.removeEventListener("pointermove", onWinMove);
    window.removeEventListener("pointerup", onWinUp);
    window.removeEventListener("beforeunload", beforeUnload);
    document.removeEventListener("fullscreenchange", onFsChange);
    timeline.pause();
    S.actors.forEach((a) => a.dispose());
    gizmo.detach();
    gizmo.dispose?.();
    orbit.dispose();
    scene.traverse((o) => { o.geometry?.dispose?.(); });
    renderer.dispose();
    renderer.forceContextLoss?.();
    // 缩略图渲染器是本模块之外的第二个 WebGL 上下文，同样跨路由留存——
    // 不在这里释放，反复进出导演台就会一路叠加直到浏览器丢弃最早的上下文
    disposePreview();
  };
}

/**
 * 舞台地面网格纹理：一块 5m 见方的瓦片（细格 1m + 粗格 5m），平铺 32×32 = 160m。
 * 尺度与 previz 的坐标约定同源（1 单位 = 1 米），导演可以直接数格子读距离——
 * 「他要走 4 米」在 3D 里是看得见的四格，不是一个手感。
 */
function makeGridTexture(renderer) {
  const TILE = 256;               // 5m → 每米 51.2px，1px 线在 4K 视口下仍锐利
  const c = document.createElement("canvas");
  c.width = c.height = TILE;
  const g = c.getContext("2d");
  // 深板岩地 + 收敛的线（专业 DCC 的地面都压得很暗）：地面越沉，灰模与琥珀
  // 辅助物越浮出来——偏亮的地面会把主体的对比度吃掉一半
  g.fillStyle = "#2e3440";
  g.fillRect(0, 0, TILE, TILE);
  g.strokeStyle = "rgba(176,188,208,.17)";   // 细格 1m
  g.lineWidth = 1;
  for (let i = 1; i < 5; i++) {
    const p = Math.round((i * TILE) / 5) + 0.5;
    g.beginPath(); g.moveTo(p, 0); g.lineTo(p, TILE);
    g.moveTo(0, p); g.lineTo(TILE, p); g.stroke();
  }
  g.strokeStyle = "rgba(198,208,226,.30)";   // 粗格 5m（瓦片边界）
  g.lineWidth = 2;
  g.beginPath(); g.moveTo(1, 0); g.lineTo(1, TILE);
  g.moveTo(0, 1); g.lineTo(TILE, 1); g.stroke();

  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(32, 32);                    // 32 × 5m = 160m，与地面 PlaneGeometry 同尺
  tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
  tex.colorSpace = THREE.SRGBColorSpace;     // 画布是 sRGB，不声明会整体偏暗
  return tex;
}

/**
 * 天幕渐变：顶部微亮冷灰蓝 → 底部页面底色。`scene.background` 挂图按视口拉伸
 * 铺满、不随相机转动——正合适：它只负责「远处还有空间」的纵深暗示。
 * 中段色 #131720 与雾色一致，地平线处天幕与雾无缝衔接。
 */
function makeBackdrop(bgHex) {
  const c = document.createElement("canvas");
  c.width = 8;
  c.height = 512;
  const g = c.getContext("2d");
  // **天顶最暗、地平线一圈辉光**（Unreal/Blender 网格世界的通行画法）——
  // 反过来「顶亮下暗」在机位监视器的小框里就是一条突兀的灰黑带，会被看成
  // 「画面上有阴影」：平视的机位相机会把大片空天装进画面，空天必须读作
  // 「有纵深的环境」而不是「一块脏色」。辉光落在 58%（平视时地平线的位置）。
  const grad = g.createLinearGradient(0, 0, 0, 512);
  grad.addColorStop(0, "#0c0f15");
  grad.addColorStop(0.46, "#10141c");
  grad.addColorStop(0.58, "#1d232e");
  grad.addColorStop(0.70, "#12151d");
  grad.addColorStop(1, `#${new THREE.Color(bgHex).getHexString()}`);
  g.fillStyle = grad;
  g.fillRect(0, 0, 8, 512);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/** 舞台光池：中心暖白向外衰减到全透明的径向渐变（叠加在地面上）。 */
function makePoolTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 256;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(128, 128, 8, 128, 128, 128);
  grad.addColorStop(0, "rgba(255, 240, 214, .9)");
  grad.addColorStop(0.55, "rgba(255, 240, 214, .28)");
  grad.addColorStop(1, "rgba(255, 240, 214, 0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 256, 256);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/* -------------------------------------------------------------------- 头部 */
/** 单行紧凑头（约 44px）：左标题右状态与操作，垂直居中——每省一行都还给视口。 */
function buildHeader(pid, cid, ch) {
  const n = (ch.shots || []).filter((s) => s.kind !== "transition" && !s.omitted).length;
  return h("div", { class: "dz-header" },
    h("span", { class: "k" }, "3D DIRECTOR"),
    h("h2", null, ch.project_title || pid),
    h("em", { class: "dz-hsub" }, ch.title || cid),
    h("div", { class: "dz-spacer" }),
    h("span", { class: "dz-chip" }, ch.aspect || "16:9"),
    h("span", { class: "dz-chip" }, `${n} 正镜`),
    h("a", { class: "dz-back", dataset: { tip: "返回章节制作台" },
      href: `#/project/${encodeURIComponent(pid)}/${encodeURIComponent(cid)}` },
      h("i", null, "←"), "制作台"));
}
