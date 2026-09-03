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

#!/usr/bin/env python3
"""一键下载音频库两套起始资产：BGM 背景音乐（bgm/）+ 转场音效（sfx/）。

用法：python music/download.py
**本脚本只拉「免署名 · 免费商用」的 CC0 / 公共领域资源**——署名类（CC-BY）与付费/
条款收紧类源刻意不做下载逻辑：库里混进一首 CC-BY，引擎按情绪确定性选曲时随时会挑中它，
成片就默认背着"必须在简介署名"的义务，而这件事在发布那一刻没人会再去核对。

两个来源（合计 **BGM 103 首 + 音效 18 枚**）：
- BGM 95 首 · **FreePD（freepd.com）CC0**——站方原文「100% Free Music - Free for
  Commercial Use, Free Of Royalties, Free Of Attribution, Creative Commons 0」。
  该站 2025 年已关站（首页只剩闭站公告、/music/*.mp3 全 404），故改从 **Internet Archive
  Wayback 存档**取同一批文件：CC0 是不可撤回授权，站关了授权不变。
- BGM 8 首 + 音效 18 枚 · **freesound CC0**（拉公开预览流 hq mp3；音效按实测能量窗口
  裁剪 + 尾部淡出 + **峰值归一到 -3 dBFS**，统一 44.1kHz 立体声 wav——此步需要系统
  ffmpeg，本仓库唯一硬依赖）。音效分两层：转场三色板 + 扫掠扩展（whoosh/riser/boom/
  swish/deep/glitch/shimmer）＋内容型打点（pop/ding/page/paper/impact/slash/heartbeat/
  wind/magic/clock/camera，给解说·说书·漫剧战斗·仙侠术法·剪纸拼贴各备一枚）。

音频文件不入库（.gitignore），克隆后跑本脚本即可重建；也可把自己的正规授权音频按
config/audio.yaml 的目录/文件名放置（来源登记 ATTRIBUTION.md）。
单文件失败不致命：BGM 缺曲退化合成氛围床、音效缺文件回落 ffmpeg 合成。
"""
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(90)   # 单个源挂住不拖死整个脚本（逐文件失败不致命）
UA = "kinema/1.0 (local media library bootstrap; +https://bladex.cn)"

