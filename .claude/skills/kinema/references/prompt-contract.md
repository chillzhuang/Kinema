<!-- 由 tools/agent_assets.py 根据 agent/contracts.json 生成；请勿手改。 -->

# Prompt 正式契约

契约版本：`prompt/v1` · 编译器版本：`1.0.0` · ChapterPlan：`chapter-plan/v1`。机器真源是 `agent/contracts.json`；本文件仅供 Agent 作者阅读。

PromptSpec 是计划与编译期 IR。章节只保存它确定性投影后的作者字段，不额外保存 PromptSpec 副本。
PromptSpec 是全量替换语义：省略的槽位会投影为空并清除旧作者字段；修改时以 `agent context` 返回的当前 PromptSpec 为基线。
画风、角色设定引用、负面地板和 provider 参数由编译器注入，Agent 不在语义槽里复制这些内容。

## 图像语义

单个可摄影瞬间的图像语义，不包含画风前缀和 provider 参数。

| 字段 | 责任 | 语言 | 投影目标 | 含义 |
|---|---|---|---|---|
| `subject` | `agent` | `zh` | `image_prompt` | 画面主体与身份锚点（默认中文） |
| `action` | `agent` | `zh` | `image_prompt` | 快门时刻可见动作（默认中文） |
| `expression` | `agent` | `zh` | `image_prompt` | 表情与视线（默认中文） |
| `composition` | `agent` | `zh` | `image_prompt` | 空间关系与构图（默认中文） |
| `framing` | `agent` | `zh` | `framing` | 景别（默认中文，除非用户明确要求英文） |
| `angle` | `agent` | `zh` | `angle` | 机位角度（默认中文） |
| `lens` | `agent` | `zh` | `lens` | 镜头与景深（默认中文，焦段/专业术语可保留原写法） |
| `lighting` | `agent` | `zh` | `lighting` | 光线设计（默认中文） |
| `creative_notes` | `agent` | `zh` | `image_prompt` | 未被其他槽位覆盖的可见细节（默认中文） |
| `text_en` | `agent` | `en` | `image_prompt_en` | 完整英文图像语义 |
| `negative` | `agent` | `zh` | `negative_prompt` | 镜级负面约束（默认中文） |

至少一个字段非空：`subject`、`action`、`composition`、`creative_notes`、`text_en`。

## 视频动作增量

从已确认首帧出发的动作增量，不重复静态造型与画风。

| 字段 | 责任 | 语言 | 投影目标 | 含义 |
|---|---|---|---|---|
| `action_delta` | `agent` | `zh` | `action` | 主体动作变化（默认中文） |
| `secondary_motion` | `agent` | `zh` | `video_prompt` | 环境与附属物次级运动（默认中文） |
| `camera` | `agent` | `zh` | `camera` | 单一主运镜（默认中文，专业技法名可保留原写法） |
| `entry_state` | `agent` | `zh` | `entry_state` | 镜头开场承接状态（默认中文）：本镜从上一镜结束时的哪个构图/位置/光线延续起步，与上一镜 end_state 互相咬合 |
| `end_state` | `agent` | `zh` | `end_state` | 镜头结束状态（默认中文） |
| `light_shift` | `agent` | `zh` | `light_shift` | 镜内光线变化（默认中文） |
| `sound` | `agent` | `zh` | `sfx` | 原生音效意图（默认中文） |
| `creative_notes` | `agent` | `zh` | `video_prompt` | 节奏、限制与未覆盖动作细节（默认中文） |
| `text_en` | `agent` | `en` | `video_prompt_en` | 完整英文视频动作语义 |

## PromptSpec 示例

```json
{
  "contract_version": "prompt/v1",
  "image": {
    "subject": "主体身份",
    "action": "快门时刻动作",
    "composition": "空间与构图",
    "lighting": "光线",
    "text_en": "Complete English image semantics"
  },
  "video": {
    "action_delta": "首帧之后发生的动作",
    "secondary_motion": "次级运动",
    "camera": "单一主运镜",
    "end_state": "结束状态",
    "text_en": "Complete English motion delta"
  }
}
```

## ChapterPlan 写入协议

