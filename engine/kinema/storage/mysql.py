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

"""MySQL 后端 —— 数据库做持久层与恢复源，本地 JSON 是工作副本（write-back）。

表模型（规范化，五张表；逻辑外键+索引，不加物理 FOREIGN KEY——外键在应用层解决）：
  kn_project        项目（系列）
  kn_asset          资产：角色/道具/武器/场景设定（含设定图路径），挂在项目下
  kn_chapter        章节（一条视频）
  kn_chapter_asset  章节 ↔ 资产 关联表（章节继承/引用哪些资产）
  kn_shot           分镜明细：每行一镜——说明(景别/运镜/说话人)、文案(旁白/字幕)、
                    提示词(图像/运动)、产物路径(图片/配音/视频片段，含逐比例)

设计要点：
  · 雪花 ID 单主键（BIGINT，见 snowflake.py）；业务标识退为 code 列并加唯一键；
    全表全列 COMMENT；InnoDB + utf8mb4_unicode_ci。
  · 文档 + 规范化双轨：项目/章节完整 JSON 存各自 data 列（保真、可整体恢复）；
    保存时自动**分解**出资产/分镜/关联行（派生行按唯一键 upsert + 清理失效行，
    重复保存主键不变）——SQL 可直接按行查资产与分镜，无需解析 JSON。
  · 媒体只存路径：图/音/视频文件永远在磁盘工作区，库里绝不存二进制。
  · 双写：save 时 本地文件 + 库 同步写（引擎按文件路径渲染，链路不变）。
  · 读取协调：本地文件是工作副本——read 时按 updated_at 与文件 mtime「新者赢」
    （见 `_row_newer`）：库行明显更新则刷新本地副本，否则以文件为准并上行入库；
    文件缺失 → 从库恢复（rehydrate）。删档/换机时数据库即恢复源。
  · 懒迁移：开启 mysql 后首次访问，本地已有而库中没有的项目自动登记入库。
  · 依赖 PyMySQL（pip install -e "engine[mysql]"），连接断线自动 ping 重连。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..errors import ConfigError
from .base import Storage, chapter_meta
from .local import LocalStorage
from .snowflake import next_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {p}project (
  id          BIGINT       NOT NULL                COMMENT '主键（雪花ID）',
  code        VARCHAR(64)  NOT NULL                COMMENT '项目业务标识（工作区目录名/slug，全局唯一）',
  title       VARCHAR(255) DEFAULT NULL            COMMENT '项目标题',
  theme       VARCHAR(500) DEFAULT NULL            COMMENT '项目主题',
  profile     VARCHAR(32)  DEFAULT NULL            COMMENT '默认风格档（config/models.yaml profiles）',
  skill       VARCHAR(32)  DEFAULT NULL            COMMENT '绑定指挥层 skill（kinema/skills.py，如 kn-anime；缺省由 profile 派生，报项目名/编号即可让 AI 查得该调哪个 skill）',
  template    VARCHAR(32)  DEFAULT NULL            COMMENT '平台规格模板名（config/templates.yaml，如 douyin_manju）',
  aspect      VARCHAR(16)  DEFAULT NULL            COMMENT '默认画面比例（9:16 / 16:9 / 1:1）',
  cover       VARCHAR(768) DEFAULT NULL            COMMENT '系列封面成品路径（3:4 竖版主视觉，工作区相对路径，媒体不入库）',
  platform    VARCHAR(255) DEFAULT NULL            COMMENT '目标平台列表（JSON 数组，如 ["douyin"]）',
  status      VARCHAR(16)  DEFAULT NULL            COMMENT '项目状态：active=进行中 archived=已归档',
  is_deleted  TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '逻辑删除：0=正常 1=已删（唯一删除语义·数据完整保留可恢复·清单类查询过滤 is_deleted=0）',
  data        LONGTEXT     NOT NULL                COMMENT '项目完整文档（project.json 原文：总体设计/角色预设/道具/章节索引）',
  created_at  DATETIME     DEFAULT NULL            COMMENT '创建时间（取自文档 created_at）',
  updated_at  DATETIME     DEFAULT NULL            COMMENT '更新时间（取自文档 updated_at）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_project_code (code),
  KEY idx_project_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 项目（系列）：总体设计+角色预设+章节索引，媒体只存路径';
---
CREATE TABLE IF NOT EXISTS {p}asset (
  id           BIGINT       NOT NULL               COMMENT '主键（雪花ID）',
  project_id   BIGINT       NOT NULL               COMMENT '所属项目主键（{p}project.id，逻辑外键）',
  project_code VARCHAR(64)  NOT NULL               COMMENT '所属项目业务标识（冗余，便于直查）',
  kind         VARCHAR(16)  NOT NULL               COMMENT '资产类型：character=角色 prop=道具 weapon=武器 scene=场景',
  name         VARCHAR(64)  NOT NULL               COMMENT '资产名称（项目内同类型唯一；全局场景图固定为 main，具名取景地用实名）',
  role         VARCHAR(255) DEFAULT NULL           COMMENT '角色定位（仅角色：主角/师傅/反派…；小说层可为富文本定位，实测 87 字）',
  description  TEXT         DEFAULT NULL           COMMENT '外貌/描述（角色=appearance，道具/场景=desc/场景描述块）',
  outfit       VARCHAR(255) DEFAULT NULL           COMMENT '服装（仅角色，用于设定图生成）',
  hair         VARCHAR(255) DEFAULT NULL           COMMENT '发型（仅角色，用于设定图生成）',
  weapon       VARCHAR(255) DEFAULT NULL           COMMENT '武器/持物（仅角色，用于设定图生成）',
  voice        VARCHAR(64)  DEFAULT NULL           COMMENT '在用音色引用（仅角色：config/voices.yaml 别名，或定制音色 custom:<档案号>）',
  voice_type   VARCHAR(128) DEFAULT NULL           COMMENT '解析后的音色ID（官方音色如 ICL_uranus_*_tob；定制=custom:<档案号>）',
  voice_cast   VARCHAR(32)  DEFAULT NULL           COMMENT '在用音色档案号（{p}voice_cast.cast_id；空=手工指派、未入档）',
  clip_path    VARCHAR(768) DEFAULT NULL           COMMENT '在用档案的音频路径（Studio 试听与 TTS 锚定参考音同一条）',
  sheet_path   VARCHAR(768) DEFAULT NULL           COMMENT '设定图路径（gen-refs 产物：角色设定图/场景图/道具图）',
  ref_image    VARCHAR(768) DEFAULT NULL           COMMENT '外部参考图路径（用户提供的形象参考）',
  origin_project VARCHAR(64) DEFAULT NULL          COMMENT '来源项目（跨项目资产复用 assets import 的血缘出处）',
  data         VARCHAR(2048) DEFAULT NULL          COMMENT '资产条目原始 JSON（保真）',
  created_at   DATETIME     DEFAULT NULL           COMMENT '创建时间（首次入库）',
  updated_at   DATETIME     DEFAULT NULL           COMMENT '更新时间（最近一次同步）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_asset_project_kind_name (project_id, kind, name),
  KEY idx_asset_project_code (project_code),
  KEY idx_asset_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 资产：角色/道具/武器/场景设定（一致性根基），设定图只存路径';
---
CREATE TABLE IF NOT EXISTS {p}chapter (
  id           BIGINT       NOT NULL               COMMENT '主键（雪花ID）',
  project_id   BIGINT       NOT NULL               COMMENT '所属项目主键（{p}project.id，逻辑外键）',
  project_code VARCHAR(64)  NOT NULL               COMMENT '所属项目业务标识（冗余，便于直查）',
  code         VARCHAR(64)  NOT NULL               COMMENT '章节业务标识（项目内唯一，如 ch01）',
  title        VARCHAR(255) DEFAULT NULL           COMMENT '章节标题',
  status       VARCHAR(16)  DEFAULT NULL           COMMENT '章节状态：draft=草稿 scripted=已编剧 rendered=已渲染',
  motion       VARCHAR(16)  DEFAULT NULL           COMMENT '运动模式：kenburns=静图运镜 dubbed=对口型 native=原生音画',
  shots        INT          DEFAULT 0              COMMENT '分镜数量',
  duration     DECIMAL(8,2) DEFAULT NULL           COMMENT '成片时长（秒，分镜时长合计）',
  video_path   VARCHAR(768) DEFAULT NULL           COMMENT '主成片文件路径（媒体不入库，只存路径）',
  animatic_path VARCHAR(768) DEFAULT NULL          COMMENT '全片样片路径（草稿两段式 Ken Burns animatic，主比例）',
  animatic_state VARCHAR(16) DEFAULT NULL          COMMENT '样片审阅状态：wfa=待审 done=通过 retake=重做（章节级节奏审）',
  cover        VARCHAR(768) DEFAULT NULL           COMMENT '章节封面路径（与系列主视觉同风格，副标题=第 N 集，工作区相对路径）',
  cost         VARCHAR(512) DEFAULT NULL           COMMENT '云 API 成本明细（JSON：image/tts/music/video）',
  data         LONGTEXT     NOT NULL               COMMENT '章节完整文档（章节 json 原文，引擎可直接渲染）',
  created_at   DATETIME     DEFAULT NULL           COMMENT '创建时间（取自文档 chapter.created_at）',
  updated_at   DATETIME     DEFAULT NULL           COMMENT '更新时间（最近一次入库）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_chapter_project_code (project_id, code),
  KEY idx_chapter_project_code (project_code),
  KEY idx_chapter_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 章节（一条视频）：渲染状态/产物路径/成本；分镜明细见 {p}shot';
---
CREATE TABLE IF NOT EXISTS {p}chapter_asset (
  id           BIGINT      NOT NULL                COMMENT '主键（雪花ID）',
  chapter_id   BIGINT      NOT NULL                COMMENT '章节主键（{p}chapter.id，逻辑外键）',
  asset_id     BIGINT      NOT NULL                COMMENT '资产主键（{p}asset.id，逻辑外键）',
  project_id   BIGINT      NOT NULL                COMMENT '所属项目主键（冗余，便于按项目清理/统计）',
  created_at   DATETIME    DEFAULT NULL            COMMENT '创建时间（首次关联）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_chapter_asset (chapter_id, asset_id),
  KEY idx_ca_asset (asset_id),
  KEY idx_ca_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 章节↔资产关联：本章节继承/引用了哪些角色/道具/场景设定';
---
CREATE TABLE IF NOT EXISTS {p}shot (
  id            BIGINT       NOT NULL              COMMENT '主键（雪花ID）',
  chapter_id    BIGINT       NOT NULL              COMMENT '所属章节主键（{p}chapter.id，逻辑外键）',
  project_id    BIGINT       NOT NULL              COMMENT '所属项目主键（冗余，便于跨章节统计）',
  project_code  VARCHAR(64)  NOT NULL              COMMENT '项目业务标识（冗余，便于直查）',
  chapter_code  VARCHAR(64)  NOT NULL              COMMENT '章节业务标识（冗余，便于直查）',
  shot_no       INT          NOT NULL              COMMENT '镜号（章节内从 1 递增）',
  speaker       VARCHAR(64)  DEFAULT NULL          COMMENT '说话人（角色名，同时驱动对话框名牌与音色解析）',
  voice         VARCHAR(64)  DEFAULT NULL          COMMENT '本镜音色（显式覆盖或按角色音色表解析的别名）',
  framing       VARCHAR(32)  DEFAULT NULL          COMMENT '景别（远/中/近/特写/过肩…）',
  camera        VARCHAR(64)  DEFAULT NULL          COMMENT '运镜（缓推/拉远/左移/固定…）',
  duration      DECIMAL(6,2) DEFAULT NULL          COMMENT '本镜时长（秒，TTS/片段实际时长回填）',
  narration     TEXT         DEFAULT NULL          COMMENT '旁白/台词文案（中文，TTS 与中文字幕真源）',
  narration_en  TEXT         DEFAULT NULL          COMMENT '旁白/台词英文对译（en/both 字幕文本位；subtitle_lang=both 时分镜必填）',
  caption       VARCHAR(500) DEFAULT NULL          COMMENT '屏幕字幕（可与旁白不同，简短）',
  image_prompt  TEXT         DEFAULT NULL          COMMENT '图像提示词·中文主（只写本镜动作/姿态/机位，风格与设定由引擎前置）',
  image_prompt_en TEXT       DEFAULT NULL          COMMENT '图像提示词·英文辅（海外模型 prompt_lang=en 时选用，缺失回退中文）',
  video_prompt  TEXT         DEFAULT NULL          COMMENT '视频提示词·中文主（只写运动/运镜，图生视频用）',
  video_prompt_en TEXT       DEFAULT NULL          COMMENT '视频提示词·英文辅（海外视频模型选用）',
  negative_prompt VARCHAR(500) DEFAULT NULL        COMMENT '负面约束（国产模型编译为"避免出现：…"肯定式约束句）',
  status        VARCHAR(16)  DEFAULT NULL          COMMENT '生成状态：done=完成 failed=失败 空=未生成',
  omitted       TINYINT(1)   NOT NULL DEFAULT 0    COMMENT '是否弃用（omt：整镜不进时间轴/字幕/成片）',
  review_image  VARCHAR(16)  DEFAULT NULL          COMMENT '分镜图审阅状态：todo/wip/wfa=待审/retake=重做/done=通过锁定',
  review_audio  VARCHAR(16)  DEFAULT NULL          COMMENT '配音审阅状态（同上枚举）',
  review_clip   VARCHAR(16)  DEFAULT NULL          COMMENT '动态片段审阅状态（同上枚举）',
  review_note   VARCHAR(500) DEFAULT NULL          COMMENT '最近重做意见汇总（结构化反馈，供提示词修正）',
  image_path    VARCHAR(768) DEFAULT NULL          COMMENT '分镜图路径（主比例）',
  images        VARCHAR(2048) DEFAULT NULL         COMMENT '逐比例分镜图路径（JSON：{"9:16":path,…}）',
  image_candidates VARCHAR(2048) DEFAULT NULL      COMMENT '宫格候选图路径（JSON 数组，人点选后定稿上画布）',
  picked_no     INT          DEFAULT NULL          COMMENT '已点选的候选编号（1 起；空=未点选或非候选模式）',
  stale_refs    VARCHAR(500) DEFAULT NULL          COMMENT '过期引用（JSON 数组：已变化的设定图名，血缘追踪）',
  audio_path    VARCHAR(768) DEFAULT NULL          COMMENT '本镜配音音频路径（TTS 产物）',
  clip_path     VARCHAR(768) DEFAULT NULL          COMMENT '图生视频片段路径（主比例，dubbed/native 产物）',
  clips         VARCHAR(2048) DEFAULT NULL         COMMENT '逐比例片段路径（JSON：{"9:16":path,…}）',
  characters    VARCHAR(500) DEFAULT NULL          COMMENT '本镜出场角色名列表（JSON 数组；空=全部角色）',
  props         VARCHAR(500) DEFAULT NULL          COMMENT '本镜出场道具名列表（JSON 数组）',
  rank_no       INT          DEFAULT NULL          COMMENT '榜单序号（ranking 风格用）',
  title         VARCHAR(255) DEFAULT NULL          COMMENT '条目标题（ranking）/ 金句署名标题（quote）',
  attribution   VARCHAR(255) DEFAULT NULL          COMMENT '署名（quote 风格用）',
  data          LONGTEXT     DEFAULT NULL          COMMENT '分镜条目原始 JSON（保真）',
  created_at    DATETIME     DEFAULT NULL          COMMENT '创建时间（首次入库）',
  updated_at    DATETIME     DEFAULT NULL          COMMENT '更新时间（最近一次同步）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_shot_chapter_no (chapter_id, shot_no),
  KEY idx_shot_project (project_id),
  KEY idx_shot_codes (project_code, chapter_code),
  KEY idx_shot_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 分镜明细：每行一镜——说明/文案/提示词/审阅状态/图片/配音/视频片段路径';
---
CREATE TABLE IF NOT EXISTS {p}shot_version (
  id          BIGINT        NOT NULL               COMMENT '主键（雪花ID）',
  shot_id     BIGINT        NOT NULL               COMMENT '所属分镜主键（{p}shot.id，逻辑外键）',
  chapter_id  BIGINT        NOT NULL               COMMENT '所属章节主键（冗余，便于清理/统计）',
  project_id  BIGINT        NOT NULL               COMMENT '所属项目主键（冗余）',
  stage       VARCHAR(16)   NOT NULL               COMMENT '产物阶段：image=分镜图 audio=配音 clip=动态片段',
  version_no  INT           NOT NULL               COMMENT '版本号（归档序；当前版=最大归档号+1，不在本表）',
  files       VARCHAR(2048) DEFAULT NULL           COMMENT '归档文件路径（JSON：{main:…, 逐比例:…}，媒体不入库）',
  reason      VARCHAR(500)  DEFAULT NULL           COMMENT '归档原因（retake 意见 / force / rollback 谱系）',
  params      VARCHAR(2048) DEFAULT NULL           COMMENT '该版生成参数快照（prompt/seed/provider JSON）',
  created_at  DATETIME      DEFAULT NULL           COMMENT '归档时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sv_shot_stage_no (shot_id, stage, version_no),
  KEY idx_sv_chapter (chapter_id),
  KEY idx_sv_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 产物版本栈：重生成不覆盖，归档谱系可回滚可审计';
---
CREATE TABLE IF NOT EXISTS {p}comment (
  id          BIGINT        NOT NULL               COMMENT '主键（雪花ID）',
  doc_id      VARCHAR(32)   NOT NULL               COMMENT '文档内评论ID（章节 JSON comments[].id，同步锚点）',
  project_id  BIGINT        NOT NULL               COMMENT '所属项目主键（冗余，便于清理/统计）',
  chapter_id  BIGINT        NOT NULL               COMMENT '所属章节主键（逻辑外键）',
  shot_id     BIGINT        DEFAULT NULL           COMMENT '所属分镜主键（空=章节级/成片评论）',
  shot_no     INT           DEFAULT NULL           COMMENT '镜号（冗余，便于直查）',
  stage       VARCHAR(16)   DEFAULT NULL           COMMENT '评论对象：image=分镜图 audio=配音 clip=片段 final=成片',
  content     VARCHAR(1000) NOT NULL               COMMENT '评论内容',
  anchor_x    DECIMAL(6,4)  DEFAULT NULL           COMMENT '像素锚 X（0~1 相对坐标，图像评论钉点）',
  anchor_y    DECIMAL(6,4)  DEFAULT NULL           COMMENT '像素锚 Y（0~1 相对坐标）',
  anchor_time DECIMAL(8,2)  DEFAULT NULL           COMMENT '时间锚（秒，视频/音频评论钉帧）',
  created_at  DATETIME      DEFAULT NULL           COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_comment_doc (chapter_id, doc_id),
  KEY idx_comment_shot (shot_id),
  KEY idx_comment_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 锚定评论：帧/像素/时间锚定到具体产物';
---
-- voice_bank.casts 的派生行：项目 upsert 时全量同步（_sync_voice_casts），引擎读侧不用本表，
-- 供库内查询音色档案台账。
CREATE TABLE IF NOT EXISTS {p}voice_cast (
  id           BIGINT       NOT NULL               COMMENT '主键（雪花ID）',
  project_id   BIGINT       NOT NULL               COMMENT '所属项目主键（{p}project.id，逻辑外键）',
  project_code VARCHAR(64)  NOT NULL               COMMENT '所属项目业务标识（冗余，便于直查）',
  cast_id      VARCHAR(32)  NOT NULL               COMMENT '音色档案号（项目内序列 vc_NNNN，永不复用）',
  owner        VARCHAR(64)  NOT NULL               COMMENT '归属实体（角色名 / 旁白）',
  mode         VARCHAR(16)  NOT NULL               COMMENT '来路：preset=模版（官方固定音色） custom=定制（按声线描述生成）',
  voice_type   VARCHAR(128) DEFAULT NULL           COMMENT '音色标识：模版=官方音色ID；定制=custom:<档案号>（按档案唯一，分镜配音留痕据此溯源到具体哪一把）',
  alias        VARCHAR(64)  DEFAULT NULL           COMMENT '音色别名（仅模版：config/voices.yaml 里的人读名）',
  prompt       VARCHAR(1024) DEFAULT NULL          COMMENT '声线描述（仅定制：造出这把声音的原话，音频剧本起草取材同一份）',
  clip_path    VARCHAR(768) DEFAULT NULL           COMMENT '档案音频路径（不可变副本；定制音色全片以它作参考音合成）',
  created_at   DATETIME     DEFAULT NULL           COMMENT '立档时间',
  used_at      DATETIME     DEFAULT NULL           COMMENT '最近一次被启用的时间',
  data         VARCHAR(2048) DEFAULT NULL          COMMENT '档案条目原始 JSON（保真）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_voice_cast (project_id, cast_id),
  KEY idx_voice_cast_owner (project_code, owner)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 音色档案：一个实体用过的每一把声音各一条（可回听·可换回·删除前查引用）';
---
CREATE TABLE IF NOT EXISTS {p}setting (
  id         BIGINT      NOT NULL                COMMENT '主键（雪花ID）',
  scope      VARCHAR(32) NOT NULL                COMMENT '配置域：models=模型连接与激活项',
  name       VARCHAR(64) NOT NULL                COMMENT '配置名（同域内唯一；models 域固定 overlay）',
  data       LONGTEXT    NOT NULL                COMMENT '配置文档全文（JSON）。**绝不含密钥值**——库行随备份与多机同步走，密钥只留本机 gitignored 文件；分两份文件存正是为了这份可以整份上传、不需要任何逐字段过滤',
  updated_at DATETIME    DEFAULT NULL            COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_setting_scope_name (scope, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 工作区级配置（不属于任何项目）：模型覆盖层的跨机同步层，密钥不入库';
"""