# ── BGM · FreePD CC0（免署名可商用）────────────────────────────────
# freepd.com 已关站，取 Wayback 存档的同一份文件。值 = (站上原文件名, 登记说明)
WAYBACK = "https://web.archive.org/web/2024id_/https://freepd.com/music/"
BGM_PD = {
    # ── calm：舒缓/治愈——语录、绘本、说书、情感戏 ──
    "bgm/calm/landras-dream.mp3": ("Landra's Dream.mp3", "Landra's Dream · 1:29 · romantic · Jason Shaw"),
    "bgm/calm/champ-de-tournesol.mp3": ("Champ de tournesol.mp3", "Champ de tournesol · 1:58 · romantic · Komiku"),
    "bgm/calm/la-citadelle.mp3": ("La Citadelle.mp3", "La Citadelle · 2:42 · romantic · Komiku"),
    "bgm/calm/shining-stars.mp3": ("Shining Stars.mp3", "Shining Stars · 2:20 · romantic · Rafael Krux"),
    "bgm/calm/nostalgic-piano.mp3": ("Nostalgic Piano.mp3", "Nostalgic Piano · 3:16 · romantic · Rafael Krux"),
    "bgm/calm/lovely-piano.mp3": ("Lovely Piano Song.mp3", "Lovely Piano Song · 1:36 · romantic · Rafael Krux"),
    "bgm/calm/study-and-relax.mp3": ("Study and Relax.mp3", "Study and Relax · 3:43 · misc · Kevin MacLeod"),
    "bgm/calm/wisdom-in-the-sun.mp3": ("Wisdom in the Sun.mp3", "Wisdom in the Sun · 2:35 · misc · Kevin MacLeod"),
    "bgm/calm/connecting-rainbows.mp3": ("Connecting Rainbows.mp3", "Connecting Rainbows · 1:56 · world · Kevin MacLeod"),
    "bgm/calm/a-waltz-for-naseem.mp3": ("A Waltz For Naseem.mp3", "A Waltz For Naseem · 3:37 · misc · dogsounds"),
    # ── upbeat：欢快/活力——口播、解说、榜单、日常 ──
    "bgm/upbeat/funky-energy-loop.mp3": ("Funky Energy Loop.mp3", "Funky Energy Loop · 3:22 · scoring · Kevin MacLeod"),
    "bgm/upbeat/driving-concern.mp3": ("Driving Concern.mp3", "Driving Concern · 3:20 · scoring · Kevin MacLeod"),
    "bgm/upbeat/goodnightmare.mp3": ("Goodnightmare.mp3", "Goodnightmare · 4:00 · electronic · Kevin MacLeod"),
    "bgm/upbeat/arpent.mp3": ("Arpent.mp3", "Arpent · 2:42 · electronic · Kevin MacLeod"),
    "bgm/upbeat/inspiration.mp3": ("Inspiration.mp3", "Inspiration · 2:18 · upbeat · Rafael Krux"),
    "bgm/upbeat/advertime.mp3": ("Advertime.mp3", "Advertime · 2:14 · upbeat · Rafael Krux"),
    "bgm/upbeat/river-meditation.mp3": ("River Meditation.mp3", "River Meditation · 2:47 · misc · Jason Shaw"),
    "bgm/upbeat/fresh-focus.mp3": ("Fresh Focus.mp3", "Fresh Focus · 2:04 · upbeat · Kevin MacLeod"),
    "bgm/upbeat/slice-of-life.mp3": ("Slice of Life.mp3", "Slice of Life · 2:21 · scoring · Bryan Teoh"),
    "bgm/upbeat/meditating-beat.mp3": ("Meditating Beat.mp3", "Meditating Beat · 2:37 · electronic · Kevin MacLeod"),
    "bgm/upbeat/city-sunshine.mp3": ("City Sunshine.mp3", "City Sunshine · 3:05 · upbeat · Kevin MacLeod"),
    # ── cinematic：电影感/史诗——漫剧剧情、战斗、游戏叙事 ──
    "bgm/cinematic/heroic-adventure.mp3": ("Heroic Adventure.mp3", "Heroic Adventure · 2:22 · epic · Rafael Krux"),
    "bgm/cinematic/epic-boss-battle.mp3": ("Epic Boss Battle.mp3", "Epic Boss Battle · 2:50 · epic · Rafael Krux"),
    "bgm/cinematic/night-attack.mp3": ("Night Attack.mp3", "Night Attack · 2:33 · epic · Rafael Krux"),
    "bgm/cinematic/go-on-without-me.mp3": ("Go On Without Me.mp3", "Go On Without Me · 3:15 · epic · Bryan Teoh"),
    "bgm/cinematic/lonely-mountain.mp3": ("Lonely Mountain.mp3", "Lonely Mountain · 3:10 · epic · Rafael Krux"),
    "bgm/cinematic/travelers-notebook.mp3": ("Travelers Notebook.mp3", "Travelers Notebook · 2:03 · scoring · Rafael Krux"),
    "bgm/cinematic/after-the-end.mp3": ("After the End.mp3", "After the End · 1:38 · scoring · Rafael Krux"),
    "bgm/cinematic/guerilla-tactics.mp3": ("Guerilla Tactics.mp3", "Guerilla Tactics · 1:49 · scoring · Rafael Krux"),
    "bgm/cinematic/ice-and-snow.mp3": ("Ice and Snow.mp3", "Ice and Snow · 2:21 · scoring · Rafael Krux"),
    "bgm/cinematic/motions.mp3": ("Motions.mp3", "Motions · 1:56 · upbeat · Rafael Krux"),
    "bgm/cinematic/think-about-it.mp3": ("Think About It.mp3", "Think About It · 2:00 · epic · Bryan Teoh"),
    # ── ambient：氛围/空灵——环境陪伴、白噪音、悬疑 ──
    "bgm/ambient/forest-night.mp3": ("Forest Night.mp3", "Forest Night · 2:22 · misc · Phase Shift"),
    "bgm/ambient/infinite-wonder.mp3": ("Infinite Wonder.mp3", "Infinite Wonder · 3:10 · misc · Kevin MacLeod"),
    "bgm/ambient/mana-two.mp3": ("Mana Two - Part 1.mp3", "Mana Two - Part 1 · 3:54 · misc · Kevin MacLeod"),
    "bgm/ambient/infinite-peace.mp3": ("Infinite Peace.mp3", "Infinite Peace · 1:16 · misc · Kevin MacLeod"),
    "bgm/ambient/ancient-winds.mp3": ("Ancient Winds.mp3", "Ancient Winds · 58:54 · misc · Kevin MacLeod"),
    "bgm/ambient/alien-atmosphere.mp3": ("Alien Spaceship Atmosphere.mp3", "Alien Spaceship Atmosphere · 2:04 · horror · Kevin MacLeod"),
    "bgm/ambient/mind-chaos.mp3": ("Mind Chaos.mp3", "Mind Chaos · 3:04 · horror · Kevin MacLeod"),
    "bgm/ambient/green-house-night.mp3": ("Midnight in the Green House.mp3", "Midnight in the Green House · 3:32 · world · Kevin MacLeod"),
    "bgm/ambient/blippy-trance.mp3": ("Blippy Trance.mp3", "Blippy Trance · 2:00 · misc · Kevin MacLeod"),
    "bgm/ambient/kalimba-relax.mp3": ("Kalimba Relaxation Music.mp3", "Kalimba Relaxation Music · 7:08 · misc · Kevin MacLeod"),
    # ── calm 扩充（FreePD 舒缓/治愈）──
    "bgm/calm/lucky-break.mp3": ("Lucky Break.mp3", "Lucky Break · 4:45 · romantic · Bryan Teoh"),
    "bgm/calm/isolation-waltz.mp3": ("Isolation Waltz.mp3", "Isolation Waltz · 3:25 · romantic · Bryan Teoh"),
    "bgm/calm/pond.mp3": ("Pond.mp3", "Pond · 2:32 · romantic · Rafael Krux"),
    "bgm/calm/romantic-inspiration.mp3": ("Romantic Inspiration.mp3", "Romantic Inspiration · 2:30 · romantic · Rafael Krux"),
    "bgm/calm/amazing-grace.mp3": ("Amazing Grace.mp3", "Amazing Grace · 1:54 · romantic · Kevin MacLeod"),
    "bgm/calm/bass-meant-jazz.mp3": ("Bass Meant Jazz.mp3", "Bass Meant Jazz · 4:40 · misc · Kevin MacLeod"),
    "bgm/calm/groovin.mp3": ("Groovin.mp3", "Groovin · 4:00 · misc · Brian Boyko"),
    "bgm/calm/the-celebrated-minuet-for-piano.mp3": ("The Celebrated Minuet for Piano.mp3", "The Celebrated Minuet for Piano · 3:37 · misc · Rafael Krux"),
    "bgm/calm/fake-it-til-you-fake-it.mp3": ("Fake It Til You Fake It.mp3", "Fake It Til You Fake It · 2:54 · misc · Kevin MacLeod"),
    "bgm/calm/martini-sunset.mp3": ("Martini Sunset.mp3", "Martini Sunset · 2:54 · misc · Anonymous"),
    "bgm/calm/painting-room.mp3": ("Painting Room.mp3", "Painting Room · 1:46 · misc · Kevin MacLeod"),
    "bgm/calm/painful-disorientation.mp3": ("Painful Disorientation.mp3", "Painful Disorientation · 1:31 · misc · Kevin MacLeod"),
    # ── upbeat 扩充（FreePD 欢快/活力）──
    "bgm/upbeat/take-the-ride.mp3": ("Take the Ride.mp3", "Take the Ride · 4:22 · comedy · Bryan Teoh"),
    "bgm/upbeat/alls-fair-in-love.mp3": ("Alls Fair In Love.mp3", "Alls Fair In Love · 3:59 · comedy · Bryan Teoh"),
    "bgm/upbeat/joeys-song.mp3": ("Joey's Song.mp3", "Joey's Song · 3:27 · comedy · Kevin MacLeod"),
    "bgm/upbeat/horns.mp3": ("Horns.mp3", "Horns · 3:24 · comedy · Kevin MacLeod"),
    "bgm/upbeat/the-entertainer.mp3": ("The Entertainer.mp3", "The Entertainer · 3:13 · comedy · Kevin MacLeod"),
    "bgm/upbeat/night-in-the-castle.mp3": ("Night in the Castle.mp3", "Night in the Castle · 3:09 · comedy · Kevin MacLeod"),
    "bgm/upbeat/jungle-mission.mp3": ("Jungle Mission.mp3", "Jungle Mission · 3:06 · comedy · Rafael Krux"),
    "bgm/upbeat/maple-leaf-rag.mp3": ("Maple Leaf Rag.mp3", "Maple Leaf Rag · 2:59 · comedy · Kevin MacLeod"),
    "bgm/upbeat/frogs-legs-rag.mp3": ("Frogs Legs Rag.mp3", "Frogs Legs Rag · 2:50 · comedy · Kevin MacLeod"),
    "bgm/upbeat/fensters-explanation.mp3": ("Fenster's Explanation.mp3", "Fenster's Explanation · 2:49 · comedy · Kevin MacLeod"),
    "bgm/upbeat/spring-chicken.mp3": ("Spring Chicken.mp3", "Spring Chicken · 2:47 · comedy · Bryan Teoh"),
    "bgm/upbeat/funshine.mp3": ("Funshine.mp3", "Funshine · 2:45 · upbeat · Kevin MacLeod"),
    "bgm/upbeat/my-giant-bunny-friend.mp3": ("My Giant Bunny Friend.mp3", "My Giant Bunny Friend · 2:45 · comedy · Bryan Teoh"),
    "bgm/upbeat/managing-mischief.mp3": ("Managing Mischief.mp3", "Managing Mischief · 2:44 · comedy · Bryan Teoh"),
    # ── cinematic 扩充（FreePD 史诗/电影感）──
    "bgm/cinematic/night-vigil.mp3": ("Night Vigil.mp3", "Night Vigil · 4:28 · epic · Kevin MacLeod"),
    "bgm/cinematic/shenzhen-nightlife.mp3": ("Shenzhen Nightlife.mp3", "Shenzhen Nightlife · 4:23 · world · Kevin MacLeod"),
    "bgm/cinematic/palm-and-soul.mp3": ("Palm and Soul.mp3", "Palm and Soul · 4:00 · world · Kevin MacLeod"),
    "bgm/cinematic/bavarian-seascape.mp3": ("Bavarian Seascape.mp3", "Bavarian Seascape · 3:51 · world · Anonymous"),
    "bgm/cinematic/breaking-bollywood.mp3": ("Breaking Bollywood.mp3", "Breaking Bollywood · 3:45 · world · Kevin MacLeod"),
    "bgm/cinematic/del-rio-bravo.mp3": ("Del Rio Bravo.mp3", "Del Rio Bravo · 3:24 · world · Kevin MacLeod"),
    "bgm/cinematic/cumbish.mp3": ("Cumbish.mp3", "Cumbish · 3:12 · world · Kevin MacLeod"),
    "bgm/cinematic/modern-island-jam.mp3": ("Modern Island Jam.mp3", "Modern Island Jam · 3:08 · world · Kevin MacLeod"),
    "bgm/cinematic/honor-bound.mp3": ("Honor Bound.mp3", "Honor Bound · 2:51 · epic · Bryan Teoh"),
    "bgm/cinematic/hillbilly-swing.mp3": ("Hillbilly Swing.mp3", "Hillbilly Swing · 2:47 · world · Kevin MacLeod"),
    "bgm/cinematic/experimental-test-subject.mp3": ("Experimental Test Subject.mp3", "Experimental Test Subject · 2:44 · world · Kevin MacLeod"),
    "bgm/cinematic/kings-trailer.mp3": ("Kings Trailer.mp3", "Kings Trailer · 2:43 · epic · Rafael Krux"),
    "bgm/cinematic/epic-blockbuster-2.mp3": ("Epic Blockbuster 2.mp3", "Epic Blockbuster 2 · 2:40 · epic · Rafael Krux"),
    "bgm/cinematic/desert-conflict.mp3": ("Desert Conflict.mp3", "Desert Conflict · 2:39 · world · Rafael Krux"),
    "bgm/cinematic/bollywood-groove.mp3": ("Bollywood Groove.mp3", "Bollywood Groove · 2:30 · world · Kevin MacLeod"),
    "bgm/cinematic/aquatic-city-vanished.mp3": ("Aquatic City Vanished.mp3", "Aquatic City Vanished · 2:29 · world · Bryan Teoh"),
    # ── ambient 扩充（FreePD 氛围/空灵）──
    "bgm/ambient/3-am-west-end.mp3": ("3 am West End.mp3", "3 am West End · 4:51 · electronic · statusq"),
    "bgm/ambient/alien-invasion.mp3": ("Alien Invasion.mp3", "Alien Invasion · 3:05 · horror · Rafael Krux"),
    "bgm/ambient/beat-one.mp3": ("Beat One.mp3", "Beat One · 3:00 · electronic · Kevin MacLeod"),
    "bgm/ambient/mysterious-lights.mp3": ("Mysterious Lights.mp3", "Mysterious Lights · 2:59 · horror · Bryan Teoh"),
    "bgm/ambient/horroriffic.mp3": ("Horroriffic.mp3", "Horroriffic · 2:48 · horror · Kevin MacLeod"),
    "bgm/ambient/satin-danger.mp3": ("Satin Danger.mp3", "Satin Danger · 2:44 · horror · Kevin MacLeod"),
    "bgm/ambient/wind-of-the-rainforest.mp3": ("Wind of the Rainforest.mp3", "Wind of the Rainforest · 58:54 · misc · Kevin MacLeod"),
    "bgm/ambient/abstract-anxiety.mp3": ("Abstract Anxiety.mp3", "Abstract Anxiety · 4:29 · misc · Kevin MacLeod"),
    "bgm/ambient/bleu.mp3": ("Bleu.mp3", "Bleu · 3:31 · misc · Komiku"),
    "bgm/ambient/witch-waltz.mp3": ("Witch Waltz.mp3", "Witch Waltz · 3:06 · misc · Kevin MacLeod"),
    "bgm/ambient/spec-ops.mp3": ("Spec Ops.mp3", "Spec Ops · 2:17 · misc · Rafael Krux"),
}

