# This file is part of Kinema.
# Copyright (C) 2018-2099 BladeX (https://bladex.cn)
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""配置真源加载 + 能力路由。

ConfigStore：加载 config/models.yaml（providers + profiles + defaults），任何 skill 独立读取。
ModelRouter：按 capability(image/video/tts/music) + profile 解析出「已配置好的 provider + 参数」，
是「模型调用返回封装起来一起调用」的核心——上层只说要什么能力/什么风格，不关心底层模型。

找不到 models.yaml 或缺 PyYAML 时回退到内置默认，保证 mock 离线可跑。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from .errors import ConfigError, ProviderError

# 内置默认（models.yaml 的最小可用子集）——保证零配置也能跑 mock 与基础 profile。
# 完整可编辑版本见 config/models.yaml（该文件为唯一真源，此处仅回退兜底）。
EMBEDDED_DEFAULTS = {
    "version": 1,
    # 比例不是配置项：默认主比例是引擎常量 project.DEFAULT_ASPECT（16:9），
    # 画布尺寸在 canvas 段。此处不设 aspect 键：无人消费的死键只会误导指挥层。
    "defaults": {"profile": "narration", "fps": 30,
                 # 能力级默认 provider 别名（全局总入口）：profile 未显式指定时用它——
                 # 换厂商只改这里一行，42 个 profile 零改动
                 "providers": {"image": "seedream", "video": "seedance-mini",
                               "tts": "seedtts", "music": "elevenlabs",
                               "lipsync": "volc-lipsync"}},
    "canvas": {"9:16": [1080, 1920], "16:9": [1920, 1080], "1:1": [1080, 1080]},
    "providers": {
        "seedream": {"kind": "image", "status": "ready",
                     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                     "model": "doubao-seedream-5-0-pro-260628",
                     "api_key_env": "ARK_API_KEY", "max_pixels": 0,
                     "price_per_image": 0.3, "price_per_image_hd": 0.6,
                     "hd_pixels": 2360000},
        "seedtts": {"kind": "tts", "status": "ready",
                    "base_url": "https://openspeech.bytedance.com/api/v3",
                    "resource_id": "seed-tts-2.0", "voice": "zh_female_vv_uranus_bigtts",
                    "format": "mp3", "sample_rate": 24000,
                    "api_key_env": "ARK_TTS_API_KEY"},
        "doubao": {"kind": "tts", "status": "ready",
                   "base_url": "https://openspeech.bytedance.com/api/v3",
                   "model": "seed-audio-1.0", "voice": "zh_female_vv_uranus_bigtts",
                   "format": "mp3", "sample_rate": 24000,
                   "api_key_env": "ARK_TTS_API_KEY"},
        # 不钉 voice：两站系统音色 ID 命名互不相通，缺省音色由适配器按 base_url
        # 所在站分派（与 yaml 真源同口径）
        "minimax": {"kind": "tts", "status": "ready",
                    "base_url": "https://api.minimax.io/v1", "model": "speech-2.8-hd",
                    "api_key_env": "MINIMAX_API_KEY",
                    "group_id_env": "MINIMAX_GROUP_ID"},
        "elevenlabs": {"kind": "music", "status": "ready",
                       "base_url": "https://api.elevenlabs.io/v1",
                       "model": "music_v2",
                       "api_key_env": "ELEVENLABS_API_KEY", "price_per_min": 0.0},
        # ⚠ 能力位必须与 models.yaml 真源逐项一致（test_router_defaults 对拍）：
        # 内嵌表是缺 PyYAML 时的回退态，能力位缺一项就回落适配器默认——
        # mini/2.5 都会被塞进官方已不支持的 seed，2.5 还会发 explicit ratio
        # 并把 20s 镜静默截到 15s（与远端无关，本地钳的）。
        "seedance-mini": {"kind": "video", "status": "ready", "impl": "seedance",
                     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                     "model": "doubao-seedance-2-0-mini-260615",
                     # mini 只支持 480p/720p（与 yaml 真源锁步）
                     "resolution": "720p",
                     "resolutions": ["480p", "720p"],
                     "timeline_unit": "shot",
                     "supports_seed": False, "supports_camera_fixed": False,
                     "supports_last_frame": True,
                     "max_ref_audios": 3, "max_ref_audio_seconds": 15,
                     "api_key_env": "ARK_API_KEY", "price_per_second": 0.5,
                     "price_per_second_4k": 1.0},
        # 2.5 大模型：**点名才用**（`gen-video --video-provider seedance-2.5` /
        # 章节文档顶层 `video_provider`），缺省恒走上面的 mini——大模型绝不静默升级
        "seedance-2.5": {"kind": "video", "status": "ready", "impl": "seedance",
                       "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                       "model": "doubao-seedance-2-5-260628",
                       "resolution": "720p",
                       "resolutions": ["480p", "720p", "1080p"],
                       "min_duration": 4, "max_duration": 30,
                       "ratio_mode": "adaptive",
                       "supports_seed": False, "supports_camera_fixed": False,
                       "supports_last_frame": True,
                       "timeline_unit": "second",
                       "max_ref_audios": 10, "max_ref_audio_seconds": 30,
                       "api_key_env": "ARK_API_KEY", "price_per_second": 1.51,
                       "price_per_second_4k": 0},
        "local": {"kind": "music", "status": "ready"},
        # 视频改口型（增强步）：req_key 须按官方接口文档在 config/models.yaml 配置，
        # 缺配置时 stage 点名跳过、不拦出片主链
        "volc-lipsync": {"kind": "lipsync", "impl": "volc_lipsync",
                         "status": "ready", "host": "visual.volcengineapi.com",
                         "ak_env": "VOLC_ACCESS_KEY", "sk_env": "VOLC_SECRET_KEY"},
        # agent 原生生图 · 工单模式：引擎开单、驱动引擎的 AI 产图。零密钥零端点，
        # 路由见 image_route（显式激活 > KINEMA_AGENT_IMAGEGEN 声明 > 默认）。
        "agent": {"kind": "image", "status": "ready", "impl": "agent",
                  "price_per_image": 0.0},
    },
    "profiles": {
        "narration": {"image": {"provider": "seedream", "style_prefix": ""},
                      "tts": {"provider": "seedtts"},
                      "music": {"provider": "elevenlabs"}, "effects": []},
        "anime": {"image": {"provider": "seedream",
                            "style_prefix": "现代日本新番动画风格，数字作画锐利线条，高饱和色设计，高密度粒子光效与拖尾，大透视倾斜构图，"},
                  "tts": {"provider": "seedtts"},
                  "music": {"provider": "elevenlabs", "mood": "cinematic"},
                  "effects": ["bloom"]},
        "anime_cel": {"image": {"provider": "seedream",
                                "style_prefix": "日式赛璐璐动画风格，鲜明清晰的轮廓线，扁平上色，高饱和度，"},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                      "effects": []},
        "anime_80s": {"image": {"provider": "seedream", "style_prefix": "1980年代日本剧场版赛璐璐动画，经费爆炸级手绘作画，胶片颗粒，复古色域，厚涂背景，OVA黄金期美学，"},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["film_grain", "vignette"]},
        "ghibli": {"image": {"provider": "seedream", "style_prefix": "吉卜力工作室手绘动画风格，水彩手绘背景，蓝天积雨云，温暖自然光，圆润角色，田埂风车与晾晒衣物同框，"},
                   "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "calm"}, "effects": ["warm"]},
        "shinkai": {"image": {"provider": "seedream", "style_prefix": "新海诚电影风格，写实系动画光影，强烈逆光与镜头光晕，饱和黄昏天空积云，壁纸级细节，"},
                    "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "calm"}, "effects": ["bloom", "vignette"]},
        "pixar": {"image": {"provider": "seedream", "style_prefix": "皮克斯3D动画电影风格，圆润夸张造型，次表面散射皮肤，电影级PBR渲染，暖色叙事光，"},
                  "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "upbeat"}, "effects": ["vignette"]},
        "disney3d": {"image": {"provider": "seedream", "style_prefix": "迪士尼3D动画电影风格，圆润角色设计、大眼睛虹膜多层高光，丝绸毛发渲染，柔焦体积光束穿过浮尘，粉紫渐变魔法粒子上浮，舞台布光，"},
                     "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["bloom"]},
        "anime_xianxia": {"image": {"provider": "seedream", "style_prefix": "国漫仙侠动画风格，古风二次元人设，工笔勾线水墨晕染，衣袂飘逸灵力流光，云海仙山飞檐，"},
                          "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["bloom", "fog"]},
        "anime_mecha": {"image": {"provider": "seedream", "style_prefix": "机甲热血动画风格，硬朗机械设定作画，装甲分件HUD光效，爆炸烟火，强透视热血构图，"},
                        "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["film_grain", "vignette"]},
        "anime_fairytale": {"image": {"provider": "seedream", "style_prefix": "童话绘本动画风格，水彩色铅笔纸纹，扁平装饰构图，圆润稚拙造型，绘本跨页插画感，"},
                            "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "calm"}, "effects": ["warm"]},
        "anime_ink": {"image": {"provider": "seedream", "style_prefix": "中国水墨动画风格，美影厂水墨片质感，湿笔晕染飞白，墨分五色留白构图，气韵生动，"},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "calm"}, "effects": ["fog", "vignette"]},
        "anime3d": {"image": {"provider": "seedream", "style_prefix": "顶级3D国漫渲染，年番级工业水准，写实向东方人物建模，发丝布料解算，材质分层考究，体积光与大气透视，"},
                    "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["bloom"]},
        "game_sim": {"image": {"provider": "seedream",
                               "style_prefix": "3D 游戏引擎实时渲染画面，带 HUD 抬头显示，",
                               # HUD 血条/小地图本来就是画面内容 → 关防字地板（opt-out 仅此与 explainer）
                               "image_text_floor": False},
                     "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                     "effects": ["hud", "scanlines"]},
        "hd2d": {"image": {"provider": "seedream",
                           "style_prefix": "HD-2D 游戏美术风格，2D 像素精灵角色置于 3D 立体透视微缩布景中，tilt-shift 移轴景深虚化，柔和 bloom 泛光，暖色氛围光，复古 JRPG 电子游戏过场画面，八方旅人 Octopath Traveler 风格，"},
                 "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                 "effects": ["bloom", "vignette"],
                 "subtitle": {"mode": "dialogue_box", "box": "#161018", "box_alpha": 40,
                              "border": "#e8c979", "name_color": "#ffd45e",
                              "text_color": "#f6f1e6"}},
        "gba": {"image": {"provider": "seedream", "style_prefix": "GBA 复古 16-bit 像素 RPG 游戏画面，有限调色板，硬边像素点阵，Game Boy Advance 时代 JRPG 美术，"},
                "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                "effects": ["vignette"],
                "subtitle": {"mode": "dialogue_box", "box": "#0b1a2a", "box_alpha": 30,
                             "border": "#6fb7ff", "name_color": "#9be0ff", "text_color": "#eaf6ff"}},
        "snes": {"image": {"provider": "seedream", "style_prefix": "SNES 超任 16-bit 像素 RPG 游戏画面，丰富饱和调色板，Mode7 伪 3D 透视，超级任天堂时代 JRPG 过场，"},
                 "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                 "effects": ["vignette", "warm"],
                 "subtitle": {"mode": "dialogue_box", "box": "#241a2e", "box_alpha": 32,
                              "border": "#f0c96b", "name_color": "#ffe08a", "text_color": "#f7efe0"}},
        "dark_fantasy": {"image": {"provider": "seedream", "style_prefix": "暗黑哥特像素美术，压抑低饱和冷色调，高对比明暗，火把幽光，血色点缀，黑暗奇幻 JRPG 画面，"},
                         "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                         "effects": ["vignette", "film_grain"],
                         "subtitle": {"mode": "dialogue_box", "box": "#100a0c", "box_alpha": 26,
                                      "border": "#8a2b2b", "name_color": "#d94b4b", "text_color": "#e8dcd0"}},
        "explainer": {"image": {"provider": "seedream", "style_prefix": "简洁现代信息图与编辑插画风格，清晰构图，高可读性，知识科普视觉，",
                                # 信息图的标注/图例本来就是画面内容 → 关防字地板（opt-out 仅此与 game_sim）
                                "image_text_floor": False},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"}, "effects": []},
        "quote": {"image": {"provider": "seedream", "style_prefix": "极简氛围背景，柔和光影，大面积留白，低对比，适合叠加文字，"},
                  "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                  "effects": ["vignette"],
                  "subtitle": {"mode": "centered", "text_color": "#ffffff", "accent": "#ffd45e"}},
        "ranking": {"image": {"provider": "seedream", "style_prefix": "干净醒目的视觉，鲜明高对比，适合叠加榜单序号，"},
                    "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"},
                    "effects": [],
                    "subtitle": {"mode": "ranking", "badge": "#ff4d4f", "num_color": "#ffffff", "title_color": "#ffffff"}},
        "storybook": {"image": {"provider": "seedream", "style_prefix": "温暖手绘绘本插画风格，柔和水彩质感，圆润造型，童话氛围，"},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs"}, "effects": ["warm"]},
        "cyberpunk": {"image": {"provider": "seedream", "identity_sheet": True, "style_prefix": "赛博朋克风格，雨夜霓虹都市，青品红霓虹光污染，湿润街道反光，高对比电影级构图，"},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["rain", "vignette"]},
        "clay": {"image": {"provider": "seedream", "style_prefix": "粘土定格动画风格，手工黏土材质带指纹瑕疵，圆润厚实造型，微距浅景深，"},
                 "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "upbeat"}, "effects": ["warm"]},
        "gunpla": {"image": {"provider": "seedream", "style_prefix": "高达塑料模型定格动画风格，注塑塑料质感与刻线渗线，可动关节，微缩战场布景，战损做旧，影棚微距布光，"},
                   "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "cinematic"}, "effects": ["vignette", "film_grain"]},
        "figure": {"image": {"provider": "seedream", "style_prefix": "手办定格动画风格，PVC光泽塑胶质感与分色涂装（渐变喷涂、边缘无溢色），粘土人大头可动造型，球形关节，桌面微缩场景，微距浅景深，"},
                   "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "upbeat"}, "effects": ["vignette"]},
        "brick": {"image": {"provider": "seedream", "style_prefix": "拼装积木定格动画风格，塑料颗粒锐利棱角与圆凸点，积木小人，全场景积木拼成，高饱和纯色块，"},
                  "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "upbeat"}, "effects": []},
        "miniature": {"image": {"provider": "seedream", "style_prefix": "微缩模型世界风格，tilt-shift 移轴摄影，玩具般微型场景，可见胶合缝与草粉颗粒，强浅景深，斜俯视角，"},
                      "tts": {"provider": "seedtts"}, "music": {"provider": "elevenlabs", "mood": "calm"}, "effects": ["vignette"]},
    },
}


# 内置角色音色预设兜底（完整可编辑版见 config/voices.yaml）
EMBEDDED_VOICES = {
    "解说男": "zh_male_jieshuoxiaoming_uranus_bigtts",
    "悬疑解说": "zh_male_xuanyijieshuo_uranus_bigtts",
    "磁性解说": "zh_male_cixingjieshuonan_uranus_bigtts",
    "儒雅旁白": "zh_male_ruyaqingnian_uranus_bigtts",
    "治愈女": "zh_female_xinlingjitang_uranus_bigtts",
    "绘本女": "zh_female_shaoergushi_uranus_bigtts",
    "少年": "zh_male_shaonianzixin_uranus_bigtts",
    "暖男": "zh_male_wennuanahu_uranus_bigtts",
    "沧桑老者": "zh_male_qingcang_uranus_bigtts",
    "温柔女": "zh_female_wenroushunv_uranus_bigtts",
    "温柔长辈": "zh_female_wenroumama_uranus_bigtts",
    "邻家女孩": "zh_female_linjianvhai_uranus_bigtts",
    "高冷御姐": "zh_female_gaolengyujie_uranus_bigtts",
    "古风少女": "zh_female_gufengshaoyu_uranus_bigtts",
    "童声": "zh_male_tiancaitongsheng_uranus_bigtts",
}


def _load_voices(models_path: Path) -> dict:
    """从 models.yaml 同目录的 voices.yaml 加载角色音色预设（别名→voice_type）。"""
    out = dict(EMBEDDED_VOICES)
    try:
        import yaml
    except ImportError:
        return out
    vp = models_path.parent / "voices.yaml"
    if not vp.is_file():
        return out
    data = yaml.safe_load(vp.read_text(encoding="utf-8")) or {}
    for alias, v in (data.get("presets") or {}).items():
        if isinstance(v, dict) and v.get("voice"):
            out[alias] = v["voice"]
        elif isinstance(v, str):
            out[alias] = v
    return out


def _config_stamp(explicit: str | None) -> tuple:
    """配置真源的文件指纹：`load()` 读过的每个文件的 (路径, mtime_ns)。

    覆盖的是 `load()` 的全部输入面——models.yaml + 覆盖层 + 两份密钥文件；
    少看一个，那个文件改了就"改了不生效"。文件不存在也要记（None），
    否则"从无到有新建覆盖层"这种变更检测不到。
    """
    from . import config_overlay
    files: list[Path] = []
    mf = _find_models_file(explicit)
    if mf is not None:
        files += [mf, mf.parent / "voices.yaml", mf.parent / "secrets.yaml"]
    cfg = config_overlay.config_dir(explicit)
    if cfg is not None:
        files += [cfg / config_overlay.OVERLAY_FILE, cfg / "secrets.yaml",
                  cfg / "secrets.local.json"]
    out = []
    for f in dict.fromkeys(files):          # 去重保序
        try:
            out.append((str(f), f.stat().st_mtime_ns))
        except OSError:
            out.append((str(f), None))
    return tuple(out)


def _find_models_file(explicit: str | None) -> Path | None:
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    env = os.environ.get("KINEMA_MODELS")
    if env and Path(env).is_file():
        return Path(env)
    # 从 cwd 与本包位置向上查找 config/models.yaml
    starts = [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for d in [start, *start.parents]:
            cand = d / "config" / "models.yaml"
            if cand.is_file():
                return cand
    home = Path.home() / ".config" / "kinema" / "models.yaml"
    return home if home.is_file() else None


# ---------------------------------------------------------------------------
# 别名与实现名的命名分工（两套写法不是随意的）
#   · **别名**（providers 段的键，出现在 yaml / `--video-provider` / 项目文档的
#     `video_provider` 里）用**连字符**：它面向用户，与各家模型 ID 的写法一致
#     （doubao-seedance-2-0-mini / image-01 / music-3.0 / MiniMax-H3 / speech-2.8-hd）。
#   · **impl**（适配器实现名）用**下划线**：它必须与 Python 模块名同名
#     （providers/image/nano_banana.py），而模块名不能带连字符。
#   两条由 `test_config_drift` 的命名守卫强制——只写在注释里的约定会漂回去。
# ---------------------------------------------------------------------------
# 改过名的别名：旧名 → 新名。存量 project.json 的顶层 `video_provider`
# 与人的肌肉记忆都可能还停在旧名上，认一下比抛「未知 provider」友好。
LEGACY_ALIASES = {
    "seedance": "seedance-mini", "seedance25": "seedance-2.5",
    "nano_banana": "nano-banana", "minimax_image": "minimax-image",
    "minimax_music": "minimax-music", "minimax_h3": "minimax-h3",
    "minimax_h3_local": "minimax-h3-local",
}


class ConfigStore:
    def __init__(self, data: dict, source: str | None = None,
                 voices: dict | None = None, secrets: dict | None = None,
                 fallback: str | None = None, overlay: dict | None = None):
        self.data = data
        self.source = source
        self.voices = voices if voices is not None else dict(EMBEDDED_VOICES)
        self.secrets = secrets or {}
        # 用户覆盖层的生效面（config_overlay.summary()）：非空=网页/CLI 配过的连接段
        # 或激活项正在凌驾于 models.yaml 之上。**独立于下面的 fallback**——那个说的是
        # "内置精简配置在服务"，两件事混成一个会让配置健康告警报错状态。
        self.overlay = overlay
        # 非空=正在用内置精简配置（EMBEDDED_DEFAULTS 是 yaml 真源的缩水子集）：
        # "missing-pyyaml"=models.yaml 在盘上但缺 PyYAML 读不了；
        # "missing-config"=磁盘根本没找到 models.yaml。
        # doctor 与 Studio /api/overview 靠它把静默回退变成看得见的告警——
        # 只打 stdout 的 ⚠ 网页用户永远看不见（kn-anime3d 画风会悄悄剩 1 个）
        self.fallback = fallback

    # ── 进程内共享实例（按配置路径分桶）+ 文件指纹自失效 ────────────────────
    # 长驻进程（Studio）若在启动时取一次 ConfigStore 并持有到进程结束，磁盘上的
    # models.yaml / 覆盖层 / secrets 改了它一概不知，表现为「新加的画风网页报 500」
    # 「保存了密钥但 provider 仍用旧值」，且每新增一个持有点就复发一次。
    # 让每个调用点各自 `ConfigStore.load()` 重读能绕开，但要求所有人记住这条纪律，
    # 且每个请求都重复解析三份文件。
    # 现行方案是「一个实例 + mtime 指纹自失效」：持有者无需纪律，文件没变就零解析。
    _shared: dict[str | None, "ConfigStore"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls, explicit: str | None = None) -> "ConfigStore":
        """取进程内共享实例；配置文件有变更则**原地重载**后返回同一个对象。

        长驻进程（Studio / 常驻任务）一律用它取配置，**不要缓存 `load()` 的返回值**
        ——那等于把启动瞬间的配置钉死在进程里。`load()` 保持"每次真读一份"的语义
        不动，供 CLI 单次执行与测试使用。
        """
        with cls._shared_lock:
            st = cls._shared.get(explicit)
            if st is None:
                st = cls._shared[explicit] = cls.load(explicit)
            else:
                st.refresh_if_stale()
            return st

    def refresh_if_stale(self) -> bool:
        """配置文件变了就原地换血，返回是否真的重载过。

        **原地**是关键：长驻持有者拿到的是同一个对象引用，换新实例它们看不见。
        """
        if _config_stamp(self._explicit) == self._stamp:
            return False
        fresh = ConfigStore.load(self._explicit)
        self.__dict__.update(fresh.__dict__)
        return True

    @classmethod
    def load(cls, explicit: str | None = None) -> "ConfigStore":
        """加载配置真源（**每次调用都真读盘**），并给实例打上文件指纹。

        指纹供 `refresh_if_stale` 判过期；三条出口共用这一个包装，
        新增出口不必记得补——忘了补的那条就是下一个「配置改了不生效」。
        """
        st = cls._load_raw(explicit)
        st._explicit = explicit
        st._stamp = _config_stamp(explicit)
        return st

    @classmethod
    def _load_raw(cls, explicit: str | None = None) -> "ConfigStore":
        """加载配置真源，最后叠上用户覆盖层。

        覆盖层挂在**这里**而不是 ModelRouter，是因为有六处直读 `store.data` 绕过
        路由（音效生成直接建适配器、doctor 分组、overview
        的画风目录与画布）。挂在路由上，那几条会继续用文件里的旧值，表现是
        「网页改了端点，生图走新的、音效走旧的」——最难查的一类分裂。
        三条出口都要叠，**兜底出口尤其不能漏**：漏了就是「缺 PyYAML 时网页配置
        静默失效」，而那正是最需要它顶上的时候。
        """
        from . import config_overlay
        ov = config_overlay.read(explicit=explicit)
        path = _find_models_file(explicit)
        if path is None:
            cfg = config_overlay.config_dir(explicit)
            return cls(config_overlay.apply(EMBEDDED_DEFAULTS, ov), source="<embedded>",
                       fallback="missing-config",
                       secrets=config_overlay.file_secrets(cfg, explicit=explicit),
                       overlay=config_overlay.summary(ov, explicit=explicit))
        try:
            import yaml
        except ImportError:
            # 磁盘上明明有 models.yaml 却因缺 PyYAML 静默换用内置精简配置，
            # 同一 profile 可能换 provider/丢字幕样式——必须显式告知
            print(f"  ⚠ 找到配置 {path} 但缺 PyYAML，已回退内置默认配置"
                  "（provider/字幕样式可能与文件不一致）——pip install -e \"engine[yaml]\"")
            return cls(config_overlay.apply(EMBEDDED_DEFAULTS, ov),
                       source="<embedded (PyYAML 缺失)>", fallback="missing-pyyaml",
                       secrets=config_overlay.file_secrets(path.parent, explicit=explicit),
                       overlay=config_overlay.summary(ov, explicit=explicit))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # 与内置默认浅合并，缺项兜底
        merged = dict(EMBEDDED_DEFAULTS)
        merged.update({k: v for k, v in data.items() if v is not None})
        return cls(config_overlay.apply(merged, ov), source=str(path),
                   voices=_load_voices(path),
                   secrets=config_overlay.file_secrets(path.parent, explicit=explicit),
                   overlay=config_overlay.summary(ov, explicit=explicit))

    def resolve_voice(self, ref: str | None) -> str | None:
        """把角色音色别名解析成 voice_type；已是 voice_type 或空则原样返回。"""
        if not ref:
            return None
        return self.voices.get(ref, ref)

    # ---- 访问 ----
    @property
    def default_profile(self) -> str:
        return (self.data.get("defaults") or {}).get("profile", "narration")

    @property
    def fps(self) -> int:
        return int((self.data.get("defaults") or {}).get("fps", 30))

    def canvas(self, aspect: str) -> tuple[int, int]:
        w, h = (self.data.get("canvas") or {}).get(aspect, [1080, 1920])
        return int(w), int(h)

    def profile(self, name: str | None) -> dict:
        name = name or self.default_profile
        profiles = self.data.get("profiles") or {}
        if name not in profiles:
            raise ConfigError(
                f"未知 profile: {name}（可选: {sorted(profiles)}）。见 config/models.yaml")
        return profiles[name]

    def provider_conn(self, name: str) -> dict:
        providers = self.data.get("providers") or {}
        conn = providers.get(name)
        if conn is None and name in LEGACY_ALIASES:
            # 别名改过名，而存量项目文档里可能点过旧名（顶层 video_provider）。
            # 直接抛「未知 provider」会让老项目突然跑不了，故认旧名并说一声。
            new = LEGACY_ALIASES[name]
            if new in providers:
                print(f"  ⚠ provider 别名 '{name}' 已更名为 '{new}'，本次按新名解析"
                      f"（建议改掉项目里的点名，旧名是兼容位）")
                name, conn = new, providers[new]
        if conn is None:
            raise ConfigError(f"未知 provider: {name}。见 config/models.yaml")
        return {**conn, "name": name}

    def default_provider(self, capability: str) -> str | None:
        """能力级默认 provider 别名（defaults.providers 全局总入口）。

        profile 只在**偏离默认**时才写 provider——换厂商/换模型只改
        defaults.providers 一行（或该 provider 别名的连接段），全部 profile 零改动。"""
        return ((self.data.get("defaults") or {}).get("providers") or {}).get(capability)

    def secret(self, env_name: str | None, *, required: bool = True) -> str | None:
        # 环境变量 > secrets.local.json > secrets.yaml（后两者已由 file_secrets 合并）
        val = os.environ.get(env_name) if env_name else None
        if not val and env_name:
            val = self.secrets.get(env_name)
        if required and not val:
            raise ConfigError(
                f"缺少密钥 {env_name}。请在 config/secrets.yaml 填写"
                f"（该文件由 setup 自动生成，直接填值即可），或用 "
                f"`kinema config secret {env_name} <值>` 写入本机密钥文件，"
                f"或 export {env_name}=...（或用 mock provider 离线测试）。")
        return val

    def effects_for(self, profile_name: str | None, override: list | None) -> list[str]:
        """生效特效解析（compose/animatic/scanner 三方共用的唯一收口）。

        特效是显式创作决定：只认章节/项目 `effects` 点名，**不回落画风清单**
        ——画风配置的 effects 仅作候选目录（选择器展示用）。
        未知特效名**当场报错**而不是静默丢弃：下游 `fx.build_plan` 对认不出的名字
        返回 None 被过滤，而阶段行在过滤之前就打印了「特效[...]」——用户与 Studio
        都以为已应用。特效名的来路是配置与文档（skills 会教 agent 直写章节
        effects），拼错或引用不存在的名字必须在花钱合成前喊出来。"""
        from . import effects as fx
        names = list(override) if override is not None else []
        unknown = [n for n in names if n not in fx.EFFECTS]
        if unknown:
            raise ConfigError(
                f"未知特效名: {', '.join(unknown)}——不会被应用。"
                f"可用: {', '.join(sorted(fx.EFFECTS))}")
        return names


# ---------------------------------------------------------------------------
# 适配器注册表（工厂的"字典分发"层）
# ---------------------------------------------------------------------------
# key = (capability, impl)。provider 别名（providers 段的 key）通过可选 `impl`
# 字段指向适配器实现，缺省 impl = 别名本身。由此：
#   · 同一实现类可挂任意多个别名（如 seedream_v6 / my_flux 指 impl: seedream，
#     各配自己的 base_url/model/计费）——加别名零代码；
#   · 新厂商 = providers/<capability>/<name>.py 写适配器 + 此表登记一行；
#   · 统一返回：所有适配器返回 ImageResult/TTSResult/VideoResult/MusicResult，
#     上层只认能力接口，不感知厂商差异。
def _mk_seedream(conn, store):
    from .providers.image.seedream import SeedreamProvider
    return SeedreamProvider(conn, store)


def _mk_nano_banana(conn, store):
    from .providers.image.nano_banana import NanoBananaProvider
    return NanoBananaProvider(conn, store)


def _mk_wan(conn, store):
    from .providers.image.wan import WanImageProvider
    return WanImageProvider(conn, store)


def _mk_seedance(conn, store):
    from .providers.video.seedance import SeedanceProvider
    return SeedanceProvider(conn, store)


def _mk_veo(conn, store):
    from .providers.video.veo import VeoVideoProvider
    return VeoVideoProvider(conn, store)


def _mk_seedtts(conn, store):
    from .providers.tts.seedtts import SeedTTSProvider
    return SeedTTSProvider(conn, store)


def _mk_doubao(conn, store):
    from .providers.tts.doubao import DoubaoTTSProvider
    return DoubaoTTSProvider(conn, store)


def _mk_minimax(conn, store):
    from .providers.tts.minimax import MiniMaxTTSProvider
    return MiniMaxTTSProvider(conn, store)


def _mk_minimax_video(conn, store):
    from .providers.video.minimax import MiniMaxVideoProvider
    return MiniMaxVideoProvider(conn, store)


def _mk_minimax_image(conn, store):
    from .providers.image.minimax import MiniMaxImageProvider
    return MiniMaxImageProvider(conn, store)


def _mk_agent_image(conn, store):
    from .providers.image.agent import AgentImageProvider
    return AgentImageProvider(conn, store)


def _mk_minimax_music(conn, store):
    from .providers.music.minimax import MiniMaxMusicProvider
    return MiniMaxMusicProvider(conn, store)


def _mk_local_music(conn, store):
    from .providers.music.local import LocalMusicProvider
    return LocalMusicProvider(store)


def _mk_volc_lipsync(conn, store):
    from .providers.lipsync.volc import VolcLipsyncProvider
    return VolcLipsyncProvider(conn, store)


def _mk_elevenlabs(conn, store):
    # 未配置 ELEVENLABS_API_KEY → 自动降级本地免版权音乐库（零成本）
    if not store.secret(conn.get("api_key_env", "ELEVENLABS_API_KEY"), required=False):
        return _mk_local_music(conn, store)
    from .providers.music.elevenlabs import ElevenLabsMusicProvider
    return ElevenLabsMusicProvider(conn, store)


_ADAPTERS = {
    ("image", "seedream"): _mk_seedream,
    ("image", "nano_banana"): _mk_nano_banana,
    ("image", "wan"): _mk_wan,
    ("image", "minimax_image"): _mk_minimax_image,
    ("image", "agent"): _mk_agent_image,
    ("video", "seedance"): _mk_seedance,
    ("video", "veo"): _mk_veo,
    ("video", "minimax_video"): _mk_minimax_video,
    ("tts", "seedtts"): _mk_seedtts,
    ("tts", "doubao"): _mk_doubao,
    ("tts", "minimax"): _mk_minimax,
    ("music", "elevenlabs"): _mk_elevenlabs,
    ("lipsync", "volc_lipsync"): _mk_volc_lipsync,
    ("music", "minimax_music"): _mk_minimax_music,
    ("music", "local"): _mk_local_music,
}


def image_route(store) -> dict:
    """生图路由三级解析（命中即停）——「谁来出图」的单一判定点。

    ① **models 页显式激活**（网页配置中心 / `config activate --capability image`，
      判据是覆盖层**原文**里写了 defaults.providers.image——用户亲手选的 API
      无条件生效，不管驱动引擎的是什么工具）；
    ② **agent 原生生图声明**：env `KINEMA_AGENT_IMAGEGEN=1`。带原生生图能力的
      agent（Codex imagegen 之类）开工时自声明，见 AGENTS.md。引擎**绝不嗅探**
      「你是谁」——嗅探没有稳定契约，声明可审计、可关闭、不随实现漂移；
    ③ **默认链** defaults.providers.image（此时才轮到常规 key 检测）。

    返回 `{"provider": 别名, "source": "explicit"|"agent"|"default"}`。
    消费点：ModelRouter.resolve 选别名，setup --check / doctor 报生效路由。"""
    from . import config_overlay as _ovl
    explicit = _ovl.explicit_default("image", explicit=getattr(store, "_explicit", None))
    if explicit:
        return {"provider": explicit, "source": "explicit"}
    if os.environ.get("KINEMA_AGENT_IMAGEGEN") == "1":
        return {"provider": "agent", "source": "agent"}
    return {"provider": store.default_provider("image"), "source": "default"}


class ModelRouter:
    """按 capability + profile 解析出「已配置好的 provider 实例 + 参数」。

    解析链（全局统一配置的落点）：
      shots[].profile / CLI --profile（选 profile）
        → profile.<capability>.provider（偏离项，可选）
        → defaults.providers.<capability>（全局默认别名，总入口）
        → provider 别名的连接段（base_url/model/密钥/impl）
        → _ADAPTERS[(capability, impl)] 工厂实例化 → 统一 Result 返回。"""

    def __init__(self, store: ConfigStore, *, force_mock: bool = False):
        self.store = store
        self.force_mock = force_mock

    def resolve(self, capability: str, profile_name: str | None):
        """返回 (provider_instance, params)。params 含 style_prefix/voice 等风格参数。"""
        entry = dict(self.store.profile(profile_name).get(capability) or {})
        route = image_route(self.store) if (capability == "image"
                                            and not self.force_mock) else None
        if self.force_mock:
            provider_name = "mock"
        elif route is not None and route["source"] == "agent":
            # agent 声明凌驾 profile 偏离项：profile 里的 provider 是画风作者的
            # API 偏好，不是用户本人的选择——只有 ①级显式激活压得过声明。
            provider_name = "agent"
        else:
            provider_name = (
                entry.get("provider")                          # profile 偏离项优先
                or self.store.default_provider(capability))    # 全局默认别名兜底
        if not provider_name:
            raise ConfigError(
                f"profile '{profile_name or self.store.default_profile}' 未定义 {capability} "
                f"provider，且 defaults.providers 缺 {capability} 默认——"
                "在 config/models.yaml 的 defaults.providers 配一行即可全局生效")
        conn = {} if provider_name == "mock" else self.store.provider_conn(provider_name)
        prov = self._build(capability, provider_name, conn)
        # 提示词语言偏好：国产模型中文（默认），海外模型英文（providers 段 prompt_lang: en）
        prov.prompt_lang = conn.get("prompt_lang", "zh")
        return prov, entry

    def resolve_named(self, capability: str, alias: str):
        """按 providers 别名**运行时点名**取 provider 实例（绕过 profile 链）。

        `gen-video --video-provider seedance-2.5` 的落点：缺省恒走 defaults 里的
        mini 主力，点名才上 2.5/veo——大模型绝不静默升级。两道校验都在
        解析层就喊出来（而不是发出请求后才 4xx）：别名必须在 providers 段登记；
        `kind` 必须匹配能力（`--video-provider seedream` 这种指错能力的直接拒）。
        mock 态照旧强制 mock：离线彩排不该因点名而联网。"""
        if self.force_mock:
            prov = self._mock(capability)
            prov.prompt_lang = "zh"
            return prov
        conn = self.store.provider_conn(alias)   # 未知别名在这里抛 ConfigError
        kind = conn.get("kind")
        if kind and kind != capability:
            raise ConfigError(
                f"provider '{alias}' 是 {kind} 能力，不能用于 {capability}——"
                f"可用的 {capability} 别名见 config/models.yaml providers 段")
        prov = self._build(capability, alias, conn)
        prov.prompt_lang = conn.get("prompt_lang", "zh")
        return prov

    def _build(self, capability, name, conn):
        """工厂：别名 → impl → 适配器类。conn.impl 缺省 = 别名本身。"""
        if name == "mock":
            return self._mock(capability)
        status = conn.get("status", "ready")
        if status != "ready":
            raise ProviderError(
                f"{capability} provider '{name}' 已登记但尚未接入（status={status}）。\n"
                f"请在 config/models.yaml 换用 ready 的 provider，或用 --mock 离线跑。")
        impl = conn.get("impl", name)
        builder = _ADAPTERS.get((capability, impl))
        if builder is None:
            known = sorted(i for c, i in _ADAPTERS if c == capability)
            raise ProviderError(
                f"{capability} provider '{name}'（impl={impl}）没有对应适配器"
                f"（已实现: {', '.join(known)}）。\n"
                f"· 同厂新模型/新端点：在 providers 段加别名并设 impl: <已有适配器>，零代码；\n"
                f"· 新厂商：providers/{capability}/ 写适配器类 + models.py 的 _ADAPTERS 登记一行。")
        return builder(conn, self.store)

    @staticmethod
    def _mock(capability):
        if capability == "image":
            from .providers.image.mock import MockImageProvider
            return MockImageProvider()
        if capability == "tts":
            from .providers.tts.mock import MockTTSProvider
            return MockTTSProvider()
        if capability == "music":
            from .providers.music.mock import MockMusicProvider
            return MockMusicProvider()
        if capability == "video":
            from .providers.video.mock import MockVideoProvider
            return MockVideoProvider()
        if capability == "lipsync":
            from .providers.lipsync.mock import MockLipsyncProvider
            return MockLipsyncProvider()
        raise ConfigError(f"mock 不支持能力: {capability}")


def resolve_video(router, store, data: dict | None, profile_name: str | None, *,
                  override: str | None = None):
    """视频 provider 的三级解析：本次点名 > 章节文档 `video_provider` > profile 链。
    返回 (provider, profile 的 video 参数块)。gen-video 报价与真发、`agent explain`
    与 Studio 标注都从这一处取，分叉就是报价与账单对不上。"""
    alias = str(override or (data or {}).get("video_provider") or "").strip()
    if alias:
        return (router.resolve_named("video", alias),
                dict(store.profile(profile_name).get("video") or {}))
    prov, params = router.resolve("video", profile_name)
    return prov, dict(params or {})