def _dt(iso: str | None) -> str | None:
    """ISO 时间串 → MySQL DATETIME 可接受格式。"""
    return iso.replace("T", " ")[:19] if iso else None


def _sync_at() -> str:
    """参与「新者赢」判据的 updated_at 列取值：写入方客户端的本地墙钟。

    该列要与本地文件 `st_mtime` 比大小，而 PyMySQL 交回的 naive DATETIME 恒按
    客户端时区解释。写 `NOW()` 取的是 MySQL 会话时钟（容器缺省 UTC），两者差一个
    时区偏移即足以让判据整体倒向一侧，把较新的那份覆盖掉。
    派生行的 `NOW()` 只是登记时刻、不参与判据，不在此列。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _jtext(v, limit: int = 2000) -> str | None:
    """对象 → JSON 文本列（截断保护，派生列仅供查询，保真靠 data 列）。"""
    if v in (None, [], {}, ""):
        return None
    s = json.dumps(v, ensure_ascii=False)
    return s[:limit]


class MySQLStorage(LocalStorage):
    """继承 LocalStorage 复用文件镜像读写；库操作叠加其上。"""

    backend = "mysql"

    def __init__(self, root: Path, cfg: dict):
        super().__init__(root)
        self.cfg = cfg
        self.prefix = cfg.get("table_prefix", "kn_")
        self._conn = None
        # Studio 是 ThreadingHTTPServer（每请求一线程），仪表盘一次加载并发多个
        # /api/*；PyMySQL 单连接不支持并发查询——RLock 串行化全部连接访问，
        # 否则随机出 "Packet sequence number wrong" 协议错乱。
        import threading
        self._lock = threading.RLock()

    # ---- 连接 / 建表 ----
    def _db(self):
        try:
            import pymysql
        except ImportError as e:
            raise ConfigError(
                "backend=mysql 需要 PyMySQL：pip install -e \"engine[mysql]\" "
                "或 pip install PyMySQL") from e
        with self._lock:
            if self._conn is None:
                try:
                    self._conn = pymysql.connect(
                        host=self.cfg.get("host", "127.0.0.1"),
                        port=int(self.cfg.get("port", 3306)),
                        user=self.cfg.get("user", "root"),
                        password=str(self.cfg.get("password") or ""),
                        database=self.cfg.get("database", "kinema"),
                        charset=self.cfg.get("charset", "utf8mb4"),
                        autocommit=True)
                except Exception as e:  # noqa: BLE001
                    raise ConfigError(
                        f"MySQL 连接失败（{self.cfg.get('user')}@{self.cfg.get('host')}:"
                        f"{self.cfg.get('port')}/{self.cfg.get('database')}）：{e}\n"
                        "请检查 config/storage.yaml 与密码（KINEMA_MYSQL_PASSWORD / "
                        "config/secrets.yaml），或改回 backend: local。") from e
                self.ensure_schema()
            else:
                self._conn.ping(reconnect=True)
            return self._conn

    # 存量库升级：CREATE TABLE IF NOT EXISTS 不给已有表加新列，
    # 此处登记「后续版本新增的列」，连接时对缺失列执行 ALTER（买家升级零手工）。
    _MIGRATE_COLUMNS = {
        "project": {
            "skill": "VARCHAR(32) DEFAULT NULL COMMENT '绑定指挥层 skill（kinema/skills.py，如 kn-anime；缺省由 profile 派生，报项目名/编号即可让 AI 查得该调哪个 skill）' AFTER profile",
            "template": "VARCHAR(32) DEFAULT NULL COMMENT '平台规格模板名（config/templates.yaml，如 douyin_manju）' AFTER profile",
            "cover": "VARCHAR(768) DEFAULT NULL COMMENT '系列封面成品路径（3:4 竖版主视觉，工作区相对路径，媒体不入库）' AFTER aspect",
            "is_deleted": "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除：0=正常 1=已删（唯一删除语义·数据完整保留可恢复·清单类查询过滤 is_deleted=0）' AFTER status",
        },
        "chapter": {
            "animatic_path": "VARCHAR(768) DEFAULT NULL COMMENT '全片样片路径（草稿两段式 Ken Burns animatic，主比例）' AFTER video_path",
            "animatic_state": "VARCHAR(16) DEFAULT NULL COMMENT '样片审阅状态：wfa=待审 done=通过 retake=重做（章节级节奏审）' AFTER animatic_path",
            "cover": "VARCHAR(768) DEFAULT NULL COMMENT '章节封面路径（与系列主视觉同风格，副标题=第 N 集，工作区相对路径）' AFTER animatic_state",
        },
        "shot": {
            "narration_en": "TEXT DEFAULT NULL COMMENT '旁白/台词英文对译（en/both 字幕文本位；subtitle_lang=both 时分镜必填）' AFTER narration",
            "image_candidates": "VARCHAR(2048) DEFAULT NULL COMMENT '宫格候选图路径（JSON 数组，人点选后定稿上画布）' AFTER images",
            "picked_no": "INT DEFAULT NULL COMMENT '已点选的候选编号（1 起；空=未点选或非候选模式）' AFTER image_candidates",
            "stale_refs": "VARCHAR(500) DEFAULT NULL COMMENT '过期引用（JSON 数组：已变化的设定图名，血缘追踪）' AFTER picked_no",
        },
        "asset": {
            "origin_project": "VARCHAR(64) DEFAULT NULL COMMENT '来源项目（跨项目资产复用 assets import 的血缘出处）' AFTER ref_image",
            "voice_cast": "VARCHAR(32) DEFAULT NULL COMMENT '在用音色档案号（voice_cast.cast_id；空=手工指派、未入档）' AFTER voice_type",
            "clip_path": "VARCHAR(768) DEFAULT NULL COMMENT '在用档案的音频路径（Studio 试听与 TTS 锚定参考音同一条）' AFTER voice_cast",
        },
    }

    # 存量库列加宽：登记「后续版本放宽的列」——(目标字符数, 完整 DDL)。
    # 判据=现有 CHARACTER_MAXIMUM_LENGTH < 目标才 ALTER MODIFY（幂等，重复连库零 ALTER）。
    # 首例 asset.role：按「主角/反派」短标签设计的 VARCHAR(64)，被小说层养出的
    # 富文本定位（87 字）当场撑爆（DataError 1406）；截断写入=库里静默丢数据，
    # 文件侧还全须全尾，两边从此对不上——所以正解是加宽列，不是截断。
    _MIGRATE_WIDEN = {
        "asset": {
            "role": (255, "VARCHAR(255) DEFAULT NULL COMMENT '角色定位"
                          "（仅角色：主角/师傅/反派…；小说层可为富文本定位，实测 87 字）'"),
        },
    }

    def ensure_schema(self) -> None:
        # 用 replace 而非 str.format：COMMENT 里的 JSON 示例含花括号，format 会误解析
        with self._conn.cursor() as cur:
            for stmt in _SCHEMA.replace("{p}", self.prefix).split("---"):
                cur.execute(stmt)
            # 缺列迁移（幂等：INFORMATION_SCHEMA 查现有列，缺的补 ALTER）
            for table, cols in self._MIGRATE_COLUMNS.items():
                cur.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                    (f"{self.prefix}{table}",))
                have = {r[0] for r in cur.fetchall()}
                for col, ddl in cols.items():
                    if col not in have:
                        cur.execute(f"ALTER TABLE {self.prefix}{table}"
                                    f" ADD COLUMN {col} {ddl}")
            # 列加宽迁移（幂等：短于目标才 MODIFY；长度未知——TEXT 列 NULL 或
            # 旧版单列返回——一律不动，宁可漏宽不误改）
            for table, cols in self._MIGRATE_WIDEN.items():
                cur.execute(
                    "SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH"
                    " FROM INFORMATION_SCHEMA.COLUMNS"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                    (f"{self.prefix}{table}",))
                length = {r[0]: (r[1] if len(r) > 1 else None)
                          for r in cur.fetchall()}
                for col, (chars, ddl) in cols.items():
                    cur_len = length.get(col)
                    if cur_len is not None and int(cur_len) < int(chars):
                        cur.execute(f"ALTER TABLE {self.prefix}{table}"
                                    f" MODIFY COLUMN {col} {ddl}")

    def _exec(self, sql: str, args=None, *, fetch: str | None = None):
        with self._lock, self._db().cursor() as cur:
            cur.execute(sql, args or ())
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None

    # ========================================================================
    # upsert（唯一键命中则更新，主键雪花 ID 保持首插值不变）+ 派生行同步
    # ========================================================================
    def _upsert_project(self, code: str, data: dict) -> int:
        doc = json.dumps(data, ensure_ascii=False)
        self._exec(
            f"INSERT INTO {self.prefix}project"
            " (id, code, title, theme, profile, skill, template, aspect, cover, platform,"
            "  status, is_deleted, data, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE title=VALUES(title), theme=VALUES(theme),"
            " profile=VALUES(profile), skill=VALUES(skill), template=VALUES(template),"
            " aspect=VALUES(aspect), cover=VALUES(cover), platform=VALUES(platform),"
            " status=VALUES(status), is_deleted=VALUES(is_deleted),"
            " data=VALUES(data), updated_at=VALUES(updated_at)",
            (next_id(), code, data.get("title"), data.get("theme"),
             data.get("profile"), data.get("skill"),
             (data.get("template") or {}).get("name"),
             data.get("aspect"), (data.get("cover") or {}).get("primary"),
             _jtext(data.get("platform"), 255), data.get("status"),
             int(data.get("is_deleted") or 0),
             # 文档没带 updated_at（占位行、旧文档）时取同步时钟，否则该行永远
             # 参与不了「新者赢」判据，冲突时恒由文件覆盖库
             doc, _dt(data.get("created_at")), _dt(data.get("updated_at")) or _sync_at()))
        pid = self._exec(f"SELECT id FROM {self.prefix}project WHERE code=%s",
                         (code,), fetch="one")[0]
        self._sync_assets(pid, code, data)
        self._sync_voice_casts(pid, code, data)
        return pid

    def _project_db_id(self, code: str) -> int:
        """项目业务标识 → 主键（雪花ID）。库中缺行时先从本地文档补插，再兜底占位行。"""
        row = self._exec(f"SELECT id FROM {self.prefix}project WHERE code=%s",
                         (code,), fetch="one")
        if row:
            return row[0]
        local = LocalStorage.load_project(self, code)
        return self._upsert_project(code, local if local is not None else {"id": code})

    # ---- 资产分解：characters / props(道具|武器) / scene → kn_asset ----
    @staticmethod
    def _asset_rows(data: dict) -> list[dict]:
        from .. import voicebank
        rows = []
        for c in data.get("characters") or []:
            if not c.get("name"):
                continue
            cast = voicebank.cast_for_ref(data, c["name"], c.get("voice")) or {}
            rows.append({"kind": "character", "name": c["name"], "role": c.get("role"),
                         "description": c.get("appearance"), "outfit": c.get("outfit"),
                         "hair": c.get("hair"), "weapon": c.get("weapon"),
                         "voice": c.get("voice"), "voice_type": cast.get("voice_type"),
                         "voice_cast": cast.get("id"),
                         "clip_path": cast.get("clip"),
                         "sheet_path": c.get("sheet"),
                         "ref_image": c.get("ref_image"),
                         "origin_project": (c.get("origin") or {}).get("project"),
                         "data": c})
        for p in data.get("props") or []:
            if not p.get("name"):
                continue
            kind = "weapon" if p.get("kind") == "weapon" else "prop"
            rows.append({"kind": kind, "name": p["name"], "role": None,
                         "description": p.get("desc"), "outfit": None, "hair": None,
                         "weapon": None, "voice": None, "voice_type": None,
                         "voice_cast": None, "clip_path": None,
                         "sheet_path": p.get("sheet"), "ref_image": None,
                         "origin_project": (p.get("origin") or {}).get("project"),
                         "data": p})
        for sc in data.get("scenes") or []:            # 具名取景地（与 props 段同构）
            if not sc.get("name"):
                continue
            rows.append({"kind": "scene", "name": sc["name"], "role": None,
                         "description": sc.get("desc"), "outfit": None, "hair": None,
                         "weapon": None, "voice": None, "voice_type": None,
                         "voice_cast": None, "clip_path": None,
                         "sheet_path": sc.get("sheet"), "ref_image": None,
                         "origin_project": (sc.get("origin") or {}).get("project"),
                         "data": sc})
        scene, scene_ref = (data.get("scene") or "").strip(), data.get("scene_ref")
        if scene or scene_ref:
            rows.append({"kind": "scene", "name": "main", "role": None,
                         "description": scene or None, "outfit": None, "hair": None,
                         "weapon": None, "voice": None, "voice_type": None,
                         "voice_cast": None, "clip_path": None,
                         "sheet_path": scene_ref, "ref_image": None,
                         "origin_project": (data.get("scene_origin") or {}).get("project"),
                         "data": {"scene": scene, "scene_ref": scene_ref}})
        return rows

    def _sync_assets(self, project_id: int, code: str, data: dict) -> None:
        rows = self._asset_rows(data)
        keep = []
        for r in rows:
            self._exec(
                f"INSERT INTO {self.prefix}asset"
                " (id, project_id, project_code, kind, name, role, description,"
                "  outfit, hair, weapon, voice, voice_type, voice_cast, clip_path,"
                "  sheet_path, ref_image, origin_project, data, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())"
                " ON DUPLICATE KEY UPDATE role=VALUES(role),"
                " description=VALUES(description), outfit=VALUES(outfit),"
                " hair=VALUES(hair), weapon=VALUES(weapon), voice=VALUES(voice),"
                " voice_type=VALUES(voice_type), voice_cast=VALUES(voice_cast),"
                " clip_path=VALUES(clip_path),"
                " sheet_path=VALUES(sheet_path), ref_image=VALUES(ref_image),"
                " origin_project=VALUES(origin_project),"
                " data=VALUES(data), updated_at=NOW()",
                (next_id(), project_id, code, r["kind"], r["name"], r["role"],
                 r["description"], r["outfit"], r["hair"], r["weapon"], r["voice"],
                 r["voice_type"], r["voice_cast"], r["clip_path"],
                 r["sheet_path"], r["ref_image"], r.get("origin_project"),
                 _jtext(r["data"])))
            keep.append((r["kind"], r["name"]))
        # 清理已从文档移除的资产（连同其章节关联）
        for aid, kind, name in (self._exec(
                f"SELECT id, kind, name FROM {self.prefix}asset WHERE project_id=%s",
                (project_id,), fetch="all") or ()):
            if (kind, name) not in keep:
                self._exec(f"DELETE FROM {self.prefix}chapter_asset WHERE asset_id=%s", (aid,))
                self._exec(f"DELETE FROM {self.prefix}asset WHERE id=%s", (aid,))

    # ---- 音色档案分解：voice_bank.casts → kn_voice_cast ----
    def _sync_voice_casts(self, project_id: int, code: str, data: dict) -> None:
        casts = ((data.get("voice_bank") or {}).get("casts") or [])
        keep = []
        for c in casts:
            if not c.get("id"):
                continue
            self._exec(
                f"INSERT INTO {self.prefix}voice_cast"
                " (id, project_id, project_code, cast_id, owner, mode, voice_type,"
                "  alias, prompt, clip_path, created_at, used_at, data)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON DUPLICATE KEY UPDATE owner=VALUES(owner), mode=VALUES(mode),"
                " voice_type=VALUES(voice_type), alias=VALUES(alias),"
                " prompt=VALUES(prompt), clip_path=VALUES(clip_path),"
                " used_at=VALUES(used_at), data=VALUES(data)",
                (next_id(), project_id, code, c["id"], c.get("owner"),
                 c.get("mode"), c.get("voice_type"), c.get("alias"), c.get("prompt"),
                 c.get("clip"), _dt(c.get("at")), _dt(c.get("used_at")), _jtext(c)))
            keep.append(c["id"])
        # 档案删除走引用闸，能删到这一步的必然已无人引用，库行随之清掉
        for row in (self._exec(
                f"SELECT id, cast_id FROM {self.prefix}voice_cast WHERE project_id=%s",
                (project_id,), fetch="all") or ()):
            if row[1] not in keep:
                self._exec(f"DELETE FROM {self.prefix}voice_cast WHERE id=%s", (row[0],))

    def _upsert_chapter(self, pcode: str, code: str, data: dict) -> None:
        meta = chapter_meta(self.root, pcode, code, data)
        project_id = self._project_db_id(pcode)
        doc = json.dumps(data, ensure_ascii=False)
        self._exec(
            f"INSERT INTO {self.prefix}chapter"
            " (id, project_id, project_code, code, title, status, motion, shots,"
            "  duration, video_path, animatic_path, animatic_state, cover, cost, data,"
            "  created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE title=VALUES(title), status=VALUES(status),"
            " motion=VALUES(motion), shots=VALUES(shots), duration=VALUES(duration),"
            " video_path=VALUES(video_path), animatic_path=VALUES(animatic_path),"
            " animatic_state=VALUES(animatic_state), cover=VALUES(cover),"
            " cost=VALUES(cost), data=VALUES(data),"
            " updated_at=VALUES(updated_at)",
            (next_id(), project_id, pcode, code,
             meta["title"], meta["status"], meta["motion"], meta["shots"],
             meta["duration"], meta["video_path"],
             meta.get("animatic_path"), meta.get("animatic_state"),
             (data.get("cover") or {}).get("primary")
             if isinstance(data.get("cover"), dict) else None,
             _jtext(meta["cost"], 512), doc,
             _dt((data.get("chapter") or {}).get("created_at")), _sync_at()))
        chapter_id = self._exec(
            f"SELECT id FROM {self.prefix}chapter WHERE project_id=%s AND code=%s",
            (project_id, code), fetch="one")[0]
        shot_ids = self._sync_shots(chapter_id, project_id, pcode, code, data)
        self._sync_chapter_assets(chapter_id, project_id, data)
        self._sync_comments(chapter_id, project_id, data, shot_ids)

    # ---- 分镜分解：shots[] → kn_shot（说明/文案/提示词/审阅状态/产物路径 + 版本栈） ----
    def _sync_shots(self, chapter_id: int, project_id: int,
                    pcode: str, code: str, data: dict) -> None:
        from ..review import STAGES as RV_STAGES, get_note, get_state, is_omitted
        adir = self.root / pcode / "chapters" / f"{code}_work" / "audio"
        voices = data.get("voices") or {}
        keep, ids = [], {}
        for s in data.get("shots") or []:
            no = s.get("id")
            if no is None:
                continue
            wav = adir / f"shot_{no}.wav"
            spk = s.get("speaker")
            notes = "；".join(f"{st}: {get_note(s, st)}"
                              for st in RV_STAGES if get_note(s, st)) or None
            self._exec(
                f"INSERT INTO {self.prefix}shot"
                " (id, chapter_id, project_id, project_code, chapter_code, shot_no,"
                "  speaker, voice, framing, camera, duration, narration, narration_en,"
                "  caption,"
                "  image_prompt, image_prompt_en, video_prompt, video_prompt_en,"
                "  negative_prompt, status,"
                "  omitted, review_image, review_audio, review_clip, review_note,"
                "  image_path, images, image_candidates, picked_no, stale_refs,"
                "  audio_path, clip_path, clips, characters, props,"
                "  rank_no, title, attribution, data, created_at, updated_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "         %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "         NOW(),NOW())"
                " ON DUPLICATE KEY UPDATE speaker=VALUES(speaker), voice=VALUES(voice),"
                " framing=VALUES(framing), camera=VALUES(camera), duration=VALUES(duration),"
                " narration=VALUES(narration), narration_en=VALUES(narration_en),"
                " caption=VALUES(caption),"
                " image_prompt=VALUES(image_prompt), image_prompt_en=VALUES(image_prompt_en),"
                " video_prompt=VALUES(video_prompt), video_prompt_en=VALUES(video_prompt_en),"
                " negative_prompt=VALUES(negative_prompt),"
                " status=VALUES(status), omitted=VALUES(omitted),"
                " review_image=VALUES(review_image), review_audio=VALUES(review_audio),"
                " review_clip=VALUES(review_clip), review_note=VALUES(review_note),"
                " image_path=VALUES(image_path), images=VALUES(images),"
                " image_candidates=VALUES(image_candidates), picked_no=VALUES(picked_no),"
                " stale_refs=VALUES(stale_refs),"
                " audio_path=VALUES(audio_path), clip_path=VALUES(clip_path), clips=VALUES(clips),"
                " characters=VALUES(characters), props=VALUES(props), rank_no=VALUES(rank_no),"
                " title=VALUES(title), attribution=VALUES(attribution), data=VALUES(data),"
                " updated_at=NOW()",
                (next_id(), chapter_id, project_id, pcode, code, no,
                 spk, s.get("voice") or (voices.get(spk) if spk else None),
                 s.get("framing"), s.get("camera"), s.get("dur"),
                 s.get("narration"), s.get("narration_en"), s.get("caption"),
                 s.get("image_prompt"), s.get("image_prompt_en"),
                 s.get("video_prompt"), s.get("video_prompt_en"),
                 (s.get("negative_prompt") or "")[:500] or None, s.get("status"),
                 int(is_omitted(s)), get_state(s, "image"), get_state(s, "audio"),
                 get_state(s, "clip"), notes[:500] if notes else None,
                 s.get("image"), _jtext(s.get("images")),
                 _jtext(s.get("image_candidates")), s.get("image_picked"),
                 _jtext(s.get("stale_refs"), 500),
                 str(wav) if wav.is_file() else None,
                 s.get("clip"), _jtext(s.get("clips")),
                 _jtext(s.get("characters"), 500), _jtext(s.get("props"), 500),
                 s.get("rank"), s.get("title"), s.get("attribution"),
                 json.dumps(s, ensure_ascii=False)))
            keep.append(no)
            sid = self._exec(
                f"SELECT id FROM {self.prefix}shot WHERE chapter_id=%s AND shot_no=%s",
                (chapter_id, no), fetch="one")[0]
            ids[no] = sid
            self._sync_shot_versions(sid, chapter_id, project_id, s)
        # 清理已从文档移除的分镜（连同其版本谱系）
        for (sid, no) in (self._exec(
                f"SELECT id, shot_no FROM {self.prefix}shot WHERE chapter_id=%s",
                (chapter_id,), fetch="all") or ()):
            if no not in keep:
                self._exec(f"DELETE FROM {self.prefix}shot_version WHERE shot_id=%s", (sid,))
                self._exec(f"DELETE FROM {self.prefix}shot WHERE id=%s", (sid,))
        return ids

    # ---- 版本栈：shots[].versions → kn_shot_version（归档条目不可变，只增不改） ----
    def _sync_shot_versions(self, shot_id: int, chapter_id: int,
                            project_id: int, s: dict) -> None:
        for stage, entries in (s.get("versions") or {}).items():
            for e in entries or []:
                self._exec(
                    f"INSERT INTO {self.prefix}shot_version"
                    " (id, shot_id, chapter_id, project_id, stage, version_no,"
                    "  files, reason, params, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE files=VALUES(files), reason=VALUES(reason),"
                    " params=VALUES(params)",
                    (next_id(), shot_id, chapter_id, project_id, stage, e.get("v"),
                     _jtext(e.get("files")), (e.get("reason") or "")[:500] or None,
                     _jtext(e.get("params")), _dt(e.get("at"))))

    # ---- 锚定评论：章节级 + 逐镜 comments[] → kn_comment（uk 文档ID，删稿清行） ----
    def _sync_comments(self, chapter_id: int, project_id: int,
                       data: dict, shot_ids: dict) -> None:
        pools = [(None, data.get("comments") or [])] + \
                [(s.get("id"), s.get("comments") or []) for s in data.get("shots") or []]
        keep = []
        for shot_no, comments in pools:
            for c in comments:
                doc_id = str(c.get("id") or "")
                if not doc_id:
                    continue
                self._exec(
                    f"INSERT INTO {self.prefix}comment"
                    " (id, doc_id, project_id, chapter_id, shot_id, shot_no, stage,"
                    "  content, anchor_x, anchor_y, anchor_time, created_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE content=VALUES(content),"
                    " anchor_x=VALUES(anchor_x),"
                    " anchor_y=VALUES(anchor_y), anchor_time=VALUES(anchor_time)",
                    (next_id(), doc_id, project_id, chapter_id,
                     shot_ids.get(shot_no), shot_no, c.get("stage"),
                     (c.get("text") or "")[:1000], c.get("x"), c.get("y"), c.get("t"),
                     _dt(c.get("at"))))
                keep.append(doc_id)
        for (rid, doc_id) in (self._exec(
                f"SELECT id, doc_id FROM {self.prefix}comment WHERE chapter_id=%s",
                (chapter_id,), fetch="all") or ()):
            if doc_id not in keep:
                self._exec(f"DELETE FROM {self.prefix}comment WHERE id=%s", (rid,))

    # ---- 章节↔资产关联：章节继承的角色/道具/场景 → kn_chapter_asset ----
    def _sync_chapter_assets(self, chapter_id: int, project_id: int, data: dict) -> None:
        # 角色/道具按章节副本的名字匹配（无副本 = 继承项目全部）；场景设定全章节共用
        chars = {c.get("name") for c in (data.get("characters") or [])} or None
        props = {p.get("name") for p in (data.get("props") or [])} or None
        keep = []
        for aid, kind, name in (self._exec(
                f"SELECT id, kind, name FROM {self.prefix}asset WHERE project_id=%s",
                (project_id,), fetch="all") or ()):
            if kind == "character" and chars is not None and name not in chars:
                continue
            if kind in ("prop", "weapon") and props is not None and name not in props:
                continue
            self._exec(
                f"INSERT INTO {self.prefix}chapter_asset"
                " (id, chapter_id, asset_id, project_id, created_at)"
                " VALUES (%s,%s,%s,%s,NOW())"
                " ON DUPLICATE KEY UPDATE asset_id=asset_id",
                (next_id(), chapter_id, aid, project_id))
            keep.append(aid)
        for (lid, aid) in (self._exec(
                f"SELECT id, asset_id FROM {self.prefix}chapter_asset WHERE chapter_id=%s",
                (chapter_id,), fetch="all") or ()):
            if aid not in keep:
                self._exec(f"DELETE FROM {self.prefix}chapter_asset WHERE id=%s", (lid,))

    # ========================================================================
    # 项目
    # ========================================================================
    def list_projects(self) -> list[dict]:
        self._db()
        local = {d.get("id"): d for d in super().list_projects() if d.get("id")}
        out, seen = [], set()
        for (code,) in (self._exec(
                f"SELECT code FROM {self.prefix}project ORDER BY code",
                fetch="all") or ()):
            seen.add(code)
            # 冲突协调必须与 load_project 同一份「新者赢」判据（_row_newer）——
            # 本方法是 Studio 开首页的第一条读路径，先于任何 load 运行；
            # 在此另写一套「文件为准」的话，换机/恢复旧副本时过期文件会覆写
            # 库中较新行，且 updated_at 随之写旧，此后再也检测不出覆盖发生过。
            data = self.load_project(code)
            if data is not None:
                out.append(data)
        for code, data in local.items():           # 库里没有 → 懒迁移入库
            if code not in seen:
                self._upsert_project(code, data)
                for ch in data.get("chapters", []):
                    cdata = super().load_chapter(code, ch.get("id"))
                    if cdata:
                        self._upsert_chapter(code, ch["id"], cdata)
                out.append(data)
        out.sort(key=lambda d: d.get("id") or "")
        return out

    def project_exists(self, code: str) -> bool:
        """id 是否已被占用：本地文件命中即真，否则查库。

        本地 JSON 只是工作副本、库才是恢复源，所以文件不在不等于 id 空闲——
        空盘上按文件判会放行同名新建，`save_project` 随即覆盖 data 列，
        `_sync_assets` 与 `_sync_voice_casts` 连带删光该项目的派生行。
        不按 is_deleted 过滤：逻辑删除的项目目录与库行都还在，id 不能被复用。"""
        if super().project_exists(code):
            return True
        return self._exec(f"SELECT 1 FROM {self.prefix}project WHERE code=%s",
                          (code,), fetch="one") is not None

    def chapter_exists(self, pcode: str, code: str) -> bool:
        """章节 id 是否已被占用。同 `project_exists`：文件命中即真，否则查库。

        `list_projects`/`load_project` 只把项目文档回填到盘上，章节文件要点名
        `load_chapter` 才 rehydrate，所以「项目在盘、章节只在库里」是常态。"""
        if super().chapter_exists(pcode, code):
            return True
        return self._exec(
            f"SELECT 1 FROM {self.prefix}chapter WHERE project_code=%s AND code=%s",
            (pcode, code), fetch="one") is not None

    @staticmethod
    def _row_newer(db_updated_at, local_file: Path) -> bool:
        """库行是否明显比本地文件新（>2s 容差，平手偏向文件——保守不打断本地工作流）。
        用于双写冲突协调「新者赢」：换机/旧工作区的过期文件
        不会无条件覆盖库中较新状态。

        参与本判据的 updated_at 列一律取写入方的客户端墙钟（`_sync_at`，项目侧取自
        文档 updated_at 亦同源），不得改用 `NOW()`——理由见 `_sync_at`。
        已知边界：多台客户端分处不同时区共用一库时判据仍按时区差偏移，
        DATETIME 列无处存偏移量。"""
        if db_updated_at is None:
            return False
        try:
            return db_updated_at.timestamp() > local_file.stat().st_mtime + 2.0
        except (OSError, AttributeError):
            return False

    def load_project(self, code: str) -> dict | None:
        self._db()
        row = self._exec(
            f"SELECT data, updated_at FROM {self.prefix}project WHERE code=%s",
            (code,), fetch="one")
        db_data = json.loads(row[0]) if row else None
        local = super().load_project(code)
        if local is not None:
            if db_data is not None and db_data != local:
                if self._row_newer(row[1], self._pfile(code)):   # 新者赢：库新 → 库为准
                    print(f"  ⚠ 项目 {code}: 数据库比本地文件新，已用库中版本刷新本地副本")
                    super().save_project(code, db_data)
                    return db_data
                self._upsert_project(code, local)                # 文件新（或平手）→ 上行入库
            return local
        if db_data is not None:                    # 从库恢复
            super().save_project(code, db_data)
        return db_data

    def save_project(self, code: str, data: dict) -> None:
        super().save_project(code, data)           # 本地工作副本
        self._db()
        self._upsert_project(code, data)           # 数据库持久层

    # ========================================================================
    # 章节
    # ========================================================================
    def load_chapter(self, pcode: str, code: str) -> dict | None:
        self._db()
        row = self._exec(
            f"SELECT data, updated_at FROM {self.prefix}chapter"
            " WHERE project_code=%s AND code=%s",
            (pcode, code), fetch="one")
        db_data = json.loads(row[0]) if row else None
        local = super().load_chapter(pcode, code)
        if local is not None:
            if db_data is not None and db_data != local:
                if self._row_newer(row[1], self._cfile(pcode, code)):  # 新者赢
                    print(f"  ⚠ 章节 {pcode}/{code}: 数据库比本地文件新，"
                          "已用库中版本刷新本地副本")
                    super().save_chapter(pcode, code, db_data)
                    return db_data
                self._upsert_chapter(pcode, code, local)   # 文件新（Skill/引擎带外写入）→ 上行
            return local
        if db_data is not None:                    # 从库恢复
            super().save_chapter(pcode, code, db_data)
        return db_data

    def save_chapter(self, pcode: str, code: str, data: dict, *, write_file: bool = True) -> None:
        super().save_chapter(pcode, code, data, write_file=write_file)
        self._db()
        self._upsert_chapter(pcode, code, data)

    def delete_chapter(self, pcode: str, code: str) -> None:
        self._db()
        row = self._exec(
            f"SELECT id FROM {self.prefix}chapter WHERE project_code=%s AND code=%s",
            (pcode, code), fetch="one")
        if row:
            cid = row[0]
            self._exec(f"DELETE FROM {self.prefix}comment WHERE chapter_id=%s", (cid,))
            self._exec(f"DELETE FROM {self.prefix}shot_version WHERE chapter_id=%s", (cid,))
            self._exec(f"DELETE FROM {self.prefix}shot WHERE chapter_id=%s", (cid,))
            self._exec(f"DELETE FROM {self.prefix}chapter_asset WHERE chapter_id=%s", (cid,))
            self._exec(f"DELETE FROM {self.prefix}chapter WHERE id=%s", (cid,))
        super().delete_chapter(pcode, code)

    # ========================================================================
    # 工作区级配置（模型覆盖层的跨机同步层）
    # ========================================================================
    def load_settings(self, scope: str, name: str, *,
                      local_file: Path | None = None) -> dict | None:
        self._db()
        row = self._exec(
            f"SELECT data, updated_at FROM {self.prefix}setting WHERE scope=%s AND name=%s",
            (scope, name), fetch="one")
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except ValueError:
            return None
        newer = self._row_newer(row[1], local_file) if local_file is not None else True
        return {"data": data, "newer": bool(newer)}

    def save_settings(self, scope: str, name: str, data: dict) -> None:
        """**密钥不入库**：库行随备份与多机同步走，一进去就等于换机把密钥也带走。
        密钥与连接段分两份文件存，正是为了这里可以整份上传、不做逐字段过滤——
        逐字段过滤只要漏一次就是把密钥送进这张表。"""
        if scope == "secrets":
            raise ConfigError("密钥绝不入库：请写本机密钥文件（config/secrets.local.json）")
        self._db()
        self._exec(
            f"INSERT INTO {self.prefix}setting (id, scope, name, data, updated_at)"
            " VALUES (%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE data=VALUES(data), updated_at=VALUES(updated_at)",
            (next_id(), scope, name, json.dumps(data, ensure_ascii=False), _sync_at()))

    # ========================================================================
    # 自述 / 统计
    # ========================================================================
    def describe(self) -> str:
        c = self.cfg
        return (f"mysql · {c.get('user')}@{c.get('host')}:{c.get('port')}"
                f"/{c.get('database')} (prefix={self.prefix}) · 镜像 {self.root}")

    def counts(self) -> dict:
        self._db()
        one = lambda t, w="": self._exec(  # noqa: E731
            f"SELECT COUNT(*) FROM {self.prefix}{t} {w}", fetch="one")[0]
        return {"projects": one("project"), "chapters": one("chapter"),
                "rendered": one("chapter", "WHERE status='rendered'"),
                "assets": one("asset"), "shots": one("shot")}