# ── BGM · freesound CC0（免署名可商用；与 FreePD 曲目同目录混放，选曲逻辑一致）──
BGM_CC0 = {
    "bgm/calm/piano-ambience.mp3": {
        "url": "https://cdn.freesound.org/previews/810/810857_2520418-hq.mp3",
        "src": "freesound #810857 「Piano Ambience 82bpm」by CVLTIV8R (CC0)"},
    "bgm/calm/calming-piano.mp3": {
        "url": "https://cdn.freesound.org/previews/679/679738_13228046-hq.mp3",
        "src": "freesound #679738 「Calming Piano Loop 60bpm」by Seth_Makes_Sounds (CC0)"},
    "bgm/upbeat/uplifting.mp3": {
        "url": "https://cdn.freesound.org/previews/670/670819_13228046-hq.mp3",
        "src": "freesound #670819 「Free Uplifting Music」by Seth_Makes_Sounds (CC0)"},
    "bgm/upbeat/shady-groove.mp3": {
        "url": "https://cdn.freesound.org/previews/789/789664_11042058-hq.mp3",
        "src": "freesound #789664 「SHADY」by MadGravityStudio (CC0)"},
    "bgm/cinematic/adventure-theme.mp3": {
        "url": "https://cdn.freesound.org/previews/716/716478_3968818-hq.mp3",
        "src": "freesound #716478 「Emotional appealing adventure soundtrack」by MusicByMisterbates (CC0)"},
    "bgm/cinematic/wonders.mp3": {
        "url": "https://cdn.freesound.org/previews/814/814843_13228046-hq.mp3",
        "src": "freesound #814843 「Wonders [cinematic background music]」by Seth_Makes_Sounds (CC0)"},
    "bgm/ambient/ambient-drone.mp3": {
        "url": "https://cdn.freesound.org/previews/799/799355_2520418-hq.mp3",
        "src": "freesound #799355 「Ambient Drone」by CVLTIV8R (CC0)"},
    "bgm/ambient/scifi-drone.mp3": {
        "url": "https://cdn.freesound.org/previews/534/534018_3968707-hq.mp3",
        "src": "freesound #534018 「Sci-fi Ambient Drone」by LookIMadeAThing (CC0)"},
}

