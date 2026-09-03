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

"""引擎统一异常类型。"""


class KinemaError(Exception):
    """所有引擎错误的基类。CLI 捕获后打印友好信息并以非零码退出。"""


class ConfigError(KinemaError):
    """配置缺失/无效（如缺少 API key）。"""


class ProviderError(KinemaError):
    """能力层 provider 调用失败。

    `code` = 厂商返回的结构化错误码（如 `InputImageSensitiveContentDetected.PrivacyInformation`），
    供调用方区分「重跑可能成功」与「重跑必然同样失败」两类失败。用它而不用错误文案
    的子串匹配：文案随厂商与版本变化，匹配会静默失效。
    """

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class FFmpegError(KinemaError):
    """FFmpeg/ffprobe 执行失败。"""


class ProjectError(KinemaError):
    """project.json 结构缺失或阶段前置未满足。"""


class DocumentCorruptError(KinemaError):
    """文档文件存在但无法解析（半写/非法 JSON/顶层非对象）。

    必须与「文档不存在」严格区分：损坏文档按不存在处理会让同 ID 的新建
    或陈旧副本整份写回，把一次读失败放大为覆盖用户数据。"""

    def __init__(self, path, reason: str):
        self.path = str(path)
        self.reason = reason
        super().__init__(
            f"文档已损坏，拒绝按「不存在」处理: {path}（{reason}）。"
            "请人工修复或移走该文件后重试。")
