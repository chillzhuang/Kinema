# kinema 执行引擎

三层架构中的第 ② 层。承接 Skill 指挥层交付的 `project.json`，按 `config/models.yaml` 的
profile 解析模型（ModelRouter），逐阶段调用能力层 provider 并用 FFmpeg 合成成片——
主比例默认 16:9 横屏，`--aspect` 点名才竖屏/方形。渲染模式三种（kenburns /
dubbed / native），章级单入口：未表态时按内容定档（有对白 → native，全旁白 → dubbed），
kenburns 须显式指定。**不执行平台发布**：`deliver` 只出交付包，上传由你自己完成。

整体说明见项目根 `../README.md`；总体设计 `../docs/kinema/design.md`；能力对比 `../docs/kinema/providers.md`。

## 依赖

- **必需**：系统 `ffmpeg` / `ffprobe`（`brew install ffmpeg`）。核心与 mock 链路**零 Python 依赖**。
- **可选**：真实云 provider 需 `requests`，yaml 配置需 `PyYAML`：
  ```bash
  pip install -e ".[cloud,yaml]"
  ```
- `vision` / `vision-clip` 两个 extra 只是**登记位**（后者 GB 级），**一期不启用**：
  角色一致性走「`consistency scan` 抽帧产料 → 指挥层多模态判定 → `consistency set` 回填」，
  引擎不算相似度分数，装了也不会被调用。

## 快速开始（离线，零成本）

```bash
python3 -m kinema doctor
cp examples/sample_project.json /tmp/demo.json     # 先拷贝，保持样例纯净
python3 -m kinema run --project /tmp/demo.json --mock
# 成片：/tmp/demo_work/output/demo_pomodoro_9x16.mp4（文件名带比例后缀）
```

样例两份：`sample_project.json`（口播解说 · explainer 档）与 `sample_hd2d.json`
（剧情 · hd2d 档，含多角色音色表、`lines[]` 逐句台词与一个纯画面呼吸镜）。
两份都可直接当分镜单模板抄；先跑一遍 `kinema lint --project <文件>`，输出即体检维度的实际样子（样例按一段式运动描述写，深度档会提示补 `sketch.beats`）。

`--mock` 用占位图/合成音替代真实 API，用于验证环境与整条链路。

## 命令

完整命令表见 `../DEVELOP.md` 五、CLI 命令全表（与 argparse 双向对拍守卫钉住）。
最短验证路径三条：

```bash
python3 -m kinema doctor                              # 自检 ffmpeg 与可用 provider
python3 -m kinema run --chapter demo/<章节> --mock    # 零成本跑通全链路
python3 -m kinema studio                              # 可视化大屏（自动使用仓库根 project/）
```

常用参数：`--mock`（离线）、`--force`（忽略 checkpoint 强制重生）、`--profile`（覆盖风格档）、
`--out`（compose 输出路径）、`--config`（指定 models.yaml）。

## 真实 provider 密钥（环境变量）

见 `config.example.yaml`，字段级说明与读取优先级（环境变量 > `config/secrets.local.json` >
`config/secrets.yaml`）在 `../config/README.md`。图像与视频 `ARK_API_KEY`；配音
`ARK_TTS_API_KEY`（火山语音独立凭证，单头 X-Api-Key；备选 `MINIMAX_API_KEY`+`MINIMAX_GROUP_ID`）；
音乐 `ELEVENLABS_API_KEY`（缺省自动降级本地曲库）；口型精修 `VOLC_ACCESS_KEY`+`VOLC_SECRET_KEY`（可选）。

> ⚠️ 真实 provider 的接口地址/参数/计费在快速变动，代码中已标注「上线前核对官方文档」。
> 首次接入某家 API 前，请用自己的账号核对实时价并小样验证。

## 目录

```
kinema/
├── cli.py            # CLI 入口与阶段编排
├── models.py         # 配置/密钥加载（ConfigStore）+ profile 模型路由（ModelRouter）
├── project.py        # project.json 读写 + checkpoint + 时间轴
├── ffmpeg.py         # ffmpeg/ffprobe 封装
├── assets/           # 内置字体 + 设定图/简笔板版式样板
├── storage/          # 持久化后端：local JSON / mysql（config/storage.yaml）
├── studio/           # 制作台后端：scanner 数据层 + server HTTP 层 + jobs 异步 + actions 写操作
├── studio_app/       # 制作台前端：原生 ESM 免构建（app/ 制作台 · director/ 3D 导演台 · vendor/ three.js）
├── providers/        # 可插拔能力层：image/ video/ tts/ music/ lipsync/（每家一个适配器 + mock）
└── pipeline/         # 确定性算法：prompts 提示词编译 / checkpoint / kenburns / transitions / mixdown / subtitle / compose / refplan / versioning / mediacheck（成片自审与供料体检的判据真源，刻意不叫 inspect.py——与 stdlib 同名会污染 sys.path[0]）/ asr
```

逐模块地图（与代码树双向对拍守卫钉住）见 `../DEVELOP.md` 三、引擎模块地图。

## 产物布局

对 `foo.json`，产物落在同目录 `foo_work/`：`images/ audio/ subs/ gen_clips/ clips_<比例>/ build/ output/`。
`project.json` 里 `shots[].status`、`shots[].image`、`audio.*`、`output.*` 记录进度，支持断点续跑。