# ── 音效 · freesound CC0（sfx/...，与 config/audio.yaml sfx 段文件名一致）──
# trim=(起点s, 时长s) 为实测能量窗口
SFX_SOURCES = {
    "sfx/transitions/whoosh.wav": {
        "url": "https://cdn.freesound.org/previews/648/648538_8698658-hq.mp3",
        "src": "freesound #648538 「Cinematic Woosh SFX-001」by AudioPapkin (CC0)",
        "trim": ("0.95", "1.8"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/riser.wav": {
        "url": "https://cdn.freesound.org/previews/715/715353_8698658-hq.mp3",
        "src": "freesound #715353 「Riser Hit sfx 062」by AudioPapkin (CC0)",
        "trim": ("7.6", "2.3"), "fade_in": 0.15, "peak": -3.0,   # 取蓄势末段收进落点
    },
    "sfx/transitions/boom.wav": {
        "url": "https://cdn.freesound.org/previews/430/430977_8698658-hq.mp3",
        "src": "freesound #430977 「Big impact」by AudioPapkin (CC0)",
        "trim": ("0", "2.2"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/swish.wav": {
        "url": "https://cdn.freesound.org/previews/425/425706_760420-hq.mp3",
        "src": "freesound #425706 「Woosh_Medium_Short_01」by moogy73 (CC0)",
        "trim": ("0", "0.6"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/deep.wav": {
        "url": "https://cdn.freesound.org/previews/649/649445_8698658-hq.mp3",
        "src": "freesound #649445 「Cinematic Woosh SFX-015」by AudioPapkin (CC0)",
        "trim": ("0.5", "2.0"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/glitch.wav": {
        "url": "https://cdn.freesound.org/previews/401/401123_7725148-hq.mp3",
        "src": "freesound #401123 「Glitch_02」by s-cheremisinov (CC0)",
        "trim": ("0", "0.7"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/shimmer.wav": {
        "url": "https://cdn.freesound.org/previews/351/351408_4067257-hq.mp3",
        "src": "freesound #351408 「GLEAM-GLOW-SFX-CHIME」by newagesoup (CC0)",
        "trim": ("1.40", "2.0"), "fade_in": 0.05, "peak": -3.0,
    },
    # ── 内容型打点音效：解说/漫剧/说书各取所需（peak= 峰值归一到该电平，
    #    跨源素材实测横跨 30 dB，不归一就是「有的音效震耳有的听不见」）──
    "sfx/transitions/pop.wav": {
        "url": "https://cdn.freesound.org/previews/333/333428_4682121-hq.mp3",
        "src": "freesound #333428 「UI Series: Hollow pop」by LittleRobotSoundFactory (CC0)",
        "trim": ("0", "0.55"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/ding.wav": {
        "url": "https://cdn.freesound.org/previews/452/452371_1844073-hq.mp3",
        "src": "freesound #452371 「Small Bell」by kwahmah_02 (CC0)",
        "trim": ("0", "2.1"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/page.wav": {
        "url": "https://cdn.freesound.org/previews/136/136778_2207512-hq.mp3",
        "src": "freesound #136778 「Page Turn」by mccormick_iain (CC0)",
        "trim": ("0.10", "1.6"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/paper.wav": {
        "url": "https://cdn.freesound.org/previews/804/804974_6863341-hq.mp3",
        "src": "freesound #804974 「Paper Tear」by Nightflame (CC0)",
        "trim": ("0", "1.4"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/impact.wav": {
        "url": "https://cdn.freesound.org/previews/559/559387_10825267-hq.mp3",
        "src": "freesound #559387 「Cinematic Impact」by Rosa_Orz (CC0)",
        "trim": ("0.14", "2.4"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/slash.wav": {
        "url": "https://cdn.freesound.org/previews/370/370204_4682356-hq.mp3",
        "src": "freesound #370204 「samurai slash」by 8bitmarch (CC0)",
        "trim": ("0", "0.7"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/heartbeat.wav": {
        "url": "https://cdn.freesound.org/previews/22/22416_120830-hq.mp3",
        "src": "freesound #22416 「Four Heartbeats」by BeatSmith (CC0)",
        "trim": ("0", "2.2"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/wind.wav": {
        "url": "https://cdn.freesound.org/previews/146/146932_1274078-hq.mp3",
        "src": "freesound #146932 「Wind Gust」by kangaroovindaloo (CC0)",
        "trim": ("0.70", "2.4"), "fade_in": 0.25, "peak": -3.0,
    },
    "sfx/transitions/magic.wav": {
        "url": "https://cdn.freesound.org/previews/360/360830_1431924-hq.mp3",
        "src": "freesound #360830 「Fantasy SFX 003」by rudmer_rotteveel (CC0)",
        "trim": ("0", "2.4"), "fade_in": 0.0, "peak": -3.0,
    },
    "sfx/transitions/clock.wav": {
        "url": "https://cdn.freesound.org/previews/321/321084_1196020-hq.mp3",
        "src": "freesound #321084 「Clock Ticking」by Tomlija (CC0)",
        "trim": ("0", "2.4"), "fade_in": 0.10, "peak": -3.0,
    },
    "sfx/transitions/camera.wav": {
        "url": "https://cdn.freesound.org/previews/338/338220_5450487-hq.mp3",
        "src": "freesound #338220 「Camera shutter analogue film SLR」by martinseeberg (CC0)",
        "trim": ("0", "0.72"), "fade_in": 0.0, "peak": -3.0,
    },
}

# 手动补充源：授权达标但站方禁止脚本抓取，只能人工下载（收尾提示原样打印给用户）
MANUAL_HINT = """
── 想再加曲子/音效？这两个源授权达标，但只能手动下 ───────────────
  Pixabay  https://pixabay.com/music/   ·   https://pixabay.com/sound-effects/
    ✓ 免费商用   ✓ 无需署名   ✓ 可改编、可作背景音随成片分发
    ✕ 不得把素材原样单独再分发（我们当 BGM/音效嵌进成片，不受此限）
    ⚠ 其服务条款禁止 robots/scraping，站点对脚本请求直接返回 403 ——
      故本脚本刻意不自动抓；请在浏览器里挑喜欢的点 Download，然后复制到：
        背景音乐 → music/bgm/{calm|upbeat|cinematic|ambient}/<自定名>.mp3
        音  效  → music/sfx/transitions/<注册表里的键>.wav（键见 config/audio.yaml）
      丢进目录即刻生效（BGM 无需登记任何配置，引擎按情绪目录自动选曲），
      顺手在 music/ATTRIBUTION.md 补一行来源即可。

  Mixkit   https://mixkit.co/free-stock-music/   ·   https://mixkit.co/free-sound-effects/
    ✓ 免费商用   ✓ 无需署名   ✓ 可作背景音嵌入并随成片分发（Mixkit Free License）
    ✕ 非 CC0——是可撤回的非独占许可，且不得把素材原样再分发
    ⚠ 其服务条款 9(10) 明文禁止「use scripts or bots to mass download Items」，
      9(4) 禁止以「stock or inventory basis」向第三方提供 —— 本脚本正是脚本批量下载、
      bgm/ 与 sfx/ 正是按情绪与类别编目的 inventory，故**绝不写进脚本**。
      手挑下载后落位规则同上。

  收音频进库前请照四条过一遍：① 免费商用 ② 免署名 ③ 允许当背景音嵌入并随成片分发
  ④ 不限平台。缺一条就别进库——引擎按情绪确定性选曲，混进一首就是哪一集随机踩雷。
"""


def _get(url: str, dst: Path, *, tries: int = 3, pause: float = 1.2) -> None:
    """抓一个文件到 dst。带指数退避重试——Wayback 对连拉几十个文件会限流
    （表现为连接直接失败而非 404），不退避就会把「限流」误判成「存档里没有」。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req) as r, open(dst, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            dst.unlink(missing_ok=True)
            if i + 1 < tries:
                time.sleep(pause * (2 ** i))
    raise last if last else RuntimeError("下载失败")


def _fetch_bgm_pd(root: Path) -> tuple[int, int]:
    ok = total = 0
    for rel, (name, credit) in BGM_PD.items():
        total += 1
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size > 300000:
            print(f"已存在  {rel}")
            ok += 1
            continue
        print(f"下载    {rel} ← FreePD「{credit}」")
        try:
            _get(WAYBACK + urllib.parse.quote(name), dst)
            if dst.stat().st_size < 300000:
                dst.unlink()
                print("  ✗ 无效（体积过小），跳过")
            else:
                ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 失败: {e}")
        time.sleep(0.8)                       # 对存档站客气些，别把自己拉进限流
    return ok, total


def _fetch_bgm_cc0(root: Path) -> tuple[int, int]:
    ok = total = 0
    for rel, meta in BGM_CC0.items():
        total += 1
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size > 200000:
            print(f"已存在  {rel}")
            ok += 1
            continue
        print(f"下载    {rel} ← {meta['src']}")
        try:
            _get(meta["url"], dst)
            if dst.stat().st_size < 200000:
                dst.unlink()
                print("  ✗ 无效，跳过")
            else:
                ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 失败: {e}")
    return ok, total


def _peak_db(path: Path, *, ss: str, t: str, chain: str) -> float | None:
    """量「过完整条滤镜链之后」的峰值（只测不改）。

    必须连 chain 一起量，不能只量原始文件：① 跨源音效原始峰值实测横跨 30 dB；
    ② **单声道源经 `-ac 2` 上混会掉 3 dB**（ffmpeg 按功率守恒 ×1/√2）——只量原文件时
    翻页/撕纸这类单声道素材会比目标整整低 3 dB，肉眼看不出、耳朵一听就是"这两个音效偏轻"。
    """
    err = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", ss, "-t", t, "-i", str(path),
         "-af", f"{chain},volumedetect", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    m = re.search(r"max_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


def _fetch_sfx(root: Path) -> tuple[int, int]:
    ok = total = 0
    for rel, meta in SFX_SOURCES.items():
        total += 1
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size > 40000:
            print(f"已存在  {rel}")
            ok += 1
            continue
        tmp = dst.with_suffix(".dl.mp3")
        print(f"下载    {rel} ← {meta['src']}")
        try:
            _get(meta["url"], tmp)
            start, dur = meta["trim"]
            fades = f"afade=t=out:st={float(dur) - 0.15:.2f}:d=0.15"
            if meta["fade_in"]:
                fades = f"afade=t=in:st=0:d={meta['fade_in']:.2f}," + fades
            chain = f"aresample=44100,aformat=channel_layouts=stereo,{fades}"
            if meta.get("peak") is not None:               # 峰值归一（只测再推，确定性）
                mx = _peak_db(tmp, ss=start, t=dur, chain=chain)
                if mx is not None:
                    chain += f",volume={meta['peak'] - mx:+.1f}dB"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", start, "-t", dur, "-i", str(tmp),
                 "-af", chain, "-ar", "44100", "-ac", "2", str(dst)],
                check=True)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 失败: {e}\n    出路：kinema sfx gen --kind <键> --yes（AI 生成）或手动放置")
        finally:
            tmp.unlink(missing_ok=True)
    return ok, total


def main():
    root = Path(__file__).resolve().parent
    print("── BGM · FreePD 存档（CC0，免署名可商用）──")
    pok, ptotal = _fetch_bgm_pd(root)
    print("\n── BGM · freesound（CC0，免署名可商用）──")
    cok, ctotal = _fetch_bgm_cc0(root)
    print("\n── 转场音效 · freesound（CC0，免署名可商用）──")
    sok, stotal = _fetch_sfx(root)
    print(f"\n完成：BGM {pok + cok}/{ptotal + ctotal} 首 · 音效 {sok}/{stotal} 枚。库位于 {root}"
          f"\n全库 CC0 / 公共领域——免署名、可商用、可修改（授权登记见 ATTRIBUTION.md）"
          f"\n（`python3 -m kinema sfx list` 查看音效就位状态）")
    print(MANUAL_HINT)


if __name__ == "__main__":
    main()
