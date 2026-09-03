# 角色跨镜一致性 consistency

**协议** 引擎产料 → 你判定 → CLI 回填

## 1. 引擎一行分数都不算

**别写打分算法。** 逐条理由：

| 手段 | 为什么不行 |
|---|---|
| ffmpeg | 没有人脸/人体检测，`cropdetect` 只检黑边 |
| `ssim`/`psnr` | 同分辨率逐像素度量，对「同角色换姿态/换角度」无判别力 |
| CLIP | 拿分镜整帧比三区灰底设定表，只会量出「像不像一张设定表」 |

`vision`/`vision-clip` extra 只是登记位，一期不启用。

## 2. 产料 `consistency scan`

```bash
python3 -m kinema consistency scan --chapter x/<ch> [--only 1,3] [--aspect 16:9] [--json]
```

零成本纯本地。产出落 `<章节>_work/consistency/manifest.json`。

### 2.1 代表帧怎么来

| 模式 | 取帧方式 |
|---|---|
| kenburns | **直接由 `shots[].image` 缩放拷贝**——图就是帧，不必抽帧 |
| dubbed / native | 从片段中点抽帧 |

### 2.2 配对的设定图怎么来

取自该镜的 `lineage.required_refs`（与 `design_refs` 同源）。

**绝不另写出场角色推导**——各写一份必然与引擎真实挂载分叉。

路径**必过 `ensure_local`**——OSS 模式下 sheet 是 URL，直接喂 ffmpeg 或 `Read` 都会失败。

### 2.3 五类镜会产出空 sheets

真源 `consistency.REASONS`：

1. `skip_design` 项目；
2. `shots[].characters=[]` 显式空出场表；
3. 项目未登记角色；
4. **`shots[].characters` 点名的角色不在角色表里（名字写错）**；
5. 设定图还没生成。

**scan 会逐行喊出是哪一种——「没料可比」绝不等于「比对通过」。**

其中「名字写错」最危险：`lineage.required_refs` 按名过滤直接落空，该镜**实际根本没喂角色设定图**。

## 3. 判定回填 `consistency set`

```bash
python3 -m kinema consistency set --chapter x/<ch> --shot N --verdict ok|drift \
    [--score 0~1] [--note "发色不对"] [--retake]
```

判定是你的活：逐镜 `Read` 帧与设定图，比五官 / 发型 / 服装配色 / 体型 / 标志配件。
`score` 是你的主观分，不是机器算的。

### 3.1 `--retake` 的行为

照 `lineage.mark_stale` 纪律：

- 未锁定镜 → 置 `retake`（下次生成自动重生 ＋ 旧版归档）；
- `done` 锁定镜 → 只留判定当标记，机器不代人解锁；
- **dubbed/native 判 clip 漂移会连 `image` 一并打回**——图生视频恒以分镜图作首帧或领衔参考图，根因几乎总在图。

### 3.2 并发纪律

`shots[].consistency` 已登记进 `_SHOT_HUMAN_KEYS`，但**人工表态永远优先于机器判定**：磁盘上人刚点的
`done` 会盖掉本判定引发的 `retake` 并打印 ⚠。这是既定纪律，不是 bug。

## 4. 失效纪律

**判定只对被判的那一版画面成立——画面一被替换就整条作废**（`consistency.invalidate`，同
`lineage.clear_stale` 范式）。

清除触发点：生图 / 图生视频重生、素材直供、局部改造、宫格换选、版本回滚（CLI `versions rollback`
与 Studio 面板两入口）——**全都清**，CLI 打印「旧一致性判定已作废」。

不清的后果：在没人判过的新图上残留「⚠ 角色漂移」角标（人工点 done 后与「✓ 已通过」并排出现），
且 `frame` 存证会指向被下次 scan 就地覆盖的另一张图。

**新开「换画面」的门必须补调 `invalidate`**；`audio` 阶段传进去是空操作（重跑配音不改画面）。

重生之后要**再 scan 一次重判**——旧判定不跟着新图走。
