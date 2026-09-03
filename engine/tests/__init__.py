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

"""引擎稳定纯函数模块的单元测试包（stdlib unittest，离线、无 ffmpeg 依赖）。"""

import os

# 用例一律不看本机的模型配置覆盖层。开发者在网页上把视频激活成别的 provider 之后，
# 21 处 `ConfigStore.load()` 夹具就会全部拿到被覆盖的值，默认链相关的用例开始
# 报「解析到 X 而不是 seedance」——而这批失败**只在这台机器上出现**，且与本次改动
# 看不出任何关系，是最难查的一类。
# 值必须是显式哨兵而不是空串：覆盖层的发现顺序里空值只是「本级没指定」，会继续
# 往下找，磁盘上真实存在的那份照样被读进去，这道闸就成了永不触发的摆设。
os.environ.setdefault("KINEMA_CONFIG_OVERLAY", "off")
