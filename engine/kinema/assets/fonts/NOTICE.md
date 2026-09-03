# 工程内置字体（免费商用 · 随仓库分发）

引擎把字体**内置进工程**，水印/角标/字幕都引用这里的相对路径，**不依赖各机器的系统字体**——
换机、换操作系统（macOS/Linux/Windows）字体都一致，白标交付零折腾。

真源：`kinema/fonts.py`（`FONTS_DIR` + `PUHUITI_*` 常量 + `bundled_path()`）。

---

## 已内置（全部免费商用）

| 文件 | 字体 | 族名（libass Fontname） | 许可 | 用途 |
|---|---|---|---|---|
| `AlibabaPuHuiTi-3-55-Regular.otf` | 阿里巴巴普惠体 3.0 Regular | `Alibaba PuHuiTi 3.0 55 Regular` | 阿里·永久免费商用 | 浮动水印 / 固定角标 |
| `AlibabaPuHuiTi-3-65-Medium.otf` | 阿里巴巴普惠体 3.0 Medium | `Alibaba PuHuiTi 3.0 65 Medium` | 阿里·永久免费商用 | 字幕主字 / 封面字卡(hei) |
| `NotoSerifSC-SemiBold.otf` | 思源宋体 SC SemiBold | `Noto Serif SC SemiBold` | **SIL OFL 1.1** | 国风/水墨衬线字幕(song) |
| `LXGWWenKaiLite-Regular.ttf` | 霞鹜文楷 Lite | `LXGW WenKai Lite` | **SIL OFL 1.1** | 古风手写楷体·封面字卡(kai) |
| `SmileySans-Oblique.otf` | 得意黑 Smiley Sans | `Smiley Sans` | **SIL OFL 1.1** | 展示型 logo 式标题(display·可选) |

- **阿里巴巴普惠体 3.0**：全球永久免费商用、无需署名；许可明确允许**将字体嵌入产品打包分发**
  （`嵌入式授权`），红线仅「单独把字库文件拿去售卖 / 改名牟利」——本白标系统随包分发属合规。
  来源：https://www.alibabafonts.com/#/font
- **思源宋体 / 霞鹜文楷 / 得意黑**：均 **SIL OFL 1.1** 开源许可，免费商用、可自由嵌入与再分发、
  无署名义务（许可最干净）。来源：notofonts/noto-cjk · lxgw/LxgwWenKai-Lite · atelier-anchor/smiley-sans。

字号/字型选取：无衬线黑体 Regular~Medium 最适合小字号视频文字（衬线在小字号+压缩下易糊显旧）；
国风/水墨用宋体衬线、古风用楷体，与画面气质一致。

---

## 全链路映射（`fonts.py` + `subtitle._FONT_ALIAS`）

| 角色 | 字体 | 加载 |
|---|---|---|
| 字幕（默认 caption） | 阿里普惠体 Medium | libass fontsdir（默认族名）|
| 浮动水印 / 固定角标 | 阿里普惠体 Regular | drawtext fontfile |
| 封面 / 字卡（hei/song/kai） | 普惠体 / 思源宋体 / 文楷 | resolve_font 候选链首位=内置 |
| 国风衬线 / 古风楷体字幕 | 思源宋体 / 文楷 | profile 写 `Songti SC`/`Kaiti` → `_FONT_ALIAS` 自动映射 → fontsdir |
| 展示型标题（可选） | 得意黑 | `resolve_font("display")` |

**全部免费商用、零系统字体依赖**（换机/换系统不变样）。profile 里遗留的系统字体名
（Songti SC / Kaiti 等）由 `subtitle._FONT_ALIAS` 自动归一到内置等价字体，无需逐一改 models.yaml。

## 换字体怎么做

- 水印 / 角标：`branding.yaml` 或 `project.json` 顶层 `watermark_fixed.font` 填**字体绝对路径**。
- 字幕：改 profile 的 `subtitle.font` 为目标字体**族名**（须先把字库放进本目录，libass 经 fontsdir 加载）。
- 全局默认：改 `fonts.py` 的 `PUHUITI_*` 常量 / `subtitle._CAPTION_DEFAULTS["font"]`。
