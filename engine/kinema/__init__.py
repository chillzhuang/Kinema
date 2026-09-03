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

"""kinema 执行引擎。

三层架构中的第 ② 层（本地 Python 执行引擎）。承接 Skill 指挥层交付的
project.json，逐阶段调用能力层 provider（图像/TTS/音乐）并用 FFmpeg 合成
竖屏成片。每步落 checkpoint、幂等、失败可单独重生某一镜。

范围：主题→成片（静图烧录与图生视频两大形态），不含发布；上传由用户自行完成。
"""

__version__ = "0.1.0"