固定流程：先 `agent context` 取得最小上下文和 `revision`，再构造计划，先 `plan validate`，
确认摘要后才 `plan apply`。apply 只接受当前 revision；冲突时重读上下文并重算计划。
`chapter_patch` 里与现状相同的字段会被剔除并在 `summary.unchanged_chapter_fields` 列出；
镜级 `update` 至少要改一个字段；整份计划没有任何变更时整份拒绝。
写明引擎缺省会落盘但不算生效变更：`motion` 按内容定档、`audio_mode` 缺省 tracks、
布尔开关缺席按引擎缺省（`voice_anchor` 开，其余关）。失效传播与 done 锁校验只看
`summary.chapter_effective_changes`；`context.effective` 给出推导的 motion 与 audio_mode。

允许的镜头操作：`add`、`update`、`omit`、`restore`。禁止 delete、镜头重排、任意 JSON Patch
和整份章节覆盖。图像/视频字段只通过 `prompt_spec` 提交。

新增镜头必须提供：`dur`、`narration` 与 `prompt_spec`。

### 章节字段

| 字段 | 类型 | 写入语义 |
|---|---|---|
| `theme` | `string` | 字段级替换 |
| `script` | `object` | 浅合并，只允许 `hook`、`body`、`cta`、`per_platform` |
| `scene` | `string` | 字段级替换 |
| `style_prompt` | `string` | 字段级替换 |
| `style_prompt_en` | `string` | 字段级替换 |
| `voiceover` | `string` | 字段级替换 |
| `subtitle_lang` | `string` | 字段级替换 |
| `motion` | `string` | 字段级替换 |
| `audio_mode` | `string` | 字段级替换 |
| `native_voiceover` | `boolean` | 字段级替换 |
| `previz_v2v` | `boolean` | 字段级替换 |
| `tail_relay` | `boolean` | 字段级替换 |
| `anchor_frame` | `boolean` | 字段级替换 |
| `frame_chain` | `boolean` | 字段级替换 |
| `scored_bgm` | `boolean` | 字段级替换 |
| `native_bgm` | `boolean` | 字段级替换 |
| `control_bgm` | `boolean` | 字段级替换 |
| `voice_anchor` | `boolean` | 字段级替换 |
| `cover_prompt` | `string` | 字段级替换 |
| `effects` | `string_list` | 字段级替换 |
| `subtitle` | `object` | 浅合并，只允许  |
| `speech_rate` | `integer` | 字段级替换 |
| `art_direction` | `object` | 浅合并，只允许 `variety`、`motion`、`density`、`avoid` |
| `voice_performance` | `object` | 浅合并，只允许 `pacing`、`energy_curve` |

镜头白名单：`dur`、`narration`、`narration_en`、`lines`、`caption`、`caption_en`、`characters`、`props`、`scenes`、`refs`、`speaker`、`emotion`、`shot_intent`、`narrative_role`、`hero_moment`、`profile`、`face_visibility`、`delivery`、`voice_instruction`、`emotion_scale`、`voice`、`dialogue`、`attribution`、`rank`、`title`、`corner_note`、`bubble_pos`、`priority`、`anchor_frame`、`frame_chain`、`guide`、`sketch`。

`lines[]` 成员：`text`（必填）、`text_en`、`speaker`、`voice`、`emotion`、`emotion_scale`、`voice_instruction`、`delivery`。
`sketch.beats[]` 成员：`t`、`action`（必填）、`camera`、`framing`、`light`、`sound`。

### ChapterPlan 最小示例

```json
{
  "contract_version": "chapter-plan/v1",
  "chapter": "demo/ch01",
  "expected_revision": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "chapter_patch": {
    "voiceover": "sparse"
  },
  "shots": [
    {
      "op": "update",
      "id": 1,
      "fields": {
        "narration": "他听见身后的脚步。"
      },
      "prompt_spec": {
        "contract_version": "prompt/v1",
        "image": {
          "subject": "主体身份",
          "action": "快门时刻动作",
          "composition": "空间与构图",
          "lighting": "光线",
          "text_en": "Complete English image semantics"
        },
        "video": {
          "action_delta": "首帧之后发生的动作",
          "secondary_motion": "次级运动",
          "camera": "单一主运镜",
          "end_state": "结束状态",
          "text_en": "Complete English motion delta"
        }
      }
    }
  ],
  "provenance": {
    "host": "codex",
    "model": "model-id"
  }
}
```
