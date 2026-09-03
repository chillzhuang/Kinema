-- ============================================================================
-- kinema · MySQL 完整建库建表脚本
-- 生成: python3 -m kinema db schema（2026-09-03）
-- 单一真源: engine/kinema/storage/mysql.py 的 _SCHEMA —— 请勿手改本文件，
--           改 schema 后重新执行上述命令再生成。
-- 说明: 雪花ID单主键 · 业务标识唯一键 · 逻辑外键(无物理FK) · 全表全列 COMMENT
--       媒体只存路径 · 完整文档存 data 列（可整体恢复）
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `kinema` DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `kinema`;

CREATE TABLE IF NOT EXISTS kn_project (
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
  COMMENT='kinema 项目（系列）：总体设计+角色预设+章节索引，媒体只存路径';;

CREATE TABLE IF NOT EXISTS kn_asset (
  id           BIGINT       NOT NULL               COMMENT '主键（雪花ID）',
  project_id   BIGINT       NOT NULL               COMMENT '所属项目主键（kn_project.id，逻辑外键）',
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
  voice_cast   VARCHAR(32)  DEFAULT NULL           COMMENT '在用音色档案号（kn_voice_cast.cast_id；空=手工指派、未入档）',
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
  COMMENT='kinema 资产：角色/道具/武器/场景设定（一致性根基），设定图只存路径';;

CREATE TABLE IF NOT EXISTS kn_chapter (
  id           BIGINT       NOT NULL               COMMENT '主键（雪花ID）',
  project_id   BIGINT       NOT NULL               COMMENT '所属项目主键（kn_project.id，逻辑外键）',
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
  COMMENT='kinema 章节（一条视频）：渲染状态/产物路径/成本；分镜明细见 kn_shot';;

CREATE TABLE IF NOT EXISTS kn_chapter_asset (
  id           BIGINT      NOT NULL                COMMENT '主键（雪花ID）',
  chapter_id   BIGINT      NOT NULL                COMMENT '章节主键（kn_chapter.id，逻辑外键）',
  asset_id     BIGINT      NOT NULL                COMMENT '资产主键（kn_asset.id，逻辑外键）',
  project_id   BIGINT      NOT NULL                COMMENT '所属项目主键（冗余，便于按项目清理/统计）',
  created_at   DATETIME    DEFAULT NULL            COMMENT '创建时间（首次关联）',
  PRIMARY KEY (id),
  UNIQUE KEY uk_chapter_asset (chapter_id, asset_id),
  KEY idx_ca_asset (asset_id),
  KEY idx_ca_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 章节↔资产关联：本章节继承/引用了哪些角色/道具/场景设定';;

CREATE TABLE IF NOT EXISTS kn_shot (
  id            BIGINT       NOT NULL              COMMENT '主键（雪花ID）',
  chapter_id    BIGINT       NOT NULL              COMMENT '所属章节主键（kn_chapter.id，逻辑外键）',
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
  COMMENT='kinema 分镜明细：每行一镜——说明/文案/提示词/审阅状态/图片/配音/视频片段路径';;

CREATE TABLE IF NOT EXISTS kn_shot_version (
  id          BIGINT        NOT NULL               COMMENT '主键（雪花ID）',
  shot_id     BIGINT        NOT NULL               COMMENT '所属分镜主键（kn_shot.id，逻辑外键）',
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
  COMMENT='kinema 产物版本栈：重生成不覆盖，归档谱系可回滚可审计';;

CREATE TABLE IF NOT EXISTS kn_comment (
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
  COMMENT='kinema 锚定评论：帧/像素/时间锚定到具体产物';;

-- voice_bank.casts 的派生行：项目 upsert 时全量同步（_sync_voice_casts），引擎读侧不用本表，
-- 供库内查询音色档案台账。
CREATE TABLE IF NOT EXISTS kn_voice_cast (
  id           BIGINT       NOT NULL               COMMENT '主键（雪花ID）',
  project_id   BIGINT       NOT NULL               COMMENT '所属项目主键（kn_project.id，逻辑外键）',
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
  COMMENT='kinema 音色档案：一个实体用过的每一把声音各一条（可回听·可换回·删除前查引用）';;

CREATE TABLE IF NOT EXISTS kn_setting (
  id         BIGINT      NOT NULL                COMMENT '主键（雪花ID）',
  scope      VARCHAR(32) NOT NULL                COMMENT '配置域：models=模型连接与激活项',
  name       VARCHAR(64) NOT NULL                COMMENT '配置名（同域内唯一；models 域固定 overlay）',
  data       LONGTEXT    NOT NULL                COMMENT '配置文档全文（JSON）。**绝不含密钥值**——库行随备份与多机同步走，密钥只留本机 gitignored 文件；分两份文件存正是为了这份可以整份上传、不需要任何逐字段过滤',
  updated_at DATETIME    DEFAULT NULL            COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_setting_scope_name (scope, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='kinema 工作区级配置（不属于任何项目）：模型覆盖层的跨机同步层，密钥不入库';;

